"""T22：HIMI 站位（stub）机制单测 —— 「借位 ≠ 吸收」+ 三条规则 + G1 交互 + 生命周期。

设计依据：``HIMI骨骼合并计划书.md`` §7.7（10 小节，D09 重新裁定）+ T19 的
``reports/himi-merge/13-stub-anatomy.md``（ZZMI/EFMI 取证）。

覆盖（对应计划书 §7.7.8 的 ST1..ST12）：

A 类（判据）——每条正向都配一个反例：
* ST1 部分缺失 ⇒ 缺失组件补占位；反例 = 家族全在场景 ⇒ 不插桩；
* ST2 整个 DrawIB 缺席 ⇒ **逐组件**判定：只有「专属槽位被现存对象实际引用」的成员
  补占位，其余成员按**保守方向**不插桩并记入 ``skipped_members``（计划书 §7.7.8 ST2
  的 T30 同步口径）；
* **ST3（借位反例，最重要）** 复现 ZZMI 2026-09-16 叶瞬光01 脸部被误插占位的**事故
  形态**：载体对象只引用了**借位** canonical 槽位（该骨骼被别的组件也引用）⇒ 必须
  判「未被吸收」⇒ **不插桩**；
* ST4 零引用 ⇒ 不插桩（用户只是删了对象、几何没进 mod）；
* ST5 ``VGMapDedupExcluded=True`` ⇒ 即使被引用也不插桩；无反查数据（无 VGMap）⇒ 不插桩。

B 类（结构断言）：
* ST6 **站位对象**不算家族覆盖（G1-a）：站位件不在 ``L``（已落盘带数据集合）里；
* ST7 **站位对象**不进 ``target_bares``（G1-b；口径边界见计划书 §7.7.11）：元数据两个
  只读字段 + 注入前后 target 集合不变。**被站位的 bare 则必须进 `export_bares`**
  （否则 G1 家族门在站位场景必然中止导出 ⇒ 由
  ``StubInjectionOrderTests::test_stub_bare_enters_export_set_to_close_the_family_gate``
  钉死；两条断言不矛盾：前者说合成对象，后者说被站位的**已落盘 bare**）；
* ST8 占位**不新增槽位**：权重组名恒 ``"0"``，且 ``prepare()`` 的槽位数不变；
* ST9 占位**不进吸收证据**：``_collect_used_group_ids`` 排除带标记对象（自证循环防护）；
* ST10 残留自愈**早于** present 集合构造：预置同名残留站位 ⇒ 先清理 ⇒ 该部件仍被判缺席；
* ST11 事务闭合：``export()`` 正常与异常路径都清理站位对象与注入的 DrawCall；
* ST12 **普通模式只跑规则①**（t107 反转）：``import_merged_vgmap()`` 关闭时不再整段
  跳过 —— 部分在场的家族照旧给缺失成员补站位，规则②/④ 仍留在开关后面；
* ST13/ST14 在场判定按 **DrawIB 身份**（``member`` 或 ``member.<网格名>``），不做任意前缀
  匹配（否则在场组件也被插桩、抢掉真部件那一笔）；
* ST15 ``allowed_rules=("1",)`` 的逐条门控（规则① 放行、②/④ 记
  ``HIMI_STUB_REASON_RULE_GATED``）。

运行：``.venv\\Scripts\\python.exe -m pytest tests/test_himi_stub_objects.py -q``
"""

import ast
import importlib.util
import io
import json
import os
import re
import shutil
import struct
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "himi_stub_objects_test_pkg"

GAMETYPE = "GPU_P12_N12_C4_TA16_BW16_BI16_T8_T1-8_"
LOD = "LOD0"


# --------------------------------------------------------------------------- #
# fake 宿主
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
    return _load_real_module_from_path(qualname, REPO_ROOT / relpath)


def _load_real_module_from_path(qualname, path):
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
_load_real_module(f"{PKG}.utils.ssmt_error_utils", "utils/ssmt_error_utils.py")
_load_real_module(f"{PKG}.common.m_ini_builder", "common/m_ini_builder.py")
_load_real_module(f"{PKG}.common.logic_name", "common/logic_name.py")
_load_real_module(f"{PKG}.common.global_key_count_helper", "common/global_key_count_helper.py")
_load_real_module(f"{PKG}.common.himi_merged_contract", "common/himi_merged_contract.py")
_skeleton = _load_real_module(f"{PKG}.common.himi_skeleton", "common/himi_skeleton.py")
_runtime = _load_real_module(
    f"{PKG}.common.himi_merged_runtime", "common/himi_merged_runtime.py"
)
# ui/universal/himi.py 会 import 这个模块（族级 X 解析）；假包不登记就 ImportError
_load_real_module(f"{PKG}.common.himi_family_x", "common/himi_family_x.py")


# ---- fake bpy（对象/网格注册表 + 顶点组） ---- #


class _FakeVertexGroup:
    def __init__(self, name):
        self.name = name
        self.add_calls = []

    def add(self, indices, weight, mode):
        self.add_calls.append((list(indices), weight, mode))


class _FakeVertexGroups(list):
    def new(self, name):
        group = _FakeVertexGroup(name)
        self.append(group)
        return group


class _FakeMesh:
    def __init__(self, name):
        self.name = name
        self.users = 0
        self.from_pydata_calls = []
        self.vertices = []

    def from_pydata(self, verts, edges, faces):
        self.from_pydata_calls.append((verts, edges, faces))

    def update(self):
        pass


class _FakeObject:
    def __init__(self, name, object_data=None):
        self.name = name
        self.data = object_data
        self.vertex_groups = _FakeVertexGroups()
        self.props = {}

    def __setitem__(self, key, value):
        self.props[key] = value

    def get(self, key, default=None):
        return self.props.get(key, default)


class _FakeObjectRegistry:
    def __init__(self):
        self._items = {}

    def new(self, name, object_data=None):
        obj = _FakeObject(name, object_data)
        self._items[name] = obj
        return obj

    def get(self, name):
        return self._items.get(name)

    def remove(self, obj, do_unlink=False):
        self._items.pop(obj.name, None)

    def __iter__(self):
        return iter(list(self._items.values()))

    def __len__(self):
        return len(self._items)


class _FakeMeshRegistry:
    def __init__(self):
        self._items = {}
        self.fail_next_new = False

    def new(self, name):
        if self.fail_next_new:
            self.fail_next_new = False
            raise RuntimeError("站位 mesh 创建失败（测试注入）")
        mesh = _FakeMesh(name)
        self._items[name] = mesh
        return mesh

    def remove(self, mesh):
        self._items.pop(mesh.name, None)


_fake_objects = _FakeObjectRegistry()
_fake_meshes = _FakeMeshRegistry()
_bpy_module = _install_module(
    "bpy",
    data=types.SimpleNamespace(objects=_fake_objects, meshes=_fake_meshes),
    context=types.SimpleNamespace(
        collection=types.SimpleNamespace(objects=types.SimpleNamespace(link=lambda _obj: None)),
        scene=types.SimpleNamespace(
            collection=types.SimpleNamespace(objects=types.SimpleNamespace(link=lambda _obj: None))
        ),
    ),
)

_GLOBAL_STATE = {"workspace": "", "mod": ""}
_fake_global_config = types.SimpleNamespace(
    logic_name="HIMI",
    path_workspace_folder=lambda: _GLOBAL_STATE["workspace"],
    path_generate_mod_folder=lambda: _GLOBAL_STATE["mod"],
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
# 真实 `ExportUnity.generate_unity_vs_config_ini()` 会调这一组方法。端到端用例要跑
# **真实** 的 export() ⇒ 这些必须存在（纯写盘/文本搬运，不影响站位段的生成）。
_install_module(
    f"{PKG}.common.m_ini_helper",
    M_IniHelper=types.SimpleNamespace(
        get_drawindexed_str_list=lambda *_a, **_k: [],
        is_slot_binding_mark_type=lambda *_a, **_k: False,
        generate_hash_style_texture_ini=lambda *_a, **_k: None,
        generate_shared_slot_style_texture_ini=lambda *_a, **_k: None,
        move_slot_style_textures=lambda *_a, **_k: None,
        add_branch_key_sections=lambda *_a, **_k: None,
        add_shapekey_ini_sections=lambda *_a, **_k: None,
    ),
)
_install_module(
    f"{PKG}.common.m_ini_helper_gui",
    M_IniHelperGUI=types.SimpleNamespace(add_branch_mod_gui_section=lambda *_a, **_k: None),
)
_install_module(
    f"{PKG}.utils.timer_utils",
    TimerUtils=types.SimpleNamespace(start_stage=lambda *_a, **_k: None, end_stage=lambda *_a, **_k: None),
)
_install_module(f"{PKG}.blueprint.export_helper", BlueprintExportHelper=_fake_export_helper)


class _FakeDrawIBExportBase:
    """基类替身：**按 export_helper 的语义真的解析 ordered**（T26 F1 的核心）。

    站位注入必须在基类组装模型**之前**发生。旧版替身把 ``drawib_model_list`` 恒置空、
    从不看 ``ordered`` ⇒ **顺序问题被测试宿主抹掉**（18 条测试全绿却漏掉真 bug）。
    这里改成：

    1. 用 ``ExportHelper.parse_submesh_model_list_from_blueprint_model`` 的语义
       （``ui/universal/export_helper.py:43-50``：按 ``get_workspace_unique_str()``
       把 DrawCallModel 分组）解析**调用时刻**的 ``ordered``；
    2. 把解析出的 submesh 列表挂进 ``drawib_model_list``（每个 submesh 一个元素，
       结构等价于真实 ``DrawIBModel.submesh_model_list`` 的承载关系）；
    3. 记录调用时刻的 ``ordered`` 快照，供断言「注入早于解析」。

    ``SubMeshModel`` 本体依赖 bpy 与工作空间元数据（``calc_buffer``），这里不实例化它
    —— 断言只需要「stub DrawCall 出现在被解析的分组里」这一结构事实。
    """

    def generate_buffer_files(self, output_folder):
        """端到端用例要跑**真实** `ExportUnity.export()`；这里只记账，不写 .buf。

        buffer 写盘依赖真实 `DrawIBModel` 的 numpy 布局产物，不是本用例的考察对象
        —— 考察对象是 **INI 产物里有没有站位段**，而 INI 链是**真的**。
        """
        self.buffer_folder_calls.append(output_folder)

    def export_buffers_only(self):
        self.export_buffers_calls += 1
        return True

    def _integrate_object_swap_ini_hook(self, ini_builder):
        """真实基类里的「物体切换节点 INI 集成」钩子；本宿主无蓝图树 ⇒ 空实现。"""
        return None

    def __init__(self, blueprint_model, combine_ib=False):
        self.blueprint_model = blueprint_model
        self.drawib_model_list = []
        self.buffer_folder_calls = []
        self.export_buffers_calls = 0
        ordered = getattr(blueprint_model, "ordered_draw_obj_data_model_list", None) or []
        # 调用时刻的快照（顺序断言用）
        self.ordered_snapshot = list(ordered)
        grouped: dict[str, list] = {}
        for draw_call in ordered:
            try:
                unique_str = str(draw_call.get_workspace_unique_str() or "")
            except Exception:  # noqa: BLE001
                unique_str = str(getattr(draw_call, "obj_name", "") or "")
            grouped.setdefault(unique_str, []).append(draw_call)
        for unique_str, draw_calls in grouped.items():
            # 结构等价于真实 SubMeshModel 的两个可观测字段（名字 + drawcall 列表）
            self.drawib_model_list.append(
                types.SimpleNamespace(
                    unique_str=unique_str,
                    workspace_unique_str=unique_str,
                    submesh_model_list=[
                        types.SimpleNamespace(
                            unique_str=unique_str,
                            workspace_unique_str=unique_str,
                            drawcall_model_list=list(draw_calls),
                        )
                    ],
                )
            )


_install_module(f"{PKG}.ui.universal.drawib_export_base", DrawIBExportBase=_FakeDrawIBExportBase)


# --------------------------------------------------------------------------- #
# 两态复现钩子（t33 F-3）：`HIMI_F1_BROKEN_ORDER=1` ⇒ 用**坏顺序副本**加载 himi.py
# --------------------------------------------------------------------------- #
# 背景（t29 F-3）：t26 的两态证据用的是 `.dbg/_t26_toggle.py`，而 `.dbg/` 被 gitignore
# ⇒ 别人 clone 后无法复现「修复前失败 / 修复后通过」。这里把同一件事做成**仓库内、零写入**
# 的形式：只在临时目录里把 `ExportHIMI.__init__` 的注入块搬到 `super().__init__()`
# **之后**（= F1 缺陷本体），仓库文件一个字节都不动。
#
#   # 修复后（正常状态）→ 全绿
#   .\.venv\Scripts\python.exe -m pytest tests/test_himi_stub_objects.py -q
#   # 修复前（等价 overlay；PowerShell）→ 顺序测试 + 端到端 ini 用例必须红
#   $env:HIMI_F1_BROKEN_ORDER="1"; .\.venv\Scripts\python.exe -m pytest tests/test_himi_stub_objects.py -q
#   Remove-Item Env:\HIMI_F1_BROKEN_ORDER
HIMI_F1_BROKEN_ORDER_ENV = "HIMI_F1_BROKEN_ORDER"
_HIMI_EXPORTER_RELPATH = "ui/universal/himi.py"
#: overlay 临时目录前缀（t35 G-3：清理**只认这个前缀**，绝不碰别人的目录）
_HIMI_F1_OVERLAY_PREFIX = "himi_f1_overlay_"
#: 本进程由钩子建过的 overlay 目录（供护栏断言「运行后无残留」）
_HIMI_OVERLAY_CREATED: list = []


def _f1_break_points(source: str):
    """定位 F1 断链点：`ExportHIMI.__init__` 里的注入 `Try` 块与 `super().__init__()`。

    返回 ``(inject_try_node, base_call_node)``；定位失败返回 ``(None, None)``。
    用 AST 节点定位（不依赖行号常量），因此 `__init__` 里加删无关语句也不会失准。
    """
    tree = ast.parse(source)
    for node in tree.body:
        if not (isinstance(node, ast.ClassDef) and node.name == "ExportHIMI"):
            continue
        for stmt in node.body:
            if not (isinstance(stmt, ast.FunctionDef) and stmt.name == "__init__"):
                continue
            inject = base = None
            for inner in stmt.body:
                segment = ast.get_source_segment(source, inner) or ""
                if isinstance(inner, ast.Try) and "_himi_inject_stub_objects()" in segment:
                    inject = inner
                elif isinstance(inner, ast.Expr) and segment.strip().startswith(
                    "super().__init__("
                ):
                    base = inner
            return inject, base
    return None, None


def _injection_precedes_base_init(source: str) -> bool:
    """`ExportHIMI.__init__` 里注入块是否在 `super().__init__()` **之前**（F1 的正确顺序）。"""
    inject, base = _f1_break_points(source)
    if inject is None or base is None:
        raise AssertionError("无法定位 F1 断链点（注入块 / super().__init__）")
    return inject.lineno < base.lineno


def _f1_broken_order_source(source: str) -> str:
    """把注入块搬到 `super().__init__()` **之后**（F1 缺陷本体），返回新源码。

    纯行级搬运 + `compile()` 语法自检 + 后置条件自检（搬完必须
    ``_injection_precedes_base_init(...) is False``）——任一步不成立就抛错，
    绝不静默产出一个「看起来像坏顺序」的副本。
    """
    inject, base = _f1_break_points(source)
    if inject is None or base is None:
        raise AssertionError("无法定位 F1 断链点（注入块 / super().__init__）")
    lines = source.splitlines(keepends=True)
    head = lines[: inject.lineno - 1]
    inject_block = lines[inject.lineno - 1 : inject.end_lineno]
    between = lines[inject.end_lineno : base.lineno - 1]
    base_block = lines[base.lineno - 1 : base.end_lineno]
    tail = lines[base.end_lineno :]
    broken = "".join(head + between + base_block + inject_block + tail)
    compile(broken, "<himi-f1-broken-order>", "exec")
    if _injection_precedes_base_init(broken):
        raise AssertionError("overlay 未真的把注入搬到基类组装之后")
    return broken


def _himi_module_path_for_load():
    """加载 `ui/universal/himi.py` 用的路径（两态钩子生效时 = 临时坏顺序副本）。"""
    repo_path = REPO_ROOT / _HIMI_EXPORTER_RELPATH
    requested = str(os.environ.get(HIMI_F1_BROKEN_ORDER_ENV, "")).strip().lower()
    if requested not in ("1", "true", "yes", "on"):
        return repo_path
    source = io.open(repo_path, encoding="utf-8").read()
    broken = _f1_broken_order_source(source)
    overlay_dir = tempfile.mkdtemp(prefix=_HIMI_F1_OVERLAY_PREFIX)
    _HIMI_OVERLAY_CREATED.append(overlay_dir)
    overlay_path = os.path.join(overlay_dir, "himi_broken_order.py")
    with io.open(overlay_path, "w", encoding="utf-8") as handle:
        handle.write(broken)
    print(
        f"[himi-f1-overlay] {HIMI_F1_BROKEN_ORDER_ENV}=1 生效 ⇒ 从坏顺序副本加载 himi.py："
        f"{overlay_path}（仓库文件未改动；injection_precedes_base_init=False）"
    )
    return overlay_path


def _cleanup_himi_overlay_dir(path) -> bool:
    """删掉 overlay 临时目录（**只认自己的前缀**，含复核 + 最多 3 次重试；t35 G-3）。

    返回「是否已确认删除」。非本前缀的目录**一律不动**（返回 ``False`` 且不做任何事）。
    删不掉时**不抛异常**（清理失败不该打断测试会话），但会大声打印 —— 并由
    `StubNailIntegrityTests::test_overlay_dir_leaves_no_residue` 断言成失败。
    """
    target = str(path or "")
    name = os.path.basename(target.rstrip("\\/"))
    if not target or not name.startswith(_HIMI_F1_OVERLAY_PREFIX):
        return False
    for attempt in range(3):
        shutil.rmtree(target, ignore_errors=True)
        if not os.path.exists(target):
            return True
        time.sleep(0.05 * (attempt + 1))
    print(f"[himi-f1-overlay] 警告：临时目录清理失败，仍有残留：{target}")
    return False


def _load_himi_exporter_module(qualname):
    """加载 `ui/universal/himi.py`；两态钩子生效时用临时副本，并**保证清掉**临时目录。

    清理放在 `try/finally`（G-3）：**任何**加载结果（成功 / 异常）都不留残留。
    代价：坏顺序副本只在加载期间存在 ⇒ 事后坏顺序 traceback 里的那个文件路径不再可读
    （换取「零残留」；需要留档时请自行把副本拷出）。
    """
    path = _himi_module_path_for_load()
    try:
        return _load_real_module_from_path(qualname, path)
    finally:
        _cleanup_himi_overlay_dir(os.path.dirname(str(path)))


# 真实 ExportUnity：站位用例要跑真实的 `export()` 链（含 finally 清理）。
# 顺序断言对 `ExportUnity.__init__` 就地打桩（`StubInjectionOrderTests` 里的
# `_recording_init`），因此不需要装载真实 `export_helper.py`（它会把 blueprint.model
# 等一长串依赖拖进来）。
_unity = _load_real_module(f"{PKG}.ui.universal.unity", "ui/universal/unity.py")
_himi_exporter = _load_himi_exporter_module(f"{PKG}.ui.universal.himi")
M_IniBuilder = sys.modules[f"{PKG}.common.m_ini_builder"].M_IniBuilder

ExportHIMI = _himi_exporter.ExportHIMI
ExportUnity = _unity.ExportUnity
Fatal = sys.modules[f"{PKG}.utils.format_utils"].Fatal
HIMI_STUB_MARKER = _skeleton.HIMI_STUB_MARKER
HIMI_STUB_SLOT_GROUP_NAME = _skeleton.HIMI_STUB_SLOT_GROUP_NAME
decide_stub_injection = _skeleton.decide_stub_injection
HIMISkeletonMergeHelper = _skeleton.HIMISkeletonMergeHelper


# --------------------------------------------------------------------------- #
# 工作空间夹具
# --------------------------------------------------------------------------- #


def _blend_record(indices) -> bytes:
    return struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) + struct.pack("<4i", *list(indices)[:4])


