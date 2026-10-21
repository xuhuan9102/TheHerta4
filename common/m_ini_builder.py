import hashlib
import os
import re
import tempfile

# 导出段登记表（纯 Python、无 bpy 依赖；它不 import 本项目任何模块，故不会循环 import）。
# 写盘前用它把「段 -> 所属 draw_ib」登记下来，供导出后的条件判断节点反查。
try:
    from . import export_section_registry as _section_registry
except ImportError:  # pragma: no cover - 仅 tests/ 里按文件路径加载本模块（fake 包 __path__ 为空）时走到
    # tests/ 部分用例用 spec_from_file_location 把本文件当成独立模块加载，相对导入找不到同级模块，
    # 这里按文件路径兜底加载；按固定模块名复用同一实例，避免同一次测试里出现两份登记表。
    import importlib.util as _importlib_util
    import sys as _sys

    _REGISTRY_MODULE_NAME = "theherta4_export_section_registry"
    _section_registry = _sys.modules.get(_REGISTRY_MODULE_NAME)
    if _section_registry is None:
        _registry_spec = _importlib_util.spec_from_file_location(
            _REGISTRY_MODULE_NAME,
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "export_section_registry.py"),
        )
        _section_registry = _importlib_util.module_from_spec(_registry_spec)
        _sys.modules[_REGISTRY_MODULE_NAME] = _section_registry
        _registry_spec.loader.exec_module(_section_registry)



class M_SectionType:
    NameSpace = "NameSpace"
    Present = "Present"
    Constants = "Constants"
    Key = "Key"

    TextureOverrideIB = "TextureOverrideIB"
    TextureOverrideVB = "TextureOverrideVB"
    TextureOverrideVertexLimitRaise = "TextureOverrideVertexLimitRaise"
    TextureOverrideTexture = "TextureOverrideTexture"
    TextureOverrideGeneral = "TextureOverrideGeneral" # 除VB IB VLR之外的通用类型
    TextureOverrideShapeKeys = "TextureOverrideShapeKeys" # Unreal形态键专用，目前只测试了WWMI

    # 纯 ShaderOverride 注册段（注册 filter_index 供 ini 条件判 pass 用；
    # EFMI 阴影 pass 单投射源门控靠它让 `if ps != <号>` 可判）
    ShaderOverride = "ShaderOverride"

    IBSkip = "IBSkip"

    ResourceBuffer = "ResourceBuffer"
    ResourceTexture = "ResourceTexture"
    ResourceAndTextureOverride_Texture = "ResourceAndTextureOverride_Texture" # 用于Hash风格贴图中方便Resource和TextureOverride放在一起
    ResourceModInfo = "ResourceModInfo" # WWMI里的Mod作者信息部分
    ResourceShapeKeysOverride = "ResourceShapeKeysOverride"
    ResourceSkeletonOverride = "ResourceSkeletonOverride"

    VertexShaderCheck = "VertexShaderCheck"

    CreditInfo = "CreditInfo"
    CommandList = "CommandList"

    # 跨IB相关Section类型
    CrossIBPresent = "CrossIBPresent"
    ResourceID = "ResourceID"
    CrossIBShaderOverride = "CrossIBShaderOverride"
    CrossIBTextureOverride = "CrossIBTextureOverride"

    # 着色器替换相关Section类型
    ShaderReplace = "ShaderReplace"

    # EFMI 骨骼合并（Merged Skeleton）相关Section类型
    MergedSkeleton = "MergedSkeleton"


class M_IniSection:
    def __init__(self,section_type:M_SectionType) -> None:
        self.SectionType:M_SectionType = section_type
        self.SectionName = "" # Constants和Present可以通过设置这个，来省去添加一行[名称]
        self.SectionLineList = []
        # 构造时快照当前 draw_ib 上下文（导出器用 export_section_registry.draw_ib_context 包裹），
        # 用于导出后按 draw_ib 反查本节的所有段；没有上下文时为空字符串。
        self.owner_draw_ib:str = _section_registry.current_draw_ib()
    
    def empty(self)->bool:
        '''
        调用此方法判断是否为空，或换行，如果内容出现不为空或换行的，就说明不是空的返回False，
        否则一直遍历完都没找到实际有效内容则返回True代表是空的
        '''
        for line in self.SectionLineList:
            if line != "" and line != "\n":
                return False
        return True

    def append(self,line:str):
        self.SectionLineList.append(line)

    def extend(self,lines) -> None:
        '''
        append multiple lines.
        '''
        for line in lines:
            self.append(line)

    def new_line(self):
        '''
        append a new line.
        '''
        self.SectionLineList.append("")


