import bpy
import numpy as np
import bmesh
import math
from ..utils.color_attribute_utils import read_color_attribute_data, write_color_attribute_data
from ..utils.vertex_color_utils import convert_color_srgb_to_linear, ensure_color_attribute

try:
    from bpy.props import FloatProperty
except Exception:
    FloatProperty = getattr(getattr(bpy, "props", None), "FloatProperty", lambda **_kwargs: None)


def _iter_face_convert_target_objects(context):
    if getattr(context, "mode", "") == 'EDIT_MESH':
        objects = getattr(context, "objects_in_mode_unique_data", None) or [getattr(context, "active_object", None)]
    else:
        objects = getattr(context, "selected_objects", []) or []

    for obj in objects:
        if obj is not None and getattr(obj, "type", "") == 'MESH':
            yield obj


def _get_active_uv_layer(bm):
    loop_layers = getattr(getattr(getattr(bm, "loops", None), "layers", None), "uv", None)
    return getattr(loop_layers, "active", None)


def _get_loop_uv(loop, uv_layer):
    uv_data = loop[uv_layer]
    uv = getattr(uv_data, "uv", uv_data)
    return (float(uv[0]), float(uv[1]))


def _uvs_equal(left_uv, right_uv, epsilon=1e-6):
    return abs(float(left_uv[0]) - float(right_uv[0])) <= epsilon and abs(float(left_uv[1]) - float(right_uv[1])) <= epsilon


def _get_face_edge_uv_map(face, edge, uv_layer):
    if uv_layer is None:
        return {}
    for loop in getattr(face, "loops", []) or []:
        if getattr(loop, "edge", None) != edge:
            continue
        next_loop = getattr(loop, "link_loop_next", None)
        if next_loop is None:
            return {}
        return {
            getattr(loop, "vert", None): _get_loop_uv(loop, uv_layer),
            getattr(next_loop, "vert", None): _get_loop_uv(next_loop, uv_layer),
        }
    return {}


def _is_uv_continuous_across_edge(edge, uv_layer):
    if uv_layer is None:
        return True
    linked_faces = list(getattr(edge, "link_faces", []) or [])
    if len(linked_faces) != 2:
        return False
    face_uv_maps = [_get_face_edge_uv_map(face, edge, uv_layer) for face in linked_faces]
    for vert in getattr(edge, "verts", []) or []:
        left_uv = face_uv_maps[0].get(vert)
        right_uv = face_uv_maps[1].get(vert)
        if left_uv is None or right_uv is None:
            return False
        if not _uvs_equal(left_uv, right_uv):
            return False
    return True


def _collect_uv_islands(faces, uv_layer):
    face_set = set(faces)
    if not face_set:
        return []
    if uv_layer is None:
        return [face_set]

    remaining = set(face_set)
    islands = []
    while remaining:
        seed = remaining.pop()
        island = {seed}
        stack = [seed]
        while stack:
            face = stack.pop()
            for edge in getattr(face, "edges", []) or []:
                if not _is_uv_continuous_across_edge(edge, uv_layer):
                    continue
                for linked_face in getattr(edge, "link_faces", []) or []:
                    if linked_face not in remaining or linked_face not in face_set:
                        continue
                    remaining.remove(linked_face)
                    island.add(linked_face)
                    stack.append(linked_face)
        islands.append(island)
    return islands


def _triangle_pair_merge_score(face_a, face_b, edge):
    normal_score = 0.0
    normal_a = getattr(face_a, "normal", None)
    normal_b = getattr(face_b, "normal", None)
    if normal_a is not None and normal_b is not None:
        try:
            normal_score = float(normal_a.normalized().dot(normal_b.normalized()))
        except Exception:
            normal_score = 0.0

    edge_length = 0.0
    calc_length = getattr(edge, "calc_length", None)
    if callable(calc_length):
        try:
            edge_length = float(calc_length())
        except Exception:
            edge_length = 0.0
    return (normal_score, edge_length)


def _pick_triangle_pair_edges_for_dissolve(faces, uv_layer):
    candidate_edges = []
    for island_faces in _collect_uv_islands(faces, uv_layer):
        triangle_faces = {face for face in island_faces if len(getattr(face, "verts", []) or []) == 3}
        seen_edges = set()
        scored_edges = []
        for face in triangle_faces:
            for edge in getattr(face, "edges", []) or []:
                if edge in seen_edges:
                    continue
                seen_edges.add(edge)
                linked_faces = [linked_face for linked_face in getattr(edge, "link_faces", []) or [] if linked_face in triangle_faces]
                if len(linked_faces) != 2:
                    continue
                face_a, face_b = linked_faces
                if len(set(getattr(face_a, "verts", []) or []) | set(getattr(face_b, "verts", []) or [])) != 4:
                    continue
                if getattr(face_a, "material_index", 0) != getattr(face_b, "material_index", 0):
                    continue
                if getattr(face_a, "smooth", True) != getattr(face_b, "smooth", True):
                    continue
                if uv_layer is not None and not _is_uv_continuous_across_edge(edge, uv_layer):
                    continue
                scored_edges.append((_triangle_pair_merge_score(face_a, face_b, edge), edge, face_a, face_b))

        used_faces = set()
        for _score, edge, face_a, face_b in sorted(scored_edges, key=lambda item: item[0], reverse=True):
            if face_a in used_faces or face_b in used_faces:
                continue
            used_faces.add(face_a)
            used_faces.add(face_b)
            candidate_edges.append(edge)
    return candidate_edges


def _convert_tris_to_quads_in_bmesh(bm, selected_only):
    bm.faces.ensure_lookup_table()
    target_faces = [face for face in bm.faces if len(face.verts) == 3 and (face.select or not selected_only)]
    if not target_faces:
        return 0
    dissolve_edges = _pick_triangle_pair_edges_for_dissolve(target_faces, _get_active_uv_layer(bm))
    if not dissolve_edges:
        return 0
    bmesh.ops.dissolve_edges(bm, edges=dissolve_edges, use_verts=False, use_face_split=False)
    return len(dissolve_edges)


def _triangulate_faces_in_bmesh(bm, selected_only):
    bm.faces.ensure_lookup_table()
    target_faces = [face for face in bm.faces if len(face.verts) > 3 and (face.select or not selected_only)]
    if not target_faces:
        return 0
    try:
        bmesh.ops.triangulate(bm, faces=target_faces, quad_method='BEAUTY', ngon_method='BEAUTY')
    except TypeError:
        bmesh.ops.triangulate(bm, faces=target_faces)
    return len(target_faces)


def _apply_face_converter_to_object(obj, converter, selected_only, edit_mode):
    if edit_mode:
        bm = bmesh.from_edit_mesh(obj.data)
        affected_faces = converter(bm, selected_only)
        if affected_faces > 0:
            bmesh.update_edit_mesh(obj.data, loop_triangles=False, destructive=False)
        return affected_faces

    bm = bmesh.new()
    try:
        bm.from_mesh(obj.data)
        affected_faces = converter(bm, False)
        if affected_faces > 0:
            bm.to_mesh(obj.data)
            obj.data.update()
        return affected_faces
    finally:
        bm.free()


