"""
ZZMI（绝区零）骨骼合并支持模块

数据流（实测结论，详见项目根目录 ZZMI骨骼合并计划书.md）：
- ZZZ 渲染为三段式管线：CS 形变 pass -> pointlist 蒙皮变形 pass（下称 deform pass）
  -> 渲染 draw。骨骼 palette 只存在于 **deform pass 的 vs-t0**（每部件一份，
  12 floats / 4x3 矩阵 = 48 字节每骨骼）；渲染 draw 只读 SO 蒙皮输出，无骨骼数据。
- 工作空间子网格 json 自带 join 线索：
  `CategoryHash.Position` == deform pass 的 vb0 资源 hash（路径 A）；
  `VertexLimitVB` == deform pass 的 SO 输出资源 hash（路径 B）；
  兜底用 `LOD0/ComponentName_DrawCallIndexList.json` 的渲染 draw（先按目标 IB 门控）
  -> vb0 -> SO（路径 C）。

去重规则（用户拍板 + 实测修正）：
- **同一部件内部绝不去重合并**（同部件索引为提取端权威分配，实测内部零重复）；
- **仅跨部件去重，严格 bitwise（字节级）判等，禁用浮点容差**
  （同一骨骼同帧上传到各 palette 是逐位拷贝；近似但非位等的矩阵是同骨骼
  异帧姿态——如脸部 morph 部件的 deform pass 更晚、动画已推进——容差会误并）。
- **单骨骼刚性部件（单权重物体）追加加权质心门控**（2026-08-24 用户拍板）：
  抓帧瞬间不同锚点骨可能逐位重合（头顶件/前额件/后脑发饰实测同矩阵），bitwise
  会误并成一根，动画分叉时两物体错位联动；刚性部件命中对要求加权质心距离
  < rigid_centroid_tolerance（默认 0.05）才合并，否则各占各槽。刚性部件误拆
  零代价（各自 attach 写同一矩阵，运行时内容恒等），只拆不并是安全方向。
  双方均为多骨骼部件时不加门控（多根同时位等不可能是巧合；真共享骨骼的
  驱动区域质心可相距甚远，实测达 0.25，加门控会误拆真共享）。
- 合并骨架布局：每部件 palette 按 VGOffset 连续摆放（VGOffset = 固定排序下
  前序部件 vg_count 累加），去重只决定顶点引用的全局 id 指向哪个 canonical 槽位，
  重复槽位为死槽（内容恒同，无害）。

产出：
- 每个子网格 json 写回 `VGMap` / `VGOffset` / `VGCount`（缓存，幂等；force 可重建）+
  `SkeletonGroup`（骨架分组号：按渲染 cb1 对象变换分组，palette 与 cb1 逐物体
  1:1 配对，跨组绝不共享骨架）；
- 骨骼 id 为**全局编号**（组基址拼接组内槽位），`VGMap`/`VGOffset` 均为全局口径；
- **无 CB1 校准（2026-08-25 用户拍板）**：运行时每组骨架只写本组骨骼（直拷），
  禁止跨组别骨骼合并——跨组引用会在导出侧大声报警；
- palette buf 复制到 `<子网格>/ModImpRuntime/<bare>-BoneMatrix.buf`（NTEMI/EFMI 缓存模式）。
"""

import hashlib
import math
import os
import re

import numpy

from ..utils.json_utils import JsonUtils
from .efmi_skeleton import EFMIBoneMapBuilder, EFMISkeletonMergeHelper
from .zzmi_channel import (
    ZZMI_CHANNEL_PLAN_VERSION,
    bone_identity_digest,
    channel_plan_digest as channel_plan_digest_value,
    same_frame_matrix_digest,
    select_channel_plan,
)

# 骨骼身份摘要口径版本：摘要输入（身份判据）变更时递增。
# v1：local_vg_id -> 合并骨架全局槽位。
# F4（复核发现）口径更正：这是 `{本地下标: 全局槽位}` 的规范化哈希 —— 槽位由
# **当帧 palette 矩阵的 bitwise 去重**派生，**不是**骨名/编号身份、也不是帧
# 不变量；`BoneIdentityBasis = "local_slot_map"` 自述这一点。真正的骨名身份
# （`bone_ids`）尚未接入生产链路 ⇒ 通道判定当前全部使用该弱代理。
ZZMI_BONE_IDENTITY_VERSION = 1

# 每骨骼矩阵的 float 数（4x3 = 48 字节，已实测确认）
_BONE_MATRIX_FLOATS = 12

# ZZMI 骨骼合并缓存算法版本：快路径幂等判定必须与本版本一致；
# 策略变更时递增，旧缓存会自动整批重建（同 EFMI 的 VGMapAlgorithmVersion 机制）。
# v2：路径 C 与 CB1 分组均加入目标 DrawIB 门控，旧缓存可能已串入相似模型，
# 必须整批重建以清除污染。
# v3：拒绝同一 DrawIB 对应多个对象 CB1 实例的歧义缓存；这类 IB 可能同时被
# 多个相似模型绘制，继续共用一套全局骨架会把修改写入其它实例。
# v4：对象变换按绑定窗口（first_constant）解析。旧实现无视窗口读 float 0，把
# 多对象共享 cb1 数组里**别的对象**的矩阵当分组键，导致同对象空间的部件被拆进
# 不同 SkeletonGroup/合集（FrameAnalysis-2026-09-16-014450 实证）；必须整批重建。
# v5（t75）：缓存新增「共享骨图 + 通道骨」判定记录（`ChannelPlan*`：逐部件的
# 通道全局槽位、本地下标、共享件数、跨部件顶点权重合计、理由与诊断），且
# VGMap 的**末位浮点**现在也参与通道判定 ⇒ 旧缓存没有这套可复核指纹，
# 必须整批重建（无该字段的 json 会被 `_zzmi_cache_intact` 拒绝）。
_ZZMI_VG_MAP_ALGORITHM_VERSION = 5
# 导出侧也需要知道当前缓存口径，不能只依赖导入阶段的幂等门控。
ZZMI_VG_MAP_ALGORITHM_VERSION = _ZZMI_VG_MAP_ALGORITHM_VERSION
# 缓存里的 VGMap 是哪个编号域：跨组骨骼池槽位，还是历史分组编号。
# `_zzmi_cache_intact` 原本只看「有没有 VGMap」⇒ 历史编号的缓存会被当成合并成果
# 幂等跳过；导入侧按跨组消费，拿到的却是各部件自己的局部编号 —— 现象就是「每个
# 部件仍各在各的局部空间」。开跨组合并时宁可重建，也不能拿错口径的 VGMap 顶上。
_ZZMI_MERGE_MODE_CROSS = "cross_group"
_ZZMI_MERGE_MODE_GROUP = "group"


def fusion_lod(unique_str: str) -> str:
    """部件身份所属的 LOD 名（``LOD0.95de0d39-...`` → ``LOD0``）；无前缀按 LOD0。"""
    value = str(unique_str or "").strip()
    head, sep, _ = value.partition(".")
    if sep and head.upper().startswith("LOD") and head[3:].isdigit():
        return head
    return "LOD0"


class ZZMILogParser:
    """解析 ZZZ FrameAnalysis/log.txt，提供 deform pass 与资源绑定查询。

    与 EFMILogParser 的差异：ZZZ 需要 Draw(VertexCount)（pointlist deform pass）、
    SOSetTargets（蒙皮输出）、IASetVertexBuffers/IASetIndexBuffer（逐槽资源 hash），
    不需要 instance config 窗口反查。
    """

    _DRAW_PREFIX_RE = re.compile(r"^(\d{6}) (.*)$")
    _DUMP_RE = re.compile(r"^3DMigoto Dumping Buffer (.+) -> (.+)$")
    _RESOURCE_LINE_RE = re.compile(
        r"^(\d+): (?:view=0x[0-9A-Fa-f]+ )?resource=0x[0-9A-Fa-f]+ hash=([0-9a-f]{8})"
    )
    _DRAW_RE = re.compile(r"^Draw\(VertexCount:(\d+), StartVertexLocation:(\d+)\)$")
    _DRAW_INDEXED_RE = re.compile(
        r"^DrawIndexedInstanced\(IndexCountPerInstance:(\d+), InstanceCount:(\d+), "
        r"StartIndexLocation:(\d+), BaseVertexLocation:(\d+), StartInstanceLocation:(\d+)\)$"
    )
    _DRAW_INDEXED_SINGLE_RE = re.compile(
        r"^DrawIndexed\(IndexCount:(\d+), StartIndexLocation:(\d+), "
        r"BaseVertexLocation:(\d+)\)$"
    )
    _IA_VB_RE = re.compile(r"^IASetVertexBuffers\(StartSlot:(\d+), NumBuffers:(\d+),")
    # 常量缓冲绑定行（对象变换 CB1 靠 first_constant 窗口定位）
    _CB_BIND_RE = re.compile(
        r"^(\d+): resource=0x[0-9A-Fa-f]+ hash=([0-9a-f]{8}) "
        r"first_constant=(\d+) num_constants=(\d+)$"
    )
    _IA_IB_RE = re.compile(r"^IASetIndexBuffer\(.*\) hash=([0-9a-f]{8})$")
    _SO_RE = re.compile(r"^SOSetTargets\(NumBuffers:(\d+),")
    _SRV_RE = re.compile(r"^(V|P|C|G|H|D)SSetShaderResources\(StartSlot:(\d+), NumViews:(\d+),")
    _VS_SHADER_RE = re.compile(r"^VSSetShader\(.*\) hash=([0-9a-f]+)$")
    # vs-cb1 dump 行（渲染 draw 的对象变换 CB；dump 文件名自带 draw 索引与 hash，
    # 比绑定调用可靠——绑定是持久状态、多数 draw 块里不重发）
    _CB1_DUMP_RE = re.compile(r"^(\d{6})-vs-cb1=([0-9a-f]{8})-.*\.buf$")

    def __init__(self, log_path: str):
        self.log_path = log_path
        self.base_dir = os.path.dirname(os.path.abspath(log_path))
        # draw_index -> {"vb": {slot: hash}, "ib": hash, "so": {slot: hash},
        #                "vs_t0": hash, "vs_shader": hash,
        #                "vertex_count": int|None, "draw_indexed": dict|None}
        self.draws: dict[str, dict] = {}
        # 渲染 draw_index -> vs-cb1 dump 的 (逻辑文件名, 记录路径)。
        # 记录路径在 dump 被搬走后会失效，因此始终保存逻辑名并延迟解析
        # （get_render_cb1_path 按候选路径逐一回退）。
        self.render_cb1_dumps: dict[str, tuple[str, str]] = {}
        # 常量缓冲绑定窗口：draw_index -> {stage: {slot: {hash, first_constant,
        # num_constants}}}。对象变换 CB1 可能来自多对象共享数组，必须用绑定窗口
        # 才能定位本 draw 的对象矩阵（见 ZZMIBoneMapBuilder.parse_object_transform）。
        self.cb_bindings: dict[str, dict] = {}
        # 逻辑文件名（根目录 dump 文件名）-> deduped 实际路径
        self.dump_map: dict[str, str] = {}
        self._parse()

    def _get_draw(self, draw_index: str) -> dict:
        info = self.draws.get(draw_index)
        if info is None:
            info = {
                "vb": {},
                "ib": "",
                "so": {},
                "vs_t0": "",
                "vs_shader": "",
                "vertex_count": None,
                "draw_indexed": None,
            }
            self.draws[draw_index] = info
        return info

    def _parse(self):
        if not os.path.isfile(self.log_path):
            raise FileNotFoundError(f"FrameAnalysis log 不存在: {self.log_path}")

        # pending 资源消费状态：("vb",) / ("so",) / ("srv", stage) / None
        # 任何带 draw 前缀的行都会重置/重设 pending；无前缀的资源描述行被当前 pending 消费。
        pending = None
        pending_draw = ""
        # IASetIndexBuffer 与其它 IA 状态一样可以跨 DrawIndexed 持久生效。
        # 记录最近一次绑定，供没有重复绑定行的后续 render draw 继承。
        current_ib_hash = ""

        with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue

                match = self._DRAW_PREFIX_RE.match(line)
                if not match:
                    # 资源描述行（无 draw 前缀）
                    if pending is not None:
                        stripped = line.strip()
                        desc = self._RESOURCE_LINE_RE.match(stripped)
                        if desc:
                            slot = int(desc.group(1))
                            res_hash = desc.group(2)
                            info = self._get_draw(pending_draw)
                            kind = pending[0]
                            if kind == "vb":
                                info["vb"][slot] = res_hash
                            elif kind == "so":
                                info["so"][slot] = res_hash
                            elif kind == "srv" and pending[1] == "V" and slot == 0:
                                info["vs_t0"] = res_hash
                            elif kind == "cb":
                                desc_cb = self._CB_BIND_RE.match(stripped)
                                if desc_cb:
                                    stage_slot = self.cb_bindings.setdefault(
                                        pending_draw, {}
                                    ).setdefault(pending[1], {})
                                    stage_slot[int(desc_cb.group(1))] = {
                                        "hash": desc_cb.group(2),
                                        "first_constant": int(desc_cb.group(3)),
                                        "num_constants": int(desc_cb.group(4)),
                                    }
                        # 注意：不在这里清空 pending——多槽资源描述是连续多行，
                        # 由下一行（无论有无前缀）继续消费或重设。
                    continue

                draw_index, payload = match.group(1), match.group(2)
                pending = None
                pending_draw = draw_index

                # 顶点缓冲绑定（内容行跟随，多行）
                vb_match = self._IA_VB_RE.match(payload)
                if vb_match:
                    pending = ("vb",)
                    continue

                # 索引缓冲绑定（同行 hash）
                ib_match = self._IA_IB_RE.match(payload)
                if ib_match:
                    current_ib_hash = ib_match.group(1)
                    self._get_draw(draw_index)["ib"] = current_ib_hash
                    continue

                # Stream-Out 目标绑定（内容行跟随）
                so_match = self._SO_RE.match(payload)
                if so_match:
                    pending = ("so",)
                    continue

                # 着色器资源绑定（内容行跟随；只关心 VS slot 0）
                srv_match = self._SRV_RE.match(payload)
                if srv_match:
                    pending = ("srv", srv_match.group(1))
                    continue

                # 常量缓冲绑定（对象变换 CB1 的窗口起点，单位 = 16 字节常量）
                cb_match = re.match(
                    r"^(V|P|C|G|H|D)SSetConstantBuffers1\(StartSlot:(\d+),", payload
                )
                if cb_match:
                    pending = ("cb", cb_match.group(1))
                    continue

                # VS hash
                vs_match = self._VS_SHADER_RE.match(payload)
                if vs_match:
                    self._get_draw(draw_index)["vs_shader"] = vs_match.group(1)
                    continue

                # pointlist 蒙皮变形 draw
                draw_match = self._DRAW_RE.match(payload)
                if draw_match:
                    self._get_draw(draw_index)["vertex_count"] = int(draw_match.group(1))
                    continue

                # 渲染 draw
                indexed_match = self._DRAW_INDEXED_RE.match(payload)
                if indexed_match:
                    info = self._get_draw(draw_index)
                    if not info.get("ib") and current_ib_hash:
                        info["ib"] = current_ib_hash
                    info["draw_indexed"] = {
                        "index_count": int(indexed_match.group(1)),
                        "start_index": int(indexed_match.group(3)),
                        "base_vertex": int(indexed_match.group(4)),
                    }
                    continue

                # 部分 ZZZ/3Dmigoto 构建使用非 Instanced 形式；IB/VB 绑定仍然
                # 是同一套渲染身份信息，不能因为 draw 调用形式不同就丢掉它。
                indexed_single_match = self._DRAW_INDEXED_SINGLE_RE.match(payload)
                if indexed_single_match:
                    info = self._get_draw(draw_index)
                    if not info.get("ib") and current_ib_hash:
                        info["ib"] = current_ib_hash
                    info["draw_indexed"] = {
                        "index_count": int(indexed_single_match.group(1)),
                        "start_index": int(indexed_single_match.group(2)),
                        "base_vertex": int(indexed_single_match.group(3)),
                    }
                    continue

                # 资源 dump 映射
                dump_match = self._DUMP_RE.match(payload)
                if dump_match:
                    src_name = os.path.basename(dump_match.group(1))
                    dst_path = dump_match.group(2)
                    if src_name not in self.dump_map:
                        self.dump_map[src_name] = dst_path
                    # vs-cb1 dump（渲染 draw 的对象变换 CB）：按 draw 索引记录
                    # (逻辑文件名, 记录路径)。不校验记录路径是否存在——dump 被
                    # 搬走后原绝对路径失效，get_render_cb1_path 按候选路径延迟解析。
                    cb1_match = self._CB1_DUMP_RE.match(src_name)
                    if cb1_match:
                        draw_key = cb1_match.group(1)
                        if draw_key not in self.render_cb1_dumps:
                            self.render_cb1_dumps[draw_key] = (src_name, dst_path)
                    continue

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def get_vb_hash(self, draw_index: str, slot: int) -> str:
        info = self.draws.get(draw_index)
        return info["vb"].get(slot, "") if info else ""

    def get_ib_hash(self, draw_index: str) -> str:
        """返回渲染 draw 绑定的 IB hash（用于路径 C 的 DrawIB 门控）。"""
        info = self.draws.get(draw_index)
        return str(info.get("ib", "") or "").strip().lower() if info else ""

    def get_render_draw_indices_for_ib(self, draw_ib: str) -> list[str]:
        """返回当前 dump 中绑定目标 IB 的 indexed render draw。

        工作空间的 ComponentName_DrawCallIndexList 可能来自另一帧；当它过期时，
        仍应以当前 log 的 IB hash 反查对象 CB1，而不是静默回退到旧实例缓存。
        """
        expected = str(draw_ib or "").strip().lower()
        if not expected:
            return []
        return sorted(
            draw_index
            for draw_index, info in self.draws.items()
            if info.get("draw_indexed") is not None
            and str(info.get("ib", "") or "").strip().lower() == expected
        )

    def _deduped_candidates(self, dst_path: str | None) -> list[str]:
        """生成 deduped 候选路径：log 记录的原路径 + dump 目录 deduped/ 下的同名文件。"""
        candidates = []
        if dst_path:
            candidates.append(dst_path)
            basename = os.path.basename(dst_path)
            if basename:
                candidates.append(os.path.join(self.base_dir, "deduped", basename))
        return candidates

    def get_render_cb1_path(self, draw_index: str) -> str | None:
        """渲染 draw 的 vs-cb1 dump 实际路径（对象变换 CB；延迟解析）。

        log 记录的绝对路径在 FrameAnalysis 被搬走后失效：按候选路径
        （记录路径 -> 当前 dump 目录 deduped/<同名文件>）逐一回退。
        未 dump 或全部候选失效返回 None。
        """
        record = self.render_cb1_dumps.get(draw_index)
        if record is None:
            return None
        _logical_name, dst_path = record
        for candidate in self._deduped_candidates(dst_path):
            if os.path.isfile(candidate):
                return candidate
        return None

    def get_vs_cb_first_constant(self, draw_index: str, slot: int) -> int:
        """返回该渲染 draw 在 VS 常量缓冲 slot 上的绑定窗口起点（单位 16 字节常量）。

        未记录绑定时返回 0（等价于整块从 0 起，与逐部件 512B dump 的语义一致）。
        """
        stage_slots = self.cb_bindings.get(draw_index, {}).get("V", {})
        binding = stage_slots.get(int(slot)) if stage_slots else None
        if not binding:
            return 0
        try:
            value = int(binding.get("first_constant", 0) or 0)
        except (TypeError, ValueError):
            return 0
        return value if value >= 0 else 0

    def get_deform_passes(self) -> dict[str, dict]:
        """识别全部 deform pass（pointlist Draw + SO 输出 + vs-t0 palette + vb0）。

        返回 draw_index -> {vertex_count, so_hash, palette_hash, vb0_hash, vb2_hash, vs_hash}
        """
        result = {}
        for draw_index, info in self.draws.items():
            vertex_count = info.get("vertex_count")
            so_hash = info.get("so", {}).get(0, "")
            palette_hash = info.get("vs_t0", "")
            vb0_hash = info.get("vb", {}).get(0, "")
            if vertex_count and so_hash and palette_hash and vb0_hash:
                result[draw_index] = {
                    "vertex_count": vertex_count,
                    "so_hash": so_hash,
                    "palette_hash": palette_hash,
                    "vb0_hash": vb0_hash,
                    "vb2_hash": info.get("vb", {}).get(2, ""),
                    "vs_hash": info.get("vs_shader", ""),
                }
        return result

    def get_deduped_path(self, logical_filename: str) -> str | None:
        """按根目录逻辑文件名（如 000002-vs-t0=...buf）查 deduped 实际路径。

        log.txt 里记录的 deduped 绝对路径可能是提取时的路径，FrameAnalysis 被
        搬走后失效：先按 log 记录的路径，失效时用文件名在**当前** dump 目录的
        deduped/ 子目录兜底定位（deduped 文件名是内容 hash，唯一）。
        """
        dst = self.dump_map.get(logical_filename)
        for candidate in self._deduped_candidates(dst):
            if os.path.isfile(candidate):
                return candidate
        # 兜底：按 draw+槽位前缀匹配（逻辑名可能缺 -vs= 尾段）
        prefix = logical_filename.split("=", 1)[0] + "="
        for src, dst2 in self.dump_map.items():
            if src.startswith(prefix):
                for candidate in self._deduped_candidates(dst2):
                    if os.path.isfile(candidate):
                        return candidate
        return None


