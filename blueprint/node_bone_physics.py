"""Blender setup for capsule bodies on the ZZMI bone-matrix node."""

import math
import re

import bpy
from mathutils import Vector

from ..common.zzmi_armature_reconstruction import WORKSPACE_IMPORT_RIG_MARKER
from ..common.zzmi_bone_matrix_playback import variable_name
from ..common.zzmi_bone_matrix_snapshot import hotkey_value
from ..common.zzmi_bone_physics import BonePhysics, Capsule, RoundedBox, SoftLink, build_followers
from . import node_bone_physics_viewport as viewport


class SSMT_BonePhysicsBody(bpy.types.PropertyGroup):
    viewport_object: bpy.props.PointerProperty(type=bpy.types.Object, options={"HIDDEN"})
    slot: bpy.props.IntProperty(name="骨骼编号", min=0, max=65535, update=viewport.update_capsule)
    kind: bpy.props.EnumProperty(name="类型", items=(
        ("COLLIDER", "碰撞胶囊", "跟随骨骼并对动态刚体施力"),
        ("DYNAMIC", "刚体胶囊", "实时计算关节旋转，写入骨骼矩阵"),
    ), update=viewport.update_capsule)
    shape: bpy.props.EnumProperty(name="碰撞形状", items=(
        ("CAPSULE", "胶囊", "圆柱和半球组成的碰撞体"),
        ("BOX", "圆角长方盒", "用宽度、沿骨长和前后厚度覆盖碰撞区域"),
    ), default="CAPSULE", update=viewport.update_capsule)
    box_size: bpy.props.FloatVectorProperty(
        name="方盒尺寸", size=3, default=(.16, .24, .19), min=.002, unit="LENGTH",
        description="骨骼局部 X 宽度、Y 沿骨长、Z 厚度", update=viewport.update_capsule,
    )
    box_corner: bpy.props.FloatProperty(
        name="圆角半径", default=.018, min=0.0, unit="LENGTH", update=viewport.update_capsule,
    )
    radius: bpy.props.FloatProperty(name="半径", min=0.001, default=0.025, unit="LENGTH", update=viewport.update_capsule)
    length: bpy.props.FloatProperty(name="总长", min=0.002, default=0.12, unit="LENGTH", update=viewport.update_capsule)
    offset: bpy.props.FloatVectorProperty(name="骨骼局部偏移", size=3, unit="LENGTH", update=viewport.update_capsule)
    mass: bpy.props.FloatProperty(name="质量", default=0.1, min=0.005, max=100.0)
    damping: bpy.props.FloatProperty(name="阻尼", default=7.0, min=0.0, max=80.0)
    air_drag: bpy.props.FloatProperty(
        name="迎风阻力", default=0.2, min=0.0, max=5.0,
        description="相对空气的水平阻力系数（1/米），阻力随相对速度的平方增加",
    )
    stiffness: bpy.props.FloatProperty(name="关节回正", default=28.0, min=0.0, max=400.0)
    friction: bpy.props.FloatProperty(name="摩擦", default=0.35, min=0.0, max=1.0)
    gravity_scale: bpy.props.FloatProperty(
        name="重力倍率", default=1.0, min=0.0, max=4.0, description="该刚体所受重力相对于全局重力的倍率",
    )
    angle: bpy.props.FloatProperty(name="最大关节摆角", subtype="ANGLE", default=math.pi / 3,
                                   min=0.0, max=math.pi)
    limit_twist: bpy.props.BoolProperty(name="限制轴向扭转", default=False)
    twist_angle: bpy.props.FloatProperty(name="最大扭转角", subtype="ANGLE", default=math.pi / 9,
                                         min=0.0, max=math.pi)
    recovery_gap: bpy.props.FloatProperty(
        name="挂点恢复距离", unit="LENGTH", default=0.0, min=0.0,
        description="超过此距离时局部拉回偏离的刚体，零表示关闭",
    )
    allow_deep_overlap: bpy.props.BoolProperty(
        name="允许深穿透", default=True,
        description="深穿区停止排斥；关闭后保持排斥并限幅修正穿入",
    )
    sweep: bpy.props.BoolProperty(
        name="快速移动时检查经过位置", default=False,
        description="胶囊跨过刚体时，在移动路径上追加一次三层力场检查",
    )
    pin_translation: bpy.props.BoolProperty(
        name="锁定挂点平移", default=False, description="让接到动画父骨的刚体分支保持挂点相连，保留物理旋转",
    )


