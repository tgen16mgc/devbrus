"""CloakBrowser Manager — FastAPI application.

Serves the React dashboard (static files) and provides a REST API
for browser profile management with live VNC viewing.
"""

from __future__ import annotations

import asyncio
import base64
import csv
import hmac
import inspect as pyinspect
import io
import logging
import os
import struct
import shutil
from contextlib import asynccontextmanager
from http.cookies import SimpleCookie
from pathlib import Path
from urllib.parse import urlparse

import httpx
from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
import starlette.requests
from pydantic import ValidationError
from starlette.types import ASGIApp, Receive, Scope, Send

from . import database as db
from . import native_window_helper
from .browser_manager import BrowserManager, _normalize_proxy, _validate_proxy
from .models import (
    ClipboardRequest,
    LaunchResponse,
    LayoutCreate,
    LayoutResponse,
    LayoutUpdate,
    LoginRequest,
    MetadataImportRequest,
    OPERATOR_CSV_MAX_ROWS,
    OperatorAutomationRequest,
    OperatorBulkRequest,
    OperatorImportCsvRequest,
    OperatorNativeGridRequest,
    ProfileCreate,
    ProfileResponse,
    ProfileStatusResponse,
    ProfileUpdate,
    StatusResponse,
    TagResponse,
)

logger = logging.getLogger("cloakbrowser.manager")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
logging.getLogger("websockets").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("asyncio").setLevel(logging.WARNING)

# Optional authentication via AUTH_TOKEN env var.
# If not set, all routes are open (local dev). If set, all /api/* routes
# (except /api/auth/* and /api/status) require Bearer token or cookie.
AUTH_TOKEN: str | None = os.environ.get("AUTH_TOKEN") or None

# Paths that bypass authentication even when AUTH_TOKEN is set
_AUTH_EXEMPT = frozenset({"/api/auth/status", "/api/auth/login", "/api/status"})


def _check_auth(scope: Scope) -> bool:
    """Check if the request has a valid auth token (header or cookie)."""
    # Check Authorization: Bearer <token> header
    for key, val in scope.get("headers", []):
        if key == b"authorization":
            auth_value = val.decode()
            if auth_value.startswith("Bearer "):
                token = auth_value[7:]
                if token and hmac.compare_digest(token, AUTH_TOKEN):
                    return True
            break

    # Check auth_token cookie
    for key, val in scope.get("headers", []):
        if key == b"cookie":
            cookies = SimpleCookie()
            cookies.load(val.decode())
            if "auth_token" in cookies:
                cookie_val = cookies["auth_token"].value
                if cookie_val and hmac.compare_digest(cookie_val, AUTH_TOKEN):
                    return True
            break

    return False


def _is_https(request: Request) -> bool:
    """Check if the original client connection was HTTPS (via reverse proxy header)."""
    proto = request.headers.get("x-forwarded-proto", "")
    return "https" in proto


async def _check_websocket_origin(websocket: WebSocket) -> bool:
    """Reject cross-origin WebSocket connections (CSWSH protection).

    Browsers always send an Origin header on WebSocket upgrades.
    Non-browser clients (Playwright, curl) typically don't — those are allowed.
    If Origin is present, its host must match the request Host header.
    """
    origin = None
    host = None
    for key, val in websocket.scope.get("headers", []):
        if key == b"origin":
            origin = val.decode("latin-1")
        elif key == b"host":
            host = val.decode("latin-1")

    # No Origin header → non-browser client (Playwright, Puppeteer) → allow
    if not origin:
        return True

    # Parse origin to extract host:port
    try:
        parsed = urlparse(origin)
        origin_host = parsed.hostname or ""
        origin_port = parsed.port
    except ValueError:
        logger.warning("WebSocket origin malformed: %s", origin)
        await websocket.close(code=4403, reason="Origin not allowed")
        return False
    # Build origin netloc (host:port or just host if default port)
    if origin_port and origin_port not in (80, 443):
        origin_netloc = f"{origin_host}:{origin_port}"
    else:
        origin_netloc = origin_host

    if not host:
        return True  # no Host header to compare against

    # Strip default port from Host too (some proxies send "example.com:443")
    host_normalized = host
    if host.endswith(":80") or host.endswith(":443"):
        host_normalized = host.rsplit(":", 1)[0]

    if origin_netloc == host_normalized:
        return True

    logger.warning("WebSocket origin mismatch: origin=%s host=%s", origin, host)
    await websocket.close(code=4403, reason="Origin not allowed")
    return False


class AuthMiddleware:
    """Raw ASGI middleware for optional token auth.

    Uses raw ASGI instead of BaseHTTPMiddleware because the latter
    breaks WebSocket routes (wraps request body, preventing WS upgrade).
    """

    def __init__(self, app: ASGIApp):
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send):
        # Pass through if auth disabled, or non-HTTP/WS scope (e.g. lifespan)
        if not AUTH_TOKEN or scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return

        path = scope["path"]

        # Skip auth for exempt endpoints and non-API paths (static frontend)
        if path in _AUTH_EXEMPT or not path.startswith("/api/"):
            await self.app(scope, receive, send)
            return

        if _check_auth(scope):
            await self.app(scope, receive, send)
            return

        # Reject — unauthenticated
        if scope["type"] == "websocket":
            # ASGI requires receiving websocket.connect before sending close
            await receive()
            await send({"type": "websocket.close", "code": 4401, "reason": "Unauthorized"})
        else:
            response = JSONResponse({"detail": "Unauthorized"}, status_code=401)
            await response(scope, receive, send)


# Singleton browser manager
browser_mgr = BrowserManager()

# Frontend build directory (React production build)
FRONTEND_DIR = Path(__file__).parent.parent / "frontend" / "dist"


# ---------------------------------------------------------------------------
# RFB server message translator — KasmVNC BinaryClipboard → standard RFB
# ---------------------------------------------------------------------------


def _parse_kasmvnc_clipboard(data: bytes) -> str | None:
    """Extract text/plain from KasmVNC BinaryClipboard (type 180).

    Format: type(1) + action(1) + flags(4) + entries...
    Each entry: mime_len(u8) + mime(N) + data_len(u32 BE) + data(M)
    """
    if len(data) < 7:
        return None
    offset = 6  # skip type(1) + action(1) + flags(4)
    while offset < len(data):
        if offset + 1 > len(data):
            break
        mime_len = data[offset]
        offset += 1
        if offset + mime_len > len(data):
            break
        mime_type = data[offset:offset + mime_len]
        offset += mime_len
        if offset + 4 > len(data):
            break
        data_len = struct.unpack_from(">I", data, offset)[0]
        offset += 4
        if mime_type == b"text/plain":
            end = min(offset + data_len, len(data))
            return data[offset:end].decode("utf-8", errors="replace")
        offset += data_len
    return None


