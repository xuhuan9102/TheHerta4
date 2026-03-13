
# TheHerta4

SSMT4的Blender插件

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

# 去AI化与分支版维护

在TheHerta3的蓝图功能扩展开发中，由于部分成员大量使用了AI Agent，导致生成的代码人力无法维护。

在TheHerta4中，除基础工具类函数外其它地方都不会使用AI，也就是AI只复杂最最基础黑盒子函数的实现，不参与任何业务逻辑和架构功能设计。

因此，吸取TheHerta3的教训，此仓库不接受任何Pull Request，请在自己的Fork分支中发布和维护一个分支版。

TheHerta4主仓库负责核心架构设计，复杂的功能在Fork版本中实现，以避免可能存在的AI滥用导致架构和设计污染问题。

例如TheHerta4重构完成后的蓝图功能扩充，超级工具集附加等，将由项目成员希尔在其Fork版仓库中独立完成。

