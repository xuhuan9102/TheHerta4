"""导出期贴图落盘计划器（落盘前联合去重）。

背景：一次导出里贴图会被三个模块先后处理——

1. 基础导出（``common/m_ini_helper.py``）把工作空间贴图复制到 ``Textures/``；
2. 「材质转资源pro」后处理再把材质贴图复制 / 转 DDS 进 ``Textures/``；
3. 「资源合并」后处理最后扫描 ini，删除内容重复的贴图并重写引用。

第 2、3 步经常把第 1 步刚写出来的文件删掉（「生了又删」）。本模块把三者改成
「先登记、联合去重、最后只落盘一次」：

- ``register(owner="base")``：基础导出只登记，不落盘；
- ``register(owner="material")``：材质转资源pro 登记后立即参与去重决策，
  与已登记内容重复时直接复用代表文件名（不复制、不转换）；
- ``finalize()``：所有代表项统一复制 / 转换一次；转换失败回退改名时重写 ini。

去重键 =「落盘规格 spec」× 内容指纹：

- ``spec=("copy",)``：原样复制（基础导出 / 材质源已是 DDS / 未开转 DDS）；
- ``spec=("dds", texture_type, workspace_source_path)``：需要 texconv 转换。

**规格不同即使源文件相同也各自落盘**——否则漫反射图会被当法线图读（BC 通道不同）。

调试开关：``DEBUG`` 为真时打印每条判定（前缀 ``[贴图计划]``）。
"""

import json
import os
import time
from concurrent.futures import ThreadPoolExecutor

from . import texture_dedup

DEBUG = False

OWNER_BASE = "base"
OWNER_MATERIAL = "material"

#: 原样复制的落盘规格
SPEC_COPY = ("copy",)


def _debug(message):
    if DEBUG:
        print(f"[贴图计划] {message}")


def _dds_analysis_worker_limit(pixel_dedup=False):
    """按真实目标预检吞吐量选择 C 并发；不按 CPU 核心数限制 C worker。"""
    try:
        from .dds_pixel import native_backend_name

        if native_backend_name() == "C-BC7":
            return 32 if pixel_dedup else 16
    except ImportError:
        pass
    return max(1, min(2, os.cpu_count() or 2))


def _report_progress(done, total):
    """落盘进度只更新状态栏文本（不推动百分比）；无进度模块时静默。"""
    try:
        from ..blueprint import export_progress

        export_progress.report_stage(f"贴图落盘 {done}/{total}")
    except Exception:
        pass


def _load_converter():
    """取 texconv 转换入口；裁剪环境 / 单测拿不到时返回 None。"""
    try:
        from ..toolkit.tt_dds_conversion import convert_texture_to_dds_cached

        return convert_texture_to_dds_cached
    except Exception:
        return None


class PlanEntry(object):
    """一条待落盘的贴图计划项。"""

    __slots__ = (
        "entry_id",
        "source_path",
        "target_folder",
        "preferred_filename",
        "final_filename",
        "owner",
        "owner_label",
        "spec_key",
        "dedup_group",
        "md5",
        "pixel_key",
        "dims",
        "features",
        "dds_signature",
        "reuse_of",
        "dropped",
        "materialized",
        "convert_kwargs",
        "overwrite_policy",
        "ini_folder",
    )

    def __init__(self, entry_id, source_path, target_folder, preferred_filename,
                 owner, owner_label, spec_key, md5, pixels=None, dims=None,
                 convert_kwargs=None, overwrite_policy="skip", ini_folder="Textures",
                 dedup_group=None, pixel_key=None, features=None,
                 dds_signature=None):
        self.entry_id = int(entry_id)
        self.source_path = source_path
        self.target_folder = target_folder
        self.preferred_filename = preferred_filename
        self.final_filename = preferred_filename
        self.owner = owner
        self.owner_label = owner_label or ""
        self.spec_key = spec_key
        self.dedup_group = dedup_group or ""
        self.md5 = md5
        self.pixel_key = pixel_key or (
            texture_dedup.pixel_key(pixels) if pixels is not None else None
        )
        self.dims = dims
        self.features = features or (
            texture_dedup.similarity_features(pixels) if pixels is not None else None
        )
        self.dds_signature = dds_signature
        self.reuse_of = None
        self.dropped = False
        self.materialized = False
        self.convert_kwargs = convert_kwargs or None
        self.overwrite_policy = overwrite_policy or "skip"
        self.ini_folder = str(ini_folder or "Textures").strip().strip("\\/") or "Textures"

    @property
    def ini_filename(self):
        return f"{self.ini_folder}/{self.final_filename}"


