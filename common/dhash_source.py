"""通过工作空间登记的 FrameAnalysis 给部件贴图标记回填 dHash。"""

import json
import os
import re
import tempfile
from pathlib import Path

try:
    from .dhash_metadata import (
        get_mark_binding_mode,
        get_mark_dhash,
        get_mark_format,
        normalize_binding_mode,
        normalize_dhash,
        normalize_format,
    )
except ImportError:  # tests 用 fake 包按文件路径加载本模块时走这里
    import importlib.util as _importlib_util
    import sys as _sys

    _DHASH_METADATA_MODULE_NAME = "theherta4_dhash_metadata_for_source"
    _dhash_metadata = _sys.modules.get(_DHASH_METADATA_MODULE_NAME)
    if _dhash_metadata is None:
        _spec = _importlib_util.spec_from_file_location(
            _DHASH_METADATA_MODULE_NAME,
            os.path.join(os.path.dirname(os.path.abspath(__file__)), "dhash_metadata.py"),
        )
        _dhash_metadata = _importlib_util.module_from_spec(_spec)
        _sys.modules[_DHASH_METADATA_MODULE_NAME] = _dhash_metadata
        _spec.loader.exec_module(_dhash_metadata)
    get_mark_binding_mode = _dhash_metadata.get_mark_binding_mode
    get_mark_dhash = _dhash_metadata.get_mark_dhash
    get_mark_format = _dhash_metadata.get_mark_format
    normalize_binding_mode = _dhash_metadata.normalize_binding_mode
    normalize_dhash = _dhash_metadata.normalize_dhash
    normalize_format = _dhash_metadata.normalize_format


#: 一行里同时出现 texture hash 与 dhash 才算候选行。没有 dhash 的
#: ``IASetIndexBuffer(... hash=...)`` / 半截污染行不会被当成映射。
_LOG_LINE_RE = re.compile(
    r"hash=([0-9a-f]{8})\b.*?\bdhash=([0-9a-f]{16})\b", re.IGNORECASE
)
_HASH8_RE = re.compile(r"(?<![0-9a-f])[0-9a-f]{8}(?![0-9a-f])", re.IGNORECASE)
_DDS_PAIR_RE = re.compile(
    r"^[0-9a-f]{8}_[0-9a-f]{8}-.+\.dds$", re.IGNORECASE
)
_LOD_NAME_RE = re.compile(r"LOD\d+", re.IGNORECASE)
_CONTAMINATION_MARK = "hash_contamination="

#: UpdateSubresource/Region 的占位 dhash；即使某行没打 hash_contamination，
#: 这个值也不能写进工作空间。
_CONTAMINATED_DHASH_SENTINELS = frozenset({"00ff00000000ffff"})

#: 标记文件名字段。优先 MarkDedupedFileName（``<hash8>_<other>-FORMAT.dds``，
#: 首个 hash8 才是 log 里的贴图 hash）；MarkFileName 只作旧工作空间兜底。
_FILENAME_KEYS = (
    "MarkDedupedFileName",
    "mark_deduped_filename",
    "MarkFileName",
    "mark_filename",
)
_FILENAME_ATTRS = (
    "mark_deduped_filename",
    "mark_filename",
    "MarkDedupedFileName",
    "MarkFileName",
)


def _basename(path) -> str:
    return os.path.basename(str(path or "").replace("\\", "/"))


def _first_hash8(name) -> str:
    """文件名/路径里第一个 8 位十六进制串（大小写归一为小写）。"""
    match = _HASH8_RE.search(_basename(name))
    return match.group(0).lower() if match else ""