def _build_server_cut_text(text: str) -> bytes:
    """Build standard RFB ServerCutText (type 3) message.

    RFB spec mandates Latin-1 encoding for ServerCutText.
    Characters outside Latin-1 (CJK, emoji, etc.) are replaced with '?'.
    """
    text_bytes = text.encode("latin-1", errors="replace")
    return struct.pack(">BxxxI", 3, len(text_bytes)) + text_bytes


# ---------------------------------------------------------------------------
# RFB client message filter — strip extension types KasmVNC doesn't support
# ---------------------------------------------------------------------------
# noVNC v1.4 batches multiple RFB messages into one WebSocket frame.
# KasmVNC 1.3.3 crashes on unsupported types (150, 248, etc.).
# We parse message boundaries using known sizes and keep only standard types.

# Client→server message sizes (fixed, except 2 and 6 which encode length)
_RFB_MSG_SIZE: dict[int, int | None] = {
    0: 20,    # SetPixelFormat
    2: None,  # SetEncodings — 4 + numEncodings*4 (rewritten to strip bad pseudo-encodings)
    3: 10,    # FramebufferUpdateRequest
    4: 8,     # KeyEvent
    5: 6,     # PointerEvent
    6: None,  # ClientCutText — 8 + length
}

# Extension types that noVNC sends — known sizes so we can skip past them
# instead of breaking and dropping all trailing data in the frame.
_RFB_EXTENSION_SIZE: dict[int, int] = {
    150: 10,  # EnableContinuousUpdates (1+1+2+2+2+2)
    248: 10,  # QEMU-like key event (observed from noVNC 1.4.0)
    252: 4,   # xvp (1+1+1+1)
    255: 4,   # QEMU audio control (1+1+2) — noVNC QEMUExtendedKeyEvent is actually 12
}

# Whitelist of encodings safe to send to KasmVNC.
# Instead of trying to blocklist problematic pseudo-encodings (error-prone —
# we had wrong numbers), we ONLY keep known-good encodings.
# Anything not on this list is stripped from SetEncodings.
_ALLOWED_ENCODINGS: set[int] = {
    # Framebuffer encodings (standard RFB)
    0,    # Raw
    1,    # CopyRect
    2,    # RRE
    5,    # Hextile
    7,    # Tight
    16,   # ZRLE
    # Safe pseudo-encodings
    -239,  # Cursor (0xFFFFFF11) — cursor shape
    -224,  # LastRect (0xFFFFFF20) — performance optimization
    # Tight quality/compress levels (these are just hints)
    *range(-32, -22),   # quality levels 0-9
    *range(-256, -246),  # compress levels 0-9
}


def _rfb_msg_length(data: bytes, offset: int) -> int | None:
    """Return total length of the RFB message at offset, or None if unrecognized."""
    if offset >= len(data):
        return None
    msg_type = data[offset]
    fixed = _RFB_MSG_SIZE.get(msg_type)
    if fixed is not None:
        return fixed
    remaining = len(data) - offset
    if msg_type == 2 and remaining >= 4:  # SetEncodings
        num_enc = struct.unpack_from(">H", data, offset + 2)[0]
        return 4 + num_enc * 4
    if msg_type == 6 and remaining >= 8:  # ClientCutText
        length = struct.unpack_from(">I", data, offset + 4)[0]
        return 8 + length
    # Known extension types — skip past them instead of giving up
    ext_size = _RFB_EXTENSION_SIZE.get(msg_type)
    if ext_size is not None:
        return ext_size
    return None  # truly unknown type


def _rewrite_set_encodings(data: bytes, offset: int, msg_len: int) -> bytes:
    """Keep only whitelisted encodings in a SetEncodings message."""
    _log = logging.getLogger("cloakbrowser.manager")
    num_enc = struct.unpack_from(">H", data, offset + 2)[0]
    kept = []
    stripped = []
    for i in range(num_enc):
        enc = struct.unpack_from(">i", data, offset + 4 + i * 4)[0]  # signed
        if enc in _ALLOWED_ENCODINGS:
            kept.append(enc)
        else:
            stripped.append(enc)
    if not stripped:
        return data[offset:offset + msg_len]
    _log.info("RFB filter: SetEncodings keeping %d: %s, stripped %d: %s", len(kept), kept, len(stripped), stripped)
    result = struct.pack(">BxH", 2, len(kept))
    for enc in kept:
        result += struct.pack(">i", enc)
    return result


def _rewrite_pointer_event(data: bytes, offset: int) -> bytes:
    """Convert standard 6-byte PointerEvent to KasmVNC's 11-byte format.

    Standard RFB:  [5:u8][mask:u8][x:u16][y:u16]          = 6 bytes
    KasmVNC:       [5:u8][mask:u16][x:u16][y:u16][sx:s16][sy:s16] = 11 bytes
    """
    mask = data[offset + 1]
    x = struct.unpack_from(">H", data, offset + 2)[0]
    y = struct.unpack_from(">H", data, offset + 4)[0]
    # Expand mask from u8 to u16.  Scroll deltas (sx, sy) are zero because
    # noVNC encodes scroll as button-mask bits (3=up, 4=down, 5=left, 6=right)
    # which pass through in the mask.  KasmVNC accepts mask-bit scroll on its
    # extended 11-byte format, so explicit deltas are unnecessary.
    return struct.pack(">BHHHhh", 5, mask, x, y, 0, 0)


def _filter_rfb_client_messages(data: bytes) -> bytes:
    """Parse concatenated RFB messages, keep only standard types (0-6).

    Rewrites PointerEvents from 6-byte standard to 11-byte KasmVNC format
    and strips unsupported pseudo-encodings from SetEncodings.
    """
    _log = logging.getLogger("cloakbrowser.manager")
    result = bytearray()
    offset = 0
    msg_idx = 0
    while offset < len(data):
        msg_type = data[offset]
        msg_len = _rfb_msg_length(data, offset)
        if msg_len is None:
            _log.info("RFB filter: DROPPING unknown type=%d at offset=%d/%d, skipping %d trailing bytes, hex=%s",
                       msg_type, offset, len(data), len(data) - offset, data[offset:offset+20].hex())
            break
        if offset + msg_len > len(data):
            # Incomplete message — DO NOT forward partial data, it desynchronizes
            # the RFB stream (KasmVNC buffers partial reads across frames).
            _log.warning("RFB filter: DROPPING incomplete type=%d need=%d have=%d — would desync stream",
                         msg_type, msg_len, len(data) - offset)
            break
        msg_idx += 1
        if msg_type in _RFB_MSG_SIZE:
            # Standard RFB type — keep (with rewrites for KasmVNC compatibility)
            _log.debug("RFB filter: KEEP type=%d len=%d at offset=%d (msg #%d in frame)", msg_type, msg_len, offset, msg_idx)
            if msg_type == 2:  # SetEncodings — whitelist safe encodings
                result.extend(_rewrite_set_encodings(data, offset, msg_len))
            elif msg_type == 5:  # PointerEvent — expand to KasmVNC's 11-byte format
                result.extend(_rewrite_pointer_event(data, offset))
            else:
                result.extend(data[offset:offset + msg_len])
        else:
            # Extension type (150, 248, etc.) — skip but continue parsing
            _log.debug("RFB filter: SKIP extension type=%d len=%d at offset=%d (msg #%d in frame)", msg_type, msg_len, offset, msg_idx)
        offset += msg_len
    if len(result) != len(data):
        _log.info("RFB filter: input=%d output=%d (delta %+d bytes)", len(data), len(result), len(result) - len(data))
    return bytes(result)


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    await browser_mgr.cleanup_stale()
    browser_mgr._auto_launch_task = asyncio.create_task(browser_mgr.auto_launch_all())
    logger.info("CloakBrowser Manager started")
    yield
    logger.info("Shutting down — stopping all browsers...")
    if browser_mgr._auto_launch_task and not browser_mgr._auto_launch_task.done():
        browser_mgr._auto_launch_task.cancel()
        await asyncio.gather(browser_mgr._auto_launch_task, return_exceptions=True)
    await browser_mgr.cleanup_all()