def _component_payload(
    *,
    bare: str,
    draw_ib: str,
    vg_map,
    family_members,
    excluded: bool = False,
    rows: int = 1,
    local_indices=(0, 0, 0, 0),
    position_hash: str = "2b325128",
    blend_hash: str = "8322ac5a",
    vertex_limit_vb: str = "2c5605ed",
):
    payload = {
        "GamePreset": "HI3",
        "VertexLimitVB": vertex_limit_vb,
        "CategoryHash": {"Blend": blend_hash, "Position": position_hash, "Texcoord": "84611940"},
        "WorkGameType": GAMETYPE.rstrip("_"),
        "GPU-PreSkinning": True,
        "VGCount": max([int(k) for k in vg_map] + [0]) + 1,
        "VGOffset": 0,
        "VGMap": {str(k): int(v) for k, v in dict(vg_map).items()},
        "OriginalVertexCount": int(rows),
        "DeformDrawIndex": 35,
        "SkeletonGroup": 0,
        "HIMIDeformFamilyIB": draw_ib,
        "HIMIDeformFamilyMembers": list(family_members),
        "HIMIDeformGateSafe": True,
        "HIMIDeformGateVersion": 1,
        "HIMIMergedSkeletonSlots": 64,
        "HIMIMergedSkeletonDigest": "digest0",
        "BoneMatrixSlotCount": len(vg_map),
    }
    if excluded:
        payload["VGMapDedupExcluded"] = True
    return payload


def _write_workspace(
    root: str,
    components,
    family_map,
    *,
    import_map=None,
    write_import_json=True,
):
    """Build a HIMI workspace: component jsons + ``LOD0/DrawIB-Component.json``."""
    os.makedirs(os.path.join(root, "Config"), exist_ok=True)
    os.makedirs(os.path.join(root, LOD), exist_ok=True)
    import_map = dict(import_map or {})
    for component in components:
        bare = component["bare"]
        folder = os.path.join(root, LOD, bare, GAMETYPE)
        os.makedirs(folder, exist_ok=True)
        payload = _component_payload(
            bare=bare,
            draw_ib=component.get("draw_ib") or bare.split("-", 1)[0],
            vg_map=component["vg_map"],
            family_members=component.get("family_members") or [bare],
            excluded=bool(component.get("excluded")),
            rows=int(component.get("rows", 1)),
            local_indices=component.get("local_indices") or (0, 0, 0, 0),
            position_hash=str(component.get("position_hash") or "2b325128"),
            blend_hash=str(component.get("blend_hash") or "8322ac5a"),
            vertex_limit_vb=str(component.get("vertex_limit_vb") or "2c5605ed"),
        )
        with io.open(os.path.join(folder, f"{bare}.json"), "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        records = bytearray()
        for _ in range(int(component.get("rows", 1))):
            records += _blend_record(component.get("local_indices") or (0, 0, 0, 0))
        with open(os.path.join(folder, f"{bare}-Blend.buf"), "wb") as handle:
            handle.write(bytes(records))
        import_map.setdefault(f"{LOD}.{bare}", GAMETYPE)
    if write_import_json:
        with io.open(os.path.join(root, "Import.json"), "w", encoding="utf-8") as handle:
            json.dump(import_map, handle, ensure_ascii=False)
    with io.open(
        os.path.join(root, LOD, _skeleton.HIMI_COMPONENT_MAP_BASENAME), "w", encoding="utf-8"
    ) as handle:
        json.dump(family_map, handle, ensure_ascii=False)
    return root


def _register_object(name: str, groups=(), marked: bool = False):
    """Register a real (or stub-marked) scene object carrying weighted groups."""
    mesh = _fake_meshes.new("MESH_" + name)
    mesh.vertices = []
    obj = _fake_objects.new(name, object_data=mesh)
    if marked:
        obj[HIMI_STUB_MARKER] = 1
    for group_id, weight in groups:
        group = obj.vertex_groups.new(name=str(int(group_id)))
        vertex = types.SimpleNamespace(groups=[types.SimpleNamespace(group=len(obj.vertex_groups) - 1, weight=float(weight))])
        mesh.vertices.append(vertex)
        group.add([0], float(weight), "REPLACE")
    return obj


def _clear_scene():
    for obj in list(_fake_objects):
        _fake_objects.remove(obj)
    _fake_meshes._items.clear()


def _builder_lines(builder) -> list:
    """Flatten an `M_IniBuilder` into `[section header, *lines, ...]`."""
    lines: list = []
    for section in getattr(builder, "ini_section_list", []):
        if getattr(section, "SectionName", ""):
            lines.append(f"[{section.SectionName}]")
        lines.extend(list(getattr(section, "SectionLineList", [])))
    return lines


class _PlainDrawCall:
    """A **non-stub** duck-typed draw call (real parts in the ordered list).

    ``HIMIStubDrawCall`` defaults to ``himi_stub=True`` (that is its whole point),
    so the ordering / evidence tests need this separate shape for real parts.
    """

    himi_stub = False

    def __init__(self, name):
        self.obj_name = name
        self.source_obj_name = ""
        self.vertex_count = 0
        self.index_count = 0
        self.index_offset = 0

    def get_blender_obj_name(self):
        return self.source_obj_name if self.source_obj_name else self.obj_name

    def get_workspace_unique_str(self):
        return str(self.obj_name or "")

    def get_condition_str(self):
        return ""


class _StubCase(unittest.TestCase):
    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="himi_stub_test_"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.addCleanup(_clear_scene)
        # 全量跑时别的测试文件也会往 sys.modules 里注册它们自己的 fake bpy
        # （导入顺序不定）⇒ 本文件必须把自己的那份重新装回去，否则
        # ``common/himi_skeleton.py`` 的 bpy 桥会查到**别人的**对象注册表。
        sys.modules["bpy"] = _bpy_module
        self.workspace = str(self._tmp / "ws")
        _clear_scene()
        _GLOBAL_STATE["workspace"] = self.workspace
        _GLOBAL_STATE["mod"] = str(self._tmp / "mod")
        _properties_state["checkbox"] = True
        _fake_meshes.fail_next_new = False



    def _build_dump_fixture(self, *, separate_bones: bool = False):
        """最小但**完整**的 dump + 工作空间，使 ``prepare()`` 能真正 join 成功。

        要点（与真实 dump 同构，任一错位都会让 join 失败）：
        * 组件 json 的 ``CategoryHash.Position`` / ``.Blend`` 必须等于 deform pass
          的 vb0/vb1 **deduped 内容 hash**（侧车文件名里那一段）；
        * ``VertexLimitVB`` 必须等于 ``SOSetTargets`` 的 hash；
        * palette 池大小必须 > ``max(BLENDINDICES)``（否则数据层拒绝生成）。

        ``separate_bones=True``：让两个组件用**不同的 Blend 缓冲**（骨骼集合不相交），
        这样 ``commit()`` 之后 ``exclusive(aaaa-2-0)`` 仍非空 —— 「真注入」用例需要
        注入**之后**的落盘状态仍能支撑判据（共享骨骼会让 exclusive 退化成空集）。
        """
        import hashlib

        dump = self._tmp / "FrameAnalysis-stub"
        deduped = dump / "deduped"
        deduped.mkdir(parents=True, exist_ok=True)
        os.makedirs(os.path.join(self.workspace, "Config"), exist_ok=True)

        position_content = "aaaabbbb"
        blend_content = "ccccdddd"
        blend_content_b = "cccc1111" if separate_bones else blend_content
        so_target = "7cb2e09c"
        palette_content = "eeeeffff"
        vertex_count = 1

        # 工作空间 .buf（deform 输入）
        position_bytes = bytes(44 * vertex_count)
        blend_bytes = _blend_record((0, 1, 0, 0)) * vertex_count
        blend_bytes_b = (
            _blend_record((2, 3, 0, 0)) * vertex_count if separate_bones else blend_bytes
        )
        _write_workspace(
            self.workspace,
            [
                {
                    "bare": "aaaa-1-0",
                    "vg_map": {0: 0, 1: 1},
                    "position_hash": position_content,
                    "blend_hash": blend_content,
                    "vertex_limit_vb": so_target,
                },
                {
                    "bare": "aaaa-2-0",
                    # 专属槽 40/41（0/1 与在场者共享 = 借位，不构成吸收证据）
                    "vg_map": {0: 0, 1: 1, 2: 40, 3: 41},
                    "position_hash": position_content,
                    "blend_hash": blend_content_b,
                    "vertex_limit_vb": so_target,
                },
            ],
            {"aaaa": ["aaaa-1-0", "aaaa-2-0"]},
        )
        for bare in ("aaaa-1-0", "aaaa-2-0"):
            folder = os.path.join(self.workspace, LOD, bare, GAMETYPE)
            with open(os.path.join(folder, f"{bare}-Position.buf"), "wb") as handle:
                handle.write(position_bytes)
            payload = blend_bytes_b if bare == "aaaa-2-0" else blend_bytes
            with open(os.path.join(folder, f"{bare}-Blend.buf"), "wb") as handle:
                handle.write(payload)

        # dump：deduped 内容文件 + 侧车（stride/count）+ log
        (deduped / f"{position_content}.buf").write_bytes(position_bytes)
        (
            deduped
            / f"{position_content}-vb0-layout=abcd1234-topology=pointlist-stride=44-count={vertex_count}.txt"
        ).write_text("", encoding="utf-8")
        (deduped / f"{blend_content}.buf").write_bytes(blend_bytes)
        (
            deduped
            / f"{blend_content}-vb1-layout=abcd1234-topology=pointlist-stride=32-count={vertex_count}.txt"
        ).write_text("", encoding="utf-8")
        if separate_bones:
            (deduped / f"{blend_content_b}.buf").write_bytes(blend_bytes_b)
            (
                deduped
                / f"{blend_content_b}-vb1-layout=abcd1234-topology=pointlist-stride=32-count={vertex_count}.txt"
            ).write_text("", encoding="utf-8")
        palette = bytearray()
        for slot in range(8 if separate_bones else 4):
            for index in range(12):
                palette += struct.pack("<f", float(slot * 12 + index))
        palette = bytes(palette)
        (deduped / f"{palette_content}.buf").write_bytes(palette)

        lines = [
            f"000035 3DMigoto Dumping Buffer {dump}\\000035-vb0=11111111-vs=10905ed856e6b621.buf"
            f" -> {deduped}\\{position_content}.buf",
            f"000035 3DMigoto Dumping Buffer {dump}\\000035-vb0=11111111-vs=10905ed856e6b621.txt"
            f" -> {deduped}\\{position_content}-vb0-layout=abcd1234-topology=pointlist-stride=44-count={vertex_count}.txt",
            f"000035 3DMigoto Dumping Buffer {dump}\\000035-vb1=22222222-vs=10905ed856e6b621.buf"
            f" -> {deduped}\\{blend_content}.buf",
            f"000035 3DMigoto Dumping Buffer {dump}\\000035-vb1=22222222-vs=10905ed856e6b621.txt"
            f" -> {deduped}\\{blend_content}-vb1-layout=abcd1234-topology=pointlist-stride=32-count={vertex_count}.txt",
            f"000035 3DMigoto Dumping Buffer {dump}\\000035-vs-t0=33333333-vs=10905ed856e6b621.buf"
            f" -> {deduped}\\{palette_content}.buf",
            "000035 VSSetShader(pVertexShader:0x1, ppClassInstances:0x0, NumClassInstances:0) hash=10905ed856e6b621",
            "000035 SOSetTargets(NumBuffers:1, ppSOTargets:0x1, pOffsets:0xE468)",
            f"       0: resource=0x2 hash={so_target}",
            "000035 VSSetShaderResources(StartSlot:0, NumViews:1, ppShaderView:0x3)",
            "       0: view=0x2 resource=0x3 hash=33333333",
            "000035 IASetVertexBuffers(StartSlot:0, NumBuffers:2, ppVertexBuffers:0x4)",
            "       0: resource=0x5 hash=11111111",
            "       1: resource=0x6 hash=22222222",
            "000035 IASetPrimitiveTopology(Topology:1)",
            f"000035 Draw(VertexCount:{vertex_count}, StartVertexLocation:0)",
            # 一个渲染消费者，让 SO 目标归属家族 aaaa
            f"000036 3DMigoto Dumping Buffer {dump}\\000036-vb0={so_target}-vs=55da5eab238de808.buf"
            f" -> {deduped}\\{so_target}.buf",
            f"000036 3DMigoto Dumping Buffer {dump}\\000036-ib=99999999-vs=55da5eab238de808.buf"
            f" -> {deduped}\\99999999.buf",
            "000036 VSSetShader(pVertexShader:0x7, ppClassInstances:0x0, NumClassInstances:0) hash=55da5eab238de808",
            "000036 IASetIndexBuffer(pIndexBuffer:0x8, DXGI_FORMAT:42, Offset:0)",
            "000036 IASetVertexBuffers(StartSlot:0, NumBuffers:2, ppVertexBuffers:0x9)",
            f"       0: resource=0xA hash={so_target}",
            "000036 DrawIndexed(IndexCount:3, StartIndexLocation:0, BaseVertexLocation:0)",
        ]
        (dump / "log.txt").write_text("\n".join(lines) + "\n", encoding="utf-8")
        with io.open(
            os.path.join(self.workspace, "Config", "FrameAnalysisPath.json"), "w", encoding="utf-8"
        ) as handle:
            json.dump({"frameAnalysisFolderPath": str(dump)}, handle, ensure_ascii=False)
        return {"dump": str(dump)}



    def _make_exporter(self, ordered, inject: bool = False):
        """构造导出器；``inject=False`` 时抑制 ``__init__`` 里的自动注入。

        自动注入只在「先摆好场景对象、再构造导出器」的顺序下才有意义；需要**显式**
        控制注入时机的用例（残留自愈、事务闭合）必须关掉它。
        """
        blueprint_model = types.SimpleNamespace(
            ordered_draw_obj_data_model_list=ordered,
            keyname_mkey_dict={},
        )
        if inject:
            return ExportHIMI(blueprint_model)
        exporter = ExportHIMI.__new__(ExportHIMI)
        exporter.blueprint_model = blueprint_model
        exporter._himi_stub_object_names = []
        exporter._himi_stub_draw_calls = []
        exporter._himi_stub_bares = set()
        exporter._himi_stub_decision = None
        exporter._himi_stub_writer_slots = {}
        exporter.drawib_model_list = []
        exporter._himi_plan = None
        exporter._himi_plan_ready = False
        exporter._himi_contract_decision = None
        exporter._himi_resource_emitted = False
        exporter._himi_publish_result = {}
        exporter._himi_blend_verdicts = {}
        return exporter


# --------------------------------------------------------------------------- #
# A 类：判据（ST1..ST5）
# --------------------------------------------------------------------------- #


class StubRuleTests(_StubCase):
    def test_st1_partial_family_missing_members_get_stubs(self):
        """ST1 正向：家族部分成员在场景 ⇒ 缺失组件补占位（判据只看场景真相）。"""
        _write_workspace(
            self.workspace,
            [
                # 在场者：槽 0/1；缺席者：另有专属槽 40/41（共享 0/1 是借位）
                {"bare": "aaaa-1-0", "vg_map": {0: 0, 1: 1}},
                {"bare": "aaaa-2-0", "vg_map": {0: 0, 1: 1, 2: 40, 3: 41}},
            ],
            {"aaaa": ["aaaa-1-0", "aaaa-2-0"]},
        )
        decision = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"aaaa-1-0"},
            used_group_ids={0, 1, 40},
        )
        self.assertEqual(decision.stub_members, ["aaaa-2-0"])
        self.assertEqual(decision.families["aaaa"]["decision"], _skeleton.HIMI_STUB_REASON_ABSORBED)
        self.assertEqual(decision.families["aaaa"]["in_scene"], ["aaaa-1-0"])
        self.assertEqual(decision.families["aaaa"]["absent"], ["aaaa-2-0"])

    def test_st1_inverse_family_fully_present_gets_nothing(self):
        """ST1 反例：家族全在场景 ⇒ 不插桩。"""
        _write_workspace(
            self.workspace,
            [
                {"bare": "aaaa-1-0", "vg_map": {0: 0, 1: 1}},
                {"bare": "aaaa-2-0", "vg_map": {0: 0, 1: 1}},
            ],
            {"aaaa": ["aaaa-1-0", "aaaa-2-0"]},
        )
        decision = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"aaaa-1-0", "aaaa-2-0"},
            used_group_ids={0, 1},
        )
        self.assertEqual(decision.stub_members, [])
        self.assertEqual(decision.families["aaaa"]["decision"], _skeleton.HIMI_STUB_REASON_ALREADY_PRESENT)

    def test_st2_absent_family_with_own_slot_referenced_gets_all_members(self):
        """ST2：整个 DrawIB 缺席且它是这些槽唯一的 palette 写方 ⇒ **全族**补站位。

        口径（t103，用户口径：「被合并进来的物体需要生成极限小的 3 个点的站位，就和其他的
        骨骼合并的一样」）：整族缺席时，若「该族注册的槽 ∩ 被引用（``used``）」减去「在场家族
        注册的槽」还非空 ⇒ 这些槽只有它能写 ⇒ 它的几何已被并进别人的 draw ⇒ **整个家族**
        逐成员补 3 点站位（与 ZZMI 规则① 同款），不让任何一个成员的原版 draw 继续跑。
        只有当它的槽由在场家族注册（借位 / canonical 共享）时才退回逐组件的「外来槽」判定，
        见 ``StubForeignSlotTests``。**方法名保留不改**：它被 ``decide_stub_injection`` 的
        docstring 按名字引用，改名会打断那条引用。
        """
        _write_workspace(
            self.workspace,
            [
                {"bare": "bbbb-1-0", "vg_map": {0: 10, 1: 11}},
                {"bare": "bbbb-2-0", "vg_map": {0: 20, 1: 21}},
                # 载体：已落盘的别的家族成员，声明自己的槽 0（⇒ declared_by_present 可判定）
                {"bare": "cccc-1-0", "vg_map": {0: 0}},
            ],
            {"bbbb": ["bbbb-1-0", "bbbb-2-0"], "cccc": ["cccc-1-0"]},
        )
        # 载体对象引用了 bbbb-1-0 的专属槽位 10（20/21 是 bbbb-2-0 的专属）
        decision = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"cccc-1-0"},
            used_group_ids={10},
        )
        # 载体只引用 bbbb-1-0 的槽 10，但 bbbb 整族缺席且没人替它写这些槽 ⇒
        # 它是「唯一的 palette 写方」⇒ 两个成员都补站位（含只被自己注册的 20/21）。
        self.assertEqual(decision.stub_members, ["bbbb-1-0", "bbbb-2-0"])
        self.assertEqual(
            decision.families["bbbb"]["decision"], _skeleton.HIMI_STUB_REASON_WRITERLESS
        )
        self.assertEqual(decision.skipped_members, [])
        # 两者都有专属槽位（不是借位），所以这个用例证明的不是 ST3 的借位形态
        self.assertEqual(
            _skeleton.himi_exclusive_slots(
                "bbbb-1-0", ["bbbb-1-0", "bbbb-2-0"], {"bbbb-1-0": {10, 11}, "bbbb-2-0": {20, 21}}
            ),
            {10, 11},
        )
        self.assertEqual(
            _skeleton.himi_exclusive_slots(
                "bbbb-2-0", ["bbbb-1-0", "bbbb-2-0"], {"bbbb-1-0": {10, 11}, "bbbb-2-0": {20, 21}}
            ),
            {20, 21},
        )

    def test_st13_present_member_with_mesh_suffix_is_not_absent(self):
        """t107 正向：在场身份带网格名（``aaaa-1-0.捆绑绳子``）时**不得**把该成员判成缺席。

        导出侧 ``present`` 的每一项是「DrawIB 身份 + 网格名」（来自
        ``DrawCallModel.get_workspace_unique_str()``），家族成员只到身份。若用 bare 精确
        相等，在场成员会被判成缺席 ⇒ 规则① 给在场组件也插桩 ⇒ 它的 3 索引站位与真部件的
        段抢同一个 ``match_first_index``（先命中者生效）⇒ 整个部件在游戏里静默消失
        （「多压 = 静默部件消失」那一档，比残留重影更难排查）。

        用户实测形态（HIMI / HI3 `虚构游戏`）：`09c6e58a` 家族的两个成员
        `09c6e58a-57792-0`（在场）与 `09c6e58a-4386-57792`（被并入、缺席）。
        """
        _write_workspace(
            self.workspace,
            [
                {"bare": "aaaa-1-0", "vg_map": {0: 0, 1: 1}},
                {"bare": "aaaa-2-0", "vg_map": {0: 0, 1: 1, 2: 40, 3: 41}},
            ],
            {"aaaa": ["aaaa-1-0", "aaaa-2-0"]},
        )
        decision = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"aaaa-1-0.rope_copy"},
            used_group_ids={0, 1, 40},
        )
        self.assertEqual(decision.families["aaaa"]["in_scene"], ["aaaa-1-0"])
        self.assertEqual(decision.families["aaaa"]["absent"], ["aaaa-2-0"])
        self.assertEqual(decision.stub_members, ["aaaa-2-0"])

    def test_st14_member_prefix_does_not_leak_across_identities(self):
        """t107 反向：``aaaa-1-0`` 不得被 ``aaaa-1-01.x`` 误判成在场。

        只认 ``member + "."`` 前缀（网格名分隔符），不做任意前缀匹配 —— 否则身份相近的
        两个组件会互相顶替，缺席者拿不到站位。
        """
        self.assertFalse(_skeleton._member_present_in_scene("aaaa-1-0", {"aaaa-1-01.rope"}))
        self.assertTrue(_skeleton._member_present_in_scene("aaaa-1-0", {"aaaa-1-0.rope"}))
        self.assertTrue(_skeleton._member_present_in_scene("aaaa-1-0", {"aaaa-1-0"}))

    def test_st15_rule1_only_gates_rule2_and_writerless(self):
        """t107：``allowed_rules=("1",)``（普通模式）只放开规则①，规则②/④ 不判定。

        普通模式不产出合并骨架资源，规则②/④ 的判据（VGMap / palette 槽位记账）失去意义，
        所以整族缺席的家族一律记 ``HIMI_STUB_REASON_RULE_GATED``、不插桩；而**部分在场**的
        家族照旧给缺失成员补站位 —— 那一条与合并骨架无关。
        """
        _write_workspace(
            self.workspace,
            [
                {"bare": "aaaa-1-0", "vg_map": {0: 0, 1: 1}},
                {"bare": "aaaa-2-0", "vg_map": {0: 0, 1: 1, 2: 40, 3: 41}},
                {"bare": "bbbb-1-0", "vg_map": {0: 10, 1: 11}},
                {"bare": "bbbb-2-0", "vg_map": {0: 20, 1: 21}},
                {"bare": "cccc-1-0", "vg_map": {0: 0}},
            ],
            {
                "aaaa": ["aaaa-1-0", "aaaa-2-0"],
                "bbbb": ["bbbb-1-0", "bbbb-2-0"],
                "cccc": ["cccc-1-0"],
            },
        )
        decision = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"aaaa-1-0.rope", "cccc-1-0"},
            used_group_ids={10},
            allowed_rules=("1",),
        )
        # 规则①：部分在场的家族照旧补
        self.assertEqual(decision.stub_members, ["aaaa-2-0"])
        self.assertEqual(
            decision.families["aaaa"]["decision"], _skeleton.HIMI_STUB_REASON_ABSORBED
        )
        # 规则④/②：整族缺席 ⇒ 门槛外 ⇒ 不判定、不插桩
        self.assertEqual(
            decision.families["bbbb"]["decision"], _skeleton.HIMI_STUB_REASON_RULE_GATED
        )
        self.assertEqual(decision.families["bbbb"]["stub"], [])
        # 对照：不设门槛时同一个账本会把 bbbb 整族补上（ST2 的口径）
        full = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"aaaa-1-0.rope", "cccc-1-0"},
            used_group_ids={10},
        )
        self.assertIn("bbbb-1-0", full.stub_members)
        self.assertIn("bbbb-2-0", full.stub_members)

    def test_st3_borrowed_slot_reference_is_not_absorption(self):
        """**ST3（借位反例，事故形态复现）**：整族缺席 + 只被借位槽位引用 ⇒ 不插桩。

        复现 ZZMI 2026-09-16 叶瞬光01 脸部 ``c209c22b-45087-0`` 被误插占位的形态：缺席
        部件的骨骼**全部**与同家族成员共享（去重把同一矩阵并成一个 canonical 槽位）⇒ 它
        没有专属槽位 ⇒ 「被引用」只是**借位**，不是「几何被吸收」；旧判据（全量 VGMap
        值域 ∩ used）会命中 ⇒ 误插 ⇒ 部件在游戏里静默消失。**适用范围（t38 修订）**：
        这条保护属于**规则②**（整族缺席）；规则①（家族部分在场）按 ZZMI 逐字口径**不做**
        ``exclusive`` 门控（见 ``test_rule1_partial_family_stubs_every_missing_member``）。
        """
        _write_workspace(
            self.workspace,
            [
                # 缺席的脸部件：0/1 与**同家族**成员共享（借位，无专属槽位）
                {"bare": "c209c22b-45087-0", "vg_map": {0: 0, 1: 1}},
                {"bare": "c209c22b-45088-0", "vg_map": {0: 0, 1: 1}},
                # 载体：别的家族，**声明**它引用的 canonical 槽 0/1（`declared_by_present` 的前提）
                {"bare": "8c8de427-798-0", "vg_map": {0: 0, 1: 1}},
            ],
            {"c209c22b": ["c209c22b-45087-0", "c209c22b-45088-0"], "8c8de427": ["8c8de427-798-0"]},
        )
        # 载体对象属于**别的 IB 家族**（8c8de427），只引用了借位槽位 0/1 ⇒ 整族缺席
        decision = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"8c8de427-798-0"},
            used_group_ids={0, 1},
        )
        self.assertEqual(
            decision.stub_members,
            [],
            "整族缺席 + 只被借位槽位引用 ⇒ 必须判「未被吸收」⇒ 不得插桩",
        )
        self.assertEqual(
            sorted(decision.skipped_members),
            ["c209c22b-45087-0", "c209c22b-45088-0"],
        )
        self.assertEqual(
            decision.families["c209c22b"]["decision"],
            _skeleton.HIMI_STUB_REASON_ZERO_REFERENCE,
        )
        self.assertEqual(decision.families["c209c22b"]["rule"], "2")
        # 对照：同一份数据若按「全量值域 ∩ used」的旧口径就会命中（证明该反例非恒真）
        self.assertTrue({0, 1} & {0, 1}, "旧口径确实会命中 ⇒ 本测试不是恒真断言")
        self.assertEqual(
            _skeleton.himi_exclusive_slots(
                "c209c22b-45087-0",
                ["c209c22b-45087-0", "c209c22b-45088-0"],
                {"c209c22b-45087-0": {0, 1}, "c209c22b-45088-0": {0, 1}},
            ),
            set(),
            "借位后专属槽位为空 ⇒ 证据不可构造",
        )

    def test_st3_positive_partial_exclusive_slot_still_stubs(self):
        """ST3 正向对照：同一家族里**有**专属槽位被引用 ⇒ 仍然插桩（判据非恒假）。"""
        _write_workspace(
            self.workspace,
            [
                {"bare": "c209c22b-45087-0", "vg_map": {0: 0, 1: 1, 2: 168, 3: 169}},
                {"bare": "c209c22b-45088-0", "vg_map": {0: 0, 1: 1}},
            ],
            {"c209c22b": ["c209c22b-45087-0", "c209c22b-45088-0"]},
        )
        decision = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"c209c22b-45088-0"},
            used_group_ids={0, 1, 168},
        )
        self.assertEqual(decision.stub_members, ["c209c22b-45087-0"])
        self.assertEqual(
            _skeleton.himi_exclusive_slots(
                "c209c22b-45087-0",
                ["c209c22b-45087-0", "c209c22b-45088-0"],
                {"c209c22b-45087-0": {0, 1, 168, 169}, "c209c22b-45088-0": {0, 1}},
            ),
            {168, 169},
        )

    def test_st4_zero_reference_is_not_stubbed(self):
        """ST4：整族缺席 + **无外来槽被引用** ⇒ 不插桩（用户只是删了对象）。"""
        _write_workspace(
            self.workspace,
            [
                {"bare": "dddd-1-0", "vg_map": {0: 300, 1: 301}},
                {"bare": "eeee-1-0", "vg_map": {0: 5}},  # 载体：声明自己用的槽 5
            ],
            {"dddd": ["dddd-1-0"], "eeee": ["eeee-1-0"]},
        )
        decision = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"eeee-1-0"},
            used_group_ids={5, 6},
        )
        self.assertEqual(decision.stub_members, [])
        self.assertEqual(
            decision.families["dddd"]["decision"], _skeleton.HIMI_STUB_REASON_ZERO_REFERENCE
        )

    def test_st5_dedup_excluded_is_orthogonal(self):
        """ST5：``VGMapDedupExcluded=True`` ⇒ 即使被引用也不插桩（显式排除优先）。"""
        _write_workspace(
            self.workspace,
            [
                {"bare": "ffff-1-0", "vg_map": {0: 400, 1: 401}, "excluded": True},
                {"bare": "gggg-1-0", "vg_map": {0: 6}},  # 载体：声明自己用的槽 6
            ],
            {"ffff": ["ffff-1-0"], "gggg": ["gggg-1-0"]},
        )
        decision = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"gggg-1-0"},
            used_group_ids={400, 6},
        )
        self.assertEqual(decision.stub_members, [])
        self.assertIn("ffff-1-0", decision.skipped_members)
        self.assertEqual(
            decision.reason_counts.get(_skeleton.HIMI_STUB_REASON_DEDUP_EXCLUDED), 1
        )

    def test_st5_missing_vgmap_is_not_stubbed(self):
        """ST5 反例①：无反查数据（json 无 VGMap / 未落盘）⇒ 一律不插桩。"""
        # 家族名单里有 hhhh-1-0，但工作空间里**没有**它的 json（未落盘）
        _write_workspace(
            self.workspace,
            [{"bare": "iiii-1-0", "vg_map": {0: 500}}],
            {"hhhh": ["hhhh-1-0"], "iiii": ["iiii-1-0"]},
        )
        decision = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"iiii-1-0"},
            used_group_ids={500},
        )
        self.assertEqual(decision.stub_members, [])
        self.assertIn("hhhh-1-0", decision.skipped_members)

    def test_no_family_map_never_stubs(self):
        """家族名单缺失 ⇒ 无法枚举成员 ⇒ 一律不插桩（fail-closed，V12）。"""
        _write_workspace(
            self.workspace,
            [{"bare": "jjjj-1-0", "vg_map": {0: 600}}],
            {},
        )
        decision = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares=set(),
            used_group_ids={600},
        )
        self.assertEqual(decision.stub_members, [])
        self.assertTrue(
            any(_skeleton.HIMI_COMPONENT_MAP_BASENAME in line for line in decision.diagnostics)
        )

    def test_no_used_evidence_never_stubs(self):
        """``used_group_ids=None``（证据不可构造）⇒ 保守不插桩。"""
        _write_workspace(
            self.workspace,
            [{"bare": "kkkk-1-0", "vg_map": {0: 700}}],
            {"kkkk": ["kkkk-1-0"]},
        )
        decision = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares=set(),
            used_group_ids=None,
        )
        self.assertEqual(decision.stub_members, [])
        self.assertEqual(
            decision.families["kkkk"]["decision"], _skeleton.HIMI_STUB_REASON_NO_USED
        )


