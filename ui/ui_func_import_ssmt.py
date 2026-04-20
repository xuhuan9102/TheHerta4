
'''
导入模型配置面板
'''
import os
import bpy

# 用于解决 AttributeError: 'IMPORT_MESH_OT_migoto_raw_buffers_mmt' object has no attribute 'filepath'
from bpy_extras.io_utils import ImportHelper

from ..utils.json_utils import JsonUtils
from ..utils.collection_utils import CollectionUtils
from ..utils.timer_utils import TimerUtils
from ..utils.translate_utils import TR

from ..common.global_config import GlobalConfig
from ..common.global_properties import GlobalProterties
from ..common.non_mirror_workflow import NonMirrorWorkflowHelper
from ..common.ssmt_import_helper import SSMTImportHelper
from ..common.workspace_helper import WorkSpaceHelper
from .ui_prefix_quick_ops import PrefixQuickOpsHelper


# 全量导入逻辑
def ImprotFromWorkSpaceFull(self, context):
    
    # 这里先创建以当前工作空间为名称的集合，并且链接到scene，确保它存在
    workspace_collection = WorkSpaceHelper.create_and_get_workspace_collection()

    # 获取当前工作空间文件夹下面的所有文件夹（仅保留名字包含 '-' 的文件夹）
    workspace_subfolders = WorkSpaceHelper.get_submesh_folderpath_list()

    # 读取当前工作空间下的DrawIB和Alias对应关系，如果不存在就是空列表
    # 空列表也没关系，下面会赋予默认名称
    drawib_aliasname_dict = WorkSpaceHelper.get_drawib_aliasname_dict()

    # 读取时保存每个导入文件夹里导入的 GameType 名称到工作空间根目录的 Import.json
    # 生成 Mod 时会用它来确定应该进入哪个 TYPE_xxx 目录读取 SubmeshJson
    foldername_gametypename_dict = {}
    imported_objects = []

    for submesh_folder_path in workspace_subfolders:
        submesh_folder_name = os.path.basename(submesh_folder_path)
        print("Import FolderName: " + submesh_folder_name)
        
        # 获取导入的数据类型文件夹路径列表
        final_import_folder_path_list = WorkSpaceHelper.get_ordered_gpu_cpu_import_folderpath_list(submesh_folder_path)
        
        # 接下来开始导入，尝试对当前DrawIB的每个数据类型都进行导入
        # 如果出错的话直接提示错误并continue
        for import_folder_path in final_import_folder_path_list:
            gametype_name = import_folder_path.split("TYPE_")[1]

            try:
                print("尝试导入路径: " + import_folder_path)
                draw_ib = submesh_folder_name.split("-")[0]
                this_alias = "." + (drawib_aliasname_dict.get(draw_ib) or "自定义名称")
                json_file_path = os.path.join(import_folder_path, submesh_folder_name + ".json")
                imported_obj = SSMTImportHelper.create_mesh_from_json(
                    json_file_path=json_file_path,
                    import_collection=workspace_collection,
                )
                if imported_obj is not None:
                    imported_obj.name = submesh_folder_name + this_alias
                    imported_obj.data.name = imported_obj.name
                    imported_objects.append(imported_obj)

                # 如果能执行到这里，说明这个DrawIB成功导入了一个数据类型
                # 然后要把这个DrawIB对应的GameType名称保存下来
                foldername_gametypename_dict[submesh_folder_name] = gametype_name
                self.report({'INFO'}, "成功导入" + submesh_folder_name + " 的数据类型: " + gametype_name)
            except Exception as e:
                print(f"Failed to import from {import_folder_path}: {e}")
                continue
            # 直到第一个导入成功就Break
            # 因为我们还没有添加多个数据类型时，让物体携带数据类型信息的机制
            # 所以这里暂时还是哪个导入成功了用哪个
            break
            

            

    # 保存工作空间级 Import.json 选择记录
    save_import_json_path = os.path.join(GlobalConfig.path_workspace_folder(),"Import.json")
    JsonUtils.SaveToFile(json_dict=foldername_gametypename_dict,filepath=save_import_json_path)

    if GlobalProterties.enable_non_mirror_workflow():
        NonMirrorWorkflowHelper.process_imported_objects(imported_objects)
    
    # 因为用户习惯了导入后就是全部选中的状态，所以默认选中所有导入的obj
    CollectionUtils.select_collection_objects(workspace_collection)
    PrefixQuickOpsHelper.merge_prefixes_from_objects(context, imported_objects)

    # ==========================
    # 自动生成蓝图节点逻辑
    # ==========================
    try:
        tree_name = GlobalConfig.workspacename

        try:
            tree = bpy.data.node_groups.new(name=tree_name, type='SSMTBlueprintTreeType')
        except Exception as e:
            print(f"Failed to create new node tree: {e}. Check if SSMTBlueprintTreeType is registered.")
            return
        tree.use_fake_user = True

        drawib_tabname_dict = WorkSpaceHelper.get_drawib_tabname_dict()

        tab_group_nodes = {}
        tab_node_lists = {}
        default_group_node = tree.nodes.new('SSMTNode_Object_Group')
        default_group_node.label = "Default Group"
        default_node_list = []

        y_gap = 200
        tab_gap = 400

        if 'workspace_collection' in locals() and workspace_collection:
             target_objects = workspace_collection.objects
        else:
             target_objects = []

        if not target_objects:
             print("Warning: Could not find Workspace collection to generate blueprint nodes.")

        for import_folder_path in workspace_subfolders:
            submesh_folder_name = os.path.basename(import_folder_path)
            namesplits = submesh_folder_name.split('-')
            if len(namesplits) < 3:
                continue
            draw_ib = namesplits[0]

            tab_name = drawib_tabname_dict.get(draw_ib)

            if tab_name and tab_name not in tab_group_nodes:
                group_node = tree.nodes.new('SSMTNode_Object_Group')
                group_node.label = tab_name
                tab_group_nodes[tab_name] = group_node
                tab_node_lists[tab_name] = []

            target_group = tab_group_nodes.get(tab_name, default_group_node) if tab_name else default_group_node

            found_objs = [obj for obj in target_objects if obj.name.startswith(submesh_folder_name)]

            for obj in found_objs:
                 if obj.type == 'MESH':
                    node = tree.nodes.new('SSMTNode_Object_Info')

                    node.object_name = obj.name
                    node.object_id = str(obj.as_pointer())

                    node.draw_ib = draw_ib

                    name_parts = obj.name.split('-')
                    if len(name_parts) >= 2:
                        node.component = name_parts[1]
                    else:
                        node.component = "1"

                    node.alias_name = drawib_aliasname_dict.get(draw_ib, "自定义名称")
                    node.label = obj.name

                    if target_group.inputs[-1].is_linked:
                        target_group.inputs.new('SSMTSocketObject', f"Input {len(target_group.inputs) + 1}")

                    tree.links.new(node.outputs[0], target_group.inputs[-1])

                    if tab_name:
                        tab_node_lists[tab_name].append(node)
                    else:
                        default_node_list.append(node)

        all_group_nodes = []
        tab_order = list(tab_group_nodes.keys())

        current_y = 0
        for tab_name in tab_order:
            nodes = tab_node_lists.get(tab_name, [])
            for node in nodes:
                node.location = (0, current_y)
                current_y -= y_gap
            current_y -= tab_gap - y_gap

        for node in default_node_list:
            node.location = (0, current_y)
            current_y -= y_gap

        has_default_links = any(inp.is_linked for inp in default_group_node.inputs)
        if has_default_links:
            all_group_nodes.append(default_group_node)
        elif not all_group_nodes:
            all_group_nodes.append(default_group_node)
        else:
            tree.nodes.remove(default_group_node)

        for tab_name in tab_order:
            all_group_nodes.append(tab_group_nodes[tab_name])

        group_x = 400
        group_current_y = 0

        for grp_node in all_group_nodes:
            grp_node.location = (group_x, group_current_y)
            group_current_y -= 300

        output_node = tree.nodes.new('SSMTNode_Result_Output')
        output_node.location = (800, 0)
        output_node.label = "Generate Mod"

        if len(output_node.inputs) > 0 and len(all_group_nodes) > 0:
            if len(all_group_nodes) == 1:
                tree.links.new(all_group_nodes[0].outputs[0], output_node.inputs[0])
            else:
                merge_node = tree.nodes.new('SSMTNode_Object_Group')
                merge_node.label = "Merge"
                merge_node.location = (600, 0)

                for grp_node in all_group_nodes:
                    if merge_node.inputs[-1].is_linked:
                        merge_node.inputs.new('SSMTSocketObject', f"Input {len(merge_node.inputs) + 1}")
                    tree.links.new(grp_node.outputs[0], merge_node.inputs[-1])

                tree.links.new(merge_node.outputs[0], output_node.inputs[0])

        for grp_node in all_group_nodes:
            if hasattr(grp_node, "update"):
                grp_node.update()

        print(f"Blueprint {tree_name} updated with imported objects, grouped by workspace tabs.")

    except Exception as e:
        print(f"Error generating blueprint nodes: {e}")
        import traceback
        traceback.print_exc()
    


