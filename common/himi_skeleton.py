"""HIMI (崩坏3 / HI3) merged-skeleton data layer.

Reads a HI3 FrameAnalysis dump recorded by the workspace's
``Config/FrameAnalysisPath.json``, joins every landed component to its deform
pass, deduplicates bone matrices **bitwise across the slots the component
actually references**, and writes the result back into the workspace in the
same shape ZZMI uses (``VGMap`` / ``VGOffset`` / ``VGCount`` + a
``ModImpRuntime`` palette cache) plus one role-level static skeleton binary.

Design authority: ``HIMI骨骼合并计划书.md`` (T5) and
``reports/himi-merge/05-decisions.md``.  Where this module diverges from the
plan, the divergence is called out in a comment and reported to the reviewer.

Zero bpy dependency: importable and testable without Blender.

Measured facts this module is built on (all re-derivable with
``tools/himi_merged_probe.py``):

* ``vs-t0`` of a deform pass is a **bone pool**, not a compact per-part table:
  ``palette_bones == max(BLENDINDICES) + 1`` only holds for 32/34 components.
  The two invariants that do hold everywhere are ``max(index) < pool size``
  and (by construction) *referenced slot set == distinct index set*; the merge
  therefore only ever deduplicates **referenced** slots.
* The pool is ring scratch: one resource hash can serve many deform passes with
  different contents, so the palette file is located by the **logical dump
  name of that draw**, never by resource hash.
* A palette's 3x3 block is ``scale * orthogonal`` with one uniform scalar
  ``s = cbrt(|det|)`` and ``s in {0.93, 1.0, 1.6}`` -- never hardcode 1.6, and
  never validate a rotation row by ``length ~= 1``.
"""

from __future__ import annotations

import hashlib
import os
import re
from collections import defaultdict
from dataclasses import dataclass, field

import numpy

from ..utils.json_utils import JsonUtils

# --------------------------------------------------------------------------- #
# 常量（全部来自 dump 实测）
# --------------------------------------------------------------------------- #

#: palette 步长：12 float = 3x4 行主序仿射矩阵
PALETTE_STRIDE_BYTES = 48
PALETTE_FLOATS = 12
#: Blend 记录步长：4xf32 权重 + 4xi32 索引
BLEND_STRIDE_BYTES = 32
#: deform pass 的 vb0 步长（P12 N12 C4 TA16）
POSITION_STRIDE_BYTES = 44
#: HIMI 缓存算法版本（与 ZZMI 的 VGMapAlgorithmVersion 独立演进）
HIMI_VG_MAP_ALGORITHM_VERSION = 1
#: HIMI 骨架槽位健全性上限（SINT 可承载 2^31-1，这里只做防御性上限）
HIMI_MERGED_SKELETON_SLOT_LIMIT = 65535
#: 角色级合并骨架元数据文件名
HIMI_MERGED_SKELETON_METADATA_FILE = "HIMIMergedSkeleton.json"
#: 门控 G1 的语义版本（字段 HIMIDeformGateSafe）
HIMI_DEFORM_GATE_VERSION = 1

#: 站位（stub）机制：对象/网格标记键 + 网格名前缀（与 ZZMI 的 ZZMI_STUB 同构）
HIMI_STUB_MARKER = "HIMI_STUB"
HIMI_STUB_MESH_PREFIX = "HIMI_STUB_MESH_"
#: 站位三角的 3 个顶点（极限小三角，游戏内不可见）
#: **不是** ``ZZMI_STUB_PREFIX_ROWS``：那个常量描述「重定向计划的前缀行数」，
#: HIMI 无重定向 ⇒ 不引入它（计划书 §7.7.5 末两行 / T19 §7 边界②）。
HIMI_STUB_TRIANGLE_VERTICES = (
    (0.0, 0.0, 0.0),
    (1e-6, 0.0, 0.0),
    (0.0, 1e-6, 0.0),
)
HIMI_STUB_TRIANGLE_FACES = ((0, 1, 2),)
#: 站位对象的权重组名：单组对象经 ``_prepare_merged_skeleton_vertex_groups``
#: 的「补缺 + 按名排序 + 重命名 str(index)」后 index 恒为 0 ⇒ 恒落在**全局槽 0**。
#: 所以站位**不新增槽位**（计划书 §7.7.5）。
HIMI_STUB_SLOT_GROUP_NAME = "0"
#: ``LOD0/DrawIB-Component.json``：IB 家族 → 全部候选成员（含从未落盘）
HIMI_COMPONENT_MAP_BASENAME = "DrawIB-Component.json"

#: 站位决策原因（诊断与测试共用，避免散落的字面量）
HIMI_STUB_REASON_ALREADY_PRESENT = "already_present"
HIMI_STUB_REASON_ABSORBED = "absorbed"
HIMI_STUB_REASON_ZERO_REFERENCE = "zero_reference"
HIMI_STUB_REASON_DEDUP_EXCLUDED = "dedup_excluded"
HIMI_STUB_REASON_NO_VGMAP = "no_vgmap"
HIMI_STUB_REASON_NO_EXCLUSIVE = "no_exclusive"
HIMI_STUB_REASON_NO_DECLARED = "no_declared_namespace"
HIMI_STUB_REASON_NO_FAMILY_MAP = "no_family_map"
HIMI_STUB_REASON_NO_USED = "no_used"
HIMI_STUB_REASON_WRITERLESS = "writerless_family"
#: t107：本次导出只允许规则①（普通模式），该家族落在规则②/④ 的地盘 ⇒ 不判定。
HIMI_STUB_REASON_RULE_GATED = "rule_gated"

DUMP_LINE_RE = re.compile(
    r"^(\d{6})\s+3DMigoto Dumping (?:Buffer|Texture2D) .*[\\/]([^\\/]+) -> .*[\\/]([^\\/]+)$"
)
SRC_SLOT_RE = re.compile(r"^(\d{6})-(vb(\d+)|ib|vs-t0)=([0-9A-Za-z_-]+)-")
DST_VB_RE = re.compile(
    r"-vb(\d+)-layout=\w+-topology=(\w+)-stride=(\d+)(?:-first=\d+)?-count=(\d+)\.txt$"
)
DRAWCALL_RE = re.compile(r"^(\d{6})\s+(.*)$")
DRAW_RE = re.compile(r"^Draw\(VertexCount:(\d+),\s*StartVertexLocation:(-?\d+)\)")
DRAW_INDEXED_RE = re.compile(
    r"^DrawIndexed\(IndexCount:(\d+),\s*StartIndexLocation:(\d+),\s*BaseVertexLocation:(-?\d+)\)"
)
_HASH_TOKEN = r"([0-9A-Fa-f]+)"
CONTINUATION_HASH_RE = re.compile(rf"hash={_HASH_TOKEN}")
VS_TAG_RE = re.compile(rf"-vs={_HASH_TOKEN}")


def bare_from_unique_str(unique_str: str) -> str:
    """``LOD0.22af2a30-63273-0`` / ``22af2a30-63273-0`` -> ``22af2a30-63273-0``."""
    text = str(unique_str or "").strip()
    if "." in text:
        head, tail = text.split(".", 1)
        if head.upper().startswith("LOD"):
            return tail
    return text


def lod_from_unique_str(unique_str: str) -> str:
    """``LOD0.22af2a30-63273-0`` -> ``LOD0``; 无前缀 -> ``''``."""
    text = str(unique_str or "").strip()
    if "." in text:
        head = text.split(".", 1)[0]
        if head.upper().startswith("LOD"):
            return head
    return ""


def draw_ib_from_bare(bare: str) -> str:
    """组件名的前缀段即该组件的 ``DrawIB``（= IB 家族名）。"""
    text = str(bare or "").strip()
    return text.split("-", 1)[0] if "-" in text else text


def _sha256_hex(payload: bytes) -> str:
    return hashlib.sha256(payload).hexdigest()


def _normalize_written_map(written: dict, target_set) -> dict[int, int]:
    """已落盘组件名 -> 在目标集合里的下标（确定性序号）。"""
    ordered = sorted(str(name) for name in (target_set or ()) if str(name) in written)
    return {name: index for index, name in enumerate(ordered)}


# --------------------------------------------------------------------------- #
# 1) log 解析
# --------------------------------------------------------------------------- #


class HIMILogParser:
    """Parse a HI3 FrameAnalysis ``log.txt`` into per-draw records.

    Follows the ZZMI parser's state machine (draw-prefixed resource description
    lines + pending continuation hash) with two HIMI-specific rules:

    * per-draw vertex shader **must** come from the dump file name's ``-vs=``
      suffix -- ``VSSetShader`` is state-deduplicated and the first one inside a
      draw block is frequently the previous pass' shader;
    * ``SOSetTargets`` normally appears twice per deform pass; only the call
      that is followed by a resource continuation line carries the actual
      stream-output target hash.
    """

    def __init__(self, log_path: str):
        self.log_path = str(log_path)
        self.draws: dict[str, dict] = {}
        self._parse()

    # -- parsing ---------------------------------------------------------- #

    def _record(self, draw: str) -> dict:
        return self.draws.setdefault(
            draw,
            {
                "draw": draw,
                "vb": {},
                "class": "",
                "vertex_count": None,
                "start_vertex": None,
                "index_count": None,
                "start_index": None,
                "base_vertex": None,
                "vertex_shader": "",
                "vs_t0": None,
                "so_targets": [],
            },
        )

    def _parse(self) -> None:
        try:
            with open(self.log_path, "r", encoding="utf-8", errors="replace") as handle:
                text = handle.read()
        except OSError:
            return
        lines = text.splitlines()

        # 第 1 遍：资源描述行（dump 逻辑名 -> deduped 目标 + 布局元数据）
        for raw in lines:
            line = raw.rstrip()
            match = DUMP_LINE_RE.match(line)
            if not match:
                continue
            draw, source_name, dest_name = match.group(1), match.group(2), match.group(3)
            entry = self._record(draw)
            if not entry["vertex_shader"]:
                vs_match = VS_TAG_RE.search(source_name)
                if vs_match:
                    entry["vertex_shader"] = vs_match.group(1)
            slot_match = SRC_SLOT_RE.match(source_name)
            if not slot_match:
                continue
            slot = slot_match.group(2)
            resource_hash = slot_match.group(4)
            if slot.startswith("vb"):
                index = int(slot[2:])
                record = entry["vb"].setdefault(index, {})
                if dest_name.lower().endswith(".txt"):
                    layout = DST_VB_RE.search(dest_name)
                    if layout:
                        record["layout"] = layout.group(1)
                        record["topology"] = layout.group(2)
                        record["stride"] = int(layout.group(3))
                        record["count"] = int(layout.group(4))
                else:
                    record["resource_hash"] = resource_hash
                    record["deduped_dump"] = os.path.basename(dest_name)
            elif slot == "ib":
                if not dest_name.lower().endswith(".txt"):
                    entry.setdefault("ib", {})["resource_hash"] = resource_hash
                    entry["ib"]["deduped_dump"] = os.path.basename(dest_name)
            elif slot == "vs-t0":
                if not dest_name.lower().endswith(".txt"):
                    entry["vs_t0"] = {
                        "resource_hash": resource_hash,
                        "deduped_dump": os.path.basename(dest_name),
                    }

        # 第 2 遍：调用行状态机（SO 目标续行 / draw 形态 / IB 继承）
        pending_so_draw: str | None = None
        for raw in lines:
            line = raw.rstrip()
            match = DRAWCALL_RE.match(line)
            if match:
                draw, rest = match.group(1), match.group(2)
                if rest.startswith("3DMigoto"):
                    continue
                entry = self._record(draw)
                if rest.startswith("SOSetTargets"):
                    pending_so_draw = draw
                    continue
                draw_match = DRAW_RE.match(rest)
                if draw_match:
                    entry["class"] = "Draw"
                    entry["vertex_count"] = int(draw_match.group(1))
                    entry["start_vertex"] = int(draw_match.group(2))
                    pending_so_draw = None
                    continue
                indexed_match = DRAW_INDEXED_RE.match(rest)
                if indexed_match:
                    entry["class"] = "DrawIndexed"
                    entry["index_count"] = int(indexed_match.group(1))
                    entry["start_index"] = int(indexed_match.group(2))
                    entry["base_vertex"] = int(indexed_match.group(3))
                    pending_so_draw = None
                    continue
                continue

            if pending_so_draw is None:
                continue
            if not line.startswith((" ", "\t")):
                pending_so_draw = None
                continue
            cont = re.match(r"^\s*(\d+):\s*(.*)$", line)
            if not cont:
                continue
            hash_match = CONTINUATION_HASH_RE.search(cont.group(2))
            if not hash_match:
                continue
            self._record(pending_so_draw)["so_targets"].append(hash_match.group(1))
            pending_so_draw = None

    # -- queries ---------------------------------------------------------- #

    @property
    def log_signature(self) -> str:
        """Full sha256 of ``log.txt``; the cache's frame-identity gate."""
        try:
            with open(self.log_path, "rb") as handle:
                return _sha256_hex(handle.read())
        except OSError:
            return ""

    def _is_deform_shaped(self, entry: dict) -> bool:
        if not entry.get("so_targets"):
            return False
        if entry.get("class") != "Draw":
            return False
        if not entry.get("vertex_count"):
            return False
        vb = entry.get("vb", {})
        vb0, vb1 = vb.get(0, {}), vb.get(1, {})
        if vb0.get("stride") != POSITION_STRIDE_BYTES or vb1.get("stride") != BLEND_STRIDE_BYTES:
            return False
        if vb0.get("topology") != "pointlist" or vb1.get("topology") != "pointlist":
            return False
        palette = entry.get("vs_t0") or {}
        # D02：必须先判 deform pass 再取 vs-t0；该槽在 render 块是 Texture2D
        return bool(palette.get("deduped_dump", "").lower().endswith(".buf"))

    def deform_passes(self) -> dict[str, dict]:
        """Deform passes keyed by draw id, with palette/vb0/vb1 identities."""
        passes: dict[str, dict] = {}
        for draw, entry in self.draws.items():
            if not self._is_deform_shaped(entry):
                continue
            vb = entry["vb"]
            palette = entry.get("vs_t0") or {}
            passes[draw] = {
                "draw": draw,
                "draw_index": int(draw),
                "vertex_count": int(entry["vertex_count"]),
                "so_target": entry["so_targets"][0],
                "all_so_targets": list(entry["so_targets"]),
                "vertex_shader": entry.get("vertex_shader", ""),
                "position_content_hash": vb[0].get("resource_hash", ""),
                "position_dump": vb[0].get("deduped_dump", ""),
                "blend_content_hash": vb[1].get("resource_hash", ""),
                "blend_dump": vb[1].get("deduped_dump", ""),
                "palette_content_hash": palette.get("resource_hash", ""),
                "palette_dump": palette.get("deduped_dump", ""),
            }
        return passes

    def render_draws(self) -> dict[str, dict]:
        """Indexed draws keyed by draw id (rendering consumers of the SO output)."""
        drawn: dict[str, dict] = {}
        for draw, entry in self.draws.items():
            if entry.get("class") != "DrawIndexed":
                continue
            vb = entry.get("vb", {})
            drawn[draw] = {
                "draw": draw,
                "index_count": entry.get("index_count"),
                "vb0_content_hash": vb.get(0, {}).get("resource_hash", ""),
                "ib_content_hash": (entry.get("ib") or {}).get("resource_hash", ""),
                "vertex_shader": entry.get("vertex_shader", ""),
            }
        return drawn

    def so_consumers(self) -> dict[str, dict]:
        """SO target hash -> ``{"draws": [...], "ib_families": {...}}``.

        A deform pass writes its SO into the part's render VB, so the consumers
        are exactly the render draws that bind that hash as ``vb0``.
        """
        consumers: dict[str, dict] = defaultdict(lambda: {"draws": [], "ib_families": {}})
        for draw, entry in sorted(self.render_draws().items()):
            vb0 = entry.get("vb0_content_hash", "")
            if not vb0:
                continue
            bucket = consumers[vb0]
            bucket["draws"].append(draw)
            family = entry.get("ib_content_hash", "")
            bucket["ib_families"][family] = bucket["ib_families"].get(family, 0) + 1
        return dict(consumers)


# --------------------------------------------------------------------------- #
# 2) 反查 / join / 门控
# --------------------------------------------------------------------------- #


