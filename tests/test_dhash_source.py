# -*- coding: utf-8 -*-
"""dHash 导入回填（common/dhash_source.py）单测。

覆盖任务要求的失败场景：
1. 带 ``hash_contamination=`` 的行不可信，只保留干净行；
2. 同一 hash 多个不同干净 dhash → 丢弃（动态资源），不任选；
3. 文件名里的 hash8 不在 log → ``resolve_dhash`` 返回空；
4. ``<hash8>_<other>-BC7_UNORM_SRGB.dds`` 用第一个 hash8 命中，格式正确；
5. ``apply_to_marks`` 只改能对上的 mark，其余保持原样；
6. 写回 SubmeshJson / 回写缓存，且产物不含 ``00ff00000000ffff``。
"""

import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path


REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "dhash_source_test_pkg"

for _pkg_name in (PKG, f"{PKG}.common"):
    _pkg = types.ModuleType(_pkg_name)
    _pkg.__path__ = []
    sys.modules[_pkg_name] = _pkg


def _load_real(qualname, relpath):
    spec = importlib.util.spec_from_file_location(qualname, REPO_ROOT / relpath)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


_load_real(f"{PKG}.common.dhash_metadata", "common/dhash_metadata.py")
_dhash_source = _load_real(f"{PKG}.common.dhash_source", "common/dhash_source.py")

parse_frame_analysis_log = _dhash_source.parse_frame_analysis_log
resolve_dhash = _dhash_source.resolve_dhash
resolve_format = _dhash_source.resolve_format
apply_to_marks = _dhash_source.apply_to_marks
workspace_marks = _dhash_source.workspace_marks
apply_workspace_dhash = _dhash_source.apply_workspace_dhash
workspace_texture_stats = _dhash_source.workspace_texture_stats

#: 真实 log 只读常量；CI/无盘环境自动 skip。
REAL_LOG = Path(
    r"K:\SSMT-Package-master\3Dmigoto\ZZZ\FrameAnalysis-2026-11-24-092700\log.txt"
)

CLEAN_DHASH = "0123456789abcdef"
OTHER_DHASH = "fedcba9876543210"
CONTAMINATED_DHASH = "00ff00000000ffff"


def _write_log(directory: Path, lines) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    log_path = directory / "log.txt"
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return log_path


def _write_json(path: Path, payload) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=4), encoding="utf-8")


