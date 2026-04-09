# 物体切换节点（ObjectSwap Node）技术报告

## 1. 功能说明

### 1.1 概述

物体切换节点是一个用于 3DMigoto/XXMI Mod 制作的核心功能节点，允许用户通过快捷键动态切换游戏中的物体显示。该节点支持无限嵌套、自定义快捷键和多种切换类型。

### 1.2 核心功能特性

| 功能 | 描述 |
|------|------|
| **多选项切换** | 支持 1-1024 个选项，每个选项对应一个输入槽 |
| **切换类型** | cycle（循环切换）、toggle（开关切换）、hold（按住激活） |
| **逻辑运算符** | 支持 `&&`（AND）和 `||`（OR）连接多个条件 |
| **嵌套支持** | 支持多个物体切换节点串联，实现多维切换 |
| **INI 自动生成** | 自动生成 KeySwap、Constants、Present 等配置段落 |

### 1.3 技术指标

- **最大选项数**: 1024
- **支持的热键格式**: 3DMigoto 虚拟键码格式（如 `No_Modifiers Numpad3`）
- **条件值范围**: 0 到 (选项数-1)
- **激活参数**: `$active0`（所有节点共享）

### 1.4 使用场景

1. **角色服装切换**: 按键切换不同服装变体
2. **发型切换**: 动态切换角色发型
3. **配件开关**: 显示/隐藏特定配件
4. **多维度组合**: 多个切换节点组合实现复杂切换逻辑

---

## 2. 文件修改记录

### 2.1 核心实现文件

| 文件路径 | 修改类型 | 说明 |
|----------|----------|------|
| `blueprint/node_swap.py` | 新建/修改 | 节点 UI 定义、SwapKeyConfig 数据类 |
| `blueprint/node_swap_processor.py` | 新建/修改 | 处理链集成逻辑、SwapKeyRegistry、ObjectSwapChainProcessor |
| `blueprint/node_swap_ini.py` | 新建/修改 | INI 生成逻辑、SwapKeyINIGenerator、SwapKeyINIIntegrator |

### 2.2 调用文件（仅包含调用代码）

| 文件路径 | 修改类型 | 修改内容 |
|----------|----------|----------|
| `blueprint/model.py` | 修改 | 添加 `swap_node_option_values` 字段、`_integrate_object_swap_nodes` 方法、检测 ObjectSwap 节点代码 |
| `blueprint/__init__.py` | 修改 | 注册/注销 `node_swap` 模块 |
| `__init__.py` | 修改 | 注册/注销 `blueprint_node_swap` 模块 |
| `blueprint/node_menu.py` | 修改 | 添加节点菜单项 |
| `ui/universal/drawib_export_base.py` | 修改 | 添加 `_integrate_object_swap_ini_hook` 钩子方法 |
| `ui/universal/unity.py` | 修改 | 调用钩子方法、设置 `$active0` |
| `ui/universal/efmi.py` | 修改 | 调用钩子方法 |
| `ui/universal/srmi.py` | 修改 | 调用钩子方法 |
| `ui/universal/yysls.py` | 修改 | 调用钩子方法 |
| `ui/universal/identityv.py` | 修改 | 调用钩子方法 |
| `ui/universal/zzmi.py` | 修改 | 调用钩子方法 |
| `ui/universal/snowbreak.py` | 修改 | 调用钩子方法 |
| `common/m_ini_helper.py` | 修改 | 跳过 swapkey 处理、声明 `$active0` |
| `common/m_key.py` | 修改 | 添加 `condition_operator` 属性 |
| `common/draw_call_model.py` | 修改 | 使用 `condition_operator` 生成条件字符串 |
| `blueprint/node_base.py` | 修改 | 扩展字库支持 |

---

## 3. 代码结构分析

### 3.1 模块依赖关系图

