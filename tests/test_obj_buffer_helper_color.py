import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest import mock

import numpy as np


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_obj_buffer_helper_color_test_pkg"
for package_name in (PKG, f"{PKG}.common", f"{PKG}.utils"):
    package = _install_module(package_name)
    package.__path__ = []

from tests import _real_modules as _real_modules
_real_modules.register_real_common_modules(f"{PKG}.common")


_install_module("bpy", types=types.SimpleNamespace(Object=object, Mesh=object))
_install_module(f"{PKG}.common.d3d11_gametype", D3D11GameType=object)
_install_module(f"{PKG}.common.draw_call_model", DrawCallModel=object)
_install_module(
    f"{PKG}.common.logic_name",
    LogicName=types.SimpleNamespace(
        WWMI="WWMI",
        NTEMI="NTEMI",
        EFMI="EFMI",
        YYSLS="YYSLS",
        ZZMI="ZZMI",
        SnowBreak="SnowBreak",
        HIMI="HIMI",
    ),
)
_install_module(f"{PKG}.common.global_config", GlobalConfig=types.SimpleNamespace(logic_name="GIMI"))
_install_module(
    f"{PKG}.common.global_properties",
    GlobalProterties=types.SimpleNamespace(
        recalculate_color=lambda: False,
        recalculate_tangent=lambda: False,
        import_merged_vgmap=lambda: False,
    ),
)
_install_module(
    f"{PKG}.utils.ssmt_error_utils",
    SSMTErrorUtils=types.SimpleNamespace(raise_fatal=lambda message: (_ for _ in ()).throw(RuntimeError(message))),
    Fatal=RuntimeError,
)
_install_module(f"{PKG}.utils.vertexgroup_utils", VertexGroupUtils=types.SimpleNamespace())
_install_module(f"{PKG}.utils.timer_utils", TimerUtils=types.SimpleNamespace(Start=lambda *_: None, End=lambda *_: None))
_install_module(f"{PKG}.utils.tbn_codec", TBNCodec=types.SimpleNamespace())