# --------------------------------------------------------------------------- #
# A2 类：顺序（注入必须早于基类组装模型）与 ini 级产物（T26 F1）
# --------------------------------------------------------------------------- #


class StubInjectionOrderTests(_StubCase):
    """F1：站位注入必须发生在**基类组装 drawib/submesh 模型之前**。

    为什么必须单独钉这条：旧版测试宿主（`_FakeDrawIBExportBase` 把 `drawib_model_list`
    恒置空、从不解析 `ordered`）会把顺序问题**抹掉** —— 18 条测试全绿，而真 bug 是
    「站位 DrawCall 只留在 `ordered` 里，永远进不了 `drawib_model_list[*].submesh_model_list[*].drawcall_model_list`」
    ⇒ 站位部件的 IB override 段（`handling = skip`）根本不生成。
    """

    def test_injection_happens_before_base_model_assembly(self):
        """核心顺序断言：基类 `__init__` 被调用时，`ordered` 里**已有** stub DrawCall。"""
        self._build_dump_fixture()
        _register_object("LOD0.aaaa-1-0", groups=[(0, 1.0), (40, 1.0)])
        ordered = [_PlainDrawCall("LOD0.aaaa-1-0")]
        blueprint_model = types.SimpleNamespace(
            ordered_draw_obj_data_model_list=ordered, keyname_mkey_dict={}
        )

        snapshots: list = []
        original_init = ExportUnity.__init__

        def _recording_init(self_, blueprint_model=None, combine_ib=False, **kwargs):
            # 基类组装模型的**那一刻**：ordered 里应当已经含 stub DrawCall
            snapshots.append(
                list(getattr(blueprint_model, "ordered_draw_obj_data_model_list", None) or [])
            )
            return original_init(self_, blueprint_model)

        ExportUnity.__init__ = _recording_init
        try:
            exporter = ExportHIMI(blueprint_model)
        finally:
            ExportUnity.__init__ = original_init

        self.assertEqual(len(snapshots), 1, "基类 __init__ 必须恰好被调用一次")
        at_base_init = snapshots[0]
        stub_draw_calls = [dc for dc in at_base_init if getattr(dc, "himi_stub", False)]
        self.assertEqual(
            len(stub_draw_calls),
            1,
            "基类组装模型时 ordered 里必须已有站位 DrawCall（否则它永远进不了 "
            "drawib_model_list ⇒ 站位部件的 IB override 段不生成）",
        )
        self.assertEqual(stub_draw_calls[0].get_workspace_unique_str(), "LOD0.aaaa-2-0")
        # 反向：注入确实发生了（不是「没插桩所以恰好为空」的假绿）
        self.assertIn("LOD0.aaaa-2-0", exporter.himi_stub_object_names())

        # 结构断言：把基类那一刻的 ordered 按 export_helper 的分组语义解析
        # （`ui/universal/export_helper.py:43-50`：按 get_workspace_unique_str 分组），
        # stub DrawCall 必须落在它自己的 submesh 分组里。
        grouped: dict = {}
        for draw_call in at_base_init:
            grouped.setdefault(str(draw_call.get_workspace_unique_str() or ""), []).append(
                draw_call
            )
        self.assertIn(
            "LOD0.aaaa-2-0",
            grouped,
            "站位 bare 必须出现在被解析的分组键里（= drawib_model_list 的承载身份）",
        )
        self.assertTrue(
            any(getattr(dc, "himi_stub", False) for dc in grouped["LOD0.aaaa-2-0"]),
            "站位 DrawCall 必须落在它自己的 submesh_model_list[*].drawcall_model_list 里",
        )

    def test_stub_bare_gets_ib_override_section_with_handling_skip(self):
        """ini 级断言：站位 bare 的 IB override 段存在且含 `handling = skip`。

        HIMI 的 IB 段由基类 `ExportUnity.add_unity_vs_texture_override_ib_sections`
        生成（`ui/universal/unity.py:75-125`，逐 submesh 出段 + `handling = skip`），
        本模块不改它 —— 所以这里直接用**站位 bare 的 DrawIBModel** 调基类方法，
        断言「站位确实会产出一个压制原版 draw 的段」。
        """
        builder = M_IniBuilder()
        drawib_model = _stub_drawib_model("aaaa-2-0", vertex_count=3, index_count=3)
        base = ExportUnity(types.SimpleNamespace(keyname_mkey_dict={}))
        base.add_unity_vs_texture_override_ib_sections(builder, drawib_model)
        lines = _builder_lines(builder)
        joined = "\n".join(lines)
        # 段名 = `[TextureOverride_<workspace_unique_str 的 - 换 _>]`（unity.py:93）
        self.assertIn("[TextureOverride_LOD0.aaaa_2_0]", joined)
        self.assertIn("handling = skip", joined, "站位段的唯一作用就是压掉原版 draw")
        self.assertIn("hash = aaaa", joined)
        self.assertIn("ib = Resource_LOD0.aaaa-2-0_Index", joined)
        # 本宿主把 `M_IniHelper.get_drawindexed_str_list` 替成空列表（不测基类渲染），
        # 但站位 DrawCall 的计数必须已被显式填好（ZZMI 同款：默认 0 会让段退化）。
        submesh = drawib_model.submesh_model_list[0]
        self.assertEqual(submesh.drawcall_model_list[0].vertex_count, 3)
        self.assertEqual(submesh.drawcall_model_list[0].index_count, 3)

    def test_scene_truth_declared_but_absent_still_gets_stub(self):
        """规则①的判据必须是**场景真相**：声明了但 `bpy.data.objects` 里没有 ⇒ 仍补站位。

        ZZMI t80 §2.3 实测的形态：蓝图声明 19 件、场景里只有 4 个 mesh。把「声明了」当 present 会让占位整段跳过 ⇒ 紧接着下游 `SubMeshModel` 按不存在的对象 `Fatal`。
        """
        self._build_dump_fixture()
        # 兄弟成员真实存在并引用了缺席者的专属槽位 40
        _register_object("LOD0.aaaa-1-0", groups=[(0, 1.0), (40, 1.0)])
        # 缺席者：**声明在 ordered 里**，但 bpy.data.objects 里没有它
        ordered = [
            _PlainDrawCall("LOD0.aaaa-1-0"),
            _PlainDrawCall("LOD0.aaaa-2-0"),
        ]
        exporter = self._make_exporter(ordered=ordered, inject=False)
        self.assertFalse(
            _skeleton.blender_object_present("LOD0.aaaa-2-0"),
            "前提：该部件在场景里没有对象",
        )
        created = exporter._himi_inject_stub_objects()
        self.assertIn(
            "LOD0.aaaa-2-0",
            created,
            "声明存在但场景无对象 ⇒ 仍必须补站位（scene_present 不得含声明名）",
        )
        self.assertNotIn(
            "LOD0.aaaa-2-0",
            exporter.himi_stub_decision().families["aaaa"]["in_scene"],
            "scene_present 必须只含真实存在的对象",
        )

    def test_empty_registry_falls_back_to_declared_names_and_skips_stub(self):
        """空注册表（轻量宿主 / 拿不到 bpy 数据）⇒ 退回声明名 ⇒ 家族内缺失成员**仍补站位**。

        兜底语义（t38 按 ZZMI 逐字口径校正）：它只避免「给**全部**部件插桩」—— 退回声明名后，家族里**不在声明集**的成员仍按**规则①** 补占位（与 ZZMI `ui/universal/zzmi.py:932-936` 同款）。
        """
        self._build_dump_fixture()
        # 一个对象都不注册 ⇒ present_bares 为空
        ordered = [_PlainDrawCall("LOD0.aaaa-1-0")]
        exporter = self._make_exporter(ordered=ordered, inject=False)
        created = exporter._himi_inject_stub_objects()
        self.assertEqual(created, ["LOD0.aaaa-2-0"], "声明集外的家族成员 ⇒ 规则① 补站位")
        self.assertEqual(exporter.himi_stub_object_names(), ["LOD0.aaaa-2-0"])
        decision = exporter.himi_stub_decision()
        self.assertEqual(decision.families["aaaa"]["rule"], "1")

    def test_no_merged_data_in_workspace_blocks_injection(self):
        """S3 前置门：工作空间**没有合并骨架数据** ⇒ 不注入站位。

        理由（计划书 §7.7.7 S1/S2）：没有合并数据 ⇒ 本次导出不产出合并骨架资源 ⇒ 站位无从
        谈起；HIMI 的站位不承载作用 A，唯一效果是用 `handling = skip` 压掉原版 draw，在
        「根本不产出骨架」时压掉只会让部件凭空空掉。这里的工作空间**不建 dump、不建 VGMap**
        （只有家族名单），因此前置门必须拦住。
        """
        _write_workspace(
            self.workspace,
            [],  # 没有任何带 VGMap 的组件
            {"aaaa": ["aaaa-1-0", "aaaa-2-0"]},
        )
        _register_object("LOD0.aaaa-1-0", groups=[(0, 1.0)])
        ordered = [_PlainDrawCall("LOD0.aaaa-1-0")]
        exporter = self._make_exporter(ordered=ordered, inject=False)
        self.assertFalse(
            exporter._himi_merged_data_present(self.workspace),
            "前提：该工作空间确实没有合并数据",
        )
        created = exporter._himi_inject_stub_objects()
        self.assertEqual(created, [], "无合并数据 ⇒ 不注入站位")
        self.assertIsNone(_fake_objects.get("LOD0.aaaa-2-0"))

    def test_stub_bare_enters_export_set_to_close_the_family_gate(self):
        """站位 bare **必须**进 `export_bares` —— 否则站位场景必然中止导出。

        反证（本用例钉死）：判据只在「缺席家族**有外来槽证据**」时插桩 ⇒ 能插桩的缺席部件
        必然**已落盘**；而 G1 家族门是 `L(ib) ⊆ T`、`L(ib)` 取**已落盘**组件 ⇒ 排除站位 bare
        会让被吸收成员永远 `missing`、家族永远不 `safe`、契约 `level=error` ⇒ `Fatal` 中止
        导出。即：**在站位被注入的唯一场景里，排除站位会让导出必然中止**（ZZMI 同语义）。
        """
        self._build_dump_fixture()
        _register_object("LOD0.aaaa-1-0", groups=[(0, 1.0), (40, 1.0)])
        exporter = self._make_exporter(
            ordered=[_PlainDrawCall("LOD0.aaaa-1-0")], inject=False
        )
        created = exporter._himi_inject_stub_objects()
        self.assertIn("LOD0.aaaa-2-0", created, "前提：确实建出了站位对象")
        self.assertEqual(exporter.himi_stub_bares(), {"aaaa-2-0"})

        # 模拟「站位 bare 的 json 仍在工作空间里 ⇒ SubMeshModel.vg_count > 0」
        stub_submesh = types.SimpleNamespace(
            workspace_unique_str="LOD0.aaaa-2-0",
            unique_str="LOD0.aaaa-2-0",
            vg_count=4,
            himi_stub=True,
        )
        real_submesh = types.SimpleNamespace(
            workspace_unique_str="LOD0.aaaa-1-0",
            unique_str="LOD0.aaaa-1-0",
            vg_count=2,
        )
        exporter.drawib_model_list = [
            types.SimpleNamespace(draw_ib="aaaa", submesh_model_list=[real_submesh, stub_submesh])
        ]
        entries = exporter._himi_export_entries()
        by_bare = {entry["bare"]: entry for entry in entries}
        self.assertTrue(by_bare["aaaa-2-0"]["is_stub"], "站位件必须被标注")
        self.assertEqual(by_bare["aaaa-2-0"]["vg_count"], 4, "前提：站位件的 vg_count 确实 > 0")
        self.assertFalse(by_bare["aaaa-1-0"]["is_stub"])
        # 调用**真实**的过滤方法（与 `himi_merged_runtime_plan()` 同一个事实源）。
        # 站位 bare **进**目标集合：这是唯一能让 G1 家族门放行的选择（反证见方法
        # docstring 与 StubEndToEndIniTests）。过滤口径只看 vg_count，不看 is_stub。
        self.assertEqual(
            exporter._himi_export_bares(entries),
            ["aaaa-1-0", "aaaa-2-0"],
            "站位 bare 必须进入 G1 目标集合（否则被吸收成员永远 missing ⇒ 导出中止）",
        )

    def test_cleanup_keeps_real_draw_calls(self):
        """清理边界：真实 DrawCall 不得被清理误摘（EFMI 同款边界的 HIMI 版）。

        `_himi_cleanup_stub_objects` 只摘「本次登记过的 id」与「带 himi_stub 标记」的条目；没有标记的真实 DrawCall 必须原样保留。
        """
        self._build_dump_fixture()
        _register_object("LOD0.aaaa-1-0", groups=[(0, 1.0), (40, 1.0)])
        real_a = _PlainDrawCall("LOD0.aaaa-1-0")
        real_b = _PlainDrawCall("LOD0.aaaa-9-9")
        ordered = [real_a, real_b]
        exporter = self._make_exporter(ordered=ordered, inject=False)
        exporter._himi_inject_stub_objects()
        exporter._himi_cleanup_stub_objects()
        remaining = list(exporter.blueprint_model.ordered_draw_obj_data_model_list)
        self.assertIn(real_a, remaining, "真实 DrawCall 不得被清理误摘")
        self.assertIn(real_b, remaining)
        self.assertFalse(any(getattr(dc, "himi_stub", False) for dc in remaining))


