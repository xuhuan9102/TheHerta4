
'''
导入模型配置面板
'''
import os
import bpy

# 用于解决 AttributeError: 'IMPORT_MESH_OT_migoto_raw_buffers_mmt' object has no attribute 'filepath'
from bpy_extras.io_utils import ImportHelper

from ..base.utils.obj_utils import ObjUtils 

from ..base.utils.json_utils import JsonUtils
from ..base.utils.collection_utils import CollectionColor, CollectionUtils
from ..base.utils.timer_utils import TimerUtils
from ..base.utils.translate_utils import TR

from ..base.config.main_config import GlobalConfig, LogicName

from ..helper.mesh_import_helper import MeshImportHelper,MigotoBinaryFile
import os
import bpy

from ..base.utils.json_utils import JsonUtils
from ..base.utils.collection_utils import CollectionColor, CollectionUtils
from ..base.utils.translate_utils import TR
from ..base.utils.timer_utils import TimerUtils

from ..base.config.main_config import GlobalConfig, LogicName

from ..helper.mesh_import_helper import MeshImportHelper,MigotoBinaryFile
from ..helper.workspace_helper import WorkSpaceHelper


# 全量导入逻辑
def ImprotFromWorkSpaceFull(self, context):
    
    # 这里先创建以当前工作空间为名称的集合，并且链接到scene，确保它存在
    workspace_collection = WorkSpaceHelper.create_and_get_workspace_collection()

    # 获取当前工作空间文件夹下面的所有文件夹（仅保留名字包含 '-' 的文件夹）
    workspace_subfolders = WorkSpaceHelper.get_submesh_folderpath_list()

    # 读取当前工作空间下的DrawIB和Alias对应关系，如果不存在就是空列表
    # 空列表也没关系，下面会赋予默认名称
    drawib_aliasname_dict = WorkSpaceHelper.get_drawib_aliasname_dict()

    # 读取时保存每个导入文件夹里导入的GameType名称到工作空间文件夹下面的Import.json，在导出时使用
    foldername_gametypename_dict = {}

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
                fmt_file_path = os.path.join(import_folder_path, submesh_folder_name + ".fmt")
                mbf = MigotoBinaryFile(fmt_path=fmt_file_path,mesh_name= submesh_folder_name + ".自定义名称")
                MeshImportHelper.create_mesh_obj_from_mbf(mbf=mbf,import_collection=workspace_collection)

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
            

            

    # 保存Import.json文件
    save_import_json_path = os.path.join(GlobalConfig.path_workspace_folder(),"Import.json")
    JsonUtils.SaveToFile(json_dict=foldername_gametypename_dict,filepath=save_import_json_path)
    
    # 因为用户习惯了导入后就是全部选中的状态，所以默认选中所有导入的obj
    CollectionUtils.select_collection_objects(workspace_collection)

    # ==========================
    # 自动生成蓝图节点逻辑
    # ==========================
    try:
        # 创建蓝图，名称为当前工作空间名称
        tree_name = GlobalConfig.workspacename
        
        # Nico: 为了防止覆盖用户修改过的蓝图，始终创建新蓝图
        # 如果已存在同名蓝图，Blender会自动添加.001等后缀，从而保留旧蓝图
        try:
            tree = bpy.data.node_groups.new(name=tree_name, type='SSMTBlueprintTreeType')
        except Exception as e:
            print(f"Failed to create new node tree: {e}. Check if SSMTBlueprintTreeType is registered.")
            return
        tree.use_fake_user = True
        
        # 创建 Group 节点 (并在循环中连接)
        group_node = tree.nodes.new('SSMTNode_Object_Group')
        group_node.label = "Default Group"
        
        # 3. 遍历导入的对象并创建对应节点
        current_x = 0
        current_y = 0
        y_gap = 200 # 增加垂直间距
        
        count = 0
        
        # 此时 default_show_collection 应该在作用域内，因为它是上面的局部变量
        # 且导入的模型都放在这个集合里
        if 'workspace_collection' in locals() and workspace_collection:
             target_objects = workspace_collection.objects
        else:
             target_objects = [] # Fallback

        if not target_objects:
             print("Warning: Could not find Workspace collection to generate blueprint nodes.")

        # 使用列表手动计算布局中心
        min_y = 0
        for import_folder_path in workspace_subfolders:
            submesh_folder_name = os.path.basename(import_folder_path)
            namesplits = submesh_folder_name.split('-')
            if len(namesplits) < 3:
                continue
            draw_ib = namesplits[0]
            index_count = namesplits[1]
            first_index = namesplits[2]
            
            # 在导入集合中寻找属于当前 DrawIB 的对象
            # 命名规则通常是: DrawIB-Part-Alias
            found_objs = [obj for obj in target_objects if obj.name.startswith(submesh_folder_name)]
            
            for obj in found_objs:
                 if obj.type == 'MESH':
                    # 创建节点
                    node = tree.nodes.new('SSMTNode_Object_Info')
                    node.location = (current_x, current_y)
                    
                    # 填充属性
                    node.object_name = obj.name
                    node.draw_ib = draw_ib
                    
                    # 解析 Part 部分作为 Component (即 DrawIB-Part-Alias 中的 Part)
                    name_parts = obj.name.split('-')
                    if len(name_parts) >= 2:
                        node.component = name_parts[1]
                    else:
                        node.component = "1"

                    node.alias_name = drawib_aliasname_dict.get(draw_ib, "自定义名称")
                        
                    node.label = obj.name # 设置节点标题方便识别

                    # ----------------------
                    # 自动连线到 Group
                    # ----------------------
                    # 如果 Group 最后一个插槽已被占用，手动扩展一个
                    if group_node.inputs[-1].is_linked:
                        group_node.inputs.new('SSMTSocketObject', f"Input {len(group_node.inputs) + 1}")
                    
                    tree.links.new(node.outputs[0], group_node.inputs[-1])
                    
                    # 布局计算
                    count += 1
                    current_y -= y_gap
                    min_y = min(min_y, current_y)

        
        # 4. 放置 Group 和 Output 节点
        # 计算垂直中心大致位置
        final_center_y = min_y / 2 if count <= 5 else -200 # 简单估算
        
        group_node.location = (current_x + 400, final_center_y)

        output_node = tree.nodes.new('SSMTNode_Result_Output')
        output_node.location = (current_x + 800, final_center_y)
        output_node.label = "Generate Mod"
        
        # 连接 Group 到 Output
        if len(output_node.inputs) > 0 and len(group_node.outputs) > 0:
            tree.links.new(group_node.outputs[0], output_node.inputs[0])

        # 触发一次group node的更新（虽然脚本连线有时不需要，但为了保险起见）
        if hasattr(group_node, "update"):
             group_node.update()

        print(f"Blueprint {tree_name} updated with imported objects.")
        
    except Exception as e:
        print(f"Error generating blueprint nodes: {e}")
        import traceback
        traceback.print_exc()
    


