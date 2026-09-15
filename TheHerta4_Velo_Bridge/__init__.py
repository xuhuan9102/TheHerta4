bl_info = {'name': 'TheHerta4 Velo Bridge', 'version': (0, 3, 2), 'blender': (4, 4, 0), 'category': 'Node'}
import bpy
import importlib.util
import re
import sys
import hashlib
from contextlib import nullcontext
from pathlib import Path
from bpy.props import StringProperty

NODE_ID = 'SSMTNode_VeloExportBridge'
BRIDGE_VERSION = '0.3.2'
LOG_PREFIX = '[TheHerta4][VeloBridge][Experimental] '
VELO_FORMATTER_MODULES = {
    'WUTHERING': 'velo_tools.games.wuthering_waves._wwmi_core.blender_export.text_formatter',
    'ENDFIELD': 'velo_tools.games.arknights_endfield._efmi_core.blender_export.text_formatter',
}

# 前置插件检测：未安装 Velo Tools 时节点仍可创建（便于搭建蓝图），
# 但导入/导出操作在执行时会直接失败并给出明确提示（参考 NTMI 的依赖检测模式）。
def velo_tools_available():
    """动态检测 Velo Tools 前置插件。

    插件启用顺序不受控：TheHerta4 先于 Velo Tools 载入时，导入期一次性判定的
    结果会永久停留在 False，导入/导出被误判为「未安装前置插件」且重启前无法恢复。
    find_spec 只做模块定位、不触发导入，因此每次使用前重新探测。
    """
    try:
        return importlib.util.find_spec('velo_tools') is not None
    except Exception:
        return False


# 兼容既有引用：模块导入时的快照，register() 与每次操作前都会刷新。
VELO_TOOLS_AVAILABLE = velo_tools_available()


def _require_velo_tools(self):
    """执行导入/导出前检查 Velo Tools 前置插件；未安装时直接终止操作。"""
    global VELO_TOOLS_AVAILABLE
    VELO_TOOLS_AVAILABLE = velo_tools_available()
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


def _velo_text_formatter(game_value):
    module_name = VELO_FORMATTER_MODULES.get(game_value)
    if module_name is None:
        raise ValueError('TheHerta4 Velo Bridge 不支持此游戏: ' + str(game_value))
    return importlib.import_module(module_name).TextFormatter()


# ── 主包名解析（吸收自 TheHerta4Test_20260915 的 2026-09-13 修复）──────────
#
# 这里原先写死了两处 `from TheHerta4.blueprint... import ...`。
# 主包目录名**是可以被改的** —— 交付方的测试构建就叫 `TheHerta4Test`
# （需要能和用户已安装的正式版共存）。
# 写死之后那两处会抛 ModuleNotFoundError，后果**两处还不一样**：
#   * `_swap_bindings` 里被 `except Exception` 吞掉 ⇒ **静默降级**：
#     变量名不再与主插件后处理节点共享同一身份，注释里那句
#     "Share the exact variable identity" 的目标就落空了；
#   * 导出路径里被包成 `raise ValueError('后处理节点执行失败: …')`
#     ⇒ 用户看到的是一句和真正原因无关的报错，**导出直接取消**。
#
# 上游 `__init__.py` 的 `_import_velo_bridge()` 已经承认桥有两种安装布局，
# 这里沿用同一个前提，但解析的是"**主包叫什么名字**"：

def _host_package_name():
    """本桥所属的**主插件**包名（不假设它叫 TheHerta4）。

    布局 A：作为主包的子包
        `__package__ == '<主包>.TheHerta4_Velo_Bridge'` ⇒ 父级就是主包名。
    布局 B：被单独复制进 addons 目录
        `__package__ == 'TheHerta4_Velo_Bridge'` ⇒ 推不出主包名，
        改从**已经加载进 `sys.modules` 的模块**里找主包的内部模块。
        （桥真正执行导入时，主包必然已经加载完毕。）
    两者都不成立时退回规范名 `TheHerta4`。
    """
    package = globals().get('__package__') or ''
    if '.' in package:
        return package.rsplit('.', 1)[0]
    for suffix in ('.blueprint.export_helper', '.blueprint.variable_registry'):
        for name in list(sys.modules):
            if name.endswith(suffix):
                return name[: -len(suffix)]
    return 'TheHerta4'


def _host_import(relative):
    """从主包导入一个模块。`relative` 以点开头，例如 `.blueprint.export_helper`。"""
    return importlib.import_module(_host_package_name() + relative)


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
    reachable = set()
    def walk(node):
        key = node.as_pointer()
        if key in reachable:
            return
        reachable.add(key)
        for socket in node.inputs:
            for link in socket.links:
                walk(link.from_node)
    if output_name:
        walk(tree.nodes[output_name])
    return [n for n in tree.nodes if n.bl_idname == 'SSMTNode_ObjectSwap'
            and not n.mute and (not output_name or n.as_pointer() in reachable)]