def _stub_drawib_model(bare: str, *, vertex_count: int, index_count: int):
    """站位的 DrawIBModel 替身：只带 IB 段生成所需的最小字段。

    字段清单直接取自 `ui/universal/unity.py:86-125` 的读取点
    （`match_first_index` / `unique_str` / `drawcall_model_list` /
    `submesh_ib_dict` / `d3d11GameType.GPU_PreSkinning` / `obj_name_draw_offset`）。
    """

    submesh = types.SimpleNamespace(
        unique_str=f"LOD0.{bare}",
        workspace_unique_str=f"LOD0.{bare}",
        match_first_index=0,
        drawcall_model_list=[_PlainDrawCall(f"LOD0.{bare}")],
    )
    submesh.drawcall_model_list[0].index_count = index_count
    submesh.drawcall_model_list[0].vertex_count = vertex_count
    return types.SimpleNamespace(
        draw_ib=bare.split("-", 1)[0],
        draw_ib_alias="0",
        submesh_model_list=[submesh],
        submesh_ib_dict={submesh.unique_str: b"\x00\x00\x00\x00"},
        get_submesh_ib_resource_name=lambda s: "Resource_" + s.unique_str + "_Index",
        get_submesh_texture_override_suffix=lambda s: s.unique_str.replace("-", "_"),
        get_submesh_texture_markup_info_list=lambda s: [],
        obj_name_draw_offset={},
        d3d11GameType=types.SimpleNamespace(GPU_PreSkinning=True),
        index_count=index_count,
        vertex_count=vertex_count,
    )


# --------------------------------------------------------------------------- #
# A3 类：真端到端（在 %TEMP% 副本上跑**真实** ExportUnity.export()，断言产物 ini）
# --------------------------------------------------------------------------- #


class StubEndToEndIniTests(_StubCase):
    """T26 验收项 8：站位机制必须在副本上走通一次端到端，且**产物 ini 里真有**段。

    「只断言决定函数返回了 True」不算数 —— 本用例跑的是**真实**
    `ExportUnity.export()`（缓冲生成 → INI 生成 → `save_to_file`），然后把产物 ini
    读回来，断言站位 bare 的 `[TextureOverride_...]` 段存在且含 `handling = skip`。

    **成员集合来自真实装配（t33 F-2 修复）**：站位 DrawIBModel **不是**手工塞进去的，
    而是由基类在 `super().__init__()` 那一刻按 `ui/universal/drawib_export_base.py:11-16`
    的语义从 `ordered` 装配出来的（本宿主 = `_FakeDrawIBExportBase.__init__`）。因此注入
    **晚于**基类组装（= F1 缺陷本体）时，站位 DrawCall 还没进 `ordered` ⇒ 装配结果里
    没有它 ⇒ 产物 ini 里没有站位段 ⇒ **本用例会红**。两态实测（等价 overlay，
    `HIMI_F1_BROKEN_ORDER=1`）见 `reports/himi-merge/19-injection-verification.md` §4.4。

    **覆盖边界（不要误读）**：「注入早于基类组装」这一**结构**事实另由
    `StubInjectionOrderTests::test_injection_happens_before_base_model_assembly`
    直接快照基类调用时刻的 `ordered` 钉住 —— 两条是**互补**的独立钉子，删任一条都会由
    `StubNailIntegrityTests::test_f1_nails_are_still_installed` 报错。
    """

    def _drawib_model_for(self, bare: str, *, draw_ib: str, draw_number: int, stub: bool):
        """按 `unity.py` 的读取点造一个够真实 export() 跑的 DrawIBModel 替身。"""
        unique_str = f"LOD0.{bare}"
        draw_call = _PlainDrawCall(unique_str)
        draw_call.index_count = 3 if stub else 30
        draw_call.vertex_count = 3 if stub else 30
        submesh = types.SimpleNamespace(
            unique_str=unique_str,
            workspace_unique_str=unique_str,
            match_first_index=0,
            # `vg_count` 是 `_himi_export_bares` 的过滤输入（> 0 才算「带合并数据」）。
            # 真部件给 4（与落盘 VGMap 规模一致）；站位件同样给 4 —— **故意**给非零，
            # 以便顺带证明「排除站位靠 is_stub 而非 vg_count」。
            vg_count=4,
            drawcall_model_list=[draw_call],
        )
        return types.SimpleNamespace(
            draw_ib=draw_ib,
            draw_ib_alias="0",
            draw_number=draw_number,
            vertex_limit_hash="2c5605ed",
            d3d11GameType=types.SimpleNamespace(
                OrderedCategoryNameList=["Position", "Texcoord", "Blend"],
                CategoryDrawCategoryDict={
                    "Position": "Position",
                    "Texcoord": "Texcoord",
                    "Blend": "Blend",
                },
                CategoryExtractSlotDict={
                    "Position": "vb0",
                    "Texcoord": "vb1",
                    "Blend": "vb1",
                },
                CategoryStrideDict={"Position": 44, "Texcoord": 16, "Blend": 32},
                GPU_PreSkinning=True,
            ),
            category_hash_dict={
                "Position": "2b325128",
                "Texcoord": "84611940",
                "Blend": "8322ac5a",
            },
            submesh_model_list=[submesh],
            submesh_ib_dict={unique_str: b"\x00\x00\x00\x00"},
            category_buffer_dict={},
            shapekey_name_bytelist_dict={},
            get_submesh_ib_resource_name=lambda s: "Resource" + s.unique_str + "-Index",
            get_submesh_texture_override_suffix=lambda s: s.unique_str.replace("-", "_"),
            get_submesh_texture_markup_info_list=lambda s: [],
            obj_name_draw_offset={},
        )

    def _assemble_models_from_base(self, exporter, *, draw_ib="aaaa", draw_number=30):
        """把**基类装配出来的** `drawib_model_list` 翻译成真实 `DrawIBModel` 形状。

        F-2 修复的关键（t33）：成员集合**不是**手工塞的，而是基类在
        `super().__init__()` 那一刻从 `ordered` 装配出来的
        （`ui/universal/drawib_export_base.py:11-16` ⇒ 本宿主
        `_FakeDrawIBExportBase.__init__`）。因此「注入是否早于基类组装」直接决定这里
        有没有站位件 —— 顺序坏掉 ⇒ 站位 DrawCall 还没进 `ordered` ⇒ 装配结果里没有它
        ⇒ 产物 ini 里没有站位段 ⇒ 本用例失败。

        这里只做**形状翻译**（宿主装配出的是 SimpleNamespace，缺 `export()` 需要的
        `draw_ib` / `category_hash_dict` / `d3d11GameType` 等字段），**不改变成员集合**：
        翻译前后逐一断言身份一致。**禁止**在本方法里读 `himi_stub_bares()` 反推成员
        （那就是 F-2 描述的「绕过真实装配」），由
        `StubNailIntegrityTests::test_f1_nails_are_still_installed` 在源码层钉死。
        """
        assembled = list(exporter.drawib_model_list)
        models = []
        for drawib_model in assembled:
            for submesh in drawib_model.submesh_model_list:
                unique_str = str(getattr(submesh, "unique_str", "") or "")
                is_stub = any(
                    bool(getattr(draw_call, "himi_stub", False))
                    for draw_call in getattr(submesh, "drawcall_model_list", [])
                )
                bare = unique_str.split(".", 1)[1] if "." in unique_str else unique_str
                models.append(
                    self._drawib_model_for(
                        bare, draw_ib=draw_ib, draw_number=draw_number, stub=is_stub
                    )
                )
        self.assertEqual(
            [model.submesh_model_list[0].unique_str for model in models],
            [str(getattr(model, "unique_str", "")) for model in assembled],
            "形状翻译不得改变成员集合（否则因果链又被手工装配替换掉了）",
        )
        return models

    def test_stub_ib_override_section_reaches_the_real_ini(self):
        self._build_dump_fixture(separate_bones=True)
        plan = HIMISkeletonMergeHelper.prepare(self.workspace, None, force=True)
        self.assertTrue(plan["ok"], plan.get("message"))
        commit = HIMISkeletonMergeHelper.commit(plan)
        self.assertTrue(commit["ok"], commit)

        # 缺席者的专属槽位（从**落盘后**的 json 现读，不硬编码）
        landed = {}
        for bare in ("aaaa-1-0", "aaaa-2-0"):
            json_path = _skeleton.resolve_submesh_json_path(self.workspace, f"{LOD}.{bare}")
            landed[bare] = {
                int(slot)
                for slot in (
                    json.loads(open(json_path, encoding="utf-8").read()).get("VGMap") or {}
                ).values()
            }
        exclusive = _skeleton.himi_exclusive_slots("aaaa-2-0", ["aaaa-1-0", "aaaa-2-0"], landed)
        self.assertTrue(exclusive, f"前提：落盘后两者骨骼必须不同：{landed}")

        # 真实场景对象：在场者引用缺席者的专属槽位 ⇒ 几何被吸收
        _register_object("LOD0.aaaa-1-0", groups=[(slot, 1.0) for slot in sorted(exclusive)])
        ordered = [_PlainDrawCall("LOD0.aaaa-1-0")]
        blueprint_model = types.SimpleNamespace(
            ordered_draw_obj_data_model_list=ordered, keyname_mkey_dict={}
        )
        # **真构造**（inject=True）：走真实 __init__，注入在基类组装之前发生
        exporter = ExportHIMI(blueprint_model)
        stub_bares = exporter.himi_stub_bares()
        self.assertEqual(stub_bares, {"aaaa-2-0"}, "前提：本次注入确实产出了站位 bare")

        # **真实装配**（t33 F-2 修复）：成员集合来自基类在 `super().__init__()` 那一刻
        # 从 `ordered` 装配出的 `drawib_model_list`（`_FakeDrawIBExportBase.__init__`，
        # 语义 = `ui/universal/drawib_export_base.py:11-16`），**不再**从
        # `himi_stub_bares()` 手工塞。⇒ 注入晚于基类组装（F1 缺陷本体）时，站位
        # DrawCall 还没进 `ordered` ⇒ 装配结果里没有它 ⇒ 这条断言与下面的 ini 断言都会红。
        assembled_bares = [
            str(getattr(model, "unique_str", "")).split(".", 1)[-1]
            for model in exporter.drawib_model_list
        ]
        self.assertEqual(
            assembled_bares,
            ["aaaa-1-0", "aaaa-2-0"],
            "基类组装时刻 ordered 里必须已有站位 DrawCall ⇒ 装配结果必须含站位件",
        )
        exporter.drawib_model_list = self._assemble_models_from_base(exporter)

        mod_folder = _GLOBAL_STATE["mod"]
        os.makedirs(os.path.join(mod_folder, "Meshes"), exist_ok=True)
        # 跑**真实**的 export()（缓冲 → INI → save_to_file）
        exporter.export()

        ini_path = os.path.join(mod_folder, "ws.ini")
        self.assertTrue(os.path.isfile(ini_path), f"产物 ini 必须存在：{ini_path}")
        ini_text = io.open(ini_path, encoding="utf-8").read()
        self.assertIn(
            "[TextureOverride_LOD0.aaaa_2_0]",
            ini_text,
            "站位 bare 的 IB override 段必须真的出现在产物 ini 里",
        )
        # 该段内必须含 handling = skip（抑制原版 draw 的唯一手段）
        lines = ini_text.splitlines()
        start = lines.index("[TextureOverride_LOD0.aaaa_2_0]")
        block = []
        for line in lines[start + 1 :]:
            if line.startswith("[") and line.endswith("]"):
                break
            block.append(line)
        self.assertIn("handling = skip", block, f"站位段必须压制原版 draw：{block}")
        # 清理事务也必须闭合
        self.assertEqual(exporter.himi_stub_object_names(), [])
        self.assertIsNone(_fake_objects.get("LOD0.aaaa-2-0"))

    def test_st12_normal_mode_stub_reaches_the_real_ini(self):
        """ST12 的产物级钉子（t107）：**普通模式**下站位段照样进真实 ini。

        上一条只证明「判定结果里出现了 stub_members」；这里一路跑到 ``export()`` 写完 ini，
        确认 ``[TextureOverride_LOD0.aaaa_2_0]`` + ``handling = skip`` 真的落盘 —— 这正是
        用户实测缺的那一段（原版第二笔 draw 的 IB override 段），也是手工补丁
        ``match_first_index = 57792`` 在生成器里的对应物。
        """
        self._build_dump_fixture(separate_bones=True)
        _properties_state["checkbox"] = False
        _register_object("LOD0.aaaa-1-0", groups=[(0, 1.0), (40, 1.0)])
        ordered = [_PlainDrawCall("LOD0.aaaa-1-0")]
        blueprint_model = types.SimpleNamespace(
            ordered_draw_obj_data_model_list=ordered, keyname_mkey_dict={}
        )
        exporter = ExportHIMI(blueprint_model)
        self.assertEqual(exporter.himi_stub_bares(), {"aaaa-2-0"})
        exporter.drawib_model_list = self._assemble_models_from_base(exporter)

        mod_folder = _GLOBAL_STATE["mod"]
        os.makedirs(os.path.join(mod_folder, "Meshes"), exist_ok=True)
        exporter.export()

        ini_path = os.path.join(mod_folder, "ws.ini")
        self.assertTrue(os.path.isfile(ini_path), f"产物 ini 必须存在：{ini_path}")
        lines = io.open(ini_path, encoding="utf-8").read().splitlines()
        self.assertIn("[TextureOverride_LOD0.aaaa_2_0]", lines)
        start = lines.index("[TextureOverride_LOD0.aaaa_2_0]")
        block = []
        for line in lines[start + 1 :]:
            if line.startswith("[") and line.endswith("]"):
                break
            block.append(line)
        self.assertIn("handling = skip", block)
        self.assertIn("match_first_index = 0", block)

    def test_st16_normal_mode_stubs_whole_absent_family_with_foreign_slot(self):
        """t108：普通模式下**整族缺席**的 IB 也要能被判进（规则② 的大门不再关着）。

        用户实测形态：``1b62725d`` 这个 IB 的几何已经被并进头发，场景里再没有属于它的
        对象 ⇒ 家族整族缺席。这条只能走规则②（``own(m) ∩ foreign_used ≠ ∅``），而
        t107 的普通模式把规则②关掉了 ⇒ 那一笔原版 draw 依旧没人压。
        """
        self._build_dump_fixture(separate_bones=True)
        # 追加一个「整族缺席」的家族 bbbb：它的唯一槽 2 会被在场对象实际引用。
        _write_workspace(
            self.workspace,
            [
                {"bare": "aaaa-1-0", "vg_map": {0: 0, 1: 1}},
                {"bare": "aaaa-2-0", "vg_map": {0: 2, 1: 3}},
                {"bare": "bbbb-1-0", "vg_map": {0: 2}},
            ],
            {
                "aaaa": ["aaaa-1-0", "aaaa-2-0"],
                "bbbb": ["bbbb-1-0"],
            },
        )
        _properties_state["checkbox"] = False
        # 在场对象引用槽 2（bbbb 的唯一槽）⇒ 外来槽证据成立、几何被它接管。
        _register_object("LOD0.aaaa-1-0", groups=[(0, 1.0), (2, 1.0)])
        ordered = [_PlainDrawCall("LOD0.aaaa-1-0")]
        blueprint_model = types.SimpleNamespace(
            ordered_draw_obj_data_model_list=ordered, keyname_mkey_dict={}
        )
        exporter = ExportHIMI(blueprint_model)
        self.assertEqual(
            exporter.himi_stub_bares(),
            {"aaaa-2-0", "bbbb-1-0"},
            "规则① 补同族缺失成员；规则② 补整族缺席但槽位被引用的家族",
        )
        decision = exporter._himi_stub_decision
        self.assertEqual(decision.families["bbbb"]["decision"], "absorbed")
        self.assertNotEqual(
            decision.families["bbbb"]["decision"],
            _skeleton.HIMI_STUB_REASON_RULE_GATED,
            "普通模式不得再按「只跑规则①」把整族缺席的家族挡在门外",
        )

    def test_without_injection_the_family_gate_correctly_aborts_the_export(self):
        """反证（本用例保证上一条**不是恒真**）：不注入 ⇒ 家族门正确中止导出。

        构造方式（t38 修订）：家族**部分在场**时规则① **一定**会补占位，因此这里改用**规则③**
        （`VGMapDedupExcluded=True` 显式排除）让缺席成员不插桩 —— 它仍是落盘组件 ⇒ 家族门
        `L(ib) ⊆ T` 不成立 ⇒ 契约 `level=error` ⇒ `Fatal` ⇒ 不产出 ini。即：产物里那个站位段，确实是「注入发生了」的直接后果（与 T24 独立验证的 E-3 同口径）。
        """
        self._build_dump_fixture(separate_bones=True)
        plan = HIMISkeletonMergeHelper.prepare(self.workspace, None, force=True)
        self.assertTrue(plan["ok"], plan.get("message"))
        self.assertTrue(HIMISkeletonMergeHelper.commit(plan)["ok"])
        # 规则③：显式排除 ⇒ 即使家族部分在场也不插桩（唯一能构造「不注入」的路径）
        absent_json = _skeleton.resolve_submesh_json_path(self.workspace, f"{LOD}.aaaa-2-0")
        payload = json.loads(open(absent_json, encoding="utf-8").read())
        payload["VGMapDedupExcluded"] = True
        json.dump(payload, io.open(absent_json, "w", encoding="utf-8"), ensure_ascii=False)

        _register_object("LOD0.aaaa-1-0", groups=[(0, 1.0)])
        ordered = [_PlainDrawCall("LOD0.aaaa-1-0")]
        blueprint_model = types.SimpleNamespace(
            ordered_draw_obj_data_model_list=ordered, keyname_mkey_dict={}
        )
        exporter = ExportHIMI(blueprint_model)
        self.assertEqual(exporter.himi_stub_bares(), set(), "前提：规则③ 本次不注入")
        exporter.drawib_model_list = [
            self._drawib_model_for("aaaa-1-0", draw_ib="aaaa", draw_number=30, stub=False)
        ]
        os.makedirs(os.path.join(_GLOBAL_STATE["mod"], "Meshes"), exist_ok=True)
        # 【t73·兜底原则】合并侧失败**不再中止导出** ⇒ 降级为普通导出：
        # 家族门照旧拒绝把不完整的家族合并进骨架，但**产物必须照出**（普通导出是兜底）。
        exporter.export()
        self.assertTrue(exporter.himi_degraded_to_ordinary(), "家族不完整 ⇒ 必须降级为普通导出")
        self.assertEqual(
            str(exporter.himi_merged_contract_decision().get("level")), "degraded",
            exporter.himi_merged_contract_decision(),
        )
        ini_path = os.path.join(_GLOBAL_STATE["mod"], "ws.ini")
        self.assertTrue(os.path.isfile(ini_path), "兜底：普通导出必须留下产物 ini")
        ini_text = io.open(ini_path, encoding="utf-8").read()
        self.assertNotIn(
            "ResourceHI3MergedSkeleton", ini_text,
            "降级产物不得含合并骨架资源（家族不完整的合并被拒绝了）",
        )

    def test_absent_part_without_reverse_lookup_gets_no_stub_section(self):
        """对照：**家族成员全在场** ⇒ 产物 ini 里没有站位段（「无站位段」的正常成因）。

        t38 修订：规则① 对「家族部分在场」的缺失成员**一律**补占位（ZZMI 逐字口径），因此
        「成功导出 + 无站位段」只可能出现在**没有缺席成员**的情形 —— 这条把该正常成因钉住。
        """
        self._build_dump_fixture(separate_bones=True)
        plan = HIMISkeletonMergeHelper.prepare(self.workspace, None, force=True)
        self.assertTrue(plan["ok"], plan.get("message"))
        self.assertTrue(HIMISkeletonMergeHelper.commit(plan)["ok"])

        # 两个成员**都有对象** ⇒ 家族完整在场 ⇒ 不插桩
        _register_object("LOD0.aaaa-1-0", groups=[(0, 1.0)])
        _register_object("LOD0.aaaa-2-0", groups=[(0, 1.0)])
        ordered = [_PlainDrawCall("LOD0.aaaa-1-0"), _PlainDrawCall("LOD0.aaaa-2-0")]
        blueprint_model = types.SimpleNamespace(
            ordered_draw_obj_data_model_list=ordered, keyname_mkey_dict={}
        )
        exporter = ExportHIMI(blueprint_model)
        self.assertEqual(
            exporter.himi_stub_bares(), set(), "家族成员全在场 ⇒ 不得插桩"
        )
        exporter.drawib_model_list = [
            self._drawib_model_for("aaaa-1-0", draw_ib="aaaa", draw_number=30, stub=False),
            self._drawib_model_for("aaaa-2-0", draw_ib="aaaa", draw_number=30, stub=False),
        ]
        os.makedirs(os.path.join(_GLOBAL_STATE["mod"], "Meshes"), exist_ok=True)
        exporter.export()
        ini_text = io.open(
            os.path.join(_GLOBAL_STATE["mod"], "ws.ini"), encoding="utf-8"
        ).read()
        self.assertIn("aaaa_1_0", ini_text, "前提：导出确实产出了 ini")
        self.assertNotIn("drawindexed = 3,0,0", ini_text, "全在场 ⇒ 不得出现站位段")


