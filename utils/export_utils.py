import math
import json
from time import perf_counter

import bpy
import numpy
from contextlib import contextmanager

from dataclasses import dataclass, field
from typing import Optional

from ..common.global_config import GlobalConfig
from ..common.logic_name import LogicName
from ..common.global_properties import GlobalProterties
from .obj_utils import ObjUtils
from .shapekey_utils import ShapeKeyUtils
from .timer_utils import TimerUtils
from .export_space import convert_position_coords

from ..common.d3d11_gametype import D3D11GameType
from ..blueprint.export_helper import BlueprintExportHelper
from ..common.obj_buffer_helper import ObjBufferHelper


@dataclass
class ObjElementContext:
    """导出元素上下文数据类"""

    obj: bpy.types.Object
    mesh: bpy.types.Mesh
    d3d11_game_type: D3D11GameType
    obj_name: str
    total_structured_dtype: numpy.dtype
    original_elementname_data_dict: dict = field(default_factory=dict)
    final_elementname_data_dict: dict = field(default_factory=dict)
    element_vertex_ndarray: Optional[numpy.ndarray] = field(default=None, repr=False)


@dataclass
class UnityBufferBuildResult:
    obj_name: str
    dtype: numpy.dtype
    element_vertex_ndarray: numpy.ndarray = field(repr=False)
    ib: list = field(default_factory=list)
    category_buffer_dict: dict = field(default_factory=dict, repr=False)
    index_loop_id_dict: Optional[dict] = field(default=None, repr=False)
    unique_first_loop_indices: Optional[numpy.ndarray] = field(default=None, repr=False)
    shape_key_buffer_dict: dict = field(default_factory=dict, repr=False)


@dataclass
class ShapeKeyBufferBuildResult:
    name: str
    element_vertex_ndarray: Optional[numpy.ndarray] = field(default=None, repr=False)
    position_bytes: Optional[numpy.ndarray] = field(default=None, repr=False)


@dataclass
class WWMIBufferBuildResult:
    obj: bpy.types.Object = field(repr=False)
    mesh: bpy.types.Mesh = field(repr=False)
    d3d11_game_type: D3D11GameType = field(repr=False)
    obj_name: str
    dtype: numpy.dtype
    element_vertex_ndarray: numpy.ndarray = field(repr=False)
    ib: list = field(default_factory=list)
    category_buffer_dict: dict = field(default_factory=dict, repr=False)
    index_vertex_id_dict: Optional[dict] = field(default=None, repr=False)
    shapekey_offsets: list = field(default_factory=list, repr=False)
    shapekey_vertex_ids: list = field(default_factory=list, repr=False)
    shapekey_vertex_offsets: list = field(default_factory=list, repr=False)
    export_shapekey: bool = False
    unique_element_vertex_ndarray: Optional[numpy.ndarray] = field(default=None, repr=False)
    unique_first_loop_indices: Optional[numpy.ndarray] = field(default=None, repr=False)