class HIMIComponent:
    """One landed workspace component (a bare submesh folder)."""

    __slots__ = (
        "unique_str",
        "bare",
        "draw_ib",
        "json_path",
        "json_dir",
        "json_payload",
        "category_hash",
        "vertex_limit_vb",
        "blend_path",
        "position_path",
        "deform_draw",
        "deform_pass",
        "palette_path",
        "join_path",
        "gpu_preskinning",
        "dedup_excluded",
    )

    def __init__(self, unique_str: str, json_path: str, json_payload: dict):
        self.unique_str = str(unique_str)
        self.bare = bare_from_unique_str(unique_str)
        self.draw_ib = draw_ib_from_bare(self.bare)
        self.json_path = str(json_path)
        self.json_dir = os.path.dirname(os.path.abspath(json_path))
        self.json_payload = dict(json_payload or {})
        self.category_hash = dict(self.json_payload.get("CategoryHash", {}) or {})
        self.vertex_limit_vb = str(self.json_payload.get("VertexLimitVB", "") or "").strip()
        # GPU-PreSkinning 三态：缺失 ⇒ 视为参与（保守）；显式 False ⇒ CPU/无顶点组不参与。
        # 与 EFMI/ZZMI 同款判据（efmi_skeleton.py:4400、zzmi_skeleton.py:1334）。
        self.gpu_preskinning = self.json_payload.get("GPU-PreSkinning") is not False
        # 组件级「排除去重」：与 EFMI/ZZMI 同名同义（json 键 VGMapDedupExcluded）。
        self.dedup_excluded = bool(self.json_payload.get("VGMapDedupExcluded"))
        self.blend_path = os.path.join(self.json_dir, f"{self.bare}-Blend.buf")
        self.position_path = os.path.join(self.json_dir, f"{self.bare}-Position.buf")
        self.deform_draw = ""
        self.deform_pass: dict = {}
        self.palette_path = ""
        self.join_path = ""

    # -- pass-throughs used by the merge builder -- #

    @property
    def blend_content_hash(self) -> str:
        return str(self.category_hash.get("Blend", "") or "").strip()

    @property
    def position_content_hash(self) -> str:
        return str(self.category_hash.get("Position", "") or "").strip()


class HIMIDeformResolver:
    """Join landed components to deform passes and apply the G1 family gate."""

    def __init__(self, dump_dir: str, components: list[HIMIComponent]):
        self.dump_dir = str(dump_dir or "")
        self.deduped_dir = os.path.join(self.dump_dir, "deduped")
        self.log_path = os.path.join(self.dump_dir, "log.txt")
        self.components = list(components or [])
        self.parser = HIMILogParser(self.log_path) if os.path.isfile(self.log_path) else None
        self.passes: dict[str, dict] = self.parser.deform_passes() if self.parser else {}
        self.so_consumers: dict[str, dict] = self.parser.so_consumers() if self.parser else {}
        self.rejections: list[dict] = []
        self._resolve()

    # -- helpers ---------------------------------------------------------- #

    def palette_dump_path(self, deform_pass: dict) -> str:
        """Locate the palette **content** file for a pass.

        Uses the logical dump file name of that draw (``<content>.buf`` under
        ``deduped/``); never a resource-hash lookup -- the hash is a
        description-level identity that several passes share with different
        contents.
        """
        name = str((deform_pass or {}).get("palette_dump", "") or "")
        if not name:
            return ""
        candidate = os.path.join(self.deduped_dir, name)
        return candidate if os.path.isfile(candidate) else ""

    def blend_dump_path(self, deform_pass: dict) -> str:
        name = str((deform_pass or {}).get("blend_dump", "") or "")
        if not name:
            return ""
        candidate = os.path.join(self.deduped_dir, name)
        return candidate if os.path.isfile(candidate) else ""

    def family_members(self, draw_ib: str, skip_bares=None) -> list[str]:
        """Landed component bares of one IB family (sorted).

        ``skip_bares`` drops components that do not take part in the merge at all
        (``GPU-PreSkinning=False``): they must not make their family look
        incomplete, otherwise a CU-less sibling would block the whole family.
        """
        key = str(draw_ib or "").strip()
        skip = {str(name) for name in (skip_bares or ())}
        return sorted(
            c.bare for c in self.components if c.draw_ib == key and c.bare not in skip
        )

    def family_so_targets(self, draw_ib: str) -> list[str]:
        key = str(draw_ib or "").strip()
        return sorted(
            {c.deform_pass.get("so_target", "") for c in self.components if c.draw_ib == key}
            - {""}
        )

    # -- core ------------------------------------------------------------- #

    def _pick_pass(
        self, component: HIMIComponent, by_position: dict, by_so: dict, by_family: dict
    ) -> tuple[dict, str]:
        """Path A/B/C resolution (R1).

        A: ``CategoryHash.Position`` == the pass' vb0 content hash
        B: ``VertexLimitVB`` == the pass' SO target hash
        C: the pass' SO target is bound as ``vb0`` by a render draw whose IB is
           this component's ``DrawIB`` (the IB gate must be applied first)
        """
        position = component.position_content_hash
        so_target = component.vertex_limit_vb
        if position:
            candidates = list(by_position.get(position, {}).values())
            if len(candidates) == 1:
                return candidates[0], "A"
        if so_target:
            candidates = list(by_so.get(so_target, {}).values())
            if len(candidates) == 1:
                return candidates[0], "B"
        family = component.draw_ib
        if family:
            candidates = list(by_family.get(family, {}).values())
            if len(candidates) == 1:
                return candidates[0], "C"
        return {}, ""

    def _resolve(self) -> None:
        by_position: dict[str, dict] = defaultdict(dict)
        by_so: dict[str, dict] = defaultdict(dict)
        by_family: dict[str, dict] = defaultdict(dict)
        for draw, record in self.passes.items():
            by_position[record.get("position_content_hash", "")][draw] = record
            by_so[record.get("so_target", "")][draw] = record

        # 路径 C 的 IB 门控：SO 目标被某 render draw 绑为 vb0，且该 draw 的 IB
        # 属于本组件的 DrawIB。SO 目标只被单一 IB 家族消费（实测 39/39），
        # 因此一个家族最多落一个 pass。
        for so_target, bucket in self.so_consumers.items():
            families = sorted(str(f) for f in bucket.get("ib_families", {}))
            if len(families) != 1:
                continue
            family = families[0]
            passes_for_so = list(by_so.get(so_target, {}).values())
            if len(passes_for_so) == 1:
                by_family[family][passes_for_so[0]["draw"]] = passes_for_so[0]

        for component in self.components:
            record, path = self._pick_pass(component, by_position, by_so, by_family)
            if not record:
                self.rejections.append(
                    {
                        "bare": component.bare,
                        "draw_ib": component.draw_ib,
                        "reason": "no_deform_pass",
                        "detail": (
                            "该组件在工作空间 json 的三条 join 键（CategoryHash.Position / "
                            "VertexLimitVB / IB 家族）上都没有命中同帧 deform pass"
                        ),
                    }
                )
                continue
            component.deform_draw = record["draw"]
            component.deform_pass = record
            component.palette_path = self.palette_dump_path(record)
            component.join_path = path
            if not component.palette_path:
                self.rejections.append(
                    {
                        "bare": component.bare,
                        "draw_ib": component.draw_ib,
                        "reason": "palette_dump_missing",
                        "detail": f"deform pass {record['draw']} 的 vs-t0 内容文件缺失",
                    }
                )

    def joined_components(self) -> list[HIMIComponent]:
        return [c for c in self.components if c.palette_path]

    def family_gate(self, target_bares, skip_bares=None) -> dict:
        """G1 family gate.

        A deform pass writes into the render VB of *every* draw of one IB
        family, so rebinding it to the merged skeleton switches the whole family
        to global indices.  The gate therefore requires every **participating**
        landed member of the family to be inside the merge target set.
        ``skip_bares`` excludes components that do not participate at all
        (``GPU-PreSkinning=False``).

        Returns ``{draw_ib: {"family": ..., "members": [...], "target": [...],
        "missing": [...], "safe": bool}}``.
        """
        target = {str(name).strip() for name in (target_bares or ()) if str(name).strip()}
        report: dict[str, dict] = {}
        for draw_ib in sorted({c.draw_ib for c in self.joined_components()}):
            members = self.family_members(draw_ib, skip_bares=skip_bares)
            selected = [name for name in members if name in target]
            missing = [name for name in members if name not in target]
            report[draw_ib] = {
                "family": draw_ib,
                "members": members,
                "target": selected,
                "missing": missing,
                "safe": bool(members) and not missing,
            }
        return report

    def pass_families(self, deform_pass: dict, skip_bares=None) -> list[str]:
        """IB families whose components share this deform pass (sorted)."""
        draw = (deform_pass or {}).get("draw", "")
        if not draw:
            return []
        skip = {str(name) for name in (skip_bares or ())}
        return sorted(
            {
                c.draw_ib
                for c in self.joined_components()
                if c.deform_pass.get("draw") == draw and c.bare not in skip
            }
        )

    def gate_allows(self, deform_pass: dict, gate_report: dict, skip_bares=None) -> bool:
        """**Pass-level** gate: true when every family sharing this pass is safe.

        A pass is only safe to rebind when *all* of its consumer families are
        fully inside the merge target set.  If any family is incomplete the whole
        pass stays on the original palette, **including the components of a
        family that is itself complete** -- rebinding is per-pass, so a safe
        family sharing a pass with an unsafe one would otherwise be switched to
        global indices while its pass-mate still expects local ones.

        Called by :meth:`HIMISkeletonMergeHelper.prepare` and by the export side
        whenever it recomputes ``HIMIDeformGateSafe`` for its own export set.
        """
        families = self.pass_families(deform_pass, skip_bares=skip_bares)
        if not families:
            return False
        return all(bool((gate_report.get(family) or {}).get("safe")) for family in families)


# --------------------------------------------------------------------------- #
# 3) 骨骼池 -> 全局去重
# --------------------------------------------------------------------------- #


def read_blend_records(blend_path: str) -> tuple[numpy.ndarray, numpy.ndarray]:
    """Read ``(weights (N,4) float32, indices (N,4) int32)`` from a Blend buffer."""
    with open(blend_path, "rb") as handle:
        raw = handle.read()
    if not raw or len(raw) % BLEND_STRIDE_BYTES:
        empty_w = numpy.zeros((0, 4), dtype=numpy.float32)
        empty_i = numpy.zeros((0, 4), dtype=numpy.int32)
        return empty_w, empty_i
    records = numpy.frombuffer(raw, dtype=numpy.uint8).reshape(-1, BLEND_STRIDE_BYTES)
    weights = records[:, 0:16].copy().view(numpy.float32).reshape(-1, 4)
    indices = records[:, 16:32].copy().view(numpy.int32).reshape(-1, 4)
    return weights, indices


def read_blend_indices(blend_path: str) -> numpy.ndarray:
    """Read the 4 int32 BLENDINDICES of every record (``(N, 4)``)."""
    return read_blend_records(blend_path)[1]


class HIMIBoneMapBuilder:
    """Turn palette pools + BLENDINDICES into a merged skeleton."""

    @staticmethod
    def load_palette(palette_path: str) -> numpy.ndarray:
        """Read a palette as ``(N, 12)`` float32 (12 floats = one 3x4 matrix)."""
        with open(palette_path, "rb") as handle:
            raw = handle.read()
        if not raw or len(raw) % PALETTE_STRIDE_BYTES:
            return numpy.zeros((0, PALETTE_FLOATS), dtype=numpy.float32)
        return numpy.frombuffer(raw, dtype=numpy.float32).reshape(-1, PALETTE_FLOATS)

    @staticmethod
    def palette_pool_slot_count(palette_path: str) -> int:
        try:
            size = os.path.getsize(palette_path)
        except OSError:
            return 0
        return size // PALETTE_STRIDE_BYTES if size % PALETTE_STRIDE_BYTES == 0 else 0

    @classmethod
    def referenced_slots(cls, blend_path: str) -> dict:
        """Referenced local slots + index statistics for one component.

        ``slots`` = every **distinct non-negative** value appearing in the
        four BLENDINDICES channels (regardless of weight).  This is the口径 that
        makes the two invariants of HIMI骨骼合并计划书.md §2.4 hold on real data
        (``max(index) < pool size`` 34/34, referenced-slot count == distinct
        index count 34/34); a channel with weight ``0`` still stores an index
        inside the pool, so keeping it cannot widen the slot range, and keeping
        it guarantees the export side can map every value it may read back
        through ``VGMap``.

        ``slots_weight_positive`` is the stricter subset that actually
        contributes weight; it is recorded for diagnostics and for the rigid
        part gate (``len == 1`` means "single bone"), which must not be fooled
        by zero-weight channels.

        The merge only ever consumes these sets, because the palette pool also
        contains slots belonging to other components.
        """
        weights, indices = read_blend_records(blend_path)
        if indices.size == 0:
            return {
                "vertex_count": 0,
                "slots": [],
                "slots_weight_positive": [],
                "distinct_count": 0,
                "index_max": -1,
                "index_min": -1,
                "index_max_used": -1,
                "negative_index_count": 0,
            }
        flat = indices.reshape(-1)
        valid = (weights > 0.0).reshape(-1)
        used = flat[valid]
        all_values = {int(v) for v in flat.tolist()}
        return {
            "vertex_count": int(indices.shape[0]),
            "slots": sorted(v for v in all_values if v >= 0),
            "slots_weight_positive": sorted({int(v) for v in used.tolist() if int(v) >= 0}),
            "distinct_count": int(len(all_values)),
            "index_max": int(flat.max()),
            "index_min": int(flat.min()),
            "index_max_used": int(used.max()) if used.size else -1,
            "negative_index_count": int((flat < 0).sum()),
        }

    @staticmethod
    def palette_scale(palette_path: str) -> float:
        """Uniform scale ``s = cbrt(|det(A)|)`` of the palette's 3x3 blocks.

        Median over blocks, so a single odd slot cannot move the value.  HIMI
        measures ``s in {0.93, 1.0, 1.6}``; callers must never assume 1.0.
        """
        matrix = HIMIBoneMapBuilder.load_palette(palette_path)
        if matrix.shape[0] == 0:
            return 0.0
        blocks = matrix.reshape(-1, 3, 4)[:, :, 0:3].astype(numpy.float64)
        determinant = numpy.abs(numpy.linalg.det(blocks))
        scale = numpy.cbrt(determinant)
        return float(numpy.median(scale))

    @staticmethod
    def scale_aware_row_check(palette_path: str, atol: float = 1e-3) -> dict:
        """Validate the 3x3 blocks **modulo the measured scale**.

        A naive ``|row| ~= 1`` check fails on 4/5 roles because their matrices
        carry a 1.6x uniform scale; dividing by ``s`` first is the scale-aware
        form.
        """
        matrix = HIMIBoneMapBuilder.load_palette(palette_path)
        if matrix.shape[0] == 0:
            return {"rows": 0, "unit_fraction": 0.0, "max_deviation": 0.0, "scale": 0.0}
        blocks = matrix.reshape(-1, 3, 4)[:, :, 0:3].astype(numpy.float64)
        scale = HIMIBoneMapBuilder.palette_scale(palette_path)
        divisor = scale if scale > 0 else 1.0
        lengths = numpy.linalg.norm(blocks / divisor, axis=2)
        deviation = numpy.abs(lengths - 1.0)
        return {
            "rows": int(lengths.size),
            "unit_fraction": float(numpy.isclose(deviation, 0.0, atol=atol).mean()),
            "max_deviation": float(deviation.max()),
            "scale": float(scale),
        }

    @classmethod
    def build_merged_skeleton(cls, parts: list[dict]) -> dict:
        """Bitwise-deduplicate referenced slots across components of one role.

        ``parts`` items: ``{"bare": str, "palette_path": str,
        "slots": [int, ...], "pool_slots": int, "palette_scale": float,
        "dedup_excluded": bool}``.
        Parts are processed in ``sorted(bare)`` order so slot assignment is
        deterministic and reproducible between import and export.

        Only the **referenced** slots are registered.  ``VGMap`` additionally
        covers the *gap* slots in ``0..max(slots)`` (mapped to a synthetic
        out-of-range value) because the importer requires ``VGMap`` to be a
        complete ``0..VGCount-1`` cover; gap slots are never read by any vertex
        because no valid BLENDINDICES channel points at them.

        ``dedup_excluded`` (json key ``VGMapDedupExcluded``) mirrors the
        EFMI/ZZMI component-level opt-out: an excluded component claims its own
        private slots and **never** shares one with another component.  It is
        applied by registering every excluded component's slots *before* the
        deduplicating pass, so a later component cannot bind into those slots
        while the excluded component itself still reuses previously registered
        identical matrices.
        """
        ordered = sorted(parts, key=lambda part: str(part.get("bare", "")))
        signature_to_slot: dict[bytes, int] = {}
        slot_records: list[dict] = []
        slot_seen_by: dict[int, list[str]] = defaultdict(list)
        per_part: dict[str, dict] = {}
        overlaps: list[dict] = []

        def _materialize() -> tuple[list[dict], dict[str, dict]]:
            """按 ``ordered`` 顺序把入参解析成每部件的工作记录（不注册签名）。"""
            resolved: list[dict] = []
            meta: dict[str, dict] = {}
            for part in ordered:
                bare = str(part.get("bare", ""))
                palette_path = str(part.get("palette_path", "") or "")
                slots = sorted({int(v) for v in (part.get("slots") or []) if int(v) >= 0})
                with open(palette_path, "rb") as handle:
                    raw = handle.read()
                pool_slots = len(raw) // PALETTE_STRIDE_BYTES
                resolved.append(
                    {
                        "bare": bare,
                        "raw": raw,
                        "slots": slots,
                        "pool_slots": pool_slots,
                        "palette_path": palette_path,
                        "dedup_excluded": bool(part.get("dedup_excluded")),
                    }
                )
                meta[bare] = {
                    "bare": bare,
                    "palette_path": palette_path,
                    "palette_pool_slots": pool_slots,
                    "referenced_slots": slots,
                    "dedup_excluded": bool(part.get("dedup_excluded")),
                }
            return resolved, meta

        def _extend_bone_record(slot: int, bare: str, local: int) -> None:
            slot_records[slot]["source_components"].append(bare)
            slot_records[slot]["source_local"].append(local)

        def _register(raw: bytes, bare: str, local: int, *, fresh: bool) -> int:
            """注册一根骨（``fresh=True`` 时无条件新建槽位）。

            槽位号取 ``len(slot_records)``（已分配槽数），**不能**取
            ``len(signature_to_slot)``：排除去重会让「同一签名占多个槽」，
            此时 dict 长度与已分配槽数不再相等，用它取号会覆盖已有槽的记录。
            """
            signature = raw[local * PALETTE_STRIDE_BYTES : (local + 1) * PALETTE_STRIDE_BYTES]
            if fresh:
                slot = len(slot_records)
                signature_to_slot[signature] = slot
                frozen_slots.add(slot)
                slot_records.append(
                    {
                        "slot": slot,
                        "signature": signature,
                        "source_components": [bare],
                        "source_local": [local],
                    }
                )
                slot_seen_by[slot].append(bare)
                return slot
            slot = signature_to_slot.get(signature)
            # 被排除去重的组件**独占**自己的槽位：它的矩阵不得被任何组件绑进来。
            # （同签名各占一个槽，因此同一矩阵可能出现多个槽位；这正是「排除去重」
            #  的语义，共享槽计数只会统计真正的跨组件共享。）
            if slot is None or slot in frozen_slots:
                slot = len(slot_records)
                signature_to_slot[signature] = slot
                slot_records.append(
                    {
                        "slot": slot,
                        "signature": signature,
                        "source_components": [bare],
                        "source_local": [local],
                    }
                )
            else:
                _extend_bone_record(slot, bare, local)
            slot_seen_by[slot].append(bare)
            return slot

        resolved, part_meta = _materialize()

        # 被排除去重组件独占的槽位（任何别的组件都不得绑进来）。
        frozen_slots: set[int] = set()
        # **单趟注册**：严格按 ``sorted(bare)`` 顺序，每个组件按自己的
        # ``dedup_excluded`` 决定「新建独占槽」还是「复用已有槽」。
        #
        # 必须是单趟而非「排除组件先占坑、其余后处理」：后者会让槽位分配取决于
        # **谁带排除标记**，而排除标记是上一次写回的结果 ⇒ 第二次运行（标记已在
        # json 里）会算出不同的 VGMap，破坏幂等与缓存门。单趟下「按名字序处理 +
        # 每个组件只用自己的排除标记」是纯粹的确定性函数。
        local_maps: dict[str, dict[int, int]] = {}
        shared_by_part: dict[str, list[int]] = defaultdict(list)
        new_by_part: dict[str, list[int]] = defaultdict(list)
        for item in resolved:
            bare = item["bare"]
            excluded = bool(item["dedup_excluded"])
            mapping: dict[int, int] = {}
            for local in item["slots"]:
                if local >= item["pool_slots"]:
                    continue
                before = signature_to_slot.get(
                    item["raw"][local * PALETTE_STRIDE_BYTES : (local + 1) * PALETTE_STRIDE_BYTES]
                )
                slot = _register(item["raw"], bare, local, fresh=excluded)
                if excluded:
                    new_by_part[bare].append(slot)
                elif before is not None:
                    shared_by_part[bare].append(slot)
                else:
                    new_by_part[bare].append(slot)
                mapping[local] = slot
            local_maps[bare] = mapping

        for item in resolved:
            bare = item["bare"]
            slots = item["slots"]
            mapping = local_maps.get(bare) or {}
            # Gap slots: never referenced, but VGMap must stay a complete
            # 0..VGCount-1 cover (see MeshCreateHelper._normalize_and_validate_vg_map).
            # 基准必须与已注册槽位同源：``len(signature_to_slot)`` 可能因为
            # 「排除去重」产生的重复槽而小于某个已注册槽位号，直接用它会撞号。
            max_slot = slots[-1] if slots else -1
            if mapping:
                base = max(max(mapping.values()) + 1, len(slot_records))
                for local in range(0, max_slot + 1):
                    if local not in mapping:
                        mapping[local] = base + local

            shared = shared_by_part.get(bare) or []
            new = new_by_part.get(bare) or []
            if shared or new:
                overlaps.append({"bare": bare, "shared": len(shared), "new": len(new)})

            per_part[bare] = {
                **part_meta[bare],
                "vg_count": int(max_slot + 1) if max_slot >= 0 else 0,
                "vg_map": {int(k): int(v) for k, v in sorted(mapping.items())},
                "shared_slots": shared,
                "new_slots": new,
                "local_to_global": mapping,
            }

        slot_count = len(slot_records)
        skeleton = bytearray()
        for record in slot_records:
            record["matrix_sha256"] = _sha256_hex(record["signature"])
            skeleton.extend(record["signature"])

        # 注意：入参 ``parts`` 的键是 ``slots``；``referenced_slots`` 只存在于
        # ``per_part`` 的返回值里（F1：曾经误取该键，导致此字段恒 0）。
        referenced_slot_total = sum(
            len(per_part[item["bare"]]["referenced_slots"])
            for item in resolved
            if item["bare"] in per_part
        )

        return {
            "slot_count": slot_count,
            "slots": slot_records,
            "skeleton_bytes": bytes(skeleton),
            "per_part": per_part,
            "shared_slot_count": int(sum(1 for users in slot_seen_by.values() if len(set(users)) > 1)),
            "referenced_slot_total": int(referenced_slot_total),
            "pool_slot_total": int(sum(int(item["pool_slots"]) for item in resolved)),
        }


