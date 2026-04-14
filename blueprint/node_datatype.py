import os

import bpy
from bpy.props import StringProperty, CollectionProperty
from bpy.types import PropertyGroup

from .node_base import SSMTNodeBase, SSMTSocketObject
from ..utils.json_utils import JsonUtils


# 全局变量：记录已打印过日志的 draw_ib，避免重复打印
_logged_datatype_overrides = set()


def reset_datatype_override_log():
    """重置日志缓存，在每次导出开始时调用"""
    global _logged_datatype_overrides
    _logged_datatype_overrides = set()


def extract_buffer_category(buffer_info: dict) -> str:
    """从 buffer 信息中提取类型名称

    优先从 FileName 中提取，格式为 "xxx-Category.buf"
    如果没有 FileName，则从第一个 D3D11Element 的 Category 字段提取

    Args:
        buffer_info: buffer 信息字典

    Returns:
        类型名称（如 Position、Texcoord、Blend）
    """
    if not isinstance(buffer_info, dict):
        return ''

    file_name = str(buffer_info.get('FileName', '') or '')
    if file_name:
        basename = os.path.basename(file_name)
        if '-' in basename:
            parts = basename.split('-')
            if len(parts) >= 2:
                return parts[-1].replace('.buf', '')
        return os.path.splitext(basename)[0]

    first_elem = None
    element_list = buffer_info.get('D3D11ElementList') or []
    if isinstance(element_list, list) and element_list:
        first_elem = element_list[0]

    if isinstance(first_elem, dict):
        return str(first_elem.get('Category', '') or first_elem.get('DrawCategory', '') or '')

    return ''


def build_category_override_dict(override_category_buffers: list) -> dict:
    """构建类型到 D3D11ElementList 的映射

    Args:
        override_category_buffers: 覆盖配置中的 CategoryBufferList

    Returns:
        dict: {category: [D3D11ElementList]}
    """
    result = {}
    for buffer_info in override_category_buffers:
        if not isinstance(buffer_info, dict):
            continue

        category = extract_buffer_category(buffer_info)
        if not category:
            continue

        element_list = buffer_info.get('D3D11ElementList', [])
        if isinstance(element_list, list):
            result[category] = [dict(elem) for elem in element_list if isinstance(elem, dict)]

    return result


def build_override_element_list(
    original_category_buffers: list,
    override_data: dict,
    draw_ib: str
) -> list:
    """构建覆盖后的 D3D11ElementList

    根据原始 CategoryBufferList 结构，使用 override_data 中对应类型的
    D3D11ElementList 进行替换。

    Args:
        original_category_buffers: 原始的 CategoryBufferList
        override_data: 节点加载的配置数据
        draw_ib: 当前处理的 draw_ib

    Returns:
        合并后的 D3D11ElementList
    """
    if not original_category_buffers:
        return []

    override_category_buffers = override_data.get('CategoryBufferList', [])
    override_dict = build_category_override_dict(override_category_buffers)

    result_elements = []
    has_override = False
    replaced_categories = []

    for buffer_info in original_category_buffers:
        if not isinstance(buffer_info, dict):
            continue

        category = extract_buffer_category(buffer_info)

        if category in override_dict:
            override_elements = override_dict[category]
            result_elements.extend(override_elements)
            has_override = True
            replaced_categories.append(category)
        else:
            original_elements = buffer_info.get('D3D11ElementList', [])
            if isinstance(original_elements, list):
                result_elements.extend([dict(elem) for elem in original_elements if isinstance(elem, dict)])

    if has_override and draw_ib not in _logged_datatype_overrides:
        _logged_datatype_overrides.add(draw_ib)
        print(f"[DataType节点] 覆盖 {draw_ib} 的数据类型:")
        print(f"  - 替换的类型: {', '.join(replaced_categories)}")
        print(f"  - 总元素数: {len(result_elements)}")

    return result_elements


class BufferInfoItem(PropertyGroup):
    """Buffer 信息项，用于存储解析后的 buffer 类型信息"""
    category: StringProperty(default='')
    file_name: StringProperty(default='')
    type_name: StringProperty(default='')


