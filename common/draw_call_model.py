from ..utils.ssmt_error_utils import SSMTErrorUtils
from .m_key import M_Key

from dataclasses import dataclass, field
from typing import Optional

@dataclass
class DrawCallModel:
    """DrawCall 模型类
    
    用于表示一个 DrawCall 的相关信息，包括匹配参数和生效条件。
    从对象名称中解析出 DrawIB、IndexCount、FirstIndex 等信息。
    
    Attributes:
        obj_name: 对象名称，格式为 "DrawIB-IndexCount-FirstIndex.AliasName"
        match_draw_ib: 用于匹配的 DrawIB 哈希值
        match_index_count: 用于匹配的 IndexCount
        match_first_index: 用于匹配的 FirstIndex
        comment_alias_name: 用于显示在注释中的自定义名称
        work_key_list: 生效条件列表，在 BlueprintModel 解析时得到
        index_count: 索引数量，在 SubMeshModel 层级计算得到
        vertex_count: 顶点数量，在 SubMeshModel 层级计算得到
        index_offset: 索引偏移，在 SubMeshModel 层级计算得到
    """
    obj_name:str

    match_draw_ib:str = field(init=False,repr=False,default="")  # 用于匹配的DrawIB哈希值
    match_index_count:str = field(init=False,repr=False,default="")  # 用于匹配的IndexCount
    match_first_index:str = field(init=False,repr=False,default="")  # 用于匹配的FirstIndex
    comment_alias_name:str = field(init=False,repr=False,default="")  # 注释中显示的自定义名称

    work_key_list:list[M_Key] = field(init=False,repr=False,default_factory=list)  # 生效条件列表

    index_count:int = field(init=False,repr=False,default=0)  # 索引数量
    vertex_count:int = field(init=False,repr=False,default=0)  # 顶点数量
    index_offset:int = field(init=False,repr=False,default=0)  # 索引偏移


    def __post_init__(self) -> None:
        """初始化后处理，解析对象名称
        
        对象名称格式: DrawIB-IndexCount-FirstIndex.AliasName
        例如: 67f829fc-2653-0.头发
        
        Raises:
            SSMTFatalError: 当对象名称格式不正确时抛出错误
        """
        objname_parse_error_tips = "Obj名称规则为: DrawIB-IndexCount-FirstIndex.AliasName,例如[67f829fc-2653-0.头发]第一个.前面的内容要符合规则,后面出现的内容是可以自定义的"

        if "." not in self.obj_name:
            SSMTErrorUtils.raise_fatal("Obj名称解析错误: " + self.obj_name + "  不包含'.'分隔符\n" + objname_parse_error_tips)

        obj_name_total_split = self.obj_name.split(".")
        obj_name_split = obj_name_total_split[0].split("-")

        if len(obj_name_total_split) < 2:
            SSMTErrorUtils.raise_fatal("Obj名称解析错误: " + self.obj_name + "  不包含'.'分隔符\n" + objname_parse_error_tips)

        self.comment_alias_name = ".".join(obj_name_total_split[1:]) if len(obj_name_total_split) > 1 else ""

        if len(obj_name_split) < 3:
            SSMTErrorUtils.raise_fatal("Obj名称解析错误: " + self.obj_name + "  '-'分隔符数量不足，至少需要2个\n" + objname_parse_error_tips)

        self.match_draw_ib = obj_name_split[0]
        self.match_index_count = obj_name_split[1]
        self.match_first_index = obj_name_split[2]
    
    def get_unique_str(self) -> str:
        """获取唯一标识符字符串
        
        该唯一标识符是根据 DrawIB、FirstIndex 和 IndexCount 组成的字符串，
        作为一个整体来标识一个 DrawCall，同时也和提取出来的工作空间目录下对应的目录名称一致。
        
        Returns:
            str: 格式为 "DrawIB-IndexCount-FirstIndex" 的唯一标识符
        """
        return self.match_draw_ib + "-" + self.match_index_count+ "-" + self.match_first_index 

    def get_condition_str(self) -> str:
        """获取生效条件字符串
        
        根据 work_key_list 生成条件字符串，多个条件之间使用各自的逻辑运算符连接。
        第一个条件不加运算符前缀，后续条件使用各自的 condition_operator。
        每个条件格式为: $swapkeyN == value
        
        Returns:
            str: 条件字符串，如 "$swapkey0 == 1 && $swapkey1 == 1 || $swapkey2 == 1"
                 如果没有条件则返回空字符串
        """
        if len(self.work_key_list) == 0:
            return ""

        condition_str_list = []
        for i, work_key in enumerate(self.work_key_list):
            condition = work_key.key_name + " == " + str(work_key.tmp_value)
            
            # 第一个条件不加运算符前缀，后续条件使用各自的 condition_operator
            if i == 0:
                condition_str_list.append(condition)
            else:
                operator = getattr(work_key, 'condition_operator', '&&')
                condition_str_list.append(f"{operator} {condition}")

        return " ".join(condition_str_list)

    def get_drawindexed_str(self, obj_name_draw_offset_dict: Optional[dict[str, int]] = None) -> str:
        """获取 drawindexed 命令字符串
        
        Args:
            obj_name_draw_offset_dict: 对象名称到绘制偏移的映射字典，
                                       如果为 None 则使用自身的 index_offset
        
        Returns:
            str: 格式为 "drawindexed = index_count,offset,0" 的命令字符串
        """
        draw_offset = self.index_offset if obj_name_draw_offset_dict is None else obj_name_draw_offset_dict.get(self.obj_name, self.index_offset)
        return f"drawindexed = {self.index_count},{draw_offset},0"

    def get_drawindexed_instanced_str(self, obj_name_draw_offset_dict: Optional[dict[str, int]] = None) -> str:
        """获取 drawindexedinstanced 命令字符串
        
        Args:
            obj_name_draw_offset_dict: 对象名称到绘制偏移的映射字典，
                                       如果为 None 则使用自身的 index_offset
        
        Returns:
            str: 格式为 "drawindexedinstanced = index_count,INSTANCE_COUNT,offset,0,FIRST_INSTANCE" 的命令字符串
        """
        draw_offset = self.index_offset if obj_name_draw_offset_dict is None else obj_name_draw_offset_dict.get(self.obj_name, self.index_offset)
        return f"drawindexedinstanced = {self.index_count},INSTANCE_COUNT,{draw_offset},0,FIRST_INSTANCE"
        
 