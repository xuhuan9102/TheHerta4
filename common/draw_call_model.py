from dataclasses import dataclass, field
from typing import Optional

from .m_key import M_Key
from .object_prefix_helper import IndexValueError, ObjectPrefixHelper, coerce_index


@dataclass
class DrawCallModel:
    """DrawCall data parsed from an exported object name."""

    obj_name: str
    source_obj_name: str = ""

    match_draw_ib: str = field(init=False, repr=False, default="")
    match_index_count: str = field(init=False, repr=False, default="")
    match_first_index: str = field(init=False, repr=False, default="")
    match_unique_str: str = field(init=False, repr=False, default="")
    match_workspace_unique_str: str = field(init=False, repr=False, default="")
    comment_alias_name: str = field(init=False, repr=False, default="")

    work_key_list: list[M_Key] = field(init=False, repr=False, default_factory=list)
    shader_replace_info_list: list[dict] = field(init=False, repr=False, default_factory=list)
    shader_replace_info_resolved: bool = field(init=False, repr=False, default=False)
    transparency_info: Optional[dict] = field(init=False, repr=False, default=None)

    index_count: int = field(init=False, repr=False, default=0)
    vertex_count: int = field(init=False, repr=False, default=0)
    index_offset: int = field(init=False, repr=False, default=0)

    @staticmethod
    def _normalize_first_index(raw_first_index: str) -> str:
        """`first_index` 只允许整数文本（薄包装，规则只在 `coerce_index`）。

        物体名 `LOD0.077983c9-10524-0.001` 的第三段会带着 Blender 的重名后缀
        `.001`（复制物体、或「快速局部导出」重跑时再现同前缀副本时自动追加）；
        下游 `int(match_first_index)` 会直接抛 ValueError 把整轮导出打死。
        身份里的 first_index 本来就是整数，所以这里只做「换成整数文本」，
        读不出来（缺段/非法文本）就留空串，由下游的 `coerce_index`/兜底点决定
        是跳过还是报错——**不在这里把它当 0**。
        """
        try:
            # 这个位置拿不到对象名（静态方法，调用点是 __post_init__ 的 prefix_parts），
            # 所以只报字段；要指名道姓的报错发生在消费点（那里有 unique_str/对象名）。
            return str(coerce_index(raw_first_index, field="match_first_index"))
        except IndexValueError:
            return ""

    def get_blender_obj_name(self) -> str:
        return self.source_obj_name if self.source_obj_name else self.obj_name

    def __post_init__(self) -> None:
        prefix_info = ObjectPrefixHelper.extract_prefix_info(self.obj_name)
        prefix = prefix_info[0] if prefix_info else self.obj_name
        prefix_parts = ObjectPrefixHelper.parse_prefix_parts(prefix)

        self.match_draw_ib = prefix_parts["draw_ib"]
        self.match_index_count = prefix_parts["index_count"]
        self.match_first_index = self._normalize_first_index(prefix_parts["first_index"])
        self.match_unique_str = (
            prefix_parts.get("bare_unique_str", "")
            or prefix_parts.get("unique_str", "")
            or prefix
        )
        self.match_workspace_unique_str = prefix_parts.get("unique_str", "") or self.match_unique_str

        if prefix_info:
            _prefix, _separator, base_name = ObjectPrefixHelper.split_name_and_prefix(
                self.obj_name,
                prefix_info[0],
                prefix_info[1],
            )
            self.comment_alias_name = base_name
        else:
            self.comment_alias_name = ""

        if not self.match_unique_str and self.match_draw_ib and self.match_index_count and self.match_first_index:
            self.match_unique_str = self.match_draw_ib + "-" + self.match_index_count + "-" + self.match_first_index
        if not self.match_workspace_unique_str:
            self.match_workspace_unique_str = self.match_unique_str

    def get_unique_str(self) -> str:
        if self.match_unique_str:
            return self.match_unique_str
        return self.match_draw_ib + "-" + self.match_index_count + "-" + self.match_first_index

    def get_workspace_unique_str(self) -> str:
        if self.match_workspace_unique_str:
            return self.match_workspace_unique_str
        return self.get_unique_str()

    def get_condition_str(self) -> str:
        if len(self.work_key_list) == 0:
            return ""

        condition_str_list = []
        for i, work_key in enumerate(self.work_key_list):
            condition = work_key.key_name + " == " + str(work_key.tmp_value)

            if i == 0:
                condition_str_list.append(condition)
            else:
                operator = getattr(work_key, "condition_operator", "&&")
                condition_str_list.append(f"{operator} {condition}")

        return " ".join(condition_str_list)

    def get_drawindexed_str(
        self,
        obj_name_draw_offset_dict: Optional[dict[str, int]] = None,
        base_vertex: int = 0,
    ) -> str:
        draw_offset = self.index_offset if obj_name_draw_offset_dict is None else obj_name_draw_offset_dict.get(
            self.obj_name,
            self.index_offset,
        )
        return f"drawindexed = {self.index_count},{draw_offset},{base_vertex}"

    def get_drawindexed_instanced_str(self, obj_name_draw_offset_dict: Optional[dict[str, int]] = None) -> str:
        draw_offset = self.index_offset if obj_name_draw_offset_dict is None else obj_name_draw_offset_dict.get(
            self.obj_name,
            self.index_offset,
        )
        return f"drawindexedinstanced = {self.index_count},INSTANCE_COUNT,{draw_offset},0,FIRST_INSTANCE"