app = FastAPI(title="CloakBrowser Manager", lifespan=lifespan)
app.add_middleware(AuthMiddleware)


# ── Authentication ────────────────────────────────────────────────────────────


@app.get("/api/auth/status")
async def auth_status(request: starlette.requests.Request):
    """Check if auth is enabled and if the current request is authenticated.

    Exempt from auth middleware so the frontend can always call it.
    """
    authenticated = False
    if AUTH_TOKEN:
        authenticated = _check_auth(request.scope)
    return {"auth_required": AUTH_TOKEN is not None, "authenticated": authenticated}


@app.post("/api/auth/login")
async def auth_login(body: LoginRequest, request: Request, response: Response):
    if not AUTH_TOKEN:
        return {"ok": True}
    if not body.token or not hmac.compare_digest(body.token, AUTH_TOKEN):
        raise HTTPException(status_code=401, detail="Invalid token")
    is_https = _is_https(request)
    response.set_cookie(
        key="auth_token",
        value=AUTH_TOKEN,
        httponly=True,
        samesite="strict",
        secure=is_https,
        path="/",
    )
    return {"ok": True}


@app.post("/api/auth/logout")
async def auth_logout(request: Request, response: Response):
    is_https = _is_https(request)
    response.delete_cookie(
        key="auth_token", path="/", secure=is_https, samesite="strict",
    )
    return {"ok": True}


# ── Profile CRUD ──────────────────────────────────────────────────────────────


@app.get("/api/profiles", response_model=list[ProfileResponse])
async def list_profiles():
    profiles = db.list_profiles()
    result = []
    for p in profiles:
        status = browser_mgr.get_status(p["id"])
        p["status"] = status["status"]
        p["vnc_ws_port"] = status["vnc_ws_port"]
        p["cdp_url"] = status["cdp_url"]
        p["tags"] = [TagResponse(**t) for t in p.get("tags", [])]
        result.append(ProfileResponse(**p))
    return result


@app.post("/api/profiles", response_model=ProfileResponse, status_code=201)
async def create_profile(req: ProfileCreate):
    data = req.model_dump()
    tags = data.pop("tags", None)
    if tags:
        data["tags"] = [t.model_dump() if hasattr(t, "model_dump") else t for t in tags]
    else:
        data["tags"] = []
    profile = db.create_profile(**data)
    status = browser_mgr.get_status(profile["id"])
    profile["status"] = status["status"]
    profile["vnc_ws_port"] = status["vnc_ws_port"]
    profile["cdp_url"] = status["cdp_url"]
    profile["tags"] = [TagResponse(**t) for t in profile.get("tags", [])]
    return ProfileResponse(**profile)


@app.get("/api/profiles/{profile_id}", response_model=ProfileResponse)
async def get_profile(profile_id: str):
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    status = browser_mgr.get_status(profile_id)
    profile["status"] = status["status"]
    profile["vnc_ws_port"] = status["vnc_ws_port"]
    profile["cdp_url"] = status["cdp_url"]
    profile["tags"] = [TagResponse(**t) for t in profile.get("tags", [])]
    return ProfileResponse(**profile)


@app.put("/api/profiles/{profile_id}", response_model=ProfileResponse)
async def update_profile(profile_id: str, req: ProfileUpdate):
    # Only pass fields that were explicitly set
    data = req.model_dump(exclude_unset=True)
    tags = data.pop("tags", None)
    if tags is not None:
        data["tags"] = [t.model_dump() if hasattr(t, "model_dump") else t for t in tags]
    profile = db.update_profile(profile_id, **data)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    status = browser_mgr.get_status(profile_id)
    profile["status"] = status["status"]
    profile["vnc_ws_port"] = status["vnc_ws_port"]
    profile["cdp_url"] = status["cdp_url"]
    profile["tags"] = [TagResponse(**t) for t in profile.get("tags", [])]
    return ProfileResponse(**profile)


@app.delete("/api/profiles/{profile_id}")
async def delete_profile(profile_id: str):
    # Stop browser if running
    if profile_id in browser_mgr.running:
        await browser_mgr.stop(profile_id)

    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")

    user_data_dir = Path(profile["user_data_dir"])

    # DB first — if this fails, filesystem is untouched
    db.delete_profile(profile_id)

    # Then clean up disk
    if user_data_dir.exists():
        shutil.rmtree(user_data_dir, ignore_errors=True)

    return {"ok": True}


# ── Launch / Stop ─────────────────────────────────────────────────────────────


@app.post("/api/profiles/{profile_id}/launch", response_model=LaunchResponse)
async def launch_profile(profile_id: str):
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    if profile_id in browser_mgr.running:
        raise HTTPException(status_code=409, detail="Profile is already running")

    try:
        running = await browser_mgr.launch(profile)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Failed to launch profile %s: %s", profile_id, exc)
        raise HTTPException(status_code=500, detail="Failed to launch browser")

    return LaunchResponse(
        profile_id=profile_id,
        status="running",
        vnc_ws_port=running.ws_port,
        display=running.display_label,
        cdp_url=f"/api/profiles/{profile_id}/cdp",
    )


@app.post("/api/profiles/{profile_id}/stop")
async def stop_profile(profile_id: str):
    if profile_id not in browser_mgr.running:
        raise HTTPException(status_code=404, detail="Profile is not running")
    await browser_mgr.stop(profile_id)
    return {"ok": True}


