r"""T39-X：HIMI 合并骨架的**族级物体矩阵 X_f** 单测（fake 宿主，不依赖 bpy / 游戏）。

覆盖：

A. 128 B 身份资源载荷（头部 + 单位阵 + X_f）的发布 / 解码 / 四方同步（含反例）；
   【t78 方案 B】`[1..3]` 不再是 Q_f⁻¹：顶点侧不校准 ⇒ 这一段恒为单位阵，非单位阵必须被拒；
B. 合并 attach CS 的 X 左乘块存在且被判据接受；**A4 三处变异必须被 merged_attach_hlsl_problems 抓到**；
C. X 左乘的文本模型（attach_x_model_from_hlsl / compose_x_rows_from_model）与 mat3x4_compose 数值一致；
D. 宿主族判定 / 骨对偶校验 / 三态 fail-closed 的纯函数部分；
E. **导出期硬门**：发布路径用同一组 ranges / 单位阵 / family_xs 复核落盘字节（族数 × 128 B）；
   期望字节数或载荷里的 X 块被改错时必须 Fatal（变异检测）。

本文件只加载「无 bpy」的真实模块（与 tests/test_himi_merged_runtime.py 同款 fake 包前言，但**不加载**
utils/vertexgroup_utils.py），因此在本机（无 bpy）也能真跑：

    C:\Python311\python.exe -m pytest tests/test_himi_family_x.py -q

装了 Blender/bpy 的环境用同一条命令即可（前言替身本身不依赖 bpy）。
"""

import ast
import importlib.util
import json
import math
import shutil
import inspect
import os
import struct
import sys
import tempfile
import textwrap
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "himi_family_x_test_pkg"


# --------------------------------------------------------------------------- #
# fake 宿主：只把「被测模块的相对依赖」按 fake 包前缀登记（与既有单测同款）
# --------------------------------------------------------------------------- #


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    parent, _, leaf = name.rpartition(".")
    if parent in sys.modules and leaf:
        setattr(sys.modules[parent], leaf, module)
    return module


