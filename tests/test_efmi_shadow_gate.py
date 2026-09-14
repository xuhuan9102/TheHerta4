# -*- coding: utf-8 -*-
"""EFMI 阴影 pass 单投射源门控的回归测试（实机定位 2026-09-13；ps 口径 + 抓帧推导）。

被测对象：
- ui/universal/efmi_shadow_gate.py（纯判定/纯扫描，无 bpy 依赖）；
- ui/universal/efmi.py 的发射侧（源码级回归：键归一化、handling=skip 在门控外、
  ShaderOverride 注册段用抓帧推导的 ps 全集、以及 vs 口径不得回退）；
- common/m_ini_builder.py 的 ShaderOverride 段类型真的会被写出（不然注册段静默丢失）。

断言要点：
  1. **实机真实数据**（艾尔黛拉 2026-09-13 最新导出，16 部件 = 8 组 LOD 对）：
     5 个 LOD0 部件必须拿到门控；
  2. 回归守卫：按原样 obj_name 配对时**一条都发不出来**（98d9856 的实际失效原因）——
     键必须走 efmi_normalize_obj_name（剥 SSMT 前缀 + 归一 _chain<N>）；
  3. **pass ps 必须由抓帧推导**：无 RT（NumViews==0）且绘制了本模组部件的 ps 全都要
     注册（实机 08-31 抓帧实测有 3 个），只注册常量那一个会漏掉其余深度/阴影 pass；
     同一 ps 若也用于有 RT 的 pass 则剔除（不敢门控）；
  4. 阶段标签 = **私有号段 99001**（实机事故回归守卫：v4.4.45 首版曾借用 RabbitFX 的
     1718.2，结果它的可见 pass 标签把本门控关掉 → 可见外壳消失、只剩背面；门控行 =
     `if ps != 99001`，且标签不得落在 1718.x 这类别家号段上）；
  5. ShaderOverride 注册段内容（hash / filter_index / allow_duplicate_hash）。
"""
import importlib.util
import os
import sys
import tempfile
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(REPO_ROOT, "ui", "universal", "efmi_shadow_gate.py")
EFMI_PY_PATH = os.path.join(REPO_ROOT, "ui", "universal", "efmi.py")
INI_BUILDER_PATH = os.path.join(REPO_ROOT, "common", "m_ini_builder.py")


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


GATE = _load_module("efmi_shadow_gate_under_test", MODULE_PATH)

# 实机模组（艾尔黛拉(1)/艾尔黛拉）的部件命名：身体 LOD0 / LOD1 是两个**不同 IB** 的 component
BODY_LOD0 = "LOD0.d6128f13-14664-0"
BODY_LOD1 = "LOD1.2dca919f-7596-0"
CLOTH_LOD0 = "LOD0.650a6c6b-53472-0"
CLOTH_LOD1 = "LOD1.f09ecf2c-24768-0"
HAIR_LOD0 = "LOD0.6bf5c79b-51789-0"
HAIR_LOD1 = "LOD1.51fba901-20451-0"

# 逻辑部件键 = 该部件各 drawcall 的源物体名集合（同一物体导出的各 LOD 共用同一个键）
BODY_KEY = frozenset({"皮肤.001_copy"})
CLOTH_KEY = frozenset({"衣服_copy"})
HAIR_KEY = frozenset({"头发_copy"})