def _swap_bindings(tree, output_name=''):
    result = {}
    used = set()
    try:
        # 主包名不写死：见 _host_package_name() 的说明。
        get_node_variable_name = _host_import('.blueprint.variable_registry').get_node_variable_name
    except Exception:
        get_node_variable_name = None
    for index, node in enumerate(_swap_nodes(tree, output_name)):
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

def _rewrite_nested_toggle_conditions(cfg, tree, bridge_node, bindings, game_value='WUTHERING'):
    folder = getattr(cfg, 'mod_output_folder', '')
    if not folder:
        return
    ini_path = Path(bpy.path.abspath(folder)) / 'mod.ini'
    if not ini_path.is_file():
        return
    fmt = _velo_text_formatter(game_value)
    paths = _object_condition_paths(bridge_node, bindings)
    lines = ini_path.read_text(encoding='utf-8').splitlines()
    declared = {
        match.group(1).casefold()
        for line in lines
        if (match := re.match(r'\s*global\s+(\$draw_\S+)\s*=', line, re.IGNORECASE))
    }
    assignments = {}
    owners = {}
    for name, alternatives in paths.items():
        variable = fmt.format_ini_drawvar(name)
        key = variable.casefold()
        if key in owners and owners[key] != name:
            raise ValueError('Blueprint object names produce the same INI variable: ' + name)
        owners[key] = name
        if key not in declared:
            continue
        # An unconditional connection takes precedence over conditional paths.
        expressions = [
            '(' + ' && '.join(f'{fmt.format_ini_swapvar(v)} == {state}' for v, state in path) + ')'
            for path in alternatives if path
        ]
        assignments[key] = (variable, '1' if any(not path for path in alternatives)
                            else ' || '.join(dict.fromkeys(expressions)) or '1')
    start = next((i for i, line in enumerate(lines)
                  if line.strip().casefold() == '[commandlistprocesstoggles]'), None)
    if start is not None:
        end = next((i for i in range(start + 1, len(lines))
                    if lines[i].lstrip().startswith('[')), len(lines))
        body = [line for line in lines[start + 1:end]
                if not re.match(r'\s*\$draw_\S+\s*=', line, re.IGNORECASE)]
        body.extend(f'{variable} = {expression}' for variable, expression in assignments.values())
        lines[start + 1:end] = body
    elif assignments:
        lines.extend(['', '[CommandListProcessToggles]'])
        lines.extend(f'{variable} = {expression}' for variable, expression in assignments.values())
    ini_path.write_text(_normalize_draw_variables('\n'.join(lines) + '\n'), encoding='utf-8')


def _normalize_draw_variables(text):
    """Keep Blender labels in comments; 3Dmigoto identifiers must be ASCII."""
    names = re.findall(r'^\s*global\s+(\$draw_\S+)\s*=', text, re.MULTILINE | re.IGNORECASE)
    occupied = {name.casefold() for name in names}
    replacements = {}
    for name in names:
        if re.fullmatch(r'\$[a-z_][a-z_0-9]*', name, re.IGNORECASE):
            continue
        if name in replacements:
            continue
        digest = hashlib.sha256(name.casefold().encode('utf-8')).hexdigest()
        target = '$draw_th4_' + digest
        if target in occupied:
            raise ValueError('Generated draw variable conflicts with an existing variable: ' + name)
        replacements[name] = target
        occupied.add(target)
    if not replacements:
        return text
    pattern = re.compile('(?:' + '|'.join(re.escape(name) for name in sorted(replacements, key=len, reverse=True))
                         + r')(?![\w])', re.IGNORECASE)
    lookup = {name.casefold(): target for name, target in replacements.items()}
    return pattern.sub(lambda match: lookup[match.group(0).casefold()], text)


def _inject_swap_toggles(cfg, tree, output_name='', game_value='WUTHERING'):
    toggles = getattr(cfg, 'ini_toggles', None)
    if toggles is None or not hasattr(cfg, 'use_ini_toggles'):
        return None
    snapshot = toggles.export_vars()
    old_enabled = bool(cfg.use_ini_toggles)
    # The bridge export is a self-contained transaction.  Do not enable and
    # validate dormant/incomplete Velo toggle rows owned by the user.
    try:
        toggles.vars.clear()
        bindings = _swap_bindings(tree, output_name)
        _swap_variable_replacements(bindings, game_value)
        for idx, node in enumerate(_swap_nodes(tree, output_name)):
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
        _restore_swap_toggles(cfg, (snapshot, old_enabled, {}))
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