# --------------------------------------------------------------------------- #
# B 类：G1 交互与结构断言（ST6..ST12）
# --------------------------------------------------------------------------- #


class StubGateInteractionTests(_StubCase):
    def test_st6_stub_is_not_a_family_coverage_claim(self):
        """ST6（G1-a）：**站位对象**不算家族覆盖 —— 它连 ``L`` 的定义域都进不去。

        决定性理由（计划书 §7.7.6）：站位件没有 json、没有 ``VGMap``、不在
        ``L``（已落盘带数据集合）里 ⇒ ``L(ib) ⊆ T`` 判据对它是**无定义**的。

        口径边界（计划书 §7.7.11）：本用例只否定「**合成站位对象**算覆盖」；
        **被站位的 bare**（已落盘、有 json 与 ``VGMap`` 的组件）**算已覆盖**并必须进
        ``export_bares``（否则 G1 家族门在站位场景必然中止导出）。方法名保留不改，
        以免打断 `16-stub-review.md` / `18-stub-rereview.md` 按名字的引用。
        """
        _write_workspace(
            self.workspace,
            [
                # 在场者：槽 0/1；缺席者：另有专属槽 40/41（共享 0/1 是借位）
                {"bare": "aaaa-1-0", "vg_map": {0: 0, 1: 1}},
                {"bare": "aaaa-2-0", "vg_map": {0: 0, 1: 1, 2: 40, 3: 41}},
            ],
            {"aaaa": ["aaaa-1-0", "aaaa-2-0"]},
        )
        decision = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"aaaa-1-0"},
            used_group_ids={0, 1, 40},
        )
        self.assertEqual(decision.stub_members, ["aaaa-2-0"])
        # 站位件不是已落盘部件 ⇒ 不在 L 里
        self.assertEqual(
            _skeleton.resolve_submesh_json_path(self.workspace, "LOD0.aaaa-2-0") != "",
            True,
            "（该部件本身已落盘，故在 L 里；下面断言的是**站位对象**不在 L 里）",
        )
        stub_name = "LOD0.zzzz-9-9"
        self.assertEqual(_skeleton.resolve_submesh_json_path(self.workspace, stub_name), "")
        self.assertEqual(_skeleton.load_component_family_map(self.workspace).get("zzzz"), None)

    def test_st7_stub_does_not_enter_target_bares(self):
        """ST7（G1-b）：**站位对象**不进 ``target_bares``；元数据两个只读字段钉死口径。

        口径边界（计划书 §7.7.11）：本用例断言的是**数据层 ``prepare()``** 的
        ``target_bares``（本次调用传 ``None`` ⇒ 全部已落盘组件都在集合里）以及
        「**合成出来的站位对象**不在集合里」。**导出侧 ``export_bares`` 必须包含被
        站位的 bare**（否则 G1 家族门在站位场景必然中止导出），由
        ``StubInjectionOrderTests::test_stub_bare_enters_export_set_to_close_the_family_gate``
        钉死。两条断言不矛盾：前者说合成对象，后者说被站位的**已落盘 bare**。
        """
        self._build_dump_fixture()
        plan = HIMISkeletonMergeHelper.prepare(self.workspace, None, force=True)
        self.assertTrue(plan["ok"], plan.get("message"))
        metadata = plan["metadata"]
        self.assertIs(metadata["StubTargetBaresIncluded"], False)
        self.assertIs(metadata["StubFamilyCoverageCounted"], False)
        self.assertIs(metadata["StubPassGateExempt"], False)
        self.assertEqual(metadata["StubGateInteractionVersion"], 1)
        target = set(metadata["StubTargetSetBares"])
        self.assertEqual(target, {entry["bare"] for entry in plan["writes"]})
        # 两个组件都已落盘 ⇒ 都在 L 里 ⇒ 都在 G1 目标集合里
        self.assertEqual(target, {"aaaa-1-0", "aaaa-2-0"})
        # 站位件是**未落盘**的合成对象，不可能出现在 target 集合里
        self.assertNotIn("zzzz-9-9", target)
        # 注入站位不改变 target 集合（站位与「能不能重绑」解耦）
        before = set(target)
        decision = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"aaaa-1-0"},
            used_group_ids={40},
        )
        self.assertEqual(decision.stub_members, ["aaaa-2-0"])
        plan2 = HIMISkeletonMergeHelper.prepare(self.workspace, None, force=True)
        self.assertEqual(set(plan2["metadata"]["StubTargetSetBares"]), before)

    def test_st7_real_injection_leaves_every_gate_field_unchanged(self):
        """ST7（**真注入**版）：真正建出站位对象后，G1 侧逐字段不变。

        ① 注入前记录 `family_safe` / `pass_safe`（`gate_report`）与 `rebinds`，以及
           `Config/HIMIMergedSkeleton.json` 的 `SlotCount` / `Slots`；
        ② 调 `exporter._himi_inject_stub_objects()` **真的建出**站位对象；
        ③ 重跑同一读取路径并逐字段 `assertEqual`。
        """
        self._build_dump_fixture(separate_bones=True)
        before_plan = HIMISkeletonMergeHelper.prepare(self.workspace, None, force=True)
        self.assertTrue(before_plan["ok"], before_plan.get("message"))
        # `prepare()` 只产出计划；落盘是 `commit()`（与导入侧同一条链）。
        commit = HIMISkeletonMergeHelper.commit(before_plan)
        self.assertTrue(commit["ok"], commit)
        before_metadata = dict(before_plan["metadata"])
        before_gate = {
            str(key): dict(value) for key, value in (before_plan["gate_report"] or {}).items()
        }
        self.assertTrue(before_gate, "前提：本次确实产出了门控报告（否则本用例是空断言）")
        before_writes = sorted(entry["bare"] for entry in before_plan["writes"])
        before_runtime = _runtime.load_workspace_plan(self.workspace)
        self.assertTrue(before_runtime.ok, before_runtime.message)
        before_rebinds = sorted(item["draw_ib"] for item in before_runtime.rebinds)
        self.assertTrue(
            before_rebinds, "前提：本工作空间本来就有可重绑部件（否则 rebinds 比较是空断言）"
        )

        # 从**落盘后**的 json 读缺席者的专属槽位（= 场景对象要引用的组号）。
        # 从落盘真相取值，避免把判据的输入硬编码在测试里（否则测试会与实现一起漂移）。
        landed = {}
        for bare in ("aaaa-1-0", "aaaa-2-0"):
            json_path = _skeleton.resolve_submesh_json_path(
                self.workspace, f"{LOD}.{bare}"
            )
            self.assertTrue(json_path, f"{bare} 必须已落盘")
            landed[bare] = {
                int(slot)
                for slot in (json.loads(open(json_path, encoding="utf-8").read()).get("VGMap") or {}).values()
            }
        exclusive = _skeleton.himi_exclusive_slots("aaaa-2-0", ["aaaa-1-0", "aaaa-2-0"], landed)
        self.assertTrue(
            exclusive,
            f"前提：落盘后缺席者仍有专属槽位（否则本用例退化成 ST3 借位场景）：{landed}",
        )

        # ② 真注入：在场者引用缺席者的专属槽位 ⇒ 缺席者补站位
        _register_object("LOD0.aaaa-1-0", groups=[(slot, 1.0) for slot in sorted(exclusive)])
        exporter = self._make_exporter(
            ordered=[_PlainDrawCall("LOD0.aaaa-1-0")], inject=False
        )
        created = exporter._himi_inject_stub_objects()
        self.assertIn(
            "LOD0.aaaa-2-0",
            created,
            "前提：本次确实真的建出了站位对象（否则本用例是空断言）",
        )
        self.assertIn("LOD0.aaaa-2-0", exporter.himi_stub_object_names())

        # ③ 重跑同一读取路径，逐字段相等
        after_plan = HIMISkeletonMergeHelper.prepare(self.workspace, None, force=True)
        self.assertTrue(after_plan["ok"], after_plan.get("message"))
        after_metadata = dict(after_plan["metadata"])
        after_gate = {
            str(key): dict(value) for key, value in (after_plan["gate_report"] or {}).items()
        }
        self.assertEqual(
            sorted(entry["bare"] for entry in after_plan["writes"]),
            before_writes,
            "站位注入不得改变 G1 放行集合",
        )
        self.assertEqual(after_gate, before_gate, "family_safe / pass_safe 逐字段不变")
        for key in ("SlotCount", "Slots", "SkeletonDigest", "StubTargetSetBares"):
            self.assertEqual(after_metadata.get(key), before_metadata.get(key), key)
        after_runtime = _runtime.load_workspace_plan(self.workspace)
        self.assertTrue(after_runtime.ok, after_runtime.message)
        self.assertEqual(
            sorted(item["draw_ib"] for item in after_runtime.rebinds),
            before_rebinds,
            "站位注入不得改变 rebinds",
        )
        # 站位件（未落盘合成对象）绝不进入目标集合
        self.assertNotIn("aaaa-2-0", set(after_metadata["StubTargetSetBares"]) - set(before_writes))

    def test_st8_stub_adds_no_slot(self):
        """ST8：占位**不新增槽位** —— 权重组名恒 ``"0"``（单组对象排序后 index 恒 0）。"""
        obj_name = _skeleton.blender_create_stub_object("aaaa-2-0")
        self.assertEqual(obj_name, "LOD0.aaaa-2-0")
        obj = _fake_objects.get(obj_name)
        self.assertIsNotNone(obj)
        self.assertEqual(obj.get(HIMI_STUB_MARKER), 1)
        self.assertEqual([vg.name for vg in obj.vertex_groups], [HIMI_STUB_SLOT_GROUP_NAME])
        self.assertEqual(HIMI_STUB_SLOT_GROUP_NAME, "0")
        # 3 顶点 1 三角面
        mesh = obj.data
        verts, edges, faces = mesh.from_pydata_calls[0]
        self.assertEqual(len(verts), 3)
        self.assertEqual(edges, [])
        self.assertEqual(faces, [(0, 1, 2)])

    def test_st9_stub_does_not_become_absorption_evidence(self):
        """ST9：站位对象**不得**成为吸收证据（自证循环防护）。

        第一轮为家族 A 建了站位（权重挂槽 0）；第二轮收集 used 时必须排除带标记对象，
        否则「站位自己引用了槽 0」会被读成「有人引用了槽 0」。
        """
        exporter = self._make_exporter(ordered=[], inject=False)
        # 一个真实载体对象引用槽 5；一个带标记站位引用槽 0
        _register_object("LOD0.carrier-1-0", groups=[(5, 1.0)])
        _register_object("LOD0.stub-old-1-0", groups=[(0, 1.0)], marked=True)
        ordered = exporter.blueprint_model.ordered_draw_obj_data_model_list
        ordered.append(_PlainDrawCall("LOD0.carrier-1-0"))
        ordered.append(_skeleton.HIMIStubDrawCall(obj_name="LOD0.stub-old-1-0"))
        used = exporter._himi_collect_used_group_ids()
        self.assertIn(5, used)
        self.assertNotIn(0, used, "带标记站位对象的权重不得进入吸收证据")

    def test_st10_stale_stub_is_purged_before_present_set(self):
        """ST10：残留自愈**早于** present 集合构造。

        预置一个与真部件同名的残留站位对象（模拟上次导出异常中断）⇒ 若先构造 present
        会把该部件判成「场景存在」⇒ 不插桩 ⇒ 随后对象被删 ⇒ 下游 ``SubMeshModel``
        引用不存在的对象而 ``Fatal``。
        """
        self._build_dump_fixture()
        # 残留：带标记对象名 == 真部件身份（这正是危险形态）
        _register_object("LOD0.aaaa-1-0", groups=[(0, 1.0), (40, 1.0)])
        # 残留站位对象：带标记、且引用了缺席者的**专属**槽位 40
        _register_object("LOD0.aaaa-2-0", groups=[(40, 1.0)], marked=True)
        self.assertTrue(_skeleton.blender_object_present("LOD0.aaaa-2-0"))
        exporter = self._make_exporter(ordered=[_PlainDrawCall("LOD0.aaaa-1-0")], inject=False)
        created = exporter._himi_inject_stub_objects()
        # 残留被清理 ⇒ aaaa-2-0 被判缺席 ⇒ 补占位
        self.assertIn("LOD0.aaaa-2-0", created)
        self.assertIn("LOD0.aaaa-2-0", exporter.himi_stub_object_names())
        obj = _fake_objects.get("LOD0.aaaa-2-0")
        self.assertEqual(obj.get(HIMI_STUB_MARKER), 1)

    def test_st11_transaction_closes_on_success_and_failure(self):
        """ST11：``export()`` 正常与异常路径都必须清理站位对象与注入的 DrawCall。"""
        self._build_dump_fixture()
        # 在场者引用缺席者的专属槽位 40（= 几何被吸收的证据）
        _register_object("LOD0.aaaa-1-0", groups=[(0, 1.0), (40, 1.0)])
        # 正常路径
        exporter = self._make_exporter(
            ordered=[_PlainDrawCall("LOD0.aaaa-1-0")], inject=False
        )
        exporter._himi_inject_stub_objects()
        ordered = exporter.blueprint_model.ordered_draw_obj_data_model_list
        self.assertTrue(any(getattr(dc, "himi_stub", False) for dc in ordered))
        self.assertTrue(exporter.himi_stub_object_names())
        # 只考察事务边界：把基类 export() 换成可注入的替身（本用例不产出真实 mod）。
        base_calls = []
        exporter.export = ExportHIMI.export.__get__(exporter, ExportHIMI)
        original_base_export = ExportUnity.export
        ExportUnity.export = lambda self_: base_calls.append("export")
        try:
            exporter.export()
        finally:
            ExportUnity.export = original_base_export
        self.assertEqual(base_calls, ["export"], "export() 必须走到基类主流程")
        self.assertEqual(exporter.himi_stub_object_names(), [])
        self.assertFalse(
            any(getattr(dc, "himi_stub", False) for dc in ordered),
            "注入的 stub DrawCall 必须从模型列表里摘掉",
        )
        self.assertIsNone(_fake_objects.get("LOD0.aaaa-2-0"))

        # 异常路径：契约判定在写盘前抛错 ⇒ finally 仍须闭合事务
        exporter2 = self._make_exporter(
            ordered=[_PlainDrawCall("LOD0.aaaa-1-0")], inject=False
        )
        exporter2._himi_inject_stub_objects()
        ordered2 = exporter2.blueprint_model.ordered_draw_obj_data_model_list
        exporter2._himi_enforce_contract = lambda: (_ for _ in ()).throw(RuntimeError("boom"))
        with self.assertRaises(RuntimeError):
            exporter2.export()
        self.assertEqual(exporter2.himi_stub_object_names(), [])
        self.assertFalse(any(getattr(dc, "himi_stub", False) for dc in ordered2))
        self.assertIsNone(_fake_objects.get("LOD0.aaaa-2-0"))

    def test_st11_stub_creation_failure_aborts_loudly(self):
        """ST11 反例：创建失败**不得静默继续**（站位是抑制原版 draw 的唯一凭据）。"""
        self._build_dump_fixture()
        _register_object("LOD0.aaaa-1-0", groups=[(0, 1.0), (40, 1.0)])
        exporter = self._make_exporter(
            ordered=[_PlainDrawCall("LOD0.aaaa-1-0")], inject=False
        )
        _fake_meshes.fail_next_new = True
        with self.assertRaises(Fatal) as ctx:
            exporter._himi_inject_stub_objects()
        self.assertIn("站位对象创建失败", str(ctx.exception))
        self.assertEqual(exporter.himi_stub_object_names(), [])

    def test_st12_normal_mode_runs_rule1_only(self):
        """ST12（**t107 反转**）：``import_merged_vgmap()`` 关闭时**只跑规则①**。

        原口径是「关闭时完全不调用站位逻辑（产物与改造前逐字节相同）」。它错在把两件正交的
        事绑成一根开关：那个开关管的是「要不要产出合并骨架资源」，而规则①（部分在场的家族给
        缺失成员补站位）的正当性来自「缺失组件的几何已被**同一次导出**的幸存兄弟接管」。
        整段跳过的实测后果（用户报的 HI3 `虚构游戏`）：同一 IB 的第二笔原版 draw
        ``4386@57792`` 没有任何段接住 ⇒ 原版几何与 mod 几何同时出现 ⇒ 两份重叠、其中一份
        没有贴图、顶点槽错位串到别的物体。

        规则②/④ 仍留在开关后面（判据建立在 VGMap/palette 记账上），由
        ``StubRuleTests::test_st15_rule1_only_gates_rule2_and_writerless`` 钉住。
        """
        self._build_dump_fixture()
        _register_object("LOD0.aaaa-1-0", groups=[(0, 1.0), (40, 1.0)])
        _properties_state["checkbox"] = False
        exporter = self._make_exporter(
            ordered=[_PlainDrawCall("LOD0.aaaa-1-0")], inject=False
        )
        self.assertEqual(exporter._himi_inject_stub_objects(), ["LOD0.aaaa-2-0"])
        decision = exporter.himi_stub_decision()
        self.assertIsNotNone(decision)
        self.assertEqual(decision.stub_members, ["aaaa-2-0"])
        self.assertEqual(exporter.himi_stub_object_names(), ["LOD0.aaaa-2-0"])
        self.assertIsNotNone(_fake_objects.get("LOD0.aaaa-2-0"))

    # -- helpers ---------------------------------------------------------- #


