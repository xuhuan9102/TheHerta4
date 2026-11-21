"""ZZMI 骨骼合并（common/zzmi_skeleton.py）单测。

使用真实提取数据做 fixture：
- dump:      K:/SSMT-Package-master/3Dmigoto/ZZZ/FrameAnalysis-2026-08-19-122152
- 工作空间:  K:/SSMT-Package-master/WorkSpace/ZZMI/希格莉德·空岛传奇
路径不存在时整套跳过。
"""

import importlib.util
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock
from pathlib import Path

import numpy

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "zzmi_skeleton_test_pkg"


def _install_package(name):
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module
    return module


def _load_module(qualname, path):
    spec = importlib.util.spec_from_file_location(qualname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


for _name in (PKG, f"{PKG}.common", f"{PKG}.utils"):
    _install_package(_name)
_load_module(f"{PKG}.utils.json_utils", REPO_ROOT / "utils" / "json_utils.py")
_efmi = _load_module(f"{PKG}.common.efmi_skeleton", REPO_ROOT / "common" / "efmi_skeleton.py")
_load_module(f"{PKG}.common.zzmi_channel", REPO_ROOT / "common" / "zzmi_channel.py")
_zzmi = _load_module(f"{PKG}.common.zzmi_skeleton", REPO_ROOT / "common" / "zzmi_skeleton.py")

EFMIBoneMapBuilder = _efmi.EFMIBoneMapBuilder

ZZMIBoneMapBuilder = _zzmi.ZZMIBoneMapBuilder
ZZMIDeformResolver = _zzmi.ZZMIDeformResolver
ZZMILogParser = _zzmi.ZZMILogParser
ZZMISkeletonMergeHelper = _zzmi.ZZMISkeletonMergeHelper

DUMP_DIR = r"K:\SSMT-Package-master\3Dmigoto\ZZZ\FrameAnalysis-2026-08-19-122152"
WORKSPACE = r"K:\SSMT-Package-master\WorkSpace\ZZMI\希格莉德·空岛传奇"
LOG_PATH = os.path.join(DUMP_DIR, "log.txt")

# 实测期望值（见 ZZMI骨骼合并计划书.md §2.5；去重 2026-08-24 起含刚性部件质心门控）
EXPECTED_VG_COUNT = {
    "84618ee0": 49, "a23aa8a3": 105, "19086112": 7, "b51bdd59": 11,
    "b20f90ea": 51, "b30db54e": 14, "48625d6d": 10, "d892c658": 16,
    "64d7d56f": 1, "454ff522": 1, "add6ff13": 1,
}
EXPECTED_DEFORM_DRAW = {  # drawib -> deform pass draw index
    "84618ee0": "000004", "a23aa8a3": "000020", "19086112": "000035",
    "b51bdd59": "000036", "b20f90ea": "000002", "b30db54e": "000008",
    "48625d6d": "000018", "d892c658": "000010", "64d7d56f": "000001",
    "454ff522": "000029", "add6ff13": "000030",
}
EXPECTED_TOTAL_SLOTS = 266
# 刚性门控后唯一骨骼数：244 + 2（64d7d56f 头顶件、b51bdd59#0 后脑发饰骨
# 从抓帧重合的头部锚点组中被质心门控拆开）
EXPECTED_UNIQUE_BONES = 246


def _list_submesh_jsons(workspace_root):
    """遍历 LOD0 全部子网格 json，返回 (unique_str, json_path, json_dict)。"""
    lod0 = os.path.join(workspace_root, "LOD0")
    import_json = {}
    import_json_path = os.path.join(workspace_root, "Import.json")
    if os.path.isfile(import_json_path):
        with open(import_json_path, encoding="utf-8") as f:
            import_json = json.load(f)
    results = []
    for name in sorted(os.listdir(lod0)):
        type_parent = os.path.join(lod0, name)
        if not os.path.isdir(type_parent):
            continue
        unique_str = "LOD0." + name
        gametype = import_json.get(unique_str, "")
        if not gametype:
            continue
        json_path = os.path.join(type_parent, "TYPE_" + gametype, name + ".json")
        if not os.path.isfile(json_path):
            continue
        with open(json_path, encoding="utf-8") as f:
            results.append((unique_str, json_path, json.load(f)))
    return results


@unittest.skipUnless(os.path.isfile(LOG_PATH) and os.path.isdir(WORKSPACE), "提取数据不在本机")
class TestZZMILogParser(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parser = ZZMILogParser(LOG_PATH)

    def test_deform_pass_count_and_spot_checks(self):
        passes = self.parser.get_deform_passes()
        self.assertEqual(len(passes), 22)

        p2 = passes["000002"]
        self.assertEqual(p2["vertex_count"], 4643)
        self.assertEqual(p2["vb0_hash"], "122883aa")
        self.assertEqual(p2["so_hash"], "dd9c8d5e")
        self.assertEqual(p2["palette_hash"], "c2f5419a")

        p36 = passes["000036"]
        self.assertEqual(p36["vertex_count"], 345)
        self.assertEqual(p36["so_hash"], "6a8ea608")
        self.assertEqual(p36["palette_hash"], "c6c3b31d")

    def test_render_draw_vb0_and_ib(self):
        # 渲染 draw 000039: ib=84618ee0, vb0=840c1713（= deform 000004 的 SO 输出）
        self.assertEqual(self.parser.draws["000039"]["ib"], "84618ee0")
        self.assertEqual(self.parser.get_ib_hash("000039"), "84618ee0")
        self.assertEqual(self.parser.get_vb_hash("000039", 0), "840c1713")
        self.assertEqual(self.parser.get_vb_hash("000040", 0), "01b35c45")

    def test_dump_map_palette_path(self):
        path = self.parser.get_deduped_path("000002-vs-t0=c2f5419a")
        self.assertIsNotNone(path)
        self.assertTrue(path.endswith(os.path.join("deduped", "e018278f.buf")))

    def test_palette_load_shape(self):
        path = self.parser.get_deduped_path("000004-vs-t0=f6a6c781")
        palette = ZZMIBoneMapBuilder.load_palette(path)
        self.assertEqual(palette.shape, (49, 12))


@unittest.skipUnless(os.path.isfile(LOG_PATH) and os.path.isdir(WORKSPACE), "提取数据不在本机")
class TestZZMIDeformResolver(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.parser = ZZMILogParser(LOG_PATH)
        cls.resolver = ZZMIDeformResolver(cls.parser)

    def test_all_submeshes_resolve_path_a_and_b(self):
        for unique_str, _json_path, submesh_json in _list_submesh_jsons(WORKSPACE):
            draw_ib = unique_str.split(".", 1)[-1].split("-")[0]
            expected_draw = EXPECTED_DEFORM_DRAW[draw_ib]

            position_hash = (submesh_json.get("CategoryHash") or {}).get("Position", "")
            draw_a, pass_a, via_a = self.resolver.resolve(position_hash=position_hash)
            self.assertEqual(via_a, "A", unique_str)
            self.assertEqual(draw_a, expected_draw, unique_str)
            self.assertEqual(pass_a["vertex_count"], pass_a["vertex_count"])

            vertex_limit_hash = submesh_json.get("VertexLimitVB", "")
            draw_b, _pass_b, via_b = self.resolver.resolve(vertex_limit_hash=vertex_limit_hash)
            self.assertEqual(via_b, "B", unique_str)
            self.assertEqual(draw_b, expected_draw, unique_str)

    def test_resolve_path_c_fallback(self):
        # 只用渲染 draw 列表（路径 C）：b20f90ea-19182-0 -> 渲染 draw 38/44/192/213/225
        draw, deform_pass, via = self.resolver.resolve(
            render_draw_indices=["000038", "000044", "000192", "000213", "000225"],
            expected_draw_ib="b20f90ea",
        )
        self.assertEqual(via, "C")
        self.assertEqual(draw, "000002")
        self.assertEqual(deform_pass["so_hash"], "dd9c8d5e")


class TestZZMIDeformResolverIBGate(unittest.TestCase):
    """路径 C 必须按目标 DrawIB 过滤渲染 draw，避免相似模型串台。"""

    class _Parser:
        def get_deform_passes(self):
            return {
                "000001": {
                    "vertex_count": 10,
                    "so_hash": "shared-so",
                    "palette_hash": "palette-a",
                    "vb0_hash": "bind-a",
                }
            }

        def get_vb_hash(self, draw_index, slot):
            return "shared-so" if draw_index in {"000101", "000102"} and slot == 0 else ""

        def get_ib_hash(self, draw_index):
            return {"000101": "other-model", "000102": "target-model"}.get(draw_index, "")

    def setUp(self):
        self.resolver = ZZMIDeformResolver(self._Parser())

    def test_path_c_skips_same_so_from_other_ib(self):
        draw, _deform_pass, via = self.resolver.resolve(
            render_draw_indices=["000101"],
            expected_draw_ib="target-model",
        )
        self.assertEqual((draw, via), ("", ""))

    def test_path_c_accepts_target_ib_after_skipping_other_model(self):
        draw, deform_pass, via = self.resolver.resolve(
            render_draw_indices=["000101", "000102"],
            expected_draw_ib="target-model",
        )
        self.assertEqual(draw, "000001")
        self.assertEqual(deform_pass["so_hash"], "shared-so")
        self.assertEqual(via, "C")


class TestZZMILogParserDrawIndexed(unittest.TestCase):
    """新旧 3Dmigoto 构建的两种 indexed draw 都必须保留渲染身份。"""

    def test_non_instanced_draw_indexed_is_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "log.txt"
            log_path.write_text(
                "000001 IASetIndexBuffer(format:R32_UINT, offset:0) hash=abcdef12\n"
                "000001 IASetVertexBuffers(StartSlot:0, NumBuffers:1, ppVertexBuffers:0x0, pStrides:0x0, pOffsets:0x0)\n"
                " 0: view=0x0 resource=0x0 hash=12345678\n"
                "000001 DrawIndexed(IndexCount:6, StartIndexLocation:12, BaseVertexLocation:4)\n",
                encoding="utf-8",
            )
            parser = ZZMILogParser(str(log_path))
            self.assertEqual(parser.get_ib_hash("000001"), "abcdef12")
            self.assertEqual(parser.get_vb_hash("000001", 0), "12345678")
            self.assertEqual(
                parser.draws["000001"]["draw_indexed"],
                {"index_count": 6, "start_index": 12, "base_vertex": 4},
            )

    def test_index_buffer_binding_is_inherited_by_later_draw(self):
        """IASetIndexBuffer 是持久状态时，后续 DrawIndexed 仍须保留 DrawIB 身份。"""
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "log.txt"
            log_path.write_text(
                "000001 IASetIndexBuffer(format:R32_UINT, offset:0) hash=abcdef12\n"
                "000001 DrawIndexed(IndexCount:6, StartIndexLocation:0, BaseVertexLocation:0)\n"
                "000002 DrawIndexed(IndexCount:3, StartIndexLocation:6, BaseVertexLocation:0)\n",
                encoding="utf-8",
            )
            parser = ZZMILogParser(str(log_path))
            self.assertEqual(parser.get_ib_hash("000001"), "abcdef12")
            self.assertEqual(parser.get_ib_hash("000002"), "abcdef12")


@unittest.skipUnless(os.path.isfile(LOG_PATH) and os.path.isdir(WORKSPACE), "提取数据不在本机")
class TestZZMIBoneMapBuilder(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        parser = ZZMILogParser(LOG_PATH)
        cls.palettes = {}
        for draw_ib, draw_index in EXPECTED_DEFORM_DRAW.items():
            deform_pass = parser.get_deform_passes()[draw_index]
            path = parser.get_deduped_path(f"{draw_index}-vs-t0={deform_pass['palette_hash']}")
            cls.palettes[draw_ib] = ZZMIBoneMapBuilder.load_palette(path)
        # 驱动签名（生产路径同款：ensure_skeleton_data 对每个 DrawIB 代表子网格计算）
        cls.signatures = {}
        for unique_str, json_path, submesh_json in _list_submesh_jsons(WORKSPACE):
            draw_ib = unique_str.split(".", 1)[-1].split("-")[0]
            if draw_ib in cls.signatures:
                continue  # 同 DrawIB 拆分子网格共享 palette，取代表即可
            base = os.path.splitext(os.path.basename(json_path))[0]
            dirpath = os.path.dirname(json_path)
            cls.signatures[draw_ib] = EFMIBoneMapBuilder.compute_driven_signatures(
                os.path.join(dirpath, base + "-Position.buf"),
                os.path.join(dirpath, base + "-Blend.buf"),
                submesh_json,
            )
        cls.vg_maps, cls.vg_offsets, cls.total_slots = ZZMIBoneMapBuilder.build_vg_maps(
            cls.palettes, cls.signatures
        )

    def test_palette_counts_match_measured(self):
        for draw_ib, palette in self.palettes.items():
            self.assertEqual(len(palette), EXPECTED_VG_COUNT[draw_ib], draw_ib)

    def test_total_slots(self):
        self.assertEqual(self.total_slots, EXPECTED_TOTAL_SLOTS)

    def test_unique_bone_count(self):
        unique_globals = set()
        for vg_map in self.vg_maps.values():
            unique_globals.update(vg_map.values())
        self.assertEqual(len(unique_globals), EXPECTED_UNIQUE_BONES)

    def test_same_part_never_merged(self):
        # 每个部件内部：所有 local 都被映射，且不会有两个 local 映射到
        # 本部件 vg_offset 段内的同一槽位（同部件不去重）
        for draw_ib, vg_map in self.vg_maps.items():
            vg_count = EXPECTED_VG_COUNT[draw_ib]
            self.assertEqual(sorted(vg_map.keys()), list(range(vg_count)), draw_ib)
            offset = self.vg_offsets[draw_ib]
            own_slots = [g for g in vg_map.values() if offset <= g < offset + vg_count]
            self.assertEqual(len(own_slots), len(set(own_slots)), draw_ib)

    def test_known_shared_bone_merged(self):
        # 454ff522#0（前额薄件）与 48625d6d#2（面部锚点）：bitwise 全同且质心距 0.034
        # 通过刚性门控 -> 仍合并
        g1 = self.vg_maps["454ff522"][0]
        g2 = self.vg_maps["48625d6d"][2]
        self.assertEqual(g1, g2)

    def test_rigid_gate_splits_coincident_head_attachments(self):
        # 刚性门控回归（2026-08-24）：64d7d56f#0（头顶件，质心距 0.072）与
        # b51bdd59#0（后脑物理发饰骨，质心距 0.18~0.20）虽与共享矩阵 bitwise 全同，
        # 但质心过远 -> 各占各槽（抓帧重合的不同锚点骨，动画分叉时绝不可共用槽位）
        shared_global = self.vg_maps["454ff522"][0]
        self.assertNotEqual(self.vg_maps["64d7d56f"][0], shared_global)
        self.assertNotEqual(self.vg_maps["b51bdd59"][0], shared_global)
        self.assertNotEqual(self.vg_maps["64d7d56f"][0], self.vg_maps["b51bdd59"][0])

    def test_same_bone_different_frame_not_merged(self):
        # 回归：48625d6d 的 #1/#8/#9 与共享骨骼 maxdiff 3.5e-07~2.5e-06（同骨骼异帧），
        # 绝不可与共享骨骼的全局 id 相同
        shared_global = self.vg_maps["64d7d56f"][0]
        for local in (1, 8, 9):
            self.assertNotEqual(self.vg_maps["48625d6d"][local], shared_global)

    def test_b20f90ea_thirteen_shared_with_a23aa8a3(self):
        # b20f90ea 的 13 根共享骨骼应映射到 a23aa8a3 段内
        a_offset = self.vg_offsets["a23aa8a3"]
        a_count = EXPECTED_VG_COUNT["a23aa8a3"]
        into_a = [
            local for local, global_id in self.vg_maps["b20f90ea"].items()
            if a_offset <= global_id < a_offset + a_count
        ]
        self.assertEqual(len(into_a), 13)


@unittest.skipUnless(os.path.isfile(LOG_PATH) and os.path.isdir(WORKSPACE), "提取数据不在本机")
class TestZZMISkeletonMergeHelper(unittest.TestCase):
    def setUp(self):
        # 把工作空间反查所需文件复制到临时目录（json + Blend.buf + 配置，跳过贴图）
        self.tmp = tempfile.mkdtemp(prefix="zzmi_ws_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        for entry in ("Import.json",):
            shutil.copy2(os.path.join(WORKSPACE, entry), os.path.join(self.tmp, entry))
        shutil.copytree(
            os.path.join(WORKSPACE, "Config"), os.path.join(self.tmp, "Config")
        )
        lod0_src = os.path.join(WORKSPACE, "LOD0")
        lod0_dst = os.path.join(self.tmp, "LOD0")
        os.makedirs(lod0_dst, exist_ok=True)
        for name in os.listdir(lod0_src):
            src_path = os.path.join(lod0_src, name)
            if os.path.isfile(src_path) and name.endswith(".json"):
                shutil.copy2(src_path, os.path.join(lod0_dst, name))
                continue
            if not os.path.isdir(src_path):
                continue
            for type_dir in os.listdir(src_path):
                if not type_dir.startswith("TYPE_"):
                    continue
                type_src = os.path.join(src_path, type_dir)
                type_dst = os.path.join(lod0_dst, name, type_dir)
                os.makedirs(type_dst, exist_ok=True)
                for file_name in os.listdir(type_src):
                    if file_name.endswith(".json") or file_name.endswith("-Blend.buf") or file_name.endswith("-Position.buf"):
                        shutil.copy2(
                            os.path.join(type_src, file_name),
                            os.path.join(type_dst, file_name),
                        )
        # 剥离已写回的骨骼合并字段，模拟纯净工作空间（真实工作空间可能已被 e2e 写回过）
        for name in os.listdir(os.path.join(self.tmp, "LOD0")):
            type_parent = os.path.join(self.tmp, "LOD0", name)
            if not os.path.isdir(type_parent):
                continue
            for type_dir in os.listdir(type_parent):
                if not type_dir.startswith("TYPE_"):
                    continue
                json_path = os.path.join(type_parent, type_dir, name + ".json")
                if not os.path.isfile(json_path):
                    continue
                with open(json_path, encoding="utf-8") as f:
                    payload = json.load(f)
                changed = False
                for field in (
                    "VGMap", "VGOffset", "VGCount", "BoneMatrixFileName",
                    "VGMapAlgorithmVersion", "ObjectCB1FileName",
                    "SkeletonGroupCb1SourceIb", "DeformDrawIndex", "OriginalVertexCount",
                ):
                    if field in payload:
                        payload.pop(field)
                        changed = True
                if changed:
                    with open(json_path, "w", encoding="utf-8") as f:
                        json.dump(payload, f, ensure_ascii=False, indent=4)

    def _unique_str_list(self):
        with open(os.path.join(self.tmp, "Import.json"), encoding="utf-8") as f:
            import_json = json.load(f)
        return sorted(import_json.keys())

    def test_ensure_skeleton_data_end_to_end(self):
        unique_str_list = self._unique_str_list()
        self.assertEqual(len(unique_str_list), 15)

        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=self.tmp, unique_str_list=unique_str_list
        )
        self.assertTrue(ok, message)

        # 每个子网格 json 写回了 VGMap/VGOffset/VGCount，且 ModImpRuntime 缓存存在
        for unique_str, json_path, submesh_json in _list_submesh_jsons(self.tmp):
            draw_ib = unique_str.split(".", 1)[-1].split("-")[0]
            vg_map = submesh_json.get("VGMap")
            self.assertTrue(vg_map, unique_str)
            self.assertEqual(submesh_json.get("VGCount"), EXPECTED_VG_COUNT[draw_ib], unique_str)
            self.assertEqual(len(vg_map), EXPECTED_VG_COUNT[draw_ib], unique_str)

            bare = unique_str.split(".", 1)[-1]
            cache_path = os.path.join(
                self.tmp, "LOD0", bare, "ModImpRuntime", bare + "-BoneMatrix.buf"
            )
            self.assertTrue(os.path.isfile(cache_path), unique_str)
            palette = ZZMIBoneMapBuilder.load_palette(cache_path)
            self.assertEqual(len(palette), EXPECTED_VG_COUNT[draw_ib], unique_str)

        # 同 DrawIB 拆分子网格：VGMap/VGOffset 必须一致
        jsons = {u: j for u, _p, j in _list_submesh_jsons(self.tmp)}
        a = jsons["LOD0.84618ee0-22296-0"]
        b = jsons["LOD0.84618ee0-1164-22296"]
        self.assertEqual(a["VGMap"], b["VGMap"])
        self.assertEqual(a["VGOffset"], b["VGOffset"])

        # 刚性门控：454ff522#0（前额件）与 48625d6d#2（面部锚点，质心距 0.034）仍合并；
        # 64d7d56f#0（头顶件，质心距 0.072）被拆开（抓帧重合的不同锚点骨）
        g_face_band = jsons["LOD0.454ff522-216-0"]["VGMap"]["0"]
        sub_486 = sorted(u for u in jsons if "48625d6d" in u)[0]
        self.assertEqual(jsons[sub_486]["VGMap"]["2"], g_face_band)
        g_top = jsons["LOD0.64d7d56f-900-0"]["VGMap"]["0"]
        self.assertNotEqual(g_top, g_face_band)

        # 骨架分组（渲染 cb1 对象变换 1:1 配对）：实测 5 组
        # 身体组 {a23aa8a3, b20f90ea, b30db54e}、头部组 {454ff522, 48625d6d, 64d7d56f, b51bdd59}、
        # 头发 84618ee0 独立、19086112 独立、{add6ff13, d892c658} 共享同一对象空间
        group_of = {}
        for unique_str, _json_path, submesh_json in _list_submesh_jsons(self.tmp):
            draw_ib = unique_str.split(".", 1)[-1].split("-")[0]
            self.assertIn("SkeletonGroup", submesh_json, unique_str)
            group_of[draw_ib] = submesh_json["SkeletonGroup"]

        body = {"a23aa8a3", "b20f90ea", "b30db54e"}
        head = {"454ff522", "48625d6d", "64d7d56f", "b51bdd59"}
        self.assertEqual(len({group_of[d] for d in body}), 1)
        self.assertEqual(len({group_of[d] for d in head}), 1)
        self.assertEqual(len(set(group_of.values())), 5)
        self.assertNotEqual(group_of["a23aa8a3"], group_of["454ff522"])
        self.assertNotEqual(group_of["84618ee0"], group_of["a23aa8a3"])
        self.assertNotEqual(group_of["19086112"], group_of["a23aa8a3"])
        # add6ff13 与 d892c658 共享同一对象空间（实测同一 cb1 变换）-> 同组
        self.assertEqual(group_of["add6ff13"], group_of["d892c658"])
        self.assertNotEqual(group_of["add6ff13"], group_of["a23aa8a3"])
        # 同组判定的刚性门控拆分不改变分组：头顶件与头部组同组（同空间，仅锚点不同）
        self.assertEqual(group_of["64d7d56f"], group_of["454ff522"])

        # 全局骨骼编号（组基址拼接；组序按组内最小 draw_ib）：
        # G0={19086112}(7) base 0 / G1=头部(23) base 7 / G2={84618ee0}(49) base 30 /
        # G3=身体(170) base 79 / G4={add6ff13,d892c658}(17) base 249
        jsons_by_ib = {u.split(".", 1)[-1].split("-")[0]: j for u, _p, j in _list_submesh_jsons(self.tmp)}
        expected_offsets = {
            "19086112": 0,
            "454ff522": 7, "48625d6d": 8, "64d7d56f": 18, "b51bdd59": 19,
            "84618ee0": 30,
            "a23aa8a3": 79, "b20f90ea": 184, "b30db54e": 235,
            "add6ff13": 249, "d892c658": 250,
        }
        for draw_ib, expected_offset in expected_offsets.items():
            self.assertEqual(jsons_by_ib[draw_ib]["VGOffset"], expected_offset, draw_ib)

        # 无 CB1 校准（2026-08-25 用户拍板）：不再写 cb1 捕获源字段
        for draw_ib, submesh_json in jsons_by_ib.items():
            self.assertNotIn("SkeletonGroupCb1SourceIb", submesh_json, draw_ib)

        # 导出侧守卫元数据：deform draw 序号 + 原部件顶点数（合并网格时序校验/vb1 换绑）
        for unique_str, _json_path, submesh_json in _list_submesh_jsons(self.tmp):
            draw_ib = unique_str.split(".", 1)[-1].split("-")[0]
            expected_draw = int(EXPECTED_DEFORM_DRAW[draw_ib])
            self.assertEqual(submesh_json.get("DeformDrawIndex"), expected_draw, unique_str)
            self.assertGreater(submesh_json.get("OriginalVertexCount", 0), 0, unique_str)
        # 实测原部件顶点数（deform draw vertex count）：组 3 三个部件的 Blend.buf 行数
        original_counts = {
            "b20f90ea": 4643, "a23aa8a3": 12314, "b30db54e": 1744,
            "84618ee0": 5846, "19086112": 3288, "b51bdd59": 345,
        }
        for draw_ib, expected_count in original_counts.items():
            self.assertEqual(
                jsons_by_ib[draw_ib]["OriginalVertexCount"], expected_count, draw_ib
            )

    def test_idempotent_second_run_skips(self):
        unique_str_list = self._unique_str_list()
        ok1, _ = ZZMISkeletonMergeHelper.ensure_skeleton_data(self.tmp, unique_str_list)
        self.assertTrue(ok1)
        ok2, message2 = ZZMISkeletonMergeHelper.ensure_skeleton_data(self.tmp, unique_str_list)
        self.assertTrue(ok2, message2)
        self.assertIn("跳过", message2)

    def test_force_rebuild(self):
        unique_str_list = self._unique_str_list()
        ZZMISkeletonMergeHelper.ensure_skeleton_data(self.tmp, unique_str_list)
        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            self.tmp, unique_str_list, force=True
        )
        self.assertTrue(ok, message)
        self.assertIn("15 个子网格", message)

class TestBlendChannelValidation(unittest.TestCase):
    """P2 回归：BLENDINDICES 无效通道（哨兵/零权重）不能算成真实骨骼。"""

    @staticmethod
    def _info(np_type):
        return {"np_type": np_type}

    def test_u2_sentinel_excluded_even_with_positive_weight(self):
        indices = numpy.array([[0, 1, 0xFFFF, 2]], dtype=numpy.uint32)
        weights = numpy.array([[1.0, 1.0, 1.0, 1.0]], dtype=numpy.float32)
        mask = EFMIBoneMapBuilder.valid_blend_channels(indices, self._info("u2"), weights)
        self.assertEqual(mask.tolist(), [[True, True, False, True]])

    def test_i4_sentinel_wraps_to_ffffffff(self):
        raw = numpy.array([-1], dtype=numpy.int32)
        indices = raw.astype(numpy.uint32).reshape(1, 1)
        weights = numpy.array([[1.0]], dtype=numpy.float32)
        mask = EFMIBoneMapBuilder.valid_blend_channels(indices, self._info("i4"), weights)
        self.assertFalse(bool(mask[0, 0]))

    def test_u4_sentinel_excluded(self):
        indices = numpy.array([[7, 0xFFFFFFFF]], dtype=numpy.uint32)
        weights = numpy.array([[1.0, 1.0]], dtype=numpy.float32)
        mask = EFMIBoneMapBuilder.valid_blend_channels(indices, self._info("u4"), weights)
        self.assertEqual(mask.tolist(), [[True, False]])

    def test_zero_weight_channel_excluded(self):
        indices = numpy.array([[0, 9, 3, 1]], dtype=numpy.uint32)
        weights = numpy.array([[0.5, 0.0, 0.3, 0.2]], dtype=numpy.float32)
        mask = EFMIBoneMapBuilder.valid_blend_channels(indices, self._info("u1"), weights)
        self.assertEqual(mask.tolist(), [[True, False, True, True]])

    def test_missing_weights_defaults_to_first_channel_only(self):
        indices = numpy.array([[0, 9, 3, 1]], dtype=numpy.uint32)
        mask = EFMIBoneMapBuilder.valid_blend_channels(indices, self._info("u1"), None)
        self.assertEqual(mask.tolist(), [[True, False, False, False]])

    def test_parse_blend_layout_and_weights_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            json_path = Path(tmp) / "sub.json"
            buf_path = Path(tmp) / "sub-Blend.buf"
            json_path.write_text(json.dumps({
                "CategoryBufferList": [{"D3D11ElementList": [
                    {"Category": "Blend", "SemanticName": "BLENDINDICES",
                     "Format": "R16G16_UINT", "ByteWidth": 4},
                    {"Category": "Blend", "SemanticName": "BLENDWEIGHTS",
                     "Format": "R32G32B32A32_FLOAT", "ByteWidth": 16},
                ]}],
            }), encoding="utf-8")
            layout = EFMIBoneMapBuilder.parse_blend_layout(
                json.loads(json_path.read_text(encoding="utf-8"))
            )
            self.assertEqual(layout["bi_np"], "u2")
            self.assertEqual(layout["bi_channels"], 2)
            self.assertEqual(layout["bw_np"], "f4")
            self.assertEqual(layout["stride"], 20)
            rows = numpy.zeros((3, 5), dtype=numpy.float32)  # 20 字节/行
            # 索引占前 4 字节（float0），权重从 float1 开始
            rows[:, 1] = [0.25, 0.5, 1.0]
            buf_path.write_bytes(rows.astype(numpy.float32).tobytes())
            weights = EFMIBoneMapBuilder.parse_blendweights_from_buf(str(buf_path), layout)
            self.assertEqual(weights.shape, (3, 4))
            self.assertAlmostEqual(float(weights[1, 0]), 0.5, places=6)


class SyntheticZZMIR16SentinelTests(unittest.TestCase):
    """P2 回归：R16_UINT 的 0xFFFF 哨兵通道不膨胀 vg_count、不整部件跳过。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zzmi_r16_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        root = Path(self.tmp)
        dump = root / "dump"
        (dump / "deduped").mkdir(parents=True, exist_ok=True)
        deduped_abs = (dump / "deduped").resolve()

        palette = numpy.zeros((3, 12), dtype=numpy.float32)
        palette[:, 0] = [1.0, 2.0, 3.0]
        palette_name = "000001-vs-t0=cccccccc.buf"
        (dump / "deduped" / palette_name).write_bytes(palette.tobytes())
        log_lines = [
            "000001 IASetVertexBuffers(StartSlot:0, NumBuffers:3,",
            "0: resource=0x00000000 hash=aaaaaaaa",
            "000001 SOSetTargets(NumBuffers:1,",
            "0: resource=0x00000000 hash=bbbbbbbb",
            "000001 VSSetShaderResources(StartSlot:0, NumViews:1,",
            "0: view=0x00000000 resource=0x00000000 hash=cccccccc",
            "000001 Draw(VertexCount:4, StartVertexLocation:0)",
            f"000001 3DMigoto Dumping Buffer {palette_name} -> {deduped_abs / palette_name}",
        ]
        (dump / "log.txt").write_text("\n".join(log_lines) + "\n", encoding="utf-8")

        self.ws = root / "ws"
        (self.ws / "Config").mkdir(parents=True, exist_ok=True)
        (self.ws / "Config" / "FrameAnalysisPath.json").write_text(
            json.dumps({"frameAnalysisFolderPath": str(dump)}), encoding="utf-8"
        )
        bare = "eeeeffff-100-0"
        gametype = "GPU_P12_"
        type_dir = self.ws / "LOD0" / bare / ("TYPE_" + gametype)
        type_dir.mkdir(parents=True, exist_ok=True)
        (type_dir / f"{bare}.json").write_text(json.dumps({
            "CategoryHash": {"Position": "aaaaaaaa"},
            "CategoryBufferList": [{"D3D11ElementList": [
                {"Category": "Blend", "SemanticName": "BLENDINDICES",
                 "Format": "R16_UINT", "ByteWidth": 2},
            ]}],
        }), encoding="utf-8")
        # R16_UINT 单通道：第 3 个顶点是 0xFFFF 哨兵（无权重通道的标准填充值）
        indices = numpy.array([0, 1, 0xFFFF, 2], dtype=numpy.uint16)
        (type_dir / f"{bare}-Blend.buf").write_bytes(indices.tobytes())
        (self.ws / "Import.json").write_text(
            json.dumps({f"LOD0.{bare}": gametype}), encoding="utf-8"
        )
        self.unique = f"LOD0.{bare}"

    def test_ensure_skeleton_data_ignores_r16_sentinel(self):
        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=[self.unique]
        )
        self.assertTrue(ok, message)

        json_path = (
            self.ws / "LOD0" / "eeeeffff-100-0" / "TYPE_GPU_P12_" / "eeeeffff-100-0.json"
        )
        payload = json.loads(json_path.read_text(encoding="utf-8"))
        self.assertEqual(payload.get("VGCount"), 3, "0xFFFF 哨兵不能算成真实骨骼")
        self.assertEqual(len(payload.get("VGMap", {})), 3)
        # 缓存与 VGCount 一致（3 根骨骼）
        cache_path = (
            self.ws / "LOD0" / "eeeeffff-100-0" / "ModImpRuntime"
            / "eeeeffff-100-0-BoneMatrix.buf"
        )
        self.assertTrue(cache_path.is_file())
        cached_palette = ZZMIBoneMapBuilder.load_palette(str(cache_path))
        self.assertEqual(len(cached_palette), 3)


class MovedDumpDedupedFallbackTests(unittest.TestCase):
    """P2 回归：FrameAnalysis 搬走后 deduped 文件按候选路径恢复。"""

    def test_get_deduped_path_falls_back_to_current_dump_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dump = root / "dump"
            (dump / "deduped").mkdir(parents=True)
            stale = root / "old-dump" / "deduped" / "e018278f.buf"  # 记录路径已失效
            real = dump / "deduped" / "e018278f.buf"
            real.write_bytes(b"palette")
            log = dump / "log.txt"
            log.write_text(
                f"000002 3DMigoto Dumping Buffer 000002-vs-t0=c2f5419a.buf -> {stale}\n",
                encoding="utf-8",
            )

            parser = ZZMILogParser(str(log))

            self.assertEqual(parser.get_deduped_path("000002-vs-t0=c2f5419a"), str(real))

    def test_get_render_cb1_path_resolves_moved_dump(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dump = root / "dump"
            (dump / "deduped").mkdir(parents=True)
            stale = root / "old-dump" / "deduped" / "shared_cb1.buf"
            real = dump / "deduped" / "shared_cb1.buf"
            real.write_bytes(b"cb1")
            log = dump / "log.txt"
            log.write_text(
                "000010 3DMigoto Dumping Buffer "
                f"000010-vs-cb1=77777777-vs=aaaaaaaaaaaaaaaa.buf -> {stale}\n",
                encoding="utf-8",
            )

            parser = ZZMILogParser(str(log))

            self.assertEqual(parser.get_render_cb1_path("000010"), str(real))


class SharedCb1WindowTests(unittest.TestCase):
    """回归 FrameAnalysis-2026-09-16-014450：多对象共享 cb1 数组按窗口取对象变换。

    同一 4096B 资源被 draw 64/65/66 按 first_constant=0/32/64 逐 512B 一窗切给
    三个对象。旧实现无视窗口读 float 0（= 别的对象的矩阵），把**本来同对象空间**
    的部件拆进不同 SkeletonGroup/合集。这里断言：两个 draw 各绑自己的窗口、
    窗口内 16 floats 逐位相同 → 解析出同一分组键 → 同一骨架组。
    """

    # 两个不同对象的对象变换（平移不同）
    TF_A = (
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
        599.68475, 1.51398, 0.14267, 1.0,
    )
    TF_SHARED = (
        -0.13356, -0.98743, 0.08457, 0.0,
        0.25211, -0.11638, -0.96068, 0.0,
        0.95844, -0.10698, 0.26448, 0.0,
        599.99939, 0.95109, -0.00280, 1.0,
    )

    def _write_shared_cb1(self, path):
        """4096B 共享数组：窗口 0 = 对象 A、窗口 64 = 本部件对象。"""
        rows = numpy.zeros((256, 4), dtype=numpy.float32)
        rows[0:4] = numpy.array(self.TF_A, dtype=numpy.float32).reshape(4, 4)
        rows[64:68] = numpy.array(self.TF_SHARED, dtype=numpy.float32).reshape(4, 4)
        rows.tofile(path)

    def _write_per_object_cb1(self, path):
        """512B 逐部件块：对象变换在 float 0。"""
        rows = numpy.zeros((32, 4), dtype=numpy.float32)
        rows[0:4] = numpy.array(self.TF_SHARED, dtype=numpy.float32).reshape(4, 4)
        rows.tofile(path)

    def test_window_makes_same_object_space_parts_share_one_group(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dump = root / "dump"
            deduped = dump / "deduped"
            deduped.mkdir(parents=True)
            shared = deduped / "aaaa0001.buf"      # 4096B 共享数组
            per_object = deduped / "bbbb0002.buf"  # 512B 逐部件块
            self._write_shared_cb1(shared)
            self._write_per_object_cb1(per_object)
            log = dump / "log.txt"
            log.write_text(
                "000066 IASetIndexBuffer(pIndexBuffer:0x1, Format:57, "
                "Offset:0) hash=cdb2cc7d\n"
                "000066 VSSetConstantBuffers1(StartSlot:1, NumBuffers:1, "
                "ppConstantBuffers:0x1, pFirstConstant:0x2, pNumConstants:0x3)\n"
                "       1: resource=0x0000017AD0994738 hash=a394978d "
                "first_constant=64 num_constants=32\n"
                "000066 DrawIndexedInstanced(IndexCountPerInstance:30177, "
                "InstanceCount:1, StartIndexLocation:0, BaseVertexLocation:0, "
                "StartInstanceLocation:0)\n"
                "000066 3DMigoto Dumping Buffer "
                f"000066-vs-cb1=a394978d-vs=c280f6945b23a42a.buf -> {shared}\n"
                "000069 IASetIndexBuffer(pIndexBuffer:0x2, Format:57, "
                "Offset:0) hash=2825da1e\n"
                "000069 VSSetConstantBuffers1(StartSlot:1, NumBuffers:1, "
                "ppConstantBuffers:0x1, pFirstConstant:0x2, pNumConstants:0x3)\n"
                "       1: resource=0x0000017AD098B978 hash=0c8934aa "
                "first_constant=0 num_constants=32\n"
                "000069 DrawIndexedInstanced(IndexCountPerInstance:44646, "
                "InstanceCount:1, StartIndexLocation:0, BaseVertexLocation:0, "
                "StartInstanceLocation:0)\n"
                "000069 3DMigoto Dumping Buffer "
                f"000069-vs-cb1=0c8934aa-vs=c280f6945b23a42a.buf -> {per_object}\n",
                encoding="utf-8",
            )

            parser = ZZMILogParser(str(log))

            # 窗口起点按 draw 分开记录（旧实现根本不采这个字段）
            self.assertEqual(parser.get_vs_cb_first_constant("000066", 1), 64)
            self.assertEqual(parser.get_vs_cb_first_constant("000069", 1), 0)
            # 未记录绑定的 draw 缺省 0（= 逐部件 512B dump 语义）
            self.assertEqual(parser.get_vs_cb_first_constant("000070", 1), 0)

            transform_shared = ZZMIBoneMapBuilder.parse_object_transform(
                str(shared),
                first_constant=parser.get_vs_cb_first_constant("000066", 1),
            )
            transform_per_object = ZZMIBoneMapBuilder.parse_object_transform(
                str(per_object),
                first_constant=parser.get_vs_cb_first_constant("000069", 1),
            )
            self.assertEqual(
                transform_shared,
                tuple(float(x) for x in numpy.array(self.TF_SHARED, dtype=numpy.float32)),
            )
            # 旧实现读 float 0 会得到 TF_A —— 断言窗口确实换掉了答案
            self.assertNotEqual(
                transform_shared,
                tuple(float(x) for x in numpy.array(self.TF_A, dtype=numpy.float32)),
            )
            # 同对象空间 → 同一 SkeletonGroup（修好前这里是两组）
            groups = _zzmi.assign_skeleton_groups({
                "cdb2cc7d": transform_shared,
                "2825da1e": transform_per_object,
            })
            self.assertEqual(groups["cdb2cc7d"], groups["2825da1e"])


def _make_zzmi_dump_and_workspace(root: Path):
    """构造最小 ZZMI dump + 工作空间：两个部件共享同一对象变换 CB。

    返回 (dump_dir, workspace_dir, unique_strs)。
    """
    dump = root / "dump"
    (dump / "deduped").mkdir(parents=True, exist_ok=True)
    deduped_abs = (dump / "deduped").resolve()

    # 两个部件各自的 deform pass（palette 各 1 根骨骼）
    parts = [
        ("000001", "11111111", "22222222", "33333333", "pal1.buf"),
        ("000002", "44444444", "55555555", "66666666", "pal2.buf"),
    ]
    for draw_index, vb0, so, t0, pal_file in parts:
        palette = numpy.zeros((1, 12), dtype=numpy.float32)
        palette[0, 0] = 1.0
        (dump / "deduped" / pal_file).write_bytes(palette.tobytes())

    # 共享对象变换 CB（identity，64 字节 <= 512 逐部件块上限）
    cb = numpy.zeros((16,), dtype=numpy.float32)
    cb[[0, 5, 10, 15]] = 1.0
    (dump / "deduped" / "shared_cb1.buf").write_bytes(cb.tobytes())

    lines = []
    for draw_index, vb0, so, t0, pal_file in parts:
        lines += [
            f"{draw_index} IASetVertexBuffers(StartSlot:0, NumBuffers:3,",
            f"0: resource=0x00000000 hash={vb0}",
            f"{draw_index} SOSetTargets(NumBuffers:1,",
            f"0: resource=0x00000000 hash={so}",
            f"{draw_index} VSSetShaderResources(StartSlot:0, NumViews:1,",
            f"0: view=0x00000000 resource=0x00000000 hash={t0}",
            f"{draw_index} Draw(VertexCount:1, StartVertexLocation:0)",
            f"{draw_index} 3DMigoto Dumping Buffer {draw_index}-vs-t0={t0}.buf "
            f"-> {deduped_abs / pal_file}",
        ]
    for render_draw, render_ib in (("000010", "aaaa1111"), ("000020", "bbbb2222")):
        lines += [
            f"{render_draw} IASetIndexBuffer(format:R32_UINT, offset:0) hash={render_ib}",
            f"{render_draw} DrawIndexedInstanced(IndexCountPerInstance:3, InstanceCount:1, "
            "StartIndexLocation:0, BaseVertexLocation:0, StartInstanceLocation:0)",
            f"{render_draw} 3DMigoto Dumping Buffer "
            f"{render_draw}-vs-cb1=77777777-vs=aaaaaaaaaaaaaaaa.buf "
            f"-> {deduped_abs / 'shared_cb1.buf'}",
        ]
    (dump / "log.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    ws = root / "ws"
    (ws / "Config").mkdir(parents=True, exist_ok=True)
    (ws / "Config" / "FrameAnalysisPath.json").write_text(
        json.dumps({"frameAnalysisFolderPath": str(dump)}), encoding="utf-8"
    )
    gametype = "GPU_P12_"
    unique_strs = []
    component_map = {}
    import_map = {}
    for bare, position_hash, render_draw in (
        ("aaaa1111-100-0", "11111111", "000010"),
        ("bbbb2222-200-0", "44444444", "000020"),
    ):
        type_dir = ws / "LOD0" / bare / ("TYPE_" + gametype)
        type_dir.mkdir(parents=True, exist_ok=True)
        (type_dir / f"{bare}.json").write_text(json.dumps({
            "CategoryHash": {"Position": position_hash},
            "CategoryBufferList": [{"D3D11ElementList": [
                {"Category": "Blend", "SemanticName": "BLENDINDICES",
                 "Format": "R32_UINT", "ByteWidth": 4},
            ]}],
        }), encoding="utf-8")
        indices = numpy.array([0], dtype=numpy.uint32)
        (type_dir / f"{bare}-Blend.buf").write_bytes(indices.tobytes())
        component_map[bare] = [render_draw]
        unique_str = f"LOD0.{bare}"
        unique_strs.append(unique_str)
        import_map[unique_str] = gametype
    (ws / "Import.json").write_text(json.dumps(import_map), encoding="utf-8")
    (ws / "LOD0" / "ComponentName_DrawCallIndexList.json").write_text(
        json.dumps(component_map), encoding="utf-8"
    )
    return dump, ws, unique_strs


def _make_zzmi_dump_and_workspace_pair(root: Path):
    """构造最小 ZZMI dump + 工作空间：两个部件共享同一对象变换 CB，各 2 根骨骼。

    与 _make_zzmi_dump_and_workspace 的区别：双方 palette 各 2 根骨骼且逐位全同
    （多骨骼对不加刚性门控）——无排除时两部件跨部件合并到同一槽位；标记
    VGMapDedupExcluded 后重建才能验证「恒等映射独占声明段」确实改变合并结果。
    返回 (dump_dir, workspace_dir, unique_strs)。
    """
    dump = root / "dump"
    (dump / "deduped").mkdir(parents=True, exist_ok=True)
    deduped_abs = (dump / "deduped").resolve()

    palette = numpy.array([
        [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 0.1, 0.2, 0.3],
        [1.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 1.0, 4.0, 5.0, 6.0],
    ], dtype=numpy.float32)

    # 两个部件各自的 deform pass（palette 各 2 根骨骼，逐位全同）
    parts = [
        ("000001", "11111111", "22222222", "33333333", "pal1.buf"),
        ("000002", "44444444", "55555555", "66666666", "pal2.buf"),
    ]
    for draw_index, _vb0, _so, _t0, pal_file in parts:
        (dump / "deduped" / pal_file).write_bytes(palette.tobytes())

    # 共享对象变换 CB（identity，64 字节 <= 512 逐部件块上限）
    cb = numpy.zeros((16,), dtype=numpy.float32)
    cb[[0, 5, 10, 15]] = 1.0
    (dump / "deduped" / "shared_cb1.buf").write_bytes(cb.tobytes())

    lines = []
    for draw_index, vb0, so, t0, pal_file in parts:
        lines += [
            f"{draw_index} IASetVertexBuffers(StartSlot:0, NumBuffers:3,",
            f"0: resource=0x00000000 hash={vb0}",
            f"{draw_index} SOSetTargets(NumBuffers:1,",
            f"0: resource=0x00000000 hash={so}",
            f"{draw_index} VSSetShaderResources(StartSlot:0, NumViews:1,",
            f"0: view=0x00000000 resource=0x00000000 hash={t0}",
            f"{draw_index} Draw(VertexCount:1, StartVertexLocation:0)",
            f"{draw_index} 3DMigoto Dumping Buffer {draw_index}-vs-t0={t0}.buf "
            f"-> {deduped_abs / pal_file}",
        ]
    for render_draw, render_ib in (("000010", "aaaa1111"), ("000020", "bbbb2222")):
        lines += [
            f"{render_draw} IASetIndexBuffer(format:R32_UINT, offset:0) hash={render_ib}",
            f"{render_draw} DrawIndexedInstanced(IndexCountPerInstance:3, InstanceCount:1, "
            "StartIndexLocation:0, BaseVertexLocation:0, StartInstanceLocation:0)",
            f"{render_draw} 3DMigoto Dumping Buffer "
            f"{render_draw}-vs-cb1=77777777-vs=aaaaaaaaaaaaaaaa.buf "
            f"-> {deduped_abs / 'shared_cb1.buf'}",
        ]
    (dump / "log.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

    ws = root / "ws"
    (ws / "Config").mkdir(parents=True, exist_ok=True)
    (ws / "Config" / "FrameAnalysisPath.json").write_text(
        json.dumps({"frameAnalysisFolderPath": str(dump)}), encoding="utf-8"
    )
    gametype = "GPU_P12_"
    unique_strs = []
    component_map = {}
    import_map = {}
    for bare, position_hash, render_draw in (
        ("aaaa1111-100-0", "11111111", "000010"),
        ("bbbb2222-200-0", "44444444", "000020"),
    ):
        type_dir = ws / "LOD0" / bare / ("TYPE_" + gametype)
        type_dir.mkdir(parents=True, exist_ok=True)
        (type_dir / f"{bare}.json").write_text(json.dumps({
            "CategoryHash": {"Position": position_hash},
            "CategoryBufferList": [{"D3D11ElementList": [
                {"Category": "Blend", "SemanticName": "BLENDINDICES",
                 "Format": "R32_UINT", "ByteWidth": 4},
            ]}],
        }), encoding="utf-8")
        # 2 根骨骼：BLENDINDICES 有效通道最大索引 + 1 = 2
        indices = numpy.array([0, 1], dtype=numpy.uint32)
        (type_dir / f"{bare}-Blend.buf").write_bytes(indices.tobytes())
        component_map[bare] = [render_draw]
        unique_str = f"LOD0.{bare}"
        unique_strs.append(unique_str)
        import_map[unique_str] = gametype
    (ws / "Import.json").write_text(json.dumps(import_map), encoding="utf-8")
    (ws / "LOD0" / "ComponentName_DrawCallIndexList.json").write_text(
        json.dumps(component_map), encoding="utf-8"
    )
    return dump, ws, unique_strs


def _read_zzmi_json(ws: Path, bare: str) -> dict:
    for type_dir in (ws / "LOD0" / bare).iterdir():
        if type_dir.is_dir() and type_dir.name.startswith("TYPE_"):
            return json.loads((type_dir / f"{bare}.json").read_text(encoding="utf-8"))
    raise AssertionError(f"未找到 {bare} 的子网格 json")


def _write_zzmi_json(ws: Path, bare: str, payload: dict):
    for type_dir in (ws / "LOD0" / bare).iterdir():
        if type_dir.is_dir() and type_dir.name.startswith("TYPE_"):
            (type_dir / f"{bare}.json").write_text(
                json.dumps(payload), encoding="utf-8"
            )
            return
    raise AssertionError(f"未找到 {bare} 的子网格 json")


class ZZMIPartitionResolverTests(unittest.TestCase):
    def test_zzmi_reuses_partition_aware_json_resolution(self):
        with tempfile.TemporaryDirectory(prefix="zzmi_partition_") as tmp:
            ws = Path(tmp)
            partition = ws / "PartA"
            partition.mkdir(parents=True, exist_ok=True)
            (partition / "Config.json").write_text("{}", encoding="utf-8")
            bare = "aaaa1111-100-0"
            type_dir = partition / "LOD0" / bare / "TYPE_GPU_P12_"
            type_dir.mkdir(parents=True, exist_ok=True)
            json_path = type_dir / f"{bare}.json"
            json_path.write_text("{}", encoding="utf-8")
            (partition / "Import.json").write_text(
                json.dumps({f"LOD0.{bare}": "GPU_P12_"}),
                encoding="utf-8",
            )

            resolved = ZZMISkeletonMergeHelper._resolve_submesh_json_path(
                str(ws), f"LOD0.{bare}"
            )
            self.assertTrue(resolved)
            self.assertTrue(os.path.samefile(resolved, json_path))


class ZZMIWorkspaceCacheOnlyTests(unittest.TestCase):
    """P2 回归：FrameAnalysis 被搬走/删除后，仍能靠工作空间缓存正常重建。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zzmi_cache_only_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        root = Path(self.tmp)
        self.dump, self.ws, self.unique_strs = _make_zzmi_dump_and_workspace(root)

    def test_each_lod_uses_its_work_page_dump_and_log_signature(self):
        lod1_dump = Path(self.tmp) / "lod1-dump"
        shutil.copytree(self.dump, lod1_dump)
        log0 = self.dump / "log.txt"
        log1 = lod1_dump / "log.txt"
        log1.write_text(log1.read_text(encoding="utf-8") + "\n", encoding="utf-8")
        config = self.ws / "Config"
        tabs_dir = config / "Tabs"
        tabs_dir.mkdir()
        tabs = []
        for lod, dump in (("LOD0", self.dump), ("LOD1", lod1_dump)):
            tab_id = f"ws-tab-{lod}"
            tabs.append({"id": tab_id, "name": lod})
            (tabs_dir / f"{tab_id}.json").write_text(
                json.dumps({"frameAnalysisFolderPath": str(dump)}), encoding="utf-8"
            )
        (config / "WorkPageTabs.json").write_text(
            json.dumps({"tabs": tabs}), encoding="utf-8"
        )
        # The workspace default points to the active LOD1 page.
        (config / "FrameAnalysisPath.json").write_text(
            json.dumps({"frameAnalysisFolderPath": str(lod1_dump)}), encoding="utf-8"
        )
        (self.ws / "LOD1").mkdir()
        old_key = self.unique_strs[1]
        lod1_key = old_key.replace("LOD0.", "LOD1.", 1)
        bare = old_key.split(".", 1)[1]
        (self.ws / "LOD0" / bare).rename(self.ws / "LOD1" / bare)
        import_file = self.ws / "Import.json"
        imports = json.loads(import_file.read_text(encoding="utf-8"))
        imports[lod1_key] = imports.pop(old_key)
        import_file.write_text(json.dumps(imports), encoding="utf-8")

        keys = [self.unique_strs[0], lod1_key]
        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(str(self.ws), keys)
        self.assertTrue(ok, message)
        for key, log in zip(keys, (log0, log1)):
            json_path = ZZMISkeletonMergeHelper._resolve_submesh_json_path(str(self.ws), key)
            metadata = json.loads(Path(json_path).read_text(encoding="utf-8"))
            self.assertEqual(
                metadata["FrameAnalysisLogSignature"],
                ZZMISkeletonMergeHelper._frame_analysis_log_signature(str(log)),
            )
            self.assertEqual(metadata["VGOffset"], 0)
            self.assertEqual(metadata["VGMap"], {"0": 0})
        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(str(self.ws), keys)
        self.assertTrue(ok, message)
        self.assertIn("幂等跳过", message)

    def test_unconfigured_workspace_does_not_use_latest_game_dump(self):
        (self.ws / "Config" / "FrameAnalysisPath.json").unlink()
        with mock.patch.object(
            _efmi.EFMISkeletonMergeHelper,
            "resolve_frame_analysis_dir",
            return_value=str(self.dump),
        ):
            self.assertEqual(ZZMISkeletonMergeHelper.resolve_frame_analysis_dir(str(self.ws)), "")

    def test_dump_moved_rebuilds_via_deduped_fallback(self):
        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok, message)
        before = {
            bare: _read_zzmi_json(self.ws, bare)
            for bare in ("aaaa1111-100-0", "bbbb2222-200-0")
        }
        self.assertEqual(before["aaaa1111-100-0"]["SkeletonGroup"],
                         before["bbbb2222-200-0"]["SkeletonGroup"])

        # 把 dump 整个搬到新位置：log 里记录的绝对路径全部失效，
        # 必须靠「当前 dump 目录 deduped/<basename>」候选路径恢复。
        moved = Path(self.tmp) / "moved-dump"
        shutil.move(str(self.dump), str(moved))
        (self.ws / "Config" / "FrameAnalysisPath.json").write_text(
            json.dumps({"frameAnalysisFolderPath": str(moved)}), encoding="utf-8"
        )

        ok2, message2 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs, force=True
        )
        self.assertTrue(ok2, message2)
        after = {
            bare: _read_zzmi_json(self.ws, bare)
            for bare in ("aaaa1111-100-0", "bbbb2222-200-0")
        }
        self.assertEqual(after["aaaa1111-100-0"]["VGMap"], before["aaaa1111-100-0"]["VGMap"])
        self.assertEqual(after["aaaa1111-100-0"]["SkeletonGroup"],
                         after["bbbb2222-200-0"]["SkeletonGroup"])

    def test_same_ib_with_different_object_cb1_is_rejected(self):
        """同一 IB 的多个对象实例不能共用一份合并骨架。"""
        foreign_cb = numpy.zeros((16,), dtype=numpy.float32)
        foreign_cb[[0, 5, 10, 15]] = 1.0
        foreign_cb[12] = 9.0
        (self.dump / "deduped" / "foreign_cb1.buf").write_bytes(foreign_cb.tobytes())
        deduped_abs = (self.dump / "deduped").resolve()
        with (self.dump / "log.txt").open("a", encoding="utf-8") as handle:
            handle.write(
                "000011 IASetIndexBuffer(format:R32_UINT, offset:0) hash=aaaa1111\n"
                "000011 DrawIndexedInstanced(IndexCountPerInstance:3, InstanceCount:1, "
                "StartIndexLocation:0, BaseVertexLocation:0, StartInstanceLocation:0)\n"
                "000011 3DMigoto Dumping Buffer "
                f"000011-vs-cb1=88888888-vs=aaaaaaaaaaaaaaaa.buf -> "
                f"{deduped_abs / 'foreign_cb1.buf'}\n"
            )

        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertFalse(ok)
        rejected = _read_zzmi_json(self.ws, "aaaa1111-100-0")
        accepted = _read_zzmi_json(self.ws, "bbbb2222-200-0")
        self.assertNotIn("VGMapAlgorithmVersion", rejected)
        self.assertEqual(
            accepted.get("VGMapAlgorithmVersion"),
            _zzmi.ZZMI_VG_MAP_ALGORITHM_VERSION,
        )
        self.assertIn("不同对象 CB1 实例", message)

    def test_dump_deleted_rebuilds_from_workspace_cache_only(self):
        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok, message)
        before = {
            bare: _read_zzmi_json(self.ws, bare)
            for bare in ("aaaa1111-100-0", "bbbb2222-200-0")
        }
        self.assertEqual(before["aaaa1111-100-0"]["SkeletonGroup"],
                         before["bbbb2222-200-0"]["SkeletonGroup"])
        # 导入完成后所需的 palette / 对象变换 CB 必须已经复制进工作空间
        for bare in ("aaaa1111-100-0", "bbbb2222-200-0"):
            runtime = self.ws / "LOD0" / bare / "ModImpRuntime"
            self.assertTrue((runtime / f"{bare}-BoneMatrix.buf").is_file())
            self.assertTrue((runtime / f"{bare}-ObjectCB1.buf").is_file())

        # 删除整个 FrameAnalysis dump（用户清理大体积提取文件）
        shutil.rmtree(self.dump)

        ok2, message2 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs, force=True
        )
        self.assertTrue(ok2, message2)
        after = {
            bare: _read_zzmi_json(self.ws, bare)
            for bare in ("aaaa1111-100-0", "bbbb2222-200-0")
        }
        # 骨骼数据、分组、时序元数据与 dump 删除前完全一致
        self.assertEqual(after["aaaa1111-100-0"]["VGMap"], before["aaaa1111-100-0"]["VGMap"])
        self.assertEqual(after["bbbb2222-200-0"]["VGMap"], before["bbbb2222-200-0"]["VGMap"])
        self.assertEqual(after["aaaa1111-100-0"]["SkeletonGroup"],
                         before["aaaa1111-100-0"]["SkeletonGroup"])
        self.assertEqual(after["aaaa1111-100-0"]["SkeletonGroup"],
                         after["bbbb2222-200-0"]["SkeletonGroup"])
        self.assertEqual(after["aaaa1111-100-0"]["DeformDrawIndex"],
                         before["aaaa1111-100-0"]["DeformDrawIndex"])

        # 缓存完整时（不 force）：dump 已被删除也必须能幂等跳过，绝不触碰 dump
        ok3, message3 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok3, message3)
        self.assertIn("幂等跳过", message3)

    def test_deleted_explicit_dump_does_not_fallback_to_latest_other_dump(self):
        """删除已记录 dump 后，ZZMI 不得静默选当前目录的另一帧。"""
        shutil.rmtree(self.dump)
        latest = Path(self.tmp) / "FrameAnalysis-latest"
        latest.mkdir()
        with mock.patch.object(
            _efmi.EFMISkeletonMergeHelper,
            "resolve_frame_analysis_dir",
            return_value=str(latest),
        ):
            self.assertEqual(
                ZZMISkeletonMergeHelper.resolve_frame_analysis_dir(str(self.ws)),
                "",
            )

    def test_legacy_cache_without_log_signature_upgrades_from_workspace_sources(self):
        """源缓存契约完整时，旧工作区不应因缺少后来新增的日志指纹而依赖 dump。"""
        ok1, message1 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok1, message1)

        before = {}
        for bare in ("aaaa1111-100-0", "bbbb2222-200-0"):
            payload = _read_zzmi_json(self.ws, bare)
            before[bare] = payload
            self.assertIs(payload.get("ObjectCB1CacheValid"), True)
            payload["VGMapAlgorithmVersion"] = 1
            payload.pop("FrameAnalysisLogSignature", None)
            _write_zzmi_json(self.ws, bare, payload)

        shutil.rmtree(self.dump)

        ok2, message2 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok2, message2)
        for bare in before:
            rebuilt = _read_zzmi_json(self.ws, bare)
            self.assertEqual(
                rebuilt["VGMapAlgorithmVersion"],
                _zzmi.ZZMI_VG_MAP_ALGORITHM_VERSION,
            )
            self.assertEqual(rebuilt["VGMap"], before[bare]["VGMap"])
            self.assertEqual(rebuilt["SkeletonGroup"], before[bare]["SkeletonGroup"])
            self.assertIs(rebuilt.get("ObjectCB1CacheValid"), True)

    def test_clear_vgmap_then_delete_dump_rebuilds_from_workspace_cache(self):
        """用户先清理 VGMap、再删除 dump 后，ZZMI 仍应只靠工作空间缓存重建。"""
        ok1, message1 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok1, message1)
        before = {
            bare: _read_zzmi_json(self.ws, bare)
            for bare in ("aaaa1111-100-0", "bbbb2222-200-0")
        }

        cleaned, _scanned = _efmi.EFMISkeletonMergeHelper.clear_vgmap_cache(
            str(self.ws)
        )
        self.assertEqual(cleaned, 2)
        shutil.rmtree(self.dump)

        ok2, message2 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok2, message2)
        after = {
            bare: _read_zzmi_json(self.ws, bare)
            for bare in ("aaaa1111-100-0", "bbbb2222-200-0")
        }
        self.assertEqual(after["aaaa1111-100-0"]["VGMap"],
                         before["aaaa1111-100-0"]["VGMap"])
        self.assertEqual(after["bbbb2222-200-0"]["VGMap"],
                         before["bbbb2222-200-0"]["VGMap"])
        self.assertEqual(after["aaaa1111-100-0"]["SkeletonGroup"],
                         after["bbbb2222-200-0"]["SkeletonGroup"])

    def test_cache_only_rebuild_uses_same_valid_cb1_as_dump_rebuild(self):
        """多个 CB1 候选时，缓存必须复制实际用于分组的有效文件，而非首个坏文件。"""
        invalid_cb1 = self.dump / "deduped" / "invalid_cb1.buf"
        invalid_cb1.write_bytes(numpy.zeros((16,), dtype=numpy.float32).tobytes())
        deduped_abs = (self.dump / "deduped").resolve()
        with (self.dump / "log.txt").open("a", encoding="utf-8") as handle:
            handle.write(
                "000009 IASetIndexBuffer(format:R32_UINT, offset:0) hash=aaaa1111\n"
            )
            handle.write(
                "000009 DrawIndexedInstanced(IndexCountPerInstance:3, InstanceCount:1, "
                "StartIndexLocation:0, BaseVertexLocation:0, StartInstanceLocation:0)\n"
            )
            handle.write(
                "000009 3DMigoto Dumping Buffer "
                "000009-vs-cb1=99999999-vs=aaaaaaaaaaaaaaaa.buf "
                f"-> {deduped_abs / 'invalid_cb1.buf'}\n"
            )
        mapping_path = self.ws / "LOD0" / "ComponentName_DrawCallIndexList.json"
        mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        mapping["aaaa1111-100-0"] = ["000009", "000010"]
        mapping_path.write_text(json.dumps(mapping), encoding="utf-8")

        ok1, message1 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok1, message1)
        before = {
            bare: _read_zzmi_json(self.ws, bare)
            for bare in ("aaaa1111-100-0", "bbbb2222-200-0")
        }
        self.assertEqual(before["aaaa1111-100-0"]["SkeletonGroup"],
                         before["bbbb2222-200-0"]["SkeletonGroup"])
        cached_cb1 = (
            self.ws / "LOD0" / "aaaa1111-100-0" / "ModImpRuntime"
            / "aaaa1111-100-0-ObjectCB1.buf"
        )
        self.assertEqual(
            cached_cb1.read_bytes(),
            (self.dump / "deduped" / "shared_cb1.buf").read_bytes(),
            "必须复制实际参与 SkeletonGroup 计算的有效 CB1",
        )

        # 模拟旧版工作空间：缓存文件被错误写成首个无效候选，且没有新版
        # ObjectCB1CacheValid 标记。dump 仍在时，普通幂等导入必须自动迁移。
        for bare in before:
            runtime_cb1 = (
                self.ws / "LOD0" / bare / "ModImpRuntime"
                / f"{bare}-ObjectCB1.buf"
            )
            runtime_cb1.write_bytes(invalid_cb1.read_bytes())
            payload = _read_zzmi_json(self.ws, bare)
            payload.pop("ObjectCB1CacheValid", None)
            _write_zzmi_json(self.ws, bare, payload)

        ok_migrate, message_migrate = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok_migrate, message_migrate)
        self.assertNotIn("幂等跳过", message_migrate)
        for bare in before:
            payload = _read_zzmi_json(self.ws, bare)
            self.assertIs(payload.get("ObjectCB1CacheValid"), True)
            runtime_cb1 = (
                self.ws / "LOD0" / bare / "ModImpRuntime"
                / f"{bare}-ObjectCB1.buf"
            )
            self.assertEqual(
                runtime_cb1.read_bytes(),
                (self.dump / "deduped" / "shared_cb1.buf").read_bytes(),
            )

        _efmi.EFMISkeletonMergeHelper.clear_vgmap_cache(str(self.ws))
        shutil.rmtree(self.dump)
        ok2, message2 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok2, message2)
        after = {
            bare: _read_zzmi_json(self.ws, bare)
            for bare in ("aaaa1111-100-0", "bbbb2222-200-0")
        }
        for bare in after:
            self.assertEqual(after[bare]["VGMap"], before[bare]["VGMap"], bare)
            self.assertEqual(after[bare]["VGOffset"], before[bare]["VGOffset"], bare)
            self.assertEqual(after[bare]["SkeletonGroup"],
                             before[bare]["SkeletonGroup"], bare)

    def test_missing_required_cb1_cache_fails_without_silent_regroup(self):
        """标记为有效的 CB1 缓存丢失后，无 dump 重建必须失败且不得改写分组。"""
        ok1, message1 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok1, message1)
        before = {
            bare: _read_zzmi_json(self.ws, bare)
            for bare in ("aaaa1111-100-0", "bbbb2222-200-0")
        }
        self.assertTrue(all(
            payload.get("ObjectCB1CacheValid") is True
            for payload in before.values()
        ))

        _efmi.EFMISkeletonMergeHelper.clear_vgmap_cache(str(self.ws))
        shutil.rmtree(self.dump)
        for bare in before:
            cb1_path = (
                self.ws / "LOD0" / bare / "ModImpRuntime"
                / f"{bare}-ObjectCB1.buf"
            )
            cb1_path.unlink()

        ok2, message2 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertFalse(ok2)
        self.assertIn("ObjectCB1", message2)
        for bare in before:
            payload = _read_zzmi_json(self.ws, bare)
            self.assertIs(payload.get("ObjectCB1CacheValid"), True)
            self.assertNotIn("VGMap", payload)
            self.assertNotIn("SkeletonGroup", payload)

    def test_missing_required_cb1_with_log_but_no_source_fails(self):
        """日志目录仍在但 CB1 原文件被删时，也不得静默改成独立分组。"""
        ok1, message1 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok1, message1)
        before = {
            bare: _read_zzmi_json(self.ws, bare)
            for bare in ("aaaa1111-100-0", "bbbb2222-200-0")
        }
        self.assertTrue(all(
            payload.get("ObjectCB1CacheValid") is True
            for payload in before.values()
        ))
        for bare in before:
            cb1_path = (
                self.ws / "LOD0" / bare / "ModImpRuntime"
                / f"{bare}-ObjectCB1.buf"
            )
            cb1_path.unlink()
        # 保留 log.txt 让 parser 仍可创建，但删除其实际 CB1 来源。
        (self.dump / "deduped" / "shared_cb1.buf").unlink()
        _efmi.EFMISkeletonMergeHelper.clear_vgmap_cache(str(self.ws))

        ok2, message2 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs, force=True
        )
        self.assertFalse(ok2)
        self.assertIn("ObjectCB1", message2)
        for bare in before:
            payload = _read_zzmi_json(self.ws, bare)
            self.assertIs(payload.get("ObjectCB1CacheValid"), True)
            self.assertNotIn("SkeletonGroup", payload)

    def test_invalid_fast_path_metadata_is_rebuilt(self):
        """分组/时序元数据必须是非负整数，不能仅凭字段存在进入幂等快路径。"""
        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok, message)
        bare = "aaaa1111-100-0"
        invalid_cases = (
            ("SkeletonGroup", -1),
            ("DeformDrawIndex", "bad"),
            ("OriginalVertexCount", -1),
            ("SkeletonGroup", 1.5),
        )
        for field, invalid_value in invalid_cases:
            with self.subTest(field=field):
                payload = _read_zzmi_json(self.ws, bare)
                payload[field] = invalid_value
                _write_zzmi_json(self.ws, bare, payload)

                ok2, message2 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
                    workspace_root=str(self.ws), unique_str_list=self.unique_strs
                )
                self.assertTrue(ok2, message2)
                self.assertNotIn("幂等跳过", message2)
                repaired = _read_zzmi_json(self.ws, bare)
                self.assertIsInstance(repaired[field], int)
                self.assertGreaterEqual(repaired[field], 0)

    def test_invalid_vgmap_schema_is_rebuilt_before_import(self):
        """规范化重复键、非整数值和 uint32 越界槽位都不能通过缓存快路径。"""
        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok, message)
        bare = "aaaa1111-100-0"
        invalid_maps = (
            {"0": 0, "00": 1},
            {"0": 1.5},
            {"0": 0x100000000},
        )
        for invalid_map in invalid_maps:
            with self.subTest(vg_map=invalid_map):
                payload = _read_zzmi_json(self.ws, bare)
                payload["VGMap"] = invalid_map
                _write_zzmi_json(self.ws, bare, payload)

                ok2, message2 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
                    workspace_root=str(self.ws), unique_str_list=self.unique_strs
                )
                self.assertTrue(ok2, message2)
                self.assertNotIn("幂等跳过", message2)
                repaired = _read_zzmi_json(self.ws, bare)
                self.assertEqual(repaired["VGMap"], {"0": 0})

    def test_cb1_stage_failure_does_not_publish_palette_or_json(self):
        """CB1 是第二份来源；其复制失败时 palette/JSON 也不能提前提交。"""
        real_copy2 = _efmi.shutil.copy2

        def fail_cb1(source, destination, *args, **kwargs):
            if str(source).endswith("shared_cb1.buf"):
                raise OSError("cb1 copy failed")
            return real_copy2(source, destination, *args, **kwargs)

        with mock.patch.object(_efmi.shutil, "copy2", side_effect=fail_cb1):
            ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
                workspace_root=str(self.ws), unique_str_list=self.unique_strs
            )

        self.assertFalse(ok)
        self.assertIn("未生成", message)
        for bare in ("aaaa1111-100-0", "bbbb2222-200-0"):
            runtime = self.ws / "LOD0" / bare / "ModImpRuntime"
            self.assertFalse((runtime / f"{bare}-BoneMatrix.buf").exists())
            self.assertFalse((runtime / f"{bare}-ObjectCB1.buf").exists())
            payload = _read_zzmi_json(self.ws, bare)
            self.assertNotIn("VGMap", payload)
            self.assertNotIn("BoneMatrixFileName", payload)

    def test_cache_incomplete_submesh_not_skipped(self):
        """P2#6 回归：复制骨骼缓存失败/漏掉 ModImpRuntime 时不得幂等跳过。"""
        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok, message)

        # 模拟工作空间搬迁漏掉 ModImpRuntime（骨骼缓存文件丢失）
        shutil.rmtree(self.ws / "LOD0" / "aaaa1111-100-0" / "ModImpRuntime")

        # 快路径必须发现缓存产物不完整并整批重建（而不是“幂等跳过”）
        ok2, message2 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok2, message2)
        self.assertIn("个子网格生成", message2)
        runtime = self.ws / "LOD0" / "aaaa1111-100-0" / "ModImpRuntime"
        self.assertTrue((runtime / "aaaa1111-100-0-BoneMatrix.buf").is_file())

    def test_missing_vgcount_vgoffset_rebuilds(self):
        """快路径必须校验 VGCount/VGOffset：缺失时整批重建，不能幂等跳过。"""
        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok, message)

        bare = "aaaa1111-100-0"
        payload = _read_zzmi_json(self.ws, bare)
        payload.pop("VGCount", None)
        payload.pop("VGOffset", None)
        _write_zzmi_json(self.ws, bare, payload)

        ok2, message2 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok2, message2)
        self.assertIn("个子网格生成", message2)
        rebuilt = _read_zzmi_json(self.ws, bare)
        self.assertEqual(rebuilt.get("VGCount"), 1)
        self.assertGreaterEqual(rebuilt.get("VGOffset", -1), 0)

    def test_vgmap_missing_key_rebuilds(self):
        """VGMap 键必须完整覆盖 0..VGCount-1：缺键时整批重建。"""
        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok, message)

        bare = "aaaa1111-100-0"
        payload = _read_zzmi_json(self.ws, bare)
        del payload["VGMap"]["0"]
        _write_zzmi_json(self.ws, bare, payload)

        ok2, message2 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok2, message2)
        self.assertIn("个子网格生成", message2)
        rebuilt = _read_zzmi_json(self.ws, bare)
        self.assertEqual(set(rebuilt["VGMap"].keys()), {"0"})

    def test_truncated_bone_matrix_file_rebuilt(self):
        """BoneMatrix 文件存在但被截断（损坏）：快路径判失效并重建恢复。"""
        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok, message)

        bare = "aaaa1111-100-0"
        cache_path = self.ws / "LOD0" / bare / "ModImpRuntime" / f"{bare}-BoneMatrix.buf"
        cache_path.write_bytes(b"\x00" * 10)  # 存在但小于 VGCount*48

        ok2, message2 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok2, message2)
        self.assertIn("个子网格生成", message2)
        self.assertGreaterEqual(cache_path.stat().st_size, 48)