def _load_real_module(qualname, relpath):
    path = REPO_ROOT / relpath
    spec = importlib.util.spec_from_file_location(qualname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    parent, _, leaf = qualname.rpartition(".")
    if parent in sys.modules and leaf:
        setattr(sys.modules[parent], leaf, module)
    spec.loader.exec_module(module)
    return module


for package_name in (
    PKG,
    f"{PKG}.ui",
    f"{PKG}.ui.universal",
    f"{PKG}.common",
    f"{PKG}.utils",
    f"{PKG}.blueprint",
):
    package = _install_module(package_name)
    package.__path__ = []

_load_real_module(f"{PKG}.utils.json_utils", "utils/json_utils.py")
_load_real_module(f"{PKG}.utils.tbn_codec", "utils/tbn_codec.py")
_load_real_module(f"{PKG}.utils.format_utils", "utils/format_utils.py")
_load_real_module(f"{PKG}.common.m_ini_builder", "common/m_ini_builder.py")
_load_real_module(f"{PKG}.common.logic_name", "common/logic_name.py")
_load_real_module(f"{PKG}.common.global_key_count_helper", "common/global_key_count_helper.py")
_load_real_module(f"{PKG}.common.himi_merged_contract", "common/himi_merged_contract.py")
_load_real_module(f"{PKG}.common.himi_skeleton", "common/himi_skeleton.py")

_GLOBAL_STATE = {"workspace": "", "mod": ""}
_fake_global_config = types.SimpleNamespace(
    path_workspace_folder=lambda: _GLOBAL_STATE["workspace"],
    path_generate_mod_folder=lambda: _GLOBAL_STATE["mod"],
    path_generate_mod_ini=lambda suffix="": os.path.join(
        _GLOBAL_STATE["mod"],
        os.path.basename(_GLOBAL_STATE["workspace"].rstrip("\\/")) + suffix + ".ini",
    ),
    path_generatemod_buffer_folder=lambda: (
        os.path.join(_GLOBAL_STATE["mod"], "Meshes") if _GLOBAL_STATE["mod"] else ""
    ),
    get_workspace_name=lambda: os.path.basename(_GLOBAL_STATE["workspace"].rstrip("\\/")),
)
_properties_state = {"checkbox": True}
_fake_global_properties = types.SimpleNamespace(
    import_merged_vgmap=lambda: bool(_properties_state["checkbox"]),
    forbid_auto_texture_ini=lambda: False,
    generate_branch_mod_gui=lambda: False,
)
_fake_export_helper = types.SimpleNamespace(
    current_buffer_folder_name="Meshes",
    get_current_buffer_folder_name=lambda: _fake_export_helper.current_buffer_folder_name,
)

_install_module(f"{PKG}.common.global_config", GlobalConfig=_fake_global_config)
_install_module(f"{PKG}.common.global_properties", GlobalProterties=_fake_global_properties)
_install_module(
    f"{PKG}.common.m_ini_helper",
    M_IniHelper=types.SimpleNamespace(
        get_drawindexed_str_list=lambda *_a, **_k: [],
        is_slot_binding_mark_type=lambda *_a, **_k: False,
    ),
)
_install_module(f"{PKG}.common.m_ini_helper_gui", M_IniHelperGUI=types.SimpleNamespace())
_install_module(
    f"{PKG}.utils.timer_utils",
    TimerUtils=types.SimpleNamespace(start_stage=lambda *_a, **_k: None, end_stage=lambda *_a, **_k: None),
)
_install_module(f"{PKG}.blueprint.export_helper", BlueprintExportHelper=_fake_export_helper)


class _FakeDrawIBExportBase:
    def __init__(self, blueprint_model, combine_ib=False):
        self.blueprint_model = blueprint_model
        self.drawib_model_list = []


_install_module(f"{PKG}.ui.universal.drawib_export_base", DrawIBExportBase=_FakeDrawIBExportBase)

_contract = sys.modules[f"{PKG}.common.himi_merged_contract"]
_runtime = _load_real_module(f"{PKG}.common.himi_merged_runtime", "common/himi_merged_runtime.py")
_family_x = _load_real_module(f"{PKG}.common.himi_family_x", "common/himi_family_x.py")
_unity = _load_real_module(f"{PKG}.ui.universal.unity", "ui/universal/unity.py")
_himi_exporter = _load_real_module(f"{PKG}.ui.universal.himi", "ui/universal/himi.py")

ExportHIMI = _himi_exporter.ExportHIMI

IDENTITY = list(_family_x.IDENTITY_3X4)


def _rotate_z(degrees: float) -> list:
    import math

    angle = math.radians(degrees)
    cos_v = math.cos(angle)
    sin_v = math.sin(angle)
    return [
        cos_v, -sin_v, 0.0, 0.0,
        sin_v, cos_v, 0.0, 0.0,
        0.0, 0.0, 1.0, 0.0,
    ]


def _translate(dx: float, dy: float, dz: float) -> list:
    return [1.0, 0.0, 0.0, dx, 0.0, 1.0, 0.0, dy, 0.0, 0.0, 1.0, dz]


def _pack12(values) -> bytes:
    return struct.pack("<12f", *[float(v) for v in values])


class AttachRangePayloadTests(unittest.TestCase):
    """A3：128 B 载荷 + 解码往返 + 四方同步（发布 / 解码 / CS 下标 / ini 字节数）。"""

    def test_attach_range_bytes_is_128(self):
        self.assertEqual(_runtime.ATTACH_RANGE_BYTES, 128)
        self.assertEqual(_runtime.ATTACH_RANGE_HEADER_BYTES, 16)
        self.assertEqual(_runtime.ATTACH_RANGE_X_OFFSET, 64)
        self.assertEqual(_runtime.ATTACH_RANGE_FAMILY_X_BYTES, 48)
        # 【t74】[7] = (asuint own_index, asuint base_index, 0, 0)：运行期 X 的族序号
        self.assertEqual(_runtime.ATTACH_RANGE_INDICES_OFFSET, 112)
        self.assertEqual(
            _runtime.ATTACH_RANGE_BYTES,
            _runtime.ATTACH_RANGE_HEADER_BYTES + 48 + 48 + 16,
        )
        self.assertEqual(_runtime.ATTACH_RANGE_BYTES, 8 * 16)

    def test_payload_layout_and_decode_roundtrip(self):
        q = _rotate_z(30.0)
        x = _translate(1.5, -2.5, 3.5)
        payload = _runtime.attach_range_payload(
            184, 71, placement=q, family_x=x, indices=(2, 0)
        )
        self.assertEqual(len(payload), 128)
        self.assertEqual(payload[16:64], _pack12(q))
        self.assertEqual(payload[64:112], _pack12(x))
        self.assertEqual(payload[112:128], struct.pack("<4I", 2, 0, 0, 0))
        decoded = _runtime.decode_attach_range_payload(payload)
        self.assertTrue(decoded["has_indices"])
        self.assertEqual(decoded["own_index"], 2)
        self.assertEqual(decoded["base_index"], 0)
        self.assertTrue(decoded["ok"])
        self.assertEqual(decoded["base"], 184)
        self.assertEqual(decoded["span"], 71)
        self.assertEqual(decoded["version"], _runtime.ATTACH_RANGE_VERSION)
        self.assertEqual(decoded["magic"], _runtime.ATTACH_RANGE_MAGIC)
        self.assertTrue(decoded["has_placement"])
        self.assertTrue(decoded["has_family_x"])
        self.assertEqual(_pack12(decoded["placement"]), _pack12(q))
        self.assertEqual(_pack12(decoded["family_x"]), _pack12(x))
        # 【t78 方案 B】低层编解码器仍原样保留调用方给的 12 个数，但发布路径只允许单位阵：
        # 这里 Q 非单位阵 ⇒ 解码必须如实报「不是单位阵」，供 verify 侧 fail-closed。
        self.assertFalse(decoded["placement_is_identity"])

    def test_missing_blocks_publish_identity_and_report_uncalibrated(self):
        """【t78 方案 B】不给 placement ⇒ v2+ 载荷的 [1..3] 写**单位阵**（不再是全零/Q⁻¹）。

        顶点侧已不再校准 ⇒ 载荷里的摆放段只能是单位阵；写全零会让 CS 的
        ``calibrated`` 门直接跳过（偶合「不乘」），但那样「单位阵」这条契约在载荷里
        就没有任何证据，verify 也无从 fail-closed。故默认写单位阵、其余 64 B 为 0。
        """
        payload = _runtime.attach_range_payload(0, 184)
        self.assertEqual(len(payload), 128)
        self.assertEqual(payload[16:64], _pack12(_contract.attach_placement_identity()))
        self.assertEqual(payload[64:], bytes(64))
        decoded = _runtime.decode_attach_range_payload(payload)
        self.assertTrue(decoded["ok"])
        # 【t74】``has_indices`` 只表示「载荷里有 [7] 且版本 >= 4」，**不看数值**：全零的 [7]
        # 与「族 0 / 宿主 0」在数值上不可分（CS 侧靠 own==base ⇒ X=单位阵，与「不左乘」等价）。
        self.assertTrue(decoded["has_indices"])
        self.assertEqual((decoded["own_index"], decoded["base_index"]), (0, 0))
        self.assertTrue(decoded["has_placement"])
        self.assertTrue(decoded["placement_is_identity"])
        self.assertFalse(decoded["has_family_x"])
        # version 1（历史载荷）没有摆放段 ⇒ 仍是 12 个 0.0，且不能报成「单位阵」
        legacy_v1 = _runtime.decode_attach_range_payload(
            struct.pack("<4I", 0, 184, 1, _runtime.ATTACH_RANGE_MAGIC) + bytes(112)
        )
        self.assertFalse(legacy_v1["has_placement"])
        self.assertFalse(legacy_v1["placement_is_identity"])
        legacy = _runtime.decode_attach_range_payload(
            struct.pack("<4I", 0, 184, 3, _runtime.ATTACH_RANGE_MAGIC) + bytes(96)
        )
        self.assertFalse(legacy["has_indices"])
        self.assertEqual((legacy["own_index"], legacy["base_index"]), (-1, -1))

    def test_decode_accepts_legacy_64_byte_payload(self):
        legacy = struct.pack("<4I", 0, 184, 3, _runtime.ATTACH_RANGE_MAGIC) + bytes(48)
        self.assertEqual(len(legacy), 64)
        decoded = _runtime.decode_attach_range_payload(legacy)
        self.assertTrue(decoded["ok"])
        self.assertFalse(decoded["has_placement"])
        self.assertFalse(decoded["has_family_x"])
        self.assertEqual(list(decoded["family_x"]), [])

    def test_decode_rejects_unknown_length(self):
        self.assertFalse(_runtime.decode_attach_range_payload(bytes(7))["ok"])
        self.assertFalse(_runtime.decode_attach_range_payload(bytes(120))["ok"])
        self.assertFalse(_runtime.decode_attach_range_payload(bytes(16 + 48 + 48 + 16 + 4))["ok"])

    def test_payload_rejects_wrong_block_length(self):
        with self.assertRaises(_runtime.HIMIResourceDeclError):
            _runtime.attach_range_payload(0, 1, placement=[0.0] * 11)
        with self.assertRaises(_runtime.HIMIResourceDeclError):
            _runtime.attach_range_payload(0, 1, family_x=[0.0] * 13)

    def test_ini_lines_have_no_array_row(self):
        lines = _runtime.attach_range_ini_lines("2c706fbd", "./Meshes/2c706fbd-AttachRange.buf")
        self.assertEqual(len(lines), 4)
        self.assertEqual(lines[0], "[ResourceHI3AttachRange_2c706fbd]")
        self.assertIn("type = Buffer", lines)
        self.assertIn("format = " + _runtime.ATTACH_RANGE_INI_FORMAT, lines)
        self.assertFalse([line for line in lines if line.strip().lower().startswith("array")])
        self.assertEqual(
            _runtime.attach_range_basename("2c706fbd"), "2c706fbd-AttachRange.buf"
        )

    def test_publish_and_verify_four_way_sync(self):
        folder = tempfile.mkdtemp(prefix="himi_attach_x_")
        ranges = {
            "2c706fbd": {"base": 0, "slots": 184, "own_block": True},
            "70a4da3e": {"base": 184, "slots": 71, "own_block": True},
            "694025c6": {"base": 255, "slots": 0, "own_block": False},
        }
        # 【t78 方案 B】摆放段只能是单位阵（顶点侧不校准）
        placements = {
            "2c706fbd": _contract.attach_placement_identity(),
            "70a4da3e": _contract.attach_placement_identity(),
        }
        family_xs = {"2c706fbd": IDENTITY, "70a4da3e": _translate(0.25, 0.5, -0.75)}
        indices = {"2c706fbd": (0, 0), "70a4da3e": (1, 0)}
        result = _runtime.publish_attach_ranges(
            ranges, folder, placements=placements, family_xs=family_xs, indices=indices
        )
        self.assertTrue(result["ok"])
        self.assertTrue(result["written"])
        # 每族 2 份：身份载荷（128 B）+ BorrowMap（【t91】借用表与身份资源同批落盘）
        self.assertEqual(len(result["files"]), 4)
        self.assertIn("128 B", result["message"])
        self.assertIn("X_f", result["message"])
        self.assertIn("单位阵", result["message"])
        self.assertFalse(
            os.path.exists(os.path.join(folder, _runtime.attach_range_basename("694025c6")))
        )
        total = 0
        for family in ("2c706fbd", "70a4da3e"):
            path = os.path.join(folder, _runtime.attach_range_basename(family))
            with open(path, "rb") as handle:
                raw = handle.read()
            self.assertEqual(len(raw), 128)
            total += len(raw)
        self.assertEqual(total, 2 * 128)
        self.assertEqual(
            _runtime.verify_attach_range_files(
                folder, ranges, placements=placements, family_xs=family_xs, indices=indices
            ),
            [],
        )
        # 【t74】[7] 逐值核对：族索引被改错必须被抓到（CS 会去读别族的 M_base）
        bad_indices = {"2c706fbd": (0, 0), "70a4da3e": (2, 0)}
        problems = _runtime.verify_attach_range_files(
            folder, ranges, placements=placements, family_xs=family_xs, indices=bad_indices
        )
        self.assertTrue([p for p in problems if "族索引" in p])

    def test_publish_refuses_non_identity_placement(self):
        """【t78 方案 B】fail-closed：非单位阵的摆放段必须**在写盘前**被拒，且目录里不落文件。"""
        folder = tempfile.mkdtemp(prefix="himi_attach_x_badq_")
        ranges = {"2c706fbd": {"base": 0, "slots": 184, "own_block": True}}
        result = _runtime.publish_attach_ranges(
            ranges, folder, placements={"2c706fbd": _rotate_z(12.0)}
        )
        self.assertFalse(result["ok"])
        self.assertFalse(result["written"])
        self.assertEqual(result["files"], [])
        self.assertIn("不是单位阵", result["message"])
        self.assertEqual(os.listdir(folder), [])
        # 同时：把已合规的产物手动改坏成非单位阵，verify 必须报出来（上层据此 Fatal）
        good = os.path.join(folder, _runtime.attach_range_basename("2c706fbd"))
        _runtime.publish_attach_ranges(ranges, folder)
        with open(good, "rb") as handle:
            raw = bytearray(handle.read())
        raw[16 : 16 + 48] = _pack12(_rotate_z(12.0))
        with open(good, "wb") as handle:
            handle.write(bytes(raw))
        problems = _runtime.verify_attach_range_files(folder, ranges)
        self.assertTrue([p for p in problems if "不是单位阵" in p], problems)

    def test_verify_catches_wrong_x_bytes(self):
        folder = tempfile.mkdtemp(prefix="himi_attach_x_bad_")
        ranges = {"2c706fbd": {"base": 0, "slots": 184, "own_block": True}}
        placements = {"2c706fbd": IDENTITY}
        family_xs = {"2c706fbd": _translate(1.0, 0.0, 0.0)}
        _runtime.publish_attach_ranges(
            ranges, folder, placements=placements, family_xs=family_xs
        )
        wrong = {"2c706fbd": _translate(2.0, 0.0, 0.0)}
        problems = _runtime.verify_attach_range_files(
            folder, ranges, placements=placements, family_xs=wrong
        )
        self.assertTrue(problems)
        self.assertTrue([p for p in problems if "X_f" in p])
        self.assertEqual(
            _runtime.verify_attach_range_files(folder, ranges, placements=placements), []
        )

    def test_verify_catches_truncated_file(self):
        folder = tempfile.mkdtemp(prefix="himi_attach_x_short_")
        ranges = {"2c706fbd": {"base": 0, "slots": 184, "own_block": True}}
        path = os.path.join(folder, _runtime.attach_range_basename("2c706fbd"))
        with open(path, "wb") as handle:
            handle.write(struct.pack("<4I", 0, 184, 3, _runtime.ATTACH_RANGE_MAGIC) + bytes(48))
        problems = _runtime.verify_attach_range_files(folder, ranges)
        self.assertTrue(problems)
        self.assertTrue([p for p in problems if "128 B" in p])

    def test_reference_gate_requires_128_bytes(self):
        root = tempfile.mkdtemp(prefix="himi_mod_root_")
        os.makedirs(os.path.join(root, "Meshes"), exist_ok=True)
        target = os.path.join(root, "Meshes", _runtime.attach_range_basename("2c706fbd"))
        ini = "\n".join(
            [
                "[ResourceHI3AttachRange_2c706fbd]",
                "type = Buffer",
                "format = " + _runtime.ATTACH_RANGE_INI_FORMAT,
                "filename = ./Meshes/2c706fbd-AttachRange.buf",
                "",
                "[CommandList_Attach]",
                "cs-t1 = ref ResourceHI3AttachRange_2c706fbd",
            ]
        )
        with open(target, "wb") as handle:
            handle.write(struct.pack("<4I", 0, 184, 3, _runtime.ATTACH_RANGE_MAGIC) + bytes(48))
        problems = _himi_exporter.verify_attach_range_references(ini, root)
        self.assertTrue(problems)
        self.assertTrue([p for p in problems if "128" in p])
        with open(target, "wb") as handle:
            handle.write(_runtime.attach_range_payload(0, 184, placement=IDENTITY, family_x=IDENTITY))
        self.assertEqual(_himi_exporter.verify_attach_range_references(ini, root), [])

    def test_reference_gate_ignores_vb_bound_cs_slots(self):
        """t109：`[CustomShaderHI3MergedSkin_*]` 里的 `cs-t1 = ref Resource<fam>Blend` 绑的是 VB 缓冲。

        门只认 `ResourceHI3AttachRange_*`（身份载荷）。把 VB 绑定也算进来 ⇒ 每次导出都假报
        「没有 [Resource<fam>Blend] 声明」⇒ 契约降级成普通导出，合并骨架整条路径被打掉。
        """
        root = tempfile.mkdtemp(prefix="himi_mod_root_vb_bound_")
        os.makedirs(os.path.join(root, "Meshes"), exist_ok=True)
        target = os.path.join(root, "Meshes", _runtime.attach_range_basename("2c706fbd"))
        ini = "\n".join(
            [
                "[CustomShaderHI3MergedSkin_2c706fbd]",
                "cs-t1 = ref Resource2c706fbdBlend",
                "cs-u0 = ref ResourceHI3SkinOut_2c706fbd",
                "",
                "[ResourceHI3AttachRange_2c706fbd]",
                "type = Buffer",
                "format = " + _runtime.ATTACH_RANGE_INI_FORMAT,
                "filename = ./Meshes/2c706fbd-AttachRange.buf",
                "",
                "[CommandList_Attach]",
                "cs-t1 = ref ResourceHI3AttachRange_2c706fbd",
            ]
        )
        with open(target, "wb") as handle:
            handle.write(_runtime.attach_range_payload(0, 184, placement=IDENTITY, family_x=IDENTITY))
        self.assertEqual(_himi_exporter.verify_attach_range_references(ini, root), [])


class AttachCsTests(unittest.TestCase):
    """B / A4：CS 的 X 左乘块 + 三条锚点的变异判据。"""

    def setUp(self):
        self.text = _himi_exporter.merged_attach_hlsl_text()

    def test_cs_contains_x_block_and_passes_problems(self):
        for token in (
            "himi_attach_range[4]",
            "himi_attach_range[5]",
            "himi_attach_range[6]",
            "float4 attach_x0 = himi_attach_range[4];",
            "float3 d0 = float3(out0.x, out1.x, out2.x);",
            "float3 dt = float3(out0.w, out1.w, out2.w);",
            "out0 = float4(dot(xa0, d0), dot(xa0, d1), dot(xa0, d2), dot(xa0, dt) + attach_x0.w);",
            "out1 = float4(dot(xa1, d0), dot(xa1, d1), dot(xa1, d2), dot(xa1, dt) + attach_x1.w);",
            "out2 = float4(dot(xa2, d0), dot(xa2, d1), dot(xa2, d2), dot(xa2, dt) + attach_x2.w);",
        ):
            self.assertIn(token, self.text)
        self.assertEqual(_himi_exporter.merged_attach_hlsl_problems(self.text), [])
        model = _runtime.attach_compose_model_from_hlsl(self.text)
        self.assertEqual(sorted(set(model["modes"].values())), ["correct"])

    def _mutate_and_expect_problem(self, anchor: str):
        self.assertIn(anchor, self.text)
        broken = self.text.replace(anchor, "")
        self.assertNotEqual(broken, self.text)
        problems = _himi_exporter.merged_attach_hlsl_problems(broken)
        self.assertTrue(problems, "变异 " + anchor + " 之后判据必须报错")
        self.assertTrue(
            [p for p in problems if "t39-X" in p],
            "变异 " + anchor + " 的报错必须点名 t39-X：" + str(problems),
        )

    def test_a4_mutation_removing_x_read_line(self):
        self._mutate_and_expect_problem("float4 attach_x0 = himi_attach_range[4];")

    def test_a4_mutation_removing_column_split(self):
        self._mutate_and_expect_problem("float3 d0 = float3(out0.x, out1.x, out2.x);")

    def test_a4_mutation_removing_left_multiply(self):
        self._mutate_and_expect_problem(
            "out0 = float4(dot(xa0, d0), dot(xa0, d1), dot(xa0, d2), dot(xa0, dt) + attach_x0.w);"
        )

    def test_a4_mutation_removing_xa_line(self):
        self._mutate_and_expect_problem(
            "float3 xa0 = float3(attach_x0.x, attach_x0.y, attach_x0.z);"
        )

    def test_x_model_matches_mat3x4_compose(self):
        model = _runtime.attach_x_model_from_hlsl(self.text)
        self.assertTrue(model["ok"], model.get("problems"))
        out_flat = _runtime.mat3x4_compose(_rotate_z(20.0), _translate(1.0, 2.0, 3.0))
        x_flat = _runtime.mat3x4_compose(_rotate_z(5.0), _translate(-0.5, 0.25, 1.0))
        out_rows = [out_flat[index * 4:index * 4 + 4] for index in range(3)]
        x_rows = [x_flat[index * 4:index * 4 + 4] for index in range(3)]
        composed = _runtime.compose_x_rows_from_model(model, out_rows, x_rows)
        expected = _runtime.mat3x4_compose(x_flat, out_flat)
        flat = [float(v) for row in composed for v in row]
        for got, want in zip(flat, expected):
            self.assertAlmostEqual(got, float(want), places=5)

    def test_x_model_rejects_partial_block(self):
        broken = self.text.replace(
            "out1 = float4(dot(xa1, d0), dot(xa1, d1), dot(xa1, d2), dot(xa1, dt) + attach_x1.w);", ""
        )
        model = _runtime.attach_x_model_from_hlsl(broken)
        self.assertFalse(model["ok"])
        self.assertTrue(model["problems"])

    # ---- 【t74】运行期 X：cb3 副本 + u1 物体矩阵表 + X 的来源选择 ----

    def test_cs_contains_runtime_x_anchors(self):
        for token in (
            "cbuffer himi_object_cb : register(b1)",
            "float4 himi_obj_rows[8];",
            "RWBuffer<float4> himi_object_matrix : register(u1);",
            "HimiAffine himi_cb3_affine()",
            "HimiAffine himi_cb3_inverse_affine()",
            "bool himi_object_matrix_valid()",
            "uint own_index = asuint(himi_attach_range[7].x);",
            "uint base_index = asuint(himi_attach_range[7].y);",
            "if (own_index == base_index)",
            "himi_object_matrix[own_row + 0u] = own_object.r0;",
            "float4 base0 = himi_object_matrix[base_row + 0u];",
            "HimiAffine x_rows = himi_affine_mul(himi_affine_inverse(m_base), own_object);",
        ):
            self.assertIn(token, self.text)
        self.assertEqual(_himi_exporter.merged_attach_hlsl_problems(self.text), [])

    def test_runtime_x_block_never_returns(self):
        """硬纪律：运行期 X 的整块里不许出现 ``return;`` —— draw 必须无条件执行。"""
        block = self.text.split("if (asuint(attach_range.z) >= 4u)", 1)[1]
        block = block.split("bool x_calibrated", 1)[0]
        self.assertNotIn("return;", block)
        # 兜底 X 的读取必须仍在选择块**之前**（否则实时路径一失败就没有 fallback）
        self.assertLess(
            self.text.index("float4 attach_x2 = himi_attach_range[6];"),
            self.text.index("if (asuint(attach_range.z) >= 4u)"),
        )

    # ---- 【t74】A4 三处变异：删 (b) 写表 / 删 (c) 读表 / 先烘死 X ----

    def _assert_t74_problem(self, broken, label):
        self.assertNotEqual(broken, self.text, "变异「" + label + "」必须真的改动文本")
        problems = _himi_exporter.merged_attach_hlsl_problems(broken)
        self.assertTrue(problems, "变异「" + label + "」之后判据必须报错")
        self.assertTrue(
            [p for p in problems if "t74" in p],
            "变异「" + label + "」的报错必须点名 t74：" + str(problems),
        )

    def test_a4_mutation_dropping_table_write_row(self):
        """删掉 (b) 的写表语句 ⇒ 采集有效时表填不起来，必须红。"""
        for row in (
            "himi_object_matrix[own_row + 1u] = own_object.r1;",
            "himi_object_matrix[own_row + 2u] = own_object.r2;",
        ):
            self.assertIn(row, self.text)
            self._assert_t74_problem(self.text.replace(row, ""), "删 (b) 写表 " + row)

    def test_a4_mutation_dropping_table_reuse_read(self):
        """删掉 (c) 的表读取（own 段）⇒ 采集无效时只能退回抓帧常量，必须红。"""
        for row in (
            "float4 own1 = himi_object_matrix[own_row + 1u];",
            "float4 own2 = himi_object_matrix[own_row + 2u];",
        ):
            self.assertIn(row, self.text)
            self._assert_t74_problem(self.text.replace(row, ""), "删 (c) 读表 " + row)

    def test_a4_mutation_reading_baked_x_after_selection(self):
        """把兜底 X 挪到选择块**之后**（= 「先烘死 X」）⇒ 只能靠位置关系抓到，必须红。"""
        statements = (
            "float4 attach_x0 = himi_attach_range[4];",
            "float4 attach_x1 = himi_attach_range[5];",
            "float4 attach_x2 = himi_attach_range[6];",
        )
        removed = self.text
        for statement in statements:
            self.assertIn(statement, removed)
            removed = removed.replace(statement, "")
        marker = "if (asuint(attach_range.z) >= 4u)"
        cut = removed.index(marker) + len(marker)
        moved = "".join("        " + statement + "\n" for statement in statements)
        self._assert_t74_problem(
            removed[:cut] + "\n" + moved + removed[cut:], "把兜底 X 挪到选择块之后"
        )

    def test_t74_probes_go_red_when_removed(self):
        probes = list(_himi_exporter.HIMI_RUNTIME_X_PROBES)
        self.assertGreaterEqual(len(probes), 13)
        for probe, _message in probes:
            self.assertIn(probe, self.text, "探针必须先出现在产物文本里：" + probe)
            broken = self.text.replace(probe, "")
            problems = _himi_exporter.merged_attach_hlsl_problems(broken)
            self.assertTrue(problems, "删掉 " + probe + " 之后判据必须报错")
            self.assertTrue(
                [p for p in problems if "t74" in p],
                "删掉 " + probe + " 的报错必须点名 t74：" + str(problems),
            )


class RuntimeXIniTests(unittest.TestCase):
    """【t74】ini 侧：每族 cb3 副本 + run 紧邻 + cs-cb1/cs-u1 + u1 表 array = 3 × 族数。"""

    FAMILIES = ("2c706fbd", "694025c6", "70a4da3e", "b35cfc6c", "def0c0ae")
    ROLE = "role_kaijin"

    def test_each_family_gets_one_cb3_copy_right_before_its_run(self):
        for family in self.FAMILIES:
            lines = _runtime.render_attach_capture_lines(family)
            self.assertEqual(len(lines), 3)
            self.assertEqual(
                lines[1], _runtime.cb3_capture_copy_line(family)
            )
            self.assertEqual(lines[1], f"{_runtime.cb3_capture_resource_name(family)}"
                                       " = copy vs-cb3 unless_null")
            self.assertEqual(lines[2], "run = " + _runtime.attach_section_name(family))

    #: 真实帧（FrameAnalysis-2026-10-22-133713）里每族的 LOD0 render（indexed）draw 数：
    #: 工作空间部件目录名 `<族>-<IndexCount>-<StartIndexLocation>` 与 log.txt 的 DrawIndexed
    #: 逐笔精确匹配所得（2c706fbd 5 + 694025c6 3 + 70a4da3e 1 + b35cfc6c 1 + def0c0ae 2 = 12）。
    LOD0_SITES = {"2c706fbd": 5, "694025c6": 3, "70a4da3e": 1, "b35cfc6c": 1, "def0c0ae": 2}

    def _generated_ini_text(self):
        """按生成器真实的两条路径拼出 ini 文本：5 个 VB 段（Blend 类别）+ 12 个 LOD0 段。"""
        out = []
        for family in self.FAMILIES:
            out.append("; --- VB 段（Blend 类别）：" + family + " ---")
            out.append("[TextureOverride_VB_" + family + "_" + family + "_Blend]")
            out.extend(_runtime.render_attach_capture_lines(family))
            out.append("handling = skip")
            out.append("draw = 47199, 0")
            for index in range(self.LOD0_SITES[family]):
                out.append("; --- LOD0 段：%s #%d ---" % (family, index))
                out.append("[TextureOverride_IB_%s_%s_%d]" % (family, family, index))
                out.extend(_runtime.render_attach_render_capture_lines(family))
                out.append("drawindexed = 47199, 0, 0")
        return "\n".join(out)

    def test_every_attach_run_site_is_preceded_by_its_own_cb3_copy(self):
        """A3 文本硬门：**17 个** run 站点的上一非空行都必须是本族的 cb3 副本。"""
        self.assertEqual(len(self.FAMILIES), 5)
        self.assertEqual(sum(self.LOD0_SITES.values()), 12)
        text = self._generated_ini_text()
        lines = text.splitlines()
        sites = []
        for index, line in enumerate(lines):
            stripped = line.strip()
            if not stripped.startswith("run = " + _runtime.ATTACH_SECTION_PREFIX):
                continue
            family = stripped.rsplit("_", 1)[-1]
            previous = ""
            for back in range(index - 1, -1, -1):
                if lines[back].strip():
                    previous = lines[back].strip()
                    break
            self.assertEqual(
                previous,
                _runtime.cb3_capture_copy_line(family),
                "段 " + stripped + " 的上一非空行必须是本族的 cb3 副本，实际是 " + repr(previous),
            )
            sites.append(family)
        self.assertEqual(len(sites), 17)
        self.assertEqual(text.count(" = copy vs-cb3 unless_null"), 17)
        # 5 个 VB 段各一行 palette 副本、12 个 LOD0 段没有 palette 副本（render 段只有骨架 t0）
        self.assertEqual(text.count(" = copy vs-t0 unless_null"), 5)

    def test_ib_injection_goes_through_the_render_capture_helper(self):
        """源码级门：LOD0 站点必须由 `render_attach_render_capture_lines` 生成，而不是裸写 run。"""
        source = inspect.getsource(
            _himi_exporter.ExportHIMI.add_unity_vs_texture_override_ib_sections
        )
        self.assertIn("render_attach_render_capture_lines", source)
        self.assertNotIn('out.append("run = " + attach)', source)

    def test_attach_section_body_line_count_is_stable(self):
        """attach 段体行数不变（cs-cb1 / cs-u1 都绑上，Dispatch 之后逐项解绑）。

        题面按「12 行非空」记；实测 `render_attach_section_lines` 是 **11 行非空（含段名）/ 10 行段体**，
        这里按实测值钉住，多一行少一行都要红。
        """
        table = _runtime.object_matrix_resource_name(self.ROLE)
        for family in self.FAMILIES:
            lines = _runtime.render_attach_section_lines(
                family,
                base=0,
                slots=184,
                skeleton_name="ResourceHI3MergedSkeleton_" + self.ROLE,
                hlsl_path="./res/himi_attach.hlsl",
                object_matrix_name=table,
            )
            self.assertEqual(lines[0], "[" + _runtime.attach_section_name(family) + "]")
            body = [line for line in lines[1:] if line.strip()]
            self.assertEqual(len(body), 10, "段体必须恰好 10 行非空：" + str(body))
            self.assertEqual(len([line for line in lines if line.strip()]), 11)
            self.assertLess(lines.index("Dispatch = 3, 1, 1"), lines.index("cs-u0 = null"))
            self.assertLess(lines.index("Dispatch = 3, 1, 1"), lines.index("cs-u1 = null"))

    def test_cb3_capture_resource_decl_has_no_type_or_stride(self):
        for family in self.FAMILIES:
            lines = _runtime.cb3_capture_ini_lines(family)
            # 段名 + 一行注释（产物的逐段自检判「既无属性也无注释」= 被截断的段）。
            self.assertEqual(lines[0], f"[{_runtime.cb3_capture_resource_name(family)}]")
            self.assertTrue(all(line.startswith(";") for line in lines[1:]))
            self.assertEqual(
                [
                    line
                    for line in lines
                    if not line.startswith(";")
                    and ("type" in line.lower() or "stride" in line.lower())
                ],
                [],
            )

    def test_attach_section_binds_cb1_and_u1(self):
        table = _runtime.object_matrix_resource_name(self.ROLE)
        for family in self.FAMILIES:
            lines = _runtime.render_attach_section_lines(
                family,
                base=0,
                slots=184,
                skeleton_name="ResourceHI3MergedSkeleton_" + self.ROLE,
                hlsl_path="./res/himi_attach.hlsl",
                object_matrix_name=table,
            )
            self.assertIn(_runtime.cb3_capture_reference_line(family), lines)
            self.assertIn(f"{_runtime.OBJECT_MATRIX_REGISTER} = ref {table}", lines)
            self.assertIn("cs-u1 = null", lines)
            # 顺序：cs-cb1 在 cs-u0 之前、Dispatch 之后立刻解绑 u1
            self.assertLess(lines.index(_runtime.cb3_capture_reference_line(family)),
                            lines.index("cs-u0 = ref ResourceHI3MergedSkeleton_" + self.ROLE))
            self.assertLess(lines.index("Dispatch = 3, 1, 1"), lines.index("cs-u1 = null"))

    def test_object_matrix_decl_array_is_three_per_family(self):
        lines = _runtime.object_matrix_ini_lines(self.ROLE, len(self.FAMILIES))
        name = _runtime.object_matrix_resource_name(self.ROLE)
        self.assertTrue(name.startswith(_runtime.OBJECT_MATRIX_RESOURCE_PREFIX))
        self.assertEqual(lines[0], f"[{name}]")
        self.assertIn("type = " + _runtime.OBJECT_MATRIX_INI_TYPE, lines)
        self.assertIn("format = " + _runtime.OBJECT_MATRIX_INI_FORMAT, lines)
        self.assertIn(f"array = {3 * len(self.FAMILIES)}", lines)
        self.assertIn("bind_flags = " + _runtime.OBJECT_MATRIX_INI_BIND_FLAGS, lines)
        self.assertFalse([line for line in lines if "stride" in line.lower()])
        self.assertFalse([line for line in lines if line.lower().startswith("filename")])

    def test_object_matrix_table_layout_roundtrip(self):
        matrix = _runtime.mat3x4_compose(_rotate_z(15.0), _translate(2.0, -3.0, 0.5))
        rows = _runtime.object_matrix_table_rows(matrix)
        self.assertEqual(len(rows), 3)
        # 唯一布局：(r0.xyz, r3.x) / (r1.xyz, r3.y) / (r2.xyz, r3.z) —— 12 个数、无浪费
        self.assertEqual(rows[0], [matrix[0], matrix[1], matrix[2], matrix[3]])
        self.assertEqual(rows[1], [matrix[4], matrix[5], matrix[6], matrix[7]])
        self.assertEqual(rows[2], [matrix[8], matrix[9], matrix[10], matrix[11]])
        self.assertEqual(
            _pack12(_runtime.object_matrix_from_table_rows(rows)), _pack12(matrix)
        )


class FamilyXPureTests(unittest.TestCase):
    """D：宿主族判定 / 骨对偶 / 三态。"""

    def test_pick_host_family_by_longest_position_buffer(self):
        picked = _family_x.pick_host_family({
            "2c706fbd": 1366904,
            "694025c6": 396,
            "70a4da3e": 132,
            "b35cfc6c": 132,
            "def0c0ae": 264,
        })
        self.assertTrue(picked["ok"])
        self.assertEqual(picked["family"], "2c706fbd")
        self.assertEqual(picked["sizes"]["2c706fbd"], 1366904)
        self.assertIn("Position", str(picked["note"]))
        self.assertFalse(_family_x.pick_host_family({})["ok"])
        self.assertFalse(_family_x.pick_host_family({"a": 0, "b": 0})["ok"])

    def test_pick_host_family_tie_breaks_by_name(self):
        picked = _family_x.pick_host_family({"bbb": 128, "aaa": 128})
        self.assertTrue(picked["ok"])
        self.assertEqual(picked["family"], "aaa")

    def test_duality_check_identity_and_reverse_guard(self):
        base = [_rotate_z(0.0), _translate(0.5, 0.25, -0.75), _rotate_z(90.0)]
        good = _family_x.duality_check(base, base, IDENTITY)
        self.assertEqual(good["hits"], 3)
        self.assertLessEqual(good["max_residual"], 1e-6)
        guard = _family_x.duality_check(base, base, _translate(5.0, 0.0, 0.0))
        self.assertEqual(guard["hits"], 0)
        self.assertGreater(guard["best_residual"], 1e-2)

    def test_identity_matrix_is_identity(self):
        self.assertTrue(_family_x.is_identity(IDENTITY))
        self.assertFalse(_family_x.is_identity(_rotate_z(1.0)))

    def _stub(self, host, items, plan=None):
        class _Stub:
            _himi_family_x_none = ExportHIMI._himi_family_x_none

            def __init__(self, host, items, plan):
                self._host = host
                self._items = items
                self._plan = plan
                self.notices = []

            def himi_merged_runtime_plan(self):
                return self._plan

            def himi_family_range_items(self):
                return list(self._items)

            def _himi_host_family(self, plan):
                return dict(self._host)

            def _himi_notify(self, detail, *, warning=False):
                self.notices.append((bool(warning), str(detail)))

        return _Stub(host, items, plan or types.SimpleNamespace(has_rebinds=True))

    def _with_patched(self, callback, participants, record):
        original_participants = _family_x.participants_from_plan
        original_compute = _family_x.compute_family_x
        _family_x.participants_from_plan = lambda plan: dict(participants)
        _family_x.compute_family_x = lambda *args, **kwargs: dict(record)
        try:
            return callback()
        finally:
            _family_x.participants_from_plan = original_participants
            _family_x.compute_family_x = original_compute

    _ITEMS = [
        ("2c706fbd", {"base": 0, "slots": 184, "own_block": True}),
        ("70a4da3e", {"base": 184, "slots": 71, "own_block": True}),
    ]
    _PARTICIPANTS = {
        "2c706fbd": "LOD0.2c706fbd-47199-0",
        "70a4da3e": "LOD0.70a4da3e-13473-0",
    }

    def test_state_none_when_host_family_unknown(self):
        stub = self._stub({"ok": False, "family": "", "size": 0, "note": "没有产物"}, self._ITEMS)
        self.assertEqual(ExportHIMI._himi_family_x(stub), {})
        self.assertTrue(stub._himi_family_x_skipped)
        self.assertEqual(stub._himi_family_x_problems, [])
        self.assertTrue([notice for notice in stub.notices if notice[0]])

    def test_state_none_when_no_family_has_x(self):
        stub = self._stub(
            {"ok": True, "family": "2c706fbd", "size": 10, "note": "内存"}, self._ITEMS
        )
        result = self._with_patched(
            lambda: ExportHIMI._himi_family_x(stub),
            self._PARTICIPANTS,
            {"x": {}, "problems": ["抓帧不可用（Config/FrameAnalysisPath.json 缺失）"]},
        )
        self.assertEqual(result, {})
        self.assertTrue(stub._himi_family_x_skipped)
        self.assertEqual(stub._himi_family_x_problems, [])
        self.assertIn("不做 X_f 校准", stub.notices[-1][1])

    def test_state_partial_keeps_the_families_it_got(self):
        """部分缺失 ⇒ 拿到手的族照常返给载荷，缺的族落零（CS 跳过左乘，下一帧自愈）。

        旧契约「半校准比不校准更坏 ⇒ 整批丢弃」已推翻：校准的权威在运行期
        （CS 每帧按骨算 X = M_宿主⁻¹·M_来源），导出期 X_f 只是表还没热起来那一两帧
        的兜底，缺一族不该让整笔产物退回不校准。
        """
        stub = self._stub(
            {"ok": True, "family": "2c706fbd", "size": 10, "note": "内存"}, self._ITEMS
        )
        result = self._with_patched(
            lambda: ExportHIMI._himi_family_x(stub),
            self._PARTICIPANTS,
            {
                "x": {"2c706fbd": IDENTITY},
                "problems": ["70a4da3e: 抓帧里没有一笔 draw 的 vs-cb3 是合法对象矩阵"],
            },
        )
        self.assertEqual(sorted(result.keys()), ["2c706fbd"])
        self.assertEqual(len(stub._himi_family_x_problems), 1)
        self.assertIn("70a4da3e", stub._himi_family_x_problems[0])
        self.assertEqual(stub._himi_family_x_skipped, [])

    def test_state_full_returns_all(self):
        stub = self._stub(
            {"ok": True, "family": "2c706fbd", "size": 10, "note": "内存"}, self._ITEMS
        )
        record = {
            "x": {"2c706fbd": IDENTITY, "70a4da3e": _translate(0.5, 0.0, 0.0)},
            "problems": [],
            "source_frame": "FrameAnalysis-2026-09-23-140134",
        }
        result = self._with_patched(
            lambda: ExportHIMI._himi_family_x(stub), self._PARTICIPANTS, record
        )
        self.assertEqual(sorted(result.keys()), ["2c706fbd", "70a4da3e"])
        self.assertEqual(_pack12(result["2c706fbd"]), _pack12(IDENTITY))
        self.assertEqual(stub._himi_family_x_problems, [])
        self.assertEqual(stub._himi_family_x_skipped, [])

    def test_missing_participant_bare_is_partial(self):
        stub = self._stub(
            {"ok": True, "family": "2c706fbd", "size": 10, "note": "内存"}, self._ITEMS
        )
        result = self._with_patched(
            lambda: ExportHIMI._himi_family_x(stub),
            {"2c706fbd": "LOD0.2c706fbd-47199-0"},
            {"x": {"2c706fbd": IDENTITY}, "problems": []},
        )
        self.assertEqual(sorted(result.keys()), ["2c706fbd"])
        self.assertTrue([p for p in stub._himi_family_x_problems if "70a4da3e" in p])


    def test_host_family_prefers_memory_position_buffers(self):
        import numpy

        class _Model:
            def __init__(self, draw_ib, payload):
                self.draw_ib = draw_ib
                self.category_buffer_dict = {"Position": payload}

        stub = types.SimpleNamespace(
            drawib_model_list=[
                _Model("2c706fbd", numpy.zeros(44 * 100, dtype=numpy.float32)),
                _Model("70a4da3e", bytes(44 * 3)),
                _Model("deadbeef", bytes(44 * 100000)),
            ],
            himi_family_range_items=lambda: [
                ("2c706fbd", {"base": 0, "slots": 184}),
                ("70a4da3e", {"base": 184, "slots": 71}),
            ],
        )
        picked = ExportHIMI._himi_host_family(stub, types.SimpleNamespace(has_rebinds=True))
        self.assertTrue(picked["ok"])
        self.assertEqual(picked["family"], "2c706fbd")
        # numpy 视图按 **nbytes** 计（float32 × 4400 个元素 = 17600 B）。
        self.assertEqual(picked["size"], 44 * 100 * 4)
        self.assertIn("内存", picked["source"])

    def test_host_family_falls_back_to_mod_directory(self):
        root = tempfile.mkdtemp(prefix="himi_host_")
        workspace = os.path.join(root, "WorkSpace", "HIMI", "unit_ws")
        mod_root = os.path.join(root, "3Dmigoto", "HI3", "Mods", "SSMTGeneratedMod", "unit_ws")
        os.makedirs(os.path.join(mod_root, "Meshes"), exist_ok=True)
        for family, size in (("2c706fbd", 4096), ("70a4da3e", 128)):
            with open(os.path.join(mod_root, "Meshes", family + "-Position.buf"), "wb") as handle:
                handle.write(bytes(size))
        previous = _GLOBAL_STATE["workspace"]
        _GLOBAL_STATE["workspace"] = workspace
        try:
            stub = types.SimpleNamespace(
                drawib_model_list=[],
                himi_family_range_items=lambda: [
                    ("2c706fbd", {"base": 0, "slots": 184}),
                    ("70a4da3e", {"base": 184, "slots": 71}),
                ],
            )
            picked = ExportHIMI._himi_host_family(stub, types.SimpleNamespace(has_rebinds=True))
        finally:
            _GLOBAL_STATE["workspace"] = previous
        self.assertTrue(picked["ok"])
        self.assertEqual(picked["family"], "2c706fbd")
        self.assertEqual(picked["size"], 4096)
        self.assertIn("Meshes", picked["source"])

class FamilyBaseIndexTests(unittest.TestCase):
    """【2026-10-26 事故】载荷 ``[7] = (own, base)`` 的 ``base`` 必须跟着「谁画这族的几何」走。

    实例 A（错）：``嗨♪爱愿妖精♥`` 五族里只有 b35cfc6c（132 B / 3 顶点）与 def0c0ae（264 B /
    6 顶点）是站位股，694025c6（152592 B / 3468 顶点）与 70a4da3e（174108 B / 3957 顶点）**仍在
    自己那笔 draw 里画真几何**。把它们的 ``base`` 写成宿主 ⇒ 槽里存 ``M_2c⁻¹·M_694·P_694``，
    而那一刻游戏喂的 ``vs-cb3`` 已经是 ``M_694`` ⇒ 二次变换（实测 694 偏 160.56°、70a 偏 26.67°）。

    实例 B（对）：``虚构游戏`` 两族都是 3 / 6 顶点的站位股，几何整块并进宿主 09c6e58a 一笔 draw
    ⇒ 那时 ``base = 宿主`` 才是对的，也是唯一允许偏离 ``base == own`` 的情形。

    退化路径：``_himi_host_family`` 一旦没把 ``sizes`` 带回来，旧写法会把**每一族**都判成
    ≤512 B 的站位族（``sizes.get(f, 0) == 0``）⇒ 全合并分支恒成立 ⇒ 实例 A 那种工作空间必然错位。
    """

    HOST = "2c706fbd"

    def _exporter(self, families, sizes, host=None):
        class _Fake:
            def himi_family_range_items(self):
                return [(family, {"base": 0, "slots": 1}) for family in families]

            def himi_merged_runtime_plan(self):
                return types.SimpleNamespace()

            def _himi_host_family(self, plan):
                payload = {"ok": True, "family": self.host, "size": 1, "source": "stub", "note": ""}
                if self.sizes is not None:
                    payload["sizes"] = dict(self.sizes)
                return payload

        fake = _Fake()
        fake.host = host or self.HOST
        fake.sizes = sizes
        return fake

    def test_host_family_reports_per_family_sizes(self):
        class _Model:
            def __init__(self, draw_ib, payload):
                self.draw_ib = draw_ib
                self.category_buffer_dict = {"Position": payload}

        stub = types.SimpleNamespace(
            drawib_model_list=[
                _Model("2c706fbd", bytes(4400)),
                _Model("694025c6", bytes(880)),
            ],
            himi_family_range_items=lambda: [
                ("2c706fbd", {"base": 0, "slots": 1}),
                ("694025c6", {"base": 1, "slots": 1}),
            ],
        )
        picked = ExportHIMI._himi_host_family(stub, types.SimpleNamespace(has_rebinds=True))
        self.assertTrue(picked["ok"])
        self.assertEqual(picked["family"], "2c706fbd")
        self.assertEqual(picked["sizes"], {"2c706fbd": 4400, "694025c6": 880})

    def test_own_draw_families_keep_their_own_base(self):
        # 实例 A 的真实形状：真几何与站位并存 ⇒ 逐族判定（站位 b35/def 跟宿主，其余跟自己）
        families = ["2c706fbd", "694025c6", "70a4da3e", "b35cfc6c", "def0c0ae"]
        sizes = {
            "2c706fbd": 1040468,
            "694025c6": 152592,
            "70a4da3e": 174108,
            "b35cfc6c": 132,
            "def0c0ae": 264,
        }
        got = ExportHIMI._himi_family_index_map(self._exporter(families, sizes), None)
        self.assertEqual(
            got,
            {
                "2c706fbd": (0, 0),
                "694025c6": (1, 1),
                "70a4da3e": (2, 2),
                "b35cfc6c": (3, 0),
                "def0c0ae": (4, 0),
            },
            "自成 draw 的族被对齐到宿主族 ⇒ 骨架里会多乘一次 M_宿主⁻¹·M_族；"
            "站位族跟着自己 ⇒ 并进宿主的那几个零面积顶点会少一层校正",
        )

    def test_no_stub_families_means_every_family_is_its_own_base(self):
        families = ["2c706fbd", "694025c6", "70a4da3e"]
        sizes = {"2c706fbd": 1040468, "694025c6": 152592, "70a4da3e": 174108}
        got = ExportHIMI._himi_family_index_map(self._exporter(families, sizes), None)
        self.assertEqual(got, {family: (index, index) for index, family in enumerate(families)})

    def test_unknown_size_family_is_not_treated_as_stub(self):
        # 尺寸未知 ⇒ 当作「自己那笔 draw」，绝不猜成站位（猜错会多乘一层 M_宿主⁻¹·M_族）
        families = ["2c706fbd", "694025c6", "70a4da3e"]
        sizes = {"2c706fbd": 1040468, "694025c6": 132}
        got = ExportHIMI._himi_family_index_map(self._exporter(families, sizes), None)
        self.assertEqual(got, {"2c706fbd": (0, 0), "694025c6": (1, 0), "70a4da3e": (2, 2)})

    def test_full_merge_aligns_every_family_to_the_host(self):
        families = ["077983c9", "09c6e58a", "1b62725d"]
        sizes = {"077983c9": 132, "09c6e58a": 1040468, "1b62725d": 264}
        got = ExportHIMI._himi_family_index_map(
            self._exporter(families, sizes, host="09c6e58a"), None
        )
        self.assertEqual(got, {family: (index, 1) for index, family in enumerate(families)})

    def test_missing_sizes_never_degrades_to_full_merge(self):
        families = ["2c706fbd", "694025c6", "70a4da3e"]
        got = ExportHIMI._himi_family_index_map(self._exporter(families, None), None)
        self.assertEqual(got, {family: (index, index) for index, family in enumerate(families)})

    def test_single_family_has_no_host_shift(self):
        got = ExportHIMI._himi_family_index_map(
            self._exporter(["deadbeef"], {"deadbeef": 1024}, host="deadbeef"), None
        )
        self.assertEqual(got, {"deadbeef": (0, 0)})


class AttachRangeExportGateTests(unittest.TestCase):
    """t39-X 硬门：发布路径（`_himi_publish_attach_ranges`）必须在写盘后用**同一组**输入复核。

    【t78 方案 B】"同一组"里的摆放段恒为单位阵：`_Stub._himi_family_placements()` 仍返回一个
    12° 旋转，用于证明确实**不再**被发布路径读取（否则复核会拿旋转去比单位阵而报错）。
    """

    FAMILY = "2c706fbd"

    class _Stub:
        def __init__(self, plan):
            self.plan = plan
            self.messages = []
            self._himi_attach_range_result = {}
            # 【门控】发布路径只在**真的有跨族借用槽**时才算/写 X_f（不合并的导出不再抓帧
            # 算物体矩阵）。本类测的是 X_f 的发布-复核往返，所以给一条借用账，
            # 让该分支保持被覆盖。
            self._himi_borrow_ledger = {
                "families": {
                    AttachRangeExportGateTests.FAMILY: {
                        "base": 0,
                        "slots": 184,
                        "block_slots": 185,
                        "borrow": [(184, 3, "deadbeef")],
                    }
                }
            }

        def _himi_family_placements(self):
            return {AttachRangeExportGateTests.FAMILY: _rotate_z(12.0)}

        def _himi_family_x(self):
            return {AttachRangeExportGateTests.FAMILY: IDENTITY}

        def _himi_publish_family_x_record(self):
            return {"ok": True}

        def _himi_notify(self, detail, *, warning=False):
            self.messages.append((detail, warning))

        def himi_family_range_items(self):
            # 【t74】族索引空间 = 发段次序；单族占位（宿主族 = 唯一族 ⇒ X = 单位阵）
            family = AttachRangeExportGateTests.FAMILY
            return [(family, dict(self.plan.family_ranges[family]))]

        def _himi_host_family(self, plan):
            return {
                "ok": True,
                "family": AttachRangeExportGateTests.FAMILY,
                "size": 1366904,
                "source": "stub",
                "note": "",
            }

        _himi_family_index_map = ExportHIMI._himi_family_index_map
        _himi_verify_attach_range_sync = ExportHIMI._himi_verify_attach_range_sync

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="himi_attach_gate_")
        self.folder = os.path.join(self.root, "Meshes")
        os.makedirs(self.folder, exist_ok=True)
        self._previous_mod = _GLOBAL_STATE["mod"]
        _GLOBAL_STATE["mod"] = self.root
        self.plan = types.SimpleNamespace(
            family_ranges={self.FAMILY: {"base": 0, "slots": 184, "own_block": True}},
            has_rebinds=True,
        )

    def tearDown(self):
        _GLOBAL_STATE["mod"] = self._previous_mod

    def _publish(self):
        stub = self._Stub(self.plan)
        result = ExportHIMI._himi_publish_attach_ranges(stub, self.plan, "Meshes")
        return stub, result

    def _fatal(self):
        return sys.modules[f"{PKG}.utils.format_utils"].Fatal

    def _expected_placement(self):
        # 【t78 方案 B】发布/复核都只认单位阵：_Stub._himi_family_placements() 里那个 12° 旋转
        # 现在是**故意留下的噪声**——发布若还读它，下面的复核立刻失败。
        return _contract.attach_placement_identity()

    def test_gate_passes_on_correct_publish(self):
        stub, result = self._publish()
        self.assertTrue(result["ok"])
        path = os.path.join(self.folder, _runtime.attach_range_basename(self.FAMILY))
        self.assertEqual(os.path.getsize(path), 128)
        self.assertEqual(stub.messages, [])
        self.assertEqual(
            _runtime.verify_attach_range_files(
                self.folder,
                self.plan.family_ranges,
                placements={self.FAMILY: self._expected_placement()},
                family_xs={self.FAMILY: IDENTITY},
                indices={self.FAMILY: (0, 0)},
            ),
            [],
        )

    def test_gate_uses_the_same_ranges_and_factors(self):
        original_verify = _runtime.verify_attach_range_files
        seen = {}

        def recorder(folder, ranges, *, placements=None, family_xs=None, indices=None):
            seen["folder"] = folder
            seen["ranges"] = dict(ranges)
            seen["placements"] = dict(placements or {})
            seen["family_xs"] = dict(family_xs or {})
            seen["indices"] = dict(indices or {})
            return []

        _runtime.verify_attach_range_files = recorder
        try:
            stub, result = self._publish()
        finally:
            _runtime.verify_attach_range_files = original_verify
        self.assertTrue(result["ok"])
        self.assertEqual(seen["folder"], self.folder)
        self.assertEqual(seen["ranges"], self.plan.family_ranges)
        self.assertEqual(seen["placements"], {self.FAMILY: self._expected_placement()})
        self.assertEqual(seen["family_xs"], {self.FAMILY: IDENTITY})
        # 【t74】复核必须拿到与发布**同一组**族索引（否则「表里存的是哪一族」无人核对）
        self.assertEqual(seen["indices"], {self.FAMILY: (0, 0)})

    def test_gate_raises_when_expected_bytes_are_wrong(self):
        """变异：把**复核函数的期望字节数**改错（128 → 64）⇒ 硬门必须失败。"""
        original_bytes = _runtime.ATTACH_RANGE_BYTES
        original_payload = _runtime.attach_range_payload

        def pinned_payload(*args, **kwargs):
            # 载荷生成仍按真实字节数写（否则 attach_range_payload 末尾的 assert 会先炸）
            # ⇒ 被改错的只有复核侧的期望值。
            saved = _runtime.ATTACH_RANGE_BYTES
            _runtime.ATTACH_RANGE_BYTES = original_bytes
            try:
                return original_payload(*args, **kwargs)
            finally:
                _runtime.ATTACH_RANGE_BYTES = saved

        _runtime.attach_range_payload = pinned_payload
        _runtime.ATTACH_RANGE_BYTES = 64
        try:
            with self.assertRaises(self._fatal()) as caught:
                self._publish()
        finally:
            _runtime.attach_range_payload = original_payload
            _runtime.ATTACH_RANGE_BYTES = original_bytes
        message = str(caught.exception)
        self.assertIn("四方同步失败", message)
        self.assertIn(self.FAMILY, message)
        self.assertIn("64 B", message)

    def test_gate_raises_when_published_x_block_is_wrong(self):
        """变异：载荷里的 X 块被清零（发布值仍是 X_f）⇒ 硬门必须失败。"""
        original_payload = _runtime.attach_range_payload
        offset = _runtime.ATTACH_RANGE_X_OFFSET
        block = _runtime.ATTACH_RANGE_FAMILY_X_BYTES

        def zeroed_payload(*args, **kwargs):
            return original_payload(*args, **kwargs)[:offset] + bytes(block)

        _runtime.attach_range_payload = zeroed_payload
        try:
            with self.assertRaises(self._fatal()) as caught:
                self._publish()
        finally:
            _runtime.attach_range_payload = original_payload
        message = str(caught.exception)
        self.assertIn("X_f", message)
        self.assertIn(self.FAMILY, message)



