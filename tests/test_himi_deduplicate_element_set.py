"""HIMI「顶点去重精度控制」面板失效的回归测试（重算跑在去重之前的口径）。

修复前 `utils/export_utils.py` 里的门控是

    recalculate_average_normal_enabled = (... or (logic_name == "HIMI" and GlobalProterties.recalculate_color()) ...)
    deduplicate_element_set = None if recalculate_average_normal_enabled else GlobalProterties.get_deduplicate_element_set()

而 `recalculate_color()` 在 `recalculate_color_mode == "AVERAGE"`（默认值）下就返回 True，
所以 HIMI + 默认配置下这一行恒等于 None ⇒ 去重键永远是「全字段」，面板勾了什么都用不上。

现在属性重算（`average_normal_tangent_xxmi` / `average_normal_color`）提前到去重**之前**，
逐 loop 行算完再交给 `calc_index_vertex_buffer_wwmi_v2`。于是：

1. 面板没动过（8 个类型全勾）⇒ 传给去重的仍是 None（历史全字段键）；
2. 面板被显式改过 ⇒ 传下去的就是勾选集本身，一个补项都没有；
3. 去重时看到的 ndarray 已经带着重算后的属性值，所以"先收窄再重算会改变均值/切线"
   这件事从顺序上消失，不需要任何门控。

本文件同时把这些顺序事实做成可断言的证据：重算拿到的是恒等 IB 与全部 loop 行，
去重拿到的是重算后的行，且同位置只差被重算列的那些行确实被合并掉了。

测试不依赖真实 bpy：用 fake 包前缀加载真实模块，只替身掉外围依赖
（桩法同 tests/test_vertex_dedup_ignore_vertex_id.py 与 tests/test_export_utils_state_consistency.py）。
"""

import importlib.util
import sys
import types
from pathlib import Path

import numpy
import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]

# 两套 fake 包树：export_utils 走替身 ObjBufferHelper（记录/模拟重算），
# obj_buffer_helper 走真实实现（去重键构造未被本次修复改动，需要真跑）。
PKG_EXPORT = "_himi_dedup_export_pkg"
PKG_HELPER = "_himi_dedup_helper_pkg"

DEDUPLICATE_ELEMENT_TYPES = (
    "POSITION",
    "NORMAL",
    "TANGENT",
    "BINORMAL",
    "TEXCOORD",
    "COLOR",
    "BLENDWEIGHT",
    "BLENDINDICES",
)


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _install_package(name):
    package = _install_module(name)
    package.__path__ = []
    return package