class ZZMIDeformResolver:
    """把工作空间子网格挂载到正确的 deform pass（三条独立 join 路径）。"""

    def __init__(self, parser: ZZMILogParser):
        self.parser = parser
        self.passes = parser.get_deform_passes()
        # deform 输入 vb0 hash -> (draw_index, pass)
        self.by_vb0: dict[str, tuple[str, dict]] = {}
        # deform SO 输出 hash -> (draw_index, pass)
        self.by_so: dict[str, tuple[str, dict]] = {}
        for draw_index, deform_pass in self.passes.items():
            if deform_pass["vb0_hash"] and deform_pass["vb0_hash"] not in self.by_vb0:
                self.by_vb0[deform_pass["vb0_hash"]] = (draw_index, deform_pass)
            if deform_pass["so_hash"] and deform_pass["so_hash"] not in self.by_so:
                self.by_so[deform_pass["so_hash"]] = (draw_index, deform_pass)

    def resolve(
        self,
        position_hash: str = "",
        vertex_limit_hash: str = "",
        render_draw_indices: list[str] | None = None,
        expected_draw_ib: str = "",
    ) -> tuple[str, dict, str]:
        """返回 (draw_index, deform_pass, 命中路径"A"/"B"/"C")；未命中返回 ("", None, "")。

        - 路径 A：子网格 json CategoryHash.Position == deform vb0 hash（最直接）；
        - 路径 B：子网格 json VertexLimitVB == deform SO 输出 hash；
        - 路径 C：渲染 draw（ComponentName_DrawCallIndexList.json）的 vb0 == SO 输出 hash，
          且 IASetIndexBuffer == 目标 DrawIB。

        路径 C 是兜底路径，映射列表可能来自不同帧或包含相似模型的 draw；只按
        SO/vb0 hash 会把另一个模型的 deform pass 归给当前子网格。生产调用必须传入
        目标 DrawIB，先做 IB 门控再做 SO hash join。
        """
        position_hash = str(position_hash or "").strip().lower()
        if position_hash and position_hash in self.by_vb0:
            draw_index, deform_pass = self.by_vb0[position_hash]
            return draw_index, deform_pass, "A"

        vertex_limit_hash = str(vertex_limit_hash or "").strip().lower()
        if vertex_limit_hash and vertex_limit_hash in self.by_so:
            draw_index, deform_pass = self.by_so[vertex_limit_hash]
            return draw_index, deform_pass, "B"

        expected_draw_ib = str(expected_draw_ib or "").strip().lower()
        if not expected_draw_ib:
            # 路径 C 没有其它能确认模型身份的可靠键；宁可不生成，也不能
            # 在相似模型之间按 SO/vb0 hash 猜一个 deform pass。
            return "", None, ""
        for render_draw in render_draw_indices or []:
            if self.parser.get_ib_hash(render_draw) != expected_draw_ib:
                continue
            vb0_hash = self.parser.get_vb_hash(render_draw, 0)
            if vb0_hash and vb0_hash in self.by_so:
                draw_index, deform_pass = self.by_so[vb0_hash]
                return draw_index, deform_pass, "C"

        return "", None, ""


def assign_skeleton_groups(part_transforms: dict[str, tuple[float, ...] | None]) -> dict[str, int]:
    """按对象变换（渲染 cb1）把 DrawIB 部件分组：返回 draw_ib -> 骨架组索引。

    规则（用户拍板，2026-08-24）：palette 与 cb1 逐物体 1:1 配对——
    共享同一对象空间（变换逐位相同）的部件进同一组、共用一套合并骨架；
    跨组绝不共享（不同空间的矩阵混用会被渲染侧 cb1 摆错位置）。
    无变换数据的部件独立成组（安全方向）。
    组索引按组内最小 draw_ib 排序分配（确定性，导入/导出两侧一致）。
    """
    key_groups: dict[tuple, list[str]] = {}
    for draw_ib, transform in part_transforms.items():
        key = transform if transform is not None else ("__solo__", draw_ib)
        key_groups.setdefault(key, []).append(draw_ib)
    ordered_keys = sorted(key_groups.keys(), key=lambda k: min(key_groups[k]))
    result: dict[str, int] = {}
    for group_index, key in enumerate(ordered_keys):
        for draw_ib in key_groups[key]:
            result[draw_ib] = group_index
    return result


def _strict_int_or_default(value, default: int = 0) -> int:
    """严格整数解析（bool 与非整数浮点都拒绝），失败抛异常由调用方兜底。

    与 `_zzmi_cache_intact` 内联的 `_strict_int` 同一口径；这里提到模块级是
    为了让「v4 缓存就地升级」的准入检查（`_zzmi_cache_usable_for_migration`）
    与它共用同一套严格性，避免两个函数对"合法整数"的定义漂移。
    """
    if isinstance(value, bool):
        raise TypeError("bool 不是缓存整数")
    if isinstance(value, float) and not value.is_integer():
        raise ValueError("非整数浮点值")
    return int(value)


def _serialize_channel_record(record: dict | None) -> dict | None:
    """把通道计划记录规范化为**纯 JSON 原生类型**（numpy 标量会写出非标准 json）。

    缺失记录返回 None——调用方（导出侧）必须把「缓存缺失」当**显式诊断**处理，
    绝不静默退回出现次判据。
    """
    if not isinstance(record, dict):
        return None
    serialized: dict = {}
    for key in (
        "channel_slot",
        "channel_local",
        "channel_shared_components",
        "channel_shared_weight",
    ):
        value = record.get(key)
        serialized[key] = None if value is None else int(value)
    serialized["channel_static_root"] = bool(record.get("channel_static_root"))
    serialized["channel_reason"] = str(record.get("channel_reason") or "")
    serialized["channel_diagnostic"] = str(record.get("channel_diagnostic") or "")
    # F5（复核发现）：身份口径必须**随记录一起落盘**，否则导出侧与人工复核都
    # 看不到「这条判定用的是骨名身份还是槽号弱代理」。此前这两个字段被静默丢掉
    # （产物 0 次出现），任何只读缓存的消费者都会把槽号误当骨名身份。
    serialized["channel_identity_basis"] = str(
        record.get("channel_identity_basis") or ""
    )
    serialized["channel_identity_token"] = str(
        record.get("channel_identity_token") or ""
    )
    serialized["vg_count"] = int(record.get("vg_count") or 0)
    serialized["vg_offset"] = int(record.get("vg_offset") or 0)
    return serialized