@app.get("/api/profiles/{profile_id}/status", response_model=ProfileStatusResponse)
async def get_profile_status(profile_id: str):
    profile = db.get_profile(profile_id)
    if not profile:
        raise HTTPException(status_code=404, detail="Profile not found")
    status = browser_mgr.get_status(profile_id)
    return ProfileStatusResponse(**status)


# ── System Status ─────────────────────────────────────────────────────────────


@app.get("/api/status", response_model=StatusResponse)
async def get_system_status():
    from cloakbrowser.config import CHROMIUM_VERSION

    profiles = db.list_profiles()
    return StatusResponse(
        running_count=len(browser_mgr.running),
        binary_version=CHROMIUM_VERSION,
        profiles_total=len(profiles),
    )


# ── Operator Cockpit ─────────────────────────────────────────────────────────


def _serialize_profile(profile: dict) -> dict:
    data = dict(profile)
    data.pop("user_data_dir", None)
    data.pop("status", None)
    data.pop("vnc_ws_port", None)
    data.pop("cdp_url", None)
    return data


def _profile_name_exists(name: str) -> bool:
    return db.get_profile_by_name(name) is not None


def _proxy_status(proxy: str | None, validate: bool) -> str:
    if not proxy:
        return "no_proxy"
    return "format_valid" if validate else "unchecked"


def _raw_import_name(record: dict) -> str | None:
    raw_name = record.get("name")
    return raw_name if isinstance(raw_name, str) else None


async def _run_limited(items: list[str], concurrency: int, fn):
    if not items:
        return []
    results: list[dict | None] = [None] * len(items)
    next_index = 0
    lock = asyncio.Lock()

    async def worker():
        nonlocal next_index
        while True:
            async with lock:
                if next_index >= len(items):
                    return
                idx = next_index
                next_index += 1
            results[idx] = await fn(items[idx])

    worker_count = min(max(1, concurrency), len(items))
    await asyncio.gather(*(worker() for _ in range(worker_count)))
    return [result for result in results if result is not None]


@app.post("/api/operator/import-csv")
async def operator_import_csv(req: OperatorImportCsvRequest):
    created_ids: list[str] = []
    skipped: list[dict] = []
    invalid: list[dict] = []

    try:
        reader = csv.DictReader(io.StringIO(req.csv_text))
        fieldnames = reader.fieldnames
        rows = list(reader)
    except csv.Error as exc:
        raise HTTPException(status_code=400, detail=f"Invalid CSV: {exc}") from exc

    if not fieldnames:
        db.record_event("csv_import", {"created": 0, "skipped": 0, "invalid": 1})
        return {
            "created": 0,
            "skipped": skipped,
            "invalid": [{"row": 1, "reason": "Missing CSV header"}],
            "profile_ids": created_ids,
        }

    if len(rows) > OPERATOR_CSV_MAX_ROWS:
        raise HTTPException(
            status_code=400,
            detail=f"CSV row limit exceeded: max {OPERATOR_CSV_MAX_ROWS} data rows",
        )

    for row_num, row in enumerate(rows, start=2):
        name = (row.get("profile_name") or row.get("name") or "").strip()
        proxy = (row.get("proxy_url") or row.get("proxy") or "").strip() or None
        group = (row.get("group") or "").strip() or None
        notes = (row.get("notes") or "").strip() or None

        if not name:
            invalid.append({"row": row_num, "name": name, "reason": "Missing profile name"})
            continue
        if _profile_name_exists(name):
            skipped.append({"row": row_num, "name": name, "reason": "duplicate"})
            continue

        if proxy and req.validate_proxies:
            try:
                normalized = _normalize_proxy(proxy)
                _validate_proxy(normalized)
                proxy = normalized
            except ValueError as exc:
                invalid.append({"row": row_num, "name": name, "reason": str(exc)})
                continue

        profile = db.create_profile(
            name=name,
            proxy=proxy,
            group=group,
            notes=notes,
            proxy_status=_proxy_status(proxy, req.validate_proxies),
        )
        created_ids.append(profile["id"])

    summary = {
        "created": len(created_ids),
        "skipped": skipped,
        "invalid": invalid,
        "profile_ids": created_ids,
    }
    db.record_event(
        "csv_import",
        {"created": len(created_ids), "skipped": len(skipped), "invalid": len(invalid)},
    )
    return summary


@app.post("/api/operator/bulk")
async def operator_bulk(req: OperatorBulkRequest):
    semaphore = asyncio.Semaphore(req.concurrency)

    async def run_one(profile_id: str) -> dict:
        async with semaphore:
            profile = db.get_profile(profile_id)
            if not profile:
                return {"profile_id": profile_id, "status": "error", "detail": "Profile not found"}
            try:
                if req.action == "launch":
                    if profile_id in browser_mgr.running:
                        return {"profile_id": profile_id, "status": "error", "detail": "Profile already running"}
                    running = await browser_mgr.launch(profile)
                    return {
                        "profile_id": profile_id,
                        "status": "ok",
                        "detail": "launched",
                        "vnc_ws_port": running.ws_port,
                        "display": running.display_label,
                    }
                if req.action == "stop":
                    if profile_id not in browser_mgr.running:
                        return {"profile_id": profile_id, "status": "error", "detail": "Profile is not running"}
                    await browser_mgr.stop(profile_id)
                    return {"profile_id": profile_id, "status": "ok", "detail": "stopped"}

                if profile_id in browser_mgr.running:
                    await browser_mgr.stop(profile_id)
                running = await browser_mgr.launch(profile)
                return {
                    "profile_id": profile_id,
                    "status": "ok",
                    "detail": "restarted",
                    "vnc_ws_port": running.ws_port,
                    "display": running.display_label,
                }
            except Exception as exc:
                return {"profile_id": profile_id, "status": "error", "detail": str(exc)}

    results = await _run_limited(req.profile_ids, req.concurrency, run_one)
    db.record_event(
        "bulk_action",
        {
            "action": req.action,
            "requested": len(req.profile_ids),
            "ok": sum(1 for r in results if r["status"] == "ok"),
            "error": sum(1 for r in results if r["status"] == "error"),
        },
    )
    return {"action": req.action, "results": results}


async def _maybe_await(value):
    if pyinspect.isawaitable(value):
        return await value
    return value


def _current_page(running):
    pages = list(getattr(running.context, "pages", []) or [])
    return pages[-1] if pages else None