def _load_module(module_name, relative_path):
    module_path = Path(__file__).resolve().parents[1] / relative_path
    spec = importlib.util.spec_from_file_location(f"{PKG}.{module_name}", module_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


format_utils_module = _load_module("utils.format_utils", "utils/format_utils.py")
_load_module("utils.color_attribute_utils", "utils/color_attribute_utils.py")
obj_buffer_helper_module = _load_module("common.obj_buffer_helper", "common/obj_buffer_helper.py")

ObjBufferHelper = obj_buffer_helper_module.ObjBufferHelper
FormatUtils = format_utils_module.FormatUtils


class ImportedSurfaceBasisTests(unittest.TestCase):
    def _parse_with_mixed_imported_basis(self):
        class Records:
            def __init__(self, **fields):
                self.fields = fields

            def __len__(self):
                return len(next(iter(self.fields.values())))

            def foreach_get(self, field, result):
                result[:] = np.asarray(self.fields[field]).ravel()

        loops = Records(
            vertex_index=[0, 1, 1, 2],
            tangent=[[1, 0, 0], [0, 0, 1], [0, 1, 0], [0, 0, 1]],
            bitangent_sign=[1, -1, 1, 1],
        )
        mesh = types.SimpleNamespace(
            vertices=[None, None, None],
            corner_normals=Records(vector=[[0, 1, 0], [1, 0, 0], [0, 0, 1], [0, 1, 0]]),
            attributes={
                "3DMigoto_NORMAL_XYZ": types.SimpleNamespace(
                    data=Records(vector=[[0, 0, 1], [0, 0, 0], [np.nan, 0, 0]]),
                ),
                "3DMigoto_TANGENT_XYZ": types.SimpleNamespace(
                    data=Records(vector=[[0, 1, 0], [0, 0, 0], [np.inf, 0, 0]]),
                ),
                "3DMigoto_TANGENT_W": types.SimpleNamespace(data=Records(value=[1, 0, 1])),
            },
        )

        return (
            ObjBufferHelper._parse_normal(
                loops, len(loops), types.SimpleNamespace(Format="R32G32B32_FLOAT"), mesh=mesh,
            ),
            ObjBufferHelper._parse_tangent(
                loops, len(loops), types.SimpleNamespace(Format="R32G32B32A32_FLOAT"), mesh=mesh,
            ),
        )

    def test_missing_imported_normals_and_tangents_use_evaluated_loop_data(self):
        normals, tangents = self._parse_with_mixed_imported_basis()

        np.testing.assert_array_equal(normals, [[0, 0, 1], [1, 0, 0], [0, 0, 1], [0, 1, 0]])
        np.testing.assert_array_equal(tangents, [[0, 1, 0, 1], [0, 0, 1, 1], [0, 1, 0, -1], [0, 0, 1, -1]])

    def test_zzmi_generated_tangent_keeps_native_bitangent_handedness(self):
        with mock.patch.object(obj_buffer_helper_module.GlobalConfig, "logic_name", "ZZMI"):
            _normals, tangents = self._parse_with_mixed_imported_basis()

        np.testing.assert_array_equal(
            tangents,
            [[0, 1, 0, 1], [0, 0, 1, -1], [0, 1, 0, 1], [0, 0, 1, 1]],
        )

    def test_zzmi_rebuilt_tangent_w_matches_final_uv0_orientation(self):
        dtype = np.dtype([
            ("TEXCOORD", "<f4", (2,)),
            ("TANGENT", "<f4", (4,)),
        ])
        vb = np.zeros(6, dtype=dtype)
        vb["TEXCOORD"] = [
            (0.0, 0.0),
            (1.0, 0.0),
            (0.0, 1.0),
            (0.0, 0.0),
            (0.0, 1.0),
            (1.0, 0.0),
        ]
        vb["TANGENT"][:, 3] = [
            -1.0,
            -1.0,
            -1.0,
            1.0,
            1.0,
            1.0,
        ]

        repaired = ObjBufferHelper.stabilize_zzmi_rebuilt_tangent_w(
            vb,
            [0, 1, 2, 3, 4, 5],
        )

        np.testing.assert_array_equal(
            repaired["TANGENT"][:, 3],
            [1.0, 1.0, 1.0, -1.0, -1.0, -1.0],
        )


def test_deduplication_reads_actual_offsets_for_aliased_fields():
    dtype = np.dtype({
        "names": ["POSITION", "TEXCOORD1", "TEXCOORD2"],
        "formats": ["u1", "<u2", "<u2"],
        "offsets": [0, 4, 4],
        "itemsize": 8,
    })
    vertices = np.zeros(2, dtype=dtype)
    vertices["TEXCOORD1"] = [0x1234, 0x5678]
    columns = ObjBufferHelper._deduplicate_active_columns(dtype, {"TEXCOORD2"}, 8)
    np.testing.assert_array_equal(np.flatnonzero(columns), [4, 5])
    selected = ObjBufferHelper._filter_deduplicate_elements(vertices, dtype, None, {"TEXCOORD2"}, 2)
    np.testing.assert_array_equal(selected, [[0x34, 0x12], [0x78, 0x56]])


class DummyColorAttributeData:
    def __init__(self, records, field_name):
        self.records = np.asarray(records, dtype=np.float32).reshape(-1, 4)
        self.field_name = field_name

    def __len__(self):
        return len(self.records)

    def foreach_get(self, field_name, output):
        if field_name != self.field_name:
            raise AssertionError(f"unexpected field {field_name}")
        output[:] = self.records.reshape(-1)


class DummyColorAttribute:
    def __init__(self, name, domain, data_type, records):
        self.name = name
        self.domain = domain
        self.data_type = data_type
        self.data = DummyColorAttributeData(
            records=records,
            field_name="color" if data_type == "FLOAT_COLOR" else "color_srgb",
        )


class DummyColorAttributes(dict):
    def __contains__(self, item):
        return dict.__contains__(self, item)


class DummyVertexColors(dict):
    def __contains__(self, item):
        return dict.__contains__(self, item)


class DummyLoops:
    def __init__(self, vertex_indices):
        self.vertex_indices = np.asarray(vertex_indices, dtype=np.int32)

    def __len__(self):
        return len(self.vertex_indices)

    def foreach_get(self, field_name, output):
        if field_name != "vertex_index":
            raise AssertionError(f"unexpected loop field: {field_name}")
        output[:] = self.vertex_indices


class DummyMesh:
    def __init__(self, vertex_indices, color_attr):
        self.loops = DummyLoops(vertex_indices)
        self.color_attributes = DummyColorAttributes({color_attr.name: color_attr})
        self.vertex_colors = DummyVertexColors()


class ObjBufferHelperColorTests(unittest.TestCase):
    def test_parse_color_expands_point_domain_to_loop_order(self):
        mesh = DummyMesh(
            vertex_indices=[2, 0, 1, 2],
            color_attr=DummyColorAttribute(
                name="COLOR",
                domain="POINT",
                data_type="BYTE_COLOR",
                records=[
                    (0.1, 0.1, 0.1, 1.0),
                    (0.2, 0.2, 0.2, 1.0),
                    (0.3, 0.3, 0.3, 1.0),
                ],
            ),
        )
        element = types.SimpleNamespace(Format="R8G8B8A8_UNORM")

        result = ObjBufferHelper._parse_color(mesh, len(mesh.loops), "COLOR", element)

        expected = FormatUtils.convert_4x_float32_to_r8g8b8a8_unorm(
            np.array(
                [
                    (0.3, 0.3, 0.3, 1.0),
                    (0.1, 0.1, 0.1, 1.0),
                    (0.2, 0.2, 0.2, 1.0),
                    (0.3, 0.3, 0.3, 1.0),
                ],
                dtype=np.float32,
            )
        )
        np.testing.assert_array_equal(result, expected)

    def test_parse_color_supports_snorm_output(self):
        mesh = DummyMesh(
            vertex_indices=[0, 1],
            color_attr=DummyColorAttribute(
                name="COLOR",
                domain="CORNER",
                data_type="FLOAT_COLOR",
                records=[
                    (-1.0, 0.0, 1.0, 1.0),
                    (0.5, -0.5, 0.25, -0.25),
                ],
            ),
        )
        element = types.SimpleNamespace(Format="R8G8B8A8_SNORM")

        result = ObjBufferHelper._parse_color(mesh, len(mesh.loops), "COLOR", element)

        expected = FormatUtils.convert_4x_float32_to_r8g8b8a8_snorm(
            np.array(
                [
                    (-1.0, 0.0, 1.0, 1.0),
                    (0.5, -0.5, 0.25, -0.25),
                ],
                dtype=np.float32,
            )
        )
        np.testing.assert_array_equal(result, expected)

    def test_parse_color_supports_r16g16b16a16_unorm(self):
        mesh = DummyMesh(
            vertex_indices=[0],
            color_attr=DummyColorAttribute(
                name="COLOR",
                domain="CORNER",
                data_type="BYTE_COLOR",
                records=[(0.25, 0.5, 0.75, 1.0)],
            ),
        )
        element = types.SimpleNamespace(Format="R16G16B16A16_UNORM")

        result = ObjBufferHelper._parse_color(mesh, len(mesh.loops), "COLOR", element)

        expected = FormatUtils.convert_4x_float32_to_r16g16b16a16_unorm(
            np.array([(0.25, 0.5, 0.75, 1.0)], dtype=np.float32)
        )
        np.testing.assert_array_equal(result, expected)


    def test_parse_color_encodes_promoted_float_color_back_to_srgb_bytes(self):
        """复现 body.skin 变色：混合类型 join 后 BYTE_COLOR 的 128 变成线性 0.21586050。

        128 是 sRGB 字节，它的线性值是 0.21586050；旧实现直接 ×255 量化成 55。
        游戏里那还是同一份顶点色，所以导出必须回到 128。
        """
        linear_value = 0.21586050
        mesh = DummyMesh(
            vertex_indices=[0, 1],
            color_attr=DummyColorAttribute(
                name="COLOR",
                domain="CORNER",
                data_type="FLOAT_COLOR",
                records=[
                    (linear_value, linear_value, linear_value, 1.0),
                    (linear_value, linear_value, linear_value, 1.0),
                ],
            ),
        )
        element = types.SimpleNamespace(Format="R8G8B8A8_UNORM")

        result = ObjBufferHelper._parse_color(mesh, len(mesh.loops), "COLOR", element)

        np.testing.assert_array_equal(
            result,
            np.array([[128, 128, 128, 255], [128, 128, 128, 255]], dtype=np.uint8),
        )

    def test_parse_color_leaves_float_color_alpha_unencoded(self):
        """alpha 是遮罩不是颜色：FLOAT_COLOR 的 0.25 要写成 64，不能被当成线性颜色编码。"""
        mesh = DummyMesh(
            vertex_indices=[0],
            color_attr=DummyColorAttribute(
                name="COLOR",
                domain="CORNER",
                data_type="FLOAT_COLOR",
                records=[(0.0, 0.0, 0.0, 0.25)],
            ),
        )
        element = types.SimpleNamespace(Format="R8G8B8A8_UNORM")

        result = ObjBufferHelper._parse_color(mesh, len(mesh.loops), "COLOR", element)

        np.testing.assert_array_equal(result, np.array([[0, 0, 0, 64]], dtype=np.uint8))

    def test_parse_color_keeps_byte_color_bytes_identical(self):
        """BYTE_COLOR 层就是游戏原始 8bit 数据：导出后必须逐字节不变，128 还是 128。"""
        mesh = DummyMesh(
            vertex_indices=[0],
            color_attr=DummyColorAttribute(
                name="COLOR",
                domain="CORNER",
                data_type="BYTE_COLOR",
                records=[(128 / 255, 128 / 255, 128 / 255, 1.0)],
            ),
        )
        element = types.SimpleNamespace(Format="R8G8B8A8_UNORM")

        result = ObjBufferHelper._parse_color(mesh, len(mesh.loops), "COLOR", element)

        np.testing.assert_array_equal(result, np.array([[128, 128, 128, 255]], dtype=np.uint8))


    def test_parse_color_round_trips_every_promoted_byte_value(self):
        """join 的提升是 sRGB→线性，导出必须正好是它的逆：8bit 全部 256 个值逐值回原。"""
        byte_values = np.arange(256, dtype=np.float32) / 255.0
        promoted_linear = np.where(
            byte_values <= 0.04045,
            byte_values / 12.92,
            ((byte_values + 0.055) / 1.055) ** 2.4,
        ).astype(np.float32)
        mesh = DummyMesh(
            vertex_indices=list(range(256)),
            color_attr=DummyColorAttribute(
                name="COLOR",
                domain="CORNER",
                data_type="FLOAT_COLOR",
                records=[(float(value), float(value), float(value), 1.0) for value in promoted_linear],
            ),
        )
        element = types.SimpleNamespace(Format="R8G8B8A8_UNORM")

        result = ObjBufferHelper._parse_color(mesh, len(mesh.loops), "COLOR", element)

        np.testing.assert_array_equal(result[:, 0], np.arange(256, dtype=np.uint8))
        np.testing.assert_array_equal(result[:, 3], np.full(256, 255, dtype=np.uint8))


class ObjBufferHelperBlendIndicesTests(unittest.TestCase):
    @staticmethod
    def _element(fmt, byte_width):
        return types.SimpleNamespace(
            SemanticIndex=0,
            Format=fmt,
            ByteWidth=byte_width,
        )

    def test_parse_r16g16_uint_after_r32_downcast(self):
        source = np.array([[12, 513, 999, 1000]], dtype=np.uint32)
        result = ObjBufferHelper._parse_blendindices(
            {0: source}, self._element("R16G16_UINT", 4)
        )
        self.assertEqual(result.dtype, np.dtype(np.uint16))
        self.assertEqual(result.tolist(), [[12, 513]])

    def test_parse_r16g16b16a16_sint_preserves_signed_dtype(self):
        source = np.array([[12, 513, 1024, 2048]], dtype=np.uint32)
        result = ObjBufferHelper._parse_blendindices(
            {0: source}, self._element("R16G16B16A16_SINT", 8)
        )
        self.assertEqual(result.dtype, np.dtype(np.int16))
        self.assertEqual(result.tolist(), [[12, 513, 1024, 2048]])

    def test_parse_r16_uint_rejects_overflow_before_cast(self):
        source = np.array([[65536]], dtype=np.uint32)
        with self.assertRaisesRegex(RuntimeError, "R16_UINT.*max=65536"):
            ObjBufferHelper._parse_blendindices(
                {0: source}, self._element("R16_UINT", 2)
            )

    def test_parse_r16_sint_rejects_unsigned_bone_id_above_signed_limit(self):
        source = np.array([[32768]], dtype=np.uint32)
        with self.assertRaisesRegex(RuntimeError, "R16_SINT.*max=32768"):
            ObjBufferHelper._parse_blendindices(
                {0: source}, self._element("R16_SINT", 2)
            )

    def test_parse_r8_uint_rejects_negative_index_before_cast(self):
        source = np.array([[-1]], dtype=np.int32)
        with self.assertRaisesRegex(RuntimeError, "R8_UINT.*min=-1"):
            ObjBufferHelper._parse_blendindices(
                {0: source}, self._element("R8_UINT", 1)
            )


class ObjBufferHelperAverageNormalColorTests(unittest.TestCase):
    """average_normal_color：COLOR 通道必须写成**单位长度**的逐位置平滑法线。

    游戏（HI3 2.0）约定 rgb = n * 0.5 + 0.5，实测游戏自带数据 |decode(COLOR)| = 0.9988 ~ 1.0；
    硬边处"平均向量"本身就短（90° 硬边只有 0.707），因此编码前必须归一化，否则描边位移会按
    |mean| 缩水（实测用户产物 32.4% 顶点长度 < 0.95）。见 common/obj_buffer_helper.py 的 docstring。
    """

    DTYPE = np.dtype([("POSITION", "<f4", 3), ("NORMAL", "<f4", 3), ("COLOR", "u1", 4)])

    def setUp(self):
        self._original_recalculate_color = obj_buffer_helper_module.GlobalProterties.recalculate_color
        obj_buffer_helper_module.GlobalProterties.recalculate_color = lambda: True
        # 全局开关只在 HI3 2.0（HIMI）下真正改写 COLOR：这些用例验证的是 HIMI 口径。
        self._original_logic_name = obj_buffer_helper_module.GlobalConfig.logic_name
        obj_buffer_helper_module.GlobalConfig.logic_name = "HIMI"
        self._had_width_accessor = hasattr(obj_buffer_helper_module.GlobalProterties, "recalculate_color_width")
        self._original_width = getattr(obj_buffer_helper_module.GlobalProterties, "recalculate_color_width", None)
        obj_buffer_helper_module.GlobalProterties.recalculate_color_width = lambda: 0.5
        self.game_type = types.SimpleNamespace(
            OrderedFullElementList=["POSITION", "NORMAL", "COLOR"]
        )

    def tearDown(self):
        obj_buffer_helper_module.GlobalProterties.recalculate_color = self._original_recalculate_color
        obj_buffer_helper_module.GlobalConfig.logic_name = self._original_logic_name
        if self._had_width_accessor:
            obj_buffer_helper_module.GlobalProterties.recalculate_color_width = self._original_width
        elif hasattr(obj_buffer_helper_module.GlobalProterties, "recalculate_color_width"):
            delattr(obj_buffer_helper_module.GlobalProterties, "recalculate_color_width")

    def _vertex(self, position, normal, alpha=200):
        row = np.zeros((), dtype=self.DTYPE)
        row["POSITION"] = position
        row["NORMAL"] = normal
        row["COLOR"] = (10, 20, 30, alpha)
        return row.tobytes()

    def _recalculate(self, vertices, game_type=None):
        return ObjBufferHelper.average_normal_color(
            obj={},
            indexed_vertices=vertices,
            d3d11GameType=game_type or self.game_type,
            dtype=self.DTYPE,
        )

    @staticmethod
    def _decoded(result):
        colors = np.asarray(result["COLOR"], dtype=np.float64)
        return colors[:, :3] / 255.0 * 2.0 - 1.0

    def test_non_himi_global_switch_does_not_overwrite_game_color(self):
        """非 HIMI（UI 不显示该栏）时，全局开关不得改写游戏原生顶点色。

        回归：默认值 AVERAGE 曾让 ZZZ 的 COLOR（游戏原生光照/材质遮罩）被这套
        "法线编码 + 描边宽度"覆盖，表现为角色属性界面光影质感错乱。
        """
        obj_buffer_helper_module.GlobalConfig.logic_name = "ZZMI"
        vertices = [
            self._vertex((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), alpha=17),
            self._vertex((0.0, 0.0, 0.0), (0.0, 1.0, 0.0), alpha=17),
        ]
        result = self._recalculate(vertices)

        colors = np.frombuffer(b"".join(result), dtype=self.DTYPE)["COLOR"]
        np.testing.assert_array_equal(
            colors, np.array([[10, 20, 30, 17], [10, 20, 30, 17]], dtype=np.uint8)
        )

    def test_non_himi_object_marker_still_applies(self):
        """非 HIMI 时物体级 3DMigoto:RecalculateCOLOR 仍然生效（手动开启路径保留）。"""
        obj_buffer_helper_module.GlobalConfig.logic_name = "ZZMI"
        result = ObjBufferHelper.average_normal_color(
            obj={"3DMigoto:RecalculateCOLOR": True},
            indexed_vertices=[self._vertex((1.0, 0.0, 0.0), (0.0, 0.0, 1.0), alpha=17)],
            d3d11GameType=self.game_type,
            dtype=self.DTYPE,
        )

        vectors = self._decoded(result)
        np.testing.assert_allclose(vectors[0], (0.0, 0.0, 1.0), atol=0.01)
        self.assertEqual(int(result["COLOR"][0, 3]), 128)

    def test_hard_edge_average_is_stored_as_unit_vector(self):
        result = self._recalculate(
            [
                self._vertex((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
                self._vertex((0.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
                self._vertex((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
            ]
        )
        vectors = self._decoded(result)

        expected = np.array([1.0, 1.0, 0.0]) / np.sqrt(2.0)
        np.testing.assert_allclose(vectors[0], expected, atol=0.01)
        np.testing.assert_allclose(vectors[1], expected, atol=0.01)
        np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=0.01)
        # 非硬边位置写入自身法线方向
        np.testing.assert_allclose(vectors[2], (0.0, 0.0, 1.0), atol=0.01)

    def test_short_average_is_normalized_not_truncated(self):
        angle = np.radians(170.0)
        result = self._recalculate(
            [
                self._vertex((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
                self._vertex((0.0, 0.0, 0.0), (np.cos(angle), np.sin(angle), 0.0)),
            ]
        )
        vectors = self._decoded(result)

        # 平均向量长度只有 0.174，修复前会被原样写入（描边位移缩水成 1/6）
        expected = np.array([1.0 + np.cos(angle), np.sin(angle), 0.0])
        expected = expected / np.linalg.norm(expected)
        np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=0.01)
        np.testing.assert_allclose(vectors[0], expected, atol=0.01)

    def test_opposite_normals_fall_back_to_a_real_normal(self):
        result = self._recalculate(
            [
                self._vertex((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
                self._vertex((0.0, 0.0, 0.0), (-1.0, 0.0, 0.0)),
            ]
        )
        vectors = self._decoded(result)

        # 平均向量为零：回退到该位置第一条可用法线，而不是塌成零向量 / 量化噪声
        np.testing.assert_allclose(vectors[0], (1.0, 0.0, 0.0), atol=0.01)
        np.testing.assert_allclose(vectors[1], (1.0, 0.0, 0.0), atol=0.01)

    def test_zero_normals_stay_finite(self):
        result = self._recalculate(
            [
                self._vertex((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
                self._vertex((0.0, 0.0, 0.0), (0.0, 0.0, 0.0)),
            ]
        )
        vectors = self._decoded(result)

        self.assertTrue(np.isfinite(vectors).all())
        np.testing.assert_allclose(vectors, 0.0, atol=0.01)

    def test_unit_input_stays_unit(self):
        result = self._recalculate(
            [
                self._vertex((0.0, 0.0, 0.0), (0.6, 0.8, 0.0)),
                self._vertex((1.0, 0.0, 0.0), (0.0, 0.6, 0.8)),
            ]
        )
        vectors = self._decoded(result)

        np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=0.01)
        np.testing.assert_allclose(vectors[0], (0.6, 0.8, 0.0), atol=0.01)

    def test_alpha_defaults_to_half_and_is_not_inherited(self):
        """alpha = 描边宽度旋钮本身：默认 0.5（128/255），且**不继承**输入旧值。

        旧口径是"保留输入 alpha"，那会把上一次写错的值（例如 255）污染到下一次 —— 实机踩过。
        现在写进去的就是 recalculate_color_width（0.5 = 游戏原版，实测原值中位 ≈ 118/255）。
        """
        result = self._recalculate(
            [
                self._vertex((0.0, 0.0, 0.0), (1.0, 0.0, 0.0), alpha=17),
                self._vertex((1.0, 0.0, 0.0), (0.0, 1.0, 0.0), alpha=0),
            ]
        )

        self.assertEqual(list(result["COLOR"][:, 3]), [128, 128])

    def test_width_scale_thins_outline_without_changing_direction(self):
        vertices = [
            self._vertex((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
            self._vertex((0.0, 0.0, 0.0), (0.0, 1.0, 0.0)),
            self._vertex((1.0, 0.0, 0.0), (0.0, 0.0, 1.0)),
        ]
        obj_buffer_helper_module.GlobalProterties.recalculate_color_width = lambda: 1.0
        base = self._recalculate(vertices)
        obj_buffer_helper_module.GlobalProterties.recalculate_color_width = lambda: 0.25
        result = self._recalculate(vertices)
        vectors = self._decoded(result)

        # 实测口径（ShaderFixes/91f78fc667efbeba 的反汇编）：描边外扩 = 方向 × 常量 × COLOR.a
        # ⇒ 变细靠 alpha 变小；RGB（单位法线）连长度都不能变，否则 8bit 量化会把方向弄噪
        np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=0.01)
        expected_dir = np.array([1.0, 1.0, 0.0]) / np.sqrt(2.0)
        np.testing.assert_allclose(vectors[0] / np.linalg.norm(vectors[0]), expected_dir, atol=0.02)
        np.testing.assert_allclose(vectors[2] / np.linalg.norm(vectors[2]), (0.0, 0.0, 1.0), atol=0.02)
        expected_alpha = np.round(base["COLOR"][:, 3].astype(float) * 0.25).astype(int)
        np.testing.assert_array_equal(result["COLOR"][:, 3].astype(int), expected_alpha)

    def test_width_scale_is_clamped_to_one(self):
        # >1 会在 8bit 每分量上截断而使方向失真，所以按 1.0 处理
        obj_buffer_helper_module.GlobalProterties.recalculate_color_width = lambda: 1.5

        vectors = self._decoded(self._recalculate([self._vertex((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))]))

        np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=0.01)

    def test_invalid_width_scale_falls_back_to_unit(self):
        obj_buffer_helper_module.GlobalProterties.recalculate_color_width = lambda: float("nan")

        vectors = self._decoded(self._recalculate([self._vertex((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))]))

        np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=0.01)

    def test_per_object_width_override_wins_over_global(self):
        obj_buffer_helper_module.GlobalProterties.recalculate_color_width = lambda: 1.0
        base = self._recalculate([self._vertex((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))])

        result = ObjBufferHelper.average_normal_color(
            obj={"3DMigoto:RecalculateCOLORWidth": 0.25},
            indexed_vertices=[self._vertex((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))],
            d3d11GameType=self.game_type,
            dtype=self.DTYPE,
        )

        # 物体级覆盖同样作用在 alpha（宽度遮罩）上；RGB 长度保持 1.0
        np.testing.assert_allclose(np.linalg.norm(self._decoded(result), axis=1), 1.0, atol=0.01)
        expected = int(round(int(base["COLOR"][0, 3]) * 0.25))
        self.assertEqual(int(result["COLOR"][0, 3]), expected)

    def test_missing_width_accessor_defaults_to_unit(self):
        # 旧版调用方（测试替身 / 老属性组）没有这个访问器时不能炸，也不能改宽度
        if hasattr(obj_buffer_helper_module.GlobalProterties, "recalculate_color_width"):
            delattr(obj_buffer_helper_module.GlobalProterties, "recalculate_color_width")

        vectors = self._decoded(self._recalculate([self._vertex((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))]))

        np.testing.assert_allclose(np.linalg.norm(vectors, axis=1), 1.0, atol=0.01)

    def test_alpha_is_the_width_knob(self):
        """写进 COLOR.a 的就是旋钮值本身（不再有"基准 × 系数"两次相乘的坑）。"""
        for value, expected in ((0.5, 128), (0.25, 64), (1.0, 255), (0.0, 0)):
            obj_buffer_helper_module.GlobalProterties.recalculate_color_width = lambda v=value: v
            result = self._recalculate([self._vertex((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))])
            self.assertEqual(int(result["COLOR"][0, 3]), expected, "width=%s" % value)

    def test_per_loop_mode_keeps_split_normals(self):
        """同一个位置的两条相反法线：默认（按位置平均）会互相抵消→回退同一条，

        而逐 loop 直写模式下两个顶点各自保留自己的方向。"""
        vertices = [
            self._vertex((0.0, 0.0, 0.0), (1.0, 0.0, 0.0)),
            self._vertex((0.0, 0.0, 0.0), (-1.0, 0.0, 0.0)),
        ]
        averaged = self._decoded(self._recalculate(list(vertices)))

        obj_buffer_helper_module.GlobalProterties.recalculate_color_mode = lambda: "PER_LOOP"
        try:
            per_loop = self._decoded(self._recalculate(list(vertices)))
        finally:
            delattr(obj_buffer_helper_module.GlobalProterties, "recalculate_color_mode")

        np.testing.assert_allclose(averaged[0], averaged[1], atol=0.02)
        np.testing.assert_allclose(per_loop[0], (1.0, 0.0, 0.0), atol=0.02)
        np.testing.assert_allclose(per_loop[1], (-1.0, 0.0, 0.0), atol=0.02)

    def test_himi_color_is_written_in_the_exported_normal_space(self):
        """HIMI：写 COLOR 时**不做**任何额外坐标变换，COLOR 与 NORMAL 列同空间。

        依据（对游戏原始 dump 实测，不经过 Blender）：
          · 产物 NORMAL 列 vs 游戏 NORMAL：中位夹角 0.51°（同空间）
          · 产物 COLOR    vs 游戏 COLOR ：中位夹角 101.44°（旧实现多转了 mirrorX·Rx(-90)）
          · 游戏自身 COLOR vs 同顶点 NORMAL：13.9°（大件）/41.5°（头发）—— 差值只来自"平滑 vs 逐面"
        流水线顺序也支持这一点：common/submesh_model.py:428 先把导出空间变换烘焙进临时物体，
        utils/export_utils.py:577 才轮到 average_normal_color ⇒ 此刻 loop 法线已在导出空间。
        非 HIMI 游戏不再进入这条路径（全局开关只对 HIMI 生效），因此这里同时验证它们原样返回。
        """
        logic = obj_buffer_helper_module.GlobalConfig.logic_name
        had_flag = hasattr(obj_buffer_helper_module.GlobalConfig, "enable_non_mirror_workflow")
        previous_flag = getattr(obj_buffer_helper_module.GlobalConfig, "enable_non_mirror_workflow", None)
        try:
            for logic_name in ("HIMI", "WWMI"):
                for mirrored in (True, False):
                    obj_buffer_helper_module.GlobalConfig.logic_name = logic_name
                    obj_buffer_helper_module.GlobalConfig.enable_non_mirror_workflow = mirrored
                    result = self._recalculate([self._vertex((0.0, 0.0, 0.0), (0.0, 1.0, 0.0))])
                    if logic_name != "HIMI":
                        # 非 HIMI：全局开关不改写游戏原生 COLOR，原样返回输入
                        self.assertIsInstance(result, list)
                        continue
                    written = self._decoded(result)
                    np.testing.assert_allclose(written[0], (0.0, 1.0, 0.0), atol=0.02,
                                               err_msg="%s(非镜像=%s) 不应该改动法线方向" % (logic_name, mirrored))
        finally:
            obj_buffer_helper_module.GlobalConfig.logic_name = logic
            if had_flag:
                obj_buffer_helper_module.GlobalConfig.enable_non_mirror_workflow = previous_flag
            elif hasattr(obj_buffer_helper_module.GlobalConfig, "enable_non_mirror_workflow"):
                delattr(obj_buffer_helper_module.GlobalConfig, "enable_non_mirror_workflow")

    def test_missing_color_attribute_is_created_before_write(self):
        """物体本身没有顶点色时，重算前必须先创建 COLOR 属性 —— 否则写入没有落点。"""

        class _ColorAttributes:
            def __init__(self):
                self.items = {}
                self.active_color_index = -1

            def get(self, name):
                return self.items.get(name)

            def new(self, name=None, type=None, domain=None):
                created = types.SimpleNamespace(name=name, data_type=type, domain=domain)
                self.items[name] = created
                return created

            def find(self, name):
                return 0 if name in self.items else -1

        class _Mesh:
            def __init__(self):
                self.color_attributes = _ColorAttributes()
                self.vertex_colors = None

        class _Obj(dict):
            def __init__(self):
                super().__init__()
                self.name = "NoColorObject"
                self.data = _Mesh()

        obj = _Obj()
        result = ObjBufferHelper.average_normal_color(
            obj=obj,
            indexed_vertices=[self._vertex((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))],
            d3d11GameType=self.game_type,
            dtype=self.DTYPE,
        )

        self.assertIsNotNone(obj.data.color_attributes.get("COLOR"))
        self.assertEqual(list(result["COLOR"][:, 3]), [128])

    def test_returns_input_when_disabled(self):
        obj_buffer_helper_module.GlobalProterties.recalculate_color = lambda: False
        vertices = [self._vertex((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))]

        self.assertIs(self._recalculate(vertices), vertices)

    def test_returns_input_without_color_element(self):
        game_type = types.SimpleNamespace(OrderedFullElementList=["POSITION", "NORMAL"])
        vertices = [self._vertex((0.0, 0.0, 0.0), (1.0, 0.0, 0.0))]

        self.assertIs(self._recalculate(vertices, game_type=game_type), vertices)


if __name__ == "__main__":
    unittest.main()
