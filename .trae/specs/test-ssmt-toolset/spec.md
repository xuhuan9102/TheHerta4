# SSMT 插件工具集全功能测试 Spec

## Why
SSMT 插件包含 69 个操作符和 27 个节点类，需要通过 MCP Blender（端口 9876）系统性地测试所有工具功能，确保每个操作符在工具集模式下均可正常工作，并记录异常和错误供后续修复。

## What Changes
- 创建自动化测试脚本，通过 MCP Blender 端口 9876 连接并执行测试
- 按模块分类测试所有 SSMT 操作符功能
- 自动创建测试物体和节点以验证功能
- 生成测试结果记录 MD 文件，记录每个功能的测试状态
- 对异常功能标记错误原因

## Impact
- Affected specs: SSMT 插件所有操作符和节点功能
- Affected code: blueprint/ 目录下所有模块、ui/ 目录下所有模块

## ADDED Requirements

### Requirement: 自动化测试框架
系统 SHALL 提供通过 MCP Blender 端口 9876 连接并执行 Python 代码的测试框架，每次执行脚本后检查场景状态确认操作已生效。

### Requirement: 导入操作符测试
系统 SHALL 测试以下导入操作符：
- `ssmt4.import_all_from_workspace` — 从当前工作空间导入全部蓝图
- `ssmt4.import_raw` — 导入 SSMT 原始格式

#### Scenario: 导入工作空间
- **WHEN** 执行 `bpy.ops.ssmt4.import_all_from_workspace()`
- **THEN** 场景中应出现导入的物体

### Requirement: 蓝图节点创建测试
系统 SHALL 测试在 SSMTBlueprintTreeType 节点树中创建所有节点类型：
- SSMTNode_Object_Info — 对象信息节点
- SSMTNode_Object_Group — 对象组节点
- SSMTNode_Result_Output — 结果输出节点
- SSMTNode_ObjectSwap — 对象交换节点
- SSMTNode_Object_Rename — 对象重命名节点
- SSMTNode_Blueprint_Nest — 嵌套蓝图节点
- SSMTNode_ShapeKey — 形变键节点
- SSMTNode_ShapeKey_Output — 形变键输出节点
- SSMTNode_DataType — 数据类型节点
- SSMTNode_CrossIB — CrossIB 节点
- SSMTNode_VertexGroupMatch — 顶点组匹配节点
- SSMTNode_MultiFile_Export — 多文件导出节点
- SSMTNode_VertexGroupProcess — 顶点组处理节点
- SSMTNode_VertexGroupMappingInput — 顶点组映射输入节点
- SSMTNode_BonePalette_Export — 骨骼调色板导出节点
- SSMTNode_PostProcess_ShapeKey — 形变键后处理节点
- SSMTNode_PostProcess_SliderPanel — 滑动面板后处理节点
- SSMTNode_PostProcess_Material — 材质后处理节点
- SSMTNode_PostProcess_CrossIB — CrossIB 后处理节点
- SSMTNode_PostProcess_WebPanel — Web 面板后处理节点
- SSMTNode_PostProcess_MultiFile — 多文件后处理节点
- SSMTNode_PostProcess_ResourceMerge — 资源合并后处理节点
- SSMTNode_PostProcess_HealthDetection — 健康检测后处理节点
- SSMTNode_PostProcess_BufferCleanup — 缓冲清理后处理节点
- SSMTNode_PostProcess_VertexAttrs — 顶点属性后处理节点

#### Scenario: 创建节点
- **WHEN** 在 SSMTBlueprintTreeType 节点树中调用 `tree.nodes.new(node_type)`
- **THEN** 节点应被成功创建并出现在节点树中

### Requirement: 同步操作符测试
系统 SHALL 测试以下同步操作符：
- `ssmt.toggle_sync` — 切换同步
- `ssmt.sync_selected_node_to_object` — 同步选中节点到物体
- `ssmt.sync_selected_object_to_node` — 同步选中物体到节点
- `ssmt.update_all_node_references` — 更新所有节点引用
- `ssmt.select_object_from_node` — 从节点选择物体
- `ssmt.select_node_from_object` — 从物体选择节点
- `ssmt.sync_debug_status` — 同步调试状态

### Requirement: 节点菜单操作符测试
系统 SHALL 测试以下节点菜单操作符：
- `ssmt.create_group_from_selection` — 从选择创建组
- `ssmt.create_internal_switch` — 创建内部切换
- `ssmt.quick_add_rename_rule` — 快速添加重命名规则
- `ssmt.quick_add_vertex_group_match` — 快速添加顶点组匹配
- `ssmt.group_nodes_to_nested_blueprint` — 分组节点到嵌套蓝图
- `ssmt.ungroup_nested_blueprint` — 取消嵌套蓝图分组
- `ssmt.view_chain` — 查看处理链
- `ssmt.align_nodes` — 对齐节点
- `ssmt.batch_connect_nodes` — 批量连接节点

### Requirement: 对象节点操作符测试
系统 SHALL 测试以下对象节点操作符：
- `ssmt.refresh_node_object_ids` — 刷新节点物体 ID
- `ssmt.select_node_object` — 选择节点物体
- `ssmt.start_pick_object` — 开始拾取物体
- `ssmt.select_generate_mod_folder` — 选择生成 Mod 文件夹
- `ssmt.view_group_objects` — 查看组物体

