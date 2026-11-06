# -*- coding: utf-8 -*-
"""TTL 层号（A 的生成端）与层权重（C）的导出测试。

不依赖 Blender：用与 ``test_node_postprocess_material_htmi.py`` 相同的
fake-bpy 桩把 ``blueprint/node_postprocess_material.py`` 当纯逻辑模块加载。

覆盖的契约（5 层修订：合法 0..4，越界抛 ValueError）：

- 物体名后缀 ``_TTL层N``（N 为 0..4 的整数，共 5 层）与 ``_TTL权重W``（0..1 浮点）；
- 两个后缀可并存、顺序无关；一个物体多张 TTLMap 共用同一组层号/权重；
- 无后缀但有 TTLMap：优先检测实际表面内外关系；无有效网格时按 r 升序，
  相邻差 <=0.005m 聚成同一层；手写后缀优先，不参与推断；
- 自动层写 ``; auto-layer: N (r=...m, gap=... from prev)``（首个 ``gap=-``；层 0
  也写，便于和手动 0/算不出区分）；表面推断写 ``(surface; r=...m)``；
- ``$\\TTL\\layer_index = N`` 每段都写（含 0），手动/自动/算不出都一样；
- 无 TTLMap 材质的工程不产生 TTL 段，产物逐字节不变；
- 自动算不出（无 bound_box、r 超可信范围）落 0、无注释，物体名进
  ``_ttl_auto_layer_skipped``；聚类层号 >4 落 4、WARN、同样进清单；
  手写层号越界/非整数抛 ``ValueError``，导出失败；
- 权重把 ``_ttl_alpha_for_material`` 的结果乘 W 后再落 ``$TTLAlpha*`` 常量。
"""
import importlib.util
import json
import os
import sys
import tempfile
import types
import unittest
from collections import OrderedDict
from pathlib import Path

BS = chr(92)


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_ttl_layer_test_pkg"
for package_name in (PKG, f"{PKG}.blueprint", f"{PKG}.utils", f"{PKG}.common", f"{PKG}.toolkit"):
    package = _install_module(package_name)
    package.__path__ = []


class _FakeBpyDataObjects(dict):
    pass


_fake_bpy = types.SimpleNamespace(
    types=types.SimpleNamespace(PropertyGroup=object, Operator=object),
    props=types.SimpleNamespace(
        StringProperty=lambda **_kwargs: None,
        BoolProperty=lambda **_kwargs: None,
        IntProperty=lambda **_kwargs: None,
        CollectionProperty=lambda **_kwargs: None,
    ),
    data=types.SimpleNamespace(objects=_FakeBpyDataObjects()),
    path=types.SimpleNamespace(abspath=lambda value: value),
    utils=types.SimpleNamespace(register_class=lambda _cls: None, unregister_class=lambda _cls: None),
)
_install_module("bpy", **_fake_bpy.__dict__)
_install_module(
    f"{PKG}.blueprint.node_postprocess_base",
    SSMTNode_PostProcess_Base=type(
        "_FakePostProcessBase",
        (object,),
        {
            "AUTO_APPENDED_SECTION_MARKERS": (
                "; --- AUTO-APPENDED SLIDER CONTROL PANEL ---",
                "; --- AUTO-APPENDED HEALTH DETECTION MODULE ---",
            ),
            "split_auto_appended_tail_content": classmethod(lambda cls, content: (content, "")),
            "split_anim_driver_block_content": classmethod(lambda cls, content: ("", content)),
        },
    ),
)
_install_module(
    f"{PKG}.utils.log_utils",
    LOG=types.SimpleNamespace(
        debug=lambda *_args, **_kwargs: None,
        info=lambda *_args, **_kwargs: None,
        warning=lambda *_args, **_kwargs: None,
    ),
)
_install_module(
    f"{PKG}.common.global_config",
    GlobalConfig=types.SimpleNamespace(logic_name="ZZMI"),
)
_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(EFMI="EFMI", NTEMI="NTEMI", ZZMI="ZZMI", HIMI="HIMI"),
)
_install_module(
    f"{PKG}.toolkit.tt_dds_conversion",
    convert_texture_to_dds_cached=lambda *_args, **_kwargs: (True, ""),
    is_dds_file=lambda path: str(path or "").lower().endswith(".dds"),
)