class ParseResolveTests(unittest.TestCase):
    def test_contaminated_line_is_skipped(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = _write_log(
                Path(tmp),
                [
                    "0: view=... hash=11111111 dhash=aaaaaaaaaaaaaaaa",
                    "1: view=... hash=11111111 hash_contamination=UpdateSubresourceRegion, dhash=bbbbbbbbbbbbbbbb",
                    "2: view=... hash=22222222 hash_contamination=UpdateSubresourceRegion, dhash=00ff00000000ffff",
                    "3: view=... hash=33333333 hash_contamination=UpdateSubresourceRegion,",
                ],
            )
            mapping = parse_frame_analysis_log(str(log_path))

        # 有干净行的 hash 采用干净值，污染行不覆盖。
        self.assertEqual(mapping["11111111"]["dhash"], "aaaaaaaaaaaaaaaa")
        self.assertFalse(mapping["11111111"]["contaminated"])
        # 只有污染行的 hash 保留记录，但 dhash 为空，resolve 取不到。
        self.assertEqual(mapping["22222222"]["dhash"], "")
        self.assertTrue(mapping["22222222"]["contaminated"])
        self.assertEqual(resolve_dhash("22222222_99999999-BC7_UNORM.dds", mapping), "")
        # 半截污染行（无 dhash）不是候选行。
        self.assertNotIn("33333333", mapping)

    def test_same_hash_with_two_different_dhashes_is_discarded(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = _write_log(
                Path(tmp),
                [
                    "0: view=... hash=44444444 dhash=1111111111111111",
                    "1: view=... hash=44444444 dhash=2222222222222222",
                ],
            )
            mapping = parse_frame_analysis_log(str(log_path))

        self.assertEqual(mapping["44444444"]["dhash"], "")
        self.assertTrue(mapping["44444444"]["contaminated"])
        self.assertEqual(resolve_dhash("44444444_99999999-BC7_UNORM.dds", mapping), "")

    def test_same_hash_same_dhash_is_kept_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = _write_log(
                Path(tmp),
                [
                    "0: view=... hash=55555555 dhash=cccccccccccccccc",
                    "1: view=... hash=55555555 dhash=cccccccccccccccc",
                ],
            )
            mapping = parse_frame_analysis_log(str(log_path))

        self.assertEqual(mapping["55555555"]["dhash"], "cccccccccccccccc")
        self.assertFalse(mapping["55555555"]["contaminated"])

    def test_hash_not_in_log_resolves_empty(self):
        mapping = {"abcdef12": {"dhash": CLEAN_DHASH, "format": "", "contaminated": False}}
        self.assertEqual(resolve_dhash("99999999_abcdef12-BC7_UNORM.dds", mapping), "")

    def test_resolve_uses_first_hash8_and_format(self):
        mapping = {"abcdef12": {"dhash": CLEAN_DHASH, "format": "", "contaminated": False}}
        name = "abcdef12_99999999-BC7_UNORM_SRGB.dds"
        self.assertEqual(resolve_dhash(name, mapping), CLEAN_DHASH)
        self.assertEqual(resolve_format(name), "BC7_UNORM_SRGB")
        # 根目录 000123-ps-t3=<hash8>-... 也能取到第一个 hash8。
        self.assertEqual(
            resolve_dhash(r"C:\dump\000123-ps-t3=abcdef12-99999999-BC7_UNORM.dds", mapping),
            CLEAN_DHASH,
        )
        # 原始 MarkFileName 末段不是格式。
        self.assertEqual(resolve_format("064cd7d3-2820-0-DiffuseMap.dds"), "")


class ApplyToMarksTests(unittest.TestCase):
    def test_only_matching_marks_are_changed(self):
        mapping = {
            "abcdef12": {"dhash": CLEAN_DHASH, "format": "", "contaminated": False},
            "deadbeef": {"dhash": "", "format": "", "contaminated": True},
        }
        matching = {
            "MarkName": "DiffuseMap",
            "MarkFileName": "orig-DiffuseMap.dds",
            "MarkDedupedFileName": "abcdef12_99999999-BC7_UNORM_SRGB.dds",
            "binding_mode": "slot",
        }
        contaminated = {
            "MarkName": "NormalMap",
            "MarkDedupedFileName": "deadbeef_99999999-R8G8B8A8_UNORM.dds",
        }
        missing = {
            "MarkName": "LightMap",
            "MarkFileName": "064cd7d3-2820-0-LightMap.dds",
        }

        class ObjectMark:
            mark_name = "MaterialMap"
            mark_filename = "abcdef12_88888888-BC6H_UF16.dds"

        object_mark = ObjectMark()
        matched, contaminated_count, missing_count = apply_to_marks(
            [matching, contaminated, missing, object_mark], mapping
        )

        self.assertEqual((matched, contaminated_count, missing_count), (3, 1, 1))
        self.assertEqual(matching["source_dhash"], CLEAN_DHASH)
        self.assertEqual(matching["source_format"], "BC7_UNORM_SRGB")
        self.assertEqual(matching["binding_mode"], "filter")
        self.assertNotIn("source_dhash", contaminated)
        self.assertNotIn("binding_mode", contaminated)
        self.assertEqual(missing, {"MarkName": "LightMap", "MarkFileName": "064cd7d3-2820-0-LightMap.dds"})
        self.assertEqual(object_mark.source_dhash, CLEAN_DHASH)
        self.assertEqual(object_mark.source_format, "BC6H_UF16")
        self.assertEqual(object_mark.binding_mode, "filter")


class WorkspaceWriteBackTests(unittest.TestCase):
    def _make_workspace(self, tmp: Path) -> Path:
        root = tmp / "workspace"
        submesh_json = (
            root
            / "LOD0"
            / "abc-1-0"
            / "TYPE_GPU_P12"
            / "abc-1-0.json"
        )
        _write_json(
            submesh_json,
            {
                "TextureMarkUpInfoList": [
                    {
                        "MarkName": "DiffuseMap",
                        "MarkHash": "abcdef12",
                        "MarkSlot": "ps-t3",
                        "MarkType": "Slot",
                        "MarkFileName": "abc-1-0-DiffuseMap.dds",
                        "MarkDedupedFileName": "abcdef12_99999999-BC7_UNORM_SRGB.dds",
                    },
                    {
                        "MarkName": "NormalMap",
                        "MarkHash": "deadbeef",
                        "MarkSlot": "ps-t4",
                        "MarkType": "Slot",
                        "MarkFileName": "abc-1-0-NormalMap.dds",
                        "MarkDedupedFileName": "deadbeef_99999999-R8G8B8A8_UNORM.dds",
                    },
                ],
                "KeepMe": {"nested": [1, 2, 3]},
            },
        )
        dump = tmp / "dump"
        _write_log(
            dump,
            [
                "0: view=... hash=abcdef12 dhash=" + CLEAN_DHASH,
                "1: view=... hash=deadbeef hash_contamination=UpdateSubresourceRegion, dhash="
                + CONTAMINATED_DHASH,
            ],
        )
        _write_json(
            root / "Config" / "FrameAnalysisPath.json",
            {"frameAnalysisFolderPath": str(dump)},
        )
        return root

    def test_apply_writes_submesh_json_and_cache(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._make_workspace(Path(tmp))
            matched = apply_workspace_dhash(str(root))
            self.assertEqual(matched, 1)

            submesh_path = (
                root / "LOD0" / "abc-1-0" / "TYPE_GPU_P12" / "abc-1-0.json"
            )
            payload = json.loads(submesh_path.read_text(encoding="utf-8"))
            marks = payload["TextureMarkUpInfoList"]
            self.assertEqual(marks[0]["source_dhash"], CLEAN_DHASH)
            self.assertEqual(marks[0]["source_format"], "BC7_UNORM_SRGB")
            self.assertEqual(marks[0]["binding_mode"], "filter")
            # 原有键全部保留。
            for key in (
                "MarkName",
                "MarkHash",
                "MarkSlot",
                "MarkType",
                "MarkFileName",
                "MarkDedupedFileName",
            ):
                self.assertIn(key, marks[0])
            # 污染 mark 保持原样。
            self.assertNotIn("source_dhash", marks[1])
            self.assertNotIn("binding_mode", marks[1])
            self.assertEqual(payload["KeepMe"], {"nested": [1, 2, 3]})
            self.assertNotIn(CONTAMINATED_DHASH, json.dumps(payload))
            self.assertNotIn(CONTAMINATED_DHASH, json.dumps(marks))

            cache_path = root / "Config" / "TextureDHashCache.json"
            self.assertTrue(cache_path.is_file())
            cache = json.loads(cache_path.read_text(encoding="utf-8"))
            entry = cache["entries"]["abc-1-0-DiffuseMap.dds"]
            self.assertEqual(entry["dhash"], CLEAN_DHASH)
            self.assertEqual(entry["format"], "BC7_UNORM_SRGB")
            self.assertEqual(entry["binding_mode"], "filter")
            self.assertEqual(entry["provider"], "log")
            self.assertNotIn(CONTAMINATED_DHASH, cache_path.read_text(encoding="utf-8"))

            marks, paths = workspace_marks(str(root))
            self.assertEqual(len(marks), 2)
            self.assertEqual(len(paths), 1)

    def test_missing_log_is_silent_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            root.mkdir()
            self.assertEqual(apply_workspace_dhash(str(root)), 0)
            self.assertFalse((root / "Config" / "TextureDHashCache.json").exists())

    def test_texture_stats_counts_deduped_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "workspace"
            textures = root / "LOD0" / "DedupedTextures"
            textures.mkdir(parents=True)
            for name in (
                "abcdef12_99999999-BC7_UNORM_SRGB.dds",
                "deadbeef_99999999-R8G8B8A8_UNORM.dds",
                "99999999_99999999-BC7_UNORM.dds",
                "abc-1-0-DiffuseMap.dds",  # 原始名，不参与去重贴图统计
            ):
                (textures / name).write_bytes(b"")
            mapping = {
                "abcdef12": {"dhash": CLEAN_DHASH, "format": "", "contaminated": False},
                "deadbeef": {"dhash": "", "format": "", "contaminated": True},
            }
            matched, usable, contaminated, missing, total = workspace_texture_stats(
                str(root), mapping
            )
            self.assertEqual((matched, usable, contaminated, missing, total), (2, 1, 1, 1, 3))


@unittest.skipUnless(REAL_LOG.is_file(), "真实 FrameAnalysis log 不在本机")
class RealLogTests(unittest.TestCase):
    def test_real_log_contract(self):
        mapping = parse_frame_analysis_log(str(REAL_LOG))
        self.assertEqual(len(mapping), 59)
        self.assertTrue(mapping["0275a048"]["contaminated"])
        self.assertEqual(mapping["0275a048"]["dhash"], "")
        self.assertTrue(mapping["ebac056e"]["contaminated"])
        self.assertEqual(mapping["ebac056e"]["dhash"], "")
        self.assertEqual(mapping["fe2cc6f3"]["dhash"], "9bc58e44bd2c19a4")
        self.assertFalse(mapping["fe2cc6f3"]["contaminated"])


if __name__ == "__main__":
    unittest.main()