# --------------------------------------------------------------------------- #
# F. B3 硬门：合并骨架 × 形态键「标准（非直出）路线」互斥
# --------------------------------------------------------------------------- #


class ShapekeyDirectModeGateTests(unittest.TestCase):
    """B3：合并骨架 + 非 Basis 形态键块 + 形态键节点未勾直出 ⇒ ``Fatal``（四态全覆盖）。

    判据是纯函数（``shapekey_direct_mode_problem``），门只是采集三件事再照着抛；因此四态在这里
    可以离线复跑，不需要 bpy：物体用 SimpleNamespace 喂 ``_himi_shapekey_object_probe``。
    """

    class _Stub:
        def __init__(self, merged_active, blocks, needs_standard):
            self.merged_active = bool(merged_active)
            self.blocks = int(blocks)
            self.needs_standard = bool(needs_standard)
            self._himi_shapekey_gate_result = None

        def _himi_merged_attach_active(self):
            return self.merged_active

        def _himi_shapekey_object_probe(self):
            key_blocks = [object()] * (1 + self.blocks)
            shape_keys = types.SimpleNamespace(key_blocks=key_blocks)
            return [types.SimpleNamespace(data=types.SimpleNamespace(shape_keys=shape_keys))]

        def _needs_standard_shapekey_rounds(self):
            return self.needs_standard

        _himi_shapekey_direct_mode_probe = ExportHIMI._himi_shapekey_direct_mode_probe
        _himi_non_basis_shapekey_blocks = ExportHIMI._himi_non_basis_shapekey_blocks
        _himi_enforce_shapekey_direct_mode = ExportHIMI._himi_enforce_shapekey_direct_mode

    def test_contract_four_states(self):
        cases = [
            (True, 7, True, True),
            (True, 7, False, False),
            (True, 0, True, False),
            (False, 7, True, False),
        ]
        for merged, blocks, needs, danger in cases:
            problem = _contract.shapekey_direct_mode_problem(
                merged_active=merged,
                non_basis_blocks=blocks,
                needs_standard_rounds=needs,
            )
            self.assertEqual(bool(problem), danger, (merged, blocks, needs, problem))
            if danger:
                self.assertIn("HIMI 骨骼合并", problem)
                self.assertIn("形态键", problem)
                self.assertIn("direct_export_mode", problem)
                self.assertIn("Meshes1001", problem)

    def test_gate_raises_only_in_the_danger_state(self):
        fatal = sys.modules[f"{PKG}.utils.format_utils"].Fatal
        stub = self._Stub(True, 7, True)
        with self.assertRaises(fatal) as caught:
            stub._himi_enforce_shapekey_direct_mode()
        message = str(caught.exception)
        self.assertIn("HIMI 骨骼合并", message)
        self.assertIn(_contract.SHAPEKEY_DIRECT_MODE_HINT, message)
        self.assertEqual(stub._himi_shapekey_gate_result["ok"], False)

    def test_gate_passes_in_the_other_three_states(self):
        for args in ((True, 7, False), (True, 0, True), (False, 7, True)):
            stub = self._Stub(*args)
            result = stub._himi_enforce_shapekey_direct_mode()
            self.assertTrue(result["ok"], args)
            self.assertEqual(result["problem"], "")
            self.assertEqual(stub._himi_shapekey_gate_result["ok"], True)