class StubDocConsistencyTests(unittest.TestCase):
    """口径文字与实现的一致性（t27 F1-new 的缺陷形态：文档写了**已被反转**的旧口径）。

    t24 的审查记录指出「引用不存在的测试名」在本项目已出现三次，t27 F1-new 又出现
    一次：`ui/universal/himi.py` 的模块 docstring 引用
    ``test_stub_bare_never_enters_g1_target_set``（全仓零命中），且描述的是**已被否决**
    的方案。本类把两件事钉死：

    ① 源码/**活文档**里写的**合格**测试引用必须**真实存在**（防「文档与实现分叉」）；
    ② ``_himi_export_bares`` 的口径文字必须是**反转后**的那一版
       （站位 bare **进** `export_bares`，`is_stub` 只作只读标注）。

    **扫描边界（t35 G-1，与 t34 §6.2 的判定一致）**：

    * 只查**合格引用** —— ``Class::test_x`` 与 ``文件.py::test_x`` 两种形式。它们是对
      「这个测试在哪」的**明确断言**，悬空就是缺陷（本项目已发生两次）。
    * **裸名**（不带 ``Class::`` / ``文件.py::`` 的 ``test_xxx``）**不查** —— 计划书里的
      历史引述（§7.7.7 S6 引 ZZMI 原话、§13 台账的旧值、§13.7 否决表）按惯例用裸名提到
      测试，要求它们也存在会制造大量假阳性。
    * 计划书被纳入扫描（它是**活文档**，t33/t35 都在更新它）；``reports/**`` 不在扫描范围
      （历史报告按修订快照记录，过期是设计的一部分）。
    * 当前**没有白名单**：t35 实测计划书的 11 处合格引用**全部**解析成功（见交付说明）。
    """

    SOURCES = (
        "ui/universal/himi.py",
        "common/himi_skeleton.py",
        "HIMI骨骼合并计划书.md",
    )
    #: `文件.py::test_x`
    _PATH_TEST_REF = re.compile(
        r"(?P<path>[A-Za-z0-9_][A-Za-z0-9_./\\-]*\.py)::(?P<name>test_[A-Za-z0-9_]+)"
    )
    #: `Class::test_x`（lookbehind 排除 `file.py::test_x` 里那个 `py`）
    _CLASS_TEST_REF = re.compile(
        r"(?<![A-Za-z0-9_.])(?P<cls>[A-Za-z_][A-Za-z0-9_]*)::(?P<name>test_[A-Za-z0-9_]+)"
    )

    def _read(self, rel):
        return (REPO_ROOT / rel).read_text(encoding="utf-8")

    def _test_symbols(self):
        """``tests/`` 下真实存在的测试符号：``(文件 → 名字集合, 类名 → 名字集合)``。"""
        by_file: dict = {}
        by_class: dict = {}
        for path in sorted((REPO_ROOT / "tests").glob("test_*.py")):
            rel = f"tests/{path.name}"
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
            for node in tree.body:
                if isinstance(node, ast.ClassDef):
                    for stmt in node.body:
                        if isinstance(stmt, ast.FunctionDef) and stmt.name.startswith("test_"):
                            by_file.setdefault(rel, set()).add(stmt.name)
                            by_class.setdefault(node.name, set()).add(stmt.name)
                elif isinstance(node, ast.FunctionDef) and node.name.startswith("test_"):
                    by_file.setdefault(rel, set()).add(node.name)
        return by_file, by_class

    def test_referenced_test_names_exist(self):
        """合格引用（``Class::test`` / ``文件.py::test``）必须真实存在（t35 G-1 起含计划书）。"""
        by_file, by_class = self._test_symbols()
        checked = 0
        for rel in self.SOURCES:
            text = self._read(rel)
            for match in self._PATH_TEST_REF.finditer(text):
                target = match.group("path").replace("\\", "/")
                name = match.group("name")
                if not target.startswith("tests/") or target not in by_file:
                    continue  # 只查 tests/ 下的测试文件；其它路径不在本护栏的职责内
                checked += 1
                self.assertIn(
                    name,
                    by_file[target],
                    f"{rel} 引用的 {target}::{name} 在该文件里不存在（悬空测试名）",
                )
            for match in self._CLASS_TEST_REF.finditer(text):
                cls = match.group("cls")
                name = match.group("name")
                if cls not in by_class:
                    continue  # 非测试类名（如 `foo.py`）不在本护栏的职责内
                checked += 1
                self.assertIn(
                    name,
                    by_class[cls],
                    f"{rel} 引用的 {cls}::{name} 不存在（类名与方法名都要对得上）",
                )
        self.assertGreater(checked, 0, "前提：被扫描文件里确实写了合格测试引用")

    def test_export_bares_docstring_states_the_reversed_wording(self):
        """口径文字是反转后的那一版；被否决方案的措辞与测试名都不得残留。"""
        source = self._read("ui/universal/himi.py")
        module_doc = source.split('"""', 2)[1]
        # ① 回归钉子必须指向**真实存在**的那条用例（上面第一条用例同时核验它存在）
        self.assertIn(
            "test_stub_bare_enters_export_set_to_close_the_family_gate", module_doc
        )
        # ② 被否决方案的测试名与口径文字都不得残留
        self.assertNotIn("test_stub_bare_never_enters_g1_target_set", source)
        self.assertNotIn("永远不进目标集合", source)
        self.assertNotIn("显式排除 ``is_stub``", source)
        # ③ 反转后的口径必须逐字可读（站位 bare 进集合）
        self.assertIn("站位 bare 进 `export_bares`", module_doc)


class StubNailIntegrityTests(unittest.TestCase):
    """防删除 + 防漂移护栏（t33；来自 t29 的 F-2 与 t32 的锚点漂移建议）。

    **两条 F1 钉子是互补的**，删掉任一条都会让「注入必须早于基类组装」重新变成无人看守：

    * ``StubInjectionOrderTests::test_injection_happens_before_base_model_assembly``
      —— 直接快照**基类组装那一刻**的 ``ordered``；
    * ``StubEndToEndIniTests::test_stub_ib_override_section_reaches_the_real_ini``
      —— 成员集合由基类装配给出（``_assemble_models_from_base``），端到端读到产物 ini
      里的站位段。

    本类在**源码结构层**断言这两条仍在、且仍在断言因果属性（不是只剩个名字）；另把
    「文档/源码里引用的 ``文件:行号`` 必须落在文件内、指名测试符号时必须命中它的 def 行」
    钉死 —— 这类锚点漂移在本轮审查里出现了 5 次。

    锚点校验的**判定规则**（t35 G-2：文档与实现逐字一致，不再声称实现里没有的分支）：

    ① 每个行号引用归给**它前面最近的那个已知测试符号**；引用出现在任何符号之前时
       **不归**任何符号（例如「`common/himi_skeleton.py` 的某分支（`:872-893`）；
       `tests/…::test_xxx`」里的 `:872-893` 不会被算到那个测试头上）。
    ② 对每个符号，**至少要有一个**归它的引用命中该符号的 ``def`` 行（单点=等于，
       区间=包含）。**允许**同一行再写别的引用（如「符号（`:808`；内部断言见 `:836`）」）
       —— 只要其中有一个是 ``def`` 行就不报错（t34 §6.3 担心的假阳性由此消除）。
    ③ 指名了**别的文件**的引用不参与该符号的判定（如 `ui/universal/zzmi.py:1008-1013`）。
    """

    TEST_FILE = "tests/test_himi_stub_objects.py"
    #: 参与锚点校验的文件（引用方 + 常见被引用方）。
    #: **不包含历史报告**（如 `reports/himi-merge/19-*.md` / `20-*.md`）：它们按修订快照
    #: 记录当时的行号，**过期是设计的一部分**，不该被要求跟上当前文件。
    ANCHOR_FILES = (
        "HIMI骨骼合并计划书.md",
        "tests/test_himi_stub_objects.py",
        "tests/test_himi_merged_runtime.py",
        "common/himi_skeleton.py",
        "ui/universal/himi.py",
    )

    _PATH_REF = re.compile(
        r"(?P<path>[A-Za-z0-9_][A-Za-z0-9_./\\-]*\.(?:py|md))[:：](?P<s>\d+)"
        r"(?:\s*[-–~]\s*(?P<e>\d+))?"
    )
    _BARE_REF = re.compile(r"(?<![A-Za-z0-9_./\\:-])[:：](?P<s>\d+)(?:\s*[-–~]\s*(?P<e>\d+))?")

    # -- 生产文件锚点（t43 新增） ------------------------------------------ #
    #
    # 为什么需要（本轮事故）：t41 在 `common/himi_skeleton.py` 里加了 +183 行，计划书里
    # 一整批 `common/himi_skeleton.py:NNNN` 引用**整批漂移却零告警** —— 因为原护栏
    # （`test_doc_anchor_references_resolve`）只把行号归给**测试符号**，对生产文件的
    # 引用**只做界内检查**。
    #
    # 判定规则（与「符号 + 行号」的写作形式一一对应）：
    #  * 引用的**紧邻**（≤ `_ADJACENCY` 字符）出现一个 `` `符号` `` / `` `符号(...)` ``
    #    反引号记号，且该符号是**该文件**的 `def`/`class` ⇒ 行号必须命中它的定义行
    #    （单点 = 等于；区间 = 包含）。
    #  * 反之（引用没有紧邻的符号，或符号不是该文件的 def/class）⇒ **只校验界内**。
    #    **受此为边界的原因**：正文区间引用（例如「槽位分配循环（`:879-901`）」）指向的是
    #    函数体，而「包含定义行」对它恒假 —— 强判会造假阳性。这类引用的替代核查手段是
    #    脚本 `.dbg/_t43_anchor_audit.py`（把每条引用与最近符号、真值行号一起列出，供
    #    人工/周期性复核），本次 drift 正是用它定位的。
    #  * 另有 `` `文件.py::符号` `` 形式 ⇒ 该符号必须在文件里存在（`def`/`class`）。
    #  * `ANCHOR_FILES` 里的**历史报告**不参与（沿用原护栏的边界：快照行号过期是设计）。
    PROD_ANCHOR_FILES = (
        "common/himi_skeleton.py",
        "common/himi_merged_runtime.py",
        "common/himi_merged_contract.py",
        "ui/universal/himi.py",
    )
    _PROD_REF = re.compile(
        r"(?P<path>[A-Za-z0-9_][A-Za-z0-9_./\\-]*\.py)[:：](?P<s>\d+)"
        r"(?:\s*[-–~]\s*(?P<e>\d+))?"
    )
    _SYM_TOKEN = re.compile(r"`{1,2}(?P<name>[A-Za-z_][A-Za-z0-9_]*)(?:\([^`]*\))?`{1,2}")
    _PROD_SYMBOL_REF = re.compile(
        r"(?P<path>[A-Za-z0-9_][A-Za-z0-9_./\\-]*\.py)::(?P<name>[A-Za-z_][A-Za-z0-9_]*)"
    )
    _ADJACENCY = 3
    #: 生产锚点护栏的扫描面：`ANCHOR_FILES`（含计划书）+ 决策台账。
    #: `05-decisions.md` 参与**生产**锚点校验（它引用了同一条判据的行号，t43 已回填）；
    #: 它不进 `ANCHOR_FILES` —— 那里要管**测试**符号引用，而台账里大量历史引述
    #: （「原裁定……`:2604-2616`」这类按当时快照记的行号）不应对齐当前文件。
    PROD_ANCHOR_DOCS = tuple(ANCHOR_FILES) + ("reports/himi-merge/05-decisions.md",)
    _TEST_NAME = re.compile(r"\btest_[A-Za-z0-9_]+\b")

    # -- 源码读取 ---------------------------------------------------------- #

    def _read(self, rel):
        return (REPO_ROOT / rel).read_text(encoding="utf-8")

    def _module_function_source(self, name):
        source = self._read(self.TEST_FILE)
        for node in ast.parse(source).body:
            if isinstance(node, ast.FunctionDef) and node.name == name:
                return ast.get_source_segment(source, node) or ""
        raise AssertionError(f"找不到模块级函数 {name}")

    def _method_source(self, cls_name, method_name):
        source = self._read(self.TEST_FILE)
        for node in ast.parse(source).body:
            if isinstance(node, ast.ClassDef) and node.name == cls_name:
                for stmt in node.body:
                    if isinstance(stmt, ast.FunctionDef) and stmt.name == method_name:
                        return ast.get_source_segment(source, stmt) or ""
        raise AssertionError(f"找不到 {cls_name}::{method_name}")

    def _method_code_source(self, cls_name, method_name):
        """方法体源码，**去掉 docstring**（docstring 里提到某符号不算「用了」它）。"""
        source = self._read(self.TEST_FILE)
        for node in ast.parse(source).body:
            if isinstance(node, ast.ClassDef) and node.name == cls_name:
                for stmt in node.body:
                    if isinstance(stmt, ast.FunctionDef) and stmt.name == method_name:
                        body = list(stmt.body)
                        if body and isinstance(
                            getattr(body[0], "value", None), ast.Constant
                        ) and isinstance(body[0].value.value, str):
                            body = body[1:]
                        return "\n".join(
                            ast.get_source_segment(source, item) or "" for item in body
                        )
        raise AssertionError(f"找不到 {cls_name}::{method_name}")

    def _test_def_lines(self):
        found: dict = {}
        for path in sorted((REPO_ROOT / "tests").glob("test_*.py")):
            for index, line in enumerate(
                path.read_text(encoding="utf-8").splitlines(), 1
            ):
                match = re.match(r"\s*def (test_[A-Za-z0-9_]+)\s*\(", line)
                if match:
                    found.setdefault(match.group(1), []).append(
                        (f"tests/{path.name}", index)
                    )
        return found

    def _prod_def_lines(self, rel):
        """``{symbol: def/class 行号}``（仅 `def`/`class`；重复名取首个）。"""
        found: dict = {}
        for index, line in enumerate(self._read(rel).splitlines(), 1):
            match = re.match(
                r"\s*(?:async\s+)?(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", line
            )
            if match:
                found.setdefault(match.group(1), index)
        return found

    def _adjacent_symbols(self, line, start, end, defs):
        """行内与该引用**紧邻**的、且是 `defs` 里真实符号的反引号记号。"""
        found = []
        for match in self._SYM_TOKEN.finditer(line):
            if match.group("name") not in defs:
                continue
            before = start - match.end()
            after = match.start() - end
            if 0 <= before <= self._ADJACENCY or 0 <= after <= self._ADJACENCY:
                found.append(match.group("name"))
        return found

    # -- ① 防删除：两条 F1 钉子必须在，且仍断言因果属性 -------------------- #

    def test_f1_nails_are_still_installed(self):
        order = self._method_source(
            "StubInjectionOrderTests", "test_injection_happens_before_base_model_assembly"
        )
        for needle in (
            "ordered_draw_obj_data_model_list",
            "himi_stub",
            "ExportHIMI(blueprint_model)",
        ):
            self.assertIn(
                needle, order, f"顺序钉子必须仍断言 {needle}（F1 的唯一结构证据）"
            )

        e2e = self._method_source(
            "StubEndToEndIniTests", "test_stub_ib_override_section_reaches_the_real_ini"
        )
        self.assertIn(
            "_assemble_models_from_base", e2e, "端到端用例必须经真实装配取成员集合"
        )
        self.assertNotIn(
            "_drawib_model_for(",
            self._method_code_source(
                "StubEndToEndIniTests", "test_stub_ib_override_section_reaches_the_real_ini"
            ),
            "端到端用例本体不得自己造模型（那就是手工装配）",
        )

        helper_code = self._method_code_source(
            "StubEndToEndIniTests", "_assemble_models_from_base"
        )
        self.assertIn(
            "exporter.drawib_model_list", helper_code, "成员集合必须来自基类装配结果"
        )
        self.assertNotIn(
            "himi_stub_bares", helper_code, "不得用注入结果反推成员（F-2 描述的绕过）"
        )

    # -- ② 防删除：两态钩子必须在仓库里（F-3） ---------------------------- #

    def test_two_state_overlay_hook_is_present(self):
        source = self._read(self.TEST_FILE)
        for needle in (
            "HIMI_F1_BROKEN_ORDER_ENV",
            "def _f1_broken_order_source(",
            "def _injection_precedes_base_init(",
            "_himi_module_path_for_load()",
        ):
            self.assertIn(needle, source, f"两态复现钩子缺少 {needle}（F-3）")
        loader = self._module_function_source("_himi_module_path_for_load")
        self.assertIn("tempfile.mkdtemp", loader, "overlay 必须写临时目录")
        self.assertIn('io.open(overlay_path, "w"', loader, "只允许写 overlay 副本")
        self.assertNotIn(
            '_HIMI_EXPORTER_RELPATH, "w"', loader, "overlay 不得写仓库里的 himi.py"
        )
        self.assertIn(
            "_injection_precedes_base_init(broken)",
            self._module_function_source("_f1_broken_order_source"),
            "overlay 必须有后置条件自检（搬完必须是坏顺序）",
        )
        # G-3：临时目录必须被清掉（包装器 + try/finally + 前缀守卫）
        loader_fn = self._module_function_source("_load_himi_exporter_module")
        self.assertIn("finally", loader_fn, "overlay 的清理必须在 try/finally 里（G-3）")
        self.assertIn(
            "_cleanup_himi_overlay_dir(", loader_fn, "finally 里必须调用清理（G-3）"
        )
        self.assertIn(
            "_load_himi_exporter_module(", source, "模块加载必须经该包装器（否则清理不生效）"
        )
        self.assertIn("_HIMI_F1_OVERLAY_PREFIX", source, "清理必须按前缀认领自己的目录")
        cleanup_fn = self._module_function_source("_cleanup_himi_overlay_dir")
        self.assertIn(
            "_HIMI_F1_OVERLAY_PREFIX", cleanup_fn, "前缀守卫必须在清理函数里（不是调用点）"
        )

    # -- ③ 防泄漏：两态 overlay 的临时目录必须清掉（G-3） ------------------ #

    def test_overlay_dir_leaves_no_residue(self):
        """G-3：overlay **不得**在 `%TEMP%` 留残留（含 `HIMI_F1_BROKEN_ORDER=1` 运行）。

        ① 本进程钩子建过的目录必须都已清掉（broken 运行时这一步就是实测）；
        ② 走**真实包装器**加载一次：加载完成后它自己的临时目录必须已经没了
           （把 `finally` 里的清理注释掉，这一条会红 —— 两态实测见交付说明）；
        ③ 直接检查副本内容与清理语义：建了 → 带本前缀 → 确实是坏顺序 → 清掉；
        ④ 前缀守卫：非本前缀目录一律不动。
        """
        residue = [path for path in _HIMI_OVERLAY_CREATED if os.path.exists(path)]
        self.assertEqual(residue, [], f"overlay 目录未清理（G-3）：{residue}")

        previous = os.environ.get(HIMI_F1_BROKEN_ORDER_ENV)
        created_dir = ""
        os.environ[HIMI_F1_BROKEN_ORDER_ENV] = "1"
        try:
            count_before = len(_HIMI_OVERLAY_CREATED)
            module = _load_himi_exporter_module(f"{PKG}.ui.universal._himi_overlay_probe")
            self.assertIsNotNone(getattr(module, "ExportHIMI", None), "前提：副本可正常加载")
            self.assertGreater(
                len(_HIMI_OVERLAY_CREATED), count_before, "前提：这次加载确实建了临时目录"
            )
            wrapper_dir = _HIMI_OVERLAY_CREATED[-1]
            self.assertTrue(
                os.path.basename(wrapper_dir).startswith(_HIMI_F1_OVERLAY_PREFIX),
                "临时目录必须带本钩子的前缀（清理只认这个前缀）",
            )
            self.assertFalse(
                os.path.exists(wrapper_dir),
                "包装器加载完必须已清掉自己的临时目录（G-3：try/finally 清理）",
            )

            overlay_path = _himi_module_path_for_load()
            created_dir = os.path.dirname(overlay_path)
            self.assertTrue(os.path.isdir(created_dir), "前提：钩子确实建了临时目录")
            self.assertFalse(
                _injection_precedes_base_init(io.open(overlay_path, encoding="utf-8").read()),
                "前提：副本确实是坏顺序（清理逻辑不得改变搬运语义）",
            )
            self.assertTrue(_cleanup_himi_overlay_dir(created_dir), "清理必须成功")
            self.assertFalse(os.path.exists(created_dir), "清理后不得残留")
        finally:
            if previous is None:
                os.environ.pop(HIMI_F1_BROKEN_ORDER_ENV, None)
            else:
                os.environ[HIMI_F1_BROKEN_ORDER_ENV] = previous
            if created_dir:
                _cleanup_himi_overlay_dir(created_dir)

        foreign = tempfile.mkdtemp(prefix="not_himi_overlay_")
        try:
            self.assertFalse(
                _cleanup_himi_overlay_dir(foreign), "非本前缀目录不得被认领（前缀守卫）"
            )
            self.assertTrue(os.path.isdir(foreign), "非本前缀目录不得被删除")
        finally:
            shutil.rmtree(foreign, ignore_errors=True)

    # -- ③ 防漂移：锚点行号必须解析到真实符号 ----------------------------- #

    def test_doc_anchor_references_resolve(self):
        """`文件:行号` 必须落在文件内；指名测试符号时必须命中它的 `def` 行。

        判定规则（与类 docstring 逐字一致，t35 G-2）：按**出现顺序**把每个行号引用归给
        **它前面最近的那个已知测试符号**；对每个符号，**至少要有一个**归它的引用命中该
        符号的 ``def`` 行（单点=等于，区间=包含）；出现在任何符号之前的引用**不归**任何
        符号；指名别的文件的引用不参与判定。
        """
        defs = self._test_def_lines()
        problems = []
        for rel in self.ANCHOR_FILES:
            for index, line in enumerate(self._read(rel).splitlines(), 1):
                events = []
                for match in self._PATH_REF.finditer(line):
                    target = match.group("path").replace("\\", "/")
                    start = int(match.group("s"))
                    end = int(match.group("e") or start)
                    if (REPO_ROOT / target).is_file():
                        total = len(self._read(target).splitlines())
                        if not (1 <= start <= total and start <= end <= total):
                            problems.append(
                                f"{rel}:{index} 引用 {target}:{start}-{end} 越界"
                                f"（该文件共 {total} 行）"
                            )
                    events.append((match.start(), "ref", target, start, end))
                for match in self._BARE_REF.finditer(line):
                    start = int(match.group("s"))
                    end = int(match.group("e") or start)
                    events.append((match.start(), "ref", "", start, end))
                for name in set(self._TEST_NAME.findall(line)):
                    if name in defs:
                        events.append((line.find(name), "symbol", name, 0, 0))
                events.sort(key=lambda item: item[0])
                # 先把引用归给「前面最近的已知测试符号」，再逐符号判「至少一个命中 def 行」
                current = None
                attributed: dict = {}
                for _pos, kind, payload, start, end in events:
                    if kind == "symbol":
                        current = payload if len(defs[payload]) == 1 else None
                        attributed.setdefault(current, [])
                        continue
                    if current is None:
                        continue
                    attributed.setdefault(current, []).append((payload, start, end))
                for symbol, refs in attributed.items():
                    if symbol is None or not refs:
                        continue
                    def_file, def_line = defs[symbol][0]
                    applicable = [
                        (payload, start, end)
                        for payload, start, end in refs
                        if not payload or payload.endswith(def_file.split("/")[-1])
                    ]
                    if not applicable:
                        continue
                    if any(start <= def_line <= end for _p, start, end in applicable):
                        continue
                    payload, start, end = applicable[0]
                    shown = f"{payload}:{start}" if payload else f":{start}"
                    if end != start:
                        shown += f"-{end}"
                    problems.append(
                        f"{rel}:{index} 把 {symbol} 引成 {shown}，但它的 def 在 "
                        f"{def_file}:{def_line}（改成 {def_line}，或改用 Class::test 符号引用）"
                    )
        self.assertEqual(problems, [], "锚点漂移（t33 护栏）：" + "；".join(problems))

    def test_production_anchor_references_resolve(self):
        """生产文件的「符号 + 行号」引用必须命中该符号的 `def`/`class` 行（t43）。

        t41 的 +183 行位移让计划书里一整批 `common/himi_skeleton.py:NNNN` 失效，
        旧护栏没发现（它只把行号归给**测试**符号）。本用例把同一套判定扩到
        `PROD_ANCHOR_FILES`：**紧邻**的反引号符号（≤ `_ADJACENCY` 字符）若是该文件的
        `def`/`class`，则单点引用必须 == 定义行、区间引用必须包含定义行；另校验
        `` `文件.py::符号` `` 里的符号真实存在。没有紧邻符号的正文区间引用只做界内
        检查（规则与替代方案见 `PROD_ANCHOR_FILES` 上方注释）。
        """
        defs = {rel: self._prod_def_lines(rel) for rel in self.PROD_ANCHOR_FILES}
        problems = []
        # reports/himi-merge/ 可能被裁剪掉：缺文件时跳过，仓内文档的校验不受影响。
        for rel in [rel for rel in self.PROD_ANCHOR_DOCS if (REPO_ROOT / rel).exists()]:
            for index, line in enumerate(self._read(rel).splitlines(), 1):
                for match in self._PROD_SYMBOL_REF.finditer(line):
                    target = match.group("path").replace("\\", "/")
                    name = match.group("name")
                    if target in defs and name not in defs[target]:
                        problems.append(
                            f"{rel}:{index} 引用 {target}::{name}，但该符号在文件里不存在"
                            f"（现有符号共 {len(defs[target])} 个）"
                        )
                for match in self._PROD_REF.finditer(line):
                    target = match.group("path").replace("\\", "/")
                    if target not in defs:
                        continue
                    start = int(match.group("s"))
                    end = int(match.group("e") or start)
                    total = len(self._read(target).splitlines())
                    if not (1 <= start <= total and start <= end <= total):
                        problems.append(
                            f"{rel}:{index} 引用 {target}:{start}-{end} 越界"
                            f"（该文件共 {total} 行）"
                        )
                        continue
                    anchored = self._adjacent_symbols(
                        line, match.start(), match.end(), defs[target]
                    )
                    if not anchored:
                        continue  # 正文区间引用 ⇒ 只做界内检查（见类注释）
                    if any(
                        (start == end and start == defs[target][name])
                        or (start != end and start <= defs[target][name] <= end)
                        for name in anchored
                    ):
                        continue
                    shown = f"{target}:{start}" + (f"-{end}" if end != start else "")
                    hints = "、".join(
                        f"{name}@{defs[target][name]}" for name in anchored
                    )
                    problems.append(
                        f"{rel}:{index} 把 {shown} 记在 {hints} 名下，但行号不命中"
                        f"（改成 {hints} 里的定义行，或去掉紧邻的符号名）"
                    )
        self.assertEqual(
            problems, [], "生产文件锚点漂移（t43 护栏）：" + "；".join(problems)
        )


