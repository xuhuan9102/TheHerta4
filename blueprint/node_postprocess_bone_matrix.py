"""Bind a Blender armature animation to ZZMI's merged bone slots."""

import re
from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix

from ..common.global_config import GlobalConfig
from ..common.logic_name import LogicName
from ..common.zzmi_armature_reconstruction import (
    WORKSPACE_IMPORT_BONE_PARENT,
    WORKSPACE_IMPORT_RIG_MIRROR_X,
    WORKSPACE_IMPORT_RIG_ROOT,
)
from ..common.zzmi_bone_matrix_layers import BoneMatrixLayer
from ..common.zzmi_bone_matrix_playback import (
    BoneMatrixClip,
    attachment_slots,
    bone_order,
    export_cb1_snapshot,
    export_clip,
    merge_pose_clips,
    native_palette_size,
    snapshot_settings,
    variable_name,
)
from ..common.zzmi_bone_matrix_snapshot import DEFAULT_CB1_KEY, DEFAULT_CB1_VARIABLE, hotkey_value
from .export_helper import BlueprintExportHelper
from .node_postprocess_base import SSMTNode_PostProcess_Base
from .node_postprocess_comment_cleanup import SSMTNode_PostProcess_CommentCleanup

NODE_IDNAME = "SSMTNode_PostProcess_BoneMatrix"


def _armatures_only(_node, obj):
    return obj.type == "ARMATURE"


def _numbered_bones(armature):
    bones = {}
    for bone in armature.data.bones:
        if not re.fullmatch(r"[0-9]+", bone.name):
            continue
        slot = int(bone.name)
        if slot > 65535 or slot in bones:
            raise ValueError(f"骨骼编号无效或重复：{bone.name}")
        bones[slot] = bone
    if not bones:
        raise ValueError("所选骨架没有以全局顶点组编号命名的骨骼")
    return dict(sorted(bones.items()))


def _mirror_x(armature):
    if WORKSPACE_IMPORT_RIG_MIRROR_X in armature:
        return bool(armature[WORKSPACE_IMPORT_RIG_MIRROR_X])
    from ..common.non_mirror_workflow import NonMirrorWorkflowHelper

    return any(
        NonMirrorWorkflowHelper._should_restore_export_object(obj)
        for obj in armature.children if obj.type == "MESH"
    )


def _parent_slot(bone):
    if WORKSPACE_IMPORT_BONE_PARENT in bone:
        return int(bone[WORKSPACE_IMPORT_BONE_PARENT])
    return _authored_parent_slot(bone)


def _authored_parent_slot(bone):
    parent = bone.parent
    while parent is not None and not re.fullmatch(r"[0-9]+", parent.name):
        parent = parent.parent
    return int(parent.name) if parent is not None else None


def _direct_parent_slots(bones, armature, scene):
    result = {slot: parent for slot, bone in bones.items() if (parent := _parent_slot(bone)) is not None}
    missing = bones.keys() - result.keys()
    workspace = armature.get("Workspace")
    if not missing or not workspace:
        return result

    # Older partial rigs can use the matching original rig still in the scene.
    candidates = []
    for source in scene.objects:
        if source == armature or source.type != "ARMATURE" or source.get("Workspace") != workspace:
            continue
        rest_bones = {slot: source.data.bones.get(bone.name) for slot, bone in bones.items()}
        if len(source.data.bones) > len(bones) and all(
            rest_bones[slot] is not None and np.allclose(
                rest_bones[slot].matrix_local, bone.matrix_local, rtol=0, atol=1e-5,
            )
            for slot, bone in bones.items()
        ):
            candidates.append(rest_bones)
    for slot in missing:
        parents = {_parent_slot(source[slot]) for source in candidates}
        if len(parents) == 1:
            parent, = parents
            if parent is not None:
                result[slot] = parent
    return result


