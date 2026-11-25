"""Bone-parented capsule guides and temporary Blender model physics preview."""

import math
import time

import bpy
from bpy.app.handlers import persistent
from mathutils import Matrix, Vector
import numpy as np


_COLLECTION = "SSMT 骨骼物理胶囊"
_GUIDE = "SSMT:BonePhysicsCapsule"
_session = None


def _guides(scene):
    collection = next((c for c in scene.collection.children if c.get(_GUIDE)), None)
    if collection is None:
        collection = bpy.data.collections.new(_COLLECTION)
        collection[_GUIDE] = True
        collection.hide_render = True
        scene.collection.children.link(collection)
    return collection


def _wire_mesh(data, radius, length):
    radius = float(radius)
    half = max(0.0, float(length) * 0.5 - radius)
    vertices, edges = [], []

    def path(points, closed=False):
        start = len(vertices)
        vertices.extend(points)
        edges.extend((i - 1, i) for i in range(start + 1, len(vertices)))
        if closed:
            edges.append((len(vertices) - 1, start))

    for scale, segments in ((1.0, 16), (0.75, 12), (0.5, 8)):
        r = radius * scale
        for side in (-1, 1):
            path([(r * math.cos(a), side * half, r * math.sin(a))
                  for a in (i * math.tau / segments for i in range(segments))], True)
            for plane in range(2):
                half_steps = segments // 2
                ring = [(r * math.cos(a), side * (half + r * math.sin(a)))
                        for a in (i * math.pi / half_steps for i in range(half_steps + 1))]
                path([(value, y, 0) if plane == 0 else (0, y, value) for value, y in ring])
        for x, z in ((r, 0), (0, r), (-r, 0), (0, -r)):
            path(((x, -half, z), (x, half, z)))
    data.clear_geometry()
    data.from_pydata(vertices, edges, [])
    data.update()


def uses_box(body):
    return body.shape == "BOX"


def _wire_box(data, dimensions, corner):
    half = [size * .5 for size in dimensions]
    radius = min(corner, *half)
    thickness = min(half[0], half[2])
    vertices, edges = [], []
    for shell in range(3):
        inset = shell * thickness * .25
        r = max(0., radius - inset)
        core = [max(0., side - max(radius, inset)) for side in half]
        for u, v in ((0, 1), (1, 2), (2, 0)):
            start = len(vertices)
            for corner_index, (sign_u, sign_v) in enumerate(((1, 1), (-1, 1), (-1, -1), (1, -1))):
                for sample in range(5):
                    angle = (corner_index + sample / 4) * math.pi / 2
                    point = [0., 0., 0.]
                    point[u] = sign_u * core[u] + r * math.cos(angle)
                    point[v] = sign_v * core[v] + r * math.sin(angle)
                    vertices.append(point)
            edges.extend((start + i, start + (i + 1) % 20) for i in range(20))
    data.clear_geometry()
    data.from_pydata(vertices, edges, [])
    data.update()


def _guide_parent(obj, armature, bone, body):
    scale = obj.scale.copy()
    if obj.parent != armature or obj.parent_type != "BONE" or obj.parent_bone != bone.name:
        obj.parent = armature
        obj.parent_type = "BONE"
        obj.parent_bone = bone.name
        obj.matrix_parent_inverse = Matrix.Identity(4)
        obj.matrix_basis = Matrix.Identity(4)
        bpy.context.view_layer.update()
        pose = armature.pose.bones[bone.name]
        obj.matrix_parent_inverse = obj.matrix_world.inverted_safe() @ (armature.matrix_world @ pose.matrix)
    center = Vector(body.offset)
    center.y += bone.length * 0.5
    obj.matrix_basis = Matrix.Translation(center) @ Matrix.Diagonal((*scale, 1.0))


def capsule_dimensions(body):
    obj = body.viewport_object
    if obj is None or not obj.get(_GUIDE):
        return body.radius, body.length
    x, y, z = map(abs, obj.scale)
    radius = max(0.001, body.radius * max(x, z))
    cylinder_length = max(0.0, body.length - 2.0 * body.radius)
    return radius, cylinder_length * y + 2.0 * radius


