'''
How to log colored text on terminal:

BLACK = '\033[30m'
RED = '\033[31m'
GREEN = '\033[32m'
YELLOW = '\033[33m'
BLUE = '\033[34m'
MAGENTA = '\033[35m'
CYAN = '\033[36m'
WHITE = '\033[37m'
RESET = '\033[0m'

BOLD = '\033[1m'
UNDERLINE = '\033[4m'
BACKGROUND_YELLOW = '\033[43m'

print(BACKGROUND_YELLOW + BLACK + BOLD + "Warning: This is a warningg message" + RESET)
'''
from .format_utils import Fatal
from datetime import datetime
import sys
import io

class LOG:
    _original_stdout = None
    _log_capture = None
    _is_collecting = False

    @classmethod
    def start_collecting(cls):
        cls._log_capture = io.StringIO()
        cls._original_stdout = sys.stdout
        sys.stdout = _TeeOutput(cls._original_stdout, cls._log_capture)
        cls._is_collecting = True

    @classmethod
    def stop_collecting(cls):
        if cls._original_stdout is not None:
            sys.stdout = cls._original_stdout
            cls._original_stdout = None
        cls._is_collecting = False

    @classmethod
    def get_log_content(cls) -> str:
        if cls._log_capture:
            return cls._log_capture.getvalue()
        return ""

    @classmethod
    def clear_log(cls):
        if cls._log_capture:
            cls._log_capture = io.StringIO()

    @classmethod
    def info(cls,input):
        if type(input) == list:
            for something in input:
                print(something)
        else:
            print(input)

    @classmethod
    def error(cls,input:str):
        raise Fatal(input)

    @classmethod
    def warning(cls,input:str):
        print("\033[33m" + "Warning: " + input + "\033[0m")
        cls.newline()

    @classmethod
    def debug(cls, input: str):
        print("\033[36m" + "Debug: " + input + "\033[0m")

    @classmethod
    def newline(cls):
        print("\033[32m" + "-" * 110 + "\033[0m")

    @classmethod
    def save_to_text_editor(cls, text_name: str = "导出流程日志"):
        import bpy
        
        log_content = cls.get_log_content()
        if not log_content:
            return
        
        clean_content = cls._strip_ansi_codes(log_content)
        
        if text_name in bpy.data.texts:
            text_block = bpy.data.texts[text_name]
            text_block.clear()
        else:
            text_block = bpy.data.texts.new(text_name)
        
        text_block.write(clean_content)
        
        return text_name

    @classmethod
    def _strip_ansi_codes(cls, text: str) -> str:
        import re
        ansi_escape = re.compile(r'\x1B(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])')
        return ansi_escape.sub('', text)


class _TeeOutput:
    def __init__(self, *outputs):
        self.outputs = outputs

    def write(self, text):
        for output in self.outputs:
            output.write(text)

    def flush(self):
        for output in self.outputs:
            try:
                output.flush()
            except:
                pass

    def __getattr__(self, name):
        return getattr(self.outputs[0], name)
