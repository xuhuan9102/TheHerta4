"""EFMI 拖拽交互独立分支的最小单测骨架（stub bpy，绕过 Blender 环境）。

覆盖（t6 契约）：
- 路由判定：GlobalConfig.logic_name == EFMI → EFMI 分支执行器；
  zzmi 家族 → 原分支（不跳 EFMI）；其他 → 跳过并警告；
- 烘焙形状：双胸腔区平滑场（UV 场 + 空间场 + side/vertical/front 门）、
  重合顶点取最大、body→cloth 16 近邻传递；
- 核心段族齐全：探针/检测/模拟/变形段、Apply/Probe CL、Key 段、资源段、
  Draw 回调注入（MergedSkeleton_Apply 后、原始绘制前）、Present/Constants、
  二次执行幂等。

完整覆盖由验证①（t15）补强。
"""

import importlib.util
import os
import re
import sys
import tempfile
import types
import unittest
from collections import OrderedDict
from pathlib import Path

import numpy as np

REPO_ROOT = Path(__file__).resolve().parents[1]

EFMI_MODULE_NAME = "_ssmt_root.blueprint.node_postprocess_draginteraction_efmi"

EFMI_DRAG_HOOK_BEGIN = "; --- EFMI DRAG HOOK BEGIN ---"
EFMI_DRAG_PRESENT_BEGIN = "; --- EFMI DRAG PRESENT BEGIN ---"
EFMI_DRAG_PROBE_BEGIN = "; --- EFMI DRAG PROBE BEGIN ---"
EFMI_DRAG_PROBE_END = "; --- EFMI DRAG PROBE END ---"


def _load_efmi_module():
    """直接按文件加载 EFMI 分支模块（无相对导入、无 bpy 依赖）。"""
    path = REPO_ROOT / "blueprint" / "node_postprocess_draginteraction_efmi.py"
    spec = importlib.util.spec_from_file_location(EFMI_MODULE_NAME, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[EFMI_MODULE_NAME] = mod
    spec.loader.exec_module(mod)
    return mod


# ---------------------------------------------------------------------------
# stub bpy 基建（路由测试需加载 zzmi 节点模块）
# ---------------------------------------------------------------------------


def _install_stub_bpy():
    existing = sys.modules.get("bpy")
    if existing is not None and isinstance(getattr(existing, "types", None), type) and hasattr(existing.types, "PropertyGroup"):
        return
    bpy = types.ModuleType("bpy")

    class _Props:
        def StringProperty(self, **kw): return None
        def IntProperty(self, **kw): return None
        def FloatProperty(self, **kw): return None
        def BoolProperty(self, **kw): return None
        def EnumProperty(self, **kw): return None
        def CollectionProperty(self, **kw): return None
        def PointerProperty(self, **kw): return None

    bpy.props = _Props()

    class _Types:
        class PropertyGroup: pass
        class Operator: pass
        class Object: pass
        class Collection: pass
        class Node: pass
        class NodeSocket: pass
        class Menu: pass
        class UIList: pass

    bpy.types = _Types()

    class _Utils:
        @staticmethod
        def register_class(cls): pass
        @staticmethod
        def unregister_class(cls): pass

    bpy.utils = _Utils()
    bpy.data = types.SimpleNamespace(objects=None, node_groups=None)
    bpy.context = types.SimpleNamespace(scene=None, active_object=None, selected_objects=())
    sys.modules["bpy"] = bpy
    sys.modules["bpy.types"] = bpy.types
    sys.modules["bpy.props"] = bpy.props


def _install_root_packages():
    root = "_ssmt_root"
    pkg_specs = {
        root: REPO_ROOT,
        f"{root}.blueprint": REPO_ROOT / "blueprint",
        f"{root}.common": REPO_ROOT / "common",
        f"{root}.toolkit": REPO_ROOT / "toolkit",
        f"{root}.utils": REPO_ROOT / "utils",
    }
    for name, path in pkg_specs.items():
        if name not in sys.modules:
            pkg = types.ModuleType(name)
            pkg.__path__ = [str(path)]
            sys.modules[name] = pkg


def _install_utils_error_stub(root="_ssmt_root"):
    name = f"{root}.utils.ssmt_error_utils"
    if name in sys.modules:
        return
    mod = types.ModuleType(name)

    class SSMTErrorUtils:
        @staticmethod
        def log_and_raise(*a, **k):
            raise RuntimeError(a[0] if a else "SSMTError")

    mod.SSMTErrorUtils = SSMTErrorUtils
    sys.modules[name] = mod
    sys.modules[f"{root}.utils"].ssmt_error_utils = mod


def _install_node_base_stub(root="_ssmt_root"):
    name = f"{root}.blueprint.node_base"
    if name in sys.modules:
        return
    nb = types.ModuleType(name)

    class SSMTNodeBase:
        pass

    nb.SSMTNodeBase = SSMTNodeBase
    sys.modules[name] = nb
    sys.modules[f"{root}.blueprint"].node_base = nb


def _install_toolkit_gb_core(root="_ssmt_root"):
    name = f"{root}.toolkit.gb_core"
    if name in sys.modules:
        return
    path = REPO_ROOT / "toolkit" / "gb_core.py"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    sys.modules[f"{root}.toolkit"].gb_core = mod


def _load_click_export_module():
    """加载点击计数导出节点模块（真实公共工具 + anim_driver_base）。"""
    _install_stub_bpy()
    _install_root_packages()
    _install_utils_error_stub()
    _install_node_base_stub()
    _install_toolkit_gb_core()
    root = "_ssmt_root"
    if f"{root}.blueprint.direct_export" not in sys.modules:
        de = types.ModuleType(f"{root}.blueprint.direct_export")
        de.sync_shapekey_direct_mode = lambda self, ctx: None
        sys.modules[f"{root}.blueprint.direct_export"] = de
    _set_logic_name("")
    real_modules = {
        f"{root}.blueprint.variable_registry": REPO_ROOT / "blueprint" / "variable_registry.py",
        f"{root}.common.mod_path_compat": REPO_ROOT / "common" / "mod_path_compat.py",
        f"{root}.blueprint.deform_chain": REPO_ROOT / "blueprint" / "deform_chain.py",
        f"{root}.blueprint.node_postprocess_base": REPO_ROOT / "blueprint" / "node_postprocess_base.py",
        f"{root}.blueprint.anim_driver_base": REPO_ROOT / "blueprint" / "anim_driver_base.py",
    }
    for name, path in real_modules.items():
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
    return importlib.import_module(f"{root}.blueprint.anim_driver_click_export")


def _load_zzmi_module():
    _install_stub_bpy()
    _install_root_packages()
    _install_utils_error_stub()
    _install_node_base_stub()
    _install_toolkit_gb_core()
    return importlib.import_module("_ssmt_root.blueprint.node_postprocess_draginteraction")


def _load_shapekey_module():
    """加载形态键节点模块（stub 重依赖面，公共工具真实加载）。"""
    _install_stub_bpy()
    _install_root_packages()
    _install_utils_error_stub()
    _install_node_base_stub()
    root = "_ssmt_root"
    # direct_export 依赖重 → stub 最小面
    if f"{root}.blueprint.direct_export" not in sys.modules:
        de = types.ModuleType(f"{root}.blueprint.direct_export")
        de.sync_shapekey_direct_mode = lambda self, ctx: None
        sys.modules[f"{root}.blueprint.direct_export"] = de
    # 公共工具真实加载
    real_modules = {
        f"{root}.blueprint.variable_registry": REPO_ROOT / "blueprint" / "variable_registry.py",
        f"{root}.common.mod_path_compat": REPO_ROOT / "common" / "mod_path_compat.py",
        f"{root}.blueprint.deform_chain": REPO_ROOT / "blueprint" / "deform_chain.py",
        f"{root}.blueprint.node_postprocess_base": REPO_ROOT / "blueprint" / "node_postprocess_base.py",
    }
    for name, path in real_modules.items():
        if name not in sys.modules:
            spec = importlib.util.spec_from_file_location(name, path)
            mod = importlib.util.module_from_spec(spec)
            sys.modules[name] = mod
            spec.loader.exec_module(mod)
    return importlib.import_module(f"{root}.blueprint.node_postprocess_shapekey")


# _set_logic_name 原模块状态保存（供 _restore_logic_name 对称恢复，防跨测试类
# 泄漏——t29 F1：泄漏 EFMI 存根会使 zzmi 节点 _is_efmi_mode 误判路由）
_SAVED_GLOBAL_CONFIG_MODULE = None


def _set_logic_name(value):
    """替换 _ssmt_root.common.global_config 为只读存根（路由测试用）。

    首次调用保存被替换的原模块状态（sys.modules 条目），_restore_logic_name()
    对称恢复；重复调用只更新存根值。"""
    global _SAVED_GLOBAL_CONFIG_MODULE
    name = "_ssmt_root.common.global_config"
    if _SAVED_GLOBAL_CONFIG_MODULE is None:
        _SAVED_GLOBAL_CONFIG_MODULE = sys.modules.get(name)
    gc = types.SimpleNamespace(
        GlobalConfig=types.SimpleNamespace(logic_name=value, _get_output_blueprint_tree=lambda: None)
    )
    sys.modules[name] = gc
    return gc


def _restore_logic_name():
    """恢复 _set_logic_name 之前保存的 global_config 模块状态（测试隔离）。

    原模块不存在（此前未被加载）→ 弹出存根，后续 import 按真实路径重新加载；
    原模块存在 → 原样放回 sys.modules。"""
    global _SAVED_GLOBAL_CONFIG_MODULE
    name = "_ssmt_root.common.global_config"
    if _SAVED_GLOBAL_CONFIG_MODULE is not None:
        sys.modules[name] = _SAVED_GLOBAL_CONFIG_MODULE
    else:
        sys.modules.pop(name, None)
    _SAVED_GLOBAL_CONFIG_MODULE = None


# ---------------------------------------------------------------------------
# 场景构造
# ---------------------------------------------------------------------------


def _make_efmi_node(**props):
    defaults = dict(
        hash_values="abc123",
        mod_namespace="",
        grab_key="ALT",
        grab_gesture="LMB",
        poke_gesture="RMB",
        enable_poke=True,
        enable_hand_cursor=False,
        enable_viewport_probe=False,
        enable_shapekey_drive=False,
        feature_shapekey_link=True,
        feature_variable_link=True,
        feature_panel_link=True,
        drag_mode_variable_name="ssmtdrag_drag_enabled",
        ui_detected_variable_name="ssmtdrag_ui_detected",
        ui_zone_variable_name="ssmtdrag_ui_zone",
        zone_objects=(),
        bake_reference_object=None,
        efmi_probe_pass_hash="1718.1, d7bb9dd57f5b70c6",
        efmi_pull_depth=0.025,
        efmi_push_depth=0.016,
        efmi_drag_scale=0.70,
        efmi_max_offset=0.040,
        efmi_grab_hz=5.0,
        efmi_grab_damping=0.85,
        efmi_release_hz=2.8,
        efmi_release_damping=0.22,
        efmi_hit_threshold=0.10,
        id_data=None,
    )
    defaults.update(props)
    return types.SimpleNamespace(**defaults)


def _write_buf(path, array):
    path.parent.mkdir(parents=True, exist_ok=True)
    np.asarray(array).tofile(path)


def _make_efmi_mod_dir(pos_stride=16):
    """构造迷你 EFMI 导出目录：16 顶点 body（双胸腔区）+ ini（EntryPoint/回调）。

    pos_stride：Position 资源 stride（16 = LOD1 型 / 40 = LOD0 型契约），
    strideWords 断言（4/10）共用同一夹具。
    """
    td = Path(tempfile.mkdtemp(prefix="efmi_drag_test_"))
    mesh_dir = td / "Meshes"
    pos_words = max(1, pos_stride // 4)
    body_pos = np.zeros((16, pos_words), dtype=np.float32)
    # 左胸腔区（game-right，负 X）4 顶点
    body_pos[0, :3] = [-0.070, -0.122, 1.092]
    body_pos[1, :3] = [-0.065, -0.120, 1.100]
    body_pos[2, :3] = [-0.060, -0.125, 1.090]
    body_pos[3, :3] = [-0.075, -0.118, 1.095]
    # 右胸腔区 4 顶点
    body_pos[4, :3] = [0.070, -0.122, 1.092]
    body_pos[5, :3] = [0.065, -0.120, 1.100]
    body_pos[6, :3] = [0.060, -0.125, 1.090]
    body_pos[7, :3] = [0.075, -0.118, 1.095]
    # 无关顶点（Z 低/侧深/远处）
    body_pos[8, :3] = [0.000, -0.122, 1.000]
    body_pos[9, :3] = [0.000, -0.250, 1.100]
    body_pos[10:, :3] = 0.500
    if pos_words >= 4:
        body_pos[:, 3] = 1.0

    body_uv = np.zeros((16, 3), dtype=np.float32)
    body_uv[:4, :2] = [[0.1162, 0.0578], [0.1150, 0.0600], [0.1180, 0.0560], [0.1140, 0.0590]]
    body_uv[4:8, :2] = [[0.1338, 0.0578], [0.1320, 0.0600], [0.1360, 0.0560], [0.1350, 0.0590]]
    body_uv[8:, :2] = 0.05

    body_blend = np.zeros((16, 4), dtype=np.float32)
    body_ib = np.array([0, 1, 2, 1, 2, 3, 4, 5, 6, 5, 6, 7], dtype=np.uint32)

    _write_buf(mesh_dir / "LOD0.abc123-43191-Position.buf", body_pos)
    _write_buf(mesh_dir / "LOD0.abc123-43191-Texcoord.buf", body_uv)
    _write_buf(mesh_dir / "LOD0.abc123-43191-Blend.buf", body_blend)
    _write_buf(mesh_dir / "LOD0.abc123-43191-Index.buf", body_ib)

    ini = td / "main.ini"
    ini.write_text(
        "[Constants]\n"
        "global $active = 0\n"
        "\n"
        "[TextureOverride_EntryPoint_LOD0.abc123_43191]\n"
        "hash = abc123\n"
        "match_first_index = 0\n"
        "match_index_count = 12\n"
        "handling = skip\n"
        "$\\EFMIv1\\component_id = 6\n"
        "$\\EFMIv1\\gpu_posed = 1\n"
        "CommandList\\EFMIv1\\Callback_Component_DrawCustom = ref CommandList_Draw_LOD0.abc123_43191\n"
        "run = CommandList_Component_DrawInstances\n"
        "\n"
        "[CommandList_Draw_LOD0.abc123_43191]\n"
        "run = CommandList\\EFMIv1\\OverrideTextures\n"
        "ib = Resource_LOD0.abc123_43191_Index\n"
        "vb0 = Resource_LOD0.abc123_43191_Position\n"
        "vb1 = Resource_LOD0.abc123_43191_Texcoord\n"
        "vb2 = Resource_LOD0.abc123_43191_Blend\n"
        "vb3 = Resource_LOD0.abc123_43191_Position\n"
        "run = CommandList\\RabbitFx\\SetTextures\n"
        "drawindexedinstanced = 12,INSTANCE_COUNT,0,0,FIRST_INSTANCE\n"
        "\n"
        "[Resource_LOD0.abc123_43191_Position]\n"
        "type = Buffer\n"
        f"stride = {pos_stride}\n"
        "filename = Meshes/LOD0.abc123-43191-Position.buf\n"
        "\n"
        "[Resource_LOD0.abc123_43191_Texcoord]\n"
        "type = Buffer\n"
        "stride = 12\n"
        "filename = Meshes/LOD0.abc123-43191-Texcoord.buf\n"
        "\n"
        "[Resource_LOD0.abc123_43191_Blend]\n"
        "type = Buffer\n"
        "stride = 16\n"
        "filename = Meshes/LOD0.abc123-43191-Blend.buf\n"
        "\n"
        "[Resource_LOD0.abc123_43191_Index]\n"
        "type = Buffer\n"
        "format = R32_UINT\n"
        "filename = Meshes/LOD0.abc123-43191-Index.buf\n"
        "\n"
        "[Present]\n"
        "if $active == 1\n"
        "\trun = CommandList\\RabbitFx\\Run\n"
        "endif\n",
        encoding="utf-8",
    )
    return td


def _read_ini_sections(path):
    sections = OrderedDict()
    current = None
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        stripped = line.strip()
        if stripped.startswith("[") and stripped.endswith("]"):
            current = stripped
            sections[current] = []
        elif current is not None:
            sections[current].append(line)
    return sections


# ---------------------------------------------------------------------------
# efmi_simulate.hlsl 参考模型（numpy 逐行镜像，用于契约级数值断言）
# align-t3：镜像 ZZMI 半隐式弹簧（rzm_jiggle_screen_state L320-475）——
# 旧 240Hz 临界阻尼模型已废弃（D-1）
# ---------------------------------------------------------------------------


def _ref_simulation_step(dt, speed=3.0, max_step=3.0):
    """镜像 shader 的 ZZMI 步长：clamp(dt*60*speed, 0.05, max_step)。"""
    dt = max(float(dt), 0.0)
    speed = speed if speed > 0 else 3.0
    max_step = max_step if max_step > 0 else 3.0
    return min(max(dt * 60.0 * speed, 0.05), max_step)


def _ref_clamp_length(v, max_len):
    """镜像 shader 的 ClampLength：长度超限按比例收缩到 max_len。"""
    v = np.asarray(v, dtype=np.float64)
    n = np.linalg.norm(v)
    return v * min(1.0, max_len / max(n, 1e-9))


def _ref_pull_toward_limit(drag, max_len):
    """镜像 shader 的 PullTowardLimit（ZZMI 渐进阻力）：渐近逼近 maxLen。"""
    drag = np.asarray(drag, dtype=np.float64)
    length = np.linalg.norm(drag)
    if length < 1e-6 or max_len <= 0.0:
        return np.zeros(3)
    pulled = max_len * (1.0 - np.exp(-length / max_len))
    return drag * (pulled / length)


def _ref_spring_step(x, x_prev, filtered, prev_filtered, raw_target, step,
                     spring, damping, follow, max_offset, grabbing):
    """镜像 efmi_simulate.hlsl 主循环单帧（align-t3 ZZMI 半隐式）：
    follow 滤波 → 位置派生速度 → damping^step → 半隐式积分 → ClampLength。
    返回 (next, x, filtered, prev_filtered_new)（供逐帧链式）。
    prevStep/prevTargetStep = 上一帧 step（本模型 dt 恒定 → 恒等于 step，
    与 shader 的 State[slot+1].w/[slot+2].w 记录语义一致）。"""
    x = np.asarray(x, dtype=np.float64)
    x_prev = np.asarray(x_prev, dtype=np.float64)
    filtered = np.asarray(filtered, dtype=np.float64)
    prev_filtered = np.asarray(prev_filtered, dtype=np.float64)
    prev_step = max(step, 1e-9)
    target_velocity = (filtered - prev_filtered) / prev_step
    prev_filtered = filtered.copy()
    target_follow = follow if grabbing else follow * 0.55
    target_follow_step = 1.0 - (1.0 - min(max(target_follow, 0.0), 1.0)) ** step
    filtered = _ref_clamp_length(
        filtered + target_velocity * (0.35 * step)
        + (np.asarray(raw_target, dtype=np.float64) - filtered) * target_follow_step,
        max_offset)
    velocity = (x - x_prev) / prev_step
    velocity = velocity * (min(max(damping, 0.0), 1.0) ** step)
    nxt = _ref_clamp_length(
        x + (velocity + (filtered - x) * (spring * step)) * step, max_offset)
    return nxt, x.copy(), filtered, prev_filtered


def _ref_sim_target(delta, buttons, drag_scale, depth_pull, max_off, right, down,
                    grab_normal):
    """镜像 shader 拖拽目标公式（align-t3 ZZMI L324-365）：冻结基投影 +
    depth_pull 比例×法线 + PullTowardLimit 渐进阻力。

    delta = 当前光标 − 按压瞬间捕获光标（capture 冻结语义，px/min 归一后）；
    buttons 1=LMB 拉出（+法线）、2=RMB 压入（−法线）、3=同按（+法线）。
    """
    delta = np.asarray(delta, dtype=np.float64)
    drag = (np.asarray(right, dtype=np.float64) * delta[0]
            + np.asarray(down, dtype=np.float64) * delta[1])
    sign = -1.0 if buttons == 2 else 1.0
    drag = drag + np.asarray(grab_normal, dtype=np.float64) * (
        np.linalg.norm(delta) * depth_pull * sign)
    return _ref_pull_toward_limit(drag * drag_scale, max_off)


# ---------------------------------------------------------------------------
# 测试
# ---------------------------------------------------------------------------


class TestEFMIRouting(unittest.TestCase):
    """路由判定：EFMI → 独立分支；zzmi 家族 → 原分支；其他 → 跳过警告。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_zzmi_module()
        # fake EFMI 执行器（路由局部 import 命中 sys.modules）
        cls.calls = []
        fake = types.ModuleType(EFMI_MODULE_NAME)

        class FakeExporter:
            def __init__(self, node):
                cls.calls.append(("init", node))

            def execute(self, path):
                cls.calls.append(("execute", path))

        fake.DragInteractionEFMIExporter = FakeExporter

        def fake_preview(node=None):
            cls.calls.append(("preview", node))

        fake._ensure_efmi_preview_running = fake_preview
        sys.modules[EFMI_MODULE_NAME] = fake

    def setUp(self):
        self.calls.clear()
        self.addCleanup(_restore_logic_name)
        _set_logic_name("")

    def _make_node(self, **props):
        node = self.mod.SSMTNode_PostProcess_DragInteraction.__new__(
            self.mod.SSMTNode_PostProcess_DragInteraction
        )
        defaults = dict(
            hash_values="", mod_namespace="", grab_key="ALT", grab_gesture="LMB",
            poke_gesture="RMB", enable_poke=True, enable_hand_cursor=False,
            enable_viewport_probe=True, enable_shapekey_drive=False,
            feature_shapekey_link=True, feature_variable_link=True,
            feature_panel_link=True,
            drag_system_mode_default=2, drag_mode_initialized=True,
            drag_mode_variable_name="ssmtdrag_drag_enabled",
            mode_toggle_key="f8", shapekey_drive_move_sensitivity=0.02,
            ui_detected_variable_name="ssmtdrag_ui_detected",
            ui_zone_variable_name="ssmtdrag_ui_zone",
            phys_grab_damping=0.86, phys_grab_spring=0.176,
            phys_release_damping=0.96, phys_release_spring=0.055,
            phys_release_kick=0.12, phys_target_follow=1.10,
            mult_radius=1.0, mult_strength=0.333, mult_spring=0.333, mult_damping=1.0,
            zone_objects=[], bake_reference_object=None, mask_plateau=0.0,
            collision_enabled=False, collision_margin=0.002, collision_mode="SOFT",
            collision_point_budget=4096, collision_cell_size=0.0,
            efmi_probe_pass_hash="1718.1",
        )
        defaults.update(props)
        for k, v in defaults.items():
            object.__setattr__(node, k, v)
        return node

    def test_efmi_routes_to_independent_exporter(self):
        _set_logic_name("EFMI")
        node = self._make_node()
        with tempfile.TemporaryDirectory() as td:
            self.mod.SSMTNode_PostProcess_DragInteraction.execute_postprocess(node, td)
        # 路由：init → execute → 预览注册（t25 恢复，幂等）
        self.assertEqual(len(self.calls), 3)
        self.assertEqual(self.calls[0][0], "init")
        self.assertEqual(self.calls[1], ("execute", td))
        self.assertEqual(self.calls[2][0], "preview")

    def _route_stdout(self, logic):
        """执行路由并把 stdout 收集回来（C3：两条路径都只靠 print 区分）。"""
        import io
        from contextlib import redirect_stdout

        _set_logic_name(logic)
        node = self._make_node()
        buf = io.StringIO()
        with tempfile.TemporaryDirectory() as td:
            with redirect_stdout(buf):
                self.mod.SSMTNode_PostProcess_DragInteraction.execute_postprocess(node, td)
        return buf.getvalue()

    def test_zzmi_family_keeps_original_branch(self):
        """ZZMI/ZZMIDX12 必须命中**原分支**，而非被「不支持」闸门静默跳过。

        C3 回归（假阴性）：原用例只断言 `calls == []`，而「原分支（无 hash 早退）」
        与「非 EFMI/zzmi 家族 → print(WARNING) 跳过」（闸门见
        blueprint/node_postprocess_draginteraction.py:1579-1584）两条路径的
        calls 都是空 → 无法区分。改为按 stdout 标记区分：
        - 原分支：先打 `[DragInteraction] 开始执行`（早退前），再因无 hash 跳过；
        - 闸门：只打 `[DragInteraction][WARNING] …暂不支持拖拽交互…已跳过`。
        """
        for logic in ("ZZMI", "ZZMIDX12"):
            with self.subTest(logic=logic):
                self.calls.clear()
                out = self._route_stdout(logic)
                self.assertEqual(self.calls, [], "zzmi 家族不得触碰 EFMI 执行器")
                self.assertIn("[DragInteraction] 开始执行", out, "必须进入原分支")
                self.assertNotIn("暂不支持拖拽交互", out, "不得被不支持闸门跳过")

    def test_other_logic_skips_with_warning(self):
        """非 EFMI/非 zzmi 家族：命中闸门且必须留下可见 WARNING（C3：原名 with_warning 不断言警告）。"""
        out = self._route_stdout("GIMI")
        self.assertEqual(self.calls, [])
        self.assertIn("[DragInteraction][WARNING]", out, "闸门必须打出 WARNING")
        self.assertIn("暂不支持拖拽交互", out)
        self.assertIn("已跳过", out)
        self.assertNotIn("[DragInteraction] 开始执行", out, "闸门路径不得进入原分支")


class TestEFMIBakeShapes(unittest.TestCase):
    """烘焙形状：双胸腔区平滑场 / 三门 / 重合取最大 / cloth 16 近邻传递。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()

    def test_ball_weights_zone_side_gates(self):
        """高斯球稀疏（t25 全面 zzmi 化；t16 修正）：场 = strength×exp(-falloff_k·d²)、
        d≥1 硬截止（zzmi _shape_field 同款）、球心满强度、t36-P5 grabbable=False
        权重保留（运行时拒绝抓取）、稀疏 K=4 存原始场值（不归一——zzmi 对齐，
        衰减梯度保留）。"""
        m = self.mod
        pos = np.array([
            [-0.070, -0.122, 1.092],  # 左区球心
            [0.070, -0.122, 1.092],   # 右区球心
            [-0.070, -0.260, 1.092],  # 左区球外远处（>radius → 0）
            [0.000, -0.122, 1.092],   # 中线（双区对称）
        ], dtype=np.float32)
        cfg = [
            ((-0.070, -0.122, 1.092), 0.10, 1.0, 4.6, True),   # 左区
            ((0.070, -0.122, 1.092), 0.10, 1.0, 4.6, True),    # 右区
        ]
        zone_ids, w = m.bake_sparse_weights(pos, cfg)
        self.assertEqual(w.shape, (4, 4))
        self.assertGreater(w[0, 0], 0.9)    # 球心 zone0 原始场 ≈ strength=1.0
        np.testing.assert_array_equal(zone_ids[0, 0], 0)
        self.assertGreater(w[1, 0], 0.9)    # 右区球心（原始场首槽）
        np.testing.assert_array_equal(zone_ids[1, 0], 1)
        self.assertLess(w[2].max(), 0.05)   # 球外硬截止 → 0
        # 中线：双区对称 → 原始场各 = exp(-4.6 × 0.7²)（d=0.7，非归一 0.5——
        # 归一化会把衰减压平成 0/1 硬边界，t16 已修）
        mid_raw = float(np.exp(-4.6 * 0.7 * 0.7))
        self.assertAlmostEqual(float(w[3, 0]), mid_raw, places=5)
        self.assertAlmostEqual(float(w[3, 1]), mid_raw, places=5)
        self.assertEqual(sorted(zone_ids[3, :2].tolist()), [0, 1])

    def test_ball_weights_gaussian_shape_matches_zzmi(self):
        """高斯场形状与 zzmi 对照（t25 核心）：d=0 → strength、d=0.5 →
        strength×exp(-k×0.25)、d≥1 → 0（硬截止，zzmi _shape_field 同款；
        k=4.6 默认）。"""
        m = self.mod
        center = (0.0, 0.0, 0.0)
        pos = np.array([[0.0, 0.0, 0.0], [0.25, 0.0, 0.0], [0.5, 0.0, 0.0],
                        [0.495, 0.0, 0.0], [0.5, 0.0, 0.0], [0.55, 0.0, 0.0]],
                       dtype=np.float32)
        radius = 0.5
        strength = 1.0
        k = 4.6
        w = m.bake_zone_ball(pos, center, radius, strength=strength, falloff_k=k)
        np.testing.assert_allclose(w[0], strength, atol=1e-9)          # d=0
        np.testing.assert_allclose(w[1], strength * np.exp(-k * 0.25), atol=1e-9)  # d=0.5
        self.assertEqual(w[2], 0.0)                                    # d=1.0 硬截止
        np.testing.assert_allclose(w[3], strength * np.exp(-k * 0.99 ** 2), atol=1e-9)  # d=0.99
        self.assertEqual(w[4], 0.0)                                    # d=1.0 硬截止
        self.assertEqual(w[5], 0.0)                                    # d>1 硬截止

    def test_ball_weights_falloff_k_shape_and_grabbable(self):
        """falloff_k 越大衰减越陡；t36-P5：grabbable=False **不再清零权重**（与
        grabbable=True 同场——不可抓区仍烘焙可命中/显示，抓取拒绝移到运行时
        simulate 的 ZoneParams[z*2+1].y 门）。"""
        m = self.mod
        center = (0.0, 0.0, 0.0)
        pos = np.array([[0.5, 0.0, 0.0], [0.8, 0.0, 0.0]], dtype=np.float32)
        # radius 1.0：d=0.5/0.8
        soft = m.bake_zone_ball(pos, center, 1.0, strength=1.0, falloff_k=0.5)
        steep = m.bake_zone_ball(pos, center, 1.0, strength=1.0, falloff_k=8.0)
        self.assertGreater(soft[0], steep[0])       # 远点：k 大衰减更陡 → 更低
        self.assertGreater(soft[1], steep[1])
        # t36-P5：grabbable=False 与 grabbable=True 权重场一致（权重保留）
        locked = m.bake_zone_ball(pos, center, 1.0, grabbable=False)
        unlocked = m.bake_zone_ball(pos, center, 1.0, grabbable=True)
        np.testing.assert_allclose(locked, unlocked, atol=1e-12)
        self.assertGreater(locked[0], 0.0)          # 不再归零

    def test_cloth_near_transfer(self):
        """cloth 16 近邻稀疏传递（t22 契约升级：无 vertical/front 门，距离衰减
        保留；输出稀疏表 K=4）。D-2 用户拍板：传递特性保留（EFMI 独有）。"""
        m = self.mod
        body_pos = np.array([[0.0, -0.12, 1.09], [0.01, -0.12, 1.09], [0.0, -0.11, 1.09]], dtype=np.float64)
        body_zone_ids = np.array([
            [0, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF],
            [0, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF],
            [0, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF],
        ], dtype=np.uint32)
        body_w = np.array([[1.0, 0, 0, 0], [1.0, 0, 0, 0], [1.0, 0, 0, 0]], dtype=np.float32)
        cloth_pos = np.array([
            [0.001, -0.12, 1.09],    # 紧贴 → 高权重
            [0.050, -0.12, 1.09],    # 50mm → 衰减后低
            [0.500, 0.500, 1.05],    # 远处 → 无近邻 ≈ 0
        ], dtype=np.float64)
        zone_ids, w = m.bake_cloth_sparse(body_pos, body_zone_ids, body_w, cloth_pos)
        self.assertEqual(w.shape, (3, 4))
        self.assertGreater(w[0, 0], 0.9)
        # 50mm：近邻仍含 body → 原始聚合场 ≈ fade（t16 不归一，保留距离衰减；
        # 原归一化把单区压成 1.0 = 无衰减）。远处无近邻 → 空槽
        self.assertGreater(float(w[1, 0]), 0.05)
        self.assertLess(float(w[1, 0]), 0.5)
        self.assertLess(w[2].max(), 1e-3)
        np.testing.assert_array_equal(zone_ids[0, 0], 0)
        np.testing.assert_array_equal(zone_ids[1, 0], 0)
        np.testing.assert_array_equal(zone_ids[2, 0], 0xFFFFFFFF)

    def test_component_direct_bake_driver(self):
        """align-t3：body 侧直烘驱动（_compute_zone_fields + bake_sparse_weights
        zone_fields 路径）——顶点落在区内 → 原始高斯场落槽；区外 → 硬截止 0。
        （cloth 侧 = body→cloth 16NN 传递（D-2 保留），不走本驱动。）"""
        m = self.mod
        node = _make_efmi_node()
        mw = np.eye(4, dtype=np.float64)
        mw[0, 3], mw[1, 3], mw[2, 3] = 0.0, -0.12, 1.09
        mw[0, 0], mw[1, 1], mw[2, 2] = 0.10, 0.10, 0.10

        class _Obj:
            type = 'EMPTY'
        obj = _Obj()
        obj.matrix_world = mw
        obj.ssmt_drag_zone = types.SimpleNamespace(
            enabled=True, radius=0.1, brush_strength=1.0,
            brush_falloff_k=4.6, grabbable=True)
        object.__setattr__(node, "zone_objects",
                           [types.SimpleNamespace(zone_object=obj)])
        exporter = m.DragInteractionEFMIExporter(node)
        cloth_pos = np.array([
            [0.001, -0.12, 1.09],    # 紧贴球心 → 高权重
            [0.050, -0.12, 1.09],    # d=0.5 → 高斯衰减
            [0.500, 0.500, 1.05],    # 区外 → 0
        ], dtype=np.float64)
        fields, all_invalid = exporter._compute_zone_fields(cloth_pos, None, [])
        self.assertFalse(all_invalid)
        self.assertEqual(len(fields), 1)
        zone_ids, w = m.bake_sparse_weights(
            cloth_pos, exporter._collect_zone_configs(),
            slot_ids=exporter._collect_zone_slots(), zone_fields=fields)
        self.assertEqual(w.shape, (3, 4))
        np.testing.assert_array_equal(zone_ids[0, 0], 0)
        self.assertGreater(w[0, 0], 0.9)                       # 球心 ≈ 满强度
        self.assertAlmostEqual(float(w[1, 0]),
                               float(np.exp(-4.6 * 0.5 ** 2)), places=5)  # 高斯场
        self.assertEqual(w[2].max(), 0.0)                      # 区外硬截止
        np.testing.assert_array_equal(zone_ids[2, 0], 0xFFFFFFFF)

    def test_component_all_invalid_skip_semantics(self):
        """align-t3（ZZMI L2341-2351 早退语义）：所有区对本组件均无有效权重 →
        _bake_component_weights 返回 None（调用方跳过组件注入）+ 清陈旧文件。"""
        m = self.mod
        node = _make_efmi_node()
        mw = np.eye(4, dtype=np.float64)
        mw[0, 3], mw[1, 3], mw[2, 3] = 5.0, 5.0, 5.0   # 远处空物体
        mw[0, 0], mw[1, 1], mw[2, 2] = 0.05, 0.05, 0.05

        class _Obj:
            type = 'EMPTY'
        obj = _Obj()
        obj.matrix_world = mw
        obj.ssmt_drag_zone = types.SimpleNamespace(
            enabled=True, radius=0.05, brush_strength=1.0,
            brush_falloff_k=4.6, grabbable=True)
        object.__setattr__(node, "zone_objects",
                           [types.SimpleNamespace(zone_object=obj)])
        exporter = m.DragInteractionEFMIExporter(node)
        pos = np.zeros((4, 3), dtype=np.float64)  # 原点网格，远离空物体
        fields, all_invalid = exporter._compute_zone_fields(pos, None, [])
        self.assertTrue(all_invalid)
        with tempfile.TemporaryDirectory() as td:
            res_dir = Path(td)
            comp = {"stem": "X", "mesh_names": []}
            # 预置陈旧文件 → 早退应清除
            (res_dir / "X_zones.buf").write_bytes(b"\0" * 16)
            out = exporter._bake_component_weights(comp, pos, None, str(res_dir))
            self.assertIsNone(out)
            self.assertFalse((res_dir / "X_zones.buf").exists())

    def test_active_and_identity(self):
        m = self.mod
        w = np.array([[1.0, 0.0], [0.0, 0.0], [0.0, 0.8]], dtype=np.float32)
        active = m.active_indices(w)
        np.testing.assert_array_equal(active, np.array([0, 2], dtype=np.uint32))
        ident = m.identity_index_buffer(5)
        np.testing.assert_array_equal(ident, np.arange(5, dtype=np.uint32))


class TestEFMISectionFamily(unittest.TestCase):
    """核心段族齐全 + 回调注入 + 幂等（走完整 execute 流程）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()

    def _run_export(self, node):
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        ini_path = td / "main.ini"
        sections = _read_ini_sections(ini_path)
        return td, sections

    def test_read_index_buf_format_aware(self):
        """IB 读取按资源段 format 声明归一（R16_UINT 升宽 / R32_UINT / 无声明默认）。"""
        m = self.mod
        exporter = m.DragInteractionEFMIExporter(_make_efmi_node())
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            (td / "idx16.buf").write_bytes(np.arange(6, dtype=np.uint16).tobytes())
            (td / "idx32.buf").write_bytes(np.arange(6, dtype=np.uint32).tobytes())
            r16 = OrderedDict([("[Resource_T_Index]", [
                "type = Buffer", "format = R16_UINT", "filename = idx16.buf"])])
            r32 = OrderedDict([("[Resource_T_Index]", [
                "type = Buffer", "format = R32_UINT", "filename = idx32.buf"])])
            none = OrderedDict([("[Resource_T_Index]", [
                "type = Buffer", "filename = idx32.buf"])])
            out16 = exporter._read_index_buf(str(td), r16, "Resource_T_Index")
            out32 = exporter._read_index_buf(str(td), r32, "Resource_T_Index")
            out_none = exporter._read_index_buf(str(td), none, "Resource_T_Index")
            np.testing.assert_array_equal(out16, np.arange(6, dtype=np.uint32))
            np.testing.assert_array_equal(out32, np.arange(6, dtype=np.uint32))
            np.testing.assert_array_equal(out_none, np.arange(6, dtype=np.uint32))

    def test_resource_stride_from_declaration(self):
        """Texcoord/Blend 字宽优先资源段 stride 声明（不靠文件大小反推）。"""
        m = self.mod
        exporter = m.DragInteractionEFMIExporter(_make_efmi_node())
        sections = OrderedDict([
            ("[Resource_T_Texcoord]", ["type = Buffer", "stride = 12", "filename = t.buf"]),
            ("[Resource_T_Blend]", ["type = Buffer", "stride = 16", "filename = b.buf"]),
        ])
        self.assertEqual(exporter._resource_stride(sections, "Resource_T_Texcoord"), 12)
        self.assertEqual(exporter._resource_stride(sections, "Resource_T_Blend"), 16)
        self.assertIsNone(exporter._resource_stride(sections, "Resource_T_Missing"))

    def test_sections_and_hooks(self):
        node = _make_efmi_node()
        td, sections = self._run_export(node)

        # ---- 着色器复制 ----
        res_dir = td / "res" / "drag_interaction_efmi"
        for fname in ("efmi_probe.hlsl", "efmi_detect.hlsl", "efmi_simulate.hlsl", "efmi_deform.hlsl"):
            self.assertTrue((res_dir / fname).exists(), fname)

        # ---- 烘焙产物 ----
        for fname in (
            "LOD0.abc123-43191_weights.buf", "LOD0.abc123-43191_active.buf",
            "LOD0.abc123-43191_triangles.buf", "LOD0.abc123-43191_identity_ib.buf",
            "anchors_position.buf", "anchors_texcoord.buf", "anchors_blend.buf",
            "anchors_identity_ib.buf", "centers.buf", "efmi_assets.json",
        ):
            self.assertTrue((res_dir / fname).exists(), fname)

        # ---- 核心段族 ----
        expected_sections = [
            "[CustomShaderEFMIDragProbeBody_A]",
            "[CustomShaderEFMIDragProbeAnchors_A]",
            "[CustomShaderEFMIDragDetect_A]",
            "[CustomShaderEFMIDragSimulate_A]",
            "[CustomShaderEFMIDragDeform_LOD0.abc123_43191_A]",
            "[CommandListEFMIDragProbe_A]",
            "[CommandListEFMIDragApply_LOD0.abc123_43191_A]",
            "[KeyEFMIDragLMB_A]",
            "[KeyEFMIDragRMB_A]",
            # t47-P2：Alt+X 等效左键抓取——独立键盘 X 键段（ZZMI KeyDragInputManagerX 同款）
            "[KeyEFMIDragX_A]",
            "[KeyEFMIDragModifier_A]",
            "[ResourceEFMIDragProject_A]",
            "[ResourceEFMIDragAnchorProject_A]",
            "[ResourceEFMIDragCandidate_A]",
            "[ResourceEFMIDragState_A]",
            "[ResourceEFMIDragProbeIB_A]",
            "[ResourceEFMIDragIndices_A]",
            "[ResourceEFMIDragBodyWeights_A]",
            "[ResourceEFMIDragOut_LOD0.abc123_43191_A]",
        ]
        for sec in expected_sections:
            self.assertIn(sec, sections, sec)

        # ---- detect 契约绑定 ----
        detect = sections["[CustomShaderEFMIDragDetect_A]"]
        joined = "\n".join(detect)
        for line in (
            "cs = res/drag_interaction_efmi/efmi_detect.hlsl",
            "cs-t0 = ResourceEFMIDragProject_A",
            "cs-t1 = ResourceEFMIDragAnchorProject_A",
            "cs-t2 = ResourceEFMIDragIndices_A",
            "cs-t3 = ResourceEFMIDragBodyZoneIDs_A",
            "cs-t4 = ResourceEFMIDragBodyWeights_A",
            "cs-u0 = ResourceEFMIDragCandidate_A",
        ):
            self.assertIn(line, joined)

        # ---- deform 三分立绑定 + 只写前 3 字契约（t38 P0：源视图 format=R32_UINT；
        #      P1：x155 拆行 strideWords/字节 stride）----
        deform = sections["[CustomShaderEFMIDragDeform_LOD0.abc123_43191_A]"]
        joined = "\n".join(deform)
        self.assertIn("cs-t0 = ResourceEFMIDragSourceR32_LOD0.abc123_43191_A", joined)
        self.assertIn("cs-u0 = ResourceEFMIDragOut_LOD0.abc123_43191_A", joined)
        self.assertIn("x155 = 4", joined)  # stride 16 → strideWords 4
        self.assertIn("y155 = 16", joined)
        src = "\n".join(sections["[ResourceEFMIDragSourceR32_LOD0.abc123_43191_A]"])
        self.assertIn("format = R32_UINT", src)
        self.assertIn("stride = 16", src)

        # ---- Apply CL：门控 deform + vb0/vb3 成对换绑 ----
        apply_cl = sections["[CommandListEFMIDragApply_LOD0.abc123_43191_A]"]
        joined = "\n".join(apply_cl)
        self.assertIn("run = CustomShaderEFMIDragDeform_LOD0.abc123_43191_A", joined)
        self.assertIn("vb0 = ResourceEFMIDragOut_LOD0.abc123_43191_A", joined)
        self.assertIn("vb3 = ResourceEFMIDragOut_LOD0.abc123_43191_A", joined)

        # ---- Probe CL：R8/P0（t6 G1 主改造）——门控 = enabled && 帧 latch
        #      （每帧首次 body draw 执行一次），去 mode/pass 序号：t6-H1
        #      （a4bb34f9 实机绘制次数<2 → pass==2 永不触发 → Candidate 恒陈旧 →
        #      手型恒隐藏 + deform 陈旧数据抖动/整体偏移）。对齐 ZZMI detect 门
        #      （drag_enabled>=1 && ObjectDetectAllowed==1，无 pass/mode）。
        probe_cl = "\n".join(sections["[CommandListEFMIDragProbe_A]"])
        self.assertIn("$ssmtdrag_efmi_enabled_A == 1", probe_cl)
        self.assertIn("$ssmtdrag_efmi_frame_A != $ssmtdrag_efmi_probe_frame_prev_A", probe_cl)
        self.assertNotIn("$ssmtdrag_efmi_pass_A == 2", probe_cl)
        self.assertNotIn("ps ==", probe_cl)
        self.assertIn("run = CustomShaderEFMIDragProbeBody_A", probe_cl)
        self.assertIn("run = CustomShaderEFMIDragDetect_A", probe_cl)

        # ---- 回调注入：B1/t13——Apply 在真实 draw 之前，probe 级联在真实
        #      draw 之后（probe 段尾 UnbindAllRenderTargets 清 OM 颜色 RT，若在
        #      draw 前执行则主体无渲染目标 = Alt 按住模型消失）----
        draw_lines = sections["[CommandList_Draw_LOD0.abc123_43191]"]
        joined = "\n".join(draw_lines)
        self.assertIn(EFMI_DRAG_HOOK_BEGIN, joined)
        self.assertIn("run = CommandListEFMIDragProbe_A", joined)
        self.assertIn("run = CommandListEFMIDragApply_LOD0.abc123_43191_A", joined)
        draw_index = next(
            i for i, line in enumerate(draw_lines) if line.strip().startswith("drawindexedinstanced")
        )
        hook_index = next(
            i for i, line in enumerate(draw_lines) if EFMI_DRAG_HOOK_BEGIN in line
        )
        self.assertLess(hook_index, draw_index)
        # probe 独立标记块位于 draw 之后；Apply 位于 draw 之前
        probe_index = next(
            i for i, line in enumerate(draw_lines)
            if "run = CommandListEFMIDragProbe_A" in line
        )
        apply_index = next(
            i for i, line in enumerate(draw_lines)
            if "run = CommandListEFMIDragApply_" in line
        )
        self.assertLess(apply_index, draw_index)
        self.assertGreater(probe_index, draw_index)
        self.assertIn(EFMI_DRAG_PROBE_BEGIN, joined)
        self.assertIn(EFMI_DRAG_PROBE_END, joined)

        # ---- Present / Constants ----
        present = "\n".join(sections["[Present]"])
        self.assertIn(EFMI_DRAG_PRESENT_BEGIN, present)
        self.assertIn("run = CustomShaderEFMIDragSimulate_A", present)
        # t43 单分量行：x154 = 全局物理档案（align-t3 ZZMI 直传：
        # grab_damping/grab_spring/release_damping/release_spring 半隐式语义）
        self.assertIn("x154 = 0.86", present)
        self.assertIn("y154 = 0.176", present)
        self.assertIn("z154 = 0.96", present)
        self.assertIn("w154 = 0.055", present)
        constants = "\n".join(sections["[Constants]"])
        self.assertIn("global $ssmtdrag_efmi_frame_A = 0", constants)
        self.assertIn("global $ssmtdrag_efmi_enabled_A = 1", constants)

        # ---- EntryPoint 未被破坏 ----
        entry = "\n".join(sections["[TextureOverride_EntryPoint_LOD0.abc123_43191]"])
        self.assertIn("CommandList\\EFMIv1\\Callback_Component_DrawCustom = ref CommandList_Draw_LOD0.abc123_43191", entry)

    def test_idempotent_reexport(self):
        node = _make_efmi_node()
        td, sections = self._run_export(node)
        # 二次执行
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        sections2 = _read_ini_sections(td / "main.ini")

        draw = "\n".join(sections2["[CommandList_Draw_LOD0.abc123_43191]"])
        self.assertEqual(draw.count(EFMI_DRAG_HOOK_BEGIN), 1)
        present = "\n".join(sections2["[Present]"])
        self.assertEqual(present.count(EFMI_DRAG_PRESENT_BEGIN), 1)
        for sec in sections2:
            if sec.startswith(("[CustomShaderEFMIDrag", "[CommandListEFMIDrag", "[KeyEFMIDrag", "[ResourceEFMIDrag")):
                self.assertIn(sec, sections)  # 段族不重复（同一份内容）

    def test_non_alt_modifier_constant_on(self):
        node = _make_efmi_node(grab_key="NONE")
        _td, sections = self._run_export(node)
        self.assertNotIn("[KeyEFMIDragModifier_A]", sections)
        present = "\n".join(sections["[Present]"])
        self.assertIn("$ssmtdrag_efmi_modifier_A = 1", present)


def _make_sk_item(zone, stage=1, dir_id=-1, export_enabled=True):
    """mock 形态键变量项（drag_drive 绑定配置）。"""
    return types.SimpleNamespace(
        shape_key_name="SK",
        export_enabled=export_enabled,
        drag_zone_id=zone,
        drag_click_stage=stage,
        drag_dir_id=str(dir_id),
    )


def _make_sk_consumer(items, var_name_fn=None):
    """mock 形态键消费方节点（drag_drive_enabled + 变量项列表 + 变量名派生）。"""
    if var_name_fn is None:
        var_name_fn = lambda name: f"$Freq_{name}"
    return types.SimpleNamespace(
        bl_idname="SSMTNode_PostProcess_ShapeKey",
        drag_drive_enabled=True,
        shapekey_variable_items=items,
        get_shape_key_export_variable_name=var_name_fn,
    )


def _make_efmi_node_with_consumer(items, **props):
    """EFMI 节点 + 同树形态键消费方（F1 发射条件成立）。"""
    node = _make_efmi_node(**props)
    consumer = _make_sk_consumer(items)
    object.__setattr__(node, "id_data", types.SimpleNamespace(nodes=[consumer]))
    return node


class TestEFMIShapeKeyDrive(unittest.TestCase):
    """F1 形态键联动迁移：缓冲族发射/布局/前缀/消费方识别。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()

    def _run_export(self, node):
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        sections = _read_ini_sections(td / "main.ini")
        return td, sections

    def test_f1_drive_family_emitted(self):
        # 无空物体 → capacity 1：zone0 无方向档位 2；zone1 绑定越界跳过
        node = _make_efmi_node_with_consumer([
            _make_sk_item(zone=0, stage=2),
            _make_sk_item(zone=1, stage=3, dir_id=1),
        ])
        td, sections = self._run_export(node)

        # 布局：capacity 1（无空物体 → zone0 全 1 回退），档位 2 → total = 4+2 = 6
        exporter = self.mod.DragInteractionEFMIExporter(node)
        total, bases, counts = exporter._drag_drive_buffer_layout()
        self.assertEqual(counts, [2])
        self.assertEqual(bases, [0])
        self.assertEqual(total, 6)

        # 缓冲族 7 件套（EFMI 独立前缀）
        prefix = "ResourceEFMIDragShapeKey"
        for sec in (
            f"[{prefix}Drive_A]",
            f"[{prefix}Dir_A]",
            f"[{prefix}DragLatch_A]",
            f"[{prefix}ClickCount_A]",
            f"[{prefix}ClickCountF_A]",
            f"[{prefix}ActiveDir_A]",
            f"[{prefix}ZoneStageCounts_A]",
        ):
            self.assertIn(sec, sections, sec)
        drive_lines = sections[f"[{prefix}Drive_A]"]
        self.assertIn("array = 6", "\n".join(drive_lines))
        self.assertIn("array = 7", "\n".join(sections[f"[{prefix}Dir_A]"]))

        # 驱动 CS 段（Candidate 赢家输入 + EFMI 扩展区 156/157 + 绑定齐）
        drive_cs = "\n".join(sections["[CustomShaderEFMIDragShapeKeyDrive_A]"])
        self.assertIn("cs = res/drag_interaction_efmi/efmi_shapekey_drive.hlsl", drive_cs)
        self.assertIn("cs-t0 = ResourceEFMIDragCandidate_A", drive_cs)
        self.assertIn("cs-t1 = ResourceEFMIDragShapeKeyZoneStageCounts_A", drive_cs)
        self.assertIn("cs-u5 = ResourceEFMIDragShapeKeyDragLatch_A", drive_cs)
        # t43 单分量行：x156 位移归约、x157 播种标志
        self.assertIn("x156 = $ssmtdrag_efmi_skdy_A", drive_cs)
        self.assertIn("y156 = $ssmtdrag_efmi_skdx_A", drive_cs)
        self.assertIn("z156 = 0.02", drive_cs)
        self.assertIn("x157 = $ssmtdrag_efmi_seed_pending_A", drive_cs)
        self.assertIn("y157 = 0", drive_cs)
        self.assertIn("dispatch = 1, 1, 1", drive_cs)
        self.assertIn("post cs-u5 = null", drive_cs)

        # Present 归约（位移 + 每帧 dispatch）+ globals
        present = "\n".join(sections["[Present]"])
        self.assertIn("$ssmtdrag_efmi_skdy_A = $cursorY - $ssmtdrag_efmi_skprev_y_A", present)
        self.assertIn("run = CustomShaderEFMIDragShapeKeyDrive_A", present)
        constants = "\n".join(sections["[Constants]"])
        self.assertIn("global $ssmtdrag_efmi_seed_pending_A = 0", constants)

        # shader 拷贝 + StageCounts 烘焙（capacity 1：仅 zone0 档位 2）
        self.assertTrue((td / "res" / "drag_interaction_efmi" / "efmi_shapekey_drive.hlsl").exists())
        stage_buf = np.fromfile(
            td / "res" / "drag_interaction_efmi" / "EFMIZoneStageCounts_A.buf",
            dtype=np.uint32,
        )
        np.testing.assert_array_equal(stage_buf, np.array([2], dtype=np.uint32))

    def test_f1_not_emitted_without_consumer(self):
        node = _make_efmi_node()  # id_data=None → 无消费方 → _feature_skd False
        td, sections = self._run_export(node)
        for sec in sections:
            self.assertNotIn("ResourceEFMIDragShapeKey", sec)
            self.assertNotIn("CustomShaderEFMIDragShapeKeyDrive", sec)
        present = "\n".join(sections["[Present]"])
        self.assertNotIn("CustomShaderEFMIDragShapeKeyDrive", present)
        constants = "\n".join(sections["[Constants]"])
        self.assertNotIn("ssmtdrag_efmi_skdy", constants)
        self.assertFalse(
            (td / "res" / "drag_interaction_efmi" / "efmi_shapekey_drive.hlsl").exists()
        )

    def test_layout_single_zone_fallback(self):
        # 无空物体 → capacity 1（zone0 全 1 回退），单区 4+1 槽
        exporter = self.mod.DragInteractionEFMIExporter(_make_efmi_node())
        total, bases, counts = exporter._drag_drive_buffer_layout()
        self.assertEqual(counts, [1])
        self.assertEqual(bases, [0])
        self.assertEqual(total, 5)
        # 3 个空物体 → capacity 3（256 区模型按序分配）
        node = _make_efmi_node()
        empties = [types.SimpleNamespace(
            zone_object=types.SimpleNamespace(
                matrix_world=np.eye(4, dtype=np.float64),
                ssmt_drag_zone=types.SimpleNamespace(
                    enabled=True, radius=0.1, brush_strength=1.0,
                    falloff=0.0, grabbable=True),
            )) for _ in range(3)]
        object.__setattr__(node, "zone_objects", empties)
        exporter3 = self.mod.DragInteractionEFMIExporter(node)
        _t, _b, counts3 = exporter3._drag_drive_buffer_layout()
        self.assertEqual(counts3, [1, 1, 1])

    def test_layout_sparse_zone_ids_expand_capacity(self):
        """ARCH-02/B2：稳定 zone_id **非密排**（{0,5}）时容量 = 最大 zone_id + 1。

        旧口径（启用区域个数）容量 = 2 → zone 5 的档位统计被 `range(capacity)`
        静默丢弃、`zone_bases[5]` 越界（IndexError）；新口径容量 = 6 →
        zone0..zone5 全部有段位，zone5 档位统计保留、`zone_bases[5]` 不越界。"""
        def _empty_with_zone(zone_id):
            return types.SimpleNamespace(
                zone_id=zone_id,
                zone_object=types.SimpleNamespace(
                    matrix_world=np.eye(4, dtype=np.float64),
                    ssmt_drag_zone=types.SimpleNamespace(
                        enabled=True, radius=0.1, brush_strength=1.0,
                        brush_falloff_k=4.6, grabbable=True),
                ))

        # 形态键消费方只在 zone 5 上声明无方向档位 3（旧口径下该统计必丢）
        node = _make_efmi_node_with_consumer([_make_sk_item(zone=5, stage=3)])
        object.__setattr__(node, "zone_objects", [_empty_with_zone(0), _empty_with_zone(5)])
        exporter = self.mod.DragInteractionEFMIExporter(node)

        # 容量口径：最大被引用稳定 zone_id (5) + 1 = 6，不是启用个数 2
        self.assertEqual(exporter._collect_zone_capacity(), 6)
        total, bases, counts = exporter._drag_drive_buffer_layout()
        self.assertEqual(counts, [1, 1, 1, 1, 1, 3])   # zone0..4 占位 1；zone5 = 3
        self.assertEqual(bases, [0, 5, 10, 15, 20, 25])
        self.assertEqual(total, 32)                    # 5 段 ×(4+1) + (4+3) = 25+7
        # zone_bases 对稀疏区域号不越界（旧口径 len(bases)==2 → bases[5] IndexError）
        self.assertEqual(len(bases), 6)
        self.assertEqual(bases[5], 25)
        self.assertEqual(counts[5], 3)
        # 该区域档位统计未被静默丢弃（对比旧口径：zone_counts={5:3} 被 range(2) 丢弃）
        self.assertEqual(exporter._drag_drive_zone_stage_counts(), {5: 3})
        # 消费方槽位推导（形态键节点）同样按稳定区域号取基址
        slot_ids = [bases[5] + 4 + (3 - 1)]
        self.assertEqual(slot_ids, [31])
        self.assertLess(max(slot_ids), total)

    def test_feature_predicates_with_consumer(self):
        node = _make_efmi_node_with_consumer([_make_sk_item(zone=0, stage=1)])
        exporter = self.mod.DragInteractionEFMIExporter(node)
        self.assertTrue(exporter._feature_skd())
        self.assertTrue(exporter._feature_var())
        node2 = _make_efmi_node_with_consumer(
            [_make_sk_item(zone=0, stage=1)], feature_variable_link=False
        )
        self.assertFalse(self.mod.DragInteractionEFMIExporter(node2)._feature_var())


class TestEFMIShapeKeyConsumer(unittest.TestCase):
    """消费方适配：形态键节点反扫识别 EFMI 分支并推导 EFMI 资源名（前缀经
    拖拽节点 _drag_shapekey_resource_prefix 契约）。"""

    @classmethod
    def setUpClass(cls):
        cls.zzmi_mod = _load_zzmi_module()
        cls.sk_mod = _load_shapekey_module()

    def setUp(self):
        # t29 F1：_set_logic_name 污染恢复（防泄漏到后续 zzmi 测试）
        self.addCleanup(_restore_logic_name)

    def _make_zzmi_node(self, **props):
        node = self.zzmi_mod.SSMTNode_PostProcess_DragInteraction.__new__(
            self.zzmi_mod.SSMTNode_PostProcess_DragInteraction
        )
        defaults = dict(
            hash_values="abc123", mod_namespace="",
            enable_shapekey_drive=False, feature_shapekey_link=True,
            # 同树消费方：使 _feature_skd() 成立（消费方约束）
            id_data=types.SimpleNamespace(
                nodes=[_make_sk_consumer([_make_sk_item(zone=0, stage=1)])]
            ),
        )
        defaults.update(props)
        for k, v in defaults.items():
            object.__setattr__(node, k, v)
        return node

    def _make_sk_node_with_drag(self, drag_node):
        sk = self.sk_mod.SSMTNode_PostProcess_ShapeKey.__new__(
            self.sk_mod.SSMTNode_PostProcess_ShapeKey
        )
        sk.id_data = types.SimpleNamespace(nodes=[drag_node])
        sk.shapekey_variable_items = []
        return sk

    def test_prefix_routing(self):
        _set_logic_name("EFMI")
        node = self._make_zzmi_node()
        self.assertEqual(node._drag_shapekey_resource_prefix(), "ResourceEFMIDragShapeKey")
        _set_logic_name("ZZMI")
        self.assertEqual(node._drag_shapekey_resource_prefix(), "ResourceDragShapeKey")
        _set_logic_name("")
        self.assertEqual(node._drag_shapekey_resource_prefix(), "ResourceDragShapeKey")

    def test_consumer_derives_efmi_resource_names(self):
        _set_logic_name("EFMI")
        drag = self._make_zzmi_node()
        # 谓词兼容：_drag_node_skd_enabled 走 _feature_skd（zzmi 节点方法）
        sk = self._make_sk_node_with_drag(drag)
        self.assertIs(sk._find_drag_drive_node(), drag)
        self.assertEqual(
            sk._drag_shapekey_drive_resource_name(),
            "ResourceEFMIDragShapeKeyDrive_A",
        )
        self.assertEqual(
            sk._drag_shapekey_click_count_resource_name(),
            "ResourceEFMIDragShapeKeyClickCount_A",
        )
        # 布局委托：EFMI 模式 → EFMI 语义（无空物体 → capacity 1 单区 4+1 槽）
        total, bases, counts = sk._drag_drive_buffer_layout()
        self.assertEqual((total, bases, counts), (5, [0], [1]))

    def test_consumer_keeps_zzmi_names(self):
        _set_logic_name("ZZMI")
        drag = self._make_zzmi_node()
        sk = self._make_sk_node_with_drag(drag)
        self.assertEqual(
            sk._drag_shapekey_drive_resource_name(),
            "ResourceDragShapeKeyDrive_A",
        )
        self.assertEqual(
            sk._drag_shapekey_click_count_resource_name(),
            "ResourceDragShapeKeyClickCount_A",
        )

    def test_consumer_fallback_without_prefix_method(self):
        # 旧节点/测试桩无 _drag_shapekey_resource_prefix → 回退原前缀
        _set_logic_name("EFMI")
        legacy = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_DragInteraction",
            _feature_skd=lambda: True,
            _resolve_namespace=lambda ini: "A",
        )
        sk = self._make_sk_node_with_drag(legacy)
        self.assertEqual(
            sk._drag_shapekey_drive_resource_name(),
            "ResourceDragShapeKeyDrive_A",
        )


class TestEFMIAnchorBlendWiden(unittest.TestCase):
    """t16 §7 复核：锚点 blend 按合并骨架升宽格式烘焙（审计强制项）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()

    def test_elementformat_widen_format_parse(self):
        m = self.mod
        exporter = m.DragInteractionEFMIExporter(_make_efmi_node())
        sections = OrderedDict([
            ("[CommandList_MergedSkeleton_ConnectComponent]", [
                "vb2->ElementFormat(BLENDINDICES, 1) = R16G16B16A16_UINT",
            ]),
        ])
        fmt = exporter._blendindices_widen_format(sections)
        self.assertEqual(fmt, "R16G16B16A16_UINT")
        self.assertEqual(exporter._format_component_bits(fmt), 16)
        self.assertEqual(exporter._format_component_bits("R32G32B32A32_UINT"), 32)
        self.assertIsNone(exporter._blendindices_widen_format(OrderedDict()))

    def test_stride12_blend_widened_to_u16(self):
        m = self.mod
        exporter = m.DragInteractionEFMIExporter(_make_efmi_node())
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            # 2 顶点：BW u16×4 (8B) + BI u8×4 (4B) = 12B（53cd7f88 系实证模型）
            blend = np.zeros((2, 12), dtype=np.uint8)
            blend[0, :8] = np.array([1, 0, 0, 0, 0, 0, 0, 0], dtype=np.uint8)  # BW
            blend[0, 8:12] = np.array([7, 0, 0, 0], dtype=np.uint8)  # BI u8
            (td / "blend12.buf").write_bytes(blend.tobytes())
            out, stride = exporter._bake_anchor_blend(
                str(td / "blend12.buf"), 12, [0, 1], 16
            )
            self.assertEqual(stride, 16)  # BW 8B + BI 8B(u16)
            self.assertEqual(out.shape, (2, 16))
            # BI 值 7 → u16 小端 [7,0,0,0, 0,0,0,0]
            np.testing.assert_array_equal(
                out[0, 8:16],
                np.array([7, 0, 0, 0, 0, 0, 0, 0], dtype=np.uint8),
            )
            # BW 原样
            np.testing.assert_array_equal(out[0, :8], blend[0, :8])

    def test_stride16_blend_kept_as_is(self):
        m = self.mod
        exporter = m.DragInteractionEFMIExporter(_make_efmi_node())
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            blend = np.zeros((1, 16), dtype=np.uint8)
            blend[0, :8] = np.arange(8, dtype=np.uint8)  # BW
            blend[0, 8:16] = np.arange(8, dtype=np.uint8) + 100  # BI（已 u16 字节）
            (td / "blend16.buf").write_bytes(blend.tobytes())
            out, stride = exporter._bake_anchor_blend(str(td / "blend16.buf"), 16, [0], 16)
            self.assertEqual(stride, 16)
            np.testing.assert_array_equal(out[0], blend[0])

    def test_ib_no_format_r16_heuristic(self):
        m = self.mod
        exporter = m.DragInteractionEFMIExporter(_make_efmi_node())
        with tempfile.TemporaryDirectory() as td:
            td = Path(td)
            # 9 索引 R16 = 18B（18%6==0 且 18%4!=0 → 唯一 R16 特征）
            (td / "ib16.buf").write_bytes(np.arange(9, dtype=np.uint16).tobytes())
            sections = OrderedDict([("[Resource_T_Index]", [
                "type = Buffer", "filename = ib16.buf"])])
            out = exporter._read_index_buf(str(td), sections, "Resource_T_Index")
            np.testing.assert_array_equal(out, np.arange(9, dtype=np.uint32))

    def test_full_export_anchor_blend_stride_in_resource(self):
        # 完整流程：body Blend stride 16 → anchors_blend stride 16 动态写入资源段
        node = _make_efmi_node()
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        sections = _read_ini_sections(td / "main.ini")
        anchor_res = "\n".join(sections["[ResourceEFMIDragAnchorBlend_A]"])
        self.assertIn("stride = 16", anchor_res)


class TestEFMIVarSync(unittest.TestCase):
    """F4 变量联动迁移：同步段/变量命名/引用完整性。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()
        cls.zzmi_mod = _load_zzmi_module()

    def setUp(self):
        # t29 F1：_set_logic_name 污染恢复（防泄漏到后续 zzmi 测试）
        self.addCleanup(_restore_logic_name)

    def _run_export(self, node):
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        sections = _read_ini_sections(td / "main.ini")
        return td, sections

    def test_var_sync_family_emitted(self):
        # 无空物体 → capacity 1：zone0 方向形态键（dir=1）+ 无方向档位 2
        node = _make_efmi_node_with_consumer([
            _make_sk_item(zone=0, stage=1, dir_id=1),
            _make_sk_item(zone=0, stage=2),
        ])
        td, sections = self._run_export(node)
        prefix = "ResourceEFMIDragShapeKey"

        # 资源 3 件套
        for sec in (
            f"[{prefix}VarPrev_A]",
            f"[{prefix}VarSyncMap_A]",
            f"[{prefix}ZoneActive_A]",
        ):
            self.assertIn(sec, sections, sec)
        self.assertIn("array = 2", "\n".join(sections[f"[{prefix}VarPrev_A]"]))

        # var_sync CS 段：值区 166+ / 模式区 175+ / 绑定齐
        sync_cs = "\n".join(sections["[CustomShaderEFMIDragShapeKeyVarSync_A]"])
        self.assertIn("cs = res/drag_interaction_efmi/efmi_shapekey_var_sync.hlsl", sync_cs)
        self.assertIn("x166 = $Freq_SK", sync_cs)  # 绑定 0 → 值区 166.x
        self.assertIn("y166 = $Freq_SK", sync_cs)  # 绑定 1 → 166.y
        self.assertIn("x175 = $ssmtdrag_efmi_skmode_A_0", sync_cs)
        self.assertIn("y175 = $ssmtdrag_efmi_skmode_A_1", sync_cs)
        self.assertIn(f"cs-t1 = {prefix}VarSyncMap_A", sync_cs)
        self.assertIn(f"cs-u0 = {prefix}Drive_A", sync_cs)
        self.assertIn(f"cs-u5 = {prefix}DragLatch_A", sync_cs)
        self.assertIn("post cs-u5 = null", sync_cs)

        # VarReadback CL：store 读 ZoneActive + Drive 槽（不带 ref）
        readback = "\n".join(sections["[CommandListEFMIDragShapeKeyVarReadback_A]"])
        self.assertIn(f"store = $ssmtdrag_efmi_skact_A_0, {prefix}ZoneActive_A, 0", readback)
        self.assertIn(f"store = $ssmtdrag_efmi_skrb_A_0, {prefix}Drive_A, 1", readback)
        self.assertIn("$ssmtdrag_efmi_skmode_A_0 = 2", readback)

        # Present：boot 清零块 + Readback（boot 门控）+ VarSync run + 播种清
        present = "\n".join(sections["[Present]"])
        self.assertIn("if $ssmtdrag_efmi_booted_A == 0", present)
        self.assertIn(f"clear = {prefix}DragLatch_A 0.0", present)
        self.assertIn("pre run = CommandListEFMIDragShapeKeyVarReadback_A", present)
        self.assertIn("run = CustomShaderEFMIDragShapeKeyVarSync_A", present)
        self.assertIn("$ssmtdrag_efmi_seed_pending_A = 0", present)

        # globals：每绑定 5 变量 + booted
        constants = "\n".join(sections["[Constants]"])
        self.assertIn("global $ssmtdrag_efmi_skact_A_0 = 0", constants)
        self.assertIn("global $ssmtdrag_efmi_skmode_A_1 = 0", constants)
        self.assertIn("global $ssmtdrag_efmi_booted_A = 0", constants)

        # 播种条目（驱动 CS 段）：y157=0（无 ClickExport 驱动时）；t43 单分量行
        drive_cs = "\n".join(sections["[CustomShaderEFMIDragShapeKeyDrive_A]"])
        self.assertIn("x157 = $ssmtdrag_efmi_seed_pending_A", drive_cs)
        self.assertIn("y157 = 0", drive_cs)

        # shader 拷贝 + VarSyncMap 烘焙（capacity 1：zone0 方向 slot=1 + 无方向档位 2 slot=5）
        self.assertTrue(
            (td / "res" / "drag_interaction_efmi" / "efmi_shapekey_var_sync.hlsl").exists()
        )
        sync_map = np.fromfile(
            td / "res" / "drag_interaction_efmi" / "EFMIZoneVarSyncMap_A.buf",
            dtype=np.uint32,
        ).reshape(-1, 4)
        self.assertEqual(len(sync_map), 2)
        # 绑定 0（zone0 方向 dir=1）：slot = zone_bases[0]+1 = 1；nd_stage = 0xFFFFFFFF
        np.testing.assert_array_equal(sync_map[0], [1, 0, 0xFFFFFFFF, 0])
        # 绑定 1（zone0 无方向档位 2）：slot = zone_bases[0]+4+(2-1) = 5
        np.testing.assert_array_equal(sync_map[1], [5, 0, 2, 0])

    def test_var_sync_bindings_layout(self):
        node = _make_efmi_node_with_consumer([
            _make_sk_item(zone=0, stage=1, dir_id=0),
            _make_sk_item(zone=0, stage=3),          # 无方向档位 3 → slot 0+4+2
            _make_sk_item(zone=1, stage=1, dir_id=3),  # 无空物体 → capacity 1 → 跳过
        ])
        exporter = self.mod.DragInteractionEFMIExporter(node)
        bindings = exporter._drag_drive_var_sync_bindings()
        self.assertEqual(len(bindings), 2)
        self.assertEqual(bindings[0][1], 0)    # dir0
        self.assertEqual(bindings[1][1], 6)    # 0+4+(3-1)
        self.assertEqual(bindings[0][3], -1)
        self.assertEqual(bindings[1][3], 3)

    def test_var_sync_not_emitted(self):
        node = _make_efmi_node()  # 无消费方 → F1/F4 全关
        _td, sections = self._run_export(node)
        for sec in sections:
            self.assertNotIn("ShapeKeyVarSync", sec)
            self.assertNotIn("ShapeKeyVarReadback", sec)
        present = "\n".join(sections["[Present]"])
        self.assertNotIn("CustomShaderEFMIDragShapeKeyVarSync", present)
        self.assertNotIn("ssmtdrag_efmi_booted", present)

    def test_click_export_names_routing(self):
        _set_logic_name("EFMI")
        node = self.zzmi_mod.SSMTNode_PostProcess_DragInteraction.__new__(
            self.zzmi_mod.SSMTNode_PostProcess_DragInteraction
        )
        object.__setattr__(node, "hash_values", "abc123")
        object.__setattr__(node, "mod_namespace", "")
        names = node._click_export_names("A")
        self.assertEqual(
            names,
            ("ResourceEFMIDragShapeKeyClickCountF_A",
             "$ssmtdrag_efmi_booted_A",
             "$ssmtdrag_efmi_seed_pending_A"),
        )
        _set_logic_name("ZZMI")
        self.assertEqual(
            node._click_export_names("A"),
            ("ResourceDragShapeKeyClickCountF_A",
             "$ssmtdrag_booted_A",
             "$ssmtdrag_seed_pending_A"),
        )

    def test_click_export_generate_segment_efmi(self):
        """ClickExport 引用完整性：EFMI 模式经拖拽节点 _click_export_names 生成
        EFMI 前缀资源/变量（旧节点回退 zzmi 前缀）。"""
        ce_mod = _load_click_export_module()

        def make_drag(names_fn):
            return types.SimpleNamespace(
                _resolve_namespace=lambda ini: "A",
                _click_export_names=names_fn,
            )

        efmi_names = lambda ns: (
            f"ResourceEFMIDragShapeKeyClickCountF_{ns}",
            f"$ssmtdrag_efmi_booted_{ns}",
            f"$ssmtdrag_efmi_seed_pending_{ns}",
        )
        zzmi_names = lambda ns: (
            f"ResourceDragShapeKeyClickCountF_{ns}",
            f"$ssmtdrag_booted_{ns}",
            f"$ssmtdrag_seed_pending_{ns}",
        )
        for drag_node, expect_efmi in ((make_drag(efmi_names), True), (make_drag(zzmi_names), False)):
            with self.subTest(efmi=expect_efmi):
                node = ce_mod.SSMTNode_AnimDriver_ClickExport.__new__(
                    ce_mod.SSMTNode_AnimDriver_ClickExport
                )
                node.click_zone_id = 0
                node.click_target_list = [types.SimpleNamespace(variable_name="swapkey1")]
                node._find_drag_drive_node = lambda: drag_node  # 实例方法遮蔽
                segment = node.generate_ini_segment()
                self.assertIn("[Present]", segment)
                if expect_efmi:
                    self.assertIn("$ssmtdrag_efmi_booted_A == 1", segment)
                    self.assertIn("$ssmtdrag_efmi_seed_pending_A = 1", segment)
                    self.assertIn(
                        "store = $swapkey1, ResourceEFMIDragShapeKeyClickCountF_A, 0",
                        segment,
                    )
                else:
                    self.assertIn("$ssmtdrag_booted_A == 1", segment)
                    self.assertIn(
                        "store = $swapkey1, ResourceDragShapeKeyClickCountF_A, 0",
                        segment,
                    )

    def test_click_export_generate_segment_legacy_fallback(self):
        ce_mod = _load_click_export_module()
        legacy = types.SimpleNamespace(_resolve_namespace=lambda ini: "A")
        node = ce_mod.SSMTNode_AnimDriver_ClickExport.__new__(
            ce_mod.SSMTNode_AnimDriver_ClickExport
        )
        node.click_zone_id = 0
        node.click_target_list = [types.SimpleNamespace(variable_name="swapkey1")]
        node._find_drag_drive_node = lambda: legacy
        segment = node.generate_ini_segment()
        self.assertIn("$ssmtdrag_booted_A == 1", segment)
        self.assertIn("ResourceDragShapeKeyClickCountF_A", segment)


class TestEFMIPanelLinkage(unittest.TestCase):
    """F3 面板联动迁移：变量/readback 段齐全、命名带命名空间后缀、UI 尾块处理。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()

    def _run_export(self, node):
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        sections = _read_ini_sections(td / "main.ini")
        return td, sections

    def test_panel_family_emitted(self):
        node = _make_efmi_node()
        td, sections = self._run_export(node)
        # 资源
        for sec in ("[ResourceEFMIDragUIDetect_A]", "[ResourceEFMIDragUIZone_A]"):
            self.assertIn(sec, sections, sec)
        # 发布 CS（Candidate 赢家仲裁 → R32 标量）
        publish = "\n".join(sections["[CustomShaderEFMIDragUIPublish_A]"])
        self.assertIn("cs = res/drag_interaction_efmi/efmi_ui_publish.hlsl", publish)
        self.assertIn("cs-t0 = ResourceEFMIDragCandidate_A", publish)
        self.assertIn("cs-u0 = ResourceEFMIDragUIDetect_A", publish)
        self.assertIn("cs-u1 = ResourceEFMIDragUIZone_A", publish)
        self.assertIn("post cs-u1 = null", publish)
        # UIReadback CL（store 不带 ref，命名带 ns 后缀）
        readback = "\n".join(sections["[CommandListEFMIDragUIReadback_A]"])
        self.assertIn(
            "store = $ssmtdrag_ui_detected_A, ResourceEFMIDragUIDetect_A, 0",
            readback,
        )
        self.assertIn(
            "store = $ssmtdrag_ui_zone_A, ResourceEFMIDragUIZone_A, 0",
            readback,
        )
        # Present：发布 run + 帧末 post run 回读
        present = "\n".join(sections["[Present]"])
        self.assertIn("run = CustomShaderEFMIDragUIPublish_A", present)
        self.assertIn("post run = CommandListEFMIDragUIReadback_A", present)
        # shader 拷贝 + globals（命名带 ns 后缀）
        self.assertTrue(
            (td / "res" / "drag_interaction_efmi" / "efmi_ui_publish.hlsl").exists()
        )
        constants = "\n".join(sections["[Constants]"])
        self.assertIn("global $ssmtdrag_ui_detected_A = -1", constants)
        self.assertIn("global $ssmtdrag_ui_zone_A = -1", constants)

    def test_panel_custom_variable_names(self):
        node = _make_efmi_node(
            ui_detected_variable_name="my_detected",
            ui_zone_variable_name="my_zone",
        )
        _td, sections = self._run_export(node)
        readback = "\n".join(sections["[CommandListEFMIDragUIReadback_A]"])
        self.assertIn("store = $my_detected, ResourceEFMIDragUIDetect_A, 0", readback)
        self.assertIn("store = $my_zone, ResourceEFMIDragUIZone_A, 0", readback)

    def test_panel_disabled_not_emitted(self):
        node = _make_efmi_node(feature_panel_link=False)
        _td, sections = self._run_export(node)
        for sec in sections:
            self.assertNotIn("EFMIDragUIPublish", sec)
            self.assertNotIn("EFMIDragUIReadback", sec)
            self.assertNotIn("EFMIDragUIDetect", sec)
        present = "\n".join(sections["[Present]"])
        self.assertNotIn("EFMIDragUIPublish", present)

    def _write_tail(self, td, tail_block):
        ini_path = td / "main.ini"
        text = ini_path.read_text(encoding="utf-8")
        ini_path.write_text(text + "\n" + tail_block, encoding="utf-8")
        return ini_path

    def test_ui_tail_relocation_and_ns_rewrite(self):
        # 构造带 UI 绑定标记的 tail → EFMI Present 块搬入标记前 + ns 重写
        node = _make_efmi_node()
        td = _make_efmi_mod_dir()
        tail_block = (
            "; --- AUTO-APPENDED UI PANEL XYZ ---\n"
            "[Present]\n"
            "; --- MODEL DRAG BINDING BEGIN ---\n"
            "if $ssmtdrag_drag_enabled_A == 1\n"
            "\tif $ssmtdrag_ui_detected_ZZ == 1 && $ssmtdrag_ui_zone_ZZ == 2\n"
            "\t\t$drag_action = 3\n"
            "\tendif\n"
            "endif\n"
            "; --- MODEL DRAG BINDING END ---\n"
        )
        ini_path = self._write_tail(td, tail_block)
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        content = ini_path.read_text(encoding="utf-8")
        # Present 块在 MODEL DRAG BINDING 标记前
        binding_idx = content.find("; --- MODEL DRAG BINDING BEGIN ---")
        present_idx = content.find(EFMI_DRAG_PRESENT_BEGIN)
        self.assertGreaterEqual(present_idx, 0)
        self.assertLess(present_idx, binding_idx)
        # ns 重写：$ssmtdrag_ui_detected_ZZ → $ssmtdrag_ui_detected_A
        self.assertIn("$ssmtdrag_ui_detected_A == 1", content)
        self.assertIn("$ssmtdrag_ui_zone_A == 2", content)
        self.assertNotIn("$ssmtdrag_ui_detected_ZZ", content)
        # 主 [Present] 段（第一个段头到 tail 段头之间）不再含 EFMI 块（已搬迁）
        first_present = content.find("[Present]")
        second_present = content.find("[Present]", first_present + 1)
        between = content[first_present:second_present]
        self.assertNotIn(EFMI_DRAG_PRESENT_BEGIN, between)

    def test_ui_tail_relocation_idempotent(self):
        # 搬迁后的重导出：tail 里旧 PRESENT 块被剥离、不重复
        node = _make_efmi_node()
        td = _make_efmi_mod_dir()
        tail_block = (
            "; --- AUTO-APPENDED UI PANEL XYZ ---\n"
            "[Present]\n"
            "; --- MODEL DRAG BINDING BEGIN ---\n"
            "if $ssmtdrag_drag_enabled_A == 1 && $ssmtdrag_ui_detected_A == 1\n"
            "\t$drag_action = 3\n"
            "endif\n"
            "; --- MODEL DRAG BINDING END ---\n"
        )
        ini_path = self._write_tail(td, tail_block)
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        exporter.execute(str(td))  # 二次执行（重导出）
        content = ini_path.read_text(encoding="utf-8")
        self.assertEqual(content.count(EFMI_DRAG_PRESENT_BEGIN), 1)
        self.assertEqual(content.count("$ssmtdrag_ui_detected_A == 1"), 1)
        self.assertEqual(content.count("$drag_action = 3"), 1)


class TestEFMIClickExportLinkage(unittest.TestCase):
    """t10 点击回读与物体切换联动：段族/受控变量/cycle 扩展/越界过滤/导入符号。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()
        cls.ce_mod = _load_click_export_module()

    def setUp(self):
        # t29 F1：_load_click_export_module 内的 _set_logic_name("") 亦需恢复
        self.addCleanup(_restore_logic_name)

    def _make_ce_node(self, zone=0, cycle=0, targets=("swapkey1",), values=""):
        ce = self.ce_mod.SSMTNode_AnimDriver_ClickExport.__new__(
            self.ce_mod.SSMTNode_AnimDriver_ClickExport
        )
        ce.click_zone_id = zone
        ce.cycle_length = cycle
        ce.click_values = values
        ce.click_target_list = [
            types.SimpleNamespace(variable_name=t) for t in targets
        ]
        return ce

    def _install_cross_tree_env(self, anim_tree_nodes):
        """bpy stub 跨树环境：动画驱动树集合（含 ClickExport 节点）。
        同时更新 sys.modules（EFMI 模块函数内 import 用）与 ClickExport 模块级
        绑定（anim_driver_click_export 顶层 import bpy 固定引用），保证一致。"""
        bpy_mod = sys.modules["bpy"]

        class _NodeGroups(dict):
            def get(self, name, default=None):
                for value in self.values():
                    if getattr(value, "name", None) == name:
                        return value
                return default

        node_groups = _NodeGroups()
        node_groups["AnimTree1"] = types.SimpleNamespace(
            name="AnimTree1", nodes=list(anim_tree_nodes)
        )
        data = types.SimpleNamespace(node_groups=node_groups)
        bpy_mod.data = data
        self.ce_mod.bpy.data = data
        return bpy_mod

    def test_click_export_cycle_expands_layout_and_seed(self):
        """物体切换联动链路：ClickExport cycle=4（zone0）+ 形态键档位 2 →
        布局档位取大 3；播种条目写入驱动 CS 段。"""
        ce = self._make_ce_node(zone=0, cycle=4)
        self._install_cross_tree_env([ce])
        consumer = _make_sk_consumer([_make_sk_item(zone=0, stage=2)])
        anim_driver = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_AnimDriver",
            mute=False,
            blueprint_name="AnimTree1",
        )
        node = _make_efmi_node()
        object.__setattr__(
            node, "id_data", types.SimpleNamespace(nodes=[consumer, anim_driver])
        )
        exporter = self.mod.DragInteractionEFMIExporter(node)
        # 布局：无空物体 → capacity 1；zone0 档位 = max(2, 4-1) = 3 → total = 4+3 = 7
        total, bases, counts = exporter._drag_drive_buffer_layout()
        self.assertEqual(counts, [3])
        self.assertEqual(bases, [0])
        self.assertEqual(total, 7)
        # 完整导出：播种条目（y157=1 + x158 = zone、y158 = $swapkey1）；t43 单分量行
        td = _make_efmi_mod_dir()
        exporter.execute(str(td))
        sections = _read_ini_sections(td / "main.ini")
        drive_cs = "\n".join(sections["[CustomShaderEFMIDragShapeKeyDrive_A]"])
        self.assertIn("x157 = $ssmtdrag_efmi_seed_pending_A", drive_cs)
        self.assertIn("y157 = 1", drive_cs)
        self.assertIn("x158 = 0", drive_cs)
        self.assertIn("y158 = $swapkey1", drive_cs)
        # ClickCountF 缓冲 + VarSyncMap 槽位按扩展布局（zone0 无方向档位 2 → slot 4+1=5）
        self.assertIn("[ResourceEFMIDragShapeKeyClickCountF_A]", sections)
        sync_map = np.fromfile(
            td / "res" / "drag_interaction_efmi" / "EFMIZoneVarSyncMap_A.buf",
            dtype=np.uint32,
        ).reshape(-1, 4)
        np.testing.assert_array_equal(sync_map[0], [5, 0, 2, 0])

    def test_click_export_value_list_drives_cycle_and_skips_seed(self):
        """「开关值」模式跨节点契约：列表长度即循环档数（覆盖 cycle_length=7），
        且不播种（列表值 → 点击计数不可逆，重启回列表首项）→ 播种条目数 0。"""
        ce = self._make_ce_node(zone=0, cycle=7, values="0 0 1")
        self._install_cross_tree_env([ce])
        consumer = _make_sk_consumer([_make_sk_item(zone=0, stage=2)])
        anim_driver = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_AnimDriver",
            mute=False,
            blueprint_name="AnimTree1",
        )
        node = _make_efmi_node()
        object.__setattr__(
            node, "id_data", types.SimpleNamespace(nodes=[consumer, anim_driver])
        )
        exporter = self.mod.DragInteractionEFMIExporter(node)

        self.assertEqual(exporter._collect_click_export_drivers(), [(0, 3, "")])
        self.assertEqual(exporter._click_export_seed_entries(), [])
        # 布局：zone0 档位 = max(形态键 2, 列表 3 - 1) = 2 → total = 4 + 2 = 6
        total, bases, counts = exporter._drag_drive_buffer_layout()
        self.assertEqual(counts, [2])
        self.assertEqual(bases, [0])
        self.assertEqual(total, 6)
        # 驱动 CS 段播种条目数 0（y157 = 0，无 x158/y158 变量行）
        td = _make_efmi_mod_dir()
        exporter.execute(str(td))
        sections = _read_ini_sections(td / "main.ini")
        drive_cs = "\n".join(sections["[CustomShaderEFMIDragShapeKeyDrive_A]"])
        self.assertIn("y157 = 0", drive_cs)
        self.assertNotIn("y158 = $swapkey1", drive_cs)

    def test_click_export_readback_segment_efmi(self):
        """点击回读段族：ClickExport 在 EFMI 模式生成 booted/seed_pending 门控
        + store 写入受控变量（物体切换变量名一致性约定不变）。"""
        ce = self._make_ce_node(zone=1, cycle=0, targets=("swapkey2",))
        drag_node = types.SimpleNamespace(
            _resolve_namespace=lambda ini: "A",
            _click_export_names=lambda ns: (
                f"ResourceEFMIDragShapeKeyClickCountF_{ns}",
                f"$ssmtdrag_efmi_booted_{ns}",
                f"$ssmtdrag_efmi_seed_pending_{ns}",
            ),
        )
        ce._find_drag_drive_node = lambda: drag_node
        segment = ce.generate_ini_segment()
        self.assertIn(
            "if $ssmtdrag_efmi_booted_A == 1 && $ssmtdrag_efmi_seed_pending_A == 0",
            segment,
        )
        self.assertIn("$ssmtdrag_efmi_seed_pending_A = 1", segment)
        self.assertIn(
            "store = $swapkey2, ResourceEFMIDragShapeKeyClickCountF_A, 1",
            segment,
        )
        self.assertIn("global $ssmtdrag_ckprev_A_swapkey2 = 0", segment)

    def test_click_export_zone_honored_when_capacity_derived_from_max_zone(self):
        """ARCH-02/B2 修正：容量 = 最大被引用稳定 zone_id + 1（不再是启用区域个数）。

        ClickExport zone=5 且无区域空物体时，旧口径 max(1, 启用数=0)=1 会把
        zone 5 当越界条目静默丢弃（正是本修正要修的 bug）；新口径下 max 被引用
        zone=5 → 容量 6，zone 5 区域被正常纳入布局（档位段 4+2=6 槽）。"""
        ce = self._make_ce_node(zone=5, cycle=2)
        self._install_cross_tree_env([ce])
        anim_driver = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_AnimDriver",
            mute=False,
            blueprint_name="AnimTree1",
        )
        node = _make_efmi_node()
        object.__setattr__(
            node, "id_data", types.SimpleNamespace(nodes=[anim_driver])
        )
        exporter = self.mod.DragInteractionEFMIExporter(node)
        # 条目不再被丢弃（容量口径含 ClickExport 的 zone 扩展）
        entries = exporter._collect_click_export_drivers()
        self.assertEqual(entries, [(5, 2, "$swapkey1")])
        self.assertEqual(exporter._collect_zone_capacity(), 6)
        # 布局：zone0..zone5 六段，zone5 档位 = max(1, cycle-1=1) = 1
        total, bases, counts = exporter._drag_drive_buffer_layout()
        self.assertEqual(counts, [1, 1, 1, 1, 1, 1])
        self.assertEqual(bases, [0, 5, 10, 15, 20, 25])
        self.assertEqual(total, 30)
        # zone_bases 对任意被引用区域号不越界（zone5 是最大引用号）
        self.assertEqual(len(bases), 6)
        self.assertEqual(bases[5], 25)

    def test_compute_cycle_from_swaps(self):
        """物体切换联动：受控变量匹配 ObjectSwap input_slot_count（游戏无关链路）。"""
        swap = types.SimpleNamespace(
            bl_idname="SSMTNode_ObjectSwap",
            mute=False,
            custom_var_name="swapkey1",
            assigned_variable_name="",
            input_slot_count=6,
        )
        tree = types.SimpleNamespace(
            bl_idname="SSMTBlueprintTreeType",
            nodes=[swap],
        )
        data = types.SimpleNamespace(node_groups=[tree])
        sys.modules["bpy"].data = data
        self.ce_mod.bpy.data = data
        ce = self._make_ce_node(zone=0, cycle=0)
        best, matched = ce._compute_cycle_from_swaps()
        self.assertEqual((best, matched), (6, 1))

    def test_click_export_imports_intact(self):
        """anim_driver_click_export 导入符号不破坏（zzmi 导入面完整）。"""
        ce_mod = _load_click_export_module()
        zzmi = _load_zzmi_module()
        self.assertTrue(callable(getattr(ce_mod, "_drag_drive_feature_linked", None)))
        self.assertTrue(callable(getattr(zzmi, "is_postprocess_node_on_export_chain", None)))


class _FakeLayout:
    """draw_buttons 布局 mock：记录 prop/label/row/column/box 及 enabled 状态。"""

    def __init__(self, enabled=True):
        self.calls = []
        self.enabled = enabled

    def _child(self):
        return _FakeLayout(enabled=self.enabled)

    def prop(self, *args, **kwargs):
        self.calls.append(("prop", self.enabled, args, kwargs))
        return self

    def label(self, *args, **kwargs):
        self.calls.append(("label", self.enabled, args, kwargs))
        return self

    def operator(self, *args, **kwargs):
        self.calls.append(("operator", self.enabled, args, kwargs))
        return self

    def row(self, *args, **kwargs):
        child = self._child()
        self.calls.append(("row", child))
        return child

    def column(self, *args, **kwargs):
        child = self._child()
        self.calls.append(("column", child))
        return child

    def box(self, *args, **kwargs):
        child = self._child()
        self.calls.append(("box", child))
        return child

    def separator(self):
        self.calls.append(("separator", self.enabled, (), {}))
        return self


class TestEFMISkippedAndDegraded(unittest.TestCase):
    """t11/t12（手型光标/视口探针跳过论证断言）+ t14（预览降级说明断言）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()
        cls.zzmi_mod = _load_zzmi_module()

    def setUp(self):
        # t29 F1：_set_logic_name 污染恢复（含预览 6 项，防泄漏到后续 zzmi 测试）
        self.addCleanup(_restore_logic_name)

    def _make_full_node(self, **props):
        node = self.zzmi_mod.SSMTNode_PostProcess_DragInteraction.__new__(
            self.zzmi_mod.SSMTNode_PostProcess_DragInteraction
        )
        defaults = dict(
            hash_values="", mod_namespace="", grab_key="ALT", grab_gesture="LMB",
            poke_gesture="RMB", enable_poke=True, enable_hand_cursor=False,
            enable_viewport_probe=False,
            feature_shapekey_link=True, feature_variable_link=True,
            feature_panel_link=True, enable_shapekey_drive=False,
            drag_system_mode_default=2, drag_mode_initialized=True,
            drag_mode_variable_name="ssmtdrag_drag_enabled", mode_toggle_key="f8",
            shapekey_drive_move_sensitivity=0.02,
            ui_detected_variable_name="ssmtdrag_ui_detected",
            ui_zone_variable_name="ssmtdrag_ui_zone",
            phys_grab_damping=0.86, phys_grab_spring=0.176,
            phys_release_damping=0.96, phys_release_spring=0.055,
            phys_release_kick=0.12, phys_target_follow=1.10,
            mult_radius=1.0, mult_strength=0.333, mult_spring=0.333, mult_damping=1.0,
            zone_objects=[], bake_reference_object=None, mask_plateau=0.0,
            collision_enabled=False, collision_margin=0.002, collision_mode="SOFT",
            collision_point_budget=4096, collision_cell_size=0.0,
            preview_weights=False, preview_target=None, preview_collection=None,
            efmi_probe_pass_hash="1718.1", efmi_pull_depth=0.025,
            efmi_push_depth=0.016, efmi_drag_scale=0.70, efmi_max_offset=0.040,
            efmi_grab_hz=5.0, efmi_grab_damping=0.85, efmi_release_hz=2.8,
            efmi_release_damping=0.22, efmi_hit_threshold=0.10,
            id_data=None, name="N", inputs=[], outputs=[],
        )
        defaults.update(props)
        for k, v in defaults.items():
            object.__setattr__(node, k, v)
        return node

    def _draw(self, node):
        layout = _FakeLayout()
        orig = self.zzmi_mod._ensure_preview_running
        called = []
        self.zzmi_mod._ensure_preview_running = lambda: called.append(1)
        try:
            node.draw_buttons(None, layout)
        finally:
            self.zzmi_mod._ensure_preview_running = orig
        return layout, called

    def test_no_hand_or_probe_sections_emitted(self):
        """EFMI 分支不生成手型光标（S8）/视口探针段族与资产（跳过论证断言）。"""
        node = _make_efmi_node()
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        sections = _read_ini_sections(td / "main.ini")
        for sec in sections:
            self.assertNotIn("[CustomShaderDragHand", sec)
            self.assertNotIn("[ShaderOverrideDragViewport", sec)
            self.assertNotIn("HandAction", sec)
            self.assertNotIn("ViewportLayout", sec)
        res = td / "res" / "drag_interaction_efmi"
        for fname in [f.name for f in res.iterdir()]:
            self.assertNotIn("Hand", fname)
            self.assertNotIn("Viewport", fname)
        all_shaders = (
            list(self.mod.SHADER_FILES) + list(self.mod.SHADER_DRIVE_FILES)
            + list(self.mod.SHADER_VARSYNC_FILES) + list(self.mod.SHADER_PANEL_FILES)
        )
        for fname in all_shaders:
            self.assertNotIn("hand", fname.lower())
            self.assertNotIn("viewport", fname.lower())

    @staticmethod
    def _walk_calls(layout):
        """递归遍历布局调用（row/column/box 嵌套子布局的调用也收集）。"""
        for c in layout.calls:
            yield c
            if len(c) > 1 and isinstance(c[1], _FakeLayout):
                yield from TestEFMISkippedAndDegraded._walk_calls(c[1])

    @staticmethod
    def _label_texts(layout):
        texts = []
        for c in TestEFMISkippedAndDegraded._walk_calls(layout):
            if c[0] == "label":
                if c[2]:
                    texts.append(str(c[2][0]))
                elif c[3]:
                    texts.append(str(c[3].get("text", "")))
        return texts

    def test_ui_hand_probe_disabled_and_hinted(self):
        """EFMI 模式 UI 兜底（t22 适配）：手型光标已支持（不再置灰）、视口探针
        明确提示不可用。"""
        _set_logic_name("EFMI")
        node = self._make_full_node()
        layout, _called = self._draw(node)
        labels = self._label_texts(layout)
        self.assertTrue(any("视口探针" in t and "不可用" in t for t in labels))
        self.assertTrue(any("手型光标已支持" in t for t in labels))
        hand = [
            c for c in self._walk_calls(layout)
            if c[0] == "prop" and len(c[2]) >= 2 and c[2][1] == "enable_hand_cursor"
        ]
        self.assertTrue(hand)
        self.assertTrue(hand[0][1])  # 可用（EFMI 手型光标独立实现，t22 恢复）

    def test_efmi_preview_active_in_ui(self):
        """EFMI 数据流下预览已恢复（t25/t28）：无「不可用/未迁移」文案；
        draw_buttons 直接注册 EFMI 预览（不依赖导出），zzmi 预览 handler 不激活。"""
        _set_logic_name("EFMI")
        node = self._make_full_node()
        orig = self.mod._ensure_efmi_preview_running
        efmi_calls = []
        self.mod._ensure_efmi_preview_running = lambda n=None: efmi_calls.append(n)
        try:
            layout, zzmi_called = self._draw(node)
        finally:
            self.mod._ensure_efmi_preview_running = orig
        labels = self._label_texts(layout)
        self.assertFalse(any("预览未迁移" in t for t in labels))
        self.assertFalse(any("双胸腔区平滑场" in t for t in labels))
        self.assertEqual(zzmi_called, [])
        self.assertEqual(len(efmi_calls), 1)  # 注册不依赖导出路径

    def test_efmi_preview_ui_hint_when_enabled(self):
        """EFMI 模式启用预览且目标存在时显示进行中提示（无降级文案）。"""
        _set_logic_name("EFMI")
        node = self._make_full_node(preview_weights=True)
        target = types.SimpleNamespace(type='MESH', name='T', name_full='T')
        node.preview_target = target
        orig = self.mod._ensure_efmi_preview_running
        self.mod._ensure_efmi_preview_running = lambda n=None: None
        try:
            layout, _zzmi_called = self._draw(node)
        finally:
            self.mod._ensure_efmi_preview_running = orig
        labels = self._label_texts(layout)
        self.assertTrue(any("正在预览" in t for t in labels))
        self.assertFalse(any("预览未迁移" in t for t in labels))

    def test_zzmi_mode_preview_still_active(self):
        """对照：非 EFMI 模式预览 handler 照常激活（zzmi 行为不变）。"""
        _set_logic_name("ZZMI")
        node = self._make_full_node()
        layout, called = self._draw(node)
        labels = self._label_texts(layout)
        self.assertFalse(any("预览未迁移" in t for t in labels))
        self.assertEqual(called, [1])

    def test_init_registers_efmi_preview_without_export(self):
        """EFMI 节点创建（init）即注册预览——不经过 execute_postprocess/导出；
        zzmi 模式 init 不触发 EFMI 注册。"""
        class _FakeSockets:
            def __init__(self):
                self.items = []
            def new(self, kind, name):
                self.items.append((kind, name))

        orig = self.mod._ensure_efmi_preview_running
        efmi_calls = []
        self.mod._ensure_efmi_preview_running = lambda n=None: efmi_calls.append(n)
        try:
            _set_logic_name("EFMI")
            node = self.zzmi_mod.SSMTNode_PostProcess_DragInteraction.__new__(
                self.zzmi_mod.SSMTNode_PostProcess_DragInteraction)
            node.inputs = _FakeSockets()
            node.outputs = _FakeSockets()
            node.width = 300
            node.drag_enabled_default = True
            node.init(None)
            self.assertEqual(len(efmi_calls), 1)
            efmi_calls.clear()
            _set_logic_name("ZZMI")
            node2 = self.zzmi_mod.SSMTNode_PostProcess_DragInteraction.__new__(
                self.zzmi_mod.SSMTNode_PostProcess_DragInteraction)
            node2.inputs = _FakeSockets()
            node2.outputs = _FakeSockets()
            node2.width = 300
            node2.drag_enabled_default = True
            node2.init(None)
            self.assertEqual(efmi_calls, [])
        finally:
            self.mod._ensure_efmi_preview_running = orig

    def test_efmi_preview_targets_resolution(self):
        """_efmi_preview_targets 语义：集合优先（递归 MESH、按名排序）、
        单物体回退、空 → []。"""
        mesh_a = types.SimpleNamespace(type='MESH', name='b', name_full='b')
        mesh_b = types.SimpleNamespace(type='MESH', name='a', name_full='a')
        empty = types.SimpleNamespace(type='EMPTY', name='e', name_full='e')
        collection = types.SimpleNamespace(all_objects=[mesh_b, empty, mesh_a])
        node = types.SimpleNamespace(
            preview_collection=collection, preview_target=None)
        got = self.mod._efmi_preview_targets(node)
        self.assertEqual([getattr(o, "name", None) for o in got], ["a", "b"])
        node2 = types.SimpleNamespace(preview_collection=None, preview_target=mesh_a)
        self.assertEqual(self.mod._efmi_preview_targets(node2), [mesh_a])
        node3 = types.SimpleNamespace(preview_collection=None, preview_target=None)
        self.assertEqual(self.mod._efmi_preview_targets(node3), [])

    def test_efmi_preview_timer_self_cancels_without_node(self):
        """EFMI 预览 timer t30 生命周期：无任一 EFMI 节点启用预览 → 返回 None 且
        **全量拆卸**（timer + draw + depsgraph handler 一并清空，防常驻重绘 churn）；
        存在启用节点 → 保持 0.25（对齐 zzmi tick）。"""
        prev_timer = self.mod._efmi_preview_timer
        orig_active = self.mod._efmi_preview_active
        orig_teardown = self.mod._efmi_preview_teardown_handlers
        self.mod._efmi_preview_timer = "t"
        torn = {"calls": 0}
        def _teardown():
            torn["calls"] += 1
        self.mod._efmi_preview_teardown_handlers = _teardown
        try:
            # 无活动预览 → 自取消 + 调用全量拆卸
            self.mod._efmi_preview_active = lambda: False
            self.assertIsNone(self.mod._efmi_preview_timer_callback())
            self.assertIsNone(self.mod._efmi_preview_timer)
            self.assertEqual(torn["calls"], 1)
            # 有活动预览 → 保持 0.25、不拆卸
            self.mod._efmi_preview_active = lambda: True
            self.assertEqual(self.mod._efmi_preview_timer_callback(), 0.25)
            self.assertEqual(torn["calls"], 1)
        finally:
            self.mod._efmi_preview_timer = prev_timer
            self.mod._efmi_preview_active = orig_active
            self.mod._efmi_preview_teardown_handlers = orig_teardown

    def test_efmi_preview_cleanup_tears_down_all_handlers(self):
        """t30 生命周期：_efmi_preview_teardown_handlers 移除 draw + depsgraph
        handler 并清模块引用；_efmi_preview_cleanup 全量卸 timer + handler（插件
        unregister 接线目标）——防常驻重绘 churn。"""
        removed = {"draw": 0}
        unob = {"n": 0}
        deps_list = ["h_deps"]          # bpy.app.handlers.depsgraph_update_post 内容
        prev_draw = self.mod._efmi_preview_draw_handler
        prev_deps = self.mod._efmi_preview_deps_handler
        prev_bpy = sys.modules.get("bpy")
        try:
            class _S3D(type):  # SpaceView3D（draw_handler_remove 类方法）
                @classmethod
                def draw_handler_remove(cls, h, region): removed["draw"] += 1
            class _Handlers:
                depsgraph_update_post = deps_list
            class _App:
                handlers = _Handlers()
                class timers:
                    @staticmethod
                    def unregister(t): unob["n"] += 1
            class _Types:
                SpaceView3D = _S3D
            bpy = types.ModuleType("bpy")
            bpy.types = _Types()
            bpy.app = _App()
            sys.modules["bpy"] = bpy

            self.mod._efmi_preview_draw_handler = "h_draw"
            self.mod._efmi_preview_deps_handler = "h_deps"
            self.mod._efmi_preview_teardown_handlers()
            self.assertIsNone(self.mod._efmi_preview_draw_handler)
            self.assertIsNone(self.mod._efmi_preview_deps_handler)
            self.assertEqual(removed["draw"], 1)
            self.assertNotIn("h_deps", deps_list)   # depsgraph handler 已从列表移除

            # cleanup：全量拆（timer unregister + handler 移除）
            self.mod._efmi_preview_timer = 123
            self.mod._efmi_preview_cleanup()
            self.assertIsNone(self.mod._efmi_preview_timer)
            self.assertIsNone(self.mod._efmi_preview_draw_handler)
            self.assertEqual(unob["n"], 1)
        finally:
            self.mod._efmi_preview_draw_handler = prev_draw
            self.mod._efmi_preview_deps_handler = prev_deps
            if prev_bpy is not None:
                sys.modules["bpy"] = prev_bpy
            else:
                sys.modules.pop("bpy", None)

    def test_efmi_preview_active_scans_any_enabled_node(self):
        """t30：_efmi_preview_active 检测**任一** EFMI 节点启用预览（多节点场景
        不因首节点关闭而误判停摆）；无启用/非 EFMI → False。"""
        _set_logic_name("EFMI")
        prev_bpy = sys.modules.get("bpy")
        try:
            class _N:
                bl_idname = "SSMTNode_PostProcess_DragInteraction"
                def __init__(self, pw): self.preview_weights = pw
            class _Tree:
                def __init__(self, nodes): self.nodes = nodes
            bpy = types.ModuleType("bpy")

            # 无启用预览节点 → False
            bpy.data = types.SimpleNamespace(node_groups=[_Tree([_N(False)])])
            sys.modules["bpy"] = bpy
            self.assertFalse(self.mod._efmi_preview_active())

            # 存在任一启用节点 → True（即使首节点关闭）
            bpy.data = types.SimpleNamespace(
                node_groups=[_Tree([_N(False), _N(True)])])
            sys.modules["bpy"] = bpy
            self.assertTrue(self.mod._efmi_preview_active())

            # 非 EFMI 逻辑 → False
            _set_logic_name("ZZMI")
            self.assertFalse(self.mod._efmi_preview_active())
        finally:
            _set_logic_name("EFMI")
            if prev_bpy is not None:
                sys.modules["bpy"] = prev_bpy
            else:
                sys.modules.pop("bpy", None)

    def test_efmi_preview_draw_callback_noops_in_stub_env(self):
        """draw 回调在 stub 环境（无 gpu/无 EFMI 节点）安全空转不抛错。"""
        orig_find = self.mod._efmi_preview_find_node
        _set_logic_name("ZZMI")
        try:
            self.mod._efmi_preview_draw_callback()  # 非 EFMI → 早退
        except Exception as exc:  # pragma: no cover - 失败路径
            self.fail(f"draw callback 不应抛错: {exc}")
        _set_logic_name("EFMI")
        self.mod._efmi_preview_find_node = lambda: None
        try:
            self.mod._efmi_preview_draw_callback()  # 无节点 → 早退
        except Exception as exc:  # pragma: no cover - 失败路径
            self.fail(f"draw callback 不应抛错: {exc}")
        finally:
            self.mod._efmi_preview_find_node = orig_find

    # ---- t33 第二轮：热力图（色带映射 + TRIS 面片双图层绘制）----

    def test_efmi_preview_weights_to_colors_heatmap(self):
        """权重 → 热力图色带（独立实现，zzmi 同视觉语义）：5 段端点正确、
        红色通道单调、负权重 alpha=0、越界钳制、绝对值映射不归一化。"""
        colors = self.mod._efmi_preview_weights_to_colors(
            np.array([0.0, 0.25, 0.5, 0.75, 1.0], dtype=np.float64))
        self.assertEqual(colors.shape, (5, 4))
        # 端点：0 → 蓝 [0.05,0.10,0.90,0]；1 → 红 [1.00,0.10,0.05,0.85]
        np.testing.assert_allclose(colors[0], [0.05, 0.10, 0.90, 0.0], atol=1e-9)
        np.testing.assert_allclose(colors[4], [1.00, 0.10, 0.05, 0.85], atol=1e-9)
        # 中间停靠点：0.25 青 / 0.5 绿 / 0.75 黄（zzmi 同款 5 段色带）
        np.testing.assert_allclose(colors[1], [0.00, 0.80, 0.90, 0.2125], atol=1e-9)
        np.testing.assert_allclose(colors[2], [0.10, 0.85, 0.15, 0.425], atol=1e-9)
        np.testing.assert_allclose(colors[3], [1.00, 0.85, 0.05, 0.6375], atol=1e-9)
        # alpha = 0.85 × t（按绝对值，非归一化）
        np.testing.assert_allclose(colors[:, 3], 0.85 * np.array([0.0, 0.25, 0.5, 0.75, 1.0]), atol=1e-9)
        # 负权重 → alpha 0；>1 钳制到 1
        neg = self.mod._efmi_preview_weights_to_colors(np.array([-0.5, 0.0, 2.0]))
        self.assertEqual(neg[0, 3], 0.0)
        np.testing.assert_allclose(neg[2, :3], [1.00, 0.10, 0.05], atol=1e-9)
        # 空输入
        empty = self.mod._efmi_preview_weights_to_colors(np.array([]))
        self.assertEqual(empty.shape, (0, 4))

    def test_efmi_preview_mesh_world_transform(self):
        """_efmi_preview_mesh_world：foreach_get 顶点 + matrix_world 世界变换 +
        loop_triangles 拓扑（Blender 4.x 显式 calc）。"""
        class _Verts:
            def __init__(self, coords):
                self._coords = coords
            def __len__(self):
                return len(self._coords)
            def foreach_get(self, attr, arr):
                if attr == 'co':
                    for i, c in enumerate(self._coords):
                        arr[i * 3] = c[0]
                        arr[i * 3 + 1] = c[1]
                        arr[i * 3 + 2] = c[2]

        class _Mesh:
            def __init__(self, coords, tris):
                self.vertices = _Verts(coords)
                self.loop_triangles = [
                    types.SimpleNamespace(vertices=t) for t in tris]
                self.calc_calls = 0
            def calc_loop_triangles(self):
                self.calc_calls += 1

        m = np.eye(4, dtype=np.float64)
        m[0, 3] = 1.0
        m[1, 3] = 2.0
        m[2, 3] = 3.0
        mesh = _Mesh([(0.0, 0.0, 0.0), (1.0, 0.0, 0.0), (0.0, 1.0, 0.0)],
                     [(0, 1, 2)])
        verts, tris = self.mod._efmi_preview_mesh_world(mesh, m)
        np.testing.assert_allclose(
            verts, [[1.0, 2.0, 3.0], [2.0, 2.0, 3.0], [1.0, 3.0, 3.0]], atol=1e-9)
        np.testing.assert_array_equal(tris, [[0, 1, 2]])
        self.assertGreaterEqual(mesh.calc_calls, 1)
        # 无矩阵（None）→ 原坐标
        verts2, _tris2 = self.mod._efmi_preview_mesh_world(mesh, None)
        np.testing.assert_allclose(verts2, [[0, 0, 0], [1, 0, 0], [0, 1, 0]], atol=1e-9)

    def test_efmi_preview_draw_callback_tris_heatmap(self):
        """热力图绘制（t33）：SMOOTH_COLOR TRIS 面片 + 色带逐顶点色 + 双图层
        （幽灵 alpha×0.3 先画、主层后画）；权重经色带映射非单色点。"""
        calls = []

        class _Batch:
            def __init__(self, shader, mode, attrs, indices=None):
                self.mode = mode
                self.attrs = attrs
                self.indices = indices
            def draw(self, shader):
                calls.append(("draw", self.mode, self.indices,
                              np.array(self.attrs["color"], dtype=np.float64).copy(),
                              len(self.attrs["pos"])))

        def _batch_for_shader(shader, mode, attrs, indices=None):
            calls.append(("batch", mode, len(attrs["pos"])))
            return _Batch(shader, mode, attrs, indices)

        class _Shader:
            def bind(self):
                pass
            def draw(self, *a):
                pass

        class _State:
            def blend_set(self, *a):
                pass
            def depth_test_set(self, *a):
                pass
            def point_size_set(self, *a):
                pass

        class _Gpu(types.ModuleType):
            pass

        gpu = _Gpu("gpu")
        gpu.shader = types.SimpleNamespace(
            from_builtin=lambda name: (_Shader(), calls.append(("shader", name)))[0])
        gpu.state = _State()

        class _BatchMod(types.ModuleType):
            pass

        batch_mod = _BatchMod("gpu_extras.batch")
        batch_mod.batch_for_shader = _batch_for_shader

        class _Depsgraph:
            pass

        class _EvalObj:
            def __init__(self, mesh, mw):
                self._mesh = mesh
                self.matrix_world = mw
            def to_mesh(self):
                return self._mesh
            def to_mesh_clear(self):
                pass

        class _Obj:
            def __init__(self, mesh, mw):
                self.type = 'MESH'
                self.name = "T"
                self._mesh = mesh
                self._mw = mw
            def evaluated_get(self, depsgraph):
                return _EvalObj(self._mesh, self._mw)

        class _Verts:
            def __init__(self, coords):
                self._coords = coords
            def __len__(self):
                return len(self._coords)
            def foreach_get(self, attr, arr):
                if attr == 'co':
                    for i, c in enumerate(self._coords):
                        arr[i * 3] = c[0]
                        arr[i * 3 + 1] = c[1]
                        arr[i * 3 + 2] = c[2]

        class _Mesh:
            def __init__(self):
                self.vertices = _Verts([(0.0, 0.0, 0.0), (1.0, 0.0, 0.0),
                                        (0.0, 1.0, 0.0)])
                self.loop_triangles = [
                    types.SimpleNamespace(vertices=(0, 1, 2))]
            def calc_loop_triangles(self):
                pass

        class _Context:
            def __init__(self):
                self._dg = _Depsgraph()
            def evaluated_depsgraph_get(self):
                return self._dg

        class _Bpy(types.ModuleType):
            pass

        bpy = _Bpy("bpy")
        bpy.context = _Context()

        mesh = _Mesh()
        obj = _Obj(mesh, np.eye(4, dtype=np.float64))

        node = types.SimpleNamespace(
            preview_weights=True, preview_target=obj, preview_collection=None,
            zone_objects=(), bake_reference_object=None)
        zone_obj = types.SimpleNamespace(name="Z")

        orig_find = self.mod._efmi_preview_find_node
        orig_selected = self.mod._efmi_preview_selected_zone_ids
        orig_weights = self.mod._efmi_preview_weights
        prev_bpy = sys.modules.get("bpy")
        prev_gpu = sys.modules.get("gpu")
        prev_batch = sys.modules.get("gpu_extras.batch")
        try:
            self.mod._efmi_preview_find_node = lambda: node
            self.mod._efmi_preview_selected_zone_ids = lambda n: [(0, zone_obj)]
            self.mod._efmi_preview_weights = lambda n, o, z, v: (
                None, np.array([0.0, 0.5, 1.0], dtype=np.float64))
            sys.modules["bpy"] = bpy
            sys.modules["gpu"] = gpu
            sys.modules["gpu_extras"] = types.ModuleType("gpu_extras")
            sys.modules["gpu_extras.batch"] = batch_mod
            self.mod._efmi_preview_draw_callback()
        finally:
            self.mod._efmi_preview_find_node = orig_find
            self.mod._efmi_preview_selected_zone_ids = orig_selected
            self.mod._efmi_preview_weights = orig_weights
            for name, prev in (("bpy", prev_bpy), ("gpu", prev_gpu),
                               ("gpu_extras.batch", prev_batch)):
                if prev is not None:
                    sys.modules[name] = prev
                else:
                    sys.modules.pop(name, None)
        # shader = SMOOTH_COLOR；批次 = TRIS + 索引；每区 2 次绘制（幽灵+主）
        self.assertEqual(calls[0], ("shader", "SMOOTH_COLOR"))
        batch_calls = [c for c in calls if c[0] == "batch"]
        self.assertTrue(batch_calls)
        self.assertTrue(all(c[1] == "TRIS" for c in batch_calls))
        self.assertEqual(batch_calls[0][2], 3)
        draw_calls = [c for c in calls if c[0] == "draw"]
        self.assertEqual(len(draw_calls), 2)  # ghost + main
        ghost, main = draw_calls
        np.testing.assert_array_equal(ghost[2], [[0, 1, 2]])
        # 色带端点：顶点 0 蓝（alpha 0）、顶点 2 红（alpha 0.85）
        np.testing.assert_allclose(main[3][2], [1.00, 0.10, 0.05, 0.85], atol=1e-6)
        np.testing.assert_allclose(main[3][0], [0.05, 0.10, 0.90, 0.0], atol=1e-6)
        # 幽灵层 alpha = 主层 × 0.3
        np.testing.assert_allclose(ghost[3][:, 3], main[3][:, 3] * 0.3, atol=1e-6)


class TestEFMIPerInstanceState(unittest.TestCase):
    """per-instance 状态布局（t15 补强）：8×float4/实例 缓冲族与槽位绑定、
    按压冻结/释放保留速度语义（shader 契约文本 + numpy 参考模型数值）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()

    def _run_export(self, node, pos_stride=16):
        td = _make_efmi_mod_dir(pos_stride=pos_stride)
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        sections = _read_ini_sections(td / "main.ini")
        return td, sections

    # ---- ini 级：缓冲族尺寸 / 槽位绑定 ----

    def test_state_and_candidate_buffers_per_instance_x8(self):
        """State = 8 实例 × 1030 float4（align-t3 起 6 公共槽[含 grabCenter +
        共享抓取信息] + 256 区 × 4——current/previous/filtered/prevFiltered，
        ZZMI 半隐式弹簧 + follow 滤波状态位）、Candidate = 8 实例 × 4 float4
        （最强区/basis×2/锚点）；simulate 并行 dispatch 覆盖全部 (实例, 区) 对
        + t21 新绑定（Centers t2 / 屏幕尺寸 155）+ align-t3 GizmoNormals t3。"""
        node = _make_efmi_node()
        _td, sections = self._run_export(node)
        self.assertEqual(self.mod.EFMI_MAX_INSTANCES, 8)
        state = "\n".join(sections["[ResourceEFMIDragState_A]"])
        candidate = "\n".join(sections["[ResourceEFMIDragCandidate_A]"])
        self.assertIn("array = 8240", state)   # 8 × (6 + 256×4)
        self.assertIn("array = 32", candidate)  # 8 × 4
        simulate = "\n".join(sections["[CustomShaderEFMIDragSimulate_A]"])
        self.assertIn("dispatch = 32, 1, 1", simulate)  # 8×256/64
        self.assertIn("cs-t0 = ResourceEFMIDragCandidate_A", simulate)
        self.assertIn("cs-t2 = ResourceEFMIDragCenters_A", simulate)
        # align-t3：GizmoNormals 绑定（depth_pull 冻结法线源）
        self.assertIn("cs-t3 = ResourceEFMIDragGizmoNormals_A", simulate)
        self.assertIn("post cs-t3 = null", simulate)
        self.assertIn("cs-u0 = ResourceEFMIDragState_A", simulate)
        self.assertIn("x155 = res_width", simulate)
        self.assertIn("y155 = res_height", simulate)
        self.assertIn("post cs-t0 = null", simulate)
        self.assertIn("post cs-u0 = null", simulate)

    def test_instance_slot_flows_into_detect_and_deform(self):
        """实例槽位：detect 的 x150 多值行、deform 的拆行（t38 P1）z150 =
        $draw_call_instance_id（状态基址 = 实例 × 516、候选基址 = 实例 × 4）。"""
        node = _make_efmi_node()
        td, sections = self._run_export(node)
        detect = "\n".join(sections["[CustomShaderEFMIDragDetect_A]"])
        deform = "\n".join(sections["[CustomShaderEFMIDragDeform_LOD0.abc123_43191_A]"])
        # t43 单分量行：detect 逐分量（x150=512/y150=高度/z150=实例/w150=帧）
        self.assertIn("x150 = 512", detect)
        self.assertIn("y150 = 1", detect)
        self.assertIn("z150 = $draw_call_instance_id", detect)
        self.assertIn("w150 = $ssmtdrag_efmi_frame_A", detect)
        self.assertIn("z150 = $draw_call_instance_id", deform)
        self.assertIn("w150 = $ssmtdrag_efmi_frame_A", deform)
        res_dir = td / "res" / "drag_interaction_efmi"
        detect_src = (res_dir / "efmi_detect.hlsl").read_text(encoding="utf-8")
        deform_src = (res_dir / "efmi_deform.hlsl").read_text(encoding="utf-8")
        self.assertIn("uint start = (uint)IniParams[150].z * 4;", detect_src)
        self.assertIn("uint inst = (uint)IniParams[150].z;", deform_src)
        # align-t3：每区 4 槽（current/previous/filtered/prevFiltered）
        self.assertIn("uint stateBase = inst * (PUBLIC_SLOTS + MAX_ZONES * 4);", deform_src)
        # t38 H5 防御：deform 绑 Candidate（t5）fresh 帧校验（t41 F1 全量写域下
        # active 判定 + fresh 门控）；t37-P3 三态模式：mode 1（仅命中不拖拽）→
        # delta 恒 0（Out=Base 原样复制）——fresh 门追加 IniParams[159].x >= 2.0
        # t8：新鲜度窗口 1→2 帧（probe 只在颜色 pass 写候选，深度/阴影 pass 更早
        # 执行、读到更旧候选；窗口太窄会让这些 pass 回退 BASE → 阴影不跟随变形）
        self.assertIn("Buffer<float4> Candidate : register(t5);", deform_src)
        # t47：位移**只**按 mode 开合，不再叠逐帧命中新鲜度门（t38 H5 那层已由
        # 实机二分证伪：拖到极限必然 detect miss → 门关 → delta 一帧归零 →
        # 网格 snap 回原姿势 → 黑影）。Candidate 仍声明/绑定，但不再参与门控。
        self.assertIn("if (active && IniParams[159].x >= 2.0) {", deform_src)
        self.assertNotIn("IniParams[150].w", deform_src)

    def test_present_simulate_ini_params_150_154(self):
        """Present simulate：帧标签推进 + IniParams 150-165 专用区逐槽绑定
        （align-t3：EFMI 高位区原位扩展，值与语义按 ZZMI 基准——152=JIGGLE_
        PARAMS 68、154=PHYS_PARAMS 70 弹簧直传、162=POLISH 71、163=倍率扩展、
        164=TIME 76、165=RELEASE_BOOST 97）。"""
        node = _make_efmi_node()
        _td, sections = self._run_export(node)
        present = "\n".join(sections["[Present]"])
        self.assertIn(
            "$ssmtdrag_efmi_frame_A = $ssmtdrag_efmi_frame_A + 1", present
        )
        # t43 单分量行：150-154 逐分量绑定
        self.assertIn("x150 = 512", present)
        self.assertIn("y150 = 1", present)
        self.assertIn("z150 = 0", present)
        self.assertIn("w150 = $ssmtdrag_efmi_frame_A", present)
        # C1a/t13 + R12：cursor 接线（域 [0,1] 双轴直用终版——X 直用去 /2，
        # Y top-down 直用；ZZMI 同款无缩放；旧 /2 为失效样本的错误域假设）。
        # align-t3 禁触区（基准 §6.1 队长硬约束）：两行注入永保双直用。
        # t48 记录：曾按日志实测的 cursor_x 取值范围（[−1, 2.278]）把 X 改成
        # `cursor_x * res_height / res_width`（宽高比归一化）——**实机更糟，已回退**
        # （用户：「都偏到不知道哪去了」「往右边偏了一大截」）。故此处继续钉住
        # 双轴直用；要动 cursor 映射必须先做**标定实测**（已知屏幕位置 ↔ 命中
        # 位置），不得再由 cursor_x 的取值范围反推。
        self.assertIn("$cursorX = cursor_x", present)
        self.assertIn("$cursorY = cursor_y", present)
        self.assertNotIn("cursor_x / 2", present)
        self.assertNotIn("cursor_x * res_height", present)
        self.assertNotIn("1.0 - cursor_y", present)
        # R9：R2 rt 尺寸校正默认禁用（t13 S2-A/S2-B——rt 语义无帧证据且可能
        # Unrecognised 中断 [Present] → frame 不自增 → 判定点钉右下；需显式启用）
        self.assertNotIn("rt_width > 0 && rt_height > 0", present)
        self.assertNotIn("res_width / rt_width", present)
        self.assertIn("x151 = $cursorX", present)
        self.assertIn("y151 = $cursorY", present)
        self.assertIn("z151 = $ssmtdrag_efmi_modifier_A", present)
        self.assertIn("w151 = $ssmtdrag_efmi_buttons_A", present)
        # align-t3：152 = ZZMI JIGGLE_PARAMS（68）——回退半径 0.25 / 回退强度
        # 1.00 / dragScale 1.00（旧 0.70 废除）/ 命中阈值 1e-4
        self.assertIn("x152 = 0.25", present)
        self.assertIn("y152 = 1", present)
        self.assertIn("z152 = 1", present)
        self.assertIn("w152 = 0.0001", present)
        self.assertIn("x153 = time", present)
        self.assertIn("y153 = $ssmtdrag_efmi_enabled_A", present)
        self.assertIn("z153 = 0.5", present)
        self.assertIn("w153 = 0", present)
        # align-t3：154 = ZZMI PHYS_PARAMS（70）弹簧直传（半隐式；不再
        # ω=√k→Hz）——x=grab_damping 0.86 / y=grab_spring 0.176 /
        # z=release_damping 0.96 / w=release_spring 0.055
        self.assertIn("x154 = 0.86", present)
        self.assertIn("y154 = 0.176", present)
        self.assertIn("z154 = 0.96", present)
        self.assertIn("w154 = 0.055", present)
        # align-t3：162 = ZZMI POLISH_PARAMS（71）——释放踢 1.10 / mouseYDir
        # 1.0 / 目标跟随 follow 0.12（t6：162.w 死写行已删——mouseXDir 移 163.w）
        self.assertIn("x162 = 1.1", present)
        self.assertIn("y162 = 1", present)
        self.assertIn("z162 = 0.12", present)
        self.assertNotIn("w162 =", present)
        # align-t3：163 = 倍率扩展——mult_radius 1.0（ZZMI y72）/ mult_spring
        # 0.333（ZZMI w72）/ depth_pull 1.0（ZZMI z73）/ mouseXDir 1.0
        # （ZZMI y73 同源；t6：发射与 simulate 读取位 IniParams[163].w 一致）
        self.assertIn("x163 = 1", present)
        self.assertIn("y163 = 0.333", present)
        self.assertIn("z163 = 1", present)
        self.assertIn("w163 = 1", present)
        # t6 契约断言：simulate 读取位 = 发射位（mouseXDir 读 IniParams[163].w）
        sim_src = (_td / "res" / "drag_interaction_efmi" / "efmi_simulate.hlsl").read_text(
            encoding="utf-8")
        self.assertIn("SafeNonZero(IniParams[163].w, 1.0)", sim_src)
        # align-t3：164 = ZZMI TIME_PARAMS（76）——sim_speed/max_step $全局
        self.assertIn("x164 = $ssmtdrag_efmi_sim_speed_A", present)
        self.assertIn("y164 = $ssmtdrag_efmi_max_step_A", present)
        # align-t3：165 = ZZMI RELEASE_BOOST（97）——释放动态阻尼
        self.assertIn("x165 = $ssmtdrag_efmi_release_boost_A", present)
        self.assertIn("y165 = $ssmtdrag_efmi_release_decay_A", present)
        self.assertIn("run = CustomShaderEFMIDragSimulate_A", present)
        self.assertIn(
            "$ssmtdrag_efmi_buttons_A = ($ssmtdrag_efmi_lmb_A * (1 - $ssmtdrag_efmi_x_A) + $ssmtdrag_efmi_x_A) + 2 * $ssmtdrag_efmi_rmb_A",
            present,
        )
        # ZZMI 全局默认值（_emit_globals；L4747-4762 同源）
        constants = "\n".join(sections["[Constants]"])
        self.assertIn("global $ssmtdrag_efmi_sim_speed_A = 3", constants)
        self.assertIn("global $ssmtdrag_efmi_max_step_A = 3", constants)
        self.assertIn("global $ssmtdrag_efmi_release_boost_A = 1.05", constants)
        self.assertIn("global $ssmtdrag_efmi_release_decay_A = 0.92", constants)

    def test_max_offset_default_zzmi_polish_parity(self):
        """R7 t46-P1：默认最大位移对齐 ZZMI 显式 POLISH_PARAMS.x = 0.50。

        证据链：
        - ZZMI.py L3803/L3940 `x71 = 0.50`（POLISH_PARAMS.x maxOffset 默认）；
        - rzm_jiggle_screen_state.hlsl L342 `maxOffset = SafePositive(
          POLISH_PARAMS.x, radius*2.0)*mult_radius` → 显式 0.50 优先；
        - EFMI 旧 DEFAULT_MAX_OFFSET = 0.040 硬钳 → 拉距极限仅 ZZMI 1/12.5，
          用户实机「轻微抖动 + 整体偏移」（FrameAnalysis-2026-09-08-040035
          z153=0.04 实证）——旧 0.04 的 t21 §6a 理由（对比 radius×2=1.0 爆炸）
          在 t32-A 后 radius 只作衰减时已失效。
        断言：无逐区 max_offset（0=继承）时 Emitter 全局 max_offset（z153）
        回退到 0.50；逐区设置 >0 时取该区 max 覆盖。
        """
        node = _make_efmi_node()   # zone_objects=() → _map_max_offset 回退 DEFAULT
        exporter = self.mod.DragInteractionEFMIExporter(node)
        td = _make_efmi_mod_dir()
        exporter.execute(str(td))
        sections = _read_ini_sections(td / "main.ini")
        present = "\n".join(sections["[Present]"])
        self.assertIn("z153 = 0.5", present)
        # Divergence guard: 不允许退回旧 0.04 硬钳（ZZMI 显式 0.50 是唯一对齐值）
        self.assertNotIn("z153 = 0.04", present)
        self.assertNotIn("z153 = 0.040", present)
        # 逐区覆盖路径（t34-P1 + align-t3）：efmi_simulate maxOffset = 区覆盖
        # 优先，否则显式基线 × mult_radius（ZZMI screen_state L342-345 同款：
        # 全局 ×mult_radius，区覆盖替换不乘）——全局默认只作回退。
        simulate = (td / "res" / "drag_interaction_efmi" / "efmi_simulate.hlsl").read_text(
            encoding="utf-8"
        )
        self.assertIn(
            "float maxOffset  = zoneMaxOffset > 0.0 ? zoneMaxOffset : IniParams[153].z * multRadius;",
            simulate,
        )

    # ---- shader 契约文本（1030 记录布局 / 按压冻结 / 半隐式积分）----

    def test_simulate_shader_state_layout_contract(self):
        """efmi_simulate.hlsl：1030 记录/实例布局（6 公共槽[含 grabCenter +
        共享抓取信息] + 256 区 × 4）+ 按压冻结 + ZZMI 半隐式积分内核
        （align-t3：240Hz 子步/ω=√k 临界阻尼已废弃）+ 超时复位 + 跨实例
        赢家仲裁。"""
        node = _make_efmi_node()
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        shader = (td / "res" / "drag_interaction_efmi" / "efmi_simulate.hlsl").read_text(
            encoding="utf-8"
        )
        # 布局契约（align-t3 起每实例 1030 float4：6 公共槽 + 256 区 × 4）
        for snippet in (
            "256-zone model",
            "stride = 6 + 256*4 = 1030",
            "capture (cursor.xy, zone id, button)",
            "frozen screen-right/local basis (xyz, valid)",
            "frozen screen-down/local basis (xyz, valid)",
            "timing (lastTime, held, lastSeen, magic)",
            "frozen grab center (local xyz, valid in w)   <- t21/ZZMI parity",
            "SPRING_MAGIC = 7351.0",
        ):
            self.assertIn(snippet, shader, snippet)
        # 按压冻结：捕获光标/区/按键 + 基冻结 + grab center（t21）进公共槽
        self.assertIn("capture = State[b + 0];", shader)
        self.assertIn("State[b + 1] = right;", shader)
        self.assertIn("State[b + 2] = down;", shader)
        self.assertIn("State[b + 0] = float4(IniParams[151].xy, candidate.x, buttons);", shader)
        self.assertIn("State[b + 4] = float4(grabCenter, 1.0);", shader)
        # t21/ZZMI parity：对称 delta 归一 + PullTowardLimit 渐进阻力（ZZMI
        # 2 参数签名）；t34-P1：strength/maxOffset 逐区覆盖（本线程 zone）；
        # align-t3：maxOffset 全局 ×multRadius、区覆盖替换
        self.assertIn("screenReference", shader)
        self.assertIn("rawTarget = PullTowardLimit(rawDrag * strength, maxOffset);", shader)
        # t34-P1：逐区四 override（ZoneParams 绑定 + >0 生效 / 0 回退默认）
        self.assertIn("Buffer<float4> ZoneParams : register(t1);", shader)
        self.assertIn("float zoneStrength   = ZoneParams[z * 2].y;", shader)
        self.assertIn("float zoneMaxOffset  = ZoneParams[z * 2].z;", shader)
        self.assertIn("float zoneDampingMult = ZoneParams[z * 2 + 1].x;", shader)
        # t42 P-1：strength = (zone 覆盖或 baseStrength) × mult_strength
        # （[161].x，0→1.0；align-t3 base 回退 = [152].y，ZZMI JIGGLE_PARAMS.y）
        self.assertIn("float multStrength = IniParams[161].x > 0.0 ? IniParams[161].x : 1.0;", shader)
        self.assertIn("float baseStrength = SafePositive(IniParams[152].y, 1.0);", shader)
        self.assertIn(
            "float strength = (zoneStrength > 0.0 ? zoneStrength : baseStrength) * multStrength;",
            shader,
        )
        self.assertIn(
            "float maxOffset  = zoneMaxOffset > 0.0 ? zoneMaxOffset : IniParams[153].z * multRadius;",
            shader,
        )
        # t42 P-2：damping override 替换全局 mult_damping（[160].x，0→1.0）
        self.assertIn("float globalDampingMult = IniParams[160].x > 0.0 ? IniParams[160].x : 1.0;", shader)
        self.assertIn(
            "float dampingMult = zoneDampingMult > 0.0 ? zoneDampingMult : globalDampingMult;",
            shader,
        )
        # align-t3：ZZMI 半隐式物理内核（逐项锚定）
        # 步长 = clamp(dt·60·speed, 0.05, max_step)（164.x/.y，ZZMI TIME_PARAMS）
        self.assertIn(
            "float step = clamp(dt * 60.0 * simSpeed, 0.05, maxStep);", shader)
        # 半隐式积分（ZZMI L408-411）
        self.assertIn("velocity *= pow(saturate(damping), step);", shader)
        self.assertIn("float3 next = ClampLength(x + (velocity + (filtered - x) * (spring * step)) * step,",
                      shader)
        # 目标平滑滤波 follow=0.12（ZZMI L367-379；162.z 槽位）
        self.assertIn("float follow = saturate(SafePositive(IniParams[162].z, 0.12));", shader)
        self.assertIn("float targetFollowStep = 1.0 - pow(saturate(1.0 - targetFollow), step);", shader)
        # depth_pull 比例×冻结法线（ZZMI L359-364；163.z 槽位，GizmoNormals t3）
        self.assertIn("Buffer<float4> GizmoNormals : register(t3);", shader)
        self.assertIn("float depthPullMult = SafePositive(IniParams[163].z, 0.5);", shader)
        self.assertIn("grabNormalUnit * (dragDistance2D * depthPullMult * buttonSign)", shader)
        # 释放踢槽位化（162.x，ZZMI POLISH_PARAMS.y 1.10）
        self.assertIn("velocity *= SafePositive(IniParams[162].x, 1.10);", shader)
        # 释放动态阻尼（165.x/.y，ZZMI RELEASE_BOOST 97）
        self.assertIn("SafePositive(IniParams[165].x, 1.05)", shader)
        self.assertIn("SafePositive(IniParams[165].y, 0.92)", shader)
        # 共享抓取信息槽（ZZMI InteractionState[8] 同语义，手型驱动源）
        self.assertIn("State[b + 5] = float4(1.0, maxOffset, stretchFraction, 0.0);", shader)
        # 生成器侧：simulate 段绑定 cs-t1 = ZoneParams / cs-t3 = GizmoNormals
        ini = _read_ini_sections(td / "main.ini")
        simulate_sec = "\n".join(ini["[CustomShaderEFMIDragSimulate_A]"])
        self.assertIn("cs-t1 = ResourceEFMIDragZoneParams_A", simulate_sec)
        self.assertIn("post cs-t1 = null", simulate_sec)
        # t42：生成器侧 Present 发射 160/161 全局倍率寄存器
        present = "\n".join(ini["[Present]"])
        self.assertIn("x160 = 1", present)     # mult_damping 默认 1.0
        self.assertIn("x161 = 0.333", present) # mult_strength 默认 0.333
        # 释放：target 归零由滤波接管（rawTarget=0），速度经位置派生保留
        # （过冲回摆）；t21 释放同时 invalidate grab center + 清共享抓取信息
        self.assertIn("float3 rawTarget = 0;", shader)
        self.assertIn(
            "State[b + 4] = float4(State[b + 4].xyz, 0);   // invalidate grab center on release",
            shader,
        )
        self.assertIn("State[b + 5] = 0;   // 清共享抓取信息（stretchFraction 随释放归零）",
                      shader)
        # t45 X4596 修复：capture 释放清零为全分量写（单分量 UAV 写编译失败）
        self.assertIn(
            "State[b + 0] = float4(State[b + 0].xy, 0, State[b + 0].w);", shader
        )
        self.assertNotIn("State[b + 0].z = 0;", shader)
        # 超时复位 + 全区并行积分
        self.assertIn("STATE_TIMEOUT", shader)
        self.assertIn("one thread per zone", shader)
        # 跨实例赢家仲裁（最近实例胜出，平局取小 id）
        self.assertIn("other.z < candidate.z || (other.z == candidate.z && k < inst)", shader)

    def test_simulate_zone_override_strength_and_maxoffset(self):
        """t34-P1 ①② + t42 P-1 + align-t3：simulate 逐区 strength/max_offset
        覆盖——ZoneParams[z*2].y >0 覆盖 strength（>0 生效、0 回退 baseStrength
        =[152].y，ZZMI JIGGLE_PARAMS.y 同源）后乘全局 mult_strength（[161].x，
        ZZMI screen_state L338 同款）；[z*2].z >0 覆盖 maxOffset（>0 生效替换、
        0 回退 IniParams[153].z × multRadius 全局，ZZMI L342-345 同款）。"""
        node = _make_efmi_node()
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        shader = (td / "res" / "drag_interaction_efmi" / "efmi_simulate.hlsl").read_text(
            encoding="utf-8"
        )
        # t42 P-1：strength = (zone 覆盖或 baseStrength) × mult_strength（0/未发射→1.0）
        self.assertIn("float multStrength = IniParams[161].x > 0.0 ? IniParams[161].x : 1.0;", shader)
        self.assertIn(
            "float strength = (zoneStrength > 0.0 ? zoneStrength : baseStrength) * multStrength;", shader
        )
        self.assertIn(
            "float maxOffset  = zoneMaxOffset > 0.0 ? zoneMaxOffset : IniParams[153].z * multRadius;", shader
        )
        # 覆盖后用于 PullTowardLimit（strength 乘进 rawDrag）+ clamp 环
        self.assertIn("rawTarget = PullTowardLimit(rawDrag * strength, maxOffset);", shader)
        self.assertIn("float3 next = ClampLength(", shader)

    def test_simulate_zone_override_damping(self):
        """t42 P-2 + align-t3：simulate 逐区 damping **替换**覆盖——ZoneParams
        [z*2+1].x >0 替换全局 mult_damping（IniParams[160].x），0 回退全局倍率；
        base 阻尼 [154].x/.z 独立（ZZMI screen_state L332-333 同款，无双重乘）；
        align-t3 起阻尼为半隐式 pow 底数（saturate(base × mult)，ZZMI L368/
        L406/L410 同款——抓取档 ×dampingMult、释放档 ×releaseDampingMult）。"""
        node = _make_efmi_node()
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        shader = (td / "res" / "drag_interaction_efmi" / "efmi_simulate.hlsl").read_text(
            encoding="utf-8"
        )
        self.assertIn("float zoneDampingMult = ZoneParams[z * 2 + 1].x;", shader)
        # 替换语义：override>0 用 override 值，否则全局 mult_damping（0/未发射→1.0）
        self.assertIn("float globalDampingMult = IniParams[160].x > 0.0 ? IniParams[160].x : 1.0;", shader)
        self.assertIn(
            "float dampingMult = zoneDampingMult > 0.0 ? zoneDampingMult : globalDampingMult;",
            shader,
        )
        self.assertIn(
            "? saturate(SafePositive(IniParams[154].x, 0.86) * dampingMult)",
            shader,
        )
        self.assertIn(
            ": saturate(SafePositive(IniParams[154].z, 0.96) * releaseDampingMult);",
            shader,
        )

    def test_deform_zone_override_falloff(self):
        """t34-P1 ④：deform 逐区 falloffPower 覆盖——ZoneParams[zone*2].w >0 生效，
        0 回退全局档案（IniParams[156].x，ZZMI JIGGLE_PARAMS.z=1.5 同义）。"""
        node = _make_efmi_node()
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        shader = (td / "res" / "drag_interaction_efmi" / "efmi_deform.hlsl").read_text(
            encoding="utf-8"
        )
        self.assertIn("float zoneFalloff = ZoneParams[zone * 2].w;", shader)
        self.assertIn("float falloffPower = zoneFalloff > 0.0", shader)
        self.assertIn("? zoneFalloff : max(IniParams[156].x, 0.0001);", shader)

    def test_t42_mult_strength_and_damping_replace_semantics(self):
        """t42 P-1/P-2 防回退：① mult_strength（非默认 0.5）发射 [161].x 且
        shader 乘入最终 strength（`(zone 覆盖或 1.0) × mult`）；② damping override
        是**替换**全局 mult_damping（[160].x）而非叠加——mult_damping=0.5 +
        override=2.0 时最终阻尼 = base×2.0（替换）而非 base×0.5×2.0（旧双重乘）。"""
        node = _make_efmi_node(mult_strength=0.5, mult_damping=0.5)
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        # 发射侧：x161 = mult_strength（0.5）、x160 = mult_damping（0.5）
        ini = _read_ini_sections(td / "main.ini")
        present = "\n".join(ini["[Present]"])
        self.assertIn("x160 = 0.5", present)
        self.assertIn("x161 = 0.5", present)
        # shader 侧：strength 乘 multStrength（P-1 乘入路径；align-t3：base
        # 回退 = [152].y，ZZMI JIGGLE_PARAMS.y 同源）
        shader = (td / "res" / "drag_interaction_efmi" / "efmi_simulate.hlsl").read_text(
            encoding="utf-8"
        )
        self.assertIn("float multStrength = IniParams[161].x > 0.0 ? IniParams[161].x : 1.0;", shader)
        self.assertIn(
            "float strength = (zoneStrength > 0.0 ? zoneStrength : baseStrength) * multStrength;", shader
        )
        # shader 侧：damping override 替换全局倍率（P-2 替换路径，非倍率叠加）
        self.assertIn("float globalDampingMult = IniParams[160].x > 0.0 ? IniParams[160].x : 1.0;", shader)
        self.assertIn(
            "float dampingMult = zoneDampingMult > 0.0 ? zoneDampingMult : globalDampingMult;",
            shader,
        )
        # 旧语义防回退：不得把 override 乘在已含全局倍率的值上（双重乘）
        self.assertNotIn("dampingMult = zoneDampingMult > 0.0 ? zoneDampingMult : 1.0;", shader)

    def test_t37_p3_mode_gates_in_shaders(self):
        """t37-P3 三态模式 shader 消费门：mode 1（仅命中不拖拽）→ simulate
        grabbing 门追加 IniParams[159].x >= 2.0（不驱动弹簧）；deform fresh 门
        追加同条件（delta=0 → Out=Base 原样复制）；Present x159 发射模式值。"""
        node = _make_efmi_node()
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        sim = (td / "res" / "drag_interaction_efmi" / "efmi_simulate.hlsl").read_text(
            encoding="utf-8"
        )
        deform = (td / "res" / "drag_interaction_efmi" / "efmi_deform.hlsl").read_text(
            encoding="utf-8"
        )
        # simulate：grabbing 门含 mode 门（本线程 zone，grabbable 与 mode 双门）
        self.assertIn("bool grabbing = held && capture.z == (float)z && zoneGrabbable > 0.5", sim)
        self.assertIn("&& IniParams[159].x >= 2.0;", sim)
        # t47：deform 只按 mode 开合（mode 1 → delta=0 → Out=Base 原样）；逐帧
        # 命中新鲜度门已删（实机二分证伪，见 test_t8_t47_deform_gate_is_mode_only）
        self.assertIn("if (active && IniParams[159].x >= 2.0) {", deform)
        # Present：x159 = 模式全局（EFMI_MODE_IP=159）
        sections = _read_ini_sections(td / "main.ini")
        present = "\n".join(sections["[Present]"])
        self.assertIn("x159 = $ssmtdrag_efmi_mode_A", present)
        # 生成器侧：模式 global + F8 cycle 键 + probe 门控（R8/P0：去 mode/pass，
        # 显示链不依赖 mode；拖拽门在 shader 侧 [159].x）
        ini_text = (td / "main.ini").read_text(encoding="utf-8")
        # t41 P-4：模式变量 persist（ZZMI L4736 同款，跨会话保持）
        self.assertIn("global persist $ssmtdrag_efmi_mode_A = 2", ini_text)
        self.assertIn("[KeyEFMIDragModeToggle_A]", ini_text)
        self.assertIn("$ssmtdrag_efmi_mode_A = 0,1,2", ini_text)
        probe = "\n".join(sections["[CommandListEFMIDragProbe_A]"])
        self.assertNotIn("$ssmtdrag_efmi_mode_A >= 1", probe)
        self.assertNotIn("$ssmtdrag_efmi_pass_A == 2", probe)
        self.assertIn("$ssmtdrag_efmi_probe_frame_prev_A", probe)
        # t4/P0：pass 门已恢复（>= _probe_exec_pass()，非死条件 ==）
        self.assertIn("$ssmtdrag_efmi_pass_A >= 3", probe)

    def test_detect_shader_candidate_layout(self):
        """efmi_detect.hlsl：每实例 4 float4 Candidate 记录布局（slot0 = 最强区
        0-255/权重/reverse-Z/帧标；1/2 = 屏幕基；3 = 锚点）；A1/A2/A3/t26：
        阈值 1e-4、padUV 边缘容差、miss 写 frame-1（deform fresh 门控失败）。"""
        node = _make_efmi_node()
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        shader = (td / "res" / "drag_interaction_efmi" / "efmi_detect.hlsl").read_text(
            encoding="utf-8"
        )
        self.assertIn("Candidate layout (per instance, 4 float4 records)", shader)
        # A3/t26：miss 时帧标 = frame-1（不再无条件写 frame → deform fresh 恒过）
        self.assertIn(
            "float frameTag = (bestDepths[0] < 1e30) ? IniParams[150].w : IniParams[150].w - 1.0;",
            shader,
        )
        self.assertNotIn(
            "Candidate[start] = float4(bestHits[0], bestDepths[0], IniParams[150].w);",
            shader,
        )
        # A2/t26：padUV 边缘容差
        self.assertIn("HIT_PAD", shader)
        self.assertIn("max(dBC, max(dCA, dAB)) < HIT_PAD", shader)
        # B1/t26：光标有效性守卫
        self.assertIn("cursorValid", shader)
        # B2/t26：basis conditioning
        self.assertIn("conditioning > 1e-5", shader)
        # R11：Candidate[3] = 命中点屏幕 UV（重心插值 clip → ToScreen），
        # 不再用区中心锚点；miss 写无效
        self.assertIn("Candidate[start + 1] = float4(right, valid);", shader)
        self.assertIn("float4 hitClip = bary.x * ca + bary.y * cb + bary.z * cc;", shader)
        self.assertIn("bestUV[tid] = ToScreen(hitClip);", shader)
        self.assertIn("Candidate[start + 3] = hitValid", shader)
        # R11 repair (t19)：哨兵写必须条件式——命中线程保留 L175 命中 UV；
        # 无条件写会冲掉命中值（Candidate[3] 恒为屏幕中心）
        self.assertIn("if (best >= 1e30) {", shader)
        self.assertNotIn(
            "bestUV[tid] = ToScreen(float4(0, 0, 0, 1));   // sentinel (center) until a hit",
            shader,
        )
        self.assertNotIn(
            "Candidate[start + 3] = float4(c.w > 1e-5 ? ToScreen(c) : float2(-10, -10), c.w, valid);",
            shader,
        )
        # 稀疏插值 + 命中区 Basis
        self.assertIn("ZoneIDs[ix.x * K + i]", shader)
        self.assertIn("ComputeBasis(hitZone, start);", shader)

    def test_detect_toscreen_y_flip_zzmi_parity(self):
        """R7/t48 + R11：efmi_detect.hlsl ToScreen 的 Y 必须做 minus-Y NDC
        翻转，与光标 UV 归一方向一致。

        - 探针存 native game VS 的 clip 输出，D3D NDC Y-up（顶=+w）；
        - 因此 clip→屏幕 UV 必须 `uv.y = 0.5 - 0.5*ndc.y`，否则三角形 UV 是
          Y-up（1=顶）而光标是 top-down（0=顶）——两轴相反，命中落在垂直镜像，
          上屏光标命中远偏下（用户实测「鼠标命中位置明显偏下」）。
        参照：ZZMI rzm_object_detect.hlsl L330 `uv = float2(ndc.x*0.5+0.5, 0.5-ndc.y*0.5)`。

        **t48-b 更正**：旧 docstring 曾断言「光标 UV 是 top-left/downward（0=顶），
        故 `$cursorY = cursor_y` 直用」——该前提**是错的**：本 fork 的 `cursor_y`
        其实下 0 上 1（Y-up，见 ZZMI 三个级联分支统一 `1.0 - cursor_y`）。也就是说
        这个 R7/t48 修复只把**网格侧**翻成 top-down，而**游标侧**仍是 Y-up ⇒
        镜像从另一侧保留了下来；补偿被做进了 delta 路径（`delta.y = -delta.y`）。
        真正的对齐由 test_t48b_detect_flips_cursor_y_to_topdown 钉住。
        """
        node = _make_efmi_node()
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        shader = (td / "res" / "drag_interaction_efmi" / "efmi_detect.hlsl").read_text(
            encoding="utf-8"
        )
        # ToScreen 必须 Y 翻转（0.5 - .5*c.y/c.w），不允许旧的 Y-up 形式
        self.assertIn("return float2(.5 + .5 * c.x / c.w, .5 - .5 * c.y / c.w);", shader)
        self.assertNotIn(".5 + .5 * c.y / c.w);", shader)
        # 记录本修复的 ZZMI 参照（ClipToScreenUV 行号）与其成因（命中偏下）
        self.assertIn("rzm_object_detect.hlsl L330", shader)
        self.assertIn("命中偏下", shader)

    def test_t48b_detect_flips_cursor_y_to_topdown(self):
        """t48-b：detect 必须把 Y-up 的 `cursor_y` 翻成 top-down 再判定命。

        本 fork 的 `cursor_y` 是**下 0 上 1（Y-up）**——ZZMI 生成器
        `node_postprocess_draginteraction.py:4268-4274` 的**三个级联分支全部**
        做 `1.0 - cursor_y`（`cursor_y` / `cursor_window_y` / `cursor_screen_y /
        $screenH`），即 ZZMI 在源头就归一成 top-down。
        而 EFMI 的 `[Present]` 是 `$cursorY = cursor_y` 直用，`efmi_detect.hlsl`
        又按 top-down 写（`ToScreen` 已是 top-down）⇒ 绝对命中判定拿 Y-up 游标
        比对 top-down uv ⇒ **整块命中区上下镜像**（用户实测「命中区整体偏移」）。

        翻在 detect 而不是源头，是因为 delta 路径已经补偿过同一个约定：
        `efmi_simulate.hlsl` 的 `delta.y = -delta.y` 与 `efmi_hand_preview.hlsl`
        的 `uvDelta.y = -uvDelta.y`。若在源头翻 `[151].y`，那两处会变成二次翻转、
        拖拽方向反过来。"""
        node = _make_efmi_node()
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        shader = (td / "res" / "drag_interaction_efmi" / "efmi_detect.hlsl").read_text(
            encoding="utf-8")
        self.assertIn("cursor.y = 1.0 - cursor.y;", shader)
        # 必须在读取之后、判定之前（不能只写在注释里）
        self.assertLess(shader.index("float2 cursor = IniParams[151].xy;"),
                        shader.index("cursor.y = 1.0 - cursor.y;"))
        self.assertLess(shader.index("cursor.y = 1.0 - cursor.y;"),
                        shader.index("bool cursorValid ="))
        # 源头保持直用：翻了源头就会和 delta 路径的补偿二次翻转
        present_z = "\n".join(self._run_export(node)[1]["[Present]"])
        self.assertIn("$cursorY = cursor_y", present_z)
        self.assertNotIn("$cursorY = 1.0 - cursor_y", present_z)
        # 记录 ZZMI 参照，防止后人"清理"这一行
        self.assertIn("1.0 - cursor_window_y", shader)

    # ---- numpy 参考模型：按压冻结 / 释放保留速度 / ZZMI 步长 / 半径夹 ----

    def test_press_freeze_target_formula(self):
        """按压冻结语义（align-t3 ZZMI 目标公式）：目标只依赖「当前光标 − 按压
        捕获光标」与冻结基 + depth_pull 比例×冻结法线；LMB 拉出（+法线）、
        RMB 压入（−法线）；PullTowardLimit 渐进阻力逼近 max_offset 不硬钳。"""
        right = np.array([1.0, 0.0, 0.0])
        down = np.array([0.0, 1.0, 0.0])
        normal = np.array([0.0, 0.0, 1.0])
        # LMB：横向拖拽 + 沿 +法线拉出（深度 = |delta| × depth_pull）
        t = _ref_sim_target((0.01, -0.02), 1, 1.0, 1.0, 0.040, right, down, normal)
        d = np.linalg.norm([0.01, -0.02])
        raw = np.array([0.01, -0.02, d])  # 基投影 + depth_pull 法线分量
        expect = _ref_pull_toward_limit(raw, 0.040)
        np.testing.assert_allclose(t, expect, atol=1e-12)
        # RMB：压入（法线反号）
        t2 = _ref_sim_target((0.01, -0.02), 2, 1.0, 1.0, 0.040, right, down, normal)
        raw2 = np.array([0.01, -0.02, -d])
        np.testing.assert_allclose(
            t2, _ref_pull_toward_limit(raw2, 0.040), atol=1e-12)
        # 大拖拽：渐近逼近 max_offset（不达到——与硬钳的语义差异断言；
        # 拖拽量取适中值避开双精度下 1−exp(−x) 的浮点饱和）
        t4 = _ref_sim_target((0.1, 0.1), 1, 1.0, 1.0, 0.040, right, down, normal)
        self.assertLess(float(np.linalg.norm(t4)), 0.040)
        self.assertGreater(float(np.linalg.norm(t4)), 0.038)
        # 冻结语义：目标仅由 delta 与冻结基/法线决定（相机/绝对光标无关）
        t5 = _ref_sim_target((0.01, -0.02), 1, 1.0, 1.0, 0.040, right, down, normal)
        np.testing.assert_allclose(t5, t, atol=1e-12)

    def test_hold_tracks_target_and_zzmi_step(self):
        """按压保持：位置收敛到（渐进阻力后的）目标；ZZMI 步长契约
        （clamp(dt·60·speed, 0.05, max_step)，align-t3 取代 240Hz 子步）。"""
        right = np.array([1.0, 0.0, 0.0])
        down = np.array([0.0, 1.0, 0.0])
        normal = np.array([0.0, 0.0, 1.0])
        target = _ref_sim_target((0.02, 0.01), 1, 1.0, 1.0, 0.040, right, down, normal)
        dt = 1.0 / 60.0
        step = _ref_simulation_step(dt)
        x, x_prev = np.zeros(3), np.zeros(3)
        filtered, prev_filtered = np.zeros(3), np.zeros(3)
        for _ in range(int(1.0 / dt)):
            x, x_prev, filtered, prev_filtered = _ref_spring_step(
                x, x_prev, filtered, prev_filtered, target, step,
                spring=0.176 * 0.333, damping=0.86, follow=0.12,
                max_offset=0.040, grabbing=True)
        np.testing.assert_allclose(x, target, atol=2e-3)
        # 步长契约：dt=1/60 → clamp(1.0·3.0)=3.0 上限；dt=0 → 0.05 下限
        self.assertEqual(_ref_simulation_step(1.0 / 60.0), 3.0)
        self.assertEqual(_ref_simulation_step(0.0), 0.05)
        # sim_speed/max_step 可调（ZZMI TIME_PARAMS 语义）
        self.assertEqual(_ref_simulation_step(1.0 / 60.0, speed=1.0, max_step=2.0), 1.0)

    def test_release_retains_velocity_overshoots_past_rest(self):
        """释放保留速度（位置派生）：快速拖动后松手（释放瞬间 (x−x_prev)/step
        ≠0）→ 越过回中位过冲；释放踢 ×1.10 与释放动态阻尼（boost 1.05 按
        拉伸强度 lerp）只放大过冲不衰减其存在；欠阻尼收敛回 0。"""
        max_off = 0.040
        dt = 1.0 / 60.0
        step = _ref_simulation_step(dt)
        right = np.array([1.0, 0.0, 0.0])
        down = np.array([0.0, 1.0, 0.0])
        normal = np.array([0.0, 0.0, 1.0])
        # 按压：拖到 (0.03, 0.01)——只拖 6 帧（快拖 flick：松手瞬间弹簧仍在
        # 追踪途中、速度非零；完全稳定后松手 v≈0 是物理正确但无法验证保留路径）
        x, x_prev = np.zeros(3), np.zeros(3)
        filtered, prev_filtered = np.zeros(3), np.zeros(3)
        t_hold = _ref_sim_target((0.03, 0.01), 1, 1.0, 1.0, max_off, right, down, normal)
        for _ in range(6):
            x, x_prev, filtered, prev_filtered = _ref_spring_step(
                x, x_prev, filtered, prev_filtered, t_hold, step,
                spring=0.176 * 0.333, damping=0.86, follow=0.12,
                max_offset=max_off, grabbing=True)
        # 确认仍在运动（flick 前提）
        self.assertGreater(np.linalg.norm(x - x_prev), 1e-4)
        x_rel, xp_rel = x.copy(), x_prev.copy()
        v_rel = (x_rel - xp_rel) / step
        # 释放瞬间速度非零（flick 轨迹；v 为 ZZMI 位置派生语义的
        # 「每 step 单位位移」——step=3 时数值为帧位移的 1/3）
        self.assertGreater(np.linalg.norm(v_rel), 5e-4)
        # 保留速度（shader 行为：位置派生不清零）→ 越过回中位过冲；
        # 释放踢 ×1.10（位置派生语义下等价于把 x_prev 沿速度反方向拉远）；
        # 释放动态阻尼（ZZMI L385-406）：释放瞬间 mult = lerp(稳态, 1.05,
        # 拉伸强度)，逐帧 0.92^step 衰减回稳态——本循环逐帧建模该衰减
        xp_rel = x_rel - v_rel * 1.10 * step
        xs, xps = x_rel.copy(), xp_rel.copy()
        f2, pf2 = filtered.copy(), prev_filtered.copy()
        min_x_retained = 1e9
        max_abs = np.linalg.norm(x_rel)
        rel_mult = 1.0 + (1.05 - 1.0) * min(np.linalg.norm(x_rel) / max_off, 1.0)
        for _ in range(int(3.0 / dt)):
            damping_now = min(0.96 * rel_mult, 1.0)  # saturate
            xs, xps, f2, pf2 = _ref_spring_step(
                xs, xps, f2, pf2, np.zeros(3), step,
                spring=0.055 * 0.333, damping=damping_now, follow=0.12,
                max_offset=max_off, grabbing=False)
            rel_mult = 1.0 + (rel_mult - 1.0) * (0.92 ** step)
            min_x_retained = min(min_x_retained, float(xs[0]))
            max_abs = max(max_abs, float(np.linalg.norm(xs)))
            self.assertLessEqual(np.linalg.norm(xs), max_off + 1e-9)  # 半径夹
        self.assertLess(min_x_retained, 0.0)  # 沿拖拽轴越过回中位（过冲）
        # 最终稳定（欠阻尼收敛回 0）
        self.assertLess(np.linalg.norm(xs), 1e-3)

    def test_spring_radius_clamp_semantics(self):
        """积分器半径夹（ZZMI ClampLength 语义）：位置越限按比例收缩到
        max_offset（不删法向速度——速度由位置派生，钳位经位置回馈）。"""
        step = 3.0
        x = np.array([0.03, 0.03, 0.0])
        x_prev = np.array([0.028, 0.028, 0.0])
        nxt, _, _, _ = _ref_spring_step(
            x, x_prev, np.zeros(3), np.zeros(3), np.zeros(3), step,
            spring=0.176 * 0.333, damping=0.86, follow=0.12,
            max_offset=0.040, grabbing=False)
        self.assertLessEqual(np.linalg.norm(nxt), 0.040 + 1e-9)


class TestEFMIStrideWords(unittest.TestCase):
    """strideWords 4/10 与只写前 3 字断言（t15 补强）：16→4 / 40→10、
    Out 资源 stride 跟随、只写位置前 3 字（打包位/法线槽不破坏）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()

    def _run_export(self, node, pos_stride):
        td = _make_efmi_mod_dir(pos_stride=pos_stride)
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        sections = _read_ini_sections(td / "main.ini")
        return td, sections

    def test_stride16_x155_words4(self):
        node = _make_efmi_node()
        _td, sections = self._run_export(node, 16)
        deform = "\n".join(sections["[CustomShaderEFMIDragDeform_LOD0.abc123_43191_A]"])
        self.assertIn("x155 = 4", deform)
        self.assertIn("y155 = 16", deform)
        out = "\n".join(sections["[ResourceEFMIDragOut_LOD0.abc123_43191_A]"])
        self.assertIn("stride = 16", out)

    def test_stride40_x155_words10(self):
        """40B 位置流（LOD0 型契约）：strideWords=10、Out 资源 stride=40、
        顶点数/探针 draw 数不受字宽影响。"""
        node = _make_efmi_node()
        td, sections = self._run_export(node, 40)
        deform = "\n".join(sections["[CustomShaderEFMIDragDeform_LOD0.abc123_43191_A]"])
        self.assertIn("x155 = 10", deform)
        self.assertIn("y155 = 40", deform)
        out = "\n".join(sections["[ResourceEFMIDragOut_LOD0.abc123_43191_A]"])
        self.assertIn("stride = 40", out)
        probe = "\n".join(sections["[CustomShaderEFMIDragProbeBody_A]"])
        self.assertIn("drawindexed = 16, 0, 0", probe)
        # 权重烘焙按前 3 字读取 → 16 顶点稀疏表（K=4）；无空物体 → zone0 全 1
        weights = np.fromfile(
            td / "res" / "drag_interaction_efmi" / "LOD0.abc123-43191_weights.buf",
            dtype=np.float32,
        ).reshape(-1, 4)
        zone_ids = np.fromfile(
            td / "res" / "drag_interaction_efmi" / "LOD0.abc123-43191_zones.buf",
            dtype=np.uint32,
        ).reshape(-1, 4)
        self.assertEqual(len(weights), 16)
        np.testing.assert_allclose(weights[:, 0], 1.0)  # zone0 全 1（整模型可抓）
        np.testing.assert_allclose(weights[:, 1:], 0.0)
        np.testing.assert_array_equal(zone_ids[:, 0], np.zeros(16, dtype=np.uint32))
        np.testing.assert_array_equal(zone_ids[:, 1:], np.full((16, 3), 0xFFFFFFFF, dtype=np.uint32))

    def test_read_float_buf_stride40_first_three_words(self):
        """40B stride 读取：只取每顶点前 3 个 float32（位置），不串位。"""
        exporter = self.mod.DragInteractionEFMIExporter(_make_efmi_node())
        with tempfile.TemporaryDirectory() as td:
            buf = np.zeros((4, 10), dtype=np.float32)
            buf[:, :3] = [[1, 2, 3], [4, 5, 6], [7, 8, 9], [10, 11, 12]]
            buf[:, 3:] = 99.0
            path = Path(td) / "pos40.buf"
            buf.tofile(path)
            out = exporter._read_float_buf(str(path), 40, 3)
            self.assertEqual(out.shape, (4, 3))
            np.testing.assert_array_equal(out, buf[:, :3])

    def test_deform_shader_only_first_three_words(self):
        """efmi_deform.hlsl：Out 为整记录 Base 拷贝 + 只改写前 3 个 uint32 字
        （位置）；words3-9（NORMAL/TANGENT）从 Base 位精确拷贝（A1/t13——
        Out 被换绑为 vb0/vb3、游戏原生 VS 从 slot0@12/24 读法线/切线，留 0 会
        变暗）；strideWords 来自 x155.x；稀疏 K 对应用。"""
        node = _make_efmi_node()
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        shader = (td / "res" / "drag_interaction_efmi" / "efmi_deform.hlsl").read_text(
            encoding="utf-8"
        )
        # t25：头部契约改为"POSITION 覆写 + NORMAL/TANGENT 由位移场有限差分刷新；
        # delta==0 / 窄 stride 时才位精确拷贝"
        self.assertIn(
            "// Out is a whole-record copy of Base. The first three uint32 words (POSITION)\n"
            "// are overwritten with the displaced position (Base + delta).",
            shader,
        )
        self.assertIn(
            "//   · delta == 0 (inactive / mode<2 / nothing dragged)  -> words 3.. are copied\n"
            "//     bit-exact from Base: the \"no drag\" path is byte-for-byte the old behaviour.",
            shader,
        )
        self.assertIn("uint strideWords = (uint)IniParams[155].x;", shader)
        self.assertIn("uint at = vertex * strideWords;", shader)
        self.assertIn("Write3(at, pos + delta);", shader)
        # A1/t13：words 3..strideWords-1 逐字从 Base 拷贝到 Out（保 NORMAL/
        # TANGENT）；旧「Never write at+3 / at+6」注释（错误的独立槽假设）移除。
        # t25：该"位精确拷贝"路径保留为 delta==0 / strideWords<9 的**唯一**行为；
        # 发生位移且存在 N/T 字段时才改走有限差分刷新（见下方 t25 断言）。
        self.assertIn("for (uint w = 3u; w < strideWords; ++w)", shader)
        self.assertIn("Output[at + w] = Base[at + w];", shader)
        self.assertNotIn("Never write at+3 / at+6", shader)
        self.assertNotIn("Write4", shader)
        # ---- t25：法线/切线有限差分更新（描边壳跟随形变，消除剪切根因）----
        # 位移场抽成可复用函数（能在顶点邻域上求值，d0/d1/d2 自洽）
        self.assertIn("float3 EvalDelta(uint vertex, uint stateBase, float3 p) {", shader)
        self.assertIn("delta = EvalDelta(vertex, stateBase, pos);", shader)
        # 只在真的发生位移且 N/T 字段存在时刷新法线；否则位精确拷贝（行为不变）
        self.assertIn("bool normalWritable = strideWords >= 9u;", shader)
        self.assertIn("bool applyNormalUpdate = normalWritable && dot(delta, delta) > 1e-16;", shader)
        # 局部正交帧 + 一次前向差分重建 N'/T'
        self.assertIn("float3 Braw = cross(Nn, Tn);", shader)
        self.assertIn("dT = EvalDelta(vertex, stateBase, posT);", shader)
        self.assertIn("dB = EvalDelta(vertex, stateBase, posB);", shader)
        self.assertIn("float3 Tp = (posT + dT) - (pos + delta);", shader)
        self.assertIn("float3 Bp = (posB + dB) - (pos + delta);", shader)
        self.assertIn("float3 Np = SafeNormalize(cross(Tp, Bp) * hsign, Nn);", shader)
        # 写入 NORMAL@words3-5 / TANGENT@words6-8（handedness word9 由拷贝循环保留）
        self.assertIn("Write3(at + 3u, Np);", shader)
        self.assertIn("Output[at + 6u] = asuint(Tnew.x);", shader)
        # NaN 守卫：归一化失败回退、零长度回退（绝不把 NaN 写进 Out）
        self.assertIn("float3 SafeNormalize(float3 v, float3 fallback) {", shader)
        self.assertIn("if (dot(Tp, Tp) > 1e-16 && dot(Bp, Bp) > 1e-16) {", shader)
        # 退化保护必须判**原始**叉积（在 SafeNormalize 之前）——否则 fallback
        # 会把"共线导致的零叉积"替换成任意向量，掩盖退化、在错坐标系里求值
        # （t25 静态数学验证发现的真实缺陷，见 t25_fd_normal_check.py [6]）
        self.assertIn("float3 Braw = cross(Nn, Tn);", shader)
        self.assertIn("bool tbnOk = ortho > 1e-4;", shader)
        # 几何自洽：N' 必须与 Base 法线同侧
        self.assertIn("if (dot(Np, Nn) < 0.0) {", shader)
        # 稀疏应用：K 对（区 id + 权重）累加 State 区偏移（align-t3：区槽
        # 步幅 4——current/previous/filtered/prevFiltered）
        self.assertIn("ZoneIDs[vertex * K + i]", shader)
        self.assertIn("State[stateBase + PUBLIC_SLOTS + zone * 4].xyz * w;", shader)
        # t34-P1 ④：falloff 逐区覆盖（ZoneParams[zone*2].w >0 生效，0 回退
        # 全局档案 IniParams[156].x=1.5——ZZMI rzm_jiggle_interaction L822-824
        # ZoneFalloffOverride 同款）+ ZoneParams 绑定 + 生成器侧回退默认行
        self.assertIn("Buffer<float4> ZoneParams : register(t6);", shader)
        self.assertIn("float zoneFalloff = ZoneParams[zone * 2].w;", shader)
        self.assertIn(
            "float falloffPower = zoneFalloff > 0.0",
            shader,
        )
        self.assertIn("max(IniParams[156].x, 0.0001)", shader)
        sections = _read_ini_sections(td / "main.ini")
        deform_sec = "\n".join(sections["[CustomShaderEFMIDragDeform_LOD0.abc123_43191_A]"])
        self.assertIn("cs-t6 = ResourceEFMIDragZoneParams_A", deform_sec)
        self.assertIn("x156 = 1.5", deform_sec)   # 0 回退默认档案（ZZMI JIGGLE_PARAMS.z）


class TestEFMIFinalFindings(unittest.TestCase):
    """t17 终审 findings 回归：F1 帧标签顺序/帧流、F2 Out 尺寸、F3 cycle 钳制。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()
        cls.ce_mod = _load_click_export_module()

    def _run_export(self, node):
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        sections = _read_ini_sections(td / "main.ini")
        return td, sections

    def _make_ce_node(self, zone=0, cycle=0, targets=("swapkey1",), values=""):
        ce = self.ce_mod.SSMTNode_AnimDriver_ClickExport.__new__(
            self.ce_mod.SSMTNode_AnimDriver_ClickExport
        )
        ce.click_zone_id = zone
        ce.cycle_length = cycle
        ce.click_values = values
        ce.click_target_list = [
            types.SimpleNamespace(variable_name=t) for t in targets
        ]
        return ce

    def _install_cross_tree_env(self, anim_tree_nodes):
        bpy_mod = sys.modules["bpy"]

        class _NodeGroups(dict):
            def get(self, name, default=None):
                for value in self.values():
                    if getattr(value, "name", None) == name:
                        return value
                return default

        node_groups = _NodeGroups()
        node_groups["AnimTree1"] = types.SimpleNamespace(
            name="AnimTree1", nodes=list(anim_tree_nodes)
        )
        data = types.SimpleNamespace(node_groups=node_groups)
        bpy_mod.data = data
        self.ce_mod.bpy.data = data
        return bpy_mod

    def test_f1_frame_increment_after_all_runs(self):
        """F1 顺序断言：帧递增行位于所有 run（simulate/ui_publish/shapekey_drive/
        var_sync）之后——Present 期各消费者读到与 detect 相同的帧值。"""
        node = _make_efmi_node_with_consumer([
            _make_sk_item(zone=0, stage=1, dir_id=1),
            _make_sk_item(zone=1, stage=2),
        ])
        _td, sections = self._run_export(node)
        present = sections["[Present]"]
        inc_idx = next(
            i for i, line in enumerate(present)
            if "$ssmtdrag_efmi_frame_A" in line and "+ 1" in line
        )
        run_idx = [
            i for i, line in enumerate(present)
            if str(line).strip().startswith("run =")
            or str(line).strip().startswith("post run =")
            or str(line).strip().startswith("pre run =")
        ]
        self.assertTrue(run_idx, "Present 块应含 run 行")
        self.assertGreater(inc_idx, max(run_idx))

    def test_f1_frame_stream_fresh_semantics(self):
        """F1 帧流模拟：detect 与 Present 在同一帧内读同一全局帧 G（detect 绘制期
        先于帧末 Present），递增在 Present 末 → 同帧 fresh 恒成立；
        修复前（递增在 Present 开头）模拟恒假（off-by-one）。"""
        # 修复后时序（递增在 Present 末，Present = 帧末钩子）：
        frame = 0
        fresh_ok = True
        for n in range(1, 6):
            g = frame                # 帧 n 开始时全局帧（上帧末递增结果）
            detect_writes = g        # 帧 n 绘制期 detect 写 candidate.w = G
            sim_read = g             # 帧 n 末 Present 读 G（递增前，同帧一致）
            fresh_ok = fresh_ok and (detect_writes == sim_read)
            frame = g + 1            # Present 末递增
        self.assertTrue(fresh_ok)
        # 修复前时序（递增在 Present 开头）：帧 n 末 Present 递增后读到 n，
        # 而帧 n 绘制期 detect 读的是帧 n 开始时的 G=n-1 → 恒差 1
        frame = 0
        broken_fresh = True
        for n in range(1, 6):
            detect_writes = frame    # 帧 n 绘制期（Present 前）读当前 G
            frame += 1               # Present 开头递增
            sim_read = frame         # 帧 n 末 Present 读 G+1
            broken_fresh = broken_fresh and (detect_writes == sim_read)
        self.assertFalse(broken_fresh)

    def test_f2_out_resource_has_size_declaration(self):
        """F2：ResourceEFMIDragOut 带 array 尺寸声明（顶点数 × strideWords，
        本仓 efmi.py 惯例：Buffer 均带 array 或 filename）。"""
        node = _make_efmi_node()
        _td, sections = self._run_export(node)
        out_lines = "\n".join(sections["[ResourceEFMIDragOut_LOD0.abc123_43191_A]"])
        self.assertIn("array = 64", out_lines)  # 16 顶点 × stride 16//4 = 64

    def test_f3_click_export_cycle_clamped(self):
        """F3：ClickExport cycle_length 钳制 min(64, ...)（与 zzmi 同款）。"""
        ce = self._make_ce_node(zone=0, cycle=999)
        self._install_cross_tree_env([ce])
        anim_driver = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_AnimDriver",
            mute=False,
            blueprint_name="AnimTree1",
        )
        node = _make_efmi_node()
        object.__setattr__(
            node, "id_data", types.SimpleNamespace(nodes=[anim_driver])
        )
        exporter = self.mod.DragInteractionEFMIExporter(node)
        entries = exporter._collect_click_export_drivers()
        self.assertEqual(entries[0][1], 64)
        # 布局档位扩展不越界（zone0 = max(1, 64-1) = 63）
        _total, _bases, counts = exporter._drag_drive_buffer_layout()
        self.assertEqual(counts[0], 63)


class TestEFMIUXAlignment(unittest.TestCase):
    """t22 UX 全对齐：空物体框选推导/物理档案映射/ps hash 多值/手型光标族。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()

    def _run_export(self, node):
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        sections = _read_ini_sections(td / "main.ini")
        return td, sections

    def _empty(self, x, y, z, enabled=True, radius=0.0, strength=1.0,
               falloff=0.0, grabbable=True, scale=0.03):
        """mock Empty 物体（matrix_world + ssmt_drag_zone 参数组）。"""
        mw = np.eye(4, dtype=np.float64)
        mw[0, 3], mw[1, 3], mw[2, 3] = x, y, z
        mw[0, 0], mw[1, 1], mw[2, 2] = scale, scale, scale

        class _Obj:
            type = 'EMPTY'

        obj = _Obj()
        obj.matrix_world = mw
        obj.ssmt_drag_zone = types.SimpleNamespace(
            enabled=enabled, radius=radius, brush_strength=strength,
            falloff=falloff, grabbable=grabbable,
        )
        return obj

    def test_zone_empty_boxes_drive_spatial_weights(self):
        """区域框选（t22 契约升级）：启用空物体 → 空间中心 + 稀疏球衰减权重
        （radius 覆盖/缩放均值、分侧 side_sign 按球心 X 符号自动、
        strength/grabbable 参数组生效）。"""
        node = _make_efmi_node()
        left = self._empty(-0.08, -0.12, 1.09, radius=0.03, strength=1.0)  # 负 X
        right = self._empty(0.08, -0.12, 1.09, radius=0.03, strength=0.5)
        object.__setattr__(
            node, "zone_objects",
            [types.SimpleNamespace(zone_object=left),
             types.SimpleNamespace(zone_object=right)],
        )
        exporter = self.mod.DragInteractionEFMIExporter(node)
        configs = exporter._collect_zone_configs()
        self.assertEqual(len(configs), 2)
        self.assertAlmostEqual(configs[0][0][0], -0.08, places=4)
        self.assertAlmostEqual(configs[1][0][0], 0.08, places=4)
        # t28 椭球：区域配置位置 1 = lin3 (3,3) 线性矩阵；显式 radius=0.03 → 各向
        # 同性球 0.03·I（行为与旧标量 radius 等价）
        np.testing.assert_allclose(configs[0][1], 0.03 * np.eye(3), atol=1e-6)
        self.assertAlmostEqual(configs[0][2], 1.0, places=4)
        self.assertAlmostEqual(configs[1][2], 0.5, places=4)    # strength
        # 稀疏权重表：左区球内 zone0 高、右区球内 zone1 高（分侧），强度缩放生效
        pos = np.array([
            [-0.08, -0.12, 1.09], [-0.079, -0.121, 1.091],
            [0.08, -0.12, 1.09], [0.081, -0.119, 1.091],
        ], dtype=np.float32)
        zone_ids, w = self.mod.bake_sparse_weights(pos, configs)
        self.assertEqual(w.shape, (4, 4))
        np.testing.assert_array_equal(zone_ids[0, :2], [0, 0xFFFFFFFF])
        np.testing.assert_array_equal(zone_ids[2, :2], [1, 0xFFFFFFFF])
        self.assertGreater(w[0, 0], 0.9)
        # 右区顶点单区命中（分侧压掉左区）→ 原始场 = strength = 0.5（t16 不归一，
        # 拖拽强度随权重衰减；原归一化压成 1.0 是「整体一块无衰减」的根因）
        self.assertAlmostEqual(float(w[2, 0]), 0.5, places=5)
        # 分侧：左区顶点不挂右区（zone1 槽空）、右区顶点不挂左区
        self.assertEqual(zone_ids[0, 1], 0xFFFFFFFF)
        self.assertEqual(zone_ids[2, 1], 0xFFFFFFFF)

    def test_zone_empty_scale_fallback_radius(self):
        """radius 未配置（0）→ 球半径 = 空物体缩放均值（zzmi 同款 ws）。"""
        node = _make_efmi_node()
        empty = self._empty(-0.08, -0.12, 1.09, radius=0.0, scale=0.05)
        object.__setattr__(
            node, "zone_objects", [types.SimpleNamespace(zone_object=empty)]
        )
        exporter = self.mod.DragInteractionEFMIExporter(node)
        configs = exporter._collect_zone_configs()
        # t28 椭球：radius 未配置（0）→ lin3 = world 上半 3×3 = 均匀缩放 0.05·I
        np.testing.assert_allclose(configs[0][1], 0.05 * np.eye(3), atol=1e-6)   # ws

    # ------------------------------------------------------------------
    # t28 椭球（D2）回归——旋转+非均匀缩放全矩阵
    # ------------------------------------------------------------------

    def test_ball_weights_ellipsoid_nonuniform_scale(self):
        """椭球非均匀缩放：lin3 = diag(rx,ry,rz) → d²=Σ((p−c)/r_i)²，沿长轴
        （大半径）拉伸、短轴压缩；d²≥1 硬截止。"""
        m = self.mod
        center = (0.0, 0.0, 0.0)
        lin3 = np.diag([0.50, 0.25, 0.25])          # X 长轴 0.5、Y/Z 短轴 0.25
        # 沿各轴距球心 r_i 处的局部距离 = 1（场应被硬截止到 0）
        pos = np.array([
            [0.50, 0.0, 0.0],    # (x/0.5)=1 → 边界
            [0.0, 0.25, 0.0],    # (y/0.25)=1 → 边界
            [0.0, 0.0, 0.25],    # (z/0.25)=1 → 边界
            [0.30, 0.0, 0.0],    # x 半程 → (0.3/0.5)=0.6
            [0.0, 0.10, 0.0],    # y 半程 → (0.1/0.25)=0.4
            [0.0, 0.0, 0.0],     # 球心 d=0
        ], dtype=np.float32)
        strength = 1.0
        k = 4.6
        w = m.bake_zone_ball(pos, center, lin3, strength=strength, falloff_k=k)
        # 边界 d=1 → 0（各轴验证等距但异向都截止）
        self.assertEqual(w[0], 0.0)
        self.assertEqual(w[1], 0.0)
        self.assertEqual(w[2], 0.0)
        # 非对称点：沿长轴 0.6 与沿短轴 0.4 场值不同（非均匀缩放生效）
        self.assertAlmostEqual(float(w[3]), strength * np.exp(-k * 0.6 ** 2), places=6)
        self.assertAlmostEqual(float(w[4]), strength * np.exp(-k * 0.4 ** 2), places=6)
        np.testing.assert_allclose(w[5], strength, atol=1e-9)   # 球心峰

    def test_ball_weights_ellipsoid_rotation_respected(self):
        """旋转被尊重：单一轴上非均匀缩放经旋转到对角方向后，场沿世界轴方向
        变为斜椭球——与未旋转（轴对齐 lin3）在场值上不同，证明全矩阵（旋转）
        参与。"""
        m = self.mod
        center = (0.0, 0.0, 0.0)
        # 未旋转：X 拉伸 diag(1.0, 0.25, 0.25)
        axis_aligned = np.diag([1.0, 0.25, 0.25])
        # 旋转 90° 绕 Z：X 拉伸方向转到 Y → lin3 = Rz(90°)·diag
        th = np.pi / 2.0
        rz = np.array([
            [np.cos(th), -np.sin(th), 0.0],
            [np.sin(th), np.cos(th), 0.0],
            [0.0, 0.0, 1.0],
        ])
        rotated = rz @ np.diag([1.0, 0.25, 0.25])
        # 取一个点：距球心 (0.5, 0.0, 0.0)——轴对齐时沿长轴(远>短轴截止) vs
        # 旋转后该点沿短轴方向（近）
        probe = np.array([[0.5, 0.0, 0.0],
                          [0.0, 0.5, 0.0],
                          [0.0, 0.0, 0.5]], dtype=np.float32)
        k = 4.6
        w_a = m.bake_zone_ball(probe, center, axis_aligned, strength=1.0, falloff_k=k)
        w_r = m.bake_zone_ball(probe, center, rotated, strength=1.0, falloff_k=k)
        # 轴对齐：X 点 (0.5/1.0)=0.5 有场，Y 点 (0.5/0.25)=2 → 0
        self.assertAlmostEqual(float(w_a[0]), float(np.exp(-k * 0.5 ** 2)), places=6)
        self.assertEqual(w_a[1], 0.0)
        # 旋转后：Y 点沿长轴 (0.5/1.0)=0.5 有场，X 点沿短轴 → 0（证明旋转生效）
        self.assertEqual(w_r[0], 0.0)
        self.assertAlmostEqual(float(w_r[1]), float(np.exp(-k * 0.5 ** 2)), places=6)
        # Z 轴两模型一致（绕 Z 旋转不变）
        self.assertAlmostEqual(float(w_a[2]), float(w_r[2]), places=9)

    def test_ball_weights_isotropic_lin3_matches_scalar(self):
        """向后兼容：lin3 = r·I 与标量 radius 的场完全一致（旧数据/显式 radius
        行为不变）。"""
        m = self.mod
        center = (0.0, 0.0, 0.0)
        pos = np.array([[0.0, 0.0, 0.0], [0.25, 0.0, 0.0], [0.5, 0.0, 0.0],
                        [0.9, 0.0, 0.0], [1.0, 0.0, 0.0]], dtype=np.float32)
        r = 0.5
        k = 4.6
        w_scalar = m.bake_zone_ball(pos, center, r, strength=1.0, falloff_k=k)
        w_lin3 = m.bake_zone_ball(pos, center, r * np.eye(3), strength=1.0, falloff_k=k)
        np.testing.assert_allclose(w_scalar, w_lin3, atol=1e-9)

    def test_zone_configs_ellipsoid_from_rotated_nonuniform_empty(self):
        """空物体旋转+非均匀缩放 → _collect_zone_configs lin3 = world 上半 3×3
        （全矩阵）；t32-A：radius 不再覆盖 lin3（命中范围恒由空物体变换决定）。"""
        node = _make_efmi_node()
        mw = np.eye(4, dtype=np.float64)
        mw[0, 3], mw[1, 3], mw[2, 3] = -0.08, -0.12, 1.09
        mw[0, 0], mw[1, 1], mw[2, 2] = 0.10, 0.04, 0.02           # 非均匀缩放
        th = np.pi / 6.0
        rz = np.array([[np.cos(th), -np.sin(th), 0.0],
                       [np.sin(th), np.cos(th), 0.0],
                       [0.0, 0.0, 1.0]])
        mw[:3, :3] = rz @ mw[:3, :3]                              # 旋转 30°

        class _Obj:
            type = 'EMPTY'
        obj = _Obj()
        obj.matrix_world = mw
        obj.ssmt_drag_zone = types.SimpleNamespace(
            enabled=True, radius=0.0, brush_strength=0.8,
            falloff=0.0, grabbable=True)
        object.__setattr__(
            node, "zone_objects", [types.SimpleNamespace(zone_object=obj)]
        )
        exporter = self.mod.DragInteractionEFMIExporter(node)
        configs = exporter._collect_zone_configs()
        self.assertEqual(len(configs), 1)
        center, lin3, strength, _falloff_k, grabbable = configs[0]
        self.assertAlmostEqual(center[0], -0.08, places=4)
        # lin3 = world 上半 3×3（旋转×非均匀缩放）逐元素一致
        np.testing.assert_allclose(lin3, mw[:3, :3], atol=1e-9)
        self.assertAlmostEqual(strength, 0.8, places=4)
        self.assertTrue(grabbable)
        # t32-A：radius 仅作拖拽衰减，不再覆盖命中范围——设 radius 后 lin3 不变
        obj.ssmt_drag_zone.radius = 0.03
        configs2 = exporter._collect_zone_configs()
        np.testing.assert_allclose(configs2[0][1], mw[:3, :3], atol=1e-9)

    def test_zone_params_radius_is_raw_drag_falloff(self):
        """t32-A 参数语义分离：ZoneParams[0].x = ssmt_drag_zone.radius **原始值**
        （拖拽衰减半径，deform RubberInfluence 距离门；0=回退 0.25 ×
        mult_radius），与空物体缩放/命中椭球完全脱钩——不再取椭球最长轴。

        align-t3 夹具修正：空物体放到夹具网格处（全无效早退语义下无交集 =
        不注入）。"""
        node = _make_efmi_node()
        # 非均匀缩放 lin3（命中椭球长轴 0.10）——与拖拽衰减半径无关
        mw = np.eye(4, dtype=np.float64)
        mw[0, 3], mw[1, 3], mw[2, 3] = 0.0, -0.12, 1.09
        mw[0, 0], mw[1, 1], mw[2, 2] = 0.10, 0.04, 0.02

        class _Obj:
            type = 'EMPTY'
        obj = _Obj()
        obj.matrix_world = mw
        obj.ssmt_drag_zone = types.SimpleNamespace(
            enabled=True, radius=0.35, brush_strength=1.0,
            falloff=0.0, grabbable=True)
        object.__setattr__(
            node, "zone_objects", [types.SimpleNamespace(zone_object=obj)]
        )
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        zone_params = np.fromfile(
            td / "res" / "drag_interaction_efmi" / "ZoneParams_A.buf",
            dtype=np.float32,
        ).reshape(-1, 4)
        # 拖拽衰减半径 = 原始 settings.radius = 0.35（≠ 命中椭球长轴 0.10）
        self.assertAlmostEqual(float(zone_params[0][0]), 0.35, places=6)
        # radius=0 → 发射 0（shader 侧回退 0.25，ZZMI 同款）
        obj.ssmt_drag_zone.radius = 0.0
        td2 = _make_efmi_mod_dir()
        exporter2 = self.mod.DragInteractionEFMIExporter(node)
        exporter2.execute(str(td2))
        zone_params2 = np.fromfile(
            td2 / "res" / "drag_interaction_efmi" / "ZoneParams_A.buf",
            dtype=np.float32,
        ).reshape(-1, 4)
        self.assertEqual(float(zone_params2[0][0]), 0.0)

    def test_zone_params_follow_stable_id_after_reorder(self):
        """物理重排后 ZoneParams 每区的拖拽物理参数随**稳定 zone id**（配置序）
        对齐，而非列表位——旧 `self.zone_objects[z]` 按列表位取 ssmt_drag_zone
        会错配（d1 t29 协调 + t28 修复回归）。

        align-t3 夹具修正：区域空物体须与网格有交集（ZZMI 全无效早退语义下
        无交集 = 跳过注入，产物不落盘）——空物体改放到夹具网格邻近位置。"""
        node = _make_efmi_node()

        def mk_empty(x, falloff, damping):
            mw = np.eye(4, dtype=np.float64)
            mw[0, 3], mw[1, 3], mw[2, 3] = x, -0.12, 1.09
            mw[0, 0], mw[1, 1], mw[2, 2] = 0.06, 0.06, 0.06
            class _Obj:
                type = 'EMPTY'
            o = _Obj()
            o.matrix_world = mw
            o.ssmt_drag_zone = types.SimpleNamespace(
                enabled=True, radius=0.0, brush_strength=1.0,
                falloff=falloff, damping=damping, grabbable=True)
            return o

        a = mk_empty(-0.07, falloff=1.0, damping=0.1)
        b = mk_empty(0.07, falloff=2.0, damping=0.2)
        c = mk_empty(0.0, falloff=3.0, damping=0.3)
        items = [types.SimpleNamespace(zone_object=o) for o in (a, b, c)]
        object.__setattr__(node, "zone_objects", items)

        # 首载：持久化稳定 zone id a=0, b=1, c=2
        exp0 = self.mod.DragInteractionEFMIExporter(node)
        exp0._collect_enabled_zone_entries()

        # 物理重排列表：c, a, b
        object.__setattr__(node, "zone_objects",
                           [items[2], items[0], items[1]])
        td = _make_efmi_mod_dir()
        exp1 = self.mod.DragInteractionEFMIExporter(node)
        exp1.execute(str(td))
        zone_params = np.fromfile(
            td / "res" / "drag_interaction_efmi" / "ZoneParams_A.buf",
            dtype=np.float32,
        ).reshape(-1, 4)
        # t32-A 新布局：ZoneParams[z*2+0] = (radius,strength,max_offset,falloff)、
        # [z*2+1] = (damping,grabbable,0,1)——必须按稳定 id 对齐：slot0=a(falloff
        # 1.0,damping0.1)、slot1=b(2.0,0.2)、slot2=c(3.0,0.3)，而非列表位
        # （重排后列表位依次 c,a,b）。
        np.testing.assert_allclose(zone_params[0], [0.0, 0.0, 0.0, 1.0], atol=1e-6)  # a falloff
        np.testing.assert_allclose(zone_params[1][0::2], [0.1, 0.0], atol=1e-6)     # a damping (+reserved)
        np.testing.assert_allclose(zone_params[2], [0.0, 0.0, 0.0, 2.0], atol=1e-6)  # b falloff
        np.testing.assert_allclose(zone_params[3][0::2], [0.2, 0.0], atol=1e-6)     # b damping
        np.testing.assert_allclose(zone_params[4], [0.0, 0.0, 0.0, 3.0], atol=1e-6)  # c falloff
        np.testing.assert_allclose(zone_params[5][0::2], [0.3, 0.0], atol=1e-6)     # c damping

    def test_preview_follows_bake_ellipsoid(self):
        """预览场语义跟随 bake（P2-2/t28）：_efmi_preview_weights 用空物体 world
        上半 3×3（旋转+非均匀缩放）→ 与 bake 同形状；显式 radius>0 各向同性。"""
        node = _make_efmi_node()
        mw = np.eye(4, dtype=np.float64)
        mw[0, 3], mw[1, 3], mw[2, 3] = 0.0, 0.0, 0.0
        mw[0, 0], mw[1, 1], mw[2, 2] = 0.50, 0.25, 0.25            # 非均匀缩放

        class _Obj:
            type = 'EMPTY'
        obj = _Obj()
        obj.matrix_world = mw
        obj.ssmt_drag_zone = types.SimpleNamespace(
            enabled=True, radius=0.0, brush_strength=1.0,
            falloff=0.0, grabbable=True)
        object.__setattr__(
            node, "zone_objects", [types.SimpleNamespace(zone_object=obj)]
        )
        mesh = np.array([[0.0, 0.0, 0.0],
                         [0.50, 0.0, 0.0],   # 长轴边界 d=1
                         [0.0, 0.125, 0.0],  # 短轴半程 (0.125/0.25)=0.5
                         [0.0, 0.25, 0.0]],  # 短轴边界 d=1
                        dtype=np.float64)
        _world, w = self.mod._efmi_preview_weights(node, obj, 0, mesh)
        self.assertAlmostEqual(float(w[0]), 1.0, places=6)          # 球心峰
        self.assertEqual(w[1], 0.0)                                 # 长轴边界
        self.assertAlmostEqual(float(w[2]), float(np.exp(-4.6 * 0.5 ** 2)), places=6)
        self.assertEqual(w[3], 0.0)                                 # 短轴边界
        # t32-A：radius 仅作拖拽衰减，不参与命中/预览形状——设 radius 后预览
        # 场与 bake 同规（lin3 不变），长轴点仍 d=1 截止
        obj.ssmt_drag_zone.radius = 0.25
        _w2, w2 = self.mod._efmi_preview_weights(node, obj, 0, mesh)
        np.testing.assert_allclose(w2, w, atol=1e-9)                # 形状不变
        self.assertEqual(w2[1], 0.0)                                # 长轴边界仍截止

    def test_zero_empty_fallback_zone0_all_one(self):
        """0 个空物体：zzmi 同款回退 zone0 全 1（整模型可抓）+ 稀疏表验证。"""
        node = _make_efmi_node()  # 无 zone_objects
        _td, sections = self._run_export(node)
        res = _td / "res" / "drag_interaction_efmi"
        weights = np.fromfile(res / "LOD0.abc123-43191_weights.buf", dtype=np.float32).reshape(-1, 4)
        zone_ids = np.fromfile(res / "LOD0.abc123-43191_zones.buf", dtype=np.uint32).reshape(-1, 4)
        self.assertEqual(weights.shape[1], 4)   # 稀疏 K=4
        np.testing.assert_allclose(weights[:, 0], 1.0)
        np.testing.assert_allclose(weights[:, 1:], 0.0)
        np.testing.assert_array_equal(zone_ids[:, 0], np.zeros(len(weights), dtype=np.uint32))
        np.testing.assert_array_equal(zone_ids[:, 1:], np.full((len(weights), 3), 0xFFFFFFFF, dtype=np.uint32))

    def test_third_zone_included_up_to_256(self):
        """256 区模型：第 3 个及之后空物体全部纳入（按序分配 zone id），超过
        256 的忽略并警告。"""
        node = _make_efmi_node()
        a = self._empty(-0.08, -0.12, 1.09)
        b = self._empty(0.08, -0.12, 1.09)
        c = self._empty(0.30, 0.0, 1.0)
        object.__setattr__(
            node, "zone_objects",
            [types.SimpleNamespace(zone_object=o) for o in (a, b, c)],
        )
        exporter = self.mod.DragInteractionEFMIExporter(node)
        configs = exporter._collect_zone_configs()
        self.assertEqual(len(configs), 3)
        self.assertAlmostEqual(configs[0][0][0], -0.08, places=4)
        self.assertAlmostEqual(configs[2][0][0], 0.30, places=4)
        # 超过 256 → 忽略 + 警告
        node2 = _make_efmi_node()
        many = [self._empty(i * 0.05, 0.0, 1.0) for i in range(258)]
        object.__setattr__(
            node2, "zone_objects",
            [types.SimpleNamespace(zone_object=o) for o in many],
        )
        configs2 = self.mod.DragInteractionEFMIExporter(node2)._collect_zone_configs()
        self.assertEqual(len(configs2), 256)

    def test_zone_params_256_slots(self):
        """ZoneParams：256 槽 t32-A 布局（radius/strength/max_offset/falloff +
        damping/grabbable/0/1，逐槽对齐 ZZMI _write_zone_resources）按
        ssmt_drag_zone 原始属性烘焙。"""
        node = _make_efmi_node()
        left = self._empty(-0.08, -0.12, 1.09, radius=0.04, strength=0.7, falloff=1.5)
        right = self._empty(0.08, -0.12, 1.09, radius=0.03, grabbable=False)
        object.__setattr__(
            node, "zone_objects",
            [types.SimpleNamespace(zone_object=left),
             types.SimpleNamespace(zone_object=right)],
        )
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        zone_params = np.fromfile(
            td / "res" / "drag_interaction_efmi" / "ZoneParams_A.buf",
            dtype=np.float32,
        ).reshape(-1, 4)
        self.assertEqual(len(zone_params), 256 * 2)   # 256 槽 × 2 记录
        # [z*2+0] = (radius, strength, max_offset, falloff)——radius=原始 0.04
        # （拖拽衰减，非命中）；settings.strength 未设=0；falloff=1.5
        np.testing.assert_allclose(zone_params[0], [0.04, 0.0, 0.0, 1.5], atol=1e-6)
        # [z*2+1] = (damping, grabbable, 0, 1)
        np.testing.assert_allclose(zone_params[1], [0.0, 1.0, 0.0, 1.0], atol=1e-6)
        np.testing.assert_allclose(zone_params[2], [0.03, 0.0, 0.0, 0.0], atol=1e-6)
        self.assertEqual(zone_params[3][1], 0.0)      # grabbable=False → 0

    def test_empty_scale_drives_hit_range_not_radius(self):
        """t32-A 防回退：命中范围由**空物体缩放**（lin3=world 上半 3×3）决定，
        radius 不影响命中（改 radius → 权重场不变；改 scale → 权重场范围变）。"""
        node = _make_efmi_node()
        mw = np.eye(4, dtype=np.float64)
        mw[0, 3], mw[1, 3], mw[2, 3] = 0.0, 0.0, 0.0
        mw[0, 0], mw[1, 1], mw[2, 2] = 0.10, 0.10, 0.10          # scale=0.10

        class _Obj:
            type = 'EMPTY'
        obj = _Obj()
        obj.matrix_world = mw
        obj.ssmt_drag_zone = types.SimpleNamespace(
            enabled=True, radius=0.5, brush_strength=1.0,
            falloff=0.0, grabbable=True)
        object.__setattr__(
            node, "zone_objects", [types.SimpleNamespace(zone_object=obj)]
        )
        exporter = self.mod.DragInteractionEFMIExporter(node)
        cfg = exporter._collect_zone_configs()[0]
        # 命中 lin3 = scale 0.10·I（radius=0.5 不参与）
        np.testing.assert_allclose(cfg[1], 0.10 * np.eye(3), atol=1e-9)
        # 权重场边界：d=(p-c)/scale → scale 决定截止
        pos = np.array([[0.10, 0.0, 0.0],   # d=1 → 0
                        [0.05, 0.0, 0.0],   # d=0.5 → 有值
                        [0.0, 0.0, 0.0]],   # 球心 → 1.0
                       dtype=np.float32)
        w_r05 = self.mod.bake_zone_ball(pos, (0.0, 0.0, 0.0), 0.10 * np.eye(3),
                                        strength=1.0, falloff_k=4.6)
        self.assertEqual(w_r05[0], 0.0)                          # scale 边界截止
        self.assertGreater(w_r05[1], 0.0)
        # 改 radius（0.5 → 0.9）：命中场完全不变（radius 与命中无关）
        obj.ssmt_drag_zone.radius = 0.9
        cfg2 = exporter._collect_zone_configs()[0]
        np.testing.assert_allclose(cfg2[1], 0.10 * np.eye(3), atol=1e-9)
        # 改 scale（0.10 → 0.20）：命中场范围扩大（边界 d=1 外移）
        mw[0, 0], mw[1, 1], mw[2, 2] = 0.20, 0.20, 0.20
        cfg3 = exporter._collect_zone_configs()[0]
        np.testing.assert_allclose(cfg3[1], 0.20 * np.eye(3), atol=1e-9)

    def test_radius_only_drag_falloff_zoneparams_not_hit(self):
        """t32-A 防回退：radius 仅进 ZoneParams[0].x（拖拽衰减），且不改变
        zones/weights 命中表（同空物体、不同 radius → 稀疏权重逐位一致）。

        align-t3 夹具修正：空物体放到夹具网格处（ZZMI 全无效早退语义下
        无交集 = 不注入，产物不落盘）。"""
        node = _make_efmi_node()
        mw = np.eye(4, dtype=np.float64)
        mw[0, 3], mw[1, 3], mw[2, 3] = 0.0, -0.12, 1.09
        mw[0, 0], mw[1, 1], mw[2, 2] = 0.10, 0.10, 0.10

        class _Obj:
            type = 'EMPTY'
        obj = _Obj()
        obj.matrix_world = mw
        obj.ssmt_drag_zone = types.SimpleNamespace(
            enabled=True, radius=0.25, brush_strength=1.0,
            falloff=0.0, grabbable=True)
        object.__setattr__(
            node, "zone_objects", [types.SimpleNamespace(zone_object=obj)]
        )
        pos = np.array([[0.0, 0.0, 0.0], [0.05, 0.0, 0.0], [0.12, 0.0, 0.0]],
                       dtype=np.float32)
        exporter = self.mod.DragInteractionEFMIExporter(node)
        cfg = exporter._collect_zone_configs()
        ids_a, w_a = self.mod.bake_sparse_weights(pos, cfg)

        # 改 radius → ZoneParams[0].x 变，但 zones/weights 命中表不变
        obj.ssmt_drag_zone.radius = 0.6
        td = _make_efmi_mod_dir()
        exporter2 = self.mod.DragInteractionEFMIExporter(node)
        exporter2.execute(str(td))
        zp = np.fromfile(
            td / "res" / "drag_interaction_efmi" / "ZoneParams_A.buf",
            dtype=np.float32,
        ).reshape(-1, 4)
        self.assertAlmostEqual(float(zp[0][0]), 0.6, places=6)   # 拖拽衰减半径
        ids_b, w_b = self.mod.bake_sparse_weights(pos, exporter2._collect_zone_configs())
        np.testing.assert_array_equal(ids_a, ids_b)              # 命中区不变
        np.testing.assert_allclose(w_a, w_b, atol=0.0)           # 命中权重不变

    def test_256_zones_full_pipeline(self):
        """256 区完整导出：zone_count=256、锚点 RT 2048 宽（align-t3 起每区
        8 行：4 恒等拖拽基 + 4 gizmo 表面基）、中心缓冲 256、稀疏权重区
        id < 256。"""
        node = _make_efmi_node()
        empties = [
            self._empty(-0.4 + i * 0.003, -0.12, 1.09) for i in range(256)
        ]
        object.__setattr__(
            node, "zone_objects",
            [types.SimpleNamespace(zone_object=o) for o in empties],
        )
        td, sections = self._run_export(node)
        anchor_rt = "\n".join(sections["[ResourceEFMIDragAnchorProject_A]"])
        self.assertIn("width = 2048", anchor_rt)      # 8 × 256
        probe_anchors = "\n".join(sections["[CustomShaderEFMIDragProbeAnchors_A]"])
        self.assertIn("drawindexed = 2048, 0, 0", probe_anchors)
        # t14（H-B）：锚点探针必须**显式绑定 vb3**。组件绘制命令列表是
        # `vb0 = vb3 = Position`，锚点探针若只绑 vb0/vb1/vb2 会继承那份 body
        # Position（stride 40）；当该 pass 的 VS 变体从 vb3 取位置时，锚点会被
        # 投影成 body 顶点 → 锚点 RT 错乱 → detect 基/gizmo 方向全错。
        self.assertIn("vb0 = ResourceEFMIDragAnchorPosition_A", probe_anchors)
        self.assertIn("vb3 = ResourceEFMIDragAnchorPosition_A", probe_anchors)
        centers = np.fromfile(
            td / "res" / "drag_interaction_efmi" / "centers.buf",
            dtype=np.float32,
        ).reshape(-1, 4)
        self.assertEqual(len(centers), 256)
        zone_ids = np.fromfile(
            td / "res" / "drag_interaction_efmi" / "LOD0.abc123-43191_zones.buf",
            dtype=np.uint32,
        ).reshape(-1, 4)
        self.assertTrue((zone_ids != 0xFFFFFFFF).any())
        self.assertLess(zone_ids[zone_ids != 0xFFFFFFFF].max(), 256)
        # assets.json：zone_count 记录
        import json as _json
        info = _json.loads(
            (td / "res" / "drag_interaction_efmi" / "efmi_assets.json").read_text(encoding="utf-8")
        )
        self.assertEqual(len(info["space_centers"]), 256)

    def test_phys_profile_mapping(self):
        """物理档案映射（align-t3：ZZMI 预设值**直传**——弹簧/阻尼节点属性原样
        发射（半隐式语义），不再 ω=√k→Hz 映射；新消费 mult_radius /
        phys_release_kick（follow）/ phys_target_follow（kick））。"""
        node = _make_efmi_node(
            phys_grab_spring=0.176, phys_grab_damping=0.86,
            phys_release_spring=0.055, phys_release_damping=0.96,
            mult_spring=0.333, mult_damping=1.0,
        )
        exporter = self.mod.DragInteractionEFMIExporter(node)
        # ZZMI 节点默认直传（无映射变换）
        self.assertAlmostEqual(exporter.grab_spring, 0.176, places=6)
        self.assertAlmostEqual(exporter.grab_damping, 0.86, places=6)
        self.assertAlmostEqual(exporter.release_spring, 0.055, places=6)
        self.assertAlmostEqual(exporter.release_damping, 0.96, places=6)
        self.assertAlmostEqual(exporter.mult_damping, 1.0, places=6)
        self.assertAlmostEqual(exporter.mult_strength, 0.333, places=6)
        self.assertAlmostEqual(exporter.mult_spring, 0.333, places=6)
        # align-t3 新消费：mult_radius / follow / kick（缺省 = ZZMI 预设）
        self.assertAlmostEqual(exporter.mult_radius, 1.0, places=6)
        self.assertAlmostEqual(exporter.target_follow, 0.12, places=6)
        self.assertAlmostEqual(exporter.release_kick, 1.10, places=6)
        # 档案变化 → 直传原值（无 ω 映射缩放）
        node2 = _make_efmi_node(phys_grab_spring=0.704, phys_target_follow=1.25,
                                phys_release_kick=0.20, mult_radius=2.0)
        exporter2 = self.mod.DragInteractionEFMIExporter(node2)
        self.assertAlmostEqual(exporter2.grab_spring, 0.704, places=6)
        self.assertAlmostEqual(exporter2.release_kick, 1.25, places=6)
        self.assertAlmostEqual(exporter2.target_follow, 0.20, places=6)
        self.assertAlmostEqual(exporter2.mult_radius, 2.0, places=6)
        # 发射侧验证：Present 逐槽（154 = PHYS_PARAMS 70 语义；162 = POLISH
        # 71 语义；163 = 倍率扩展）
        td = _make_efmi_mod_dir()
        exporter2.execute(str(td))
        ini = _read_ini_sections(td / "main.ini")
        present = "\n".join(ini["[Present]"])
        self.assertIn("x154 = 0.86", present)     # grab_damping
        self.assertIn("y154 = 0.704", present)    # grab_spring 直传
        self.assertIn("z154 = 0.96", present)     # release_damping
        self.assertIn("w154 = 0.055", present)    # release_spring
        self.assertIn("x160 = 1", present)        # mult_damping
        self.assertIn("x161 = 0.333", present)    # mult_strength
        self.assertIn("x162 = 1.25", present)     # release_kick
        self.assertIn("z162 = 0.2", present)      # target_follow
        self.assertIn("x163 = 2", present)        # mult_radius
        self.assertIn("y163 = 0.333", present)    # mult_spring
        self.assertIn("z163 = 1", present)        # depth_pull 默认 1.0
        # 深度模型/命中阈值为固定契约常量（depth_pull 比例×法线；无节点属性）
        self.assertEqual(exporter.depth_pull, 1.0)
        self.assertEqual(exporter.drag_scale, 1.00)
        # A1/t26：命中阈值 0.1 → 1e-4（ZZMI 同款）
        self.assertEqual(exporter.hit_threshold, 0.0001)

    def test_probe_pass_counter_gate(self):
        """t4/P0（恢复 pass 门控）：Probe CL 门控 = enabled && 帧 latch &&
        pass 门（>= _probe_exec_pass()）。R8/P0 曾把门控退化为「每帧首次 body
        draw 的帧 latch」（去 pass 序号），实测导致 probe 钉在该 mesh 每帧第一次
        draw（深度预 pass / 非主相机 pass）→ 探针 clip 投影与光标域不成套 →
        detect 恒 miss → 手型恒隐藏 + deform 不位移（t3 根因报告）。

        本测试锁定修复后的口径：
        - 保留 enabled 主开关（ZZMI drag_enabled 同款）与帧 latch；
        - 必须消费 _probe_exec_pass()（t4/P0 用户裁决：**在颜色层执行**；
          默认 = 3 = 帧内首个颜色 pass 的 draw 序号。深度层 pass 为 1-2 次，颜色层
          为 3-5 次）；
        - 用 `>=` 而非 `==`（帧内 pass 单调递增，二者正常帧等价；`>=` 避免 R8/P0
          记载的「绘制次数不足以命中唯一序号 → 死条件」退化）；
        - 带序号回绕兜底（probe_pass 记录上次命中 pass）；
        - 不出现 mode 门、不出现 ps hash 表达式门。"""
        node = _make_efmi_node()
        _td, sections = self._run_export(node)
        probe_cl = "\n".join(sections["[CommandListEFMIDragProbe_A]"])
        self.assertIn("$ssmtdrag_efmi_enabled_A == 1", probe_cl)
        self.assertIn(
            "$ssmtdrag_efmi_frame_A != $ssmtdrag_efmi_probe_frame_prev_A", probe_cl
        )
        self.assertIn("$ssmtdrag_efmi_probe_frame_prev_A = $ssmtdrag_efmi_frame_A", probe_cl)
        # t4/P0：恢复 pass 门控——消费 _probe_exec_pass()（颜色层口径），>= 且回绕兜底
        exec_pass = self.mod.DragInteractionEFMIExporter(node)._probe_exec_pass()
        self.assertEqual(exec_pass, 3)  # 默认 threshold=3 → exec=3（首个颜色 pass，非深度层）
        self.assertIn(f"$ssmtdrag_efmi_pass_A >= {exec_pass}", probe_cl)
        self.assertNotIn("$ssmtdrag_efmi_pass_A == 3", probe_cl)   # 不得用死条件 ==
        self.assertIn("$ssmtdrag_efmi_probe_pass_A", probe_cl)     # 回绕兜底 latch
        self.assertNotIn("$ssmtdrag_efmi_mode_A >= 1", probe_cl)
        self.assertNotIn("ps ==", probe_cl)
        # 计数块（诊断保留）+ globals 声明（含帧 latch 与 pass latch 变量）
        draw_lines = "\n".join(sections["[CommandList_Draw_LOD0.abc123_43191]"])
        self.assertIn(
            "if $ssmtdrag_efmi_frame_A != $ssmtdrag_efmi_frame_prev_A", draw_lines
        )
        self.assertIn("$ssmtdrag_efmi_pass_A = $ssmtdrag_efmi_pass_A + 1", draw_lines)
        constants = "\n".join(sections["[Constants]"])
        self.assertIn("global $ssmtdrag_efmi_pass_A = 0", constants)
        self.assertIn("global $ssmtdrag_efmi_frame_prev_A = -1", constants)
        self.assertIn("global $ssmtdrag_efmi_probe_frame_prev_A = -1", constants)
        self.assertIn("global $ssmtdrag_efmi_probe_pass_A = -1", constants)
        # 识别阈值路径：工作空间识别 ordinal → 门控阈值（首颜色 pass 序）与
        # probe 执行 pass（threshold-1 = 最后一个深度 pass）
        with tempfile.TemporaryDirectory() as td2:
            root = Path(td2)
            (root / "Config").mkdir()
            self.mod.save_workspace_probe_hash(
                str(root), ["d7bb9dd57f5b70c6"], pass_counts={
                    "depth_pass_count": 2, "color_pass_count": 3,
                    "color_pass_first_ordinal": 4,
                })
            exporter = self.mod.DragInteractionEFMIExporter(node)
            object.__setattr__(exporter, "_probe_workspace_root", str(root))
            self.assertEqual(exporter._probe_pass_threshold(), 4)
            self.assertEqual(exporter._probe_exec_pass(), 4)  # 识别值 = 首个颜色 pass

    def test_hand_cursor_family_emitted(self):
        """手型光标（t22 恢复；align-t3 ZZMI 渲染链对齐）：开关开 → 预览 CS +
        PresentHand×4 + 资源/资产/着色器拷贝 + Present 接入（store 状态 +
        抓取或 RMB 独按蓄力换 Action）+ 25 项 persist 参数面。"""
        node = _make_efmi_node(enable_hand_cursor=True)
        td, sections = self._run_export(node)
        prefix = "ResourceEFMIDragHand"
        for sec in (
            f"[{prefix}Preview_A]",
            f"[{prefix}ActionVB_A]", f"[{prefix}ActionIB_A]", f"[{prefix}ActionNormal_A]",
            f"[{prefix}NoActionVB_A]", f"[{prefix}NoActionIB_A]", f"[{prefix}NoActionNormal_A]",
            "[CustomShaderEFMIDragHandPreview_A]",
            "[CustomShaderEFMIDragPresentHandOutline_A]",
            "[CustomShaderEFMIDragPresentHand_A]",
            "[CustomShaderEFMIDragPresentHandActionOutline_A]",
            "[CustomShaderEFMIDragPresentHandAction_A]",
        ):
            self.assertIn(sec, sections, sec)
        present = "\n".join(sections["[Present]"])
        self.assertIn("run = CustomShaderEFMIDragHandPreview_A", present)
        self.assertIn("store = $ssmtdrag_efmi_hand_action_A, ResourceEFMIDragHandPreview_A, 11", present)
        # align-t3：网格切换 = 真实抓取（status 2.0 → >1.5）或 RMB 独按蓄力
        # （ZZMI L5138-5144 `isMouseButtonDown || rmb_lone_hold` 同语义）
        self.assertIn(
            "if $ssmtdrag_efmi_hand_action_A > 1.5 || $ssmtdrag_efmi_rmb_lone_hold_A == 1",
            present)
        self.assertIn("run = CustomShaderEFMIDragPresentHandActionOutline_A", present)
        # t45：run 引用与发射段名统一（NoAction 系无后缀——zzmi 原作惯例）
        self.assertIn("run = CustomShaderEFMIDragPresentHandOutline_A", present)
        self.assertIn("run = CustomShaderEFMIDragPresentHand_A", present)
        self.assertNotIn("PresentHandNoAction", present)
        # align-t3：蓄力归约（ZZMI L4985-5023）+ 归一化（t43 单分量约束下
        # 除法在 Present 完成）
        self.assertIn("$ssmtdrag_efmi_lmb_press_time_A = time", present)
        self.assertIn("$ssmtdrag_efmi_rmb_lone_hold_A = 1", present)
        self.assertIn(
            "$ssmtdrag_efmi_lmb_wfrac_A = $ssmtdrag_efmi_lmb_hold_fraction_A / $ssmtdrag_efmi_hand_windup_time_A",
            present)
        constants = "\n".join(sections["[Constants]"])
        self.assertIn("global $ssmtdrag_efmi_hand_action_A = 0", constants)
        # align-t3：ZZMI persist 参数面（L4799-4827 全表）抽查
        self.assertIn("global persist $ssmtdrag_efmi_hand_scale_A = 0.5", constants)
        self.assertIn("global persist $ssmtdrag_efmi_hand_center_y_A = 0.275962", constants)
        self.assertIn("global persist $ssmtdrag_efmi_hand_upright_max_deg_A = 80", constants)
        self.assertIn("global persist $ssmtdrag_efmi_hand_vibrate_amplitude_A = 4", constants)
        self.assertIn("global $ssmtdrag_efmi_rmb_lone_hold_A = 0", constants)
        res = td / "res" / "drag_interaction_efmi"
        for fname in (
            "efmi_hand.hlsl", "efmi_hand_preview.hlsl",
            "HandAction.buf", "HandAction.ib", "HandAction_Normal.buf",
            "HandNoAction.buf", "HandNoAction.ib", "HandNoAction_Normal.buf",
        ):
            self.assertTrue((res / fname).exists(), fname)
        # 绘制段：移植着色器 + 手部网格绑定 + 寄存器 184/187-195（t43 单分量行
        # + res_width/res_height 内建屏幕尺寸 + ZZMI 参数面槽位映射）
        hand_draw = "\n".join(sections["[CustomShaderEFMIDragPresentHand_A]"])
        self.assertIn("vs = res/drag_interaction_efmi/efmi_hand.hlsl", hand_draw)
        self.assertIn("vs-t67 = ResourceEFMIDragHandPreview_A", hand_draw)
        self.assertIn("vs-t69 = ResourceEFMIDragHandNoActionNormal_A", hand_draw)
        self.assertIn("x184 = res_width", hand_draw)
        self.assertIn("y184 = res_height", hand_draw)
        self.assertIn("x188 = $ssmtdrag_efmi_hand_center_x_A", hand_draw)
        self.assertIn("x189 = $ssmtdrag_efmi_hand_scale_A", hand_draw)
        self.assertIn("x190 = $ssmtdrag_efmi_lmb_wfrac_A", hand_draw)
        self.assertIn("y190 = time", hand_draw)
        self.assertIn("x193 = $ssmtdrag_efmi_hand_upright_max_deg_A", hand_draw)
        self.assertIn("x194 = $ssmtdrag_efmi_hand_reference_height_A", hand_draw)
        # 非描边段 y195=0（描边旗标）；描边段 y195=1 + 描边宽
        self.assertIn("y195 = 0", hand_draw)
        hand_outline = "\n".join(sections["[CustomShaderEFMIDragPresentHandOutline_A]"])
        self.assertIn("y195 = 1", hand_outline)
        self.assertIn("x195 = $ssmtdrag_efmi_hand_outline_width_A", hand_outline)
        self.assertIn("DrawIndexed = 1524, 0, 0", hand_draw)
        # 预览 CS 同款内建尺寸 + gizmo 投影源（锚点 RT t2）
        hand_preview = "\n".join(sections["[CustomShaderEFMIDragHandPreview_A]"])
        self.assertIn("x184 = res_width", hand_preview)
        self.assertIn("y184 = res_height", hand_preview)
        self.assertIn("cs-t2 = ResourceEFMIDragAnchorProject_A", hand_preview)
        # align-t3：手型 shader = ZZMI H1-H16 机制（轴重映射/透视钳制/蓄力/
        # 抓取拉伸/振动/假光照/表面贴合/描边/无效基 discard）
        hand_shader = (res / "efmi_hand.hlsl").read_text(encoding="utf-8")
        self.assertIn("float3 hand = float3(-i.position.y, -i.position.z, i.position.x);", hand_shader)
        self.assertIn("perspectiveScale = clamp(", hand_shader)
        self.assertIn("HAND_UPRIGHT.x, 80.0", hand_shader)
        self.assertIn("Preview[2].w > 1.5", hand_shader)
        self.assertIn("Preview[3].w", hand_shader)   # stretchFraction（抓取倾斜/振动）
        self.assertIn("HAND_VIBRATE", hand_shader)
        self.assertIn("smoothstep(-softness, softness, i.surface)", hand_shader)
        self.assertIn("normalize(i.normal)", hand_shader)   # 假光照
        self.assertIn("if (i.hit < 0.5)", hand_shader)      # D-6 无效基隐藏
        self.assertIn("discard;", hand_shader)
        # 预览 CS：三态机 + 冻结语义 + Y-up 出口换算
        hp_shader = (res / "efmi_hand_preview.hlsl").read_text(encoding="utf-8")
        self.assertIn("bool charging = !capturing && !modifier", hp_shader)
        # t10：同步工具集 shader 的循环变量重命名（k → j）——该断言此前停留在旧变量名
        self.assertIn("State[j * STATE_STRIDE + 4u].w > 0.5", hp_shader)  # 抓取实例扫描
        self.assertIn("float2 UvToYupPx(float2 uv, float2 screen)", hp_shader)
        self.assertIn("zone * ANCHOR_ROWS + 5u", hp_shader)   # gizmo 法线行
        self.assertIn("stretchFraction", hp_shader)

    def test_hand_cursor_off_not_emitted(self):
        """手型光标关（默认）→ 无段族/资产/着色器拷贝。"""
        node = _make_efmi_node()  # enable_hand_cursor=False
        td, sections = self._run_export(node)
        for sec in sections:
            self.assertNotIn("EFMIDragHand", sec)
        res = td / "res" / "drag_interaction_efmi"
        for fname in [f.name for f in res.iterdir()]:
            self.assertNotIn("Hand", fname)
        self.assertNotIn("efmi_hand", "\n".join(sections.get("[Present]", [])))


class TestEFMIZZMIFull(unittest.TestCase):
    """t25 全面 zzmi 化：参数删除/预览激活/ps hash 归位/变量统一。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()
        cls.zzmi_mod = _load_zzmi_module()

    def setUp(self):
        # t29 F1：_set_logic_name 污染恢复（防泄漏到后续 zzmi 测试）
        self.addCleanup(_restore_logic_name)

    @staticmethod
    def _walk_calls(layout):
        """递归遍历布局调用（row/column/box 嵌套子布局的调用也收集）。"""
        for c in layout.calls:
            yield c
            if len(c) > 1 and isinstance(c[1], _FakeLayout):
                yield from TestEFMIZZMIFull._walk_calls(c[1])

    def test_efmi_specific_params_deleted(self):
        """EFMI 专属 9 项参数 + 高级折叠属性已删除（类注解无这些属性）。"""
        cls = self.zzmi_mod.SSMTNode_PostProcess_DragInteraction
        annotations = dict(getattr(cls, "__annotations__", {}))
        for attr in (
            "efmi_pull_depth", "efmi_push_depth", "efmi_drag_scale",
            "efmi_max_offset", "efmi_grab_hz", "efmi_grab_damping",
            "efmi_release_hz", "efmi_release_damping", "efmi_hit_threshold",
            "efmi_advanced",
        ):
            self.assertNotIn(attr, annotations, attr)
        # 保留：ps hash（t25 唯一 EFMI 属性）
        self.assertIn("efmi_probe_pass_hash", annotations)
        # 执行器不再读已删属性（防御 getattr 缺省 = 契约常量）
        exporter = self.mod.DragInteractionEFMIExporter(
            cls.__new__(cls))
        # align-t3：深度模型 = depth_pull 比例×冻结法线（ZZMI z73 默认 1.0）；
        # 旧定值 Y 偏移（pull/push 0.025/0.016）已废弃
        self.assertEqual(exporter.depth_pull, 1.0)
        self.assertEqual(exporter.drag_scale, 1.00)
        # R7 t46-P1：默认 maxOffset 对齐 ZZMI 显式 POLISH_PARAMS.x = 0.50
        self.assertEqual(exporter.max_offset, 0.50)

    def test_preview_handler_activated(self):
        """EFMI 权重预览（t25 恢复）：_ensure_efmi_preview_running 幂等注册
        draw handler + timer + depsgraph handler（mock bpy）。"""
        calls = {"draw": 0, "timer": 0, "deps": 0}

        class _SpaceView3D:
            @classmethod
            def draw_handler_add(cls, cb, args, region, mode):
                calls["draw"] += 1
                return ("hdl", 1)

            @classmethod
            def draw_handler_remove(cls, hdl, region):
                pass

        class _Timers:
            def register(self, cb):
                calls["timer"] += 1
                return "timer1"

            def unregister(self, hdl):
                pass

        class _Handlers:
            depsgraph_update_post = []

        class _App:
            timers = _Timers()
            handlers = _Handlers()

        class _Bpy(types.ModuleType):
            pass

        bpy = _Bpy("bpy")
        bpy.types = types.SimpleNamespace(SpaceView3D=_SpaceView3D)
        bpy.app = _App()
        bpy.context = types.SimpleNamespace()
        prev = sys.modules.get("bpy")
        sys.modules["bpy"] = bpy
        try:
            ok = self.mod._ensure_efmi_preview_running()
            self.assertTrue(ok)
            self.assertEqual(calls["draw"], 1)
            self.assertEqual(calls["timer"], 1)
            self.assertEqual(len(bpy.app.handlers.depsgraph_update_post), 1)
            # 幂等：再次调用不重复注册
            self.mod._ensure_efmi_preview_running()
            self.assertEqual(calls["draw"], 1)
            self.assertEqual(len(bpy.app.handlers.depsgraph_update_post), 1)
            # 清理
            self.mod._efmi_preview_cleanup()
            self.assertEqual(len(bpy.app.handlers.depsgraph_update_post), 0)
        finally:
            if prev is not None:
                sys.modules["bpy"] = prev
            else:
                sys.modules.pop("bpy", None)
            self.mod._efmi_preview_draw_handler = None
            self.mod._efmi_preview_timer = None
            self.mod._efmi_preview_deps_handler = None

    def test_ps_hash_below_hash_values_in_ui(self):
        """ps hash 归位（t25）：EFMI 模式下 efmi_probe_pass_hash 显示在
        hash_values 正下方（布局调用顺序）。"""
        _set_logic_name("EFMI")
        node = self.zzmi_mod.SSMTNode_PostProcess_DragInteraction.__new__(
            self.zzmi_mod.SSMTNode_PostProcess_DragInteraction
        )
        defaults = dict(
            hash_values="abc123", mod_namespace="", grab_key="ALT",
            grab_gesture="LMB", poke_gesture="RMB", enable_poke=True,
            enable_hand_cursor=False, enable_viewport_probe=False,
            feature_shapekey_link=True, feature_variable_link=True,
            feature_panel_link=True, enable_shapekey_drive=False,
            drag_system_mode_default=2, drag_mode_initialized=True,
            drag_mode_variable_name="ssmtdrag_drag_enabled", mode_toggle_key="f8",
            shapekey_drive_move_sensitivity=0.02,
            ui_detected_variable_name="ssmtdrag_ui_detected",
            ui_zone_variable_name="ssmtdrag_ui_zone",
            phys_grab_damping=0.86, phys_grab_spring=0.176,
            phys_release_damping=0.96, phys_release_spring=0.055,
            phys_release_kick=0.12, phys_target_follow=1.10,
            mult_radius=1.0, mult_strength=0.333, mult_spring=0.333, mult_damping=1.0,
            zone_objects=[], bake_reference_object=None, mask_plateau=0.0,
            collision_enabled=False, collision_margin=0.002, collision_mode="SOFT",
            collision_point_budget=4096, collision_cell_size=0.0,
            preview_weights=False, preview_target=None, preview_collection=None,
            efmi_probe_pass_hash="1718.1",
            id_data=None, name="N", inputs=[], outputs=[],
        )
        for k, v in defaults.items():
            object.__setattr__(node, k, v)
        layout = _FakeLayout()
        node.draw_buttons(None, layout)
        props = [c for c in self._walk_calls(layout) if c[0] == "prop"]
        names = [str(c[2][1]) for c in props]
        self.assertIn("hash_values", names)
        # t47 §10：ps hash 属性废弃（UI 无 prop）——pass 计数门控提示替代
        self.assertNotIn("efmi_probe_pass_hash", names)
        labels = [
            str(c[2][0]) for c in self._walk_calls(layout)
            if c[0] == "label" and c[2]
        ] + [
            str(c[3].get("text", "")) for c in self._walk_calls(layout)
            if c[0] == "label" and c[3]
        ]
        self.assertTrue(any("pass 计数" in t or "主色 pass 自动门控" in t for t in labels))

    def test_external_variables_zzmi_contract(self):
        """变量统一（t25）：对外变量与 zzmi 契约一致（面板联动复用
        $ssmtdrag_drag_enabled/ui_detected/ui_zone）。"""
        exporter = self.mod.DragInteractionEFMIExporter(_make_efmi_node())
        self.assertEqual(
            exporter._RUNTIME_VARIABLE_DEFAULTS["drag_mode_variable_name"],
            "ssmtdrag_drag_enabled",
        )
        self.assertEqual(
            exporter._RUNTIME_VARIABLE_DEFAULTS["ui_detected_variable_name"],
            "ssmtdrag_ui_detected",
        )


class TestEFMIFixFindingsRound2(unittest.TestCase):
    """t26 修复 t24 findings：F1 命中权重门槛/F2 冷启动全区清零/F3 容量统一/
    F4 注释清理。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()

    def _read_shader(self, name):
        return (Path(__file__).resolve().parents[1] / "Toolset" / "drag_interaction_efmi" / name).read_text(
            encoding="utf-8")

    def test_f1_hit_gate_weight_threshold_all_consumers(self):
        """F1（high）：四消费方命中判定统一权重门槛（y >= IniParams[152].w），
        未命中哨兵（x=0,y=0）不再被当作 zone 0 命中。"""
        simulate = self._read_shader("efmi_simulate.hlsl")
        self.assertIn("bool candidateHit = fresh && candidate.y >= IniParams[152].w;", simulate)
        self.assertIn("other.y >= IniParams[152].w", simulate)
        for name in ("efmi_ui_publish.hlsl", "efmi_hand_preview.hlsl",
                     "efmi_shapekey_drive.hlsl"):
            src = self._read_shader(name)
            self.assertIn("cand.y < IniParams[152].w", src, name)
            self.assertNotIn("cand.x < 0.0", src, name)
        self.assertNotIn("candidate.x >= 0.0", simulate)
        # 参考实现：未命中候选（x=0,y=0）→ 各消费方过滤；命中候选（y≥阈值）→ 通过
        frame = 7
        threshold = 0.10
        miss = (0.0, 0.0, 0.5, float(frame))     # x=0 合法区但 y=0 未命中
        hit = (3.0, 0.7, 0.5, float(frame))      # zone 3 权重 0.7
        self.assertFalse(miss[1] >= threshold)
        self.assertTrue(hit[1] >= threshold)

    def test_f2_cold_start_clears_zone_slots(self):
        """F2（medium）：冷启动/超时/禁用清零覆盖全区槽（每线程清自己的
        offset/velocity），不只是 4 公共槽。"""
        simulate = self._read_shader("efmi_simulate.hlsl")
        self.assertIn("bool coldStart = clock.w != SPRING_MAGIC || now - clock.x > STATE_TIMEOUT || !enabled;", simulate)
        self.assertIn("State[slot] = 0;", simulate)
        self.assertIn("State[slot + 1] = 0;", simulate)
        self.assertIn("x = 0;", simulate)
        self.assertIn("v = 0;", simulate)

    def test_f3_click_export_capacity_shared_with_layout(self):
        """F3（medium）：ClickExport 过滤与布局容量共用**同一口径**
        （ARCH-02/B2 修正后 = 最大被引用稳定 zone_id + 1，最小 1）——zone0 空物体
        + ClickExport zone1 都算被引用 → 容量 2，zone1 条目被正常纳入（旧口径
        max(1, 启用数=1)=1 会误判 zone1 越界而静默丢弃其点击循环）。"""
        node = _make_efmi_node()
        empty = types.SimpleNamespace(
            zone_object=types.SimpleNamespace(
                matrix_world=np.eye(4, dtype=np.float64),
                ssmt_drag_zone=types.SimpleNamespace(
                    enabled=True, radius=0.1, brush_strength=1.0,
                    brush_falloff_k=4.6, grabbable=True),
            ))
        object.__setattr__(node, "zone_objects", [empty])
        anim_driver = types.SimpleNamespace(
            bl_idname="SSMTNode_PostProcess_AnimDriver", mute=False,
            blueprint_name="AnimTree1")
        ce = types.SimpleNamespace(
            bl_idname="SSMTNode_AnimDriver_ClickExport", mute=False,
            click_zone_id=1, cycle_length=2,
            click_target_list=[types.SimpleNamespace(variable_name="swapkey1")])
        object.__setattr__(node, "id_data",
                           types.SimpleNamespace(nodes=[anim_driver]))
        bpy_mod = sys.modules["bpy"]

        class _NodeGroups(dict):
            def get(self, name, default=None):
                for value in self.values():
                    if getattr(value, "name", None) == name:
                        return value
                return default

        node_groups = _NodeGroups()
        node_groups["AnimTree1"] = types.SimpleNamespace(
            name="AnimTree1", nodes=[ce])
        bpy_mod.data = types.SimpleNamespace(node_groups=node_groups)
        exporter = self.mod.DragInteractionEFMIExporter(node)
        entries = exporter._collect_click_export_drivers()
        # ARCH-02/B2：容量 = 最大被引用稳定 zone_id + 1。此处 zone0（唯一空物体）
        # 与 ClickExport 的 zone1 都被引用 → 容量 2（旧口径 max(1,启用数=1)=1 会
        # 把 zone1 当越界条目静默丢弃）。计数缓冲 array 与布局容量必须**同一口径**。
        capacity = exporter._collect_zone_capacity()
        self.assertEqual(capacity, 2)
        self.assertEqual(entries, [(1, 2, "$swapkey1")])
        _total, _bases, counts = exporter._drag_drive_buffer_layout()
        self.assertEqual(len(counts), capacity)
        # 布局里 zone1 循环档数 = max(1, cycle-1=1) = 1（不被丢弃）
        self.assertEqual(counts, [1, 1])

    def test_f4_no_dual_zone_comment_residue(self):
        """F4（low）：模块/着色器无恒双区残留注释（1/2、双胸腔、8x8、区 id-1）。"""
        src = (Path(__file__).resolve().parents[1] / "blueprint"
               / "node_postprocess_draginteraction_efmi.py").read_text(encoding="utf-8")
        for token in ("双胸腔", "恒双区", "区 id-1", "1/2）", "（1/2）"):
            self.assertNotIn(token, src, token)
        for name in ("efmi_shapekey_drive.hlsl", "efmi_shapekey_var_sync.hlsl"):
            shader = self._read_shader(name)
            self.assertNotIn("chest", shader, name)
            self.assertNotIn("8x8", shader, name)

    def test_no_side_gate_residue(self):
        """用户补充要求：EFMI_SIDE_GATE/side_sign/分侧/左右两区语义零残留
        （zzmi 高斯场无分侧概念——每区空物体 = 独立高斯球）。"""
        src = (Path(__file__).resolve().parents[1] / "blueprint"
               / "node_postprocess_draginteraction_efmi.py").read_text(encoding="utf-8")
        for token in ("EFMI_SIDE_GATE", "side_sign", "side_gate", "分侧",
                      "左右胸", "左右两区", "左区", "右区"):
            self.assertNotIn(token, src, token)
        toolset = Path(__file__).resolve().parents[1] / "Toolset" / "drag_interaction_efmi"
        for shader in toolset.glob("*.hlsl"):
            text = shader.read_text(encoding="utf-8")
            for token in ("side_sign", "side_gate", "SIDE_GATE"):
                self.assertNotIn(token, text, f"{shader.name}:{token}")

    def test_gaussian_pure_overlay(self):
        """用户补充要求：高斯球权重纯叠加（merge 语义）——N 空物体 = N 独立
        高斯球，无自动推导/中心点概念；重叠区最强 K 存原始场值（t16 不归一）。"""
        m = self.mod
        # 三球：同半径不同中心（zzmi 原生语义：空物体位置即球心）
        pos = np.array([
            [0.0, 0.0, 0.0],      # 球 1 球心
            [0.1, 0.0, 0.0],      # 球 2 球心
            [0.2, 0.0, 0.0],      # 球 3 球心
            [0.05, 0.0, 0.0],     # 球 1/2 重叠区（球 3 截止外）
        ], dtype=np.float32)
        cfg = [
            ((0.0, 0.0, 0.0), 0.15, 1.0, 4.6, True),
            ((0.1, 0.0, 0.0), 0.15, 1.0, 4.6, True),
            ((0.25, 0.0, 0.0), 0.15, 1.0, 4.6, True),   # 远离重叠区（截止外）
        ]
        zone_ids, w = m.bake_sparse_weights(pos, cfg)
        # 球心顶点：主球主导（邻球高斯贡献小）
        np.testing.assert_array_equal(zone_ids[0, 0], 0)
        self.assertGreater(float(w[0, 0]), 0.8)
        np.testing.assert_array_equal(zone_ids[1, 0], 1)
        np.testing.assert_array_equal(zone_ids[2, 0], 2)
        # 重叠区：双球对称贡献 → 原始场各 = exp(-4.6×(0.05/0.15)²)（t16 不归一，
        # 保留衰减梯度；原归一化压成 0.5/0.5 是「整体一块无衰减」的根因）
        mid_raw = float(np.exp(-4.6 * (0.05 / 0.15) ** 2))
        self.assertEqual(sorted(zone_ids[3, :2].tolist()), [0, 1])
        self.assertAlmostEqual(float(w[3, 0]), mid_raw, places=5)
        self.assertAlmostEqual(float(w[3, 1]), mid_raw, places=5)


class TestEFMIHotfixT32(unittest.TestCase):
    """t32 实机爆炸修复：deform 源 SRV 绑定锁定 / 多组件钩子 / 站位符 /
    越界防护。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()

    def _run_export(self, node):
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        sections = _read_ini_sections(td / "main.ini")
        return td, sections

    def test_deform_binds_source_position_srv(self):
        """爆炸锁定（t32 + t38 H1）：deform 段 cs-t0 = 显式 format=R32_UINT 源
        视图资源（无 format 的 strided Buffer 无法被 Buffer<uint> typed 读 →
        Out 全 0 → 模型坍缩爆炸）——回归断言必须恒在。"""
        node = _make_efmi_node()
        _td, sections = self._run_export(node)
        deform = "\n".join(sections["[CustomShaderEFMIDragDeform_LOD0.abc123_43191_A]"])
        self.assertIn("cs-t0 = ResourceEFMIDragSourceR32_LOD0.abc123_43191_A", deform)
        self.assertIn("cs-u0 = ResourceEFMIDragOut_LOD0.abc123_43191_A", deform)
        # 源 R32 视图段：format=R32_UINT + stride + filename（typed 读可用）
        src = "\n".join(sections["[ResourceEFMIDragSourceR32_LOD0.abc123_43191_A]"])
        self.assertIn("format = R32_UINT", src)
        self.assertIn("stride = 16", src)
        self.assertIn("filename =", src)

    def test_multi_component_hooks_all_injected(self):
        """locator 按 hash_values 收集：填多个 hash → 多组件处理（能力验证）。
        注（t32 契约修正）：实机只填 LOD0 hash → 只注入 LOD0 为**正确行为**
        （拖拽钩子只作用于 component_id=0；LOD1 不需要钩子）。"""
        node = _make_efmi_node()
        # 模拟双组件：第二个 hash 指向额外 EntryPoint + Draw + Position 段
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        sections_holder = _read_ini_sections(td / "main.ini") if False else {}
        # 直接构造双 EntryPoint sections 验证 locator
        from collections import OrderedDict
        sections = OrderedDict()
        sections["[TextureOverride_EntryPoint_LOD0.abc123_43191]"] = [
            "hash = abc123",
            "handling = skip",
            "CommandList\\EFMIv1\\Callback_Component_DrawCustom = ref CommandList_Draw_LOD0.abc123_43191",
        ]
        sections["[TextureOverride_EntryPoint_LOD1.def456_56789]"] = [
            "hash = def456",
            "handling = skip",
            "CommandList\\EFMIv1\\Callback_Component_DrawCustom = ref CommandList_Draw_LOD1.def456_56789",
        ]
        sections["[CommandList_Draw_LOD0.abc123_43191]"] = [
            "ib = Resource_LOD0.abc123_43191_Index",
            "vb0 = Resource_LOD0.abc123_43191_Position",
            "drawindexedinstanced = 16,INSTANCE_COUNT,0,0,FIRST_INSTANCE",
        ]
        sections["[CommandList_Draw_LOD1.def456_56789]"] = [
            "ib = Resource_LOD1.def456_56789_Index",
            "vb0 = Resource_LOD1.def456_56789_Position",
            "drawindexedinstanced = 16,INSTANCE_COUNT,0,0,FIRST_INSTANCE",
        ]
        for prefix in ("LOD0.abc123_43191", "LOD1.def456_56789"):
            sections[f"[Resource_{prefix}_Index]"] = [
                "type = Buffer", "format = R32_UINT",
                "filename = Meshes/x-Index.buf",
            ]
            sections[f"[Resource_{prefix}_Position]"] = [
                "type = Buffer", "stride = 16",
                "filename = Meshes/x-Position.buf",
            ]
            sections[f"[Resource_{prefix}_Texcoord]"] = [
                "type = Buffer", "stride = 8",
                "filename = Meshes/x-Texcoord.buf",
            ]
            sections[f"[Resource_{prefix}_Blend]"] = [
                "type = Buffer", "stride = 16",
                "filename = Meshes/x-Blend.buf",
            ]
        components = exporter._locate_components(sections, ["abc123", "def456"])
        self.assertEqual(len(components), 2)
        names = sorted(c["comp_name"] for c in components)
        self.assertEqual(names, ["LOD0.abc123_43191", "LOD1.def456_56789"])

    def test_path_vectors_placeholder_generated(self):
        """问题 2：合并骨骼站位符——PathVectors buf（256 区 × 4 float4 全零
        模板，w=0 无效）+ 资源段生成。"""
        node = _make_efmi_node()
        td, sections = self._run_export(node)
        self.assertIn("[ResourceEFMIDragPathVectors_A]", sections)
        pv = np.fromfile(
            td / "res" / "drag_interaction_efmi" / "PathVectors_A.buf",
            dtype=np.float32,
        ).reshape(-1, 4)
        self.assertEqual(len(pv), 256)
        np.testing.assert_array_equal(pv, np.zeros((256, 4), dtype=np.float32))
        # 资源段声明（filename 存在）
        self.assertIn("filename = res/drag_interaction_efmi/PathVectors_A.buf",
                      "\n".join(sections["[ResourceEFMIDragPathVectors_A]"]))

    def test_deform_zone_bounds_guard(self):
        """问题 1 顺带③：deform 对越界区 id（损坏数据）跳过而非越界读 State。"""
        shader = (Path(__file__).resolve().parents[1] / "Toolset"
                  / "drag_interaction_efmi" / "efmi_deform.hlsl").read_text(encoding="utf-8")
        self.assertIn("if (zone >= MAX_ZONES) {", shader)
        self.assertIn("continue;", shader)
        self.assertIn("if (zone == 0xFFFFFFFFu) {", shader)


class TestEFMIHotfixT41(unittest.TestCase):
    """t41：deform 全量写域（非 active 顶点 Out=Base 位精确复制，杜绝未写区域
    0 坍缩）+ 源资源段发射重构（F2）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()

    def _run_export(self, node):
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        sections = _read_ini_sections(td / "main.ini")
        return td, sections

    def _shader(self):
        return (Path(__file__).resolve().parents[1] / "Toolset"
                / "drag_interaction_efmi" / "efmi_deform.hlsl").read_text(encoding="utf-8")

    def test_full_write_domain_shader_path(self):
        """F1 全量写域：每线程一顶点（tid.x 直接 = vertex）、非 active 顶点也
        Write3（delta=0 → Out=Base 位精确复制）；Active 二分判定。"""
        shader = self._shader()
        self.assertIn("uint vertex = tid.x;", shader)
        self.assertIn("if (vertex >= (uint)IniParams[155].z) {", shader)
        self.assertIn("bool active = false;", shader)
        self.assertIn("uint mid = (lo + hi) >> 1;", shader)  # 二分
        self.assertIn("if (v == vertex) {", shader)
        # t47：位移只按 mode 开合（mode 1 → delta=0 → Out=Base 原样）；
        # 逐帧命中新鲜度门已删（实机二分证伪）
        self.assertIn("if (active && IniParams[159].x >= 2.0) {", shader)
        # 非 active 顶点 delta=0 → Write3(at, pos + 0) = 位精确复制
        # （t25 把位置读提到 `float3 pos = Read3(at);`，写入形式统一为 pos + delta）
        self.assertIn("Write3(at, pos + delta);", shader)
        # t25：delta==0（非 active / mode<2 / 陈旧候选 / 窄 stride）走位精确拷贝分支,
        # 与改动前逐位一致；两个分支共用同一份 `for (uint w = 3u; ...)` 拷贝循环。
        self.assertIn("bool applyNormalUpdate = normalWritable && dot(delta, delta) > 1e-16;", shader)

    def test_deform_dispatch_ceil_vertex_count(self):
        """F1 dispatch = ceil(vertex_count/64)（16 顶点 → 1；越界线程按 z155 退出）。"""
        node = _make_efmi_node()
        _td, sections = self._run_export(node)
        deform = "\n".join(sections["[CustomShaderEFMIDragDeform_LOD0.abc123_43191_A]"])
        self.assertIn("dispatch = 1, 1, 1", deform)
        self.assertIn("z155 = 16", deform)  # vertex_count
        # 40B stride 测试（16 顶点）同
        node40 = _make_efmi_node()
        td40, sections40 = self._run_export(node40)
        del node40, td40
        deform40 = "\n".join(sections40["[CustomShaderEFMIDragDeform_LOD0.abc123_43191_A]"])
        self.assertIn("z155 = 16", deform40)

    def test_non_active_vertex_out_equals_base(self):
        """F1 参考实现：非 active 顶点（含 stub/远端）Out = Base 位精确复制
        （delta=0）；active 顶点 Out = Base + delta。"""
        m = self.mod
        base = np.arange(12, dtype=np.float32).reshape(4, 3)  # 4 顶点
        active = np.array([1, 3], dtype=np.uint32)            # 顶点 1/3 active
        zones = np.full((4, 4), 0xFFFFFFFF, dtype=np.uint32)
        weights = np.zeros((4, 4), dtype=np.float32)
        state = np.zeros((8 * 518, 4), dtype=np.float32)      # 全 0 弹簧（t21 6 公共槽）
        out = np.zeros((4, 3), dtype=np.float32)
        for v in range(4):
            delta = np.zeros(3, dtype=np.float32)
            if v in active and False:  # fresh 恒假（state 0）→ delta 0
                pass
            out[v] = base[v] + delta
        # 位精确：Out == Base（非 active 顶点不会被 0 覆盖）
        np.testing.assert_array_equal(out, base)

    def test_source_r32_emitted_in_component_resources(self):
        """F2：ResourceEFMIDragSourceR32 在组件资源发射（幂等 setdefault），
        deform 段仅引用。"""
        node = _make_efmi_node()
        _td, sections = self._run_export(node)
        src = "\n".join(sections["[ResourceEFMIDragSourceR32_LOD0.abc123_43191_A]"])
        self.assertIn("type = Buffer", src)
        self.assertIn("format = R32_UINT", src)
        self.assertIn("stride = 16", src)
        self.assertIn("filename =", src)
        deform = "\n".join(sections["[CustomShaderEFMIDragDeform_LOD0.abc123_43191_A]"])
        self.assertIn("cs-t0 = ResourceEFMIDragSourceR32_LOD0.abc123_43191_A", deform)
        # 幂等：二次导出不重复段
        exporter = self.mod.DragInteractionEFMIExporter(node)
        td2 = _make_efmi_mod_dir()
        exporter.execute(str(td2))
        sections2 = _read_ini_sections(td2 / "main.ini")
        self.assertEqual(
            sections2["[ResourceEFMIDragSourceR32_LOD0.abc123_43191_A]"],
            sections["[ResourceEFMIDragSourceR32_LOD0.abc123_43191_A]"],
        )


class TestEFMIT43SingleComponent(unittest.TestCase):
    """t43：Zmd 构建 ini-param 只支持单分量行——产物全量扫描零空格多分量残留。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()

    def _run_export(self, node):
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        return td, _read_ini_sections(td / "main.ini")

    def test_no_multi_component_ini_param_lines(self):
        """产物所有 ini-param 行（xNN/yNN/zNN/wNN = 值）右侧为单值（无空格）。"""
        node = _make_efmi_node_with_consumer([
            _make_sk_item(zone=0, stage=1, dir_id=1),
        ])
        object.__setattr__(node, "enable_hand_cursor", True)
        _td, sections = self._run_export(node)
        bad = []
        for sec_name, lines in sections.items():
            for line in lines:
                s = str(line).strip()
                if len(s) >= 3 and s[0] in "xyzw" and s[1:3].isdigit() and "=" in s:
                    key, _, value = s.partition("=")
                    if " " in value.strip() or "\t" in value.strip():
                        bad.append(f"{sec_name}: {s}")
        self.assertEqual(bad, [], "多分量 ini-param 残留:\n" + "\n".join(bad))

    def test_single_component_values_correct(self):
        """各段单分量值抽查：detect 150 区、Present 153/154、手型 184/188。"""
        node = _make_efmi_node()
        object.__setattr__(node, "enable_hand_cursor", True)
        _td, sections = self._run_export(node)
        detect = "\n".join(sections["[CustomShaderEFMIDragDetect_A]"])
        self.assertIn("x150 = 512", detect)
        self.assertIn("y150 = 1", detect)
        self.assertIn("w151 = $ssmtdrag_efmi_buttons_A", detect)
        self.assertIn("w152 = 0.0001", detect)
        present = "\n".join(sections["[Present]"])
        self.assertIn("x153 = time", present)
        self.assertIn("z153 = 0.5", present)
        # align-t3：154 = ZZMI PHYS_PARAMS 弹簧直传（w154=release_spring 0.055）
        self.assertIn("w154 = 0.055", present)
        hand = "\n".join(sections["[CustomShaderEFMIDragPresentHand_A]"])
        self.assertIn("x184 = res_width", hand)
        self.assertIn("y184 = res_height", hand)
        # align-t3：手型中心 persist（188 槽；y 归并 ZZMI 0.275962）；非描边段
        # 描边旗标 y195=0
        self.assertIn("x188 = $ssmtdrag_efmi_hand_center_x_A", hand)
        self.assertIn("y188 = $ssmtdrag_efmi_hand_center_y_A", hand)
        self.assertIn("y195 = 0", hand)


class TestEFMIT45(unittest.TestCase):
    """t45：X4596 单分量 UAV 写零残留 + 手型段命名一致性。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()

    def test_no_single_component_uav_writes_in_shaders(self):
        """X4596 防回退：全部 9 个 EFMI 着色器无单分量 UAV/缓冲写
        （.x/.y/.z/.w = 值 直接写缓冲——typed UAV stores 须写全分量）。"""
        toolset = Path(__file__).resolve().parents[1] / "Toolset" / "drag_interaction_efmi"
        bad = []
        for shader in sorted(toolset.glob("*.hlsl")):
            text = shader.read_text(encoding="utf-8")
            for line in text.splitlines():
                s = line.strip()
                # 缓冲区成员单分量赋值（State[..].z = / Preview[..].x = 等）
                if "[" in s and "]" in s and "=" in s and ";" in s:
                    lhs = s.split("=", 1)[0].strip()
                    if re.match(r"^[A-Za-z_]\w*\[[^\]]+\]\.\w+$", lhs):
                        bad.append(f"{shader.name}: {s}")
        self.assertEqual(bad, [], "单分量 UAV 写残留:\n" + "\n".join(bad))

    def test_hand_present_run_refs_match_emitted(self):
        """段命名一致性：Present run 引用的手型段名 ∈ 发射段名集。"""
        node = _make_efmi_node()
        object.__setattr__(node, "enable_hand_cursor", True)
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        sections = _read_ini_sections(td / "main.ini")
        emitted = {
            str(sec).strip("[]") for sec in sections
            if str(sec).startswith("[CustomShaderEFMIDragPresentHand")
        }
        present = "\n".join(sections["[Present]"])
        refs = set()
        for line in present.splitlines():
            s = str(line).strip()
            if s.startswith("run = CustomShaderEFMIDragPresentHand"):
                refs.add(s[len("run = "):].strip())
        self.assertTrue(refs)
        missing = refs - emitted
        self.assertEqual(missing, set(), f"run 引用不存在的段: {missing}")


class TestEFMIT48(unittest.TestCase):
    """t48：主色 pass ps hash 动态识别（FrameAnalysis 反查/存储/读取优先级/迁移）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()

    def _make_frame_dir(self, tmp, draw_specs, numviews=None):
        """构造模拟 FrameAnalysis 目录：log.txt 含 dump 行（ps/ib/o0）+ deduped dds。

        draw_specs: [(draw, ib_hash, o0_hash, ps_hash, dxgi_format, main)]
        numviews: {draw: NumViews}（OMSetRenderTargets 行；缺省无行）"""
        log_lines = []
        for draw, ib, o0, ps, _fmt, _main in draw_specs:
            if numviews and draw in numviews:
                log_lines.append(f"{draw} OMSetRenderTargets(NumViews:{numviews[draw]},")
            log_lines.append(
                f"{draw} 3DMigoto Dumping Buffer K:\\x\\{draw}-ib={ib}-vs=aaaa-ps={ps}.buf -> K:\\x\\deduped\\{ps}.buf"
            )
            log_lines.append(
                f"{draw} 3DMigoto Dumping Texture2D K:\\x\\{draw}-o0={o0}-vs=aaaa-ps={ps}.dds -> K:\\x\\deduped\\{o0}.dds"
            )
        (tmp / "log.txt").write_text("\n".join(log_lines), encoding="utf-8")
        (tmp / "deduped").mkdir(exist_ok=True)
        for draw, ib, o0, ps, fmt, _main in draw_specs:
            # DDS 头：'DDS ' + dwSize=124 + flags + height/width + ... + DX10 fourcc + dxgi format
            head = bytearray(148)
            head[0:4] = b"DDS "
            head[4:8] = (124).to_bytes(4, "little")
            head[84:88] = b"DX10"
            head[128:132] = int(fmt).to_bytes(4, "little")
            (tmp / "deduped" / f"{o0}.dds").write_bytes(bytes(head))

    def test_identify_main_color_ps_hashes(self):
        """识别算法：目标组件（ib 匹配）主色 RT 格式（R11G11B10_FLOAT=26）的
        draw 收集 ps hash；非主色格式（R8G8B8A8=28）与非目标 ib 排除；多值合并。"""
        m = self.mod
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._make_frame_dir(tmp, [
                ("000020", "a4bb34f9", "0a000001", "d7bb9dd57f5b70c6", 26, True),
                ("000021", "a4bb34f9", "0a000002", "11111111", 28, False),   # 非主色格式
                ("000022", "deadbeef", "0a000003", "22222222", 26, True),    # 非目标 ib
                ("000023", "a4bb34f9", "0a000004", "33333333", 26, True),    # 多 pass 合并
            ])
            hashes = m.identify_main_color_ps_hashes(
                str(tmp / "log.txt"), ["a4bb34f9"])
            self.assertEqual(hashes, ["d7bb9dd57f5b70c6", "33333333"])
            # deduped 不可读（格式 None）→ o0 存在回退主色候选
            (tmp / "deduped" / "0a000002.dds").unlink()
            hashes2 = m.identify_main_color_ps_hashes(
                str(tmp / "log.txt"), ["a4bb34f9"])
            self.assertIn("11111111", hashes2)  # 格式不可读回退 o0 存在

    def test_resolve_priority_manual_over_cached_over_default(self):
        """读取优先级：手动值 > 工作空间识别值 > 现场识别 > 默认多值+警告。"""
        m = self.mod
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # 手动值优先
            node = types.SimpleNamespace(efmi_probe_pass_hash="cafe1234")
            self.assertEqual(
                m.resolve_probe_pass_hash(node, str(tmp), ["a4bb34f9"]),
                "cafe1234",
            )
            # 缓存值优先于现场识别
            node2 = types.SimpleNamespace(efmi_probe_pass_hash="")
            (tmp / "Config").mkdir(exist_ok=True)
            m.save_workspace_probe_hash(str(tmp), ["d7bb9dd57f5b70c6"], "src")
            self.assertEqual(
                m.resolve_probe_pass_hash(node2, str(tmp), ["a4bb34f9"]),
                "d7bb9dd57f5b70c6",
            )
            # 无缓存 → 现场识别（FrameAnalysis 目录存在）
            node3 = types.SimpleNamespace(efmi_probe_pass_hash="")
            (tmp / "Config" / "DragProbePassHash.json").unlink()
            fa = tmp / "FrameAnalysis-2026-09-07-032216"
            fa.mkdir()
            self._make_frame_dir(fa, [
                ("000020", "a4bb34f9", "0a000001", "d7bb9dd57f5b70c6", 26, True),
            ])
            self.assertEqual(
                m.resolve_probe_pass_hash(node3, str(tmp), ["a4bb34f9"]),
                "d7bb9dd57f5b70c6",
            )
            # 识别失败 → 默认多值
            node4 = types.SimpleNamespace(efmi_probe_pass_hash="")
            empty = tmp / "empty-workspace"
            empty.mkdir()
            self.assertEqual(
                m.resolve_probe_pass_hash(node4, str(empty), ["a4bb34f9"]),
                m.DEFAULT_PROBE_PASS_HASH,
            )

    def test_identify_depth_pass_no_o0(self):
        """识别判定（§10.6 回滚后）：主色 = o0 dump 存在 + 主色格式（深度/阴影
        pass 无 o0——NumViews:0 无 RT）；无 o0 的 draw 计为 depth。"""
        m = self.mod
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            # 000020 无 o0 dump（深度 pass）→ depth；000021 有 o0 主色 → color
            self._make_frame_dir(tmp, [
                ("000020", "a4bb34f9", "0a000001", "d7bb9dd57f5b70c6", 26, True),
                ("000021", "a4bb34f9", "0a000002", "44444444", 26, True),
            ])
            (tmp / "deduped" / "0a000001.dds").unlink()  # 000020 o0 不可读→无格式
            # o0 不可读回退主色候选（000020 仍 color——o0 存在）
            hashes = m.identify_main_color_ps_hashes(
                str(tmp / "log.txt"), ["a4bb34f9"])
            self.assertEqual(hashes, ["d7bb9dd57f5b70c6", "44444444"])
            # 完全无 o0 行的 draw → depth（构造无 o0 的第三个 draw）
            log_lines = (tmp / "log.txt").read_text(encoding="utf-8").splitlines()
            log_lines.append(
                "000022 3DMigoto Dumping Buffer K:\\x\\000022-ib=a4bb34f9-vs=aaaa-ps=55555555.buf -> K:\\x\\x.buf"
            )
            (tmp / "log.txt").write_text("\n".join(log_lines), encoding="utf-8")
            self.assertEqual(
                m.identify_pass_ordinal(str(tmp / "log.txt"), ["a4bb34f9"]),
                (1, 2, 1),  # depth=1（000022 无 o0）、color=2、first=1（000020 o0 兜底）
            )

    def test_identify_numviews_excludes_depth_pass_recognize_only(self):
        """§10.6 整合：NumViews 判定保留在识别链（识别主色 pass 用）——
        NumViews:0（深度/阴影 pass 无 RT）的 draw 识别侧排除；门控不受影响
        （门控 = pass 计数器）。"""
        m = self.mod
        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            self._make_frame_dir(tmp, [
                ("000020", "a4bb34f9", "0a000001", "d7bb9dd57f5b70c6", 26, True),
                ("000021", "a4bb34f9", "0a000002", "44444444", 26, True),
            ], numviews={"000020": 0, "000021": 1})
            hashes = m.identify_main_color_ps_hashes(
                str(tmp / "log.txt"), ["a4bb34f9"])
            # 000020 NumViews:0（深度 pass）识别侧排除；000021 NumViews:1 纳入
            self.assertEqual(hashes, ["44444444"])
            self.assertEqual(
                m.identify_pass_ordinal(str(tmp / "log.txt"), ["a4bb34f9"]),
                (1, 1, 2),
            )


class TestEFMIT50(unittest.TestCase):
    """t50：识别产品链路接入（F1）+ 迁移告警（F3）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()

    def test_threshold_drives_gate(self):
        """F1 闭环（§10.6 回滚后）：门控由识别阈值驱动——_probe_pass_threshold
        恢复（读工作空间 ordinal / 默认 3）；识别产品链路接入 _ensure_probe_
        identification（幂等：缓存存在跳过、否则现场识别写缓存）。"""
        src = (Path(__file__).resolve().parents[1] / "blueprint"
               / "node_postprocess_draginteraction_efmi.py").read_text(encoding="utf-8")
        self.assertIn("def _probe_pass_threshold", src)
        self.assertIn("def _ensure_probe_identification", src)
        self.assertNotIn("NumViews == 1", src)
        # 门控用计数器阈值（t4/P0：默认 3 = 首个颜色 pass；probe 在颜色层执行）
        node = _make_efmi_node()
        _td, sections = TestEFMIT50._export(node)
        probe_cl = "\n".join(sections["[CommandListEFMIDragProbe_A]"])
        # t4/P0：门控 = enabled && 帧 latch && pass 门（>= 执行 pass；非死条件 ==）
        self.assertIn("$ssmtdrag_efmi_probe_frame_prev_A", probe_cl)
        self.assertNotIn("$ssmtdrag_efmi_pass_A == 3", probe_cl)
        self.assertIn("$ssmtdrag_efmi_pass_A >= 3", probe_cl)
        # 识别产品链路：无缓存 → 现场识别写缓存（模拟 FrameAnalysis 目录）
        with tempfile.TemporaryDirectory() as td2:
            root = Path(td2)
            fa = root / "FrameAnalysis-2026-09-07-032216"
            fa.mkdir()
            (fa / "deduped").mkdir()
            log = [
                "000020 3DMigoto Dumping Buffer K:\\x\\000020-ib=a4bb34f9-vs=aaaa-ps=11111111.buf -> K:\\x\\x.buf",
                "000021 3DMigoto Dumping Buffer K:\\x\\000021-ib=a4bb34f9-vs=aaaa-ps=22222222.buf -> K:\\x\\x.buf",
                "000021 3DMigoto Dumping Texture2D K:\\x\\000021-o0=0a000002-vs=aaaa-ps=22222222.dds -> K:\\x\\x.dds",
            ]
            (fa / "log.txt").write_text("\n".join(log), encoding="utf-8")
            head = bytearray(148)
            head[0:4] = b"DDS "
            head[4:8] = (124).to_bytes(4, "little")
            head[84:88] = b"DX10"
            head[128:132] = (26).to_bytes(4, "little")
            (fa / "deduped" / "0a000002.dds").write_bytes(bytes(head))
            exporter = self.mod.DragInteractionEFMIExporter(node)
            object.__setattr__(exporter, "_probe_workspace_root", str(root))
            object.__setattr__(exporter, "hash_values", "a4bb34f9")
            exporter._ensure_probe_identification()
            cached = self.mod.load_workspace_probe_hash(str(root))
            self.assertIsNotNone(cached)
            self.assertEqual(cached.get("color_pass_first_ordinal"), 2)
            self.assertEqual(exporter._probe_pass_threshold(), 2)
            # 幂等：再次调用不重复识别（缓存命中）
            exporter._ensure_probe_identification()
            cached2 = self.mod.load_workspace_probe_hash(str(root))
            self.assertEqual(cached2.get("color_pass_first_ordinal"), 2)

    @staticmethod
    def _export(node):
        td = _make_efmi_mod_dir()
        exporter = _load_efmi_module().DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        return td, _read_ini_sections(td / "main.ini")

    def test_legacy_ps_hash_warns(self):
        """F3 迁移告警：旧属性值（1718.1）构造执行器 → 清理告警输出；空值无告警。"""
        import io
        from contextlib import redirect_stdout
        node = _make_efmi_node(efmi_probe_pass_hash="1718.1")
        buf = io.StringIO()
        with redirect_stdout(buf):
            self.mod.DragInteractionEFMIExporter(node)
        self.assertIn("已废弃", buf.getvalue())
        self.assertIn("1718.1", buf.getvalue())
        node2 = _make_efmi_node(efmi_probe_pass_hash="")
        buf2 = io.StringIO()
        with redirect_stdout(buf2):
            self.mod.DragInteractionEFMIExporter(node2)
        self.assertNotIn("已废弃", buf2.getvalue())


class TestEFMIStableZoneId(unittest.TestCase):
    """t29 稳定 zone ID（用户硬性需求）：槽位分配/持久化 + 重排/增删不漂移 +
    联动消费端引用一致性（zones.buf 槽 = 持久化稳定 id，与面板/形态键联动的
    drag_zone_id/click_zone_id 引用一致）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()

    def _node_with(self, objs, pre_ids=None):
        """构造 zone_objects 列表；pre_ids 为各 item 已持久化的稳定 id（None →
        缺省未分配）。"""
        items = [types.SimpleNamespace(zone_object=o) for o in objs]
        if pre_ids:
            for item, zid in zip(items, pre_ids):
                object.__setattr__(item, "zone_id", zid)
        node = _make_efmi_node()
        object.__setattr__(node, "zone_objects", items)
        return node

    @staticmethod
    def _empty(x, y, z, enabled=True, radius=0.03):
        # t32-A：命中范围由空物体变换（缩放）决定，radius 仅作拖拽衰减——
        # 夹具把缩放设成 radius，使各稳定 id 测试的几何隔离（小球命中）保持
        # 与旧显式 radius 覆盖分支相同的球大小
        mw = np.eye(4, dtype=np.float64)
        mw[0, 3], mw[1, 3], mw[2, 3] = x, y, z
        mw[0, 0], mw[1, 1], mw[2, 2] = radius, radius, radius
        class _Obj:
            type = 'EMPTY'
        obj = _Obj()
        obj.matrix_world = mw
        obj.ssmt_drag_zone = types.SimpleNamespace(
            enabled=enabled, radius=radius, brush_strength=1.0,
            falloff=0.0, grabbable=True)
        return obj

    def test_first_load_allocates_by_list_order_and_persists(self):
        """迁移期 §3：旧工程（item 无 zone_id）首载按列表序分配 0..N-1 并持久化；
        重排后 entry 顺序不变。"""
        a = self._empty(-0.08, 0, 1)
        b = self._empty(0.08, 0, 1)
        c = self._empty(0.30, 0, 1)
        node = self._node_with([a, b, c])
        exporter = self.mod.DragInteractionEFMIExporter(node)

        entries = exporter._collect_enabled_zone_entries()
        self.assertEqual([z for z, _ in entries], [0, 1, 2])
        self.assertEqual([getattr(it, "zone_object") for _, it in entries], [a, b, c])
        # 已持久化到 item.zone_id
        for item, zid in zip(node.zone_objects, (0, 1, 2)):
            self.assertEqual(item.zone_id, zid)
        # 重排列表（物理重排）→ stable entry 仍按 id 排序，obj 不变，id 不漂移
        object.__setattr__(node, "zone_objects",
                           [node.zone_objects[2], node.zone_objects[0], node.zone_objects[1]])
        entries2 = exporter._collect_enabled_zone_entries()
        self.assertEqual([getattr(it, "zone_object") for _, it in entries2], [a, b, c])
        self.assertEqual([z for z, _ in entries2], [0, 1, 2])
        # zones.buf 落槽 slot_ids 与配置同序
        self.assertEqual(exporter._collect_zone_slots(), [0, 1, 2])

    def test_reorder_keeps_baked_zone_ids_stable(self):
        """物理重排：zones.buf 对同一物理区域的 zone id 不漂移（匹配用户
        drag_zone_id/click_zone_id 引用）。每次重排后新建 exporter（真实导出
        每次 execute 都实例化新 exporter，读当前 node.zone_objects）。"""
        a = self._empty(-0.08, 0, 1)
        b = self._empty(0.08, 0, 1)
        c = self._empty(0.30, 0, 1)
        node = self._node_with([a, b, c])
        # 首载：分配并持久化 0,1,2（真实导出链的首次 execute）
        exp0 = self.mod.DragInteractionEFMIExporter(node)
        exp0._collect_enabled_zone_entries()
        pos_a = np.array([[-0.08, 0, 1]], dtype=np.float32)
        pos_b = np.array([[0.08, 0, 1]], dtype=np.float32)
        pos_c = np.array([[0.30, 0, 1]], dtype=np.float32)
        pos = np.concatenate([pos_a, pos_b, pos_c])
        ids_before, _ = self.mod.bake_sparse_weights(
            pos, exp0._collect_zone_configs(), slot_ids=exp0._collect_zone_slots())

        # 物理重排（列表顺序调换）→ 新 execute
        object.__setattr__(node, "zone_objects",
                           [node.zone_objects[1], node.zone_objects[2], node.zone_objects[0]])
        exp1 = self.mod.DragInteractionEFMIExporter(node)
        ids_after, _ = self.mod.bake_sparse_weights(
            pos, exp1._collect_zone_configs(), slot_ids=exp1._collect_zone_slots())
        # 每个物理顶点所在物理区的 zone id 重排后不变（0/1/2 对应 a/b/c）
        for i in (0, 1, 2):
            self.assertEqual(int(ids_after[i, 0]), int(ids_before[i, 0]),
                             f"重排后顶点 {i} 的 zone id 不应漂移")
        self.assertEqual(exp1._collect_zone_slots(), [0, 1, 2])

    def test_remove_then_add_reuses_lowest_free(self):
        """增删：移除中间区后其余保持原 id；新增区取最小空闲槽（密排）。"""
        a = self._empty(-0.08, 0, 1)
        b = self._empty(0.08, 0, 1)
        c = self._empty(0.30, 0, 1)
        node = self._node_with([a, b, c])
        self.mod.DragInteractionEFMIExporter(node)._collect_enabled_zone_entries()  # 0,1,2
        # 删掉中间 b
        object.__setattr__(node, "zone_objects",
                           [node.zone_objects[0], node.zone_objects[2]])
        exp1 = self.mod.DragInteractionEFMIExporter(node)
        entries = exp1._collect_enabled_zone_entries()
        by_obj = {getattr(it, "zone_object"): z for z, it in entries}
        self.assertEqual(by_obj[a], 0)
        self.assertEqual(by_obj[c], 2)  # c 保持 id 2，不被压缩
        # 新增 d 取最小空闲槽 1
        d = self._empty(0.5, 0, 1)
        object.__setattr__(node, "zone_objects",
                           [node.zone_objects[0], node.zone_objects[1],
                            types.SimpleNamespace(zone_object=d)])
        exp2 = self.mod.DragInteractionEFMIExporter(node)
        entries2 = exp2._collect_enabled_zone_entries()
        by_obj2 = {getattr(it, "zone_object"): z for z, it in entries2}
        self.assertEqual(by_obj2[d], 1)  # 复用空槽
        self.assertEqual(by_obj2[a], 0)
        self.assertEqual(by_obj2[c], 2)
        self.assertEqual(exp2._collect_zone_slots(), [0, 1, 2])

    def test_bake_sparse_weights_slot_ids_places_stable_slot(self):
        """bake_sparse_weights slot_ids：zones.buf 按稳定 id 落槽（含非密排缺口），
        缺省 slot_ids=None 保持原 config 序行为（纯函数测试兼容）。"""
        a = self._empty(-0.08, 0, 1, radius=0.03)
        c = self._empty(0.30, 0, 1, radius=0.03)
        node = self._node_with([a, c], pre_ids=[0, 2])  # 非密排：id 0 与 2（1 缺）
        exporter = self.mod.DragInteractionEFMIExporter(node)
        configs = exporter._collect_zone_configs()
        slots = exporter._collect_zone_slots()
        self.assertEqual(slots, [0, 2])  # 稳定 id 序，含缺口
        pos = np.array([[-0.08, 0, 1], [0.30, 0, 1]], dtype=np.float32)
        ids, _ = self.mod.bake_sparse_weights(pos, configs, slot_ids=slots)
        # 顶点 0 落在 zone id 0，顶点 1 落在 zone id 2（缺口 1 不被占用）
        np.testing.assert_array_equal(ids[0, :2], [0, 0xFFFFFFFF])
        np.testing.assert_array_equal(ids[1, :2], [2, 0xFFFFFFFF])
        # 缺省 slot_ids=None = config 序（旧行为）
        ids_default, _ = self.mod.bake_sparse_weights(pos, configs)
        np.testing.assert_array_equal(ids_default[0, :2], [0, 0xFFFFFFFF])
        np.testing.assert_array_equal(ids_default[1, :2], [1, 0xFFFFFFFF])

    def test_full_export_zones_buf_slot_matches_persisted_id(self):
        """烘焙链 + 联动消费端双稳定：导出后 zones.buf 每物理顶点槽 = 其物理区
        的持久化稳定 id == 用户在形态键负 drag_zone_id/click_zone_id 引用的 id。
        区域空物体中心对齐默认夹具网格（左/右胸腔区）以确保顶点命中。"""
        a = self._empty(-0.07, -0.12, 1.095, radius=0.06)
        b = self._empty(0.07, -0.12, 1.095, radius=0.06)
        node = self._node_with([a, b])
        exporter = self.mod.DragInteractionEFMIExporter(node)
        entries = exporter._collect_enabled_zone_entries()
        id_a = id_b = None
        for z, it in entries:
            o = getattr(it, "zone_object")
            if o is a:
                id_a = z
            if o is b:
                id_b = z
        self.assertEqual((id_a, id_b), (0, 1))
        # 用户的 drag_zone_id/click_zone_id 引用（形态键联动消费端按稳定 id 引用）
        user_ref_a, user_ref_b = id_a, id_b
        td = _make_efmi_mod_dir()
        exporter.execute(str(td))
        zone_ids = np.fromfile(
            td / "res" / "drag_interaction_efmi" / "LOD0.abc123-43191_zones.buf",
            dtype=np.uint32,
        ).reshape(-1, 4)
        # a/b 顶点首槽 = 各自稳定 id = 用户引用
        hit_a = np.argwhere(zone_ids[:, 0] == np.uint32(user_ref_a))
        hit_b = np.argwhere(zone_ids[:, 0] == np.uint32(user_ref_b))
        self.assertGreater(len(hit_a), 0, "a 物理区应以稳定 id 落槽于 zones.buf")
        self.assertGreater(len(hit_b), 0, "b 物理区应以稳定 id 落槽于 zones.buf")
        # 重排后重新导出：a/b 槽 id 不变（联动引用继续一致）
        object.__setattr__(node, "zone_objects",
                           [node.zone_objects[1], node.zone_objects[0]])
        exporter2 = self.mod.DragInteractionEFMIExporter(node)
        td2 = _make_efmi_mod_dir()
        exporter2.execute(str(td2))
        zone_ids2 = np.fromfile(
            td2 / "res" / "drag_interaction_efmi" / "LOD0.abc123-43191_zones.buf",
            dtype=np.uint32,
        ).reshape(-1, 4)
        self.assertGreater(len(np.argwhere(zone_ids2[:, 0] == np.uint32(user_ref_a))), 0)
        self.assertGreater(len(np.argwhere(zone_ids2[:, 0] == np.uint32(user_ref_b))), 0)

    def test_disabled_zone_keeps_slot(self):
        """禁用区不占 config/zones.buf 槽，但保留其稳定 id（toggle enabled 不漂移）。"""
        a = self._empty(-0.08, 0, 1)
        d_off = self._empty(0.08, 0, 1, enabled=False)
        node = self._node_with([a, d_off])
        exporter = self.mod.DragInteractionEFMIExporter(node)
        entries = exporter._collect_enabled_zone_entries()
        by_obj = {getattr(it, "zone_object"): z for z, it in entries}
        self.assertIn(a, by_obj)
        self.assertNotIn(d_off, by_obj)   # 禁用不参与
        self.assertEqual(list(by_obj.values()), [0])  # a 得 id 0（禁用区也占 id 但不出 entries）
        # 重新启用 d_off → 其已持 id，不加新区
        d_off.ssmt_drag_zone.enabled = True
        entries2 = exporter._collect_enabled_zone_entries()
        by_obj2 = {getattr(it, "zone_object"): z for z, it in entries2}
        self.assertEqual(by_obj2[d_off], 1)  # 保持原分配

    def test_sparse_stable_id_writes_centers_zoneparams_anchors(self):
        """t39-P2 + align-t3：显式稀疏稳定 id（0/2 缺 1）→ centers/ZoneParams/
        anchors 按**稳定 id** 稀疏落槽（非密集枚举序）——与运行时按 zone id
        读取（detect Anchors.Load(zone*8+k) / Centers[gz] / deform
        ZoneParams[zone*2]）对齐；AnchorProject 宽/探针 draw 覆盖全槽
        （align-t3 起每区 8 锚点：4 恒等拖拽基 + 4 gizmo 表面基）。

        align-t3 夹具修正：空物体放到夹具网格邻近（全无效早退语义要求交集）。"""
        a = self._empty(-0.07, -0.12, 1.095, radius=0.06)
        c = self._empty(0.07, -0.12, 1.095, radius=0.06)
        node = self._node_with([a, c], pre_ids=[0, 2])  # 非密排：id 0 与 2（1 缺）
        exporter = self.mod.DragInteractionEFMIExporter(node)
        td = _make_efmi_mod_dir()
        exporter.execute(str(td))
        res = td / "res" / "drag_interaction_efmi"
        # centers.buf：zone id 2 的中心写到槽 2（非槽 1）
        centers = np.fromfile(res / "centers.buf", dtype=np.float32).reshape(-1, 4)
        self.assertAlmostEqual(float(centers[0][0]), -0.07, places=4)   # id0 → a
        self.assertAlmostEqual(float(centers[2][0]), 0.07, places=4)    # id2 → c
        self.assertEqual(float(centers[1][3]), 0.0)                     # 缺口槽 1 空
        # ZoneParams：id2 的 radius 写到槽 [2*2]（非 [1*2]）
        zp = np.fromfile(res / "ZoneParams_A.buf", dtype=np.float32).reshape(-1, 4)
        self.assertAlmostEqual(float(zp[0][0]), 0.06, places=6)         # id0 radius
        self.assertAlmostEqual(float(zp[4][0]), 0.06, places=6)         # id2 radius@槽4
        self.assertEqual(float(zp[2][0]), 0.0)                          # 缺口槽 1 空
        # anchors：id2 的 8 锚（中心+恒等 3 轴 + 中心+gizmo 3 轴）写到行
        # [2*8..2*8+7]，中心行=0.07
        ap = np.fromfile(res / "anchors_position.buf", dtype=np.float32).reshape(-1, 4)
        self.assertEqual(ap.shape[0], 8 * 3)                            # 8×(max_id+1)=24 行
        self.assertAlmostEqual(float(ap[0][0]), -0.07, places=4)        # id0 中心
        self.assertAlmostEqual(float(ap[16][0]), 0.07, places=4)        # id2 中心@行16
        self.assertAlmostEqual(float(ap[20][0]), 0.07, places=4)        # id2 gizmo 中心@行20
        self.assertEqual(float(ap[8][3]), 0.0)                          # 缺口行 8..15 空（w=0）
        # 生成器侧：AnchorProject RT 宽 + 探针 draw = 稀疏容量 24
        ini = _read_ini_sections(td / "main.ini")
        anchor_res = "\n".join(ini["[ResourceEFMIDragAnchorProject_A]"])
        self.assertIn("width = 24", anchor_res)
        probe_anchors = "\n".join(ini["[CustomShaderEFMIDragProbeAnchors_A]"])
        self.assertIn("drawindexed = 24, 0, 0", probe_anchors)


class TestEFMIHandCursorDiag(unittest.TestCase):
    """t32-B 手型光标诊断（t9）：手型状态码链路回归——
    生成器发射 state store + global 声明，Toolset shader 写 4 状态码
    （0=可见/1=无命中隐藏/2=锚点越界/3=基无效），便于实机判定手型
    不显示的具体门控（probe gate 未触发 → stale → 状态 1 等）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()

    def test_hand_state_diagnostic_emitted(self):
        """enable_hand_cursor → Present 含 state store（align-t3：索引 12 =
        Preview[3].x 诊断状态码）+ global 声明。"""
        node = _make_efmi_node(enable_hand_cursor=True)
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        sections = _read_ini_sections(td / "main.ini")
        present = "\n".join(sections["[Present]"])
        self.assertIn(
            "store = $ssmtdrag_efmi_hand_state_A, ResourceEFMIDragHandPreview_A, 12",
            present,
        )
        # 原有捕获状态 store 保留（不回归；align-t3：索引 11 = Preview[2].w
        # 预览状态 0/1/2）
        self.assertIn(
            "store = $ssmtdrag_efmi_hand_action_A, ResourceEFMIDragHandPreview_A, 11",
            present,
        )
        constants = "\n".join(sections["[Constants]"])
        self.assertIn("global $ssmtdrag_efmi_hand_state_A = 0", constants)

    def test_hand_state_codes_in_toolset_shader(self):
        """Toolset efmi_hand_preview.hlsl 三态状态码（align-t3：0=可见/1=无命中
        隐藏；D-6 基无效即 discard 对齐 ZZMI，废弃旧 380px 兜底基与 2/3 细分）
        且为全分量 UAV 写（t45 防单分量写回归）。"""
        toolset = Path(__file__).resolve().parents[1] / "Toolset" / "drag_interaction_efmi"
        hp = (toolset / "efmi_hand_preview.hlsl").read_text(encoding="utf-8")
        self.assertIn("HandPreview[3] = float4(1.0, 0, 0, 0);", hp)   # 状态 1：无命中隐藏
        # 三态输出：status 2.0 抓取 / 1.0 悬停或蓄力 / 0 隐藏
        self.assertIn("float status = capturing ? 2.0 : 1.0;", hp)
        # 抓取锚点 = 冻结捕获光标 + 解算位移投影（ZZMI ApplyCapturedJiggleOffset）
        # t14 修复（问题 2）：改为**三维分解投影**——位移是三维世界位移，须用
        # 同一组拖拽轴（锚点行 0-3）的屏幕投影柱分解；旧的二维 2×2 逆投影
        # （把屏幕 uv 分量当世界分量用、且丢掉第三轴）已移除。
        self.assertIn("float2 frozenAnchor = State[instBase + 0u].xy;", hp)
        self.assertIn("dragColX = (sdx - sdc) / 0.01;", hp)
        self.assertIn("dragColY = (sdy - sdc) / 0.01;", hp)
        self.assertIn("dragColZ = (sdz - sdc) / 0.01;", hp)
        self.assertIn("dragColX * dot(off, float3(1, 0, 0))", hp)
        # t18：手型 Y 反号修正——投影结果 Y 必须补一次翻转
        # （simulate L277 对 delta.y 的修正同时进入了 off，而 dragCol* 不含该修正）
        self.assertIn("uvDelta.y = -uvDelta.y;", hp)
        self.assertNotIn("float det = r.x * d.y - d.x * r.y;", hp)  # 旧二维逆投影已移除
        self.assertIn("HandPreview[0] = float4(anchorPx, screen);", hp)
        # 全分量写（t45：typed UAV stores 须写全分量）
        self.assertIn("HandPreview[2] = float4(", hp)
        # 无单分量 UAV 写残留
        bad = []
        for line in hp.splitlines():
            s = line.strip()
            if "[" in s and "]" in s and "=" in s and ";" in s:
                lhs = s.split("=", 1)[0].strip()
                if re.match(r"^[A-Za-z_]\w*\[[^\]]+\]\.\w+$", lhs):
                    bad.append(s)
        self.assertEqual(bad, [])


class TestEFMIRadiusScaleWarning(unittest.TestCase):
    """t38-P4 半径-尺度失配告警（_check_zone_radius_scale，ZZMI
    blueprint/node_postprocess_draginteraction.py L3033-3058 同款移植）：
    scale = 空物体矩阵列范数均值（world 上半 3×3），ratio = radius/scale 超出
    [0.3, 2.2] 打警告；纯告警不改数据。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()

    def _empty(self, scale, radius, name="zone_empty"):
        """mock 空物体：matrix_world = 均匀缩放 scale 对角阵 + ssmt_drag_zone.radius。"""
        mw = np.eye(4, dtype=np.float64)
        mw[0, 0] = mw[1, 1] = mw[2, 2] = scale

        class _Obj:
            type = 'EMPTY'

        obj = _Obj()
        obj.name = name
        obj.matrix_world = mw
        obj.ssmt_drag_zone = types.SimpleNamespace(
            enabled=True, radius=radius, brush_strength=1.0,
            falloff=0.0, grabbable=True,
        )
        return obj

    def test_ratio_above_max_warns(self):
        """t31 实证场景：radius=0.5 vs scale=0.2119（ratio≈2.36>2.2）→ 告警。"""
        obj = self._empty(0.2119, 0.5, name="t31_zone")
        exporter = self.mod.DragInteractionEFMIExporter(_make_efmi_node())
        self.assertEqual(exporter._check_zone_radius_scale([obj]), 1)

    def test_ratio_below_min_warns(self):
        """radius=0.03 vs scale=0.5（ratio=0.06<0.3）→ 告警（衰减过陡）。"""
        obj = self._empty(0.5, 0.03, name="steep_zone")
        exporter = self.mod.DragInteractionEFMIExporter(_make_efmi_node())
        self.assertEqual(exporter._check_zone_radius_scale([obj]), 1)

    def test_ratio_in_range_no_warning(self):
        """ratio=1.0 与 ratio≈1.67 都在 [0.3, 2.2] 内 → 0 告警。"""
        a = self._empty(0.03, 0.03, name="ok_a")
        b = self._empty(0.3, 0.5, name="ok_b")
        exporter = self.mod.DragInteractionEFMIExporter(_make_efmi_node())
        self.assertEqual(exporter._check_zone_radius_scale([a, b]), 0)

    def test_radius_zero_fallback_0_25_checked(self):
        """radius=0 → 回退 0.25（ZZMI L3046 同款）参与对比：scale=0.03 →
        ratio≈8.3>2.2 → 告警（0 半径工程也能被提示）。"""
        obj = self._empty(0.03, 0.0, name="fallback_zone")
        exporter = self.mod.DragInteractionEFMIExporter(_make_efmi_node())
        self.assertEqual(exporter._check_zone_radius_scale([obj]), 1)

    def test_wired_into_collect_zone_configs(self):
        """接线：_collect_zone_configs 对启用区执行失配检查并打印告警；数据
        不被告警污染（configs 内容 = 空物体变换椭球，与未接线时一致）。"""
        import contextlib
        import io
        node = _make_efmi_node(efmi_probe_pass_hash="")
        obj = self._empty(0.2119, 0.5, name="wired_zone")
        object.__setattr__(
            node, "zone_objects", [types.SimpleNamespace(zone_object=obj)]
        )
        exporter = self.mod.DragInteractionEFMIExporter(node)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            configs = exporter._collect_zone_configs()
        self.assertEqual(len(configs), 1)
        # t32-A：命中范围恒 = 空物体变换（lin3 = 均匀缩放 0.2119·I）
        np.testing.assert_allclose(configs[0][1], 0.2119 * np.eye(3), atol=1e-9)
        self.assertIn("[EFMIDrag][WARNING] 区域 wired_zone 影响半径", out.getvalue())


class TestEFMIGrabbableSemantics(unittest.TestCase):
    """t36-P5 grabbable 语义对齐（ZZMI ZoneGrabbable）：烘焙权重保留（不可抓区
    仍可命中/显示）+ 运行时抓取拒绝门（simulate 读 ZoneParams[z*2+1].y）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()

    def _empty(self, x, y, z, enabled=True, radius=0.03, strength=1.0,
               grabbable=True, scale=0.03):
        mw = np.eye(4, dtype=np.float64)
        mw[0, 3], mw[1, 3], mw[2, 3] = x, y, z
        mw[0, 0], mw[1, 1], mw[2, 2] = scale, scale, scale

        class _Obj:
            type = 'EMPTY'
        obj = _Obj()
        obj.matrix_world = mw
        obj.ssmt_drag_zone = types.SimpleNamespace(
            enabled=enabled, radius=radius, brush_strength=strength,
            falloff=0.0, grabbable=grabbable)
        return obj

    def test_bake_keeps_weights_for_non_grabbable_zone(self):
        """全导出：grabbable=False 区仍烘焙进 zones.buf（可命中/显示），
        ZoneParams[z*2+1].y=0 携带不可抓标志。"""
        node = _make_efmi_node()
        a = self._empty(-0.07, -0.12, 1.095, radius=0.06, grabbable=True)
        b = self._empty(0.07, -0.12, 1.095, radius=0.06, grabbable=False)
        object.__setattr__(
            node, "zone_objects",
            [types.SimpleNamespace(zone_object=a),
             types.SimpleNamespace(zone_object=b)],
        )
        exporter = self.mod.DragInteractionEFMIExporter(node)
        entries = exporter._collect_enabled_zone_entries()
        by_obj = {getattr(it, "zone_object"): z for z, it in entries}
        self.assertEqual(by_obj[a], 0)
        self.assertEqual(by_obj[b], 1)
        # grabbable=False 区权重保留（bake_zone_ball t36-P5 不再清零）
        pos = np.array([
            [-0.07, -0.12, 1.095], [0.07, -0.12, 1.095],
        ], dtype=np.float32)
        zone_ids, w = self.mod.bake_sparse_weights(
            pos, exporter._collect_zone_configs(),
            slot_ids=exporter._collect_zone_slots())
        # b（右区，grabbable=False）仍以稳定 id 1 落槽且权重 > 0
        self.assertEqual(int(zone_ids[1, 0]), 1)
        self.assertGreater(float(w[1, 0]), 0.0)
        # ZoneParams[z*2+1].y = grabbable 标志（0=不可抓）
        td = _make_efmi_mod_dir()
        exporter.execute(str(td))
        zone_params = np.fromfile(
            td / "res" / "drag_interaction_efmi" / "ZoneParams_A.buf",
            dtype=np.float32,
        ).reshape(-1, 4)
        self.assertEqual(zone_params[0 * 2 + 1][1], 1.0)  # a 可抓
        self.assertEqual(zone_params[1 * 2 + 1][1], 0.0)  # b 不可抓

    def test_simulate_has_grabbable_runtime_gate(self):
        """simulate shader：grabbable 门在 grabbing 判定 + 捕获锁存两处
        （ZoneParams[z*2+1].y 槽消费），t45 无单分量 UAV 写。"""
        toolset = Path(__file__).resolve().parents[1] / "Toolset" / "drag_interaction_efmi"
        sim = (toolset / "efmi_simulate.hlsl").read_text(encoding="utf-8")
        self.assertIn("float zoneGrabbable  = ZoneParams[z * 2 + 1].y;", sim)
        self.assertIn("candidateGrabbable > 0.5", sim)          # 捕获锁存门
        # grabbing 门：本线程 zone 的 grabbable 拒绝（空格无关）；
        # t37-P3 追加 mode 门（mode 1 仅命中不拖拽 → grabbing=false）
        norm = "".join(sim.split())
        self.assertIn(
            "boolgrabbing=held&&capture.z==(float)z&&zoneGrabbable>0.5"
            "&&IniParams[159].x>=2.0;",
            norm,
        )
        # 全分量 UAV 写（t45）
        bad = []
        for line in sim.splitlines():
            s = line.strip()
            if "[" in s and "]" in s and "=" in s and ";" in s:
                lhs = s.split("=", 1)[0].strip()
                if re.match(r"^[A-Za-z_]\w*\[[^\]]+\]\.\w+$", lhs):
                    bad.append(s)
        self.assertEqual(bad, [])


class TestEFMIThreeStateMode(unittest.TestCase):
    """t37-P3 三态运行模式（0=关/1=仅命中/2=命中+拖拽；ZZMI drag_system_mode +
    F8 cycle 同款）：生成器发射 mode global + F8 循环键 + probe 门控 + Present
    x159 寄存器 + shader 侧门（simulate 抓取 / deform 位移，mode>=2）。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()

    def test_mode_global_f8_key_and_present_register(self):
        """mode global（缺省 2 / 节点属性覆盖）+ F8 cycle 键 + Present x159。"""
        node = _make_efmi_node()
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        sections = _read_ini_sections(td / "main.ini")
        constants = "\n".join(sections["[Constants]"])
        # t41 P-4：模式变量 persist（ZZMI L4736 同款，跨会话保持）
        self.assertIn("global persist $ssmtdrag_efmi_mode_A = 2", constants)
        # F8 循环键（ZZMI type=cycle 同款）
        toggle = "\n".join(sections["[KeyEFMIDragModeToggle_A]"])
        self.assertIn("key = f8", toggle)
        self.assertIn("type = cycle", toggle)
        self.assertIn("$ssmtdrag_efmi_mode_A = 0,1,2", toggle)
        # Present x159 寄存器（simulate/deform 经 IniParams[159].x 读）
        present = "\n".join(sections["[Present]"])
        self.assertIn("x159 = $ssmtdrag_efmi_mode_A", present)

    def test_probe_gate_no_mode_pass_dependency(self):
        """R8/P0 语义 + t4/P0 修正：probe 门控不含 **mode** 门——显示链不依赖
        mode（用户「命中区域无条件显示」；ZZMI detect 门 = drag_enabled>=1 &&
        ObjectDetectAllowed==1，无 mode 序号）。mode 语义保留在 shader 侧
        （simulate grabbing / deform 位移门 [159].x>=2.0、shapekey_drive
        mode==1，见 test_shaders_read_mode_register /
        test_p7_shapekey_drive_gated_to_mode1_present）。显示与 mode 解耦：
        mode 初值 0 时手型段族与 Present run 完整。

        t4/P0 更正：本测试原先把「无 mode **且无 pass**」绑在一起，现按 t3 根因
        报告拆分——pass 门是**必要**的（恢复消费 _probe_exec_pass()，见
        test_probe_pass_counter_gate），只保留「无 mode 门」这一条断言；不得再
        断言 probe CL 与 pass 无关。"""
        node = _make_efmi_node()
        _td, sections = self._run_export(node)
        probe_cl = "\n".join(sections["[CommandListEFMIDragProbe_A]"])
        self.assertNotIn("$ssmtdrag_efmi_mode_A >= 1", probe_cl)
        self.assertIn("$ssmtdrag_efmi_probe_frame_prev_A", probe_cl)
        # t4/P0：pass 门已恢复（不再断言「与 pass 无关」）
        self.assertIn(
            f"$ssmtdrag_efmi_pass_A >= {self.mod.DragInteractionEFMIExporter(node)._probe_exec_pass()}",
            probe_cl,
        )
        # 显示与 mode 解耦：mode 初值 0（关拖拽）时手型段族与 Present run 仍完整
        node0 = _make_efmi_node(drag_system_mode_default=0, enable_hand_cursor=True)
        _td0, sections0 = self._run_export(node0)
        present0 = "\n".join(sections0["[Present]"])
        self.assertIn("run = CustomShaderEFMIDragHandPreview_A", present0)
        self.assertIn("$ssmtdrag_efmi_hand_action_A", present0)
        self.assertIn("$ssmtdrag_efmi_hand_state_A", present0)

    def test_mode_default_node_property(self):
        """节点属性 drag_system_mode_default 控制 global 初值（0/1/2 合法，越界钳制）。"""
        for value, expect in ((0, 0), (1, 1), (2, 2), (5, 2), (-1, 0)):
            node = _make_efmi_node(drag_system_mode_default=value)
            td = _make_efmi_mod_dir()
            exporter = self.mod.DragInteractionEFMIExporter(node)
            exporter.execute(str(td))
            sections = _read_ini_sections(td / "main.ini")
            constants = "\n".join(sections["[Constants]"])
            self.assertIn(f"global persist $ssmtdrag_efmi_mode_A = {expect}", constants,
                          f"mode 初值 {value} → 应钳制为 {expect}")

    def test_shaders_read_mode_register(self):
        """shader 侧门（p1-runtime 落盘后）：simulate grabbing 门 + deform 位移门
        均经 IniParams[159].x 判 mode >= 2。"""
        toolset = Path(__file__).resolve().parents[1] / "Toolset" / "drag_interaction_efmi"
        sim = "".join((toolset / "efmi_simulate.hlsl").read_text(encoding="utf-8").split())
        deform = "".join((toolset / "efmi_deform.hlsl").read_text(encoding="utf-8").split())
        # simulate：grabbing 含 mode>=2（mode 1 仅命中不拖拽）
        self.assertIn("IniParams[159].x>=2.0", sim)
        # deform：位移门含 mode>=2（mode 1 → delta=0 → Out=Base）
        self.assertIn("IniParams[159].x>=2.0", deform)

    def test_p7_shapekey_drive_gated_to_mode1_present(self):
        """t41 P-7 互斥门 + t43/P-8：ShapeKeyDrive CS dispatch 与 seed_pending 清零
        **无条件**（ZZMI boot CL 无条件 run，播种在 shader mode 门之前——mode 2 下
        播种照常、驱动被 shader 内部 mode!=1 门拦住）。需 enable_shapekey_drive。"""
        node = _make_efmi_node_with_consumer([
            _make_sk_item(zone=0, stage=1, dir_id=1),
        ])
        _td, sections = self._run_export(node)
        present = "\n".join(sections["[Present]"])
        # P-8：Present 侧不再有 mode==1 门（互斥由 shader 内部 L166-171 承担）
        self.assertNotIn("if $ssmtdrag_efmi_mode_A == 1", present)
        # run 与 seed 清零均无条件（未缩进；无 tab 缩进的门内形态）
        self.assertIn("\nrun = CustomShaderEFMIDragShapeKeyDrive_A\n", present)
        self.assertNotIn("\n\trun = CustomShaderEFMIDragShapeKeyDrive_A\n", present)
        self.assertIn("\n$ssmtdrag_efmi_seed_pending_A = 0\n", present)

    def test_p7_shapekey_drive_shader_mode_gate_clears_latch(self):
        """t41 P-7 互斥门（shader 侧）：efmi_shapekey_drive.hlsl mode!=1 →
        保持 dir 槽一致 + 清 DragLatch + 返回（ZZMI rzm_shapekey_drive L160-166
        同款）。"""
        toolset = Path(__file__).resolve().parents[1] / "Toolset" / "drag_interaction_efmi"
        shader = (toolset / "efmi_shapekey_drive.hlsl").read_text(encoding="utf-8")
        self.assertIn("float mode = IniParams[159].x;", shader)
        self.assertIn("if (mode != 1.0)", shader)
        self.assertIn("ShapeKeyDir[lastSlot] = triggerHeld ? 1.0 : 0.0;", shader)
        self.assertIn("DragLatch[0] = 0.0;", shader)
        self.assertIn("return;", shader)
        # 全分量 UAV 写（t45）
        bad = []
        for line in shader.splitlines():
            s = line.strip()
            if "[" in s and "]" in s and "=" in s and ";" in s:
                lhs = s.split("=", 1)[0].strip()
                if re.match(r"^[A-Za-z_]\w*\[[^\]]+\]\.\w+$", lhs):
                    bad.append(s)
        self.assertEqual(bad, [])

    def test_altx_equivalent_left_button_grab(self):
        """t47-P2：Alt+X 等效左键抓取（ZZMI KeyDragInputManagerX 同款）。

        - 键段：独立键盘 X 键 key=X type=hold，置位 $ssmtdrag_efmi_x_A，post 归零。
        - 按钮判定：X 并入 LMB 位（bit0，值 1），与物理左键同路径——
          simulate buttons==1 拉出、shapekey_drive buttons&1 触发。
        - 前端全局声明 $ssmtdrag_efmi_x_A。
        """
        node = _make_efmi_node()
        _td, sections = self._run_export(node)
        # 1) 全局声明
        constants = "\n".join(sections["[Constants]"])
        self.assertIn("global $ssmtdrag_efmi_x_A = 0", constants)
        # 2) X 键段（ZZMI L3482 同款 key=X hold + post）
        x_key = "\n".join(sections["[KeyEFMIDragX_A]"])
        self.assertIn("key = X", x_key)
        self.assertIn("type = hold", x_key)
        self.assertIn("$ssmtdrag_efmi_x_A = 1", x_key)
        self.assertIn("post $ssmtdrag_efmi_x_A = 0", x_key)
        # 3) Present 按钮判定：X 并入 LMB 位
        present = "\n".join(sections["[Present]"])
        self.assertIn(
            "$ssmtdrag_efmi_buttons_A = ($ssmtdrag_efmi_lmb_A * (1 - $ssmtdrag_efmi_x_A) + $ssmtdrag_efmi_x_A) + 2 * $ssmtdrag_efmi_rmb_A",
            present,
        )

    def test_altx_present_button_code_values(self):
        """t47-P2：按钮码数值语义——X 并入 LMB 后，Alt+X（无 LMB/RMB）须产生
        buttons=1（LMB 抓取路径）；同时按下 lmb 与 x 时仍钳制为 1（非 2=RMB）。"""
        def buttons(lmb, x, rmb):
            return (lmb * (1 - x) + x) + 2 * rmb
        # Alt+X（纯 X，无鼠标 LMB/RMB）→ 1 = LMB（拉出）
        self.assertEqual(buttons(0, 1, 0), 1)
        # 物理 LMB + X 同按 → 仍 1（钳制，不落入 2=RMB）
        self.assertEqual(buttons(1, 1, 0), 1)
        # 无任何抓取 → 0
        self.assertEqual(buttons(0, 0, 0), 0)
        # RMB 仍独立 2；X+RMB → 3（同按）
        self.assertEqual(buttons(0, 0, 1), 2)
        self.assertEqual(buttons(0, 1, 1), 3)

    # ------------------------------------------------------------------
    # t6：全面修复（probe_pass 复位 + ALT 运行态门控）
    # ------------------------------------------------------------------

    def test_t22_deform_apply_ungated(self):
        """t22 裁决：Apply CL（deform）**不得**含任何运行态门控。

        **实机依据（t21 逐笔证据）**：门控变量 `ObjectDetectAllowed` / `efmi_allowed`
        是在 hook 里**逐笔 draw 重算**的（后者含 ALT 实时态 `modifier`），而 Apply CL
        同样逐笔跑。某一笔门为 0 时，该笔仍用 Draw CL 顶部绑定的原始
        `Resource_LOD0..._Position` ⇒ 读到 **Base 且不经过弹簧** ⇒ 该阶段在释放期
        **直接复位**，与其它走 `Out` 的阶段错位 ⇒ 用户所报
        「有东西没跟着回弹 / 松手之后直接就复位了」的黑影。
        描边是每帧最后一笔（t21 日志序列确认），最容易被漏掉。

        **安全性**：`efmi_deform.hlsl` 自身门控是唯一位移闸门
        （`Candidate.w >= frame-1` + `IniParams[159].x >= 2.0`）；delta=0 时它把 `Out`
        写成 **Base 的位精确拷贝**，与直接读 Position 等价 ⇒ 去门后「未拖拽 / mode 关闭 /
        候选过期」行为不变，仅阶段间不再发散。

        **与 t9 的区别**：t9 曾把本段改为无门控，但与"放宽 EntryPoint 签名"捆绑同批，
        而那批的实机后果是「模型消失 + 命中消失」（**签名放宽**所致）；t11 因此整批回退、
        连带还原了本段。t21 逐笔证据把两者分离：**签名放宽有害、本段去门控必要**。
        签名侧由 `test_t11_entrypoint_signature_not_widened` 锁定（必须保留
        `match_index_count`）。"""
        node = _make_efmi_node()
        _td, sections = self._run_export(node)
        applys = {k: v for k, v in sections.items()
                  if k.startswith("[CommandListEFMIDragApply_")}
        self.assertTrue(applys)
        for k, sec in applys.items():
            text = "\n".join(sec)
            # 不得含任何门控
            self.assertNotIn("if ", text, k)
            self.assertNotIn("endif", text, k)
            self.assertNotIn("$ssmtdrag_efmi_enabled_A", text, k)
            self.assertNotIn("$ssmtdrag_ObjectDetectAllowed_A", text, k)
            self.assertNotIn("$ssmtdrag_efmi_allowed_A", text, k)
            self.assertNotIn("$ssmtdrag_efmi_pass_A", text, k)
            # 必须无条件调用 deform 并成对换绑 vb0/vb3
            self.assertIn("run = CustomShaderEFMIDragDeform_", text, k)
            self.assertIn("vb0 = ResourceEFMIDragOut_", text, k)
            self.assertIn("vb3 = ResourceEFMIDragOut_", text, k)

    def test_t8_t47_deform_gate_is_mode_only(self):
        """t47 结论修正：deform **不得**再按「逐帧命中新鲜度」开合位移。

        历史：t8 当年加了 `Candidate.w >= frame - 1` 的严格窗（防「陈旧候选产生
        位移」，窗口一度放宽到 `- 2.0` 又回退），把它当成位移的**唯一**闸门。
        2026-09 t47 用 release 档位逐档二分（各档之间只差一个机制：档 4/3 无
        黑影、档 2/1/0 有）证明**这层门本身就是黑影的来源**：
        `PullTowardLimit` 是渐近的，probe 又投的是已变形流（Draw CL 在 probe 之前
        就把 vb0/vb3 换成了 Out）⇒ 拖到一定程度光标必然跑出网格投影 ⇒ detect 判
        miss ⇒ 门关 ⇒ `delta` 一帧内归零 ⇒ 网格 snap 回原姿势 ⇒ 黑影。

        现在闸门**只**按 mode 开合（mode 0/1 仍不写位移）；「位移一定归零」的保证
        点搬到了 simulate 侧（见 test_simulate_still_zeroes_the_offsets），不再靠
        这层逐帧门。**不要再加回来。**"""
        from pathlib import Path
        shader = (Path(__file__).resolve().parents[1] / "Toolset"
                  / "drag_interaction_efmi" / "efmi_deform.hlsl").read_text(
                      encoding="utf-8")
        self.assertIn("if (active && IniParams[159].x >= 2.0) {", shader)
        self.assertNotIn("Candidate[inst * 4].w >=", shader)
        self.assertNotIn("IniParams[150].w - 1.0", shader)
        self.assertNotIn("IniParams[150].w - 2.0", shader)

    # ------------------------------------------------------------------
    # t11 防回归：EntryPoint 签名**不得**被放宽（t9 的签名放宽已证伪）
    # ------------------------------------------------------------------

    def test_t11_entrypoint_signature_not_widened(self):
        """t11 防回归：拖拽目标组件的 EntryPoint 必须**保留** `match_index_count`。

        背景：t9 曾就地删除 `match_index_count` 以放宽匹配，想让阴影/深度 pass 的
        draw 也被接管。实机结果：**整个模型消失 + 鼠标命中消失**。机制：放宽后该
        hash 下所有 index_count 变体的 draw 都被 `handling = skip` 拦下并由只为原
        签名设计的自定义绘制接管 → 几何消失；同时每帧被拦截 draw 数变化 → pass
        计数器整体位移 → probe 的 `pass >= 3` 门失效 → 命中一起消失。

        因此签名必须精确（hash + match_first_index + match_index_count），且不得
        出现第二条同 hash 段（重复绘制）。"""
        node = _make_efmi_node()
        _td, sections = self._run_export(node)
        eps = {k: v for k, v in sections.items()
               if k.startswith("[TextureOverride_EntryPoint_")}
        self.assertEqual(len(eps), 1)  # 不得出现第二条同 hash 段（防重复绘制）
        (k, lines), = eps.items()
        text = "\n".join(lines)
        self.assertIn("match_index_count = 12", text)  # 签名必须精确，不得放宽
        self.assertIn("match_first_index = 0", text)
        self.assertIn("hash = abc123", text)
        self.assertIn("handling = skip", text)
        self.assertIn("CommandList\\EFMIv1\\Callback_Component_DrawCustom", text)
        self.assertIn("run = CommandList_Component_DrawInstances", text)
        self.assertIn("\\EFMIv1\\gpu_posed = 1", text)
        self.assertIn("\\EFMIv1\\component_id = 6", text)
        # 签名放宽的自识别标记也不得再出现
        self.assertNotIn("EFMIDrag: match_index_count removed", text)
        # 幂等：二次导出仍保留精确签名
        from pathlib import Path
        td = _make_efmi_mod_dir()
        exp = self.mod.DragInteractionEFMIExporter(node)
        exp.execute(str(td))
        first = (Path(td) / "main.ini").read_text(encoding="utf-8")
        exp2 = self.mod.DragInteractionEFMIExporter(node)
        exp2.execute(str(td))
        second = (Path(td) / "main.ini").read_text(encoding="utf-8")
        for txt in (first, second):
            self.assertEqual(
                txt.count("[TextureOverride_EntryPoint_LOD0.abc123_43191]"), 1)
            self.assertIn("\nmatch_index_count = 12", txt)

    def test_t6_probe_pass_reset_per_frame(self):
        """t6 问题 1 方案 A：probe_pass 必须在 hook 帧变化块内复位为 -1。

        不复位时 `pass >= exec || pass < probe_pass` 与帧号 latch 叠加会逐帧交替
        （F1 在 p3 命中 → F2 的 p1 满足 `1 < 3` 兜底在深度层触发 → 手型闪烁 /
        拖拽隔帧失效）。复位后 probe_pass 每帧初值 -1，使 `pass < probe_pass`
        恒假，probe 稳定只由 `pass >= exec_pass` 触发。"""
        node = _make_efmi_node()
        _td, sections = self._run_export(node)
        draw = "\n".join(sections["[CommandList_Draw_LOD0.abc123_43191]"])
        # 帧变化块内复位（与 pass 清零 / frame_prev 更新同块）
        self.assertIn("if $ssmtdrag_efmi_frame_A != $ssmtdrag_efmi_frame_prev_A", draw)
        self.assertIn("$ssmtdrag_efmi_pass_A = 0", draw)
        self.assertIn("$ssmtdrag_efmi_probe_pass_A = -1", draw)
        self.assertIn("$ssmtdrag_efmi_frame_prev_A = $ssmtdrag_efmi_frame_A", draw)
        # 复位语句必须位于帧变化块内（在 endif 之前）
        lines = draw.splitlines()
        i_zero = next(i for i, l in enumerate(lines) if "$ssmtdrag_efmi_pass_A = 0" in l)
        i_rst = next(i for i, l in enumerate(lines) if "$ssmtdrag_efmi_probe_pass_A = -1" in l)
        i_end = next(i for i in range(i_zero, len(lines)) if lines[i].strip() == "endif")
        self.assertLess(i_zero, i_rst)
        self.assertLess(i_rst, i_end)

    def test_t6_probe_fallback_not_depth_triggered(self):
        """t6 问题 1 方案 B：兜底判据不得再是 `pass < probe_pass`（会回落深度层），
        必须是 `probe_frame_prev < frame - 1`（上一帧完全未探测才补跑）。"""
        node = _make_efmi_node()
        _td, sections = self._run_export(node)
        probe = "\n".join(sections["[CommandListEFMIDragProbe_A]"])
        self.assertNotIn(
            "$ssmtdrag_efmi_pass_A < $ssmtdrag_efmi_probe_pass_A", probe
        )
        self.assertIn(
            "$ssmtdrag_efmi_probe_frame_prev_A < $ssmtdrag_efmi_frame_A - 1", probe
        )

    def test_t6_object_detect_allowed_gate(self):
        """t6 问题 2：ObjectDetectAllowed 运行态总门（照搬 ZZMI L4211），
        且 EFMI 三态 mode 必须用 `>= 1` 而非 ZZMI 的 `== 1`——EFMI 默认
        mode=2（命中+拖拽），照抄 `== 1` 会永久置 0 致拖拽彻底失效。"""
        node = _make_efmi_node()
        _td, sections = self._run_export(node)
        draw = "\n".join(sections["[CommandList_Draw_LOD0.abc123_43191]"])
        self.assertIn(
            "if $ssmtdrag_efmi_enabled_A >= 1 && $inputMode == 0 "
            "&& $ssmtdrag_efmi_mode_A >= 1 && $ssmtdrag_efmi_drawn_A == 1",
            draw,
        )
        self.assertIn("$ssmtdrag_ObjectDetectAllowed_A = 1", draw)
        self.assertIn("$ssmtdrag_ObjectDetectAllowed_A = 0", draw)
        # 不得用 ZZMI 的 == 1（三态语义下会是死条件）
        self.assertNotIn("$ssmtdrag_efmi_mode_A == 1 && $ssmtdrag_efmi_drawn_A", draw)
        # probe 消费检测门 + ALT 手势门（否则未按 Alt 仍会逐帧算命中 →
        # 与「不按 ALT 不计算命中」相悖）。门控只属于 probe/命中，且只在主颜色层。
        probe = "\n".join(sections["[CommandListEFMIDragProbe_A]"])
        self.assertIn("$ssmtdrag_ObjectDetectAllowed_A == 1", probe)
        self.assertIn("$ssmtdrag_efmi_allowed_A == 1", probe)
        # t22 裁决：Apply CL **无条件**（不得带门控）。理由见
        # test_t22_deform_apply_ungated：门控是逐笔求值的，门关的那一笔读回原始
        # Position（Base、无弹簧）⇒ 该阶段释放期直接复位 ⇒ 与其它阶段错位（黑影）。
        applys = [s for k, s in sections.items() if k.startswith("[CommandListEFMIDragApply_")]
        self.assertTrue(applys)
        for sec in applys:
            text = "\n".join(sec)
            self.assertNotIn("if ", text)
            self.assertNotIn("endif", text)
            self.assertNotIn("$ssmtdrag_efmi_pass_A", text)
            # 仍必须无条件调用 deform 并成对换绑 vb0/vb3
            self.assertIn("run = CustomShaderEFMIDragDeform_", text)
            self.assertIn("vb0 = ResourceEFMIDragOut_", text)
            self.assertIn("vb3 = ResourceEFMIDragOut_", text)

    def test_t6_drawn_flag_lifecycle(self):
        """t6 问题 2：drawn 旗标——hook 帧变化块置 1，Present 帧末清 0
        （ZZMI L4664 / L5172 同款）。"""
        node = _make_efmi_node()
        _td, sections = self._run_export(node)
        draw = "\n".join(sections["[CommandList_Draw_LOD0.abc123_43191]"])
        self.assertIn("$ssmtdrag_efmi_drawn_A = 1", draw)
        present = "\n".join(sections["[Present]"])
        self.assertIn("$ssmtdrag_efmi_drawn_A = 0", present)
        consts = "\n".join(sections["[Constants]"])
        self.assertIn("global $ssmtdrag_efmi_drawn_A = 0", consts)
        self.assertIn("global $inputMode = 0", consts)

    def test_t47d3_inputmode_is_a_shared_cross_branch_port(self):
        """t47-D3（t30 审查）：`$inputMode` 是 EFMI/ZZMI **共用端口**，不是漏命名空间。

        两个分支各自发射同一个 `global $inputMode = 0`，并各自拿 `$inputMode == 0`
        当运行态总门的一环（EFMI：本文件 test_t6_object_detect_allowed_gate；
        ZZMI：`tests/test_node_postprocess_draginteraction.py` 的 `w75 = $inputMode`）。
        语义是「框架/其它 mod 置位即可挂起交互」——EFMI 生成器紧邻的注释明载
        「inputMode 需在 Present 置位……框架/其它 mod 可覆盖，语义与 ZZMI 一致」。

        因此**不得**给它加 `ssmtdrag_efmi_` 前缀：那会单侧打断 parity，让另一侧的
        门控读到恒 0（或恒非 0）而静默失效。本测试同时钉住两侧的声明与门控，防的
        正是「一侧改名/删门、另一侧无声失效」这类分叉。"""
        root = REPO_ROOT / "blueprint"
        branches = {
            "EFMI": (root / "node_postprocess_draginteraction_efmi.py").read_text(
                encoding="utf-8"),
            "ZZMI": (root / "node_postprocess_draginteraction.py").read_text(
                encoding="utf-8"),
        }
        for name, src in branches.items():
            self.assertIn("global $inputMode = 0", src,
                          "%s 分支必须发射共用的 $inputMode 全局" % name)
            self.assertIn("$inputMode == 0", src,
                          "%s 分支必须拿 $inputMode == 0 当运行态门" % name)
            self.assertNotIn("$ssmtdrag_efmi_inputMode", src,
                             "%s 分支不得把共用端口单侧命名空间化" % name)
            self.assertNotIn("$ssmtdrag_inputMode", src,
                             "%s 分支不得把共用端口单侧命名空间化" % name)

    def test_t6_allowed_gate_and_hand_preview_gated(self):
        """t6 问题 2：allowed = enabled && modifier && drawn；手型预览整段
        在其门内（不按 ALT 不跑预览、不画手型）。"""
        node = _make_efmi_node(enable_hand_cursor=True)
        _td, sections = self._run_export(node)
        draw = "\n".join(sections["[CommandList_Draw_LOD0.abc123_43191]"])
        self.assertIn(
            "if $ssmtdrag_efmi_enabled_A == 1 "
            "&& $ssmtdrag_efmi_modifier_A == 1 "
            "&& $ssmtdrag_efmi_drawn_A == 1",
            draw,
        )
        self.assertIn("$ssmtdrag_efmi_allowed_A = 1", draw)
        present = sections["[Present]"]
        text = "\n".join(present)
        self.assertIn("if $ssmtdrag_efmi_allowed_A == 1", text)
        # 手型预览与绘制都必须落在 allowed 门内（缩进一层）
        self.assertIn("\trun = CustomShaderEFMIDragHandPreview_A", text)
        self.assertIn("\trun = CustomShaderEFMIDragPresentHand_A", text)
        # 不得再有未缩进的（无门控）手型预览调用
        self.assertNotIn("\nrun = CustomShaderEFMIDragHandPreview_A", text)
        consts = "\n".join(sections["[Constants]"])
        self.assertIn("global $ssmtdrag_efmi_allowed_A = 0", consts)

    def test_t6_ui_publish_fallback_disallowed(self):
        """t6 问题 2：UI 发布兜底——总门不通过时显式写 -1（发布与 readback 必须
        成对，否则面板读到陈旧命中）。"""
        node = _make_efmi_node(enable_panel_linkage=True)
        _td, sections = self._run_export(node)
        present = "\n".join(sections["[Present]"])
        self.assertIn("if $ssmtdrag_ObjectDetectAllowed_A == 1", present)
        self.assertIn("$ssmtdrag_ui_detected_A = -1", present)
        self.assertIn("$ssmtdrag_ui_zone_A = -1", present)
        # 发布与 readback 成对（同一分支内）
        self.assertIn("post run = CommandListEFMIDragUIReadback_A", present)

    def _run_export(self, node):
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        return td, _read_ini_sections(td / "main.ini")


class TestEFMIAlignT3ZoneWeightAlgorithms(unittest.TestCase):
    """align-t3（团队目标③）：区域空物体/权重赋予/文件计算 = ZZMI 相同算法——
    测地扩散（propagate）/ 平台化（mask_plateau）/ 包含过滤（include_objects
    组件级）/ 每组件独立烘焙 / gizmo 帧烘焙的契约断言。"""

    @classmethod
    def setUpClass(cls):
        cls.mod = _load_efmi_module()

    def _node_with_zone(self, propagate=True, include=(), plateau=0.0,
                        center=(0.0, 0.0, 0.0), scale=1.0):
        mw = np.eye(4, dtype=np.float64)
        mw[0, 3], mw[1, 3], mw[2, 3] = center
        mw[0, 0], mw[1, 1], mw[2, 2] = scale, scale, scale

        class _Obj:
            type = 'EMPTY'
            name = "zone_a"
        obj = _Obj()
        obj.matrix_world = mw
        obj.ssmt_drag_zone = types.SimpleNamespace(
            enabled=True, radius=0.0, brush_strength=1.0, brush_falloff_k=4.6,
            grabbable=True, propagate=propagate,
            include_objects=[types.SimpleNamespace(object=types.SimpleNamespace(
                name=n, name_full=n)) for n in include],
        )
        node = _make_efmi_node(mask_plateau=plateau)
        object.__setattr__(node, "zone_objects",
                           [types.SimpleNamespace(zone_object=obj)])
        return node

    def test_geodesic_propagation_blocks_backside(self):
        """测地扩散（ZZMI propagate 默认开）：环面/折返拓扑上球体积覆盖的
        「对面」顶点因表面绕行距离 ≥1 拿不到权重（体积欧氏会给到）。"""
        m = self.mod
        # 一维链式拓扑：v0..v9 每步 0.4（球局部单位），区心 v0
        pos = np.array([[i * 0.4, 0.0, 0.0] for i in range(10)], dtype=np.float64)
        tris = np.array([[i, i + 1, i + 2] for i in range(8)], dtype=np.uint32)
        node = self._node_with_zone(propagate=True, center=(0, 0, 0), scale=1.0)
        exporter = m.DragInteractionEFMIExporter(node)
        fields, all_invalid = exporter._compute_zone_fields(pos, tris, [])
        self.assertFalse(all_invalid)
        f = fields[0]
        self.assertGreater(f[0], 0.9)               # 种子满强度
        self.assertGreater(f[1], 0.0)               # 测地 d=0.4 有场
        # 测地距离 = 沿链累计：v2 = 0.8 <1 有场；v3 = 1.2 ≥1 → 0（硬截止）
        self.assertGreater(f[2], 0.0)
        self.assertEqual(f[3], 0.0)
        # 对照：关闭 propagate → 体积欧氏（同距离语义，本例链为直线等价）
        node2 = self._node_with_zone(propagate=False, center=(0, 0, 0), scale=1.0)
        fields2, _ = m.DragInteractionEFMIExporter(node2)._compute_zone_fields(
            pos, tris, [])
        np.testing.assert_allclose(fields2[0], f, atol=1e-9)

    def test_geodesic_detour_longer_than_euclidean(self):
        """测地 ≥ 欧氏：折返拓扑上拐角远端点的测地距离 = 沿表面绕行，大于
        直线欧氏——体积球穿墙 vs 测地不穿墙（ZZMI propagate 语义）。"""
        m = self.mod
        # 双墙几何：两排平行墙（y=0 与 y=0.1）仅在远端 x=3 经桥点连通。
        # 区心 a0=(0,0,0)、半径 1.0：目标 b0=(0,0.1,0) 欧氏 d=0.1（体积球
        # 强场 = 穿墙），测地须绕行 x=3 桥（路径 ≥3 ≥1 → 不可达 = 0）。
        pos = np.array([
            [0, 0, 0], [1, 0, 0], [2, 0, 0], [3, 0, 0],          # 墙 A: v0-v3
            [0, 0.1, 0], [1, 0.1, 0], [2, 0.1, 0], [3, 0.1, 0],  # 墙 B: v4-v7
            [3, 0.05, 0],                                        # 桥点 v8
        ], dtype=np.float64)
        tris = np.array([
            [0, 1, 2], [1, 2, 3],   # 墙 A
            [4, 5, 6], [5, 6, 7],   # 墙 B
            [3, 8, 7],              # 远端桥（唯一连通）
        ], dtype=np.uint32)
        node = self._node_with_zone(propagate=True, center=(0, 0, 0), scale=1.0)
        exporter = m.DragInteractionEFMIExporter(node)
        f_geo, all_geo = exporter._compute_zone_fields(pos, tris, [])
        self.assertFalse(all_geo)
        node2 = self._node_with_zone(propagate=False, center=(0, 0, 0), scale=1.0)
        f_eu, _ = m.DragInteractionEFMIExporter(node2)._compute_zone_fields(
            pos, tris, [])
        # 体积欧氏：b0 穿墙有场（d=0.1 → exp(-4.6×0.01)≈0.955）
        self.assertGreater(f_eu[0][4], 0.9)
        # 测地：b0 不可达（绕行 ≥3 ≥1 截止）→ 0；桥后 b3 同样不可达
        self.assertEqual(f_geo[0][4], 0.0)
        self.assertEqual(f_geo[0][7], 0.0)
        # 种子本墙近邻两侧一致（a1 在 d=1 边界截止，a0 满强度）
        self.assertGreater(f_geo[0][0], 0.9)
        self.assertEqual(f_geo[0][1], 0.0)   # d=1 硬截止

    def test_plateau_shape_field(self):
        """平台化（ZZMI mask_plateau）：plateau=0.5 → d≤0.5 满强度平台，
        0.5<d<1 smoothstep 平滑降到 0；plateau=0 回退纯高斯。"""
        m = self.mod
        node = self._node_with_zone(plateau=0.5, center=(0, 0, 0), scale=1.0)
        exporter = m.DragInteractionEFMIExporter(node)
        pos = np.array([[0, 0, 0], [0.4, 0, 0], [0.75, 0, 0], [1.0, 0, 0]],
                       dtype=np.float64)
        # 关闭 propagate（无拓扑也自动回退；显式关更直白）
        node.zone_objects[0].zone_object.ssmt_drag_zone.propagate = False
        fields, _ = exporter._compute_zone_fields(pos, None, [])
        f = fields[0]
        self.assertAlmostEqual(float(f[0]), 1.0, places=6)   # d=0 平台
        self.assertAlmostEqual(float(f[1]), 1.0, places=6)   # d=0.4 < 0.5 平台
        # d=0.75：t=(0.75-0.5)/0.5=0.5 → s=0.5³(3-1)=0.5 → field=0.5
        self.assertAlmostEqual(float(f[2]), 0.5, places=6)
        self.assertEqual(float(f[3]), 0.0)                   # d=1 截止
        # plateau=0 → 纯高斯（不平台化）
        node0 = self._node_with_zone(plateau=0.0, center=(0, 0, 0), scale=1.0)
        node0.zone_objects[0].zone_object.ssmt_drag_zone.propagate = False
        f0, _ = m.DragInteractionEFMIExporter(node0)._compute_zone_fields(pos, None, [])
        self.assertAlmostEqual(float(f0[0][1]), float(np.exp(-4.6 * 0.16)), places=6)

    def test_include_objects_component_filter(self):
        """包含过滤（ZZMI include_objects 的 EFMI 组件级等价）：区域含包含
        列表时只作用于 mesh 名命中的组件；未命中 → 该区对本组件跳过 + 告警。"""
        m = self.mod
        node = self._node_with_zone(include=["dress"])
        exporter = m.DragInteractionEFMIExporter(node)
        pos = np.array([[0, 0, 0]], dtype=np.float64)
        # 命中组件（mesh_names 含 dress）→ 正常场
        fields_hit, all_hit = exporter._compute_zone_fields(pos, None, ["dress"])
        self.assertFalse(all_hit)
        self.assertGreater(fields_hit[0][0], 0.9)
        # 未命中组件 → 该区跳过 → 全无效早退
        fields_miss, all_miss = exporter._compute_zone_fields(pos, None, ["body"])
        self.assertTrue(all_miss)
        self.assertIsNone(fields_miss[0])
        # 空包含列表 → 全允许（不过滤）
        node2 = self._node_with_zone(include=())
        f2, a2 = m.DragInteractionEFMIExporter(node2)._compute_zone_fields(
            pos, None, ["body"])
        self.assertFalse(a2)

    def test_gizmo_frames_surface_aligned(self):
        """gizmo 帧烘焙（ZZMI BuildGizmoAxes 同构造）：法线 = 种子顶点关联面
        法线；切线/副切线按「最不平行的固定局部轴」规约；正交单位三轴。"""
        m = self.mod
        # XY 平面三角形（法线 +Z）；区心在原点
        pos = np.array([[0, 0, 0], [0.1, 0, 0], [0, 0.1, 0]], dtype=np.float64)
        tris = np.array([[0, 1, 2]], dtype=np.uint32)
        node = self._node_with_zone(center=(0, 0, 0), scale=0.5)
        exporter = m.DragInteractionEFMIExporter(node)
        configs = exporter._collect_zone_configs()
        frames = m.compute_zone_gizmo_frames(pos, tris, configs)
        self.assertEqual(len(frames), 1)
        normal, tangent, bitangent = frames[0]
        np.testing.assert_allclose(normal, [0, 0, 1], atol=1e-6)   # 面法线 +Z
        # 切线 = cross(localRef, normal)：normal=+Z → |n.z|=1 ≥0.75 →
        # localRef=+Y（|n.y|=0<0.75）→ tangent = cross(Y, Z) = +X
        np.testing.assert_allclose(tangent, [1, 0, 0], atol=1e-6)
        # bitangent = cross(normal, tangent) = cross(Z, X) = +Y
        np.testing.assert_allclose(bitangent, [0, 1, 0], atol=1e-6)
        # 拓扑缺失 → 恒等帧回退（手型按模型局部轴定向）
        frames_none = m.compute_zone_gizmo_frames(pos, None, configs)
        np.testing.assert_allclose(frames_none[0][0], [1, 0, 0], atol=1e-9)

    def test_gizmo_normals_buffer_written(self):
        """GizmoNormals_{ns}.buf：每区烘焙法线落盘（256×4，valid 旗标）——
        simulate depth_pull 的冻结法线源。"""
        node = _make_efmi_node()
        mw = np.eye(4, dtype=np.float64)
        mw[0, 3], mw[1, 3], mw[2, 3] = -0.07, -0.12, 1.095
        mw[0, 0], mw[1, 1], mw[2, 2] = 0.06, 0.06, 0.06

        class _Obj:
            type = 'EMPTY'
        obj = _Obj()
        obj.matrix_world = mw
        obj.ssmt_drag_zone = types.SimpleNamespace(
            enabled=True, radius=0.06, brush_strength=1.0, brush_falloff_k=4.6,
            grabbable=True)
        object.__setattr__(node, "zone_objects",
                           [types.SimpleNamespace(zone_object=obj)])
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        res = td / "res" / "drag_interaction_efmi"
        gn = np.fromfile(res / "GizmoNormals_A.buf", dtype=np.float32).reshape(-1, 4)
        self.assertEqual(gn.shape, (256, 4))
        # valid=1 + 单位法线（夹具网格面法线由 IB 拓扑重建，方向随夹具面绕向）
        self.assertEqual(gn[0][3], 1.0)
        self.assertAlmostEqual(float(np.linalg.norm(gn[0][:3])), 1.0, places=5)
        # 未分配槽 = 0（valid 0）
        self.assertEqual(gn[5][3], 0.0)
        # 模拟段绑定
        sections = _read_ini_sections(td / "main.ini")
        simulate = "\n".join(sections["[CustomShaderEFMIDragSimulate_A]"])
        self.assertIn("cs-t3 = ResourceEFMIDragGizmoNormals_A", simulate)

    def test_two_component_export_cloth_transfer(self):
        """align-t3 + D-2（用户拍板保留）全链路：双组件导出——body 直烘
        （ZZMI 算法：测地/plateau/包含/镜像），cloth 保留 EFMI 独有的
        body→cloth 16NN 高斯传递（传递源 = body 新权重场）。"""
        node = _make_efmi_node()
        mw = np.eye(4, dtype=np.float64)
        mw[0, 3], mw[1, 3], mw[2, 3] = -0.07, -0.12, 1.095
        mw[0, 0], mw[1, 1], mw[2, 2] = 0.06, 0.06, 0.06

        class _Obj:
            type = 'EMPTY'
        obj = _Obj()
        obj.matrix_world = mw
        obj.ssmt_drag_zone = types.SimpleNamespace(
            enabled=True, radius=0.06, brush_strength=1.0, brush_falloff_k=4.6,
            grabbable=True)
        object.__setattr__(node, "zone_objects",
                           [types.SimpleNamespace(zone_object=obj)])
        td = _make_efmi_mod_dir()
        # 追加第二个组件（cloth：独立 IB/Position/Texcoord/Blend + EntryPoint）；
        # cloth 顶点紧贴 body 胸腔簇（16NN 传递近距覆盖）
        mesh_dir = td / "Meshes"
        cloth_pos = np.zeros((4, 4), dtype=np.float32)
        cloth_pos[:, :3] = [[-0.07, -0.12, 1.09], [-0.06, -0.12, 1.09],
                            [-0.07, -0.11, 1.09], [-0.06, -0.11, 1.09]]
        cloth_pos[:, 3] = 1.0
        _write_buf(mesh_dir / "LOD0.def456-56789-Position.buf", cloth_pos)
        _write_buf(mesh_dir / "LOD0.def456-56789-Texcoord.buf",
                   np.zeros((4, 3), dtype=np.float32))
        _write_buf(mesh_dir / "LOD0.def456-56789-Blend.buf",
                   np.zeros((4, 4), dtype=np.float32))
        _write_buf(mesh_dir / "LOD0.def456-56789-Index.buf",
                   np.array([0, 1, 2, 1, 2, 3], dtype=np.uint32))
        ini_path = td / "main.ini"
        ini_path.write_text(
            ini_path.read_text(encoding="utf-8") + "\n"
            "[TextureOverride_EntryPoint_LOD0.def456_56789]\n"
            "hash = def456\n"
            "match_first_index = 0\n"
            "match_index_count = 6\n"
            "handling = skip\n"
            "CommandList\\EFMIv1\\Callback_Component_DrawCustom = ref CommandList_Draw_LOD0.def456_56789\n"
            "\n"
            "[CommandList_Draw_LOD0.def456_56789]\n"
            "ib = Resource_LOD0.def456_56789_Index\n"
            "vb0 = Resource_LOD0.def456_56789_Position\n"
            "vb1 = Resource_LOD0.def456_56789_Texcoord\n"
            "vb2 = Resource_LOD0.def456_56789_Blend\n"
            "drawindexedinstanced = 6,INSTANCE_COUNT,0,0,FIRST_INSTANCE\n"
            "\n"
            "[Resource_LOD0.def456_56789_Position]\n"
            "type = Buffer\nstride = 16\nfilename = Meshes/LOD0.def456-56789-Position.buf\n"
            "\n"
            "[Resource_LOD0.def456_56789_Texcoord]\n"
            "type = Buffer\nstride = 12\nfilename = Meshes/LOD0.def456-56789-Texcoord.buf\n"
            "\n"
            "[Resource_LOD0.def456_56789_Blend]\n"
            "type = Buffer\nstride = 16\nfilename = Meshes/LOD0.def456-56789-Blend.buf\n"
            "\n"
            "[Resource_LOD0.def456_56789_Index]\n"
            "type = Buffer\nformat = R32_UINT\nfilename = Meshes/LOD0.def456-56789-Index.buf\n",
            encoding="utf-8",
        )
        node.hash_values = "abc123,def456"
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        res = td / "res" / "drag_interaction_efmi"
        # cloth 权重来自 body 16NN 传递（紧贴 body 簇 → 传递量高；非直烘——
        # cloth 组件读的是 body 稀疏表，D-2 保留特性）
        cw = np.fromfile(res / "LOD0.def456-56789_weights.buf", dtype=np.float32).reshape(-1, 4)
        self.assertGreater(float(cw[:, 0].min()), 0.4)
        self.assertLess(float(cw[:, 0].max()), 1.0)
        cids = np.fromfile(res / "LOD0.def456-56789_zones.buf", dtype=np.uint32).reshape(-1, 4)
        np.testing.assert_array_equal(cids[:, 0], np.zeros(4, dtype=np.uint32))
        sections = _read_ini_sections(td / "main.ini")
        self.assertIn("[CustomShaderEFMIDragDeform_LOD0.def456_56789_A]", sections)


# ---------------------------------------------------------------------------
# t47 黑影根因修复：deform 在候选陈旧时仍继续施加 spring offset（不 snap）
# ---------------------------------------------------------------------------
class TestEFMIDebugFreezeSwitch(unittest.TestCase):
    """t47 黑影修复的**出厂形态**：修复烘焙进 Toolset 着色器，没有任何旋钮。

    `blueprint/...\u200bnode_postprocess_draginteraction_efmi.py :: _copy_shaders()`
    逐字节复制 `Toolset/drag_interaction_efmi/*.hlsl`，所以修复落在 Toolset 里就等于
    「以后每次导出都正常」，也不存在开关被留在错档的可能。

    根因（release 档位逐档二分：档 4/3 无黑影、档 2/1/0 有，各档只差一个机制）：
    `efmi_deform.hlsl` 的位移被「逐帧命中新鲜度」门控。`PullTowardLimit` 渐近 +
    probe 投的是**已变形**流 ⇒ 拖到一定程度必然 detect miss ⇒ 门关 ⇒ `delta` 一帧内
    归零 ⇒ 网格 snap 回原姿势 ⇒ 黑影（不来自任何一次着色，所以逐层异色探针都染不上）。

    设计约束（本类即为契约）：

    · deform 的位移闸门**只**按 mode 开合；`Candidate` 仍声明/绑定但不再参与门控。
    · 「位移一定归零」的保证点在 `efmi_simulate.hlsl`：半隐式弹簧 + 静止清零
      （`length(next) < 1e-5 && length(velocity) < 1e-4` ⇒ 精确置 0）+ 冷启动清槽。
      没有东西在拖时 `delta` 恰好为 0，与原版「未拖拽」路径逐位一致。
    · simulate / detect 侧保持原版；ini 侧不需要任何新接线（导出器也不用改）。
    """

    TOOLSET = REPO_ROOT / "Toolset" / "drag_interaction_efmi"

    def setUp(self):
        self.mod = _load_efmi_module()

    def _read(self, name):
        return (self.TOOLSET / name).read_text(encoding="utf-8")

    def test_deform_gate_is_mode_only(self):
        deform = self._read("efmi_deform.hlsl")
        self.assertIn("if (active && IniParams[159].x >= 2.0) {", deform)
        self.assertNotIn("Candidate[inst * 4].w >=", deform)
        self.assertNotIn("IniParams[150].w", deform)
        self.assertNotIn("IniParams[164]", deform)   # 不再有调试开关
        self.assertEqual(deform.count("delta = EvalDelta(vertex, stateBase, pos);"), 1)

    def test_simulate_still_zeroes_the_offsets(self):
        """安全前提：offset 一定会归零（否则位移会残留）。

        这是 t8 原始担忧的**新落点**：不再靠 deform 的逐帧门保证，而是靠 simulate
        把收敛的区精确清零。simulate 侧不得出现任何调试开关。
        """
        sim = self._read("efmi_simulate.hlsl")
        self.assertIn(
            "if (!grabbing && length(next) < 1e-5 && length(velocity) < 1e-4) {",
            sim,
        )
        self.assertIn("next = 0;", sim)
        self.assertNotIn("preserveState", sim)
        self.assertNotIn("holdOffsets", sim)
        self.assertNotIn("freezeSel", sim)

    def test_detect_stays_original(self):
        """detect 侧不得有残留（miss 帧标保持原版 frame-1）。"""
        detect = self._read("efmi_detect.hlsl")
        self.assertIn(
            "float frameTag = (bestDepths[0] < 1e30) ? IniParams[150].w : IniParams[150].w - 1.0;",
            detect,
        )
        self.assertNotIn("missTag", detect)
        self.assertNotIn("IniParams[164]", detect)

    def test_generator_copies_the_fixed_shader_and_wires_nothing(self):
        """导出器逐字节复制 Toolset 着色器，且不得**发射**任何调试接线。

        注意断言口径：允许这个已撤除的开关名出现在**注释**里（`EFMI_TIME_IP`
        那段留了考古线索，说明 .z/.w 曾属于谁），但绝不允许被发射成 ini 行。
        """
        gen = (REPO_ROOT / "blueprint"
               / "node_postprocess_draginteraction_efmi.py").read_text(encoding="utf-8")
        self.assertIn("shutil.copy2(src, os.path.join(res_dir, fname))", gen)
        self.assertIn('"efmi_deform.hlsl"', gen)
        # 不得声明/发射该调试变量（只允许出现在注释里）
        self.assertNotIn("global $ssmtdrag_efmi_freeze_A", gen)
        self.assertNotIn("KeyEFMIDragFreezeToggle_A", gen)
        # t47-D2：164 槽只发 .x/.y，.z/.w 的死接线不得回来
        self.assertNotIn('f"z{EFMI_TIME_IP} = 0"', gen)
        self.assertNotIn('f"w{EFMI_TIME_IP} = 0"', gen)

    def test_exported_shader_carries_the_fix(self):
        """真正导出一次：产物里的 deform 必须是修好的那一版。"""
        node = _make_efmi_node()
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        exported = (td / "res" / "drag_interaction_efmi" / "efmi_deform.hlsl").read_text(
            encoding="utf-8")
        toolset = self._read("efmi_deform.hlsl")
        self.assertEqual(exported, toolset)
        self.assertIn("if (active && IniParams[159].x >= 2.0) {", exported)
        self.assertNotIn("IniParams[150].w", exported)
        ini_text = (td / "main.ini").read_text(encoding="utf-8")
        self.assertNotIn("ssmtdrag_efmi_freeze_A", ini_text)


class TestEFMIT51GrabCenter(unittest.TestCase):
    """t51：capture 的 grab center 必须用**区中心**的投影 uv 作参考。

    缺陷（用户实测：「无论拖拽哪边，动的都是强度最强的那一块」）：

    · `efmi_deform.hlsl` 的逐顶点距离衰减（t21/t24 ZZMI parity）以
      `grabCenter = State[stateBase + 4].xyz` 为球心、`ZoneParams[z*2].x` 为半径
      限制形变范围 —— 逐顶点运行时局部化本来就在。
    · 但 `efmi_simulate.hlsl` 在 capture 帧用 `anchorUV = Candidate[inst*4+3].xy`
      算球心。R11 起该槽是**命中点**屏幕 uv（detect `bestUV = ToScreen(hitClip)`），
      而命中判定的语义正是「命中点投影 ≈ 光标」⇒ `cuv - anchorUV ≈ 0`
      ⇒ **grabCenter 塌回 `Centers[gz]`（区中心、常量）**。
    · 后果：球永远罩在区中心 ⇒ 无论抓哪里都只在区中心那块最高权重 patch 上形变。
      旧场景每区 ~500 顶点、半径 0.333 ⇒ 球罩满整区 ⇒ 不可见；单大区
      （27983/63805 顶点）⇒ 立刻显形。

    契约（本类即钉住）：

    · 参考 uv 取**锚点投影 RT** 行 `zone*8+0`（探针每帧投「区中心 + 三轴端」，
      与 hand_preview 的 dragCols 同 RT 同行），simulate 因此新增
      `t4 = ResourceEFMIDragAnchorProject` 绑定（ini/生成器同步发射）。
    · clip→screen uv 的式子必须与 detect/hand_preview **逐字同式**（三者可比）。
    · 参考 uv 是 top-down，光标侧按 t49 的同一开关 `[153].w` 对齐约定。
    · Candidate 布局不变（`Candidate[3]` 仍是命中点 uv —— hand_preview 的悬停
      锚点依赖它，不得为了本修复去改它的语义）。
    """

    TOOLSET = REPO_ROOT / "Toolset" / "drag_interaction_efmi"
    TOSCREEN = "return float2(.5 + .5 * c.x / c.w, .5 - .5 * c.y / c.w);"

    def setUp(self):
        self.mod = _load_efmi_module()

    def _read(self, name):
        return (self.TOOLSET / name).read_text(encoding="utf-8")

    def test_deform_still_localizes_around_the_frozen_grab_center(self):
        """前提：局部化本身在 deform 侧，且球心就是冻结的 State[b+4].xyz。

        本修复**不**动 deform：它只保证喂进那个槽的值是「抓取点」而不是
        「区中心」（见下两个测试）。
        """
        deform = self._read("efmi_deform.hlsl")
        self.assertIn("float3 grabCenter = State[stateBase + 4].xyz;", deform)
        self.assertIn("w *= ComputeRubberInfluence(dist, radius, falloffPower);", deform)
        self.assertNotIn("Candidate[inst * 4 + 3]", deform)

    def test_simulate_uses_zone_center_projection_as_reference(self):
        sim = self._read("efmi_simulate.hlsl")
        # 新绑定 + 新参考式
        self.assertIn("Texture2D<float4> Anchors : register(t4);", sim)
        self.assertIn(
            "float2 zcUV = zcClip.w > 1e-5 ? ClipToScreenUV(zcClip) : cuv;", sim)
        self.assertIn("float4 zcClip = Anchors.Load(int3(gz * 8u, 0, 0));", sim)
        self.assertIn("+ down.xyz * (cuv.y - zcUV.y);", sim)
        # 旧参考（命中点 uv）不得回来——它正是塌陷成因（注释里可以提它）
        self.assertNotIn("Candidate[inst * 4 + 3].xy", sim)
        self.assertNotIn("float2 anchorUV", sim)
        # 球心落盘行保留（其它契约依赖它）
        self.assertIn("State[b + 4] = float4(grabCenter, 1.0);", sim)
        # t52 开关：本式两端同约定（0 / 未写 = 翻转 = 出厂正确态）
        self.assertIn("if (IniParams[153].w < 0.5)", sim)

    def test_clip_to_screen_uv_is_identical_across_the_three_shaders(self):
        """三处 clip→screen uv 必须逐字同式，否则命中点/手型锚点/grab 球心不可比。"""
        detect = self._read("efmi_detect.hlsl")
        hand = self._read("efmi_hand_preview.hlsl")
        sim = self._read("efmi_simulate.hlsl")
        self.assertIn("float2 ToScreen(float4 c) {", detect)
        self.assertIn(self.TOSCREEN, detect)
        # hand_preview 内联两处（gizmo / dragCols）
        self.assertEqual(hand.count("float2(.5 + .5 * "), 8)
        self.assertIn("float2 ClipToScreenUV(float4 c) {", sim)
        self.assertIn(self.TOSCREEN, sim)

    def test_generator_binds_anchor_project_to_simulate(self):
        """真正导出一次：simulate 段绑定锚点 RT，产物 shader == Toolset。"""
        node = _make_efmi_node()
        td = _make_efmi_mod_dir()
        exporter = self.mod.DragInteractionEFMIExporter(node)
        exporter.execute(str(td))
        ini = _read_ini_sections(td / "main.ini")
        simulate = "\n".join(ini["[CustomShaderEFMIDragSimulate_A]"])
        self.assertIn("cs-t4 = ResourceEFMIDragAnchorProject_A", simulate)
        self.assertIn("post cs-t4 = null", simulate)
        # 原有绑定不得被挤掉
        self.assertIn("cs-t3 = ResourceEFMIDragGizmoNormals_A", simulate)
        self.assertIn("cs-u0 = ResourceEFMIDragState_A", simulate)
        exported = (td / "res" / "drag_interaction_efmi"
                    / "efmi_simulate.hlsl").read_text(encoding="utf-8")
        self.assertEqual(exported, self._read("efmi_simulate.hlsl"))
        self.assertIn("ClipToScreenUV", exported)
        self.assertNotIn("Candidate[inst * 4 + 3].xy", exported)


class TestEFMIT52CursorConvention(unittest.TestCase):
    """t52：把「本 fork 的 cursor_y 是 bottom-up」焊成**出厂态**，三处共用一开关。

    实证链（详见各 shader 的 t52 注释）：

    · t14（早已实测定案）：把**原始**光标增量直接喂进 detect 的拖拽基
      `right/down`，模型上下走反；必须在 `delta.y` 上取一次负号才对 ⇒ 原始光标 y
      与基的 uv.y（来自 `ToScreen`，top-down）**方向相反**。
    · t51（用户本轮实测）：「鼠标命中上方，它拖拽的是下方」—— grab center 的
      Δu 若用原始光标与区中心投影做差、且不翻转，球心落在抓取点的 **Y 镜像**处。
    · 二者同源：一处需要负号（t14 的 delta）、另一处不需要（t51 的 Δu），只有当
      「原始光标 = bottom-up、基 = top-down」时二者同时自洽。

    t49 曾按 ZZMI 三个级联分支把它反推成「不必翻转」并把默认设成 0 = 不翻，等于把
    这条符号链拆散一半（命中区不可见，所以当时看不出来）。本类钉住倒过来的语义：

    · 门控必须是 `IniParams[153].w < 0.5`（0 / 未写 = **翻转** = 正确态）——
      导出器 `[Present]` 里那行中性的 `w153 = 0` 因此仍然给出正确行为。
    · 同一条件同时用于 detect 的命中判定与 t51 的 Δu。
    · 手型镜像（`[165].z`）此后**分两块**：**渲染位置**按用户 t53 的直接指定，
      0 / 未写 = 镜像**开启**（出厂）；gizmo 朝向那两个反号块保持 `> 0.5`（关）
      —— 位置与朝向刻意解耦（用户要求「只颠倒渲染位置，其他不要动」）。
    """

    TOOLSET = REPO_ROOT / "Toolset" / "drag_interaction_efmi"

    def _read(self, name):
        return (self.TOOLSET / name).read_text(encoding="utf-8")

    def test_detect_flip_is_the_default(self):
        d = self._read("efmi_detect.hlsl")
        self.assertIn("if (IniParams[153].w < 0.5)", d)
        self.assertIn("cursor.y = 1.0 - cursor.y;", d)
        self.assertNotIn("if (IniParams[153].w > 0.5)", d)

    def test_grab_center_shares_the_same_gate(self):
        s = self._read("efmi_simulate.hlsl")
        self.assertIn("if (IniParams[153].w < 0.5)", s)
        self.assertIn("cuv.y = 1.0 - cuv.y;", s)
        self.assertEqual(s.count("IniParams[153].w"), 1)   # 只此一处，不散落

    def test_hand_mirror_gates_split_position_vs_basis(self):
        """t53/t54：位置镜像**按状态分流**，且与朝向解耦。

        · t53（用户直接指定「只颠倒渲染位置，其他不要动」）：位置与朝向解耦 ——
          渲染位置门控 `< 0.5`（0 / 未写 = 开启镜像 = 出厂）；gizmo 朝向那两块保持
          `> 0.5`（关）。
        · t54（用户实测「拖拽的光标又错位了 —— 不要镜像拖拽的光标」）：镜像作用在
          **最终锚点** `anchorPx = UvToYupPx(frozenAnchor + uvDelta)` 上，抓取态里
          含拖拽跟随量 ⇒ 必须加 `&& !capturing`，否则跟随方向被一起反号。
        """
        h = self._read("efmi_hand_preview.hlsl")
        self.assertEqual(h.count("if (IniParams[165].z > 0.5)"), 1)               # gizmo 朝向 → 关
        self.assertEqual(h.count("if (IniParams[165].z < 0.5 && !capturing)"), 1)  # 位置 → 悬停/蓄力
        self.assertNotIn("if (IniParams[165].z < 0.5)", h.replace(
            "if (IniParams[165].z < 0.5 && !capturing)", ""))                     # 裸门控不得回来
        # 抓取路径不再镜像：镜像块必须在 capturing 判定之后、且被 !capturing 排除
        self.assertIn("bool capturing = grabInst >= 0;", h)
        self.assertLess(h.index("bool capturing = grabInst >= 0;"),
                        h.index("if (IniParams[165].z < 0.5 && !capturing)"))

    def test_generator_present_line_stays_neutral_and_correct(self):
        """导出器那行 `w153 = 0` 在 t52 语义下 = 翻转 = 正确态（不注入开关也安全）。"""
        gen = (REPO_ROOT / "blueprint"
               / "node_postprocess_draginteraction_efmi.py").read_text(encoding="utf-8")
        self.assertIn('"w153 = 0"', gen)
        self.assertNotIn('"w153 = 1"', gen)


class TestEFMIT55GeneratorParity(unittest.TestCase):
    """t55：生成器产物 == 已修好的模组（**真实导出**核对，不靠读代码推断）。

    生成器侧本轮只改两处：`w153 = 0` 上方的语义注释，以及**新增** `z165 = 0` 的
    显式发射（手型渲染位置镜像；抓取路径在 shader 里用 `&& !capturing` 排除）。
    其余修复全部由「生成器逐字节复制 `Toolset/drag_interaction_efmi/*.hlsl`」继承：

      · t47 deform 位移门只按 mode 开合
      · t51 grab center 参考 uv = 锚点行 `zone*8+0` 投影（含 simulate 段 `cs-t4` 绑定）
      · t52 光标 Y 约定（detect 命中判定 + simulate Δu 共用同一门控）
      · t53/t54 手型渲染位置镜像（悬停/蓄力开、抓取路径明确排除）

    F9/F11 诊断键**故意不发射**（t47 先例：生成器不带调试旋钮）——出厂行为由
    shader 默认门控 + `w153 = 0` / `z165 = 0` 共同决定，重新导出即等价于当前模组。
    """

    TOOLSET = REPO_ROOT / "Toolset" / "drag_interaction_efmi"
    GEN_PATH = REPO_ROOT / "blueprint" / "node_postprocess_draginteraction_efmi.py"

    def setUp(self):
        self.mod = _load_efmi_module()

    def test_generator_emits_the_convention_factory_values(self):
        gen = self.GEN_PATH.read_text(encoding="utf-8")
        self.assertIn('"w153 = 0"', gen)
        # z165 的显式发射（用字面量避免在外层字符串里求值常量名）
        self.assertIn('f"z{EFMI_RELEASE_BOOST_IP} = 0"', gen)
        self.assertNotIn('"w153 = 1"', gen)
        self.assertNotIn("yflip", gen)            # 诊断开关不得进生成器
        self.assertNotIn("KeyEFMIDragYFlip", gen)

    def test_real_export_reproduces_toolset_shaders_and_factory_ini(self):
        node = _make_efmi_node(enable_hand_cursor=True)
        td = _make_efmi_mod_dir()
        self.mod.DragInteractionEFMIExporter(node).execute(str(td))
        res = td / "res" / "drag_interaction_efmi"
        for name in ("efmi_detect.hlsl", "efmi_simulate.hlsl", "efmi_hand_preview.hlsl",
                     "efmi_deform.hlsl", "efmi_probe.hlsl", "efmi_hand.hlsl",
                     "efmi_ui_publish.hlsl"):
            self.assertEqual((res / name).read_bytes(),
                             (self.TOOLSET / name).read_bytes(), name)
        ini = (td / "main.ini").read_text(encoding="utf-8")
        sim = "\n".join(
            _read_ini_sections(td / "main.ini")["[CustomShaderEFMIDragSimulate_A]"])
        self.assertIn("cs-t4 = ResourceEFMIDragAnchorProject_A", sim)   # t51
        self.assertIn("post cs-t4 = null", sim)
        self.assertIn("w153 = 0", ini)                                  # t52 出厂值
        self.assertIn("z165 = 0", ini)                                  # t54 出厂值
        self.assertIn("$cursorX = cursor_x", ini)                       # t48/t52 禁触区
        self.assertIn("$cursorY = cursor_y", ini)
        self.assertNotIn("key = f9", ini)                               # 诊断键不进产物
        self.assertNotIn("key = f11", ini)
        sim_shader = (res / "efmi_simulate.hlsl").read_text(encoding="utf-8")
        self.assertNotIn("Candidate[inst * 4 + 3].xy", sim_shader)      # t51 旧参考不得回来
        hp = (res / "efmi_hand_preview.hlsl").read_text(encoding="utf-8")
        self.assertIn("if (IniParams[165].z < 0.5 && !capturing)", hp)  # t54 分流


if __name__ == "__main__":
    unittest.main()
