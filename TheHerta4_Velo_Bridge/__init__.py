bl_info = {'name': 'TheHerta4 Velo Bridge', 'version': (0, 2, 0), 'blender': (4, 4, 0), 'category': 'Node'}
import bpy
import importlib.util
import re
from pathlib import Path
from bpy.props import StringProperty

NODE_ID = 'SSMTNode_VeloExportBridge'
BRIDGE_VERSION = '0.2.0'
LOG_PREFIX = '[TheHerta4][VeloBridge][Experimental] '

# 前置插件检测：未安装 Velo Tools 时节点仍可创建（便于搭建蓝图），
# 但导入/导出操作在执行时会直接失败并给出明确提示（参考 NTMI 的依赖检测模式）。
VELO_TOOLS_AVAILABLE = importlib.util.find_spec('velo_tools') is not None


def _require_velo_tools(self):
    """执行导入/导出前检查 Velo Tools 前置插件；未安装时直接终止操作。"""
    if VELO_TOOLS_AVAILABLE:
        return True
    message = '未检测到前置插件 Velo Tools，无法执行 Velo 导入/导出。请先安装并启用 Velo Tools。'
    _debug('velo_tools_missing: ' + message)
    try:
        self.report({'ERROR'}, message)
    except Exception:
        print(message)
    return False

def _debug(message):
    message = LOG_PREFIX + str(message)
    print(message)
    try:
        p = Path.home() / 'TheHerta4_Velo_Bridge.debug.log'
        with p.open('a', encoding='utf-8') as f:
            f.write(message + '\n')
    except Exception:
        pass

def workspace(scene):
    from velo_tools.games.registry import get_active_descriptor
    desc = get_active_descriptor(scene)
    return desc, desc.settings(scene) if desc else None

def linked_objects(node):
    result, seen, visiting = [], set(), set()
    def walk(n):
        key = n.as_pointer()
        if key in visiting:
            raise ValueError('蓝图存在循环连接')
        if key in seen:
            return
        visiting.add(key)
        if n.bl_idname == 'SSMTNode_Object_Info':
            obj = bpy.data.objects.get(n.object_name)
            if obj is None:
                raise ValueError('物体不存在: ' + n.object_name)
            if obj.type == 'MESH' and obj not in result:
                result.append(obj)
        elif n.bl_idname in (NODE_ID, 'SSMTNode_Object_Group', 'SSMTNode_ObjectSwap', 'NodeReroute'):
            for socket in n.inputs:
                for link in socket.links:
                    walk(link.from_node)
        else:
            raise ValueError('此处理节点尚未适配 Velo 导出: ' + n.bl_idname)
        visiting.remove(key)
        seen.add(key)
    walk(node)
    return result

def _swap_nodes(tree, output_name):
    return [n for n in tree.nodes if n.bl_idname == 'SSMTNode_ObjectSwap' and not n.mute]

def _swap_bindings(tree):
    result = {}
    used = set()
    try:
        from TheHerta4.blueprint.variable_registry import get_node_variable_name
    except Exception:
        get_node_variable_name = None
    for index, node in enumerate(_swap_nodes(tree, '')):
        if get_node_variable_name is not None:
            # Share the exact variable identity used by every TheHerta4 post-process node.
            result[node.as_pointer()] = get_node_variable_name(node).lstrip('$')
            used.add(result[node.as_pointer()].casefold())
            continue
        suffix = re.sub(r'[^A-Za-z0-9_]+', '_', node.name).strip('_')[:40]
        base = f'TH4_SWAP_{suffix}' if suffix else f'TH4_SWAP_{index}'
        name = base
        duplicate = 1
        while name.casefold() in used:
            name = f'{base}_{duplicate}'
            duplicate += 1
        used.add(name.casefold())
        result[node.as_pointer()] = name
    return result

def _object_condition_paths(node, bindings):
    paths = {}
    counters = {}
    def walk(n, conditions):
        if n.bl_idname == 'SSMTNode_Object_Info':
            if n.object_name:
                paths.setdefault(n.object_name, []).append(list(conditions))
            return
        if n.bl_idname in ('SSMTNode_Object_Group', 'NodeReroute', NODE_ID):
            for sock in n.inputs:
                for link in sock.links: walk(link.from_node, conditions)
            return
        if n.bl_idname == 'SSMTNode_ObjectSwap':
            var = bindings[n.as_pointer()]
            for option, sock in enumerate(n.inputs):
                for link in sock.links: walk(link.from_node, conditions + [(var, option)])
    walk(node, [])
    return paths

