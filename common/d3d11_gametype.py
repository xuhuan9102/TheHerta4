'''
基础数据类型
'''

import json
import os
import numpy

from .d3d11_element import D3D11Element

from ..utils.format_utils import FormatUtils
from dataclasses import dataclass, field
from typing import Dict


# Designed to read from json file for game type config
@dataclass
class D3D11GameType:
    # Read config from json file, easy to modify and test.
    FilePath:str = field(repr=False)

    # Original file name.
    FileName:str = field(init=False,repr=False)
    # The name of the game type, usually the filename without suffix.
    GameTypeName:str = field(init=False)
    # Is GPU-PreSkinning or CPU-PreSkinning
    GPU_PreSkinning:bool = field(init=False,default=False)
    # All d3d11 element,should be already ordered in config json.
    D3D11ElementList:list[D3D11Element] = field(init=False,repr=False)
    # Ordered ElementName list.
    OrderedFullElementList:list[str] = field(init=False,repr=False)
    # 按顺序排列的CategoryName
    OrderedCategoryNameList:list[str] = field(init=False,repr=False)
    # Category name and draw category name, used to decide the category should draw on which category's TextureOverrideVB.
    CategoryDrawCategoryDict:Dict[str,str] = field(init=False,repr=False)


    # Generated
    ElementNameD3D11ElementDict:Dict[str,D3D11Element] = field(init=False,repr=False)
    CategoryExtractSlotDict:Dict[str,str] =  field(init=False,repr=False)
    CategoryExtractTechniqueDict:Dict[str,str] =  field(init=False,repr=False)
    CategoryStrideDict:Dict[str,int] =  field(init=False,repr=False)

    def __post_init__(self):
        self.FileName = os.path.basename(self.FilePath)
        self.GameTypeName = os.path.splitext(self.FileName)[0]

        with open(self.FilePath, 'r', encoding='utf-8') as f:
            game_type_json = json.load(f)

        self._load_from_json_dict(game_type_json)

    def _load_from_json_dict(self, game_type_json: dict):
        """从 JSON 字典加载游戏类型配置，解析所有 D3D11 元素"""

        self.OrderedFullElementList = []
        self.OrderedCategoryNameList = []
        self.D3D11ElementList = []

        self.CategoryDrawCategoryDict = {}
        self.CategoryExtractSlotDict = {}
        self.CategoryExtractTechniqueDict = {}
        self.CategoryStrideDict = {}
        self.ElementNameD3D11ElementDict = {}

        self.GPU_PreSkinning = game_type_json.get("GPU-PreSkinning",False)
        self.GameTypeName = game_type_json.get("WorkGameType","")
        self.CategoryDrawCategoryDict = game_type_json.get("CategoryDrawCategoryMap",{})
        d3d11_element_list_json = game_type_json.get("D3D11ElementList",[])
        category_offsets = {}
        for d3d11_element_json in d3d11_element_list_json:
            category_name = d3d11_element_json.get("Category", "")
            raw_aligned_byte_offset = d3d11_element_json.get("AlignedByteOffset", "")
            if raw_aligned_byte_offset is not None and str(raw_aligned_byte_offset).strip():
                element_offset = int(raw_aligned_byte_offset)
            else:
                element_offset = category_offsets.get(category_name, 0)
            d3d11_element = D3D11Element(
                SemanticName=d3d11_element_json.get("SemanticName",""),
                SemanticIndex=int(d3d11_element_json.get("SemanticIndex","")),
                Format=d3d11_element_json.get("Format",""),
                ByteWidth=int(d3d11_element_json.get("ByteWidth",0)),
                ExtractSlot=d3d11_element_json.get("ExtractSlot",""),
                ExtractTechnique=d3d11_element_json.get("ExtractTechnique",""),
                Category=category_name,
                AlignedByteOffset=element_offset,
            )
            category_offsets[category_name] = max(
                category_offsets.get(category_name, 0),
                element_offset + d3d11_element.ByteWidth,
            )
            self.D3D11ElementList.append(d3d11_element)

            # 这俩常用
            self.OrderedFullElementList.append(d3d11_element.get_indexed_semantic_name())
            if d3d11_element.Category not in self.OrderedCategoryNameList:
                self.OrderedCategoryNameList.append(d3d11_element.Category)
        
        for d3d11_element in self.D3D11ElementList:
            self.CategoryExtractSlotDict[d3d11_element.Category] = d3d11_element.ExtractSlot
            self.CategoryExtractTechniqueDict[d3d11_element.Category] = d3d11_element.ExtractTechnique
            self.ElementNameD3D11ElementDict[d3d11_element.ElementName] = d3d11_element
        self.CategoryStrideDict = dict(category_offsets)

    @classmethod
    def from_submesh_json_dict(
        cls,
        submesh_json_dict: dict,
        file_path: str = "",
        override_d3d11_element_list: list[dict] | None = None,
    ):
        """从 SubmeshJson 字典创建 D3D11GameType 实例，支持覆盖 D3D11 元素列表"""
        game_type_json = {
            "GPU-PreSkinning": submesh_json_dict.get("GPU-PreSkinning", False),
            "WorkGameType": submesh_json_dict.get("WorkGameType", ""),
            "CategoryDrawCategoryMap": submesh_json_dict.get("CategoryDrawCategoryMap", {}),
            "D3D11ElementList": override_d3d11_element_list or cls._collect_element_list_from_submesh_json(submesh_json_dict),
        }

        instance = cls.__new__(cls)
        instance.FilePath = file_path or submesh_json_dict.get("WorkGameType", "")
        instance.FileName = os.path.basename(instance.FilePath) if instance.FilePath else ""
        instance.GameTypeName = game_type_json.get("WorkGameType", "")
        instance._load_from_json_dict(game_type_json)
        return instance

    @staticmethod
    def _collect_element_list_from_submesh_json(submesh_json_dict: dict) -> list[dict]:
        d3d11_element_list_json = []
        for category_buffer_json in submesh_json_dict.get("CategoryBufferList", []):
            for d3d11_element_json in category_buffer_json.get("D3D11ElementList", []):
                d3d11_element_list_json.append(dict(d3d11_element_json))
        return d3d11_element_list_json
    
    def get_real_category_stride_dict(self) -> dict:
        new_dict = {}
        for categoryname,category_stride in self.CategoryStrideDict.items():
            new_dict[categoryname] = category_stride
        return new_dict

    def widen_blendindices(self) -> bool:
        """将 BLENDINDICES 元素统一归一化到 R16 系（用于 EFMI 骨骼合并场景）。

        EFMI 合并骨架按 LOD 输出一套布局：运行时对 vb 槽位只声明一种
        BLENDINDICES ElementFormat（efmi.py:1054-1058），且导出缓冲统一按
        uint16 打包（FormatUtils.get_nptype_from_format），因此同 LOD 所有
        部件的 BLENDINDICES 必须同为 R16 系。此处做两个方向的归一化：
        - R8* 升宽到 R16*（保留通道数与符号）；
        - R32*_UINT / R32*_SINT 降宽到 R16*（角色全局骨骼池远小于 65536，
          uint16 无损承载；同 LOD 出现 R16/R32 混用即"布局不一致"直出失败的
          根因——本方法在子网格构建期就把两侧统一，导出期校验不再报错）。

        修改运行时副本的元素 Format/ByteWidth，并联动重算 AlignedByteOffset 与
        CategoryStrideDict（get_total_structured_dtype 为动态计算，自动生效）。
        已是 R16 系时保持原通道数与格式；R32 非整数格式（如 *_FLOAT）不是骨骼
        索引语义，按旧行为保留不动。

        Returns:
            是否发生了升/降宽修改。
        """
        widen_map = {
            "R8_UINT": ("R16_UINT", 2),
            "R8G8_UINT": ("R16G16_UINT", 4),
            "R8G8B8A8_UINT": ("R16G16B16A16_UINT", 8),
            "R8_SINT": ("R16_SINT", 2),
            "R8G8_SINT": ("R16G16_SINT", 4),
            "R8G8B8A8_SINT": ("R16G16B16A16_SINT", 8),
        }
        downcast_map = {
            "R32_UINT": ("R16_UINT", 2),
            "R32G32_UINT": ("R16G16_UINT", 4),
            "R32G32B32A32_UINT": ("R16G16B16A16_UINT", 8),
            "R32_SINT": ("R16_SINT", 2),
            "R32G32_SINT": ("R16G16_SINT", 4),
            "R32G32B32A32_SINT": ("R16G16B16A16_SINT", 8),
        }

        changed = False
        for element in self.D3D11ElementList:
            if str(getattr(element, "SemanticName", "") or "").upper() != "BLENDINDICES":
                continue
            current_format = str(getattr(element, "Format", "") or "").upper()
            # R16 系已是合并骨架的最终布局；必须保留通道数和真实布局，不能把
            # 16 字节数据伪装成 R16（导出缓冲也按 R16 打包，格式与数据一致）。
            if current_format.startswith("R16"):
                continue
            if current_format.startswith("R32"):
                if current_format in {"R32G32B32_UINT", "R32G32B32_SINT"}:
                    # DXGI 没有 R16G16B16_UINT/SINT。旧逻辑会构造这个不存在的
                    # ElementFormat，直到运行时才失败；不能把三通道 R32 假装降宽。
                    raise ValueError(
                        "三通道 R32 BLENDINDICES 无法归一化为有效的 R16 DXGI 格式: "
                        f"{current_format}"
                    )
                downcasted = downcast_map.get(current_format)
                if downcasted is None:
                    # R32 非整数格式（*_FLOAT 等）不是骨骼索引语义，按旧行为保留
                    continue
                new_format, new_width = downcasted
            else:
                widened = widen_map.get(current_format)
                if widened is None:
                    raise ValueError(f"不支持升宽的 BLENDINDICES 格式: {current_format}")
                new_format, new_width = widened
            old_width = int(getattr(element, "ByteWidth", 0) or 0)
            if old_width <= 0:
                continue
            element.Format = new_format
            element.ByteWidth = new_width
            changed = True
            print(
                f"[D3D11GameType] BLENDINDICES{element.SemanticIndex} 归一化: "
                f"{current_format} -> {new_format} (ByteWidth {old_width} -> {new_width})"
            )

        if not changed:
            return False

        # 重算各类别内部的 AlignedByteOffset。
        category_offsets = {}
        for d3d11_element in self.D3D11ElementList:
            offset = category_offsets.get(d3d11_element.Category, 0)
            d3d11_element.AlignedByteOffset = offset
            category_offsets[d3d11_element.Category] = (
                offset + int(d3d11_element.ByteWidth)
            )

        # 重算 CategoryStrideDict
        self.CategoryStrideDict = dict(category_offsets)

        return True

    def get_blendindices_layouts(self) -> list[tuple[int, str, str]]:
        """返回最终 BLENDINDICES 布局：(SemanticIndex, Format, ExtractSlot)。"""
        layouts = []
        for element in self.D3D11ElementList:
            if str(getattr(element, "SemanticName", "") or "").upper() != "BLENDINDICES":
                continue
            layouts.append((
                int(getattr(element, "SemanticIndex", 0) or 0),
                str(getattr(element, "Format", "") or "").upper(),
                str(getattr(element, "ExtractSlot", "") or ""),
            ))
        layouts.sort(key=lambda item: item[0])
        return layouts

    def get_blendindices_count_wwmi(self) -> int:
        """获取 WWMI 游戏的混合索引数量（给 WWMI 准备，其它逻辑不兼容）

        Nico:注意这个方法是给WWMI准备的,其它逻辑不兼容此方法,也不需要用到此方法
        Return the number of blend indices (VG channels) used by the game type.

        Historically code used a pattern like::
            num_vgs = 4
            if blendindices_element.Format == "R8_UINT":
                num_vgs = blendindices_element.ByteWidth

        This helper centralizes that logic. If the BLENDINDICES element is not
        present, or the format is not R8_UINT, default to 4.
        """
        elem = self.ElementNameD3D11ElementDict.get("BLENDINDICES", None)
        if elem is None:
            return 4
        try:
            if getattr(elem, 'Format', None) == "R8_UINT":
                bw = int(getattr(elem, 'ByteWidth', 0))
                return bw if bw > 0 else 4
        except Exception:
            pass
        return 4

    def get_total_structured_dtype(self) -> numpy.dtype:
        names = []
        formats = []
        offsets = []
        category_base_offsets = {}
        category_base_offset = 0
        for category_name in self.OrderedCategoryNameList:
            category_base_offsets[category_name] = category_base_offset
            category_base_offset += self.CategoryStrideDict.get(category_name, 0)

        for element_name in self.OrderedFullElementList:
            element = self.ElementNameD3D11ElementDict[element_name]
            np_type = FormatUtils.get_nptype_from_format(element.Format)
            format_len = int(element.ByteWidth / numpy.dtype(np_type).itemsize)
            names.append(element_name)
            formats.append(numpy.dtype((np_type, (format_len,))))
            offsets.append(
                category_base_offsets[element.Category]
                + int(element.AlignedByteOffset)
            )

        return numpy.dtype({
            "names": names,
            "formats": formats,
            "offsets": offsets,
            "itemsize": category_base_offset,
        })