def _load_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _atomic_write_json(path: str, payload) -> None:
    """同目录 tmp + os.replace，避免半截 JSON。"""
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(
        prefix=".dhash-", suffix=".tmp", dir=directory
    )
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=4)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def parse_frame_analysis_log(path: str) -> dict:
    """解析 log.txt，返回 ``{hash8: {"dhash": 16hex, "format": "", "contaminated": bool}}``。

    - 只认同一行同时含 ``hash=`` 与 ``dhash=`` 的行；
    - 污染行（``hash_contamination=`` 或占位 dhash）的 dhash 不采用；若该 hash
      没有干净行，则保留一条 ``dhash=""`` / ``contaminated=True`` 的记录，
      这样上层能区分「log 里没出现」与「出现但不可用」；
    - 同一 hash 出现多个不同干净 dhash（动态资源）时不任选，记为不可用；
    - 同一 hash 同一干净 dhash 重复出现只保留一条。
    """
    clean_dhashes: dict[str, set] = {}
    contaminated_hashes: set[str] = set()

    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        for line in handle:
            match = _LOG_LINE_RE.search(line)
            if match is None:
                continue
            hash8 = match.group(1).lower()
            dhash = match.group(2).lower()
            if _CONTAMINATION_MARK in line or dhash in _CONTAMINATED_DHASH_SENTINELS:
                contaminated_hashes.add(hash8)
                continue
            clean_dhashes.setdefault(hash8, set()).add(dhash)

    mapping: dict[str, dict] = {}
    for hash8 in clean_dhashes.keys() | contaminated_hashes:
        dhashes = clean_dhashes.get(hash8, set())
        if len(dhashes) == 1:
            mapping[hash8] = {
                "dhash": next(iter(dhashes)),
                "format": "",
                "contaminated": False,
            }
        elif len(dhashes) > 1:
            # 动态资源：同一 hash 多个不同 dhash，不能任选一个。
            mapping[hash8] = {"dhash": "", "format": "", "contaminated": True}
        else:
            # 只在污染行里出现过，或只有占位 dhash。
            mapping[hash8] = {"dhash": "", "format": "", "contaminated": True}
    return mapping


def resolve_dhash(stem: str, mapping: dict) -> str:
    """取文件名/路径 basename 里第一个 hash8，查 mapping；不可用返回空串。"""
    hash8 = _first_hash8(stem)
    if not hash8 or not isinstance(mapping, dict):
        return ""
    entry = mapping.get(hash8)
    if not isinstance(entry, dict):
        return ""
    if entry.get("contaminated"):
        return ""
    return normalize_dhash(entry.get("dhash"))


def resolve_format(stem: str) -> str:
    """从 ``-<FORMAT>.dds`` 取格式并去掉 ``DXGI_FORMAT_`` 前缀；没有返回空串。

    只接受全大写/数字/下划线且含数字的最后一段（DXGI 格式都带数字），
    避免把 ``064cd7d3-2820-0-DiffuseMap.dds`` 的 ``DiffuseMap`` 误当格式。
    """
    base = _basename(stem)
    base = re.sub(r"\.dds$", "", base, flags=re.IGNORECASE)
    _head, separator, token = base.rpartition("-")
    if not separator:
        return ""
    text = token.strip().upper()
    if text.startswith("DXGI_FORMAT_"):
        text = text[len("DXGI_FORMAT_"):]
    if not text or not re.fullmatch(r"[A-Z0-9_]+", text) or not re.search(r"\d", text):
        return ""
    return normalize_format(text)


def _mark_filename_candidates(mark) -> list[str]:
    values = []
    if isinstance(mark, dict):
        for key in _FILENAME_KEYS:
            value = mark.get(key)
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
    else:
        for attr in _FILENAME_ATTRS:
            value = getattr(mark, attr, None)
            if isinstance(value, str) and value.strip():
                values.append(value.strip())
    # 去重且保持顺序
    return list(dict.fromkeys(values))


def _set_mark_field(mark, key: str, value: str) -> None:
    if isinstance(mark, dict):
        mark[key] = value
        return
    try:
        setattr(mark, key, value)
    except Exception:
        pass


