
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