class ZZMIBoneMapBuilder:
    """palette 解析与跨部件 bitwise 去重（同部件绝不去重）。"""
    @staticmethod
    def load_palette(palette_path: str) -> numpy.ndarray:
        """读取 palette buf 为 (N, 12) float32 矩阵数组。"""
        data = numpy.fromfile(palette_path, dtype=numpy.float32)
        if len(data) == 0 or len(data) % _BONE_MATRIX_FLOATS != 0:
            raise ValueError(
                f"palette 大小不能按 {_BONE_MATRIX_FLOATS} floats 切分: {palette_path} "
                f"({len(data)} floats)"
            )
        return data.reshape(-1, _BONE_MATRIX_FLOATS)

    @staticmethod
    def parse_object_transform(
        cb1_path: str,
        first_constant: int = 0,
    ) -> tuple[float, ...] | None:
        """从渲染 draw 的 vs-cb1 dump 解析对象→世界矩阵，返回 16 floats 元组（分组键）。

        实测布局（FrameAnalysis-2026-08-19-122152 逆向）：逐部件 cb1 块的前 4 个
        float4 = 3x4 对象变换（rows 0-2 旋转行 w=0、row 3 平移 w=1）。
        palette 矩阵把顶点蒙皮到该对象空间，渲染 VS 再用本矩阵摆到世界——
        两者逐物体 1:1 配对，共享同一份变换的部件才共享同一对象空间。

        ``first_constant`` 是该 draw 绑定 cb1 时的窗口起点（log 的
        ``VSSetConstantBuffers1(first_constant=N)``，单位 = 16 字节常量；
        实测 FrameAnalysis-2026-09-16-014450：同一 4096B 资源被 draw 64/65/66
        按 first_constant=0/32/64 逐 512B 一窗切给三个不同对象）。**dump 是整块
        资源**，本 draw 的对象变换只在该窗口内——忽略窗口就会读到别的对象的矩阵
        （实测 draw 66 在窗口 64 处与 draw 69 逐位同空间，而 float 0 处是 draw 64
        的对象），把同空间部件误拆成不同 SkeletonGroup。

        窗口取不到（越界/缺文件）返回 None（调用方按独立组兜底 = 不共享，安全方向）；
        窗口内形态不合法（w 列/行范数 sanity 不过）同样返回 None。
        """
        try:
            data = numpy.fromfile(cb1_path, dtype=numpy.float32)
        except (OSError, ValueError):
            return None
        try:
            offset = int(first_constant) * 4
        except (TypeError, ValueError):
            return None
        if offset < 0 or offset + 16 > len(data):
            # 窗口越界：dump 缺该窗口（旧版只 dump 了逐部件 512B 块）或绑定值异常。
            # 不得回退到 float 0——那会静默读成其它对象的矩阵。
            return None
        m = data[offset:offset + 16].reshape(4, 4)
        # w 列形态：旋转行 w=0、平移行 w=1
        if abs(float(m[3, 3]) - 1.0) > 1e-3:
            return None
        if float(numpy.abs(m[:3, 3]).max()) > 1e-3:
            return None
        # 旋转行 sanity（允许缩放/镜像，排除纯参数块）
        row_norms = numpy.linalg.norm(m[:3, :3].astype(numpy.float64), axis=1)
        if numpy.any((row_norms < 0.05) | (row_norms > 20.0)):
            return None
        return tuple(float(x) for x in data[offset:offset + 16])

    @staticmethod
    def build_vg_maps(
        part_palettes: dict[str, numpy.ndarray],
        part_signatures: dict[str, dict] | None = None,
        rigid_centroid_tolerance: float = 0.05,
        dedup_excluded: set[str] | None = None,
    ) -> tuple[dict[str, dict], dict[str, int], int]:
        """跨部件按矩阵 bitwise 判等去重（刚性部件加权质心门控），构建 vg_map / vg_offset / 总槽位。

        参数: part_key -> (N, 12) palette 矩阵数组（part_key 为去重单元，同 DrawIB 的
              多个子网格共享同一 palette，只参与一次）；
              part_signatures: part_key -> {local: 驱动签名}（EFMIBoneMapBuilder.
              compute_driven_signatures 产出，仅用其 centroid 字段）；不传则退化为
              纯 bitwise 旧行为；
              rigid_centroid_tolerance: 刚性部件命中对的质心确认阈值（默认 0.05 米）；
              dedup_excluded: 部件级「排除去重」集合（part_key 集合，对齐 EFMI
              efmi_skeleton.build_vg_maps）。集合内部件的每根骨骼**不参与任何去重合并**：
              其 VGMap 恒为恒等映射（local -> offset + local，独占自己声明段内的
              **组内**槽位，外部拼接组基址后即全局 vg_offset + local），且不注册进
              owners——其它部件也查不到其槽位、无法并入。排除不改变其它部件的
              offset 布局。对应子网格 json 标记键：``VGMapDedupExcluded=True``。
        返回:
            vg_maps: part_key -> {local_vg_id(int): global_vg_id(int)}
            vg_offsets: part_key -> 该部件在合并骨架中的起始槽位
            total_slots: 合并骨架总槽位（Σ vg_count，含死槽）

        规则（用户拍板 + 2026-08-24 增补刚性门控）：
        - 同部件内部绝不去重（同 key 只来自更靠前的其它部件才合并）；
        - 跨部件严格 bitwise（tobytes）判等，无浮点容差；
        - **刚性部件质心门控**：命中对任一方是单骨骼刚性部件（palette 仅 1 根，
          即"单权重物体"——其唯一骨骼就是整个物体的锚点，质心 = 物体位置指纹）时，
          追加加权质心距离确认，距离 >= 阈值则拆开各占各槽。
          背景：抓帧瞬间不同锚点骨可能逐位重合（头顶件/前额件/后脑发饰同矩阵实测案例），
          bitwise 会把它们误并成一根；刚性部件误拆零代价（各自 attach 各自写同一矩阵，
          运行时内容恒等），误并则游戏内动画分叉时两物体错位联动——只拆不并是安全方向。
          门控触发时任一方缺签名 -> 保守拆开。
        - 双方都是多骨骼部件时不加质心门控：整块 palette 多根骨骼同时逐位相同不可能是
          巧合；且真共享骨骼跨部件驱动区域的质心可相距甚远（实测 b20f90ea ↔ a23aa8a3
          达 0.25），多骨骼加门控会误拆真共享。
        - canonical 槽位 = 按 part_key 排序后首次（通过门控的）出现处的槽位（确定性）。
        """
        # bone_key -> 该矩阵字节序列的既有归属者列表：
        # {"part": 部件, "slot": 全局槽位, "rigid": 是否单骨骼刚性部件, "centroid": 加权质心|None}
        owners: dict[bytes, list[dict]] = {}
        vg_maps: dict[str, dict] = {}
        vg_offsets: dict[str, int] = {}
        offset = 0
        excluded_set: set[str] = set(dedup_excluded or ())

        def _centroid_of(part_key: str, local_id: int):
            if part_signatures is None:
                return None
            sig = (part_signatures.get(part_key) or {}).get(local_id)
            return None if sig is None else sig["centroid"]

        def _gate_ok(hitter_rigid: bool, hitter_centroid, owner: dict) -> bool:
            """刚性门控：命中对任一方为刚性部件时要求质心贴合，否则纯 bitwise 放行。"""
            if not (hitter_rigid or owner["rigid"]):
                return True
            if part_signatures is None:
                return True
            if hitter_centroid is None or owner["centroid"] is None:
                return False
            dist = float(numpy.linalg.norm(
                hitter_centroid.astype(numpy.float64) - owner["centroid"].astype(numpy.float64)
            ))
            return dist < rigid_centroid_tolerance

        for part_key in sorted(part_palettes.keys()):
            palette = part_palettes[part_key]
            part_rigid = len(palette) == 1
            vg_offsets[part_key] = offset
            vg_map = {}
            if part_key in excluded_set:
                # 排除去重（对齐 EFMI efmi_skeleton.py:2377-2398）：被排除部件的
                # 每根骨骼恒等映射 local -> offset + local（组内 offset 语义，
                # 外部拼组基址后即全局 vg_offset + local），独占自己声明段；
                # 不注册进 owners —— 其它部件查不到其槽位，无法把骨骼并入。
                for local_id in range(len(palette)):
                    vg_map[local_id] = offset + local_id
                vg_maps[part_key] = vg_map
                offset += len(palette)
                continue
            for local_id in range(len(palette)):
                bone_key = palette[local_id].tobytes()
                hitter_centroid = _centroid_of(part_key, local_id)
                target_slot = None
                for owner in owners.get(bone_key, ()):
                    if owner["part"] == part_key:
                        continue
                    if _gate_ok(part_rigid, hitter_centroid, owner):
                        # 跨部件命中（且通过刚性门控）：合并到归属者槽位
                        target_slot = owner["slot"]
                        break
                if target_slot is not None:
                    vg_map[local_id] = target_slot
                else:
                    # 新骨骼（或同部件重复/门控拆开的重合骨骼，不合并）：占本部件自己的槽位
                    slot = offset + local_id
                    vg_map[local_id] = slot
                    owners.setdefault(bone_key, []).append({
                        "part": part_key,
                        "slot": slot,
                        "rigid": part_rigid,
                        "centroid": hitter_centroid,
                    })
            vg_maps[part_key] = vg_map
            offset += len(palette)

        return vg_maps, vg_offsets, offset


