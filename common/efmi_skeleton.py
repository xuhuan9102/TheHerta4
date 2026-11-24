"""
EFMI（明日方舟：终末地）骨骼合并支持模块

数据来源：
1. SSMT 工作空间：子网格 json（`<drawib>-<index_count>-<first_index>` 目录）+ 子网格 json 内
   CategoryBufferList（Blend 类别 buffer 的 D3D11ElementList）+ 角色级
   `ComponentName_DrawCallIndexList.json`（子网格名 -> drawcall 索引列表）。
2. FrameAnalysis 帧提取 dump（3Dmigoto 原始捕获）：
   - `log.txt`：逐 draw 调用记录（DrawIndexedInstanced / VSSetConstantBuffers1 / VSSetShaderResources /
     3DMigoto Dumping Buffer 资源去重映射），含每个常量缓冲绑定的 first_constant/num_constants 窗口。
   - `deduped/`：去重后的实际数据文件（.buf 原始字节 / .txt 文本 dump）。

骨骼合并算法参照 EFMI-Tools（SpectrumQT）参考插件：
- `migoto_object_builder.get_skeleton_data`：取顶点阶段 num_constants==4096 的常量缓冲
  （instance config），其 first_constant 窗口第 6 个 float4 的 xy 分量（uint32 位型）为骨骼矩阵段偏移；
  骨骼矩阵存于 vs-t0（compute 蒙皮输出 u0 共享），每骨骼 12 floats（4x3 矩阵）。
- `build_merged_skeleton_vg_map`：跨子网格按骨骼矩阵内容和接触位置权重扩散去重，
  构建 local->global 的 vg_map / vg_offset / vg_count（矩阵硬门控 + 扩散确认，
  _DEDUP_ENABLED 控制总开关）。

产出：
- 每个子网格 json 写回 `VGMap` / `VGOffset` / `VGCount`（缓存，幂等；提取端重导可覆盖）。
- 骨骼池 buffer 复制到 `<submesh>/ModImpRuntime/<bare>-BoneMatrix.buf`（参照 NTEMI 缓存模式）。
- instance-config buffer 同时复制到 `<submesh>/ModImpRuntime/<bare>-InstanceConfig.buf`，
  并记录 first_constant 窗口；清理 VGMap、删除 dump 后可只靠工作空间原始文件重建。

多 LOD：LOD0 / LOD1 用**自己**的 FrameAnalysis dump 提取目录（Config/WorkPageTabs.json
的 tab 名即 LOD 名，每个 tab 的 Config/Tabs/<tabid>.json 记录自己的路径）读取原始
候选。v10：每 LOD 用**自己的 dump** 独立执行权重扩散去重
（槽位从 0 起），随后非基准 LOD 的整个编号空间**平移到基准 LOD 段之后**
（LOD0: 0..max0，LOD1: base 起）——两域不相交、全局唯一，跨 LOD **零共享零串扰**。
v9 投影（LOD1 顶点组引用 LOD0 基准槽位 + 运行时 full→lod BlendRemap 共享槽位）
实测爆炸：同帧 LOD0/LOD1 绘制对同一 component 只导入一次骨骼、仅允许更优 LOD
覆盖，LOD1 网格混用 LOD0 矩阵。跨 LOD 对应账本（build_cross_lod_correspondence）
仍写入 json（EFMILODCorrespondence / EFMILODReference / EFMILODGroupCount 等）
但不让 LOD1 直接借用 LOD0 槽位：开关开启时，它用于把 LOD0 最终去重分区镜像
约束到 LOD1 自己的槽位段，并在导入后为实际 Blender 物体配对、生成并执行
LOD0→LOD1 顶点组匹配节点；账本本身不充当物体顶点组映射表。
几何匹配不成功的 LOD1 部件（未进入部件配对或配对得分超
_CROSS_LOD_PART_IMPORT_SCORE_LIMIT）不生成 VGMap、json 写入
EFMILODProjectionSkipped=True，导入侧据此排除。CPU/无顶点组（GPU-PreSkinning=
false）子网格不参与骨骼合并，但投影模式下非基准 LOD 的 CPU 目标仍须接受
LOD0 几何匹配裁决（draw IB 同现判定）：匹配成功写 EFMILODProjectionMatched=True
（可导入、不生成 VGMap），匹配失败写 EFMILODProjectionSkipped=True（导入侧
排除）；导入侧对缺状态的非基准 LOD 目标 fail-closed 默认排除（旧缓存不会
自动补标记）。约束、过滤和自动匹配链均由 lod_group_projection 开关统一控制；
关闭后两侧只按各自 dump 独立去重。
"""

# 注解延迟求值（PEP 563）：避免类定义期解析 numpy.ndarray 等注解，
# 防止在 sys.modules['numpy'] 被 stub 的进程（如全量 unittest discover）中导入失败。
from __future__ import annotations

import os
import re
import shutil
import tempfile
import json
from collections import defaultdict

import numpy

from ..utils.json_utils import JsonUtils

# 每骨骼矩阵的 float 数（4x3）
_BONE_MATRIX_FLOATS = 12
# 每个骨骼段的 float4 数（256 骨骼 x 3 float4）
_BONE_SEGMENT_FLOAT4 = 256 * 3
# instance config 中骨骼段偏移所在的 float4 行（第 6 行）
_INSTANCE_CONFIG_BONE_OFFSET_ROW = 5
# 没有 LOD 前缀的身份默认按哪个 LOD 解析（与 common/workspace_helper.DEFAULT_LOD_NAME
# 同口径；本模块刻意不依赖 bpy，因此不直接 import 那个模块里的常量）。
_DEFAULT_LOD_NAME = "LOD0"
# 写回 json 的算法版本。旧版只保存 VGMap，没有扩散采样判据，不能继续
# 作为当前策略的幂等缓存使用；版本不匹配时 ensure_skeleton_data 自动重算。
# v15：全零骨骼仍不参与跨 Component 去重，但会保留独立稳定槽位，使 VGMap
# 始终完整覆盖 0..VGCount-1；旧版稀疏映射不能继续通过导入/导出完整性门控。
_VG_MAP_ALGORITHM_VERSION = 15
_MATRIX_AMBIGUITY_FLOOR = 1e-6

# 跨 LOD 原始候选对应层版本。它和 VGMap 算法版本分开记录，便于以后只调整
# 对应评分而不误把旧的运行时槽位当成新布局。
# v5: cross-LOD local correspondence is geometry-led; matrix is no longer a
# hard acceptance gate and is only a bounded secondary score term.
# v7: 目标侧 VGMap 编号空间修正——不再沿用参考侧组 id（跨 LOD 串扰根因：参考号
# 在目标池中命中错误骨骼段/空洞）。
# v8: 完全独立 + 分段平移（2026-08-27 用户拍板）：每 LOD 用自身 dump 独立去重，
# 非基准 LOD 的整个编号空间平移到基准 LOD 段之后（LOD0: 0..max0，LOD1: base 起），
# 两域不相交；build_lod_maps_from_reference 已移除，跨 LOD 对应仅存诊断账本。
# v9（2026-08-28 用户拍板：恢复投影）：非基准 LOD 的 VGMap **投影到基准分区**——
# 匹配局部映射到 LOD0 分区的全局槽位（LOD0 中已合并的两个组，其 LOD1 对应组
# 也落在同一槽位 = 继承合并）；未匹配局部按恒等暗影槽位（part_offset + local），
# 对应参考插件 remap 的 get(i,i) 回退。跨 LOD 顶点组编号统一（单池单编号空间），
# LOD 骨骼布局差异由 full→lod remap 在运行时消化。
# v10（**撤销 v9 共享槽位投影**）：实测 LOD1 模型爆炸根因 = LOD1 的
# 顶点组编号与 LOD0 共用同一批槽位，而运行时 MergedSkeleton_Apply 对同一
# component 每帧只导入一次骨骼（Instance_UpdateFrame 门控 + 仅允许更优
# $lod_level 覆盖）：同帧先 LOD0 后 LOD1 时，LOD1 网格读取的是 LOD0 已导入的
# 矩阵（L0/L1 两侧矩阵数据不同）→ 爆炸。恢复 v8 语义：每 LOD 独立去重（自身
# 槽位从 0 起），非基准 LOD 编号空间整体平移到基准段之后（LOD0 0..370、
# LOD1 371 起），两域不相交、全局唯一；每个 LOD 绘制入口挂**自己的** component
# 槽位段，运行时把**当前 LOD 自己的矩阵**写入自己的槽位（无跨 LOD remap 共享）。
# v11（修复 v10 两点缺陷）：两边去重不一致（L0 5/6 合并，
# L1 对应 11..17 却各占槽位）——以 L0 最终合并组为标签约束 L1 去重（不同标签
# 断边）+ 强制同标签共用最小槽位，L0 合并 ⇒ L1 对应必合并；L1 可多于 L0
# （无对应组保持独立槽位）。
# v12（暂存）：曾把两侧值域压缩重排（L0 [0,k0)、L1 从 k0 起）——实测游戏内
# 直接乱掉（L1 值域侵入 L0 声明段区域）。v13 撤销：**两侧都不做值域重排**，
# 恢复 v10 平移口径（值域 = 去重原生槽位 + 整段平移，跨 LOD 零共享），
# 仅保留 v11 镜像约束。
_CROSS_LOD_LAYOUT_VERSION = 13

# 跨 LOD 部件配对得分上限：部件双方已进入一对一配对（中心距通过 0.75 硬门控）
# 但得分仍超过该值时视作“几何匹配不成立”——用于分组投影模式的导入过滤，
# 该部件同样不导入。不参与部件内部 VG 级对应/去重的判定。
# 得分构成：0.5*(对称最近邻点云中位距离) + bbox 间隙*0.25 + 中心距*0.10
#   + 尺寸误差*0.05 + 部件数对数项(<=0.20)。
# 真实同部件跨 LOD 的表面间距通常 <=0.05（简化 LOD 亦在 ~0.1 内）；
# 错配且中心恰好相近（如贴身衣物/邻近附件）的部件往往 >=0.3。
_CROSS_LOD_PART_IMPORT_SCORE_LIMIT = 0.30
# LOD0/LOD1 may be captured in different poses or after mesh simplification;
# this is the scale used for the secondary matrix score. Same-LOD deduplication
# continues to use its independent 1e-3 hard gate.
_CROSS_LOD_MATRIX_SCORE_SCALE = 1.5

# 跨子网格骨骼去重总开关。
# 分层判据（矩阵硬门控 + 权重扩散确认）：矩阵 diff >= match_tolerance 永不合并；
# bitwise 相同在缺少扩散字段时兼容直接合并；有扩散字段也要通过接触位置
# 权重一致性确认；近似矩阵同样做扩散确认。
# 注意：2026-08 曾实测误判（390/393 案例）而临时整体关闭；后查明当时观测数据
# 被陈旧缓存污染（网格与 json 账本不一致），现已随官方运行时架构重写一并恢复。
# 其后的"多维度投票"判据（几何维度可推翻矩阵不一致）经"测试"工作空间 08-10 dump
# 实测产生 42 组矩阵不可兼容的误并（195 组中），已废止并回到分层判据。
# 再遇误判先查数据一致性（清除 VGMap 缓存重导），再考虑关开关。
# 变更策略后，VGMapAlgorithmVersion 会让旧结果自动失效；Position/Blend **数据**
# 变更由 _efmi_cache_intact 的源数据指纹（EFMIVGMapSourceFingerprint，
# mtime_ns+size，F1/t1）自动失效整批重算——不再需要手动清缓存。
# 也可用面板的清理按钮提前删除缓存。
# 权重扩散去重是 EFMI 合并模式的默认行为。关闭只用于诊断/回滚；关闭后
# build_vg_maps 仍返回安全的恒等映射，不会改变原始蒙皮。
_DEDUP_ENABLED = True


def efmi_ib_key(unique_str: str) -> str:
    """从 unique_str 提取 IB 判定键（首段 draw IB hash，C8/P6 同源实现）。

    ``_component_lifetime_domains``（I1 生命周期域）、``ui/universal/efmi.py``
    的 same-IB 折叠判定（``_baseline_draw_key`` 的 match_draw_ib 缺省回退）与
    I2 距离集共用本函数作为 IB 键的唯一规范化来源：同一 draw 的各 LOD 版本
    （LOD0.x-y-z / LOD1.x-y-z）必须得到同一 IB 键，否则折叠判定/去重域/导出
    校验三视图漂移会漏判折叠、残留未注册段。

    语义：去掉 LOD 前缀（``LOD0.``/``LOD1.``），取剩余 bare 名首段
    （``-`` 分隔第一段 = draw IB hash）；无 ``-`` 时 whole-string 即 IB
    （单 LOD 工作空间，域恒单元素，去重行为不变）。
    """
    bare = str(unique_str or "")
    if bare.upper().startswith("LOD") and "." in bare:
        bare = bare.split(".", 1)[1]
    return bare.split("-")[0] if "-" in bare else bare