def _rewrite_nested_toggle_conditions(cfg, tree, bridge_node, bindings):
    _debug('rewrite_enter version=' + BRIDGE_VERSION)
    folder = getattr(cfg, 'mod_output_folder', '')
    if not folder:
        return
    ini_path = Path(bpy.path.abspath(folder)) / 'mod.ini'
    _debug('ini_path=' + str(ini_path))
    if not ini_path.is_file():
        return
    from velo_tools.games.wuthering_waves._wwmi_core.blender_export.text_formatter import TextFormatter
    fmt = TextFormatter()
    paths = _object_condition_paths(bridge_node, bindings)
    _debug('paths=' + repr(paths))
    # Velo may split one source object into several material fragments.
    # Bind conditions by Component N so every generated draw variable is covered.
    component_paths = {}
    component_names = {}
    component_pattern = re.compile(r'.*component[_ -]*(\d+).*', re.IGNORECASE)
    for object_name, alternatives in paths.items():
        match = component_pattern.match(object_name)
        if match:
            component_paths.setdefault(int(match.group(1)), []).extend(alternatives)
            component_names.setdefault(int(match.group(1)), object_name)
    lines = ini_path.read_text(encoding='utf-8').splitlines()
    draw_vars = {}
    for line in lines:
        m = re.match(r'\s*(?:global\s+)?(\$draw_component_(\d+)_\S+)\s*=\s*', line, re.IGNORECASE)
        if m:
            draw_vars.setdefault(int(m.group(2)), []).append(m.group(1))
    # Canonicalize every generated component variable to the TheHerta object name.
    # This removes material-derived names from the bridge contract entirely.
    rename_map = {}
    for component_id, names in draw_vars.items():
        canonical = component_names.get(component_id)
        if not canonical:
            continue
        target = fmt.format_ini_drawvar(canonical)
        for source in names:
            if source.casefold() != target.casefold():
                rename_map[source] = target
    if rename_map:
        _debug('rename_map=' + repr(rename_map))
        for i, line in enumerate(lines):
            for source, target in rename_map.items():
                lines[i] = re.sub(r'(?<![A-Za-z0-9_])' + re.escape(source) + r'(?![A-Za-z0-9_])', target, lines[i], flags=re.IGNORECASE)
        draw_vars = {cid: [rename_map.get(v, v) for v in vals] for cid, vals in draw_vars.items()}
    rewritten = set()
    for component_id, alternatives in component_paths.items():
        expressions = []
        for path in alternatives:
            if path:
                expressions.append(' && '.join(f'{fmt.format_ini_swapvar(v)} == {state}' for v, state in path))
        if not expressions:
            continue
        expression = ' || '.join(f'({x})' for x in expressions)
        for lhs in draw_vars.get(component_id, []):
            for i, line in enumerate(lines):
                if line.strip().startswith(lhs + ' ='):
                    lines[i] = f'{lhs} = {expression}'
                    rewritten.add(lhs.casefold())
    # Remove stale bridge assignments that target names absent from draw blocks.
    process_start = next((i for i, line in enumerate(lines) if line.strip() == '[CommandListProcessToggles]'), None)
    if process_start is not None:
        process_end = next((i for i in range(process_start + 1, len(lines)) if lines[i].startswith('[')), len(lines))
        valid = {v.casefold() for vals in draw_vars.values() for v in vals}
        lines = [line for i, line in enumerate(lines)
                 if not (process_start < i < process_end and re.match(r'\s*\$draw_\S+\s*=', line)
                         and re.match(r'\s*(\$draw_\S+)\s*=', line).group(1).casefold() not in valid)]
    ini_path.write_text('\n'.join(lines) + '\n', encoding='utf-8')
    _debug('rewrite_done')