class SSMTNode_DataType(SSMTNodeBase):
    """数据类型节点

    这个节点用于在导出阶段将指定 IB 的数据类型替换为选定的 JSON 配置文件中的数据。
    支持通过文件名识别数据类型（Position、Texcoord、Blend等），并替换对应的 D3D11ElementList。
    """

    bl_idname = 'SSMTNode_DataType'
    bl_label = 'Data Type'
    bl_icon = 'FILE_FOLDER'
    bl_width_min = 320

    # IB 匹配字符串，多个 IB 用逗号分隔
    draw_ib_match: StringProperty(
        name='IB',
        description='多个 IB 使用逗号分隔，节点会匹配这些 IB',
        default='',
    )

    # 配置文件路径（自动刷新）
    tmp_json_path: StringProperty(
        name='配置文件',
        description='用于覆盖数据类型的 JSON 配置文件',
        subtype='FILE_PATH',
        default='',
        update=lambda self, context: self._auto_refresh(),
    )

    # 解析后的 WorkGameType
    work_game_type: StringProperty(
        name='WorkGameType',
        default='',
    )

    # 解析错误信息
    parse_error: StringProperty(
        name='ParseError',
        default='',
    )

    # 解析后的 buffer 信息列表
    parsed_buffers: CollectionProperty(
        name='ParsedBuffers',
        type=BufferInfoItem,
    )

    def init(self, context):
        """节点初始化"""
        self.inputs.new('SSMTSocketObject', 'Input')
        self.outputs.new('SSMTSocketObject', 'Output')
        self.width = 360

    def draw_buttons(self, context, layout):
        """绘制节点界面"""
        col = layout.column()
        col.prop(self, 'draw_ib_match', text='IB')
        col.prop(self, 'tmp_json_path', text='配置文件')

        # 显示解析错误
        if self.parse_error:
            layout.label(text=f"解析失败: {self.parse_error}", icon='ERROR')

        # 显示数据类型信息
        if self.work_game_type:
            layout.separator()
            if self.draw_ib_match:
                layout.label(text=f"IB: {self.draw_ib_match}")
            layout.label(text=f"数据类型: TYPE_{self.work_game_type}")

        # 显示 Buffer 类型信息
        if len(self.parsed_buffers) > 0:
            layout.separator()
            layout.label(text="Buffer 类型:")
            for item in self.parsed_buffers:
                label_text = item.category or '<未知>'
                details = []
                if item.file_name:
                    details.append(item.file_name)
                if item.type_name:
                    details.append(item.type_name)
                detail_text = ' / '.join(details)
                layout.label(text=f"  {label_text}: {detail_text}")
        else:
            if self.tmp_json_path and not self.parse_error and not self.work_game_type:
                layout.separator()
                layout.label(text="未解析到任何数据类型", icon='INFO')

    def _auto_refresh(self):
        """自动刷新回调，在配置文件路径变化时自动调用"""
        try:
            self.refresh_datatype_info()
        except Exception:
            pass

    def refresh_datatype_info(self):
        """刷新数据类型信息，解析配置文件"""
        # 清空之前的数据
        self.work_game_type = ''
        self.parse_error = ''
        self.parsed_buffers.clear()

        if not self.tmp_json_path:
            return

        # 处理文件路径
        raw_path = self.tmp_json_path.strip()
        if os.path.isabs(raw_path):
            abs_json_path = raw_path
        else:
            abs_json_path = bpy.path.abspath(raw_path)

        # 检查文件是否存在
        if not os.path.exists(abs_json_path):
            self.parse_error = '配置文件不存在'
            return

        # 加载 JSON 文件
        json_data = JsonUtils.LoadFromFile(abs_json_path)
        if not isinstance(json_data, dict) or not json_data:
            self.parse_error = '配置文件不是有效的 JSON 对象'
            return

        # 解析 WorkGameType
        work_game_type = json_data.get('WorkGameType', '')
        if work_game_type:
            self.work_game_type = str(work_game_type)

        # 解析 CategoryBufferList
        category_buffer_list = json_data.get('CategoryBufferList', [])
        if isinstance(category_buffer_list, list):
            for buffer_info in category_buffer_list:
                if not isinstance(buffer_info, dict):
                    continue
                file_name = str(buffer_info.get('FileName', '') or '')
                type_name = str(buffer_info.get('Type', '') or '')
                category = extract_buffer_category(buffer_info)
                item = self.parsed_buffers.add()
                item.category = category
                item.file_name = file_name
                item.type_name = type_name

        # 检查是否解析到有效数据
        if not self.work_game_type and len(self.parsed_buffers) == 0 and not self.parse_error:
            self.parse_error = '未能从配置文件中解析到有效数据'

    def is_draw_ib_matched(self, draw_ib: str) -> bool:
        """检查给定的 draw_ib 是否匹配节点设置的 IB

        Args:
            draw_ib: 要检查的 IB 字符串

        Returns:
            是否匹配
        """
        if not self.draw_ib_match or not draw_ib:
            return False

        draw_ib_values = [item.strip() for item in self.draw_ib_match.split(',') if item.strip()]
        return draw_ib in draw_ib_values

    def get_work_game_type(self) -> str:
        """获取 WorkGameType"""
        return self.work_game_type

    def get_category_buffer_list(self) -> list:
        """获取解析后的 buffer 信息列表"""
        result = []
        for item in self.parsed_buffers:
            result.append({
                'category': item.category,
                'file_name': item.file_name,
                'type': item.type_name,
            })
        return result

    @property
    def loaded_data(self) -> dict:
        """获取加载的配置数据

        直接从配置文件读取，不依赖于 loaded_json_path
        """
        if not self.tmp_json_path:
            return {}

        raw_path = self.tmp_json_path.strip()
        if os.path.isabs(raw_path):
            abs_json_path = raw_path
        else:
            abs_json_path = bpy.path.abspath(raw_path)

        if not os.path.exists(abs_json_path):
            return {}

        return JsonUtils.LoadFromFile(abs_json_path)

    def get_override_d3d11_element_list(self, target_category: str = None) -> list:
        """获取用于覆盖的 D3D11ElementList

        Args:
            target_category: 目标数据类型（Position、Texcoord、Blend等）
                           如果为 None，则返回所有类型的合并列表

        Returns:
            D3D11ElementList 列表
        """
        loaded = self.loaded_data
        if not loaded:
            return []

        if target_category is None:
            return self._get_all_d3d11_element_list(loaded)

        return self._get_category_d3d11_element_list(loaded, target_category)

    def _get_all_d3d11_element_list(self, loaded_data: dict) -> list:
        """获取所有类型的 D3D11ElementList"""
        result = []
        for buffer_info in loaded_data.get('CategoryBufferList', []):
            if not isinstance(buffer_info, dict):
                continue
            element_list = buffer_info.get('D3D11ElementList', [])
            if isinstance(element_list, list):
                for elem in element_list:
                    if isinstance(elem, dict):
                        result.append(dict(elem))
        return result

    def _get_category_d3d11_element_list(self, loaded_data: dict, target_category: str) -> list:
        """获取指定类型的 D3D11ElementList"""
        result = []
        for buffer_info in loaded_data.get('CategoryBufferList', []):
            if not isinstance(buffer_info, dict):
                continue

            category = extract_buffer_category(buffer_info)
            if category != target_category:
                continue

            element_list = buffer_info.get('D3D11ElementList', [])
            if isinstance(element_list, list):
                for elem in element_list:
                    if isinstance(elem, dict):
                        result.append(dict(elem))
        return result

    def get_category_buffer_override_dict(self) -> dict:
        """获取按类型分组的 D3D11ElementList 覆盖数据

        Returns:
            dict: {category: D3D11ElementList}
        """
        loaded = self.loaded_data
        if not loaded:
            return {}

        return build_category_override_dict(loaded.get('CategoryBufferList', []))


def register():
    bpy.utils.register_class(BufferInfoItem)
    bpy.utils.register_class(SSMTNode_DataType)


def unregister():
    bpy.utils.unregister_class(SSMTNode_DataType)
    bpy.utils.unregister_class(BufferInfoItem)