class SSMT_BonePhysicsLink(bpy.types.PropertyGroup):
    first: bpy.props.IntProperty(name="骨骼 A", min=0, max=65535)
    second: bpy.props.IntProperty(name="骨骼 B", min=0, max=65535)
    stiffness: bpy.props.FloatProperty(name="刚度", default=140.0, min=0.001, max=10000.0)
    damping: bpy.props.FloatProperty(name="阻尼", default=7.0, min=0.0, max=80.0)


class SSMT_UL_BonePhysicsLinks(bpy.types.UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_propname, _index):
        layout.label(text=f"{item.first} ↔ {item.second}")


class SSMT_OT_BonePhysicsLinkEdit(bpy.types.Operator):
    bl_idname = "ssmt4.bone_physics_link_edit"
    bl_label = "编辑刚体软连接"
    bl_options = {"INTERNAL", "UNDO"}

    remove: bpy.props.BoolProperty(default=False)

    def execute(self, context):
        node = getattr(context, "node", None)
        if getattr(node, "bl_idname", "") != "SSMTNode_PostProcess_BoneMatrix":
            return {"CANCELLED"}
        if self.remove:
            if node.physics_links:
                node.physics_links.remove(min(node.physics_link_index, len(node.physics_links) - 1))
                node.physics_link_index = max(0, min(node.physics_link_index, len(node.physics_links) - 1))
        else:
            node.physics_links.add()
            node.physics_link_index = len(node.physics_links) - 1
        return {"FINISHED"}


class SSMT_UL_BonePhysicsBodies(bpy.types.UIList):
    def draw_item(self, _context, layout, _data, item, _icon, _active_data, _active_propname, _index):
        columns = layout.split(factor=0.17)
        columns.label(text=" ")
        role = "碰撞" if item.kind == "COLLIDER" else "刚体"
        shape = "方盒" if viewport.uses_box(item) else "胶囊"
        columns.label(text=f"{item.slot}    {role}{shape}",
                      icon="MESH_UVSPHERE" if item.kind == "COLLIDER" else "PHYSICS")


class SSMT_OT_BonePhysicsBodyEdit(bpy.types.Operator):
    bl_idname = "ssmt4.bone_physics_body_edit"
    bl_label = "编辑骨骼物理胶囊"
    bl_options = {"INTERNAL", "UNDO"}

    action: bpy.props.EnumProperty(items=(
        ("DYNAMIC", "添加刚体胶囊", ""), ("COLLIDER", "添加碰撞胶囊", ""), ("REMOVE", "删除胶囊", ""),
        ("SYNC", "同步视图胶囊", ""),
        ("APPLY_SCALE", "应用胶囊缩放", ""),
    ))

    def execute(self, context):
        node = getattr(context, "node", None)
        if getattr(node, "bl_idname", "") != "SSMTNode_PostProcess_BoneMatrix":
            return {"CANCELLED"}
        if self.action == "REMOVE":
            if node.physics_bodies:
                index = min(node.physics_index, len(node.physics_bodies) - 1)
                body = node.physics_bodies[index]
                slot = body.slot
                viewport.remove_capsule(body)
                node.physics_bodies.remove(index)
                node.physics_index = max(0, index - 1)
                for i in reversed(range(len(node.physics_links))):
                    link = node.physics_links[i]
                    if slot in (link.first, link.second):
                        node.physics_links.remove(i)
                node.physics_link_index = min(node.physics_link_index, max(0, len(node.physics_links) - 1))
            return {"FINISHED"}
        if self.action == "SYNC":
            viewport.sync_capsules(node, context.scene)
            return {"FINISHED"}
        if self.action == "APPLY_SCALE":
            selected = set(getattr(context, "selected_objects", ()))
            capsules = [body for body in node.physics_bodies if body.viewport_object in selected]
            if not capsules:
                self.report({"WARNING"}, "先在物体模式选中需要应用缩放的胶囊")
                return {"CANCELLED"}
            for body in capsules:
                viewport.apply_capsule_scale(body)
            return {"FINISHED"}
        rigs = [entry.armature for entry in node.rigs if entry.armature] or [node.armature]
        active = getattr(context, "active_object", None)
        armature = active if active and active.type == "ARMATURE" else next((rig for rig in rigs if rig), None)
        selected = getattr(context, "selected_pose_bones", None)
        if not selected and armature is not None:
            selected = [
                pose for pose in armature.pose.bones
                if getattr(pose, "select", getattr(pose.bone, "select", False))
            ]
        bones = [pose.bone for pose in (selected or ()) if re.fullmatch(r"[0-9]+", pose.name)]
        if not bones:
            self.report({"WARNING"}, "先在骨架的姿态模式中选择数字编号的骨骼")
            return {"CANCELLED"}
        known = {item.slot: i for i, item in enumerate(node.physics_bodies)}
        for bone in bones:
            slot = int(bone.name)
            if slot in known:
                index = known[slot]
                item = node.physics_bodies[index]
            else:
                index = len(node.physics_bodies)
                item = node.physics_bodies.add()
                item.slot = slot
                item.length = max(float(bone.length), 0.012)
                item.radius = max(0.005, item.length * 0.23)
            item.kind = self.action
            node.physics_index = index
        viewport.sync_capsules(node, context.scene)
        return {"FINISHED"}


