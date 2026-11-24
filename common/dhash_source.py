"""从 FrameAnalysis ``log.txt`` 回填贴图 dHash 的纯函数与工作空间落盘。

本模块不 import bpy，也不依赖 GlobalConfig：导入 hook、CLI 与单测共用同一套
解析/配对/写回逻辑。导出侧读的是 SubmeshJson 的 ``TextureMarkUpInfoList``
条目（``source_dhash`` / ``source_format`` / ``binding_mode``），因此
``--apply`` 与导入 hook 必须把三个字段写回 JSON；``Config/TextureDHashCache.json``
只是便于诊断的 sidecar。
"""

import json
import os
import re
import tempfile

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
            resolved = (dhash, resolve_format(filename))
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


def _iter_workspace_json_files(workspace_root: str) -> list[str]:
    """遍历 ``LOD*/*/TYPE_*/*.json``；没有 LOD 前缀时兼容旧扁平布局。"""
    root = os.path.abspath(str(workspace_root or ""))
    if not os.path.isdir(root):
        return []

    try:
        root_entries = sorted(os.scandir(root), key=lambda entry: entry.name)
    except OSError:
        return []

    lod_dirs = [
        entry
        for entry in root_entries
        if entry.is_dir() and re.fullmatch(r"LOD\d+", entry.name, re.IGNORECASE)
    ]
    bases = lod_dirs or [
        entry for entry in root_entries if entry.is_dir() and not entry.name.startswith(".")
    ]

    found = []
    for base in bases:
        try:
            sub_dirs = sorted(os.scandir(base.path), key=lambda entry: entry.name)
        except OSError:
            continue
        for sub_dir in sub_dirs:
            if not sub_dir.is_dir():
                continue
            try:
                type_dirs = sorted(os.scandir(sub_dir.path), key=lambda entry: entry.name)
            except OSError:
                continue
            for type_dir in type_dirs:
                if not type_dir.is_dir() or not type_dir.name.upper().startswith("TYPE_"):
                    continue
                try:
                    files = sorted(os.scandir(type_dir.path), key=lambda entry: entry.name)
                except OSError:
                    continue
                for file_entry in files:
                    if file_entry.is_file() and file_entry.name.lower().endswith(".json"):
                        found.append(file_entry.path)
    return sorted(set(found))


def workspace_marks(workspace_root: str) -> tuple[list, list[str]]:
    """返回工作空间里所有 ``TextureMarkUpInfoList`` 的 mark（dict）与 JSON 路径。"""
    marks: list = []
    paths: list[str] = []
    for json_path in _iter_workspace_json_files(workspace_root):
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
        paths.append(json_path)
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


def frameanalysis_dir_from_workspace(workspace_root: str) -> str:
    """读工作空间登记的抓帧目录；目录不存在/没登记返回空串。"""
    for config_path in _frameanalysis_candidates(workspace_root):
        if not os.path.isfile(config_path):
            continue
        try:
            payload = _load_json(config_path)
        except (OSError, ValueError):
            continue
        if not isinstance(payload, dict):
            continue
        raw_value = (
            payload.get("frameAnalysisFolderPath")
            or payload.get("FrameAnalysisPath")
            or payload.get("frame_analysis_folder_path")
            or ""
        )
        folder = str(raw_value or "").strip()
        if folder and os.path.isdir(folder):
            return os.path.normpath(folder)
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


def _write_dhash_cache(workspace_root: str, marks, frameanalysis_dir: str) -> str:
    """写 ``Config/TextureDHashCache.json``（sidecar，仅供诊断）。"""
    entries = {}
    for mark in marks or []:
        dhash = get_mark_dhash(mark)
        if not dhash:
            continue
        filename = _mark_field(mark, ("MarkFileName", "mark_filename"))
        if not filename:
            candidates = _mark_filename_candidates(mark)
            filename = _basename(candidates[0]) if candidates else ""
        if not filename:
            continue
        entries[filename] = {
            "dhash": dhash,
            "format": get_mark_format(mark),
            "binding_mode": "filter",
            "provider": "log",
        }

    cache_path = os.path.join(os.path.abspath(workspace_root), "Config", "TextureDHashCache.json")
    _atomic_write_json(
        cache_path,
        {
            "frameanalysisFolderPath": str(frameanalysis_dir or ""),
            "entries": entries,
        },
    )
    return cache_path


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


def _apply_to_json_file(json_path: str, mapping: dict) -> bool:
    try:
        payload = _load_json(json_path)
    except (OSError, ValueError):
        return False
    if not isinstance(payload, dict):
        return False
    raw_marks = payload.get("TextureMarkUpInfoList")
    if not isinstance(raw_marks, list):
        return False
    before = _mark_signature(raw_marks)
    apply_to_marks(raw_marks, mapping)
    if before == _mark_signature(raw_marks):
        return False
    _atomic_write_json(json_path, payload)
    return True


def apply_workspace_dhash(
    workspace_root: str,
    frameanalysis_dir: str = None,
    write_cache: bool = True,
    write_back: bool = True,
) -> int:
    """找抓帧目录 → 解析 log → 回填工作空间；返回干净写入的 mark 数（无 log 返回 0）。

    ``write_back=True`` 时把 ``source_dhash`` / ``source_format`` /
    ``binding_mode="filter"`` 写回对应 SubmeshJson 的
    ``TextureMarkUpInfoList`` 条目（保留原有键），并可选写诊断缓存。
    找不到 log 或目录时静默返回 0，不抛异常。
    """
    root = os.path.abspath(str(workspace_root or ""))
    if not os.path.isdir(root):
        return 0

    dump_dir = str(frameanalysis_dir or "").strip()
    if not dump_dir:
        dump_dir = frameanalysis_dir_from_workspace(root)
    if not dump_dir:
        return 0
    log_path = os.path.join(dump_dir, "log.txt")
    if not os.path.isfile(log_path):
        return 0

    try:
        mapping = parse_frame_analysis_log(log_path)
    except OSError:
        return 0

    try:
        marks, json_paths = workspace_marks(root)
    except OSError:
        return 0

    matched, contaminated, _missing = apply_to_marks(marks, mapping)
    usable = matched - contaminated
    if usable == 0 and not marks:
        return 0

    if write_back:
        for json_path in json_paths:
            try:
                _apply_to_json_file(json_path, mapping)
            except Exception as exc:  # 单个 JSON 写失败不阻断导入
                print("[dHash] 写回失败（跳过）: " + json_path + "，原因: " + str(exc))

    if write_cache:
        try:
            _write_dhash_cache(root, marks, dump_dir)
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