class SSMTImportAllFromCurrentWorkSpaceBlueprint(bpy.types.Operator):
    bl_idname = "ssmt.import_all_from_workspace_blueprint"
    bl_label = TR.translate("一键导入当前工作空间内容(蓝图架构)")
    bl_description = "一键导入当前工作空间文件夹下所有的DrawIB对应的模型为SSMT蓝图架构"
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
    

class Import3DMigotoRaw(bpy.types.Operator, ImportHelper):
    """Import raw 3DMigoto vertex and index buffers"""
    bl_idname = "import_mesh.migoto_raw_buffers_mmt"
    bl_label = TR.translate("导入.fmt .ib .vb格式模型")
    bl_description = "导入3Dmigoto格式的 .ib .vb .fmt文件，只需选择.fmt文件即可"
    bl_options = {'REGISTER','UNDO'}

    # 我们只需要选择fmt文件即可，因为其它文件都是根据fmt文件的前缀来确定的。
    # 所以可以实现一个.ib 和 .vb文件存在多个数据类型描述的.fmt文件的导入。
    filename_ext = '.fmt'

    filter_glob: bpy.props.StringProperty(
        default='*.fmt',
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

        # 如果用户不选择任何fmt文件，则默认返回读取所有的fmt文件。
        import_filename_list = []
        if len(self.files) == 1:
            if str(self.filepath).endswith(".fmt"):
                import_filename_list.append(self.filepath)
            else:
                for filename in os.listdir(self.filepath):
                    if filename.endswith(".fmt"):
                        import_filename_list.append(filename)
        else:
            for fmt_file in self.files:
                import_filename_list.append(fmt_file.name)

        # 逐个fmt文件导入
        for fmt_file_name in import_filename_list:
            fmt_file_path = os.path.join(dirname, fmt_file_name)
            mbf = MigotoBinaryFile(fmt_path=fmt_file_path)
            MeshImportHelper.create_mesh_obj_from_mbf(mbf=mbf,import_collection=collection)

        # Select all objects under collection (因为用户习惯了导入后就是全部选中的状态). 
        CollectionUtils.select_collection_objects(collection)

        return {'FINISHED'}

def register():
    bpy.utils.register_class(Import3DMigotoRaw)
    bpy.utils.register_class(SSMTImportAllFromCurrentWorkSpaceBlueprint)


def unregister():
    bpy.utils.unregister_class(Import3DMigotoRaw)
    bpy.utils.unregister_class(SSMTImportAllFromCurrentWorkSpaceBlueprint)