class ZZMIDedupExcludedTests(unittest.TestCase):
    """组件级排除去重（VGMapDedupExcluded）端到端：ensure_skeleton_data 收集 json
    标记 -> 去重阶段恒等映射独占声明段；快路径命中时排除不生效（对齐 EFMI 时效语义）。"""

    def test_dedup_excluded_rebuild_only_identity_slots(self):
        with tempfile.TemporaryDirectory(prefix="zzmi_dedup_excl_") as tmp:
            root = Path(tmp)
            _dump, ws, unique_strs = _make_zzmi_dump_and_workspace_pair(root)

            # 基线：无排除时两部件 2 根 bitwise 全同骨骼合并到同一槽位
            ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
                workspace_root=str(ws), unique_str_list=unique_strs
            )
            self.assertTrue(ok, message)
            base = {
                bare: _read_zzmi_json(ws, bare)
                for bare in ("aaaa1111-100-0", "bbbb2222-200-0")
            }
            self.assertEqual(
                base["aaaa1111-100-0"]["VGMap"]["0"],
                base["bbbb2222-200-0"]["VGMap"]["0"],
            )
            self.assertEqual(
                base["aaaa1111-100-0"]["VGMap"]["1"],
                base["bbbb2222-200-0"]["VGMap"]["1"],
            )

            # 标记排除后不 force：幂等快路径命中，排除不生效（EFMI 同口径：
            # 缓存完整性校验不看 VGMapDedupExcluded，重建才重新计算）
            payload = _read_zzmi_json(ws, "bbbb2222-200-0")
            payload["VGMapDedupExcluded"] = True
            _write_zzmi_json(ws, "bbbb2222-200-0", payload)
            ok_skip, message_skip = ZZMISkeletonMergeHelper.ensure_skeleton_data(
                workspace_root=str(ws), unique_str_list=unique_strs
            )
            self.assertTrue(ok_skip, message_skip)
            self.assertIn("幂等跳过", message_skip)
            skipped = _read_zzmi_json(ws, "bbbb2222-200-0")
            self.assertEqual(skipped["VGMap"], base["bbbb2222-200-0"]["VGMap"])

            # force 重建：排除生效——恒等映射独占声明段，不再与 aaaa1111 合并
            ok_force, message_force = ZZMISkeletonMergeHelper.ensure_skeleton_data(
                workspace_root=str(ws), unique_str_list=unique_strs, force=True
            )
            self.assertTrue(ok_force, message_force)
            after = {
                bare: _read_zzmi_json(ws, bare)
                for bare in ("aaaa1111-100-0", "bbbb2222-200-0")
            }
            self.assertEqual(
                after["bbbb2222-200-0"]["VGMap"]["0"],
                after["bbbb2222-200-0"]["VGOffset"] + 0,
            )
            self.assertEqual(
                after["bbbb2222-200-0"]["VGMap"]["1"],
                after["bbbb2222-200-0"]["VGOffset"] + 1,
            )
            self.assertNotEqual(
                after["bbbb2222-200-0"]["VGMap"]["0"],
                after["aaaa1111-100-0"]["VGMap"]["0"],
            )
            # 排除不改变其它部件的 offset 布局（组内 offset 口径 + 组基址拼接）
            self.assertEqual(
                after["aaaa1111-100-0"]["VGOffset"],
                base["aaaa1111-100-0"]["VGOffset"],
            )
            self.assertEqual(
                after["bbbb2222-200-0"]["VGOffset"],
                base["bbbb2222-200-0"]["VGOffset"],
            )
            # json 标记键原样保留（LoadFromFile -> 修改 -> 保存）
            self.assertTrue(
                _read_zzmi_json(ws, "bbbb2222-200-0").get("VGMapDedupExcluded")
            )