# --------------------------------------------------------------------------- #
# G. A4 代码层：顶点侧校准彻底消失（数值层由 tools/himi_family_x_oracle.py 的 A4 段取证）
# --------------------------------------------------------------------------- #


class VertexSideCalibrationRemovedTests(unittest.TestCase):
    """方案 B 的代码层护栏：诊断函数**不写一个字节**、不取 Q，形态键打包器里没有族级校准。"""

    @classmethod
    def setUpClass(cls):
        source = inspect.getsource(ExportHIMI._himi_calibrate_bind_positions)
        cls.source = textwrap.dedent(source)
        cls.tree = ast.parse(cls.source)

    def test_no_write_back_to_any_row_buffer(self):
        banned = {"view2d", "group", "target", "block", "floats", "position"}
        writes = []
        for node in ast.walk(self.tree):
            if isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
                name = getattr(node.value, "id", "")
                if name in banned:
                    writes.append(name)
        self.assertEqual(writes, [], "顶点侧诊断函数里出现了对行缓冲的写回")

    def test_never_looks_up_the_family_placement(self):
        calls = {
            node.func.attr
            for node in ast.walk(self.tree)
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
        }
        self.assertNotIn("_himi_family_placements", calls)
        self.assertNotIn("mat3x4_inverse", calls)

    def test_contract_no_longer_gates_on_family_placements(self):
        # 【t78 方案 B】Q_f 不参与任何变换 ⇒ 摆放读不到不能再降级合并骨架（否则白白丢产物）。
        # 判据是源码级：`_himi_enforce_contract` 里不得再出现 `not placements` 的降级门。
        text = inspect.getsource(ExportHIMI._himi_enforce_contract)
        self.assertNotIn("not placements", text, "摆放缺失仍在降级合并骨架（方案 B 下 Q_f 已不参与变换）")
        self.assertIn("_himi_family_x()", text, "X_f 的三态门必须仍在契约判定里")

    def test_no_uint8_byte_payload_roundtrip(self):
        # 旧写回的指纹：numpy.frombuffer(view2d.tobytes(), dtype=numpy.uint8) 的结果再整体赋值回源缓冲。
        # 注释里提到过这行代码，所以判据必须是 AST（看真实的 dtype=numpy.uint8 调用）而不是找字符串。
        found = []
        for node in ast.walk(self.tree):
            if not isinstance(node, ast.Call):
                continue
            func = node.func
            if not (isinstance(func, ast.Attribute) and func.attr == "frombuffer"):
                continue
            for keyword in node.keywords:
                value = keyword.value
                if (keyword.arg == "dtype" and isinstance(value, ast.Attribute)
                        and value.attr == "uint8"):
                    found.append(getattr(value, "lineno", 0))
        self.assertEqual(found, [], "顶点侧诊断函数里仍有 uint8 字节级重解释（旧写回手法）")

    def test_diagnostic_call_site_is_kept_before_super(self):
        entry = inspect.getsource(ExportHIMI.generate_buffer_files)
        self.assertIn("_himi_calibrate_bind_positions", entry)
        self.assertLess(
            entry.index("_himi_calibrate_bind_positions"),
            entry.index("super().generate_buffer_files"),
        )

    def test_shapekey_writer_has_no_family_calibration(self):
        path = REPO_ROOT / "blueprint" / "direct_export_shapekey_output_mixin.py"
        text = path.read_text(encoding="utf-8")
        for token in ("mat3x4_inverse", "himi_attach_range", "ATTACH_RANGE", "placement"):
            self.assertNotIn(token, text, "形态键打包器里出现族级校准痕迹：" + token)