@app.post("/api/operator/automation")
async def operator_automation(req: OperatorAutomationRequest):
    if req.action == "open_url" and not req.url:
        raise HTTPException(status_code=422, detail="url is required for open_url")
    if req.action == "click_text" and not req.text:
        raise HTTPException(status_code=422, detail="text is required for click_text")
    if req.action == "click_xpath" and not req.xpath:
        raise HTTPException(status_code=422, detail="xpath is required for click_xpath")

    async def run_one(profile_id: str) -> dict:
        running = browser_mgr.running.get(profile_id)
        if not running:
            return {"profile_id": profile_id, "status": "error", "detail": "Profile not running"}
        try:
            page = _current_page(running)
            if req.action == "new_tab":
                if not hasattr(running.context, "new_page"):
                    return {"profile_id": profile_id, "status": "error", "detail": "Context cannot create pages"}
                page = await _maybe_await(running.context.new_page())
                return {"profile_id": profile_id, "status": "ok", "detail": "new_tab"}
            if not page:
                return {"profile_id": profile_id, "status": "error", "detail": "No active page"}

            if req.action == "open_url":
                response = await _maybe_await(page.goto(req.url))
                status_code = getattr(response, "status", None)
                result = {"profile_id": profile_id, "status": "ok", "detail": "opened"}
                if status_code is not None:
                    result["page_status"] = status_code
                return result
            if req.action == "close_tab":
                await _maybe_await(page.close())
                return {"profile_id": profile_id, "status": "ok", "detail": "closed"}
            if req.action == "close_extra_tabs":
                pages = list(getattr(running.context, "pages", []) or [])
                for extra_page in pages[:-1]:
                    await _maybe_await(extra_page.close())
                return {"profile_id": profile_id, "status": "ok", "detail": f"closed {max(len(pages) - 1, 0)} extra tabs"}
            if req.action == "click_text":
                button = page.get_by_role("button", name=req.text)
                await _maybe_await(button.click())
                return {"profile_id": profile_id, "status": "ok", "detail": "clicked_text"}
            if req.action == "click_xpath":
                target = page.locator(f"xpath={req.xpath}")
                await _maybe_await(target.click())
                return {"profile_id": profile_id, "status": "ok", "detail": "clicked_xpath"}
            if req.action == "reload":
                response = await _maybe_await(page.reload())
                return {"profile_id": profile_id, "status": "ok", "page_status": getattr(response, "status", None)}
            if req.action == "back":
                response = await _maybe_await(page.go_back())
                return {"profile_id": profile_id, "status": "ok", "page_status": getattr(response, "status", None)}
            if req.action == "forward":
                response = await _maybe_await(page.go_forward())
                return {"profile_id": profile_id, "status": "ok", "page_status": getattr(response, "status", None)}
            if req.action == "screenshot":
                image = await _maybe_await(page.screenshot(type="png"))
                encoded = base64.b64encode(image).decode("ascii")
                return {"profile_id": profile_id, "status": "ok", "screenshot": f"data:image/png;base64,{encoded}"}
            if req.action == "inspect":
                title = await _maybe_await(page.title())
                return {
                    "profile_id": profile_id,
                    "status": "ok",
                    "url": getattr(page, "url", None),
                    "title": title,
                    "page_status": "running",
                }
            return {"profile_id": profile_id, "status": "error", "detail": "Unsupported action"}
        except Exception as exc:
            return {"profile_id": profile_id, "status": "error", "detail": str(exc)}

    results = await _run_limited(req.profile_ids, req.concurrency, run_one)
    db.record_event(
        "automation_action",
        {
            "action": req.action,
            "requested": len(req.profile_ids),
            "ok": sum(1 for r in results if r["status"] == "ok"),
            "error": sum(1 for r in results if r["status"] == "error"),
        },
    )
    return {"action": req.action, "results": results}


@app.post("/api/operator/native-grid")
async def operator_native_grid(req: OperatorNativeGridRequest):
    running_profiles: list[dict] = []
    results: list[dict] = []

    for profile_id in req.profile_ids:
        profile = db.get_profile(profile_id)
        if not profile:
            results.append({"profile_id": profile_id, "status": "error", "detail": "Profile not found"})
            continue
        if profile_id not in browser_mgr.running:
            results.append({"profile_id": profile_id, "status": "error", "detail": "Profile is not running"})
            continue
        running_profiles.append(profile)
        results.append({"profile_id": profile_id, "status": "ok", "detail": "arranged"})

    frames = []
    if running_profiles:
        try:
            frames = native_window_helper.grid_native_windows(
                titles=[profile["name"] for profile in running_profiles],
                columns=req.columns,
                rows=req.rows,
                bounds=native_window_helper.Bounds(**req.bounds.model_dump()),
                gap=req.gap,
                scale=req.scale,
                strategy=req.strategy,
                apply=req.apply,
            )
        except Exception as exc:
            running_ids = {profile["id"] for profile in running_profiles}
            results = [
                {"profile_id": result["profile_id"], "status": "error", "detail": str(exc)}
                if result["profile_id"] in running_ids
                else result
                for result in results
            ]

    ok_count = sum(1 for result in results if result["status"] == "ok")
    error_count = sum(1 for result in results if result["status"] == "error")
    db.record_event(
        "native_grid",
        {
            "requested": len(req.profile_ids),
            "ok": ok_count,
            "error": error_count,
            "columns": req.columns,
            "rows": req.rows,
            "strategy": req.strategy,
            "apply": req.apply,
        },
    )
    status = "ok" if error_count == 0 else "error" if ok_count == 0 else "partial"
    return {
        "status": status,
        "results": results,
        "frames": [native_window_helper.asdict(frame) for frame in frames],
    }


@app.get("/api/operator/layouts", response_model=list[LayoutResponse])
async def operator_list_layouts():
    return [LayoutResponse(**layout) for layout in db.list_layouts()]


@app.post("/api/operator/layouts", response_model=LayoutResponse, status_code=201)
async def operator_create_layout(req: LayoutCreate):
    layout = db.create_layout(**req.model_dump())
    db.record_event("layout_save", {"layout_id": layout["id"], "name": layout["name"], "action": "create"})
    return LayoutResponse(**layout)


@app.put("/api/operator/layouts/{layout_id}", response_model=LayoutResponse)
async def operator_update_layout(layout_id: str, req: LayoutUpdate):
    layout = db.update_layout(layout_id, **req.model_dump(exclude_unset=True))
    if not layout:
        raise HTTPException(status_code=404, detail="Layout not found")
    db.record_event("layout_save", {"layout_id": layout["id"], "name": layout["name"], "action": "update"})
    return LayoutResponse(**layout)


@app.delete("/api/operator/layouts/{layout_id}")
async def operator_delete_layout(layout_id: str):
    layout = db.get_layout(layout_id)
    if not layout:
        raise HTTPException(status_code=404, detail="Layout not found")
    db.delete_layout(layout_id)
    db.record_event("layout_delete", {"layout_id": layout_id, "name": layout["name"]})
    return {"ok": True}


@app.get("/api/operator/events")
async def operator_events(limit: int = 100):
    limit = max(1, min(limit, 500))
    return db.list_events(limit=limit)


