"""顶点组 Blend 重排：等宽 / 升宽原样，**超槽位自动降宽（保留最强的 N 个 + 重新归一化）**。

2026-09-21 用户裁定（修正旧口径）：不再因为「影响数 > 目标槽位数」中断导出——
与其它所有游戏路径（v1 / v3 / v4_fast）同款，自动取权重最强的 N 个并重新归一化，
损失量由主守卫汇总后大声报告。

覆盖两层：
- `utils/vertexgroup_utils.py` 的 `VertexGroupUtils.get_blendweights_blendindices_for_layout`
  （纵深防御：超槽位取 top-N，并按**幸存权重之和**重新归一化）；
- `common/obj_buffer_helper.py` 的 `ObjBufferHelper.parse_elementname_data_dict`
  （ZZMI 合并分支：汇总超限顶点数 / 最大丢弃权重占比 / 样本顶点索引并打印；占比
  ≥5% 时额外提示回 Blender 用 清理 Clean → 限制总影响数 Limit Total → 归一化 All）。

第三层（本次新增）：**t66 门控** —— `parse_elementname_data_dict` 只在 HIMI 把
`vertex_group_names` 交给 v3 提取器（按组名解析槽号），其它逻辑一律传 `None`
⇒ 仍是「`object.vertex_groups` 列表序号」= 改动前的逐字节行为。
机制本身（名字优先、非十进制名回退序号）由
`tests/test_himi_merged_runtime.py::HIMIVertexGroupNameResolutionTests` 钉住。
"""
import contextlib
import importlib.util
import io
import sys
import types
import unittest
from pathlib import Path

import numpy as np


ROOT = Path(__file__).resolve().parents[1]
PKG = "_vertexgroup_blend_layout_test_pkg"


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


# --- 假包树：{PKG} / {PKG}.common / {PKG}.utils ------------------------------
for package_name in (PKG, f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []

from tests import _real_modules as _real_modules
_real_modules.register_real_common_modules(f"{PKG}.common")

_install_module("bpy", types=types.SimpleNamespace(Object=object, Mesh=object))
_install_module("mathutils", Vector=lambda value: value)

# utils：只桩掉与本用例无关的重依赖；format_utils / ssmt_error_utils /
# vertexgroup_utils 一律用**真实实现**（要断言的正是它们的行为）。
_install_module(
    f"{PKG}.utils.timer_utils",
    TimerUtils=types.SimpleNamespace(Start=lambda *_: None, End=lambda *_: None),
)
_install_module(
    f"{PKG}.utils.color_attribute_utils",
    encode_color_for_unorm_target=lambda *args, **kwargs: None,
    read_color_attribute_data=lambda *args, **kwargs: None,
    sample_color_attribute_to_loops=lambda *args, **kwargs: None,
)

_load_module(f"{PKG}.utils.tbn_codec", ROOT / "utils" / "tbn_codec.py")
format_utils = _load_module(f"{PKG}.utils.format_utils", ROOT / "utils" / "format_utils.py")
ssmt_error_utils = _load_module(
    f"{PKG}.utils.ssmt_error_utils", ROOT / "utils" / "ssmt_error_utils.py"
)
vertexgroup_utils = _load_module(
    f"{PKG}.utils.vertexgroup_utils", ROOT / "utils" / "vertexgroup_utils.py"
)

# common：只有主守卫需要的字段，全部用替身（避免拖入 bpy 重型依赖）
_install_module(f"{PKG}.common.d3d11_gametype", D3D11GameType=object)
_install_module(f"{PKG}.common.draw_call_model", DrawCallModel=object)
_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(
        ZZMI="ZZMI",
        ZZMIDX12="ZZMIDX12",
        WWMI="WWMI",
        NTEMI="NTEMI",
        EFMI="EFMI",
        SnowBreak="SnowBreak",
        is_zzmi_family=lambda logic_name: logic_name in {"ZZMI", "ZZMIDX12"},
    ),
)
_install_module(f"{PKG}.common.global_config", GlobalConfig=types.SimpleNamespace(logic_name="ZZMI"))
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(import_merged_vgmap=lambda: True),
)

obj_buffer_helper = _load_module(
    f"{PKG}.common.obj_buffer_helper", ROOT / "common" / "obj_buffer_helper.py"
)

ObjBufferHelper = obj_buffer_helper.ObjBufferHelper
VertexGroupUtils = vertexgroup_utils.VertexGroupUtils
Fatal = ssmt_error_utils.Fatal
# 门控用例要读/改当前逻辑名（模块对象在上面只装进了 sys.modules，这里取个名字）
GlobalConfig = sys.modules[f"{PKG}.common.global_config"].GlobalConfig