class EFMILogParser:
    """解析 FrameAnalysis/log.txt，提供 draw 调用与资源绑定查询。"""

    _DRAW_PREFIX_RE = re.compile(r"^(\d{6}) (.*)$")
    _DUMP_RE = re.compile(r"^3DMigoto Dumping Buffer (.+) -> (.+)$")
    _IB_FILE_RE = re.compile(r"^(\d{6})-ib=([0-9a-f]{8})")
    _CB_BIND_RE = re.compile(
        r"^(\d+): resource=0x[0-9A-Fa-f]+ hash=([0-9a-f]{8}) "
        r"first_constant=(\d+) num_constants=(\d+)$"
    )
    _SRV_BIND_RE = re.compile(
        r"^(\d+): view=0x[0-9A-Fa-f]+ resource=0x[0-9A-Fa-f]+ hash=([0-9a-f]{8})$"
    )
    _DRAW_CALL_RE = re.compile(
        r"^DrawIndexedInstanced\(IndexCountPerInstance:(\d+), InstanceCount:(\d+), "
        r"StartIndexLocation:(\d+), BaseVertexLocation:(\d+), StartInstanceLocation:(\d+)\)$"
    )

    def __init__(self, log_path: str):
        self.log_path = log_path
        self.base_dir = os.path.dirname(os.path.abspath(log_path))
        # draw_index -> DrawCallInfo
        self.draw_calls: dict[str, dict] = {}
        # (draw_index, stage, slot) -> {"hash", "first_constant", "num_constants"}
        self.cb_bindings: dict[tuple[str, str, int], dict] = {}
        # (draw_index, stage, slot) -> {"hash"}
        self.srv_bindings: dict[tuple[str, str, int], dict] = {}
        # 逻辑文件名（根目录 dump 文件名）-> deduped 实际路径
        self.dump_map: dict[str, str] = {}
        self._parse()

    def _parse(self):
        if not os.path.isfile(self.log_path):
            raise FileNotFoundError(f"FrameAnalysis log 不存在: {self.log_path}")

        current_draw = ""
        pending_cb = None  # (stage, slot) 等待下一行资源描述
        pending_srv = None

        with open(self.log_path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                line = line.rstrip("\n")
                if not line:
                    continue

                match = self._DRAW_PREFIX_RE.match(line)
                if not match:
                    # 无 draw 前缀的行：资源描述行（跟随绑定行），在 pending 状态下消费。
                    stripped = line.strip()
                    if pending_cb is not None and stripped:
                        desc = self._CB_BIND_RE.match(stripped)
                        if desc:
                            slot = int(desc.group(1))
                            self.cb_bindings[(pending_cb[0], pending_cb[1], slot)] = {
                                "hash": desc.group(2),
                                "first_constant": int(desc.group(3)),
                                "num_constants": int(desc.group(4)),
                            }
                        pending_cb = None
                        continue
                    if pending_srv is not None and stripped:
                        desc = self._SRV_BIND_RE.match(stripped)
                        if desc:
                            slot = int(desc.group(1))
                            self.srv_bindings[(pending_srv[0], pending_srv[1], slot)] = {
                                "hash": desc.group(2),
                            }
                        pending_srv = None
                        continue
                    continue
                draw_index, payload = match.group(1), match.group(2)

                # 常量缓冲绑定（VSSetConstantBuffers1 等）
                cb_match = re.match(r"^(V|P|C|G|H|D)SSetConstantBuffers1\(StartSlot:(\d+),", payload)
                if cb_match:
                    stage = cb_match.group(1)
                    slot = int(cb_match.group(2))
                    pending_cb = (draw_index, stage, slot)
                    pending_srv = None
                    continue

                # 着色器资源绑定（VSSetShaderResources 等）
                srv_match = re.match(r"^(V|P|C|G|H|D)SSetShaderResources\(StartSlot:(\d+),", payload)
                if srv_match:
                    stage = srv_match.group(1)
                    slot = int(srv_match.group(2))
                    pending_srv = (draw_index, stage, slot)
                    pending_cb = None
                    continue

                # 资源描述行（跟随绑定行，但带 draw 前缀的少见情形）
                if pending_cb is not None:
                    desc = self._CB_BIND_RE.match(payload.strip())
                    if desc:
                        slot = int(desc.group(1))
                        self.cb_bindings[(pending_cb[0], pending_cb[1], slot)] = {
                            "hash": desc.group(2),
                            "first_constant": int(desc.group(3)),
                            "num_constants": int(desc.group(4)),
                        }
                    pending_cb = None
                    continue

                if pending_srv is not None:
                    desc = self._SRV_BIND_RE.match(payload.strip())
                    if desc:
                        slot = int(desc.group(1))
                        self.srv_bindings[(pending_srv[0], pending_srv[1], slot)] = {
                            "hash": desc.group(2),
                        }
                    pending_srv = None
                    continue

                # draw 调用
                draw_match = self._DRAW_CALL_RE.match(payload)
                if draw_match:
                    self.draw_calls[draw_index] = {
                        "index_count": int(draw_match.group(1)),
                        "instance_count": int(draw_match.group(2)),
                        "start_index": int(draw_match.group(3)),
                        "base_vertex": int(draw_match.group(4)),
                        "start_instance": int(draw_match.group(5)),
                    }
                    current_draw = draw_index
                    continue

                # 资源 dump 映射
                dump_match = self._DUMP_RE.match(payload)
                if dump_match:
                    src_name = os.path.basename(dump_match.group(1))
                    dst_path = dump_match.group(2)
                    if src_name not in self.dump_map:
                        self.dump_map[src_name] = dst_path
                    continue

    # ------------------------------------------------------------------
    # 查询接口
    # ------------------------------------------------------------------

    def get_vs_cb(self, draw_index: str, slot: int) -> dict | None:
        """获取 draw 调用顶点阶段指定槽位的常量缓冲绑定。"""
        return self.cb_bindings.get((draw_index, "V", slot))

    def get_instance_config_cb(self, draw_index: str) -> dict | None:
        """获取 instance config 常量缓冲（num_constants == 4096 的顶点阶段 cb）。"""
        best = None
        for (idx, stage, _slot), binding in self.cb_bindings.items():
            if idx != draw_index or stage != "V":
                continue
            if binding.get("num_constants") == 4096:
                best = binding
                break
        return best

    def get_vs_t0(self, draw_index: str) -> str | None:
        """获取顶点阶段 slot0 纹理（骨骼池）的 hash。"""
        binding = self.srv_bindings.get((draw_index, "V", 0))
        return binding.get("hash") if binding else None

    def get_deduped_path(self, logical_filename: str) -> str | None:
        """按根目录逻辑文件名（如 000015-vs-cb1=...buf）查 deduped 实际路径。

        log.txt 里记录的 deduped 绝对路径可能是提取时的临时缓存路径（如 E:\\SSMT4缓存文件夹\\...），
        dump 被挪动后失效。因此：先按 log 记录的路径，失效时用文件名在 dump 目录的
        deduped/ 子目录兜底定位（deduped 文件名是内容 hash，唯一）。
        """
        dst = self.dump_map.get(logical_filename)
        for candidate in self._deduped_candidates(dst):
            if os.path.isfile(candidate):
                return candidate
        # 兜底：按前缀匹配（同 hash 资源可能对应多个逻辑名）
        prefix = logical_filename.split("=", 1)[0] + "="
        for src, dst2 in self.dump_map.items():
            if src.startswith(prefix):
                for candidate in self._deduped_candidates(dst2):
                    if os.path.isfile(candidate):
                        return candidate
        return None

    def _deduped_candidates(self, dst_path: str | None) -> list[str]:
        """生成 deduped 候选路径：log 记录的原路径 + dump 目录 deduped/ 下的同名文件。"""
        candidates = []
        if dst_path:
            candidates.append(dst_path)
            basename = os.path.basename(dst_path)
            if basename:
                candidates.append(os.path.join(self.base_dir, "deduped", basename))
        return candidates

    def find_drawcalls_by_ib(
        self,
        draw_ib: str,
        index_count: int | None = None,
        first_index: int | None = None,
    ) -> list[str]:
        """按 IB hash（+可选 index_count/first_index）反查 drawcall 索引列表。

        从 dump 文件名（`{idx}-ib=<hash>...`）解析 drawcall -> ib hash 映射，
        再按 DrawIndexedInstanced 的 index_count/first_index 过滤。
        用于 ComponentName_DrawCallIndexList.json 缺失/被重置时的兜底。
        """
        draw_ib = str(draw_ib or "").strip().lower()
        if not draw_ib:
            return []
        candidates = []
        for src in self.dump_map.keys():
            match = self._IB_FILE_RE.match(src)
            if match and match.group(2) == draw_ib:
                candidates.append(match.group(1))
        candidates = sorted(set(candidates))
        if index_count is None and first_index is None:
            return candidates

        matched = []
        for idx in candidates:
            dc = self.draw_calls.get(idx)
            if dc is None:
                continue
            if index_count is not None and dc.get("index_count") != index_count:
                continue
            if first_index is not None and dc.get("start_index") != first_index:
                continue
            matched.append(idx)
        # 精确匹配失败时回退到全部候选（提取切分可能与 draw 参数不完全一致）
        return matched if matched else candidates


class EFMIBoneMapBuilder:
    """构建 EFMI 子网格的骨骼合并映射（vg_map / vg_offset / vg_count）。"""

    def __init__(self, parser: EFMILogParser, blend_formats: dict[str, dict] | None = None):
        self.parser = parser
        # unique_str -> 子网格 Blend 类别解析信息（由调用方提供，避免重复解析 json）
        self.blend_formats = blend_formats or {}

    # ------------------------------------------------------------------
    # 子网格 blendindices 读取
    # ------------------------------------------------------------------

    @staticmethod
    def parse_blendindices_from_buf(blend_buf_path: str, element_info: dict) -> numpy.ndarray:
        """从工作空间 Blend.buf 读取 BLENDINDICES 局部索引数组。

        element_info: {
            "byte_offset": BLENDINDICES 在顶点行内的字节偏移,
            "byte_width": 元素总字节数,
            "stride": 顶点行总字节数,
            "np_type": numpy dtype 字符串（如 'u1'/'u4'）,
            "component_count": 通道数,
        }
        返回 (vertex_count, component_count) 的 uint32 数组。
        """
        if not os.path.isfile(blend_buf_path):
            raise FileNotFoundError(f"Blend buffer 不存在: {blend_buf_path}")

        stride = int(element_info.get("stride", 0) or 0)
        byte_offset = int(element_info.get("byte_offset", 0) or 0)
        byte_width = int(element_info.get("byte_width", 0) or 0)
        np_type = element_info.get("np_type", "")
        component_count = int(element_info.get("component_count", 4) or 4)
        if stride <= 0 or byte_width <= 0 or not np_type:
            raise ValueError(f"无效的 BLENDINDICES 元素信息: {element_info}")

        file_size = os.path.getsize(blend_buf_path)
        if file_size % stride != 0:
            raise ValueError(
                f"Blend buffer 大小与 stride 不对齐: {blend_buf_path} "
                f"({file_size} % {stride})"
            )
        vertex_count = file_size // stride

        raw = numpy.fromfile(blend_buf_path, dtype=numpy.uint8)
        rows = raw.reshape(vertex_count, stride)
        element_bytes = rows[:, byte_offset:byte_offset + byte_width]
        indices = numpy.frombuffer(
            element_bytes.tobytes(), dtype=numpy.dtype(np_type)
        ).reshape(vertex_count, component_count)
        return indices.astype(numpy.uint32, copy=False)

    @staticmethod
    def blend_index_sentinel(np_type: str) -> int:
        """BLENDINDICES 数据格式对应的无效通道哨兵值（uint32 空间）。

        解析器把索引统一 astype(uint32)：SINT 的 -1 经无符号转换后同样变成全 1
        位（0xFFFFFFFF），因此 i4 与 u4 共用同一哨兵。
        """
        np_type = str(np_type or "").strip().lower()
        if np_type in {"u1", "i1"}:
            return 0xFF
        if np_type in {"u2", "i2"}:
            return 0xFFFF
        return 0xFFFFFFFF  # u4 / i4；有符号类型的全部负值由 valid_blend_channels 过滤

    @staticmethod
    def parse_blend_layout(submesh_json_dict: dict) -> dict | None:
        """解析 Blend 类别的 BLENDINDICES + BLENDWEIGHTS 布局。

        返回 {
            "stride": 顶点行总字节数,
            "bi_offset": BLENDINDICES 字节偏移,
            "bi_np": numpy dtype 字符串（'u1'/'u2'/'u4'/'i1'/'i2'/'i4'）,
            "bi_channels": 索引通道数,
            "bw_offset": BLENDWEIGHTS 字节偏移（无权重元素时为 None）,
            "bw_np": 权重 dtype（无权重元素时为 None）,
            "bw_channels": 权重通道数,
            "bw_div": 权重归一化除数,
        }；无法解析返回 None。
        """
        blend_stride = 0
        bi_offset = None
        bi_layout = None
        bw_offset = None
        bw_layout = None
        bw_div = 1.0
        for category_buffer in submesh_json_dict.get("CategoryBufferList", []):
            elements = category_buffer.get("D3D11ElementList", [])
            is_blend = any(
                str(e.get("Category", "") or "").strip().lower() == "blend"
                for e in elements
            )
            if not is_blend:
                continue
            off = 0
            for element in elements:
                width = int(element.get("ByteWidth", 0) or 0)
                blend_stride += width
                semantic = str(element.get("SemanticName", "") or "").upper()
                fmt = str(element.get("Format", "") or "").upper()
                if semantic == "BLENDINDICES":
                    bi_offset = off
                    bi_layout = EFMIBoneMapBuilder._blend_index_layout_entry(fmt)
                elif semantic.startswith("BLENDWEIGHT"):
                    bw_offset = off
                    if fmt == "R16G16B16A16_UNORM":
                        bw_layout, bw_div = ("u2", 4), 65535.0
                    elif fmt == "R32G32B32A32_FLOAT":
                        bw_layout, bw_div = ("f4", 4), 1.0
                    elif fmt == "R32G32_FLOAT":
                        bw_layout, bw_div = ("f4", 2), 1.0
                    elif fmt == "R8G8B8A8_UNORM":
                        bw_layout, bw_div = ("u1", 4), 255.0
                off += width
            break

        if blend_stride <= 0 or bi_offset is None or not bi_layout:
            return None
        return {
            "stride": blend_stride,
            "bi_offset": bi_offset,
            "bi_np": bi_layout[0],
            "bi_channels": bi_layout[1],
            "bw_offset": bw_offset,
            "bw_np": bw_layout[0] if bw_layout else None,
            "bw_channels": bw_layout[1] if bw_layout else 0,
            "bw_div": bw_div,
        }

    @staticmethod
    def _blend_index_layout_entry(fmt: str) -> tuple[str, int] | None:
        layout = {
            "R8G8B8A8_UINT": ("u1", 4),
            "R8G8_UINT": ("u1", 2),
            "R8_UINT": ("u1", 1),
            "R8G8B8A8_SINT": ("i1", 4),
            "R8G8_SINT": ("i1", 2),
            "R8_SINT": ("i1", 1),
            "R16G16B16A16_UINT": ("u2", 4),
            "R16G16_UINT": ("u2", 2),
            "R16_UINT": ("u2", 1),
            "R16G16B16A16_SINT": ("i2", 4),
            "R16G16_SINT": ("i2", 2),
            "R16_SINT": ("i2", 1),
            "R32G32B32A32_UINT": ("u4", 4),
            "R32G32_UINT": ("u4", 2),
            "R32_UINT": ("u4", 1),
            "R32G32B32A32_SINT": ("i4", 4),
            "R32G32_SINT": ("i4", 2),
            "R32_SINT": ("i4", 1),
        }
        return layout.get(fmt)

    @staticmethod
    def layout_element_info(blend_layout: dict) -> dict:
        """把 parse_blend_layout 的布局转成 parse_blendindices_from_buf 的元素信息。"""
        return {
            "byte_offset": int(blend_layout["bi_offset"]),
            "byte_width": int(blend_layout["bi_channels"])
            * numpy.dtype(blend_layout["bi_np"]).itemsize,
            "stride": int(blend_layout["stride"]),
            "np_type": blend_layout["bi_np"],
            "component_count": int(blend_layout["bi_channels"]),
        }

    @staticmethod
    def parse_blendweights_from_buf(
        blend_buf_path: str, blend_layout: dict | None
    ) -> numpy.ndarray | None:
        """从 Blend.buf 读取 BLENDWEIGHTS 数组（(vertex_count, bw_channels) float32）。

        blend_layout 由 parse_blend_layout 产出；布局缺失或没有 BLENDWEIGHTS
        元素时返回 None（调用方按“每顶点第一索引权重=1”兜底）。
        """
        if not blend_layout or blend_layout.get("bw_offset") is None or not blend_layout.get("bw_np"):
            return None
        if not os.path.isfile(blend_buf_path):
            raise FileNotFoundError(f"Blend buffer 不存在: {blend_buf_path}")

        stride = int(blend_layout["stride"])
        raw = numpy.fromfile(blend_buf_path, dtype=numpy.uint8)
        if len(raw) % stride != 0:
            raise ValueError(
                f"Blend buffer 大小与 stride 不对齐: {blend_buf_path} "
                f"({len(raw)} % {stride})"
            )
        vertex_count = len(raw) // stride
        rows = raw.reshape(vertex_count, stride)
        bw_np, bw_channels = blend_layout["bw_np"], int(blend_layout["bw_channels"])
        bw_byte_width = bw_channels * numpy.dtype(bw_np).itemsize
        weights = numpy.frombuffer(
            rows[:, blend_layout["bw_offset"]:blend_layout["bw_offset"] + bw_byte_width].tobytes(),
            dtype=numpy.dtype(bw_np),
        ).reshape(vertex_count, bw_channels).astype(numpy.float32) / float(blend_layout["bw_div"])
        return weights

    @staticmethod
    def cache_file_size_ok(cache_path: str, vg_count: int) -> bool:
        """骨骼缓存文件大小合理性：float32 流（4 字节对齐）且 >= vg_count * 48。

        48 字节 = 每骨骼 4x3 float32。EFMI 缓存是整池拷贝（远大于下限），
        ZZMI 缓存是 palette 拷贝（通常等于下限）；被截断/损坏的文件都会
        在这里被判不通过，由写回阶段重新复制。
        """
        try:
            size = os.path.getsize(cache_path)
        except OSError:
            return False
        return size % 4 == 0 and size >= int(vg_count) * _BONE_MATRIX_FLOATS * 4

    @staticmethod
    def valid_blend_channels(
        blend_indices: numpy.ndarray,
        element_info: dict,
        blend_weights: numpy.ndarray | None = None,
    ) -> numpy.ndarray:
        """返回 BLENDINDICES 数组的有效通道布尔掩码（与索引同形状）。

        有效通道 = 索引不是该数据格式的哨兵值（0xFF/0xFFFF/0xFFFFFFFF）且
        对应权重 > 0。没有 BLENDWEIGHTS 元素时按“每顶点第一索引权重=1、其余
        通道权重=0”兜底（与导入侧 mesh_create_helper 的默认权重语义一致）。
        """
        indices = numpy.asarray(blend_indices)
        if indices.ndim == 1:
            indices = indices.reshape(-1, 1)
        np_type = str(element_info.get("np_type", "u4") or "u4").strip().lower()
        sentinel = EFMIBoneMapBuilder.blend_index_sentinel(np_type)
        if blend_weights is not None:
            weights = numpy.asarray(blend_weights, dtype=numpy.float32)
            if weights.ndim == 1:
                weights = weights.reshape(-1, 1)
        else:
            # 无 BLENDWEIGHTS 元素：按“每顶点第一索引权重=1、其余通道=0”兜底
            weights = numpy.zeros((indices.shape[0], indices.shape[1]), dtype=numpy.float32)
            weights[:, 0] = 1.0

        mask = numpy.zeros(indices.shape, dtype=bool)
        for channel in range(indices.shape[1]):
            weight_col = weights[:, channel] if channel < weights.shape[1] else weights[:, 0]
            column = indices[:, channel]
            if np_type.startswith("i"):
                # SINT 经 uint32 转换后，任何负值都落在高位；-1 只是其中一种
                # 无效标记，这里把全部负数回绕值一并过滤。
                index_valid = column < 0x80000000
            else:
                index_valid = column != sentinel
            mask[:, channel] = (
                index_valid
                & (weight_col > 0)
                & numpy.isfinite(weight_col)
            )
        return mask

    @staticmethod
    def compute_driven_centroids(
        position_buf_path: str,
        blend_buf_path: str,
        submesh_json_dict: dict,
    ) -> dict[int, numpy.ndarray]:
        """计算每个局部骨骼的驱动签名和权重扩散采样。

        返回: {local_vg_id(int): 加权质心 numpy.ndarray(3)}（绑定姿态坐标）。
        """
        signatures = EFMIBoneMapBuilder.compute_driven_signatures(
            position_buf_path, blend_buf_path, submesh_json_dict
        )
        return {
            local: sig["centroid"] for local, sig in signatures.items()
        }

    @staticmethod
    def compute_driven_signatures(
        position_buf_path: str,
        blend_buf_path: str,
        submesh_json_dict: dict,
        *,
        blend_data: tuple | None = None,
        use_native_normals: bool = False,
    ) -> dict[int, dict]:
        """计算每个局部骨骼的"驱动签名"（质心回退 + 权重扩散确认）。

        返回 {local_vg_id(int): {
            "centroid": 加权质心(3,),
            "bbox_min": 包围盒最小(3,),
            "bbox_max": 包围盒最大(3,),
            "vertex_count": 驱动顶点数,
            "diffusion_points": 正权重扩散采样点,
            "diffusion_weights": 对应原始权重,
            "diffusion_normals": 点云局部 PCA 表面法向（不可判定时为 NaN）
        }}（绑定姿态坐标）。

        原理：同一骨骼跨部件驱动时，绑定姿态空间中的权重扩散场会在接触表面
        保持一致；整体质心可能因为“巨大的平面 + 散落物体”而完全不同，不能
        单独作为判据。不同骨骼即使几何相邻，接触位置的原始权重通常不一致。
        无 BLENDWEIGHTS 元素的类型按"每顶点第一索引权重=1"处理。
        """
        empty = {}
        if not os.path.isfile(position_buf_path) or not os.path.isfile(blend_buf_path):
            return empty

        # ---- Position 布局（Category=Position 元素，POSITION 在 offset 0，float32 x3）----
        pos_stride = 0
        has_position = False
        for category_buffer in submesh_json_dict.get("CategoryBufferList", []):
            for element in category_buffer.get("D3D11ElementList", []):
                if str(element.get("Category", "") or "").strip().lower() == "position":
                    pos_stride += int(element.get("ByteWidth", 0) or 0)
                    if str(element.get("SemanticName", "") or "").upper() == "POSITION":
                        has_position = True
        if pos_stride <= 0 or not has_position:
            return empty

        pos_raw = numpy.fromfile(position_buf_path, dtype=numpy.uint8)
        vertex_count = len(pos_raw) // pos_stride
        if vertex_count <= 0:
            return empty
        positions = (
            pos_raw.reshape(vertex_count, pos_stride)[:, 0:12]
            .copy().view(numpy.float32).reshape(vertex_count, 3)
        )

        # ---- Blend 布局（BLENDINDICES + BLENDWEIGHTS offset/格式）----
        # 统一走 parse_blend_layout / parse_blendindices_from_buf /
        # parse_blendweights_from_buf：中央格式表覆盖 R16G16_UINT、R32G32_UINT
        # 等全部解析器支持格式，哨兵按数据格式判定（不再硬编码 0xFFFF）。
        if blend_data is None:
            blend_layout = EFMIBoneMapBuilder.parse_blend_layout(submesh_json_dict)
            if not blend_layout:
                return empty
            element_info = EFMIBoneMapBuilder.layout_element_info(blend_layout)
            try:
                blend_indices = EFMIBoneMapBuilder.parse_blendindices_from_buf(
                    blend_buf_path, element_info
                )
                blend_weights = EFMIBoneMapBuilder.parse_blendweights_from_buf(
                    blend_buf_path, blend_layout
                )
            except Exception:
                return empty
            bi_channels = blend_layout["bi_channels"]
            valid_mask = EFMIBoneMapBuilder.valid_blend_channels(
                blend_indices, element_info, blend_weights
            )
        else:
            blend_indices, blend_weights, valid_mask, bi_channels = blend_data
        if len(blend_indices) != vertex_count:
            return empty

        indices = blend_indices.astype(numpy.int64)
        if blend_weights is not None:
            weights = numpy.asarray(blend_weights, dtype=numpy.float32)
        else:
            weights = numpy.zeros((len(blend_indices), bi_channels), dtype=numpy.float32)
            weights[:, 0] = 1.0

        # ---- 每 local 的驱动顶点集合（质心 + 包围盒 + 权重扩散采样）----
        # 采样不是把整组顶点复制到每个候选的临时对象，而是保留正权重
        # 的 (position, weight) 对。build_vg_maps 会在接触位置做最近邻
        # 扩散检测；固定上限保证大型平面不会让跨部件两两比较爆炸。
        max_diffusion_samples = 256
        accum: dict[int, dict] = {}
        for c in range(indices.shape[1]):
            idx_col = indices[:, c]
            w_col = weights[:, c] if c < weights.shape[1] else weights[:, 0]
            valid = (
                valid_mask[:, c]
                & numpy.isfinite(positions).all(axis=1)
            )
            if not numpy.any(valid):
                continue
            v_idx = idx_col[valid]
            v_w = w_col[valid].astype(numpy.float64)
            v_pos = positions[valid].astype(numpy.float64)
            for local in numpy.unique(v_idx):
                mask = v_idx == local
                pts = v_pos[mask]
                ws_ = v_w[mask]
                w_sum = float(ws_.sum())
                if w_sum <= 0:
                    continue
                entry = accum.setdefault(int(local), {
                    "weighted_sum": numpy.zeros(3, dtype=numpy.float64),
                    "weight_total": 0.0,
                    "weighted_sq_pos": 0.0,  # Σ w_i |p_i|²（用于扩散半径）
                    "bbox_min": numpy.full(3, numpy.inf),
                    "bbox_max": numpy.full(3, -numpy.inf),
                    "vertex_count": 0,
                    "points": [],
                    "weights": [],
                })
                entry["weighted_sum"] += (pts * ws_[:, None]).sum(axis=0)
                entry["weight_total"] += w_sum
                entry["weighted_sq_pos"] += float((ws_ * (pts ** 2).sum(axis=1)).sum())
                entry["bbox_min"] = numpy.minimum(entry["bbox_min"], pts.min(axis=0))
                entry["bbox_max"] = numpy.maximum(entry["bbox_max"], pts.max(axis=0))
                entry["vertex_count"] += int(mask.sum())
                entry["points"].extend(pts.astype(numpy.float32, copy=False).tolist())
                entry["weights"].extend(ws_.astype(numpy.float32, copy=False).tolist())

        result = {}
        for local, e in accum.items():
            if e["weight_total"] <= 0:
                continue
            centroid = e["weighted_sum"] / e["weight_total"]
            # 扩散半径：加权 RMS 半径 spread² = Σw|p|²/Σw - |c|²
            mean_sq = e["weighted_sq_pos"] / e["weight_total"]
            var = max(float(mean_sq - float((centroid ** 2).sum())), 0.0)
            spread = float(numpy.sqrt(var))
            diffusion_points = numpy.asarray(e["points"], dtype=numpy.float32)
            diffusion_weights = numpy.asarray(e["weights"], dtype=numpy.float32)
            if len(diffusion_points) > max_diffusion_samples:
                # 均匀抽样保留整片扩散区域，而不是只取最高权重的中心，
                # 这样“平面 + 散落物体”的接触边界不会被丢掉。
                sample_idx = numpy.linspace(
                    0, len(diffusion_points) - 1, max_diffusion_samples,
                    dtype=numpy.int64,
                )
                diffusion_points = diffusion_points[sample_idx]
                diffusion_weights = diffusion_weights[sample_idx]
            result[local] = {
                "centroid": centroid.astype(numpy.float32),
                "bbox_min": e["bbox_min"].astype(numpy.float32),
                "bbox_max": e["bbox_max"].astype(numpy.float32),
                "vertex_count": e["vertex_count"],
                "spread": spread,  # 扩散矢量球半径（权重强度衰减的扩散路径范围）
                "weight_total": float(e["weight_total"]),  # 权重强度
                "mean_weight": float(e["weight_total"]) / max(e["vertex_count"], 1),
                "diffusion_points": diffusion_points,
                "diffusion_weights": diffusion_weights,
                "diffusion_radius": EFMIBoneMapBuilder._diffusion_radius(diffusion_points),
                "diffusion_normals": EFMIBoneMapBuilder._estimate_diffusion_normals(
                    diffusion_points, use_native=use_native_normals,
                ),
            }
        return result

    # ------------------------------------------------------------------
    # 双套顶点组导出转换（t3 规格 §2/§4：BL 合并组 -> 导出运行时身份）
    #
    # 两边分离契约（t3 设计 §2/§3，t4 固化）：
    # - **BL 侧**：Blender 内顶点组名 = 合并槽位号（去重池命名空间，mesh_
    #   create_helper.import_vertex_groups），编辑/雕刻/权重均在此空间，**永不
    #   直接写盘**；
    # - **导出侧**：写盘 BLENDINDICES 一律是运行时身份（VGOffset+local，自属
    #   声明段内）——经 build_per_mesh_identity_map（对照任务书
    #   get_dualset_slot_identity_map 别名，t3 术语统一）更名回身份域，
    #   FC-2 写盘域断言兜底（validate_export_indices_in_segment）。
    # 身份选择主判 = 顶点数（vertex_count，用户 2026-08-29 裁决「顶点组多的
    # 那一边」），非 weight_total（weight_total 仍收集用于 A4 强度可得性）。
    # 本部分为无 bpy 依赖的纯数据层：外部（submesh_model M1 挂载点）调用
    # build_dualset_export_table 获全表后再对临时导出对象顶点组更名。
    # ------------------------------------------------------------------

    @staticmethod
    def validate_export_indices_in_segment(
        segment: tuple[int, int],
        fold_alias_targets=(),
        indices=(),
        component_label: str = "",
    ) -> None:
        """FC-2 写盘域断言：索引值域必须 ⊆ 本部件自属声明段 ∪ 折叠别名目标段。

        唯一写盘合法性判据（t3 设计 §4）：产物级 BLENDINDICES ∈
        ``[segment_start, segment_end) ∪ fold_alias_targets``。这是「两边分离」
        的硬闸：写盘索引要么是自己的运行时身份（自属段，运行时必写入），要么
        是 same-IB 折叠别名目标（基准部件连续导入槽，运行时同样必写入）；任何
        无法映射到合法运行时身份的引用一律 RuntimeError 中止导出（fail-closed，
        不静默写盘、不回退槽位直写）。

        无 bpy/numpy 强依赖（纯数据层），单测可直测（tests U1）；与更名/折叠/
        I2 是否执行无关——对**产物缓冲**断言，任何绕过更名的直写路径（旧版/
        工作区缺失）都会在此被拦下（t2 R-B）。

        Args:
            segment: 本部件声明段 ``(start, end)``（VGOffset, VGOffset+VGCount）。
            fold_alias_targets: same-IB 折叠别名目标值集合（均为基准声明段身份）；
                空集合时等价于纯段内断言。
            indices: 待校验的写盘索引（int 可迭代或 numpy 数组）。
            component_label: 错误信息中的部件标识（unique_str）。
        """
        try:
            seg_start = int(segment[0])
            seg_end = int(segment[1])
        except (TypeError, ValueError, IndexError) as exc:
            raise RuntimeError(
                f"[EFMI双套导出/FC-2] {component_label} 声明段非法: {segment!r}"
            ) from exc
        if seg_end <= seg_start:
            raise RuntimeError(
                f"[EFMI双套导出/FC-2] {component_label} 声明段为空 "
                f"[{seg_start},{seg_end})，写盘索引无合法身份可归，中止导出"
            )
        targets: set[int] = set()
        for raw_target in fold_alias_targets or ():
            try:
                targets.add(int(raw_target))
            except (TypeError, ValueError):
                raise RuntimeError(
                    f"[EFMI双套导出/FC-2] {component_label} 折叠别名目标非法: "
                    f"{raw_target!r}"
                )
        if isinstance(indices, numpy.ndarray):
            arr = numpy.asarray(indices)
        else:
            try:
                arr = numpy.asarray(list(indices))
            except (TypeError, ValueError) as exc:
                raise RuntimeError(
                    f"[EFMI双套导出/FC-2] {component_label} 写盘索引不可解析: {exc}"
                ) from exc
        if arr.dtype.kind == "f" and (numpy.isnan(arr).any() or numpy.isinf(arr).any()):
            raise RuntimeError(
                f"[EFMI双套导出/FC-2] {component_label} 写盘索引含 NaN/Inf，中止导出"
            )
        try:
            arr = arr.astype(numpy.int64)
        except (TypeError, ValueError, OverflowError) as exc:
            raise RuntimeError(
                f"[EFMI双套导出/FC-2] {component_label} 写盘索引不可解析: {exc}"
            ) from exc
        if arr.size == 0:
            return
        valid = (arr >= seg_start) & (arr < seg_end)
        for target in targets:
            valid |= arr == target
        bad = arr[~valid]
        if bad.size:
            preview = sorted({int(v) for v in bad})[:20]
            suffix = "…" if len(preview) == 20 and len(set(bad.tolist())) > 20 else ""
            allowed = f"[{seg_start},{seg_end}) ∪ 折叠目标 {sorted(targets)[:10]}"
            raise RuntimeError(
                f"[EFMI双套导出/FC-2] {component_label} 写盘 BLENDINDICES 越出"
                f"运行时身份域 {allowed}，非法引用 {preview}{suffix}（共 "
                f"{len(bad)} 项）。产物将引用运行时未注册/未写入的骨骼槽位，"
                "中止导出（fail-closed，不静默写盘）。请重新执行骨骼合并反查/"
                "重新导入该角色后重试"
            )

    @staticmethod
    def _category_buffer_path_from_json(submesh_json: dict, category: str) -> str:
        """按类别定位子网格 json 声明的 buffer 文件名（无声明返回空串）。

        类别优先按 Category/DrawCategory 字段判定；SSMT4 提取端在 LOD1+
        把 Position 缓冲命名为 ``<bare>-trianglelist.buf``（旧端为
        ``-Position.buf``），文件名后缀作为兼容兜底。
        """
        wanted = str(category or "").lower()
        for category_buffer in submesh_json.get("CategoryBufferList", []) or []:
            if not isinstance(category_buffer, dict):
                continue
            file_name = str(category_buffer.get("FileName", "") or "")
            if not file_name:
                continue
            cat = str(
                category_buffer.get("Category")
                or category_buffer.get("DrawCategory")
                or ""
            ).lower()
            lowered = file_name.lower()
            suffix_ok = lowered.endswith(f"-{wanted}.buf")
            if wanted == "position":
                suffix_ok = suffix_ok or lowered.endswith("-trianglelist.buf")
            if cat == wanted or suffix_ok:
                return file_name
        return ""

    @staticmethod
    def _declared_source_buffers(submesh_json: dict) -> dict[str, str]:
        """json 声明的 Position/Blend 源 buffer（F1 指纹用）：FileName -> FileName。

        与 ``_dualset_workspace_strength`` 同口径：CategoryBufferList 中属于
        Position/Blend 类别的文件（Category 字段优先，``-position.buf`` /
        ``-trianglelist.buf`` / ``-blend.buf`` 后缀兜底）。无声明返回空 dict
        （该 json 不适用源数据指纹校验）。
        """
        declared: dict[str, str] = {}
        for category in ("Position", "Blend"):
            file_name = EFMIBoneMapBuilder._category_buffer_path_from_json(
                submesh_json, category
            )
            if file_name:
                declared[file_name] = file_name
        return declared

    @staticmethod
    def _vgmap_source_fingerprint(submesh_dir: str, submesh_json: dict) -> dict | None:
        """F1 源数据指纹：命名为 ``FileName -> [mtime_ns, size]``（缺失文件记 None）。

        json 声明了 Position/Blend.buf（去重结果依赖其内容）时，写回缓存必须
        携带此指纹；``_efmi_cache_intact`` 据此在源数据变更（重新提取 dump /
        替换绑定姿势 buffer）而 json 未变时自动失效整批重算。无声明返回 None
        （不适用指纹校验）。
        """
        declared = EFMIBoneMapBuilder._declared_source_buffers(submesh_json)
        if not declared:
            return None
        fingerprint: dict = {}
        for file_name in declared:
            path = os.path.join(submesh_dir, file_name)
            try:
                stat_result = os.stat(path)
            except OSError:
                fingerprint[file_name] = None
            else:
                fingerprint[file_name] = [
                    int(stat_result.st_mtime_ns),
                    int(stat_result.st_size),
                ]
        return fingerprint

    # t14：导出会话级建表缓存。build_dualset_export_table 每次全量扫工作区
    # （解析全部含 VGMap json + 逐子网格 Position/Blend.buf 重算 weight_total，
    # 含扩散 PCA），导出 N 个子网格时若每个 SubMeshModel 各建一次 = N 倍
    # 开销（0000 场景 ~33 导出子网格 × 建表 = 用户实测「明显变慢」）。
    # 缓存按 (workspace_root, 指纹) 复用：指纹 = 工作区全部 json 的
    # (mtime_ns, size) 摘要（添加/删除/修改任一 json 即失效重建）。
    # 冻结接口 build_dualset_export_table 不变；新入口 additive。
    _dualset_table_cache: dict = {}

    @staticmethod
    def _dualset_table_fingerprint(workspace_root: str) -> tuple:
        """工作区 json 文件指纹（path 相对名 + mtime_ns + size），不读内容。

        覆盖全部 .json（含非 VGMap 的元数据 json）——任一工作区 json 增删改
        都会改指纹，保守失效；仅在指纹与缓存不一致时才重建表。
        """
        if not workspace_root or not os.path.isdir(workspace_root):
            return ()
        entries = []
        for dirpath, _dirs, files in os.walk(workspace_root):
            for name in files:
                if not name.lower().endswith(".json"):
                    continue
                full = os.path.join(dirpath, name)
                try:
                    st = os.stat(full)
                except OSError:
                    continue
                rel = os.path.relpath(full, workspace_root)
                entries.append((rel, st.st_mtime_ns, st.st_size))
        return tuple(sorted(entries))

    @staticmethod
    def get_dualset_export_table_cached(
        workspace_root: str,
        recompute_strength: bool = True,
    ) -> dict[int, dict]:
        """导出会话级缓存入口：同一工作区同指纹只建表一次，全导出复用。

        - 语义/断言与 build_dualset_export_table 完全一致（首个真实调用即
          build 本体：A1/A3/A4/B10 fail-closed 原样生效），缓存只消除重复
          全量重建；
        - 指纹变化（工作区中途增删改 json）→ 下次调用自动重建；
        - **缓存键 = (workspace_root, recompute_strength)**：True/False 各自
          独立缓存（t15 F1 修复）——False（纯逻辑单测/无 dump 环境）不再
          错误复用 True 的真实强度表，参数语义严格成立；
        - 无工作区 / 指纹为空 → 退化为直接建表（不缓存）；
        - 已知设计取舍（F2，文档化）：指纹仅覆盖 .json，不含 Position/Blend.buf
          内容——仅 buffer 变化不触发重建（缓冲与 json 同事务写入，低危；
          如需更保守可将 buf 的 (mtime_ns,size) 并入指纹，成本仅多 stat）。
        """
        if not workspace_root:
            return EFMIBoneMapBuilder.build_dualset_export_table(
                workspace_root, recompute_strength
            )
        fingerprint = EFMIBoneMapBuilder._dualset_table_fingerprint(workspace_root)
        if not fingerprint:
            return EFMIBoneMapBuilder.build_dualset_export_table(
                workspace_root, recompute_strength
            )
        cache_key = (workspace_root, bool(recompute_strength))
        cached = EFMIBoneMapBuilder._dualset_table_cache.get(cache_key)
        if cached is not None and cached[0] == fingerprint:
            return cached[1]
        table = EFMIBoneMapBuilder.build_dualset_export_table(
            workspace_root, recompute_strength
        )
        EFMIBoneMapBuilder._dualset_table_cache[cache_key] = (
            fingerprint, table
        )
        return table

    @staticmethod
    def _split_lod_identity(unique_str: str) -> tuple[str, str]:
        """拆分身份里的 LOD 前缀（`LOD0.xxx-1-0` -> ("LOD0", "xxx-1-0")；裸身份 -> ("", 身份)）。"""
        text = str(unique_str or "")
        if "." in text:
            head, tail = text.split(".", 1)
            if head.upper().startswith("LOD") and head[3:].isdigit() and tail:
                return head, tail
        return "", text

    @staticmethod
    def _identity_matches(unique_str: str, component_unique_str: str) -> bool:
        """工作区 json 身份（`__unique_str__`）是否对应当前组件身份（含 LOD0 兜底）。

        裸身份（旧扁平工作空间遗留 / 手工命名的物体）与 `LOD0.<bare>` 互认，
        与 WorkSpaceHelper.get_submesh_folder_path 的目录兜底、以及
        ui/ntmi_modimp/runtime_cache.prefix_identity_matches 的「裸前缀按 LOD0
        兼容」同口径；显式带 LOD 前缀的组件身份只认完全相同的身份（LOD1 部件
        不能借 LOD0 的 json）。
        """
        if unique_str == component_unique_str:
            return True
        if not component_unique_str:
            return False
        lod_name, bare = EFMIBoneMapBuilder._split_lod_identity(component_unique_str)
        if lod_name or not bare:
            return False
        return unique_str == f"{_DEFAULT_LOD_NAME}.{bare}"

    @staticmethod
    def _find_component_record(workspace_root: str, component_unique_str: str):
        """按身份定位工作区组件 json（精确身份优先，裸身份再按 LOD0 兜底）。

        返回 (submesh_dir, submesh_json)；找不到返回 None。兜底命中时 json 的
        `__unique_str__` 带 LOD 前缀而传入身份是裸的——身份段 / VGMap / 强度
        全部取自 json 本身，与传入字符串无关，因此可直接使用。
        """
        fallback = None
        for submesh_dir, submesh_json in EFMIBoneMapBuilder._iter_submesh_jsons(
            workspace_root
        ):
            unique_str = str(submesh_json.get("__unique_str__", "") or "")
            if unique_str == component_unique_str:
                return submesh_dir, submesh_json
            if fallback is None and EFMIBoneMapBuilder._identity_matches(
                unique_str, component_unique_str
            ):
                fallback = (submesh_dir, submesh_json)
        return fallback

    @staticmethod
    def build_per_mesh_identity_map(
        workspace_root: str,
        component_unique_str: str,
        recompute_strength: bool = True,
    ) -> dict[int, int]:
        """per-mesh 身份映射（t25 方向 A / t3 §4.3c v3）：槽 s → e_M(s)。

        语义：对组件 C（unique_str）的每个引用槽 s，导出身份 = **槽成员中属于
        组件 C 的成员身份**（VGOffset_C + local），按 v2 顶点数裁决（多本组件
        成员时）；**槽无本组件成员时 FC-1 中止**（数据损坏即 RuntimeError，
        不静默回退全局 e(s)——t24 §6 / t25 notes 实证 0000 1275 引用 0 违反，
        违反即数据损坏/未重导入）。

        与 v2 全局 e(s) 的关系：本映射是**全量覆盖层**（含未更名槽与 canonical
        恒等槽）——凡 canonical 属其它组件的槽，写盘索引改写为本组件成员身份，
        使网格引用落回**自己 attach 必写的身份域**（自写自读、时序无关），根治
        t24 d173f868 型「跨组件 canonical 单写者时序塌陷」。

        FC-1 双重 fail-closed：本函数 raise 后，submesh_model 的 M1 更名点
        （_apply_dualset_export_rename）按 RuntimeError 中止导出；efmi.py 的
        I2 可达性守卫调用点（:1664-1665）``except Exception`` 吞掉并回退全局
        e(s) 严格路径——两条路径都不会静默放行越段引用（FC-2 写盘断言兜底）。

        输出 {slot(int): e_M(s)(int)}；供 submesh_model M1 更名与 efmi.py
        rekey/守卫同步使用。只覆盖组件 C 的 VGMap 引用槽（其网格顶点组实际
        存在的槽），不映射全表（避免无关槽的回退洪泛）。
        """
        table = EFMIBoneMapBuilder.get_dualset_export_table_cached(
            workspace_root, recompute_strength
        )
        # 定位组件 C 的身份段 [vg_offset, vg_offset+vg_count) 与其 VGMap 引用槽。
        # 身份匹配含 LOD0 兜底（裸身份 ↔ `LOD0.<bare>`）：物体/组件身份可能没有
        # LOD 前缀（旧扁平工作空间遗留 / 手工命名），而工作区 json 一律带 LOD0
        # 前缀；不兜底就会误报「找不到组件的 VGOffset/VGCount」。
        seg = None
        ref_slots: list[int] = []
        record = EFMIBoneMapBuilder._find_component_record(
            workspace_root, component_unique_str
        )
        if record is not None:
            submesh_json = record[1]
            offset = int(submesh_json.get("VGOffset", 0) or 0)
            count = int(submesh_json.get("VGCount", 0) or 0)
            seg = (offset, offset + count)
            vg_map = submesh_json.get("VGMap") or {}
            ref_slots = sorted({int(v) for v in vg_map.values()})
        if seg is None:
            raise RuntimeError(
                f"[EFMI双套导出] 找不到组件 {component_unique_str} 的 "
                "VGOffset/VGCount（工作区不一致或缓存陈旧），中止导出"
                f"（已按 裸身份 与 默认 {_DEFAULT_LOD_NAME} 身份查找）"
            )
        result: dict[int, int] = {}
        for slot in ref_slots:
            row = table.get(slot)
            members = row.get("members", []) if row is not None else []
            self_members = [
                m for m in members
                if seg[0] <= int(m.get("identity", -1)) < seg[1]
            ]
            # FC-1（t3 设计 §4 / t6-F4）：引用槽既不在建表结果中（row is None，
            # 表与 json 不一致/陈旧缓存）也没有「身份落在本组件声明段」的成员
            # ——合并为同一条拒绝路径：该引用无法映射到本组件任何合法运行时
            # 身份，一律大声中止（fail-closed），绝不静默回退槽位原值/全局 e(s)
            # （越段直写 = 引用运行时未注册/未写入的骨骼槽位）。t2/t24 实证
            # 0000 0 违反——违反即数据损坏/工作区经旧工具链处理/未重导入。
            if not self_members:
                raise RuntimeError(
                    f"[EFMI双套导出/FC-1] {component_unique_str} 引用槽 {slot} "
                    "无法映射到本组件的合法运行时身份（槽不在建表结果中或成员"
                    "集中无任何身份落于本组件声明段 "
                    f"[{seg[0]},{seg[1]})）：VGMap 与 VGOffset/VGCount 不一致、"
                    "缓存陈旧或工作区经旧工具链处理，中止导出（fail-closed）。"
                    "请重新执行骨骼合并反查/重新导入该角色"
                )
            if len(self_members) == 1:
                result[slot] = int(self_members[0]["identity"])
                continue
            # 多本组件成员：v2 顶点数精确裁决 + tie-break（同 select 语义）
            counts = [int(m.get("vertex_count", 0) or 0) for m in self_members]
            best = max(counts)
            tied = [m for m, c in zip(self_members, counts) if c == best]
            canonical_self = [m for m in tied if m["identity"] == int(slot)]
            if canonical_self:
                result[slot] = int(canonical_self[0]["identity"])
            else:
                result[slot] = min(int(m["identity"]) for m in tied)
        return result

    @staticmethod
    def select_dualset_export_identity(slot: int, members: list[dict]) -> int:
        """合并槽导出身份 = 顶点数最多成员身份 + 三层 tie-break（t3 §4 v2，t18）。

        members: [{'local': int, 'identity': int, 'vertex_count': int, ...}, ...]
        - 主度量 = vertex_count（驱动顶点数，compute_driven_signatures 字段；
          用户 2026-08-29 裁决：改为「顶点组多的那一边」）；与 canonical 判据
          weighted_vertex_count 的差异：vertex_count 含位置有限性过滤（inf/NaN
          顶点不计），canonical 无该过滤——两者排序可不同，更名不恒空（t18
          等价性分析实证）；
        - 单源槽（len==1）：恒等（identity == 槽位号，canonical 恒等式）；
        - 并列（计数型精确相等，无容差）：① canonical 成员（identity == slot）
          优先；② identity 升序；③ 正常数据不可达 fail-closed；
        - 缺 vertex_count 的成员按 0 参与（B7 全零韧性）。
        """
        if len(members) == 1:
            return members[0]["identity"]
        counts = [int(m.get("vertex_count", 0) or 0) for m in members]
        best = max(counts)
        tied = [m for m, c in zip(members, counts) if c == best]
        canonical = [m for m in tied if m["identity"] == slot]
        if canonical:
            return canonical[0]["identity"]
        return min(m["identity"] for m in tied)

    @staticmethod
    def build_dualset_export_table(
        workspace_root: str,
        recompute_strength: bool = True,
    ) -> dict[int, dict]:
        """反查工作区 json 构建「合并槽 -> 导出身份」全表（t3 §3，无新增持久化）。

        输入：workspace_root（EFMI 工作空间根，含 LOD0/LOD1 子网格目录）。
        输出：{slot: {"export_identity": int, "renamed": bool, "members": [
              {"comp": unique_str, "local": int, "identity": int,
               "weight_total": float, "vertex_count": int}]}}
        - identity = VGOffset + local（非去重身份；VGOffset 连续累加 ⇒ 全局单射，
          t3 §3.2）；
        - 身份选择主判 = vertex_count（驱动顶点数；用户 2026-08-29 裁决「顶点组
          多的那一边」，见 select_dualset_export_identity）；weight_total 仍收集
          用于 A4 强度可得性核算（recompute_strength=True 时对每个子网格用工作区
          json 同目录的 Position.buf + Blend.buf 调用 compute_driven_signatures
          重算，与去重运行期同一函数同一口径；buffer 缺失/损坏时该成员按 0——
          B7 全零成员韧性）；recompute_strength=False 供纯逻辑单测/无 dump 环境
          （全部按 0）。
        - 单源槽恒等；合并槽按 select_dualset_export_identity（含三层 tie-break）。
        - fail-closed 断言（写入输出表前）：
            A1：VGMap 键集必须 == 0..VGCount-1（缺项 = 成员不完整，拒绝转换）；
            A3：identity 全局单射（VGOffset 段重叠/陈旧 json ⇒ RuntimeError）；
            A4：任一子网格 json 声明了 Position/Blend 类别但 buffer 物理缺失 ⇒
                RuntimeError（强度数据不可得，拒绝转换、不静默回退槽位直写）；
            B10：跨 LOD 段共享槽位号（违反 v10/v11 段不相交布局）⇒ RuntimeError；
            B11（FC-3，t3 设计 §4）：任一 VGMap 引用槽必须落在全池声明段并集内
                （损坏数据引用「无人声明」槽位 ⇒ RuntimeError）；「越出自属段」
                的池共享按设计语义保持允许（canonical 借位）。
        """
        table: dict[int, dict] = {}
        slot_members: dict[int, list[dict]] = defaultdict(list)
        identity_owner: dict[int, str] = {}
        slot_lod_prefixes: dict[int, set] = defaultdict(set)
        missing_strength_components: list[str] = []
        # B11（FC-3）：全部声明段 [vg_offset, vg_offset+vg_count) 的精确并集
        # （逐段判定；投影跳过部件会在声明段间留下空隙，包围盒会漏放凹槽引用）。
        declared_pool_bounds: list[int] = []
        declared_segments: list[tuple[int, int]] = []
        for submesh_dir, submesh_json in EFMIBoneMapBuilder._iter_submesh_jsons(
            workspace_root
        ):
            vg_offset = int(submesh_json.get("VGOffset", 0) or 0)
            vg_map = submesh_json.get("VGMap") or {}
            vg_count = int(submesh_json.get("VGCount", 0) or 0)
            unique_str = submesh_json.get("__unique_str__", "")
            lod_prefix = str(unique_str or "").split(".", 1)[0]
            declared_pool_bounds.extend([vg_offset, vg_offset + vg_count])
            declared_segments.append((vg_offset, vg_offset + vg_count))
            # A1：VGMap 键集必须 == 0..VGCount-1（损坏 json 不得静默纳入——
            # 缺项会导致成员列表不完整、该槽 e(s) 可能算错，M1 生产路径 fail-closed）。
            if len(vg_map) != vg_count:
                raise RuntimeError(
                    f"[EFMI双套导出] VGMap 键集不完整（A1）：{unique_str} "
                    f"VGMap({len(vg_map)} 键) != VGCount({vg_count})，拒绝转换"
                )
            strength = {}
            if recompute_strength:
                if EFMIBoneMapBuilder._is_dualset_strength_missing(
                    submesh_dir, submesh_json
                ):
                    missing_strength_components.append(unique_str)
                try:
                    strength = EFMIBoneMapBuilder._dualset_workspace_strength(
                        submesh_dir, submesh_json
                    )
                except Exception:
                    strength = {}
            for raw_local, raw_slot in vg_map.items():
                local = int(raw_local)
                slot = int(raw_slot)
                identity = vg_offset + local
                prev = identity_owner.get(identity)
                if prev is not None and prev != unique_str:
                    raise RuntimeError(
                        f"[EFMI双套导出] identity {identity} 被多组件同时占用 "
                        f"({prev} 与 {unique_str})——VGOffset 段重叠或陈旧 json，"
                        "中止转换（断言 A3）"
                    )
                identity_owner[identity] = unique_str
                slot_lod_prefixes[slot].add(lod_prefix)
                sig = strength.get(local)
                weight_total = 0.0
                vertex_count = 0
                if isinstance(sig, dict):
                    try:
                        weight_total = float(sig.get("weight_total", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        weight_total = 0.0
                    try:
                        vertex_count = int(sig.get("vertex_count", 0) or 0)
                    except (TypeError, ValueError):
                        vertex_count = 0
                slot_members[slot].append({
                    "comp": unique_str,
                    "local": local,
                    "identity": identity,
                    "weight_total": weight_total,
                    "vertex_count": vertex_count,
                })
        if missing_strength_components:
            raise RuntimeError(
                "[EFMI双套导出] 强度数据不可得（A4）：以下子网格声明了 "
                f"Position/Blend 类别但 buffer 缺失: {missing_strength_components}。"
                "无法重算 weight_total，拒绝转换（不静默回退槽位直写）"
            )
        # B11（FC-3，t3 设计 §4 / t6-F3）：任一 VGMap 引用槽必须落在**声明段精确
        # 并集**内（逐段比对 [vg_offset, vg_offset+vg_count)，任一命中即合法）。
        # - 引用「无人声明」的槽位 = 数据损坏（t2 §1.2 的 B 空洞槽）；
        # - 「越出自属段」的池共享（canonical 借位）是设计语义，仍允许；
        # - 不用包围盒 [min,max)：投影跳过部件会在声明段间留空隙，凹槽引用
        #   （落进空隙的损坏数据）不能被包围盒放行（T6-F3）。
        if declared_segments:
            pool_lo = min(declared_pool_bounds)
            pool_hi = max(declared_pool_bounds)
            b11_violations: list[tuple[str, str, int]] = []
            # B11 定位诊断（只读附加输出）：哪些 json「本该贡献声明段却缺席」
            # —— 解析失败、或无 VGMap 键（投影未匹配跳过 / 本次未产出映射 /
            # 被外部清理）。旧版只报越界引用方，用户必须人工按 VGOffset 反查
            # 缺席部件；这里把缺席侧一并收集进异常文本。
            scan_diag: dict[str, list] = {"unreadable": [], "no_vgmap": []}
            for _submesh_dir, submesh_json in EFMIBoneMapBuilder._iter_submesh_jsons(
                workspace_root, diagnostics=scan_diag
            ):
                unique_str = submesh_json.get("__unique_str__", "")
                for raw_local, raw_slot in (submesh_json.get("VGMap") or {}).items():
                    slot = int(raw_slot)
                    if not any(
                        seg_start <= slot < seg_end
                        for seg_start, seg_end in declared_segments
                    ):
                        b11_violations.append((unique_str, raw_local, slot))
            if b11_violations:
                preview = ", ".join(
                    f"{us}:{local}->{slot}" for us, local, slot in b11_violations[:12]
                )
                suffix = "…" if len(b11_violations) > 12 else ""
                raise RuntimeError(
                    "[EFMI双套导出] VGMap 引用槽越出声明段精确并集（B11/FC-3）: "
                    f"{preview}{suffix}（声明段并集 [{pool_lo},{pool_hi})，共 "
                    f"{len(b11_violations)} 项）——引用了无人声明的骨骼槽位"
                    "（含段间/段内空洞），数据损坏或陈旧，中止转换"
                    + EFMIBoneMapBuilder._describe_declaration_gaps(
                        workspace_root,
                        declared_segments,
                        pool_lo,
                        pool_hi,
                        scan_diag,
                    )
                )
        cross_lod_collisions = [
            slot for slot, lod_set in slot_lod_prefixes.items()
            if len(lod_set) > 1
        ]
        if cross_lod_collisions:
            raise RuntimeError(
                f"[EFMI双套导出] 跨 LOD 段共享槽位号（B10）: {sorted(cross_lod_collisions)}"
                "——违反 v10/v11 段不相交布局（LOD0 段与 LOD1 平移段不允许撞车），"
                "中止转换"
            )
        for slot, members in slot_members.items():
            e = EFMIBoneMapBuilder.select_dualset_export_identity(slot, members)
            table[slot] = {
                "export_identity": e,
                "renamed": e != slot,
                "members": members,
            }
        return table

    # ------------------------------------------------------------------
    # t6 契约入口（tests/test_dualset_vg_convert.py 单一导入点）
    # 同语义暴露四个公开接口：select_export_identity / build_export_table /
    # compute_weight_total / run。规格 §3.3/A4/B10 的 fail-closed 由
    # build_dualset_export_table（A3/A4/B10）与 run（A2/A4/B10 汇总）落实。
    # ------------------------------------------------------------------

    @staticmethod
    def select_export_identity(slot: int, members: list[dict]) -> int:
        """t6 契约别名：委托 select_dualset_export_identity（同语义）。"""
        return EFMIBoneMapBuilder.select_dualset_export_identity(slot, members)

    @staticmethod
    def build_export_table(slot_members: dict[int, list[dict]]) -> dict[int, dict]:
        """t6 契约：由聚合后的 slot->members 构建决策表（纯函数，无 I/O）。

        输出 {slot: {'slot', 'member_count', 'members', 'export_identity', 'renamed'}}——
        与 t5 原型 build_export_table 结构一致。
        """
        table = {}
        for slot, members in slot_members.items():
            e = EFMIBoneMapBuilder.select_dualset_export_identity(slot, members)
            table[slot] = {
                "slot": slot,
                "member_count": len(members),
                "members": members,
                "export_identity": e,
                "renamed": e != slot,
            }
        return table

    @staticmethod
    def compute_weight_total(
        position_buf_path: str,
        blend_buf_path: str,
        submesh_json_dict: dict,
    ) -> dict[int, float]:
        """t6 契约：每 local 的 weight_total（Σ 有效通道原始权重）。

        - 任一必需 buf 缺失 ⇒ 返回 {}（A4 权威语义，不静默取 0，与产品
          compute_driven_signatures L661 口径一致）；
        - buffer 与 stride 不对齐 ⇒ 抛 ValueError（拒绝，不静默截断）；
        - w==0 的 local 丢弃（产品 L759 语义）。
        实现为独立直读（不委托吞异常的 compute_driven_signatures），保证
        对齐/缺失语义上抛给调用方。
        """
        if not os.path.isfile(position_buf_path) or not os.path.isfile(blend_buf_path):
            return {}
        layout = EFMIBoneMapBuilder.parse_blend_layout(submesh_json_dict)
        if not layout:
            return {}
        element_info = EFMIBoneMapBuilder.layout_element_info(layout)
        indices = EFMIBoneMapBuilder.parse_blendindices_from_buf(
            blend_buf_path, element_info
        )
        positions = EFMIBoneMapBuilder._read_positions_for_strength(
            position_buf_path, submesh_json_dict
        )
        weights = EFMIBoneMapBuilder.parse_blendweights_from_buf(
            blend_buf_path, layout
        )
        valid_mask = EFMIBoneMapBuilder.valid_blend_channels(
            indices, element_info, weights
        )
        bi_channels = int(layout.get("bi_channels", 4) or 4)
        if weights is None:
            weights = numpy.zeros((len(indices), bi_channels), dtype=numpy.float32)
            weights[:, 0] = 1.0
        accum: dict[int, float] = defaultdict(float)
        for c in range(indices.shape[1]):
            idx_col = indices[:, c]
            w_col = weights[:, c] if c < weights.shape[1] else weights[:, 0]
            valid = valid_mask[:, c]
            if positions is not None:
                valid = valid & numpy.isfinite(positions).all(axis=1)
            if not numpy.any(valid):
                continue
            v_idx = idx_col[valid]
            v_w = w_col[valid].astype(numpy.float64)
            for local in numpy.unique(v_idx):
                mask = v_idx == local
                w_sum = float(v_w[mask].sum())
                if w_sum > 0:
                    accum[int(local)] += w_sum
        return {local: s for local, s in accum.items() if s > 0}

    @staticmethod
    def _read_positions_for_strength(
        position_buf_path: str, submesh_json_dict: dict
    ) -> numpy.ndarray | None:
        """读取绑定姿态位置 (v,3)；布局缺失/文件缺失返回 None（跳过位置过滤）。"""
        pos_stride = 0
        has_position = False
        for category_buffer in submesh_json_dict.get("CategoryBufferList", []):
            for element in category_buffer.get("D3D11ElementList", []):
                if str(element.get("Category", "") or "").strip().lower() == "position":
                    pos_stride += int(element.get("ByteWidth", 0) or 0)
                    if str(element.get("SemanticName", "") or "").upper() == "POSITION":
                        has_position = True
        if pos_stride <= 0 or not has_position:
            return None
        raw = numpy.fromfile(position_buf_path, dtype=numpy.uint8)
        vertex_count = len(raw) // pos_stride
        if vertex_count <= 0:
            return None
        return (
            raw.reshape(vertex_count, pos_stride)[:, 0:12]
            .copy().view(numpy.float32).reshape(vertex_count, 3)
        )

    @staticmethod
    def run(workspace_root: str, outdir: str) -> int:
        """t6 契约：完整转换 CLI（只读工作区，输出到 outdir）。

        - A1：VGMap 键集 != VGCount 的子网格跳过 + 警告（不越权修复、不纳入、rc 仍 0）；
        - A4：json 声明了 Position/Blend 条目但 buffer 物理缺失 ⇒ 抛 RuntimeError
          （数据缺失，大声失败，不静默取 0）；json 完全未声明 Position 类别且
          无 Blend 条目 ⇒ rc=1 + summary.assert_A4_strength_available=false；
        - A2/A3/B10：冲突/撞车 ⇒ rc=1 + summary 标记 + fail_reason；
        - 输出 t5-summary.json / t5-ledger.json / t5-rename_map.json。
        返回 0=成功（或无可转换数据），1=规格断言触发。
        """
        summary = {
            "workspace_root": workspace_root,
            "submesh_with_vgmap": 0,
            "slots_total": 0,
            "slots_single": 0,
            "slots_merged": 0,
            "slots_renamed": 0,
            "assert_A2_export_unique": True,
            "assert_A3_identity_single": True,
            "assert_A4_strength_available": True,
            "assert_B10_lod_segments_disjoint": True,
            "fail_reason": "",
            "warnings": [],
        }
        slot_members: dict[int, list[dict]] = defaultdict(list)
        identity_seen: dict[int, str] = {}
        identity_conflicts: list[tuple] = []
        slot_lod_prefixes: dict[int, set] = defaultdict(set)
        a4_missing: list[str] = []  # 强度不可得子网格（文件缺失 或 形态缺 Position）
        for submesh_dir, submesh_json in EFMIBoneMapBuilder._iter_submesh_jsons(
            workspace_root
        ):
            vg_map = submesh_json.get("VGMap") or {}
            vg_offset = int(submesh_json.get("VGOffset", 0) or 0)
            vg_count = int(submesh_json.get("VGCount", 0) or 0)
            unique_str = submesh_json.get("__unique_str__", "")
            lod_prefix = str(unique_str or "").split(".", 1)[0]
            if len(vg_map) != vg_count:  # A1：损坏 json 跳过 + 警告
                summary["warnings"].append(
                    f"[A1] {unique_str}: VGMap 键集({len(vg_map)}) != VGCount({vg_count})，跳过"
                )
                continue
            # A4：强度数据不可得（json 声明的 buf 文件缺失，或声明了 Blend 但未声明
            # Position 类别）⇒ 拒绝转换（rc=1 + summary 标记），绝不静默取 0。
            if EFMIBoneMapBuilder._dualset_strength_unavailable(
                submesh_dir, submesh_json
            ):
                a4_missing.append(unique_str)
                continue
            summary["submesh_with_vgmap"] += 1
            _pos = EFMIBoneMapBuilder._dualset_workspace_strength(submesh_dir, submesh_json)
            for raw_local, raw_slot in vg_map.items():
                local = int(raw_local)
                slot = int(raw_slot)
                identity = vg_offset + local
                if identity in identity_seen and identity_seen[identity] != unique_str:
                    identity_conflicts.append((identity, identity_seen[identity], unique_str))
                identity_seen[identity] = unique_str
                slot_lod_prefixes[slot].add(lod_prefix)
                sig = _pos.get(local)
                wt = 0.0
                vc = 0
                if isinstance(sig, dict):
                    try:
                        wt = float(sig.get("weight_total", 0.0) or 0.0)
                    except (TypeError, ValueError):
                        wt = 0.0
                    try:
                        vc = int(sig.get("vertex_count", 0) or 0)
                    except (TypeError, ValueError):
                        vc = 0
                slot_members[slot].append({
                    "comp": unique_str,
                    "local": local,
                    "identity": identity,
                    "weight_total": wt,
                    "vertex_count": vc,
                })
        # A4 汇总：任一子网格强度不可得 ⇒ rc=1 + 标记 + fail_reason（不抛异常）。
        if a4_missing:
            summary["assert_A4_strength_available"] = False
            summary["fail_reason"] = (
                f"强度数据不可得（A4）: {a4_missing}"
            )
            EFMIBoneMapBuilder._write_dualset_outputs(outdir, {}, summary)
            return 1

        table = EFMIBoneMapBuilder.build_export_table(slot_members)
        export_ids = [row["export_identity"] for row in table.values()]
        a2_ok = len(export_ids) == len(set(export_ids))
        a3_ok = not identity_conflicts
        b10_ok = all(len(lod_set) == 1 for lod_set in slot_lod_prefixes.values())
        summary["assert_A2_export_unique"] = a2_ok
        summary["assert_A3_identity_single"] = a3_ok
        summary["assert_B10_lod_segments_disjoint"] = b10_ok
        if not a2_ok or not a3_ok or not b10_ok:
            summary["fail_reason"] = (
                f"A2={a2_ok} A3={a3_ok} B10={b10_ok} "
                f"identity_conflicts={identity_conflicts[:5]}"
            )
            EFMIBoneMapBuilder._write_dualset_outputs(outdir, table, summary)
            return 1
        merged = {s: r for s, r in table.items() if len(r["members"]) >= 2}
        renamed = {s: r for s, r in merged.items() if r["renamed"]}
        summary["slots_total"] = len(table)
        summary["slots_single"] = len(table) - len(merged)
        summary["slots_merged"] = len(merged)
        summary["slots_renamed"] = len(renamed)
        EFMIBoneMapBuilder._write_dualset_outputs(outdir, table, summary)
        return 0

    @staticmethod
    def _write_dualset_outputs(outdir: str, table: dict, summary: dict) -> None:
        """写 t5-summary.json / t5-ledger.json / t5-rename_map.json（outdir 在工作区之外）。"""
        os.makedirs(outdir, exist_ok=True)
        with open(os.path.join(outdir, "t5-summary.json"), "w", encoding="utf-8") as f:
            json.dump(summary, f, ensure_ascii=False, indent=2)
        with open(os.path.join(outdir, "t5-ledger.json"), "w", encoding="utf-8") as f:
            json.dump({str(s): row for s, row in sorted(table.items())},
                      f, ensure_ascii=False, indent=2)
        rename_map = {
            str(s): row["export_identity"]
            for s, row in sorted(table.items())
            if row.get("renamed")
        }
        with open(os.path.join(outdir, "t5-rename_map.json"), "w", encoding="utf-8") as f:
            json.dump(rename_map, f, ensure_ascii=False, indent=2)

    @staticmethod
    def _dualset_workspace_strength(submesh_dir: str, submesh_json: dict) -> dict:
        """从工作区子网格 json 定位 Position/Blend.buf 并重算每 local 强度签名。

        buffer 文件与 json 同目录（CategoryBufferList.FileName）。
        任一必需 buf 缺失/无法解析 ⇒ 返回 {}（A4：强度数据不可得）。
        返回 compute_driven_signatures 的原始签名 dict。
        """
        pos_file = EFMIBoneMapBuilder._category_buffer_path_from_json(
            submesh_json, "Position"
        )
        blend_file = EFMIBoneMapBuilder._category_buffer_path_from_json(
            submesh_json, "Blend"
        )
        pos_path = os.path.join(submesh_dir, pos_file) if pos_file else ""
        blend_path = os.path.join(submesh_dir, blend_file) if blend_file else ""
        if not pos_path or not blend_path:
            return {}
        if not os.path.isfile(pos_path) or not os.path.isfile(blend_path):
            return {}
        return EFMIBoneMapBuilder.compute_driven_signatures(
            pos_path, blend_path, submesh_json
        )

    @staticmethod
    def _is_dualset_strength_missing(submesh_dir: str, submesh_json: dict) -> bool:
        """A4 判定：子网格强度数据不可得（refuse 转换）。

        满足任一即不可得：
        1. json 声明了 Position/Blend 类别条目但对应 buffer 物理缺失；
        2. json 未声明 Position 类别（或未声明 -Position.buf）——compute_driven_
           signatures 对无 Position 布局直接返回空表（L671），该子网格无法
           重算 weight_total。
        """
        pos_file = EFMIBoneMapBuilder._category_buffer_path_from_json(
            submesh_json, "Position"
        )
        blend_file = EFMIBoneMapBuilder._category_buffer_path_from_json(
            submesh_json, "Blend"
        )
        pos_path = os.path.join(submesh_dir, pos_file) if pos_file else ""
        blend_path = os.path.join(submesh_dir, blend_file) if blend_file else ""
        blend_declared = bool(blend_file)
        if (pos_path and not os.path.isfile(pos_path)) or (
            blend_path and not os.path.isfile(blend_path)
        ):
            return True
        if not pos_path or not blend_declared:
            # 未声明 Position 类别 / 没有任何 Blend buffer → 无法重算强度。
            return True
        return False

    @staticmethod
    def _is_dualset_file_missing(submesh_dir: str, submesh_json: dict) -> bool:
        """A4（数据缺失）细分判定：json 声明了 Position/Blend 条目但文件物理缺失。

        与 _is_dualset_strength_missing 的区别：本函数只查「声明了但文件不在」
        的硬缺失（应大声失败）；「根本没声明强度类别」由 _dualset_has_strength_decl
        统一判形态缺失（rc=1）。
        """
        pos_file = EFMIBoneMapBuilder._category_buffer_path_from_json(
            submesh_json, "Position"
        )
        blend_file = EFMIBoneMapBuilder._category_buffer_path_from_json(
            submesh_json, "Blend"
        )
        pos_path = os.path.join(submesh_dir, pos_file) if pos_file else ""
        blend_path = os.path.join(submesh_dir, blend_file) if blend_file else ""
        if pos_path and not os.path.isfile(pos_path):
            return True
        if blend_path and not os.path.isfile(blend_path):
            return True
        return False

    @staticmethod
    def _dualset_has_strength_decl(submesh_dir: str, submesh_json: dict) -> bool:
        """是否声明了任一强度类别（Position 或 Blend 条目）。"""
        for category in ("Position", "Blend"):
            if EFMIBoneMapBuilder._category_buffer_path_from_json(
                submesh_json, category
            ):
                return True
        return False

    @staticmethod
    def _dualset_strength_shape_ok(submesh_dir: str, submesh_json: dict) -> bool:
        """强度类别形态完备：Position 与 Blend 条目都必须声明（缺一不可重算）。"""
        has_pos = bool(
            EFMIBoneMapBuilder._category_buffer_path_from_json(
                submesh_json, "Position"
            )
        )
        has_blend = bool(
            EFMIBoneMapBuilder._category_buffer_path_from_json(
                submesh_json, "Blend"
            )
        )
        return has_pos and has_blend

    @staticmethod
    def _dualset_strength_unavailable(submesh_dir: str, submesh_json: dict) -> bool:
        """A4 统一判定：子网格强度数据不可得（拒绝转换）。

        任一满足即不可得：
        1. json 声明的 Position/Blend buffer 文件物理缺失；
        2. 未声明 Position 类别（或只有 Blend 条目）——无法重算 weight_total。
        """
        if EFMIBoneMapBuilder._is_dualset_file_missing(submesh_dir, submesh_json):
            return True
        declared = EFMIBoneMapBuilder._dualset_has_strength_decl(submesh_dir, submesh_json)
        if declared:
            return not EFMIBoneMapBuilder._dualset_strength_shape_ok(
                submesh_dir, submesh_json
            )
        return True

    @staticmethod
    def _iter_submesh_jsons(workspace_root: str, diagnostics: dict | None = None):
        """遍历工作区含 VGMap 的子网格 json（无 bpy；LOD 目录与 TYPE_ 目录两级）。

        yield (submesh_dir, submesh_json_dict)；json dict 附加 __unique_str__。
        路径形态（与 t1 scan 脚本同规则）：
        - <ws>/LOD0/<bare>/TYPE_*/<bare>.json -> "LOD0.<bare>"
        - <ws>/<bare>/TYPE_*/<bare>.json -> "<bare>"
        submesh_dir = 含 json 的 TYPE_ 目录（供调用方定位 Position/Blend.buf）。

        diagnostics：可选诊断收集器（B11 定位用，纯只读附加输出）。传入 dict 时填写：
        - diagnostics["unreadable"] += [(json 绝对路径, 异常摘要)]：json 读不出来，
          该文件既不贡献声明段、也不贡献 VGMap 条目；
        - diagnostics["no_vgmap"] += [(unique_str, 缺席原因)]：解析成功但**没有
          VGMap 键**（投影未匹配跳过 / 本次未产出槽位映射 / 被清理过的 json），
          同样不贡献声明段 —— 这类 json 正是「本该有声明却缺席」的候选方。
        两条清单只收子网格形态的文件（TYPE_ 目录内、且文件名 == 子网格名），
        避免把 Config/*.json、Import.json 等无关文件报成缺席方；该收窄**只影响
        诊断输出**，声明段与 VGMap 的建表语义保持原样。
        """
        if not workspace_root or not os.path.isdir(workspace_root):
            return
        if diagnostics is not None:
            unreadable = diagnostics.setdefault("unreadable", [])
            no_vgmap = diagnostics.setdefault("no_vgmap", [])
        else:
            unreadable = None
            no_vgmap = None
        for dirpath, _dirs, files in os.walk(workspace_root):
            for name in files:
                if not name.lower().endswith(".json"):
                    continue
                full = os.path.join(dirpath, name)
                # 子网格 json 的规范位置是 <ws>/[LODn/]<bare>/TYPE_*/<bare>.json。
                # 声明段的语义不在下面收窄（非 TYPE_ 位置的 VGMap 仍照旧参与建表，
                # 避免静默改变 fail-closed 判定），但**诊断清单只收子网格形态的
                # 文件**：否则 Config/*.json、Import.json、角色级映射表等无关 json
                # 会把 B11 缺席方清单挤满。
                in_submesh_dir = os.path.basename(dirpath).startswith("TYPE_")
                try:
                    with open(full, "r", encoding="utf-8") as f:
                        data = json.load(f)
                except Exception as exc:
                    if unreadable is not None and in_submesh_dir:
                        unreadable.append((full, f"{type(exc).__name__}: {exc}"))
                    continue
                if not isinstance(data, dict):
                    if unreadable is not None and in_submesh_dir:
                        unreadable.append(
                            (full, f"顶层不是 JSON 对象（{type(data).__name__}）")
                        )
                    continue
                bare = os.path.basename(os.path.dirname(dirpath))          # <bare>
                lod = os.path.basename(os.path.dirname(os.path.dirname(dirpath)))  # LOD0 | <ws>
                unique_str = f"{lod}.{bare}" if lod.upper().startswith("LOD") else bare
                if "VGMap" not in data:
                    if (
                        no_vgmap is not None
                        and in_submesh_dir
                        and os.path.splitext(name)[0] == bare
                    ):
                        no_vgmap.append(
                            (unique_str, EFMIBoneMapBuilder._vgmap_absence_reason(data))
                        )
                    continue
                data = dict(data)
                data["__unique_str__"] = unique_str
                yield dirpath, data

    @staticmethod
    def _vgmap_absence_reason(payload: dict) -> str:
        """无 VGMap 键的 json 的状态摘要（B11 缺席方定位用，纯读）。"""
        if payload.get("EFMILODProjectionSkipped") is True:
            return "跨 LOD 投影未匹配，已裁决跳过导入"
        if payload.get("EFMILODProjectionMatched") is True:
            return "跨 LOD 投影匹配（CPU/无顶点组目标，本就不含 VGMap）"
        if payload.get("VGMapAlgorithmVersion") or payload.get("VGCount"):
            return "VGMap 键族被部分清空（半成品/被外部清理）"
        return "从未写回或已被清理，无任何状态标记"

    @staticmethod
    def _describe_declaration_gaps(
        workspace_root: str,
        declared_segments: list[tuple[int, int]],
        pool_lo: int,
        pool_hi: int,
        scan_diag: dict | None,
    ) -> str:
        """B11 定位后缀：无人声明的空洞区间 + 未贡献声明段的 json 清单。

        旧版 B11 报错只给出「越界引用方」（谁引用了不存在的槽位），用户必须自己
        按 VGOffset 反查「谁本该声明却没声明」。这里把缺席侧直接列出来：
        - 空洞 = 声明段并集在 [pool_lo, pool_hi) 内的补集，也就是越界引用指向的
          区间（B11 注释里的「段间/段内空洞」）；
        - 未计入声明段的 json（解析失败 / 解析成功但无 VGMap 键），缺席方通常是
          其中的投影未匹配部件、本次未产出映射的部件或被外部清理过的 json。
        纯读诊断，不改变 fail-closed 语义。
        """
        parts: list[str] = []
        holes: list[tuple[int, int]] = []
        cursor = pool_lo
        for seg_start, seg_end in sorted(declared_segments):
            if seg_end <= cursor:
                continue
            if seg_start > cursor:
                holes.append((cursor, seg_start))
            cursor = seg_end
        if cursor < pool_hi:
            holes.append((cursor, pool_hi))
        if holes:
            shown = "、".join(f"[{start},{end})" for start, end in holes[:8])
            trailer = "…" if len(holes) > 8 else ""
            parts.append(
                f"无人声明的槽位空洞 {shown}{trailer}（共 {len(holes)} 段，"
                "越界引用落于其中）"
            )
        unreadable = list((scan_diag or {}).get("unreadable", []) or [])
        if unreadable:
            def _brief(path: str) -> str:
                try:
                    return os.path.relpath(path, workspace_root)
                except Exception:
                    return os.path.basename(path)
            shown = "、".join(
                f"{_brief(path)}（{reason}）" for path, reason in unreadable[:5]
            )
            trailer = "…" if len(unreadable) > 5 else ""
            parts.append(
                f"json 解析失败未计入声明段 {len(unreadable)} 个: {shown}{trailer}"
            )
        no_vgmap = list((scan_diag or {}).get("no_vgmap", []) or [])
        if no_vgmap:
            shown = "、".join(f"{unique_str}[{reason}]" for unique_str, reason in no_vgmap[:8])
            trailer = "…" if len(no_vgmap) > 8 else ""
            parts.append(
                f"无 VGMap 键未计入声明段 {len(no_vgmap)} 个: {shown}{trailer}"
            )
        if not parts:
            return ""
        listed_json = bool(unreadable or no_vgmap)
        if listed_json:
            tail = (
                "。缺席方通常就在上面这些 json 里：其声明段整体消失，而其它部件的"
                "去重借位仍指向它占过的槽位。请在确认没有外部进程反复清理工作区"
                "json 后，执行「清除骨骼合并VGMap缓存」并重新导入整角色（重建后本表"
                "按新布局重新对账）"
            )
        else:
            tail = (
                "。空洞通常来自「声明段整体消失」的部件 json（未写回 / 被清理 / 投影"
                "跳过），上面的 json 清单为空说明这些文件已从工作区消失或从未生成："
                "请确认该区间的部件 json 是否存在，再执行「清除骨骼合并VGMap缓存」并"
                "重新导入整角色（重建后本表按新布局重新对账）"
            )
        return "；" + "；".join(parts) + tail

    @staticmethod
    def _diffusion_radius(points: numpy.ndarray) -> float:
        """估计一个扩散采样的空间影响半径。

        EFMI 的 Position.buf 是绑定姿态空间，网格密度因部件而异；用
        包围盒尺度/采样数估计局部间距，并限制在合理范围，避免稀疏部件
        的单个远点把整个场错误连起来。
        """
        if len(points) < 2:
            return 0.05
        extent = float(numpy.linalg.norm(
            numpy.max(points, axis=0) - numpy.min(points, axis=0)
        ))
        spacing = extent / max(float(numpy.sqrt(len(points))), 1.0)
        return float(numpy.clip(spacing * 0.25, 0.02, 0.20))

    @staticmethod
    def _estimate_diffusion_normals(
        points: numpy.ndarray,
        neighbor_count: int = 8,
        *,
        use_native: bool = False,
    ) -> numpy.ndarray:
        """从点云局部 PCA 估计表面法向；无法判定为表面时返回 NaN。

        这不是网格拓扑法向（Position.buf 不携带面/边连接），但能识别
        “大腿表面/丝袜表面”这种两层近似平行的点云。线状或体积状点云
        不会强行套用表面投影规则，继续走原来的接触距离门控。
        """
        points = numpy.asarray(points, dtype=numpy.float32)
        normals = numpy.full((len(points), 3), numpy.nan, dtype=numpy.float32)
        if len(points) < 4 or points.ndim != 2 or points.shape[1] != 3:
            return normals

        neighbor_count = max(3, min(int(neighbor_count), len(points) - 1))
        distances = None
        if use_native:
            from .skeleton_fusion_native import squared_point_distances

            distances = squared_point_distances(points)

        # Keep temporary distance arrays bounded and reuse LAPACK's batched entry point.
        for start in range(0, len(points), 64):
            batch = points[start:start + 64]
            if distances is None:
                delta = points[None, :, :] - batch[:, None, :]
                squared = numpy.sum(delta * delta, axis=2)
            else:
                squared = distances[start:start + len(batch)]
            order = numpy.argsort(squared, axis=1)
            neighbors = points[order[:, 1:neighbor_count + 1]]
            centered = neighbors - numpy.mean(neighbors, axis=1, keepdims=True)
            covariance = centered.swapaxes(1, 2) @ centered / neighbor_count
            finite = numpy.isfinite(covariance).all(axis=(1, 2))
            try:
                eigenvalues, eigenvectors = numpy.linalg.eigh(covariance[finite])
            except numpy.linalg.LinAlgError:
                # Preserve valid rows if one covariance matrix fails to converge.
                eigenvalues = numpy.full((int(finite.sum()), 3), numpy.nan)
                eigenvectors = numpy.full((len(eigenvalues), 3, 3), numpy.nan)
                for row, matrix in enumerate(covariance[finite]):
                    try:
                        eigenvalues[row], eigenvectors[row] = numpy.linalg.eigh(matrix)
                    except numpy.linalg.LinAlgError:
                        continue
            eigenvalues = eigenvalues.astype(numpy.float64)
            largest = eigenvalues[:, -1]
            # Plane-like neighborhoods have two large eigenvalues.
            planar = (
                (largest > 1e-10)
                & (eigenvalues[:, 0] <= 0.20 * largest)
                & (eigenvalues[:, 1] >= 0.10 * largest)
            )
            vectors = eigenvectors[planar, :, 0]
            lengths = numpy.linalg.norm(vectors, axis=1)
            usable = lengths > 1e-8
            rows = start + numpy.flatnonzero(finite)[planar][usable]
            normals[rows] = vectors[usable] / lengths[usable, None]
        return normals

    @staticmethod
    def _nearest_diffusion_points(
        source: numpy.ndarray,
        target: numpy.ndarray,
    ) -> tuple[numpy.ndarray, numpy.ndarray]:
        """返回 source 每个点在 target 中的**全局**最近距离和索引。

        旧版只检查所在均匀网格的相邻 27 个 cell；只要相邻 cell 恰好存在
        一个候选，就不会扫描更远 cell，即使后者的欧氏距离实际更小。平行层、
        凹槽边缘和非均匀三角网格都会触发这种漏检。扩散签名最多 256 点，按
        source 分块做精确向量化最近邻既确定又有界，也比逐点 Python 网格循环快。
        """
        if len(source) == 0 or len(target) == 0:
            return (
                numpy.full(len(source), numpy.inf, dtype=numpy.float32),
                numpy.full(len(source), -1, dtype=numpy.int64),
            )
        source = numpy.asarray(source, dtype=numpy.float32)
        target = numpy.asarray(target, dtype=numpy.float32)
        distances = numpy.empty(len(source), dtype=numpy.float32)
        indices = numpy.empty(len(source), dtype=numpy.int64)
        for start in range(0, len(source), 64):
            chunk = source[start:start + 64]
            delta = chunk[:, None, :] - target[None, :, :]
            squared = numpy.sum(delta * delta, axis=2)
            nearest = numpy.argmin(squared, axis=1)
            indices[start:start + len(chunk)] = nearest
            distances[start:start + len(chunk)] = numpy.sqrt(
                squared[numpy.arange(len(chunk)), nearest]
            )
        return distances, indices

    @staticmethod
    def _nearest_compatible_diffusion_points(
        source: numpy.ndarray,
        target: numpy.ndarray,
        source_normals: numpy.ndarray,
        target_normals: numpy.ndarray,
        contact_distance: float,
        layer_distance: float,
        tangent_tolerance: float,
        normal_alignment: float,
    ) -> tuple[numpy.ndarray, numpy.ndarray]:
        """在层间几何约束内查找最近点，而不是先取最近点再做拒绝。

        多层裙摆等点云里，欧氏最近点可能来自相邻的错误层。旧流程选中该点后
        才检查法向/切向约束，失败时不会继续搜索稍远但层级正确的点。这里先对
        每个点对应用距离、切向和法向约束，再从仍兼容的点里取最近者。
        """
        source = numpy.asarray(source, dtype=numpy.float32)
        target = numpy.asarray(target, dtype=numpy.float32)
        if len(source) == 0 or len(target) == 0:
            return (
                numpy.full(len(source), numpy.inf, dtype=numpy.float32),
                numpy.full(len(source), -1, dtype=numpy.int64),
            )

        source_normals = numpy.asarray(source_normals, dtype=numpy.float32)
        target_normals = numpy.asarray(target_normals, dtype=numpy.float32)
        source_shape_ok = source_normals.shape == (len(source), 3)
        target_shape_ok = target_normals.shape == (len(target), 3)
        source_valid_all = (
            numpy.isfinite(source_normals).all(axis=1)
            if source_shape_ok else numpy.zeros(len(source), dtype=bool)
        )
        target_valid_all = (
            numpy.isfinite(target_normals).all(axis=1)
            if target_shape_ok else numpy.zeros(len(target), dtype=bool)
        )
        safe_source_normals = (
            numpy.where(numpy.isfinite(source_normals), source_normals, 0.0)
            if source_shape_ok else numpy.zeros((len(source), 3), dtype=numpy.float32)
        )
        safe_target_normals = (
            numpy.where(numpy.isfinite(target_normals), target_normals, 0.0)
            if target_shape_ok else numpy.zeros((len(target), 3), dtype=numpy.float32)
        )

        distances = numpy.full(len(source), numpy.inf, dtype=numpy.float32)
        indices = numpy.full(len(source), -1, dtype=numpy.int64)
        for start in range(0, len(source), 64):
            chunk = source[start:start + 64]
            chunk_len = len(chunk)
            displacement = target[None, :, :] - chunk[:, None, :]
            squared = numpy.sum(displacement * displacement, axis=2)
            euclidean = numpy.sqrt(squared)

            source_valid = source_valid_all[start:start + chunk_len]
            paired_surface = source_valid[:, None] & target_valid_all[None, :]
            allowed_distance = numpy.where(
                paired_surface,
                float(layer_distance),
                float(contact_distance),
            )
            compatible = euclidean <= allowed_distance

            if numpy.any(source_valid):
                source_normal = safe_source_normals[start:start + chunk_len]
                normal_component = numpy.sum(
                    displacement * source_normal[:, None, :], axis=2
                )
                tangent = (
                    displacement
                    - normal_component[:, :, None] * source_normal[:, None, :]
                )
                tangent_distance = numpy.linalg.norm(tangent, axis=2)
                compatible &= (~source_valid[:, None]) | (
                    tangent_distance <= float(tangent_tolerance)
                )

            if numpy.any(target_valid_all):
                target_component = numpy.sum(
                    displacement * safe_target_normals[None, :, :], axis=2
                )
                tangent = (
                    displacement
                    - target_component[:, :, None] * safe_target_normals[None, :, :]
                )
                tangent_distance = numpy.linalg.norm(tangent, axis=2)
                compatible &= (~target_valid_all[None, :]) | (
                    tangent_distance <= float(tangent_tolerance)
                )

            if numpy.any(paired_surface):
                alignment = numpy.abs(
                    safe_source_normals[start:start + chunk_len]
                    @ safe_target_normals.T
                )
                compatible &= (~paired_surface) | (
                    alignment >= float(normal_alignment)
                )

            compatible_squared = numpy.where(compatible, squared, numpy.inf)
            nearest = numpy.argmin(compatible_squared, axis=1)
            nearest_squared = compatible_squared[numpy.arange(chunk_len), nearest]
            found = numpy.isfinite(nearest_squared)
            if numpy.any(found):
                found_indices = numpy.flatnonzero(found)
                output_indices = start + found_indices
                indices[output_indices] = nearest[found]
                distances[output_indices] = numpy.sqrt(nearest_squared[found])
        return distances, indices

    @classmethod
    def weight_diffusion_similarity(
        cls,
        signature_a: dict | None,
        signature_b: dict | None,
        distance_tolerance: float = 0.05,
        weight_tolerance: float = 0.20,
        min_coverage: float = 0.30,
        layer_distance: float = 0.15,
        tangent_tolerance: float = 0.05,
        normal_alignment: float = 0.70,
        weak_weight_floor: float = 0.25,
        min_support_points: int = 2,
        return_metrics: bool = False,
    ) -> bool | dict:
        """检测两个顶点组在空间接触处是否扩散出一致的权重场。

        每个方向都把本组的正权重点投影到另一组最近的正权重点。普通点云
        仍要求落在接触半径内；当两边能估计出局部表面法向时，允许沿法向
        存在一段层间距，并要求切向投影误差小、两层法向平行。这覆盖
        “大腿表面 + 悬空一小段的丝袜表面”而不要求共享顶点或拓扑连接。
        最终取覆盖率较高的方向。为避免“强权重点淹没弱权重点”，每个正权重
        点的评估权重至少达到该方向最大权重的 ``weak_weight_floor``；同时要求
        至少 ``min_support_points`` 个不同源点与不同目标点匹配。这只是评估用的
        最低影响，不会修改写回的原始蒙皮权重。``return_metrics`` 仅供候选歧义
        消解复用同一次扫描得到的覆盖率、权重误差和空间误差；默认仍返回 bool。
        """
        def finish(
            passes: bool,
            coverage: float = 0.0,
            weight_error: float = float("inf"),
            spatial_error: float = float("inf"),
            coverage_ab: float = 0.0,
            coverage_ba: float = 0.0,
        ):
            metrics = {
                "passes": bool(passes),
                "coverage": float(coverage),
                "weight_error": float(weight_error),
                "spatial_error": float(spatial_error),
                "coverage_ab": float(coverage_ab),
                "coverage_ba": float(coverage_ba),
            }
            return metrics if return_metrics else bool(passes)

        if not signature_a or not signature_b:
            return finish(False)
        points_a = numpy.asarray(signature_a.get("diffusion_points", []), dtype=numpy.float32)
        weights_a = numpy.asarray(signature_a.get("diffusion_weights", []), dtype=numpy.float32)
        points_b = numpy.asarray(signature_b.get("diffusion_points", []), dtype=numpy.float32)
        weights_b = numpy.asarray(signature_b.get("diffusion_weights", []), dtype=numpy.float32)
        if (
            len(points_a) == 0 or len(points_b) == 0
            or len(points_a) != len(weights_a) or len(points_b) != len(weights_b)
            or points_a.ndim != 2 or points_b.ndim != 2
            or points_a.shape[1] != 3 or points_b.shape[1] != 3
            or weights_a.ndim != 1 or weights_b.ndim != 1
        ):
            return finish(False)

        radius_a = float(signature_a.get("diffusion_radius", cls._diffusion_radius(points_a)))
        radius_b = float(signature_b.get("diffusion_radius", cls._diffusion_radius(points_b)))
        contact_distance = max(float(distance_tolerance), radius_a, radius_b)
        # 跨层投影有明确上限；没有成对可靠法向时仍走 contact_distance。
        layer_distance = min(max(float(layer_distance), contact_distance), 0.15)
        tangent_tolerance = max(float(tangent_tolerance), float(distance_tolerance))

        normals_a = signature_a.get("diffusion_normals")
        if "diffusion_normals" not in signature_a:
            normals_a = cls._estimate_diffusion_normals(points_a)
        normals_a = numpy.asarray(normals_a, dtype=numpy.float32)
        normals_b = signature_b.get("diffusion_normals")
        if "diffusion_normals" not in signature_b:
            normals_b = cls._estimate_diffusion_normals(points_b)
        normals_b = numpy.asarray(normals_b, dtype=numpy.float32)
        normals_a_valid = (
            normals_a.ndim == 2 and normals_a.shape == (len(points_a), 3)
            and numpy.isfinite(normals_a).all(axis=1)
        )
        normals_b_valid = (
            normals_b.ndim == 2 and normals_b.shape == (len(points_b), 3)
            and numpy.isfinite(normals_b).all(axis=1)
        )
        # 只有两侧都能提供至少一个可靠法向，才打开层间走廊；单侧/局部
        # 法向缺失的点对继续使用严格接触半径，避免把体积点云当成表面。
        has_surface_normals = bool(
            numpy.any(normals_a_valid) and numpy.any(normals_b_valid)
        )

        def directional(
            source_points,
            source_weights,
            target_points,
            target_weights,
            source_normals,
            target_normals,
        ):
            # 没有可靠表面法向时保持原来的“真实接触”距离；
            # 两层表面都有法向时，允许沿法向存在一小段层间距。
            if has_surface_normals:
                distances, nearest = cls._nearest_compatible_diffusion_points(
                    source_points,
                    target_points,
                    source_normals,
                    target_normals,
                    contact_distance,
                    layer_distance,
                    tangent_tolerance,
                    normal_alignment,
                )
            else:
                distances, nearest = cls._nearest_diffusion_points(
                    source_points, target_points
                )
            valid = nearest >= 0
            if not has_surface_normals:
                valid &= distances <= contact_distance
            # 最近邻不应被强制成双射：同一连续表面在两个部件上经常有完全
            # 不同的三角网格密度，高密度侧的多个点合理投影到低密度侧同一点。
            # 防止“整条槽边吸到一个孤立点”的方式改为要求至少多个不同目标
            # 支持点；只有目标本来就只有一个点时，另用局部范围限制处理。
            # 原始权重总量会让少量弱权重点几乎没有话语权（例如左/右两侧
            # 各有一个很弱的点，强中心点却能把错误匹配“冲淡”）。使用相对
            # 权重下限保留强度排序，同时让每个正权重样本都能影响判定。
            finite_positive = source_weights[numpy.isfinite(source_weights) & (source_weights > 0)]
            if len(finite_positive) == 0:
                return 0.0, float("inf"), float("inf")
            source_peak = float(numpy.max(finite_positive))
            floor = max(source_peak * max(float(weak_weight_floor), 0.0), 0.05)
            evaluation_weights = numpy.where(
                numpy.isfinite(source_weights) & (source_weights > 0),
                numpy.maximum(source_weights, floor),
                0.0,
            )
            total = float(numpy.sum(evaluation_weights))
            # 稀疏的凹槽底/装饰物可能只有 1~2 个正权重采样点，不能因为
            # 全局默认值较高就被强制拆开。两边都很稀疏时按可用点数动态下调，
            # 但仍至少保留一个真正的扩散配对作为证据。
            required_support = min(
                max(int(min_support_points), 1),
                len(source_points),
                len(target_points),
            )
            if total <= 1e-8 or int(numpy.count_nonzero(valid)) < required_support:
                return 0.0, float("inf"), float("inf")
            unique_target_support = len(numpy.unique(nearest[valid]))
            if unique_target_support < required_support:
                return 0.0, float("inf"), float("inf")
            evaluation_w = evaluation_weights[valid]
            # weak_weight_floor 只用于“每个采样点有多少评估影响”，不能
            # 覆写拿来比较的原始权重值；旧实现把 0.01 抬成 0.25 后再与
            # 另一侧真实 0.01 比，凭空制造了 0.24 的误差。
            source_w = source_weights[valid]
            target_w = target_weights[nearest[valid]]
            coverage = float(numpy.sum(evaluation_w) / total)
            error = float(numpy.sum(evaluation_w * numpy.abs(source_w - target_w)) /
                          max(float(numpy.sum(evaluation_w)), 1e-8))
            spatial_error = float(
                numpy.sum(evaluation_w * distances[valid])
                / max(float(numpy.sum(evaluation_w)), 1e-8)
            )
            return coverage, error, spatial_error

        cov_ab, err_ab, spatial_ab = directional(
            points_a, weights_a, points_b, weights_b, normals_a, normals_b
        )
        cov_ba, err_ba, spatial_ba = directional(
            points_b, weights_b, points_a, weights_a, normals_b, normals_a
        )
        if (cov_ab, -err_ab, -spatial_ab) >= (cov_ba, -err_ba, -spatial_ba):
            coverage, error, spatial_error = cov_ab, err_ab, spatial_ab
        else:
            coverage, error, spatial_error = cov_ba, err_ba, spatial_ba
        # 极稀疏的一侧只有一个采样点时没有“多个目标支持点”可用。此时
        # 要求另一侧本身局限在一个接触直径内，并且双向都有覆盖；这样保留
        # 单点小附件，同时拒绝一个点吸附整条长槽边。
        if min(len(points_a), len(points_b)) <= 1:
            larger = points_a if len(points_a) > len(points_b) else points_b
            extent = float(numpy.linalg.norm(
                numpy.max(larger, axis=0) - numpy.min(larger, axis=0)
            ))
            if extent > contact_distance * 2.0:
                return finish(
                    False, coverage, error, spatial_error, cov_ab, cov_ba
                )
            if min(cov_ab, cov_ba) < float(min_coverage):
                return finish(
                    False, coverage, error, spatial_error, cov_ab, cov_ba
                )
        passes = coverage >= float(min_coverage) and error <= float(weight_tolerance)
        return finish(
            passes, coverage, error, spatial_error, cov_ab, cov_ba
        )

    # ------------------------------------------------------------------
    # 骨骼矩阵读取
    # ------------------------------------------------------------------

    @staticmethod
    def load_skeleton_buffer_from_sources(
        instance_config_path: str,
        pool_path: str,
        first_constant: int,
    ) -> numpy.ndarray | None:
        """从 instance-config 原文件和骨骼池原文件提取组件骨骼段。

        该函数同时服务 FrameAnalysis 路径和工作空间缓存路径，保证两条路径执行
        完全相同的切片逻辑；``first_constant`` 是 log 绑定窗口起点，首次生成时
        会写入子网格 json，后续不再依赖 log。
        """
        try:
            cb_data = numpy.fromfile(
                instance_config_path, dtype=numpy.float32
            ).reshape(-1, 4)
        except Exception:
            return None

        first_constant = int(first_constant)
        if first_constant < 0:
            return None
        if first_constant + 16 > len(cb_data):
            return None
        instance_config = cb_data[first_constant:first_constant + 16]
        skeleton_offsets = instance_config[_INSTANCE_CONFIG_BONE_OFFSET_ROW][0:2].view(numpy.uint32)

        try:
            pool_data = numpy.fromfile(pool_path, dtype=numpy.float32).reshape(-1, 4)
        except Exception:
            return None

        # 取第一个非零骨骼段（current 主骨骼段，单帧）
        for offset_value in skeleton_offsets:
            offset = int(offset_value)
            if not offset:
                continue
            data_offset = offset + 3  # GLOBAL_RESERVED_ROWS = 3
            skeleton_raw = pool_data[data_offset:data_offset + _BONE_SEGMENT_FLOAT4]
            usable = (len(skeleton_raw) // 3) * 3
            if usable == 0:
                continue
            skeleton = skeleton_raw[:usable].reshape(-1, _BONE_MATRIX_FLOATS)
            return skeleton
        return None

    def get_skeleton_source(self, draw_index: str) -> dict | None:
        """解析 draw 的完整可缓存骨骼来源及已切出的组件骨骼段。"""
        instance_cb = self.parser.get_instance_config_cb(draw_index)
        if instance_cb is None:
            return None

        first_constant = int(instance_cb.get("first_constant", 0) or 0)
        cb_hash = instance_cb.get("hash", "")
        cb_logical = (
            f"{draw_index}-vs-cb{self._find_cb_slot(draw_index, cb_hash)}={cb_hash}"
        )
        instance_config_path = self.parser.get_deduped_path(cb_logical)
        if not instance_config_path:
            return None

        skeleton_t0_hash = self.parser.get_vs_t0(draw_index)
        if not skeleton_t0_hash:
            return None
        pool_logical = f"{draw_index}-vs-t0={skeleton_t0_hash}"
        pool_path = self.parser.get_deduped_path(pool_logical)
        if not pool_path:
            return None

        skeleton_buffer = self.load_skeleton_buffer_from_sources(
            instance_config_path,
            pool_path,
            first_constant,
        )
        if skeleton_buffer is None:
            return None
        return {
            "skeleton_buffer": skeleton_buffer,
            "instance_config_path": instance_config_path,
            "pool_path": pool_path,
            "first_constant": first_constant,
            "draw_index": str(draw_index),
        }

    def get_skeleton_buffer(self, draw_index: str) -> numpy.ndarray | None:
        """兼容接口：读取 draw 调用对应组件骨骼段矩阵数组。"""
        source = self.get_skeleton_source(draw_index)
        return source["skeleton_buffer"] if source is not None else None

    def _find_cb_slot(self, draw_index: str, cb_hash: str) -> int:
        for (idx, stage, slot), binding in self.parser.cb_bindings.items():
            if idx == draw_index and stage == "V" and binding.get("hash") == cb_hash:
                return slot
        return 0

    # ------------------------------------------------------------------
    # 跨子网格 vg_map 构建
    # ------------------------------------------------------------------

    @staticmethod
    def build_vg_maps(
        submesh_skeletons: dict[str, tuple],
        match_tolerance: float = 1e-3,
        centroid_tolerance: float = 0.02,
        diffusion_distance: float = 0.05,
        diffusion_weight_tolerance: float = 0.20,
        diffusion_min_coverage: float = 0.30,
        diffusion_layer_distance: float = 0.15,
        diffusion_tangent_tolerance: float = 0.05,
        diffusion_normal_alignment: float = 0.70,
        diffusion_weak_weight_floor: float = 0.25,
        diffusion_min_support_points: int = 2,
        protected_pairs: set[tuple[tuple[str, int], tuple[str, int]]] | None = None,
        constraint_labels: dict[tuple[str, int], object] | None = None,
        deduplicate: bool | None = None,
        lifetime_domains: dict[str, object] | None = None,
        dedup_excluded: set[str] | None = None,
    ) -> tuple[dict[str, dict], dict[str, int]]:
        """跨子网格按"矩阵硬门控 + 权重扩散确认"去重构建 vg_map（同部件不去重）。

        总开关 _DEDUP_ENABLED（默认 True；关闭时仅用于诊断/回滚）；
        置 False 时退化为恒等映射（local → vg_offset + local），下方并查集逻辑不执行。

        参数:
            unique_str -> (skeleton_buffer, vg_count, weighted_vertex_counts[, signatures])
            - signatures: {local: {centroid, bbox_min/max, vertex_count, spread,
              weight_total, diffusion_points, diffusion_weights}}
              （扩散字段参与近似矩阵判定，其余字段供回退/调试）
            - protected_pairs: 已由跨 LOD 原始候选对应层确认“不可合并”的候选对。
              这些对在本 LOD 内强制断边，用于防止一侧过度去重吞掉另一侧能区分的组。
            - constraint_labels: 跨 LOD 迭代产生的临时语义标签。两个候选都已有
              标签且标签不同时，其并查集组不可合并；无标签候选可附着到一侧，
              但不能再作为桥把两个不同标签的组串起来。
            - lifetime_domains: 组件级“运行时更新生命周期”域（unique_str ->
              域标签，如 frozenset 的 LOD 集合）。跨组件去重共享槽位只在其
              生命周期域相同时才安全：槽由维护组件在其绘制距离写入，若某网格
              出现的距离下维护者不绘制，槽就无人写入（R1 悬空槽）。
              两个候选都带域且域不同时断边；缺域候选可附着到一侧，但不能再
              作为桥把两个不同域的组串起来（与 constraint_labels 同构）。
            - dedup_excluded: 组件级“排除去重”集合（unique_str 集合）。集合内
              组件的每根骨骼**不参与任何去重合并**（含组内/跨组件）：其 VGMap
              恒为恒等映射（local -> vg_offset + local，独占自己声明段内的槽位），
              其它组件也无法把骨骼并入它的槽位。用于用户指定“此部件骨骼必须
              自持、不得被并入他段”的部件（如剔除导出但仍保留占位的部件）。
              对应子网格 json 标记键：``VGMapDedupExcluded=True``。
        返回: (vg_maps, vg_offsets)

        去重判据（分层，矩阵是必要条件——几何接近无权推翻矩阵不一致）：
        1. **矩阵 diff >= match_tolerance：永不合并**。
           实测定案"误合并有害、漏合并无害"（容差误并手指两节导致功能丢失；
           漏并仅多占槽位，蒙皮仍正确）。
        2. **矩阵 bitwise 完全相同（diff == 0）**：有扩散采样时仍需通过接触
           权重一致性；缺少采样时兼容参考插件直接合并。
        3. **0 < diff < match_tolerance：优先做权重扩散确认**。
           将每个组的正权重点视为空间扩散场，在另一个组的正权重点上做最近邻
           采样；普通点云要求接触半径内，能估计局部表面法向时允许沿法向
           存在层间距，但切向误差和法向夹角必须通过；接触位置覆盖率达到 30%，
           原始权重平均误差不超过 0.20 才合并。这能识别“大腿 + 丝袜”而不要求
           共享顶点或整体质心相同。
        4. 没有扩散采样时回退到加权质心距离 < centroid_tolerance；两者都缺失
           则保守不合并（bitwise 完全相同仍保留直接合并的兼容语义）。

        废止记录：此前的"多维度投票"（矩阵/质心/包围盒/扩散球 vote>=2）把矩阵
        降为可输的一票——几何接近度会同时通过并推翻矩阵反对票；实测"测试"工作空间
        08-10 dump 上 195 个合并组中 42 组矩阵差异 > 1e-3（最高 0.27）。当前扩散
        判据只在矩阵硬门内使用“接触位置权重值”确认，不允许几何接近单独促成合并。

        **同部件冲突拒绝**（硬性规则）：并查集 union 时检查，组内同部件最多 1 个 local，
        防"同位置功能骨骼"（如手指两节）合并及链式绕过。

        **歧义候选一对一消解**：每对 Component 内先收集所有通过硬门控的边，再同步
        收紧证据等级、矩阵差、权重误差、覆盖缺口和空间误差。A:1 同时命中 B:8/B:9
        时只保留扩散相似度更高的一条；完全相同才用稳定 id 决胜，结果不依赖遍历先后。

        **连续扩散图判定**：每个成员必须通过至少一条权重扩散边连接到组，
        并在并入后对整组重新做连通性检查；不要求平面和每一个凹槽底直接
        两两相交，允许“平面 → 槽壁/槽底”的连续桥接，同时禁止孤立断点。

        参数（可实测调整）:
            match_tolerance: 矩阵硬门控上限（默认 1e-3，达到即不合并）。
            centroid_tolerance: 无扩散采样时的质心回退阈值（默认 0.02）。
            diffusion_distance: 接触点基础距离阈值（默认 0.05）。
            diffusion_weight_tolerance: 接触点原始权重平均误差（默认 0.20）。
            diffusion_min_coverage: 一侧扩散场需被另一侧覆盖的最小比例（默认 0.30）。
            diffusion_layer_distance: 平行/错位表面允许的法向扩散走廊（默认 0.15）。
            diffusion_tangent_tolerance: 表面投影允许的切向误差（默认 0.05）。
            diffusion_normal_alignment: 两层局部法向最小绝对点积（默认 0.70）。
            diffusion_weak_weight_floor: 弱权重点的最小评估权重，占该方向最大
                权重的比例（默认 0.25；同时有 0.05 的绝对下限）。
            diffusion_min_support_points: 每个方向至少需要的唯一配对点数（默认 2，
                对稀疏组按实际采样数动态下调到至少 1）。
            deduplicate: 显式设为 False 时只建立恒等槽位映射，供目标 LOD 同步使用，
                不运行权重扩散/并查集去重；省略时使用全局 _DEDUP_ENABLED。
        """
        # 收集所有骨骼候选
        candidates: list[dict] = []
        offset = 0
        vg_offsets: dict[str, int] = {}

        for unique_str in sorted(submesh_skeletons.keys()):
            entry = submesh_skeletons[unique_str]
            skeleton_buffer = entry[0]
            vg_count = entry[1]
            weighted_vertex_counts = entry[2] if len(entry) > 2 else None
            signatures = entry[3] if len(entry) > 3 else {}

            if skeleton_buffer is None or vg_count <= 0:
                continue
            if len(skeleton_buffer) < vg_count:
                print(
                    f"[EFMI骨骼合并] 警告: {unique_str} 骨骼段仅 {len(skeleton_buffer)} 根骨骼，"
                    f"但声明了 {vg_count} 个顶点组，跳过该子网格参与合并。"
                )
                continue

            vg_offsets[unique_str] = offset
            for vg_id in range(vg_count):
                bone = skeleton_buffer[vg_id]
                is_zero_bone = bool(numpy.all(bone == 0))
                weighted_count = (
                    int(weighted_vertex_counts[vg_id])
                    if weighted_vertex_counts is not None and vg_id < len(weighted_vertex_counts)
                    else 0
                )
                candidates.append({
                    "unique_str": unique_str,
                    "local_vg_id": vg_id,
                    "global_vg_id": offset + vg_id,
                    "weighted_vertex_count": weighted_count,
                    "bone": bone,
                    "signature": signatures.get(vg_id),
                    # 全零矩阵没有可比较的骨骼语义：保留自己的运行时槽位，
                    # 但绝不能因 bitwise 相同而跨 Component 合并。
                    "is_zero_bone": is_zero_bone,
                })
            offset += vg_count

        n = len(candidates)
        if n == 0:
            return {}, {}

        dedup_enabled = _DEDUP_ENABLED if deduplicate is None else bool(deduplicate)
        if not dedup_enabled:
            # 恒等映射：每根骨骼独占全局槽位（候选收集阶段已按
            # global_vg_id = vg_offset + local_vg_id 分配），不做任何合并。
            if deduplicate is None:
                print(
                    f"[EFMI骨骼合并] 顶点组去重已全局关闭（_DEDUP_ENABLED=False），"
                    f"{n} 根骨骼全部独占槽位（恒等映射，无任何合并）。"
                )
            identity_maps: dict[str, dict] = {}
            for cand in candidates:
                identity_maps.setdefault(cand["unique_str"], {})[
                    cand["local_vg_id"]
                ] = cand["global_vg_id"]
            return identity_maps, vg_offsets

        # 组件级“排除去重”（VGMapDedupExcluded=True）：被排除组件的每根骨骼
        # 从候选池中取出，预建恒等映射（local -> 自己段的 global 槽位），
        # 不参与下方任何并查集/扩散合并；其它组件也不能把骨骼并入其槽位。
        # 槽位分配（offset 累加）已在上方候选收集阶段完成，排除不会改变布局。
        excluded_set: set[str] = set(dedup_excluded or ())
        excluded_maps: dict[str, dict] = {}
        if excluded_set:
            excluded_cands = [
                c for c in candidates if c["unique_str"] in excluded_set
            ]
            if excluded_cands:
                for cand in excluded_cands:
                    excluded_maps.setdefault(cand["unique_str"], {})[
                        cand["local_vg_id"]
                    ] = cand["global_vg_id"]
                active_cands = [
                    c for c in candidates if c["unique_str"] not in excluded_set
                ]
                if not active_cands:
                    return excluded_maps, vg_offsets
                candidates = active_cands
                n = len(candidates)

        parent = list(range(n))
        group_submeshes: list[set] = [{candidates[i]["unique_str"]} for i in range(n)]
        # 每组的所有成员索引（用于合并后的权重扩散连通性校验）
        group_members: list[list[int]] = [[i] for i in range(n)]

        def find(x):
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        mats = numpy.stack([c["bone"] for c in candidates]).astype(numpy.float64)

        def _sig(idx):
            return candidates[idx].get("signature")

        def evaluate(i, j) -> dict:
            """分层判据（两两独立）及后续歧义消解所需的连续指标。

            - 矩阵 diff >= match_tolerance：永不合并（几何接近无权推翻）；
            - 矩阵 bitwise 完全相同：有扩散采样时仍验证权重场，无采样时兼容直接通过；
            - 近似：优先验证权重扩散，无扩散时才用加权质心距离（缺签名保守拒绝）。
            """
            matrix_diff = float(numpy.abs(mats[i] - mats[j]).max())
            result = {
                "passes": False,
                "evidence_rank": 3,
                "matrix_diff": matrix_diff,
                "coverage": 0.0,
                "weight_error": float("inf"),
                "spatial_error": float("inf"),
            }
            if matrix_diff >= match_tolerance:
                return result
            si, sj = _sig(i), _sig(j)
            if matrix_diff == 0.0 and (si is None or sj is None):
                # 没有 Position/Blend 扩散证据时保留参考插件的 bitwise
                # 兼容语义；有证据则必须验证接触位置的权重场。
                result.update({
                    "passes": True,
                    "evidence_rank": 1,
                    "weight_error": 0.0,
                    "spatial_error": 0.0,
                })
                return result
            if si is None or sj is None:
                return result

            # 两边都有 Position/Blend 生成的扩散采样时，扩散场是主判据；
            # 质心仅作为旧缓存/测试签名的兼容回退，不能覆盖已观测到的
            # 接触位置权重冲突。
            has_diffusion = (
                len(si.get("diffusion_points", [])) > 0
                and len(si.get("diffusion_weights", [])) > 0
                and len(sj.get("diffusion_points", [])) > 0
                and len(sj.get("diffusion_weights", [])) > 0
            )
            if has_diffusion:
                # 包围盒完全分离且间隙超过扩散半径时不可能存在接触
                # 证据，先在这里剪枝，避免对所有候选执行最近邻扫描。
                try:
                    a_min = numpy.asarray(si["bbox_min"], dtype=numpy.float64)
                    a_max = numpy.asarray(si["bbox_max"], dtype=numpy.float64)
                    b_min = numpy.asarray(sj["bbox_min"], dtype=numpy.float64)
                    b_max = numpy.asarray(sj["bbox_max"], dtype=numpy.float64)
                    gap_vec = numpy.maximum(numpy.maximum(a_min - b_max, b_min - a_max), 0.0)
                    gap = float(numpy.linalg.norm(gap_vec))
                    radius = max(
                        float(diffusion_distance),
                        float(diffusion_layer_distance),
                        EFMIBoneMapBuilder._diffusion_radius(
                            numpy.asarray(si["diffusion_points"], dtype=numpy.float32)
                        ),
                        EFMIBoneMapBuilder._diffusion_radius(
                            numpy.asarray(sj["diffusion_points"], dtype=numpy.float32)
                        ),
                    )
                    if gap > radius:
                        return result
                except (KeyError, TypeError, ValueError):
                    # 外部调用者可只提供 diffusion_points/weights；字段不全
                    # 时交给最近邻函数做保守判定。
                    pass
                metrics = EFMIBoneMapBuilder.weight_diffusion_similarity(
                    si,
                    sj,
                    distance_tolerance=diffusion_distance,
                    weight_tolerance=diffusion_weight_tolerance,
                    min_coverage=diffusion_min_coverage,
                    layer_distance=diffusion_layer_distance,
                    tangent_tolerance=diffusion_tangent_tolerance,
                    normal_alignment=diffusion_normal_alignment,
                    weak_weight_floor=diffusion_weak_weight_floor,
                    min_support_points=diffusion_min_support_points,
                    return_metrics=True,
                )
                result.update({
                    "passes": bool(metrics["passes"]),
                    "evidence_rank": 0,
                    "coverage": float(metrics["coverage"]),
                    "weight_error": float(metrics["weight_error"]),
                    "spatial_error": float(metrics["spatial_error"]),
                })
                return result
            if matrix_diff == 0.0:
                result.update({
                    "passes": True,
                    "evidence_rank": 1,
                    "weight_error": 0.0,
                    "spatial_error": 0.0,
                })
                return result
            dist = float(numpy.linalg.norm(
                si["centroid"].astype(numpy.float64) - sj["centroid"].astype(numpy.float64)
            ))
            result.update({
                "passes": dist < centroid_tolerance,
                "evidence_rank": 2,
                "weight_error": 0.0,
                "spatial_error": dist,
            })
            return result

        pair_evaluation_cache: dict[tuple[int, int], dict] = {}

        normalized_protected_pairs: set[tuple[tuple[str, int], tuple[str, int]]] = set()
        for pair in protected_pairs or ():
            try:
                left, right = pair
                left_key = (str(left[0]), int(left[1]))
                right_key = (str(right[0]), int(right[1]))
            except (TypeError, ValueError, IndexError):
                continue
            if left_key == right_key:
                continue
            normalized_protected_pairs.add(
                tuple(sorted((left_key, right_key)))
            )

        normalized_constraint_labels = {}
        for candidate_key, label in (constraint_labels or {}).items():
            try:
                key = (str(candidate_key[0]), int(candidate_key[1]))
            except (TypeError, ValueError, IndexError):
                continue
            normalized_constraint_labels[key] = label

        normalized_lifetime_domains = {
            str(unique_str): domain
            for unique_str, domain in (lifetime_domains or {}).items()
        }

        def _candidate_label(idx: int):
            candidate = candidates[idx]
            return normalized_constraint_labels.get((
                str(candidate["unique_str"]), int(candidate["local_vg_id"])
            ))

        def _candidate_domain(idx: int):
            return normalized_lifetime_domains.get(
                str(candidates[idx]["unique_str"])
            )

        group_labels: list[set] = []
        group_domains: list[set] = []
        for idx in range(n):
            label = _candidate_label(idx)
            group_labels.append(set() if label is None else {label})
            domain = _candidate_domain(idx)
            group_domains.append(set() if domain is None else {domain})

        def is_protected(i: int, j: int) -> bool:
            left = (str(candidates[i]["unique_str"]), int(candidates[i]["local_vg_id"]))
            right = (str(candidates[j]["unique_str"]), int(candidates[j]["local_vg_id"]))
            return tuple(sorted((left, right))) in normalized_protected_pairs

        def pair_evaluation(a, b) -> dict:
            key = (a, b) if a < b else (b, a)
            if key not in pair_evaluation_cache:
                if (
                    candidates[a].get("is_zero_bone")
                    or candidates[b].get("is_zero_bone")
                    or is_protected(a, b)
                ):
                    pair_evaluation_cache[key] = {
                        "passes": False,
                        "evidence_rank": 3,
                        "matrix_diff": float("inf"),
                        "coverage": 0.0,
                        "weight_error": float("inf"),
                        "spatial_error": float("inf"),
                    }
                else:
                    pair_evaluation_cache[key] = evaluate(a, b)
            return pair_evaluation_cache[key]

        def pair_passes(a, b) -> bool:
            return bool(pair_evaluation(a, b)["passes"])

        def _continuous_members(members: list[int]) -> bool:
            """检查合并出来的 VG 是否存在权重扩散断点。

            这是一个无向图：顶点是原始顶点组，边是通过矩阵硬门控和
            权重扩散确认的配对。只有整组连通才允许写回同一个 global VG；
            因而“平面 + 多个槽底”可以通过各自的局部桥接合并，但没有任何
            扩散证据的孤立物体永远不会被带进来。
            """
            if len(members) <= 1:
                return True
            visited = {members[0]}
            stack = [members[0]]
            member_set = set(members)
            while stack:
                current = stack.pop()
                for other in member_set - visited:
                    if candidates[current]["unique_str"] == candidates[other]["unique_str"]:
                        continue
                    if pair_passes(current, other):
                        visited.add(other)
                        stack.append(other)
            return len(visited) == len(member_set)

        def try_union(a, b):
            ra, rb = find(a), find(b)
            if ra == rb:
                return
            # 冲突拒绝：合并后某子网格在同组会有 >1 个 local → 拒绝
            if group_submeshes[ra] & group_submeshes[rb]:
                return
            # 跨 LOD 语义冲突拒绝：不同标签的两组不能被无标签候选桥接。
            if len(group_labels[ra] | group_labels[rb]) > 1:
                return
            # 生命周期域冲突拒绝（I1）：不同“运行时更新生命周期”域的组件共享
            # 槽位会在某 LOD 距离留下无人写入的悬空槽（R1）；与标签同构地
            # 禁止无域候选把两个不同域的组桥接起来。
            if len(group_domains[ra] | group_domains[rb]) > 1:
                return
            # 只要两组之间存在一条真实扩散桥即可尝试合并；随后对合并结果
            # 做整组连通性复核。这样不会把平面和每个凹槽底强行当作完全图，
            # 也不会允许没有任何桥接的孤立成员混入。
            if not any(
                pair_passes(mi, mj)
                for mi in group_members[ra]
                for mj in group_members[rb]
            ):
                return
            merged_members = group_members[ra] + group_members[rb]
            if not _continuous_members(merged_members):
                return
            parent[ra] = rb
            group_submeshes[rb] = group_submeshes[ra] | group_submeshes[rb]
            group_members[rb] = merged_members
            group_labels[rb] = group_labels[ra] | group_labels[rb]
            group_domains[rb] = group_domains[ra] | group_domains[rb]

        def edge_dimensions(edge) -> tuple[float, ...]:
            """把通过边投影到可共同收紧的无量纲“不相似度”维度。

            矩阵差放在第一维；候选冲突时先按有限矩阵级联消解，只有
            矩阵仍无法区分时才读取后续扩散维度。
            """
            _i, _j, evaluation = edge
            matrix_error = float(evaluation["matrix_diff"]) / max(
                float(match_tolerance), 1e-8
            )
            evidence_rank = float(evaluation["evidence_rank"])
            if int(evaluation["evidence_rank"]) == 0:
                weight_error = float(evaluation["weight_error"]) / max(
                    float(diffusion_weight_tolerance), 1e-8
                )
                coverage_error = max(0.0, 1.0 - float(evaluation["coverage"]))
                spatial_error = float(evaluation["spatial_error"]) / max(
                    float(diffusion_distance),
                    float(diffusion_layer_distance),
                    1e-8,
                )
            elif int(evaluation["evidence_rank"]) == 2:
                # 无扩散采样的质心回退仍可参与确定性一对一选择，但其证据
                # 等级低于真实扩散场，空间误差按自己的通过阈值归一化。
                weight_error = 0.0
                coverage_error = 1.0
                spatial_error = float(evaluation["spatial_error"]) / max(
                    float(centroid_tolerance), 1e-8
                )
            else:
                # bitwise 相同但无采样时没有扩散维度可比较；最后由矩阵及
                # 稳定的候选 id 决胜，保留旧数据的兼容合并语义。
                weight_error = 0.0
                coverage_error = 1.0
                spatial_error = 0.0
            return (
                matrix_error,
                evidence_rank,
                weight_error,
                coverage_error,
                spatial_error,
            )

        def choose_ambiguous_edge(edges: list[tuple[int, int, dict]]):
            """先有限收紧矩阵，再逐维收紧扩散指标，直到只剩唯一候选。

            初始集合已经通过 ``match_tolerance`` 硬门控。矩阵歧义阶段最多
            从 1e-3 逐级收紧到 1e-6；只要某一级只剩一个候选就立即返回，
            后续扩散维度不能把它抢走。到 1e-6 仍有多个候选时，才同步
            收紧证据等级、权重误差、覆盖缺口和空间误差；最后用稳定 id
            处理完全相同或维度互有胜负的候选。
            """
            contenders = list(edges)
            dimensions = {id(edge): edge_dimensions(edge) for edge in contenders}

            matrix_thresholds = [float(match_tolerance)]
            matrix_threshold = float(match_tolerance)
            while matrix_threshold > _MATRIX_AMBIGUITY_FLOOR:
                next_threshold = max(
                    _MATRIX_AMBIGUITY_FLOOR,
                    matrix_threshold / 10.0,
                )
                if next_threshold >= matrix_threshold:
                    break
                matrix_thresholds.append(next_threshold)
                matrix_threshold = next_threshold
                if matrix_threshold <= _MATRIX_AMBIGUITY_FLOOR:
                    break

            for matrix_threshold in matrix_thresholds:
                narrowed = [
                    edge for edge in contenders
                    if float(edge[2]["matrix_diff"]) < matrix_threshold
                ]
                if narrowed:
                    contenders = narrowed
                    if len(contenders) == 1:
                        return contenders[0]

            # 到矩阵下限仍无法区分，矩阵维度冻结，只处理后续维度。
            for factor in (0.75, 0.50, 0.25, 0.10, 0.05, 0.01, 0.0):
                mins = tuple(
                    min(dimensions[id(edge)][dimension] for edge in contenders)
                    for dimension in range(1, 5)
                )
                maxs = tuple(
                    max(dimensions[id(edge)][dimension] for edge in contenders)
                    for dimension in range(1, 5)
                )
                limits = tuple(
                    mins[dimension - 1]
                    + (maxs[dimension - 1] - mins[dimension - 1]) * float(factor)
                    for dimension in range(1, 5)
                )
                narrowed = [
                    edge for edge in contenders
                    if all(
                        dimensions[id(edge)][dimension] <= limits[dimension - 1] + 1e-9
                        for dimension in range(1, 5)
                    )
                ]
                if narrowed:
                    contenders = narrowed
                    if len(contenders) == 1:
                        return contenders[0]

            def stable_quality(edge):
                vector = dimensions[id(edge)]
                return (max(vector), sum(vector), vector, edge[0], edge[1])

            return min(contenders, key=stable_quality)

        # 每对 Component 先形成一个一对一候选集。同一个 A local 即使同时
        # 命中 B:8/B:9，也只能保留动态收紧后的最佳边；反向同理。之后仍由
        # 并查集的 group_submeshes 约束保证跨多个 Component 的组内唯一性。
        edges_by_component_pair: dict[
            tuple[str, str], list[tuple[int, int, dict]]
        ] = {}
        for i in range(n):
            for j in range(i + 1, n):
                component_i = str(candidates[i]["unique_str"])
                component_j = str(candidates[j]["unique_str"])
                if component_i == component_j:
                    continue
                evaluation = pair_evaluation(i, j)
                if not evaluation["passes"]:
                    continue
                labels = group_labels[i] | group_labels[j]
                if len(labels) > 1:
                    continue
                domains = group_domains[i] | group_domains[j]
                if len(domains) > 1:
                    continue
                component_pair = tuple(sorted((component_i, component_j)))
                edges_by_component_pair.setdefault(component_pair, []).append(
                    (i, j, evaluation)
                )

        selected_edges: list[tuple[int, int, dict]] = []
        for component_pair in sorted(edges_by_component_pair):
            remaining = list(edges_by_component_pair[component_pair])
            while remaining:
                chosen = choose_ambiguous_edge(remaining)
                selected_edges.append(chosen)
                chosen_members = {chosen[0], chosen[1]}
                remaining = [
                    edge for edge in remaining
                    if not chosen_members.intersection((edge[0], edge[1]))
                ]

        # 全局并入顺序也必须保持“矩阵优先”。之前这里先比较所有维度的
        # 最大值，可能让扩散误差较小但矩阵差明显更大的浮层边先占用槽位，
        # 使真正的同骨骼边在 try_union 的子网格冲突检查中被拒绝。
        selected_edges.sort(key=lambda edge: (
            edge_dimensions(edge),
            edge[0],
            edge[1],
        ))
        for i, j, _evaluation in selected_edges:
            if find(i) != find(j):
                try_union(i, j)

        # 分组。并查集合并过程中已经逐次检查过连通性；这里再做一次最终
        # 权重扩散图校验，并把任何意外断开的组件拆回独立 global VG，避免
        # “中途通过、最终写回却有孤岛”的缓存/调用方回归。
        raw_groups: dict[int, list[int]] = {}
        for i in range(n):
            raw_groups.setdefault(find(i), []).append(i)

        groups: dict[int, list[int]] = {}
        next_group_id = 0
        for members in raw_groups.values():
            remaining = set(members)
            while remaining:
                seed = next(iter(remaining))
                component = {seed}
                stack = [seed]
                remaining.remove(seed)
                while stack:
                    current = stack.pop()
                    for other in tuple(remaining):
                        if candidates[current]["unique_str"] == candidates[other]["unique_str"]:
                            continue
                        if pair_passes(current, other):
                            remaining.remove(other)
                            component.add(other)
                            stack.append(other)
                groups[next_group_id] = sorted(component)
                next_group_id += 1

        # 每组 canonical = 权重顶点数最多的候选
        vg_maps: dict[str, dict] = {}
        for root, members in groups.items():
            canonical_idx = max(members, key=lambda i: candidates[i]["weighted_vertex_count"])
            canonical_global = candidates[canonical_idx]["global_vg_id"]
            for i in members:
                cand = candidates[i]
                vg_maps.setdefault(cand["unique_str"], {})[cand["local_vg_id"]] = canonical_global

        # 合并“排除去重”组件的恒等映射（不参与并查集的槽位）
        for _excluded_unique_str, _identity_map in excluded_maps.items():
            vg_maps.setdefault(_excluded_unique_str, {}).update(_identity_map)

        return vg_maps, vg_offsets

    @staticmethod
    def project_non_reference_partitions(
        collected_by_lod: dict[str, dict[str, tuple]],
        correspondence: dict,
        reference_lod: str,
        projection_skip_by_lod: dict[str, set[str]] | None = None,
    ) -> tuple[dict[str, dict[str, dict]], dict[str, dict[str, int]], int]:
        """[已废弃 v9 投影语义] 遗留入口，见 build_independent_lod_maps。

        v9 曾把非基准 LOD 的槽位投影到基准分区，实测 LOD1 爆炸
        （同帧共用槽位 + 运行时每 component 每帧只导入一次骨骼 → LOD1 网格读到
        LOD0 矩阵）。该方案已经撤销，此函数保留仅作历史参考与兼容旧
        测试桩；新代码一律使用 build_independent_lod_maps。
        """
        raise RuntimeError(
            "project_non_reference_partitions（v9 投影）已废弃；"
            "请使用 EFMIBoneMapBuilder.build_independent_lod_maps（v10 独立+分段平移）。"
        )

    @staticmethod
    def _component_lifetime_domains(
        collected_by_lod: dict[str, dict[str, tuple]],
    ) -> dict[str, dict[str, frozenset]]:
        """按“跨 LOD 同 IB（同一 draw）”归组，计算每部件的运行时更新生命周期域。

        I1 共享槽安全域：组件 A/B 共享槽位时，槽由其中一方在其绘制距离写入
        （MergedSkeleton_AttachComponent 写当前 draw 自身声明段）。若某网格
        出现的 LOD 距离下槽维护者不绘制，槽无人写入（R1 悬空槽冻结）——
        因此只有**域相同**的组件才允许跨组件去重共享槽位：
        - same-IB 跨 LOD 部件（脸等：LOD0/LOD1 同一 draw 单入口）域 = 该 draw
          出现的全部 LOD（{LOD0, LOD1}）；
        - L0-only 组件（其 L1 版本是不同 IB/draw，独立 EntryPoint）域 = {LOD0}；
        - 独立 LOD1 部件域 = {LOD1}。

        域标签取 unique_str 首段 IB 哈希（与 submesh_metadata 的
        ``bare_unique_str.split("-")[0]`` 口径一致），忽略工作空间级缺前缀的
        单 LOD 场景（whole-string 作为 IB，域恒单元素，去重行为不变）。

        返回 {lod_name: {unique_str: frozenset(域内 LOD 名集合)}}。
        """
        def _ib_of(unique_str: str) -> str:
            # C8（P6 同源）：IB 判定键统一走模块级 efmi_ib_key（去 LOD 前缀 +
            # 首段），与导出侧 same-IB 折叠判定（efmi.py _baseline_draw_key 的
            # match_draw_ib 缺省回退）同源，杜绝三视图（去重域/折叠/导出校验）
            # 判定漂移。
            return efmi_ib_key(unique_str)

        presence: dict[str, set] = {}
        for lod_name, submeshes in (collected_by_lod or {}).items():
            for unique_str in submeshes:
                presence.setdefault(_ib_of(unique_str), set()).add(str(lod_name))

        domains_by_lod: dict[str, dict[str, frozenset]] = {}
        for lod_name, submeshes in (collected_by_lod or {}).items():
            domains_by_lod[lod_name] = {
                unique_str: frozenset(presence.get(_ib_of(unique_str), ()))
                for unique_str in submeshes
            }
        return domains_by_lod

    @staticmethod
    def build_independent_lod_maps(
        collected_by_lod: dict[str, dict[str, tuple]],
        reference_lod: str,
        projection_skip_by_lod: dict[str, set[str]] | None = None,
        correspondence: dict | None = None,
        deduplicate: bool | None = None,
    ) -> tuple[dict[str, dict[str, dict]], dict[str, dict[str, int]], int]:
        """把各 LOD 的槽位编号建成**相互独立、分段平移**的全局空间（v10/v11/v13）。

        v10（撤销 v9 共享槽位投影）：每个 LOD 都用**自己的 dump**
        独立执行权重扩散去重（build_vg_maps，槽位从 0 起、组内自洽），再把非基准
        LOD 的整个编号空间**平移到基准 LOD 段之后**：

        - 基准 LOD0：0..max0（唯一去重，不做任何平移）；
        - 非基准 LOD1：base 起（base = 基准段大小 = 基准组全部候选槽位数，
          即 0..370 之后从 371 起）——两域不相交、全局唯一，跨 LOD 零共享。

        v11（修复 v10 两点缺陷；v13 撤销“值域压缩”部分）：
        1. ~~值域空洞~~（v13 撤销）：曾把每个 LOD 的值域压缩为密集段（基准段
           [0,k0)、非基准段从上一段压缩后段尾起）——实测游戏内直接乱掉（L1
           值域侵入 L0 声明段区域，运行时按全局槽位/声明段混合索引时串扰）。
           撤销后恢复 v10 平移口径：值域 = 各 LOD 去重原生槽位 + 整体平移，
           非基准 LOD 值域 ⊆ [baseline_size, 其声明段尾)，跨 LOD 零共享。
        2. **两边去重不一致**：LOD1 自己的矩阵/扩散判据可能把 L0 同一合并组的
           多个对应组拆成多个槽位（L0 的 5/6 合并了，L1 对应的 11..17 却各自为政）。
           修复：传入跨 LOD 对应（``correspondence``），先给 LOD1 候选打上
           “所属 L0 合并组”标签（_build_cross_lod_constraint_labels，不同标签
           断边），再强制同标签候选共用同一槽位（_enforce_same_reference_group_
           single_slot，取组内最小槽、不改编号空间）——L0 合并 ⇒ L1 对应组
           必合并，L0 未合并 ⇒ L1 对应组也绝不合。L1 总体可多于 L0（无对应的
           L1-only 组保持独立槽位）。

        v9 投影让 LOD1 顶点组引用 LOD0 的槽位，运行时（MergedSkeleton.ini：
        每 component 每帧只导入一次骨骼，且仅允许更优 $lod_level 覆盖）同帧
        先 LOD0 后 LOD1 时，LOD1 网格读取的是 LOD0 已导入的矩阵（两侧矩阵
        数据不同）→ 模型爆炸。分段平移后每个 LOD 绘制入口挂自己的槽位段，
        运行时把**当前 LOD 自己的矩阵**写入自己的槽位，无跨 LOD 共享。

        参数与返回同 v9 约定：``collected_by_lod[lod][unique_str] =
        (skeleton_buffer, vg_count, weighted_counts, signatures)``；返回
        (maps_by_lod, offsets_by_lod, base_slot)。base_slot 为基准段大小
        （非基准 LOD 的起始基址），用于统计数据与后续 LOD2+ 的继续平移。
        ``correspondence`` 为 build_cross_lod_correspondence 的返回值，缺省
        None 时退化为纯 v10 行为（各自去重 + 平移，无镜像约束）。
        ``deduplicate`` 透传给 build_vg_maps：False 时不执行权重扩散去重
        （每根骨骼独占槽位），None 沿用全局 _DEDUP_ENABLED。
        """
        projection_skip_by_lod = projection_skip_by_lod or {}
        reference_lod = str(reference_lod or "").strip()
        if not reference_lod:
            reference_lod = "LOD0" if "LOD0" in collected_by_lod else (
                sorted(collected_by_lod.keys())[0] if collected_by_lod else ""
            )

        reference_retained = {
            unique_str: entry
            for unique_str, entry in (collected_by_lod.get(reference_lod, {}) or {}).items()
            if unique_str not in projection_skip_by_lod.get(reference_lod, ())
        }
        # I1 生命周期域：跨 LOD 同 IB（同一 draw）归组，跨组件去重只在域相同时允许。
        lifetime_domains_by_lod = EFMIBoneMapBuilder._component_lifetime_domains(
            collected_by_lod
        )
        if reference_retained:
            baseline_maps, baseline_offsets = EFMIBoneMapBuilder.build_vg_maps(
                reference_retained,
                lifetime_domains=lifetime_domains_by_lod.get(reference_lod),
                deduplicate=deduplicate,
            )
        else:
            baseline_maps, baseline_offsets = {}, {}

        maps_by_lod: dict[str, dict[str, dict]] = {
            reference_lod: baseline_maps,
        }
        offsets_by_lod: dict[str, dict[str, int]] = {
            reference_lod: baseline_offsets,
        }

        # 基准段大小：基准组全部候选槽位（offset 按 vg_count 连续累加，
        # max(offset+count) 即基准池总槽位，也是非基准 LOD 的平移基址）。
        baseline_size = 0
        for unique_str, ref_offset in baseline_offsets.items():
            entry = reference_retained.get(unique_str)
            if entry is None:
                continue
            vg_count = int(entry[1] or 0)
            baseline_size = max(baseline_size, ref_offset + vg_count)

        # 非基准 LOD 按名称排序逐个处理（LOD1、LOD2…），保证平移确定性；
        # 每个 LOD 独立去重后整体平移，并把自身段大小累加到下一个基址
        # （LOD2 起继续落在上一段之后；LOD1 只关心与基准不相交）。
        # v13：**两侧都不做值域压缩重排**——重排使
        # L0 值域压成 [0,k0) 后 L1 值域被迫从 k0 起，侵入 L0 的声明段区域
        # [0, baseline_size)，实测游戏内直接乱掉。恢复 v10 平移口径：
        # 值域 = 各 LOD 去重后的原生槽位（canonical 借位可能留下未引用空洞），
        # 非基准 LOD 整体平移到基准声明段之后（值域 ⊆ 自身声明段 ∪ 段内借位，
        # 即 ⊂ [baseline_size, 基准声明尾 + 本段声明尾)），跨 LOD 零共享。
        # 镜像约束（同 L0 合并组强制同槽、不同 L0 组断边）**保留**——
        # 它是"拿 L0 约束 L1 去重"，不改变编号语义。
        shift_base = baseline_size
        for lod_name in sorted(collected_by_lod.keys()):
            if lod_name == reference_lod:
                continue
            retained = {
                unique_str: entry
                for unique_str, entry in (collected_by_lod.get(lod_name, {}) or {}).items()
                if unique_str not in projection_skip_by_lod.get(lod_name, ())
            }
            if not retained:
                maps_by_lod[lod_name] = {}
                offsets_by_lod[lod_name] = {}
                continue
            # v11 镜像约束：从跨 LOD 对应生成本 LOD 候选的“所属 L0 合并组”标签。
            # 有对应时不同标签断边（不能跨 L0 组合并），无对应时退化为 v10 语义。
            constraint_labels = (
                EFMIBoneMapBuilder._build_cross_lod_constraint_labels(
                    correspondence, lod_name, baseline_maps
                )
                if correspondence
                else {}
            )
            lod_maps, lod_offsets = EFMIBoneMapBuilder.build_vg_maps(
                retained,
                constraint_labels=constraint_labels,
                lifetime_domains=lifetime_domains_by_lod.get(lod_name),
                deduplicate=deduplicate,
            )
            # v11 镜像强制：同一 L0 合并组的全部目标候选强制共用最小槽位
            # （build_vg_maps 的标签只断不同组，不并同组）。只重定目标侧
            # 槽位（取组内最小值），不做任何全局重排。
            EFMIBoneMapBuilder._enforce_same_reference_group_single_slot(
                lod_maps, constraint_labels
            )
            shifted_maps = {
                unique_str: {
                    int(local_id): int(global_id) + shift_base
                    for local_id, global_id in sorted(part_map.items())
                }
                for unique_str, part_map in lod_maps.items()
            }
            shifted_offsets = {
                unique_str: int(offset) + shift_base
                for unique_str, offset in lod_offsets.items()
            }
            maps_by_lod[lod_name] = shifted_maps
            offsets_by_lod[lod_name] = shifted_offsets
            # 本 LOD 段大小（供 LOD2+ 继续平移）。
            segment_end = shift_base
            for unique_str, shifted_offset in shifted_offsets.items():
                entry = retained.get(unique_str)
                if entry is None:
                    continue
                segment_end = max(segment_end, shifted_offset + int(entry[1] or 0))
            shift_base = segment_end

        return maps_by_lod, offsets_by_lod, baseline_size

    @staticmethod
    def _bone_matrix_rotation_diff(
        a, b, default: float = float("inf")
    ) -> float:
        """4x3 骨骼矩阵的旋转/尺度块最大差（去掉捕获帧世界平移列）。

        布局：每骨骼 12 floats = 3 行 × 4 列（行列式 4x3）；
        平移在每行末列（index 3/7/11），旋转在列 0..2（index 0:3/4:7/8:11）。

        显式论证（用户领域裁决，2026-09-01；t4 铁证，佩丽卡脸部 b9767716 same-IB
        家族）：
        - 跨环境捕获是**固有常态**（L0/L1 本就在不同环境/不同 FrameAnalysis 目录
          捕获；同 IB = 不管哪个环境都是这个 draw 在渲染）；
        - 骨骼矩阵里的世界平移 = 捕获帧角色世界位置（环境噪声），不携带骨骼身份：
          去除平移列后 53 根旋转差全部 <2.0（5 根 raw 大差行 26/32/34/35/37 →
          1.082/0.902/1.139/0.886/1.994），无一根骨骼旋转语义不同；同一平移常数
          -368..-408 跨组件一字不差复用；
        - 因此 **rotation-only 判据 = 语义修正、不是放松闸门**：阈值 16.0/1.0 未动；
          旋转/缩放真实不兼容时旋转差仍 >16，fail-closed 拒绝不变；单调性：旋转差
          ⊆ raw 差元素（12 中取 9 的 max），此前 raw ≤16 合法放行的家族旋转必 ≤16
          ——不产生任何新拒绝。用户已复核拍板**接受此判据现状**（不再补 n<3 守卫 /
          中位数残差 / 审计日志）。

        ``default``：骨骼形状无法解析时返回的保守值（默认 inf = 当作不兼容，
        闸门按 fail-closed 拒绝；调用方不应传有限值放宽）。
        """
        try:
            ra = numpy.asarray(a, dtype=numpy.float64)
            rb = numpy.asarray(b, dtype=numpy.float64)
            if ra.size < 12 or rb.size < 12:
                return default
            rot_a = numpy.concatenate((ra[0:3], ra[4:7], ra[8:11]))
            rot_b = numpy.concatenate((rb[0:3], rb[4:7], rb[8:11]))
            diff = float(numpy.max(numpy.abs(rot_a - rot_b)))
            if not numpy.isfinite(diff):
                return default
            return diff
        except Exception:
            return default

    @staticmethod
    def build_cross_lod_correspondence(
        lod_submesh_skeletons: dict[str, dict[str, tuple]],
        reference_lod: str = "LOD0",
        match_tolerance: float = _CROSS_LOD_MATRIX_SCORE_SCALE,
        centroid_tolerance: float = 0.10,
    ) -> dict:
        """用原始顶点组的矩阵 + 加权中心建立跨 LOD 对应，并生成去重保护边。

        这里故意接收 *未去重* 的 ``submesh_skeletons``。如果先在每个 LOD
        内部去重，某一侧已经吞掉的候选就无法再被另一侧矩阵区分出来。返回值中
        ``protected_pairs`` 保留为诊断/兼容字段；联合导入路径不再把它交给两侧
        独立去重，而是用 ``matches`` 将 LOD0 的分区同步到目标 LOD。

        对应不是按文件夹名配对：LOD 提取时组件 hash、局部索引和顶点数量都可能
        轻微变化。先为每个部件聚合其权重扩散点云，使用对称最近邻几何距离、包围盒
        和整体中心做一对一部件匹配；再**只在匹配的部件对内部**按局部加权中心配对
        原始顶点组，权重中心是主排序证据，矩阵只作为有限的第二排序项；有有效
        权重中心时，矩阵差异不会直接拒绝对应。只有两侧都缺少几何中心时，才保守
        要求矩阵近似一致，避免在完全没有几何证据时凭局部编号乱配。LOD 之间的
        矩阵本身可能因为简化网格/捕获帧不同而有明显差异，因此这里的 1.5 只是
        矩阵次级评分的尺度，不改变各 LOD 内部 build_vg_maps 的 1e-3 硬门控。
        不要求两个 LOD 的整片驱动区域完全重合（例如平面和贴合物体）。
        同一参考候选在目标 LOD 可以有多个命中，表示目标 LOD 的额外细分；反向
        保护只在两个候选分别对应不同参考候选时触发。

        返回字段是纯 Python/JSON 友好的结构：
        ``reference_lod``、``matches``、``protected_pairs``、``counts``、
        ``unmatched_reference``、``unmatched_by_lod``。
        """
        if not lod_submesh_skeletons:
            return {
                "reference_lod": "",
                "part_matches": [],
                "unmatched_reference_parts": [],
                "unmatched_target_parts": {},
                "matches": [],
                "protected_pairs": {},
                "counts": {},
                "unmatched_reference": [],
                "unmatched_by_lod": {},
            }

        lod_names = sorted(str(name) for name in lod_submesh_skeletons.keys())
        reference_lod = str(reference_lod or "").strip()
        if reference_lod not in lod_names:
            reference_lod = "LOD0" if "LOD0" in lod_names else lod_names[0]

        def _flatten(lod_name: str) -> list[dict]:
            result = []
            parts = lod_submesh_skeletons.get(lod_name, {}) or {}
            for unique_str in sorted(parts.keys()):
                entry = parts[unique_str]
                if not entry or len(entry) < 2:
                    continue
                skeleton = entry[0]
                vg_count = int(entry[1] or 0)
                weighted = entry[2] if len(entry) > 2 else None
                signatures = entry[3] if len(entry) > 3 else {}
                if skeleton is None or vg_count <= 0 or len(skeleton) < vg_count:
                    continue
                for local in range(vg_count):
                    bone = numpy.asarray(skeleton[local], dtype=numpy.float64)
                    if bone.size == 0 or numpy.all(bone == 0):
                        continue
                    count = 0
                    if weighted is not None and local < len(weighted):
                        try:
                            count = int(weighted[local])
                        except (TypeError, ValueError):
                            count = 0
                    signature = signatures.get(local) if isinstance(signatures, dict) else None
                    centroid = None
                    spread = 0.0
                    diffusion_points = numpy.empty((0, 3), dtype=numpy.float64)
                    diffusion_weights = numpy.empty((0,), dtype=numpy.float64)
                    if isinstance(signature, dict):
                        raw_centroid = signature.get("centroid")
                        if raw_centroid is not None:
                            try:
                                value = numpy.asarray(raw_centroid, dtype=numpy.float64)
                                if value.shape == (3,) and numpy.isfinite(value).all():
                                    centroid = value
                            except (TypeError, ValueError):
                                centroid = None
                        try:
                            spread = max(float(signature.get("spread", 0.0) or 0.0), 0.0)
                        except (TypeError, ValueError):
                            spread = 0.0
                        try:
                            raw_points = numpy.asarray(
                                signature.get("diffusion_points", []),
                                dtype=numpy.float64,
                            )
                            raw_weights = numpy.asarray(
                                signature.get("diffusion_weights", []),
                                dtype=numpy.float64,
                            )
                            if (
                                raw_points.ndim == 2
                                and raw_points.shape[1] == 3
                                and raw_weights.ndim == 1
                                and len(raw_points) == len(raw_weights)
                            ):
                                valid = numpy.isfinite(raw_points).all(axis=1) & numpy.isfinite(raw_weights)
                                valid &= raw_weights > 0
                                diffusion_points = raw_points[valid]
                                diffusion_weights = raw_weights[valid]
                        except (TypeError, ValueError):
                            diffusion_points = numpy.empty((0, 3), dtype=numpy.float64)
                            diffusion_weights = numpy.empty((0,), dtype=numpy.float64)
                    result.append({
                        "lod": lod_name,
                        "unique_str": str(unique_str),
                        "local_vg_id": int(local),
                        "bone": bone,
                        "weighted_vertex_count": count,
                        "centroid": centroid,
                        "spread": spread,
                        "diffusion_points": diffusion_points,
                        "diffusion_weights": diffusion_weights,
                    })
            return result

        flattened = {lod: _flatten(lod) for lod in lod_names}
        counts = {lod: len(items) for lod, items in flattened.items()}
        reference_candidates = flattened.get(reference_lod, [])

        # 先建立子网格/节点级对应。跨 LOD 文件夹名是不同 hash，不能直接拼接；
        # 部件整体中心通常比单个骨骼中心稳定，也能避免两个局部骨骼恰好共心时
        # 被分到不同部件。
        part_candidates = {}
        for lod_name, candidates_for_lod in flattened.items():
            by_part = {}
            for candidate in candidates_for_lod:
                by_part.setdefault(candidate["unique_str"], []).append(candidate)
            part_candidates[lod_name] = by_part

        def _part_descriptor(items):
            """建立部件级点云描述，避免把不同部件的局部组放到同一候选池。"""
            center_items = [item for item in items if item["centroid"] is not None]
            center = None
            total_weight = 0.0
            if center_items:
                center_weights = numpy.asarray([
                    max(int(item["weighted_vertex_count"]), 1)
                    for item in center_items
                ], dtype=numpy.float64)
                center_points = numpy.stack([
                    item["centroid"] for item in center_items
                ]).astype(numpy.float64)
                center = numpy.average(center_points, axis=0, weights=center_weights)
                total_weight = float(center_weights.sum())

            point_chunks = []
            weight_chunks = []
            for item in items:
                points = numpy.asarray(item.get("diffusion_points", []), dtype=numpy.float64)
                weights = numpy.asarray(item.get("diffusion_weights", []), dtype=numpy.float64)
                if (
                    points.ndim == 2 and points.shape[1] == 3
                    and weights.ndim == 1 and len(points) == len(weights)
                    and len(points) > 0
                ):
                    # 每个局部组最多贡献 64 个点，部件级匹配只需要形状指纹，
                    # 不把全部平面点云复制进 11×11 的比较矩阵。
                    stride = max(int(numpy.ceil(len(points) / 64.0)), 1)
                    points = points[::stride][:64]
                    weights = weights[::stride][:64]
                    valid = numpy.isfinite(points).all(axis=1) & numpy.isfinite(weights)
                    valid &= weights > 0
                    if numpy.any(valid):
                        point_chunks.append(points[valid])
                        weight_chunks.append(weights[valid])
                elif item["centroid"] is not None:
                    point_chunks.append(numpy.asarray(item["centroid"], dtype=numpy.float64)[None, :])
                    weight_chunks.append(numpy.asarray([
                        max(int(item["weighted_vertex_count"]), 1)
                    ], dtype=numpy.float64))

            if point_chunks:
                points = numpy.concatenate(point_chunks, axis=0)
                weights = numpy.concatenate(weight_chunks, axis=0)
                if len(points) > 512:
                    sample_idx = numpy.linspace(0, len(points) - 1, 512, dtype=numpy.int64)
                    points = points[sample_idx]
                    weights = weights[sample_idx]
                if center is None:
                    center = numpy.average(points, axis=0, weights=weights)
                total_weight = max(total_weight, float(weights.sum()))
                bbox_min = numpy.min(points, axis=0)
                bbox_max = numpy.max(points, axis=0)
            elif center is not None:
                points = center[None, :].astype(numpy.float64)
                weights = numpy.asarray([max(total_weight, 1.0)], dtype=numpy.float64)
                bbox_min = center.copy()
                bbox_max = center.copy()
            else:
                points = numpy.empty((0, 3), dtype=numpy.float64)
                weights = numpy.empty((0,), dtype=numpy.float64)
                bbox_min = None
                bbox_max = None

            return {
                "center": center,
                "count": len(items),
                "weight": total_weight,
                "points": points,
                "weights": weights,
                "bbox_min": bbox_min,
                "bbox_max": bbox_max,
            }

        part_descriptors = {
            lod_name: {
                unique_str: _part_descriptor(items)
                for unique_str, items in by_part.items()
            }
            for lod_name, by_part in part_candidates.items()
        }

        def _part_cloud_distance(source, target):
            source_points = source["points"]
            target_points = target["points"]
            if len(source_points) == 0 or len(target_points) == 0:
                return None

            def nearest_median(points_a, points_b):
                distances = []
                for start in range(0, len(points_a), 64):
                    chunk = points_a[start:start + 64]
                    delta = chunk[:, None, :] - points_b[None, :, :]
                    squared = numpy.sum(delta * delta, axis=2)
                    distances.extend(numpy.sqrt(numpy.min(squared, axis=1)).tolist())
                return float(numpy.median(numpy.asarray(distances, dtype=numpy.float64)))

            return 0.5 * (
                nearest_median(source_points, target_points)
                + nearest_median(target_points, source_points)
            )

        def _part_pair_score(source_lod, source, target_lod, target):
            source_desc = part_descriptors[source_lod][source]
            target_desc = part_descriptors[target_lod][target]
            center_distance = None
            if source_desc["center"] is not None and target_desc["center"] is not None:
                center_distance = float(numpy.linalg.norm(
                    source_desc["center"] - target_desc["center"]
                ))
                if center_distance > max(float(centroid_tolerance) * 8.0, 0.75):
                    return None

            cloud_distance = _part_cloud_distance(source_desc, target_desc)
            bbox_gap = 0.0
            if (
                source_desc["bbox_min"] is not None
                and source_desc["bbox_max"] is not None
                and target_desc["bbox_min"] is not None
                and target_desc["bbox_max"] is not None
            ):
                gap_vec = numpy.maximum(
                    numpy.maximum(source_desc["bbox_min"] - target_desc["bbox_max"],
                                  target_desc["bbox_min"] - source_desc["bbox_max"]),
                    0.0,
                )
                bbox_gap = float(numpy.linalg.norm(gap_vec))

            source_extent = (
                numpy.zeros(3, dtype=numpy.float64)
                if source_desc["bbox_min"] is None
                else source_desc["bbox_max"] - source_desc["bbox_min"]
            )
            target_extent = (
                numpy.zeros(3, dtype=numpy.float64)
                if target_desc["bbox_min"] is None
                else target_desc["bbox_max"] - target_desc["bbox_min"]
            )
            extent_scale = max(float(numpy.linalg.norm(source_extent)),
                               float(numpy.linalg.norm(target_extent)), 0.05)
            extent_error = float(numpy.linalg.norm(source_extent - target_extent)) / extent_scale
            count_ratio = max(
                source_desc["count"] / max(target_desc["count"], 1),
                target_desc["count"] / max(source_desc["count"], 1),
            )
            count_term = min(float(numpy.log(max(count_ratio, 1.0))), 4.0) * 0.05
            # 点云形状是主判据；中心/bbox/部件数量只做稳定器，防止两个部件
            # 恰好整体中心接近时被错误交换。
            score = 0.0 if cloud_distance is None else cloud_distance
            score += bbox_gap * 0.25
            score += (0.0 if center_distance is None else center_distance * 0.10)
            score += extent_error * 0.05 + count_term
            return score

        def _match_parts(source_lod, target_lod):
            source_parts = sorted(part_descriptors.get(source_lod, {}).keys())
            target_parts = sorted(part_descriptors.get(target_lod, {}).keys())
            rows = []
            for source in source_parts:
                for target in target_parts:
                    score = _part_pair_score(source_lod, source, target_lod, target)
                    if score is not None:
                        rows.append((score, source, target))
            rows.sort(key=lambda row: (row[0], row[1], row[2]))

            # 11×11 规模使用精确的一对一最小代价分配，避免贪心先占用
            # 一个近似部件后把后续部件错配；部件数量不等时退化为贪心。
            score_by_pair = {(source, target): score for score, source, target in rows}
            if len(source_parts) == len(target_parts) and len(source_parts) <= 12:
                states = {0: (0.0, [])}
                for source_index, source in enumerate(source_parts):
                    next_states = {}
                    for mask, (total, selected) in states.items():
                        for target_index, target in enumerate(target_parts):
                            if mask & (1 << target_index):
                                continue
                            score = score_by_pair.get((source, target))
                            if score is None:
                                continue
                            new_mask = mask | (1 << target_index)
                            candidate = (total + score, selected + [(source, target, score)])
                            previous = next_states.get(new_mask)
                            if previous is None or candidate[0] < previous[0]:
                                next_states[new_mask] = candidate
                    states = next_states
                full_mask = (1 << len(target_parts)) - 1
                if full_mask in states:
                    selected = states[full_mask][1]
                    return (
                        {source: target for source, target, _score in selected},
                        {(source, target): float(score) for source, target, score in selected},
                    )

            used_source, used_target = set(), set()
            mapping = {}
            selected_scores = {}
            for _score, source, target in rows:
                if source in used_source or target in used_target:
                    continue
                used_source.add(source)
                used_target.add(target)
                mapping[source] = target
                selected_scores[(source, target)] = float(_score)
            return mapping, selected_scores

        def _pair_score(left: dict, right: dict):
            matrix_diff = float(numpy.max(numpy.abs(left["bone"] - right["bone"])))
            # 用户领域裁决（2026-09-01）：rotation-only 判据为主修复——跨捕获帧的
            # 世界平移（角色世界位置）是环境噪声，不携带骨骼身份；比较前去掉平移
            # 列只比旋转/尺度块（_bone_matrix_rotation_diff）。raw matrix_diff 保留
            # 供内部评分/无几何硬门控/诊断口径使用。
            matrix_diff_rotation = EFMIBoneMapBuilder._bone_matrix_rotation_diff(
                left["bone"], right["bone"]
            )
            if not numpy.isfinite(matrix_diff):
                return None
            # 跨 LOD 对应由几何证据建立。没有任何权重中心时不能凭宽松的矩阵
            # 差异硬配；这类旧缓存/退化数据只有近似矩阵才足够安全。
            if left["centroid"] is None or right["centroid"] is None:
                if matrix_diff >= 1e-3:
                    return None
            centroid_distance = None
            if left["centroid"] is not None and right["centroid"] is not None:
                centroid_distance = float(numpy.linalg.norm(
                    left["centroid"] - right["centroid"]
                ))
            # 同一根骨骼在两个 LOD 的大平面/附件顶点数可能相差很大；权重数量
            # 只用于同分候选的轻微排序，不作为硬门槛。
            weight_ratio = 0.0
            if left["weighted_vertex_count"] > 0 and right["weighted_vertex_count"] > 0:
                ratio = max(
                    left["weighted_vertex_count"] / right["weighted_vertex_count"],
                    right["weighted_vertex_count"] / left["weighted_vertex_count"],
                )
                weight_ratio = min(float(numpy.log(max(ratio, 1.0))), 4.0)
            center_term = 0.0
            if centroid_distance is not None:
                center_term = min(
                    centroid_distance / max(float(centroid_tolerance), 1e-6),
                    8.0,
                )
                # 这里已经限定在同一个“部件节点”内；部件可能是大平面，
                # 同一根骨骼的散落附件中心相距很远，因此不能再用绝对中心距
                # 做硬拒绝，只把它作为候选排序项。
            # 跨 LOD 以加权中心为第一排序键，矩阵只做次级稳定器；不能把
            # LOD0/LOD1 的捕获姿态差异误当成不同骨骼的硬拒绝。
            matrix_scale = max(float(match_tolerance), 1e-8)
            matrix_term = min(matrix_diff / matrix_scale, 8.0) * 0.05
            score = center_term + matrix_term
            score += weight_ratio * 0.03
            return score, matrix_diff, matrix_diff_rotation, centroid_distance

        protected_by_lod: dict[str, set[tuple[tuple[str, int], tuple[str, int]]]] = {
            lod: set() for lod in lod_names
        }
        all_matches = []
        part_matches = []

        for target_lod in lod_names:
            if target_lod == reference_lod:
                continue
            target_candidates = flattened.get(target_lod, [])
            reference_part_to_target, part_pair_scores = _match_parts(
                reference_lod, target_lod
            )
            for (reference_part, target_part), score in sorted(part_pair_scores.items()):
                part_matches.append({
                    "reference_lod": reference_lod,
                    "target_lod": target_lod,
                    "reference_unique_str": reference_part,
                    "target_unique_str": target_part,
                    "score": float(score),
                    "reference_group_count": len(part_candidates.get(reference_lod, {}).get(reference_part, [])),
                    "target_group_count": len(part_candidates.get(target_lod, {}).get(target_part, [])),
                })
            pair_rows = []
            for ref in reference_candidates:
                for target in target_candidates:
                    if reference_part_to_target.get(ref["unique_str"]) != target["unique_str"]:
                        continue
                    scored = _pair_score(ref, target)
                    if scored is None:
                        continue
                    score, matrix_diff, matrix_diff_rotation, centroid_distance = scored
                    pair_rows.append((
                        score,
                        ref,
                        target,
                        matrix_diff,
                        centroid_distance,
                        matrix_diff_rotation,
                    ))

            # 先做稳定的一对一主匹配：一个目标候选不能被两个参考候选抢走。
            # 额外目标候选随后仍可通过各自最佳参考建立“LOD1 多于 LOD0”的关系，
            # 但不会因此制造错误的保护边。
            pair_rows.sort(key=lambda row: (
                row[0],
                -row[1]["weighted_vertex_count"],
                -row[2]["weighted_vertex_count"],
                row[1]["unique_str"],
                row[1]["local_vg_id"],
                row[2]["unique_str"],
                row[2]["local_vg_id"],
            ))
            used_ref = set()
            used_target = set()
            primary = []
            for row in pair_rows:
                ref, target = row[1], row[2]
                ref_key = (ref["unique_str"], ref["local_vg_id"])
                target_key = (target["unique_str"], target["local_vg_id"])
                if ref_key in used_ref or target_key in used_target:
                    continue
                used_ref.add(ref_key)
                used_target.add(target_key)
                primary.append(row)

            # 每个目标候选都保留一个最佳参考（包括额外候选）。
            best_ref_for_target = {}
            for row in pair_rows:
                target = row[2]
                target_key = (target["unique_str"], target["local_vg_id"])
                if target_key not in best_ref_for_target:
                    best_ref_for_target[target_key] = row

            target_rows_by_ref: dict[tuple[str, int], list[dict]] = {}
            for row in pair_rows:
                ref, target = row[1], row[2]
                ref_key = (ref["unique_str"], ref["local_vg_id"])
                target_rows_by_ref.setdefault(ref_key, []).append(target)
            for ref_key in target_rows_by_ref:
                target_rows_by_ref[ref_key].sort(
                    key=lambda item: (item["unique_str"], item["local_vg_id"])
                )

            best_target_for_ref = {}
            for row in pair_rows:
                ref = row[1]
                ref_key = (ref["unique_str"], ref["local_vg_id"])
                if ref_key not in best_target_for_ref:
                    best_target_for_ref[ref_key] = row

            primary_target_for_ref = {
                (row[1]["unique_str"], row[1]["local_vg_id"]): row[2]
                for row in primary
            }
            primary_ref_for_target = {
                (row[2]["unique_str"], row[2]["local_vg_id"]): row[1]
                for row in primary
            }

            def _cross_side_is_distinct(left: dict, right: dict, threshold: float) -> bool:
                """判断另一侧是否提供了足够强的“拆分证据”。

                中心相距很远本身不能拆分：同一根骨骼可以同时驱动大平面和
                散落附件。只有另一侧的矩阵也出现明显分离时才回传保护边；
                这样跨 LOD 姿态的小幅矩阵变化不会把正常的重复骨骼全部拆散。
                """
                return float(numpy.max(numpy.abs(left["bone"] - right["bone"]))) >= float(threshold)

            def _current_side_can_merge(left: dict, right: dict) -> bool:
                # 保护边只约束本侧原本有机会通过矩阵硬门的候选；不同矩阵
                # 已经由 build_vg_maps 拒绝，不应让跨 LOD 层扩大拆分范围。
                return float(numpy.max(numpy.abs(left["bone"] - right["bone"]))) < 1e-3

            # 参考侧有两个候选，而目标侧分别存在两个可区分候选时，禁止参考
            # 侧把它们并成一组；反过来也一样。这正是“用另一侧矩阵补齐”
            # 的约束来源。
            for ref_a_index in range(len(reference_candidates)):
                ref_a = reference_candidates[ref_a_index]
                ref_a_key = (ref_a["unique_str"], ref_a["local_vg_id"])
                target_a = primary_target_for_ref.get(ref_a_key)
                if target_a is None:
                    continue
                target_a_key = (target_a["unique_str"], target_a["local_vg_id"])
                for ref_b in reference_candidates[ref_a_index + 1:]:
                    ref_b_key = (ref_b["unique_str"], ref_b["local_vg_id"])
                    target_b = primary_target_for_ref.get(ref_b_key)
                    if target_b is None:
                        continue
                    target_b_key = (target_b["unique_str"], target_b["local_vg_id"])
                    if (
                        target_a_key != target_b_key
                        and _current_side_can_merge(ref_a, ref_b)
                        # LOD0 是基准，只有 LOD1 矩阵出现明显分叉才拆它，
                        # 避免跨帧姿态差异把基准侧拆得过碎。
                        and _cross_side_is_distinct(target_a, target_b, 0.75)
                    ):
                        protected_by_lod[reference_lod].add(
                            tuple(sorted((ref_a_key, ref_b_key)))
                        )

            target_to_refs: dict[tuple[str, int], list[dict]] = {}
            for row in pair_rows:
                target = row[2]
                target_key = (target["unique_str"], target["local_vg_id"])
                ref = row[1]
                target_to_refs.setdefault(target_key, []).append(ref)
            for target_key, refs in target_to_refs.items():
                refs = sorted(refs, key=lambda item: (item["unique_str"], item["local_vg_id"]))
                for ref_a_index in range(len(refs)):
                    ref_a = refs[ref_a_index]
                    ref_a_key = (ref_a["unique_str"], ref_a["local_vg_id"])
                    for ref_b in refs[ref_a_index + 1:]:
                        ref_b_key = (ref_b["unique_str"], ref_b["local_vg_id"])
                        # 一个目标候选对应多个参考候选，不能在目标侧无中生有
                        # 地拆分；只有目标侧本身存在不同候选时才添加保护边，
                        # 该情况由下面的反向扫描覆盖。
                        if ref_a_key == ref_b_key:
                            continue

            for target_a_index in range(len(target_candidates)):
                target_a = target_candidates[target_a_index]
                target_a_key = (target_a["unique_str"], target_a["local_vg_id"])
                ref_a = primary_ref_for_target.get(target_a_key)
                if ref_a is None:
                    continue
                ref_a_key = (ref_a["unique_str"], ref_a["local_vg_id"])
                for target_b in target_candidates[target_a_index + 1:]:
                    target_b_key = (target_b["unique_str"], target_b["local_vg_id"])
                    ref_b = primary_ref_for_target.get(target_b_key)
                    if ref_b is None:
                        continue
                    ref_b_key = (ref_b["unique_str"], ref_b["local_vg_id"])
                    if (
                        ref_a_key != ref_b_key
                        and _current_side_can_merge(target_a, target_b)
                        # LOD1 允许比 LOD0 更细；当参考侧能区分时优先保留
                        # 目标侧的细分，确保目标组数不会因独立去重而变少。
                        and _cross_side_is_distinct(ref_a, ref_b, 0.24)
                    ):
                        protected_by_lod[target_lod].add(
                            tuple(sorted((target_a_key, target_b_key)))
                        )

            for row in primary:
                ref, target = row[1], row[2]
                all_matches.append({
                    "reference_lod": reference_lod,
                    "target_lod": target_lod,
                    "reference_unique_str": ref["unique_str"],
                    "reference_local_vg_id": int(ref["local_vg_id"]),
                    "target_unique_str": target["unique_str"],
                    "target_local_vg_id": int(target["local_vg_id"]),
                    "reference_component": ref["unique_str"],
                    "target_component": target["unique_str"],
                    "component_score": float(part_pair_scores.get((
                        ref["unique_str"], target["unique_str"]
                    ), 0.0)),
                    "score": float(row[0]),
                    "matrix_diff": float(row[3]),
                    "centroid_distance": (
                        None if row[4] is None else float(row[4])
                    ),
                    "matrix_diff_rotation": float(row[5]),
                })

        unmatched_reference = []
        matched_ref_keys = {
            (item["reference_unique_str"], item["reference_local_vg_id"])
            for item in all_matches
        }
        for candidate in reference_candidates:
            key = (candidate["unique_str"], candidate["local_vg_id"])
            if key not in matched_ref_keys:
                unmatched_reference.append({
                    "unique_str": candidate["unique_str"],
                    "local_vg_id": int(candidate["local_vg_id"]),
                })

        unmatched_by_lod = {}
        for lod_name in lod_names:
            if lod_name == reference_lod:
                continue
            matched_targets = {
                (item["target_unique_str"], item["target_local_vg_id"])
                for item in all_matches if item["target_lod"] == lod_name
            }
            unmatched_by_lod[lod_name] = [
                {
                    "unique_str": candidate["unique_str"],
                    "local_vg_id": int(candidate["local_vg_id"]),
                }
                for candidate in flattened.get(lod_name, [])
                if (candidate["unique_str"], candidate["local_vg_id"]) not in matched_targets
            ]

        serialized_protected = {}
        for lod_name, pairs in protected_by_lod.items():
            serialized_protected[lod_name] = [
                [list(left), list(right)]
                for left, right in sorted(pairs)
            ]
        matched_part_pairs = {
            (row["reference_unique_str"], row["target_unique_str"])
            for row in part_matches
        }
        matched_reference_parts = {row[0] for row in matched_part_pairs}
        matched_target_parts = {row[1] for row in matched_part_pairs}
        return {
            "reference_lod": reference_lod,
            "part_matches": part_matches,
            "unmatched_reference_parts": [
                part for part in sorted(part_candidates.get(reference_lod, {}))
                if part not in matched_reference_parts
            ],
            "unmatched_target_parts": {
                lod_name: [
                    part for part in sorted(part_candidates.get(lod_name, {}))
                    if part not in matched_target_parts
                ]
                for lod_name in lod_names if lod_name != reference_lod
            },
            "matches": all_matches,
            "protected_pairs": protected_by_lod,
            "protected_pairs_json": serialized_protected,
            "counts": counts,
            "unmatched_reference": unmatched_reference,
            "unmatched_by_lod": unmatched_by_lod,
        }

    @staticmethod
    def _build_cross_lod_constraint_labels(
        correspondence: dict,
        lod_name: str,
        vg_maps: dict[str, dict],
    ) -> dict[tuple[str, int], tuple]:
        """从另一侧当前 global group 生成本侧下一轮的单调约束标签。"""
        labels = {}
        reference_lod = correspondence.get("reference_lod", "LOD0")
        for row in correspondence.get("matches", []) or []:
            if lod_name == reference_lod:
                unique_str = row.get("reference_unique_str", "")
                local_id = int(row.get("reference_local_vg_id", 0) or 0)
                other_unique = row.get("target_unique_str", "")
                other_local = int(row.get("target_local_vg_id", 0) or 0)
                other_lod = row.get("target_lod", "")
            else:
                if row.get("target_lod") != lod_name:
                    continue
                unique_str = row.get("target_unique_str", "")
                local_id = int(row.get("target_local_vg_id", 0) or 0)
                other_unique = row.get("reference_unique_str", "")
                other_local = int(row.get("reference_local_vg_id", 0) or 0)
                other_lod = row.get("reference_lod", reference_lod)
            other_map = vg_maps.get(other_unique, {})
            if other_local not in other_map and str(other_local) not in other_map:
                continue
            other_group = other_map.get(other_local, other_map.get(str(other_local)))
            labels[(str(unique_str), local_id)] = (str(other_lod), int(other_group))
        return labels

    @staticmethod
    def _enforce_same_reference_group_single_slot(
        vg_maps: dict[str, dict],
        constraint_labels: dict[tuple[str, int], object],
    ) -> dict[str, dict]:
        """强制：同一参考合并组（同 label）的所有目标候选落到同一个槽位（v11）。

        build_vg_maps 的 constraint_labels 只保证“不同参考组不合并”，不保证
        “同一参考组必须合并”——目标 LOD 自己的矩阵硬门控/权重扩散可能把同一
        参考组的多个候选拆到多个槽位（L1 细分：L0 一个合并组对应 L1 多个组，
        用户用例：L0 的 5/6 合并后，L1 对应的 11..17 也必须合并）。本步是跨 LOD
        镜像的**强制**语义：把同 label 的全部候选统一到该组目标侧最小槽位
        （确定性，不依赖遍历先后）；未带 label 的候选（L0 无对应）保持自身
        槽位不动。不同 label 的组在 build_vg_maps 阶段已被断边，槽位集天然
        不相交，因此本步不会产生跨参考组串扰。
        """
        if not constraint_labels:
            return vg_maps
        label_candidates: dict[object, list[tuple[str, int]]] = {}
        for key, label in constraint_labels.items():
            unique_str, local_id = key
            if vg_maps.get(unique_str) is None or local_id not in vg_maps[unique_str]:
                continue
            label_candidates.setdefault(label, []).append((unique_str, local_id))
        for members in label_candidates.values():
            if len(members) <= 1:
                continue
            rep_slot = min(int(vg_maps[unique][local_id]) for unique, local_id in members)
            for unique, local_id in members:
                vg_maps[unique][local_id] = rep_slot
        return vg_maps

    @staticmethod
    def _renumber_lod_maps_for_segment(
        vg_maps: dict[str, dict],
        base: int,
    ) -> tuple[dict[str, dict], int]:
        """【已废弃（F3，t1 §6）】把本 LOD 的槽位**值域**压到 [base, base+k)。

        本函数在 v13 撤销「值域压缩」（efmi_skeleton.py:104-107、build_-
        independent_lod_maps docstring）后已无任何调用者（全仓仅定义处），
        仅保留作历史参考：v12 曾把两侧值域压缩重排，实测游戏内直接乱掉
        （L1 值域侵入 L0 声明段区域），v13 已恢复 v10 平移口径（值域 = 去重
        原生槽位 + 整段平移，跨 LOD 零共享）。新代码一律使用
        ``build_independent_lod_maps``；删除或恢复使用前请先确认平移语义。

        历史语义（不再生效）：按（子网格, 局部）首次出现顺序重编号，消除
        值域空洞；声明段（VGOffset 按 vg_count 连续累加）保持不变。
        """
        order: dict[int, int] = {}
        next_slot = int(base)
        for unique_str in sorted(vg_maps.keys()):
            for local_id in sorted(vg_maps[unique_str], key=lambda k: int(k)):
                old_slot = int(vg_maps[unique_str][local_id])
                if old_slot not in order:
                    order[old_slot] = next_slot
                    next_slot += 1
        for unique_str, local_map in vg_maps.items():
            for local_id in list(local_map.keys()):
                local_map[local_id] = order[int(local_map[local_id])]
        return vg_maps, next_slot


class EFMISkeletonMergeHelper:
    """EFMI 骨骼合并总流程：定位 FrameAnalysis -> 解析 log -> 构建映射 -> 写回工作空间。"""

    @staticmethod
    def _atomic_publish_cache_bundle(entries: list[dict]) -> None:
        """先完整暂存、再成组提交文件；失败时恢复所有旧目标。

        单文件 ``os.replace`` 只能保证一个目标原子更新。EFMI 需要同时发布
        BoneMatrix + InstanceConfig，ZZMI 需要 BoneMatrix + 可选 CB1；若第二份
        复制失败而第一份已经覆盖，就会留下旧 JSON 指向新原始文件的混合状态。
        本方法先复制并校验全部来源，之后才替换目标；提交阶段异常则从同目录备份
        逆序回滚。源就是目标（cache-only 重建）时只校验，不自拷贝。

        ``entries`` 每项字段：source_path、dest_path、vg_count、min_size，及可选
        ``float_aligned``（缓存 buffer 默认为 True；JSON 事务项为 False）。
        """
        prepared: list[dict] = []
        committed: list[dict] = []
        commit_succeeded = False

        def _validate(path: str, entry: dict) -> None:
            size = os.path.getsize(path)
            min_size = int(entry.get("min_size", 4) or 0)
            if size < min_size:
                raise OSError(f"缓存文件大小无效: {path} ({size} bytes)")
            if bool(entry.get("float_aligned", True)) and size % 4 != 0:
                raise OSError(f"缓存文件大小未按 float32 对齐: {path} ({size} bytes)")
            vg_count = int(entry.get("vg_count", 0) or 0)
            if vg_count > 0 and not EFMIBoneMapBuilder.cache_file_size_ok(path, vg_count):
                raise OSError(
                    f"缓存文件不足以容纳 {vg_count} 根骨骼: {path} ({size} bytes)"
                )

        try:
            # 准备阶段：任何来源复制/校验失败时，目标文件一个都不动。
            for raw_entry in entries:
                entry = dict(raw_entry)
                source_path = os.path.abspath(str(entry.get("source_path", "") or ""))
                dest_path = os.path.abspath(str(entry.get("dest_path", "") or ""))
                entry["source_path"] = source_path
                entry["dest_path"] = dest_path
                entry["temp_path"] = ""
                entry["backup_path"] = ""
                entry["dest_existed"] = os.path.isfile(dest_path)
                if not os.path.isfile(source_path):
                    raise FileNotFoundError(f"骨骼缓存源文件不存在: {source_path}")
                if source_path == dest_path:
                    _validate(source_path, entry)
                    prepared.append(entry)
                    continue

                os.makedirs(os.path.dirname(dest_path), exist_ok=True)
                fd, temp_path = tempfile.mkstemp(
                    prefix=f".{os.path.basename(dest_path)}.",
                    suffix=".tmp",
                    dir=os.path.dirname(dest_path),
                )
                os.close(fd)
                entry["temp_path"] = temp_path
                prepared.append(entry)
                shutil.copy2(source_path, temp_path)
                _validate(temp_path, entry)

            # 提交阶段：同目录 rename；每个旧目标先改名为唯一备份。
            for entry in prepared:
                temp_path = entry.get("temp_path", "")
                if not temp_path:
                    continue
                dest_path = entry["dest_path"]
                if entry["dest_existed"]:
                    fd, backup_path = tempfile.mkstemp(
                        prefix=f".{os.path.basename(dest_path)}.",
                        suffix=".bak",
                        dir=os.path.dirname(dest_path),
                    )
                    os.close(fd)
                    os.remove(backup_path)
                    os.replace(dest_path, backup_path)
                    entry["backup_path"] = backup_path
                committed.append(entry)
                os.replace(temp_path, dest_path)
                entry["temp_path"] = ""
            commit_succeeded = True
        except Exception as original_error:
            rollback_errors = []
            for entry in reversed(committed):
                dest_path = entry["dest_path"]
                backup_path = entry.get("backup_path", "")
                try:
                    if backup_path and os.path.exists(backup_path):
                        if os.path.exists(dest_path):
                            os.remove(dest_path)
                        os.replace(backup_path, dest_path)
                        entry["backup_path"] = ""
                    elif not entry.get("dest_existed") and os.path.exists(dest_path):
                        os.remove(dest_path)
                except Exception as rollback_error:
                    rollback_errors.append(str(rollback_error))
            if rollback_errors:
                raise OSError(
                    f"文件事务失败且回滚不完整: {original_error}; "
                    f"{'；'.join(rollback_errors)}"
                ) from original_error
            raise
        finally:
            for entry in prepared:
                temp_path = entry.get("temp_path", "")
                if temp_path and os.path.exists(temp_path):
                    try:
                        os.remove(temp_path)
                    except OSError:
                        pass
                backup_path = entry.get("backup_path", "")
                if commit_succeeded and backup_path and os.path.exists(backup_path):
                    try:
                        os.remove(backup_path)
                    except OSError:
                        pass

    @classmethod
    def _atomic_publish_cache(
        cls,
        source_path: str,
        dest_path: str,
        *,
        vg_count: int = 0,
        min_size: int = 4,
    ) -> None:
        """兼容单文件调用；实现委托给成组发布事务。"""
        cls._atomic_publish_cache_bundle([{
            "source_path": source_path,
            "dest_path": dest_path,
            "vg_count": vg_count,
            "min_size": min_size,
        }])

    @classmethod
    def _atomic_publish_skeleton_transaction(
        cls,
        cache_entries: list[dict],
        submesh_json: dict,
        json_path: str,
    ) -> None:
        """将全部来源缓存和 UTF-8 JSON 作为一个可回滚文件事务提交。"""
        json_dir = os.path.dirname(os.path.abspath(json_path))
        fd, staged_json_path = tempfile.mkstemp(
            prefix=f".{os.path.basename(json_path)}.",
            suffix=".tmp",
            dir=json_dir,
        )
        os.close(fd)
        try:
            JsonUtils.SaveToFile(
                filepath=staged_json_path,
                json_dict=submesh_json,
            )
            entries = list(cache_entries)
            entries.append({
                "source_path": staged_json_path,
                "dest_path": json_path,
                "vg_count": 0,
                "min_size": 2,
                "float_aligned": False,
            })
            cls._atomic_publish_cache_bundle(entries)
        finally:
            if os.path.exists(staged_json_path):
                try:
                    os.remove(staged_json_path)
                except OSError:
                    pass

    @staticmethod
    def resolve_frame_analysis_dir(workspace_root: str) -> str:
        """定位 FrameAnalysis 目录（多候选回退）。

        顺序：
        1. `Config/FrameAnalysisPath.json` 的 frameAnalysisFolderPath（isdir 校验）；
        2. `Config/Tabs/ws-tab-*.json` 的同名字段；
        3. 当前游戏 migoto 目录（GlobalConfig.current_game_migoto_folder）下
           mtime 最新的 `FrameAnalysis-*` 目录（工作空间记录的 dump 被删/挪动后的兜底）。
        """
        def _valid(path: str) -> str:
            path = str(path or "").strip()
            return path if path and os.path.isdir(path) else ""

        config_path = os.path.join(workspace_root, "Config", "FrameAnalysisPath.json")
        if os.path.isfile(config_path):
            try:
                payload = JsonUtils.LoadFromFile(config_path)
                found = _valid(payload.get("frameAnalysisFolderPath", ""))
                if found:
                    return found
            except Exception:
                pass

        tabs_dir = os.path.join(workspace_root, "Config", "Tabs")
        if os.path.isdir(tabs_dir):
            for tab_file in sorted(os.listdir(tabs_dir)):
                if not tab_file.startswith("ws-tab-") or not tab_file.endswith(".json"):
                    continue
                try:
                    payload = JsonUtils.LoadFromFile(os.path.join(tabs_dir, tab_file))
                    found = _valid(payload.get("frameAnalysisFolderPath", ""))
                    if found:
                        return found
                except Exception:
                    continue

        # 兜底：当前游戏 migoto 目录下最新的 FrameAnalysis-*
        try:
            from .global_config import GlobalConfig
            migoto_folder = str(getattr(GlobalConfig, "current_game_migoto_folder", "") or "").strip()
            if migoto_folder and os.path.isdir(migoto_folder):
                candidates = []
                for entry in os.scandir(migoto_folder):
                    if entry.is_dir() and entry.name.startswith("FrameAnalysis-"):
                        try:
                            candidates.append((entry.stat().st_mtime, entry.path))
                        except Exception:
                            continue
                if candidates:
                    candidates.sort(key=lambda item: item[0], reverse=True)
                    return candidates[0][1]
        except Exception:
            pass
        return ""

    @staticmethod
    def _parse_lod_name(unique_str: str) -> str:
        """解析 unique_str 的 LOD 前缀（'LOD0.xxx' -> 'LOD0'；无前缀 -> ''）。

        与 WorkSpaceHelper.parse_lod_unique_str 语义一致，但本模块需保持无 bpy
        依赖（单测以 stub 包加载），故本地实现。
        """
        normalized = str(unique_str or "").strip()
        if normalized.upper().startswith("LOD") and "." in normalized:
            dot_idx = normalized.index(".")
            potential = normalized[:dot_idx]
            if potential[3:].isdigit():
                return potential
        return ""

    @classmethod
    def resolve_frame_analysis_dirs_by_lod(
        cls, workspace_root: str
    ) -> tuple[dict[str, str], str]:
        """解析每 LOD 的 FrameAnalysis 目录映射 + 默认目录。

        多 LOD 语义（2026-08 实测定案）：不同 LOD 对应各自独立的 dump 提取目录。
        官方工具按工作页（tab）管理：Config/WorkPageTabs.json 的 tab.name 即 LOD 名
        （'LOD0'/'LOD1'），每个 tab 在 Config/Tabs/<tabid>.json 里记录自己的
        frameAnalysisFolderPath（该 LOD 的 dump 提取目录）；而工作空间级
        Config/FrameAnalysisPath.json 只记录"当前活动 tab"的路径——拿它喂所有 LOD
        会导致其它 LOD 全部查错目录（实测 LOD0 数据在 tab1 的 dump、LOD1 在 tab2 的
        dump，用任一单一路径都会漏掉另一侧）。

        返回 (lod_map, default_dir)：
        - lod_map: {tab/LOD 名 -> FrameAnalysis 目录}（仅收录目录有效的条目）；
        - default_dir: 现有 resolve_frame_analysis_dir 的解析结果
          （Config/FrameAnalysisPath.json -> Tabs 扫描 -> 最新 FrameAnalysis-* 兜底），
          供无 LOD 前缀的子网格与未在 tab 中登记的 LOD 使用。
        """
        lod_map: dict[str, str] = {}
        work_page_path = os.path.join(workspace_root, "Config", "WorkPageTabs.json")
        tabs_dir = os.path.join(workspace_root, "Config", "Tabs")
        if os.path.isfile(work_page_path) and os.path.isdir(tabs_dir):
            try:
                payload = JsonUtils.LoadFromFile(work_page_path)
                for tab in payload.get("tabs", []) or []:
                    tab_name = str(tab.get("name", "") or "").strip()
                    tab_id = str(tab.get("id", "") or "").strip()
                    if not tab_name or not tab_id:
                        continue
                    tab_file = os.path.join(tabs_dir, tab_id + ".json")
                    if not os.path.isfile(tab_file):
                        continue
                    try:
                        tab_payload = JsonUtils.LoadFromFile(tab_file)
                    except Exception:
                        continue
                    fa_path = str(
                        tab_payload.get("frameAnalysisFolderPath", "") or ""
                    ).strip()
                    if fa_path and os.path.isdir(fa_path):
                        lod_map[tab_name] = fa_path
            except Exception:
                pass
        default_dir = cls.resolve_frame_analysis_dir(workspace_root)
        return lod_map, default_dir

    @staticmethod
    def load_drawcall_index_list(lod_dir: str) -> dict[str, list[str]]:
        """读取 ComponentName_DrawCallIndexList.json（子网格名 -> drawcall 索引列表）。"""
        path = os.path.join(lod_dir, "ComponentName_DrawCallIndexList.json")
        if not os.path.isfile(path):
            return {}
        try:
            payload = JsonUtils.LoadFromFile(path)
            if isinstance(payload, dict):
                return {
                    str(k): [str(v) for v in (val if isinstance(val, list) else [])]
                    for k, val in payload.items()
                }
        except Exception:
            pass
        return {}

    @staticmethod
    def parse_blend_element_info(submesh_json_dict: dict) -> dict | None:
        """从子网格 json 的 CategoryBufferList 解析 Blend 类别 BLENDINDICES 元素信息。

        工作空间 json 的 CategoryBufferList 没有顶层 Category 字段，
        类别由每个元素的 Category 字段决定；Blend buffer 文件名形如 <name>-Blend.buf。
        """
        for category_buffer in submesh_json_dict.get("CategoryBufferList", []):
            elements = category_buffer.get("D3D11ElementList", [])
            if not elements:
                continue
            # 判定 Blend 类别：元素列表中任一元素 Category == "Blend"
            is_blend = any(
                str(element.get("Category", "") or "").strip().lower() == "blend"
                for element in elements
            )
            if not is_blend:
                continue

            stride = 0
            for element in elements:
                stride += int(element.get("ByteWidth", 0) or 0)
            if stride <= 0:
                return None

            byte_offset = 0
            for element in elements:
                element_width = int(element.get("ByteWidth", 0) or 0)
                semantic_name = str(element.get("SemanticName", "") or "").upper()
                if semantic_name == "BLENDINDICES":
                    fmt = str(element.get("Format", "") or "").upper()
                    np_type, component_count = EFMISkeletonMergeHelper._blend_indices_layout(fmt)
                    if not np_type:
                        return None
                    return {
                        "byte_offset": byte_offset,
                        "byte_width": element_width,
                        "stride": stride,
                        "np_type": np_type,
                        "component_count": component_count,
                    }
                byte_offset += element_width
        return None

    @staticmethod
    def _blend_indices_layout(fmt: str) -> tuple[str, int]:
        """BLENDINDICES 格式 -> (numpy dtype, 通道数)。"""
        layout = {
            "R8G8B8A8_UINT": ("u1", 4),
            "R8G8_UINT": ("u1", 2),
            "R8_UINT": ("u1", 1),
            "R8G8B8A8_SINT": ("i1", 4),
            "R8G8_SINT": ("i1", 2),
            "R8_SINT": ("i1", 1),
            "R16G16B16A16_UINT": ("u2", 4),
            "R16G16_UINT": ("u2", 2),
            "R16_UINT": ("u2", 1),
            "R16G16B16A16_SINT": ("i2", 4),
            "R16G16_SINT": ("i2", 2),
            "R16_SINT": ("i2", 1),
            "R32G32B32A32_UINT": ("u4", 4),
            "R32G32_UINT": ("u4", 2),
            "R32_UINT": ("u4", 1),
            "R32G32B32A32_SINT": ("i4", 4),
            "R32G32_SINT": ("i4", 2),
            "R32_SINT": ("i4", 1),
        }
        return layout.get(fmt, (None, 0))

    @staticmethod
    def _resolve_submesh_json_path(workspace_root: str, unique_str: str) -> str:
        """定位子网格 json（不依赖 bpy/submesh_metadata）。

        规则（与 check_and_get_submesh_json_path 对齐，但无 bpy 依赖）：
        1. Import.json 记录了 unique_str 的数据类型 → `<子网格>/TYPE_<gametype>/<bare>.json`；
        2. 否则：子网格目录下只有一个 TYPE_ 目录含 json 时直接用；多个则拒绝（返回空）。
        unique_str 形如 `LOD0.<drawib>-<n>-<i>` 或 `<drawib>-<n>-<i>`。

        没有 LOD 前缀的身份（旧扁平工作空间遗留 / 手工命名的物体）按默认 LOD0
        兜底：先找裸身份目录，找不到再找 `LOD0/<bare>`（与
        WorkSpaceHelper.get_submesh_folder_path 的目录兜底同口径）。
        """
        lod_name = ""
        bare = unique_str
        if "." in unique_str and unique_str.split(".", 1)[0].upper().startswith("LOD"):
            lod_name, bare = unique_str.split(".", 1)

        # 每轮只用一个 LOD 身份：裸身份优先，整轮都没命中时才轮到 LOD0 兜底，
        # 避免同一身份在裸目录与 LOD0 目录各命中一次被判成"多分区歧义"。
        lod_passes = [[lod_name] if lod_name else [""]]
        if not lod_name:
            lod_passes.append([_DEFAULT_LOD_NAME])

        # 分区工作空间不会把分区名编码进 import_key；调用方仍传全局根目录。
        # 因此必须在这里沿与 WorkSpaceHelper 相同的 Config.json 契约枚举分区，
        # 再用真实命中的分区根读取它自己的 Import.json。多个分区出现同一
        # LOD/bare 时无法可靠消歧，宁可返回空并显式失败，也不能写错 JSON。
        base_candidates = [os.path.abspath(workspace_root)]
        if os.path.isdir(workspace_root):
            try:
                partition_entries = sorted(
                    (
                        entry
                        for entry in os.scandir(workspace_root)
                        if entry.is_dir()
                        and os.path.isfile(os.path.join(entry.path, "Config.json"))
                    ),
                    key=lambda entry: entry.name.casefold(),
                )
            except OSError:
                partition_entries = []
            base_candidates.extend(os.path.abspath(entry.path) for entry in partition_entries)

        found: list[str] = []
        for lod_candidates in lod_passes:
            for candidate_base in base_candidates:
                for candidate_lod in lod_candidates:
                    base = os.path.join(candidate_base, candidate_lod) if candidate_lod else candidate_base
                    submesh_dir = os.path.join(base, bare)
                    if not os.path.isdir(submesh_dir):
                        continue

                    import_json = {}
                    # 根目录映射先读，分区自己的映射后读并覆盖同名项。
                    for import_root in (workspace_root, candidate_base):
                        import_json_path = os.path.join(import_root, "Import.json")
                        if not os.path.isfile(import_json_path):
                            continue
                        try:
                            loaded = JsonUtils.LoadFromFile(import_json_path)
                        except Exception:
                            loaded = None
                        if isinstance(loaded, dict):
                            import_json.update(loaded)

                    # 多 LOD 工作空间的键带 LOD 前缀；本轮身份（含 LOD0 兜底轮）
                    # 对应的键都要试，否则会退化成"扫 TYPE_ 目录"的兜底路径。
                    import_keys = [unique_str]
                    for key_lod in lod_candidates:
                        key = f"{key_lod}.{bare}" if key_lod else bare
                        if key not in import_keys:
                            import_keys.append(key)
                    gametype = ""
                    for key in import_keys:
                        gametype = str(import_json.get(key, "") or "").strip()
                        if gametype:
                            break

                    if gametype:
                        candidate = os.path.join(
                            submesh_dir, "TYPE_" + gametype, bare + ".json"
                        )
                        if os.path.isfile(candidate):
                            found.append(os.path.abspath(candidate))
                            continue

                    try:
                        type_directories = os.listdir(submesh_dir)
                    except OSError:
                        continue
                    local_found = []
                    for dirname in type_directories:
                        if not dirname.startswith("TYPE_"):
                            continue
                        candidate = os.path.join(submesh_dir, dirname, bare + ".json")
                        if os.path.isfile(candidate):
                            local_found.append(os.path.abspath(candidate))
                    if len(local_found) == 1:
                        found.extend(local_found)

            if found:
                break

        unique_found = sorted(set(found), key=str.casefold)
        return unique_found[0] if len(unique_found) == 1 else ""

    @classmethod
    def ensure_skeleton_data(
        cls,
        workspace_root: str,
        unique_str_list: list[str],
        force: bool = False,
        lod_group_projection: bool = True,
        dedup_enabled: bool | None = None,
    ) -> tuple[bool, str]:
        """为 EFMI 工作空间的子网格生成并写回骨骼合并数据（幂等）。

        多 LOD 语义（v10/v11/v13：独立分段 + 镜像分区约束）：
        unique_str_list 按 LOD 前缀分组，每组优先用**自己的 dump** 解析原始骨骼
        候选；dump 不可用时改用该 LOD 子网格中已复制的工作空间原始文件缓存。
        每 LOD 用自身 dump 独立执行权重扩散去重，随后非基准 LOD 的编号空间整体
        **平移**到基准 LOD 段之后（LOD0: 0..max0，LOD1: base 起）——两域不相交、
        全局唯一，跨 LOD 零共享零串扰。v11 追加镜像约束（跨 LOD 对应作为标签：
        L0 合并组 ⇒ L1 对应组必合并、不同 L0 组合并断边）；v13 撤销 v12 的
        值域压缩，保留每个 component 的原生连续声明段。v9 投影
        （LOD1 顶点组映射到 LOD0 槽位 + 运行时 full→lod BlendRemap）实测 LOD1
        爆炸：运行时对同一 component 每帧只导入一次骨骼、仅允许更优 $lod_level
        覆盖，同帧先 LOD0 后 LOD1 时 LOD1 网格读到的是 LOD0 已导入的矩阵
        （L0/L1 两侧矩阵数据不同）。
        ``lod_group_projection`` 控制跨 LOD 约束功能，但不改变独立分段编号：
        True 时用 LOD0 的去重结果镜像约束 LOD1（L0 同组 ⇒ L1 对应组同组，
        L0 不同组 ⇒ L1 对应组断边），并过滤几何未匹配的 LOD1 部件（不生成
        VGMap，json 写 EFMILODProjectionSkipped=True）；False 时不传镜像约束、
        不过滤，LOD0/LOD1 完全按各自 dump 独立去重。两种模式的 LOD 槽位段
        始终互不重叠，绝不会让 LOD1 直接引用 LOD0 槽位。
        只有 LOD0 时直接独立计算，不要求 LOD1 存在；明确标记为
        GPU-PreSkinning=false 的 CPU 子网格不参与骨骼合并，也不会因此使 GPU
        子网格整批回退。投影模式下，非基准 LOD 的 CPU 目标仍参与 LOD0 几何
        匹配裁决（IB 同现判定）并写回 EFMILODProjectionMatched /
        EFMILODProjectionSkipped 标记（C1/C2/C3），但永不生成 VGMap。
        对应成功的部件写入 EFMILODCorrespondence/EFMILOD*
        诊断账本；开关开启时，该账本还用于构造 LOD1 去重约束标签，但不共享
        或重排 LOD0 的实际槽位编号。

        ``dedup_enabled`` 控制顶点组去重（权重扩散合并）：None（省略）沿用全局
        _DEDUP_ENABLED；False 时不执行任何去重，每根骨骼独占槽位
        （build_vg_maps 的恒等映射路径，与 _DEDUP_ENABLED=False 语义一致）。

        返回 (是否成功, 描述)。
        """
        effective_dedup = _DEDUP_ENABLED if dedup_enabled is None else bool(dedup_enabled)
        if not unique_str_list:
            return False, "没有子网格需要处理。"

        lod_map, default_dir = cls.resolve_frame_analysis_dirs_by_lod(workspace_root)

        # 按 LOD 分组（同 LOD 内保持输入顺序）。EFMI 的 CPU/非预蒙皮
        # 子网格没有 BLENDINDICES，也没有可加入统一骨骼池的顶点组；它们
        # 不应因为被完整导入批次一起传入，就把真正的 GPU 蒙皮子网格整批
        # 判成“合并骨骼生成失败”。保留无法定位/无法解析的目标进入后续
        # 失败路径，只有明确声明 GPU-PreSkinning=false 的目标才安全跳过。
        groups: dict[str, list[str]] = {}
        non_skeletal_unique_str_list: list[str] = []
        for unique_str in unique_str_list:
            json_path = cls._resolve_submesh_json_path(workspace_root, unique_str)
            payload = None
            if json_path:
                try:
                    payload = JsonUtils.LoadFromFile(json_path)
                except Exception:
                    payload = None
            if (
                isinstance(payload, dict)
                and payload.get("GPU-PreSkinning") is False
            ):
                non_skeletal_unique_str_list.append(unique_str)
                continue
            groups.setdefault(cls._parse_lod_name(unique_str), []).append(unique_str)
        lod_group_projection = bool(lod_group_projection)

        # C1：CPU/无顶点组目标不得因 GPU-PreSkinning=False 提前退出跨 LOD 几何
        # 匹配流程（归档 68 CPU unknown：黑白名单过滤漏过、全部被错误导入）。
        # 投影模式下，非基准 LOD 的 CPU 目标执行与 GPU 同等的 LOD0 几何匹配
        # 裁决（CPU 无顶点组/骨骼候选，无法进入 build_cross_lod_correspondence
        # 的点云配对，其可用的几何对应证据是 draw IB 是否在基准侧同现）：
        # 匹配成功写 EFMILODProjectionMatched=True（C2：可导入、不生成 VGMap），
        # 匹配失败写 EFMILODProjectionSkipped=True（C3：导入侧排除、不生成
        # VGMap）。基准 LOD 的 CPU 目标与投影关闭场景保持原行为（只跳过骨骼
        # 合并，仍可导入）。
        cpu_matched: set[str] = set()
        cpu_skipped: set[str] = set()
        cpu_adjudication_failed: list[str] = []
        if lod_group_projection and non_skeletal_unique_str_list:
            cpu_matched, cpu_skipped, cpu_adjudication_failed = (
                cls._adjudicate_cpu_projection_targets(
                    workspace_root=workspace_root,
                    cpu_unique_str_list=non_skeletal_unique_str_list,
                    groups=groups,
                    request_unique_strs=set(unique_str_list),
                )
            )
            adjudicated_cpu = cpu_matched | cpu_skipped
        else:
            adjudicated_cpu = set()
        cpu_adjudication_suffix = cls._cpu_adjudication_summary(
            cpu_matched, cpu_skipped, cpu_adjudication_failed
        )

        if not groups:
            unhandled_cpu = sorted(set(non_skeletal_unique_str_list) - adjudicated_cpu)
            skipped_label = "、".join(unhandled_cpu[:5])
            suffix = "…" if len(unhandled_cpu) > 5 else ""
            if skipped_label:
                return True, (
                    "没有需要合并骨骼的 GPU 蒙皮子网格，已跳过非蒙皮子网格: "
                    f"{skipped_label}{suffix}。{cpu_adjudication_suffix}"
                )
            return False, "没有子网格需要处理。" + cpu_adjudication_suffix

        unhandled_non_skeletal = sorted(set(non_skeletal_unique_str_list) - adjudicated_cpu)
        skipped_non_skeletal_message = ""
        if unhandled_non_skeletal:
            skipped_label = "、".join(unhandled_non_skeletal[:5])
            suffix = "…" if len(unhandled_non_skeletal) > 5 else ""
            skipped_non_skeletal_message = (
                "；已跳过非蒙皮子网格: "
                f"{skipped_label}{suffix}"
            )
        skipped_non_skeletal_message += cpu_adjudication_suffix

        parsers_by_lod: dict[str, EFMILogParser | None] = {}

        def _parser_for_lod(lod_name: str) -> EFMILogParser | None:
            if lod_name in parsers_by_lod:
                return parsers_by_lod[lod_name]
            frame_analysis_dir = lod_map.get(lod_name) or default_dir
            label = lod_name or "工作空间根目录"
            parser = None
            log_path = (
                os.path.join(frame_analysis_dir, "log.txt")
                if frame_analysis_dir else ""
            )
            if log_path and os.path.isfile(log_path):
                try:
                    parser = EFMILogParser(log_path)
                except Exception as e:
                    print(
                        f"[EFMI骨骼合并] {label}: FrameAnalysis log 解析失败（{e}），"
                        "将仅用工作空间缓存重建"
                    )
            else:
                print(
                    f"[EFMI骨骼合并] {label}: FrameAnalysis 不可用，"
                    "将仅用工作空间 BoneMatrix/InstanceConfig 缓存重建"
                )
            parsers_by_lod[lod_name] = parser
            return parser

        # 多 LOD 先收集原始候选，再建立跨 LOD 对应。单独只有一个 LOD（包括
        # 关闭分组投影时只有 LOD0）必须直接走下面的单组路径，不能把 LOD1
        # 当成合并计算的前置条件。全独立 + 分段平移（2026-08-27 用户拍板）：
        # 每 LOD 用**自己的 dump** 独立执行权重扩散去重（槽位自洽），随后
        # 非基准 LOD 的编号空间整体**平移**到基准段之后（LOD0: 0..max0，
        # LOD1: base 起），两域不相交、全局唯一——跨 LOD 零共享、零串扰
        # （此前“把 LOD1 投影到参考侧组 id”实为串扰根因：参考号在 LOD1 池
        # 命中错误骨骼段/空洞）。LOD1 独立槽位从自身 0 起步再由平移归位。
        # 已有完整联合缓存时保持幂等，不重复读取大型 Position/Blend 缓冲。
        has_multiple_lods = len(groups) > 1
        if has_multiple_lods:
            joint_cache_ready = True
            projection_skipped_in_joint: set[str] = set()
            for lod_name, group_list in groups.items():
                parser = _parser_for_lod(lod_name)
                for unique_str in group_list:
                    json_path = cls._resolve_submesh_json_path(workspace_root, unique_str)
                    if not json_path:
                        # 任一输入子网格 json 缺失都必须使联合缓存失效：
                        # 缺失者会被跳过重建，其余子网格若仍幂等跳过，
                        # 两侧槽位口径将不一致。
                        joint_cache_ready = False
                        break
                    try:
                        payload = JsonUtils.LoadFromFile(json_path)
                    except Exception:
                        joint_cache_ready = False
                        break
                    if not isinstance(payload, dict):
                        joint_cache_ready = False
                        break
                    if cls._is_projection_skipped(payload, lod_group_projection):
                        # 分组投影裁决的“有意排除”：该目标不生成 VGMap 是设计结果，
                        # 不能当作缓存缺失，否则每次导入都重算整个联合缓存。
                        projection_skipped_in_joint.add(unique_str)
                        continue
                    if not cls._efmi_cache_intact(
                        payload, json_path, unique_str, expected_dedup_enabled=effective_dedup
                    ):
                        joint_cache_ready = False
                        break
                    if (
                        parser is not None
                        and cls._load_workspace_skeleton_source(
                            payload, json_path, unique_str
                        ) is None
                    ):
                        # 与单 LOD 快路径一致：dump 尚在时自动迁移旧版 EFMI
                        # 工作空间，把未来无 dump 重建需要的双来源缓存补齐。
                        joint_cache_ready = False
                        break
                    try:
                        lod_layout_version = int(
                            payload.get("EFMILODLayoutVersion", 0) or 0
                        )
                    except (TypeError, ValueError):
                        lod_layout_version = 0
                    if lod_layout_version != _CROSS_LOD_LAYOUT_VERSION:
                        joint_cache_ready = False
                        break
                    if bool(payload.get("EFMILODProjection", False)) != lod_group_projection:
                        joint_cache_ready = False
                        break
                if not joint_cache_ready:
                    break
            if joint_cache_ready and not force:
                if projection_skipped_in_joint:
                    return True, (
                        "全部子网格已有跨 LOD 骨骼合并缓存（VGMap）；其中 "
                        f"{len(projection_skipped_in_joint)} 个为投影未匹配裁决跳过，无需重新生成。"
                    )
                return True, "全部子网格已有跨 LOD 骨骼合并缓存（VGMap），无需重新生成。"

            collected_by_lod = {}
            for lod_name in sorted(groups.keys()):
                group_list = groups[lod_name]
                parser = _parser_for_lod(lod_name)
                collected, _metadata, _skipped = cls._ensure_skeleton_data_for_group(
                    workspace_root=workspace_root,
                    unique_str_list=group_list,
                    parser=parser,
                    force=True,
                    collect_only=True,
                    dedup_enabled=effective_dedup,
                )
                collected_by_lod[lod_name] = collected

            correspondence = EFMIBoneMapBuilder.build_cross_lod_correspondence(
                collected_by_lod,
                reference_lod="LOD0",
            )
            reference_lod = correspondence.get("reference_lod", "")

            # 分组投影导入过滤裁决（先于编号计算）：非基准 LOD 的部件若未进入
            # 部件一对一配对（unmatched_target_parts）、配对得分超过
            # _CROSS_LOD_PART_IMPORT_SCORE_LIMIT（弱匹配），或**未能收集到原始
            # 候选**（dump/工作空间均无骨骼来源、Blend 无 BLENDINDICES 等——
            # 正是 LOD1 中与 LOD0 无对应的“未知物体”），一律视为“几何匹配不
            # 成功”：不生成 VGMap、导入时排除。
            # **关键**：被排除部件必须**参与编号之前**就剔除——统一顶点组编号
            # 只针对“会被导入的匹配部件”这一整块计算（LOD0 全 + LOD1 保留集），
            # 被排除的 90 多个不能占用任何槽位（否则匹配部件 offset 落点被撑高，
            # 顶点组数量虚涨到两三千，且运行时池出现大段空洞）。
            projection_skip_by_lod: dict[str, set[str]] = {}
            if lod_group_projection:
                score_limit = float(_CROSS_LOD_PART_IMPORT_SCORE_LIMIT)
                for lod_name in sorted(groups.keys()):
                    if lod_name == reference_lod:
                        continue
                    group_list = groups[lod_name]
                    skipped_parts: set[str] = set()
                    for part in (
                        correspondence.get("unmatched_target_parts", {}).get(lod_name, []) or []
                    ):
                        skipped_parts.add(str(part))
                    for row in correspondence.get("part_matches", []) or []:
                        if str(row.get("target_lod", "") or "") != lod_name:
                            continue
                        try:
                            part_score = float(row.get("score", 0.0) or 0.0)
                        except (TypeError, ValueError):
                            part_score = 0.0
                        if part_score > score_limit:
                            skipped_parts.add(str(row.get("target_unique_str", "") or ""))
                    # 未收集目标 = 该 LOD 请求集合中未进入 collected_by_lod 的目标；
                    # 它们是 dump/工作空间里拿不到骨骼原始候选的未知部件，几何上
                    # 无从对应，按“匹配不成功”跳过导入。
                    uncollected = set(group_list) - set(
                        collected_by_lod.get(lod_name, {}) or {}
                    )
                    skipped_parts |= uncollected
                    skipped_parts.discard("")
                    projection_skip_by_lod[lod_name] = skipped_parts

            maps_by_lod = {}
            offsets_by_lod = {}
            reference_skeletons = collected_by_lod.get(reference_lod, {})
            # v10/v11/v13 语义：
            # 每 LOD 用**自己的 dump** 独立执行权重扩散去重（槽位从 0 起），随后
            # 非基准 LOD 的编号空间整体平移到基准 LOD 段之后（LOD0: 0..max0，
            # LOD1: base 起）——两域不相交、全局唯一，跨 LOD 零共享零串扰。
            # 开启分组投影时，跨 LOD 对应作为 L1 去重的镜像约束（L0 合并组 ⇒
            # L1 对应组必合并）；关闭时传 None，恢复纯 v10 双侧独立去重。
            maps_by_lod, offsets_by_lod, _base_slot = (
                EFMIBoneMapBuilder.build_independent_lod_maps(
                    collected_by_lod,
                    reference_lod,
                    projection_skip_by_lod,
                    correspondence=correspondence if lod_group_projection else None,
                    deduplicate=effective_dedup,
                )
            )

            provisional_counts = {
                lod_name: len({
                    int(global_id)
                    for local_map in maps.values()
                    for global_id in local_map.values()
                })
                for lod_name, maps in maps_by_lod.items()
            }
            reference_lod = correspondence.get("reference_lod", "")
            baseline_group_count = int(provisional_counts.get(reference_lod, 0) or 0)
            correspondence["baseline_group_count"] = baseline_group_count
            correspondence["group_count_by_lod"] = dict(provisional_counts)
            correspondence["projection_enabled"] = lod_group_projection

            group_results: list[str] = []
            total_written = 0
            total_skipped = 0
            for lod_name in sorted(groups.keys()):
                group_list = groups[lod_name]
                label = lod_name or "工作空间根目录"
                parser = _parser_for_lod(lod_name)
                written, skipped, message = cls._ensure_skeleton_data_for_group(
                    workspace_root=workspace_root,
                    unique_str_list=group_list,
                    parser=parser,
                    force=True,
                    vg_maps_override=maps_by_lod.get(lod_name),
                    vg_offsets_override=offsets_by_lod.get(lod_name),
                    cross_lod_info=correspondence,
                    projection_skip_parts=(
                        projection_skip_by_lod.get(lod_name, set())
                        if lod_group_projection else None
                    ),
                    dedup_enabled=effective_dedup,
                )
                total_written += written
                total_skipped += skipped
                group_results.append(f"{label}: {message}")

            expected_targets = sum(
                len(set(group_list)) for group_list in groups.values()
            )
            processed = total_written + total_skipped
            if processed == 0:
                if group_results:
                    return False, "没有子网格成功生成骨骼数据（" + "；".join(group_results) + "）"
                return False, "没有子网格成功生成骨骼数据。"
            message = "；".join(group_results)
            message += skipped_non_skeletal_message
            if processed < expected_targets:
                message += f"；共 {expected_targets - processed} 个目标未生成骨骼数据"
            return processed == expected_targets, message

        group_results: list[str] = []
        total_written = 0
        total_skipped = 0
        for lod_name in sorted(groups.keys()):
            group_list = groups[lod_name]
            label = lod_name or "工作空间根目录"
            parser = _parser_for_lod(lod_name)
            written, skipped, message = cls._ensure_skeleton_data_for_group(
                workspace_root=workspace_root,
                unique_str_list=group_list,
                parser=parser,
                force=force,
                dedup_enabled=effective_dedup,
            )
            total_written += written
            total_skipped += skipped
            group_results.append(f"{label}: {message}")

        expected_targets = sum(
            len(set(group_list)) for group_list in groups.values()
        )
        processed = total_written + total_skipped
        if processed == 0:
            if group_results:
                return False, "没有子网格成功生成骨骼数据（" + "；".join(group_results) + "）"
            return False, "没有子网格成功生成骨骼数据。"
        message = "；".join(group_results)
        message += skipped_non_skeletal_message
        if processed < expected_targets:
            message += f"；共 {expected_targets - processed} 个目标未生成骨骼数据"
        # 全部命中缓存（无新写入）也视为成功；但只要存在未处理目标就必须失败
        return processed == expected_targets, message

    @classmethod
    def _ensure_skeleton_data_for_group(
        cls,
        workspace_root: str,
        unique_str_list: list[str],
        parser: EFMILogParser | None,
        force: bool = False,
        protected_pairs: set[tuple[tuple[str, int], tuple[str, int]]] | None = None,
        constraint_labels: dict[tuple[str, int], object] | None = None,
        vg_maps_override: dict[str, dict] | None = None,
        vg_offsets_override: dict[str, int] | None = None,
        collect_only: bool = False,
        cross_lod_info: dict | None = None,
        projection_skip_parts: set[str] | None = None,
        dedup_enabled: bool | None = None,
    ) -> tuple[int, int, str] | tuple[dict[str, tuple], dict[str, dict], int]:
        """为单个 LOD 组的子网格生成并写回骨骼合并数据（幂等）。

        projection_skip_parts：分组投影裁决为“几何匹配不成功”的部件集合。
        命中者不写 VGMap/VGOffset/VGCount，只写 EFMILODProjectionSkipped 标记
        与骨骼来源缓存，供导入侧过滤排除；计入返回的 skipped 使外层对账把
        它们视为“已处理”（既不重复生成，也不误报未完整生成）。

        返回 (written, skipped, 描述消息)；vg_offsets 在该组内从 0 起分配，
        与其它 LOD 组完全独立。collect_only=True 时只返回原始候选和元数据，
        不写文件，供跨 LOD 对应阶段使用。

        ``dedup_enabled`` 控制本组 build_vg_maps 的顶点组去重：None 沿用全局
        _DEDUP_ENABLED；False 时该组每根骨骼独占槽位（恒等映射，不去重）。
        """
        if not unique_str_list:
            return 0, 0, "没有子网格需要处理。"

        # 收集每个子网格的信息
        submesh_skeletons: dict[str, tuple] = {}
        submesh_meta: dict[str, dict] = {}
        skipped = 0

        # 幂等门控（整组原子语义）：单 LOD 组内所有子网格共享同一次 build_vg_maps
        # 分配的全局槽位，绝不允许“部分子网格用缓存、部分子网格重算”——重算的
        # 子网格会从槽位 0 重新编号，与缓存子网格的 VGOffset 碰撞（实测复现：
        # A VGOffset=0、B VGOffset=1，仅 B 失效后 B 被单独重算成 VGOffset=0）。
        # 因此：全部缓存完整才整批跳过；任一子网格缓存失效则整组全部重算。
        resolved_cache: dict[str, bool] = {}
        for unique_str in unique_str_list:
            json_path = cls._resolve_submesh_json_path(workspace_root, unique_str)
            if not json_path:
                # 目标无法定位必须记为缓存失效：否则"全部已缓存"的批跳过
                # 会把该目标静默遗漏、误报整组缓存完整。
                resolved_cache[unique_str] = False
                continue
            try:
                cached_json = JsonUtils.LoadFromFile(json_path)
            except Exception:
                resolved_cache[unique_str] = False
                continue
            if not isinstance(cached_json, dict):
                resolved_cache[unique_str] = False
                continue
            cache_intact = cls._efmi_cache_intact(
                cached_json, json_path, unique_str, expected_dedup_enabled=dedup_enabled
            )
            if (
                cache_intact
                and parser is not None
                and cls._load_workspace_skeleton_source(
                    cached_json, json_path, unique_str
                ) is None
            ):
                # 兼容旧版工作空间：旧 VGMap 本身可以完整，但没有复制
                # InstanceConfig/first_constant，日后清 VGMap + 删 dump 就无法重建。
                # 只在当前 dump 仍可用时打破快路径，借本次重建自动补齐来源缓存；
                # dump 已删除时仍保留现有完整 VGMap，不做无法完成的破坏性迁移。
                cache_intact = False
            resolved_cache[unique_str] = cache_intact
        if (
            not force
            and not collect_only
            and resolved_cache
            and all(resolved_cache.values())
        ):
            skipped = len(resolved_cache)
            return 0, skipped, (
                f"全部 {skipped} 个子网格已有骨骼合并缓存（VGMap），无需重新生成。"
            )

        seen_targets: set[str] = set()
        for unique_str in unique_str_list:
            if unique_str in seen_targets:
                continue
            seen_targets.add(unique_str)
            json_path = cls._resolve_submesh_json_path(workspace_root, unique_str)
            if not json_path:
                print(f"[EFMI骨骼合并] 跳过 {unique_str}: 未找到子网格 json")
                continue

            submesh_json = JsonUtils.LoadFromFile(json_path)
            if not isinstance(submesh_json, dict):
                continue

            # 整组原子语义下这里不再单独跳过：组内任一子网格缓存失效时
            # 全组重算（见上方门控注释）。存在旧缓存但失效的子网格打印原因，
            # 方便排查（版本过期 / schema 缺字段 / BoneMatrix 文件缺失等）。
            if submesh_json.get("VGMap"):
                print(
                    f"[EFMI骨骼合并] {unique_str}: VGMap 缓存不完整或版本失效，"
                    f"按算法版本 {_VG_MAP_ALGORITHM_VERSION} 整组重算"
                )

            element_info = cls.parse_blend_element_info(submesh_json)
            if element_info is None:
                print(f"[EFMI骨骼合并] 跳过 {unique_str}: Blend 类别缺少 BLENDINDICES 元素")
                continue

            bare_name = os.path.splitext(os.path.basename(json_path))[0]
            submesh_dir = os.path.dirname(os.path.dirname(json_path))  # TYPE_ 的上一级 = 子网格目录
            blend_file_name = (
                EFMIBoneMapBuilder._category_buffer_path_from_json(
                    submesh_json, "Blend"
                )
                or (bare_name + "-Blend.buf")
            )
            blend_buf_path = os.path.join(os.path.dirname(json_path), blend_file_name)

            # 子网格 -> drawcall 索引（优先角色级映射，其次 LOD 目录映射，最后 dump 反查兜底）
            drawcall_index_list: list[str] = []
            lod_dir = os.path.dirname(submesh_dir)
            role_mapping = cls.load_drawcall_index_list(lod_dir)
            drawcall_index_list = role_mapping.get(os.path.basename(submesh_dir), [])
            if not drawcall_index_list:
                role_mapping_root = cls.load_drawcall_index_list(workspace_root)
                drawcall_index_list = role_mapping_root.get(os.path.basename(submesh_dir), [])

            # 兜底：ComponentName 映射缺失/被重置时，从 dump 按 ib hash + index_count + first_index 反查
            if not drawcall_index_list and parser is not None:
                submesh_name = os.path.basename(submesh_dir)
                parts = submesh_name.split("-")
                if len(parts) >= 3:
                    try:
                        draw_ib = parts[0]
                        index_count = int(parts[1])
                        first_index = int(parts[2])
                        drawcall_index_list = parser.find_drawcalls_by_ib(
                            draw_ib, index_count, first_index
                        )
                        if drawcall_index_list:
                            print(
                                f"[EFMI骨骼合并] {unique_str}: ComponentName 缺失，"
                                f"已从 dump 反查 drawcall {drawcall_index_list[:3]}"
                            )
                    except (ValueError, IndexError):
                        pass

            # 优先从当前 dump 读取；dump 不可用/相关原文件被删时，回退首次导入
            # 复制到工作空间的骨骼池 + instance-config 原文件和 first_constant。
            # 取第一个有有效骨骼数据的 drawcall，并记录实际成功的 draw_index。
            # 后备 drawcall 成功后元数据仍必须指向它——骨骼池复制（vs-t0）按
            # draw_index 反查，指向第一个失败的候选会拿到错误骨骼池或根本没有。
            skeleton_source = None
            if parser is not None:
                builder = EFMIBoneMapBuilder(parser)
                for draw_index in drawcall_index_list:
                    candidate_source = builder.get_skeleton_source(draw_index)
                    if candidate_source is not None:
                        skeleton_source = candidate_source
                        break
            if skeleton_source is None:
                skeleton_source = cls._load_workspace_skeleton_source(
                    submesh_json,
                    json_path,
                    unique_str,
                )
                if skeleton_source is not None:
                    print(
                        f"[EFMI骨骼合并] {unique_str}: dump 骨骼来源不可用，"
                        "已回退工作空间 BoneMatrix/InstanceConfig 缓存"
                    )

            if skeleton_source is None:
                source_reason = (
                    "未找到 drawcall 映射，且工作空间骨骼来源缓存不完整"
                    if not drawcall_index_list
                    else "dump 与工作空间均无法读取完整骨骼来源"
                )
                print(f"[EFMI骨骼合并] 跳过 {unique_str}: {source_reason}")
                continue
            skeleton_buffer = skeleton_source["skeleton_buffer"]
            skeleton_draw_index = str(skeleton_source.get("draw_index", "") or "")

            try:
                blend_indices = EFMIBoneMapBuilder.parse_blendindices_from_buf(blend_buf_path, element_info)
                blend_layout = EFMIBoneMapBuilder.parse_blend_layout(submesh_json)
                blend_weights = EFMIBoneMapBuilder.parse_blendweights_from_buf(
                    blend_buf_path, blend_layout
                )
            except Exception as e:
                print(f"[EFMI骨骼合并] 跳过 {unique_str}: 读取 Blend.buf 失败: {e}")
                continue

            # vg_count 复用与 ZZMI 相同的有效通道判定（按数据格式排除哨兵 +
            # 正权重过滤）：u1 的 0xFF / u2 的 0xFFFF / u4|i4 的 0xFFFFFFFF
            # 都不再被算成真实骨骼。
            valid_mask = EFMIBoneMapBuilder.valid_blend_channels(
                blend_indices, element_info, blend_weights
            )
            valid_indices = blend_indices[valid_mask].astype(numpy.int64)
            if len(valid_indices) == 0:
                print(f"[EFMI骨骼合并] 跳过 {unique_str}: BLENDINDICES 无有效通道")
                continue
            vg_count = int(valid_indices.max()) + 1

            # 先与骨骼段长度比对，再 bincount——bincount 的 minlength=vg_count
            # 会按 vg_count 分配内存，损坏数据（如未被哨兵覆盖的巨值索引）
            # 必须在这里被骨骼段上限拦截，否则可能尝试分配数十 GB。
            if len(skeleton_buffer) < vg_count:
                print(
                    f"[EFMI骨骼合并] 跳过 {unique_str}: 骨骼段 {len(skeleton_buffer)} < 顶点组 {vg_count}"
                )
                continue
            weighted_vertex_counts = numpy.bincount(valid_indices, minlength=vg_count)

            # 权重扩散去重签名（读 Position.buf + Blend.buf 计算每骨骼的
            # 正权重采样场；质心/包围盒仅作回退与剪枝）
            position_file_name = (
                EFMIBoneMapBuilder._category_buffer_path_from_json(
                    submesh_json, "Position"
                )
                or (bare_name + "-Position.buf")
            )
            position_buf_path = os.path.join(os.path.dirname(json_path), position_file_name)
            try:
                centroids = EFMIBoneMapBuilder.compute_driven_signatures(
                    position_buf_path, blend_buf_path, submesh_json
                )
            except Exception as e:
                print(f"[EFMI骨骼合并] {unique_str}: 驱动签名计算失败（退化为纯矩阵匹配）: {e}")
                centroids = {}

            submesh_skeletons[unique_str] = (skeleton_buffer, vg_count, weighted_vertex_counts, centroids)
            submesh_meta[unique_str] = {
                "json_path": json_path,
                "submesh_dir": submesh_dir,
                "bare_name": bare_name,
                "dedup_excluded": bool(submesh_json.get("VGMapDedupExcluded")),
                # 实际成功读到骨骼数据的 draw_index（后备 drawcall 成功时
                # 绝不能回落到 drawcall_index_list[0] 的失败候选）
                "draw_index": skeleton_draw_index,
                "pool_path": skeleton_source["pool_path"],
                "instance_config_path": skeleton_source["instance_config_path"],
                "instance_config_first_constant": int(
                    skeleton_source["first_constant"]
                ),
            }

        if collect_only:
            # 联合 LOD 流程需要保留去重前的完整候选；这里不写任何缓存。
            return submesh_skeletons, submesh_meta, skipped

        if not submesh_skeletons:
            return 0, 0, "没有子网格成功生成骨骼数据。"

        # 组内跨子网格去重构建 vg_map（组内从 0 起分配槽位，与其它 LOD 组互不影响）。
        # 联合 LOD 写回阶段传入预计算映射：LOD0 使用唯一一次真实去重结果，
        # LOD1 使用按对应关系投影后的映射，避免重新跑一套权重扩散并查集。
        if vg_maps_override is not None and vg_offsets_override is not None:
            vg_maps = vg_maps_override
            vg_offsets = vg_offsets_override
        else:
            vg_maps, vg_offsets = EFMIBoneMapBuilder.build_vg_maps(
                submesh_skeletons,
                protected_pairs=protected_pairs,
                constraint_labels=constraint_labels,
                deduplicate=dedup_enabled,
                dedup_excluded={
                    us for us, meta in submesh_meta.items()
                    if meta.get("dedup_excluded")
                },
            )

        # 写回工作空间 json + 复制骨骼池缓存
        written = 0
        written_targets: set[str] = set()
        projection_skipped_targets: set[str] = set()
        # 本次参与编号但未产出槽位映射、已清空旧代际键的目标（见主循环内说明）。
        stale_cleared: list[str] = []
        for unique_str, entry in submesh_skeletons.items():
            skeleton_buffer = entry[0]
            vg_count = entry[1]
            meta = submesh_meta[unique_str]
            json_path = meta["json_path"]
            submesh_json = JsonUtils.LoadFromFile(json_path)
            if not isinstance(submesh_json, dict):
                continue

            if projection_skip_parts and unique_str in projection_skip_parts:
                # 分组投影裁决：几何匹配不成功 -> 不导入。JSON 不写任何 VGMap/
                # VGOffset/VGCount（导入侧据此排除对象、导出侧自然不含该部件），
                # 但仍写“投影未匹配”标记与骨骼来源缓存（保留日后清缓存/取消
                # 过滤后凭工作空间原文件重建的能力），并发布 BoneMatrix/
                # InstanceConfig 两份缓存。
                try:
                    # 历史缓存键必须清空（统一口径见 _clear_stale_vgmap_keys）：
                    # 旧版可能留有 VGCount=0/VGOffset=0 等半成品，残留会让
                    # 导入/幂等判定产生歧义。
                    cls._clear_stale_vgmap_keys(submesh_json)
                    cls._mark_projection_skipped(submesh_json, cross_lod_info)
                    cls._publish_skeleton_source_cache(meta, submesh_json, vg_count)
                except Exception as e:
                    print(
                        f"[EFMI骨骼合并] 提交投影未匹配标记/来源缓存事务失败 {unique_str}: {e}"
                    )
                    continue
                projection_skipped_targets.add(unique_str)
                continue

            vg_map = vg_maps.get(unique_str, {})
            if not vg_map:
                # 本次参与了编号但**没有产出任何槽位映射**（骨骼池读取失败、
                # 声明顶点组数超过 pool 容量、无有效 BLENDINDICES 等 ——
                # build_vg_maps 只为有候选骨骼的部件建条目）。旧实现在这里
                # 直接 continue：json 里上一代的 VGMap/VGOffset/VGCount 被静默
                # 保留成为「陈旧声明」，与本次新布局的其它部件段并集错配，导出
                # 侧按 A3/B10/B11 fail-closed 中止（B11 报「引用了无人声明的
                # 槽位」，病根却是这份 json 从未被清理）。改为明确清空键族并
                # 提交 json 事务：_efmi_cache_intact 立即判定缓存不完整，下次
                # ensure 整组重算（自愈），工作区不再残留跨代际的静默声明。
                try:
                    cls._clear_stale_vgmap_keys(submesh_json)
                    cls._atomic_publish_skeleton_transaction(
                        [], submesh_json, json_path
                    )
                except Exception as e:
                    print(
                        f"[EFMI骨骼合并] 清理陈旧 VGMap 键失败 {unique_str}: {e}"
                    )
                    continue
                stale_cleared.append(unique_str)
                continue

            # 只要本次写回 VGMap，就撤销任何历史“投影未匹配”标记（策略变更/
            # 取消过滤后该部件回归导入，标记不得残留造成导入侧误排除）。
            submesh_json.pop("EFMILODProjectionSkipped", None)

            # VGOffset = 该子网格在组内全局骨架中的起始（取去重时分配的槽位，保证与 vg_map 一致）
            vg_offset = vg_offsets.get(unique_str, 0)

            submesh_json["VGCount"] = vg_count
            submesh_json["VGOffset"] = vg_offset
            submesh_json["VGMap"] = {str(k): int(v) for k, v in sorted(vg_map.items())}
            submesh_json["VGMapAlgorithmVersion"] = _VG_MAP_ALGORITHM_VERSION
            submesh_json["VGMapDedupEnabled"] = (
                bool(_DEDUP_ENABLED) if dedup_enabled is None else bool(dedup_enabled)
            )
            # F1（t1 §4.5）：源数据指纹。去重结果依赖 json 同目录的
            # Position.buf/Blend.buf；写回缓存时刻的 (mtime_ns, size) 指纹入 json，
            # _efmi_cache_intact 据此在「源数据变更而 json 未变」（重新提取 dump /
            # 替换绑定姿势 buffer）时自动失效整批重算（旧实现只校验版本常量，
            # 陈旧合并布局会持续生效）。无声明 Position/Blend 的 json 记 None。
            submesh_json["EFMIVGMapSourceFingerprint"] = (
                EFMIBoneMapBuilder._vgmap_source_fingerprint(
                    os.path.dirname(json_path), submesh_json
                )
            )

            # 联合 LOD 对应只写诊断/后续处理元数据，不改变本 LOD 的运行时
            # offset 布局。这样 LOD0/LOD1 仍可各自挂载自己的骨骼池，同时保留
            # “哪一个原始组对应哪一个基准组、是否存在缺口”的事实账本。
            if cross_lod_info:
                reference_lod = str(cross_lod_info.get("reference_lod", "") or "")
                current_lod = cls._parse_lod_name(unique_str)
                baseline_count = int(cross_lod_info.get("baseline_group_count", 0) or 0)
                count_by_lod = cross_lod_info.get("group_count_by_lod", {}) or {}
                actual_count = int(count_by_lod.get(current_lod, 0) or 0)
                # 账本口径遵守 LOD0 基准：LOD1 如果原始候选确实少，不能把
                # 少出来的语义槽位悄悄当成“已对应”。Actual 保留真实去重数，
                # GroupCount 是后续对应层使用的有效槽位下限。
                current_count = max(actual_count, baseline_count) \
                    if current_lod != reference_lod else actual_count
                correspondence_rows = {}
                for row in cross_lod_info.get("matches", []) or []:
                    if row.get("reference_lod") == reference_lod and current_lod == reference_lod:
                        if row.get("reference_unique_str") != unique_str:
                            continue
                        local_id = row.get("reference_local_vg_id")
                        correspondence_rows[str(local_id)] = {
                            "lod": row.get("target_lod", ""),
                            "unique_str": row.get("target_unique_str", ""),
                            "local_vg_id": int(row.get("target_local_vg_id", 0) or 0),
                            "reference_component": row.get("reference_component", unique_str),
                            "target_component": row.get(
                                "target_component", row.get("target_unique_str", "")
                            ),
                            "component_score": float(row.get("component_score", 0.0) or 0.0),
                            "matrix_diff": float(row.get("matrix_diff", 0.0) or 0.0),
                            "matrix_diff_rotation": float(
                                row.get("matrix_diff_rotation", 0.0) or 0.0
                            ),
                            "centroid_distance": row.get("centroid_distance"),
                        }
                    elif row.get("target_lod") == current_lod:
                        if row.get("target_unique_str") != unique_str:
                            continue
                        local_id = row.get("target_local_vg_id")
                        correspondence_rows[str(local_id)] = {
                            "lod": row.get("reference_lod", ""),
                            "unique_str": row.get("reference_unique_str", ""),
                            "local_vg_id": int(row.get("reference_local_vg_id", 0) or 0),
                            "reference_component": row.get(
                                "reference_component", row.get("reference_unique_str", "")
                            ),
                            "target_component": row.get("target_component", unique_str),
                            "component_score": float(row.get("component_score", 0.0) or 0.0),
                            "matrix_diff": float(row.get("matrix_diff", 0.0) or 0.0),
                            "matrix_diff_rotation": float(
                                row.get("matrix_diff_rotation", 0.0) or 0.0
                            ),
                            "centroid_distance": row.get("centroid_distance"),
                        }
                submesh_json["EFMILODCorrespondence"] = correspondence_rows
                submesh_json["EFMILODLayoutVersion"] = _CROSS_LOD_LAYOUT_VERSION
                submesh_json["EFMILODReference"] = reference_lod
                submesh_json["EFMILODProjection"] = bool(
                    cross_lod_info.get("projection_enabled", True)
                )
                submesh_json["EFMILODBaselineGroupCount"] = baseline_count
                submesh_json["EFMILODGroupCount"] = current_count
                submesh_json["EFMILODActualGroupCount"] = actual_count
                submesh_json["EFMILODMissingBaselineCount"] = max(
                    baseline_count - actual_count, 0
                ) if current_lod != reference_lod else 0
            else:
                # F6（t1 §3.4）：单 LOD 批次也盖章「三件套」使 json 自描述。
                # 单 LOD（或无跨 LOD 对应）时槽位按原生 0 起（无分段平移），
                # 旧实现不写任何 EFMILOD* 键，产生「非规范布局」的临时状态；
                # 下次联合导入时被版本门控发现并整批重算（可自愈，无静默损坏）。
                # 现盖章 layout version（reference 留空、projection 记 False）：
                # - 联合导入门控（:4217）见到版本 13 + Projection=False 与开启的
                #   投影开关不一致 → 整批重算平移，行为不变且更显式；
                # - 单 LOD 自身幂等判定不受影响（_is_projection_skipped 需
                #   EFMILODProjectionSkipped=True，此处不写该键）。
                submesh_json["EFMILODLayoutVersion"] = _CROSS_LOD_LAYOUT_VERSION
                submesh_json["EFMILODReference"] = ""
                submesh_json["EFMILODProjection"] = False

            # 双来源缓存与 JSON 作为同一可回滚文件事务提交；任一准备/替换失败
            # 都不能留下“新 BoneMatrix + 旧 InstanceConfig/JSON”的混合状态。
            try:
                cls._publish_skeleton_source_cache(meta, submesh_json, vg_count)
            except Exception as e:
                print(f"[EFMI骨骼合并] 提交骨骼来源/JSON 事务失败 {unique_str}: {e}")
                continue

            written += 1
            written_targets.add(unique_str)

        # 分组投影跳过但**未收集**的目标（dump/工作空间无骨骼原始候选、Blend 无
        # BLENDINDICES 等）：它们不在 submesh_skeletons 里，主循环写不到，这里
        # 单独打“投影未匹配”标记（无来源缓存可发布，只提交 JSON）。json 也读
        # 不出来的目标保持未处理状态，由外层按失败处理（不静默丢弃）。
        for unique_str in sorted(set(unique_str_list)):
            if unique_str in submesh_skeletons or unique_str in projection_skipped_targets:
                continue
            if not (projection_skip_parts and unique_str in projection_skip_parts):
                continue
            json_path = cls._resolve_submesh_json_path(workspace_root, unique_str)
            if not json_path:
                continue
            try:
                submesh_json = JsonUtils.LoadFromFile(json_path)
                if not isinstance(submesh_json, dict):
                    continue
                cls._clear_stale_vgmap_keys(submesh_json)
                cls._mark_projection_skipped(submesh_json, cross_lod_info)
                cls._atomic_publish_skeleton_transaction([], submesh_json, json_path)
            except Exception as e:
                print(
                    f"[EFMI骨骼合并] 写入投影未匹配标记失败 {unique_str}: {e}"
                )
                continue
            projection_skipped_targets.add(unique_str)

        # 完整性：written + skipped 必须覆盖全部请求目标；重建过程中任何
        # 读取失败/生成失败的目标都会让 unprocessed > 0，由外层据此判定失败。
        unprocessed_targets = sorted(
            set(unique_str_list) - written_targets - projection_skipped_targets
        )
        unprocessed_count = len(unprocessed_targets)
        message = f"已为 {written} 个子网格生成骨骼合并数据"
        if skipped:
            message += f"（跳过已缓存 {skipped} 个）"
        if projection_skipped_targets:
            shown = sorted(projection_skipped_targets)[:5]
            suffix = "…" if len(projection_skipped_targets) > 5 else ""
            message += (
                f"；按跨 LOD 投影未匹配跳过 {len(projection_skipped_targets)} 个: "
                f"{'、'.join(shown)}{suffix}"
            )
        if stale_cleared:
            shown = sorted(stale_cleared)[:5]
            suffix = "…" if len(stale_cleared) > 5 else ""
            message += (
                f"；{len(stale_cleared)} 个部件本次未产出槽位映射，"
                f"已清理旧代际 VGMap 键（下次导入整组重算）: "
                f"{'、'.join(shown)}{suffix}"
            )
        if unprocessed_count > 0:
            shown = unprocessed_targets[:5]
            suffix = "…" if len(unprocessed_targets) > 5 else ""
            message += (
                f"；{unprocessed_count} 个目标未生成骨骼数据: "
                f"{'、'.join(shown)}{suffix}"
            )
        # 投影未匹配的目标按“已处理”口径计入 skipped，使外层 processed 对账通过
        skipped += len(projection_skipped_targets)
        return written, skipped, message

    @classmethod
    def _clear_stale_vgmap_keys(cls, submesh_json: dict) -> None:
        """清空 VGMap 键族 + F1 源指纹（陈旧声明的唯一清理口径）。

        写回侧的三个「不再持有槽位映射」出口（投影未匹配跳过、本次未产出映射、
        未收集目标）必须走同一份键族清单：早期实现各写各的 pop 列表，导致
        `common/efmi_skeleton.py` 主循环 `if not vg_map: continue` 那条路径漏清
        旧代际键 —— json 里残留上一代布局的 VGMap/VGOffset/VGCount，与本次新
        布局的其它部件段并集错配，导出侧按 A3/B10/B11 fail-closed 中止（B11
        报「引用了无人声明的槽位」，病根却是这份 json 没被清理）。

        只改内存 dict；落盘由调用方用 `_atomic_publish_skeleton_transaction`
        事务提交。清空后 `_efmi_cache_intact` 立即判定缓存不完整，下次 ensure
        整组重算（自愈），不静默保留跨代际声明。
        """
        for stale_key in (
            "VGMap",
            "VGOffset",
            "VGCount",
            "VGMapAlgorithmVersion",
            "VGMapDedupEnabled",
            "EFMIVGMapSourceFingerprint",
        ):
            submesh_json.pop(stale_key, None)

    @staticmethod
    def _mark_projection_skipped(submesh_json: dict, cross_lod_info: dict | None) -> None:
        """给子网格 json 写入“跨 LOD 投影未匹配”裁决标记。

        写 EFMILODProjectionSkipped=True + 布局版本/基准 LOD/投影开关三件套，
        供幂等门控与导入侧过滤读取；不写任何 VGMap 系列字段。同时清除对侧
        的“匹配成功”标记，保证任一 json 至多携带一种裁决。
        """
        submesh_json["EFMILODLayoutVersion"] = _CROSS_LOD_LAYOUT_VERSION
        submesh_json["EFMILODReference"] = (
            str(cross_lod_info.get("reference_lod", "") or "")
            if cross_lod_info else ""
        )
        submesh_json["EFMILODProjection"] = True
        submesh_json["EFMILODProjectionSkipped"] = True
        submesh_json.pop("EFMILODProjectionMatched", None)

    @staticmethod
    def _mark_projection_matched(submesh_json: dict, cross_lod_info: dict | None) -> None:
        """给子网格 json 写入“跨 LOD 投影匹配成功”裁决标记（CPU/无顶点组目标）。

        与 _mark_projection_skipped 同构：EFMILODProjectionMatched=True +
        布局版本/基准 LOD/投影开关三件套。匹配成功只是“几何匹配裁决通过”，
        不写任何 VGMap 系列字段——CPU 目标永无顶点组，静态网格导入。
        """
        submesh_json["EFMILODLayoutVersion"] = _CROSS_LOD_LAYOUT_VERSION
        submesh_json["EFMILODReference"] = (
            str(cross_lod_info.get("reference_lod", "") or "")
            if cross_lod_info else ""
        )
        submesh_json["EFMILODProjection"] = True
        submesh_json["EFMILODProjectionMatched"] = True
        submesh_json.pop("EFMILODProjectionSkipped", None)

    @staticmethod
    def _cpu_ib_intersects_baseline(unique_str: str, baseline_strs: set[str]) -> bool:
        """CPU/无顶点组目标的 LOD0 几何对应判定（draw IB 同现）。

        CPU 目标没有顶点组/骨骼候选，无法进入基于蒙皮点云的
        build_cross_lod_correspondence 配对；其可用的几何匹配证据是子网格名
        首段的 draw IB hash 是否在基准 LOD 侧同现（归档调查 F11：68 个 CPU
        的 IB 与 LOD0 全部 20 个零交集 → 按几何匹配语义全部应判“未匹配”）。
        """
        bare = str(unique_str or "").split(".", 1)[-1]
        ib = bare.split("-", 1)[0].strip().lower()
        if not ib:
            return False
        for item in baseline_strs:
            other_ib = str(item or "").split(".", 1)[-1].split("-", 1)[0].strip().lower()
            if other_ib == ib:
                return True
        return False

    @classmethod
    def _adjudicate_cpu_projection_targets(
        cls,
        workspace_root: str,
        cpu_unique_str_list: list[str],
        groups: dict[str, list[str]],
        request_unique_strs: set[str],
    ) -> tuple[set[str], set[str], list[str]]:
        """对非基准 LOD 的 CPU/无顶点组目标执行 LOD0 几何匹配裁决并写回 json。

        裁决语义（契约 C1/C2/C3）：
        - draw IB 在基准 LOD 侧同现 → 匹配成功：写 EFMILODProjectionMatched=True
          （C2：导入侧放行、不生成 VGMap/骨骼槽位）；
        - 不同现 → 匹配失败：写 EFMILODProjectionSkipped=True（C3：导入侧排除、
          不生成 VGMap；无来源缓存可发布，只提交 json 事务）。
        基准 LOD 的 CPU 目标不裁决、不写标记（C5：LOD0 行为不变）。
        返回 (匹配成功集合, 匹配失败集合, 写入失败列表)——失败目标由外层按
        “未处理”报告，导入侧对无标记目标 fail-closed 排除（C4）。
        """
        matched: set[str] = set()
        skipped: set[str] = set()
        failures: list[str] = []
        if not cpu_unique_str_list:
            return matched, skipped, failures

        found_lods = sorted(
            set(groups.keys())
            | {cls._parse_lod_name(u) for u in request_unique_strs}
            - {""}
        )
        reference_lod = "LOD0" if "LOD0" in found_lods else (
            found_lods[0] if found_lods else ""
        )
        if not reference_lod:
            return matched, skipped, failures

        # 基准侧的“可与目标对应的物体” = 基准 LOD 组内全部目标 + 基准 LOD 的
        # CPU 目标（同 IB 跨 LOD 组件可能两侧都是 CPU/无顶点组）。
        baseline_strs = set(groups.get(reference_lod, []) or [])
        baseline_strs |= {
            u for u in cpu_unique_str_list
            if cls._parse_lod_name(u) == reference_lod
        }
        cross_lod_info = {"reference_lod": reference_lod}
        for unique_str in cpu_unique_str_list:
            lod_name = cls._parse_lod_name(unique_str)
            if not lod_name or lod_name == reference_lod:
                continue
            json_path = cls._resolve_submesh_json_path(workspace_root, unique_str)
            if not json_path:
                failures.append(unique_str)
                continue
            try:
                payload = JsonUtils.LoadFromFile(json_path)
            except Exception:
                failures.append(unique_str)
                continue
            if not isinstance(payload, dict):
                failures.append(unique_str)
                continue
            is_matched = cls._cpu_ib_intersects_baseline(unique_str, baseline_strs)
            # 清历史键：半成品 VGMap 系列与对侧裁决标记，避免幂等判定歧义
            # （统一口径见 _clear_stale_vgmap_keys）。
            cls._clear_stale_vgmap_keys(payload)
            if is_matched:
                cls._mark_projection_matched(payload, cross_lod_info)
            else:
                cls._mark_projection_skipped(payload, cross_lod_info)
            try:
                cls._atomic_publish_skeleton_transaction([], payload, json_path)
            except Exception as e:
                print(f"[EFMI骨骼合并] 写入 CPU 投影裁决标记失败 {unique_str}: {e}")
                failures.append(unique_str)
                continue
            (matched if is_matched else skipped).add(unique_str)
        return matched, skipped, failures

    @staticmethod
    def _cpu_adjudication_summary(
        cpu_matched: set[str],
        cpu_skipped: set[str],
        cpu_failures: list[str],
    ) -> str:
        """CPU 投影裁决结果的消息后缀（无结果时返回空串）。"""
        parts = []
        if cpu_matched or cpu_skipped:
            parts.append(
                f"CPU 投影裁决：{len(cpu_matched)} 个匹配成功可导入、"
                f"{len(cpu_skipped)} 个未匹配跳过"
            )
        if cpu_failures:
            shown = "、".join(sorted(cpu_failures)[:5])
            suffix = "…" if len(cpu_failures) > 5 else ""
            parts.append(
                f"{len(cpu_failures)} 个 CPU 目标裁决标记写入失败: {shown}{suffix}"
            )
        return ("；" + "；".join(parts)) if parts else ""

    @classmethod
    def _publish_skeleton_source_cache(
        cls,
        meta: dict,
        submesh_json: dict,
        vg_count: int,
    ) -> None:
        """写骨骼来源缓存字段并按可回滚文件事务发布 BoneMatrix/InstanceConfig。

        同时服务常规写回（配合 VGMap）与投影未匹配标记写回（不写 VGMap，
        但保留日后凭工作空间原文件重建的能力）；任何失败向上抛出。
        """
        runtime_dir = os.path.join(meta["submesh_dir"], "ModImpRuntime")
        dest_name = f"{meta['bare_name']}-BoneMatrix.buf"
        dest_path = os.path.join(runtime_dir, dest_name)
        submesh_json["BoneMatrixFileName"] = dest_name

        instance_dest_name = f"{meta['bare_name']}-InstanceConfig.buf"
        instance_dest_path = os.path.join(runtime_dir, instance_dest_name)
        first_constant = int(meta["instance_config_first_constant"])
        submesh_json["InstanceConfigFileName"] = instance_dest_name
        submesh_json["InstanceConfigFirstConstant"] = first_constant
        if meta.get("draw_index"):
            submesh_json["SkeletonSourceDrawIndex"] = str(meta["draw_index"])
        cls._atomic_publish_skeleton_transaction(
            [
                {
                    "source_path": meta["pool_path"],
                    "dest_path": dest_path,
                    "vg_count": vg_count,
                    "min_size": 4,
                },
                {
                    "source_path": meta["instance_config_path"],
                    "dest_path": instance_dest_path,
                    "vg_count": 0,
                    "min_size": (first_constant + 16) * 16,
                },
            ],
            submesh_json,
            meta["json_path"],
        )

    @staticmethod
    def _runtime_cache_path(submesh_json: dict, json_path: str, unique_str: str) -> str:
        """解析子网格 json 的 BoneMatrixFileName 指向的实际缓存路径。

        只接受纯文件名（拒绝路径穿越）；指向 ModImpRuntime 下的文件。
        返回路径字符串（文件可能不存在，由调用方 isfile 校验）。
        """
        file_name = str(submesh_json.get("BoneMatrixFileName", "") or "").strip()
        if not file_name or os.path.basename(file_name) != file_name:
            bare_name = unique_str.split(".", 1)[-1]
            file_name = f"{bare_name}-BoneMatrix.buf"
        submesh_dir = os.path.dirname(os.path.dirname(json_path))
        return os.path.join(submesh_dir, "ModImpRuntime", file_name)

    @staticmethod
    def _instance_config_cache_path(
        submesh_json: dict,
        json_path: str,
        unique_str: str,
    ) -> str:
        """解析 EFMI instance-config 原文件的工作空间缓存路径。"""
        file_name = str(
            submesh_json.get("InstanceConfigFileName", "") or ""
        ).strip()
        if not file_name or os.path.basename(file_name) != file_name:
            bare_name = unique_str.split(".", 1)[-1]
            file_name = f"{bare_name}-InstanceConfig.buf"
        submesh_dir = os.path.dirname(os.path.dirname(json_path))
        return os.path.join(submesh_dir, "ModImpRuntime", file_name)

    @classmethod
    def _load_workspace_skeleton_source(
        cls,
        submesh_json: dict,
        json_path: str,
        unique_str: str,
    ) -> dict | None:
        """仅靠工作空间缓存恢复 EFMI 骨骼段及其两份原始来源文件。"""
        pool_path = cls._runtime_cache_path(submesh_json, json_path, unique_str)
        instance_config_path = cls._instance_config_cache_path(
            submesh_json, json_path, unique_str
        )
        if not os.path.isfile(pool_path) or not os.path.isfile(instance_config_path):
            return None
        try:
            first_constant = int(submesh_json.get("InstanceConfigFirstConstant"))
        except (TypeError, ValueError):
            return None
        if first_constant < 0:
            return None
        skeleton_buffer = EFMIBoneMapBuilder.load_skeleton_buffer_from_sources(
            instance_config_path,
            pool_path,
            first_constant,
        )
        if skeleton_buffer is None:
            return None
        return {
            "skeleton_buffer": skeleton_buffer,
            "instance_config_path": instance_config_path,
            "pool_path": pool_path,
            "first_constant": first_constant,
            "draw_index": str(
                submesh_json.get("SkeletonSourceDrawIndex", "") or ""
            ),
        }

    @classmethod
    def _efmi_cache_intact(
        cls,
        submesh_json: dict,
        json_path: str,
        unique_str: str,
        expected_dedup_enabled: bool | None = None,
    ) -> bool:
        """EFMI 缓存快路径完整性校验（版本 + schema + 映射覆盖 + 骨骼缓存文件）。

        与 ZZMI 的 _zzmi_cache_intact 同构：任何一项缺失都判定缓存不完整，
        整批重建——骨骼池复制失败 / 工作空间搬迁漏掉 ModImpRuntime / 旧算法
        缓存都不允许带着半成品 VGMap 永久幂等跳过。校验项：
        - VGMapAlgorithmVersion == 当前算法版本、VGMapDedupEnabled ==
          期望去重开关（省略时沿用全局 _DEDUP_ENABLED）；
        - VGCount/VGOffset 存在且非负；
        - VGMap 非空、键完整覆盖 0..VGCount-1 且槽位非负；全零矩阵骨骼
          不参与去重，但从 v15 起也必须保留独立稳定槽位；
        - BoneMatrixFileName 指向的 ModImpRuntime 文件存在且大小
          >= VGCount * 48 字节（每骨骼 4x3 float32）；
        - F1（t1 §4.5）：json 声明 Position/Blend.buf 时，EFMIVGMapSource-
          Fingerprint 必须存在且与当前 (mtime_ns, size) 一致（缺指纹键 =
          旧口径缓存 = 失效重建；源数据变更 = 失效整批重算）。
        """
        def _strict_int(value) -> int:
            if isinstance(value, bool):
                raise TypeError("bool 不是缓存整数")
            if isinstance(value, float) and not value.is_integer():
                raise ValueError("非整数浮点值")
            return int(value)

        try:
            cache_version = _strict_int(
                submesh_json.get("VGMapAlgorithmVersion", 0) or 0
            )
        except (TypeError, ValueError):
            cache_version = 0
        if cache_version != _VG_MAP_ALGORITHM_VERSION:
            return False
        cache_dedup_enabled = submesh_json.get("VGMapDedupEnabled")
        expected_dedup = (
            bool(_DEDUP_ENABLED)
            if expected_dedup_enabled is None
            else bool(expected_dedup_enabled)
        )
        if not isinstance(cache_dedup_enabled, bool) or cache_dedup_enabled != expected_dedup:
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

        cache_path = cls._runtime_cache_path(submesh_json, json_path, unique_str)
        if not os.path.isfile(cache_path):
            return False
        if not EFMIBoneMapBuilder.cache_file_size_ok(cache_path, vg_count):
            return False

        # F1 源数据指纹（t1 §4.5 / FC-5）：去重结果依赖 Position/Blend.buf
        # 内容；缓存必须携带写回时刻的 (mtime_ns, size) 指纹并与当前一致。
        # - 未声明 Position/Blend 的 json（无 FileName 的旧/合成形态）不适用，
        #   跳过（既有 schema 测试形态保持完整）；
        # - 声明了但指纹键缺失/不一致/文件也不存在 → 失效（旧口径/数据变更）。
        declared_buffers = EFMIBoneMapBuilder._declared_source_buffers(submesh_json)
        if declared_buffers:
            stored_fingerprint = submesh_json.get("EFMIVGMapSourceFingerprint")
            if not isinstance(stored_fingerprint, dict):
                return False
            submesh_dir = os.path.dirname(json_path)
            for file_name in declared_buffers:
                entry = stored_fingerprint.get(file_name)
                if not isinstance(entry, list) or len(entry) != 2:
                    return False
                try:
                    stored_mtime_ns = int(entry[0])
                    stored_size = int(entry[1])
                except (TypeError, ValueError):
                    return False
                path = os.path.join(submesh_dir, file_name)
                if stored_mtime_ns < 0 or stored_size < 0:
                    # 写缓存时源文件缺失 → 现在仍缺失才算一致
                    if os.path.isfile(path):
                        return False
                    continue
                try:
                    stat_result = os.stat(path)
                except OSError:
                    return False
                if (
                    int(stat_result.st_mtime_ns) != stored_mtime_ns
                    or int(stat_result.st_size) != stored_size
                ):
                    return False
        return True

    @staticmethod
    def _is_projection_skipped(payload: dict, projection_enabled: bool) -> bool:
        """判定 json 是否携带“跨 LOD 投影未匹配”裁决且与当前投影开关一致。

        只有标记、布局版本与投影开关三者同时匹配时才算有效裁决；任何一项
        不一致都视为需要重新评估（旧缓存/切换开关/半成品），按缓存失效处理。
        """
        if not isinstance(payload, dict) or payload.get("EFMILODProjectionSkipped") is not True:
            return False
        try:
            layout_version = int(payload.get("EFMILODLayoutVersion", 0) or 0)
        except (TypeError, ValueError):
            return False
        return (
            layout_version == _CROSS_LOD_LAYOUT_VERSION
            and bool(payload.get("EFMILODProjection", False)) == bool(projection_enabled)
        )

    @staticmethod
    def _is_projection_matched(payload: dict, projection_enabled: bool) -> bool:
        """判定 json 是否携带“跨 LOD 投影匹配成功”裁决且与当前投影开关一致。

        与 _is_projection_skipped 同构：标记、布局版本与投影开关三者同时匹配
        才算有效裁决。“匹配成功”只用于 CPU/无顶点组目标（无 VGMap、静态网格
        导入），导入侧据此放行非基准 LOD 目标。
        """
        if not isinstance(payload, dict) or payload.get("EFMILODProjectionMatched") is not True:
            return False
        try:
            layout_version = int(payload.get("EFMILODLayoutVersion", 0) or 0)
        except (TypeError, ValueError):
            return False
        return (
            layout_version == _CROSS_LOD_LAYOUT_VERSION
            and bool(payload.get("EFMILODProjection", False)) == bool(projection_enabled)
        )

    @classmethod
    def load_projection_skipped_targets(
        cls,
        workspace_root: str,
        unique_str_list: list[str],
    ) -> set[str]:
        """读取“跨 LOD 投影未匹配”目标集合（供导入侧过滤）。

        json 中 EFMILODProjectionSkipped=True 且布局版本/投影开关均一致时，
        该目标在分组投影模式下被裁决为几何匹配不成功，导入时必须排除。
        """
        skipped: set[str] = set()
        for unique_str in unique_str_list:
            json_path = cls._resolve_submesh_json_path(workspace_root, unique_str)
            if not json_path:
                continue
            try:
                payload = JsonUtils.LoadFromFile(json_path)
            except Exception:
                continue
            if cls._is_projection_skipped(payload, True):
                skipped.add(unique_str)
        return skipped

    @staticmethod
    def _has_valid_vgmap(payload: dict) -> bool:
        """轻量 VGMap 有效性（导入过滤用）：非空映射 + 正 VGCount + 非负 VGOffset。

        完整校验（缓存文件大小等）由 _efmi_cache_intact 负责；本方法只判断
        “json 携带了可行的 GPU 几何匹配成功证据”。
        """
        vg_map = payload.get("VGMap")
        if not isinstance(vg_map, dict) or not vg_map:
            return False
        try:
            vg_count = int(payload.get("VGCount", 0) or 0)
            vg_offset = int(payload.get("VGOffset", -1) or -1)
        except (TypeError, ValueError):
            return False
        return vg_count > 0 and vg_offset >= 0

    @classmethod
    def classify_projection_import_targets(
        cls,
        workspace_root: str,
        unique_str_list: list[str],
    ) -> dict[str, str]:
        """投影模式下逐目标导入裁决：'import' | 'skip' | 'fail_closed'。

        供导入侧在投影模式（efmi_lod_group_projection）下对所有 import_targets
        使用——不依赖合并预生成是否成功（裁决标记在 ensure 内部任何失败点
        之前已写入，失败路径同样必须过滤）：
        - 基准 LOD（reference_lod，动态推导：请求含 'LOD0' 则取之，否则按
          字典序取首个 LOD）或无 LOD 前缀：'import'（C5：基准 LOD 行为不变，
          C4 只约束非基准 LOD）；
        - 非基准 LOD：
          * json 携带有效 EFMILODProjectionSkipped（版本/开关三件套一致）→
            'skip'（C3：几何匹配不成功，明确排除）；
          * json 携带有效 EFMILODProjectionMatched（版本/开关三件套一致，
            CPU/无顶点组目标匹配成功）→ 'import'（C2：可导入、无 VGMap）；
          * 有可行 VGMap → 'import'（GPU 几何匹配成功，C5）；
          * 其余（缺状态/旧缓存/半成品/json 不可读）→ 'fail_closed'
            （C4：**默认排除**，绝不放行无明确成功结果的非基准 LOD 目标）。

        选择“白名单放行”而非旧“黑名单剔除”：旧缓存不会自动补标记，只有
        显式成功证据才能通过过滤。可直接调用本方法；需要在 classify 异常时
        自动降级 fail-closed 兜底，请使用 resolve_projection_import_decisions。
        """
        lods = sorted({cls._parse_lod_name(u) for u in unique_str_list} - {""})
        reference_lod = "LOD0" if "LOD0" in lods else (lods[0] if lods else "")
        decisions: dict[str, str] = {}
        for unique_str in unique_str_list:
            lod_name = cls._parse_lod_name(unique_str)
            if not lod_name or lod_name == reference_lod:
                decisions[unique_str] = "import"
                continue
            json_path = cls._resolve_submesh_json_path(workspace_root, unique_str)
            payload = None
            if json_path:
                try:
                    payload = JsonUtils.LoadFromFile(json_path)
                except Exception:
                    payload = None
            if not isinstance(payload, dict):
                decisions[unique_str] = "fail_closed"
                continue
            if cls._is_projection_skipped(payload, True):
                decisions[unique_str] = "skip"
            elif cls._is_projection_matched(payload, True):
                decisions[unique_str] = "import"
            elif cls._has_valid_vgmap(payload):
                decisions[unique_str] = "import"
            else:
                decisions[unique_str] = "fail_closed"
        return decisions

    @classmethod
    def fallback_projection_import_decisions(
        cls,
        unique_str_list: list[str],
    ) -> dict[str, str]:
        """classify 无法执行时的 fail-closed 兜底裁决（'import' | 'fail_closed'）。

        基准判定与 classify_projection_import_targets **完全同一口径**（动态
        reference_lod 推导，不硬编码 LOD0）：reference_lod = 请求中存在
        'LOD0' 则取之，否则按字典序取首个 LOD；无 LOD 前缀目标不受投影约束。
        LOD1-only 工作空间（请求中无 LOD0）→ 基准即 LOD1、全部放行，与主
        路径不分裂；LOD0 存在时非基准 LOD 目标一律 fail-closed（缺状态/
        裁决不可用 → 默认排除，绝不 fail-open）。
        """
        lods = sorted({cls._parse_lod_name(u) for u in unique_str_list} - {""})
        reference_lod = "LOD0" if "LOD0" in lods else (lods[0] if lods else "")
        decisions: dict[str, str] = {}
        for unique_str in unique_str_list:
            lod_name = cls._parse_lod_name(unique_str)
            if not lod_name or lod_name == reference_lod:
                decisions[unique_str] = "import"
            else:
                decisions[unique_str] = "fail_closed"
        return decisions

    @classmethod
    def resolve_projection_import_decisions(
        cls,
        workspace_root: str,
        unique_str_list: list[str],
    ) -> dict[str, str]:
        """投影模式下导入过滤裁决总入口（classify + fail-closed 兜底，不抛错）。

        正常情况委托 classify_projection_import_targets；classify 抛异常时降级
        到 fallback_projection_import_decisions（与 classify 同一动态
        reference_lod 口径，非基准 LOD 目标一律 fail-closed）——调用方无需
        自行 try/except，也绝不会因 classify 异常而跳过过滤（契约 C4：
        缺状态/裁决不可用不得放行）。
        """
        try:
            return cls.classify_projection_import_targets(workspace_root, unique_str_list)
        except Exception as e:
            print(f"[EFMI骨骼合并] 投影裁决分类失败，降级 fail-closed 兜底: {e}")
            return cls.fallback_projection_import_decisions(unique_str_list)

    @classmethod
    def load_lod_match_pairs(
        cls,
        workspace_root: str,
        import_keys: list[str],
    ) -> list[dict]:
        """读取跨 LOD 自动匹配节点所需的物体配对。

        每对来自**目标侧**（非基准 LOD）子网格 json 的 EFMILODCorrespondence 账本：
        非空即表示该部件与基准侧存在几何对应。基准侧 json 也写对应账本，但其中
        ``unique_str`` 指向目标侧；以 EFMILODReference 区分后只收集目标侧，保证
        每对只出现一次。

        单对返回：target_key（目标侧 import_key）/ reference_key（基准侧 import_key）/
        target_lod / reference_lod。这里的账本只负责确认“哪个实际导入物体和哪个
        实际导入物体匹配”，不能作为顶点组重命名表：导入器随后必须让自动生成的
        ``SSMTNode_VertexGroupMatch`` 读取 Blender 中两边的真实顶点组并重新匹配。
        因此本方法刻意不读取 ``VGMap``，也不会把 local 对应换算成全局组映射。
        """
        pairs: list[dict] = []
        seen: set[tuple[str, str]] = set()

        for unique_str in import_keys:
            lod_name = cls._parse_lod_name(unique_str)
            if not lod_name:
                continue
            json_path = cls._resolve_submesh_json_path(workspace_root, unique_str)
            if not json_path:
                continue
            try:
                payload = JsonUtils.LoadFromFile(json_path)
            except Exception:
                continue
            if not isinstance(payload, dict):
                continue
            reference_lod = str(payload.get("EFMILODReference", "") or "").strip()
            correspondence = payload.get("EFMILODCorrespondence")
            if not isinstance(correspondence, dict) or not correspondence:
                continue

            reference_keys: set[str] = set()
            for row in correspondence.values():
                if not isinstance(row, dict):
                    continue
                candidate = str(row.get("unique_str", "") or "")
                if candidate and cls._parse_lod_name(candidate) == reference_lod:
                    reference_keys.add(candidate)
            if not reference_keys:
                continue

            for reference_key in sorted(reference_keys, key=str.casefold):
                key = (str(unique_str), reference_key)
                if key in seen:
                    continue
                seen.add(key)
                pairs.append({
                    "target_key": str(unique_str),
                    "reference_key": reference_key,
                    "target_lod": lod_name,
                    "reference_lod": cls._parse_lod_name(reference_key),
                })
        pairs.sort(key=lambda item: (item["target_lod"], item["target_key"]))
        return pairs

    @classmethod
    def missing_merged_metadata_exist(
        cls, workspace_root: str, unique_str_list: list[str]
    ) -> bool:
        """检查 GPU 子网格的融合映射和日后重建所需的两份来源缓存。"""
        if not workspace_root or not unique_str_list:
            return False
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
            if cls._load_workspace_skeleton_source(payload, json_path, unique_str) is None:
                return True
        return False

    @classmethod
    def clear_vgmap_cache(cls, workspace_root: str) -> tuple[int, int]:
        """删除工作空间内所有子网格 json 的 VGMap/VGOffset/VGCount/SkeletonGroup 缓存键。

        用途：去重策略变更（或去重关闭）后，手动清掉缓存即可强制下次导入
        按当前策略重新生成；正常导入也会通过 VGMapAlgorithmVersion 自动
        使旧策略缓存失效。SkeletonGroup 是 ZZMI 分组版字段（EFMI json 没有，
        一并列出无副作用）。
        清理范围（F2，t1 §4.4/计划书 §415 承诺）：**含 VGMap 系列键 或 投影
        裁决标记（EFMILODProjectionSkipped/Matched）的 json**——被投影裁决
        「未匹配」的部件 json 没有 VGMap（写跳过标记前已 pop VGMap 五键），
        旧实现只清含 VGMap 的文件，导致清缓存后该部件仍被持续排除；现按
        「缓存/裁决键集合」判定，无 VGMap 的裁决 json 同样清除。
        ModImpRuntime/*-BoneMatrix.buf、*-InstanceConfig.buf 及其来源元数据不删：
        它们是原始骨骼池与 instance-config 的工作空间副本，与去重策略无关；
        即使 FrameAnalysis 已删除，重新生成也会复用这些文件。

        返回 (清理的子网格 json 数, 扫描的 json 文件总数)。
        """
        cleaned = 0
        scanned = 0
        if not workspace_root or not os.path.isdir(workspace_root):
            return cleaned, scanned
        for dirpath, dirnames, filenames in os.walk(workspace_root):
            # Config 目录是工作空间配置（FrameAnalysisPath.json / Tabs 等），
            # 与子网格缓存无关，直接跳过。
            dirnames[:] = [d for d in dirnames if d != "Config"]
            for filename in filenames:
                if not filename.endswith(".json"):
                    continue
                path = os.path.join(dirpath, filename)
                scanned += 1
                try:
                    payload = JsonUtils.LoadFromFile(path)
                except Exception:
                    continue
                if not isinstance(payload, dict):
                    continue
                # F2：清理判定 = 携带任一缓存/裁决键（含无 VGMap 的投影裁决
                # json——重复导入时该部件不会被旧的「未匹配」标记永久排除）。
                if not any(
                    key in payload
                    for key in (
                        "VGMap", "EFMILODProjectionSkipped", "EFMILODProjectionMatched",
                    )
                ):
                    continue
                for key in (
                    "VGMap", "VGOffset", "VGCount", "VGMapAlgorithmVersion",
                    "VGMapDedupEnabled", "SkeletonGroup", "EFMILODLayoutVersion",
                    "EFMILODReference", "EFMILODProjection", "EFMILODBaselineGroupCount", "EFMILODGroupCount",
                    "EFMILODActualGroupCount", "EFMILODMissingBaselineCount",
                    "EFMILODCorrespondence", "EFMILODProjectionSkipped",
                    "EFMILODProjectionMatched", "EFMIVGMapSourceFingerprint"
                ):
                    payload.pop(key, None)
                try:
                    JsonUtils.SaveToFile(filepath=path, json_dict=payload)
                    cleaned += 1
                except Exception as e:
                    print(f"[EFMI骨骼合并] 清理 VGMap 缓存失败 {path}: {e}")
        return cleaned, scanned