def bake_pose_frames(context, armature, first_frame: int, last_frame: int) -> BoneMatrixClip:
    bones = _numbered_bones(armature)
    slots = tuple(bones)
    names = {bone.name for bone in bones.values()}
    inferred_anchor = next(
        (slot for slot, bone in bones.items() if bone.parent is None or bone.parent.name not in names),
        slots[0],
    )
    anchor = int(armature.get(WORKSPACE_IMPORT_RIG_ROOT, inferred_anchor))
    if not 0 <= anchor <= 65535:
        raise ValueError(f"骨架的校准根骨编号无效：{anchor}")
    frame_count = last_frame - first_frame + 1
    scene = context.scene
    if frame_count < 1:
        raise ValueError("结束帧必须大于或等于起始帧")
    if armature.name not in scene.objects:
        raise ValueError("所选骨架不在当前导出场景")
    if frame_count * len(slots) * 3 > (1 << 27):
        raise ValueError("采样帧和骨骼数量超过 D3D11 缓冲容量")

    previous_frame = scene.frame_current
    previous_subframe = scene.frame_subframe
    mirror = Matrix.Diagonal((-1.0, 1.0, 1.0, 1.0)) if _mirror_x(armature) else None
    matrices = np.empty((frame_count, len(slots), 3, 4), dtype="<f4")
    pivot = None
    try:
        scene.frame_set(first_frame)
        graph = context.evaluated_depsgraph_get()
        bind_world = armature.evaluated_get(graph).matrix_world.copy()
        if anchor in bones:
            position = bind_world @ bones[anchor].head_local
            if mirror is not None:
                position = mirror @ position
            pivot = tuple(position)
        inverse_bind = {
            slot: (bind_world @ bone.matrix_local).inverted()
            for slot, bone in bones.items()
        }
        for index in range(frame_count):
            if index:
                scene.frame_set(first_frame + index)
            evaluated = armature.evaluated_get(graph)
            for track, slot in enumerate(slots):
                pose = evaluated.pose.bones[bones[slot].name]
                matrix = evaluated.matrix_world @ pose.matrix @ inverse_bind[slot]
                if mirror is not None:
                    matrix = mirror @ matrix @ mirror
                matrices[index, track] = np.asarray(matrix)[:3]
    finally:
        scene.frame_set(previous_frame, subframe=previous_subframe)
    if not np.isfinite(matrices).all():
        raise ValueError("骨架求值得到了无效的蒙皮矩阵")
    parents = attachment_slots(slots, _direct_parent_slots(bones, armature, scene), anchor)
    return BoneMatrixClip(slots, anchor, first_frame, frame_count, matrices.tobytes(), pivot, parents)


class SSMT_BoneMatrixParentLink(bpy.types.PropertyGroup):
    child_slot: bpy.props.IntProperty(name="子骨编号", min=0, max=65535)
    parent_slot: bpy.props.IntProperty(name="父骨编号", min=0, max=65535)


class SSMT_BoneMatrixRig(bpy.types.PropertyGroup):
    armature: bpy.props.PointerProperty(name="骨架", type=bpy.types.Object, poll=_armatures_only)
    attachment_slot: bpy.props.IntProperty(
        name="默认挂接骨", description="链的根跟随此全局顶点组的骨骼，-1 沿用原骨骼链",
        default=-1, min=-1, max=65535,
    )
    parent_links: bpy.props.CollectionProperty(type=SSMT_BoneMatrixParentLink)
    parent_index: bpy.props.IntProperty(default=0)
    play_variable: bpy.props.StringProperty(name="播放开关")
    frame_variable: bpy.props.StringProperty(name="播放变量")
    play_on_load: bpy.props.BoolProperty(name="默认开启播放", default=False)
    initial_frame: bpy.props.IntProperty(name="默认播放帧", default=0)
    play_key: bpy.props.StringProperty(name="播放快捷键", description="留空时通过开关变量控制")


def _rig_variables(item, index):
    return (
        variable_name(item.play_variable or f"$bone_matrix_rig_{index + 1}_play"),
        variable_name(item.frame_variable or f"$bone_matrix_rig_{index + 1}_frame"),
    )


def _editing_rig(node):
    if node.rigs:
        return node.rigs[min(max(0, node.rig_index), len(node.rigs) - 1)]
    return node if node.armature is not None else None


