"""离线编译检查 3Dmigoto 的自定义着色器。

为什么要自己造一个：机器上没有 fxc.exe / dxc.exe，但 `D3DCompiler_47.dll` 就在
游戏目录和 System32 里——3Dmigoto 编译 mod 着色器用的也正是它。这里用 ctypes
直接调 `D3DCompile`，把每个入口文件连它的 `#include` 一起展开后再编译，
于是能在**不进游戏**的前提下拿到和 fxc 同类别的报错（语法、未声明标识符、
寄存器冲突等）。

用法：
    python tools\\compile-hlsl.py --ini <mod 的 ini> [--root <res 根目录>]

入口与 profile 从 ini 里 `cs = ...` / `ps = ...` / `vs = ...` / `gs = ...` 读，
一个文件被多个阶段引用时按每个阶段各编一次。
"""

from __future__ import annotations

import argparse
import ctypes
import os
import re
import sys

PROFILE_OF_STAGE = {
    "cs": "cs_5_0",
    "ps": "ps_5_0",
    "vs": "vs_5_0",
    "gs": "gs_5_0",
    "hs": "hs_5_0",
    "ds": "ds_5_0",
}

STAGE_RE = re.compile(r"^\s*(cs|ps|vs|gs|hs|ds)\s*=\s*([^\s;]+\.hlsl)\s*$", re.I | re.M)
INCLUDE_RE = re.compile(r'^\s*#include\s+"([^"]+)"\s*$', re.M)


def load_compiler():
    """找一个可用的 d3dcompiler_47.dll 并返回其 D3DCompile。"""
    candidates = [
        r"K:\SSMT-Package-master\3Dmigoto\ZZZ\d3dcompiler_47.dll",
        os.path.join(os.environ.get("SystemRoot", r"C:\Windows"), "System32", "D3DCompiler_47.dll"),
    ]
    last = None
    for path in candidates:
        if not os.path.exists(path):
            continue
        try:
            dll = ctypes.WinDLL(path)
            return dll.D3DCompile
        except OSError as exc:            # 位数不匹配等情况
            last = exc
    raise RuntimeError("找不到可用的 d3dcompiler_47.dll: %s" % last)


def expand(path, seen=None, depth=0):
    """把 `#include "..."` 递归展开成单文件源码。

    只用相对路径（这套着色器全部是相对包含），所以不需要 ID3DInclude。
    同一文件在一条包含链上重复出现时只展开一次，避免环形包含。
    """
    seen = seen or set()
    key = os.path.normcase(os.path.abspath(path))
    if key in seen or depth > 32:
        return ""
    seen = seen | {key}
    with open(path, encoding="utf-8-sig") as handle:
        text = handle.read()
    base = os.path.dirname(path)
    out = []
    for line in text.splitlines():
        match = INCLUDE_RE.match(line)
        if not match:
            out.append(line)
            continue
        child = os.path.normpath(os.path.join(base, match.group(1)))
        if os.path.exists(child):
            out.append(expand(child, seen, depth + 1))
        else:
            out.append("// [compile-hlsl] 找不到包含文件: %s" % child)
    return "\n".join(out)


def read_blob(blob):
    """ID3DBlob → bytes（vtable 第 3、4 项是 GetBufferPointer / GetBufferSize）。"""
    if not blob:
        return b""
    vtbl = ctypes.cast(blob, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    get_ptr = ctypes.CFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(vtbl[3])
    get_size = ctypes.CFUNCTYPE(ctypes.c_size_t, ctypes.c_void_p)(vtbl[4])
    ptr = get_ptr(blob)
    size = get_size(blob)
    return ctypes.string_at(ptr, size) if ptr and size else b""


def compile_one(D3DCompile, source, filename, stage, entry="main"):
    target = PROFILE_OF_STAGE[stage]
    src = source.encode("utf-8")
    code = ctypes.c_void_p()
    err = ctypes.c_void_p()
    hr = D3DCompile(
        src, ctypes.c_size_t(len(src)), filename.encode("utf-8"),
        None, None, entry.encode("ascii"), target.encode("ascii"),
        0, 0, ctypes.byref(code), ctypes.byref(err),
    )
    message = read_blob(err.value).decode("utf-8", "replace").strip()
    if err.value:
        ctypes.cast(err.value, ctypes.POINTER(ctypes.c_void_p)).contents  # 保活
    return hr, message


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--ini", required=True)
    parser.add_argument("--root", default=None, help="res 根目录，默认取 ini 所在目录")
    args = parser.parse_args(argv)

    root = args.root or os.path.dirname(os.path.abspath(args.ini))
    with open(args.ini, encoding="utf-8-sig") as handle:
        ini = handle.read()

    jobs = []
    for stage, rel in STAGE_RE.findall(ini):
        stage = stage.lower()
        path = os.path.normpath(os.path.join(root, rel.replace("/", os.sep)))
        if os.path.exists(path) and (path, stage) not in jobs:
            jobs.append((path, stage))

    if not jobs:
        print("没有从 ini 里找到任何 cs/ps/vs/gs/hlsl 引用")
        return 2

    D3DCompile = load_compiler()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    def entry_candidates(source):
        names = re.findall(r"(?m)^\s*(?:void|float[234]?|half[234]?)\s+(\w+)\s*\(", source)
        out = ["main"]
        for n in names:
            if n not in out and n.lower().endswith("main"):
                out.append(n)
        for n in names:
            if n not in out:
                out.append(n)
        return out[:8]
    failed = 0
    for path, stage in jobs:
        source = expand(path)
        hr, message, used = 0, "", "main"
        for cand in entry_candidates(source):
            hr, message = compile_one(D3DCompile, source, path, stage, cand)
            used = cand
            if hr >= 0 and not message.startswith("error"):
                break
        if hr < 0 or message.startswith("error"):
            failed += 1
            first = next((ln for ln in message.splitlines() if "error" in ln.lower()), message[:200])
            print("FAIL  %-52s %s\n        %s" % (os.path.basename(path), PROFILE_OF_STAGE[stage], first))
        else:
            print("ok    %-46s %-7s entry=%s" % (os.path.basename(path), PROFILE_OF_STAGE[stage], used))

    print("\n%d 个入口，失败 %d" % (len(jobs), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