def apply_to_marks(marks, mapping: dict) -> tuple[int, int, int]:
    """把干净 dhash 写进 mark；返回 ``(matched, contaminated, missing)``。

    ``matched`` 包含命中但被污染/动态丢弃的 mark（用于证明文件名配对键正确）；
    可用数 = ``matched - contaminated``。``marks`` 里每项可以是 dict 或对象，
    不能解析的项保持原样（缺省 slot）。
    """
    matched = 0
    contaminated = 0
    missing = 0

    for mark in marks or []:
        if not isinstance(mark, dict) and not any(
            hasattr(mark, attr) for attr in _FILENAME_ATTRS
        ):
            missing += 1
            continue

        resolved = None
        saw_unusable = False
        for filename in _mark_filename_candidates(mark):
            hash8 = _first_hash8(filename)
            if not hash8:
                continue
            entry = mapping.get(hash8) if isinstance(mapping, dict) else None
            if not isinstance(entry, dict):
                continue
            dhash = normalize_dhash(entry.get("dhash"))
            if entry.get("contaminated") or not dhash:
                saw_unusable = True
                continue
            fmt = resolve_format(filename) or get_mark_format(mark)
            if not fmt:
                saw_unusable = True
                continue
            resolved = (dhash, fmt)
            break

        if resolved is not None:
            dhash, fmt = resolved
            matched += 1
            _set_mark_field(mark, "source_dhash", dhash)
            _set_mark_field(mark, "source_format", fmt)
            _set_mark_field(mark, "binding_mode", "filter")
        elif saw_unusable:
            matched += 1
            contaminated += 1
        else:
            missing += 1

    return matched, contaminated, missing


def _workspace_json_entries(workspace_root: str):
    """枚举子网格 JSON，同时保留所属工作空间分区与 LOD。"""
    root = Path(workspace_root)

    def entries(scope):
        yield from ((path, scope, "") for path in sorted(scope.glob("*/TYPE_*/*.json")))
        for folder in sorted(scope.iterdir()):
            if folder.is_dir() and _LOD_NAME_RE.fullmatch(folder.name):
                yield from (
                    (path, scope, folder.name.upper())
                    for path in sorted(folder.glob("*/TYPE_*/*.json"))
                )

    try:
        direct = list(entries(root))
        if direct:
            yield from direct
            return
        for partition in sorted(root.iterdir()):
            if partition.is_dir() and (partition / "Config.json").is_file():
                yield from entries(partition)
    except OSError:
        return


def workspace_marks(workspace_root: str) -> tuple[list, list[str]]:
    """返回工作空间里所有 ``TextureMarkUpInfoList`` 的 mark（dict）与 JSON 路径。"""
    marks: list = []
    paths: list[str] = []
    for json_path, _scope, _lod in _workspace_json_entries(workspace_root):
        try:
            payload = _load_json(json_path)
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        raw_marks = payload.get("TextureMarkUpInfoList")
        if not isinstance(raw_marks, list):
            continue
        dict_marks = [mark for mark in raw_marks if isinstance(mark, dict)]
        if not dict_marks:
            continue
        paths.append(str(json_path))
        marks.extend(dict_marks)
    return marks, paths


def _frameanalysis_candidates(workspace_root: str) -> list[str]:
    root = os.path.abspath(str(workspace_root or ""))
    candidate_paths = [
        os.path.join(root, "Config", "FrameAnalysisPath.json"),
        os.path.join(root, "Import.json"),
    ]
    tabs_dir = os.path.join(root, "Config", "Tabs")
    if os.path.isdir(tabs_dir):
        try:
            candidate_paths.extend(
                os.path.join(tabs_dir, name)
                for name in sorted(os.listdir(tabs_dir))
                if name.lower().endswith(".json")
            )
        except OSError:
            pass
    return candidate_paths


def _recorded_frameanalysis_dir(config_path, workspace_root):
    try:
        payload = _load_json(config_path)
    except (OSError, ValueError):
        return ""
    if not isinstance(payload, dict):
        return ""
    folder = str(
        payload.get("frameAnalysisFolderPath")
        or payload.get("FrameAnalysisPath")
        or payload.get("frame_analysis_folder_path")
        or ""
    ).strip()
    return os.path.normpath(os.path.join(workspace_root, folder)) if folder else ""


def _lod_frameanalysis_dirs(workspace_root):
    config = Path(workspace_root) / "Config"
    try:
        payload = _load_json(config / "WorkPageTabs.json")
    except (OSError, ValueError):
        return {}
    tabs = payload.get("tabs") if isinstance(payload, dict) else None
    if not isinstance(tabs, list):
        return {}
    paths = {}
    for tab in tabs:
        if not isinstance(tab, dict):
            continue
        name = str(tab.get("name", "")).strip().upper()
        tab_id = str(tab.get("id", "")).strip()
        if not _LOD_NAME_RE.fullmatch(name) or not tab_id:
            continue
        path = _recorded_frameanalysis_dir(config / "Tabs" / (tab_id + ".json"), workspace_root)
        if path:
            paths[name] = path
    return paths