class TextureExportPlan(object):
    """导出期单例：登记 → 去重决策 → 统一落盘。"""

    #: owner 常量同时挂在类上：调用方写 ``TextureExportPlan.OWNER_MATERIAL``
    OWNER_BASE = OWNER_BASE
    OWNER_MATERIAL = OWNER_MATERIAL
    SPEC_COPY = SPEC_COPY

    #: 是否启用（= 蓝图里存在「资源合并」节点且勾选了「落盘前联合去重」）
    _enabled = False
    _pixel_dedup = False
    _similarity_dedup = False
    _threshold = 90
    _mod_export_path = ""
    _textures_dir = ""

    _entries = []
    _warmed_sources = {}
    _next_id = 0
    _by_source = {}
    _by_md5 = {}
    _by_pixel = {}
    _similarity_reps = {}
    _by_target = {}
    _pending_renames = {}
    _stats = {}
    #: 计划管理的落盘路径缓存（资源合并据此跳过二次去重；无效时置 None）
    _managed_paths_cache = None
    #: 单测注入点：替换 texconv 转换函数（None 时用真实实现）
    _converter_override = None

    # -- 配置 ---------------------------------------------------------------

    @classmethod
    def configure(cls, enabled, pixel_dedup=False, similarity_dedup=False,
                  threshold=90, mod_export_path=""):
        """由导出入口在开始时调用一次。"""
        cls._enabled = bool(enabled)
        cls._pixel_dedup = bool(pixel_dedup)
        cls._similarity_dedup = bool(similarity_dedup)
        try:
            cls._threshold = max(50, min(100, int(threshold)))
        except (TypeError, ValueError):
            cls._threshold = 90
        cls._mod_export_path = str(mod_export_path or "")
        cls._textures_dir = (
            os.path.join(cls._mod_export_path, "Textures")
            if cls._mod_export_path
            else ""
        )
        cls._reset_state()
        _debug(
            f"configure 启用={cls._enabled} 像素={cls._pixel_dedup} "
            f"相似度={cls._similarity_dedup} 阈值={cls._threshold} "
            f"导出目录={cls._mod_export_path or '<未指定>'}"
        )
        return cls._enabled

    @classmethod
    def configure_from_tree(cls, tree, mod_export_path):
        """从蓝图的后处理链里找「资源合并」节点，读它的开关与去重配置。"""
        merge_node = cls.find_resource_merge_node(tree)
        if merge_node is None:
            return cls.configure(False, mod_export_path=mod_export_path)
        return cls.configure(
            enabled=bool(getattr(merge_node, "pre_plan_dedup", True)),
            pixel_dedup=bool(getattr(merge_node, "use_pixel_dedup", False)),
            similarity_dedup=bool(getattr(merge_node, "use_similarity_dedup", False)),
            threshold=getattr(merge_node, "similarity_threshold", 90),
            mod_export_path=mod_export_path,
        )

    @staticmethod
    def find_resource_merge_node(tree):
        """返回蓝图中第一个未禁用的「资源合并」节点（含嵌套蓝图）；找不到返回 None。"""
        if tree is None:
            return None
        nodes = []
        try:
            from ..blueprint.export_helper import BlueprintExportHelper

            nodes = list(BlueprintExportHelper._collect_postprocess_nodes(tree) or [])
        except Exception:
            nodes = list(getattr(tree, "nodes", []) or [])
        for node in nodes:
            if str(getattr(node, "bl_idname", "") or "") != "SSMTNode_PostProcess_ResourceMerge":
                continue
            if bool(getattr(node, "mute", False)):
                continue
            return node
        return None

    @classmethod
    def reset(cls):
        cls._reset_state()
        cls._enabled = False
        cls._mod_export_path = ""
        cls._textures_dir = ""

    @classmethod
    def overwrite_targets(cls):
        """返回允许显式覆盖旧文件的贴图代表项路径。"""
        return {
            os.path.join(entry.target_folder, entry.final_filename)
            for entry in cls._entries
            if entry.overwrite_policy == "overwrite" and not entry.dropped and entry.reuse_of is None
        }

    @classmethod
    def _reset_state(cls):
        cls._entries = []
        cls._warmed_sources = {}
        cls._next_id = 0
        cls._by_source = {}
        cls._by_md5 = {}
        cls._by_pixel = {}
        cls._similarity_reps = {}
        cls._by_target = {}
        cls._pending_renames = {}
        cls._managed_paths_cache = None
        cls._stats = {
            "registered": 0,
            "reuse_source": 0,
            "reuse_md5": 0,
            "reuse_pixel": 0,
            "reuse_similarity": 0,
            "reuse_target_name": 0,
            "dropped": 0,
            "written": 0,
            "skip_same_content": 0,
            "skip_keep_existing": 0,
            "convert_failed": 0,
            "renamed": 0,
        }
        cls._profile = {
            "started_at": time.perf_counter(),
            "source_md5_s": 0.0,
            "source_analysis_s": 0.0,
            "source_decode_s": 0.0,
            "source_decode_count": 0,
            "source_feature_s": 0.0,
            "source_cache_hits": 0,
            "source_signature_s": 0.0,
            "source_similarity_s": 0.0,
            "source_similarity_pairs": 0,
            "target_md5_s": 0.0,
            "target_decode_s": 0.0,
            "target_decode_count": 0,
            "target_feature_s": 0.0,
            "target_similarity_s": 0.0,
            "target_similarity_checks": 0,
            "target_cache_hits": 0,
            "target_dimension_prefilters": 0,
            "target_compressed_exact": 0,
            "target_parallel_workers": 0,
            "target_preflight_s": 0.0,
            "convert_s": 0.0,
            "copy_s": 0.0,
        }

    # -- 查询 ---------------------------------------------------------------

    @classmethod
    def enabled(cls):
        return bool(cls._enabled)

    @classmethod
    def managed_paths(cls):
        """本次导出由贴图计划管理的落盘路径集合（小写绝对路径）。

        资源合并节点在落盘后还会扫描 ini 再按 MD5 / 像素 / 相似度去重一次——计划内
        文件已经做过联合去重（含「同一切换组不合并」的保护），再让资源合并按相似度
        合并一次会把脸妆等刻意变体并掉。资源合并据此直接跳过计划内文件。
        """
        if cls._managed_paths_cache is None:
            paths = set()
            for entry in cls._entries:
                if entry.dropped:
                    continue
                target = os.path.join(entry.target_folder, entry.final_filename)
                paths.add(os.path.normcase(os.path.abspath(target)))
            cls._managed_paths_cache = paths
        return cls._managed_paths_cache

    @classmethod
    def manages_file(cls, file_path):
        """该文件是否由本次导出的贴图计划管理（已联合去重，资源合并不得再动）。"""
        text = str(file_path or "").strip()
        if not text:
            return False
        normalized = os.path.normcase(os.path.abspath(text))
        return normalized in cls.managed_paths()

    @classmethod
    def stats(cls):
        return dict(cls._stats)

    @classmethod
    def entry_count(cls):
        return len(cls._entries)

    @classmethod
    def has_pending(cls):
        for entry in cls._entries:
            if entry.reuse_of is not None or entry.dropped or entry.materialized:
                continue
            return True
        return False

    # -- 登记 ---------------------------------------------------------------

    @staticmethod
    def _spec_key(owner, spec):
        """落盘规格键：不同规格的产物不能合并（格式 / 通道不同）。"""
        if spec:
            return tuple(str(part) for part in spec)
        if owner == OWNER_MATERIAL:
            return ("copy",)
        return SPEC_COPY

    @classmethod
    def prewarm_sources(cls, source_paths):
        """Prepare independent native analyses; register still decides in original order."""
        if not cls._enabled or not (cls._pixel_dedup or cls._similarity_dedup):
            return
        paths = list(dict.fromkeys(os.path.abspath(path) for path in source_paths
                                 if str(path).lower().endswith(('.png', '.dds'))))
        paths = [path for path in paths if os.path.normcase(path) not in cls._warmed_sources]
        if not paths:
            return
        # Snapshot Scene policy before starting workers. No worker may read RNA.
        texture_dedup._analysis_cache_enabled()
        started = time.perf_counter()
        def fingerprint(path):
            try:
                stat = os.stat(path)
                signature = (stat.st_size, stat.st_mtime_ns)
                digest = texture_dedup.compute_md5(path)
                return path, signature, digest
            except (OSError, ValueError):
                return None
        workers = min(len(paths), _dds_analysis_worker_limit(cls._pixel_dedup))
        groups = {}
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for item in pool.map(fingerprint, paths):
                if item is not None and item[2]:
                    groups.setdefault(item[2], []).append(item)
            def analyze(group):
                path, signature, digest = group[0]
                analysis = texture_dedup.analyze_file(path, file_md5=digest,
                    need_pixel_key=cls._pixel_dedup, need_features=cls._similarity_dedup,
                    allow_blender=False)
                return group, analysis
            for group, analysis in pool.map(analyze, groups.values()):
                cls._profile['source_decode_s'] += analysis.get('decode_seconds', 0.0)
                cls._profile['source_decode_count'] += bool(analysis.get('decoded'))
                cls._profile['source_feature_s'] += analysis.get('feature_seconds', 0.0)
                cls._profile['source_cache_hits'] += bool(analysis.get('cache_hit'))
                for path, signature, digest in group:
                    cls._warmed_sources[os.path.normcase(path)] = (signature, digest, analysis)
        print('[TextureSourcePrewarmProfile] ' + json.dumps({
            'files':len(paths), 'unique_contents':len(groups), 'workers':workers, 'elapsed_s':time.perf_counter()-started}))

    @classmethod
    def register(cls, source_path, target_folder, preferred_filename, owner,
                 spec=None, owner_label="", convert_kwargs=None,
                 overwrite_policy="skip", ini_folder="Textures", dedup_group=None):
        """登记一条贴图落盘任务，返回 ini 里应写的文件名。

        - 与已登记项（同规格）内容重复时复用代表文件名，本项不落盘；
        - 内容不同时作为新代表，等 ``finalize()`` 统一落盘；
        - 未启用计划器、源不存在或参数非法时返回 ``None``（调用方走旧路径）。

        ``dedup_group``：同一材质切换组的不同档位互为**刻意变体**（脸妆/服装微调），
        组内绝不互相去重，否则切档会显示同一张图；跨组仍按内容去重。
        """
        if not cls._enabled:
            return None

        source_path = os.path.abspath(str(source_path or "").strip())
        if not source_path or not os.path.exists(source_path):
            return None

        target_folder = str(target_folder or "").strip()
        if not target_folder:
            return None

        preferred_filename = os.path.basename(str(preferred_filename or "").strip())
        if not preferred_filename:
            return None

        spec_key = cls._spec_key(owner, spec)

        # 1) 同源 + 同规格：直接复用（源内容甚至连 MD5 都不用再算）
        source_key = (spec_key, os.path.normcase(source_path))
        existing_id = cls._by_source.get(source_key)
        if existing_id is not None:
            existing = cls._entries[existing_id]
            if not existing.dropped:
                cls._stats["reuse_source"] += 1
                _debug(
                    f"复用(同源) {os.path.basename(source_path)} -> "
                    f"{existing.final_filename} [{existing.owner_label}]"
                )
                return existing.final_filename

        started = time.perf_counter()
        warmed = cls._warmed_sources.get(os.path.normcase(source_path))
        if warmed is not None:
            stat = os.stat(source_path)
            if warmed[0] != (stat.st_size, stat.st_mtime_ns):
                warmed = None
        md5 = warmed[1] if warmed is not None else texture_dedup.compute_md5(source_path)
        cls._profile["source_md5_s"] += time.perf_counter() - started

        # 2) 同规格 + 同 MD5（"同规格"= 同贴图类型；跨类型永远不合并）
        if md5:
            for existing_id in cls._by_md5.get((spec_key, md5), ()):
                existing = cls._entries[existing_id]
                if existing.dropped:
                    continue
                cls._stats["reuse_md5"] += 1
                _debug(
                    f"复用(MD5) {os.path.basename(source_path)} -> "
                    f"{existing.final_filename} [{existing.owner_label}]"
                )
                cls._link_reuse(existing, source_key)
                return existing.final_filename

        pixel_key = None
        features = None
        dims = None
        if cls._pixel_dedup or cls._similarity_dedup:
            started = time.perf_counter()
            analysis = warmed[2] if warmed is not None else None
            reused_prepared_analysis = True
            if not analysis or not analysis.get('dims') or (
                cls._pixel_dedup and analysis.get('pixel_key') is None
            ) or (cls._similarity_dedup and analysis.get('features') is None):
                reused_prepared_analysis = False
                analysis = texture_dedup.analyze_file(
                    source_path,
                    file_md5=md5,
                    need_pixel_key=cls._pixel_dedup,
                    need_features=cls._similarity_dedup,
                )
            cls._profile["source_analysis_s"] += time.perf_counter() - started
            if not reused_prepared_analysis:
                cls._profile["source_decode_s"] += analysis.get("decode_seconds", 0.0)
                if analysis.get("decoded"):
                    cls._profile["source_decode_count"] += 1
                cls._profile["source_feature_s"] += analysis.get("feature_seconds", 0.0)
                if analysis.get("cache_hit"):
                    cls._profile["source_cache_hits"] += 1
            dims = analysis.get("dims")
            pixel_key = analysis.get("pixel_key")
            features = analysis.get("features")

        # 3) 同规格 + 像素完全相同（同组变体不合并）
        if cls._pixel_dedup and pixel_key:
            for existing_id in cls._by_pixel.get((spec_key, pixel_key), ()):
                existing = cls._entries[existing_id]
                if existing.dropped:
                    continue
                cls._stats["reuse_pixel"] += 1
                _debug(
                    f"复用(像素) {os.path.basename(source_path)} -> "
                    f"{existing.final_filename} [{existing.owner_label}]"
                )
                cls._link_reuse(existing, source_key)
                return existing.final_filename

        # 4) 同规格 + 相似度达标（按资源合并节点设置的阈值判定"重复"）
        if cls._similarity_dedup and features is not None and dims is not None:
            compare_started = time.perf_counter()
            try:
                for representative in cls._similarity_reps.get((spec_key, dims), ()):
                    if representative.dropped or representative.features is None:
                        continue
                    cls._profile["source_similarity_pairs"] += 1
                    scores = texture_dedup.similarity_scores(
                        representative.features, features
                    )
                    required = texture_dedup.required_ratio(
                        cls._threshold,
                        preferred_filename,
                        representative.final_filename,
                    )
                    duplicate, combined = texture_dedup.similarity_duplicate(
                        scores, required
                    )
                    if not duplicate:
                        continue
                    cls._stats["reuse_similarity"] += 1
                    _debug(
                        f"复用(相似度 {combined * 100:.2f}% >= {required * 100:.2f}%) "
                        f"{os.path.basename(source_path)} -> {representative.final_filename}"
                    )
                    cls._link_reuse(representative, source_key)
                    return representative.final_filename
            finally:
                cls._profile["source_similarity_s"] += (
                    time.perf_counter() - compare_started
                )

        # 5) 新代表：目标文件名被占用时按旧「先到先得」处理
        target_key = (
            os.path.normcase(os.path.abspath(target_folder)),
            preferred_filename.casefold(),
        )
        occupied_id = cls._by_target.get(target_key)
        if occupied_id is not None:
            occupied = cls._entries[occupied_id]
            if not occupied.dropped:
                cls._stats["reuse_target_name"] += 1
                _debug(
                    f"目标文件名已被占用，先到先得：{preferred_filename} -> "
                    f"{occupied.final_filename}"
                )
                cls._link_reuse(occupied, source_key)
                return occupied.final_filename

        dds_signature = None
        if (
            str(source_path).lower().endswith(".dds")
            and not convert_kwargs
            and (cls._pixel_dedup or cls._similarity_dedup)
        ):
            signature_started = time.perf_counter()
            metadata = texture_dedup.dds_file_metadata(
                source_path, include_signature=True
            )
            if metadata:
                dds_signature = metadata.get("signature")
                if dims is None:
                    dims = metadata.get("dims")
            cls._profile["source_signature_s"] += (
                time.perf_counter() - signature_started
            )

        entry = PlanEntry(
            entry_id=cls._next_id,
            source_path=source_path,
            target_folder=target_folder,
            preferred_filename=preferred_filename,
            owner=owner,
            owner_label=owner_label,
            spec_key=spec_key,
            md5=md5,
            pixels=None,
            dims=dims,
            convert_kwargs=convert_kwargs,
            overwrite_policy=overwrite_policy,
            ini_folder=ini_folder,
            dedup_group=dedup_group,
            pixel_key=pixel_key,
            features=features,
            dds_signature=dds_signature,
        )
        cls._entries.append(entry)
        cls._next_id += 1
        cls._managed_paths_cache = None
        cls._by_source[source_key] = entry.entry_id
        cls._by_target[target_key] = entry.entry_id
        if md5:
            cls._by_md5.setdefault((spec_key, md5), []).append(entry.entry_id)
        if entry.pixel_key:
            cls._by_pixel.setdefault((spec_key, entry.pixel_key), []).append(entry.entry_id)
        if entry.features is not None and dims is not None:
            cls._similarity_reps.setdefault((spec_key, dims), []).append(entry)
        cls._stats["registered"] += 1
        _debug(
            f"新计划项 #{entry.entry_id} {preferred_filename} "
            f"owner={owner} spec={spec_key} 源={os.path.basename(source_path)}"
        )
        return entry.final_filename

    @classmethod
    def _link_reuse(cls, representative, source_key):
        """把复用关系记到代表项上（drop 时级联），并登记源索引。"""
        cls._by_source[source_key] = representative.entry_id

    @staticmethod
    def _same_dedup_group(entry, dedup_group):
        """两个计划项是否属于同一个材质切换组（同组不同档是刻意变体，不合并）。"""
        if not dedup_group:
            return False
        return entry.dedup_group == dedup_group

    # -- 取消（清除未指定部件贴图时用） --------------------------------------

    @classmethod
    def drop_by_ini_filename(cls, relative_filename):
        """按 ini 里的相对文件名（如 ``Textures/aaa.dds``）取消尚未落盘的计划项。

        返回被取消的项数；复用该代表文件的项一并取消（它们共用同一条 ini 引用）。
        """
        normalized = texture_dedup.normalize_relative_filename(relative_filename)
        if not normalized:
            return 0
        removed_ids = set()
        for entry in cls._entries:
            if entry.dropped:
                continue
            if texture_dedup.normalize_relative_filename(entry.ini_filename) != normalized:
                continue
            entry.dropped = True
            entry.materialized = True
            removed_ids.add(entry.entry_id)
        if not removed_ids:
            return 0
        # 级联：复用这些代表的项也取消（它们引用同一个 filename）
        for entry in cls._entries:
            if entry.dropped or entry.reuse_of is None:
                continue
            if entry.reuse_of in removed_ids:
                entry.dropped = True
                entry.materialized = True
        cls._stats["dropped"] += len(removed_ids)
        cls._managed_paths_cache = None
        _debug(f"取消计划项 {sorted(removed_ids)}（{relative_filename}）")
        return len(removed_ids)

    # -- 落盘 ---------------------------------------------------------------

    @classmethod
    def finalize(cls, mod_export_path=None):
        """把所有待落盘的代表项复制 / 转换一次；幂等。返回统计快照。"""
        mod_export_path = str(mod_export_path or cls._mod_export_path or "")
        if not cls._enabled:
            return cls.stats()

        pending = [
            entry for entry in cls._entries
            if entry.reuse_of is None and not entry.dropped and not entry.materialized
        ]
        if not pending:
            return cls.stats()

        total = len(pending)
        target_analysis = cls._preflight_existing_targets(pending)
        batched = cls._materialize_new_dds_batches(pending, total)
        done = len(batched)
        for entry in pending:
            if entry.entry_id in batched:
                entry.materialized = True
                continue
            try:
                cls._materialize_entry(entry, target_analysis.get(entry.entry_id))
            except Exception as exc:
                print(f"[贴图计划] 落盘失败 {entry.preferred_filename}: {exc}")
            entry.materialized = True
            done += 1
            _report_progress(done, total)

        if cls._pending_renames and mod_export_path:
            replaced = texture_dedup.rewrite_ini_filenames(
                mod_export_path, cls._pending_renames
            )
            cls._stats["renamed"] += replaced
            _debug(f"回退改名后重写 ini filename {replaced} 行")
            cls._pending_renames = {}

        _debug(
            "finalize 完成: "
            + " ".join(f"{key}={value}" for key, value in cls._stats.items())
        )
        profile = cls._profile
        print(
            "[TexturePlanProfile] "
            f"elapsed={time.perf_counter() - profile['started_at']:.3f}s "
            f"source_md5={profile['source_md5_s']:.3f}s "
            f"source_analysis={profile['source_analysis_s']:.3f}s "
            f"source_decode={profile['source_decode_s']:.3f}s/{profile['source_decode_count']} "
            f"source_features={profile['source_feature_s']:.3f}s "
            f"source_cache_hits={profile['source_cache_hits']} "
            f"source_signature={profile['source_signature_s']:.3f}s "
            f"source_similarity={profile['source_similarity_s']:.3f}s/"
            f"{profile['source_similarity_pairs']} pairs "
            f"target_md5={profile['target_md5_s']:.3f}s "
            f"target_decode={profile['target_decode_s']:.3f}s/"
            f"{profile['target_decode_count']} "
            f"target_features={profile['target_feature_s']:.3f}s "
            f"target_similarity={profile['target_similarity_s']:.3f}s/"
            f"{profile['target_similarity_checks']} checks "
            f"target_cache_hits={profile['target_cache_hits']} "
            f"target_dim_prefilters={profile['target_dimension_prefilters']} "
            f"target_compressed_exact={profile['target_compressed_exact']} "
            f"target_parallel_workers={profile['target_parallel_workers']} "
            f"target_preflight={profile['target_preflight_s']:.3f}s "
            f"convert={profile['convert_s']:.3f}s copy={profile['copy_s']:.3f}s"
        )
        print("[TexturePlanStats] " + json.dumps(cls.stats(), ensure_ascii=False), flush=True)
        return cls.stats()

    @classmethod
    def _materialize_new_dds_batches(cls, pending, total):
        """新 DDS 目标按格式批量编码；已有目标仍逐项执行覆盖判定。"""
        if cls._converter_override is not None:
            return set()
        requests = []
        for entry in pending:
            target = os.path.join(entry.target_folder, entry.final_filename)
            if entry.convert_kwargs and not os.path.exists(target):
                requests.append((entry.entry_id, entry.source_path, target,
                                 dict(entry.convert_kwargs)))
        if len(requests) < 2:
            return set()
        try:
            from ..toolkit.tt_dds_conversion import convert_new_textures_to_dds_batch
        except ImportError:
            return set()
        started = time.perf_counter()
        try:
            _report_progress(0, total)
            completed = convert_new_textures_to_dds_batch(
                requests, lambda count: _report_progress(count, total))
        except Exception as exc:
            print(f"[贴图计划] 批处理未完成，沿用逐项转换: {exc}")
            completed = set()
        cls._profile["convert_s"] += time.perf_counter() - started
        cls._stats["written"] += len(completed)
        return completed

    @classmethod
    def _preflight_existing_targets(cls, entries):
        """并行分析已有 DDS 目标；最终判定与写入仍严格按登记顺序执行。"""
        if not (cls._pixel_dedup or cls._similarity_dedup):
            return {}
        targets = []
        for entry in entries:
            path = os.path.join(entry.target_folder, entry.final_filename)
            if os.path.isfile(path):
                targets.append((entry, path))
        if not targets:
            return {}

        started = time.perf_counter()
        dds_targets = [
            item for item in targets
            if item[1].lower().endswith(".dds")
        ]
        other_targets = [
            item for item in targets if not item[1].lower().endswith(".dds")
        ]
        prepared = {}
        workers = min(_dds_analysis_worker_limit(cls._pixel_dedup), len(dds_targets))
        if workers > 1:
            cls._profile["target_parallel_workers"] = workers
            with ThreadPoolExecutor(max_workers=workers) as executor:
                futures = [
                    executor.submit(cls._prepare_target_analysis, entry, path, False)
                    for entry, path in dds_targets
                ]
                for (entry, _), future in zip(dds_targets, futures):
                    prepared[entry.entry_id] = future.result()
        else:
            for entry, path in dds_targets:
                prepared[entry.entry_id] = cls._prepare_target_analysis(
                    entry, path, False
                )
        for entry, path in other_targets:
            prepared[entry.entry_id] = cls._prepare_target_analysis(
                entry, path, True
            )

        for result in prepared.values():
            cls._profile["target_md5_s"] += result.get("md5_seconds", 0.0)
            cls._profile["target_decode_s"] += result.get("decode_seconds", 0.0)
            if result.get("decoded"):
                cls._profile["target_decode_count"] += 1
            cls._profile["target_feature_s"] += result.get("feature_seconds", 0.0)
            if result.get("cache_hit"):
                cls._profile["target_cache_hits"] += 1
            if result.get("dimension_prefilter"):
                cls._profile["target_dimension_prefilters"] += 1
            if result.get("compressed_exact"):
                cls._profile["target_compressed_exact"] += 1
        cls._profile["target_preflight_s"] += time.perf_counter() - started
        return prepared

    @classmethod
    def _prepare_target_analysis(cls, entry, target_path, allow_blender):
        result = {
            "md5": None,
            "mode": None,
            "dims": None,
            "pixel_key": None,
            "features": None,
            "md5_seconds": 0.0,
            "decode_seconds": 0.0,
            "feature_seconds": 0.0,
            "decoded": False,
            "cache_hit": False,
            "dimension_prefilter": False,
            "compressed_exact": False,
        }
        started = time.perf_counter()
        result["md5"] = texture_dedup.compute_md5(target_path)
        result["md5_seconds"] = time.perf_counter() - started
        if entry.md5 and result["md5"] and entry.md5 == result["md5"]:
            result["mode"] = "MD5"
            return result
        if not (cls._pixel_dedup or cls._similarity_dedup):
            return result

        if target_path.lower().endswith(".dds"):
            metadata = texture_dedup.dds_file_metadata(target_path)
            if metadata:
                result["dims"] = metadata.get("dims")
                if (
                    entry.dims is not None
                    and result["dims"] is not None
                    and tuple(entry.dims) != tuple(result["dims"])
                ):
                    result["dimension_prefilter"] = True
                    return result
                if entry.dds_signature and not entry.convert_kwargs:
                    signed_metadata = texture_dedup.dds_file_metadata(
                        target_path, include_signature=True
                    )
                    target_signature = (
                        signed_metadata.get("signature") if signed_metadata else None
                    )
                    if target_signature == entry.dds_signature:
                        result["mode"] = "DDS 压缩块"
                        result["compressed_exact"] = True
                        return result

        analysis = texture_dedup.analyze_file(
            target_path,
            file_md5=result["md5"],
            need_pixel_key=cls._pixel_dedup,
            need_features=cls._similarity_dedup,
            allow_blender=allow_blender,
        )
        result.update({
            "dims": analysis.get("dims") or result["dims"],
            "pixel_key": analysis.get("pixel_key"),
            "features": analysis.get("features"),
            "decode_seconds": analysis.get("decode_seconds", 0.0),
            "feature_seconds": analysis.get("feature_seconds", 0.0),
            "decoded": analysis.get("decoded", False),
            "cache_hit": analysis.get("cache_hit", False),
        })
        return result

    @classmethod
    def _materialize_entry(cls, entry, target_analysis=None):
        target_path = os.path.join(entry.target_folder, entry.final_filename)
        target_exists = os.path.exists(target_path)

        if target_exists:
            duplicate_mode = cls._existing_target_duplicate(
                entry, target_path, target_analysis
            )
            if duplicate_mode:
                cls._stats["skip_same_content"] += 1
                _debug(f"跳过(目标文件命中{duplicate_mode}去重) {entry.final_filename}")
                return True, entry.final_filename
            if entry.overwrite_policy != "overwrite":
                # 旧行为：不覆盖已存在的文件（保护用户手工替换的贴图）
                cls._stats["skip_keep_existing"] += 1
                _debug(f"跳过(保留旧文件) {entry.final_filename}")
                return True, entry.final_filename

        os.makedirs(entry.target_folder, exist_ok=True)

        if entry.convert_kwargs:
            converter = cls._converter_override or _load_converter()
            if converter is None:
                # 裁剪环境拿不到 texconv 入口：与转换失败同样回退原样复制
                cls._stats["convert_failed"] += 1
                _debug("texconv 入口不可用，回退原样复制")
                cls._fallback_copy(entry)
                return True, entry.final_filename
            started = time.perf_counter()
            ok, reason = cls._convert(converter, entry, target_path)
            cls._profile["convert_s"] += time.perf_counter() - started
            if not ok:
                cls._stats["convert_failed"] += 1
                _debug(f"转 DDS 失败，回退原样复制 {entry.source_path}: {reason}")
                cls._fallback_copy(entry)
            else:
                cls._stats["written"] += 1
            return True, entry.final_filename

        if not target_exists or entry.overwrite_policy == "overwrite":
            started = time.perf_counter()
            from ..toolkit.png_rgba import copy_png_rgba

            copy_png_rgba(entry.source_path, target_path)
            cls._profile["copy_s"] += time.perf_counter() - started
            cls._stats["written"] += 1
            _debug(f"复制落盘 {entry.final_filename}")
        return True, entry.final_filename

    @classmethod
    def _existing_target_duplicate(cls, entry, target_path, prepared=None):
        """按资源合并节点本轮配置，判断同名旧目标是否与源贴图重复。

        资源合并配置里的 MD5 始终执行；像素级与相似度按对应开关执行。这里只
        比较将要覆盖的这个目标文件，不扫描目录、不改名，也不把其它现存贴图加入
        全局合并。源图与 DDS 的编码字节通常不同，因此不能只靠两者的 MD5 判断。
        """
        prepared = prepared or cls._prepare_target_analysis(
            entry, target_path, True
        )
        if prepared.get("mode"):
            return prepared["mode"]
        target_md5 = prepared.get("md5")
        if entry.md5 and target_md5 and entry.md5 == target_md5:
            return "MD5"

        if not (cls._pixel_dedup or cls._similarity_dedup):
            return None

        target_dims = prepared.get("dims")
        target_pixel_key = prepared.get("pixel_key")
        target_features = prepared.get("features")
        if target_dims is None:
            return None

        if cls._pixel_dedup and entry.pixel_key and target_dims == entry.dims:
            if target_pixel_key and target_pixel_key == entry.pixel_key:
                return "像素级"

        if (
            cls._similarity_dedup
            and entry.features is not None
            and target_dims == entry.dims
        ):
            if target_features is not None:
                started = time.perf_counter()
                scores = texture_dedup.similarity_scores(entry.features, target_features)
                required = texture_dedup.required_ratio(
                    cls._threshold,
                    entry.final_filename,
                    os.path.basename(target_path),
                )
                duplicate, combined = texture_dedup.similarity_duplicate(
                    scores, required
                )
                cls._profile["target_similarity_s"] += time.perf_counter() - started
                cls._profile["target_similarity_checks"] += 1
                if duplicate:
                    return f"相似度 {combined * 100:.2f}% >= {required * 100:.2f}%"
        return None

    @staticmethod
    def _convert(converter, entry, target_path):
        kwargs = dict(entry.convert_kwargs)
        kwargs.pop("_target_path", None)
        try:
            return converter(entry.source_path, target_path, **kwargs)
        except TypeError:
            # 兼容轻量测试桩的更窄签名
            try:
                return converter(entry.source_path, target_path)
            except Exception as exc:  # noqa: BLE001 - 转换器崩了要回退，不能把图丢掉
                return False, f"{type(exc).__name__}: {exc}"
        except Exception as exc:  # noqa: BLE001 - 同上：任何转换异常都必须走回退复制
            return False, f"{type(exc).__name__}: {exc}"

    @classmethod
    def _fallback_copy(cls, entry):
        """转换失败：按源扩展名原样复制，并登记 ini 的 filename 改名。"""
        source_extension = os.path.splitext(entry.source_path)[1]
        stem = os.path.splitext(entry.final_filename)[0]
        fallback_name = f"{stem}{source_extension}"
        fallback_path = os.path.join(entry.target_folder, fallback_name)
        if os.path.exists(entry.source_path):
            from ..toolkit.png_rgba import copy_png_rgba

            copy_png_rgba(entry.source_path, fallback_path)
        if fallback_name != entry.final_filename:
            cls._pending_renames[entry.ini_filename] = (
                f"{entry.ini_folder}/{fallback_name}"
            )
            _debug(
                f"回退改名 {entry.final_filename} -> {fallback_name}"
            )
            entry.final_filename = fallback_name
            cls._managed_paths_cache = None
