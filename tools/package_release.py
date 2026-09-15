# -*- coding: utf-8 -*-
"""手动打包 TheHerta4 发布包（本仓库的发布方式：**不使用 CI 自动打包**）。

背景与约定见 docs/release-packaging.md：GitHub Actions 的 Release Package 工作流
（.github/workflows/release.yml）**不采用**（账号计费锁导致 job 起不来，历史上
v4.4.45/46/47 的 tag 触发的运行全部 failure），发布包一律本地手工打。

本脚本做的事等价于那次 CI 的运行逻辑：
  1. 只取**被 git 跟踪**的文件（等价 CI 的 actions/checkout）+ 按 release.yml 的
     --exclude 规则过滤（脚本直接解析该文件，保证两边永不脱节）；
  2. 套一层外层目录 ``TheHerta4/``（Blender 可直接 Install from Disk 安装 zip）；
  3. 输出到 ``dist/TheHerta4-<版本点号换横线>.zip`` 并打印 字节数/条目数/sha256，
     这三项直接抄进同目录的 ``*-更新说明.md``。

用法：
    python tools/package_release.py                 # 版本号读 __init__.py 的 bl_info
    python tools/package_release.py --version 4.4.47
    python tools/package_release.py --out-dir %TEMP%\\th4 --check   # 只比规则，不写包
"""
import argparse
import hashlib
import io
import os
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from fnmatch import fnmatch

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORKFLOW = os.path.join(REPO_ROOT, ".github", "workflows", "release.yml")

# release.yml 读不到时的兜底（与 2026-09-15 的工作流一致）
FALLBACK_EXCLUDE_NAMES = {
    ".git", ".github", ".agents", ".trae", ".venv", ".codex_benchmarks", ".codegraph",
    ".dbg", ".kunsdd", "__pycache__", "docs", "dist", "DIST", "Logs", "references",
    "reports", "test", "tests", "tools", "theherta4_updater",
}
FALLBACK_EXCLUDE_GLOBS = [
    "*.pyc", "*.pyo", "*.bak-*", "*.GOOD-*", "*.log", "*.md",
    ".gitignore", "skills-lock.json", "README.md", "README_DEV.md", "requirements.txt",
]


def read_excludes():
    """从 release.yml 解析 --exclude 名单：无斜杠=任意同名路径组件，其余按 basename 通配。"""
    try:
        text = io.open(WORKFLOW, encoding="utf-8").read()
    except OSError:
        return FALLBACK_EXCLUDE_NAMES, FALLBACK_EXCLUDE_GLOBS
    pats = re.findall(r"--exclude='([^']+)'", text)
    if not pats:
        return FALLBACK_EXCLUDE_NAMES, FALLBACK_EXCLUDE_GLOBS
    names = {p for p in pats if "/" not in p and not any(ch in p for ch in "*?[")}
    globs = [p for p in pats if p not in names]
    return names, globs


def read_version():
    text = io.open(os.path.join(REPO_ROOT, "__init__.py"), encoding="utf-8").read()
    m = re.search(r'"version"\s*:\s*\((\d+)\s*,\s*(\d+)\s*,\s*(\d+)\)', text)
    if not m:
        raise SystemExit("无法从 __init__.py 的 bl_info 读到 version")
    return "%d.%d.%d" % tuple(int(g) for g in m.groups())


def tracked_files():
    out = subprocess.run(["git", "-C", REPO_ROOT, "ls-files"], capture_output=True,
                         text=True, encoding="utf-8", check=True).stdout
    return out.splitlines()


def keep(rel, names, globs):
    parts = rel.split("/")
    if any(p in names for p in parts[:-1]):      # 目录名命中
        return False
    if parts[-1] in names:                        # 同名文件
        return False
    return not any(fnmatch(parts[-1], g) for g in globs)


def main():
    ap = argparse.ArgumentParser(description="手动打包 TheHerta4 发布包")
    ap.add_argument("--version", help="版本号，如 4.4.47（缺省读 __init__.py）")
    ap.add_argument("--out-dir", default=os.path.join(REPO_ROOT, "dist"),
                    help="输出目录（缺省仓库 dist/）")
    ap.add_argument("--check", action="store_true", help="只列出规则与将入包的文件数，不写包")
    args = ap.parse_args()

    version = args.version or read_version()
    zip_name = "TheHerta4-%s.zip" % version.replace(".", "-")
    out_path = os.path.join(args.out_dir, zip_name)
    names, globs = read_excludes()

    files = tracked_files()
    kept = [f for f in files if keep(f, names, globs)]
    print("版本: %s" % version)
    print("排除规则: %d 个同名项 + %d 个通配（解析自 .github/workflows/release.yml）"
          % (len(names), len(globs)))
    print("跟踪文件 %d → 入包 %d" % (len(files), len(kept)))
    if args.check:
        print("（--check：不写包）")
        return 0

    staging = tempfile.mkdtemp(prefix="th4_pkg_")
    try:
        root = os.path.join(staging, "TheHerta4")
        for rel in kept:
            src = os.path.join(REPO_ROOT, rel.replace("/", os.sep))
            dst = os.path.join(root, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            shutil.copy2(src, dst)
        os.makedirs(args.out_dir, exist_ok=True)
        with zipfile.ZipFile(out_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for dirpath, _dirnames, filenames in os.walk(root):
                for fn in sorted(filenames):
                    full = os.path.join(dirpath, fn)
                    zf.write(full, os.path.relpath(full, staging).replace(os.sep, "/"))
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    size = os.path.getsize(out_path)
    with zipfile.ZipFile(out_path) as zf:
        entries = len(zf.namelist())
    sha = hashlib.sha256(io.open(out_path, "rb").read()).hexdigest()
    print("产物: %s" % out_path)
    print("  bytes=%d  entries=%d" % (size, entries))
    print("  sha256=%s" % sha)
    print("（把上面三项抄进同目录的 %s-更新说明.md）" % zip_name[:-4])
    return 0


if __name__ == "__main__":
    sys.exit(main())
