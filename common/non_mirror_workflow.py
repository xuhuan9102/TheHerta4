import bmesh
import bpy
from mathutils import Matrix, Vector

from ..utils.log_utils import LOG


class NonMirrorWorkflowHelper:
    _AXIS_VECTOR = (1.0, 0.0, 0.0)
    _WORKFLOW_MARKER_PROP = "_ssmt_non_mirror_workflow_processed"

    @classmethod
    def process_imported_objects(cls, imported_objects: list[bpy.types.Object]):
        cls._process_objects(imported_objects, stage_name="导入")

    @classmethod
    def iter_imported_objects(cls, imported_objects: list[bpy.types.Object]):
        yield from cls._iter_process_objects(imported_objects, stage_name="导入")

    @classmethod
    def restore_export_objects(cls, export_objects: list[bpy.types.Object]):
        cls._process_objects(export_objects, stage_name="导出前处理")

    @classmethod
    def _process_objects(cls, objects: list[bpy.types.Object], stage_name: str):
        for _ in cls._iter_process_objects(objects, stage_name):
            pass

    @classmethod
    def _iter_process_objects(cls, objects: list[bpy.types.Object], stage_name: str):
        processed_count = 0
        skipped_count = 0
        failed_count = 0

        for index, obj in enumerate(objects):
            yield index
            if not obj or obj.type != "MESH" or not getattr(obj, "data", None):
                skipped_count += 1
                continue

            try:
                cls._mirror_apply_and_flip(obj)
                if stage_name == "导入":
                    cls._mark_import_processed(obj)
                processed_count += 1
            except Exception as exc:
                failed_count += 1
                LOG.warning(
                    f"   ❌ {stage_name}执行非镜像工作流失败 "
                    f"{getattr(obj, 'name', '<unknown object>')}: {exc}"
                )

        LOG.info(
            f"   ✅ {stage_name}非镜像工作流: 成功 {processed_count} 个, "
            f"跳过 {skipped_count} 个, 失败 {failed_count} 个"
        )

    @classmethod
    def _mirror_apply_and_flip(cls, obj: bpy.types.Object):
        mesh = obj.data
        mirror_matrix = Matrix.Scale(-1.0, 4, cls._AXIS_VECTOR)
        source_corner_normals = cls._capture_corner_normals(mesh)

        cls._transform_mesh_with_shape_keys(mesh, mirror_matrix)

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
        cls._restore_mirrored_corner_normals(mesh, source_corner_normals, mirror_matrix)
        mesh.update()

    @classmethod
    def _transform_mesh_with_shape_keys(cls, mesh: bpy.types.Mesh, matrix: Matrix):
        # Blender 低版本没有 shape_keys=True 时，手动把所有 ShapeKey 顶点一起变换，避免镜像后基态和形态键错位。
        try:
            mesh.transform(matrix, shape_keys=True)
        except TypeError:
            mesh.transform(matrix)
            cls._transform_shape_key_blocks(mesh, matrix)

    @classmethod
    def _transform_shape_key_blocks(cls, mesh: bpy.types.Mesh, matrix: Matrix):
        shape_keys = getattr(mesh, "shape_keys", None)
        key_blocks = getattr(shape_keys, "key_blocks", None)
        if not key_blocks:
            return

        for key_block in key_blocks:
            for point in key_block.data:
                point.co = matrix @ point.co

    @classmethod
    def _mark_import_processed(cls, obj: bpy.types.Object):
        try:
            obj[cls._WORKFLOW_MARKER_PROP] = True
        except Exception:
            pass

    @classmethod
    def _should_restore_export_object(cls, obj: bpy.types.Object) -> bool:
        try:
            return bool(obj.get(cls._WORKFLOW_MARKER_PROP, False))
        except Exception:
            return False

    @classmethod
    def _capture_corner_normals(cls, mesh: bpy.types.Mesh) -> list[list[tuple[int, tuple[float, float, float]]]]:
        loops = getattr(mesh, "loops", None)
        polygons = getattr(mesh, "polygons", None)
        if not loops or not polygons:
            return []

        if hasattr(mesh, "calc_normals_split"):
            mesh.calc_normals_split()

        raw_normals = [0.0] * (len(loops) * 3)
        vertex_indices = [0] * len(loops)
        loops.foreach_get("normal", raw_normals)
        loops.foreach_get("vertex_index", vertex_indices)

        captured = []
        for polygon in polygons:
            polygon_normals = []
            for loop_index in polygon.loop_indices:
                normal_index = loop_index * 3
                polygon_normals.append((
                    vertex_indices[loop_index],
                    (
                        raw_normals[normal_index],
                        raw_normals[normal_index + 1],
                        raw_normals[normal_index + 2],
                    ),
                ))
            captured.append(polygon_normals)
        return captured

    @classmethod
    def _restore_mirrored_corner_normals(
        cls,
        mesh: bpy.types.Mesh,
        source_corner_normals: list[list[tuple[int, tuple[float, float, float]]]],
        mirror_matrix: Matrix,
    ):
        polygons = getattr(mesh, "polygons", None)
        loops = getattr(mesh, "loops", None)
        if not source_corner_normals or not polygons or not loops:
            return
        if len(source_corner_normals) != len(polygons):
            return

        normal_matrix = mirror_matrix.inverted_safe().transposed().to_3x3()
        mirrored_normals = [None] * len(loops)

        for polygon, source_normals in zip(polygons, source_corner_normals):
            remaining = list(source_normals)
            for loop_index in polygon.loop_indices:
                vertex_index = loops[loop_index].vertex_index
                source_index = next(
                    (index for index, item in enumerate(remaining) if item[0] == vertex_index),
                    None,
                )
                if source_index is None:
                    return
                _source_vertex_index, normal = remaining.pop(source_index)
                mirrored = normal_matrix @ Vector(normal)
                if mirrored.length_squared > 0.0:
                    mirrored.normalize()
                mirrored_normals[loop_index] = (mirrored.x, mirrored.y, mirrored.z)

        if any(normal is None for normal in mirrored_normals):
            return

        try:
            mesh.normals_split_custom_set(mirrored_normals)
        except Exception as exc:
            LOG.warning(
                f"   ⚠️ 非镜像工作流重建自定义法线失败 "
                f"{getattr(mesh, 'name', '<unknown mesh>')}: {exc}"
            )
