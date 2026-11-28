"""身份没有 LOD 前缀时的 LOD0 兜底单测。

场景（实盘 2026-09-24 报错）：旧扁平工作空间（DrawIB 目录直接铺在根目录）里
导入/手工命名的物体，前缀是裸身份 `aaaabbbb-100-0`；重新提取后工作空间变成
`LOD0/`、`LOD1/` 分层，数据在 `LOD0/aaaabbbb-100-0`。此时导出按裸身份查目录
一律落空，抛「unique_str '...' 没有找到对应的提取数据」，而数据其实就在 LOD0 下。

契约（用户裁决）：没有 LOD 前缀就**默认按 LOD0 解析** ——
1. `WorkSpaceHelper.get_submesh_folder_path`：裸身份目录优先（扁平工作空间
   行为不变），找不到再看 `LOD0/<bare>`（含分区工作空间）；
2. `check_and_get_submesh_json_path`：Import.json 的键在多 LOD 工作空间带
   LOD 前缀，裸身份要按 `LOD0.<bare>` 兜底查一次；
3. `EFMISkeletonMergeHelper._resolve_submesh_json_path`：同口径（无 bpy 版本），
   裸身份先找裸目录、整轮没命中才轮到 LOD0，避免被判成多分区歧义。
"""

import importlib.util
import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
PKG = "workspace_lod0_fallback_test_pkg"

BARE = "aaaabbbb-100-0"
LOD1_ONLY = "ccccdddd-200-0"


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


sys.modules["bpy"] = types.SimpleNamespace(
    types=types.SimpleNamespace(Collection=object),
    context=types.SimpleNamespace(scene=types.SimpleNamespace(collection=object())),
)

for _name in (PKG, f"{PKG}.common", f"{PKG}.utils", f"{PKG}.blueprint"):
    _install_package(_name)

_load_module(f"{PKG}.utils.json_utils", REPO_ROOT / "utils" / "json_utils.py")

_collection_utils = types.ModuleType(f"{PKG}.utils.collection_utils")
_collection_utils.CollectionColor = types.SimpleNamespace(Red=0)
_collection_utils.CollectionUtils = types.SimpleNamespace(
    create_new_collection=lambda *args, **kwargs: object(),
)
sys.modules[f"{PKG}.utils.collection_utils"] = _collection_utils


class _Fatal(Exception):
    pass


_format_utils = types.ModuleType(f"{PKG}.utils.format_utils")
_format_utils.Fatal = _Fatal
sys.modules[f"{PKG}.utils.format_utils"] = _format_utils


class _GlobalConfig:
    workspace_folder = ""

    @classmethod
    def path_workspace_folder(cls):
        return _GlobalConfig.workspace_folder


_config_module = types.ModuleType(f"{PKG}.common.global_config")
_config_module.GlobalConfig = _GlobalConfig
sys.modules[f"{PKG}.common.global_config"] = _config_module

_export_helper = types.ModuleType(f"{PKG}.blueprint.export_helper")
_export_helper.BlueprintExportHelper = types.SimpleNamespace(
    get_datatype_node_info=lambda: [],
)
sys.modules[f"{PKG}.blueprint.export_helper"] = _export_helper

_node_datatype = types.ModuleType(f"{PKG}.blueprint.node_datatype")
_node_datatype.reset_datatype_override_log = lambda: None
_node_datatype.build_override_element_list = lambda *args, **kwargs: None
sys.modules[f"{PKG}.blueprint.node_datatype"] = _node_datatype

_gametype = types.ModuleType(f"{PKG}.common.d3d11_gametype")
_gametype.D3D11GameType = types.SimpleNamespace(
    from_submesh_json_dict=lambda **kwargs: None,
)
sys.modules[f"{PKG}.common.d3d11_gametype"] = _gametype

_submesh_json = types.ModuleType(f"{PKG}.common.submesh_json")
_submesh_json.SubmeshJson = types.SimpleNamespace()
sys.modules[f"{PKG}.common.submesh_json"] = _submesh_json

