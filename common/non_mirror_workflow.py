import bmesh
import bpy
from mathutils import Matrix, Vector

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
        source_vertex_normals = cls._capture_vertex_normals(mesh)

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
        cls._restore_mirrored_vertex_normals(mesh, source_vertex_normals, mirror_matrix)
        mesh.update()

    @classmethod
    def _capture_vertex_normals(cls, mesh: bpy.types.Mesh) -> list[tuple[float, float, float]]:
        if len(mesh.vertices) == 0:
            return []

        if hasattr(mesh, "calc_normals"):
            mesh.calc_normals()

        raw_normals = [0.0] * (len(mesh.vertices) * 3)
        mesh.vertices.foreach_get("normal", raw_normals)

        return [
            (raw_normals[index], raw_normals[index + 1], raw_normals[index + 2])
            for index in range(0, len(raw_normals), 3)
        ]

    @classmethod
    def _restore_mirrored_vertex_normals(
        cls,
        mesh: bpy.types.Mesh,
        source_vertex_normals: list[tuple[float, float, float]],
        mirror_matrix: Matrix,
    ):
        if not source_vertex_normals or len(source_vertex_normals) != len(mesh.vertices):
            return

        normal_matrix = mirror_matrix.inverted_safe().transposed().to_3x3()
        mirrored_normals = []

        for normal in source_vertex_normals:
            mirrored = normal_matrix @ Vector(normal)
            if mirrored.length_squared > 0.0:
                mirrored.normalize()
            mirrored_normals.append((mirrored.x, mirrored.y, mirrored.z))

        try:
            mesh.normals_split_custom_set_from_vertices(mirrored_normals)
        except Exception as exc:
            LOG.warning(f"   ⚠️ 非镜像工作流重建自定义法线失败 {getattr(mesh, 'name', '<未知网格>')}: {exc}")
