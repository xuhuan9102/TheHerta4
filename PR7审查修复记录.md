# PR #7 审查修复记录

> 记录 PR #7（Velo 桥接 + 配置文件清理）审查中发现并处理的问题、逐函数对比结论，
> 以及留给下次处理的遗留项。审查日期与修复提交见 git log。

## 一、本次已修复（随 PR #7 提交）

### 阻断级

1. **插件无法启用（ModuleNotFoundError）**
   原因：`TheHerta4/__init__.py` 使用顶级绝对导入 `import TheHerta4_Velo_Bridge`，
   而该模块位于 addon 包内部（`addons/TheHerta4/TheHerta4_Velo_Bridge/`），
   打包安装后 `sys.path` 上不存在顶层同名包 → `register()` 直接失败。
   修复：改为双路径导入（先绝对、失败再相对），两种安装布局（包内子包 /
   独立复制到 addons 目录）均可用。

2. **继承已注册节点类导致「材质转资源」节点损坏**
   原因：`node_postprocess_material.py` 删除了未注册基类，让
   `SSMTNode_PostProcess_CustomMaterialAssign` 直接继承已注册的
   `SSMTNode_PostProcess_Material`——正是被删除注释中记载的禁用模式。
   实测（Blender 5.0.1 / 4.5.5）：注册后创建旧节点报
   `unable to get Python class for RNA struct 'SSMTNode_PostProcess_Material'`。
   修复：恢复「未注册实现基类 + 各自注册」结构——
   `SSMTNode_PostProcess_MaterialBase`（未注册，全部实现与属性）←
   `SSMTNode_PostProcess_Material`（弃用壳，仅保 bl_idname 兼容旧文件）与
   `SSMTNode_PostProcess_CustomMaterialAssign`（pro，继承 Base）。

### 原版「材质转资源」的处置（逐函数对比结论）

逐方法对比（方法清单见下）结论：**pro 是原版的真超集**，覆盖成立，因此：

- 菜单移除「材质转资源」入口，只保留「材质转资源pro」；
- 旧蓝图节点：加载时由弃用壳承接（实测：类未注册时 Blender 会**静默丢弃**节点，
  必须保留壳），随后 `load_post` / 注册时自动原位迁移为 pro——
  复制全部共享属性、切换为「使用全局指定」（等价原版全场景行为）、重连 socket。

pro 覆盖方式：每个被覆写的方法均为 `super() + 增量`：

| 方法 | pro 的覆盖方式 |
|---|---|
| `execute_postprocess` | 目标校验后直接 `super()`（导出核心完全复用） |
| `process_texture_override_section` | 目标段过滤 + swapkey 规划 + `super()` + 目标绘制块移动/恢复块 |
| `find_matching_materials` / `find_object_by_mesh_name` | 白名单过滤 + `super()`，全局模式直通 |
| `generate_material_lines` / `_find_workspace_slot_materials` / `_collect_ps_texture_slot_materials` | `super()` + 禁用切换只留第一套材质 |
| `define_swapkeys_in_sections` | 清理旧 KeySwap 块 + `super()` + 增量 KeySwap 段写入 |
| `_strip_generated_material_lines` | 先清恢复标记块 + `super()` |
| `draw_buttons` | 完整复刻检测面板 + 增量（目标部件/全局扫描/keyswap 面板） |

属性层面：pro 直接继承基类全部 8 个原版属性（`material_to_resource_override`、
`debug_disable_fx_ttl`、`material_switch_var`、`material_detect_prefixes`、
`temp_prefix_input`、`detected_materials`、`detect_all_ok`、`show_detect_panel`）。
`apply_name_mapping` 等导出管线按 `getattr` 调用的挂钩亦随继承保留。

### 中等/次要问题（均本次合并引入，随提交修复）

1. `node_swap_ini.py`：`SSMTNode_Result_Output_NTMI_ModImp` 拼写错误（实为
   `SSMTNode_Result_Output_NTMIModImp`），该条目原本是死代码 → 已修正。
2. `ExportVeloWorkspace.execute`：`original_auto_split` 未预初始化，
   先于赋值点抛错（如游戏不匹配守卫）时 finally 触发 `UnboundLocalError`
   掩盖真实错误 → 已与 `toggle_state` 一并预初始化，去掉 `locals()` 取巧。
3. `_inject_swap_toggles`：清空用户 toggle 行后若构建失败，原配置无法还原
   （数据丢失风险）→ 已事务化：异常时自动 `_restore_swap_toggles` 后重抛。
4. `_sync_switch_variable_fields`：按变量名在**全部**蓝图树间同步，不同蓝图
   同名变量互相干扰 → 已限定同步范围为触发变量所属蓝图。
5. 桥接死代码/怪代码：`_normalize_postprocess_swap_variables`（无调用）已删除；
   `_rewrite_nested_toggle_conditions` 的空转 `except: raise` 与
   `__import__('pathlib')` 已清理；`BRIDGE_VERSION` 与 `bl_info` 对齐为 0.2.0。
6. **前置插件检测**：新增 `VELO_TOOLS_AVAILABLE`（`importlib.util.find_spec`）
   检测，未安装时节点可创建但导入/导出操作直接 CANCELLED 并明确报错
   （参考 NTMI 的 `SSMT_OT_CheckNTMIModImpDependency` 检测模式），
   节点面板同步显示「未安装前置插件 Velo Tools，无法导出」。

## 二、遗留项（下次处理）

1. **`material_switch_var` 无面板编辑入口**：pro 面板未暴露该属性（属性经继承
   仍存在且导出逻辑照常使用默认值），原版面板可编辑。如需完全等价，可在
   pro 的「材质转资源选项」框内补一个该属性的 prop。
2. **名称映射提示行缺失**：原版面板显示「已应用 N 条名称映射」信息行，
   pro 面板未显示（`apply_name_mapping` 功能本身仍生效）。
3. **Velo 桥接主链路未实测**：本机未安装 Velo Tools，`workspace`/导出/INI
   重写/后处理链集成只能静态审查。合并后需在装有 Velo Tools 的环境跑通
   「导入工作空间 → 导出 mod → 后处理链执行 → 配置还原」全流程。
4. **测试覆盖**：新增的桥接模块、迁移逻辑、扫描重建（shape 保持/合并组）
   均无自动化测试；`tests/test_node_postprocess_material_htmi.py` 未覆盖
   pro 相关改动。
5. **迁移为一次性数据变更**：旧节点迁移在 addon 启用/文件加载时自动执行，
   会改写用户 .blend 数据（换用 pro 节点）。建议在版本发布说明中提示用户
   先备份蓝图文件。

## 三、验证方法（复现用）

- 注册/节点创建实测：
  `blender --background --factory-startup --python <script>`，
  脚本将仓库快照目录加入 `sys.path` 后 `import TheHerta4; TheHerta4.register()`，
  再对 `SSMTBlueprintTreeType` 树逐个 `nodes.new(...)`。
- 旧节点丢弃行为实测：先注册「树+节点」保存 .blend，再用仅注册树（不注册
  节点类）的进程加载 → 节点被静默移除（`NO_NODES`）。
- 迁移实测：构造含 `SSMTNode_PostProcess_Material` 节点的树，触发
  `_migrate_legacy_material_nodes_handler()` 后检查节点已变为
  `SSMTNode_PostProcess_CustomMaterialAssign` 且连线/属性保留。