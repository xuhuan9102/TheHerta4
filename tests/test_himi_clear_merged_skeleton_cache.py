"""HIMI 合并骨架缓存清除入口（``SSMT_OT_ClearMergedSkeletonCache`` 逻辑名校验）单测。

背景（T6 发现的用户可见缺口）：复选框「使用融合统一顶点组」已对 HIMI 可见
（``ui/ui_panel_basic.py`` 的 ``_merged_skeleton_logic_names``），但清除缓存按钮的
``poll()`` / ``_precheck()`` 仍只认 EFMI/ZZMI ⇒ HIMI 一旦在工作空间里拿到坏掉的
合并骨架缓存，**没有任何 UI 回退路径**（复选框可见但清除按钮不可用）。

实现不需要新代码：``EFMISkeletonMergeHelper.clear_vgmap_cache`` 按「携带缓存键」判定并
清 VGMap/VGOffset/VGCount/VGMapAlgorithmVersion/SkeletonGroup，对 HIMI 组件同样有效
（HIMI 复用同一批键）。本文件钉死四件事：

1. **行为**：``poll()`` 在 EFMI/ZZMI/HIMI 下为真、其余逻辑名为假；
   ``_precheck()`` 同样按该集合拒绝，且工作空间无效时给出可读错误。
2. **两个命名集合**：复选框用 ``_merged_skeleton_logic_names()``
   （WWMI/EFMI/ZZMI/HIMI），按钮用 ``_merged_skeleton_cache_logic_names()``
   （EFMI/ZZMI/HIMI）。**两者刻意不等**：本轮只给 HIMI 开按钮这个口子，
   WWMI 保持改动前的不可用状态（按钮是破坏性操作，不该被顺带放开）；
   两个集合都必须是命名来源，不得再内联硬编码元组。
3. **只读**：真实 HIMI 工作空间上只验证 ``poll``/``_precheck``，不执行清除
   （前后对全部 json 做内容指纹比对，证明零副作用）。
4. **闭环**（临时目录副本，真实 dump 只读）：清缓存 → 元数据缺失 → 导入预生成重新
   放行 → ``ensure_skeleton_data`` 重新写出完整 VGMap 覆盖。

测试纪律：无真实 bpy。UI 侧用假 bpy + 假包树加载真实 ``ui/ui_panel_basic.py``；
数据侧按仓库既有模式（``tests/test_efmi_clear_vgmap_cache.py``）以假包前缀加载真实模块。
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
import shutil
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

UI_PKG = "himi_clear_cache_ui_test_pkg"
DATA_PKG = "himi_clear_cache_data_test_pkg"

REAL_WORKSPACE_ROOT = Path(r"K:\SSMT-Package-master\WorkSpace\HIMI")

#: 闭环用例使用的角色与组件。选 ``af17c02f-11772-0``：它是本工作空间里
#: ``af17c02f`` 家族的**唯一**成员（另一家族 ``a391eadc`` 有两个成员，单拷贝会被
#: G1 家族门控拒绝），因此「只拷贝一个组件」的临时工作空间仍能完整走通。
LOOP_ROLE = "咚！炽愿吉星"
LOOP_COMPONENT = "af17c02f-11772-0"
LOOP_UNIQUE_STR = f"LOD0.{LOOP_COMPONENT}"


# --------------------------------------------------------------------------- #
# 通用加载器
# --------------------------------------------------------------------------- #


def _install_package(name):
    module = types.ModuleType(name)
    module.__path__ = []
    sys.modules[name] = module
    return module


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _load_module(qualname, path):
    spec = importlib.util.spec_from_file_location(qualname, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[qualname] = module
    spec.loader.exec_module(module)
    return module


# --------------------------------------------------------------------------- #
# 1) UI 侧：假 bpy + 假包树，加载真实的 ui/ui_panel_basic.py
# --------------------------------------------------------------------------- #


class _FakeOperatorBase:
    """最小 Operator 基类：只提供 report()（面板代码不依赖其它 bpy 行为）。"""

    bl_idname = ""
    bl_label = ""
    bl_description = ""
    bl_options = set()

    def __init__(self):
        self.reports = []

    def report(self, level, message):
        self.reports.append((level, str(message)))


class _FakePanelBase:
    bl_idname = ""
    bl_label = ""


def _install_fake_bpy():
    bpy_stub = types.ModuleType("bpy")
    bpy_stub.types = types.SimpleNamespace(
        Operator=_FakeOperatorBase,
        Panel=_FakePanelBase,
    )
    bpy_stub.props = types.SimpleNamespace(
        BoolProperty=lambda *args, **kwargs: None,
        StringProperty=lambda *args, **kwargs: None,
        EnumProperty=lambda *args, **kwargs: None,
        IntProperty=lambda *args, **kwargs: None,
        FloatProperty=lambda *args, **kwargs: None,
    )
    bpy_stub.utils = types.SimpleNamespace(
        register_class=lambda cls: None,
        unregister_class=lambda cls: None,
    )
    sys.modules["bpy"] = bpy_stub
    return bpy_stub


class _FakeGlobalConfig:
    """可控的 GlobalConfig 替身（面板只读 logic_name 与工作空间路径）。"""

    logic_name = "HIMI"
    _workspace_folder = ""

    @classmethod
    def path_workspace_folder(cls):
        return cls._workspace_folder


_install_fake_bpy()

for _name in (UI_PKG, f"{UI_PKG}.common", f"{UI_PKG}.ui", f"{UI_PKG}.blueprint", f"{UI_PKG}.utils"):
    _install_package(_name)

# 真实 LogicName（无 bpy 依赖）—— 逻辑名常量必须与生产一致，不能自造
_load_module(f"{UI_PKG}.common.logic_name", REPO_ROOT / "common" / "logic_name.py")

_install_module(f"{UI_PKG}.common.global_config", GlobalConfig=_FakeGlobalConfig)
_install_module(
    f"{UI_PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(),
)
# 补齐贴图标记入口（main 的 ui_panel_basic 顶层 import 比 WIP 分支快照多这一个）：
# 本文件只测清除缓存按钮的可见性/门控，桩掉即可。
_install_module(
    f"{UI_PKG}.common.marked_texture_material",
    build_missing_marked_materials=lambda *args, **kwargs: ([], []),
)
_install_module(
    f"{UI_PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=types.SimpleNamespace(),
)
_install_module(
    f"{UI_PKG}.common.workspace_helper",
    WorkSpaceHelper=types.SimpleNamespace(),
)
# 清理 IB 时的配置引用清理器（ui_panel_basic 顶层 import）：本文件只测清除缓存按钮，
# 桩掉即可（真实实现的单测见 tests/test_workspace_config_prune.py）。
_install_module(
    f"{UI_PKG}.common.workspace_config_pruner",
    WorkspaceConfigPruner=types.SimpleNamespace(prune=lambda *args, **kwargs: {}),
)
_install_module(
    f"{UI_PKG}.blueprint.export_helper",
    BlueprintExportHelper=types.SimpleNamespace(),
)
_install_module(f"{UI_PKG}.utils.translate_utils", TR=types.SimpleNamespace())
_install_module(
    f"{UI_PKG}.ui.ui_func_import_ssmt",
    SSMT4ImportAllFromCurrentWorkSpaceBlueprint=type("_OpA", (), {}),
    SSMT4ImportRaw=type("_OpB", (), {}),
)
_install_module(f"{UI_PKG}.ui.ui_prefix_quick_ops")
_install_module(
    f"{UI_PKG}.ui.ui_func_export",
    SSMTGenerateModBlueprint=type("_OpC", (), {}),
    SSMTQuickExportSelected=type("_OpD", (), {}),
)
_install_module(f"{UI_PKG}.blueprint.preprocess_cache", PreProcessCache=types.SimpleNamespace())
_install_module(
    f"{UI_PKG}.blueprint.preprocess_parallel",
    ParallelPreprocessCoordinator=types.SimpleNamespace(),
)

_panel = _load_module(f"{UI_PKG}.ui.ui_panel_basic", REPO_ROOT / "ui" / "ui_panel_basic.py")

LogicName = sys.modules[f"{UI_PKG}.common.logic_name"].LogicName
GlobalConfig = _FakeGlobalConfig
ClearMergedSkeletonCache = _panel.SSMT_OT_ClearMergedSkeletonCache
merged_skeleton_logic_names = _panel._merged_skeleton_logic_names
merged_skeleton_cache_logic_names = _panel._merged_skeleton_cache_logic_names

PANEL_SOURCE_PATH = REPO_ROOT / "ui" / "ui_panel_basic.py"
PANEL_SOURCE = PANEL_SOURCE_PATH.read_text(encoding="utf-8")


# --------------------------------------------------------------------------- #
# 2) 数据侧：假包前缀加载真实 common/himi_skeleton.py 与 common/efmi_skeleton.py
# --------------------------------------------------------------------------- #

for _name in (DATA_PKG, f"{DATA_PKG}.common", f"{DATA_PKG}.utils"):
    _install_package(_name)
_load_module(f"{DATA_PKG}.utils.json_utils", REPO_ROOT / "utils" / "json_utils.py")
_himi = _load_module(
    f"{DATA_PKG}.common.himi_skeleton", REPO_ROOT / "common" / "himi_skeleton.py"
)
_efmi = _load_module(
    f"{DATA_PKG}.common.efmi_skeleton", REPO_ROOT / "common" / "efmi_skeleton.py"
)

HIMISkeletonMergeHelper = _himi.HIMISkeletonMergeHelper
EFMISkeletonMergeHelper = _efmi.EFMISkeletonMergeHelper
HIMI_VG_MAP_ALGORITHM_VERSION = _himi.HIMI_VG_MAP_ALGORITHM_VERSION
PALETTE_STRIDE_BYTES = _himi.PALETTE_STRIDE_BYTES


# --------------------------------------------------------------------------- #
# 3) 辅助
# --------------------------------------------------------------------------- #


def _read_json(path) -> dict:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _fingerprint_json_tree(root: Path) -> dict:
    """工作空间内全部 json 的内容指纹（只读；用于证明零副作用）。"""
    prints = {}
    for path in sorted(root.rglob("*.json")):
        payload = path.read_bytes()
        prints[str(path.relative_to(root))] = (
            len(payload),
            hashlib.sha256(payload).hexdigest(),
        )
    return prints


def _real_role_workspace(role: str = LOOP_ROLE) -> Path:
    return REAL_WORKSPACE_ROOT / role


def _real_dump_dir(workspace: Path) -> Path:
    """按工作空间登记的 Config/FrameAnalysisPath.json 解析真实 dump 目录。"""
    config = workspace / "Config" / "FrameAnalysisPath.json"
    if not config.is_file():
        return Path("")
    payload = _read_json(config)
    return Path(str(payload.get("frameAnalysisFolderPath", "") or ""))


# --------------------------------------------------------------------------- #
# 4) 逻辑名门控（行为）
# --------------------------------------------------------------------------- #


class ClearCacheLogicGateTests(unittest.TestCase):
    """poll()/_precheck() 的逻辑名集合。"""

    def setUp(self):
        GlobalConfig._workspace_folder = ""
        GlobalConfig.logic_name = "HIMI"

    def tearDown(self):
        GlobalConfig._workspace_folder = ""
        GlobalConfig.logic_name = "HIMI"

    def _precheck(self, logic_name, workspace=""):
        GlobalConfig.logic_name = logic_name
        GlobalConfig._workspace_folder = workspace
        return ClearMergedSkeletonCache()._precheck(None)

    def test_himi_poll_true_and_precheck_passes_on_valid_workspace(self):
        """HIMI：按钮可用，且工作空间有效时 _precheck 放行（返回 None）。"""
        workspace = _real_role_workspace()
        if not workspace.is_dir():
            self.skipTest(f"missing {workspace}")
        GlobalConfig.logic_name = LogicName.HIMI
        GlobalConfig._workspace_folder = str(workspace)
        self.assertTrue(ClearMergedSkeletonCache.poll(None))
        self.assertIsNone(ClearMergedSkeletonCache()._precheck(None))

    def test_button_logics_are_efmi_zzmi_himi_only(self):
        """按钮集合 = EFMI / ZZMI / HIMI（HIMI 为本次新增，其余与改动前逐字相同）。"""
        for logic_name in (LogicName.EFMI, LogicName.ZZMI, LogicName.HIMI):
            with self.subTest(logic_name=logic_name):
                GlobalConfig.logic_name = logic_name
                self.assertTrue(ClearMergedSkeletonCache.poll(None))

    def test_wwmi_is_deliberately_kept_out_of_the_button(self):
        """WWMI 刻意不在按钮集合里：本轮只给 HIMI 开这个口子。

        按钮是破坏性操作（删工作空间 json 的 VGMap/VGOffset/VGCount），
        改动前 WWMI 就没有它；HIMI 工作不得顺带改变 WWMI 的可用性。
        复选框那边 WWMI 仍然可见（见下一条断言），这是改动前就有的状态。
        """
        GlobalConfig.logic_name = LogicName.WWMI
        self.assertFalse(ClearMergedSkeletonCache.poll(None))
        self.assertIn(LogicName.WWMI, merged_skeleton_logic_names())

    def test_other_logics_are_disabled(self):
        """其余逻辑名一律不可用（含 ZZMI 家族之外的近亲逻辑）。"""
        for logic_name in (
            "GIMI",
            "SRMI",
            "YYSLS",
            "IdentityV",
            "SnowBreak",
            "Naraka",
            "NarakaM",
            "GF2",
            "AILIMIT",
            "DOAV",
            "HTMI",
            "NTEMI",
            "ZZMIDX12",
            "APMI",
            "NEMI",
            "",
        ):
            with self.subTest(logic_name=logic_name):
                GlobalConfig.logic_name = logic_name
                self.assertFalse(ClearMergedSkeletonCache.poll(None))

    def test_precheck_rejects_other_logics_with_readable_message(self):
        error = self._precheck("GIMI", str(REPO_ROOT))
        self.assertIsNotNone(error)
        self.assertIn("HIMI", error)

    def test_precheck_reports_invalid_workspace_for_himi(self):
        """HIMI 下工作空间无效时给出可读错误，而不是静默放行去扫盘。"""
        for workspace in ("", str(REPO_ROOT / "不存在的目录")):
            with self.subTest(workspace=workspace):
                error = self._precheck(LogicName.HIMI, workspace)
                self.assertIsNotNone(error)
                self.assertIn("工作空间", error)

    def test_shared_source_of_truth_names(self):
        """两个集合的内容与顺序固定；getattr 兜底让子集 LogicName 桩也不炸。

        复选框集合（含 WWMI）与按钮集合（刻意不含 WWMI）是两个**命名**来源：
        按钮集合若被悄悄扩成复选框集合，本用例会失败并提醒同步口径。
        """
        self.assertEqual(
            merged_skeleton_logic_names(),
            (
                LogicName.WWMI,
                LogicName.EFMI,
                LogicName.ZZMI,
                LogicName.HIMI,
                "APMI",
            ),
        )
        self.assertEqual(
            merged_skeleton_cache_logic_names(),
            (
                LogicName.EFMI,
                LogicName.ZZMI,
                LogicName.HIMI,
            ),
        )
        subset = types.SimpleNamespace(HIMI="HIMI")
        original = _panel.LogicName
        try:
            _panel.LogicName = subset
            self.assertEqual(merged_skeleton_logic_names(), ("WWMI", "EFMI", "ZZMI", "HIMI", "APMI"))
            self.assertEqual(merged_skeleton_cache_logic_names(), ("EFMI", "ZZMI", "HIMI"))
        finally:
            _panel.LogicName = original

    def test_description_mentions_himi_and_not_wwmi(self):
        """描述必须与按钮集合一致：提 HIMI，不提已刻意排除的 WWMI。"""
        self.assertIn("HIMI", ClearMergedSkeletonCache.bl_description)
        self.assertNotIn("WWMI", ClearMergedSkeletonCache.bl_description)


# --------------------------------------------------------------------------- #
# 5) 面板门控单一来源（源码级，防止复选框与按钮再次漂移）
# --------------------------------------------------------------------------- #


class PanelGateSingleSourceTests(unittest.TestCase):
    """复选框与清除按钮各自取自**命名**集合；两者不等是刻意的（WWMI）。"""

    CHECKBOX_GUARD = "if GlobalConfig.logic_name in _merged_skeleton_logic_names():"
    BUTTON_GUARD = "if GlobalConfig.logic_name in _merged_skeleton_cache_logic_names():"

    def test_each_panel_gate_uses_its_named_helper(self):
        """复选框用复选框集合、按钮用按钮集合，各恰好一处；都不得内联硬编码元组。"""
        stripped = [line.strip() for line in PANEL_SOURCE.splitlines()]
        self.assertEqual(
            stripped.count(self.CHECKBOX_GUARD),
            1,
            "复选框门控必须恰好调用一次 _merged_skeleton_logic_names()；"
            f"实际命中 {stripped.count(self.CHECKBOX_GUARD)} 处。",
        )
        self.assertEqual(
            stripped.count(self.BUTTON_GUARD),
            1,
            "清除按钮门控必须恰好调用一次 _merged_skeleton_cache_logic_names()；"
            f"实际命中 {stripped.count(self.BUTTON_GUARD)} 处。",
        )

    def test_panel_has_no_inline_merged_logic_tuple_left(self):
        """不得再出现「把逻辑名硬编码成元组」的门控（正是本次缺口成因）。"""
        for forbidden in (
            "GlobalConfig.logic_name in (LogicName.EFMI, LogicName.ZZMI)",
            "GlobalConfig.logic_name in (LogicName.WWMI, LogicName.ZZMI, LogicName.EFMI)",
        ):
            self.assertNotIn(forbidden, PANEL_SOURCE)

    def test_button_render_is_guarded_by_the_button_helper(self):
        """清除按钮的 layout.operator 必须落在**按钮集合**门控内。"""
        lines = PANEL_SOURCE.splitlines()
        index = next(
            i
            for i, line in enumerate(lines)
            if "SSMT_OT_ClearMergedSkeletonCache.bl_idname" in line
            and line.strip().startswith("layout.operator(")
        )
        guard = None
        for line in reversed(lines[:index]):
            if line.strip().startswith("if GlobalConfig.logic_name"):
                guard = line.strip()
                break
        self.assertEqual(guard, self.BUTTON_GUARD)


# --------------------------------------------------------------------------- #
# 6) 清除路径复用（源码级：不新实现一套 HIMI 清理）
# --------------------------------------------------------------------------- #


class ClearImplementationReuseTests(unittest.TestCase):
    def test_execute_reuses_efmi_clear_helper(self):
        source = PANEL_SOURCE
        self.assertIn("from ..common.efmi_skeleton import EFMISkeletonMergeHelper", source)
        self.assertIn("EFMISkeletonMergeHelper.clear_vgmap_cache(workspace_root)", source)

    def test_clear_helper_removes_the_keys_himi_writes(self):
        """HIMI 写回的缓存键必须都在 clear_vgmap_cache 的清理名单里。

        HIMI 的写回键见 ``common/himi_skeleton.py`` 的 ``commit``；若日后新增缓存键
        却忘了加进清理名单，这条会失败（提示更新 clear_vgmap_cache）。
        """
        efmi_source = (REPO_ROOT / "common" / "efmi_skeleton.py").read_text(encoding="utf-8")
        himi_source = (REPO_ROOT / "common" / "himi_skeleton.py").read_text(encoding="utf-8")
        for key in (
            "VGMap",
            "VGOffset",
            "VGCount",
            "VGMapAlgorithmVersion",
            "SkeletonGroup",
        ):
            with self.subTest(key=key):
                self.assertIn(f'"{key}"', himi_source, f"HIMI 写回契约应包含 {key}")
                self.assertIn(f'"{key}"', efmi_source, f"clear_vgmap_cache 名单应包含 {key}")

    def test_import_side_pregeneration_gate_consumes_missing_metadata(self):
        """闭环的另一半：导入侧 HIMI 预生成门控读的就是 missing_merged_metadata_exist。

        ``clear_vgmap_cache`` 清掉 VGMap → ``missing_merged_metadata_exist`` 为真 →
        导入预生成 ``ensure_skeleton_data`` 重新执行。三处任一处断链，用户就修不回
        坏缓存；本断言把这条链固定在源码上（配合上面的临时副本闭环用例）。
        """
        source = (REPO_ROOT / "ui" / "ui_func_import_ssmt.py").read_text(encoding="utf-8")
        self.assertIn("_HIMIMergeHelper.missing_merged_metadata_exist(", source)
        self.assertIn(
            "if himi_merged_consumption or himi_merged_metadata_missing:", source
        )
        self.assertIn("HIMISkeletonMergeHelper.ensure_skeleton_data(", source)


# --------------------------------------------------------------------------- #
# 7) 真实工作空间只读验证（不执行清除）
# --------------------------------------------------------------------------- #


@unittest.skipUnless(REAL_WORKSPACE_ROOT.is_dir(), f"missing {REAL_WORKSPACE_ROOT}")
class RealWorkspaceReadOnlyTests(unittest.TestCase):
    """真实 HIMI 工作空间：只验证判定，落盘路径不在此触发。"""

    def setUp(self):
        GlobalConfig._workspace_folder = ""
        GlobalConfig.logic_name = "HIMI"
        self.addCleanup(setattr, GlobalConfig, "_workspace_folder", "")

    def test_poll_and_precheck_are_read_only_on_every_real_role(self):
        for role_dir in sorted(REAL_WORKSPACE_ROOT.iterdir()):
            if not role_dir.is_dir():
                continue
            with self.subTest(role=role_dir.name):
                before = _fingerprint_json_tree(role_dir)
                GlobalConfig.logic_name = LogicName.HIMI
                GlobalConfig._workspace_folder = str(role_dir)
                self.assertTrue(ClearMergedSkeletonCache.poll(None))
                self.assertIsNone(ClearMergedSkeletonCache()._precheck(None))
                after = _fingerprint_json_tree(role_dir)
                self.assertEqual(
                    before,
                    after,
                    "poll/_precheck 不得修改工作空间（本用例只读验证，清除必须走显式 execute）",
                )
                self.assertGreater(len(before), 0, "真实工作空间应有可解析的 json")


# --------------------------------------------------------------------------- #
# 8) 闭环：清缓存 → 预生成重新放行 → 重新写出完整元数据（临时目录副本）
# --------------------------------------------------------------------------- #


@unittest.skipUnless(REAL_WORKSPACE_ROOT.is_dir(), f"missing {REAL_WORKSPACE_ROOT}")
class ClearRegenerateClosedLoopTests(unittest.TestCase):
    """在**临时目录副本**上跑完整闭环；真实工作空间与 dump 全程只读。"""

    def setUp(self):
        self._tmp = Path(tempfile.mkdtemp(prefix="himi_clear_loop_"))
        self.addCleanup(shutil.rmtree, self._tmp, ignore_errors=True)
        self.source_workspace = _real_role_workspace()
        if not self.source_workspace.is_dir():
            self.skipTest(f"missing {self.source_workspace}")
        self.dump_dir = _real_dump_dir(self.source_workspace)
        if not self.dump_dir.is_dir():
            self.skipTest(f"missing dump {self.dump_dir}")

        self.workspace = self._build_temp_workspace()

    # -- fixture ---------------------------------------------------------- #

    def _build_temp_workspace(self) -> Path:
        """拷贝单个组件 + Import.json + FrameAnalysisPath.json 到临时工作空间。

        只拷贝 ``af17c02f-11772-0``（该家族唯一成员）——拷贝一半家族会被 G1
        家族门控拒绝，那样测的就不是本闭环了。
        """
        source_component_dir = self.source_workspace / "LOD0" / LOOP_COMPONENT
        self.assertTrue(source_component_dir.is_dir(), f"missing {source_component_dir}")

        workspace = self._tmp / "workspace"
        (workspace / "Config").mkdir(parents=True, exist_ok=True)
        shutil.copytree(source_component_dir, workspace / "LOD0" / LOOP_COMPONENT)

        # gametype 以**组件目录里真实存在的 TYPE_*/ 目录名**为准：工作空间的
        # Import.json 可能与磁盘上的类型目录不同步（实测 K:\…\HIMI\咚！炽愿吉星
        # 上 Import.json 写 T4_T1-4_，而磁盘只有 TYPE_…_T8_），按 Import.json 推导
        # 会指向一个不存在的 json 路径、闭环测的就不是真实产物了。
        type_dirs = sorted(p for p in (workspace / "LOD0" / LOOP_COMPONENT).glob("TYPE_*") if p.is_dir())
        self.assertEqual(len(type_dirs), 1, f"组件目录下 TYPE_* 目录应唯一：{type_dirs}")
        gametype = type_dirs[0].name[len("TYPE_"):]
        (workspace / "Import.json").write_text(
            json.dumps({LOOP_UNIQUE_STR: gametype}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        shutil.copy2(
            self.source_workspace / "Config" / "FrameAnalysisPath.json",
            workspace / "Config" / "FrameAnalysisPath.json",
        )
        # 真实工作空间的这份组件**可能已经带着上一次生成的合并元数据**（研究期反复
        # 导入过）。用例①要的是"未生成"的起点，所以用生产侧的清理入口把它退回未生成
        # 状态——顺便让这份副本与「用户在面板上按过清除缓存」等价。
        EFMISkeletonMergeHelper.clear_vgmap_cache(str(workspace))
        return workspace

    def _json_path(self) -> Path:
        return self.workspace / "LOD0" / LOOP_COMPONENT / f"TYPE_{self._gametype()}" / f"{LOOP_COMPONENT}.json"

    def _gametype(self) -> str:
        return _read_json(self.workspace / "Import.json")[LOOP_UNIQUE_STR]

    def _regenerate(self):
        return HIMISkeletonMergeHelper.ensure_skeleton_data(
            str(self.workspace), [LOOP_UNIQUE_STR]
        )

    def _assert_complete_metadata(self, payload: dict):
        vg_count = int(payload.get("VGCount", 0) or 0)
        self.assertGreater(vg_count, 0)
        self.assertEqual(
            sorted(int(k) for k in payload.get("VGMap", {})),
            list(range(vg_count)),
            "VGMap 必须是 0..VGCount-1 的完整覆盖（导入侧硬要求）",
        )
        self.assertEqual(payload.get("VGOffset"), 0)
        self.assertEqual(
            int(payload.get("VGMapAlgorithmVersion", 0) or 0),
            HIMI_VG_MAP_ALGORITHM_VERSION,
        )
        self.assertIn("SkeletonGroup", payload)
        cache_name = str(payload.get("BoneMatrixFileName", "") or "")
        self.assertTrue(cache_name, "必须写出 palette 缓存文件名")
        # 用生产侧的路径解析器定位（`<bare>/ModImpRuntime/<file>`），避免在测试里
        # 硬编码目录层级；缓存必须真实落盘且大小与槽位数一致。
        cache_path = Path(
            _himi.runtime_cache_path(str(self._json_path()), LOOP_COMPONENT, cache_name)
        )
        self.assertTrue(cache_path.is_file(), f"palette 缓存缺失: {cache_path}")
        self.assertEqual(
            cache_path.stat().st_size,
            int(payload.get("BoneMatrixSlotCount", 0) or 0) * PALETTE_STRIDE_BYTES,
        )

    # -- 用例 -------------------------------------------------------------- #

    def test_clear_then_regenerate_rewrites_complete_metadata(self):
        json_path = self._json_path()
        self.assertTrue(json_path.is_file(), f"missing {json_path}")

        # ① 初始：临时副本来自真实工作空间，尚无合并元数据
        self.assertTrue(
            HIMISkeletonMergeHelper.missing_merged_metadata_exist(
                str(self.workspace), [LOOP_UNIQUE_STR]
            ),
            "未生成的组件必须被判为「元数据缺失」",
        )

        # ② 预生成：写出完整元数据
        ok, message = self._regenerate()
        self.assertTrue(ok, message)
        payload = _read_json(json_path)
        self._assert_complete_metadata(payload)
        self.assertFalse(
            HIMISkeletonMergeHelper.missing_merged_metadata_exist(
                str(self.workspace), [LOOP_UNIQUE_STR]
            )
        )

        # ③ 清除：走 UI 操作符实际调用的同一个实现
        cleaned, scanned = EFMISkeletonMergeHelper.clear_vgmap_cache(str(self.workspace))
        self.assertEqual(cleaned, 1, "临时工作空间里应恰好清掉这一个组件 json")
        self.assertGreaterEqual(scanned, 1)

        cleared = _read_json(json_path)
        for key in (
            "VGMap",
            "VGOffset",
            "VGCount",
            "VGMapAlgorithmVersion",
            "SkeletonGroup",
        ):
            with self.subTest(key=key):
                self.assertNotIn(key, cleared, f"{key} 必须被清除")
        # 清除范围之外的工作空间副本保留（palette 池缓存与去重策略无关）
        self.assertIn("BoneMatrixFileName", cleared)
        self.assertIn("BoneMatrixSlotCount", cleared)
        # 与合并缓存无关的原始字段不得被误清（真实 HIMI json 上的正向证据）
        unrelated_keys = (
            "GamePreset",
            "WorkGameType",
            "CategoryHash",
            "CategoryDrawCategoryMap",
            "GPU-PreSkinning",
            "VertexLimitVB",
            "ShapeKeysInfo",
            "CategoryBufferList",
            "IndexBufferList",
        )
        present_unrelated = [key for key in unrelated_keys if key in payload]
        self.assertGreaterEqual(
            len(present_unrelated), 8, f"真实 HIMI json 应带这些字段：{unrelated_keys}"
        )
        for key in present_unrelated:
            with self.subTest(unrelated_key=key):
                self.assertIn(key, cleared, f"{key} 与合并缓存无关，不得被清除")

        # ④ 闭环：清除后导入预生成必须重新放行（否则用户修不回来）
        self.assertTrue(
            HIMISkeletonMergeHelper.missing_merged_metadata_exist(
                str(self.workspace), [LOOP_UNIQUE_STR]
            ),
            "清除后必须重新判为元数据缺失，导入侧才会重新预生成",
        )

        # ⑤ 重新生成：完整元数据重新写出
        ok2, message2 = self._regenerate()
        self.assertTrue(ok2, message2)
        self._assert_complete_metadata(_read_json(json_path))
        self.assertFalse(
            HIMISkeletonMergeHelper.missing_merged_metadata_exist(
                str(self.workspace), [LOOP_UNIQUE_STR]
            )
        )

        # 角色级静态骨架文件（Config/<role>-MergedSkeleton.buf）非空
        skeleton_files = sorted((self.workspace / "Config").glob("*-MergedSkeleton.buf"))
        self.assertEqual(len(skeleton_files), 1)
        self.assertGreater(skeleton_files[0].stat().st_size, 0)

    def test_clear_does_not_touch_the_real_workspace(self):
        """副本上的清除不得波及真实工作空间（前后指纹一致）。"""
        before = _fingerprint_json_tree(self.source_workspace)
        ok, message = self._regenerate()
        self.assertTrue(ok, message)
        EFMISkeletonMergeHelper.clear_vgmap_cache(str(self.workspace))
        after = _fingerprint_json_tree(self.source_workspace)
        self.assertEqual(before, after)


if __name__ == "__main__":
    unittest.main()
