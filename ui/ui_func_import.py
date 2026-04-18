
'''
导入模型配置面板
'''
import os
import bpy

from bpy_extras.io_utils import ImportHelper

from ..utils.collection_utils import CollectionUtils
from ..utils.translate_utils import TR

from ..common.mesh_import_helper import MeshImportHelper, MigotoBinaryFile
from .ui_prefix_quick_ops import PrefixQuickOpsHelper


class Import3DMigotoRaw(bpy.types.Operator, ImportHelper):
    """Import raw 3DMigoto vertex and index buffers"""
    bl_idname = "import_mesh.migoto_raw_buffers_mmt"
    bl_label = TR.translate("导入.fmt .ib .vb格式模型")
    bl_description = "导入3Dmigoto格式的 .ib .vb .fmt文件，只需选择.fmt文件即可"
    bl_options = {'REGISTER','UNDO'}

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
        dirname = os.path.dirname(self.filepath)

        collection_name = os.path.basename(dirname)
        collection = bpy.data.collections.new(collection_name)
        bpy.context.scene.collection.children.link(collection)
        imported_objects = []

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

        for fmt_file_name in import_filename_list:
            fmt_file_path = os.path.join(dirname, fmt_file_name)
            mbf = MigotoBinaryFile(fmt_path=fmt_file_path)
            imported_obj = MeshImportHelper.create_mesh_obj_from_mbf(mbf=mbf, import_collection=collection)
            if imported_obj is not None:
                imported_objects.append(imported_obj)

        CollectionUtils.select_collection_objects(collection)
        PrefixQuickOpsHelper.merge_prefixes_from_objects(context, imported_objects)

        return {'FINISHED'}

def register():
    bpy.utils.register_class(Import3DMigotoRaw)


def unregister():
    bpy.utils.unregister_class(Import3DMigotoRaw)