class _Assignment:
    def __init__(self, group, weight):
        self.group = group
        self.weight = weight


class _Vertex:
    def __init__(self, index, groups):
        self.index = index
        self.groups = [_Assignment(group, weight) for group, weight in groups]


class _Loops:
    def __init__(self, indices):
        self.indices = np.asarray(indices, dtype=np.int64)

    def __len__(self):
        return len(self.indices)

    def foreach_get(self, name, output):
        if name != "vertex_index":
            raise AssertionError(name)
        output[:] = self.indices


class _Mesh:
    def __init__(self, vertices, loop_indices, name="TestMesh"):
        self.name = name
        self.vertices = [_Vertex(i, groups) for i, groups in enumerate(vertices)]
        self.loops = _Loops(loop_indices)


class _FakeGameType:
    """`D3D11GameType` 的最小替身：只给 ZZMI 合并分支主守卫用到的字段。"""

    def __init__(self, blend_format="R32G32_UINT", byte_width=8):
        self.D3D11ElementList = [
            types.SimpleNamespace(
                SemanticName="BLENDINDICES", Format=blend_format, ByteWidth=byte_width
            )
        ]
        self.OrderedCategoryNameList = ["Blend", "Position"]
        # 元素循环走空表 ⇒ 守卫放行时函数直接返回 {}（用于证明没有误报）
        self.OrderedFullElementList = []
        self.ElementNameD3D11ElementDict = {}


class BlendLayoutExtractionTests(unittest.TestCase):
    """纵深防御层：提取函数只接受等宽或升宽。"""

    def test_upcast_pads_rigid_and_preserves_four_channels(self):
        mesh = _Mesh([[(12, 1.0)], [(2, 0.5), (5, 0.3)]], [0, 1])
        weights, indices = VertexGroupUtils.get_blendweights_blendindices_for_layout(
            mesh, 4
        )
        np.testing.assert_array_equal(indices[0], [[12, 0, 0, 0], [2, 5, 0, 0]])
        np.testing.assert_allclose(weights[0], [[1.0, 0.0, 0.0, 0.0], [0.625, 0.375, 0.0, 0.0]])

    def test_downcast_four_influences_to_two_channels_keeps_strongest(self):
        """改写自旧 `..._raises` 用例（2026-09-21 用户裁定修正口径）。

        旧口径要求抛错；现在与其它游戏路径一致——保留最强的 2 个，并按**幸存
        权重之和**重新归一化（被丢弃的 0.1 / 0.2 不再占权重和）。
        """
        mesh = _Mesh(
            [
                [(9, 0.1), (3, 0.6), (7, 0.3), (11, 0.2)],
                [(4, 1.0)],
            ],
            [0, 1, 0],
        )
        weights, indices = VertexGroupUtils.get_blendweights_blendindices_for_layout(
            mesh, 2
        )
        np.testing.assert_array_equal(indices[0], [[3, 7], [4, 0], [3, 7]])
        np.testing.assert_allclose(
            weights[0], [[0.6 / 0.9, 0.3 / 0.9], [1.0, 0.0], [0.6 / 0.9, 0.3 / 0.9]]
        )
        np.testing.assert_allclose(weights[0].sum(axis=1), [1.0, 1.0, 1.0])

    def test_downcast_four_influences_to_one_channel_keeps_strongest(self):
        mesh = _Mesh([[(9, 0.1), (3, 0.6), (7, 0.3), (11, 0.2)]], [0])
        weights, indices = VertexGroupUtils.get_blendweights_blendindices_for_layout(
            mesh, 1
        )
        np.testing.assert_array_equal(indices[0], [[3]])
        np.testing.assert_allclose(weights[0], [[1.0]])

    def test_widen_one_or_two_influences_to_four_channels_pads_and_normalizes(self):
        mesh = _Mesh([[(2, 0.5), (5, 0.3)], [(12, 1.0)]], [0, 1])
        weights, indices = VertexGroupUtils.get_blendweights_blendindices_for_layout(
            mesh, 4
        )
        np.testing.assert_array_equal(indices[0], [[2, 5, 0, 0], [12, 0, 0, 0]])
        np.testing.assert_allclose(weights[0], [[0.625, 0.375, 0.0, 0.0], [1.0, 0.0, 0.0, 0.0]])
        np.testing.assert_allclose(weights[0].sum(axis=1), [1.0, 1.0])

    def test_zero_or_nonfinite_weight_groups_do_not_count_as_influences(self):
        """`weight == 0` / 负权重 / NaN 都不算有效影响数（否则会被误判成降宽）。"""
        mesh = _Mesh([[(1, 0.0), (2, -0.5), (3, float("nan")), (4, 0.7)]], [0])
        weights, indices = VertexGroupUtils.get_blendweights_blendindices_for_layout(
            mesh, 1
        )
        # 只有一个有效影响（组 4）⇒ 1 槽不算降宽，直接通过
        np.testing.assert_array_equal(indices[0], [[4]])
        np.testing.assert_allclose(weights[0], [[1.0]])

        mesh_many_zero = _Mesh(
            [[(1, 0.0), (2, 0.0), (3, 0.0), (4, 0.0), (5, 0.25)]], [0]
        )
        weights, indices = VertexGroupUtils.get_blendweights_blendindices_for_layout(
            mesh_many_zero, 1
        )
        np.testing.assert_array_equal(indices[0], [[5]])
        np.testing.assert_allclose(weights[0], [[1.0]])

    def test_ties_use_group_id_as_stable_tiebreaker(self):
        """tie-break 稳定性在**等宽**（允许）区间继续锁定：同权重按 group id 升序。

        旧版本用 1 槽（3 影响降宽）来测 tie-break，与新口径冲突 ⇒ 改成等宽 3 槽；
        断言的是同一套稳定排序语义，未放松覆盖。
        """
        mesh = _Mesh([[(8, 0.5), (2, 0.5), (4, 0.1)]], [0])
        weights, indices = VertexGroupUtils.get_blendweights_blendindices_for_layout(
            mesh, 3
        )
        self.assertEqual(indices[0].tolist(), [[2, 8, 4]])
        np.testing.assert_allclose(weights[0], [[5 / 11, 5 / 11, 1 / 11]])
        np.testing.assert_allclose(weights[0].sum(axis=1), [1.0])