# ---- 实机导出的 16 个组件（unique_str, drawcall obj_name）逐字抄自
# K:\...\Mods\艾尔黛拉\艾尔黛拉.ini 的 `; [mesh:...]` 注释（= dc.obj_name）----
REAL_PARTS = (
    (BODY_LOD0, ["LOD0.d6128f13-14664-0.皮肤.001_copy"]),
    (BODY_LOD1, ["LOD1.2dca919f-7596-0.皮肤.001_chain1_copy"]),
    (CLOTH_LOD0, ["LOD0.650a6c6b-53472-0.衣服_copy"]),
    (CLOTH_LOD1, ["LOD1.f09ecf2c-24768-0.衣服_chain1_copy"]),
    (HAIR_LOD0, ["LOD0.6bf5c79b-51789-0.头发_copy"]),
    (HAIR_LOD1, ["LOD1.51fba901-20451-0.头发_chain1_copy"]),
    ("LOD0.902703a0-83514-0", ["LOD0.902703a0-83514-0.鞋子_copy"]),
    ("LOD1.a9b7357b-43773-0", ["LOD1.a9b7357b-43773-0.鞋子_chain1_copy"]),
    ("LOD0.b9623e31-11112-0", ["LOD0.b9623e31-11112-0.装饰_copy"]),
    ("LOD1.08d8ea21-4797-0", ["LOD1.08d8ea21-4797-0.装饰_chain1_copy"]),
    # 占位件（vertex_count = 3）：没有「前缀.物体名」形态，不该被当成逻辑部件配对
    ("LOD0.99451bd3-3879-0", ["LOD0.99451bd3-3879-0"]),
    ("LOD0.0af3ccb1-3780-0", ["LOD0.0af3ccb1-3780-0"]),
    ("LOD0.844e90f4-48735-2565", ["LOD0.844e90f4-48735-2565"]),
    ("LOD1.32a26544-1026-0", ["LOD1.32a26544-1026-0.001"]),
    ("LOD1.4532f3b9-1779-0", ["LOD1.4532f3b9-1779-0.001"]),
    ("LOD1.108b0ab1-1860-0", ["LOD1.108b0ab1-1860-0.001"]),
)

# 实机 5 组真配对（皮肤/衣服/头发/鞋子/装饰）→ 只有它们的 LOD0 需要门控
REAL_GATED = (
    BODY_LOD0,
    CLOTH_LOD0,
    HAIR_LOD0,
    "LOD0.902703a0-83514-0",
    "LOD0.b9623e31-11112-0",
)

# 实机部件 IB（ini 的 hash = 值）
MOD_IBS = {
    "d6128f13", "2dca919f", "650a6c6b", "f09ecf2c", "6bf5c79b", "51fba901",
    "902703a0", "a9b7357b", "b9623e31", "08d8ea21", "99451bd3", "0af3ccb1",
    "844e90f4", "32a26544", "4532f3b9", "108b0ab1",
}
# 08-31 抓帧实测：本模组部件出现在 3 个无 RT（深度/阴影）pass，另有多个有 RT 的材质 pass
REAL_SHADOW_PS = ("044b75548e7c9fd7", "a357a884e01209a0", "d7bb9dd57f5b70c6")
REAL_MATERIAL_PS = ("78015be8a27acd24", "2f079737a005dcf6", "8b0a39337f7454cd")


def _fake_dump_line(draw, ib=None, ib_original=None, ps=None, vs="f11c7e1dbf876a69"):
    """拼一行真实格式的 dump 记录（帧分析日志）。"""
    name = "%s-" % draw
    if ib:
        name += "ib=%s" % ib
        if ib_original:
            name += "(%s)" % ib_original
        name += "-"
    name += "vs=%s" % vs
    if ps:
        name += "-ps=%s" % ps
    return "%s 3DMigoto Dumping Buffer C:\\dump\\%s.buf -> C:\\dump\\out\\%s.buf\n" % (
        draw, name, name,
    )


def _write_log(path, draws):
    """draws: [(draw, numviews, [(ib, ib_original)], [ps, ...])] → 写真实格式 log.txt。"""
    with open(path, "w", encoding="utf-8") as handle:
        for draw, numviews, ibs, pss in draws:
            handle.write(
                "%s OMSetRenderTargets(NumViews:%d, ppRenderTargetViews:0x0, "
                "pDepthStencilView:0x0)\n" % (draw, numviews)
            )
            handle.write(
                "%s DrawIndexedInstanced(IndexCountPerInstance:100, InstanceCount:1, "
                "StartIndexLocation:0, BaseVertexLocation:0, StartInstanceLocation:0)\n"
                % draw
            )
            for ib, ib_original in ibs:
                handle.write(_fake_dump_line(draw, ib=ib, ib_original=ib_original))
            for ps in pss:
                handle.write(_fake_dump_line(draw, ps=ps))
    return path