def _inject_swap_toggles(cfg, tree):
    toggles = getattr(cfg, 'ini_toggles', None)
    if toggles is None or not hasattr(cfg, 'use_ini_toggles'):
        return None
    snapshot = toggles.export_vars()
    old_enabled = bool(cfg.use_ini_toggles)
    # The bridge export is a self-contained transaction.  Do not enable and
    # validate dormant/incomplete Velo toggle rows owned by the user.
    try:
        toggles.vars.clear()
        bindings = _swap_bindings(tree)
        for idx, node in enumerate(_swap_nodes(tree, '')):
            name = bindings[node.as_pointer()]
            var = toggles.vars.add()
            var.name = name
            var.hotkeys = getattr(node, 'hotkey', '') or ''
            var.default_state = '0'
            for state_index, socket in enumerate(node.inputs):
                state = var.states.add()
                state.name = str(state_index)
                # An unlinked ObjectSwap input deliberately remains an empty state.
                # Velo then emits no object bindings for that state, which matches
                # TheHerta4's `skip` semantics: all objects from other options hide.
                for link in socket.links:
                    upstream = link.from_node
                    try:
                        candidates = linked_objects(upstream)
                    except Exception:
                        candidates = []
                    for obj in candidates:
                        item = state.objects.add()
                        item.object = obj
                        item.add_default_condition(var.name, state.name)
            if not len(var.states):
                toggles.vars.remove(toggles.vars.find(var.name))
        cfg.use_ini_toggles = True
    except Exception:
        # 注入失败必须还原用户原有的切换配置，避免失败后遗留被清空的 toggle 行。
        _restore_swap_toggles(cfg, (snapshot, old_enabled))
        raise
    return snapshot, old_enabled, bindings

def _restore_swap_toggles(cfg, snapshot_state):
    if not snapshot_state:
        return
    snapshot, old_enabled, _bindings = snapshot_state
    cfg.ini_toggles.vars.clear()
    cfg.ini_toggles.import_vars(snapshot, replace_vars=True, clear_vars=False)
    cfg.use_ini_toggles = old_enabled

def _seed_postprocess_detection_from_ini(tree, ini_path):
    """For Velo imports, seed character detection from the exported component hash."""
    try:
        text = Path(ini_path).read_text(encoding='utf-8')
        for node in tree.nodes:
            if node.bl_idname != 'SSMTNode_PostProcess_SwapPanel' or node.mute:
                continue
            target = str(getattr(node, 'target_object', '') or '')
            comp = re.search(r'component[_ -]*(\d+)', target, re.IGNORECASE)
            component_id = comp.group(1) if comp else '0'
            block = re.search(r'\[TextureOverrideComponent' + re.escape(component_id) + r'\][\s\S]*?(?=\n\[|\Z)', text, re.MULTILINE)
            match = re.search(r'^hash\s*=\s*([0-9a-fA-F]{8})[\s\S]*?^match_index_count\s*=\s*(\d+)', block.group(0), re.MULTILINE) if block else None
            if not match:
                continue
            if not str(getattr(node, 'detect_hash', '') or '').strip():
                node.detect_hash = match.group(1).lower()
            if not str(getattr(node, 'detect_index_count', '') or '').strip():
                node.detect_index_count = match.group(2)
    except Exception:
        pass

def _restore_th4_swap_variable_names(ini_path):
    path = Path(ini_path)
    text = path.read_text(encoding='utf-8')
    text = re.sub(r'(?<![A-Za-z0-9_])\$swapvar_(swapkey\d+)(?![A-Za-z0-9_])', r'$\1', text, flags=re.IGNORECASE)
    path.write_text(text, encoding='utf-8')

class SSMTNode_VeloExportBridge(bpy.types.Node):
    bl_idname = NODE_ID
    bl_label = 'Velo Mod（实验性）'
    bl_description = '实验性桥接：使用 Velo Tools 当前工作空间导出，并运行已连接的 TheHerta4 后处理节点。'
    bl_icon = 'EXPORT'
    @classmethod
    def poll(cls, tree):
        return tree.bl_idname == 'SSMTBlueprintTreeType'
    def init(self, context):
        self.inputs.new('SSMTSocketObject', '物体')
        # Match TheHerta4 result output: downstream post-process nodes connect here.
        self.outputs.new('SSMTSocketPostProcess', 'Post Process')
        self.width = 240
    def draw_buttons(self, context, layout):
        if not VELO_TOOLS_AVAILABLE:
            layout.label(text='未安装前置插件 Velo Tools，无法导出', icon='ERROR')
        op = layout.operator('ssmt.velo_bridge_execute', text='导出mod', icon='EXPORT')
        op.tree_name = self.id_data.name
        op.node_name = self.name

