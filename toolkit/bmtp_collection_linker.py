import bpy


class BMTP_OT_LinkListAdd(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_link_list_add"
    bl_label = "添加选中项至列表"
    bl_description = "将在大纲视图中选中的物体，以及当前激活的集合，添加到下方的源列表中"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        props = context.scene.bmtp_props
        source_list = props.link_source_list
        existing_items = {item.item for item in source_list}
        added_count = 0

        for obj in context.selected_objects:
            if obj not in existing_items:
                new_item = source_list.add()
                new_item.item = obj
                added_count += 1
        
        if context.view_layer.active_layer_collection:
            active_collection = context.view_layer.active_layer_collection.collection
            if active_collection and active_collection not in existing_items:
                new_item = source_list.add()
                new_item.item = active_collection
                added_count += 1

        if added_count == 0:
            self.report({'INFO'}, "没有新的项目被添加 (可能已存在于列表中或未选择任何内容)")
        else:
            self.report({'INFO'}, f"已添加 {added_count} 个新项目到源列表")

        return {'FINISHED'}


class BMTP_OT_LinkListRemove(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_link_list_remove"
    bl_label = "移除选中项"
    bl_description = "从源列表中移除当前选中的项目"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.bmtp_props
        return len(props.link_source_list) > 0 and props.link_source_list_index >= 0

    def execute(self, context):
        props = context.scene.bmtp_props
        index = props.link_source_list_index
        props.link_source_list.remove(index)
        if index > 0 and index >= len(props.link_source_list):
            props.link_source_list_index = len(props.link_source_list) - 1
        return {'FINISHED'}


class BMTP_OT_LinkListClear(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_link_list_clear"
    bl_label = "清空列表"
    bl_description = "一键清除源列表中的所有项目"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return len(context.scene.bmtp_props.link_source_list) > 0

    def execute(self, context):
        context.scene.bmtp_props.link_source_list.clear()
        return {'FINISHED'}


class BMTP_OT_ExecuteLink(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_execute_collection_link"
    bl_label = "执行关联"
    bl_description = "将源列表中的所有项目以'关联'方式添加到目标集合中"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.bmtp_props
        return props.link_target_collection and len(props.link_source_list) > 0

    def execute(self, context):
        props = context.scene.bmtp_props
        target_collection = props.link_target_collection
        
        linked_collections, linked_objects, skipped_items = 0, 0, 0

        for item_wrapper in props.link_source_list:
            source_item = item_wrapper.item
            if not source_item: continue

            if isinstance(source_item, bpy.types.Collection):
                if source_item == target_collection or target_collection in source_item.children_recursive:
                    self.report({'WARNING'}, f"无法将集合 '{source_item.name}' 关联到其自身或其子集合中，已跳过。")
                    skipped_items += 1
                    continue
                if source_item.name not in target_collection.children:
                    target_collection.children.link(source_item)
                    linked_collections += 1
                else: skipped_items += 1
            
            elif isinstance(source_item, bpy.types.Object):
                if source_item.name not in target_collection.objects:
                    target_collection.objects.link(source_item)
                    linked_objects += 1
                else: skipped_items += 1

        report_message = f"关联完成: {linked_collections} 个集合, {linked_objects} 个物体。"
        if skipped_items > 0: report_message += f" {skipped_items} 个项目被跳过。"
        self.report({'INFO'}, report_message)
        return {'FINISHED'}


class BMTP_OT_ExecuteUnlink(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_execute_collection_unlink"
    bl_label = "取消关联"
    bl_description = "从目标集合中移除(取消链接)源列表中的所有项目"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.bmtp_props
        return props.link_target_collection and len(props.link_source_list) > 0

    def execute(self, context):
        props = context.scene.bmtp_props
        target_collection = props.link_target_collection
        
        unlinked_collections, unlinked_objects, skipped_items = 0, 0, 0

        for item_wrapper in props.link_source_list:
            source_item = item_wrapper.item
            if not source_item: continue

            if isinstance(source_item, bpy.types.Collection):
                if source_item.name in target_collection.children:
                    target_collection.children.unlink(source_item)
                    unlinked_collections += 1
                else: skipped_items += 1
            
            elif isinstance(source_item, bpy.types.Object):
                if source_item.name in target_collection.objects:
                    target_collection.objects.unlink(source_item)
                    unlinked_objects += 1
                else: skipped_items += 1

        report_message = f"取消关联完成: {unlinked_collections} 个集合, {unlinked_objects} 个物体。"
        if skipped_items > 0: report_message += f" {skipped_items} 个项目因未在目标集合中找到而被跳过。"
        self.report({'INFO'}, report_message)
        return {'FINISHED'}


bmtp_collection_linker_list = (
    BMTP_OT_LinkListAdd,
    BMTP_OT_LinkListRemove,
    BMTP_OT_LinkListClear,
    BMTP_OT_ExecuteLink,
    BMTP_OT_ExecuteUnlink,
)
