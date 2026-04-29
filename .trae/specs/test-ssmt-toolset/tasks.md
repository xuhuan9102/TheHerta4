# Tasks

- [ ] Task 1: 创建测试框架基础设施
  - [ ] SubTask 1.1: 创建 MCP Blender 连接工具函数库（端口 9876）
  - [ ] SubTask 1.2: 创建测试结果记录 MD 文件模板
  - [ ] SubTask 1.3: 创建测试场景初始化脚本（清空场景、创建测试物体、创建蓝图节点树）

- [ ] Task 2: 测试蓝图节点创建功能（25 个节点类型）
  - [ ] SubTask 2.1: 测试基础节点（Object_Info, Object_Group, Result_Output）
  - [ ] SubTask 2.2: 测试交换/重命名/嵌套节点（ObjectSwap, Object_Rename, Blueprint_Nest）
  - [ ] SubTask 2.3: 测试形变键/数据类型节点（ShapeKey, ShapeKey_Output, DataType）
  - [ ] SubTask 2.4: 测试 CrossIB/顶点组节点（CrossIB, VertexGroupMatch, VertexGroupProcess, VertexGroupMappingInput）
  - [ ] SubTask 2.5: 测试导出/骨骼节点（MultiFile_Export, BonePalette_Export）
  - [ ] SubTask 2.6: 测试后处理节点（ShapeKey, SliderPanel, Material, CrossIB, WebPanel, MultiFile, ResourceMerge, HealthDetection, BufferCleanup, VertexAttrs）

- [ ] Task 3: 测试同步操作符（7 个）
  - [ ] SubTask 3.1: 测试 toggle_sync, sync_debug_status
  - [ ] SubTask 3.2: 测试 sync_selected_node_to_object, sync_selected_object_to_node
  - [ ] SubTask 3.3: 测试 update_all_node_references, select_object_from_node, select_node_from_object

- [ ] Task 4: 测试节点菜单操作符（9 个）
  - [ ] SubTask 4.1: 测试 create_group_from_selection, create_internal_switch
  - [ ] SubTask 4.2: 测试 quick_add_rename_rule, quick_add_vertex_group_match
  - [ ] SubTask 4.3: 测试 group_nodes_to_nested_blueprint, ungroup_nested_blueprint
  - [ ] SubTask 4.4: 测试 view_chain, align_nodes, batch_connect_nodes

- [ ] Task 5: 测试对象节点操作符（5 个）
  - [ ] SubTask 5.1: 测试 refresh_node_object_ids, select_node_object
  - [ ] SubTask 5.2: 测试 start_pick_object, select_generate_mod_folder, view_group_objects

- [ ] Task 6: 测试物体交换操作符（2 个）
  - [ ] SubTask 6.1: 测试 add_swap_option, remove_swap_option

- [ ] Task 7: 测试重命名操作符（4 个）
  - [ ] SubTask 7.1: 测试 add_rename_rule, remove_rename_rule, move_rename_rule_up, move_rename_rule_down

- [ ] Task 8: 测试材质检测操作符（5 个）
  - [ ] SubTask 8.1: 测试 material_detect_add_prefix, material_detect_remove_prefix, material_detect_add_custom_prefix
  - [ ] SubTask 8.2: 测试 material_detect, material_detect_clear

- [ ] Task 9: 测试 CrossIB 操作符（2 个）
  - [ ] SubTask 9.1: 测试 cross_ib_add_item, cross_ib_remove_item

- [ ] Task 10: 测试顶点组匹配操作符（8 个）
  - [ ] SubTask 10.1: 测试 vertex_group_match_execute, vertex_group_match_clear
  - [ ] SubTask 10.2: 测试 vertex_group_match_apply_to_source, vertex_group_match_toggle_debug
  - [ ] SubTask 10.3: 测试 vertex_group_match_sync, vertex_group_match_delete_connection
  - [ ] SubTask 10.4: 测试 vertex_group_match_detect_multi, vertex_group_match_quick_weight

- [ ] Task 11: 测试 Shader 快捷连接操作符（4 个）
  - [ ] SubTask 11.1: 测试 shader_quick_transparent, shader_quick_emission
  - [ ] SubTask 11.2: 测试 shader_quick_diffuse, shader_quick_principled

- [ ] Task 12: 测试多文件导出操作符（6 个）
  - [ ] SubTask 12.1: 测试 multi_file_export_parse_collection, multi_file_export_remove_object
  - [ ] SubTask 12.2: 测试 multi_file_export_check_vertex_count, multi_file_export_move_up, multi_file_export_move_down
  - [ ] SubTask 12.3: 测试 multi_file_export_split_animation

- [ ] Task 13: 测试顶点属性后处理操作符（2 个）
  - [ ] SubTask 13.1: 测试 post_process_add_vertex_attribute, post_process_remove_vertex_attribute

- [ ] Task 14: 测试 Web 面板后处理操作符（1 个）
  - [ ] SubTask 14.1: 测试 post_process_open_web_panel_builder

- [ ] Task 15: 测试嵌套蓝图导航操作符（2 个）
  - [ ] SubTask 15.1: 测试 blueprint_nest_navigate, create_blueprint_from_nest

- [ ] Task 16: 测试节点预设操作符（3 个）
  - [ ] SubTask 16.1: 测试 save_node_preset, load_node_preset, delete_node_preset

- [ ] Task 17: 测试导出操作符（2 个）
  - [ ] SubTask 17.1: 测试 generate_mod_blueprint, quick_export_selected

- [ ] Task 18: 测试 UI 面板操作符（1 个）
  - [ ] SubTask 18.1: 测试 clear_preprocess_cache

- [ ] Task 19: 测试前缀快捷操作符（3 个）
  - [ ] SubTask 19.1: 测试 prefix_quick_apply, prefix_quick_refresh, prefix_quick_clear

- [ ] Task 20: 汇总测试结果并生成最终报告
  - [ ] SubTask 20.1: 汇总所有测试结果到 MD 清单
  - [ ] SubTask 20.2: 统计通过/失败/无法测试数量
  - [ ] SubTask 20.3: 整理失败项的错误原因

# Task Dependencies
- [Task 2-19] depends on [Task 1]
- [Task 20] depends on [Task 2-19]
