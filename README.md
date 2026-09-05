
# TheHerta4

SSMT4的Blender插件


# 仍在开发中

在此仓库开发完成发布Release版本之前，请优先考虑使用希尔维护的TheHerta3版本：

https://github.com/xuhuan9102/TheHerta3

# 说明

TheHerta3中的蓝图架构设计与导出流程高度耦合导致无法轻易修改和测试SSMT4中全新的全量提取设计

所以直接开了个新仓库，与旧SSMT3整体切割，你可以理解为SSMT4 + TheHerta4是一套全新的工具了

> 注意: 此插件不兼容SSMT3，只能搭配SSMT4使用

# 版本选择

- SSMT4和TheHerta4的版本几乎是同步更新，尽量全部使用最新版防止功能无法一一对应。
- Blender推荐最低使用4.5LTS版本，如遇到BUG请提交issue。

# Velo Tools 桥接（实验性）

本分支提供 `TheHerta4_Velo_Bridge` 实验性扩展，用于把 Velo Tools 当前游戏工作空间中的组件集合导入 TheHerta4 蓝图，并从蓝图的 `Velo Mod（实验性）` 输出节点调用 Velo 导出器。桥接会临时建立导出集合、同步物体所属 Component、注入蓝图中的物体切换状态，导出完成后再运行连接在输出节点后的 TheHerta4 后处理链；原 Velo 配置会在流程结束时恢复。

桥接日志同时写入 Blender 控制台和用户目录下的 `TheHerta4_Velo_Bridge.debug.log`，统一使用 `[TheHerta4][VeloBridge][Experimental]` 前缀。日志用于定位工作空间、物体解析、切换变量注入、INI 条件重写、导出和后处理阶段的问题。该功能依赖已安装并启用的 Velo Tools，当前仍属于实验性适配，建议保留导出日志并检查生成的 INI。

当前已适配的 TheHerta4 节点：

- `材质转资源pro`：支持 Velo 输出节点作为结果节点，并按物体切换组限制资源/INI 生成。
- `物体切换`：桥接会读取切换状态并生成 Velo KeySwap 变量。
- `物体切换面板`：支持沿 Velo 输出链运行，并复用 Velo 生成的组件检测信息。
- `配置文件清理`：可作为桥接输出后的后处理节点，清理生成 INI 中的非 ASCII 文本。
- 常规对象信息、对象组和重定向节点：用于构建桥接节点的输入对象链。

# 插件开发

开发插件请使用VSCode和VSCode插件:
- Blender Development (作者是 Jacques Lucke)

# 主仓库与分支仓库

此仓库用于探索最新架构，完整版功能将由希尔迁移至TheHerta3中实现。

开发者职责分布：

- Nico 负责 TheHerta4 新架构开发，适用于新特性新功能新架构研发测试。
- 希尔 负责 TheHerta3 维护，功能扩展，工具集集成，适用于Mod制作生产环境。

如果需要在此仓库内容基础上进行功能扩展，请Fork一份在自己的仓库中开发和发布，不要提交回主仓库

严格来说，TheHerta4主仓库仅负责核心架构的搭建，附加扩展功能(例如各种蓝图节点)都将由各位Fork版作者进行维护

# Fork分支列表

- https://github.com/xuhuan9102/TheHerta3

如果你有自己维护的分支，可以提交PR修改README在此处添加你维护的分支版。