class SSMT_OT_BonePhysicsPreview(bpy.types.Operator):
    bl_idname = "ssmt4.bone_physics_preview"
    bl_label = "导出游戏物理预览"
    bl_description = "按当前蓝图导出并在模组载入时开启物理与胶囊显示"
    bl_options = {"INTERNAL"}

    def execute(self, context):
        node = getattr(context, "node", None)
        if getattr(node, "bl_idname", "") != "SSMTNode_PostProcess_BoneMatrix" or not node.physics_bodies:
            return {"CANCELLED"}
        result = bpy.ops.ssmt.generate_mod_blueprint(
            "INVOKE_DEFAULT", blueprint_name=node.id_data.name, bone_physics_preview_node_name=node.name,
        )
        if "CANCELLED" in result:
            return {"CANCELLED"}
        self.report({"INFO"}, "预览随模组导出，完成后在游戏中按 F10 重载")
        return {"FINISHED"}


def draw_physics(node, layout):
    box = layout.box()
    box.label(text="实时骨骼物理", icon="PHYSICS")
    row = box.row(align=True)
    for kind, label in (("COLLIDER", "添加碰撞胶囊"), ("DYNAMIC", "添加刚体胶囊")):
        edit = row.operator(SSMT_OT_BonePhysicsBodyEdit.bl_idname, text=label, icon="ADD")
        edit.action = kind
    if not node.physics_bodies:
        return
    row = box.row(align=True)
    row.prop(node, "physics_viewport_shapes")
    row.operator(SSMT_OT_BonePhysicsBodyEdit.bl_idname, text="同步胶囊", icon="FILE_REFRESH").action = "SYNC"
    row = box.row(align=True)
    row.prop(node, "physics_edit_capsules")
    row.operator(SSMT_OT_BonePhysicsBodyEdit.bl_idname, text="应用胶囊缩放").action = "APPLY_SCALE"
    row = box.row()
    row.template_list(SSMT_UL_BonePhysicsBodies.__name__, "", node, "physics_bodies", node, "physics_index", rows=3)
    row.operator(SSMT_OT_BonePhysicsBodyEdit.bl_idname, text="", icon="REMOVE").action = "REMOVE"
    body = node.physics_bodies[min(node.physics_index, len(node.physics_bodies) - 1)]
    row = box.row(align=True)
    row.prop(body, "slot")
    row.prop(body, "kind")
    box.prop(body, "shape")
    if viewport.uses_box(body):
        box.prop(body, "box_size")
        box.prop(body, "box_corner")
    else:
        row = box.row(align=True)
        row.prop(body, "radius")
        row.prop(body, "length")
    box.prop(body, "offset")
    box.prop(body, "friction")
    if body.kind == "DYNAMIC":
        row = box.row(align=True)
        row.prop(body, "mass")
        row.prop(body, "damping")
        box.prop(body, "air_drag")
        row = box.row(align=True)
        row.prop(body, "angle")
        row.prop(body, "stiffness")
        box.prop(body, "gravity_scale")
        box.prop(body, "pin_translation")
        row = box.row(align=True)
        row.prop(body, "limit_twist")
        if body.limit_twist:
            row.prop(body, "twist_angle")
        box.prop(body, "recovery_gap")
        box.prop(body, "allow_deep_overlap")
    else:
        box.prop(body, "sweep")
    links = box.box()
    links.label(text="软连接")
    row = links.row(align=True)
    row.template_list(SSMT_UL_BonePhysicsLinks.__name__, "", node, "physics_links",
                      node, "physics_link_index", rows=2)
    controls = row.column(align=True)
    controls.operator(SSMT_OT_BonePhysicsLinkEdit.bl_idname, text="", icon="ADD")
    controls.operator(SSMT_OT_BonePhysicsLinkEdit.bl_idname, text="", icon="REMOVE").remove = True
    if node.physics_links:
        link = node.physics_links[min(node.physics_link_index, len(node.physics_links) - 1)]
        row = links.row(align=True)
        row.prop(link, "first")
        row.prop(link, "second")
        row = links.row(align=True)
        row.prop(link, "stiffness")
        row.prop(link, "damping")
    row = box.row(align=True)
    row.prop(node, "physics_gravity")
    row.prop(node, "physics_force")
    box.prop(node, "physics_use_substeps")
    if node.physics_use_substeps:
        box.prop(node, "physics_adaptive_substeps")
        row = box.row(align=True)
        row.prop(node, "physics_step_hz")
        row.prop(node, "physics_max_substeps")
    else:
        box.prop(node, "physics_frame_steps")
    row = box.row(align=True)
    row.prop(node, "physics_joints_per_frame")
    row.prop(node, "physics_joint_iterations")
    row = box.row(align=True)
    row.prop(node, "physics_var")
    row.prop(node, "physics_key")
    row = box.row(align=True)
    row.prop(node, "physics_debug_var")
    row.prop(node, "physics_debug_key")
    row = box.row(align=True)
    row.prop(node, "physics_on_load")
    row.prop(node, "physics_debug_on_load")
    box.prop(node, "physics_reset_key")
    active = viewport.is_running(node)
    preparing = viewport.is_preparing(node)
    label = "正在准备预览，点击取消" if preparing else "停止 Blender 实时预览" if active else "Blender 实时预览"
    box.operator(viewport.SSMT_OT_BonePhysicsViewportPreview.bl_idname,
                 text=label, icon="TIME" if preparing else "PAUSE" if active else "PLAY")
    box.operator(SSMT_OT_BonePhysicsPreview.bl_idname, icon="HIDE_OFF")