def _load_real_module(module_name, relative_path):
    spec = importlib.util.spec_from_file_location(module_name, REPO_ROOT / relative_path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    return module


class _AnyFakeType:
    """`bpy.types.XXX` 的占位（模块级注解会在 import 时求值）。"""

    def __init__(self, name):
        self.__name__ = name


class _FakeBpyTypes(types.SimpleNamespace):
    def __getattr__(self, item):
        if item.startswith("__"):
            raise AttributeError(item)
        value = _AnyFakeType(item)
        setattr(self, item, value)
        return value


_install_module(
    "bpy",
    types=_FakeBpyTypes(Mesh=object, Object=object),
    context=types.SimpleNamespace(),
    data=types.SimpleNamespace(),
    props=types.SimpleNamespace(),
    app=types.SimpleNamespace(handlers=types.SimpleNamespace(load_post=[])),
)

# 两套树共用同一份逻辑名 / 全局配置实例：改逻辑名时真实 helper 与 export_utils 必须看到同一个值。
_LOGIC_NAME = types.SimpleNamespace(
    HIMI="HIMI",
    GIMI="GIMI",
    GF2="GF2",
    YYSLS="YYSLS",
    SnowBreak="SnowBreak",
    WWMI="WWMI",
    NTEMI="NTEMI",
    SRMI="SRMI",
    EFMI="EFMI",
    IdentityV="IdentityV",
    HTMI="HTMI",
    ZZMI="ZZMI",
    ZZMIDX12="ZZMIDX12",
)
_GLOBAL_CONFIG = types.SimpleNamespace(logic_name="HIMI")


def _raise_fatal(message):
    raise RuntimeError(message)


# ---------------------------------------------------------------------------
# 第一套树：真实 common/obj_buffer_helper.py（去重键构造不能改，必须真跑）
# ---------------------------------------------------------------------------
for _name in (PKG_HELPER, f"{PKG_HELPER}.common", f"{PKG_HELPER}.utils"):
    _install_package(_name)
sys.modules[f"{PKG_HELPER}.common"].__path__ = [str(REPO_ROOT / "common")]
sys.modules[f"{PKG_HELPER}.utils"].__path__ = [str(REPO_ROOT / "utils")]

_install_module(f"{PKG_HELPER}.common.d3d11_gametype", D3D11GameType=object)
_install_module(f"{PKG_HELPER}.common.draw_call_model", DrawCallModel=object)
_install_module(f"{PKG_HELPER}.common.logic_name", LogicName=_LOGIC_NAME)
_install_module(f"{PKG_HELPER}.common.global_config", GlobalConfig=_GLOBAL_CONFIG)


class _HelperProterties:
    """真实 obj_buffer_helper 读到的开关（与 export_utils 侧的替身分开，避免互相串味）。"""

    tangent_recalculate = False
    tangent_basis_recalculate = False

    @classmethod
    def recalculate_tangent(cls):
        return cls.tangent_recalculate

    @classmethod
    def recalculate_tangent_basis(cls):
        return cls.tangent_basis_recalculate


_install_module(f"{PKG_HELPER}.common.global_properties", GlobalProterties=_HelperProterties)
_install_module(f"{PKG_HELPER}.utils.format_utils", FormatUtils=types.SimpleNamespace())
_install_module(f"{PKG_HELPER}.utils.vertexgroup_utils", VertexGroupUtils=types.SimpleNamespace())
_install_module(f"{PKG_HELPER}.utils.timer_utils", TimerUtils=types.SimpleNamespace())
_install_module(f"{PKG_HELPER}.utils.tbn_codec", TBNCodec=types.SimpleNamespace())
_install_module(
    f"{PKG_HELPER}.utils.color_attribute_utils",
    encode_color_for_unorm_target=lambda rgba_data, values: values,
    read_color_attribute_data=lambda *args, **kwargs: None,
    sample_color_attribute_to_loops=lambda *args, **kwargs: None,
)
_install_module(
    f"{PKG_HELPER}.utils.ssmt_error_utils",
    SSMTErrorUtils=types.SimpleNamespace(raise_fatal=_raise_fatal),
    Fatal=RuntimeError,
)

_REAL_OBJ_BUFFER_HELPER = _load_real_module(
    f"{PKG_HELPER}.common.obj_buffer_helper", Path("common") / "obj_buffer_helper.py"
).ObjBufferHelper


# ---------------------------------------------------------------------------
# 第二套树：真实 utils/export_utils.py
# ---------------------------------------------------------------------------
for _name in (PKG_EXPORT, f"{PKG_EXPORT}.common", f"{PKG_EXPORT}.utils"):
    _install_package(_name)
# 只放开 utils 的搜索路径：export_space 是纯 numpy 模块，可以真导入。
sys.modules[f"{PKG_EXPORT}.utils"].__path__ = [str(REPO_ROOT / "utils")]

_install_module(f"{PKG_EXPORT}.common.d3d11_gametype", D3D11GameType=object)
_install_module(f"{PKG_EXPORT}.common.logic_name", LogicName=_LOGIC_NAME)
_install_module(f"{PKG_EXPORT}.common.global_config", GlobalConfig=_GLOBAL_CONFIG)
_install_module(f"{PKG_EXPORT}.utils.obj_utils", ObjUtils=types.SimpleNamespace())
_install_module(f"{PKG_EXPORT}.utils.shapekey_utils", ShapeKeyUtils=types.SimpleNamespace())
_install_module(f"{PKG_EXPORT}.utils.timer_utils", TimerUtils=types.SimpleNamespace())


class _FakeProterties:
    """替身 GlobalProterties：只保留导出判据读到的几个入口。"""

    deduplicate_types = set(DEDUPLICATE_ELEMENT_TYPES)
    color_recalculate = True       # 默认 recalculate_color_mode == "AVERAGE"
    tangent_recalculate = False
    ignore_vertex_id = False

    @classmethod
    def get_deduplicate_element_set(cls):
        return set(cls.deduplicate_types)

    @classmethod
    def deduplicate_element_set_is_default(cls):
        return len(cls.deduplicate_types) == len(DEDUPLICATE_ELEMENT_TYPES)

    @classmethod
    def recalculate_color(cls):
        return cls.color_recalculate

    @classmethod
    def recalculate_tangent(cls):
        return cls.tangent_recalculate

    @classmethod
    def deduplicate_ignore_vertex_id(cls):
        return cls.ignore_vertex_id

    @classmethod
    def reset(cls):
        cls.deduplicate_types = set(DEDUPLICATE_ELEMENT_TYPES)
        cls.color_recalculate = True
        cls.tangent_recalculate = False
        cls.ignore_vertex_id = False


_install_module(f"{PKG_EXPORT}.common.global_properties", GlobalProterties=_FakeProterties)
_install_package(f"{PKG_EXPORT}.blueprint")


class _FakeBlueprintExportHelper:
    suppress_shapekey = True
    preserve_shapekey_mix = False

    @classmethod
    def should_suppress_shapekey_resource_export(cls):
        return cls.suppress_shapekey

    @classmethod
    def should_preserve_current_shapekey_mix_for_export(cls):
        return cls.preserve_shapekey_mix


_install_module(f"{PKG_EXPORT}.blueprint.export_helper", BlueprintExportHelper=_FakeBlueprintExportHelper)


class _RecordingObjBufferHelper:
    """记录 export_utils 的调用顺序与实参；重算算子做可控替身。"""

    calls = []
    order = []
    recalc_inputs = []

    @staticmethod
    def calc_index_vertex_buffer_wwmi_v2(
        mesh=None,
        element_vertex_ndarray=None,
        dtype=None,
        d3d11_game_type=None,
        deduplicate_element_set=None,
        deduplicate_ignore_vertex_id=False,
        flip_triangles=False,
        **kwargs,
    ):
        color_seen = None
        if element_vertex_ndarray is not None and "COLOR" in (element_vertex_ndarray.dtype.names or ()):
            color_seen = numpy.array(element_vertex_ndarray["COLOR"], dtype=numpy.float32, copy=True)
        _RecordingObjBufferHelper.order.append("deduplicate")
        _RecordingObjBufferHelper.calls.append(
            {
                "deduplicate_element_set": deduplicate_element_set,
                "deduplicate_ignore_vertex_id": deduplicate_ignore_vertex_id,
                "color_seen": color_seen,
                "element_vertex_ndarray": element_vertex_ndarray,
            }
        )
        return _REAL_OBJ_BUFFER_HELPER.calc_index_vertex_buffer_wwmi_v2(
            mesh=mesh,
            element_vertex_ndarray=element_vertex_ndarray,
            dtype=dtype,
            d3d11_game_type=d3d11_game_type,
            deduplicate_element_set=deduplicate_element_set,
            deduplicate_ignore_vertex_id=deduplicate_ignore_vertex_id,
            flip_triangles=flip_triangles,
        )

    @staticmethod
    def average_normal_tangent_xxmi(
        obj=None,
        indexed_vertices=None,
        flattened_ib=None,
        d3d11GameType=None,
        dtype=None,
    ):
        _RecordingObjBufferHelper.order.append("tangent")
        _RecordingObjBufferHelper.recalc_inputs.append(
            {"operator": "tangent", "vertices": indexed_vertices, "flattened_ib": flattened_ib}
        )
        return indexed_vertices

    @staticmethod
    def average_normal_color(obj=None, indexed_vertices=None, d3d11GameType=None, dtype=None):
        _RecordingObjBufferHelper.order.append("color")
        _RecordingObjBufferHelper.recalc_inputs.append({"operator": "color", "vertices": indexed_vertices})
        if not _FakeProterties.color_recalculate:
            return indexed_vertices
        if not isinstance(indexed_vertices, numpy.ndarray):
            return indexed_vertices
        recalculated = indexed_vertices.copy()
        if "COLOR" in (recalculated.dtype.names or ()) and "NORMAL" in (recalculated.dtype.names or ()):
            # 模拟 average_normal_color：同一位置的所有角点拿到同一个平均值。
            mean_normal = numpy.asarray(recalculated["NORMAL"], dtype=numpy.float32).mean(axis=0)
            recalculated["COLOR"][:, :3] = mean_normal
            recalculated["COLOR"][:, 3] = 1.0
        return recalculated


_install_module(f"{PKG_EXPORT}.common.obj_buffer_helper", ObjBufferHelper=_RecordingObjBufferHelper)

ExportUtils = _load_real_module(
    f"{PKG_EXPORT}.utils.export_utils", Path("utils") / "export_utils.py"
).ExportUtils


# ---------------------------------------------------------------------------
# 夹具与桩数据
# ---------------------------------------------------------------------------
DTYPE = numpy.dtype(
    [
        ("POSITION", "<f4", (3,)),
        ("NORMAL", "<f4", (3,)),
        ("COLOR", "<f4", (4,)),
        ("TEXCOORD", "<f4", (2,)),
    ]
)
POSITION_STRIDE = 12

_POSITION = (1.0, 2.0, 3.0)
_NORMAL = (0.0, 0.0, 1.0)
_UV_A = (0.25, 0.75)
_UV_B = (0.5, 0.5)
_COLOR_A = (1.0, 1.0, 1.0, 1.0)
_COLOR_B = (0.5, 0.5, 0.5, 1.0)

# 6 个 loop 全来自同一个源顶点（顶点编号相同）。
_LOOP_VERTEX_IDS = [0, 0, 0, 0, 0, 0]
# 只有 COLOR 分两组：不重算时全字段键给 2 行，重算（同位置同值）后并成 1 行。
_LOOP_COLORS = [_COLOR_A, _COLOR_A, _COLOR_A, _COLOR_B, _COLOR_B, _COLOR_B]
# 只有 TEXCOORD 分两组：用来验证"显式收窄真的会改变导出顶点数"。
_LOOP_UVS = [_UV_A, _UV_A, _UV_A, _UV_B, _UV_B, _UV_B]


class _FakeLoops:
    def __init__(self, vertex_indices):
        self._vertex_indices = numpy.asarray(vertex_indices, dtype=numpy.int64)

    def __len__(self):
        return int(self._vertex_indices.shape[0])

    def foreach_get(self, attribute_name, out):
        assert attribute_name == "vertex_index"
        out[:] = self._vertex_indices


class _FakeMesh:
    def __init__(self, vertex_indices):
        self.loops = _FakeLoops(vertex_indices)


class _FakeObj:
    name = "Body"

    def __init__(self, extra=None):
        self._extra = dict(extra or {})

    def get(self, key, default=None):
        return self._extra.get(key, default)


class _FakeGameType:
    OrderedFullElementList = ["POSITION", "NORMAL", "COLOR", "TEXCOORD"]

    def get_real_category_stride_dict(self):
        return {"Position": 12, "Normal": 12, "Color": 16, "Texcoord": 8}


def _make_rows(colors=None, uvs=None):
    """按 loop 顺序生成 (n_loops, DTYPE) 结构化数组。"""
    colors = list(colors if colors is not None else [_COLOR_A] * 6)
    uvs = list(uvs if uvs is not None else [_UV_A] * 6)
    assert len(colors) == len(uvs)
    rows = numpy.zeros(len(colors), dtype=DTYPE)
    rows["POSITION"] = numpy.asarray([_POSITION] * len(colors), dtype=numpy.float32).reshape(-1, 3)
    rows["NORMAL"] = numpy.asarray([_NORMAL] * len(colors), dtype=numpy.float32).reshape(-1, 3)
    rows["COLOR"] = numpy.asarray(colors, dtype=numpy.float32).reshape(-1, 4)
    rows["TEXCOORD"] = numpy.asarray(uvs, dtype=numpy.float32).reshape(-1, 2)
    return rows


@pytest.fixture(autouse=True)
def _reset_fakes():
    _FakeProterties.reset()
    _HelperProterties.tangent_recalculate = False
    _HelperProterties.tangent_basis_recalculate = False
    _FakeBlueprintExportHelper.suppress_shapekey = True
    _FakeBlueprintExportHelper.preserve_shapekey_mix = False
    _GLOBAL_CONFIG.logic_name = "HIMI"
    _RecordingObjBufferHelper.calls = []
    _RecordingObjBufferHelper.order = []
    _RecordingObjBufferHelper.recalc_inputs = []
    yield
    _FakeProterties.reset()
    _HelperProterties.tangent_recalculate = False
    _HelperProterties.tangent_basis_recalculate = False
    _GLOBAL_CONFIG.logic_name = "HIMI"
    _RecordingObjBufferHelper.calls = []
    _RecordingObjBufferHelper.order = []
    _RecordingObjBufferHelper.recalc_inputs = []


def _run_build(rows=None):
    """跑一遍 build_unity_index_buffers，返回 (记录到的实参, 分类缓冲)。"""
    rows = _make_rows() if rows is None else rows
    _, category_buffer_dict, _, _ = ExportUtils.build_unity_index_buffers(
        obj=_FakeObj(),
        mesh=_FakeMesh(_LOOP_VERTEX_IDS),
        element_vertex_ndarray=rows,
        d3d11_game_type=_FakeGameType(),
        dtype=DTYPE,
    )
    return _RecordingObjBufferHelper.calls[-1], category_buffer_dict


def _exported_vertex_count(category_buffer_dict):
    return len(category_buffer_dict["Position"]) // POSITION_STRIDE


DEFAULT_PANEL = set(DEDUPLICATE_ELEMENT_TYPES)


# ---------------------------------------------------------------------------
# 重算：读全 loop 行、恒等 IB、返回值形态
# ---------------------------------------------------------------------------
def test_recalculate_runs_on_every_loop_row_with_identity_ib():
    rows = _make_rows(_LOOP_COLORS)

    recalculated = ExportUtils.recalculate_element_vertex_ndarray(
        obj=_FakeObj(),
        element_vertex_ndarray=rows,
        d3d11_game_type=_FakeGameType(),
        dtype=DTYPE,
    )

    # 算子拿到的是全部 loop 行（不是去重后的唯一行），IB 是恒等映射
    tangent_input, color_input = _RecordingObjBufferHelper.recalc_inputs
    assert len(tangent_input["vertices"]) == len(_LOOP_VERTEX_IDS)
    assert numpy.array_equal(
        numpy.asarray(tangent_input["flattened_ib"]),
        numpy.arange(len(_LOOP_VERTEX_IDS), dtype=numpy.int32),
    )
    assert len(color_input["vertices"]) == len(_LOOP_VERTEX_IDS)
    # 顺序：先 TANGENT 轮廓线，再 COLOR
    assert _RecordingObjBufferHelper.order == ["tangent", "color"]
    # 返回同 dtype、同长度、可写
    assert recalculated.dtype == rows.dtype
    assert len(recalculated) == len(rows)
    assert recalculated.flags.writeable is True
    assert recalculated is not rows


def test_recalculate_accepts_bytes_list_return(monkeypatch):
    rows = _make_rows(_LOOP_COLORS)
    monkeypatch.setattr(
        _RecordingObjBufferHelper,
        "average_normal_color",
        staticmethod(lambda **kwargs: [row.tobytes() for row in kwargs["indexed_vertices"]]),
    )

    recalculated = ExportUtils.recalculate_element_vertex_ndarray(
        obj=_FakeObj(),
        element_vertex_ndarray=rows,
        d3d11_game_type=_FakeGameType(),
        dtype=DTYPE,
    )

    assert isinstance(recalculated, numpy.ndarray)
    assert recalculated.dtype == rows.dtype
    assert numpy.array_equal(recalculated, rows)


def test_recalculate_falls_back_when_shape_mismatches(monkeypatch):
    rows = _make_rows(_LOOP_COLORS)
    monkeypatch.setattr(
        _RecordingObjBufferHelper,
        "average_normal_color",
        staticmethod(lambda **kwargs: kwargs["indexed_vertices"][:2]),
    )

    recalculated = ExportUtils.recalculate_element_vertex_ndarray(
        obj=_FakeObj(),
        element_vertex_ndarray=rows,
        d3d11_game_type=_FakeGameType(),
        dtype=DTYPE,
    )

    # 长度对不上 ⇒ 原样退回入参，不抛
    assert recalculated is rows


def test_recalculate_is_passthrough_when_operators_early_return():
    rows = _make_rows(_LOOP_COLORS)
    _FakeProterties.color_recalculate = False

    recalculated = ExportUtils.recalculate_element_vertex_ndarray(
        obj=_FakeObj(),
        element_vertex_ndarray=rows,
        d3d11_game_type=_FakeGameType(),
        dtype=DTYPE,
    )

    # 两个算子都 early-return ⇒ 连拷贝都不做
    assert recalculated is rows


def test_recalculate_empty_array_is_passthrough():
    empty = numpy.zeros(0, dtype=DTYPE)
    assert (
        ExportUtils.recalculate_element_vertex_ndarray(
            obj=_FakeObj(),
            element_vertex_ndarray=empty,
            d3d11_game_type=_FakeGameType(),
            dtype=DTYPE,
        )
        is empty
    )


# ---------------------------------------------------------------------------
# 面板口径：默认 ⇒ None；显式 ⇒ 就是勾选
# ---------------------------------------------------------------------------
def test_default_panel_passes_none_full_field_key():
    call, category_buffer_dict = _run_build()

    assert call["deduplicate_element_set"] is None
    # 重算把同位置的 COLOR 抹成同一个值 ⇒ 全字段键也只剩 1 行
    assert _exported_vertex_count(category_buffer_dict) == 1


def test_narrowed_panel_passes_exactly_the_checked_types():
    narrowed = DEFAULT_PANEL - {"COLOR"}
    _FakeProterties.deduplicate_types = set(narrowed)

    call, _ = _run_build()

    assert call["deduplicate_element_set"] == narrowed


def test_hand_picked_panel_gains_no_extra_types():
    # 只勾 POSITION：传下去的就只有 POSITION，不因为重算补任何东西
    _FakeProterties.deduplicate_types = {"POSITION"}

    call, category_buffer_dict = _run_build()

    assert call["deduplicate_element_set"] == {"POSITION"}
    assert _exported_vertex_count(category_buffer_dict) == 1


def test_narrowed_panel_actually_changes_exported_vertex_count():
    # 只有 TEXCOORD 分两组：全字段 ⇒ 2 行；把 TEXCOORD 取消勾选 ⇒ 1 行
    _FakeProterties.color_recalculate = False
    _, default_buffers = _run_build(_make_rows(uvs=_LOOP_UVS))
    assert _exported_vertex_count(default_buffers) == 2

    _FakeProterties.deduplicate_types = DEFAULT_PANEL - {"TEXCOORD"}
    call, narrowed_buffers = _run_build(_make_rows(uvs=_LOOP_UVS))

    assert call["deduplicate_element_set"] == DEFAULT_PANEL - {"TEXCOORD"}
    assert _exported_vertex_count(narrowed_buffers) == 1


# ---------------------------------------------------------------------------
# 顺序证据：去重拿到的是重算后的行
# ---------------------------------------------------------------------------
def test_deduplicate_receives_recalculated_values_before_keying():
    # COLOR 分两组：不重算 ⇒ 2 行；重算（同位置同值）⇒ 1 行。
    # 顶点数只有在"去重看到的是重算后的值"时才会掉下来，这就是顺序证据。
    _FakeProterties.color_recalculate = False
    call, buffers = _run_build(_make_rows(_LOOP_COLORS))
    assert _exported_vertex_count(buffers) == 2
    assert numpy.array_equal(
        call["color_seen"], numpy.asarray(_LOOP_COLORS, dtype=numpy.float32)
    )

    _FakeProterties.color_recalculate = True
    call, buffers = _run_build(_make_rows(_LOOP_COLORS))
    assert _exported_vertex_count(buffers) == 1
    # 去重收到的 ndarray 里 COLOR 已经是重算后的值（不再是原始的 A/B 两色）
    assert not numpy.array_equal(call["color_seen"], numpy.asarray(_LOOP_COLORS, dtype=numpy.float32))
    assert numpy.allclose(call["color_seen"], numpy.asarray([call["color_seen"][0]] * 6))


def test_recalculate_happens_before_deduplicate_call():
    _run_build(_make_rows(_LOOP_COLORS))
    assert _RecordingObjBufferHelper.order == ["tangent", "color", "deduplicate"]


# ---------------------------------------------------------------------------
# WWMI 路径同序 + 空勾选仍报原错误
# ---------------------------------------------------------------------------
def test_wwmi_path_recalculates_before_dedup():
    rows = _make_rows(_LOOP_COLORS)
    element_context = types.SimpleNamespace(
        obj=_FakeObj(),
        mesh=_FakeMesh(_LOOP_VERTEX_IDS),
        d3d11_game_type=_FakeGameType(),
        total_structured_dtype=DTYPE,
        obj_name="Body",
        element_vertex_ndarray=rows,
        original_elementname_data_dict={},
        final_elementname_data_dict={},
    )

    result = ExportUtils.build_wwmi_obj_buffer_result(element_context=element_context)

    assert _RecordingObjBufferHelper.order == ["tangent", "color", "deduplicate"]
    call = _RecordingObjBufferHelper.calls[-1]
    assert call["deduplicate_element_set"] is None
    assert not numpy.array_equal(call["color_seen"], numpy.asarray(_LOOP_COLORS, dtype=numpy.float32))
    # 结果里的 element_vertex_ndarray 是与 ib/category buffer 自洽的那一份（重算后的）
    assert result.element_vertex_ndarray is call["element_vertex_ndarray"]
    assert len(result.unique_element_vertex_ndarray) == 1
    # 入参本身不被改写
    assert numpy.array_equal(rows["COLOR"], numpy.asarray(_LOOP_COLORS, dtype=numpy.float32))


def test_empty_panel_still_raises_original_fatal():
    # 一个都不勾 ⇒ 空键仍然报原错误，不许静默并成一个顶点
    _FakeProterties.deduplicate_types = set()

    with pytest.raises(RuntimeError, match="顶点去重精度控制"):
        _run_build()


def test_no_pinning_api_left_behind():
    # 上一版「补回重算所需类型」的实现不许留下任何入口
    for removed in (
        "_recalculate_required_deduplicate_elements",
        "_deduplicate_element_set_covers_all_fields",
        "_resolve_deduplicate_element_set",
    ):
        assert not hasattr(ExportUtils, removed)


# ---------------------------------------------------------------------------
# 恒等 IB 与真实切线算子：数学部分不需要为「先重算」改一行
# ---------------------------------------------------------------------------
DTYPE_TANGENT = numpy.dtype(
    [
        ("POSITION", "<f4", (3,)),
        ("NORMAL", "<f4", (3,)),
        ("TANGENT", "<f4", (4,)),
    ]
)

# 共享棱 (0,0,0)-(1,0,0) 的两片三角形，夹角 90°：
#   loop 0/1/2 的法线 +Z，loop 3/4/5 的法线 -Y
#   位置 (0,0,0) 在 loop 0 与 loop 3 各出现一次（同位置、不同 loop）
#   ＜重算后 (0,0,0) 处两个直角等权：(0,0,1)+(0,-1,0) 归一 ⇒ (0,-0.70711,0.70711)
_LOOP_POSITIONS_TANGENT = [
    (0.0, 0.0, 0.0),
    (1.0, 0.0, 0.0),
    (0.0, 1.0, 0.0),
    (0.0, 0.0, 0.0),
    (1.0, 0.0, 0.0),
    (0.0, 0.0, 1.0),
]


class _FakeGameTypeTangent:
    OrderedFullElementList = ["POSITION", "NORMAL", "TANGENT"]


def test_identity_ib_drives_real_tangent_operator_per_loop():
    rows = numpy.zeros(len(_LOOP_POSITIONS_TANGENT), dtype=DTYPE_TANGENT)
    rows["POSITION"] = numpy.asarray(_LOOP_POSITIONS_TANGENT, dtype=numpy.float32).reshape(-1, 3)
    rows["NORMAL"] = (0.0, 0.0, 1.0)
    rows["TANGENT"] = (1.0, 0.0, 0.0, 1.0)

    _HelperProterties.tangent_recalculate = True
    _HelperProterties.tangent_basis_recalculate = False
    try:
        result = _REAL_OBJ_BUFFER_HELPER.average_normal_tangent_xxmi(
            obj=_FakeObj(),
            indexed_vertices=rows,
            flattened_ib=numpy.arange(len(rows), dtype=numpy.int32),
            d3d11GameType=_FakeGameTypeTangent(),
            dtype=DTYPE_TANGENT,
        )
    finally:
        _HelperProterties.tangent_recalculate = False

    tangents = numpy.asarray(result["TANGENT"], dtype=numpy.float32)
    # 恒等 IB 不合并任何 loop 行：一行进、一行出
    assert len(result) == len(rows)
    # 每个 loop 都被写到（不再是输入里的占位值）
    assert not numpy.allclose(tangents[:, :3], numpy.asarray([1.0, 0.0, 0.0], dtype=numpy.float32))
    # 同位置跨 loop 合并：loop 0 拿到两片法线的角度加权均值，而不是自己那一片的法线
    assert numpy.allclose(tangents[0, :3], (0.0, -0.70711, 0.70711), atol=1e-4)
    assert numpy.allclose(tangents[3, :3], tangents[0, :3], atol=1e-4)
    # 只属于一片的位置仍是该片法线
    assert numpy.allclose(tangents[2, :3], (0.0, 0.0, 1.0), atol=1e-4)
    assert numpy.allclose(tangents[5, :3], (0.0, -1.0, 0.0), atol=1e-4)
    # w 符号重写照旧生效（输入 +1 ⇒ -1），说明算子走完了
    assert numpy.allclose(tangents[:, 3], -1.0)

