import bpy
import subprocess
import sys
import importlib.util

REQUIRED_DEPS = ['scipy']

def is_dependency_installed(module_name):
    spec = importlib.util.find_spec(module_name)
    return spec is not None

class TT_OT_ensure_dependencies(bpy.types.Operator):
    bl_idname = "toolkit.tt_ensure_dependencies"
    bl_label = "检查并安装依赖项"
    bl_description = "自动检查并安装插件所需的Python库 (如 scipy)。安装过程可能需要几分钟。"
    _timer = None
    installation_finished = False
    installation_success = False
    installation_message = ""
    
    def install_deps(self):
        try:
            python_exe = sys.executable
            command = [python_exe, "-m", "pip", "install", "--user", "--upgrade", "--no-cache-dir", *REQUIRED_DEPS]
            subprocess.run(command, capture_output=True, text=True, check=True, encoding='utf-8', errors='ignore')
            self.installation_success = True
            self.installation_message = f"成功安装 {', '.join(REQUIRED_DEPS)}！\n\n请务必【重启Blender】以使新库生效。"
        except Exception as e:
            self.installation_success = False
            error_msg = str(e.stderr) if hasattr(e, 'stderr') else str(e)
            self.installation_message = f"安装失败！\n\n错误信息: {error_msg}\n\n解决方案：\n1. 确认网络连接正常。\n2. 【以管理员身份】重新启动Blender重试。"
        self.installation_finished = True
        return 0.1
    
    def modal(self, context, event):
        if event.type == 'TIMER':
            if self.installation_finished:
                self.show_popup()
                context.window_manager.event_timer_remove(self._timer)
                return {'FINISHED'}
        return {'PASS_THROUGH'}
    
    def execute(self, context):
        if all(is_dependency_installed(dep) for dep in REQUIRED_DEPS):
            self.report({'INFO'}, "所有依赖项均已安装。")
            return {'FINISHED'}
        self.installation_finished = False
        import threading
        threading.Thread(target=self.install_deps).start()
        self._timer = context.window_manager.event_timer_add(0.5, window=context.window)
        context.window_manager.modal_handler_add(self)
        self.report({'INFO'}, f"开始安装 {', '.join(REQUIRED_DEPS)}... 请稍候。")
        return {'RUNNING_MODAL'}
    
    def show_popup(self):
        def draw_popup(menu, context):
            for line in self.installation_message.split('\n'): 
                menu.layout.label(text=line)
        bpy.context.window_manager.popup_menu(draw_popup, title="安装结果", icon='INFO')


tt_dependency_check_list = (
    TT_OT_ensure_dependencies,
)
