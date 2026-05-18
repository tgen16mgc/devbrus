"""Tests for native window helper grid planning."""

from __future__ import annotations

import pytest

from backend.native_window_helper import Bounds, compute_grid_frames


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