### Requirement: 物体交换操作符测试
系统 SHALL 测试以下物体交换操作符：
- `ssmt.add_swap_option` — 添加交换选项
- `ssmt.remove_swap_option` — 移除交换选项

### Requirement: 重命名操作符测试
系统 SHALL 测试以下重命名操作符：
- `ssmt.add_rename_rule` — 添加重命名规则
- `ssmt.remove_rename_rule` — 移除重命名规则
- `ssmt.move_rename_rule_up` — 上移重命名规则
- `ssmt.move_rename_rule_down` — 下移重命名规则

### Requirement: 材质检测操作符测试
系统 SHALL 测试以下材质检测操作符：
- `ssmt.material_detect_add_prefix` — 材质检测添加前缀
- `ssmt.material_detect_remove_prefix` — 材质检测移除前缀
- `ssmt.material_detect_add_custom_prefix` — 材质检测添加自定义前缀
- `ssmt.material_detect` — 材质检测
- `ssmt.material_detect_clear` — 材质检测清除

### Requirement: CrossIB 操作符测试
系统 SHALL 测试以下 CrossIB 操作符：
- `ssmt.cross_ib_add_item` — CrossIB 添加项
- `ssmt.cross_ib_remove_item` — CrossIB 移除项

### Requirement: 顶点组匹配操作符测试
系统 SHALL 测试以下顶点组匹配操作符：
- `ssmt.vertex_group_match_execute` — 顶点组匹配执行
- `ssmt.vertex_group_match_clear` — 顶点组匹配清除
- `ssmt.vertex_group_match_apply_to_source` — 顶点组匹配应用到源
- `ssmt.vertex_group_match_toggle_debug` — 顶点组匹配切换调试
- `ssmt.vertex_group_match_sync` — 顶点组匹配同步
- `ssmt.vertex_group_match_delete_connection` — 顶点组匹配删除连接
- `ssmt.vertex_group_match_detect_multi` — 顶点组匹配多检测
- `ssmt.vertex_group_match_quick_weight` — 顶点组匹配快速权重

### Requirement: Shader 快捷连接操作符测试
系统 SHALL 测试以下 Shader 快捷连接操作符：
- `ssmt.shader_quick_transparent` — 快速透明着色器
- `ssmt.shader_quick_emission` — 快速自发光着色器
- `ssmt.shader_quick_diffuse` — 快速漫反射着色器
- `ssmt.shader_quick_principled` — 快速原理化着色器

### Requirement: 多文件导出操作符测试
系统 SHALL 测试以下多文件导出操作符：
- `ssmt.multi_file_export_split_animation` — 多文件导出拆分动画
- `ssmt.multi_file_export_remove_object` — 多文件导出移除物体
- `ssmt.multi_file_export_parse_collection` — 多文件导出解析集合
- `ssmt.multi_file_export_check_vertex_count` — 多文件导出检查顶点数
- `ssmt.multi_file_export_move_up` — 多文件导出上移
- `ssmt.multi_file_export_move_down` — 多文件导出下移

### Requirement: 顶点属性后处理操作符测试
系统 SHALL 测试以下顶点属性后处理操作符：
- `ssmt.post_process_add_vertex_attribute` — 添加顶点属性
- `ssmt.post_process_remove_vertex_attribute` — 移除顶点属性

### Requirement: Web 面板后处理操作符测试
系统 SHALL 测试以下 Web 面板后处理操作符：
- `ssmt.post_process_open_web_panel_builder` — 打开 Web 面板构建器

### Requirement: 嵌套蓝图导航操作符测试
系统 SHALL 测试以下嵌套蓝图导航操作符：
- `ssmt.blueprint_nest_navigate` — 嵌套蓝图导航
- `ssmt.create_blueprint_from_nest` — 从嵌套创建蓝图

### Requirement: 节点预设操作符测试
系统 SHALL 测试以下节点预设操作符：
- `ssmt.save_node_preset` — 保存节点预设
- `ssmt.load_node_preset` — 加载节点预设
- `ssmt.delete_node_preset` — 删除节点预设

### Requirement: 导出操作符测试
系统 SHALL 测试以下导出操作符：
- `ssmt.generate_mod_blueprint` — 生成 Mod 蓝图
- `ssmt.quick_export_selected` — 快速导出选中项

### Requirement: UI 面板操作符测试
系统 SHALL 测试以下 UI 面板操作符：
- `ssmt.clear_preprocess_cache` — 清除预处理缓存

### Requirement: 前缀快捷操作符测试
系统 SHALL 测试以下前缀快捷操作符：
- `ssmt.prefix_quick_apply` — 前缀快捷应用
- `ssmt.prefix_quick_refresh` — 前缀快捷刷新
- `ssmt.prefix_quick_clear` — 前缀快捷清除

### Requirement: 测试结果记录
系统 SHALL 生成 MD 格式的测试结果清单，包含：
- 每个功能的测试状态（通过/失败/无法测试）
- 失败功能的错误原因描述
- 测试执行时间戳
