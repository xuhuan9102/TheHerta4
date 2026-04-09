import bpy
import bmesh

from bpy.props import BoolProperty, CollectionProperty

from ..utils.obj_utils import ObjUtils
from ..utils.collection_utils import CollectionUtils
from ..utils.vertexgroup_utils import VertexGroupUtils
from ..utils.shapekey_utils import ShapeKeyUtils
from ..utils.algorithm_utils import AlgorithmUtils


class ModelSplitByLoosePart(bpy.types.Operator):
    bl_idname = "toolkit.split_by_loose_part"
    bl_label = "根据UV松散块儿分割模型"
    bl_description = "功能与Edit界面的Split => Split by Loose Parts相似，但是分割模型为松散块儿并放入新集合。"

    def execute(self, context):
        
        if len(bpy.context.selected_objects) == 0:
            self.report({'ERROR'}, "没有选中的对象！")
            return {'CANCELLED'}
        obj = bpy.context.selected_objects[0]
        collection_name = f"{obj.name}_LooseParts"
        ObjUtils.split_obj_by_loose_parts_to_collection(obj=obj,collection_name=collection_name)

        self.report({'INFO'}, "根据UV松散块儿分割模型成功!")
        return {'FINISHED'}


class ModelSplitByVertexGroup(bpy.types.Operator):
    bl_idname = "toolkit.split_by_vertex_group"
    bl_label = "根据共享与孤立顶点组分割模型"
    bl_description = "把模型根据共享的顶点组分开，方便快速分离身体上的小物件，方便后续刷权重不受小物件影响。"

    def execute(self, context):
        if len(bpy.context.selected_objects) == 0:
            self.report({'ERROR'}, "没有选中的对象！")
            return {'CANCELLED'}
        obj = bpy.context.selected_objects[0]
        collection_name = f"{obj.name}_Splits"
        ObjUtils.split_obj_by_loose_parts_to_collection(obj=obj,collection_name=collection_name)
        
        collection = CollectionUtils.get_collection_by_name(collection_name=collection_name)
        CollectionUtils.select_collection_objects(collection)
        selected_objects = bpy.context.selected_objects

        number_vgnameset_dict = {}
        number_objlist_dict = {}

        for obj in selected_objects:
            VertexGroupUtils.remove_unused_vertex_groups(obj)
            vertex_group_names = [vg.name for vg in obj.vertex_groups]
            vgname_set = set()
            for vgname in vertex_group_names:
                    vgname_set.add(vgname)

            if len(number_vgnameset_dict) == 0:
                number_vgnameset_dict[1] = vgname_set
                number_objlist_dict[1] = [obj]
            else:
                exists = False
                for number, tmp_vgname_set in number_vgnameset_dict.items():
                    vgname_jiaoji = tmp_vgname_set & vgname_set
                    if len(vgname_jiaoji) != 0:
                        vgname_quanji = tmp_vgname_set.union(vgname_set)
                        number_vgnameset_dict[number] = vgname_quanji
                        exists = True
                        break
                
                if not exists:
                    number_objlist_dict[len(number_objlist_dict) + 1] = [obj]
                    number_vgnameset_dict[len(number_vgnameset_dict) + 1] = vgname_set
                else:
                    number_objlist_dict[number].append(obj)

        for number, objlist in number_objlist_dict.items():
            ObjUtils.merge_objects(obj_list=objlist,target_collection=collection)
        self.report({'INFO'}, "根据顶点组分割模型成功!")
        return {'FINISHED'}
    

class ModelDeleteLoosePoint(bpy.types.Operator):
    bl_idname = "toolkit.delete_loose_point"
    bl_label = "删除模型中的松散点"
    bl_description = "删除模型中的松散点，避免影响后续的模型处理。"

    def execute(self, context):
        
        if len(bpy.context.selected_objects) == 0:
            self.report({'ERROR'}, "没有选中的对象！")
            return {'CANCELLED'}
        
        ObjUtils.selected_obj_delete_loose()

        self.report({'INFO'}, "删除松散点成功!")
        return {'FINISHED'}
    
class ModelClearCustomSplitNormals(bpy.types.Operator):
    bl_idname = "toolkit.clear_custom_split_normals"
    bl_label = "清除自定义拆分法向"
    bl_description = "WuWa 逆向得到的模型，有时顶点法线会歪，用这个处理一下就行。"
    def execute(self, context):
        sel = context.selected_objects
        if not sel:
            self.report({'ERROR'}, "未选中对象！")
            return {'CANCELLED'}
        for obj in sel:
            if obj.type == 'MESH':
                context.view_layer.objects.active = obj
                bpy.ops.object.mode_set(mode='OBJECT')
                bpy.ops.mesh.customdata_custom_splitnormals_clear()
        return {'FINISHED'}
    
