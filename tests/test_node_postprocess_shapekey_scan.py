import importlib.util
import sys
import tempfile
import types
import unittest
from pathlib import Path


def _install_module(name, **attrs):
    """安装 Fake 模块到 sys.modules"""
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_node_postprocess_shapekey_scan_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeShapeKeyBlock:
    def __init__(self, name):
        self.name = name


class _FakeShapeKeyData:
    def __init__(self, *names):
        self.key_blocks = [_FakeShapeKeyBlock("Basis"), *[_FakeShapeKeyBlock(name) for name in names]]


class _FakeObject:
    def __init__(self, name, *shape_key_names):
        self.name = name
        self.type = "MESH"
        self.data = types.SimpleNamespace(shape_keys=_FakeShapeKeyData(*shape_key_names))


_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(PropertyGroup=object, Operator=object, UIList=object),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kwargs: None,
        BoolProperty=lambda **_kwargs: None,
        IntProperty=lambda **_kwargs: None,
        EnumProperty=lambda **_kwargs: None,
        CollectionProperty=lambda **_kwargs: None,
    ),
    data=types.SimpleNamespace(objects={}),
    utils=types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module(
    f"{PKG}.blueprint.node_postprocess_base",
    SSMTNode_PostProcess_Base=type(
        "_FakePostProcessBase",
        (object,),
        {
            "split_anim_driver_block_content": staticmethod(lambda content: ("", content)),
            "split_auto_appended_tail_content": staticmethod(
                lambda content: (content, "")
            ),
        },
    ),
)
_install_module(f"{PKG}.blueprint.direct_export", sync_shapekey_direct_mode=lambda *_args, **_kwargs: None)
_install_module(
    f"{PKG}.blueprint.variable_registry",
    allocate_shape_key_variable_name=lambda shape_key_name, **_kwargs: f"Freq_{shape_key_name}",
    mark_variable_name_used=lambda *_args, **_kwargs: None,
    normalize_variable_name=lambda value: str(value or "").strip(),
    cjk_to_ascii=lambda value: str(value or ""),
    is_pinyin_available=lambda **_kwargs: False,
    reset_pinyin_cache=lambda *_args, **_kwargs: None,
)
_install_module(
    f"{PKG}.common.mod_path_compat",
    collect_base_position_resource_map=lambda *_args, **_kwargs: {},
    derive_shapekey_base_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_freq_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_merged_data_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_merged_map_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_slot_map_resource_name=lambda *args, **_kwargs: "",
    derive_shapekey_slot_resource_name=lambda *args, **_kwargs: "",
    ensure_resource_alias_section=lambda *_args, **_kwargs: None,
    resolve_hash_buffer_candidate=lambda *_args, **_kwargs: "",
)
_install_module(
    f"{PKG}.common.object_prefix_helper",
    ObjectPrefixHelper=types.SimpleNamespace(
        resolve_source_object_name=lambda name: name,
        extract_prefix_info=lambda name: None,
    ),
)
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(info=lambda *_args, **_kwargs: None, warning=lambda *_args, **_kwargs: None),
)
_install_module(
    f"{PKG}.utils.shapekey_utils",
    ShapeKeyUtils=types.SimpleNamespace(
        is_basis_shape_key_name=lambda name: str(name or "").strip().lower() == "basis",
    ),
)

_helper_state = {"collect_connected_start_nodes": lambda _tree: [], "blueprint_model": None}
_install_module(
    f"{PKG}.blueprint.export_helper",
    BlueprintExportHelper=types.SimpleNamespace(
        collect_connected_start_nodes=lambda tree: _helper_state["collect_connected_start_nodes"](tree),
        get_current_blueprint_model=lambda: _helper_state["blueprint_model"],
        _resolve_shapekey_object_in_scene=lambda name: _fake_bpy.data.objects.get(name),
    ),
)


module_path = Path(__file__).resolve().parents[1] / "blueprint" / "node_postprocess_shapekey.py"
spec = importlib.util.spec_from_file_location(f"{PKG}.blueprint.node_postprocess_shapekey", module_path)
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)