class BareKeyAndLodIndexTests(unittest.TestCase):
    def test_bare_key_strips_lod_prefix(self):
        self.assertEqual(GATE.efmi_bare_part_key(BODY_LOD0), "d6128f13-14664-0")
        self.assertEqual(GATE.efmi_bare_part_key("LOD2.abc-1-0"), "abc-1-0")
        self.assertEqual(GATE.efmi_bare_part_key("plain-1-0"), "plain-1-0")
        self.assertEqual(GATE.efmi_bare_part_key(None), "")

    def test_lod_index(self):
        self.assertEqual(GATE.efmi_lod_index(BODY_LOD0), 0)
        self.assertEqual(GATE.efmi_lod_index(BODY_LOD1), 1)
        self.assertIsNone(GATE.efmi_lod_index("plain-1-0"))
        self.assertIsNone(GATE.efmi_lod_index(""))


class NormalizeObjNameTests(unittest.TestCase):
    def test_strips_ssmt_prefix_and_chain_suffix(self):
        self.assertEqual(
            GATE.efmi_normalize_obj_name("LOD0.d6128f13-14664-0.皮肤.001_copy"),
            "皮肤.001_copy",
        )
        self.assertEqual(
            GATE.efmi_normalize_obj_name("LOD1.2dca919f-7596-0.皮肤.001_chain1_copy"),
            "皮肤.001_copy",
        )
        self.assertEqual(
            GATE.efmi_normalize_obj_name("LOD1.a9b7357b-43773-0.鞋子_chain1_copy"),
            "鞋子_copy",
        )

    def test_chain_suffix_without_copy(self):
        self.assertEqual(GATE.efmi_normalize_obj_name("头发_chain2"), "头发")
        self.assertEqual(GATE.efmi_normalize_obj_name("头发_chain1_copy"), "头发_copy")

    def test_no_prefix_workspace_still_normalizes_chain(self):
        """无 SSMT 前缀的工程：仍靠 _chain<N> 归一收敛到同一键。"""
        self.assertEqual(GATE.efmi_normalize_obj_name("皮肤.001_copy"), "皮肤.001_copy")
        self.assertEqual(
            GATE.efmi_normalize_obj_name("皮肤.001_chain1_copy"), "皮肤.001_copy"
        )

    def test_placeholder_without_base_name_keeps_lod_prefix(self):
        """占位件（没有「前缀.物体名」形态）保留 LOD 前缀 → 不同 LOD 永不误配。"""
        self.assertEqual(
            GATE.efmi_normalize_obj_name("LOD0.99451bd3-3879-0"), "LOD0.99451bd3-3879-0"
        )
        self.assertEqual(
            GATE.efmi_normalize_obj_name("LOD1.32a26544-1026-0.001"), "001"
        )
        self.assertEqual(GATE.efmi_normalize_obj_name(""), "")
        self.assertEqual(GATE.efmi_normalize_obj_name(None), "")

    def test_part_key_drops_empty_names(self):
        self.assertEqual(GATE.efmi_part_key(["", None, "  "]), frozenset())
        self.assertEqual(
            GATE.efmi_part_key(["LOD0.d6128f13-14664-0.皮肤.001_copy"]), BODY_KEY
        )