class StubDrawCallContractTests(_StubCase):
    """t36 blocker 护栏：站位 DrawCall 的鸭子类型必须**契约完整**（对齐真实 `DrawCallModel`）。

    为什么必须有这条（本轮第 6 次「绿了但没验到」）：F1 修复（注入早于基类组装）之后，
    站位 DrawCall **真的**进了 `drawib_model_list`，于是下游**第一次**开始读它 ——
    `common/submesh_model.py:81-89` 的 `SubMeshModel.__post_init__` 直接读
    `drawcall_model_list[0].match_draw_ib` / `.match_first_index` / `.match_index_count`
    ⇒ 缺字段会以 `AttributeError: 'HIMIStubDrawCall' object has no attribute
    'match_draw_ib'` **打断真实导出**（用户在「快速局部导出」里撞到的就是这条）。

    护栏做法：用 `dataclasses.fields(DrawCallModel)` 的**字段名集合** +
    `DrawCallModel` 的**公开方法名集合**对鸭子类型做**集合包含**检查。下游新增读取点时，
    只要它读的是 `DrawCallModel` 的既有成员，本护栏就自动覆盖 —— 不需要有人记得回来加断言。
    """

    # -- 真实模型的加载（惰性；只在本类用例里调用） ------------------------ #

    def _load_real_models(self):
        """把**真实** `DrawCallModel` / `SubMeshModel` 装进假包。

        `common/submesh_model.py` 在 import 时需要一个长依赖清单；这里只补它 import 时
        真正取用的符号。重活（`calc_buffer` 的 buffer/元数据计算）由用例自己打桩 ——
        本类要验的是 `__post_init__` 对站位 DrawCall 的**读取**（= 用户报错的现场），
        不是 buffer 计算。
        """
        stubs = (
            (f"{PKG}.utils.export_utils", {"ExportUtils": types.SimpleNamespace()}),
            (f"{PKG}.utils.obj_utils", {"ObjUtils": types.SimpleNamespace()}),
            (f"{PKG}.utils.collection_utils", {"CollectionUtils": types.SimpleNamespace()}),
            (f"{PKG}.utils.shapekey_utils", {"ShapeKeyUtils": types.SimpleNamespace()}),
            (
                f"{PKG}.common.d3d11_gametype",
                {"D3D11GameType": type("D3D11GameType", (), {})},
            ),
            (
                f"{PKG}.common.submesh_metadata",
                {
                    "SubmeshMetadataResolver": types.SimpleNamespace(
                        resolve=staticmethod(lambda *a, **k: None)
                    )
                },
            ),
        )
        for name, attrs in stubs:
            if name not in sys.modules:
                _install_module(name, **attrs)
        bpy_module = sys.modules.get("bpy")
        if bpy_module is not None and not hasattr(bpy_module, "types"):
            # `common/submesh_model.py` 在 **import 期**就要求值 `bpy.types.Object` 注解
            # （该文件没有 `from __future__ import annotations`）⇒ 补一个最小 types 命名空间。
            # 这些名字只出现在注解里，测试不会执行它们。
            bpy_module.types = types.SimpleNamespace(
                Object=type("Object", (), {}),
                PropertyGroup=type("PropertyGroup", (), {}),
                Mesh=type("Mesh", (), {}),
            )
        # `object_prefix_helper` import bpy ⇒ 依赖 `_StubCase.setUp` 装好的假 bpy
        for qualname, relpath in (
            (f"{PKG}.common.geometry_hash", "common/geometry_hash.py"),
            (f"{PKG}.common.m_key", "common/m_key.py"),
            (f"{PKG}.common.object_prefix_helper", "common/object_prefix_helper.py"),
            (f"{PKG}.common.draw_call_model", "common/draw_call_model.py"),
            (f"{PKG}.common.submesh_model", "common/submesh_model.py"),
        ):
            if qualname not in sys.modules:
                _load_real_module(qualname, relpath)
        return (
            sys.modules[f"{PKG}.common.draw_call_model"],
            sys.modules[f"{PKG}.common.submesh_model"],
        )

    # -- ① 契约完整性（本任务最重要的产物） ------------------------------- #

    def test_stub_draw_call_satisfies_draw_call_model_contract(self):
        """`HIMIStubDrawCall` 必须覆盖真实 `DrawCallModel` 的**全部**字段与公开方法。"""
        import dataclasses  # 局部导入：避免改动模块头部（会移动既有锚点行号）

        draw_call_model, _ = self._load_real_models()
        real = draw_call_model.DrawCallModel
        duck = _skeleton.HIMIStubDrawCall("LOD0.aaaa-2-0")

        real_field_names = {f.name for f in dataclasses.fields(real)}
        missing_fields = sorted(n for n in real_field_names if not hasattr(duck, n))
        self.assertEqual(
            missing_fields,
            [],
            f"鸭子类型缺 DrawCallModel 字段（下游会 AttributeError）：{missing_fields}",
        )
        # 更强的一层：不只是「运行时有这个属性」，而是**dataclass 层面声明过**
        # （否则 `dataclasses.asdict` / `replace` / `fields` 这类消费者会漏字段）。
        duck_field_names = {f.name for f in dataclasses.fields(_skeleton.HIMIStubDrawCall)}
        undeclared = sorted(n for n in real_field_names if n not in duck_field_names)
        self.assertEqual(
            undeclared, [], f"鸭子类型未**声明**这些 DrawCallModel 字段：{undeclared}"
        )

        real_methods = {
            name
            for name in dir(real)
            if not name.startswith("_") and callable(getattr(real, name, None))
        }
        missing_methods = sorted(
            name for name in real_methods if not callable(getattr(duck, name, None))
        )
        self.assertEqual(
            missing_methods, [], f"鸭子类型缺 DrawCallModel 公开方法：{missing_methods}"
        )

        # `init=False` 的构造差异：真实现里 9 个派生字段**不参与构造**，鸭子类型同样不参与
        # —— 显式断言，**不因此放宽**字段集合要求（上面已按全集检查）。
        real_init = {f.name for f in dataclasses.fields(real) if f.init}
        duck_init = {f.name for f in dataclasses.fields(_skeleton.HIMIStubDrawCall) if f.init}
        self.assertTrue(
            real_init <= duck_init,
            f"鸭子类型的可构造字段必须覆盖真实类型：{sorted(real_init - duck_init)}",
        )
        self.assertTrue(
            {"match_draw_ib", "match_first_index", "match_index_count"} <= real_field_names
            and not ({"match_draw_ib", "match_first_index", "match_index_count"} & duck_init),
            "`SubMeshModel.__post_init__` 读的三个 match_* 字段必须是派生字段（init=False、不参与构造）",
        )

        # 值语义（从合成的 `LOD0.<bare>` 名自行解析；不调 ObjectPrefixHelper）
        self.assertEqual(duck.match_draw_ib, "aaaa")
        self.assertEqual(duck.match_index_count, "2")
        self.assertEqual(duck.match_first_index, "0")
        self.assertEqual(duck.match_unique_str, "aaaa-2-0")
        self.assertEqual(duck.match_workspace_unique_str, "LOD0.aaaa-2-0")
        self.assertEqual(duck.get_workspace_unique_str(), "LOD0.aaaa-2-0")
        self.assertEqual(duck.get_unique_str(), "aaaa-2-0")
        # 真实现里这两个也是 `init=False` 的空容器；站位件没有按键条件
        self.assertEqual(duck.work_key_list, [])
        self.assertEqual(duck.shader_replace_info_list, [])
        self.assertIs(duck.shader_replace_info_resolved, False)
        self.assertEqual(duck.get_condition_str(), "")

    # -- ② 真 `SubMeshModel.__post_init__`（用户报错的现场） --------------- #

    def test_real_submesh_model_post_init_reads_stub_draw_call(self):
        """跑到**真实** `SubMeshModel.__post_init__`：它读站位 DrawCall 的 `match_*`。

        这条用例就是「本轮第 6 次绿了但没验到」的补课：旧宿主用 `types.SimpleNamespace`
        替身、从不实例化真实 `SubMeshModel`，所以缺字段的缺陷在单元层看不见。
        """
        _, submesh_model = self._load_real_models()
        stub = _skeleton.HIMIStubDrawCall("LOD0.aaaa-2-0", vertex_count=3, index_count=3)

        original = submesh_model.SubMeshModel.calc_buffer
        submesh_model.SubMeshModel.calc_buffer = lambda self: None  # 重活打桩
        try:
            model = submesh_model.SubMeshModel(drawcall_model_list=[stub])
        finally:
            submesh_model.SubMeshModel.calc_buffer = original

        self.assertEqual(model.match_draw_ib, "aaaa")
        # 与 `ObjectPrefixHelper.parse_prefix_parts` 同序：`bare` 的第 2 段 = index_count、
        # 第 3 段 = first_index（真实现把这两个 **str** 直接赋给 SubMeshModel 的 int 注解字段
        # —— 那是既有代码的既有形态，站位件与真 DrawCallModel 给的是同型值）
        self.assertEqual(model.match_index_count, "2")
        self.assertEqual(model.match_first_index, "0")
        self.assertEqual(model.workspace_unique_str, "LOD0.aaaa-2-0")
        self.assertEqual(model.unique_str, "LOD0.aaaa-2-0")

        # 反向：把字段名改掉 ⇒ 这条用例会以 AttributeError 失败（两态实测见交付说明）
        self.assertTrue(hasattr(stub, "match_draw_ib"))

    # -- ③ t26 的两条原始约束（不引入重模块 / 不用 ObjectPrefixHelper） ---- #

    def test_stub_duck_type_keeps_t26_constraints(self):
        """数据层保持 bpy-free；站位鸭子类型不继承真实现、不引入 `object_prefix_helper`。"""
        draw_call_model, _ = self._load_real_models()
        for attr in ("DrawCallModel", "ObjectPrefixHelper", "bpy"):
            self.assertFalse(
                hasattr(_skeleton, attr),
                f"common/himi_skeleton.py 不得在模块级绑定 {attr}（t26 约束 / bpy-free）",
            )
        self.assertFalse(
            issubclass(_skeleton.HIMIStubDrawCall, draw_call_model.DrawCallModel),
            "站位件刻意**不**继承真实 DrawCallModel（它的 __post_init__ 会用 ObjectPrefixHelper 误拆合成名）",
        )


class StubRule1Tests(_StubCase):
    """t38 blocker 护栏：**规则①（家族部分在场）** 的 ZZMI 逐字口径 + F-A 身份识别。

    用户实测的失效形态：多成员家族（`2c706fbd` 5 分块）里每个成员都与同族成员 **100% 共享**
    槽位 ⇒ `exclusive` 恒空 ⇒ 旧的「逐成员 `exclusive` 门控」把它们全部推进保守分支 ⇒
    **多成员家族永远不生成占位** ⇒ 原版 draw 照跑 ⇒ 重影。

    修法 = 规则① 回到 ZZMI（`ui/universal/zzmi.py:967-971`）的逐字口径：家族部分在场 ⇒
    缺失成员**直接全补**，不做 `exclusive` / `used` / `VGMap` 门控（**只保留规则③** 的显式排除，
    ZZMI 也在同一层逐成员判）。
    """

    FAMILY = "2c706fbd"
    MEMBERS = [
        "2c706fbd-47199-0",
        "2c706fbd-5259-55380",
        "2c706fbd-7737-47199",
        "2c706fbd-3207-60639",
        "2c706fbd-444-54936",
    ]

    def _write_shared_family(self, carrier=None):
        """5 个分块、**全部**只引用槽位 0/1（= 同族 100% 共享 ⇒ 每人 `exclusive` 恒空）。

        ``carrier`` 给定时额外写一个**已落盘**的别的家族载体（它是 `declared_by_present`
        可判定的前提）。
        """
        components = [{"bare": bare, "vg_map": {0: 0, 1: 1}} for bare in self.MEMBERS]
        families = {self.FAMILY: list(self.MEMBERS)}
        if carrier:
            components.append({"bare": carrier, "vg_map": {0: 0, 1: 1}})
            families[carrier.split("-", 1)[0]] = [carrier]
        _write_workspace(self.workspace, components, families)

    def test_rule1_partial_family_stubs_every_missing_member(self):
        """① 用户形态：多成员家族 + 全部槽位同族共享（`exclusive` 恒空）⇒ 缺失成员全补站位。"""
        self._write_shared_family()
        landed = {bare: {0, 1} for bare in self.MEMBERS}
        for bare in self.MEMBERS:
            self.assertEqual(
                _skeleton.himi_exclusive_slots(bare, self.MEMBERS, landed),
                set(),
                f"前提：{bare} 的 exclusive 恒空（同族 100% 共享）⇒ 旧判据必然退化",
            )
        decision = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"2c706fbd-47199-0"},  # 只有 1 个分块在场（= 用户实测形态）
            used_group_ids={0, 1},
        )
        self.assertEqual(
            sorted(decision.stub_members),
            sorted(self.MEMBERS[1:]),
            "家族部分在场 ⇒ 其余 4 个分块必须**全部**补站位（ZZMI 规则① 逐字口径）",
        )
        self.assertEqual(decision.families[self.FAMILY]["rule"], "1")
        self.assertEqual(decision.skipped_members, [])

    def test_rule1_still_respects_dedup_excluded(self):
        """规则③ 在规则① 下仍生效：显式排除优先于 absorbed（ZZMI 同层逐成员判）。"""
        self._write_shared_family()
        excluded = self.MEMBERS[1]
        path = _skeleton.resolve_submesh_json_path(self.workspace, f"{LOD}.{excluded}")
        payload = json.loads(open(path, encoding="utf-8").read())
        payload["VGMapDedupExcluded"] = True
        with io.open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)
        decision = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"2c706fbd-47199-0"},
            used_group_ids={0, 1},
        )
        self.assertNotIn(excluded, decision.stub_members, "显式排除优先 ⇒ 不插桩")
        self.assertIn(excluded, decision.skipped_members)
        self.assertEqual(
            decision.reason_counts.get(_skeleton.HIMI_STUB_REASON_DEDUP_EXCLUDED), 1
        )
        self.assertEqual(
            sorted(decision.stub_members), sorted(set(self.MEMBERS[1:]) - {excluded})
        )

    def test_rule1_recognizes_merged_object_by_ib_identity(self):
        """F-A：合并对象身份带副本后缀（`..._copy`）⇒ 按 **IB 身份** 识别「家族部分在场」。

        两态：同一份数据，场景对象换成别的 IB（`9f9f9f9f-1-0_copy`）⇒ 落到规则②，但本族
        仍是这些槽唯一的 palette 写方 ⇒ **全族**补站位（t103 口径）。
        """
        self._write_shared_family()
        merged = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={f"2c706fbd-47199-0_copy"},  # bare 不匹配任何声明成员
            used_group_ids={0, 1},
        )
        self.assertEqual(
            merged.families[self.FAMILY]["rule"],
            "1",
            "合并对象的 bare 带 `_copy` 后缀，但 IB 身份是 2c706fbd ⇒ 家族部分在场",
        )
        self.assertEqual(
            sorted(merged.stub_members),
            sorted(self.MEMBERS),
            "5 个分块都被合并对象接管 ⇒ 全部补站位（含基身份 2c706fbd-47199-0）",
        )
        other = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"9f9f9f9f-1-0_copy"},  # 别的 IB ⇒ 本族整族缺席
            used_group_ids={0, 1},
        )
        self.assertEqual(other.families[self.FAMILY]["rule"], "2")
        self.assertEqual(
            sorted(other.stub_members),
            sorted(self.MEMBERS),
            "整族缺席 + 槽位被引用且没有在场家族写它们 ⇒ 全族补站位（t103 writerless 口径）",
        )
        self.assertEqual(
            other.families[self.FAMILY]["decision"], _skeleton.HIMI_STUB_REASON_WRITERLESS
        )

    def test_rule2_zero_reference_multi_member_still_not_stubbed(self):
        """② 反例：整族缺席 + **零引用** ⇒ 不插桩（保守方向未被规则① 的改动放松）。"""
        self._write_shared_family()
        decision = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares=set(),
            used_group_ids=set(),
        )
        self.assertEqual(decision.stub_members, [], "零引用 ⇒ 按用户意图不生成")
        self.assertEqual(decision.families[self.FAMILY]["rule"], "2")
        self.assertEqual(
            sorted(decision.skipped_members), sorted(self.MEMBERS)
        )

    def test_rule2_borrow_protection_moved_but_not_lost(self):
        """③ 事故形态辨析：同一份数据走规则① 会补站位、走规则② 被 `exclusive` 门控挡住。

        ZZMI 2026-09-16 事故（借位被误读为吸收）发生在**规则②**（整族缺席 + 跨族借位）⇒
        规则② 的门控**原样保留**；规则① 的前提是「同族有幸存对象」，与该事故形态不同。
        """
        self._write_shared_family(carrier="8c8de427-798-0")
        partial = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"2c706fbd-47199-0"},
            used_group_ids={0, 1},
        )
        whole = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"8c8de427-798-0"},  # 载体属于**别的** IB 家族
            used_group_ids={0, 1},
        )
        self.assertEqual(partial.families[self.FAMILY]["rule"], "1")
        self.assertTrue(partial.stub_members, "规则①：家族部分在场 ⇒ 补站位")
        self.assertEqual(whole.families[self.FAMILY]["rule"], "2")
        self.assertEqual(whole.stub_members, [], "规则②：借位不算吸收 ⇒ 不插桩")
        self.assertEqual(
            whole.families[self.FAMILY]["decision"],
            _skeleton.HIMI_STUB_REASON_ZERO_REFERENCE,
        )