_workspace = _load_module(
    f"{PKG}.common.workspace_helper", REPO_ROOT / "common" / "workspace_helper.py"
)
WorkSpaceHelper = _workspace.WorkSpaceHelper

_submesh_metadata = _load_module(
    f"{PKG}.common.submesh_metadata", REPO_ROOT / "common" / "submesh_metadata.py"
)
check_and_get_submesh_json_path = _submesh_metadata.check_and_get_submesh_json_path

_efmi_skeleton = _load_module(
    f"{PKG}.common.efmi_skeleton", REPO_ROOT / "common" / "efmi_skeleton.py"
)
EFMISkeletonMergeHelper = _efmi_skeleton.EFMISkeletonMergeHelper
EFMIBoneMapBuilder = _efmi_skeleton.EFMIBoneMapBuilder


class Lod0FallbackTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        _GlobalConfig.workspace_folder = str(self.root) + os.sep

    def tearDown(self):
        _GlobalConfig.workspace_folder = ""
        self._tmp.cleanup()

    def _make_submesh(self, rel_path, gametype="GPU-X", with_json=True, extra_type=None):
        folder = self.root / rel_path
        type_dir = folder / ("TYPE_" + gametype)
        type_dir.mkdir(parents=True, exist_ok=True)
        if with_json:
            (type_dir / (folder.name + ".json")).write_text("{}", encoding="utf-8")
        if extra_type:
            extra_dir = folder / ("TYPE_" + extra_type)
            extra_dir.mkdir(parents=True, exist_ok=True)
            (extra_dir / (folder.name + ".json")).write_text("{}", encoding="utf-8")
        return folder

    def _write_import_json(self, mapping, base=None):
        import json as _json

        target = Path(base or self.root) / "Import.json"
        target.write_text(_json.dumps(mapping, ensure_ascii=False), encoding="utf-8")

    # ---------------- 目录兜底 ----------------

    def test_bare_identity_falls_back_to_lod0_folder(self):
        lod0 = self._make_submesh(f"LOD0/{BARE}")
        resolved = WorkSpaceHelper.get_submesh_folder_path(BARE)
        self.assertEqual(os.path.normcase(resolved), os.path.normcase(str(lod0)))

    def test_flat_workspace_bare_folder_keeps_priority(self):
        flat = self._make_submesh(BARE)
        self._make_submesh(f"LOD0/{BARE}")
        resolved = WorkSpaceHelper.get_submesh_folder_path(BARE)
        self.assertEqual(os.path.normcase(resolved), os.path.normcase(str(flat)))

    def test_no_lod0_folder_keeps_old_missing_path(self):
        self._make_submesh(f"LOD1/{LOD1_ONLY}")
        resolved = WorkSpaceHelper.get_submesh_folder_path(LOD1_ONLY)
        self.assertEqual(
            os.path.normcase(resolved),
            os.path.normcase(os.path.join(str(self.root), LOD1_ONLY)),
        )
        self.assertFalse(os.path.isdir(resolved))

    def test_lod_prefixed_identity_is_untouched(self):
        lod1 = self._make_submesh(f"LOD1/{BARE}")
        self._make_submesh(f"LOD0/{BARE}")
        resolved = WorkSpaceHelper.get_submesh_folder_path(f"LOD1.{BARE}")
        self.assertEqual(os.path.normcase(resolved), os.path.normcase(str(lod1)))

        missing = WorkSpaceHelper.get_submesh_folder_path(f"LOD3.{BARE}")
        self.assertEqual(
            os.path.normcase(missing),
            os.path.normcase(os.path.join(str(self.root), "LOD3", BARE)),
        )

    def test_partition_workspace_falls_back_to_lod0(self):
        partition = self.root / "角色A"
        partition.mkdir(parents=True, exist_ok=True)
        (partition / "Config.json").write_text("[]", encoding="utf-8")
        lod0 = partition / "LOD0" / BARE
        type_dir = lod0 / "TYPE_GPU-X"
        type_dir.mkdir(parents=True, exist_ok=True)
        (type_dir / (BARE + ".json")).write_text("{}", encoding="utf-8")

        resolved = WorkSpaceHelper.get_submesh_folder_path(BARE)
        self.assertEqual(os.path.normcase(resolved), os.path.normcase(str(lod0)))

    def test_source_dds_format_lookup_uses_submesh_prefix_and_texture_type(self):
        lod0 = self._make_submesh(f"LOD0/{BARE}")
        source = lod0 / "TYPE_GPU-X" / f"{BARE}-LightMap.dds"
        source.write_bytes(b"DDS ")

        resolved = WorkSpaceHelper.find_submesh_texture_source(
            f"LOD0.{BARE}", "LightMap"
        )

        self.assertEqual(os.path.normcase(resolved), os.path.normcase(str(source)))

    def test_source_dds_lookup_uses_import_selected_gametype(self):
        lod0 = self._make_submesh(f"LOD0/{BARE}", gametype="GPU-X", extra_type="GPU-Y")
        for gametype in ("GPU-X", "GPU-Y"):
            (lod0 / f"TYPE_{gametype}" / f"{BARE}-LightMap.dds").write_bytes(b"DDS ")
        self._write_import_json({f"LOD0.{BARE}": "GPU-Y"})
        self.assertEqual(
            Path(WorkSpaceHelper.find_submesh_texture_source(f"LOD0.{BARE}", "LightMap")),
            lod0 / "TYPE_GPU-Y" / f"{BARE}-LightMap.dds",
        )

    # ---------------- Import.json 键兜底 ----------------

    def test_import_json_lod0_key_selects_gametype(self):
        folder = self._make_submesh(f"LOD0/{BARE}", gametype="GPU-X", extra_type="GPU-Y")
        self._write_import_json({f"LOD0.{BARE}": "GPU-X"})

        exists, error_msg, json_path = check_and_get_submesh_json_path(BARE)
        self.assertTrue(exists, error_msg)
        self.assertEqual(
            os.path.normcase(json_path),
            os.path.normcase(str(folder / "TYPE_GPU-X" / (BARE + ".json"))),
        )

    def test_import_json_without_lod0_key_rejects_ambiguous_types(self):
        self._make_submesh(f"LOD0/{BARE}", gametype="GPU-X", extra_type="GPU-Y")
        self._write_import_json({f"LOD1.{BARE}": "GPU-X"})

        exists, error_msg, json_path = check_and_get_submesh_json_path(BARE)
        self.assertFalse(exists)
        self.assertEqual(json_path, "")
        # 裸身份没命中 Import.json（键是 LOD1.*）→ 多个 TYPE_ 目录无法消歧，
        # 显式失败而不是随手挑一个。
        self.assertIn("找到以下数据类型但没有在 Import.json 中记录", error_msg)
        self.assertIn("GPU-X", error_msg)
        self.assertIn("GPU-Y", error_msg)

    def test_explicit_lod_prefix_does_not_borrow_lod0_key(self):
        self._make_submesh(f"LOD1/{BARE}", gametype="GPU-X", extra_type="GPU-Y")
        self._write_import_json({f"LOD0.{BARE}": "GPU-X"})

        exists, error_msg, json_path = check_and_get_submesh_json_path(f"LOD1.{BARE}")
        self.assertFalse(exists)
        self.assertEqual(json_path, "")
        # 显式 LOD1 身份不能借 LOD0 的键：否则会把 LOD0 的格式套到 LOD1 数据上。
        self.assertIn("找到以下数据类型但没有在 Import.json 中记录", error_msg)

    def test_missing_folder_reports_tried_paths(self):
        self._write_import_json({})

        exists, error_msg, json_path = check_and_get_submesh_json_path(BARE)
        self.assertFalse(exists)
        self.assertEqual(json_path, "")
        self.assertIn("没有找到对应的提取数据", error_msg)
        self.assertIn("LOD0", error_msg)
    # ---------------- 身份自定义后缀：点号之后是显示名 ----------------

    def test_parse_lod_unique_str_keeps_the_dot_segment(self):
        # 共用解析不剥点号段：显示名链路把它当别名（见 get_display_submesh_name）。
        self.assertEqual(
            WorkSpaceHelper.parse_lod_unique_str(f"LOD0.{BARE}.我的别名"), ("LOD0", f"{BARE}.我的别名")
        )
        self.assertEqual(WorkSpaceHelper.parse_lod_unique_str(f"LOD1.{BARE}"), ("LOD1", BARE))

    def test_suffixed_identity_still_resolves_the_folder(self):
        lod0 = self._make_submesh(f"LOD0/{BARE}")
        for identity in (f"LOD0.{BARE}.001", f"LOD0.{BARE}.rthft001ygjyytfj中文", f"{BARE}.001"):
            resolved = WorkSpaceHelper.get_submesh_folder_path(identity)
            self.assertEqual(os.path.normcase(resolved), os.path.normcase(str(lod0)), identity)
    def test_alias_folder_is_still_found_by_its_full_name(self):
        # 别名后缀是既有约定（`aaaabbbb-100-0.Face`）：整名命中时不许被剥成身份再兜底。
        alias_folder = self._make_submesh(f"LOD0/{BARE}.Face")
        resolved = WorkSpaceHelper.get_submesh_folder_path(f"LOD0.{BARE}.Face")
        self.assertEqual(os.path.normcase(resolved), os.path.normcase(str(alias_folder)))

    def test_import_json_key_matches_by_prefix_when_suffixed(self):
        folder = self._make_submesh(f"LOD0/{BARE}", gametype="GPU-X", extra_type="GPU-Y")
        self._write_import_json({f"LOD0.{BARE}": "GPU-X"})

        exists, error_msg, json_path = check_and_get_submesh_json_path(f"LOD0.{BARE}.001")
        self.assertTrue(exists, error_msg)
        self.assertEqual(
            os.path.normcase(json_path),
            os.path.normcase(str(folder / "TYPE_GPU-X" / (BARE + ".json"))),
        )

    # ---------------- EFMI 骨骼合并侧的同一口径 ----------------

    def test_efmi_resolver_falls_back_to_lod0(self):
        folder = self._make_submesh(f"LOD0/{BARE}", gametype="GPU-X", extra_type="GPU-Y")
        self._write_import_json({f"LOD0.{BARE}": "GPU-X"})

        resolved = EFMISkeletonMergeHelper._resolve_submesh_json_path(
            str(self.root), BARE
        )
        self.assertEqual(
            os.path.normcase(resolved),
            os.path.normcase(str(folder / "TYPE_GPU-X" / (BARE + ".json"))),
        )

    def test_efmi_resolver_prefers_bare_folder_over_lod0(self):
        flat = self._make_submesh(BARE, gametype="GPU-X")
        self._make_submesh(f"LOD0/{BARE}", gametype="GPU-Y")
        self._write_import_json({f"LOD0.{BARE}": "GPU-Y"})

        resolved = EFMISkeletonMergeHelper._resolve_submesh_json_path(
            str(self.root), BARE
        )
        self.assertEqual(
            os.path.normcase(resolved),
            os.path.normcase(str(flat / "TYPE_GPU-X" / (BARE + ".json"))),
        )

    def test_efmi_resolver_lod_prefixed_identity_is_untouched(self):
        lod1 = self._make_submesh(f"LOD1/{BARE}", gametype="GPU-X")
        self._make_submesh(f"LOD0/{BARE}", gametype="GPU-Y")
        self._write_import_json({f"LOD1.{BARE}": "GPU-X"})

        resolved = EFMISkeletonMergeHelper._resolve_submesh_json_path(
            str(self.root), f"LOD1.{BARE}"
        )
        self.assertEqual(
            os.path.normcase(resolved),
            os.path.normcase(str(lod1 / "TYPE_GPU-X" / (BARE + ".json"))),
        )

    # ---------------- 双套导出：per-mesh 身份表 ----------------

    def _write_component_json(self, rel_dir, vg_map, vg_offset, vg_count):
        """写出带 VGMap/VGOffset/VGCount 的组件 json（模拟骨骼合并反查写回）。"""
        import json as _json

        folder = self.root / rel_dir
        type_dir = folder / "TYPE_GPU-X"
        type_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "VGMap": {str(local): slot for local, slot in vg_map.items()},
            "VGOffset": vg_offset,
            "VGCount": vg_count,
        }
        json_path = type_dir / (folder.name + ".json")
        json_path.write_text(_json.dumps(payload), encoding="utf-8")
        return json_path

    def test_dualset_per_mesh_map_accepts_bare_identity_for_lod0_component(self):
        self._write_component_json(f"LOD0/{BARE}", {0: 0, 1: 1}, 0, 2)

        identity_map = EFMIBoneMapBuilder.build_per_mesh_identity_map(
            str(self.root), BARE, recompute_strength=False
        )
        self.assertEqual(identity_map, {0: 0, 1: 1})

    def test_dualset_per_mesh_map_prefers_exact_bare_identity(self):
        # 扁平工作空间里裸身份自己的 json（段 [10,12)）优先于 LOD0 的同名部件
        self._write_component_json(BARE, {0: 10, 1: 11}, 10, 2)
        self._write_component_json(f"LOD0/{BARE}", {0: 0, 1: 1}, 0, 2)

        identity_map = EFMIBoneMapBuilder.build_per_mesh_identity_map(
            str(self.root), BARE, recompute_strength=False
        )
        # 结果以**引用槽**为键（VGMap 值 10/11 = 裸 json 的段 [10,12)），
        # 若误用了 LOD0 的 json 就会是 {0: 0, 1: 1}。
        self.assertEqual(identity_map, {10: 10, 11: 11})

    def test_dualset_per_mesh_map_still_fails_for_unknown_identity(self):
        self._write_component_json(f"LOD0/{BARE}", {0: 0, 1: 1}, 0, 2)

        with self.assertRaises(RuntimeError) as ctx:
            EFMIBoneMapBuilder.build_per_mesh_identity_map(
                str(self.root), LOD1_ONLY, recompute_strength=False
            )
        self.assertIn("找不到组件", str(ctx.exception))
        self.assertIn("LOD0", str(ctx.exception))

    def test_dualset_per_mesh_map_explicit_lod_identity_does_not_borrow_lod0(self):
        self._write_component_json(f"LOD0/{BARE}", {0: 0, 1: 1}, 0, 2)

        with self.assertRaises(RuntimeError) as ctx:
            EFMIBoneMapBuilder.build_per_mesh_identity_map(
                str(self.root), f"LOD1.{BARE}", recompute_strength=False
            )
        self.assertIn("找不到组件", str(ctx.exception))

    def test_identity_matches_rules(self):
        matches = EFMIBoneMapBuilder._identity_matches
        self.assertTrue(matches(f"LOD0.{BARE}", BARE))
        self.assertTrue(matches(BARE, BARE))
        self.assertTrue(matches(f"LOD0.{BARE}", f"LOD0.{BARE}"))
        self.assertFalse(matches(f"LOD1.{BARE}", BARE))
        self.assertFalse(matches(f"LOD0.{BARE}", f"LOD1.{BARE}"))
        self.assertFalse(matches(f"LOD1.{BARE}", f"LOD0.{BARE}"))
        self.assertFalse(matches("", BARE))

    def test_split_lod_identity_rules(self):
        split = EFMIBoneMapBuilder._split_lod_identity
        self.assertEqual(split(f"LOD1.{BARE}"), ("LOD1", BARE))
        self.assertEqual(split(BARE), ("", BARE))
        self.assertEqual(split(""), ("", ""))
        # 非 LOD 的点号身份（别名后缀）整段算裸身份，不误判成 LOD 前缀
        self.assertEqual(split(f"{BARE}.Face"), ("", f"{BARE}.Face"))


if __name__ == "__main__":
    unittest.main()
