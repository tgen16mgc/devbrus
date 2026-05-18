"""VNC display allocation and lifecycle management."""

from __future__ import annotations

import asyncio
import logging
import os
import shutil
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

logger = logging.getLogger("cloakbrowser.manager.vnc")


@dataclass
class VNCInstance:
    display: int
    ws_port: int
    rfb_port: int | None = None
    process: subprocess.Popen | None = None
    processes: list[subprocess.Popen] = field(default_factory=list)


class VNCManager:
    BASE_DISPLAY = 100
    BASE_WS_PORT = 6100
    BASE_RFB_PORT = 5900

    def __init__(self):
        self._allocated: dict[int, VNCInstance] = {}
        self._lock = asyncio.Lock()

    async def allocate(self) -> tuple[int, int]:
        """Returns (display_number, ws_port) for a new profile."""
        async with self._lock:
            display = self.BASE_DISPLAY
            while display in self._allocated:
                display += 1
            ws_port = self.BASE_WS_PORT + (display - self.BASE_DISPLAY)
            rfb_port = self.BASE_RFB_PORT + (display - self.BASE_DISPLAY)
            self._allocated[display] = VNCInstance(
                display=display,
                ws_port=ws_port,
                rfb_port=rfb_port,
            )
            return display, ws_port

    @staticmethod
    def _xvfb_bin() -> str | None:
        return shutil.which("Xvfb") or (
            "/opt/X11/bin/Xvfb" if Path("/opt/X11/bin/Xvfb").exists() else None
        )

    @staticmethod
    def _x11vnc_bin() -> str | None:
        return shutil.which("x11vnc")

    @staticmethod
    def _xvnc_bin() -> str | None:
        return shutil.which("Xvnc")

    def is_available(self) -> bool:
        """Return True when either KasmVNC or the local macOS VNC stack exists."""
        return self._xvnc_bin() is not None or (
            self._xvfb_bin() is not None and self._x11vnc_bin() is not None
        )

    def has_kasmvnc(self) -> bool:
        """Return True when a KasmVNC-compatible Xvnc binary is available."""
        return self._xvnc_bin() is not None

    async def start_vnc(
        self,
        display: int,
        ws_port: int,
        width: int = 1920,
        height: int = 1080,
    ) -> subprocess.Popen:
        """Start a VNC-backed X display for a profile."""
        xvnc_bin = self._xvnc_bin()
        if xvnc_bin:
            return await self._start_kasmvnc(display, ws_port, width, height)

        return await self._start_xvfb_x11vnc(display, ws_port, width, height)

    async def _start_kasmvnc(
        self,
        display: int,
        ws_port: int,
        width: int,
        height: int,
    ) -> subprocess.Popen:
        """Start KasmVNC Xvnc on the given display."""
        xvnc_bin = self._xvnc_bin() or "Xvnc"

        # KasmVNC requires -httpd to enable the WebSocket handler on the websocket port.
        # Without it, the port accepts TCP but won't do WebSocket upgrade.
        httpd_dir = "/usr/share/kasmvnc/www"

        cmd = [
            xvnc_bin,
            f":{display}",
            "-websocketPort", str(ws_port),
            "-rfbport", "-1",  # disable raw VNC TCP port — WebSocket only
            "-geometry", f"{width}x{height}",
            "-depth", "24",
            "-SecurityTypes", "None",
            "-DisableBasicAuth",
            "-interface", "127.0.0.1",  # internal only, proxied by FastAPI
            "-AlwaysShared",
            "-httpd", httpd_dir,
        ]

        log_path = f"/tmp/xvnc-{display}.log"
        logger.info("Starting Xvnc on :%d (ws_port=%d) log=%s", display, ws_port, log_path)

        log_file = open(log_path, "w")
        proc = subprocess.Popen(
            cmd,
            stdout=log_file,
            stderr=log_file,
        )
        log_file.close()  # Popen inherited the fd, parent doesn't need it

        # Wait a moment for Xvnc to initialize
        await asyncio.sleep(0.5)

        if proc.poll() is not None:
            try:
                with open(log_path) as f:
                    err = f.read()
            except Exception as exc:
                logger.debug("Failed to read Xvnc log %s: %s", log_path, exc)
                err = ""
            raise RuntimeError(f"Xvnc failed to start on :{display}: {err}")

        async with self._lock:
            if display in self._allocated:
                self._allocated[display].process = proc
                self._allocated[display].processes = [proc]

        return proc

    async def _start_xvfb_x11vnc(
        self,
        display: int,
        ws_port: int,
        width: int,
        height: int,
    ) -> subprocess.Popen:
        """Start Xvfb + x11vnc + websockify for macOS/local development."""
        xvfb_bin = self._xvfb_bin()
        x11vnc_bin = self._x11vnc_bin()
        if not xvfb_bin or not x11vnc_bin:
            raise RuntimeError(
                "No local VNC runtime found. Install KasmVNC/Xvnc, or install "
                "XQuartz + x11vnc + websockify for local macOS dashboard mode."
            )

        instance = self._allocated.get(display)
        rfb_port = instance.rfb_port if instance and instance.rfb_port else self.BASE_RFB_PORT + (display - self.BASE_DISPLAY)
        log_path = f"/tmp/xvfb-x11vnc-{display}.log"
        logger.info(
            "Starting Xvfb/x11vnc on :%d (rfb_port=%d, ws_port=%d) log=%s",
            display,
            rfb_port,
            ws_port,
            log_path,
        )

        log_file = open(log_path, "w")
        env = {
            **os.environ,
            "PATH": f"/opt/X11/bin:{os.environ.get('PATH', '')}",
        }
        xvfb_proc = subprocess.Popen(
            [
                xvfb_bin,
                f":{display}",
                "-screen",
                "0",
                f"{width}x{height}x24",
                "-ac",
                "-nolisten",
                "tcp",
            ],
            stdout=log_file,
            stderr=log_file,
            env=env,
        )
        await asyncio.sleep(0.7)
        if xvfb_proc.poll() is not None:
            log_file.close()
            raise RuntimeError(f"Xvfb failed to start on :{display}: {self._read_log(log_path)}")

        x11vnc_proc = subprocess.Popen(
            [
                x11vnc_bin,
                "-display",
                f":{display}",
                "-rfbport",
                str(rfb_port),
                "-listen",
                "127.0.0.1",
                "-localhost",
                "-forever",
                "-shared",
                "-nopw",
                "-noxdamage",
                "-quiet",
            ],
            stdout=log_file,
            stderr=log_file,
            env=env,
        )
        await asyncio.sleep(0.7)
        if x11vnc_proc.poll() is not None:
            xvfb_proc.terminate()
            log_file.close()
            raise RuntimeError(f"x11vnc failed to start on :{display}: {self._read_log(log_path)}")

        websockify_proc = subprocess.Popen(
            [
                sys.executable,
                "-m",
                "websockify",
                f"127.0.0.1:{ws_port}",
                f"127.0.0.1:{rfb_port}",
            ],
            stdout=log_file,
            stderr=log_file,
            env=env,
        )
        await asyncio.sleep(0.7)
        log_file.close()
        if websockify_proc.poll() is not None:
            x11vnc_proc.terminate()
            xvfb_proc.terminate()
            raise RuntimeError(f"websockify failed to start on :{display}: {self._read_log(log_path)}")

        async with self._lock:
            if display in self._allocated:
                self._allocated[display].process = xvfb_proc
                self._allocated[display].processes = [
                    websockify_proc,
                    x11vnc_proc,
                    xvfb_proc,
                ]

        return xvfb_proc

    async def stop_vnc(self, display: int):
        """Kill VNC processes for given display and release allocation."""
        async with self._lock:
            instance = self._allocated.pop(display, None)

        if instance:
            processes = instance.processes or ([instance.process] if instance.process else [])
            logger.info("Stopping VNC runtime on :%d", display)
            for proc in processes:
                if proc and proc.poll() is None:
                    proc.terminate()
            for proc in processes:
                if not proc:
                    continue
                if proc.poll() is not None:
                    continue
                try:
                    await asyncio.get_event_loop().run_in_executor(
                        None, proc.wait, 5,
                    )
                except subprocess.TimeoutExpired:
                    proc.kill()

    @staticmethod
    def _read_log(log_path: str) -> str:
        try:
            with open(log_path) as f:
                return f.read()
        except Exception as exc:
            logger.debug("Failed to read VNC log %s: %s", log_path, exc)
            return ""

    async def cleanup_all(self):
        """Kill all managed VNC processes. Called on shutdown."""
        async with self._lock:
            displays = list(self._allocated.keys())

        for display in displays:
            await self.stop_vnc(display)

    async def cleanup_stale(self):
        """Kill orphan VNC processes from previous runs."""
        patterns = [r"Xvnc :[0-9]", r"Xvfb :[0-9]", r"x11vnc.*-display :[0-9]", r"websockify.*127.0.0.1:61[0-9][0-9]"]
        for pattern in patterns:
            try:
                result = subprocess.run(
                    ["pkill", "-f", pattern],
                    capture_output=True,
                )
                if result.returncode == 0:
                    logger.info("Cleaned up stale VNC processes matching %s", pattern)
            except FileNotFoundError:
                logger.debug("pkill not found, skipping stale VNC cleanup")

    def get_ws_port(self, display: int) -> int | None:
        """Get WebSocket port for a display."""
        instance = self._allocated.get(display)
        return instance.ws_port if instance else None

    @property
    def active_displays(self) -> list[int]:
        return list(self._allocated.keys())
