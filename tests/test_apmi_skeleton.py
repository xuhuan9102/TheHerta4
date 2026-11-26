import importlib.util
from pathlib import Path
import struct
import sys

import numpy
import pytest

MODULE_PATH = Path(__file__).resolve().parents[1] / "common" / "apmi_skeleton.py"
SPEC = importlib.util.spec_from_file_location("_apmi_skeleton_test", MODULE_PATH)
SKELETON = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = SKELETON
SPEC.loader.exec_module(SKELETON)


def matrices(translations):
    return b"".join(
        struct.pack("<16f", 1, 0, 0, 0, 0, 1, 0, 0, 0, 0, 1, 0, tx, 0, 0, 1)
        for tx in translations
    )


def palette(output, translations):
    return SKELETON.Palette(output, 0, "skin", b"", output, len(translations), 0), matrices(translations)


def test_apmi_single_pose_retains_numbering():
    palettes, count = SKELETON._merge_palette_slots({
        "11111111": palette("11111111", [0, 1]),
        "22222222": palette("22222222", [0, 1]),
    }, None)

    assert count == 4
    assert [(p.slots, p.writes) for p in palettes] == [((0, 1), (0, 1)), ((2, 3), (2, 3))]


def test_apmi_aliases_only_unique_moving_poses(tmp_path):
    samples = tmp_path / "FrameAnalysis-sample"
    samples.mkdir()
    (samples / "11111111.buf").write_bytes(matrices([0, 1.5, 2.5, 2.5, 4.5]))
    (samples / "22222222.buf").write_bytes(matrices([0, 1.5, 2.5, 3.5, 4.75]))
    palettes, count = SKELETON._merge_palette_slots({
        "11111111": palette("11111111", [0, 1, 2, 2, 4]),
        "22222222": palette("22222222", [0, 1, 2, 3, 4]),
    }, tmp_path)

    assert count == 9
    assert palettes[0].slots == (0, 1, 2, 3, 4)
    assert palettes[1].slots == (5, 1, 6, 7, 8)
    assert palettes[1].writes == (5, -1, 6, 7, 8)


def test_apmi_splits_alias_that_diverges_in_a_later_capture(tmp_path):
    sources = {
        "11111111": palette("11111111", [1]),
        "22222222": palette("22222222", [1]),
    }
    menu = tmp_path / "FrameAnalysis-menu"
    menu.mkdir()
    (menu / "11111111.buf").write_bytes(matrices([1.5]))
    (menu / "22222222.buf").write_bytes(matrices([1.5]))
    assert SKELETON._merge_palette_slots(sources, tmp_path)[1] == 1

    world = tmp_path / "FrameAnalysis-world"
    world.mkdir()
    (world / "11111111.buf").write_bytes(matrices([2]))
    (world / "22222222.buf").write_bytes(matrices([3]))
    palettes, count = SKELETON._merge_palette_slots(sources, tmp_path)
    assert count == 2
    assert [p.slots for p in palettes] == [(0,), (1,)]


def test_apmi_palette_order_tracks_native_indices_of_the_same_vertices():
    old = numpy.zeros(2, dtype=SKELETON.BLEND_DTYPE)
    old["weight"] = [[0.75, 0.25, 0, 0], [1, 0, 0, 0]]
    old["index"] = [[2, 1, 0, 0], [1, 0, 0, 0]]
    world = old.copy()
    world["index"] = [[4, 3, 0, 0], [3, 0, 0, 0]]

    assert SKELETON.infer_palette_order(old, world, 5) == (-1, 3, 4, -1, -1)

    world["index"][1, 0] = 4
    with pytest.raises(ValueError, match="唯一"):
        SKELETON.infer_palette_order(old, world, 5)
