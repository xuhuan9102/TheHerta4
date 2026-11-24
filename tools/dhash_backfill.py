"""手动把 FrameAnalysis log.txt 的 dHash 回填到工作空间。

用法::

    python -m tools.dhash_backfill <workspace> <frameanalysis-dir> [--apply]

dry-run 只读：打印去重贴图文件名的配对统计
``matched=N usable=M contaminated=K missing=J total=T``。
``--apply`` 额外把干净 dhash 写回 SubmeshJson 的 ``TextureMarkUpInfoList``
条目与 ``Config/TextureDHashCache.json``（原子写）。
"""

import argparse
import os
import sys

try:
    from common.dhash_source import (
        apply_to_marks,
        apply_workspace_dhash,
        parse_frame_analysis_log,
        workspace_marks,
        workspace_texture_stats,
    )
except ImportError:  # 允许直接 python tools/dhash_backfill.py
    _REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if _REPO_ROOT not in sys.path:
        sys.path.insert(0, _REPO_ROOT)
    from common.dhash_source import (
        apply_to_marks,
        apply_workspace_dhash,
        parse_frame_analysis_log,
        workspace_marks,
        workspace_texture_stats,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="从 FrameAnalysis log.txt 回填贴图 dHash 到 SSMT 工作空间"
    )
    parser.add_argument("workspace", help="SSMT 工作空间目录")
    parser.add_argument(
        "frameanalysis_dir",
        metavar="frameanalysis-dir",
        help="含 log.txt 的 FrameAnalysis 目录",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="写回 SubmeshJson 与 Config/TextureDHashCache.json（默认只读 dry-run）",
    )
    return parser


def main(argv=None) -> int:
    args = _build_parser().parse_args(argv)
    workspace_root = os.path.abspath(args.workspace)
    frameanalysis_dir = os.path.abspath(args.frameanalysis_dir)
    log_path = os.path.join(frameanalysis_dir, "log.txt")

    if not os.path.isdir(workspace_root):
        print(f"工作空间不存在: {workspace_root}", file=sys.stderr)
        return 2
    if not os.path.isfile(log_path):
        print(f"找不到 log.txt: {log_path}", file=sys.stderr)
        return 2

    try:
        mapping = parse_frame_analysis_log(log_path)
    except OSError as exc:
        print(f"读取 log 失败: {exc}", file=sys.stderr)
        return 2

    matched, usable, contaminated, missing, total = workspace_texture_stats(
        workspace_root, mapping
    )
    # 这一行是验收口径：matched 含污染命中，usable 才是干净可写。
    print(
        f"matched={matched} usable={usable} contaminated={contaminated} "
        f"missing={missing} total={total}"
    )

    marks, _json_paths = workspace_marks(workspace_root)
    mark_matched, mark_contaminated, mark_missing = apply_to_marks(marks, mapping)
    print(
        f"marks: matched={mark_matched} usable={mark_matched - mark_contaminated} "
        f"contaminated={mark_contaminated} missing={mark_missing} total={len(marks)}"
    )

    if args.apply:
        written = apply_workspace_dhash(
            workspace_root,
            frameanalysis_dir,
            write_cache=True,
            write_back=True,
        )
        print(f"applied: marks_written={written} (SubmeshJson + Config/TextureDHashCache.json)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
