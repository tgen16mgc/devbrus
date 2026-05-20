"""Host-side native window arrangement helper.

The Manager backend usually runs in Docker, so host OS window movement must be
done by a local helper process. This module keeps the grid math testable and
provides macOS/Windows adapters for applying the calculated frames.
"""

from __future__ import annotations

import argparse
import json
import platform
import subprocess
from dataclasses import asdict, dataclass
from typing import Iterable, Literal


@dataclass(frozen=True)
class Bounds:
    left: int
    top: int
    width: int
    height: int


@dataclass(frozen=True)
class WindowFrame:
    title: str
    left: int
    top: int
    width: int
    height: int


def compute_grid_frames(
    titles: Iterable[str],
    columns: int,
    rows: int,
    bounds: Bounds,
    gap: int = 0,
    scale: float = 1.0,
) -> list[WindowFrame]:
    """Compute stable grid frames for native windows."""
    if columns < 1:
        raise ValueError("columns must be >= 1")
    if rows < 1:
        raise ValueError("rows must be >= 1")
    if bounds.width < 1 or bounds.height < 1:
        raise ValueError("bounds width/height must be >= 1")
    if gap < 0:
        raise ValueError("gap must be >= 0")
    if scale <= 0:
        raise ValueError("scale must be > 0")

    usable_width = bounds.width - gap * (columns - 1)
    usable_height = bounds.height - gap * (rows - 1)
    if usable_width < columns or usable_height < rows:
        raise ValueError("gap leaves no usable grid area")

    cell_width = usable_width // columns
    cell_height = usable_height // rows
    frame_width = max(1, int(cell_width * scale))
    frame_height = max(1, int(cell_height * scale))

    frames: list[WindowFrame] = []
    for index, title in enumerate(titles):
        if index >= columns * rows:
            break
        row = index // columns
        column = index % columns
        cell_left = bounds.left + column * (cell_width + gap)
        cell_top = bounds.top + row * (cell_height + gap)
        frames.append(
            WindowFrame(
                title=title,
                left=cell_left,
                top=cell_top,
                width=frame_width,
                height=frame_height,
            )
        )
    return frames


def apply_macos(
    frames: list[WindowFrame],
    process_name: str = "Chromium",
    strategy: Literal["title", "index"] = "title",
) -> None:
    """Move macOS windows by title substring using System Events."""
    script_lines = [
        'tell application "System Events"',
        f'  tell process "{process_name}"',
    ]
    if strategy == "index":
        for index, frame in enumerate(frames, start=1):
            script_lines.extend(
                [
                    f"    if (count of windows) >= {index} then",
                    f"      set position of window {index} to {{{frame.left}, {frame.top}}}",
                    f"      set size of window {index} to {{{frame.width}, {frame.height}}}",
                    "    end if",
                ]
            )
    else:
        for frame in frames:
            safe_title = frame.title.replace("\\", "\\\\").replace('"', '\\"')
            script_lines.extend(
                [
                    f'    set matches to windows whose name contains "{safe_title}"',
                    "    if (count of matches) > 0 then",
                    f"      set position of item 1 of matches to {{{frame.left}, {frame.top}}}",
                    f"      set size of item 1 of matches to {{{frame.width}, {frame.height}}}",
                    "    end if",
                ]
            )
    script_lines.extend(["  end tell", "end tell"])
    subprocess.run(["osascript", "-e", "\n".join(script_lines)], check=True)


def apply_windows(frames: list[WindowFrame]) -> None:
    """Move Windows top-level windows by title substring using Win32 APIs."""
    import ctypes
    from ctypes import wintypes

    user32 = ctypes.windll.user32
    enum_windows = user32.EnumWindows
    enum_windows_proc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    get_window_text = user32.GetWindowTextW
    get_window_text_length = user32.GetWindowTextLengthW
    is_window_visible = user32.IsWindowVisible
    set_window_pos = user32.SetWindowPos

    remaining = list(frames)

    def callback(hwnd, _lparam):
        if not is_window_visible(hwnd):
            return True
        length = get_window_text_length(hwnd)
        if length == 0:
            return True
        buffer = ctypes.create_unicode_buffer(length + 1)
        get_window_text(hwnd, buffer, length + 1)
        title = buffer.value
        for frame in list(remaining):
            if frame.title in title:
                set_window_pos(hwnd, 0, frame.left, frame.top, frame.width, frame.height, 0x0040)
                remaining.remove(frame)
                break
        return True

    enum_windows(enum_windows_proc(callback), 0)


def grid_native_windows(
    *,
    titles: list[str],
    columns: int,
    rows: int,
    bounds: Bounds,
    gap: int = 0,
    scale: float = 1.0,
    strategy: Literal["title", "index"] = "index",
    apply: bool = True,
    macos_process: str = "Chromium",
) -> list[WindowFrame]:
    """Compute and optionally apply a grid to native OS browser windows."""
    frames = compute_grid_frames(
        titles,
        columns=columns,
        rows=rows,
        bounds=bounds,
        gap=gap,
        scale=scale,
    )
    if not apply:
        return frames

    system = platform.system()
    if system == "Darwin":
        apply_macos(frames, process_name=macos_process, strategy=strategy)
    elif system == "Windows":
        apply_windows(frames)
    else:
        raise RuntimeError(f"Native arrangement is not supported on {system}")
    return frames


def _parse_bounds(raw: str) -> Bounds:
    parts = [int(part.strip()) for part in raw.split(",")]
    if len(parts) != 4:
        raise argparse.ArgumentTypeError("bounds must be left,top,width,height")
    return Bounds(left=parts[0], top=parts[1], width=parts[2], height=parts[3])


def main() -> None:
    parser = argparse.ArgumentParser(description="Arrange CloakBrowser windows into a native OS grid.")
    parser.add_argument("--titles", required=True, help="JSON array of window title substrings, in layout order.")
    parser.add_argument("--columns", type=int, required=True)
    parser.add_argument("--rows", type=int, required=True)
    parser.add_argument("--bounds", type=_parse_bounds, required=True, help="left,top,width,height")
    parser.add_argument("--gap", type=int, default=0)
    parser.add_argument("--scale", type=float, default=1.0)
    parser.add_argument("--strategy", choices=["index", "title"], default="index")
    parser.add_argument("--apply", action="store_true", help="Move windows instead of printing the plan.")
    parser.add_argument("--macos-process", default="Chromium")
    args = parser.parse_args()

    titles = json.loads(args.titles)
    if not isinstance(titles, list) or not all(isinstance(title, str) for title in titles):
        raise SystemExit("--titles must be a JSON array of strings")

    frames = grid_native_windows(
        titles=titles,
        columns=args.columns,
        rows=args.rows,
        bounds=args.bounds,
        gap=args.gap,
        scale=args.scale,
        strategy=args.strategy,
        apply=args.apply,
        macos_process=args.macos_process,
    )
    if not args.apply:
        print(json.dumps([asdict(frame) for frame in frames], indent=2))


if __name__ == "__main__":
    main()
