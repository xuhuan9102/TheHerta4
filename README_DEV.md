# Blender下载地址
- https://download.blender.org/release/

# Change Log
为了让工具保持与时俱进，支持3.6开始的所有版本，我们需要时刻关注Blender的API变化：
- 4.4 to 4.5
- https://docs.blender.org/api/4.5/change_log.html#change-log
- 4.3 to 4.4
- https://docs.blender.org/api/4.4/change_log.html#change-log
- 4.2 to 4.3
- https://docs.blender.org/api/4.3/change_log.html#change-log
- 4.1 to 4.2
- https://docs.blender.org/api/4.2/change_log.html#to-4-2
- 4.0 to 4.1
- https://docs.blender.org/api/4.1/change_log.html
- 3.6 to 4.0 
- https://docs.blender.org/api/4.0/change_log.html#change-log

# Blender API手册
- https://www.blender.org/support/
- https://docs.blender.org/api/3.6/
- https://docs.blender.org/api/4.2/
- https://docs.blender.org/api/4.5/

# 开发必备插件
- https://github.com/JacquesLucke/blender_vscode              (推荐，首选)
- https://github.com/BlackStartx/PyCharm-Blender-Plugin       (也能用，但不推荐)

# Blender插件开发中的缓存问题

在使用VSCode进行Blender插件开发中，会创建一个指向项目的软连接，路径大概如下：

C:\Users\Administrator\AppData\Roaming\Blender Foundation\Blender\4.2\scripts\addons

在插件架构发生大幅度变更时可能导致无法启动Blender，此时需要手动删掉插件缓存的这个软链接。

也就是说，迁移插件位置可能会导致如下错误：

```
Traceback (most recent call last):
  File "c:\Users\Administrator\.vscode\extensions\jacqueslucke.blender-development-0.0.30\pythonFiles\launch.py", line 28, in <module>
    blender_vscode.startup(
  File "c:\Users\Administrator\.vscode\extensions\jacqueslucke.blender-development-0.0.30\pythonFiles\include\blender_vscode\__init__.py", line 31, in startup
    path_mappings = load_addons.setup_addon_links(addons_to_load)
                    ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "c:\Users\Administrator\.vscode\extensions\jacqueslucke.blender-development-0.0.30\pythonFiles\include\blender_vscode\load_addons.py", line 40, in setup_addon_links
    load_path = _link_addon_or_extension(addon_info)
                ^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^^
  File "c:\Users\Administrator\.vscode\extensions\jacqueslucke.blender-development-0.0.30\pythonFiles\include\blender_vscode\load_addons.py", line 64, in _link_addon_or_extension
    create_link_in_user_addon_directory(addon_info.load_dir, load_path)
  File "c:\Users\Administrator\.vscode\extensions\jacqueslucke.blender-development-0.0.30\pythonFiles\include\blender_vscode\load_addons.py", line 237, in create_link_in_user_addon_directory
    _winapi.CreateJunction(str(directory), str(link_path))
FileExistsError: [WinError 183] 当文件已存在时，无法创建该文件。
```

出现此报错后，去删除掉对应位置的软链接，再次Ctrl + Shift + P即可正常调试项目

# 文件夹与文件名命名大小写问题 

所有的文件夹都必须小写，因为git无法追踪文件夹名称大小写改变的记录,至少VSCode集成的git做不到，也可能是VSCode的问题。

文件名也必须小写，因为Github也无法追踪文件名的大小写变化。

# 插件架构设计
架构由底层到上层如下依次排列，但需求是从用户层ui触发然后设计到具体每一个小utils的。

基础层级:  
- base 基础抽象数据类型  
 - config:所有与配置相关的内容
 - resources: hlsl与图片资源
 - utils 所有基础功能最小的单元拆分为工具类
 - wwmi_config WWMI用到的配置类
- common 高级抽象数据类型
 - blueprint_node 蓝图节点
 - d3d11 基础d3d11数据类型抽象
 - export 生成Mod逻辑
 - migoto 3Dmigoto数据结构
 - read_in 模型导入逻辑


帮助类层级: 
- helper 相当于建立在基础抽象数据类型上的工具类

逻辑处理层级:
- common:高级一点的拆分为功能类。功能类的功能是比较复杂的实现一个功能，工具类则只负责特定小功能实现，比如字符串分割是一个工具类功能，导入顶点组权重到mesh是一个功能类功能。
- games:各个游戏的导入流程和Mod生成流程必须拆分开来

用户交互层级:
- ui:用户交互部分


# Properties的设计和使用问题

我们不得不给把Properties分开放到不同类中，并提供classmethod来直接进行调用。

如果不这样设计的话，在其中一个Property遭到废弃或者发生大幅度变更时，

如果修改代码的时候不注意，就会导致部分地方没有完全修改，

设计成现在这样就能避免这些问题，用法和声明上都有了统一的规范。

# Jinja2的ini模板问题