class ModelRenameVertexGroupNameWithTheirSuffix(bpy.types.Operator):
    bl_idname = "toolkit.rename_vertex_group_name_with_their_suffix"
    bl_label = "用模型名称作为前缀重命名顶点组"
    bl_description = "用模型名称作为前缀重命名顶点组，方便后续合并到一个物体后同名称的顶点组不会合在一起冲突，便于后续一键绑定骨骼。"

    def execute(self, context):
        
        if len(bpy.context.selected_objects) == 0:
            self.report({'ERROR'}, "没有选中的对象！")
            return {'CANCELLED'}
        
        for obj in context.selected_objects:
            if obj.type == 'MESH':
                model_name = obj.name
                for vertex_group in obj.vertex_groups:
                    original_name = vertex_group.name
                    new_name = f"{model_name}_{original_name}"
                    vertex_group.name = new_name

        self.report({'INFO'}, "用模型名称作为前缀重命名顶点组成功!")
        return {'FINISHED'}


class AddBoneFromVertexGroupV2(bpy.types.Operator):
    bl_idname = "toolkit.add_bone_from_vertex_group_v2"
    bl_label = "根据顶点组生成基础骨骼"
    bl_description = "把当前选中的obj的每个顶点组都生成一个默认位置的骨骼，方便接下来手动调整骨骼位置和父级关系来绑骨，虹汐哥改进版本"
    def execute(self, context):
        if len(bpy.context.selected_objects) == 0:
            self.report({'ERROR'}, "没有选中的对象！")
            return {'CANCELLED'}
        VertexGroupUtils.create_armature_from_vertex_groups()
        self.report({'INFO'}, self.bl_label + " 成功!")
        return {'FINISHED'}


class SplitMeshByCommonVertexGroup(bpy.types.Operator):
    bl_idname = "toolkit.split_mesh_by_common_vertex_group"
    bl_label = "根据顶点组将模型打碎为松散块儿"
    bl_description = "把当前选中的obj按顶点组进行分割，适用于部分精细刷权重并重新组合模型的场景"
    
    def execute(self, context):
        for obj in bpy.context.selected_objects:
            VertexGroupUtils.split_mesh_by_vertex_group(obj)
        self.report({'INFO'}, self.bl_label + " 成功!")
        return {'FINISHED'}
    

class SmoothNormalSaveToUV(bpy.types.Operator):
    bl_idname = "toolkit.smooth_normal_save_to_uv"
    bl_label = "平滑法线存UV(近似)"
    bl_description = "平滑法线存UV算法，可用于修复ZZZ,WuWa的某些UV(只是近似实现60%的效果)" 

    def execute(self, context):
        AlgorithmUtils.smooth_normal_save_to_uv()
        return {'FINISHED'}
    


        
class PropertyCollectionModifierItem(bpy.types.PropertyGroup):
    checked: BoolProperty(
        name="", 
        default=False
    ) # type: ignore

class WWMI_ApplyModifierForObjectWithShapeKeysOperator(bpy.types.Operator):
    bl_idname = "toolkit.apply_modifier_for_object_with_shape_keys"
    bl_label = "在有形态键的模型上应用修改器"
    bl_description = "Apply selected modifiers and remove from the stack for object with shape keys (Solves 'Modifier cannot be applied to a mesh with shape keys' error when pushing 'Apply' button in 'Object modifiers'). Sourced by Przemysław Bągard"
 
    def item_list(self, context):
        return [(modifier.name, modifier.name, modifier.name) for modifier in bpy.context.object.modifiers]
    
    my_collection: CollectionProperty(
        type=PropertyCollectionModifierItem
    ) # type: ignore
    
    disable_armatures: BoolProperty(
        name="Don't include armature deformations",
        default=True,
    ) # type: ignore
 
    def execute(self, context):
        ob = bpy.context.object
        bpy.ops.object.select_all(action='DESELECT')
        context.view_layer.objects.active = ob
        ob.select_set(True)
        
        selectedModifiers = [o.name for o in self.my_collection if o.checked]
        
        if not selectedModifiers:
            self.report({'ERROR'}, 'No modifier selected!')
            return {'FINISHED'}
        
        success, errorInfo = ShapeKeyUtils.apply_modifiers_for_object_with_shape_keys(context, selectedModifiers, self.disable_armatures)
        
        if not success:
            self.report({'ERROR'}, errorInfo)
        
        return {'FINISHED'}
        
    def draw(self, context):
        if context.object.data.shape_keys and context.object.data.shape_keys.animation_data:
            self.layout.separator()
            self.layout.label(text="Warning:")
            self.layout.label(text="              Object contains animation data")
            self.layout.label(text="              (like drivers, keyframes etc.)")
            self.layout.label(text="              assigned to shape keys.")
            self.layout.label(text="              Those data will be lost!")
            self.layout.separator()
        box = self.layout.box()
        for prop in self.my_collection:
            box.prop(prop, "checked", text=prop["name"])
        self.layout.prop(self, "disable_armatures")
 
    def invoke(self, context, event):
        self.my_collection.clear()
        for i in range(len(bpy.context.object.modifiers)):
            item = self.my_collection.add()
            item.name = bpy.context.object.modifiers[i].name
            item.checked = False
        return context.window_manager.invoke_props_dialog(self)
    