class ImportVeloWorkspace(bpy.types.Operator):
    bl_idname = 'ssmt.import_current_velo_workspace'
    bl_label = '导入当前velo工作空间'
    bl_options = {'REGISTER', 'UNDO'}
    def execute(self, context):
        if not _require_velo_tools(self):
            return {'CANCELLED'}
        if context.scene.global_properties.workspace_source_mode != 'VELO':
            self.report({'WARNING'}, '请先选择velo工作空间')
            return {'CANCELLED'}
        desc, cfg = workspace(context.scene)
        coll = getattr(cfg, 'component_collection', None)
        if coll is None or not coll.all_objects or coll not in list(context.scene.collection.children_recursive):
            self.report({'WARNING'}, '没有检测到工作空间')
            return {'CANCELLED'}
        tree = bpy.data.node_groups.new(coll.name, 'SSMTBlueprintTreeType')
        try:
            _debug('export_enter version=' + BRIDGE_VERSION)
            tree.use_fake_user = True
            tree['velo_game'] = desc.game_value
            tree['velo_collection'] = coll.name
            group = tree.nodes.new('SSMTNode_Object_Group')
            group.label = coll.name
            group.location = (0, 0)
            output = tree.nodes.new(NODE_ID)
            output.location = (340, 0)
            tree.links.new(group.outputs[0], output.inputs[0])
            for i, obj in enumerate(coll.all_objects):
                node = tree.nodes.new('SSMTNode_Object_Info')
                node.object_name = obj.name
                node.location = (-450, -i * 220)
                socket = next((s for s in group.inputs if not s.is_linked), None)
                if socket is None:
                    socket = group.inputs.new('SSMTSocketObject', '物体')
                tree.links.new(node.outputs[0], socket)
            context.scene['theherta_velo_blueprint'] = tree.name
            coll['theherta_velo_blueprint'] = tree.name
            context.scene.global_properties.selected_blueprint_name = tree.name
            for window in context.window_manager.windows:
                for area in window.screen.areas:
                    if area.type == 'NODE_EDITOR' and area.spaces.active.tree_type == 'SSMTBlueprintTreeType':
                        area.spaces.active.node_tree = tree
                        area.tag_redraw()
        except Exception:
            bpy.data.node_groups.remove(tree)
            raise
        _debug('workspace_import collection=' + coll.name + ' objects=' + str(len(coll.all_objects)))
        self.report({'INFO'}, '已导入 Velo 工作空间（实验性）: ' + coll.name)
        return {'FINISHED'}

