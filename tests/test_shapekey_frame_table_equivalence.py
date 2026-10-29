"""Differential test against the original per-key scan/searchsorted/add.at algorithm."""
import ast
import os
from pathlib import Path
import tempfile
import unittest

import numpy as np


SOURCE = Path(__file__).resolve().parents[1] / "blueprint/direct_export_shapekey_output_mixin.py"
module = ast.parse(SOURCE.read_text(encoding="utf-8"))
definition = next(n for n in module.body if isinstance(n, ast.ClassDef) and n.name == "DirectShapeKeyOutputMixin")
methods = [n for n in definition.body if isinstance(n, ast.FunctionDef) and n.name in {"_pack_half_float3", "_write_frame_table_files"}]
namespace = {"np": np, "os": os, "NO_FREQ_INDEX": 0xffffffff, "__package__": "_frame_equivalence.blueprint"}
exec(compile(ast.Module(body=methods, type_ignores=[]), str(SOURCE), "exec"), namespace)


class Writer:
    FRAME_TABLE_GROUP_MAP_REGISTER_BASE = 52
    _pack_half_float3 = namespace["_pack_half_float3"]
    _write_frame_table_files = namespace["_write_frame_table_files"]


def reference(vertex_count, plan, inputs):
    deltas, merged, frequencies = inputs
    merged, frequencies = merged.reshape(vertex_count, -1), frequencies.reshape(vertex_count, -1)
    records = {}
    for slot in range(merged.shape[1]):
        valid = (frequencies[:, slot] != 0xffffffff) & (merged[:, slot] >= 0)
        rows = np.flatnonzero(valid)
        keys = frequencies[rows, slot]
        for key in np.unique(keys):
            picked = rows[keys == key]
            records.setdefault(int(key), []).append((picked, deltas[merged[picked, slot]]))
    parts, metadata = [], []
    offset = 0
    for group in plan["groups"]:
        mask = np.zeros(vertex_count, dtype=bool)
        for key in group["members"]:
            for vertices, _payload in records.get(key, ()):
                mask[vertices] = True
        union = np.flatnonzero(mask)
        if not len(union):
            continue
        index = np.full(vertex_count, -1, dtype=np.int32)
        index[union] = np.arange(len(union), dtype=np.int32)
        frames = np.zeros((len(group["members"]) + 1, len(union), 3), dtype=np.float32)
        accumulator = np.zeros((len(union), 3), dtype=np.float32)
        for step, key in enumerate(group["members"], 1):
            for vertices, payload in records.get(key, ()):
                np.add.at(accumulator, np.searchsorted(union, vertices), payload)
            frames[step] = accumulator
        metadata.append((offset, len(union), len(group["members"]), index))
        parts.append(frames.reshape(-1, 3))
        offset += frames.shape[0] * len(union)
    if not parts:
        return None, []
    halves = np.concatenate(parts).astype(np.float16).reshape(-1).view(np.uint16)
    return np.pad(halves, (0, (-len(halves)) % 6)).tobytes(), metadata


class FrameTableEquivalenceTests(unittest.TestCase):
    def test_random_groups_and_cross_slot_overlaps_preserve_all_bytes_and_metadata(self):
        for seed in range(12):
            rng = np.random.default_rng(seed)
            vertices, slots = 101 + seed, 9
            merged = rng.integers(-2, 800, size=(vertices, slots), dtype=np.int32)
            frequencies = rng.integers(0, 8, size=merged.shape, dtype=np.uint32)
            frequencies[rng.random(merged.shape) < .2] = 0xffffffff
            deltas = rng.normal(size=(800, 3)).astype(np.float32)
            plan = {"groups": [{"group": i, "progress": f"$g{i}", "members": members}
                               for i, members in enumerate(([0, 1, 2], [99], [3, 1], [7, 4, 5]))]}
            inputs = deltas, merged.ravel(), frequencies.ravel()
            expected, metadata = reference(vertices, plan, inputs)
            with tempfile.TemporaryDirectory() as root:
                writer = Writer()
                writer.meshes_dir = root
                result = writer._write_frame_table_files("hash", vertices, plan, inputs)
                self.assertEqual((Path(root) / "hash-Position_frame_table.buf").read_bytes(), expected)
                self.assertEqual(len(result["groups"]), len(metadata))
                for ordinal, (actual, (offset, size, keys, indices)) in enumerate(zip(result["groups"], metadata)):
                    self.assertEqual((actual["offset"], actual["union_size"], actual["key_count"]), (offset, size, keys))
                    self.assertEqual(actual["ordinal"], ordinal)
                    self.assertEqual(actual["map_register"], 52 + ordinal)
                    self.assertEqual(Path(actual["map_path"]).read_bytes(), indices.tobytes())

    def test_no_matching_group_produces_no_table(self):
        writer = Writer()
        inputs = np.zeros((1, 3), np.float32), np.full(6, -1, np.int32), np.full(6, 0xffffffff, np.uint32)
        self.assertIsNone(writer._write_frame_table_files("hash", 3, {"groups": [{"members": [0]}]}, inputs))


if __name__ == "__main__":
    unittest.main()