class ZZMIBlendDowncastGuardTests(unittest.TestCase):
    """主守卫层：超槽位不再中断导出，改为汇总报告（自动降宽在提取函数里完成）。"""

    def _run(self, mesh, game_type):
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            result = ObjBufferHelper.parse_elementname_data_dict(mesh, game_type)
        return result, buffer.getvalue()

    def test_guard_auto_downsizes_and_reports(self):
        mesh = _Mesh(
            [
                [(9, 0.1), (3, 0.6), (7, 0.3), (11, 0.2)],
                [(4, 1.0)],
            ],
            [0, 1, 0],
            name="MergedObject",
        )
        game_type = _FakeGameType(blend_format="R32G32_UINT", byte_width=8)  # N = 2
        result, output = self._run(mesh, game_type)
        self.assertEqual(result, {})
        self.assertIn("MergedObject", output)
        self.assertIn("已自动降宽", output)
        self.assertIn("R32G32_UINT", output)
        self.assertIn("ByteWidth=8", output)
        self.assertIn("N=2", output)
        self.assertIn("M=4", output)
        self.assertIn("超限顶点 1/2", output)
        self.assertIn("样本顶点索引 [0]", output)

    def test_guard_reports_single_channel_target(self):
        mesh = _Mesh([[(1, 0.6), (2, 0.4)]], [0], name="RigidMerged")
        game_type = _FakeGameType(blend_format="R32_UINT", byte_width=4)  # N = 1
        result, output = self._run(mesh, game_type)
        self.assertEqual(result, {})
        self.assertIn("RigidMerged", output)
        self.assertIn("N=1", output)
        self.assertIn("M=2", output)
        self.assertIn("R32_UINT", output)
        self.assertIn("ByteWidth=4", output)

    def test_guard_warns_when_dropped_fraction_is_large(self):
        """丢弃占比 ≥5% ⇒ 追加提示（重新归一化会让顶点位移，需回 Blender 修权重）。"""
        mesh = _Mesh([[(1, 0.4), (2, 0.3), (3, 0.2), (4, 0.1)]], [0], name="HeavyDrop")
        game_type = _FakeGameType(blend_format="R32G32_UINT", byte_width=8)  # N = 2
        result, output = self._run(mesh, game_type)
        self.assertEqual(result, {})
        self.assertIn("最大丢弃权重占比 30.00%", output)
        self.assertIn("丢弃占比偏大", output)
        self.assertIn("Limit Total", output)

    def test_guard_stays_quiet_when_influences_fit(self):
        """等宽 / 升宽不得误报，也不打印降宽横幅。"""
        for game_type in (
            _FakeGameType(blend_format="R32G32_UINT", byte_width=8),
            _FakeGameType(blend_format="R32G32B32A32_UINT", byte_width=16),
        ):
            with self.subTest(blend_format=game_type.D3D11ElementList[0].Format):
                mesh = _Mesh([[(2, 0.5), (5, 0.5)]], [0], name="WithinSlots")
                result, output = self._run(mesh, game_type)
                self.assertEqual(result, {})
                self.assertNotIn("已自动降宽", output)

    def test_guard_allows_equal_width_target(self):
        """等宽（M == N = 2）不得误报：守卫放行，函数继续走到元素循环（空表 ⇒ {}）。"""
        mesh = _Mesh([[(2, 0.5), (5, 0.5)]], [0], name="EqualWidth")
        game_type = _FakeGameType(blend_format="R32G32_UINT", byte_width=8)
        self.assertEqual(
            ObjBufferHelper.parse_elementname_data_dict(mesh, game_type), {}
        )

    def test_guard_allows_wider_target(self):
        """升宽（M = 2 < N = 4）不得误报。"""
        mesh = _Mesh([[(2, 0.5), (5, 0.5)]], [0], name="WideWidth")
        game_type = _FakeGameType(blend_format="R32G32B32A32_UINT", byte_width=16)
        self.assertEqual(
            ObjBufferHelper.parse_elementname_data_dict(mesh, game_type), {}
        )


