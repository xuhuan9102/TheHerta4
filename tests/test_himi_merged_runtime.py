"""T7：HIMI 合并骨架**导出侧 / 运行时资源**单测（fake 宿主，不依赖 bpy / 游戏）。

覆盖四组验收：

A. 资源声明沿用 ZZMI 跨显卡不变量（typed RWBuffer + format + array=3×槽位数 +
   bind_flags；palette 形态 Buffer/stride=48 且绝不加 format）。
B. INI 与 HLSL **由同一份资源计划渲染**，逐字段一致；HLSL 按 ``bone*3`` /
   ``slot*3`` 索引、``GetDimensions`` 单参后 ``/3u`` 还原；``Toolset/``
   下随仓库冻结的那份与本模块渲染结果**逐字节一致**。
C. G1 家族硬门控（家族不全 ⇒ 整个家族不合并 + 大声诊断）+ 契约档位映射。
D. 导出接线：``vs-t0`` 重绑 + ``draw = <模组自己上传的行数，= 同产物 override_vertex_count>``
   只出现在通过 G1 的部件上（t48 修正：家族 Σ 只作诊断 —— t47 实机证伪了「draw = 家族 Σ」
   的旧前提）；无合并数据时与基类**逐行相同**；无 CS/attach/守卫/姿态指纹；
   新增代码不引用 ZZMI、不引用 bpy。

运行：``.venv\\Scripts\\python.exe -m pytest tests/test_himi_merged_runtime.py -q``
"""

import ast
import hashlib
import importlib.util
import io
import re
import json
import os
import shutil
import struct
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "himi_merged_runtime_test_pkg"

RUNTIME_REL = "common/himi_merged_runtime.py"
EXPORTER_REL = "ui/universal/himi.py"
HLSL_REL = "Toolset/himi_merged_skeleton_attach.hlsl"


# --------------------------------------------------------------------------- #
# fake 宿主：只把「被测模块的相对依赖」按 fake 包前缀登记
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

# 真实叶子模块（纯标准库 / numpy，无 bpy）。
_load_real_module(f"{PKG}.utils.json_utils", "utils/json_utils.py")
_load_real_module(f"{PKG}.utils.tbn_codec", "utils/tbn_codec.py")
_load_real_module(f"{PKG}.utils.format_utils", "utils/format_utils.py")
_load_real_module(f"{PKG}.common.m_ini_builder", "common/m_ini_builder.py")
_load_real_module(f"{PKG}.common.logic_name", "common/logic_name.py")
_load_real_module(f"{PKG}.common.global_key_count_helper", "common/global_key_count_helper.py")
_load_real_module(f"{PKG}.common.himi_merged_contract", "common/himi_merged_contract.py")
_load_real_module(f"{PKG}.common.himi_skeleton", "common/himi_skeleton.py")

# 运行期状态：工作空间 / mod 目录由每个测试设置。
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
    """基类替身：真实 ExportUnity 只用到 ``blueprint_model`` 与 ``drawib_model_list``。"""

    def __init__(self, blueprint_model, combine_ib=False):
        self.blueprint_model = blueprint_model
        self.drawib_model_list = []


_install_module(f"{PKG}.ui.universal.drawib_export_base", DrawIBExportBase=_FakeDrawIBExportBase)

_runtime = _load_real_module(f"{PKG}.common.himi_merged_runtime", RUNTIME_REL)
_contract = sys.modules[f"{PKG}.common.himi_merged_contract"]
_skeleton = sys.modules[f"{PKG}.common.himi_skeleton"]
#: 【t39-X】himi.py 在方法里延迟 import 它（fake 包的 common.__path__ 是空的 ⇒ 必须先登记）。
_family_x = _load_real_module(f"{PKG}.common.himi_family_x", "common/himi_family_x.py")
_unity = _load_real_module(f"{PKG}.ui.universal.unity", "ui/universal/unity.py")
_himi_exporter = _load_real_module(f"{PKG}.ui.universal.himi", EXPORTER_REL)
#: 【t66】按组名解析槽号的那个模块（`VertexGroupUtils.resolve_group_slot`）。
#: 它顶层 `import bpy`（只在函数体里用）与 `from mathutils import Vector`（Blender 自带模块）
#: ⇒ 先给最小替身（被测的 resolve_group_slot / get_blendweights_blendindices_v3 是纯函数，
#: 不需要真 bpy）。没有这一步，本机（无 Blender）整个模块在收集期就 ImportError。
if "bpy" not in sys.modules and importlib.util.find_spec("bpy") is None:
    _install_module("bpy")
if "mathutils" not in sys.modules:
    class _FakeVector(tuple):
        def __new__(cls, values=(0.0, 0.0, 0.0)):
            return super().__new__(cls, tuple(values))

    _install_module("mathutils", Vector=_FakeVector)
_vertexgroup = _load_real_module(
    f"{PKG}.utils.vertexgroup_utils", "utils/vertexgroup_utils.py"
)

ExportHIMI = _himi_exporter.ExportHIMI
ExportUnity = _unity.ExportUnity
M_IniBuilder = sys.modules[f"{PKG}.common.m_ini_builder"].M_IniBuilder
M_IniSection = sys.modules[f"{PKG}.common.m_ini_builder"].M_IniSection
M_SectionType = sys.modules[f"{PKG}.common.m_ini_builder"].M_SectionType


def _read_text(relpath: str) -> str:
    with io.open(REPO_ROOT / relpath, encoding="utf-8") as handle:
        return handle.read()


# --------------------------------------------------------------------------- #
# t57：attach CS 的新口径判据（先 strip 注释，再判结构）
# --------------------------------------------------------------------------- #
#
# 为什么必须先 strip 注释：本项目的 CS 抬头**故意**写着「不要用 StructuredBuffer<48B>」
# 「**绝不读 IniParams**」「Dispatch 由 ini 段给出」这类**反例说明**。对全文做子串搜索会把
# 注释里的字样当成真实代码 —— 旧断言 `test_hlsl_has_no_cs_and_no_mount` 就是被这个踩中的
# （t55 把该文件做成真 CS 之后，它断言的「不得含 numthreads / [CustomShader / 不被任何 ini
# 段引用」这类**过渡态**前提已作废）。
def _strip_hlsl_comments(text: str) -> str:
    """去掉 HLSL 的 ``//`` 行注释与 ``/* */`` 块注释。"""
    import re as _re

    without_block = _re.sub(r"/\*.*?\*/", "", str(text or ""), flags=_re.S)
    return _re.sub(r"//[^\n]*", "", without_block)


def _attach_cs_problems(text: str) -> list:
    """t57 的 attach CS 判据：真 CS 结构 + typed 3× 索引 + 零 IniParams（**纯函数**）。

    返回违规清单（空 = 通过）。两态证明
    （``HIMIIniAndHlslAgreementTests::test_attach_cs_predicate_is_not_vacuous``）直接复用它：
    把写侧改回结构化取址 / 去掉读侧 ``*3`` / 加上 ``IniParams`` 读参，每一条都必须报错。
    """
    code = _strip_hlsl_comments(text)
    problems: list = []
    if "[numthreads(64, 1, 1)]" not in code:
        problems.append("缺 [numthreads(64, 1, 1)]（与 Dispatch = ceil(槽数/64) 配套）")
    if "void main(" not in code:
        problems.append("缺 void main(...) 入口")
    if "SV_DispatchThreadID" not in code:
        problems.append("缺 SV_DispatchThreadID（块搬运的线程映射）")
    if "#define HIMI_ATTACH_BASE 0" not in code:
        problems.append("缺 #define HIMI_ATTACH_BASE（基址由导出侧写进文件，不走 ini 参数）")
    if "bone_index * 3u" not in code:
        problems.append("读侧必须按 bone*3 索引（typed 16 B 视图）")
    if "(HIMI_ATTACH_BASE + bone_index) * 3u" not in code:
        problems.append("写侧必须按 slot*3 索引（slot = 基址 + 本族骨号）")
    if "GetDimensions(palette_elements);" not in code:
        problems.append("槽数必须取资源自身维度（GetDimensions 单参）")
    if "palette_elements / 3u" not in code:
        problems.append("单参 GetDimensions 之后必须 /3u 还原槽数")
    if "GetDimensions(palette_elements, " in code:
        problems.append("不得出现两参 GetDimensions（那是结构化声明口径）")
    if "IniParams" in code:
        problems.append("CS 不得读 IniParams（fork 相关 ⇒ 读错位置只会读到未设置的 0）")
    if "StructuredBuffer" in code:
        problems.append("不得出现结构化声明（typed 16 B 与结构化 48 B 混用会落 D3D11 未定义区）")
    if "[CustomShader" in code:
        problems.append("CS 文件里不得声明 ini 段（段在 ini 侧）")
    if "Dispatch =" in code or "Dispatch(" in code:
        # 注意：`SV_DispatchThreadID` 里也含 "Dispatch"，所以判据必须带 `=` / `(`
        problems.append("Dispatch 由 ini 段给出，CS 里不得出现")
    return problems


# --------------------------------------------------------------------------- #
# 夹具
# --------------------------------------------------------------------------- #

GAMETYPE = "GPU_P12_N12_C4_TA16_BW16_BI16_T8_T1-8_"
SLOT_COUNT = 8
DIGEST = ""
SKELETON_BYTES = b""


def _skeleton_bytes(slot_count: int) -> bytes:
    """Deterministic 48 B/slot payload (bit pattern is irrelevant to the plan)."""
    payload = bytearray()
    for slot in range(slot_count):
        for index in range(12):
            payload += struct.pack("<f", float(slot * 12 + index))
    return bytes(payload)


def _blend_record(weights, indices) -> bytes:
    """One 32 B Blend record: 4×f32 weights + 4×i32 indices."""
    payload = struct.pack("<4f", *weights)
    payload += struct.pack("<4i", *indices)
    return payload


def _component_payload(
    *,
    bare: str,
    draw_ib: str,
    vg_count: int,
    original_vertex_count: int,
    family_members,
    blend_hash: str = "8322ac5a",
    slots: int = SLOT_COUNT,
    digest: str = "",
    gate_safe: bool = True,
    vg_map=None,
    rows: int = 0,
    local_indices=None,
):
    """A landed component json (+ its raw game Blend.buf when ``rows`` > 0)."""
    if vg_map is None:
        vg_map = {i: i for i in range(int(vg_count))}
    payload = {
        "GamePreset": "HI3",
        "VertexLimitVB": "2c5605ed",
        "CategoryHash": {"Blend": blend_hash, "Position": "2b325128", "Texcoord": "84611940"},
        "CategoryDrawCategoryMap": {"Blend": "Blend", "Position": "Position", "Texcoord": "Texcoord"},
        "WorkGameType": GAMETYPE.rstrip("_"),
        "GPU-PreSkinning": True,
        "VertexCount": 0,
        "VGCount": int(vg_count),
        "VGOffset": 0,
        "VGMap": {str(k): int(v) for k, v in dict(vg_map).items()},
        "OriginalVertexCount": int(original_vertex_count),
        "DeformDrawIndex": 35,
        "SkeletonGroup": 0,
        "HIMIDeformFamilyIB": draw_ib,
        "HIMIDeformFamilyMembers": list(family_members),
        "HIMIDeformGateSafe": bool(gate_safe),
        "HIMIDeformGateVersion": 1,
        "HIMIDeformJoinPath": "C",
        "HIMIMergedSkeletonSlots": int(slots),
        "HIMIMergedSkeletonDigest": str(digest),
        "_LocalBlendIndices": list(local_indices or []),
        "_LocalBlendRows": int(rows),
    }
    return payload


def _fixture_placement(bare: str) -> list:
    """夹具用的族级摆放 ``Q_f``（3×4 行主序）：**可逆**、含旋转 + 平移 + 非 1 尺度。

    为什么夹具也要给：t39 的导出期硬门要求「每族的 ``Q_f`` 可得且可逆」，否则 Fatal
    （宁可中止，也不产出"只校准一半"的产物）。不同 bare 给不同矩阵，才能验证
    「每族各自用各自的 Q」而不是全局一个常量。
    """
    import math

    index = sum(ord(ch) for ch in str(bare)) % 7
    angle = math.radians(20.0 + 15.0 * index)
    scale = 1.6
    cos, sin = math.cos(angle) * scale, math.sin(angle) * scale
    return [
        cos, -sin, 0.0, 0.25 + 0.1 * index,
        sin, cos, 0.0, -0.15 + 0.05 * index,
        0.0, 0.0, scale, 0.5,
    ]


def _make_workspace(
    root: str,
    role_name: str,
    components,
    *,
    slot_count: int = SLOT_COUNT,
    algorithm_version: int = 1,
    gate_version: int = 1,
    write_skeleton: bool = True,
    lod: str = "LOD0",
    write_palette_cache: bool = False,
) -> dict:
    """Build a HIMI workspace the export side can read (cache-only, no dump)."""
    os.makedirs(os.path.join(root, "Config"), exist_ok=True)
    role_key = _skeleton.derive_role_key(role_name)
    payload = _skeleton_bytes(slot_count)
    digest = hashlib.sha256(payload).hexdigest()
    skeleton_name = f"{role_key}-MergedSkeleton.buf"
    if write_skeleton:
        with open(os.path.join(root, "Config", skeleton_name), "wb") as handle:
            handle.write(payload)
    import_map = {}
    for component in components:
        bare = component["bare"]
        folder = os.path.join(root, lod, bare, GAMETYPE)
        os.makedirs(folder, exist_ok=True)
        component = dict(component)
        component["digest"] = component.get("digest") or digest
        component["slots"] = slot_count
        payload_dict = _component_payload(**component)
        rows = int(payload_dict.pop("_LocalBlendRows", 0) or 0)
        local_indices = list(payload_dict.pop("_LocalBlendIndices", []) or [])
        with io.open(os.path.join(folder, f"{bare}.json"), "w", encoding="utf-8") as handle:
            json.dump(payload_dict, handle, ensure_ascii=False)
        if write_palette_cache:
            # 【t39】族级校准（Q_f）的来源：`<component>/ModImpRuntime/<bare>-BoneMatrix.buf`。
            # 注意：这份缓存**必须与角色级骨架逐字节一致**（`verify_palette_cache_against_skeleton`），
            # 否则部件会被判 stale、计划里没有 rebind —— 所以只有显式要求时才写
            # （默认不写 ⇒ 走 t39 的「一个都拿不到 ⇒ 两侧都不校准」路径 = 与改造前逐字节相同）。
            slot0 = _fixture_placement(bare)
            rows = [bytes(48) for _ in range(int(slot_count))]
            rows[0] = struct.pack("<12f", *slot0)
            for slot in range(1, min(4, int(slot_count))):
                rows[slot] = struct.pack("<12f", *_fixture_placement(f"{bare}#{slot}"))
            runtime_dir = os.path.join(root, lod, bare, "ModImpRuntime")
            os.makedirs(runtime_dir, exist_ok=True)
            with open(os.path.join(runtime_dir, f"{bare}-BoneMatrix.buf"), "wb") as handle:
                handle.write(b"".join(rows))
        if rows > 0:
            # 工作空间里那份**原部件** Blend（游戏局部骨骼编号）：核对命名空间的对照物。
            records = bytearray()
            for row in range(rows):
                indices = local_indices[row % len(local_indices)] if local_indices else [0, 0, 0, 0]
                records += _blend_record((1.0, 0.0, 0.0, 0.0), list(indices)[:4])
            with open(os.path.join(folder, f"{bare}-Blend.buf"), "wb") as handle:
                handle.write(bytes(records))
        import_map[f"{lod}.{bare}"] = GAMETYPE
    with io.open(os.path.join(root, "Import.json"), "w", encoding="utf-8") as handle:
        json.dump(import_map, handle, ensure_ascii=False)
    metadata = {
        "AlgorithmVersion": int(algorithm_version),
        "GateVersion": int(gate_version),
        "RoleKey": role_key,
        "SlotCount": int(slot_count),
        "SkeletonFileName": skeleton_name,
        "SkeletonDigest": digest,
        "SourceLogSignature": "deadbeef",
        "ComponentCount": len(components),
    }
    with io.open(os.path.join(root, "Config", "HIMIMergedSkeleton.json"), "w", encoding="utf-8") as handle:
        json.dump(metadata, handle, ensure_ascii=False)
    return {
        "root": root,
        "role_key": role_key,
        "digest": digest,
        "skeleton_name": skeleton_name,
        "skeleton_bytes": payload,
        "metadata": metadata,
    }


class _FakeElement:
    def __init__(self, semantic_name, category, byte_width):
        self.SemanticName = semantic_name
        self.Category = category
        self.ByteWidth = int(byte_width)
        self.AlignedByteOffset = 0


class _FakeGameType:
    OrderedCategoryNameList = ["Position", "Texcoord", "Blend"]
    CategoryDrawCategoryDict = {"Blend": "Blend", "Position": "Position", "Texcoord": "Texcoord"}
    CategoryExtractSlotDict = {"Position": "vb0", "Blend": "vb1", "Texcoord": "vb1"}
    CategoryStrideDict = {"Position": 44, "Texcoord": 16, "Blend": 32}
    # 基类 IB 段会读它（t54 起本文件要测 IB 段的 run 插入）
    GPU_PreSkinning = False
    # Blend 记录：4xf32 权重（偏移 0，16B）+ 4xi32 索引（偏移 16，16B）
    D3D11ElementList = [
        _FakeElement("POSITION", "Position", 12),
        _FakeElement("NORMAL", "Position", 12),
        _FakeElement("COLOR", "Position", 4),
        _FakeElement("TANGENT", "Position", 16),
        _FakeElement("BLENDWEIGHTS", "Blend", 16),
        _FakeElement("BLENDINDICES", "Blend", 16),
        _FakeElement("TEXCOORD", "Texcoord", 8),
        _FakeElement("TEXCOORD", "Texcoord", 8),
    ]


def _blend_buffer_bytes(rows) -> bytes:
    """Exported Blend bytes for a list of index quadruples (same layout as the game)."""
    payload = bytearray()
    for indices in rows:
        payload += _blend_record((1.0, 0.0, 0.0, 0.0), list(indices)[:4])
    return bytes(payload)


def _component_json(workspace: str, bare: str, lod: str = "LOD0") -> dict:
    """读回工作空间里那份已落盘组件 json（与真实导出侧同一份数据）。"""
    path = _skeleton.resolve_submesh_json_path(workspace, f"{lod}.{bare}")
    with io.open(path, encoding="utf-8") as handle:
        return json.load(handle)


def _submesh(bare: str, *, vg_count: int, lod: str = "LOD0", original_vertex_count: int = 0):
    return types.SimpleNamespace(
        workspace_unique_str=f"{lod}.{bare}",
        unique_str=f"{lod}.{bare}",
        vg_count=int(vg_count),
        original_vertex_count=int(original_vertex_count),
        # 基类 IB 段用到的字段（t54 起本文件要测 IB 段的 run 插入）
        match_first_index=0,
        drawcall_model_list=[],
    )


def _drawib_model(
    draw_ib: str,
    submeshes,
    *,
    draw_number: int = 100,
    blend_hash: str = "8322ac5a",
    blend_rows=None,
):
    return types.SimpleNamespace(
        draw_ib=draw_ib,
        draw_ib_alias="0",
        draw_number=int(draw_number),
        category_hash_dict={"Position": "2b325128", "Texcoord": "84611940", "Blend": blend_hash},
        d3d11GameType=_FakeGameType(),
        submesh_model_list=list(submeshes),
        # 基类资源段用到的两个访问器（真实 DrawIBModel 有）
        get_submesh_ib_resource_name=lambda submesh: "Resource" + (submesh.workspace_unique_str or submesh.unique_str) + "-Index",
        submesh_ib_dict={},
        # 基类 IB 段用到的访问器（t54 起本文件要测 IB 段的 run 插入）
        get_submesh_texture_override_suffix=lambda submesh: (
            "IB_" + draw_ib + "_" + str(submesh.workspace_unique_str or submesh.unique_str)
        ),
        get_submesh_texture_markup_info_list=lambda submesh: [],
        obj_name_draw_offset={},
        # 本次导出真正写出的 VB（numpy uint8 扁平数组 → 这里用同形状的 bytes 替身）
        category_buffer_dict=(
            {}
            if blend_rows is None
            else {"Blend": _blend_buffer_bytes(blend_rows)}
        ),
    )


def _builder_lines(builder) -> list:
    lines = []
    for section in builder.ini_section_list:
        if section.SectionName:
            lines.append(f"[{section.SectionName}]")
        lines.extend(section.SectionLineList)
    return lines


class HIMIMergedRuntimeBase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.mkdtemp(prefix="himi_merged_runtime_")
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.workspace = os.path.join(self._tmp, "RoleA")
        self.mod_folder = os.path.join(self._tmp, "mod")
        os.makedirs(self.workspace, exist_ok=True)
        os.makedirs(self.mod_folder, exist_ok=True)
        _GLOBAL_STATE["workspace"] = self.workspace
        _GLOBAL_STATE["mod"] = self.mod_folder
        _properties_state["checkbox"] = True
        _fake_export_helper.current_buffer_folder_name = "Meshes"

    def make_exporter(self, models, *, plan_workspace=None):
        blueprint_model = types.SimpleNamespace(keyname_mkey_dict={}, ordered_draw_obj_data_model_list=[])
        exporter = ExportHIMI(blueprint_model)
        exporter.drawib_model_list = list(models)
        if plan_workspace is not None:
            _GLOBAL_STATE["workspace"] = plan_workspace
        return exporter


# --------------------------------------------------------------------------- #
# A. 资源声明：跨显卡不变量
# --------------------------------------------------------------------------- #


class HIMIResourceDeclTests(unittest.TestCase):
    def test_typed_invariants(self):
        decl = _runtime.build_merged_skeleton_decl("role_ab12", 343)
        self.assertEqual(decl.name, "ResourceHI3MergedSkeleton_role_ab12")
        self.assertEqual(decl.ini_type, "RWBuffer")
        self.assertEqual(decl.ini_format, "R32G32B32A32_FLOAT")
        self.assertEqual(decl.array, 343 * 3)
        self.assertEqual(decl.bind_flags, "shader_resource unordered_access")
        self.assertEqual(decl.stride, 0)
        self.assertEqual(decl.byte_width, 343 * 48)
        # t51：骨架是**活资源**（每帧由 attach CS 写入）⇒ 不得声明 filename，
        # 否则加载器按静态文件创建它、内容永远不更新（姿态停在文件里的那一帧）。
        self.assertEqual(decl.filename, "", "活骨架不得带 filename（t51 裁定）")
        self.assertNotIn("filename", decl.ini_fields(), "ini 段里连这个键都不该出现")

    def test_ini_block_emits_exactly_the_expected_keys(self):
        decl = _runtime.build_merged_skeleton_decl("role_ab12", 4)
        parsed = _runtime.parse_ini_resource_block(decl.ini_lines())
        self.assertEqual(
            set(parsed) - {"name"}, {"type", "format", "array", "bind_flags"}
        )
        self.assertNotIn("stride", parsed, "合并骨架绝对不能声明 stride（A3）")
        self.assertNotIn(
            "filename", parsed, "活骨架不得声明 filename（t51；每帧由 attach CS 填充）"
        )

    def test_stride_is_rejected(self):
        decl = _runtime.build_merged_skeleton_decl("role_ab12", 4)
        import dataclasses

        with self.assertRaises(_runtime.HIMIResourceDeclError):
            _runtime.validate_resource_decl(dataclasses.replace(decl, stride=48))

    def test_array_must_be_three_slots(self):
        decl = _runtime.build_merged_skeleton_decl("role_ab12", 4)
        import dataclasses

        with self.assertRaises(_runtime.HIMIResourceDeclError):
            _runtime.validate_resource_decl(dataclasses.replace(decl, array=4))

    def test_bind_flags_required_and_both_uses(self):
        import dataclasses

        decl = _runtime.build_merged_skeleton_decl("role_ab12", 4)
        with self.assertRaises(_runtime.HIMIResourceDeclError):
            _runtime.validate_resource_decl(dataclasses.replace(decl, bind_flags=""))
        with self.assertRaises(_runtime.HIMIResourceDeclError):
            _runtime.validate_resource_decl(
                dataclasses.replace(decl, bind_flags="unordered_access")
            )
        with self.assertRaises(_runtime.HIMIResourceDeclError):
            _runtime.validate_resource_decl(dataclasses.replace(decl, bind_flags="shader_resource"))

    def test_palette_shape_guard(self):
        palette = _runtime.build_palette_decl("ResourceZZPalette_x", 41, "Meshes/x.buf")
        lines = palette.ini_lines()
        text = "\n".join(lines)
        self.assertIn("type = Buffer", text)
        self.assertIn("stride = 48", text)
        self.assertIn("array = 41", text)
        self.assertNotIn("format", text, "palette 绝不加 format（会把 SBS 改成 16 → copy 溢出）")
        self.assertNotIn("format", _runtime.parse_ini_resource_block(lines))

    def test_palette_format_is_rejected(self):
        import dataclasses

        palette = _runtime.build_palette_decl("Pal", 4, "Meshes/p.buf")
        with self.assertRaises(_runtime.HIMIResourceDeclError):
            _runtime.validate_resource_decl(
                dataclasses.replace(palette, ini_format="R32G32B32A32_FLOAT")
            )
        with self.assertRaises(_runtime.HIMIResourceDeclError):
            _runtime.validate_resource_decl(dataclasses.replace(palette, stride=16))

    def test_slot_count_limit(self):
        with self.assertRaises(_runtime.HIMIResourceDeclError):
            _runtime.build_merged_skeleton_decl(
                "role", _skeleton.HIMI_MERGED_SKELETON_SLOT_LIMIT + 1
            )
        with self.assertRaises(_runtime.HIMIResourceDeclError):
            _runtime.build_merged_skeleton_decl("role", 0)

    def test_folder_override_keeps_basename(self):
        """目录覆写只对**带 filename 的形态**有意义。

        t51 之后合并骨架是活资源（无 filename）⇒ 目录对它**无效**（也不该凭空造出
        一个 filename）；palette 形态（家族副本 / 家族 palette）仍按目录覆写并保留
        文件名 —— 这条覆盖「覆写不会把 basename 丢掉」原意。
        """
        decl = _runtime.build_merged_skeleton_decl("role_ab12", 4)
        moved = _runtime.render_resource_ini_lines(decl, folder="Meshes0000")
        parsed = _runtime.parse_ini_resource_block(moved)
        self.assertNotIn(
            "filename", parsed, "活骨架不得因为目录覆写而变回静态文件（t51）"
        )
        palette = _runtime.build_palette_decl("Pal", 4, "Meshes/p.buf")
        moved_palette = _runtime.render_resource_ini_lines(palette, folder="Meshes0000")
        self.assertEqual(
            _runtime.parse_ini_resource_block(moved_palette)["filename"],
            "Meshes0000/p.buf",
        )


# --------------------------------------------------------------------------- #
# B. INI 与 HLSL 同源
# --------------------------------------------------------------------------- #


class HIMIIniAndHlslAgreementTests(unittest.TestCase):
    def _compare(self, slot_count, role_key, folder=None):
        decl = _runtime.build_merged_skeleton_decl(role_key, slot_count)
        arg = {"folder": folder} if folder else {}
        ini = _runtime.parse_ini_resource_block(decl.ini_lines())
        hlsl_text = _runtime.render_attach_hlsl(decl, **arg)
        hlsl = _runtime.parse_hlsl_resource_decl(hlsl_text)
        self.assertEqual(hlsl["name"], ini["name"])
        self.assertEqual(hlsl["type"], ini["type"])
        self.assertEqual(hlsl["format"], ini["format"])
        self.assertEqual(hlsl["array"], ini["array"])
        self.assertEqual(hlsl["filename"], "none", "活骨架：机器可读行必须记 filename=none")
        self.assertNotIn(
            "filename", ini, "活骨架的 ini 段不得声明 filename（t51；每帧 attach CS 填充）"
        )
        self.assertEqual(hlsl["bind_flags"], ini["bind_flags"])
        self.assertEqual(hlsl["slots"], str(slot_count))
        self.assertEqual(hlsl["bytes"], str(slot_count * 48))
        self.assertEqual(hlsl["stride"], "none")
        self.assertNotIn("stride", ini)
        return ini, hlsl, hlsl_text

    def test_field_by_field_agreement(self):
        for slots in (1, 4, 276, 343, 1405):
            self._compare(slots, f"role_{slots}")

    def test_hlsl_object_declaration_is_typed_and_named(self):
        """【t57 改写 · 旧前提 / 新口径 / 理由】

        旧：对**全文**断言 `"StructuredBuffer" not in text` 与
        `"stride = 48" not in text.split("机器可读声明行")[-1]`。
        新：先 strip 注释再判 —— 抬头现在**故意**写着「不要用 StructuredBuffer<48B>」
        「ini 侧该副本必须是 type = Buffer / stride = 48」这类反例说明，全文搜索会把它们
        当成真实代码（假阳性）。判据本身不变（typed 声明必须在、结构化声明不得在），
        且加了 palette 侧的 typed 声明 —— 强度只增不减。
        """
        ini, _hlsl, text = self._compare(343, "role_ab12")
        code = _strip_hlsl_comments(text)
        self.assertIn(f"RWBuffer<float4> {ini['name']} : register(u0);", code)
        self.assertIn("Buffer<float4> himi_src_palette : register(t0);", code)
        self.assertNotIn("StructuredBuffer", code)
        self.assertNotIn("RWStructuredBuffer", code)
        # 资源声明不得带 ini 风格的 stride 字段（合并骨架是 typed：array = 3 × 槽位数）
        self.assertNotIn("stride =", code.lower())

    def test_hlsl_index_forms(self):
        """【t57 改写 · 旧前提 / 新口径 / 理由】

        旧：断言旧「声明与索引参考」里的 helper 文本 —— `"bone * 3u"` / `"slot * 3u"` /
        `".GetDimensions(elements);"` / `"return elements / 3u;"`。那四个 helper
        （``HIMI_MergedSkeletonLoadBone`` 等）随 t57 的 CS 换代一起消失。
        新：按 **CS 正文**的三处索引与 3× 维度还原断言，并补上「块搬运」的三写三读 ——
        与旧断言**等价强度**（都要求 typed `*3` 取址 + 单参 GetDimensions + `/3u`），
        且先 strip 注释 ⇒ 比旧版更强（旧版那两条 `bone * 3u` / `slot * 3u` 在旧文本里
        也是**注释**里的字样）。
        """
        text = _runtime.render_attach_hlsl(
            _runtime.build_merged_skeleton_decl("role_ab12", 343)
        )
        code = _strip_hlsl_comments(text)
        self.assertIn("bone_index * 3u", code, "读侧必须按 bone*3 索引")
        self.assertIn(
            "(HIMI_ATTACH_BASE + bone_index) * 3u", code, "写侧必须按 slot*3 索引"
        )
        self.assertIn("himi_src_palette.GetDimensions(palette_elements);", code)
        self.assertIn("uint count = palette_elements / 3u;", code, "单参 GetDimensions 后必须 /3u 还原")
        self.assertIn(
            f"{_runtime.build_merged_skeleton_decl('role_ab12', 343).name}"
            ".GetDimensions(skeleton_elements);",
            code,
            "骨架侧也必须取资源自身维度",
        )
        # 单参 GetDimensions：不得出现两参形式（那是结构化声明口径）
        self.assertNotIn("GetDimensions(palette_elements, ", code)
        # 块搬运：每根骨 3 个 float4，两端都显式按 *3 取址
        for offset in (0, 1, 2):
            self.assertIn(
                f"[dst + {offset}u] = himi_src_palette[src + {offset}u];",
                code,
                "块搬运必须逐元素搬 3 个 float4（4×3 行主序矩阵）",
            )

    def test_hlsl_is_a_real_attach_cs(self):
        """【t57 改写 + 改名登记 · 旧名 `test_hlsl_has_no_cs_and_no_mount`】

        旧：断言「不得含 numthreads / sv_dispatchthreadid / cs-t0 / cs-u0 / cs = / cs= /
        [CustomShader」+「必须含『不被任何 ini 段引用』」—— 那是 **HIMI 没有 CS 路径**的
        过渡态前提（五帧 dump 零 CS / 零 Dispatch），t55 已把该文件做成真 attach CS。
        新：把同一批 token 反过来钉死，且**不该有的一个不多**：
        CS 该有的（numthreads / main / SV_DispatchThreadID / 基址 #define / 块搬运）一个不少；
        零 IniParams、文件里不得声明 ini 段、Dispatch 只在 ini 段。名字随之改成真话
        （改名已在 reports/himi-merge/28-perframe-attach.md §11 登记，**未删除任何断言**）。
        """
        text = _runtime.render_attach_hlsl(
            _runtime.build_merged_skeleton_decl("role_ab12", 343)
        )
        code = _strip_hlsl_comments(text)
        self.assertEqual(_attach_cs_problems(text), [], "新口径判据必须全过")
        self.assertNotIn("IniParams", code, "CS 绝不读 IniParams（fork 相关 ⇒ 静默不写）")
        self.assertNotIn("[CustomShader", code, "段声明在 ini 侧，不在 CS 文件里")
        self.assertNotIn("Dispatch =", code, "Dispatch 由 ini 段给出")
        self.assertNotIn("Dispatch(", code, "Dispatch 由 ini 段给出")
        self.assertIn("SV_DispatchThreadID", code, "CS 自己的线程 ID 语义（与 ini 的 Dispatch 无关）")
        # 每族一个 CS 文件的形态 + 基址 #define（与导出侧同一份 helper 同源）
        self.assertIn(
            _runtime.family_attach_hlsl_path("<fam>"), text, "抬头必须写清每族一个 CS 文件"
        )
        self.assertIn(_runtime.FAMILY_ATTACH_BASE_DEFINE, text)
        self.assertIn(_runtime.render_family_attach_base_line(0), code)
        # 抬头必须写清真实机制链（①捕获 → ②attach 写骨架 → ③重绑）
        for token in ("①", "②", "③", "ResourceHI3PaletteFam_<fam> = copy vs-t0 unless_null"):
            self.assertIn(token, text, f"抬头必须写清机制链（缺 {token}）")
        # 旧静态口径表述必须删净
        for stale in ("静态合并骨架", "不被任何 ini 段引用", "没有任何写方", "为什么没有 attach CS"):
            self.assertNotIn(stale, text, f"旧静态口径表述必须删净（{stale}）")

    def test_hlsl_records_unverified_items(self):
        """【t57 强化】保留「V1/V2/V3 必须在场 + 不得过度声称」，并补上机制链契约
        （捕获行方向 / run 必须在前 / 逐帧原因）——抬头现在承担的是「真实机制说明」。"""
        text = _runtime.render_attach_hlsl(
            _runtime.build_merged_skeleton_decl("role_ab12", 343)
        )
        for token in ("V1", "V2", "V3"):
            self.assertIn(token, text)
        for overclaim in ("实机通过", "已实机验证", "实测通过"):
            self.assertNotIn(overclaim, text, f"不得声称 {overclaim}")
        self.assertIn("copy vs-t0 unless_null", text, "捕获行方向必须写在抬头里（LHS = 目标）")
        self.assertIn("反序", text, "必须写明 run 排到 vs-t0 之后 = 自反馈")
        self.assertIn("每帧", text, "必须写明为什么逐帧写（palette 是每帧姿态数据）")

    def test_attach_cs_predicate_is_not_vacuous(self):
        """两态证明（t57）：判据喂进三种坏形态都必须报错 ⇒ 不是恒真。

        ① 把**写侧改回结构化取址**（``(HIMI_ATTACH_BASE + bone_index) * 3u`` → 去掉 ``* 3u``）
           —— 这正是跨显卡不变量 A2 的反面（AMD 会按 48 取址 ⇒ 越界读返回 0）；
        ② 读侧去掉 ``*3`` 缩放；
        ③ 加上 ``IniParams`` 读参（fork 相关 ⇒ 读错位置只会读到未设置的 0）。
        """
        text = _runtime.render_attach_hlsl(
            _runtime.build_merged_skeleton_decl("role_ab12", 343)
        )
        self.assertEqual(_attach_cs_problems(text), [], "正例（真实渲染文本）必须无错")
        cases = {
            "写侧改回结构化取址": text.replace(
                "(HIMI_ATTACH_BASE + bone_index) * 3u", "(HIMI_ATTACH_BASE + bone_index)"
            ),
            "读侧去掉 *3 缩放": text.replace("bone_index * 3u", "bone_index"),
            "加上 IniParams 读参": text.replace(
                "uint count = palette_elements / 3u;",
                "uint count = palette_elements / 3u;\n    uint bad = (uint)IniParams[1].y;",
            ),
        }
        for label, broken in cases.items():
            self.assertNotEqual(broken, text, f"{label}：变异必须真的改到文本")
            self.assertTrue(_attach_cs_problems(broken), f"{label} ⇒ 判据必须报错（非恒真）")

    def test_checked_in_toolset_hlsl_matches_renderer(self):
        checked_in = _read_text(HLSL_REL)
        self.assertEqual(
            checked_in,
            _runtime.reference_attach_hlsl(),
            "Toolset/himi_merged_skeleton_attach.hlsl 必须由同一份声明渲染，不得手改漂移",
        )

    def test_checked_in_hlsl_is_reference_decl(self):
        parsed = _runtime.parse_hlsl_resource_decl(_read_text(HLSL_REL))
        self.assertEqual(parsed["name"], "ResourceHI3MergedSkeleton_REFERENCE")
        self.assertEqual(parsed["slots"], str(_runtime.REFERENCE_SLOT_COUNT))
        self.assertEqual(
            parsed["array"], str(_runtime.REFERENCE_SLOT_COUNT * 3)
        )


# --------------------------------------------------------------------------- #
# C. 计划 + G1 + 契约
# --------------------------------------------------------------------------- #


class HIMIPlanFromMetadataTests(unittest.TestCase):
    def _metadata(self, slots=SLOT_COUNT):
        payload = _skeleton_bytes(slots)
        return {
            "AlgorithmVersion": 1,
            "GateVersion": 1,
            "RoleKey": "role_ab12",
            "SlotCount": slots,
            "SkeletonFileName": "role_ab12-MergedSkeleton.buf",
            "SkeletonDigest": hashlib.sha256(payload).hexdigest(),
        }, payload

    def _part(self, bare, *, family_members, draw_ib="aaaa1111", **kwargs):
        data = {
            "bare": bare,
            "draw_ib": draw_ib,
            "vg_count": 4,
            "original_vertex_count": 100,
            "family": draw_ib,
            "family_members": tuple(family_members),
            "category_hash": "8322ac5a",
        }
        data.update(kwargs)
        return data

    def _oracle(self, safe_bares, rejected=None, *, stale=False, slot_count=SLOT_COUNT):
        """`HIMISkeletonMergeHelper.prepare()` 归一化后的权威门控结果（pass 级）。"""
        return {
            "available": True,
            "stale": bool(stale),
            "source": "prepare",
            "safe_bares": [str(name) for name in safe_bares],
            "rejected": dict(rejected or {}),
            "message": "",
            "slot_count": int(slot_count),
            "digest": "",
            "cache_intact": True,
        }

    def test_oracle_governs_the_rebind_set(self):
        """有权威门控时，可重绑集合 **完全** 由 prepare 的 writes 决定。"""
        metadata, payload = self._metadata()
        members = ["aaaa1111-1-0", "aaaa1111-2-0"]
        parts = [self._part(bare, family_members=members) for bare in members]
        plan = _runtime.plan_from_metadata(
            metadata, parts, skeleton_bytes=payload,
            target_bares=members,
            gate_oracle=self._oracle(members),
        )
        self.assertTrue(plan.ok, plan.message)
        self.assertEqual(len(plan.rebinds), 2)
        self.assertEqual(plan.rejected, {})
        self.assertEqual(plan.gate_source, "prepare")
        rebind = plan.rebind_for("aaaa1111")
        self.assertEqual(rebind["vs_t0_resource"], "ResourceHI3MergedSkeleton_role_ab12")
        self.assertEqual(rebind["deform_vertex_count"], 100)
        self.assertEqual(rebind["family_members"], members)

    def test_oracle_rejection_blocks_the_whole_pass_and_is_loud(self):
        """prepare 判定该 pass/家族不可合并 ⇒ 一率不重绑 + 大声诊断（T9-F4 侧）。"""
        metadata, payload = self._metadata()
        members = ["aaaa1111-1-0", "aaaa1111-2-0", "aaaa1111-3-0"]
        parts = [self._part(bare, family_members=members) for bare in members]
        plan = _runtime.plan_from_metadata(
            metadata, parts, skeleton_bytes=payload,
            target_bares=members[:2],
            gate_oracle=self._oracle(
                [],  # prepare 一个都没放行（pass 级：家族不完整）
                {
                    "aaaa1111": {
                        "family": "aaaa1111",
                        "members": members,
                        "missing": [members[2]],
                        "reason": "deform_pass_incomplete",
                    }
                },
            ),
        )
        self.assertEqual(plan.rebinds, [], "prepare 不放行时一个部件都不得重绑")
        self.assertEqual(list(plan.rejected), ["aaaa1111"])
        self.assertEqual(plan.rejected["aaaa1111"]["missing"], [members[2]])
        self.assertIn(_contract.DEFORM_GATE_REJECT_PREFIX, plan.diagnostics_text())
        self.assertIn(members[2], plan.diagnostics_text())
        self.assertIn("pass 级", plan.diagnostics_text())
        self.assertEqual(plan.component_count, 0)
        self.assertEqual(plan.parts_with_data, 3)

    def test_oracle_rejection_is_consumed_verbatim(self):
        """本模块不补充/推断 pass 关系：rejected 就照 prepare 的搬运。"""
        metadata, payload = self._metadata()
        parts = [self._part("aaaa1111-1-0", family_members=["aaaa1111-1-0"])]
        plan = _runtime.plan_from_metadata(
            metadata, parts, skeleton_bytes=payload, target_bares=[],
            gate_oracle=self._oracle(
                [], {"bbbb2222": {"family": "bbbb2222", "members": [], "missing": ["x-1-0"]}}
            ),
        )
        self.assertEqual(plan.rebinds, [])
        self.assertEqual(list(plan.rejected), ["bbbb2222"])
        self.assertIn("x-1-0", plan.diagnostics_text())

    def test_cache_only_fallback_uses_the_import_verdict(self):
        """没有权威门控但有全套缓存（导出覆盖全部已落盘部件）⇒ 沿用导入期判定。"""
        metadata, payload = self._metadata()
        members = ["aaaa1111-1-0", "aaaa1111-2-0"]
        parts = [self._part(bare, family_members=members) for bare in members]
        plan = _runtime.plan_from_metadata(
            metadata, parts, skeleton_bytes=payload, target_bares=members,
            landed_data_bares=set(members),
        )
        self.assertEqual(len(plan.rebinds), 2)
        self.assertEqual(plan.gate_source, "cache-only")
        self.assertIn("无法用 dump 在 pass 级重算", plan.diagnostics_text())

    def test_cache_only_fallback_refuses_a_subset_export(self):
        """dump 不可用 + 只导出部分部件 ⇒ 无法在 pass 级判定 ⇒ 一律不重绑。"""
        metadata, payload = self._metadata()
        members = ["aaaa1111-1-0", "aaaa1111-2-0"]
        parts = [self._part(members[0], family_members=members)]
        plan = _runtime.plan_from_metadata(
            metadata, parts, skeleton_bytes=payload, target_bares=[members[0]],
            landed_data_bares=set(members),  # 2-0 已落盘但没进本次导出
        )
        self.assertEqual(plan.rebinds, [])
        self.assertIn(_contract.DEFORM_GATE_REJECT_PREFIX, plan.diagnostics_text())
        self.assertIn(members[1], plan.diagnostics_text())
        self.assertIn("全部不重绑", plan.diagnostics_text())

    def test_cache_only_fallback_respects_the_cached_gate_flag(self):
        """缓存里 HIMIDeformGateSafe=False 的部件绝不重绑（并把理由报出来）。"""
        metadata, payload = self._metadata()
        parts = [
            self._part("aaaa1111-1-0", family_members=["aaaa1111-1-0"], gate_safe=True),
            self._part("bbbb2222-1-0", draw_ib="bbbb2222", family_members=["bbbb2222-1-0"],
                       gate_safe=False),
        ]
        plan = _runtime.plan_from_metadata(
            metadata, parts, skeleton_bytes=payload,
            target_bares=["aaaa1111-1-0", "bbbb2222-1-0"],
            landed_data_bares={"aaaa1111-1-0", "bbbb2222-1-0"},
        )
        self.assertEqual([item["bare"] for item in plan.rebinds], ["aaaa1111-1-0"])
        self.assertIn("bbbb2222", plan.rejected)

    def test_stale_oracle_falls_back_to_cache(self):
        """dump 反查结果与缓存不是同一次导入 ⇒ 不采用它（几何与骨架必须同源）。"""
        metadata, payload = self._metadata()
        members = ["aaaa1111-1-0"]
        parts = [self._part(members[0], family_members=members)]
        plan = _runtime.plan_from_metadata(
            metadata, parts, skeleton_bytes=payload, target_bares=members,
            landed_data_bares=set(members),
            gate_oracle=self._oracle(members, stale=True),
        )
        self.assertEqual(plan.gate_source, "cache-only")
        self.assertEqual(len(plan.rebinds), 1)

    def test_import_side_semantics_when_export_bares_is_none(self):
        metadata, payload = self._metadata()
        members = ["aaaa1111-1-0", "aaaa1111-2-0"]
        parts = [self._part(bare, family_members=members) for bare in members]
        plan = _runtime.plan_from_metadata(
            metadata, parts, skeleton_bytes=payload, gate_oracle=self._oracle(members)
        )
        self.assertEqual(len(plan.rebinds), 2)
        self.assertEqual(plan.rejected, {})

    def test_checkbox_off_blocks_everything_but_keeps_the_count(self):
        """【t71 改口径】开关关闭 ⇒ 计划照旧不启用（不消费合并数据），但**契约放行**（普通模式）。

        两态：开关关闭 ⇒ `warning`（不中止）；开关打开但全部被拒 ⇒ `error`（真阳性保留）。
        """
        metadata, payload = self._metadata()
        members = ["aaaa1111-1-0"]
        parts = [self._part(members[0], family_members=members)]
        plan = _runtime.plan_from_metadata(
            metadata, parts, skeleton_bytes=payload, checkbox_enabled=False,
        )
        self.assertFalse(plan.ok)
        self.assertEqual(plan.rebinds, [])
        self.assertEqual(plan.parts_with_data, 1)
        self.assertEqual(plan.component_count, 0)
        snapshot = plan.contract_snapshot(checkbox_enabled=False)
        decision = _contract.evaluate_merged_skeleton_contract(**snapshot)
        self.assertEqual(decision["level"], "warning", decision)
        self.assertIn("普通模式", decision["message"])
        on_rejected = _contract.evaluate_merged_skeleton_contract(
            **plan.contract_snapshot(checkbox_enabled=True)
        )
        self.assertEqual(on_rejected["level"], "error", "真阳性必须保留")

    def test_stale_algorithm_version(self):
        metadata, payload = self._metadata()
        metadata["AlgorithmVersion"] = 99
        plan = _runtime.plan_from_metadata(metadata, [], skeleton_bytes=payload)
        self.assertFalse(plan.ok)
        self.assertIn("版本", plan.message)

    def test_stale_gate_version(self):
        metadata, payload = self._metadata()
        metadata["GateVersion"] = 0
        plan = _runtime.plan_from_metadata(metadata, [], skeleton_bytes=payload)
        self.assertFalse(plan.ok)
        self.assertIn("门控版本", plan.message)

    def test_missing_metadata_field(self):
        metadata, payload = self._metadata()
        metadata["SkeletonFileName"] = ""
        plan = _runtime.plan_from_metadata(metadata, [], skeleton_bytes=payload)
        self.assertFalse(plan.ok)
        self.assertIn("不完整", plan.message)

    def test_skeleton_size_and_digest_are_verified(self):
        metadata, payload = self._metadata()
        short = _runtime.plan_from_metadata(metadata, [], skeleton_bytes=payload[:-1])
        self.assertFalse(short.ok)
        self.assertIn("字节数不符", short.message)
        tampered = _runtime.plan_from_metadata(metadata, [], skeleton_bytes=b"\x00" * len(payload))
        self.assertFalse(tampered.ok)
        self.assertIn("摘要", tampered.message)

    def test_stale_component_digest_blocks_the_whole_family(self):
        """同族里有一个部件的合并数据来自**另一次导入** ⇒ 整个家族不重绑。

        （这里不靠家族推演：陈旧部件被剔出「目标集合」，而退路的覆盖检查发现
        「已落盘带数据部件」没被目标集合覆盖 ⇒ 一律不重绑。）
        """
        metadata, payload = self._metadata()
        members = ["aaaa1111-1-0", "aaaa1111-2-0"]
        parts = [
            self._part(members[0], family_members=members, skeleton_digest=metadata["SkeletonDigest"]),
            self._part(members[1], family_members=members, skeleton_digest="0" * 64),
        ]
        plan = _runtime.plan_from_metadata(
            metadata, parts, skeleton_bytes=payload, target_bares=members,
        )
        self.assertEqual(plan.rebinds, [])
        self.assertIn("aaaa1111", plan.skip_reasons)
        self.assertIn(members[1], plan.diagnostics_text())

    def test_skip_reason_for_missing_deform_vertex_count(self):
        metadata, payload = self._metadata()
        parts = [self._part("aaaa1111-1-0", family_members=["aaaa1111-1-0"], original_vertex_count=0)]
        plan = _runtime.plan_from_metadata(
            metadata, parts, skeleton_bytes=payload, target_bares=["aaaa1111-1-0"],
            gate_oracle=self._oracle(["aaaa1111-1-0"]),
        )
        self.assertEqual(plan.rebinds, [])
        self.assertIn("OriginalVertexCount", plan.skip_reasons["aaaa1111"])

    def test_skip_reason_for_missing_blend_hash(self):
        metadata, payload = self._metadata()
        parts = [self._part("aaaa1111-1-0", family_members=["aaaa1111-1-0"], category_hash="")]
        plan = _runtime.plan_from_metadata(
            metadata, parts, skeleton_bytes=payload, target_bares=["aaaa1111-1-0"],
            gate_oracle=self._oracle(["aaaa1111-1-0"]),
        )
        self.assertEqual(plan.rebinds, [])
        self.assertIn("CategoryHash.Blend", plan.skip_reasons["aaaa1111"])

    def test_contract_levels_through_the_plan_snapshot(self):
        metadata, payload = self._metadata()
        members = ["aaaa1111-1-0", "aaaa1111-2-0"]
        other = ["bbbb2222-1-0", "bbbb2222-2-0"]
        all_parts = [self._part(bare, family_members=members) for bare in members] + [
            self._part(other[0], family_members=other, draw_ib="bbbb2222"),
            self._part(other[1], family_members=other, draw_ib="bbbb2222"),
        ]

        # （a）有数据 + prepare 全部放行 → ok（无诊断）
        plan = _runtime.plan_from_metadata(
            metadata, all_parts, skeleton_bytes=payload,
            target_bares=members + other,
            gate_oracle=self._oracle(members + other),
        )
        self.assertEqual(len(plan.rebinds), 4)
        decision = _contract.evaluate_merged_skeleton_contract(
            **plan.contract_snapshot(checkbox_enabled=True)
        )
        self.assertEqual(decision["level"], "ok")

        # （b）prepare 放行一个家族、拒绝另一个 → warning + pass/缺席成员写进 hint
        plan = _runtime.plan_from_metadata(
            metadata, all_parts, skeleton_bytes=payload,
            target_bares=members + other[:1],
            gate_oracle=self._oracle(
                members,
                {"bbbb2222": {"family": "bbbb2222", "members": other, "missing": [other[1]]}},
            ),
        )
        self.assertEqual(len(plan.rebinds), 2)
        decision = _contract.evaluate_merged_skeleton_contract(
            **plan.contract_snapshot(checkbox_enabled=True)
        )
        self.assertEqual(decision["level"], "warning")
        self.assertIn(_contract.DEFORM_GATE_REJECT_PREFIX, decision["hint"])
        self.assertIn(other[1], decision["hint"])

        # （c）全部被拒 → error
        plan = _runtime.plan_from_metadata(
            metadata, all_parts, skeleton_bytes=payload, target_bares=[],
            gate_oracle=self._oracle([]),
        )
        decision = _contract.evaluate_merged_skeleton_contract(
            **plan.contract_snapshot(checkbox_enabled=True)
        )
        self.assertEqual(decision["level"], "error")

        # （d）没有数据 → notice（普通导出，不打断）
        empty = _runtime.plan_from_metadata(metadata, [], skeleton_bytes=payload)
        decision = _contract.evaluate_merged_skeleton_contract(
            **empty.contract_snapshot(checkbox_enabled=True)
        )
        self.assertEqual(decision["level"], "notice")

    def test_family_deform_vertex_count_is_summed_over_the_family(self):
        """一个 IB 家族只有一段 VB 段 / 一笔 draw ⇒ 行数 = 各分片之和。"""
        metadata, payload = self._metadata()
        members = ["aaaa1111-1-0", "aaaa1111-2-0"]
        parts = [
            self._part(members[0], family_members=members, original_vertex_count=42912),
            self._part(members[1], family_members=members, original_vertex_count=13521),
        ]
        plan = _runtime.plan_from_metadata(
            metadata, parts, skeleton_bytes=payload, target_bares=members,
        )
        for rebind in plan.rebinds:
            self.assertEqual(rebind["family_deform_vertex_count"], 42912 + 13521)
            self.assertEqual(rebind["deform_vertex_count"] in (42912, 13521), True)

    def test_resolve_deform_draw_count(self):
        """t48：**模组自己上传的行数无条件权威**；家族 Σ 只作诊断（t47 实机证伪旧前提）。

        旧前提「draw = 家族各分片 OVC 之和」写在计划书 §7.4 第 1 条，被 t47 的实机数据推翻：
        每家族只有一个 deform pass，而同族每成员 OVC 相同（分片/LOD 变体）⇒ Σ 是 N 倍。
        """
        equal = _runtime.resolve_deform_draw_count(3497, 3497)
        self.assertEqual((equal["count"], equal["source"]), (3497, "exported"))
        self.assertEqual(equal["family_deform_vertex_count"], 3497)
        self.assertEqual(equal["note"], "")
        # 用户实机数据：家族 Σ = 99475，模组自身行数 = 31066 ⇒ 必须采用 31066
        real = _runtime.resolve_deform_draw_count(99475, 31066)
        self.assertEqual((real["count"], real["source"]), (31066, "exported"))
        self.assertIn("99475", real["note"], "家族 Σ 只能出现在诊断 note 里")
        # Σ 只会让值变小（N 倍放大被去掉）⇒ 不得再把 Σ 当权威
        inflated = _runtime.resolve_deform_draw_count(3497, 999)
        self.assertEqual((inflated["count"], inflated["source"]), (999, "exported"))
        # 只有在拿不到模组行数时才退回家族 Σ（兜底）
        fallback = _runtime.resolve_deform_draw_count(3497, 0)
        self.assertEqual(
            (fallback["count"], fallback["source"]), (3497, "family-sum-fallback")
        )
        self.assertIn("兜底", fallback["note"])
        missing = _runtime.resolve_deform_draw_count(0, 0)
        self.assertEqual((missing["count"], missing["source"]), (0, "none"))

    def test_plan_to_dict_exposes_unverified_items(self):
        metadata, payload = self._metadata()
        plan = _runtime.plan_from_metadata(metadata, [], skeleton_bytes=payload)
        snapshot = plan.to_dict()
        self.assertIn("UnverifiedInGame", snapshot)
        joined = "\n".join(snapshot["UnverifiedInGame"])
        for token in ("V1", "V2", "V3"):
            self.assertIn(token, joined)


class HIMIBlendNamespaceTests(unittest.TestCase):
    """核对 mod Blend 的骨骼编号命名空间（重绑 vs-t0 的前置条件）。"""

    def test_blend_indices_span(self):
        span = _runtime.blend_indices_span(_FakeGameType.D3D11ElementList)
        self.assertEqual(span["offset"], 16)
        self.assertEqual(span["width"], 16)
        self.assertEqual(span["stride"], 32)
        self.assertEqual(_runtime.blend_indices_span([])["width"], 0)

    def test_extract_blend_index_values(self):
        span = _runtime.blend_indices_span(_FakeGameType.D3D11ElementList)
        payload = _blend_record((1.0, 0.5, 0.25, 0.25), [3, 7, 11, 13])
        payload += _blend_record((1.0, 0.0, 0.0, 0.0), [9, 0, 0, 0])
        # 缺省只取「权重 > 0」的通道（那才是顶点真正引用的骨骼）
        self.assertEqual(_runtime.extract_blend_index_values(payload, span), {3, 7, 9, 11, 13})
        self.assertEqual(
            _runtime.extract_blend_index_values(payload, span, only_weighted=False),
            {0, 3, 7, 9, 11, 13},
        )
        self.assertEqual(_runtime.extract_blend_index_values(b"", span), set())
        # 整行权重为 0 的行退回全部通道（那种行本身可疑，宁可纳入比较）
        zeros = _blend_record((0.0, 0.0, 0.0, 0.0), [4, 5, 6, 7])
        self.assertEqual(_runtime.extract_blend_index_values(zeros, span), {4, 5, 6, 7})
        # 尾部残缺记录只按完整记录解析（不静默造出假值）
        self.assertEqual(len(_runtime.extract_blend_index_values(b"\x00" * 33, span)), 1)
        # span 与记录步长矛盾 ⇒ 无法核对，返回空集合（调用方降级为 unavailable）
        self.assertEqual(
            _runtime.extract_blend_index_values(payload, {"offset": 20, "width": 16, "stride": 32}),
            set(),
        )

    def test_classify_global_local_and_indeterminate(self):
        vg_map = {0: 5, 1: 6, 2: 7, 3: 8}
        global_verdict = _runtime.classify_blend_namespace(
            exported_values={5, 6, 7, 8}, local_values={0, 1, 2, 3}, vg_map=vg_map
        )
        self.assertEqual(global_verdict["verdict"], "global")
        local_verdict = _runtime.classify_blend_namespace(
            exported_values={0, 1, 2, 3}, local_values={0, 1, 2, 3}, vg_map=vg_map
        )
        self.assertEqual(local_verdict["verdict"], "local")
        # 恒等映射：值相同就是全局编号（不能误判成 local）
        identity = _runtime.classify_blend_namespace(
            exported_values={0, 1, 2, 3},
            local_values={0, 1, 2, 3},
            vg_map={0: 0, 1: 1, 2: 2, 3: 3},
        )
        self.assertEqual(identity["verdict"], "global")
        other = _runtime.classify_blend_namespace(
            exported_values={99}, local_values={0, 1, 2, 3}, vg_map=vg_map
        )
        self.assertEqual(other["verdict"], "indeterminate")
        for missing in (
            _runtime.classify_blend_namespace(exported_values=None, local_values={1}, vg_map=vg_map),
            _runtime.classify_blend_namespace(exported_values={5}, local_values=set(), vg_map=vg_map),
            _runtime.classify_blend_namespace(exported_values={5}, local_values={0}, vg_map={}),
        ):
            self.assertEqual(missing["verdict"], "unavailable")

    def test_check_degrades_to_unavailable_without_inputs(self):
        verdict = _runtime.check_exported_blend_namespace(
            workspace_root="", unique_strs=["LOD0.x-1-0"], exported_blend_bytes=b"", blend_span={}
        )
        self.assertEqual(verdict["verdict"], "unavailable")

    def test_check_merges_multiple_components_of_one_family(self):
        """一个家族的多个部件各有自己的 VGMap/局部编号空间：各自映射后取并集。"""
        import tempfile

        root = tempfile.mkdtemp(prefix="himi_ns_")
        self.addCleanup(shutil.rmtree, root, ignore_errors=True)
        span = _runtime.blend_indices_span(_FakeGameType.D3D11ElementList)
        # 部件 1：局部 0..1 → 全局 5..6；部件 2：局部 0..1 → 全局 7..8（局部编号空间相同、
        # 映射不同 —— 正是不能合并映射表的原因）
        _make_workspace(
            root,
            "RoleA",
            [
                {"bare": "aaaa1111-1-0", "draw_ib": "aaaa1111", "vg_count": 2,
                 "original_vertex_count": 2, "family_members": ["aaaa1111-1-0", "aaaa1111-2-0"],
                 "vg_map": {0: 5, 1: 6}, "rows": 1, "local_indices": [[0, 1, 0, 0]]},
                {"bare": "aaaa1111-2-0", "draw_ib": "aaaa1111", "vg_count": 2,
                 "original_vertex_count": 2, "family_members": ["aaaa1111-1-0", "aaaa1111-2-0"],
                 "vg_map": {0: 7, 1: 8}, "rows": 1, "local_indices": [[0, 1, 0, 0]]},
            ],
            slot_count=16,
        )
        exported = _blend_buffer_bytes([[5, 6, 0, 0], [7, 8, 0, 0]])  # 已分别映射后的全局值
        verdict = _runtime.check_exported_blend_namespace(
            workspace_root=root,
            unique_strs=["LOD0.aaaa1111-1-0", "LOD0.aaaa1111-2-0"],
            exported_blend_bytes=exported,
            blend_span=span,
        )
        self.assertEqual(verdict["verdict"], "global", verdict)
        # 若把两个部件的局部编号空间混成一张表，就会误判；反向验证：
        local_only = _runtime.check_exported_blend_namespace(
            workspace_root=root,
            unique_strs=["LOD0.aaaa1111-1-0", "LOD0.aaaa1111-2-0"],
            exported_blend_bytes=_blend_buffer_bytes([[0, 1, 0, 0], [0, 1, 0, 0]]),
            blend_span=span,
        )
        self.assertEqual(local_only["verdict"], "local", local_only)


class HIMIPaletteCacheOrderTests(HIMIMergedRuntimeBase):
    """ITEM 3：``ModImpRuntime/<bare>-BoneMatrix.buf`` 是**按引用序压缩**的槽表。

    构造「引用序 != 全局槽序」的用例：该组件的原部件 Blend 引用**全部** 10 个局部槽
    （升序 0..9 就是引用序），而 ``VGMap`` 是 [0,10) 上的**置换**且
    ``2→7 / 5→1 / 9→4``（局部序与全局槽序完全不同）：

    * 正确写法 = 按引用序（= 局部升序）取行、再用 ``VGMap`` 对回全局槽
      ⇒ 第 ``j`` 行 = ``skeleton[VGMap[j]]``；
    * 「池镜像」写法 = 第 ``j`` 行 = ``skeleton[j]`` ⇒ 必须被检出。

    **为什么必须让 Blend 引用全部局部槽**：t51 起家族区间 = 该族**被引用**局部槽映出的
    全局槽集合（数据层只为被引用的局部槽注册 canonical 槽位；``common/himi_skeleton.py:883``
    的注册循环走的就是 ``referenced_slots``），而未被引用的局部槽拿到的是数据层补的
    "gap slots"。只引用 3 个局部槽会让该族区间 ``{1,4,7}`` 出现洞并被 fail-closed 正确拒绝
    —— 那是**夹具**与数据层口径不一致，不是实现问题。
    """

    #: 三个「局部序 ≠ 全局序」的记号映射（文档用；``VG_MAP`` 里保持一致）。
    MARKER_MAPPINGS = {2: 7, 5: 1, 9: 4}
    VG_MAP = {
        str(0): 3,
        str(1): 2,
        str(2): 7,
        str(3): 0,
        str(4): 5,
        str(5): 1,
        str(6): 6,
        str(7): 8,
        str(8): 9,
        str(9): 4,
    }
    SLOTS = 10

    def _reference_order_rows(self, skeleton_rows):
        """正确写法的缓存行：第 ``j`` 行 = 引用序第 ``j`` 个局部槽对应的全局槽。"""
        return [skeleton_rows[int(self.VG_MAP[str(local)])] for local in range(self.SLOTS)]

    def _pool_image_rows(self, skeleton_rows):
        """池镜像（错误）写法：第 ``j`` 行 = 全局槽 ``j``。"""
        return [skeleton_rows[local] for local in range(self.SLOTS)]

    def _workspace(self, cache_rows=None, *, drops_cache=False):
        """建工作空间 + 原部件 Blend（引用局部 0..9）+ 角色骨架 + 部件 palette 缓存。"""
        info = _make_workspace(
            self.workspace,
            "RoleA",
            [
                {
                    "bare": "aaaa1111-1-0",
                    "draw_ib": "aaaa1111",
                    "vg_count": self.SLOTS,
                    "original_vertex_count": self.SLOTS,
                    "family_members": ["aaaa1111-1-0"],
                    "vg_map": self.VG_MAP,
                    "rows": self.SLOTS,
                    # 每行只引用一个局部槽 ⇒ referenced_slots == [0..9]（全覆盖、无 gap 干扰）
                    "local_indices": [[local, local, local, local] for local in range(self.SLOTS)],
                }
            ],
            slot_count=self.SLOTS,
        )
        skeleton_rows = [
            info["skeleton_bytes"][i * 48 : (i + 1) * 48] for i in range(self.SLOTS)
        ]
        component_dir = os.path.join(
            self.workspace, "LOD0", "aaaa1111-1-0", "ModImpRuntime"
        )
        os.makedirs(component_dir, exist_ok=True)
        if not drops_cache:
            if cache_rows is None:
                cache_rows = self._reference_order_rows(skeleton_rows)
            with open(
                os.path.join(component_dir, "aaaa1111-1-0-BoneMatrix.buf"), "wb"
            ) as handle:
                handle.write(b"".join(cache_rows))
        return info, skeleton_rows

    def _verify(self, info):
        return _runtime.verify_palette_cache_against_skeleton(
            workspace_root=self.workspace,
            unique_str="LOD0.aaaa1111-1-0",
            json_payload={
                "VGMap": self.VG_MAP,
                "BoneMatrixFileName": "aaaa1111-1-0-BoneMatrix.buf",
            },
            skeleton_bytes=info["skeleton_bytes"],
            slot_count=self.SLOTS,
        )

    def test_reference_order_cache_verifies(self):
        info, rows = self._workspace()
        check = self._verify(info)
        self.assertTrue(check["ok"], check)
        self.assertEqual(check["checked"], self.SLOTS)
        self.assertEqual(check["rows"], self.SLOTS)
        self.assertNotEqual(
            self._reference_order_rows(rows),
            self._pool_image_rows(rows),
            "夹具前提：引用序与池镜像必须真的不同（否则下面两条反例恒真）",
        )

    def test_reading_the_cache_as_a_pool_image_fails(self):
        """把缓存当成「下标 i 在偏移 i*48」的池镜像（忽略 VGMap）⇒ 必须对不上。"""
        info, rows = self._workspace(cache_rows=None)
        self._workspace(cache_rows=self._pool_image_rows(rows))  # 池镜像读法写出的缓存
        check = self._verify(info)
        self.assertFalse(check["ok"], "池镜像读法必须被检出")
        self.assertIn("逐字节不一致", check["reason"])

    def test_sorted_by_global_slot_order_fails(self):
        """按「全局槽升序」重排（而不是引用序）⇒ 必须对不上（顺序契约钉死）。"""
        info, rows = self._workspace(cache_rows=None)
        self._workspace(cache_rows=self._pool_image_rows(rows))  # 按全局槽 0<1<… 排
        check = self._verify(info)
        self.assertFalse(check["ok"], "按全局槽排序必须被检出")
        self.assertEqual(check["mismatches"][0]["row"], 0)

    def test_row_count_mismatch_is_reported(self):
        info, rows = self._workspace(cache_rows=None)
        self._workspace(cache_rows=self._reference_order_rows(rows)[:-1])  # 少一行
        check = self._verify(info)
        self.assertFalse(check["ok"])
        self.assertIn(
            f"缓存行数 {self.SLOTS - 1} != 被引用槽数 {self.SLOTS}", check["reason"]
        )

    def test_missing_cache_and_missing_blend_are_reported(self):
        info, _rows = self._workspace(cache_rows=None, drops_cache=True)
        check = self._verify(info)
        self.assertFalse(check["ok"])
        self.assertIn("缓存缺失", check["reason"])
        os.remove(os.path.join(self.workspace, "LOD0", "aaaa1111-1-0", GAMETYPE,
                               "aaaa1111-1-0-Blend.buf"))
        check = self._verify(info)
        self.assertFalse(check["ok"])
        self.assertIn("没有原部件 Blend", check["reason"])

    def test_read_palette_cache_rows(self):
        info, rows = self._workspace()
        payload = _runtime.read_palette_cache(
            os.path.join(self.workspace, "LOD0", "aaaa1111-1-0", "ModImpRuntime",
                         "aaaa1111-1-0-BoneMatrix.buf")
        )
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["row_count"], self.SLOTS)
        self.assertEqual(payload["rows"], self._reference_order_rows(rows))
        self.assertNotEqual(
            payload["rows"], self._pool_image_rows(rows), "引用序 != 池镜像（夹具前提）"
        )
        self.assertEqual(
            payload["rows"][2], rows[self.MARKER_MAPPINGS[2]], "局部 2 → 全局 7"
        )
        with open(os.path.join(self._tmp, "bad.buf"), "wb") as handle:
            handle.write(b"\x00" * 47)
        self.assertFalse(_runtime.read_palette_cache(os.path.join(self._tmp, "bad.buf"))["ok"])

    def test_loader_excludes_component_on_cache_mismatch(self):
        """核对不通过 ⇒ 该部件不进目标集合（=> 不会重绑）+ 大声诊断。"""
        info, rows = self._workspace(cache_rows=None)
        # 故意写错缓存（池镜像读法）
        with open(
            os.path.join(self.workspace, "LOD0", "aaaa1111-1-0", "ModImpRuntime",
                         "aaaa1111-1-0-BoneMatrix.buf"), "wb"
        ) as handle:
            handle.write(b"".join(self._pool_image_rows(rows)))
        plan = _runtime.load_workspace_plan(
            self.workspace,
            export_unique_strs=["LOD0.aaaa1111-1-0"],
            export_bares=["aaaa1111-1-0"],
            enable_gate_oracle=False,
        )
        self.assertEqual(plan.rebinds, [], "缓存与骨架不一致时绝不能重绑")
        self.assertEqual(plan.palette_cache["aaaa1111-1-0"]["verdict"], "mismatch")
        self.assertIn("部件 palette 缓存与角色级骨架不一致", plan.diagnostics_text())

    def test_loader_tolerates_a_missing_cache_but_says_so(self):
        """旧工作空间没有部件缓存 ⇒ 无法核对（不据此排除），但必须留诊断。"""
        self._workspace(cache_rows=None, drops_cache=True)
        plan = _runtime.load_workspace_plan(
            self.workspace,
            export_unique_strs=["LOD0.aaaa1111-1-0"],
            export_bares=["aaaa1111-1-0"],
            enable_gate_oracle=False,
        )
        self.assertEqual(plan.palette_cache["aaaa1111-1-0"]["verdict"], "unavailable")
        self.assertEqual(len(plan.rebinds), 1, "无法核对不等于数据错误")
        self.assertIn("未能核对部件 palette 缓存顺序", plan.diagnostics_text())

    def test_loader_accepts_verified_component(self):
        self._workspace()
        plan = _runtime.load_workspace_plan(
            self.workspace,
            export_unique_strs=["LOD0.aaaa1111-1-0"],
            export_bares=["aaaa1111-1-0"],
            enable_gate_oracle=False,
        )
        self.assertEqual(plan.palette_cache["aaaa1111-1-0"]["verdict"], "verified")
        self.assertEqual(len(plan.rebinds), 1)
        self.assertEqual(plan.rebinds[0]["vg_count"], 10)


class HIMIGateOracleTests(HIMIMergedRuntimeBase):
    """G1 权威判定 = 数据层 ``prepare(target_bares=...)``（本模块只消费，不重写）。"""

    def _prepare_result(self, info, *, safe_bares, rejected=None, digest=None, slot_count=None):
        return {
            "ok": True,
            "message": "已扫描 2 个组件；join 2 个；参与合并 %d 个" % len(safe_bares),
            "slot_count": int(slot_count if slot_count is not None else SLOT_COUNT),
            # pass 级正向物证（R-T14-1 之后必须齐备，否则 oracle 会被 fail-closed 拒绝）
            "dump_dir": r"K:\SSMT-Package-master\3Dmigoto\HI3\FrameAnalysis-2026-09-23-135739",
            "resolver": object(),
            "metadata": {
                "SkeletonDigest": digest if digest is not None else info["digest"],
                "RoleKey": info["role_key"],
                "Slots": [],
            },
            "writes": [{"bare": bare} for bare in safe_bares],
            "gate_rejected": list(rejected or []),
            "cache_intact": True,
        }

    def _workspace(self):
        # vg_count = SLOT_COUNT ⇒ 缺省 VGMap 是恒等映射、值集 = [0,8)（密集、覆盖骨架）：
        # t51 起「族内密集 + 全量导出时 ∑span == 槽数」是 attach 段 x1/y1 的 fail-closed 前提。
        return _make_workspace(
            self.workspace,
            "RoleA",
            [
                {"bare": "aaaa1111-1-0", "draw_ib": "aaaa1111", "vg_count": SLOT_COUNT,
                 "original_vertex_count": 3377, "family_members": ["aaaa1111-1-0", "aaaa1111-2-0"]},
                {"bare": "aaaa1111-2-0", "draw_ib": "aaaa1111", "vg_count": SLOT_COUNT,
                 "original_vertex_count": 120, "family_members": ["aaaa1111-1-0", "aaaa1111-2-0"]},
            ],
        )

    def _load(self, *, target_bares=None, **kwargs):
        return _runtime.load_workspace_plan(
            self.workspace,
            export_unique_strs=["LOD0.aaaa1111-1-0", "LOD0.aaaa1111-2-0"],
            export_bares=(
                ["aaaa1111-1-0", "aaaa1111-2-0"] if target_bares is None else target_bares
            ),
            **kwargs,
        )

    def test_loader_passes_the_target_set_to_prepare_and_consumes_writes(self):
        from unittest import mock

        info = self._workspace()
        calls = {}

        def _fake_prepare(root, unique_str_list=None, force=False, target_bares=None):
            calls["target_bares"] = list(target_bares or [])
            calls["unique_str_list"] = unique_str_list
            return self._prepare_result(info, safe_bares=["aaaa1111-1-0"])

        with mock.patch.object(_skeleton.HIMISkeletonMergeHelper, "prepare", staticmethod(_fake_prepare)):
            plan = self._load()

        self.assertEqual(
            calls["target_bares"], ["aaaa1111-1-0", "aaaa1111-2-0"],
            "必须把本次导出的目标集合交给数据层（它是 pass 级门控的入参）",
        )
        self.assertIsNone(
            calls["unique_str_list"],
            "已落盘集合必须由数据层自己发现（否则家族成员会被漏掉）",
        )
        self.assertEqual(plan.gate_source, "prepare")
        self.assertEqual([item["bare"] for item in plan.rebinds], ["aaaa1111-1-0"])

    def test_loader_consumes_a_pass_level_rejection_loudly(self):
        """prepare 判 pass 不可合并 ⇒ 整个 pass/家族不重绑 + 大声诊断（T9-F4 侧）。"""
        from unittest import mock

        info = self._workspace()

        def _fake_prepare(root, unique_str_list=None, force=False, target_bares=None):
            return self._prepare_result(
                info,
                safe_bares=[],
                rejected=[
                    {
                        "bare": "aaaa1111-2-0",
                        "draw_ib": "aaaa1111",
                        "reason": "deform_pass_incomplete",
                        "missing": ["aaaa1111-2-0"],
                    }
                ],
            )

        with mock.patch.object(_skeleton.HIMISkeletonMergeHelper, "prepare", staticmethod(_fake_prepare)):
            plan = self._load()

        self.assertTrue(plan.ok)
        self.assertEqual(plan.rebinds, [], "权威判定不放行 ⇒ 一个部件都不得重绑")
        self.assertEqual(plan.rejected["aaaa1111"]["missing"], ["aaaa1111-2-0"])
        self.assertEqual(plan.rejected["aaaa1111"]["reason"], "deform_pass_incomplete")
        text = plan.diagnostics_text()
        self.assertIn(_contract.DEFORM_GATE_REJECT_PREFIX, text)
        self.assertIn("aaaa1111-2-0", text)
        self.assertIn("prepare", text)

    def test_exporter_refuses_the_rebind_when_the_oracle_rejects(self):
        """消费者侧：权威门控拒绝时 VB 段不得出现 vs-t0（整家族退回普通路径）。"""
        from unittest import mock

        info = self._workspace()
        model = _drawib_model(
            "aaaa1111",
            [
                _submesh("aaaa1111-1-0", vg_count=4, original_vertex_count=3377),
                _submesh("aaaa1111-2-0", vg_count=4, original_vertex_count=120),
            ],
            draw_number=999,
            blend_rows=HIMIExporterWiringTests.GLOBAL_ROWS,
        )

        def _fake_prepare(root, unique_str_list=None, force=False, target_bares=None):
            return self._prepare_result(
                info, safe_bares=[], rejected=[{"bare": "aaaa1111-1-0", "draw_ib": "aaaa1111"}]
            )

        with mock.patch.object(_skeleton.HIMISkeletonMergeHelper, "prepare", staticmethod(_fake_prepare)):
            exporter = self.make_exporter([model])
            builder = M_IniBuilder()
            exporter.add_unity_vs_texture_override_vb_sections(builder, model)
            text = "\n".join(_builder_lines(builder))
            exporter.add_unity_vs_resource_vb_sections(builder, model)
            resource_text = "\n".join(_builder_lines(builder))

        self.assertNotIn("vs-t0", text)
        self.assertIn("draw = 999, 0", text)
        self.assertNotIn("ResourceHI3MergedSkeleton_", resource_text)

    def test_loader_falls_back_when_prepare_is_unavailable(self):
        from unittest import mock

        self._workspace()

        def _boom(*_args, **_kwargs):
            raise RuntimeError("dump 被删了")

        with mock.patch.object(_skeleton.HIMISkeletonMergeHelper, "prepare", staticmethod(_boom)):
            plan = self._load()
        self.assertEqual(plan.gate_source, "cache-only")
        self.assertEqual(len(plan.rebinds), 2, "缓存完整时沿用导入期判定")
        self.assertIn("prepare 异常", plan.diagnostics_text())

    def test_loader_ignores_a_stale_oracle(self):
        from unittest import mock

        info = self._workspace()

        def _fake_prepare(root, unique_str_list=None, force=False, target_bares=None):
            return self._prepare_result(info, safe_bares=[], digest="0" * 64)

        with mock.patch.object(_skeleton.HIMISkeletonMergeHelper, "prepare", staticmethod(_fake_prepare)):
            plan = self._load()
        self.assertEqual(plan.gate_source, "cache-only")
        self.assertEqual(len(plan.rebinds), 2, "陈旧 oracle 不得被采用")
        self.assertIn("不是同一次导入", plan.diagnostics_text())

    def test_prepare_gate_oracle_normalizes_the_data_layer_result(self):
        from unittest import mock

        info = self._workspace()

        def _fake_prepare(root, unique_str_list=None, force=False, target_bares=None):
            return self._prepare_result(
                info,
                safe_bares=["aaaa1111-1-0"],
                rejected=[{"bare": "aaaa1111-2-0", "draw_ib": "aaaa1111", "missing": ["x"]}],
            )

        with mock.patch.object(_skeleton.HIMISkeletonMergeHelper, "prepare", staticmethod(_fake_prepare)):
            oracle = _runtime.prepare_gate_oracle(
                self.workspace, target_bares=["aaaa1111-1-0"],
                expected_digest=info["digest"], expected_slot_count=SLOT_COUNT,
            )
        self.assertTrue(oracle["available"])
        self.assertFalse(oracle["stale"])
        self.assertEqual(oracle["safe_bares"], ["aaaa1111-1-0"])
        self.assertEqual(oracle["rejected"]["aaaa1111"]["missing"], ["x"])

    def test_prepare_gate_oracle_degrades_when_prepare_fails(self):
        from unittest import mock

        self._workspace()

        def _fake_prepare(root, unique_str_list=None, force=False, target_bares=None):
            return {"ok": False, "message": "没有组件能 join 到同帧 deform pass", "writes": []}

        with mock.patch.object(_skeleton.HIMISkeletonMergeHelper, "prepare", staticmethod(_fake_prepare)):
            oracle = _runtime.prepare_gate_oracle(self.workspace, target_bares=[])
        self.assertFalse(oracle["available"])
        self.assertIn("join", oracle["message"])
        self.assertFalse(_runtime.prepare_gate_oracle("", target_bares=[])["available"])


class HIMIWorkspacePlanTests(HIMIMergedRuntimeBase):
    def test_load_workspace_plan_full_path(self):
        info = _make_workspace(
            self.workspace,
            "RoleA",
            [
                # vg_count = SLOT_COUNT ⇒ 恒等 VGMap、该族值集 = [0,8)（密集且覆盖骨架）。
                {"bare": "aaaa1111-1-0", "draw_ib": "aaaa1111", "vg_count": SLOT_COUNT,
                 "original_vertex_count": 120, "family_members": ["aaaa1111-1-0", "aaaa1111-2-0"]},
                {"bare": "aaaa1111-2-0", "draw_ib": "aaaa1111", "vg_count": SLOT_COUNT,
                 "original_vertex_count": 240, "family_members": ["aaaa1111-1-0", "aaaa1111-2-0"]},
            ],
        )
        plan = _runtime.load_workspace_plan(
            self.workspace,
            export_unique_strs=["LOD0.aaaa1111-1-0", "LOD0.aaaa1111-2-0"],
            export_bares=["aaaa1111-1-0", "aaaa1111-2-0"],
        )
        self.assertTrue(plan.ok, plan.message)
        self.assertEqual(plan.role_key, info["role_key"])
        self.assertEqual(plan.slot_count, SLOT_COUNT)
        self.assertEqual(len(plan.rebinds), 2)
        self.assertEqual(
            plan.family_ranges,
            {"aaaa1111": {"base": 0, "slots": SLOT_COUNT, "own_block": True}},
            "全量导出的族区间必须落进计划（attach 段的 x1/y1 来源）",
        )
        self.assertEqual(plan.uncovered_slot_ranges(), [], "全量导出不得有空缺槽位")
        self.assertEqual(plan.skeleton_source_path, os.path.join(self.workspace, "Config", info["skeleton_name"]))
        # 资源/HLSL 都由这个计划渲染
        self.assertIn(
            f"ResourceHI3MergedSkeleton_{info['role_key']}",
            "\n".join(plan.resource_ini_lines()),
        )

    def test_no_metadata_is_a_plain_export(self):
        plan = _runtime.load_workspace_plan(self.workspace, export_unique_strs=[])
        self.assertFalse(plan.ok)
        self.assertEqual(plan.component_count, 0)
        self.assertEqual(plan.parts_with_data, 0)
        decision = _contract.evaluate_merged_skeleton_contract(
            **plan.contract_snapshot(checkbox_enabled=True)
        )
        self.assertEqual(decision["level"], "notice")

    def test_missing_workspace(self):
        plan = _runtime.load_workspace_plan(os.path.join(self._tmp, "nope"))
        self.assertFalse(plan.ok)
        self.assertIn("工作空间不存在", plan.message)

    def test_role_key_mismatch_is_reported(self):
        _make_workspace(
            self.workspace,
            "RoleA",
            [{"bare": "aaaa1111-1-0", "draw_ib": "aaaa1111", "vg_count": SLOT_COUNT,
              "original_vertex_count": 120, "family_members": ["aaaa1111-1-0"]}],
        )
        other = os.path.join(self._tmp, "RoleB")
        shutil.copytree(self.workspace, other)
        plan = _runtime.load_workspace_plan(
            other, export_unique_strs=["LOD0.aaaa1111-1-0"], export_bares=["aaaa1111-1-0"],
        )
        self.assertTrue(plan.ok)
        self.assertIn("RoleKey", plan.diagnostics_text())

    def test_skeleton_file_missing_rejects_the_plan(self):
        _make_workspace(
            self.workspace,
            "RoleA",
            [{"bare": "aaaa1111-1-0", "draw_ib": "aaaa1111", "vg_count": 4,
              "original_vertex_count": 120, "family_members": ["aaaa1111-1-0"]}],
            write_skeleton=False,
        )
        plan = _runtime.load_workspace_plan(
            self.workspace, export_unique_strs=["LOD0.aaaa1111-1-0"], export_bares=["aaaa1111-1-0"],
        )
        self.assertFalse(plan.ok)
        self.assertIn("骨架", plan.diagnostics_text())

    def test_corrupt_skeleton_rejects_the_plan(self):
        info = _make_workspace(
            self.workspace,
            "RoleA",
            [{"bare": "aaaa1111-1-0", "draw_ib": "aaaa1111", "vg_count": 4,
              "original_vertex_count": 120, "family_members": ["aaaa1111-1-0"]}],
        )
        # 元数据里的摘要不变，但磁盘上的骨架二进制被改写 ⇒ 必须拒绝（不静默用坏数据）。
        with open(os.path.join(self.workspace, "Config", info["skeleton_name"]), "wb") as handle:
            handle.write(b"\x00" * (SLOT_COUNT * 48))
        plan = _runtime.load_workspace_plan(
            self.workspace, export_unique_strs=["LOD0.aaaa1111-1-0"], export_bares=["aaaa1111-1-0"],
        )
        self.assertFalse(plan.ok)
        self.assertIn("摘要", plan.message)


class HIMIPublishTests(HIMIMergedRuntimeBase):
    def _plan(self):
        info = _make_workspace(
            self.workspace,
            "RoleA",
            [{"bare": "aaaa1111-1-0", "draw_ib": "aaaa1111", "vg_count": SLOT_COUNT,
              "original_vertex_count": 120, "family_members": ["aaaa1111-1-0"]}],
        )
        plan = _runtime.load_workspace_plan(
            self.workspace, export_unique_strs=["LOD0.aaaa1111-1-0"], export_bares=["aaaa1111-1-0"],
        )
        return plan, info

    def test_publish_writes_the_binary_into_the_mod_folder(self):
        plan, info = self._plan()
        target = os.path.join(self.mod_folder, "Meshes")
        result = _runtime.publish_skeleton_binary(plan, target)
        self.assertTrue(result["ok"], result)
        self.assertTrue(result["written"])
        self.assertEqual(result["path"], os.path.join(target, info["skeleton_name"]))
        with open(result["path"], "rb") as handle:
            self.assertEqual(handle.read(), info["skeleton_bytes"])

    def test_publish_is_idempotent(self):
        plan, _info = self._plan()
        target = os.path.join(self.mod_folder, "Meshes")
        first = _runtime.publish_skeleton_binary(plan, target)
        second = _runtime.publish_skeleton_binary(plan, target)
        self.assertTrue(first["written"])
        self.assertFalse(second["written"], "内容一致时不得重写（多轮导出会重复调用）")

    def test_publish_rewrites_when_content_differs(self):
        plan, info = self._plan()
        target = os.path.join(self.mod_folder, "Meshes")
        os.makedirs(target, exist_ok=True)
        with open(os.path.join(target, info["skeleton_name"]), "wb") as handle:
            handle.write(b"stale")
        result = _runtime.publish_skeleton_binary(plan, target)
        self.assertTrue(result["written"])
        with open(result["path"], "rb") as handle:
            self.assertEqual(handle.read(), info["skeleton_bytes"])

    def test_publish_rejects_bad_plan_and_bad_size(self):
        plan, info = self._plan()
        target = os.path.join(self.mod_folder, "Meshes")
        broken = _runtime.HIMIExportRuntimePlan(ok=True, slot_count=SLOT_COUNT, skeleton_basename=info["skeleton_name"], skeleton_source_path=plan.skeleton_source_path, skeleton_digest=plan.skeleton_digest)
        broken.slot_count = SLOT_COUNT + 1
        result = _runtime.publish_skeleton_binary(broken, target)
        self.assertFalse(result["ok"])
        self.assertIn("字节数不符", result["message"])
        not_ok = _runtime.HIMIExportRuntimePlan(ok=False, message="无计划")
        self.assertFalse(_runtime.publish_skeleton_binary(not_ok, target)["ok"])
        self.assertFalse(_runtime.publish_skeleton_binary(plan, "")["ok"])


# --------------------------------------------------------------------------- #
# D. 导出接线
# --------------------------------------------------------------------------- #


class HIMIExporterWiringTests(HIMIMergedRuntimeBase):
    # 局部槽位 0..3 → 全局槽位 4..7（非恒等映射，才能区分「局部编号」与「全局编号」）
    VG_MAP = {str(i): i + 4 for i in range(4)}
    LOCAL_ROWS = [[0, 1, 2, 3]]
    # 导出侧「全局编号」的样本：必须覆盖**整族的映射值域**（= 映射后并集）才判 global。
    GLOBAL_ROWS = [[0, 1, 2, 3], [4, 5, 6, 7]]

    def _workspace_with_two_parts(self, *, vg_map=None, local_rows=None, slot_count=SLOT_COUNT):
        """两个部件同族；它们的 VGMap 值域**互补** ⇒ 族区间 = [0, slot_count)。

        t51 起「族内密集 + 全量导出时 ∑span == 槽数」是 attach 段 x1/y1 的 fail-closed
        前提，所以这个夹具不能只覆盖骨架的一半槽位（`VG_MAP` 的值域必须被第二个部件补满）。
        """
        vg_map = self.VG_MAP if vg_map is None else vg_map
        local_rows = self.LOCAL_ROWS if local_rows is None else local_rows
        used = {int(value) for value in dict(vg_map).values()}
        free = [slot for slot in range(int(slot_count)) if slot not in used]
        second = (
            {str(index): slot for index, slot in enumerate(free)}
            if len(free) >= len(vg_map)
            else dict(vg_map)
        )
        return _make_workspace(
            self.workspace,
            "RoleA",
            [
                {"bare": "aaaa1111-1-0", "draw_ib": "aaaa1111", "vg_count": 4,
                 "original_vertex_count": 3377, "family_members": ["aaaa1111-1-0", "aaaa1111-2-0"],
                 "vg_map": vg_map, "rows": len(local_rows), "local_indices": local_rows},
                {"bare": "aaaa1111-2-0", "draw_ib": "aaaa1111", "vg_count": 4,
                 "original_vertex_count": 120, "family_members": ["aaaa1111-1-0", "aaaa1111-2-0"],
                 "vg_map": second, "rows": len(local_rows), "local_indices": local_rows},
            ],
            slot_count=slot_count,
        )

    def _models(self, *, vg_counts=(4, 4), blend_rows="global"):
        rows = self.GLOBAL_ROWS if blend_rows == "global" else (
            self.LOCAL_ROWS if blend_rows == "local" else None
        )
        return [
            _drawib_model(
                "aaaa1111",
                [
                    _submesh("aaaa1111-1-0", vg_count=vg_counts[0], original_vertex_count=3377),
                    _submesh("aaaa1111-2-0", vg_count=vg_counts[1], original_vertex_count=120),
                ],
                draw_number=999,
                blend_rows=rows,
            )
        ]

    def test_vb_section_without_plan_matches_base(self):
        """无合并数据时，HIMI 的 VB 段必须与基类**逐行相同**（回归护栏）。"""
        model = self._models()[0]
        base = ExportUnity(types.SimpleNamespace(keyname_mkey_dict={}))
        base.drawib_model_list = [model]
        base_builder = M_IniBuilder()
        base.add_unity_vs_texture_override_vb_sections(base_builder, model)

        exporter = self.make_exporter([model])  # 工作空间里没有 Config/HIMIMergedSkeleton.json
        himi_builder = M_IniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(himi_builder, model)

        self.assertEqual(_builder_lines(himi_builder), _builder_lines(base_builder))

    def test_vb_section_injects_vs_t0_and_deform_draw_count(self):
        """`vs-t0` 注入 + `draw` = **模组自己上传的行数**（t48 修正；家族 Σ 只作诊断）。"""
        self._workspace_with_two_parts()
        model = self._models()[0]
        exporter = self.make_exporter([model])
        builder = M_IniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, model)
        lines = _builder_lines(builder)

        blend_index = lines.index("[TextureOverride_VB_aaaa1111_0_Blend]")
        blend_block = []
        for line in lines[blend_index + 1 :]:
            if line.startswith("[") and line.endswith("]"):
                break
            blend_block.append(line)
        self.assertIn("hash = 8322ac5a", blend_block)
        self.assertIn("vb1 = Resourceaaaa1111Blend", blend_block)
        self.assertIn("vs-t0 = ResourceHI3MergedSkeleton_RoleA_", "\n".join(blend_block))
        self.assertIn("handling = skip", blend_block)
        # t48：权威值 = 模组自己上传的行数（draw_number = vertex_count = override_vertex_count）
        self.assertEqual(int(model.draw_number), 999)
        self.assertIn("draw = 999, 0", blend_block, "必须等于模组自己上传的行数（t48）")
        # 家族 Σ（3377 + 120 = 3497）**不得**再进 ini —— 那正是 t47 的实机 blocker
        self.assertNotIn("draw = 3497, 0", blend_block, "家族 Σ 不得进 ini（t47 实机证伪）")

        # 其它类别段不得出现 vs-t0
        position_index = lines.index("[TextureOverride_VB_aaaa1111_0_Position]")
        position_block = []
        for line in lines[position_index + 1 :]:
            if line.startswith("[") and line.endswith("]"):
                break
            position_block.append(line)
        self.assertNotIn("vs-t0", position_block)

    def test_deform_draw_never_exceeds_uploaded_rows(self):
        """t48 物理不变量：**凡写进 ini 的 `draw` 必须 ≤ 该部件上线 buffer 的行数**。

        这正是离线链此前**完全缺失**的那一环（t47：`draw` = 99475 vs 模组 31066 行、
        差 68409 行、**零红**）。两态：把权威来源换回「家族 Σ 优先」（旧实现）⇒ 同一断言
        必须失败（零写入：进程内 monkeypatch）。
        """
        self._workspace_with_two_parts()
        model = self._models()[0]
        uploaded_rows = int(model.draw_number)
        exporter = self.make_exporter([model])

        def _blend_block(exp) -> list:
            builder = M_IniBuilder()
            exp.add_unity_vs_texture_override_vb_sections(builder, model)
            lines = _builder_lines(builder)
            index = lines.index("[TextureOverride_VB_aaaa1111_0_Blend]")
            block = []
            for line in lines[index + 1 :]:
                if line.startswith("[") and line.endswith("]"):
                    break
                block.append(line)
            return block

        def _written_draw(block: list) -> int:
            draws = [line for line in block if line.startswith("draw = ")]
            self.assertEqual(len(draws), 1, f"Blend 段应恰有一笔 draw：{draws}")
            return int(draws[0].removeprefix("draw = ").split(",")[0])

        block = _blend_block(exporter)
        written = _written_draw(block)
        self.assertLessEqual(
            written,
            uploaded_rows,
            f"写进 ini 的 draw（{written}）不得超过模组上传的行数（{uploaded_rows}）",
        )
        self.assertIn("vs-t0 = ", "\n".join(block), "重绑仍在（draw 修正不影响 vs-t0）")
        # 「draw == 同产物 override_vertex_count」的机器断言：两者都由 `draw_number` 渲染
        # （`unity.py:137` 的 `override_vertex_count = str(drawib_model.draw_number)`）。
        self.assertEqual(
            written, uploaded_rows, "draw 必须 == draw_number（= override_vertex_count 的来源）"
        )
        unity_source = (
            REPO_ROOT / "ui" / "universal" / "unity.py"
        ).read_text(encoding="utf-8")
        self.assertIn(
            '"override_vertex_count = " + str(drawib_model.draw_number)',
            unity_source,
            "同产物的 override_vertex_count 必须仍由 draw_number 渲染（口径未变）",
        )

        # 两态：旧实现（家族 Σ 优先）⇒ 写出的 draw 必然越界 ⇒ 本护栏会红
        original = _runtime.resolve_deform_draw_count

        def _old_premise(spec, exported):
            return {
                "count": max(int(spec or 0), int(exported or 0)),
                "source": "mutant-family-sum",
                "note": "",
            }

        _runtime.resolve_deform_draw_count = _old_premise
        try:
            mutant_written = _written_draw(_blend_block(exporter))
        finally:
            _runtime.resolve_deform_draw_count = original
        self.assertGreater(
            mutant_written,
            uploaded_rows,
            "变异体（家族 Σ 优先）确实越界 ⇒ 本护栏能抓到 t47 的缺陷（非恒真）",
        )
        # 还原后同一份代码仍合规（证明上面那次变异是**唯一**差别）
        self.assertEqual(_written_draw(_blend_block(exporter)), written)

    def test_blend_namespace_verdicts_are_recorded(self):
        self._workspace_with_two_parts()
        model = self._models()[0]
        exporter = self.make_exporter([model])
        builder = M_IniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, model)
        verdicts = exporter.himi_blend_namespace_verdicts()
        self.assertEqual(verdicts["aaaa1111"]["verdict"], "global")

    def test_blend_namespace_local_blocks_the_rebind(self):
        """mod 的 Blend 仍是局部编号 ⇒ 绝不能重绑 vs-t0（否则骨骼错位）。

        【t95】不重绑 **不等于** 不写槽：该族仍有专属块 ⇒ 本族 deform pass 必须照旧发
        「捕获 palette → 捕获 cb3 → run attach」，否则骨架里那块槽恒零 ⇒ 索引到它的
        顶点拿零矩阵（13:54 产物里 09c6e58a 的 165 个槽就是这样全空的）。
        """
        self._workspace_with_two_parts()
        model = self._models(blend_rows="local")[0]
        exporter = self.make_exporter([model])
        builder = M_IniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, model)
        text = "\n".join(_builder_lines(builder))
        self.assertNotIn("vs-t0 = ", text, "判定 local ⇒ 不得重绑（重绑才叫错位）")
        self.assertIn("draw = 999, 0", text, "退回基类路径（保持与今天相同的渲染行为）")
        self.assertIn("= copy vs-t0 unless_null", text, "本族 palette 捕获必须仍在")
        self.assertIn("run = " + _runtime.attach_section_name("aaaa1111"), text)
        verdict = exporter.himi_blend_namespace_verdicts()["aaaa1111"]
        self.assertEqual(verdict["verdict"], "local")
        self.assertIn("局部", verdict["note"])

    def test_blend_namespace_identity_map_is_not_a_false_positive(self):
        """VGMap 是恒等映射时，导出值 == 原局部值 **就是**全局编号（不得误判）。"""
        identity = {str(i): i for i in range(4)}
        # slot_count = 4：恒等映射的值域就是 [0,4) ⇒ 该族区间密集且覆盖骨架。
        self._workspace_with_two_parts(vg_map=identity, slot_count=4)
        model = self._models(blend_rows="local")[0]  # 值相同，但映射是恒等
        exporter = self.make_exporter([model])
        builder = M_IniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, model)
        self.assertIn("vs-t0", "\n".join(_builder_lines(builder)))
        self.assertEqual(
            exporter.himi_blend_namespace_verdicts()["aaaa1111"]["verdict"], "global"
        )

    def test_blend_namespace_unavailable_still_rebinds(self):
        """没有可核对的导出 Blend（例如 buffers-only 轮次）⇒ 不阻断，按允许处理。"""
        self._workspace_with_two_parts()
        model = self._models(blend_rows="none")[0]
        exporter = self.make_exporter([model])
        builder = M_IniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, model)
        self.assertIn("vs-t0", "\n".join(_builder_lines(builder)))
        self.assertEqual(
            exporter.himi_blend_namespace_verdicts()["aaaa1111"]["verdict"], "unavailable"
        )

    def test_vb_section_falls_back_on_blend_hash_mismatch(self):
        """hash 与计划记录不一致 ⇒ 不重绑（风险落在「落到别的 pass」上），

        但**该族块仍必须被写**：捕获行读的是这笔 pass 自己的 vs-t0，与重绑无关
        （【t95】与 :meth:`ExportHIMI._himi_rebind_for` 解耦）。
        """
        self._workspace_with_two_parts()
        model = self._models()[0]
        model.category_hash_dict["Blend"] = "deadbeef"
        exporter = self.make_exporter([model])
        builder = M_IniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, model)
        text = "\n".join(_builder_lines(builder))
        self.assertNotIn("vs-t0 = ", text)
        self.assertIn("draw = 999, 0", text, "hash 不一致必须退回基类的渲染 draw 数")
        self.assertIn("= copy vs-t0 unless_null", text, "本族块仍要有人写")
        self.assertIn("run = " + _runtime.attach_section_name("aaaa1111"), text)

    def test_vb_section_falls_back_when_family_incomplete(self):
        self._workspace_with_two_parts()
        # 只导出第一个部件 ⇒ G1 拒绝整个家族
        model = self._models(vg_counts=(4, 0))[0]
        exporter = self.make_exporter([model])
        builder = M_IniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, model)
        text = "\n".join(_builder_lines(builder))
        self.assertNotIn("vs-t0", text)
        self.assertIn("draw = 999, 0", text)

    def test_resource_section_emitted_once_and_publishes(self):
        info = self._workspace_with_two_parts()
        models = self._models()
        exporter = self.make_exporter(models)
        builder = M_IniBuilder()
        for model in models:
            exporter.add_unity_vs_resource_vb_sections(builder, model)
        lines = _builder_lines(builder)
        resource_name = f"ResourceHI3MergedSkeleton_{info['role_key']}"
        self.assertEqual(lines.count(f"[{resource_name}]"), 1, "角色级资源只能声明一次")
        parsed = _runtime.parse_ini_resource_block(
            lines[lines.index(f"[{resource_name}]") : lines.index(f"[{resource_name}]") + 6]
        )
        self.assertEqual(parsed["type"], "RWBuffer")
        self.assertEqual(parsed["array"], str(SLOT_COUNT * 3))
        self.assertEqual(parsed["bind_flags"], "shader_resource unordered_access")
        self.assertNotIn("stride", parsed)
        self.assertNotIn(
            "filename", parsed, "活骨架不得声明 filename（t51；每帧由 attach CS 填充）"
        )
        # 【t54 步骤②】逐族副本声明 + 逐族 attach 段：与 plan.family_ranges **同源**、一一对应
        plan = exporter.himi_merged_runtime_plan()
        wanted = {
            str(fam): int(dict(info)["slots"])
            for fam, info in plan.family_ranges.items()
            if bool(dict(info).get("own_block", False))
        }
        self.assertTrue(wanted, "夹具前提：至少一族有专属块")
        text = "\n".join(lines)
        for fam, slots in sorted(wanted.items()):
            decl_name = f"[ResourceHI3PaletteFam_{fam}]"
            self.assertEqual(lines.count(decl_name), 1, f"{fam}: 副本声明必须恰好一次")
            idx = lines.index(decl_name)
            block = _runtime.parse_ini_resource_block(lines[idx : idx + 4])
            self.assertEqual(block["type"], "Buffer", "副本必须是 Buffer 形态")
            self.assertEqual(block["stride"], "48", "副本 stride 必须 48（48 B/骨）")
            self.assertEqual(block["array"], str(slots), "副本 array 必须等于该族槽数")
            self.assertNotIn(
                "format", block, "副本绝不能加 format（ByteWidth 会缩到 16×槽数 ⇒ copy 溢出）"
            )
            attach_name = f"[CustomShaderHI3MergedSkeletonAttach_{fam}]"
            self.assertEqual(lines.count(attach_name), 1, f"{fam}: attach 段必须恰好一次")
            start = lines.index(attach_name)
            # 【t39-X/t74】段体 = 段名 + [flags, cs, cs-t0, cs-t1, cs-cb1, cs-u0, Dispatch,
            # cs-u0 = null] = 9 行；发布物体矩阵表时再加 `cs-u1 = ref …` 与段末
            # `cs-u1 = null` = 11 行（形状见
            # HIMIAttachResourceTests::test_attach_section_matches_the_mod_reference_shape）。
            # 按「下一个 section 起点」切段，避免固定行数把 Dispatch / 解绑行切掉。
            end = start + 1
            while end < len(lines) and not lines[end].startswith("["):
                end += 1
            attach = lines[start:end]
            # 段尾的空行（``new_line()``）不属于段体，否则 ``attach[-1]`` 是 ``''``。
            while attach and not attach[-1].strip():
                attach.pop()
            self.assertEqual(attach[0], attach_name)
            # 【t75 口径变更】用户要求「这个着色器需要合并成一个」⇒ 每族的段都引用**同一个**
            # `./res/himi_attach.hlsl`，不再有逐族的 `himi_attach_<fam>.hlsl`。
            # 基址也不再逐族写进 #define，改由该 shader 用源槽位数在运行期查表算（见
            # HIMIMergedAttachCsTests 的判据）。
            self.assertIn(
                f"cs = {_himi_exporter.MERGED_ATTACH_HLSL_PATH}",
                attach,
                "5 个族段必须共用同一个 CS 文件（单文件口径）",
            )
            self.assertNotIn(
                f"himi_attach_{fam}.hlsl",
                "\n".join(attach),
                "不得再出现逐族 CS 文件（单文件口径）",
            )
            self.assertIn(f"cs-t0 = ref ResourceHI3PaletteFam_{fam}", attach, "t0 = 本族当帧副本")
            self.assertIn(f"cs-u0 = ref {resource_name}", attach, "u0 = 合并骨架")
            self.assertIn(f"Dispatch = {max(1, -(-slots // 64))}, 1, 1", attach, "Dispatch = ceil(槽数/64)")
            self.assertIn("cs-u0 = null", attach, "段内必须解绑 u0")
            if any(line.startswith(f"{_runtime.OBJECT_MATRIX_REGISTER} = ref ") for line in attach):
                self.assertEqual(
                    attach[-1],
                    f"{_runtime.OBJECT_MATRIX_REGISTER} = null",
                    "发布 cs-u1 后必须在段末解绑（不留悬挂引用）",
                )
            self.assertNotIn("x1", "\n".join(attach), "不得用 ini 参数纹理传参（fork 相关）")
        # 声明与段的数量必须等于「有专属块的族数」（不多不少，也不给借位族发段）
        self.assertEqual(text.count("[ResourceHI3PaletteFam_"), len(wanted))
        self.assertEqual(text.count("[CustomShaderHI3MergedSkeletonAttach_"), len(wanted))
        # 【t75】attach 段全部 `cs =` 指向同一个文件（防止回退成每族一份）；
        # 【t94】蒙皮段是**另一个** CS（`himi_merged_skin.hlsl`，部署产物形态），
        # 两族各用自己的、互不混用，也不得再出现逐族 `<fam>` 变体。
        attach_cs_values = sorted(
            {
                line.split("=", 1)[1].strip()
                for line in lines
                if re.match(r"^\s*cs\s*=", line)
                and line.split("=", 1)[1].strip()
                == _himi_exporter.MERGED_ATTACH_HLSL_PATH
            }
        )
        self.assertEqual(
            attach_cs_values,
            [_himi_exporter.MERGED_ATTACH_HLSL_PATH],
            "attach 段的 `cs =` 必须唯一指向 attach CS",
        )
        all_cs_values = {
            line.split("=", 1)[1].strip()
            for line in lines
            if re.match(r"^\s*cs\s*=", line)
        }
        allowed_cs = {_himi_exporter.MERGED_ATTACH_HLSL_PATH}
        skin_sections = [
            name for name in wanted if f"[{_runtime.skin_section_name(name)}]" in text
        ]
        if skin_sections:
            allowed_cs.add(_himi_exporter.MERGED_SKIN_HLSL_PATH)
            for fam in skin_sections:
                start = lines.index(f"[{_runtime.skin_section_name(fam)}]")
                end = start + 1
                while end < len(lines) and not lines[end].startswith("["):
                    end += 1
                skin = [line for line in lines[start:end] if re.match(r"^\s*cs\s*=", line)]
                self.assertEqual(
                    [line.split("=", 1)[1].strip() for line in skin],
                    [_himi_exporter.MERGED_SKIN_HLSL_PATH],
                    f"{fam}: 蒙皮段只能引用蒙皮 CS",
                )
        self.assertEqual(
            all_cs_values,
            allowed_cs,
            "`cs =` 只允许 attach CS 与蒙皮 CS 两个文件，且不得出现逐族变体",
        )
        self.assertEqual(
            exporter.himi_attach_cs_paths(),
            [_himi_exporter.MERGED_ATTACH_HLSL_PATH] * len(wanted),
            "导出发射的 cs 值序列 = 每个可发段的族一条、且值全部相同",
        )
        # 骨架二进制已发布到当前 buffer 目录
        published = os.path.join(self.mod_folder, "Meshes", info["skeleton_name"])
        self.assertTrue(os.path.isfile(published))
        with open(published, "rb") as handle:
            self.assertEqual(handle.read(), info["skeleton_bytes"])

    def test_resource_section_absent_without_rebinds(self):
        exporter = self.make_exporter(self._models())  # 无工作空间数据
        builder = M_IniBuilder()
        exporter.add_unity_vs_resource_vb_sections(builder, self._models()[0])
        self.assertNotIn("ResourceHI3MergedSkeleton_", "\n".join(_builder_lines(builder)))

    def test_export_output_has_the_perframe_sections_and_no_zzmi_names(self):
        """【t54 改写】步骤② 之后产物**必然**含 ``CustomShader…Attach_*`` / ``cs-t0`` / ``Dispatch``
        ——这就是逐帧机制本身（原用例断言的是「步骤① 阶段尚无 CS」的过渡态，前提已过时）。

        改写后钉死的是**更强**的两组：① 该有的都有且顺序正确（捕获 → run → 重绑）；
        ② 不该有的一个都没有（ZZMI/EFMI 名字、IniParams 传参、``[Constants]``/``[Present]``
        计数器、``numthreads``）。
        """
        self._workspace_with_two_parts()
        models = self._models()
        exporter = self.make_exporter(models)
        builder = M_IniBuilder()
        for model in models:
            exporter.add_unity_vs_texture_override_vb_sections(builder, model)
            exporter.add_unity_vs_resource_vb_sections(builder, model)
        lines = _builder_lines(builder)
        text = "\n".join(lines)
        low = text.lower()
        for forbidden in (
            "resourcezz",
            "poskey",
            "pool_size",
            "res/zzmi",
            "zzmi",
            "efmi",
            "[constants]",
            "[present]",
            "numthreads",
            "iniparams",
            "if $zz",
        ):
            self.assertNotIn(forbidden, low, f"HIMI 产物不得含 {forbidden}")

        family = str(models[0].draw_ib)
        capture = f"ResourceHI3PaletteFam_{family} = copy vs-t0 unless_null"
        run = f"run = CustomShaderHI3MergedSkeletonAttach_{family}"
        rebind = f"vs-t0 = ResourceHI3MergedSkeleton_"
        self.assertIn(capture, text, "① 捕获行必须存在")
        self.assertNotIn(
            f"copy vs-t0 = ResourceHI3PaletteFam_{family}", text,
            "B-1：方向不得写反（LHS 是目标）",
        )
        self.assertIn(run, text, "② run attach 段必须存在")
        self.assertIn(rebind, text, "③ 重绑必须存在")
        self.assertLess(text.index(capture), text.index(run), "顺序必须是 捕获 → run")
        self.assertLess(text.index(run), text.index(rebind), "B-2：run 必须排在重绑之前")
        self.assertLess(
            text.index(capture), text.index(rebind), "B-2：捕获必须排在重绑之前（否则自反馈）"
        )
        # 源码级顺序护栏：对产物本身再跑一次（空列表 = 通过）
        self.assertEqual(
            _himi_exporter.blend_block_order_problems(lines), [], "产物必须通过顺序护栏"
        )

    def test_ib_sections_get_run_before_every_drawindexed(self):
        """【t54 步骤②】每一笔 ``drawindexed`` **之前**必须有 ``run = <该族 attach>``。

        为什么：骨架由各族 CS 逐帧填充，而渲染 draw 可能发生在该族 deform pass 之外的时刻；
        CS 幂等 ⇒ 每笔前插一次是安全且必要的（少了它 ⇒ 骨架为空 ⇒ 模型整体消失）。
        本宿主把 ``M_IniHelper.get_drawindexed_str_list`` 替成空列表（不渲染基类），
        这里临时换成真实形状的 drawindexed 行来验证插入位置。
        """
        self._workspace_with_two_parts()
        model = self._models()[0]
        # IB 段只有在 submesh_ib_dict 有内容时才会走到 drawindexed（否则 `ib = null` 提前 continue）
        model.submesh_ib_dict = {
            str(submesh.unique_str): b"\x00\x00\x00\x00" for submesh in model.submesh_model_list
        }
        exporter = self.make_exporter([model])
        helper = sys.modules[f"{PKG}.common.m_ini_helper"].M_IniHelper
        original = helper.get_drawindexed_str_list
        helper.get_drawindexed_str_list = lambda calls, obj_name_draw_offset_dict=None: [
            "if $costume_mods",
            "  ; [mesh:LOD0.aaaa1111-1-0] [vertex_count:30]",
            "  drawindexed = 30, 0, 0",
            "endif",
            "drawindexed = 12, 30, 0",
        ]
        self.addCleanup(setattr, helper, "get_drawindexed_str_list", original)
        builder = M_IniBuilder()
        exporter.add_unity_vs_texture_override_ib_sections(builder, model)
        lines = _builder_lines(builder)
        family = str(model.draw_ib)
        attach = f"run = CustomShaderHI3MergedSkeletonAttach_{family}"
        drawindexed_lines = [i for i, line in enumerate(lines) if line.strip().startswith("drawindexed")]
        self.assertEqual(
            len(drawindexed_lines),
            4,
            f"夹具前提：两个 submesh × 两笔 drawindexed；实读 {lines}",
        )
        for index in drawindexed_lines:
            self.assertEqual(
                lines[index - 1],
                attach,
                "run 必须紧贴在该笔 drawindexed **前一行**（幂等，且不得插到 if 之外）",
            )
        self.assertEqual(lines.count(attach), 4, "每笔 drawindexed 前各一次")

    def test_ib_sections_never_inject_the_merged_skin_pass(self):
        """【t100 回归】IB 段里**不得**出现带蒙皮的两行：``run = …MergedSkin_<fam>`` / ``vb0 = ResourceHI3SkinOut_<fam>``。

        为什么（t100 实机事故，m01572）：``_himi_rebind_for`` 放行后 IB 段的 ``skin_entry``
        变成非空 ⇒ 每笔 ``drawindexed`` 前多出「跑蒙皮 CS + 把 vb0 换成自建蒙皮输出」。
        那条 CS（``Toolset/himi_merged_skin.hlsl``）编译失败会被 3DMigoto 静默跳过 ——
        dispatch 照常记录、缓冲一个字节都不写 ⇒ 顶点塌到原点、物体消失且不报错
        （t89 实机）。渲染侧唯一的职责是 ``cb3 捕获 + run attach``（t74 路线）。
        """
        self._workspace_with_two_parts()
        model = self._models()[0]
        model.submesh_ib_dict = {
            str(submesh.unique_str): b"\x00\x00\x00\x00" for submesh in model.submesh_model_list
        }
        exporter = self.make_exporter([model])
        helper = sys.modules[f"{PKG}.common.m_ini_helper"].M_IniHelper
        original = helper.get_drawindexed_str_list
        helper.get_drawindexed_str_list = lambda calls, obj_name_draw_offset_dict=None: [
            "drawindexed = 30, 0, 0"
        ]
        self.addCleanup(setattr, helper, "get_drawindexed_str_list", original)
        builder = M_IniBuilder()
        exporter.add_unity_vs_texture_override_ib_sections(builder, model)
        text = "\n".join(_builder_lines(builder))
        family = str(model.draw_ib)
        # 前提：本夹具确实走到了注入路径（attach 行存在），否则下面的断言恒真。
        self.assertIn(f"run = CustomShaderHI3MergedSkeletonAttach_{family}", text)
        self.assertNotIn("CustomShaderHI3MergedSkin_", text, "蒙皮 CS 不得被 run")
        self.assertNotIn("ResourceHI3SkinOut_", text, "vb0 不得换成蒙皮输出")

    def test_ib_sections_are_untouched_without_rebind(self):
        """护栏（非恒真）：不通过 G1 的 DrawIB 的 IB 段**逐字**等于基类产物（不插 run）。"""
        exporter = self.make_exporter(self._models())  # 无工作空间数据 ⇒ 无重绑
        model = self._models()[0]
        model.submesh_ib_dict = {
            str(submesh.unique_str): b"\x00\x00\x00\x00" for submesh in model.submesh_model_list
        }
        helper = sys.modules[f"{PKG}.common.m_ini_helper"].M_IniHelper
        original = helper.get_drawindexed_str_list
        helper.get_drawindexed_str_list = lambda calls, obj_name_draw_offset_dict=None: [
            "drawindexed = 30, 0, 0"
        ]
        self.addCleanup(setattr, helper, "get_drawindexed_str_list", original)
        builder = M_IniBuilder()
        exporter.add_unity_vs_texture_override_ib_sections(builder, model)
        self.assertNotIn("run = ", "\n".join(_builder_lines(builder)))
        plain_builder = M_IniBuilder()
        _unity.ExportUnity.add_unity_vs_texture_override_ib_sections(
            exporter, ini_builder=plain_builder, drawib_model=model
        )
        self.assertEqual(_builder_lines(builder), _builder_lines(plain_builder), "必须逐字走基类")

    def test_blend_order_guard_is_not_vacuous(self):
        """源码级顺序护栏：正例通过；四种坏形态各自必须被抓到（**非恒真**）。"""
        guard = _himi_exporter.blend_block_order_problems
        family = "aaaa1111"
        palette = f"ResourceHI3PaletteFam_{family} = copy vs-t0 unless_null"
        run = f"run = CustomShaderHI3MergedSkeletonAttach_{family}"
        rebind = "vs-t0 = ResourceHI3MergedSkeleton_RoleA_00000000"
        good = [palette, run, rebind, "handling = skip", "draw = 999, 0"]
        self.assertEqual(guard(good), [], "正例必须通过")

        reversed_direction = [
            f"copy vs-t0 = ResourceHI3PaletteFam_{family} unless_null",
            run, rebind, "handling = skip", "draw = 999, 0",
        ]
        self.assertTrue(
            any("方向写反" in p for p in guard(reversed_direction)),
            "B-1：写反方向必须被抓到（会把空副本写进游戏当帧 palette）",
        )
        capture_after_rebind = [run, rebind, palette, "handling = skip", "draw = 999, 0"]
        self.assertTrue(
            any("之后" in p for p in guard(capture_after_rebind)),
            "B-2：捕获排在重绑之后必须被抓到（自反馈）",
        )
        run_after_rebind = [palette, rebind, run, "handling = skip", "draw = 999, 0"]
        self.assertTrue(
            any("之后" in p for p in guard(run_after_rebind)),
            "B-2：run 排在重绑之后必须被抓到",
        )
        run_before_capture = [run, palette, rebind, "handling = skip", "draw = 999, 0"]
        self.assertTrue(any("之前" in p for p in guard(run_before_capture)), "run 排在捕获之前")
        self.assertTrue(any("没有捕获行" in p for p in guard([run, rebind, "draw = 999, 0"])))
        self.assertTrue(any("run =" in p for p in guard([palette, rebind, "draw = 999, 0"])))

    def test_rebind_without_family_block_emits_no_capture_or_attach(self):
        """借位族（``own_block=False``）**不发**捕获行/attach 段，但重绑仍在（t53 §借位语义）。"""
        self._workspace_with_two_parts()
        model = self._models()[0]
        exporter = self.make_exporter([model])
        original = exporter.himi_family_range
        exporter.himi_family_range = lambda family: None  # type: ignore[assignment]
        self.addCleanup(setattr, exporter, "himi_family_range", original)
        builder = M_IniBuilder()
        exporter.add_unity_vs_texture_override_vb_sections(builder, model)
        text = "\n".join(_builder_lines(builder))
        self.assertNotIn("ResourceHI3PaletteFam_", text, "没有专属块 ⇒ 不得发捕获行")
        self.assertNotIn("run = ", text, "没有专属块 ⇒ 不得发 run")
        self.assertIn("vs-t0 = ResourceHI3MergedSkeleton_", text, "重绑仍必须存在")
        self.assertEqual(_himi_exporter.blend_block_order_problems(text.splitlines()), [])

    def test_export_proceeds_when_checkbox_off_TWO_STATE(self):
        """【t71】开关关闭 + 有合并缓存 ⇒ **不再中止**（用户实报的误拦），走普通模式继续导出。

        本用例钉「误拦发生的那一关」：`_himi_enforce_contract()` 在开关关闭时必须返回 warning、
        **不抛 Fatal**（旧行为在这里抛 Fatal 拦死普通导出）。
        另一半（真阳性：开关打开 + 全部被拒 ⇒ Fatal）由契约层用例
        `test_all_parts_rejected_is_error` 与 `.dbg/_t71_two_state.py`（走真实 level 处理链）覆盖。
        """
        _make_workspace(
            self.workspace,
            "RoleA",
            [{"bare": "aaaa1111-1-0", "draw_ib": "aaaa1111", "vg_count": 4,
              "original_vertex_count": 120, "family_members": ["aaaa1111-1-0"]}],
        )
        _properties_state["checkbox"] = False  # 有数据但开关关闭 ⇒ 普通模式，放行
        exporter = self.make_exporter(self._models())
        decision = exporter._himi_enforce_contract()      # 误拦就在这一步抛过 Fatal
        self.assertEqual(decision["level"], "warning", decision)
        self.assertIn("普通模式", decision["message"])
        self.assertIn("局部编号", decision["hint"])
        self.assertEqual(
            exporter.himi_merged_contract_decision()["level"], "warning",
            "判定要记在导出器上（供 ini/日志引用）",
        )

    def test_contract_decision_is_recorded_on_the_exporter(self):
        self._workspace_with_two_parts()
        exporter = self.make_exporter(self._models())
        decision = exporter.himi_merged_contract_decision()
        self.assertEqual(decision, {}, "尚未判定时为 {}")
        exporter._himi_enforce_contract()
        self.assertEqual(exporter.himi_merged_contract_decision()["level"], "ok")
        self.assertTrue(exporter.himi_merged_runtime_plan().has_rebinds)
        self.assertIn("资源声明", exporter.himi_merged_diagnostics())

    def test_exporter_exposes_the_unverified_in_game_list(self):
        self.assertTrue(_himi_exporter.UNVERIFIED_IN_GAME)
        joined = "\n".join(_himi_exporter.UNVERIFIED_IN_GAME)
        for token in ("V1", "V2", "V3"):
            self.assertIn(token, joined)


# --------------------------------------------------------------------------- #
# E. 独立性：不引用 ZZMI、不引用 bpy
# --------------------------------------------------------------------------- #


class HIMIIndependenceTests(unittest.TestCase):
    def _imported_modules(self, relpath):
        tree = ast.parse(_read_text(relpath))
        names = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                names.append(node.module or "")
        return names

    def test_new_code_does_not_import_zzmi_or_efmi(self):
        for relpath in (RUNTIME_REL, EXPORTER_REL):
            for name in self._imported_modules(relpath):
                lowered = name.lower()
                self.assertNotIn("zzmi", lowered, f"{relpath} 不得依赖 ZZMI：{name}")
                self.assertNotIn("efmi_skeleton", lowered, f"{relpath} 不得依赖 EFMI：{name}")

    def test_new_code_does_not_import_bpy(self):
        for relpath in (RUNTIME_REL, EXPORTER_REL):
            for name in self._imported_modules(relpath):
                self.assertNotEqual(name.split(".")[0], "bpy", f"{relpath} 不得依赖 bpy")

    def test_runtime_module_is_importable_without_bpy(self):
        # 本测试的 fake 宿主**没有**注册 bpy；模块与导出器都已成功导入。
        for module in (_runtime, _himi_exporter):
            self.assertTrue(getattr(module, "__file__", ""))
        self.assertNotIn(
            "bpy",
            [name for name in sys.modules if name == "bpy" and not isinstance(sys.modules[name], types.ModuleType)],
        )

    def test_no_zzmi_module_was_loaded_under_the_fake_package(self):
        loaded = [name for name in sys.modules if name.startswith(PKG) and "zzmi" in name.lower()]
        self.assertEqual(loaded, [], "fake 宿主里加载了 ZZMI 模块 ⇒ HIMI 侧产生了依赖")


# --------------------------------------------------------------------------- #
# F. 真实工作空间（只读）：不变量
# --------------------------------------------------------------------------- #


class HIMIRealWorkspaceInvariantTests(unittest.TestCase):
    """只读扫五个真实 HIMI 工作空间：有数据 ⇒ 骨架必须自洽；无数据 ⇒ 普通导出。"""

    HIMI_ROOT = r"K:\SSMT-Package-master\WorkSpace\HIMI"

    def test_real_workspaces_stay_consistent(self):
        root = self.HIMI_ROOT
        if not os.path.isdir(root):
            self.skipTest(f"真实 HIMI 工作空间不存在：{root}")
        roles = sorted(
            name for name in os.listdir(root) if os.path.isdir(os.path.join(root, name))
        )
        self.assertTrue(roles)
        for role in roles:
            role_root = os.path.join(root, role)
            plan = _runtime.load_workspace_plan(role_root)
            with self.subTest(role=role):
                if not plan.ok:
                    # 没有合并数据（尚未用合并开关导入过）⇒ 普通导出路径，必须没有重绑。
                    self.assertEqual(plan.rebinds, [])
                    continue
                self.assertGreater(plan.slot_count, 0)
                self.assertTrue(os.path.isfile(plan.skeleton_source_path))
                size = os.path.getsize(plan.skeleton_source_path)
                self.assertEqual(size, plan.slot_count * 48)
                self.assertEqual(
                    hashlib.sha256(
                        open(plan.skeleton_source_path, "rb").read()
                    ).hexdigest(),
                    plan.skeleton_digest,
                )
                decl = plan.resource()
                self.assertEqual(decl.array, plan.slot_count * 3)
                for rebind in plan.rebinds:
                    self.assertEqual(
                        rebind["vs_t0_resource"],
                        f"ResourceHI3MergedSkeleton_{plan.role_key}",
                    )
                    self.assertGreaterEqual(rebind["vg_count"], 1)


class HIMICacheOnlyOracleTests(HIMIMergedRuntimeBase):
    """R-T14-1：``prepare_gate_oracle(target_bares=None)`` 不得把空 writes 当权威判定。

    场景 = 「dump 缺失 + 缓存完好 + ``target_bares=None``」：数据层的 ``prepare`` 会走
    cache-only 捷径（``ok=True`` 但 ``writes=[]``），若被当成 G1 权威就会得到
    ``ok=True / gate_source="prepare" / rebinds=0`` —— 正是「空 writes 被当权威」，
    会让导出侧的保守退路静默失效。
    """

    HIMI_ROOT = r"K:\SSMT-Package-master\WorkSpace\HIMI"

    # -- 单元级：直接钉住 cache-only 形状的 prepare 结果 -- #

    def _cache_only_prepare_result(self, root):
        """数据层 cache-only 成功的返回形状（字段照 common/himi_skeleton.py:1562-1589）。"""
        return {
            "ok": True,
            "cache_only": True,
            "gate_source": "cache-only",
            "cache_intact": True,
            "message": (
                "dump 目录缺失，但工作空间缓存完好的 2 个组件与角色级骨架"
                f"（{SLOT_COUNT} 槽）已通过完整性校验；本次不重算、不写盘（cache-only）"
            ),
            "workspace_root": root,
            "dump_dir": "",
            "resolver": None,
            "slot_count": SLOT_COUNT,
            "skeleton_bytes": b"",
            "metadata": {},
            "writes": [],
            "gate_rejected": [],
        }

    def test_prepare_gate_oracle_rejects_a_cache_only_result(self):
        from unittest import mock

        def _fake_prepare(root, unique_str_list=None, force=False, target_bares=None):
            return self._cache_only_prepare_result(root)

        with mock.patch.object(
            _skeleton.HIMISkeletonMergeHelper, "prepare", staticmethod(_fake_prepare)
        ):
            oracle = _runtime.prepare_gate_oracle(self.workspace, target_bares=None)
        self.assertFalse(oracle["available"], "cache-only 结果绝不能被当成 G1 权威")
        self.assertFalse(oracle["pass_level"])
        self.assertEqual(oracle["safe_bares"], [])
        self.assertIn("pass 级", oracle["message"])
        self.assertIn("非权威", oracle["message"])

    def test_prepare_gate_oracle_requires_positive_evidence_too(self):
        """**物证**判据：即便没有任何 cache-only 标记，缺正向物证也不得被采纳（fail-closed）。"""
        from unittest import mock

        def _fake_prepare(root, unique_str_list=None, force=False, target_bares=None):
            plan = self._cache_only_prepare_result(root)
            plan.pop("cache_only")  # 声明信号全部拿掉
            plan["gate_source"] = "some-future-shortcut"
            return plan

        with mock.patch.object(
            _skeleton.HIMISkeletonMergeHelper, "prepare", staticmethod(_fake_prepare)
        ):
            oracle = _runtime.prepare_gate_oracle(self.workspace, target_bares=None)
        self.assertFalse(oracle["available"], "没有 pass 级物证 ⇒ fail-closed 拒绝")
        self.assertFalse(oracle["pass_level"])
        self.assertIn("正向证据", oracle["message"])

    def test_prepare_gate_oracle_accepts_a_dump_based_result(self):
        """反向：带正向物证的真反查结果必须被采纳（fail-closed 不能误杀正常路径）。"""
        from unittest import mock

        def _fake_prepare(root, unique_str_list=None, force=False, target_bares=None):
            return {
                "ok": True,
                "cache_intact": True,
                "dump_dir": r"K:\dumps\FrameAnalysis-2026-09-23-135739",
                "resolver": object(),
                "metadata": {"SkeletonDigest": "a" * 64, "Slots": []},
                "slot_count": SLOT_COUNT,
                "writes": [{"bare": "aaaa1111-1-0"}],
                "gate_rejected": [],
                "message": "已扫描 2 个组件",
            }

        with mock.patch.object(
            _skeleton.HIMISkeletonMergeHelper, "prepare", staticmethod(_fake_prepare)
        ):
            oracle = _runtime.prepare_gate_oracle(self.workspace, target_bares=[])
        self.assertTrue(oracle["available"], oracle)
        self.assertTrue(oracle["pass_level"])
        self.assertEqual(oracle["safe_bares"], ["aaaa1111-1-0"])

    def test_oracle_authority_matrix(self):
        """``oracle_authority`` 判据表（含「真 pass 级空放行」必须被采纳）。"""
        self.assertEqual(_runtime.oracle_authority(None), (False, "未提供权威门控"))
        self.assertFalse(_runtime.oracle_authority({"available": True, "stale": True})[0])
        adopt, reason = _runtime.oracle_authority(
            {
                "available": True,
                "cache_intact": True,
                "pass_level": False,
                "safe_bares": [],
            }
        )
        self.assertFalse(adopt)
        self.assertIn("pass 级重算", reason)
        # 带「未重算」证据（cache-only 词汇）的外来 oracle 同样拒绝
        self.assertFalse(
            _runtime.oracle_authority(
                {"available": True, "cache_only": True, "safe_bares": ["x"]}
            )[0]
        )
        self.assertFalse(
            _runtime.oracle_authority(
                {"available": True, "gate_source": "cache-only-invalid", "safe_bares": []}
            )[0]
        )
        # 真 pass 级、放行 0 个（dump 在场、按导出集合重算）⇒ 必须采纳
        self.assertTrue(
            _runtime.oracle_authority(
                {
                    "available": True,
                    "pass_level": True,
                    "cache_intact": True,
                    "safe_bares": [],
                    "rejected": {"aaaa1111": {"family": "aaaa1111", "missing": ["x"]}},
                }
            )[0]
        )
        self.assertTrue(_runtime.oracle_authority({"available": True})[0])
        self.assertFalse(_runtime.oracle_authority({"available": False})[0])

    def _plan_kwargs(self):
        members = ["aaaa1111-1-0"]
        parts = [
            {
                "bare": members[0],
                "draw_ib": "aaaa1111",
                "vg_count": 4,
                "original_vertex_count": 120,
                "family_members": members,
                "category_hash": "8322ac5a",
                "gate_safe": True,
            }
        ]
        payload = _skeleton_bytes(SLOT_COUNT)
        metadata = {
            "AlgorithmVersion": 1,
            "GateVersion": 1,
            "RoleKey": "role_ab12",
            "SlotCount": SLOT_COUNT,
            "SkeletonFileName": "role_ab12-MergedSkeleton.buf",
            "SkeletonDigest": hashlib.sha256(payload).hexdigest(),
        }
        return metadata, parts, payload

    def test_plan_from_metadata_refuses_a_cache_only_oracle(self):
        """喂进一个 cache-only 形状的 oracle：结果**不是** ok/prepare/rebinds=0。"""
        metadata, parts, payload = self._plan_kwargs()
        oracle = {
            "available": True,  # 旧行为：cache-only 也自称可用
            "stale": False,
            "cache_intact": True,
            "pass_level": False,
            "gate_source": "cache-only",
            "safe_bares": [],
            "rejected": {},
            "message": "cache-only",
        }
        plan = _runtime.plan_from_metadata(
            metadata,
            parts,
            target_bares=None,
            skeleton_bytes=payload,
            gate_oracle=oracle,
        )
        self.assertFalse(
            plan.ok and plan.gate_source == "prepare" and plan.component_count == 0,
            "cache-only 的空 writes 绝不能变成『自称权威、0 重绑』的判定",
        )
        self.assertEqual(plan.gate_source, "cache-only")
        self.assertEqual(len(plan.rebinds), 1, "保守退路必须仍然放行缓存判定为安全的部件")
        self.assertIn("拒绝采用 G1 权威判定", plan.diagnostics_text())
        self.assertIn("cache-only", plan.diagnostics_text())

    def test_plan_from_metadata_adopts_a_real_pass_level_empty_verdict(self):
        """反向：真 pass 级、放行 0 个的判定必须被**采纳**（否则退路会去重绑）。"""
        metadata, parts, payload = self._plan_kwargs()
        members = ["aaaa1111-1-0"]
        oracle = {
            "available": True,
            "pass_level": True,
            "stale": False,
            "cache_intact": True,
            "safe_bares": [],
            "rejected": {
                "aaaa1111": {"family": "aaaa1111", "members": members, "missing": members}
            },
            "message": "pass 级重算",
        }
        plan = _runtime.plan_from_metadata(
            metadata,
            parts,
            target_bares=None,
            skeleton_bytes=payload,
            gate_oracle=oracle,
        )
        self.assertEqual(plan.gate_source, "prepare")
        self.assertEqual(plan.rebinds, [], "pass 级拒绝必须被采纳，不得退回缓存判定去重绑")
        self.assertIn("放行 0 个部件", plan.diagnostics_text())
        self.assertIn("不是退路失效", plan.diagnostics_text())

    # -- 真实数据级：dump 缺失 + 缓存完好 + target_bares=None -- #

    def _imported_copy(self):
        """真实角色工作空间副本 + 一次真实导入（缓存完好，dump 仍在场）。"""
        if not os.path.isdir(self.HIMI_ROOT):
            self.skipTest(f"真实 HIMI 工作空间不存在：{self.HIMI_ROOT}")
        role = ""
        for candidate in sorted(os.listdir(self.HIMI_ROOT)):
            if not os.path.isdir(os.path.join(self.HIMI_ROOT, candidate)):
                continue
            if os.path.isfile(
                os.path.join(self.HIMI_ROOT, candidate, "Config", "FrameAnalysisPath.json")
            ):
                role = candidate
                break
        if not role:
            self.skipTest("没有任何角色登记了 FrameAnalysis")
        root = os.path.join(self._tmp, role)
        shutil.copytree(
            os.path.join(self.HIMI_ROOT, role),
            root,
            ignore=shutil.ignore_patterns("DedupedTextures", "*.blend", "*.blend1", "*.dds"),
        )
        ok, message = _skeleton.HIMISkeletonMergeHelper.ensure_skeleton_data(root)
        self.assertTrue(ok, f"数据层落盘失败：{message}")
        return root

    def _break_dump(self, root):
        """把 dump 指针弄失效（FrameAnalysisPath.json + Tabs 都指向不存在的目录）。"""
        broken = os.path.join(self._tmp, "no-such-FrameAnalysis")
        with io.open(
            os.path.join(root, "Config", "FrameAnalysisPath.json"), "w", encoding="utf-8"
        ) as handle:
            json.dump(
                {"frameAnalysisFolderPath": broken, "selectedFrameAnalysis": "gone"},
                handle,
                ensure_ascii=False,
            )
        tabs_dir = os.path.join(root, "Config", "Tabs")
        if os.path.isdir(tabs_dir):
            for name in os.listdir(tabs_dir):
                if name.startswith("ws-tab-") and name.endswith(".json"):
                    with io.open(
                        os.path.join(tabs_dir, name), "w", encoding="utf-8"
                    ) as handle:
                        json.dump(
                            {"frameAnalysisFolderPath": broken}, handle, ensure_ascii=False
                        )
        self.assertEqual(
            _skeleton.resolve_frame_analysis_dir(root), "", "dump 指针必须已失效"
        )
        return root

    def test_real_dump_missing_target_none_is_not_authoritative(self):
        root = self._imported_copy()

        # ⓪ dump 在场：真反查结果必须被采纳（fail-closed 不得误杀正常路径）
        live = _runtime.prepare_gate_oracle(root, target_bares=None)
        self.assertTrue(live["available"], live)
        self.assertTrue(live["pass_level"])
        self.assertGreater(len(live["safe_bares"]), 0)

        self._break_dump(root)

        # ① 陷阱的源头：只复现缓存结论的 prepare 结果不得被当成权威
        oracle = _runtime.prepare_gate_oracle(root, target_bares=None)
        self.assertFalse(oracle["available"], f"未重算的结果不得自称权威：{oracle}")
        self.assertFalse(oracle["pass_level"])
        self.assertEqual(oracle["safe_bares"], [])
        self.assertIn("非权威", oracle["message"])
        print(
            "HIMI-R-T14-1",
            {
                "available": oracle["available"],
                "pass_level": oracle["pass_level"],
                "safe_bares": oracle["safe_bares"],
                "message": oracle["message"],
            },
        )

        # ② 端到端钉死：喂给 plan_from_metadata 后**不是** ok/prepare/rebinds=0
        cached = _runtime.load_workspace_plan(root, enable_gate_oracle=False)
        self.assertTrue(cached.ok, cached.message)
        with io.open(
            os.path.join(root, "Config", "HIMIMergedSkeleton.json"), encoding="utf-8"
        ) as handle:
            metadata = json.load(handle)
        with io.open(cached.skeleton_source_path, "rb") as handle:
            skeleton_bytes = handle.read()
        plan = _runtime.plan_from_metadata(
            metadata,
            cached.parts,
            target_bares=None,
            skeleton_bytes=skeleton_bytes,
            gate_oracle=oracle,
        )
        self.assertFalse(
            plan.ok and plan.gate_source == "prepare" and plan.component_count == 0,
            "R-T14-1：cache-only 的空 writes 被当成权威判定（ok/prepare/rebinds=0）",
        )
        self.assertEqual(plan.gate_source, "cache-only")
        self.assertEqual(len(plan.rebinds), len(cached.parts))
        self.assertIn("拒绝采用 G1 权威判定", plan.diagnostics_text())

        # ③ 已发布导出路径行为不变（全量有重绑 / 部分导出保守全部不重绑）
        full = _runtime.load_workspace_plan(root)
        self.assertTrue(full.ok, full.message)
        self.assertEqual(full.gate_source, "cache-only")
        self.assertEqual(len(full.rebinds), len(full.parts))
        self.assertGreater(len(full.rebinds), 0)
        first = full.parts[0]
        partial = _runtime.load_workspace_plan(
            root,
            export_unique_strs=[first.unique_str],
            export_bares=[first.bare],
        )
        self.assertEqual(partial.rebinds, [], "部分导出 + 无 dump 必须保守地全部不重绑")
        self.assertIn("只覆盖了部分已落盘部件", partial.diagnostics_text())
        self.assertIn("全部不重绑", partial.diagnostics_text())


class HIMISubsetExportTests(HIMIMergedRuntimeBase):
    """2026-09 实报：按家族局部导出被 stale 误判中止。

    真实场景（`嗨♪爱愿妖精♥`）：用户只导出某个 IB 家族的成员，`prepare(target_bares=子集)`
    按**子集**重建骨架 ⇒ 摘要与角色级缓存（全部部件的并集）不同 ⇒ 旧代码判 stale ⇒
    退回 cache-only ⇒ 覆盖性检查又要求「导出覆盖全部已落盘部件」⇒ 0 重绑 ⇒ 契约 error 中止。
    修好后：子集模式采用 G1 门控结论（跳过摘要比对），完整家族照常重绑，契约是 warning。
    """

    FAMILY_A = ["aaaa1111-1-0", "aaaa1111-2-0"]
    FAMILY_B = ["bbbb2222-1-0", "bbbb2222-2-0"]

    def _workspace(self, *, stale_bare=""):
        components = []
        for bare in self.FAMILY_A + self.FAMILY_B:
            draw_ib = bare.split("-")[0]
            entry = {
                "bare": bare,
                "draw_ib": draw_ib,
                "vg_count": 4,
                "original_vertex_count": 100,
                "family_members": list(
                    self.FAMILY_A if draw_ib == "aaaa1111" else self.FAMILY_B
                ),
            }
            if draw_ib == "bbbb2222":
                # 两个家族的值域必须**互补**（A=[0,4) / B=[4,8)）：t51 起族区间互相重叠
                # 会让两个 attach 段写同一批槽位（后写的赢），而「全量导出 ∑span == 槽数」
                # 还要求并集恰好铺满骨架。
                entry["vg_map"] = {0: 4, 1: 5, 2: 6, 3: 7}
            if bare == stale_bare:
                entry["digest"] = "0" * 64  # 与角色级骨架不同源
            components.append(entry)
        return _make_workspace(self.workspace, "RoleA", components)

    def _oracle(self, safe_bares, rejected=None, *, stale=False, subset=False, digest=""):
        return {
            "available": True,
            "stale": bool(stale),
            "pass_level": True,
            "subset": bool(subset),
            "source": "prepare",
            "safe_bares": list(safe_bares),
            "rejected": dict(rejected or {}),
            "message": "",
            "slot_count": SLOT_COUNT,
            "digest": digest,
            "cache_intact": True,
        }

    def _parts(self, info):
        return _runtime.load_workspace_plan(self.workspace, enable_gate_oracle=False).parts

    def test_partial_export_of_a_complete_family_rebinds_only_that_family_and_is_warning(self):
        """① 覆盖完整家族的部分导出 ⇒ 该家族重绑、其余不重绑，契约 level = warning。"""
        info = self._workspace()
        plan = _runtime.plan_from_metadata(
            info["metadata"],
            self._parts(info),
            target_bares=list(self.FAMILY_A),
            skeleton_bytes=info["skeleton_bytes"],
            gate_oracle=self._oracle(
                self.FAMILY_A,
                {
                    "bbbb2222": {
                        "family": "bbbb2222",
                        "members": self.FAMILY_B,
                        "missing": [self.FAMILY_B[1]],
                    }
                },
                subset=True,
            ),
        )
        self.assertTrue(plan.ok, plan.message)
        self.assertEqual(
            [item["bare"] for item in plan.rebinds],
            self.FAMILY_A,
            "覆盖完整的家族必须照常重绑",
        )
        self.assertEqual(list(plan.rejected), ["bbbb2222"], "未覆盖的家族不重绑")
        decision = _contract.evaluate_merged_skeleton_contract(
            **plan.contract_snapshot(checkbox_enabled=True)
        )
        self.assertEqual(
            decision["level"],
            "warning",
            "按家族局部导出是 G1 支持的路径，不得升级成 error 中止导出",
        )
        self.assertIn(_contract.DEFORM_GATE_REJECT_PREFIX, decision["hint"])
        self.assertIn(self.FAMILY_B[1], decision["hint"], "提示词要点名未纳入的成员")
        self.assertIn("IB 家族", decision["hint"], "提示词要给 G1 的正确处置")
        self.assertNotIn(
            "清除骨骼合并VGMap缓存", decision["hint"], "不得再建议清除正常数据"
        )

    def test_subset_export_is_not_stale_on_digest_mismatch(self):
        """② 子集模式下不得因骨架摘要不同而判 stale（摘要只在覆盖全量时才有比较意义）。"""
        from unittest import mock

        info = self._workspace()
        calls = {}

        def _fake_prepare(root, unique_str_list=None, force=False, target_bares=None):
            calls["target_bares"] = list(target_bares or [])
            return {
                "ok": True,
                "cache_intact": False,
                "dump_dir": r"K:\SSMT-Package-master\3Dmigoto\HI3\FrameAnalysis-2026-09-23-135739",
                "resolver": object(),
                "metadata": {"SkeletonDigest": "eb548446" + "0" * 56, "Slots": []},
                "slot_count": 184,
                "writes": [{"bare": bare} for bare in self.FAMILY_A],
                "gate_rejected": [
                    {
                        "bare": self.FAMILY_B[1],
                        "draw_ib": "bbbb2222",
                        "missing": [self.FAMILY_B[1]],
                    }
                ],
                "message": "已扫描 4 个组件；参与合并 2 个；全局唯一骨骼 184",
            }

        with mock.patch.object(
            _skeleton.HIMISkeletonMergeHelper, "prepare", staticmethod(_fake_prepare)
        ):
            plan = _runtime.load_workspace_plan(
                self.workspace,
                export_unique_strs=[f"LOD0.{bare}" for bare in self.FAMILY_A],
                export_bares=list(self.FAMILY_A),
            )
        self.assertEqual(
            calls["target_bares"],
            self.FAMILY_A,
            "目标集合必须原样交给数据层（G1 按导出集合重算）",
        )
        self.assertEqual(plan.gate_source, "prepare", "子集模式必须采用 G1 门控结论")
        self.assertEqual([item["bare"] for item in plan.rebinds], self.FAMILY_A)
        self.assertIn("子集导出", plan.diagnostics_text())
        decision = _contract.evaluate_merged_skeleton_contract(
            **plan.contract_snapshot(checkbox_enabled=True)
        )
        self.assertNotEqual(decision["level"], "error", decision)

    def test_full_export_still_detects_a_stale_oracle(self):
        """护栏：覆盖全部已落盘部件时，摘要不一致**仍然**判 stale（不得放松）。"""
        from unittest import mock

        info = self._workspace()
        all_bares = self.FAMILY_A + self.FAMILY_B

        def _fake_prepare(root, unique_str_list=None, force=False, target_bares=None):
            return {
                "ok": True,
                "cache_intact": True,
                "dump_dir": r"K:\dumps\FrameAnalysis-2026-09-23-135739",
                "resolver": object(),
                "metadata": {"SkeletonDigest": "f" * 64, "Slots": []},
                "slot_count": SLOT_COUNT,
                "writes": [{"bare": bare} for bare in all_bares],
                "gate_rejected": [],
                "message": "全量重算",
            }

        with mock.patch.object(
            _skeleton.HIMISkeletonMergeHelper, "prepare", staticmethod(_fake_prepare)
        ):
            plan = _runtime.load_workspace_plan(
                self.workspace,
                export_unique_strs=[f"LOD0.{bare}" for bare in all_bares],
                export_bares=list(all_bares),
            )
        self.assertEqual(plan.gate_source, "cache-only", "全量摘要不一致 ⇒ oracle 不可信")
        self.assertIn("不同源", plan.diagnostics_text())

    def test_truly_stale_part_is_still_rejected_in_subset_mode(self):
        """③ 真正陈旧的部件（逐部件摘要与角色级不一致）在子集模式下**仍然**被拒绝。"""
        stale_bare = self.FAMILY_A[1]
        info = self._workspace(stale_bare=stale_bare)
        plan = _runtime.plan_from_metadata(
            info["metadata"],
            self._parts(info),
            target_bares=list(self.FAMILY_A),
            skeleton_bytes=info["skeleton_bytes"],
            gate_oracle=self._oracle(self.FAMILY_A, subset=True),
        )
        self.assertNotIn(
            stale_bare,
            [item["bare"] for item in plan.rebinds],
            "逐部件摘要不一致 ⇒ 该部件不得重绑（这道护栏不许放松）",
        )
        self.assertIn("aaaa1111", plan.skip_reasons)
        self.assertIn(stale_bare, plan.diagnostics_text())

    def test_subset_export_reports_the_unwritten_skeleton_slots(self):
        """④ 子集导出的**已知后果**必须写进诊断：未覆盖的骨架槽没有 attach 写方。

        覆盖层（从 0 起 / 首尾相接 / ∑span == 槽数）只在「本次导出 = 已落盘全集」时可判；
        子集导出天然只写一部分槽位 ⇒ 不中止（t20 裁定），但也不能装作没这回事：
        引用那些槽的顶点会读到未写入矩阵。
        """
        info = self._workspace()
        members = [
            _runtime.part_from_json_payload(
                unique_str=f"LOD0.{bare}", json_payload=_component_json(self.workspace, bare)
            )
            for bare in self.FAMILY_A
        ]
        plan = _runtime.plan_from_metadata(
            info["metadata"],
            members,
            target_bares=list(self.FAMILY_A),
            skeleton_bytes=info["skeleton_bytes"],
            gate_oracle=self._oracle(
                self.FAMILY_A,
                {
                    "bbbb2222": {
                        "family": "bbbb2222",
                        "members": self.FAMILY_B,
                        "missing": [self.FAMILY_B[1]],
                    }
                },
                subset=True,
            ),
            landed_data_bares=set(self.FAMILY_A + self.FAMILY_B),
        )
        self.assertTrue(plan.ok, plan.message)
        self.assertEqual(
            plan.family_ranges,
            {"aaaa1111": {"base": 0, "slots": 4, "own_block": True}},
            "本族值集 = [0,4)（两个成员共用恒等 VGMap 0..3）",
        )
        self.assertEqual(
            plan.uncovered_slot_ranges(),
            [(4, SLOT_COUNT)],
            "未覆盖的槽位必须可由计划直接读出",
        )
        self.assertIn("没有 attach 写方", plan.diagnostics_text())
        self.assertIn("子集导出的骨架覆盖", plan.diagnostics_text())
        decision = _contract.evaluate_merged_skeleton_contract(
            **plan.contract_snapshot(checkbox_enabled=True)
        )
        self.assertEqual(decision["level"], "warning", "子集导出是合法路径，不得中止")
        self.assertIn("没有 attach 写方", decision["hint"], "后果必须进用户可见提示词")

    def test_full_export_has_no_unwritten_slots(self):
        """护栏：覆盖全集时不得出现「未覆盖槽位」诊断（否则上面那条会恒真）。"""
        info = self._workspace()
        parts = [
            _runtime.part_from_json_payload(
                unique_str=f"LOD0.{bare}", json_payload=_component_json(self.workspace, bare)
            )
            for bare in self.FAMILY_A + self.FAMILY_B
        ]
        plan = _runtime.plan_from_metadata(
            info["metadata"],
            parts,
            target_bares=list(self.FAMILY_A + self.FAMILY_B),
            skeleton_bytes=info["skeleton_bytes"],
            gate_oracle=self._oracle(self.FAMILY_A + self.FAMILY_B),
            landed_data_bares=set(self.FAMILY_A + self.FAMILY_B),
        )
        self.assertTrue(plan.ok, plan.message)
        self.assertEqual(plan.uncovered_slot_ranges(), [])
        self.assertNotIn("没有 attach 写方", plan.diagnostics_text())
        self.assertEqual(
            plan.family_ranges,
            {
                "aaaa1111": {"base": 0, "slots": 4, "own_block": True},
                "bbbb2222": {"base": 4, "slots": 4, "own_block": True},
            },
            "两个家族的区间必须首尾相接且铺满 8 槽",
        )

    def test_subset_coverage_check_message_when_oracle_unavailable(self):
        """dump 不可用 + 子集导出 ⇒ 仍然保守全部不重绑，且诊断说明原因与正确处置。"""
        self._workspace()
        plan = _runtime.load_workspace_plan(
            self.workspace,
            export_unique_strs=[f"LOD0.{self.FAMILY_A[0]}"],
            export_bares=[self.FAMILY_A[0]],
            enable_gate_oracle=False,
        )
        self.assertEqual(plan.rebinds, [])
        self.assertIn("只覆盖了部分已落盘部件", plan.diagnostics_text())
        decision = _contract.evaluate_merged_skeleton_contract(
            **plan.contract_snapshot(checkbox_enabled=True)
        )
        self.assertEqual(decision["level"], "error")
        self.assertIn(_contract.DEFORM_GATE_REJECT_PREFIX, decision["hint"])
        self.assertIn("IB 家族", decision["hint"])
        self.assertNotIn("清除骨骼合并VGMap缓存", decision["hint"])


class HIMIFamilyRangeTests(unittest.TestCase):
    """t51：attach 段按「族内本地槽 i → 全局槽 ``base+i``」搬**连续块** ⇒ 区间必须自洽。

    这一组是 ``compute_family_ranges`` 的 fail-closed 证据：五个判据（族内密集 /
    互不重叠 / 从 0 起 / 首尾相接 / ``∑span == 槽数``）各有一条**必须抛**
    ``HIMIFamilyRangeError`` 的反例，另有一条真实数据正向用例。
    """

    #: 真实工作空间「嗨♪爱愿妖精♥」实测的 5 段（343 槽；`29-t52-handoff.md` §4）。
    REAL_RANGES = {
        "2c706fbd": (0, 184),
        "694025c6": (184, 18),
        "70a4da3e": (202, 71),
        "b35cfc6c": (273, 19),
        "def0c0ae": (292, 51),
    }

    @staticmethod
    def _part(family, values, bare=""):
        return _runtime.HIMIRebindPart(
            bare=str(bare or f"{family}-1-0"),
            draw_ib=str(family),
            slot_values=tuple(int(value) for value in values),
        )

    def _real_parts(self):
        return [
            self._part(family, range(base, base + span), bare=f"{family}-1-0")
            for family, (base, span) in self.REAL_RANGES.items()
        ]

    # -- 正向：实测值逐段复现 ------------------------------------------------ #

    def test_real_measured_ranges_are_reproduced(self):
        ranges = _runtime.compute_family_ranges(self._real_parts(), 343)
        self.assertEqual(
            ranges,
            {
                family: {"base": base, "slots": span, "own_block": True}
                for family, (base, span) in self.REAL_RANGES.items()
            },
        )
        self.assertEqual(sum(int(info["slots"]) for info in ranges.values()), 343)
        self.assertEqual(_runtime.uncovered_slot_ranges(ranges, 343), [], "全量导出不得有空缺槽")
        self.assertEqual(
            _runtime.family_block_slots(ranges), set(range(343)), "五族块正好铺满 343 槽"
        )

    def test_dispatch_counts_match_the_measured_families(self):
        """Dispatch 由槽数算出（t51 §4.2 ②），实测 = 3/1/2/1/1。"""
        dispatches = {}
        for family, (base, span) in self.REAL_RANGES.items():
            lines = _runtime.render_attach_section_lines(
                family, base=base, slots=span, skeleton_name="ResourceHI3MergedSkeleton_role_x"
            )
            dispatches[family] = int(
                next(line for line in lines if line.startswith("Dispatch = ")).split()[2].rstrip(",")
            )
        self.assertEqual(
            dispatches,
            {
                "2c706fbd": 3,
                "694025c6": 1,
                "70a4da3e": 2,
                "b35cfc6c": 1,
                "def0c0ae": 1,
            },
        )

    # -- 反例：五条判据各一条 ------------------------------------------------ #

    # -- 借位族：注册槽全部落在别人的块里（真实角色 镇×偃月叩晓 实测形态） ------------- #

    def test_borrowed_family_needs_no_own_block(self):
        """家族 ``9430996d`` 形态：46 个注册槽全部落在 ``2c1f2636`` 的块 ``[0,148)`` 内。

        真实数据（``镇×偃月叩晓``，只读 K: 副本实测）：
        ``2c1f2636`` 注册槽 = ``[0,148)`` 密集；``557a7477`` = ``[148,156)``；
        ``efe5d0ce`` = ``[156,252)``；``9430996d`` 的 46 个槽**全在** ``2c1f2636`` 的块里
        （跨部件矩阵逐位去重把它收敛到同一批 canonical 槽）⇒ 它**不需要**自己的 attach 段，
        否则会与拥有者的段写同一批槽位。
        """
        parts = [
            self._part("2c1f2636", range(0, 148)),
            self._part("557a7477", range(148, 156)),
            self._part("efe5d0ce", range(156, 252)),
            # 借位族：46 个注册槽取 [78,148) 的一个子集（真实数据里就是这种带洞形状）
            self._part("9430996d", [slot for slot in range(78, 148) if slot % 3 == 0]),
        ]
        ranges = _runtime.compute_family_ranges(parts, 252)
        self.assertEqual(
            ranges,
            {
                "2c1f2636": {"base": 0, "slots": 148, "own_block": True},
                "557a7477": {"base": 148, "slots": 8, "own_block": True},
                "efe5d0ce": {"base": 156, "slots": 96, "own_block": True},
                "9430996d": {"base": 78, "slots": 0, "own_block": False},
            },
            "借位族必须被识别成「无自己的块」，而不是因为带洞被拒绝",
        )
        self.assertEqual(_runtime.uncovered_slot_ranges(ranges, 252), [])
        self.assertEqual(_runtime.family_block_slots(ranges), set(range(252)))
        # 非恒真：把借位族换成「有专属槽但带洞」⇒ 必须抛（区分布局错误与借位）
        broken = [
            self._part("2c1f2636", range(0, 148)),
            self._part("9430996d", list(range(78, 148, 3)) + [200]),
        ]
        with self.assertRaises(_runtime.HIMIFamilyRangeError) as ctx:
            _runtime.compute_family_ranges(broken, 252)
        self.assertIn("不密集", str(ctx.exception))

    def test_family_hole_is_rejected(self):
        """族内不密集：连续块搬运会把后面的槽写到错位置。"""
        with self.assertRaises(_runtime.HIMIFamilyRangeError) as ctx:
            _runtime.compute_family_ranges([self._part("aaaa1111", [0, 1, 4])], 8)
        self.assertIn("不密集", str(ctx.exception))

    def test_overlapping_families_are_rejected(self):
        """两族区间相叠：两个 attach 段写同一批槽位（后写的赢 ⇒ 姿态错）。"""
        with self.assertRaises(_runtime.HIMIFamilyRangeError) as ctx:
            _runtime.compute_family_ranges(
                [self._part("aaaa1111", [0, 1, 2, 3]), self._part("bbbb2222", [3, 4, 5, 6])], 8
            )
        self.assertIn("重叠", str(ctx.exception))

    def test_gap_between_families_is_rejected(self):
        """首尾不相接（洞）：中间的骨架槽没有写方 ⇒ deform 读零。"""
        with self.assertRaises(_runtime.HIMIFamilyRangeError) as ctx:
            _runtime.compute_family_ranges(
                [self._part("aaaa1111", [0, 1]), self._part("bbbb2222", [4, 5])], 8
            )
        self.assertIn("不接续", str(ctx.exception))

    def test_not_starting_at_zero_is_rejected(self):
        with self.assertRaises(_runtime.HIMIFamilyRangeError) as ctx:
            _runtime.compute_family_ranges([self._part("aaaa1111", [2, 3, 4, 5, 6, 7])], 8)
        self.assertIn("没有写方", str(ctx.exception))

    def test_total_span_mismatch_is_rejected(self):
        """``∑span != slot_count``：骨架尾部没有写方。"""
        with self.assertRaises(_runtime.HIMIFamilyRangeError) as ctx:
            _runtime.compute_family_ranges([self._part("aaaa1111", [0, 1, 2, 3])], 8)
        self.assertIn("∑span", str(ctx.exception))

    def test_out_of_bounds_is_rejected(self):
        """越出骨架槽位：attach CS 会写到骨架外面（``base+slots > slot_count``）。"""
        with self.assertRaises(_runtime.HIMIFamilyRangeError) as ctx:
            _runtime.compute_family_ranges([self._part("aaaa1111", [6, 7, 8, 9])], 8)
        self.assertIn("越出骨架槽位", str(ctx.exception))
        # 子集模式下同样抛（布局层与覆盖层分离，越界不属于「覆盖」）
        with self.assertRaises(_runtime.HIMIFamilyRangeError):
            _runtime.compute_family_ranges([self._part("aaaa1111", [6, 7, 8, 9])], 8, complete=False)

    def test_no_slot_values_is_rejected(self):
        with self.assertRaises(_runtime.HIMIFamilyRangeError):
            _runtime.compute_family_ranges([], 8)
        with self.assertRaises(_runtime.HIMIFamilyRangeError):
            _runtime.compute_family_ranges([self._part("aaaa1111", [])], 8)

    # -- 子集语义：complete=False 只放松覆盖层，不放松布局层 ------------------ #

    def test_subset_mode_tolerates_a_partial_cover(self):
        """子集导出：区间可以停在骨架中间（覆盖层不适用），但布局层仍然 fail-closed。"""
        ranges = _runtime.compute_family_ranges(
            [self._part("2c706fbd", range(0, 184))], 343, complete=False
        )
        self.assertEqual(
            ranges, {"2c706fbd": {"base": 0, "slots": 184, "own_block": True}}
        )
        self.assertEqual(_runtime.uncovered_slot_ranges(ranges, 343), [(184, 343)])
        # 布局层（重叠/越界/族内洞）在子集模式下**照样**抛
        for broken in (
            [self._part("aaaa1111", [0, 1]), self._part("bbbb2222", [1, 2])],
            [self._part("aaaa1111", [8, 9])],
            [self._part("aaaa1111", [0, 2])],
        ):
            with self.assertRaises(_runtime.HIMIFamilyRangeError):
                _runtime.compute_family_ranges(broken, 8, complete=False)


class HIMIFamilyRangePlanTests(HIMIMergedRuntimeBase):
    """族区间接进计划（``plan.family_ranges``）与**大声失败**（不静默退化）的两态。"""

    def _metadata(self, slots=SLOT_COUNT):
        payload = _skeleton_bytes(slots)
        return {
            "AlgorithmVersion": 1,
            "GateVersion": 1,
            "RoleKey": "role_ab12",
            "SlotCount": slots,
            "SkeletonFileName": "role_ab12-MergedSkeleton.buf",
            "SkeletonDigest": hashlib.sha256(payload).hexdigest(),
        }, payload

    def test_plan_exposes_the_family_ranges(self):
        metadata, payload = self._metadata()
        parts = [
            _runtime.part_from_json_payload(
                unique_str="LOD0.aaaa1111-1-0",
                json_payload={
                    "HIMIDeformFamilyIB": "aaaa1111",
                    "VGMap": {"0": 4, "1": 5, "2": 6, "3": 7},
                    "VGCount": 4,
                    "OriginalVertexCount": 100,
                    "CategoryHash": {"Blend": "8322ac5a"},
                },
            ),
            _runtime.part_from_json_payload(
                unique_str="LOD0.aaaa1111-2-0",
                json_payload={
                    "HIMIDeformFamilyIB": "aaaa1111",
                    "VGMap": {"0": 0, "1": 1, "2": 2, "3": 3},
                    "VGCount": 4,
                    "OriginalVertexCount": 100,
                    "CategoryHash": {"Blend": "8322ac5a"},
                },
            ),
        ]
        self.assertEqual(parts[0].slot_values, (4, 5, 6, 7), "slot_values 必须来自 json VGMap 的值集")
        members = [part.bare for part in parts]
        plan = _runtime.plan_from_metadata(
            metadata,
            parts,
            skeleton_bytes=payload,
            target_bares=members,
            gate_oracle={
                "available": True,
                "stale": False,
                "pass_level": True,
                "source": "prepare",
                "safe_bares": members,
                "rejected": {},
                "message": "",
                "slot_count": SLOT_COUNT,
                "digest": "",
                "cache_intact": True,
            },
        )
        self.assertTrue(plan.ok, plan.message)
        self.assertEqual(
            plan.family_ranges,
            {"aaaa1111": {"base": 0, "slots": SLOT_COUNT, "own_block": True}},
        )
        self.assertEqual(plan.uncovered_slot_ranges(), [])
        self.assertEqual(
            plan.to_dict()["FamilyRanges"],
            {"aaaa1111": {"base": 0, "slots": SLOT_COUNT, "own_block": True}},
        )

    def test_slot_values_prefer_the_registered_set_over_gap_slots(self):
        """``BoneMatrixSlotCount`` ⇒ 只取**最小的 N 个**值（= 注册槽；gap 值恒大于全部注册值）。

        这是真实数据逼出来的口径：``镇×偃月叩晓`` 的 ``2c1f2636`` 成员 VGMap 全值集
        distinct=156 span=401，注册槽 = ``[0,148)``；不排除 gap slots 会让 attach 块跨到
        别的族的区间（互相覆盖）或被 fail-closed 拒绝。
        """
        payload = {
            "HIMIDeformFamilyIB": "9430996d",
            "VGCount": 6,
            "VGMap": {"0": 0, "1": 1, "2": 2, "3": 3, "4": 40, "5": 41},
            "BoneMatrixSlotCount": 4,
            "OriginalVertexCount": 10,
            "CategoryHash": {"Blend": "8322ac5a"},
        }
        part = _runtime.part_from_json_payload(
            unique_str="LOD0.9430996d-1656-0", json_payload=payload
        )
        self.assertEqual(part.slot_values, (0, 1, 2, 3), "gap slots（40/41）必须被排除")
        # 非恒真：去掉该字段 ⇒ 退回全量值（区间更大，由 fail-closed 判据兜住）
        loose = _runtime.part_from_json_payload(
            unique_str="LOD0.9430996d-1656-0",
            json_payload={k: v for k, v in payload.items() if k != "BoneMatrixSlotCount"},
        )
        self.assertEqual(loose.slot_values, (0, 1, 2, 3, 40, 41))
        # 最高精度档：显式给出被引用局部槽 ⇒ 原样采用（优先级最高，不再看数量字段）
        exact = _runtime.part_from_json_payload(
            unique_str="LOD0.9430996d-1656-0",
            json_payload=payload,
            referenced_locals=(4, 5),
        )
        self.assertEqual(exact.slot_values, (40, 41))

    def test_inconsistent_layout_raises_and_is_never_silently_tolerated(self):
        """布局不自洽（族内洞）⇒ ``HIMIFamilyRangeError`` **必须抛出去**，不得静默容忍。

        口径边界（照实写，不假装比现状更强）：抛出的异常由导出器
        （``ui/universal/himi.py`` 的兜底 except）接住 ⇒ 打印可读原因 + 本次退化成
        「没有运行时计划」。**已知缺口**：那条兜底路径最后是**普通导出**，而 local 几何
        用的是全局骨骼编号 ⇒ 没有骨架可读的产物在游戏里会整块不显示；把这一步升级成
        「有理由的 Fatal」必须与 ``tests/test_himi_stub_objects.py`` 的 3 个合成夹具
        （它们的工作空间自身不自洽）同批做，属 t54/t55。本用例钉住的是：**fail-closed
        的判据本体真的生效**（不是「不报错就行」）。
        """
        metadata, payload = self._metadata()
        members = ["aaaa1111-1-0", "aaaa1111-2-0"]
        parts = [
            _runtime.HIMIRebindPart(
                bare=members[0], draw_ib="aaaa1111", vg_count=4, original_vertex_count=100,
                family_members=tuple(members), category_hash="8322ac5a",
                slot_values=(0, 1, 2, 3),
            ),
            _runtime.HIMIRebindPart(
                bare=members[1], draw_ib="aaaa1111", vg_count=4, original_vertex_count=100,
                family_members=tuple(members), category_hash="8322ac5a",
                slot_values=(5, 6, 7),  # 族内洞：4 缺失
            ),
        ]
        with self.assertRaises(_runtime.HIMIFamilyRangeError) as ctx:
            _runtime.plan_from_metadata(
                metadata,
                parts,
                skeleton_bytes=payload,
                target_bares=members,
                gate_oracle={
                    "available": True, "stale": False, "pass_level": True, "source": "prepare",
                    "safe_bares": members, "rejected": {}, "message": "",
                    "slot_count": SLOT_COUNT, "digest": "", "cache_intact": True,
                },
            )
        self.assertIn("不密集", str(ctx.exception))
        self.assertIn("aaaa1111", str(ctx.exception))


class HIMIAttachResourceTests(unittest.TestCase):
    """t51 §4.2 的 attach 段与家族副本形态（B-1 方向 / B-2 顺序）。"""

    def test_capture_line_lhs_is_the_target_and_order_is_capture_then_run(self):
        """3DMigoto 的 copy 是**赋值式**：``LHS`` 是目标、``copy vs-t0`` 是源。

        写反（``copy vs-t0 = ResourceHI3PaletteFam_…``）会把**空副本**写进游戏当帧
        palette —— 比不改更坏。顺序同样固定：捕获 → ``run``（``run`` 必须排在
        ``vs-t0 = 骨架`` 之前，否则捕到的是刚绑上去的骨架 = 自反馈）。
        """
        family = "2c706fbd"
        lines = _runtime.render_attach_capture_lines(family)
        self.assertEqual(
            lines[0],
            "ResourceHI3PaletteFam_2c706fbd = copy vs-t0 unless_null",
            "捕获行的 LHS 必须是**目标**（家族副本）",
        )
        self.assertFalse(lines[0].startswith("copy vs-t0"), "旧契约的写反方向必须被拒绝")
        # 【t74】捕获行现在是两行（palette 的 vs-t0 + 本族物体矩阵的 vs-cb3），run 仍在最后且紧邻。
        self.assertEqual(len(lines), 3)
        self.assertEqual(
            lines[1], "ResourceHI3Cb3_2c706fbd = copy vs-cb3 unless_null"
        )
        self.assertEqual(lines[2], "run = CustomShaderHI3MergedSkeletonAttach_2c706fbd")
        self.assertIn("copy vs-t0", lines[0])
        self.assertLess(
            lines.index(next(line for line in lines if "copy vs-t0" in line)),
            lines.index(next(line for line in lines if line.startswith("run = "))),
            "顺序必须是 ①捕获 → ②run",
        )
        self.assertLess(
            lines.index(next(line for line in lines if "copy vs-cb3" in line)),
            lines.index(next(line for line in lines if line.startswith("run = "))),
            "cb3 副本必须在 run 之前（run 里才有 cs-cb1 = ref）",
        )

    def test_family_palette_decl_is_the_palette_shape(self):
        """家族副本 = palette 形态：``Buffer`` + ``stride = 48`` + **绝不允许 format**。

        （加 format 会把 StructureByteStride 改成 16、ByteWidth 缩到 ``16×vg_count``，
        而 ``copy vs-t0`` 要拷 ``48×vg_count`` 字节 ⇒ 溢出。）
        """
        decl = _runtime.build_family_palette_decl("2c706fbd", 184)
        self.assertEqual(decl.name, "ResourceHI3PaletteFam_2c706fbd")
        fields = decl.ini_fields()
        self.assertEqual(fields["type"], "Buffer")
        self.assertEqual(fields["stride"], 48)
        self.assertEqual(fields["array"], 184)
        self.assertNotIn("format", fields)
        self.assertNotIn("bind_flags", fields)
        self.assertEqual(decl.byte_width, 184 * 48)

    def test_attach_section_matches_the_mod_reference_shape(self):
        """段体形状以 **2026-09-25 模组侧手工参考实现**为准
        （``reports/himi-merge/30-verify-mod-perframe.py`` D 组逐条同款）。

        关键：**不读 IniParams**（不要 ``x1``/``y1``/``z1``）—— 基址写死在该族的 CS 文件里
        （``#define HIMI_ATTACH_BASE``），槽数由 CS 用 palette 资源自身维度 ``/3u`` 还原；
        每族一个 CS 文件，``cs = ./res/himi_attach_<fam>.hlsl``。

        【t4/F5 变更（唯一一处既有断言被改写，理由见 reports/himi-merge/51-fix-implementation.md）】
        段体**多一行** ``cs-t1 = ref ResourceHI3AttachRange_<fam>``（= 本族身份资源：
        ``(base, span, version, magic)`` + ``Q_f⁻¹`` + ``X_f``（128 B），由导出侧按 ``plan.family_ranges`` 生成）。
        旧断言写的是「HIMI 不需要 cs-t1（那要一份不存在的资源）」—— 现在那份资源**确实存在**
        且是 F5 的唯一实质修法：只有它能让「本族 span 上界」独立于查表键（否则恒真），
        副本维度大于本族 span 时就会写进下一族的块（报告 50 §1.2 入口 ①）。

        【t39-X/t74 口径】段体又多一行 ``cs-cb1 = ref ResourceHI3Cb3_<fam>``` ——
        该族六根物体矩阵的运行期来源（``cbuffer … : register(b1)``）。段体现在 **9 行**：
        ``[段名, flags, cs, cs-t0, cs-t1, cs-cb1, cs-u0, Dispatch, cs-u0 = null]``。
        传了 ``object_matrix_name`` 时再多 2 行（``cs-u1 = ref …`` 与段末 ``cs-u1 = null``）
        ⇒ **11 行**（导出侧总是传，见 ui/universal/himi.py:3610-3616；本用例不传）。
        """
        lines = _runtime.render_attach_section_lines(
            "2c706fbd",
            base=0,
            slots=184,
            skeleton_name="ResourceHI3MergedSkeleton_role_x",
        )
        self.assertEqual(
            len(lines),
            9,
            "段体 9 行：[段名, flags, cs, cs-t0, cs-t1, cs-cb1, cs-u0, Dispatch, cs-u0 = null]",
        )
        self.assertIn("cs-cb1 = ref ResourceHI3Cb3_2c706fbd", lines, "cb1 = 该族物体矩阵来源")
        self.assertEqual(lines[0], "[CustomShaderHI3MergedSkeletonAttach_2c706fbd]")
        self.assertIn("flags = optimization_level3 all_resources_bound skip_validation", lines)
        self.assertEqual(
            [line for line in lines if line.startswith("cs = ")],
            ["cs = ./res/himi_attach_2c706fbd.hlsl"],
            "每族一个 CS 文件；写法与仓库其它导出器同款（./res/…，mod 根相对）",
        )
        self.assertIn("cs-t0 = ref ResourceHI3PaletteFam_2c706fbd", lines, "t0 = 本族当帧副本")
        self.assertIn(
            "cs-t1 = ref ResourceHI3AttachRange_2c706fbd", lines, "t1 = 本族身份（F5，base/span 权威）"
        )
        self.assertIn("cs-u0 = ref ResourceHI3MergedSkeleton_role_x", lines)
        self.assertIn("Dispatch = 3, 1, 1", lines, "Dispatch = ceil(184/64)")
        self.assertEqual(lines[-1], "cs-u0 = null", "段尾必须解绑 u0（不留悬挂引用）")
        # 参考实现逐字判据：不得用 ini 参数纹理传参（fork 相关 ⇒ 读错位置静默不写）
        joined = "\n".join(lines)
        for forbidden in ("x1", "y1", "z1", "w1"):
            self.assertNotIn(f"{forbidden} =", joined, f"attach 段不得出现 {forbidden}（不读 IniParams）")
        self.assertNotIn("IniParams", joined)
        # HIMI 不需要 VgMap 重映射表（区间已同序）；也不得引入第三个输入槽
        self.assertNotIn("cs-t2", joined)

    def test_family_attach_hlsl_interface_is_pinned(self):
        """基址宏是导出侧与 CS 之间**唯一**的逻辑参数接口（槽数走资源维度）。"""
        self.assertEqual(_runtime.family_attach_hlsl_basename("70a4da3e"), "himi_attach_70a4da3e.hlsl")
        self.assertEqual(_runtime.family_attach_hlsl_path("70a4da3e"), "./res/himi_attach_70a4da3e.hlsl")
        self.assertEqual(_runtime.FAMILY_ATTACH_BASE_DEFINE, "HIMI_ATTACH_BASE")
        self.assertEqual(_runtime.render_family_attach_base_line(202), "#define HIMI_ATTACH_BASE 202")
        with self.assertRaises(_runtime.HIMIResourceDeclError):
            _runtime.render_family_attach_base_line(-1)
        # 五族实测基址 → 宏行（与 30-verify-mod-perframe.py 的 D 组一一对应）
        self.assertEqual(
            {
                family: _runtime.render_family_attach_base_line(base)
                for family, (base, _span) in HIMIFamilyRangeTests.REAL_RANGES.items()
            },
            {
                "2c706fbd": "#define HIMI_ATTACH_BASE 0",
                "694025c6": "#define HIMI_ATTACH_BASE 184",
                "70a4da3e": "#define HIMI_ATTACH_BASE 202",
                "b35cfc6c": "#define HIMI_ATTACH_BASE 273",
                "def0c0ae": "#define HIMI_ATTACH_BASE 292",
            },
        )

    def test_attach_section_rejects_a_degenerate_slots_count(self):
        for slots in (0, -1):
            with self.assertRaises(_runtime.HIMIResourceDeclError):
                _runtime.render_attach_section_lines(
                    "f", base=0, slots=slots, skeleton_name="ResourceHI3MergedSkeleton_r"
                )


class HIMIRealPartialExportTests(unittest.TestCase):
    """用户实报场景的真实数据回归（**只读 K:**）：按家族局部导出不再被误判中止。

    真实工作空间：``嗨♪爱愿妖精♥``（12 个已落盘部件，343 槽，其中 ``2c706fbd`` 家族 5 个成员）。
    """

    WS = r"K:\SSMT-Package-master\WorkSpace\HIMI\嗨♪爱愿妖精♥"

    def _landed_bares(self):
        if not os.path.isdir(self.WS):
            self.skipTest(f"真实工作空间不存在：{self.WS}")
        bares = []
        lod_root = os.path.join(self.WS, "LOD0")
        for name in sorted(os.listdir(lod_root)):
            bare_dir = os.path.join(lod_root, name)
            if not os.path.isdir(bare_dir):
                continue
            for gametype in sorted(os.listdir(bare_dir)):
                json_path = os.path.join(bare_dir, gametype, f"{name}.json")
                if not os.path.isfile(json_path):
                    continue
                try:
                    with io.open(json_path, encoding="utf-8") as handle:
                        payload = json.load(handle)
                except Exception:  # noqa: BLE001
                    continue
                if "CategoryBufferList" in payload and int(payload.get("VGCount") or 0) > 0:
                    bares.append(name)
                break
        if len(bares) < 3:
            self.skipTest(f"该工作空间没有已落盘的合并数据（{len(bares)} 个部件）")
        return sorted(bares)

    def _plan_for(self, bares):
        return _runtime.load_workspace_plan(
            self.WS,
            export_unique_strs=[f"LOD0.{bare}" for bare in bares],
            export_bares=list(bares),
        )

    def _decision(self, plan):
        return _contract.evaluate_merged_skeleton_contract(
            **plan.contract_snapshot(checkbox_enabled=True)
        )

    def test_real_partial_family_export_rebinds_the_family_and_is_warning(self):
        bares = self._landed_bares()
        families = {}
        for bare in bares:
            families.setdefault(bare.split("-")[0], []).append(bare)
        family, members = max(families.items(), key=lambda item: len(item[1]))
        if len(members) < 2:
            self.skipTest("没有多成员家族可用于本回归")

        plan = self._plan_for(members)
        self.assertEqual(plan.gate_source, "prepare", plan.diagnostics_text())
        self.assertEqual(
            sorted(item["bare"] for item in plan.rebinds),
            sorted(members),
            "覆盖完整的家族必须照常重绑（这正是被 stale 误判打断的路径）",
        )
        self.assertEqual(plan.parts_with_data, len(members))
        decision = self._decision(plan)
        self.assertEqual(
            decision["level"], "warning",
            f"按家族局部导出不得中止导出：{decision['message']} / {decision['hint']}",
        )
        self.assertIn("IB 家族", decision["hint"])
        self.assertNotIn("清除骨骼合并VGMap缓存", decision["hint"])
        print(
            "HIMI-T20-PARTIAL",
            {
                "family": family,
                "members": len(members),
                "landed": len(bares),
                "rebinds": len(plan.rebinds),
                "rejected": sorted(plan.rejected),
                "level": decision["level"],
            },
        )

    def test_real_single_member_family_export_rebinds_it(self):
        """单成员家族单独导出 = 家族**完整** ⇒ 照常重绑（这是合法路径，不是回归）。

        对应 T14 oracle 里那条「只导出 1 个组件 ⇒ 其家族不完整 ⇒ 不重绑」的旧断言：
        它的 fixture（``af17c02f-11772-0``）其实是 1 成员家族，G1 本就该放行；
        旧代码之所以 rebinds=[]，是因为 stale 误判（本任务修掉的 bug）把它拦下了。
        真正「家族不完整」的反例由 ``test_real_partial_family_export_*`` 的多成员家族覆盖。
        """
        bares = self._landed_bares()
        families = {}
        for bare in bares:
            families.setdefault(bare.split("-")[0], []).append(bare)
        singles = [members[0] for members in families.values() if len(members) == 1]
        if not singles:
            self.skipTest("该工作空间没有单成员家族")
        plan = self._plan_for([singles[0]])
        self.assertEqual(plan.gate_source, "prepare", plan.diagnostics_text())
        self.assertEqual(
            [item["bare"] for item in plan.rebinds], [singles[0]],
            "单成员家族单独导出时家族是完整的 ⇒ G1 应放行",
        )
        decision = self._decision(plan)
        self.assertEqual(decision["level"], "warning", decision)
        self.assertNotEqual(decision["level"], "error", "不得因为『只导出一个』就中止")

    def test_real_single_part_export_is_error_with_reason_and_correct_advice(self):
        bares = self._landed_bares()
        family = bares[0].split("-")[0]
        if len([b for b in bares if b.startswith(family)]) < 2:
            self.skipTest("首个家族不是多成员家族")
        plan = self._plan_for([bares[0]])
        self.assertEqual(plan.rebinds, [], "家族不完整时不得重绑（G1 硬规则）")
        decision = self._decision(plan)
        self.assertEqual(decision["level"], "error")
        hint = decision["hint"]
        self.assertIn(_contract.DEFORM_GATE_REJECT_PREFIX, hint, "必须给出理由")
        self.assertIn("IB 家族", hint, "必须给出 G1 的正确处置")
        self.assertIn("一起导出", hint)
        self.assertNotIn(
            "清除骨骼合并VGMap缓存",
            hint,
            "不得再建议清除完全正常的合并数据",
        )


class HIMIRealDumpEndToEndTests(unittest.TestCase):
    """真实工作空间 + 真实 dump 的端到端（**K: 只读**；缓存写进临时副本）。

    走完整链路：数据层 ``ensure_skeleton_data`` 落盘 → 本模块的 ``load_workspace_plan``
    （含 ``prepare`` 权威门控）→ 资源声明 + 骨架发布。槽位数必须落在 T4 实测集合内。
    """

    HIMI_ROOT = r"K:\SSMT-Package-master\WorkSpace\HIMI"
    #: T4 实测各角色全局唯一骨骼数（``04-probe-results.md`` §4）
    MEASURED_SLOT_COUNTS = {276, 343, 315, 219, 252}

    def test_real_role_end_to_end(self):
        if not os.path.isdir(self.HIMI_ROOT):
            self.skipTest(f"真实 HIMI 工作空间不存在：{self.HIMI_ROOT}")
        roles = sorted(
            name for name in os.listdir(self.HIMI_ROOT)
            if os.path.isdir(os.path.join(self.HIMI_ROOT, name))
        )
        if not roles:
            self.skipTest("真实 HIMI 工作空间为空")
        # 组件最多、且 dump 仍登记在册的那一个角色
        sources = []
        for role in roles:
            frame_path = os.path.join(self.HIMI_ROOT, role, "Config", "FrameAnalysisPath.json")
            if os.path.isfile(frame_path):
                sources.append(role)
        if not sources:
            self.skipTest("没有任何角色登记了 FrameAnalysis")

        tmp_root = tempfile.mkdtemp(prefix="himi_real_e2e_")
        self.addCleanup(shutil.rmtree, tmp_root, ignore_errors=True)
        role = sources[0]
        target = os.path.join(tmp_root, role)
        # 只复制数据层需要的部分（跳过 DedupedTextures / .blend，避免拷贝几百 MB）
        shutil.copytree(
            os.path.join(self.HIMI_ROOT, role),
            target,
            ignore=shutil.ignore_patterns("DedupedTextures", "*.blend", "*.blend1", "*.dds"),
        )

        ok, message = _skeleton.HIMISkeletonMergeHelper.ensure_skeleton_data(target)
        self.assertTrue(ok, f"数据层落盘失败：{message}")

        plan = _runtime.load_workspace_plan(target)
        self.assertTrue(plan.ok, plan.message)
        self.assertEqual(
            plan.gate_source, "prepare",
            "dump 仍在册时必须用数据层的 pass 级门控作为权威",
        )
        self.assertIn(
            plan.slot_count, self.MEASURED_SLOT_COUNTS,
            f"角色 {role} 的槽位数 {plan.slot_count} 不在 T4 实测集合内",
        )
        self.assertGreater(len(plan.rebinds), 0)
        # ITEM 4 的实证：部件 palette 缓存（按引用序压缩）与角色级骨架**逐字节同构**，
        # 且顺序契约在真实数据上成立（否则这里会是 mismatch）。
        for bare, check in plan.palette_cache.items():
            self.assertEqual(check["verdict"], "verified", f"{bare}: {check}")
            self.assertGreater(check["checked"], 0)
        decl = plan.resource()
        self.assertEqual(decl.array, plan.slot_count * 3)
        self.assertEqual(decl.bind_flags, "shader_resource unordered_access")
        for rebind in plan.rebinds:
            self.assertEqual(
                rebind["vs_t0_resource"], f"ResourceHI3MergedSkeleton_{plan.role_key}"
            )
            self.assertGreaterEqual(rebind["family_deform_vertex_count"], 1)

        published = _runtime.publish_skeleton_binary(plan, os.path.join(tmp_root, "Meshes"))
        self.assertTrue(published["ok"], published)
        self.assertEqual(
            os.path.getsize(published["path"]), plan.slot_count * 48
        )
        print(
            "HIMI-E2E",
            {
                "role": role,
                "role_key": plan.role_key,
                "slots": plan.slot_count,
                "gate_source": plan.gate_source,
                "rebinds": len(plan.rebinds),
                "rejected_passes": len(plan.rejected),
                "parts_with_data": plan.parts_with_data,
                "skeleton_bytes": os.path.getsize(published["path"]),
                "resource": plan.resource().name,
            },
        )


# --------------------------------------------------------------------------- #
# F. 站位（stub）与 G1 的交互（T22 / 计划书 §7.7.6）
# --------------------------------------------------------------------------- #


class HIMIMergedAttachCsTests(unittest.TestCase):
    """【t75】**单个**合并 attach CS 落盘 + `cs =` 引用硬门。

    用户口径：**「这个着色器需要合并成一个」** ⇒
      * `<mod>/res/` 下只有**一个** `himi_attach.hlsl`，5 个族段的 `cs =` **指向同一路径**；
      * 基址不再逐族写 `#define`，由该 shader 用**源 palette 的槽位数**在运行期查表算出来；
      * 槽数一律走 `GetDimensions`（运行期真值），**绝不读 `IniParams`**（fork 相关 ⇒ 静默不写）；
      * **硬门**：ini 里每条 `cs =` 都必须解析到真实文件 ⇒ 否则 `Fatal` 中止导出（两态证明）。
    """

    #: 五族实测（槽位数 → 合并骨架里的起始槽），来自工作空间 `role_0512e26f` 的 343 槽排布
    REAL_BASE_TABLE = (
        (184, 0, "2c706fbd"),
        (18, 184, "694025c6"),
        (71, 202, "70a4da3e"),
        (19, 273, "b35cfc6c"),
        (51, 292, "def0c0ae"),
    )

    # ---------------------------------------------------------------- 单 shader 文本

    def test_merged_attach_hlsl_derives_the_base_from_the_source_slot_count(self):
        """基址必须由**源槽位数**查表得出，且表覆盖五族 —— 这是「一个 shader」的前提。"""
        text = _himi_exporter.merged_attach_hlsl_text()
        code = _himi_exporter._strip_hlsl_comments(text)
        problems = _himi_exporter.merged_attach_hlsl_problems(text)
        self.assertEqual(problems, [], f"合并 attach CS 自检必须通过：{problems}")
        # 五族各一行，且 (槽位数 → 基址) 与实测一致
        for slots, base, fam in self.REAL_BASE_TABLE:
            self.assertIn(
                f"source_slots == {slots}u) {{ base_slot = {base}u; }}",
                code,
                f"{fam}: 槽位数 {slots} 必须映射到基址 {base}",
            )
        # 无逐族基址宏、无 ini 参数、无结构化声明
        self.assertNotIn("HIMI_ATTACH_BASE", code, "合并成单文件后不得再有逐族基址宏")
        self.assertNotIn("IniParams", code, "绝不读 IniParams（fork 相关 ⇒ 静默不写）")
        self.assertNotIn("StructuredBuffer", code, "typed 16 B 与结构化 48 B 混用会落未定义区")
        # 两个 GetDimensions 都按单参 + /3u 还原槽数
        self.assertIn("himi_src_palette.GetDimensions(palette_elements);", code)
        self.assertIn("skeleton_elements / 3u", code)
        # 未知族必须**拒写**（不得冒用别人的槽位）
        self.assertIn("base_slot = 0xFFFFFFFFu;", code)
        self.assertIn("if (base_slot == 0xFFFFFFFFu)", code)

    def test_merged_attach_borrow_block_uses_export_time_rows_and_live_palettes(self):
        """借用块：行数只信导出期常量、X_源 每帧从**两支活 palette** 现算（t91 修）。

        实机证伪的两条旧写法都必须被判据钉死：
        1. ``himi_borrow_map.GetDimensions()`` —— typed 视图 1 分量 + HLSL 元素 4 分量落进跨显卡
           未定义区，实测 216 行只回 60 行 ⇒ 表里后 156 行永远不执行；
        2. 读 ``himi_object_matrix`` 合成 X_源 —— 那张表的来源在真机上不是物体矩阵（恒零）⇒
           X 恒退回载荷 = 宿主族单位阵 ⇒ 借用槽写成裸 P_源。
        """
        sample = _himi_exporter.MERGED_ATTACH_BASE_TABLE_SAMPLE_RANGES
        text = _himi_exporter.merged_attach_hlsl_text()
        code = _himi_exporter._strip_hlsl_comments(text)
        families = _runtime.borrow_palette_families(sample)
        rank_count = max(1, len(families))
        # 行数 = 本次 ranges 里**借用行最多的那一族**（参考样例没有借用 ⇒ 下限 1，空表哨兵）
        sample_rows = max(
            [len(rows) for rows in _runtime.borrow_map_rows_by_family(sample).values()] or [0]
        )
        self.assertIn(
            f"#define HIMI_BORROW_ROWS {max(1, sample_rows)}u",
            code,
            "借用表行数必须由导出期按本次 ranges 算出（不信运行期 GetDimensions）",
        )
        self.assertIn(f"#define HIMI_BORROW_RANK_COUNT {rank_count}u", code)
        self.assertIn("uint borrow_elements = HIMI_BORROW_ROWS;", code)
        self.assertNotIn(
            "himi_borrow_map.GetDimensions", code, "行数必须来自导出期常量，不许退回运行期读维度"
        )
        # 骨对 = 只有索引的两张表，缺省全是哨兵（没有解出的骨对 ⇒ 该槽退回单位阵）
        for name in ("HIMI_BORROW_PAIR_OWN", "HIMI_BORROW_PAIR_SRC"):
            self.assertIn(f"static const uint {name}[{rank_count * rank_count}] = {{", code)
            self.assertIn(f"{name}[borrow_pair_index]", code)
        self.assertIn(
            "himi_affine_mul(borrow_m_own,",
            code,
            "X_源 必须 = P_本族[p]·P_来源[q]⁻¹（own 在前）",
        )
        self.assertEqual(
            code.count("himi_affine_mul(borrow_m_own, himi_affine_inverse(borrow_m_src))"),
            1,
            "X_源 每帧现算恰好一次",
        )
        self.assertNotIn("himi_object_matrix[borrow_base_row", code, "不许再读恒零的物体矩阵表")
        # 结构门：借用目标必然落在本族块尾，读到 0 / 被截断的行必须跳过
        self.assertIn("borrow_entry.x < base_slot + span_slots", code)
        self.assertLess(
            code.index("borrow_entry.x == 0xFFFFFFFFu"),
            code.index("borrow_entry.x < base_slot + span_slots"),
            "哨兵判断必须先于结构门（哨兵行的 x 是 0xFFFFFFFF，落在块尾判断之外）",
        )
        self.assertLess(
            code.index("borrow_entry.x < base_slot + span_slots"),
            code.index("uint borrow_rank = borrow_entry.y;"),
            "结构门必须先于读 rank",
        )
        # 有借用行时行数由 ranges 现算（这里 1 行），显式给出 borrow_rows 时以它为准
        borrowed = {
            "famA": {
                "base": 0,
                "slots": 10,
                "block_slots": 11,
                "own_block": True,
                "borrow": ({"target_slot": 10, "source_family": "famB", "source_local": 3},),
            },
            "famB": {"base": 11, "slots": 8, "block_slots": 8, "own_block": True},
        }
        self.assertEqual(_runtime.borrow_palette_families(borrowed), ["famA", "famB"])
        self.assertEqual(
            _runtime.borrow_map_rows_by_family(borrowed), {"famA": [(10, 1, 3)], "famB": []}
        )
        borrowed_code = _himi_exporter._strip_hlsl_comments(
            _himi_exporter.merged_attach_hlsl_text(ranges=borrowed)
        )
        self.assertIn("#define HIMI_BORROW_ROWS 1u", borrowed_code, "行数 = ranges 现算")
        self.assertIn("#define HIMI_BORROW_RANK_COUNT 2u", borrowed_code)
        self.assertIn(
            "#define HIMI_BORROW_ROWS 216u",
            _himi_exporter._strip_hlsl_comments(
                _himi_exporter.merged_attach_hlsl_text(ranges=borrowed, borrow_rows=216)
            ),
            "导出侧显式给出的行数必须优先（实机借用表 216 行）",
        )
        # 骨对表真被填进去时能逐字出现在文本里（索引表，不是矩阵常量）
        filled = _himi_exporter.merged_attach_hlsl_text(
            borrow_pairs={(0, 1): (7, 11), (2, 0): (5, 3)}
        )
        filled_code = _himi_exporter._strip_hlsl_comments(filled)
        own_table = filled_code.split("HIMI_BORROW_PAIR_OWN[", 1)[1].split("};", 1)[0]
        self.assertIn("7u", own_table, "本族槽必须按 (本族序号, rank) 落进索引表")
        self.assertIn("5u", own_table)
        self.assertEqual(_himi_exporter.merged_attach_borrow_problems(filled), [])

    def test_merged_attach_borrow_predicate_rejects_the_old_x_path(self):
        """两态：把行数退回 GetDimensions / 把 X_源 退回物体矩阵表，都必须报 problem。"""
        good = _himi_exporter.merged_attach_hlsl_text()
        self.assertEqual(_himi_exporter.merged_attach_borrow_problems(good), [])
        rows_back = good.replace(
            "uint borrow_elements = HIMI_BORROW_ROWS;",
            "uint borrow_elements = 0u;\n    himi_borrow_map.GetDimensions(borrow_elements);",
        )
        self.assertNotEqual(rows_back, good, "夹具前提：替换必须真的生效")
        self.assertNotEqual(
            _himi_exporter.merged_attach_borrow_problems(rows_back),
            [],
            "退回运行期 GetDimensions 必须判红（实机只回 1/4 不到的行数）",
        )
        x_back = good.replace(
            "himi_affine_mul(borrow_m_own, himi_affine_inverse(borrow_m_src))",
            "himi_affine_mul(himi_affine_inverse(borrow_m_base), borrow_m_src)",
        )
        self.assertNotEqual(x_back, good, "夹具前提：替换必须真的生效")
        self.assertNotEqual(
            _himi_exporter.merged_attach_borrow_problems(x_back),
            [],
            "退回 inv(P_宿主)·P_源 必须判红（实测差 1.8 / 1.3，方向错）",
        )

    def test_merged_attach_cs_predicate_is_not_vacuous(self):
        """两态：把基址表删掉 / 把 IniParams 读回来 / 退回逐族宏，每一条都必须报错。"""
        good = _himi_exporter.merged_attach_hlsl_text()
        self.assertEqual(_himi_exporter.merged_attach_hlsl_problems(good), [])
        # 删掉基址表首行（程序化定位，不靠字面替换）
        first_row = _himi_exporter.MERGED_ATTACH_BASE_TABLE_SAMPLE_LINES[0]
        self.assertIn(first_row, good, "夹具前提：基址表首行必须逐字在文本里")
        dropped_row = good.replace(first_row + "\n", "", 1)
        self.assertNotEqual(dropped_row, good, "夹具前提：替换必须真的删掉一行")
        mutations = {
            "删掉基址表首行": dropped_row,
            "读回 IniParams": good.replace(
                "uint bone_index = dispatch_id.x;",
                "uint bone_index = dispatch_id.x; uint bad = (uint)IniParams[1].y;",
            ),
            "退回逐族基址宏": good.replace(
                "    uint base_slot = 0xFFFFFFFFu;",
                "#define HIMI_ATTACH_BASE 0\n    uint base_slot = 0xFFFFFFFFu;",
            ),
            "写侧丢掉 * 3u": good.replace(
                "uint dst = target_slot * 3u;", "uint dst = target_slot;"
            ),
        }
        for label, mutated in mutations.items():
            with self.subTest(mutation=label):
                self.assertNotEqual(
                    _himi_exporter.merged_attach_hlsl_problems(mutated),
                    [],
                    f"{label} 必须被判据抓到（否则判据是恒真的）",
                )

    # ---------------------------------------------------------------- 硬门（纯函数）

    def test_verify_cs_references_accepts_a_real_file(self):
        with tempfile.TemporaryDirectory(prefix="himi_cs_gate_") as root:
            res = os.path.join(root, "res")
            os.makedirs(res, exist_ok=True)
            with io.open(os.path.join(res, "himi_attach.hlsl"), "w", encoding="utf-8") as handle:
                handle.write("// ok\n")
            ini = "\n".join(
                [
                    "[CustomShaderHI3MergedSkeletonAttach_2c706fbd]",
                    f"cs = {_himi_exporter.MERGED_ATTACH_HLSL_PATH}",
                    "",
                    "[CustomShaderHI3MergedSkeletonAttach_694025c6]",
                    f"cs = {_himi_exporter.MERGED_ATTACH_HLSL_PATH}",
                ]
            )
            self.assertEqual(_himi_exporter.verify_cs_references(ini, root), [])

    def test_verify_cs_references_names_the_missing_file(self):
        """硬门的两态之一：文件不在 ⇒ 必须报出**该文件名**（不能只说「有一处问题」）。"""
        with tempfile.TemporaryDirectory(prefix="himi_cs_gate_") as root:
            os.makedirs(os.path.join(root, "res"), exist_ok=True)
            ini = f"[S]\ncs = {_himi_exporter.MERGED_ATTACH_HLSL_PATH}\n"
            problems = _himi_exporter.verify_cs_references(ini, root)
            self.assertEqual(len(problems), 1, problems)
            self.assertIn("himi_attach.hlsl", problems[0], "必须指名缺失文件")
            self.assertIn("第 2 行", problems[0], "必须报出行号")

    def test_verify_cs_references_rejects_escape_and_empty_and_absent(self):
        with tempfile.TemporaryDirectory(prefix="himi_cs_gate_") as root:
            res = os.path.join(root, "res")
            os.makedirs(res, exist_ok=True)
            with io.open(os.path.join(root, "outside.hlsl"), "w", encoding="utf-8") as handle:
                handle.write("// outside\n")
            # `../` 越界 ⇒ 即使文件真实存在也必须拒绝（否则 mod 目录约束形同虚设）
            escaped = _himi_exporter.verify_cs_references("cs = ../outside.hlsl\n", root)
            self.assertEqual(len(escaped), 1, escaped)
            self.assertIn("越出 mod 目录", escaped[0])
            # 空引用（另一行给一个「存在」的占位，避免同时触发"一条 cs 都没有"那条）
            empty = _himi_exporter.verify_cs_references("cs = ./res/x.hlsl\ncs =\n", root)
            self.assertEqual(len(empty), 2, empty)
            self.assertTrue(any("空引用" in item for item in empty), empty)
            self.assertTrue(all("第 2 行" in item for item in empty if "空引用" in item), empty)
            # 整份 ini 一条都没有 ⇒ 也要报（否则「有写方」这条前提无人守）
            none = _himi_exporter.verify_cs_references("[S]\nfoo = 1\n", root)
            self.assertEqual(len(none), 1, none)
            self.assertIn("没有任何 `cs =` 引用", none[0])

    def test_merged_attach_reference_problems_requires_one_single_path(self):
        single = _himi_exporter.MERGED_ATTACH_HLSL_PATH
        self.assertEqual(_himi_exporter.merged_attach_reference_problems([single, single]), [])
        # 回退成每族一份 ⇒ 必须报错
        mixed = _himi_exporter.merged_attach_reference_problems(
            [single, "./res/himi_attach_694025c6.hlsl"]
        )
        self.assertEqual(len(mixed), 1, mixed)
        self.assertIn("不是同一个文件", mixed[0])

    # ---------------------------------------------------------------- 导出端到端

    def _family_plan(self):
        """真实五族槽位数 → `plan.family_ranges`（不依赖任何 K: 数据）。"""
        plan = types.SimpleNamespace(
            slot_count=343,
            family_ranges={
                fam: {"base": base, "slots": slots, "own_block": True}
                for slots, base, fam in self.REAL_BASE_TABLE
            },
            resource=lambda folder=None: types.SimpleNamespace(
                name="ResourceHI3MergedSkeleton_role_0512e26f"
            ),
            diagnostics_text=lambda: "",
        )
        return plan

    def _exporter_with_plan(self, mod_root: str):
        blueprint_model = types.SimpleNamespace(keyname_mkey_dict={}, ordered_draw_obj_data_model_list=[])
        exporter = ExportHIMI(blueprint_model)
        exporter.drawib_model_list = []
        exporter.himi_merged_runtime_plan = lambda: self._family_plan()
        return exporter

    def test_export_writes_exactly_one_cs_and_all_five_sections_share_it(self):
        """落盘必须**只有一个** `res/himi_attach.hlsl`，且 5 条 `cs =` 全指它。"""
        with tempfile.TemporaryDirectory(prefix="himi_attach_write_") as root:
            _GLOBAL_STATE["mod"] = root
            _GLOBAL_STATE["workspace"] = os.path.join(root, "RoleA")
            os.makedirs(_GLOBAL_STATE["workspace"], exist_ok=True)
            try:
                exporter = self._exporter_with_plan(root)
                builder = M_IniBuilder()
                exporter._himi_emit_family_palette_and_attach_sections(
                    builder, self._family_plan(), "Meshes"
                )
                lines = _builder_lines(builder)
                sections = [line for line in lines if line.startswith("[CustomShaderHI3MergedSkeletonAttach_")]
                self.assertEqual(len(sections), 5, "5 个族各一个 attach 段")
                cs_lines = [line for line in lines if re.match(r"^\s*cs\s*=", line)]
                self.assertEqual(len(cs_lines), 5, "5 条 cs 引用")
                self.assertEqual(
                    sorted({line.split("=", 1)[1].strip() for line in cs_lines}),
                    [_himi_exporter.MERGED_ATTACH_HLSL_PATH],
                    "5 条 cs 必须指向同一个文件",
                )
                # 落盘
                result = exporter._himi_write_attach_cs()
                self.assertTrue(result["ok"], result)
                res_dir = os.path.join(root, "res")
                self.assertTrue(os.path.isdir(res_dir), "必须创建 <mod>/res/")
                self.assertEqual(
                    sorted(os.listdir(res_dir)),
                    [_himi_exporter.MERGED_ATTACH_HLSL_BASENAME],
                    "<mod>/res/ 下只能有这一个 hlsl（不得逐族 5 份）",
                )
                self.assertEqual(result["path"], os.path.join(res_dir, "himi_attach.hlsl"))
                with io.open(result["path"], encoding="utf-8") as handle:
                    written = handle.read()
                self.assertEqual(
                    _himi_exporter.merged_attach_hlsl_problems(written), [], "落盘内容必须过判据"
                )
                # 幂等：第二次不重写
                again = exporter._himi_write_attach_cs()
                self.assertFalse(again["written"], "内容一致时不得重写")
                self.assertEqual(again["bytes"], result["bytes"])
            finally:
                _GLOBAL_STATE["mod"] = ""
                _GLOBAL_STATE["workspace"] = ""

    def test_attach_range_file_gate_two_state(self):
        """【T6-F2 回归钉子】F5 的**产物级文件存在性硬门**必须真的被喂到数据。

        背景（t6 §8 的 T6-F2，medium）：`verify_attach_range_references` 此前**零回归钉子** ——
        `git grep -n verify_attach_range_references -- tests` 无命中；把它变异成恒返回 `[]` 后
        跑**整个测试文件**：调用 0 次、0 个用例失败。这与 K2 的「空转」是**同一类失效**
        （保护看起来存在、但没有任何人守）。本用例照同一文件里
        `test_cs_reference_hard_gate_two_state` 的范式，给这条硬门钉四个两态。

        **变异实验（t6 的 m3）复现方式**：把
        `ui/universal/himi.py::verify_attach_range_references` 的函数体换成 `return []`，
        再跑本用例 ⇒ **必须红**（A/B 态都会失败）。
        """
        Fatal = sys.modules[f"{PKG}.utils.format_utils"].Fatal
        with tempfile.TemporaryDirectory(prefix="himi_range_twostate_") as root:
            _GLOBAL_STATE["mod"] = root
            _GLOBAL_STATE["workspace"] = os.path.join(root, "RoleA")
            os.makedirs(_GLOBAL_STATE["workspace"], exist_ok=True)
            try:
                exporter = self._exporter_with_plan(root)
                plan = self._family_plan()
                builder = M_IniBuilder()
                exporter._himi_emit_family_palette_and_attach_sections(builder, plan, "Meshes")
                ini_path = os.path.join(root, "RoleA.ini")
                with io.open(ini_path, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(_builder_lines(builder)) + "\n")
                # 夹具前提：5 个族段各一条 `cs-t1`，且都指向身份资源
                references = [
                    line for line in _builder_lines(builder) if re.match(r"^\s*cs-t1\s*=", line)
                ]
                self.assertEqual(len(references), 5, references)
                self.assertTrue(all("ResourceHI3AttachRange_" in item for item in references))

                # ---- B 态（反例①）：身份资源一个都还没落盘 ⇒ 每族 2 条全报「不存在」
                # （【t91】每族除身份载荷外还有一份 BorrowMap，硬门两样都查 ⇒ 5 族 × 2 = 10）
                problems = exporter._himi_enforce_attach_range_references(ini_path)
                self.assertEqual(len(problems), 10, problems)
                self.assertTrue(all("引用的文件不存在" in item for item in problems), problems)
                self.assertTrue(
                    all(
                        "AttachRange.buf" in item or "BorrowMap.buf" in item for item in problems
                    ),
                    f"每条都必须指名缺哪个族的文件：{problems}",
                )
                # 【t73·兜底原则】不再是 Fatal ⇒ 降级为普通导出 + 大声警告。
                exporter._himi_raise_for_attach_range_references(ini_path)   # 不得抛
                self.assertTrue(exporter.himi_degraded_to_ordinary())
                self.assertTrue(
                    any("AttachRange" in item for item in exporter._himi_degrade_notices),
                    exporter._himi_degrade_notices,
                )

                # ---- A 态（正例）：5 个身份资源齐备 ⇒ []，且不得降级（用新导出器）----
                clean = self._exporter_with_plan(root)
                published = clean._himi_publish_attach_ranges(plan, "Meshes")
                self.assertTrue(published["ok"], published)
                # 每族 2 份：身份载荷 + BorrowMap（【t91】借用表与身份资源同目录同批写出）
                self.assertEqual(len(published["files"]), 10, published)
                self.assertEqual(clean._himi_enforce_attach_range_references(ini_path), [])
                clean._himi_raise_for_attach_range_references(ini_path)
                self.assertFalse(clean.himi_degraded_to_ordinary(), "正例不得降级")
                exporter = clean          # 后续 B 态继续用干净的那个

                # ---- B 态（反例②）：删掉**一族**的文件 ⇒ 只报那一个，且指名该族 ----
                victim_family = "694025c6"  # 18 槽那一族（_family_plan 的真实排布之一）
                victim = os.path.join(root, "Meshes", f"{victim_family}-AttachRange.buf")
                self.assertTrue(os.path.isfile(victim), f"夹具前提：{victim} 必须存在")
                os.remove(victim)
                single = exporter._himi_enforce_attach_range_references(ini_path)
                self.assertEqual(len(single), 1, single)
                self.assertIn("引用的文件不存在", single[0])
                self.assertIn(os.path.basename(victim), single[0])
                exporter._himi_raise_for_attach_range_references(ini_path)
                self.assertTrue(exporter.himi_degraded_to_ordinary(), "缺一族 ⇒ 降级")
                # 还原 ⇒ 又绿（两态都能反复观测，不是一次性）
                exporter._himi_publish_attach_ranges(plan, "Meshes")
                self.assertEqual(exporter._himi_enforce_attach_range_references(ini_path), [])

                # ---- 反例③：ini 里**没有任何** `cs-t1` ⇒ 必须报（否则「没绑定」会静默通过）----
                no_reference = os.path.join(root, "no_reference.ini")
                with io.open(no_reference, "w", encoding="utf-8") as handle:
                    handle.write("[SomeSection]\nfoo = 1\n")
                missing = exporter._himi_enforce_attach_range_references(no_reference)
                self.assertEqual(len(missing), 1, missing)
                self.assertIn("cs-t1", missing[0])
                self.assertIn("没有任何", missing[0])

                # ---- 反例④：`filename` 为空 ⇒ 必须报（不得因为「声明存在」就算通过）----
                empty_name = os.path.join(root, "empty_filename.ini")
                with io.open(empty_name, "w", encoding="utf-8") as handle:
                    handle.write(
                        "\n".join(
                            [
                                "[ResourceHI3AttachRange_aaaa1111]",
                                "type = Buffer",
                                "format = R32G32B32A32_FLOAT",
                                "filename =",
                                "",
                                "[CustomShaderHI3MergedSkeletonAttach_aaaa1111]",
                                "cs-t1 = ref ResourceHI3AttachRange_aaaa1111",
                            ]
                        )
                        + "\n"
                    )
                empty = exporter._himi_enforce_attach_range_references(empty_name)
                self.assertEqual(len(empty), 1, empty)
                self.assertIn("filename 为空", empty[0])
            finally:
                _GLOBAL_STATE["mod"] = ""
                _GLOBAL_STATE["workspace"] = ""

    def test_cs_reference_hard_gate_two_state(self):
        """**两态证明**：删掉那**一个** `.hlsl` ⇒ 硬门报错并指名该文件；还原 ⇒ 通过。"""
        with tempfile.TemporaryDirectory(prefix="himi_cs_twostate_") as root:
            _GLOBAL_STATE["mod"] = root
            _GLOBAL_STATE["workspace"] = os.path.join(root, "RoleA")
            os.makedirs(_GLOBAL_STATE["workspace"], exist_ok=True)
            try:
                exporter = self._exporter_with_plan(root)
                builder = M_IniBuilder()
                exporter._himi_emit_family_palette_and_attach_sections(
                    builder, self._family_plan(), "Meshes"
                )
                # 让 `cs =` 能被解析到：把段写进产物 ini
                ini_path = os.path.join(root, "RoleA.ini")
                with io.open(ini_path, "w", encoding="utf-8") as handle:
                    handle.write("\n".join(_builder_lines(builder)) + "\n")

                # ---- B 态（反例）：CS 不存在 ----
                problems = exporter._himi_enforce_attach_cs_references(ini_path)
                cs_total = sum(
                    1 for line in _builder_lines(builder) if re.match(r"^\s*cs\s*=", line)
                )
                self.assertEqual(cs_total, 5, "夹具前提：5 个族段各一条 cs")
                self.assertEqual(len(problems), cs_total, problems)
                self.assertTrue(
                    all("himi_attach.hlsl" in item for item in problems),
                    f"每条都必须指名缺失文件：{problems}",
                )
                self.assertTrue(
                    all("引用的文件不存在" in item for item in problems), problems
                )
                # 【t73·兜底原则】失败通道**不再是 Fatal**：合并侧失败 ⇒ 降级为普通导出 + 大声警告
                # （普通导出是兜底，不可被合并骨架剥夺）。
                Fatal = sys.modules[f"{PKG}.utils.format_utils"].Fatal
                exporter._himi_raise_for_attach_cs_references(ini_path)   # 不得抛
                self.assertTrue(exporter.himi_degraded_to_ordinary(), "必须已降级")
                self.assertFalse(
                    getattr(exporter.himi_merged_runtime_plan(), "ok", False),
                    "降级必须把计划关掉（否则会产出半合并的产物）",
                )
                self.assertTrue(
                    any("cs" in item and "himi_attach.hlsl" in item
                        for item in exporter._himi_degrade_notices),
                    exporter._himi_degrade_notices,
                )

                # ---- A 态（正例）：把 CS 落盘 ⇒ 判据回到 []（用**新**导出器，避免上一步的降级残留）----
                clean = self._exporter_with_plan(root)
                clean._himi_write_attach_cs()
                self.assertEqual(clean._himi_enforce_attach_cs_references(ini_path), [])
                clean._himi_raise_for_attach_cs_references(ini_path)
                self.assertFalse(clean.himi_degraded_to_ordinary(), "正例不得降级")
                # 再删掉 ⇒ 判据又报（两态都能反复观测），且仍走降级
                os.remove(os.path.join(root, "res", "himi_attach.hlsl"))
                again = clean._himi_enforce_attach_cs_references(ini_path)
                self.assertEqual(len(again), cs_total, again)
                self.assertTrue(all("himi_attach.hlsl" in item for item in again), again)
                clean._himi_raise_for_attach_cs_references(ini_path)
                self.assertTrue(clean.himi_degraded_to_ordinary())
            finally:
                _GLOBAL_STATE["mod"] = ""
                _GLOBAL_STATE["workspace"] = ""


class HIMIStubGateInteractionTests(HIMIMergedRuntimeBase):
    """站位只决定「要不要压原版 draw」，与「能不能重绑」解耦。

    本节只断言**运行时计划侧**的口径；判据本体与三条规则在
    ``tests/test_himi_stub_objects.py``。
    """

    def _make_dump_fixture(self, *, so_target: str = "7cb2e09c") -> str:
        """给 `_make_workspace` 造出的组件补一份**最小但完整**的 dump，使 join 成立。

        要点：组件 json 的 `CategoryHash.Position` / `.Blend` 必须等于 deform pass
        vb0/vb1 的 **deduped 内容 hash**（侧车文件名里那一段），`VertexLimitVB` 必须
        等于 `SOSetTargets` 的 hash。
        """
        import hashlib

        info = _make_workspace(
            self.workspace,
            "RoleA",
            [
                {
                    "bare": "aaaa1111-1-0",
                    "draw_ib": "aaaa1111",
                    "vg_count": 2,
                    "original_vertex_count": 10,
                    "family_members": ["aaaa1111-1-0"],
                    "rows": 10,
                    "local_indices": [[0, 1, 0, 0]],
                }
            ],
            # 槽位数必须等于**被引用**局部槽映出的全局槽数（= 2）：家族区间只由「数据层为
            # 被引用局部槽注册的槽位」构成（未被引用的局部槽拿到的是 gap slots，永不进几何、
            # 也不该撑大 attach 段的区间）。写成 8 会造出「骨架 8 槽 / 只有 2 槽有写方」的
            # 不一致夹具，被 fail-closed 正确拒绝。
            slot_count=2,
        )
        component_dir = os.path.join(self.workspace, "LOD0", "aaaa1111-1-0", GAMETYPE)
        position_bytes = open(os.path.join(component_dir, "aaaa1111-1-0-Blend.buf"), "rb").read()
        # 工作空间里没有 Position.buf（_make_workspace 只写 Blend），补一份
        position_path = os.path.join(component_dir, "aaaa1111-1-0-Position.buf")
        with open(position_path, "wb") as handle:
            handle.write(bytes(44 * 10))
        position_bytes = open(position_path, "rb").read()
        blend_bytes = open(os.path.join(component_dir, "aaaa1111-1-0-Blend.buf"), "rb").read()
        position_content = hashlib.sha256(position_bytes).hexdigest()[:8]
        blend_content = hashlib.sha256(blend_bytes).hexdigest()[:8]

        # 用真实内容 hash 覆盖组件 json 的两个键（join 靠它）
        json_path = os.path.join(component_dir, "aaaa1111-1-0.json")
        payload = json.loads(open(json_path, encoding="utf-8").read())
        payload["CategoryHash"]["Position"] = position_content
        payload["CategoryHash"]["Blend"] = blend_content
        payload["VertexLimitVB"] = so_target
        with io.open(json_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False)

        dump = os.path.join(self._tmp, "FrameAnalysis-runtime-stub")
        deduped = os.path.join(dump, "deduped")
        os.makedirs(deduped, exist_ok=True)
        with open(os.path.join(deduped, position_content + ".buf"), "wb") as handle:
            handle.write(position_bytes)
        with io.open(
            os.path.join(
                deduped,
                f"{position_content}-vb0-layout=abcd1234-topology=pointlist-stride=44-count=10.txt",
            ),
            "w",
            encoding="utf-8",
        ) as handle:
            handle.write("")
        with open(os.path.join(deduped, blend_content + ".buf"), "wb") as handle:
            handle.write(blend_bytes)
        with io.open(
            os.path.join(
                deduped,
                f"{blend_content}-vb1-layout=abcd1234-topology=pointlist-stride=32-count=10.txt",
            ),
            "w",
            encoding="utf-8",
        ) as handle:
            handle.write("")
        palette = bytearray()
        for slot in range(SLOT_COUNT):
            for index in range(12):
                palette += struct.pack("<f", float(slot * 12 + index))
        with open(os.path.join(deduped, "eeeeffff.buf"), "wb") as handle:
            handle.write(bytes(palette))

        lines = [
            f"000035 3DMigoto Dumping Buffer {dump}\\000035-vb0=11111111-vs=10905ed856e6b621.buf"
            f" -> {deduped}\\{position_content}.buf",
            f"000035 3DMigoto Dumping Buffer {dump}\\000035-vb0=11111111-vs=10905ed856e6b621.txt"
            f" -> {deduped}\\{position_content}-vb0-layout=abcd1234-topology=pointlist-stride=44-count=10.txt",
            f"000035 3DMigoto Dumping Buffer {dump}\\000035-vb1=22222222-vs=10905ed856e6b621.buf"
            f" -> {deduped}\\{blend_content}.buf",
            f"000035 3DMigoto Dumping Buffer {dump}\\000035-vb1=22222222-vs=10905ed856e6b621.txt"
            f" -> {deduped}\\{blend_content}-vb1-layout=abcd1234-topology=pointlist-stride=32-count=10.txt",
            f"000035 3DMigoto Dumping Buffer {dump}\\000035-vs-t0=33333333-vs=10905ed856e6b621.buf"
            f" -> {deduped}\\eeeeffff.buf",
            "000035 VSSetShader(pVertexShader:0x1, ppClassInstances:0x0, NumClassInstances:0) hash=10905ed856e6b621",
            "000035 SOSetTargets(NumBuffers:1, ppSOTargets:0x1, pOffsets:0xE468)",
            f"       0: resource=0x2 hash={so_target}",
            "000035 VSSetShaderResources(StartSlot:0, NumViews:1, ppShaderView:0x3)",
            "       0: view=0x2 resource=0x3 hash=33333333",
            "000035 IASetVertexBuffers(StartSlot:0, NumBuffers:2, ppVertexBuffers:0x4)",
            "       0: resource=0x5 hash=11111111",
            "       1: resource=0x6 hash=22222222",
            "000035 IASetPrimitiveTopology(Topology:1)",
            "000035 Draw(VertexCount:10, StartVertexLocation:0)",
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
        with io.open(os.path.join(dump, "log.txt"), "w", encoding="utf-8") as handle:
            handle.write("\n".join(lines) + "\n")
        with io.open(
            os.path.join(self.workspace, "Config", "FrameAnalysisPath.json"),
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump({"frameAnalysisFolderPath": dump}, handle, ensure_ascii=False)
        return info

    def test_gate_metadata_declares_stub_is_not_a_coverage_claim(self):
        """G1-a/G1-b/G1-c：字段必须由 **`prepare()` 自己写进 metadata**（口径冻结）。

        站位判据本体的用例在 ``tests/test_himi_stub_objects.py``；这里核对
        「计划书 §7.7.6 的裁定是否真的落进了写回元数据契约」——所以断言的是
        `prepare()['metadata']` 的真值，而不是手工塞进去的字段。
        """
        self._make_dump_fixture()
        plan = _skeleton.HIMISkeletonMergeHelper.prepare(self.workspace, None, force=True)
        self.assertTrue(plan["ok"], plan.get("message"))
        metadata = plan["metadata"]
        # 站位不参与 G1：三个字段都是 False（口径冻结，改动会红）
        self.assertIs(metadata["StubTargetBaresIncluded"], False)
        self.assertIs(metadata["StubFamilyCoverageCounted"], False)
        self.assertIs(metadata["StubPassGateExempt"], False)
        self.assertEqual(metadata["StubGateInteractionVersion"], 1)
        # G1-b 的站位清单（计划书 §7.7.6 逐字要求的字段名）
        self.assertIn("StubInjectedComponents", metadata)
        self.assertIn("StubSkippedComponents", metadata)
        self.assertEqual(metadata["StubDecisionVersion"], 1)
        self.assertIsInstance(metadata["StubInjectedComponents"], list)
        self.assertIsInstance(metadata["StubSkippedComponents"], list)
        # 站位件（未落盘的合成对象）不在 target 集合里；target 集合 = 通过门控的已落盘部件
        self.assertEqual(
            set(metadata["StubTargetSetBares"]),
            {entry["bare"] for entry in plan["writes"]},
        )
        self.assertNotIn("aaaa1111-9-9", metadata["StubTargetSetBares"])
        # 站位不改变 G1 的 pass 级判定：`gate_report` 里每个家族的 `safe`（家族完整性）
        # 与 `members`/`missing` 都必须照实存在（G1-c：pass 消费者层**不豁免**）
        gate_report = plan["gate_report"] or {}
        self.assertTrue(gate_report, "前提：本次确实产出了门控报告")
        for family, entry in gate_report.items():
            self.assertEqual(entry["family"], family)
            self.assertIn("members", entry, f"{family} 必须带家族成员表")
            self.assertIn("missing", entry, f"{family} 必须带未纳入成员表")
            self.assertIn("safe", entry, f"{family} 必须带家族完整性判定")
            # family_safe 的语义：家族成员全部纳入目标集合才 safe（站位不参与）
            self.assertIs(entry["safe"], not entry["missing"])

    def test_stub_decision_does_not_change_rebinds(self):
        """站位决策（只算不注入）前后，运行时计划的 rebinds 完全相同。"""
        self._make_dump_fixture()
        with io.open(
            os.path.join(self.workspace, "LOD0", _skeleton.HIMI_COMPONENT_MAP_BASENAME),
            "w",
            encoding="utf-8",
        ) as handle:
            json.dump({"aaaa1111": {"0": "aaaa1111-1-0"}}, handle, ensure_ascii=False)

        before = _runtime.load_workspace_plan(self.workspace)
        self.assertTrue(before.ok, before.message)
        rebinds_before = sorted(item["draw_ib"] for item in before.rebinds)
        self.assertTrue(rebinds_before, "前提：本工作空间本来就有可重绑部件")

        # 走一次真实的站位判据（不注入对象，只算决策）：
        # 家族成员不在场景里，且它的专属槽位 0 被引用 ⇒ 判「被吸收」⇒ 会插桩。
        decision = _skeleton.decide_stub_injection(
            workspace_root=self.workspace,
            scene_present_bares=set(),
            family_map=_skeleton.load_component_family_map(self.workspace),
            used_group_ids={0},
        )
        self.assertEqual(
            decision.stub_members,
            ["aaaa1111-1-0"],
            "专属槽位被引用 ⇒ 该部件应补占位（本用例借此证明「插桩」也不影响 rebinds）",
        )

        after = _runtime.load_workspace_plan(self.workspace)
        self.assertEqual(
            sorted(item["draw_ib"] for item in after.rebinds),
            rebinds_before,
            "站位决策不得改变 rebinds",
        )


# --------------------------------------------------------------------------- #
# E. t4 修复：F5（本族 span 上界）/ F6（基址表硬门）/ T45-F4（组索引==名字）
#    / F1-F2 判据（变形重放必须无条件）
# --------------------------------------------------------------------------- #
#
# 每个修复点都配**两态**用例：正例必须绿，把修复改回去（变异体）必须红。
# 根因编号对应 `reports/himi-merge/50-code-audit.md`：
#   F5 = §1.2 入口 ①（CS 的界用错了量）/ §4.3 F5
#   F6 = §1.2 入口 ②（冻结表 vs 动态 plan 无双事实源校验）
#   T45-F4 = §5.G（组名→全局 id 无导出期断言）
#   F1/F2 = §4.1/§4.2（任何让合并 deform 被跳过的方案都不可接受；F2 已证伪）


class HIMIAttachRangeIdentityTests(unittest.TestCase):
    """【F5】每族身份资源：``cs-t1`` 让「本族 span 上界」独立于查表键。"""

    #: 真实五族（报告 50 §1.1 的复算结果）
    REAL_RANGES = {
        "2c706fbd": (0, 184),
        "694025c6": (184, 18),
        "70a4da3e": (202, 71),
        "b35cfc6c": (273, 19),
        "def0c0ae": (292, 51),
    }

    def test_identity_resource_shape_and_payload_come_from_the_plan(self):
        """(base, span) 必须来自调用方给的 plan（不是写死的常量），且载荷可逐位解回。"""
        for family, (base, span) in self.REAL_RANGES.items():
            with self.subTest(family=family):
                payload = _runtime.attach_range_payload(base, span)
                self.assertEqual(len(payload), _runtime.ATTACH_RANGE_BYTES)
                decoded = _runtime.decode_attach_range_payload(payload)
                self.assertTrue(decoded["ok"], decoded)
                self.assertEqual(decoded["base"], base)
                self.assertEqual(decoded["span"], span)
                self.assertEqual(decoded["version"], _runtime.ATTACH_RANGE_VERSION)
        # 犯罪现场保护：不同 (base, span) 的载荷必须不同（否则身份就没有鉴别力）
        self.assertNotEqual(
            _runtime.attach_range_payload(184, 18), _runtime.attach_range_payload(202, 71)
        )
        # 身份资源是 typed（与合并骨架同款：写 format、**不加** stride）；
        # 长度由文件字节数决定 = 128 B（头部 + Q_f⁻¹ + X_f + 族索引，没有 array= 行）。
        lines = _runtime.attach_range_ini_lines("694025c6", "Meshes/694025c6-AttachRange.buf")
        self.assertEqual(lines[0], "[ResourceHI3AttachRange_694025c6]")
        self.assertIn("type = Buffer", lines)
        self.assertIn(f"format = {_runtime.ATTACH_RANGE_INI_FORMAT}", lines)
        self.assertIn("filename = Meshes/694025c6-AttachRange.buf", lines)
        self.assertFalse([line for line in lines if line.startswith("stride")], lines)

    def test_publish_writes_every_own_block_family_and_is_idempotent(self):
        """落盘必须**每族一份**、内容 = 本次 plan；第二次内容相同时**仍要重写**（刷 mtime）。"""
        ranges = {
            family: {"base": base, "slots": span, "own_block": True}
            for family, (base, span) in self.REAL_RANGES.items()
        }
        ranges["ffff0000"] = {"base": 0, "slots": 0, "own_block": False}  # 借位族 ⇒ 不发身份
        with tempfile.TemporaryDirectory(prefix="himi_attach_range_") as folder:
            result = _runtime.publish_attach_ranges(ranges, folder)
            self.assertTrue(result["ok"], result)
            # 每族 2 份：`-AttachRange.buf`（恒 128 B）+ `-BorrowMap.buf`（【t91】借用表，
            # 没有借用槽的族写一行哨兵）——两者同批落盘，缺一样就是产物不自洽。
            self.assertEqual(len(result["files"]), 2 * len(self.REAL_RANGES))
            self.assertEqual(
                sorted(os.listdir(folder)),
                sorted(
                    [(_runtime.attach_range_basename(fam)) for fam in self.REAL_RANGES]
                    + [(_runtime.borrow_map_basename(fam)) for fam in self.REAL_RANGES]
                ),
                "借位族（slots=0）不得发身份资源，也不得发借用表",
            )
            for family, (base, span) in self.REAL_RANGES.items():
                with io.open(os.path.join(folder, _runtime.attach_range_basename(family)), "rb") as handle:
                    decoded = _runtime.decode_attach_range_payload(handle.read())
                self.assertEqual((decoded["base"], decoded["span"]), (base, span), family)
            before_mtime = {
                family: os.stat(os.path.join(folder, _runtime.attach_range_basename(family))).st_mtime_ns
                for family in self.REAL_RANGES
            }
            again = _runtime.publish_attach_ranges(ranges, folder)
            self.assertTrue(again["ok"])
            # 【t39-X 校准】每次导出都**无条件重写**身份资源（旧实现内容一致就跳过写盘，
            # 产物里出现过「AttachRange 的 mtime 停在上一次场景」的可观测证据）。
            self.assertTrue(again["written"], "每次导出都要重写身份资源（不允许按内容相同跳过）")
            self.assertEqual(again["rewritten"], 0, "内容一致 ⇒ rewritten 记 0（只是重刷，不是变了）")
            for family, (base, span) in self.REAL_RANGES.items():
                path = os.path.join(folder, _runtime.attach_range_basename(family))
                with io.open(path, "rb") as handle:
                    decoded = _runtime.decode_attach_range_payload(handle.read())
                self.assertEqual((decoded["base"], decoded["span"]), (base, span), family)
                self.assertGreaterEqual(
                    os.stat(path).st_mtime_ns, before_mtime[family],
                    "重写必须刷新 mtime（否则产物又可能比场景旧）",
                )

    def test_span_bound_truncates_and_never_spills_into_the_next_block_TWO_STATE(self):
        """**两态**：副本维度大于/错配本族 span 时，新逻辑截断或拒写，旧逻辑溢出到下一族。

        场景取 **t3 §1.2 入口 ① 的原例**：族 ``694025c6``（base=184, span=18）的副本维度
        变成 71（= 头发族 ``70a4da3e`` 的 span，例如该角色 palette 池大于被引用槽数）。
        """
        table = {
            span: base
            for base, span in ((0, 184), (184, 18), (202, 71), (273, 19), (292, 51))
        }
        self.assertEqual(table[18], 184, "夹具前提：18 槽 → 基址 184")
        self.assertEqual(table[71], 202, "夹具前提：71 槽 → 基址 202（头发族块）")
        skeleton_slots = 343

        def _old_logic(bone_index, source_slots, base_slot):
            """**修复前的判据**：只有「源维度」与「骨架总槽数」两道界（报告 50 §1.2 入口 ①）。"""
            if base_slot < 0:
                return -1
            if bone_index >= source_slots:
                return -1
            target = base_slot + bone_index
            return -1 if target >= skeleton_slots else target

        def _new_logic(bone_index, source_slots, table_base, identity_base, identity_span):
            return _runtime.attach_write_target(
                bone_index=bone_index,
                source_slots=source_slots,
                table_base=table_base,
                identity_base=identity_base,
                identity_span=identity_span,
                skeleton_slots=skeleton_slots,
            )

        # ---- A 态（正常）：副本维度 == 本族 span ⇒ 逐槽落进自己的块 ----
        for bone in range(18):
            self.assertEqual(
                _new_logic(bone, 18, table[18], 184, 18), 184 + bone, f"A 态 bone={bone}"
            )
        # ---- B 态（异常）：副本维度 71 ≠ 本族 span 18 ----
        # 旧逻辑：把 694025c6 的骨矩阵写进 202..272 = **头发族的块**（整块错位形态）
        spilled = [
            _old_logic(bone, 71, table[71]) for bone in range(71) if _old_logic(bone, 71, table[71]) >= 0
        ]
        self.assertEqual(spilled[0], 202, "旧逻辑的第一次写入落在头发族块基址上")
        self.assertGreater(len(spilled), 18, "旧逻辑确实会写超出本族 span 的槽位（非恒真夹具）")
        # 新逻辑：表与身份不一致 ⇒ **拒写**（一条都不写，绝不冒用别人的槽）
        self.assertEqual(
            [_new_logic(bone, 71, table[71], 184, 18) for bone in range(71)],
            [-1] * 71,
            "表（202）与身份（184）矛盾 ⇒ 必须整族拒写",
        )
        # ---- C 态（异常但表不认识这一族）：身份权威 ⇒ 写进**自己的**块、按 span 截断 ----
        targets = [_new_logic(bone, 71, -1, 184, 18) for bone in range(71)]
        self.assertEqual(targets[:18], [184 + bone for bone in range(18)], "前 18 根落在自己的块")
        self.assertEqual(set(targets[18:]), {-1}, "第 18 根起必须截断（不得溢出到 202..）")
        # ---- D 态：身份不可用（magic 不命中 / 绑空）⇒ 退回冻结表 = 今天的行为 ----
        fallback = [_new_logic(bone, 18, table[18], -1, -1) for bone in range(18)]
        self.assertEqual(fallback, [184 + bone for bone in range(18)], "退回冻结表不得改变行为")

    def test_generated_cs_has_identity_and_span_guard_TWO_STATE(self):
        """CS 文本必须真的读身份资源 + 有 span 上界；删掉任一处判据必须红。"""
        text = _himi_exporter.merged_attach_hlsl_text()
        code = _himi_exporter._strip_hlsl_comments(text)
        self.assertEqual(_himi_exporter.merged_attach_hlsl_problems(text), [], "正例必须通过")
        self.assertIn("himi_attach_range : register(t1);", code)
        self.assertIn("identity_base = asuint(attach_range.x);", code)
        self.assertIn("span_slots = asuint(attach_range.y);", code)
        self.assertIn("bone_index >= span_slots", code)
        self.assertIn("base_slot != identity_base", code)
        # 身份资源不可用时必须退回冻结表（不得因为 t1 空就整族消失）
        self.assertIn("identity_base = 0xFFFFFFFFu;", code)
        mutations = {
            "删掉 span 上界（F5 的反面）": text.replace(
                "    if (span_slots > 0u && bone_index >= span_slots)", "    if (false)"
            ),
            "删掉身份资源声明": text.replace(
                "Buffer<float4> himi_attach_range : register(t1);", ""
            ),
            "身份不校验魔数": text.replace(
                f"asuint(attach_range.w) == {_runtime.ATTACH_RANGE_MAGIC_MACRO}",
                "asuint(attach_range.w) >= 0u",
            ),
            "表与身份矛盾时不再拒写": text.replace(
                "        if (base_slot != 0xFFFFFFFFu && base_slot != identity_base)",
                "        if (false)",
            ),
        }
        for label, mutated in mutations.items():
            with self.subTest(mutation=label):
                self.assertNotEqual(mutated, text, f"{label}：变异必须真的改到文本")
                self.assertNotEqual(
                    _himi_exporter.merged_attach_hlsl_problems(mutated),
                    [],
                    f"{label} ⇒ 判据必须报错（否则判据恒真）",
                )


class HIMIBaseTableGateTests(unittest.TestCase):
    """【F6】基址表 ← 本次 ``plan.family_ranges``（唯一权威来源）：矛盾必须 Fatal，其余大声诊断。

    表不再是常量：``merged_attach_base_table_lines(ranges)`` 按 plan 生成，导出侧渲染的就是
    这一份。于是这道门抓的是生成器层面的**结构性残余** —— 两个族的**源槽位数相同**（运行期
    只按源槽位数查表 ⇒ 两族撞同一个槽块），以及 ∑span ≠ 槽位数这类诊断。
    """

    REAL_RANGES = {
        "2c706fbd": {"base": 0, "slots": 184, "own_block": True},
        "694025c6": {"base": 184, "slots": 18, "own_block": True},
        "70a4da3e": {"base": 202, "slots": 71, "own_block": True},
        "b35cfc6c": {"base": 273, "slots": 19, "own_block": True},
        "def0c0ae": {"base": 292, "slots": 51, "own_block": True},
    }

    def test_table_rows_pin_the_real_layout(self):
        rows = _himi_exporter.merged_attach_base_table_rows()
        self.assertEqual(
            sorted((slots, base) for slots, base, _family in rows),
            sorted((info["slots"], info["base"]) for info in self.REAL_RANGES.values()),
            "参考样例必须逐行等于参考角色的真实五族 (span, base)",
        )
        # 样本常量是**生成器算出来的**（不是手写）⇒ 与本角色真实排布生成的表逐字一致
        self.assertEqual(
            list(_himi_exporter.MERGED_ATTACH_BASE_TABLE_SAMPLE_LINES),
            _himi_exporter.merged_attach_base_table_lines(self.REAL_RANGES),
            "样本表必须与「按真实排布生成」逐字相同（注释里的族 ID 也是）",
        )

    def test_audit_is_clean_on_the_real_layout_and_matches_the_export_gate(self):
        audit = _himi_exporter.merged_attach_base_table_audit(self.REAL_RANGES, 343)
        self.assertEqual(audit["problems"], [], audit)
        self.assertEqual(audit["notices"], [], audit)
        # 与导出侧调用点同源：真实排布下 gate 不得抛
        plan = types.SimpleNamespace(family_ranges=self.REAL_RANGES, slot_count=343)
        blueprint_model = types.SimpleNamespace(
            keyname_mkey_dict={}, ordered_draw_obj_data_model_list=[]
        )
        exporter = ExportHIMI(blueprint_model)
        exporter.drawib_model_list = []
        exporter._himi_verify_base_table_against_plan(plan)  # 不抛 = 通过

    def test_generated_table_carries_this_roles_layout_not_the_reference_one(self):
        """**本任务的回归点**：本角色 ``077983c9`` 的 span 也是 51，但基址是 0。

        表由 plan 生成 ⇒ 渲染出的是 ``51→0 / 165→51 / 1→216``，绝不是参考角色那份
        （``51→292``，那会让运行期 ``base_slot != identity_base`` 整族拒写、导出期 F6 Fatal）。
        """
        ranges = {
            "077983c9": {"base": 0, "slots": 51, "own_block": True},
            "09c6e58a": {"base": 51, "slots": 165, "own_block": True},
            "1b62725d": {"base": 216, "slots": 1, "own_block": True},
        }
        lines = _himi_exporter.merged_attach_base_table_lines(ranges)
        self.assertEqual(
            lines,
            [
                "    if (source_slots == 51u) { base_slot = 0u; }          // 077983c9",
                "    else if (source_slots == 165u) { base_slot = 51u; }   // 09c6e58a",
                "    else if (source_slots == 1u) { base_slot = 216u; }    // 1b62725d",
            ],
        )
        text = _himi_exporter.merged_attach_hlsl_text(ranges=ranges)
        self.assertEqual(
            _himi_exporter.merged_attach_base_table_lines_in_text(text), lines
        )
        self.assertEqual(_himi_exporter.merged_attach_hlsl_problems(text), [])
        self.assertNotIn("292", "".join(lines), "不得残留参考角色的基址")

    def test_borrowed_family_does_not_get_a_row(self):
        """借位族（``own_block`` 假 / span 0）没有自己的槽块 ⇒ 不得进表。"""
        ranges = dict(self.REAL_RANGES)
        ranges["0000beef"] = {"base": 0, "slots": 0, "own_block": False}
        lines = _himi_exporter.merged_attach_base_table_lines(ranges)
        self.assertEqual(len(lines), 5)
        self.assertFalse(any("0000beef" in line for line in lines))

    def test_two_families_with_the_same_source_span_must_be_fatal_TWO_STATE(self):
        """**两态**：两族源槽位数相同 ⇒ 运行期撞同一个槽块 ⇒ 导出期必须 Fatal。

        这是表改由 plan 生成之后 F6 唯一还能抓到的**真矛盾**（同 span 不同 base 已经不可能
        出现：那一行只有一个 base）。两族各写 51 个槽到同一段地址 ⇒ 后写的覆盖先写的。
        """
        Fatal = sys.modules[f"{PKG}.utils.format_utils"].Fatal
        blueprint_model = types.SimpleNamespace(
            keyname_mkey_dict={}, ordered_draw_obj_data_model_list=[]
        )
        exporter = ExportHIMI(blueprint_model)
        exporter.drawib_model_list = []
        # A 态：真实排布 ⇒ 不抛（走导出侧真调用点，没有替身）
        plan = types.SimpleNamespace(family_ranges=dict(self.REAL_RANGES), slot_count=343)
        exporter._himi_verify_base_table_against_plan(plan)
        # B 态：新角色里两族都恰好 64 槽 ⇒ 同 span 撞车
        clashed = {
            "aaaa0001": {"base": 0, "slots": 64, "own_block": True},
            "aaaa0002": {"base": 64, "slots": 64, "own_block": True},
        }
        plan_b = types.SimpleNamespace(family_ranges=clashed, slot_count=128)
        with self.assertRaises(Fatal) as caught:
            exporter._himi_verify_base_table_against_plan(plan_b)
        self.assertIn("重复的源槽位数 64", str(caught.exception))
        # 还原 ⇒ 又必须不抛（两态都能反复观测）
        exporter._himi_verify_base_table_against_plan(plan)

    def test_foreign_reference_table_against_this_role_is_fatal_TWO_STATE(self):
        """审计一张**外来的**表（参考角色那份）对上本角色排布 ⇒ 真矛盾（这正是历史 bug）。

        导出路径已经不可能产出这种组合（表由 plan 生成）；这条守的是审计函数本身：给它一张
        陈旧表时它必须真判红（否则 F6 是恒真的摆设）。
        """
        stale = list(_himi_exporter.MERGED_ATTACH_BASE_TABLE_SAMPLE_LINES)
        this_role = {
            "077983c9": {"base": 0, "slots": 51, "own_block": True},
            "09c6e58a": {"base": 51, "slots": 165, "own_block": True},
            "1b62725d": {"base": 216, "slots": 1, "own_block": True},
        }
        audit = _himi_exporter.merged_attach_base_table_audit(
            this_role, 219, table_lines=stale
        )
        self.assertTrue(audit["problems"], "外部陈旧表 vs 本族排布必须报矛盾")
        self.assertTrue(
            any("077983c9" in line and "292" in line for line in audit["problems"]),
            audit["problems"],
        )
        # 同一张表对**参考排布**则必须干净（否则判据会把对的也判错）
        self.assertEqual(
            _himi_exporter.merged_attach_base_table_audit(
                self.REAL_RANGES, 343, table_lines=stale
            )["problems"],
            [],
        )

    def test_new_span_is_a_loud_notice_not_a_fatal(self):
        """**两态（F6 的第二态）**：外来表里没有 plan 的新 span ⇒ 诊断（不是 Fatal）。

        为什么不是 Fatal：运行期的 base/span 来自身份资源（本次 plan 生成），「表不认识这一族」
        不等于「一根骨都不写」；导出侧的表本来就是按 plan 生成的，换角色不会命中这条。
        """
        Fatal = sys.modules[f"{PKG}.utils.format_utils"].Fatal
        ranges = {family: dict(info) for family, info in self.REAL_RANGES.items()}
        ranges["9999abcd"] = {"base": 343, "slots": 40, "own_block": True}
        audit = _himi_exporter.merged_attach_base_table_audit(
            ranges, 383, table_lines=_himi_exporter.MERGED_ATTACH_BASE_TABLE_SAMPLE_LINES
        )
        self.assertEqual(audit["problems"], [], "换个角色不得被当成矛盾")
        self.assertTrue(
            any("9999abcd" in line for line in audit["notices"]),
            f"必须写出诊断（不得静默）：{audit['notices']}",
        )
        # 导出侧：表按 plan 生成 ⇒ 这个新族**有**自己的一行，gate 不抛也不该报缺
        plan = types.SimpleNamespace(family_ranges=ranges, slot_count=383)
        blueprint_model = types.SimpleNamespace(
            keyname_mkey_dict={}, ordered_draw_obj_data_model_list=[]
        )
        exporter = ExportHIMI(blueprint_model)
        exporter.drawib_model_list = []
        exporter._himi_verify_base_table_against_plan(plan)  # 诊断而不中止
        self.assertFalse(isinstance(None, Fatal))  # 走到这里就说明没抛


class HIMIBindPositionGateTests(unittest.TestCase):
    """【t74】H-A 门：产物里"位置被写成别行的位置"必须被判出来（导出段拉丝的直接成因）。

    判据照搬 t72：逐元素对应 u→v、稳健相似拟合、离群行 + 「位置精确等于另一在用行」⇒ 判红并指名到行。
    两态：正常产物（B = 源点的相似像）⇒ 绿；把若干行的位置换成另一在用行的 ⇒ 红；还原 ⇒ 绿。
    """

    @staticmethod
    def _cloud(count=200, seed=7):
        import math
        import numpy
        generator = numpy.random.default_rng(seed)
        A = generator.normal(size=(count, 3)).astype(numpy.float64)
        theta = 0.7
        R = numpy.array([[math.cos(theta), -math.sin(theta), 0.0],
                         [math.sin(theta), math.cos(theta), 0.0],
                         [0.0, 0.0, 1.0]])
        scale, shift = 1.6, numpy.array([0.25, -0.5, 0.1])
        return A, scale * (A @ R.T) + shift, scale

    def test_written_from_another_row_two_state(self):
        import numpy
        A, B, scale = self._cloud()
        good = _runtime.bind_position_mismatch_problems(A, B)
        self.assertTrue(good["ok"], good["problems"])
        self.assertLess(good["ledger"]["rms"], 1e-12, "正常产物必须是 float 精度级")
        self.assertAlmostEqual(good["ledger"]["scale"], scale, places=6)

        victims = [7, 19, 33]
        bad = B.copy()
        for index, victim in enumerate(victims):
            bad[victim] = B[index]           # 别行的位置被写进来（t72 实测距离精确为 0）
        red = _runtime.bind_position_mismatch_problems(A, bad)
        self.assertFalse(red["ok"], "H-A 必须判红")
        hit = {item["row"] for item in red["written_from_rows"]}
        self.assertTrue(set(victims).issubset(hit), (hit, victims))
        for item in red["written_from_rows"][:1]:
            self.assertLessEqual(item["distance"], _runtime.BIND_POSITION_EXACT_TOLERANCE)
            self.assertGreater(item["residual"], red["ledger"]["threshold"])
        self.assertIn("第 7 行", red["problems"][0])
        self.assertTrue(_runtime.bind_position_mismatch_problems(A, B)["ok"], "还原必须回绿")

    def test_gate_is_wired_and_degrades_instead_of_fatal(self):
        """接点 + 兜底原则：判红 ⇒ 降级为普通导出（**不抛 Fatal**），并写进产物注释块。"""
        import inspect
        source = inspect.getsource(ExportHIMI.generate_buffer_files)
        self.assertIn("_himi_scan_bind_positions", source)
        gate = inspect.getsource(ExportHIMI._himi_scan_bind_positions)
        for needle in ("scan_bind_positions", "_himi_merged_attach_active",
                       "_himi_degrade_to_ordinary"):
            self.assertIn(needle, gate, f"H-A 门实现必须含 {needle}")
        self.assertNotIn("raise Fatal", gate, "兜底原则：H-A 判红不得中止导出")
        diagnostics = inspect.getsource(ExportHIMI.himi_diagnostic_lines)
        self.assertIn("H-A", diagnostics, "H-A 账目/警告必须进产物 ini 注释块")


class HIMIVertexGroupNameResolutionTests(unittest.TestCase):
    """【t66 根治】BLENDINDICES 的槽号必须**按组名**解析，而不是取对象组列表序号。

    用户实测的现场（`LOD0.b35cfc6c-8052-0`）：顶点组 #275 的名字是 `279`、#279 的名字是 `275`
    ⇒ 老实现写 `g.group`（列表序号）⇒ "刷 275"的顶点实际读到 279 的矩阵（左腿往前、它往后）。
    本用例把根治修法（`VertexGroupUtils.resolve_group_slot` + `get_blendweights_blendindices_v3`
    的 `vertex_group_names`）钉在单测层，并给出**两态**：无分叉 ⇒ 与旧行为逐值相同；
    有分叉 ⇒ 用组名（正确槽），而**不是**列表序号。
    """

    @staticmethod
    def _fake_mesh(group_rows):
        """``group_rows`` = 每顶点的 ``[(list_index, weight), …]`` ⇒ 造一个最小 mesh 替身。

        ``loops`` 只需支持 ``len()`` 与 ``foreach_get("vertex_index", array)``（v3 只用它填循环索引）。
        """
        class _Group:
            def __init__(self, group, weight):
                self.group = group
                self.weight = weight

        class _Vertex:
            def __init__(self, groups):
                self.groups = [_Group(g, w) for g, w in groups]

        class _Loops:
            def __init__(self, count):
                self._count = count

            def __len__(self):
                return self._count

            def foreach_get(self, _attr, array):
                for index in range(min(len(array), self._count)):
                    array[index] = index

        class _Mesh:
            def __init__(self, vertices):
                self.vertices = [_Vertex(row) for row in vertices]
                self.loops = _Loops(len(vertices))

        return _Mesh(group_rows)

    def test_resolve_group_slot_name_wins_TWO_STATE(self):
        """名字是十进制 ⇒ 用名字；不是（或没有名单）⇒ 回退序号（旧行为）。"""
        names = ["0", "1", "279", "275"]          # 故意让 #2 名 279、#3 名 275（用户现场形态）
        self.assertEqual(_vertexgroup.VertexGroupUtils.resolve_group_slot(names, 3), 275)
        self.assertEqual(_vertexgroup.VertexGroupUtils.resolve_group_slot(names, 2), 279)
        # 无分叉：名 == str(序号) ⇒ 与旧行为逐值相同
        clean = [str(i) for i in range(8)]
        for index in range(8):
            self.assertEqual(
                _vertexgroup.VertexGroupUtils.resolve_group_slot(clean, index), index
            )
        # 没有名单 / 非十进制名 ⇒ 回退序号（旧行为，保证其它游戏路径不变）
        self.assertEqual(_vertexgroup.VertexGroupUtils.resolve_group_slot(None, 5), 5)
        self.assertEqual(
            _vertexgroup.VertexGroupUtils.resolve_group_slot(["Bone", "Bone.001"], 1), 1
        )

    def test_blendindices_use_group_name_not_list_index(self):
        """端到端（最小替身）：刷"275"的顶点在按名解析下写 **275**，旧口径会写 **3**（列表序号）。"""
        names = ["0", "1", "279", "275"]
        mesh = self._fake_mesh([[(3, 1.0)]])          # 该顶点只挂在 #3（名 `275`）上
        _weights, indices = _vertexgroup.VertexGroupUtils.get_blendweights_blendindices_v3(
            mesh=mesh, normalize_weights=False, vertex_group_names=names
        )
        got = int(list(indices.values())[0][0][0])
        self.assertEqual(got, 275, "按名解析 ⇒ 槽号 = 组名 275")
        _weights_old, indices_old = _vertexgroup.VertexGroupUtils.get_blendweights_blendindices_v3(
            mesh=mesh, normalize_weights=False
        )
        old = int(list(indices_old.values())[0][0][0])
        self.assertEqual(old, 3, "不传名单 ⇒ 旧行为（列表序号 3）—— 这正是会绑错骨的那条路")
        self.assertNotEqual(got, old, "两态必须可分辨（否则本用例是恒真的）")

    def test_clean_names_are_byte_equivalent_to_old_behaviour(self):
        """回归不变量（单测层）：名 == str(序号) 时，新口径与旧口径**逐值相同**。"""
        names = [str(i) for i in range(12)]
        mesh = self._fake_mesh([[(i, 1.0 / (i + 1)) for i in range(4)],
                                [(7, 0.5), (9, 0.5)]])
        _w_new, i_new = _vertexgroup.VertexGroupUtils.get_blendweights_blendindices_v3(
            mesh=mesh, normalize_weights=False, vertex_group_names=names
        )
        _w_old, i_old = _vertexgroup.VertexGroupUtils.get_blendweights_blendindices_v3(
            mesh=mesh, normalize_weights=False
        )
        self.assertEqual(
            [list(row) for row in list(i_new.values())[0]],
            [list(row) for row in list(i_old.values())[0]],
        )


class HIMILocalZeroSlotMappingTests(unittest.TestCase):
    """【局部 0 ⇒ 族基址】把「非宿主族的局部下标 0」钉死在族基址上。

    现场报障的形态是「合并 Blend 里同一个局部组号 0 有时写成 base、有时写成 0」。
    resolve_group_slot 按组名取值，组名恒为全局槽的十进制写法 ⇒ local 0 与
    local k 走**同一条规则**（int(vg.name) = base + local），不存在「0 被当成没有骨骼」。
    本组用例把这条规则钉在单测层，并给出两态（不传名单 = 老口径，正是会把 0 写成 0 的那条路）。
    """

    #: 游戏侧固定槽位布局（合并骨架 343 槽）：(族, base, span)。
    FAMILIES = (
        ("2c706fbd", 0, 184),
        ("694025c6", 184, 18),
        ("70a4da3e", 202, 71),
        ("b35cfc6c", 273, 19),
        ("def0c0ae", 292, 51),
    )

    _fake_mesh = staticmethod(HIMIVertexGroupNameResolutionTests._fake_mesh)

    def _first_row(self, mesh, names):
        _weights, indices = _vertexgroup.VertexGroupUtils.get_blendweights_blendindices_v3(
            mesh=mesh, normalize_weights=False, vertex_group_names=names
        )
        return [int(value) for value in list(indices.values())[0][0]]

    def test_local_zero_maps_to_family_base_TWO_STATE(self):
        """非宿主族局部 0：按名 ⇒ base；不传名单（老口径）⇒ 0（= 报障形态）。"""
        mesh = self._fake_mesh([[(0, 1.0)]])
        self.assertEqual(
            self._first_row(mesh, ["202"])[0], 202, "局部 0 必须落到族基址 202"
        )
        self.assertEqual(
            self._first_row(mesh, None)[0],
            0,
            "不传名单 = 列表序号口径 ⇒ 0（这正是会被读成宿主槽 0 的那条路）",
        )

    def test_every_family_slice_maps_base_plus_local_including_zero(self):
        """整段族切片：local k ⇒ base + k（k = 0 与 k = span-1 同规则），宿主族基址 0 也成立。"""
        for family, base, span in self.FAMILIES:
            names = [str(base + local) for local in range(span)]
            mesh = self._fake_mesh([[(local, 1.0)] for local in range(span)])
            _weights, indices = _vertexgroup.VertexGroupUtils.get_blendweights_blendindices_v3(
                mesh=mesh, normalize_weights=False, vertex_group_names=names
            )
            rows = list(indices.values())[0]
            got = [int(rows[local][0]) for local in range(span)]
            self.assertEqual(
                got, [base + local for local in range(span)], family + ": local k ⇒ base + k"
            )
            self.assertEqual(got[0], base, family + ": 局部 0 ⇒ 族基址")
            self.assertEqual(
                len(set(got)), span, family + ": 不得折叠成同一槽（" + str(span) + " 个槽互不相同）"
            )

    def test_host_family_is_byte_equivalent_to_old_behaviour(self):
        """宿主族（base = 0、组名 = str(序号)）⇒ 与老口径逐值相同（本次改动零影响）。"""
        names = [str(index) for index in range(6)]
        mesh = self._fake_mesh(
            [[(0, 1.0)], [(1, 0.25), (5, 0.75)], [(3, 0.5), (4, 0.5)]]
        )
        new_rows = list(
            _vertexgroup.VertexGroupUtils.get_blendweights_blendindices_v3(
                mesh=mesh, normalize_weights=False, vertex_group_names=names
            )[1].values()
        )[0]
        old_rows = list(
            _vertexgroup.VertexGroupUtils.get_blendweights_blendindices_v3(
                mesh=mesh, normalize_weights=False
            )[1].values()
        )[0]
        self.assertEqual(
            [[int(value) for value in row] for row in new_rows],
            [[int(value) for value in row] for row in old_rows],
        )

    def test_out_of_list_group_index_keeps_legacy_strategy(self):
        """越界下标（超出名单长度 / 名单外的组）仍按既有策略回退序号，不抛错、不静默归零。"""
        resolve = _vertexgroup.VertexGroupUtils.resolve_group_slot
        self.assertEqual(resolve(["202", "203"], 7), 7, "名单长度外 ⇒ 回退序号")
        self.assertEqual(resolve([], 0), 0, "空名单 ⇒ 回退序号（不是缺失值 0）")
        self.assertEqual(resolve(None, 273), 273, "没有名单 ⇒ 回退序号")
        self.assertEqual(resolve(["202", " 203 "], 1), 203, "名字两端空白不改变解析")
        self.assertEqual(
            resolve(["202", "Bone"], 1), 1, "非十进制名 ⇒ 回退序号（其它游戏的形态）"
        )


class HIMIVertexGroupNameContractTests(unittest.TestCase):
    """【t111】导出只认组名；导出前不再扫场景网格的「索引 == 名字」。

    被删门控的事故（m02503）：它按**工作空间 submesh 名**去场景里取物体（LOD0.2c706fbd-5259-55380 等 12 个名字），而导出真正的几何来自 DrawCall 指向的 …_copy 物体、写 BLENDINDICES 的是 TEMP_SUBMESH_MERGED_* 临时合并对象 ⇒ 检查集与写入集无关，蓝图里根本没连的遗留原版网格把导出整死。
    """

    def test_early_gate_and_its_helpers_are_gone(self):
        self.assertFalse(
            hasattr(ExportHIMI, "_himi_verify_vertex_group_index_names"),
            "早期门控已删除：它查的不是导出读取的物体（作用域错位）",
        )
        for name in (
            "vertex_group_index_name_problems",
            "blender_vertex_group_index_name_problems",
        ):
            self.assertFalse(hasattr(_skeleton, name), name + " 已随门控删除")

    def test_slot_comes_from_group_name_not_index_TWO_STATE(self):
        """索引与名字分叉时取**名字**（真对象形态）；非数字名 fail-closed。

        这就是删掉「索引 == 名字」门控的依据：分叉本身不再是错误，只有非数字名才是。
        """
        Fatal = sys.modules[f"{PKG}.utils.format_utils"].Fatal
        resolve = _vertexgroup.VertexGroupUtils.resolve_group_slot
        # A 态：索引 1 / 名字 273 分叉 ⇒ 取名字（不是索引），不抛
        self.assertEqual(resolve(["184", "273"], 1, True), 273)
        self.assertEqual(resolve(["0", "216"], 1, True), 216)
        # B 态：非数字名 ⇒ strict 下 Fatal（写盘点的存活护栏）
        with self.assertRaises(Fatal) as caught:
            resolve(["Bone"], 0, True)
        self.assertIn("不是纯数字", str(caught.exception))
        # C 态：没有名单 ⇒ strict 下 Fatal（不许退回取序号）
        with self.assertRaises(Fatal):
            resolve(None, 0, True)


class HIMIDeformReplayUnconditionalTests(unittest.TestCase):
    """【F1/F2】变形重放必须无条件：任何条件行都是坏产物（s3 那种写法永不得回流）。"""

    def test_generated_block_passes_and_s3_style_guard_is_rejected_TWO_STATE(self):
        block = [
            "; aaaa1111",
            "[TextureOverride_VB_aaaa1111_0_Blend]",
            "hash = 4349c385",
            "vb1 = Resourceaaaa1111Blend",
            "ResourceHI3PaletteFam_aaaa1111 = copy vs-t0 unless_null",
            "run = CustomShaderHI3MergedSkeletonAttach_aaaa1111",
            "vs-t0 = ResourceHI3MergedSkeleton_role_x",
            "handling = skip",
            "draw = 31054, 0",
        ]
        self.assertEqual(_himi_exporter.deform_replay_block_problems(block), [], "正例必须通过")
        # B 态①：s3 诊断态的写法（`$seen` 守卫包住 draw）⇒ 必须被抓到
        guarded = block[:8] + ["if $seen_2c706fbd >= 1", "draw = 31054, 0", "endif"]
        problems = _himi_exporter.deform_replay_block_problems(guarded)
        self.assertTrue(problems, "条件包裹 draw 必须报错")
        self.assertTrue(any("条件行" in item for item in problems), problems)
        # B 态②：整块被守卫包住（含 handling = skip）
        wrapped = block[:7] + ["if $seen >= 1"] + block[7:] + ["endif"]
        self.assertTrue(_himi_exporter.deform_replay_block_problems(wrapped))
        # B 态③：draw 排在 handling = skip 之前 / 多笔 draw / 缺 draw
        self.assertTrue(
            _himi_exporter.deform_replay_block_problems(
                block[:7] + ["draw = 31054, 0", "handling = skip"]
            )
        )
        self.assertTrue(
            _himi_exporter.deform_replay_block_problems(block + ["draw = 3, 0"])
        )
        self.assertTrue(
            _himi_exporter.deform_replay_block_problems([line for line in block if not line.startswith("draw")])
        )
        self.assertEqual(_himi_exporter.deform_replay_block_problems([]), [], "空块不判（非本链）")


class HIMIMergedDeformKeyTests(unittest.TestCase):
    """【t12 追加】合并 deform 的触发键不得 1→多（否则重放可能落在别的族的变形段上）。"""

    def test_generated_key_is_one_family_one_hash_TWO_STATE(self):
        # 【t18 / T5-F3 修正】五族的真实 `CategoryHash.Blend` —— 取值来源是**产物 ini**：
        # `.dbg/export_t4_merged_only/嗨♪爱愿妖精♥.ini` 的
        # `[TextureOverride_VB_<fam>_<fam>_Blend] hash = …`（与用户模组 ini 同值）。
        # 此前 694025c6 误写 `c6bf68c0`（只是夹具里的假值）；真实值是 `3957eebb`（t5 复核）。
        rebinds = [
            {"family": fam, "draw_ib": fam, "category_hash": value}
            for fam, value in (
                ("2c706fbd", "e1cae779"),
                ("694025c6", "3957eebb"),
                ("70a4da3e", "4349c385"),
                ("b35cfc6c", "b7824ab2"),
                ("def0c0ae", "71837027"),
            )
        ]
        self.assertEqual(
            _himi_exporter.merged_deform_key_problems(rebinds), [], "真实排布（一族一哈希）必须通过"
        )
        # B 态：两个族共用同一个 Blend 键 ⇒ 必须报错（这正是 t12 的候选根因形态）
        shared = [dict(item) for item in rebinds]
        shared[1]["category_hash"] = shared[2]["category_hash"]
        problems = _himi_exporter.merged_deform_key_problems(shared)
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("694025c6", problems[0])
        self.assertIn("70a4da3e", problems[0])
        self.assertIn("触发键", problems[0])
        # 单族导出（合法路径）不判 1→多
        self.assertEqual(
            _himi_exporter.merged_deform_key_problems([rebinds[2]]), []
        )
        # 缺 category_hash 的记录不参与判定（不得假阳性）
        self.assertEqual(
            _himi_exporter.merged_deform_key_problems(
                [{"family": "a"}, {"family": "b", "category_hash": ""}]
            ),
            [],
        )

    def test_exporter_gate_reports_shared_key_without_blocking(self):
        """K4 是**诊断**（t13：4 帧内无实际歧义 ⇒ 不为杞人忧天写守卫/不阻断导出）。"""
        blueprint_model = types.SimpleNamespace(
            keyname_mkey_dict={}, ordered_draw_obj_data_model_list=[]
        )
        exporter = ExportHIMI(blueprint_model)
        exporter.drawib_model_list = []
        exporter.himi_merged_runtime_plan = lambda: types.SimpleNamespace(
            rebinds=[
                {"family": "aaaa", "category_hash": "4349c385"},
                {"family": "bbbb", "category_hash": "4349c385"},
            ],
            contract_snapshot=lambda **kw: {
                "checkbox_enabled": True,
                "parts_with_data": 0,
                "component_count": 0,
                "skip_reasons": {},
                "gate_rejected_parts": {},
                "diagnostics": [],
            },
            family_ranges={},
            slot_count=0,
            ok=False,
            message="",
        )
        exporter._himi_verify_base_table_against_plan = lambda plan: None
        exporter._himi_verify_deform_draw_structure = lambda plan: None
        # 不抛 = 不阻断（K4 只诊断）；诊断文本由 merged_deform_key_problems 直接断言
        decision = exporter._himi_enforce_contract()
        self.assertEqual(decision.get("level"), "notice")
        self.assertEqual(
            len(_himi_exporter.merged_deform_key_problems(exporter.himi_merged_runtime_plan().rebinds)),
            1,
        )

    def test_deform_draw_structure_requires_pointlist_TWO_STATE(self):
        """【K2】Position/Blend 必须是 pointlist；texcoord 的 trianglelist 不得误判。"""
        good = {
            "CategoryBufferList": [
                {
                    "FileName": "x-Position.buf",
                    "D3D11ElementList": [
                        {"Category": "Position", "ExtractTechnique": "pointlist"},
                        {"Category": "Position", "ExtractTechnique": "pointlist"},
                    ],
                },
                {
                    "FileName": "x-Blend.buf",
                    "D3D11ElementList": [
                        {"Category": "Blend", "ExtractTechnique": "pointlist"},
                    ],
                },
                {
                    "FileName": "x-Texcoord.buf",
                    "D3D11ElementList": [
                        {"Category": "Texcoord", "ExtractTechnique": "trianglelist"},
                    ],
                },
            ]
        }
        self.assertEqual(
            _himi_exporter.deform_draw_structure_problems(good, unique_str="LOD0.aaaa-1-0"),
            [],
            "真实 json 形态（Position/Blend=pointlist，Texcoord=trianglelist）必须通过",
        )
        broken = {
            "CategoryBufferList": [
                {
                    "FileName": "x-Position.buf",
                    "D3D11ElementList": [
                        {"Category": "Position", "ExtractTechnique": "trianglelist"},
                    ],
                },
                {
                    "FileName": "x-Blend.buf",
                    "D3D11ElementList": [
                        {"Category": "Blend", "ExtractTechnique": "pointlist"},
                    ],
                },
            ]
        }
        problems = _himi_exporter.deform_draw_structure_problems(broken, unique_str="LOD0.aaaa-1-0")
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("pointlist", problems[0])
        self.assertIn("LOD0.aaaa-1-0", problems[0])
        # 缺 json ⇒ unavailable，不阻断
        self.assertEqual(_himi_exporter.deform_draw_structure_problems({}), [])


# --------------------------------------------------------------------------- #
# F. t18：修 t5 的四个 finding（K2 空转 / 诊断不落地 / span=0 语义分歧 / 夹具写错）
# --------------------------------------------------------------------------- #


class HIMIK2GateWiringTests(unittest.TestCase):
    """【T5-F2】K2 门必须**真的做判断**（原来因 plan 缺 `unique_str` 而完全空转）。"""

    GAMETYPE = "TYPE_GPU_P12_N12_C4_TA16_BW16_BI16_T8_"

    @staticmethod
    def _pointlist_buffer(category="Position", technique="pointlist"):
        return [
            {
                "FileName": f"x-{category}.buf",
                "D3D11ElementList": [
                    {"Category": category, "ExtractTechnique": technique},
                ],
            }
        ]

    def _workspace(self, root, rows):
        """root/LOD0/<bare>/<gametype>/<bare>.json（K2 门只读 json，不读 dump）。"""
        names = []
        import_map = {}
        for index, buffers in enumerate(rows):
            bare = f"aaaa-{index}-0"
            folder = os.path.join(root, "LOD0", bare, self.GAMETYPE)
            os.makedirs(folder, exist_ok=True)
            with io.open(os.path.join(folder, f"{bare}.json"), "w", encoding="utf-8") as handle:
                json.dump({"CategoryBufferList": buffers}, handle, ensure_ascii=False)
            names.append(f"LOD0.{bare}")
            import_map[f"LOD0.{bare}"] = self.GAMETYPE
        with io.open(os.path.join(root, "Import.json"), "w", encoding="utf-8") as handle:
            json.dump(import_map, handle, ensure_ascii=False)
        return names

    def _exporter(self, workspace, rebinds):
        blueprint_model = types.SimpleNamespace(
            keyname_mkey_dict={}, ordered_draw_obj_data_model_list=[]
        )
        exporter = ExportHIMI(blueprint_model)
        exporter.drawib_model_list = []
        exporter.himi_merged_runtime_plan = lambda: types.SimpleNamespace(rebinds=rebinds)
        return exporter

    def test_gate_actually_judges_every_record_TWO_STATE(self):
        """**两态**：① 真实形状的 plan（带 `unique_str`）⇒ 账目全是非零、逐条判过；
        ② 某部件 json 的通道改坏 ⇒ `Fatal`。另证「缺 `unique_str` 会被单独计数并警告」
        （= t5 指出的「`unavailable` 恒为 0 是空转副产物」已修：现在每个计数都有意义）。
        """
        Fatal = sys.modules[f"{PKG}.utils.format_utils"].Fatal
        good = [
            self._pointlist_buffer("Position")
            + self._pointlist_buffer("Blend")
            + self._pointlist_buffer("Texcoord", "trianglelist")
            for _ in range(3)
        ]
        with tempfile.TemporaryDirectory(prefix="himi_k2_") as root:
            names = self._workspace(root, good)
            old_workspace = _GLOBAL_STATE.get("workspace")
            _GLOBAL_STATE["workspace"] = root
            try:
                rebinds = [{"bare": "aaaa-%d-0" % i, "unique_str": n} for i, n in enumerate(names)]
                exporter = self._exporter(root, rebinds)
                warnings: list = []
                exporter._himi_notify = lambda detail, warning=False: warnings.append(
                    (bool(warning), str(detail))
                )
                # ---- A 态：3 条记录逐条判过、3 个部件读到 json 并判定、零未核对 ----
                exporter._himi_verify_deform_draw_structure(types.SimpleNamespace(rebinds=rebinds))
                result = exporter.himi_deform_structure_result()
                self.assertEqual(result["judged"], 3, result)
                self.assertEqual(result["checked"], 3, result)
                self.assertEqual(result["unavailable"], 0, result)
                self.assertEqual(result["unjudgeable"], 0, result)
                self.assertEqual(result["missing_identity"], 0, result)
                self.assertEqual(result["problems"], [], result)
                self.assertIn("判过 3 条记录", result["summary"])
                self.assertIn("判定 3 个部件", result["summary"])
                self.assertEqual(warnings, [], "全绿时不应产生警告")

                # ---- B 态：把其中一个部件的 Position 改成 trianglelist ⇒ Fatal 且指名 ----
                folder = os.path.join(root, "LOD0", "aaaa-1-0", self.GAMETYPE)
                with io.open(os.path.join(folder, "aaaa-1-0.json"), "w", encoding="utf-8") as handle:
                    json.dump(
                        {
                            "CategoryBufferList": self._pointlist_buffer(
                                "Position", "trianglelist"
                            )
                            + self._pointlist_buffer("Blend")
                        },
                        handle,
                        ensure_ascii=False,
                    )
                with self.assertRaises(Fatal) as caught:
                    exporter._himi_verify_deform_draw_structure(
                        types.SimpleNamespace(rebinds=rebinds)
                    )
                self.assertIn("aaaa-1-0", str(caught.exception))
                self.assertIn("pointlist", str(caught.exception))
                # 还原 ⇒ 又不抛（两态都能反复观测）
                with io.open(os.path.join(folder, "aaaa-1-0.json"), "w", encoding="utf-8") as handle:
                    json.dump({"CategoryBufferList": good[1]}, handle, ensure_ascii=False)
                exporter._himi_verify_deform_draw_structure(types.SimpleNamespace(rebinds=rebinds))
                self.assertEqual(exporter.himi_deform_structure_result()["problems"], [])

                # ---- C 态：记录缺 unique_str（= 修复前的真实形状）⇒ 单独计数 + 大声警告 ----
                warnings.clear()
                blind = [{"bare": "aaaa-0-0"}, {"bare": "aaaa-1-0"}]
                exporter._himi_verify_deform_draw_structure(
                    types.SimpleNamespace(rebinds=blind)
                )
                result = exporter.himi_deform_structure_result()
                self.assertEqual(result["judged"], 2, "每条记录都要进账（不再静默 continue）")
                self.assertEqual(result["checked"], 0, result)
                self.assertEqual(result["missing_identity"], 2, result)
                self.assertTrue(
                    any(flag and "unique_str" in text for flag, text in warnings),
                    f"缺身份必须大声警告（不再是 0 条通知）：{warnings}",
                )

                # ---- D 态：json 里没有可判元素 ⇒ 不算「通过」而是 unjudgeable ----
                warnings.clear()
                folder = os.path.join(root, "LOD0", "aaaa-2-0", self.GAMETYPE)
                with io.open(os.path.join(folder, "aaaa-2-0.json"), "w", encoding="utf-8") as handle:
                    json.dump({"CategoryBufferList": []}, handle, ensure_ascii=False)
                exporter._himi_verify_deform_draw_structure(
                    types.SimpleNamespace(rebinds=[rebinds[2]])
                )
                result = exporter.himi_deform_structure_result()
                self.assertEqual((result["checked"], result["unjudgeable"]), (0, 1), result)
                self.assertTrue(
                    any(flag and "没有 Position/Blend" in text for flag, text in warnings),
                    f"不可判必须说出来：{warnings}",
                )
            finally:
                _GLOBAL_STATE["workspace"] = old_workspace

    def test_real_plan_carries_unique_str(self):
        """计划侧必须带 `unique_str`（否则上面那道门又会退化成空转）—— 直接盯计划构建点。"""
        source = (REPO_ROOT / RUNTIME_REL).read_text(encoding="utf-8")
        self.assertIn(
            '"unique_str": part.unique_str,',
            source,
            "plan_from_metadata 的 rebind 字典必须带 unique_str（t18/T5-F2 的实质修法）",
        )


    def test_zero_rebind_records_is_not_reported_as_pass_TWO_STATE(self):
        """【t20 / T19-F1】**空转不得报成功**：`judged == 0` ⇒ 判词是「未核对（无 rebind 记录）」。

        形状与 K2 的原病相同（门没被喂数据却给出绿结论）；虽然 `rebinds` 为空在当前路径上
        不可达，但一旦有人把计划侧改空、或把门接错，这里必须**先说没核对**而不是说通过。
        """
        blueprint_model = types.SimpleNamespace(
            keyname_mkey_dict={}, ordered_draw_obj_data_model_list=[]
        )
        exporter = ExportHIMI(blueprint_model)
        exporter.drawib_model_list = []
        # 【t85】本用例测的是**注释块内容**（判词必须跟随账目），与「哪种导出模式」无关；
        # 诊断块现在只在**合并路径激活**时产出 ⇒ 显式声明走合并路径。
        # （「关闭该特性 ⇒ 产物 ini 一个字节都不写」由
        #   `test_no_merged_diagnostics_in_ordinary_export_TWO_STATE` 单独覆盖。）
        exporter._himi_merged_attach_active = lambda: True
        # A 态：没有任何 rebind 记录 ⇒ 判词必须不是「通过」
        exporter._himi_verify_deform_draw_structure(types.SimpleNamespace(rebinds=[]))
        result = exporter.himi_deform_structure_result()
        self.assertEqual(result["judged"], 0, result)
        self.assertNotEqual(result["verdict"], "通过", result)
        self.assertIn("未核对", result["verdict"])
        self.assertIn("未核对", result["summary"])
        # 产物诊断块里的图标也必须跟着判词（不能写「通过」）
        builder = M_IniBuilder()
        exporter._himi_emit_diagnostic_comments(builder)
        text = "\n".join(_builder_lines(builder))
        self.assertIn("未核对（K2 变形 pass 结构）", text)
        self.assertNotIn("通过（K2 变形 pass 结构）", text)
        # B 态（对照）：给它一条 rebind（缺身份 ⇒ 只警告不阻断）⇒ 仍然不是「通过」
        exporter_b = ExportHIMI(blueprint_model)
        exporter_b.drawib_model_list = []
        exporter_b._himi_notify = lambda detail, warning=False: None
        exporter_b._himi_verify_deform_draw_structure(
            types.SimpleNamespace(rebinds=[{"bare": "aaaa-0-0"}])
        )
        result_b = exporter_b.himi_deform_structure_result()
        self.assertEqual(result_b["judged"], 1, result_b)
        self.assertEqual(result_b["missing_identity"], 1, result_b)
        self.assertNotEqual(result_b["verdict"], "通过", result_b)

class HIMIDiagnosticVisibilityTests(unittest.TestCase):
    """【T5-F5】F6/K2/K4 的诊断必须落在用户**一定会看到**的地方（产物 ini 注释块）。"""

    def _exporter(self):
        blueprint_model = types.SimpleNamespace(
            keyname_mkey_dict={}, ordered_draw_obj_data_model_list=[]
        )
        exporter = ExportHIMI(blueprint_model)
        exporter.drawib_model_list = []
        # 【t85】诊断块只在**合并路径激活**时产出（普通导出一个字节都不写）⇒ 本组用例测的是
        # 块的内容与可见性，显式把谓词置真；「普通模式不产出」另有用例覆盖。
        exporter._himi_merged_attach_active = lambda: True
        return exporter

    def test_diagnostics_land_in_the_artifact_ini_TWO_STATE(self):
        """**两态**：干净 ⇒ 注释块里是「通过」；有 notice ⇒ 原样出现在产物 ini 里，且**不抛**。"""
        # ---- A 态（干净）：K2 账目仍要留痕（正例也留痕 ⇒ 不能再用「没有该行」当检查过的证据）
        exporter = self._exporter()
        exporter._himi_deform_structure_result = {
            "judged": 12,
            "checked": 12,
            "unavailable": 0,
            "unjudgeable": 0,
            "missing_identity": 0,
            "problems": [],
            # 【t20 / T19-F1】判词必须是账目给出来的那一个（夹具不能再省略它，
            # 否则图标会（正确地）退化成「未核对」）。
            "verdict": "通过",
            "summary": "变形 pass 结构核对（K2）：判过 12 条记录、读到 json 并判定 12 个部件 ⇒ 通过",
        }
        builder = M_IniBuilder()
        exporter._himi_emit_diagnostic_comments(builder)
        lines = [line for line in "\n".join(_builder_lines(builder)).splitlines() if line.strip()]
        self.assertTrue(
            any("HIMI 合并骨架诊断" in line for line in lines), lines
        )
        self.assertTrue(
            any("通过（K2 变形 pass 结构）" in line and "判过 12 条记录" in line for line in lines),
            lines,
        )
        self.assertFalse(
            any(line.startswith("; 警告") for line in lines), lines
        )

        # ---- B 态（有 notice）：基址表 notice 与 K4 触发键警告都必须出现在注释块里 ----
        exporter_b = self._exporter()
        exporter_b._himi_base_table_notices = ["基址表没有 9999abcd 的映射（本族槽位数 40、基址 900）"]
        exporter_b._himi_key_notices = ["合并 deform 的触发键 `hash = 4349c385` 被 2 个族共用（a, b）"]
        exporter_b._himi_deform_structure_result = {
            "judged": 12,
            "checked": 12,
            "unavailable": 0,
            "unjudgeable": 0,
            "missing_identity": 0,
            "problems": [],
            "verdict": "通过",
            "summary": "变形 pass 结构核对（K2）：判过 12 条记录 … ⇒ 通过",
        }
        builder_b = M_IniBuilder()
        exporter_b._himi_emit_diagnostic_comments(builder_b)
        text = "\n".join(_builder_lines(builder_b))
        self.assertIn("警告（F6 基址表）：基址表没有 9999abcd 的映射", text)
        self.assertIn("警告（K4 触发键）：合并 deform 的触发键", text)
        # 「不阻断」：emit 之后 exporter 仍然可用、没有任何 Fatal 被抛出
        self.assertFalse(exporter_b.himi_diagnostic_lines() == [])

    def test_no_merged_diagnostics_in_ordinary_export_TWO_STATE(self):
        """【t85】**两态**：合并路径未激活（= 开关关闭的普通导出）⇒ 产物 ini 里
        **一个字节的合并骨架内容都没有**（连注释块表头都不发）。

        用户实报：「为什么我在**不使用合并骨骼**的时候，配置表里面会有合并骨骼的生成？」
        —— 旧行为在这里**无条件**发整块（`; ===== HIMI 合并骨架诊断 … =====` 表头 +
        F6 基址表 5 行 + `out_of_range_refs` + K2 变形 pass 结构行），普通导出的 ini 因此
        带上了合并骨架内容。t85 把 emit 挂在与 `generate_unity_vs_config_ini` **同一个**
        谓词上（`_himi_merged_attach_active() or himi_degraded_to_ordinary()`）。

        规则：**普通导出永远是兜底** —— 这里只少一个注释块，几何/资源/其它段一字不改
        （B 态与 A 态用同一份账目，证明「空」不是因为没数据）。
        """
        account = {
            "judged": 0, "checked": 0, "unavailable": 0, "unjudgeable": 0,
            "missing_identity": 0, "problems": [],
            "verdict": "未核对（无 rebind 记录）",
            "summary": "变形 pass 结构核对（K2）：判过 0 条记录 ⇒ 未核对（无 rebind 记录）",
        }
        notice = ["基址表多出一行（槽位数 18 → 基址 184，标注 694025c6）"]

        # ---- A 态：普通导出（谓词 False）⇒ 全部模式零命中 ----
        exporter = self._exporter()
        exporter._himi_merged_attach_active = lambda: False   # 覆盖 _exporter 的置真
        exporter._himi_base_table_notices = list(notice)
        exporter._himi_deform_structure_result = dict(account)
        builder = M_IniBuilder()
        exporter._himi_emit_diagnostic_comments(builder)
        text = "\n".join(_builder_lines(builder))
        for token in ("HIMI 合并骨架诊断", "F6 基址表", "K2 变形", "合并骨架", "out_of_range_refs"):
            self.assertNotIn(token, text, text)

        # ---- B 态（对照）：同一份账目 + 谓词置真 ⇒ 内容照出 ----
        exporter_b = self._exporter()
        exporter_b._himi_base_table_notices = list(notice)
        exporter_b._himi_deform_structure_result = dict(account)
        builder_b = M_IniBuilder()
        exporter_b._himi_emit_diagnostic_comments(builder_b)
        text_b = "\n".join(_builder_lines(builder_b))
        self.assertIn("HIMI 合并骨架诊断", text_b)
        self.assertIn("警告（F6 基址表）", text_b)
        self.assertIn("未核对（K2 变形 pass 结构）", text_b)

        # ---- C 态：同一次导出只发一块（幂等），且注释行都以 ';' 开头（ini 安全）----
        builder_c = M_IniBuilder()
        exporter_b._himi_emit_diagnostic_comments(builder_c)
        self.assertEqual(
            _builder_lines(builder_c), [], "第二次调用必须不再追加（幂等）"
        )
        for line in _builder_lines(builder_b):
            if line.startswith("[") or line.startswith(";") or not line.strip():
                continue
            self.fail(f"诊断块里出现了非注释行（会污染 ini）：{line!r}")


class HIMIIdentitySpanZeroSemanticsTests(unittest.TestCase):
    """【T5-F1】`identity span == 0` 的语义必须两边统一（模型 ↔ 生成文本）。"""

    def test_span_zero_means_identity_unusable_on_both_sides_TWO_STATE(self):
        """**两态**：span=0 ⇒ 两处都当「身份不可用 ⇒ 退回冻结表」（此前模型全拒、CS 不拦）。

        传送门：`attach_write_target`（模型）与 `merged_attach_hlsl_text`（生成文本）必须
        对同一载荷给出同一结论；文本侧由 `merged_attach_hlsl_problems` 钉住 `> 0u`。
        """
        # ---- A 态：span > 0 ⇒ 身份权威 + 本族 span 截断（F5 的正常路径）
        normal = _runtime.attach_write_target(
            bone_index=5, source_slots=18, table_base=184, identity_base=184, identity_span=18,
            skeleton_slots=343,
        )
        self.assertEqual(normal, 189)
        # ---- B 态：span == 0 ⇒ **退回冻结表**（不是整族拒写），且无 span 上界
        zero = [
            _runtime.attach_write_target(
                bone_index=bone, source_slots=18, table_base=184, identity_base=184,
                identity_span=0, skeleton_slots=343,
            )
            for bone in range(18)
        ]
        self.assertEqual(zero, [184 + bone for bone in range(18)], "span=0 必须与身份缺失同路")
        # 与「身份完全缺失」逐值一致（= 今天的行为）
        missing = [
            _runtime.attach_write_target(
                bone_index=bone, source_slots=18, table_base=184, identity_base=-1,
                identity_span=-1, skeleton_slots=343,
            )
            for bone in range(18)
        ]
        self.assertEqual(zero, missing, "span=0 与身份缺失必须**逐值等价**")
        # ---- C 态：span=0 但表也不认识这一族 ⇒ 仍然拒写（fail-closed 不变）
        self.assertEqual(
            _runtime.attach_write_target(
                bone_index=0, source_slots=18, table_base=-1, identity_base=184,
                identity_span=0, skeleton_slots=343,
            ),
            -1,
        )
        # ---- 文本侧必须与模型同语义：`> 0u` 是「身份可用」的一部分
        text = _himi_exporter.merged_attach_hlsl_text()
        code = _himi_exporter._strip_hlsl_comments(text)
        self.assertIn("asuint(attach_range.y) > 0u", code)
        self.assertEqual(_himi_exporter.merged_attach_hlsl_problems(text), [])
        broken = text.replace("asuint(attach_range.y) > 0u", "asuint(attach_range.y) >= 0u")
        self.assertNotEqual(broken, text)
        self.assertNotEqual(
            _himi_exporter.merged_attach_hlsl_problems(broken),
            [],
            "丢掉 span>0 判据必须被抓住（否则模型与文本再次分叉）",
        )


class HIMIFamilyCalibrationTests(unittest.TestCase):
    """【t39 历史 / t78 方案 B】族级校准：**顶点侧不校准**，写槽矩阵 = ``X_f · P_f``。

    方案 B 之前的实现是「顶点侧 ``v' = Q·v`` + palette 侧 ``P' = P∘Q⁻¹``」：同族恒等，
    但形态键通道（δ 直接叠加到基态行）没有 Q ⇒ 运行期是 ``Q·v+δ`` 而不是 ``Q·(v+δ)``，
    非宿主族整块撕裂。本组用例因此全部改成「顶点缓冲逐字节不动」口径；下面这段历史对照
    仍保留（t39 当初的配对推导）。

    ⚠ 与任务书初稿的差异（**有证据**）：任务书写的是 ``P' = Q⁻¹·P`` + ``v' = Q·v``，
    但那对组合的左式 = ``Q⁻¹·P·Q·v``（相似变换），只有 P 与 Q 可交换才等于 ``P·v``
    ⇒ 实测偏差 0.774（≫ 1e-5，见 `.dbg/_t39_math_verify.py`）。本实现取
    ``P' = P∘Q`` + ``v' = Q⁻¹·v`` ⇒ ``P·Q·Q⁻¹·v = P·v`` **恒等**（本文件第一个用例证明）。
    """

    @staticmethod
    def _matrix(angle_deg, scale, translation, axis="z"):
        """3×4 行主序：绕 ``axis`` 旋转 + 缩放 + 平移。"""
        import math

        rad = math.radians(float(angle_deg))
        c, s = math.cos(rad) * float(scale), math.sin(rad) * float(scale)
        tx, ty, tz = translation
        if axis == "z":
            return [c, -s, 0.0, tx, s, c, 0.0, ty, 0.0, 0.0, float(scale), tz]
        return [float(scale), 0.0, 0.0, tx, 0.0, c, -s, ty, 0.0, s, c, tz]

    def test_mat3x4_algebra_roundtrip(self):
        q = self._matrix(37, 1.6, (0.4, -0.2, 0.7))
        q_inv = _runtime.mat3x4_inverse(q)
        identity = _runtime.mat3x4_compose(q, q_inv)
        for row in range(3):
            for col in range(3):
                expected = 1.0 if row == col else 0.0
                self.assertAlmostEqual(identity[row * 4 + col], expected, places=12)
            self.assertAlmostEqual(identity[row * 4 + 3], 0.0, places=12)
        point = (0.35, 0.90, -0.05)
        back = _runtime.mat3x4_apply(q_inv, _runtime.mat3x4_apply(q, point))
        for got, want in zip(back, point):
            self.assertAlmostEqual(got, want, places=12)
        direction = (0.1, 0.2, 0.97)
        rotated = _runtime.mat3x4_rotate(q, direction)
        self.assertAlmostEqual(
            sum(a * b for a, b in zip(rotated, rotated)),
            sum(a * b for a, b in zip(
                _runtime.mat3x4_rotate(q, direction), _runtime.mat3x4_rotate(q, direction))),
            places=9,
        )
        with self.assertRaises(_runtime.HIMIResourceDeclError):
            _runtime.mat3x4_inverse([0.0] * 12)

    def test_invariant_holds_for_the_implemented_pairing_only_TWO_STATE(self):
        """**两态**：本实现的配对恒等（≤1e-9），任务书初稿的配对偏差巨大。"""
        import random

        random.seed(7)
        worst_ok = 0.0
        # 初稿配对换成 **真实的退化数据** 才看得见巨大偏差：用旗标矩阵（构造性反例）
        flag = [
            1.0, 0.0, 0.0, 0.0,
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
        ]
        worst_bad = 0.0
        for _ in range(50):
            q = self._matrix(random.uniform(0, 360), random.uniform(1.1, 2.0),
                             (random.uniform(-3, 3), random.uniform(-3, 3), random.uniform(-1, 1)))
            p = self._matrix(random.uniform(0, 360), random.uniform(0.8, 1.4),
                             (random.uniform(-2, 2), random.uniform(-2, 2), random.uniform(-1, 1)))
            v = (random.uniform(-1, 1), random.uniform(-1, 1), random.uniform(0, 2))
            base = _runtime.mat3x4_apply(p, v)
            # 本实现：v' = Q⁻¹ v ；P' = P ∘ Q
            implemented = _runtime.mat3x4_apply(
                _runtime.mat3x4_compose(p, q), _runtime.mat3x4_apply(_runtime.mat3x4_inverse(q), v)
            )
            worst_ok = max(worst_ok, max(abs(a - b) for a, b in zip(base, implemented)))
            # 任务书初稿：v' = Q v ；P' = Q⁻¹ ∘ P  ⇒ 等于 Q⁻¹·P·Q·v
            draft = _runtime.mat3x4_apply(
                _runtime.mat3x4_compose(_runtime.mat3x4_inverse(q), p),
                _runtime.mat3x4_apply(q, v),
            )
            worst_bad = max(worst_bad, max(abs(a - b) for a, b in zip(base, draft)))
        self.assertLessEqual(worst_ok, 1e-9, f"本实现的配对必须恒等，实测 {worst_ok:.3e}")
        self.assertGreater(worst_bad, 1e-5, "初稿配对在随机姿态下必须明显不恒等（非恒真夹具）")
        self.assertLessEqual(worst_ok * 1.0, 1e-5, "硬门容差 1e-5")

    def test_payload_v2_carries_placement_and_v1_still_decodes(self):
        q = self._matrix(41, 1.6, (1.0, -0.5, 0.25))
        payload = _runtime.attach_range_payload(184, 18, placement=q)
        self.assertEqual(len(payload), _runtime.ATTACH_RANGE_BYTES)
        # 【t74】112 → 128 B：头部 + Q_f⁻¹ 3×4 + X_f 3×4 + 族索引 [7]（CS 读 [4..7]）。
        self.assertEqual(_runtime.ATTACH_RANGE_BYTES, 128)
        self.assertEqual(
            _runtime.ATTACH_RANGE_BYTES, _runtime.ATTACH_RANGE_HEADER_BYTES + 48 + 48 + 16
        )
        self.assertEqual(len(payload[16:64]), 48)
        self.assertEqual(len(payload[64:112]), 48)
        self.assertEqual(_runtime.ATTACH_RANGE_INDICES_OFFSET, 112)
        self.assertEqual(len(payload[112:128]), 16)
        decoded = _runtime.decode_attach_range_payload(payload)
        self.assertTrue(decoded["ok"])
        self.assertTrue(decoded["has_placement"])
        self.assertEqual((decoded["base"], decoded["span"]), (184, 18))
        self.assertEqual(decoded["version"], _runtime.ATTACH_RANGE_VERSION)
        for got, want in zip(decoded["placement"], q):
            self.assertAlmostEqual(got, want, places=6)
        # v1 的 16 B 载荷仍然解得出、但**不带摆放**（CS 因此不做校准）
        legacy = _runtime.attach_range_payload(0, 184, version=1)
        legacy = legacy[: _runtime.ATTACH_RANGE_HEADER_BYTES]
        self.assertEqual(len(legacy), 16)
        decoded_legacy = _runtime.decode_attach_range_payload(legacy)
        self.assertTrue(decoded_legacy["ok"])
        self.assertFalse(decoded_legacy["has_placement"])
        # 垃圾长度 ⇒ ok=False（不猜）
        self.assertFalse(_runtime.decode_attach_range_payload(b"\x00" * 7)["ok"])

    def _exporter_with_buffer(self, rows, *, bones=None):
        """**与真实产物同形**的缓冲：Position/Blend 都是一维 uint8 原始字节。

        * Position：44 B/行 = 11 个 float32（P3 N3 C1 T4）；
        * Blend：32 B/行 = 4 个权重 float32 + 4 个骨索引 int32（与产物同布局）；
        * ``bones``：每行的 ``[(weight, slot), …]``；缺省 = 全部挂在槽 1（= 自己族）。
        """
        import numpy

        raw = bytearray()
        for x, y, z in rows:
            raw += struct.pack("<11f", x, y, z, 0.0, 0.0, 1.0, 0.0, 1.0, 0.0, 0.0, 1.0)
        buffer = numpy.frombuffer(bytes(raw), dtype=numpy.uint8).copy()
        if bones is None:
            bones = [[(1.0, 1)]] * len(rows)
        blend = bytearray()
        for channels in bones:
            padded = list(channels) + [(0.0, 0)] * (4 - len(channels))
            blend += struct.pack("<4f", *[float(w) for w, _s in padded])
            blend += struct.pack("<4i", *[int(s) for _w, s in padded])
        blend_buffer = numpy.frombuffer(bytes(blend), dtype=numpy.uint8).copy()
        model = types.SimpleNamespace(
            draw_ib="aaaa1111",
            category_buffer_dict={"Position": buffer, "Blend": blend_buffer},
        )
        blueprint_model = types.SimpleNamespace(
            keyname_mkey_dict={}, ordered_draw_obj_data_model_list=[]
        )
        exporter = ExportHIMI.__new__(ExportHIMI)
        exporter.blueprint_model = blueprint_model
        exporter.drawib_model_list = [model]
        exporter._himi_notify = lambda detail, warning=False: None
        return exporter, buffer

    @staticmethod
    def _plan_with_two_families():
        """两个族各 4 个槽：``aaaa1111`` = [0,4)、``bbbb2222`` = [4,8)（与 palette 侧同一张表）。"""
        return types.SimpleNamespace(
            has_rebinds=True,
            family_ranges={
                "aaaa1111": {"base": 0, "slots": 4, "own_block": True},
                "bbbb2222": {"base": 4, "slots": 4, "own_block": True},
            },
        )

    @staticmethod
    def _row(buffer, index):
        """从逐字节缓冲里取第 ``index`` 行的 11 个 float。"""
        start = index * 44
        return struct.unpack("<11f", bytes(buffer[start : start + 44]))

    @staticmethod
    def _index_for(points, family, *, tolerance=1e-4, extra=None):
        """按 `load_family_position_index` 的**返回形状**造一个最小顶点族索引（含旧口径的 keys）。

        ``extra`` = ``{family: [点…]}``：额外族（用来造"跨族"或"多族候选"）。
        """
        import math as _math

        grid: dict = {}
        keys: dict = {}
        for name, group in [(family, list(points))] + list((extra or {}).items()):
            for point in group:
                x, y, z = (float(point[0]), float(point[1]), float(point[2]))
                cell_key = (
                    int(_math.floor(x / tolerance)),
                    int(_math.floor(y / tolerance)),
                    int(_math.floor(z / tolerance)),
                )
                grid.setdefault(cell_key, []).append((x, y, z, name))
                keys.setdefault((round(x, 5), round(y, 5), round(z, 5)), name)
        return {"ok": bool(grid), "grid": grid, "cell": tolerance, "tolerance": tolerance,
                "keys": keys, "points": sum(len(v) for v in grid.values()), "missing": [],
                "note": "测试夹具"}

    def test_vertex_side_never_rewrites_the_buffer_TWO_STATE(self):
        """【t78 方案 B】**两态**：有没有族级摆放，Position 缓冲都逐字节不动（顶点侧不校准）。

        旧实现（t39–t77）在这里把每行乘 Q_f 搬进「共同空间」，代价是形态键通道
        （``-Position_merged_packed_pos_delta.buf`` 的 δ 直接叠加到基态行上）变成 ``Q·v+δ``
        而不是 ``Q·(v+δ)`` ⇒ 非宿主族（Q = 1.6×R、R 84.6°–133.5°）整块撕裂/飞走。
        方案 B 把变换整体从顶点侧删掉、只留 palette 侧的 ``S = X_f·P_f``：同族
        ``P·Q⁻¹·Q·v ≡ P·v``（实测残差 4.6e-16）⇒ 基态逐位不变，δ 也回到同一空间。

        本用例对「偷偷写回一行」敏感：B 态给了 Q_f 且逐行归属判得出来，任何写回都会让
        ``assertEqual(buffer_b.tobytes(), before_b.tobytes())`` 立刻红。
        """
        rows = [(0.35, 0.90, -0.05), (1.0, 0.0, 0.5), (-0.25, 0.75, 1.25)]
        q = self._matrix(33, 1.6, (0.5, -0.25, 0.125))

        # A 态（非合并路径）：不诊断、不写
        exporter_a, buffer_a = self._exporter_with_buffer(rows)
        exporter_a._himi_family_placements = lambda: {}
        exporter_a.himi_merged_runtime_plan = lambda: types.SimpleNamespace(has_rebinds=False)
        before_a = buffer_a.copy()
        result_a = exporter_a._himi_calibrate_bind_positions()
        self.assertEqual(buffer_a.tobytes(), before_a.tobytes(), "A 态必须逐字节不动")
        self.assertEqual(result_a["families"], 0)

        # B 态（合并路径 + 给了 Q_f）：只数行数，缓冲原样；连取 Q_f 都不该发生
        exporter_b, buffer_b = self._exporter_with_buffer(rows)
        exporter_b._himi_family_placements = lambda: (_ for _ in ()).throw(
            AssertionError("【t78 方案 B】顶点侧不再取族级摆放 Q_f")
        )
        exporter_b.himi_merged_runtime_plan = self._plan_with_two_families
        before_b = buffer_b.copy()
        result_b = exporter_b._himi_calibrate_bind_positions()
        self.assertTrue(result_b["ok"], result_b)
        self.assertEqual(result_b["families"], 1, result_b)
        self.assertEqual(result_b["rows"], len(rows), result_b)
        self.assertEqual(result_b["per_family"], {"aaaa1111": len(rows)}, result_b)
        self.assertNotIn("cross_max_shift", result_b, "没有顶点侧变换就没有位移")
        self.assertEqual(buffer_b.tobytes(), before_b.tobytes(), "B 态也必须逐字节不动")
        for index, point in enumerate(rows):
            got = self._row(buffer_b, index)
            for axis, want in enumerate(point):
                self.assertAlmostEqual(got[axis], want, places=6, msg=f"第 {index} 行原位不动")
            self.assertEqual(got[3:6], (0.0, 0.0, 1.0), "法线原样")
            self.assertEqual(got[7:10], (1.0, 0.0, 0.0), "切线原样")
            self.assertEqual(got[6], 0.0, "COLOR 不动")
            self.assertEqual(got[10], 1.0, "TANGENT.w（手性）不动")
        # 反向护栏：夹具必须能让「写回 Q·v」被看见（否则本用例对写回是盲的）
        self.assertGreater(
            max(abs(a - b) for a, b in zip(_runtime.mat3x4_apply(q, rows[0]), rows[0])), 1e-2,
            "夹具必须能让『写回 Q·v』被看见",
        )

    def test_generate_buffer_files_hooks_the_calibration(self):
        """接点检查：`generate_buffer_files` 必须先校准再走基类（否则产物与 ini 不同源）。"""
        import inspect

        source = inspect.getsource(ExportHIMI.generate_buffer_files)
        self.assertIn("_himi_calibrate_bind_positions", source)
        self.assertIn("super().generate_buffer_files", source)

    def test_cs_text_has_the_calibration_and_version_gate_TWO_STATE(self):
        text = _himi_exporter.merged_attach_hlsl_text()
        code = _himi_exporter._strip_hlsl_comments(text)
        self.assertEqual(_himi_exporter.merged_attach_hlsl_problems(text), [])
        for needle in (
            "himi_attach_range[1]", "himi_attach_range[2]", "himi_attach_range[3]",
            "asuint(attach_range.z) >= 3u", "float4 out0 = src0;",
            "out0 = float4(dot(src0.xyz, qc0)", "[dst + 0u] = out0;",
        ):
            self.assertIn(needle, code, f"生成的 CS 必须含 {needle}")
        mutations = {
            "去掉版本门（旧 16 B/v2 载荷会按错误方向复合）": text.replace(
                "asuint(attach_range.z) >= 3u", "true"
            ),
            "去掉复合（退回直接搬运）": text.replace(
                f"    {himi_runtime_skeleton_name_placeholder()}",
                "",
            ).replace("out0 = float4(dot(src0.xyz, qc0)", "out0 = src0; //"),
            "去掉写侧的 out0": text.replace("[dst + 0u] = out0;", "[dst + 0u] = src0;"),
        }
        for label, mutated in mutations.items():
            with self.subTest(mutation=label):
                self.assertNotEqual(mutated, text, f"{label}：变异必须真的改到文本")
                self.assertNotEqual(
                    _himi_exporter.merged_attach_hlsl_problems(mutated), [],
                    f"{label} ⇒ 判据必须报错（否则判据恒真）",
                )

    def test_row_family_diagnostics_do_not_touch_the_buffer_TWO_STATE(self):
        """【t41/t43 诊断 + t78 方案 B】逐行「几何族 vs 骨头族」只产出数字，缓冲原样。

        夹具：DrawIB ``aaaa1111``（族区间 [0,4)），另有族 ``bbbb2222``（[4,8)）。
        第 0/2 行的骨头在自己族（槽 1/2）、第 1 行借 ``bbbb2222`` 的槽 5、第 3 行无活动通道。

        方案 B 下跨族借用的正确性来自着色器里的 ``S = X_g·P_g[k]``：LBS 是
        ``world = Σ w_i·S[c_i]·v``，作用在 v 上的任何常量都能搬到槽上 ⇒ **顶点不必先搬空间**。
        旧方案错在这一行可以量化：顶点被乘了 Q_a 之后，槽里的 ``P_b∘Q_b⁻¹`` 得到
        ``P_b·Q_b⁻¹·Q_a·v``，与真值 ``P_b·v`` 相差 > 0.1（各族 Q 两两相差 133°–180°）。
        本用例一次钉住三件事：(1) 诊断数字仍然正确；(2) 缓冲一个字节都没动；
        (3) 旧方案在跨族行上确实错（所以「撤销 R1 Fatal」不是放水，而是把错的那侧删掉）。
        """
        rows = [(0.35, 0.90, -0.05), (1.0, 0.0, 0.5), (-0.25, 0.75, 1.25), (2.0, 1.0, 0.0)]
        bones = [
            [(1.0, 1)],                 # 自己族 aaaa1111
            [(1.0, 5)],                 # 跨族借用 bbbb2222
            [(0.7, 2), (0.3, 1)],       # 自己族（两个通道同族）
            [(0.0, 0)],                 # 无活动通道
        ]
        q_a = self._matrix(33, 1.6, (0.5, -0.25, 0.125))
        q_b = self._matrix(-24, 1.6, (-0.8, 0.4, 0.05))
        p_b = self._matrix(-9, 1.0, (-0.4, 0.6, 0.2))     # 借出族的某根骨
        exporter, buffer = self._exporter_with_buffer(rows, bones=bones)
        exporter._himi_family_placements = lambda: {"aaaa1111": q_a, "bbbb2222": q_b}
        exporter.himi_merged_runtime_plan = self._plan_with_two_families
        # 顶点族索引：真实路径由 `load_family_position_index` 建（容差网格）；这里给形状相同的
        # 最小索引（第 0/1/2 行的顶点都属于 aaaa1111 ⇒ 第 1 行是**跨族借用**）。
        exporter._himi_vertex_family_index = lambda: self._index_for(rows, "aaaa1111")
        warnings = []
        exporter._himi_notify = lambda detail, warning=False: warnings.append((detail, warning))
        before = bytes(buffer)

        result = exporter._himi_calibrate_bind_positions()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["per_family"], {"aaaa1111": 3}, result)
        self.assertEqual(result["cross"], 1, result)
        self.assertEqual(result["mixed"], 0, result)
        self.assertEqual(result["unweighted"], 1, result)
        self.assertEqual(result["rows"], 3, result)
        # 方案 B 的硬门：诊断不得改写任何字节（含第 3 行「无活动通道」）
        self.assertEqual(bytes(buffer), before, "方案 B：诊断不得改写缓冲")
        # 跨族 > 0 ⇒ 一条 warn（R1 门已按 coordinator 复审撤销：不 Fatal，只让人看见）
        self.assertTrue(warnings, "跨族借用必须留一条诊断")
        self.assertTrue(all(warning for _detail, warning in warnings), warnings)
        self.assertIn("跨族借用权重 1 行", warnings[0][0])
        self.assertIn("方案 B", warnings[0][0])
        # 旧方案（顶点乘 Q_a + palette 乘 Q_b⁻¹）在跨族行上的偏差：必须 > 1e-2
        old_world = _runtime.mat3x4_apply(
            _runtime.mat3x4_compose(p_b, _runtime.mat3x4_inverse(q_b)),
            _runtime.mat3x4_apply(q_a, rows[1]),
        )
        truth_world = _runtime.mat3x4_apply(p_b, rows[1])
        deviation = max(abs(a - b) for a, b in zip(old_world, truth_world))
        self.assertGreater(deviation, 1e-2, f"旧方案在跨族行上必须明显错（实测 {deviation:.6f}）")

    def test_blend_origin_sidecar_and_out_of_range_log_TWO_STATE(self):
        """【t49 / t47 方案 A+C】旁写 ``<fam>-BlendOrigin.buf``（8 B/顶点）+ 越界引用计数。

        A 态（干净）：全部活动通道都在本族区段 ⇒ `out_of_range = 0`、`flags = 0`、
        载荷长度 = 8 × 顶点数、硬门 `[]`。
        B 态①（注入一条越界引用）：某行的一路索引 = 别族的槽 ⇒ `out_of_range = 1`、`flags.bit0 = 1`、
        但该行**仍有在范围通道** ⇒ `local_slot` 仍可解（< span），硬门仍 `[]`。
        B 态②（整行都越界）：该行**没有**在范围通道 ⇒ `local_slot = 0xFFFFFFFF`（哨兵）+
        `flags.bit1|bit2` ⇒ **硬门必须报出来**（不静默放过）—— 这正是"导出侧注入越界索引"的直接证据。
        """
        base, span = 0, 4
        clean = struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) + struct.pack("<4i", 1, 0, 0, 0)
        clean += struct.pack("<4f", 0.5, 0.5, 0.0, 0.0) + struct.pack("<4i", 3, 2, 0, 0)
        mixed = struct.pack("<4f", 0.6, 0.4, 0.0, 0.0) + struct.pack("<4i", 275, 2, 0, 0)
        broken = struct.pack("<4f", 1.0, 0.0, 0.0, 0.0) + struct.pack("<4i", 275, 0, 0, 0)

        good = _runtime.blend_origin_rows(clean, base=base, span=span)
        self.assertTrue(good["ok"], good)
        self.assertEqual(good["vertices"], 2)
        self.assertEqual(good["out_of_range"], 0)
        self.assertEqual(good["local_unavailable"], 0)
        self.assertEqual([f for _l, f in good["rows"]], [0, 0])
        self.assertEqual([l for l, _f in good["rows"]], [1, 2])
        payload = _runtime.blend_origin_payload(good["rows"])
        self.assertEqual(len(payload), 8 * 2)
        self.assertEqual(_runtime.blend_origin_problems(
            good["rows"], span=span, vertices=good["vertices"]), [])

        bad = _runtime.blend_origin_rows(clean + mixed + broken, base=base, span=span)
        self.assertEqual(bad["vertices"], 4)
        self.assertEqual(bad["out_of_range"], 2, bad)
        self.assertEqual(bad["local_unavailable"], 1, bad)
        rows = bad["rows"]
        self.assertEqual(rows[2][0], 2, "① 行仍有在范围通道 ⇒ local_slot 可解")
        self.assertTrue(rows[2][1] & _runtime.BLEND_ORIGIN_FLAG_OUT_OF_RANGE)
        self.assertFalse(rows[2][1] & _runtime.BLEND_ORIGIN_FLAG_LOCAL_UNAVAILABLE)
        self.assertEqual(rows[3][0], _runtime.BLEND_ORIGIN_LOCAL_UNAVAILABLE)
        self.assertTrue(rows[3][1] & _runtime.BLEND_ORIGIN_FLAG_LOCAL_UNAVAILABLE)
        self.assertTrue(rows[3][1] & _runtime.BLEND_ORIGIN_FLAG_ALL_CHANNELS_OUT)
        problems = _runtime.blend_origin_problems(rows, span=span, vertices=bad["vertices"])
        self.assertTrue(problems, "哨兵行必须被硬门报出来（本用例 = 方案 C 的变红证据）")
        self.assertTrue(any("不可得" in item for item in problems), problems)
        self.assertTrue(_runtime.blend_origin_problems(rows[:3], span=span, vertices=4))

    def test_blend_origin_writer_is_wired_and_reports_the_count(self):
        """接点 + 三层诊断：`generate_buffer_files` 必须调用旁写；越界告警必须进产物注释块。"""
        import inspect

        source = inspect.getsource(ExportHIMI.generate_buffer_files)
        self.assertIn("_himi_write_blend_origin", source)
        self.assertIn("_himi_calibrate_bind_positions", source)
        writer = inspect.getsource(ExportHIMI._himi_write_blend_origin)
        for needle in ("blend_origin_rows", "blend_origin_payload", "out_of_range_refs=",
                       "_himi_blend_origin_notices"):
            self.assertIn(needle, writer, f"旁写实现必须含 {needle}")
        diagnostics = inspect.getsource(ExportHIMI.himi_diagnostic_lines)
        self.assertIn("out_of_range_refs", diagnostics, "计数必须进产物 ini 注释块（第三层诊断）")

    def test_common_space_inversion_blind_spot_TWO_STATE(self):
        """【t44 取反盲区门】只有「共同空间取反」才会红的用例。

        为什么需要（t38 预警的 E3 陷阱）：同族行上 `P·Q·Q⁻¹·v = P·v` **与** `P·Q⁻¹·Q·v = P·v`
        代数上都成立 ⇒ 现有全部同族硬门对"取反"是**盲的**（t39–t43 的错版就是这样过门的）。
        本用例取 `Q²≠I` 的两个族，用**跨族行**的世界坐标对照期望公式，并且：
        * 正版（顶点 `Q_f·v` + palette `P_g∘Q_g⁻¹`）⇒ 与期望一致（≤1e-5）；
        * 取反版（顶点 `Q_f⁻¹·v` + palette `P_g∘Q_g`）⇒ 与期望差 **> 0.1**（实测量级见报告 §3）；
        ⇒ 把实现改回取反，本用例必红。
        """
        q_f = self._matrix(41, 1.6, (0.9, -0.3, 0.2))     # 顶点族
        q_g = self._matrix(-19, 1.6, (-0.7, 0.5, 0.05))   # 借出族
        # 取 Q² ≠ I 的检查（否则"取反"与"正版"可能碰巧一致 ⇒ 用例恒真）
        square = _runtime.mat3x4_compose(q_f, q_f)
        identity = [1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0]
        self.assertGreater(
            max(abs(a - b) for a, b in zip(square, identity)), 0.1,
            "夹具必须满足 Q²≠I（否则本用例对取反是盲的）",
        )
        p_g = self._matrix(-7, 1.0, (-0.5, 0.3, 0.15))    # 借出族的某根骨
        vertex = (0.35, 0.90, -0.05)                       # 顶点族空间里的绑定位置
        palette_f = _runtime.mat3x4_inverse(q_f)
        palette_g = _runtime.mat3x4_inverse(q_g)

        # 正版：v' = Q_f·v ⇒ world = P_g·Q_g⁻¹·v'
        correct = _runtime.mat3x4_apply(
            _runtime.mat3x4_compose(p_g, palette_g), _runtime.mat3x4_apply(q_f, vertex)
        )
        # 期望公式（等价写法，逐步展开）：P_g · (Q_g⁻¹ · (Q_f · v))
        expected = _runtime.mat3x4_apply(
            p_g, _runtime.mat3x4_apply(palette_g, _runtime.mat3x4_apply(q_f, vertex))
        )
        self.assertLess(
            max(abs(a - b) for a, b in zip(correct, expected)), 1e-9,
            "正版必须与期望公式逐值一致",
        )
        # 取反版：v' = Q_f⁻¹·v ；palette = P_g·Q_g
        inverted = _runtime.mat3x4_apply(
            _runtime.mat3x4_compose(p_g, q_g), _runtime.mat3x4_apply(palette_f, vertex)
        )
        gap = max(abs(a - b) for a, b in zip(inverted, expected))
        self.assertGreater(gap, 0.1, f"取反版必须与期望明显不同（实测 {gap:.6f}）")
        # 且"取反版"与"不校准"也不同（说明它不是恰好退化成不校准 ⇒ 确实是一个错的中间空间）
        uncalibrated = _runtime.mat3x4_apply(p_g, vertex)
        self.assertGreater(
            max(abs(a - b) for a, b in zip(inverted, uncalibrated)), 0.1,
            "取反版 ≠ 不校准（否则本用例分辨不出「取反」与「不校准」两种错）",
        )

    def test_attach_compose_text_and_numeric_gate_TWO_STATE(self):
        """【t41 / T40-F3】文本门 + **与 CS 文本同源的数值门**：变异必须变红。

        * A 态：生成的 CS ⇒ 文本判据 `[]`；解析出的模型三行都是 `correct`；
          模型算出的 3×4 与 `mat3x4_compose` 逐元素差 < 1e-9（验收要求 < 1e-6）。
        * B 态 ①：把平移项退回旧写法 `placement_i.www` ⇒ 文本判据报错 **且** 数值差 > 1e-6
          （实测旧写法差 3.010439）。
        * B 态 ②：把平移项 `* 0.0` 抹掉 ⇒ 文本判据报错 **且** 数值差 > 1e-6。
        """
        text = _himi_exporter.merged_attach_hlsl_text()
        self.assertEqual(_himi_exporter.merged_attach_hlsl_problems(text), [])
        model = _runtime.attach_compose_model_from_hlsl(text)
        self.assertTrue(model["ok"], model)
        self.assertEqual(
            set(model["modes"].values()), {_runtime.ATTACH_COMPOSE_CORRECT}, model["modes"]
        )
        # 文本门：`placement_t` 4 次（1 次定义 + 3 次使用）、旧写法 0 次
        self.assertEqual(text.count("placement_t"), 4, text.count("placement_t"))
        for index in (0, 1, 2):
            self.assertEqual(text.count(f"placement{index}.www"), 0)

        # 数值门：真实载荷 Q（b35cfc6c 的 Q_f）+ 样例 P（第 0 槽与第 3 槽）
        import numpy

        payload = _runtime.attach_range_payload(0, 184, placement=self._matrix(37, 1.6, (0.4, -0.2, 0.7)))
        decoded = _runtime.decode_attach_range_payload(payload)
        q_rows = numpy.asarray(decoded["placement"], dtype="float64").reshape(3, 4).tolist()
        p_rows = numpy.asarray(self._matrix(11, 1.3, (1.0, 2.0, -0.5)), dtype="float64").reshape(3, 4).tolist()
        reference = numpy.asarray(_runtime.mat3x4_compose(
            [v for row in p_rows for v in row], [v for row in q_rows for v in row]
        ), dtype="float64").reshape(3, 4)
        got = numpy.asarray(
            _runtime.compose_rows_from_model(model, p_rows, q_rows), dtype="float64"
        ).reshape(3, 4)
        correct_diff = float(numpy.max(numpy.abs(got - reference)))
        self.assertLess(correct_diff, 1e-6, f"正确写法必须 < 1e-6，实测 {correct_diff:.6e}")

        mutations = {
            "退回旧写法 placement_i.www": text.replace(
                "dot(src0.xyz, placement_t) + src0.w", "dot(src0.xyz, placement0.www) + src0.w"
            ).replace(
                "dot(src1.xyz, placement_t) + src1.w", "dot(src1.xyz, placement1.www) + src1.w"
            ).replace(
                "dot(src2.xyz, placement_t) + src2.w", "dot(src2.xyz, placement2.www) + src2.w"
            ),
            "平移项 * 0.0（丢掉平移）": text.replace(
                "dot(src0.xyz, placement_t) + src0.w", "dot(src0.xyz, placement_t) * 0.0 + src0.w"
            ).replace(
                "dot(src1.xyz, placement_t) + src1.w", "dot(src1.xyz, placement_t) * 0.0 + src1.w"
            ).replace(
                "dot(src2.xyz, placement_t) + src2.w", "dot(src2.xyz, placement_t) * 0.0 + src2.w"
            ),
        }
        diffs = {}
        for label, mutated in mutations.items():
            with self.subTest(mutation=label):
                self.assertIsInstance(mutated, str)
                self.assertNotEqual(mutated, text, f"{label}：变异必须真的改到文本")
                problems = _himi_exporter.merged_attach_hlsl_problems(mutated)
                self.assertTrue(problems, f"{label} ⇒ 文本门必须报错（否则判据恒真）")
                mutated_model = _runtime.attach_compose_model_from_hlsl(mutated)
                mutated_rows = numpy.asarray(
                    _runtime.compose_rows_from_model(mutated_model, p_rows, q_rows), dtype="float64"
                ).reshape(3, 4)
                diff = float(numpy.max(numpy.abs(mutated_rows - reference)))
                diffs[label] = diff
                self.assertGreater(
                    diff, 1e-6, f"{label} ⇒ 数值门必须变红（实测差 {diff:.6e}）"
                )
        # 记录实测差（供报告引用）：两种变异都必须明显偏掉（旧写法实测 3.010439 量级）
        self.assertGreater(diffs["退回旧写法 placement_i.www"], 1e-3)
        self.assertGreater(diffs["平移项 * 0.0（丢掉平移）"], 1e-3)
        print("[t41] 数值门实测差：%s" % {k: round(v, 6) for k, v in diffs.items()})

    def test_product_ini_section_problems_TWO_STATE(self):
        good = "\n".join([
            "[Constants]",
            "global $x = 0",
            "",
            "[ResourceHI3MergedSkeleton_role_x]",
            "type = RWBuffer",
            "array = 1029",
            "",
            "[TextureOverride_a]",
            "hash = 1",
            "drawindexed = 3,0,0",
        ])
        self.assertEqual(_himi_exporter.product_ini_section_problems(good), [])
        # B①：段头插在别的段的**段头与属性之间** ⇒ 被顶掉的那段属性为 0 条（2026-09-26 事故形态）
        broken = "\n".join([
            "[ResourceHI3MergedSkeleton_role_x]",   # ← 骨架段被顶掉：段头之后紧跟下一个段头
            "[ResourceHI3AttachRange_x]",
            "type = Buffer",
            "filename = Meshes/x-AttachRange.buf",
            "array = 1029",
        ])
        problems = _himi_exporter.product_ini_section_problems(broken)
        self.assertTrue(problems, "插错位置的段头必须被抓到")
        self.assertTrue(any("属性为 0 条" in item for item in problems), problems)
        self.assertIn("ResourceHI3MergedSkeleton_role_x", problems[0])
        # B②：空段头
        self.assertTrue(
            _himi_exporter.product_ini_section_problems("[]\nkey = 1\n"),
        )
        # B③：文件开头就是属性（悬空）
        self.assertTrue(_himi_exporter.product_ini_section_problems("key = 1\n[Sec]\nkey = 2\n"))
        # 真实产物必须过（用 t19 的产物 ini，若存在）
        ini_path = REPO_ROOT / ".dbg" / "export_t19_merged_only" / "嗨♪爱愿妖精♥.ini"
        if ini_path.is_file():
            text = ini_path.read_text(encoding="utf-8")
            self.assertEqual(
                _himi_exporter.product_ini_section_problems(text), [],
                "真实产物必须通过逐段解析自检",
            )


class HIMINearestFamilyTieTests(unittest.TestCase):
    """【t24】顶点族判别的近并列（焊接点）回归：94 行位置污染的**生成器侧**修复。

    复用 :class:`HIMIFamilyCalibrationTests` 的矩阵/缓冲/导出器夹具（显式调用，不继承 ——
    避免父类用例在本类里重复执行一遍）。

    机制（reports/himi-merge/200-audit/26-94rows-contamination.md §5.1）：焊接点在两个族的
    源池里各有一份几乎相同的坐标（到 query 的距离差 7.45e-09 ~ 8.71e-08，float32 重舍入噪声
    量级），旧并列阈值 **1e-12** 比它小 4 个数量级 ⇒ 「并列 ⇒ prefer 骨头族」的保守分支
    永不触发，胜负由舍入尾差掷硬币 ⇒ 94 行被乘上别族 Q。t24：窗口放宽到 1e-6（量纲依据见
    ``himi_merged_runtime.FAMILY_TIE_WINDOW``）+ 法线/切线在 prefer 没投票时做唯一命中判别。
    """

    TOL = 1e-4
    FAM_BONE = "aaaa1111"
    FAM_OTHER = "bbbb2222"
    FAM_THIRD = "cccc3333"

    @staticmethod
    def _entry(point, family, normal=None, tangent=None):
        x, y, z = (float(v) for v in point)
        if normal is None and tangent is None:
            return (x, y, z, family)          # 旧 4 元组形状（属性判别停用）
        nx, ny, nz = (float(v) for v in (normal or (0.0, 0.0, 1.0)))
        tx, ty, tz = (float(v) for v in (tangent or (1.0, 0.0, 0.0)))
        return (x, y, z, family, nx, ny, nz, tx, ty, tz)

    def _index(self, entries, tolerance=1e-4):
        import math as _math

        grid = {}
        for entry in entries:
            key = (int(_math.floor(entry[0] / tolerance)),
                   int(_math.floor(entry[1] / tolerance)),
                   int(_math.floor(entry[2] / tolerance)))
            grid.setdefault(key, []).append(entry)
        return {"ok": bool(grid), "grid": grid, "cell": tolerance, "tolerance": tolerance,
                "keys": {}, "points": len(entries), "missing": [], "note": "t24 夹具"}

    @staticmethod
    def _legacy_nearest_family(info, point, *, family_order=(), prefer=""):
        """修复前实现的逐字镜像（1e-12 并列、无属性）—— 供两态对照：它必须在焊接点上翻错。"""
        import math as _math

        grid = dict(info or {}).get("grid") or {}
        tolerance = float(dict(info or {}).get("tolerance", 0.0) or 0.0)
        cell = float(dict(info or {}).get("cell", 0.0) or 0.0)
        x, y, z = (float(point[0]), float(point[1]), float(point[2]))
        base = (int(_math.floor(x / cell)), int(_math.floor(y / cell)), int(_math.floor(z / cell)))
        best = {}
        for dx in (-1, 0, 1):
            for dy in (-1, 0, 1):
                for dz in (-1, 0, 1):
                    for entry in grid.get((base[0] + dx, base[1] + dy, base[2] + dz), ()):
                        distance = _math.dist((x, y, z), (entry[0], entry[1], entry[2]))
                        if distance > tolerance:
                            continue
                        family = str(entry[3])
                        if family not in best or distance < best[family]:
                            best[family] = distance
        if not best:
            return ""
        lowest = min(best.values())
        tied = [name for name, distance in best.items() if distance <= lowest + 1e-12]
        if prefer and prefer in tied:
            return prefer
        for name in list(family_order) + sorted(tied):
            if name in tied:
                return name
        return ""

    def test_weld_near_tie_keeps_bone_family_TWO_STATE(self):
        """**近并列 ⇒ 不判别族**：两族拷贝差 3e-8（>1e-12、<1e-6）⇒ 保持骨头族。

        夹具对齐报告 26 的实测形态：query（导出行）离**别族**拷贝更近（差 3e-8）——
        这正是"胜负由舍入尾差决定"的翻转态。
        * 旧口径镜像（1e-12）：严格最近赢 ⇒ 翻给别族（= 94 行污染的机制，改前必红）；
        * 新口径：3e-8 落进 1e-6 窗口 ⇒ prefer 骨头族（= 同族校准，变形逐值不变）。
        """
        bone_copy = (0.35, 0.90, -0.05)
        other_copy = (bone_copy[0] + 3e-8, bone_copy[1], bone_copy[2])
        entries = [
            self._entry(bone_copy, self.FAM_BONE),
            self._entry(other_copy, self.FAM_OTHER),
        ]
        info = self._index(entries)
        point = other_copy                       # query 离别族拷贝更近（污染行的实测形态）
        legacy = self._legacy_nearest_family(
            info, point, family_order=[self.FAM_BONE, self.FAM_OTHER], prefer=self.FAM_BONE
        )
        self.assertEqual(
            legacy, self.FAM_OTHER,
            "旧口径镜像必须翻给别族（否则本用例对 1e-12 病根是盲的）",
        )
        hit = _runtime.nearest_family(
            info, point, family_order=[self.FAM_BONE, self.FAM_OTHER], prefer=self.FAM_BONE
        )
        self.assertEqual(hit["family"], self.FAM_BONE, hit)
        self.assertEqual(sorted(hit["tied"]), [self.FAM_BONE, self.FAM_OTHER], hit)
        self.assertTrue(hit["ambiguous"], "焊接点必须仍被记为多族候选（诊断语义不变）")

    def test_true_tie_prefers_bone_family(self):
        """**真并列 ⇒ prefer 骨族**：两族拷贝逐位相同 ⇒ 骨头族（与旧实现同一结论，回归钉）。"""
        point = (1.0, 2.0, 3.0)
        info = self._index([
            self._entry(point, self.FAM_OTHER),
            self._entry(point, self.FAM_BONE),
        ])
        hit = _runtime.nearest_family(
            info, point, family_order=[self.FAM_BONE, self.FAM_OTHER], prefer=self.FAM_BONE
        )
        self.assertEqual(hit["family"], self.FAM_BONE, hit)

    def test_unique_attribute_hit_overrides_when_prefer_has_no_vote(self):
        """**唯一命中 ⇒ 照常判族**（借骨行场景）：骨头族不在并列集时，属性唯一命中者改判。

        两族拷贝并列（差 5e-8）；query 的法线只与 ``bbbb`` 的一致（``cccc`` 差 ~1.4）。
        严格最近是 ``cccc`` ⇒ 旧口径会选 cccc；新口径按属性改判 bbbb。
        """
        weld = (0.5, 0.5, 0.5)
        normal = (0.0, 0.6, 0.8)
        entries = [
            self._entry(weld, self.FAM_THIRD, normal=(1.0, 0.0, 0.0)),          # 严格最近（错族）
            self._entry((weld[0] + 5e-8, weld[1], weld[2]), self.FAM_OTHER, normal=normal),
        ]
        info = self._index(entries)
        query_point = (weld[0] + 1e-8, weld[1], weld[2])
        legacy = self._legacy_nearest_family(
            info, query_point, family_order=[self.FAM_OTHER, self.FAM_THIRD],
            prefer=self.FAM_BONE,
        )
        self.assertEqual(legacy, self.FAM_THIRD, "旧口径镜像必须选严格最近（错族）")
        hit = _runtime.nearest_family(
            info, query_point, family_order=[self.FAM_OTHER, self.FAM_THIRD],
            prefer=self.FAM_BONE, normal=normal,
        )
        self.assertEqual(hit["family"], self.FAM_OTHER, hit)
        self.assertIsNotNone(hit["attr_residual"], hit)
        self.assertLessEqual(hit["attr_residual"], _runtime.FAMILY_ATTRIBUTE_TOLERANCE, hit)

    def test_attribute_ambiguous_or_unavailable_falls_back_to_strictly_nearest(self):
        """**判不出唯一族 ⇒ 保守**：属性双命中 / 零命中 / 索引无属性 ⇒ 不改判。

        回退结果 = 旧实现的同一确定性规则（``family_order`` 先到先得；本夹具把严格最近族
        排在 ``family_order`` 首位 ⇒ 两者同值），绝不因属性勉强改判。
        """
        weld = (0.5, 0.5, 0.5)
        normal = (0.0, 0.6, 0.8)
        order = [self.FAM_THIRD, self.FAM_OTHER]   # 严格最近族（cccc，距离 0）排首位
        both_match = self._index([
            self._entry(weld, self.FAM_THIRD, normal=normal),
            self._entry((weld[0] + 5e-8, weld[1], weld[2]), self.FAM_OTHER, normal=normal),
        ])
        hit = _runtime.nearest_family(
            both_match, weld, family_order=order,
            prefer=self.FAM_BONE, normal=normal,
        )
        self.assertEqual(hit["family"], self.FAM_THIRD, hit)      # 双命中 ⇒ 族次序回退
        neither = self._index([
            self._entry(weld, self.FAM_THIRD, normal=(1.0, 0.0, 0.0)),
            self._entry((weld[0] + 5e-8, weld[1], weld[2]), self.FAM_OTHER, normal=(0.0, 1.0, 0.0)),
        ])
        hit = _runtime.nearest_family(
            neither, weld, family_order=order,
            prefer=self.FAM_BONE, normal=normal,
        )
        self.assertEqual(hit["family"], self.FAM_THIRD, hit)      # 零命中 ⇒ 族次序回退
        legacy_shape = self._index([
            self._entry(weld, self.FAM_THIRD),                    # 旧 4 元组：无属性可判
            self._entry((weld[0] + 5e-8, weld[1], weld[2]), self.FAM_OTHER),
        ])
        hit = _runtime.nearest_family(
            legacy_shape, weld, family_order=order,
            prefer=self.FAM_BONE, normal=normal,
        )
        self.assertEqual(hit["family"], self.FAM_THIRD, hit)      # 索引无属性 ⇒ 族次序回退

    def test_nan_or_missing_query_attributes_do_not_change_the_verdict(self):
        """查询属性缺/坏（NaN）⇒ 属性判别停用，结论与不带属性时完全一致。"""
        weld = (0.5, 0.5, 0.5)
        normal = (0.0, 0.6, 0.8)
        info = self._index([
            self._entry(weld, self.FAM_THIRD, normal=(1.0, 0.0, 0.0)),
            self._entry((weld[0] + 5e-8, weld[1], weld[2]), self.FAM_OTHER, normal=normal),
        ])
        baseline = _runtime.nearest_family(
            info, weld, family_order=[self.FAM_OTHER, self.FAM_THIRD], prefer=self.FAM_BONE
        )
        for query in (None, (float("nan"), 0.6, 0.8)):
            hit = _runtime.nearest_family(
                info, weld, family_order=[self.FAM_OTHER, self.FAM_THIRD],
                prefer=self.FAM_BONE, normal=query,
            )
            self.assertEqual(hit["family"], baseline["family"], (query, hit))

    def test_calibration_end_to_end_weld_rows_stay_on_bone_family_TWO_STATE(self):
        """**端到端两态**：焊接行在完整诊断路径上不被写回，且归属仍判回**骨头族**。

        夹具 = 报告 26 §1 的 1:1 缩影：族 ``aaaa1111`` 缓冲里的 2 行，每行的位置在
        ``bbbb2222`` 源池里有一份差 3e-8 的拷贝（焊接点）。旧行为（镜像复算）会把两行
        判给 bbbb ⇒ 当时乘 Q_bbbb 而不是 Q_aaaa（t44 之后那一版）；【t78 方案 B】起顶点侧
        根本不写字节，但归属**数字**仍必须稳定判回 aaaa（判给 bbbb 会让 cross 变 2）。
        """
        rows = [(0.35, 0.90, -0.05), (1.0, 0.0, 0.5)]
        q_a = HIMIFamilyCalibrationTests._matrix(33, 1.6, (0.5, -0.25, 0.125))
        q_b = HIMIFamilyCalibrationTests._matrix(-24, 1.6, (-0.8, 0.4, 0.05))
        exporter, buffer = HIMIFamilyCalibrationTests._exporter_with_buffer(self, rows)
        exporter._himi_family_placements = lambda: {"aaaa1111": q_a, "bbbb2222": q_b}
        exporter.himi_merged_runtime_plan = lambda: (
            HIMIFamilyCalibrationTests._plan_with_two_families()
        )
        # 10 元组索引：每行的位置有一个 bbbb 焊接拷贝（与行逐位同点 = 舍入尾差的翻转态）
        # 和一个差 3e-8 的 aaaa 拷贝（法线与缓冲行一致 = (0,0,1)；bbbb 法线刻意错开 ⇒
        # 即便走到属性判别也会否决 bbbb）。
        entries = []
        for x, y, z in rows:
            entries.append(self._entry((x, y, z), self.FAM_OTHER, normal=(0.0, 1.0, 0.0)))
            entries.append(self._entry((x - 3e-8, y, z), self.FAM_BONE,
                                       normal=(0.0, 0.0, 1.0)))
        exporter._himi_vertex_family_index = lambda: self._index(entries)
        # 旧口径镜像 ⇒ 两行都翻给 bbbb（= 污染机制，改前必红）
        for x, y, z in rows:
            legacy = self._legacy_nearest_family(
                self._index(entries), (x, y, z),
                family_order=[self.FAM_BONE, self.FAM_OTHER], prefer=self.FAM_BONE,
            )
            self.assertEqual(legacy, self.FAM_OTHER, "旧口径镜像必须翻给别族（两态的红态）")

        before = bytes(buffer)
        result = exporter._himi_calibrate_bind_positions()
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["per_family"], {"aaaa1111": len(rows)}, result)
        self.assertEqual(result["claimed"], len(rows), result)
        self.assertEqual(result["cross"], 0, "焊接行必须判回骨头族（否则会被记成跨族借用）")
        # 【t78 方案 B】顶点侧不校准 ⇒ 缓冲一个字节都不动
        self.assertEqual(bytes(buffer), before, "方案 B：焊接行也不得被写回")
        for index, point in enumerate(rows):
            got = HIMIFamilyCalibrationTests._row(buffer, index)[0:3]
            for axis, want in enumerate(point):
                self.assertAlmostEqual(got[axis], want, places=6, msg=f"焊接行 {index} 原位不动")
            # 反向护栏：两族 Q 必须可分辨（否则「乘错族」这类变异对本用例是盲的）
            self.assertGreater(
                max(abs(a - b) for a, b in zip(
                    _runtime.mat3x4_apply(q_a, point), _runtime.mat3x4_apply(q_b, point))), 1e-3,
                f"焊接行 {index}: Q_aaaa 与 Q_bbbb 必须明显不同",
            )


class HIMIBorrowLayoutBaselineTests(unittest.TestCase):
    """【t93】跨族借用骨骼的专用槽：**块尾追加**布局 + 借用表 + 身份资源 + 基址表的基准复算。

    基准 = 已实机验证通过的那份手改产物（一个角色，3 族；原布局 219 槽 → 新布局 223 槽）：

    ==== ===== ==== ==== ============
    族    base  span 块长 块尾借用槽
    ==== ===== ==== ==== ============
    077…  0     51   55   51/52/53/54
    09c…  55    165  166  220
    1b6…  221   1    2    222
    ==== ===== ==== ==== ============

    这一组全部走**纯函数**（不抓帧、不读 K: 产物）：布局 → 借用行 → 借用表字节 →
    身份载荷 → 基址表 → Blend 重映射表，逐项与实测值对齐。反向用例覆盖第 5 条校验钩子
    （块重叠 / 目标槽越界 / 源局部槽越界 / 源族序号非法 / 未映射通道 / 块外索引）。
    """

    FAMILIES = ("077983c9", "09c6e58a", "1b62725d")
    #: 原布局：（base, span）
    OWN = {"077983c9": (0, 51), "09c6e58a": (51, 165), "1b62725d": (216, 1)}
    #: 实测：每族顶点**实际引用**到的全局骨号（块外那些 = 借用）
    REFS = {
        "077983c9": tuple(range(0, 51)) + (51, 146, 147, 216),
        "09c6e58a": tuple(range(51, 216)) + (0,),
        "1b62725d": (216, 0),
    }
    #: 新布局：（base, span, block_slots）
    NEW = {"077983c9": (0, 51, 55), "09c6e58a": (55, 165, 166), "1b62725d": (221, 1, 2)}
    #: 实测借用行：（目标槽, 来源族序号 rank, 来源族**局部**槽）
    BORROW_ROWS = {
        "077983c9": [(51, 1, 0), (52, 1, 95), (53, 1, 96), (54, 2, 0)],
        "09c6e58a": [(220, 0, 0)],
        "1b62725d": [(222, 0, 0)],
    }
    TOTAL = 223

    # -- 构造 -------------------------------------------------------------- #

    @classmethod
    def parts(cls):
        return [
            _runtime.HIMIRebindPart(
                bare=f"{family}-1-0",
                draw_ib=family,
                slot_values=tuple(range(base, base + span)),
                referenced_slots=cls.REFS[family],
            )
            for family, (base, span) in cls.OWN.items()
        ]

    def ranges(self):
        return _runtime.compute_family_ranges(
            self.parts(), 219, complete=False, borrowed_slots=self.REFS
        )

    # -- 布局 -------------------------------------------------------------- #

    def test_layout_appends_borrow_slots_at_block_tail(self):
        """块 = 自己的注册槽（原顺序）+ 块尾借用槽；总槽数由新布局决定。"""
        ranges = self.ranges()
        for family, (base, span, block) in self.NEW.items():
            info = dict(ranges[family])
            self.assertEqual(
                (info["base"], info["slots"], info["block_slots"], info["own_base"]),
                (base, span, block, self.OWN[family][0]),
                f"{family}: 新块基址/自有 span/块长/原基址必须与实测一致",
            )
            self.assertTrue(info["own_block"], f"{family}: 有注册槽的族必须有专属块")
            self.assertEqual(
                len(info["borrow"]), block - span, f"{family}: 块尾槽数 = 借用行数"
            )
        self.assertEqual(_runtime.ranges_slot_count(ranges), self.TOTAL)
        self.assertEqual(
            _runtime.family_block_slots(ranges), set(range(self.TOTAL)),
            "每槽恰有一个写方：块（含块尾借用槽）铺满整个新骨架",
        )
        self.assertEqual(
            sum(int(dict(info)["block_slots"]) for info in ranges.values()),
            self.TOTAL,
            "各块首尾相接、铺满新骨架（借用槽不另开区间）",
        )
        self.assertEqual(
            [dict(ranges[name])["family_index"] for name in self.FAMILIES], [0, 1, 2],
            "rank = 族按 base 升序的序号",
        )

    def test_borrow_targets_ranks_and_source_local_slots(self):
        """借用行：目标槽在块尾、rank 指向来源族、第三分量是来源族的**局部**槽。"""
        rows_by_family = _runtime.borrow_map_rows_by_family(self.ranges())
        self.assertEqual(
            {family: list(rows) for family, rows in rows_by_family.items()},
            self.BORROW_ROWS,
            "目标槽 / rank / 源局部槽必须与实测借用表一致",
        )

    def test_borrow_map_payload_and_decl(self):
        """借用表字节：每行 ``<4I`` = (目标槽, rank, 源局部槽, 0)；空表一行哨兵。"""
        ranges = self.ranges()
        for family, rows in self.BORROW_ROWS.items():
            payload = _runtime.borrow_map_payload(rows)
            self.assertEqual(len(payload), len(rows) * _runtime.BORROW_MAP_ROW_BYTES)
            decoded = [
                struct.unpack_from("<4I", payload, index * 16) for index in range(len(rows))
            ]
            self.assertEqual(decoded, [tuple(row) + (0,) for row in rows])
            decl = _runtime.build_borrow_map_decl(family, len(rows))
            self.assertEqual(decl.name, f"ResourceHI3BorrowMap_{family}")
            self.assertEqual(int(decl.array), len(rows), f"{family}: ini array = 该族行数")
            self.assertEqual(int(decl.stride), _runtime.BORROW_MAP_STRIDE)
            self.assertEqual(
                decl.ini_format,
                "R32G32B32A32_UINT",
                f"{family}: 4 分量 typed 视图（与 HLSL 的 Buffer<uint4> 元素同宽）；"
                "R32_UINT 是 1 分量视图 + 4 分量元素，实机 GetDimensions 只回 60/216 行",
            )
        empty = _runtime.borrow_map_payload(())
        self.assertEqual(
            struct.unpack("<4I", empty),
            (_runtime.BORROW_MAP_EMPTY_SENTINEL, 0, 0, 0),
            "没有借用的族必须写一行哨兵（文件永远存在，ini 的 filename 不会指空）",
        )
        self.assertEqual(ranges["1b62725d"]["slots"], 1, "1b6 只有 1 个自有槽")

    def test_source_palette_binding_follows_base_order(self):
        """rank → cs-tN + palette 别名：0→t2、1→t3、2→t5（t4 给借用表、t0 本族、t1 身份）。"""
        ranges = self.ranges()
        self.assertEqual(_runtime.borrow_palette_families(ranges), list(self.FAMILIES))
        expected = [(0, "a", "t2"), (1, "b", "t3"), (2, "c", "t5")]
        for rank, suffix, register in expected:
            self.assertEqual(_runtime.borrow_palette_hlsl_name(rank), f"himi_borrow_palette_{suffix}")
            self.assertEqual(_runtime.borrow_palette_register(rank), register)

    def test_attach_range_payload_keeps_identity_encoding(self):
        """身份载荷：``[0]`` 是 **uint 位** (base, span, version, magic)，``[7]`` 是物体矩阵表序号。"""
        ranges = self.ranges()
        for index, family in enumerate(self.FAMILIES):
            info = dict(ranges[family])
            payload = _runtime.attach_range_payload(
                int(info["base"]), int(info["slots"]), indices=(index, index)
            )
            self.assertEqual(len(payload), _runtime.ATTACH_RANGE_BYTES)
            self.assertEqual(
                struct.unpack_from("<4I", payload, 0),
                (
                    int(info["base"]),
                    int(info["slots"]),
                    _runtime.ATTACH_RANGE_VERSION,
                    _runtime.ATTACH_RANGE_MAGIC,
                ),
                f"{family}: 魔数必须是整型位（写成 float 会让 CS 的 identity 门整族拒写）",
            )
            self.assertEqual(
                struct.unpack_from("<4I", payload, _runtime.ATTACH_RANGE_INDICES_OFFSET),
                (index, index, 0, 0),
                f"{family}: [7] = (本族序号, 宿主族序号) —— 是**表序号**，不是新基址",
            )
            self.assertEqual(
                struct.unpack_from("<4f", payload, 16), (1.0, 0.0, 0.0, 0.0),
                "[1..3] 方案 B 恒为单位阵",
            )

    def test_base_table_is_generated_from_the_new_layout(self):
        """冻结基址表的键是**源族 span**、值是**新基址**（旧 0/51/216 必须全被替换）。"""
        ranges = self.ranges()
        lines = _himi_exporter.merged_attach_base_table_lines(ranges)
        table = {}
        for line in lines:
            match = re.search(r"source_slots == (\d+)u\)[^{]*\{ base_slot = (\d+)u;", line)
            if match:
                table[int(match.group(1))] = int(match.group(2))
        self.assertEqual(
            table,
            {51: 0, 165: 55, 1: 221},
            "表行必须由这份布局生成（键 = 源族 span，值 = 新基址）",
        )
        self.assertEqual(
            _himi_exporter.merged_attach_base_table_audit(ranges, self.TOTAL)["problems"], []
        )

    def test_blend_slot_map_points_borrows_at_the_block_tail(self):
        """Blend 重映射：本族槽搬到新基址、跨族借用槽落到本族块尾。"""
        table = _runtime.family_slot_map(self.ranges())
        self.assertEqual(
            {slot: table["077983c9"][slot] for slot in (0, 50, 51, 146, 147, 216)},
            {0: 0, 50: 50, 51: 51, 146: 52, 147: 53, 216: 54},
            "077：自有槽原位（base 0），借 09c 的 0/95/96 与 1b6 的 0 落在块尾",
        )
        self.assertEqual(
            {slot: table["09c6e58a"][slot] for slot in (51, 215, 0)},
            {51: 55, 215: 219, 0: 220},
            "09c：自有块搬 55 起，借 077 的槽 0 落在块尾 220",
        )
        self.assertEqual(
            table["1b62725d"], {216: 221, 0: 222}, "1b6：自有槽 216→221，借 077 的 0→222"
        )

    def test_layout_is_unchanged_without_borrows(self):
        """没有借用时一个字段都不改（= 今天导出的非合并/无借用布局逐字节等价）。

        注意槽数取 **217**：原布局 219 槽里有 2 个（217/218）不属于这三族 —— 那是 t91
        时代「全骨架尾部借用区」的残留。无借用复算只关心「三族注册槽首尾相接」这一条，
        所以这里按 217 起算；带借用那份的 223 槽是独立的（由 reference 集推出）。
        """
        plain = _runtime.compute_family_ranges(self.parts(), 217, complete=True)
        self.assertEqual(
            plain,
            {
                family: {"base": base, "slots": span, "own_block": True}
                for family, (base, span) in self.OWN.items()
            },
            "无借用 ⇒ 区间 / 槽数 / 键集与旧排布完全一致（没有 block_slots/borrow 这些新键）",
        )
        self.assertEqual(_runtime.ranges_slot_count(plain), 217)
        self.assertEqual(
            _runtime.family_block_slots(plain), set(range(217)), "无借用的块 = 注册槽本身"
        )
        self.assertEqual(
            {family: list(rows) for family, rows in _runtime.borrow_map_rows_by_family(plain).items()},
            {family: [] for family in self.OWN},
        )
        self.assertEqual(
            _himi_exporter.merged_attach_base_table_lines(plain),
            _himi_exporter.merged_attach_base_table_lines(
                _runtime.compute_family_ranges(self.parts(), 217, complete=True)
            ),
        )

    # -- 校验钩子（第 5 条：导出期必须能失败得响） ------------------------- #

    def test_borrow_map_gate_rejects_bad_rows(self):
        """借用表判据：目标槽必须在块尾、rank 合法且非本族、源局部槽 < 源族 span。"""
        ranges = self.ranges()
        good = _runtime.borrow_map_rows_by_family(ranges)
        self.assertEqual(_runtime.borrow_map_layout_problems(ranges, good), [])

        inside_span = dict(good)
        inside_span["077983c9"] = [(50, 1, 0)]           # 落在自己的 span 里 ⇒ 覆盖自有槽
        self.assertTrue(_runtime.borrow_map_layout_problems(ranges, inside_span))

        self_target = dict(good)
        self_target["09c6e58a"] = [(223, 0, 0)]          # 目标槽 = 总槽数 ⇒ 越界
        self.assertTrue(_runtime.borrow_map_layout_problems(ranges, self_target))

        bad_local = dict(good)
        bad_local["077983c9"] = [(51, 1, 165)]           # 09c 只有 165 个局部槽 [0,165)
        self.assertTrue(_runtime.borrow_map_layout_problems(ranges, bad_local))

        bad_rank = dict(good)
        bad_rank["1b62725d"] = [(222, 3, 0)]             # 只有 3 个族
        self.assertTrue(_runtime.borrow_map_layout_problems(ranges, bad_rank))

        self_rank = dict(good)
        self_rank["1b62725d"] = [(222, 2, 0)]            # 借自己
        self.assertTrue(_runtime.borrow_map_layout_problems(ranges, self_rank))

    def test_attach_range_gate_rejects_overlap_and_overflow(self):
        """身份资源判据：块两两不重叠、不越出总槽数（借用槽算在块长里）。"""
        ranges = self.ranges()
        self.assertEqual(
            _runtime.attach_range_layout_problems(ranges, skeleton_slots=self.TOTAL), []
        )
        self.assertTrue(
            _runtime.attach_range_layout_problems(ranges, skeleton_slots=self.TOTAL - 1),
            "少给一槽 ⇒ 最后一块越界必须判红",
        )
        overlap = {family: dict(info) for family, info in ranges.items()}
        overlap["09c6e58a"]["base"] = 54                 # 与 077 的块尾 [51,55) 重叠
        self.assertTrue(_runtime.attach_range_layout_problems(overlap, skeleton_slots=self.TOTAL))

    def test_remap_gate_rejects_unmapped_and_out_of_block(self):
        """重映射判据：未映射通道数必须为 0，且每个索引都落在本族块内。"""
        ranges = self.ranges()
        clean = {"077983c9": {0, 50, 51, 52, 53, 54}, "1b62725d": {221, 222}}
        self.assertEqual(
            _runtime.borrow_blend_remap_problems({"unmapped": 0}, clean, ranges), []
        )
        leftover = _runtime.borrow_blend_remap_problems({"unmapped": 3}, clean, ranges)
        self.assertEqual(len(leftover), 1)
        self.assertIn("3 个通道", leftover[0])
        stray = _runtime.borrow_blend_remap_problems(
            {"unmapped": 0}, {"077983c9": {0, 55, 220}}, ranges
        )
        self.assertEqual(len(stray), 1)
        self.assertIn("本族块 [0,55)", stray[0])
        self.assertIn("55", stray[0])

    def test_publish_writes_one_borrow_map_per_family(self):
        """落盘：每族一张借用表（空表 1 行哨兵）+ 128 B 身份资源；行数 = 该族借用槽数。"""
        ranges = self.ranges()
        with tempfile.TemporaryDirectory() as folder:
            result = _runtime.publish_attach_ranges(
                ranges, folder, skeleton_slots=self.TOTAL
            )
            self.assertTrue(result["ok"], result.get("message"))
            self.assertEqual(
                {item["family"]: item["rows"] for item in result["borrow_maps"]},
                {family: len(rows) for family, rows in self.BORROW_ROWS.items()},
                "每族行数 = 借用槽数（ini 的 array 就按这个数声明）",
            )
            for family, rows in self.BORROW_ROWS.items():
                path = os.path.join(folder, _runtime.borrow_map_basename(family))
                with open(path, "rb") as handle:
                    blob = handle.read()
                self.assertEqual(blob, _runtime.borrow_map_payload(rows), f"{family}: 字节必须可复现")
                self.assertEqual(len(blob) // _runtime.BORROW_MAP_ROW_BYTES, len(rows))
            for family in self.FAMILIES:
                path = os.path.join(folder, _runtime.attach_range_basename(family))
                self.assertEqual(os.path.getsize(path), _runtime.ATTACH_RANGE_BYTES)

    def test_validation_hooks_have_no_false_positive_on_baseline(self):
        """基准布局必须一次过全部门（否则导出期会 Fatal）。"""
        ranges = self.ranges()
        self.assertEqual(
            _runtime.borrow_map_layout_problems(ranges, _runtime.borrow_map_rows_by_family(ranges)),
            [],
        )
        self.assertEqual(
            _runtime.attach_range_layout_problems(ranges, skeleton_slots=self.TOTAL), []
        )
        decl = _runtime.build_merged_skeleton_decl("role_06c391d0", self.TOTAL)
        self.assertEqual(int(decl.array), self.TOTAL * 3, "合并骨架 array = 槽数 × 3")
        self.assertEqual(int(decl.slots), self.TOTAL)


def himi_runtime_skeleton_name_placeholder() -> str:
    """占位（避免在测试里硬编码骨架资源名）。"""
    return "uint dst = target_slot * 3u;"



# --------------------------------------------------------------------------- #
# 【t95】局部骨骼编号回基址：与借用布局解耦
# --------------------------------------------------------------------------- #


class HIMILocalIndexRebaseTests(HIMIMergedRuntimeBase):
    """块基址 > 0 的族，Blend 里写的是**局部**编号时必须加回基址。

    为什么单列一个类：这是 13:54 产物「模型爆炸」的主因 —— ``09c6e58a`` 的块基址是 51，
    它的 Blend 里却写着局部 0..164（93.93% 的行），而合并骨架被宿主族绑上去之后，
    这些顶点就按槽 0..164 查到了别人的骨。
    """

    def _two_family_workspace(self):
        return _make_workspace(
            self.workspace,
            "RoleA",
            [
                {
                    "bare": "aaaa1111-1-0",
                    "draw_ib": "aaaa1111",
                    "vg_count": 4,
                    "original_vertex_count": 8,
                    "family_members": ["aaaa1111-1-0"],
                    "vg_map": {str(index): index for index in range(4)},
                    "rows": 1,
                    "local_indices": [[0, 1, 2, 3]],
                },
                {
                    "bare": "bbbb2222-1-0",
                    "draw_ib": "bbbb2222",
                    "vg_count": 4,
                    "original_vertex_count": 4,
                    "family_members": ["bbbb2222-1-0"],
                    "vg_map": {str(index): index + 4 for index in range(4)},
                    "rows": 1,
                    "local_indices": [[0, 1, 2, 3]],
                },
            ],
            slot_count=SLOT_COUNT,
        )

    @staticmethod
    def _numpy_blend(rows, *, all_weights: bool = False):
        """Blend 记录：默认只让通道 0 有权重；``all_weights`` 时 4 个通道各 0.25。"""
        import numpy
        import struct

        weights = (0.25, 0.25, 0.25, 0.25) if all_weights else (1.0, 0.0, 0.0, 0.0)
        payload = bytearray()
        for indices in rows:
            payload += struct.pack("<4f", *weights)
            payload += struct.pack("<4i", *list(indices)[:4])
        return numpy.frombuffer(bytes(payload), dtype=numpy.uint8).copy()

    def _pair(self, host_rows=None, second_rows=None):
        """两族各一个模型（G1 要求本次导出覆盖全部已落盘部件，否则一律不重绑）。"""
        host = _drawib_model(
            "aaaa1111", [_submesh("aaaa1111-1-0", vg_count=4)], draw_number=1, blend_rows=None
        )
        second = _drawib_model(
            "bbbb2222", [_submesh("bbbb2222-1-0", vg_count=4)], draw_number=1, blend_rows=None
        )
        host.category_buffer_dict = {"Blend": self._numpy_blend(host_rows or [[0, 1, 2, 3]])}
        second.category_buffer_dict = {"Blend": self._numpy_blend(second_rows or [[0, 1, 2, 3]])}
        return host, second

    def test_rebased_without_any_borrow_and_rebind_unblocked(self):
        self._two_family_workspace()
        host, model = self._pair()

        # 死锁的一半：局部编号的产物被判 local ⇒ 不重绑（此时合并骨架压根不会生效）
        probe = self.make_exporter([host, model])
        self.assertIsNone(probe._himi_rebind_for(model))
        self.assertEqual(probe.himi_blend_namespace_verdicts()["bbbb2222"]["verdict"], "local")

        model.category_buffer_dict = {
            "Blend": self._numpy_blend([[0, 1, 2, 3]], all_weights=True)
        }
        exporter = self.make_exporter([host, model])
        ranges = dict(exporter.himi_merged_runtime_plan().family_ranges)
        info = dict(ranges["bbbb2222"])
        self.assertEqual(int(info["base"]), 4, "第二族的块基址必须是 4")
        self.assertFalse(list(info.get("borrow") or ()), "本夹具没有借用槽（旧 t93 门会直接早退）")

        ledger = exporter._himi_remap_merged_blend_to_blocks()
        self.assertFalse(ledger["skipped"], "没有借用表也必须跑（这是本次修的正是这一条）")
        self.assertEqual(ledger["local_rebased"], 4, "4 个局部编号必须加回基址")
        self.assertEqual(ledger["rewritten"], 1)
        self.assertEqual(ledger["passthrough"], 0, "本族只写局部编号，没有跨族全局引用")

        import struct

        raw = model.category_buffer_dict["Blend"].tobytes()
        self.assertEqual(struct.unpack_from("<4i", raw, 16), (4, 5, 6, 7))

        # 死锁的另一半：字节修正后缓存的 local 判定必须作废 ⇒ vs-t0 重绑放行
        rebind = exporter._himi_rebind_for(model)
        self.assertIsInstance(rebind, dict, "修正后必须重绑（否则合并骨架压根不生效）")
        self.assertEqual(
            exporter.himi_blend_namespace_verdicts()["bbbb2222"]["verdict"], "indeterminate"
        )

    def test_cross_family_global_references_become_borrow_rows(self):
        """【t98】导出 Blend 里的跨族全局槽必须变成**借用行**（块尾槽 + 源族 palette 副本）。

        为什么：跨族引用只出现在**池化行**上（同一个 draw 里拼进来的别族几何），它们的骨号
        是全局合并槽。旧行为（t95）把这类引用原样留着 ⇒ 运行期按借用方的对象矩阵摆位，
        那正是实机上「错位/爆炸」的那一批顶点。t98 起由发现集驱动 compute_family_ranges
        把它们排进借用方块尾，并让源族的 dispatch 写 X_借用方·P_源。
        """
        self._two_family_workspace()
        # 宿主族 base=0、4 个注册槽；它引用 4/5/7（别族的合法全局槽）⇒ 3 个借用槽
        model, second = self._pair(host_rows=[[0, 4, 5, 7]])
        model.category_buffer_dict = {"Blend": self._numpy_blend([[0, 4, 5, 7]], all_weights=True)}
        # 第二族自己已经写成全局槽（4..7 全在自己块内）⇒ 它一个借用都不需要
        second.category_buffer_dict = {
            "Blend": self._numpy_blend([[4, 5, 6, 7]], all_weights=True)
        }
        exporter = self.make_exporter([model, second])
        plan = exporter.himi_merged_runtime_plan()
        ranges = dict(plan.family_ranges)

        host = dict(ranges["aaaa1111"])
        self.assertEqual(int(host["base"]), 0)
        self.assertEqual(int(host["slots"]), 4)
        self.assertEqual(int(host["block_slots"]), 7, "4 个注册槽 + 3 个块尾借用槽")
        borrow = list(host["borrow"])
        self.assertEqual([int(row["target_slot"]) for row in borrow], [4, 5, 6], "借用槽在块尾连续")
        self.assertEqual([str(row["source_family"]) for row in borrow], ["bbbb2222"] * 3)
        self.assertEqual([int(row["source_local"]) for row in borrow], [0, 1, 3], "对应全局 4/5/7")

        # 借用方族占了 [0,7)，源族跟着右移到 [7,11)：合并骨架 8 → 11 槽
        self.assertEqual(int(dict(ranges["bbbb2222"])["base"]), 7)
        self.assertEqual(int(plan.slot_count), 11)
        self.assertEqual(int(plan.skeleton_extra_slots), 3)
        self.assertFalse(
            _runtime.borrow_map_layout_problems(
                ranges, _runtime.borrow_map_rows_by_family(ranges)
            ),
            "借用表必须自洽（否则运行期读到别人的行）",
        )
        self.assertFalse(
            _runtime.attach_range_layout_problems(ranges, skeleton_slots=int(plan.slot_count))
        )

        ledger = exporter._himi_remap_merged_blend_to_blocks()
        self.assertFalse(ledger["skipped"])
        self.assertEqual(ledger["local_rebased"], 0, "宿主族 base=0 ⇒ 恒等，局部编号不该被改")
        self.assertEqual(ledger["borrowed"], 3, "3 个跨族引用必须落到块尾借用槽")
        self.assertEqual(ledger["passthrough"], 0, "t98 起不再原样放行跨族引用")
        self.assertEqual(ledger["unmapped"], 0)
        self.assertEqual(ledger["rewritten"], 2, "两族块基址都变了（源族右移到 7）⇒ 两族都要重写")

        import struct

        raw = model.category_buffer_dict["Blend"].tobytes()
        self.assertEqual(
            struct.unpack_from("<4i", raw, 16), (0, 4, 5, 6), "4/5/7 ⇒ 块尾 4/5/6"
        )
        # 源族自己的槽 4..7 跟着块右移到 7..10（块基址 7）
        self.assertEqual(
            struct.unpack_from("<4i", second.category_buffer_dict["Blend"].tobytes(), 16),
            (7, 8, 9, 10),
        )

    def test_orphan_slot_references_abort_export(self):
        """【t98】引用「没有任何写方」的槽 ⇒ 导出 Fatal（零矩阵会把顶点拉向原点）。"""
        self._two_family_workspace()
        model, second = self._pair()
        model.category_buffer_dict = {"Blend": self._numpy_blend([[0, 99, 99, 99]], all_weights=True)}
        second.category_buffer_dict = {
            "Blend": self._numpy_blend([[4, 5, 6, 7]], all_weights=True)
        }
        Fatal = sys.modules[f"{PKG}.utils.format_utils"].Fatal
        exporter = self.make_exporter([model, second])
        with self.assertRaises(Fatal) as caught:
            exporter.himi_merged_runtime_plan()
        message = str(caught.exception)
        self.assertIn("aaaa1111", message, "报告必须指名引用方")
        self.assertIn("99", message, "报告必须列出没有写方的槽")
        self.assertIn("零矩阵", message, "报告必须说清运行期后果")
        self.assertEqual(exporter._himi_borrow_orphans, {"aaaa1111": [99]})




class HIMISharedSlotClaimTests(HIMIMergedRuntimeBase):
    """【t99】同一槽被两个家族注册时由「先注册者」独占，骨架里不会出现无人写的槽。

    为什么单列：t98 的 orphan 报告把「槽没有写方」当成可能的运行期灾难。实测这条路径
    不可达 —— load_workspace_plan 的认领循环（resolve_borrow_slots，:3015）在导出集内给
    每个注册槽只留一个拥有者，其余家族在该槽上的注册直接归零，因此每个被注册的槽都落在
    某个家族的块里。真实工作空间的槽 217（6e0ccfd4-6030-0 与三个 bbbbf903-* 都注册）
    因此有写方；此前的「owner(217)=None」是离线探针绕过了认领循环算出来的误判。
    """

    @staticmethod
    def _silent_blend():
        """权重全 0 的 Blend 记录：不产生任何加权索引，因此不干扰借用发现。"""
        import numpy
        import struct

        payload = struct.pack("<4f", 0.0, 0.0, 0.0, 0.0) + struct.pack("<4i", 0, 0, 0, 0)
        return numpy.frombuffer(payload, dtype=numpy.uint8).copy()

    def test_shared_registration_is_claimed_by_first_family(self):
        _make_workspace(
            self.workspace,
            "RoleA",
            [
                {
                    "bare": "aaaa1111-1-0",
                    "draw_ib": "aaaa1111",
                    "vg_count": 2,
                    "original_vertex_count": 4,
                    "family_members": ["aaaa1111-1-0"],
                    "vg_map": {"0": 0, "1": 1},
                    "rows": 1,
                    "local_indices": [[0, 1, 0, 0]],
                },
                {
                    "bare": "bbbb2222-1-0",
                    "draw_ib": "bbbb2222",
                    "vg_count": 1,
                    "original_vertex_count": 4,
                    "family_members": ["bbbb2222-1-0"],
                    "vg_map": {"0": 2},
                    "rows": 1,
                    "local_indices": [[0, 0, 0, 0]],
                },
                {
                    "bare": "cccc3333-1-0",
                    "draw_ib": "cccc3333",
                    "vg_count": 1,
                    "original_vertex_count": 4,
                    "family_members": ["cccc3333-1-0"],
                    "vg_map": {"0": 2},
                    "rows": 1,
                    "local_indices": [[0, 0, 0, 0]],
                },
            ],
            slot_count=3,
        )
        models = [
            _drawib_model("aaaa1111", [_submesh("aaaa1111-1-0", vg_count=2)], draw_number=1),
            _drawib_model("bbbb2222", [_submesh("bbbb2222-1-0", vg_count=1)], draw_number=1),
            _drawib_model("cccc3333", [_submesh("cccc3333-1-0", vg_count=1)], draw_number=1),
        ]
        for model in models:
            model.category_buffer_dict = {"Blend": self._silent_blend()}
        exporter = self.make_exporter(models)

        plan = exporter.himi_merged_runtime_plan()
        self.assertTrue(plan.ok, getattr(plan, "message", ""))
        ranges = {name: dict(info or {}) for name, info in dict(plan.family_ranges).items()}
        self.assertIn("bbbb2222", ranges, "先注册者（bbbb2222 < cccc3333）拿到槽 2")
        self.assertEqual(int(ranges["bbbb2222"]["slots"]), 1)
        self.assertFalse(
            bool(ranges.get("cccc3333", {}).get("own_block", False)),
            "后注册者在该槽上不再拥有块",
        )
        covered = set()
        for info in ranges.values():
            span = int(info.get("block_slots", info.get("slots", 0)) or 0)
            base = int(info.get("base", 0) or 0)
            covered.update(range(base, base + span))
        self.assertTrue(
            {0, 1, 2} <= covered,
            "每个被注册的槽都必须落在某个家族的块里（否则运行期读零矩阵）",
        )
