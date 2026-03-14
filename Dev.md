
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