def _seed_detection_for_export(tree, cfg):
    """导出后为切换面板回填物体检测值。

    不按游戏分支：能否回填完全由 ini 决定——只有 `[TextureOverrideComponent<id>]`
    形状的段（hash + match_index_count）才能解析出检测值，EFMI 的
    `[TextureOverride_Component<id>_<hash>]` 段名匹配不到，自然跳过。
    因此 WWMI 与 EFMI 走同一条路径，不需要在调用点判断 desc.game_value。
    """
    folder = getattr(cfg, 'mod_output_folder', '')
    if not folder:
        return
    _seed_postprocess_detection_from_ini(tree, bpy.path.abspath(folder) + '/mod.ini')

def _swap_variable_replacements(bindings, game_value):
    formatter = _velo_text_formatter(game_value)
    replacements = {}
    owners = {}
    for name in bindings.values():
        target = '$' + str(name).lstrip('$')
        source = formatter.format_ini_swapvar(name)
        key = source.casefold()
        previous = owners.get(key)
        if previous is not None and previous.casefold() != target.casefold():
            raise ValueError(
                f"物体切换变量 {previous} 和 {target} 会被 Velo 格式化为同一个变量 {source}，"
                "请在物体切换节点中使用不同的变量名"
            )
        owners[key] = target
        replacements[key] = target
    return replacements


def _restore_th4_swap_variable_names(ini_path, bindings, game_value):
    """Replace Velo-generated identifiers with the exact ObjectSwap node variables."""
    path = Path(ini_path)
    text = path.read_text(encoding='utf-8')
    replacements = _swap_variable_replacements(bindings, game_value)
    if replacements:
        pattern = re.compile(
            r'(?<![A-Za-z0-9_])(?:'
            + '|'.join(re.escape(name) for name in sorted(replacements, key=len, reverse=True))
            + r')(?![A-Za-z0-9_])',
            re.IGNORECASE,
        )
        text = pattern.sub(lambda match: replacements[match.group(0).casefold()], text)
    path.write_text(text, encoding='utf-8')

