import bmesh
import bpy
from mathutils import Matrix

from ..utils.log_utils import LOG


class NonMirrorWorkflowHelper:
    _AXIS_VECTOR = (1.0, 0.0, 0.0)

    @classmethod
    def process_imported_objects(cls, imported_objects: list[bpy.types.Object]):
        cls._process_objects(imported_objects, stage_name="导入")

    @classmethod
    def restore_export_objects(cls, export_objects: list[bpy.types.Object]):
        cls._process_objects(export_objects, stage_name="导出前处理")

    @classmethod
    def _process_objects(cls, objects: list[bpy.types.Object], stage_name: str):
        processed_count = 0
        skipped_count = 0
        failed_count = 0

        for obj in objects:
            if not obj or obj.type != 'MESH' or not getattr(obj, "data", None):
                skipped_count += 1
                continue

            try:
                cls._mirror_apply_and_flip(obj)
                processed_count += 1
            except Exception as exc:
                failed_count += 1
                LOG.warning(f"   ❌ {stage_name}执行非镜像工作流失败 {getattr(obj, 'name', '<未知物体>')}: {exc}")

        LOG.info(
            f"   ✅ {stage_name}非镜像工作流: 成功 {processed_count} 个, "
            f"跳过 {skipped_count} 个, 失败 {failed_count} 个"
        )

    @classmethod
    def _mirror_apply_and_flip(cls, obj: bpy.types.Object):
        mesh = obj.data
        mirror_matrix = Matrix.Scale(-1.0, 4, cls._AXIS_VECTOR)

        mesh.transform(mirror_matrix)

        bm = bmesh.new()
        try:
            bm.from_mesh(mesh)
            if bm.faces:
                bmesh.ops.reverse_faces(bm, faces=list(bm.faces))
            bm.to_mesh(mesh)
        finally:
            bm.free()

        mesh.update()
        if hasattr(mesh, "calc_normals"):
            mesh.calc_normals()