def frameanalysis_dir_from_workspace(workspace_root: str, lod_name: str = "") -> str:
    """优先选当前 LOD 的日志，其次选工作空间登记的通用日志。"""
    lod_name = str(lod_name or "").strip().upper()
    lod_paths = _lod_frameanalysis_dirs(workspace_root)
    lod_path = lod_paths.get(lod_name, "")
    if lod_path and os.path.isfile(os.path.join(lod_path, "log.txt")):
        return lod_path
    # 工作空间全局记录可能是当前活动工作页，避免借用已知属于其它 LOD 的抓帧。
    other_lod_paths = {
        os.path.normcase(path) for lod, path in lod_paths.items() if lod_name and lod != lod_name
    }
    for config_path in _frameanalysis_candidates(workspace_root):
        folder = _recorded_frameanalysis_dir(config_path, workspace_root)
        if folder and os.path.normcase(folder) not in other_lod_paths and os.path.isfile(
            os.path.join(folder, "log.txt")
        ):
            return folder
    return ""


def _mark_field(mark, names) -> str:
    if isinstance(mark, dict):
        for name in names:
            if name in mark and mark[name] is not None:
                return str(mark[name]).strip()
        return ""
    for name in names:
        value = getattr(mark, name, None)
        if value is not None:
            return str(value).strip()
    return ""


def _cache_entry_for_mark(mark):
    dhash, fmt = get_mark_dhash(mark), get_mark_format(mark)
    if dhash and fmt:
        return {"dhash": dhash, "format": fmt, "binding_mode": get_mark_binding_mode(mark), "provider": "log"}
    return None


def _cache_key(workspace_root, json_path, mark):
    filenames = _mark_filename_candidates(mark)
    if not filenames:
        return ""
    part_path = os.path.relpath(json_path, workspace_root).replace("\\", "/")
    mark_hash = _mark_field(mark, ("MarkHash", "mark_hash"))
    return f"{part_path}|{_basename(filenames[0])}|{mark_hash}".casefold()


def _write_dhash_cache(cache_path, marks, frameanalysis_dir, scoped_entries):
    """保留旧的文件名索引，按子网格路径与原图身份保存可恢复的缓存。"""
    entries = {}
    for mark in marks or []:
        entry = _cache_entry_for_mark(mark)
        if entry is None:
            continue
        filename = _mark_field(mark, ("MarkFileName", "mark_filename"))
        if not filename:
            candidates = _mark_filename_candidates(mark)
            filename = _basename(candidates[0]) if candidates else ""
        if not filename:
            continue
        entries[filename] = entry

    _atomic_write_json(
        cache_path,
        {
            "frameanalysisFolderPath": str(frameanalysis_dir or ""),
            "entries": entries,
            "scopedEntries": scoped_entries,
        },
    )


def _read_scoped_dhash_cache(cache_path):
    try:
        payload = _load_json(cache_path)
    except (OSError, ValueError):
        return {}
    entries = payload.get("scopedEntries") if isinstance(payload, dict) else None
    return entries if isinstance(entries, dict) else {}


def _restore_cached_marks(workspace_root, json_path, marks, scoped_entries):
    restored = 0
    for mark in marks:
        if _cache_entry_for_mark(mark) is not None:
            continue
        entry = scoped_entries.get(_cache_key(workspace_root, json_path, mark))
        if not isinstance(entry, dict):
            continue
        dhash, fmt = get_mark_dhash(entry), get_mark_format(entry)
        if dhash and fmt:
            mark.update(source_dhash=dhash, source_format=fmt, binding_mode=get_mark_binding_mode(entry))
            restored += 1
    return restored


def _mark_signature(marks) -> tuple:
    signature = []
    for mark in marks or []:
        if isinstance(mark, dict):
            signature.append(
                (get_mark_dhash(mark), get_mark_format(mark), get_mark_binding_mode(mark))
            )
        else:
            signature.append(
                (
                    get_mark_dhash(mark),
                    get_mark_format(mark),
                    get_mark_binding_mode(mark),
                )
            )
    return tuple(signature)