class RecalculateTANGENTWithVectorNormalizedNormal(bpy.types.Operator):
    bl_idname = "toolkit.recalculate_tangent_arithmetic_average_normal"
    bl_label = "使用向量相加归一化算法重计算TANGENT"
    bl_description = "近似修复轮廓线算法，可以达到99%的轮廓线相似度，适用于GI,HSR,ZZZ,HI3 2.0之前的老角色" 
    def execute(self, context):
        for obj in bpy.context.selected_objects:
            if obj.type == "MESH":
                if obj.get("3DMigoto:RecalculateTANGENT",False):
                    obj["3DMigoto:RecalculateTANGENT"] = not obj["3DMigoto:RecalculateTANGENT"]
                else:
                    obj["3DMigoto:RecalculateTANGENT"] = True
                self.report({'INFO'},"重计算TANGENT设为:" + str(obj["3DMigoto:RecalculateTANGENT"]))
        return {'FINISHED'}


class RecalculateCOLORWithVectorNormalizedNormal(bpy.types.Operator):
    bl_idname = "toolkit.recalculate_color_arithmetic_average_normal"
    bl_label = "使用算术平均归一化算法重计算COLOR"
    bl_description = "近似修复轮廓线算法，可以达到99%的轮廓线相似度，仅适用于HI3 2.0新角色" 

    def execute(self, context):
        for obj in bpy.context.selected_objects:
            if obj.type == "MESH":
                if obj.get("3DMigoto:RecalculateCOLOR",False):
                    obj["3DMigoto:RecalculateCOLOR"] = not obj["3DMigoto:RecalculateCOLOR"]
                else:
                    obj["3DMigoto:RecalculateCOLOR"] = True
                self.report({'INFO'},"重计算COLOR设为:" + str(obj["3DMigoto:RecalculateCOLOR"]))
        return {'FINISHED'}
    


class RenameAmatureFromGame(bpy.types.Operator):
    bl_idname = "toolkit.rename_amature_from_game"
    bl_label = "重命名选中Amature的骨骼名称(GI)(测试)"
    bl_description = "用于把游戏里解包出来的骨骼重命名，方便我们直接一键绑定到提取出的Mod模型上，Credit to Leotorrez"
    def execute(self, context):
        armature_name = bpy.context.active_object.name

        object_name_original = 'Body'
        if not bpy.context.active_object:
            raise RuntimeError("The selected object is not an armature.")
        if bpy.context.active_object.type != "ARMATURE" or armature_name not in bpy.data.objects:
            raise RuntimeError("Error: No object selected.")

        bpy.ops.object.scale_clear()
        bpy.context.view_layer.objects.active = bpy.data.objects[armature_name]
        bpy.ops.object.mode_set(mode='OBJECT')
        bpy.ops.transform.mirror(constraint_axis=(True, False, False))
        bpy.ops.object.transform_apply(scale=True, rotation=False)

        vertex_groups = [vg.name for vg in bpy.data.objects[object_name_original].vertex_groups]
        pairs = {old:new for old,new in zip(vertex_groups, sorted(vertex_groups))}
        name_mapping = {new: str(i) for i, (_, new) in enumerate(pairs.items())}
        for vertex_group in bpy.data.objects[object_name_original].vertex_groups:
            armature_obj = bpy.data.objects[armature_name].data
            armature_obj.bones[vertex_group.name].name = vertex_group.name = name_mapping[vertex_group.name]

        new_armature_name = f"{armature_name}_sorted"
        bpy.data.objects[armature_name].name = new_armature_name
        bpy.context.view_layer.objects.active = bpy.data.objects[new_armature_name]
        obj = bpy.data.objects.get(new_armature_name)
        obj.parent = None
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
        obj.rotation_euler[0] = -1.5708
        bpy.ops.object.transform_apply(location=False, rotation=True, scale=False)
        obj.rotation_euler[0] = 1.5708

        for obj in bpy.data.objects:
            if obj.name != new_armature_name:
                for child in obj.children:
                    bpy.data.objects.remove(child)
                bpy.data.objects.remove(obj)
        return {'FINISHED'}


model_operators_list = [
    ModelSplitByLoosePart,
    ModelSplitByVertexGroup,
    ModelDeleteLoosePoint,
    ModelClearCustomSplitNormals,
    ModelRenameVertexGroupNameWithTheirSuffix,
    AddBoneFromVertexGroupV2,
    SplitMeshByCommonVertexGroup,
    SmoothNormalSaveToUV,
    PropertyCollectionModifierItem,
    WWMI_ApplyModifierForObjectWithShapeKeysOperator,
    RecalculateTANGENTWithVectorNormalizedNormal,
    RecalculateCOLORWithVectorNormalizedNormal,
    RenameAmatureFromGame,
]