class SSMTNode_VeloExportBridge(bpy.types.Node):
    bl_idname = NODE_ID
    bl_label = 'Velo Mod（实验性）'
    bl_description = '实验性桥接：使用 Velo Tools 当前工作空间导出，并运行已连接的 TheHerta4 后处理节点。'
    bl_icon = 'EXPORT'
    # 桥接节点自己的后端标记：后处理节点据此判断「本次导出是不是 Velo 桥接驱动的」，
    # 不再依赖 ini 文本特征（TheHerta4 自家的 EFMI 导出同样会写 \EFMIv1\ 命名空间）。
    velo_game: StringProperty(name='Velo 游戏', default='')
    @classmethod
    def poll(cls, tree):
        return tree.bl_idname == 'SSMTBlueprintTreeType'
    def init(self, context):
        self.inputs.new('SSMTSocketObject', '物体')
        # Match TheHerta4 result output: downstream post-process nodes connect here.
        self.outputs.new('SSMTSocketPostProcess', 'Post Process')
        self.width = 240
    def draw_buttons(self, context, layout):
        if not velo_tools_available():
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
            output.velo_game = desc.game_value
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
        original_filters = {}
        temporary_collections = []
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
            # 桥接节点与蓝图树都显式记住自己的后端，供下游后处理节点判断。
            tree.nodes[self.node_name].velo_game = desc.game_value
            tree['velo_game'] = desc.game_value
            original = cfg.component_collection
            original_auto_split = getattr(cfg, 'velo_auto_split_by_material', None)
            if original_auto_split is not None:
                cfg.velo_auto_split_by_material = False
            tmp = bpy.data.collections.new('VeloBridge_Export')
            temporary_collections.append(tmp)
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
                    temporary_collections.append(child_dst)
                    dst.children.link(child_dst)
                    component_id = child.get('velo_component_id', None)
                    if component_id is not None:
                        component_targets[int(component_id)] = child_dst
                    clone_collection(child, child_dst)
            clone_collection(cfg.component_collection, tmp)
            for obj in selected:
                match = component_pattern.match(obj.name)
                if not match:
                    raise ValueError('Blueprint mesh has no Component N name: ' + obj.name)
                component_id = int(match.group(1))
                target = component_targets.get(component_id)
                if target is None:
                    raise ValueError(f'对象 {obj.name} 的 Component {component_id} 不在 Velo 元数据集合中')
                target.objects.link(obj)
            if not list(tmp.all_objects):
                raise ValueError('蓝图连接的对象不在 Velo 工作空间集合中')
            cfg.component_collection = tmp
            if desc.game_value == 'ENDFIELD':
                for key in ('ignore_hidden_objects', 'ignore_hidden_collections', 'ignore_nested_collections'):
                    original_filters[key] = getattr(cfg, key)
                    setattr(cfg, key, False)
            toggle_state = _inject_swap_toggles(cfg, tree, self.node_name, desc.game_value)
            _debug('swap_toggles_injected=' + str(bool(toggle_state)))
            category, name = desc.export_op.split('.')
            from .efmi_selection import explicit_efmi_objects
            selection_scope = explicit_efmi_objects(tmp, objects) if desc.game_value == 'ENDFIELD' else nullcontext()
            with selection_scope:
                result = getattr(getattr(bpy.ops, category), name)('EXEC_DEFAULT')
            _debug('velo_export_result=' + repr(result) + ' output=' + str(cfg.mod_output_folder))
            if 'FINISHED' not in result or getattr(cfg, 'last_error_text', ''):
                raise ValueError(getattr(cfg, 'last_error_text', '') or 'Velo 导出未完成')
            _rewrite_nested_toggle_conditions(
                cfg,
                tree,
                tree.nodes[self.node_name],
                toggle_state[2] if toggle_state else {},
                desc.game_value,
            )
            _seed_detection_for_export(tree, cfg)
            _restore_th4_swap_variable_names(
                bpy.path.abspath(cfg.mod_output_folder) + '/mod.ini',
                toggle_state[2] if toggle_state else {},
                desc.game_value,
            )
            # Run TheHerta4's connected post-process chain against the final Velo INI.
            try:
                # 主包名不写死：见 _host_package_name() 的说明。
                BlueprintExportHelper = _host_import('.blueprint.export_helper').BlueprintExportHelper
                previous_runtime_tree = BlueprintExportHelper.runtime_blueprint_tree_name
                previous_result_node_type = BlueprintExportHelper.runtime_result_output_node_type
                previous_velo_game = BlueprintExportHelper.set_runtime_velo_bridge_game(desc.game_value)
                BlueprintExportHelper.set_runtime_blueprint_tree(tree)
                BlueprintExportHelper.set_runtime_result_output_node_type(NODE_ID)
                try:
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
                finally:
                    # 运行时标记只在本次后处理过程内有效，避免泄漏给后续的原生导出。
                    BlueprintExportHelper.runtime_blueprint_tree_name = previous_runtime_tree
                    BlueprintExportHelper.set_runtime_result_output_node_type(previous_result_node_type)
                    BlueprintExportHelper.set_runtime_velo_bridge_game(previous_velo_game)
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
            for key, value in original_filters.items():
                setattr(cfg, key, value)
            if tmp is not None:
                cfg.component_collection = original
                for collection in reversed(temporary_collections):
                    bpy.data.collections.remove(collection)
        return {'FINISHED'}

CLASSES = (SSMTNode_VeloExportBridge, ImportVeloWorkspace, ExportVeloWorkspace)


def _bpy_data_ready():
    """启动阶段的受限上下文里 bpy.data 不可访问，此时迁移必须延后。"""
    try:
        bpy.data.node_groups
    except Exception:
        return False
    return True


def _migrate_existing_bridge_nodes():
    """既有 Velo Mod 节点不会重跑 init()，这里补上后处理输出（幂等）。"""
    if not _bpy_data_ready():
        _schedule_bridge_node_migration()
        return
    try:
        for tree in bpy.data.node_groups:
            if tree.bl_idname != 'SSMTBlueprintTreeType':
                continue
            for node in tree.nodes:
                if node.bl_idname == NODE_ID and not any(s.bl_idname == 'SSMTSocketPostProcess' for s in node.outputs):
                    node.outputs.new('SSMTSocketPostProcess', 'Post Process')
    except Exception as exc:
        # 注册流程必须保持非致命：socket 类尚未就绪时只记录，不向 Blender 抛错。
        _debug('node_migration_failed: ' + repr(exc))


def _schedule_bridge_node_migration():
    """启动阶段无法访问 bpy.data 时，等一次事件循环后再补做迁移。"""
    def _timer():
        _migrate_existing_bridge_nodes()
        return None

    try:
        bpy.app.timers.register(_timer, first_interval=0.5, persistent=False)
    except Exception:
        pass


def register():
    global VELO_TOOLS_AVAILABLE
    VELO_TOOLS_AVAILABLE = velo_tools_available()
    for cls in CLASSES:
        if not cls.is_registered:
            bpy.utils.register_class(cls)
    _migrate_existing_bridge_nodes()


def unregister():
    for cls in reversed(CLASSES):
        if cls.is_registered:
            bpy.utils.unregister_class(cls)