class ShadowPsDerivationTests(unittest.TestCase):
    """pass ps 必须由抓帧推导（实机缺口：无 RT 的 pass 不止一个 ps）。"""

    def _derive(self, draws, part_ibs=MOD_IBS):
        with tempfile.TemporaryDirectory() as tmp:
            log = _write_log(os.path.join(tmp, "log.txt"), draws)
            return GATE.efmi_derive_shadow_ps_hashes([log], part_ibs)

    def test_real_frame_derives_three_pass_hashes(self):
        """实机 08-31 抓帧形态：3 个无 RT pass + 若干材质 pass → 推出 3 个 ps。"""
        draws = [
            ("000010", 0, [("d6128f13", "1d6a6186")], ["d7bb9dd57f5b70c6"]),
            ("000011", 0, [("2dca919f", "1d6a6186")], ["d7bb9dd57f5b70c6"]),
            ("000012", 0, [("650a6c6b", "1d6a6186")], ["044b75548e7c9fd7"]),
            ("000013", 0, [("a357a884"[:8], "1d6a6186")], ["d7bb9dd57f5b70c6"]),
            ("000014", 0, [("844e90f4", "1d6a6186")], ["a357a884e01209a0"]),
            ("000015", 5, [("d6128f13", "1d6a6186")], ["78015be8a27acd24"]),
            ("000016", 2, [("2dca919f", "1d6a6186")], ["2f079737a005dcf6"]),
            ("000017", 2, [("650a6c6b", "1d6a6186")], ["8b0a39337f7454cd"]),
        ]
        hashes, source = self._derive(draws)
        self.assertEqual(hashes, REAL_SHADOW_PS)
        self.assertEqual(source, GATE.EFMI_SHADOW_SOURCE_FRAME_ANALYSIS)
        for material_ps in REAL_MATERIAL_PS:
            self.assertNotIn(material_ps, hashes)

    def test_non_module_draws_are_ignored(self):
        """别的网格/AI 的无 RT pass 不该被注册（只收本模组部件所在的 pass）。"""
        draws = [
            ("000010", 0, [("deadbeef", None)], ["1111111111111111"]),
            ("000011", 0, [("d6128f13", None)], ["d7bb9dd57f5b70c6"]),
        ]
        hashes, source = self._derive(draws)
        self.assertEqual(hashes, ("d7bb9dd57f5b70c6",))
        self.assertEqual(source, GATE.EFMI_SHADOW_SOURCE_FRAME_ANALYSIS)

    def test_ps_used_by_both_rt_and_no_rt_is_excluded(self):
        """同一 ps 也用于有 RT 的 pass → 绝不注册它（拿不准就回退常量）。"""
        draws = [
            ("000010", 0, [("d6128f13", None)], ["aabbccdd00112233"]),
            ("000011", 5, [("d6128f13", None)], ["aabbccdd00112233"]),
        ]
        hashes, source = self._derive(draws)
        self.assertNotIn("aabbccdd00112233", hashes)
        self.assertEqual(hashes, GATE.EFMI_SHADOW_FALLBACK_PS_HASHES)
        self.assertEqual(source, GATE.EFMI_SHADOW_SOURCE_FALLBACK)

    def test_fallback_constant_proven_unsafe_is_dropped(self):
        """抓帧证明兜底常量落在有 RT 的 pass 上 → 不得再注册它（fail-open 不门控）。"""
        draws = [
            ("000010", 5, [("d6128f13", None)], [GATE.EFMI_SHADOW_PASS_PS_HASH]),
        ]
        hashes, source = self._derive(draws)
        self.assertEqual(hashes, ())
        self.assertEqual(source, GATE.EFMI_SHADOW_SOURCE_NOT_GATED)

    def test_partial_fallback_keeps_only_safe_entries(self):
        """多兜底项时只保留没被证明用于有 RT pass 的那些。"""
        draws = [
            ("000010", 5, [("d6128f13", None)], ["aabbccdd00112233"]),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            log = _write_log(os.path.join(tmp, "log.txt"), draws)
            hashes, source = GATE.efmi_derive_shadow_ps_hashes(
                [log], MOD_IBS, fallback=("aabbccdd00112233", "d7bb9dd57f5b70c6")
            )
        self.assertEqual(hashes, ("d7bb9dd57f5b70c6",))
        self.assertEqual(source, GATE.EFMI_SHADOW_SOURCE_FALLBACK)

    def test_multi_lod_logs_union(self):
        """多份 log（工作空间级 + 各 LOD tab）取并集。"""
        with tempfile.TemporaryDirectory() as tmp:
            log0 = _write_log(
                os.path.join(tmp, "log0.txt"),
                [("000010", 0, [("d6128f13", None)], ["d7bb9dd57f5b70c6"])],
            )
            log1 = _write_log(
                os.path.join(tmp, "log1.txt"),
                [("000020", 0, [("2dca919f", None)], ["044b75548e7c9fd7"])],
            )
            hashes, source = GATE.efmi_derive_shadow_ps_hashes([log0, log1], MOD_IBS)
        self.assertEqual(hashes, ("044b75548e7c9fd7", "d7bb9dd57f5b70c6"))
        self.assertEqual(source, GATE.EFMI_SHADOW_SOURCE_FRAME_ANALYSIS)

    def test_missing_log_or_parts_falls_back(self):
        hashes, source = GATE.efmi_derive_shadow_ps_hashes(
            [os.path.join(tempfile.gettempdir(), "not-exist-efmi-log.txt")], MOD_IBS
        )
        self.assertEqual(hashes, GATE.EFMI_SHADOW_FALLBACK_PS_HASHES)
        self.assertEqual(source, GATE.EFMI_SHADOW_SOURCE_FALLBACK)

        with tempfile.TemporaryDirectory() as tmp:
            log = _write_log(
                os.path.join(tmp, "log.txt"),
                [("000010", 0, [("d6128f13", None)], ["d7bb9dd57f5b70c6"])],
            )
            self.assertEqual(
                GATE.efmi_derive_shadow_ps_hashes([log], set()), 
                (GATE.EFMI_SHADOW_FALLBACK_PS_HASHES, GATE.EFMI_SHADOW_SOURCE_FALLBACK),
            )
            self.assertEqual(
                GATE.efmi_derive_shadow_ps_hashes([], MOD_IBS),
                (GATE.EFMI_SHADOW_FALLBACK_PS_HASHES, GATE.EFMI_SHADOW_SOURCE_FALLBACK),
            )

    def test_original_ib_hash_in_parentheses_also_matches(self):
        """dump 名里 `ib=<dedup>(<原始>)` 的括号 hash 也算命中。"""
        draws = [
            ("000010", 0, [("99999999", "d6128f13")], ["d7bb9dd57f5b70c6"]),
        ]
        hashes, source = self._derive(draws)
        self.assertEqual(hashes, ("d7bb9dd57f5b70c6",))
        self.assertEqual(source, GATE.EFMI_SHADOW_SOURCE_FRAME_ANALYSIS)


class RealExportGateTests(unittest.TestCase):
    """实机真实数据：门控必须真的发得出来（98d9856 失效的直接回归）。"""

    def test_real_export_gates_exactly_five_parts(self):
        by_part, keep_keys = GATE.efmi_shadow_part_keys(REAL_PARTS)
        gated = tuple(
            unique_str
            for unique_str, _names in REAL_PARTS
            if GATE.efmi_shadow_gate_needed(
                GATE.efmi_lod_index(unique_str),
                by_part.get(unique_str),
                keep_keys,
            )
        )
        self.assertEqual(gated, REAL_GATED)

    def test_lod1_never_gated_in_real_export(self):
        by_part, keep_keys = GATE.efmi_shadow_part_keys(REAL_PARTS)
        for unique_str, _names in REAL_PARTS:
            if GATE.efmi_lod_index(unique_str) == 1:
                self.assertFalse(
                    GATE.efmi_shadow_gate_needed(
                        GATE.efmi_lod_index(unique_str),
                        by_part.get(unique_str),
                        keep_keys,
                    )
                )

    def test_raw_obj_name_keying_yields_zero_gates(self):
        """回归守卫：按**原样 obj_name** 建键时实机 16 部件一条都发不出来。

        LOD0/LOD1 是两个不同 IB，LOD1 还是链路复制件（_chain1）——
        98d9856 就是这么写的，生成器侧修复实际从未生效。
        """
        by_part = {
            unique_str: frozenset(names)
            for unique_str, names in REAL_PARTS
            if names
        }
        keep_keys = frozenset(
            key
            for unique_str, key in by_part.items()
            if GATE.efmi_lod_index(unique_str) == 1
        )
        gated = [
            unique_str
            for unique_str, key in by_part.items()
            if GATE.efmi_shadow_gate_needed(
                GATE.efmi_lod_index(unique_str), key, keep_keys
            )
        ]
        self.assertEqual(gated, [])


class ShadowGateDecisionTests(unittest.TestCase):
    def test_lod0_gated_when_keep_lod_sibling_key_present(self):
        """实机修复场景：身体 LOD0/LOD1 是两个独立 component → LOD0 必须被门控。"""
        self.assertTrue(
            GATE.efmi_shadow_gate_needed(0, BODY_KEY, {BODY_KEY, CLOTH_KEY})
        )

    def test_lod1_itself_never_gated(self):
        """留 LOD1 作为唯一投射源：LOD1 入口不门控（否则阴影图里没有该部件）。"""
        self.assertFalse(
            GATE.efmi_shadow_gate_needed(1, BODY_KEY, {BODY_KEY, CLOTH_KEY})
        )

    def test_no_gate_without_keep_lod_sibling(self):
        """没有 LOD1 兄弟的部件行为不变（无回归）。"""
        self.assertFalse(GATE.efmi_shadow_gate_needed(0, HAIR_KEY, {BODY_KEY}))
        self.assertFalse(GATE.efmi_shadow_gate_needed(0, BODY_KEY, set()))
        self.assertFalse(GATE.efmi_shadow_gate_needed(0, BODY_KEY, None))

    def test_none_key_never_gated(self):
        """拿不到源物体键（无 drawcall 物体名）时不门控，宁可少改。"""
        self.assertFalse(GATE.efmi_shadow_gate_needed(0, None, {BODY_KEY}))

    def test_pairing_key_must_not_be_the_part_name(self):
        """回归守卫：实机身体的 LOD0/LOD1 裸名不同（两个不同 IB）。"""
        self.assertNotEqual(
            GATE.efmi_bare_part_key(BODY_LOD0), GATE.efmi_bare_part_key(BODY_LOD1)
        )
        self.assertFalse(GATE.efmi_shadow_gate_needed(0, BODY_KEY, {CLOTH_KEY}))

    def test_multi_drawcall_part_requires_whole_group_match(self):
        """多 drawcall 部件按整组键匹配：少一件就不门控（保守方向）。"""
        whole = GATE.efmi_part_key(["LOD0.a-1-0.皮肤_copy", "LOD0.a-1-0.皮肤2_copy"])
        partial = GATE.efmi_part_key(["LOD0.a-1-0.皮肤_copy"])
        self.assertTrue(GATE.efmi_shadow_gate_needed(0, whole, {whole}))
        self.assertFalse(GATE.efmi_shadow_gate_needed(0, partial, {whole}))

    def test_part_keys_merges_duplicate_unique_str(self):
        by_part, keep_keys = GATE.efmi_shadow_part_keys(
            (
                ("LOD0.a-1-0", ["LOD0.a-1-0.皮肤_copy"]),
                ("LOD0.a-1-0", ["LOD0.a-1-0.皮肤2_copy"]),
                (BODY_LOD1, ["LOD1.2dca919f-7596-0.皮肤.001_chain1_copy"]),
            )
        )
        self.assertEqual(
            by_part["LOD0.a-1-0"], frozenset({"皮肤_copy", "皮肤2_copy"})
        )
        self.assertEqual(by_part[BODY_LOD1], BODY_KEY)
        self.assertIn(BODY_KEY, keep_keys)

    def test_empty_or_nameless_parts_are_skipped(self):
        by_part, keep_keys = GATE.efmi_shadow_part_keys(
            (("LOD0.a-1-0", []), ("", ["LOD0.a-1-0.皮肤_copy"]), (None, ["x"]))
        )
        self.assertEqual(by_part, {})
        self.assertEqual(keep_keys, frozenset())


class GateLineTests(unittest.TestCase):
    def test_gate_open_lines_use_ps_stage_tag(self):
        lines = GATE.efmi_shadow_gate_open_lines()
        self.assertEqual(len(lines), 2)
        self.assertIn("shadow-gate", lines[0])
        self.assertEqual(lines[1], "if ps != 99001")
        self.assertEqual(GATE.EFMI_SHADOW_PS_FILTER_INDEX, 99001)
        self.assertEqual(GATE.EFMI_SHADOW_KEEP_LOD, 1)
        self.assertEqual(GATE.EFMI_SHADOW_GATE_LOD, 0)

    def test_stage_tag_is_private_number(self):
        """回归守卫（2026-09-15 实机事故）：阶段标签必须是私有号段，**不得**借用别家 mod 的号。

        v4.4.45 首版把标签写成 RabbitFX 的 1718.2 → RabbitFX 给它改写的**可见 G-buffer**
        shader 打同一个号 → `if ps != 1718.2` 在可见 pass 里也不成立 → 门控关闭 →
        `handling = skip` 之外那份 LOD0 外壳消失（实机：正面被剔、只剩背面）。
        """
        self.assertEqual(GATE.efmi_shadow_filter_index_text(), "99001")
        self.assertEqual(GATE.EFMI_SHADOW_FALLBACK_PS_HASHES,
                         (GATE.EFMI_SHADOW_PASS_PS_HASH,))
        for foreign in (1718.1, 1718.2, 1718.3, 200, 201, 202, 203, 204, 99001.5):
            self.assertNotEqual(GATE.EFMI_SHADOW_PS_FILTER_INDEX, foreign)

    def test_override_lines_single_hash(self):
        lines = GATE.efmi_shadow_override_lines(
            GATE.efmi_shadow_override_section_name("Mod_Test"),
            ["d7bb9dd57f5b70c6"],
        )
        self.assertEqual(lines[0], "[ShaderOverride_ShadowPS_ModTest]")
        self.assertEqual(lines[1], "hash = d7bb9dd57f5b70c6")
        self.assertEqual(lines[2], "filter_index = 99001")
        self.assertEqual(lines[3], "allow_duplicate_hash = overrule")

    def test_override_lines_register_all_derived_hashes_with_one_tag(self):
        lines = GATE.efmi_shadow_override_lines(
            GATE.efmi_shadow_override_section_name("Mod_Test"),
            REAL_SHADOW_PS,
        )
        text = "\n".join(lines)
        for index, ps_hash in enumerate(REAL_SHADOW_PS, start=1):
            self.assertIn("[ShaderOverride_ShadowPS_ModTest_%d]" % index, text)
            self.assertIn("hash = " + ps_hash, text)
        self.assertEqual(text.count("filter_index = 99001"), len(REAL_SHADOW_PS))
        self.assertEqual(text.count("allow_duplicate_hash = overrule"), len(REAL_SHADOW_PS))

    def test_override_lines_default_to_fallback(self):
        lines = GATE.efmi_shadow_override_lines("ShaderOverride_Test")
        self.assertIn("hash = " + GATE.EFMI_SHADOW_PASS_PS_HASH, lines)

    def test_override_section_name_is_unique_and_deterministic(self):
        self.assertEqual(
            GATE.efmi_shadow_override_section_name("艾尔黛拉"),
            GATE.efmi_shadow_override_section_name("艾尔黛拉"),
        )
        self.assertNotEqual(
            GATE.efmi_shadow_override_section_name("艾尔黛拉"),
            GATE.efmi_shadow_override_section_name("伊冯"),
        )
        name = GATE.efmi_shadow_override_section_name("艾尔黛拉")
        self.assertTrue(name.startswith("ShaderOverride_ShadowPS_"))
        self.assertNotIn(" ", name)


class InBuilderWiringTests(unittest.TestCase):
    """ShaderOverride 段类型必须真的进 save_to_file 的写出顺序（否则注册段静默丢失）。"""

    def test_shader_override_section_is_written(self):
        builder_mod = _load_module("m_ini_builder_under_test", INI_BUILDER_PATH)
        builder = builder_mod.M_IniBuilder()
        section = builder_mod.M_IniSection(builder_mod.M_SectionType.ShaderOverride)
        section.extend(
            GATE.efmi_shadow_override_lines(
                GATE.efmi_shadow_override_section_name("Mod_Test"),
                REAL_SHADOW_PS,
            )
        )
        builder.append_section(section)
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "mod.ini")
            builder.save_to_file(target)
            with open(target, encoding="utf-8") as handle:
                text = handle.read()
        self.assertIn("[ShaderOverride_ShadowPS_ModTest_1]", text)
        self.assertIn("[ShaderOverride_ShadowPS_ModTest_3]", text)
        self.assertIn("hash = " + REAL_SHADOW_PS[0], text)
        self.assertIn("filter_index = 99001", text)
        self.assertIn(";MARK:ShaderOverride", text)


