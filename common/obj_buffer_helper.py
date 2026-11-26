import collections
import math
import copy
from .d3d11_gametype import D3D11GameType
from .draw_call_model import DrawCallModel


from ..utils.format_utils import FormatUtils
from ..utils.vertexgroup_utils import VertexGroupUtils
from ..utils.timer_utils import TimerUtils
from ..utils.tbn_codec import TBNCodec
from ..utils.color_attribute_utils import (
    encode_color_for_unorm_target,
    read_color_attribute_data,
    sample_color_attribute_to_loops,
)
from ..utils.ssmt_error_utils import SSMTErrorUtils, Fatal
from .logic_name import LogicName
from .global_config import GlobalConfig
from .global_properties import GlobalProterties
from .mesh_attribute_names import (
    IMPORTED_NORMAL_XYZ,
    IMPORTED_TANGENT_W,
    IMPORTED_TANGENT_XYZ,
)


import bpy
import numpy

class ObjBufferHelper:
    '''
    工具类，由于使用了抽象数据类型
    所以归为Helper类中
    '''

    @staticmethod
    def ensure_color_attribute(obj, element_name, d3d11_element=None):
        """确保物体上有 element_name 这个顶点色属性；没有就按元素格式创建。

        为什么导出前也要做：物体本身没有顶点色时，"重算 COLOR" 在物体上就没有落点
        （实测表现：这种物体看起来什么都没发生）。格式按 d3d11 元素的 Format 选
        BYTE_COLOR / FLOAT_COLOR，域与导出读取口径一致用 CORNER。
        """
        mesh = getattr(obj, "data", None)
        if mesh is None:
            return None
        collection = getattr(mesh, "color_attributes", None)
        if collection is None:
            legacy = getattr(mesh, "vertex_colors", None)
            if legacy is None:
                return None
            if element_name in legacy:
                return legacy[element_name]
            created = legacy.new(name=element_name)
            print("当前obj [" + str(getattr(obj, "name", "?")) + "] 缺少游戏渲染所需的COLOR: [" + element_name + "]，已自动补全")
            return created
        existing = collection.get(element_name)
        if existing is not None:
            return existing
        attribute_type = "BYTE_COLOR"
        normalized_format = str(getattr(d3d11_element, "Format", "") or "").upper()
        if (
            normalized_format.endswith("_FLOAT")
            or normalized_format.endswith("_SNORM")
            or "16" in normalized_format
            or "32" in normalized_format
        ):
            attribute_type = "FLOAT_COLOR"
        created = collection.new(name=element_name, type=attribute_type, domain="CORNER")
        try:
            index = collection.find(element_name)
            if index is not None and index >= 0:
                collection.active_color_index = index
        except Exception:
            pass
        print("当前obj [" + str(getattr(obj, "name", "?")) + "] 缺少游戏渲染所需的COLOR: [" + element_name + "]，已自动补全")
        return created

    @staticmethod
    def check_and_verify_attributes(obj:bpy.types.Object, d3d11_game_type:D3D11GameType):
        '''
        校验并补全部分元素
        COLOR
        TEXCOORD、TEXCOORD1、TEXCOORD2、TEXCOORD3
        '''
        original_active_uv = obj.data.uv_layers.active

        for d3d11_element_name in d3d11_game_type.OrderedFullElementList:
            d3d11_element = d3d11_game_type.ElementNameD3D11ElementDict[d3d11_element_name]
            # 校验并补全所有COLOR的存在
            if d3d11_element_name.startswith("COLOR"):
                ObjBufferHelper.ensure_color_attribute(obj, d3d11_element_name, d3d11_element)
            
            # 校验TEXCOORD是否存在
            if d3d11_element_name.startswith("TEXCOORD"):
                if d3d11_element_name + ".xy" not in obj.data.uv_layers:
                    if len(obj.data.uv_layers) == 1 and d3d11_element_name == "TEXCOORD":
                        existing_uv = obj.data.uv_layers[0]
                        if not existing_uv.name.startswith("TEXCOORD"):
                            existing_uv.name = d3d11_element_name + ".xy"
                            print("当前obj ["+ obj.name +"] 只有一个UV层，已自动改名为: ["+ d3d11_element_name + ".xy" + "]")
                        else:
                            obj.data.uv_layers.new(name=d3d11_element_name + ".xy")
                            print("当前obj ["+ obj.name +"] 缺少游戏渲染所需的TEXCOORD: ["+ d3d11_element_name + ".xy" + "]，已自动补全")
                    else:
                        obj.data.uv_layers.new(name=d3d11_element_name + ".xy")
                        print("当前obj ["+ obj.name +"] 缺少游戏渲染所需的TEXCOORD: ["+ d3d11_element_name + ".xy" + "]，已自动补全")

            if d3d11_element_name.startswith("BLENDINDICES"):
                if not obj.vertex_groups:
                    SSMTErrorUtils.raise_fatal("your object [" +obj.name + "] need at leat one valid Vertex Group, Please check if your model's Vertex Group is correct.")

        if original_active_uv and obj.data.uv_layers.active != original_active_uv:
            obj.data.uv_layers.active = original_active_uv

    @staticmethod
    def _allow_wide_blendindices_for_remap() -> bool:
        return (
            GlobalConfig.logic_name in (LogicName.WWMI, LogicName.NTEMI)
            and GlobalProterties.import_merged_vgmap()
        )

    @staticmethod
    def get_obj_data_model_list_by_draw_ib(ordered_draw_obj_data_model_list:list[DrawCallModel], draw_ib:str):
        '''
        只返回指定draw_ib的obj列表
        这个方法存在的目的是为了兼容鸣潮的MergedObj
        这里只是根据IB获取一下对应的obj列表,不需要额外计算其它东西,因为WWMI的逻辑是融合后计算。
        '''
        final_ordered_draw_obj_model_list:list[DrawCallModel] = []

        for obj_model in ordered_draw_obj_data_model_list:
            # 只统计给定DrawIB的数据
            if obj_model.match_draw_ib != draw_ib:
                continue

            final_ordered_draw_obj_model_list.append(copy.deepcopy(obj_model))

        return final_ordered_draw_obj_model_list


    @staticmethod
    def _parse_position(mesh_vertices, mesh_vertices_length, loop_vertex_indices, d3d11_element):
        vertex_coords = numpy.empty(mesh_vertices_length * 3, dtype=numpy.float32)
        # Follow WWMI-Tools: fetch the undeformed vertex coordinates and do
        # not apply mirroring or dtype conversion at extraction stage.
        # mesh_vertices.foreach_get('undeformed_co', vertex_coords)
        mesh_vertices.foreach_get('co', vertex_coords)
        return ObjBufferHelper._parse_position_from_vertex_coords(
            vertex_coords.reshape(-1, 3),
            loop_vertex_indices,
            d3d11_element,
        )

    @staticmethod
    def _parse_position_from_vertex_coords(vertex_coords, loop_vertex_indices, d3d11_element):
        positions = numpy.asarray(vertex_coords, dtype=numpy.float32).reshape(-1, 3)[loop_vertex_indices]
        if d3d11_element.Format == 'R32G32B32A32_FLOAT':
            # If format expects 4 components, add a zero alpha column (float32)
            new_array = numpy.zeros((positions.shape[0], 4), dtype=numpy.float32)
            new_array[:, :3] = positions
            positions = new_array
        elif d3d11_element.Format == 'R16G16B16A16_FLOAT':
            # If format expects 4 components, add a W column (float16).
            # Expand the 3-component positions into the first 3 slots
            # and set the 4th (W) component to 1.0 (homogeneous coord).
            new_array = numpy.zeros((positions.shape[0], 4), dtype=numpy.float16)
            new_array[:, :3] = positions.astype(numpy.float16)
            new_array[:, 3] = numpy.ones(positions.shape[0], dtype=numpy.float16)
            positions = new_array
        return positions

    @staticmethod
    def _invalid_surface_directions(vectors):
        # Blender fills missing imported attributes with zero when joining meshes.
        return ~numpy.isfinite(vectors).all(axis=1) | numpy.all(vectors == 0, axis=1)

    @staticmethod
    def _parse_normal(
        mesh_loops,
        mesh_loops_length,
        d3d11_element,
        has_encoded_data=False,
        mesh=None,
    ):
        # 统一获取法线数据
        normals = None
        if mesh is not None:
            attr = mesh.attributes.get(IMPORTED_NORMAL_XYZ)
            if attr is not None and len(attr.data) == len(mesh.vertices):
                loop_vertex_indices = numpy.empty(mesh_loops_length, dtype=numpy.int32)
                mesh_loops.foreach_get("vertex_index", loop_vertex_indices)
                values = numpy.empty(len(mesh.vertices) * 3, dtype=numpy.float32)
                attr.data.foreach_get("vector", values)
                normals = values.reshape(-1, 3)[loop_vertex_indices]
        invalid = None if normals is None else ObjBufferHelper._invalid_surface_directions(normals)
        if normals is None or invalid.any():
            evaluated = numpy.empty((mesh_loops_length, 3), dtype=numpy.float32)
            corners = getattr(mesh, "corner_normals", None)
            if corners is not None and len(corners) == mesh_loops_length:
                corners.foreach_get("vector", evaluated.ravel())
            else:
                mesh_loops.foreach_get("normal", evaluated.ravel())
            if normals is None:
                normals = evaluated
            else:
                normals[invalid] = evaluated[invalid]
        normals = normals.ravel()

        if d3d11_element.Format == 'R16G16B16A16_FLOAT':
            result = numpy.ones(mesh_loops_length * 4, dtype=numpy.float32)
            result[0::4] = normals[0::3]
            result[1::4] = normals[1::3]
            result[2::4] = normals[2::3]
            result = result.reshape(-1, 4)

            result = result.astype(numpy.float16)
            return result

        elif d3d11_element.Format == 'R32G32B32A32_FLOAT':
            
            result = numpy.ones(mesh_loops_length * 4, dtype=numpy.float32)
            result[0::4] = normals[0::3]
            result[1::4] = normals[1::3]
            result[2::4] = normals[2::3]
            result = result.reshape(-1, 4)

            result = result.astype(numpy.float32)
            return result

        elif d3d11_element.Format == 'R8G8B8A8_SNORM':
            # WWMI 这里已经确定过NORMAL没问题

            result = numpy.ones(mesh_loops_length * 4, dtype=numpy.float32)
            result[0::4] = normals[0::3]
            result[1::4] = normals[1::3]
            result[2::4] = normals[2::3]
            
            if GlobalConfig.logic_name == LogicName.WWMI or GlobalConfig.logic_name == LogicName.NTEMI:
                bitangent_signs = numpy.empty(mesh_loops_length, dtype=numpy.float32)
                mesh_loops.foreach_get("bitangent_sign", bitangent_signs)
                result[3::4] = bitangent_signs * -1
                # print("Unreal: Set NORMAL.W to bitangent_sign")
            
            result = result.reshape(-1, 4)

            return FormatUtils.convert_4x_float32_to_r8g8b8a8_snorm(result)


        elif d3d11_element.Format == 'R8G8B8A8_UNORM':
            # 因为法线数据是[-1,1]如果非要导出成UNORM，那一定是进行了归一化到[0,1]
            
            result = numpy.ones(mesh_loops_length * 4, dtype=numpy.float32)
            

            # 燕云十六声的最后一位w固定为0
            if GlobalConfig.logic_name == LogicName.YYSLS:
                result = numpy.zeros(mesh_loops_length * 4, dtype=numpy.float32)
                
            result[0::4] = normals[0::3]
            result[1::4] = normals[1::3]
            result[2::4] = normals[2::3]
            result = result.reshape(-1, 4)

            # 归一化 (此处感谢 球球 的代码开发)
            def DeConvert(nor):
                return (nor + 1) * 0.5

            for i in range(len(result)):
                result[i][0] = DeConvert(result[i][0])
                result[i][1] = DeConvert(result[i][1])
                result[i][2] = DeConvert(result[i][2])

            return FormatUtils.convert_4x_float32_to_r8g8b8a8_unorm(result)

        elif d3d11_element.Format == "R32_UINT" and GlobalConfig.logic_name == LogicName.EFMI:
            raw_normals = normals.reshape(-1, 3)
            tangents = numpy.empty(mesh_loops_length * 3, dtype=numpy.float32)
            mesh_loops.foreach_get("tangent", tangents)
            tangents = tangents.reshape(-1, 3)

            bitangent_signs = numpy.empty(mesh_loops_length, dtype=numpy.float32)
            mesh_loops.foreach_get("bitangent_sign", bitangent_signs)

            new_normals = TBNCodec.encode_efmi_tools_r32_uint_from_tbn(
                raw_normals,
                tangents,
                bitangent_signs,
                flip_texcoord_v=True,
                flip_bitangent_sign=True,
            ).reshape(-1, 1)
            
            return new_normals
        else:
            # 将一维数组 reshape 成 (mesh_loops_length, 3) 形状的二维数组
            result = normals.reshape(-1, 3)

            return result

    @staticmethod
    def _parse_tangent(mesh_loops, mesh_loops_length, d3d11_element, mesh=None):
        result = numpy.empty(mesh_loops_length * 4, dtype=numpy.float32)

        imported_xyz = None
        imported_w = None
        if mesh is not None:
            xyz_attr = mesh.attributes.get(IMPORTED_TANGENT_XYZ)
            w_attr = mesh.attributes.get(IMPORTED_TANGENT_W)
            if (
                xyz_attr is not None
                and w_attr is not None
                and len(xyz_attr.data) == len(mesh.vertices)
                and len(w_attr.data) == len(mesh.vertices)
            ):
                loop_vertex_indices = numpy.empty(mesh_loops_length, dtype=numpy.int32)
                mesh_loops.foreach_get("vertex_index", loop_vertex_indices)
                xyz = numpy.empty(len(mesh.vertices) * 3, dtype=numpy.float32)
                xyz_attr.data.foreach_get("vector", xyz)
                xyz = xyz.reshape(-1, 3)
                imported_xyz = xyz[loop_vertex_indices]
                w = numpy.empty(len(mesh.vertices), dtype=numpy.float32)
                w_attr.data.foreach_get("value", w)
                imported_w = w[loop_vertex_indices]

        valid_imported = None
        if imported_xyz is not None:
            valid_imported = (
                ~ObjBufferHelper._invalid_surface_directions(imported_xyz)
                & numpy.isfinite(imported_w)
            )
        if valid_imported is None or not valid_imported.all():
            tangents = numpy.empty((mesh_loops_length, 3), dtype=numpy.float32)
            mesh_loops.foreach_get("tangent", tangents.ravel())
            if valid_imported is not None:
                tangents[valid_imported] = imported_xyz[valid_imported]
        else:
            tangents = imported_xyz
        tangents = tangents.ravel()

        # 将切线分量放置到输出数组中
        result[0::4] = tangents[0::3]  # x 分量
        result[1::4] = tangents[1::3]  # y 分量
        result[2::4] = tangents[2::3]  # z 分量

        if GlobalConfig.logic_name == LogicName.YYSLS:
            # 燕云十六声的TANGENT.w固定为1
            tangent_w = numpy.ones(mesh_loops_length, dtype=numpy.float32)
            result[3::4] = tangent_w
        elif GlobalConfig.logic_name == LogicName.WWMI or GlobalConfig.logic_name == LogicName.NTEMI:
            # Unreal引擎中这里要填写固定的1
            tangent_w = numpy.ones(mesh_loops_length, dtype=numpy.float32)
            result[3::4] = tangent_w
        else:
            # print("其它游戏翻转TANGENT的W分量")
            # 默认就设置BITANGENT的W翻转，大部分Unity游戏都要用到
            bitangent_signs = numpy.empty(mesh_loops_length, dtype=numpy.float32)
            mesh_loops.foreach_get("bitangent_sign", bitangent_signs)
            # XXX 将副切线符号乘以 -1
            # 这里翻转（翻转指的就是 *= -1）是因为如果要确保Unity游戏中渲染正确，必须翻转TANGENT的W分量
            bitangent_signs *= -1
            result[3::4] = bitangent_signs  # w 分量 (副切线符号)
        if valid_imported is not None:
            tangent_signs = result[3::4]
            tangent_signs[valid_imported] = imported_w[valid_imported]
        # 重塑 output_tangents 成 (mesh_loops_length, 4) 形状的二维数组
        result = result.reshape(-1, 4)

        if d3d11_element.Format == 'R16G16B16A16_FLOAT':
            result = result.astype(numpy.float16)

        elif d3d11_element.Format == 'R8G8B8A8_SNORM':
            # print("WWMI TANGENT To SNORM")
            result = FormatUtils.convert_4x_float32_to_r8g8b8a8_snorm(result)

        elif d3d11_element.Format == 'R8G8B8A8_UNORM':
            result = FormatUtils.convert_4x_float32_to_r8g8b8a8_unorm(result)
        
        # 第五人格格式
        elif d3d11_element.Format == "R32G32B32_FLOAT":
            result = numpy.empty(mesh_loops_length * 3, dtype=numpy.float32)

            result[0::3] = tangents[0::3]  # x 分量
            result[1::3] = tangents[1::3]  # y 分量
            result[2::3] = tangents[2::3]  # z 分量

            result = result.reshape(-1, 3)
        
        # 燕云十六声格式
        elif d3d11_element.Format == 'R16G16B16A16_SNORM':
            result = FormatUtils.convert_4x_float32_to_r16g16b16a16_snorm(result)
        
        return result

    @staticmethod
    def _parse_binormal(mesh_loops, mesh_loops_length, d3d11_element):
        result = numpy.empty(mesh_loops_length * 4, dtype=numpy.float32)

        # 使用 foreach_get 批量获取切线和副切线符号数据
        binormals = numpy.empty(mesh_loops_length * 3, dtype=numpy.float32)
        mesh_loops.foreach_get("bitangent", binormals)
        
        if GlobalConfig.logic_name == LogicName.WWMI or GlobalConfig.logic_name == LogicName.NTEMI:
            # 鸣潮逆向翻转：Binormal (-x, -y, z)
            binormals[0::3] *= -1
            binormals[1::3] *= -1

        # 将切线分量放置到输出数组中
        # BINORMAL全部翻转即可得到和YYSLS游戏中一样的效果。
        result[0::4] = binormals[0::3]  # x 分量
        result[1::4] = binormals[1::3]   # y 分量
        result[2::4] = binormals[2::3]  # z 分量
        binormal_w = numpy.ones(mesh_loops_length, dtype=numpy.float32)
        result[3::4] = binormal_w
        result = result.reshape(-1, 4)

        if d3d11_element.Format == 'R16G16B16A16_SNORM':
            #  燕云十六声格式
            result = FormatUtils.convert_4x_float32_to_r16g16b16a16_snorm(result)
            
        return result


    @staticmethod
    def _parse_color(mesh, mesh_loops_length, d3d11_element_name, d3d11_element):
        if hasattr(mesh, 'color_attributes') and d3d11_element_name in mesh.color_attributes:
            color_data = mesh.color_attributes[d3d11_element_name]
        elif d3d11_element_name in mesh.vertex_colors:
            color_data = mesh.vertex_colors[d3d11_element_name]
        else:
            color_data = None

        if color_data is not None:
            # Blender 颜色层读取接口统一返回 0-1 的 RGBA 浮点值，这里按 float32 处理中间结果。
            result = numpy.zeros(mesh_loops_length, dtype=(numpy.float32, 4))

            if hasattr(color_data, "domain"):
                result = sample_color_attribute_to_loops(mesh, color_data)
            else:
                result = read_color_attribute_data(color_data, mesh_loops_length)
            
            if d3d11_element.Format == 'R16G16B16A16_FLOAT':
                result = result.astype(numpy.float16)
            elif d3d11_element.Format == 'R16G16B16A16_UNORM':
                result = FormatUtils.convert_4x_float32_to_r16g16b16a16_unorm(result)
            elif d3d11_element.Format == "R16G16_UNORM":
                # 鸣潮的平滑法线存UV，在WWMI中的处理方式是转为R16G16_UNORM。
                # 但是这里很可能存在转换问题。
                result = FormatUtils.convert_2x_float32_to_r16g16_unorm(result[:, :2])
            # TODO 添加八面体压缩法线到R32_UINT的代码

            elif d3d11_element.Format == "R16G16_FLOAT":
                result = result[:, :2].astype(numpy.float16)
            elif d3d11_element.Format == 'R8G8B8A8_SNORM':
                result = FormatUtils.convert_4x_float32_to_r8g8b8a8_snorm(result)
            elif d3d11_element.Format == 'R8G8B8A8_UNORM':
                # 8bit UNORM 颜色流里游戏存的是 sRGB 编码后的字节，而 FLOAT_COLOR 层装的是
                # 线性值：混合类型 join 时 Blender 会把 BYTE_COLOR 提升成 FLOAT_COLOR 并把
                # sRGB 128 变成线性 0.21586，直接 ×255 量化就写成 55（实测 body.skin 变了色）。
                # 所以按存储类型先编码回 sRGB 再量化；BYTE_COLOR 层本来就对，原样通过。
                # 有符号与非颜色分支（SNORM、16bit、法线存 UV）不受影响。
                result = FormatUtils.convert_4x_float32_to_r8g8b8a8_unorm(
                    encode_color_for_unorm_target(color_data, result)
                )

            return result
        return None

    @staticmethod
    def _parse_texcoord(mesh, mesh_loops_length, d3d11_element_name, d3d11_element):
        result = None
        # TimerUtils.Start("GET TEXCOORD")
        for uv_name in ('%s.xy' % d3d11_element_name, '%s.zw' % d3d11_element_name):
            if uv_name in mesh.uv_layers:
                uvs_array = numpy.empty(mesh_loops_length ,dtype=(numpy.float32,2))
                mesh.uv_layers[uv_name].data.foreach_get("uv",uvs_array.ravel())
                uvs_array[:,1] = 1.0 - uvs_array[:,1]

                if d3d11_element.Format == 'R16G16_FLOAT':
                    uvs_array = uvs_array.astype(numpy.float16)
                
                # 重塑 uvs_array 成 (mesh_loops_length, 2) 形状的二维数组
                # uvs_array = uvs_array.reshape(-1, 2)

                result = uvs_array 
                if d3d11_element_name == "TEXCOORD" and GlobalConfig.logic_name == LogicName.ZZMI:
                    break
        # TimerUtils.End("GET TEXCOORD")
        return result

    @staticmethod
    def _parse_blendindices(blendindices_dict, d3d11_element, owner_label: str = ""):
        blendindices = blendindices_dict.get(d3d11_element.SemanticIndex,None)
        # print("blendindices: " + str(len(blendindices_dict)))
        # 如果当前索引对应的 blendindices 为 None，则使用索引0的数据并全部置0
        if blendindices is None:
            blendindices_0 = blendindices_dict.get(0, None)
            if blendindices_0 is not None:
                # 创建一个与 blendindices_0 形状相同的全0数组，保持相同的数据类型
                blendindices = numpy.zeros_like(blendindices_0)
            else:
                SSMTErrorUtils.raise_fatal(
                    f"{owner_label + ': ' if owner_label else ''}"
                    "Cannot find any valid BLENDINDICES data in this model, "
                    "Please check if your model's Vertex Group is correct."
                )
        # print(len(blendindices))
        component_count = int(
            d3d11_element.ByteWidth / numpy.dtype(FormatUtils.get_nptype_from_format(d3d11_element.Format)).itemsize
        )
        blendindices = FormatUtils.fit_component_width(blendindices, component_count)
        r16_integer_dtypes = {
            "R16_UINT": numpy.uint16,
            "R16G16_UINT": numpy.uint16,
            "R16G16B16A16_UINT": numpy.uint16,
            "R16_SINT": numpy.int16,
            "R16G16_SINT": numpy.int16,
            "R16G16B16A16_SINT": numpy.int16,
        }
        if d3d11_element.Format in r16_integer_dtypes:
            target_dtype = numpy.dtype(r16_integer_dtypes[d3d11_element.Format])
            limits = numpy.iinfo(target_dtype)
            min_index = int(numpy.min(blendindices, initial=0))
            max_index = int(numpy.max(blendindices, initial=0))
            if min_index < int(limits.min) or max_index > int(limits.max):
                SSMTErrorUtils.raise_fatal(
                    f"BLENDINDICES 超出 {d3d11_element.Format} 可承载范围: "
                    f"min={min_index}, max={max_index}"
                )
            return blendindices.astype(target_dtype)
        elif d3d11_element.Format == "R32G32B32A32_SINT":
            return blendindices
        elif d3d11_element.Format == "R32G32B32A32_UINT":
            return blendindices
        elif d3d11_element.Format == "R32G32_UINT":
            return blendindices[:, :2]
        elif d3d11_element.Format == "R32G32_SINT":
            return blendindices[:, :2]
        elif d3d11_element.Format == "R32_UINT":
            return blendindices[:, :1]
        elif d3d11_element.Format == "R32_SINT":
            return blendindices[:, :1]
        elif d3d11_element.Format == 'R8G8B8A8_SNORM':
            return FormatUtils.convert_4x_float32_to_r8g8b8a8_snorm(blendindices)
        elif d3d11_element.Format == 'R8G8B8A8_UNORM':
            return FormatUtils.convert_4x_float32_to_r8g8b8a8_unorm(blendindices)
        elif d3d11_element.Format == 'R8G8B8A8_UINT':
            min_index = int(numpy.min(blendindices, initial=0))
            max_index = int(numpy.max(blendindices, initial=0))
            if min_index < 0 or max_index > 255:
                if ObjBufferHelper._allow_wide_blendindices_for_remap():
                    if min_index < 0 or max_index > 65535:
                        raise Fatal(
                            "BLENDINDICES cannot be losslessly widened to uint16 "
                            f"(min={min_index}, max={max_index})."
                        )
                    return blendindices.astype(numpy.uint16)
                raise Fatal(
                    "BLENDINDICES is outside the R8G8B8A8_UINT range "
                    f"(min={min_index}, max={max_index})."
                )
            blendindices = blendindices.astype(numpy.uint8)
            return blendindices
            # print(original_elementname_data_dict[d3d11_element_name].dtype)
        elif d3d11_element.Format == "R8_UINT":
            min_index = int(numpy.min(blendindices, initial=0))
            max_index = int(numpy.max(blendindices, initial=0))
            if min_index < 0 or max_index > 255:
                if ObjBufferHelper._allow_wide_blendindices_for_remap():
                    if min_index < 0 or max_index > 65535:
                        raise Fatal(
                            "BLENDINDICES cannot be losslessly widened to uint16 "
                            f"(min={min_index}, max={max_index})."
                        )
                    return blendindices.astype(numpy.uint16)
                raise Fatal(
                    "BLENDINDICES is outside the R8_UINT range "
                    f"(min={min_index}, max={max_index})."
                )
            blendindices = blendindices.astype(numpy.uint8)

            return blendindices
            # print(original_elementname_data_dict[d3d11_element_name].dtype)
            # print("WWMI R8_UINT特殊处理")
        else:
            # print(blendindices.shape)
            SSMTErrorUtils.raise_fatal("未知的BLENDINDICES格式")

    @staticmethod
    def _parse_blendweight(blendweights_dict, d3d11_element):
        blendweights = blendweights_dict.get(d3d11_element.SemanticIndex, None)
        if blendweights is None:
            # print("遇到了为None的情况！")
            blendweights_0 = blendweights_dict.get(0, None)
            if blendweights_0 is not None:
                # 创建一个与 blendweights_0 形状相同的全0数组，保持相同的数据类型
                blendweights = numpy.zeros_like(blendweights_0)
            else:
                SSMTErrorUtils.raise_fatal("Cannot find any valid BLENDWEIGHT data in this model, Please check if your model's Vertex Group is correct.")
        # print(len(blendweights))
        component_count = int(
            d3d11_element.ByteWidth / numpy.dtype(FormatUtils.get_nptype_from_format(d3d11_element.Format)).itemsize
        )
        blendweights = FormatUtils.fit_component_width(blendweights, component_count)
        if d3d11_element.Format == "R32G32B32A32_FLOAT":
            return blendweights
        elif d3d11_element.Format == "R32G32_FLOAT":
            return blendweights[:, :2]
        elif d3d11_element.Format == 'R8G8B8A8_SNORM':
            # print("BLENDWEIGHT R8G8B8A8_SNORM")
            return FormatUtils.convert_4x_float32_to_r8g8b8a8_snorm(blendweights)
        elif d3d11_element.Format == 'R8G8B8A8_UNORM':
            # print("BLENDWEIGHT R8G8B8A8_UNORM")
            return FormatUtils.convert_4x_float32_to_r8g8b8a8_unorm_blendweights(blendweights)
        elif d3d11_element.Format == 'R16G16B16A16_FLOAT':
            return blendweights.astype(numpy.float16)
        elif d3d11_element.Format == 'R16G16B16A16_UNORM':
            return FormatUtils.convert_4x_float32_to_r16g16b16a16_unorm(blendweights)
        elif d3d11_element.Format == "R8_UNORM" and d3d11_element.ByteWidth == 8:
            # TimerUtils.Start("WWMI BLENDWEIGHT R8_UNORM特殊处理")
            blendweights = FormatUtils.convert_4x_float32_to_r8g8b8a8_unorm_blendweights(blendweights)
            # original_elementname_data_dict[d3d11_element_name] = blendweights
            print("WWMI R8_UNORM特殊处理")
            # TimerUtils.End("WWMI BLENDWEIGHT R8_UNORM特殊处理")
            return blendweights

        else:
            print(blendweights.shape)
            SSMTErrorUtils.raise_fatal("未知的BLENDWEIGHTS格式")

    @staticmethod
    def _parse_encodeddata(mesh_loops, mesh_loops_length, d3d11_element):
        """EFMI ENCODEDDATA 元素导出：将 loop 级 TBN 打包为 R10G10B10A2_UINT。

        与 EFMI-Tools 参考实现一致：flip_texcoord_v 时切线取反、flip_bitangent_sign 时符号取反，
        然后 octahedral 法线 + 角度切线 + 符号位打包为 R32_UINT。
        """
        normals = numpy.empty(mesh_loops_length * 3, dtype=numpy.float32)
        mesh_loops.foreach_get("normal", normals)
        normals = normals.reshape(-1, 3)

        tangents = numpy.empty(mesh_loops_length * 3, dtype=numpy.float32)
        mesh_loops.foreach_get("tangent", tangents)
        tangents = tangents.reshape(-1, 3)

        bitangent_signs = numpy.empty(mesh_loops_length, dtype=numpy.float32)
        mesh_loops.foreach_get("bitangent_sign", bitangent_signs)

        encoded = TBNCodec.encode_efmi_tools_r32_uint_from_tbn(
            normals,
            tangents,
            bitangent_signs,
            flip_texcoord_v=True,
            flip_bitangent_sign=True,
        )
        return encoded.reshape(-1, 1)

    @staticmethod
    def parse_elementname_data_dict(
        mesh: bpy.types.Mesh,
        d3d11_game_type: D3D11GameType,
        vertex_group_names=None,
        object_name: str = "",
    ):
        '''
        - 注意这里是从mesh.loops中获取数据，而不是从mesh.vertices中获取数据
        - 所以后续使用的时候要用mesh.loop里的索引来进行获取数据
        - 【t66】`vertex_group_names` = `[vg.name for vg in obj.vertex_groups]`：
          BLENDINDICES 的槽号按**组名**解析（组名 = 全局槽的十进制写法）；不传 ⇒ 旧行为。
        '''

        original_elementname_data_dict: dict = {}

        mesh_loops = mesh.loops
        mesh_loops_length = len(mesh.loops)
        mesh_vertices = mesh.vertices
        mesh_vertices_length = len(mesh.vertices)

        loop_vertex_indices = numpy.empty(mesh_loops_length, dtype=int)
        mesh_loops.foreach_get("vertex_index", loop_vertex_indices)

        # 预设的权重个数，也就是每个顶点组受多少个权重影响
        blend_size = 4

        if GlobalConfig.logic_name == LogicName.WWMI or GlobalConfig.logic_name == LogicName.NTEMI:
            blend_size = d3d11_game_type.get_blendindices_count_wwmi()

        normalize_weights = "Blend" in d3d11_game_type.OrderedCategoryNameList

        # normalize_weights = False
        if (
            LogicName.is_zzmi_family(GlobalConfig.logic_name)
            and GlobalProterties.import_merged_vgmap()
        ):
            # ZZMI 合并对象可把 BI4/BI8/BI16 混在同一个 Blender Mesh 中。
            # 目标 IB 的 BLENDINDICES 通道数就是最终 ABI。
            # 2026-09-21 用户裁定（修正旧口径）：与其它所有游戏路径（v1 / v3 /
            # v4_fast）一致——顶点骨骼影响数超过目标槽位数时**自动降宽**（保留权重
            # 最强的 N 个 + 重新归一化，见
            # `VertexGroupUtils.get_blendweights_blendindices_for_layout`），不再
            # Fatal 中断导出：权重平滑会制造大量极小尾巴，逐个顶点人工检查不现实。
            # 影响损失改为**汇总后大声报告**（超限顶点数 / 最大丢弃权重占比 / 样本
            # 顶点索引），占比过大时提示回 Blender 修权重。
            blend_index_element = next(
                (
                    element
                    for element in getattr(d3d11_game_type, "D3D11ElementList", [])
                    if str(getattr(element, "SemanticName", "") or "").upper()
                    == "BLENDINDICES"
                ),
                None,
            )
            if blend_index_element is not None:
                try:
                    scalar_size = numpy.dtype(
                        FormatUtils.get_nptype_from_format(blend_index_element.Format)
                    ).itemsize
                    blend_size = max(
                        1,
                        int(blend_index_element.ByteWidth) // max(scalar_size, 1),
                    )
                except (TypeError, ValueError, ZeroDivisionError):
                    blend_size = 4
            # 统计（在调用提取函数**之前**，与提取函数同一口径：weight > 0 且有限）：
            # 全网格最大有效影响数 M、超限顶点数、最大丢弃权重占比、样本顶点索引。
            blendweights_dict, blendindices_dict, diagnostics = VertexGroupUtils.get_blend_layout_with_diagnostics(
                mesh, blend_size, normalize_weights=True, loop_vertex_indices=loop_vertex_indices)
            max_effective_influences = diagnostics['max_influences']
            over_slot_vertices = diagnostics['over_vertices']
            max_dropped_fraction = diagnostics['max_dropped']
            sample_offenders = diagnostics['samples']
            if over_slot_vertices:
                target_format = (
                    str(getattr(blend_index_element, "Format", "") or "unknown")
                    if blend_index_element is not None
                    else "unknown"
                )
                try:
                    target_byte_width = int(
                        getattr(blend_index_element, "ByteWidth", 0) or 0
                    ) if blend_index_element is not None else 0
                except (TypeError, ValueError):
                    target_byte_width = 0
                print(
                    "[ZZMI骨骼合并] !!! 顶点影响数超过目标 BLENDINDICES 槽位，"
                    f"已自动降宽（保留最强 N={blend_size} 个 + 重新归一化）："
                    f"对象 [{getattr(mesh, 'name', '') or '<unnamed mesh>'}]，"
                    f"超限顶点 {over_slot_vertices}/{len(mesh_vertices)} 个，"
                    f"最大丢弃权重占比 {max_dropped_fraction * 100:.2f}%，"
                    f"样本顶点索引 {sample_offenders}（最大有效影响数 M="
                    f"{max_effective_influences}；目标 Format={target_format} / "
                    f"ByteWidth={target_byte_width}）"
                )
                if max_dropped_fraction >= 0.05:
                    print(
                        "[ZZMI骨骼合并] !!! 上述丢弃占比偏大（≥5%）：这些顶点重新"
                        "归一化后蒙皮位置会偏移；请回 Blender 选中该物体执行 "
                        "清理 Clean → 限制总影响数 Limit Total = "
                        f"{blend_size} → 归一化 All 修好权重后再导出"
                        "（合并骨骼下组名 = 全局骨骼 id，不要靠删组解决）。"
                    )
        elif GlobalConfig.logic_name == LogicName.WWMI or GlobalConfig.logic_name == LogicName.NTEMI:
            # print("鸣潮专属测试版权重处理：")
            blendweights_dict, blendindices_dict = VertexGroupUtils.get_blendweights_blendindices_v4_fast(mesh=mesh,normalize_weights = normalize_weights,blend_size=blend_size)

        elif GlobalConfig.logic_name == LogicName.SnowBreak:
            print("尘白禁区权重处理")
            blendweights_dict, blendindices_dict = VertexGroupUtils.get_blendweights_blendindices_v4_fast(mesh=mesh,normalize_weights = normalize_weights,blend_size=blend_size)
        else:
            # 【t66 门控】按组名解析槽号**只对 HIMI 生效**：其它逻辑一律传 None
            # ⇒ `resolve_group_slot` 不被调用，BLENDINDICES 仍是「`object.vertex_groups`
            # 列表序号」= 本次改动之前的逐字节行为。
            # 机制与单测保留（`utils/vertexgroup_utils.resolve_group_slot`），待某个游戏
            # 验证过「组名 = 全局骨骼槽」口径之后再逐个放开。
            if GlobalConfig.logic_name != getattr(LogicName, "HIMI", "HIMI"):
                vertex_group_names = None
            blendweights_dict, blendindices_dict = VertexGroupUtils.get_blendweights_blendindices_v3(
                mesh=mesh,
                normalize_weights=normalize_weights,
                vertex_group_names=vertex_group_names,   # 【t66】按组名解析槽号（仅 HIMI）
                # 【t88】HIMI 合并路线 fail-closed：组名不是纯数字就终止导出，
                # 不再静默退回「取列表序号」——那条路会绑错骨而且没有任何提示。
                strict_group_names=vertex_group_names is not None,
                object_name=object_name,
            )


        # 检查是否存在 ENCODEDDATA 元素 (用于 EFMI 格式的 TBN 编码)
        has_encoded_data = 'ENCODEDDATA' in d3d11_game_type.ElementNameD3D11ElementDict

        # 对每一种Element都获取对应的数据
        for d3d11_element_name in d3d11_game_type.OrderedFullElementList:
            d3d11_element = d3d11_game_type.ElementNameD3D11ElementDict[d3d11_element_name]
            
            data = None

            if d3d11_element_name == 'POSITION':
                data = ObjBufferHelper._parse_position(mesh_vertices, mesh_vertices_length, loop_vertex_indices, d3d11_element)

            elif d3d11_element_name == 'NORMAL':
                if has_encoded_data and (GlobalConfig.logic_name == LogicName.EFMI ):
                    pass
                else:
                    data = ObjBufferHelper._parse_normal(
                        mesh_loops,
                        mesh_loops_length,
                        d3d11_element,
                        has_encoded_data,
                        mesh=mesh,
                    )

            elif d3d11_element_name == 'TANGENT':
                if has_encoded_data and (GlobalConfig.logic_name == LogicName.EFMI ):
                    pass
                else:
                    data = ObjBufferHelper._parse_tangent(
                        mesh_loops,
                        mesh_loops_length,
                        d3d11_element,
                        mesh=mesh,
                    )

            elif d3d11_element_name.startswith('BINORMAL'):
                if has_encoded_data and (GlobalConfig.logic_name == LogicName.EFMI):
                    pass
                else:
                    data = ObjBufferHelper._parse_binormal(mesh_loops, mesh_loops_length, d3d11_element)
            
            elif d3d11_element_name.startswith('COLOR'):
                data = ObjBufferHelper._parse_color(mesh, mesh_loops_length, d3d11_element_name, d3d11_element)

            elif d3d11_element_name.startswith('TEXCOORD') and d3d11_element.Format.endswith('FLOAT'):
                data = ObjBufferHelper._parse_texcoord(mesh, mesh_loops_length, d3d11_element_name, d3d11_element)
            
            elif d3d11_element_name.startswith('BLENDINDICES'):
                data = ObjBufferHelper._parse_blendindices(
                    blendindices_dict,
                    d3d11_element,
                    owner_label=str(getattr(mesh, "name", "") or ""),
                )
                
            elif d3d11_element_name.startswith('BLENDWEIGHT'):
                data = ObjBufferHelper._parse_blendweight(blendweights_dict, d3d11_element)

            elif d3d11_element_name.startswith('ENCODEDDATA'):
                if GlobalConfig.logic_name == LogicName.EFMI:
                    data = ObjBufferHelper._parse_encodeddata(
                        mesh_loops,
                        mesh_loops_length,
                        d3d11_element,
                    )
                else:
                    print(f"警告: ENCODEDDATA 元素仅在 EFMI 格式中支持，当前游戏类型: {GlobalConfig.logic_name}")
                    data = None

            if data is not None:
                original_elementname_data_dict[d3d11_element_name] = data

        return original_elementname_data_dict


    @classmethod
    def convert_to_element_vertex_ndarray(
        cls,
        d3d11_game_type:D3D11GameType, 
        mesh:bpy.types.Mesh,
        original_elementname_data_dict:dict,
        final_elementname_data_dict:dict):

        total_structured_dtype:numpy.dtype = d3d11_game_type.get_total_structured_dtype()

        # Create the element array with the original dtype (matching ByteWidth)
        element_vertex_ndarray = numpy.zeros(len(mesh.loops), dtype=total_structured_dtype)
        # For each expected element, prefer the remapped/modified value in
        # `final_elementname_data_dict` if present; otherwise use the parsed
        # value from `original_elementname_data_dict`.
        for d3d11_element_name in d3d11_game_type.OrderedFullElementList:
            if d3d11_element_name in final_elementname_data_dict:
                data = final_elementname_data_dict[d3d11_element_name]
            else:
                data = original_elementname_data_dict.get(d3d11_element_name, None)

            if data is None:
                # Missing data is a fatal condition – better to raise so caller
                # can diagnose than to silently write zeros for an expected
                # element (which would corrupt downstream buffers).
                SSMTErrorUtils.raise_fatal(f"Missing element data for '{d3d11_element_name}' when packing vertex ndarray")

            target_dtype = element_vertex_ndarray.dtype[d3d11_element_name].base
            if (
                d3d11_element_name == "BLENDINDICES"
                and target_dtype == numpy.uint8
                and isinstance(data, numpy.ndarray)
                and data.size != 0
            ):
                # 合并模式（EFMI import_merged_vgmap / WWMI/NTEMI remap）下
                # BLENDINDICES 必须是 R16 全局骨骼 id；仍为 uint8 = widen 未生效
                # 的静默截断路径（全局 id > 255 被截断、运行时按 16 位读取错位
                # → 权重爆炸）。这里大声失败，禁止输出坏 mod。
                if (
                    GlobalConfig.logic_name == LogicName.EFMI
                    and GlobalProterties.import_merged_vgmap()
                    and bool(getattr(d3d11_game_type, "GPU_PreSkinning", False))
                ) or (
                    GlobalConfig.logic_name in (LogicName.WWMI, LogicName.NTEMI)
                    and GlobalProterties.import_merged_vgmap()
                ):
                    SSMTErrorUtils.raise_fatal(
                        "BLENDINDICES 仍以 uint8 打包（合并模式必须 R16 全局骨骼 id）："
                        f"max_index={int(numpy.max(data, initial=0))}。请检查 "
                        "widen_blendindices 是否在导出前生效（重新导入或重新执行合并骨骼预处理）。"
                    )
                max_index = int(numpy.max(data, initial=0))
                if max_index > 255:
                    SSMTErrorUtils.raise_fatal(
                        "BLENDINDICES remap did not reduce values to uint8 range before packing "
                        f"(max={max_index})."
                    )
            element_vertex_ndarray[d3d11_element_name] = data
        
        return element_vertex_ndarray
    

    @staticmethod
    def _deduplicate_active_columns(
        dtype:numpy.dtype,
        deduplicate_element_set:'set | None',
        row_size:int) -> numpy.ndarray:
        '''
        返回长度 = row_size 的 bool 数组：True 表示该字节列参与去重键比较。

        deduplicate_element_set 为 None 时全部字段参与；否则按 `_filter_deduplicate_elements`
        的同一条规则（字段名等于、或以元素类型开头）匹配，例如 TEXCOORD 匹配 TEXCOORD1。
        '''
        active = numpy.zeros(row_size, dtype=bool)
        offset = 0
        for field_name in dtype.names:
            field_size = dtype.fields[field_name][0].itemsize
            if deduplicate_element_set is None:
                keep = True
            else:
                keep = any(
                    field_name == element_type or field_name.startswith(element_type)
                    for element_type in deduplicate_element_set
                )
            if keep:
                active[offset:offset + field_size] = True
            offset += field_size
        return active

    @staticmethod
    def _seam_marked_loop_mask(mesh, loop_vertex_indices:numpy.ndarray):
        '''
        返回每个 loop 是否属于「标记了缝合边的顶点」；没有可用标记时返回 None。

        判定口径（宽）：顶点只要有一条邻接边 use_seam=True 就算被标记。
        读不到边信息（没有边/没有该属性）时返回 None，调用方按「没有标记」处理。
        '''
        try:
            edges = getattr(mesh, "edges", None)
            if edges is None:
                return None
            n_edges = len(edges)
            if n_edges == 0:
                return None

            seam_flags = numpy.zeros(n_edges, dtype=bool)
            try:
                edges.foreach_get("use_seam", seam_flags)
            except Exception:
                seam_flags[:] = [bool(getattr(edge, "use_seam", False)) for edge in edges]
            if not bool(seam_flags.any()):
                return None

            vertices = getattr(mesh, "vertices", None)
            n_vertices = len(vertices) if vertices is not None else 0
            if n_vertices == 0:
                return None

            edge_vertices = numpy.empty(n_edges * 2, dtype=int)
            edges.foreach_get("vertices", edge_vertices)
            marked_vertices = numpy.zeros(n_vertices, dtype=bool)
            marked_vertices[edge_vertices.reshape(n_edges, 2)[seam_flags].ravel()] = True
            return marked_vertices[loop_vertex_indices]
        except Exception as exc:  # noqa: BLE001 - 标记读不到就退化成正常通道，绝不弄坏导出
            print(f"顶点去重精度控制: 读取缝合边标记失败，特殊通道已跳过（{exc}）")
            return None

    @staticmethod
    def calc_index_vertex_buffer_wwmi_v2(
        mesh:bpy.types.Mesh, 
        element_vertex_ndarray:numpy.ndarray, 
        dtype:numpy.dtype,
        d3d11_game_type:D3D11GameType,
        deduplicate_element_set:set = None,
        deduplicate_ignore_vertex_id: bool = True,
        flip_triangles: bool = True):
        '''
        - 用 numpy 将结构化顶点视图为一行字节，避免逐顶点 bytes() 与 dict 哈希。
        - 使用 numpy.unique(..., axis=0, return_index=True, return_inverse=True) 在 C 层完成唯一化与逆映射。
        - 仅在构建 per-polygon IB 时使用少量 Python 切片，整体效率大幅提高。
        - 当 structured dtype 非连续时，内部会做一次拷贝（ascontiguousarray）；通常开销小于逐顶点哈希开销。
        - deduplicate_element_set: 指定哪些元素类型参与去重，为 None 时使用全部元素。
          元素类型名称包括: POSITION, NORMAL, TANGENT, BINORMAL, TEXCOORD, COLOR, BLENDWEIGHT, BLENDINDICES
        - deduplicate_ignore_vertex_id: 缝合标记特殊通道的总开关，默认开启。
          只有「至少有一条邻接边被标记为缝合边(use_seam)」的顶点走特殊通道：键只取
          POSITION + TEXCOORD，且不含顶点编号 ⇒ 编号不同、位置与 UV 逐字节相同的标记
          顶点会合并成一行。没被标记的顶点照旧走正常通道（勾选的类型 + 顶点编号）。
          网格上一条缝合边都没标时，本开关不改变任何输出（等于没有这套机制）。
          合并行仍取首个出现 loop 的完整数据与顶点编号，下游 shapekey/骨骼映射语义不变。
        '''

        # (1) loop -> vertex mapping
        loops = mesh.loops
        n_loops = len(loops)
        loop_vertex_indices = numpy.empty(n_loops, dtype=int)
        loops.foreach_get("vertex_index", loop_vertex_indices)

        # (2) 将 element_vertex_ndarray 保证为连续，并视为 (n_loops, row_bytes) uint8 矩阵
        vb = numpy.ascontiguousarray(element_vertex_ndarray)
        row_size = vb.dtype.itemsize
        try:
            row_bytes = vb.view(numpy.uint8).reshape(n_loops, row_size)
        except Exception:
            raw = vb.tobytes()
            row_bytes = numpy.frombuffer(raw, dtype=numpy.uint8).reshape(n_loops, row_size)

        # 两个通道：
        #   * 特殊通道：顶点只要有一条邻接边被标记为缝合边(use_seam)，键就只取
        #     POSITION + TEXCOORD 且不含顶点编号（编号用 0xFFFFFFFF 占位，保持键宽度统一）
        #     ⇒ 编号不同、位置与 UV 相同的标记顶点会合并成一行。
        #   * 正常通道：键 = 勾选的类型 + 顶点编号（历史行为，与改动前逐字节一致）。
        seam_loop_mask = None
        if deduplicate_ignore_vertex_id:
            seam_loop_mask = ObjBufferHelper._seam_marked_loop_mask(mesh, loop_vertex_indices)
            if seam_loop_mask is not None and not bool(seam_loop_mask.any()):
                seam_loop_mask = None

        if seam_loop_mask is not None:
            active_columns = ObjBufferHelper._deduplicate_active_columns(
                dtype, deduplicate_element_set, row_size
            )
            if deduplicate_element_set is not None and not bool(active_columns.any()):
                SSMTErrorUtils.raise_fatal(
                    "顶点去重精度控制: 没有选择任何元素参与去重，请至少选择一个元素类型"
                )
            # 标记顶点只比 位置 + UV：把这些之外的所有列清零，等价于「不参与比较」
            special_columns = ObjBufferHelper._deduplicate_active_columns(
                dtype, {"POSITION", "TEXCOORD"}, row_size
            )
            filtered_row_bytes = numpy.array(row_bytes, copy=True)
            dropped_columns = numpy.nonzero(~active_columns)[0]
            if dropped_columns.size:
                filtered_row_bytes[:, dropped_columns] = 0
            special_rows = numpy.nonzero(seam_loop_mask)[0]
            if special_rows.size:
                # 标记顶点：先整行清零，再把 位置 + UV 的列按原始字节填回去
                filtered_row_bytes[seam_loop_mask] = 0
                special_columns_idx = numpy.nonzero(special_columns)[0]
                if special_columns_idx.size:
                    special_slice = numpy.ix_(special_rows, special_columns_idx)
                    filtered_row_bytes[special_slice] = row_bytes[special_slice]
            filtered_row_size = row_size
            vid_values = loop_vertex_indices.astype(numpy.uint32)
            vid_values[seam_loop_mask] = numpy.uint32(0xFFFFFFFF)
            vid_bytes = vid_values.view(numpy.uint8).reshape(n_loops, 4)
            vid_width = 4
        else:
            # 根据 deduplicate_element_set 过滤参与去重的元素
            if deduplicate_element_set is not None:
                filtered_row_bytes = ObjBufferHelper._filter_deduplicate_elements(
                    element_vertex_ndarray, dtype, d3d11_game_type, deduplicate_element_set, n_loops
                )
            else:
                filtered_row_bytes = row_bytes
            filtered_row_size = filtered_row_bytes.shape[1]
            vid_bytes = loop_vertex_indices.astype(numpy.uint32).view(numpy.uint8).reshape(n_loops, 4)
            vid_width = 4

        # WWMI-Tools deduplicates loop rows including the loop's VertexId -> they
        # effectively perform uniqueness on loop attributes + VertexId treated as
        # a field. To replicate that reliably (preserving structured field layout
        # and alignment) we build a structured array that copies all existing
        # fields and appends a 'VERTEXID' uint32 field, then call numpy.unique on it.
        # Afterwards we select unique rows from the original `row_bytes` using
        # the indices returned by numpy.unique to preserve exact original layout.

        # 键 = [参与比较的字节 | 4 字节顶点编号]。顶点编号在特殊通道里是占位符，
        # 在正常通道里是真实编号，两种通道的键宽度一致。

        # Combine row bytes + vid bytes, but to make numpy.unique faster we pad the
        # combined row to a multiple of 8 bytes and view it as uint64 blocks.
        total_bytes = filtered_row_size + vid_width
        pad = (-total_bytes) % 8
        padded_width = total_bytes + pad

        if padded_width == 0:
            SSMTErrorUtils.raise_fatal(
                "顶点去重精度控制: 去重键宽度为 0（没有勾选任何参与去重的数据类型，"
                "又勾选了「无视顶点编号」），无法去重，请至少勾选一个数据类型"
            )

        # Allocate padded combined buffer and fill
        combined_padded = numpy.zeros((n_loops, padded_width), dtype=numpy.uint8)
        combined_padded[:, :filtered_row_size] = filtered_row_bytes
        if vid_bytes is not None:
            combined_padded[:, filtered_row_size:filtered_row_size+4] = vid_bytes

        native_unique = None
        try:
            from .export_geometry_native import stable_unique_rows
            native_unique = stable_unique_rows(combined_padded)
        except ImportError:
            pass
        if native_unique is not None:
            unique_first_indices_insertion, inverse = native_unique
        else:
            n_blocks = padded_width // 8
            combined_u64 = combined_padded.view(numpy.uint64).reshape(n_loops, n_blocks)
            dtype_descr = [(f'f{i}', numpy.uint64) for i in range(n_blocks)]
            structured = combined_u64.view(numpy.dtype(dtype_descr)).reshape(n_loops)
            _, unique_first_indices, inverse = numpy.unique(
                structured, return_index=True, return_inverse=True)
            order = numpy.argsort(unique_first_indices)
            new_id = numpy.empty_like(order)
            new_id[order] = numpy.arange(len(order), dtype=new_id.dtype)
            inverse = new_id[inverse]
            unique_first_indices_insertion = unique_first_indices[order]

        # Pick original unique rows from row_bytes using insertion-ordered indices
        # 注意：这里使用原始的 row_bytes 而不是 filtered_row_bytes，以保留完整数据
        unique_rows = row_bytes[unique_first_indices_insertion]

        # Expose the loop indices (first-occurrence loop indices) used to select
        # the unique rows. Callers can sample per-loop original arrays using
        # these indices to reconstruct per-unique-row original element values.
        unique_first_loop_indices = unique_first_indices_insertion

        # Reconstruct a structured ndarray of the unique element rows.
        # This lets callers access element fields by name for the unique
        # vertex set (useful for debugging or further processing).
        # Ensure the byte width matches the dtype itemsize.
        if unique_rows.shape[1] != dtype.itemsize:
            SSMTErrorUtils.raise_fatal(f"Unique row byte-size ({unique_rows.shape[1]}) does not match structured dtype itemsize ({dtype.itemsize})")

        n_unique = unique_rows.shape[0]
        unique_rows_contig = numpy.ascontiguousarray(unique_rows)
        try:
            # Zero-copy view where possible
            unique_element_vertex_ndarray = unique_rows_contig.view(dtype).reshape(n_unique)
        except Exception:
            # Fallback to a safe copy-based reconstruction
            unique_element_vertex_ndarray = numpy.frombuffer(unique_rows_contig.tobytes(), dtype=dtype).reshape(n_unique)

        # Expose for downstream use: structure-aligned unique vertex records
        # self.unique_element_vertex_ndarray = unique_element_vertex_ndarray

        # 构建 index -> original vertex id（使用每个 unique 行的第一个 loop 对应的 vertex）
        original_vertex_ids = loop_vertex_indices[unique_first_indices_insertion]
        index_vertex_id_dict = dict(enumerate(original_vertex_ids.astype(int).tolist()))

        # ------------------------------------------------------------------ #
        # 【t84】行↔源映射插桩（**只打日志、不改行为**）—— **只在 HIMI 打开**。
        # 它是排查 HIMI 合并导出行错位时的一次性诊断；留在通用路径上会让**每个游戏**
        # 的每一次导出都刷屏。其它逻辑保持改动前：一行都不打。
        # ------------------------------------------------------------------ #
        # 口径：
        #   * 去重键 = `numpy.unique(structured(vertex_id + element bytes))`（本函数 `:872-893`）
        #     ⇒ **同一个源顶点只要 element 字节不同就会各占一行** ⇒ 行数 > 源顶点数（本例 1321 vs 1298，
        #     多出的 23 = 那些"同顶点多变体"的顶点数）；
        #   * 每行的 `src_v` 就在这一行给出（`:934`）；`donor_row(r)` = 与第 r 行 `src_v` 相同的**最早**行
        #     （同顶点变体 ⇒ 位置按定义相同）；`ib_majority(r)` 需要源 IB 对照，由 `.dbg` 侧脚本给出。
        if GlobalConfig.logic_name == getattr(LogicName, "HIMI", "HIMI"):
            try:
                import numpy as _np

                _ids = original_vertex_ids.astype(int)
                _uniq, _counts = _np.unique(_ids, return_counts=True)
                _multi = _uniq[_counts > 1]
                # donor_row：同 src_v 的第一次出现行
                _donor = {}
                for _row, _vid in enumerate(_ids.tolist()):
                    _donor.setdefault(int(_vid), int(_row))
                print(
                    f"[t84-POS] loops={n_loops} rows(n_unique)={int(_ids.shape[0])} "
                    f"distinct_src_v={int(_uniq.shape[0])} extra_rows={int(_ids.shape[0]) - int(_uniq.shape[0])} "
                    f"multi_src_v={int(_multi.shape[0])}"
                )
                if _multi.size:
                    head = []
                    for _vid in _multi.tolist()[:8]:
                        rows = [int(r) for r in _np.nonzero(_ids == _vid)[0].tolist()]
                        head.append({"src_v": int(_vid), "rows": rows, "donor_row": _donor[int(_vid)]})
                    print(f"[t84-POS]   同顶点多变体（前 8）：{head}")
                # 行↔源映射表摘要（前 12 行 + 末 4 行）
                head_rows = [(int(r), int(v)) for r, v in enumerate(_ids.tolist()[:12])]
                tail_rows = [(int(r), int(v)) for r, v in
                             enumerate(_ids.tolist()[max(0, int(_ids.shape[0]) - 4):],
                                       start=max(0, int(_ids.shape[0]) - 4))]
                print(f"[t84-POS]   row→src_v 头={head_rows} 尾={tail_rows}")
            except Exception as _exc:  # noqa: BLE001 - 插桩绝不许弄坏导出
                print(f"[t84-POS] 插桩异常（已忽略）：{_exc}")

        # (4) 为每个 polygon 构建 IB（使用 inverse 映射）
        # inverse is already ordered by loops; concatenating polygon slices in
        # polygon order is equivalent to taking inverse in sequence.
        flattened_ib_arr = inverse.astype(numpy.int32)

        # (5) 按 category 从 unique_rows 切分 bytes 序列
        category_stride_dict = d3d11_game_type.get_real_category_stride_dict()
        category_buffer_dict = {}
        stride_offset = 0
        for cname, cstride in category_stride_dict.items():
            category_buffer_dict[cname] = unique_rows[:, stride_offset:stride_offset + cstride].flatten()
            stride_offset += cstride

        if flip_triangles:
            flat_arr = flattened_ib_arr
            if flat_arr.size % 3 == 0:
                ib = flat_arr.reshape(-1, 3)[:, ::-1].flatten().tolist()
            else:
                flipped = []
                iarr = flat_arr.tolist()
                for i in range(0, len(iarr), 3):
                    tri = iarr[i:i + 3]
                    flipped.extend(tri[::-1])
                ib = flipped
        else:
            ib = flattened_ib_arr.tolist()

        return ib, category_buffer_dict, index_vertex_id_dict, unique_element_vertex_ndarray,unique_first_loop_indices

    @staticmethod
    def _filter_deduplicate_elements(
        element_vertex_ndarray:numpy.ndarray,
        dtype:numpy.dtype,
        d3d11_game_type:D3D11GameType,
        deduplicate_element_set:set,
        n_loops:int) -> numpy.ndarray:
        '''
        根据 deduplicate_element_set 过滤参与去重的元素，返回过滤后的字节数组。
        deduplicate_element_set 中的元素名称会匹配以该名称开头的所有字段。
        例如: "TEXCOORD" 会匹配 "TEXCOORD", "TEXCOORD1", "TEXCOORD2" 等。
        '''
        fields_to_include = []
        for field_name in dtype.names:
            should_include = False
            for element_type in deduplicate_element_set:
                if field_name == element_type or field_name.startswith(element_type):
                    should_include = True
                    break
            if should_include:
                fields_to_include.append(field_name)
        
        if not fields_to_include:
            SSMTErrorUtils.raise_fatal("顶点去重精度控制: 没有选择任何元素参与去重，请至少选择一个元素类型")
        
        total_bytes = 0
        field_byte_ranges = []
        for field_name in dtype.names:
            field_dtype = dtype.fields[field_name][0]
            field_size = field_dtype.itemsize
            if field_name in fields_to_include:
                field_byte_ranges.append((total_bytes, total_bytes + field_size))
            total_bytes += field_size
        
        row_bytes = element_vertex_ndarray.view(numpy.uint8).reshape(n_loops, total_bytes)
        
        filtered_bytes_list = []
        for start, end in field_byte_ranges:
            filtered_bytes_list.append(row_bytes[:, start:end])
        
        if filtered_bytes_list:
            filtered_row_bytes = numpy.concatenate(filtered_bytes_list, axis=1)
        else:
            filtered_row_bytes = numpy.zeros((n_loops, 0), dtype=numpy.uint8)
        
        return filtered_row_bytes


    @staticmethod
    def _color_property_getter(name, default):
        """读全局属性，读不到/非法时回退 default（兼容老属性组与测试替身）。"""
        getter = getattr(GlobalProterties, name, None)
        if callable(getter):
            try:
                value = getter()
            except Exception:
                return default
            if isinstance(default, bool):
                return bool(value)
            try:
                value = float(value)
            except (TypeError, ValueError):
                return default
            if not numpy.isfinite(value):
                return default
            return value
        return default

    @staticmethod
    def _color_mode():
        """COLOR 写入方式：OFF / AVERAGE / PER_LOOP（来自 recalculate_color_mode）。"""
        getter = getattr(GlobalProterties, "recalculate_color_mode", None)
        if callable(getter):
            try:
                return str(getter() or "AVERAGE")
            except Exception:
                return "AVERAGE"
        return "AVERAGE"

    @staticmethod
    def _color_per_loop_enabled():
        return ObjBufferHelper._color_mode() == "PER_LOOP"

    @staticmethod
    def _color_alpha(obj):
        """写进 COLOR.a 的值 = 描边宽度旋钮（0.5 = 游戏原版；实测游戏原值中位 ≈ 118/255）。

        物体属性 3DMigoto:RecalculateCOLORWidth 优先，否则取全局 recalculate_color_width。
        """
        value = float(ObjBufferHelper._color_property_getter("recalculate_color_width", 0.5))
        try:
            override = obj.get("3DMigoto:RecalculateCOLORWidth", None)
            if override is not None:
                value = float(override)
        except Exception:
            pass
        if not numpy.isfinite(value):
            value = 0.5
        return max(0.0, min(1.0, value))

    @staticmethod
    def average_normal_color(obj,indexed_vertices,d3d11GameType:D3D11GameType,dtype):
        '''
        Nico: 算数平均归一化法线，HI3 2.0角色使用的方法

        游戏（HI3 2.0）约定：顶点 COLOR 通道存的不是装饰色，而是**单位长度**的逐位置平滑法线
        （rgb = n * 0.5 + 0.5，8bit），方向与同一缓冲的 NORMAL 列同空间；alpha 是逐顶点描边宽度遮罩
        （游戏自带数据里主值 128/255，另有 7~11% 顶点为 0 = 该处不描边）。

        ⇒ 本方法三步：
          ① 按位置算术平均 + 归一化：硬边处平均向量本身就短（90° 硬边只有 0.707），不归一化会让
             描边位移按 |mean| 缩水，8bit 量化误差还会被放大成方向噪声（实测最坏 |mean| = 0.03
             → 15°~30° 角度噪声）。若 recalculate_color_mode = PER_LOOP，则跳过平均、逐顶点直接用
             自己的 NORMAL。
          ② **不做坐标变换**：调用点在导出流水线的下游（临时物体已经烘焙了导出空间变换，
             common/submesh_model.py:353 → utils/export_utils.py:571），此刻的 loop 法线已在导出空间，
             与最终写进缓冲的 NORMAL 列同空间。对游戏原始 dump 实测：COLOR 与同一顶点 NORMAL 的中位
             夹角 13.9°（大件）/41.5°（头发），而对"同位置平均法线"只有 2.7°/18.4° —— 差别全部来自
             "平滑 vs 逐面"，不存在任何 107° 量级的刚性旋转；早期版本多乘的一次 mirrorX·Rx(-90)
             会让产物 COLOR 与游戏 COLOR 差 101°（描边左右颠倒 / 往模型里侧扩张）。
          ③ 编码 c=(n+1)/2；alpha = 描边宽度旋钮（recalculate_color_width，默认 0.5 = 游戏原版，
             物体属性 3DMigoto:RecalculateCOLORWidth 可覆盖）。alpha **不继承输入旧值** ——
             继承会把上一次写错的值污染到下一次。模式由 recalculate_color_mode 决定
             （OFF / AVERAGE / PER_LOOP）。
        '''
        if "COLOR" not in d3d11GameType.OrderedFullElementList:
            return indexed_vertices
        # 「COLOR 写入方式」是 HI3 2.0 专用：UI 只在 HIMI 模式绘制该栏
        # （blueprint/node_obj.py 的轮廓线修复栏）。此前这里不判游戏类型，而默认值是
        # AVERAGE，于是 ZZZ 等游戏的顶点色（游戏原生的光照/材质遮罩，实测身体部件只有
        # 6 种值）被这套"法线编码 + 描边宽度"覆盖（实测产物 7739 种值、alpha 恒 128），
        # 表现为角色属性界面等吃 COLOR 的 pass 光影质感错乱。
        # 非 HIMI 游戏如需该功能，仍可用物体属性 3DMigoto:RecalculateCOLOR 单独开启。
        allow_calc = False
        if obj.get("3DMigoto:RecalculateCOLOR", False):
            allow_calc = True
        elif GlobalProterties.recalculate_color() and GlobalConfig.logic_name == LogicName.HIMI:
            allow_calc = True
        if not allow_calc:
            return indexed_vertices

        # 写入前先确认物体本身有这个顶点色属性：没有就创建（否则"重算 COLOR"在物体上没有落点，
        # 表现就是"改了没反应"）。属性名取游戏类型里第一个 COLOR 元素，格式随该元素的 Format。
        elements = getattr(d3d11GameType, "ElementNameD3D11ElementDict", None) or {}
        for element_name in d3d11GameType.OrderedFullElementList:
            if str(element_name).startswith("COLOR"):
                ObjBufferHelper.ensure_color_attribute(obj, element_name, elements.get(element_name))
                break

        # 开始重计算COLOR
        TimerUtils.Start("Recalculate COLOR")

        # 不用担心这个转换的效率，速度非常快
        vb = bytearray()
        for vertex in indexed_vertices:
            vb += bytes(vertex)
        vb = numpy.frombuffer(vb, dtype = dtype)

        # 首先提取所有唯一的位置，并创建一个索引映射
        unique_positions, position_indices = numpy.unique(
            [tuple(val['POSITION']) for val in vb], 
            return_inverse=True, 
            axis=0
        )

        # 初始化累积法线和计数器为零
        accumulated_normals = numpy.zeros((len(unique_positions), 3), dtype=float)
        counts = numpy.zeros(len(unique_positions), dtype=int)

        # 累加法线并增加计数（这里假设vb是一个list）
        for i, val in enumerate(vb):
            accumulated_normals[position_indices[i]] += numpy.array(val['NORMAL'], dtype=float)
            counts[position_indices[i]] += 1

        # 对所有位置的法线求算术平均
        mask = counts > 0
        average_normals = numpy.zeros_like(accumulated_normals)
        average_normals[mask] = (accumulated_normals[mask] / counts[mask][:, None])

        # 归一化平均方向：游戏约定存单位向量，不归一化会让描边位移按 |mean| 缩水
        average_lengths = numpy.linalg.norm(average_normals, axis=1)
        unit_normals = numpy.zeros_like(average_normals)
        has_average_direction = mask & (average_lengths > 1e-6)
        unit_normals[has_average_direction] = (
            average_normals[has_average_direction] / average_lengths[has_average_direction][:, None]
        )

        # 退化位置（同位置的±法线完全抵消，例如零厚度发片）没有可用的平均方向：
        # 回退到该位置第一条非零的顶点法线——它仍是一条真实表面法线，
        # 比让方向塌成 8bit 量化噪声更接近原模型。
        degenerate_positions = mask & ~has_average_direction
        if degenerate_positions.any():
            source_normals = numpy.asarray(vb['NORMAL'], dtype=float)
            source_lengths = numpy.linalg.norm(source_normals, axis=1)
            usable_vertex_indices = numpy.flatnonzero(source_lengths > 1e-6)
            # 逆序写入 => 每个位置留下下标最小的那条可用法线
            first_usable_indices = numpy.full(len(unique_positions), -1, dtype=int)
            first_usable_indices[position_indices[usable_vertex_indices[::-1]]] = usable_vertex_indices[::-1]
            has_usable_normal = first_usable_indices >= 0
            fallback_normals = numpy.zeros_like(unit_normals)
            fallback_normals[has_usable_normal] = (
                source_normals[first_usable_indices[has_usable_normal]]
                / source_lengths[first_usable_indices[has_usable_normal]][:, None]
            )
            apply_fallback = degenerate_positions & has_usable_normal
            unit_normals[apply_fallback] = fallback_normals[apply_fallback]

        # 源数据里的 NaN/Inf 不允许污染输出：留零向量（解码后不产生位移）
        unit_normals[~numpy.isfinite(unit_normals)] = 0.0

        # 逐 loop 直写模式（开关，默认关）：头发这类带分裂法线/硬边的网格，按位置算术平均会把
        # 方向抹平；打开后每个导出顶点直接用自己的 NORMAL（同样归一化、同样做导出空间映射）。
        if ObjBufferHelper._color_per_loop_enabled():
            raw_normals = numpy.asarray(vb['NORMAL'], dtype=float)
            raw_lengths = numpy.linalg.norm(raw_normals, axis=1)
            usable = raw_lengths > 1e-6
            per_vertex_normals = numpy.zeros_like(raw_normals)
            per_vertex_normals[usable] = raw_normals[usable] / raw_lengths[usable][:, None]
            per_vertex_normals[~numpy.isfinite(per_vertex_normals)] = 0.0
        else:
            per_vertex_normals = unit_normals[position_indices]

        # COLOR 与 NORMAL 必须同空间：COLOR 是"平滑法线"，只比同一顶点的 NORMAL 平滑一点。
        # 这里**不做任何坐标变换** —— 导出流水线是"先把导出空间变换烘焙进临时物体，再装配缓冲"：
        #   common/submesh_model.py:353 _apply_export_rotation_for_logic（HIMI 绕 X −90°）
        #   → utils/export_utils.py:459/571 才 check_and_verify_attributes + average_normal_color
        # 所以此刻读到的 loop 法线已经在导出空间里了。早期版本在这里又乘了一次
        # mirrorX·Rx(-90)，等于对法线多转一次：实测产物 NORMAL 列与游戏 dump 的 NORMAL 中位夹角
        # 0.51°（同空间），而产物 COLOR 与游戏 COLOR 中位夹角 101.44° —— 描边方向因此整片错位
        # （表现为左右颠倒 / 往模型里侧扩张）。移除映射后 COLOR 与 NORMAL 列同向。

        # 轮廓线宽度 = COLOR.a（游戏那条描边 VS：外扩 = 归一化法线 × 常量 × COLOR.a）。
        # 唯一旋钮就是写进去的 alpha 值（默认 0.5 = 游戏原版，物体属性可覆盖）；
        # **不继承输入里的旧 alpha** —— 继承会把上一次写错的值污染到下一次（这个坑已经踩过）。
        alpha = int(max(0, min(255, round(ObjBufferHelper._color_alpha(obj) * 255.0))))

        # 编码 c = (n + 1) / 2，8bit
        normalized_normals = numpy.clip(
            numpy.rint((per_vertex_normals + 1.0) * 0.5 * 255.0), 0, 255
        ).astype(numpy.uint8)

        # 更新颜色信息
        new_color = []
        for i, val in enumerate(vb):
            color = [0, 0, 0, alpha]

            if mask[position_indices[i]]:
                color[:3] = normalized_normals[i]

            new_color.append(color)

        # 将新的颜色列表转换为NumPy数组
        new_color_array = numpy.array(new_color, dtype=numpy.uint8)

        # 更新vb中的颜色信息
        for i, val in enumerate(vb):
            val['COLOR'] = new_color_array[i]

        TimerUtils.End("Recalculate COLOR")
        return vb
    


    @staticmethod
    def average_normal_tangent(obj,indexed_vertices,d3d11GameType,dtype):
        '''
        Nico: 米游所有游戏都能用到这个，还有曾经的GPU-PreSkinning的GF2也会用到这个，崩坏三2.0新角色除外。
        尽管这个可以起到相似的效果，但是仍然无法完美获取模型本身的TANGENT数据，只能做到身体轮廓线99%近似。
        经过测试，头发轮廓线部分并不是简单的向量归一化，也不是算术平均归一化。

        ⚠️ ARCH-06 现状标注：**全仓已无调用方**（`git grep average_normal_tangent`
        仅命中定义处与本文件 `average_normal_tangent_xxmi` 的名称前缀；在用的是
        `:1050 average_normal_tangent_xxmi`，被 utils/export_utils.py:563 与
        本文件 :1184 / :1396 调用）。按"先证无调用方再删，否则保留 + 加提示"的
        纪律：**保留**（它是轮廓线修复的算法参照实现，删掉会丢失口径对照），
        并在此标注"当前无调用方"——包括其内部 `recalculate_tangent_basis()`
        early-return 目前是死分支。若后续确认不再需要该参照，可整段删除。
        '''
        # TimerUtils.Start("Recalculate TANGENT")

        if "TANGENT" not in d3d11GameType.OrderedFullElementList:
            return indexed_vertices
        # 标准切线(TBN)导出与轮廓线写入 TANGENT 互斥：勾选「重新计算标准切线」时
        # 保持标准 TBN，不再把平滑法线覆盖进 TANGENT。
        if GlobalProterties.recalculate_tangent_basis():
            return indexed_vertices
        allow_calc = False
        if GlobalProterties.recalculate_tangent():
            allow_calc = True
        elif obj.get("3DMigoto:RecalculateTANGENT",False): 
            allow_calc = True
        
        if not allow_calc:
            return indexed_vertices
        
        # 不用担心这个转换的效率，速度非常快
        vb = bytearray()
        for vertex in indexed_vertices:
            vb += bytes(vertex)
        vb = numpy.frombuffer(vb, dtype = dtype)

        # 开始重计算TANGENT
        positions = numpy.array([val['POSITION'] for val in vb])
        normals = numpy.array([val['NORMAL'] for val in vb], dtype=float)

        # 对位置进行排序，以便相同的位置会相邻
        sort_indices = numpy.lexsort(positions.T)
        sorted_positions = positions[sort_indices]
        sorted_normals = normals[sort_indices]

        # 找出位置变化的地方，即我们需要分组的地方
        group_indices = numpy.flatnonzero(numpy.any(sorted_positions[:-1] != sorted_positions[1:], axis=1))
        group_indices = numpy.r_[0, group_indices + 1, len(sorted_positions)]

        # 累加法线和计算计数
        unique_positions = sorted_positions[group_indices[:-1]]
        accumulated_normals = numpy.add.reduceat(sorted_normals, group_indices[:-1], axis=0)
        counts = numpy.diff(group_indices)

        # 归一化累积法线向量
        normalized_normals = accumulated_normals / numpy.linalg.norm(accumulated_normals, axis=1)[:, numpy.newaxis]
        normalized_normals[numpy.isnan(normalized_normals)] = 0  # 处理任何可能出现的零向量导致的除零错误

        # 构建结果字典
        position_normal_dict = dict(zip(map(tuple, unique_positions), normalized_normals))

        # TimerUtils.End("Recalculate TANGENT")

        # 获取所有位置并转换为元组，用于查找字典
        positions = [tuple(pos) for pos in vb['POSITION']]

        # 从字典中获取对应的标准化法线
        normalized_normals = numpy.array([position_normal_dict[pos] for pos in positions])

        # 计算 w 并调整 tangent 的第四个分量
        w = numpy.where(vb['TANGENT'][:, 3] >= 0, -1.0, 1.0)

        # 更新 TANGENT 分量，注意这里的切片操作假设 TANGENT 有四个分量
        vb['TANGENT'][:, :3] = normalized_normals
        vb['TANGENT'][:, 3] = w

        # TimerUtils.End("Recalculate TANGENT")

        return vb

    @staticmethod
    def average_normal_tangent_xxmi(obj, indexed_vertices, flattened_ib, d3d11GameType, dtype, rounding_precision: int = 4):
        '''
        使用 XXMI 的角度加权 outline 思路重计算 TANGENT.xyz。
        这里只替换 xyz，w 仍保持当前导出路径的处理习惯。
        '''
        if "TANGENT" not in d3d11GameType.OrderedFullElementList:
            return indexed_vertices
        # 标准切线(TBN)导出与轮廓线写入 TANGENT 互斥：勾选「重新计算标准切线」时
        # 保持标准 TBN，不再把平滑法线覆盖进 TANGENT。
        if GlobalProterties.recalculate_tangent_basis():
            return indexed_vertices

        allow_calc = False
        if GlobalProterties.recalculate_tangent():
            allow_calc = True
        elif obj.get("3DMigoto:RecalculateTANGENT",False):
            allow_calc = True

        if not allow_calc:
            return indexed_vertices

        vb = bytearray()
        for vertex in indexed_vertices:
            vb += bytes(vertex)
        vb = numpy.frombuffer(vb, dtype=dtype)

        if len(vb) == 0 or len(flattened_ib) < 3 or "POSITION" not in vb.dtype.names or "TANGENT" not in vb.dtype.names:
            return vb

        positions = numpy.asarray(vb['POSITION'], dtype=numpy.float32)
        tangents = numpy.asarray(vb['TANGENT'], dtype=numpy.float32)

        if positions.ndim != 2 or positions.shape[1] < 3 or tangents.ndim != 2 or tangents.shape[1] < 3:
            return vb

        def unit_vector(vector_array: numpy.ndarray) -> numpy.ndarray:
            norms = numpy.linalg.norm(vector_array, axis=1, keepdims=True)
            norms = numpy.where(norms == 0, 1, norms)
            return vector_array / norms

        def calc_angle(edge_a: numpy.ndarray, edge_b: numpy.ndarray) -> numpy.ndarray:
            vector_a = numpy.abs(unit_vector(edge_a))
            vector_b = numpy.abs(unit_vector(edge_b))
            return numpy.arccos(
                numpy.clip(
                    numpy.einsum("ij,ij->i", vector_a, vector_b),
                    -1,
                    1,
                )
            )

        ib_data = numpy.asarray(flattened_ib, dtype=numpy.int32)
        valid_triangle_index_count = (len(ib_data) // 3) * 3
        if valid_triangle_index_count == 0:
            return vb

        ib_data = ib_data[:valid_triangle_index_count]
        if ib_data.max(initial=-1) >= len(vb) or ib_data.min(initial=0) < 0:
            return vb

        loops_coord = positions[ib_data, 0:3]
        triangles = loops_coord.reshape(-1, 3, 3)

        edge0 = triangles[:, 1] - triangles[:, 2]
        edge1 = triangles[:, 2] - triangles[:, 0]
        edge2 = triangles[:, 0] - triangles[:, 1]

        angle0 = calc_angle(edge2, edge1)
        angle1 = calc_angle(edge0, edge2)
        angle2 = calc_angle(edge1, edge0)

        loops_angle = numpy.zeros((len(triangles), 3), dtype=numpy.float32)
        loops_angle[:, 0] = angle0
        loops_angle[:, 1] = angle1
        loops_angle[:, 2] = angle2

        faces_normal = unit_vector(numpy.cross(edge0, edge1))
        loops_face_normal = faces_normal.repeat(3, axis=0)

        loops_round_coord = numpy.round(loops_coord, rounding_precision)
        loops_weighted_normal = loops_face_normal * loops_angle.reshape(-1, 1)

        _, unique_indices, unique_inverse = numpy.unique(
            loops_round_coord,
            axis=0,
            return_index=True,
            return_inverse=True,
        )

        accumulated_normals = numpy.zeros((len(unique_indices), 3), dtype=numpy.float32)
        numpy.add.at(accumulated_normals, unique_inverse, loops_weighted_normal)

        accumulated_magnitudes = numpy.linalg.norm(accumulated_normals, axis=1, keepdims=True)
        fallback_normals = loops_face_normal[unique_indices]
        accumulated_normals = numpy.where(
            accumulated_magnitudes < 1e-6,
            fallback_normals,
            accumulated_normals,
        )

        outline_vectors = tangents[:, 0:3].copy()
        outline_vectors[ib_data] = unit_vector(accumulated_normals[unique_inverse])
        vb['TANGENT'][:, :3] = outline_vectors

        if tangents.shape[1] >= 4:
            w = numpy.where(vb['TANGENT'][:, 3] >= 0, -1.0, 1.0)
            vb['TANGENT'][:, 3] = w

        return vb

    @staticmethod
    def calc_index_vertex_buffer_universal(element_vertex_ndarray,mesh,obj,d3d11GameType,dtype):
        '''
        计算IndexBuffer和CategoryBufferDict并返回

        这里是速度瓶颈，23万顶点情况下测试，前面的获取mesh数据只用了1.5秒
        但是这里两个步骤加起来用了6秒，占了4/5运行时间。
        不过暂时也够用了，先不管了。
        '''
        # TimerUtils.Start("Calc IB VB")
        # (1) 统计模型的索引和唯一顶点
        '''
        不保持相同顶点时，仍然使用经典而又快速的方法
        '''
        # print("calc ivb universal")
        indexed_vertices = collections.OrderedDict()
        ib = [[indexed_vertices.setdefault(element_vertex_ndarray[blender_lvertex.index].tobytes(), len(indexed_vertices))
                for blender_lvertex in mesh.loops[poly.loop_start:poly.loop_start + poly.loop_total]
                    ]for poly in mesh.polygons] 
            
        flattened_ib = [item for sublist in ib for item in sublist]
        # TimerUtils.End("Calc IB VB")

        # 重计算TANGENT步骤
        indexed_vertices = ObjBufferHelper.average_normal_tangent_xxmi(
            obj=obj,
            indexed_vertices=indexed_vertices,
            flattened_ib=flattened_ib,
            d3d11GameType=d3d11GameType,
            dtype=dtype,
        )
        
        # 重计算COLOR步骤
        indexed_vertices = ObjBufferHelper.average_normal_color(obj=obj, indexed_vertices=indexed_vertices, d3d11GameType=d3d11GameType,dtype=dtype)

        # print("indexed_vertices:")
        # print(str(len(indexed_vertices)))

        # (2) 转换为CategoryBufferDict
        # TimerUtils.Start("Calc CategoryBuffer")
        category_stride_dict = d3d11GameType.get_real_category_stride_dict()
        category_buffer_dict:dict[str,list] = {}
        for categoryname,category_stride in d3d11GameType.CategoryStrideDict.items():
            category_buffer_dict[categoryname] = []

        data_matrix = numpy.array([numpy.frombuffer(byte_data,dtype=numpy.uint8) for byte_data in indexed_vertices])
        stride_offset = 0
        for categoryname,category_stride in category_stride_dict.items():
            category_buffer_dict[categoryname] = data_matrix[:,stride_offset:stride_offset + category_stride].flatten()
            stride_offset += category_stride

        ib = flattened_ib
        if GlobalConfig.logic_name == LogicName.YYSLS or GlobalConfig.logic_name == LogicName.SnowBreak:
            print("导出时翻转面朝向")

            flipped_indices = []
            # print(flattened_ib[0],flattened_ib[1],flattened_ib[2])
            for i in range(0, len(flattened_ib), 3):
                triangle = flattened_ib[i:i+3]
                flipped_triangle = triangle[::-1]
                flipped_indices.extend(flipped_triangle)
            # print(flipped_indices[0],flipped_indices[1],flipped_indices[2])
            ib = flipped_indices


        
        category_buffer_dict = category_buffer_dict
        index_vertex_id_dict = None

        return ib,category_buffer_dict,index_vertex_id_dict



    @staticmethod
    def calc_index_vertex_buffer_girlsfrontline2(
        mesh:bpy.types.Mesh, 
        element_vertex_ndarray:numpy.ndarray, 
        d3d11_game_type:D3D11GameType,
        dtype:numpy.dtype):
        '''
        [特殊模式：少前2专用] 强制索引对齐模式
        --------------------------------------------------
        核心逻辑：
        - 强制保持 "游戏引擎顶点数" == "Blender顶点数"。
        - 忽略硬边、UV缝隙导致的数据分裂，强制合并。
        
        适用场景：
        - 少前2等特殊渲染管线，或者模型已经预先处理过（所有硬边/UV缝隙确实就是物理断开的顶点）。
        - 这种模式下生成 ShapeKey 极其简单，因为索引是一一对应的。
        
        缺点：
        - 如果模型存在硬边或UV接缝，数据会被覆盖（合并），可能导致渲染错误（如法线平滑过度、UV错乱）。
        
        1. Blender 的“顶点数”= mesh.vertices 长度，只要位置不同就算一个。
        2. 我们预分配同样长度的盒子列表，盒子下标 == 顶点下标，保证一一对应。
        3. 遍历 loop 时，把真实数据写进对应盒子；没人引用的盒子留 dummy（坐标填对，其余 0）。
        4. 最后按盒子顺序打包成字节数组，长度必然与 mesh.vertices 相同，导出数就能和 Blender 状态栏完全一致。
        '''
        print("calc ivb gf2")

        loops = mesh.loops
        v_cnt = len(mesh.vertices)
        loop_vidx = numpy.empty(len(loops), dtype=int)
        loops.foreach_get("vertex_index", loop_vidx)

        # 1. 预分配：每条 Blender 顶点一条记录，先填“空”
        dummy = numpy.zeros(1, dtype=element_vertex_ndarray.dtype)
        vertex_buffer = [dummy.copy() for _ in range(v_cnt)]   # list[ndarray]
        # 2. 标记哪些顶点被 loop 真正用到
        used_mask = numpy.zeros(v_cnt, dtype=bool)
        used_mask[loop_vidx] = True

        # 3. 共享 TANGENT 字典
        pos_normal_key = {}   # (position_tuple, normal_tuple) -> tangent

        # 4. 先给“被用到”的顶点填真实数据
        for lp in loops:
            v_idx = lp.vertex_index
            if used_mask[v_idx]:          # 其实恒为 True，留着可读性
                data = element_vertex_ndarray[lp.index].copy()
                pn_key = (tuple(data['POSITION']), tuple(data['NORMAL']))
                if pn_key in pos_normal_key:
                    data['TANGENT'] = pos_normal_key[pn_key]
                else:
                    pos_normal_key[pn_key] = data['TANGENT']
                vertex_buffer[v_idx] = data

        # 5. 给“死顶点”也填上 dummy，但位置必须对
        for v_idx in range(v_cnt):
            if not used_mask[v_idx]:
                vertex_buffer[v_idx]['POSITION'] = mesh.vertices[v_idx].co
                # 其余字段保持 0

        # 6. 现在 vertex_buffer 长度 == v_cnt，直接转 bytes 即可
        indexed_vertices = [arr.tobytes() for arr in vertex_buffer]

        # 7. 重建索引缓冲（IB）
        ib = []
        for poly in mesh.polygons:
            ib.append([v_idx for lp in loops[poly.loop_start:poly.loop_start + poly.loop_total]
                    for v_idx in [lp.vertex_index]])

        flattened_ib = [i for sub in ib for i in sub]

        # 8. 拆 CategoryBuffer
        category_stride_dict = d3d11_game_type.get_real_category_stride_dict()
        category_buffer_dict = {name: [] for name in d3d11_game_type.CategoryStrideDict}
        data_matrix = numpy.array([numpy.frombuffer(b, dtype=numpy.uint8) for b in indexed_vertices])
        stride_offset = 0
        for name, stride in category_stride_dict.items():
            category_buffer_dict[name] = data_matrix[:, stride_offset:stride_offset + stride].flatten()
            stride_offset += stride

        # print("长度：", v_cnt)          
        ib = flattened_ib
        index_vertex_id_dict = None

        return ib, category_buffer_dict, index_vertex_id_dict
 


    @staticmethod
    def calc_index_vertex_buffer_unified(
        mesh:bpy.types.Mesh, 
        element_vertex_ndarray:numpy.ndarray, 
        obj:bpy.types.Object, 
        d3d11_game_type:D3D11GameType,
        dtype:numpy.dtype):
        '''
        [通用模式] 标准图形学导出逻辑
        --------------------------------------------------
        核心逻辑：
        - 以 "(数据内容 + Blender原始顶点索引)" 作为唯一标识。
        - 自动处理硬边、UV缝隙：如果同一个顶点在不同 Loop 上的法线/UV不同，会自动分裂成多个游戏顶点。
        - 自动处理 ShapeKey 安全：即使两个点坐标重合，只要 Blender 索引不同，就不会合并。
        
        适用场景：
        - 绝大多数现代游戏的标准导出流程。
        - 保证渲染正确性（法线、UV、顶点色）。
        
        代价：
        - 导出的顶点数通常多于 Blender 顶点数（因为分裂）。
        - 需要返回 index_vertex_id_dict 映射表，以便后续生成 ShapeKey Buffer 时能找回原始对应关系。

        计算IndexBuffer和CategoryBufferDict并返回
        如果模型具有形态键，那么形态键盘的值为0到1的任何值应用后，都不会造成由于顶点合并导致的顶点数改变。
        '''
        # TimerUtils.Start("Calc IB VB")
        
        # 统一逻辑：始终将 (数据 + 顶点索引) 作为唯一标识
        # 1. 彻底解决 ShapeKey 问题：防止 Basis 中重合但在 Morph 中分离的顶点被错误合并。
        # 2. 保持拓扑结构：确保 Blender 中不同的点导出后依然是不同的点。
        unique_map = collections.OrderedDict()
        
        # KEY: unique_vertex_index (buffer index), VALUE: first_loop_index
        # 记录每一个生成的 Buffer 顶点对应的是哪一个原始 Loop
        # 这对于 Shape Key 的法线导出至关重要，因为法线是存储在 Loop 上的
        unique_loop_map = {} 

        ib = []
        for poly in mesh.polygons:
            poly_indices = []
            for loop_index in range(poly.loop_start, poly.loop_start + poly.loop_total):
                loop = mesh.loops[loop_index]
                data = element_vertex_ndarray[loop.index].tobytes()
                
                # 核心：Key 始终包含 vertex_index (data, index)
                # 这样只有当 "数据完全一致" 且 "是同一个顶点(仅因硬边/UV断开)" 时才会共用索引
                key = (data, loop.vertex_index)
                
                if key in unique_map:
                    idx = unique_map[key]
                else:
                    idx = len(unique_map)
                    unique_map[key] = idx
                    unique_loop_map[idx] = loop.index
                
                poly_indices.append(idx)
            ib.append(poly_indices)
        
        # 提取 vertex buffer 需要的数据 (也就是 key 中的 data 部分)
        # vertex_data_list = [k[0] for k in unique_map.keys()]

        # 同时构建 index -> blender_loop_index 的映射
        # 这对于后续生成 ShapeKey Buffer 至关重要，因为我们需要知道当前生成的第 i 个点对应 Blender 的哪个 Loop
        # Loop Index 可进一步转换为 Vertex Index，但 Vertex Index 无法反推唯一的 Loop Index (Split Normals)
        vertex_data_list = []
        for i, (data_bytes, blender_v_idx) in enumerate(unique_map.keys()):
            vertex_data_list.append(data_bytes)
        
        index_loop_id_dict = unique_loop_map

        flattened_ib = [item for sublist in ib for item in sublist]
        # TimerUtils.End("Calc IB VB")

        # 重计算TANGENT步骤
        indexed_vertices = ObjBufferHelper.average_normal_tangent_xxmi(
            obj=obj,
            indexed_vertices=vertex_data_list,
            flattened_ib=flattened_ib,
            d3d11GameType=d3d11_game_type,
            dtype=dtype,
        )
        
        # 重计算COLOR步骤
        indexed_vertices = ObjBufferHelper.average_normal_color(obj=obj, indexed_vertices=indexed_vertices, d3d11GameType=d3d11_game_type,dtype=dtype)

        # (2) 转换为CategoryBufferDict
        # TimerUtils.Start("Calc CategoryBuffer")
        category_stride_dict = d3d11_game_type.get_real_category_stride_dict()
        category_buffer_dict:dict[str,list] = {}
        for categoryname,category_stride in d3d11_game_type.CategoryStrideDict.items():
            category_buffer_dict[categoryname] = []

        data_matrix = numpy.array([numpy.frombuffer(byte_data,dtype=numpy.uint8) for byte_data in indexed_vertices])
        stride_offset = 0
        for categoryname,category_stride in category_stride_dict.items():
            category_buffer_dict[categoryname] = data_matrix[:,stride_offset:stride_offset + category_stride].flatten()
            stride_offset += category_stride

        # 设置ib，准备返回
        ib = flattened_ib
        # YYSLS是目前除了鸣潮外，唯一需要翻转面朝向的游戏
        if GlobalConfig.logic_name == LogicName.YYSLS or GlobalConfig.logic_name == LogicName.SnowBreak:
            flipped_indices = []
            # print(flattened_ib[0],flattened_ib[1],flattened_ib[2])
            for i in range(0, len(flattened_ib), 3):
                triangle = flattened_ib[i:i+3]
                flipped_triangle = triangle[::-1]
                flipped_indices.extend(flipped_triangle)
            # print(flipped_indices[0],flipped_indices[1],flipped_indices[2])
            ib = flipped_indices

        return ib, category_buffer_dict,index_loop_id_dict
      
