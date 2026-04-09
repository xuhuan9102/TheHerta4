import bpy


def get_object_used_bones(obj, armature_obj=None):
    if not obj or obj.type != 'MESH':
        return set()
    
    used_bones = set()
    
    if armature_obj is None:
        for modifier in obj.modifiers:
            if modifier.type == 'ARMATURE':
                armature_obj = modifier.object
                break
    
    if not armature_obj or armature_obj.type != 'ARMATURE':
        return used_bones
    
    all_bone_names = {bone.name for bone in armature_obj.data.bones}
    
    for vertex_group in obj.vertex_groups:
        bone_name = vertex_group.name
        if bone_name in all_bone_names:
            used_bones.add(bone_name)
    
    return used_bones


def hide_unused_bones(armature_obj, used_bones):
    if not armature_obj or armature_obj.type != 'ARMATURE':
        return
    
    bpy.ops.object.select_all(action='DESELECT')
    armature_obj.select_set(True)
    bpy.context.view_layer.objects.active = armature_obj
    
    current_mode = armature_obj.mode
    
    try:
        bpy.ops.object.mode_set(mode='OBJECT')
        for bone in armature_obj.data.bones:
            if bone.name in used_bones:
                bone.hide = False
                bone.hide_select = False
            else:
                bone.hide = True
                bone.hide_select = True
        
        bpy.ops.object.mode_set(mode='EDIT')
        for edit_bone in armature_obj.data.edit_bones:
            if edit_bone.name in used_bones:
                edit_bone.hide = False
                edit_bone.hide_select = False
            else:
                edit_bone.hide = True
                edit_bone.hide_select = True
        
        bpy.ops.object.mode_set(mode='POSE')
        for pose_bone in armature_obj.pose.bones:
            if pose_bone.name in used_bones:
                pose_bone.bone.hide = False
                pose_bone.bone.hide_select = False
            else:
                pose_bone.bone.hide = True
                pose_bone.bone.hide_select = True
        
        bpy.ops.object.mode_set(mode=current_mode)
        
    except RuntimeError as e:
        for bone in armature_obj.data.bones:
            if bone.name in used_bones:
                bone.hide = False
                bone.hide_select = False
            else:
                bone.hide = True
                bone.hide_select = True


def show_all_bones(armature_obj):
    if not armature_obj or armature_obj.type != 'ARMATURE':
        return
    
    bpy.ops.object.select_all(action='DESELECT')
    armature_obj.select_set(True)
    bpy.context.view_layer.objects.active = armature_obj
    
    current_mode = armature_obj.mode
    
    try:
        bpy.ops.object.mode_set(mode='OBJECT')
        for bone in armature_obj.data.bones:
            bone.hide = False
            bone.hide_select = False
        
        bpy.ops.object.mode_set(mode='EDIT')
        for edit_bone in armature_obj.data.edit_bones:
            edit_bone.hide = False
            edit_bone.hide_select = False
        
        bpy.ops.object.mode_set(mode='POSE')
        for pose_bone in armature_obj.pose.bones:
            pose_bone.bone.hide = False
            pose_bone.bone.hide_select = False
        
        bpy.ops.object.mode_set(mode=current_mode)
        
    except RuntimeError as e:
        for bone in armature_obj.data.bones:
            bone.hide = False
            bone.hide_select = False