# --------------------------------------------------------------------------- #
# 3b) 站位（stub）机制 —— 「借位 ≠ 吸收」的判据 + 三条规则
# --------------------------------------------------------------------------- #


def load_component_family_map(workspace_root: str, lod: str = "LOD0") -> dict:
    """``{draw_ib: [member_bare, ...]}`` from ``<lod>/DrawIB-Component.json``.

    That file is the workspace's **candidate** family roster: it contains members
    that were never extracted (实测五角色家族数 8/8/7/6/9，且含未落盘成员)。
    Missing / unreadable file returns ``{}``, which the stub decision treats as
    "family membership cannot be enumerated ⇒ do not inject" (fail-closed).
    """
    root = str(workspace_root or "").strip()
    if not root:
        return {}
    path = os.path.join(root, str(lod or "LOD0"), HIMI_COMPONENT_MAP_BASENAME)
    if not os.path.isfile(path):
        return {}
    try:
        payload = JsonUtils.LoadFromFile(path)
    except Exception:  # noqa: BLE001
        return {}
    if not isinstance(payload, dict):
        return {}
    families: dict[str, list[str]] = {}
    for draw_ib, members in payload.items():
        names: list[str] = []
        if isinstance(members, dict):
            names = [str(value) for value in members.values()]
        elif isinstance(members, (list, tuple)):
            names = [str(value) for value in members]
        cleaned = sorted({bare_from_unique_str(name) for name in names if str(name).strip()})
        if cleaned:
            families[str(draw_ib)] = cleaned
    return families


def _component_vg_values(payload: dict) -> set:
    """One component's ``VGMap`` **value set** (``own(x)``); non-negative only.

    The empty set means "no reverse-lookup data" ⇒ the caller must not inject.
    """
    vg_map = (payload or {}).get("VGMap")
    if not isinstance(vg_map, dict):
        return set()
    values = set()
    for raw in vg_map.values():
        try:
            slot = int(raw)
        except (TypeError, ValueError):
            continue
        if slot >= 0:
            values.add(slot)
    return values


def himi_exclusive_slots(bare: str, family_members, landed_values: dict) -> set:
    """``exclusive(bare) := own(bare) \\ ⋃_{c ∈ C(ib), c ≠ bare} own(c)``.

    **t41 起不再参与生产判据**：规则② 已换成「外来槽」口径（见
    ``is_drawib_absorbed`` / ``declared_by_present_slots``，计划书 §7.7.12.2），
    因为族内差集在「同族 100% 共享槽位」的常态下结构性失效。保留本函数是为了
    ① 记录被替换判据的确切语义；② 测试/探针仍用它做「旧判据在同一 fixture 上会
    得出什么」的**非恒真对照**。

    This is HIMI's equivalent of ZZMI's "self-declared segment"
    (``[VGOffset, VGOffset + VGCount)``).  HIMI **cannot** reuse the segment
    predicate: ``VGOffset`` is always 0 and ``VGCount = max(referenced)+1``, so
    the segment also contains gap slots and borrowed canonical slots — it
    degenerates into "all slots" and has **zero** discriminating power for
    dedup borrowing (计划书 §7.7.4).

    ``landed_values`` maps ``bare -> own(bare)`` for the landed members of the
    family; members without landed data contribute nothing to the union.

    **差集只在同家族（``family_members``）内做**：跨家族的去重借位**不会**被排除。
    也就是说，若某根骨骼被**别的 IB 家族**的组件也引用，本函数仍会把它算作
    ``bare`` 的「专属槽位」⇒ 判据可能把「跨家族借位」误读成「几何被吸收」而多插桩。
    这个方向是**偏保守**（多压原版 draw）的已知边界：`C(ib)` 的来源
    （``LOD0/DrawIB-Component.json``）只有**本家族**的候选成员名单，跨家族归属在
    工作空间侧无法枚举 ⇒ 宁可偏保守也不引入第二套猜测口径（计划书 §7.7.4 的
    退化情形取向）。
    """
    key = str(bare or "")
    own = set(landed_values.get(key) or ())
    if not own:
        return set()
    others = set()
    for member in family_members or ():
        member = str(member)
        if member == key:
            continue
        others |= set(landed_values.get(member) or ())
    return own - others


def _member_dedup_excluded(root: str, lod: str, member: str) -> bool:
    """该成员的 json 是否带 ``VGMapDedupExcluded=True``（规则③，正交于两条规则）。

    ZZMI 在**两条规则**的成员循环里都判这一条（``ui/universal/zzmi.py:992-999``）：
    「该部件即使被引用也不生成占位 —— 显式排除优先于 absorbed 判定（对齐 EFMI）」。
    因此规则① 的「不做门控」指的是 **`exclusive` / `used` / `VGMap` 三关**，
    规则③ 仍然生效（t38 逐字对齐 ZZMI）。
    """
    json_path = resolve_submesh_json_path(root, member) or resolve_submesh_json_path(
        root, f"{lod}.{member}"
    )
    if not json_path:
        return False
    try:
        payload = JsonUtils.LoadFromFile(json_path) or {}
    except Exception:  # noqa: BLE001
        return False
    return bool(payload.get("VGMapDedupExcluded"))


def declared_by_present_slots(present, family_map, landed_values) -> set | None:
    """现存对象的**声明命名空间** ``declared_by_present``（t41「外来槽」判据的输入）。

    ``declared_by_present := ⋃ { own(c) : c ∈ present ∧ own(c) 可判定 }``
    （计划书 §7.7.12.2）。它是「现存对象自己声明过的全局槽」的集合；``used`` 减去它，
    剩下的就是**谁都没声明**的槽（``foreign_used``）——只有这种槽才能当「几何被吸收」
    的证据，因为借位槽（无论同族还是跨族）**必由它的 canonical 拥有者声明**。

    返回 ``None`` 表示**不可判定 ⇒ 外来槽路径不参与**（fail-closed，调用方按「不插桩」处理）。
    身份归一化（与规则① 的 F-A 修复同一套）：

    ================================================  ====================================
    情形                                              处置
    ================================================  ====================================
    名字就是某个家族的**已落盘成员**                  取其 ``own``
    名字**不是**已知成员，但首段是已知 ``draw_ib``    取该 IB **全部已落盘成员** ``own`` 的并集
                                                      （声明得更多 ⇒ 外来槽更少 ⇒ 更少压，
                                                      方向安全；合并对象带 ``_copy`` 后缀
                                                      走这一支）
    名字是已知成员但**未落盘 / 无 json**              返回 ``None``（成员身份已知而 ``own``
                                                      不可判定 ⇒ 不猜）
    身份完全解析不出（既不是成员、首段也不是已知 IB）  返回 ``None``
    ================================================  ====================================
    """
    declared: set = set()
    known_members = {
        str(member) for members in (family_map or {}).values() for member in (members or ())
    }
    known_ibs = {str(draw_ib) for draw_ib in (family_map or {})}
    for raw in present or ():
        name = str(raw)
        if name in known_members:
            own = landed_values.get(name)
            if not own:
                # 成员身份已知但 own 不可判定 ⇒ 外来槽路径不可用（不猜）。
                return None
            declared |= set(own)
            continue
        draw_ib = name.split("-", 1)[0]
        if draw_ib in known_ibs:
            for member in family_map.get(draw_ib) or ():
                declared |= set(landed_values.get(str(member)) or ())
            continue
        return None
    return declared


def is_drawib_absorbed(
    draw_ib: str,
    *,
    family_members,
    landed_values,
    used_group_ids,
    declared_by_present=None,
) -> bool:
    """HIMI 的规则② 判据（t41「外来槽」口径，替换 t38 之前的 ``exclusive ∩ used``）。

    ``foreign_used := used \\ declared_by_present``；
    ``absorbed(ib) := ∃ m ∈ C(ib) ∩ L : own(m) ∩ foreign_used ≠ ∅``（逐成员）。

    为什么替换旧判据（``exclusive_C(ib)(m) ∩ used``）：HIMI 实测「同族 100% 共享槽位」
    是常态 ⇒ 多成员家族的每个成员 ``exclusive`` 恒空 ⇒ 规则② 对它们**结构性失效**
    （计划书 §7.7.12.1）。新判据不依赖族内差集，只看「这个槽有没有人认领」。

    为什么不会重演 ZZMI 2026-09-16 的跨族借位误插（脸部 ``c209c22b-45087-0``）：
    **构造性免疫** —— 被 present 对象引用的槽，要么由它自己的 ``VGMap`` 映出
    （⇒ ∈ ``declared_by_present``），要么来自被吸收组件的 ``VGMap``；只有**没有任何**
    present 对象声明它时才判为外来槽。借位槽的 canonical 拥有者在场 ⇒ 一定被声明
    ⇒ 永不算外来槽（计划书 §7.7.12.2 的 (b) 论证）。

    ``declared_by_present=None`` ⇒ **返回 False**（fail-closed：present 对象的声明命名
    空间不可得 ⇒ 外来槽路径不参与 ⇒ 宁可不压）。

    **已知代价**：被吸收组件的槽位**全部**被某个 present 对象声明时检不出（该场景下
    「共享骨骼」与「几何被吸收」在现有证据下不可区分）⇒ 保留「少压」方向
    （可见重影 > 静默消失）。
    """
    members = [str(member) for member in (family_members or ()) if str(member).strip()]
    if not members:
        return False
    used = {int(slot) for slot in (used_group_ids or ()) if int(slot) >= 0}
    if not used:
        # 无法构造「现存对象实际引用的槽位」证据 ⇒ 不插桩（与 ZZMI 兼容兜底同款）。
        return False
    if declared_by_present is None:
        # fail-closed：声明命名空间不可判定 ⇒ 外来槽路径不参与。
        return False
    foreign_used = used - {int(slot) for slot in declared_by_present if int(slot) >= 0}
    if not foreign_used:
        return False
    for member in members:
        own = set(landed_values.get(member) or ())
        if not own:
            continue
        if own & foreign_used:
            return True
    return False


@dataclass
class HIMIStubDecision:
    """Which IB families/components need a 3-vertex stub, and why."""

    stub_members: list = field(default_factory=list)
    skipped_members: list = field(default_factory=list)
    families: dict = field(default_factory=dict)
    diagnostics: list = field(default_factory=list)
    families_stubbed: list = field(default_factory=list)
    families_skipped: list = field(default_factory=list)
    reason_counts: dict = field(default_factory=dict)

    @property
    def inject_count(self) -> int:
        return len(self.stub_members)


def _member_present_in_scene(member: str, present) -> bool:
    """组件 ``member``（DrawIB 身份，如 ``09c6e58a-57792-0``）在场景里是否有对象。

    **必须按 DrawIB 身份判，不能只做 bare 精确相等**：导出侧 ``present`` 的每一项是
    「DrawIB 身份 + 网格名」（``09c6e58a-57792-0.捆绑绳子``），而家族成员只到身份
    （``09c6e58a-57792-0``）。精确相等会让**在场的成员也被判成缺席** ⇒ 规则① 给在场
    组件也插桩 ⇒ 它的 3 索引站位与真部件的段抢同一个 ``match_first_index``（先命中者
    生效）⇒ 整个部件在游戏里消失（用户实测形态：「部件整块不见了」）。

    网格名可能带副本/重命名后缀，所以只认 ``member.`` 前缀，不认任意前缀
    （``09c6e58a-57792-0`` 不得匹配 ``09c6e58a-57792-01.x``）。
    """
    member = str(member or "")
    if not member:
        return False
    prefix = member + "."
    for bare in present or ():
        bare = str(bare or "")
        if bare == member or bare.startswith(prefix):
            return True
    return False


