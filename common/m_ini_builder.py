import hashlib
import os
import tempfile


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