@app.get("/api/operator/export-metadata")
async def operator_export_metadata():
    return {
        "profiles": [_serialize_profile(p) for p in db.list_profiles()],
        "layouts": db.list_layouts(),
        "events": db.list_events(limit=500),
    }


@app.post("/api/operator/import-metadata")
async def operator_import_metadata(req: MetadataImportRequest):
    profile_created = 0
    profile_skipped = 0
    layout_created = 0
    layout_skipped = 0
    profile_ids: list[str] = []
    layout_ids: list[str] = []
    profile_invalid: list[dict] = []
    layout_invalid: list[dict] = []
    events_imported = 0
    events_skipped = 0

    for profile in req.profiles:
        try:
            profile_data = ProfileCreate.model_validate(profile)
        except ValidationError as exc:
            profile_skipped += 1
            profile_invalid.append({
                "name": _raw_import_name(profile),
                "reason": exc.errors()[0]["msg"] if exc.errors() else "Invalid profile",
            })
            continue
        name = profile_data.name.strip()
        if not name:
            profile_skipped += 1
            profile_invalid.append({"name": profile_data.name, "reason": "Missing profile name"})
            continue
        if _profile_name_exists(name):
            profile_skipped += 1
            continue
        tags = [tag.model_dump() for tag in profile_data.tags or []]
        profile_values = profile_data.model_dump(exclude={"tags"})
        profile_values["name"] = name
        created = db.create_profile(
            **profile_values,
            tags=tags,
        )
        profile_created += 1
        profile_ids.append(created["id"])

    for layout in req.layouts:
        try:
            layout_data = LayoutCreate.model_validate(layout)
        except ValidationError as exc:
            layout_skipped += 1
            layout_invalid.append({
                "name": _raw_import_name(layout),
                "reason": exc.errors()[0]["msg"] if exc.errors() else "Invalid layout",
            })
            continue
        name = layout_data.name.strip()
        if not name:
            layout_skipped += 1
            layout_invalid.append({"name": layout_data.name, "reason": "Missing layout name"})
            continue
        if db.get_layout_by_name(name):
            layout_skipped += 1
            continue
        layout_values = layout_data.model_dump()
        layout_values["name"] = name
        created = db.create_layout(**layout_values)
        layout_created += 1
        layout_ids.append(created["id"])

    for event in req.events or []:
        if db.import_event(event):
            events_imported += 1
        else:
            events_skipped += 1

    summary = {
        "profiles": {
            "created": profile_created,
            "skipped": profile_skipped,
            "invalid": profile_invalid,
            "profile_ids": profile_ids,
        },
        "layouts": {
            "created": layout_created,
            "skipped": layout_skipped,
            "invalid": layout_invalid,
            "layout_ids": layout_ids,
        },
        "events": {"imported": events_imported, "skipped": events_skipped},
    }
    db.record_event("metadata_import", summary)
    return summary


# ── Clipboard Relay ──────────────────────────────────────────────────────────

_CLIPBOARD_MAX_READ = 1_048_576  # 1MB cap on GET response

# Track xclip processes per display so we can kill the old one before spawning new
_xclip_procs: dict[int, asyncio.subprocess.Process] = {}


@app.post("/api/profiles/{profile_id}/clipboard")
async def set_clipboard(profile_id: str, body: ClipboardRequest):
    """Push text into the VNC session's X clipboard via xclip."""
    running = browser_mgr.running.get(profile_id)
    if not running:
        raise HTTPException(status_code=404, detail="Profile not running")

    import os

    # Kill previous xclip for this display (it stays alive to serve paste)
    old = _xclip_procs.pop(running.display, None)
    if old and old.returncode is None:
        old.kill()
        await old.wait()

    env = {**os.environ, "DISPLAY": f":{running.display}"}
    proc = await asyncio.create_subprocess_exec(
        "xclip", "-selection", "clipboard",
        stdin=asyncio.subprocess.PIPE,
        env=env,
    )
    # xclip reads stdin then stays alive to serve paste requests.
    proc.stdin.write(body.text.encode())  # type: ignore[union-attr]
    await proc.stdin.drain()  # type: ignore[union-attr]
    proc.stdin.close()  # type: ignore[union-attr]

    _xclip_procs[running.display] = proc

    return {"ok": True}


