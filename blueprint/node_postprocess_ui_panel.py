import bpy
import datetime
import os
import glob
import hashlib
import re
import shutil
import zipfile
from collections import OrderedDict

from .node_postprocess_base import SSMTNode_PostProcess_Base


class SSMT_OT_RefreshUIPanelExportSection(bpy.types.Operator):
    bl_idname = "ssmt.refresh_ui_panel_export_section"
    bl_label = "刷新已导出面板"
    bl_description = "不重新导出整个Mod，仅按当前面板目录重写已导出INI中的UI面板段"
    bl_options = {'REGISTER'}

    node_name: bpy.props.StringProperty(
        name="Node Name",
        description="关联的UI面板注入节点名称",
        default="",
    )

    def execute(self, context):
        tree = getattr(getattr(context, "space_data", None), "edit_tree", None)
        if not tree:
            self.report({'ERROR'}, "未找到当前蓝图编辑上下文")
            return {'CANCELLED'}

        node = tree.nodes.get(self.node_name) if self.node_name else tree.nodes.active
        if node is None or getattr(node, "bl_idname", "") != 'SSMTNode_PostProcess_UIPanel':
            self.report({'ERROR'}, "未找到UI面板注入后处理节点")
            return {'CANCELLED'}

        from ..common.global_config import GlobalConfig
        GlobalConfig.read_from_main_json_ssmt4()
        mod_export_path = str(GlobalConfig.path_generate_mod_folder() or "").strip()
        if not mod_export_path or not os.path.isdir(mod_export_path):
            self.report({'ERROR'}, "当前导出目录不存在，请先确认Generate Mod输出路径")
            return {'CANCELLED'}

        success, message = node.refresh_exported_ui_panel_section(mod_export_path)
        self.report({'INFO'} if success else {'ERROR'}, message)
        return {'FINISHED'} if success else {'CANCELLED'}