def decide_stub_injection(
    *,
    workspace_root: str,
    scene_present_bares,
    family_map=None,
    lod: str = "LOD0",
    used_group_ids=None,
    allowed_rules=None,
) -> HIMIStubDecision:
    """The three ZZMI stub rules, rewritten for HIMI (计划书 §7.7.3 / §7.7.4 / §7.7.12).

    规则① 部分缺失的 DrawIB ⇒ 缺失组件补占位（判据只看**场景真相**，不看蓝图声明）；
    规则② 整个 DrawIB 缺席 ⇒ 按「**外来槽**是否被现存对象实际引用（weight>0）」判定
    （``own(m) ∩ (used \\ declared_by_present) ≠ ∅``，t41 起替换旧的 ``exclusive ∩ used``）；
    规则③ ``VGMapDedupExcluded=True`` ⇒ 即使被引用也不插桩（显式排除正交）；
    反例① 无反查数据（json 无 VGMap / 未落盘）⇒ 不插桩；
    反例② 无外来槽证据（该成员的槽位都被现存对象声明过 ⇒ 与共享骨骼不可区分）⇒
    **不插桩**（HIMI 取保守方向）。

    ``scene_present_bares`` must be built from **scene truth** (objects that
    really exist), never from the blueprint's declared names: ZZMI's t80 §2.3
    measurement showed declared-but-absent parts make the whole stub pass skip
    and the downstream ``SubMeshModel`` then raises ``Fatal``.

    ``used_group_ids`` is the set of vertex-group ids that **scene objects other
    than stubs** actually reference with weight > 0; ``None`` means "could not be
    constructed" and forbids injection.

    **逐组件判定（实现口径，登记给集成阶段）**
    ------------------------------------------
    规则② 的判据是**逐组件**的：家族整体缺席时，只有「自身仍有**外来槽证据**」的成员才
    补站位；其余成员按**保守方向**不插桩并记入 ``skipped_members``。计划书 §7.7.3 规则②
    与 §7.7.8 ST2 的「全组件补站位」措辞是这条口径的**粗粒度前身**，实现以本函数为准
    （口径锚点：
    ``tests/test_himi_stub_objects.py::test_st2_absent_family_with_own_slot_referenced_gets_all_members``
    —— 它逐组件断言「只对专属槽位被引用的那一个补站位，另一个记入 skipped」）。

    **``allowed_rules``（t107）**
    -----------------------------
    ``None`` = 三条规则全开（合并骨架运行时路径）。``("1",)`` = **只跑规则①**：
    部分在场的家族照旧给缺失成员补站位，规则②/④ 一律不判定（记
    ``HIMI_STUB_REASON_RULE_GATED``）。用在哪：HIMI「普通模式」（``import_merged_vgmap()``
    关闭，不产出合并骨架资源）—— 规则① 的正当性不依赖合并骨架（缺失成员的几何被**同一
    个导出**的幸存兄弟接管，站位只负责压掉原版那一笔），而规则②/④ 的判据全部建立在
    VGMap/palette 记账上，没有合并运行时就没有意义。
    """
    root = str(workspace_root or "").strip()
    decision = HIMIStubDecision()
    if allowed_rules is None:
        allow_rule1 = allow_rule_other = True
    else:
        _allowed = {str(rule) for rule in allowed_rules}
        allow_rule1 = "1" in _allowed
        allow_rule_other = bool(_allowed - {"1"})
    if not root or not os.path.isdir(root):
        decision.diagnostics.append("站位：工作空间目录无效 ⇒ 不插桩")
        return decision

    present = {bare_from_unique_str(str(name)) for name in (scene_present_bares or ())}
    present.discard("")
    # F-A（t38 修复）：家族是否「部分在场」**不能只按 bare 精确匹配** —— 合并对象的
    # 场景身份可能带副本/重命名后缀（实测 mesh 名 `LOD0.2c706fbd-47199-0_copy`），此时
    # bare 不匹配任何声明成员 ⇒ 家族会被误判成「整族缺席」⇒ 规则① 根本不触发。
    # 这里额外按 **IB 身份**（bare 的首段 = ``draw_ib``）识别在场对象：只要场景里有一个
    # 对象属于该 IB，该家族就是「部分在场」（对齐 ZZMI 的
    # ``_normalize_merged_object_drawcalls`` + `present` 账本：不把两个身份对齐，导出器
    # 会把目标物体判成缺席）。
    present_ibs = {str(bare).split("-", 1)[0] for bare in present if str(bare)}
    if family_map is None:
        family_map = load_component_family_map(root, lod)
    if not family_map:
        decision.diagnostics.append(
            "站位：无法枚举 IB 家族成员（缺 "
            f"{lod}/{HIMI_COMPONENT_MAP_BASENAME}）⇒ 一律不插桩"
        )
        return decision

    used_available = used_group_ids is not None
    used = {int(slot) for slot in (used_group_ids or ()) if int(slot) >= 0}

    # 每个家族成员的已落盘 VGMap 值域（own）——只读一次。
    landed_values: dict[str, set] = {}
    for draw_ib, members in sorted(family_map.items()):
        for member in members:
            if member in landed_values:
                continue
            json_path = resolve_submesh_json_path(root, member) or resolve_submesh_json_path(
                root, f"{lod}.{member}"
            )
            if not json_path:
                landed_values[member] = set()
                continue
            try:
                payload = JsonUtils.LoadFromFile(json_path)
            except Exception:  # noqa: BLE001
                payload = None
            landed_values[member] = _component_vg_values(payload or {})

    # 规则② 的输入（t41「外来槽」口径，计划书 §7.7.12.2）：现存对象的声明命名空间。
    # 不可判定（`None`）⇒ 外来槽路径不参与 ⇒ 规则② 一律不插桩（fail-closed）。
    declared_by_present = declared_by_present_slots(present, family_map, landed_values)
    foreign_used = (
        None if declared_by_present is None else (used - set(declared_by_present))
    )

    # 规则④（t103）的输入：**在场家族**注册过的槽位并集。缺席家族只在自己持有在场家族
    # 都不持有的槽位时才是「palette 写方」——那些槽没有它就会读到零矩阵。
    present_family_slots: set = set()
    for _draw_ib, _members in family_map.items():
        if str(_draw_ib) not in present_ibs:
            continue
        for _member in _members:
            present_family_slots |= set(landed_values.get(str(_member)) or ())

    for draw_ib, members in sorted(family_map.items()):
        draw_ib = str(draw_ib)
        members = [str(member) for member in members]
        in_scene = [member for member in members if _member_present_in_scene(member, present)]
        absent = [member for member in members if not _member_present_in_scene(member, present)]
        # 「家族部分在场」= 有成员按 **DrawIB 身份**在场，**或**场景里有属于同一 IB 的对象
        # （合并对象可能带副本后缀 ⇒ 身份对不上，但 IB 身份对得上；见上面的 F-A 注释）。
        partial_family = bool(in_scene) or (draw_ib in present_ibs)
        if partial_family and not allow_rule1:
            decision.families[draw_ib] = {
                "decision": HIMI_STUB_REASON_RULE_GATED,
                "members": members,
                "in_scene": in_scene,
                "absent": absent,
                "absorbed": False,
                "stub": [],
                "skipped": absent,
                "rule": "1",
                "partial_family": True,
            }
            decision.diagnostics.append(
                f"站位：{draw_ib} 部分在场，但本次只允许规则①之外的规则 ⇒ 不插桩"
            )
            continue
        if not partial_family and not allow_rule_other:
            decision.families[draw_ib] = {
                "decision": HIMI_STUB_REASON_RULE_GATED,
                "members": members,
                "in_scene": in_scene,
                "absent": absent,
                "absorbed": False,
                "stub": [],
                "skipped": absent,
                "rule": "2",
                "partial_family": False,
            }
            decision.diagnostics.append(
                f"站位：{draw_ib} 整族缺席，但本次只允许规则①（普通模式不产出合并骨架"
                "资源）⇒ 不判定规则②/④、不插桩"
            )
            continue
        if not absent:
            decision.families[draw_ib] = {
                "decision": HIMI_STUB_REASON_ALREADY_PRESENT,
                "members": members,
                "in_scene": in_scene,
                "absent": [],
                "absorbed": False,
                "rule": "1" if partial_family else "2",
                "partial_family": bool(partial_family),
            }
            continue

        if partial_family:
            # 规则①（ZZMI 逐字口径，t38 修复）：家族**部分在场** ⇒ 缺失组件直接补占位
            # （其几何显然被同 DrawIB 的幸存对象接管）。**不做** `exclusive` / `used` /
            # `VGMap` 门控 —— 那些检查只属于规则②（**整个** DrawIB 缺席时）。
            # 为什么必须这样：HIMI 实测「同族 100% 共享槽位」是常态（`exclusive` 恒空），
            # 把 `exclusive` 门控套到规则① 上会让**多成员家族永远不插桩**（用户实测
            # `2c706fbd` 5 分块只导出 1 个合并对象、另 4 个无占位 ⇒ 原版 draw 照跑 ⇒ 重影）。
            absorbed = True
            reason = HIMI_STUB_REASON_ABSORBED
        else:
            # 规则④（t103）：整个 DrawIB 缺席、它注册的槽位**没有任何在场家族注册**、
            # 而且这些槽**确实被引用**（used）⇒ 它是它们唯一的 palette 写方。不补站位 ⇒
            # 运行期读到零矩阵 ⇒ 顶点被拉向原点（用户实测的「模型爆炸」）。几何方面它是
            # 安全的：HIMI 实测里这些族的顶点本来就整块躺在在场族那笔 draw 的顶点池里
            # （t103：1b62725d 580/580、09c6e58a-57792-0 19264/19264 三角形都由在场 draw
            # 画过），所以 3 顶点站位不会丢几何，只丢一笔重复 draw。
            # 「被引用」这一条必须留着：没人引用的槽不需要写方，补站位只会白抑制原版 draw
            # （规则② 对它们的保守方向仍然正确）。
            family_slots: set = set()
            for member in members:
                family_slots |= set(landed_values.get(member) or ())
            writerless = bool((family_slots & used) - present_family_slots)
            if writerless:
                absorbed = True
                reason = HIMI_STUB_REASON_WRITERLESS
            else:
                # 规则②：整个 DrawIB 缺席 ⇒ 看**外来槽**是否被现存对象实际引用（t41）。
                absorbed = is_drawib_absorbed(
                    draw_ib,
                    family_members=members,
                    landed_values=landed_values,
                    used_group_ids=used,
                    declared_by_present=declared_by_present,
                )
            if absorbed:
                reason = reason if writerless else HIMI_STUB_REASON_ABSORBED
            elif foreign_used is None:
                # fail-closed：present 对象的声明命名空间不可判定 ⇒ 外来槽路径不参与。
                reason = HIMI_STUB_REASON_NO_DECLARED
            elif used_available:
                reason = HIMI_STUB_REASON_ZERO_REFERENCE
            else:
                reason = HIMI_STUB_REASON_NO_USED

        stubbed: list[str] = []
        skipped: list[str] = []
        for member in absent:
            if not absorbed:
                # 零引用 / 证据不可构造 ⇒ 按用户意图不生成（保留原版绘制）。
                skipped.append(member)
                continue
            if partial_family:
                # 规则①：缺失成员直接全补占位；**只保留规则③**（`VGMapDedupExcluded`
                # 正交 —— ZZMI 也在同一层逐成员判，且「显式排除优先于 absorbed」）。
                if _member_dedup_excluded(root, lod, member):
                    decision.reason_counts[HIMI_STUB_REASON_DEDUP_EXCLUDED] = (
                        decision.reason_counts.get(HIMI_STUB_REASON_DEDUP_EXCLUDED, 0) + 1
                    )
                    skipped.append(member)
                    continue
                stubbed.append(member)
                continue
            values = landed_values.get(member) or set()
            if not values:
                # 反例①：无反查数据 ⇒ 不插桩（证据不可构造）。
                decision.reason_counts[HIMI_STUB_REASON_NO_VGMAP] = (
                    decision.reason_counts.get(HIMI_STUB_REASON_NO_VGMAP, 0) + 1
                )
                skipped.append(member)
                continue
            if _member_dedup_excluded(root, lod, member):
                # 规则③：显式排除优先于 absorbed（正交）。
                decision.reason_counts[HIMI_STUB_REASON_DEDUP_EXCLUDED] = (
                    decision.reason_counts.get(HIMI_STUB_REASON_DEDUP_EXCLUDED, 0) + 1
                )
                skipped.append(member)
                continue
            if writerless:
                # 规则④：这些槽没有别的写方 ⇒ 补站位（上面已过掉无反查数据与 dedup 两道）。
                stubbed.append(member)
                continue
            if foreign_used is None or not (values & foreign_used):
                # 反例② / ST3、ST13 的反面：**无外来槽证据**（该成员的槽位都被现存
                # 对象声明过 ⇒ 与「共享骨骼」不可区分）⇒ **不插桩**（保守方向）。
                decision.reason_counts[HIMI_STUB_REASON_NO_EXCLUSIVE] = (
                    decision.reason_counts.get(HIMI_STUB_REASON_NO_EXCLUSIVE, 0) + 1
                )
                skipped.append(member)
                continue
            stubbed.append(member)

        decision.families[draw_ib] = {
            "decision": reason,
            "members": members,
            "in_scene": in_scene,
            "absent": absent,
            "absorbed": bool(absorbed),
            "stub": stubbed,
            "skipped": skipped,
            "rule": "1" if partial_family else "2",
            "partial_family": bool(partial_family),
        }
        if stubbed:
            decision.families_stubbed.append(draw_ib)
            decision.stub_members.extend(stubbed)
            if partial_family:
                decision.diagnostics.append(
                    f"站位：{draw_ib} 有部分组件在场景中（规则①，ZZMI 逐字口径：缺失组件"
                    f"直接补占位、不做 exclusive 门控）⇒ 缺失的 {len(stubbed)} 个组件"
                    "补占位小三角面（抑制原版 draw 防重影）"
                )
            elif reason == HIMI_STUB_REASON_WRITERLESS:
                decision.diagnostics.append(
                    f"站位：{draw_ib} 没有对象，但它注册的槽位没有任何在场家族注册"
                    f"（它是这些槽唯一的 palette 写方），补占位 {len(stubbed)} 个组件"
                )
            else:
                decision.diagnostics.append(
                    f"站位：{draw_ib} 没有对象，但其**外来槽**被其它模型引用"
                    f"（几何已被合并），补占位 {len(stubbed)} 个组件"
                )
        if skipped:
            decision.families_skipped.append(draw_ib)
            decision.skipped_members.extend(skipped)
            if reason == HIMI_STUB_REASON_NO_EXCLUSIVE:
                decision.diagnostics.append(
                    f"站位：{draw_ib} 的缺失组件没有**外来槽**证据（其槽位都被现存对象"
                    "声明过 ⇒ 与共享骨骼不可区分）⇒ 保守不插桩（可能残留重影）"
                )
            elif reason == HIMI_STUB_REASON_NO_DECLARED:
                decision.diagnostics.append(
                    f"站位：{draw_ib} 无对象且无法取得现存对象的**声明命名空间**"
                    "（有对象未落盘 / 身份不可解析）⇒ 外来槽路径不参与、保守不插桩"
                    "（可能残留重影）"
                )
            elif reason == HIMI_STUB_REASON_NO_USED:
                decision.diagnostics.append(
                    f"站位：{draw_ib} 无对象且无法取得「现存对象实际引用的槽位」"
                    "⇒ 保守不插桩（可能残留重影）"
                )
            elif reason == HIMI_STUB_REASON_ZERO_REFERENCE:
                decision.diagnostics.append(
                    f"站位：{draw_ib} 无对象且没有外来槽被引用，按用户意图不生成"
                    "（游戏内保留原版绘制）"
                )
            elif reason == HIMI_STUB_REASON_ABSORBED:
                decision.diagnostics.append(
                    f"站位：{draw_ib} 有 {len(skipped)} 个缺失组件未插桩"
                    "（无反查数据或已标记 VGMapDedupExcluded），游戏保留原版绘制"
                )
    return decision


# --------------------------------------------------------------------------- #
# 3c) 站位对象的 Blender 侧操作（bpy 惰性导入，本模块其余部分仍无 bpy 依赖）
# --------------------------------------------------------------------------- #
#
# 为什么放在这里而不是 ``ui/universal/himi.py``：``tests/test_himi_merged_runtime.py``
# 的 ``test_new_code_does_not_import_bpy`` 用 AST 逐条断言**导出器与运行时计划模块**
# 不得 import bpy（那条约束是为「数据层/计划层可在无 Blender 环境单测」立的）。站位
# 必须真的创建 Blender 对象，所以这些操作落在数据层模块里、并以**函数内惰性 import**
# 访问 bpy；本模块在无 bpy 环境下仍然可以完整导入与单测（实测：本文件的全部单测
# 都在无 bpy 的 pytest 宿主里跑）。
# 函数名以 ``blender_`` 开头，便于一眼看出它们需要真实宿主。