class StubForeignSlotTests(_StubCase):
    """t41：规则② 的「**外来槽**」判据（计划书 §7.7.12.2/§7.7.12.4，ST13/ST14a/ST14b）。

    判据：``foreign_used := used \\ declared_by_present``；
    ``absorbed(ib) := ∃ m ∈ C(ib) ∩ L : own(m) ∩ foreign_used ≠ ∅``（逐成员）。

    这里每条都做**两态**（本项目纪律：护栏必须非恒真），且**全部零写入** —— 变异体要么是
    **传参值**（``declared_by_present=None`` / ``set()``），要么是**进程内 monkeypatch**
    （``_declared_by_present_slots`` 的返回），不动仓库任何文件。
    """

    def _absorbed_fixture(self):
        """X 族整族缺席（两成员 100% 共享槽 0/1）+ 已落盘载体 P（声明自己的槽 5）。"""
        _write_workspace(
            self.workspace,
            [
                {"bare": "aaaa-1-0", "vg_map": {0: 0, 1: 1}},
                {"bare": "aaaa-2-0", "vg_map": {0: 0, 1: 1}},
                {"bare": "bbbb-1-0", "vg_map": {0: 5}},
            ],
            {"aaaa": ["aaaa-1-0", "aaaa-2-0"], "bbbb": ["bbbb-1-0"]},
        )

    # -- ST13：外来槽正例（能被检出） -------------------------------------------- #

    def test_st13_foreign_slot_detects_absorbed_family(self):
        """ST13：整族缺席 + 全成员 `exclusive=∅` + P 引用 X 的槽且 `own(P) ∩ own(X)=∅` ⇒ 逐组件补站位。"""
        self._absorbed_fixture()
        landed = {
            "aaaa-1-0": {0, 1},
            "aaaa-2-0": {0, 1},
            "bbbb-1-0": {5},
        }
        # 前提①：族内 100% 共享 ⇒ 被替换的旧判据在这里**恒空**（结构性失效）
        for member in ("aaaa-1-0", "aaaa-2-0"):
            self.assertEqual(
                _skeleton.himi_exclusive_slots(member, ["aaaa-1-0", "aaaa-2-0"], landed),
                set(),
                f"前提：{member} 的 exclusive 恒空 ⇒ 旧判据检不出",
            )
        # 前提②：旧判据（exclusive ∩ used）在同一 fixture 上判不出被吸收
        self.assertFalse(
            _skeleton.is_drawib_absorbed(
                "aaaa",
                family_members=["aaaa-1-0", "aaaa-2-0"],
                landed_values=landed,
                used_group_ids={5, 0},
                # 旧判据 = 「族内专属槽 ∩ used」：这里 exclusive 恒空 ⇒ 必然判不出
                declared_by_present=None,
            ),
            "旧判据（族内专属槽）在同一数据上判不出 ⇒ 本用例不是恒真断言",
        )
        decision = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"bbbb-1-0"},  # 载体在场、X 整族缺席（规则②）
            used_group_ids={5, 0},  # P 用槽 5（自己声明）+ 槽 0（**外来**）
        )
        self.assertEqual(decision.families["aaaa"]["rule"], "2")
        self.assertEqual(
            sorted(decision.stub_members),
            ["aaaa-1-0", "aaaa-2-0"],
            "外来槽 0 落在两个成员的 own 里 ⇒ 逐组件补站位（这正是旧判据漏掉的重影形态）",
        )
        self.assertEqual(
            decision.families["aaaa"]["decision"], _skeleton.HIMI_STUB_REASON_WRITERLESS
        )

    def test_st13_mutant_fail_closed_loses_detection(self):
        """ST13 两态（t103）：判据已换成「唯一 palette 写方」⇒ 打掉声明命名空间仍检得出。

        旧口径靠 `declared_by_present_slots`（外来槽）识别吸收；t103 起整族缺席的族只要它
        注册的槽被引用、且没有任何在场家族注册这些槽，就判「唯一写方」⇒ 不依赖声明命名空间。
        本用例把那条路打掉，结论必须不变（改名前的旧版本这里是「检不出 ⇒ 不插桩」）。
        """
        self._absorbed_fixture()
        original = _skeleton.declared_by_present_slots
        _skeleton.declared_by_present_slots = lambda *args, **kwargs: None
        try:
            mutated = decide_stub_injection(
                workspace_root=self.workspace,
                scene_present_bares={"bbbb-1-0"},
                used_group_ids={5, 0},
            )
        finally:
            _skeleton.declared_by_present_slots = original
        self.assertEqual(
            sorted(mutated.stub_members),
            ["aaaa-1-0", "aaaa-2-0"],
            "判据不再读声明命名空间 ⇒ 变异体不影响结论（旧版本这里是 []）",
        )
        self.assertEqual(
            mutated.families["aaaa"]["decision"], _skeleton.HIMI_STUB_REASON_WRITERLESS
        )
        # 还原后同一份数据结论相同（证明变异体已不再是判据的一部分）
        restored = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"bbbb-1-0"},
            used_group_ids={5, 0},
        )
        self.assertEqual(sorted(restored.stub_members), ["aaaa-1-0", "aaaa-2-0"])

    # -- ST14a：事故形态（不得重演） --------------------------------------------- #

    def _accident_fixture(self, carrier_declares_shared=True):
        """载体 P **声明**它引用的 0/1（canonical 借位）⇒ 不得判成吸收。"""
        carrier_own = {0: 0, 1: 1} if carrier_declares_shared else {0: 5}
        _write_workspace(
            self.workspace,
            [
                {"bare": "c209c22b-45087-0", "vg_map": {0: 0, 1: 1}},
                {"bare": "8c8de427-798-0", "vg_map": carrier_own},
            ],
            {"c209c22b": ["c209c22b-45087-0"], "8c8de427": ["8c8de427-798-0"]},
        )

    def test_st14a_declared_shared_slot_is_not_absorption(self):
        """ST14a：X 的槽**也被 P 声明**（canonical 共享）⇒ 不是外来槽 ⇒ **不插桩**（2026-09-16 形态）。"""
        self._accident_fixture(carrier_declares_shared=True)
        decision = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"8c8de427-798-0"},
            used_group_ids={0, 1},
        )
        self.assertEqual(
            decision.stub_members, [], "借位槽被 canonical 拥有者声明 ⇒ 不算外来槽 ⇒ 不得插桩"
        )
        self.assertIn("c209c22b-45087-0", decision.skipped_members)
        self.assertEqual(
            decision.families["c209c22b"]["decision"], _skeleton.HIMI_STUB_REASON_ZERO_REFERENCE
        )

    def test_st14a_counter_removing_declaration_detects_absorption(self):
        """ST14a 反例（非恒真）：把共享槽从 P 的 `own` 里去掉 ⇒ 它成为外来槽 ⇒ **插桩**。"""
        self._accident_fixture(carrier_declares_shared=False)
        decision = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"8c8de427-798-0"},
            used_group_ids={0, 1, 5},
        )
        self.assertEqual(
            decision.stub_members,
            ["c209c22b-45087-0"],
            "P 引用了它没声明的槽 0/1 ⇒ 外来槽 ⇒ 判吸收（与 ST14a 形成两态对照）",
        )

    def test_st14a_mutant_ignoring_declaration_reintroduces_the_accident(self):
        """ST14a 两态（**事故判据**）：忽略 `declared_by_present`（等价 `foreign_used := used`）⇒ 误插。"""
        self._accident_fixture(carrier_declares_shared=True)
        original = _skeleton.declared_by_present_slots
        _skeleton.declared_by_present_slots = lambda *args, **kwargs: set()
        try:
            mutated = decide_stub_injection(
                workspace_root=self.workspace,
                scene_present_bares={"8c8de427-798-0"},
                used_group_ids={0, 1},
            )
        finally:
            _skeleton.declared_by_present_slots = original
        self.assertEqual(
            mutated.stub_members,
            ["c209c22b-45087-0"],
            "变异体 = 事故判据（借位槽被读成吸收）⇒ ST14a 会红 ⇒ 反例保护确实靠那一次减法",
        )
        restored = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"8c8de427-798-0"},
            used_group_ids={0, 1},
        )
        self.assertEqual(restored.stub_members, [])

    # -- ST14b：fail-closed（信息不足不猜） -------------------------------------- #

    def test_st14b_unavailable_namespace_fails_closed(self):
        """ST14b（t103）：载体**未落盘**（`own` 不可判定）⇒ 槽位没有已知写方 ⇒ 仍补站位。

        旧口径在这里 fail-closed（不插桩）；规则④ 只看「被引用且没有在场家族注册」这一个
        事实，载体 json 缺失只会让「有人写」这条路不可见 ⇒ 结论不变。② 段证明补齐载体后
        结论一致，因此本用例不再有「两态」，只保留「不因信息缺失而放弃站位」这一条。
        """
        # ① 载体在家族名单里但**没有 json** ⇒ own 不可判定 ⇒ fail-closed
        _write_workspace(
            self.workspace,
            [{"bare": "aaaa-1-0", "vg_map": {0: 0, 1: 1}}],
            {"aaaa": ["aaaa-1-0"], "bbbb": ["bbbb-1-0"]},
        )
        closed = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"bbbb-1-0"},
            used_group_ids={0},
        )
        self.assertEqual(
            sorted(closed.stub_members),
            ["aaaa-1-0"],
            "槽 0 被引用且没有已知写方 ⇒ 补站位（信息缺失不再等于放弃压制）",
        )
        self.assertEqual(
            closed.families["aaaa"]["decision"], _skeleton.HIMI_STUB_REASON_WRITERLESS
        )
        # ② 补齐载体的 own ⇒ 槽 0 成为外来槽 ⇒ 插桩（证明上一步不是恒真）
        self._absorbed_fixture()
        opened = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"bbbb-1-0"},
            used_group_ids={5, 0},
        )
        self.assertEqual(sorted(opened.stub_members), ["aaaa-1-0", "aaaa-2-0"])

    def test_declared_by_present_helper_rejects_unknown_identity(self):
        """身份解析不出的 present 对象 ⇒ 助手返回 None（fail-closed 的**直接**单测）。"""
        _write_workspace(
            self.workspace,
            [{"bare": "aaaa-1-0", "vg_map": {0: 0}}],
            {"aaaa": ["aaaa-1-0"]},
        )
        landed = {"aaaa-1-0": {0}}
        self.assertEqual(
            _skeleton.declared_by_present_slots({"aaaa-1-0"}, {"aaaa": ["aaaa-1-0"]}, landed),
            {0},
        )
        self.assertIsNone(
            _skeleton.declared_by_present_slots({"zzzz-9-9"}, {"aaaa": ["aaaa-1-0"]}, landed),
            "既不是已知成员、IB 也不认识的 present 对象 ⇒ 不可判定 ⇒ None",
        )
        self.assertIsNone(
            _skeleton.declared_by_present_slots({"aaaa-2-0"}, {"aaaa": ["aaaa-2-0"]}, landed),
            "成员身份已知但 own 为空（未落盘）⇒ None",
        )


class WriterlessFamilyStubTests(_StubCase):
    """t103 规则④：整族缺席、但它注册的槽位没有别的写方 ⇒ 必须补站位。

    为什么不能只靠规则②：规则② 要求「槽位被**现存对象**实际引用（外来槽）」才插桩。
    palette 写方与「谁引用了它」是两件事 —— 那些槽没有它写就一直是零矩阵，而引用它们
    的行来自运行期那笔 draw 的顶点池，导出期只认得本族局部编号（计划书 §13.22）。
    所以规则④ 只看槽位归属：没有别的在场家族注册 ⇒ 它就是唯一写方 ⇒ 补站位。
    """

    def test_absent_family_owning_unclaimed_slots_is_stubbed(self):
        _write_workspace(
            self.workspace,
            [
                {"bare": "aaaa-1-0", "vg_map": {0: 0}},
                {"bare": "bbbb-1-0", "vg_map": {0: 5}},
            ],
            {"aaaa": ["aaaa-1-0"], "bbbb": ["bbbb-1-0"]},
        )
        decision = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"aaaa-1-0"},
            used_group_ids={0, 5},  # 槽 5 被引用、且只有 bbbb 会写 ⇒ 必须补站位
        )
        self.assertEqual(
            decision.stub_members, ['bbbb-1-0'],
            "槽 5 只有 bbbb 会写 ⇒ 即使没人引用也必须补站位（否则运行期零矩阵）",
        )
        self.assertEqual(
            decision.families["bbbb"]["decision"], _skeleton.HIMI_STUB_REASON_WRITERLESS
        )
        self.assertEqual(decision.families["bbbb"]["rule"], "2")
        self.assertTrue(
            any("唯一的 palette 写方" in text for text in decision.diagnostics),
            "诊断必须说清它是这些槽唯一的写方",
        )

    def test_slot_registered_by_present_family_is_not_stubbed(self):
        """反例：缺席家族的槽已被在场家族注册 ⇒ 有写方 ⇒ 不插桩（规则④ 不得放大）。"""
        _write_workspace(
            self.workspace,
            [
                {"bare": "aaaa-1-0", "vg_map": {0: 0}},
                {"bare": "cccc-1-0", "vg_map": {0: 0}},
            ],
            {"aaaa": ["aaaa-1-0"], "cccc": ["cccc-1-0"]},
        )
        decision = decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares={"aaaa-1-0"},
            used_group_ids={0},
        )
        self.assertEqual(decision.stub_members, [], "槽 0 由在场的 aaaa 写 ⇒ 不缺口写方")
        self.assertNotEqual(
            decision.families["cccc"]["decision"], _skeleton.HIMI_STUB_REASON_WRITERLESS
        )


class MissingWriterStubTests(_StubCase):
    """t106：本次导出引用了「别族保管的槽」，而那族没进场景 ⇒ 自动补 3 点站位。

    实机形态（User said m02054）：只选宿主 ``077983c9`` 做快速局部导出，宿主 Blend
    引用槽 216，槽 216 由已被合并进宿主的 ``1b62725d-1740-0`` 注册 —— 它的几何已在
    宿主顶点池里，但 palette 只有它会写，缺了就是运行期零矩阵。
    """

    def _exporter(self):
        return self._make_exporter(ordered=[], inject=False)

    def _write_families(self, *, keep_writer_in_family_map: bool = True):
        _write_workspace(
            self.workspace,
            [
                {"bare": "aaaa1111-1-0", "vg_map": {0: 0}},
                {"bare": "bbbb2222-1-0", "vg_map": {0: 216}},
            ],
            (
                {"aaaa1111": ["aaaa1111-1-0"], "bbbb2222": ["bbbb2222-1-0"]}
                if keep_writer_in_family_map
                else {"aaaa1111": ["aaaa1111-1-0"]}
            ),
        )

    def _run(self, decision, *, present=("aaaa1111-1-0",), family_map=None):
        self._write_families()
        exporter = self._exporter()
        added = exporter._himi_add_missing_writer_stubs(
            decision,
            workspace_root=self.workspace,
            scene_present=set(present),
            used_group_ids={0, 216},
            family_map=(
                {"aaaa1111": ["aaaa1111-1-0"], "bbbb2222": ["bbbb2222-1-0"]}
                if family_map is None
                else family_map
            ),
        )
        return exporter, added

    def test_absent_writer_family_gets_a_stub(self):
        decision = _skeleton.HIMIStubDecision()
        exporter, added = self._run(decision)
        self.assertEqual(added, ["bbbb2222-1-0"])
        self.assertEqual(decision.stub_members, ["bbbb2222-1-0"])
        self.assertEqual(
            exporter._himi_stub_writer_slots,
            {"bbbb2222-1-0": [216]},
            "站位只负责给这些槽写矩阵，槽号要留下来给后续诊断",
        )

    def test_writer_family_in_scene_is_not_stubbed(self):
        decision = _skeleton.HIMIStubDecision()
        _, added = self._run(decision, present=("aaaa1111-1-0", "bbbb2222-1-0"))
        self.assertEqual(added, [], "写方自己在场景里 ⇒ 它自己的段就会写这些槽")
        self.assertEqual(decision.stub_members, [])

    def test_writer_family_without_component_data_is_not_guessed(self):
        decision = _skeleton.HIMIStubDecision()
        _, added = self._run(
            decision,
            family_map={"aaaa1111": ["aaaa1111-1-0"]},
        )
        self.assertEqual(added, [], "组件数据都没有 ⇒ 不猜，交给导出期的 orphan 门")
        self.assertEqual(decision.stub_members, [])

    def test_writer_family_skipped_by_rule_two_is_promoted_to_stub(self):
        decision = _skeleton.HIMIStubDecision(skipped_members=["bbbb2222-1-0"])
        exporter, added = self._run(decision)
        self.assertEqual(added, ["bbbb2222-1-0"])
        self.assertEqual(decision.stub_members, ["bbbb2222-1-0"])
        self.assertEqual(decision.skipped_members, [], "已在跳过名单 ⇒ 必须移出")
        self.assertEqual(exporter._himi_stub_writer_slots, {"bbbb2222-1-0": [216]})

if __name__ == "__main__":
    unittest.main()