class _RelocatingItem:
    """模拟 Blender 的 PropertyGroup 元素：一旦集合搬移内存，旧引用即失效。"""

    _DEFAULTS = {
        "shape_key_name": "",
        "assigned_variable_name": "",
        "custom_variable_name": "",
        "export_enabled": True,
        "drag_zone_id": -1,
        "drag_click_stage": 1,
        "drag_dir_id": "-1",
    }

    def __init__(self, **kwargs):
        object.__setattr__(self, "_freed", False)
        data = dict(self._DEFAULTS)
        data.update(kwargs)
        object.__setattr__(self, "_data", data)

    def _check_alive(self):
        if object.__getattribute__(self, "_freed"):
            raise RuntimeError("访问了已失效的形态键条目引用（use-after-free）")

    def __getattr__(self, name):
        self._check_alive()
        return object.__getattribute__(self, "_data")[name]

    def __setattr__(self, name, value):
        self._check_alive()
        object.__getattribute__(self, "_data")[name] = value


class _RelocatingCollection(list):
    """模拟 CollectionProperty 的内存搬移语义。

    ``add()``/``remove()`` 会整块搬移元素内存，使此前取得的元素引用全部失效；
    这里用「旧对象标记失效 + 替换为新对象」表达同一效果：沿用旧引用的实现会抛
    ``RuntimeError``，而不是像 ``list`` 那样静默通过。
    """

    def _relocate(self):
        for index, item in enumerate(self):
            object.__setattr__(item, "_freed", True)
            list.__setitem__(
                self, index, _RelocatingItem(**object.__getattribute__(item, "_data"))
            )

    def add(self):
        self._relocate()
        item = _RelocatingItem()
        list.append(self, item)
        return item

    def remove(self, index):
        self._relocate()
        del self[index]


