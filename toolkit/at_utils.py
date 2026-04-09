# -*- coding: utf-8 -*-

import bpy


def is_alembic_object(obj):
    """检查对象是否由Alembic缓存驱动"""
    if not obj or obj.type != 'MESH':
        return False
    for mod in obj.modifiers:
        if mod.type == 'MESH_SEQUENCE_CACHE':
            return True
    return False


def move_object_to_collection(obj, target_collection):
    """确保一个物体只存在于目标集合中"""
    if not obj or not target_collection:
        return
    for coll in list(obj.users_collection):
        coll.objects.unlink(obj)
    target_collection.objects.link(obj)
