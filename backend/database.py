"""SQLite database operations for browser profiles."""

from __future__ import annotations

import datetime
import json
import os
import random
import sqlite3
import uuid
from contextlib import contextmanager
from pathlib import Path
from typing import Any

DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
DB_PATH = DATA_DIR / "profiles.db"


@contextmanager
def get_db():
    conn = sqlite3.connect(str(DB_PATH))
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with get_db() as conn:
        conn.executescript("""
            CREATE TABLE IF NOT EXISTS profiles (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                fingerprint_seed INTEGER NOT NULL,
                proxy TEXT,
                timezone TEXT,
                locale TEXT,
                platform TEXT DEFAULT 'windows',
                user_agent TEXT,
                screen_width INTEGER DEFAULT 1920,
                screen_height INTEGER DEFAULT 1080,
                gpu_vendor TEXT,
                gpu_renderer TEXT,
                hardware_concurrency INTEGER,
                humanize BOOLEAN DEFAULT 0,
                human_preset TEXT DEFAULT 'default',
                headless BOOLEAN DEFAULT 0,
                geoip BOOLEAN DEFAULT 0,
                clipboard_sync BOOLEAN DEFAULT 1,
                auto_launch BOOLEAN DEFAULT 0,
                color_scheme TEXT,
                profile_group TEXT,
                sort_order INTEGER DEFAULT 0,
                proxy_status TEXT,
                notes TEXT,
                user_data_dir TEXT NOT NULL,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS profile_tags (
                profile_id TEXT REFERENCES profiles(id) ON DELETE CASCADE,
                tag TEXT NOT NULL,
                color TEXT,
                PRIMARY KEY (profile_id, tag)
            );

            CREATE TABLE IF NOT EXISTS operator_layouts (
                id TEXT PRIMARY KEY,
                name TEXT NOT NULL,
                mode TEXT NOT NULL,
                columns INTEGER NOT NULL,
                rows INTEGER NOT NULL,
                tile_scale REAL NOT NULL,
                monitor TEXT,
                profile_order TEXT NOT NULL DEFAULT '[]',
                layout_group TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS operator_events (
                id TEXT PRIMARY KEY,
                event_type TEXT NOT NULL,
                detail TEXT NOT NULL DEFAULT '{}',
                created_at TEXT NOT NULL
            );
        """)
        conn.commit()

        # Migrations for existing databases
        cols = {row[1] for row in conn.execute("PRAGMA table_info(profiles)").fetchall()}
        if "clipboard_sync" not in cols:
            conn.execute("ALTER TABLE profiles ADD COLUMN clipboard_sync BOOLEAN DEFAULT 1")
            conn.commit()
        if "launch_args" not in cols:
            conn.execute("ALTER TABLE profiles ADD COLUMN launch_args TEXT DEFAULT '[]'")
            conn.commit()
        if "auto_launch" not in cols:
            conn.execute("ALTER TABLE profiles ADD COLUMN auto_launch BOOLEAN DEFAULT 0")
            conn.commit()
        if "profile_group" not in cols:
            conn.execute("ALTER TABLE profiles ADD COLUMN profile_group TEXT")
            conn.commit()
        if "sort_order" not in cols:
            conn.execute("ALTER TABLE profiles ADD COLUMN sort_order INTEGER DEFAULT 0")
            conn.commit()
        if "proxy_status" not in cols:
            conn.execute("ALTER TABLE profiles ADD COLUMN proxy_status TEXT")
            conn.commit()


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def create_profile(
    name: str,
    fingerprint_seed: int | None = None,
    **fields: Any,
) -> dict[str, Any]:
    profile_id = str(uuid.uuid4())
    seed = fingerprint_seed if fingerprint_seed is not None else random.randint(10000, 99999)
    user_data_dir = str(DATA_DIR / "profiles" / profile_id)
    now = _now()
    tags = fields.pop("tags", None) or []
    proxy = fields.get("proxy")
    proxy_status = fields.get("proxy_status")
    if proxy_status is None:
        proxy_status = "unchecked" if proxy else "no_proxy"

    with get_db() as conn:
        conn.execute(
            """INSERT INTO profiles (
                id, name, fingerprint_seed, proxy, timezone, locale, platform,
                user_agent, screen_width, screen_height, gpu_vendor, gpu_renderer,
                hardware_concurrency, humanize, human_preset, headless, geoip,
                clipboard_sync, auto_launch, color_scheme, launch_args,
                profile_group, sort_order, proxy_status, notes,
                user_data_dir, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                profile_id, name, seed,
                proxy,
                fields.get("timezone"),
                fields.get("locale"),
                fields.get("platform", "windows"),
                fields.get("user_agent"),
                fields.get("screen_width", 1920),
                fields.get("screen_height", 1080),
                fields.get("gpu_vendor"),
                fields.get("gpu_renderer"),
                fields.get("hardware_concurrency"),
                fields.get("humanize", False),
                fields.get("human_preset", "default"),
                fields.get("headless", False),
                fields.get("geoip", False),
                fields.get("clipboard_sync", True),
                fields.get("auto_launch", False),
                fields.get("color_scheme"),
                json.dumps(fields.get("launch_args") or []),
                fields.get("group"),
                fields.get("sort_order", 0),
                proxy_status,
                fields.get("notes"),
                user_data_dir, now, now,
            ),
        )
        for t in tags:
            conn.execute(
                "INSERT INTO profile_tags (profile_id, tag, color) VALUES (?, ?, ?)",
                (profile_id, t["tag"], t.get("color")),
            )
        conn.commit()

    return get_profile(profile_id)  # type: ignore[return-value]


def _decode_profile(row: sqlite3.Row) -> dict[str, Any]:
    profile = dict(row)
    profile["launch_args"] = json.loads(profile.get("launch_args") or "[]")
    profile["group"] = profile.pop("profile_group", None)
    profile["sort_order"] = profile.get("sort_order") or 0
    if profile.get("proxy_status") is None:
        profile["proxy_status"] = "unchecked" if profile.get("proxy") else "no_proxy"
    return profile


def get_profile(profile_id: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM profiles WHERE id = ?", (profile_id,)).fetchone()
        if not row:
            return None
        profile = _decode_profile(row)
        tags = conn.execute(
            "SELECT tag, color FROM profile_tags WHERE profile_id = ?",
            (profile_id,),
        ).fetchall()
        profile["tags"] = [dict(t) for t in tags]
        return profile


def get_profile_by_name(name: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM profiles WHERE name = ?", (name,)).fetchone()
        if not row:
            return None
        profile = _decode_profile(row)
        tags = conn.execute(
            "SELECT tag, color FROM profile_tags WHERE profile_id = ?",
            (profile["id"],),
        ).fetchall()
        profile["tags"] = [dict(t) for t in tags]
        return profile


def list_profiles() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM profiles ORDER BY sort_order ASC, created_at DESC").fetchall()
        profiles = []
        for row in rows:
            profile = _decode_profile(row)
            tags = conn.execute(
                "SELECT tag, color FROM profile_tags WHERE profile_id = ?",
                (profile["id"],),
            ).fetchall()
            profile["tags"] = [dict(t) for t in tags]
            profiles.append(profile)
        return profiles


def update_profile(profile_id: str, **fields: Any) -> dict[str, Any] | None:
    existing = get_profile(profile_id)
    if not existing:
        return None

    tags = fields.pop("tags", None)

    # Only update fields that were explicitly provided
    update_cols = []
    update_vals = []
    # Pre-serialize launch_args to JSON before the generic update loop
    if "launch_args" in fields:
        fields["launch_args"] = json.dumps(fields["launch_args"] or [])
    if "group" in fields:
        fields["profile_group"] = fields.pop("group")

    for col in (
        "name", "fingerprint_seed", "proxy", "timezone", "locale", "platform",
        "user_agent", "screen_width", "screen_height", "gpu_vendor", "gpu_renderer",
        "hardware_concurrency", "humanize", "human_preset", "headless", "geoip",
        "clipboard_sync", "auto_launch", "color_scheme", "launch_args",
        "profile_group", "sort_order", "proxy_status", "notes",
    ):
        if col in fields:
            update_cols.append(f"{col} = ?")
            update_vals.append(fields[col])

    if update_cols:
        update_cols.append("updated_at = ?")
        update_vals.append(_now())
        update_vals.append(profile_id)
        with get_db() as conn:
            conn.execute(
                f"UPDATE profiles SET {', '.join(update_cols)} WHERE id = ?",
                update_vals,
            )
            conn.commit()

    if tags is not None:
        with get_db() as conn:
            conn.execute("DELETE FROM profile_tags WHERE profile_id = ?", (profile_id,))
            for t in tags:
                conn.execute(
                    "INSERT INTO profile_tags (profile_id, tag, color) VALUES (?, ?, ?)",
                    (profile_id, t["tag"], t.get("color")),
                )
            conn.commit()

    return get_profile(profile_id)


def delete_profile(profile_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM profiles WHERE id = ?", (profile_id,))
        conn.commit()
        return cursor.rowcount > 0


def _decode_layout(row: sqlite3.Row) -> dict[str, Any]:
    layout = dict(row)
    layout["profile_order"] = json.loads(layout.get("profile_order") or "[]")
    layout["group"] = layout.pop("layout_group", None)
    return layout


def create_layout(**fields: Any) -> dict[str, Any]:
    layout_id = str(uuid.uuid4())
    now = _now()
    with get_db() as conn:
        conn.execute(
            """INSERT INTO operator_layouts (
                id, name, mode, columns, rows, tile_scale, monitor, profile_order,
                layout_group, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                layout_id,
                fields["name"],
                fields["mode"],
                fields["columns"],
                fields["rows"],
                fields["tile_scale"],
                fields.get("monitor"),
                json.dumps(fields.get("profile_order") or []),
                fields.get("group"),
                now,
                now,
            ),
        )
        conn.commit()
    return get_layout(layout_id)  # type: ignore[return-value]