def physics_controls(node, reserved):
    play, debug = map(variable_name, (node.physics_var, node.physics_debug_var))
    names = {name.casefold() for name in reserved}
    if play.casefold() == debug.casefold() or any(
        name.casefold() in names or name.casefold().startswith("$ssmt_") for name in (play, debug)
    ):
        raise ValueError("物理开关、显示开关、动画和定身变量需要使用独立名称")
    try:
        keys = tuple(map(hotkey_value, (node.physics_key, node.physics_debug_key, node.physics_reset_key)))
    except ValueError as exc:
        raise ValueError("物理快捷键格式无效，请使用 Ctrl+Alt+P 或 VK_F7 等按键名称") from exc
    if len({key.casefold() for key in keys if key}) != sum(bool(key) for key in keys):
        raise ValueError("物理快捷键不能重复")
    return play, debug, *keys


def bone_sources(node, scene):
    linked = list(dict.fromkeys(item.armature for item in (node.rigs or (node,)) if item.armature))
    workspaces = {str(arm.get("Workspace", "")) for arm in linked}
    imports = [obj for obj in scene.objects if obj.type == "ARMATURE" and obj not in linked
               and obj.get(WORKSPACE_IMPORT_RIG_MARKER) and obj.get("Workspace", "") in workspaces]
    return {
        int(bone.name): (armature, bone)
        for armature in (*imports, *linked) for bone in armature.data.bones
        if re.fullmatch(r"[0-9]+", bone.name)
    }


