import bpy
import itertools
import math
import numpy
import os
from types import SimpleNamespace

from bpy_extras.io_utils import unpack_list, axis_conversion

from ..utils.format_utils import Fatal, FormatUtils
from ..utils.mesh_utils import MeshUtils
from ..utils.obj_utils import ObjUtils
from ..utils.color_attribute_utils import write_color_attribute_data
from ..utils.texture_utils import TextureUtils
from ..utils.timer_utils import TimerUtils
from ..utils.vertexgroup_utils import VertexGroupUtils

from .global_config import GlobalConfig
from .global_properties import GlobalProterties
from .logic_name import LogicName
from .object_prefix_helper import ObjectPrefixHelper
from .d3d11_element import D3D11Element
from ..ui.wwmi.extracted_object import ExtractedObjectHelper


class MeshCreateHelper:
    @staticmethod
    def _get_color_attribute_type_by_format(dxgi_format: str) -> str:
        normalized_format = str(dxgi_format or "").upper()
        if (
            normalized_format.endswith("_FLOAT")
            or normalized_format.endswith("_SNORM")
            or "16" in normalized_format
            or "32" in normalized_format
        ):
            return "FLOAT_COLOR"
        return "BYTE_COLOR"

    @staticmethod
    def _get_mesh_prefix_parts(mesh_name: str) -> dict:
        normalized_mesh_name = str(mesh_name or "").strip()
        prefix_info = ObjectPrefixHelper.extract_prefix_info(normalized_mesh_name)
        if prefix_info:
            return ObjectPrefixHelper.parse_prefix_parts(prefix_info[0])

        prefix_candidate = normalized_mesh_name.split(".", 1)[0] if "." in normalized_mesh_name else normalized_mesh_name
        return ObjectPrefixHelper.parse_prefix_parts(prefix_candidate)

    @staticmethod
    def create_mesh_object(
        mesh_name:str,
        source_path:str,
        logic_name:str,
        gametypename:str,
        elements:list[D3D11Element],
        vb_data:dict,
        ib_data,
        vb_vertex_count:int,
        ib_count:int,
        ib_polygon_count:int,
        local_bounding_box_min:list | None = None,
        local_bounding_box_max:list | None = None,
        vertex_compression_params:list | None = None,
        import_collection:bpy.types.Collection | None = None,
        wwmi_shapekey_buffers:dict | None = None,
        wwmi_vertex_offset:int = 0,
        wwmi_vertex_count:int = -1,
        wwmi_vg_map:dict | None = None,
        wwmi_vg_offset:int = 0,
        wwmi_vg_count:int = 0,
    ):
        TimerUtils.Start("Import 3Dmigoto Raw")
        print("导入模型: " + mesh_name)

        if vb_vertex_count == 0:
            raise Fatal("VB vertex count is zero, skip import.")
        if ib_count == 0:
            raise Fatal("IB count is zero, skip import.")

        if import_collection is None:
            import_collection = bpy.context.scene.collection

        mesh = bpy.data.meshes.new(mesh_name)
        obj = bpy.data.objects.new(mesh.name, mesh)

        MeshCreateHelper.set_import_coordinate(obj=obj)
        MeshCreateHelper.set_import_attributes(obj=obj, gametypename=gametypename)
        MeshCreateHelper.initialize_mesh(
            mesh=mesh,
            ib_data=ib_data,
            ib_count=ib_count,
            ib_polygon_count=ib_polygon_count,
            logic_name=logic_name,
            vb_vertex_count=vb_vertex_count,
        )

        blend_indices = {}
        blend_index_formats = {}
        blend_weights = {}
        texcoords = {}
        shapekeys = {}
        use_normals = False
        normals = []

        for element in elements:
            data = vb_data[element.ElementName]

            print("当前Element: " + element.ElementName)
            print("当前数据转换前 Shape: " + str(data.shape))
            data = FormatUtils.apply_format_conversion(data, element.Format)
            print("当前数据转换后 Shape: " + str(data.shape))

            if element.SemanticName == "POSITION":
                if len(data[0]) == 4:
                    if not numpy.all((data[:, 3] == 0) | (data[:, 3] == 1)):
                        raise Fatal('Positions are 4D')

                positions = numpy.ascontiguousarray(data[:, :3], dtype=numpy.float32)
                mesh.vertices.foreach_set('co', positions.ravel())
            elif element.SemanticName.startswith("COLOR"):
                num_loops = len(mesh.loops)
                loop_vertex_indices = numpy.empty(num_loops, dtype=numpy.int32)
                mesh.loops.foreach_get('vertex_index', loop_vertex_indices)

                colors_flat = numpy.zeros((num_loops, 4), dtype=numpy.float32)
                if data.ndim > 1:
                    actual_channels = min(data.shape[1], 4)
                    colors_flat[:, :actual_channels] = data[loop_vertex_indices, :actual_channels].astype(numpy.float32)
                else:
                    colors_flat[:, 0] = data[loop_vertex_indices].astype(numpy.float32)

                if hasattr(mesh, 'color_attributes'):
                    color_attr = mesh.color_attributes.new(
                        name=element.ElementName,
                        type=MeshCreateHelper._get_color_attribute_type_by_format(element.Format),
                        domain='CORNER',
                    )
                    write_color_attribute_data(color_attr, colors_flat)
                else:
                    mesh.vertex_colors.new(name=element.ElementName)
                    mesh.vertex_colors[element.ElementName].data.foreach_set('color', colors_flat.ravel())
            elif element.SemanticName == "BLENDINDICES":
                blend_index_formats[element.SemanticIndex] = element.Format
                if data.ndim == 1:
                    blend_indices[element.SemanticIndex] = numpy.array([(x,) for x in data])
                else:
                    blend_indices[element.SemanticIndex] = data
            elif element.SemanticName == "BLENDWEIGHT" or element.SemanticName == "BLENDWEIGHTS":
                blend_weights[element.SemanticIndex] = data
            elif element.SemanticName.startswith("TEXCOORD"):
                texcoords[element.SemanticIndex] = data
            elif element.SemanticName.startswith("SHAPEKEY"):
                shapekeys[element.SemanticIndex] = data
            elif element.SemanticName.startswith("NORMAL"):
                use_normals = True
                if logic_name == LogicName.YYSLS:
                    print("燕云十六声法线处理")
                    normals = numpy.asarray(data[:, :3]) * 2 - 1
                elif logic_name == LogicName.EFMI and element.Format == "R32_UINT":
                    print("终末地压缩法线处理(Endfield Packed Normals) - 使用 TBNCodec")
                    raw = data
                    if raw.dtype != numpy.uint32:
                        raw = raw.view(numpy.uint32)
                    if raw.ndim > 1:
                        raw = raw[:, 0]

                    from ..utils.tbn_codec import TBNCodec
                    normals = TBNCodec.decode_octahedral_r32_uint(raw)
                    print("终末地压缩法线处理完成")
                else:
                    normals = numpy.ascontiguousarray(data[:, :3], dtype=numpy.float32)
            elif element.SemanticName == "ENCODEDDATA":
                if logic_name == LogicName.EFMI:
                    print("终末地 ENCODEDDATA 处理 - 使用 TBNCodec 解码 TBN 数据")
                    use_normals = True

                    raw = data
                    if raw.dtype != numpy.uint32:
                        raw = raw.view(numpy.uint32)
                    if raw.ndim > 1:
                        raw = raw[:, 0]

                    from ..utils.tbn_codec import TBNCodec
                    normals = TBNCodec.decode_octahedral_r32_uint(raw)
                    print("终末地 ENCODEDDATA 处理完成")
                else:
                    print(f"警告: ENCODEDDATA 元素仅在 EFMI 格式中支持，当前游戏类型: {logic_name}")
            elif element.SemanticName == "TANGENT":
                pass
            elif element.SemanticName == "BINORMAL":
                pass
            else:
                raise Fatal("Unknown ElementName: " + element.ElementName)

        if len(blend_weights) == 0 and len(blend_indices) != 0:
            print("检测到 BLENDWEIGHTS 为空但含 BLENDINDICES，按实际通道数补充首通道权重 1")
            for semantic_index, blendindices_tuple in blend_indices.items():
                index_array = numpy.asarray(blendindices_tuple)
                channel_count = index_array.shape[1] if index_array.ndim > 1 else 1
                default_weights = (1.0,) + (0.0,) * max(channel_count - 1, 0)
                new_list = []
                for _indices in blendindices_tuple:
                    new_list.append(default_weights)
                blend_weights[semantic_index] = new_list

        MeshCreateHelper.import_uv_layers(mesh, obj, texcoords)

        component = None
        if wwmi_vg_map is not None:
            normalized_vg_map = MeshCreateHelper._normalize_and_validate_vg_map(
                wwmi_vg_map,
                vg_count=int(wwmi_vg_count or 0),
            )
            component = SimpleNamespace(
                vg_map=normalized_vg_map,
                vg_offset=int(wwmi_vg_offset or 0),
                vg_count=int(wwmi_vg_count or 0),
            )
        elif GlobalProterties.import_merged_vgmap() and logic_name == LogicName.WWMI:
            metadatajsonpath = os.path.join(os.path.dirname(source_path), 'Metadata.json')
            if os.path.exists(metadatajsonpath):
                try:
                    extracted_object = ExtractedObjectHelper.read_metadata(metadatajsonpath)
                    prefix_parts = MeshCreateHelper._get_mesh_prefix_parts(mesh_name)
                    component_name = str(prefix_parts.get("component", "") or "").strip()
                    if component_name.isdigit():
                        partname_count = int(component_name) - 1
                        if 0 <= partname_count < len(extracted_object.components):
                            component = extracted_object.components[partname_count]
                except Exception:
                    pass

        print("导入顶点组")
        MeshCreateHelper.import_vertex_groups(
            mesh,
            obj,
            blend_indices,
            blend_weights,
            component,
            blend_index_formats=blend_index_formats,
        )
        print("导入顶点组完毕")

        MeshCreateHelper.import_shapekeys(mesh, obj, shapekeys)
        if wwmi_shapekey_buffers is not None:
            MeshCreateHelper.import_shapekeys_wwmi(
                mesh=mesh,
                obj=obj,
                shapekey_buffers=wwmi_shapekey_buffers,
                vertex_offset=wwmi_vertex_offset,
                vertex_count=wwmi_vertex_count,
            )

        mesh.validate(verbose=False, clean_customdata=False)
        mesh.update()
        if use_normals:
            MeshUtils.set_import_normals_v2(mesh=mesh, normals=normals)

        MeshCreateHelper.create_bsdf_with_diffuse_linked(
            obj=obj,
            mesh_name=mesh_name,
            directory=os.path.dirname(source_path),
        )

        if logic_name == LogicName.WWMI or logic_name == LogicName.NTEMI or logic_name == LogicName.SnowBreak:
            obj.rotation_euler[0] = 0
            obj.rotation_euler[1] = 0
            obj.rotation_euler[2] = math.radians(180)
            obj.scale = (0.01, 0.01, 0.01)

        print("导入模型完成: " + logic_name)
        if LogicName.is_zzmi_family(logic_name) or logic_name == LogicName.Naraka:
            obj.rotation_euler[0] = 0
            obj.rotation_euler[1] = 0
            obj.rotation_euler[2] = 0

        if logic_name == LogicName.EFMI:
            obj.rotation_euler[0] = 0
            obj.rotation_euler[1] = 0
            obj.rotation_euler[2] = 0

        if GlobalConfig.logic_name == LogicName.WWMI or GlobalConfig.logic_name == LogicName.NTEMI:
            if GlobalProterties.import_skip_empty_vertex_groups():
                VertexGroupUtils.remove_unused_vertex_groups(obj)
        elif GlobalConfig.logic_name == LogicName.EFMI:
            if (
                GlobalProterties.import_skip_empty_vertex_groups()
                and wwmi_vg_map is None
            ):
                # 删除空顶点组会重排组 index，而导出按组 index 取骨骼 id。
                # 只有本对象实际拿到合并 VGMap 时才禁止普通清理；预生成失败的
                # 导入级覆盖会传 None，即使 UI 选项此前为 True 也必须走普通路径。
                try:
                    VertexGroupUtils.remove_unused_vertex_groups(obj)
                    if obj.vertex_groups:
                        bpy.context.view_layer.objects.active = obj
                        bpy.ops.object.vertex_group_sort()
                except Exception as e:
                    print(f"EFMI 空顶点组清理失败（忽略）: {e}")

        import_collection.objects.link(obj)
        ObjUtils.select_obj(obj)

        bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)

        bpy.context.view_layer.update()
        if not bpy.app.background:
            # A forced full-window swap per object dominates batch import time.
            # Request redraw; Blender renders once control returns to its event loop.
            manager = getattr(bpy.context, 'window_manager', None)
            for window in getattr(manager, 'windows', ()):
                for area in window.screen.areas:
                    area.tag_redraw()

        TimerUtils.End("Import 3Dmigoto Raw")
        return obj

    @staticmethod
    def set_import_attributes(obj, gametypename:str):
        obj["3DMigoto:RecalculateTANGENT"] = False
        obj["3DMigoto:RecalculateCOLOR"] = False
        obj['3DMigoto:GameTypeName'] = gametypename

    @staticmethod
    def set_import_coordinate(obj):
        obj.matrix_world = axis_conversion(from_forward='-Z', from_up='Y').to_4x4()

    @staticmethod
    def initialize_mesh(mesh, ib_data, ib_count:int, ib_polygon_count:int, logic_name:str, vb_vertex_count:int):
        if (
            logic_name == LogicName.WWMI
            or logic_name == LogicName.NTEMI
            or logic_name == LogicName.YYSLS
            or logic_name == LogicName.SnowBreak
        ):
            flipped_indices = []
            for i in range(0, len(ib_data), 3):
                triangle = ib_data[i:i + 3]
                flipped_indices.extend(triangle[::-1])
            ib_data = flipped_indices

        mesh.loops.add(ib_count)
        mesh.polygons.add(ib_polygon_count)
        mesh.loops.foreach_set('vertex_index', ib_data)
        mesh.polygons.foreach_set('loop_start', [x * 3 for x in range(ib_polygon_count)])
        mesh.polygons.foreach_set('loop_total', [3] * ib_polygon_count)
        mesh.vertices.add(vb_vertex_count)
        mesh.update()

    @staticmethod
    def import_uv_layers(mesh, obj, texcoords):
        loops = mesh.loops
        vertex_indices = numpy.array([loop.vertex_index for loop in loops], dtype=numpy.int32)

        for texcoord, data in sorted(texcoords.items()):
            data_np = numpy.array(data, dtype=numpy.float32)
            dim = data_np.shape[1]

            if dim == 4:
                components_list = ('xy', 'zw')
            elif dim == 2:
                components_list = ('xy',)
            else:
                raise Fatal(f'Unhandled TEXCOORD dimension: {dim}')

            cmap = {'x': 0, 'y': 1, 'z': 2, 'w': 3}

            for components in components_list:
                uv_name = f'TEXCOORD{texcoord if texcoord else ""}.{components}'
                mesh.uv_layers.new(name=uv_name)
                blender_uvs = mesh.uv_layers[uv_name]

                c0 = cmap[components[0]]
                c1 = cmap[components[1]]

                uvs = numpy.empty((len(data_np), 2), dtype=numpy.float32)
                uvs[:, 0] = data_np[:, c0]
                uvs[:, 1] = 1.0 - data_np[:, c1]

                max_index = numpy.max(vertex_indices) if len(vertex_indices) > 0 else 0
                if max_index >= len(uvs):
                    print(f"Warning: UV data too short. Max index: {max_index}, UV data len: {len(uvs)}.Padding with zeros.")
                    padding_length = max_index - len(uvs) + 1
                    padding = numpy.zeros((padding_length, 2), dtype=numpy.float32)
                    uvs = numpy.vstack((uvs, padding))

                uv_array = uvs[vertex_indices].ravel()
                blender_uvs.data.foreach_set('uv', uv_array)

    @staticmethod
    def _normalize_blend_index_array(values, dxgi_format:str | None = None):
        """把 BLENDINDICES 转成 int64，并按实际 DXGI 位宽消除 UINT 哨兵。"""
        source = numpy.asarray(values)
        if source.dtype.kind == 'f':
            normalized = numpy.rint(source).astype(numpy.int64)
        else:
            normalized = source.astype(numpy.int64, copy=True)

        format_name = str(dxgi_format or "").upper()
        sentinel = None
        if "_UINT" in format_name:
            if format_name.startswith("R8"):
                sentinel = 0xFF
            elif format_name.startswith("R16"):
                sentinel = 0xFFFF
            elif format_name.startswith("R32"):
                sentinel = 0xFFFFFFFF
        elif not format_name and source.dtype.kind == 'u':
            sentinel = (1 << (source.dtype.itemsize * 8)) - 1

        if sentinel is not None:
            normalized[normalized == sentinel] = -1
        return normalized

    @staticmethod
    def _valid_blend_channel_mask(indices, weights):
        """只把“索引有效且权重大于零”的通道计入顶点组。"""
        index_array = numpy.asarray(indices)
        weight_array = numpy.asarray(weights)
        if index_array.shape != weight_array.shape:
            raise Fatal(
                f"BLENDINDICES/BLENDWEIGHT shape mismatch: "
                f"{index_array.shape} != {weight_array.shape}"
            )
        return (
            (index_array >= 0)
            & numpy.isfinite(weight_array)
            & (weight_array > 0.0)
        )

    @staticmethod
    def _normalize_and_validate_vg_map(vg_map:dict, vg_count:int):
        """规范化合并骨架 VGMap；缺键/重复键/负槽位一律大声失败。"""
        if not isinstance(vg_map, dict):
            raise Fatal("合并骨架 VGMap 必须是字典")
        try:
            expected_count = int(vg_count)
        except (TypeError, ValueError) as exc:
            raise Fatal(f"合并骨架 VGCount 无效: {vg_count}") from exc
        if expected_count < 0:
            raise Fatal(f"合并骨架 VGCount 不能为负数: {expected_count}")

        normalized = {}
        for raw_key, raw_value in vg_map.items():
            try:
                key = int(raw_key)
                value = int(raw_value)
            except (TypeError, ValueError) as exc:
                raise Fatal(f"合并骨架 VGMap 含非整数项: {raw_key!r} -> {raw_value!r}") from exc
            if key in normalized:
                raise Fatal(f"合并骨架 VGMap 规范化后键重复: {key}")
            if value < 0:
                raise Fatal(f"合并骨架 VGMap 槽位不能为负数: {key} -> {value}")
            normalized[key] = value

        expected_keys = set(range(expected_count))
        actual_keys = set(normalized)
        if actual_keys != expected_keys:
            missing = sorted(expected_keys - actual_keys)
            extra = sorted(actual_keys - expected_keys)
            raise Fatal(
                f"合并骨架 VGMap 键不完整（VGCount={expected_count}，"
                f"缺少={missing}，多余={extra}）"
            )
        return normalized

    @staticmethod
    def import_vertex_groups(
        mesh,
        obj,
        blend_indices,
        blend_weights,
        component,
        blend_index_formats=None,
    ):
        blend_index_formats = blend_index_formats or {}

        for semantic_index, bone_indices_list in blend_indices.items():
            blend_indices[semantic_index] = MeshCreateHelper._normalize_blend_index_array(
                bone_indices_list,
                blend_index_formats.get(semantic_index),
            )

        assert len(blend_indices) == len(blend_weights)
        if blend_indices:
            max_valid_group_id = -1
            valid_masks = {}
            for semantic_index, bone_indices_array in blend_indices.items():
                valid_mask = MeshCreateHelper._valid_blend_channel_mask(
                    bone_indices_array,
                    blend_weights[semantic_index],
                )
                valid_masks[semantic_index] = valid_mask
                valid_indices = bone_indices_array[valid_mask]
                if valid_indices.size > 0:
                    max_valid_group_id = max(max_valid_group_id, int(valid_indices.max()))

            if max_valid_group_id < 0:
                return

            if component is None:
                num_vertex_groups = max_valid_group_id + 1
            else:
                vg_count = int(getattr(component, "vg_count", len(component.vg_map)) or 0)
                component.vg_map = MeshCreateHelper._normalize_and_validate_vg_map(
                    component.vg_map,
                    vg_count,
                )
                mapped_group_ids = set(component.vg_map.values())
                max_global_group_id = max(mapped_group_ids) if mapped_group_ids else -1
                if max_global_group_id < 0:
                    return
                num_vertex_groups = max_global_group_id + 1

            print("num_vertex_groups: " + str(num_vertex_groups))

            if num_vertex_groups > 10000:
                raise Fatal("检测到在当前导入的数据类型" + obj.get('3DMigoto:GameTypeName', "") + "描述下，BLENDINDICES顶点组数量为: " + str(num_vertex_groups) + " 基本不可能是正常情况，请更换其他数据类型重新导入")

            vertex_group_by_id = {}
            for i in range(num_vertex_groups):
                vertex_group_by_id[i] = obj.vertex_groups.new(name=str(i))
            for vertex in mesh.vertices:
                for semantic_index in sorted(blend_indices.keys()):
                    for i, w in zip(blend_indices[semantic_index][vertex.index], blend_weights[semantic_index][vertex.index]):
                        if i < 0 or not numpy.isfinite(w) or w <= 0.0:
                            continue
                        if component is None:
                            target_group_id = int(i)
                        else:
                            try:
                                target_group_id = int(component.vg_map[int(i)])
                            except KeyError as exc:
                                raise Fatal(
                                    f"BLENDINDICES 引用了 VGMap 未覆盖的局部骨骼 {int(i)}"
                                ) from exc

                        if target_group_id < 0:
                            continue

                        vertex_group = vertex_group_by_id.get(target_group_id)
                        if vertex_group is None:
                            vertex_group = obj.vertex_groups.get(str(target_group_id))
                            if vertex_group is None:
                                vertex_group = obj.vertex_groups.new(name=str(target_group_id))
                            vertex_group_by_id[target_group_id] = vertex_group
                        vertex_group.add((vertex.index,), float(w), 'REPLACE')

    @staticmethod
    def import_shapekeys(mesh, obj, shapekeys):
        if not shapekeys:
            return

        basis = obj.shape_key_add(name='Basis')
        basis.interpolation = 'KEY_LINEAR'
        obj.data.shape_keys.use_relative = True
        try:
            basis.value = 0.0
        except Exception:
            pass

        vert_count = len(obj.data.vertices)
        basis_co = numpy.empty(vert_count * 3, dtype=numpy.float32)
        basis.data.foreach_get('co', basis_co)
        basis_co = basis_co.reshape(-1, 3)

        for sk_id, offsets in shapekeys.items():
            new_sk = obj.shape_key_add(name=f'Deform {sk_id}')
            new_sk.interpolation = 'KEY_LINEAR'
            try:
                new_sk.value = 0.0
            except Exception:
                pass

            offset_arr = numpy.array(offsets, dtype=numpy.float32).reshape(-1, 3)
            new_co = basis_co + offset_arr
            new_sk.data.foreach_set('co', new_co.ravel())
            try:
                new_sk.value = 0.0
            except Exception:
                pass
            del new_sk

        del basis_co, offset_arr, new_co

    @staticmethod
    def import_shapekeys_wwmi(mesh, obj, shapekey_buffers:dict, vertex_offset:int, vertex_count:int):
        sk_offset_raw = shapekey_buffers.get("ShapeKeyOffset")
        sk_vertex_id_raw = shapekey_buffers.get("ShapeKeyVertexId")
        sk_vertex_offset_raw = shapekey_buffers.get("ShapeKeyVertexOffset")

        if sk_offset_raw is None or sk_vertex_id_raw is None or sk_vertex_offset_raw is None:
            return

        offsets = numpy.asarray(sk_offset_raw).view(numpy.uint32)
        if len(offsets) < 2:
            return

        vertex_id_buffer = numpy.asarray(sk_vertex_id_raw).view(numpy.uint32)
        vertex_offset_buffer = numpy.asarray(sk_vertex_offset_raw).view(numpy.float16)
        effective_vertex_count = vertex_count if vertex_count > 0 else len(obj.data.vertices)
        if effective_vertex_count <= 0:
            return

        if obj.data.shape_keys is None:
            basis = obj.shape_key_add(name='Basis')
            basis.interpolation = 'KEY_LINEAR'
            obj.data.shape_keys.use_relative = True
            try:
                basis.value = 0.0
            except Exception:
                pass
        else:
            basis = obj.data.shape_keys.key_blocks.get('Basis') or obj.data.shape_keys.key_blocks[0]

        basis_co = numpy.empty(len(obj.data.vertices) * 3, dtype=numpy.float32)
        basis.data.foreach_get('co', basis_co)
        basis_co = basis_co.reshape(-1, 3)

        shapekey_count = min(127, len(offsets) - 1)
        for sk_id in range(shapekey_count):
            first_entry = int(offsets[sk_id])
            last_entry = int(offsets[sk_id + 1])
            if last_entry <= first_entry:
                continue

            entries = numpy.arange(first_entry, last_entry, dtype=numpy.int64)
            entries = entries[entries < len(vertex_id_buffer)]
            if len(entries) == 0:
                continue

            global_vertex_ids = vertex_id_buffer[entries].astype(numpy.int64)
            local_vertex_ids = global_vertex_ids - int(vertex_offset)
            local_vertex_mask = (local_vertex_ids >= 0) & (local_vertex_ids < effective_vertex_count)
            entries = entries[local_vertex_mask]
            local_vertex_ids = local_vertex_ids[local_vertex_mask]
            if len(entries) == 0:
                continue

            dx_idx = entries * 6
            dy_idx = dx_idx + 1
            dz_idx = dx_idx + 2
            valid_offset_mask = dz_idx < len(vertex_offset_buffer)
            entries = entries[valid_offset_mask]
            local_vertex_ids = local_vertex_ids[valid_offset_mask]
            if len(entries) == 0:
                continue

            dx_idx = entries * 6
            dy_idx = dx_idx + 1
            dz_idx = dx_idx + 2

            new_co = basis_co.copy()
            numpy.add.at(new_co[:, 0], local_vertex_ids, vertex_offset_buffer[dx_idx].astype(numpy.float32))
            numpy.add.at(new_co[:, 1], local_vertex_ids, vertex_offset_buffer[dy_idx].astype(numpy.float32))
            numpy.add.at(new_co[:, 2], local_vertex_ids, vertex_offset_buffer[dz_idx].astype(numpy.float32))

            shapekey_name = f'Deform {sk_id}'
            shapekey = None if obj.data.shape_keys is None else obj.data.shape_keys.key_blocks.get(shapekey_name)
            if shapekey is None:
                shapekey = obj.shape_key_add(name=shapekey_name)
            shapekey.interpolation = 'KEY_LINEAR'
            shapekey.data.foreach_set('co', new_co.ravel())
            try:
                shapekey.value = 0.0
            except Exception:
                pass

    @staticmethod
    def get_import_texture_paths(mesh_name: str, directory: str):
        prefix_parts = MeshCreateHelper._get_mesh_prefix_parts(mesh_name)
        draw_ib = str(prefix_parts.get("draw_ib", "") or "").strip()
        component = str(prefix_parts.get("component", "") or "").strip()
        if not draw_ib or not component:
            return None, None

        texture_prefix = draw_ib + "-" + component + "-"
        texture_path = TextureUtils.find_texture(texture_prefix, "-DiffuseMap.dds", directory)
        normal_path = TextureUtils.find_texture(texture_prefix, "-NormalMap.dds", directory)
        return texture_path, normal_path

    @staticmethod
    def get_material_output_node(nodes):
        output = nodes.get("Material Output")
        if output is None:
            output = next((node for node in nodes if node.bl_idname == 'ShaderNodeOutputMaterial'), None)
        if output is None:
            output = nodes.new(type='ShaderNodeOutputMaterial')
        return output

    @staticmethod
    def clear_material_nodes(node_tree):
        for node in list(node_tree.nodes):
            try:
                node_tree.nodes.remove(node)
            except Exception:
                continue

    @staticmethod
    def create_diffuse_material_graph(node_tree, texture_path: str):
        MeshCreateHelper.clear_material_nodes(node_tree)
        tex_image = node_tree.nodes.new('ShaderNodeTexImage')
        tex_image.image = bpy.data.images.load(texture_path)
        tex_image.image.colorspace_settings.name = "sRGB"
        tex_image.image.alpha_mode = "NONE"
        tex_image.location = (-400, 0)

        diffuse = node_tree.nodes.new('ShaderNodeBsdfDiffuse')
        diffuse.location = (-100, 0)

        output = MeshCreateHelper.get_material_output_node(node_tree.nodes)
        output.location = (180, 0)

        node_tree.links.new(tex_image.outputs['Color'], diffuse.inputs['Color'])
        node_tree.links.new(diffuse.outputs['BSDF'], output.inputs['Surface'])
        return tex_image, diffuse

    @staticmethod
    def create_transparent_material_graph(node_tree, texture_path: str):
        MeshCreateHelper.clear_material_nodes(node_tree)
        tex_image = node_tree.nodes.new('ShaderNodeTexImage')
        tex_image.image = bpy.data.images.load(texture_path)
        tex_image.image.colorspace_settings.name = "sRGB"
        tex_image.location = (-520, 0)

        transparent = node_tree.nodes.new('ShaderNodeBsdfTransparent')
        transparent.location = (-180, 140)

        diffuse = node_tree.nodes.new('ShaderNodeBsdfDiffuse')
        diffuse.location = (-180, -80)

        mix_shader = node_tree.nodes.new('ShaderNodeMixShader')
        mix_shader.location = (100, 0)

        output = MeshCreateHelper.get_material_output_node(node_tree.nodes)
        output.location = (360, 0)

        node_tree.links.new(tex_image.outputs['Color'], diffuse.inputs['Color'])
        # 开启"导入贴图时忽略透明度通道"时，贴图 Alpha 模式设为"无"，
        # Alpha 输出始终为 1，等效于不透明，且不破坏着色器连接结构
        try:
            ignore_texture_alpha = bool(GlobalProterties.ignore_texture_alpha())
        except Exception:
            ignore_texture_alpha = False
        if ignore_texture_alpha:
            tex_image.image.alpha_mode = "NONE"
        else:
            tex_image.image.alpha_mode = "CHANNEL_PACKED"
        node_tree.links.new(tex_image.outputs['Alpha'], mix_shader.inputs[0])
        node_tree.links.new(transparent.outputs['BSDF'], mix_shader.inputs[1])
        node_tree.links.new(diffuse.outputs['BSDF'], mix_shader.inputs[2])
        node_tree.links.new(mix_shader.outputs['Shader'], output.inputs['Surface'])
        return tex_image, diffuse

    @staticmethod
    def create_identity_v_normal_map(node_tree, diffuse, normal_path: str):
        norm_image = node_tree.nodes.new('ShaderNodeTexImage')
        norm_image.image = bpy.data.images.load(normal_path)
        norm_image.location.x = diffuse.location.x - 1100
        norm_image.location.y = diffuse.location.y - 300
        norm_image.image.colorspace_settings.is_data = True
        norm_image.image.colorspace_settings.name = 'Non-Color'

        norm_separate = node_tree.nodes.new('ShaderNodeSeparateColor')
        norm_separate.location.x = diffuse.location.x - 700
        norm_separate.location.y = diffuse.location.y - 450
        if hasattr(norm_separate, 'mode'):
            norm_separate.mode = 'RGB'

        rgb_curve = node_tree.nodes.new('ShaderNodeRGBCurve')
        rgb_curve.location.x = diffuse.location.x - 700
        rgb_curve.location.y = diffuse.location.y - 100
        if 'Fac' in rgb_curve.inputs:
            rgb_curve.inputs['Fac'].default_value = 1.0
        if hasattr(rgb_curve, 'mapping'):
            rgb_curve.mapping.initialize()
            green_curve = rgb_curve.mapping.curves[1]
            if len(green_curve.points) >= 2:
                green_curve.points[0].location = (0.0, 1.0)
                green_curve.points[1].location = (1.0, 0.0)
            rgb_curve.mapping.update()

        norm_map = node_tree.nodes.new('ShaderNodeNormalMap')
        norm_map.location.x = diffuse.location.x - 300
        norm_map.location.y = diffuse.location.y - 100
        norm_map.uv_map = "TEXCOORD.xy"
        if hasattr(norm_map, 'space'):
            norm_map.space = 'TANGENT'
        if 'Strength' in norm_map.inputs:
            norm_map.inputs['Strength'].default_value = 1.0

        node_tree.links.new(norm_separate.inputs['Color'], norm_image.outputs['Color'])
        node_tree.links.new(rgb_curve.inputs['Color'], norm_image.outputs['Color'])
        node_tree.links.new(norm_map.inputs['Color'], rgb_curve.outputs['Color'])
        node_tree.links.new(diffuse.inputs['Normal'], norm_map.outputs['Normal'])
        return norm_map, norm_separate

    @staticmethod
    def create_standard_normal_map(node_tree, diffuse, normal_path: str):
        norm_image = node_tree.nodes.new('ShaderNodeTexImage')
        norm_image.image = bpy.data.images.load(normal_path)
        norm_image.location.x = diffuse.location.x - 700
        norm_image.location.y = diffuse.location.y - 400
        norm_image.image.colorspace_settings.is_data = True
        norm_image.image.colorspace_settings.name = 'Non-Color'

        norm_map = node_tree.nodes.new('ShaderNodeNormalMap')
        norm_map.location.x = diffuse.location.x - 300
        norm_map.location.y = diffuse.location.y - 400
        norm_map.uv_map = "TEXCOORD.xy"
        node_tree.links.new(norm_map.inputs['Color'], norm_image.outputs['Color'])
        node_tree.links.new(diffuse.inputs['Normal'], norm_map.outputs['Normal'])
        return norm_map

    @staticmethod
    def create_zzmi_gimi_normal_map(node_tree, diffuse, normal_path: str):
        norm_image = node_tree.nodes.new('ShaderNodeTexImage')
        norm_image.image = bpy.data.images.load(normal_path)
        norm_image.location.x = diffuse.location.x - 1100
        norm_image.location.y = diffuse.location.y - 400
        norm_image.image.colorspace_settings.is_data = True
        norm_image.image.colorspace_settings.name = 'Non-Color'

        norm_separate = node_tree.nodes.new('ShaderNodeSeparateColor')
        norm_separate.location.x = diffuse.location.x - 700
        norm_separate.location.y = diffuse.location.y - 400
        node_tree.links.new(norm_separate.inputs['Color'], norm_image.outputs['Color'])

        norm_combine = node_tree.nodes.new('ShaderNodeCombineColor')
        norm_combine.location.x = diffuse.location.x - 500
        norm_combine.location.y = diffuse.location.y - 400
        node_tree.links.new(norm_combine.inputs['Red'], norm_separate.outputs['Red'])
        node_tree.links.new(norm_combine.inputs['Green'], norm_separate.outputs['Green'])

        norm_math = node_tree.nodes.new('ShaderNodeMath')
        norm_math.location.x = diffuse.location.x - 300
        norm_math.location.y = diffuse.location.y - 600
        norm_math.operation = 'SQRT'
        norm_math.use_clamp = True

        norm_math_2 = node_tree.nodes.new('ShaderNodeMath')
        norm_math_2.location.x = diffuse.location.x - 500
        norm_math_2.location.y = diffuse.location.y - 800
        norm_math_2.operation = 'SUBTRACT'
        norm_math_2.inputs[0].default_value = 1.0
        norm_math_2.use_clamp = True

        norm_math_r2 = node_tree.nodes.new('ShaderNodeMath')
        norm_math_r2.location.x = diffuse.location.x - 700
        norm_math_r2.location.y = diffuse.location.y - 600
        norm_math_r2.operation = 'POWER'
        norm_math_r2.inputs[1].default_value = 2.0

        norm_math_g2 = node_tree.nodes.new('ShaderNodeMath')
        norm_math_g2.location.x = diffuse.location.x - 700
        norm_math_g2.location.y = diffuse.location.y - 800
        norm_math_g2.operation = 'POWER'
        norm_math_g2.inputs[1].default_value = 2.0

        norm_math_add_r_g = node_tree.nodes.new('ShaderNodeMath')
        norm_math_add_r_g.location.x = diffuse.location.x - 500
        norm_math_add_r_g.location.y = diffuse.location.y - 600
        norm_math_add_r_g.operation = 'ADD'

        node_tree.links.new(norm_math_r2.inputs[0], norm_separate.outputs['Red'])
        node_tree.links.new(norm_math_g2.inputs[0], norm_separate.outputs['Green'])
        node_tree.links.new(norm_math_add_r_g.inputs[0], norm_math_r2.outputs['Value'])
        node_tree.links.new(norm_math_add_r_g.inputs[1], norm_math_g2.outputs['Value'])
        node_tree.links.new(norm_math_2.inputs[1], norm_math_add_r_g.outputs['Value'])
        node_tree.links.new(norm_math.inputs[0], norm_math_2.outputs['Value'])
        node_tree.links.new(norm_combine.inputs['Blue'], norm_math.outputs['Value'])

        norm_map = node_tree.nodes.new('ShaderNodeNormalMap')
        norm_map.location.x = diffuse.location.x - 300
        norm_map.location.y = diffuse.location.y - 400
        norm_map.uv_map = "TEXCOORD.xy"
        node_tree.links.new(norm_map.inputs['Color'], norm_combine.outputs['Color'])
        node_tree.links.new(diffuse.inputs['Normal'], norm_map.outputs['Normal'])
        return norm_map

    @staticmethod
    def apply_normal_texture(node_tree, diffuse, normal_path: str, logic_name: str):
        if logic_name == LogicName.IdentityV:
            MeshCreateHelper.create_identity_v_normal_map(node_tree, diffuse, normal_path)
            return

        if not LogicName.is_zzmi_family(logic_name) and logic_name != LogicName.GIMI:
            MeshCreateHelper.create_standard_normal_map(node_tree, diffuse, normal_path)
            return

        MeshCreateHelper.create_zzmi_gimi_normal_map(node_tree, diffuse, normal_path)

    @staticmethod
    def assign_material(obj, material):
        if obj.data.materials:
            obj.data.materials[0] = material
        else:
            obj.data.materials.append(material)

    @staticmethod
    def create_bsdf_with_diffuse_linked(obj, mesh_name: str, directory: str, logic_name: str | None = None):
        if logic_name is None:
            logic_name = GlobalConfig.logic_name

        # SSMT 子网格标记优先：标记（SubmeshJson.TextureMarkUpInfoList）是导入/导出
        # 两端共用的权威来源——导出 mod 时按它决定导出哪些类型贴图、写哪些 ps-tN，
        # 没标记的类型既不会导出贴图也不会写进 ini。勾选后按标记逐类型建独立材质，
        # 命名与「材质转资源pro」按材质名首段识别类型的口径一致。
        # 读不到标记（旧工作空间 / 无标记部件）时返回 0，原样回退下面的文件名搜索。
        # 注：「导入贴图材质去掉颜色贴图前缀」与「自动上贴图时使用法线贴图」两个开关已移除
        # （2026-10-07）：材质名固定带 DiffuseMap_ 前缀，法线由 IMGPV 预览材质按游戏类型接。
        try:
            use_marked_materials = bool(GlobalProterties.import_materials_by_submesh_mark())
        except Exception:
            use_marked_materials = False
        if use_marked_materials:
            from .marked_texture_material import build_marked_materials

            created = build_marked_materials(
                obj=obj,
                mesh_name=mesh_name,
                directory=directory,
                logic_name=logic_name,
            )
            if created > 0:
                return

        texture_path, _normal_path = MeshCreateHelper.get_import_texture_paths(mesh_name, directory)
        if texture_path is None:
            return

        material_name = f"DiffuseMap_{mesh_name}"
        material = bpy.data.materials.new(name=material_name)
        material.use_nodes = True
        if logic_name == LogicName.IdentityV:
            material.blend_method = "OPAQUE"
            _tex_node, diffuse = MeshCreateHelper.create_diffuse_material_graph(
                node_tree=material.node_tree,
                texture_path=texture_path,
            )
        else:
            material.blend_method = "BLEND"
            if hasattr(material, "use_transparency_overlap"):
                material.use_transparency_overlap = False
            elif hasattr(material, "show_transparent_back"):
                material.show_transparent_back = False

            _tex_node, diffuse = MeshCreateHelper.create_transparent_material_graph(
                node_tree=material.node_tree,
                texture_path=texture_path,
            )

        MeshCreateHelper.assign_material(obj, material)