def box_dimensions(body):
    obj = body.viewport_object
    scale = tuple(map(abs, obj.scale)) if obj is not None and obj.get(_GUIDE) else (1., 1., 1.)
    dimensions = tuple(max(.002, size * factor) for size, factor in zip(body.box_size, scale))
    return dimensions, min(body.box_corner * min(scale), .5 * min(dimensions))


def apply_capsule_scale(body):
    if uses_box(body):
        dimensions, corner = box_dimensions(body)
        body.viewport_object.scale = (1.0, 1.0, 1.0)
        body.box_size = dimensions
        body.box_corner = corner
        return
    radius, length = capsule_dimensions(body)
    body.viewport_object.scale = (1.0, 1.0, 1.0)
    body.radius = radius
    body.length = length


def _sync_one(node, scene, body, sources, *, create):
    target = sources.get(body.slot)
    obj = body.viewport_object
    if target is None:
        if obj:
            obj.hide_set(True)
        return
    armature, bone = target
    if obj is None and create:
        data = bpy.data.meshes.new("SSMT 骨骼胶囊")
        obj = bpy.data.objects.new("SSMT 骨骼胶囊", data)
        obj[_GUIDE] = True
        _guides(scene).objects.link(obj)
        obj.hide_render = True
        obj.display_type = "WIRE"
        obj.show_in_front = True
        body.viewport_object = obj
    if obj is None or not obj.get(_GUIDE):
        return
    if _session is not None and _session.node == node:
        armature = _session.clones.get(armature, armature)
    role = "碰撞" if body.kind == "COLLIDER" else "刚体"
    kind = role + ("方盒" if uses_box(body) else "胶囊")
    obj.name = f"{body.slot} · {kind}"
    obj.color = (0.18, 0.77, 1.0, 1.0) if body.kind == "COLLIDER" else (0.26, 1.0, 0.40, 1.0)
    if uses_box(body):
        _wire_box(obj.data, body.box_size, body.box_corner)
    else:
        _wire_mesh(obj.data, body.radius, body.length)
    _guide_parent(obj, armature, bone, body)
    obj.lock_location = obj.lock_rotation = (True, True, True)
    obj.hide_select = not node.physics_edit_capsules
    obj.hide_set(not node.physics_viewport_shapes)


def sync_capsules(node, scene):
    from .node_bone_physics import bone_sources

    sources = bone_sources(node, scene)
    for body in node.physics_bodies:
        _sync_one(node, scene, body, sources, create=True)


def update_capsule(body, context):
    if not body.viewport_object or not getattr(context, "scene", None):
        return
    from .node_bone_physics import bone_sources

    for node in getattr(body.id_data, "nodes", ()):
        if any(item.as_pointer() == body.as_pointer() for item in getattr(node, "physics_bodies", ())):
            _sync_one(node, context.scene, body, bone_sources(node, context.scene), create=False)
            return


def update_guide_visibility(node, context):
    for body in node.physics_bodies:
        if body.viewport_object:
            body.viewport_object.hide_set(not node.physics_viewport_shapes)
            body.viewport_object.hide_select = not node.physics_edit_capsules


def remove_capsule(body):
    obj = body.viewport_object
    if obj is not None and obj.get(_GUIDE):
        data = obj.data
        bpy.data.objects.remove(obj, do_unlink=True)
        if data is not None and not data.users:
            bpy.data.meshes.remove(data)