class SSMT_UL_BoneMatrixRigs(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        layout.prop(item, "armature", text="", icon="ARMATURE_DATA")


class SSMT_UL_BoneMatrixParents(bpy.types.UIList):
    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        row.prop(item, "child_slot", text="子骨")
        row.prop(item, "parent_slot", text="父骨")


class SSMT_OT_BoneMatrixListEdit(bpy.types.Operator):
    bl_idname = "ssmt4.bone_matrix_list_edit"
    bl_label = "编辑骨骼矩阵列表"
    bl_options = {"UNDO", "INTERNAL"}

    target: bpy.props.EnumProperty(items=(("RIG", "骨架", ""), ("PARENT", "父骨", "")))
    remove: bpy.props.BoolProperty(default=False)
    direction: bpy.props.IntProperty(default=0, min=-1, max=1)

    def execute(self, context):
        node = getattr(context, "node", None)
        if getattr(node, "bl_idname", None) != NODE_IDNAME:
            return {"CANCELLED"}
        if self.target == "RIG":
            if not self.remove and not self.direction and not node.rigs and node.armature is not None:
                legacy = node.rigs.add()
                legacy.armature = node.armature
                legacy.attachment_slot = node.attachment_slot
                legacy.play_variable, legacy.frame_variable = node.play_variable, node.frame_variable
                legacy.play_on_load, legacy.initial_frame = node.play_on_load, node.initial_frame
                for source in node.parent_links:
                    target = legacy.parent_links.add()
                    target.child_slot, target.parent_slot = source.child_slot, source.parent_slot
                node.armature = None
                node.attachment_slot = -1
                node.parent_links.clear()
            owner, collection, index_property = node, node.rigs, "rig_index"
        else:
            owner = _editing_rig(node)
            if owner is None or owner.armature is None:
                return {"CANCELLED"}
            collection, index_property = owner.parent_links, "parent_index"
        index = getattr(owner, index_property)
        if self.direction:
            next_index = index + self.direction
            if not 0 <= index < len(collection) or not 0 <= next_index < len(collection):
                return {"CANCELLED"}
            collection.move(index, next_index)
            setattr(owner, index_property, next_index)
        elif self.remove:
            if not 0 <= index < len(collection):
                return {"CANCELLED"}
            collection.remove(index)
            setattr(owner, index_property, max(0, min(index, len(collection) - 1)))
        else:
            item = collection.add()
            setattr(owner, index_property, len(collection) - 1)
            if self.target == "RIG":
                names = {
                    name.casefold()
                    for i, existing in enumerate(list(collection)[:-1])
                    for name in _rig_variables(existing, i)
                }
                candidate = len(collection) - 1
                pair = _rig_variables(item, candidate)
                while any(name.casefold() in names for name in pair):
                    candidate += 1
                    pair = _rig_variables(item, candidate)
                item.play_variable, item.frame_variable = pair
                item.play_on_load, item.initial_frame = node.play_on_load, node.initial_frame
            elif owner.armature is not None:
                existing = {entry.child_slot for entry in list(collection)[:-1]}
                bones = (bone for bone in owner.armature.data.bones if re.fullmatch(r"[0-9]+", bone.name))
                bone = next((bone for bone in bones if int(bone.name) not in existing), None)
                if bone is not None:
                    item.child_slot = int(bone.name)
                    parent = _parent_slot(bone)
                    item.parent_slot = parent if parent is not None and parent >= 0 else int(bone.name) + 1
        return {"FINISHED"}


class SSMTNode_PostProcess_BoneMatrix(SSMTNode_PostProcess_Base):
    bl_idname = NODE_IDNAME
    bl_label = "骨骼矩阵控制"
    bl_description = "按全局顶点组编号驱动 ZZMI 骨骼，支持新增骨骼、多骨架挂接和 CB1 定身"
    bl_icon = "ARMATURE_DATA"

    armature: bpy.props.PointerProperty(name="骨架", type=bpy.types.Object, poll=_armatures_only)
    rigs: bpy.props.CollectionProperty(type=SSMT_BoneMatrixRig)
    rig_index: bpy.props.IntProperty(default=0)
    attachment_slot: bpy.props.IntProperty(
        name="默认挂接骨", description="全局顶点组编号，-1 沿用原骨骼链", default=-1, min=-1, max=65535,
    )
    parent_links: bpy.props.CollectionProperty(type=SSMT_BoneMatrixParentLink)
    parent_index: bpy.props.IntProperty(default=0)
    frame_start: bpy.props.IntProperty(name="起始帧", default=0)
    frame_end: bpy.props.IntProperty(name="结束帧", default=30)
    play_variable: bpy.props.StringProperty(
        name="播放开关",
        description="该 INI 变量非零时启用矩阵替换",
        default="$bone_matrix_play",
    )
    frame_variable: bpy.props.StringProperty(
        name="播放变量",
        description="该 INI 变量指定当前帧，超出范围时取最近的端点帧",
        default="$bone_matrix_frame",
    )
    play_on_load: bpy.props.BoolProperty(
        name="默认开启播放",
        description="为播放开关设置 Mod 加载时的初值",
        default=False,
    )
    initial_frame: bpy.props.IntProperty(
        name="默认播放帧",
        description="为播放变量设置 Mod 加载时的初值",
        default=0,
    )
    cb1_variable: bpy.props.StringProperty(
        name="定身开关变量", description="非零时捕获并固定目标绘制的 CB1", default=DEFAULT_CB1_VARIABLE,
    )
    cb1_key: bpy.props.StringProperty(
        name="定身快捷键", description="可填写 Ctrl+Alt+B 等组合键，留空时通过变量控制", default=DEFAULT_CB1_KEY,
    )

    def _list_controls(self, row, target):
        tools = row.column(align=True)
        add = tools.operator(SSMT_OT_BoneMatrixListEdit.bl_idname, text="", icon="ADD")
        add.target, add.remove = target, False
        remove = tools.operator(SSMT_OT_BoneMatrixListEdit.bl_idname, text="", icon="REMOVE")
        remove.target, remove.remove = target, True
        if target == "RIG" and len(self.rigs) > 1:
            for direction, icon in ((-1, "TRIA_UP"), (1, "TRIA_DOWN")):
                move = tools.operator(SSMT_OT_BoneMatrixListEdit.bl_idname, text="", icon=icon)
                move.target, move.direction = target, direction

    def draw_buttons(self, context, layout):
        layout.context_pointer_set("node", self)
        rigs = layout.box()
        rigs.label(text="动画骨架", icon="ARMATURE_DATA")
        row = rigs.row()
        if not self.rigs and self.armature is not None:
            row.prop(self, "armature")
        else:
            row.template_list(SSMT_UL_BoneMatrixRigs.__name__, "", self, "rigs", self, "rig_index", rows=2)
        self._list_controls(row, "RIG")
        if len(self.rigs) > 1:
            rigs.label(text="同一编号以列表下方已开启的骨架为准")
        rig = _editing_rig(self)
        if rig is not None and rig.armature is not None:
            rigs.prop(rig, "attachment_slot")
            rigs.label(text="父骨覆盖（全局顶点组编号）")
            row = rigs.row()
            row.template_list(SSMT_UL_BoneMatrixParents.__name__, "", rig, "parent_links", rig, "parent_index", rows=2)
            self._list_controls(row, "PARENT")

        if rig is not None:
            playback = layout.box()
            playback.label(text="播放")
            row = playback.row(align=True)
            row.prop(self, "frame_start")
            row.prop(self, "frame_end")
            playback.prop(rig, "play_variable")
            playback.prop(rig, "frame_variable")
            row = playback.row(align=True)
            row.prop(rig, "play_on_load")
            row.prop(rig, "initial_frame")
            if self.rigs:
                playback.prop(rig, "play_key")

        snapshot = layout.box()
        snapshot.label(text="定身（CB1 快照）")
        snapshot.prop(self, "cb1_variable")
        snapshot.prop(self, "cb1_key")

    def _rig_configuration(self, scene):
        configs = []
        global_parents = {}
        items = self.rigs if self.rigs else ((self,) if self.armature is not None else ())
        for item in items:
            rig = item.armature
            if rig is None:
                raise ValueError("骨骼矩阵控制的每一项都需要选择骨架")
            bones = _numbered_bones(rig)
            parents = _direct_parent_slots(bones, rig, scene)
            for slot, parent in parents.items():
                global_parents.setdefault(slot, parent)
            source = {
                slot: parent for slot, bone in bones.items()
                if (parent := _authored_parent_slot(bone)) in bones
            }
            configs.append((item, bones, parents, source))

        result = []
        for item, bones, parents, source in configs:
            for slot in bones:
                if slot not in parents and slot in global_parents:
                    parents[slot] = global_parents[slot]
            manual_parents = set()
            if item.attachment_slot >= 0:
                for slot in bones:
                    if parents.get(slot) not in bones:
                        parents[slot] = item.attachment_slot
                manual_parents.add(item.attachment_slot)
            edited = set()
            for link in item.parent_links:
                if link.child_slot not in bones:
                    raise ValueError(f"父骨覆盖找不到子骨 {link.child_slot}")
                if link.child_slot == link.parent_slot or link.child_slot in edited:
                    raise ValueError(f"子骨 {link.child_slot} 的父骨覆盖重复或指向了自己")
                parents[link.child_slot] = link.parent_slot
                manual_parents.add(link.parent_slot)
                edited.add(link.child_slot)
            bone_order(bones, parents)
            result.append((item, parents, source, manual_parents))
        return result

    def _layer_controls(self):
        names = {variable_name(self.cb1_variable).casefold()}
        controls = []
        for i, item in enumerate(self.rigs):
            play, frame = _rig_variables(item, i)
            if any(var.casefold().startswith("$ssmt_bm_") for var in (play, frame)):
                raise ValueError("骨架的播放变量名不能使用导出器内部前缀 $ssmt_bm_")
            if play.casefold() == frame.casefold() or any(var.casefold() in names for var in (play, frame)):
                raise ValueError("每副骨架的播放开关、播放变量和定身变量需要使用不同的名称")
            names.update((play.casefold(), frame.casefold()))
            controls.append((
                play, frame,
                item.play_on_load if item.is_property_set("play_on_load") else self.play_on_load,
                item.initial_frame if item.is_property_set("initial_frame") else self.initial_frame,
                hotkey_value(item.play_key),
            ))
        return controls

    def validate_export_configuration(self):
        if GlobalConfig.logic_name != LogicName.ZZMI:
            raise ValueError("骨骼矩阵控制需要 ZZMI 导出")
        if _editing_rig(self) is None:
            snapshot_settings(self.cb1_variable, self.cb1_key)
            return
        if self.frame_start > self.frame_end:
            raise ValueError("骨骼矩阵控制的结束帧必须大于或等于起始帧")
        if self.rigs:
            snapshot_settings(self.cb1_variable, self.cb1_key)
            self._layer_controls()
        else:
            if variable_name(self.play_variable).casefold() == variable_name(self.frame_variable).casefold():
                raise ValueError("播放开关和播放变量必须使用不同的变量名")
            snapshot_settings(self.cb1_variable, self.cb1_key, self.play_variable, self.frame_variable)
        self._rig_configuration(bpy.context.scene)

    def execute_postprocess(self, mod_export_path):
        self.validate_export_configuration()
        expected = Path(mod_export_path) / f"{GlobalConfig.get_generate_mod_name()}.ini"
        if not expected.is_file():
            found, error = SSMTNode_PostProcess_CommentCleanup._find_target_ini_file(mod_export_path)
            if not found:
                raise ValueError(error)
            expected = Path(found)
        rigs = self._rig_configuration(bpy.context.scene)
        if not rigs:
            export_cb1_snapshot(expected, snapshot_settings(self.cb1_variable, self.cb1_key))
            return
        clips = [
            bake_pose_frames(bpy.context, item.armature, self.frame_start, self.frame_end)
            for item, _, _, _ in rigs
        ]
        native_capacity = native_palette_size(expected)
        output_tree = GlobalConfig._get_output_blueprint_tree() or self.id_data
        has_drag = any(
            node.bl_idname == "SSMTNode_PostProcess_DragInteraction"
            for node in BlueprintExportHelper._collect_postprocess_nodes(output_tree)
        )
        if self.rigs or has_drag or any(slot >= native_capacity for clip in clips for slot in clip.slots):
            slots = tuple(sorted({slot for clip in clips for slot in clip.slots}))
            reference = next((clip for clip in clips if clip.pivot is not None and clip.anchor < native_capacity), clips[0])
            manual_parents = tuple(sorted({parent for _, _, _, parents in rigs for parent in parents}))
            clip = BoneMatrixClip(
                slots, reference.anchor, reference.first_frame, reference.frame_count, b"",
                reference.pivot, manual_parents=manual_parents,
            )
            layers = [
                BoneMatrixLayer(baked, parents, source, *control)
                for baked, (_, parents, source, _), control in zip(
                    clips, rigs,
                    self._layer_controls() if self.rigs else [
                        (variable_name(self.play_variable), variable_name(self.frame_variable),
                         self.play_on_load, self.initial_frame, ""),
                    ],
                )
            ]
            export_clip(
                expected, None, None, clip, rig_layers=layers,
                cb1=snapshot_settings(self.cb1_variable, self.cb1_key),
            )
            return
        _, hierarchy, source_hierarchy, manual_parents = rigs[0]
        clip = merge_pose_clips(clips, hierarchy, source_hierarchy, sorted(manual_parents))
        export_clip(
            expected, variable_name(self.play_variable), variable_name(self.frame_variable), clip,
            play_on_load=self.play_on_load, initial_frame=self.initial_frame,
            cb1=snapshot_settings(self.cb1_variable, self.cb1_key, self.play_variable, self.frame_variable),
        )


_CLASSES = (
    SSMT_BoneMatrixParentLink, SSMT_BoneMatrixRig, SSMT_UL_BoneMatrixRigs, SSMT_UL_BoneMatrixParents,
    SSMT_OT_BoneMatrixListEdit, SSMTNode_PostProcess_BoneMatrix,
)


def register():
    for cls in _CLASSES:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(_CLASSES):
        bpy.utils.unregister_class(cls)