def apply_workspace_dhash(
    workspace_root: str,
    frameanalysis_dir: str = None,
    write_cache: bool = True,
    write_back: bool = True,
    *,
    create_empty_cache: bool = False,
) -> int:
    """优先用当前抓帧更新贴图缓存，抓帧缺项时保留部件的已有标记。"""
    root = os.path.abspath(str(workspace_root or ""))
    if not os.path.isdir(root):
        return 0

    root_path = Path(root)
    cache_path = os.path.join(root, "Config", "TextureDHashCache.json")
    previous_cache = _read_scoped_dhash_cache(cache_path)
    scoped_entries = dict(previous_cache)
    explicit_dir = str(frameanalysis_dir or "").strip()
    source_dirs = {}
    log_mappings = {}
    cached_marks = []
    used_dirs = set()
    usable = 0
    for json_path, workspace_scope, lod in _workspace_json_entries(root):
        try:
            payload = _load_json(json_path)
        except (OSError, ValueError):
            continue
        marks = payload.get("TextureMarkUpInfoList") if isinstance(payload, dict) else None
        if not isinstance(marks, list):
            continue
        candidates = [mark for mark in marks if isinstance(mark, dict)]
        if write_cache:
            cached_marks.extend(candidates)
        if not candidates:
            continue
        scope = (workspace_scope, lod)
        if scope not in source_dirs:
            source_dirs[scope] = (
                explicit_dir
                or frameanalysis_dir_from_workspace(workspace_scope, lod)
                or (frameanalysis_dir_from_workspace(root, lod) if workspace_scope != root_path else "")
            )
        dump_dir = source_dirs[scope]
        if dump_dir and dump_dir not in log_mappings:
            try:
                log_mappings[dump_dir] = parse_frame_analysis_log(os.path.join(dump_dir, "log.txt"))
            except OSError:
                log_mappings[dump_dir] = None
        mapping = log_mappings.get(dump_dir)
        before = _mark_signature(candidates)
        matched = contaminated = 0
        if mapping is not None:
            used_dirs.add(dump_dir)
            matched, contaminated, _missing = apply_to_marks(candidates, mapping)
        restored = _restore_cached_marks(root, json_path, candidates, previous_cache)
        if write_back and before != _mark_signature(candidates):
            try:
                _atomic_write_json(json_path, payload)
            except OSError as exc:
                print(f"[dHash] 写回失败: {json_path}，原因: {exc}")
                continue
        usable += matched - contaminated + restored
        if write_cache:
            for mark in candidates:
                key = _cache_key(root, json_path, mark)
                entry = _cache_entry_for_mark(mark)
                if key and entry:
                    scoped_entries[key] = entry

    if write_cache and (
        scoped_entries != previous_cache
        or (not os.path.isfile(cache_path) and (used_dirs or create_empty_cache))
    ):
        try:
            cache_source = next(iter(used_dirs)) if len(used_dirs) == 1 else ""
            _write_dhash_cache(cache_path, cached_marks, cache_source, scoped_entries)
        except Exception as exc:
            print("[dHash] 诊断缓存写入失败（跳过）: " + str(exc))

    return usable


def workspace_texture_stats(workspace_root: str, mapping: dict) -> tuple[int, int, int, int, int]:
    """统计去重贴图文件名（``<hash8>_<hash8>-FORMAT.dds``）的配对情况。

    返回 ``(matched, usable, contaminated, missing, total)``。这一口径用于
    CLI dry-run：证明「文件名首 hash8」与 log 的配对键是否正确；写回本身只
    落在 SubmeshJson 的 mark 上。
    """
    root = os.path.abspath(str(workspace_root or ""))
    matched = usable = contaminated = missing = total = 0
    if not os.path.isdir(root):
        return matched, usable, contaminated, missing, total

    for dir_path, dir_names, file_names in os.walk(root):
        dir_names[:] = [name for name in dir_names if not name.startswith(".")]
        for file_name in file_names:
            if not _DDS_PAIR_RE.match(file_name):
                continue
            total += 1
            hash8 = _first_hash8(file_name)
            entry = mapping.get(hash8) if hash8 else None
            if not isinstance(entry, dict):
                missing += 1
                continue
            matched += 1
            if entry.get("contaminated") or not normalize_dhash(entry.get("dhash")):
                contaminated += 1
            else:
                usable += 1
    return matched, usable, contaminated, missing, total
