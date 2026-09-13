# -*- coding: utf-8 -*-
"""EFMI 阴影 pass 单投射源门控判定（2026-09-13 实机定位）。

背景（抓帧实证，FrameAnalysis-2026-09-13-081358）：
同一逻辑部件若同时存在 LOD0/LOD1 两个独立 component，游戏会在阴影 pass 里把该部件
画多次（身体 = LOD1 x2 级联 + LOD0 x1）。EFMI 的骨骼矩阵是「按 component 分帧独立
导入」的，两套矩阵在运动时必然错开 → 同一张阴影图里出现两个姿态的同一部件 →
光照 pass 把错位的那份当成遮挡 = 身体上的自遮黑块（与地面影子同形；静止时两套
重合故不可见）。

修法（实机验证：黑块消失、地面影子保留）：同一逻辑部件只让 LOD1 那一份参与投影，
LOD0 入口在阴影 pass 内不再回画（handling = skip 仍在门控外，原版网格全 pass 压制）。

⚠ 配对键不能用「部件名/IP 哈希」：实机身体的 LOD0 与 LOD1 是**两个不同的 IB**
（d6128f13-14664-0 vs 2dca919f-7596-0），按裸名永远配不上（曾因此让门控永不触发）。
配对键必须来自**源物体**（同一 Blender 物体导出的各 LOD 版本），由发射侧传入。

本模块只放纯判定逻辑（无 bpy 依赖），便于单元测试；发射侧见 ui/universal/efmi.py。
"""
import re

# 阴影 pass 的角色 VS 过滤号：与 ShaderOverridevs1000（hash = f11c7e1dbf876a69）的
# filter_index 一致；颜色 pass 为 201/202/203，其余 pass 不受该门控影响。
EFMI_SHADOW_PASS_VS_FILTER_INDEX = 200

# 参与投影的 LOD 与需要被门控的 LOD（实机验证的组合：留 LOD1，门控 LOD0）
EFMI_SHADOW_KEEP_LOD = 1
EFMI_SHADOW_GATE_LOD = 0

_LOD_PREFIX_RE = re.compile(r"^LOD(\d+)\.")


def efmi_bare_part_key(unique_str):
    """去掉 LOD<n>. 前缀后的部件裸名（LOD0.x-y-z → x-y-z）。

    注意：裸名只用于日志/诊断，**不可**作为跨 LOD 配对键（不同 LOD 可以是不同 IB）。
    """
    return _LOD_PREFIX_RE.sub("", str(unique_str or ""))


def efmi_lod_index(unique_str):
    """LOD 序号；无 LOD 前缀时返回 None。"""
    match = _LOD_PREFIX_RE.match(str(unique_str or ""))
    return int(match.group(1)) if match else None


def efmi_shadow_gate_needed(
    part_lod,
    part_key,
    keep_lod_keys,
    keep_lod=EFMI_SHADOW_KEEP_LOD,
    gate_lod=EFMI_SHADOW_GATE_LOD,
):
    """该部件入口是否需要发射「阴影 pass 不重复投射」门控。

    参数
    ----
    part_lod : 本部件的 LOD 序号（无 LOD 前缀传 None）。
    part_key : 本部件的**逻辑部件键**（同一源物体各 LOD 共用同一个键，需可哈希）。
    keep_lod_keys : 参与投影那一档（默认 LOD1）全部部件的逻辑部件键集合。

    判定：本部件 LOD == gate_lod，且其逻辑部件键出现在 keep_lod_keys 中
    —— 即该逻辑部件在阴影 pass 里确实会被两套 component 各投一遍。
    没有对应 LOD 兄弟的部件返回 False：行为与修复前一致，无回归。
    """
    if part_lod != gate_lod:
        return False
    if part_key is None:
        return False
    return part_key in set(keep_lod_keys or ())


def efmi_shadow_gate_open_lines():
    """门控开启行（配套发射 ``endif`` 关闭）。"""
    return [
        "; [shadow-gate] 同部件 LOD%d 已在阴影 pass 投影，本入口不重复投射"
        % EFMI_SHADOW_KEEP_LOD,
        "if vs != %d" % EFMI_SHADOW_PASS_VS_FILTER_INDEX,
    ]
