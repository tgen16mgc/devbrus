"""Tests for native window helper grid planning."""

from __future__ import annotations

import pytest

from backend.native_window_helper import Bounds, WindowFrame, apply_macos, compute_grid_frames


def test_compute_grid_frames_fills_rows_in_order():
    frames = compute_grid_frames(
        ["profile-1", "profile-2", "profile-3"],
        columns=2,
        rows=2,
        bounds=Bounds(left=100, top=200, width=800, height=600),
        gap=10,
    )

    assert [frame.title for frame in frames] == ["profile-1", "profile-2", "profile-3"]
    assert frames[0].left == 100
    assert frames[0].top == 200
    assert frames[0].width == 395
    assert frames[0].height == 295
    assert frames[1].left == 505
    assert frames[2].top == 505


def test_compute_grid_frames_applies_scale_inside_cell():
    frames = compute_grid_frames(
        ["profile-1"],
        columns=1,
        rows=1,
        bounds=Bounds(left=0, top=0, width=1000, height=800),
        scale=0.5,
    )

    assert frames[0].width == 500
    assert frames[0].height == 400


def test_compute_grid_frames_limits_to_available_cells():
    frames = compute_grid_frames(
        ["one", "two", "three"],
        columns=1,
        rows=2,
        bounds=Bounds(left=0, top=0, width=500, height=500),
    )

    assert [frame.title for frame in frames] == ["one", "two"]


@pytest.mark.parametrize(
    "columns, rows, bounds, gap, scale",
    [
        (0, 1, Bounds(0, 0, 100, 100), 0, 1),
        (1, 0, Bounds(0, 0, 100, 100), 0, 1),
        (1, 1, Bounds(0, 0, 0, 100), 0, 1),
        (1, 1, Bounds(0, 0, 100, 100), -1, 1),
        (1, 1, Bounds(0, 0, 100, 100), 0, 0),
    ],
)
def test_compute_grid_frames_rejects_invalid_inputs(columns, rows, bounds, gap, scale):
    with pytest.raises(ValueError):
        compute_grid_frames(["profile"], columns, rows, bounds, gap=gap, scale=scale)


def test_apply_macos_targets_chromium_by_default(monkeypatch):
    calls = []
    monkeypatch.setattr("backend.native_window_helper.subprocess.run", lambda cmd, check: calls.append((cmd, check)))

    apply_macos([WindowFrame("profile", 0, 0, 400, 300)], strategy="index")

    assert 'processes whose name is "Chromium"' in calls[0][0][2]


def test_apply_macos_index_grids_windows_across_multiple_processes(monkeypatch):
    calls = []
    monkeypatch.setattr("backend.native_window_helper.subprocess.run", lambda cmd, check: calls.append((cmd, check)))

    apply_macos(
        [
            WindowFrame("one", 0, 0, 400, 300),
            WindowFrame("two", 410, 0, 400, 300),
        ],
        strategy="index",
    )

    script = calls[0][0][2]
    assert "repeat with targetProcess in targetProcesses" in script
    assert "repeat with targetWindow in windows of targetProcess" in script
    assert "set end of targetWindows to targetWindow" in script
    assert "if targetWindowCount >= 2 then" in script
    assert "set position of item 2 of targetWindows to {410, 0}" in script
