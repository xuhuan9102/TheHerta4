"""Bind a Blender armature animation to ZZMI's merged bone slots."""

import re
from pathlib import Path

import bpy
import numpy as np
from mathutils import Matrix

from ..common.global_config import GlobalConfig
from ..common.logic_name import LogicName
from ..common.zzmi_armature_reconstruction import (
    WORKSPACE_IMPORT_RIG_MIRROR_X,
    WORKSPACE_IMPORT_RIG_ROOT,
)
from ..common.zzmi_bone_matrix_playback import (
    BoneMatrixClip,
    export_clip,
    variable_name,
)
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
    return BoneMatrixClip(slots, anchor, first_frame, frame_count, matrices.tobytes(), pivot)


class SSMTNode_PostProcess_BoneMatrix(SSMTNode_PostProcess_Base):
    bl_idname = NODE_IDNAME
    bl_label = "骨骼矩阵控制"
    bl_description = "按播放变量覆盖 ZZMI 合并骨架中已编号的骨骼矩阵"
    bl_icon = "ARMATURE_DATA"

    armature: bpy.props.PointerProperty(name="骨架", type=bpy.types.Object, poll=_armatures_only)
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

    def draw_buttons(self, context, layout):
        layout.prop(self, "armature")
        row = layout.row(align=True)
        row.prop(self, "frame_start")
        row.prop(self, "frame_end")
        layout.prop(self, "play_variable")
        layout.prop(self, "frame_variable")
        layout.prop(self, "play_on_load")
        layout.prop(self, "initial_frame")

    def validate_export_configuration(self):
        if GlobalConfig.logic_name != LogicName.ZZMI:
            raise ValueError("骨骼矩阵控制需要 ZZMI 合并骨架导出")
        if self.armature is None:
            raise ValueError("骨骼矩阵控制需要选择一副骨架")
        if self.frame_start > self.frame_end:
            raise ValueError("骨骼矩阵控制的结束帧必须大于或等于起始帧")
        if variable_name(self.play_variable).casefold() == variable_name(self.frame_variable).casefold():
            raise ValueError("播放开关和播放变量必须使用不同的变量名")
        _numbered_bones(self.armature)

    def execute_postprocess(self, mod_export_path):
        self.validate_export_configuration()
        expected = Path(mod_export_path) / f"{GlobalConfig.get_generate_mod_name()}.ini"
        if not expected.is_file():
            found, error = SSMTNode_PostProcess_CommentCleanup._find_target_ini_file(mod_export_path)
            if not found:
                raise ValueError(error)
            expected = Path(found)
        clip = bake_pose_frames(bpy.context, self.armature, self.frame_start, self.frame_end)
        export_clip(
            expected, variable_name(self.play_variable), variable_name(self.frame_variable), clip,
            play_on_load=self.play_on_load, initial_frame=self.initial_frame,
        )


def register():
    bpy.utils.register_class(SSMTNode_PostProcess_BoneMatrix)


def unregister():
    bpy.utils.unregister_class(SSMTNode_PostProcess_BoneMatrix)