class ZZMISkeletonMergeHelper:
    """ZZMI 骨骼合并总流程：定位 FrameAnalysis -> 解析 log -> 反查 deform pass -> 去重 -> 写回工作空间。"""

    # 本次 ensure_skeleton_data 调用里「旧版但可升级」的 DrawIB 集合（阻断修复）。
    # 每个 DrawIB 的**全部**成员都要一起升级：通道计划是共享骨连通分量的组级
    # 性质，逐成员单独算会退化（`select_channel_plan` 的单件分量）。
    _zzmi_legacy_upgrade_draw_ibs: set[str] = set()

    # 骨骼池方案（参考实现 loyaltools 路线）：导入期算好，导出期只读。
    # None = 本次没算（开关关 / 锚点链算不出来）⇒ 导出侧走历史分组编号。
    cross_group_profile: dict | None = None

    @staticmethod
    def _configured_frame_analysis_paths(workspace_root: str) -> list[str]:
        """读取 ZZMI 工作区显式记录的 FrameAnalysis 路径。"""
        paths: list[str] = []
        config_path = os.path.join(workspace_root, "Config", "FrameAnalysisPath.json")
        if os.path.isfile(config_path):
            try:
                payload = JsonUtils.LoadFromFile(config_path)
                path = str(payload.get("frameAnalysisFolderPath", "") or "").strip()
                if path:
                    paths.append(path)
            except Exception:
                pass

        tabs_dir = os.path.join(workspace_root, "Config", "Tabs")
        if os.path.isdir(tabs_dir):
            for tab_file in sorted(os.listdir(tabs_dir)):
                if not tab_file.startswith("ws-tab-") or not tab_file.endswith(".json"):
                    continue
                try:
                    payload = JsonUtils.LoadFromFile(os.path.join(tabs_dir, tab_file))
                    path = str(payload.get("frameAnalysisFolderPath", "") or "").strip()
                    if path:
                        paths.append(path)
                except Exception:
                    continue
        return paths

    @classmethod
    def resolve_frame_analysis_dir(cls, workspace_root: str) -> str:
        """定位 ZZMI 的 FrameAnalysis；显式失效路径不自动偷换成另一帧。"""
        configured_paths = cls._configured_frame_analysis_paths(workspace_root)
        if configured_paths and not any(os.path.isdir(path) for path in configured_paths):
            # 工作区已经记录过提取源，但该源被用户删除/搬走：优先使用导入时
            # 搬进 ModImpRuntime 的 palette/ObjectCB1 缓存，不能从当前游戏目录
            # 随便挑一个最新 FrameAnalysis，尤其不能把相似模型的 CB1 混进来。
            return ""
        return EFMISkeletonMergeHelper.resolve_frame_analysis_dir(workspace_root)
    _resolve_submesh_json_path = staticmethod(
        EFMISkeletonMergeHelper._resolve_submesh_json_path
    )
    _load_drawcall_index_list = staticmethod(
        EFMISkeletonMergeHelper.load_drawcall_index_list
    )
    _parse_blend_element_info = staticmethod(
        EFMISkeletonMergeHelper.parse_blend_element_info
    )

    @staticmethod
    def _merge_driven_signatures(target: dict, incoming: dict) -> dict:
        """合并同 DrawIB 拆分子网格的局部骨骼驱动签名。"""
        merged = dict(target or {})
        for local_id, new_sig in (incoming or {}).items():
            local_id = int(local_id)
            old_sig = merged.get(local_id)
            if old_sig is None:
                merged[local_id] = dict(new_sig)
                continue

            old_points = numpy.asarray(
                old_sig.get("diffusion_points", []), dtype=numpy.float32
            ).reshape(-1, 3)
            new_points = numpy.asarray(
                new_sig.get("diffusion_points", []), dtype=numpy.float32
            ).reshape(-1, 3)
            points = numpy.concatenate((old_points, new_points), axis=0)
            old_weights = numpy.asarray(
                old_sig.get("diffusion_weights", []), dtype=numpy.float32
            ).reshape(-1)
            new_weights = numpy.asarray(
                new_sig.get("diffusion_weights", []), dtype=numpy.float32
            ).reshape(-1)
            weights = numpy.concatenate((old_weights, new_weights), axis=0)
            if len(points) > 256:
                sample_index = numpy.linspace(0, len(points) - 1, 256, dtype=numpy.int64)
                points = points[sample_index]
                weights = weights[sample_index]

            old_total = float(old_sig.get("weight_total", 0.0) or 0.0)
            new_total = float(new_sig.get("weight_total", 0.0) or 0.0)
            weight_total = old_total + new_total
            if weight_total > 0:
                centroid = (
                    numpy.asarray(old_sig.get("centroid", (0, 0, 0)), dtype=numpy.float64)
                    * old_total
                    + numpy.asarray(new_sig.get("centroid", (0, 0, 0)), dtype=numpy.float64)
                    * new_total
                ) / weight_total
            elif len(points):
                centroid = numpy.mean(points, axis=0, dtype=numpy.float64)
            else:
                centroid = numpy.zeros(3, dtype=numpy.float64)

            vertex_count = int(old_sig.get("vertex_count", 0) or 0) + int(
                new_sig.get("vertex_count", 0) or 0
            )
            if len(points) and len(weights) == len(points) and float(weights.sum()) > 0:
                sampled_total = float(weights.sum())
                sampled_mean_sq = float(
                    (weights * ((points - centroid) ** 2).sum(axis=1)).sum()
                    / sampled_total
                )
                spread = float(numpy.sqrt(max(sampled_mean_sq, 0.0)))
            else:
                spread = max(
                    float(old_sig.get("spread", 0.0) or 0.0),
                    float(new_sig.get("spread", 0.0) or 0.0),
                )
            merged[local_id] = {
                "centroid": numpy.asarray(centroid, dtype=numpy.float32),
                "bbox_min": numpy.minimum(
                    numpy.asarray(old_sig.get("bbox_min", centroid), dtype=numpy.float32),
                    numpy.asarray(new_sig.get("bbox_min", centroid), dtype=numpy.float32),
                ),
                "bbox_max": numpy.maximum(
                    numpy.asarray(old_sig.get("bbox_max", centroid), dtype=numpy.float32),
                    numpy.asarray(new_sig.get("bbox_max", centroid), dtype=numpy.float32),
                ),
                "vertex_count": vertex_count,
                "spread": spread,
                "weight_total": weight_total,
                "mean_weight": weight_total / max(vertex_count, 1),
                "diffusion_points": points,
                "diffusion_weights": weights,
                "diffusion_radius": EFMIBoneMapBuilder._diffusion_radius(points),
                "diffusion_normals": EFMIBoneMapBuilder._estimate_diffusion_normals(points),
            }
        return merged

    @staticmethod
    def _runtime_cache_path(
        submesh_json: dict,
        json_path: str,
        unique_str: str,
        cb1_file_name: str | None = None,
    ) -> str:
        """解析子网格 json 运行时缓存文件的路径（ModImpRuntime 下）。

        默认解析 BoneMatrixFileName 指向的骨骼缓存路径；cb1_file_name="ObjectCB1"
        时解析对象变换 CB 缓存（<bare>-ObjectCB1.buf，优先 json 的 ObjectCB1FileName）。
        只接受纯文件名（拒绝路径穿越）；文件可能不存在，由调用方 isfile 校验。
        """
        if cb1_file_name:
            file_name = str(submesh_json.get("ObjectCB1FileName", "") or "").strip()
            if not file_name or os.path.basename(file_name) != file_name:
                bare_name = unique_str.split(".", 1)[-1]
                file_name = f"{bare_name}-{cb1_file_name}.buf"
        else:
            file_name = str(submesh_json.get("BoneMatrixFileName", "") or "").strip()
            if not file_name or os.path.basename(file_name) != file_name:
                bare_name = unique_str.split(".", 1)[-1]
                file_name = f"{bare_name}-BoneMatrix.buf"
        submesh_dir = os.path.dirname(os.path.dirname(json_path))
        return os.path.join(submesh_dir, "ModImpRuntime", file_name)

    @staticmethod
    def _parse_first_constant(value) -> int:
        """解析写回 json 的 CB1 窗口起点；非法值按 0 处理（旧版缓存语义）。"""
        if isinstance(value, bool):
            return 0
        try:
            parsed = int(value)
        except (TypeError, ValueError):
            return 0
        return parsed if parsed >= 0 else 0

    # 可以直接升级到当前算法版本的**旧缓存版本**（阻断修复）。
    #
    # 背景（t80 §2.4 实测）：版本从 4 抬到 5 后，任何旧工作区一开就死——
    # 导出侧逐件拒绝 + 合并骨架契约把「全部被拒」升级成整次导出中止，而
    # "重新一键导入"要求工作区登记的 FrameAnalysis 帧还在（用户那里已被删除）。
    # v5 相对 v4 **只新增了可由 VGMap 单独推导的派生字段**（ChannelPlan 系列：
    # channel_slot/channel_local 必须落在既有 VGMap 上；ChannelPlanSlotWeights
    # 能从 palette 重算），不需要 dump，因此 v4 缓存**可以就地升级**。
    MIGRATABLE_VG_MAP_ALGORITHM_VERSIONS = (4,)

    @classmethod
    def _zzmi_cache_usable_for_migration(
        cls, submesh_json: dict, json_path: str, unique_str: str
    ) -> bool:
        """旧版缓存是否满足「仅凭 VGMap 就地升级」的硬条件。

        必须**全部**成立（任何一项缺失都退回整批重建，绝不带着半成品升级）：
        - ``VGMapAlgorithmVersion`` 在 ``MIGRATABLE_VG_MAP_ALGORITHM_VERSIONS``；
        - ``SkeletonGroup/DeformDrawIndex/OriginalVertexCount`` 与 ``VGCount/VGOffset``
          齐备且合法（升级后导出侧守卫仍要读它们）；
        - ``VGMap`` 完整覆盖 ``0..VGCount-1``、槽位非负（channel_local 必须能落在
          这张表上，否则派生出来的通道记录与 VGMap 自相矛盾）；
        - ``BoneMatrixFileName`` 指向的 ModImpRuntime 缓存文件存在且大小达标
          （升级后的导出要按同一本地下标读它）。
        """
        try:
            cache_version = _strict_int_or_default(
                submesh_json.get("VGMapAlgorithmVersion", 0)
            )
        except (TypeError, ValueError):
            return False
        if cache_version not in cls.MIGRATABLE_VG_MAP_ALGORITHM_VERSIONS:
            return False
        try:
            skeleton_group = _strict_int_or_default(submesh_json["SkeletonGroup"])
            deform_draw_index = _strict_int_or_default(
                submesh_json["DeformDrawIndex"]
            )
            original_vertex_count = _strict_int_or_default(
                submesh_json["OriginalVertexCount"]
            )
            vg_count = _strict_int_or_default(submesh_json["VGCount"])
            vg_offset = _strict_int_or_default(submesh_json["VGOffset"])
        except (KeyError, TypeError, ValueError):
            return False
        if (
            skeleton_group < 0
            or skeleton_group > 0xFFFFFFFF
            or deform_draw_index < 0
            or deform_draw_index > 0xFFFFFFFF
            or original_vertex_count < 0
            or original_vertex_count > 0xFFFFFFFF
            or vg_count <= 0
            or vg_count > 0xFFFFFFFF
            or vg_offset < 0
            or vg_offset > 0xFFFFFFFF
            or vg_offset + vg_count > 0x100000000
        ):
            return False
        vg_map = submesh_json.get("VGMap")
        if not isinstance(vg_map, dict) or not vg_map:
            return False
        try:
            mapped = {}
            for key, value in vg_map.items():
                normalized_key = _strict_int_or_default(key)
                if normalized_key in mapped:
                    return False
                mapped[normalized_key] = _strict_int_or_default(value)
        except (TypeError, ValueError):
            return False
        if len(mapped) != vg_count or set(mapped.keys()) != set(range(vg_count)):
            return False
        if any(slot < 0 or slot > 0xFFFFFFFF for slot in mapped.values()):
            return False
        bone_matrix_path = cls._runtime_cache_path(
            submesh_json, json_path, unique_str
        )
        if not os.path.isfile(bone_matrix_path):
            return False
        if not EFMIBoneMapBuilder.cache_file_size_ok(bone_matrix_path, vg_count):
            return False
        return True

    @classmethod
    def _zzmi_upgrade_legacy_vgmap_cache(cls, groups: dict) -> int:
        """旧版（v4）缓存**就地升级**到当前算法版本（阻断修复，t80 §2.4）。

        为什么可以在没有 dump 的情况下升级：v5 相对 v4 的**全部**新增字段都是
        可由既有 ``VGMap`` 单独推导的**派生量**——

        - ``ChannelPlan`` 的 ``channel_slot``/``channel_local`` 必须落在既有 VGMap
          上（``vg_map[channel_local] == channel_slot``，导出侧的自洽门会复核）；
        - ``ChannelPlanSlotWeights`` 从 ModImpRuntime 里的 palette + 既有权重口径重算；
        - 身份摘要来自 ``{本地下标: 全局槽位}``，同样只看 VGMap。

        因此升级**不重跑去重**（不碰全局槽位编号 ⇒ 用户既有配色/几何语义不变），
        只补字段。**绝不静默**：逐组打印升级记录，返回值 = 升级的 json 份数。

        失败的组不写任何字段（保持 v4 原样），由调用方按既有口径处理。
        """
        channel_components = []
        member_json_cache: dict[str, dict] = {}
        for draw_ib, group in groups.items():
            if draw_ib not in cls._zzmi_legacy_upgrade_draw_ibs:
                continue
            for member in group["members"]:
                member_json_path = group["json_paths"].get(member, "")
                member_json = (
                    JsonUtils.LoadFromFile(member_json_path)
                    if member_json_path
                    else {}
                )
                if not isinstance(member_json, dict):
                    continue
                member_json_cache[member] = member_json
                try:
                    vg_map = {
                        int(key): int(value)
                        for key, value in (member_json.get("VGMap") or {}).items()
                    }
                    vg_count = int(member_json.get("VGCount") or 0)
                    vg_offset = int(member_json.get("VGOffset") or 0)
                    skeleton_group = int(member_json.get("SkeletonGroup") or 0)
                except (TypeError, ValueError):
                    continue
                if not vg_map or vg_count <= 0:
                    continue
                channel_components.append(
                    {
                        "draw_ib": str(draw_ib),
                        "vg_map": vg_map,
                        "vg_count": vg_count,
                        "vg_offset": vg_offset,
                        "skeleton_group": skeleton_group,
                        "slot_weights": cls._legacy_slot_weights(
                            member_json, group, vg_map, member
                        ),
                    }
                )
        if not channel_components:
            return 0

        channel_plan = select_channel_plan(channel_components)
        plan_digest = channel_plan_digest_value(channel_plan)
        upgraded = 0
        for draw_ib, group in groups.items():
            if draw_ib not in cls._zzmi_legacy_upgrade_draw_ibs:
                continue
            record = channel_plan.get(str(draw_ib))
            serialized = _serialize_channel_record(record)
            if serialized is None:
                print(
                    f"[ZZMI骨骼合并] 警告 {draw_ib}: 旧版缓存就地升级失败"
                    "（通道计划推不出来），保留 VGMap v4 原样；"
                    "导出侧会按缓存缺失补算"
                )
                continue
            for member in group["members"]:
                member_json_path = group["json_paths"].get(member, "")
                member_json = member_json_cache.get(member) or (
                    JsonUtils.LoadFromFile(member_json_path)
                    if member_json_path
                    else {}
                )
                if not member_json_path or not isinstance(member_json, dict):
                    continue
                try:
                    vg_map = {
                        int(key): int(value)
                        for key, value in (member_json.get("VGMap") or {}).items()
                    }
                except (TypeError, ValueError):
                    continue
                if not vg_map:
                    continue
                member_json["VGMapAlgorithmVersion"] = _ZZMI_VG_MAP_ALGORITHM_VERSION
                member_json["ChannelPlanVersion"] = ZZMI_CHANNEL_PLAN_VERSION
                member_json["ChannelPlan"] = dict(serialized)
                member_json["ChannelPlanDigest"] = plan_digest
                member_json["ChannelPlanSlotWeights"] = {
                    str(slot): int(weight)
                    for slot, weight in sorted(
                        cls._legacy_slot_weights(
                            member_json, group, vg_map, member
                        ).items()
                    )
                }
                member_json["BoneIdentityVersion"] = ZZMI_BONE_IDENTITY_VERSION
                member_json["BoneIdentityDigest"] = bone_identity_digest(vg_map)
                member_json["BoneIdentityBasis"] = "local_slot_map"
                member_json["BoneIdentityBasisNote"] = (
                    "local_slot_map：槽位号由当帧 palette 矩阵 bitwise 去重派生，"
                    "**不是**骨名/编号身份（bone_ids 未接入生产链路）；"
                    "本字段由 v4→v5 就地升级补齐"
                )
                try:
                    JsonUtils.SaveToFile(member_json_path, member_json)
                except OSError as error:
                    print(
                        f"[ZZMI骨骼合并] 警告 {member}: 旧版缓存升级写盘失败"
                        f"（{error}），保留 v4 原样"
                    )
                    continue
                upgraded += 1
            print(
                f"[ZZMI骨骼合并] 旧版缓存就地升级: {draw_ib} → VGMap "
                f"v{_ZZMI_VG_MAP_ALGORITHM_VERSION} + ChannelPlan "
                f"(slot={serialized.get('channel_slot')} "
                f"local={serialized.get('channel_local')} "
                f"reason={serialized.get('channel_reason')}，未重跑去重)"
            )
        return upgraded

    @classmethod
    def _legacy_slot_weights(
        cls,
        member_json: dict,
        group: dict,
        vg_map: dict,
        member: str,
    ) -> dict[int, int]:
        """旧版缓存升级时的「全局槽位 → 跨部件顶点权重合计」。

        v4 缓存里没有 ``ChannelPlanSlotWeights``（那是 v5 新增字段），但
        ``BoneMatrixFileName`` 指向的 palette 还在工作空间里 ⇒ 用与导入期
        **同一口径**（行和 → 本地下标 → 全局槽位）重算，保证升级后的排序键
        与重新导入一致。任何一步失败都返回空表（候选排序退化为按槽位号，
        仍然确定性，只是权重次级键失效——不得因此让升级整体失败）。
        """
        existing = member_json.get("ChannelPlanSlotWeights")
        if isinstance(existing, dict) and existing:
            try:
                return {int(key): int(value) for key, value in existing.items()}
            except (TypeError, ValueError):
                return {}
        palette_path = group.get("palette_path") or ""
        if not palette_path:
            json_path = group.get("json_paths", {}).get(member, "")
            try:
                palette_path = cls._runtime_cache_path(member_json, json_path, member)
            except Exception:
                palette_path = ""
        if not palette_path or not os.path.isfile(palette_path):
            return {}
        try:
            palette = ZZMIBoneMapBuilder.load_palette(palette_path)
        except (OSError, ValueError):
            return {}
        weights: dict[int, int] = {}
        for local, slot in vg_map.items():
            if not (0 <= int(local) < len(palette)):
                continue
            row_sum = float(numpy.abs(palette[int(local)]).sum())
            if not math.isfinite(row_sum) or row_sum <= 0:
                continue
            weights[int(slot)] = weights.get(int(slot), 0) + int(round(row_sum))
        return weights

    @classmethod
    def _zzmi_cache_intact(
        cls,
        submesh_json: dict,
        json_path: str,
        unique_str: str,
        expect_cross: bool = False,
    ) -> bool:
        """ZZMI 缓存快路径完整性校验（schema + 算法版本 + 映射覆盖 + 缓存文件）。

        任何一项缺失都判定缓存不完整，走整批重建——复制骨骼缓存失败或工作空间
        搬迁漏掉 ModImpRuntime 时，绝不允许带着半成品 VGMap 永久幂等跳过。校验项：
        - SkeletonGroup / DeformDrawIndex / OriginalVertexCount 是非负整数；
        - VGMapAlgorithmVersion == 当前算法版本；
        - `expect_cross` 时 `SkeletonMergeMode` 必须是 `cross_group`：历史分组编号
          与跨组池槽位是两个编号域，拿错口径的缓存顶上就是「每个部件各在各的
          局部空间」；
        - VGCount / VGOffset 存在且非负；
        - VGMap 键完整覆盖 0..VGCount-1、len(VGMap) == VGCount、全局槽位非负
          （缺键或规范化重复键会让导入/导出映射口径不一致）；
        - BoneMatrixFileName 指向的 ModImpRuntime 文件存在且大小
          >= VGCount * 48 字节（每骨骼 4x3 float32）。
        - 有当前 dump 时，FrameAnalysisLogSignature 必须与 log.txt 内容一致。
        """
        def _strict_int(value) -> int:
            # 与模块级 `_strict_int_or_default` 同一口径（单一事实源）。
            return _strict_int_or_default(value)

        try:
            cache_version = _strict_int(
                submesh_json.get("VGMapAlgorithmVersion", 0) or 0
            )
        except (TypeError, ValueError):
            cache_version = 0
        if cache_version != _ZZMI_VG_MAP_ALGORITHM_VERSION:
            return False
        if expect_cross and (
            submesh_json.get("SkeletonMergeMode") != _ZZMI_MERGE_MODE_CROSS
        ):
            return False
        try:
            metadata_values = (
                submesh_json["SkeletonGroup"],
                submesh_json["DeformDrawIndex"],
                submesh_json["OriginalVertexCount"],
            )
            skeleton_group, deform_draw_index, original_vertex_count = (
                _strict_int(value) for value in metadata_values
            )
        except (KeyError, TypeError, ValueError):
            return False
        if (
            skeleton_group < 0
            or deform_draw_index < 0
            or original_vertex_count < 0
            or skeleton_group > 0xFFFFFFFF
            or deform_draw_index > 0xFFFFFFFF
            or original_vertex_count > 0xFFFFFFFF
        ):
            return False

        vg_map = submesh_json.get("VGMap")
        if not isinstance(vg_map, dict) or not vg_map:
            return False
        try:
            vg_count = _strict_int(submesh_json.get("VGCount"))
            vg_offset = _strict_int(submesh_json.get("VGOffset"))
            mapped = {}
            for key, value in vg_map.items():
                normalized_key = _strict_int(key)
                if normalized_key in mapped:
                    return False
                mapped[normalized_key] = _strict_int(value)
        except (TypeError, ValueError):
            return False
        if (
            vg_count <= 0
            or vg_count > 0xFFFFFFFF
            or vg_offset < 0
            or vg_offset > 0xFFFFFFFF
            or vg_offset + vg_count > 0x100000000
        ):
            return False
        if len(vg_map) != vg_count or len(mapped) != vg_count:
            return False
        if set(mapped.keys()) != set(range(vg_count)):
            return False
        if any(slot < 0 or slot > 0xFFFFFFFF for slot in mapped.values()):
            return False

        bone_matrix_path = cls._runtime_cache_path(submesh_json, json_path, unique_str)
        if not os.path.isfile(bone_matrix_path):
            return False
        if not EFMIBoneMapBuilder.cache_file_size_ok(bone_matrix_path, vg_count):
            return False
        # t75：通道计划是**唯一**实例判定口径的输入。缺失 / 版本不符 / 内容
        # 与 VGMap 不一致的缓存一律拒绝复用——导出侧不得静默退回出现次判据。
        try:
            channel_version = _strict_int(
                submesh_json.get("ChannelPlanVersion", 0) or 0
            )
        except (TypeError, ValueError):
            channel_version = 0
        if channel_version != ZZMI_CHANNEL_PLAN_VERSION:
            return False
        channel_record = submesh_json.get("ChannelPlan")
        if not isinstance(channel_record, dict):
            return False
        try:
            channel_slot = _strict_int(channel_record.get("channel_slot"))
            channel_local = _strict_int(channel_record.get("channel_local"))
            channel_vg_count = _strict_int(channel_record.get("vg_count"))
            channel_vg_offset = _strict_int(channel_record.get("vg_offset"))
        except (TypeError, ValueError):
            return False
        if channel_slot < 0 or channel_slot > 0xFFFFFFFF:
            return False
        if channel_local < 0 or channel_local >= vg_count:
            return False
        if channel_vg_count != vg_count or channel_vg_offset != vg_offset:
            return False
        if mapped.get(channel_local) != channel_slot:
            return False
        return True

    @staticmethod
    def _frame_analysis_log_signature(log_path: str) -> str:
        """返回当前 FrameAnalysis/log.txt 的内容指纹，用于缓存源一致性校验。"""
        digest = hashlib.sha256()
        with open(log_path, "rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @classmethod
    def _zzmi_source_cache_intact(
        cls,
        submesh_json: dict,
        json_path: str,
        unique_str: str,
    ) -> bool:
        """校验可供未来无 dump 重建的 ZZMI CB1 来源缓存契约。

        新版每次生成都会显式写入 ``ObjectCB1CacheValid``：True 必须指向
        可解析的实际 CB1，False 表示当前 dump 没有可用对象变换、应独立分组。
        缺少该字段的是旧版缓存，不能证明目录中的 CB1 就是当时实际参与分组的
        候选；dump 尚在时必须迁移，dump 已删后的重建则按不可信处理。
        """
        marker = submesh_json.get("ObjectCB1CacheValid")
        if not isinstance(marker, bool):
            return False
        if marker is False:
            return True
        cb1_path = cls._runtime_cache_path(
            submesh_json,
            json_path,
            unique_str,
            cb1_file_name="ObjectCB1",
        )
        return bool(
            os.path.isfile(cb1_path)
            and ZZMIBoneMapBuilder.parse_object_transform(
                cb1_path,
                first_constant=cls._parse_first_constant(
                    submesh_json.get("ObjectCB1FirstConstant", 0)
                ),
            ) is not None
        )

    @classmethod
    def missing_merged_metadata_exist(
        cls, workspace_root: str, unique_str_list: list[str]
    ) -> bool:
        """请求集内是否有子网格的骨骼合并数据**缺失或不完整**（自愈判据）。

        供导入侧判断「复选框关闭时也必须预生成/修复」：与 EFMI 的同名方法
        （t15 活性修复）语义一致但更严——EFMI 只判 ``VGMap``/``VGMapAlgorithmVersion``
        是否缺失；ZZMI 还要判**快路径完整性**（``_zzmi_cache_intact``），因此旧算法
        版本、缺 ``SkeletonGroup``/``DeformDrawIndex``/``OriginalVertexCount``、
        palette 缓存缺失/截断、通道计划版本不符等一律算「需要处理」，复选框关闭时
        的下一次导入也会自愈：能就地升级的就地升级（v4→v5 只需 VGMap，不需要提取
        文件），其余按当前提取源重建，提取源不在则用工作空间缓存重建。

        刻意**不做**提取源指纹（log.txt 全量哈希）比对：那会让每次导入都哈希整个
        log；换用新提取文件请重新选择提取源并重新导入（或清缓存后重导入）。
        只检查请求集内可解析的 json；``GPU-PreSkinning`` 为 False 的 json 不参与判定。

        口径随开关走：开着跨组合并时，历史分组编号的缓存也算「需要处理」——
        两种编号域的 VGMap 长得一样，只有 ``SkeletonMergeMode`` 分得清。
        """
        if not workspace_root or not unique_str_list:
            return False
        expect_cross = cls._cross_numbering_expected(workspace_root)
        for unique_str in unique_str_list:
            json_path = cls._resolve_submesh_json_path(workspace_root, unique_str)
            if not json_path:
                continue
            try:
                payload = JsonUtils.LoadFromFile(json_path)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("GPU-PreSkinning") is False:
                continue
            if not payload.get("VGMap") or not payload.get("VGMapAlgorithmVersion"):
                return True
            if not cls._zzmi_cache_intact(
                payload, json_path, unique_str, expect_cross=expect_cross
            ):
                return True
        return False

    @classmethod
    def count_ready_merged_metadata(
        cls, workspace_root: str, unique_str_list: list[str]
    ) -> int:
        """请求集内**已具备完整骨骼合并数据**的子网格数量（0 = 一个都没有）。

        供导入侧区分「整体失败」与「部分生成」（ui/ui_func_import_ssmt.py）：
        ``ensure_skeleton_data`` 的 json 写回发生在返回之前，因此它报 ok=False 时
        仍可能已有部件拿到 VGMap —— 同一 DrawIB 在一帧内被多个对象实例绘制
        （对象 CB1 各不相同）时，该 DrawIB 按「无法安全确定目标模型」被跳过，
        其它单实例部件照常生成。旧实现把这两种情形一律当成整体失败，于是
        一个多实例部件就能把整批部件拖回普通导入并自动关闭开关。

        判定口径与 ``missing_merged_metadata_exist`` 逐件完全一致（同一份
        ``_zzmi_cache_intact``）：把一个「有没有缺失」的问题换成「有多少件就绪」。
        切换开关时口径同样随开关走——开着跨组却只有历史分组编号的缓存不算就绪，
        否则导入侧会把「各在各的局部空间」当成合并部分生效。
        """
        if not workspace_root or not unique_str_list:
            return 0
        expect_cross = cls._cross_numbering_expected(workspace_root)
        ready = 0
        for unique_str in unique_str_list:
            json_path = cls._resolve_submesh_json_path(workspace_root, unique_str)
            if not json_path:
                continue
            try:
                payload = JsonUtils.LoadFromFile(json_path)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("GPU-PreSkinning") is False:
                continue
            if not payload.get("VGMap") or not payload.get("VGMapAlgorithmVersion"):
                continue
            if not cls._zzmi_cache_intact(
                payload, json_path, unique_str, expect_cross=expect_cross
            ):
                continue
            ready += 1
        return ready

    @classmethod
    def _cross_numbering_expected(cls, workspace_root: str) -> bool:
        """本工作空间是否应按**骨骼池编号**判定缓存是否可用。

        开关开着、并且池方案文件就在盘上时才成立：没有方案文件时历史分组编号
        才是这个工作空间的正常态（提取源没有锚点骨可锚），不能因此把它常年判成
        「缺件」反复重建。
        """
        if not cls.cross_group_merged_enabled():
            return False
        try:
            from .zzmi_cross_group_palette import get_profile_path

            return os.path.isfile(get_profile_path(workspace_root))
        except Exception:  # pragma: no cover - 磁盘异常
            return False

    @staticmethod
    def cross_group_merged_enabled() -> bool:
        """跨组骨骼合并开关（`cross_group_merged_vgmap_test`）。

        `common/` 不能反向 import `ui/`，这里只按名字取 `GlobalProterties` 上的同名
        访问器；宿主没注册该属性时退回插件里的默认值（开启，与属性声明一致）。
        """
        try:
            from .global_properties import GlobalProterties
        except Exception:
            return True
        getter = getattr(GlobalProterties, "cross_group_merged_vgmap_test", None)
        if not callable(getter):
            return True
        try:
            return bool(getter())
        except Exception:
            return True

    @classmethod
    def _cross_group_plan_or_none(cls, ready_groups: dict) -> dict | None:
        """算「整只角色一个骨骼池」的锚点链方案；算不出来返回 None（回落历史编号）。

        绝不让它把导入搞崩：跨组没有共享骨可锚、骨骼表/物体矩阵读不到、部件之间
        互相矛盾时 `plan_from_parts` 会抛 `ZZMICrossGroupPaletteError` —— 这里吞掉
        并打印原因，调用方走历史分组 + 组基址编号（跨组骨骼不合并，行为同旧版）。

        单个部件的脏数据不在这条路上：骨骼表带 NaN/inf 的部件由 `plan_from_parts`
        排除出合并计算（仍在池尾独占槽位），权重带 NaN 的按 0 计数 —— 一个部件
        的坏数据不许否决整只角色的骨骼池。
        """
        try:
            from .zzmi_cross_group_palette import plan_from_parts
        except Exception as exc:  # pragma: no cover - 导入期缺文件
            print(f"[ZZMI骨骼合并] 骨骼池方案不可用：{exc}")
            return None
        # 共有骨从**导入期去重表**取：`build_vg_maps` 按骨骼矩阵逐位相等判等，得到
        # 的「本部件局部骨号 ↔ 另一部件局部骨号」是精确对应关系。本机各部件的
        # `vs-cb1` 逐位相同（同一帧五份 dump 的 sha 一致），抬到世界空间一根骨都
        # 对不上——参考实现「靠世界空间认骨」这一步在本机没有数据可依，但锚点公式、
        # 生成树、运行期换算仍然完全用参考实现那一套，只是「谁和谁是同一根骨」
        # 这个事实改由去重表回答。
        shared_pairs: dict[str, dict[str, list]] = {}
        try:
            dedup_maps, dedup_offsets, _ = ZZMIBoneMapBuilder.build_vg_maps(
                {draw_ib: group["palette"] for draw_ib, group in ready_groups.items()},
                {
                    draw_ib: group["signatures"]
                    for draw_ib, group in ready_groups.items()
                },
                dedup_excluded={
                    draw_ib
                    for draw_ib, group in ready_groups.items()
                    if group.get("dedup_excluded")
                },
            )
        except Exception as exc:
            print(f"[ZZMI骨骼合并] 去重表算不出来，共有骨退回世界空间匹配：{exc}")
            dedup_maps, dedup_offsets = {}, {}
        if dedup_maps:
            block_owner: dict[int, tuple] = {}
            for draw_ib, offset in dedup_offsets.items():
                bone_count = len(ready_groups[draw_ib]["palette"])
                for local in range(int(bone_count)):
                    block_owner.setdefault(int(offset) + local, (draw_ib, local))
            for draw_ib, mapping in dedup_maps.items():
                for local, slot in mapping.items():
                    owner = block_owner.get(int(slot))
                    if owner is None or owner[0] == draw_ib:
                        continue
                    shared_pairs.setdefault(draw_ib, {}).setdefault(
                        owner[0], []
                    ).append((int(local), int(owner[1])))
            if shared_pairs:
                total_pairs = sum(
                    len(pairs)
                    for per_owner in shared_pairs.values()
                    for pairs in per_owner.values()
                )
                print(
                    f"[ZZMI骨骼合并] 去重表认出 {total_pairs} 对共有骨，"
                    f"跨 {len(shared_pairs)} 个部件（用作锚点）"
                )
        try:
            planned_maps, planned_offsets, profile, plan_warnings = plan_from_parts({
                draw_ib: {
                    "palette": group["palette"],
                    "transform": group["transform"],
                    "slot_weights": group.get("slot_weights"),
                    "vertex_count": group.get("vertex_count"),
                    "shape_keyed": bool(group.get("shape_keyed")),
                    "shared": shared_pairs.get(draw_ib) or {},
                }
                for draw_ib, group in ready_groups.items()
            })
        except Exception as exc:
            print(f"[ZZMI骨骼合并] 骨骼池方案算不出来，本次按历史分组编号：{exc}")
            return None
        if not profile or not profile.get("components"):
            print("[ZZMI骨骼合并] 骨骼池方案为空，本次按历史分组编号。")
            return None
        components = profile.get("components") or []
        anchored = sum(
            1 for item in components
            if int(item.get("parent_component_id", -1)) >= 0
        )
        print(
            f"[ZZMI骨骼合并] 骨骼池方案就绪：{len(components)} 个部件 / "
            f"{int(profile.get('pool_bones', 0))} 根骨骼 / {anchored} 条锚点边"
            f"（跨组骨骼合并将按参考实现的锚点链换算）"
        )
        return {
            "maps": planned_maps,
            "offsets": planned_offsets,
            "profile": profile,
            "warnings": list(plan_warnings or ()),
        }

    @classmethod
    def ensure_skeleton_data(
        cls,
        workspace_root: str,
        unique_str_list: list[str],
        force: bool = False,
    ) -> tuple[bool, str]:
        """为 ZZMI 工作空间的子网格生成并写回骨骼合并数据（幂等）。

        返回 (是否成功, 描述)。
        """
        if not unique_str_list:
            return False, "没有子网格需要处理。"

        frame_analysis_dir = cls.resolve_frame_analysis_dir(workspace_root)
        log_path = os.path.join(frame_analysis_dir, "log.txt") if frame_analysis_dir else ""
        parser = None
        resolver = None
        frame_analysis_signature = ""
        if frame_analysis_dir:
            if not os.path.isfile(log_path):
                print(
                    f"[ZZMI骨骼合并] 提示: FrameAnalysis 缺少 log.txt（{log_path}），"
                    "将仅用工作空间缓存重建"
                )
            else:
                parser = ZZMILogParser(log_path)
                resolver = ZZMIDeformResolver(parser)
                frame_analysis_signature = cls._frame_analysis_log_signature(log_path)
                if not resolver.passes:
                    print(
                        "[ZZMI骨骼合并] 提示: FrameAnalysis log 中未识别到 deform pass，"
                        "将仅用工作空间缓存重建"
                    )
                    parser = None
                    resolver = None
        else:
            # dump 目录已被删除：上次导入已把 palette / 对象变换 CB 复制进工作空间
            # ModImpRuntime，缓存完整的子网格可以脱离 dump 重建。工作区已记录过提取源
            # 时**不会**自动改用游戏目录里的其它帧（防跨帧/跨模型混用，见
            # resolve_frame_analysis_dir）：要换用新的提取文件，请在面板重新选择
            # 提取源后重新导入。
            print(
                "[ZZMI骨骼合并] 提示: 未找到 FrameAnalysis 目录（记录过的提取源已被"
                "删除/搬走），本次仅用工作空间缓存重建；如需改用新的提取文件，"
                "请重新选择提取源后重新导入"
            )

        # 第一遍：收集每个子网格信息并按 DrawIB 分组（同 DrawIB 的拆分子网格共享
        # 同一 deform pass / palette / VGMap，只参与一次去重）。
        # drawib -> {"palette", "vg_count", "members": [unique_str...],
        #            "json_paths": {unique_str: path}, "palette_path", "draw_index",
        #            "signatures", "transform"}
        #
        # 幂等 schema 门控（分组版一致性规则）：VGMap 按"组内槽位"写回，一次导入的
        # 所有部件必须共享同一次分组计算——不允许"部分部件用缓存、部分部件新算"
        # （否则两组槽位口径可能不一致）。因此：只要任一目标子网格缺 VGMap 或
        # 缺 SkeletonGroup 字段（旧缓存/新工作空间），就对全部目标重建；全部齐备
        # 才整批幂等跳过（快速路径，不解析 dump）。
        # DeformDrawIndex/OriginalVertexCount 属于导出侧守卫所需 schema（合并网格
        # 的 deform 时序校验 + 渲染 vb1 换绑），同样纳入门控：旧缓存缺字段时
        # 整批重建刷新。
        # 快路径还校验**缓存产物完整性**：算法版本必须与当前一致；BoneMatrixFileName
        # 引用的骨骼缓存文件必须真实存在。否则（复制失败 / 工作空间搬迁漏掉
        # ModImpRuntime / 旧算法缓存）整批重建，绝不允许带着半成品永久幂等跳过。
        groups: dict[str, dict] = {}
        stale = 0
        # 阻断修复（t80 §2.4）：本次调用里「旧版但仅凭 VGMap 可升级」的 DrawIB 集合。
        # 只有**全部成员**都可升级的 DrawIB 才就地升级——通道计划是共享骨连通分量
        # 的组级性质，半个组升级会让组内两套口径并存。
        cls._zzmi_legacy_upgrade_draw_ibs = set()
        _migratable_members: dict[str, set[str]] = {}
        _group_members: dict[str, set[str]] = {}
        # 无法定位/无法解析 json 的目标（最后按“未处理目标”计入失败报告，
        # 不能让部分完成被报告成完整成功）
        unresolved_targets: list[str] = []
        seen_members: set[str] = set()
        # 快路径必须按**本次要的口径**校验：开跨组合并时，缓存里的 VGMap 必须是
        # 池槽位，并且池方案文件得在盘上（两者缺一就无法确认编号域，而「看起来
        # 有 VGMap」正是把历史分组编号当成合并成果的入口）。
        expect_cross = bool(cls.cross_group_merged_enabled())
        pool_profile_ready = False
        if expect_cross:
            try:
                from .zzmi_cross_group_palette import get_profile_path

                pool_profile_ready = os.path.isfile(
                    get_profile_path(workspace_root)
                )
            except Exception:  # pragma: no cover - 磁盘异常
                pool_profile_ready = False
        # 编号域校验只在池方案在盘上时才有意义：没有方案文件时历史分组编号就是
        # 本工作空间的正常态，判据由下面的「池方案不在 ⇒ 强制重建」承担。
        cross_mode_ready = expect_cross and pool_profile_ready

        for unique_str in unique_str_list:
            if unique_str in seen_members:
                continue  # 重复目标只处理一次（计数口径与写回均不重复）
            seen_members.add(unique_str)
            json_path = cls._resolve_submesh_json_path(workspace_root, unique_str)
            if not json_path:
                print(f"[ZZMI骨骼合并] 跳过 {unique_str}: 未找到子网格 json")
                unresolved_targets.append(unique_str)
                continue

            submesh_json = JsonUtils.LoadFromFile(json_path)
            if not isinstance(submesh_json, dict):
                print(f"[ZZMI骨骼合并] 跳过 {unique_str}: 子网格 json 读取失败")
                unresolved_targets.append(unique_str)
                continue

            bare_name = unique_str.split(".", 1)[-1]
            draw_ib = bare_name.split("-")[0] if "-" in bare_name else bare_name
            if not draw_ib:
                print(f"[ZZMI骨骼合并] 跳过 {unique_str}: 无法解析 DrawIB")
                unresolved_targets.append(unique_str)
                continue

            group = groups.get(draw_ib)
            if group is None:
                group = {
                    "palette": None,
                    "vg_count": 0,
                    "original_vertex_counts": {},
                    "members": [],
                    "json_paths": {},
                    "palette_path": "",
                    "draw_index": "",
                    "signatures": {},
                    "transform": None,
                    "cb1_path": "",
                    "cb1_cache_valid": False,
                    "skip_reason": "",
                    "representative": unique_str,
                    # 组件级「排除去重」（json 标记 VGMapDedupExcluded=True）：任一成员
                    # 声名排除即整 DrawIB 退出合并（palette 按 DrawIB 共享），去重阶段
                    # 恒等映射独占声明段；缺件占位侧同键跳过（ui/universal/zzmi.py）。
                    # 时效性对齐 EFMI：只在**重建**时生效，幂等快路径命中即跳过重算。
                    "dedup_excluded": False,
                }
                groups[draw_ib] = group
            group["members"].append(unique_str)
            group["json_paths"][unique_str] = json_path
            if bool(submesh_json.get("VGMapDedupExcluded")):
                group["dedup_excluded"] = True

            cache_intact = cls._zzmi_cache_intact(
                submesh_json, json_path, unique_str, expect_cross=cross_mode_ready
            )
            if (
                cache_intact
                and parser is not None
                and submesh_json.get("FrameAnalysisLogSignature") != frame_analysis_signature
            ):
                cache_intact = False
            if (
                cache_intact
                and parser is not None
                and not cls._zzmi_source_cache_intact(
                    submesh_json, json_path, unique_str
                )
            ):
                # dump 尚在时自动迁移旧版/缺件来源缓存，避免本次幂等跳过后
                # 用户清 VGMap、删 dump 才发现 CB1 不可复现。
                cache_intact = False
            if (
                cache_intact
                and expect_cross
                and not pool_profile_ready
                and submesh_json.get("SkeletonMergeMode") != _ZZMI_MERGE_MODE_GROUP
            ):
                # 池方案文件不在、缓存却自称池编号（或根本没有口径字段）：无从
                # 确认这份 VGMap 是池槽位还是历史分组编号，重建一次把口径落定。
                # 已写明 group 的是上次方案失败后的历史编号——那是本工作空间的
                # 正常态，不必每次导入都重算一遍。
                cache_intact = False
            if not force and cache_intact:
                continue  # 该子网格缓存完整（临时计数，见下）
            # 阻断修复（t80 §2.4）：旧版但**仅凭 VGMap 可升级**的缓存不计入 stale。
            # v5 相对 v4 只多了可从 VGMap 单独推导的 ChannelPlan 系列字段 ⇒ 不需要
            # dump、不需要重跑去重，就地升级即可。把它判成 stale 会触发整批重建，
            # 而重建依赖已删除的 FrameAnalysis/dump（"工作空间缓存重建"只能保住
            # palette，分组口径会随 CB1 缓存漂移）⇒ 旧工作区一开就死。
            draw_ib_for_migration = draw_ib
            _group_members.setdefault(draw_ib_for_migration, set()).add(unique_str)
            if (
                not cache_intact
                and not force
                and cls._zzmi_cache_usable_for_migration(
                    submesh_json, json_path, unique_str
                )
            ):
                _migratable_members.setdefault(draw_ib_for_migration, set()).add(
                    unique_str
                )
                continue  # 留到重建阶段统一升级（见下"v4→v5 就地升级"）
            stale += 1

        # 只有**全部成员**都可升级的 DrawIB 才就地升级（组级口径：通道计划是共享骨
        # 连通分量的性质，半个组升级会让组内两套口径并存）。
        cls._zzmi_legacy_upgrade_draw_ibs = {
            draw_ib
            for draw_ib, members in _migratable_members.items()
            if members and members == _group_members.get(draw_ib, set())
        }
        if cls._zzmi_legacy_upgrade_draw_ibs:
            # 就地升级（写回工作空间 json，不重跑去重）。**先升级再判定**：
            # 升级后的组不再需要重建，把它们从候选集里摘掉并重算 stale，避免
            # "一边升级一边又按慢路径重建"（后者会把全局槽位编号重排）。
            upgraded_count = cls._zzmi_upgrade_legacy_vgmap_cache(groups)
            if upgraded_count:
                exempted = {
                    member
                    for draw_ib in cls._zzmi_legacy_upgrade_draw_ibs
                    for member in groups.get(draw_ib, {}).get("members", [])
                }
                stale = max(0, stale - len(exempted))
                print(
                    "[ZZMI骨骼合并] 旧版 VGMap 缓存已就地升级到 v"
                    f"{_ZZMI_VG_MAP_ALGORITHM_VERSION}："
                    f"{len(cls._zzmi_legacy_upgrade_draw_ibs)} 个 DrawIB / "
                    f"{upgraded_count} 份 json（补齐 ChannelPlan，未重跑去重）"
                )
            cls._zzmi_legacy_upgrade_draw_ibs = set()

        up_to_date = len(groups) and stale == 0
        if up_to_date and not force:
            total = sum(len(g["members"]) for g in groups.values())
            if unresolved_targets:
                # 目标目录被删/无法解析时绝不能报“全部已缓存”：这些目标
                # 没有进入任何组，必须显式失败，否则会被幂等快路径静默遗漏。
                shown = unresolved_targets[:5]
                suffix = "…" if len(unresolved_targets) > 5 else ""
                return False, (
                    f"{total} 个子网格已有骨骼合并数据（幂等），但 "
                    f"{len(unresolved_targets)} 个目标无法解析: "
                    f"{'、'.join(shown)}{suffix}"
                )
            if cls._zzmi_upgrade_legacy_vgmap_cache(groups):
                total = sum(len(g["members"]) for g in groups.values())
                return True, (
                    f"所有 {total} 个子网格均已有骨骼合并数据；"
                    f"其中旧版缓存已就地升级到 VGMap v{_ZZMI_VG_MAP_ALGORITHM_VERSION}"
                    "（补齐 ChannelPlan 系列字段，未重跑去重）。"
                )
            return True, f"所有 {total} 个子网格均已有骨骼合并数据（幂等跳过）。"

        # 第二遍：逐组反查 deform pass -> palette，读取 Blend.buf 得 vg_count
        for draw_ib, group in groups.items():
            if group.get("skip_reason") and parser is None:
                continue
            representative = group["representative"]
            json_path = group["json_paths"][representative]
            submesh_json = JsonUtils.LoadFromFile(json_path)
            if not isinstance(submesh_json, dict):
                group["skip_reason"] = "子网格 json 读取失败"
                continue

            category_hash = submesh_json.get("CategoryHash", {}) or {}
            position_hash = str(category_hash.get("Position", "") or "")
            vertex_limit_hash = str(submesh_json.get("VertexLimitVB", "") or "")

            # 路径 C 的渲染 draw 列表（LOD 目录下的 ComponentName_DrawCallIndexList.json）
            lod_dir = os.path.dirname(os.path.dirname(os.path.dirname(json_path)))
            role_mapping = cls._load_drawcall_index_list(lod_dir)
            render_draws = role_mapping.get(
                os.path.basename(os.path.dirname(os.path.dirname(json_path))), []
            )
            parser_render_draws_for_ib = []
            if parser is not None:
                # 组件索引表可能仍指向上一帧；当前 log 的 IB hash 才是本次
                # CB1/实例判定的权威来源。合并两者，旧索引在后续 IB 门控中会
                # 自动被过滤；新索引则能发现同一 IB 的多个对象实例。
                parser_render_draws_for_ib = parser.get_render_draw_indices_for_ib(draw_ib)
                render_draws = sorted(
                    set(render_draws)
                    | set(parser_render_draws_for_ib)
                )

            draw_index, deform_pass, via = "", None, ""
            if resolver is not None:
                draw_index, deform_pass, via = resolver.resolve(
                    position_hash=position_hash,
                    vertex_limit_hash=vertex_limit_hash,
                    render_draw_indices=render_draws,
                    expected_draw_ib=draw_ib,
                )

            palette_logical = ""
            if deform_pass is not None:
                palette_logical = f"{draw_index}-vs-t0={deform_pass['palette_hash']}"
            palette_path = parser.get_deduped_path(palette_logical) if parser is not None else None
            if not palette_path:
                # dump 被删除/搬走后的兜底：上次导入已把 palette 复制到工作空间
                # ModImpRuntime/<bare>-BoneMatrix.buf（导出侧同款缓存）。同 DrawIB
                # 的拆分子网格共享同一 palette，因此**遍历组内全部成员**找缓存——
                # 代表子网格的缓存丢失时，兄弟子网格的缓存同样有效。
                for member in group["members"]:
                    member_json_path = group["json_paths"].get(member, "")
                    if not member_json_path:
                        continue
                    try:
                        member_json = JsonUtils.LoadFromFile(member_json_path)
                    except Exception:
                        member_json = None
                    if not isinstance(member_json, dict):
                        member_json = {}
                    cached_path = cls._runtime_cache_path(
                        member_json, member_json_path, member
                    )
                    if os.path.isfile(cached_path):
                        palette_path = cached_path
                        print(
                            f"[ZZMI骨骼合并] {draw_ib}: dump 中 palette 缺失，"
                            f"已回退工作空间缓存（{member}）"
                        )
                        break
            if not palette_path:
                print(f"[ZZMI骨骼合并] 跳过 {draw_ib}: palette 缺失 {palette_logical or '(工作空间缓存也不存在)'}")
                group["skip_reason"] = "palette 缺失（dump 与全部成员缓存均无）"
                continue

            # draw_index：dump 可用时来自 deform pass 反查；否则回退上次写回的
            # DeformDrawIndex 元数据（导出侧守卫时序校验依赖它）。同 DrawIB 成员
            # 共享同一序号，代表子网格缺字段时遍历兄弟子网格。
            if not draw_index:
                cached_deform_index = None
                for member in group["members"]:
                    member_json_path = group["json_paths"].get(member, "")
                    if not member_json_path:
                        continue
                    try:
                        member_json = JsonUtils.LoadFromFile(member_json_path)
                    except Exception:
                        member_json = None
                    if not isinstance(member_json, dict):
                        continue
                    cached_deform_index = member_json.get("DeformDrawIndex")
                    if cached_deform_index is not None:
                        break
                try:
                    draw_index = str(int(cached_deform_index)).zfill(6) if cached_deform_index is not None else ""
                except (TypeError, ValueError):
                    draw_index = ""
            if not draw_index:
                print(f"[ZZMI骨骼合并] 跳过 {draw_ib}: 无 deform draw 序号（dump 与缓存均缺失）")
                group["skip_reason"] = "无 deform draw 序号"
                continue

            try:
                palette = ZZMIBoneMapBuilder.load_palette(palette_path)
            except Exception as e:
                print(f"[ZZMI骨骼合并] 跳过 {draw_ib}: 读取 palette 失败: {e}")
                group["skip_reason"] = f"palette 读取失败: {e}"
                continue

            # vg_count：同 DrawIB 的全部拆分子网格共同决定。每个成员可能使用
            # 不同的局部骨骼；只解析代表成员会截短 palette 并生成不完整 VGMap。
            # 每个 Blend.buf 的 BLENDINDICES 有效通道最大索引 + 1。
            # 有效通道 = 索引非哨兵（按数据格式：u1->0xFF / u2->0xFFFF /
            # u4|i4->0xFFFFFFFF）且对应 BLENDWEIGHTS > 0；无权重元素时按
            # “每顶点第一索引权重=1”兜底（与导入侧默认权重语义一致）。
            # 否则 R16_UINT 等格式的空通道哨兵 0xFFFF 会被算成真实骨骼，
            # vg_count 膨胀到 65,536 后因 palette 不足整部件被跳过。
            vg_count = 0
            combined_signatures = {}
            # t75：逐**局部骨槽**的顶点权重合计（= 该骨在几何里被多少权重引用），
            # 供通道骨选择当「带顶点权重最多」的次级排序键。同 DrawIB 的成员求和。
            slot_weights = numpy.zeros(0, dtype=numpy.float64)
            member_parse_failed = False
            for member in group["members"]:
                member_json_path = group["json_paths"][member]
                try:
                    member_json = JsonUtils.LoadFromFile(member_json_path)
                except Exception as e:
                    group["skip_reason"] = f"{member}: 子网格 json 读取失败: {e}"
                    member_parse_failed = True
                    break
                if not isinstance(member_json, dict):
                    group["skip_reason"] = f"{member}: 子网格 json 读取失败"
                    member_parse_failed = True
                    break

                element_info = cls._parse_blend_element_info(member_json)
                if element_info is None:
                    group["skip_reason"] = f"{member}: Blend 类别缺少 BLENDINDICES 元素"
                    member_parse_failed = True
                    break
                blend_buf_path = os.path.join(
                    os.path.dirname(member_json_path),
                    os.path.splitext(os.path.basename(member_json_path))[0] + "-Blend.buf",
                )
                try:
                    blend_indices = EFMIBoneMapBuilder.parse_blendindices_from_buf(
                        blend_buf_path, element_info
                    )
                    blend_layout = EFMIBoneMapBuilder.parse_blend_layout(member_json)
                    blend_weights = EFMIBoneMapBuilder.parse_blendweights_from_buf(
                        blend_buf_path, blend_layout
                    )
                except Exception as e:
                    group["skip_reason"] = f"{member}: 读取 Blend.buf 失败: {e}"
                    member_parse_failed = True
                    break

                valid_mask = EFMIBoneMapBuilder.valid_blend_channels(
                    blend_indices, element_info, blend_weights
                )
                valid_indices = blend_indices[valid_mask].astype(numpy.int64)
                if len(valid_indices) == 0:
                    group["skip_reason"] = f"{member}: BLENDINDICES 无有效通道"
                    member_parse_failed = True
                    break
                vg_count = max(vg_count, int(valid_indices.max()) + 1)
                # 导出侧按成员自己的几何行数做 vb1 换绑判定。
                group["original_vertex_counts"][member] = int(len(blend_indices))
                # t75：本成员的逐骨权重合计（只在有效通道上累加）。
                try:
                    weights_flat = numpy.asarray(
                        blend_weights, dtype=numpy.float64
                    ).reshape(-1)
                    indices_flat = numpy.asarray(
                        blend_indices, dtype=numpy.int64
                    ).reshape(-1)
                    mask_flat = numpy.asarray(valid_mask).reshape(-1)
                    usable = min(len(weights_flat), len(indices_flat), len(mask_flat))
                    if usable > 0:
                        member_sums = numpy.bincount(
                            indices_flat[:usable][mask_flat[:usable]],
                            weights=weights_flat[:usable][mask_flat[:usable]],
                        )
                        if len(member_sums) > len(slot_weights):
                            grown = numpy.zeros(len(member_sums), dtype=numpy.float64)
                            grown[: len(slot_weights)] = slot_weights
                            slot_weights = grown
                        slot_weights[: len(member_sums)] += member_sums
                except Exception:
                    # 权重缺失只影响通道骨的次级排序键（记账口径），不影响判定
                    # 可行性：按 0 计入。绝不因此跳过部件。
                    pass

                position_buf_path = os.path.join(
                    os.path.dirname(member_json_path),
                    os.path.splitext(os.path.basename(member_json_path))[0] + "-Position.buf",
                )
                try:
                    member_signatures = EFMIBoneMapBuilder.compute_driven_signatures(
                        position_buf_path, blend_buf_path, member_json
                    )
                    combined_signatures = cls._merge_driven_signatures(
                        combined_signatures, member_signatures
                    )
                except Exception as e:
                    print(
                        f"[ZZMI骨骼合并] 提示 {member}: 驱动签名计算失败"
                        f"（刚性命中对将拆开）: {e}"
                    )

            if member_parse_failed:
                print(f"[ZZMI骨骼合并] 跳过 {draw_ib}: {group['skip_reason']}")
                continue

            if len(palette) < vg_count:
                print(
                    f"[ZZMI骨骼合并] 跳过 {draw_ib}: palette {len(palette)} 根骨骼 < "
                    f"顶点组 {vg_count}（数据不一致）"
                )
                group["skip_reason"] = f"palette 骨骼数 {len(palette)} < 顶点组 {vg_count}"
                continue
            if len(palette) != vg_count:
                # 实测应恒等；不等时裁到实际用量并提示
                print(
                    f"[ZZMI骨骼合并] 提示 {draw_ib}: palette {len(palette)} 根 > "
                    f"顶点组 {vg_count}，按实际用量切分"
                )
                palette = palette[:vg_count]

            group["palette"] = palette
            group["vg_count"] = vg_count
            group["palette_path"] = palette_path
            group["draw_index"] = draw_index
            group["via"] = via
            group["signatures"] = combined_signatures
            # t75：局部骨槽 → 顶点权重合计（长度 = vg_count，缺失项按 0）。
            local_weights = numpy.zeros(vg_count, dtype=numpy.float64)
            if len(slot_weights):
                usable = min(len(slot_weights), vg_count)
                local_weights[:usable] = slot_weights[:usable]
            group["slot_weights"] = local_weights

            # 骨架分组键：渲染 draw 的 vs-cb1 对象→世界矩阵（palette 蒙皮到对象空间，
            # 渲染 VS 用 cb1 摆到世界，两者逐物体 1:1 配对）。取该部件渲染 draw 列表中
            # 第一个可解析的逐部件 cb1 块。当前 dump 明确无有效 CB1 时可独立成组；
            # 仅靠工作空间重建时，标记为有效/未标记的来源缺失必须显式失败。
            # 解析顺序：当前 dump（按候选路径延迟解析）-> 上次导入复制到工作空间的
            # ObjectCB1 缓存（dump 已删除时仍能重建出相同的骨架分组）。同 DrawIB
            # 成员共享同一对象变换，代表子网格缓存缺失时遍历兄弟子网格。
            cb1_cache_paths = []
            cb1_cache_first_constants: dict[str, int] = {}
            cb1_cache_required = False
            cb1_cache_contract_unknown = False
            for member in group["members"]:
                member_json_path = group["json_paths"].get(member, "")
                if not member_json_path:
                    continue
                try:
                    member_json = JsonUtils.LoadFromFile(member_json_path)
                except Exception:
                    member_json = None
                if not isinstance(member_json, dict):
                    member_json = {}
                marker = member_json.get("ObjectCB1CacheValid")
                if marker is True:
                    cb1_cache_required = True
                elif marker is False:
                    # 只有新版明确验证过的 CB1 才能参与 cache-only 分组。False
                    # 表示当前 dump 曾判定无效，必须忽略残留文件。
                    continue
                else:
                    # 未标记的旧版来源无法证明曾参与 SkeletonGroup；有 dump 时
                    # 可以迁移，无 dump 时不得静默按独立对象重分组。
                    cb1_cache_contract_unknown = True
                    continue
                member_cb1 = cls._runtime_cache_path(
                    member_json, member_json_path, member, cb1_file_name="ObjectCB1"
                )
                if os.path.isfile(member_cb1):
                    cb1_cache_paths.append(member_cb1)
                    cb1_cache_first_constants[member_cb1] = cls._parse_first_constant(
                        member_json.get("ObjectCB1FirstConstant", 0)
                    )
            cb1_path = ""
            dump_cb1_seen = False
            first_dump_cb1_path = ""
            dump_transform_candidates = []
            for render_draw in sorted(render_draws):
                # ComponentName 映射可能包含相似模型的渲染 draw；CB1 是对象空间分组键，
                # 不能只按 draw 序号/文件存在就采纳，否则当前部件会被搬进别的模型的
                # SkeletonGroup。只有该渲染 draw 明确绑定目标 DrawIB 时才允许参与分组。
                if parser is not None and parser.get_ib_hash(render_draw) != draw_ib:
                    continue
                candidate = parser.get_render_cb1_path(render_draw) if parser is not None else None
                if candidate:
                    dump_cb1_seen = True
                    if not first_dump_cb1_path:
                        first_dump_cb1_path = candidate
                    # dump 是整块资源：本 draw 的对象变换在它绑定的 first_constant
                    # 窗口内。多对象共享数组里 float 0 处是别的对象的矩阵，必须
                    # 按窗口取，否则同对象空间的部件会被误拆成不同 SkeletonGroup。
                    first_constant = (
                        parser.get_vs_cb_first_constant(render_draw, 1)
                        if parser is not None
                        else 0
                    )
                    transform = ZZMIBoneMapBuilder.parse_object_transform(
                        candidate,
                        first_constant=first_constant,
                    )
                    if transform is not None:
                        dump_transform_candidates.append(
                            (transform, candidate, first_constant)
                        )

            # 一个 DrawIB 可能在同一帧被多个实例绘制；它们的 IB/VB hash 相同，
            # 但对象 CB1 不同。旧实现无条件取第一个 CB1，随后把所有实例当成
            # 一个 SkeletonGroup，导出时修改其中一个实例会污染另一个。没有额外
            # 的实例选择键时，安全策略是拒绝该 DrawIB 的合并缓存，而不是猜一个。
            unique_dump_transforms = {}
            for transform, candidate, candidate_first_constant in dump_transform_candidates:
                unique_dump_transforms.setdefault(
                    transform, (candidate, candidate_first_constant)
                )
            if len(unique_dump_transforms) > 1:
                group["skip_reason"] = (
                    f"同一 DrawIB 对应 {len(unique_dump_transforms)} 个不同对象 CB1 实例，"
                    "无法安全确定目标模型；请使用只包含单个实例的 FrameAnalysis"
                )
                group["palette"] = None
                group["cb1_path"] = ""
                group["cb1_cache_valid"] = False
                print(f"[ZZMI骨骼合并] 跳过 {draw_ib}: {group['skip_reason']}")
                continue
            if unique_dump_transforms:
                # 必须缓存“实际用于分组”的同一个有效 CB1。旧实现先记住首个
                # 候选，即使后续候选才有效，也会把错误实例的 CB1 发布到工作空间。
                # 同时记下该候选的窗口起点：ObjectCB1 缓存保存的是 dump 原始字节，
                # 无 dump 重建时必须按同一窗口还原同一个对象变换。
                group["transform"], cb1_pick = next(iter(unique_dump_transforms.items()))
                cb1_path, group["cb1_first_constant"] = cb1_pick
                group["cb1_cache_valid"] = True
            if (
                group["transform"] is None
                and parser is not None
                and parser_render_draws_for_ib
                and cb1_cache_required
                and not dump_cb1_seen
            ):
                group["skip_reason"] = (
                    "当前 FrameAnalysis 命中了目标 IB，但没有可用对象 CB1；"
                    "不能回退到其它帧的 ObjectCB1 对象实例缓存"
                )
                group["palette"] = None
                print(f"[ZZMI骨骼合并] 跳过 {draw_ib}: {group['skip_reason']}")
                continue
            if group["transform"] is None and not dump_cb1_seen and parser is None:
                for member_cb1 in cb1_cache_paths:
                    # ObjectCB1 缓存保存的是 dump 原始字节（整块资源），窗口起点由
                    # 生成时写回的 ObjectCB1FirstConstant 还原；缺失时按 0 处理
                    # （旧版缓存来自逐部件 512B 块，窗口恒为 0）。
                    cache_first_constant = cb1_cache_first_constants.get(member_cb1, 0)
                    transform = ZZMIBoneMapBuilder.parse_object_transform(
                        member_cb1,
                        first_constant=cache_first_constant,
                    )
                    if transform is not None:
                        group["transform"] = transform
                        group["cb1_cache_valid"] = True
                        if not cb1_path:
                            cb1_path = member_cb1
                        print(
                            f"[ZZMI骨骼合并] {draw_ib}: dump 中 cb1 缺失，"
                            f"已回退工作空间 ObjectCB1 缓存（{os.path.basename(os.path.dirname(os.path.dirname(member_cb1)))}）"
                        )
                        break
                if (
                    group["transform"] is None
                    and (
                        cb1_cache_required
                        or (cb1_cache_contract_unknown and parser is None)
                    )
                ):
                    group["skip_reason"] = (
                        "工作空间 ObjectCB1 来源缓存缺失、损坏或未标记，"
                        "无法保持原 SkeletonGroup"
                    )
                    # 来源契约不完整时禁止进入 ready_groups；否则后续会把
                    # transform=None 当作独立对象，静默改写正确分组。
                    group["palette"] = None
                    print(f"[ZZMI骨骼合并] 跳过 {draw_ib}: {group['skip_reason']}")
                    continue
            elif group["transform"] is None and first_dump_cb1_path:
                # 当前 dump 确实给出了 CB1，但其内容不适合作为对象变换。
                # 仍刷新原始文件副本（便于诊断/保持“当前源覆盖旧源”），同时
                # 以 ObjectCB1CacheValid=False 明确禁止 cache-only 路径使用它。
                cb1_path = first_dump_cb1_path
            group["cb1_path"] = cb1_path
            if group["transform"] is None:
                print(f"[ZZMI骨骼合并] 提示 {draw_ib}: 未能解析对象变换（渲染 cb1），独立成组")

        ready_groups = {
            draw_ib: group for draw_ib, group in groups.items() if group["palette"] is not None
        }
        unprocessed_groups = {
            draw_ib: (group.get("skip_reason") or "未知原因")
            for draw_ib, group in groups.items()
            if group["palette"] is None
        }
        if not ready_groups:
            failure_parts: list[str] = []
            if unprocessed_groups:
                detail = "、".join(
                    f"{draw_ib}({reason})"
                    for draw_ib, reason in sorted(unprocessed_groups.items())
                )
                failure_parts.append(f"{len(unprocessed_groups)} 个部件未生成: {detail}")
            if unresolved_targets:
                shown = unresolved_targets[:5]
                suffix = "…" if len(unresolved_targets) > 5 else ""
                failure_parts.append(
                    f"{len(unresolved_targets)} 个目标无法解析: {'、'.join(shown)}{suffix}"
                )
            if failure_parts:
                return False, "没有子网格成功生成骨骼数据（" + "；".join(failure_parts) + "）"
            return False, "没有子网格成功生成骨骼数据。"

        # 第三遍：按对象变换分组（跨组绝不共享骨架），组内 bitwise + 刚性门控去重得
        # 组内槽位；然后按组基址拼接成**全局骨骼编号**（Blender 侧全局命名空间，
        # 组内 join 无歧义）。运行时每组骨架为全宽 buffer，**只直拷本组骨骼**——
        # 无 CB1 校准（用户拍板 2026-08-25），禁止跨组别骨骼合并。
        group_of = assign_skeleton_groups(
            {draw_ib: group["transform"] for draw_ib, group in ready_groups.items()}
        )
        group_members: dict[int, list[str]] = {}
        for draw_ib, group_index in group_of.items():
            group_members.setdefault(group_index, []).append(draw_ib)

        cross_plan = (
            cls._cross_group_plan_or_none(ready_groups)
            if cls.cross_group_merged_enabled()
            else None
        )
        if cross_plan is not None:
            # 跨组：整只角色共用一个骨骼池，槽位由锚点链算（参考实现 loyaltools 路线）。
            # 池槽位 = 部件基址 + 本地骨号；canonical 号只当身份用，正好当全局骨号下发。
            profile = cross_plan["profile"]
            cls.cross_group_profile = profile
            # 落盘：导出可能发生在这个会话之外（重开 Blender 再导出），只挂在类
            # 属性上会丢，掉盘后就按历史分组发 ini，跨组等于白算。
            try:
                from .zzmi_cross_group_palette import write_profile
                cls.cross_group_profile_path = write_profile(workspace_root, profile)
            except Exception as exc:  # pragma: no cover - 磁盘异常
                cls.cross_group_profile_path = None
                print(f"[ZZMI骨骼合并] 骨骼池方案落盘失败（导出会退回历史分组）：{exc}")
            for warning in cross_plan["warnings"]:
                print(f"[ZZMI骨骼合并] {warning}")
            group_of = {draw_ib: 0 for draw_ib in ready_groups}
            group_members = {0: list(ready_groups)}
            local_maps = {
                draw_ib: dict(vg_map)
                for draw_ib, vg_map in cross_plan["maps"].items()
            }
            local_offsets = dict(cross_plan["offsets"])
            group_base = {0: 0}
            total_slots = int(profile["pool_bones"])
        else:
            # 历史路线（含「开关开着但锚点链算不出来」的回落）：清掉上一次会话残留的
            # 骨骼池方案——内存和磁盘都要清，否则导出侧会按上一次的池槽位发 ini，
            # 而这次的 VGMap 是历史编号，两者对不上就是满屏错位。
            cls.cross_group_profile = None
            try:
                from .zzmi_cross_group_palette import get_profile_path
                stale_path = get_profile_path(workspace_root)
                if os.path.isfile(stale_path):
                    os.remove(stale_path)
            except Exception:
                pass
            cls.cross_group_profile_path = None
            # 组内去重（组内槽位 0 起）
            local_maps: dict[str, dict] = {}
            local_offsets: dict[str, int] = {}
            group_slots: dict[int, int] = {}
            for group_index in sorted(group_members):
                members = group_members[group_index]
                gm, go, total = ZZMIBoneMapBuilder.build_vg_maps(
                    {draw_ib: ready_groups[draw_ib]["palette"] for draw_ib in members},
                    {draw_ib: ready_groups[draw_ib]["signatures"] for draw_ib in members},
                    dedup_excluded={
                        draw_ib for draw_ib in members
                        if ready_groups[draw_ib].get("dedup_excluded")
                    },
                )
                local_maps.update(gm)
                local_offsets.update(go)
                group_slots[group_index] = total

            # 组基址（组索引升序累加）
            group_base: dict[int, int] = {}
            base = 0
            for group_index in sorted(group_members):
                group_base[group_index] = base
                base += group_slots[group_index]
            total_slots = base

        merge_mode = (
            _ZZMI_MERGE_MODE_CROSS
            if cls.cross_group_profile is not None
            else _ZZMI_MERGE_MODE_GROUP
        )
        vg_maps: dict[str, dict] = {}
        vg_offsets: dict[str, int] = {}
        slot_weights_by_draw_ib: dict[str, dict[int, int]] = {}
        for draw_ib in ready_groups:
            group_index = group_of[draw_ib]
            vg_offsets[draw_ib] = group_base[group_index] + local_offsets[draw_ib]
            vg_maps[draw_ib] = {
                local: group_base[group_index] + slot
                for local, slot in local_maps[draw_ib].items()
            }
            # t75：把逐**局部**骨槽权重换算到**全局**槽位（通道骨选择用）。
            local_weights = ready_groups[draw_ib].get("slot_weights")
            global_weights: dict[int, int] = {}
            if local_weights is not None:
                for local, slot in local_maps[draw_ib].items():
                    if not (0 <= int(local) < len(local_weights)):
                        continue
                    weight = float(local_weights[int(local)])
                    # 合成/损坏的 Blend 权重可能是 NaN/Inf：只做**记账**，一律按 0
                    # 计入，绝不因为一个坏权重让整批导入失败。
                    if not math.isfinite(weight) or weight <= 0:
                        continue
                    global_slot = group_base[group_index] + int(slot)
                    global_weights[global_slot] = global_weights.get(
                        global_slot, 0
                    ) + int(round(weight))
            slot_weights_by_draw_ib[draw_ib] = global_weights

        # ------------------------------------------------------------------
        # t75：共享骨图 + 通道骨判定（唯一判定口径，导入期一次性算好并落盘）
        # ------------------------------------------------------------------
        # 「全组共享骨」（旧 `_merged_group_pose_anchor_slot` 的交集判据）会被
        # 一个与谁都不共骨的部件整组打空（叶瞬光01 G0 的 8c8de427）⇒ 按**共享骨图
        # 的连通分量**讨论：分量内仍存在 11 个共用槽（含槽 0，五件引用）。
        # 判定结果（通道全局槽位 / 本地下标 / 共享件数 / 跨部件顶点权重 / 理由 /
        # 诊断）随 VGMap 一起写回 json，供导出侧直接消费与人工复核。
        channel_components = [
            {
                "draw_ib": draw_ib,
                "vg_map": vg_maps[draw_ib],
                "vg_count": int(ready_groups[draw_ib]["vg_count"]),
                "vg_offset": vg_offsets[draw_ib],
                "skeleton_group": group_of[draw_ib],
                "slot_weights": slot_weights_by_draw_ib[draw_ib],
            }
            for draw_ib in sorted(vg_maps)
        ]
        channel_plan = select_channel_plan(channel_components)
        channel_plan_digest = channel_plan_digest_value(channel_plan)

        # 写回工作空间 json + 复制 palette 缓存（组内所有子网格写相同结果）
        written = 0
        for draw_ib, group in ready_groups.items():
            vg_map = vg_maps.get(draw_ib, {})
            if not vg_map:
                continue
            vg_offset = vg_offsets[draw_ib]
            vg_count = group["vg_count"]
            skeleton_group = group_of[draw_ib]

            for unique_str in group["members"]:
                json_path = group["json_paths"][unique_str]
                submesh_json = JsonUtils.LoadFromFile(json_path)
                if not isinstance(submesh_json, dict):
                    continue

                submesh_json["VGCount"] = vg_count
                submesh_json["VGOffset"] = vg_offset
                submesh_json["VGMap"] = {str(k): int(v) for k, v in sorted(vg_map.items())}
                # 算法版本：快路径幂等判定依据；策略变更时递增版本使旧缓存自动失效
                submesh_json["VGMapAlgorithmVersion"] = _ZZMI_VG_MAP_ALGORITHM_VERSION
                # 编号域：cross_group = 骨骼池槽位；group = 历史分组编号。快路径靠它
                # 区分两种 VGMap——只看「有 VGMap」会把历史编号当成合并成果。
                submesh_json["SkeletonMergeMode"] = merge_mode
                # t75 通道计划：唯一判定口径的产物 + 复核素材。缺失/版本不符时
                # `_zzmi_cache_intact` 拒绝复用（显式重建，不静默退化）。
                submesh_json["ChannelPlanVersion"] = ZZMI_CHANNEL_PLAN_VERSION
                submesh_json["ChannelPlan"] = _serialize_channel_record(
                    channel_plan.get(draw_ib)
                )
                submesh_json["ChannelPlanDigest"] = channel_plan_digest
                # t75/t78：骨骼**身份**摘要与"当帧矩阵"指纹必须分开存（t78 #3）。
                # F4（复核发现）口径更正：`BoneIdentityDigest` 哈希的是
                # `{本地下标: 全局槽位}`，而槽位由当帧 palette 矩阵的 bitwise 去重
                # 派生 ⇒ 它是**当帧矩阵的派生量**，不是帧不变量、**不是**骨名/编号
                # 身份。`BoneIdentityBasis` 因此必须自述为 `local_slot_map`（弱代理），
                # 不得让读者以为它是骨名身份；骨名真值（bone_ids）尚未接入生产链路。
                submesh_json["BoneIdentityVersion"] = ZZMI_BONE_IDENTITY_VERSION
                submesh_json["BoneIdentityDigest"] = bone_identity_digest(vg_map)
                submesh_json["BoneIdentityBasis"] = "local_slot_map"
                submesh_json["BoneIdentityBasisNote"] = (
                    "local_slot_map：槽位号由当帧 palette 矩阵 bitwise 去重派生，"
                    "**不是**骨名/编号身份、也不是帧不变量（bone_ids 未接入生产链路）"
                )
                palette_bytes = None
                try:
                    if group.get("palette_path"):
                        with open(group["palette_path"], "rb") as palette_file:
                            palette_bytes = palette_file.read()
                except OSError:
                    palette_bytes = None
                submesh_json["BoneMatrixFrameDigest"] = same_frame_matrix_digest(
                    palette_bytes
                )
                submesh_json["BoneMatrixFrameDigestNote"] = (
                    "当帧矩阵指纹——只证明同一次抓帧，不是骨骼身份依据（t78）"
                )
                submesh_json["ChannelPlanSlotWeights"] = {
                    str(slot): int(weight)
                    for slot, weight in sorted(
                        slot_weights_by_draw_ib.get(draw_ib, {}).items()
                    )
                }
                if frame_analysis_signature:
                    submesh_json["FrameAnalysisLogSignature"] = frame_analysis_signature
                # 骨架分组（渲染 cb1 对象变换配对）：导出侧把 deform pass 换绑到本组
                # ResourceZZMergedSkeleton_G<N>；VGMap/VGOffset 为全局骨骼编号（组基址拼接）
                submesh_json["SkeletonGroup"] = skeleton_group
                # 导出侧守卫元数据：本部件 deform pass 的 draw 序号（组内时序校验——
                # 合并网格必须由组内最后一个 deform draw 蒙皮，否则其它部件骨骼
                # 还是上一帧内容）+ 原部件顶点数（渲染 vb1 换绑判定）
                submesh_json["DeformDrawIndex"] = int(group["draw_index"])
                submesh_json["OriginalVertexCount"] = int(
                    group.get("original_vertex_counts", {}).get(unique_str, 0) or 0
                )

                # palette / CB1 / JSON 作为同一可回滚文件事务发布。重建时始终
                # 刷新当前源；任一文件失败都不得留下新旧混合状态或计入 written。
                try:
                    bare_name = unique_str.split(".", 1)[-1]
                    submesh_dir = os.path.dirname(os.path.dirname(json_path))
                    runtime_dir = os.path.join(submesh_dir, "ModImpRuntime")
                    dest_name = f"{bare_name}-BoneMatrix.buf"
                    dest_path = os.path.join(runtime_dir, dest_name)
                    submesh_json["BoneMatrixFileName"] = dest_name
                    cache_entries = [{
                        "source_path": group["palette_path"],
                        "dest_path": dest_path,
                        "vg_count": vg_count,
                        "min_size": 4,
                    }]

                    # 对象变换 CB 是分组稳定性的缓存来源；存在源时同样原子刷新。
                    if group.get("cb1_path"):
                        cb1_dest_name = f"{bare_name}-ObjectCB1.buf"
                        cb1_dest_path = os.path.join(runtime_dir, cb1_dest_name)
                        cache_entries.append({
                            "source_path": group["cb1_path"],
                            "dest_path": cb1_dest_path,
                            "vg_count": 0,
                            "min_size": 4,
                        })
                        submesh_json["ObjectCB1FileName"] = cb1_dest_name
                        submesh_json["ObjectCB1CacheValid"] = bool(
                            group.get("cb1_cache_valid", False)
                        )
                        # 窗口起点随缓存一起写回：ObjectCB1 保存的是 dump 原始字节
                        # （可能是多对象共享数组），无 dump 重建时必须按同一窗口
                        # 还原同一个对象变换，否则分组会在重建后漂移。
                        submesh_json["ObjectCB1FirstConstant"] = int(
                            group.get("cb1_first_constant", 0) or 0
                        )
                    else:
                        # 显式沉淀“本次没有有效 CB1”；缓存读取必须忽略可能残留的
                        # 旧文件，才能复现 dump 路径的独立分组语义。
                        submesh_json.pop("ObjectCB1FileName", None)
                        submesh_json.pop("ObjectCB1FirstConstant", None)
                        submesh_json["ObjectCB1CacheValid"] = False
                    EFMISkeletonMergeHelper._atomic_publish_skeleton_transaction(
                        cache_entries,
                        submesh_json,
                        json_path,
                    )
                except Exception as e:
                    print(f"[ZZMI骨骼合并] 提交运行时缓存/JSON 事务失败 {unique_str}: {e}")
                    continue

                written += 1

        success_message = (
            f"已为 {written} 个子网格生成骨骼合并数据"
            f"（{len(ready_groups)} 个部件 / {len(group_members)} 个骨架组，"
            f"全局共 {total_slots} 槽）"
        )
        # 完整性报告：只要还有目标未处理（部件未生成 / 目标无法解析 / json 写回
        # 失败），就不能报告完整成功——部分完成必须大声暴露，否则调用方会把
        # 缺失 BoneMatrix 的部件当成已生成。
        failure_parts: list[str] = []
        if unprocessed_groups:
            detail = "、".join(
                f"{draw_ib}({reason})"
                for draw_ib, reason in sorted(unprocessed_groups.items())
            )
            failure_parts.append(f"{len(unprocessed_groups)} 个部件未生成: {detail}")
        if unresolved_targets:
            shown = unresolved_targets[:5]
            suffix = "…" if len(unresolved_targets) > 5 else ""
            failure_parts.append(
                f"{len(unresolved_targets)} 个目标无法解析: {'、'.join(shown)}{suffix}"
            )
        ready_member_total = sum(len(group["members"]) for group in ready_groups.values())
        if written < ready_member_total:
            failure_parts.append(
                f"{ready_member_total - written} 个子网格未生成"
                "（缓存发布或 json 写回失败）"
            )
        if failure_parts:
            return False, success_message + "；" + "；".join(failure_parts)
        return written > 0, success_message