@app.get("/api/profiles/{profile_id}/clipboard")
async def get_clipboard(profile_id: str):
    """Read the VNC session's clipboard.

    Chrome doesn't write to X11 clipboard under KasmVNC, so xclip can't read it.
    Instead, read via Playwright's CDP connection to Chrome (navigator.clipboard.readText).
    Falls back to xclip for non-Chrome clipboard owners.
    """
    running = browser_mgr.running.get(profile_id)
    if not running:
        raise HTTPException(status_code=404, detail="Profile not running")

    # Read Chrome's current text selection via Playwright.
    # Chrome's native copy (via VNC Ctrl+C) doesn't write to X11 clipboard
    # and doesn't fire DOM events, so we read the visible selection instead.
    # The init script also captures copy events when they do fire.
    # Check all pages — user may have copied in any tab
    try:
        for page in running.context.pages:
            try:
                text = await page.evaluate("window.__clipboardText || ''")
                if text:
                    return {"text": text[:_CLIPBOARD_MAX_READ]}
            except Exception as exc:
                logger.debug("Clipboard read failed on page: %s", exc)
                continue
    except Exception as exc:
        logger.debug("Playwright clipboard read failed: %s", exc)

    # Fallback: xclip for non-Chrome clipboard owners
    import os

    env = {**os.environ, "DISPLAY": f":{running.display}"}
    proc = await asyncio.create_subprocess_exec(
        "xclip", "-selection", "clipboard", "-o",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    try:
        stdout, _ = await asyncio.wait_for(proc.communicate(), timeout=5)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return {"text": ""}

    if proc.returncode != 0:
        return {"text": ""}

    text = stdout[:_CLIPBOARD_MAX_READ].decode("utf-8", errors="replace")
    return {"text": text}


# ── VNC WebSocket Proxy ──────────────────────────────────────────────────────


@app.websocket("/api/profiles/{profile_id}/vnc")
async def vnc_proxy(websocket: WebSocket, profile_id: str):
    """Proxy WebSocket frames between the frontend and a profile's KasmVNC."""
    if not await _check_websocket_origin(websocket):
        return

    running = browser_mgr.running.get(profile_id)
    if not running:
        await websocket.close(code=4004, reason="Profile not running")
        return
    if running.ws_port is None:
        await websocket.close(code=4005, reason="Profile is running in native window mode")
        return

    # Accept with client's requested subprotocol (if any) — RFC 6455 requires
    # the server must not respond with a subprotocol the client didn't request.
    requested = websocket.scope.get("subprotocols", [])
    subprotocol = "binary" if "binary" in requested else None
    await websocket.accept(subprotocol=subprotocol)

    import websockets

    vnc_url = f"ws://127.0.0.1:{running.ws_port}/websockify"

    try:
        async with websockets.connect(
            vnc_url,
            subprotocols=["binary"],
            origin=f"http://127.0.0.1:{running.ws_port}",
            max_size=None,  # VNC frames can be large (1920x1080 framebuffer)
            ping_interval=None,  # KasmVNC doesn't respond to WS pings
            ping_timeout=None,
            compression=None,  # KasmVNC can't handle permessage-deflate
        ) as vnc_ws:
            logger.info(
                "VNC proxy: connected to KasmVNC for %s (subprotocol=%s)",
                profile_id, vnc_ws.subprotocol,
            )

            # noVNC v1.4 sends extension message types (150=ContinuousUpdates,
            # 248=QEMUKey, etc.) that KasmVNC 1.3.3 doesn't support, causing
            # "unknown message type" → disconnect.
            #
            # noVNC batches multiple RFB messages into a single WebSocket frame,
            # so we must parse the RFB stream to find message boundaries and strip
            # unsupported types before forwarding. Standard client→server types
            # have known fixed sizes (except SetEncodings and ClientCutText which
            # encode their length).

            async def client_to_vnc():
                count = 0
                handshake = 0  # first 3 messages are RFB handshake
                dropped = 0
                try:
                    while True:
                        msg = await websocket.receive()
                        msg_type = msg.get("type", "")
                        if msg_type == "websocket.disconnect":
                            logger.info("VNC proxy [c->v]: client disconnect (code=%s) after %d msgs (%d dropped)", msg.get("code"), count, dropped)
                            break
                        if "bytes" in msg and msg["bytes"]:
                            count += 1
                            data = msg["bytes"]
                            handshake += 1

                            # First 3 messages are RFB handshake — forward as-is
                            if handshake <= 3:
                                logger.debug("VNC handshake #%d: %d bytes hex=%s", handshake, len(data), data[:20].hex())
                                await vnc_ws.send(data)
                                continue

                            # Parse RFB messages and strip unsupported types
                            filtered = _filter_rfb_client_messages(data)
                            if filtered:
                                # Safety: verify first byte is a valid RFB client type
                                if filtered[0] not in _RFB_MSG_SIZE:
                                    logger.error("RFB SAFETY: refusing to send data with invalid first byte=%d hex=%s",
                                                 filtered[0], filtered[:20].hex())
                                    dropped += 1
                                    continue
                                logger.debug("VNC send: %d bytes first_type=%d hex=%s", len(filtered), filtered[0], filtered[:100].hex())
                                await vnc_ws.send(filtered)
                            else:
                                dropped += 1

                        elif "text" in msg and msg["text"]:
                            # noVNC only sends binary frames — text frames are unexpected
                            # and would bypass the RFB filter, so drop them.
                            count += 1
                            logger.warning("VNC proxy [c->v]: DROPPING text frame len=%d (noVNC should only send binary)", len(msg["text"]))
                            dropped += 1
                        else:
                            logger.warning("VNC proxy [c->v]: unhandled msg keys=%s type=%s", list(msg.keys()), msg_type)
                except WebSocketDisconnect as exc:
                    logger.info("VNC proxy [c->v]: WebSocketDisconnect code=%s after %d msgs (%d dropped)", exc.code, count, dropped)
                except Exception as exc:
                    logger.warning("VNC proxy [c->v]: %s: %s (after %d msgs)", type(exc).__name__, exc, count)

            async def vnc_to_client():
                count = 0
                try:
                    async for msg in vnc_ws:
                        count += 1
                        if isinstance(msg, bytes) and len(msg) > 0:
                            msg_type = msg[0]
                            if msg_type == 180:
                                # KasmVNC BinaryClipboard → convert to standard
                                # ServerCutText (type 3) so noVNC can handle it
                                text = _parse_kasmvnc_clipboard(msg)
                                if text:
                                    logger.info("VNC proxy [v->c]: clipboard %d chars", len(text))
                                    await websocket.send_bytes(_build_server_cut_text(text))
                                else:
                                    logger.info("VNC proxy [v->c]: dropped type 180 (no text/plain)")
                                continue
                            await websocket.send_bytes(msg)
                        elif isinstance(msg, bytes):
                            await websocket.send_bytes(msg)
                        else:
                            await websocket.send_text(msg)
                    logger.info("VNC proxy [v->c]: KasmVNC stream ended after %d msgs (close_code=%s)", count, vnc_ws.close_code)
                except WebSocketDisconnect as exc:
                    logger.info("VNC proxy [v->c]: client disconnect code=%s after %d msgs", exc.code, count)
                except Exception as exc:
                    logger.warning("VNC proxy [v->c]: %s: %s (after %d msgs)", type(exc).__name__, exc, count)

            c2v = asyncio.create_task(client_to_vnc(), name="c2v")
            v2c = asyncio.create_task(vnc_to_client(), name="v2c")

            done, pending = await asyncio.wait(
                [c2v, v2c],
                return_when=asyncio.FIRST_COMPLETED,
            )
            finished = [t.get_name() for t in done]
            still_running = [t.get_name() for t in pending]

            # Check if Xvnc is still alive
            vnc_instance = browser_mgr.vnc._allocated.get(running.display)
            xvnc_alive = vnc_instance and vnc_instance.process and vnc_instance.process.poll() is None
            logger.info(
                "VNC proxy: finished=%s pending=%s xvnc_alive=%s display=:%d for %s",
                finished, still_running, xvnc_alive, running.display, profile_id,
            )

            # Dump Xvnc log on disconnect
            import os
            xvnc_log = f"/tmp/xvnc-{running.display}.log"
            if os.path.exists(xvnc_log):
                with open(xvnc_log) as f:
                    log_content = f.read()
                if log_content.strip():
                    for line in log_content.strip().split("\n")[-20:]:
                        logger.info("Xvnc[:%d] %s", running.display, line)

            for task in pending:
                task.cancel()

    except Exception as exc:
        logger.error("VNC proxy connect error for %s: %s: %s", profile_id, type(exc).__name__, exc)
    finally:
        try:
            await websocket.close()
        except Exception as exc:
            logger.debug("VNC proxy: websocket.close() failed: %s", exc)


# ── CDP WebSocket Proxy ──────────────────────────────────────────────────────
# Simple bidirectional passthrough — CDP is standard JSON over WebSocket,
# no protocol translation needed (unlike VNC which requires RFB filtering).


@app.get("/api/profiles/{profile_id}/cdp")
async def cdp_info(profile_id: str):
    """Return CDP connection info. Prevents SPA catch-all from serving index.html."""
    running = browser_mgr.running.get(profile_id)
    if not running:
        raise HTTPException(status_code=404, detail="Profile not running")
    return {
        "cdp_url": f"/api/profiles/{profile_id}/cdp",
        "usage": "playwright.chromium.connect_over_cdp('http://<host>/api/profiles/"
        + profile_id + "/cdp')",
    }


@app.get("/api/profiles/{profile_id}/cdp/json/version/")
@app.get("/api/profiles/{profile_id}/cdp/json/version")
async def cdp_json_version(profile_id: str, request: Request):
    """Proxy Chrome's /json/version, rewriting WS URLs to go through our proxy."""
    running = browser_mgr.running.get(profile_id)
    if not running:
        raise HTTPException(status_code=404, detail="Profile not running")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{running.cdp_port}/json/version", timeout=5
            )
            data = resp.json()
    except Exception as exc:
        logger.error("CDP proxy: failed to reach Chrome CDP for %s: %s", profile_id, exc)
        raise HTTPException(status_code=502, detail="CDP endpoint unreachable")

    # Rewrite webSocketDebuggerUrl to point through our proxy
    host = request.headers.get("host", "localhost:8080")
    ws_scheme = "wss" if _is_https(request) else "ws"
    data["webSocketDebuggerUrl"] = f"{ws_scheme}://{host}/api/profiles/{profile_id}/cdp"
    return data