def _run_face_converter(context, converter):
    edit_mode = getattr(context, "mode", "") == 'EDIT_MESH'
    processed_objects = 0
    affected_faces = 0
    for obj in _iter_face_convert_target_objects(context):
        current_affected = _apply_face_converter_to_object(
            obj=obj,
            converter=converter,
            selected_only=edit_mode,
            edit_mode=edit_mode,
        )
        if current_affected > 0:
            processed_objects += 1
            affected_faces += current_affected
    return processed_objects, affected_faces


def _has_face_convert_target(context):
    return any(True for _obj in _iter_face_convert_target_objects(context))


def _iter_selected_mesh_objects(context):
    for obj in getattr(context, "selected_objects", []) or []:
        if obj is not None and getattr(obj, "type", "") == "MESH":
            yield obj


def _object_has_shape_keys(obj):
    shape_keys = getattr(getattr(obj, "data", None), "shape_keys", None)
    key_blocks = getattr(shape_keys, "key_blocks", None)
    return bool(key_blocks)


def _create_temp_subsurf_modifier(obj, levels=1):
    modifiers = getattr(obj, "modifiers", None)
    if modifiers is None or not hasattr(modifiers, "new"):
        return None
    modifier = modifiers.new(name="TH4_TempLimitSurface", type='SUBSURF')
    if modifier is None:
        return None
    modifier.subdivision_type = 'CATMULL_CLARK'
    modifier.levels = int(levels)
    modifier.render_levels = int(levels)
    if hasattr(modifier, "quality"):
        modifier.quality = max(3, int(getattr(modifier, "quality", 3) or 3))
    if hasattr(modifier, "use_limit_surface"):
        modifier.use_limit_surface = True
    return modifier


def _build_temp_limit_surface_object(context, obj, levels=1):
    object_name = str(getattr(obj, "name", "") or "").strip() or "TH4_LimitSurface"
    source_mesh = getattr(obj, "data", None)
    if source_mesh is None or not hasattr(source_mesh, "copy"):
        return None, None

    temp_mesh = source_mesh.copy()
    temp_mesh.name = f"{object_name}_TempLimitSurfaceMesh"

    object_factory = getattr(getattr(bpy, "data", None), "objects", None)
    temp_obj = None
    if object_factory is not None and hasattr(object_factory, "new"):
        temp_obj = object_factory.new(f"{object_name}_TempLimitSurface", temp_mesh)
    else:
        return None, temp_mesh

    temp_obj.matrix_world = getattr(obj, "matrix_world", None)
    temp_obj.parent = None
    for modifier in reversed(list(getattr(temp_obj, "modifiers", []) or [])):
        _remove_modifier(temp_obj, modifier)
    _create_temp_subsurf_modifier(temp_obj, levels=levels)
    collection = getattr(context, "collection", None) or getattr(getattr(context, "scene", None), "collection", None)
    if collection is not None and hasattr(getattr(collection, "objects", None), "link"):
        try:
            collection.objects.link(temp_obj)
        except Exception:
            pass
    return temp_obj, temp_mesh


def _remove_modifier(obj, modifier):
    modifiers = getattr(obj, "modifiers", None)
    if modifiers is None or modifier is None:
        return
    remove = getattr(modifiers, "remove", None)
    if callable(remove):
        remove(modifier)


def _new_mesh_from_evaluated_object(context, obj):
    depsgraph = context.evaluated_depsgraph_get()
    evaluated_obj = obj.evaluated_get(depsgraph)
    meshes = getattr(getattr(bpy, "data", None), "meshes", None)
    new_from_object = getattr(meshes, "new_from_object", None)
    if callable(new_from_object):
        for kwargs in (
            {"preserve_all_data_layers": True, "depsgraph": depsgraph},
            {"depsgraph": depsgraph},
            {},
        ):
            try:
                return new_from_object(evaluated_obj, **kwargs)
            except TypeError:
                continue
    raise RuntimeError("unable_to_create_evaluated_mesh")


def _replace_object_mesh_data(obj, new_mesh):
    old_mesh = getattr(obj, "data", None)
    if old_mesh is None or new_mesh is None:
        return False
    old_name = getattr(old_mesh, "name", "")
    if old_name and hasattr(new_mesh, "name"):
        new_mesh.name = old_name
    obj.data = new_mesh
    remove_mesh = getattr(getattr(getattr(bpy, "data", None), "meshes", None), "remove", None)
    if callable(remove_mesh) and old_mesh is not None and getattr(old_mesh, "users", 0) <= 0:
        try:
            remove_mesh(old_mesh)
        except Exception:
            pass
    return True


def _bake_limit_surface_from_temp_subsurf(context, obj, levels=1):
    temp_obj, temp_mesh = _build_temp_limit_surface_object(context, obj, levels=levels)
    if temp_obj is None:
        return False, 0, 0, "no_temp_object"
    try:
        baked_mesh = _new_mesh_from_evaluated_object(context, temp_obj)
        if baked_mesh is None:
            return False, 0, 0, "no_evaluated_mesh"
        vertex_count = len(getattr(baked_mesh, "vertices", []) or [])
        polygon_count = len(getattr(baked_mesh, "polygons", []) or [])
        if not _replace_object_mesh_data(obj, baked_mesh):
            return False, 0, 0, "replace_mesh_failed"
        if hasattr(obj.data, "update"):
            obj.data.update()
        return True, vertex_count, polygon_count, ""
    except Exception as exc:
        return False, 0, 0, str(exc)
    finally:
        if temp_obj is not None:
            try:
                bpy.data.objects.remove(temp_obj, do_unlink=True)
            except Exception:
                pass
        elif temp_mesh is not None:
            remove_mesh = getattr(getattr(getattr(bpy, "data", None), "meshes", None), "remove", None)
            if callable(remove_mesh):
                try:
                    remove_mesh(temp_mesh)
                except Exception:
                    pass


def srgb_to_linear(srgb_value):
    """将 SRGB 值转换为线性值"""
    if srgb_value <= 0.04045:
        return srgb_value / 12.92
    else:
        return math.pow((srgb_value + 0.055) / 1.055, 2.4)


def linear_to_srgb(linear_value):
    """将线性值转换为 SRGB 值"""
    if linear_value <= 0.0031308:
        return linear_value * 12.92
    else:
        return math.pow(linear_value, 1.0 / 2.4) * 1.055 - 0.055


def convert_color_srgb_to_linear(color_rgba):
    """将 RGBA 颜色从 SRGB 空间转换到线性空间"""
    return [
        srgb_to_linear(color_rgba[0]),
        srgb_to_linear(color_rgba[1]),
        srgb_to_linear(color_rgba[2]),
        color_rgba[3]
    ]


