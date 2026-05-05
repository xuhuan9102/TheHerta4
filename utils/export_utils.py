import bpy
import numpy

from dataclasses import dataclass, field
from typing import Optional

from ..common.global_config import GlobalConfig
from ..common.logic_name import LogicName
from ..common.global_properties import GlobalProterties
from .obj_utils import ObjUtils
from .shapekey_utils import ShapeKeyUtils
from .timer_utils import TimerUtils

from ..common.d3d11_gametype import D3D11GameType
from ..blueprint.export_helper import BlueprintExportHelper
from ..common.obj_buffer_helper import ObjBufferHelper


@dataclass
class ObjElementContext:
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
    element_vertex_ndarray: numpy.ndarray = field(repr=False)


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
    @staticmethod
    def build_obj_element_context(
        d3d11_game_type: D3D11GameType,
        obj: Optional[bpy.types.Object] = None,
        obj_name: str = "",
        final_elementname_data_dict: Optional[dict] = None,
    ) -> ObjElementContext:
        resolved_obj = obj or ObjUtils.get_obj_by_name(name=obj_name)

        if not BlueprintExportHelper.should_preserve_current_shapekey_mix_for_export():
            ShapeKeyUtils.reset_shapekey_values(resolved_obj)

        mesh = ObjUtils.get_mesh_evaluate_from_obj(obj=resolved_obj)
        if len(mesh.polygons) > 0:
            uv_map_name = "TEXCOORD.xy" if "TEXCOORD.xy" in mesh.uv_layers else None
            try:
                mesh.calc_tangents(uvmap=uv_map_name)
            except RuntimeError:
                ObjUtils.mesh_triangulate(mesh)
                mesh.calc_tangents(uvmap=uv_map_name)

        original_elementname_data_dict = ObjBufferHelper.parse_elementname_data_dict(
            mesh=mesh,
            d3d11_game_type=d3d11_game_type,
        )

        return ObjElementContext(
            obj=resolved_obj,
            mesh=mesh,
            d3d11_game_type=d3d11_game_type,
            obj_name=resolved_obj.name,
            total_structured_dtype=d3d11_game_type.get_total_structured_dtype(),
            original_elementname_data_dict=original_elementname_data_dict,
            final_elementname_data_dict=final_elementname_data_dict or {},
        )

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

            bpy.ops.object.select_all(action='DESELECT')
            bpy.context.view_layer.objects.active = sample_obj
            sample_obj.select_set(True)
            bpy.ops.object.shape_key_remove(all=True, apply_mix=True)
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
                dtype=element_context.total_structured_dtype,
                index_loop_id_dict=index_loop_id_dict,
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
                element_vertex_ndarray=element_vertex_ndarray,
                d3d11_game_type=d3d11_game_type,
                dtype=dtype,
            )

        deduplicate_element_set = GlobalProterties.get_deduplicate_element_set()

        ib, _, _, unique_element_vertex_ndarray, unique_first_loop_indices = \
            ObjBufferHelper.calc_index_vertex_buffer_wwmi_v2(
                mesh=mesh,
                element_vertex_ndarray=element_vertex_ndarray,
                dtype=dtype,
                d3d11_game_type=d3d11_game_type,
                deduplicate_element_set=deduplicate_element_set,
                flip_triangles=False,
            )

        index_loop_id_dict = dict(enumerate(unique_first_loop_indices.tolist()))

        vertex_data_list = [row.tobytes() for row in unique_element_vertex_ndarray]

        indexed_vertices = ObjBufferHelper.average_normal_tangent_xxmi(
            obj=obj,
            indexed_vertices=vertex_data_list,
            flattened_ib=ib,
            d3d11GameType=d3d11_game_type,
            dtype=dtype,
        )

        indexed_vertices = ObjBufferHelper.average_normal_color(
            obj=obj,
            indexed_vertices=indexed_vertices,
            d3d11GameType=d3d11_game_type,
            dtype=dtype,
        )

        if isinstance(indexed_vertices, numpy.ndarray):
            n_unique = len(indexed_vertices)
            row_size = indexed_vertices.dtype.itemsize
            post_processed_bytes = numpy.ascontiguousarray(indexed_vertices).view(numpy.uint8).reshape(n_unique, row_size)
        else:
            post_processed_bytes = numpy.array([numpy.frombuffer(bd, dtype=numpy.uint8) for bd in indexed_vertices])

        category_stride_dict = d3d11_game_type.get_real_category_stride_dict()
        category_buffer_dict = {}
        stride_offset = 0
        for cname, cstride in category_stride_dict.items():
            category_buffer_dict[cname] = post_processed_bytes[:, stride_offset:stride_offset + cstride].flatten()
            stride_offset += cstride

        if GlobalConfig.logic_name == LogicName.YYSLS:
            flat_arr = numpy.asarray(ib, dtype=numpy.int32)
            if flat_arr.size % 3 == 0:
                ib = flat_arr.reshape(-1, 3)[:, ::-1].flatten().tolist()

        return ib, category_buffer_dict, index_loop_id_dict, unique_first_loop_indices

    @staticmethod
    def build_unity_shape_key_buffer_dict(
        obj: bpy.types.Object,
        d3d11_game_type: D3D11GameType,
        dtype: numpy.dtype,
        index_loop_id_dict: Optional[dict],
    ) -> dict:
        shape_key_buffer_dict = {}

        shapekeyname_mkey_dict = BlueprintExportHelper.get_current_shapekeyname_mkey_dict()
        shapekey_names = list(dict.fromkeys(
            list(shapekeyname_mkey_dict.keys())
            + BlueprintExportHelper.get_runtime_shapekey_buffer_names(getattr(obj, "name", ""))
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

        TimerUtils.Start(f"Processing {len(shape_keys)} ShapeKeys for {obj.name}")
        try:
            if index_loop_id_dict is not None:
                target_count = len(index_loop_id_dict)
                indices_map = numpy.zeros(target_count, dtype=int)
                indices_map[:] = list(index_loop_id_dict.values())
            else:
                target_count = len(obj.data.vertices)
                indices_map = numpy.arange(target_count, dtype=int)

            base_shape_vertex_ndarray = numpy.zeros(target_count, dtype=dtype)

            for shapekey in shape_keys:
                shapekey_name = shapekey.name
                sample_obj = None
                try:
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

                    shape_key_buffer_dict[shapekey_name] = ExportUtils.build_shape_key_buffer_result(
                        name=shapekey_name,
                        base_element_vertex_ndarray=base_shape_vertex_ndarray,
                        mesh=mesh_eval,
                        indices_map=indices_map,
                        d3d11_game_type=d3d11_game_type,
                    )
                finally:
                    ExportUtils._cleanup_shape_key_sample_object(sample_obj)
        finally:
            ShapeKeyUtils.reset_shapekey_values(obj)
            TimerUtils.End(f"Processing {len(shape_keys)} ShapeKeys for {obj.name}")

        return shape_key_buffer_dict

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
                all_normals = ObjBufferHelper._parse_normal(
                    mesh_loops=mesh.loops,
                    mesh_loops_length=len(mesh.loops),
                    d3d11_element=d3d11_element,
                )
                data = all_normals[loop_indices]

            elif elem_name == "TANGENT":
                all_tangents = ObjBufferHelper._parse_tangent(
                    mesh_loops=mesh.loops,
                    mesh_loops_length=len(mesh.loops),
                    d3d11_element=d3d11_element,
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
    ):
        return ObjBufferHelper.calc_index_vertex_buffer_wwmi_v2(
            mesh=mesh,
            element_vertex_ndarray=element_vertex_ndarray,
            dtype=dtype,
            d3d11_game_type=d3d11_game_type,
            deduplicate_element_set=deduplicate_element_set,
        )

    @staticmethod
    def build_wwmi_shapekey_payload(obj: bpy.types.Object, index_vertex_id_dict: dict):
        if obj.data.shape_keys is None or len(getattr(obj.data.shape_keys, "key_blocks", [])) == 0:
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

        deduplicate_element_set = GlobalProterties.get_deduplicate_element_set()

        ib, category_buffer_dict, index_vertex_id_dict, unique_element_vertex_ndarray, unique_first_loop_indices = ExportUtils.build_wwmi_index_buffers(
            mesh=element_context.mesh,
            element_vertex_ndarray=element_vertex_ndarray,
            dtype=element_context.total_structured_dtype,
            d3d11_game_type=element_context.d3d11_game_type,
            deduplicate_element_set=deduplicate_element_set,
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