@app.get("/api/profiles/{profile_id}/cdp/json/list/")
@app.get("/api/profiles/{profile_id}/cdp/json/list")
@app.get("/api/profiles/{profile_id}/cdp/json/")
@app.get("/api/profiles/{profile_id}/cdp/json")
async def cdp_json_list(profile_id: str, request: Request):
    """Proxy Chrome's /json/list, rewriting WS URLs."""
    running = browser_mgr.running.get(profile_id)
    if not running:
        raise HTTPException(status_code=404, detail="Profile not running")

    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{running.cdp_port}/json/list", timeout=5
            )
            data = resp.json()
    except Exception as exc:
        logger.error("CDP proxy: failed to reach Chrome CDP for %s: %s", profile_id, exc)
        raise HTTPException(status_code=502, detail="CDP endpoint unreachable")

    host = request.headers.get("host", "localhost:8080")
    ws_scheme = "wss" if _is_https(request) else "ws"
    for entry in data:
        if "webSocketDebuggerUrl" in entry:
            ws_path = entry["webSocketDebuggerUrl"].split("/devtools/")[-1]
            entry["webSocketDebuggerUrl"] = (
                f"{ws_scheme}://{host}/api/profiles/{profile_id}/cdp/devtools/{ws_path}"
            )
    return data


async def _proxy_cdp_websocket(
    websocket: WebSocket, target_url: str, label: str,
) -> None:
    """Bidirectional WebSocket proxy between a FastAPI client and a CDP target.

    Used by both browser-level and page-level CDP proxy endpoints.
    """
    import websockets

    try:
        async with websockets.connect(
            target_url, max_size=None, ping_interval=None, ping_timeout=None
        ) as cdp_ws:
            logger.info("%s: connected to %s", label, target_url)

            async def client_to_cdp():
                try:
                    while True:
                        msg = await websocket.receive()
                        if msg.get("type") == "websocket.disconnect":
                            break
                        if "text" in msg and msg["text"]:
                            await cdp_ws.send(msg["text"])
                        elif "bytes" in msg and msg["bytes"]:
                            await cdp_ws.send(msg["bytes"])
                except WebSocketDisconnect:
                    pass
                except Exception as exc:
                    logger.warning("%s [c->cdp]: %s: %s", label, type(exc).__name__, exc)

            async def cdp_to_client():
                try:
                    async for msg in cdp_ws:
                        if isinstance(msg, str):
                            await websocket.send_text(msg)
                        else:
                            await websocket.send_bytes(msg)
                except WebSocketDisconnect:
                    pass
                except Exception as exc:
                    logger.warning("%s [cdp->c]: %s: %s", label, type(exc).__name__, exc)

            c2d = asyncio.create_task(client_to_cdp(), name="c2d")
            d2c = asyncio.create_task(cdp_to_client(), name="d2c")
            done, pending = await asyncio.wait(
                [c2d, d2c], return_when=asyncio.FIRST_COMPLETED
            )
            for task in pending:
                task.cancel()
            logger.info("%s: disconnected", label)

    except Exception as exc:
        logger.error("%s error: %s", label, exc)
    finally:
        try:
            await websocket.close()
        except Exception as exc:
            logger.debug("%s: websocket.close() failed: %s", label, exc)


@app.websocket("/api/profiles/{profile_id}/cdp")
async def cdp_proxy(websocket: WebSocket, profile_id: str):
    """Proxy WebSocket frames between external tools and Chrome's CDP."""
    if not await _check_websocket_origin(websocket):
        return

    running = browser_mgr.running.get(profile_id)
    if not running:
        await websocket.close(code=4004, reason="Profile not running")
        return

    await websocket.accept()

    # Get browser-level CDP WebSocket URL from Chrome
    try:
        async with httpx.AsyncClient() as client:
            resp = await client.get(
                f"http://127.0.0.1:{running.cdp_port}/json/version", timeout=5
            )
            ws_url = resp.json()["webSocketDebuggerUrl"]
    except Exception as exc:
        logger.error("CDP proxy: failed to get WS URL for %s: %s", profile_id, exc)
        await websocket.close(code=4005, reason="CDP not available")
        return

    await _proxy_cdp_websocket(websocket, ws_url, f"CDP proxy [{profile_id}]")


@app.websocket("/api/profiles/{profile_id}/cdp/devtools/{path:path}")
async def cdp_page_proxy(websocket: WebSocket, profile_id: str, path: str):
    """Proxy page-specific CDP WebSocket connections (e.g. /devtools/page/GUID)."""
    if not await _check_websocket_origin(websocket):
        return

    running = browser_mgr.running.get(profile_id)
    if not running:
        await websocket.close(code=4004, reason="Profile not running")
        return

    await websocket.accept()

    target_url = f"ws://127.0.0.1:{running.cdp_port}/devtools/{path}"
    await _proxy_cdp_websocket(websocket, target_url, f"CDP page proxy [{profile_id}]")


# ── Static Frontend ───────────────────────────────────────────────────────────

# Serve React build. Must be AFTER API routes so /api/* isn't caught by the SPA.
if FRONTEND_DIR.exists():
    app.mount("/assets", StaticFiles(directory=FRONTEND_DIR / "assets"), name="assets")

    @app.get("/{full_path:path}")
    async def serve_spa(full_path: str):
        """Serve React SPA — all non-API routes return index.html."""
        if full_path.startswith("api/"):
            raise HTTPException(status_code=404, detail="Not found")
        file_path = FRONTEND_DIR / full_path
        if full_path and file_path.exists() and file_path.is_file():
            return FileResponse(file_path)
        return FileResponse(FRONTEND_DIR / "index.html")