class EmitterSourceRegressionTests(unittest.TestCase):
    """发射侧源码级回归（无 bpy：只读文本）。"""

    @classmethod
    def setUpClass(cls):
        with open(EFMI_PY_PATH, encoding="utf-8") as handle:
            cls.source = handle.read()
        # 剥离行内注释：注释里的解释性文字不算门控
        cls.code = "\n".join(
            line.split("#", 1)[0] for line in cls.source.splitlines()
        )
        start = cls.source.index("[阴影 pass 单投射源]")
        end = cls.source.index("ini_builder.append_section(shadow_override_section)")
        cls.gate_region = "\n".join(
            line.split("#", 1)[0] for line in cls.source[start:end].splitlines()
        )

    def test_keys_come_from_pure_module(self):
        self.assertIn("efmi_shadow_part_keys(", self.code)
        self.assertIn("efmi_shadow_gate_needed(", self.code)
        # 不得再按原样 obj_name 建键（98d9856 的失效写法：frozenset(obj_name)）
        self.assertNotIn("_efmi_shadow_part_keys", self.code)
        self.assertNotIn('str(getattr(_dc, "obj_name"', self.code)

    def test_pass_hashes_are_derived_from_frame_analysis(self):
        """注册段必须用抓帧推导的 ps 全集，且抓帧不可用时能回退常量。"""
        self.assertIn("_efmi_shadow_pass_ps_hashes(", self.code)
        self.assertIn("efmi_derive_shadow_ps_hashes(", self.code)
        self.assertIn("resolve_frame_analysis_dirs_by_lod(", self.code)
        self.assertIn("EFMI_SHADOW_FALLBACK_PS_HASHES", self.code)

    def test_empty_ps_set_disables_gate_entirely(self):
        """fail-open 闸门：推导结果为空（兜底项被证明不安全）时整体不发射门控。"""
        self.assertIn("shadow_ps_hashes = None", self.code)
        self.assertIn("if _shadow_gate and not shadow_ps_hashes:", self.code)
        self.assertIn("EFMI_SHADOW_SOURCE_NOT_GATED", self.code)

    def test_no_vs_filter_regression(self):
        """阴影门控区间不得回退 vs 口径，也不得用 DRAW_TYPE 当阶段判据。"""
        self.assertNotIn("EFMI_SHADOW_PASS_VS_FILTER_INDEX", self.code)
        self.assertNotIn("if vs", self.gate_region)
        self.assertNotIn("DRAW_TYPE", self.gate_region)

    def test_handling_skip_stays_outside_gate(self):
        skip_pos = self.code.index('texture_override_ib_section.append("handling = skip")')
        gate_pos = self.code.index("efmi_shadow_gate_open_lines()")
        payload_pos = self.code.index("component_id = {merged_component_id}")
        self.assertLess(skip_pos, gate_pos, "handling = skip 必须在门控外（前面）")
        self.assertLess(gate_pos, payload_pos, "门控必须包住组件绘制载荷")

    def test_endif_and_override_section_emitted(self):
        self.assertIn('texture_override_ib_section.append("endif")', self.code)
        self.assertIn("efmi_shadow_override_lines(", self.code)
        self.assertIn("shadow_override_section", self.code)
        self.assertIn("ini_builder.append_section(shadow_override_section)", self.code)
        self.assertIn("M_SectionType.ShaderOverride", self.code)


if __name__ == "__main__":
    unittest.main(verbosity=2)