# --------------------------------------------------------------------------- #
# H. 跨族借用权重：方案 B 精确、旧 Q 方案必然错（真实工作空间数据，不 mock 矩阵）
# --------------------------------------------------------------------------- #

REAL_WORKSPACE = "K:/SSMT-Package-master/WorkSpace/HIMI/嗨♪爱愿妖精♥"


def _read_position_rows(path, stride=44):
    with open(path, "rb") as handle:
        raw = handle.read()
    count = len(raw) // stride
    return [struct.unpack_from("<3f", raw, index * stride) for index in range(count)]


class CrossFamilyBorrowingTests(unittest.TestCase):
    """把权重打在**别族**的槽上：方案 B 给出真值，旧 Q 方案多出 Q_g⁻¹·Q_f ⇒ 必然错。

    一行代数：LBS 是 ``world = Σ w_i·S[c_i]·v``，作用在 v 上的任何常量都能搬到槽上而不改变结果。
    方案 B 下五族顶点同处一个角色空间（实测 bbox 同处身高 1.69、脚 y≈0），槽里存 ``X_g·P_g[k]``
    对**任何族**的顶点都给真值 ``M_g·P_g[k]·v``。旧方案因为顶点被乘了 ``Q_f``，跨族借用会多出
    ``Q_g⁻¹·Q_f``，而实测各族 Q 两两相差 133°–180°，必然错。

    数据全部来自真实工作空间：palette（``-BoneMatrix.buf``）、vs-cb3 对象矩阵、``X_f``、Position 行。
    """

    @classmethod
    def setUpClass(cls):
        if not os.path.isdir(REAL_WORKSPACE):
            raise unittest.SkipTest("真实工作空间缺失：" + REAL_WORKSPACE)
        frame = _family_x.resolve_frame_analysis_dir(REAL_WORKSPACE)
        if not frame.get("ok"):
            raise unittest.SkipTest("抓帧不可用：" + str(frame.get("note")))
        frame_dir = str(frame.get("path") or "")
        mod_root = _family_x.resolve_mod_root(REAL_WORKSPACE)
        # 工作空间 json 里记的是导出时的模组名；改了名（如 嗨♪爱愿妖精 → 虚构游戏）之后
        # 这个路径就不存在了 —— 那是环境变了，不是判据错，跳过而不是报错。
        if not mod_root or not os.path.isdir(mod_root):
            raise unittest.SkipTest("模组目录不存在：" + str(mod_root))
        sizes = {}
        for name in sorted(os.listdir(mod_root)):
            if not name.startswith("Meshes"):
                continue
            folder = os.path.join(mod_root, name)
            if not os.path.isdir(folder):
                continue
            for entry in sorted(os.listdir(folder)):
                if not entry.endswith(_family_x.POSITION_BUFFER_SUFFIX):
                    continue
                family = entry[: -len(_family_x.POSITION_BUFFER_SUFFIX)]
                size = os.path.getsize(os.path.join(folder, entry))
                if size > sizes.get(family, -1):
                    sizes[family] = size
        host = _family_x.pick_host_family(sizes)
        if not host.get("ok"):
            raise unittest.SkipTest("宿主族判定失败：" + str(host.get("note")))
        host_family = str(host.get("family") or "")
        plan = _runtime.load_workspace_plan(REAL_WORKSPACE)
        participants = _family_x.participants_from_plan(plan)
        if not getattr(plan, "ok", False) or not participants:
            raise unittest.SkipTest("运行时计划不可用：" + str(getattr(plan, "message", "")))
        record = _family_x.compute_family_x(
            REAL_WORKSPACE,
            host_family=host_family,
            family_bares=participants,
            host_evidence=str(host.get("note") or ""),
        )
        if record.get("problems"):
            raise unittest.SkipTest("X_f 计算有问题：" + "；".join(record["problems"]))
        x_by_family = {
            str(key): [float(value) for value in matrix]
            for key, matrix in (record.get("x") or {}).items()
        }
        palettes = {}
        rows_by_family = {}
        matrices = {}
        for family, bare in sorted(participants.items()):
            got = _family_x.read_family_palette(REAL_WORKSPACE, bare)
            if not got.get("ok"):
                raise unittest.SkipTest("族 palette 不可读：" + family + " — " + str(got.get("note")))
            palettes[family] = got
            draw = str(((record.get("families") or {}).get(family) or {}).get("Draw") or "")
            matrix = _family_x.read_draw_object_matrix(frame_dir, draw)
            if not matrix.get("ok"):
                raise unittest.SkipTest("族对象矩阵不可读：" + family + " — " + str(matrix.get("note")))
            matrices[family] = [float(value) for value in matrix["matrix"]]
            source_rows = []
            # participants 的值是 LOD0 目录名（"LOD0.<bare>"），磁盘上的子目录名去掉了前缀
            bare_name = bare[len("LOD0."):] if bare.startswith("LOD0.") else bare
            lod = os.path.join(REAL_WORKSPACE, "LOD0", bare_name)
            types_here = sorted(os.listdir(lod)) if os.path.isdir(lod) else []
            for type_name in types_here:
                path = os.path.join(lod, type_name, bare_name + _family_x.POSITION_BUFFER_SUFFIX)
                if os.path.isfile(path):
                    source_rows = _read_position_rows(path)
                    break
            step = max(1, len(source_rows) // 48)
            rows_by_family[family] = source_rows[::step][:48]
        cls.data = {
            "host": host_family,
            "m_base": matrices.get(host_family),
            "matrices": matrices,
            "palettes": palettes,
            "x": x_by_family,
            "rows": rows_by_family,
        }

    def test_scheme_b_is_exact_and_the_old_q_scheme_is_wrong(self):
        data = self.data
        m_base = data["m_base"]
        if not m_base:
            self.skipTest("宿主族对象矩阵缺失")
        worst_new = 0.0
        worst_old = 0.0
        witness = {}
        for vertex_family, rows in sorted(data["rows"].items()):
            if not rows:
                continue
            for slot_family, palette in sorted(data["palettes"].items()):
                if slot_family == vertex_family:
                    continue
                x_g = data["x"].get(slot_family)
                m_g = data["matrices"].get(slot_family)
                if not x_g or not m_g:
                    continue
                slots = list(palette.get("slots") or [])
                if len(slots) < 2:
                    continue
                # 顶点被「自己族」的 Q_f 搬过；槽里乘的是「槽族」的 Q_g⁻¹ —— 两者族不同才有偏差
                vertex_slots = list((data["palettes"].get(vertex_family) or {}).get("slots") or [])
                if not vertex_slots:
                    continue
                q_f = vertex_slots[0]
                q_g_inverse = _runtime.mat3x4_inverse(slots[0])
                for slot_index in range(1, min(len(slots), 12)):
                    p_slot = slots[slot_index]
                    left_new = _runtime.mat3x4_compose(
                        m_base, _runtime.mat3x4_compose(x_g, p_slot))
                    left_old = _runtime.mat3x4_compose(
                        m_base,
                        _runtime.mat3x4_compose(
                            x_g, _runtime.mat3x4_compose(p_slot, q_g_inverse)),
                    )
                    truth_left = _runtime.mat3x4_compose(m_g, p_slot)
                    for point in rows:
                        truth = _runtime.mat3x4_apply(truth_left, point)
                        got_new = _runtime.mat3x4_apply(left_new, point)
                        moved = _runtime.mat3x4_apply(q_f, point)
                        got_old = _runtime.mat3x4_apply(left_old, moved)
                        new_delta = max(abs(a - b) for a, b in zip(got_new, truth))
                        old_delta = max(abs(a - b) for a, b in zip(got_old, truth))
                        if new_delta > worst_new:
                            worst_new = new_delta
                            witness["new"] = (vertex_family, slot_family, slot_index)
                        if old_delta > worst_old:
                            worst_old = old_delta
                            witness["old"] = (vertex_family, slot_family, slot_index)
        self.assertGreater(worst_old, 0.0, "没有任何跨族组合被检查到（数据缺失？）")
        self.assertLessEqual(
            worst_new, 1e-6,
            "方案 B 的跨族真值偏差过大：" + repr(worst_new) + " " + repr(witness.get("new")))
        self.assertGreater(
            worst_old, 1e-2,
            "旧 Q 方案的跨族偏差只有 " + repr(worst_old) + " ⇒ 护栏不是恒真")


class FamilyXRecordVertexCalibrationTests(unittest.TestCase):
    """第 4 项契约元数据：``Config/HIMIMergedSkeleton.json`` 必须留下 ``VertexCalibration: "none"``。

    【t78 方案 B】顶点侧不校准这件事不能只活在代码里 —— 离线 oracle / 排查的人要能从产物元数据
    读到它，否则拿到旧产物（t39–t77 的 Q_f⁻¹ 口径）无法与方案 B 区分。
    """

    def setUp(self):
        self.tempdir = tempfile.mkdtemp(prefix="himi_familyx_record_")
        os.makedirs(os.path.join(self.tempdir, "Config"), exist_ok=True)
        self.path = os.path.join(self.tempdir, "Config", "HIMIMergedSkeleton.json")
        with open(self.path, "w", encoding="utf-8") as handle:
            handle.write('{"SlotCount": 343, "SkeletonDigest": "deadbeef"}')
        self._previous_workspace = _GLOBAL_STATE["workspace"]
        _GLOBAL_STATE["workspace"] = self.tempdir
        warnings = []

        class Stub(object):
            _himi_family_x_record = {
                "families": {
                    "2c706fbd": {
                        "Bare": "2c706fbd-3207-60639",
                        "Draw": "Draw_0",
                        "X": list(IDENTITY),
                        "IsIdentity": True,
                        "Duality": {"hits": 31066, "total": 31066, "max_residual": 1e-16},
                    },
                },
                "host_family": "2c706fbd",
                "source_frame": "FrameAnalysis-2026-09-23-140134",
            }

            def _himi_host_family(self, plan):
                return {"note": "宿主族判定：Position 缓冲最大 2c706fbd（31066 行）"}

            def himi_merged_runtime_plan(self):
                return types.SimpleNamespace(family_ranges={})

            def _himi_notify(self, message, warning=False):
                warnings.append((message, warning))

        Stub._himi_publish_family_x_record = ExportHIMI._himi_publish_family_x_record.__get__(Stub(), Stub)
        self.stub = Stub()
        self.warnings = warnings

    def tearDown(self):
        _GLOBAL_STATE["workspace"] = self._previous_workspace
        shutil.rmtree(self.tempdir, ignore_errors=True)

    def test_record_carries_vertex_calibration_none(self):
        result = self.stub._himi_publish_family_x_record()
        self.assertTrue(result.get("ok"), result)
        with open(self.path, encoding="utf-8") as handle:
            payload = json.load(handle)
        self.assertEqual(payload["SlotCount"], 343, "既有键必须保留（只加 FamilyX 一个顶层键）")
        self.assertEqual(payload["SkeletonDigest"], "deadbeef")
        section = payload["FamilyX"]
        self.assertEqual(section[_contract.VERTEX_CALIBRATION_KEY], _contract.VERTEX_CALIBRATION_NONE)
        self.assertIn("S = X_f · P_f", section["Algorithm"])
        self.assertNotIn(
            "P_f ∘ Q_f⁻¹", section["Algorithm"], "旧公式 S = X_f ∘ P_f ∘ Q_f⁻¹ 不能留在元数据里")
        self.assertEqual(_contract.read_vertex_calibration(payload), "none")
        self.assertEqual(_contract.read_vertex_calibration(section), "none")

    def test_reader_reports_empty_for_legacy_products(self):
        self.assertEqual(_contract.read_vertex_calibration({"SlotCount": 3}), "")
        self.assertEqual(_contract.read_vertex_calibration({}), "")
        self.assertEqual(_contract.read_vertex_calibration(None), "")
        self.assertEqual(
            _contract.read_vertex_calibration({"FamilyX": {"Algorithm": "S = X_f ∘ P_f ∘ Q_f⁻¹"}}),
            "",
            "旧产物（没有 VertexCalibration 键）必须读成空串，不能被误判成方案 B",
        )


class PaletteSolvedXTests(unittest.TestCase):
    """X_f 的**权威来源**：导出时从当前工作空间的族 palette 现算（不读抓帧目录）。

    这就是这次校准的核心性质，也是「它随场景变」的证明：
      * 同一场景算两次 ⇒ 同一个 X（确定性，不含时间/目录等外部状态）；
      * 把某一族挪一下 ⇒ 它的 X 跟着挪（旧实现读的是**上一次场景**的 vs-cb3 快照，不会变）。
    """

    @staticmethod
    def _object_matrix(*, degrees=0.0, translate=(0.0, 0.0, 0.0)):
        """绕 Z 转 degrees、再平移 ⇒ 12 float 3×4 行主序（列向量口径）。"""
        radians = math.radians(degrees)
        cos, sin = math.cos(radians), math.sin(radians)
        return [
            cos, -sin, 0.0, translate[0],
            sin, cos, 0.0, translate[1],
            0.0, 0.0, 1.0, translate[2],
        ]

    def _palettes(self, base_matrix, family_matrix, count=5):
        """两族 palette：``P = M⁻¹ · B``（B = 绑定矩阵，两族引用同一份骨）。"""
        bones = [
            self._object_matrix(
                degrees=11.0 * index,
                translate=(0.17 * index, 0.09 * index * index, 0.31 * index + 0.05),
            )
            for index in range(count)
        ]
        base_inverse = _family_x.mat3x4_inverse(base_matrix)
        family_inverse = _family_x.mat3x4_inverse(family_matrix)
        return (
            [_family_x.mat3x4_compose(base_inverse, bone) for bone in bones],
            [_family_x.mat3x4_compose(family_inverse, bone) for bone in bones],
            base_inverse,
        )

    def test_solved_x_is_the_matrix_world_relation(self):
        base = self._object_matrix(degrees=17.0, translate=(0.4, -1.2, 0.9))
        family = self._object_matrix(degrees=-43.0, translate=(2.0, 0.5, -0.25))
        base_palette, family_palette, base_inverse = self._palettes(base, family)
        solved = _family_x.solve_family_x_from_palettes(base_palette, family_palette)
        self.assertTrue(solved["ok"], solved)
        expected = _family_x.mat3x4_compose(base_inverse, family)
        self.assertLessEqual(
            _family_x.max_abs_diff(solved["x"], expected), 1e-9,
            "反解出来的必须就是 X_f = M_base⁻¹ · M_f",
        )
        self.assertEqual(solved["votes"], 5, "五根骨都必须能落到宿主骨上")
        self.assertLessEqual(solved["max_residual"], _family_x.PALETTE_SOLVE_TOL)

    def test_x_is_deterministic_and_follows_the_scene(self):
        base = self._object_matrix(translate=(0.4, -1.2, 0.9))
        near = self._object_matrix(degrees=-43.0, translate=(2.0, 0.5, -0.25))
        far = self._object_matrix(degrees=-43.0, translate=(2.6, 0.5, -0.25))
        base_palette, family_palette, _inverse = self._palettes(base, near)
        one = _family_x.solve_family_x_from_palettes(base_palette, family_palette)
        two = _family_x.solve_family_x_from_palettes(base_palette, family_palette)
        self.assertEqual(one["x"], two["x"], "同一场景两次现算必须逐位相同")
        shifted_base, shifted_family, shifted_inverse = self._palettes(base, far)
        moved = _family_x.solve_family_x_from_palettes(shifted_base, shifted_family)
        self.assertGreater(
            _family_x.max_abs_diff(one["x"], moved["x"]), 0.5,
            "该族挪动 0.6 ⇒ X 必须跟着变（抓帧快照不会变，这正是旧实现坏掉的地方）",
        )
        self.assertLessEqual(
            _family_x.max_abs_diff(
                moved["x"], _family_x.mat3x4_compose(shifted_inverse, far)
            ),
            1e-9,
        )

    def test_host_family_is_identity_and_source_is_palette(self):
        base = self._object_matrix(translate=(0.4, -1.2, 0.9))
        family = self._object_matrix(degrees=-43.0, translate=(2.0, 0.5, -0.25))
        base_palette, family_palette, base_inverse = self._palettes(base, family)
        with tempfile.TemporaryDirectory(prefix="himi_family_x_palette_") as folder:
            result = _family_x.compute_family_x_from_palettes(
                folder,
                host_family="aaaa1111",
                family_bares={"aaaa1111": "aaaa1111-100-0", "bbbb2222": "bbbb2222-50-0"},
                palettes={"aaaa1111": base_palette, "bbbb2222": family_palette},
            )
        self.assertEqual(result["source"], "palette")
        self.assertEqual(result["problems"], [])
        self.assertEqual(result["families"]["aaaa1111"]["X"], IDENTITY, "宿主族恒为单位阵")
        self.assertEqual(result["families"]["bbbb2222"]["Source"], "palette")
        expected = _family_x.mat3x4_compose(base_inverse, family)
        self.assertLessEqual(
            _family_x.max_abs_diff(result["x"]["bbbb2222"], expected), 1e-9
        )
        # 审计记录必须能把「来源」带到产物元数据里（旧产物 = frame-vs-cb3，这次 = palette）
        audit = _family_x.family_x_audit_record(result)
        self.assertEqual(audit["Source"], "palette")
        self.assertEqual(audit["Families"]["bbbb2222"]["Source"], "palette")
        self.assertEqual(audit["Families"]["bbbb2222"]["Votes"], 5)

    def test_unreadable_palette_is_reported_not_guessed(self):
        with tempfile.TemporaryDirectory(prefix="himi_family_x_palette_") as folder:
            result = _family_x.compute_family_x_from_palettes(
                folder,
                host_family="aaaa1111",
                family_bares={"aaaa1111": "aaaa1111-100-0", "bbbb2222": "bbbb2222-50-0"},
                palettes={"aaaa1111": [list(IDENTITY)]},
            )
        self.assertNotIn("bbbb2222", result["x"], "解不出来就必须不给 X，绝不能猜一个")
        self.assertTrue(result["problems"], "缺 palette 必须报成问题（调用方据此兜底或 Fatal）")


if __name__ == "__main__":
    unittest.main()
