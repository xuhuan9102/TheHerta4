import base64
import glob
import json
import os
import shutil
import webbrowser
from pathlib import Path

import bpy

from .node_postprocess_base import SSMTNode_PostProcess_Base


def _update_web_panel_node_width(self, context):
    self.update_node_width([self.preset_file_path])


class SSMT_OT_PostProcess_OpenWebPanelBuilder(bpy.types.Operator):
    bl_idname = "ssmt_postprocess.open_web_panel_builder"
    bl_label = "打开网页面板构造器"
    bl_options = {'INTERNAL'}

    def execute(self, context):
        builder_path = SSMTNode_PostProcess_WebPanel.get_builder_html_path()
        if not builder_path:
            self.report({'ERROR'}, "未找到网页面板构造器 HTML 文件")
            return {'CANCELLED'}

        builder_uri = Path(builder_path).resolve().as_uri()

        try:
            if hasattr(os, "startfile"):
                os.startfile(builder_path)  # type: ignore[attr-defined]
            else:
                opened = webbrowser.open_new_tab(builder_uri)
                if not opened:
                    raise RuntimeError("默认浏览器未响应")
        except Exception:
            opened = webbrowser.open_new_tab(builder_uri)
            if not opened:
                self.report({'ERROR'}, "无法使用系统默认浏览器打开网页面板构造器")
                return {'CANCELLED'}

        return {'FINISHED'}