class M_IniBuilder:
    def __init__(self):
        self.line_list = []
        self.ini_section_list:list[M_IniSection] = []

        # 用于控制是否是第一次出现这个名字的Section
        self.ini_section_name_set:set = set()
    
    def clear(self):
        self.line_list.clear()
        self.ini_section_list.clear()
        self.ini_section_name_set.clear()

    def __append_section_line(self,ini_section_type:M_SectionType):
        '''
        Only can be legally call in M_IniBuilder.
        '''
        for ini_section in self.ini_section_list:
            if ini_section.SectionType == ini_section_type:
                section_name_exists = ini_section.SectionName in self.ini_section_name_set
                if not section_name_exists:
                    self.line_list.append("\n;MARK:" + ini_section_type + "\n")

                # SectionName不为空的时候才会自动补SectionName，否则由用户控制
                if ini_section.SectionName != "" and not section_name_exists:
                    self.line_list.append("[" + ini_section.SectionName + "]\n")
                    self.ini_section_name_set.add(ini_section.SectionName)

                # 添加Section的内容
                for line in ini_section.SectionLineList:
                    self.line_list.append(line + "\n")
                

    def append_section(self,m_inisection:M_IniSection):
        # 先判断是否为空，如果为空就不往里放了
        if not m_inisection.empty():
            self.ini_section_list.append(m_inisection)

    def _collect_section_entries(self):
        '''
        收集本 builder 所有段，返回 [(段名, 段类型, 所属draw_ib), ...]，供导出段登记表使用。

        段名有两个来源，都要收：
        1. section.SectionName：Constants / Present 这类通过属性省掉 "[名称]" 行的段；
        2. 段体里以 [xxx] 开头的行：很多段把段头写在 SectionLineList 里，例如 hash 风格
           贴图的 [Resource_Texture_<贴图hash>] / [TextureOverride_<贴图hash>]，
           只按段名关键词匹配必然漏掉它们。

        同一 (段名, 段类型, draw_ib) 由 commit_ini 去重，这里只负责按顺序收集。
        '''
        entries = []
        for section in self.ini_section_list:
            names = []
            if section.SectionName:
                names.append(section.SectionName)
            for line in section.SectionLineList or []:
                m = re.match(r"^\s*\[([^\]]+)\]\s*$", line or "")
                if m:
                    names.append(m.group(1).strip())
            for name in names:
                # getattr 兜底：tests/ 里有与 M_IniSection 同 API 的假段对象会被塞进真 builder。
                entries.append((name, section.SectionType, getattr(section, "owner_draw_ib", "")))
        return entries

    @staticmethod
    def _atomic_write_lines(config_ini_path: str, lines: list[str]) -> None:
        """Write a UTF-8/LF INI without ever exposing a truncated destination."""
        destination = os.path.abspath(config_ini_path)
        destination_dir = os.path.dirname(destination)
        os.makedirs(destination_dir, exist_ok=True)
        fd, temp_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(destination)}.",
            suffix=".tmp",
            dir=destination_dir,
        )
        descriptor_open = True
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
                descriptor_open = False
                handle.writelines(lines)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, destination)
            temp_path = ""
        finally:
            if descriptor_open:
                os.close(fd)
            if temp_path and os.path.exists(temp_path):
                try:
                    os.remove(temp_path)
                except OSError:
                    pass
    
    def save_to_file_not_reorder(self,config_ini_path:str):
        '''
        不重新排序的版本，方便我们的ini格式和其它工具生成的ini格式进行对比。
        '''
        # A builder may be previewed/saved more than once. Emission state belongs
        # to one save operation; retaining it duplicates every line on the next save.
        self.line_list.clear()
        self.ini_section_name_set.clear()
        for ini_section in self.ini_section_list:
            section_name_exists = ini_section.SectionName in self.ini_section_name_set
            # if not section_name_exists:
            #     self.line_list.append("\n;MARK:" + ini_section.SectionType + "\n")
            
            # SectionName不为空的时候才会自动补SectionName，否则由用户控制
            if ini_section.SectionName != "" and not section_name_exists:
                self.line_list.append("[" + ini_section.SectionName + "]\n")
                self.ini_section_name_set.add(ini_section.SectionName)

            # 添加Section的内容
            for line in ini_section.SectionLineList:
                self.line_list.append(line + "\n")

        # Add tools credit.
        
        # self.line_list.append("\n\n; Mod generated by TheHerta.\n")
        
        # Add sha256 to verify if ini need to overwrite.
        sha256 = self.calculate_sha256_for_list(self.line_list)
        # print("sha256: " + sha256)

        # Add after sha256 calculation.
        self.line_list.append("\n;sha256=" + sha256 + "\n\n") 


        # 写盘前把本节所有段整批登记到导出段登记表（供条件判断节点按 draw_ib 反查）。
        # 注意：sha256 相同、跳过写文件时同样要登记 —— 内容没变不代表这次导出的 draw_ib 归属没变，
        # 而登记表用的是整批覆盖语义，漏登记会让后处理查到上一轮甚至空的数据。
        _section_registry.commit_ini(config_ini_path, self._collect_section_entries())

        # Read ini and find sha256, if not same then write ini, if same do nothing.
        ini_sha256 = self.get_sha256_from_ini(config_ini_path)
        # print("old ini sha256: " + ini_sha256)
        # print("new ini sha256: " + sha256)
        if ini_sha256 != sha256:
            print("Write new mod ini because sha256 is not same.")
            self._atomic_write_lines(config_ini_path, self.line_list)
        else:
            print("Skip write mod ini becuase sha256 is same, ini file content not changed so we are safe to skip.")
        pass

    def save_to_file(self,config_ini_path:str):
        self.line_list.clear()
        self.ini_section_name_set.clear()
        self.__append_section_line(M_SectionType.CrossIBPresent)
        
        self.__append_section_line(M_SectionType.ResourceID)

        self.__append_section_line(M_SectionType.NameSpace)

        self.__append_section_line(M_SectionType.Key)

        self.__append_section_line(M_SectionType.Constants)

        self.__append_section_line(M_SectionType.MergedSkeleton)

        self.__append_section_line(M_SectionType.Present)


        self.__append_section_line(M_SectionType.IBSkip)


        self.__append_section_line(M_SectionType.TextureOverrideVertexLimitRaise)

        self.__append_section_line(M_SectionType.TextureOverrideVB)

        self.__append_section_line(M_SectionType.TextureOverrideIB)

        self.__append_section_line(M_SectionType.TextureOverrideShapeKeys)

        self.__append_section_line(M_SectionType.TextureOverrideGeneral)

        self.__append_section_line(M_SectionType.ShaderOverride)

        self.__append_section_line(M_SectionType.CommandList)

        self.__append_section_line(M_SectionType.ResourceShapeKeysOverride)

        self.__append_section_line(M_SectionType.ResourceSkeletonOverride)

        self.__append_section_line(M_SectionType.ResourceBuffer)

        self.__append_section_line(M_SectionType.ResourceTexture)

        self.__append_section_line(M_SectionType.TextureOverrideTexture)

        self.__append_section_line(M_SectionType.ResourceAndTextureOverride_Texture)

        self.__append_section_line(M_SectionType.ResourceModInfo)


        self.__append_section_line(M_SectionType.VertexShaderCheck)
        
        self.__append_section_line(M_SectionType.CreditInfo)

        self.__append_section_line(M_SectionType.ShaderReplace)


        # Add tools credit.
        
        # self.line_list.append("\n\n; Mod generated by TheHerta.\n")

        # Add sha256 to verify if ini need to overwrite.
        sha256 = self.calculate_sha256_for_list(self.line_list)
        # print("sha256: " + sha256)

        # Add after sha256 calculation.
        self.line_list.append("\n;sha256=" + sha256 + "\n\n") 


        # 写盘前把本节所有段整批登记到导出段登记表（供条件判断节点按 draw_ib 反查）。
        # 注意：sha256 相同、跳过写文件时同样要登记 —— 内容没变不代表这次导出的 draw_ib 归属没变，
        # 而登记表用的是整批覆盖语义，漏登记会让后处理查到上一轮甚至空的数据。
        _section_registry.commit_ini(config_ini_path, self._collect_section_entries())

        # Read ini and find sha256, if not same then write ini, if same do nothing.
        ini_sha256 = self.get_sha256_from_ini(config_ini_path)
        # print("old ini sha256: " + ini_sha256)
        # print("new ini sha256: " + sha256)
        if ini_sha256 != sha256:
            print("Write new mod ini because sha256 is not same.")
            self._atomic_write_lines(config_ini_path, self.line_list)
        else:
            print("Skip write mod ini becuase sha256 is same, ini file content not changed so we are safe to skip.")

    def calculate_sha256_for_list(self,string_list):
        # 创建一个新的sha256哈希对象
        sha256_hash = hashlib.sha256()
        
        # 将列表中的每个字符串编码为字节，并更新哈希对象
        for line in string_list:
            byte_line = line.encode('utf-8')
            sha256_hash.update(byte_line)
        
        # 获取十六进制格式的哈希值
        hex_digest = sha256_hash.hexdigest()
        
        return hex_digest

    def get_sha256_from_ini(self,ini_file_path:str):
        """
        读取指定路径的INI文件，找到以;sha256=开头的行，
        并返回该行中;sha256=后面的内容（去除前后空白字符）。
        找不到或者出错则返回空字符串
        """
        sha256_value = ""
        
        try:
            with open(ini_file_path, 'r', encoding='utf-8') as file:
                for line in file:
                    # 去除行首尾的空白字符
                    stripped_line = line.strip()
                    
                    # 检查行是否以";sha256="开头
                    if stripped_line.startswith(";sha256="):
                        # 提取";sha256="后面的内容，并去除前后空白字符
                        sha256_value = stripped_line[len(";sha256="):].strip()
                        break  # 找到后停止循环，假设只有一行匹配
                        
        except FileNotFoundError:
            print(f"The file at {ini_file_path} was not found.")
            return ""
        except Exception as e:
            print(f"An error occurred: {e}")
            return ""
            
        return sha256_value