class ExportVeloWorkspace(bpy.types.Operator):
    bl_idname = 'ssmt.velo_bridge_execute'
    bl_label = '导出mod'
    tree_name: StringProperty()
    node_name: StringProperty()
    def execute(self, context):
        if not _require_velo_tools(self):
            return {'CANCELLED'}
        tmp = None
        cfg = None
        original = None
        toggle_state = None
        original_auto_split = None
        try:
            tree = bpy.data.node_groups[self.tree_name]
            objects = linked_objects(tree.nodes[self.node_name])
            _debug('export_start tree=' + self.tree_name + ' node=' + self.node_name + ' objects=' + str(len(objects)))
            if not objects:
                raise ValueError('没有连接到 Velo Mod 的网格物体')
            desc, cfg = workspace(context.scene)
            if cfg is None:
                raise ValueError('没有检测到工作空间')
            if tree.get('velo_game', desc.game_value) != desc.game_value:
                raise ValueError('请在 Velo 中切换回该蓝图的游戏')
            original = cfg.component_collection
            original_auto_split = getattr(cfg, 'velo_auto_split_by_material', None)
            if original_auto_split is not None:
                cfg.velo_auto_split_by_material = False
            tmp = bpy.data.collections.new('VeloBridge_Export')
            context.scene.collection.children.link(tmp)
            selected = set(objects)
            # Preserve Velo's collection metadata, but route each selected object by
            # its effective `Component N` name prefix, matching Velo ObjectMerger.
            component_pattern = re.compile(r'.*component[_ -]*(\d+).*', re.IGNORECASE)
            component_targets = {}
            def clone_collection(src, dst):
                for key, value in src.items():
                    dst[key] = value
                for child in src.children:
                    child_dst = bpy.data.collections.new(child.name)
                    dst.children.link(child_dst)
                    component_id = child.get('velo_component_id', None)
                    if component_id is not None:
                        component_targets[int(component_id)] = child_dst
                    clone_collection(child, child_dst)
            clone_collection(cfg.component_collection, tmp)
            for obj in selected:
                match = component_pattern.match(obj.name)
                if not match:
                    continue
                component_id = int(match.group(1))
                target = component_targets.get(component_id)
                if target is None:
                    raise ValueError(f'对象 {obj.name} 的 Component {component_id} 不在 Velo 元数据集合中')
                target.objects.link(obj)
            if not list(tmp.all_objects):
                raise ValueError('蓝图连接的对象不在 Velo 工作空间集合中')
            cfg.component_collection = tmp
            toggle_state = _inject_swap_toggles(cfg, tree)
            _debug('swap_toggles_injected=' + str(bool(toggle_state)))
            category, name = desc.export_op.split('.')
            result = getattr(getattr(bpy.ops, category), name)('EXEC_DEFAULT')
            _debug('velo_export_result=' + repr(result) + ' output=' + str(cfg.mod_output_folder))
            if 'FINISHED' not in result or getattr(cfg, 'last_error_text', ''):
                raise ValueError(getattr(cfg, 'last_error_text', '') or 'Velo 导出未完成')
            _rewrite_nested_toggle_conditions(cfg, tree, tree.nodes[self.node_name], toggle_state[2] if toggle_state else {})
            _seed_postprocess_detection_from_ini(tree, bpy.path.abspath(cfg.mod_output_folder) + '/mod.ini')
            _restore_th4_swap_variable_names(bpy.path.abspath(cfg.mod_output_folder) + '/mod.ini')
            # Run TheHerta4's connected post-process chain against the final Velo INI.
            try:
                from TheHerta4.blueprint.export_helper import BlueprintExportHelper
                BlueprintExportHelper.set_runtime_blueprint_tree(tree)
                BlueprintExportHelper.runtime_result_output_node_type = NODE_ID
                post_nodes = []
                visited = set()
                def collect_post(node):
                    if node.as_pointer() in visited:
                        return
                    visited.add(node.as_pointer())
                    if node.bl_idname.startswith('SSMTNode_PostProcess_') and not node.mute:
                        post_nodes.append(node)
                    for output in node.outputs:
                        for link in output.links:
                            collect_post(link.to_node)
                collect_post(tree.nodes[self.node_name])
                _debug('post_nodes=' + repr([(n.name, n.bl_idname) for n in post_nodes]))
                for post_node in post_nodes:
                    fn = getattr(post_node, 'execute_postprocess', None)
                    if callable(fn):
                        _debug('post_execute=' + post_node.bl_idname)
                        fn(bpy.path.abspath(cfg.mod_output_folder))
            except Exception as post_exc:
                raise ValueError('后处理节点执行失败: ' + str(post_exc))
        except Exception as exc:
            _debug('export_failed=' + repr(exc))
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        finally:
            _restore_swap_toggles(cfg, toggle_state)
            if cfg is not None and original_auto_split is not None:
                cfg.velo_auto_split_by_material = original_auto_split
            if tmp is not None:
                cfg.component_collection = original
                bpy.data.collections.remove(tmp)
        return {'FINISHED'}

CLASSES = (SSMTNode_VeloExportBridge, ImportVeloWorkspace, ExportVeloWorkspace)
def register():
    for cls in CLASSES:
        if not cls.is_registered:
            bpy.utils.register_class(cls)
    # Existing node instances do not run init() again after an addon reload.
    # Migrate them so the post-process output is visible immediately.
    try:
        for tree in bpy.data.node_groups:
            if tree.bl_idname != 'SSMTBlueprintTreeType':
                continue
            for node in tree.nodes:
                if node.bl_idname == NODE_ID and not any(s.bl_idname == 'SSMTSocketPostProcess' for s in node.outputs):
                    node.outputs.new('SSMTSocketPostProcess', 'Post Process')
    except Exception as exc:
        # Registration must remain non-fatal if TheHerta4 socket classes load later.
        _debug('node migration deferred: ' + repr(exc))
def unregister():
    for cls in reversed(CLASSES):
        if cls.is_registered:
            bpy.utils.unregister_class(cls)