class SSMTNode_PostProcess_WebPanel(SSMTNode_PostProcess_Base):
    bl_idname = 'SSMTNode_PostProcess_WebPanel'
    bl_label = '网页面板'
    bl_description = '读取 UI 构造器预设，并在导出结束后把生成的面板配置追加到 ini 尾部'

    APPEND_MARKER_START = '; --- AUTO-APPENDED WEB PANEL PRESET START ---'
    APPEND_MARKER_END = '; --- AUTO-APPENDED WEB PANEL PRESET END ---'
    DEFAULT_TOOLSET_ASSETS = {'0.png', '1.png', '2.png', '3.png', 'draw_2d.hlsl'}

    create_cumulative_backup: bpy.props.BoolProperty(
        name="创建累积备份",
        description="是否在写入网页面板配置前创建备份文件",
        default=True,
    )  # type: ignore

    preset_file_path: bpy.props.StringProperty(
        name="预设文件",
        description="UI 构造器保存的 JSON 预设文件路径",
        subtype='FILE_PATH',
        default="",
        update=_update_web_panel_node_width,
    )  # type: ignore

    def draw_buttons(self, context, layout):
        layout.prop(self, "create_cumulative_backup")

        layout.prop(self, "preset_file_path", text="预设")

        layout.operator("ssmt_postprocess.open_web_panel_builder", text="打开网页面板构造器", icon='WORLD_DATA')

        preset_path = bpy.path.abspath(self.preset_file_path).strip() if self.preset_file_path else ""
        if preset_path:
            if os.path.isfile(preset_path):
                layout.label(text=os.path.basename(preset_path), icon='FILE_TICK')
            else:
                layout.label(text="预设文件不存在", icon='ERROR')
        else:
            layout.label(text="请先打开网页并保存预设", icon='INFO')

    def execute_postprocess(self, mod_export_path):
        print(f"[WebPanel] 开始执行，Mod导出路径: {mod_export_path}")

        preset_path = bpy.path.abspath(self.preset_file_path).strip() if self.preset_file_path else ""
        if not preset_path:
            print("[WebPanel] 未设置预设文件路径，跳过")
            return

        if not os.path.isfile(preset_path):
            print(f"[WebPanel] 预设文件不存在: {preset_path}")
            return

        ini_files = glob.glob(os.path.join(mod_export_path, "*.ini"))
        if not ini_files:
            print("[WebPanel] 路径中未找到任何 .ini 文件")
            return

        target_ini_file = ini_files[0]

        try:
            preset_data = self._load_preset_data(preset_path)
            generated_ini = self._extract_generated_ini(preset_data)
            if not generated_ini:
                print("[WebPanel] 预设中未找到生成后的面板配置，请先在网页中重新保存预设")
                return

            if self.create_cumulative_backup:
                self._create_cumulative_backup(target_ini_file, mod_export_path)

            copied_default_assets = self._copy_default_toolset_assets(preset_data, mod_export_path)
            written_res_assets, written_font_assets = self._write_embedded_assets(preset_data, mod_export_path)
            self._append_or_replace_panel_block(target_ini_file, generated_ini)

            print(
                f"[WebPanel] 已写入网页面板配置: {os.path.basename(target_ini_file)} | "
                f"默认资源 {copied_default_assets} 个, 嵌入资源 {written_res_assets} 个, 字体资源 {written_font_assets} 个"
            )
        except Exception as e:
            print(f"[WebPanel] 执行失败: {e}")
            import traceback
            traceback.print_exc()

    @classmethod
    def get_builder_html_path(cls):
        addon_root = Path(__file__).resolve().parent.parent
        toolset_dir = addon_root / 'Toolset'

        # 先尝试稳定文件名，再回退到“同前缀里最新的 HTML”，减少版本号变更带来的路径失效。
        stable_name = 'UI 构造器.html'
        stable_path = toolset_dir / stable_name
        if stable_path.is_file():
            return str(stable_path)

        # Ignore trailing version text in the builder filename and pick the newest match.
        candidates = [path for path in toolset_dir.glob('UI 构造器*.html') if path.is_file()]
        if candidates:
            candidates.sort(
                key=lambda path: (
                    path.name != stable_name,
                    -path.stat().st_mtime,
                    path.name,
                )
            )
            return str(candidates[0])

        return ""

    def _load_preset_data(self, preset_path):
        with open(preset_path, 'r', encoding='utf-8') as f:
            return json.load(f)

    def _extract_generated_ini(self, preset_data):
        generated = preset_data.get('generated') or {}
        candidate_values = (
            generated.get('iniText'),
            generated.get('ini'),
            preset_data.get('generatedIni'),
            preset_data.get('iniText'),
        )

        for value in candidate_values:
            if isinstance(value, str) and value.strip():
                return value.strip()

        return ""

    def _append_or_replace_panel_block(self, ini_file_path, generated_ini):
        with open(ini_file_path, 'r', encoding='utf-8') as f:
            content = f.read()

        panel_block = self._format_panel_block(generated_ini)

        start_marker = self.APPEND_MARKER_START
        end_marker = self.APPEND_MARKER_END
        start_index = content.find(start_marker)
        end_index = content.find(end_marker)

        if start_index != -1 and end_index != -1 and end_index >= start_index:
            end_index += len(end_marker)
            new_content = content[:start_index].rstrip() + "\n\n" + panel_block
        else:
            new_content = content.rstrip() + "\n\n" + panel_block

        with open(ini_file_path, 'w', encoding='utf-8') as f:
            f.write(new_content.rstrip() + "\n")

    def _format_panel_block(self, generated_ini):
        return (
            "; ==============================================================================\n"
            f"{self.APPEND_MARKER_START}\n"
            "; ==============================================================================\n\n"
            f"{generated_ini.strip()}\n\n"
            f"{self.APPEND_MARKER_END}\n"
        )

    def _copy_default_toolset_assets(self, preset_data, mod_export_path):
        addon_root = Path(__file__).resolve().parent.parent
        toolset_dir = addon_root / 'Toolset'
        copied_count = 0

        copied_count += self._copy_toolset_asset(toolset_dir, 'draw_2d.hlsl', Path(mod_export_path) / 'res' / 'draw_2d.hlsl')

        for component in preset_data.get('components', []):
            component_paths = component.get('paths') or {}
            embedded_assets = component.get('embeddedAssets') or {}

            for key, raw_rel_path in component_paths.items():
                rel_path = self._normalize_resource_rel_path(raw_rel_path)
                if not rel_path or key in embedded_assets:
                    continue

                asset_name = os.path.basename(rel_path)
                if asset_name not in self.DEFAULT_TOOLSET_ASSETS:
                    continue

                copied_count += self._copy_toolset_asset(
                    toolset_dir,
                    asset_name,
                    Path(mod_export_path) / 'res' / Path(rel_path)
                )

        return copied_count

    def _copy_toolset_asset(self, source_dir, asset_name, target_path):
        source_path = Path(source_dir) / asset_name
        if not source_path.is_file():
            return 0

        target_path.parent.mkdir(parents=True, exist_ok=True)
        if not target_path.exists():
            shutil.copy2(source_path, target_path)
            return 1

        return 0

    def _write_embedded_assets(self, preset_data, mod_export_path):
        resource_payloads = {}
        font_payloads = {}

        for component in preset_data.get('components', []):
            component_paths = component.get('paths') or {}
            embedded_assets = component.get('embeddedAssets') or {}

            for key, asset_info in embedded_assets.items():
                rel_path = self._normalize_resource_rel_path(component_paths.get(key, ''))
                data_url = self._extract_data_url(asset_info)
                if rel_path and data_url:
                    self._register_payload(resource_payloads, rel_path, data_url)

            if component.get('type') == 'sequence':
                for frame in component.get('frames', []):
                    rel_path = self._normalize_resource_rel_path(frame.get('path', ''))
                    data_url = self._extract_data_url(frame)
                    if rel_path and data_url:
                        self._register_payload(resource_payloads, rel_path, data_url)

        generated = preset_data.get('generated') or {}
        for raw_rel_path, data_url in (generated.get('fontAssets') or {}).items():
            rel_path = self._normalize_font_rel_path(raw_rel_path)
            if rel_path and isinstance(data_url, str) and data_url.startswith('data:'):
                self._register_payload(font_payloads, rel_path, data_url)

        written_res_assets = self._write_payload_group(resource_payloads, Path(mod_export_path) / 'res')
        written_font_assets = self._write_payload_group(font_payloads, Path(mod_export_path) / 'font')
        return written_res_assets, written_font_assets

    def _write_payload_group(self, payload_map, root_dir):
        written_count = 0
        for rel_path, data_url in payload_map.items():
            target_path = root_dir / Path(rel_path)
            target_path.parent.mkdir(parents=True, exist_ok=True)
            self._write_data_url_file(data_url, target_path)
            written_count += 1
        return written_count

    def _register_payload(self, payload_map, rel_path, data_url):
        existing = payload_map.get(rel_path)
        if existing and existing != data_url:
            raise ValueError(f"同一路径存在不同嵌入资源，无法确定写入内容: {rel_path}")
        payload_map[rel_path] = data_url

    def _write_data_url_file(self, data_url, target_path):
        if not isinstance(data_url, str) or not data_url.startswith('data:'):
            raise ValueError(f"不是有效的 Data URL: {target_path}")

        _, encoded = data_url.split(',', 1)
        binary = base64.b64decode(encoded)
        with open(target_path, 'wb') as f:
            f.write(binary)

    def _extract_data_url(self, value):
        if isinstance(value, str) and value.startswith('data:'):
            return value

        if isinstance(value, dict):
            for key in ('dataUrl', 'data_url', 'preview'):
                candidate = value.get(key)
                if isinstance(candidate, str) and candidate.startswith('data:'):
                    return candidate

        return ""

    def _normalize_resource_rel_path(self, rel_path):
        normalized = str(rel_path or '').replace('\\', '/').strip()
        while normalized.startswith('./'):
            normalized = normalized[2:]
        if normalized.lower().startswith('res/'):
            normalized = normalized[4:]
        return normalized.lstrip('/')

    def _normalize_font_rel_path(self, rel_path):
        normalized = str(rel_path or '').replace('\\', '/').strip()
        while normalized.startswith('./'):
            normalized = normalized[2:]
        if normalized.lower().startswith('font/'):
            normalized = normalized[5:]
        return normalized.lstrip('/')


classes = (
    SSMT_OT_PostProcess_OpenWebPanelBuilder,
    SSMTNode_PostProcess_WebPanel,
)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