def get_layout(layout_id: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM operator_layouts WHERE id = ?", (layout_id,)).fetchone()
        return _decode_layout(row) if row else None


def get_layout_by_name(name: str) -> dict[str, Any] | None:
    with get_db() as conn:
        row = conn.execute("SELECT * FROM operator_layouts WHERE name = ?", (name,)).fetchone()
        return _decode_layout(row) if row else None


def list_layouts() -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute("SELECT * FROM operator_layouts ORDER BY updated_at DESC").fetchall()
        return [_decode_layout(row) for row in rows]


def update_layout(layout_id: str, **fields: Any) -> dict[str, Any] | None:
    if not get_layout(layout_id):
        return None

    if "profile_order" in fields:
        fields["profile_order"] = json.dumps(fields["profile_order"] or [])
    if "group" in fields:
        fields["layout_group"] = fields.pop("group")

    update_cols = []
    update_vals = []
    for col in (
        "name", "mode", "columns", "rows", "tile_scale", "monitor",
        "profile_order", "layout_group",
    ):
        if col in fields:
            update_cols.append(f"{col} = ?")
            update_vals.append(fields[col])

    if update_cols:
        update_cols.append("updated_at = ?")
        update_vals.append(_now())
        update_vals.append(layout_id)
        with get_db() as conn:
            conn.execute(
                f"UPDATE operator_layouts SET {', '.join(update_cols)} WHERE id = ?",
                update_vals,
            )
            conn.commit()

    return get_layout(layout_id)


def delete_layout(layout_id: str) -> bool:
    with get_db() as conn:
        cursor = conn.execute("DELETE FROM operator_layouts WHERE id = ?", (layout_id,))
        conn.commit()
        return cursor.rowcount > 0


def record_event(event_type: str, detail: dict[str, Any] | None = None) -> dict[str, Any]:
    event_id = str(uuid.uuid4())
    now = _now()
    payload = detail or {}
    with get_db() as conn:
        conn.execute(
            "INSERT INTO operator_events (id, event_type, detail, created_at) VALUES (?, ?, ?, ?)",
            (event_id, event_type, json.dumps(payload), now),
        )
        conn.commit()
    return {"id": event_id, "event_type": event_type, "detail": payload, "created_at": now}


def import_event(event: dict[str, Any]) -> bool:
    raw_event_type = event.get("event_type")
    if not isinstance(raw_event_type, str):
        return False
    event_type = raw_event_type.strip()
    if not event_type:
        return False

    event_id = event.get("id") or str(uuid.uuid4())
    detail = event.get("detail")
    if not isinstance(detail, dict):
        detail = {}
    created_at = event.get("created_at") or _now()

    with get_db() as conn:
        if conn.execute("SELECT 1 FROM operator_events WHERE id = ?", (event_id,)).fetchone():
            return False
        conn.execute(
            "INSERT INTO operator_events (id, event_type, detail, created_at) VALUES (?, ?, ?, ?)",
            (event_id, event_type, json.dumps(detail), created_at),
        )
        conn.commit()
    return True


def list_events(limit: int = 100) -> list[dict[str, Any]]:
    with get_db() as conn:
        rows = conn.execute(
            "SELECT * FROM operator_events ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        events = []
        for row in rows:
            event = dict(row)
            event["detail"] = json.loads(event.get("detail") or "{}")
            events.append(event)
        return events