def build_bone_physics(node, scene, rigs, capacity, mirror_x, reserved):
    transforms = {}
    parents = {}
    worlds = {}
    for slot, (armature, bone) in bone_sources(node, scene).items():
        if armature not in worlds:
            worlds[armature] = armature.matrix_world.copy(), mirror_x(armature)
        world, mirror = worlds[armature]
        transforms[slot] = (world, bone, mirror)
        authored_parent = bone.parent
        while authored_parent is not None and not re.fullmatch(r"[0-9]+", authored_parent.name):
            authored_parent = authored_parent.parent
        if authored_parent is not None:
            parents[slot] = int(authored_parent.name)
    for _, hierarchy, _, _ in rigs:
        parents.update(hierarchy)

    settings = {}
    for item in node.physics_bodies:
        if item.slot in settings:
            raise ValueError(f"骨骼 {item.slot} 配置了多个胶囊")
        if not viewport.uses_box(item) and item.length < 2 * item.radius:
            raise ValueError(f"骨骼 {item.slot} 的胶囊总长需要大于或等于直径")
        settings[item.slot] = item
    actors = {}
    for slot, item in settings.items():
        if item.kind == "DYNAMIC":
            parent = parents.get(slot, -1)
            if parent >= 0 and parent not in settings:
                actors.setdefault(parent, None)
        actors[slot] = item

    capsules = []
    for slot, item in sorted(actors.items()):
        if slot not in transforms:
            raise ValueError(f"骨骼物理找不到编号 {slot} 的绑定姿势")
        world, bone, mirror = transforms[slot]
        bind = world @ bone.matrix_local
        head = bind.translation.copy()
        axis = bind.to_3x3() @ Vector((0, 1, 0))
        scale = axis.length
        axis.normalize()
        local_center = Vector(tuple(item.offset)) if item else Vector()
        local_center.y += bone.length / 2 if item else 0
        center = bind @ local_center
        side = (bind.to_3x3() @ Vector((1, 0, 0))).normalized()
        if mirror:
            head.x, center.x, axis.x = -head.x, -center.x, -axis.x
            side.x = -side.x
        radius, length = viewport.capsule_dimensions(item) if item else (0., 0.)
        collision_box = None
        if item and viewport.uses_box(item):
            dimensions, corner = viewport.box_dimensions(item)
            collision_box = RoundedBox(tuple(size * scale * .5 for size in dimensions),
                                       tuple(side), corner * scale)
            radius = min(dimensions[0], dimensions[2]) * .5
            length = 2.0 * radius
        parent = parents.get(slot, -1)
        visited = {slot}
        while parent >= 0:
            if parent in visited:
                raise ValueError(f"骨骼物理的父骨链存在循环：{parent}")
            if parent in actors:
                break
            visited.add(parent)
            parent = parents.get(parent, -1)
        pin_translation = bool(item and item.kind == "DYNAMIC" and item.pin_translation)
        parent_body = actors.get(parent)
        if pin_translation and parent_body and parent_body.kind == "DYNAMIC":
            raise ValueError(f"骨骼 {slot} 的锁定挂点需要连接到动画父骨")
        capsules.append(Capsule(
            slot, parent,
            (2 if item.kind == "DYNAMIC" else 1) if item else 0,
            tuple(center), tuple(axis), tuple(head),
            scale * radius, scale * max(0.0, length / 2 - radius),
            item.mass if item else 0, item.damping if item else 0, item.stiffness if item else 0,
            item.friction if item else 0, item.angle if item else 0, item.gravity_scale if item else 1,
            pin_translation=pin_translation,
            air_drag=item.air_drag if item and item.kind == "DYNAMIC" else 0.0,
            twist_limit=item.twist_angle if item and item.kind == "DYNAMIC" and item.limit_twist else None,
            recovery_gap=item.recovery_gap if item and item.kind == "DYNAMIC" else 0.0,
            sweep=bool(item and item.kind == "COLLIDER" and item.sweep),
            box=collision_box,
            allow_deep_overlap=item.allow_deep_overlap if item and item.kind == "DYNAMIC" else True,
        ))
    return BonePhysics(
        tuple(capsules), build_followers(capsules, parents, capacity), *physics_controls(node, reserved),
        node.physics_on_load, node.physics_debug_on_load, node.physics_gravity, node.physics_force,
        node.physics_step_hz, node.physics_max_substeps, node.physics_joint_iterations,
        use_substeps=node.physics_use_substeps,
        adaptive_substeps=node.physics_adaptive_substeps,
        frame_steps=node.physics_frame_steps,
        joints_per_frame=node.physics_joints_per_frame,
        soft_links=tuple(SoftLink(row.first, row.second, row.stiffness, row.damping)
                         for row in node.physics_links),
    )


CLASSES = (SSMT_BonePhysicsBody, SSMT_BonePhysicsLink, SSMT_UL_BonePhysicsLinks, SSMT_OT_BonePhysicsLinkEdit,
           SSMT_UL_BonePhysicsBodies, SSMT_OT_BonePhysicsBodyEdit,
           SSMT_OT_BonePhysicsPreview, viewport.SSMT_OT_BonePhysicsViewportPreview)