class ExportUtils:
    """导出工具类，负责模型数据打包、索引缓冲区和 ShapeKey 缓冲区构建"""

    @staticmethod
    @contextmanager
    def _temporarily_reset_shapekey_values_for_export(obj: bpy.types.Object):
        if BlueprintExportHelper.should_preserve_current_shapekey_mix_for_export():
            yield
            return

        with ShapeKeyUtils.preserve_shape_key_state(obj):
            ShapeKeyUtils.reset_shapekey_values(obj)
            ShapeKeyUtils._update_view_layer()
            yield

    @staticmethod
    def build_obj_element_context(
        d3d11_game_type: D3D11GameType,
        obj: Optional[bpy.types.Object] = None,
        obj_name: str = "",
        final_elementname_data_dict: Optional[dict] = None,
    ) -> ObjElementContext:
        profile_started = perf_counter()
        resolved_obj = obj or ObjUtils.get_obj_by_name(name=obj_name)
        resolved_at = perf_counter()
        with ExportUtils._temporarily_reset_shapekey_values_for_export(resolved_obj):
            entered_at = perf_counter()
            mesh = ObjUtils.get_mesh_evaluate_from_obj(obj=resolved_obj)
            evaluated_at = perf_counter()
            if len(mesh.polygons) > 0:
                uv_map_name = "TEXCOORD.xy" if "TEXCOORD.xy" in mesh.uv_layers else None
                try:
                    mesh.calc_tangents(uvmap=uv_map_name)
                except RuntimeError:
                    ObjUtils.mesh_triangulate(mesh)
                    mesh.calc_tangents(uvmap=uv_map_name)

            tangents_at = perf_counter()
            original_elementname_data_dict = ObjBufferHelper.parse_elementname_data_dict(
                mesh=mesh,
                d3d11_game_type=d3d11_game_type,
                # 【t66 根治】把**组名**传下去：BLENDINDICES 的槽号按组名解析（组名 = 全局槽的十进制写法），
                # 而不是取 `object.vertex_groups` 的列表序号 —— 后者在"索引≠名字"时会绑错骨。
                # 取不到 `vertex_groups`（替身/特殊对象）⇒ 传 None = 旧行为（逐字节等价）。
                vertex_group_names=[
                    vg.name for vg in (getattr(resolved_obj, "vertex_groups", None) or ())
                ] or None,
                # 【t88】把物体名带下去：组名不合规时报错要能指名道姓。
                object_name=getattr(resolved_obj, "name", "") or "",
            )
            parsed_at = perf_counter()

        restored_at = perf_counter()
        context = ObjElementContext(
            obj=resolved_obj,
            mesh=mesh,
            d3d11_game_type=d3d11_game_type,
            obj_name=resolved_obj.name,
            total_structured_dtype=d3d11_game_type.get_total_structured_dtype(),
            original_elementname_data_dict=original_elementname_data_dict,
            final_elementname_data_dict=final_elementname_data_dict or {},
        )
        print('[ElementParseProfile] ' + json.dumps({
            'object': resolved_obj.name,
            'vertices': len(getattr(mesh, 'vertices', ())),
            'loops': len(getattr(mesh, 'loops', ())),
            'resolve_s': resolved_at - profile_started,
            'shapekey_enter_s': entered_at - resolved_at,
            'evaluate_s': evaluated_at - entered_at,
            'tangents_s': tangents_at - evaluated_at,
            'attributes_s': parsed_at - tangents_at,
            'shapekey_restore_s': restored_at - parsed_at,
            'context_s': perf_counter() - restored_at,
            'total_s': perf_counter() - profile_started,
        }), flush=True)
        return context

    @staticmethod
    def pack_element_vertex_ndarray(
        d3d11_game_type: D3D11GameType,
        mesh: bpy.types.Mesh,
        original_elementname_data_dict: dict,
        final_elementname_data_dict: Optional[dict] = None,
    ) -> numpy.ndarray:
        return ObjBufferHelper.convert_to_element_vertex_ndarray(
            d3d11_game_type=d3d11_game_type,
            mesh=mesh,
            original_elementname_data_dict=original_elementname_data_dict,
            final_elementname_data_dict=final_elementname_data_dict or {},
        )

    @staticmethod
    def _get_key_block_coords(key_block) -> Optional[numpy.ndarray]:
        if key_block is None:
            return None

        coords = numpy.empty((len(key_block.data), 3), dtype=numpy.float32)
        key_block.data.foreach_get("co", coords.ravel())
        return coords

    @staticmethod
    def _get_basis_key_block(obj: bpy.types.Object):
        key_blocks = getattr(getattr(getattr(obj, "data", None), "shape_keys", None), "key_blocks", None)
        if not key_blocks:
            return None
        return key_blocks[0]

    @staticmethod
    def _get_position_element(d3d11_game_type: D3D11GameType):
        position_element = d3d11_game_type.ElementNameD3D11ElementDict.get("POSITION")
        if position_element is not None:
            return position_element

        for d3d11_element in d3d11_game_type.D3D11ElementList:
            if d3d11_element.Category == "Position" and d3d11_element.ElementName == "POSITION":
                return d3d11_element
        return None

    @staticmethod
    def get_effective_export_logic_name(logic_name: Optional[str] = None) -> str:
        resolved_logic_name = str(logic_name or getattr(GlobalConfig, "logic_name", "") or "")
        if resolved_logic_name == getattr(LogicName, "HTMI", "HTMI"):
            return getattr(LogicName, "EFMI", "EFMI")
        return resolved_logic_name

    @staticmethod
    def requires_export_space_transform(logic_name: Optional[str] = None) -> bool:
        resolved_logic_name = ExportUtils.get_effective_export_logic_name(logic_name)
        return resolved_logic_name in {
            getattr(LogicName, "APMI", "APMI"),
            getattr(LogicName, "SRMI", "SRMI"),
            getattr(LogicName, "GIMI", "GIMI"),
            getattr(LogicName, "HIMI", "HIMI"),
            getattr(LogicName, "YYSLS", "YYSLS"),
            getattr(LogicName, "IdentityV", "IdentityV"),
            getattr(LogicName, "SnowBreak", "SnowBreak"),
            getattr(LogicName, "EFMI", "EFMI"),
        }

    @staticmethod
    def convert_position_coords_for_export(sampled_coords, logic_name: Optional[str] = None) -> numpy.ndarray:
        resolved_logic_name = ExportUtils.get_effective_export_logic_name(logic_name)
        return convert_position_coords(sampled_coords, logic_name=resolved_logic_name)

    @staticmethod
    def convert_position_deltas_for_export(sampled_deltas, logic_name: Optional[str] = None) -> numpy.ndarray:
        return ExportUtils.convert_position_coords_for_export(sampled_deltas, logic_name=logic_name)

    @staticmethod
    def apply_export_space_transform_to_object(temp_obj: bpy.types.Object, logic_name: Optional[str] = None):
        if temp_obj is None:
            return

        resolved_logic_name = ExportUtils.get_effective_export_logic_name(logic_name)
        if resolved_logic_name in {
            getattr(LogicName, "APMI", "APMI"),
            getattr(LogicName, "SRMI", "SRMI"),
            getattr(LogicName, "GIMI", "GIMI"),
            getattr(LogicName, "HIMI", "HIMI"),
            getattr(LogicName, "YYSLS", "YYSLS"),
            getattr(LogicName, "IdentityV", "IdentityV"),
        }:
            ObjUtils.select_obj(temp_obj)
            temp_obj.rotation_euler[0] = math.radians(-90)
            temp_obj.rotation_euler[1] = 0
            temp_obj.rotation_euler[2] = 0
            ShapeKeyUtils.transform_apply_preserve_shape_keys(temp_obj, location=False, rotation=True, scale=True)
            return

        if resolved_logic_name == getattr(LogicName, "SnowBreak", "SnowBreak"):
            ObjUtils.select_obj(temp_obj)
            temp_obj.rotation_euler[0] = 0
            temp_obj.rotation_euler[1] = 0
            temp_obj.rotation_euler[2] = math.radians(180)
            temp_obj.scale = (100, 100, 100)
            ShapeKeyUtils.transform_apply_preserve_shape_keys(temp_obj, location=False, rotation=True, scale=True)
            return

        if resolved_logic_name == getattr(LogicName, "EFMI", "EFMI"):
            temp_obj.rotation_euler[0] = 0
            temp_obj.rotation_euler[1] = 0
            temp_obj.rotation_euler[2] = 0
            if all(abs(axis - 1.0) <= 1e-7 for axis in temp_obj.scale):
                return
            ObjUtils.select_obj(temp_obj)
            ShapeKeyUtils.transform_apply_preserve_shape_keys(temp_obj, location=False, rotation=True, scale=True)

    @staticmethod
    def _position_element_layout(position_element, position_stride: int):
        position_stride = int(position_stride or 0)
        if position_stride <= 0:
            raise ValueError(f"Invalid POSITION stride: {position_stride}")

        offset = int(getattr(position_element, "AlignedByteOffset", 0) or 0) if position_element is not None else 0
        byte_width = int(getattr(position_element, "ByteWidth", 0) or 0) if position_element is not None else 0
        if byte_width <= 0:
            if position_stride == 8:
                byte_width = 8
            elif position_stride == 16:
                byte_width = 16
            else:
                byte_width = 12

        position_format = str(getattr(position_element, "Format", "") or "").upper() if position_element is not None else ""
        if "16" in position_format and "FLOAT" in position_format:
            dtype = numpy.float16
            item_size = 2
        else:
            dtype = numpy.float32
            item_size = 4

        if offset < 0 or offset + byte_width > position_stride:
            raise ValueError(
                f"POSITION layout out of bounds: offset={offset}, byte_width={byte_width}, stride={position_stride}"
            )

        component_count = max(3, byte_width // item_size)
        return offset, byte_width, dtype, component_count

    @staticmethod
    def format_position_bytes_from_coords(
        sampled_coords,
        d3d11_game_type: Optional[D3D11GameType] = None,
        position_stride: int = 12,
        logic_name: Optional[str] = None,
    ) -> bytes:
        coords = ExportUtils.convert_position_coords_for_export(sampled_coords, logic_name=logic_name)
        if coords.size == 0:
            return b""

        position_element = ExportUtils._get_position_element(d3d11_game_type) if d3d11_game_type is not None else None
        position_format = str(getattr(position_element, "Format", "") or "")
        if position_format == "R32G32B32A32_FLOAT":
            formatted = numpy.zeros((coords.shape[0], 4), dtype=numpy.float32)
            formatted[:, :3] = coords
            return formatted.tobytes()
        if position_format == "R16G16B16A16_FLOAT":
            formatted = numpy.zeros((coords.shape[0], 4), dtype=numpy.float16)
            formatted[:, :3] = coords.astype(numpy.float16)
            formatted[:, 3] = 1.0
            return formatted.tobytes()
        if int(position_stride) == 16:
            formatted = numpy.zeros((coords.shape[0], 4), dtype=numpy.float32)
            formatted[:, :3] = coords
            formatted[:, 3] = 1.0
            return formatted.tobytes()
        if int(position_stride) == 8:
            formatted = numpy.zeros((coords.shape[0], 4), dtype=numpy.float16)
            formatted[:, :3] = coords.astype(numpy.float16)
            formatted[:, 3] = 1.0
            return formatted.tobytes()
        return numpy.asarray(coords, dtype=numpy.float32).tobytes()

    @staticmethod
    def convert_position_buffer_bytes_for_export(
        position_bytes: bytes,
        d3d11_game_type: Optional[D3D11GameType],
        position_stride: int,
        logic_name: Optional[str] = None,
    ) -> bytes:
        if not position_bytes:
            return b""

        if not ExportUtils.requires_export_space_transform(logic_name):
            return bytes(position_bytes)

        position_stride = int(position_stride or 0)
        if position_stride <= 0 or len(position_bytes) % position_stride != 0:
            raise ValueError(
                f"POSITION buffer size does not match stride: bytes={len(position_bytes)}, stride={position_stride}"
            )

        position_element = ExportUtils._get_position_element(d3d11_game_type) if d3d11_game_type is not None else None
        offset, byte_width, dtype, component_count = ExportUtils._position_element_layout(position_element, position_stride)
        vertex_count = int(len(position_bytes) / position_stride)
        coords = numpy.zeros((vertex_count, 3), dtype=numpy.float32)

        for vertex_index in range(vertex_count):
            src_start = vertex_index * position_stride + offset
            src_end = src_start + byte_width
            values = numpy.frombuffer(position_bytes[src_start:src_end], dtype=dtype, count=component_count)
            coords[vertex_index] = values[:3].astype(numpy.float32)

        converted_coords = ExportUtils.convert_position_coords_for_export(coords, logic_name=logic_name)
        converted_bytes = bytearray(position_bytes)
        for vertex_index in range(vertex_count):
            src_start = vertex_index * position_stride + offset
            src_end = src_start + byte_width
            values = numpy.frombuffer(position_bytes[src_start:src_end], dtype=dtype, count=component_count).copy()
            values[:3] = converted_coords[vertex_index].astype(dtype)
            converted_bytes[src_start:src_end] = values.astype(dtype, copy=False).tobytes()

        return bytes(converted_bytes)

    @staticmethod
    def _loop_vertex_indices(mesh: bpy.types.Mesh, loop_indices: Optional[numpy.ndarray] = None) -> numpy.ndarray:
        all_loop_vertex_indices = numpy.empty(len(mesh.loops), dtype=int)
        mesh.loops.foreach_get("vertex_index", all_loop_vertex_indices)
        if loop_indices is None:
            return all_loop_vertex_indices
        return all_loop_vertex_indices[loop_indices]

    @staticmethod
    def _shape_key_position_data(
        obj: bpy.types.Object,
        mesh: bpy.types.Mesh,
        d3d11_game_type: D3D11GameType,
        key_block=None,
        loop_indices: Optional[numpy.ndarray] = None,
    ) -> Optional[numpy.ndarray]:
        position_element = ExportUtils._get_position_element(d3d11_game_type)
        if position_element is None:
            return None

        key_block = key_block or ExportUtils._get_basis_key_block(obj)
        coords = ExportUtils._get_key_block_coords(key_block)
        if coords is None:
            return None

        loop_vertex_indices = ExportUtils._loop_vertex_indices(mesh, loop_indices)
        if loop_vertex_indices.size and int(loop_vertex_indices.max()) >= len(coords):
            raise ValueError(
                f"ShapeKey Position vertex index out of range: max={int(loop_vertex_indices.max())}, "
                f"coords={len(coords)}, obj={getattr(obj, 'name', '')}"
            )

        return ObjBufferHelper._parse_position_from_vertex_coords(
            coords,
            loop_vertex_indices,
            position_element,
        )

    @staticmethod
    def _override_position_with_shape_key(
        element_vertex_ndarray: numpy.ndarray,
        obj: bpy.types.Object,
        mesh: bpy.types.Mesh,
        d3d11_game_type: D3D11GameType,
        key_block=None,
        loop_indices: Optional[numpy.ndarray] = None,
    ) -> bool:
        if element_vertex_ndarray is None or "POSITION" not in element_vertex_ndarray.dtype.names:
            return False

        position_data = ExportUtils._shape_key_position_data(
            obj=obj,
            mesh=mesh,
            d3d11_game_type=d3d11_game_type,
            key_block=key_block,
            loop_indices=loop_indices,
        )
        if position_data is None:
            return False

        element_vertex_ndarray["POSITION"] = position_data
        return True

    @staticmethod
    def _create_baked_shape_key_sample_mesh(obj: bpy.types.Object, shapekey_name: str):
        # 这里单独烘焙一个只保留目标 ShapeKey 的临时网格，避免直接改动导出对象本身的状态。
        sample_obj = obj.copy()
        sample_obj.name = f"{obj.name}_shapekey_sample"
        if obj.data:
            sample_obj.data = obj.data.copy()
        bpy.context.scene.collection.objects.link(sample_obj)

        original_active = bpy.context.view_layer.objects.active
        try:
            key_blocks = getattr(getattr(sample_obj.data, "shape_keys", None), "key_blocks", None)
            if not key_blocks:
                return sample_obj, None

            sample_key_block = key_blocks.get(shapekey_name)
            if sample_key_block is None:
                return sample_obj, None

            for idx, key_block in enumerate(key_blocks):
                if idx == 0:
                    continue
                key_block.value = 0.0
            sample_key_block.value = 1.0
            bpy.context.view_layer.update()

            ShapeKeyUtils.remove_shape_keys(sample_obj, all=True, apply_mix=True)
            bpy.context.view_layer.update()

            return sample_obj, sample_obj.data
        except Exception:
            sample_mesh = sample_obj.data
            if bpy.data.objects.get(sample_obj.name) is not None:
                bpy.data.objects.remove(sample_obj, do_unlink=True)
            if sample_mesh and sample_mesh.users == 0:
                bpy.data.meshes.remove(sample_mesh)
            raise
        finally:
            try:
                sample_obj.select_set(False)
            except Exception:
                pass
            if original_active is not None:
                bpy.context.view_layer.objects.active = original_active

    @staticmethod
    def _cleanup_shape_key_sample_object(sample_obj: Optional[bpy.types.Object]):
        if sample_obj is None:
            return
        sample_mesh = sample_obj.data
        bpy.data.objects.remove(sample_obj, do_unlink=True)
        if sample_mesh and sample_mesh.users == 0:
            bpy.data.meshes.remove(sample_mesh)

    @staticmethod
    def build_unity_obj_buffer_result(
        obj: bpy.types.Object,
        d3d11_game_type: D3D11GameType,
    ) -> UnityBufferBuildResult:
        ObjBufferHelper.check_and_verify_attributes(obj=obj, d3d11_game_type=d3d11_game_type)

        TimerUtils.start_stage("Element数据解析")
        element_context = ExportUtils.build_obj_element_context(
            d3d11_game_type=d3d11_game_type,
            obj=obj,
        )
        TimerUtils.end_stage("Element数据解析")

        TimerUtils.start_stage("Element数据打包")
        element_vertex_ndarray = ExportUtils.pack_element_vertex_ndarray(
            d3d11_game_type=d3d11_game_type,
            mesh=element_context.mesh,
            original_elementname_data_dict=element_context.original_elementname_data_dict,
            final_elementname_data_dict=element_context.final_elementname_data_dict,
        )
        # 直出基态如果要求保留当前 ShapeKey 混合值，就在真正打包前直接覆盖 POSITION 数据。
        if BlueprintExportHelper.should_preserve_current_shapekey_mix_for_export():
            ExportUtils._override_position_with_shape_key(
                element_vertex_ndarray=element_vertex_ndarray,
                obj=obj,
                mesh=element_context.mesh,
                d3d11_game_type=d3d11_game_type,
            )
        TimerUtils.end_stage("Element数据打包")

        TimerUtils.start_stage("索引去重与Buffer构建")
        ib, category_buffer_dict, index_loop_id_dict, unique_first_loop_indices = ExportUtils.build_unity_index_buffers(
            obj=obj,
            mesh=element_context.mesh,
            element_vertex_ndarray=element_vertex_ndarray,
            d3d11_game_type=d3d11_game_type,
            dtype=element_context.total_structured_dtype,
        )
        TimerUtils.end_stage("索引去重与Buffer构建")

        TimerUtils.start_stage("ShapeKey计算")
        shape_key_buffer_dict = {}
        if not BlueprintExportHelper.should_suppress_shapekey_resource_export():
            shape_key_buffer_dict = ExportUtils.build_unity_shape_key_buffer_dict(
                obj=obj,
                d3d11_game_type=d3d11_game_type,
                index_loop_id_dict=index_loop_id_dict,
                element_vertex_ndarray=element_vertex_ndarray,
            )
        TimerUtils.end_stage("ShapeKey计算")

        return UnityBufferBuildResult(
            obj_name=element_context.obj_name,
            dtype=element_context.total_structured_dtype,
            element_vertex_ndarray=element_vertex_ndarray,
            ib=ib,
            category_buffer_dict=category_buffer_dict,
            index_loop_id_dict=index_loop_id_dict,
            unique_first_loop_indices=unique_first_loop_indices,
            shape_key_buffer_dict=shape_key_buffer_dict,
        )

    @staticmethod
    def _deduplicate_ignore_vertex_id() -> bool:
        """读取「缝合标记特殊去重」开关，缺省关闭。"""
        getter = getattr(GlobalProterties, "deduplicate_ignore_vertex_id", None)
        if callable(getter):
            try:
                return bool(getter())
            except Exception:
                return False
        if getter is None:
            return False
        return bool(getter)

    @staticmethod
    def _apply_current_pose_shading(obj, rows, d3d11_game_type):
        attributes = getattr(getattr(obj, "data", None), "attributes", None)
        if attributes is None or "_ssmt_pose_normal" not in attributes:
            return rows

        from .current_pose_shading import PoseLoops

        loops = PoseLoops(obj, GlobalConfig.logic_name)
        if len(loops.normals) != len(rows):
            raise RuntimeError(f"以当前状态计算法线、切线：{obj.name} 的导出面角数发生变化")
        elements = d3d11_game_type.ElementNameD3D11ElementDict
        encoded = "ENCODEDDATA" in elements and GlobalConfig.logic_name == LogicName.EFMI
        for name in rows.dtype.names:
            if encoded:
                if name != "ENCODEDDATA":
                    continue
                parser = ObjBufferHelper._parse_encodeddata
            elif name == "NORMAL":
                parser = ObjBufferHelper._parse_normal
            elif name == "TANGENT":
                parser = ObjBufferHelper._parse_tangent
            elif name.startswith("BINORMAL"):
                parser = ObjBufferHelper._parse_binormal
            else:
                continue
            rows[name] = parser(loops, len(rows), elements[name]).reshape(rows[name].shape)
        return rows

    @staticmethod
    def recalculate_element_vertex_ndarray(
        obj: bpy.types.Object,
        element_vertex_ndarray: numpy.ndarray,
        d3d11_game_type: D3D11GameType,
        dtype: numpy.dtype,
    ):
        """在去重之前，逐 loop 行重算属性（轮廓线 TANGENT / HI3 2.0 的 COLOR）。

        这里的行就是 loop 数组、本来就按三角形顺序排列，所以喂恒等 IB（arange(n)）：
        算子按三角形遍历求角点权重、再按位置分组累加，恒等 IB 让"每个角点各自一行"与
        重算之后的行一一对应，两个算子内部一行都不用改。

        重算发生在去重**之前**，于是去重键比较的就是重算之后的值——被合并的行在重算
        口径下本来就相等，"先收窄再重算会改变均值/切线"这件事从顺序上消失，面板勾选
        也不需要为它额外增加任何类型。

        两个算子都有 early-return 分支（开关关着、元素类型缺列、勾了标准切线基准），
        那种情况原样返回入参；返回结果统一规整成同 dtype、同长度、可写的数组，对不上
        就原样退回入参，不抛。
        """
        if element_vertex_ndarray is None or len(element_vertex_ndarray) == 0:
            return element_vertex_ndarray

        recalculated = ObjBufferHelper.average_normal_tangent_xxmi(
            obj=obj,
            indexed_vertices=element_vertex_ndarray,
            flattened_ib=numpy.arange(len(element_vertex_ndarray), dtype=numpy.int32),
            d3d11GameType=d3d11_game_type,
            dtype=dtype,
        )
        recalculated = ObjBufferHelper.average_normal_color(
            obj=obj,
            indexed_vertices=recalculated,
            d3d11GameType=d3d11_game_type,
            dtype=dtype,
        )
        recalculated = ExportUtils._coerce_recalculated_element_vertex_ndarray(
            recalculated=recalculated,
            fallback=element_vertex_ndarray,
        )
        return ExportUtils._apply_current_pose_shading(obj, recalculated, d3d11_game_type)

    @staticmethod
    def _coerce_recalculated_element_vertex_ndarray(recalculated, fallback):
        """把重算结果规整成同 dtype、同长度、可写的结构化数组；对不上就原样退回 fallback。"""
        if recalculated is fallback:
            return fallback
        if not isinstance(recalculated, numpy.ndarray):
            # 有分支返回 bytes 列表（每行一条）。
            try:
                recalculated = numpy.frombuffer(
                    b"".join(bytes(row) for row in recalculated),
                    dtype=fallback.dtype,
                )
            except Exception:
                return fallback
        if recalculated.dtype != fallback.dtype or len(recalculated) != len(fallback):
            return fallback
        return numpy.array(recalculated, dtype=fallback.dtype, copy=True)

    @staticmethod
    def _deduplicate_element_set_is_panel_default():
        """读「顶点去重精度控制」面板是否仍是默认（8 个类型全勾）。读不到就当非默认。"""
        checker = getattr(GlobalProterties, "deduplicate_element_set_is_default", None)
        if not callable(checker):
            return False
        try:
            return bool(checker())
        except Exception:
            return False

    @staticmethod
    def _deduplicate_element_set_for_export():
        """这次导出交给去重的元素类型。

        面板没动过（8 个类型全勾）⇒ None：走历史的全字段去重键，导出逐字节不变。
        面板被显式改过 ⇒ 就是勾选集本身，不补任何东西（重算已经在去重之前做完了）。
        """
        if ExportUtils._deduplicate_element_set_is_panel_default():
            return None
        return GlobalProterties.get_deduplicate_element_set()

    @staticmethod
    def build_unity_index_buffers(
        obj: bpy.types.Object,
        mesh: bpy.types.Mesh,
        element_vertex_ndarray: numpy.ndarray,
        d3d11_game_type: D3D11GameType,
        dtype: numpy.dtype,
    ):
        if (
            GlobalConfig.logic_name == LogicName.GF2
            and "TANGENT" in d3d11_game_type.OrderedFullElementList
        ):
            return ObjBufferHelper.calc_index_vertex_buffer_girlsfrontline2(
                mesh=mesh,
                element_vertex_ndarray=ExportUtils._apply_current_pose_shading(
                    obj, element_vertex_ndarray, d3d11_game_type
                ),
                d3d11_game_type=d3d11_game_type,
                dtype=dtype,
            )

        # 属性重算（average_normal_tangent_xxmi / average_normal_color）跑在去重**之前**：
        # 先按 loop 行把 TANGENT / COLOR 算出来，再交给下面的 calc_index_vertex_buffer_wwmi_v2
        # 去重。于是去重键比较的就是重算之后的值，算子也永远拿得到完整的角点数据——原来那条
        # "重算开着就退回全字段去重"的门控因此不存在了，面板勾选直接生效（见
        # _deduplicate_element_set_for_export）。GF2 的 TANGENT 分支在上面提前返回、走它
        # 自己的老路径，不受这里的顺序影响。
        element_vertex_ndarray = ExportUtils.recalculate_element_vertex_ndarray(
            obj=obj,
            element_vertex_ndarray=element_vertex_ndarray,
            d3d11_game_type=d3d11_game_type,
            dtype=dtype,
        )
        deduplicate_element_set = ExportUtils._deduplicate_element_set_for_export()

        ib, _, _, unique_element_vertex_ndarray, unique_first_loop_indices = \
            ObjBufferHelper.calc_index_vertex_buffer_wwmi_v2(
                mesh=mesh,
                element_vertex_ndarray=element_vertex_ndarray,
                dtype=dtype,
                d3d11_game_type=d3d11_game_type,
                deduplicate_element_set=deduplicate_element_set,
                deduplicate_ignore_vertex_id=ExportUtils._deduplicate_ignore_vertex_id(),
                flip_triangles=False,
            )

        if GlobalConfig.logic_name == LogicName.ZZMI:
            unique_element_vertex_ndarray = (
                ObjBufferHelper.stabilize_zzmi_rebuilt_tangent_w(
                    unique_element_vertex_ndarray,
                    ib,
                )
            )

        index_loop_id_dict = dict(enumerate(unique_first_loop_indices.tolist()))

        row_size = unique_element_vertex_ndarray.dtype.itemsize
        post_processed_bytes = numpy.ascontiguousarray(unique_element_vertex_ndarray).view(
            numpy.uint8
        ).reshape(len(unique_element_vertex_ndarray), row_size)

        category_stride_dict = d3d11_game_type.get_real_category_stride_dict()
        category_buffer_dict = {}
        stride_offset = 0
        for cname, cstride in category_stride_dict.items():
            category_buffer_dict[cname] = post_processed_bytes[:, stride_offset:stride_offset + cstride].flatten()
            stride_offset += cstride

        if GlobalConfig.logic_name == LogicName.YYSLS or GlobalConfig.logic_name == LogicName.SnowBreak:
            flat_arr = numpy.asarray(ib, dtype=numpy.int32)
            if flat_arr.size % 3 == 0:
                ib = flat_arr.reshape(-1, 3)[:, ::-1].flatten().tolist()

        return ib, category_buffer_dict, index_loop_id_dict, unique_first_loop_indices

    @staticmethod
    def _frame_table_sample_signatures(obj, shape_groups, indices_map, element_vertex_ndarray, sample_version):
        import hashlib
        import json
        from ..blueprint.frame_table_cache import hash_array, hash_mesh_topology
        from ..common.shapekey_native import read_coords

        mesh = obj.data
        digest = hashlib.sha256()
        digest.update(json.dumps([sample_version, bpy.app.version_string, GlobalConfig.logic_name]).encode())
        digest.update(str(element_vertex_ndarray.dtype).encode("utf-8"))
        hash_array(digest, indices_map)
        if not hash_mesh_topology(digest, mesh):
            return None, []
        reference = mesh.shape_keys.reference_key
        hash_array(digest, read_coords(reference))
        key_digests = {}
        signatures = []
        for group in shape_groups:
            group_digest = digest.copy()
            group_digest.update(json.dumps([(key.name, key.relative_key.name) for key in group]).encode())
            for key in group:
                for block in (key, key.relative_key):
                    if block not in key_digests:
                        coordinates = hashlib.sha256()
                        hash_array(coordinates, read_coords(block))
                        key_digests[block] = coordinates.digest()
                    group_digest.update(key_digests[block])
            signatures.append(group_digest.hexdigest())
        return hashlib.sha256("".join(signatures).encode("ascii")).hexdigest(), signatures

    @staticmethod
    def _write_pose_group_cache(cache, mesh_name, ordinal, signature, poses):
        try:
            cache.store_pose_group(mesh_name, ordinal, signature, poses)
        except OSError as exc:
            print(f"形态键组缓存写入失败（{mesh_name}）: {exc}", flush=True)

    @staticmethod
    def _store_pose_group_results(cache, mesh_name, pending_groups, frames, base, group_results):
        start = 0
        for ordinal, signature, group in pending_groups:
            stop = start + len(group)
            poses = frames[start:stop]
            ExportUtils._write_pose_group_cache(cache, mesh_name, ordinal, signature, poses)
            group_results[ordinal] = ExportUtils._build_parallel_shape_buffers(group, base, poses)
            start = stop
        return {name: result for ordinal in sorted(group_results)
                for name, result in group_results[ordinal].items()}

    @staticmethod
    def _collect_parallel_pose_results(
        cache, mesh_name, pending_groups, samples, shared_frames, completed, base, group_results,
    ):
        from itertools import chain, islice

        frames = chain.from_iterable(completed)
        if not pending_groups:
            return ExportUtils._build_parallel_shape_buffers(samples, base, frames)

        from concurrent.futures import ThreadPoolExecutor

        start = 0
        with ThreadPoolExecutor(max_workers=1) as writer:
            writes = []
            for ordinal, signature, group in pending_groups:
                stop = start + len(group)
                group_results[ordinal] = ExportUtils._build_parallel_shape_buffers(
                    group, base, islice(frames, len(group)),
                )
                # The group has been fully published. The worker will only
                # write to later blocks while the read-only cache write runs.
                writes.append(writer.submit(
                    ExportUtils._write_pose_group_cache,
                    cache, mesh_name, ordinal, signature, shared_frames[start:stop],
                ))
                start = stop
            for write in writes:
                write.result()
        return {name: result for ordinal in sorted(group_results)
                for name, result in group_results[ordinal].items()}

    @staticmethod
    def build_unity_shape_key_buffer_dict(
        obj: bpy.types.Object,
        d3d11_game_type: D3D11GameType,
        index_loop_id_dict: Optional[dict],
        element_vertex_ndarray: numpy.ndarray,
    ) -> dict:
        shape_key_buffer_dict = {}

        use_evaluated_samples = BlueprintExportHelper.should_preserve_current_shapekey_mix_for_export()
        shapekey_names = BlueprintExportHelper.get_runtime_shapekey_buffer_names(obj.name)
        if not use_evaluated_samples:
            # Other exporters can also request keys outside the direct export classification.
            shapekey_names = list(dict.fromkeys(
                list(BlueprintExportHelper.get_current_shapekeyname_mkey_dict()) + shapekey_names
            ))
        if not shapekey_names:
            return shape_key_buffer_dict

        if not obj.data.shape_keys or not obj.data.shape_keys.key_blocks:
            return shape_key_buffer_dict

        # Match exact shape key names; Blender labels can be numeric, default names,
        # Chinese/Japanese text, or any other user-defined string.
        # 这里必须按精确名称匹配，默认名、数字名或中文名都不能被二次规范化。
        shape_keys = [
            sk for sk in obj.data.shape_keys.key_blocks if sk.name in shapekey_names
        ]
        if not shape_keys:
            return shape_key_buffer_dict

        shape_groups = []
        grouped_keys = set()
        if use_evaluated_samples:
            keys_by_name = {key.name: key for key in shape_keys}
            for sequence in BlueprintExportHelper.runtime_shapekey_sequences:
                group = [keys_by_name[name] for name in sequence if name in keys_by_name]
                if group:
                    shape_groups.append(group)
                    grouped_keys.update(key.name for key in group)
        shape_groups.extend([key] for key in shape_keys if key.name not in grouped_keys)
        sample_groups = [
            [(key, index == 0, len(group) > 1) for index, key in enumerate(group)]
            for group in shape_groups
        ]
        samples = [sample for group in sample_groups for sample in group]
        indices_map = (
            numpy.fromiter(index_loop_id_dict.values(), dtype=int, count=len(index_loop_id_dict))
            if index_loop_id_dict is not None else numpy.arange(len(obj.data.vertices), dtype=int)
        )
        fast_coordinates = False
        pose_layout = False
        pending_groups = []
        group_results = {}
        if use_evaluated_samples:
            from ..common.shapekey_native import capture_relative_shapes, fast_path_reason

            fast_coordinates = (
                not fast_path_reason(obj)
                and all(key.slider_min <= 0.0 <= key.slider_max for key in obj.data.shape_keys.key_blocks)
                and all(key.slider_max >= 1.0 for key in shape_keys)
            )
            pose_layout = (
                fast_coordinates
                and BlueprintExportHelper.runtime_shapekey_sequences
                and LogicName.is_zzmi_family(GlobalConfig.logic_name)
                and [
                    (element.ElementName, element.Format)
                    for element in d3d11_game_type.D3D11ElementList
                    if element.Category == "Position"
                ] == [
                    ("POSITION", "R32G32B32_FLOAT"),
                    ("NORMAL", "R32G32B32_FLOAT"),
                    ("TANGENT", "R32G32B32A32_FLOAT"),
                ]
            )
            cache = BlueprintExportHelper.runtime_frame_table_cache
            if cache is not None:
                # Evaluated modifiers and drivers require
                # Blender state beyond the deterministic coordinate sampler.
                signature, group_signatures = (
                    ExportUtils._frame_table_sample_signatures(
                        obj, shape_groups, indices_map, element_vertex_ndarray, cache.sample_version,
                    ) if fast_coordinates else (None, [])
                )
                if cache.record_sample(signature):
                    return shape_key_buffer_dict
                if pose_layout and group_signatures:
                    base = element_vertex_ndarray[indices_map]
                    for ordinal, (group, group_signature) in enumerate(zip(sample_groups, group_signatures)):
                        poses = cache.load_pose_group(obj.name, ordinal, group_signature, (len(group), len(indices_map), 10))
                        if poses is None:
                            pending_groups.append((ordinal, group_signature, group))
                        else:
                            group_results[ordinal] = ExportUtils._build_parallel_shape_buffers(group, base, poses)
                    del poses
                    samples = [sample for _, _, group in pending_groups for sample in group]
                    if group_results:
                        print(
                            f"形态键组缓存命中：{obj.name} 复用 {len(group_results)} 组，重新采样 {len(samples)} 帧",
                            flush=True,
                        )
                    if not samples:
                        return ExportUtils._store_pose_group_results(
                            cache, obj.name, pending_groups, (), base, group_results,
                        )

        stage = f"Processing {len(samples)} ShapeKeys for {obj.name}"
        TimerUtils.Start(stage)
        sample_mesh = None
        try:
            with ShapeKeyUtils.preserve_shape_key_state(obj):
                ShapeKeyUtils.reset_shapekey_values(obj)
                ShapeKeyUtils._update_view_layer()

                # Start each sample with the unchanged base attributes.
                base_shape_vertex_ndarray = element_vertex_ndarray[indices_map]
                depsgraph = bpy.context.evaluated_depsgraph_get() if use_evaluated_samples else None
                sampled_coords = None
                pose_frames = None
                if fast_coordinates:
                    ordered_keys = [key for key, _, _ in samples]
                    basis_coords, sampled_coords = capture_relative_shapes(obj, ordered_keys)
                    # Keep Blender's normal and tangent calculation on an unlinked mesh.
                    sample_mesh = bpy.data.meshes.new_from_object(
                        obj.evaluated_get(depsgraph),
                        preserve_all_data_layers=True,
                        depsgraph=depsgraph,
                    )
                    if pose_layout and len(samples) >= 120:
                        from .shapekey_pose_worker import pose_worker_count, sample_parallel

                        workers = pose_worker_count(sample_mesh, len(samples), len(indices_map))
                        if workers:
                            from ..blueprint import export_progress

                            flags = [(first, sequential) for _, first, sequential in samples]
                            print(f"形态键并行采样：{workers} 个 Blender 进程，{len(samples)} 帧", flush=True)
                            export_progress.report_stage(f"ShapeKey计算 | {workers} 个进程采样 {len(samples)} 帧")
                            with sample_parallel(sample_mesh, indices_map, basis_coords, sampled_coords, flags, workers) as (shared, completed):
                                return ExportUtils._collect_parallel_pose_results(
                                    cache, obj.name, pending_groups, samples, shared, completed,
                                    base_shape_vertex_ndarray, group_results,
                                )
                    if pending_groups:
                        pose_frames = numpy.empty((len(samples), len(indices_map), 10), dtype=numpy.float32)
                active_keys = []
                previous_attributes = {}

                for frame_index, (shapekey, first_in_group, sequential) in enumerate(samples):
                    shapekey_name = shapekey.name
                    sample_obj = None
                    evaluated_obj = None
                    try:
                        if first_in_group:
                            previous_attributes.clear()
                            for active_key in active_keys:
                                active_key.value = 0.0
                            active_keys.clear()
                        if sampled_coords is not None:
                            _, coords = next(sampled_coords)
                            if sequential:
                                if first_in_group:
                                    sequence_coords = basis_coords.copy()
                                sequence_coords += coords - basis_coords
                                posed_coords = sequence_coords
                            else:
                                posed_coords = coords
                            sample_mesh.vertices.foreach_set("co", posed_coords.ravel())
                            sample_mesh.update()
                            mesh_eval = sample_mesh
                        elif use_evaluated_samples:
                            # Sequence weights are cumulative. Group boundaries
                            # reset the temporary export object to its basis.
                            shapekey.value = 1.0
                            active_keys.append(shapekey)
                            ShapeKeyUtils._update_view_layer()
                            evaluated_obj = obj.evaluated_get(depsgraph)
                            mesh_eval = evaluated_obj.to_mesh()
                        else:
                            sample_obj, mesh_eval = ExportUtils._create_baked_shape_key_sample_mesh(
                                obj=obj,
                                shapekey_name=shapekey_name,
                            )
                        if mesh_eval is None:
                            continue

                        if len(mesh_eval.polygons) > 0:
                            uv_map_name = "TEXCOORD.xy" if "TEXCOORD.xy" in mesh_eval.uv_layers else None
                            try:
                                mesh_eval.calc_tangents(uvmap=uv_map_name)
                            except RuntimeError:
                                ObjUtils.mesh_triangulate(mesh_eval)
                                mesh_eval.calc_tangents(uvmap=uv_map_name)

                        result = ExportUtils.build_shape_key_buffer_result(
                            name=shapekey_name,
                            base_element_vertex_ndarray=base_shape_vertex_ndarray,
                            mesh=mesh_eval,
                            indices_map=indices_map,
                            d3d11_game_type=d3d11_game_type,
                            position_coords=coords if sampled_coords is not None else None,
                        )
                        if pose_frames is not None:
                            frame = pose_frames[frame_index]
                            frame[:, :3] = result.element_vertex_ndarray["POSITION"]
                            frame[:, 3:6] = result.element_vertex_ndarray["NORMAL"]
                            frame[:, 6:] = result.element_vertex_ndarray["TANGENT"]
                            continue
                        if sequential:
                            sampled_attributes = result.element_vertex_ndarray
                            # The slot merger sums float deltas. Encode the
                            # difference between two adjacent posed frames so
                            # their sum reconstructs the current tangent frame.
                            for field in ("POSITION", "NORMAL", "TANGENT", "BINORMAL"):
                                if field == "POSITION" and sampled_coords is not None:
                                    continue
                                if field not in sampled_attributes.dtype.names:
                                    continue
                                value = sampled_attributes[field]
                                if value.dtype.kind != "f":
                                    continue
                                basis = base_shape_vertex_ndarray[field]
                                current = value.copy()
                                value[:] = basis + (current - previous_attributes.get(field, basis))
                                previous_attributes[field] = current
                        shape_key_buffer_dict[shapekey_name] = result
                    finally:
                        if evaluated_obj is not None:
                            evaluated_obj.to_mesh_clear()
                        ExportUtils._cleanup_shape_key_sample_object(sample_obj)
                if pose_frames is not None:
                    return ExportUtils._store_pose_group_results(
                        cache, obj.name, pending_groups, pose_frames, base_shape_vertex_ndarray, group_results,
                    )
        finally:
            if sample_mesh is not None:
                bpy.data.meshes.remove(sample_mesh)
            TimerUtils.End(stage)

        return shape_key_buffer_dict

    @staticmethod
    def _build_parallel_shape_buffers(samples, base, frames):
        # The other vertex categories are invariant across poses. Keep only the
        # Position resource bytes (position, normal and tangent) for this layout.
        base_surface = numpy.concatenate((base["NORMAL"], base["TANGENT"]), axis=1)
        result = {}
        previous = base_surface
        for (key, first, sequential), frame in zip(samples, frames):
            packed = frame.copy()
            if sequential:
                current = frame[:, 3:]
                previous_frame = base_surface if first else previous
                packed[:, 3:] = base_surface + (current - previous_frame)
                previous = current
            result[key.name] = ShapeKeyBufferBuildResult(
                name=key.name,
                position_bytes=packed.view(numpy.uint8).reshape(-1),
            )
        return result

    @staticmethod
    def build_shape_key_buffer_result(
        name: str,
        base_element_vertex_ndarray: numpy.ndarray,
        indices_map: numpy.ndarray,
        d3d11_game_type: D3D11GameType,
        mesh: Optional[bpy.types.Mesh] = None,
        position_coords: Optional[numpy.ndarray] = None,
    ) -> ShapeKeyBufferBuildResult:
        element_vertex_ndarray = base_element_vertex_ndarray.copy()

        if mesh is None:
            return ShapeKeyBufferBuildResult(
                name=name,
                element_vertex_ndarray=element_vertex_ndarray,
            )

        loop_indices = indices_map
        loop_vertex_indices = None
        target_category = "Position"

        for d3d11_element in d3d11_game_type.D3D11ElementList:
            if d3d11_element.Category != target_category:
                continue

            elem_name = d3d11_element.ElementName
            data = None

            if elem_name == "POSITION":
                if loop_vertex_indices is None:
                    all_loop_vertex_indices = numpy.empty(len(mesh.loops), dtype=int)
                    mesh.loops.foreach_get("vertex_index", all_loop_vertex_indices)
                    loop_vertex_indices = all_loop_vertex_indices[loop_indices]

                if position_coords is not None:
                    data = ObjBufferHelper._parse_position_from_vertex_coords(
                        position_coords,
                        loop_vertex_indices,
                        d3d11_element,
                    )
                else:
                    data = ObjBufferHelper._parse_position(
                        mesh_vertices=mesh.vertices,
                        mesh_vertices_length=len(mesh.vertices),
                        loop_vertex_indices=loop_vertex_indices,
                        d3d11_element=d3d11_element,
                    )

            elif elem_name == "NORMAL":
                corners = getattr(mesh, "corner_normals", None)
                if d3d11_element.Format == "R32G32B32_FLOAT" and corners is not None and len(corners) == len(mesh.loops):
                    all_normals = numpy.empty((len(corners), 3), dtype=numpy.float32)
                    corners.foreach_get("vector", all_normals.ravel())
                else:
                    all_normals = ObjBufferHelper._parse_normal(
                        mesh_loops=mesh.loops,
                        mesh_loops_length=len(mesh.loops),
                        d3d11_element=d3d11_element,
                    mesh=mesh,
                    )
                data = all_normals[loop_indices]

            elif elem_name == "TANGENT":
                all_tangents = ObjBufferHelper._parse_tangent(
                    mesh_loops=mesh.loops,
                    mesh_loops_length=len(mesh.loops),
                    d3d11_element=d3d11_element,
                    mesh=mesh,
                )
                data = all_tangents[loop_indices]

            elif elem_name.startswith("BINORMAL"):
                all_binormals = ObjBufferHelper._parse_binormal(
                    mesh_loops=mesh.loops,
                    mesh_loops_length=len(mesh.loops),
                    d3d11_element=d3d11_element,
                )
                data = all_binormals[loop_indices]

            if data is not None:
                element_vertex_ndarray[elem_name] = data

        return ShapeKeyBufferBuildResult(
            name=name,
            element_vertex_ndarray=element_vertex_ndarray,
        )

    @staticmethod
    def build_wwmi_index_buffers(
        mesh: bpy.types.Mesh,
        element_vertex_ndarray: numpy.ndarray,
        dtype: numpy.dtype,
        d3d11_game_type: D3D11GameType,
        deduplicate_element_set: set = None,
        deduplicate_ignore_vertex_id: bool = True,
    ):
        return ObjBufferHelper.calc_index_vertex_buffer_wwmi_v2(
            mesh=mesh,
            element_vertex_ndarray=element_vertex_ndarray,
            dtype=dtype,
            d3d11_game_type=d3d11_game_type,
            deduplicate_element_set=deduplicate_element_set,
            deduplicate_ignore_vertex_id=deduplicate_ignore_vertex_id,
        )

    @staticmethod
    def build_wwmi_shapekey_payload(obj: bpy.types.Object, index_vertex_id_dict: dict):
        if not ShapeKeyUtils.has_exportable_shape_keys(obj):
            return [], [], [], False

        shapekey_offsets, shapekey_vertex_ids, shapekey_vertex_offsets = ShapeKeyUtils.extract_shapekey_data(
            merged_obj=obj,
            index_vertex_id_dict=index_vertex_id_dict,
        )
        return shapekey_offsets, shapekey_vertex_ids, shapekey_vertex_offsets, True

    @staticmethod
    def build_wwmi_obj_buffer_result(element_context: ObjElementContext) -> WWMIBufferBuildResult:
        element_vertex_ndarray = element_context.element_vertex_ndarray
        if element_vertex_ndarray is None:
            element_vertex_ndarray = ExportUtils.pack_element_vertex_ndarray(
                d3d11_game_type=element_context.d3d11_game_type,
                mesh=element_context.mesh,
                original_elementname_data_dict=element_context.original_elementname_data_dict,
                final_elementname_data_dict=element_context.final_elementname_data_dict,
            )
        if BlueprintExportHelper.should_preserve_current_shapekey_mix_for_export():
            ExportUtils._override_position_with_shape_key(
                element_vertex_ndarray=element_vertex_ndarray,
                obj=element_context.obj,
                mesh=element_context.mesh,
                d3d11_game_type=element_context.d3d11_game_type,
            )

        # 与 unity 路径同序：先按 loop 行重算属性，再按面板勾选去重。这样
        # build_wwmi_shapekey_payload 拿到的 index_vertex_id_dict 对应的还是同一套
        # "重算后的行"，形态键 payload 的取数口径不变。
        element_vertex_ndarray = ExportUtils.recalculate_element_vertex_ndarray(
            obj=element_context.obj,
            element_vertex_ndarray=element_vertex_ndarray,
            d3d11_game_type=element_context.d3d11_game_type,
            dtype=element_context.total_structured_dtype,
        )
        deduplicate_element_set = ExportUtils._deduplicate_element_set_for_export()

        ib, category_buffer_dict, index_vertex_id_dict, unique_element_vertex_ndarray, unique_first_loop_indices = ExportUtils.build_wwmi_index_buffers(
            mesh=element_context.mesh,
            element_vertex_ndarray=element_vertex_ndarray,
            dtype=element_context.total_structured_dtype,
            d3d11_game_type=element_context.d3d11_game_type,
            deduplicate_element_set=deduplicate_element_set,
            deduplicate_ignore_vertex_id=ExportUtils._deduplicate_ignore_vertex_id(),
        )
        if BlueprintExportHelper.should_suppress_shapekey_resource_export():
            shapekey_offsets, shapekey_vertex_ids, shapekey_vertex_offsets, export_shapekey = [], [], [], False
        else:
            shapekey_offsets, shapekey_vertex_ids, shapekey_vertex_offsets, export_shapekey = ExportUtils.build_wwmi_shapekey_payload(
                obj=element_context.obj,
                index_vertex_id_dict=index_vertex_id_dict,
            )

        return WWMIBufferBuildResult(
            obj=element_context.obj,
            mesh=element_context.mesh,
            d3d11_game_type=element_context.d3d11_game_type,
            obj_name=element_context.obj_name,
            dtype=element_context.total_structured_dtype,
            element_vertex_ndarray=element_vertex_ndarray,
            ib=ib,
            category_buffer_dict=category_buffer_dict,
            index_vertex_id_dict=index_vertex_id_dict,
            shapekey_offsets=shapekey_offsets,
            shapekey_vertex_ids=shapekey_vertex_ids,
            shapekey_vertex_offsets=shapekey_vertex_offsets,
            export_shapekey=export_shapekey,
            unique_element_vertex_ndarray=unique_element_vertex_ndarray,
            unique_first_loop_indices=unique_first_loop_indices,
        )
