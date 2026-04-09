import bpy
import json


class BMTP_OT_TransferWeights(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_transfer_weights"
    bl_label = "执行权重传递"
    bl_options = {'REGISTER', 'UNDO'}
    
    def apply_shapekey_positions(self, obj):
        if not obj.data.shape_keys:
            return False
            
        shape_keys = obj.data.shape_keys.key_blocks
        
        if len(shape_keys) <= 1:
            return False
            
        active_key = obj.active_shape_key_index
        
        temp_mesh = obj.data.copy()
        temp_mesh.transform(obj.matrix_world)
        
        for i, shape_key in enumerate(shape_keys):
            if i == 0:
                continue
                
            obj.active_shape_key_index = i
            shape_key.value = 1.0
            
            obj.data.update()
        
        obj.data.update()
        
        try:
            bpy.ops.object.mode_set(mode='OBJECT')
        except:
            pass
        
        applied_positions = []
        for vert in obj.data.vertices:
            applied_positions.append(vert.co.copy())
        
        for i, shape_key in enumerate(shape_keys):
            if i == 0:
                continue
            shape_key.value = 0.0
            
        obj.active_shape_key_index = active_key
        
        obj.data.update()
        
        for i, vert in enumerate(obj.data.vertices):
            if i < len(applied_positions):
                vert.co = applied_positions[i]
        
        return True

    def execute(self, context):
        props = context.scene.bmtp_props
        source_obj = props.wt_source_obj
        if not source_obj:
            self.report({'ERROR'}, "请指定源物体")
            return {'CANCELLED'}
        target_objects = [obj for obj in context.selected_objects if obj != source_obj and obj.type == 'MESH']
        if not target_objects:
            self.report({'ERROR'}, "请选择至少一个目标网格物体")
            return {'CANCELLED'}
        
        original_selection = {obj: obj.select_get() for obj in context.view_layer.objects}
        original_active = context.active_object
        
        original_modes = {}
        for obj in [source_obj] + target_objects:
            original_modes[obj.name] = obj.mode
        
        source_shapekey_applied = False
        if props.wt_use_shapekey_positions:
            source_shapekey_applied = self.apply_shapekey_positions(source_obj)
        
        target_shapekey_applied = {}
        if props.wt_use_shapekey_positions:
            for target_obj in target_objects:
                target_shapekey_applied[target_obj.name] = self.apply_shapekey_positions(target_obj)
        
        if props.wt_use_selected_groups:
            selected_vg_names = [item.name for item in props.wt_vertex_groups if item.selected]
            
            if not selected_vg_names:
                self.report({'ERROR'}, "请至少选择一个顶点组进行传递")
                return {'CANCELLED'}
            
            self.report({'INFO'}, f"将传递以下顶点组: {', '.join(selected_vg_names)}")
            
            backup_data = None
            if hasattr(context.scene, 'bmtp_vertex_group_weights_backup') and source_obj.name in context.scene['bmtp_vertex_group_weights_backup']:
                try:
                    backup_json = context.scene['bmtp_vertex_group_weights_backup'][source_obj.name]
                    backup_data = json.loads(backup_json)
                except:
                    pass
            
            if not backup_data:
                self.report({'WARNING'}, "未找到权重备份数据，正在创建临时备份...")
                backup_data = {}
                for vg in source_obj.vertex_groups:
                    backup_data[vg.name] = {}
                    for i, v in enumerate(source_obj.data.vertices):
                        try:
                            weight = vg.weight(i)
                            if weight > 0:
                                backup_data[vg.name][str(i)] = weight
                        except:
                            pass
                
                self.report({'INFO'}, f"备份完成，共备份 {len(backup_data)} 个顶点组")
                for vg_name, weights in backup_data.items():
                    self.report({'INFO'}, f"  {vg_name}: {len(weights)} 个顶点有权重")
            
            temp_removed_groups = []
            temp_removed_vg_objects = []
            for vg in source_obj.vertex_groups:
                if vg.name not in selected_vg_names:
                    temp_removed_groups.append(vg.name)
                    temp_removed_vg_objects.append(vg)
            
            self.report({'INFO'}, f"选中的顶点组: {selected_vg_names}")
            self.report({'INFO'}, f"将要删除的顶点组: {temp_removed_groups}")
            self.report({'INFO'}, f"删除前源物体有 {len(source_obj.vertex_groups)} 个顶点组")
            
            for vg in temp_removed_vg_objects:
                source_obj.vertex_groups.remove(vg)
            
            self.report({'INFO'}, f"删除后源物体有 {len(source_obj.vertex_groups)} 个顶点组")
            
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
                
                source_obj.select_set(True)
                context.view_layer.objects.active = source_obj
                
                for target_obj in target_objects:
                    target_obj.select_set(True)
                    
                    if props.wt_cleanup:
                        target_obj.vertex_groups.clear()
                    
                    for vg in source_obj.vertex_groups:
                        if vg.name not in target_obj.vertex_groups:
                            target_obj.vertex_groups.new(name=vg.name)
                    
                    modifier = target_obj.modifiers.new(name="TempWeightTransfer", type='DATA_TRANSFER')
                    modifier.object = source_obj
                    modifier.use_loop_data = False
                    modifier.use_vert_data = True
                    modifier.data_types_loops = set()
                    modifier.data_types_verts = {'VGROUP_WEIGHTS'}
                    modifier.vert_mapping = 'POLY_NEAREST'
                    modifier.mix_mode = 'REPLACE'
                    modifier.mix_factor = 1.0
                    
                    context.view_layer.objects.active = target_obj
                    bpy.ops.object.modifier_apply(modifier=modifier.name)
                    
                    target_obj.select_set(False)
            finally:
                if original_active and original_active.name in bpy.data.objects:
                    context.view_layer.objects.active = original_active
                
                for obj, selected in original_selection.items():
                    if obj.name in bpy.data.objects:
                        obj.select_set(selected)
                
                for obj_name, mode in original_modes.items():
                    if obj_name in bpy.data.objects:
                        obj = bpy.data.objects[obj_name]
                        context.view_layer.objects.active = obj
                        bpy.ops.object.mode_set(mode=mode)
                
                for vg_name in temp_removed_groups:
                    vg = source_obj.vertex_groups.new(name=vg_name)
                    if backup_data and vg_name in backup_data:
                        self.report({'INFO'}, f"正在恢复顶点组 {vg_name}，共 {len(backup_data[vg_name])} 个顶点")
                        for vert_idx_str, weight in backup_data[vg_name].items():
                            vert_idx = int(vert_idx_str)
                            vg.add([vert_idx], weight, 'REPLACE')
                    else:
                        self.report({'WARNING'}, f"未找到顶点组 {vg_name} 的备份数据")
                
                self.report({'INFO'}, f"恢复后源物体有 {len(source_obj.vertex_groups)} 个顶点组")
                
                if temp_removed_groups:
                    self.report({'INFO'}, f"已恢复以下顶点组: {', '.join(temp_removed_groups)}")
        else:
            self.report({'INFO'}, "将传递所有顶点组")
            
            for obj in context.view_layer.objects:
                obj.select_set(False)
            
            try:
                bpy.ops.object.mode_set(mode='OBJECT')
                
                source_obj.select_set(True)
                context.view_layer.objects.active = source_obj
                
                for target_obj in target_objects:
                    target_obj.select_set(True)
                    
                    if props.wt_cleanup:
                        target_obj.vertex_groups.clear()
                    
                    for vg in source_obj.vertex_groups:
                        if vg.name not in target_obj.vertex_groups:
                            target_obj.vertex_groups.new(name=vg.name)
                    
                    modifier = target_obj.modifiers.new(name="TempWeightTransfer", type='DATA_TRANSFER')
                    modifier.object = source_obj
                    modifier.use_loop_data = False
                    modifier.use_vert_data = True
                    modifier.data_types_loops = set()
                    modifier.data_types_verts = {'VGROUP_WEIGHTS'}
                    modifier.vert_mapping = 'POLY_NEAREST'
                    modifier.mix_mode = 'REPLACE'
                    modifier.mix_factor = 1.0
                    
                    context.view_layer.objects.active = target_obj
                    bpy.ops.object.modifier_apply(modifier=modifier.name)
                    
                    target_obj.select_set(False)
            finally:
                if original_active and original_active.name in bpy.data.objects:
                    context.view_layer.objects.active = original_active
                
                for obj, selected in original_selection.items():
                    if obj.name in bpy.data.objects:
                        obj.select_set(selected)
        
        if source_shapekey_applied:
            pass
        
        if props.wt_use_shapekey_positions:
            for target_obj in target_objects:
                if target_obj.name in target_shapekey_applied and target_shapekey_applied[target_obj.name]:
                    pass
        
        self.report({'INFO'}, f"成功将权重从 '{source_obj.name}' 传递到 {len(target_objects)} 个物体")
        return {'FINISHED'}


class BMTP_OT_RefreshVertexGroups(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_refresh_vertex_groups"
    bl_label = "刷新顶点组列表"
    bl_description = "刷新源物体的顶点组列表并备份权重数据"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        props = context.scene.bmtp_props
        return props.wt_source_obj is not None

    def execute(self, context):
        props = context.scene.bmtp_props
        source_obj = props.wt_source_obj
        
        if not source_obj or source_obj.type != 'MESH':
            self.report({'ERROR'}, "请选择一个有效的网格物体作为源物体")
            return {'CANCELLED'}
        
        props.wt_vertex_groups.clear()
        
        vertex_group_weights = {}
        
        for vg in source_obj.vertex_groups:
            vertex_group_weights[vg.name] = {}
            for i, v in enumerate(source_obj.data.vertices):
                try:
                    weight = vg.weight(i)
                    if weight > 0:
                        vertex_group_weights[vg.name][str(i)] = weight
                except:
                    pass
        
        backup_json = json.dumps(vertex_group_weights)
        
        if not hasattr(context.scene, 'bmtp_vertex_group_weights_backup'):
            context.scene['bmtp_vertex_group_weights_backup'] = {}
        context.scene['bmtp_vertex_group_weights_backup'][source_obj.name] = backup_json
        
        for i, vg in enumerate(source_obj.vertex_groups):
            item = props.wt_vertex_groups.add()
            item.name = vg.name
            item.index = i
            item.selected = True
        
        self.report({'INFO'}, f"已刷新 '{source_obj.name}' 的顶点组列表并备份了 {len(source_obj.vertex_groups)} 个顶点组的权重数据")
        return {'FINISHED'}


class BMTP_OT_SelectAllVertexGroups(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_select_all_vertex_groups"
    bl_label = "全选/全不选顶点组"
    bl_options = {'REGISTER', 'UNDO'}
    
    select: bpy.props.BoolProperty(
        name="选择状态",
        description="True为全选，False为全不选",
        default=True
    )
    
    def execute(self, context):
        props = context.scene.bmtp_props
        
        for item in props.wt_vertex_groups:
            item.selected = self.select
        
        action = "全选" if self.select else "全不选"
        self.report({'INFO'}, f"已{action}所有顶点组")
        return {'FINISHED'}


class BMTP_OT_SmoothWeights(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_smooth_weights"
    bl_label = "平滑选中物体权重"
    bl_description = "对所有选中网格物体的全部顶点组执行权重平滑操作"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def execute(self, context):
        props = context.scene.bmtp_props
        
        selected_mesh_objects = [obj for obj in context.selected_objects if obj.type == 'MESH' and obj.vertex_groups]
        
        if not selected_mesh_objects:
            self.report({'WARNING'}, "未选择任何带有顶点组的网格物体")
            return {'CANCELLED'}

        original_active = context.active_object
        original_mode = original_active.mode if original_active else 'OBJECT'
        
        processed_count = 0
        
        try:
            for obj in selected_mesh_objects:
                context.view_layer.objects.active = obj
                obj.select_set(True)
                
                bpy.ops.object.mode_set(mode='WEIGHT_PAINT')
                
                bpy.ops.object.vertex_group_smooth(
                    group_select_mode='ALL',
                    factor=props.wt_smooth_factor,
                    repeat=props.wt_smooth_repeat,
                    expand=0
                )
                
                bpy.ops.object.mode_set(mode='OBJECT')
                processed_count += 1
        
        finally:
            if original_active and original_active.name in bpy.data.objects:
                context.view_layer.objects.active = original_active
                if context.object.mode != original_mode:
                    try:
                        bpy.ops.object.mode_set(mode=original_mode)
                    except RuntimeError:
                        bpy.ops.object.mode_set(mode='OBJECT')

            for obj in selected_mesh_objects:
                obj.select_set(True)

        self.report({'INFO'}, f"已对 {processed_count} 个物体的权重进行平滑处理")
        return {'FINISHED'}


class BMTP_OT_SpreadWeights(bpy.types.Operator):
    bl_idname = "toolkit.bmtp_spread_weights"
    bl_label = "权重扩散"
    bl_description = "将权重从有权重的顶点扩散到相邻的无权重顶点"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        return True

    def spread_weights_for_object(self, obj, max_iter):
        mesh = obj.data
        verts = mesh.vertices
        edges = mesh.edges
        vgroups = obj.vertex_groups

        if len(vgroups) == 0:
            return False

        adj = [[] for _ in range(len(verts))]
        for e in edges:
            v1, v2 = e.vertices
            adj[v1].append(v2)
            adj[v2].append(v1)

        def has_weights(vertex):
            for g in vertex.groups:
                if g.weight > 0:
                    return True
            return False

        def get_weights(vertex):
            wdict = {}
            for g in vertex.groups:
                if g.weight > 0:
                    wdict[g.group] = g.weight
            return wdict

        for iteration in range(max_iter):
            changed = False
            updates = []

            for i, v in enumerate(verts):
                if has_weights(v):
                    continue

                neighbor_weights = []
                for n_idx in adj[i]:
                    n_vert = verts[n_idx]
                    if has_weights(n_vert):
                        neighbor_weights.append(get_weights(n_vert))

                if not neighbor_weights:
                    continue

                sum_weights = {}
                count = len(neighbor_weights)
                for wdict in neighbor_weights:
                    for g_idx, w in wdict.items():
                        sum_weights[g_idx] = sum_weights.get(g_idx, 0.0) + w

                avg_weights = {g_idx: total / count for g_idx, total in sum_weights.items()}

                total = sum(avg_weights.values())
                if total > 0:
                    avg_weights = {g_idx: w / total for g_idx, w in avg_weights.items()}
                    updates.append((i, avg_weights))
                    changed = True

            for v_idx, wdict in updates:
                v = verts[v_idx]
                for g in vgroups:
                    g.remove([v_idx])
                for g_idx, weight in wdict.items():
                    if weight > 0:
                        vgroups[g_idx].add([v_idx], weight, 'REPLACE')

            if not changed:
                break

        return True

    def execute(self, context):
        props = context.scene.bmtp_props
        
        selected_mesh_objects = [obj for obj in context.selected_objects if obj.type == 'MESH']
        
        if not selected_mesh_objects:
            self.report({'WARNING'}, "未选择任何网格物体")
            return {'CANCELLED'}

        original_active = context.active_object
        original_modes = {}
        for obj in selected_mesh_objects:
            original_modes[obj.name] = obj.mode

        try:
            bpy.ops.object.mode_set(mode='OBJECT')

            processed_count = 0
            for obj in selected_mesh_objects:
                context.view_layer.objects.active = obj
                
                if len(obj.vertex_groups) == 0:
                    continue
                
                if self.spread_weights_for_object(obj, props.wt_spread_iterations):
                    processed_count += 1

        finally:
            if original_active and original_active.name in bpy.data.objects:
                context.view_layer.objects.active = original_active

            for obj_name, mode in original_modes.items():
                if obj_name in bpy.data.objects:
                    obj = bpy.data.objects[obj_name]
                    context.view_layer.objects.active = obj
                    try:
                        bpy.ops.object.mode_set(mode=mode)
                    except RuntimeError:
                        bpy.ops.object.mode_set(mode='OBJECT')

            for obj in selected_mesh_objects:
                obj.select_set(True)

        if processed_count > 0:
            self.report({'INFO'}, f"已对 {processed_count} 个物体的权重进行扩散处理")
        else:
            self.report({'WARNING'}, "没有物体需要处理（可能所有物体都没有顶点组）")
        
        return {'FINISHED'}


bmtp_weight_tools_list = (
    BMTP_OT_TransferWeights,
    BMTP_OT_RefreshVertexGroups,
    BMTP_OT_SelectAllVertexGroups,
    BMTP_OT_SmoothWeights,
    BMTP_OT_SpreadWeights,
)
