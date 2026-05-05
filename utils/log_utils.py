from .format_utils import Fatal
import sys
import io
import unicodedata


def _reconfigure_stdio_utf8():
    # Blender 外部进程和 Windows 终端编码经常不一致，这里统一切到 UTF-8 以免日志提示乱码。
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if not callable(reconfigure):
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


_reconfigure_stdio_utf8()


class LOG:
    _original_stdout = None
    _log_capture = None
    _is_collecting = False

    @classmethod
    def start_collecting(cls):
        _reconfigure_stdio_utf8()
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
    def _normalize_log_text(cls, text) -> str:
        if text is None:
            return ""

        normalized_chars = []
        for ch in str(text):
            if ch in ("\ufe0f", "\ufe0e", "\u200d"):
                continue

            # Windows + Blender 的控制台链路对 emoji 等符号兼容性很差，
            # 这里统一剥离掉，优先保证中文提示稳定可读。
            if unicodedata.category(ch) == "So":
                continue

            normalized_chars.append(ch)

        return "".join(normalized_chars)

    @classmethod
    def info(cls,input):
        if type(input) == list:
            for something in input:
                print(cls._normalize_log_text(something))
        else:
            print(cls._normalize_log_text(input))

    @classmethod
    def error(cls,input:str):
        raise Fatal(input)

    @classmethod
    def warning(cls,input:str):
        print("\033[33m" + "Warning: " + cls._normalize_log_text(input) + "\033[0m")
        cls.newline()

    @classmethod
    def debug(cls, input: str):
        print("\033[36m" + "Debug: " + cls._normalize_log_text(input) + "\033[0m")

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
        normalized_text = LOG._normalize_log_text(text)
        for output in self.outputs:
            output.write(normalized_text)

    def flush(self):
        for output in self.outputs:
            try:
                output.flush()
            except:
                pass

    def __getattr__(self, name):
        return getattr(self.outputs[0], name)