def _require_bpy():
    import bpy  # noqa: PLC0415 - 惰性导入：无 bpy 环境下本模块仍可导入

    return bpy


def _stub_workspace_unique_str(bare: str) -> str:
    text = str(bare or "")
    if not text.upper().startswith("LOD"):
        text = "LOD0." + text
    return text


def blender_remove_stub_object_data(obj) -> None:
    """Remove a stub object and its mesh data (best effort, never raises)."""
    try:
        bpy = _require_bpy()
    except Exception:  # noqa: BLE001
        return
    mesh = getattr(obj, "data", None)
    try:
        bpy.data.objects.remove(obj, do_unlink=True)
    except Exception:  # noqa: BLE001
        return
    if mesh is not None and getattr(mesh, "users", 0) == 0:
        try:
            bpy.data.meshes.remove(mesh)
        except (AttributeError, RuntimeError):
            pass


def blender_purge_stale_stub_objects() -> set:
    """Delete every object carrying :data:`HIMI_STUB_MARKER`; return their names.

    Must run **before** the present/scene-truth set is built (计划书 §7.7.5): a
    leftover stub whose name equals a real part's identity would make that part
    look "present in scene" ⇒ no stub injected ⇒ the object is then removed ⇒
    downstream ``SubMeshModel`` references a non-existent object and raises
    ``Fatal``.
    """
    try:
        bpy = _require_bpy()
    except Exception:  # noqa: BLE001
        return set()
    stale: set = set()
    try:
        objects = list(bpy.data.objects)
    except Exception:  # noqa: BLE001
        return stale
    for obj in objects:
        try:
            marked = bool(obj.get(HIMI_STUB_MARKER))
        except Exception:  # noqa: BLE001
            marked = False
        if not marked:
            continue
        stale.add(str(getattr(obj, "name", "") or ""))
        blender_remove_stub_object_data(obj)
    return stale


def _strip_duplicate_suffix(segment: str) -> str:
    """去掉 Blender 给重名对象追加的 ``.001`` 后缀。

    站位件的名字是 ``LOD0.<bare>``，``bare`` 的第三段是 ``first_index``；同名对象在
    Blender 里被复制过一次，段尾就变成 ``0.001``。下游 ``int()`` 当场抛
    ``invalid literal for int()``，一轮快速局部导出直接中止。
    """
    text = str(segment or "").strip()
    head, separator, tail = text.rpartition(".")
    if separator and head and len(tail) == 3 and tail.isdigit():
        return head
    return text


def _himi_stub_name_parts(obj_name: str) -> dict:
    """把合成的 ``LOD0.<bare>`` 名拆成 ``DrawCallModel.match_*`` 的语义。

    ``bare`` 形如 ``aaaa-2-0`` ⇒ ``draw_ib`` = 首段、``index_count`` = 第二段、
    ``first_index`` = 第三段（段数不足时缺的段为 ``""``）。

    **不调 ``ObjectPrefixHelper``**（t26 约束）：站位件的名字是我们自己合成的，身份已知；
    真实现的 ``ObjectPrefixHelper`` 会把 ``LOD0.<bare>`` 误拆（它按「6 位以上前缀 +
    分隔符」的启发式切分，合成名并不满足那条约定）。
    """
    bare = bare_from_unique_str(str(obj_name or ""))
    segments = bare.split("-")
    if segments:
        segments[-1] = _strip_duplicate_suffix(segments[-1])
    return {
        "bare": bare,
        "draw_ib": segments[0] if len(segments) > 0 else "",
        "index_count": segments[1] if len(segments) > 1 else "",
        "first_index": segments[2] if len(segments) > 2 else "",
    }


@dataclass
class HIMIStubDrawCall:
    """Duck-typed ``DrawCallModel`` for an injected stub object — **contract-complete**.

    Deliberately **not** ``common.draw_call_model.DrawCallModel``:

    * that module pulls in ``m_key`` / ``object_prefix_helper`` (heavier import
      graph, and it is not part of the HIMI runtime plan's dependency set);
    * its ``__post_init__`` derives ``match_unique_str`` from
      ``ObjectPrefixHelper``, which would run on the synthetic ``LOD0.<bare>``
      name and can mis-split it — the identity is already known here.

    **契约完整性（t36 blocker 修复）**：F1 修复（注入必须早于基类组装）之后，站位
    DrawCall **真的**进入了 ``drawib_model_list``，于是下游**第一次**开始读它 ——
    ``common/submesh_model.py`` 的 ``SubMeshModel.__post_init__`` 直接读
    ``match_draw_ib`` / ``match_first_index`` / ``match_index_count`` ⇒ 缺字段会以
    ``AttributeError: 'HIMIStubDrawCall' object has no attribute 'match_draw_ib'``
    **打断真实导出**（用户在「快速局部导出」里撞到的就是这条）。

    因此这里补齐 ``DrawCallModel`` 暴露的**全部**字段与方法（不是「够用就行」）：
    权威清单 = ``dataclasses.fields(DrawCallModel)`` 的字段名集合 ∪
    ``DrawCallModel`` 的公开方法名集合，由
    ``tests/test_himi_stub_objects.py::StubDrawCallContractTests``
    做**集合包含**机械校验（下次再加下游读取点会立刻红，而不是等用户撞）。
    ``match_*`` 的值由 ``obj_name``（``LOD0.<bare>``，``bare`` = ``aaaa-2-0``）
    **自行解析** —— 不调 ``ObjectPrefixHelper``（它会把合成的 ``LOD0.<bare>`` 名误拆）。
    """

    obj_name: str
    source_obj_name: str = ""
    vertex_count: int = 0
    index_count: int = 0
    index_offset: int = 0
    himi_stub: bool = True

    # ---- DrawCallModel 契约字段（全部 init=False：派生自 obj_name，不参与构造） ---- #
    #: 与 `DrawCallModel.match_*` 同型（**都是 str**，不是 int —— 真实现如此）
    match_draw_ib: str = field(init=False, repr=False, default="")
    match_index_count: str = field(init=False, repr=False, default="")
    match_first_index: str = field(init=False, repr=False, default="")
    match_unique_str: str = field(init=False, repr=False, default="")
    match_workspace_unique_str: str = field(init=False, repr=False, default="")
    comment_alias_name: str = field(init=False, repr=False, default="")
    #: 站位件没有按键条件 / 着色器替换信息（真实现里这两个也是 `init=False` 的空容器）
    work_key_list: list = field(init=False, repr=False, default_factory=list)
    shader_replace_info_list: list = field(init=False, repr=False, default_factory=list)
    shader_replace_info_resolved: bool = field(init=False, repr=False, default=False)

    def __post_init__(self) -> None:
        parts = _himi_stub_name_parts(self.obj_name)
        self.match_draw_ib = parts["draw_ib"]
        self.match_index_count = parts["index_count"]
        self.match_first_index = parts["first_index"]
        self.match_unique_str = parts["bare"]
        self.match_workspace_unique_str = str(self.obj_name or "")
        # 合成名没有「前缀 + 注释别名」的拆分结果 ⇒ 与真实现的 fallback 分支同值
        self.comment_alias_name = ""

    def get_blender_obj_name(self) -> str:
        return self.source_obj_name if self.source_obj_name else self.obj_name

    def get_workspace_unique_str(self) -> str:
        return self.match_workspace_unique_str or str(self.obj_name or "")

    def get_unique_str(self) -> str:
        return self.match_unique_str or bare_from_unique_str(self.obj_name)

    def get_condition_str(self) -> str:
        return ""

    def get_drawindexed_str(self, obj_name_draw_offset_dict=None, base_vertex: int = 0) -> str:
        return f"drawindexed = {int(self.index_count)},{int(self.index_offset)},{int(base_vertex)}"

    def get_drawindexed_instanced_str(self, obj_name_draw_offset_dict=None) -> str:
        """与 ``DrawCallModel`` 同名同形（HIMI 无 ``DrawIndexedInstanced``，但契约要完整）。"""
        draw_offset = (
            self.index_offset
            if obj_name_draw_offset_dict is None
            else obj_name_draw_offset_dict.get(self.obj_name, self.index_offset)
        )
        return (
            f"drawindexedinstanced = {int(self.index_count)},INSTANCE_COUNT,"
            f"{int(draw_offset)},0,FIRST_INSTANCE"
        )


def blender_object_present(name: str) -> bool:
    """True when ``bpy.data.objects`` really has an object with that name.

    Scene truth, **not** the blueprint's declared names (ZZMI t80 §2.3: treating
    "declared" as present made the whole stub pass skip and the downstream
    ``SubMeshModel`` raise ``Fatal``).
    """
    text = str(name or "")
    if not text:
        return False
    try:
        bpy = _require_bpy()
    except Exception:  # noqa: BLE001
        return False
    try:
        return bpy.data.objects.get(text) is not None
    except Exception:  # noqa: BLE001
        return False


def blender_remove_stub_object_data_by_name(name: str) -> bool:
    """Look up a stub object by name and remove it (best effort)."""
    text = str(name or "")
    if not text:
        return False
    try:
        bpy = _require_bpy()
    except Exception:  # noqa: BLE001
        return False
    try:
        obj = bpy.data.objects.get(text)
    except Exception:  # noqa: BLE001
        return False
    if obj is None:
        return False
    blender_remove_stub_object_data(obj)
    return True


def blender_collect_used_group_ids(object_names) -> set:
    """Vertex-group ids that the given objects reference with **weight > 0**.

    Only numeric group names count (HIMI merged-skeleton namespace).  Callers must
    pass **non-stub** object names: a stub created in this round must never become
    evidence for the next one (计划书 §7.7.5 的第一个消费点 / ST9).
    """
    used: set = set()
    try:
        bpy = _require_bpy()
    except Exception:  # noqa: BLE001
        return used
    for name in object_names or ():
        name = str(name or "")
        if not name:
            continue
        try:
            obj = bpy.data.objects.get(name)
        except Exception:  # noqa: BLE001
            continue
        if obj is None:
            continue
        try:
            if obj.get(HIMI_STUB_MARKER):
                continue
        except Exception:  # noqa: BLE001
            pass
        vertices = getattr(getattr(obj, "data", None), "vertices", None)
        if vertices is None:
            continue
        for vertex in vertices:
            for group_elem in getattr(vertex, "groups", ()) or ():
                try:
                    if float(group_elem.weight) <= 0:
                        continue
                    group_index = int(group_elem.group)
                    group_name = str(obj.vertex_groups[group_index].name).strip()
                except (AttributeError, IndexError, TypeError, ValueError):
                    continue
                if group_name.isdigit():
                    used.add(int(group_name))
    return used


def blender_create_stub_object(bare: str) -> str:
    """Create the 3-vertex stub object for ``bare``; return its object name.

    Weights sit on the **only** group, named ``HIMI_STUB_SLOT_GROUP_NAME`` (``"0"``): the export writes ``BLENDINDICES`` from the group **index**
    (``get_blendweights_blendindices_v3``) ⇒ a single-group object always lands on global slot 0 ⇒ **no new slot** (计划书 §7.7.5；t45 实测 ``distinct_slots=[0]``).
    HIMI does **not** run ``_prepare_merged_skeleton_vertex_groups`` (the gate only has EFMI / ZZMI branches); that preprocess would be an identity for a
    single-group object anyway ⇒ this conclusion does not depend on it（§7.7.5 的 t45 机制链）。

    Raises on failure -- callers must treat that as fatal (a stub is the only
    evidence that the original draw gets suppressed).
    """
    bpy = _require_bpy()
    workspace_unique_str = _stub_workspace_unique_str(bare)
    mesh = None
    obj = None
    try:
        mesh = bpy.data.meshes.new(name=HIMI_STUB_MESH_PREFIX + workspace_unique_str)
        mesh.from_pydata(
            list(HIMI_STUB_TRIANGLE_VERTICES),
            [],
            [tuple(face) for face in HIMI_STUB_TRIANGLE_FACES],
        )
        mesh.update()
        obj = bpy.data.objects.new(name=workspace_unique_str, object_data=mesh)
        obj[HIMI_STUB_MARKER] = 1
        obj["3DMigoto:WorkspaceUniqueStr"] = workspace_unique_str
        vertex_group = obj.vertex_groups.new(name=HIMI_STUB_SLOT_GROUP_NAME)
        vertex_group.add([0, 1, 2], 1.0, "REPLACE")
        try:
            bpy.context.collection.objects.link(obj)
        except Exception:  # noqa: BLE001
            bpy.context.scene.collection.objects.link(obj)
        return str(obj.name)
    except Exception:
        if obj is not None:
            blender_remove_stub_object_data(obj)
        elif mesh is not None and getattr(mesh, "users", 0) == 0:
            try:
                bpy.data.meshes.remove(mesh)
            except (AttributeError, RuntimeError):
                pass
        raise


# --------------------------------------------------------------------------- #
# 4) 文件 / 缓存辅助
# --------------------------------------------------------------------------- #


def derive_role_key(role_dir_name: str) -> str:
    """File-name-safe role key.

    HI3 role directories contain ``♪``/``♥``/``！``/``×``; those cannot go into
    resource names or ``Meshes/`` file names.  Keep the safe characters and
    append a short stable digest of the original name so two roles that sanitize
    to the same stem never collide.
    """
    name = str(role_dir_name or "").strip()
    safe = re.sub(r"[^0-9A-Za-z_]+", "_", name).strip("_")
    safe = re.sub(r"_{2,}", "_", safe)[:24]
    digest = hashlib.sha1(name.encode("utf-8")).hexdigest()[:8]
    return f"{safe}_{digest}" if safe else f"role_{digest}"


def resolve_frame_analysis_dir(workspace_root: str) -> str:
    """Locate the FrameAnalysis directory recorded by a HIMI workspace.

    Same primary contract as the EFMI/ZZMI helpers (``Config/FrameAnalysisPath.json``
    then ``Config/Tabs/ws-tab-*.json``), but HIMI **does not** fall back to "the
    newest FrameAnalysis-* under the current game folder": a wrong frame would
    silently produce a skeleton for a different pose.  A missing source must be
    repaired from the workspace cache or reported, not swapped.
    """
    def _valid(path) -> str:
        text = str(path or "").strip()
        return text if text and os.path.isdir(text) else ""

    root = str(workspace_root or "").strip()
    if not root:
        return ""
    config_path = os.path.join(root, "Config", "FrameAnalysisPath.json")
    if os.path.isfile(config_path):
        try:
            payload = JsonUtils.LoadFromFile(config_path)
            found = _valid(payload.get("frameAnalysisFolderPath", ""))
            if found:
                return found
        except Exception:
            pass
    tabs_dir = os.path.join(root, "Config", "Tabs")
    if os.path.isdir(tabs_dir):
        for name in sorted(os.listdir(tabs_dir)):
            if not (name.startswith("ws-tab-") and name.endswith(".json")):
                continue
            try:
                payload = JsonUtils.LoadFromFile(os.path.join(tabs_dir, name))
                found = _valid(payload.get("frameAnalysisFolderPath", ""))
                if found:
                    return found
            except Exception:
                continue
    return ""


def resolve_submesh_json_path(workspace_root: str, unique_str: str) -> str:
    """Resolve ``LOD0/<bare>/<WorkGameType>/<bare>.json`` for one component.

    HIMI's ``WorkGameType`` directories are **not** ``TYPE_``-prefixed, so the
    EFMI helper's prefix rule does not transfer.  Order:

    1. ``Import.json``'s ``<unique_str> -> WorkGameType`` mapping
       (``<lod>/<bare>/<gametype>/<bare>.json`` -- the directory may be plain
       ``GPU_...`` or ``TYPE_GPU_...``; both spellings are probed);
    2. a single ``<bare>.json`` inside the bare folder;
    3. otherwise ambiguous -> empty (explicit failure, never a guess).
    """
    bare = bare_from_unique_str(unique_str)
    lod = lod_from_unique_str(unique_str)
    if not bare:
        return ""
    base = os.path.join(str(workspace_root or ""), lod) if lod else str(workspace_root or "")
    submesh_dir = os.path.join(base, bare)
    if not os.path.isdir(submesh_dir):
        return ""

    gametype = ""
    import_path = os.path.join(str(workspace_root or ""), "Import.json")
    if os.path.isfile(import_path):
        try:
            payload = JsonUtils.LoadFromFile(import_path)
        except Exception:
            payload = None
        if isinstance(payload, dict):
            gametype = str(
                payload.get(str(unique_str), "") or payload.get(bare, "") or ""
            ).strip()
    if gametype:
        for dir_name in (gametype, f"TYPE_{gametype}"):
            candidate = os.path.join(submesh_dir, dir_name, f"{bare}.json")
            if os.path.isfile(candidate):
                return os.path.abspath(candidate)

    try:
        entries = sorted(os.listdir(submesh_dir))
    except OSError:
        return ""
    found = [
        os.path.abspath(os.path.join(submesh_dir, name, f"{bare}.json"))
        for name in entries
        if os.path.isdir(os.path.join(submesh_dir, name))
        and os.path.isfile(os.path.join(submesh_dir, name, f"{bare}.json"))
    ]
    return found[0] if len(found) == 1 else ""