我们不模仿XXMI-Tools和WWMI-Tools使用Jinja2的ini模板，
以便于在生成Mod逻辑发生变更后，能够第一时间通过修改代码来同步特性。

我们的项目尽可能遵循奥卡姆剃刀原理，不引入额外的组件和学习成本，
尽可能让每个人拿到源码后都能像滑雪一样顺畅的读完整个逻辑并理解大概原理，
除非到了哪天由于重要的特性不得不引入，否则暂时不加入Jinja2模板ini功能。


# 现存问题
目前TheHerta3里很多错误的用法限制了整体数据类型的搭建，要进行以下改动：
- 凡不使用类中信息的工具类，应从@classmethod改为@staticmethod。
- 所有的类都应该使用@dataclass,在post_init中进行后初始化，否则难以理解。
- 先声明，后执行，而不是边声明边执行，例如WWMITools就是先把所有的状态和逻辑声明好，最后触发的时候统一执行，层层传递数据类型，其对数据类型的准确封装是构建优雅数据结构的关键。如果不是先声明后执行的话，就会导致重复获取某些信息，例如mesh.loops在导出时多次重复获取其中内容导致执行速度下降。这就是设计上的缺陷导致的，如果是先声明后执行，可复用之前结果就不会导致重复情况。
- Python看似简单，但是如果不当成一个强数据类型的语言来使用，在Blender插件开发时就会遇到许多问题，所以后续所有的方法参数和返回值以及类的属性都必须声明类型，应逐个检查并重构整个插件。
- 对于Blender插件开发来说，其对Blender的bpy部分的封装应该在数据结构的较底层，否则将会导致代码混乱且无法理解原理，也就是我们目前面临的情况。有一部分工具类必须是专门负责和bpy中类型进行对接使用的。


# IndexBuffer和CategoryBuffer的架构

一个DrawIB里，基于MatchFirstIndex和IndexCount可以分出多个Component

此时每个Component都可以有属于它自己的VertexBuffer
也可以所有Component公用一个VertexBuffer

所有Component可以共用一个IndexBuffer
也可以每个Component分配一个独立的IndexBuffer

上述问题一共会导致4种情况，目前这四种情况在各个游戏中均有出现。

例如：
EFMI 
- 每个Component有独立的IB和VB

WWMI 
- 所有Component公用一个IB和VB  

GIMI/其它米游
- 每个Component可以公用一个独立的IB，也可以每个Component一个IB
- 所有Component公用一个VB

我们之前的架构是，依次对每个obj统计IndexBuffer和每个Category的Buffer
到导出的时候让对应的逻辑决定是否去合并IndexBuffer以及CategoryBuffer

我们4的新架构要从点击导出按钮的那一刻开始，根据这个游戏的最优架构，先走一个obj合并流程，得到一批克隆出来的obj
- 直接去按component合并obj，或者不合并obj
- 直接去按DrawIB合并obj，或者不合并obj
得到的这些obj，再去做shapekey、ib、vb计算等处理
在物体较多时应该能够明显改善导出速度，因为省去了拼接过程，且更加不容易出错，因为合并在一起和分开时有的边和点什么的都不一样。


# DrawIBModel问题

我们之前的流程非常简单，因为每个DrawIB为一个组，所以导出时按照DrawIB轻松分组

但是现在是以每个IndexCount,FirstIndex,DrawIB为一个单位，所以在DrawIB下面还要多一个ComponentModel

每个蓝图解析完毕后，应该有一堆ComponentModel，随后按照不同的游戏处理逻辑进行最终内容的组装。

所以在解析完成：

        # 全局obj_model列表，主要是obj_model里装了每个obj的生效条件。
        self.ordered_draw_obj_data_model_list:list[ObjDataModel] = [] 

之后，在此之上完成ComponentModel层级抽象，设计为一个ComponentModel的列表
后续导出流程就是每个游戏都有单独的流程去处理这个ComponentModel列表。

或者说，到了ordered_draw_obj_data_model_list这一步，后续的步骤直接转移给每个游戏具体的导出流程
省去ComponentModel层级，各个游戏处理流程尽可能函数式编程避免无法修改。

因为DrawIBModel存在的意义是进行buffer文件的解析和导出
如果游戏需要ComponentModel这个层级则需要实现，如果不需要还得走DrawIBModel层级
所以这俩东西其实是一样的，只是每个游戏处理方式不同导致过程也不同，所以最终移动到每个游戏各自的逻辑里是最好的，而不是抽象复用

复用类级别的抽象在这里反而导致了过度依赖问题。

# 非镜像工作流问题
在导入时，通过把Scale的X分量设为-1并应用，来让模型不镜像
在导出时，把Scale的X分量再设为-1并应用，让模型镜像回来
这样就避免了底层数据结构的操作，非常优雅，且后续基本上就应该这么做

所以暂时删掉所有旧的非镜像工作流代码，等待后续测试