class BMTP_OT_DynamicBridge(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_dynamic_bridge"
    bl_label = "动态桥接循环边"
    bl_description = "桥接两个顶点数不同的循环边，自动处理顶点数不匹配的情况"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return context.active_object and context.active_object.type == 'MESH' and context.mode == 'EDIT_MESH'

    def execute(self, context):
        props = context.scene.bmtp_props
        
        obj = context.active_object
        
        bm = bmesh.from_edit_mesh(obj.data)
        
        selected_edges = [e for e in bm.edges if e.select]
        
        if len(selected_edges) < 2:
            self.report({'ERROR'}, "请至少选择两条边")
            return {'CANCELLED'}
            
        bpy.ops.mesh.select_all(action='DESELECT')
        for e in selected_edges:
            e.select = True
            
        try:
            bpy.ops.mesh.bridge_edge_loops(
                number_cuts=props.bridge_segments,
                interpolation='LINEAR',
                smoothness=props.bridge_smooth
            )
            self.report({'INFO'}, f"已动态桥接循环边，分段数: {props.bridge_segments}")
        except Exception as e:
            self.report({'ERROR'}, f"桥接失败: {str(e)}")
            return {'CANCELLED'}
        
        bmesh.update_edit_mesh(obj.data)
        
        return {'FINISHED'}


class BMTP_OT_TrisToQuadsPreserveUV(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_tris_to_quads_preserve_uv"
    bl_label = "Tris to Quads (UV Island Safe)"
    bl_description = "Convert triangles to quads without crossing active UV island boundaries"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return getattr(context, "mode", "") in {'OBJECT', 'EDIT_MESH'} and _has_face_convert_target(context)

    def execute(self, context):
        processed_objects, affected_faces = _run_face_converter(context, _convert_tris_to_quads_in_bmesh)
        if processed_objects <= 0:
            self.report({'WARNING'}, "No triangle pairs were merged")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Merged {affected_faces} triangle pairs on {processed_objects} object(s)")
        return {'FINISHED'}


class BMTP_OT_QuadsToTris(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_quads_to_tris"
    bl_label = "Quads to Tris"
    bl_description = "Triangulate selected quads and ngons"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return getattr(context, "mode", "") in {'OBJECT', 'EDIT_MESH'} and _has_face_convert_target(context)

    def execute(self, context):
        processed_objects, affected_faces = _run_face_converter(context, _triangulate_faces_in_bmesh)
        if processed_objects <= 0:
            self.report({'WARNING'}, "No quads or ngons were triangulated")
            return {'CANCELLED'}
        self.report({'INFO'}, f"Triangulated {affected_faces} face(s) on {processed_objects} object(s)")
        return {'FINISHED'}


class BMTP_OT_EnableSubdivisionLimitSurface(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_enable_subdivision_limit_surface"
    bl_label = "应用极限表面"
    bl_description = "临时执行 Catmull-Clark 表面细分并启用极限表面，再将结果直接烘焙回网格"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return getattr(context, "mode", "") == 'OBJECT' and any(True for _obj in _iter_selected_mesh_objects(context))

    def execute(self, context):
        props = getattr(getattr(context, "scene", None), "bmtp_props", None)
        levels = int(getattr(props, "limit_surface_subdiv_levels", 2) or 2)
        processed_objects = 0
        baked_vertices = 0
        baked_faces = 0
        skipped_shape_key_objects = []
        failed_objects = []

        for obj in _iter_selected_mesh_objects(context):
            if _object_has_shape_keys(obj):
                skipped_shape_key_objects.append(getattr(obj, "name", ""))
                continue

            success, vertex_count, polygon_count, _error = _bake_limit_surface_from_temp_subsurf(context, obj, levels=levels)
            if not success:
                failed_objects.append(getattr(obj, "name", ""))
                continue

            processed_objects += 1
            baked_vertices += vertex_count
            baked_faces += polygon_count

        if processed_objects <= 0:
            if skipped_shape_key_objects and not failed_objects:
                self.report({'WARNING'}, "No object was baked; shape-key objects were skipped")
            else:
                self.report({'WARNING'}, "No object was baked to the limit surface")
            return {'CANCELLED'}

        message = (
            f"Baked limit surface on {processed_objects} object(s), "
            f"{baked_vertices} vertices, {baked_faces} faces, level {levels}"
        )
        if skipped_shape_key_objects:
            message += f"; skipped {len(skipped_shape_key_objects)} shape-key object(s)"
        if failed_objects:
            message += f"; failed {len(failed_objects)} object(s)"
        self.report({'INFO'}, message)
        return {'FINISHED'}


def _collect_edit_selection_indices(bm, attr_domain):
    """在编辑模式下只读收集选中元素，返回 (顶点索引集合, 面索引集合)。

    POINT 域只使用顶点索引；CORNER 域优先使用选中面，未选中面时退回
    选中顶点（由调用方在对象模式下还原为对应 loop）。
    """
    bm.verts.ensure_lookup_table()
    bm.faces.ensure_lookup_table()

    selected_verts = {v.index for v in bm.verts if v.select}
    selected_faces = set()
    if attr_domain == 'CORNER':
        selected_faces = {f.index for f in bm.faces if f.select}

    return selected_verts, selected_faces


def _build_element_selection_mask(mesh, attr_domain, element_count, selected_verts, selected_faces):
    """把编辑模式选中的顶点/面映射为网格颜色数据的布尔掩码。"""
    mask = np.zeros(element_count, dtype=bool)

    if attr_domain == 'POINT':
        if selected_verts:
            indices = np.fromiter(selected_verts, dtype=np.int64, count=len(selected_verts))
            indices = indices[(indices >= 0) & (indices < element_count)]
            mask[indices] = True
        return mask

    # CORNER 域
    if selected_faces:
        polygon_count = len(mesh.polygons)
        if polygon_count:
            loop_start = np.empty(polygon_count, dtype=np.int32)
            loop_total = np.empty(polygon_count, dtype=np.int32)
            mesh.polygons.foreach_get('loop_start', loop_start)
            mesh.polygons.foreach_get('loop_total', loop_total)
            for face_index in selected_faces:
                if 0 <= face_index < polygon_count:
                    start = int(loop_start[face_index])
                    total = int(loop_total[face_index])
                    mask[start:start + total] = True
        return mask

    if selected_verts:
        loop_vertex_index = np.empty(element_count, dtype=np.int32)
        mesh.loops.foreach_get('vertex_index', loop_vertex_index)
        selected_array = np.fromiter(selected_verts, dtype=np.int64, count=len(selected_verts))
        mask = np.isin(loop_vertex_index, selected_array)

    return mask


def _apply_vertex_color_object_mode(
    mesh,
    vc_mode,
    attr_name,
    attr_domain,
    attr_data_type,
    color_rgba_srgb,
    selected_verts=None,
    selected_faces=None,
):
    """在对象模式下写入顶点色，只操作 color_attributes，绝不触碰 UV 层。

    selected_verts / selected_faces 任一非 None 表示部分写入（编辑模式）；
    否则写入整个网格。
    """
    is_partial = selected_verts is not None or selected_faces is not None

    if vc_mode == 'FULL_COLOR':
        # 设计要求：FULL_COLOR 清空原有所有顶点色，只保留本次指定的颜色属性。
        # 注意：Blender 在移除一个自定义数据层后会移动其余层，之前通过
        # list()/遍历拿到的旧 Attribute 包装器会变成悬垂指针，直接移除会误删
        # UV 等其他自定义数据层。因此必须按名称逐个重新获取后再移除，并且
        # 先无条件清空 active_color（对象相等比较对 RNA 包装器不可靠）。
        mesh.color_attributes.active_color = None
        for old_name in [a.name for a in mesh.color_attributes]:
            old_attr = mesh.color_attributes.get(old_name)
            if old_attr is not None:
                mesh.color_attributes.remove(old_attr)

    color_attr = ensure_color_attribute(
        color_attributes=mesh.color_attributes,
        attr_name=attr_name,
        attr_domain=attr_domain,
        attr_data_type=attr_data_type,
    )

    if attr_domain == 'CORNER':
        element_count = len(mesh.loops)
    else:
        element_count = len(mesh.vertices)

    # Blender 5.0 防御：新建/复用后属性数据可能尚未分配（data 长度为 0）。
    if len(color_attr.data) != element_count:
        mesh.update()
        if len(color_attr.data) != element_count:
            mesh.color_attributes.remove(color_attr)
            color_attr = ensure_color_attribute(
                color_attributes=mesh.color_attributes,
                attr_name=attr_name,
                attr_domain=attr_domain,
                attr_data_type=attr_data_type,
            )
    if len(color_attr.data) != element_count:
        raise ValueError(f"颜色属性 '{attr_name}' 数据长度异常，无法写入")

    rgba = np.clip(np.asarray(color_rgba_srgb, dtype=np.float32), 0.0, 1.0)
    if attr_data_type == 'FLOAT_COLOR':
        rgba = convert_color_srgb_to_linear(rgba)

    existing = read_color_attribute_data(color_attr, element_count)

    if vc_mode == 'FULL_COLOR':
        if is_partial:
            final = np.zeros((element_count, 4), dtype=np.float32)
            final[:, 3] = 1.0
            mask = _build_element_selection_mask(
                mesh, attr_domain, element_count, selected_verts, selected_faces
            )
            final[mask] = rgba
        else:
            final = np.tile(rgba, (element_count, 1))
    else:  # ALPHA_ONLY
        final = existing.copy()
        if is_partial:
            mask = _build_element_selection_mask(
                mesh, attr_domain, element_count, selected_verts, selected_faces
            )
            final[mask, 3] = rgba[3]
        else:
            final[:, 3] = rgba[3]

    write_color_attribute_data(color_attr, final)
    mesh.color_attributes.active_color = color_attr
    mesh.update()


class BMTP_OT_SetVertexColor(bpy.types.Operator):
    """为选中网格物体设置顶点色颜色属性"""
    bl_idname = "toolkit.bmtp_set_vertex_color"
    bl_label = "应用顶点色"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        props = context.scene.bmtp_props
        is_edit_mode = getattr(context, "mode", "") == 'EDIT_MESH'

        if is_edit_mode:
            obj = getattr(context, "edit_object", None)
            if obj is None or obj.type != 'MESH':
                self.report({'ERROR'}, "请选择至少一个网格物体")
                return {'CANCELLED'}
            target_objects = [obj]
        else:
            target_objects = [o for o in context.selected_objects if o.type == 'MESH']
            if not target_objects:
                self.report({'ERROR'}, "请选择至少一个网格物体")
                return {'CANCELLED'}

        attr_name = props.vc_attr_name.strip() or "COLOR"
        attr_domain = props.vc_attr_domain
        attr_data_type = props.vc_attr_data_type
        vc_mode = props.vc_mode
        color_rgba_srgb = np.asarray(props.vc_color[:], dtype=np.float32)

        processed = 0
        for obj in target_objects:
            mesh = obj.data
            selected_verts = None
            selected_faces = None
            switched_to_object = False

            if is_edit_mode:
                # 只读收集选中元素，随后切到对象模式写颜色属性，避免在 bmesh 中
                # 新建颜色层并通过 destructive update 回写导致 UV 层丢失。
                bm = bmesh.from_edit_mesh(mesh)
                try:
                    selected_verts, selected_faces = _collect_edit_selection_indices(bm, attr_domain)
                finally:
                    bm.free()

                if not selected_verts and not selected_faces:
                    self.report({'WARNING'}, f"对象 '{obj.name}' 没有选中的元素，已跳过")
                    continue

                bpy.ops.object.mode_set(mode='OBJECT')
                switched_to_object = True

            try:
                _apply_vertex_color_object_mode(
                    mesh=mesh,
                    vc_mode=vc_mode,
                    attr_name=attr_name,
                    attr_domain=attr_domain,
                    attr_data_type=attr_data_type,
                    color_rgba_srgb=color_rgba_srgb,
                    selected_verts=selected_verts,
                    selected_faces=selected_faces,
                )
                processed += 1
            except ValueError as exc:
                self.report({'ERROR'}, str(exc))
            finally:
                if switched_to_object:
                    bpy.ops.object.mode_set(mode='EDIT')

        self.report({'INFO'}, f"顶点色操作完成，处理了 {processed} 个对象")
        return {'FINISHED'}


class BMTP_OT_DeleteEmptyMeshes(bpy.types.Operator):
    """删除选中物体中没有面的空网格"""
    bl_idname = "toolkit.bmtp_delete_empty_meshes"
    bl_label = "删除选中物体的空网格"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        objects_to_delete = [obj for obj in context.selected_objects if obj.type == 'MESH' and (not obj.data or not obj.data.polygons)]
        
        if objects_to_delete:
            count = len(objects_to_delete)
            for obj in objects_to_delete:
                bpy.data.objects.remove(obj, do_unlink=True)
            self.report({'INFO'}, f"删除了 {count} 个没有面的选中网格对象")
        else:
            self.report({'INFO'}, "选中的对象中没有找到没有面的网格对象")
        return {'FINISHED'}


class BMTP_OT_SyncDataNames(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_sync_data_names"
    bl_label = "同步选中物体数据块名称"
    bl_options = {'REGISTER', 'UNDO'}
    
    @classmethod
    def poll(cls, context):
        return True
    
    def execute(self, context):
        renamed_count = 0
        for obj in context.selected_objects:
            if obj.data and obj.data.name != obj.name:
                try:
                    obj.data.name = obj.name
                    renamed_count += 1
                except Exception: pass
        self.report({'INFO'}, f"操作完成，同步了 {renamed_count} 个选中物体的数据块名称。")
        return {'FINISHED'}


class BMTP_OT_CleanUselessShapeKeys(bpy.types.Operator):
    """清理选中物体中没有效果的形态键（所有顶点与基础键相同），并删除内容完全相同的重复形态键（只保留第一个）"""
    bl_idname = "toolkit.bmtp_clean_useless_shape_keys"
    bl_label = "清理选中物体的无效形态键"
    bl_description = "清理选中物体中没有效果的形态键（顶点位置与基础形态键几乎一致）；对于顶点位置几乎相同的多个形态键，只保留第一个"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        total_removed = 0
        total_duplicates_removed = 0
        processed_objects = 0
        threshold = float(context.scene.bmtp_props.shapekey_cleanup_threshold)
        
        for obj in context.selected_objects:
            if obj.type != 'MESH':
                continue
                
            if not obj.data.shape_keys:
                continue
                
            shape_keys = obj.data.shape_keys.key_blocks
            if len(shape_keys) <= 1:
                continue
                
            basis_key = shape_keys[0]
            vertex_count = len(basis_key.data)
            if vertex_count == 0:
                continue
            
            basis_coords = np.empty(vertex_count * 3, dtype=np.float32)
            basis_key.data.foreach_get("co", basis_coords)
            
            keys_to_remove = []  # (shape_key, kind) kind in {"useless", "duplicate"}
            kept_signatures = []  # list of np.ndarray, 已保留的非 basis 形态键坐标
            
            for i, shape_key in enumerate(shape_keys):
                if i == 0:
                    continue
                
                coords = np.empty(vertex_count * 3, dtype=np.float32)
                shape_key.data.foreach_get("co", coords)
                
                # 与基础键完全相同 → 无效形态键
                if np.max(np.abs(coords - basis_coords)) <= threshold:
                    keys_to_remove.append((shape_key, "useless"))
                    continue
                
                # 与已保留的某个形态键完全相同 → 重复形态键，跳过
                is_duplicate = False
                for kept_coords in kept_signatures:
                    if np.max(np.abs(coords - kept_coords)) <= threshold:
                        is_duplicate = True
                        break
                
                if is_duplicate:
                    keys_to_remove.append((shape_key, "duplicate"))
                    continue
                
                kept_signatures.append(coords)
            
            removed_useless = 0
            removed_duplicates = 0
            for shape_key, kind in keys_to_remove:
                obj.shape_key_remove(shape_key)
                if kind == "useless":
                    removed_useless += 1
                else:
                    removed_duplicates += 1
            
            total_removed += removed_useless
            total_duplicates_removed += removed_duplicates
            
            if keys_to_remove:
                processed_objects += 1
        
        bpy.ops.outliner.orphans_purge(do_local_ids=True, do_linked_ids=True, do_recursive=True)
        
        if total_removed > 0 or total_duplicates_removed > 0:
            self.report(
                {'INFO'},
                f"已从 {processed_objects} 个选中物体中删除 {total_removed} 个无效形态键、{total_duplicates_removed} 个重复形态键"
            )
        else:
            self.report({'INFO'}, "选中的物体中未找到无效或重复的形态键")
        return {'FINISHED'}


# ======================================================================
# 反细分＆细分（UV防错乱）
# ======================================================================
def _us_restore_seams(bm, original_seams):
    """按顶点索引对还原缝合线；新增边一律清 seam（不依赖遍历顺序）"""
    bm.edges.ensure_lookup_table()
    bm.verts.ensure_lookup_table()
    for edge in bm.edges:
        try:
            a, b = edge.verts[0].index, edge.verts[1].index
        except Exception:
            continue
        key = (a, b) if a < b else (b, a)
        edge.seam = original_seams.get(key, False)


def _us_fix_t_junctions(bm, enabled, only_selected, max_connections=20000):
    """消除处理区与保留区交界处的 T 型顶点，细分与反细分共用同一实现。

    - enabled=False       -> 完全不动，等价原生行为
    - only_selected=True  -> 只在"保留区（未处理）多边形"这一侧吞并 T 点，绝不跨进保留区
    - only_selected=False -> 全网格范围扫描
    """
    if not enabled:
        return 0

    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    bm.verts.index_update()

    selected_faces = [face for face in bm.faces if face.select]
    partial = bool(only_selected) and len(selected_faces) < len(bm.faces)
    if partial:
        protected_faces = [face for face in bm.faces if not face.select]
    else:
        protected_faces = list(bm.faces)

    connections = []   # [(t_vert, target_vert, face)]
    face_normals = {}  # face -> 连接前法线（用于纠正翻面）

    # ---------- 阶段 1：先全部识别，再统一连接（面结构未变时识别最可靠）----------
    for face in protected_faces:
        if len(face.verts) <= 3:
            continue

        protected_verts = set(face.verts)

        # 该面内与"处理区"相邻的边 = 边界边
        boundary_edges = set()
        for edge in face.edges:
            for other_face in edge.link_faces:
                if other_face is face:
                    continue
                if partial:
                    if other_face.select:
                        boundary_edges.add(edge)
                        break
                else:
                    if not set(other_face.verts) <= protected_verts:
                        boundary_edges.add(edge)
                        break

        if len(boundary_edges) < 2:
            continue

        center = face.calc_center_median()
        try:
            radius = max((v.co - center).length for v in face.verts)
        except ValueError:
            continue
        if radius <= 0.0:
            continue

        face_edges = set(face.edges)
        for vert in face.verts:
            in_face_edges = [e for e in vert.link_edges if e in face_edges]
            if len([e for e in in_face_edges if e in boundary_edges]) != 2:
                continue

            neighbors = [e.other_vert(vert) for e in in_face_edges]
            neighbors = [n for n in neighbors if n is not None and n is not vert]
            if len(neighbors) < 2:
                continue

            # 真正的对面边：不含 T 点、且不含 T 点邻居的边
            opp_edges = [
                e for e in face.edges
                if vert not in e.verts and not any(n in e.verts for n in neighbors)
            ]
            if not opp_edges:
                continue

            target_edge = opp_edges[0]
            v1, v2 = target_edge.verts
            if v1 is vert or v2 is vert:
                continue

            # 取离 T 点更近的那个端点，且只接受落在面内的连接
            target_vert = v1 if (v1.co - vert.co).length <= (v2.co - vert.co).length else v2

            if (target_vert.co - center).length > radius * 1.05:
                continue
            nearest_neighbor = min((vert.co - n.co).length for n in neighbors)
            if (vert.co - target_vert.co).length <= nearest_neighbor * 0.5:
                continue
            if bm.edges.get((vert, target_vert)) is not None:
                continue

            connections.append((vert, target_vert, face))
            face_normals[face] = face.normal.copy()
            break  # 每个面最多处理一个 T 点，避免边连接边改结构

    # ---------- 阶段 2：统一执行连接 ----------
    done_pairs = set()
    connected = 0
    skipped_degenerate = 0
    for t_vert, target_vert, face in connections:
        if connected >= max_connections:
            break
        if not (getattr(t_vert, "is_valid", False) and getattr(target_vert, "is_valid", False)):
            continue
        try:
            pair = tuple(sorted((t_vert.index, target_vert.index)))
        except Exception:
            continue
        if pair in done_pairs:
            continue
        done_pairs.add(pair)

        try:
            result = bmesh.ops.connect_verts(bm, verts=[t_vert, target_vert])
        except Exception:
            result = None
        new_edges = list((result or {}).get('edges', []) or [])
        if not new_edges:
            continue

        # 结果校验：新边至少要被两个面共用，否则说明连接退化，撤销掉
        bad_edges = [e for e in new_edges if len(e.link_faces) < 2]
        if bad_edges:
            try:
                bmesh.ops.delete(bm, geom=bad_edges, context='EDGES')
            except Exception:
                pass
            skipped_degenerate += 1
            continue

        # 纠正可能被 connect_verts 翻掉的面法线
        before_normal = face_normals.get(face)
        if getattr(face, "is_valid", False) and before_normal is not None:
            try:
                if before_normal.length > 0.0 and face.normal.length > 0.0 and before_normal.dot(face.normal) < 0.0:
                    face.normal_flip()
                bmesh.ops.recalc_face_normals(bm, faces=[face])
            except Exception:
                pass

        connected += 1

    bm.verts.ensure_lookup_table()
    bm.edges.ensure_lookup_table()
    bm.faces.ensure_lookup_table()
    return connected


def _us_mark_seams_from_islands_in_bmesh(bm, original_seam_keys=None):
    """纯 bmesh 的 UV 孤岛检测兜底。

    bpy.ops.uv.seams_from_islands 依赖 UV/3D 视图上下文，在无 UI 上下文
    （批处理、部分脚本调用）时会直接 CANCELLED，导致反细分永远拿不到孤岛边界。

    语义对齐原生算子（原生会把 UV 孤岛的边界边标成 seam，含网格外边界）：
    - 该边两侧面相接处 UV 不连续 => 孤岛边界；
    - 该边本来就是用户标好的 seam（调用方在 clear seam 之前把原始 seam 键传进来）
      => 仍是孤岛边界，而 clear seam 后该信息就没了，必须靠调用方传入；
    - 只有 1 个邻面的网格外边界 => 也是孤岛边界（实测：不算的话反细分会没动作）。

    实测依据（5x5 网格带中缝、全选细分 cuts=2 后反细分）：
    外边界不算 seam -> 256 顶点一个都没去掉；算 seam -> 256→122（撤销 134 个），
    且"边界保护圈=1"时 256→159，圈数确实控制接缝附近的保留量。
    """
    uv_layer = _get_active_uv_layer(bm)
    original_seam_keys = original_seam_keys or set()
    marked = 0
    bm.edges.ensure_lookup_table()
    bm.verts.ensure_lookup_table()
    for edge in bm.edges:
        linked = list(getattr(edge, "link_faces", []) or [])
        if len(linked) != 2:
            # 网格外边界：算孤岛边界（保护外圈，否则反细分会没动作）
            edge.seam = True
            marked += 1
            continue
        try:
            a, b = edge.verts[0].index, edge.verts[1].index
            key = (a, b) if a < b else (b, a)
        except Exception:
            key = None
        if edge.seam or (key is not None and key in original_seam_keys):
            edge.seam = True
            marked += 1
            continue
        if uv_layer is not None and not _is_uv_continuous_across_edge(edge, uv_layer):
            edge.seam = True
            marked += 1
    return marked


def _us_opposite_edge(face, edge):
    """四边形中与给定边相对（无公共顶点）的那条边；非四边形返回 None。"""
    if len(face.verts) != 4:
        return None
    for cand in face.edges:
        if cand is edge:
            continue
        if edge.verts[0] in cand.verts or edge.verts[1] in cand.verts:
            continue
        return cand
    return None


def _us_boundary_chains(boundary_edges):
    """把孤岛边界边整理成有序顶点链（环或折线），用于给边界顶点编列号。"""
    adj = {}
    for edge in boundary_edges:
        a, b = edge.verts
        adj.setdefault(a, []).append((b, edge))
        adj.setdefault(b, []).append((a, edge))
    visited = set()
    chains = []
    for start in boundary_edges:
        if start in visited:
            continue
        visited.add(start)
        a, b = start.verts
        chain = [a, b]
        current = b
        while True:
            nxt = None
            for other, edge in adj.get(current, []):
                if edge not in visited:
                    nxt = (other, edge)
                    break
            if nxt is None:
                break
            other, edge = nxt
            visited.add(edge)
            if other in chain:  # 闭合环：连回起点，不重复记录
                break
            chain.append(other)
            current = other
        current = a
        while True:
            nxt = None
            for other, edge in adj.get(current, []):
                if edge not in visited:
                    nxt = (other, edge)
                    break
            if nxt is None:
                break
            other, edge = nxt
            visited.add(edge)
            if other in chain:
                break
            chain.insert(0, other)
            current = other
        chains.append(chain)
    return chains


def _us_assign_grid_coords(bm, boundary_edges, boundary_edge_set):
    """以孤岛边界为第 0 排，沿四边条带逐排向内给顶点编 (链, 行 r, 列 c) 坐标。

    关键约束（防菱形）：
    - 只走规则四边形条带；遇到三角面/n 边形即停；
    - 条带碰到**别的边界链**编过坐标的顶点就停走——不同链的相位各自独立，
      若互相穿插，两个相位在相遇带错开半格，删除后就会产生菱形。
      相遇带保持原密度，绝不出错拓扑；
    - 未编到坐标的顶点不参与删除（宁可不动）。
    """
    coords = {}  # vert -> (chain_idx, r, c)
    chains = _us_boundary_chains(boundary_edges)
    for chain_idx, chain in enumerate(chains):
        for c, vert in enumerate(chain):
            coords.setdefault(vert, (chain_idx, 0, c))
        pairs = list(zip(chain, chain[1:]))
        # 闭合环要补上首尾相接的那条边
        if len(chain) > 2:
            closing = bm.edges.get((chain[-1], chain[0]))
            if closing is not None and closing in boundary_edge_set:
                pairs.append((chain[-1], chain[0]))
        for c, (v0, v1) in enumerate(pairs):
            edge = bm.edges.get((v0, v1))
            if edge is None:
                continue
            c0, c1 = (c, c + 1) if c < len(chain) - 1 else (c, 0)
            for face in list(edge.link_faces):
                r = 0
                cur_edge = edge
                cur_face = face
                seen_faces = set()
                while (cur_face is not None and cur_face not in seen_faces
                       and len(cur_face.verts) == 4):
                    # 面上有别的链编过的顶点 => 到达势力范围边界，停走
                    foreign = False
                    for w in cur_face.verts:
                        info = coords.get(w)
                        if info is not None and info[0] != chain_idx:
                            foreign = True
                            break
                    if foreign:
                        break
                    seen_faces.add(cur_face)
                    opp = _us_opposite_edge(cur_face, cur_edge)
                    if opp is None:
                        break
                    r += 1
                    u0, u1 = cur_edge.verts
                    w0, w1 = opp.verts
                    if bm.edges.get((u0, w0)) is not None:
                        near, far = w0, w1
                    else:
                        near, far = w1, w0
                    coords.setdefault(near, (chain_idx, r, c0))
                    coords.setdefault(far, (chain_idx, r, c1))
                    next_face = None
                    for lf in opp.link_faces:
                        if lf is not cur_face:
                            next_face = lf
                            break
                    cur_edge, cur_face = opp, next_face
    return coords


def _us_v2_is_straight(v, min_cos=0.5):
    """度=2 顶点的两个邻居是否近似共线（可安全接合成一条直边）。

    网格行/列链上的中点满足；拐角（L 弯）不满足——拐角接合会产生斜边。
    """
    edges = list(v.link_edges)
    if len(edges) != 2:
        return False
    a = edges[0].other_vert(v)
    b = edges[1].other_vert(v)
    d1 = a.co - v.co
    d2 = b.co - v.co
    if d1.length <= 0.0 or d2.length <= 0.0:
        return False
    return d1.normalized().dot(d2.normalized()) < -min_cos


def _us_straight_unsubdivide(bm, boundary_edges, protected_verts, allowed_verts, iterations):
    """定向直合反细分：相位从孤岛边界起算，只删除 (r,c) 不在保留格点上的顶点。

    关键结构（防菱形的核心）：
    - dissolve_verts 只删顶点、合并面，**不会**在幸存顶点之间补新边；
      所以分两类动作循环到不动点：
      1) 度=4 的内部点：溶解 = 合并四周的面，方向保持、不产生新边；
      2) 度=2 且两邻居近似共线的点：溶解 = 把左右邻居用一条顺向直边接合；
    - 边界顶点绝不删除；不跨不同边界链的势力范围（相位不穿插）；
    - 结果只会是顺向四边形 + 保留边界中点带来的五边形，不会有旋转 45° 的菱形；
    - 返回 (删除顶点数, 编入坐标的顶点数)。
    """
    coords = _us_assign_grid_coords(bm, boundary_edges, set(boundary_edges))
    if not coords:
        return 0, 0
    step = 1 << max(1, int(iterations))
    protected = set(protected_verts)
    allowed = set(allowed_verts)
    workset = {
        v for v, (_chain, r, c) in coords.items()
        if v.is_valid and v not in protected and v in allowed
        and (r % step != 0 or c % step != 0)
    }
    removed = 0

    def _parity(v):
        _chain, r, c = coords[v]
        return (r % 2, c % 2)

    # 不动点循环。三类动作的严格顺序是防菱形的核心：
    # 1) 细格面心（奇,奇）度4：溶解只合并面、不产生新边，且互不相邻可批量；
    #    溶完它，边上中点才掉成度2；
    # 2) 度2 且两邻居近似共线的点：溶解 = 顺向接合出一条直边（绝不斜连）；
    # 3) 粗格面心（偶,偶 且两个坐标都不在保留格点）度4：iterations>1 时出现，
    #    同样只合并不新连。
    # 任何一类只在四周全是四边形时才动手，结构不规整就宁可留下。
    for _ in range(256):
        bm.verts.ensure_lookup_table()
        batch = [v for v in workset
                 if v.is_valid and len(v.link_edges) == 4 and _parity(v) == (1, 1)
                 and all(len(f.verts) == 4 for f in v.link_faces)]
        if not batch:
            batch = [v for v in workset
                     if v.is_valid and len(v.link_edges) == 2 and _us_v2_is_straight(v)]
        if not batch:
            batch = [v for v in workset
                     if v.is_valid and len(v.link_edges) == 4 and _parity(v) == (0, 0)
                     and all(len(f.verts) == 4 for f in v.link_faces)
                     and (lambda rc: rc[1] % step != 0 and rc[2] % step != 0)(coords[v])]
        if not batch:
            break
        bmesh.ops.dissolve_verts(bm, verts=batch, use_face_split=False, use_boundary_tear=False)
        removed += len(batch)
    return removed, len(coords)


def _us_capture_selected_vertex_indices(me):
    """进入编辑模式后立即快照选中顶点索引，供"仅选中部分"使用"""
    bm = bmesh.from_edit_mesh(me)
    bm.verts.ensure_lookup_table()
    bm.verts.index_update()
    return {v.index for v in bm.verts if v.select}


def _us_write_mesh_selection_flags(me, selected_vert_indices, only_selected):
    """把选区直接写进网格数据的选择标志。

    这是唯一在"顶点/边/面"三种选择模式下都能被 bpy.ops.mesh.subdivide 识别的
    写法：先清空、按索引置位，再由调用方 select_flush(True) 向上同步。
    """
    index_set = set(selected_vert_indices) if only_selected else None
    for vert in me.vertices:
        vert.select = (vert.index in index_set) if index_set is not None else True
    for edge in me.edges:
        edge.select = True if index_set is None else (
            edge.vertices[0] in index_set and edge.vertices[1] in index_set)
    for poly in me.polygons:
        poly.select = True if index_set is None else all(v in index_set for v in poly.vertices)


def _us_restore_selection(bm, me, selected_vert_indices, only_selected):
    """把选区还原成细分/反细分算子能识别的状态。

    踩坑记录（关键）：
    - 只写 bmesh 的 v/e/f.select 再 update_edit_mesh，后续 bpy.ops.mesh.subdivide
      在"顶点选择模式"下读不到选区，细分会完全没效果；
    - 只写网格数据的 select 标志而不 flush，同样读不到；
    - 正确做法：写网格数据标志 → 重新取 bmesh → select_flush(True) 向上同步。
      三种选择模式（顶点/边/面）实测都生效，且不需要切换选择模式或切换物体模式。
    """
    _us_write_mesh_selection_flags(me, selected_vert_indices, only_selected)
    bm = bmesh.from_edit_mesh(me)
    bm.verts.ensure_lookup_table()
    bm.verts.index_update()
    bm.select_flush(True)
    bmesh.update_edit_mesh(me, loop_triangles=False, destructive=False)


class BMTP_OT_UVIslandAwareUnsubdivide(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_uv_island_aware_unsubdivide"
    bl_label = "反细分＆细分·UV防错乱"
    bl_description = (
        "反细分/细分二合一：按UV孤岛边界保护顶点后再反细分，"
        "原有缝合线数据原样保留；可选消除处理区交界处的T型顶点（两种模式行为一致）"
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        obj = getattr(context, "active_object", None)
        if getattr(context, "mode", "") != 'EDIT_MESH':
            return False
        return (
            obj is not None
            and getattr(obj, "type", "") == 'MESH'
            and getattr(getattr(obj, "data", None), "uv_layers", None) is not None
            and obj.data.uv_layers.active is not None
        )

    def execute(self, context):
        props = context.scene.bmtp_props
        obj = context.active_object
        me = obj.data

        mode = str(getattr(props, "us_operation", 'UNSUBDIVIDE') or 'UNSUBDIVIDE')
        only_selected = bool(getattr(props, "us_only_selected", False))
        connect_boundary = bool(getattr(props, "us_connect_boundary", False))

        selected_vert_indices = _us_capture_selected_vertex_indices(me)

        if mode == 'SUBDIVIDE':
            return self._do_subdivide(context, me, props, only_selected, connect_boundary, selected_vert_indices)
        return self._do_unsubdivide(context, me, props, only_selected, connect_boundary, selected_vert_indices)

    # ------------------------------------------------------------------
    def _do_subdivide(self, context, me, props, only_selected, connect_boundary, selected_vert_indices):
        if only_selected and not selected_vert_indices:
            self.report({'WARNING'}, "没有选中任何顶点，无法执行仅选中部分细分")
            return {'CANCELLED'}

        cuts = max(1, int(getattr(props, "us_cuts", 1) or 1))

        bm = bmesh.from_edit_mesh(me)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        bm.faces.ensure_lookup_table()
        bm.verts.index_update()

        _us_restore_selection(bm, me, selected_vert_indices, only_selected)

        try:
            bpy.ops.mesh.subdivide(number_cuts=cuts, smoothness=0.0, fractal=0.0)
        except Exception as exc:
            self.report({'ERROR'}, f"细分失败: {exc}")
            return {'CANCELLED'}

        bm = bmesh.from_edit_mesh(me)
        connected = _us_fix_t_junctions(bm, connect_boundary, only_selected)
        bmesh.update_edit_mesh(me)

        scope = "（仅选中部分）" if only_selected else "（全部）"
        message = f"细分完成：每条边切成 {cuts + 1} 段{scope}"
        if connect_boundary:
            message += f"，连接边界顶点 {connected} 处"
        self.report({'INFO'}, message)
        return {'FINISHED'}

    # ------------------------------------------------------------------
    def _do_unsubdivide(self, context, me, props, only_selected, connect_boundary, selected_vert_indices):
        iterations = max(1, int(getattr(props, "us_iterations", 2) or 2))
        protection_rings = max(0, int(getattr(props, "us_protection_rings", 0) or 0))

        # 全选以便进行孤岛检测
        bpy.ops.mesh.select_all(action='SELECT')
        bpy.ops.uv.select_all(action='SELECT')

        bm = bmesh.from_edit_mesh(me)
        bm.edges.ensure_lookup_table()
        bm.verts.ensure_lookup_table()
        bm.verts.index_update()

        # 1. 备份原有缝合线状态（顶点索引对做稳定键，不依赖遍历顺序）
        original_seams = {}
        for edge in bm.edges:
            try:
                a, b = edge.verts[0].index, edge.verts[1].index
            except Exception:
                continue
            original_seams[(a, b) if a < b else (b, a)] = bool(edge.seam)

        # 2. 临时用UV孤岛生成缝合线，借它反推孤岛边界
        bpy.ops.mesh.mark_seam(clear=True)
        op_ok = True
        try:
            op_result = bpy.ops.uv.seams_from_islands()
            op_ok = 'CANCELLED' not in (op_result or set())
        except Exception:
            op_ok = False

        bm = bmesh.from_edit_mesh(me)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()
        if not op_ok or not any(e.seam for e in bm.edges):
            # 无 UI 上下文时原生算子会 CANCELLED，这里用 bmesh 兜底。
            # 必须把 clear seam 之前备份的原始 seam 键传进去——注意只传
            # seam=True 的键，否则所有边都会被当成边界。
            original_seam_keys = {k for k, v in original_seams.items() if v}
            _us_mark_seams_from_islands_in_bmesh(bm, original_seam_keys)
        bmesh.update_edit_mesh(me)

        bm = bmesh.from_edit_mesh(me)
        bm.verts.ensure_lookup_table()
        bm.edges.ensure_lookup_table()

        # 3. 收集边界顶点与边界边
        boundary_verts = set()
        boundary_edges = []
        for edge in bm.edges:
            if edge.seam:
                boundary_edges.append(edge)
                boundary_verts.add(edge.verts[0])
                boundary_verts.add(edge.verts[1])

        # 4. 立刻恢复原有缝合线状态
        _us_restore_seams(bm, original_seams)
        bmesh.update_edit_mesh(me)

        if not boundary_verts:
            bmesh.update_edit_mesh(me)
            self.report({'WARNING'}, "未检测到任何UV孤岛边界")
            return {'CANCELLED'}

        # 5. 扩展保护圈
        protected_verts = set(boundary_verts)
        for _ in range(protection_rings):
            new_verts = set()
            for vert in protected_verts:
                for edge in vert.link_edges:
                    other_vert = edge.other_vert(vert)
                    if other_vert not in protected_verts:
                        new_verts.add(other_vert)
            protected_verts.update(new_verts)

        # 6. 准备参与反细分的顶点
        bm.verts.index_update()
        verts_to_unsubdivide = [
            v for v in bm.verts
            if v not in protected_verts
            and (not only_selected or v.index in selected_vert_indices)
        ]
        if not verts_to_unsubdivide:
            bmesh.update_edit_mesh(me)
            self.report({'WARNING'}, "没有可反细分的顶点")
            return {'CANCELLED'}

        # 7. 定向直合反细分：相位从孤岛边界起算，按行/列对齐删除。
        #    边界顶点绝不删除；因为删除始终顺着网格方向，不会产生菱形。
        #    注意：反细分不做"连接边界顶点"——它只删除，不主动加边。
        try:
            removed, covered = _us_straight_unsubdivide(
                bm, boundary_edges, protected_verts, verts_to_unsubdivide, iterations)
        except Exception as exc:
            bmesh.update_edit_mesh(me)
            self.report({'ERROR'}, f"反细分失败: {exc}")
            return {'CANCELLED'}

        bmesh.update_edit_mesh(me)
        if removed <= 0:
            self.report(
                {'WARNING'},
                "没有可反细分的顶点（边界保护之外未找到可对齐删除的规则四边顶点）")
            return {'CANCELLED'}
        self.report(
            {'INFO'},
            f"反细分完成：保护 {len(protected_verts)} 个边界顶点，"
            f"对齐删除 {removed} 个顶点（顺向合并，不产生菱形）"
            + ("（仅选中部分）" if only_selected else "")
        )
        return {'FINISHED'}


bmtp_mesh_tools_list = (
    BMTP_OT_DynamicBridge,
    BMTP_OT_TrisToQuadsPreserveUV,
    BMTP_OT_QuadsToTris,
    BMTP_OT_EnableSubdivisionLimitSurface,
    BMTP_OT_UVIslandAwareUnsubdivide,
    BMTP_OT_SetVertexColor,
    BMTP_OT_DeleteEmptyMeshes,
    BMTP_OT_SyncDataNames,
    BMTP_OT_CleanUselessShapeKeys,
)