def runtime_cache_path(json_path: str, bare: str, file_name: str) -> str:
    """``<component>/ModImpRuntime/<file_name>`` (pure file name only).

    The component directory is the one **named after the bare** on the json's
    ancestor chain (HIMI: ``LOD0/<bare>/<WorkGameType>/<bare>.json``, so it is
    the json's grandparent; the name lookup keeps the helper correct if a
    workspace nests one level deeper).
    """
    safe = os.path.basename(str(file_name or ""))
    if not safe:
        safe = f"{bare}-BoneMatrix.buf"
    json_path = os.path.abspath(str(json_path or ""))
    component_dir = ""
    cursor = json_path
    for _ in range(4):
        cursor = os.path.dirname(cursor)
        if not cursor or cursor == os.path.dirname(cursor):
            break
        if os.path.basename(cursor) == str(bare or ""):
            component_dir = cursor
            break
    if not component_dir:
        component_dir = os.path.dirname(json_path)
    return os.path.join(component_dir, "ModImpRuntime", safe)


def atomic_publish_bundle(entries: list[dict]) -> None:
    """Stage every source, validate it, then commit -- or restore all targets.

    Same discipline as the EFMI/ZZMI transaction helper: a single
    ``os.replace`` only makes one target atomic, so a HIMI build that publishes
    ``BoneMatrix.buf`` + the submesh json must not be able to leave a new json
    pointing at an old buffer.
    """
    import shutil
    import tempfile

    def _validate(path: str, entry: dict) -> None:
        if not os.path.isfile(path):
            raise OSError(f"缓存源文件不存在: {path}")
        size = os.path.getsize(path)
        min_size = int(entry.get("min_size", 4) or 0)
        if size < min_size:
            raise OSError(f"缓存文件大小无效: {path} ({size} bytes)")
        if bool(entry.get("float_aligned", True)) and size % 4 != 0:
            raise OSError(f"缓存文件大小未按 float32 对齐: {path} ({size} bytes)")
        exact = entry.get("exact_size")
        if exact is not None and size != int(exact):
            raise OSError(f"缓存文件大小不符（期望 {int(exact)}，实际 {size}）: {path}")

    prepared: list[dict] = []
    committed: list[dict] = []
    try:
        for raw_entry in entries:
            entry = dict(raw_entry)
            source = os.path.abspath(str(entry.get("source_path", "") or ""))
            dest = os.path.abspath(str(entry.get("dest_path", "") or ""))
            entry["source_path"] = source
            entry["dest_path"] = dest
            entry["temp_path"] = ""
            entry["backup_path"] = ""
            entry["dest_existed"] = os.path.isfile(dest)
            if not os.path.isfile(source):
                raise OSError(f"缓存源文件不存在: {source}")
            if source == dest:
                _validate(source, entry)
                prepared.append(entry)
                continue
            os.makedirs(os.path.dirname(dest), exist_ok=True)
            fd, temp_path = tempfile.mkstemp(
                prefix=f".{os.path.basename(dest)}.", suffix=".tmp", dir=os.path.dirname(dest)
            )
            os.close(fd)
            entry["temp_path"] = temp_path
            prepared.append(entry)
            shutil.copy2(source, temp_path)
            _validate(temp_path, entry)

        for entry in prepared:
            temp_path = entry.get("temp_path", "")
            if not temp_path:
                continue
            dest = entry["dest_path"]
            if entry["dest_existed"]:
                fd, backup = tempfile.mkstemp(
                    prefix=f".{os.path.basename(dest)}.",
                    suffix=".bak",
                    dir=os.path.dirname(dest),
                )
                os.close(fd)
                os.remove(backup)
                os.replace(dest, backup)
                entry["backup_path"] = backup
            os.replace(temp_path, dest)
            committed.append(entry)
    except Exception:
        for entry in reversed(committed):
            backup = entry.get("backup_path", "")
            dest = entry["dest_path"]
            try:
                if backup and os.path.isfile(backup):
                    os.replace(backup, dest)
                elif not entry.get("dest_existed") and os.path.isfile(dest):
                    os.remove(dest)
            except OSError:
                pass
        raise
    finally:
        for entry in prepared:
            for key in ("temp_path", "backup_path"):
                path = entry.get(key, "")
                if path and os.path.isfile(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass


# --------------------------------------------------------------------------- #
# 5) 幂等总流程
# --------------------------------------------------------------------------- #


def _discover_landed_components(workspace_root: str) -> list[str]:
    """All ``LOD*.<bare>`` unique strings that have a resolvable json."""
    root = str(workspace_root or "").strip()
    if not root or not os.path.isdir(root):
        return []
    found: list[str] = []
    try:
        lod_entries = sorted(
            name
            for name in os.listdir(root)
            if name.upper().startswith("LOD") and os.path.isdir(os.path.join(root, name))
        )
    except OSError:
        return []
    for lod in lod_entries:
        lod_dir = os.path.join(root, lod)
        try:
            bares = sorted(name for name in os.listdir(lod_dir) if os.path.isdir(os.path.join(lod_dir, name)))
        except OSError:
            continue
        for bare in bares:
            unique_str = f"{lod}.{bare}"
            if resolve_submesh_json_path(root, unique_str):
                found.append(unique_str)
    return found


class HIMISkeletonMergeHelper:
    """Idempotent HIMI merged-skeleton build: dump -> workspace."""

    #: 单组件 palette 缓存文件的固定后缀
    PALETTE_CACHE_SUFFIX = "-BoneMatrix.buf"

    # ------------------------------------------------------------------ #
    # 路径 / 缓存门
    # ------------------------------------------------------------------ #

    @staticmethod
    def resolve_frame_analysis_dir(workspace_root: str) -> str:
        return resolve_frame_analysis_dir(workspace_root)

    @staticmethod
    def _resolve_submesh_json_path(workspace_root: str, unique_str: str) -> str:
        return resolve_submesh_json_path(workspace_root, unique_str)

    @staticmethod
    def _is_himi_metadata_complete(payload: dict) -> bool:
        """The HIMI merged fields must be present and self-consistent."""
        if not isinstance(payload, dict):
            return False
        required = (
            "VGOffset",
            "VGCount",
            "VGMap",
            "SkeletonGroup",
            "DeformDrawIndex",
            "OriginalVertexCount",
            "VGMapAlgorithmVersion",
        )
        if not any(field in payload for field in required):
            return True  # 从未写过：交给调用方按「缺数据」处理
        for field in required:
            if field not in payload:
                return False
        try:
            vg_count = int(payload["VGCount"])
            vg_offset = int(payload["VGOffset"])
            algorithm = int(payload["VGMapAlgorithmVersion"])
            skeleton_group = int(payload["SkeletonGroup"])
            int(payload["DeformDrawIndex"])
            int(payload["OriginalVertexCount"])
        except (TypeError, ValueError):
            return False
        if vg_count < 0 or vg_offset < 0 or algorithm < 0 or skeleton_group != 0:
            return False
        vg_map = payload.get("VGMap")
        if not isinstance(vg_map, dict):
            return False
        try:
            keys = sorted(int(key) for key in vg_map)
            values = [int(value) for value in vg_map.values()]
        except (TypeError, ValueError):
            return False
        if any(value < 0 for value in values):
            return False
        # importer 要求 VGMap 是 0..VGCount-1 的完整覆盖
        return keys == list(range(vg_count))

    @classmethod
    def missing_merged_metadata_exist(cls, workspace_root: str, unique_str_list) -> bool:
        """True when any requested component lacks (or has broken) merged data."""
        root = str(workspace_root or "").strip()
        if not root:
            return False
        for unique_str in unique_str_list or []:
            json_path = cls._resolve_submesh_json_path(root, unique_str)
            if not json_path:
                continue
            try:
                payload = JsonUtils.LoadFromFile(json_path)
            except Exception:
                return True
            if not isinstance(payload, dict):
                return True
            # CPU/无顶点组目标（显式 GPU-PreSkinning=False）不参与骨骼合并，因此
            # 它没有合并元数据**不是**缺陷。与 EFMI/ZZMI 同款判据
            # （efmi_skeleton.py:5872、zzmi_skeleton.py:1334）。
            if payload.get("GPU-PreSkinning") is False:
                continue
            if not payload.get("VGMap"):
                return True
            if int(payload.get("VGMapAlgorithmVersion", 0) or 0) != HIMI_VG_MAP_ALGORITHM_VERSION:
                return True
            if not cls._is_himi_metadata_complete(payload):
                return True
            cache_path = runtime_cache_path(
                json_path,
                bare_from_unique_str(unique_str),
                str(payload.get("BoneMatrixFileName", "") or ""),
            )
            if not os.path.isfile(cache_path):
                return True
            try:
                expected = int(payload.get("BoneMatrixSlotCount", 0) or 0) * PALETTE_STRIDE_BYTES
            except (TypeError, ValueError):
                return True
            if expected <= 0 or os.path.getsize(cache_path) != expected:
                return True
        return False

    # ------------------------------------------------------------------ #
    # 构建
    # ------------------------------------------------------------------ #

    @classmethod
    def _himi_cache_intact_without_dump(cls, root: str, needed_bares) -> dict:
        """Structural cache-only integrity check -- **no dump read at all**.

        Used when the recorded FrameAnalysis directory is gone (or, defensively,
        as a second opinion).  It answers exactly one question: *does the
        workspace still carry a complete, self-consistent set of merged-skeleton
        artifacts?*  Deriving the values again would need the dump, so every
        check here is relational (recorded vs on-disk) rather than computational:

        1. ``Config/HIMIMergedSkeleton.json`` exists and parses;
        2. its ``AlgorithmVersion`` / ``GateVersion`` are current;
        3. ``SkeletonFileName`` / ``SkeletonDigest`` / ``SlotCount`` are usable
           and the skeleton file is ``SlotCount * 48`` bytes with matching sha256;
        4. every **needed** component has merged metadata whose
           ``HIMIMergedSkeletonDigest`` equals the role digest, whose
           ``VGMapAlgorithmVersion`` is current, whose ``VGMap`` is a complete
           ``0..VGCount-1`` cover, and whose ``ModImpRuntime`` palette cache
           exists at ``BoneMatrixSlotCount * 48`` bytes;
        5. every component recorded in the role metadata is still present (a
           renamed/removed component means the cache no longer describes the
           workspace).

        Returns ``{"valid": bool, "reason": str, "checked": int, "missing": [...]}``;
        ``reason`` is empty when valid.
        """
        root = str(root or "").strip()
        needed = sorted({str(name) for name in (needed_bares or []) if str(name).strip()})
        result = {"valid": False, "reason": "", "checked": 0, "missing": []}
        if not root or not os.path.isdir(root):
            result["reason"] = f"工作空间目录不存在: {root}"
            return result

        metadata_path = os.path.join(root, "Config", HIMI_MERGED_SKELETON_METADATA_FILE)
        if not os.path.isfile(metadata_path):
            result["reason"] = f"角色级合并骨架元数据缺失（{HIMI_MERGED_SKELETON_METADATA_FILE}）"
            return result
        try:
            metadata = JsonUtils.LoadFromFile(metadata_path)
        except Exception as exc:  # noqa: BLE001
            result["reason"] = f"角色级元数据无法解析: {exc}"
            return result
        if not isinstance(metadata, dict) or not metadata:
            result["reason"] = "角色级元数据为空或格式错误"
            return result
        if int(metadata.get("AlgorithmVersion", 0) or 0) != HIMI_VG_MAP_ALGORITHM_VERSION:
            result["reason"] = (
                f"角色级元数据算法版本不是当前版本"
                f"（需 {HIMI_VG_MAP_ALGORITHM_VERSION}，实为 {metadata.get('AlgorithmVersion')!r}）"
            )
            return result
        if int(metadata.get("GateVersion", 0) or 0) != HIMI_DEFORM_GATE_VERSION:
            result["reason"] = (
                f"角色级元数据门控版本不是当前版本"
                f"（需 {HIMI_DEFORM_GATE_VERSION}，实为 {metadata.get('GateVersion')!r}）"
            )
            return result

        try:
            slot_count = int(metadata.get("SlotCount", 0) or 0)
        except (TypeError, ValueError):
            result["reason"] = "角色级元数据 SlotCount 非法"
            return result
        if slot_count <= 0:
            result["reason"] = f"角色级元数据 SlotCount 非正（{slot_count}）"
            return result
        digest = str(metadata.get("SkeletonDigest", "") or "")
        skeleton_name = str(metadata.get("SkeletonFileName", "") or "")
        if not digest or not skeleton_name or os.path.basename(skeleton_name) != skeleton_name:
            result["reason"] = "角色级元数据的骨架文件名/摘要非法"
            return result
        skeleton_path = os.path.join(root, "Config", skeleton_name)
        if not os.path.isfile(skeleton_path):
            result["reason"] = f"角色级骨架文件缺失: {skeleton_name}"
            return result
        try:
            if os.path.getsize(skeleton_path) != slot_count * PALETTE_STRIDE_BYTES:
                result["reason"] = (
                    f"角色级骨架大小不符（期望 {slot_count * PALETTE_STRIDE_BYTES}，"
                    f"实为 {os.path.getsize(skeleton_path)}）"
                )
                return result
            with open(skeleton_path, "rb") as handle:
                if _sha256_hex(handle.read()) != digest:
                    result["reason"] = "角色级骨架字节与元数据摘要不符"
                    return result
        except OSError as exc:
            result["reason"] = f"角色级骨架不可读: {exc}"
            return result

        def _bare_of(entry) -> str:
            if isinstance(entry, dict):
                return bare_from_unique_str(str(entry.get("bare", "") or ""))
            return bare_from_unique_str(str(entry))

        recorded = {
            bare
            for bare in (_bare_of(item) for item in (metadata.get("ComponentSet") or []))
            if bare
        }
        if not recorded:
            result["reason"] = "角色级元数据缺少 ComponentSet（旧缓存，无法判定集合完整性）"
            return result

        checked = 0
        for bare in needed:
            if bare not in recorded:
                result["missing"].append(bare)
                continue
            json_path = cls._resolve_submesh_json_path(root, bare)
            if not json_path:
                json_path = cls._resolve_submesh_json_path(root, f"LOD0.{bare}")
            if not json_path:
                result["missing"].append(bare)
                continue
            try:
                payload = JsonUtils.LoadFromFile(json_path)
            except Exception:
                result["missing"].append(bare)
                continue
            if not isinstance(payload, dict):
                result["missing"].append(bare)
                continue
            try:
                vg_count = int(payload.get("VGCount", 0) or 0)
            except (TypeError, ValueError):
                result["missing"].append(bare)
                continue
            vg_map = payload.get("VGMap")
            if (
                int(payload.get("VGMapAlgorithmVersion", 0) or 0) != HIMI_VG_MAP_ALGORITHM_VERSION
                or str(payload.get("HIMIMergedSkeletonDigest", "") or "") != digest
                or not isinstance(vg_map, dict)
                or vg_count <= 0
                or sorted(int(key) for key in vg_map) != list(range(vg_count))
            ):
                result["missing"].append(bare)
                continue
            try:
                slot_total = int(payload.get("BoneMatrixSlotCount", 0) or 0)
            except (TypeError, ValueError):
                result["missing"].append(bare)
                continue
            if slot_total <= 0:
                result["missing"].append(bare)
                continue
            cache_path = runtime_cache_path(
                json_path, bare, str(payload.get("BoneMatrixFileName", "") or "")
            )
            if not os.path.isfile(cache_path):
                result["missing"].append(bare)
                continue
            try:
                if os.path.getsize(cache_path) != slot_total * PALETTE_STRIDE_BYTES:
                    result["missing"].append(bare)
                    continue
            except OSError:
                result["missing"].append(bare)
                continue
            checked += 1

        result["checked"] = checked
        if result["missing"]:
            result["reason"] = (
                "以下组件的合并骨架缓存缺失或损坏: " + ", ".join(sorted(result["missing"])[:8])
            )
            return result
        if checked != len(needed):
            result["reason"] = f"仅校验了 {checked}/{len(needed)} 个组件"
            return result
        result["valid"] = True
        return result

    @classmethod
    def cache_only_plan(cls, root: str, landed, explicit_target=None, force: bool = False) -> dict:
        """Cache-only plan used when the FrameAnalysis directory is gone.

        The workspace cache holds every derived quantity (``VGMap`` /
        ``VGCount`` / per-component palette slices with their order contract /
        the role-level static skeleton bytes with a digest), so a rebuild is not
        needed -- and is not possible without the dump.  This mirrors the export
        side's ``gate_source=cache-only`` behaviour instead of failing.

        **Only the import path (``explicit_target is None``) may take this
        shortcut.**  A ``target_bares`` request means the caller wants the G1
        family/pass gate recomputed against a specific set, which requires the
        dump; answering it from cache would report "nothing rebindable" and
        silently turn the caller's own conservative fallback into a no-op.  That
        case fails loudly here so the caller keeps its own (correct) handling.

        Failures stay **explicit** and never write: if the cache is not complete
        and self-consistent the caller gets ``ok=False`` and a message that names
        the broken pieces, so nothing can silently degrade into a partial or
        stale skeleton.
        """
        root = str(root or "").strip()
        if explicit_target is not None:
            return {
                "ok": False,
                "cache_only": False,
                "gate_source": "cache-only-unavailable",
                "message": (
                    "工作空间登记 FrameAnalysis 已失效"
                    "（Config/FrameAnalysisPath.json 指向的目录不存在）；"
                    "调用方请求了按目标集合重算 G1（target_bares），但脱离 dump 无法做 "
                    "pass 级重算（cache-only 只能复现导入期的缓存结论，不能替调用方判定门控）。"
                    "HIMI 不做「自动改用最新帧」兜底：请恢复该 FrameAnalysis 帧后重试"
                    "（本次未写入任何文件）。"
                ),
                "writes": [],
                "landed": list(landed or []),
                "needed_bares": [],
                "cache_only_missing": [],
                "cache_only_checked": 0,
            }
        needed = cls._cache_only_needed_bares(root, landed)
        verdict = cls._himi_cache_intact_without_dump(root, needed)
        gate_source = "cache-only" if verdict["valid"] else "cache-only-invalid"
        skeleton_name = ""
        slot_count = 0
        digest = ""
        try:
            metadata = JsonUtils.LoadFromFile(
                os.path.join(root, "Config", HIMI_MERGED_SKELETON_METADATA_FILE)
            )
            if isinstance(metadata, dict):
                skeleton_name = str(metadata.get("SkeletonFileName", "") or "")
                slot_count = int(metadata.get("SlotCount", 0) or 0)
                digest = str(metadata.get("SkeletonDigest", "") or "")
        except Exception:
            pass

        if force and verdict["valid"]:
            # 强制重建需要 dump：没有 dump 只能复用缓存，不能谎报「已重算」。
            return {
                "ok": False,
                "cache_only": True,
                "gate_source": gate_source,
                "message": (
                    "工作空间登记 FrameAnalysis 已失效"
                    "（Config/FrameAnalysisPath.json 指向的目录不存在），"
                    "无法按要求强制重算；cache-only 缓存虽完好但不能替代一次真实重建。"
                    "HIMI 不做「自动改用最新帧」兜底：请重新选择提取源后再次导入"
                    "（本次未写入任何文件）。"
                ),
                "writes": [],
                "landed": list(landed or []),
                "needed_bares": needed,
                "cache_only_missing": verdict["missing"],
                "cache_only_checked": verdict["checked"],
            }

        if not verdict["valid"]:
            return {
                "ok": False,
                "cache_only": False,
                "gate_source": gate_source,
                "message": (
                    "工作空间登记 FrameAnalysis 已失效"
                    "（Config/FrameAnalysisPath.json 指向的目录不存在），"
                    "且工作空间缓存不完整或已损坏，无法 cache-only 重建："
                    f"{verdict['reason']}。"
                    "HIMI 不做「自动改用最新帧」兜底：请重新选择提取源后再次导入"
                    "（本次未写入任何文件）。"
                ),
                "writes": [],
                "landed": list(landed or []),
                "needed_bares": needed,
                "cache_only_missing": verdict["missing"],
                "cache_only_checked": verdict["checked"],
            }

        return {
            "ok": True,
            "cache_only": True,
            "gate_source": gate_source,
            "cache_intact": True,
            "message": (
                f"dump 目录缺失，但工作空间缓存完好的 {verdict['checked']} 个组件"
                f"与角色级骨架（{slot_count} 槽）已通过完整性校验；"
                "本次不重算、不写盘（cache-only）"
            ),
            "workspace_root": root,
            "dump_dir": "",
            "role_key": derive_role_key(os.path.basename(os.path.abspath(root))),
            "slot_count": slot_count,
            "skeleton_bytes": b"",
            "skeleton_path": os.path.join(root, "Config", skeleton_name) if skeleton_name else "",
            "metadata_path": os.path.join(root, "Config", HIMI_MERGED_SKELETON_METADATA_FILE),
            "metadata": {},
            "writes": [],
            "rejections": [],
            "gate_report": {},
            "gate_rejected": [],
            "resolver": None,
            "landed": list(landed or []),
            "needed_bares": needed,
            "cache_only_checked": verdict["checked"],
            "cache_only_digest": digest,
        }

    @classmethod
    def _cache_only_needed_bares(cls, root: str, landed, target=None) -> list[str]:
        """Which component bares the cache-only check must cover.

        ``target`` is always ``None`` here: a ``target_bares`` request is refused
        earlier (it needs the dump to recompute the pass gate).  The check
        therefore covers **every landed** component, because a component that
        appeared after the last build has no merged metadata yet and that must
        force a real rebuild rather than be silently skipped.

        Keep this reachable with a non-``None`` ``target`` for callers that only
        want a subset (export-side style): the requested names are unioned with
        the landed set and unresolvable names are dropped, so a bare-only name or
        a stale entry cannot turn a satisfiable subset request into a failure.
        """
        names = {bare_from_unique_str(str(name)) for name in (landed or []) if str(name).strip()}
        if target is not None:
            names |= {bare_from_unique_str(str(name)) for name in target if str(name).strip()}
            resolvable = set()
            for name in names:
                # 同时试「带 LOD 前缀的 unique_str」与「裸组件名」：调用方既可能传
                # `LOD0.<bare>`，也可能只传 `<bare>`（导出侧就是后者）。
                if cls._resolve_submesh_json_path(root, name) or cls._resolve_submesh_json_path(
                    root, f"LOD0.{name}"
                ):
                    resolvable.add(name)
            return sorted(resolvable)
        return sorted(names)

    @classmethod
    def prepare(
        cls,
        workspace_root: str,
        unique_str_list=None,
        force: bool = False,
        target_bares=None,
    ) -> dict:
        """Compute everything needed for the write-back **without touching disk**.

        Returns a plan dict; :meth:`commit` applies it.  Splitting the two keeps
        the resolver/merge unit-testable against real dumps with zero writes.

        ``target_bares`` overrides the **merge target set** used by the deform
        family gate (G1).  It defaults to every landed component, which is the
        import-side semantics; the export side passes the components it is
        actually about to emit, so a component whose family is not fully part of
        that export set is rejected (``HIMIDeformGateSafe = False``).

        When the recorded FrameAnalysis directory is gone but the workspace
        cache is still complete, the call returns an **ok=True cache-only plan**
        instead of failing (see :meth:`cache_only_plan`): the cache already holds
        every derived quantity, so nothing has to be recomputed and nothing is
        written.
        """
        root = str(workspace_root or "").strip()
        if not root or not os.path.isdir(root):
            return {"ok": False, "message": f"工作空间不存在: {root}", "writes": []}

        explicit_target = (
            None if target_bares is None else {str(name).strip() for name in target_bares}
        )

        landed = [str(name) for name in (unique_str_list or []) if str(name).strip()]
        landed = [name for name in landed if cls._resolve_submesh_json_path(root, name)]
        if not landed:
            landed = _discover_landed_components(root)
        landed = sorted(set(landed))
        if not landed:
            return {"ok": False, "message": "工作空间里没有可解析的 LOD 子网格 json", "writes": []}

        dump_dir = cls.resolve_frame_analysis_dir(root)
        if not dump_dir:
            # 「无 dump」不再直接失败：缓存完好时走 cache-only 快路径
            # （对齐导出侧 load_workspace_plan 的 gate_source=cache-only）。
            return cls.cache_only_plan(root, landed, explicit_target, force=force)

        components: list[HIMIComponent] = []
        for unique_str in landed:
            json_path = cls._resolve_submesh_json_path(root, unique_str)
            try:
                payload = JsonUtils.LoadFromFile(json_path)
            except Exception:
                payload = None
            if not isinstance(payload, dict) or not payload:
                continue
            components.append(HIMIComponent(unique_str, json_path, payload))
        if not components:
            return {"ok": False, "message": "没有可读取的子网格 json", "writes": []}

        resolver = HIMIDeformResolver(dump_dir, components)
        joined = resolver.joined_components()
        if not joined:
            return {
                "ok": False,
                "message": (
                    "没有组件能 join 到同帧 deform pass（三条键 CategoryHash.Position / "
                    "VertexLimitVB / IB 家族全部未命中）。请确认工作空间登记的提取帧与这些组件同源。"
                ),
                "writes": [],
                "resolver": resolver,
            }

        # 组件级参与门（F3）：显式 GPU-PreSkinning=False 的 CPU/无顶点组目标不参与骨骼
        # 合并（与 EFMI/ZZMI 同款判据），并且**不得**因此拖累同 pass 的其它家族。
        cpu_skipped = [c for c in joined if not c.gpu_preskinning]
        if cpu_skipped:
            joined = [c for c in joined if c.gpu_preskinning]

        # G1 目标集合 = 本次实际要纳入合并的组件；家族不完整 ⇒ 整家族不合并。
        # 导入侧（target_bares=None）默认 = 全部已落盘组件。
        target_bares = (
            [c.bare for c in joined] if explicit_target is None else sorted(explicit_target)
        )
        cpu_bares = {c.bare for c in cpu_skipped}
        gate_report = resolver.family_gate(target_bares, skip_bares=cpu_bares)
        gate_rejected = {
            draw_ib: entry for draw_ib, entry in gate_report.items() if not entry.get("safe")
        }

        # 门控必须做到 **pass 级**（F4）：重绑是按 deform pass 生效的，所以一个 pass
        # 被多个家族共享时，只要有一个家族不完整，这个 pass 上的**全部**组件都不得
        # 进入合并骨架——哪怕它自己那个家族是完整的。
        gate_safe_bares: set[str] = set()
        pass_blocked: dict[str, dict] = {}
        for component in joined:
            deform_pass = component.deform_pass or {}
            family_safe = bool((gate_report.get(component.draw_ib) or {}).get("safe"))
            pass_safe = resolver.gate_allows(deform_pass, gate_report, skip_bares=cpu_bares)
            if family_safe and pass_safe:
                gate_safe_bares.add(component.bare)
                continue
            if not pass_safe:
                families = resolver.pass_families(deform_pass, skip_bares=cpu_bares)
                pass_blocked.setdefault(
                    deform_pass.get("draw", ""),
                    {
                        "draw": deform_pass.get("draw", ""),
                        "families": families,
                        "unsafe_families": [
                            family
                            for family in families
                            if not bool((gate_report.get(family) or {}).get("safe"))
                        ],
                        "missing": sorted(
                            {
                                name
                                for family in families
                                for name in ((gate_report.get(family) or {}).get("missing") or [])
                            }
                        ),
                    },
                )

        merge_components = [c for c in joined if c.bare in gate_safe_bares]

        parts: list[dict] = []
        slot_stats: dict[str, dict] = {}
        for component in merge_components:
            try:
                stats = HIMIBoneMapBuilder.referenced_slots(component.blend_path)
            except OSError:
                continue
            pool_slots = HIMIBoneMapBuilder.palette_pool_slot_count(component.palette_path)
            if stats["index_max"] >= 0 and stats["index_max"] >= pool_slots:
                return {
                    "ok": False,
                    "message": (
                        f"{component.bare}: BLENDINDICES 最大值 {stats['index_max']} "
                        f"超出 palette 池大小 {pool_slots}（越界，拒绝生成）"
                    ),
                    "writes": [],
                    "resolver": resolver,
                }
            if stats["negative_index_count"]:
                return {
                    "ok": False,
                    "message": f"{component.bare}: BLENDINDICES 含负值（SINT 越界），拒绝生成",
                    "writes": [],
                    "resolver": resolver,
                }
            slot_stats[component.bare] = stats
            parts.append(
                {
                    "bare": component.bare,
                    "palette_path": component.palette_path,
                    "slots": stats["slots"],
                    "pool_slots": pool_slots,
                    "palette_scale": HIMIBoneMapBuilder.palette_scale(component.palette_path),
                    # 组件级「排除去重」（F3）：独占自己的槽位，不与其它组件共享。
                    "dedup_excluded": bool(component.dedup_excluded),
                }
            )

        merged = HIMIBoneMapBuilder.build_merged_skeleton(parts)
        if merged["slot_count"] > HIMI_MERGED_SKELETON_SLOT_LIMIT:
            return {
                "ok": False,
                "message": (
                    f"合并骨架槽位数 {merged['slot_count']} 超过上限 "
                    f"{HIMI_MERGED_SKELETON_SLOT_LIMIT}"
                ),
                "writes": [],
                "resolver": resolver,
            }

        role_key = derive_role_key(os.path.basename(os.path.abspath(root)))
        skeleton_name = f"{role_key}-MergedSkeleton.buf"
        skeleton_path = os.path.join(root, "Config", skeleton_name)
        metadata_path = os.path.join(root, "Config", HIMI_MERGED_SKELETON_METADATA_FILE)
        log_signature = resolver.parser.log_signature if resolver.parser else ""
        digest = _sha256_hex(merged["skeleton_bytes"])

        writes: list[dict] = []
        payloads: dict[str, dict] = {}
        gated_out: list[dict] = []

        for component in joined:
            if component.bare not in gate_safe_bares:
                entry = gate_report.get(component.draw_ib) or {}
                draw = (component.deform_pass or {}).get("draw", "")
                blocked = pass_blocked.get(draw) or {}
                # 家族自身完整、但共享的 pass 被别的家族拖累 ⇒ 用 pass 级 reason，
                # 诊断文案必须能区分这两种拒绝（F4）。
                family_safe = bool(entry.get("safe"))
                if family_safe and blocked:
                    gated_out.append(
                        {
                            "bare": component.bare,
                            "draw_ib": component.draw_ib,
                            "reason": "deform_pass_family_incomplete",
                            "detail": (
                                f"deform pass {draw} 被多个 IB 家族共享，其中 "
                                f"{', '.join(blocked.get('unsafe_families') or [])} 不完整；"
                                "重绑按 pass 生效，故本组件也不参与合并"
                            ),
                            "deform_draw": draw,
                            "families": list(blocked.get("families") or []),
                            "unsafe_families": list(blocked.get("unsafe_families") or []),
                            "missing": list(blocked.get("missing") or []),
                        }
                    )
                else:
                    gated_out.append(
                        {
                            "bare": component.bare,
                            "draw_ib": component.draw_ib,
                            "reason": "deform_family_incomplete",
                            "missing": list(entry.get("missing") or []),
                            "deform_draw": draw,
                        }
                    )
                continue
            part = merged["per_part"].get(component.bare) or {}
            stats = slot_stats.get(component.bare) or {}
            vg_map = part.get("vg_map") or {}
            vg_count = int(part.get("vg_count") or 0)
            if vg_count <= 0 or not vg_map:
                continue

            # palette 缓存：只保留**被引用槽**的 48 字节切片（按局部下标升序）
            slots = list(part.get("referenced_slots") or [])
            with open(component.palette_path, "rb") as handle:
                pool_bytes = handle.read()
            cache_bytes = b"".join(
                pool_bytes[local * PALETTE_STRIDE_BYTES : (local + 1) * PALETTE_STRIDE_BYTES]
                for local in slots
            )
            bare = component.bare
            cache_name = f"{bare}{cls.PALETTE_CACHE_SUFFIX}"
            cache_path = runtime_cache_path(component.json_path, bare, cache_name)

            payload = dict(component.json_payload)
            payload["VGCount"] = vg_count
            payload["VGOffset"] = 0
            payload["VGMap"] = {str(key): int(value) for key, value in sorted(vg_map.items())}
            payload["VGMapAlgorithmVersion"] = HIMI_VG_MAP_ALGORITHM_VERSION
            payload["SkeletonGroup"] = 0
            payload["DeformDrawIndex"] = int(component.deform_pass.get("draw_index", 0) or 0)
            payload["OriginalVertexCount"] = int(stats.get("vertex_count", 0) or 0)
            payload["BoneMatrixFileName"] = cache_name
            payload["BoneMatrixSlotCount"] = len(slots)
            payload["FrameAnalysisLogSignature"] = log_signature
            payload["BoneIdentityVersion"] = HIMI_VG_MAP_ALGORITHM_VERSION
            payload["BoneIdentityDigest"] = hashlib.sha256(
                ",".join(f"{k}:{v}" for k, v in sorted(vg_map.items())).encode("utf-8")
            ).hexdigest()
            payload["BoneIdentityBasis"] = "local_slot_map"
            payload["BoneIdentityBasisNote"] = (
                "local_slot_map：槽位号由当帧 palette 矩阵 bitwise 去重派生，"
                "**不是**骨名/编号身份、也不是帧不变量"
            )
            payload["BoneMatrixFrameDigest"] = _sha256_hex(pool_bytes)
            payload["BoneMatrixFrameDigestNote"] = (
                "当帧矩阵指纹——只证明同一次抓帧，不是骨骼身份依据"
            )
            payload["VGMapDedupExcluded"] = bool(component.dedup_excluded)
            payload["HIMIPalettePoolSlotCount"] = int(part.get("palette_pool_slots") or 0)
            payload["HIMIPaletteScale"] = float(
                next(
                    (p.get("palette_scale") for p in parts if p["bare"] == bare),
                    0.0,
                )
            )
            payload["HIMIMergedSkeletonFile"] = os.path.join("Config", skeleton_name)
            payload["HIMIMergedSkeletonSlots"] = int(merged["slot_count"])
            payload["HIMIMergedSkeletonDigest"] = digest
            payload["HIMIDeformFamilyIB"] = component.draw_ib
            payload["HIMIDeformFamilyMembers"] = list(
                (gate_report.get(component.draw_ib) or {}).get("members") or []
            )
            payload["HIMIDeformGateSafe"] = True
            payload["HIMIDeformGateVersion"] = HIMI_DEFORM_GATE_VERSION
            payload["HIMIDeformJoinPath"] = component.join_path
            # HIMI 不适用（D05/D06）：显式清理 ZZMI 的三件套
            payload.pop("ObjectCB1FileName", None)
            payload.pop("ObjectCB1FirstConstant", None)
            payload["ObjectCB1CacheValid"] = False

            payloads[bare] = payload
            writes.append(
                {
                    "bare": bare,
                    "json_path": component.json_path,
                    "cache_path": cache_path,
                    "cache_bytes": cache_bytes,
                    "payload": payload,
                    "palette_pool_slots": int(part.get("palette_pool_slots") or 0),
                    "referenced_slots": slots,
                    "slots_weight_positive": list(stats.get("slots_weight_positive") or []),
                    "vg_count": vg_count,
                }
            )

        # ---- G1-b 的站位清单（计划书 §7.7.6 逐字要求的两个只读字段）----
        #
        # 与导出侧**同一个判据函数**（`decide_stub_injection`）⇒ 单一事实源。
        # 场景真相这里拿不到（数据层没有 bpy），因此按「导出集合里的已落盘组件
        # 都在场景中」这一**保守**口径计算：`scene_present_bares = 全部已落盘 bare`。
        # 结果是「规则① 不会触发、规则② 的 used 由已落盘组件的 VGMap 值域近似」——
        # 对「站位清单」这个**只读诊断字段**足够（导出侧的真实决策仍由它自己算，
        # 两者口径不同的部分会体现在导出侧诊断里）。
        try:
            stub_decision = decide_stub_injection(
                workspace_root=root,
                scene_present_bares={c.bare for c in components},
                used_group_ids=set(),
            )
        except Exception:  # noqa: BLE001 - 只读诊断字段绝不阻断写回
            stub_decision = HIMIStubDecision()

        metadata = {
            "AlgorithmVersion": HIMI_VG_MAP_ALGORITHM_VERSION,
            "GateVersion": HIMI_DEFORM_GATE_VERSION,
            "RoleKey": role_key,
            "SlotCount": int(merged["slot_count"]),            "Slots": [
                {
                    "slot": int(record["slot"]),
                    "source_components": sorted(set(record["source_components"])),
                    "source_local": [int(v) for v in record["source_local"]],
                    "matrix_sha256": record["matrix_sha256"],
                }
                for record in merged["slots"]
            ],
            "SkeletonFileName": skeleton_name,
            "SkeletonDigest": digest,
            "SourceLogSignature": log_signature,
            "FrameAnalysisPath": dump_dir,
            "ComponentCount": len(writes),
            # 本次写回覆盖的组件集合（bare，排序）。cache-only 快路径靠它判定
            # 「工作空间里出现的组件是否都已在缓存里」——没有它就无法区分
            # 「缓存完整」与「这次多了一个从未落盘的新组件」。
            "ComponentSet": sorted(w["bare"] for w in writes),
            "ReferencedSlotTotal": int(merged["referenced_slot_total"]),
            "PoolSlotTotal": int(merged["pool_slot_total"]),
            "SharedSlotCount": int(merged["shared_slot_count"]),
            "DeformPassTotal": len(resolver.passes),
            "DeformGateRejected": sorted(
                str(entry["bare"]) for entry in gated_out
            ),
            "DeformGateRejectedDetail": gated_out,
            # pass 级门控（F4）：被同 pass 其它家族拖累而整 pass 拒绝时的明细。
            "DeformGateBlockedPasses": sorted(pass_blocked.values(), key=lambda e: e["draw"]),
            "DeformGateMode": "pass",
            # 组件级参与门（F3）：GPU-PreSkinning=False 被跳过的组件。
            "CPUPreskinningSkipped": sorted(c.bare for c in cpu_skipped),
            "DedupExcludedComponents": sorted(
                c.bare for c in joined if c.dedup_excluded
            ),
            # ---- G1 与站位的交互（计划书 §7.7.6 + §7.7.11，t21/t30 裁定）----
            #
            # G1-a **站位对象不算家族覆盖**：站位件没有 json、没有 VGMap、也不在
            # ``L``（已落盘带数据集合）里 ⇒ 连 ``L(ib) ⊆ T`` 判据的**定义域**都
            # 进不去。这条是定义域事实，不是「只有 3 顶点所以不算」的几何论证。
            #
            # G1-b **下面两个字段是 ``prepare()`` 的数据层口径，不等于导出侧
            # ``export_bares``**（口径边界，务必分开读）：
            #   * 这里说的是**合成出来的站位对象**（3 顶点小三角）不在目标集合里、
            #     不算家族覆盖 —— 它没有 json / 没有 VGMap / 没有几何数据；
            #   * 导出侧**被站位的 bare**（已落盘、有 ``VGMap`` 的组件）**必须进**
            #     ``export_bares`` —— 否则 ``L(ib) ⊆ T`` 永远不成立、契约
            #     ``level=error`` ⇒ ``Fatal`` 中止导出，站位机制在它**唯一**能生效
            #     的场景里必然失效（反证链：计划书 §7.7.11.1；独立复核：
            #     ``reports/himi-merge/18-stub-rereview.md`` §4.2；裁定：计划书
            #     §7.7.11.2 方案 (A) 采用 / 方案 (B) 否决 +
            #     ``reports/himi-merge/05-decisions.md`` §3.20）。
            #   * 按 §7.7.11.3，**被站位的 bare 算已覆盖**（它进 ``T`` 就使
            #     ``L(ib) ⊆ T`` 成立）；本字段的 ``False`` 只描述**站位对象**不算覆盖。
            # 站位只决定「要不要压原版 draw」，与「能不能重绑」解耦。
            # 更精确的改名（``StubBareInExportTargetSet`` /
            # ``StubBareCountsAsFamilyCoverage`` / ``StubObjectInTargetSet`` + version
            # 1→2）登记在计划书 §7.7.11.6，**本轮只加口径说明、不改字段名与值**
            # （避免既有断言无谓漂移）。
            "StubTargetBaresIncluded": False,
            "StubFamilyCoverageCounted": False,
            # G1-c **pass 消费者层不豁免**：站位不改变 G1 的 pass 级判定。
            "StubPassGateExempt": False,
            "StubGateInteractionVersion": 1,
            "StubTargetSetBares": sorted(w["bare"] for w in writes),
            # G1-b 的**站位清单**（计划书 §7.7.6 逐字要求 `StubInjectedComponents`
            # / `StubSkippedComponents` 两个只读字段）。由数据层自己的站位判据
            # （``decide_stub_injection``，与导出侧**同一个函数**）汇总 ⇒ 单一事实源，
            # 不引入第二套口径。
            "StubInjectedComponents": sorted(stub_decision.stub_members),
            "StubSkippedComponents": sorted(stub_decision.skipped_members),
            "StubDecisionVersion": 1,
            "RejectedComponents": list(resolver.rejections),
            "Software": "TheHerta4 HIMI merged skeleton",
        }

        cache_intact = (not force) and cls._himi_cache_intact(
            root=root,
            writes=writes,
            metadata_path=metadata_path,
            skeleton_path=skeleton_path,
            digest=digest,
            slot_count=int(merged["slot_count"]),
            log_signature=log_signature,
        )

        return {
            "ok": True,
            "message": (
                f"已扫描 {len(components)} 个组件；join {len(joined)} 个；"
                f"参与合并 {len(writes)} 个；全局唯一骨骼 {merged['slot_count']}；"
                f"deform pass {len(resolver.passes)} 个"
                + (f"；门控 G1 拒绝 {len(gated_out)} 个" if gated_out else "")
            ),
            "workspace_root": root,
            "dump_dir": dump_dir,
            "role_key": role_key,
            "slot_count": int(merged["slot_count"]),
            "skeleton_bytes": merged["skeleton_bytes"],
            "skeleton_path": skeleton_path,
            "metadata_path": metadata_path,
            "metadata": metadata,
            "writes": writes,
            "rejections": list(resolver.rejections),
            "gate_report": gate_report,
            "gate_rejected": gated_out,
            "resolver": resolver,
            "cache_intact": bool(cache_intact),
        }

    @classmethod
    def _himi_cache_intact(
        cls,
        *,
        root: str,
        writes: list[dict],
        metadata_path: str,
        skeleton_path: str,
        digest: str,
        slot_count: int,
        log_signature: str,
    ) -> bool:
        """Fast-path idempotence gate. Any failure => full rebuild."""
        if not writes:
            return False
        if not os.path.isfile(skeleton_path) or not os.path.isfile(metadata_path):
            return False
        try:
            if os.path.getsize(skeleton_path) != slot_count * PALETTE_STRIDE_BYTES:
                return False
            with open(skeleton_path, "rb") as handle:
                if _sha256_hex(handle.read()) != digest:
                    return False
        except OSError:
            return False
        try:
            metadata = JsonUtils.LoadFromFile(metadata_path)
        except Exception:
            return False
        if not isinstance(metadata, dict):
            return False
        if int(metadata.get("AlgorithmVersion", 0) or 0) != HIMI_VG_MAP_ALGORITHM_VERSION:
            return False
        if int(metadata.get("GateVersion", 0) or 0) != HIMI_DEFORM_GATE_VERSION:
            return False
        if str(metadata.get("SkeletonDigest", "") or "") != digest:
            return False
        if int(metadata.get("SlotCount", 0) or 0) != slot_count:
            return False
        if str(metadata.get("SourceLogSignature", "") or "") != str(log_signature or ""):
            return False
        for entry in writes:
            json_path = entry["json_path"]
            try:
                payload = JsonUtils.LoadFromFile(json_path)
            except Exception:
                return False
            if not isinstance(payload, dict):
                return False
            if int(payload.get("VGMapAlgorithmVersion", 0) or 0) != HIMI_VG_MAP_ALGORITHM_VERSION:
                return False
            if not bool(payload.get("HIMIDeformGateSafe")):
                return False
            if str(payload.get("HIMIMergedSkeletonDigest", "") or "") != digest:
                return False
            expected_map = {str(k): int(v) for k, v in (entry["payload"].get("VGMap") or {}).items()}
            if {str(k): int(v) for k, v in (payload.get("VGMap") or {}).items()} != expected_map:
                return False
            cache_path = entry["cache_path"]
            if not os.path.isfile(cache_path):
                return False
            try:
                if os.path.getsize(cache_path) != len(entry["cache_bytes"]):
                    return False
            except OSError:
                return False
        return True

    @classmethod
    def commit(cls, plan: dict) -> dict:
        """Apply a :meth:`prepare` plan to disk as one rollback-capable bundle."""
        if not plan or not plan.get("ok"):
            return {"ok": False, "written": 0, "message": (plan or {}).get("message", "无计划")}
        writes = plan.get("writes") or []
        if not writes:
            return {"ok": False, "written": 0, "message": "没有可写回的组件"}

        import tempfile

        root = plan["workspace_root"]
        staged: list[str] = []
        entries: list[dict] = []
        try:
            os.makedirs(os.path.join(root, "Config"), exist_ok=True)
            for write in writes:
                cache_path = write["cache_path"]
                os.makedirs(os.path.dirname(cache_path), exist_ok=True)
                fd, cache_stage = tempfile.mkstemp(
                    prefix=f".{os.path.basename(cache_path)}.",
                    suffix=".buf",
                    dir=os.path.dirname(cache_path),
                )
                os.close(fd)
                with open(cache_stage, "wb") as handle:
                    handle.write(write["cache_bytes"])
                staged.append(cache_stage)
                entries.append(
                    {
                        "source_path": cache_stage,
                        "dest_path": cache_path,
                        "exact_size": len(write["cache_bytes"]),
                        "min_size": 0,
                    }
                )

                json_path = write["json_path"]
                fd, json_stage = tempfile.mkstemp(
                    prefix=f".{os.path.basename(json_path)}.",
                    suffix=".json",
                    dir=os.path.dirname(json_path),
                )
                os.close(fd)
                JsonUtils.SaveToFile(filepath=json_stage, json_dict=write["payload"])
                staged.append(json_stage)
                entries.append(
                    {
                        "source_path": json_stage,
                        "dest_path": json_path,
                        "min_size": 2,
                        "float_aligned": False,
                    }
                )

            skeleton_bytes = plan.get("skeleton_bytes") or b""
            fd, skeleton_stage = tempfile.mkstemp(
                prefix=".HIMIMergedSkeleton.",
                suffix=".buf",
                dir=os.path.join(root, "Config"),
            )
            os.close(fd)
            with open(skeleton_stage, "wb") as handle:
                handle.write(skeleton_bytes)
            staged.append(skeleton_stage)
            entries.append(
                {
                    "source_path": skeleton_stage,
                    "dest_path": plan["skeleton_path"],
                    "exact_size": len(skeleton_bytes),
                    "min_size": 0,
                }
            )

            fd, metadata_stage = tempfile.mkstemp(
                prefix=".HIMIMergedSkeleton.",
                suffix=".json",
                dir=os.path.join(root, "Config"),
            )
            os.close(fd)
            JsonUtils.SaveToFile(filepath=metadata_stage, json_dict=plan["metadata"])
            staged.append(metadata_stage)
            entries.append(
                {
                    "source_path": metadata_stage,
                    "dest_path": plan["metadata_path"],
                    "min_size": 2,
                    "float_aligned": False,
                }
            )

            atomic_publish_bundle(entries)
        finally:
            for path in staged:
                if path and os.path.isfile(path):
                    try:
                        os.remove(path)
                    except OSError:
                        pass

        return {
            "ok": True,
            "written": len(writes),
            "slot_count": int(plan.get("slot_count", 0)),
            "skeleton_path": plan["skeleton_path"],
            "metadata_path": plan["metadata_path"],
            "message": f"已写回 {len(writes)} 个组件的合并骨架数据与角色级骨架",
        }

    @classmethod
    def ensure_skeleton_data(
        cls,
        workspace_root: str,
        unique_str_list=None,
        force: bool = False,
    ) -> tuple[bool, str]:
        """Import-side entry point: build + write, idempotently.

        Returns ``(ok, message)``.  Never raises for expected conditions -- the
        caller keeps importing either way (failures must not block an import).

        Three outcomes:
        * normal path -- recomputed from the dump (and skipped when the cache is
          already intact);
        * **cache-only** -- the dump is gone but the workspace cache is complete,
          so nothing is recomputed and nothing is written (same capability the
          export side already relies on);
        * explicit failure -- dump gone *and* cache incomplete/damaged (or a
          forced rebuild was requested), with the broken pieces named and zero
          files written.
        """
        try:
            plan = cls.prepare(workspace_root, unique_str_list, force=force)
        except Exception as exc:  # noqa: BLE001 - 数据层绝不阻断导入
            return False, f"HIMI 骨骼合并预生成异常: {exc}"
        if not plan.get("ok"):
            return False, str(plan.get("message", "预生成失败"))
        if plan.get("cache_only"):
            # 快路径：dump 缺失 + 缓存完好 ⇒ 不重算、不写盘。
            return True, str(plan.get("message", "cache-only"))
        if plan.get("cache_intact"):
            return True, (
                f"{plan['message']}；缓存完整，本次跳过重算（{plan['slot_count']} 槽）"
            )
        try:
            result = cls.commit(plan)
        except Exception as exc:  # noqa: BLE001
            return False, f"HIMI 骨骼合并写回失败: {exc}"
        if not result.get("ok"):
            return False, str(result.get("message", "写回失败"))
        return True, f"{plan['message']}；{result['message']}"


# --------------------------------------------------------------------------- #
# 维护者注记（t47/t48，2026-09-24）：``OriginalVertexCount`` 的语义
# --------------------------------------------------------------------------- #
# 上面 metadata 组装写下的 ``OriginalVertexCount``（`prepare()` 的写回，见
# ``payload["OriginalVertexCount"]``）是**该分片/LOD 自己**的顶点数 —— 实测同族每个
# 成员的该值**完全相同**（它们是分片/LOD 变体），所以：
#
# * **不可**把它按家族求和当作 deform 重放的 ``draw``：每家族只跑 1 个 deform pass，
#   求和会放大 N 倍（用户产物实测 draw = 99475 vs 模组自身 31066 行，最高 1263×）⇒
#   越界读 Blend（垃圾骨骼索引）+ 写爆角色级 SO 池 ⇒ 实机「无骨骼约束 + 错位 +
#   上下颠倒 + 部件随意旋转」（t47 实证）。
# * deform 重放 ``draw`` 的**权威值 = 模组自己上传的行数**（``drawib_model.draw_number``
#   = 同一产物的 ``override_vertex_count``），由
#   ``common/himi_merged_runtime.py::resolve_deform_draw_count`` 决定；家族 Σ 只作诊断。
#
# 注记放在文件末尾是**刻意的**：计划书用行号锚点引用本文件的多个符号，插在中间会让
# 那些锚点整体漂移（t43 已回填过一次；见 §9.6 的生产锚点护栏）。