class ZZMISiblingCacheTests(unittest.TestCase):
    """P2 回归：dump 删除后同 DrawIB 代表子网格缓存缺失时，回退兄弟子网格缓存；
    存在未处理目标时不得报告完整成功。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zzmi_sibling_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        root = Path(self.tmp)
        self.dump = root / "dump"
        (self.dump / "deduped").mkdir(parents=True, exist_ok=True)
        deduped_abs = (self.dump / "deduped").resolve()

        parts = [
            ("000001", "11111111", "22222222", "33333333", "pal1.buf"),
            ("000002", "44444444", "55555555", "66666666", "pal2.buf"),
        ]
        for draw_index, vb0, so, t0, pal_file in parts:
            palette = numpy.zeros((1, 12), dtype=numpy.float32)
            palette[0, 0] = 1.0
            (self.dump / "deduped" / pal_file).write_bytes(palette.tobytes())
        cb = numpy.zeros((16,), dtype=numpy.float32)
        cb[[0, 5, 10, 15]] = 1.0
        (self.dump / "deduped" / "shared_cb1.buf").write_bytes(cb.tobytes())
        foreign_cb = cb.copy()
        foreign_cb[12] = 9.0  # 另一 IB 的对象空间，不能污染 aaaa1111
        (self.dump / "deduped" / "foreign_cb1.buf").write_bytes(foreign_cb.tobytes())

        lines = []
        for draw_index, vb0, so, t0, pal_file in parts:
            lines += [
                f"{draw_index} IASetVertexBuffers(StartSlot:0, NumBuffers:3,",
                f"0: resource=0x00000000 hash={vb0}",
                f"{draw_index} SOSetTargets(NumBuffers:1,",
                f"0: resource=0x00000000 hash={so}",
                f"{draw_index} VSSetShaderResources(StartSlot:0, NumViews:1,",
                f"0: view=0x00000000 resource=0x00000000 hash={t0}",
                f"{draw_index} Draw(VertexCount:1, StartVertexLocation:0)",
                f"{draw_index} 3DMigoto Dumping Buffer {draw_index}-vs-t0={t0}.buf "
                f"-> {deduped_abs / pal_file}",
            ]
        lines += [
            "000000 IASetIndexBuffer(format:R32_UINT, offset:0) hash=foreignmodel",
            "000000 DrawIndexedInstanced(IndexCountPerInstance:3, InstanceCount:1, "
            "StartIndexLocation:0, BaseVertexLocation:0, StartInstanceLocation:0)",
            f"000000 3DMigoto Dumping Buffer 000000-vs-cb1=66666666-vs=aaaaaaaaaaaaaaaa.buf "
            f"-> {deduped_abs / 'foreign_cb1.buf'}",
        ]
        for render_draw, render_ib in (("000010", "aaaa1111"), ("000020", "bbbb2222")):
            lines += [
                f"{render_draw} IASetIndexBuffer(format:R32_UINT, offset:0) hash={render_ib}",
                f"{render_draw} DrawIndexedInstanced(IndexCountPerInstance:3, InstanceCount:1, "
                "StartIndexLocation:0, BaseVertexLocation:0, StartInstanceLocation:0)",
                f"{render_draw} 3DMigoto Dumping Buffer "
                f"{render_draw}-vs-cb1=77777777-vs=aaaaaaaaaaaaaaaa.buf "
                f"-> {deduped_abs / 'shared_cb1.buf'}",
            ]
        (self.dump / "log.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")

        self.ws = root / "ws"
        (self.ws / "Config").mkdir(parents=True, exist_ok=True)
        (self.ws / "Config" / "FrameAnalysisPath.json").write_text(
            json.dumps({"frameAnalysisFolderPath": str(self.dump)}), encoding="utf-8"
        )
        gametype = "GPU_P12_"
        component_map = {}
        import_map = {}
        # aaaa1111 的两个拆分子网格共享同一 deform pass（同 DrawIB）；
        # bbbb2222 是另一个独立部件。
        self.bares = ("aaaa1111-100-0", "aaaa1111-200-0", "bbbb2222-200-0")
        for bare in self.bares:
            type_dir = self.ws / "LOD0" / bare / ("TYPE_" + gametype)
            type_dir.mkdir(parents=True, exist_ok=True)
            position_hash = "11111111" if bare.startswith("aaaa1111") else "44444444"
            (type_dir / f"{bare}.json").write_text(json.dumps({
                "CategoryHash": {"Position": position_hash},
                "CategoryBufferList": [{"D3D11ElementList": [
                    {"Category": "Blend", "SemanticName": "BLENDINDICES",
                     "Format": "R32_UINT", "ByteWidth": 4},
                ]}],
            }), encoding="utf-8")
            (type_dir / f"{bare}-Blend.buf").write_bytes(
                numpy.array([0], dtype=numpy.uint32).tobytes()
            )
            render_draw = "000010" if bare.startswith("aaaa1111") else "000020"
            component_map[bare] = (["000000", render_draw] if bare.startswith("aaaa1111") else [render_draw])
            import_map[f"LOD0.{bare}"] = gametype
        (self.ws / "Import.json").write_text(json.dumps(import_map), encoding="utf-8")
        (self.ws / "LOD0" / "ComponentName_DrawCallIndexList.json").write_text(
            json.dumps(component_map), encoding="utf-8"
        )
        # 代表子网格 = 组内第一个成员（aaaa1111-100-0）
        self.unique_strs = [f"LOD0.{bare}" for bare in self.bares]

    def _runtime_dir(self, bare):
        return self.ws / "LOD0" / bare / "ModImpRuntime"

    def test_representative_cache_missing_recovers_from_sibling(self):
        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok, message)

        # 删除 dump + 只删除代表子网格（aaaa1111-100-0）的 ModImpRuntime
        shutil.rmtree(self.dump)
        shutil.rmtree(self._runtime_dir("aaaa1111-100-0"))

        ok2, message2 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs, force=True
        )
        self.assertTrue(ok2, message2)

        # 代表子网格的缓存已从兄弟子网格（aaaa1111-200-0）恢复
        restored = self._runtime_dir("aaaa1111-100-0") / "aaaa1111-100-0-BoneMatrix.buf"
        self.assertTrue(restored.is_file(), "代表缓存缺失时应回退兄弟子网格缓存")
        # 同 DrawIB 拆分子网格的 VGMap / 分组一致
        json_rep = _read_zzmi_json(self.ws, "aaaa1111-100-0")
        json_sib = _read_zzmi_json(self.ws, "aaaa1111-200-0")
        self.assertEqual(json_rep["VGMap"], json_sib["VGMap"])
        self.assertEqual(json_rep["SkeletonGroup"], json_sib["SkeletonGroup"])
        self.assertEqual(json_rep["VGCount"], 1)

    def test_partial_completion_reports_incomplete(self):
        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok, message)

        # 删除 dump + 删除 aaaa1111 全部成员的缓存（bbbb2222 缓存仍在）
        shutil.rmtree(self.dump)
        shutil.rmtree(self._runtime_dir("aaaa1111-100-0"))
        shutil.rmtree(self._runtime_dir("aaaa1111-200-0"))

        ok2, message2 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs, force=True
        )
        # 只要存在未处理目标，就不能报告完整成功
        self.assertFalse(ok2)
        self.assertIn("未生成", message2)
        self.assertIn("aaaa1111", message2)
        # 独立部件 bbbb2222 已生成、代表子网格的缓存依然缺失（诚实暴露）
        self.assertEqual(_read_zzmi_json(self.ws, "bbbb2222-200-0").get("VGCount"), 1)
        missing = self._runtime_dir("aaaa1111-100-0") / "aaaa1111-100-0-BoneMatrix.buf"
        self.assertFalse(missing.is_file())

    def test_fast_path_does_not_ignore_unresolved_targets(self):
        """P2 回归：幂等快路径必须把无法解析的目标计入失败，不能提前返回成功。"""
        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok, message)

        # 删除一个目标目录（其余两个缓存完整），仍用原目标列表调用（不 force）
        shutil.rmtree(self.ws / "LOD0" / "aaaa1111-200-0")

        ok2, message2 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertFalse(ok2, "快路径不得忽略无法解析的目标")
        self.assertIn("无法解析", message2)
        self.assertIn("LOD0.aaaa1111-200-0", message2)
        # 未受影响的目标缓存完好
        self.assertEqual(_read_zzmi_json(self.ws, "bbbb2222-200-0").get("VGCount"), 1)
        self.assertEqual(_read_zzmi_json(self.ws, "aaaa1111-100-0").get("VGCount"), 1)

    def test_sibling_blend_buffers_are_aggregated(self):
        """同 DrawIB 拆分 Component 可以使用不同局部骨骼，不能只看代表成员。"""
        palette = numpy.zeros((3, 12), dtype=numpy.float32)
        palette[:, 0] = 1.0
        (self.dump / "deduped" / "pal1.buf").write_bytes(palette.tobytes())
        sibling_blend = (
            self.ws / "LOD0" / "aaaa1111-200-0" / "TYPE_GPU_P12_"
            / "aaaa1111-200-0-Blend.buf"
        )
        sibling_blend.write_bytes(numpy.array([2], dtype=numpy.uint32).tobytes())

        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok, message)
        sibling = _read_zzmi_json(self.ws, "aaaa1111-200-0")
        self.assertEqual(sibling["VGCount"], 3)
        self.assertEqual(set(sibling["VGMap"]), {"0", "1", "2"})
        self.assertEqual(sibling["OriginalVertexCount"], 1)

    def test_cache_copy_failure_is_not_reported_as_success(self):
        with mock.patch.object(_efmi.shutil, "copy2", side_effect=OSError("disk full")):
            ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
                workspace_root=str(self.ws), unique_str_list=self.unique_strs
            )

        self.assertFalse(ok)
        self.assertIn("未生成", message)
        for bare in self.bares:
            self.assertFalse(
                (self._runtime_dir(bare) / f"{bare}-BoneMatrix.buf").exists()
            )

    def test_rebuild_refreshes_same_size_palette_and_cb1_cache(self):
        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok, message)

        source_palette = self.dump / "deduped" / "pal1.buf"
        changed_palette = bytearray(source_palette.read_bytes())
        changed_palette[0:4] = numpy.float32(123.0).tobytes()
        source_palette.write_bytes(changed_palette)
        source_cb1 = self.dump / "deduped" / "shared_cb1.buf"
        changed_cb1 = bytearray(source_cb1.read_bytes())
        changed_cb1[0:4] = numpy.float32(456.0).tobytes()
        source_cb1.write_bytes(changed_cb1)

        cache_a = self._runtime_dir("aaaa1111-100-0") / "aaaa1111-100-0-BoneMatrix.buf"
        cb1_a = self._runtime_dir("aaaa1111-100-0") / "aaaa1111-100-0-ObjectCB1.buf"
        (self._runtime_dir("bbbb2222-200-0") / "bbbb2222-200-0-BoneMatrix.buf").unlink()

        ok2, message2 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok2, message2)
        self.assertEqual(cache_a.read_bytes(), source_palette.read_bytes())
        self.assertEqual(cb1_a.read_bytes(), source_cb1.read_bytes())

        dump_groups = {
            bare: _read_zzmi_json(self.ws, bare)["SkeletonGroup"]
            for bare in self.bares
        }
        for bare in self.bares:
            payload = _read_zzmi_json(self.ws, bare)
            self.assertIs(payload.get("ObjectCB1CacheValid"), False)

        # 当前 dump 的 CB1 已被判定无效。即使保留了原始副本用于诊断，
        # 清掉 VGMap 并删除 dump 后也不得把该文件重新当作有效对象变换。
        _efmi.EFMISkeletonMergeHelper.clear_vgmap_cache(str(self.ws))
        shutil.rmtree(self.dump)
        ok3, message3 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok3, message3)
        cache_groups = {
            bare: _read_zzmi_json(self.ws, bare)["SkeletonGroup"]
            for bare in self.bares
        }
        self.assertEqual(cache_groups, dump_groups)


class ZZMIMergedMetadataGateTests(unittest.TestCase):
    """生成侧与开关无关（用户要求）+ 自愈的判据单测。

    导入侧门控（ui/ui_func_import_ssmt.py）为
    ``zzmi_merged_consumption or zzmi_merged_metadata_missing``——即复选框关闭
    时，只要请求集内还有子网格缺合并元数据（或数据不完整），本次导入也必须
    修复性反查/升级落盘。本类钉住这一半判据（`missing_merged_metadata_exist`）：
    首次导入/清缓存后/旧版缓存必须为 True，数据完整后必须为 False（幂等，开关
    关闭时不会重复解析提取文件）。
    """

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="zzmi_meta_gate_")
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.dump, self.ws, self.unique_strs = _make_zzmi_dump_and_workspace(
            Path(self.tmp)
        )

    def _missing(self) -> bool:
        return ZZMISkeletonMergeHelper.missing_merged_metadata_exist(
            str(self.ws), self.unique_strs
        )

    def test_missing_before_generation_and_present_after(self):
        # 首次导入（新工作空间）：没有任何 VGMap ⇒ 必须触发预生成
        self.assertTrue(self._missing())
        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok, message)
        # 数据完整后幂等：复选框关闭时不会因为本判据重复反查提取文件
        self.assertFalse(self._missing())

    def test_clear_cache_makes_metadata_missing_again(self):
        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok, message)
        self.assertFalse(self._missing())

        # 用户执行「清除骨骼合并VGMap缓存」：即使复选框关闭，下次导入也必须
        # 重新反查（有新提取文件用新的，没有则用工作空间缓存）。
        _efmi.EFMISkeletonMergeHelper.clear_vgmap_cache(str(self.ws))
        self.assertTrue(self._missing())

        shutil.rmtree(self.dump)
        ok2, message2 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok2, message2)
        self.assertFalse(self._missing())
        for bare in ("aaaa1111-100-0", "bbbb2222-200-0"):
            runtime = self.ws / "LOD0" / bare / "ModImpRuntime"
            self.assertTrue((runtime / f"{bare}-BoneMatrix.buf").is_file())
            self.assertTrue((runtime / f"{bare}-ObjectCB1.buf").is_file())

    def test_incomplete_cache_is_self_healed_without_dump(self):
        """旧算法版本的「存在但不完整」数据也必须自愈，且不需要提取文件。"""
        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok, message)
        self.assertFalse(self._missing())

        # 把缓存降级成 v4（缺 ChannelPlan 系列字段）：快路径完整性不再成立
        for bare in ("aaaa1111-100-0", "bbbb2222-200-0"):
            payload = _read_zzmi_json(self.ws, bare)
            payload["VGMapAlgorithmVersion"] = 4
            for key in ("ChannelPlan", "ChannelPlanVersion", "ChannelPlanSlotWeights"):
                payload.pop(key, None)
            _write_zzmi_json(self.ws, bare, payload)
        self.assertTrue(
            self._missing(),
            "旧算法版本的缓存必须被判为需要处理（复选框关闭时也要自愈）",
        )

        # 提取文件已删除：v4→v5 只凭 VGMap 就地升级，不得要求重新提取
        shutil.rmtree(self.dump)
        ok2, message2 = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok2, message2)
        # 就地升级（v4→v5 只补 ChannelPlan，不需要提取文件）：两份 json 必须
        # 已经带上当前算法版本与通道计划记录
        for bare in ("aaaa1111-100-0", "bbbb2222-200-0"):
            payload = _read_zzmi_json(self.ws, bare)
            self.assertEqual(
                payload["VGMapAlgorithmVersion"],
                _zzmi.ZZMI_VG_MAP_ALGORITHM_VERSION,
            )
            self.assertIsInstance(payload.get("ChannelPlan"), dict)
        self.assertFalse(self._missing())

    def test_cross_toggle_rejects_group_numbered_cache(self):
        """开关开着时，历史分组编号的缓存不算「已合并」。

        两种编号域的 VGMap 长得一样（VGMap/VGCount/Hash 字段全在），只看
        「有没有 VGMap」会把历史编号当成合并成果跳过重建：导入侧再按跨组池
        消费，每个部件就落在自己的局部空间里（现场：带脸部件一起导入后
        「和没开跨组合并一样」）。口径由 ``SkeletonMergeMode`` 自证。
        """
        ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
            workspace_root=str(self.ws), unique_str_list=self.unique_strs
        )
        self.assertTrue(ok, message)
        # 这套合成提取源没有锚点骨可锚 ⇒ 本次写的是历史分组编号
        unique_str, json_path, payload = _list_submesh_jsons(str(self.ws))[0]
        self.assertEqual(payload.get("SkeletonMergeMode"), "group")
        self.assertTrue(
            ZZMISkeletonMergeHelper._zzmi_cache_intact(payload, json_path, unique_str)
        )
        self.assertFalse(
            ZZMISkeletonMergeHelper._zzmi_cache_intact(
                payload, json_path, unique_str, expect_cross=True
            ),
            "开着跨组合并时，历史分组编号必须判成需要重建",
        )
        payload["SkeletonMergeMode"] = "cross_group"
        self.assertTrue(
            ZZMISkeletonMergeHelper._zzmi_cache_intact(
                payload, json_path, unique_str, expect_cross=True
            )
        )


if __name__ == "__main__":
    unittest.main()