```
┌─────────────────────────────────────────────────────────────────┐
│                     核心实现模块                                  │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────────┐    ┌──────────────────────┐               │
│  │  node_swap.py   │───▶│ node_swap_processor.py│               │
│  │                 │    │                       │               │
│  │ - SwapKeyConfig │    │ - SwapKeyRegistry     │               │
│  │ - SSMTNode_     │    │ - ObjectSwapChain     │               │
│  │   ObjectSwap    │    │   Processor           │               │
│  │ - Operators     │    │ - integrate_object_   │               │
│  └─────────────────┘    │   swap_to_blueprint   │               │
│                         └──────────────────────┘               │
│                                    │                            │
│                                    ▼                            │
│                         ┌──────────────────────┐               │
│                         │  node_swap_ini.py    │               │
│                         │                      │               │
│                         │ - SwapKeyINIGenerator│               │
│                         │ - SwapKeyINIIntegrator│              │
│                         │ - SwapKeyDebugINIWriter│             │
│                         └──────────────────────┘               │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
                              │
                              │ 调用
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                       调用模块                                    │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │
│  │  model.py   │  │ unity.py    │  │ efmi.py     │             │
│  │             │  │             │  │             │             │
│  │ - _integrate│  │ - $active0  │  │ - hook call │             │
│  │   _object_  │  │ - hook call │  └─────────────┘             │
│  │   swap_nodes│  └─────────────┘                              │
│  └─────────────┘                                               │
│                                                                 │
│  ┌─────────────┐  ┌─────────────┐  ┌─────────────┐             │
│  │ m_key.py    │  │draw_call_   │  │m_ini_helper │             │
│  │             │  │model.py     │  │.py          │             │
│  │ - condition_│  │             │  │             │             │
│  │   operator  │  │ - get_      │  │ - skip      │             │
│  └─────────────┘  │ condition_  │  │   swapkey   │             │
│                   │ str         │  └─────────────┘             │
│                   └─────────────┘                              │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 非调用性质的实现代码分析

经过详细分析，以下文件包含非调用性质的实现代码：

#### 3.2.1 `blueprint/model.py`

**问题代码 1**: `ProcessingChain.swap_node_option_values` 字段

```python
# 第 47-48 行
# 记录每个 ObjectSwap 节点的选项值（节点名称 -> 选项索引）
swap_node_option_values: Dict[str, int] = field(default_factory=dict)
```

**问题代码 2**: `_get_forward_connections_with_socket_index` 方法中的检测代码

```python
# 第 450-453 行
for next_node, socket_index in output_connections:
    # 检查下一个节点是否是 ObjectSwap，如果是，记录选项值
    if next_node.bl_idname == 'SSMTNode_ObjectSwap':
        chain.swap_node_option_values[next_node.name] = socket_index
        LOG.debug(f"   🔄 ObjectSwap 节点 '{next_node.name}' 选项值: {socket_index}")
```

**解耦状态**: ✅ 已解耦
- 字段定义使用 `field(default_factory=dict)`，默认值为空字典，不影响其他功能
- 检测代码使用字符串比较 `bl_idname == 'SSMTNode_ObjectSwap'`，如果节点不存在，条件永远为 False

#### 3.2.2 `common/m_key.py`

**问题代码**: `condition_operator` 属性

```python
# 第 33 行
self.condition_operator = "&&"  # 该条件与前面条件之间的逻辑运算符（第一个条件忽略此值）
```

**解耦状态**: ✅ 已解耦
- 属性有默认值 `"&&"`
- 使用 `getattr(work_key, 'condition_operator', '&&')` 获取，即使属性不存在也能正常工作

#### 3.2.3 `common/draw_call_model.py`

**问题代码**: `get_condition_str` 方法中的 `condition_operator` 使用

```python
# 第 101-102 行
operator = getattr(work_key, 'condition_operator', '&&')
condition_str_list.append(f"{operator} {condition}")
```

**解耦状态**: ✅ 已解耦
- 使用 `getattr` 获取属性，有默认值 `'&&'`
- 即使 `M_Key` 没有 `condition_operator` 属性也能正常工作

### 3.3 迁移方案

**结论**: 所有非调用性质的实现代码均已实现解耦，无需迁移。

当前设计已经确保：
1. 所有核心实现代码都在三个核心文件中
2. 其他文件中的代码都是调用代码或兼容性代码
3. 使用 try-except、getattr、默认值等方式确保解耦

---

## 4. 模块解耦验证

### 4.1 验证方法

删除以下三个核心文件后，验证系统是否能正常运行：
- `blueprint/node_swap.py`
- `blueprint/node_swap_processor.py`
- `blueprint/node_swap_ini.py`

### 4.2 解耦点检查清单

| 检查点 | 文件 | 状态 | 说明 |
|--------|------|------|------|
| `_integrate_object_swap_nodes` | model.py | ✅ | 使用 try-except 包裹 ImportError |
| `_integrate_object_swap_ini_hook` | drawib_export_base.py | ✅ | 使用 try-except 包裹 ImportError |
| `swap_node_option_values` 字段 | model.py | ✅ | 使用默认值，不影响其他功能 |
| `condition_operator` 属性 | m_key.py | ✅ | 有默认值 `"&&"` |
| `get_condition_str` 方法 | draw_call_model.py | ✅ | 使用 getattr 获取属性 |
| 节点注册/注销 | __init__.py | ✅ | 已改为条件导入 |
| 节点菜单项 | node_menu.py | ✅ | 已改为条件显示 |

### 4.3 解耦验证结果

| 验证项 | 结果 | 说明 |
|--------|------|------|
| 系统启动 | ✅ | 无节点注册错误 |
| 蓝图编辑器 | ✅ | 无节点类型错误 |
| 导出功能 | ✅ | 无模块导入错误 |
| INI 生成 | ✅ | 无 KeySwap 相关配置 |
| 条件生成 | ✅ | 使用默认逻辑运算符 |

---

## 5. 数据流图

### 5.1 节点创建流程

```
用户创建节点
     │
     ▼
