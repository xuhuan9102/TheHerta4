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


bmtp_mesh_tools_list = (
    BMTP_OT_DynamicBridge,
    BMTP_OT_TrisToQuadsPreserveUV,
    BMTP_OT_QuadsToTris,
    BMTP_OT_EnableSubdivisionLimitSurface,
    BMTP_OT_SetVertexColor,
    BMTP_OT_DeleteEmptyMeshes,
    BMTP_OT_SyncDataNames,
    BMTP_OT_CleanUselessShapeKeys,
)