class SSMTNode_PostProcess_UIPanel(SSMTNode_PostProcess_Base):
    bl_idname = 'SSMTNode_PostProcess_UIPanel'
    bl_label = 'UI面板注入'
    bl_description = '将 UI 构造器（网页）生成的 INI 配置与 res/font 资源自动追加到导出配置表'

    UI_PANEL_MARKER_PREFIX = "; --- AUTO-APPENDED UI PANEL"
    # 这些段是共享定义，多个面板/后处理节点复用同一份，不做改名或重复追加。
    SHARED_SECTION_NAMES = frozenset({
        "Constants",
        "CustomShaderDraw",
        "CustomShaderFx",
    })

    panel_name: bpy.props.StringProperty(
        name="面板名称",
        default="UIPanel",
        description="面板标识，用于生成去重标记。多个 UI 面板注入节点必须使用不同的名称",
    )
    panel_folder: bpy.props.StringProperty(
        name="面板目录",
        subtype='DIR_PATH',
        default="",
        description="网页导出目录：包含 ui_config_*.txt（自动取日期最新的一份）以及 ui_assets_*.zip（自动解压到模组目录）或已解压的 res/、font/ 子目录",
    )
    def draw_buttons(self, context, layout):
        layout.prop(self, "panel_name")
        layout.prop(self, "panel_folder")
        refresh_row = layout.row(align=True)
        refresh_op = refresh_row.operator(
            "ssmt.refresh_ui_panel_export_section",
            text="刷新已导出面板",
            icon='FILE_REFRESH',
        )
        refresh_op.node_name = self.name
        layout.separator()
        layout.label(text="自动使用目录中最新的配置文件", icon='FILE_REFRESH')
        layout.label(text="（ui_config_*.txt 按日期取最新）")
        layout.label(text="检测到 ui_assets_*.zip 时自动取最新一份解压", icon='PACKAGE')
        layout.separator()
        layout.label(text="导出时会自动：", icon='EXPORT')
        layout.label(text="1. 追加面板 INI 段到配置表")
        layout.label(text="2. 解压/复制 res 与 font 资源到模组目录")

    # ------------------------------------------------------------------
    # 标记与块管理
    # ------------------------------------------------------------------

    def get_panel_marker(self) -> str:
        name = str(getattr(self, "panel_name", "") or "").strip() or "UIPanel"
        name = " ".join(name.splitlines()).strip() or "UIPanel"
        return f"{self.UI_PANEL_MARKER_PREFIX} {name} ---"

    @classmethod
    def _is_known_append_marker(cls, line: str) -> bool:
        stripped = str(line or "").strip()
        if stripped.startswith(cls.UI_PANEL_MARKER_PREFIX):
            return True
        return SSMTNode_PostProcess_Base.is_known_auto_appended_marker(stripped)

    def _split_own_block(self, content: str):
        """按自己的标记切分：返回 (保留内容, 本节点旧块)。

        旧块 = 从本节点标记（含标记前一行分隔线）到下一个已知追加标记或文件末尾。
        """
        lines = content.splitlines(keepends=True)
        marker = self.get_panel_marker()
        start_index = None
        for index, line in enumerate(lines):
            if marker in line:
                start_index = index
                break
        if start_index is None:
            return content, ""
        marker_index = start_index
        # 标记前一行通常是本块的分隔注释行
        if start_index > 0 and lines[start_index - 1].strip().startswith(";"):
            start_index -= 1
        end_index = len(lines)
        for index in range(marker_index + 1, len(lines)):
            if self._is_known_append_marker(lines[index]):
                end_index = index
                break
        kept = "".join(lines[:start_index] + lines[end_index:])
        removed = "".join(lines[start_index:end_index])
        return kept, removed

    @classmethod
    def _split_appended_tail(cls, content: str):
        lines = content.splitlines(keepends=True)
        for index, line in enumerate(lines):
            if cls._is_known_append_marker(line):
                return "".join(lines[:index]), "".join(lines[index:])
        return content, ""

    # ------------------------------------------------------------------
    # INI 解析
    # ------------------------------------------------------------------

    @staticmethod
    def parse_sections(text: str) -> "OrderedDict[str, list[str]]":
        sections = OrderedDict()
        current_section = None
        for raw_line in text.splitlines():
            line = raw_line.rstrip()
            stripped = line.strip()
            if stripped.startswith('[') and stripped.endswith(']') and len(stripped) > 2:
                current_section = stripped[1:-1]
                sections.setdefault(current_section, [])
            elif current_section is not None:
                sections[current_section].append(line)
        return sections

    @staticmethod
    def _extract_preamble(text: str) -> list[str]:
        preamble = []
        for raw_line in text.splitlines():
            stripped = raw_line.strip()
            if stripped.startswith('[') and stripped.endswith(']') and len(stripped) > 2:
                break
            preamble.append(raw_line.rstrip())
        return preamble

    @staticmethod
    def _strip_inline_comment(value: str) -> str:
        quote = ""
        for index, char in enumerate(str(value or "")):
            if char in "\"'":
                if not quote:
                    quote = char
                elif quote == char:
                    quote = ""
            elif char == ';' and not quote:
                return value[:index]
        return value

    @staticmethod
    def _extract_referenced_files(sections) -> list:
        """收集 INI 中引用的相对资源路径。

        覆盖 Resource 段的 filename = ./res/x.png、着色器段的 vs/ps = ./res/x.hlsl。
        仅保留带扩展名的相对路径；跳过 null（3DMigoto 着色器槽的"禁用"值）与绝对/协议路径。
        """
        referenced = []
        path_pattern = re.compile(r'^(?:filename|vs|ps|hs|ds|gs|cs)\s*=\s*(.+)$', re.IGNORECASE)
        for section_lines in sections.values():
            for line in section_lines:
                match = path_pattern.match(line.strip())
                if not match:
                    continue
                raw_path = SSMTNode_PostProcess_UIPanel._strip_inline_comment(match.group(1))
                raw_path = raw_path.strip().strip('"').strip("'")
                if not raw_path or raw_path.casefold() == 'null':
                    continue
                if re.match(r'^[A-Za-z][A-Za-z0-9+.-]*:', raw_path):
                    continue  # 协议/盘符绝对路径，不作为相对资源处理
                if not re.search(r'\.[A-Za-z0-9]+$', raw_path):
                    continue  # 非文件路径取值
                referenced.append(raw_path)
        return referenced

    @staticmethod
    def _resolve_reference(root_dir: str, ref_path: str) -> str:
        if os.path.isabs(ref_path):
            return os.path.normpath(ref_path)
        return os.path.normpath(os.path.join(root_dir, ref_path))

    @staticmethod
    def _resolve_bounded_path(root_dir: str, relative_path: str, description: str) -> str:
        if os.path.isabs(relative_path):
            raise ValueError(f"{description}包含非法绝对路径: {relative_path}")

        root_path = os.path.realpath(os.path.abspath(root_dir))
        target_path = os.path.realpath(os.path.abspath(os.path.join(root_path, relative_path)))
        try:
            is_within_root = os.path.normcase(os.path.commonpath([root_path, target_path])) == os.path.normcase(root_path)
        except ValueError:
            is_within_root = False
        if not is_within_root:
            raise ValueError(f"{description}包含非法路径: {relative_path}")
        return target_path

    # ------------------------------------------------------------------
    # 合并
    # ------------------------------------------------------------------

    def _merge_panel_sections(self, target_sections: "OrderedDict[str, list[str]]",
                              panel_sections: "OrderedDict[str, list[str]]",
                              merge_constants: bool = True):
        """Keep every panel section in the node's own bottom block."""
        return list(panel_sections.keys()), []

    def _present_block_markers(self):
        panel_name = str(getattr(self, "panel_name", "") or "UIPanel").strip() or "UIPanel"
        panel_id = hashlib.sha1(panel_name.encode("utf-8")).hexdigest()[:12]
        return (
            f"; --- SSMT UI PANEL PRESENT {panel_id} BEGIN ---",
            f"; --- SSMT UI PANEL PRESENT {panel_id} END ---",
        )

    def _replace_present_block(self, target_lines: list, panel_lines: list):
        begin_marker, end_marker = self._present_block_markers()
        cleaned = []
        inside_own_block = False
        for line in target_lines:
            stripped = str(line or "").strip()
            if stripped == begin_marker:
                inside_own_block = True
                continue
            if inside_own_block:
                if stripped == end_marker:
                    inside_own_block = False
                continue
            cleaned.append(line)

        # 兼容首次引入归属标记前生成的配置：删除完全相同的旧面板 Present 片段。
        old_block = list(panel_lines)
        if old_block:
            index = 0
            while index <= len(cleaned) - len(old_block):
                if cleaned[index:index + len(old_block)] == old_block:
                    del cleaned[index:index + len(old_block)]
                    continue
                index += 1

        if cleaned and cleaned[-1].strip():
            cleaned.append("")
        cleaned.append(begin_marker)
        cleaned.extend(panel_lines)
        cleaned.append(end_marker)
        target_lines[:] = cleaned

    # 匹配 global 声明中的变量名，如 "global persist $active = 1" 中的 "$active"
    _GLOBAL_DECLARATION_PATTERN = re.compile(
        r'^\s*global\s+(?:persist\s+)?(\$[A-Za-z0-9_]+)', re.IGNORECASE
    )

    @classmethod
    def _merge_global_lines(cls, target_lines: list, panel_lines: list):
        """合并面板 [Constants] 的 global 声明到配置表，以主配置表为准。

        主配置表（蓝图输出节点生成的 INI）已声明的变量，面板中的同名声明一律
        跳过——即使初始值不同，也保留主配置表的定义；面板独有的变量声明才追加。
        """
        existing_names = set()
        for line in target_lines:
            match = cls._GLOBAL_DECLARATION_PATTERN.match(line)
            if match:
                existing_names.add(match.group(1))
        for line in panel_lines:
            match = cls._GLOBAL_DECLARATION_PATTERN.match(line)
            if not match:
                continue
            var_name = match.group(1)
            if var_name in existing_names:
                continue
            target_lines.append(line)
            existing_names.add(var_name)

    # ------------------------------------------------------------------
    # 导出执行
    # ------------------------------------------------------------------

    # 网页导出的配置命名有两种：ui_config_<hash>_<毫秒时间戳> 与
    # ui_config_<hash>_<YYYYMMDD>_<HHMMSS>；手工放置的日期名（YYYY-MM-DD）也认。
    # 三种写法都换算成同一条时间轴（epoch 秒）再比较。
    _UI_CONFIG_MS_PATTERN = re.compile(
        r'_(\d{13})(?=\.(?:txt|ini|zip)$)', re.IGNORECASE
    )
    _UI_CONFIG_COMPACT_DATETIME_PATTERN = re.compile(
        r'_(\d{8})_(\d{6})(?=\.(?:txt|ini|zip)$)', re.IGNORECASE
    )
    _UI_CONFIG_DATE_PATTERN = re.compile(
        r'_(\d{4})-(\d{1,2})-(\d{1,2})(?=\.(?:txt|ini|zip)$)', re.IGNORECASE
    )

    @classmethod
    def _parse_config_time(cls, path_or_name):
        """解析配置表/压缩包名里的真实时间，返回 epoch 秒；解析不出返回 None。

        认三种写法：末尾 `_` + 13 位毫秒时间戳、`YYYYMMDD_HHMMSS`、
        `YYYY-MM-DD`。其余（`ui_config_副本.txt`、`ui_config_20261005.txt`
        这类）返回 None——由调用方决定忽略还是告警。
        """
        if not path_or_name:
            return None
        base = os.path.basename(str(path_or_name))
        match = cls._UI_CONFIG_MS_PATTERN.search(base)
        if match:
            return float(match.group(1)) / 1000.0
        match = cls._UI_CONFIG_COMPACT_DATETIME_PATTERN.search(base)
        if match:
            try:
                return datetime.datetime.strptime(
                    match.group(1) + match.group(2), "%Y%m%d%H%M%S").timestamp()
            except ValueError:
                return None
        match = cls._UI_CONFIG_DATE_PATTERN.search(base)
        if match:
            try:
                return datetime.datetime(
                    int(match.group(1)), int(match.group(2)), int(match.group(3))).timestamp()
            except ValueError:
                return None
        return None

    def _find_panel_ini_path(self) -> str:
        """在面板目录中查找时间最新的一份面板配置文件。

        仅匹配网页导出的 ui_config_*.txt/.ini（目录里可能有其他文件，严格限前缀）。
        候选按文件名自带的真实时间取最新（毫秒 id 与日期写法换算到同一时间轴）；
        文件名不含时间的候选不参与选择，但会在日志里点名。
        """
        folder = self._get_panel_folder()
        if not folder or not os.path.isdir(folder):
            raise ValueError("UI面板注入节点未设置有效的面板目录 (panel_folder)")

        candidates = []
        for pattern in ("ui_config_*.txt", "ui_config_*.ini"):
            candidates.extend(glob.glob(os.path.join(folder, pattern)))
        if not candidates:
            raise ValueError(f"面板目录中未找到 INI 配置文件: {folder}")

        ranked = []
        ignored = []
        for path in candidates:
            stamp = self._parse_config_time(path)
            if stamp is None:
                ignored.append(os.path.basename(path))
                continue
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0.0
            ranked.append((stamp, mtime, path))
        if ignored:
            print(f"  [UI面板注入] 已忽略（文件名不含时间，无法判断新旧）: "
                  f"{', '.join(sorted(ignored))}")
        if not ranked:
            raise ValueError(
                f"面板目录中没有可判断新旧的 INI 配置文件（已忽略: {', '.join(sorted(ignored))}）")

        ranked.sort(key=lambda item: (item[0], item[1]))
        best = ranked[-1]
        tied = [item for item in ranked if item[0] == best[0]]
        if len(tied) > 1:
            print(f"  [UI面板注入] 警告: {len(tied)} 份配置时间相同，取修改时间较晚的: "
                  f"{os.path.basename(best[2])}")
        return best[2]

    # 网页导出的资源压缩包命名：ui_assets_<毫秒时间戳>.zip / ui_assets_<YYYYMMDD>_<HHMMSS>.zip，
    # 目录里可能有其他文件，压缩包与配置文件一样严格限前缀；时间解析复用 _parse_config_time。
    # 压缩包内的配置文件条目只读取不解压，不作为模组资源写出
    _UI_CONFIG_ENTRY_PATTERN = re.compile(r'^ui_config_.*\.(?:txt|ini)$', re.IGNORECASE)

    def _get_panel_folder(self) -> str:
        folder = str(getattr(self, "panel_folder", "") or "").strip()
        if not folder:
            return ""
        try:
            return bpy.path.abspath(folder)
        except Exception:
            return folder

    def _find_ui_assets_zip_path(self) -> str:
        """在面板目录中查找时间最新的资源压缩包。

        仅匹配网页导出的 ui_assets_*.zip（目录里可能有其他文件，严格限前缀）。
        名字里能解析出时间的按真实时间比（毫秒 id 与日期写法同一时间轴），
        解析不出的（手动改名）排在后面、按修改时间比。
        """
        folder = self._get_panel_folder()
        if not folder or not os.path.isdir(folder):
            return ""
        candidates = glob.glob(os.path.join(folder, "ui_assets_*.zip"))
        if not candidates:
            return ""

        def sort_key(path):
            stamp = self._parse_config_time(path)
            try:
                mtime = os.path.getmtime(path)
            except OSError:
                mtime = 0.0
            if stamp is not None:
                return (1, stamp, mtime)
            return (0, mtime, mtime)

        return max(candidates, key=sort_key)

    def _load_panel_config_text(self, zip_path: str):
        """读取面板配置文本，返回 (文本内容, 来源描述)。

        松散配置与压缩包内配置条目都按文件名里的真实时间取最新——资源检查与
        注入必须基于「实际最新的配置表」，避免旧版松散配置（可能引用已被新版
        移除的资源，如 draw_2d_fx.hlsl）与新资源压缩包错配而误报缺文件。
        文件名不含时间的松散配置不参与比较（`_find_panel_ini_path` 会点名忽略）。
        目录与压缩包都没有配置时读取失败。
        """
        folder = self._get_panel_folder()
        if not folder or not os.path.isdir(folder):
            raise ValueError("UI面板注入节点未设置有效的面板目录 (panel_folder)")
        try:
            panel_ini_path = self._find_panel_ini_path()
        except ValueError:
            panel_ini_path = ""
        zip_entry_name = ""
        if zip_path:
            zip_entry_name = self._find_latest_config_entry_in_zip(zip_path)

        loose_ts = self._config_timestamp(panel_ini_path) if panel_ini_path else None
        zip_ts = self._config_timestamp(zip_entry_name) if zip_entry_name else None

        if panel_ini_path and zip_entry_name and loose_ts is not None and zip_ts is not None and zip_ts > loose_ts:
            # 压缩包内配置条目更新：以压缩包内的配置为准
            with zipfile.ZipFile(zip_path, 'r') as zfile:
                text = zfile.read(zip_entry_name).decode('utf-8-sig')
            return text, f"{os.path.basename(zip_path)} 内的 {zip_entry_name}"
        if panel_ini_path:
            with open(panel_ini_path, 'r', encoding='utf-8-sig') as f:
                return f.read(), os.path.basename(panel_ini_path)
        if zip_entry_name:
            with zipfile.ZipFile(zip_path, 'r') as zfile:
                text = zfile.read(zip_entry_name).decode('utf-8-sig')
            return text, f"{os.path.basename(zip_path)} 内的 {zip_entry_name}"
        raise ValueError(f"面板目录中未找到 INI 配置文件: {folder}")

    @classmethod
    def _config_timestamp(cls, path_or_name):
        """返回配置表名里的真实时间（epoch 秒），解析不出返回 None。"""
        return cls._parse_config_time(path_or_name)

    def _find_latest_config_entry_in_zip(self, zip_path: str) -> str:
        """在压缩包内查找时间最新的 ui_config_*.txt/.ini 条目（严格限前缀）。

        名字不含时间的条目点名忽略，不参与「新旧」判断。
        """
        with zipfile.ZipFile(zip_path, 'r') as zfile:
            entry_names = [
                str(name).replace('\\', '/')
                for name in zfile.namelist()
                if not str(name).endswith('/')
            ]

        def base_name(name):
            return name.rsplit('/', 1)[-1]

        candidates = [
            name for name in entry_names
            if self._UI_CONFIG_ENTRY_PATTERN.match(base_name(name))
        ]
        if not candidates:
            return ""

        ranked = []
        ignored = []
        for name in candidates:
            stamp = self._parse_config_time(base_name(name))
            if stamp is None:
                ignored.append(base_name(name))
                continue
            ranked.append((stamp, name))
        if ignored:
            print(f"  [UI面板注入] 已忽略压缩包内条目（文件名不含时间，无法判断新旧）: "
                  f"{', '.join(sorted(ignored))}")
        if not ranked:
            return ""
        ranked.sort(key=lambda item: item[0])
        return ranked[-1][1]

    @staticmethod
    def _normalize_reference_path(path) -> str:
        normalized = str(path or "").replace('\\', '/').strip()
        while normalized.startswith('./'):
            normalized = normalized[2:]
        return normalized

    def _extract_ui_assets_zip(self, zip_path: str, mod_export_path: str, panel_sections,
                               config_source: str = "") -> int:
        """把 ui_assets 压缩包解压到模组导出目录，返回解压的文件数。

        解压前校验 INI 引用的资源都在压缩包内；配置条目（ui_config_*.txt/.ini）
        不解压；拒绝越出目标目录的条目（zip slip）。
        """
        referenced = {
            self._normalize_reference_path(ref)
            for ref in self._extract_referenced_files(panel_sections)
        }
        target_root = os.path.realpath(os.path.abspath(mod_export_path))
        extracted_count = 0
        with zipfile.ZipFile(zip_path, 'r') as zfile:
            file_names = [
                str(name).replace('\\', '/')
                for name in zfile.namelist()
                if not str(name).endswith('/')
            ]
            normalized_entries = {self._normalize_reference_path(name) for name in file_names}
            missing = sorted(ref for ref in referenced if ref not in normalized_entries)
            if missing:
                source_hint = f" (配置来源: {config_source})" if config_source else ""
                raise ValueError(
                    f"UI面板资源压缩包缺少 INI 引用的文件: {', '.join(missing)} "
                    f"(压缩包: {os.path.basename(zip_path)}){source_hint}"
                )
            for info in zfile.infolist():
                if info.is_dir():
                    continue
                entry_name = str(info.filename).replace('\\', '/')
                if not entry_name or entry_name.endswith('/'):
                    continue
                if self._UI_CONFIG_ENTRY_PATTERN.match(entry_name.rsplit('/', 1)[-1]):
                    continue
                target_path = self._resolve_bounded_path(
                    target_root,
                    entry_name,
                    "UI面板资源压缩包",
                )
                os.makedirs(os.path.dirname(target_path), exist_ok=True)
                with zfile.open(info, 'r') as source, open(target_path, 'wb') as target:
                    shutil.copyfileobj(source, target)
                extracted_count += 1
        return extracted_count

    def _apply_panel_to_ini_file(self, target_ini_file, panel_sections):
        """Rewrite only this panel's independent bottom block in one INI file."""
        with open(target_ini_file, 'r', encoding='utf-8-sig') as f:
            target_text = f.read()
        kept_text, _removed = self._split_own_block(target_text)
        driver_block, kept_after_driver = self.split_anim_driver_block_content(kept_text)
        base_text, preserved_tail = self._split_appended_tail(kept_after_driver)
        target_preamble = self._extract_preamble(base_text)
        target_sections = self.parse_sections(base_text)

        append_order, warnings = self._merge_panel_sections(target_sections, panel_sections)
        for warning in warnings:
            print(f"  [??] {warning}")

        # Keep the base table as-is and append the whole panel block after any preserved tail.
        rebuilt_lines = list(target_preamble)
        if rebuilt_lines and rebuilt_lines[-1].strip():
            rebuilt_lines.append("")
        for section_name, section_lines in target_sections.items():
            rebuilt_lines.append(f"[{section_name}]")
            body = list(section_lines)
            # 去掉段尾空行再加一个分隔空行：重跑时不会一次多长一行空行（字节幂等）
            while body and not body[-1].strip():
                body.pop()
            rebuilt_lines.extend(body)
            rebuilt_lines.append("")

        new_block = self._build_appended_block(panel_sections, append_order)

        content_parts = []
        if driver_block:
            content_parts.append(driver_block.rstrip())

        rebuilt_body = "\n".join(rebuilt_lines).rstrip()
        if rebuilt_body:
            content_parts.append(rebuilt_body)

        if preserved_tail:
            content_parts.append(preserved_tail.rstrip())

        if new_block:
            content_parts.append("\n".join(new_block).rstrip())

        final_text = "\n\n".join(content_parts)
        if final_text:
            final_text += "\n"

        with open(target_ini_file, 'w', encoding='utf-8') as f:
            f.write(final_text)

    @staticmethod
    def _find_target_ini_files(mod_export_path):
        """按导出流程的工作空间名选择要写入的目标 INI（可能多份）。

        导出器统一把配置表写成 `<工作空间名>.ini`（zzmi.py:8442、
        efmi.py:3428 等）；WWMI 每个 draw_ib 一份 `<工作空间名>_<draw_ib>.ini`
        （wwmi_export.py:758），这些分文件属于同一个工作空间，必须全部写入。
        判定顺序：严格同名 → 忽略大小写同名 → `<工作空间名>_` 前缀全部
        → 目录里唯一 .ini → 都没有才报错并列出候选，不静默挑一个。

        返回 (路径列表, 错误说明)；成功时错误说明为空串。
        """
        ini_files = sorted(glob.glob(os.path.join(mod_export_path, "*.ini")))
        if not ini_files:
            return [], "导出目录中未找到任何.ini文件"

        workspace_name = ""
        try:
            from ..common.global_config import GlobalConfig
            workspace_name = str(GlobalConfig.get_workspace_name() or "").strip()
        except Exception:
            workspace_name = ""

        if workspace_name:
            stems = {
                path: os.path.splitext(os.path.basename(path))[0]
                for path in ini_files
            }
            exact = [path for path in ini_files if stems[path] == workspace_name]
            if exact:
                return exact, ""
            folded = workspace_name.casefold()
            ignore_case = [path for path in ini_files if stems[path].casefold() == folded]
            if ignore_case:
                return ignore_case, ""
            prefixed = [
                path for path in ini_files
                if os.path.basename(path).startswith(f"{workspace_name}_")
            ]
            if prefixed:
                return prefixed, ""

        if len(ini_files) == 1:
            return ini_files, ""

        names = ", ".join(os.path.basename(p) for p in ini_files)
        return [], (
            f"导出目录中存在多个INI文件，无法确定应写入哪一个"
            f"（工作空间: {workspace_name or '未知'}）: {names}")

    def execute_postprocess(self, mod_export_path):
        print(f"UI面板注入后处理节点开始执行，Mod导出路径: {mod_export_path}")

        target_ini_files, target_ini_error = self._find_target_ini_files(mod_export_path)
        if not target_ini_files:
            raise ValueError(target_ini_error)

        zip_path = self._find_ui_assets_zip_path()
        panel_text, config_source = self._load_panel_config_text(zip_path)
        panel_sections = self.parse_sections(panel_text)
        if not panel_sections:
            print(f"面板 INI 为空或无法解析: {config_source}")
            return

        # 有压缩包时资源直接解压到模组目录；否则退回松散文件复制
        if zip_path:
            extracted_count = self._extract_ui_assets_zip(
                zip_path, mod_export_path, panel_sections, config_source)
            print(f"UI面板资源压缩包已解压: {os.path.basename(zip_path)} "
                  f"({extracted_count} 个文件 -> {mod_export_path})")
        else:
            self._copy_referenced_files(panel_sections, self._get_panel_folder(), mod_export_path)

        for target_ini_file in target_ini_files:
            self._apply_panel_to_ini_file(target_ini_file, panel_sections)

        target_names = ", ".join(os.path.basename(p) for p in target_ini_files)
        print(f"UI面板配置已追加到 {len(target_ini_files)} 份配置表: {target_names} "
              f"(来源: {config_source}, 段数: {len(panel_sections)})")

    def refresh_exported_ui_panel_section(self, mod_export_path):
        """Refresh only the exported UI panel block without re-exporting the Mod."""
        target_ini_files, target_ini_error = self._find_target_ini_files(mod_export_path)
        if not target_ini_files:
            return False, target_ini_error

        try:
            zip_path = self._find_ui_assets_zip_path()
            panel_text, config_source = self._load_panel_config_text(zip_path)
            panel_sections = self.parse_sections(panel_text)
            if not panel_sections:
                return False, f"面板INI为空或无法解析: {config_source}"

            if zip_path:
                self._extract_ui_assets_zip(zip_path, mod_export_path, panel_sections, config_source)
            else:
                self._copy_referenced_files(panel_sections, self._get_panel_folder(), mod_export_path)
        except Exception as exc:
            return False, str(exc)

        try:
            for target_ini_file in target_ini_files:
                self._apply_panel_to_ini_file(target_ini_file, panel_sections)
        except Exception as exc:
            return False, f"写入UI面板段失败: {exc}"

        target_names = ", ".join(os.path.basename(p) for p in target_ini_files)
        return True, (
            f"已刷新 {len(target_ini_files)} 份配置表中的UI面板段: {target_names} "
            f"(来源: {config_source}, 段数: {len(panel_sections)})"
        )

    def _build_appended_block(self, panel_sections, append_order) -> list:
        marker = self.get_panel_marker()
        lines = [
            "; ==============================================================================",
            marker,
            "; ==============================================================================",
            "",
        ]
        for section_name in append_order:
            lines.append(f"[{section_name}]")
            lines.extend(panel_sections[section_name])
            lines.append("")
        return lines

    def _copy_referenced_files(self, panel_sections, panel_root: str, mod_export_path: str):
        referenced = self._extract_referenced_files(panel_sections)
        copied_count = 0
        for ref_path in referenced:
            source = self._resolve_bounded_path(panel_root, ref_path, "UI面板资源引用")
            if not os.path.isfile(source):
                raise ValueError(
                    f"UI面板引用的资源文件不存在: {ref_path} (查找位置: {source})。"
                    "请先解压网页导出的 ui_assets_*.zip 到面板目录。"
                )
            target = self._resolve_bounded_path(mod_export_path, ref_path, "UI面板资源目标")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            try:
                if not (os.path.exists(target) and os.path.samefile(source, target)):
                    import shutil
                    shutil.copy2(source, target)
                copied_count += 1
            except OSError as exc:
                raise ValueError(f"复制 UI面板资源失败 {ref_path}: {exc}") from exc
        if copied_count:
            print(f"UI面板资源复制完成: {copied_count} 个文件 -> {mod_export_path}")


classes = (
    SSMT_OT_RefreshUIPanelExportSection,
    SSMTNode_PostProcess_UIPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