SSMTNode_ObjectSwap.init()
     │
     ├── 创建输出 socket
     ├── 创建输入 sockets (选项_0, 选项_1, ...)
     └── 设置默认属性
```

### 5.2 处理链解析流程

```
BluePrintModel._forward_parse_blueprint()
     │
     ├── _traverse_forward()
     │      │
     │      ├── 检测 ObjectSwap 节点
     │      │      │
     │      │      └── 记录选项值到 swap_node_option_values
     │      │
     │      └── 继续遍历
     │
     └── _integrate_object_swap_nodes()
            │
            └── integrate_object_swap_to_blueprint_model()
                   │
                   ├── 收集所有 ObjectSwap 节点
                   ├── 分配 swapkey 索引
                   ├── 生成 M_Key 条件
                   └── 添加到 shapekey_params
```

### 5.3 INI 生成流程

```
ExportUnity.generate_unity_vs_config_ini()
     │
     ├── _integrate_object_swap_ini_hook()
     │      │
     │      └── SwapKeyINIIntegrator.integrate_to_export()
     │             │
     │             ├── 收集所有 ObjectSwap 节点
     │             ├── 生成 KeySwap 段落
     │             ├── 更新 Constants 段落
     │             └── 更新 Present 段落
     │
     └── 生成 TextureOverride 段落
            │
            └── 设置 $active0 = 1
```

---

## 6. 生成的 INI 配置示例

### 6.1 KeySwap 段落

```ini
[KeySwap_0]
; 头发切换
condition = $active0 == 1
key = No_Modifiers Numpad3
type = cycle
$swapkey0 = 0,1,2,

[KeySwap_1]
; 衣服切换
condition = $active0 == 1
key = No_Modifiers Numpad4
type = toggle
$swapkey1 = 0,1,
```

### 6.2 Constants 段落

```ini
[Constants]
global $active0
global persist $swapkey0 = 0
global persist $swapkey1 = 0
```

### 6.3 Present 段落

```ini
[Present]
post $active0 = 0
```

### 6.4 TextureOverride 段落

```ini
[TextureOverride_*]
hash = xxx
$active0 = 1
```

### 6.5 drawindexed 条件

```ini
; 单条件
if $swapkey0 == 1
  drawindexed = 7068,0,0
endif

; 多条件 (AND)
if $swapkey0 == 1 && $swapkey1 == 0
  drawindexed = 7068,0,0
endif

; 多条件 (OR)
if $swapkey0 == 1 || $swapkey1 == 1
  drawindexed = 7068,0,0
endif
```

---

## 7. 总结

### 7.1 实现状态

- ✅ 核心功能完整实现
- ✅ 模块解耦已完成
- ✅ 支持无限嵌套
- ✅ 支持多种切换类型
- ✅ 支持逻辑运算符
- ✅ 注册代码已改为条件导入
- ✅ 菜单项已改为条件显示

### 7.2 维护建议

1. 所有新功能应添加到三个核心文件中
2. 调用代码应使用 try-except 包裹
3. 新增属性应提供默认值
4. 使用 getattr 获取可选属性

### 7.3 删除节点后的系统行为

当删除以下三个核心文件后：
- `blueprint/node_swap.py`
- `blueprint/node_swap_processor.py`
- `blueprint/node_swap_ini.py`

系统将：
1. 正常启动，无注册错误
2. 菜单中不显示 "Object Swap" 选项
3. 导出时跳过 KeySwap 相关配置
4. 使用默认逻辑运算符生成条件字符串

---

*报告生成时间: 2026-04-09*
*适用版本: TheHerta4 4.5+*
