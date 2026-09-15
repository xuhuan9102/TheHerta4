# -*- coding: utf-8 -*-
"""EFMI 深度/阴影 pass 门控（2026-09-15 起：rt_width 口径、全 IB 挂载）。

机制
----
深度/阴影类 pass 不绑颜色 RT（FrameAnalysis 里 ``OMSetRenderTargets(NumViews:0)``），
可见 pass 都有颜色输出；3DMigoto command list 内建变量 ``rt_width``（当前颜色 RT0
宽度）在无 RT pass 读为 0。门控行 = ``if rt_width != 0``：仅存在颜色输出目标的
pass 才回画模组网格。配合 EntryPoint 的 ``handling = skip``（原版绘制全 pass
压制），模组部件在深度/阴影 pass 完全不出现 → 自遮黑块/错位投影根除，角色整体
不产生投影。

**所有 EntryPoint 一律挂载**（2026-09-15 用户实机决策）：不做 LOD0/LOD1 配对
挑选，不注册任何 ShaderOverride 标签——零哈希、与角色/画质档无关。

历史（三次实机事故换来的教训，勿回退到哈希口径）
----------------------------------------------
1. 2026-09-13 实机定位：同一逻辑部件以 LOD0/LOD1 两个 component 各投一遍阴影，
   两套「按 component 分帧导入」的矩阵在运动时错开 → 身体自遮黑块。首版修法 =
   LOD0 入口在阴影 pass 不回画，用 ``if ps != <标签>`` 判 pass，标签靠给阴影
   PS 打 ShaderOverride（filter_index）。
2. v4.4.45 事故：阶段标签曾借用 RabbitFX 的 1718.2——它改写的正是角色可见
   G-buffer shader，``if ps != 1718.2`` 在可见 pass 也不成立 → 门控关闭 +
   handling=skip 压掉原版 → 可见外壳整块消失、只剩背面。随后改回私有号段
   99001 修复。
3. 2026-09-15 艾尔黛拉事故：044b75548e7c9fd7 是引擎级共享着色器（多角色、
   多份抓帧复用，甚至服务过别的 LOD），按抓帧推导的哈希集按角色漂移、无法
   证明运行时只用于阴影 pass，实机纳入即出问题。同日实机验证
   ``rt_width != 0`` 口径有效 → 全面切换并推广到全部 IB。

哈希时代的抓帧推导 / LOD 配对键 / ShaderOverride 注册段代码已全部移除
（需要考古时查 git 历史：v4.4.46 及更早）。

本模块只放行构造（无 bpy 依赖，可脱离 Blender 单测）；发射侧见 ui/universal/efmi.py。
"""

# 门控条件字面量（唯一来源）。rt_width = 3DMigoto command list 内建变量：
# 当前绑定颜色 RT0 的宽度；深度/阴影 pass 无颜色 RT → 读为 0（2026-09-15 实机验证）。
EFMI_SHADOW_GATE_CONDITION = "rt_width != 0"


def efmi_shadow_gate_open_lines():
    """门控开启行（发射侧配套追加 ``endif`` 关闭）。

    条件用 3DMigoto 内建的 ``rt_width``（见 EFMI_SHADOW_GATE_CONDITION）：
    无颜色 RT 的深度/阴影 pass 读为 0 → 门控关闭、入口不回画；可见 pass 恒有
    颜色 RT → 门控开启。不依赖任何着色器哈希，对所有角色/画质档一致。

    本函数**只返回可执行行**，不往生成的 ini 里写开发者注释——机制说明留在本模块
    文档串里（生成物是给 3DMigoto 读的配置表，不是给人读的开发笔记）。
    """
    return [
        "if " + EFMI_SHADOW_GATE_CONDITION,
    ]