class NodePostprocessShapeKeyScanTests(unittest.TestCase):
    """测试形态键后处理扫描节点：收集和变量映射管理"""

    def setUp(self):
        """每个测试前清空伪装数据"""
        _fake_bpy.data.objects.clear()
        _helper_state["collect_connected_start_nodes"] = lambda _tree: []
        _helper_state["blueprint_model"] = None

    def test_collect_blueprint_shape_key_names_uses_processing_chain_aliases(self):
        """测试 collect_blueprint_shape_key_names 使用处理链别名收集形态键名"""
        _fake_bpy.data.objects["Body"] = _FakeObject("Body", "Smile", "Blink")
        _helper_state["blueprint_model"] = types.SimpleNamespace(
            processing_chains=[
                types.SimpleNamespace(
                    is_valid=True,
                    reached_output=True,
                    object_name="LOD0.hash-0.Body_chain1_copy",
                    original_object_name="Body",
                    virtual_object_name="LOD0.hash-0.Body_chain1_copy",
                    export_object_name_override="",
                    rename_history=[],
                    get_export_object_name=lambda: "LOD0.hash-0.Body_chain1_copy",
                )
            ]
        )

        node = module.SSMTNode_PostProcess_ShapeKey()
        node.id_data = object()

        result = node.collect_blueprint_shape_key_names()

        self.assertEqual(result, ["Blink", "Smile"])

    def test_ensure_shape_key_variable_map_rebuilds_items_from_current_scan(self):
        """测试 ensure_shape_key_variable_map 从当前扫描重建变量映射条目"""
        class _FakeItem:
            def __init__(self, shape_key_name="", assigned_variable_name="", custom_variable_name=""):
                self.shape_key_name = shape_key_name
                self.assigned_variable_name = assigned_variable_name
                self.custom_variable_name = custom_variable_name
                self.drag_zone_id = -1
                self.drag_click_stage = 1
                self.drag_dir_id = "-1"

        class _FakeCollection(list):
            def add(self):
                item = _FakeItem()
                self.append(item)
                return item

            def remove(self, index):
                del self[index]

        node = module.SSMTNode_PostProcess_ShapeKey()
        node.shapekey_variable_items = _FakeCollection([
            _FakeItem("A", "Freq_A", "Freq_A"),
            _FakeItem("B", "Freq_B", "Manual_B"),
            _FakeItem("C", "Freq_C", "Freq_C"),
            _FakeItem("D", "Freq_D", "Freq_D"),
            _FakeItem("E", "Freq_E", "Freq_E"),
        ])

        created_count, backfilled_count = node.ensure_shape_key_variable_map(["A", "B", "C"])

        self.assertEqual(created_count, 0)
        self.assertEqual(backfilled_count, 0)
        self.assertEqual(
            [item.shape_key_name for item in node.shapekey_variable_items],
            ["A", "B", "C"],
        )
        self.assertEqual(node.shapekey_variable_items[1].custom_variable_name, "Manual_B")

    def test_ensure_shape_key_variable_map_adds_new_items_after_pruning_stale_ones(self):
        """测试 ensure_shape_key_variable_map 在裁剪过期条目后添加新条目"""
        class _FakeItem:
            def __init__(self, shape_key_name="", assigned_variable_name="", custom_variable_name=""):
                self.shape_key_name = shape_key_name
                self.assigned_variable_name = assigned_variable_name
                self.custom_variable_name = custom_variable_name
                self.drag_zone_id = -1
                self.drag_click_stage = 1
                self.drag_dir_id = "-1"

        class _FakeCollection(list):
            def add(self):
                item = _FakeItem()
                self.append(item)
                return item

            def remove(self, index):
                del self[index]

        node = module.SSMTNode_PostProcess_ShapeKey()
        node.shapekey_variable_items = _FakeCollection([
            _FakeItem("A", "Freq_A", "Freq_A"),
            _FakeItem("B", "Freq_B", "Freq_B"),
            _FakeItem("C", "Freq_C", "Freq_C"),
            _FakeItem("D", "Freq_D", "Freq_D"),
            _FakeItem("E", "Freq_E", "Freq_E"),
        ])

        node.ensure_shape_key_variable_map(["A", "B", "C"])
        created_count, _backfilled_count = node.ensure_shape_key_variable_map(["A", "B", "F"])

        self.assertEqual(created_count, 1)
        self.assertEqual(
            [item.shape_key_name for item in node.shapekey_variable_items],
            ["A", "B", "F"],
        )

    def test_ensure_shape_key_variable_map_preserves_drag_drive_settings_on_rebuild(self):
        """测试重建时保留区域/档位/方向等拖拽设置"""
        class _FakeItem:
            def __init__(self, shape_key_name="", assigned_variable_name="", custom_variable_name=""):
                self.shape_key_name = shape_key_name
                self.assigned_variable_name = assigned_variable_name
                self.custom_variable_name = custom_variable_name
                self.drag_zone_id = -1
                self.drag_click_stage = 1
                self.drag_dir_id = "-1"

        class _FakeCollection(list):
            def add(self):
                item = _FakeItem()
                self.append(item)
                return item

            def remove(self, index):
                del self[index]

        node = module.SSMTNode_PostProcess_ShapeKey()
        a = _FakeItem("A", "Freq_A", "Freq_A")
        a.drag_zone_id = 2
        a.drag_click_stage = 3
        a.drag_dir_id = "1"
        node.shapekey_variable_items = _FakeCollection([
            a,
            _FakeItem("B", "Freq_B", "Freq_B"),
            _FakeItem("C", "Freq_C", "Freq_C"),
            _FakeItem("D", "Freq_D", "Freq_D"),
            _FakeItem("E", "Freq_E", "Freq_E"),
        ])

        # 名称集合变化（D/E 被裁剪，新增 F）触发重建
        node.ensure_shape_key_variable_map(["A", "B", "C", "F"])

        self.assertEqual(
            [item.shape_key_name for item in node.shapekey_variable_items],
            ["A", "B", "C", "F"],
        )
        preserved = next(item for item in node.shapekey_variable_items if item.shape_key_name == "A")
        self.assertEqual(preserved.drag_zone_id, 2)
        self.assertEqual(preserved.drag_click_stage, 3)
        self.assertEqual(preserved.drag_dir_id, "1")

    def test_ensure_shape_key_variable_map_no_rebuild_when_names_unchanged(self):
        """测试形态键集合未变化时（即使顺序不同）不重建，保留拖拽设置"""
        class _FakeItem:
            def __init__(self, shape_key_name="", assigned_variable_name="", custom_variable_name=""):
                self.shape_key_name = shape_key_name
                self.assigned_variable_name = assigned_variable_name
                self.custom_variable_name = custom_variable_name
                self.drag_zone_id = -1
                self.drag_click_stage = 1
                self.drag_dir_id = "-1"

        class _FakeCollection(list):
            def add(self):
                item = _FakeItem()
                self.append(item)
                return item

            def remove(self, index):
                del self[index]

        node = module.SSMTNode_PostProcess_ShapeKey()
        a = _FakeItem("A", "Freq_A", "Freq_A")
        a.drag_zone_id = 5
        a.drag_click_stage = 2
        a.drag_dir_id = "3"
        b = _FakeItem("B", "Freq_B", "Freq_B")
        node.shapekey_variable_items = _FakeCollection([b, a])  # 顺序 B,A

        # 传入 A,B（名称集合相同，顺序不同）→ 不重建
        node.ensure_shape_key_variable_map(["A", "B"])

        self.assertEqual(len(node.shapekey_variable_items), 2)
        self.assertIs(node.shapekey_variable_items[0], b)
        self.assertIs(node.shapekey_variable_items[1], a)
        self.assertEqual(a.drag_zone_id, 5)
        self.assertEqual(a.drag_click_stage, 2)
        self.assertEqual(a.drag_dir_id, "3")

    def test_ensure_shape_key_variable_map_preserves_export_enabled_on_rebuild(self):
        """测试重建时保留形态键的导出勾选状态，新增条目默认勾选"""
        class _FakeItem:
            def __init__(self, shape_key_name="", assigned_variable_name="", custom_variable_name=""):
                self.shape_key_name = shape_key_name
                self.assigned_variable_name = assigned_variable_name
                self.custom_variable_name = custom_variable_name
                self.export_enabled = True
                self.drag_zone_id = -1
                self.drag_click_stage = 1
                self.drag_dir_id = "-1"

        class _FakeCollection(list):
            def add(self):
                item = _FakeItem()
                self.append(item)
                return item

            def remove(self, index):
                del self[index]

        node = module.SSMTNode_PostProcess_ShapeKey()
        unchecked = _FakeItem("B", "Freq_B", "Freq_B")
        unchecked.export_enabled = False
        node.shapekey_variable_items = _FakeCollection([
            _FakeItem("A", "Freq_A", "Freq_A"),
            unchecked,
            _FakeItem("C", "Freq_C", "Freq_C"),
        ])

        # 名称集合变化（C 被裁剪，新增 D）触发重建
        node.ensure_shape_key_variable_map(["A", "B", "D"])

        self.assertEqual(
            [item.shape_key_name for item in node.shapekey_variable_items],
            ["A", "B", "D"],
        )
        preserved = next(item for item in node.shapekey_variable_items if item.shape_key_name == "B")
        self.assertFalse(preserved.export_enabled)
        added = next(item for item in node.shapekey_variable_items if item.shape_key_name == "D")
        self.assertTrue(added.export_enabled)

    def test_ensure_shape_key_variable_map_survives_collection_growth(self):
        """add() 搬移元素内存后，循环前建立的既有条目缓存不得再被使用。

        真实崩溃路径：``shapekey_variable_items.add()`` 扩容会整块搬移元素内存，
        循环前建立的 ``existing_by_name`` 缓存里的条目引用随即失效；若之后还要按名单
        读取这些条目（例如新形态键按名称排序后排在既有条目之前），读写已释放内存
        就会直接闪退。因此实现必须先把缺失条目全部补齐，再建立缓存并写入属性。
        """
        node = module.SSMTNode_PostProcess_ShapeKey()
        node.shapekey_variable_items = _RelocatingCollection([
            _RelocatingItem(shape_key_name="A", assigned_variable_name="Freq_A", custom_variable_name="Freq_A"),
            _RelocatingItem(shape_key_name="B", assigned_variable_name="Freq_B", custom_variable_name="Freq_B"),
            _RelocatingItem(shape_key_name="C", assigned_variable_name="Freq_C", custom_variable_name="Freq_C"),
        ])

        # 名单内部会排序：新键名 "0D" 排在 A/B/C 之前，因此 add() 发生在读取既有条目
        # 之前，实现若沿用 add() 之前建立的缓存引用，这里就会命中已释放内存。
        created_count, backfilled_count = node.ensure_shape_key_variable_map(["0D", "A", "B", "C"])

        self.assertEqual(created_count, 1)
        # 新建项只算 created、不得再算成回填：否则扫描算子会误报「回填变量框 N 项」
        self.assertEqual(backfilled_count, 0)
        # 阶段二必须真的给新建项写上预分配名（统计对但名字没写 = 预分配静默失效）
        created_item = next(
            item for item in node.shapekey_variable_items if item.shape_key_name == "0D"
        )
        self.assertEqual(created_item.assigned_variable_name, "Freq_0D")
        self.assertEqual(created_item.custom_variable_name, "Freq_0D")
        # 名称集合未变化时不重建（保持既有顺序），仅补齐缺失条目
        self.assertEqual(
            [item.shape_key_name for item in node.shapekey_variable_items],
            ["A", "B", "C", "0D"],
        )

    def test_ensure_shape_key_variable_map_rebuild_survives_collection_growth(self):
        """集合增长且存在过期条目时会走重建路径，序列化用的 rebuilt_items 同样不能失效。"""
        node = module.SSMTNode_PostProcess_ShapeKey()
        node.shapekey_variable_items = _RelocatingCollection([
            _RelocatingItem(shape_key_name="A", assigned_variable_name="Freq_A", custom_variable_name="Freq_A"),
            _RelocatingItem(shape_key_name="B", assigned_variable_name="Freq_B", custom_variable_name="Freq_B"),
            _RelocatingItem(shape_key_name="C", assigned_variable_name="Freq_C", custom_variable_name="Freq_C"),
            _RelocatingItem(shape_key_name="D", assigned_variable_name="Freq_D", custom_variable_name="Freq_D"),
            _RelocatingItem(shape_key_name="E", assigned_variable_name="Freq_E", custom_variable_name="Freq_E"),
        ])

        # A/B/C 过期 + F 新增：既触发 add()，也触发按新名单重建
        created_count, backfilled_count = node.ensure_shape_key_variable_map(["D", "E", "F"])

        self.assertEqual(created_count, 1)
        self.assertEqual(backfilled_count, 0)
        self.assertEqual(
            [item.shape_key_name for item in node.shapekey_variable_items],
            ["D", "E", "F"],
        )
        # 重建走的是序列化字典：新建项的预分配名必须被带进新条目
        rebuilt = {item.shape_key_name: item for item in node.shapekey_variable_items}
        self.assertEqual(rebuilt["F"].assigned_variable_name, "Freq_F")
        self.assertEqual(rebuilt["F"].custom_variable_name, "Freq_F")
        self.assertEqual(rebuilt["D"].assigned_variable_name, "Freq_D")

    def test_ensure_shape_key_variable_map_counts_created_and_backfilled_separately(self):
        """同一名单里既有新建项、又有 assigned 为空的既有条目：两类统计必须分开计。"""
        node = module.SSMTNode_PostProcess_ShapeKey()
        node.shapekey_variable_items = _RelocatingCollection([
            # 既有但从未预分配 → 走回填分支，计 backfilled
            _RelocatingItem(shape_key_name="A"),
            _RelocatingItem(shape_key_name="B", assigned_variable_name="Freq_B", custom_variable_name="Freq_B"),
        ])

        created_count, backfilled_count = node.ensure_shape_key_variable_map(["A", "B", "C"])

        self.assertEqual((created_count, backfilled_count), (1, 1))
        by_name = {item.shape_key_name: item for item in node.shapekey_variable_items}
        # 既有空条目被回填，且 custom 与 assigned 一致
        self.assertEqual(by_name["A"].assigned_variable_name, "Freq_A")
        self.assertEqual(by_name["A"].custom_variable_name, "Freq_A")
        # 新建项被预分配
        self.assertEqual(by_name["C"].assigned_variable_name, "Freq_C")
        self.assertEqual(by_name["C"].custom_variable_name, "Freq_C")
        # 用户手改过的 custom 名不得被覆盖
        self.assertEqual(by_name["B"].custom_variable_name, "Freq_B")

    def test_parse_classification_text_final_skips_unchecked_shape_keys(self):
        """测试解析分类文本时跳过未勾选导出的形态键及其物体行"""
        node = module.SSMTNode_PostProcess_ShapeKey()
        node.shapekey_variable_items = [
            types.SimpleNamespace(shape_key_name="Smile", export_enabled=True),
            types.SimpleNamespace(shape_key_name="Frown", export_enabled=False),
        ]

        text = "\n".join([
            "# 自动化形态键导出 - 分类报告",
            "槽位 1:",
            "  - 名称: Smile",
            "    - 物体: Body",
            "槽位 2:",
            "  - 名称: Frown",
            "    - 物体: Body",
            "  - 名称: Blink",
            "    - 物体: Body",
        ])

        slot_to_name_to_objects, _hashes, _hash_to_objects, all_objects = (
            node._parse_classification_text_final(text)
        )

        self.assertEqual(list(slot_to_name_to_objects[1].keys()), ["Smile"])
        # Frown 未勾选被跳过；Blink 不在映射列表中，默认勾选保留
        self.assertEqual(list(slot_to_name_to_objects[2].keys()), ["Blink"])
        self.assertEqual(all_objects, ["Body"])

    def test_compute_dispatch_group_count_rounds_up_by_thread_group(self):
        node = module.SSMTNode_PostProcess_ShapeKey()

        self.assertEqual(node._compute_dispatch_group_count(0, threads_per_group=16), 1)
        self.assertEqual(node._compute_dispatch_group_count(1, threads_per_group=16), 1)
        self.assertEqual(node._compute_dispatch_group_count(16, threads_per_group=16), 1)
        self.assertEqual(node._compute_dispatch_group_count(17, threads_per_group=16), 2)
        self.assertEqual(node._compute_dispatch_group_count(128, threads_per_group=64), 2)

    def test_parse_ini_for_draw_info_follows_run_block_for_draw_and_outer_ib(self):
        node = module.SSMTNode_PostProcess_ShapeKey()
        base_path = str(Path("E:/mod"))
        sections = {
            "[Resource_DrawIB]": [
                "filename = Meshes0000/body.ib",
            ],
            "[CustomShader_Test]": [
                "handling = skip",
                "drawindexed = 36,12,0",
            ],
            "[TextureOverride_Test]": [
                "ib = ref Resource_DrawIB",
                "; [mesh:Body]",
                "if $Body_ps_replace == 1",
                "    run = CustomShader_Test",
                "else",
                "    run = CustomShader_Test",
                "endif",
            ],
        }

        draw_info = node._parse_ini_for_draw_info(sections, base_path)

        self.assertIn("Body", draw_info)
        self.assertEqual(draw_info["Body"][0]["draw_params"], (36, 12, 0))
        self.assertEqual(
            draw_info["Body"][0]["ib_path"],
            str(Path(base_path) / "Meshes0000" / "body.ib"),
        )

    def test_parse_ini_for_draw_info_follows_nested_run_path_for_ib_lookup(self):
        node = module.SSMTNode_PostProcess_ShapeKey()
        base_path = str(Path("E:/mod"))
        sections = {
            "[Resource_DrawIB]": [
                "filename = Meshes0000/body_nested.ib",
            ],
            "[CommandList_IB]": [
                "ib = ref Resource_DrawIB",
            ],
            "[CustomShader_Final]": [
                "handling = skip",
                "drawindexed = 48,24,0",
            ],
            "[CommandList_DrawWrapper]": [
                "run = CommandList_IB",
                "run = CustomShader_Final",
            ],
            "[TextureOverride_Test]": [
                "; [mesh:BodyNested]",
                "run = CommandList_DrawWrapper",
            ],
        }

        draw_info = node._parse_ini_for_draw_info(sections, base_path)

        self.assertIn("BodyNested", draw_info)
        self.assertEqual(draw_info["BodyNested"][0]["draw_params"], (48, 24, 0))
        self.assertEqual(
            draw_info["BodyNested"][0]["ib_path"],
            str(Path(base_path) / "Meshes0000" / "body_nested.ib"),
        )

    def test_parse_ini_for_draw_info_reads_ib_from_same_run_section_as_draw(self):
        node = module.SSMTNode_PostProcess_ShapeKey()
        base_path = str(Path("E:/mod"))
        sections = {
            "[Resource_DrawIB]": [
                "filename = Meshes0000/body_same_run.ib",
            ],
            "[CommandList_Draw]": [
                "ib = ref Resource_DrawIB",
                "drawindexed = 60,30,0",
            ],
            "[TextureOverride_Test]": [
                "; [mesh:BodySameRun]",
                "run = CommandList_Draw",
            ],
        }

        draw_info = node._parse_ini_for_draw_info(sections, base_path)

        self.assertIn("BodySameRun", draw_info)
        self.assertEqual(draw_info["BodySameRun"][0]["draw_params"], (60, 30, 0))
        self.assertEqual(
            draw_info["BodySameRun"][0]["ib_path"],
            str(Path(base_path) / "Meshes0000" / "body_same_run.ib"),
        )

    def test_parse_ini_for_draw_info_resolves_case_insensitive_compact_assignments(self):
        node = module.SSMTNode_PostProcess_ShapeKey()
        base_path = str(Path("E:/mod"))
        sections = {
            "[RESOURCE_DRAWIB]": [
                "FILENAME=Meshes0000/body_case.ib",
            ],
            "[customshader_draw]": [
                "DRAWINDEXED=72,36,0",
            ],
            "[TextureOverride_Test]": [
                "IB=ref resource_drawib",
                "; [mesh:BodyCase]",
                "RUN=CUSTOMSHADER_DRAW",
            ],
        }

        draw_info = node._parse_ini_for_draw_info(sections, base_path)

        self.assertEqual(draw_info["BodyCase"][0]["draw_params"], (72, 36, 0))
        self.assertEqual(
            draw_info["BodyCase"][0]["ib_path"],
            str(Path(base_path) / "Meshes0000" / "body_case.ib"),
        )

    def test_parse_draw_command_rejects_non_numeric_geometry_parameters(self):
        node = module.SSMTNode_PostProcess_ShapeKey()

        self.assertIsNone(node._parse_draw_command_line("drawindexed = $count,12,0"))
        self.assertIsNone(
            node._parse_draw_command_line(
                "drawindexedinstanced = 36,INSTANCE_COUNT,$offset,0,FIRST_INSTANCE"
            )
        )

    def test_ini_read_write_preserves_namespace_preamble_with_lf(self):
        node = module.SSMTNode_PostProcess_ShapeKey()
        with tempfile.TemporaryDirectory() as tmpdir:
            ini_path = Path(tmpdir) / "test.ini"
            ini_path.write_bytes(
                b"namespace = Example\\Mod\n; header\n\n[Constants]\nglobal $x = 1\n"
            )

            sections, tail, _driver = node._read_ini_to_ordered_dict(str(ini_path))
            node._write_ordered_dict_to_ini(sections, str(ini_path), tail)

            first_output = ini_path.read_bytes()
            sections, tail, _driver = node._read_ini_to_ordered_dict(str(ini_path))
            node._write_ordered_dict_to_ini(sections, str(ini_path), tail)

            output = ini_path.read_bytes()
            self.assertEqual(output, first_output)
            self.assertIn(b"namespace = Example\\Mod\n; header\n\n[Constants]", output)
            self.assertNotIn(b"\r", output)

    def test_update_shader_file_optimized_mode_skips_vertex_range_definitions(self):
        node = module.SSMTNode_PostProcess_ShapeKey()
        node.INTENSITY_START_INDEX = 100
        node.VERTEX_RANGE_START_INDEX = 200
        node._get_vertex_struct_definition = lambda **_kwargs: (
            "struct VertexAttributes {\n"
            "    float3 position;\n"
            "    float3 normal;\n"
            "    float4 tangent;\n"
            "};"
        )

        import tempfile
        with tempfile.TemporaryDirectory() as temp_dir:
            shader_path = Path(temp_dir) / "shader.hlsl"
            shader_path.write_text(
                "// --- [PYTHON-MANAGED BLOCK START] ---\n"
                "// --- [PYTHON-MANAGED BLOCK END] ---\n"
                "// --- [PYTHON-MANAGED LOGIC START] ---\n"
                "// --- [PYTHON-MANAGED LOGIC END] ---\n",
                encoding="utf-8",
            )

            success = node._update_shader_file(
                str(shader_path),
                hash_slot_data={1: {"Smile": ["ObjA"]}},
                use_packed=True,
                use_delta=True,
                unique_names=["Smile"],
                unique_objects=["ObjA"],
                use_optimized=True,
                merge_slot_files=False,
            )

            self.assertTrue(success)
            shader_source = shader_path.read_text(encoding="utf-8")
            self.assertIn("FREQ1", shader_source)
            self.assertNotIn("START1", shader_source)
            self.assertNotIn("END1", shader_source)

    def test_draw_buttons_renders_shape_key_variable_mappings_as_template_list(self):
        class _FakeItem:
            def __init__(self, shape_key_name="", assigned_variable_name="", custom_variable_name=""):
                self.shape_key_name = shape_key_name
                self.assigned_variable_name = assigned_variable_name
                self.custom_variable_name = custom_variable_name

        node = module.SSMTNode_PostProcess_ShapeKey()
        node.name = "ShapeKey"
        node.shapekey_variable_items = [
            _FakeItem("A", "Freq_A", "Freq_A"),
            _FakeItem("B", "Freq_B", "Manual_B"),
        ]
        node.shapekey_variable_index = 0
        node.drag_drive_enabled = False

        calls = []

        class _FakeOperator:
            node_name = ""

        class _FakeBox:
            def label(self, *args, **kwargs):
                calls.append(("label", args, kwargs))

            def prop(self, *args, **kwargs):
                calls.append(("prop", args, kwargs))

            def template_list(self, *args, **kwargs):
                calls.append(("template_list", args, kwargs))

            def operator(self, *args, **kwargs):
                calls.append(("box_operator", args, kwargs))
                return _FakeOperator()

            def box(self):
                calls.append(("box", (), {}))
                return _FakeBox()

            def row(self, *args, **kwargs):
                calls.append(("row", args, kwargs))
                return _FakeLayout()

        class _FakeLayout:
            def operator(self, *args, **kwargs):
                calls.append(("operator", args, kwargs))
                return _FakeOperator()

            def box(self):
                calls.append(("box", (), {}))
                return _FakeBox()

            def prop(self, *args, **kwargs):
                calls.append(("prop", args, kwargs))

            def label(self, *args, **kwargs):
                calls.append(("label", args, kwargs))

        node.draw_buttons(context=None, layout=_FakeLayout())

        self.assertTrue(any(call[0] == "template_list" for call in calls))

    def test_shape_key_variable_mapping_ui_list_is_registered(self):
        self.assertIn(module.SSMT_UL_ShapeKeyVariableMappings, module.classes)

    # ---------- 工作空间优先格式检测（形态键配置生成） ----------

    def _patch_workspace_resolvers(self, stride_by_hash, prefix_game_type=None):
        module._resolve_workspace_category_stride = lambda unique_str, category: int(
            stride_by_hash.get(str(unique_str or "").strip(), 0) or 0
        )
        module._resolve_workspace_game_type_by_prefix = lambda prefix: prefix_game_type

    def test_detect_vertex_format_uses_workspace_stride_per_hash_heterogeneous(self):
        """9 个 16 字节 IB + 1 个 40 字节 IB 并存：每个 IB 取自己的工作空间步长。"""
        node = module.SSMTNode_PostProcess_ShapeKey()
        base_40 = bytes(3 * 40)   # 3 顶点 × 40B
        base_16 = bytes(3 * 16)   # 3 顶点 × 16B
        self._patch_workspace_resolvers(
            {"aaaa0000-1-0": 40, "bbbb1111-2-0": 16},
        )

        stride_40, floats_40, vertices_40 = node._detect_vertex_format(
            base_40, base_40, struct_definition=None, hash_val="aaaa0000-1-0",
        )
        self.assertEqual((stride_40, floats_40, vertices_40), (40, 10, 3))

        stride_16, floats_16, vertices_16 = node._detect_vertex_format(
            base_16, base_16, struct_definition=None, hash_val="bbbb1111-2-0",
        )
        self.assertEqual((stride_16, floats_16, vertices_16), (16, 4, 3))

    def test_detect_vertex_format_workspace_wins_over_conflicting_struct(self):
        """用户手填 16 字节顶点属性定义时，40 字节 IB 不再被写成 16 字节（本次 bug 根因）。"""
        node = module.SSMTNode_PostProcess_ShapeKey()
        base_40 = bytes(3 * 40)
        self._patch_workspace_resolvers({"aaaa0000-1-0": 40})
        struct_16 = "struct VertexAttributes {\n    float4 position;\n};"

        stride, floats, vertices = node._detect_vertex_format(
            base_40, base_40, struct_definition=struct_16, hash_val="aaaa0000-1-0",
        )
        self.assertEqual((stride, floats, vertices), (40, 10, 3))

    def test_detect_vertex_format_falls_back_to_struct_when_workspace_absent(self):
        node = module.SSMTNode_PostProcess_ShapeKey()
        base_48 = bytes(3 * 16)
        self._patch_workspace_resolvers({})
        struct_16 = "struct VertexAttributes {\n    float4 position;\n};"

        stride, floats, vertices = node._detect_vertex_format(
            base_48, base_48, struct_definition=struct_16, hash_val="aaaa0000-1-0",
        )
        self.assertEqual((stride, floats, vertices), (16, 4, 3))

    def test_detect_vertex_format_legacy_default_with_divisibility_fallback(self):
        """工作空间与结构体都不可用时：40B 默认优先，16B 缓冲走整除回退不再静默错位。"""
        node = module.SSMTNode_PostProcess_ShapeKey()
        self._patch_workspace_resolvers({})
        base_48 = bytes(3 * 16)

        stride, floats, vertices = node._detect_vertex_format(
            base_48, base_48, struct_definition=None, hash_val=None,
        )
        self.assertEqual((stride, floats, vertices), (16, 4, 3))

    def test_workspace_stride_cache_is_per_hash(self):
        node = module.SSMTNode_PostProcess_ShapeKey()
        self._patch_workspace_resolvers({"aaaa0000-1-0": 40, "bbbb1111-2-0": 16})
        self.assertEqual(node._get_workspace_position_stride("aaaa0000-1-0"), 40)
        self.assertEqual(node._get_workspace_position_stride("bbbb1111-2-0"), 16)
        # 缓存命中，再次获取结果一致
        self.assertEqual(node._get_workspace_position_stride("aaaa0000-1-0"), 40)

    def test_workspace_stride_prefix_fallback_when_unique_str_unresolvable(self):
        node = module.SSMTNode_PostProcess_ShapeKey()
        node._extract_hash_prefix = lambda value: str(value or "").split("-")[0]
        module._resolve_workspace_category_stride = lambda unique_str, category: 0
        module._resolve_workspace_game_type_by_prefix = lambda prefix: types.SimpleNamespace(
            CategoryStrideDict={"Position": 40},
        )
        self.assertEqual(node._get_workspace_position_stride("cccc2222-3-0"), 40)

if __name__ == "__main__":
    unittest.main()