class GroupNameResolutionGateTests(unittest.TestCase):
    """【t66 门控】按组名解析 BLENDINDICES 槽号**只对 HIMI 生效**。

    `parse_elementname_data_dict` 把 `vertex_group_names` 交给 v3 提取器之前有一道门：
    `GlobalConfig.logic_name != HIMI ⇒ vertex_group_names = None`。本用例用**间谍**替换
    v3 提取器，直接观察它实际收到的参数 —— 非 HIMI 必须是 `None`（旧行为），
    HIMI 必须拿到组名表（新行为）。

    「拿到组名表之后会按名字写槽号」由
    `tests/test_himi_merged_runtime.py::HIMIVertexGroupNameResolutionTests` 覆盖；
    两条合起来才等于「HIMI 新、其余旧」。
    """

    def setUp(self):
        self._original_logic = GlobalConfig.logic_name
        self._original_v3 = VertexGroupUtils.get_blendweights_blendindices_v3
        self.calls = []
        saved = self._original_v3

        def spy(*args, **kwargs):
            self.calls.append(kwargs.get("vertex_group_names"))
            return saved(*args, **kwargs)

        VertexGroupUtils.get_blendweights_blendindices_v3 = spy

    def tearDown(self):
        VertexGroupUtils.get_blendweights_blendindices_v3 = self._original_v3
        GlobalConfig.logic_name = self._original_logic

    def _names_seen_by_extractor(self, logic_name, names):
        GlobalConfig.logic_name = logic_name
        self.calls.clear()
        # 组列表序号 1，而组名表把序号 1 映射到 "9" ⇒ 名字与序号刻意不一致
        mesh = _Mesh([[(1, 1.0)]], [0])
        ObjBufferHelper.parse_elementname_data_dict(
            mesh, _FakeGameType(), vertex_group_names=names
        )
        return self.calls

    def test_non_himi_logics_keep_the_old_index_behaviour(self):
        """EFMI / GIMI / SRMI / HTMI 走 else 分支：提取器必须收到 None（= 旧行为）。

        刻意不含 ZZMI / ZZMIDX12（合并开关打开时走 for_layout 分支）、
        WWMI / NTEMI / SnowBreak（走 v4_fast）—— 那几条根本不经过这道门。
        """
        for logic_name in ("EFMI", "GIMI", "SRMI", "HTMI"):
            with self.subTest(logic_name=logic_name):
                self.assertEqual(
                    self._names_seen_by_extractor(logic_name, ["5", "9"]),
                    [None],
                )

    def test_himi_receives_the_group_names(self):
        """HIMI 是唯一放行的逻辑：提取器必须收到组名表。"""
        self.assertEqual(
            self._names_seen_by_extractor("HIMI", ["5", "9"]),
            [["5", "9"]],
        )

    def test_gate_is_present_in_the_call_site(self):
        """源码级钉子：门控一旦被删掉（例如为了「统一口径」而顺手放开），本用例失败。"""
        source = (ROOT / "common" / "obj_buffer_helper.py").read_text(encoding="utf-8")
        self.assertIn(
            'if GlobalConfig.logic_name != getattr(LogicName, "HIMI", "HIMI"):\n'
            "                vertex_group_names = None",
            source,
        )


if __name__ == "__main__":
    unittest.main()