class _LivePreview:
    def __init__(self, node, scene):
        self.node, self.scene = node, scene
        self.clones = {}
        self.modifiers = []
        self.driver = None
        self.signature = None
        self.frame = 0
        self.started = None
        self._request_driver()

    def _settings(self):
        fields = ("slot", "kind", "radius", "length", "mass", "damping", "stiffness", "friction", "angle",
                  "gravity_scale", "pin_translation", "air_drag", "limit_twist", "twist_angle", "recovery_gap", "sweep",
                  "shape", "allow_deep_overlap")
        bodies = tuple(tuple(getattr(body, field) for field in fields) + tuple(body.offset)
                       + (box_dimensions(body) if uses_box(body) else capsule_dimensions(body))
                       for body in self.node.physics_bodies)
        links = tuple((link.first, link.second, link.stiffness, link.damping) for link in self.node.physics_links)
        rigs = tuple((item.armature.as_pointer() if item.armature else None, item.attachment_slot,
                      tuple((link.child_slot, link.parent_slot) for link in item.parent_links))
                     for item in (self.node.rigs or (self.node,)))
        return (rigs, bodies, links, self.node.physics_use_substeps, self.node.physics_adaptive_substeps, self.node.physics_step_hz,
                self.node.physics_max_substeps, self.node.physics_joint_iterations, self.node.physics_frame_steps,
                self.node.physics_joints_per_frame)

    def _request_driver(self):
        from .node_bone_physics import bone_sources, build_bone_physics
        from ..common.zzmi_bone_physics_preview_dx11 import prepare_preview_solver

        rigs = self.node._rig_configuration(self.scene)
        sources = bone_sources(self.node, self.scene)
        if not sources or not self.node.physics_bodies or not rigs:
            raise ValueError("请选择骨架并添加物理胶囊")
        settings = build_bone_physics(
            self.node, self.scene, rigs, max(sources) + 1, lambda _rig: False,
            self.node._physics_reserved_variables(),
        )
        self.pending = (prepare_preview_solver(settings), sources, settings, self._settings())

    def _attach_driver(self, driver, sources, settings, signature):
        if self.driver is not None:
            self.driver.close()
        self.driver = driver
        self.sources = sources
        self.followers = settings.followers
        self.bind = {
            slot: (armature.matrix_world @ bone.matrix_local).inverted_safe()
            for slot, (armature, bone) in self.sources.items()
        }
        locked = set()
        for armature, _bone in self.sources.values():
            locked.add(armature)
        for armature in locked - self.clones.keys():
            clone = armature.copy()
            clone.data = armature.data.copy()
            self.scene.collection.objects.link(clone)
            self.clones[armature] = clone
            clone.name = "SSMT 骨骼物理实时预览"
            clone.animation_data_clear()
            for constraint in list(clone.constraints):
                clone.constraints.remove(constraint)
            clone.parent = None
            clone.hide_render = True
            clone.hide_select = True
            clone.hide_set(True)
            for pose in clone.pose.bones:
                for constraint in list(pose.constraints):
                    pose.constraints.remove(constraint)
            for obj in self.scene.objects:
                if obj.type == "MESH" and not obj.get(_GUIDE):
                    for modifier in obj.modifiers:
                        if modifier.type == "ARMATURE" and modifier.object == armature:
                            self.modifiers.append((modifier, armature, clone))
                            modifier.object = clone
        self.signature = signature
        sync_capsules(self.node, self.scene)

    def tick(self):
        starting = self.started is None
        attached = False
        if self.pending is not None:
            work, sources, settings, signature = self.pending
            if not work.done():
                return False
            self.pending = None
            driver = work.result()
            if signature != self._settings():
                driver.close()
                self._request_driver()
                return False
            self._attach_driver(driver, sources, settings, signature)
            attached = True
        elif self.signature != self._settings():
            self._request_driver()
            return False
        if starting:
            self.started = time.monotonic()
        evaluated = bpy.context.evaluated_depsgraph_get()
        originals = {arm: arm.evaluated_get(evaluated) for arm in self.clones}
        matrices = np.repeat(np.eye(4, dtype="<f4")[None, :3], len(self.followers), axis=0)
        for slot, (armature, bone) in self.sources.items():
            rig = originals[armature]
            matrices[slot] = np.asarray(rig.matrix_world @ rig.pose.bones[bone.name].matrix @ self.bind[slot])[:3]
        self.frame += 1
        clock = time.monotonic() - self.started
        result = self.driver.step(matrices, clock, self.frame, 0, self.node.physics_gravity,
                                  self.node.physics_force)
        for root, duplicate in self.clones.items():
            reference = originals[root]
            duplicate.matrix_world = reference.matrix_world
            inverse = duplicate.matrix_world.inverted_safe()
            desired = {bone.name: bone.matrix.copy() for bone in reference.pose.bones}
            for slot, (armature, bone) in self.sources.items():
                if armature == root and self.followers[slot] >= 0:
                    transform = Matrix((*result[slot].tolist(), (0., 0., 0., 1.)))
                    desired[bone.name] = inverse @ transform @ self.bind[slot].inverted_safe()
            for pose in duplicate.pose.bones:
                arguments = {}
                if pose.parent is not None:
                    arguments = dict(parent_matrix=desired[pose.parent.name],
                                     parent_matrix_local=pose.parent.bone.matrix_local)
                pose.matrix_basis = pose.bone.convert_local_to_pose(
                    desired[pose.name], pose.bone.matrix_local, invert=True, **arguments,
                )
            duplicate.update_tag()
        for window in bpy.context.window_manager.windows:
            for area in window.screen.areas:
                if area.type == "VIEW_3D" or (attached and area.type == "NODE_EDITOR"):
                    area.tag_redraw()
        return True

    def stop(self):
        if self.pending is not None:
            from ..common.zzmi_bone_physics_preview_dx11 import discard_preview_solver
            discard_preview_solver(self.pending[0])
            self.pending = None
        if self.driver is None and not self.clones:
            return
        for modifier, original, clone in self.modifiers:
            try:
                if modifier.object == clone:
                    modifier.object = original
            except ReferenceError:
                continue
        try:
            sync_capsules(self.node, self.scene)
        except ReferenceError:
            pass
        finally:
            for clone in self.clones.values():
                try:
                    data = clone.data
                    bpy.data.objects.remove(clone, do_unlink=True)
                    if data is not None and not data.users:
                        bpy.data.armatures.remove(data)
                except ReferenceError:
                    continue
            if self.driver is not None:
                self.driver.close()