class SSMT4ImportAllFromCurrentWorkSpaceBlueprint(bpy.types.Operator):
    bl_idname = "ssmt4.import_all_from_workspace"
    bl_label = TR.translate("一键导入SSMT工作空间内容")
    bl_description = "一键导入当前工作空间文件夹下所有的内容"
    bl_options = {'REGISTER','UNDO'}

    def execute(self, context):
        # print("Current WorkSpace: " + GlobalConfig.workspacename)
        # print("Current Game: " + GlobalConfig.gamename)
        if GlobalConfig.workspacename == "":
            self.report({"ERROR"},"Please select your WorkSpace in SSMT before import.")
        elif not os.path.exists(GlobalConfig.path_workspace_folder()):
            self.report({"ERROR"},"WorkSpace Folder Didn't exists, Please create a WorkSpace in SSMT before import " + GlobalConfig.path_workspace_folder())
        else:
            TimerUtils.Start("ImportFromWorkSpaceBlueprint")
            ImprotFromWorkSpaceFull(self, context)
            TimerUtils.End("ImportFromWorkSpaceBlueprint")
        
        return {'FINISHED'}
    

class SSMT4ImportRaw(bpy.types.Operator, ImportHelper):
    bl_idname = "ssmt4.import_raw"
    bl_label = TR.translate("导入SSMT格式模型")
    bl_description = "导入SSMT格式的模型文件, 只需选择.json文件即可"
    bl_options = {'REGISTER','UNDO'}

    filter_glob: bpy.props.StringProperty(
        default='*.json',
        options={'HIDDEN'},
    ) # type: ignore

    files: bpy.props.CollectionProperty(
        name="File Path",
        type=bpy.types.OperatorFileListElement,
    ) # type: ignore

    def execute(self, context):
        # 我们需要添加到一个新建的集合里，方便后续操作
        # 这里集合的名称需要为当前文件夹的名称
        dirname = os.path.dirname(self.filepath)

        collection_name = os.path.basename(dirname)
        collection = bpy.data.collections.new(collection_name)
        bpy.context.scene.collection.children.link(collection)
        imported_objects = []

        # 如果用户不选择任何json文件，则默认返回读取所有的json文件。
        import_filename_list = []
        if len(self.files) == 1:
            if str(self.filepath).endswith(".json"):
                import_filename_list.append(self.filepath)
            else:
                for filename in os.listdir(self.filepath):
                    if filename.endswith(".json"):
                        import_filename_list.append(filename)
        else:
            for json_file in self.files:
                import_filename_list.append(json_file.name)

        # 逐个json文件导入
        for json_file_name in import_filename_list:
            if os.path.isabs(json_file_name):
                json_file_path = json_file_name
            else:
                json_file_path = os.path.join(dirname, json_file_name)
            imported_obj = SSMTImportHelper.create_mesh_from_json(json_file_path=json_file_path, import_collection=collection)
            if imported_obj is not None:
                imported_objects.append(imported_obj)

        if GlobalProterties.enable_non_mirror_workflow():
            NonMirrorWorkflowHelper.process_imported_objects(imported_objects)

        # Select all objects under collection (因为用户习惯了导入后就是全部选中的状态). 
        CollectionUtils.select_collection_objects(collection)
        PrefixQuickOpsHelper.merge_prefixes_from_objects(context, imported_objects)

        return {'FINISHED'}

def register():
    bpy.utils.register_class(SSMT4ImportRaw)
    bpy.utils.register_class(SSMT4ImportAllFromCurrentWorkSpaceBlueprint)


def unregister():
    bpy.utils.unregister_class(SSMT4ImportRaw)
    bpy.utils.unregister_class(SSMT4ImportAllFromCurrentWorkSpaceBlueprint)