ROOT = Path(__file__).resolve().parents[1]


def _load_blueprint_module(module_name):
    spec = importlib.util.spec_from_file_location(
        f"{PKG}.blueprint.{module_name}", ROOT / "blueprint" / f"{module_name}.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


fx_namespace = _load_blueprint_module("fx_namespace")
node_postprocess_material = _load_blueprint_module("node_postprocess_material")


class _FakeImage:
    def __init__(self, filepath):
        self.filepath = filepath
        self.name = os.path.basename(filepath)


class _FakeTextureNode:
    type = "TEX_IMAGE"

    def __init__(self, filepath):
        self.image = _FakeImage(filepath)


class _FakeNodeTree:
    def __init__(self, texture_path):
        self.nodes = [_FakeTextureNode(texture_path)] if texture_path else []


class _FakeMaterial:
    def __init__(self, name, texture_path=""):
        self.name = name
        self.use_nodes = bool(texture_path)
        self.node_tree = _FakeNodeTree(texture_path) if texture_path else None


class _FakeMaterialSlot:
    def __init__(self, material):
        self.material = material


class _FakeObject(dict):
    def __init__(self, name, materials, texture_slots=None,
                 bound_box=None, matrix_world=None, armature=None):
        super().__init__()
        self.name = name
        self.original_object_name = name
        self.material_slots = []
        for material_name, texture_path in materials:
            self.material_slots.append(_FakeMaterialSlot(_FakeMaterial(material_name, texture_path)))
        self["modimp_texture_slots"] = json.dumps(texture_slots or {})
        # 自动层号用的几何信息；默认全空，走「算不出 → 第 0 层」回退。
        self.bound_box = bound_box
        self.matrix_world = matrix_world
        self.armature = armature

    def find_armature(self):
        return self.armature


class _FakeArmature:
    def __init__(self, matrix_world, name="Armature"):
        self.name = name
        self.matrix_world = matrix_world


def _identity_matrix():
    return [
        [1.0, 0.0, 0.0, 0.0],
        [0.0, 1.0, 0.0, 0.0],
        [0.0, 0.0, 1.0, 0.0],
        [0.0, 0.0, 0.0, 1.0],
    ]


def _bound_box_with_radius(radius):
    """8 个角点，水平方向离原点最远正好 ``radius`` 米（竖直方向不参与）。"""
    corners = [(0.0, 0.0, 0.0)] * 7
    corners.append((float(radius), 0.0, 0.0))
    return corners


def _write_png(temp_dir, filename):
    path = os.path.join(temp_dir, filename)
    with open(path, "wb") as file_obj:
        file_obj.write(filename.encode("ascii"))
    return path


def _ttl_node():
    node = node_postprocess_material.SSMTNode_PostProcess_Material()
    node.name = "MaterialNode"
    node.material_to_resource_override = False
    node.material_switch_var = "$swapkey150"
    # 真实基类里的备份动作与本测试无关，且 fake 基类没有这个方法。
    node._create_cumulative_backup = lambda *_args, **_kwargs: None
    return node


def _section_text(title, mesh_name):
    return "\n".join([
        f"[{title}]",
        "hash = 241deac5",
        "match_first_index = 0",
        "ib = Resource_Body_Index",
        "run = CommandListSkinTexture",
        f"; [mesh:{mesh_name}]",
        "drawindexed = 100, 5, 0",
    ]) + "\n"


def _export_bytes(temp_dir, ini_text, node=None):
    """把 ini 写进临时目录，跑一遍真实导出路径，返回最终文件的字节。"""
    ini_path = os.path.join(temp_dir, "mod.ini")
    with open(ini_path, "w", encoding="utf-8", newline="\n") as file_obj:
        file_obj.write(ini_text)
    (node or _ttl_node()).execute_postprocess(temp_dir)
    return Path(ini_path).read_bytes()


def _run_process(node, sections, section_name):
    node.process_texture_override_section(
        section_name, sections,
        material_group_to_swapkey={},
        swap_key_prefix="$swapkey",
        next_swap_key_num=150,
        used_swap_keys=set(),
        transparency_sections_to_add=OrderedDict(),
    )


def _reset_fake_scene():
    _fake_bpy.data.objects.clear()
    node_postprocess_material.clear_name_mapping_cache()
    sys.modules[f"{PKG}.common.global_config"].GlobalConfig.logic_name = "ZZMI"


class TTLLayerExportTests(unittest.TestCase):
    def setUp(self):
        _reset_fake_scene()

    # ---------------------------------------------------------------- H1/H2
    def test_two_real_exports_are_byte_identical(self):
        """同一场景（带层号+权重）真实导出两次到两个目录，最终文件逐字节相同。"""
        with tempfile.TemporaryDirectory() as work_dir:
            ttl_path = _write_png(work_dir, "ttl.png")
            obj = _FakeObject("Body_TTL层1_TTL权重0.6", [("TTLMap_0.5_x", ttl_path)])
            _fake_bpy.data.objects[obj.name] = obj
            ini_text = _section_text("TextureOverride_Body", obj.name)

            first_dir = os.path.join(work_dir, "export_a")
            second_dir = os.path.join(work_dir, "export_b")
            os.makedirs(first_dir)
            os.makedirs(second_dir)
            first = _export_bytes(first_dir, ini_text)
            second = _export_bytes(second_dir, ini_text)

        self.assertEqual(first, second)
        body = first.decode("utf-8")
        self.assertIn(f"${BS}TTL{BS}layer_index = 1", body)
        self.assertIn(f"${BS}TTL{BS}alpha = $TTLAlpha0_3", body)
        self.assertIn("global $TTLAlpha0_3 = 0.3", body)

    def test_section_permutation_keeps_layer_ids(self):
        """正序/倒序喂同一组 sections，层号仍跟物体走，序列化字节相同。

        资源段/常量段是处理时按需插入的；两个物体共用同名材质与同一张图，
        因此插入结果只有一个 ``[Resource_TTLMap_Mask]`` 与一个常量，不存在
        由处理顺序决定的段序，字节比较才有意义。
        """
        with tempfile.TemporaryDirectory() as work_dir:
            ttl_path = _write_png(work_dir, "ttl.png")
            layered = _FakeObject("BodyA_TTL层1", [("TTLMap_0.4_Mask", ttl_path)])
            plain = _FakeObject("BodyB", [("TTLMap_0.4_Mask", ttl_path)])
            _fake_bpy.data.objects[layered.name] = layered
            _fake_bpy.data.objects[plain.name] = plain

            def run(order):
                # 只清名字/资源缓存，不动物体表：两个层序共用同一份场景。
                node_postprocess_material.clear_name_mapping_cache()
                sections = OrderedDict([
                    ("[TextureOverride_A]", [
                        f"[mesh:{layered.name}]", "drawindexed = 100, 5, 0",
                    ]),
                    ("[TextureOverride_B]", [
                        f"[mesh:{plain.name}]", "drawindexed = 200, 7, 0",
                    ]),
                    ("_config_path", work_dir),
                ])
                node = _ttl_node()
                for section_name in order:
                    _run_process(node, sections, section_name)
                del sections["_config_path"]
                return node._serialize_ini_content([], sections)

            forward = run(["[TextureOverride_A]", "[TextureOverride_B]"])
            backward = run(["[TextureOverride_B]", "[TextureOverride_A]"])

        self.assertEqual(forward, backward)
        # 层号只出现在 A 的块里，且始终是 1（不是按处理顺序分配）。
        layer_line = f"${BS}TTL{BS}layer_index = 1"
        self.assertEqual(forward.count(layer_line), 1)
        a_start = forward.index(f"[mesh:{layered.name}]")
        b_start = forward.index(f"[mesh:{plain.name}]")
        self.assertIn(layer_line, forward[a_start:b_start])
        # 无后缀的 B 走自动判定但 r 不可算 → 落 0，仍显式写 0（每段自包含）。
        self.assertIn(f"${BS}TTL{BS}layer_index = 0", forward[b_start:])

    def test_old_project_matches_frozen_golden(self):
        """无后缀老工程（只有 TTLMap、无层号/权重）的导出文本逐字节冻结。"""
        with tempfile.TemporaryDirectory() as work_dir:
            ttl_path = _write_png(work_dir, "ttl.png")
            obj = _FakeObject("Body", [("TTLMap_Generic", ttl_path)])
            _fake_bpy.data.objects[obj.name] = obj
            exported = _export_bytes(work_dir, _section_text("TextureOverride_Body", "Body"))

        frozen_golden = "\n".join([
            "[TextureOverride_Body]",
            "hash = 241deac5",
            "match_first_index = 0",
            "ib = Resource_Body_Index",
            "run = CommandListSkinTexture",
            "; [mesh:Body]",
            f"Resource{BS}TTL{BS}TransparencyTex = ref Resource_TTLMap_Generic",
            f"${BS}TTL{BS}mask_channel = 3",
            f"${BS}TTL{BS}mask_invert = 1",
            f"${BS}TTL{BS}alpha = $TTLAlpha1_0",
            f"${BS}TTL{BS}layer_index = 0",
            f"${BS}TTL{BS}V = 1.5",
            f"${BS}TTL{BS}_1 = 100",
            f"${BS}TTL{BS}_2 = 5",
            f"${BS}TTL{BS}_3 = 0",
            f"run = CommandList{BS}TTL{BS}Draw",
            "",
            "[Resource_TTLMap_Generic]",
            "filename = Textures/TTLMap_Generic.png",
            "",
            "[Constants]",
            "global $TTLAlpha1_0 = 1.0",
            "",
        ]).encode("utf-8")

        self.assertEqual(exported, frozen_golden)

    def test_no_ttl_project_stays_byte_identical(self):
        """没有任何 TTLMap 材质的工程：导出逐字节等于原文件，不出现层号/自动注释。"""
        with tempfile.TemporaryDirectory() as work_dir:
            obj = _FakeObject("PlainBody", [])
            _fake_bpy.data.objects[obj.name] = obj
            ini_text = _section_text("TextureOverride_Plain", obj.name)
            exported = _export_bytes(work_dir, ini_text)

        self.assertEqual(exported, ini_text.encode("utf-8"))
        self.assertNotIn(b"layer_index", exported)
        self.assertNotIn(b"auto-layer", exported)

    def test_auto_matches_manual_layer(self):
        """全局聚类到第 2 层的物体，和手写 `_TTL层2` 得到同一条 layer_index。"""
        with tempfile.TemporaryDirectory() as work_dir:
            ttl_path = _write_png(work_dir, "ttl.png")
            armature = _FakeArmature(_identity_matrix())
            auto_objects = []
            for name, radius in (
                ("BodySmall", 0.10), ("BodyMid", 0.20), ("BodyTarget", 0.40),
            ):
                obj = _FakeObject(
                    name,
                    [("TTLMap_Generic", ttl_path)],
                    bound_box=_bound_box_with_radius(radius),
                    matrix_world=_identity_matrix(),
                    armature=armature,
                )
                _fake_bpy.data.objects[name] = obj
                auto_objects.append((name, obj))
            ini_text = "\n".join(
                _section_text(f"TextureOverride_Auto{i}", name)
                for i, (name, _obj) in enumerate(auto_objects)
            )
            auto_body = _export_bytes(work_dir, ini_text).decode("utf-8")

            manual_dir = os.path.join(work_dir, "manual")
            os.makedirs(manual_dir)
            _reset_fake_scene()
            manual_obj = _FakeObject("BodyTarget_TTL层2", [("TTLMap_Generic", ttl_path)])
            _fake_bpy.data.objects[manual_obj.name] = manual_obj
            manual_body = _export_bytes(
                manual_dir, _section_text("TextureOverride_Manual", manual_obj.name)
            ).decode("utf-8")

        layer_line = f"${BS}TTL{BS}layer_index = 2"
        auto_lines = auto_body.splitlines()
        manual_lines = manual_body.splitlines()
        self.assertEqual(auto_lines.count(layer_line), 1)
        self.assertEqual(manual_lines.count(layer_line), 1)
        comment = "; auto-layer: 2 (r=0.400m, gap=0.200 from prev)"
        self.assertEqual(auto_lines.count(comment), 1)
        self.assertEqual(auto_lines[auto_lines.index(layer_line) - 1], comment)
        self.assertFalse(any("auto-layer" in line for line in manual_lines))
        # 聚类单调：0.10→0、0.20→1、0.40→2。
        self.assertIn(f"${BS}TTL{BS}layer_index = 0", auto_body)
        self.assertIn(f"${BS}TTL{BS}layer_index = 1", auto_body)

    def test_auto_layer_clusters(self):
        """全局排序 + 相邻聚类：r=0.10/0.102/0.20/0.202/0.40 → 0/0/1/1/2。"""
        cluster = node_postprocess_material.SSMTNode_PostProcess_Material._ttl_auto_cluster_layers
        result = cluster([
            (0.40, "E"), (0.102, "B"), (0.202, "D"), (0.10, "A"), (0.20, "C"),
        ])
        self.assertEqual([item[0] for item in result], ["A", "B", "C", "D", "E"])
        self.assertEqual([item[1] for item in result], [0, 0, 1, 1, 2])
        self.assertIsNone(result[0][2])
        for item, expected in zip(result[1:], (0.002, 0.098, 0.002, 0.198)):
            self.assertAlmostEqual(item[2], expected, places=9)
        self.assertFalse(any(item[3] for item in result))

        # gap 正好 0.005：<= 归同一层（浮点尾差也要吃掉）。
        boundary = cluster([(0.10, "A"), (0.105, "B")])
        self.assertEqual([item[1] for item in boundary], [0, 0])
        self.assertAlmostEqual(boundary[1][2], 0.005, places=9)
        above_boundary = cluster([(0.10, "A"), (0.105001, "B")])
        self.assertEqual([item[1] for item in above_boundary], [0, 1])

    def test_auto_close_planes_export_to_separate_layers(self):
        """相邻半径差 9~37mm 的四个平面，在同一绘制段中分别生成四层。"""
        scene = [
            ("LOD0.9040e2c4-41253-0.Plane_copy", 0.145, 0),
            ("LOD0.9040e2c4-41253-0.Plane.001_copy", 0.192, 2),
            ("LOD0.9040e2c4-41253-0.Plane.002_copy", 0.182, 1),
            ("LOD0.9040e2c4-41253-0.Plane.003_copy", 0.201, 3),
        ]
        with tempfile.TemporaryDirectory() as work_dir:
            ttl_path = _write_png(work_dir, "ttl.png")
            ini_lines = [
                "[TextureOverride_Planes]",
                "hash = 9040e2c4",
                "match_first_index = 0",
                "ib = Resource_Planes_Index",
                "run = CommandListSkinTexture",
            ]
            for index, (name, radius, _layer) in enumerate(scene):
                _fake_bpy.data.objects[name] = _FakeObject(
                    name,
                    [("TTLMap_Generic", ttl_path)],
                    bound_box=_bound_box_with_radius(radius),
                    matrix_world=_identity_matrix(),
                )
                ini_lines.extend([
                    f"; [mesh:{name}] [vertex_count:4]",
                    f"drawindexed = 6, {41253 + index * 6}, 0",
                ])
            body = _export_bytes(work_dir, "\n".join(ini_lines) + "\n").decode("utf-8")

        self.assertEqual(body.count("layer_index"), len(scene))
        for index, (name, _radius, layer) in enumerate(scene):
            start = body.index(f"[mesh:{name}]")
            end = body.index(f"[mesh:{scene[index + 1][0]}]") if index + 1 < len(scene) else len(body)
            self.assertIn(f"${BS}TTL{BS}layer_index = {layer}", body[start:end])

    def test_auto_layer_overflow(self):
        """6 个间隔 >0.005 的物体：第 6 个聚类层 >4 → WARN + 落 4 + 进清单。"""
        with tempfile.TemporaryDirectory() as work_dir:
            ttl_path = _write_png(work_dir, "ttl.png")
            names = []
            for index in range(6):
                name = f"Layer{index}"
                obj = _FakeObject(
                    name,
                    [("TTLMap_Generic", ttl_path)],
                    bound_box=_bound_box_with_radius(0.10 + index * 0.10),
                    matrix_world=_identity_matrix(),
                )
                _fake_bpy.data.objects[name] = obj
                names.append(name)
            ini_text = "\n".join(
                _section_text(f"TextureOverride_Layer{index}", name)
                for index, name in enumerate(names)
            )
            log_utils = sys.modules[f"{PKG}.utils.log_utils"]
            original_warning = log_utils.LOG.warning
            warnings = []
            log_utils.LOG.warning = lambda message: warnings.append(str(message))
            try:
                node = _ttl_node()
                body = _export_bytes(work_dir, ini_text, node=node).decode("utf-8")
            finally:
                log_utils.LOG.warning = original_warning

        self.assertIn(names[-1], node._ttl_auto_layer_skipped)
        self.assertTrue(any(names[-1] in message for message in warnings))
        self.assertTrue(any("超过" in message for message in warnings))
        block = body[body.index(f"[mesh:{names[-1]}]"):]
        self.assertIn(f"${BS}TTL{BS}layer_index = 4", block)
        self.assertIn("; auto-layer: 4 (r=0.600m, gap=0.100 from prev)", block)
        layers = [
            int(line.split("=")[-1])
            for line in body.splitlines() if "layer_index" in line
        ]
        self.assertEqual(layers, [0, 1, 2, 3, 4, 4])

    def test_auto_layer_order_independent(self):
        """section 顺序打乱，层号映射不变。"""
        names_radii = [("A", 0.10), ("B", 0.102), ("C", 0.20), ("D", 0.202)]
        objects = []
        for name, radius in names_radii:
            obj = _FakeObject(
                name,
                [("TTLMap_Generic", "")],
                bound_box=_bound_box_with_radius(radius),
                matrix_world=_identity_matrix(),
            )
            _fake_bpy.data.objects[name] = obj
            objects.append(obj)

        def sections_in(order):
            sections = OrderedDict(
                (f"[TextureOverride_{name}]", [f"; [mesh:{name}]"])
                for name in order
            )
            # 同一物体在多个 section 只算一次。
            sections["[TextureOverride_Dupe]"] = ["; [mesh:A]"]
            return sections

        def mapping(order):
            node_postprocess_material.clear_name_mapping_cache()
            node = _ttl_node()
            node._ttl_prepare_auto_layers(sections_in(order))
            self.assertEqual(
                len(node._ttl_auto_layer_assignments), len(objects)
            )
            return {
                obj.name: node._ttl_auto_layer_assignments[id(obj)][0]
                for obj in objects
            }

        forward = mapping(["A", "B", "C", "D"])
        backward = mapping(["D", "C", "B", "A"])
        self.assertEqual(forward, backward)
        self.assertEqual(forward, {"A": 0, "B": 0, "C": 1, "D": 1})

    def test_auto_uncomputable_falls_back_zero(self):
        """没有 bound_box 或 r 超出可信范围：落 0（显式写 0）、无注释、物体名进清单。"""
        with tempfile.TemporaryDirectory() as work_dir:
            ttl_path = _write_png(work_dir, "ttl.png")
            no_box = _FakeObject("BodyNoBox", [("TTLMap_Generic", ttl_path)])
            _fake_bpy.data.objects[no_box.name] = no_box
            node = _ttl_node()
            body = _export_bytes(
                work_dir, _section_text("TextureOverride_NoBox", no_box.name), node=node
            ).decode("utf-8")
        self.assertIn(f"${BS}TTL{BS}layer_index = 0", body)
        self.assertNotIn("auto-layer", body)
        self.assertEqual(node._ttl_auto_layer_skipped, ["BodyNoBox"])

        with tempfile.TemporaryDirectory() as work_dir:
            ttl_path = _write_png(work_dir, "ttl.png")
            _reset_fake_scene()
            too_far = _FakeObject(
                "BodyFar",
                [("TTLMap_Generic", ttl_path)],
                bound_box=_bound_box_with_radius(4.0),
                matrix_world=_identity_matrix(),
            )
            _fake_bpy.data.objects[too_far.name] = too_far
            node = _ttl_node()
            body = _export_bytes(
                work_dir, _section_text("TextureOverride_Far", too_far.name), node=node
            ).decode("utf-8")
        self.assertIn(f"${BS}TTL{BS}layer_index = 0", body)
        self.assertEqual(node._ttl_auto_layer_skipped, ["BodyFar"])

    def test_layer_off_emits_no_new_lines(self):
        """无后缀 + 算不出时落 0，layer_index = 0 仍显式写（每段自包含）。"""
        with tempfile.TemporaryDirectory() as work_dir:
            ttl_path = _write_png(work_dir, "ttl.png")
            obj = _FakeObject("Body", [("TTLMap_0.5_遮罩", ttl_path)])
            _fake_bpy.data.objects[obj.name] = obj
            body = _export_bytes(
                work_dir, _section_text("TextureOverride_Body", "Body")
            ).decode("utf-8")

        self.assertIn(f"${BS}TTL{BS}layer_index = 0", body)
        self.assertNotIn("auto-layer", body)  # 算不出，没有自动注释

    def test_five_layers_each_written(self):
        """5 层（0..4）各写各的：每层一条（含 0），alpha 常量互不覆盖。"""
        with tempfile.TemporaryDirectory() as work_dir:
            ttl_path = _write_png(work_dir, "ttl.png")
            bodies = []
            for layer in range(node_postprocess_material._TTL_LAYER_COUNT):
                name = f"BodyL{layer}_TTL层{layer}"
                alpha = f"0.{layer + 1}"
                obj = _FakeObject(name, [(f"TTLMap_{alpha}_x", ttl_path)])
                _fake_bpy.data.objects[name] = obj
                bodies.append((layer, name, alpha))
            ini_text = "\n".join(
                _section_text(f"TextureOverride_Layer{layer}", name)
                for layer, name, _alpha in bodies
            )
            body = _export_bytes(work_dir, ini_text).decode("utf-8")

        # 0..4 各出现一次（层 0 也显式写），总行数正好 5。
        self.assertEqual(body.count("layer_index"), 5)
        for layer in range(node_postprocess_material._TTL_LAYER_COUNT):
            self.assertEqual(
                body.count(f"${BS}TTL{BS}layer_index = {layer}"), 1
            )

        starts = sorted(
            (body.index(f"[mesh:{name}]"), layer, name, alpha)
            for layer, name, alpha in bodies
        )
        for position, (start, layer, _name, alpha) in enumerate(starts):
            block = body[start:starts[position + 1][0]] if position + 1 < len(starts) else body[start:]
            var = "$TTLAlpha" + alpha.replace(".", "_")
            self.assertIn(f"${BS}TTL{BS}layer_index = {layer}", block)
            self.assertEqual(block.count("layer_index"), 1)
            # 每块引用自己的 alpha 常量；五条 global 各一条，互不覆盖。
            self.assertIn(f"${BS}TTL{BS}alpha = {var}", block)
            self.assertEqual(body.count(f"global {var} = {alpha}"), 1)

    def test_out_of_range_layer_fails_export(self):
        """越界层号从导出路径冒泡：整次导出抛 ValueError，不落产物。"""
        with tempfile.TemporaryDirectory() as work_dir:
            ttl_path = _write_png(work_dir, "ttl.png")
            obj = _FakeObject("Body_TTL层5", [("TTLMap_0.5_x", ttl_path)])
            _fake_bpy.data.objects[obj.name] = obj
            ini_text = _section_text("TextureOverride_Body", obj.name)
            with self.assertRaises(ValueError) as raised:
                _export_bytes(work_dir, ini_text)
            # 处理在写回之前抛错，原文件保持原样。
            self.assertEqual(
                Path(os.path.join(work_dir, "mod.ini")).read_text(encoding="utf-8"),
                ini_text,
            )

        message = str(raised.exception)
        self.assertIn("Body_TTL层5", message)
        self.assertIn("0..4", message)
        self.assertNotIn("已按第 0 层处理", message)

    def test_weight_multiplies_alpha(self):
        """``_TTL权重0.6`` × ``TTLMap_0.5_x`` → 0.3；无权重维持 0.5。"""
        with tempfile.TemporaryDirectory() as work_dir:
            ttl_path = _write_png(work_dir, "ttl.png")
            weighted = _FakeObject("Body_TTL权重0.6", [("TTLMap_0.5_x", ttl_path)])
            _fake_bpy.data.objects[weighted.name] = weighted
            weighted_body = _export_bytes(
                work_dir, _section_text("TextureOverride_Body", weighted.name)
            ).decode("utf-8")

        self.assertIn(f"${BS}TTL{BS}alpha = $TTLAlpha0_3", weighted_body)
        self.assertIn("global $TTLAlpha0_3 = 0.3", weighted_body)

        with tempfile.TemporaryDirectory() as work_dir:
            ttl_path = _write_png(work_dir, "ttl.png")
            _reset_fake_scene()
            plain = _FakeObject("Body", [("TTLMap_0.5_x", ttl_path)])
            _fake_bpy.data.objects[plain.name] = plain
            plain_body = _export_bytes(
                work_dir, _section_text("TextureOverride_Body", plain.name)
            ).decode("utf-8")

        self.assertIn(f"${BS}TTL{BS}alpha = $TTLAlpha0_5", plain_body)
        self.assertIn("global $TTLAlpha0_5 = 0.5", plain_body)
        self.assertNotIn("0_3", plain_body)

        # 两个后缀并存且顺序无关：层号与权重同时生效。
        with tempfile.TemporaryDirectory() as work_dir:
            ttl_path = _write_png(work_dir, "ttl.png")
            _reset_fake_scene()
            both = _FakeObject("Body_TTL层1_TTL权重0.6", [("TTLMap_0.5_x", ttl_path)])
            _fake_bpy.data.objects[both.name] = both
            both_body = _export_bytes(
                work_dir, _section_text("TextureOverride_Body", both.name)
            ).decode("utf-8")

        self.assertIn(f"${BS}TTL{BS}layer_index = 1", both_body)
        self.assertIn(f"${BS}TTL{BS}alpha = $TTLAlpha0_3", both_body)

    def test_layer_suffix_parsing_edges(self):
        """边界：0..4 合法；5 / -1 / abc 抛 ValueError（不夹、不回退 0）。"""
        parse_layer = node_postprocess_material.SSMTNode_PostProcess_Material._ttl_layer_token_from_name
        parse_weight = node_postprocess_material.SSMTNode_PostProcess_Material._ttl_weight_token_from_name

        self.assertEqual(parse_layer("Body"), (None, None))
        self.assertEqual(parse_layer("Body_TTL层0"), (0, None))
        self.assertEqual(parse_layer("Body_TTL层1"), (1, None))
        self.assertEqual(parse_layer("Body_TTL层4"), (4, None))

        for bad_name in ("Body_TTL层5", "Body_TTL层-1", "Body_TTL层abc"):
            with self.assertRaises(ValueError, msg=bad_name) as raised:
                parse_layer(bad_name)
            message = str(raised.exception)
            self.assertIn(bad_name, message)
            self.assertIn("0..4", message)
            self.assertIn("请改成 0..4 或删掉后缀", message)

        # 常量与测试口径一致：5 层 = 0..4。
        self.assertEqual(
            node_postprocess_material._TTL_LAYER_MAX_INDEX, 4
        )
        self.assertEqual(node_postprocess_material._TTL_LAYER_COUNT, 5)

        self.assertEqual(parse_weight("Body"), (None, None))
        self.assertEqual(parse_weight("Body_TTL权重0.6"), (0.6, None))
        self.assertEqual(parse_weight("Body_TTL权重1.0"), (1.0, None))
        weight, warning = parse_weight("Body_TTL权重1.5")
        self.assertEqual(weight, 1.0)
        self.assertTrue(warning)
        weight, warning = parse_weight("Body_TTL权重abc")
        self.assertEqual(weight, 1.0)
        self.assertTrue(warning)

        node = _ttl_node()
        self.assertEqual(node._ttl_layer_settings_for_mesh("Body"), (0, 1.0))
        self.assertEqual(
            node._ttl_layer_settings_for_mesh("Body_TTL层1_TTL权重0.6"), (1, 0.6)
        )
        self.assertEqual(
            node._ttl_layer_settings_for_mesh("Body_TTL权重0.6_TTL层1"), (1, 0.6)
        )


if __name__ == "__main__":
    unittest.main()