def is_running(node):
    return _session is not None and _session.node == node


def is_preparing(node):
    return is_running(node) and _session.pending is not None


@persistent
def stop_preview(*_args):
    global _session
    session, _session = _session, None
    if session is not None:
        session.stop()


def _preview_timer():
    if _session is None:
        return None
    try:
        active = _session.tick()
    except Exception as exc:
        stop_preview()
        print(f"SSMT 骨骼物理预览已停止：{exc}")
        return None
    return (1 / 30.0) if active else .1


class SSMT_OT_BonePhysicsViewportPreview(bpy.types.Operator):
    bl_idname = "ssmt4.bone_physics_viewport_preview"
    bl_label = "切换 Blender 实时物理预览"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        global _session
        node = getattr(context, "node", None)
        if getattr(node, "bl_idname", "") != "SSMTNode_PostProcess_BoneMatrix":
            return {"CANCELLED"}
        same = is_running(node)
        stop_preview()
        if same:
            return {"FINISHED"}
        if not node.physics_bodies or (not node.rigs and node.armature is None):
            self.report({"ERROR"}, "请选择骨架并添加物理胶囊")
            return {"CANCELLED"}
        try:
            _session = _LivePreview(node, context.scene)
        except Exception as exc:
            stop_preview()
            self.report({"ERROR"}, str(exc))
            return {"CANCELLED"}
        if not bpy.app.timers.is_registered(_preview_timer):
            bpy.app.timers.register(_preview_timer, first_interval=1 / 30.0)
        return {"FINISHED"}


def register():
    for sequence in (bpy.app.handlers.save_pre, bpy.app.handlers.load_pre, bpy.app.handlers.undo_pre):
        if stop_preview not in sequence:
            sequence.append(stop_preview)


def unregister():
    stop_preview()
    if bpy.app.timers.is_registered(_preview_timer):
        bpy.app.timers.unregister(_preview_timer)
    for sequence in (bpy.app.handlers.save_pre, bpy.app.handlers.load_pre, bpy.app.handlers.undo_pre):
        if stop_preview in sequence:
            sequence.remove(stop_preview)
