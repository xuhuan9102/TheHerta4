import bpy
import itertools
import math
import numpy
import os

from bpy_extras.io_utils import unpack_list, axis_conversion

from ..utils.format_utils import Fatal, FormatUtils
from ..utils.mesh_utils import MeshUtils
from ..utils.obj_utils import ObjUtils
from ..utils.texture_utils import TextureUtils
from ..utils.timer_utils import TimerUtils
from ..utils.vertexgroup_utils import VertexGroupUtils

from .global_config import GlobalConfig
from .global_properties import GlobalProterties
from .logic_name import LogicName
from .d3d11_element import D3D11Element
from ..ui.wwmi.extracted_object import ExtractedObjectHelper


class MeshCreateHelper:
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
                    if not all(x[3] in (0, 1) for x in data):
                        raise Fatal('Positions are 4D')

                positions = [(x[0], x[1], x[2]) for x in data]
                mesh.vertices.foreach_set('co', unpack_list(positions))
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
                    color_attr = mesh.color_attributes.new(name=element.ElementName, type='FLOAT_COLOR', domain='CORNER')
                    color_attr.data.foreach_set('color', colors_flat.ravel())
                else:
                    mesh.vertex_colors.new(name=element.ElementName)
                    mesh.vertex_colors[element.ElementName].data.foreach_set('color', colors_flat.ravel())
            elif element.SemanticName == "BLENDINDICES":
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
                    normals = [(x[0] * 2 - 1, x[1] * 2 - 1, x[2] * 2 - 1) for x in data]
                elif logic_name == LogicName.EFMI and element.Format == "R32_UINT":
                    print("终末地压缩法线处理(Endfield Packed Normals) - 使用 TBNCodec")
                    raw = data
                    if raw.dtype != numpy.uint32:
                        raw = raw.view(numpy.uint32)
                    if raw.ndim > 1:
                        raw = raw[:, 0]

                    from ..utils.tbn_codec import TBNCodec
                    normals = TBNCodec.decode_octahedral_r32_uint(raw).tolist()
                    print("终末地压缩法线处理完成")
                else:
                    normals = [(x[0], x[1], x[2]) for x in data]
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
                    normals = TBNCodec.decode_octahedral_r32_uint(raw).tolist()
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
            print("检测到BLENDWEIGHTS为空，但是含有BLENDINDICES数据，特殊情况，默认补充1,0,0,0的BLENDWEIGHTS")
            for semantic_index, blendindices_tuple in blend_indices.items():
                new_list = []
                for _indices in blendindices_tuple:
                    new_list.append((1.0, 0, 0, 0))
                blend_weights[semantic_index] = new_list

        MeshCreateHelper.import_uv_layers(mesh, obj, texcoords)

        component = None
        if GlobalProterties.import_merged_vgmap() and (GlobalConfig.logic_name == LogicName.WWMI):
            print("尝试读取Metadata.json")
            metadatajsonpath = os.path.join(os.path.dirname(source_path), 'Metadata.json')
            if os.path.exists(metadatajsonpath):
                print("鸣潮读取Metadata.json")
                extracted_object = ExtractedObjectHelper.read_metadata(metadatajsonpath)
                if "-" in mesh_name:
                    partname_count = int(mesh_name.split("-")[1]) - 1
                    print("import partname count: " + str(partname_count))
                    component = extracted_object.components[partname_count]

        print("导入顶点组")
        MeshCreateHelper.import_vertex_groups(mesh, obj, blend_indices, blend_weights, component)
        print("导入顶点组完毕")

        MeshCreateHelper.import_shapekeys(mesh, obj, shapekeys)

        mesh.validate(verbose=False, clean_customdata=False)
        mesh.update()
        if use_normals:
            MeshUtils.set_import_normals_v2(mesh=mesh, normals=normals)

        MeshCreateHelper.create_bsdf_with_diffuse_linked(
            obj=obj,
            mesh_name=mesh_name,
            directory=os.path.dirname(source_path),
        )

        if logic_name == LogicName.WWMI:
            obj.rotation_euler[0] = 0
            obj.rotation_euler[1] = 0
            obj.rotation_euler[2] = math.radians(180)
            obj.scale = (0.01, 0.01, 0.01)

        print("导入模型完成: " + logic_name)
        if logic_name == LogicName.ZZMI or logic_name == LogicName.Naraka:
            obj.rotation_euler[0] = 0
            obj.rotation_euler[1] = 0
            obj.rotation_euler[2] = 0

        if logic_name == LogicName.EFMI:
            obj.rotation_euler[0] = 0
            obj.rotation_euler[1] = 0
            obj.rotation_euler[2] = 0

        if GlobalConfig.logic_name == LogicName.WWMI:
            if GlobalProterties.import_skip_empty_vertex_groups():
                VertexGroupUtils.remove_unused_vertex_groups(obj)

        import_collection.objects.link(obj)
        ObjUtils.select_obj(obj)

        bpy.ops.object.transform_apply(location=False, rotation=True, scale=True)

        bpy.context.view_layer.update()
        bpy.ops.wm.redraw_timer(type='DRAW_WIN_SWAP', iterations=1)

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
        if logic_name == LogicName.WWMI or logic_name == LogicName.YYSLS:
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
    def import_vertex_groups(mesh, obj, blend_indices, blend_weights, component):
        for semantic_index, bone_indices_list in blend_indices.items():
            arr = numpy.array(bone_indices_list)
            arr = numpy.where(arr == 65535, -1, arr)
            blend_indices[semantic_index] = arr

        assert len(blend_indices) == len(blend_weights)
        if blend_indices:
            if component is None:
                num_vertex_groups = max(itertools.chain(*itertools.chain(*blend_indices.values()))) + 1
            else:
                num_vertex_groups = max(component.vg_map.values()) + 1

            print("num_vertex_groups: " + str(num_vertex_groups))

            if num_vertex_groups > 10000:
                raise Fatal("检测到在当前导入的数据类型" + obj.get('3DMigoto:GameTypeName', "") + "描述下，BLENDINDICES顶点组数量为: " + str(num_vertex_groups) + " 基本不可能是正常情况，请更换其他数据类型重新导入")

            for i in range(num_vertex_groups):
                obj.vertex_groups.new(name=str(i))
            for vertex in mesh.vertices:
                for semantic_index in sorted(blend_indices.keys()):
                    for i, w in zip(blend_indices[semantic_index][vertex.index], blend_weights[semantic_index][vertex.index]):
                        if w == 0.0:
                            continue
                        if component is None:
                            obj.vertex_groups[i].add((vertex.index,), w, 'REPLACE')
                        else:
                            obj.vertex_groups[component.vg_map[str(i)]].add((vertex.index,), w, 'REPLACE')

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
    def create_bsdf_with_diffuse_linked(obj, mesh_name:str, directory:str):
        material_name = f"{mesh_name}_Material"

        if "." in mesh_name:
            mesh_name_split = str(mesh_name).split(".")[0].split("-")
        else:
            mesh_name_split = str(mesh_name).split("-")

        if len(mesh_name_split) < 2:
            return

        texture_prefix = mesh_name_split[0] + "-" + mesh_name_split[1] + "-"

        texture_path = TextureUtils.find_texture(texture_prefix, "-DiffuseMap.dds", directory)

        if texture_path is not None:
            material = bpy.data.materials.new(name=material_name)
            material.use_nodes = True

            bsdf = material.node_tree.nodes.get("原理化 BSDF")
            if not bsdf:
                bsdf = material.node_tree.nodes.get("原理化BSDF")
            if not bsdf:
                bsdf = material.node_tree.nodes.get("Principled BSDF")

            if bsdf:
                tex_image = material.node_tree.nodes.new('ShaderNodeTexImage')
                tex_image.image = bpy.data.images.load(texture_path)
                tex_image.image.alpha_mode = "NONE"
                tex_image.location.x = bsdf.location.x - 400
                tex_image.location.y = bsdf.location.y
                material.node_tree.links.new(bsdf.inputs['Base Color'], tex_image.outputs['Color'])
                material.node_tree.links.new(bsdf.inputs['Alpha'], tex_image.outputs['Alpha'])

            if obj.data.materials:
                obj.data.materials[0] = material
            else:
                obj.data.materials.append(material)