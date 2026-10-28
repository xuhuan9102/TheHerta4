# HIMI（崩坏3 / HI3）骨骼合并计划书

> 状态：**已实现并交付（数据层 + 导入侧接线 + 导出/运行时合并骨架资源生成 + 独立验证 + 审查 + 集成收口全部完成）**；实机验证项未做（见 §11） · 最后更新：2026-09-23（T10 定稿）
> 目标：参照 ZZMI 合并骨架（`common/zzmi_skeleton.py` / `ui/universal/zzmi.py` / `Toolset/zzmi_merged_*.hlsl`），为 HIMI（崩坏3 / HI3，游戏目录 `K:\SSMT-Package-master\3Dmigoto\HI3`）实现骨骼合并。
> **交付物与复现**：新增 `common/himi_skeleton.py`、`common/himi_merged_contract.py`、`common/himi_merged_runtime.py`、`ui/universal/himi.py`（接线）、`Toolset/himi_merged_skeleton_attach.hlsl`（声明参考）、4 个测试文件、`tools/himi_merged_probe.py`（探针）；详见 `reports/himi-merge/10-integration.md`。
> **对用户原始问题的直接回答**：
> 1. **能不能做？——能，而且比 ZZMI 简单。** HIMI 的 deform pass 结构在五帧 39/39 上与 ZZZ 同构（pointlist `Draw(VertexCount)` + `SOSetTargets` + `vs-t0` = 48 B/骨骼 palette + `vb0`=Position/`vb1`=Blend），三条 join 键（`CategoryHash.Position` / `CategoryHash.Blend` / `VertexLimitVB`）在工作空间 json 里全部实测命中，34/34 个组件都能 join 到自己的 deform pass（`04-probe-results.md` §6.1）。
> 2. **有什么不同？——三处结构差异决定了「运行时合并骨架」这一半必须重写，数据层这一半可以照搬。** ① HIMI **没有 CS/Dispatch**（五帧 0 命中，`01-dump-facts.md` §6），ZZMI 的 attach/skin CS 通道没有挂载点；② HIMI 的 deform pass **不绑任何常量缓冲**（全 dump 无 `*SetConstantBuffers1`，`01` §1.1 D5 / `02` §5.1），ZZMI 的「palette ↔ cb1 逐物体 1:1 配对 + 按对象变换分组」前提不存在；③ HIMI 的 **SO 目标就是该部件的渲染 VB**（就地写入，`02` §5.2 #2），ZZMI 的「SO → 另开一块重定向 buffer」模型不适用。
> 3. **有什么共通？——数据链路五段（dump 反查 / 工作空间落盘 / 导入消费 / 导出生成 / 运行时资源）的前四段几乎可以整段移植，第五段必须重写。** 共用矩阵见 §3.1，差异矩阵见 §3.2。
> 4. **本方案相对 ZZMI 的最大简化**：HIMI 的合并骨架是**静态**的（骨架取自抓帧时的 bind 姿态 palette，逐帧不变），因此**不需要任何逐帧 attach / CS / 双缓冲 / 出现次槽位 / 姿态指纹**——ZZMI 修复链里 90% 的复杂度（v1..v9）在 HIMI 上不存在。运行时唯一动作是「在 deform pass 上把 `vs-t0` 重绑到一份静态骨架资源」。
>
> **⚠ 更正（2026-09-25 / t69 回写）—— 上面第 4 条的「静态骨架」前提已被实测推翻（原文按纪律保留在上，不删除）**：
> - **实测（每帧内容都在变）**：同一 3DMigoto 资源哈希 `vs-t0=23de2d6a`（1 槽 palette）出现在 **14 个**抓帧里，
>   内容 **14/14 各不相同**；同一批里 `vs-t0=ee10e893`（257 槽 palette）出现在 10 个抓帧、内容 **10/10 各不相同**；
>   **对照（静态资源）**`vb0=1a6d79fc` 出现在 10 个抓帧、内容 **10/10 完全相同**。
>   （原始样本：`reports/himi-merge/27-static-vs-perframe.md` §1 = 6/6 不同；`28-perframe-attach.md` §5.2 的 CF-1 = 9/9 不同；
>   本轮 t69 独立复测 = 14/14，样本随抓帧增加而增加，方向不变。）
> - **合并骨架在字节上就是当帧五族 palette 的拼接**（`27-static-vs-perframe.md` §1.3：5/5 区间逐字节相同、343/343 槽覆盖）
>   ⇒ 「逐帧不变」不成立 ⇒ **「不需要任何逐帧 attach / CS」不成立**。
> - **现状机制（正确形状）**：**逐帧捕获（deform 段 `copy vs-t0`）→ attach CS 把本族 palette 写进骨架槽位**；
>   骨架资源段**不得再有 `filename`**。落地见 `reports/himi-merge/28-perframe-attach.md`
>   （`Toolset/himi_merged_skeleton_attach.hlsl` 已是可挂载的 attach CS）+ `reports/himi-merge/29-t52-handoff.md`。
> - **表述口径更正**：「本方案比 ZZMI 简单」只在**数据链路前四段**成立；在**骨架填充**这一点上**已不比 ZZMI 简单**。
> - **本节第 2 条仍成立**（HIMI **游戏侧**无 CS/Dispatch 是五帧实测）；但由此推出「mod 侧也没有可挂载的 CS 通道」**不成立**
>   （mod 自己的 `[CustomShader]` 段不在游戏 dump 里出现）—— 这正是上面那条错误简化的来源。
> - 另三处同类表述的更正注记：**§3.2 D10**、**§3.3 N08**、**§4.5 标题与表体**；台账见 **§13.12**。
>
> 依据（全部为可复现实测）：
> - `reports/himi-merge/01-dump-facts.md`（T1，五帧 dump 事实表，数据口径最终依据）
> - `reports/himi-merge/02-zzmi-anatomy.md`（T2，ZZMI 全链路解剖；**已知 4 处错误，以 01/04 为准，更正记录见 `reports/himi-merge/05-decisions.md` §1**）
> - `reports/himi-merge/03-himi-integration-points.md`（T3，HIMI 接入点审计，逐条带 `文件:行号`）
> - `reports/himi-merge/04-probe-results.md` + `04-probe-results.json`（T4，跨五角色合并收益与不变量探针）
> - `reports/himi-merge/08-verification.md`（T8，独立验证）、`09-review.md`（T9，审查）、`10-integration.md`（T10，交付清单）
> - 复现入口：`.venv\Scripts\python.exe .dbg\himi_dump_facts.py`（五帧全量 + `FINAL INVARIANTS`）、`.venv\Scripts\python.exe tools\himi_merged_probe.py --self-check`（**23/23 PASS**）、`.venv\Scripts\python.exe tools\himi_merged_probe.py --json reports\himi-merge\04-probe-results.json`、`.venv\Scripts\python.exe -m pytest tests/ -q --ignore=tests/test_swap_panel_button_icon.py`（**2674 passed**）

---

## 1. 背景与结论摘要

### 1.1 现状（T3 实测）

| 项 | 现状 | 位置 |
|---|---|---|
| HIMI 导出器 | `class ExportHIMI(ExportUnity): pass`（全文 5 行，零改写） | `ui/universal/himi.py:1-5` |
| 导入侧顶点组/骨架处理 | **完全没有**。唯一拦点是 json `VGCount=0`（46/46 份 json 实测全 0） | `ui/ui_func_import_ssmt.py:652-714`、`common/ssmt_import_helper.py:47-56`、`common/mesh_create_helper.py:496-531` |
| 导出侧 GPU 产物 | 只有 `VertexLimitRaise`（SO 容量声明）；**无** palette / deform 重放 / vs-t0 / 自定义 shader | `ui/universal/unity.py:127-141` |
| 骨骼合并复选框可见性 | `import_merged_vgmap` 只在 `(WWMI, ZZMI, EFMI)` 下渲染 | `ui/ui_panel_basic.py:559-563` |
| 反查源 | 五个工作空间各有 `Config/FrameAnalysisPath.json`，各自指向存在的 dump 目录 | `01-dump-facts.md` §0 |

### 1.2 本次目标

1. HIMI 复刻 ZZMI 的**数据层模式**：从工作空间反查 HI3 FrameAnalysis dump → 取 deform pass 的 palette 与 BLENDINDICES → 跨组件 bitwise 去重 → 写回工作空间 json + 落盘合并骨架二进制。
2. **导入、导出都实现合并骨骼，且就是 EFMI/ZZMI 那种「统一顶点组」**：同一角色的全部组件共用一套全局顶点组编号（全局骨骼 id），**组件 A 可以引用组件 B 的骨骼**。
3. **直接复用复选框** `import_merged_vgmap`（`common/global_properties.py:202-206`，默认 True，不新增 UI、不改默认值）：勾选 = 导入全局顶点组、导出走合并骨架；不勾选 = 两侧都维持现状。

### 1.3 非目标（本期不做）

- **不做五角色共用一套骨架**：跨角色逐位重合 = 0，五角色并集 1405 > 单套上限 374（`04` §4/§5.1）。**必须按角色分套**。
- **不做跨角色合并、不做多 LOD 骨架合并**（HIMI 工作空间当前只有 LOD0 落盘；`01` §8.1 表）。
- **不做合并几何重定向（RedirectSO）**：HIMI 的 SO 目标就是渲染 VB（`02` §5.2 #2），ZZMI 那套 `_build_merged_mesh_redirect_plan` / carrier/target 拆分 / base_vertex 自检在 HIMI 上没有物理对应物。
  ⚠️ **更正（2026-09-23）**：本条**只**覆盖重定向那一半。**「3 顶点站位（stub）」不在非目标里——它是必须实现的能力**（用户实测发现 HIMI 缺这条 ⇒ 跨 IB join 后原版 draw 重影）。原 D09 把两件事绑在一起否决是错的，理由与逐条裁定见 **§7.7**。
- **不做 CS 形变路径**：HIMI 五帧零 CS/Dispatch（`01` §6），没有挂载点。
- **不做通道骨判定**（`common/zzmi_channel.py` 全套）：它依赖「同实例跨部件共享同一根骨」的 ZZZ 实测事实，HIMI 侧未取证；字段形态（`ChannelPlan*`）保留不写。
- **不做多实例身份对齐**：HIMI 五帧的 deform 段是非实例化的 `Draw(VertexCount, StartVertexLocation:0)`（无 Instance 字段），且每帧每个组件只出现一次 deform pass（`01` §3.1–3.5：39 个 pass 对 34 个组件）。若未来出现同帧多实例，按 §10.1 风险 R4 处理。
- **不改 SSMT4 提取端**，不改 HIMI 现有导入导出的默认行为（复选框关闭 = 与今天逐字节一致）。

---

## 2. 实证基础（HIMI 侧冻结事实）

> 本节只写已在 dump / 工作空间上复现的观测。**推断一律显式标注「推断」，未取证一律标注「待验证」。**

### 2.1 管线结构（五帧 39/39 成立）

```text
Map(调色板 scratch) → Unmap
  → SOSetTargets(SO 目标, 两笔：pOffsets …E478 / …E468)
  → VSSetShaderResources(StartSlot:0, vs-t0 = 调色板, Buffer/.buf)
  → IASetVertexBuffers(StartSlot:0, NumBuffers:2: vb0=CategoryHash.Position, vb1=CategoryHash.Blend)
  → Draw(VertexCount:N, StartVertexLocation:0)        # Topology:1 = pointlist
随后若干 DrawIndexed：vb0 = 该 SO 目标, vb1 = CategoryHash.Texcoord, IB = 组件 DrawIB  # Topology:4
```

来源：`01-dump-facts.md` §9.1（原文行号见 §8.4 链路样例）。deform VS = `10905ed856e6b621` 39/39（`01` §3.6）。

### 2.2 五帧计数（`01` §2）

| stamp | 角色 | draw id 范围 | deform pass | render DrawIndexed | 其它块（无 draw） |
|---|---|---|---|---|---|
| 133530 | 镇×偃月叩晓 | 000001–000152 | **8** | 143 | 1（000152） |
| 134442 | 愈生佑翎 | 000001–000166 | **8** | 157 | 1（000166） |
| 135117 | 窈窕谍影 | 000001–000155 | **7** | 147 | 1（000155） |
| 135739 | 咚！炽愿吉星 | 000001–000133 | **6** | 126 | 1（000133） |
| 140134 | 嗨♪爱愿妖精♥ | 000001–000170 | **10** | 159 | 1（000170） |
| **合计** | | | **39** | **732** | 5 |

deform 块在每帧都是**连续 draw id 段**（133530 为 000033–000040、134442 为 000001–000008、135117 为 000035–000041、135739 为 000035–000040、140134 为 000035–000044）。

### 2.3 组件与 join（`04` §1/§6）

| 角色 | dump | 组件数（json 数） | deform pass | 已 join / 未 join | 组件 join 率 |
|---|---|---|---|---|---|
| 咚！炽愿吉星 | 135739 | 3（4） | 6 | 2 / 4 | 3/3 |
| 嗨♪爱愿妖精♥ | 140134 | 12（18） | 10 | 5 / 5 | 12/12 |
| 愈生佑翎 | 134442 | 6（8） | 8 | 4 / 4 | 6/6 |
| 窈窕谍影 | 135117 | 7（9） | 7 | 3 / 4 | 7/7 |
| 镇×偃月叩晓 | 133530 | 6（7） | 8 | 4 / 4 | 6/6 |
| **合计** | | **34（46）** | **39** | **18 / 21** | **34/34** |

三条独立 join 键，全部双向命中（`01` §3.6、`04` §0.5 新发现、`04` §6.2）：

| 键 | 语义 | 实测 |
|---|---|---|
| `CategoryHash.Position` | = deform pass `vb0` 资源 hash（路径 A） | OWN pass 全中；`vb0` 是 1→1（`01` §3.6） |
| `CategoryHash.Blend` | = deform pass `vb1` 资源 hash（**HIMI 新增可用，ZZMI 未用**） | OWN pass 全中；**1→多，不能单独用作精确键**（`01` §3.6） |
| `VertexLimitVB` | = deform pass `SOSetTargets` 目标 hash（路径 B，同时是渲染 pass 的 `vb0`） | 34/34 一致（`04` §0.5） |

### 2.4 调色板是「骨骼池」，不是紧凑骨骼表（`04` §1.2 / T5 复算）

`vs-t0` 是**按角色/deform pass 共享的骨骼池**：同一 deform pass 的多个组件共用同一份 palette buffer；不同 deform pass 之间该 buffer 会被重写（ring scratch）。逐条不变量（口径 = 34 个组件，字段取自 `04-probe-results.json` 的 `roles[*].components[*]`）：

| # | 命题 | 实测 | 结论 |
|---|---|---|---|
| 1 | `palette_bones >= index_max_used + 1`（不越界） | **34/34** | ✅ **恒真**（T6 必须用它） |
| 2 | 引用槽集合长度 == `distinct_index_count` | **34/34** | ✅ **恒真** |
| 3 | `palette_bones == index_max_used + 1` | **32/34** | ❌ 不是恒真 |
| 4 | 引用槽集合长度 == `index_max_used + 1`（即引用 0..max 连续前缀、无空洞） | **29/34** | ❌ 不是恒真 |
| 5 | 池内**最大被引用索引** == 池大小 − 1（`pool_exact_fit`） | **33/34**（按池口径 16/18） | ❌ 不是恒真 |

- #3 的反例（**池容量偏大、尾部残留**）：`557a7477-3966-0`（池 156 / `index_max_used` 94）、`9430996d-1656-0`（池 156 / 136），均在 133530。
- #4 的反例（**中间空洞**）：`2c1f2636-*` 三兄弟各引用 148/156 槽（8 个中间空洞）、`557a7477-3966-0` 只引用 8 槽（87 个空洞）、`9430996d-1656-0` 引用 46 槽（91 个空洞）。
- #5 的反例：`557a7477-3966-0`（池 156 / 最大被引用索引 94）。
- **这些未引用的槽是真实骨骼，不是 padding**：镇×偃月叩晓的 282 个未引用槽 **282/282** 都满足「3×3 = 同一缩放 × 正交矩阵」（`04` §1.2 末、`role_pool_only_all_valid = true` 5/5 角色）。
- ⚠️ 口径差异说明：`04` §0.5 C1 / §1.2 正文写的「31/34」与 §8 的「31/34」在 JSON 里**复算不出**（可复算的是 32/34 或 33/34，取决于口径）。**本计划书以 JSON 字段复算值为准**（上表 #3/#4/#5），差异记录见 `05-decisions.md` §2.2。这个差异**不影响任何实现动作**（正确做法都只依赖 #1/#2）。
- **对合并的直接后果**：全局骨架必须**只对被 `BLENDINDICES` 引用的槽**做 bitwise 去重。若按「palette 文件大小」累加，镇×偃月叩晓会被高估 876 − 594 = **282 根**（`04` §1.2）。

### 2.5 palette 编码（`01` §4.2 / `04` §2）

- **48 B / 12 float / 骨骼**，布局 = 3×4 仿射矩阵按行主序，每行 `(m0, m1, m2, t)`。16-float（64 B）解释**被结构性否决**：15/34 个 palette 字节数不被 64 整除（`04` §2.1）。
- **3×3 = 正交矩阵 × 同一个均匀标量 s，且 `s = cbrt(|det|)`：39/39 无例外**（`01` §4.2）。实测 s：**1.6**（133530 / 135117 / 135739 / 140134）、**1.0 与 0.93**（134442）。
- ⇒ **ZZZ 的「旋转行模长 == 1.0、无缩放」不能外推到 HIMI**；矩阵身份去重按 **bitwise 仍然安全**，但任何「行模长 ≈ 1」的校验与 ZZMI 的 **0.05 m 刚性质心门控阈值**都必须改成 scale-aware 口径（§10.2）。

### 2.6 合并收益与上限（`04` §3/§4/§5）

| 角色 | 池大小之和 | 引用槽数之和 | **全局唯一骨骼** | 共享槽 | 池内未引用（已排除） |
|---|---|---|---|---|---|
| 咚！炽愿吉星 | 511 | 511 | **276** | 235 | 0 |
| 嗨♪爱愿妖精♥ | 1166 | 1166 | **343** | 253 | 0 |
| 愈生佑翎 | 819 | 819 | **315** | 252 | 0 |
| 窈窕谍影 | 891 | 891 | **219** | 168 | 0 |
| 镇×偃月叩晓 | 876 | **594** | **252** | 149 | **282** |

- 跨角色逐位重合 **0**；五角色并集 **1405** = 各角色唯一骨骼之和（`04` §4）。
- **单角色最大 343 ≤ 374（ZZMI 参照上限）**，余量仅 **31**；**五角色 1405 > 374 ⇒ 必须按角色分套**（`04` §5.1）。
- **索引不需要升宽**：34/34 组件 `BLENDWEIGHTS = R32G32B32A32_FLOAT`、`BLENDINDICES = R32G32B32A32_SINT`、每顶点 ≤ 4 权重、索引全非负、max = 251；无 BI4 / BW8_BI8 变体。8 位不够（343 > 255）、16 位够、32 位够（`04` §5.2）。
- 7 个 IB 家族被拆成多子网格（5 条独立判据，`04` §3.1）；同一 IB 的多 bare **共享同一 SO 输出**，分片区间首尾相接（`42912+13521=56433`、`56433+13968=70401`、`70401+1986=72387`）。
- **唯一 SO 输出行数**（= 合并后 `override_vertex_count` 的声明口径）：25787 / 30359 / 27792 / 28187 / 28839；按 bare 累加会翻倍（`04` §6.3）。

### 2.7 未 join 的 21 个 deform pass 的确定语义（T1↔T4 裁决，详见 `05-decisions.md` §3）

**口径：以 `01-dump-facts.md` §8 的 R1/R2 为准。**

- **R1（主判据）**：deform pass `P` 属于角色 `R`，当且仅当 **A**（`P.vb0` ∈ R 的 LOD0 组件 `CategoryHash.Position`）或 **B**（`P.SO` ∈ R 的 LOD0 组件 `VertexLimitVB`）或 **C**（`P.SO` 被某 render draw 绑为 `vb0`，**且该 render draw 的 `IASetIndexBuffer` ∈ R 的 LOD0 组件 `DrawIB`**——路径 C 必须先做 IB 门控）。
- **R2（反判据）**：`LOD0/DrawIB-Component.json` 与 `LOD0/ComponentName_DrawCallIndexList.json` 是**帧级候选表，严格宽于已落盘集合**，**不能**用来判归属。
- 实测：五帧 39 个 pass 中 **只有 140134/`000035` 是真外来对象**（DrawIB `b840f29b` 不在任何角色的候选表里，257 根骨骼，1 个 render 消费者）；**其余 20 个未 join 的 pass** 的 render 消费者 DrawIB **都在本角色 `DrawIB-Component.json` 里**（`DIB-listed-only` 档）。
- ⇒ **正确表述 = 「不属于本次落盘的合并目标集合」**。**本计划书全文不使用「外来角色」措辞**（`04` §6.2/§8 的对应表述按本条更正，见 `05-decisions.md` §3）。
- T4 的 7 条判据（`04` §6.2 a–g）**全部保留为「拒绝理由」的分类器**：未 join 的 pass 与已 join 的 pass 在 blend 内容 hash / palette 内容文件 / SO 目标 hash 上零交集，且不命中任何其它角色的工作空间。
- **两种解释下实现动作相同：一律拒绝（不纳入合并骨架）**。

### 2.8 T5 设计验证：deform pass 的「家族独占」门控（本次新测，只读）

> 命令（本次执行）：`.venv\Scripts\python.exe .dbg\_t5_gate_check.py`（临时只读脚本，五帧全量；K: 盘零写入）。
> **注意**：该脚本未随交付保留（见 §11.1）；复现方式与判定逻辑见下，T6 应把它并进 `tools/himi_merged_probe.py` 并纳入 `--self-check`。

对五帧逐 pass 统计「该 pass 的 SO 目标被哪些 render draw 消费、这些 draw 的 IB 是什么」：

| 命题 | 实测 |
|---|---|
| 每个 deform pass 的 SO 目标**只被一个 IB 家族**的 render draw 消费 | **39/39**（`passes_so_multi_family = 0`） |
| 每个工作空间 IB 家族的组件**共享同一个 SO 目标**（家族内 SO 唯一） | **18/18 家族**（复算：2+5+4+3+4；`04` 的 `shared_so_output` 只列「多组件家族」共 5 个，`families_without_so = 0`） |
| 「deform pass 的 IB 家族全部组件都在合并目标集合里」这条门控可放行的 pass | **18/39**（= 已 join 的 pass 数） |

逐帧（节选，`consumers` = 绑定该 SO 为 `vb0` 的 render draw 数）：

| stamp | deform | SO | consumers | consumer IB | 家族组件数 | 门控放行 |
|---|---|---|---|---|---|---|
| 133530 | 000033 | `7cb2e09c` | 15 | `2c1f2636` | 3 | ✅ |
| 133530 | 000034 | `6b6461d0` | 5 | `557a7477` | 1 | ✅ |
| 133530 | 000036 | `c31832cb` | 6 | `cafe86c8` | 0（未落盘） | ❌ |
| 135117 | 000041 | `34565909` | 26 | `7c0ec5d3` | 5 | ✅ |
| 140134 | 000035 | `d2cc3481` | 1 | `b840f29b` | 0（真外来对象） | ❌ |

**这条门控是本期设计的硬前提**（§7.4 G1）：它把「按 SO hash 无门控 join 会跨组出错」的风险（T1 §8.4）变成了一个可判定的、在五帧上 100% 成立的判据。

### 2.9 关键结构事实（冻结，供实现直接使用）

| # | 事实 | 来源 |
|---|---|---|
| F1 | deform VS 全五帧同一个：`10905ed856e6b621`；render VS 与它**集合完全不相交** | `01` §3.6/§7 |
| F2 | 逐 draw 的 VS **必须取 dump 文件名 `-vs=` 后缀**（771/771 无歧义）；取「块内第一条 `VSSetShader`」会把 133530/`000041` 误记成 `96212453d158ae82`（真值 `55da5eab238de808`） | `01` §7 |
| F3 | 取调色板只能按 `(draw id, vs-t0, Buffer, .buf)` → `deduped/<content>.buf`；**绝不能按资源 hash**（`f6dab4b5`/`23de2d6a` 是**描述级身份 hash**，同 hash 可对应不同内容：`23de2d6a` 被 19 个 pass 跨五帧复用、7 种内容） | `01` §5 |
| F4 | deform 块的 `vs-t0` 恒为 Buffer/.buf（39/39）；render 块的 `vs-t0` 恒为 Texture2D/.dds（732/732）；13 167 个 `.txt` sidecar 全部丢弃 | `01` §1.1 D3/D4 |
| F5 | 每 pass 有**两笔** `SOSetTargets`（`pOffsets` `…E478`/`…E468`），只有后者带资源 hash 续行；**成因未做逆向确认**（`01` §11.1），但「带 hash 续行的那笔 = 该 pass 的 SO 目标」34/34 与工作空间 `VertexLimitVB` 一致 | `01` §3.6、`04` §0.5 |
| F6 | HIMI **零 CS/Dispatch**（含 `DispatchIndirect`/`CSSet*`/`ClearUnorderedAccessView*`/`CopyStructureCount`） | `01` §6 |
| F7 | HIMI **零 `*SetConstantBuffers1`**，`first_constant` 恒 0；`vs-cb1` 的 `data[0:16]` **不是**对象变换（105/151 条连 sanity 都不过，46 条读出来是单位矩阵） | `02` §5.1 |
| F8 | `SOSetTargets` 目标**就是该部件的渲染 VB**（渲染 draw `vb0` = 同一个 hash；组件 json `VertexLimitVB` 就是它） | `02` §5.2 #2、`01` §8.4 |
| F9 | 工作空间 `Position.buf` **逐字节等于** dump 里该 deform pass 的 `vb0` dump（34/34）⇒ 工作空间几何 = deform 输入空间（1.0×） | `04` §7 D7 |
| F10 | `IndexOffset/IndexCount/VertexOffset/VertexCount` 46/46 全为 0 ⇒ `match_first_index` 恒 0 | `04` §7 补充、`03` §5.2 |
| **F10 更正**<br>（2026-09-26 / t23） | ⚠ **原文后半句「⇒ `match_first_index` 恒 0」不成立（已更正，原文保留在上）**。**前半句是真的**：那四个 JSON 字段**确实 46/46 全为 0**（原始 JSON 复核：同一文件里 `VertexOffset/VertexCount/IndexOffset/IndexCount` 四项皆 0，**而文件名第 3 段是 `55380`**）。**但导出侧不读它们** —— 读的是**物体名第 3 段**：`common/object_prefix_helper.py:286-299` 的 `parse_prefix_parts()`，其中 **`:295-297` 把 `parts[2]` 当 `first_index`**；再经 `common/draw_call_model.py:40` → `common/submesh_model.py:86` → `ui/universal/zzmi.py:5849,5853` 写进 ini。⇒ **「⇒ 恒 0」是无代码依据的推断**；产物 ini 的 `match_first_index` 实际**非零且逐子网格不同**：**`60639 / 54936 / 55380 / 47199 / 1716 / 9372 / 4656`**（另有 5 段为 0）。**12/12 段与两帧实测 draw 精确吻合**（140134 覆盖 46/46、泄漏 0；212119 接管 68/68、0 笔对不上任何段） | `64-matchfirstindex-forensics.md` §1.1/§1.4/§1.5、§2、§4、§5；`48-forensics-artifacts.md` §4.4、§7.3；**更正详情见 §13.14** |
| F11 | `analyse_options: 0140073d`（N 卡位掩码）五帧一致；抓帧时 `HI3\Mods` 已挂载，但 `[CommandListSkin]` 只做 `checktextureoverride` + `x140 = 0`，**不改绑定/不换 buffer/不替换 shader** ⇒ deform 段取证有效；**必须排除** `vs-t120`/`ps-t120` 与 IniParams 数值 | `01` §0、`02` §6 |

---

## 3. 共通 / 差异矩阵

### 3.1 共通矩阵 —— 可直接复用（原样或改常量）

| # | 组件 | ZZMI 位置 | HIMI 复用方式 | 来源 |
|---|---|---|---|---|
| C01 | log 解析骨架（draw 前缀 / 资源描述行 / pending 状态机 / dump 逻辑名→deduped 映射） | `common/zzmi_skeleton.py:86-297` | 直接复用。HI3 的 dump 行多一层绝对路径，但 `os.path.basename` 归一后形态一致 | `01` §1.1 D4 |
| C02 | `IASetIndexBuffer` 跨 draw 继承（`current_ib_hash`） | `zzmi_skeleton.py:164-166,217-221,256-266` | 直接复用（HIMI 渲染 draw 大量不重发 IB 绑定） | `01` §8.4 |
| C03 | `Draw` / `DrawIndexed` / `DrawIndexedInstanced` 三形态解析 | `zzmi_skeleton.py:100-107,255-280` | 直接复用（HIMI 只有前两种） | `01` §2 |
| C04 | `_deduped_candidates` / `get_deduped_path` 的「记录路径 → 当前 dump `deduped/`」回退 | `zzmi_skeleton.py:328-352,391-409` | 直接复用（dump 搬走后同样需要） | `01` §1.2 |
| C05 | `get_deform_passes` 的识别（pointlist `Draw` + SO + `vs-t0` + `vb0`） | `zzmi_skeleton.py:369-389` | 直接复用；HIMI 需**追加**结构约束（见 D02） | `01` §1.1、`04` §7 D3 |
| C06 | `ZZMIDeformResolver` 的 A/B/C 三 join + 路径 C 的 **IB 门控** | `zzmi_skeleton.py:412-469` | 直接复用（HIMI 三条键全部命中；路径 C 的 IB 门控必须保留） | `01` §8.1 R1 |
| C07 | `build_vg_maps` 的 bitwise 去重 / 同部件不去重 / canonical 槽位 / 死槽语义 | `zzmi_skeleton.py:601-710` | 直接复用（与游戏无关）；**HIMI 必须只喂「被引用的槽」**（见 D03） | `04` §3 |
| C08 | `dedup_excluded` 排除去重语义（`VGMapDedupExcluded`） | `zzmi_skeleton.py:674-683` | 直接复用 | `02` §1.1 |
| C09 | 缓存 schema 门 `_zzmi_cache_intact`（除 cb1 两项） | `zzmi_skeleton.py:1146-1258` | 形态直接复用；HIMI 版本号另起（§4.1） | `02` §1.2 |
| C10 | `FrameAnalysisLogSignature` + `_frame_analysis_log_signature` | `zzmi_skeleton.py:1260-1267,2135-2136` | 直接复用（HIMI log 同构） | `01` §0 |
| C11 | `_configured_frame_analysis_paths` / `resolve_frame_analysis_dir`（读 `Config/FrameAnalysisPath.json`，失效不偷换帧） | `zzmi_skeleton.py:722-758` | 直接复用（五个工作空间都有该文件） | `03` §3.5、`01` §0 |
| C12 | `_runtime_cache_path`（纯文件名、拒路径穿越、`ModImpRuntime/`） | `zzmi_skeleton.py:850-874` | 直接复用 | `02` §7.1 |
| C13 | `_atomic_publish_skeleton_transaction`（缓存 + json 同一事务） | `common/efmi_skeleton.py:4011-4044` | 直接复用 | `02` §7.1 |
| C14 | `missing_merged_metadata_exist` 的**自愈判据形状**（缺 VGMap 家族 或 快路径不完整） | `zzmi_skeleton.py:1305-1340` | 直接复用形状，判据内容换 HIMI 版 | `03` §3.2 |
| C15 | 导入侧「生成侧与消费侧分离」门控形状（无论开关都预生成；失败不阻断 + 显式关复选框） | `ui/ui_func_import_ssmt.py:634-714` | 直接复用（`:696-708` 的回退分支对 HIMI **天然生效**，无需改动） | `03` §3.1/§3.2 |
| C16 | 契约判定纯函数 `evaluate_merged_skeleton_contract` + 用户文案 | `common/zzmi_merged_contract.py:32-95` | 直接复用（无 bpy 依赖） | `02` §7.1 |
| C17 | 导出前顶点组预处理纪律（补缺 / 排序后恒等重命名 / 非数字组处理） | **实现**：`_prepare_merged_skeleton_vertex_groups`（`common/submesh_model.py:747-797`）；**ZZMI 壳（纯委派）**：`_prepare_zzmi_merged_skeleton_vertex_groups`（`common/submesh_model.py:798-810`，函数体只有 `self._prepare_merged_skeleton_vertex_groups(obj)`） | **直接复用** —— 但**HIMI 不调用这两者**（t45 实测：`run_merged_skeleton_preprocess` 门只认 EFMI/ZZMI，HIMI 各分支皆 False）；**HIMI 的等价物 = 导入期密集建组**（`common/mesh_create_helper.py:515-517`）。**t44 更正（O-1）**：原行引 `:385-436` 是**陈旧锚点**，并把「ZZMI 壳 docstring 的契约措辞」当成了实现 —— 两者有细节差异（非数字组：实现**移除并打印计数**，ZZMI docstring 写「不剔除任何组 / 只警告」）⇒ 详见 §13.9 的 T44-O1 / T44-F1 与 §13.10 | `ZZMI骨骼合并计划书.md` §7 修复链 R7 |
| C18 | `blend_size` 按目标 IB 的 `BLENDINDICES` 通道数取（合并模式） | `common/obj_buffer_helper.py:571-604` | 直接复用；**HIMI 需纳入条件**（当前只判 `is_zzmi_family`） | `03` §4.2 E8b |
| C19 | 测试基建四种模板（真实数据 fixture / INI 产物断言 / 路由桩含 `_ExportHIMI` / 契约纯函数） | `tests/test_zzmi_skeleton.py:22-74`、`tests/test_zzmi_merged_skeleton_ini.py`、`tests/test_export_logic_name_htmi.py:26-196`、`tests/conftest.py:17-23` | 直接复用 | `03` §6.1/§6.2 |
| C20 | 复选框复用（`import_merged_vgmap`，默认 True）+ 访问器/设置器 | `common/global_properties.py:202-206`、`:500-506` | 直接复用；**不新增属性** | `03` §3.3 |
| C21 | `ModImpRuntime/` 缓存目录纪律（「不要删除」） | 项目 key 记忆 2026-09-21 | 直接沿用 | — |
| C22 | 离线可单测的数据层分层（`common/*_skeleton.py` 无 bpy 依赖） | `zzmi_skeleton.py` 全文 | 直接复用分层 | `03` §6.1 |

### 3.2 差异矩阵 —— 必须重写（附理由与来源）

| # | 项 | ZZMI | HIMI 实测 | 必须重写为 | 来源 |
|---|---|---|---|---|---|
| D01 | 运行时形变通道 | CS morph pass + deform VS 两段可挂载 | **零 CS/Dispatch**（五帧 0 命中） | **只走 deform VS 的 `vs-t0` 一个挂载点**；`Toolset/zzmi_merged_skin.hlsl` 与 `ui/universal/zzmi.py:2479-2551,5630-5695` 整体不移植 | `01` §6、`02` §7.3 |
| D02 | deform pass 的额外结构约束 | 只需 pointlist `Draw` + SO + `vs-t0` + `vb0` | deform 块的 `vs-t0` 是 Buffer，render 块是 Texture2D（同槽异型） | 识别顺序必须是 **(a) 先锁定 deform pass →(b) 再取 `vs-t0` →(c) 再要求 `Dumping Buffer` 且 deduped 为 `.buf`**；否则会把渲染 pass 的阴影图当调色板 | `01` §1.1 D3、`04` §7 D3 |
| D03 | palette 与骨骼数语义 | 每部件 palette 只含本部件骨骼；`len(palette) >= vg_count` 以等号成立 | palette 是**骨骼池**（尾部空槽 + 中间空洞）；`palette_bones == imax+1` 仅 32/34 | **骨骼集合 = 只对被 `BLENDINDICES` 引用的槽做 bitwise 去重**；断言用「不越界」+「引用槽集合长度 == distinct 索引数」，**不得用 `==`** | `04` §1.2、§2.4（本计划书） |
| D04 | 矩阵缩放 | 旋转行模长 == 1.0（无缩放） | 3×3 = 正交 × 均匀 s，`s = cbrt(|det|)`；s = 1.6（4 帧）/ 1.0 / 0.93 | ① 禁用任何「行模长 ≈ 1」校验；② **刚性质心门控必须 scale-aware**（§10.2） | `01` §4.2、`04` §2.2 |
| D05 | 分组键 | 渲染 `vs-cb1` 对象变换（含 `first_constant` 窗口） | **零 `*SetConstantBuffers1`**；`vs-cb1` 的 `data[0:16]` 不是对象变换（105/151 不过 sanity，46 条是单位矩阵） | **`SkeletonGroup` 恒 0（整角色一组）**；`_CB_BIND_RE` / `get_vs_cb_first_constant` / `parse_object_transform` **不复用** | `02` §5.1、`01` §1.1 |
| D06 | cb1 缓存三件套 | `ObjectCB1FileName` / `ObjectCB1CacheValid` / `ObjectCB1FirstConstant` + `ModImpRuntime/<bare>-ObjectCB1.buf` | 同上（HIMI 无窗口化 cb1） | **不生成**这三个字段与文件；**显式 `ObjectCB1CacheValid = False`**；`_zzmi_source_cache_intact` 退化为恒真（保留不删） | `02` §5.1、任务书冻结项 |
| D07 | SO 语义 | SO → 另开一块重定向 buffer；每 SO 一偏移 | **SO 目标 = 该部件的渲染 VB（就地写入）**；每 pass **两笔** `SOSetTargets`（`pOffsets` `…E478`/`…E468`） | **不需要 SO 别名 / 不需要 RedirectSO 资源 / 不需要重定向计划**；`override_vertex_count` 保持原值（§7.5） | `02` §5.2 #2/#4、`01` §3.6 |
| D08 | 权重槽 | vb2（`BLENDWEIGHTS`+`BLENDINDICES`），且存在变宽（BI4/BI8/BI16 混用） | **Blend 在 `vb1`**；34/34 恒 `R32G32B32A32_FLOAT` + `R32G32B32A32_SINT`，每顶点 ≤ 4 权重，**无变宽** | 解析层仍走 `parse_blend_layout` 保留未来兼容；**本期不做升宽/降宽**（全局 id 直接装进 32 位 SINT） | `02` §5 表 #3/#4、`04` §5.2 |
| D09 | 合并几何重定向（任意 IB 挂载） | `_build_merged_mesh_redirect_plan` / carrier+target / base_vertex 自检 / **3 顶点 stub** | SO 就地写渲染 VB，无第二块 SO | **拆成两半（2026-09-23 更正，见 §7.7）**：<br>① `_build_merged_mesh_redirect_plan` / carrier+target / `base_vertex` 自检 / `RedirectSO` —— **确实不移植**（无第二块 SO 可重定向，原判成立）；<br>② **3 顶点站位（stub）—— 必须移植**（原判「整体不移植」**错**）。<br>**错因（T19 取证后精确化）**：站位承载**两条独立作用**（`13` §0/§4.3）——**A = 让缺席部件的 deform pass 在 mod 里跑起来、把当帧 palette 送进合并骨架**（依赖「骨架是否静态」）；**B = 抑制原版 draw 防重影**（不依赖骨架形态）。D09 的理由「合并不改几何拓扑」**只覆盖了 A 的推理**（HIMI 静态骨架 ⇒ A 无对应，这一半判对了），**完全没有覆盖 B**。B 的真实因果链是「合并让跨 IB 权重合法 ⇒ 用户自然会跨 IB join ⇒ **被吸收部件的原版 draw 在游戏里仍然存在** ⇒ 必须用站位让 mod 生成它的 IB override 段（`handling = skip`）把它吞掉，否则**重影**」——**与 SO 是否重定向、骨架是否静态全都无关**。**后果**：用户实测发现 HIMI 完全没有站位机制（跨 IB join 后原版 draw 重影） | `02` §7.2（原判来源，**其「3 顶点 stub」一项已被更正**）、`01` §8.4；**T19 取证 = `13-stub-anatomy.md`（449 行 / 240 处 文件:行号）**；更正与完整规格见 **§7.7**；ZZMI 原文依据 `ui/universal/zzmi.py:872-890,1120-1170,5785-5787,3449-3454` |
| D10 | 运行时骨架写方式 | 逐帧 CS attach（`copy vs-t0` + `cs-u0` 写槽）+ 出现次槽位 + 每槽守卫 + 姿态指纹 | 骨架 = **抓帧时的 bind 姿态**，逐帧不变；且无 CS | **静态骨架资源**：导出时直接落盘 `Meshes/<role>-MergedSkeleton.buf`，INI 声明 typed `Buffer` 只读，**无 CS、无 attach、无 [Present] 状态机** | `01` §4.2（s 逐帧一致）、`04` §2；推断：bind 姿态逐帧不变（**待验证**，§11 V1）<br>⚠ **更正（2026-09-25 / t69 回写，原文保留在上）：本行结论已被实测推翻** —— ① **palette 是每帧内容**：同一资源哈希 `vs-t0=23de2d6a` 跨 **14** 个抓帧内容 **14/14 不同**，对照**静态**资源 `vb0=1a6d79fc` 为 **10/10 相同**（`27-static-vs-perframe.md` §1、`28-perframe-attach.md` §5.2 的 CF-1 = 9/9；t69 独立复测 14/14）；② **合并骨架 ≡ 当帧五族 palette 的拼接**（`27` §1.3）。⇒ **现状正确形状** = 「逐帧捕获 `copy vs-t0` → attach CS 写本族区间 → deform 段 `vs-t0` 指向骨架」，骨架资源段**无 `filename`**（落地见 `28-perframe-attach.md` / `29-t52-handoff.md`）。⇒ 本行「无 CS / 无 attach / 无 `[Present]` 状态机」三项**均不成立**，逐帧状态已需要（见 N08 更正的**口径边界**） |
| D11 | 通道骨 / 键池 / 槽守卫 | `common/zzmi_channel.py` 全套 + `[PoolZZMISlotOfKey_G<g>]` | HIMI 是否真有「同实例跨部件共享同一根骨」未取证 | **本期不实现**；`ChannelPlan*` 字段形态保留但不写 | `02` §3.3/§7.2 |
| D12 | 导出空间变换 | ZZZ 不旋转 | **HIMI 做 −90°X**（导出几何在旋转空间，palette/SO 在游戏空间） | 合并骨架是**游戏空间**量（直接取 dump 字节），**不得**参与导出旋转；导出几何的顶点顺序/行号约束见 §7.5 | `03` §4.2 E9、`04` §7 D7 |
| D13 | `match_first_index` | 每部件非零偏移 | **恒 0**（46/46），每部件独立 IB | 合并重放**不得**按 `match_first_index` 区分部件；按 `draw_ib` + SO 行区间区分 | `03` §7 D10、`04` §7 补充 |
| **D13 更正**<br>（2026-09-26 / t23） | `match_first_index` | 每部件非零偏移 | ⚠ **原「HIMI 实测 = 恒 0（46/46）」不成立（已更正，原文保留在上）**：46/46 全 0 的是 submesh JSON 的 `IndexOffset/IndexCount/VertexOffset/VertexCount`；ini 的 `match_first_index` 读的是**物体名第 3 段**（`common/object_prefix_helper.py:295-297`）⇒ **实测非零且逐子网格不同**：`60639 / 54936 / 55380 / 47199 / 1716 / 9372 / 4656`（其余 5 段为 0）。**即原行 ZZMI 栏写的「每部件非零偏移」才是对的** | 原「必须重写为」栏的结论**不变**（合并重放仍不得按 `match_first_index` 区分部件；按 `draw_ib` + SO 行区间区分）；t22 已实测 12 个 `(hash, MFI)` 键**两两互异**、**无键命中不同 `IndexCount` 的 draw** | `64-matchfirstindex-forensics.md` §1.5/§5；`48-forensics-artifacts.md` §4.4；**更正详情见 §13.14** |
| D14 | 复选框可见性 | 在 `(WWMI, ZZMI, EFMI)` 内 | HIMI 不在该元组 | **必须把 `LogicName.HIMI` 加进 `ui/ui_panel_basic.py:562`**，否则开关默认 True 却不可见、用户无法回退 | `03` §3.3 |
| D15 | `blend_size` 门控 | `is_zzmi_family(...) and import_merged_vgmap()` | HIMI 不是 ZZMI 家族 | 把 HIMI 纳入该条件（`common/obj_buffer_helper.py:571-574`），否则恒按 4 影响打包 | `03` §4.2 E8b |
| D16 | 空顶点组清理 | 合并模式下不清理 | HIMI 今天完全不清 | 引入合并后**必须保持不清**（`common/mesh_create_helper.py:282-299`），否则组 index 重排 → 全局骨骼 id 失效 | `03` §3.4 |
| D17 | 逐 draw VS 取样口径 | `VSSetShader` 行（状态去重） | `VSSetShader` 只在变化时出现；块首那条常是上一 pass 的残留 | **必须取 dump 文件名 `-vs=` 后缀**（771/771 无歧义） | `01` §7 |
| D18 | 「未 join 的 pass」语义 | 同帧其它角色/NPC | 只有 140134/`000035` 是真外来对象，其余 20 个在本角色候选表内 | 一律按 R1/R2 判归属，语义 = 「不属于本次落盘的合并目标集合」；**不使用「外来角色」措辞** | `01` §8、`05-decisions.md` §3 |

### 3.3 HIMI 不适用（不移植、不保留接口）

| # | ZZMI 组件 | 位置 | 依据 |
|---|---|---|---|
| N01 | `ObjectCB1FirstConstant` 窗口语义 | `zzmi_skeleton.py:566-575,1860-1875,2178-2183` | HIMI 零 `*SetConstantBuffers1`（F7） |
| N02 | CS morph pass 相关（`Dispatch` 计数、CS 路径布局校验） | `ui/universal/zzmi.py:2139-2148,2151-2388` | HIMI 零 `Dispatch`/`CSSet*`（F6） |
| N03 | 变宽 BI/BW 的升宽/降宽决策 | `zzmi.py:4529-4550,4799-4809` | HIMI 恒 `BW16_BI16`（`04` §5.2） |
| N04 | vb2 权重槽溯源（`cs-t1 = ref vb2`） | `zzmi.py:563-564,350-372,4528-4537` | HIMI Blend 在 **vb1**（D08） |
| N05 | 跨组引用守卫的**触发路径** | `zzmi.py:4230-4279` | HIMI 单组 ⇒ 恒不触发；**机制保留不删**（多对象扩展护栏） |
| N06 | `ResourceZZRedirectSO_G<g>_s<k>` 的跨组覆盖防护 | `zzmi.py:2112-2124` | HIMI 无 RedirectSO（D07） |
| N07 | 蒙皮 CS 发布（按索引写 SO） | `Toolset/zzmi_merged_skin.hlsl` 全文 | HIMI 无 CS 且 SO 就地写（D01/D07） |
| N08 | `[Constants]`/`[Present]` 的 occ/seen/prev 状态机、键池、姿态指纹键 | `zzmi.py:5347-5376,5607-5628,5698-5719` | HIMI 静态骨架 ⇒ 无逐帧状态需要维护（D10）<br>⚠ **更正（2026-09-25 / t69 回写，原文保留在上）：前提已倒（D10 已加注）⇒ 逐帧状态「需要」维护，本行的「不适用」只对一半** —— **需要**的是：到达标记 `$hi3_ms_seen_<fam>`（每族 deform 段顶层自增）+ `[Present]` 清场 + 次序/重放/闩锁探针 + **上一帧快照**（`seen_prev`，用于顺序无关的「用上一帧捕获重放」）；**仍不移植**的是：ZZMI 的**出现次双槽 `occ`**（8/8 帧实测每族恰 1 笔 ⇒ 单槽足够，见 `37-arrival-guard.md` 抬头更正）、**键池**（`[PoolZZMISlotOfKey_*]`，与 HIMI 无关）、**姿态指纹键**（HIMI 无 key 概念）。⇒ 口径：**「逐帧状态」整体从「不适用」改为「部分需要、口径已缩到到达/次序/上一帧」** |

---

## 4. 数据链路五段（逐段指明复用/重写）

### 4.1 第 1 段：dump 反查（复用 C01–C06 + C11，重写 D02/D05/D17/D18）

| 步骤 | 实现 | 复用/重写 |
|---|---|---|
| 定位 dump | `Config/FrameAnalysisPath.json` → `resolve_frame_analysis_dir`（失效**不偷换帧**） | 复用 C11（`zzmi_skeleton.py:722-758`） |
| 解析 log | `HIMILogParser`：draw 前缀状态机、资源描述行、dump 逻辑名→deduped 映射、IB 跨 draw 继承 | 复用 C01–C04 |
| 识别 deform pass | 结构判据 = 存在 `SOSetTargets`（带 hash 续行）**且** 存在非索引 `Draw(VertexCount:N, StartVertexLocation:0)` **且** `vs-t0` dump 为 `Buffer` 且 deduped 为 `.buf` | 复用 C05 + **新增 D02 的 `.buf` 约束** |
| 逐 draw VS | 取 dump 文件名 `-vs=` 后缀（HIMI 里 deform VS 恒 `10905ed856e6b621`，但**不硬编码**） | 重写 D17 |
| join | A：`CategoryHash.Position == vb0`；B：`VertexLimitVB == SO`；C：render draw 的 `vb0 == SO` **且** `IASetIndexBuffer == DrawIB`（先 IB 门控） | 复用 C06 |
| 归属判定 | R1（A/B/C 三选一）+ R2 反判据；未命中 = 不属于本次落盘集合 | 重写 D18 |
| 取 palette | `(draw id, vs-t0, Buffer, .buf)` → `deduped/<content>.buf`；**绝不按资源 hash** | 复用 C04，**强化 F3** |
| 读 BLENDINDICES | `vb1` 的 deduped `.buf`，32 B/记录（`0:16` = 4×f32 权重，`16:32` = 4×i32 索引） | 复用（`04` §1 口径） |
| 分组 | **恒 0**（不解析 cb1） | 重写 D05/D06 |

### 4.2 第 2 段：工作空间落盘（复用 C09–C13，重写字段内容）

写回位置与形状对齐 ZZMI：每成员子网格 json 写字段（对照 `zzmi_skeleton.py:2091-2146`），二进制缓存写 `ModImpRuntime/`（对照 `:2150-2194`，经 `_atomic_publish_skeleton_transaction` 提交）。**字段全集与逐字段契约见 §5.1。**

产物清单：

| 产物 | 路径 | 内容 |
|---|---|---|
| 每组件 json 字段 | `LOD0/<bare>/<WorkGameType>/<bare>.json` | §5.1 的字段全集 |
| palette 原始字节 | `<bare>/ModImpRuntime/<bare>-BoneMatrix.buf` | **「按引用序压缩的槽表」**（T6 偏离 ②）：行 `j` = 该组件**第 `j` 小的被引用局部槽**（`referenced_slots` 升序）的 48 字节切片；`BoneMatrixSlotCount` = 被引用槽数（**不是**池大小、**不是** `VGCount`）。**不是 palette 池的镜像**（池含未引用槽，镜像会把行号错位）。导出侧核对方式：用工作空间原部件 Blend 的 `referenced_slots`（升序，与写出侧同一输入/算法）+ `VGMap` 对回角色骨架做**逐字节**比较；不一致的部件被剔出目标集合 + 大声诊断 |
| 合并骨架（角色级） | `<角色工作空间>/Config/HIMIMergedSkeleton.json` + `Config/<role_key>-MergedSkeleton.buf` | 全局槽位表 + 静态骨架字节（§5.2） |

**幂等**：`_himi_cache_intact` 全条件成立 ⇒ 整批跳过（零文件读写）；任一不成立 ⇒ 整批重建（不允许「部分部件用缓存、部分新算」——槽位口径必须一次算齐，对照 `zzmi_skeleton.py:1396-1406` 的分组版一致性规则）。

**cache-only（dump 目录消失 + 缓存完好 ⇒ 不重算、不写盘）的最终状态**（T13 修复 + **T14 独立裁决 pass**，2026-09-23 定稿）：

| 侧 | 状态 | 依据 |
|---|---|---|
| **导入侧** `ensure_skeleton_data()` | **已修复**（T13）：`prepare()` 在 `resolve_frame_analysis_dir()` 返回空时不再提前失败，改调 `cache_only_plan()`；新增 `_himi_cache_intact_without_dump()`（**零 dump 读取**的结构性校验：角色级 `AlgorithmVersion`/`GateVersion`/`SkeletonFileName`/`SkeletonDigest`/`SlotCount`、骨架文件 `SlotCount*48` 字节且 sha256 匹配、每组件 `HIMIMergedSkeletonDigest`/`VGMapAlgorithmVersion`/`VGMap` 完整覆盖/`BoneMatrixSlotCount*48` 缓存存在、`ComponentSet` 覆盖）；`ensure_skeleton_data` 返回 `(True, "…cache-only…")` | 真实数据 11/11 通过（`%TEMP%` 副本、零残留）：删 dump + 缓存完好 ⇒ `ok=True` / cache-only / slot=276 / **零写入**；**真·无缓存 ⇒ `ok=False` + 「无法 cache-only 重建」+「不做自动改用最新帧兜底」+ tree 零变化** |
| 导入侧 · **带 `target_bares` 的请求** | **必须拒绝 cache-only**（返回 `ok=False` / `gate_source='cache-only-unavailable'`）：`target_bares` 的语义 = 按集合重算 **pass 级 G1**，脱离 dump 无法做；若用空 `writes` 回答，导出侧会把「权威可用但放行 0 个」当成判定，使保守退路**静默失效** | `common/himi_skeleton.py` 的 `cache_only_plan`；T14 独立复验（`ok=False` / `gate_source="cache-only-unavailable"` / `force=True` 亦拒绝 / 全量导出 `rebinds=6`、部分导出 `rebinds=0` 且诊断显式说明） |
| **导出侧** `load_workspace_plan` | **已支持**（T7）：dump 缺失 ⇒ `gate_source=cache-only`、6/6 部件重绑；**部分导出 ⇒ 保守地全部不重绑** | `common/himi_merged_runtime.py:1415` |
| **独立脚本的旧契约断言** | ✅ **T14 独立裁决：pass，无真实回归**。`reports/himi-merge/11-t13-adjudication.md`（340 行）逐条裁决三条 FAIL：① `导入侧在 dump 缺失 + 缓存完好时显式失败` → **预期语义变化**（旧断言编码的正是 T13 有意修掉的行为；旁证 = 导出侧早在 T7 就用同一语义 `gate_source=cache-only`，且 T8 脚本对导出侧的断言本轮仍 PASS）；② `dump 缺失 + 无缓存 ⇒ 显式失败` → **脚本前置条件不足**（旧表达式 `os.path.join(rec["comp"]["dir"], "ModImpRuntime")` **恒为空** ⇒ 循环未移任何缓存；物证已做成断言：**旧表达式 0 个 / 真实 6 个**）；③ `FrameAnalysis 目录不存在 ⇒ 显式失败` → **脚本前置条件不足**（只改 `Config`/`Tabs` 指针、从未触碰缓存 ⇒「缓存完好」不变，新契约正确地走 cache-only；旧断言把「指针失效」误当「缓存不可用」）。**核心反证**：用真实路径把 6 个 `LOD0/<bare>/ModImpRuntime` 整体移走并断言磁盘上不再有 `ModImpRuntime` 后 ⇒ `ok=False` / `gate_source=cache-only-invalid` / `cache_only=False` / `writes=0` / `cache_only_missing` 点名全部 6 个组件 / 产物字节级不变；**恢复缓存后 cache-only 又成功且产物回基线** ⇒ 直接推翻「实现把一切 dump 缺失都静默变成 cache-only 成功」 | `reports/himi-merge/11-t13-adjudication.md` §2–§4；oracle 基线演进 **159（T8）→ 184（T14：删除 0 / 改写 5 / 新增 25）→ 194（T17：删除 0 / 改写 4 / 新增 10）**；**两次改写都是「删除 0 条」**（护栏只增不减，未放松任何要求） |
| **台账缺陷（登记，终态不可改）** | `t13` 的任务 ledger `output` 字段是一句占位文字（「占位探测：仅用于确认 verify 门禁是否读 exitCode」），**不是它的真实交付描述**。**权威记录 = T13 的交付说明消息 + `11-t13-adjudication.md`**（T14 明确不采信该 `output` 字段）。**教训：提交占位探测以穿过 verify 门禁是错误做法**——正确做法是停下来报告（说明门禁为什么过不去、需要什么裁定），用无信息的文字让 `verify` 通过会让台账失去可追溯性、后续裁决只能依赖旁路消息 | 本表与 `10-integration.md` §6.3 |
| **T14 登记的公共 API 陷阱（已由 t16 修复 + t17 独立复核成立）** | ✅ **R-T14-1（medium）已修复并独立复核**。**权威 runtime sha256 以 t17 实测 pin 为准 = `85F1BBCF…4D8E`**（旧值 `3DE7E86A…A983`）。**t16 修复语义 = fail-closed + 双重判据（缺一不可）**：① `_plan_declares_no_recompute()`（`common/himi_merged_runtime.py:2571-2586`）读数据层**自己的声明**（`cache_only` 位，或 `gate_source` 前缀 `cache-only`）；② `_plan_pass_level_evidence()`（`common/himi_merged_runtime.py:2589-2609`）要求**正向物证**（`dump_dir` 非空 + `resolver is not None` + `metadata` 非空）——未来新增的、忘了打标记的捷径也会被拒。任一不满足 ⇒ `prepare_gate_oracle` 返回 `available=False` / `pass_level=False` + 可读 message。消费侧第二道闸 `oracle_authority()`（`:1781-1807`）拒绝任何自称非 pass 级或带「未重算」证据的 oracle（诊断「拒绝采用 G1 权威判定：…」）；并**反向钉死**——真 pass 级、放行 0 个的判定**必须被采纳**（否则退路会去重绑），附「这是 pass 级结论，不是退路失效」说明；**不误杀**（dump 在场 ⇒ `available=True / pass_level=True / safe_bares=6`）。**实测陷阱行为已消失**：`prepare_gate_oracle(ws, target_bares=None)` 现返回 `available=False safe_bares=[] stale=False`；喂进 `plan_from_metadata` 得 `gate_source=cache-only` + `rebinds=6 == len(parts)`，**不再是 `ok/prepare/rebinds=0`**。原现象（对照）：曾返回 `available=True, safe_bares=[]`，喂给 `plan_from_metadata` 得 `ok=True / gate_source="prepare" / rebinds=0`——即「自称 G1 权威、实际什么都没放行」。**可达性（当时已核）**：唯一生产调用点（`load_workspace_plan` 内）始终传非 None 的 `target_bares` ⇒ 已发布导出路径一直安全 | `tests/test_himi_merged_runtime.py:1854-2108`（R-T14-1 专门用例）；**独立复核 = `reports/himi-merge/12-t16-adjudication.md`（T17，8 场景逐项比对自述 vs 实测，全部一致）**。**不影响本计划书任何结论**。**使用注意（仍然有效）：集成侧若要直接调 `prepare_gate_oracle` / `plan_from_metadata`，仍建议传非 None 的 `target_bares`**。⚠️ 副作用（非回归）：T14 oracle 的 3 条**冻结基线断言**因该修复而失效，已由 t17 改写（**删除 0 条**），基线演进为 **194/194**——见 `10-integration.md` §7「基线演进链」 |
| **T17 登记的诊断措辞问题（已知改进项 I2，本轮不修）** | ⚠️ **R-T17-2（low，仅诊断措辞）**：`oracle_authority`（`common/himi_merged_runtime.py:2612-2638`）在「**dump 在场但 prepare 因别的原因失败**」时，理由串说成「自称没有做 pass 级重算」，而真实原因是 prepare 失败。**行为正确且保守**（拒绝 + 走退路，且用户同时能看到 oracle 自己的正确 message）。**队长裁定本轮不修**：属诊断措辞问题、不影响任何判定；而每改一次生产代码都要重跑并改写 oracle 基线（已发生两轮 T14/T17），**代价大于收益**。建议修法（约 3 行）：在 `oracle_authority` 里区分「无重算证据」与「prepare 失败」两种来源，分别给理由串 | 见 `10-integration.md` §4.2 I2 |
| **环境事件（已恢复，附队长独立核验）** | ⚠️ **T17 第一次探索脚本用 `shutil.copytree` 复制工作空间但未改写 `Config` 指针**，副本仍指向真实 K: dump，导致 `FrameAnalysis-2026-09-23-133530` 一度被改名；崩溃的 `finally` 已移回原名。**队长独立核验**：五份 dump 全部存在；133530 = **8140 个实体文件 + 846 个 deduped 符号链接**（与 T17 报告的 7294 / 846 一致，差额是符号链接计数口径）、`log.txt` = **2 322 674 B** 逐字节一致；`K:\SSMT-Package-master\3Dmigoto\HI3` 下**无任何 `-DELETED` / `-MOVED` 残留**；五个工作空间仍 `ModImpRuntime=0`、无 MergedSkeleton 文件。**教训：复制工作空间做实验必须用 `make_selfcontained_workspace()`**（`reports/himi-merge/08-verify-independent.py:868`：复制 dump + 改写 `Config`/`Tabs` 指针），否则 `move` 会打到真盘 | 见 `10-integration.md` §4.3 E4 |

### 4.3 第 3 段：导入消费（复用 C15/C20，改动点 = IP-01/03/05）

| 步骤 | 复用/改动 | 位置 |
|---|---|---|
| 预生成段 | **新增**（插在 `:694` 与 `:696` 之间，与 EFMI/ZZMI 段并列） | `ui/ui_func_import_ssmt.py:694` |
| 失败回退 | **无需改动**（HIMI 满足 `logic_name != LogicName.ZZMI`，天然落入 `:696-708`） | `ui/ui_func_import_ssmt.py:696-708` |
| 消费覆盖 | **新增 HIMI 分支**（复选框关 ⇒ `override = None`） | `ui/ui_func_import_ssmt.py:712-714` |
| 复选框可见性 | **把 `LogicName.HIMI` 加进元组** | `ui/ui_panel_basic.py:562` |
| VGMap 消费 | **无需改动**：`VGCount > 0` 自动放行（`common/ssmt_import_helper.py:47-56`） | — |
| component 构造 | **放宽到 HIMI**（`wwmi_vg_map is not None` 这一条本身就够） | `common/mesh_create_helper.py:209-231` |
| 空顶点组清理 | **确保 HIMI 不进入** `remove_unused_vertex_groups` | `common/mesh_create_helper.py:282-299` |
| 分组合集 | HIMI 单组 ⇒ **不需要** `SkeletonGroup_<N>` 归集（或统一归入 `SkeletonGroup_0`，二选一在 T6 定，默认不归集以减少改动面） | `ui/ui_func_import_ssmt.py:766-777` |

### 4.4 第 4 段：导出生成（重写为主）

| 步骤 | 参照 ZZMI | HIMI 差异 |
|---|---|---|
| 组件扫描 + 契约判定 | `ui/universal/zzmi.py:1351-1592`、`:1650-1713`、`common/zzmi_merged_contract.py:32-95` | 复用形状；契约判定必须在 `generate_buffer_files` **之前**（`ui/universal/drawib_export_base.py:18-46`） |
| `export()` / `export_buffers_only()` / `generate_unity_vs_config_ini()` 覆写 | `ui/universal/zzmi.py:6074-6085`、`:6087-6199` | 新增；`ExportHIMI` 类名与路由不变（`blueprint/export_parallel.py:135-136,163-164`、`blueprint/direct_export.py:150-151`） |
| VB 段注入（deform 重放） | `ui/universal/zzmi.py:4050-4106`（插入点 `:4076-4094`） | **HIMI 形态不同**：见 §7.4 |
| `VertexLimitRaise` | `ui/universal/zzmi.py:4108-4182` | **HIMI 保持原值**（`override_vertex_count = draw_number`，`ui/universal/unity.py:127-141`）；SO 就地写、行数不变（§7.5） |
| 资源声明 | `ui/universal/zzmi.py:5319-5737` | **大幅简化**：只需 1 个 typed 骨架资源 + 1 个 vg_map 二进制（可选）；**无 palette 资源、无 SO 资源、无 RedirectSO、无 CS** |
| 顶点组预处理 | **实现** `common/submesh_model.py:747-797`（`_prepare_merged_skeleton_vertex_groups`）；**ZZMI 壳** `:798-810` | **HIMI 不调用这两者**（t45 实测：`run_merged_skeleton_preprocess` 门 `:362-371` 只认 EFMI（`:365`）/ ZZMI（`:369`），HIMI 各分支皆 False）；**HIMI 的等价物 = 导入期密集建组**（`common/mesh_create_helper.py:515-517` ⇒ `index == int(name)`），另有导出后 `check_exported_blend_namespace` 复核（判 local 则退不重绑）。原行「把 HIMI 纳入 `run_merged_skeleton_preprocess`」的写法已由 t45 裁定为**文档错**，见 §13.9 **T44-F1** 与 §13.10 |
| `blend_size` | `common/obj_buffer_helper.py:571-604` | 纳入 HIMI（D15） |
| 自定义 shader 拷贝 | `ui/universal/zzmi.py:5737` | **HIMI 本期不需要任何自定义 shader**（无 CS、无换绑 shader） |

### 4.5 第 5 段：运行时资源（重写为「静态骨架 + 单点重绑」）

> ⚠ **本节已整体更正（2026-09-25 / t69 回写）—— 「静态骨架 + 单点重绑」是**错**的形状，照它做生成器会建成错的 mod**。
> 标题与下表**原文按纪律保留**，但**下列各行结论已被实测推翻**，正确形状写在每行后的 ⚠ 里：
> **骨架资源行**（不得带 `filename`）、**palette 资源行**（需要、且是**逐帧**捕获）、**CS 行**（需要 attach CS）、
> **`[Constants]`/`[Present]` 行**（需要）、**唯一运行时动作行**（是三步而非一步）、**节末「没有任何逐帧操作」**。
> 依据：`27-static-vs-perframe.md`（palette 每帧变 + 骨架 ≡ 当帧拼接）、`28-perframe-attach.md`（attach CS 落地 + CF-1 9/9）、
> `40-adversarial-review.md` §5.3 缺陷 ②。**未被推翻**：`vg_map` 行、SO/重定向行、以及「`binding` 形态（typed + `bind_flags`）」

| 项 | ZZMI | HIMI 规格 |
|---|---|---|
| 骨架资源 | 每组每槽 `ResourceZZMergedSkeleton_G<g>_s<k>` = `RWBuffer` + `format=R32G32B32A32_FLOAT` + `array=3×槽位` + `bind_flags=shader_resource unordered_access` | **每角色一份** `ResourceHI3MergedSkeleton_<role_key>` = **`RWBuffer`** + `format = R32G32B32A32_FLOAT` + `array = 3 × 槽位` + **`bind_flags = shader_resource unordered_access`** + `filename = Meshes/<role_key>-MergedSkeleton.buf`（**与 ZZMI 声明逐项相同**；UAV 那一半本期无写入方，属**冗余但安全**，理由与已否决备选见 §7.1）<br>⚠ **2026-09-25 / t69 更正**：末尾的 `filename = Meshes/<role_key>-MergedSkeleton.buf` 这一项**不成立**（骨架必须每帧被 attach CS 写入；t51 起产物已去掉 `filename`）。typed 三件套（`RWBuffer` + `format = R32G32B32A32_FLOAT` + `array = 3 × 槽位` + `bind_flags`）**保留**（跨显卡不变量，§7.3）。 |
| vg_map 表 | `cs-t1`（`format=R32G32B32A32_UINT`，二进制加载） | **不需要**（全局索引在导出时已写进 mod 的 Blend 顶点缓冲，运行时不做局部→全局查表） |
| palette 资源 | 每部件 `ResourceZZPalette_<draw_ib>_s<k>` = `Buffer / stride=48 / array=vg_count` + `copy vs-t0` | **不需要**（不做逐帧捕获）<br>⚠ **2026-09-25 / t69 更正：需要 —— 「不做逐帧捕获」已被实测推翻**（`27` §1/§1.3：palette 每帧变、骨架 ≡ 当帧拼接）。现状 = 每族一份 `ResourceHI3PaletteFam_<fam>`（`type = Buffer` + `stride = 48` + `array = 族槽数`，**绝不得加 `format`**；`copy vs-t0 unless_null` 捕获），源=该族自己的切片（§7 与 `32-deform-palette-binding.md` §4 实测：不溢出、不截断） |
| SO / 重定向资源 | `ResourceZZRedirectSO_G<g>_s<k>` | **不需要** |
| CS | attach（`cs`）+ skin 发布（`cs-u0`） | **不需要**<br>⚠ **2026-09-25 / t69 更正**：**attach CS 需要**（`Toolset/himi_merged_skeleton_attach.hlsl` + 每族 `[CustomShaderHI3MergedSkeletonAttach_<fam>]` 段 + 段**顶层**无条件 `run`；形状见 `28-perframe-attach.md` §0 A）；**只有「skin 发布 CS」不移植**（HIMI 仍由游戏 deform VS 蒙皮，§7.2） |
| `[Constants]` / `[Present]` | occ/seen/prev/pose_key 状态机 | **不需要**（无逐帧状态）<br>⚠ **2026-09-25 / t69 更正：需要（口径已缩）** —— 现状有 `[Constants]`（`global $hi3_ms_seen_<fam>` / 次序 / 重放与 draw 闩锁）+ `[Present]`（**先快照 `prev` 再清零**）；**不需要**的只是 ZZMI 的 `occ` 双槽、键池、姿态指纹（见 N08 更正与 `37-arrival-guard.md` 抬头更正） |
| 唯一运行时动作 | 每 deform pass：`copy vs-t0` + `run` attach + `vs-t0 = <骨架>` | 每 deform pass 的 VB 段（Blend 类别）内：**`vs-t0 = ResourceHI3MergedSkeleton_<role_key>`**（外加 `vb1 = Resource<ib>Blend`，见 §7.4）<br>⚠ **2026-09-25 / t69 更正：Z 侧这一列才是对的，HIMI 侧同样是三步**（每族 deform 段**顶层**、顺序不可换）：① `ResourceHI3PaletteFam_<fam> = copy vs-t0 unless_null`（捕获）→ ② `run = CustomShader…Attach_<fam>`（attach CS 写本族区间）→ ③ `vs-t0 = <本族骨架>`（换绑）+ `vb1` 换成含**全局**索引的 mod Blend |

> **HIMI 运行时的完整语义**：deform VS 按 `palette[3b+m]` 读 `vs-t0`；把 `vs-t0` 换成合并骨架后，`b` 必须是**全局槽位号** ⇒ 必须同时把 `vb1` 换成**含全局索引的 mod Blend 顶点缓冲**。deform 输出仍写进游戏的 SO（= 渲染 VB），渲染 draw 完全不变。~~**没有任何逐帧操作。**~~
⇒ ⚠ **2026-09-25 / t69 更正：恰恰相反** —— 每族 deform 段**每帧**都在做「**捕获 → attach CS 写骨架 → 换绑**」三步
（`27-static-vs-perframe.md` / `28-perframe-attach.md`；骨架资源因此**不得**有 `filename`）。
本节余下**仍然成立**的部分：`vs-t0` = `palette[3b+m]` 的 typed 读法（`33-view-experiment-matrix.md` 实机结案）、
`b` 必须是**全局槽位**、SO 就地写（无重定向）。

---

## 5. 逐字段冻结的元数据契约

> 命名纪律：**与 ZZMI 同名同义者原样沿用**（便于对照维护与复用既有缓存门代码）；HIMI 特有的新字段加 `HIMI` 前缀或写进独立角色级文件。**改变理由逐条给出。**

### 5.1 写回每成员子网格 json 的字段

| 字段 | 类型 | 语义（HIMI 口径） | 生产者 | 消费者 | 命名 |
|---|---|---|---|---|---|
| `VGCount` | int ≥ 0 | **`max(被引用局部槽下标) + 1`**（**不是**被引用槽数、**不是** palette 池大小） | 数据层 `himi_skeleton.py`（`vg_count = max(被引用局部槽下标) + 1` 的计算点，`common/himi_skeleton.py:921`） | 导入 `common/ssmt_import_helper.py:47-56`（`VGCount > 0` 才放行）、`common/submesh_json.py:51-53`、`common/mesh_create_helper.py:451-457`；导出 `common/submesh_model.py:369` | 沿用 ZZMI（同名同义） |
| `VGOffset` | int ≥ 0 | 本组件在**角色级合并骨架**中的起始槽位（组基址语义；HIMI 单组 ⇒ 组基址恒 0） | 同上 | 同上 | 沿用 ZZMI |
| `VGMap` | dict[str,int] | `{局部槽下标(str): 全局槽位(int)}`；**键必须完整覆盖 `0..VGCount-1`**：被引用槽 → 对应全局槽位，**空洞槽 → `槽位数 + 局部下标`**（合成值，无顶点会读到，仅为满足导入侧的完整覆盖硬校验） | 同上 | 导入 `common/submesh_json.py:141` → `ssmt_import_helper.py:52-56` → `mesh_create_helper.py:209-217,496-531`；导出 `common/obj_buffer_helper.py` | 沿用 ZZMI（**键集口径与 ZZMI 相同**：都是 `0..VGCount-1` 的完整覆盖） |

> **T6 偏离 ① 的口径更正（2026-09-23 队长裁定：计划书错、实现对）**：设计初稿写的是
> 「`VGCount` = 被引用槽数、`VGMap` 键集 = 被引用槽集合」。这在导入侧**会直接崩**：
> `common/submesh_json.py:51-53` 的 `set(normalized) != set(range(vg_count))` 会判元数据无效，
> `common/mesh_create_helper.py:451-457` 的 `actual_keys != set(range(expected_count))` 会抛 `Fatal`，
> `common/mesh_create_helper.py:526-531` 的 `component.vg_map[int(i)]` 缺失也抛 `Fatal`。
> **正确口径**（已实现并有测试）：`VGCount = max(被引用局部槽下标) + 1`，`VGMap` 完整覆盖
> `0..VGCount-1`——被引用槽映射到全局槽位，**空洞槽映射到 `槽位数 + 局部下标`**（一个必然越界的
> 合成值；因为「只对被引用槽去重」这条纪律，没有任何顶点的 BLENDINDICES 会指向空洞槽，所以
> 该值永不被读到）。这样**既保住 D03 的「不越界 + 引用槽集合长度 == distinct 索引数」两条不变量，
> 又满足导入侧的完整覆盖硬校验**。
> 实证：`2c1f2636-14415-0` 的 `VGCount = 156`（= `max(引用下标)+1`）而引用槽只有 **148** 个
> （8 个空洞槽指向 `槽位数 + local`）；`557a7477-3966-0` 的 `VGCount = 95` 而引用槽只有 **8** 个。
> 判据测试：`tests/test_himi_skeleton.py` 的 `test_real_referenced_slot_totals_are_pinned`
> 逐角色钉死 `(referenced, pool)`；`_merged_skeleton_metadata_valid` 必须返回 `True`（§9.4 C4）。
| `VGMapAlgorithmVersion` | int | HIMI 缓存算法版本，**从 1 起**（ZZMI 当前 5，两者独立演进） | 同上 | 缓存门 `_himi_cache_intact` | 沿用字段名，**值独立**（改变理由：算法口径不同，不能与 ZZMI 混用） |
| `SkeletonGroup` | int = **0** | 骨架分组号。HIMI **恒 0**（整角色一组；无 cb1 对象变换可分组） | 同上 | 导出组守卫（保留机制，恒真） | 沿用 ZZMI（值恒 0；改变理由见 D05） |
| `DeformDrawIndex` | int ≥ 0 | 本组件 deform pass 的 draw 序号（6 位字符串转 int，例 `000035` → 35） | 同上 | 导出侧时序判定（HIMI 下仅用于诊断与 R4 风险判定） | 沿用 ZZMI |
| `OriginalVertexCount` | int ≥ 0 | 本组件几何行数（= `Blend.buf` 字节数 / 32，且 == 该 deform pass 的 `Draw(VertexCount)`，34/34 实测相等） | 同上 | 导出侧 SO 行数/顶点数预算 | 沿用 ZZMI |
| `BoneMatrixFileName` | str | 指向 `ModImpRuntime/<bare>-BoneMatrix.buf`（**文件名，非绝对路径**） | 同上 | 无 dump 时的重建回退（`zzmi_skeleton.py:1621-1630` 同款） | 沿用 ZZMI |
| `BoneMatrixSlotCount` | int | 该缓存文件按 48 B/骨切出的槽数（= 去重前池内**被引用槽数**，用于大小校验） | 同上 | 缓存门（`size % 48 == 0 and size >= 48 * max_referenced_index+1`） | **HIMI 新增**（ZZMI 用 `VGCount*48` 判大小；HIMI 必须按「最大被引用下标」判，理由见 D03） |
| `FrameAnalysisLogSignature` | str | 当次 `log.txt` 全量 sha256 | 同上 | 有 dump 时快路径比对（不一致 ⇒ 整批重建） | 沿用 ZZMI |
| `BoneIdentityVersion` / `BoneIdentityDigest` | int / str | 身份摘要口径版本（HIMI 从 1 起）+ `{局部槽: 全局槽位}` 规范化 sha256 | 同上 | **无生产消费者**（诊断/复核） | 沿用 ZZMI（纯诊断） |
| `BoneIdentityBasis` | str = `"local_slot_map"` | 自述身份口径（弱代理，与 ZZMI 同） | 同上 | 产物注释 | 沿用 ZZMI |
| `BoneMatrixFrameDigest` | str | 当帧 palette 字节的 sha256（**只证明同一次抓帧**，任何去重/身份判定都不得读它） | 同上 | 无 | 沿用 ZZMI |
| `HIMIPalettePoolSlotCount` | int | 该组件 deform pass 的 palette **池大小**（= `palette_bytes / 48`） | 同上 | 诊断；导出侧「不越界」断言 | **HIMI 新增**（HIMI 特有：池 ≠ 引用集合，必须分开记录，理由见 D03） |
| `HIMIPaletteScale` | float | 该 palette 的均匀缩放 `s = cbrt(|det|)`（逐骨骼一致性已实测 39/39） | 同上 | 质心门控的空间归一化（§10.2） | **HIMI 新增**（ZZMI 无缩放，无此概念；禁止硬编码 1.6） |
| `HIMIMergedSkeletonFile` | str | 指向角色级合并骨架二进制（相对工作空间根，例 `Config/<role_key>-MergedSkeleton.buf`） | 同上 | 导出侧资源声明 | **HIMI 新增**（角色级产物，ZZMI 是组级） |
| `HIMIMergedSkeletonSlots` | int | 角色级合并骨架槽位数（= 该角色全局唯一骨骼数：219–343） | 同上 | 导出侧 `array = 3 × slots` | **HIMI 新增** |
| `HIMIMergedSkeletonDigest` | str | 合并骨架字节的 sha256（幂等/一致性校验） | 同上 | 缓存门、导出前校验 | **HIMI 新增** |
| `HIMIDeformFamilyIB` | str | 该组件所属 **IB 家族**（= 组件名前缀 `DrawIB`，例 `2c1f2636`） | 同上 | **导出侧门控 G1**（§7.4） | **HIMI 新增**（HIMI 特有：deform pass 按 IB 家族共享，必须显式记录家族身份） |
| `HIMIDeformFamilyMembers` | list[str] | 该家族**全部**已落盘组件名（排序） | 同上 | **导出侧门控 G1** | **HIMI 新增** |
| `HIMIDeformGateSafe` | bool | 该组件所属 deform pass 是否通过门控 G1（**家族安全 ∧ pass 安全**，见 §7.4 G1） | 数据层算「家族 + pass 自洽」，导出层按实际导出集合**重算**（`prepare(target_bares=...)`） | 导出侧 | **HIMI 新增** |
| `VGMapDedupExcluded` | bool（输入 + 写回） | 组件级「排除去重」标记：**为真则整组件独占自己的槽位**（恒等映射、不注册 owners、别的组件查不到它的槽位），并记入角色级 `DedupExcludedComponents` | 外部/用户（输入）；数据层写回原值 | 数据层 `build_merged_skeleton` 的 `fresh` 分支 | 沿用 ZZMI |
| `GPU-PreSkinning` | bool（**输入字段**） | **本期补的是「读取」语义**：显式 `False` 的组件不参与合并（与 `common/efmi_skeleton.py:4400`、`common/zzmi_skeleton.py:1334` 同款），记入角色级 `CPUPreskinningSkipped`，且**不得把同 pass 的其它家族拖成 missing**（`family_gate` / `pass_families` / `gate_allows` 带 `skip_bares`），也**不因缺合并元数据被误判需修复** | 提取端（46/46 份 json 本就存在且恒 `true`） | 数据层组件级参与门 | 沿用 ZZMI（字段本身是**输入**，不是本方案新增产物） |

**显式不生成**（HIMI 不适用，理由见 D05/D06）：

| 字段 | 处理 |
|---|---|
| `ObjectCB1FileName` | **不生成**（显式 pop） |
| `ObjectCB1CacheValid` | **显式写 `False`**（三态契约里的「本次生成明确无有效对象变换」档） |
| `ObjectCB1FirstConstant` | **不生成** |
| `ModImpRuntime/<bare>-ObjectCB1.buf` | **不落盘** |
| `ChannelPlan*` / `ChannelPlanVersion` / `ChannelPlanDigest` / `ChannelPlanSlotWeights` | **不生成**（D11；字段命名空间保留给未来） |

### 5.2 角色级合并骨架产物

| 项 | 规格 |
|---|---|
| 元数据文件 | `<工作空间>/Config/HIMIMergedSkeleton.json` |
| 元数据字段（实现全集，`common/himi_skeleton.py:2749-2827`） | `AlgorithmVersion`(int)、**`GateVersion`(int)**、`RoleKey`(str)、`SlotCount`(int)、`Slots`(list[dict])、`SkeletonFileName`(str)、`SkeletonDigest`(str)、`SourceLogSignature`(str)、`FrameAnalysisPath`(str)、`ComponentCount`(int)、**`ComponentSet`(list[str])**、`ReferencedSlotTotal`(int)、`PoolSlotTotal`(int)、`SharedSlotCount`(int)、`DeformPassTotal`(int)、`DeformGateRejected`(list[str])、`DeformGateRejectedDetail`(list)、**`DeformGateBlockedPasses`(list[dict])**、**`DeformGateMode`(str = `"pass"`)**、**`CPUPreskinningSkipped`(list[str])**、**`DedupExcludedComponents`(list[str])** |
| `Slots[i]` | `{ "slot": int, "source_components": [str], "source_local": [int], "matrix_sha256": str }`（槽位定义 + 来源可追溯；`matrix_sha256` = 该 48 字节的 sha256） |
| `ComponentSet` | 本次写回覆盖的组件 bare 集合（排序）。**cache-only 快路径靠它判定「工作空间里出现的组件是否都已在缓存里」**——没有它就无法区分「缓存完整」与「这次多了一个从未参与构建的组件」 |
| `ReferencedSlotTotal` / `PoolSlotTotal` | 该角色「被引用槽数之和」与「palette 池大小之和」。实测 511/1166/819/891/**594** 与 511/1166/819/891/**876**（镇×偃月叩晓的 282 差值是 D03 的直接体现） |
| `DeformGateBlockedPasses` / `DeformGateMode` | **pass 级门控明细**（见 §7.4 G1）：被同 pass 其它家族拖累而整 pass 拒绝时的 `{draw, families, unsafe_families, missing}` 列表；`DeformGateMode = "pass"` |
| `CPUPreskinningSkipped` / `DedupExcludedComponents` | `GPU-PreSkinning=False` 被跳过、与 `VGMapDedupExcluded=True` 独占槽位的组件清单（见 §5.1 的读取语义） |
| 骨架二进制 | `<工作空间>/Config/<RoleKey>-MergedSkeleton.buf`：**槽位 i 的 48 字节放在偏移 `i*48`**，布局 = 3×4 行主序 12×float32（与 dump palette 逐字节同构） |
| 槽位数 | 该角色全局唯一骨骼数（219/252/276/315/343，`04` §5.1）；**上限校验 `<= 65535`**（预留；实际远小） |
| 排序 | 槽位分配按 `sorted(part_key)` 的确定性顺序（复用 C07 的 canonical 规则），保证导入/导出两次计算一致 |
| `RoleKey` | 角色目录名的**文件名安全**派生（去掉/替换 Windows 非法字符，例 `嗨♪爱愿妖精♥` → 稳定 hash 后缀，实测 `role_a2681028`）。**改变理由**：HIMI 角色名含 `♪`/`♥`/`！`/`×` 等字符，直接入文件名/资源名不安全 |

### 5.3 不写回的字段（只读输入）

| 字段 | 用途 |
|---|---|
| `CategoryHash.Position` / `.Blend` / `.Texcoord` | join 键（A）+ 导出侧 VB 段匹配键 + 渲染 vb1 换绑 |
| `VertexLimitVB` | join 键（B）；导出侧 `VertexLimitRaise` 的 `hash` |
| `WorkGameType` | 布局选择（HIMI 3 种：`...T8_T1-8_` / `...T4_T1-4_` / `...T8_`） |
| `CategoryDrawCategoryMap` | draw 命令落在 **Blend** 段（`ui/universal/unity.py:58-61`） |
| `IndexBufferList[0].DXGI_FORMAT` | IB 打包 |
| `ShapeKeysInfo` | 形态键直出必须与合并共存 |
| `Import.json` | `<unique_str>` → `WorkGameType` 定位；**必须容忍「记录存在但文件缺失」**（67 条记录 / 46 份 json） |

---

## 6. 模块设计

### 6.1 新增 `common/himi_skeleton.py`（数据层，无 bpy 依赖）

| 类/函数 | 职责 | 对应 ZZMI |
|---|---|---|
| `HIMILogParser` | 解析 HI3 `log.txt`：draw 前缀状态机、`Draw(VertexCount)`、`SOSetTargets`（两笔 + hash 续行）、`IASetVertexBuffers`/`IASetIndexBuffer`、`VSSetShaderResources(vs-t0)`、dump 逻辑名→deduped 映射、逐 draw VS（**取 `-vs=` 后缀**） | `ZZMILogParser`（C01–C04）+ D17 |
| `HIMIDeformResolver` | deform pass 识别（结构判据 + `.buf` 约束）+ A/B/C 三 join + IB 门控 + R1/R2 归属判定 | `ZZMIDeformResolver`（C05/C06）+ D02/D18 |
| `HIMIBoneMapBuilder.load_palette` | 读 palette 为 `(N, 12)` float32 | `ZZMIBoneMapBuilder.load_palette`（C07） |
| `HIMIBoneMapBuilder.referenced_slots` | 由 BLENDINDICES 算「被引用槽集合」与 `imax_used` | **HIMI 新增**（D03） |
| `HIMIBoneMapBuilder.build_merged_skeleton` | 只对被引用槽做 bitwise 去重 → `VGMap` / `VGOffset` / 角色级 `Slots` / 骨架字节 | `build_vg_maps`（C07）+ D03 |
| `HIMIBoneMapBuilder.palette_scale` | `s = cbrt(|det|)` 与正交性校验（用于门控空间归一化） | **HIMI 新增**（D04） |
| `HIMISkeletonMergeHelper.ensure_skeleton_data(workspace_root, unique_str_list, force=False)` | 幂等总流程：定位 dump → 解析 → 反查 → 去重 → 写回 json + 落盘缓存 + 落盘角色级骨架 | `ZZMISkeletonMergeHelper`（C09–C14） |
| `HIMISkeletonMergeHelper.missing_merged_metadata_exist` | 自愈判据（缺 VGMap 家族 或 `_himi_cache_intact` 不通过） | C14 |
| `HIMISkeletonMergeHelper._himi_cache_intact` | 快路径幂等门（schema + 文件存在 + 大小 + digest） | C09 |
| `HIMISkeletonMergeHelper.deform_family_gate` | 门控 G1 判定（家族全组件在目标集合内） | **HIMI 新增**（§7.4 G1） |

依赖：`os` / `re` / `hashlib` / `numpy` + `utils.json_utils`；**不得 import bpy**。

### 6.2 新增 `common/himi_merged_contract.py`

照抄 `common/zzmi_merged_contract.py`（C16）的四个 level（`ok`/`notice`/`warning`/`error`）与文案形状，仅替换措辞与「运行时合并骨架」的资源名。**HIMI 额外加一个 error 档**：`HIMIDeformGateSafe=False` 的组件出现在导出集合里时，必须**大声报警并拒绝该组件**（不是整批中止）——理由见 §7.4 G1。

### 6.3 扩展 `ui/universal/himi.py`（导出器）

`himi.py` 只做委托，本体拆到 `ui/universal/himi_merged.py`（保持「一个逻辑一个导出器」结构，对照 `ui/universal/zzmi.py` 的形态）：

| 覆写 | 内容 |
|---|---|
| `export()` / `export_buffers_only()` | 顺序：组件扫描 + 契约判定 → `generate_buffer_files` → ini 里插合并段（对照 `zzmi.py:6087-6199`） |
| `generate_unity_vs_config_ini()` | 调基类（`ui/universal/unity.py:391-422`），插桩点见 §4.4 |
| `add_unity_vs_texture_override_vb_sections()` | Blend 类别段内插入 deform 重放（§7.4） |
| `add_unity_vs_texture_override_vlr_section()` | **保持原值**（不覆写，除非 §7.5 的口径需要调整） |
| `add_unity_vs_resource_vb_sections()` | 追加角色级骨架资源声明 |
| 新增 `add_merged_skeleton_sections()` | 骨架资源 + 契约诊断注释 |

### 6.4 集成点总表（IP 编号沿用 T3 的 `03-himi-integration-points.md` §8）

| ID | 文件:行 | 动作 | 本期 |
|---|---|---|---|
| IP-01 | `ui/ui_func_import_ssmt.py:694`（插入点） | 新增 HIMI 预生成段 | ✅ 本期 |
| IP-02 | `ui/ui_func_import_ssmt.py:696-708` | **无需改动** | — |
| IP-03 | `ui/ui_func_import_ssmt.py:712-714` | 新增 HIMI 消费覆盖 | ✅ 本期 |
| IP-05 | `ui/ui_panel_basic.py:562` | 把 `LogicName.HIMI` 加进复选框可见集合 | ✅ 本期 |
| IP-07 | `common/mesh_create_helper.py:209-231` | 放宽 `component` 构造到 HIMI | ✅ 本期 |
| IP-09 | `common/mesh_create_helper.py:282-299` | 确保 HIMI 不进 `remove_unused_vertex_groups` | ✅ 本期（加断言/注释） |
| IP-10 | 新增 `common/himi_skeleton.py` | 数据层 | ✅ 本期 |
| IP-11 | `common/zzmi_skeleton.py:722-758` | 复用/提升为共享实现 | ✅ 本期 |
| IP-12 | `common/submesh_json.py:106-113` / `submesh_metadata.py:140-184` | 复用同一批缓存键 | ✅ 本期 |
| IP-13 | `ui/universal/himi.py:1-5` | 扩展为 HIMI 导出器（委托 `himi_merged.py`） | ✅ 本期 |
| IP-14 | `ui/universal/himi.py` 新增三个入口覆写 | 顺序照 `zzmi.py:6087-6199` | ✅ 本期 |
| IP-15 | 覆写 `add_unity_vs_texture_override_vb_sections` | 插入 deform 重放块（§7.4） | ✅ 本期 |
| IP-16 | 覆写 `add_unity_vs_texture_override_vlr_section` | **本期不改**（保持原值；见 §7.5） | ⛔ 非目标 |
| IP-17 | 覆写 `add_unity_vs_resource_vb_sections` | 追加骨架资源声明 | ✅ 本期 |
| IP-18 | 新增 `add_merged_skeleton_sections` + 组件收集 + 契约判定 | 结构照 `zzmi.py:5319-5737` 的**简化版** | ✅ 本期 |
| IP-19 | `Toolset/*.hlsl` 拷贝 | **不需要**（无自定义 shader） | ⛔ 非目标 |
| IP-20 | `utils/export_utils.py:171-210`、`common/submesh_model.py:236,1150` | 显式处理 −90°X 与合并骨架的空间关系 | ✅ 本期（文档化 + 断言） |
| IP-21 | `common/obj_buffer_helper.py:571-574` | 把 HIMI 纳入合并模式 `blend_size` | ✅ 本期 |
| IP-22 | `common/submesh_model.py:362-371` | 把 HIMI 纳入 `run_merged_skeleton_preprocess` | ❌ **未做（本期不做；t45 裁定为文档错，t46 回写）**。按当前代码该门只有 EFMI（`:365`）/ ZZMI（`:369`）两个分支，HIMI 各分支皆 False；**HIMI 也不需要它**：① 站位的「槽 0」结论由**桥的单组命名**保证（§7.7.5 的 t45 机制链），与预处理无关；② 导出侧顶点组命名空间的达成路径是**导入期密集建组**（`common/mesh_create_helper.py:515-517`）+ gametype BI16 + 导出后 `check_exported_blend_namespace` 复核。**将来若做，验收标准**：门加入 HIMI 分支后 —— `15-verify-stub.py` 仍 **79/79**、真实副本仍 **5 个 IB 段 + 4 个站位段 + `vs-t0`**、角色级 `SlotCount` **不变**（且上述三项在加/不加该门两种状态下结果一致，即「恒等」可实证） |
| IP-23 | `common/himi_merged_contract.py` | 契约纯函数（含 G1 拒绝档） | ✅ 本期 |
| IP-24 | `tools/himi_merged_probe.py` | 把 §2.8 的门控统计并入 `--self-check` | ✅ 本期（T6，20→23） |
| IP-25 | 新增 `common/himi_merged_runtime.py` | 导出侧资源计划层（`HIMIResourceDecl` 是 INI 与 HLSL 的**唯一来源**；A1/A2/A3/A4 校验器；`plan_from_metadata`；`load_workspace_plan`；`prepare_gate_oracle`；`read_palette_cache`/`verify_palette_cache_against_skeleton`；`resolve_deform_draw_count`；`publish_skeleton_binary`） | ✅ 本期（T7） |
| IP-26 | 新增 `Toolset/himi_merged_skeleton_attach.hlsl` | **声明与索引参考**（由 `render_attach_hlsl()` 生成、随仓库冻结）；**不被任何 ini 段引用、也不拷进 mod** | ✅ 本期（T7） |
| IP-27 | `ui/ui_panel_basic.py` 的 `_merged_skeleton_logic_names()` | 复选框与**清除缓存按钮**共用同一来源（`(WWMI, EFMI, ZZMI, HIMI)`），结构上杜绝「开关可见但清除按钮不可用」的漂移 | ✅ 本期（T11） |
| IP-28 | `common/himi_skeleton.py` 的 `prepare(target_bares=...)` | 导出侧按**实际导出集合**重算 G1 的入口（T6 偏离 ③） | ✅ 本期（T6/T7） |
| IP-29 | `common/himi_skeleton.py` 的 `cache_only_plan` / `_himi_cache_intact_without_dump` | 导入侧 cache-only（dump 消失 + 缓存完好 ⇒ 不重算不写盘）；带 `target_bares` 时**拒绝** cache-only | ✅ 本期（T13） |

---

## 7. 导出/运行时阶段的可交付规格

### 7.1 资源声明（INI 形态，逐字可照写）

```ini
[ResourceHI3MergedSkeleton_<role_key>]
type = RWBuffer
format = R32G32B32A32_FLOAT
array = <3 × 槽位数>
bind_flags = shader_resource unordered_access
filename = Meshes/<role_key>-MergedSkeleton.buf
```

- **必须是 typed（带 `format`）**：游戏 deform VS 按 typed 索引读骨架，声明成结构化会让「视图元素尺寸(16) vs 声明步长(48)」不匹配，落 D3D11 未定义区（跨显卡差异根因，见 §7.4）。
- **`bind_flags` 必须显式写 `shader_resource unordered_access`**（2026-09-23 队长裁定，实现已按此落地）：typed `RWBuffer` 缺 `bind_flags` 时 **SRV 创建会静默失败并绑 NULL**，deform 读到全零（项目 key 记忆 2026-09-22 的 ZZMI 实机验证结论）。与 ZZMI 声明逐项相同可避免视图创建路径分叉。
  - **UAV 那一半本期没有写入方**（HIMI 的骨架是静态磁盘数据、无 CS、无 attach 段）：`unordered_access` 是**冗余但安全**的——它不改变 SRV 的读取语义，只是让资源描述与 ZZMI 实机验证过的形态完全一致。这是本仓的**新组合**（ZZMI 的 `RWBuffer` 一直有 attach CS 写入），已登记进未实机验证清单（§11 V8）。
  - **被否决的备选方案**：曾按「静态只读 ⇒ 只做 SRV ⇒ 不需要 `bind_flags`」写成 `type = Buffer` 且省略 `bind_flags`。**否决理由**：① 该写法在本仓没有实机验证先例，而缺 `bind_flags` 的失败模式是**静默**的（SRV 绑 NULL → 顶点塌到原点），只在两张显卡中的一张上暴露；② `type = Buffer` + `filename` 与「作为 `vs-t0` 被 deform VS typed 读」的组合同样未验证，换声明形态等于同时换两个未验证变量；③ 沿用 ZZMI 已实机验证的声明是**零成本**的（多一个 flag 不产生任何运行时副作用）。**记录在案、不得静默删除**（见 `reports/himi-merge/10-integration.md` §4 与 `05-decisions.md` §3.13）。
- **`array = 3 × 槽位数`**（不是槽位数）：因为 typed `R32G32B32A32_FLOAT` 的元素是 16 B，而每根骨是 48 B = 3 个元素。
- 二进制 `filename` 指向 mod 的 `Meshes/` 目录，与现有 VB/IB 资源同一路径体系（`ui/universal/unity.py:143-164`）。**唯一来源**：`common/himi_merged_runtime.py` 的 `HIMIResourceDecl`（INI 与 HLSL 由同一对象渲染，避免两边各写一份）。

### 7.2 HLSL 索引方式（游戏 deform VS 侧，不可改）

```hlsl
// 游戏 deform VS（10905ed856e6b621）读 palette 的形态（推断 + 待验证 V2）
Buffer<float4> palette : register(t0);
float4 r0 = palette[bone * 3u + 0u];
float4 r1 = palette[bone * 3u + 1u];
float4 r2 = palette[bone * 3u + 2u];
```

- 合并骨架必须**逐元素与 palette 同构**：槽位 `s` 的第 `m` 个 float4 放在元素 `s*3 + m`。
- `bone` = `BLENDINDICES` 的值 = **全局槽位号**（导出侧保证）。
- **禁止**在合并骨架资源上加 `stride = 48`（会把 `StructureByteStride` 置 48 并改变视图语义；palette 侧的同类教训见 `zzmi_merged_skeleton_attach.hlsl:33-34`）。

### 7.3 跨显卡不变量（三条必须同时满足）

| # | 不变量 | 理由 |
|---|---|---|
| A1 | 合并骨架资源 = **typed `RWBuffer` + `format = R32G32B32A32_FLOAT` + `array = 3 × slots` + `bind_flags = shader_resource unordered_access`** | 与游戏 deform VS 的 typed 索引精确匹配；结构化声明会落未定义区（AMD 按 SBS 取址 ⇒ 索引放大 3 倍、越界读返回精确 0）；缺 `bind_flags` ⇒ SRV 创建静默失败、绑 NULL |
| A2 | 二进制字节布局 = 槽位 `i` 在偏移 `i*48`，3×4 行主序 | 与 dump palette 逐字节同构（可 bitwise 比对校验） |
| A3 | **不得**给该资源加 `stride` | 加 `stride` 会改 `StructureByteStride`（声明 48 B vs 视图 16 B）⇒ 落未定义区 |
| A4 | 任何 **palette 形态**的资源必须 `type = Buffer` + `stride = 48` 且**绝不允许 `format`**（本期不生成 palette 资源，只保留形状守卫） | 加 `format` 会把 `StructureByteStride` 改成 16、`ByteWidth` 缩到 `16 × vg_count`，而 `copy vs-t0` 要拷 `48 × vg_count` 字节 ⇒ 溢出（ZZMI 实机教训） |

> A1/A2/A3 与 ZZMI 的跨显卡不变量同源（项目 key 记忆 2026-09-22）。**A1 的方向性更正**：设计初稿曾写「静态只读 ⇒ 不需要 `bind_flags`」，已按队长裁定更正为与实现一致（理由与被否决备选见 §7.1）。
> A1/A2/A3/A4 四条由 `common/himi_merged_runtime.py` 的 `validate_resource_decl` 逐条校验，四类破坏性声明会被显式拒绝（`tests/test_himi_merged_runtime.py` 锁死）。
> **待验证 V2**：HIMI 的 deform VS 究竟按 `palette[3b+m]` 的 typed 索引读，还是按结构化读。本次**无法**静态确认（HI3 `d3dx.ini` `cache_shaders=0`、`ShaderFixes/` 为空、`ShaderCache/10905ed856e6b621-vs_regex.dat` 只有 20 B 匹配元数据，无 DXBC）。**强旁证**：palette 在磁盘上就是 3×float4/骨（`start=0` 时 468 个行范数全部 = 1.6，`start=1/2` 时 410/459 行偏离 ⇒ 唯一对齐偏移是 0）。**验证动作**：在 HI3 `d3dx.ini` 打开 shader dump 后核对 `dcl_resource` 与索引表达式。

### 7.4 deform 重放块规格（§7.4 G1 门控 + VB 段形态）

**G1（硬门控，必须先过）**：把某个 deform pass 的 `vs-t0` 重绑到合并骨架，等价于**替该 pass 的全部 render 消费者**改用全局索引骨架。因此：

> **G1（pass 级，2026-09-23 T12 升级）**：仅当该 deform pass 的 **全部 consumer 家族**都已完整落在本次合并目标集合里时，才允许对该 pass 做重绑；否则该 pass 的**所有**组件（**哪怕它自己那个家族是完整的**）**一律不参与合并**（保持原版渲染），并打印显式诊断。
> 判据 = `family_safe and pass_safe`（`common/himi_skeleton.py:2527`）；家族级不完整 ⇒ `reason = deform_family_incomplete`；**同 pass 被其它家族拖累** ⇒ `reason = deform_pass_family_incomplete`，明细进 `metadata.DeformGateBlockedPasses`（`DeformGateMode = "pass"`）。
> 依据：§2.8 实测（SO 目标只被同一 IB 家族的 render draw 消费 39/39；家族内 SO 唯一 18/18）。门控可放行 18/39 个 pass = 全部已 join 的 pass。

**G1 的两条设计边界（T8 独立核出，必须写明以免后来者误判门控能力）**：

1. **`family_members()` 只遍历「工作空间里已落盘」的组件**（`common/himi_skeleton.py:466-477`），因此**看不到「从未落盘的家族成员」**——此时会判该家族 `safe=True`。这是**有意边界且方向安全**：放行的前提是「本次导出集合覆盖了工作空间里该家族的全部已落盘成员」，即导出侧自己不会切掉同家族兄弟。门控**不能**覆盖「工作空间外部的缺失」（那种缺失对 dump 侧同样不可见）。
2. **家族级拒绝时 `DeformGateBlockedPasses` 仍会记录该 pass**：它只是**诊断明细**，`reason` 仍按「家族自身是否 safe」正确选择（家族级 vs pass 级），**不改变任何写回内容**。

**G1 的导出侧重算入口**：`HIMISkeletonMergeHelper.prepare(target_bares=...)`（T6 偏离 ③）——`target_bares` 覆盖「合并目标集合」，导入侧缺省 = 全部已落盘组件，导出侧传入**本次实际要导出的组件**，因此「本次不导出同家族兄弟」会被正确判为不完整。导出侧经 `prepare_gate_oracle`（`common/himi_merged_runtime.py:2430`）消费该结论，`gate_source=prepare`；dump 不可用时退化为 `gate_source=cache-only`（见 §7.4 D3）。

**VB 段形态（在 Blend 类别的 `TextureOverride` 内，对照 `ui/universal/unity.py:58-61`）**：

```ini
[TextureOverride_VB_<draw_ib>_<alias>_Blend]
hash = <CategoryHash.Blend>
vb1 = Resource<draw_ib>Blend            ; ← 含全局索引的 mod Blend 顶点缓冲
vs-t0 = ResourceHI3MergedSkeleton_<role_key>   ; ← 唯一的新增语句
handling = skip
draw = <deform pass 的 Draw(VertexCount)>, 0   ; ← 注意：不是 draw_number
```

三条必须遵守：

1. **`draw` 的顶点数 = 该 deform pass 的 `Draw(VertexCount)` 的家族分片之和**（T7 偏离 ②），**不是** `drawib_model.draw_number`（渲染用）。实测两者不同（例 135739/`000035`：deform `VertexCount = 3377`，组件 `Blend.buf` 也是 3377 行；而渲染 `draw_number` 是该组件的导出顶点数）。同一 IB 家族的多个 bare 共享同一 SO 输出（分片区间首尾相接），因此该 pass 的行数 = **家族内各分片 `OriginalVertexCount` 之和**。**写错 ⇒ SO 只写前 N 行，其余行保留上一帧/未初始化内容 ⇒ 模型残缺。**
   > ⚠️ **t48/t49 更正（2026-09-25；**这正是本轮实机 blocker 的成因**）**：上述「家族分片之和」口径**已被实机推翻**，且它本身就是 blocker。真机 dump 实测：每家族**只有 1 个** deform pass（`StartVertexLocation: 0`），游戏真实 deform draw = **单个分片/LOD** 的数量（`2c706fbd` = **19895**、`694025c6` = 3363、`70a4da3e` = 3789、`b35cfc6c` = 1816、`def0c0ae` = 1496），而**同族每个成员的 `OriginalVertexCount` 完全相同**（它们是分片/LOD 变体：`5×19895`、`3×3363`、`2×1496`）⇒ 求和**必然**给出 N 倍。**新口径（已落地，t48）**：`draw` 的**唯一权威 = 模组自己这次上传的顶点数**（= `drawib_model.draw_number` = 产物 `override_vertex_count` = `Position.buf` 行数）；家族 Σ **只用于诊断打印**，且**只报警、不夹取**（静默夹取会让离线护栏永远看不到越界值）。旧口径下五族写出 `99475 / 10089 / 3789 / 1816 / 2992`，对上传行数 `31066 / 9 / 3 / 3 / 6` 越界 **3.2× / 1121× / 1263× / 605× / 499×**（其中 4 个家族只有站位几何）。**但注意：量纲修好 ≠ 实机修好**（见 §13.11「被推翻的假设①」）。
   **~~安全夹取~~ ⇒ 导出顶点数无条件权威（t48 反转）**：`resolve_deform_draw_count()`（`common/himi_merged_runtime.py:2168`）**已反转方向** —— `exported > 0` **无条件采用**（`source = "exported"`），家族 Σ 只进 `note`；仅当 `exported == 0`（不可得）才退回 Σ（`family-sum-fallback`）。**原「取较大值」方向是错的**：越界写会把**角色级 SO 池**写爆（t47 实测多写 68409 行），而「少写」只会让尾行保留上一帧 —— 两害相权，**上传行数才是那个物理量**。实测数据上两者相等（`04` §1：34/34 组件 `vertex_count_blend == vertex_count_deform`）⇒ 旧口径在**取证数据**上是空操作，**只在真实导出上炸**。
2. **`vs-t0` 重绑必须在同一个 VB 段内**（与 `vb1` 一起），保证重绑与 draw 之间没有别的 pass 插进来。
3. **非合并路径必须保留 `handling = skip` + `draw = <draw_number>, 0`**（对照 `zzmi.py:4083-4094` 的回归教训）——即：`draw` 的值在合并/非合并两条路径下**不同**，实现时必须分支。

**D3 —— dump 不可用时的门控退化（T7 偏离 ③）**：`load_workspace_plan`（`common/himi_merged_runtime.py:2927`）在 dump 缺失时走 `gate_source=cache-only`，**不做家族/pass 重算**（重算需要 dump）；仅当「本次导出覆盖了**全部**已落盘带数据部件」时沿用缓存判定，否则**保守地全部不重绑**（不替别人换骨架）。带 `target_bares` 的请求**必须拒绝 cache-only**（`gate_source='cache-only-unavailable'`）——否则「权威可用但放行 0 个」会被导出侧当成判定，使保守退路静默失效。

> **未验证项（R3）**：`vb1` 覆盖 + `vs-t0` 重绑在 **非索引 `Draw`** 上的实际行为未经实机验证（ZZMI 的先例是同形态：`zzmi_merged_skeleton_attach.hlsl` 就是给 deform pass 用的）。验收标准 §9 的第 6 条要求游戏内实测。

### 7.5 顶点顺序 / 行号不变量（合并路径必须保持）

| 不变量 | 内容 | 依据 |
|---|---|---|
| O1 | deform pass 的 `Draw(VertexCount)` 保持原值 | §7.4 第 1 条 |
| O2 | mod 的 Blend 顶点缓冲行数 == `Draw(VertexCount)` | `04` §1（`vertex_count_blend` 与 `vertex_count_deform` 逐组件相等 34/34） |
| O3 | mod 的 Blend 行 `i` 必须对应 SO 输出行 `i`（即原始顶点顺序） | **推断**：HIMI 的现有非合并流程已经依赖这条（mod 的 Position/Blend 被 deform pass 消费、输出写进游戏 SO、渲染 IB 按原始顺序索引），所以它必然成立，否则现有 HIMI mod 早就不显示。**待验证 V3**：导出侧 `common/obj_buffer_helper.py:825-956` 的顶点顺序是「loop 首次出现序」（`unique_first_indices_insertion`），不是「原始顶点号序」；需要用真实工作空间做一次逐顶点比对（§9.2 A5）。 |
| O4 | `override_vertex_count` 保持 `draw_number`（不按合并后行数改写） | SO 就地写、行数不变（D07） |
| O5 | 合并骨架不参与导出 −90°X 旋转（它直接取自 dump 字节） | D12 |

### 7.6 不引入的资源/段落（明确清单，防过度实现）

`ResourceZZPalette_*` / `ResourceZZVgMap_*` / `ResourceZZRedirectSO_*` / `ResourceZZMergedSkeleton_*` / `ResourceZZPoseKeySrc` / `ResourceZZDelayedPalette_*` / `[PoolZZMISlotOfKey_*]` / `[PoolZZMIG_Taken_*]` / `CustomShaderZZMIMergedSkeletonAttach_*` / `CustomShaderZZMISkin_*` / `[Constants]` 的 occ/seen/prev/pose_key / `[Present]` 的任何语句 —— **HIMI 全部不生成**。

**D4 —— HIMI 本期不生成 palette 资源（T7 偏离 ④）**：本方案不需要 `copy vs-t0` 的逐帧捕获，因此**不生成**任何 `ResourceHI3Palette_*`。但「palette 形态」的**形状守卫**保留并用测试锁死：任何 palette 形态资源必须 `type = Buffer` + `stride = 48` 且**绝不允许 `format`**（`common/himi_merged_runtime.py` 的 `PALETTE_RESOURCE_INI_TYPE` / `PALETTE_FORBIDDEN_INI_KEYS` + `build_palette_decl` / `validate_resource_decl`）。**理由**：加 `format` 会把 `StructureByteStride` 改成 16、`ByteWidth` 缩到 `16 × vg_count`，而 `copy vs-t0` 要拷 `48 × vg_count` 字节 ⇒ 溢出（ZZMI 实机教训）。守住这条形状，未来若要重新引入 palette 捕获通道就不会踩同一个坑。

---

### 7.7 站位（stub / 占位小三角）机制 —— **D09 重新裁定（基于 `13-stub-anatomy.md` 取证）**

> **本节是对设计冻结期 D09「整体不移植」裁定的正面更正**（2026-09-23 发起，2026-09-24 按 T19 取证重新裁定）。
> **不得静默改写**：D09 原文措辞与更正理由都保留在 §3.2、§1.3 与 §13.5，便于追溯。
> **本节的结构依据 = `reports/himi-merge/13-stub-anatomy.md`（T19，449 行 / 240 处 `文件:行号`）**：T19 只取证 ZZMI/EFMI 事实、不下 HIMI 判定；本节是它的 HIMI 侧裁定。

#### 7.7.1 D09 为什么错 —— 站位承载**两条独立作用**，而 D09 的理由只覆盖了 A

T19 §0/§4.3 把站位机制拆成**两条彼此独立的作用**（代码把它们耦合在同一个「注入占位小三角」动作里）：

| # | 作用 | 依赖什么 | 缺席部件不插桩的后果 | T19 依据 |
|---|---|---|---|---|
| **A** | **让缺席部件的 deform pass 在 mod 里跑起来**，把该部件当帧 palette 送进合并骨架 | 合并骨架由 attach CS **逐 deform pass 现场填充** | 该部件骨骼矩阵**永远进不了** MergedSkeleton ⇒ 引用其槽位的顶点读到全零 ⇒ 塌陷 | `13` §0/§4.1；`ui/universal/zzmi.py:3449-3454, 3357-3363, 5563-5565, 3037-3046` |
| **B** | **抑制原版 draw，防重影** | IB override 段的 `handling = skip` | 游戏按原版把该部件**再画一遍**，与已合并进别人的几何**重影** | `13` §0/§4.2；`ui/universal/zzmi.py:5785-5787` |

**D09 的原文推理**（§3.2 / §13.5）：从「HIMI 的『合并』只体现在顶点组命名空间与骨架槽位，**不改变任何 draw 的几何拓扑**」**直接跳到**「3 顶点 stub 不适用」。

**错在哪**：这条理由**只覆盖了作用 A 的推理**（A 依赖逐帧填充 ⇒ 静态骨架不需要 ⇒ 那一半判对了），**完全没有覆盖作用 B**。而 B 的必要性不来自「合并不改几何拓扑」，来自一条与骨架形态无关的运行时事实：

```text
合并让跨 IB 权重合法 ⇒ 用户自然会跨 IB join（把 A 的几何并进 B 的对象）
  ⇒ 被吸收部件的**原版 draw 在游戏里仍然存在**
  ⇒ 必须用站位让 mod 生成该部件的 IB override 段（handling = skip）把它吞掉
  ⇒ 否则重影
```

**关键的一跳**：HIMI 的 IB override 段**只为「场景里存在对象的部件」生成**（`ui/universal/unity.py:75-125`，逐 submesh 出段）。**部件缺席 ⇒ mod 里没有它的 IB 段 ⇒ `handling = skip` 不生效 ⇒ 原版那一笔照跑。** 这与 ZZMI 的机理**逐字同构**（`13` §4.2），且**与 SO 是否重定向、骨架是否静态全都无关**。

⇒ **「HIMI 无 RedirectSO」+「HIMI 静态骨架」只否定了 A 与重定向，否定不了 B。** D09 把三件独立的事绑在一句话里，结论越界到了 B 上。

#### 7.7.2 作用 B 在 HIMI 是否必要 —— **必要**，附用户可复现场景

**决定性问题的答案**：**HIMI 会出现「几何被合并进别的对象、原版那一笔仍跑」的状态**。理由分两层：

**第一层（HIMI 机制上的必然）**：合并让同一角色全部组件共用一套全局顶点组（§1.2 目标 2）⇒ 用户把组件 A 的几何 join 进组件 B 的对象后，**A 的顶点连同权重进了 B 的 mesh**；导出时 A 没有自己的对象 ⇒ mod **不为 A 生成任何 IB override 段** ⇒ 游戏提交的 A 的原版渲染 draw **没有任何东西拦它** ⇒ 画面里 A 的几何被画第二遍（B 的合并 IB 里已含 A 的面）。

**第二层（用户可复现的具体场景）**：

| # | 场景（用户操作） | 为什么触发 | 预期症状 |
|---|---|---|---|
| **S-1** | 在 Blender 里把同一 IB 家族的两个组件对象 **join 成一个**（例 咚！炽愿吉星 的 `a391eadc-69561-0` + `a391eadc-8685-69561`，实测同一家族 2 个组件、共享同一 SO 与同一 palette 池），只保留幸存名字的那个对象 | 被 join 掉的组件在场景里**不再有对象** ⇒ mod 不生成它的 IB 段 ⇒ 它的原版 draw 照跑；而它的面已被并入幸存对象 ⇒ 画两遍 | **重影**（该组件的面在画面里多画一份） |
| **S-2** | join 之后**删掉被并对象的空壳**（join 会自动这样做） | 同上 | 同上 |
| **S-3** | 只导出**部分**组件（局部导出），而某个未选中的组件**已被 join 走**（它的几何现在在选中对象里） | 未选中但**仍在场景里**的组件不触发站位（保持原版渲染是**预期行为**，见 §7.7.8）；但**被 join 走的那个**是「真正缺席」，它的原版 draw 仍在跑 | 同上（重影） |
| **S-4** | 把组件 A 的几何 join 进 B 后，又**把 A 的对象删掉**（先 join 再删） | 同 S-1 | 同上 |
| **不触发** | 用户**只是删掉** A 的对象、**没有**把它的几何 join 进任何对象 | A 的顶点根本没进 mod ⇒ 原版 A 照跑正是用户想要的 | 无（**不应插桩**，见 §7.7.5 的零引用情形） |

> **S-1 的实测支撑**：`04` §3.1 已实测「同一 IB 的多 bare 共享同一 SO 输出」（分片区间首尾相接），且 7 个 IB 家族被拆成多子网格（咚！炽愿吉星 `a391eadc` ×2、窈窕谍影 `7c0ec5d3` ×5、嗨♪爱愿妖精♥ `2c706fbd` ×5 等）⇒「同家族多组件 join 成一个」是**最常见的用户操作**，也正是站位要覆盖的场景。

**结论**：**作用 B 在 HIMI 必要**；**作用 A 无对应**（静态骨架不逐帧填充，§7.1）。

#### 7.7.3 与 ZZMI 三条规则 + 两条反例的逐条对照（HIMI 侧）

| ZZMI 规则（`13` §1） | HIMI 对应 | 依据/差异 |
|---|---|---|
| **规则① 部分缺失**：家族里有的成员在场景、有的不在 ⇒ 缺失的直接补站位，判据只看 **`scene_present`（场景真相）** | **照搬**（HIMI 同构） | HIMI 的 IB 段同样只为「场景里存在对象的 submesh」生成（`ui/universal/unity.py:89-101`）⇒ 缺失组件同样需要站位才能被压制。**必须用场景真相、不能用蓝图声明**（`13` §1.1 的 t80 §2.3 实测：把声明当 present 会让占位整段跳过 ⇒ 下游 Fatal）。**逐组件，不是「全组件」（T30 同步 F3）**：规则① 只把**家族级** `absorbed` 置真，随后逐成员处理。<br>**⚠️ 更正（t38 实施 / t40 登记，2026-09-24）：本节原先写的「逐成员过三道成员级护栏——无反查数据 / `VGMapDedupExcluded` / `exclusive` 为空」中，第三道（`exclusive`）在规则① 下是**错的**（原文保留以便追溯）。** 实测：HIMI「同族 100% 共享槽位」是常态 ⇒ 每个成员的 `exclusive` 恒空 ⇒ 把 `exclusive` 门控套到规则① 上会让**多成员家族永远不插桩**（用户实机：`2c706fbd` 5 分块只导出 1 个合并对象、另 4 个无占位 ⇒ 原版 draw 照跑 ⇒ 重影）。⇒ **规则① = ZZMI 逐字口径：家族部分在场 ⇒ 缺失成员直接全补占位，不做 `exclusive` / `used` / `VGMap` 门控；唯一保留的逐成员门是规则③（`VGMapDedupExcluded` 正交 —— ZZMI 也在同一层判）**（实现：`common/himi_skeleton.py` 的 `partial_family` 分支 + `_member_dedup_excluded()`）。「部分在场」的识别按 **IB 身份**（合并对象可能带副本后缀），见 §7.7.12.5。回归锚点：`tests/test_himi_stub_objects.py::StubRule1Tests::test_rule1_partial_family_stubs_every_missing_member`（用户形态 —— 多分块家族、`exclusive` 恒空、仅一个分块在场 ⇒ 其余分块**全部**补站位；逐条见 §7.7.12.5 与 §9.6 的 C11） |
| **规则② 整个 DrawIB 缺席**：看「自属声明段」内的全局骨骼 id 是否被现存对象**实际引用（权重>0）** | **照搬形状、重写判据本体；且判据是「逐组件」的（T30/t31 同步 F3）** | 见 §7.7.5（HIMI 的「自属声明段」等价物）。**不能直接套用 ZZMI 的段判据**（T19 §7 边界④）。**逐组件判定（与规则① 同款）**：家族整体缺席时，只有「**自身仍有专属槽位、且该槽位被现存对象实际引用**」的成员补站位；其余成员按**保守方向**不插桩并记入 `skipped_members`（逐成员循环 `common/himi_skeleton.py:1325-1365`，`skipped_members` 汇总在 `:1393`）。**口径锚点**：`tests/test_himi_stub_objects.py::StubRuleTests::test_st2_absent_family_with_own_slot_referenced_gets_all_members`（`:854`；t103 起断言 `stub_members == ["bbbb-1-0", "bbbb-2-0"]` —— 整族缺席且是唯一写方 ⇒ 两个成员都补站位，见本单元格末的更正与 §13.24）。<br>**锚点纪律（t33，按 t32 复核改正）**：原文的「该区间现在是 `_StubCase.setUp`」**不成立**（t32 复核时那段属 `_PlainDrawCall`，而 `_StubCase` 与其 `setUp` 在别处；t33 之后又整体漂移）⇒ **测试锚点不再引用方法内部行号**，只写 `Class::test` 符号名 + `def` 行号；行号由 `tests/test_himi_stub_objects.py::StubNailIntegrityTests::test_doc_anchor_references_resolve` 机械校验（改测试文件后跑一次即可定位漂移）。<br>**⚠️ 更正（t103，2026-10-25）：本单元格的「逐组件」判据在「整族缺席、且这些槽只有它在写」时不再生效** —— 规则④（`common/himi_skeleton.py:1326` 的 `writerless`）把这种家族**整族全成员**补站位（含 `exclusive` 恒空的同族兄弟），并**跳过** `foreign_used` 门（`common/himi_skeleton.py:1382`）。理由：这种槽没有别的写方，不补站位 = 运行期读零矩阵 ⇒ 顶点被拉向原点； 而用户口径（m01903）是「被合并进来的物体必须有 3 点站位」。判据依据见 §13.24；`exclusive` 口径仍只用于**非**唯一写方的家族。 |
| **规则③ `VGMapDedupExcluded=True` 正交**：即使被引用也不插桩（显式排除优先） | **照搬** | HIMI 已实现该字段的**读取**语义（§5.1、T12 F3：独占槽位 + 记入 `DedupExcludedComponents`）⇒ 站位判定直接复用同一标志，**不新增字段** |
| **反例① 无反查数据（json 无 VGMap）一律不插桩** | **照搬**（**只属规则②**，t40 F-3 限定） | 与 ZZMI `ui/universal/zzmi.py:886,1166-1167`、EFMI `efmi.py:327-328` 同构；HIMI 侧 = 该 bare 不在 `L`（未落盘）或 `VGMap` 为空 ⇒ 不插桩。**适用范围（t40 更正）**：规则① **不再**逐成员查 VGMap（缺失成员即使 json 缺失也直接补站位 —— ZZMI 逐字口径），该反例只在**规则②**（整族缺席、需要证据）里生效 |
| **反例② 判「不生成」的部件必须从模型列表原地摘除** | **照搬**（HIMI 同款风险） | ZZMI `ui/universal/zzmi.py:1021-1047`（t81 阻断④）：只 `continue` 不摘除 ⇒ 下游 `SubMeshModel` 按不存在的对象 `Fatal`。HIMI 的导出链路同样按 `submesh_model_list` 构建 ⇒ **必须原地摘除**，且只在正向判定缺席时删（安全边界） |

> **⚠️ 两个「反例②」不是同一件事（t40 登记，防误读）**：本表的**反例② = ZZMI 的「原地摘除」**（模型列表卫生）；而 `common/himi_skeleton.py` 的 docstring 与 §7.7.4 里的**反例② = `exclusive` 为空 ⇒ 不插桩**（成员级证据门）。两者编号撞车。**t38 之后**：`exclusive` 那道门**只属规则②**，规则① 已不看它（见上一行与 §7.7.12）；`exclusive` 本身在**多成员家族上恒空**（判据退化），t40 已裁定把规则② 的判据改为「**外来槽**」口径（§7.7.12）。

#### 7.7.4 HIMI 的「自属声明段」等价物 —— 为什么不能照搬 ZZMI，以及替代定义

**先看 ZZMI 的判据本体**（`13` §2.4，`ui/universal/zzmi.py:1163-1170`，4 行）：

```python
all_slots, own_segment_slots = self._load_drawib_vg_slots(draw_ib, workspace_root)
if not all_slots:            return False                              # 无反查数据
if not own_segment_slots:    return bool(all_slots & used_group_ids)   # 退化：宽松
return bool(own_segment_slots & used_group_ids)                        # 正常：只认自属段
```

其中「自属段」= `segment_start <= slot < segment_start + segment_count`，`segment_start = VGOffset`、`segment_count = VGCount`（`13` §2.1）。

**HIMI 为什么不能照搬**（T19 §7 边界④ + `09-review.md` F2）：

| 硬事实 | 后果 |
|---|---|
| `VGOffset` **恒 0**（HIMI 单骨架组，§3.2 D05） | 段基址恒 0，无「别的部件声明段」这一概念 |
| `VGCount = max(被引用局部槽下标) + 1`（§5.1，T6 偏离 ①），**不是**「自属槽数」 | `[0, VGCount)` 里同时含**真自属槽**、**空洞槽**、**借位槽** |
| 空洞槽的 `VGMap` 值被映到 `槽位数 + 局部下标`（§5.1） | 段内值域**既含真槽也含合成槽** |

⇒ 用 `[0, VGCount)` 当「自属段」会**退化成「全部槽位」** ⇒ 与 ZZMI 事故前那条被证伪的宽松口径**等价** ⇒ 对借位**零区分力**，必然重演 2026-09-16 的脸部误插。

**HIMI 的替代定义（= ZZMI 自属段的语义等价物，判据可测）**：

```text
「自属声明段」在 ZZMI 里的**语义**是：本部件 VGMap 的值里，**只有它自己会声明**的那些
（其余是去重借位到 canonical 拥有者段的）。

HIMI 没有段，但可以直接按语义定义：
  exclusive(bare) := own(bare) \ ( ∪_{c ∈ C(ib), c ≠ bare} own(c) )
  其中 own(x) := { v for v in VGMap(x).values() if v >= 0 }        # 该组件 VGMap 的**值域**
       C(ib)  := LOD0/DrawIB-Component.json 里该 IB 家族的全部候选成员（含未落盘）
```

**判据本体（HIMI 版，与 ZZMI 的 4 行一一对应）**：

```python
own_bare  = own(bare)                       # 本组件 VGMap 值域（≥0）
if not own_bare:                 return False                    # 无反查数据 ⇒ 不插桩
exclusive = own_bare - union_of_others      # 「自属段」等价物
if not exclusive:                return False                    # 退化：无专属槽 ⇒ 不插桩（见下）
used = { 数字顶点组名 | 场景现存对象的顶点 weight > 0 } - {站位对象}
return bool(exclusive & used)
```

**为什么这条与 ZZMI 的判据语义等价（逐条对照）**：

| 情形 | ZZMI（自属段口径） | HIMI（`exclusive` 口径） | 一致 |
|---|---|---|---|
| 真·几何被 join 走 | 自属段内的槽被引用 ⇒ 命中 | `exclusive` 里的槽被引用 ⇒ 命中 | ✅ |
| 只是共享/借位骨骼 | 借位值落在**别人段内** ⇒ 被排除 ⇒ 不命中 | 借位值在 `own(别人)` 里 ⇒ 被 `union_of_others` 排除 ⇒ 不命中 | ✅ |
| 全部骨骼都与别人共享 | `own_segment_slots` 为空 ⇒ 走退化分支 | `exclusive` 为空 ⇒ 走退化分支 | ✅ 形状一致 |
| 无反查数据 | `all_slots` 空 ⇒ False | `own_bare` 空 ⇒ False | ✅ |

**四个必备输入**（`13` §2.5 的清单，HIMI 侧落点）：

| 输入 | HIMI 来源 | 缺失时的行为 |
|---|---|---|
| `own(bare)` = VGMap 值域 | 子网格 json `VGMap`（已落盘组件） | 空 ⇒ `return False`（不插桩） |
| `own(c)` for 同家族其它成员 | 同家族的**全部落盘**组件 json（`C(ib) ∩ L`） | 同家族只有它一个已落盘 ⇒ `exclusive = own(bare)`（无借位可排除，判据退化为「自己的槽被别人引用」——仍是安全方向）。**t40 更正**：`exclusive` 判据已被裁定**替换**为「外来槽」口径（§7.7.12）——本行描述的是**旧判据**的退化行为，保留以便追溯 |
| `declared_by_present`（**t40 新增，第 5 个输入**） | **现存（present）对象的 `own` 并集**：由 `scene_present_bares` + 各组件 json 的 `VGMap` 值域求得（`decide_stub_injection` 内部可算，无需改导出侧签名） | **任一 present 对象的 `own` 不可判定 ⇒ 规则② 的「外来槽」路径 fail-closed 不参与 ⇒ 不插桩**（宁可不压）；IB 身份可判而成员身份不可判时，取该 IB **全部落盘成员 `own` 的并集**作上界（≤ 少压方向） |
| 现存对象**实际引用**的槽位集合 `used` | Blender 对象顶点组名，**`weight > 0`**、组名为数字、**排除站位对象自身** | 无法构造 ⇒ 不插桩（与 ZZMI 兼容兜底同款，`13` §1.1） |
| 家族成员名单 `C(ib)` | `LOD0/DrawIB-Component.json`（实测五角色均存在，**含未落盘成员**）。**t40 标注**：§7.7.9 / 本文旧文写的「家族数 8/8/7/6/9」是**多重集**正确、**角色顺序未标注**——按 咚！炽愿吉星 / 嗨♪爱愿妖精♥ / 愈生佑翎 / 窈窕谍影 / 镇×偃月叩晓 实测为 **6/9/8/7/8**（同一多重集 {6,7,8,8,9}） | 文件缺失 ⇒ 无法枚举 ⇒ **不插桩**（与 ZZMI `ui/universal/zzmi.py:893-898` 同） |

**两条退化情形 + 方向选择（与 ZZMI **相反**，理由写死）**：

| 退化情形 | ZZMI 的处置 | **HIMI 的处置** | 理由 |
|---|---|---|---|
| `exclusive` 为空（该部件的骨骼全都被别人也引用） | 退回**宽松**口径（宁松勿漏） | **不插桩**（保守）。**t40 更正（只属规则②，且判据已替换）**：t38 实测**多成员家族的每个成员 `exclusive` 恒空**（同族 100% 共享槽位）⇒ 该退化分支对它们**恒真**、判据**零区分力**（不是「保守」而是「结构性失效」）⇒ t40 裁定把规则② 的判据替换为「**外来槽**」口径（§7.7.12）：`own(m) ∩ (used \ declared_by_present) ≠ ∅`。规则① 不再使用本行（t38 已改为 ZZMI 逐字口径） | ZZMI 的宽松口径只多压（它的事故正是多压）；**HIMI 的宽松口径会「静默让用户原版部件消失」**——因为 HIMI 的站位不承载作用 A（无骨架填充收益），插桩的**唯一**效果就是 `handling = skip` 压掉原版 draw。用户把部件删掉、只保留共享骨骼时，宽松口径会把**用户想看到的原版部件**压没。**少压 = 可见重影（用户立刻发现并报告）；多压 = 静默部件消失（用户以为提取失败）** ⇒ 保守方向更安全 |
| `used` 不可得（轻量宿主/无 bpy） | 兼容兜底：宁可不插桩 | **不插桩** | 同 ZZMI（`13` §1.1）；信息不足时不得凭空插桩。**t40 补充**：`declared_by_present` 不可得时**同样不插桩**（同一 fail-closed 取向；见 §7.7.4 的输入表与 §7.7.12.3） |

> **这条「方向相反」是 HIMI 与 ZZMI 最实质的设计差异，必须写进实现注释与诊断**。ZZMI 的退化口径来自 EFMI 侧 t37 实机裁决（「收紧会让被合并部件不再生成占位、修坏合并骨骼」，`13` §5.2/§5.3），**那条裁决的前提是作用 A**（不插桩 ⇒ palette 进不了骨架 ⇒ 骨架被修坏）。**HIMI 没有作用 A，那条裁决的前提不成立 ⇒ 不得照搬。**

#### 7.7.5 站位对象的构造、标记与生命周期（照搬形状）

| 项 | HIMI 规格 | 依据 |
|---|---|---|
| 几何 | 3 顶点 1 三角面，坐标 `(0,0,0) / (1e-6,0,0) / (0,1e-6,0)` | `13` §3.1（`ui/universal/zzmi.py:1233-1236`） |
| 标记 | 对象/网格带 `STUB` 类标记；导出结束后清理 | `13` §3.2/§3.4 |
| **标记的第一个消费点** | **吸收判定的证据里必须排除站位对象自己** | `13` §3.2（`zzmi.py:1181-1182`）——否则本轮刚建的站位会反过来成为后续判定证据 |
| 残留自愈 | **必须早于 present/`scene_present` 构造**（否则残留站位对象名恰好等于某真部件身份 ⇒ 该部件被误判「场景存在」⇒ 不插桩 ⇒ 随后对象被删 ⇒ Fatal） | `13` §3.3（`zzmi.py:904-906`，t80 §2.3 同源） |
| 清理闭合 | `__init__` 的 `except` + `export()` 的 `finally` + `export_buffers_only()` 的 `finally` **三处** | `13` §3.4（`zzmi.py:481-485, 6074-6078, 6080-6085`） |
| 摘除判据 | 用「登记 id」与「stub 标记」两个条件（EFMI 的更严口径是只摘「登记过**且**带标记」，可作为加固选项） | `13` §3.4/§5.1 |
| **权重组名的取值** | **不需要为站位新增任何槽位**。**t45 实证的机制链（逐环节，替换 t44 的「机制归属」更正）**：① `blender_create_stub_object` 只建**唯一**一个权重组（`common/himi_skeleton.py:1712`），组名 = 常量 `HIMI_STUB_SLOT_GROUP_NAME = "0"`（`:79`）；② 导出侧 `get_blendweights_blendindices_v3`（`common/obj_buffer_helper.py:679-680` 的 else 分支 —— HIMI 走这里）写进 BLENDINDICES 的是**组索引**，单组 ⇒ **索引恒 0**；③ 产物实测 `distinct_slots = [0]`、decoded 全 0、packed 48 B ⇒ **全局槽 0**。**`_prepare_merged_skeleton_vertex_groups` 与本结论无关**：它对 HIMI **不运行**（`run_merged_skeleton_preprocess` 门只认 EFMI/ZZMI ⇒ 运行时求值各分支皆 False），且对单组对象本来就是**恒等操作** ⇒ 跑不跑结果相同。**核心事实（三条）**：ⓐ **结果正确** —— 站位件**确实**落在全局槽 0；ⓑ 槽 0 是 `2c706fbd` 家族**既有**槽（t45 实测家族 `own = [0,183]`、密集从 0；角色 `SlotCount = 343`）⇒ 加不加站位件的 0，命名空间判定都是 `global`（184 值）⇒ **不新增槽、不新增值、不影响重绑**；ⓒ 真正的链**不是**那个预处理 | `13` §3.2/§5.1 的同构做法是「json VGMap 首个非负值」；HIMI 因单组 + 固定常量命名而**天然**落到槽 0，**不引入新槽** |
| **不得混淆的常量** | `ZZMI_STUB_PREFIX_ROWS = 3` 描述的是「重定向计划里的**前缀行数**」，与「站位对象有多少顶点」在代码上**独立、无绑定断言**（数值相同是语义巧合） | T19 §7 边界②（`13` §3.5）。**HIMI 无重定向 ⇒ 不引入这个常量**，只在站位对象的几何上写死 3 个顶点 |
| **站位是否进合并骨架** | HIMI：**站位对象不进**（不写 `VGMap`、不占槽位、不落 palette 缓存、不进 `target_bares`）。**注意区分（2026-09-24 T30，见 §7.7.11）**：**被站位的 bare 必须进 `target_bares`** ——「不进 `T`」只描述那个 3 顶点合成对象，不描述被它代表的已落盘 bare | T19 §7 边界① 指出 ZZMI 的唯一门控是 `submesh_model.vg_count > 0` 且**站位 `vg_count` 的具体取值未实测**——**HIMI 不需要这条链**（无作用 A），故不复制该门控，也不依赖那个未实测值 |

#### 7.7.6 站位与 G1 的交互（逐条裁定）

**先固定四个集合，避免把不同用途混为一谈**：

| 记号 | 含义 | 来源 |
|---|---|---|
| `L` | 工作空间**已落盘**组件（有 json 与 .buf） | `_discover_landed_components` |
| `C(ib)` | `LOD0/DrawIB-Component.json` 里该 IB 家族的**全部候选成员**（含从未落盘） | 实测五角色该文件均存在（家族数 8/8/7/6/9） |
| `S` | 本次导出**真正要生成**的部件（**场景里存在对象的**部件） | 导出侧蓝图 + **场景真相**（不是声明） |
| `T` | **合并目标集合**（`target_bares`）= `S ∩ L` 里带合并数据、且骨架摘要校验通过者 | `common/himi_merged_runtime.py:1604-1683` |
| `P` | 站位组件集合（为抑制原版 draw 而注入的小三角） | 本节新引入，**待实现** |

| # | 问题 | 裁定 | 理由 |
|---|---|---|---|
| **G1-a** | 占位部件是否算「该 IB 家族完整」的覆盖？ | **要区分两个不同的东西（2026-09-24 更正）**：<br>① **站位对象**（那个 3 顶点小三角）**不算**——它没有 json、没有 `VGMap`、不在 `L` 里；<br>② **被站位的 bare**（一个**已落盘、有 `VGMap`、有真实骨骼**的组件）**算已覆盖**。 | 原裁定的①**正确**（定义域事实：站位对象不在 `L`，连 `L(ib) ⊆ T` 的定义域都进不去）；但原裁定把①外推成「所有与站位有关的身份都不算覆盖」——**这是错的**，它让站位机制在唯一能生效的场景里必然中止导出（§7.7.11 的反证链）。② 的理由见 §7.7.11「被站位覆盖的成员算不算已覆盖」 |
| **G1-b** | 占位是否进入 `target_bares`？ | **同样区分两个不同的东西（2026-09-24 更正）**：<br>① **站位对象**（3 顶点小三角）**不进** `T`——它没有 `VGMap`、没有顶点数据、没有 palette 缓存，进 `T` 只会污染槽位统计与骨架摘要校验；<br>② **被站位的 bare**（已落盘组件）**必须进** `T`（`export_bares` / `target_bares`），`is_stub` 只作**只读标注**（`himi_stub_bares()`），**不参与过滤**；`target_bares` 的**槽位统计 / 骨架摘要校验仍不受影响** —— **站位不写 `VGMap`、不占槽位**（被站位的 bare 的 `VGMap` 与骨骼本来就在导入期参与了骨架构建，导出侧进 `T` 只是登记「它的几何已被覆盖」，不触发任何骨架重建，见 §7.7.11.4）。 | 原裁定的①**正确**；② **错误且致命**：把被站位的 bare 排除出 `T` ⇒ 被吸收成员永远 `missing` ⇒ 家族永远不 `safe` ⇒ 契约 `level=error` ⇒ `Fatal` **中止导出**。而「站位被注入」的**唯一**场景就是「该家族有成员被吸收」，两者恰好重合 ⇒ 功能永远不可能生效（§7.7.11 反证链，已实测复现） |
| **G1-c** | G1 的 **pass 级**判定要不要把占位算作家族成员？ | **分两层**（原裁定**正确、不变**）：<br>① **家族完整性层**：站位**对象**不算（它不在 `L`）；但**被站位的 bare 算**（同 G1-a②）；<br>② **pass 消费者层**：**不豁免**——若 dump 显示该 pass 被某家族消费，该家族仍必须安全，**无论该家族里有多少站位** | 第 ② 层是既有语义（重绑按 pass 生效，替该 pass 的**全部** render 消费者换骨架）。**站位不改变「谁消费这个 pass」**——被吸收部件的原版 draw 仍在消费它（这正是站位要压掉的那一笔）。若让站位豁免 pass 判定 ⇒ 站位件所在家族不完整时错误放行 ⇒ 那一族的 deform 读到错骨架 |
| **G1-d** | 一个家族里既有真几何又有占位时，重绑决策是什么？ | **三步固定顺序**（原裁定**正确、不变**）：<br>① 家族完整性 = `L(ib) ⊆ T`？（被站位的 bare 在 `T` 里 ⇒ 该家族完整）<br>② pass 级 = 该 pass 上**全部消费者家族**都安全？<br>③ 仅当 ①② 都过才重绑 `vs-t0`。**站位的注入与「该家族能否重绑」无关**——它只决定「被吸收部件的原版 draw 要不要被压掉」 | 把两件事解耦：**能不能重绑**（几何/骨架问题）与**要不要压原版 draw**（重影问题）是独立的。混在一起会出现两种坏结果：站位挡住本该放行的重绑，或站位让本该拒绝的重绑放行。**G1-b 的原口径正是第一种坏结果的实例** |
| **G1-e** | **执行顺序** | 先定 `S`（含被站位的 bare）→ 过 G1（家族级 + pass 级）→ **最后**按 `absorbed ∧ 合并骨架已生成` 注入站位**对象** | 站位**对象**不参与 G1 ⇒ 必须排在 G1 **之后**，否则出现「站位影响判定」的循环。注意：**站位 bare 的加入不是「站位参与 G1」，而是「该成员的几何已被覆盖」这一事实的登记**——它由 §7.7.11 的语义判定给出，不是由站位注入动作给出。**实现注记（T30 同步 F6；t31 补「等价于最后注入」的措辞）**：实现里注入发生在 `ui/universal/himi.py` 的 `__init__`（**早于** G1 重算），**字面顺序与本行相反**；**语义等价于「最后注入」**——因为站位**对象**不参与**任何** G1 判据（**G1-a/b/c**：G1-a 定义域 / G1-b 目标集合 / G1-c pass 消费者层，见 §7.7.6 与 §7.7.11），且 `export_bares` 的构造不读注入结果（§7.7.11.4）；`tests/test_himi_stub_objects.py::StubInjectionOrderTests` 把「注入必须早于基类组装模型」钉死 |

**是否改变 G1 的既有语义？——不改变（逐条兼容性）**：

| 已交付行为 | 是否受影响 | 说明 |
|---|---|---|
| G1 家族级判定（`family_safe`） | **不受影响** | 判据仍是 `L(ib) ⊆ T`；站位不在 `L` |
| G1 pass 级判定（`pass_safe`） | **不受影响** | 站位**对象**不进 pass 的消费者家族集合；`family_members()` 仍只遍历 `L`（被站位的 bare 本来就在 `L` 里，与是否注入站位无关） |
| `target_bares` 语义与 `prepare_gate_oracle` | **不受影响**（判据与函数签名都不改） | **更正（2026-09-24 T30，见 §7.7.11）**：原写「站位不进 `T`」——只对**站位对象**成立；**被站位的 bare 必须进 `T`**（否则被吸收成员永远 `missing` ⇒ 家族永远不 `safe` ⇒ 契约 `level=error` ⇒ `Fatal` **中止导出**，§7.7.11.1 反证链）。本行的结论「不受影响」仍成立：变的只是**进入 `T` 的身份是谁**（是已落盘 bare，不是合成对象），`target_bares` 的语义与 `prepare_gate_oracle` 的签名都不动 |
| `VGMap` / 槽位分配 / 角色级骨架 | **不受影响** | 站位不写合并元数据（G1-b）；其权重组由桥创建为**唯一**组、组名常量 `"0"` ⇒ 索引 0（**不依赖**导出侧预处理 —— 见 §7.7.5 的 t45 机制链）；**t45 实测**：槽 0 是 `2c706fbd` 家族既有槽 ⇒ 不新增槽、不新增值，角色级 `SlotCount` / `Slots` 不含站位槽 |
| `metadata` 字段集 | **仅新增两个只读字段**（G1-b） | 不改既有字段语义 |
| 既有测试（**T30 起测基线 2743 passed / 42 skipped / 151 subtests**；本轮结束前并发任务 t31 新增 2 条文档一致性钉子 ⇒ **复测 2745**；设计冻结期为 2681） | **不应受影响** | 站位是**新增**的注入路径；未启用站位时行为与现在逐字节一致 |
| **当前交付的已知缺口** | 用户在实测中发现的正是这条 | **HIMI 当前没有站位 ⇒ 跨 IB join 后原版 draw 重影**（§7.7.2 S-1..S-4）。见 §11 V11 与 `10-integration.md` §4.2 I6 |

#### 7.7.7 站位与「快速局部导出」的关系（与 t20 的配合）

> **先划清 t20 的边界**：t20 修的是**另一个**问题——`prepare_gate_oracle` 把「子集重建的骨架摘要」当成陈旧证据 ⇒ 丢弃 G1 本已放行的结论 ⇒ 中止导出；且报错不显示原因、建议词诱导用户丢弃正常数据。**两件事不得混做**，但必须说清交互。

**两个**必须区分**的「未导出」（这是本节最易混的一点）**：

| 形态 | 判定 | 站位规则 | 游戏内行为 |
|---|---|---|---|
| **① 局部导出里未选中、但仍在场景中的部件** | 该部件**有对象** ⇒ 它在 `S` 之外，但**不是缺席** | **不触发任何站位规则** | **保持原版渲染——这是预期行为**（用户只想替换选中的部件）。**不得**因为「它没被导出」就给它插站位 |
| **② 真正缺席的部件**（被删 / 被 join 走） | 该部件**在场景里没有对象** | 走 §7.7.4 的 absorbed 判据 | 被吸收 ⇒ 插站位压原版防重影；零引用 ⇒ 不插桩、保留原版 |

| # | 问题 | 裁定 | 理由 |
|---|---|---|---|
| **S1** | 局部导出（`S ⊊ L`）时，未导出但**骨骼被引用**的 DrawIB 是否**自动**补站位？ | **不自动补**。局部导出**不产出合并骨架资源**（未生成 `ResourceHI3MergedSkeleton_*`）⇒ 站位压掉原版 draw 只会让部件**凭空消失**，不会消除任何重影 | 站位的唯一目的 = 抑制「原版 draw 与**合并后几何**重影」。局部导出不产出合并几何，前提不成立 |
| **S2** | 那什么情况下该补？ | **仅当「合并骨架资源被生成」∧「该部件几何确实被吸收」两个条件同时成立** | 与 §7.7.4 的 absorbed 判据联动 |
| **S3** | 与 t20 的修复怎么配合？ | **站位判定必须消费与 G1 **同源**的 `S`（= `target_bares` 的输入集合），不得自己另算一套集合**；t20 修好「覆盖完整家族的部分导出应可用」之后，`S` 会正确地包含该家族全部落盘成员 ⇒ 站位判定随之正确 | 若站位自己算集合，就会出现「G1 用一套、站位用另一套」的双口径 |
| **S4** | 站位注入会不会被 t20 的修复阻塞？ | **不会**：站位判定**不需要**骨架摘要校验（§7.7.4 的输入只有 `VGMap` + `used` + `C(ib)`），与「摘要陈旧」完全无关 | 这正是两件事可以分开做的原因 |
| **S5** | 与 t20 的**行为交互**（不混做） | t20 修的是「部分导出被误中止」；站位是「缺席部件被吸收后防重影」。**t20 修好后，局部导出会正常产出合并骨架**（这正是 S1/S2 的前提之一）⇒ **站位判定在 t20 修好之后才真正可用**；在 t20 修好之前，局部导出本身会被中止，站位无从谈起 | 两件事**顺序上有依赖**（S2 的前提依赖 t20 的修复），但**实现上互相独立**（S4） |
| **S6** | 诊断 | 站位注入/跳过都必须**显式打印**并进契约诊断，至少覆盖 ZZMI 的三条文案（`13` §1.2/§1.3/§1.4）：`几何已被合并 ⇒ 全组件补站位` / `本部件专属槽位未被引用 ⇒ 按用户意图不生成` / `已标记 VGMapDedupExcluded ⇒ 不生成站位`；**HIMI 另加一条**：`exclusive 为空 ⇒ 证据不可构造、保守不插桩（可能残留重影）`。**T30 同步 F3**：第一条是 **ZZMI 的**文案（ZZMI 在 absorbed 时对家族**全部**缺席成员补）；**HIMI 是逐组件**——同一个 `absorbed` 下，每个缺席成员仍要各自过三道成员级护栏（无反查数据 / 显式排除 / `exclusive` 为空，见 §7.7.3 规则① 的 T30 注记），因此 HIMI 的诊断必须**逐成员**打印「注入/跳过 + 原因」 | 这条链全是启发式判据，静默必然导致误压难排查（ZZMI 的事故就是靠游戏内实测才发现） |

#### 7.7.8 验收标准（可判定，含反例）

**A 类断言（五条正向 + 反例，全部可用合成 fixture 判定）**：

| ID | 断言 | 期望 | 反例（必须同时断言） |
|---|---|---|---|
| **ST1** | **被吸收 ⇒ 补站位**：家族成员 `A` 不在 `S`，`exclusive(A) ∩ used ≠ ∅` | 为 `A` 注入站位；`A` 的 IB override 段生成且 `handling = skip` | 反例：把 `exclusive(A)` 的槽从 `used` 里去掉 ⇒ **不**注入 |
| **ST2** | **零引用 ⇒ 不插桩**：`exclusive(A) ∩ used = ∅`（**t40 更正：判据已替换为「外来槽」口径 —— `own(A) ∩ (used \ declared_by_present) = ∅`，见 §7.7.12.2；原文保留以便追溯**） | 不注入；`A` 被摘出模型列表（**不得**留在 `ordered` 里造成下游 Fatal）。**逐组件判定（T30 同步 F3；t31 补口径锚点）**：同一家族里只有「有证据被现存对象吸收」的成员补站位，其余成员**保守不插桩**（`common/himi_skeleton.py:1325-1365` 的逐成员循环；`skipped_members` 汇总 `:1393`）。**适用范围（t40 更正）**：ST2 **只属规则②**（整族缺席、需要证据）；规则① 不再逐成员做证据门控（t38）。**口径锚点**：`tests/test_himi_stub_objects.py::StubRuleTests::test_st2_absent_family_with_own_slot_referenced_gets_all_members`（`:854`；t103 起断言整族两个成员都补站位 —— 见下面「⚠️ 更正（t103）」）。<br>**锚点纪律（t33）**：同 §7.7.3 规则② —— 只引 `Class::test` 符号名 + `def` 行号，方法内部行号一律作废（原 `478-482` / `699-738` 两处引用均已失效） | 反例：`used` 加入 `exclusive(A)` 的任一槽 ⇒ 注入（**t40 后**：反例需改为「把该槽从 present 对象的 `declared_by_present` 里去掉」才构成外来槽证据，见 §7.7.12.4 的 ST13/ST14a） |
| **ST3** | **借位 ⇒ 不插桩（最关键的反例）**：`A` 与同家族 `B` 共享骨骼（`own(A) ⊆ own(B)`，`exclusive(A) = ∅`）且 `B` 的对象引用了这些共享槽。**t40 更正两点**：① **只属规则②**（整族缺席 + 借位 —— 规则① 的「部分在场」前提与该事故形态不同，不适用本反例；t38 已把该用例重建为规则② 形态）；② 新判据下它**仍然成立且更强**：借位槽由 canonical 拥有者声明 ⇒ 落在 `declared_by_present` 里 ⇒ **永不算外来槽** ⇒ 判据层不可表达事故形态（§7.7.12.2 (b)） | **不注入**（不得重演 2026-09-16 脸部误插） | 反例：把共享骨骼改成 `A` 专属（`own(A) ∩ foreign_used ≠ ∅`）且被引用 ⇒ 注入（即 §7.7.12.4 的 ST14a 反向：把该槽从 present 的 `declared_by_present` 里去掉） |
| **ST4** | **`VGMapDedupExcluded=True` ⇒ 不插桩**：`A` 满足 ST1 的全部条件但带该标记 | **不注入**（显式排除优先于 absorbed，正交） | 反例：去掉该标记 ⇒ 注入 |
| **ST5** | **无 VGMap ⇒ 不插桩**：`A` 未落盘（不在 `L`）或 `VGMap` 为空 | 不注入（证据不可构造） | 反例：补齐 `VGMap` 且 `exclusive` 被引用 ⇒ 注入 |

**B 类断言（规则与结构）**：

| ID | 断言 |
|---|---|
| **ST6** | **规则① 部分缺失**：家族里既有场景内成员又有场景外成员 ⇒ **只对场景外成员**补站位；判据必须用**场景真相**而非蓝图声明（构造「声明了但场景无对象」的用例，断言仍补站位）。**t40 补强（t38 实施）**：规则① = **ZZMI 逐字口径** ⇒ 缺失成员**直接全补**，不做 `exclusive`/`used`/`VGMap` 门控；**唯一保留规则③**（`VGMapDedupExcluded`）；「部分在场」按 **IB 身份**识别（合并对象带 `_copy` 后缀）。锚点：`StubRule1Tests`（5 条） |
| **ST7** | **站位对象不影响 G1 判定**（不写成「站位不进 G1」以免与 G1-b 混淆）：注入站位后重跑 `prepare`/`load_workspace_plan` ⇒ `target_bares`、`family_safe`、`pass_safe`、`VGMap`、`SlotCount` **与注入前逐字节相同** |
| **ST8** | **站位不新增槽**：站位对象的权重组由 `blender_create_stub_object` 创建为**唯一**组（名 = 常量 `HIMI_STUB_SLOT_GROUP_NAME = "0"`）⇒ `index == 0`；导出 BLENDINDICES = **组索引** = 0（t45 实测 `distinct_slots = [0]`）；角色级骨架的 `SlotCount` 与 `Slots` 不含任何站位槽 |
| **ST9** | **站位对象不进吸收证据**：`used` 集合构造必须排除站位对象（否则本轮站位会成为后续判定证据 ⇒ 自证循环） |
| **ST10** | **残留自愈时序**：残留站位对象必须在 `scene_present` 构造**之前**被清理（构造「残留站位对象名 == 某真部件身份」的用例 ⇒ 断言该部件仍被正确判定为缺席） |
| **ST11** | **事务闭合**：`export()` / `export_buffers_only()` 正常与异常路径都必须清理站位对象与注入的 DrawCall；模型列表里不得残留 stub DrawCall |
| **ST12**（t107/t108 更正） | **普通模式不再整段跳过站位逻辑**：`import_merged_vgmap()` 关闭时**三条规则照常判定**（t107 先解禁规则①，t108 把规则②/④ 也放开）—— 部分在场的家族给缺失成员补站位（缺失组件的几何已被同一次导出的幸存兄弟接管，与是否产出合并骨架资源无关）；整族缺席、但槽位已被在场对象实际引用的家族，也必须由规则② 补站位压掉原版那一笔。只有 t106 的 writer 站位（`_himi_add_missing_writer_stubs`）与合并骨架数据前置门仍留在开关后面 —— 它们依赖合并骨架运行时本身。原口径「完全不调用、产物逐字节相同」已作废：它把「要不要产出合并骨架资源」和「该压掉的原版 draw」绑成一根开关，实测后果是两条漏压形态同时出现 —— ①同一 IB 的第二笔原版 draw（`4386@57792`）没有任何段接住；②整族被并进头发后的那个 IB（`1b62725d`）连判定都没走，原版几何与 mod 几何同时出现（两份重叠 + 一份没贴图 + 顶点槽错位串到别的物体）。钉子：`StubGateInteractionTests::test_st12_normal_mode_runs_rule1_only`、`StubRuleTests::test_st15_rule1_only_gates_rule2_and_writerless`、`StubEndToEndIniTests::test_st16_normal_mode_stubs_whole_absent_family_with_foreign_slot` |
| **ST16**（t108 新增） | **整族缺席也不许静默漏压**：普通模式（`import_merged_vgmap()` 关闭）下，场景里没有任何属于该 IB 的对象、但它的独占槽被在场对象实际引用（`own(member) ∩ foreign_used ≠ ∅`）⇒ 规则② 必须逐组件补站位；`decision.families[draw_ib]["decision"]` 不得再落成 `HIMI_STUB_REASON_RULE_GATED`。钉子：`StubEndToEndIniTests::test_st16_normal_mode_stubs_whole_absent_family_with_foreign_slot` |
| **ST13** | **规则② 外来槽正例（t40 新增，待实现）**：整族缺席 + 全成员 `exclusive = ∅`（族内 100% 共享）+ present 对象引用 `X` 的槽且 `own(P) ∩ own(X) = ∅` ⇒ 相应成员**逐组件**补站位；反例：`own(P)` 不可判定 ⇒ 不插桩（fail-closed）—— 详见 §7.7.12.4 |
| **ST14a/b** | **规则② 事故形态与 fail-closed 反例（t40 新增，待实现）**：① `X` 的槽**也被 present 声明**（canonical 共享）⇒ **不插桩**（不得重演 2026-09-16；反例 = 把该槽从 present 的 `own` 里去掉 ⇒ 插桩）；② `declared_by_present` 不可得 ⇒ 不插桩 —— 详见 §7.7.12.4 |

**C 类：历史教训的两个方向（都已被实测过，两侧都要有测试锚点）**：

| 方向 | 事故 | 可复现测试锚点（本仓） | HIMI 的取向 |
|---|---|---|---|
| **过松**（判据把借位当吸收证据） | ZZMI 2026-09-16 叶瞬光01 **脸部 `c209c22b-45087-0` 被误插占位**（`13` §2.3） | `tests/test_zzmi_merged_skeleton_ini.py:2444-2471`（`test_no_stub_when_only_dedup_borrowed_slots_are_referenced`） | **必须避开** ⇒ HIMI 的 `exclusive` 口径（§7.7.4） |
| **过紧**（判据要求「真·未注册槽」） | EFMI `43d5f62`（2026-09-04）收紧为「未注册槽才算证据」，**t37 实机确认导致被合并 IB 不再生成占位、修坏合并骨骼**，裁决撤销（`13` §5.2/§5.3） | `tests/test_efmi_stub_lod.py:375-389`（`test_shared_slot_from_matrix_dedup_reference_absorbs_missing_drawib`） | **前提不适用**：那条裁决的前提是**作用 A**（不插桩 ⇒ palette 进不了骨架 ⇒ 骨架被修坏）。HIMI 无作用 A ⇒ 不得照搬 ⇒ 退化情形取**保守（不插桩）**（§7.7.4） |

> **两侧都有事故 + 前提不同** ⇒ HIMI 的取向必须**同时写明**「为什么避开过松」与「为什么这里可以取保守（而不照搬 EFMI 的宁松勿漏）」。

> **⚠️ ST 编号在「计划书」与「测试文件」之间并不一一对应（T30 复核发现，登记为追溯性缺陷）**：`tests/test_himi_stub_objects.py` 抬头自己定义了一套 ST1..ST12（`:6-24`），其中 **ST2 与 ST4 的含义与本表的 ST2/ST4 互换、ST6/ST7 指的是 G1-a/G1-b 而不是本表的「规则① 部分缺失 / 站位不进 G1」**；只有 **ST8..ST12 两侧一致**。逐条对照表见 §9.6。**读测试时以测试文件自己的抬头为准，读规格时以本表为准**；改名/重编号已登记给后续任务（§13.7 待补 ③）。

#### 7.7.9 待验证项与登记

| ID | 内容 | 取证条件 |
|---|---|---|
| **V11** | 站位机制的**实机有效性**：1e-6 小三角在 HI3 下是否真的不可见、被吸收部件的原版 draw 是否真的被压掉 | 实机导入 + 导出 + 游戏内观察（与 V1/V2/V3/V8 同批） |
| **V12** | HIMI 的 `DrawIB-Component.json` 是否**总是**该角色全部候选（= 完整家族名单） | 已实测五角色均存在（家族数 8/8/7/6/9）且含未落盘成员；需在更多角色上复核。若某角色缺该文件 ⇒ 站位判定退化为「不可枚举家族成员 ⇒ 不插桩」 |
| **V13** | 站位件的 IB override 段（`drawindexed = 3, 0`）在 HIMI 的**非索引 deform** 语境下是否需要额外声明 | 实机观察（HIMI 的 deform 是 `Draw(VertexCount)`、渲染是 `DrawIndexed`；站位只需要渲染侧画出 3 顶点） |
| **V14** | 站位对象的权重组恒为槽 0 这一结论（由**桥创建为唯一组** + 组名常量 `HIMI_STUB_SLOT_GROUP_NAME = "0"` ⇒ 索引 0）在**多权重组**的边界下是否仍成立 | 合成用例：给站位对象人为加第二个权重组 ⇒ 断言要么组名仍连续合法、要么实现显式报错（不得静默产生非法组名）。**t45 实测补充**：HIMI **无**导出期自检 ⇒ 加第二组后实现不会报错（导出的索引会变成 0/1 之一），见 **T45-F4** |

#### 7.7.10 尊重 T19 §7 的四条边界（逐条声明不得误推）

| T19 边界 | 本节的处置 |
|---|---|
| ① 站位进合并骨架的唯一门控是 `submesh_model.vg_count > 0`，站位 `vg_count` 的具体取值**未实测** | **不复制该门控**：HIMI 无作用 A（不逐帧填充骨架），因此**不需要**站位进合并骨架。**更正（2026-09-24 T30，见 §7.7.11）**：不进 `T` 的是**站位对象**（无 json / 无 `VGMap` / 无几何数据）；**被站位的 bare 必须进 `T`**（否则家族门永远不放行 ⇒ 必然中止导出）。两者都不写 `VGMap`、不占槽位 ⇒ 仍**不依赖**那个未实测的 `vg_count` 取值 |
| ② `ZZMI_STUB_PREFIX_ROWS` 的「3」与站位顶点数在代码上**独立、无绑定断言** | **明确不混淆**：HIMI **无重定向 ⇒ 不引入该常量**（§7.7.5 末两行）；站位顶点数是几何事实（3 个坐标），与前缀行数无关 |
| ③ EFMI 的 present 构造**不比对场景真相**（只记录差异）⇒ **不得据此判定 EFMI 有同类风险** | **本节不对 EFMI 下任何风险判定**；只引用它的两条**历史裁决**（过紧方向，`13` §5.2）作为「另一侧事故」的证据，并写明那条裁决的前提（作用 A）在 HIMI 不成立 |
| ④ HIMI 的去重同样产生 canonical 借位，但 `VGCount`/`VGMap` 口径与 ZZMI 不同 ⇒ **不能直接套用「自属声明段」判据** | **已按此处理**：§7.7.4 定义 HIMI 自己的等价物（`exclusive(bare) = own(bare) \ ∪_{c≠bare} own(c)`）并给出「为什么与 ZZMI 语义等价」的逐条对照，**未套用段判据** |



#### 7.7.11 **G1-a/G1-b 的规格级矛盾与更正裁定（自相矛盾 / 必然中止导出 / 功能级死锁）**（2026-09-24，T30）

> **本节更正 §7.7.6 的 G1-a/G1-b**。原措辞**保留未删**（见 §7.7.6 表格内的「原裁定」引述与本节 §7.7.11.5 的更正注记），更正以「区分两个不同的东西」的形式给出，便于追溯。
>
> **一句话结论（逐字口径）**：**站位 bare 进 `T`**（`export_bares` / `target_bares`）；**站位对象不进 `T`**；`is_stub` 只读标注、不参与过滤。原 G1-b 的字面口径（「占位不进 `target_bares`」）在**唯一能触发站位机制的场景里必然中止导出**，是一条**自相矛盾 / 功能级死锁**的规格（反证链见 §7.7.11.1），故按方案 (A) 更正。台账记录见 §13.7，否决表见 `reports/himi-merge/05-decisions.md` §3.20。

##### 7.7.11.1 矛盾本身（含反证链）

**原裁定（§7.7.6 的字面口径）**：G1-a「占位不算家族覆盖」+ G1-b「占位不进 `target_bares`」。

**反证链（三条，缺一不可）**：

| # | 命题 | 依据 |
|---|---|---|
| 1 | 站位判据只在「缺席部件**有** `VGMap` 且**专属槽位**被现存对象引用」时插桩 ⇒ **能插桩的缺席部件必然已落盘**（在 `L` 里） | §7.7.4 的判据本体：`exclusive(bare)` 需要 `own(bare)` 非空（来自 json `VGMap`），且 `exclusive` 非空才可能命中 ⇒ 该 bare 必有 json、必有 `VGMap`。两条规则都如此：规则① 的缺失组件与规则② 的逐组件命中者，**都要先过 `landed_values[member]` 非空这一关**（`common/himi_skeleton.py:1341-1348` 的反例① 分支：`values` 为空 ⇒ 记 `NO_VGMAP` ⇒ 不插桩） |
| 2 | G1 家族门是 `L(ib) ⊆ T`，而 `L(ib)` 取该家族**已落盘**组件 | `common/himi_skeleton.py` 的 `family_members`（只遍历 `self.components`，即已落盘） |
| 3 | 若把被站位的 bare 排除出 `T` ⇒ 被吸收成员**永远** `missing` ⇒ 家族**永远**不 `safe` ⇒ 契约 `level=error` ⇒ **`Fatal` 中止导出** | `family_gate` → `gate_rejected` → `evaluate_merged_skeleton_contract`（`data_count>0 ∧ component_count==0` ⇒ `error`）→ 导出器 `Fatal` |

**⇒ 结论：在「站位被注入」的唯一场景里，原 G1-b 的字面口径会让导出必然中止 —— 而那个场景正是站位要解决的场景。站位机制因此永远不可能生效。这是一条功能级死锁：能触发站位判据的场景集合 ⊆ 原 G1-b 会让导出必然中止的场景集合（两者的交集不是「部分」，而是全部）。**

**实测复现**（`.dbg/t26_g1b_deadlock_probe.py`，T26 留下，T30 已复跑确认）：

```text
prepare ok: True
writes (= L(aaaa) 已落盘组件): ['aaaa-1-0', 'aaaa-2-0']
gate_report: {"aaaa": {..., "target": ["aaaa-1-0","aaaa-2-0"], "missing": [], "safe": true}}
exclusive(aaaa-2-0) = [2, 3, 5] （非空 ⇒ 判据会插桩）
[HIMI站位] 部件 aaaa-2-0 没有对应对象，已创建极限小三角面占位（…）
stub_bares: ['aaaa-2-0']
stub 是否被放进 ordered: True

G1-b 字面（排除站位）: T=['aaaa-1-0'] L=['aaaa-1-0','aaaa-2-0'] missing=['aaaa-2-0'] family_safe=False
ZZMI 语义（含站位）  : T=['aaaa-1-0','aaaa-2-0'] L=['aaaa-1-0','aaaa-2-0'] missing=[] family_safe=True
```

> 这条矛盾是 T26 在做**真端到端**（`%TEMP%` 副本上跑真实 `ExportUnity.export()` → 读回产物 `ws.ini`）时撞出来的：**单元级测试永远看不到它**，因为单元测试直接喂 `target_bares`，绕过了「导出集合如何构造」这一环。

##### 7.7.11.2 裁定：**方案 (A)** —— 被站位的 bare **进** `target_bares`，`is_stub` 只作只读标注

**裁定要点（逐字，供实现与后续任务直接引用）**：**站位 bare 进 `T`**；**站位对象不进 `T`**；`is_stub` 只读、不参与过滤；家族门按「被站位的 bare 算已覆盖」放行（理由见 §7.7.11.3）。

| 方案 | 内容 | 裁定 |
|---|---|---|
| **(A)** | 被站位的 bare **进** `export_bares` / `target_bares`；`is_stub` 保留为**只读标注**（`himi_stub_bares()`），**不参与过滤** | ✅ **采用** |
| (B) | `target_bares` 排除站位 + 家族门把站位 bare 视为**已覆盖**（需改 `prepare(target_bares=…, stub_bares=…)` 与 `prepare_gate_oracle` 的签名） | ❌ **否决** |

**取舍理由（逐条）**：

| 维度 | (A) | (B) |
|---|---|---|
| **语义正确性** | ✅ 一致：「被站位的 bare 的几何**已被覆盖**」是**几何归属事实**，与「哪个部件在场景里有对象」无关。家族门的输入本来就该是「本次导出把谁的几何纳入了 mod」——被站位的 bare **确实**被纳入了（以「压掉原版 draw」的形式） | ✅ 也能正确，但需要家族门**额外知道**「站位集合」才能修正判据 ⇒ 判据不再是纯几何事实，而依赖一个**注入动作的产物** |
| **改动面** | ✅ **零代码改动**（T22/T26 已按 (A) 落地；`_himi_export_bares` 的 docstring 已写反证） | ❌ 需改两个函数的签名（`prepare` / `prepare_gate_oracle`）+ 数据层的门控过滤 + 导出侧调用点 + 相应测试 ⇒ 至少 4 处 |
| **与已交付行为的兼容性** | ✅ 与 ZZMI 同语义（`ui/universal/zzmi.py:1008-1013`：站位 DrawCall 被追加进 `ordered` ⇒ 身份自然进计划 ⇒ 家族门放行）；既有 2715 条测试全绿 | ⚠️ 引入一条 ZZMI 没有的**双口径**（家族门的输入不再是「导出集合」而是「导出集合 + 站位修正」） |
| **后续维护成本** | ✅ 单一事实源：家族门只看「本次导出的部件身份集合」 | ❌ **两套身份集合**（`target_bares` 与 `stub_bares`）会随实现演进而漂移 —— 本仓已有同类教训（ZZMI 的 `_zzmi_merged_identity_records` 与 `ordered` 两套口径） |
| **循环依赖风险** | ✅ 无：站位 bare 的加入是**事实登记**，不依赖「站位对象是否创建成功」 | ⚠️ 家族门的结论会**依赖站位判定**，而站位判定又要在门控之后执行（§7.7.6 G1-e 的顺序要求）⇒ 需要额外论证「判定顺序不会互相影响」 |

**⇒ 裁定 (A)**，理由的核心是：**「被站位的 bare 的几何已被覆盖」是几何归属事实，不是注入动作的产物**；把事实登记进目标集合，比让家族门去理解「注入动作」更简单、更单一事实源，且与 ZZMI 实机验证过的语义一致。

##### 7.7.11.3 决定性问题的答案：**被站位覆盖的成员算不算「已覆盖」？——算**

G1 的立意（§7.4）是「重绑 `vs-t0` 等于替该 pass 的**全部** render 消费者换骨架」。据此逐条核验「被站位覆盖的成员 `M` 是否算已覆盖」：

| # | 要核验的事 | 事实 | 结论 |
|---|---|---|---|
| 1 | `M` 的**几何**还需要骨架服务吗？ | 不需要——`M` 的顶点已并入吸收对象 `B` 的 mesh；`M` 自己的对象不存在 ⇒ 没有「`M` 的几何」需要蒙皮 | ✅ 已覆盖 |
| 2 | `M` 的**渲染消费者 draw** 还需要骨架吗？ | 不需要——它被站位的 `handling = skip` 吞掉（**这正是站位的作用 B**） | ✅ 已覆盖 |
| 3 | `M` 的**骨骼**进得了合并骨架吗？ | 进得了——`M` 的骨骼被 `B` 的顶点引用 ⇒ 那些槽位在 `B` 的 `VGMap` 值域里 ⇒ 已随 `B` 进入骨架（**HIMI 的骨架是静态的、在导入期由全部已落盘组件建成，与 `T` 无关**） | ✅ 已覆盖 |
| 4 | `M` 的 **deform pass** 会怎样？ | 该 pass 按家族共享（`04` §2.8 实测 39/39 单家族）⇒ 随家族一起被重绑；`M` 在该 pass 上的消费者 draw 已被站位压掉 ⇒ **不存在任何「用 `M` 的局部索引骨架」的消费者** | ✅ 重绑安全 |
| 5 | 若 `M` 缺席 `T`，会不会漏掉某个真实消费者？ | 不会——`M` 的消费者要么被压掉（第 2 条），要么是家族其它成员（他们的几何仍在 `T` 里） | ✅ 无遗漏 |

**⇒ 判定：算已覆盖。** 而且这里的「覆盖」有**双重保障**：① 几何被并入 `B`（`B` 在 `T` 里，骨架为它服务）；② 原版 draw 被站位压掉（不会出现「用 `M` 的局部索引骨架」的消费者）。

**两种方案分歧的根源**：原 G1-b 把「覆盖」理解成**「该部件自己的几何还在 mod 里」**（几何存在性口径）；而 G1 的立意要求的是**「重绑后不会有消费者读到错的骨架」**（重绑安全性口径）。**被站位的 bare 在第一个口径下不算覆盖，在第二个口径下算覆盖** —— G1 的判据服务于第二个口径，所以应当按第二个口径裁定。

##### 7.7.11.4 处置 G1-b 的原始顾虑（「污染 `VGMap` / 槽位统计」）

**该顾虑在方案 (A) 下不成立**，原因是它把**两个不同的东西**混为一谈：

| 东西 | 有 `VGMap`？ | 有几何？ | 进 `T` 吗？ | 会不会污染骨架/槽位统计？ |
|---|---|---|---|---|
| **被站位的 bare**（例 `aaaa-2-0`） | ✅ **有**（它是已落盘组件，有自己的 json 与 `VGMap`、有自己的真实骨骼） | ❌ 场景里没有它的对象（几何已并入别人） | ✅ **进** `T` | **不会**——它的 `VGMap`/骨骼**本来就在导入期参与了骨架构建**（导入侧 `target_bares=None` ⇒ 全部已落盘组件）；导出侧进 `T` 只是**登记「它的几何已被覆盖」**，不触发任何骨架重建 |
| **站位对象**（那个 3 顶点小三角） | ❌ **没有**（不是工作空间组件，没有 json） | ✅ 有（3 顶点） | ❌ **不进** `T` | **不会**——它根本不进任何元数据/槽位统计 |

**为什么「进 `T` 不会污染骨架」是结构性事实（不是运气）**：

| 事实 | 依据 |
|---|---|
| `target_bares` 的**唯一**生产消费者是 `family_gate`（家族门） | `grep target_bares`：`common/himi_skeleton.py` 里只有 `family_gate`（`common/himi_skeleton.py:567-594`）与 `prepare` 的赋值点（`common/himi_skeleton.py:2509-2513`）；**骨架构建路径不读它** |
| 骨架（`SlotCount` / `Slots` / 骨架二进制）在**导入期**由**全部已落盘组件**建成 | 导入侧 `target_bares=None` ⇒ `[c.bare for c in joined]`（`:2326-2327`）⇒ 全部已落盘；导出侧 `load_workspace_plan` **只读**缓存里的骨架，不重建 |
| 站位 bare 的 `VGMap` 与骨骼**本来就是**导入期骨架的一部分 | 它是有 json 的已落盘组件 ⇒ 导入期 `joined` 里就有它 |

**⇒ 若要在下游拦截**（防未来有人让运行时按 `T` 重建骨架）：拦截点是 **`is_stub` 只读标注**（`himi_stub_bares()`）——它的语义是「这个 bare 是**被站位的成员**，它的几何不在场景里，**不得**用它构造几何缓冲/顶点数预算」。**当前消费点**：`ui/universal/himi.py` 的 `_himi_export_entries()` 把 `is_stub` 写进条目供诊断；`_himi_export_bares()` **不使用**它（单一过滤口径 = `vg_count > 0`）。**不需要新增拦截**，但**必须保留该标注**并在实现注释里写明它的用途。

##### 7.7.11.5 更正记录（可追溯）

| 项 | 内容 |
|---|---|
| **被更正的原裁定** | §7.7.6 的 **G1-a**（「占位不算家族覆盖」）与 **G1-b**（「占位不进 `target_bares`」）——它们把「站位**对象**」与「被站位的 **bare**」混为一谈 |
| **更正后的口径** | **站位对象**不算覆盖、不进 `T`（原裁定这一半正确）；**被站位的 bare 算覆盖、必须进 `T`**（原裁定这一半错误且致命） |
| **原文是否删除** | **未删**：§7.7.6 的表格保留了「原裁定」的引述，并在同一行给出「① 正确 / ② 错误」的拆分 |
| **更正理由的位置** | 本节 §7.7.11.1（矛盾与反证链）/ §7.7.11.2（二选一裁定）/ §7.7.11.3（决定性问题的答案）/ §7.7.11.4（原始顾虑的处置） |
| **G1-c/G1-d/G1-e** | **原裁定正确，未改**（pass 消费者层不豁免 / 三步顺序 / 执行顺序），仅在 G1-c 的①里补一句「被站位的 bare 算」 |
| **连带更正（元数据字段，需后续实现）** | 角色级 metadata 的 `StubTargetBaresIncluded=False` 与 `StubFamilyCoverageCounted=False`（`common/himi_skeleton.py:2811-2812`）是**按原 G1-b 口径写的，现已错误** ⇒ 见 §7.7.11.6 |
| **同步位置** | `reports/himi-merge/05-decisions.md` **§3.20**（本次裁定的理由与被推翻的原裁定）；§13 台账见 §13.7 |

##### 7.7.11.6 实现侧的确认与唯一待补项

**实现现状（T22/T26 已落地，T30 复核）**：

| 项 | 现状 | 裁定 (A) 下的结论 |
|---|---|---|
| 被站位的 bare 是否进 `export_bares` | ✅ **进**（`ui/universal/himi.py` 的 `_himi_export_bares`：`vg_count > 0` 是唯一过滤口径） | ✅ 符合 (A) |
| `is_stub` 是否只读 | ✅ **只读**（`himi_stub_bares()` 供诊断；`_himi_export_bares` 的 docstring 已逐字写入反证链） | ✅ 符合 (A) |
| 家族门是否放行 | ✅ 放行（probe 实测 `missing=[] family_safe=True`） | ✅ 符合 (A) |
| **元数据声明字段** | ✅ **口径注释已补**（`common/himi_skeleton.py:2787-2812` 写死「这是 `prepare()` 的数据层口径，不等于导出侧 `export_bares`；只描述**合成出来的站位对象**，被站位的 bare 必须进 `export_bares`」）；❌ 字段名与值未改（仍 `False`/`False`/version `1`，改名仍登记） | ✅ 符合 (A)（口径已无歧义）；命名见下「待补 ①」 |

**待补项（T30 复核时点状态；复核时点 = 2026-09-24 16:05–16:15）**

> **并发修改提示**：T30 复核期间，工作区里有**另一个实现任务（t31）**正在按本条登记清单改代码与文档（`ui/universal/himi.py` mtime 15:59、`common/himi_skeleton.py` 15:59、`tests/test_himi_stub_objects.py` 16:02、本计划书 16:09+）。下表是**该时点的实测状态**；若与当前不符，以代码为准（判断口径不变）。

| # | 待补项 | 时点状态 | 处置结论 |
|---|---|---|---|
| ① | `common/himi_skeleton.py:2811-2812` 的字段名反向陈述（`StubTargetBaresIncluded=False` / `StubFamilyCoverageCounted=False`） | ✅ **口径注释已补**（含反证链与 §7.7.11 / `18-stub-rereview.md` §4.2 / `05-decisions.md` §3.20 的指针）。❌ **字段名与值未改**，改名仍登记 | **处置 =「补字段注释消歧」（采用），本轮不改名**：字段**无生产消费者**，且两处测试断言（`tests/test_himi_stub_objects.py`、`tests/test_himi_merged_runtime.py::HIMIStubGateInteractionTests`）把 `False`/`1` 钉死 ⇒ 改名/改 version 会让既有断言无谓漂移，收益只是命名美观。**改名作为后续可选保留登记**：`StubBareInExportTargetSet=True` / `StubBareCountsAsFamilyCoverage=True` / `StubObjectInTargetSet=False` / `StubGateInteractionVersion` 1→2（改则**必须同步**那两处断言） |
| ② | `ui/universal/himi.py` 模块 docstring 与 `_himi_export_entries()` docstring 曾写「显式排除 `is_stub`」并引用**不存在**的测试名 `test_stub_bare_never_enters_g1_target_set` | ✅ **已修复（t31）**：模块 docstring 旧段整段删除、改写为「**口径（已反转）：站位 bare 进 `export_bares`**」+ 完整反证链；`_himi_export_entries()` 末句改为「**当前口径是不排除**，理由见 `_himi_export_bares`」；新增 `tests/test_himi_stub_objects.py::StubDocConsistencyTests` 两条护栏（① 源码里的 `Class::test` 引用必须真实存在；② `_himi_export_bares` 的口径文字必须是反转后那一版，被否决方案的措辞与测试名不得残留） | ✅ **关闭**（这是本轮最有价值的护栏：把「照注释改回去 ⇒ 重新引入死锁」的路径用测试堵住了） |
| ③ | `common/himi_skeleton.py:2787-2812` 注释块曾按原 G1-b 口径写 | ✅ **已修复（t31）**（见 ① 的口径注释：`common/himi_skeleton.py:2787-2812`） | ✅ **关闭** |
| ④ | 两处测试**名字**易误读：`tests/test_himi_stub_objects.py` 的 `test_st6_stub_is_not_a_family_coverage_claim`（`:1768`）与 `test_st7_stub_does_not_enter_target_bares`（`:1804`） | ❌ **未改名**（body 已与更正后口径一致，只有名字与 docstring 标题像原口径；t33 只补了 docstring 澄清） | 登记为后续可选（建议改名 `..._stub_object_...`）；**不影响任何行为**。行号由 `tests/test_himi_stub_objects.py::StubNailIntegrityTests::test_doc_anchor_references_resolve` 机械校验 |
| ⑤ | ST 编号在计划书 §7.7.8 与测试文件抬头之间错位（ST2/ST4 互换、ST6/ST7 指 G1-a/G1-b、两个 `test_st5_*`） | ❌ **未处理** | 登记为后续可选；**逐条对照表已写进 §9.6**（读者不再需要靠编号猜） |

**待补 ① 的原文依据（保留未删，便于追溯本轮登记的理由）**：`StubTargetBaresIncluded=False` 这个字面在 (A) 下是**反向陈述**（读者会读成「站位 bare 不在目标集合里」，与实现恰好相反）；这两个字段**只有声明、无生产消费者**（`grep` 确认：只在 `common/himi_skeleton.py` 写出 + 两处测试断言），且**不在** `_himi_cache_intact` 的判据里 ⇒ 改名零缓存代价。**当时的建议是改名**；t31 的实际处置是「补注释消歧 + 改名登记」（理由：避免既有断言无谓漂移）——两者都满足「必须处置字段名易被误读的问题」这条要求，**裁定接受 t31 的处置**，改名保持为后续可选项。

---

#### 7.7.12 **F-2 裁定：规则② 的「被吸收」判据退化 —— 判据替换为「外来槽」口径**（2026-09-24，T40）

> **本节更正 §7.7.4 的判据本体与 §7.7.8 的 ST2/ST3**。原文措辞**保留未删**（§7.7.4 的四个输入表 / 两条退化情形、§7.7.8 的 ST2/ST3 行内均标注「更正」并指向本节），便于追溯。
> 来源：t38（repair round）的登记 **F-2（high）** + 本轮（T40）的独立实测与裁定。**本节只做裁定与文档更正；实现登记给后续任务**（§7.7.12.4）。

##### 7.7.12.1 问题与独立复核（判据对多成员家族**结构性失效**）

**F-2 的原始表述**：`694025c6`（3 成员）/ `def0c0ae`（2 成员）的**每个**成员 `exclusive = ∅` ⇒ 即使几何真的被 join 走，规则② 也永远判不出吸收。

**T40 独立复核（本仓自写探针 `.dbg/_t40_f2_probe.py`，只读 K:，五角色）**：

| 项 | 实测（2026-09-24） |
|---|---|
| 家族名册（`LOD0/DrawIB-Component.json`） | 五角色 **6 / 9 / 8 / 7 / 8**（按 咚！炽愿吉星 / 嗨♪爱愿妖精♥ / 愈生佑翎 / 窈窕谍影 / 镇×偃月叩晓；多重集 = 旧文所称 8/8/7/6/9） |
| **有合并数据（`VGMap`）的角色** | **仅 `嗨♪爱愿妖精♥`**：12 个可解析组件**全部带 `VGMap`** ⇒ 5 个落地家族 / 12 个落地组件。**其余四角色**：可解析的组件 json 数 3 / 6 / 7 / 6，但**全部没有 `VGMap`**（尚未合并 ⇒ 不在 `L` 内）⇒ 规则② 的 `own` 恒不可判定 ⇒ 按 fail-closed 不插桩（§7.7.12.2） |
| 多成员落地家族 | `2c706fbd`（5 成员）/ `694025c6`（3 成员）/ `def0c0ae`（2 成员）——**三者 `max_exclusive = 0`** |
| 单成员落地家族（对照） | `70a4da3e`（`max_exclusive = 71`）/ `b35cfc6c`（`max_exclusive = 19`）⇒ 判据**对它们有效** |
| 家族槽位对其他家族的命名空间 | **全部独占**：`2c706fbd` 184/184、`694025c6` 18/18、`def0c0ae` 51/51（`shared_other = 0`） |
| 旧判据的多压面（`exclusive` 非空但被**别的家族**声明） | **0**（`multi_press_surface = 0`，本角色落地数据） |
| 与合并对象命名空间的重叠 | `694025c6 ∩ 2c706fbd = 0`、`def0c0ae ∩ 2c706fbd = 0`（与 t38 一致） |

**⇒ 结论（回答「这是否是真缺陷」）：是。**
- **判据退化是真的、且范围比 t38 报的更大**：t38 只点了两个家族；实测**三个**多成员落地家族（`2c706fbd`/`694025c6`/`def0c0ae`）的**每个**成员 `exclusive` 恒空 ⇒ 现行 `exclusive ∩ used ≠ ∅` 对它们**恒假**（无论是否真的被吸收），即**不是「保守」而是「结构性失效」**（区分：保守 = 有证据但不压；这里是**证据口径本身不成立**）。
- **当前不可达、故现状不插桩是对的**：这三族与合并对象的命名空间重叠为 0 ⇒ 现阶段它们确实没被吸收。**但这是潜在风险**：一旦用户把 `694025c6`/`def0c0ae` 的分块 join 进别的对象，规则② 仍然判不出来 ⇒ 原版 draw 照跑 ⇒ **同一重影问题重演**。
- **旧判据还有一个方向性缺陷**（t38 提示 #1，实测暴露为 0 但**构造性存在**）：`himi_exclusive_slots` 只在**同家族**内做差集 ⇒ 一个槽位若被**别的家族**的组件声明，仍会被算作「专属槽」⇒ 若那个家族的现存对象引用了它，旧判据会把「共享骨骼」误读成「几何被吸收」⇒ **多压**（与 HIMI「宁可不压」取向相反，且正是 2026-09-16 事故的形态）。

##### 7.7.12.2 裁定采用的判据（「外来槽」口径）

```text
declared_by_present := ⋃ { own(c) : c ∈ present ∧ own(c) 可判定 }        # 现存对象的「声明命名空间」
foreign_used        := used \ declared_by_present                       # 现存对象「用了但谁都没声明」的槽
外来槽路径: absorbed_rule2(ib) := ∃ m ∈ C(ib) ∩ L : own(m) ∩ foreign_used ≠ ∅   # 逐成员（HIMI 风格）
```

**为什么它同时满足验收要求的 (a) 与 (b)**：

| 要求 | 该判据如何满足 | 支撑 |
|---|---|---|
| **(a) 能检出「整族缺席 + 几何被同族/他族幸存对象吸收」** | 吸收会把**被吸收组件的顶点**带进幸存对象，而那些顶点用的是**被吸收组件的全局槽**；这些槽**不在**幸存对象的 `own` 里（它的 `VGMap` 只覆盖它自己的局部槽）⇒ 只要**没有任何 present 对象声明**它们，它们就是「外来槽」，且必然落在缺席家族的 `own` 里 ⇒ 判据触发 | 实测的**检出容量**：三族的槽位对其他家族**全部独占**（18/18、51/51、184/184）⇒ 一旦被吸收，其槽位**全部**成为外来槽 ⇒ 以最大灵敏度触发（t38 的 `overlap = 0` 正是这个前提） |
| **(b) 不得重新引入 2026-09-16 事故**（跨族借位被误读为吸收 ⇒ 误插） | **构造性免疫**：任何被 present 对象引用的槽位，在正常管线里**都在该对象自己的 `own` 里**（导入期写入的 `VGMap` = 「本组件用到的局部槽 → 全局槽」，§5.1）；**借位槽（无论同族还是跨族）由其 canonical 拥有者声明**，拥有者在场 ⇒ 落在 `declared_by_present` 里 ⇒ **永不算外来槽**。事故形态（载体对象引用共享骨骼的 canonical 槽）在新判据下**不可表达** | 事故原型 `tests/test_zzmi_merged_skeleton_ini.py:2444-2471`（单成员家族整族缺席 + 跨族载体）；判据层论证见下 |

**「借位槽必然被声明」的论证（(b) 的关键一跳）**：设槽 `s` 被 present 对象 `P` 引用。`P` 的几何要么是它自己的顶点（则 `s` 由 `P` 的 `VGMap` 映出 ⇒ `s ∈ own(P)`），要么是**被吸收组件的顶点**（则 `s` 来自被吸收组件的 `VGMap`）。前者 ⇒ `s ∈ declared_by_present` ⇒ 不是外来槽；后者 ⇒ 只有当**没有任何** present 对象声明 `s` 时才被判为吸收——**这正是想要的语义**。⇒ 判据不区分「谁引用了多少」，只问「这个槽有没有人认领」；**「共享骨骼」永远有人认领**。

**fail-closed 规则（信息不足时不猜）**：

| 情形 | 处置 |
|---|---|
| 任一 present 对象的 `own` **不可判定**（未落盘 / 无 json / 无法解析身份） | **外来槽路径不参与 ⇒ 该族按「不插桩」**（宁可不压；与 `used` 不可得的既有兜底同一取向） |
| present 对象的**成员身份**判不出、但 **IB 身份**判得出（合并对象带 `_copy` 后缀，见 §7.7.12.5） | 取该 IB **全部落盘成员 `own` 的并集**作 `declared_by_present` 的**上界**（声明得更多 ⇒ 外来槽更少 ⇒ 更少压，方向安全） |
| 家族名册缺失 / 成员名单不可枚举 | 沿用现状：**不插桩**（`HIMI_STUB_REASON_NO_FAMILY_MAP`） |

**成员级判定顺序（规则②，替换后）**：① `own(m) = ∅`（无反查数据）⇒ skip；② 规则③ `VGMapDedupExcluded` ⇒ skip；③ `own(m) ∩ foreign_used = ∅` ⇒ skip；④ 否则 stub。**规则① 不参与本判据**（t38 已改为 ZZMI 逐字口径，见 §7.7.3）。

##### 7.7.12.3 候选比较与否决表

| 候选 | 判定 | 理由 |
|---|---|---|
| **(A) 现状：`exclusive_C(ib)(m) ∩ used ≠ ∅`**（族内专属槽 ∩ 现存引用） | ❌ **替换** | ① 对多成员家族**结构性失效**（实测三族 `max_exclusive = 0`）⇒ 潜在重影；② 差集只在本家族内做 ⇒ **构造性**多压面（跨族借位被当专属槽），方向与 HIMI「宁可不压」相反 |
| **(B) 家族级 `own(family) ∩ used ≠ ∅`**（t38 提示的候选） | ❌ **否决** | `own(family)` **包含借位槽**，而现存载体**必然**引用借位槽（共享骨骼）⇒ 判据退化成「族里有任何槽被谁引用过」⇒ **正是 2026-09-16 事故形态**（把共享骨骼读成吸收）⇒ 多压。**它不是 (A) 的加强版，而是回到事故前的宽松口径** |
| **(C) 「外来槽」口径（本节采纳）** | ✅ **采用** | 对 (a) 有实测支撑的检出容量（槽位全局独占）；对 (b) **构造性免疫**（借位槽必然被声明）；且**顺带消除 (A) 的构造性多压面**（替换后归零） |
| **(D) 全量值域 `own(m) ∩ used ≠ ∅`**（ZZMI 2026-09-16 事故前口径） | ❌ 否决 | 就是事故判据（`13` §2.3）：借位 canonical 槽被当成「被吸收」⇒ 脸部误插占位 |
| **(E) 顶点数膨胀旁证**（present 对象当前顶点数 > 记录的 `OriginalVertexCount`） | ⚠️ **登记为可选正交旁证，不作判据** | 会被「用户自己改几何」污染，且无法定位到「哪个家族被吸收」；可在诊断里打印以辅助排障（**实现任务可选**） |

> **被否决方案 (B)/(D) 同时记入 `reports/himi-merge/05-decisions.md` §3.22 的否决表**。

##### 7.7.12.4 最终处置：**需要修**（登记给后续实现任务；本任务不写生产代码）

**接口（建议形态，实现任务可微调命名）**：

| 面 | 改动 |
|---|---|
| `common/himi_skeleton.py::is_drawib_absorbed` | 新增形参 `declared_by_present=None`（`None` ⇒ 外来槽路径不可用 ⇒ 返回 `False`）；判据体换成 `own(m) ∩ (used − declared_by_present)` |
| `common/himi_skeleton.py::decide_stub_injection` | 内部由 `present` + `landed_values` 计算 `declared_by_present`（**导出侧签名不变**：`ui/universal/himi.py` 仍只传 `scene_present_bares` / `used_group_ids`）；present 身份归一化复用 §7.7.12.5 的 IB 身份逻辑 |
| 诊断常量/文案 | `HIMI_STUB_REASON_NO_EXCLUSIVE` 的**用户可见文案**改为「无外来槽证据 ⇒ 保守不插桩」类措辞（常量名可保留，避免无谓改名） |
| 测试 | ST1/ST2/ST3 的 fixture 需给 present 对象**声明命名空间**（否则新判据下不可达 ⇒ 用例会变成恒真）；`15-verify-stub.py` 的 A 组需同步（该脚本由 verifier 持有） |

**验收标准（供后续任务直接用）**：

| ID | 断言 | 反例（必须同时断言） |
|---|---|---|
| **ST13** | **规则② 外来槽正例**：整族缺席 + 全成员 `exclusive = ∅`（族内 100% 共享）+ present 对象 `P`（已落盘）引用 `X` 的槽、且 `own(P) ∩ own(X) = ∅` ⇒ `X` 的相应成员**逐组件**补站位 | 把 `P` 的 `own` 改成 `∅`（不可判定）⇒ **不插桩**（fail-closed） |
| **ST14a** | **事故形态反例**：`X` 的槽**也被 `P` 声明**（跨族/同族 canonical 共享）⇒ `own(X) ∩ foreign_used = ∅` ⇒ **不插桩**（不得重演 2026-09-16 脸部误插） | 把该槽从 `P` 的 `own` 里去掉 ⇒ 插桩（证明 ST14a 非恒真） |
| **ST14b** | **`declared_by_present` 不可得** ⇒ 规则② 不插桩（信息不足不猜） | 补齐全部 present 的 `own` ⇒ 按 ST13 行为插桩 |
| **ST14c** | **回归**：ST1..ST12（按新语义重建）+ `15-verify-stub.py` 68/68 + 探针 23/23 + 全量无回归 | — |

**已知代价（如实登记为限制）**：被吸收组件的槽位**全部被某个 present 对象声明**时，新判据**检不出**吸收（该场景下「共享骨骼」与「几何被吸收」在现有证据下**不可区分**）⇒ 保留「少压」方向（可见重影 > 静默消失）。

##### 7.7.12.5 连带记录：F-A（`in_scene` 按 bare 精确匹配 ⇒ 合并对象识别不到）

t38 同时修掉一条**同族缺陷**：`in_scene` 原按 **bare 精确匹配**，而合并对象的场景身份可能带副本后缀（实测 mesh 名 `LOD0.2c706fbd-47199-0_copy`，其 bare 不匹配任何声明成员）⇒ 家族被误判「整族缺席」⇒ **规则① 根本不触发**。修法：`present_ibs = {bare.split("-",1)[0] for bare in present}`，`partial_family = bool(in_scene) or (draw_ib in present_ibs)`（对齐 ZZMI 的 `_normalize_merged_object_drawcalls` + `present` 账本）。

- **两态证据**：`StubRule1Tests::test_rule1_recognizes_merged_object_by_ib_identity` —— 场景对象名 `LOD0.2c706fbd-47199-0_copy` ⇒ `rule=1`、5 个成员**全部**补站位；换成别的 IB（`9f9f9f9f-1-0_copy`）⇒ `rule=2`、`stub_members=[]`。
- **与 F-2 的关系**：本节的 `declared_by_present` 需要 present 对象的 `own` ⇒ **必须复用同一套身份归一化**（否则带后缀的合并对象会让 `own` 不可判定 ⇒ 外来槽路径 fail-closed ⇒ 少压）。

---

## 8. 任务拆解（TDD，每步可独立验收）

| # | 任务 | 依赖 | 验收 |
|---|---|---|---|
| H0 | **门控验证并入探针**：把 §2.8 的「SO 家族独占」统计并进 `tools/himi_merged_probe.py`，加 3 条 `--self-check` | — | `--self-check` 23/23 PASS（20 + 3） |
| H1 | `HIMILogParser` + 单测（五帧） | H0 | 五帧 deform pass 计数 = 8/8/7/6/10；逐 draw VS 取 `-vs=` 后缀（771/771 一致） |
| H2 | `HIMIDeformResolver`（A/B/C + IB 门控 + R1/R2）+ 单测 | H1 | 34/34 组件 join 成功；18/39 pass 归属 OWN；140134/`000035` 判为真外来对象 |
| H3 | `HIMIBoneMapBuilder`（被引用槽集合 + bitwise 去重 + 角色级骨架）+ 单测 | H2 | 唯一骨骼 276/343/315/219/252；镇×偃月叩晓引用槽和 = 594（不是 876）；跨角色重合 0 |
| H4 | `HIMISkeletonMergeHelper.ensure_skeleton_data`（写回 + 缓存 + 幂等 + force） | H3 | 34 个组件写回；二次运行全跳过；`force` 重建 |
| H5 | 导入门控（IP-01/03/05）+ 消费走既有双条件路径 | H4 | 复选框关 ⇒ `override=None`；预生成失败 ⇒ `set_import_merged_vgmap(False)` 且导入继续 |
| H6 | 导出侧：**导入期密集建组**（`common/mesh_create_helper.py:515-517` ⇒ `index == int(name)` 导入时即成立）+ `blend_size`（IP-21）+ 全局 id 直写 + 导出后命名空间复核 | H4 | 导出 buffer 的 BLENDINDICES == 全局 id；无升宽。**t45 更正**：达成路径**不是** `_prepare_merged_skeleton_vertex_groups`（该预处理不在 HIMI 路径上；IP-22 状态改为 ❌未做，见 §6.4） |
| H7 | 导出侧：`add_merged_skeleton_sections` + VB 段 deform 重放（IP-13..IP-18） | H6 | 生成 INI 断言全绿（§9 A3/A4）；非合并路径逐字节不变 |
| H8 | 契约判定 + 诊断（IP-23） | H7 | G1 拒绝档触发时有显式文案与组件清单 |
| H9 | 文档：`CONTEXT.md` 增补词条（HIMI deform pass / 骨骼池 / 合并骨架） | H8 | — |
| H10 | 端到端：Blender headless 导入 + 导出 [PASS] | H8 | 脚本 + 产物校验 |

**落地状态（T30 刷新，2026-09-24）**——H0–H10 是设计冻结期的拆解；实现期把它们合并落地为 4 个模块，并在设计冻结之后新增了若干任务（下表「实现期任务」列）。**行号/文件名以 T30 复核为准**。

| # | 落地位置（T30 复核） | 状态 |
|---|---|---|
| H0 | `tools/himi_merged_probe.py --self-check`（T6 并入 §2.8 的三条门控统计） | ✅ **23/23 PASS**（exit=0） |
| H1 | `common/himi_skeleton.py:155` `HIMILogParser` + `tests/test_himi_skeleton.py`（38 条；真实 dump fixture 不存在时整套 skip） | ✅ |
| H2 | `common/himi_skeleton.py:432` `HIMIDeformResolver`（`family_gate` `common/himi_skeleton.py:570` / `pass_families` `common/himi_skeleton.py:598` / `gate_allows` `common/himi_skeleton.py:612`） | ✅ |
| H3 | `common/himi_skeleton.py:655` `HIMIBoneMapBuilder` | ✅ |
| H4 | `common/himi_skeleton.py:2116` `HIMISkeletonMergeHelper`（`prepare` `common/himi_skeleton.py:2554` / `commit` `common/himi_skeleton.py:3056` / `cache_only_plan` `common/himi_skeleton.py:2397` / `ensure_skeleton_data` `common/himi_skeleton.py:3165`） | ✅ |
| H5 | 导入门控（`ui/ui_func_import_ssmt.py` 的 `import_merged_vgmap` 消费点）+ `tests/test_himi_clear_merged_skeleton_cache.py`（16 条） | ✅ |
| H6 | 导出侧顶点组处理 = **导入期密集建组**（`common/mesh_create_helper.py:515-517`，`index == int(name)` 导入时就成立）+ gametype **BI16** 免升宽 + 导出后 `check_exported_blend_namespace` 复核 + `tests/test_himi_merged_runtime.py`（104 条） | ✅（**t45 裁定 + t46 回写**：`_prepare_merged_skeleton_vertex_groups` **不在 HIMI 路径上**（`run_merged_skeleton_preprocess` 三分支皆 False）；IP-22 状态已改为 ❌未做，见 §6.4） |
| H7 | `common/himi_merged_runtime.py`（资源声明 `:315-726` / 计划层 `load_workspace_plan` `:2029` / 骨架发布 `:2245`）+ `ui/universal/himi.py` | ✅ |
| H8 | `common/himi_merged_contract.py`（`evaluate_merged_skeleton_contract` `:127` + `_format_diagnostics` `:55`）+ `tests/test_himi_merged_contract.py`（17 条） | ✅ |
| H9 | `CONTEXT.md` 增补 HIMI 词条（deform pass / 骨骼池 / 合并骨架） | ❌ **未完成**（`grep HIMI CONTEXT.md` 零命中）——登记为**文档欠账**，不影响功能交付 |
| H10 | 真端到端：`tests/test_himi_stub_objects.py::StubEndToEndIniTests`（`%TEMP%` 副本上跑真实 `ExportUnity.export()`，读回产物 `ws.ini`） | ✅（**Blender headless 那半仍未做**：本环境无 Blender 运行条件，见 §9.5 D1） |

**设计冻结之后新增的任务（实现期台账，与 §13 互为索引）**：

| 任务 | 内容 | 台账位置 |
|---|---|---|
| T12 | G1 升级为 **pass 级**；`VGMapDedupExcluded` / `GPU-PreSkinning` 落地 | §13.2 E4–E6；`05-decisions.md` §3.15/§3.16 |
| T13 | 导入侧 **cache-only**（dump 消失 + 缓存完好 ⇒ 不重算、不写盘） | §13.2 E7；`05-decisions.md` §3.17 |
| T14 / T17 | T13 三条旧契约断言的**独立裁决**（pass）；R-T14-1 陷阱的**独立复核** | `11-t13-adjudication.md` / `12-t16-adjudication.md` |
| T16 | 修 R-T14-1（`prepare_gate_oracle` 的「自称权威、什么都没放行」） | `05-decisions.md` §3.18 |
| T19 | 站位机制规格**取证**（只取证 ZZMI/EFMI 事实，不下 HIMI 判定） | `13-stub-anatomy.md` |
| T20 | 修「子集导出被 stale 误判中止」+ 契约提示词不显示原因 | §9.6（t20 行） |
| T21 | 重新裁定 **D09**（站位机制规格 + 与 G1、与局部导出的交互） | §13.5；`05-decisions.md` §3.19 |
| T22 / T26 | 站位机制**实现** + F1（注入顺序）修复 + 真端到端 | §9.3 B7–B10 |
| T27 | 站位实现的**独立复核**（F2-new / F3-new / F6） | §7.7.11、§13.7 |
| T30 | **G1-a/G1-b 规格级矛盾的裁定**（本节 §7.7.11）+ §13 重建 + §8–§12 刷新 | §13.7；`05-decisions.md` §3.20 |

---

## 9. 验收标准

### 9.1 命令

```powershell
# 仓库根目录 E:\代码\TheHerta4
.venv\Scripts\python.exe -m pytest tests/ -q --ignore=tests/test_swap_panel_button_icon.py
#   ↑ 契约命令。T30 起测基线 = 2743 passed / 42 skipped / 151 subtests（设计冻结期为 2580）；
#     本轮结束前并发任务 t31 新增 2 条文档一致性钉子 ⇒ 复测 = 2745 passed / 42 skipped / 151 subtests
#     --ignore 的原因见 `reports/himi-merge/10-integration.md` §4.3 E1（venv 缺 PIL，HEAD 上同样失败）

# HIMI 相关（t44 复核：合计 226 passed / 56 subtests —— 实测 = 51+38+104+17+16）
.venv\Scripts\python.exe -m pytest tests/test_himi_skeleton.py -q                # 数据层 38 条
.venv\Scripts\python.exe -m pytest tests/test_himi_merged_runtime.py -q          # 导出侧计划层 + 产物断言 104 条
.venv\Scripts\python.exe -m pytest tests/test_himi_merged_contract.py -q         # 契约判定 17 条
.venv\Scripts\python.exe -m pytest tests/test_himi_stub_objects.py -q            # 站位机制 51 条（分项 10+7+3+9+2+5+3+5+7；护栏 10 / 规则① 5 / 外来槽 7 / 真端到端 ini 3）
.venv\Scripts\python.exe -m pytest tests/test_himi_clear_merged_skeleton_cache.py -q   # 清缓存 16 条

# 真实 dump 回归（只读，K: 盘零写入）
.venv\Scripts\python.exe tools\himi_merged_probe.py --self-check              # 门禁：期望 23/23 PASS，exit=0
.venv\Scripts\python.exe tools\himi_merged_probe.py --json reports\himi-merge\04-probe-results.json
```

> **命名更正（T30 刷新）**：§9.3 原先写的 `tests/test_himi_merged_skeleton_ini.py` **不存在**——INI 产物断言落在 `tests/test_himi_merged_runtime.py`（合并骨架资源/VB 段/非合并路径逐字节）与 `tests/test_himi_stub_objects.py`（站位段）两个文件里。原文件名的写法**保留未删**（本节以下表格的表头仍用它指代「导出产物断言」这一组）。

### 9.2 针对真实 dump 的回归断言（**全部可在 T4 数据上判定真假**）

| ID | 断言 | 期望值（T4 实测） | 判定来源 |
|---|---|---|---|
| A1 | 每组件 `max(BLENDINDICES) < palette 池大小` | 34/34 成立 | `04-probe-results.json` `roles[*].components[*]`：`palette_bones > index_max_used` |
| A2 | 引用槽集合长度 == distinct 索引数 | 34/34 成立 | 同上（`palette_bones - unreferenced_slots == distinct_index_count`） |
| A3 | **`palette_bones == index_max_used + 1` 必须断言为「32/34 成立」**（不得断言恒真） | 32/34；反例 = `557a7477-3966-0`（156 vs 95）、`9430996d-1656-0`（156 vs 137） | 同上 |
| A4 | 单角色全局唯一骨骼 | `{276, 343, 315, 219, 252}`（按角色名映射） | `roles[*].role_global_unique_bones` |
| A5 | 镇×偃月叩晓的「引用槽数之和」== 594（不是池和 876） | 594 | `roles["镇×偃月叩晓"].role_referenced_slot_sum` |
| A6 | 跨角色逐位重合 == 0；五角色并集 == 各角色之和 == 1405 | 0 / 1405 | `cross_role.shared_bone_signature_count`、`merged.global_union_bones` |
| A7 | 单角色最大 343 ≤ 374；五角色 1405 > 374 | `True` / `False` | `merged.max_single_role_fits_374`、`merged.all_five_roles_fits_374` |
| A8 | palette 步长 48 B：全部组件字节数被 48 整除，且**至少一个不被 64 整除** | 34/34 / ≥1 | `components[*].palette_divisible_by` |
| A9 | 每份 palette 的 3×3 = 正交 × 均匀 s，且 `s == cbrt(|det|)` | 39/39（五帧） | `04` §2.2 表；`01` §4.2 |
| A10 | 实测 s 集合 == `{1.6}`（133530/135117/135739/140134）与 `{1.0, 0.93}`（134442） | 同 | `04` §2.2 表 |
| A11 | 索引/权重格式：`BLENDINDICES = R32G32B32A32_SINT`、`BLENDWEIGHTS = R32G32B32A32_FLOAT`、每顶点 ≤ 4、索引非负 | 34/34 / 34/34 / 34/34 / 34/34 | `merged.index_format_histogram`、`channels_used_hist`、`merged.negative_index_any` |
| A12 | 组件 join 率 == 34/34；deform pass join == 18/39 | 34/34 / 18/39 | `roles[*].joined_components`、`deform_passes_joined` |
| A13 | 未 join 的 pass 与已 join 的在 blend 内容 hash / palette 内容文件 / SO 目标 hash 上零交集 | 5/5 角色 | `join_evidence[*].blend_hash_overlap_joined_vs_unjoined` 等三个字段均为 `[]` |
| A14 | 未 join 的 pass 不命中任何**其它**角色的工作空间 | `unjoined_matching_other_role_count == 0`（5/5） | `join_evidence[*]` |
| A15 | 真外来对象恰好 1 个：140134/`000035`（DrawIB `b840f29b`、SO `d2cc3481`、3334 顶点、257 骨） | 同 | `01` §8.3 |
| A16 | **G1 门控**：SO 目标只被单一 IB 家族的 render draw 消费（39/39）；家族内 SO 唯一（18/18）；放行 pass 数 == 18/39 | 同 | §2.8（T6 并入探针后由 `--self-check` 判定） |
| A17 | 唯一 SO 输出行数 == 25787/30359/27792/28187/28839（**不是**按 bare 累加值） | 同 | `04` §6.3 |
| A18 | `IndexOffset/IndexCount/VertexOffset/VertexCount` 46/46 全 0（`match_first_index` 恒 0） | 同 | `merged.no_extra_offset_fields` |
| **A18 更正**<br>（2026-09-26 / t23） | ⚠ **原文括号里的「（`match_first_index` 恒 0）」不成立（已更正，原文保留在上）**：46/46 全 0 的是那四个 JSON 字段；ini 的 `match_first_index` 来自**物体名第 3 段**（`common/object_prefix_helper.py:295-297`），**实测非零**：`60639 / 54936 / 55380 / 47199 / 1716 / 9372 / 4656` | **断言本体可保留**：`merged.no_extra_offset_fields`（那四个字段全 0）仍为 `True`；**但不得**由它推出 `match_first_index` 恒 0（两者无因果关系） | `64-matchfirstindex-forensics.md` §1.1/§1.5/§8-1；`48-forensics-artifacts.md` §4.4；**更正详情见 §13.14** |
| A19 | 逐 draw VS 取 `-vs=` 后缀：771/771 无歧义；133530/`000041` 真值 `55da5eab238de808` | 同 | `01` §7 |
| A20 | deform 块 `vs-t0` 恒 `.buf`（39/39）；render 块恒 `.dds`（732/732） | 同 | `01` §1.1 D3、`04` §7 D3 |

### 9.3 导出产物断言（`tests/test_himi_merged_skeleton_ini.py`，照 `tests/test_zzmi_merged_skeleton_ini.py` 模板）

| ID | 断言 |
|---|---|
| B1 | 骨架资源段含 `type = RWBuffer` / `format = R32G32B32A32_FLOAT` / `array = 3 × 槽位数` / `bind_flags = shader_resource unordered_access` / `filename = Meshes/<role_key>-MergedSkeleton.buf`；**不含 `stride =`**。**T30 更正**：原文写「`type = Buffer` … **不含 `bind_flags =`**」——与 §7.1/§7.3 的冻结口径**相反**，已按 2026-09-23 队长裁定更正（理由与被否决备选见 §7.1、`05-decisions.md` §3.13）；`validate_resource_decl` 逐条校验四类破坏性声明 |
| B2 | deform 重放段：`vs-t0 = ResourceHI3MergedSkeleton_<role_key>` 与 `vb1 = Resource<draw_ib>Blend` **在同一个 `[TextureOverride_..._Blend]` 段内**，且 `draw = <deform VertexCount>, 0` |
| B3 | **非合并路径**产物与改造前逐字节相同（回归基线快照） |
| B4 | 产物中**不出现**：`ResourceZZPalette_`、`ResourceZZVgMap_`、`ResourceZZRedirectSO_`、`CustomShaderZZMI`、`[PoolZZMI`、`$zz_ms_` |
| B5 | 门控 G1 未过的组件：不生成 `vs-t0` 重绑段，且 stdout/产物注释含阻塞组件清单 |
| B6 | `VertexLimitRaise` 段的 `hash` / `override_byte_stride` / `override_vertex_count` 与改造前相同（IP-16 本期不改） |

### 9.4 数据层断言（`tests/test_himi_skeleton.py`，照 `tests/test_zzmi_skeleton.py:22-74` 模板）

| ID | 断言 |
|---|---|
| C1 | 用 `K:\SSMT-Package-master\WorkSpace\HIMI\咚！炽愿吉星` + `FrameAnalysis-2026-09-23-135739` 作 fixture（路径不存在时整套 skip），识别出 **6** 个 deform pass |
| C2 | `af17c02f-11772-0` 双向命中：`CategoryHash.Position=891c0d12 → vb0`、`VertexLimitVB=2c5605ed → SO` |
| C3 | palette 载入 `1a75212f.buf` → `(41, 12)`；引用槽集合 = 41 个；`VGCount == 41` |
| C4 | `VGMap` 写回后 `_merged_skeleton_metadata_valid` 为真（`common/submesh_json.py:20-55`） |
| C5 | 幂等：二次 `ensure_skeleton_data` 不重算（cache-only 用例） |
| C6 | **负例**：对 `557a7477-3966-0` 断言「引用槽数 == 8」且「池大小 == 156」且「两者不相等」——把 D03 的口径钉死 |
| C7 | 角色级骨架：`SlotCount == 276`（咚！炽愿吉星）；骨架字节长度 == `276*48`；`Slots` 的 sha256 与字节切片一致 |

### 9.5 端到端与游戏内

| ID | 标准 |
|---|---|
| D1 | Blender headless 导入 + 导出 [PASS]；产物 ini 与 buffer 齐全 |
| D2 | **游戏内实测**（用户侧执行）：合并导出的 mod 在 HI3 下姿态正确；同角色跨组件权重生效；**同角色内 A 组件引用 B 组件骨骼时动画正确**；非合并路径无回归 |
| D3 | 游戏内 dump 复验：hook 的 deform pass `vs-t0` 无原版 dump（换绑生效），未 hook 的 pass 正常 |

### 9.6 T30 刷新：基线演进、新增断言组、ST 编号对照（2026-09-24）

**基线演进链**（同一契约命令：`.venv\Scripts\python.exe -m pytest tests/ -q --ignore=tests/test_swap_panel_button_icon.py`）：

| 时点 | 计数 | 事件 |
|---|---|---|
| 设计冻结（T5） | 2580 | §9.1 的原基线 |
| T12 | 2681 | pass 级门控 + `VGMapDedupExcluded` / `GPU-PreSkinning` 落地（多处文档引用此值） |
| T20 → T22 | 2668 → **2715** | 子集导出 stale 误判修复（t20）；站位机制实现（T22，全量 2715 passed / 42 skipped / 151 subtests） |
| **T30（本轮）** | **2743** → **2745** passed / 42 skipped / 151 subtests | 起测 = **2743**（本轮开始时实测，与任务书基线一致）；本轮只做裁定与文档更正、**不改生产代码**，但并发任务 t31 在本轮期间新增 2 条文档一致性钉子（`StubDocConsistencyTests`）⇒ 复测 = **2745**，两者全绿。探针 `--self-check` **23/23 PASS，exit=0** |
| **t33（本轮最后一条修复）** | **2745** → **2748** passed / 42 skipped / 151 subtests | 只动测试与文档（**零行为改动**）：① F-2 —— 端到端 ini 用例改用**真实装配**（成员集合由基类在 `super().__init__()` 那一刻从 `ordered` 装配给出，不再手工塞）；② 新增 `StubNailIntegrityTests` **3 条**（防删除 2 条 + 锚点漂移机械校验 1 条）⇒ +3；③ 两态复现钩子入库（`HIMI_F1_BROKEN_ORDER=1`，零写入）。探针 `--self-check` **23/23 PASS，exit=0** |
| **t35（收尾修复）** | **2748** → **2749** passed / 42 skipped / 151 subtests | 只动测试与文档（**零行为改动**）：① G-1 —— 「合格测试引用必须真实存在」护栏**纳入计划书**（+0 条断言，但覆盖面前移；两态实测：注入悬空 `Class::test` ⇒ 该护栏红）；② G-2 —— 锚点护栏的 docstring 与实现改成同一套规则（每符号**至少一个**引用命中 `def` 行）；③ G-3 —— 两态 overlay 的临时目录改为 `try/finally` 清理 + 新增「运行后无残留」断言（**+1 条**，`StubNailIntegrityTests::test_overlay_dir_leaves_no_residue`）。**全量实测 2760**，其中 **11 条**来自非本队并发文件 `tests/test_workspace_config_prune.py`（按文件归属核对，不计入本队增量）。探针 `--self-check` **23/23 PASS，exit=0** |

| **t36（blocker 修复）** | 2749 → **2769** passed / 42 skipped / 151 subtests | 只动契约与测试（**零行为改动**）：`HIMIStubDrawCall` 补齐 `DrawCallModel` 的 **9 字段 + 1 方法**（F1 修复后下游首次真正读取）+ 新增 `StubDrawCallContractTests` **3 条**。**2769 = 2763 + 6**：其中 **6 条**来自非本队并发新文件 `tests/test_direct_shapekey_multisubmesh_base_buffer.py`（按文件归属核对，不计入本队增量） |
| **t37 / t38** | 2769 → **2774** passed / 42 skipped / 151 subtests | t38（规则① 归位 + F-A）新增 `StubRule1Tests` **5 条**（t37 为纯文档）。`15-verify-stub.py` 因规则① 语义变化一度 **62/68**（F-1，交 t39） |
| **t39 / t40 / t41** | 2774 → **2781** passed / 42 skipped / 151 subtests | t41（「外来槽」判据替换）新增 `StubForeignSlotTests` **7 条** ⇒ 站位单文件 43 → **50**；`15-verify-stub.py` **73/73**（t39 改写：删除 0 条） |
| **t42（独立验证）** | 2781（不变） | 只改 `15-verify-stub.py`（不在 pytest 计数内）⇒ 该脚本 **79/79**；五角色真实副本三条不回退 |
| **t43（锚点回填 + 护栏扩展）** | 2781 → **2782** passed / 42 skipped / 151 subtests | 新增 `test_production_anchor_references_resolve`（**+1**）⇒ 站位单文件 50 → **51**；计划书 22 处 + 05-decisions 7 处锚点按实测回填 |
| **t44（本轮收口，本条）** | **2782** passed / 42 skipped / 151 subtests | 纯文档（计划书 / `10-integration.md` / `05-decisions.md`）⇒ 计数不变。**实测**：站位单文件 **51**、五文件 **226 / 56 subtests**、全量 **2782 / 42 / 151**、探针 **23/23**、`15-verify-stub.py` **79/79**、`08-verify-independent.py` **211/211** |

> 中间各值按其任务报告的原话引用（T30/T44 未重新测量）；**t44 对本行的四项计数（2782 / 51 / 226 / 23-23）与两条脚本结果（79/79、211/211）负实测责任** —— 全部为本轮自己跑出的输出。

**派生基线产物约定（t35 G-4，即日生效）**：`reports/himi-merge/08-verify-independent.json` 是 **oracle 脚本自己的输出**（**生成产物**）——其 `metadata.FrameAnalysisPath` 内嵌每次运行不同的 `%TEMP%` 路径 ⇒ **跨运行字节稳定在设计中就不可能**。因此：

* **任何验收标准不得再要求该文件的固定 sha**，一律引用**语义结果**（当前 = **211/211**，`summary = {total:211, passed:211, failed:0}`）；
* 该文件被脚本每次运行重写属**正常行为**，**无需还原**，也不应被当作越界或基线漂移；
* **不要改 oracle 脚本本身**（`reports/himi-merge/08-verify-independent.py` 是 verifier 的产物）。

完整登记见 `reports/himi-merge/10-integration.md` §7「派生产物刷新属预期行为」（该节同时承载本条约定）。

**新增断言组（实现期新增，全部已有测试锚点）**：

| 组 | 内容 | 测试锚点（T30 复核） |
|---|---|---|
| **A21** | 探针自检 23/23（§2.8 的三条 G1 统计并入探针，T6） | `tools/himi_merged_probe.py --self-check`（机器判定，exit=0） |
| **B7** | **站位段**：被吸收部件生成 `[TextureOverride_...]` 且段内 `handling = skip`；**真端到端**（`%TEMP%` 副本上跑真实 `ExportUnity.export()`，读回产物 `ws.ini`） | `tests/test_himi_stub_objects.py::StubEndToEndIniTests::test_stub_ib_override_section_reaches_the_real_ini` |
| **B8** | 站位段的两条**反例**（保证非恒真）：① 不注入 ⇒ 契约 `level=error` ⇒ `Fatal` ⇒ 不产出 ini；② 无 VGMap 的缺席部件 ⇒ 导出照常但产物里**没有**站位段 | 同上类另两条用例 |
| **B9** | **站位 bare 必须进 `export_bares`**（否则家族门永不放行 ⇒ 必然中止导出，§7.7.11.1） | `StubInjectionOrderTests::test_stub_bare_enters_export_set_to_close_the_family_gate` |
| **B10** | **注入顺序**（早于基类组装模型；T26 F1，两态实测） | `StubInjectionOrderTests::test_injection_happens_before_base_model_assembly` |
| **C11** | **规则① 逐字口径 + 多成员家族形态**（t38 建立 / t40 登记）：① 用户形态 —— 5 分块家族、`exclusive` 恒空、1 个在场 ⇒ 其余 **4 个全部**补站位（`rule=1`、`skipped_members=[]`）；② **反例** —— 整族缺席 + 零引用 ⇒ 不插桩（`rule=2`，全部进 `skipped_members`）；③ **事故防护归位** —— 同一份数据：走规则① 补站位、走规则② 被证据门控挡住（事故防护**没丢、只是归位到规则②**）；④ 规则③ 在规则① 下仍生效；⑤ **「部分在场」按 IB 身份识别**（`LOD0.2c706fbd-47199-0_copy` ⇒ `rule=1`；换成别的 IB ⇒ `rule=2`） | `tests/test_himi_stub_objects.py::StubRule1Tests`（**5 条**） |
| **C8** | 站位**数据层**判据（规则①②③ + 两条反例 + 借位反例 ST3 + 逐组件）。**t40 注**：其中 ST3 已由 t38 重建为**规则② 形态**；`exclusive` 判据本身已被裁定**替换**为「外来槽」口径（§7.7.12）⇒ 本条的实现侧断言将在 F-2 修复任务里同步 | `tests/test_himi_stub_objects.py::StubRuleTests`（10 条） |
| **C9** | **防删除 + 防漂移 + 防泄漏护栏**（t33 建立 / t35 扩展）：① 两条 F1 钉子仍在且仍断言因果属性；② 两态复现钩子在仓库里（`HIMI_F1_BROKEN_ORDER=1`）且**不留临时目录残留**（`try/finally` + 前缀守卫）；③ 文档锚点 `文件:行号` 必须解析到真实符号（每符号至少一个引用命中 `def` 行）；④ **合格测试引用**（`Class::test` / `文件.py::test`）必须真实存在 —— **扫描面含本计划书**（t35 G-1） | `tests/test_himi_stub_objects.py::StubNailIntegrityTests`（**4 条**）+ `StubDocConsistencyTests`（2 条） |
| **C10** | **鸭子类型契约完整性护栏**（t36）：`HIMIStubDrawCall` 必须覆盖真实 `DrawCallModel` 的**全部**字段与公开方法 —— 用 `dataclasses.fields` 的字段名集合（含 dataclass **声明层**）+ `dir()` 的公开方法名集合做**集合包含**检查；并配一条跑到**真实 `SubMeshModel.__post_init__`**（读 `drawcall_model_list[0].match_draw_ib`）的用例（t36 blocker 的现场） | `tests/test_himi_stub_objects.py::StubDrawCallContractTests`（**3 条**） |
| **C12** | **规则②「外来槽」判据**（t41 建立 / t42 独立验证）：事故形态（缺席族的槽被在场载体引用**且被载体声明**）⇒ 不插桩；检出形态（外来槽被引用）⇒ 逐成员补站位；`declared_by_present` 不可得 ⇒ fail-closed 不插桩。两态矩阵（M1 去掉减法 / M2 fail-closed）证明「事故保护 = 那次减法」「检出能力 = 命名空间可得」 | `tests/test_himi_stub_objects.py::StubForeignSlotTests`（**7 条**） |
| **C13** | **生产文件锚点护栏**（t43 建立）：`PROD_ANCHOR_FILES` 白名单内，「**紧邻**（≤3 字符）的反引号符号若是该文件 `def`/`class` ⇒ 单点引用 == 定义行、区间引用包含定义行」+ `文件.py::符号` 存在性；扫描面含本计划书与 `05-decisions.md`。两态：改错生产锚点 ⇒ 红并打印真值；注入不存在符号 ⇒ 红。**边界**：白名单外的生产文件（如 `common/submesh_model.py`）不自动校验 —— 裁定与理由见 §13.9 **T44-O2** | `tests/test_himi_stub_objects.py::StubNailIntegrityTests::test_production_anchor_references_resolve`（**1 条**，属 C9 的 `StubNailIntegrityTests` 计数内） |
| **E1** | **子集导出不再被 stale 误判中止**（t20）：覆盖完整家族的部分导出 ⇒ 只重绑该家族、契约 `warning` 而非 `error`；覆盖全量时摘要不一致**仍判 stale**（护栏不放松） | `tests/test_himi_merged_runtime.py::HIMISubsetExportTests`（5 条）+ `tests/test_himi_merged_contract.py`（诊断进提示词） |
| **E2** | **契约判定四档**（`ok` / `warning` / `error` / 拒绝档）+ 诊断文案（`_format_diagnostics` 限 6 条、逐条截断、与 `gate_rejected_parts` 去重） | `tests/test_himi_merged_contract.py`（17 条） |
| **F1** | **元数据字段冻结口径**（站位与 G1 的交互字段） | `tests/test_himi_merged_runtime.py::HIMIStubGateInteractionTests` + `tests/test_himi_stub_objects.py::StubGateInteractionTests` |

> **计数纪律（t33）**：上表与 §9.1 里的「N 条」以 `.venv\Scripts\python.exe -m pytest <file> -q --collect-only` 的**实测**为准（每次新增用例都可能过时）；测试锚点的**行号**由 `tests/test_himi_stub_objects.py::StubNailIntegrityTests::test_doc_anchor_references_resolve` 机械校验（改测试文件后跑一次即可定位漂移，不必手改）。

**ST 编号对照表（计划书 §7.7.8 ↔ 测试文件抬头 `tests/test_himi_stub_objects.py:6-24`）**——**两侧编号并不一一对应**，只有 ST8..ST12 一致：

| 计划书 §7.7.8 | 测试文件里的对应用例 | 一致性 |
|---|---|---|
| ST1 被吸收 ⇒ 补站位 | `test_st1_partial_family_missing_members_get_stubs`（+ 反例）——口径 = 规则① 的具体形态 | ⚠️ 相关但口径不同 |
| ST2 零引用 ⇒ 不插桩 | `test_st4_zero_reference_is_not_stubbed` | ⚠️ **错位**（测试 ST2 是「缺席家族 + 专属槽被引用 ⇒ 补」） |
| ST3 借位 ⇒ 不插桩 | `test_st3_borrowed_slot_reference_is_not_absorption` + `test_st3_positive_partial_exclusive_slot_still_stubs` | ✅ 一致 |
| ST4 `VGMapDedupExcluded` ⇒ 不插桩 | `test_st5_dedup_excluded_is_orthogonal` | ⚠️ **错位** |
| ST5 无 VGMap ⇒ 不插桩 | `test_st5_missing_vgmap_is_not_stubbed` | ⚠️ 与上一条**共用 ST5 编号**（两个 `test_st5_*`） |
| ST6 规则① 部分缺失（场景真相） | `test_st2_absent_family_with_own_slot_referenced_gets_all_members` + `test_scene_truth_declared_but_absent_still_gets_stub` | ⚠️ **错位**（测试 ST6 指 G1-a） |
| ST7 站位不进 G1（字段逐字节不变） | `test_st7_real_injection_leaves_every_gate_field_unchanged`（真注入版）；另有 `test_st7_stub_does_not_enter_target_bares`（元数据版） | ⚠️ **错位**（测试 ST7 的字面指 G1-b） |
| ST8 / ST9 / ST10 / ST11 / ST12 | 同名 `test_st8_*` / `test_st9_*` / `test_st10_*` / `test_st11_*`（2 条）/ `test_st12_*` | ✅ 一致 |

> **t40 补记（t38 之后本表需按新语义读）**：t38 把**规则①** 改成 ZZMI 逐字口径（缺失成员直接全补，见 §7.7.3）并新增 `StubRule1Tests`（5 条，`C11`）；把测试文件的 **ST3** 重建为**规则② 形态**（整族缺席 + 借位 ⇒ 不插桩）；**ST1/ST2/ST4/ST5 的语义**随规则① 变更而收窄（现在描述的是规则② 或规则① 的具体形态）。⇒ 本表「计划书 ST ↔ 测试用例」的对应关系**只作导航**；判据本体的最终口径以 **§7.7.12** 为准。

**两条「名字像原口径、断言是更正后口径」的测试（登记改名，见 §13.7 待补 ③）**：

| 测试 | 名字的问题 | body 的实际断言（与更正后口径一致） |
|---|---|---|
| `test_st6_stub_is_not_a_family_coverage_claim`（`tests/test_himi_stub_objects.py:1768`） | 读起来像原 G1-a「所有与站位有关的身份都不算覆盖」 | 站位**对象**不在 `L`（并且用例自己注明「该部件本身已落盘，故在 `L` 里」）⇒ 正是更正后的 G1-a① |
| `test_st7_stub_does_not_enter_target_bares`（同上 `:1804`） | 读起来像原 G1-b「占位不进 `target_bares`」 | `StubTargetSetBares == {"aaaa-1-0","aaaa-2-0"}`（**被站位的 bare 在目标集合里**）、只有合成对象 `zzzz-9-9` 不在 |

---

### 9.7 通用纪律（t47/t48 实机事故的教训，2026-09-25；事故记录见 §13.11）

> 本节是**对「验收标准」本身的补充约束** —— 起因：本轮出现**两条「离线全绿 + 推理自洽」但实机被推翻**的结论。

**纪律 R1（数值口径纪律）**：**凡写进 ini 的数值，必须同时给出三样东西** ——
1. 它约束的**物理量**（它到底是「多少个什么东西」）；
2. **独立实测来源**（不是从别的数字推出来的，而是量出来的：文件行数 / 资源体积 / 游戏侧 draw）；
3. **至少一条离线护栏**（能在仓库里红）。

**纪律 R2（离线链自洽纪律）**：断言不能只验「**计划书规定的形态**」，必须至少有一条验**产物物理自洽性**与**与实机量对照** ——
- 反例（本轮）：离线断言验 `vs-t0` 在场、`draw` = 家族 Σ、IB 段形态，**从无一条**把 `draw` 与模组自己的 buffer 行数对比（`99475` vs `31066`，差 **68409** 行、**零红**）；`08-verify-independent.py` 甚至把错误前提**固化成断言**（t48 已改正 ⇒ 215/215）。
- 必备不变量示例：`draw ≤ 该族 buffer 行数`；`SO 写入行数 == 渲染索引覆盖行数`；`Blend 行数 == Position 行数 == override_vertex_count`。
- **「实测常态」必须比对外部真值**：`04` §1 的 34/34 是 json **内部两字段**相等，不是与游戏真实 draw 比。

**纪律 R3（关闭纪律）**：**离线「全绿」只证明「与计划书一致」，不证明「实机正确」** —— 任何 blocker 的发现在修好后，**必须要求实机复测**才能宣布关闭；「工具里没有命中」也不能当反证（本轮 `MergedSkeleton` 零命中被三条独立证据推翻）。

---

## 10. 风险与后续可选

### 10.1 风险清单

| # | 风险 | 等级 | 缓解 |
|---|---|---|---|
| R1 | **`vs-t0` 重绑 + `vb1` 覆盖在非索引 `Draw` 上的运行时行为未实机验证** | **高** | 验收 D2；先做单组件最小验证（1 个 IB 家族）再全角色 |
| R2 | **合并骨架的 typed 声明与游戏 deform VS 的读法不匹配**（若游戏按结构化读） | **高** | 待验证 V2（打开 shader dump 核对 `dcl_resource`）；失败特征 = 模型爆炸/顶点零 |
| R3 | **顶点顺序不变量 O3 未独立验证** | 中 | 验收 A5（逐顶点比对）；若失败则必须先解决顺序保持，否则整个运行时方案不成立 |
| R4 | 同帧多实例（同一组件在一帧内出现多次 deform pass） | 低（五帧 0 例） | 检测到即拒绝合并该组件并大声报警 |
| R5 | 门控 G1 导致用户「想合并的子集」被阻塞（家族里含未落盘组件） | 中 | 显式诊断 + 给出「把家族内全部组件一起纳入」的指引；**不得**静默降级 |
| R6 | 复选框默认 True：`ensure_skeleton_data` 失败必须静默降级（打印 + 不阻断导入），不得让「无 dump 的老工作空间」导入失败 | 中 | 复用 C15 的回退形状 |
| R7 | 组号移位塌陷（删面/删空组 → 全局 id 移位） | 中 | 复用 C17 的「无条件补缺 + 不删任何组 + 排序后恒等重命名 + 非数字组名告警」；**保持 HIMI 不进 `remove_unused_vertex_groups`**（D16） |
| R8 | 全局槽位 > 65535 时的静默截断 | 低（实测 343） | `_himi_cache_intact` 与导出前断言上限；SINT 可承载 2³¹−1 |
| R9 | 合并骨架与工作空间几何的空间不一致（−90°X / 1.6× 缩放） | 中 | 骨架是游戏空间量、不参与旋转（D12）；门控必须 scale-aware（§5.6 见下） |

### 10.2 刚性质心门控的 HIMI 口径（**必须改，不能照搬**）

ZZMI 的门控（`zzmi_skeleton.py:656-667`）触发条件是「命中对任一方是单骨骼刚性部件」，判据是 `||hitter_centroid − owner_centroid|| < 0.05`（米）。HIMI 不能照搬，因为：

1. **palette 带 1.6× 均匀缩放**（4/5 角色），而工作空间几何是 1.0×（`04` §7 D7）；0.05 m 阈值搬到 1.6× 空间里**等效变成 0.03125 m**。
2. HIMI 的 `part_rigid` 判据不能再用 `len(palette) == 1`（那是池大小），必须用**被引用槽数 == 1**（D03）。
3. HIMI 导出还做 **−90°X** 旋转（D12），几何质心与 palette 平移列不在同一坐标系。

**HIMI 口径（冻结）**：

```text
门控触发：命中对任一方「被引用槽数 == 1」（单权重组件）
空间对齐：把 palette 侧质心用 A⁻¹ 变换回几何空间（A = 该 palette 的 3×4 块，含均匀缩放 s），
          或等价地按 1/s 归一化后比较（s 由 HIMIPaletteScale 提供，**禁止硬编码 1.6**）
阈值：沿用 ZZMI 的 0.05（几何空间米）；两侧都必须在同一空间比较
缺签名：保守拆开（与 ZZMI 同：只拆不并是安全方向）
双方多骨骼：不加门控（与 ZZMI 同）
```

> **待验证 V4**：HIMI 是否真的存在「抓帧瞬间不同锚点骨矩阵逐位重合」的情形。`04` §4 实测跨角色重合 0，但**同角色内**的刚性重合未专项取证。若不存在，门控可以简化；**但不得先删**（ZZMI 的教训成本极低，保留只拆不并的方向是安全的）。

### 10.3 后续可选（本期不做）

- 跨角色骨架合并（需先解决 1405 > 374 的容量问题与角色切换时机）。
- 合并骨架的跨工作空间共享池（同角色多 LOD）。
- 通道骨判定（`common/zzmi_channel.py` 移植；需先取证 HIMI 的跨部件共享骨图）。
- B 模式降级：导出时按 `VGMap` 反映射回局部索引（零运行时依赖，但跨组件权重被越界剔除）。
- 多实例身份对齐（需要 HI3 加载器侧信号，本期未调研）。

### 10.4 实现期新增风险（T30 刷新，2026-09-24）

| # | 风险 | 等级 | 状态 / 缓解 |
|---|---|---|---|
| **R10** | **规格与实现双口径**：G1-a/G1-b 的原文字面与实现相反（**被站位的 bare 必须进 `T`**）。最坏后果是「照原文改实现 ⇒ 重新引入**必然中止导出**的死锁」 | **高 → 已收口（含文字部分，t31 关闭）** | T30 裁定**方案 (A)** + §7.7.11 更正 + §13.7 台账 + `05-decisions.md` §3.20 否决表；反证实测 `.dbg/t26_g1b_deadlock_probe.py`（T30 复跑确认）。**文字部分已由 t31 关闭**：`ui/universal/himi.py` 模块 docstring 的旧段（原 `:48-55`，含引用不存在的测试名）**整段删除**并改写为反转后的口径、`:403-411` 末句改为「当前口径是不排除」、`common/himi_skeleton.py` 的元数据注释块写死「这是 `prepare()` 的数据层口径，不等于导出侧 `export_bares`」；并新增护栏 `tests/test_himi_stub_objects.py::StubDocConsistencyTests`（**两态实测**：注入旧措辞 ⇒ 1 failed；注入旧测试名 ⇒ 2 failed；还原后 ⇒ 2 passed）。⇒ **本半条风险已关闭**；仍开放的只有 §7.7.11.6 待补 ① 的**字段改名 + `StubGateInteractionVersion` 1→2**（见 R11，不改行为） |
| **R11** | **字段名反向陈述**：`StubTargetBaresIncluded=False` / `StubFamilyCoverageCounted=False` 在 (A) 下字面反向，易被读成「站位不进 `T`」 | 中 → 已登记 | 改名 + 改值 + `StubGateInteractionVersion` 1→2（§7.7.11.6 待补 ①）。字段**无生产消费者**、不在 `_himi_cache_intact` 判据里 ⇒ 改名零缓存代价 |
| **R12** | **ST 编号两套**（计划书 §7.7.8 vs 测试文件抬头；测试里有两个 `test_st5_*`） | 中（追溯性） | §9.6 对照表 + §7.7.8 注记；改名/重编号登记（§13.7 待补 ③）。**不影响任何行为**，只影响「按编号找测试」的效率 |
| **R13** | **站位判据取保守方向** ⇒ 借位/证据不可构造时**不插桩** ⇒ 可能残留可见重影 | 中（**有意的取舍**） | §7.7.4 的方向论证：**少压 = 可见重影（用户立刻发现）vs 多压 = 静默部件消失（用户以为提取失败）**；诊断逐成员打印「注入/跳过 + 原因」（§7.7.7 S6） |
| **R14** | **站位件与真实部件同名**的残留自愈时序 | 中 → 已有钉子 | ST10（清理**早于** `scene_present` 构造）：`tests/test_himi_stub_objects.py::StubGateInteractionTests::test_st10_stale_stub_is_purged_before_present_set` |
| **R15** | **子集导出的 stale 判定**（t20 修好后仍有覆盖性前提） | 中 → 已修 + 钉子 | 只在「本次导出覆盖全部已落盘带数据部件」时才用骨架摘要/槽位数判 stale；不覆盖 ⇒ 采用 prepare 的 pass 级结论 + 「本次是子集导出」诊断；**两道部件级护栏未放松**（逐部件摘要 + palette 缓存逐字节核对），见 §9.6 E1 |
| **R16** | **`CONTEXT.md` 未补 HIMI 词条**（H9 未完成） | 低（文档欠账） | 登记在 §8 落地状态表；不影响功能交付 |

---

## 11. 待验证项（显式清单，不得混入结论）

| ID | 待验证内容 | 验证动作 | 影响 |
|---|---|---|---|
| V1 | **合并骨架是静态的**（bind 姿态逐帧不变）——本方案「无 CS、静态骨架」的前提 | 抓两帧不同姿态的 dump，比对同一组件的 palette 是否不同（若不同 ⇒ 骨架必须逐帧更新，本方案需回退到 ZZMI 式的 attach 通道，而 HIMI 无 CS ⇒ 必须另找挂载点） | **若 V1 不成立，本方案的核心简化失效**。<br>⚠ **2026-09-25 / t69 回写：V1 已实测证伪 ⇒ 本条状态从「待验证」升级为「已证伪 / 阻断」** —— 验证动作**已执行**：同一 palette 资源哈希 `vs-t0=23de2d6a` 跨 **14** 个抓帧内容 **14/14 各不相同**，对照静态资源 `vb0=1a6d79fc` 跨 10 个抓帧 **10/10 相同**；且**合并骨架 ≡ 当帧五族 palette 的拼接**（`reports/himi-merge/27-static-vs-perframe.md` §1/§1.3；`28-perframe-attach.md` §5.2 的 CF-1 = 9/9）。⇒ 骨架**必须逐帧更新**。**本行预测的一半是错的**：挂载点**已找到并落地**（**mod 侧** `[CustomShader…]` attach CS + deform 段顶层 `copy vs-t0` 捕获），**不需要另找** —— 原推断把「HIMI **游戏侧**无 CS/Dispatch（五帧 0 命中）」当成了「mod 侧没有可挂载通道」。 |
| V2 | HIMI deform VS 按 `palette[3b+m]` typed 索引读骨架 | 打开 HI3 shader dump，核对 `10905ed856e6b621` 的 `dcl_resource` 与索引表达式 | 决定 §7.1 的资源声明形态（R2） |
| V3 | mod 的 Blend 顶点缓冲行 `i` == 游戏 SO 行 `i`（原始顶点顺序） | 用真实工作空间 + dump 做逐顶点比对（验收 A5） | 决定运行时方案是否成立（R3） |
| V4 | HIMI 同角色内是否存在「抓帧瞬间不同锚点骨逐位重合」 | 用五个角色的 palette 做同角色内跨组件 bitwise 重合统计（`04` 只做了跨角色） | 决定刚性门控是否必需（§10.2） |
| V5 | 「SO 输出位置 = 1.6 × 输入位置」 | dump 里**没有** SO 输出缓冲区的独立 dump（`SOSetTargets` 目标只以 hash 出现，`deduped/` 无对应 `.buf`），需重抓带 SO 输出的 dump | 仅影响诊断；不影响 §10.2 的门控（门控只用 palette 侧证据） |
| V6 | 每 pass 两笔 `SOSetTargets`（`pOffsets` `…E478`/`…E468`）的成因 | 需要逆向或加载器源码 | 仅登记（`01` §11.1）；本方案只依赖「带 hash 续行的那笔 = SO 目标」（34/34 一致） |
| V7 | `04` §0.5/§1.2/§8 的「31/34」口径 | 已复算（§2.4）：可复算值是 32/34（池 == imax+1）与 33/34（pool_exact_fit）；差异不影响实现动作 | 已记入 `05-decisions.md` §2.2 |
| **V8** | **合并骨架资源声明 = `RWBuffer` + `filename` 且本期无 UAV 写入方**（本仓**新组合**：ZZMI 的 `RWBuffer` 一直有 attach CS 写入） | 实机**各跑一次 N 卡与 A 卡**，观察 deform pass 的 `vs-t0` 是否被正确绑成 SRV。T8 已核出：HLSL 无可执行 `Dispatch`、仓库无任何 ini 引用该资源或该 HLSL（0 处） | **阻断级**：缺 `bind_flags` 的失败模式是**静默**的（SRV 绑 NULL ⇒ 顶点塌到原点），且只在两张显卡中的一张上暴露（§7.1/§7.3 A1） |
| **V9** | ~~draw 顶点数的**安全夹取分支**（mod 顶点数 > deform pass 顶点数）~~ ⇒ **已由 t48 反转前提**：`draw` **无条件采用模组自己上传的顶点数**，家族 Σ 只进诊断注记 | 真实数据上两者相等 ⇒ 旧「夹取」是空操作；**t47 实机证明越界写会写爆角色级 SO 池**（多写 68409 行）⇒ 保留的唯一实机项 = 造一个「模组自身顶点数 > 游戏单分片 draw」的工作空间，观察 SO 尾行行为（**注意**：这不是「取较大值」的旧分支） | 低（`resolve_deform_draw_count`，§7.4 第 1 条；方向已按 t48 反转） |
| **V10** | **多 bare 家族的行数求和** | 实测 7 个 IB 家族被拆成多子网格、分片区间首尾相接；实机观察多 bare 家族的合并 mod | 中（写错 ⇒ SO 只写前 N 行 ⇒ 模型残缺） |
| **V11** | 站位机制的**实机有效性**：1e-6 小三角在 HI3 下是否真的不可见、被吸收部件的原版 draw 是否真的被压掉 | 实机导入 + 导出 + 游戏内观察（与 V1/V2/V3/V8 同批） | **站位机制的唯一实机判据**（本环境只能到「产物里确实有 `handling = skip` 段」，见 §9.3 B7） |
| **V12** | HIMI 的 `LOD0/DrawIB-Component.json` 是否**总是**该角色全部候选（= 完整家族名单） | 已实测五角色均存在（家族数 8/8/7/6/9）且含未落盘成员；需在更多角色上复核 | 若某角色缺该文件 ⇒ 站位判定退化为「不可枚举家族成员 ⇒ 不插桩」（fail-closed，`HIMI_STUB_REASON_NO_FAMILY_MAP`） |
| **V13** | 站位件的 IB override 段（`drawindexed = 3, 0`）在 HIMI 的**非索引 deform** 语境下是否需要额外声明 | 实机观察（HIMI 的 deform 是 `Draw(VertexCount)`、渲染是 `DrawIndexed`；站位只需要渲染侧画出 3 顶点） | 低（本环境只能断言「段存在且含 `handling = skip`」） |
| **V14** | 站位对象的权重组恒为槽 0 这一结论（由**桥创建为唯一组** + 组名常量 `HIMI_STUB_SLOT_GROUP_NAME = "0"` ⇒ 索引 0）在**多权重组**的边界下是否仍成立 | 合成用例：给站位对象人为加第二个权重组 ⇒ 断言要么组名仍连续合法、要么实现显式报错（**不得静默产生非法组名**）。**t45 实测补充（重要）**：HIMI **无**导出期自检 ⇒ 人为加第二组后**实现不会报错**，导出的索引会变成 0/1 之一（登记为 **T45-F4**，可选加固） | 低（当前实现：单组 ⇒ 索引恒 0 ⇒ 槽 0，永远合法） |
| **V17** | **运行期骨架块内容**：attach 那一刻骨架块里装的到底是不是该族**当帧**自己的 palette —— **本轮最能解释症状的唯一未观测环节** | 用户抓一帧 **s5b**（先关游戏 → 双击 `HIMI探针_4_切到s5b_palette可见.bat` → **再启动游戏** → 角色展示页 F8 → 退出）→ `.venv\Scripts\python.exe .dbg\t15_palette_vs_skeleton.py "<FrameAnalysis 目录>"`。**判据已预注册**（阈值/分支优先级在抓帧之前写定，不得事后调参） | **高**：t10 量化「错喂 palette」的代价 = **99°–178° 旋转 + 0.24–1.78 单位位移**，其中 `70a4da3e ← b35cfc6c` = **178.06° 绕 +Y、位移 0.4859** ⇒ **同时**覆盖用户描述的「沿 Y 180°」与「上移约半个身体」。**2026-09-26 / t7 登记**（`reports/himi-merge/63-t7-closing.md` §1.3/§2.2；判读管线 `61-s5b-analysis-pipeline.md`） |
| **V18** | **`cs-t1`（`ResourceHI3AttachRange_<fam>`）的实机加载与 SRV 绑定** —— **本轮修复新引入**、从未实机跑过 | **U2-a**：`findstr /C:"AttachRange" "K:\SSMT-Package-master\3Dmigoto\HI3\d3d11_log.txt"` ⇒ 期望 **5 条** `filename=Meshes/<fam>-AttachRange.buf`（**0 条时先确认该日志是不是「当前这次运行」写的**；**不要拿 FrameAnalysis 目录当搜索对象** —— 它从不记录 mod 资源名，T21-F1 实测）<br>**U2-b**（行为两态）：备份 5 个 `<fam>-AttachRange.buf` → 把 5 个载荷的 **base 全改成 0**（span/magic 不动）→ 进游戏：**可见崩坏 = 身份资源确实被读取**；**外观完全不变 = 退回冻结表**。做完用备份还原并核 sha256 | **中**：失败时 CS **退回冻结表**（= 旧行为，**不新增「模型消失」模式**），但用户会失去本轮 F5 加固的全部保护而**无感知**。**2026-09-26 / t7 登记**（`63-t7-closing.md` §4；`53c-t21-review-round2.md` §6.1） |
| **V19** | **K1 / K3 / K5**：触发键在该帧的命中数 == 1 / 命中笔的 SO == 本族 SO 哈希 / 键不得取自渲染 draw 的哈希 | 需要 FrameAnalysis 的 draw↔VB/SO 绑定表（cache-only 重建路径没有 dump）⇒ **导出期判不了**；运行时/实机一帧即可判 | **中**：本角色 4 帧实测成立（绑定 `4349c385` 各 1 笔且同一笔；三帧 SO 全 = `61facde5`），但**没有导出期门**。**2026-09-26 / t7 登记**（`51-fix-implementation.md` §3.7/§8-U7） |
| **V20** | **`vb2` 的 motion-vector / 双缓冲语义**（t11 §4.3 用「它是游戏的速度 pass 源」**解释**掉，非实测） | 一帧 dump 比对 `vb2` 与相邻 SO 的时间关系（`.dbg/t2_parse_frame.py` 口径） | **低**：本轮裁定**不改**（重绑 = 换语义；收益 0.04% 身高）。**2026-09-26 / t7 登记**（`51-fix-implementation.md` §3.4） |
| **V21** | **合并渲染 4 个 pass 的材质错配在实机画面上的可见后果**（主渲染 6 笔套头发贴图） | 一帧 dump 比对合并渲染那 9 笔的 `ps-t0/1/2` 哈希与各族原笔 | **中**：t11 定性「**确定的错色、但不产生几何错位**」；**没有人看过实机画面** ⇒ 用户可能把串色描述成「错位」。**2026-09-26 / t7 登记**（`57-render-read-forensics.md`；`51` §8-U3） |
| **V22** | **动画 / 多帧下的陈旧量级** | 连抓 **3 帧同一动作** → `.venv\Scripts\python.exe .dbg\t13_so_provenance.py <目录…>`（t17 起支持目录参数）比逐帧 SO 与当帧骨架差；**> 5% 身高 ⇒ 重新成为候选** | **中**：t4 关闭「帧内陈旧」的依据是**单帧静态**的 0.25% 身高（68.5% 槽 / 70.2% 顶点滞后）；运动姿态下可能放大。**2026-09-26 / t7 登记**（`51-fix-implementation.md` §4.6；`62b-t17-changelog.md` §1） |
| **V23** | **别的角色 / 别场景**（F5「池 > 引用槽」通道、F6 诊断、5 个身份资源） | 同一工作流导出**第二个角色**：看 F6 是否报诊断（表不认识某 span ⇒ 大声诊断不阻断）、5 个 `*-AttachRange.buf` 是否生成、产物注释块里有无 `; 警告` | **中**：本角色实测 `池 == 引用槽 == span` ⇒ **F5 通道不活**（本轮加固在本角色上属"备而不用"）；报告 04 记录过两个「池 > 引用槽」的实例。**2026-09-26 / t7 登记**（`51-fix-implementation.md` §3.1；`53-fix-review.md` §7-R5） |
| **V24** | **`94c8cea0` 的 BC7 转储** | 一次性脚本（未分析） | **低**：与本次错位无已知关联，登记备查。**2026-09-26 / t7 登记**（`53-fix-review.md` §7-R8） |
| **V25** | **`223923` / `224222` 两帧的模组态归属**（是 s1 系还是 s3；合并 deform 是否真的落盘） | **t14 的判别抓帧包**：同镜头先 `HIMI探针_9_切回原状.bat`(s0) 抓一帧、再 `HIMI探针_2_切到s1_基准.bat`(s1) 抓一帧，间隔越短越好 → `.dbg\t13_so_provenance.py` 看「闭合 / 不闭合 / 全零」 | **中**：t12 判「不是 s0、不是 s3、落在 {s1,s2,s4}」；**t13 反驳**（`vb1 = 993728` 只证明段被匹配、不证明段尾 `draw` 执行）⇒ **s3 未被排除、模组态未确证**。**2026-09-26 / t7 登记**（`58-t12-palette-visible-and-frame-state.md` §2.3；`57-t13-merge-deform-trigger.md` `:22`/`:244`；`63-t7-closing.md` §1.3 末行） |
| **V26**（**已核实，非待验证**） | **`match_first_index` 的来源与唯一性**：值来自**物体名第 3 段**（`common/object_prefix_helper.py:286-299`，其中 **`:295-297` 取 `parts[2]` 作 `first_index`**），**不是** submesh JSON 的 `IndexOffset`、**也不是** `.blend` 属性 `3DMigoto:MatchFirstIndex`（后者恒 0 且无导出侧消费者） | **已核实（t22）**：12/12 段与两帧实测 draw **精确吻合** —— 140134 覆盖 **46/46**、泄漏 **0**；212119 接管 **68/68**、**0 笔对不上任何段**；12 个 `(hash, MFI)` 键**两两互异**；**无键命中不同 `IndexCount` 的 draw**、**无键从不触发**；同 bare 内 `StartIndexLocation` **累加且互异** ⇒ 键在同一 IB 内唯一确定一个子网格 | **无需实机**。**登记在此的理由**：F10 / D13 / A18 曾把它写成反的（三处「更正」行 + §13.14）。**2026-09-26 / t23 登记**（`reports/himi-merge/64-matchfirstindex-forensics.md` §1.1/§5） |
| **V27** | **（t22-U2）`<IndexCount>-<IndexOffset>` 在 SSMT 包内的写入点未定位** | SSMT 包（`K:\SSMT-Package-master`）**不在本仓** ⇒ 本次只读未展开；已证明的是**导出侧消费的就是这个文件名**（`common/ssmt_import_helper.py:41`），而**不是** `JsonDict.IndexOffset` | **低**：不影响导出正确性（消费侧已实测 12/12 精确命中）；只影响「名字是怎么来的」这一溯源。**2026-09-26 / t23 登记**（`64-matchfirstindex-forensics.md` §7-U2） |
| **V28** | **（t22-U4）别的帧 / 别的角色是否会出现 `(hash, match_first_index)` 跨物体碰撞** | 只在**两帧**（140134 / 212119）内做了穷举（**均无碰撞**）；`hash` 是 IB 的**内容哈希** ⇒ 理论上两个内容完全相同的 IB 会碰撞，**未做全库排查** | **低**：碰撞的后果是「另一个物体的一笔 draw 被误吃」（部件级漏吃/多吃，或整块消失当且仅当头发段失配），**不产生整块错位 / 不产生 180°**。要关闭需在更多角色/场景上重跑 `.dbg\t22_mfi.py`。**2026-09-26 / t23 登记**（`64-matchfirstindex-forensics.md` §7-U4） |
| **V29** | **（t22-U6）`match_first_index` 在 3dmigoto 内部与 `StartIndexLocation` 的比较语义** | 依据是「SSMT/ZZMI 既有约定 + 本帧 46/46 + 68/68 的实测吻合」；**未从 3dmigoto 源码确认**其内部比较的是 `StartIndexLocation` 还是含 `BaseVertexLocation` 的复合量（本帧所有目标 draw 的 `BaseVertexLocation` 均为 0 ⇒ **两者不可区分**） | **低**：不可区分 ⇒ 当前结论不受影响；要关闭需读 3dmigoto 源码，或构造一个 `BaseVertexLocation ≠ 0` 的帧。**2026-09-26 / t23 登记**（`64-matchfirstindex-forensics.md` §7-U6） |

> **V26–V29（2026-09-26，t23 新增）**：**V26 = 已核实项（非待验证）**，登记在此是为了给 F10 / D13 / A18 的「`match_first_index` 恒 0」更正留一条**正面入口**；**V27–V29 = t22 的三个未取证项（U2 / U4 / U6）**，各配「一个可判定实验」。**不重编号既有 V1–V25，不删除任何既有行**；更正详情见 **§13.14**。

> **V17–V25（2026-09-26，t7 收口新增）**：九条实机 / 运行时待验证项，来源 = `reports/himi-merge/63-t7-closing.md` §1.3（仍未验证清单）与 §4（实机校准清单，含 U2 的**修正版实验**）；每条都配「一个可判定实验」。**不重编号既有 V1–V16，不删除任何既有行**。其中 V18 的实验措辞经 `53c-t21-review-round2.md` §6.1 更正（原写法指向 FrameAnalysis 目录 ⇒ 必然 0 命中 ⇒ 假阴性）。

> **V15（**已关闭**，t48/t49，2026-09-25）**：原登记为「重绑段 `draw = 99475`（家族分片求和）的实机核对 —— base 段写 `31054`」。**结论：base 的 `draw_number` / `31054` 才是正确量纲，rebind 的 `99475` 就是本轮实机 blocker 本身。** 口径已按 §7.4 第 1 条的更正落地（`draw` ← 模组自己上传的顶点数；`vs-t0` 仍应在场）；`08-verify-independent.py` 里把「draw = 家族 Σ」固化成断言的那一项也已在 t48 改正（oracle 211 → **215/215**）。**⇒ t39 的 O-1（F-3）随之关闭**（见 §13.11）。**注意**：本条关闭的是「量纲口径」，**不是**实机症状 —— 症状在改量纲后**仍存在**（t49 假设①被推翻）。
>
> **V16（t44 新增，登记在 `reports/himi-merge/10-integration.md` §4.1，本表不重编号）**：`common/submesh_model.py::calc_buffer`（`:94`）的**完整顶点缓冲路径**未被离线覆盖 ⇒ 需真实工作空间完整导出后核对产物 `.buf`（Blend 行数 / BLENDINDICES / `vb1`+`vs-t0` 同段）。

---

## 12. 与 `reports/himi-merge/01..04` 的一致性核对表

| 本计划书结论 | 来源 | 一致 |
|---|---|---|
| deform pass = 8/8/7/6/10，render = 143/157/147/126/159 | `01` §2 | ✅ |
| deform VS = `10905ed856e6b621` 39/39；逐 draw VS 取 `-vs=` 后缀 | `01` §3.6/§7 | ✅ |
| `palV48 == imaxUsed+1` 仅 37/39（pass 口径）；恒真的是 `>=` | `01` §4.1 | ✅（本计划书另给组件口径 32/34，见 §2.4） |
| 取 palette 只能按 `(draw id, vs-t0, Buffer, .buf)`，绝不按资源 hash | `01` §5.3 | ✅ |
| 五帧零 CS/Dispatch | `01` §6 | ✅ |
| 未 join 的 pass 语义 = 「不属于本次落盘的合并目标集合」；真外来对象仅 140134/`000035` | `01` §8.3 | ✅（**不使用「外来角色」措辞**） |
| palette 3×3 = 正交 × 均匀 s，`s = cbrt(|det|)`，39/39；s = 1.6/1.0/0.93 | `01` §4.2、`04` §2.2 | ✅ |
| HIMI 无 `*SetConstantBuffers1`；`SkeletonGroup` 恒 0；`ObjectCB1*` 不生成 | `02` §5.1、任务书冻结项 | ✅ |
| SO 目标就是该部件的渲染 VB；每 pass 两笔 `SOSetTargets` | `02` §5.2 #2/#4、`01` §3.6 | ✅ |
| 组件 34（json 46）；单角色唯一骨骼 276/343/315/219/252；共享槽 235/253/252/168/149 | `04` §0.5 C3/§3 | ✅ |
| 跨角色重合 0；五角色并集 1405；单角色最大 343 ≤ 374 | `04` §4/§5.1 | ✅ |
| 48 B/12 float；16-float 被结构性否决（15/34 不被 64 整除） | `04` §2.1 | ✅ |
| 索引/权重格式统一；每顶点 ≤ 4；索引非负 max=251；无需升宽 | `04` §5.2 | ✅ |
| 组件 join 34/34；deform pass join 18/39 | `04` §6.1 | ✅ |
| 同一 IB 多 bare 共享同一 SO；唯一 SO 行数 25787/30359/27792/28187/28839 | `04` §3.1/§6.3 | ✅ |
| 门控 G1（SO 家族独占 39/39、家族内 SO 唯一 18/18、放行 18/39） | §2.8（T5 新增只读验证） | ✅（**T6 已并入探针**：`--self-check` 23/23 PASS） |

**实现期新增事实的核对（T30 刷新；本节前段核对的是「计划书 ↔ `01..04`」，这一组核对「计划书 ↔ `05`/`11..19` 报告与实现」）**：

| 本计划书结论 | 来源 | 一致 |
|---|---|---|
| G1 从家族级升级为 **pass 级**（`family_safe and pass_safe`；同 pass 任一消费者家族不完整 ⇒ 该 pass 上全部组件不合并） | `05-decisions.md` §3.15（T10 裁定、T12 实施）；§7.4 | ✅ |
| `VGCount = max(被引用局部槽下标)+1`；`VGMap` 完整覆盖 `0..VGCount-1`；空洞槽 → `槽位数 + 局部下标` | `05-decisions.md` §3.14；§13.1 E1 | ✅（**计划书原口径错、实现对**） |
| 合并骨架资源 = typed `RWBuffer` + `bind_flags = shader_resource unordered_access`；`type = Buffer` + 省略 `bind_flags` 的写法**被否决** | `05-decisions.md` §3.13；§7.1/§7.3 | ✅（§9.3 B1 已同步更正） |
| 站位机制：两条作用分离（A 依赖骨架形态 / B 不依赖）与 D09 更正 | `13-stub-anatomy.md` §0/§4.3；§7.7；`05-decisions.md` §3.19 | ✅（T22 已实现，T26 端到端，T27 独立复核） |
| **G1-a/G1-b 更正**：**站位 bare 进 `T`**、站位对象不进 `T`、`is_stub` 只读 | §7.7.11；`05-decisions.md` §3.20 | ✅（T30 裁定；实现已按此落地 ⇒ 不需改代码） |
| 子集导出不再被 stale 误判中止（t20）：不覆盖全量 ⇒ 采用 prepare 的 pass 级结论 + 显式诊断 | §9.6 E1；`tests/test_himi_merged_runtime.py::HIMISubsetExportTests` | ✅ |
| 契约诊断进提示词（`_format_diagnostics`：过滤规格行、去重、限 6 条、逐条截断） | §9.6 E2；`tests/test_himi_merged_contract.py` | ✅ |
| 导入侧 cache-only（dump 消失 + 缓存完好 ⇒ 不重算、不写盘；带 `target_bares` 的请求必须拒绝） | `11-t13-adjudication.md`；`10-integration.md` §6.3 | ✅（T13 实施、T14 裁决 pass） |
| 基线计数 **2743 →（t31 新增 2 条钉子后）2745 passed / 42 skipped / 151 subtests**；探针 **23/23** | §9.6（T30 两次实跑） | ✅ |
| 计划书自身结构：`^## ` 章节列表 **§1–§14 连续无缺** | §13 重建（T30） | ✅（T30 交付前自查） |

---

## 13. 实现期偏离 / 修复 / 口径更正台账（T6–T49 回写）

> 本节是**计划书与最终代码的对账台账**：左「计划书原口径」= 设计冻结时的写法；右「最终口径」= 已落地并有测试/实测支撑的实现。**所有更正的理由都在这里，避免后来者只看到结论看不到原因。**
>
> **重建说明（T30，2026-09-24）**：本节在上一轮的「段落重排重建」事故中**整节丢失**（当前文档一度只剩 §1–§12 + §14）。T30 按队长从 designer 会话日志取证导出的 5 段唯一碎片（`.dbg/_s13_fragments.md`，含原 L252 / L624 / L644 / L806 / L1342 的台账正文）+ `reports/himi-merge/05-decisions.md` + `reports/himi-merge/10-integration.md` 重建。**编号约定**：碎片里 D09 自我更正记在 13.6、台账缺陷记在 13.5；T30 按本轮验收要求把 **D09 记录固定为 §13.5**（本轮最重要的一条可追溯记录），台账缺陷顺延为 §13.6，并新增 §13.7 记录本轮的 G1-a/G1-b 裁定。
> **行号口径**：表中行号是 **T30 复核（2026-09-24）时的快照**；若与当前不符，**以函数名/字段名为准**（行号会随实现演进漂移）。

### 13.1 计划书错、实现对（3 条）

| # | 计划书原口径 | 最终口径 | 理由 | 落地位置（T30 复核） |
|---|---|---|---|---|
| E1 | §5.1「`VGCount` = 被引用槽数、`VGMap` 键集 = 被引用槽集合」 | **`VGCount = max(被引用局部槽下标) + 1`；`VGMap` 完整覆盖 `0..VGCount-1`**（被引用槽 → 全局槽位；**空洞槽 → `槽位数 + 局部下标`**） | 导入侧硬校验：`common/submesh_json.py:53` 的 `set(normalized) == set(range(vg_count))` 不成立 ⇒ 元数据判无效；`common/mesh_create_helper.py:451-457` 的 `actual_keys != expected_keys` ⇒ `Fatal`；`:527` 的 `component.vg_map[int(i)]` 缺失 ⇒ `Fatal`。**照原口径实现，首个 HIMI 组件导入即崩** | `common/himi_skeleton.py:879-901`（槽位分配循环）；`tests/test_himi_skeleton.py::test_real_referenced_slot_totals_are_pinned` |
| E2 | §4.2「palette 原始字节 = 该组件 palette 被引用槽按 48 B/骨切片后的字节」 | **「按引用序压缩的槽表」**：行 `j` = 第 `j` 小的被引用局部槽；`BoneMatrixSlotCount` = 被引用槽数 | 池含未引用槽 ⇒ 「池镜像」会把行号错位；导出侧必须用同一「引用序」解析并与角色骨架**逐字节**核对 | `common/himi_skeleton.py:796`（`slots = sorted({…})`）；`common/himi_merged_runtime.py:2672`（`read_palette_cache` 逐字写明「按引用序压缩的槽表」）与 `:1931`（对回全局槽位）；`common/himi_merged_runtime.py:2696`（`verify_palette_cache_against_skeleton`） |
| E3 | §5.1 `BoneMatrixSlotCount` 语义写「= 去重前池内**被引用槽数**，用于大小校验」+ §9.4 C4 只写「`VGMap` 写回后 `_merged_skeleton_metadata_valid` 为真」 | 语义同上，但**校验判据必须真的调用该函数**：`_merged_skeleton_metadata_valid(payload) is True`，并补三个负例 | 原 C4 的测试只断言 payload 键值、**从未调用校验函数** ⇒ 无法证伪原口径（T9 F6） | `tests/test_himi_skeleton.py` 的 `_load_merged_skeleton_validator()` + 2 条用例 |

### 13.2 计划书未写、实现补上（5 条）

| # | 计划书缺口 | 实现补上的内容 | 落地位置（T30 复核） |
|---|---|---|---|
| E4 | §5.1 只把 `VGMapDedupExcluded` 写成「沿用 ZZMI」的输入字段，未写落地形态 | **已实现**：为真 ⇒ 该组件**独占自己的槽位**（恒等映射、不注册 owners）、写回 json、记入角色级 `DedupExcludedComponents`。**实现纪律**：单趟严格按 `sorted(bare)` 处理（原「排除组件先占坑、其余后处理」两趟实现会让槽位分配依赖「谁带排除标记」——而该标记是上一次写回的产物 ⇒ 第二次运行算出不同 `VGMap`、破坏幂等与缓存门） | `common/himi_skeleton.py` 的 `fresh`/`frozen_slots` 分支（t43 复核 `common/himi_skeleton.py:879-901`）；`tests/test_himi_skeleton.py::test_dedup_exclusion_is_idempotent_across_runs` |
| E5 | §5.1 只把 `GPU-PreSkinning` 写成「是否参与合并判定（46/46 全 true）」 | **本期补的是「读取」语义**（该字段是**输入**、不是本方案新增产物）：显式 `False` ⇒ 不参与合并、记入 `CPUPreskinningSkipped`、**不把同 pass 的其它家族拖成 missing**（`family_gate` / `pass_families` / `gate_allows` 带 `skip_bares`）、也不因缺合并元数据被误判需修复 | `common/himi_skeleton.py:408`（`gpu_preskinning`）、`common/himi_skeleton.py:2503-2505`（组件级参与门）、`common/himi_skeleton.py:567-594`（`family_gate(skip_bares=)`）、`common/himi_skeleton.py:598`（`pass_families`）/ `common/himi_skeleton.py:612`（`gate_allows`） |
| E6 | §7.4 G1 只写「家族全部已落盘组件都在目标集合里」 | **pass 级升级**：判据 = `family_safe and pass_safe`（`common/himi_skeleton.py:2527`）；新增 reason `deform_pass_family_incomplete` 与角色级字段 `DeformGateBlockedPasses` / `DeformGateMode = "pass"`。理由：重绑按 **pass** 生效，同 pass 被多家族共享时只要有一个家族不完整，该 pass 上的**全部**组件都不得进入合并骨架 | 同上；`tests/test_himi_merged_runtime.py` 的 `TestPassLevelGate` 3 条；`05-decisions.md` §3.15 |
| E7 | §4.2/§4.3 未写导入侧 cache-only 的落点 | 见 §4.2 的 cache-only 表（T13）；最终状态另见 `reports/himi-merge/10-integration.md` §6.3 与 `11-t13-adjudication.md`（T14 裁决 pass） | `common/himi_skeleton.py:2397`（`cache_only_plan`）、`common/himi_skeleton.py:2224`（`_himi_cache_intact_without_dump`）、`common/himi_skeleton.py:2988`（`_himi_cache_intact`） |
| E8 | §5.2 角色级元数据字段表未列全 | 补全实现全集（含 `GateVersion` / `ComponentSet` / `DeformGateBlockedPasses` / `DeformGateMode` / `CPUPreskinningSkipped` / `DedupExcludedComponents` / 站位字段等） | §5.2 表；`common/himi_skeleton.py` 的 metadata 构建段（t43 复核 `common/himi_skeleton.py:2749-2827`） |

### 13.3 实现期自查出的两处自身缺陷（已修 + 回归锁死）

| # | 缺陷 | 修复语义 |
|---|---|---|
| E9 | 「排除去重」两趟实现 ⇒ 槽位分配依赖上一次写回的产物 ⇒ **第二次运行算出不同 `VGMap`**，破坏幂等与缓存门 | 改**单趟**严格按 `sorted(bare)` 处理；`test_dedup_exclusion_is_idempotent_across_runs` 钉死「两次 prepare 的 VGMap 逐字相同、第三次 ensure 命中缓存完整」 |
| E10 | 槽位号取过 `len(signature_to_slot)`，在「同一签名占多个槽」时会与已分配槽**撞号并覆盖记录** | 改为 `len(slot_records)`（已分配槽数）；空洞槽基准改为 `max(max(mapping.values()) + 1, len(slot_records))` |

### 13.4 有意偏离（T7 四处，队长已裁定成立、写法保留）

见 §7.1（**D1** 资源声明：typed `RWBuffer` + `bind_flags = shader_resource unordered_access`）、§7.4 第 1 条（**D2** draw 顶点数 —— **t48 更正：唯一权威 = 模组自己上传的顶点数（`draw_number` / `override_vertex_count`），家族 Σ 只用于诊断、不夹取**；见 §13.11）、§7.4 **D3**（dump 不可用时不做门控重算，`gate_source=cache-only`；带 `target_bares` 的请求必须拒绝 cache-only）、§7.6 **D4**（不生成 palette 资源、只保留形状守卫）。**D1 同时作为被否决备选方案记入 `05-decisions.md` §3.13**（原写「静态只读 ⇒ 只做 SRV ⇒ 不需要 `bind_flags`」，被队长裁定更正）。

### 13.5 设计冻结期的错误裁定：D09「3 顶点 stub 不适用」（自我更正记录，2026-09-23 发起 / 2026-09-24 按 T19 取证重新裁定）

> **这是本计划书最重要的一条自我更正记录，不得静默改写。** 用户在实测中发现 HIMI 完全没有站位机制，追责到 D09。

| 项 | 内容 |
|---|---|
| **原裁定（错）** | §3.2 的 D09 把「合并几何重定向（carrier/target/`base_vertex` 自检 / **3 顶点 stub**）」**整体**列进「不移植」，理由 = 「HIMI 的『合并』只体现在顶点组命名空间与骨架槽位，**不改变任何 draw 的几何拓扑**」。§1.3 非目标同步照抄；`05-decisions.md` §3.9 把 stub 写进「方案」描述并连坐否决 |
| **错误性质** | **推理链断在第一环**：从「合并不改几何拓扑」直接跳到「所以 stub 不适用」，跳过了中间环。T19 取证（`reports/himi-merge/13-stub-anatomy.md` §0/§4.3）把站位拆成**两条彼此独立的作用**：**A = 让缺席部件的 deform pass 在 mod 里跑起来、把当帧 palette 送进合并骨架**（依赖「骨架是否静态」）；**B = 抑制原版 draw 防重影**（**不依赖**骨架形态）。**D09 的理由只覆盖了 A 的推理**（HIMI 静态骨架 ⇒ A 无对应 ⇒ 这一半判对了），**完全没有覆盖 B**。ZZMI 原文（`ui/universal/zzmi.py:872-890`）写明的真实因果链是：**合并让跨 IB 权重合法 ⇒ 用户自然会跨 IB join ⇒ 被吸收部件的原版 draw 在游戏里仍然存在 ⇒ 必须用站位让 mod 生成它的 IB override 段（`handling = skip`）把它吞掉，否则重影**。**关键一跳**：HIMI 的 IB override 段只为「场景里存在对象的部件」生成（`ui/universal/unity.py:75-125`）⇒ 部件缺席 ⇒ mod 里没有它的 IB 段 ⇒ 原版那一笔照跑 |
| **为什么「可预见」** | 这条链的每一环都不依赖 ZZZ 特有的数据结构：① 合并让跨 IB 权重合法 = 「统一顶点组」的定义（HIMI 同）；② 用户跨 IB join = 同一套工作流、同一个复选框（HIMI 同）；③ 原版 draw 仍存在 = 游戏自己的绘制表决定（HIMI 同，**与 SO 是否重定向、骨架是否静态全都无关**）。**只看「HIMI 无 RedirectSO」+「静态骨架」就否决 stub，等于用前半段（A 与重定向）的结论覆盖了后半段（B）** |
| **成立的那一半（不连坐）** | `_build_merged_mesh_redirect_plan` / carrier+target 拆分 / `base_vertex` 自检 / `RedirectSO` 资源 **确实不移植**（HIMI 的 SO 就地写渲染 VB，没有第二块 SO） |
| **后果** | HIMI 在 D09 期间**没有站位机制** ⇒ 跨 IB join 后原版 draw 与合并几何**重影**。这是当时的**已知缺口**（`reports/himi-merge/10-integration.md` §4.2 I6）。**已于 T22 实现修复**，端到端产物断言见 §9.3 B7–B10 |
| **更正后的完整裁定** | 见 **§7.7**（站位机制，11 个小节）：① 两条作用分离与「为什么错」（§7.7.1）；② 作用 B 必要 + 4 个用户可复现场景 S-1..S-4（§7.7.2）；③ 与 ZZMI 三条规则 + 两条反例逐条对照（§7.7.3）；④ HIMI 的「自属声明段」等价物 `exclusive(bare)` + 两条退化情形（方向与 ZZMI **相反**，理由写死）（§7.7.4）；⑤ 站位对象的构造/标记/生命周期（§7.7.5）；⑥ 与 G1 的五条交互 G1-a..G1-e（§7.7.6，**G1-a/G1-b 已由 §7.7.11 更正**）；⑦ 与「快速局部导出」的关系 S1..S6（§7.7.7）；⑧ 验收标准 ST1..ST12（§7.7.8）；⑨ 待验证 V11..V14（§7.7.9）；⑩ 尊重 T19 §7 四条边界（§7.7.10）；⑪ **G1-a/G1-b 的规格级矛盾与更正裁定（§7.7.11，T30）** |
| **可追溯性** | D09 原文措辞保留在 §3.2 的「ZZMI」与「HIMI 实测」两列（**未删**）；更正以「拆成两半」的形式写在「必须重写为」列并指向 §7.7；§1.3 的非目标条目已加更正注记；`05-decisions.md` §3.19 单列本条 |
| **责任说明** | 本更正由队长发起（追责链：`02` §7.2 → 计划书 D09 → 定稿 → 审查 pass），**链上每一环都有责任**；本条记录的是「设计阶段应当发现而未发现」的失误，**不是实现偏差** |

### 13.6 台账缺陷（t13 的 ledger `output` 是占位文字）

`t13` 的任务 ledger `output` 字段是一句占位文字（「占位探测：仅用于确认 verify 门禁是否读 exitCode」），**不是它的真实交付描述**。**权威记录 = T13 的交付说明消息 + `reports/himi-merge/11-t13-adjudication.md`**（T14 明确不采信该 `output` 字段）。**教训：提交占位探测以穿过 verify 门禁是错误做法**——正确做法是停下来报告（说明门禁为什么过不去、需要什么裁定），用无信息的文字让 `verify` 通过会让台账失去可追溯性、后续裁决只能依赖旁路消息。终态不可改，故登记在案（另见 `reports/himi-merge/10-integration.md` §6.3）。

### 13.7 T30：G1-a/G1-b 的规格级矛盾（自相矛盾 / 功能级死锁）与更正裁定

| 项 | 内容 |
|---|---|
| **被更正的原裁定** | §7.7.6 的 **G1-a**（「占位不算家族覆盖」）与 **G1-b**（「占位不进 `target_bares`」）——它们把「站位**对象**」与「被站位的 **bare**」混为一谈 |
| **矛盾（自相矛盾 / 功能级死锁）** | 站位判据只在「缺席部件**有** `VGMap` 且专属槽位被引用」时插桩 ⇒ 能插桩的缺席部件**必然已落盘**；而 G1 家族门是 `L(ib) ⊆ T`、`L(ib)` 取**已落盘**组件（`common/himi_skeleton.py:469` `family_members`）⇒ 若按原 G1-b 把站位 bare 排除出 `T`，被吸收成员**永远** `missing` ⇒ 家族**永远**不 `safe` ⇒ 契约 `level=error` ⇒ `Fatal` **中止导出**。**能触发站位的场景集合 ⊆ 原 G1-b 必然中止导出的场景集合** ⇒ 站位机制永远不可能生效 |
| **反证（实测）** | `.dbg/t26_g1b_deadlock_probe.py`（T26 留下，**T30 已复跑确认**）：`G1-b 字面（排除站位）: T=['aaaa-1-0'] L=['aaaa-1-0','aaaa-2-0'] missing=['aaaa-2-0'] family_safe=False` / `ZZMI 语义（含站位）: missing=[] family_safe=True`；`t27` 独立复核该反证链成立，并确认 ZZMI 同语义（`ui/universal/zzmi.py:1008-1013`：站位 DrawCall 被追加进 `ordered` ⇒ 身份自然进计划 ⇒ 家族门放行） |
| **裁定** | **方案 (A)**：**被站位的 bare 进 `export_bares` / `target_bares`**，`is_stub` 保留为**只读标注**（`himi_stub_bares()`），**不参与过滤**；**站位对象不进 `T`**（`StubObjectInTargetSet = False`）；`StubGateInteractionVersion` 1 → 2。完整理由、决定性问题的答案与顾虑处置见 §7.7.11 |
| **被否决方案（B）** | `target_bares` 排除站位 + 家族门把站位 bare 视为**已覆盖**（需改 `prepare(target_bares=…, stub_bares=…)` 与 `prepare_gate_oracle` 签名）。**否决理由**：① 判据不再是纯几何事实，而要依赖「注入动作的产物」；② 至少 4 处改动面（两个函数签名 + 数据层过滤 + 导出侧调用点 + 测试）；③ 引入 ZZMI 没有的**双口径**（`target_bares` 与 `stub_bares` 两套身份集合会随演进漂移，本仓已有 ZZMI 同类教训）；④ 家族门结论会依赖站位判定，而站位判定又要在门控之后执行 ⇒ 需额外论证判定顺序无相互影响。**记入 `05-decisions.md` §3.20 否决表** |
| **实现侧确认** | ✅ **实现已按 (A) 落地**：站位 bare 进 `export_bares`（`ui/universal/himi.py:436-462`，唯一过滤口径 `vg_count > 0`）、`is_stub` 只读（`:455-456`）、反证链写进 `_himi_export_bares` docstring（`:439-453`）；probe 实测家族门放行（`missing=[] family_safe=True`）。**结论：本裁定不需要改任何生产代码** |
| **待补（T30 复核时点状态，见 §7.7.11.6）** | ① 元数据字段**改名 + 改值**（`common/himi_skeleton.py:2811-2812`）——**仍未改**，保留为后续可选（t31 已改为「补口径注释消歧」，裁定接受）；② `ui/universal/himi.py` 模块 docstring 与 `_himi_export_entries` docstring 的旧口径 + 引用不存在的测试名——✅ **已由 t31 修复**，并新增 `StubDocConsistencyTests` 两条护栏；③ `common/himi_skeleton.py` 注释块——✅ **已由 t31 修复**；④ 两处测试名易误读、⑤ ST 编号两套——❌ 未处理，登记为后续可选 |
| **同步位置** | 计划书 §7.7.6（G1-a/G1-b/G1-e 三行）、§7.7.5、§7.7.8（ST2）、§7.7.10（边界①）、§7.7.11（本节正文）、§13.5（指向 §7.7.11）；`reports/himi-merge/05-decisions.md` §3.20 |

### 13.8 t38 / T40：规则① 归位 + F-2（规则② 判据退化）与 F-3（规格同步）台账

| 项 | 内容 |
|---|---|
| **t38 的实质修复（已落地）** | ① **规则① 归位**：家族部分在场 ⇒ 缺失成员**直接全补**，不做 `exclusive`/`used`/`VGMap` 门控，**唯一保留规则③**（ZZMI 逐字口径；原实现把 `exclusive` 门控放进逐成员循环 ⇒ 多成员家族永不插桩）；② **F-A 身份归一化**：`present_ibs` ⇒ 「部分在场」按 IB 身份识别（合并对象 `_copy` 后缀）。两态实测（真实 `嗨♪爱愿妖精♥` 副本）：修复前该家族 1 个 IB 段、站位段 0；修复后 5 个 IB 段、**4 个站位段**（段内 `handling = skip`）。 |
| **t38 的 blocker（F-1，未关闭）** | `reports/himi-merge/15-verify-stub.py` 由 68/68 变 **62/68**：6 条 FAIL 全在它的 A 组，且**逐条断言的是 t38 之前的规则① 语义**（部分在场 + `exclusive` 空 ⇒ 不插桩）。该脚本不在 t38 inScope（属 verifier）⇒ 交由 **t39（verifier-b）** 按新语义改写（本任务复核时该脚本 mtime 已更新为 22:25，说明 t39 正在改）。**本任务（T40）不碰它**（其 sha 变化不属本轮）。 |
| **F-2（high）：规则② 判据退化** | T40 独立复核（`.dbg/_t40_f2_probe.py`，五角色只读）：仅 `嗨♪爱愿妖精♥` 有落盘数据（5 家族 / 12 组件）；**三个多成员家族**（`2c706fbd` 5 / `694025c6` 3 / `def0c0ae` 2）**每个成员 `exclusive` 恒空** ⇒ 现行规则② 判据对它们**结构性失效**（≠ 保守）；当前与合并对象命名空间重叠 = 0 ⇒ 现状不插桩是对的、风险是**潜在**的。**裁定：需要修，判据替换为「外来槽」口径**（见 §7.7.12；候选比较与否决表在 §7.7.12.3，同步进 `05-decisions.md` §3.22）。 |
| **F-3（medium）：规格同步 6 处 + 2 处计数** | 已在本轮同步（§7.7.3 规则①/反例①/反例②、§7.7.8 ST2/ST3、§7.7.4 两条退化情形；§9.1 计数 38→**43**、合计 213→**218**），并新增 §7.7.12（F-2 的完整裁定）与 `05-decisions.md` §3.23。**原文措辞一律保留**，以「⚠️ 更正」注记的形式改写并指向新裁定。 |
| **T40 的三条附带纠正** | ① §7.7.4 的「家族数 8/8/7/6/9」**角色顺序未标注**（实测按 咚！炽愿吉星 / 嗨♪爱愿妖精♥ / 愈生佑翎 / 窈窕谍影 / 镇×偃月叩晓 = 6/9/8/7/8，同一多重集）⇒ 已在输入表内标注；② §7.7.3「两个反例②不是同一件事」的防误读注记；③ `exclusive` 判据在多成员家族上恒空的事实写进 §7.7.4 的退化情形表（原先只写「保守」，未写「结构性失效」）。 |
| **未关闭清单（交接）** | F-2 的实现（数据层判据替换 + ST13/ST14a/b 用例 + `15-verify-stub.py` 同步）；F-1（`15-verify-stub.py` 6 条期望，t39）；测试文件两个**方法名**易误读（§7.7.11.6 待补 ④）；`HIMI_STUB_REASON_NO_EXCLUSIVE` 的用户可见文案（§7.7.12.4）；`CONTEXT.md` 词条（H9 文档欠账）。 |

### 13.9 t38–t43 台账：规则① 归位、规则② 判据替换、文档漂移回填与护栏扩展（2026-09-24，t44 收口）

> 本节按 §13 体例补记**本轮（站位机制轮）**6 条任务的偏离 / 修复 / 更正。**原文措辞一律保留** —— 相应的就地更正注记已落在 §7.7.3（规则① / 反例① / 反例②）、§7.7.4（两条退化情形 + 输入表）、§7.7.8（ST2 / ST3 / ST6 + ST13 / ST14a-b）、§7.7.12（F-2 判据）、§3.1（C17）、§4.4（顶点组预处理）、§6.4（IP-22）、§7.7.5（权重组名机制）。

| # | 任务 | 性质 | 记录 |
|---|---|---|---|
| **T38** | repair | 实现修正 | ① **规则① 归位**：`exclusive` 门控原在逐成员循环里、对规则① 也生效 ⇒ **多成员家族永不插桩**（同族 100% 共享槽位是常态；用户实机 `2c706fbd` 5 分块只导出 1 个合并对象、另 4 个无占位）⇒ 改为 **ZZMI 逐字口径**（部分在场 ⇒ 缺失成员**直接全补**，唯一保留规则③）。② **F-A 身份归一化**：`in_scene` 原按 bare 精确匹配 ⇒ 合并对象（`_copy` 后缀）识别不到 ⇒ 新增 `present_ibs`（按 **IB 身份**判定，对齐 ZZMI `_normalize_merged_object_drawcalls`）。两态：修复前 **1 个 IB 段 / 0 站位段** → 修复后 **5 段 / 4 站位段**（真实副本）。**同时登记 F-2（规则② 判据退化）与 F-3（规格同步 6 处 + 2 处计数）**。 |
| **T39** | verification（裁决 F-1） | 独立裁决 | `15-verify-stub.py` **62/68 → 73/73**：6 条 FAIL 全在 A 组、逐条断言 t38 之前的规则① 语义 ⇒ 裁为**预期语义变化**，fixture 改成「整族缺席」落规则②（**删除 0 条**）+ 新增 5 条。独立复算 t38 成立；**队长追加的 `vs-t0` 推论链实测为「结果 1」**（fixed 的 VB Blend 段含 `vs-t0 = ResourceHI3MergedSkeleton_*`、`draw = 99475, 0`）。 |
| **T40** | requirements | 裁定 + 文档 | **F-2 成立，且范围比 t38 报的更大**（独立实测：仅 `嗨♪爱愿妖精♥` 有合并数据；`2c706fbd`(5)/`694025c6`(3)/`def0c0ae`(2) 三个多成员家族**全部 `max_exclusive = 0`**；单成员 `70a4da3e`=71 / `b35cfc6c`=19 判据有效）。**判据替换为「外来槽」口径**（`own(m) ∩ (used − declared_by_present)`），否决「家族级 `own(family) ∩ used`」与「全量值域 ∩ used」（§7.7.12.3 否决表）。F-3 六处措辞 + 计数同步。 |
| **T41** | repair | 实现 F-2 | 新增 `declared_by_present_slots(...) -> set 或 None`（身份归一化 + **fail-closed**）与 `foreign_used` 判据；`is_drawib_absorbed(..., declared_by_present=None)`；`decide_stub_injection` 内部算好并传入 ⇒ **导出侧签名一字未改**。两态矩阵（M1 去掉减法 / M2 fail-closed）证明「事故保护 = 那次减法」「检出能力 = 命名空间可得」。 |
| **T42** | verification | 独立验证 + 脚本改写 | `15-verify-stub.py` **73 → 79/79**（删除 0 条）；真实副本三条**不回退**；**关键净结论见下**。留 4 条 low 观察项（T42-O1..O4，本节点名登记）。 |
| **T43** | repair | 文档 + 护栏 | t41 的 **+183 行位移**使计划书 **22 处** `common/himi_skeleton.py` 锚点失效，而旧护栏**零告警**（只把行号归给**测试**符号、生产引用只做界内检查）⇒ 按当前文件**逐个实测**回填（另有 `himi_merged_runtime.py` 7 处、`05-decisions.md` 7 处）；**扩展护栏** `StubNailIntegrityTests::test_production_anchor_references_resolve`（白名单 4 份 HIMI 生产文件 + 扫描面含 `05-decisions.md`；规则 = 「**紧邻**（≤3 字符）的反引号符号若是该文件 `def`/`class` ⇒ 单点必须 == 定义行、区间必须包含定义行」+ `文件.py::符号` 存在性）。**两态实证**：改错锚点 ⇒ 1 failed 并打印**真值**；注入不存在的符号 ⇒ 1 failed；还原 ⇒ 逐字节相同。 |

**t42 的四条 low 观察项（登记与 t44 裁定）**

| 编号 | 内容 | t44 裁定 |
|---|---|---|
| **T42-O1**（空 `present`） | `present` 为空时 `declared_by_present` 退化为空集 ⇒ `foreign_used = used` | **不是现行缺陷（不可达）**：导出侧 `present_bares` 与 `used` **同源同迭代**（都来自 `_himi_ordered_draw_calls()` 且排除站位）——`present_bares` 为空时要么回退到 `declared_present`（非空），要么导出集合为空 ⇒ `used` 也为空 ⇒ `foreign_used = ∅` ⇒ 不插桩（符合规则② 语义）。**但它是公共 API 层的 fail-open 缺口**（直接调 `decide_stub_injection(present=∅, used≠∅)` 会造出意外插桩，与 R-T14-1 同型）⇒ **登记后续小修**：`present` 空 ∧ `used` 非空 ⇒ `declared_by_present` 返 `None`（fail-closed）+ 一条单测。 |
| **T42-O2**（两处消费必须同源） | 判据输入被「家族级重算」与「调用方 `foreign_used`」两处消费 | **登记为维护注意事项**（**当前不是缺陷**）：`declared_by_present` 只算一次并传入，两处读同一集合；且**成员证据 ⊆ 家族证据**（成员命中 ⇒ 家族必命中）⇒ 唯一可能出现的是「`absorbed=True` 但某成员无 stub」，而那正是**逐组件设计的正常输出**（其余成员记入 `skipped_members`）。**纪律：改一处必须同时改另一处**。 |
| **T42-O3**（跨族共享槽一次性检查） | 五角色逐家族 `own` 并集**两两相交全为空** ⇒ 漏检前提实测不存在 | **采纳为登记项**：检查点 = **新角色首次导入后**（或探针扩展 `--role-audit`）；判据 = 逐家族**已落盘**组件 `own` 并集**两两相交为空**；若不为空 ⇒ 打印「规则② 的漏检前提可能成立」提示（并注明**规则① 不受影响**）。实现登记给后续任务（`tools/` 不在本轮 in-scope）。 |
| **T42-O4**（措辞限定） | t41 披露的偏差 (a) 措辞需补限定 | **采纳更正（落在本节）**：「传 `declared_by_present=None`」对 `stub_members` 断言**确实不翻转**，但**会改 `reason`**；若用例同时断言 `reason`（如 ST14a 断言 `ZERO_REFERENCE`），M2 下**仍会红 —— 红在 reason，而不是误插**。t41 的报告 `reports/himi-merge/23-t41-verification.md` 不在本轮 in-scope，**原文不删**，更正以本条为准。 |

**t42 的关键净结论（本轮最重要的正面结论之一，t44 引用）**

> **「已知漏检」在用户工作流上不可达** —— ① **同族 join** ⇒ 合并对象在场 ⇒ `present_ibs` 含该 IB ⇒ **规则① 无条件全补** ⇒ 不漏；② **跨族 join**（如把 `694025c6` 的几何 join 进合并对象）⇒ `foreign_used = 18` ⇒ **可检出**；③ 漏检所需的「两个不同家族共享 canonical 槽」前提在五角色实测**不存在**。⇒ 会漏的只有「在场对象声明了某个缺席组件的槽位」，而**它蕴含同 IB ⇒ 走规则① ⇒ 不漏**。
>
> **三条不回退（t42 实测）**：`2c706fbd` `rule=1` / IB 段 **5** / 站位段 **4**；`VB_2c706fbd_0_Blend` 含 `vs-t0` + `draw = 99475, 0`；`694025c6` / `def0c0ae` `rule=2` / `zero_reference` / `stub=[]`。

**t44（本轮收口）的两条裁定 + 一条新发现**

| 编号 | 内容 | 裁定 |
|---|---|---|
| **T44-O1** | §3.1 矩阵 C17 行引 `common/submesh_model.py:385-436`（`_prepare_zzmi_merged_skeleton_vertex_groups`）：**既陈旧又语义歧义** | **拆开引两个符号**（已在 §3.1/§4.4 就地改）：**实现** = `_prepare_merged_skeleton_vertex_groups`（`:747-797` —— 真正的补缺 / 排序 / 恒等重命名 / 非数字组**移除并打印**）；**ZZMI 壳** = `_prepare_zzmi_merged_skeleton_vertex_groups`（`:798-810` —— **纯委派**，函数体只有一行调用）。**不把行号简单改成 798**：那会把「壳 = 实现」的歧义固化；壳的 docstring 与实现另有细节差异（docstring 写「不剔除任何组 / 非数字组名只警告」，实现**会移除**非数字组）⇒ 一并登记给该文件 owner。 |
| **T44-O2**（锚点护栏白名单边界） | `PROD_ANCHOR_FILES` 只含 4 份 HIMI 生产文件 ⇒ 计划书引到的其它生产文件（如 `common/submesh_model.py`）不受自动校验 —— 这正是 O-1 能长期存在的原因 | **不扩，保持现状**。理由：① 非本队文件**正被并发会话编辑**（本轮实测 `common/efmi_skeleton.py`、`common/submesh_model.py` 等由其它会话改动）⇒ 把它们的行号纳入本队门禁会让门禁与**别人的编辑**耦合、产生**假红**；② 计划书对这些文件的引用是**证据快照**（引用当时核过的行号，与 `ANCHOR_FILES` 明确排除历史报告同一逻辑）；③ 缺口已用替代手段覆盖：`.dbg/_t43_anchor_audit.py`（逐条列出引用 / 最近符号 / 真值行号）+ 本节写明「机器校验只覆盖 4 份 HIMI 生产文件」。**登记可选后续项**：给非白名单引用加「复核日期」标注（不阻塞）。 |
| **T44-F1**（新发现，medium）→ **已由 t45 裁定 / t46 回写** | 计划书 **3 处**（IP-22 ✅、§4.4 顶点组预处理行、§8 H6）声称「把 HIMI 纳入 `run_merged_skeleton_preprocess`」，但 `common/submesh_model.py:362-371` 的门**只认 EFMI（`:365`）/ ZZMI（`:369`）**，**没有 HIMI 分支**（HEAD 版本亦然，且该文件未被本队修改） | **裁定（t45，`24-t44f1-investigation.md`）：文档错（机制归属说错）＋ 实现结果正确** —— 站位件**确实**落在全局槽 0，但机制是「桥只建一个组 + 组名常量 `"0"` ⇒ 索引 0」（导出写的是**组索引**），**不是** `_prepare_merged_skeleton_vertex_groups`；且槽 0 是家族既有槽 ⇒ 不新增槽/值、不影响重绑。**t46 已回写 9 处**（§7.7.5 / §7.7.4 / §7.7.8 ST8 / §7.7.9 V14 / §11 V14 / §8 H6 ×2 / §3.1 C17 / §4.4 / IP-22 ❌未做），并修正生产 docstring（`common/himi_skeleton.py` 的 `blender_create_stub_object`）。详见 **§13.10**；登记在 `05-decisions.md` §3.25/§4。 |

### 13.10 t45 / t46：T44-F1 的裁定与回写 —— 「结果正确、机制归属说错」（2026-09-24）

> **裁定（t45，`reports/himi-merge/24-t44f1-investigation.md`）：文档错（机制归属说错）＋ 实现结果正确。** 本节记录裁定要点、9 处回写、以及 t45 发现的**可选加固 T45-F4**。原文措辞保留（各处的更正注记已就地落在 §7.7.5 / §7.7.4 / §7.7.8 ST8 / §7.7.9 V14 / §11 V14 / §8 H6 ×2 / §3.1 C17 / §4.4 / §6.4 IP-22）。

**① 核心事实三条（写进 §7.7.5）**

| # | 事实 | 实测依据（t45） |
|---|---|---|
| **ⓐ 结果正确** | 站位件**确实**落在**全局槽 0** | 真实桥 `blender_create_stub_object('2c706fbd-3207-60639')` 建出的对象：`group_count = 1`、`[{name: '0', index: 0, indices: [0,1,2]}]`；把该 mesh 喂给 HIMI 分支真正调用的 `get_blendweights_blendindices_v3`（`common/obj_buffer_helper.py:679-680` 的 else）再过产物同款 `_parse_blendindices` ⇒ `distinct_slots = [0]`、decoded 全 0、packed 48 B |
| **ⓑ 不新增槽 / 不新增值 / 不影响重绑** | 槽 0 是 `2c706fbd` 家族**既有**槽 ⇒ 加不加站位件的 0，命名空间判定都是 `global` | 家族 `own = 184 个 [0,183]`（密集从 0）；角色 `SlotCount = 343` |
| **ⓒ 机制不是那个预处理** | 真正的链 = **桥只建一个组**（`common/himi_skeleton.py:1712`）+ 组名常量 `HIMI_STUB_SLOT_GROUP_NAME = "0"`（`:79`）⇒ 单组索引恒 0；导出写的是**组索引**（`[g.group for g in groups]`）⇒ 产物 = 0 | 运行时求值 `LogicName.HIMI = 'HIMI'`、`is_zzmi_family(HIMI) = False`、`branch_efmi = False`、`branch_zzmi = False` ⇒ `run_merged_skeleton_preprocess = False`；且该预处理对单组对象本来就是**恒等操作** ⇒ 跑不跑结果相同 |

**② 回写清单（9 处，t46 已全部落位）**：§7.7.5（机制链 + 三条核心事实）→ §7.7.4（影响表）→ §7.7.8 ST8 → §7.7.9 V14 → §11 V14 → §8 H6（任务表 + 落地状态两行，达成路径改为「导入期密集建组 + BI16 + 事后复核」）→ §3.1 C17 → §4.4 → **IP-22 状态由 ✅ 改 ❌未做（本期不做）** + 「将来若做」的验收标准。

**③ 生产 docstring 同错（T45-F2）已修（t46）**：`common/himi_skeleton.py` 的 `blender_create_stub_object` docstring 原写「export-side `_prepare_merged_skeleton_vertex_groups` … renames to `str(index)` ⇒ a single-group object is always group `"0"`」⇒ 改为「this bridge creates the **only** group named `HIMI_STUB_SLOT_GROUP_NAME` ⇒ index 0；导出按**组索引**写 BLENDINDICES；HIMI 不运行该预处理且它对单组是恒等」。**改动为 docstring-only 且行数中性**：sha `cdd69820c2d05425` → **`2abb96339074e6fc`**（**3080 行不变** ⇒ 下游 14 处 `common/himi_skeleton.py:N` 锚点零位移）；**逆向替换后逐字节回到旧 sha**，且**剥离 docstring 后的 AST 与旧版完全相同**（证明零代码改动，脚本 `.dbg/_t46_proof_docstring_only.py`，exit 0）。**注**：第一次改写多出 6 行，被 §9.6 的生产锚点护栏**当场抓出 8 处漂移** ⇒ 改成「同 4 行」的写法后护栏复绿（这正是 t43 护栏的用途）。

**④ T45-F4（可选加固，只登记不实现）**：HIMI **缺导出期 `index == int(name)` 自检**（该自检只在 `_prepare_merged_skeleton_vertex_groups:790-796`，HIMI 不跑它）。
- **触发条件**：恰好出现「数字顶点组名与 `index` 不一致」的工作空间 —— 例如**用户手工加/删/重排数字顶点组**，或导入期密集建组被绕过（非标准工作空间、外部改过的 `.blend`）。
- **风险**：导出的 BLENDINDICES 用的是**组索引**（`[g.group for g in groups]`），名称与索引不一致时**静默错位**（权重指向错骨骼）—— 站位侧还叠加「人为给站位对象加第二个权重组 ⇒ 索引变 0/1 之一，且**实现不会报错**」（见 V14）。
- **现有兜底是部分的**：`check_exported_blend_namespace` 只在 verdict == `local` 时阻断，**`indeterminate` 放行** ⇒ 「未编辑 + 带合并数据」之外的形态不会被挡。
- **建议**：在导出侧（或数据层）补一条与 §7.7.5 同源的断言（仅当对象带 `HIMI_STUB` 标记或 `vg_count > 0` 时）——**登记为可选加固，不阻塞交付**。

### 13.11 t47 / t48 / t49：实机 blocker 的根因、修复、**被推翻的假设**与两条通用纪律（2026-09-25）

> 本节是**方法论价值最高**的一节：本轮有**两条**「离线全绿 + 推理自洽」的结论被实机推翻。记录它们**为什么在当时看起来成立**、以及**被什么推翻**，比记录修好的代码更重要。

**① 主表（根因 / 修法 / 证据 / 状态）**

| # | 任务 | 根因 / 内容 | 修法与证据 | 状态 |
|---|---|---|---|---|
| **T47** | 诊断（实机症状：**无骨骼约束 + 错位 + 上下颠倒 + 部件随意旋转**） | deform 重放段（`vs-t0` 段）的 **`draw` 写错量纲**：必须是**模组自己上传的顶点数**（同产物 `override_vertex_count` = `31066 / 9 / 3 / 3 / 6`），却写成**家族各分片 `OriginalVertexCount` 之和**（`99475 / 10089 / 3789 / 1816 / 2992`）⇒ 五族**全部越界**（**3.2× / 1121× / 1263× / 605× / 499×**）⇒ 越界读 Blend + **写爆角色级 SO 池**（`2c706fbd` 多写 **68409** 行）⇒ 渲染按 mod IB 读到被覆盖/未初始化行 | 三路独立证据：用户产物 `.bak-20260925-072515`（只读）字段对照；`%TEMP%` 副本上用 monkeypatch 复现旧前提 ⇒ 与备份逐字一致；工作空间实测同族 5 成员 OVC 全 = 19895 ⇒ Σ = 5×。**旁证（时间线）**：站位修复前 `2c706fbd` 的 `draw = 31054`（= 当时模组顶点数，行数自洽、无 `vs-t0`），修复后换成 99475 ⇒ 同一次重载即出症状 | ✅ 结论成立（但**不是唯一根因**，见 ②） |
| **T48** | 修复（draw 量纲）+ 同步 oracle 前提 | `draw` 权威输入改为 `drawib_model.draw_number`（= `vertex_count` = 产物 `override_vertex_count`），家族 Σ 仅诊断；**`vs-t0` 一行未动**；加「**只报警不夹取**」的不变量通知。runtime 侧 `resolve_deform_draw_count` **方向反转**：`exported > 0` 无条件采用（`source="exported"`），Σ 只进 `note`；`exported == 0` 才退回 Σ（`family-sum-fallback`）。oracle（`08-verify-independent.py`）**改写 5 条 + 新增 5 条**（旧 211 → **215/215**），其中「mod 顶点更多 ⇒ 取较大者」被**反转**为「导出数更小也必须采用」；新增离线护栏 `tests/test_himi_merged_runtime.py::test_deform_draw_never_exceeds_uploaded_rows`（`draw ≤ 上传行数` + `draw == draw_number`，**两态**：monkeypatch 回旧前提 ⇒ 断言红） | 真实副本前后对照（零写入）：`draw = 99475 → 31066`（== 上传行数），`vs-t0`/`handling = skip`/`hash` 均不变。**三处编辑都做成行数中性**（锚点零位移） | ✅ 修复落地；**实机复测症状不变** ⇒ 假设①被推翻（见 ②） |
| **T49** | 再诊断（假设重排） | 量纲修正后症状**不变** ⇒ 重新排序假设：① 游戏 palette 资源声明（typed `float4` 3×槽 vs 结构化 48 B）；② 模组 Blend 的 BLENDINDICES 是**全局**还是**局部**索引（用 `694025c6`/`def0c0ae` 判定，`2c706fbd` 全局==局部无法区分）；③ SO 输出是否被正确写入；④ **实验 A**（停用 `2c706fbd` 的 `vs-t0`）两种结果的含义 | 队长已实测排除一条：12 个 IB 段的 `match_first_index = 0 / 47199 / 54936 / 55380 / 60639`（+ `0/1716/9372`、`0/4656`）**与游戏每分片 render draw 的 `StartIndexLocation` 逐一对应** ⇒ **IB 抑制侧正确** | ⏳ **实验 A 待用户回报**；palette typed vs 结构化 = 需实机 A/B（**F1 修好前不得动声明**，否则无法归因） |

**② 被推翻的假设（两条，逐条写「为什么当时看起来成立 / 被什么推翻」）**

**假设①：「`draw` = 家族分片之和 ⇒ 把量纲改成模组自身顶点数，实机就会好」——已被实机推翻（t49）。**
- **当时为什么看起来成立**：证据链在**产物内部**完全自洽且可复现 —— ⓐ 同一份产物里 `draw` 与 `override_vertex_count` 本应同源，却差 **68409** 行；ⓑ 五个家族**全部**越界且倍数从 3.2× 到 1263×（不是个别族）；ⓒ 时间线与症状吻合（站位修复把 `draw` 从 31054 换成 99475，同一次重载即坏）；ⓓ 机制解释顺：「越界写把角色级 SO 池写爆 ⇒ 渲染读到垃圾行 ⇒ 乱转」。**四条合起来像是「主因已锁定」。**
- **被什么推翻**：t48 修好后用户**重导实测症状不变**（模型仍「所有数据全部扭曲、骨骼失效」）。⇒ **「一个真实的数字不自洽」足以定位一个真实缺陷，但不足以证明它是唯一/主因**。**教训**：修复后必须要求**实机复测**才能宣布 blocker 关闭；离线「全绿」只证明「与计划书一致」。
- **处置**：量纲修正**保留**（它确实是缺陷，且 `draw > 上传行数` 是硬越界）；诊断**回到开放状态**（t49 的四条假设）。

**假设②：「`log.txt` / FrameAnalysis 里 `MergedSkeleton` 零命中 ⇒ `vs-t0` 重绑没生效」——已被 t47 用三条独立证据推翻。**
- **当时为什么看起来成立**：`MergedSkeleton` 别名在日志与 dump 里 **0 命中**，而症状（骨骼失效）**恰好**也符合「骨架没换 ⇒ 读到游戏自己的 palette」；加上当时手上只有这两类文本证据，最省事的解释就是「重绑没生效」。
- **被什么推翻（三条互证）**：ⓐ **3DMigoto 自身日志**（`d3d11_log.txt`）里骨架资源**加载 5 次**（`[Resource\…\HI3MergedSkeleton_role_0512e26f]` + `filename=Meshes/role_0512e26f-MergedSkeleton.buf`），且 5 段 Blend 的 `vs-t0 = resourcehi3mergedskeleton_role_0512e26f` 在**快照 2–5 逐帧在场**（22 处）；ⓑ 新 dump 的 5 个 deform pass **vb0/vb1/vs-t0 全无哈希**，且**仅**这 5 个 pass 出现 `src bind_flags=0x088 [shader_resource unordered_access]`（骨架的 bind_flags 指纹）；ⓒ **体积硬证**：这 5 个 pass 的 vs-t0 = **16464 B = 343×48 = 1029×16**（与模组骨架同尺寸），未接管 pass 只有 `48 / 768 / 12336 B`（游戏自己的小 palette）。
- **结论**：**工具不记录模组别名**（同一 log 里模组段名也 0 命中）⇒ 「零命中」是**记录方式的假象**，**不能当反证**。**教训**：**不要用工具的沉默当证据**；跨工具核对（3DMigoto 日志 / dump 指纹 / 资源体积）比在单一文本里数命中更可靠。

**③ 两条通用纪律（t47 的方法论结论，长期有效）**

1. **凡写进 ini 的数值，必须同时给出三样东西**：① 它约束的**物理量**（它到底是「多少个东西」）；② **独立实测来源**（不是从别的数字推出来的，而是量出来的：文件行数 / 资源体积 / 游戏侧 draw）；③ **至少一条离线护栏**（能在仓库里红）。本轮 `draw` 违反了第 ①③ 条：它没有物理量定义（「家族 Σ」是推理出来的），也没有任何测试把 `draw` 与**模组自己的 buffer 行数**对比过。
2. **离线链缺「产物物理自洽性」与「与实机 draw 对照」这一环**：本轮的离线断言**全部**在验「**计划书规定的形态**」（`vs-t0` 在场、`draw` = 家族 Σ、IB 段形态），**从无一条**把 `draw` 与模组自己的 buffer 行数对比（`99475` vs `31066`，差 **68409** 行，**零红**）；更进一步，`08-verify-independent.py` 反过来把**错误前提写成了断言**（`08-verification.md:43` 记 passed），任何后续独立验证都会**再确认一次这个 bug**（t48 已改正 ⇒ 215/215）。缺的不变量示例：`draw ≤ 该族 buffer 行数`、`SO 写入行数 == 渲染索引覆盖行数`、`Blend 行数 == Position 行数 == override_vertex_count`。**「实测常态」也曾比错对象**：`04` §1 的 34/34 是 json **内部两字段**相等，**不是**与游戏真实 draw 比（游戏真值到本轮才测出 = 单分片 19895）。
   > **附加登记**：t39 的 **O-1（F-3）** 与 `10-integration.md` 的 **V15** 其实已点到「base `31054` vs rebind `99475` 需实机核对」，但停在「待核验」而**未成为阻断项** ⇒ 这与上面第 1 条同源：**未被护栏接住的疑点会一直躺在原地**。

**④ t39 的 O-1（F-3）：已关闭。** 原登记（t39）：base 段写 `31054`、rebind 段写 `99475`，方向未知、建议实机核对。**结论：base 的 `draw_number` / `31054` 是正确量纲；rebind 的 `99475` 就是本轮实机 blocker 本身**（越界 **3.2×**，见 T47）。口径已落 `draw ← 模组自己上传的顶点数`（§7.4 第 1 条 + `05-decisions.md` §3.26）。**注意关闭范围**：关闭的是**量纲口径**，不是实机症状（见假设①）。

**⑤ 本节新增/更新的状态**：`V9` 前提已反转（§11）、`V15` **已关闭**（§11）、§13.4 的 **D2** 引用已更正。**仍未关闭**：实机**实验 A**（停用 `2c706fbd` 的 `vs-t0`）待用户回报；palette **typed vs 结构化**的实机 A/B（**F1 修好前不得动声明**）；SO 行映射 / Blend 全局-局部索引两条待 t49 收口。

---

### 13.12 t66 / t69：对抗审查的三项文档回写（2026-09-25）—— 「静态骨架」四处更正 + 第 6 环假阴性 + 37 抬头「恰 1 笔」

**来源**：`reports/himi-merge/40-adversarial-review.md`（t66 对抗审查；裁定「线路不是错的」，§5.3 列出 3 条必修的**证据/文档**缺陷）
+ `reports/himi-merge/43-doc-writeback.md`（t69 逐处回写记录）。**纪律**：只加注、**不删原文**（删除 0 条）。
**性质**：纯文档回写 —— 不改生产代码 / 测试 / 模组（`common/`、`ui/`、`Toolset/`、`K:` 零改动）。

| # | 位置 | 被推翻的原说法 | 更正（注记落在哪） | 证据出处 |
|---|---|---|---|---|
| 1 | 本计划书 §1 第 4 条 | 「合并骨架是**静态**的 ⇒ **不需要任何逐帧 attach / CS / 双缓冲**」 | 骨架必须逐帧填 ⇒ 需要「捕获 → attach CS 写槽 → 换绑」；「比 ZZMI 简单」只在数据链路前四段成立 | `27` §1/§1.3、`28` §5.2（CF-1 9/9）、t69 复测 **14/14**（`43-doc-writeback.md` §1） |
| 2 | §3.2 **D10** | 「静态骨架资源 / 无 CS / 无 attach / 无 `[Present]` 状态机」 | 同上；骨架资源段**不得有 `filename`** | 同上 |
| 3 | §3.3 **N08** | 「HIMI 静态骨架 ⇒ 无逐帧状态需要维护」 | **部分需要**（`seen` / 次序 / 闩锁 / `prev` 快照）；仍不移植 `occ` 双槽、键池、姿态指纹 | `37-arrival-guard.md` 抬头更正（8/8 帧每族恰 1 笔）、`38-probe-suite.md` §0 面板 |
| 4 | **§4.5 标题 + 表体 6 处** | 「静态骨架 + 单点重绑」/「不做逐帧捕获」/「CS 不需要」/「`[Constants]`/`[Present]` 不需要」/「唯一运行时动作」/「没有任何逐帧操作」 | 逐行加 ⚠ 更正（**原文全部保留**）；本节**未被推翻**的只有 `vg_map` 行、SO/重定向行、typed 声明形态 | `27`、`28`、`40` §5.3 ② |
| 5（附带） | §11 **V1** | 「待验证：合并骨架是静态的」 | 升级为「**已证伪 / 阻断**」；并更正其「必须另找挂载点」的一半预测（挂载点 = **mod 侧** attach CS） | `27` §1、`28` §5.2、t69 复测 14/14 |
| 6（**不在本计划书**） | `reports/himi-merge/38-probe-suite.md` 第 6 环 | 「render 段没有同尺寸 vb0 dump ⇒ 合并几何没被画」 | 判据描述改为「按 `-ps=` 分组 + 内容定身份 + **LBS 残差对照**」；**假阴性记录保留在 §5** | `40` §1.1/§1.2、`38` §5（t69 复跑：24 个同尺寸渲染笔 vb0、其中 23 个已变换；LBS `max|Δ|=8.29e-4`、100% 行 <1e-3） |
| 7（**不在本计划书**） | `reports/himi-merge/37-arrival-guard.md` 抬头 | 「每族每帧 **2** 个 deform pass ⇒ 多实例是常态」 | 改为「**8/8 帧实测每族恰 1 笔**；双槽 + 按槽预测层 = **简化机会，非缺陷**」（原文按纪律保留在上） | `40` §1.3、`.dbg/_t81_premise_falsify.py` **测量 B** |

**与既有台账的关系**：本节与 §13.11 同源（都是「实测推翻旧说法」），但**只回写文档**。
**冲突裁定（以实测为准，不调和）**：`40` §1.1 对第 6 环缺陷的**机制描述**（「23 个渲染笔被归进了 deform 集合」）与实测不符 ——
实测是**正则整类丢掉带 `-ps=` 的 dump**（2485/2605 条），二者**结论方向一致**（第 6 环是假阴性、`26` §3.1 对）；以 `38-probe-suite.md` §5.1 为准。
**本节未关闭项**（不在回写范围）：`40` §3 的 **P5**（四族索引命名空间不可测）与 **P8**（「整帧晚一帧」的跨帧语义未验）。

---

### 13.13 t7 收口：本轮进度快照、根因裁定与被推翻的假设（2026-09-26）

**来源**：`reports/himi-merge/63-t7-closing.md`（t7 集成收口报告，本节的完整版）+ `49`（t2）/`50`（t3）/`51`+`51b`+`51c`（t4/t18/t20）/
`52`（t5）/`53`+`53b`+`53c`（t6/t19/t21）/`54`（t8）/`55`（t9）/`56`（t10）/`57`+`57-t13`（t11/t13）/`58`（t12）/`59`（t14）/`61`（t15）/`62`+`62b`（t16/t17）。
**纪律**：只加注、**不删原文**（删除 0 条）；本节**不改生产代码 / 测试 / Toolset**；未写 `K:`；未 git commit。
**性质**：台账 + 裁定 —— **不得据此声称错位已修好**（§2.3 红线）。

**① 本轮完成（有产物 / 有回归钉子 / 可复算，逐条带出处）**

| # | 已完成 | 判据 | 出处 |
|---|---|---|---|
| D1 | 导出侧**可复现**（两次独立导出 62/63 个产物逐字节相同，唯一差异 = TANGENT 末位 1 ulp） | `.dbg/_t8_compare_merged_only.json` | `54` §3/§3.1 |
| D2 | 导出侧坐标 / 镜像 / join 三项**排除** | 并集命中 99.82%（identity）、其余候选变换 0% | `54` §4 |
| D3 | 合并骨架**每帧填充**链路落地（「静态骨架」前提被证伪后的改造） | 跨 14 帧内容 14/14 不同；CF-1 9/9；oracle 215/215 | `27` §1、`28` §5.2 |
| D4 | **F5** 每族身份资源（16 B `(base,span,version,magic)`）+ 本族 span 上界 + 表/身份矛盾 ⇒ 整族拒写 | 5 个 16 B 文件；身份载荷 == ini `array=` == 冻结表；m6 变异 127 次调用真红 | `51` §4.1/§5；`52` §3.1 |
| D5 | **F6** 冻结表 vs 动态 plan 导出期审计（矛盾 ⇒ Fatal；新 span ⇒ 大声诊断不阻断） | B 态真抛 Fatal；m7 真红；本产物 `problems=[] notices=[]` | `51` §4.2；`52` §3.2 |
| D6 | **T45-F4** `index == int(组名)` 导出期断言 | 真实场景 **3120/3120 组满足、0 非数字组** | `51` §4.3；`52` §3.3 |
| D7 | **重放块无条件判据**（禁条件行；`handling = skip` 后必须紧跟**一笔**无条件 `draw`） | m4 变异真红；产物 5 块全绿 | `51` §4.4；`53` §E3 |
| D8 | **K2 硬门从「恒空转」修成「真的判过 12 条记录」**（补 `unique_str` + 总账行 + `unjudgeable`） | 真实导出日志出现汇总行；改坏 1 个 json ⇒ Fatal 指名部件 | `51b` §1；`52` §0-7 |
| D9 | **诊断落地到产物**（控制台 / 导出日志 / **产物 ini 注释块**三层） | 产物 654–656 行；块内只有注释；`;sha256=` 仍在最末；段数 108 不变 | `51b` §2；`53c` §3.1 |
| D10 | **F5 产物级文件硬门补上回归钉子**（t6 新发现的空转） | 四态用例；m3 变异**调用 1 次 + 真红**（t6 时 0 次调用 0 失败） | `51c` §3；`53c` §2.1 |
| D11 | **K2 判词三档**（失败 / 未核对（无 rebind 记录）/ 未核对（没有可判的部件）/ 通过） | 两态用例；m8 变异真红 | `51c` §3.4；`53c` §2.2 |
| D12 | **质量链闭合**：t4→t5→t6(needs_revision)→t18→t19(pass)；t6→t20→t21(pass) | 两条 verify 逐字复现；改动面 diff 实测；用户数据 0 写入 | `53b`、`53c` §1/§2/§5 |
| D13 | **t20 的改动对产物零影响**（修的是诊断/判词层） | t21 用 t20 代码新导出产物，ini 与 t18/t19 **逐字节相同**（`255ff840…`） | `53c` §3.2 |
| D14 | **用户一页操作卡**（导出卡 + 抓帧卡 + 回传清单） | 12/12 源件名可机器复核；H1/H2 判据；引用校验 C1–C9 全通过 | `62`；`62b` §3 |

**② 「已排除 12 条」的覆盖口径（**重要更正**）**

本轮排除的 12 条（几何/顶点位置、join 烘变换、镜像不对称、顶点→槽块串线、CB 摆放、骨架静态快照无损、合并变形链闭合、渲染读入层、帧内陈旧量级、触发键歧义、根骨骼落位、站位与去重行数差）
**全部锚在「本角色 `role_0512e26f` + 已抓的 4 帧 + 本产物」上**；其中 #9 只有**单帧静态姿态**、#10 只有 **4 帧**、#4 是**单帧 + 静态**、#6 是 **workspace 文件 vs 基线帧（非运行期）**。
⇒ **它们都不是「普遍排除」**：只能推出「这些通道**在这几帧/这个产物上**的量级不足以产生症状」。逐条的「证据覆盖（帧数/角色数）」列见 `63-t7-closing.md` §1.2。

**③ 被推翻的假设（本轮 9 条，如实登记；含队长提错的）**

| # | 被推翻 | 谁提 → 谁推翻 | 依据 |
|---|---|---|---|
| 1 | 「触发漂移 ⇒ 写错 SO 目标」 | **队长** → t13 证伪 | 三帧各恰 1 笔、都是头发族自己的 deform pass；SO 全部 = `61facde5`（`57-t13` `:17`） |
| 2 | 「三帧全是 s0」 | t11 → t12 证伪 | 11 行 `[Present] $hi3_probe_* = 0`；且 t11 用「抓帧**之后**的文件现状」证「抓帧**当时**的运行态」，**推理形式无效**（`58` §2.1/§2.2） |
| 3 | 「223923/224222 是 s3」 | **队长** → t12 部分证伪、t13 又指出 t12 排除不成立 | `vb1` 存在 ≠ `draw` 执行 ⇒ s3 未被排除（`57-t13` `:22`/`:244`） |
| 4 | t3 的 **F2 修法**（`draw` 搬到渲染 IB 段） | t3 → t4 证伪 | 渲染段只在 `DrawIndexed` 触发、那一刻没有 SO 目标/变形 VS/pointlist（`51` §3.3） |
| 5 | t9 的**读数载体 A** | t9（自报 R1）→ t12 实测否证 | `run` 行被打印但命令列表**函数体一行都没打印**（`58` §3） |
| 6 | t4 §5「几何 = 并集」（逐族行数求和） | t4 → t5 指出是**恒等式** | 真实口径：位级 0%、容差 1e-5 下 30659/31054 = **98.73%**（`52` §4.1） |
| **6b**（**口径再更正**） | 上格里的「容差 1e-5 下 30659/31054 = **98.73%**」这一口径 | t5（当年）→ **t1 判定为量化伪影** | ⚠ **2026-09-26 / t23 更正**：`30659/31054 = 98.73%` 是 **`round(5)` 口径的跨格边界伪影** —— 被判「未命中」的那 **395 行**真实最近邻距离 **max 仅 2.384e-7**，**全部 ≤1e-6**。**准确口径 = 最近邻切比雪夫 ≤1e-6 ⇒ 31054/31054 = 100%**，且**双向**（12 个部件的 **118161/118161** 行也在产物里 ≤1e-6 找到对应）。「**z 差 2 ulp**」的说法**不准确**（ULP 数逐行从 1 铺到 200+）；准确说法 = **绝对偏差 ≤2.384e-7（那 395 行）、全体最大 8.311e-6 且仅 6 行 >1e-6**（`48-forensics-artifacts.md` §2.2 P1–P10 / §2.3 / §2.4 / §2.9；**详见 §13.14**） |
| 7 | t4 §5「K2 在 12 个部件上全部判过 pointlist」 | t4 → t5 证伪 + t6 三重钉死 | 那两行日志没有，是因为循环**一次都没进** ⇒ 以缺失推断存在（`53` §E5） |
| 8 | 「合并骨架是静态的」（更早一轮） | 计划书 → `27`/`28` 证伪 | 同一 palette 跨 14 帧内容 14/14 不同 ⇒ 必须逐帧填（本表 §11 V1 已升级为「已证伪/阻断」） |
| 9 | 「抓帧 `log.txt` 里 `MergedSkeleton` 零命中 ⇒ 重绑没生效」（更早一轮） | t47 前 → t47 三条互证推翻 | **工具不记录 mod 别名** ⇒ 零命中是记录方式的假象（§13.11 假设②） |

**④ 根因裁定（三态；完整表见 `63-t7-closing.md` §2.1）**

- **已证实（是真实缺陷）**：合并渲染 4 个 pass 的**材质错配**（t11；**非几何错位**）；当前 `.blend` 的**双份几何形态**（`54` §2）；**K2 硬门空转**（`52`/`53`；**不改变产物字节** ⇒ 已排除为症状成因）。
- **已排除（本角色 / 已抓帧 / 本产物）**：导出侧顶点→槽块串线、坐标/镜像/join 变换、蒙皮数学、CB 摆放、渲染读入层、帧内陈旧量级、触发键歧义、根骨骼落位、F2 修法；F5/F6/T45-F4 通道**在本角色不活**（`池 == 引用槽 == span`）。
- **仍不确定**：**运行期骨架块内容**（§11 V17，**最能解释症状的唯一未观测环节**：t10 量化的「错喂 palette」代价 99°–178° + 0.24–1.78 单位，其中 `70a4da3e ← b35cfc6c` = **178.06°/+Y + 0.4859**，**同时**覆盖「沿 Y 180°」与「上移半个身体」）；`cs-t1` 实机加载（V18，本轮新引入）；K1/K3/K5（V19）；`vb2` 语义（V20）；材质错配的实机可见后果（V21）；动画下陈旧（V22）；别角色/别场景（V23）；223923/224222 的模组态（V25）。
- **两条不可混用的量**：t13 的 **2.6% 身高 / 0.4–1.6°** 是**跨帧**量（含两帧姿态差）；t2 的 **0.25% 身高**是**帧内陈旧**量。
- **至今没有任何一帧是在症状在场时抓的** ⇒ §1 的排除**不能**升级为「症状不来自这些通道」。

**⑤ 质量链与台账记账**

- t6 的 verdict = `needs_revision`（review 契约下任务记 `failed`）**不是未收敛**：6 条 finding 全部被 t18/t20 接管，t19/t21 两位独立审查者都判 **pass**。
- 台账更正：**T21-F2**（t20 的结构化 `changedPaths` 不准）⇒ 按实测 diff 记为 `{ui/universal/himi.py, tests/test_himi_merged_runtime.py, reports/himi-merge/51-fix-implementation.md}`（`common/himi_merged_runtime.py` **`+0/−0`**）。
- **T21-F3**（契约生成缺陷，**根因在队长写契约**）：`inScope` 里混进两条非路径数字串 ⇒ 按「acceptance 优先」记账，t20 的改动**不算越界**。
- **T21-F1**（U2 实验指错文件）⇒ 已按修正版落进 §11 **V18**；措辞源自 t6 的 T6-F5②，t20 属逐字实现 ⇒ **不扣 t20**。
- **T21-F4/F5**：`51c` 报告 §7 自身 sha256 过期、`51c:12` 摘要式简写 ⇒ 以磁盘实测与 `51c` §1 分诊表为准。
- **流程**：`t1`（取证-几何产物）因 provider `429 RATE_LIMIT` 失败，其取证目标由 t8 的 `54-blender-export-repro.md` 覆盖。

**⑥ 本轮验证（t7 实跑，不引用他人数字）**

```powershell
cd E:\代码\TheHerta4 ; .\.venv\Scripts\python.exe -m pytest -q
#   2854 passed, 42 skipped, 2 warnings, 164 subtests passed in 79.75s (0:01:19)
#   exit 0 ; 外墙 90.23 s ; HEAD 88bf8ec51f547d1a76bab0df0f3d786fafb171cb
```

**⑦ 用户下一步（8 条，逐条给「为什么需要」）**

见 `63-t7-closing.md` §3；**第 1 条 = 先抓一帧「错位正在发生」的帧（带截图）** —— 现有全部排除都是在**无症状帧**上做的；s5b 判读帧解决的是「骨架内容对不对」，**解决不了「症状出现在哪一帧」**。

**⑧ 本节新增/更新的状态**：§11 追加 **V17–V25**（九条，2026-09-26，带报告链接）；本节为**新增小节**，未改动 §13.1–§13.12 的任何一个字。**仍未关闭**：V17（等用户 s5b 帧）是**唯一能关掉「最能解释症状」那一条**的实验。

---

### 13.14 t1 / t22 的最终结果回写 + F10/D13/A18 三处「恒 0」更正 + 「几何 = 并集」口径更正（2026-09-26，t23）

**来源**：`reports/himi-merge/48-forensics-artifacts.md`（t1 文件侧产物取证）+ `reports/himi-merge/64-matchfirstindex-forensics.md`（t22 `match_first_index` 链路取证）；变更短记 = `reports/himi-merge/65-plan-corrections.md`。
**纪律**：只加注、**不删原文**（删除 0 行，实测见 `65` §3）；不改生产代码 / 测试 / Toolset；`K:` 只读；未 git commit。
**性质**：**文档更正 + 台账追加** —— **不涉及任何产物变化**（模组 ini 仍 `b5392eb95c9575fb…` 原状），也**不得据此声称错位已修好**。

**① 三处「`match_first_index` 恒 0」的更正（F10 `:208` / D13 `:258` / A18 `:1217`；三处已一致）**

| 位置 | 原文（**保留在上**） | 更正 |
|---|---|---|
| §2.9 **F10** | 「…46/46 全为 0 ⇒ `match_first_index` 恒 0」 | 前半句**真**、后半句**假**：那四个 JSON 字段确实 46/46 全 0（同一 JSON 里 `IndexOffset=0` 与**文件名第 3 段 `55380`** 并存）；**导出侧读的是物体名第 3 段**（`common/object_prefix_helper.py:295-297`）⇒ ini 实际**非零**：`60639 / 54936 / 55380 / 47199 / 1716 / 9372 / 4656` |
| §3.2 **D13** | 「HIMI 实测 = **恒 0**（46/46）」 | **撤回**；**原行 ZZMI 栏写的「每部件非零偏移」才是对的**。原「必须重写为」栏的结论**不变**（合并重放不得按 `match_first_index` 区分部件） |
| §9.2 **A18** | 「…46/46 全 0（`match_first_index` 恒 0）」 | 断言本体（`merged.no_extra_offset_fields` = True）**保留**；括号里的**推论撤回** |

**链路（6 跳，全部带行号）**：`submesh JSON 文件名 <bare>-<IndexCount>-<IndexOffset>` → `mesh_name`（`common/ssmt_import_helper.py:41`）→ 物体名 → `extract_prefix_info` → **`parse_prefix_parts()["first_index"]`（`common/object_prefix_helper.py:297`）** → `DrawCallModel.match_first_index`（`common/draw_call_model.py:40`）→ `SubMeshModel.match_first_index`（`common/submesh_model.py:86`）→ **ini `match_first_index = …`（`ui/universal/zzmi.py:5849,5853`）**。
**F10 的错因**：它量的四个字段取自 `tools/himi_merged_probe.py:242-245`（`*_json`），而 `:1031-1038` 的 `no_extra_offset_fields` 只能证明**那四个字段**为 0 ⇒ **「⇒ 恒 0」是把两个不相干的量当成了同一个**（`64` §1.5）。

**② t22：`match_first_index` 链路是干净的（关键数字）**

- **12/12 段精确命中（两帧）**：140134 该角色 46 笔 render draw **46/46 被覆盖、0 泄漏**；212119 **68 笔**被 override 接管、**0 笔对不上任何段**；
- **键两两互异**（12 个 `(hash, MFI)` 组合）；**11/12 个键命中多笔**（= 「子网格 × 多 pass」等价类，正是 t11 观察到的「合并渲染吃掉 4 个 pass」的来源）；
- **安全性质**：无键命中不同 `IndexCount` 的 draw；无键从不触发；同 bare 内 `StartIndexLocation` **累加且互异**（`2c706fbd`: 0/47199/54936/55380/60639）⇒ 键在同一 IB 内唯一确定一个子网格；
- **它不是症状成因**：`match_first_index` 只决定**哪一笔 draw 被接管**（**选择器**），**不参与任何坐标运算** ⇒ **不可能产生「整块错位」，也不可能产生「b35cfc6c 绕 Y 180°」**；唯一能由它造成的整块级症状是**整块消失**（当且仅当**头发段** `70a4da3e`（MFI=0）失配、导致合并模型那一笔不再绘制）；
- 交叉验证：工作空间 `LOD0\ComponentName_DrawCallIndexList.json` 逐条代入 log ⇒ **79/81 精确一致**，2 处例外都在**非目标组件**（`9ccbfb88-24-0` / `9ccbfb88-30-1512`：SSMT 把非索引 deform pass 的序号也算进了组件）⇒ **与本 mod 的 12 个段无关**。

**③ t1：文件侧最终裁决（12/12 部件「位置逐点保留 + 无刚体变换」）**

| 项 | 结果 |
|---|---|
| **几何 = 并集（更正后的口径）** | **最近邻切比雪夫 ≤1e-6 ⇒ 31054/31054 = 100%**，且**双向**：12 个部件的 **118161/118161** 行也在产物里 ≤1e-6 找到对应 |
| **两条「不要再引用」的说法** | ① **`round(5)` 的 `30659/31054 = 98.73%` 是跨格边界伪影**（被判「未命中」的 **395 行**真实最近距离 **max 仅 2.384e-7**，全部 ≤1e-6）；② 「**z 差 2 ulp**」**不准确**（ULP 数逐行从 1 铺到 200+）⇒ 准确说法 = **绝对偏差 ≤2.384e-7、全体最大 8.311e-6，且仅 6 行 >1e-6** |
| 空间变换 | 48 种「符号 × 轴置换」全部试过，**只有恒等**达 100%（最好的非恒等仅 49.11%） |
| 12/12 刚体拟合 | `\|R−I\|max` = 1.106e-7 ~ 1.376e-7、`\|t\|∞` ≤ 2.792e-8、**拟合旋转角 0.0000°** ⇒ **无刚体变换** |
| **`b35cfc6c`（下半身）专项** | **拟合旋转角 6.613e-6 度**；**恒等命中率 1.0000**；**`RY(180°)` 命中率 0.0000**（`RX(180°)` / `RZ(180°)` 同为 **0.0000**）⇒ **文件侧否证「合并/导出后 b35cfc6c 绕 Y 轴额外转了 180°」** |
| 顶点组零错位 | 13 个物体（12 部件 + 合并物）**全部 `name == str(index)`**（组数 184/202/273/292/343/343，无空洞）；join 新追加组**恰好**落在 273..291 与 292..342，**追加顺序 == 数字顺序**；合并物 = 12 段**逐字节拼接**（`bit_exact` **12/12**，链式铺满 `[0,118161)`，无空洞、无重叠） |
| 343 槽四方对齐 | ① `HIMIMergedSkeleton.json` ② CS 基址表（源码 + 产物两份）③ ini `array =` ④ 产物 `-Blend.buf` 索引分布 —— **5/5 一致、不一致 0 条**；跨族行（权重 > 0）= **0**、全通道 = **10532**；主导族行 20300/3462/3957/1831/1504 |
| 量级论证 | 文件侧能找到的**最大**几何偏差 = **8.311e-6**（6 行，中位数 1.3e-7）；用户症状是 ~**0.8 单位**位移与 **180°（3.14 rad）** ⇒ **相差 5~6 个数量级** |

⇒ **文件侧几何 / 索引 / 槽位布局不能解释「整体上移半个身体 + 下半身沿 Y 轴 180°」**（`48` §8 的 E1–E6 六条排除依据）。

**④ 新增/更新的状态**：§11 追加 **V26（已核实，非待验证：`match_first_index` 的来源与唯一性）** + **V27–V29（t22 的未取证项 U2/U4/U6）**；三处「恒 0」更正分别落在 §2.9 F10 / §3.2 D13 / §9.2 A18 的**紧邻新行**（原文全部保留）；§13.13 ③ 的第 6 行后补 **6b（口径再更正）**。**仍未关闭**：V27–V29（低，见 §11）；V17–V25 原样保留。**本节不改变任何既有裁决**，也不改变「错位仍未修好」的状态。

**⑤ 已知残留旧口径的报告清单（t24 登记，2026-09-26）**

**下列报告里仍写着旧的几何口径** —— 它们**是各自时间点的记录（快照性质）**，按纪律**不逐个追改**（理由与处置见 `reports/himi-merge/66-stale-wording-registration.md`）。
**口径以本节（§13.14）为准**；**日后引用请不要从它们里取「`30659/31054 = 98.73%`」或「z 差 `2 ulp`」这两个数字**。

| 文件:行 | 该处写的（旧口径） |
|---|---|
| `reports/himi-merge/51-fix-implementation.md:394`（另见 `:410`、`:412`） | 「…位级命中 **0%**（x/y 逐位相同、**z 差 2 ulp**）…**容差 1e-5 ⇒ 98.73% 逐行命中**」 |
| `reports/himi-merge/51b-t18-repair-after-t5.md:175-176`（另见 `:180`） | 「z 差 **2 ulp**」/「容差 1e-5 逐行 **30659 / 31054 = 98.73%**」 |
| `reports/himi-merge/51c-t20-repair-round2.md:59` | 复述 t4 表格行（同上两个数字） |
| `reports/himi-merge/52-fix-verification.md:23`（另见 `:227`、`:229`、`:233`、`:421`） | 「z 差 2 ulp」/「产物逐行 **30659/31054 = 98.73%**」 |
| `reports/himi-merge/63-t7-closing.md:136` | 「位级 **0%**（z 差 2 ulp）、容差 1e-5 下 **30659/31054 = 98.73%**」——**该文件已在 `:137` 就地补 ⚠ 更正注（t24）**，故它是**唯一**已就地更正的主入口 |

> **登记口径**：本清单只登记**几何口径**（上面那两个数字）的残留。**F10 / D13 / A18 的「`match_first_index` 恒 0」** 已由本节 ① 更正并给出三处一致表述；同批报告里如 `48` §7.3 提到的「`.blend` 属性 `3DMigoto:MatchFirstIndex` 恒 0」**不是错**（该属性确实恒 0），错的只是「**⇒ ini 的 `match_first_index` 也恒 0**」这个推断。

---


### 13.15 本轮（2026-09-26）全部失败 / 无效尝试的完整台账（用户要求；t31 补记）

> 用户指示：**「把每次错误的尝试全部写入到计划书」**。本节**只增不删**，把本轮所有
> 「试过但没效果 / 被实测证伪 / 设计错误」的尝试逐条记下，并写明**它排除了什么** ——
> 目的是让后来者不必重走。

#### A. 对模组的直接修改尝试（实机结果）

| # | 尝试 | 预期 | 实际结果 | 排除了什么 |
|---|---|---|---|---|
| **A1** | **每族一份单独着色器**：把单文件共享的 `res/himi_attach.hlsl`（靠 `GetDimensions` 读到的槽数查冻结表 184/18/71/19/51 识别本族，查不到就把 `base_slot` 置 `0xFFFFFFFF` ⇒ **一个字节都不写**）换成 5 份各自带 `#define HIMI_ATTACH_BASE`（0/184/202/273/292）的 CS，去掉「查表失败⇒不写」这条路径 | 若错位源于「某族查表失败 ⇒ 永远不更新 ⇒ 用旧骨骼蒙皮」，应看到变化 | **用户实机：没有变化**。且用户指出：**这条此前早已试过、同样无效果**（本轮未先在计划书里查证，属重走老路 —— 见 §D2） | ⇒ **排除「attach CS 的族识别 / 查表失败」这条通道**。（该改法作为**加固**仍有价值：它消掉了一条「静默不写」路径，但不是错位成因。） |
| **A2** | **s3 诊断态**：在合并 deform 段加「五族都已到达」守卫 | 让合并 deform 只在骨架齐全时执行 | **守卫恒假 ⇒ `draw = 31054, 0` 从不执行 ⇒ SO 全零 ⇒ 模型消失**（与 `224222` 同形态） | ⇒ 队长设计错误。确立纪律：**`draw` 必须无条件执行，守卫只能管骨架内容**（见 §D1） |
| **A3** | s1 / s1b（只加仪表，不改行为） | 读到「各族到达计数」 | **读数不出现**：从 `[Present]` `run` 的 CommandList 只打印 `run` 行、不打印段体 | ⇒ 排除「用自建读数命令列表读运行期计数」这条载体 |
| **A4** | s2（去掉 `2c706fbd` 的 `unless_null`） | 改变捕获行为 | 与 s0 **行为等价**（t12 实测） | ⇒ 排除「`unless_null` 的捕获时机」 |
| **A5** | s4（把 `70a4da3e` 段内的 `vs-t0` 提到 `run` 之前） | 改变捕获顺序 | 与 s0 **行为等价**（t12 实测） | ⇒ 排除「该段内的绑定顺序」 |
| **A6** | s5（palette 可见化：deform 段 + 渲染段都挂 `vs-t60`/`ps-t60`） | 让 dump 带上 palette 字节 | **未采用**：在 deform 段插 `vs-t60` 会改变触发（t13 机制 a） | ⇒ 改为只挂渲染段的 **s5b** |
| **A7** | **s5b**（palette 可见化，只挂渲染段 12 行 `ps-t60`） | 让下一帧 FrameAnalysis 带上各族当帧 palette | **用户实机：没有变化**（**设计如此** —— 它是诊断态，不改渲染行为） | ⇒ 它不是修复；只用于「看见骨架内容」 |
| **A8** | s_perfam / s_perdraw（合并渲染按族 5 笔 / 按部件 12 笔，各自绑自己的贴图） | 恢复逐部件材质 | **未实测**：用户明确「材质目前全都是好的，只有错位的问题」 | ⇒ 材质层不是本次要解决的问题 |
| **A9** | t30 的「按部件保留 draw」实验态（含设计分析） | 分离「材质层」与「几何层」 | 同上，**未实测** | ⇒ 同上 |

#### B. 被实测证伪的假设（含队长的）

| # | 假设 | 证伪者 | 结论 |
|---|---|---|---|
| **B1** | 队长：「触发漂移 ⇒ 合并 deform 写错 SO 目标」 | t13 | 4/4 帧只命中同一笔、SO 目标恒 `61facde5` ⇒ **排除** |
| **B2** | 队长：「`223923` / `224222` 是 s3 态」 | t12 → t13 来回反转 | **未定论**（判别抓帧包未做） |
| **B3** | 队长：「至今没有任何一帧是在症状在场时抓的」 | **用户直接更正** | **错**：`223923` / `224222` / `043219` 都是**错位帧**；模型**持续错位** |
| **B4** | 队长：「材质层是『整个模型一团糟』的主因」 | **用户直接更正** | **错**：材质目前是好的；**只有错位问题** |
| **B5** | 队长：「SO 点云宽而扁 = 错位」 | 队长自查（对照 140134 里同样是形变输出的缓冲） | **错**：那个空间本来就不是 Y 向上，是**读错了轴** |
| **B6** | 队长：「五族 `Dispatch` 数不足（应为 3/1/2/1/1）」 | 队长实测 | 实测就是 **3/1/2/1/1** ⇒ **排除** |
| **B7** | 队长：「其他模组冲突（`Mods/RemoveTrans.ini` / `Mods/HI3FX`）」 | 队长实测 | 前者只打 **PS** 的 `discard_nz`（透明修复）、后者只提供 FX 命名空间 ⇒ **都不碰 deform** ⇒ 排除 |
| **B8** | 队长引用的口径：「容差 1e-5 逐行 98.73%」「z 差 2 ulp」 | t1 | 前者是 `round(5)` **跨格边界伪影**（真实口径：最近邻 ≤1e-6 = **100%**）；后者**不准确**（准确说法：绝对偏差 ≤2.4e-7、最大 8.311e-6） |
| **B9** | t3 的 F2 修法（把 `draw = 31054, 0` 移进渲染 IB 段） | t4 | deform 是**非索引 `Draw`**；IB 段只在 `DrawIndexed` 触发 ⇒ **不可行** |
| **B10** | t4：「K2 门在 12 个部件上全部判定 pointlist」 | t5 / t6 | **错**：rebind 字典缺 `unique_str` ⇒ 门逐条 `continue`，**一次都没判定**（t18 已修） |
| **B11** | t4：「几何 = 并集（逐族行数 20300/3462/3957/1831/1504）」 | t5 / t6 | 那是**恒等式**（自划分求和），不能当证据；真实口径见 B8 |
| **B12** | t9 的「读数载体 A」 | t12 | 实测否证（同 A3） |
| **B13** | t11：「三帧全是 s0」 | t12 | **错**：t11 用「抓帧**之后**的文件现状」证「抓帧**当时**的运行态」 |
| **B14** | t28 的 U7：「导出侧 `NormalMap.dds` 192 B 是导出 bug」 | t29 | **错**：游戏侧本来就绑这个占位（与基线帧 10/10 一致、sha256 四方全等）；真伤害在**合并侧**把 4 个有真图的部件拖成平法线 |
| **B15** | 计划书 F10 / D13 / A18：「`match_first_index` 恒 0」 | t22 | **错**：导出侧读的是**物体名第 3 段**，那四个 JSON 字段根本不被读；实测产物有非零值（60639/54936/55380/47199/1716/9372/4656）。已就地更正（§13.14） |

#### C. 本轮**未能**解决的（不得当成已成立）

1. **错位的根因**：仍未找到。所有可离线验证的层（几何 / 顶点组 / 四流顺序 / IB 连通性 / 变形链 / 渲染读入 / CB / override 匹配 / 骨架块装错族）**逐条排除**，但用户报告的错位**依然存在**。
2. **没有任何一帧被独立量到「错位」**：本轮全部结论都是「**这些层是对的**」的排除法结论，**从未独立确认过错位本身**（`043219` 等错位帧的 o0 转储与用户所见未必一致 —— 见 §D3）。
3. **症状在场的 s5b 抓帧**：用户未做（s5b 已回退）。

#### D. 本轮留下的纪律（写给后来者）

1. **`draw` 必须无条件执行**；守卫只能管骨架内容。（A2 的教训 —— 一次「守卫恒假」就让模型整块消失。）
2. **任何修复动手前，先在计划书里查「这条是否已经试过」**。A1 就是重走老路的实例：用户指出「每族一份单独着色器」**此前已试无效**，而本轮又做了一遍。**计划书没记的，要补记**（本节即为此而设）。
3. **不要把 dump 图当作「用户看到的画面」**：o0 是**渲染目标转储**，与最终呈现未必一致；观感类判断**必须问用户**。（B4 / B5 的教训。）
4. **「链闭合 / 自洽」不能当作「该层正确」**：`SO = skin(位置, 权重, 骨架)` 用的骨架**就是当帧骨架**，用错骨架照样闭合。（B10 / t27 的教训。）
5. **历史报告不是现行口径**：口径以 **§13.14** 为准；本轮新增更正以 **本节 + §13.15** 为准。
6. **对模组的改动一律先备份、可一键回退**；本轮所有实验态均已回退到原状
   （`sha256 b5392eb95c9575fbf3aab6d647f981443f63db61c26320346ae9b660d893082a`）。


### 13.16 改动纪律：**无佐证不改**（用户裁定，2026-09-26；t33 回写）

> 用户原话：**「不要猜啊，任何的改动都必须要有佐证，没有佐证就不要改。」**

#### A. 纪律条文（对本项目队长与全部成员生效）

1. **任何对模组 / 产物 / 生产代码的改动，动手前必须给出佐证** —— 一条**可复现的离线测量**，
   它同时说明 **(a) 目标缺陷确实存在**、**(b) 该改动能消除它**。
2. **佐证必须能预测结果**。「这个可能有问题」不是佐证；
   「A 与 B 在字节层面不一致，且 B 是游戏实际读取的那一份」才是。
3. **实现本身也要有佐证**：改动引入的每一条绑定 / 状态，都要说清它的**作用域与还原方式**
   （会不会泄漏到游戏后续 draw），并能**离线自检**（条件行数、draw 数、绑定泄漏检查）。
4. **没有佐证就不改** —— 只出报告、只出结论。
5. **绝不在用户实机上试未验证的改动**。实验态必须：可一键回退 + **明确告知「可能崩」**。

#### B. 复盘：本轮三次「无佐证 / 佐证不足」的改动

| # | 改动 | 我当时的依据 | 为什么不算佐证 | 结果 |
|---|---|---|---|---|
| 1 | **每族一份单独 CS**（基址写死，去掉运行期查表） | `224222` 帧 SO 全零 ⇒ 怀疑 attach CS 查表失败 | **不能预测结果**：实测五族槽数 `184/18/71/19/51` 与游戏 palette 字节数**逐字节对得上** ⇒ 查表本就不会失败；且用户指出**此前已试过、无效** | **无变化**（已回退） |
| 2 | **s3 诊断态**（合并 deform 加「五族都已到达」守卫） | 想「等骨架齐全再变形」 | **没验证守卫条件是否可满足**：`[Present]` 每帧清零计数器 ⇒ 守卫恒假 ⇒ `draw` 不执行 | **SO 全零、模型消失**（已回退） |
| 3 | **redirect-SO**（捕获 SO 目标 + 五族段末尾重跑合并 deform） | `223923` 残差 `0.3745` ⇒ 骨架各块取自不同时刻（**这一条是真佐证**） | **实现没有佐证**：未读 ZZMI 的守卫 / 还原结构，未分析 `vb0` / `vs-t0` 的**泄漏范围** | **模型消失 + 切角色卡死**（已回退） |

**结论**：第 3 条的**问题判断有佐证**，但**实现是猜的** ⇒ 仍然违反纪律。
若要做，必须先补**实现层佐证**（读全 ZZMI 的守卫 / 还原 / carrier 结构 + 离线泄漏自检），再交用户。

#### C. 与既有纪律的关系

- §9.7 的通用纪律（t47/t48 实机事故的教训）仍然有效；
- §13.15 D1「`draw` 必须无条件执行」是本条的一个特例（s3 的教训）；
- **本条优先级最高**：任何与本条冲突的「先试试看」一律不执行。


### 13.17 两次 redirect-SO 实机失败：精确 diff 与崩因分析（2026-09-26；t34 回写）

> 用户裁定：**「回退之后正常了，也就是说就是你上次的改动导致模型消失，再加卡死。」**
> 本节记录**改了哪几行、观察到什么、以及目前仍无法解释的部分**。**两版均已回退**，
> 模组 sha256 = `b5392eb95c9575fbf3aab6d647f981443f63db61c26320346ae9b660d893082a`。

#### A. 第一版（模型消失 + 切角色卡死）

新增/改动三处：
1. 声明 `[ResourceHI3RedirectSO]` / `type = Buffer` / `stride = 44`
2. 宿主（`[TextureOverride_VB_70a4da3e_70a4da3e_Blend]`）段内：
   `ResourceHI3RedirectSO = ref so0`，并把**主绘制**由 `draw = 31054, 0` 改为 `draw = 3, 0`
3. 五个族段末尾各追加（**无条件**）：`vs-t0 = <合并骨架>` / `vb0 = Resource70a4da3ePosition` /
   `vb1 = Resource70a4da3eBlend` / `so0 = ref ResourceHI3RedirectSO` / `draw = 31054, 0` / `so0 = null`

**三个明确的设计缺陷（事后可指出，但当时未做任何验证）**：
- **主绘制被移出宿主段** ⇒ 一旦后续重跑未生效，**没有任何几何被写出**（消失的直接通路）；
- **无条件重跑** ⇒ 引用**可能过期的 referent**：切角色时游戏资源被重建，
  写已释放的缓冲 ⇒ 设备挂起（**卡死**）；
- **`so0 = null`** ⇒ 解掉**游戏自己的 SO 绑定**（ZZMI 之所以能 `null`，是因为它绑的是自建重定向缓冲）。

#### B. 第二版（仍然消失）

针对 A 的三点各做修正：主绘制**回到宿主段且无条件**；重跑全部包进
`if $hi3_rso_ok == 1`（新增 `[Constants] global $hi3_rso_ok = 0` + `[Present] $hi3_rso_ok = 0` 每帧清零，
宿主段置位）；块尾改为 `so0 = ref ResourceHI3RedirectSO`（重绑回捕获到的游戏原有目标）。
**离线自检全过**（if/endif 4/4、主绘制上一行为 `handling = skip`、`draw = 31054, 0` 共 5、守卫体内无 `run`/赋值、
五族 Position 段齐全）—— **但实机仍然「模型直接消失」。**

#### C. 目前**无法解释**的部分（不得当成已知）

1. **第二版为什么仍然消失**：主绘制已无条件保留，理论上最坏只应退回原行为。
   现有证据不足以判定是「重跑本身」「referent 机制」「`[Present]`/`[Constants]` 的引入」
   还是「每帧 5 次 × 31054 顶点重放的负载」造成的。**未做二分定位。**
2. **`[Present]` + `$变量` 在本模组 ini 里从未使用过**（本 ini 原状无 `[Constants]`/`[Present]`），
   引入方式是否与 d3dx.ini 的用法一致，**未核对**。
3. **B 的自检只覆盖静态文本**，不覆盖运行期绑定语义 ⇒ **不能作为「不会崩」的佐证**。

#### D. 纪律回扣（§13.16）

两版都**违反 §13.16 第 3 条**（实现本身要有佐证）：改动引入的绑定/状态未逐条论证作用域与还原，
自检只做了文本层面。**后续若要再做**，必须先：
① 按 ZZMI 的守卫/还原结构逐段对齐（`ui/universal/zzmi.py:3291-3303, 3525-3536, 3839-3845, 3930-3935`）；
② **二分定位**（先只加捕获与标志、不加重跑；再加一处重跑；逐步上）；
③ 每步给出「预期现象 + 失败回退」。

### 13.18 t74：合并骨骼残余错位的真根因 —— **cb3 采集点错，运行期 X 一次都没生效过**（2026-10-22，实机验收通过）

**现象**：运行期 X（t39-X）落地后仍然错位（用户：「怪怪的，还是错位的。我 F 8提取了一下」）。

**真根因**（抓帧 `FrameAnalysis-2026-10-22-133713` 实测，判据 = CS 自己的列映射 + `himi_object_matrix_valid()`）：

| 观测对象 | 实测 | 含义 |
| --- | --- | --- |
| deform（non-indexed）draw 的 `vs-cb3` | **368 B / 23 个 float4**，rows 0..7 的 `r3.w = 0` | 不是物体矩阵 + 其逆 ⇒ 校验恒假 |
| render（indexed）draw 的 `vs-cb3` | **160 B / 10 个 float4**，rows 0..7 = 物体矩阵 + 其逆（列口径） | 57/60 通过，残差 7.1e-06 |
| 抓帧里 12 笔 non-indexed draw | VertexCount 19895 / 3363 / 3789 / 1816 / 1496 … | **正是模组 VB 段挂的那些** |

生成器当时只把 `ResourceHI3Cb3_<fam> = copy vs-cb3 unless_null` 放进 5 个 deform（VB）段 ⇒ 每一笔采集都落在 368 B 布局上 ⇒ `himi_object_matrix_valid()` 恒假 ⇒ **运行期 X 从未生效，一直在用导出期烘死的 X**。

**为什么烘死不行：X 会跨帧漂移。** 9 月 23 日抓帧的烘死值 vs 10 月 22 日 live 值：694025c6 155.797° → **175.415°**（`|ΔX|max` 0.356）、70a4da3e 26.669° → **11.080°**（0.359）、b35cfc6c 179.125° → **172.897°**（0.405）、def0c0ae 151.308° → **149.814°**（0.283）；宿主族恒等。
**本节同时作废两条我方此前的错误结论**：「X 恒定」与「运行期 X ≡ 烘死 X」——它们只用了几帧**同帧内**的一致性（同族逐 draw 确实逐位相同），跨帧一比即被推翻。

**修法**

1. **采集点**：**每一个** `run = CustomShaderHI3MergedSkeletonAttach_<fam>` 站点（共 17 个 = 5 deform + 12 render）之前都插本族 cb3 副本；两行同源于 `common/himi_merged_runtime.py:935` `render_attach_render_capture_lines`，不另拼文本。
2. **CS 三态**（**任何分支都不 return**，draw 仍无条件执行）：(a) `own == base` ⇒ 单位阵；(b) 采集有效 ⇒ **先把本族三行写进物体矩阵表**，再读宿主族的行算 `X = M_base⁻¹·M_own`；(c) 采集无效 ⇒ 只读表里 own + base 两段算，表空才退回载荷里的烘死 X。**采集无效时绝不写表** —— 否则 deform 段那笔会把 render 段刚写好的好值覆盖成垃圾。

**验收证据（离线，可复跑）**

- `tools/himi_family_x_oracle.py` ⇒ **ORACLE PASS**：A1 矩阵元 3.343e-08 / 端到端世界 8.207e-08 / A2 反向护栏 5.3617；新增 Oct-22 三态自检三路逐位一致（采集有效 == 直接 cb3、无效 + 表非空 == 同值、无效 + 空表 == 载荷逐位）。
- `pytest tests/test_himi_family_x.py tests/test_himi_merged_runtime.py` ⇒ **232 passed / 26 subtests passed**（另 2 条既存环境失败：`HIMIRealDumpEndToEndTests::test_real_role_end_to_end`、`HIMICacheOnlyOracleTests::test_real_dump_missing_target_none_is_not_authoritative`，与本次无关）。
- 文本硬门：生成的 ini 里 run 站点 17 个、`copy vs-cb3 unless_null` 17 行、每站点上一非空行 == 本族副本；探针变异删写表 / 删读表 / 把兜底 X 提前，各让 13 条测试变红。
- **实机：用户验收通过。**

**遗留（已知边界）**：deform pass 早于 render pass ⇒ 合并 draw 消费的 X 必然是**上一帧 render** 写下的（天然 1 帧滞后）；某族当帧没有 render draw 时表可能整帧为空而退回烘死 X（符合设计）。

## 14. 附：本次新增/引用的证据与工具

| 路径 | 说明 |
|---|---|
| `reports/himi-merge/01-dump-facts.md` | T1 五帧事实表（**数据口径最终依据**） |
| `reports/himi-merge/02-zzmi-anatomy.md` | T2 ZZMI 解剖（**已知 4 处错误，见 `05-decisions.md` §1**） |
| `reports/himi-merge/03-himi-integration-points.md` | T3 HIMI 接入点审计（IP 编号来源） |
| `reports/himi-merge/04-probe-results.md` / `.json` | T4 探针结果（数字基线；**原始数据文件不得改写**） |
| `reports/himi-merge/05-decisions.md` | T5：T1↔T4 裁决、被否决备选方案、口径更正 |
| `reports/himi-merge/08-verification.md` / `08-verify-independent.py` / `.json` | T8 独立验证（159 条独立 check；§4.2/§4.3 两条设计边界；§4.4 资源声明旧口径检出） |
| `reports/himi-merge/09-review.md` | T9 审查（6 条 findings，全部由 T12 关闭） |
| `reports/himi-merge/13-stub-anatomy.md` | **T19 站位机制规格取证**（449 行 / 240 处 `文件:行号`；**只取证 ZZMI/EFMI 事实、不下 HIMI 判定**；§0/§4.3 的「两条作用分离」是 §7.7 的结构依据） |
| `reports/himi-merge/10-integration.md` | **本次**：交付清单（新增/修改文件、复现步骤、已知限制） |
| `HIMI骨骼合并计划书.md` | **本次**：本文件（定稿） |
| `.dbg/himi_dump_facts.py` | T1 取证脚本（五帧全量 + FINAL INVARIANTS） |
| `tools/himi_merged_probe.py` | T4/T6 探针（只读，`--self-check` **23/23** PASS） |
| `common/himi_skeleton.py` | T6/T12/T13 数据层 |
| `common/himi_merged_contract.py` | T6 契约纯函数（含 G1 拒绝档） |
| `common/himi_merged_runtime.py` | T7 导出侧资源计划层（t20 修 `prepare_gate_oracle` 的子集重建摘要误判） |
| `ui/universal/himi.py` | T7 导出器接线 |
| `Toolset/himi_merged_skeleton_attach.hlsl` | T7 声明与索引参考（不被任何 ini 引用、不拷进 mod） |
| `reports/himi-merge/11-t13-adjudication.md` | T14：T13 三条旧契约断言的独立裁决（pass） |
| `reports/himi-merge/12-t16-adjudication.md` | T17：R-T14-1 修复的独立复核（8 场景逐项比对） |
| `reports/himi-merge/15-stub-verification.md` / `15-verify-stub.py` / `.json` | T24/T26：站位机制的独立验证（含真端到端 ini） |
| `reports/himi-merge/16-stub-review.md` / `18-stub-rereview.md` | T25 / T27：站位实现的审查与复核（F1 / F2-new / F3-new / F6 的来源）。**⚠️ 磁盘告警（t31 复核，2026-09-24）**：`18-stub-rereview.md` 是**双重编码乱码**（全仓 4321 个文本文件中唯一一个；sha256 前缀 `463569d4dce068ff`）——引用它的内容请**从 t27 的会话日志恢复**；**不要用反向转码覆盖原文件**（会留下 1575 处替换字符，且它已含 590 个 `?` 丢字节）。本计划书对 F2-new/F3-new/F6 的引用同时有任务书原文与 §7.7.11 / §9.6 的独立复述，不依赖该文件的编码 |
| `reports/himi-merge/17-t20-adjudication.md` | T23：t20 修复的独立裁决 |
| `reports/himi-merge/19-verify-injection.py` / `.json` | 注入顺序（T26 F1）的独立验证 |
| `.dbg/_t26_g1b_deadlock_probe.py` | **G1-b 死锁反证的实测脚本**（§7.7.11.1；T30 复跑确认） |
| `.dbg/_t40_f2_probe.py` / `_t40_f2_probe.json` | **T40：规则② 判据退化的独立实测**（五角色只读；`max_exclusive` / 槽位全局独占 / 旧判据多压面；§7.7.12.1） |
| `.dbg/_t40_plan_backup.md` / `_t40_05decisions_backup.md` | T40 改文档前的定点备份（恢复纪律） |
| `reports/himi-merge/05-decisions.md` §3.22 / §3.23 | **T40 的两条裁定 + 被否决方案否决表（F-2）/ 规格同步台账（F-3）** |
| `.dbg/_s13_fragments.md` | §13 重建的唯一碎片来源（designer 会话日志导出） |
| `.dbg/_plan_backup_t30_attempt3.md` / `_05-decisions_backup_t30_attempt3.md` | **T30 改文档前的定点备份**（恢复纪律要求；见 §13 重建说明） |
| `reports/himi-merge/05-decisions.md` §3.20 | **T30 裁定与方案 (B) 的否决表** |
| `reports/himi-merge/24-t44f1-investigation.md` | **T45 实证**：HIMI 未纳入 `run_merged_skeleton_preprocess` ⇒ 站位件落槽 0 的**真实机制链**（§7/§13.10 的依据） |
| `.dbg/_t46_proof_docstring_only.py` | **T46**：证明 docstring 改动**零代码**（逆向替换回到旧 sha + 剥离 docstring 的 AST 相同） |
| `reports/himi-merge/05-decisions.md` §3.24 / §3.25 | T44 的两条裁定（C17 拆解 / 白名单边界）；**T45-T46 的裁定与回写（含 T45-F4 登记）** |

### 13.19 t78：形态键与合并骨架的无损兼容 —— **顶点侧校准整块删除（方案 B）**（2026-10-22；离线验收完成，待形态键实机复验）

**现象**：用户报「HIMI 下形态键配置的生成有问题」——按键有反应，但位置错乱、撕裂或整块飞走；路线走的是直出（`direct_export_mode`）。

**真根因**：顶点侧族级校准 `Q_f`（t44 起，`ui/universal/himi.py` `_himi_calibrate_bind_positions`）只改写 Position 缓冲的**前三个通道**（44 B/行 = P12 N12 C4 TA16）；形态键走的是另一条通道（`blueprint/direct_export_shapekey_output_mixin.py` 打包 base + 增量），**从不经过这一步** ⇒ 运行期实际算的是 `Q·v_base + δ_ws`，而正确应是 `Q·(v_base + δ_ws)`。宿主族 `Q = I` 所以宿主看着没事，另外四族缺旋转与 1.6× 缩放 ⇒ 撕裂/飞走。产物指纹：`-Position_merged_packed_pos_delta.buf` 由 32220 B 变 **32196 B**，因为旧增量里混着 `v − Q·v` 这份错误差值。

**为什么可以整块删（不再用"把 Q 扩到形态键"那种补丁）**：LBS 恒等式 `world = Σ wᵢ·S[cᵢ]·v` —— 作用在顶点上的任何常量 T 都能搬到它用到的**每一个槽**上（`S → S·T⁻¹`）而结果不变 ⇒ **顶点侧校准从来不是必需项**。旧方案的 `Q_f`/`Q_f⁻¹` 本就精确相消（实测矩阵元差 1.110e-15），唯一还活着的效果就是让形态键那条路漏乘一半。方案 B 两侧一起删：写槽 `S = X_f · P_f`，顶点缓冲原样写盘。

**顺带撤销一条我方写错的门**：t78 初稿曾令「跨族借用权重 > 0 ⇒ Fatal」。方案 B 下五族顶点同处一个角色空间（bbox 同处身高 1.69、脚在 y≈0），槽里存 `X_g·P_g[k]`，对**任何族**的顶点都给真值 `M_g·P_g[k]·v` ⇒ **跨族借用天然正确**；旧方案反而会多出 `Q_g⁻¹·Q_f`（各族 Q 第 0 槽两两相差 133.38°–180.00°）必然错。现降级为一条警告 + 正向单测（≤1e-6）+ 反向护栏（旧公式 >1e-2），产物里该值实测 = 0。

**修法（6 条）**

1. `_himi_calibrate_bind_positions` 改为**只读诊断**（读 `view2d` 统计，一个字节都不写回）。
2. palette 侧不再乘 `Q_f⁻¹`；身份资源仍 **128 B / version 4**，`[1..3]` 发布**单位阵**，并在 `common/himi_merged_runtime.py verify_attach_range_files` 与契约层**无条件**断言（容差 1e-6）。
3. `Config/HIMIMergedSkeleton.json` 记 `FamilyX.VertexCalibration = "none"`、`FamilyX.Algorithm = "S = X_f · P_f（方案 B…）"`。
4. **B3 硬门**：合并骨架 + 存在非 Basis 键块 + `blueprint/direct_export.py:580-586 _needs_standard_shapekey_rounds()`（有节点没勾 `direct_export_mode`）⇒ 导出前置 `Fatal`。位置必须是 `ui/universal/himi.py:2309 _himi_enforce_shapekey_direct_mode`（`export()` 里、`super().export()` **之前**）——**不能**挂在 `common/m_ini_helper.py`：直出时 `blueprint/direct_export.py:659` 置 suppress，那段直接 return，告警永远不会出现。
5. 口径注释同步：`common/himi_merged_runtime.py` 载荷版本表、`common/himi_merged_contract.py`、`common/himi_family_x.py` 头 docstring 与 `family_x_audit_record` 的默认 Algorithm 文案。
6. 测试：`tests/test_himi_family_x.py` 新增 `ShapekeyDirectModeGateTests` / `VertexSideCalibrationRemovedTests` / `CrossFamilyBorrowingTests` / `FamilyXRecordVertexCalibrationTests` 四类；oracle 增 `A1-B`（旧 vs 新）双通道。

**验收证据（离线，可复跑）**

- `tools/himi_family_x_oracle.py`（`PYTHONUTF8=1`）⇒ **ORACLE PASS**：A1 矩阵元 7.386e-08 / 端到端世界 8.207e-08 / **A1-B 旧 vs 新 1.110e-15** / A1-RT 2.220e-16 / A2 反向护栏 5.361728397399057（694025c6 2.796 / 70a4da3e 0.869 / b35cfc6c 3.109 / def0c0ae 5.362 / 宿主 0）/ A3 载荷 128 B·`[1..3]` 单位阵·`[7]` 族索引 / 真实顶点行 19895+3363+3789+1816+1496。
- 重导产物（2026-10-22 16:47:22）：`2c706fbd-Position.buf` **31066 行**、bbox 最长边 **4.49 → 1.8107**（方案 B 确已生效）；AttachRange 全 128 B、`[1..3]` 残差 **0.00e+00**、X 三行与上一版逐位相同；Blend 跨族行 **0**、权重和恒 1.000000；ini 5 个 attach 段各 11 行非空，**每一个** `run` 前紧邻本族 cb3 副本。
- `pytest tests/test_himi_family_x.py tests/test_himi_merged_runtime.py` ⇒ 245 passed / 26 subtests（另 2 条既存环境失败：`位面武器·失序时空` 的 `Config/FrameAnalysisPath.json` 指向已删的 `FrameAnalysis-2026-10-16-220530`，与本次改动无关）。

**遗留（不在本次边界，未动）**：`blueprint/*` 共享路径上三条与行序相关的隐患——(a) 前处理采 evaluated mesh 顶点序、与 `obj.data` 无相等断言（`blueprint/preprocess.py:823-839`）；(b) 坐标不随 loop 索引刷新（`:864-870`/`:884`）；(c) 范围靠名字/前缀回退匹配（`blueprint/direct_export_shapekey_runtime_mixin.py:197-203`）。三条影响所有游戏类型，仅作为后续建议上报。

### 13.20 t95：「模型爆炸」的两道门 —— 写槽与重绑解耦、索引修正与借用布局解耦（2026-10-25；离线验收完成，待实机复验）

**现象**：13:54:53 那次导出的产物（`array = 657` = 219 槽）换上后实机「模型爆炸 / 极其扭曲」，`077983c9`（4241 顶点）与 `09c6e58a`（18636 顶点）一起炸。

**真根因（两条，机制没错，是门写错）**

1. **「写槽」与「重绑 vs-t0」共用一个返回值**。`ui/universal/himi.py add_unity_vs_texture_override_vb_sections`（:3083）原写法 `rebind = self._himi_rebind_for(drawib_model)`、`rebind is None ⇒ super()` 之后**直接 return**；而 `_himi_rebind_for`（:2980）在「Blend 命名空间判 local」「VB 段 Blend hash 与合并数据 CategoryHash.Blend 不一致」时返回 None ⇒ 该族连 `ResourceHI3PaletteFam_<fam> = copy vs-t0 unless_null` / `ResourceHI3Cb3_<fam> = copy vs-cb3 unless_null` / `run = CustomShaderHI3MergedSkeletonAttach_<fam>` 一起**不生成**。产物实况：`09c6e58a` 的 attach 段生成了却没有任何 `run =`（`[TextureOverride_VB_09c6e58a_09c6e58a_Blend]` 只有 `vb1`/`handling = skip`/`draw = 18636, 0`）⇒ 它的 165 个槽（34022 个权重大于 0 的通道）从未被写 ⇒ 运行期读到零矩阵 ⇒ 顶点全部压向原点 = 爆炸。IB 侧 `add_unity_vs_texture_override_ib_sections`（:4787）是同一个门（`rebind is None ⇒ return`），12 条 drawindexed 的 cb3 捕获与 run 也一并不发。
2. **Blend 索引修正与借用布局绑死**。`_himi_remap_merged_blend_to_blocks`（:4175）原来两道门：`shifted = any(info["borrow"])` 为假就 `return`（**一个字节都不改**），循环里又要求 `rebind` 是 dict ⇒「判 local ⇒ 不改字节；不改字节 ⇒ 判还是 local」死锁。

**修法（4 条）**

1. VB 侧：`rebind is None` 分支改为 `before = len(ini_section_list)` → `super()` → 新增 `_himi_inject_deform_capture(ini_builder, drawib_model, before=before)`，把 `himi_runtime.render_attach_capture_lines(family)`（palette → cb3 → run 三行）插到新增段里**第一行以 `draw` 开头**的位置；`vs-t0`/`handling`/`draw` 一律不动（不重绑 ≠ 不写槽）。
2. IB 侧：去掉 `rebind is None ⇒ return`；family 取 `str((rebind or {}).get("family") or draw_ib)`，只要 `himi_family_range(family)` 有块就插 `render_attach_render_capture_lines(family)`；`skin_lines` 仍只在 rebind 为 dict 时插（`ResourceHI3SkinOut_*` 至今没有任何 `vb0 =` 引用，属未激活路径）。
3. `_himi_remap_merged_blend_to_blocks`：删掉 `shifted` 早退；族按 `slot_maps` 的键认（不再要求 rebind），命名空间 verdict 只作**输入**；写完字节就 `self._himi_blend_verdicts.pop(draw_ib, None)`，让 ini 阶段按**修正后**的产物重判；落在本族块外、又不是局部编号的通道计 `passthrough` 原样保留；`borrow_blend_remap_problems` 的 Fatal 仍只在 `shifted` 时启用。
4. 新增 `unowned` 账目：落入「本族块 ∪ 块尾借用槽」之外的通道逐个计数，收尾大声警告（那些通道运行期读到零矩阵、顶点被拉向原点）。

**验收（离线）**

- `pytest tests/test_himi_merged_runtime.py tests/test_himi_family_x.py -q` ⇒ **267 passed / 1 skipped / 26 subtests**（2 条既存环境失败同 §13.19：`位面武器·失序时空` 的 `Config/FrameAnalysisPath.json` 指向已删的 `FrameAnalysis-2026-10-16-220530`）。
- 新测试类 `HIMILocalIndexRebaseTests`：`test_rebased_without_any_borrow_and_rebind_unblocked`（无借用也修索引，且修完立刻放行重绑）、`test_cross_family_global_references_are_left_alone`（全局布局的跨族引用原样保留、不判 unmapped）、`test_orphan_slot_references_are_counted`（零写方槽计数）。
- 已部署 mod 手工补丁（按新行为模拟，备份 `虚构游戏.ini.bak_t95_145636`）：379 → **386 行**，三处插 capture+run —— `[TextureOverride_VB_09c6e58a_09c6e58a_Blend]` + 两条 `LOD0.09c6e58a` 的 drawindexed 段。身份载荷（`09c6e58a-AttachRange.buf` uint32 = `51, 165, 4, 1212763474`）magic 命中，`res/himi_attach.hlsl` :247 走权威 base ⇒ 补丁后 [51,216) 当帧真的会被写。

**口径更正（重要，避免后人照抄错读数）**

- 一度按「Blend 行 = 16 B」读表、得出「`09c6e58a` 的索引全是本地 0、被别的骨占用」的结论是**错的**。正确口径：**stride = 32 B/行，offset 0 = 4×f32 权重，offset 16 = 4×u32 索引**。按此实测 13:54 产物：`077983c9` 权重通道 8501 / distinct 55 / 取值 0..216，越出本族块 [0,51) 的是 51(1124)、146(540)、147(546)、216(383)；`09c6e58a` 权重通道 34022 / distinct 164 / 取值 0..215（**已是全局编号**，含属于 `077983c9` 的 0..50）；`1b62725d` 3 通道全为 0。

**遗留（未修，须重新导入才能消除）**

- 槽 **[216,219)** 没有任何写方：`1b62725d` 在 13:54 的产物里没有 palette / AttachRange / attach 段（`Meshes/1b62725d-AttachRange.buf` 与 `-BlendOrigin.buf` 停在 09:32），而 `077983c9` 有 **383 个通道**引用槽 216 ⇒ 这些顶点会被拉向原点。根因是它的部件 json 没有可用 VGMap（`slot_values` 为空 ⇒ 不进 `family_ranges`）。要让这 3 个槽有写方，必须让 `1b62725d` 带合并数据重新导入；生成器侧现在只能把这件事说出来（`unowned` 警告 + `tools/himi_t91_check.py` 的「块覆盖总长 216 ≠ 219」）。

### 13.21 t98：借用槽布局（每族拥有它顶点引用到的每一根骨）+ 生成器回写（2026-10-25；离线验收完成，实机已由用户确认 t97 模组有效）

**现象与背景**：t95 让「判 local」的族也能写槽后，实机不再整体爆炸，但仍有一批顶点被拉向原点：`077983c9` 的顶点里有 **383 行池化进来的 `1b62725d` 几何**（`Meshes/077983c9-Position.buf` 4241 行 = [0,2734) 自己 + [2734,3117) `1b62725d` + [3117,4241) `09c6e58a`），它们的骨号是**全局合并槽** 216/51/146/147 —— 这些槽不在 `077983c9` 的 palette 块 [0,51) 内，运行期读到的是**零矩阵**。这正是 §13.20 遗留项的另一半：不是「没人写」，而是「写在了别人的块里」。

**为什么原设计从未生效（决定性证据）**：`common/himi_merged_runtime.py compute_family_ranges` 的借用集来自 `part.referenced_slots`，而它是 `local_group_slots(payload)` = 组件 json 的 **VGMap 全部值**；VGMap 只映射**本物体自己的局部顶点组**（实测 `077983c9`: 51 项 → 0..50、`09c6e58a`: 165 项 → 51..215、`1b62725d`: 1 项 → 216）⇒ **引用集 ≡ 注册集** ⇒ `family_borrows` 恒空。真正的跨族引用只存在于**导出后的 Blend 字节**里（池化进来的别族顶点），plan 阶段根本看不到。

**修法（3 条，`ui/universal/himi.py` + `tests/`）**

1. **借用集改为从产物 Blend 实测发现**：新增 `_himi_discover_borrow_slots(self, plan, *, slot_count)`——用 `himi_runtime.compute_family_ranges(parts, slot_count, complete=False)` 建 owner 表（`owner_of` / `own_base_of` / `own_slots_of`），逐 `drawib_model` 用 `draw_ib` 认族（**不调** `_himi_rebind_for`，避开 `himi_merged_runtime_plan` 的重入），取 `_himi_exported_blend_bytes` + `blend_indices_span(drawib_model.d3d11GameType.D3D11ElementList)` + `extract_blend_index_values`，逐值判归属：本族块 ⇒ 忽略；`0 <= v < own_slots` ⇒ 本族局部编号（重映射会补基址，不算借用）；别族块 ⇒ **借用**；无人认领 ⇒ **orphan**。返回 `(borrowed, orphans)`。新增 `_himi_refresh_borrow_layout()` 在 `generate_buffer_files` 的 `_himi_remap_ordinary_blend_to_local()` 与 `_himi_remap_merged_blend_to_blocks()` **之间**调用（此刻 Blend 才是最终形态、`drawib_model_list` 已完整）。
2. **借用集注入布局**：`_himi_apply_borrow_layout` 的借用集 = plan 的 `family_borrows` **∪** 发现集；布局仍是 t84/t91 的**块尾借用槽**（借用槽紧跟本族块尾，`target_slot = base + own_slots + offset`，`source_local = 全局槽 − 源族 own_base`）。HLSL 基址表、身份资源、palette array、BorrowMap、Blend 索引全部从 `plan.family_ranges` 派生，因此自洽。
3. **orphan 升级为导出期硬门**：`_himi_apply_borrow_layout` 里 orphan 非空 ⇒ `Fatal('导出 Blend 引用了本次导出里没有写方的合并骨架槽（运行期读到零矩阵 ⇒ 顶点被拉向原点、模型爆炸）：…')`，消息含族名 + 槽号 + 处置建议；`_himi_remap_merged_blend_to_blocks` 同步加 `local_rebased` 逻辑（`verdict != 'global'` 且 `0 <= v < own_slots` ⇒ `v += own_base`），并让 `mapping` 未命中的跨族引用**不再原样放行**（计 `unowned`/`unmapped`/`passthrough`）；`borrow_slot_problems` 由 Fatal 降为 warning，`borrow_map_layout_problems` / `attach_range_layout_problems` 两条 Fatal 保留。

**离线验收（可复跑）**

- `pytest tests/test_himi_merged_runtime.py tests/test_himi_family_x.py -q` ⇒ **267 passed / 1 skipped / 26 subtests**（2 条既存环境失败同 §13.19）。`HIMILocalIndexRebaseTests` 的两条按 t98 语义重写：`test_cross_family_global_references_become_borrow_rows`（宿主 `aaaa1111` 引用 4/5/7 ⇒ 3 个块尾借用槽 target 4/5/6、`source_family=bbbb2222`、`source_local` 0/1/3；源族 base 右移 7；`slot_count=11`、`skeleton_extra_slots=3`；宿主 `(0,4,5,7)→(0,4,5,6)`、源族 `(4,5,6,7)→(7,8,9,10)`；`borrowed=3`、`passthrough=0`、`unmapped=0`）、`test_orphan_slot_references_abort_export`（槽 99 无写方 ⇒ `Fatal`，消息含 `aaaa1111`/`99`/`零矩阵`，`_himi_borrow_orphans == {'aaaa1111': [99]}`）。
- 真实数据离线模拟（`.dbg/t98_sim.py`，输入 = t97 之前的真实 `Meshes/<fam>-Blend.buf` 与工作空间 parts）：owner 块 = `077983c9(0,51)`/`09c6e58a(51,165)`/`1b62725d(216,1)`/`72fabb02(218,1)`；distinct 索引 077=55、09c=164、1b6=1；**发现集 = {077983c9: [51,146,147,216]}、ORPHANS = {}**，与 t97 手工推导完全一致；新布局 `077983c9` base 0 block 55 borrow `[(51,09c6e58a,0),(52,09c6e58a,95),(53,09c6e58a,96),(54,1b62725d,0)]`、`09c6e58a` base 55 block 165、`1b62725d` base 220、`72fabb02` base 221、`6e0ccfd4`/`bbbbf903` 借位（base 217、`own_block=False`），`total slots 222`；`borrow_map_layout_problems = []`、`attach_range_layout_problems = []`。
- **实机（用户确认）**：t96（脚本 `.dbg/t98_patch_t97_ini.py` 与校验 `.dbg/t98_verify_t97.py`；给 `1b62725d` 补 palette/cb3/run 与 `09c6e58a` 补 `vs-t0`）+ t97（借用槽追加在块外尾部 219..223、Blend 重映射 `077`: 51→219 / 146→220 / 147→221 / 216→222（2593 通道）、`09c`: 0→223（3 通道）、`1b6`: 0→216（3 通道）、AttachRange `[7].z` 写借用行数 4/1/0）打到已部署 mod 后，用户验收：**修正有效**。t97 独立校验：三族引用集全部落在「本族块 ∪ 借用尾」（`outside = []`）、`referenced 220 / written 222 / unwritten refs = []`、BorrowMap 逐字节符合、ini 8 项断言全 True、线程覆盖足（077 需 55 ← 1 块 64、09c 需 166 ← 3 块 192、1b6 需 1）。

**口径（HLSL 侧，`res/himi_attach.hlsl`，589 行）**：`himi_src_palette=t0`、`himi_attach_range=t1`、`himi_borrow_palette_a..e=t2,t3,t5,t6,t7`、`himi_borrow_map=t4`（`Buffer<uint4>`）、骨架 `u0`、`himi_object_matrix=u1`、`cbuffer b1 himi_obj_rows[8]`；`[numthreads(64,1,1)]`，`bone_index = dispatch_id.x`，`source_slots = palette_elements/3`；身份资源 `[0] = (base, span, version, 1212763474u)` 权威。借用由 **线程号 ≥ 源槽数** 触发（不是按 Blend 值触发）：`borrow_index = bone_index − source_slots`、`himi_borrow_map[borrow_index] = (target_slot, rank, src_local, 0)`、哨兵 `0xFFFFFFFF ⇒ return`、`borrow_dst = target_slot * 3`。

**遗留**

- 生成器改动**尚未用真实 Blender 导出复跑**（本会话环境里没有 Blender），目前只有 pytest + 真实数据的离线 plan/模拟两层证据；下次导出后应按 §9 复验：`array` = 222×3、每族 attach 段都有 `run`、`AttachRange[7].z` = 借用行数、Blend 索引全部落在「本族块 ∪ 块尾借用槽」。
- 生成器的**块尾借用布局**与 t97 的**块外尾部**布局数值不同（222 槽 vs 224 槽）但语义等价；若实机出现新的错位，先比对的应是 `plan.family_ranges` 派生出来的四张表是否自洽，而不是槽号本身。

---

### 13.22 t99：快速局部导出被 orphan 门拦住 ⇒ 可执行诊断 + 自动纳入缺失家族（2026-10-25；离线验收完成，实机待复验）

**现象**：t98 的 orphan 硬门在快速局部导出（只选 `077983c9`）时报错并中止：「导出 Blend 引用了本次导出里没有写方的合并骨架槽…：077983c9 引用槽 [216]。合并骨架共 219 槽。这些槽属于没有 palette 写入方的家族（没进本次导出，或工作空间里缺它那份组件 json ⇒ 计划里整族消失）。」这条消息本身是对的，但它回答不了「我该怎么做」。

**取证（为什么只选一族导出结构上不可能自洽）**

- 跨族引用只存在于**游戏那笔 draw 的顶点池**里：`Meshes/077983c9-Position.buf` 的 4241 行 = `077983c9` 自己 [0,2734) + `1b62725d` 383 行 + `09c6e58a` 1124 行。导出哪一族、Blender 里选中谁，都改不了那笔 draw 拼进来的别族几何。
- **预检不可能**：`part.referenced_slots` 读工作空间 `<bare>-Blend.buf`，里面只有本族**局部**编号（`077983c9` 的 51 行权重通道取值全在 0..50），跨族全局槽只在**导出后的 Blend 字节**里才出现。因此「导出前先算一遍够不够」这条路线不存在。
- 部署 mod 逐字节扫描（`.dbg/t99_scan_mod_blend.py`，stride 32 B、offset 16 起 4×u32）：`077983c9` 4241 行 distinct 55 = 0..50 + 219/220/221/222；`09c6e58a` 18636 行 distinct 164 = 51..223；`1b62725d` 3 行仅 216；**任何 Blend 都不引用 217**。

**用户裁定**：快速局部导出走 A —— **自动把缺的家族网格纳入本次导出并打印加了谁**；槽 217 只加诊断、不动布局。

**修法（3 处，`common/himi_merged_runtime.py` + `ui/universal/himi.py` + `ui/ui_func_export.py`）**

1. **orphan 消息可执行化**：新增 `workspace_slot_owners(workspace_root)`（只读组件 json 的 VGMap ⇒ `{owners: {slot: bare}, members: {bare: [unique_str]}}`）与 `describe_ownerless_slots(workspace_root, families, *, exported_bares=None)`（返回 `{lines, missing_bares}`）。`_himi_orphan_message` 用它把每个孤儿槽翻译成「属于谁、在不在工作空间、在不在本次导出」，`missing_bares` 非空 ⇒ 处置是「把它们一起选中再导出；只想导一部分时请先取消合并骨骼」，否则 ⇒ 「`LOD0/` 下补齐组件 json」。
2. **快速导出自动补家族**：`SSMTQuickExportSelected._collect_export_meshes` 先收选定网格，再 `_expand_merged_skeleton_families`：仅当 `Config/HIMIMergedSkeleton.json` 存在时，用 `workspace_slot_owners(root)['members']` 与 `missing_family_bares` 求差集，遍历 `bpy.data.objects` 把 type 为 MESH、未选中、bare 命中缺失集的网格补进去（`_merged_family_bare` 优先读自定义属性 `3DMigoto:WorkspaceUniqueStr`，退回落物体名），`LOG.info` 打印补了谁；场景里找不到的家族 `LOG.warning`，任何异常都不阻断导出。家族键 `merge_family_bare` 先剥 Blender 重名后缀 `\.\d{3,}$` 再剥 `LOD0.` 前缀（漏一步就会把一族算成两族，「缺哪个家族」永远算不对）。
3. **被证否的假设**：曾加过 `_himi_report_writerless_slots`（报「被家族注册却没有任何 attach 块会写」的槽），实现后实测**不可达**——`load_workspace_plan` 在导出集内按 `sorted(bare)` 走 `resolve_borrow_slots`（:3015）认领，每个注册槽只留一个拥有者，其余家族在该槽上的注册归零，块又是 min..max 密集区间 ⇒ 每个注册槽必落在某族块里。真实工作空间的槽 217 因此**有写方**（`6e0ccfd4-6030-0` 与三个 `bbbf903-*` 都注册，先注册的那个独占）；早先离线探针报 `owner(217)=None` 是它绕过了认领循环。该诊断已删除，改为回归测试 `HIMISharedSlotClaimTests::test_shared_registration_is_claimed_by_first_family` 把这条事实钉住（共享槽由先注册者独占、覆盖集必须包含所有被注册槽）。

**离线验收（可复跑）**

- `pytest tests/test_himi_merged_runtime.py tests/test_himi_family_x.py -q` ⇒ **270 passed / 1 skipped / 26 subtests**（2 条既存环境失败同 §13.19）；新增 `HIMISharedSlotClaimTests`（共享注册的认领与覆盖）与 `HIMIFamilyNameTests`（`merge_family_bare('LOD0.077983c9-10524-0.001') == '077983c9-10524-0'`、`missing_family_bares` 保序去重）。
- `py_compile ui/universal/himi.py ui/ui_func_export.py common/himi_merged_runtime.py` ⇒ OK。
- `.dbg/t99_owner_check.py`（全部 9 部件的离线 owner 复现）与 `.dbg/t99b_probe.py`（`compute_family_ranges` 在共享槽/不相交槽下的形状）保留作证据。

**遗留**

- 自动补家族只在 pytest 与离线探针里验过，**尚未在真实 Blender 里跑一次快速局部导出**；下次导出应确认：日志出现「已自动补入 N 个家族网格物体」、orphan 门不再触发、mod 的 `array` = 222×3（§13.21 的块尾借用布局）。
- `.dbg/` 下 t98/t99 的六份脚本是取证工具，不属于产品代码。

---

## 13.23 t100 —— 最新导出里 `LOD0.09c6e58a-4386-57792` 消失：IB 段被注入蒙皮 pass

**现象（用户 m01572）**

16:16 那次导出后，`LOD0.09c6e58a-4386-57792` 这个物体**不显示**（1b62725d 同样消失），
而 077983c9 正常。

**取证**

1. 缓冲与索引全部正常：09c6e58a-Position.buf 34026 行、`LOD0.09c6e58a-4386-57792` 的 IB
   `drawindexed = 72456` ⇒ max 18632 < 34026 行，四份 IB 均不越界；三族 Position 无 NaN/Inf、
   无原点行；Blend 权重和恒 1.000，引用集 077(0..54)/09c(55..219)/1b6(220) 与 t98 块尾借用
   布局完全一致；AttachRange/BorrowMap 与 ini 的 `array=663`(=221×3)、Dispatch 1/3/1 自洽。
2. 与**上一版已实机验收**的 `虚构游戏.ini.bak_t97_20261025_151941`（432 行）逐块对比，唯一
   差异是 IB 段多出两行：
   ```ini
   run = CustomShaderHI3MergedSkin_09c6e58a
   vb0 = ResourceHI3SkinOut_09c6e58a
   ```
   09c6e58a 的两笔 `drawindexed` 前各有一次、1b62725d 一笔前一次（共 3 处 6 行）；077983c9 没有。
3. 机理：t99 之前 `_himi_rebind_for` 对 09c6e58a/1b62725d 返回 `None` ⇒ `add_unity_vs_texture_override_ib_sections`
   里 `skin_entry = ... if rebind is not None else {}` 恒空 ⇒ 蒙皮 CS 从不被 `run`；t95/t98
   放行 rebind 后 `skin_entry` 变非空 ⇒ 每笔渲染 draw 前跑 `CustomShaderHI3MergedSkin_<fam>`
   并把 `vb0` 换成自建蒙皮输出。该 CS（`Toolset/himi_merged_skin.hlsl`，186 行）自己的注释
   就写着失败形状：「编译失败被 3DMigoto 静默跳过 ⇒ dispatch 照常记录、缓冲一个字节都不写
   ⇒ 整具模型塌到原点消失，不报错也不崩（t89 实机）」；且它会把调色板二次叠加在已被流输出
   重放过的顶点上。该路径属 t86「路线 1」，与 §13.18 已验收的 t74 路线（attach CS 写 palette
   + `vs-t0` 重绑 + 单 draw）互斥。

**修法**

- `ui/universal/himi.py:5350` `add_unity_vs_texture_override_ib_sections` 删掉 `skin_entry` /
  `skin_lines` 的构建与 `out.extend(skin_lines)`，只保留 `out.extend(capture_lines)`
  （即「cb3 捕获 → run attach」）。全仓 `skin_entry|skin_lines` 引用数为 0。
- `himi_skin_sections()` 的段与资源发射**保持不变**（与 t97 产物一致：段存在但无人引用 ⇒
  3DMigoto 不会调用，无副作用）。将来若复活蒙皮路线必须单独立项并先在实机验 CS 编译。

**验收读数**

- `tests/test_himi_merged_runtime.py` 新增
  `test_ib_sections_never_inject_the_merged_skin_pass`：同 t54 夹具下先断言 attach 行存在
  （非恒真），再断言 IB 段里不含 `CustomShaderHI3MergedSkin_` 与 `ResourceHI3SkinOut_`。
- `pytest tests/test_himi_merged_runtime.py tests/test_himi_family_x.py -q` ⇒ **271 passed /
  1 skipped / 26 subtests**（2 条既存环境失败同 §13.19）。
- 部署侧已就地打补丁：备份 `虚构游戏.ini.bak_t100_20261025_162135`（455 行），删 6 行 ⇒ 449 行；
  三处 IB 段回到 t97 形状（「cb3 捕获 → run attach → drawindexed」）。脚本 `.dbg/t100_patch_ini.py`。

**遗留**

- 本次修的是「为什么会消失」；t74 路线的顶点变形是否完全正确仍待一次实机确认（用户 F10 后报一个 bool）。
- `Toolset/himi_merged_skin.hlsl` 与 ini 里的 `CustomShaderHI3MergedSkin_*` 段属死代码，留着是为了
  t97 产物对齐；若长期不用应整段删（避免下一个人再把它接回渲染段）。

---


---

### 13.24 t103/t104：规则④「唯一 palette 写方 ⇒ 全族补站位」与部署 mod 的站位形态（2026-10-25）

**现象（用户三连）**：「回来了，但是我发现原本那些被合并的也回来了。这什么鬼？」→「现在合并的以及原本的叠在了一起」→
「根据设计合并进来的物体需要生成极限小的3个点的站位，就和其他的骨骼合并的一样」。

**取证一：draw 清单逐段对比**（`.dbg/t100b_draws.py`；当前 449 行 vs t97 备份 432 行）：两版都是 7 个带 draw 的段，差异全在被合并部件的两段，
旧版是「3 索引站位」、新版退化成「整笔真 draw」，且两族的 VB 段 `draw` 也从 3/18636 涨成 383/34026 ⇒ 同一份几何被画两遍。

| 段 | t97（用户确认有效） | 16:16 导出 |
|---|---|---|
| 被吸收部件 09c6e58a-57792-0 | 段名带 `.001`、`drawindexed = 3,0,0` | `drawindexed = 57792,0,0` |
| 被吸收部件 1b62725d-1740-0 | 段名带 `.001`、`drawindexed = 3,0,0` | `drawindexed = 1740,0,0` |
| `TextureOverride_VB_09c6e58a_..._Blend` | `draw = 18636, 0` | `draw = 34026, 0` |
| `TextureOverride_VB_1b62725d_..._Blend` | `draw = 3, 0` | `draw = 383, 0` |

**取证二：顶点池里确实有别族副本**（`.dbg/t101_pool_dup.py`，stride 44）：`077983c9-Position.buf` 的 `[2734,3117)` 与
`1b62725d-Position.buf` 的 `[0,383)` **逐行 byte 相同 383/383**；`077983c9-Position.buf` 的 `[3117,4241)` 与
`09c6e58a-Position.buf` 的 `[29065,30189)` 相同 1121/1124。⇒ 每族导出缓冲物理包含别族顶点副本，那两笔 draw 一旦真画，必然叠影。

**已排除**：Position 无 NaN/Inf、无原点行；四个 IB 的索引全部落在缓冲行数内；AttachRange / BorrowMap / `array = 663` / Dispatch 1:3:1 自洽；
Blend 权重和恒 1.0 ⇒ 不是数据坏，是同一份几何被画了多次。

**修法 A（生成器主线）：规则④**。整族缺席时，`writerless = bool((family_slots & used) - present_family_slots)`（`common/himi_skeleton.py:1326`）——
即「它注册的槽**被引用**、且**没有任何在场家族注册**」（= 这些槽唯一的 palette 写方）⇒ `absorbed = True`、
`reason = HIMI_STUB_REASON_WRITERLESS`（常量在 `common/himi_skeleton.py:93`），成员循环里 `common/himi_skeleton.py:1382` 直接
`stubbed.append(member); continue`、**跳过** `foreign_used` 门。输入 `present_family_slots` 从 `common/himi_skeleton.py:1276` 起按
「本族 DrawIB 是否在场」聚合成员 `landed_values`；诊断文案在 `common/himi_skeleton.py:1416`（「它是这些槽唯一的 palette 写方」）。

- 为什么必须**整族全补**：同族兄弟的 `exclusive` 恒空（§7.7.12 的 F-2 结论），只补「有专属槽」的成员会让被吸收的其余成员继续原版 draw ⇒ 残留重影。
  用户口径（m01903）要求「被合并进来的物体」都拿到 3 点站位。
- 为什么这条判据安全：槽没人引用时补站位只是白抑制原版 draw（规则② 的保守方向仍正确）⇒ 判据是 `(family_slots & used) - present_family_slots`，不是「整族缺席就补」。

**修法 B（回退 t99 的自动补家族）**：`ui/ui_func_export.py` 删掉 `_collect_export_meshes` / `_expand_merged_skeleton_families` 整段
（1891 → 1763 行），导出入口回到 `_collect_selected_meshes`（现于 `ui/ui_func_export.py:1287`；导出统一收进后台路径后，
只剩快速导出入口还在调用它）；
`common/himi_merged_runtime.py` 删掉无调用方的 `merge_family_bare` / `missing_family_bares`（现 5392 行），保留 `workspace_slot_owners`
（`common/himi_merged_runtime.py:2839`）与 `describe_ownerless_slots`（`common/himi_merged_runtime.py:2865`，仍供 orphan 消息使用）；
`tests/test_himi_merged_runtime.py` 删掉 `HIMIFamilyNameTests`（现 7072 行）。

**修法 C（部署 mod 的站位形态，t104）**：`虚构游戏.ini` 备份 `虚构游戏.ini.bak_t104_20261025_165240`（451 行）→ **463 行**，
脚本 `.dbg/t104_patch_stub_ib.py`。四处改动：`ib = Resource_LOD0.09c6e58a_57792_0.001_Index`（ini:215）、`drawindexed = 3,0,0`（ini:224）、
`ib = Resource_LOD0.1b62725d_1740_0.001_Index`（ini:232）、`drawindexed = 3,0,0`（ini:241），并补两条资源声明（ini:377 / ini:404）。
站位 IB 实测：`Meshes/LOD0.09c6e58a-57792-0.001-Index.buf` = `18633,18634,18635`、`Meshes/LOD0.1b62725d-1740-0.001-Index.buf` = `0,1,2`
（各 12 B）⇒ 与 t97 已实机验收过的形态逐字一致。

**测试与护栏**：`tests/test_himi_stub_objects.py` 5 处断言按新口径改写（ST2、规则① 的 `other` 态、ST13 的 reason、ST13 变异体、ST14b），
另加 `WriterlessFamilyStubTests` 两条；`PROD_ANCHOR_DOCS` 的遍历改成跳过不存在的文件（`reports/himi-merge/` 整棵树已裁剪）；
26 处生产锚点用 `.dbg/t105_fix_prod_anchors.py` 整体平移回填，6 处测试符号锚点手工回填（`846→848`、`1592→1598`、`1628→1634` 各两处）。

**验收读数**：`python -m pytest tests/test_himi_merged_runtime.py tests/test_himi_family_x.py tests/test_himi_stub_objects.py -q`
⇒ **322 passed / 1 skipped / 26 subtests passed / 2 failed**（两条仍是 FrameAnalysis 失效的既存环境失败）。

**遗留**

- 站位注入发生在**导出期**（`ui/universal/himi.py:2039` 在 exporter `__init__` 里调注入函数）⇒ 不需要重新导入。
- t104 的 mod 形态是手改的；下一次导出由规则④ 自动生成 `.001` 站位段，需要一次逐段对比确认两者一致。
- 用户需 F10 重载后确认「3 点站位形态正常、没有叠影」。
---

### 13.25 t106 · 被吸收族自动补站位（快速局部导出不再要求「把它一起选中」）

**现象**。t103/t104 之后，用户在快速局部导出里只选了宿主 `077983c9`（另含 `09c6e58a` 的两个成员），导出被 orphan 门拦下：

> 导出 Blend 引用了本次导出里没有写方的合并骨架槽 …… `077983c9` 引用槽 `[216]` …… 它已落盘，但**没进本次导出** …… 处置：把 `1b62725d-1740-0` 一起选中再导出

用户随即给出设计口径（m02054）：「这个物体被合并进了077，它应该生成一个站位」。即：被吸收族的几何已经逐字节躺在宿主的顶点池里（§13.24 的 byte 比对），它**不需要再画几何**，但它是那些槽唯一的 palette 写方 —— 缺了它就是运行期零矩阵。正确形态是**给它补一个 3 点站位**，而不是让用户把真网格再选一次（那会重复画 = 叠影），也不是纯 Fatal 拦下。

**规则④ 的缺口**。§13.24 落地的规则④ 只覆盖「整族缺席」：`decide_stub_injection` 的判据入口是 `scene_present_bares`（场景里有哪些族）与 `used_group_ids`（本次导出引用了哪些槽），它枚举的是 `family_map` 里缺席的**族**。但实机形状里缺席的是**保管槽的那一族**，而宿主族在场 —— 规则④ 看不见「我需要一个不是我自己的写方」。

**修法**（`ui/universal/himi.py`，一处新方法 + 一处调用）：

- 新增 `_himi_add_missing_writer_stubs(self, decision, *, workspace_root, scene_present, used_group_ids, family_map) -> list`（紧邻 `_himi_cleanup_stub_objects`）：用 `himi_runtime.workspace_slot_owners(workspace_root)` 取「槽 → 拥有的 bare」，对本次导出引用的每个槽取出 owner；owner 已在场景里 ⇒ 跳过；按 owner bare 聚合成 `by_bare`。
- 对每个 bare：已在 `decision.stub_members` ⇒ 跳过；bare 不在 `family_map` 的成员并集里（工作空间没有它的组件数据）⇒ **不猜**，打一行「无法补站位，导出期 orphan 门会说明缺什么」并跳过；在 `decision.skipped_members` 里 ⇒ 移出；最后追加进 `decision.stub_members`、记 `self._himi_stub_writer_slots[bare] = slots`（诊断用），并打印「自动补一个 3 点站位（几何已在别的部件顶点池里，站位只负责给这些槽写矩阵）」。
- `_himi_inject_stub_objects`（`_himi_inject_stub_objects@2167`）在 `decide_stub_injection` 之后（调用点在 `ui/universal/himi.py:2238`）、创建站位对象之前调用它 ⇒ 补进来的 bare 与规则④/② 选出来的 bare 走**完全相同**的下游（`_himi_create_stub_object` → `HIMIStubDrawCall` → 站位 IB 段带出该族的 palette/attach）。
- 旧的 orphan 硬门（`_himi_orphan_message` → `Fatal`）**保留作后盾**：只有「连组件数据都没有、无法补站位」时才会再触发，消息里的处置句此时才真正成立。

**与规则④ 的边界**。两者互补而不重叠：规则④ 判「这个缺席的族有没有人需要它的写方」，t106 判「本次引用的槽有没有写方在场」。规则④ 已在跳过名单里的族（规则② 的保守跳桩）会被 t106 移出并升级为站位 —— 这正是「有人引用了它保管的槽」时应有的方向。

**测试与护栏**（`tests/test_himi_stub_objects.py`）：

- `MissingWriterStubTests`（4 条）：宿主 `aaaa1111-1-0` 注册槽 0、被吸收族 `bbbb2222-1-0` 注册槽 216，`used_group_ids = {0, 216}`：① 写方不在场景 ⇒ `stub_members == ['bbbb2222-1-0']` 且 `_himi_stub_writer_slots == {'bbbb2222-1-0': [216]}`；② 写方在场景 ⇒ 不补；③ 写方不在 `family_map` 成员里 ⇒ 不猜；④ 写方原在 `decision.skipped_members` ⇒ 移出并升级为站位。
- `_make_exporter` 夹具补 `exporter._himi_stub_writer_slots = {}`（`__new__` 手工构造的导出器不跑 `__init__`）。

**验收读数**：`python -m pytest tests/test_himi_merged_runtime.py tests/test_himi_family_x.py tests/test_himi_stub_objects.py -q` ⇒ **326 passed / 1 skipped / 26 subtests passed / 2 failed**（两条仍是 `Config/FrameAnalysisPath.json` 指向已删目录的既存环境失败）；`py_compile` 四个模块 OK。

**遗留**

- 本修法的「补站位」结论来自 t104 手改 mod 的实机验证（3 点站位形态显示正常、无叠影）；**自动补站位这条路径本身还没有在真实 Blender 里跑过** —— 需要一次「只选宿主」的快速局部导出确认日志里出现「自动补一个 3 点站位」且 orphan 门不再触发。
- 站位 bare 与真网格同名 ⇒ 若用户之后把被吸收族也选进来，规则④/t106 都不会重复补（`stub_members` 去重 + `scene_present` 判据），但**两族都在场时**仍可能重复画几何（宿主顶点池里已有副本）—— 那是 §13.24 的「叠影」问题，不在本条范围内。


## 13.27 t108/t109 · 后台代为导出：t106 已生效，假降级门才是新问题

### 13.27.1 现象（用户 m02168）

t106 落地后用户在 Blender 里点「快速局部导出」仍然撞到 orphan Fatal（`077983c9 引用槽 [216]`）。

### 13.27.2 取证：先看谁在执行

- `C:\Users\anlingQWQ\AppData\Roaming\Blender Foundation\Blender\5.0\scripts\addons\TheHerta4` 是 **Junction → `e:\代码\TheHerta4`**，两边是同一批文件。
- `ui/universal/himi.py` mtime 16:58:39（t106 的 `_himi_add_missing_writer_stubs` 已在其中），用户那次失败栈（`ui_func_export.py:1667 → export_parallel.py:108 → himi.py:3171 → :2971 → :2755 → :2639`）写于 17:04:21。
- 同一个 `.blend`（`C:\Users\anlingQWQ\Desktop\Untitled.blend`）用同一份代码后台跑：只选 `077983c9` 与全选两个网格都 `RESULT {'FINISHED'}`，自动补站位生效（`覆盖 221/221 槽，无空洞`）。

⇒ 结论：那份 Fatal 来自**启动于 16:58 之前**的 Blender 进程（Python 模块在启动时载入，改文件不热更新）。重启 Blender / Reload Scripts 即可。

### 13.27.3 场景实测（`Untitled.blend`）

- 只有两个网格：`LOD0.077983c9-10524-0`（5249 顶点、217 个纯数字顶点组、用到 0..50 + 51/146/147/216）、`LOD0.09c6e58a-4386-57792`（37121 顶点、用到 51..215）。
- `1b62725d-1740-0` 在场景里**没有对象**，而槽 216 被 077 引用 ⇒ t106 的自动补站位路径被真实触发。

### 13.27.4 产物读数（本次后台导出的 mod）

| 项 | 读数 |
| --- | --- |
| 骨架 | `array = 663` = 221×3；palette 51/165/1；BorrowMap 4/1/1（后两张是哨兵） |
| 身份载荷 | 077 `(0,51,4,1212763474)`、09c `(55,165,4,MAGIC)`、1b6 `(220,1,4,MAGIC)` |
| 借用表 | 077 四行 `(51,1,0,0) (52,1,95,0) (53,1,96,0) (54,2,0,0)` |
| Blend 引用集 | 077→0..54、09c→55..219、1b6→220，全部落在「本族块 ∪ 块尾借用槽」内 |
| 顶点 | 无 NaN/Inf、权重和恒 1.000 |
| draw 形态 | 宿主真几何 `drawindexed = 14982` / `72456`；被吸收族 `drawindexed = 3,0,0`（3 索引站位 IB） |

### 13.27.5 t109 修法：身份资源门只认 AttachRange

`verify_attach_range_references`（`ui/universal/himi.py`）原来用正则抓**整份 ini 的所有** `cs-t1 = ref …`，而 `[CustomShaderHI3MergedSkin_*]` 段（t100 起没有任何 `run`，段仍保留）里绑的 `cs-t1 = ref Resource<fam>Blend` 是 **VB 缓冲**，永远不在 `ResourceHI3AttachRange_*` 的声明表里 ⇒ 每次导出都假报「没有 `[Resource<fam>Blend]` 资源声明」⇒ 契约 `level=error` ⇒ `_himi_degrade_to_ordinary()`。

修法：`referenced` 只保留 `startswith(ATTACH_RANGE_RESOURCE_PREFIX)` 的引用；「整份 ini 里没有任何 AttachRange 绑定」的判据同样只看这些。回归钉子：`tests/test_himi_family_x.py` 的 `test_reference_gate_ignores_vb_bound_cs_slots`（ini 里同时有 VB 绑定的 skin 段与合法 AttachRange 段 ⇒ 问题清单必须为空）。

### 13.27.6 验收读数

- `pytest tests/test_himi_family_x.py tests/test_himi_merged_runtime.py tests/test_himi_stub_objects.py -q` ⇒ **327 passed / 1 skipped / 26 subtests / 2 failed**（两条既存环境失败：`HIMICacheOnlyOracleTests::test_real_dump_missing_target_none_is_not_authoritative`、`HIMIRealDumpEndToEndTests::test_real_role_end_to_end`）。
- 修后重导：`已写出 6 个家族资源 … 覆盖 221/221 槽，无空洞`，**不再出现「已降级为普通导出」**。
- **实机：用户 F10 重载本次产物后确认「显示正常，没有叠影」**（2026-10-25）。

### 13.27.7 遗留

- 子集导出（只导 077+09c）仍会报「本次只写 217/219 个槽，未覆盖 [217,219)」与「变形家族未完整纳入：6e0ccfd4 / 72fabb02 / bbbbf903」——这三族在本 `.blend` 里没有对象，属预期；要让它们的几何也吃到合并收益必须一起导出。
- 产物 ini 里「基址表 span 之和 217 ≠ 221」是旧口径（只数 palette span、没数借用行），只影响自述文字。
- 后台导出脚本：`.dbg/t108_export_selected.py`（`T108_SELECT=host|all` 控制选择）、`.dbg/t108_probe_scene.py`（场景物体与顶点组读数）。

## §13.28 t116/t117 借用槽两处真因：行数只走了 60/216 + X_源 恒退回宿主单位阵

### 13.28.1 现象与现场基准

用户口径：「直接导入 + 简单合并后导出 ⇒ 整具扭/错位」（m02987 / m03245 / m03289），并要求「先修模组再修生成器」（m03436）、物体变换矫正 X 必须运行期实时算（m03260）。

基准产物 = 17:50 那次蓝图导出（`K:\SSMT-Package-master\3Dmigoto\HI3\Mods\SSMTGeneratedMod\嗨♪爱愿妖精♥`）：ini 869 行；`ResourceHI3MergedSkeleton_role_0512e26f` array = 1677（559 槽 × 3）；五族 PaletteFam array 184/18/71/19/51；宿主族 = **b35cfc6c（19 根骨）**（HostEvidence 原文「Meshes/b35cfc6c-Position.buf 的缓冲最长（1039940 B）」）；其 BorrowMap **216 行**（rank 直方图 `{0: 165, 4: 51}`，target 292..507），借用权重 18776.4 ≈ **全模型 79%**；借方 2c706fbd 与 def0c0ae 与宿主的物体空间差 179.124° / 159.416°。

对照已验收的 虚构游戏：宿主 09c6e58a（165 骨）、借用行 5、唯一借方差 11.651° ⇒ **同样的缺陷在那套产物上几乎看不出来**，这就是「另一个角色正常」的原因。

### 13.28.2 决定性读数（F8 抓帧 `FrameAnalysis-2026-10-25-181539`）

`deduped\` 下 5 份 26832 B（= 559 槽 × 48 B）的合并骨架副本**读数完全一致**：

* 各族本块写满：2c 184/184、694 18/18、70a 71/71、b35 19/19、def 51/51；
* **借用尾 [292,508) 只有 292..351（60 个槽）非零，352..507（156 个槽）12 个 float 全零**，分布是硬边界 `{0: 156, 12: 60}` ⇒ 用到这 156 个槽的顶点被零矩阵拽向原点；
* 已写的 60 个槽与来源族 palette 的行**逐字节相同** ⇒ 写进去的是裸 `P_源`，**一次都没乘物体空间校正**。

借用表文件本身健康（3456 B = 216 行，target 连续 292..507、无哨兵行、无 target=0 行），ini 也声明 `array = 216`、`dispatch = 4,1,1`（256 线程 ≥ 19 + 216 = 235）⇒ 唯一的解释是运行期 `himi_borrow_map.GetDimensions()` 只回 **60**。

### 13.28.3 两个真因

1. **typed 视图分量数 ≠ HLSL 元素分量数。** 借用表声明是 `type = Buffer / format = R32_UINT / stride = 16`，而 HLSL 是 `Buffer<uint4>`（4 × u32 = 16 B 元素）⇒ 1 分量视图配 4 分量元素，落进 D3D11 未定义区（与本文件 A1 条同一个失效模式），表现为 `GetDimensions` 回不到真行数。
2. **X_源 的数据源根本不存在。** 原逻辑读 `himi_object_matrix`（cs-u1），来源是 `vs-cb3`；实测该游戏 7 笔 draw 的 `vs-cb0..cb6` 每个槽内容都**完全相同**，cb3 是投影矩阵（rows0..2.xyz 全 0、rows0..3.w = 0）⇒ 表恒零 ⇒ X 恒退回「本族载荷」= 宿主族单位阵。生成器自己在 `HIMIMergedSkeleton.json` 的 `FamilyX.Problems` 里也记过这件事（「抓帧里没有一笔 draw 的 vs-cb3 是合法对象矩阵（试过 15 笔）」）。

### 13.28.4 修法：骨对索引表 + 每帧现算矩阵

离线全量验证（3×4 仿射逆，数据源 `LOD0/<bare>/ModImpRuntime/<bare>-BoneMatrix.buf`）：`X_源 = P_本族[p] · P_来源[q]⁻¹`（**own 在前**）精确成立 —— b35[0] ↔ 2c[1] 残差 4.6e-7、b35[5] ↔ def[0] 4.9e-7；反方向 `inv(P_own)·P_src` 差 1.8 / 1.3（原来的写法方向就是错的）。

⇒ 导出期只烘「同一根物理骨在两族 palette 里的局部槽」这对**索引**，矩阵每帧从两支活 palette 现算（cs-t0 本族、cs-t2/t3/t5/t6/t7 来源族，本来就都绑在同一次 dispatch 上）：

* `ui/universal/himi.py::merged_attach_hlsl_text` 新增 `borrow_rows` / `borrow_pairs` 两个可选参数；模板发出 `#define HIMI_BORROW_ROWS`、`#define HIMI_BORROW_RANK_COUNT`、`#define HIMI_BORROW_PAIR_COUNT`、`static const uint HIMI_BORROW_PAIR_OWN/SRC[…]`（下标 = 本族序号 × RANK_COUNT + rank，`0xFFFFFFFF` = 这对不用）；
* 借用块不再读 `GetDimensions`（行数取导出期常量），新增结构门 `borrow_entry.x < base_slot + span_slots ⇒ return`（防止表被截断后把「行号 0 的那根骨」写进 slot 0 覆盖别人的块）；
* X 改为 `borrow_x = P_本族[p] · P_来源[q]⁻¹`，两支任一为全零（本帧还没捕到 palette）就退回单位阵（下一帧自愈），**不再从载荷取 X 的逆**；
* `common/himi_family_x.py::solve_palette_pair` 给出骨对；`ui/universal/himi.py::_himi_borrow_pairs` 在导出时按 `ranges` 只对「有借用行的族 × 每个 rank」求解，解不出就跳过并告警（不阻断导出）；
* `common/himi_merged_runtime.py`：`BORROW_MAP_INI_FORMAT` 改 `R32G32B32A32_UINT`（4 分量 typed，与 `Buffer<uint4>` 同宽；`stride` 仍 16）。

### 13.28.5 验收读数

* `pytest tests/test_himi_merged_runtime.py tests/test_himi_family_x.py tests/test_himi_stub_objects.py -q` ⇒ **330 passed / 1 skipped / 26 subtests / 2 failed**，两条失败仍是既存环境失败（`HIMICacheOnlyOracleTests::test_real_dump_missing_target_none_is_not_authoritative`、`HIMIRealDumpEndToEndTests::test_real_role_end_to_end`，缺 FrameAnalysis 目录与 `Config/HIMIMergedSkeleton.json`）。
* 参考渲染路（`merged_attach_hlsl_text()` 缺省参数）：`#define HIMI_BORROW_ROWS` 在、`HIMI_BORROW_PAIR_OWN[` 在、`himi_borrow_map.GetDimensions` 不在；`merged_attach_borrow_problems` / `merged_attach_hlsl_problems` 均为空。
* 真实工作空间 `K:\SSMT-Package-master\WorkSpace\HIMI\嗨♪爱愿妖精♥` 上 `_himi_borrow_pairs` 的实际结果：`{(3, 0): (0, 1), (3, 4): (0, 0)}`（残差 4.4e-16 / 2.2e-16），没有解不出的对。
* 真机未验：F10 reload 在该机器上连续 4 次导致 BH3「停止交互」被关闭（Windows `Application Hang` 17:46 / 17:51 / 17:55 / 18:09 各一条），**验证必须走重启游戏**。

### 13.28.6 遗留

* 部署目录里那份 `res/himi_attach.hlsl` 上的 t116 手工补丁（行数 216 + 结构门 + 实时 X）只作临时验证用，下一版导出会由生成器整体覆盖；
* 死蒙皮段 `[CustomShaderHI3MergedSkin_<fam>]` 仍指向不存在的 `res/himi_merged_skin.hlsl`，每次 reload 刷 `Shader not found` 噪声，待删；
* 产物 ini 诊断块「基址表 span 之和 343 ≠ 559」「out_of_range_refs = 70%」仍是审计没减 base 的老 bug（假警报），待修；
* 本角色的 559 槽里 216 个来自借用（79% 权重），宿主挑选口径（「Position 缓冲最长」）会让「19 根骨的族」当宿主 —— 是否要改成「palette 骨数最多者」需要单独决策。


## 13.29 t117 · 「第一个模组错、第二个模组对」：`sizes` 从未被带回 ⇒ 全合并判据恒成立（2026-10-26；代码已修，待实机复验）

### 13.29.1 现象

用户在 Blender 里连续导出两个工作空间，肉眼看到 **`嗨♪爱愿妖精♥` 的合并骨架整块错位**，而
**`虚构游戏` 正确**，并质问「是不是有什么写死了，按道理一切都该按工作空间实时算」。
两个产物目录的 ini / buf 时间戳是 `2026-10-26 11:05` 与 `11:06` —— 同一次会话、同一个插件，
所以不是「旧产物没重导」这类外部原因。

### 13.29.2 决定性证据：两份产物的「站位族」根本不是一回事

按 `<draw_ib>-Position.buf` 字节数 / 44 反算顶点数，并与 ini 里各 LOD 段的 `drawindexed` 对照：

| 产物 | 族 | Position | 顶点 | 自己那笔 draw | 判定 |
| --- | --- | --- | --- | --- | --- |
| 嗨♪爱愿妖精♥ | `2c706fbd` | 1040468 B | 23647 | 23647 | 宿主（真几何） |
| 同上 | `694025c6` | 152592 B | 3468 | 3468 | **真几何，自成一笔** |
| 同上 | `70a4da3e` | 174108 B | 3957 | 3957 | **真几何，自成一笔** |
| 同上 | `b35cfc6c` | 132 B | 3 | 3 | 站位股（零面积） |
| 同上 | `def0c0ae` | 264 B | 6 | 6 | 站位股（零面积） |
| 虚构游戏 | `077983c9` | 132 B | 3 | 3 | 站位股 |
| 同上 | `09c6e58a` | 最大 | — | 19512 | 宿主（真几何） |
| 同上 | `1b62725d` | 264 B | 6 | 6 | 站位股 |

⇒ 「真几何的族」与「只剩站位的族」**可以同时出现在同一份产物里**。这是本次事故的全部前提。

### 13.29.3 真因：判据拿不到尺寸，退化成「全是站位」

`ui/universal/himi.py:_himi_family_index_map()` 决定每族 `[7] = (own, base)`（运行期 CS 靠
`base` 决定读表里哪一段当 `M_base`）。`0c3823e`（2026-10-26 06:54）引入的判据是：

```python
sizes = dict((host or {}).get("sizes") or {})          # ← 但 _himi_host_family 从不返回这个键
stubs = [f for f in families if f != host_family and sizes.get(family, 0) <= 512]
if stubs and len(stubs) == len(families) - 1:          # ← 空字典 ⇒ 恒成立
    return {family: (index, host_index) for index, family in enumerate(families)}   # 全体对齐宿主
```

`_himi_host_family()` 只返回 `{ok, family, size, source, note}`，`sizes` 是它内部的局部变量。
于是 `sizes` 恒为空 ⇒ 每一族都被判成 ≤512 B 的站位族 ⇒ **全合并分支恒成立** ⇒ 五族的
`base` 全部指向宿主 `2c706fbd`。

运行期后果（`merged_attach_hlsl_text` 的分支 (b)）：`own_index != base_index` ⇒ CS 每帧算
`X = M_base⁻¹·M_own` 再左乘 keyframe：

```
S = M_2c⁻¹ · M_694 · P_694
```

而 694 那笔 draw 那一刻喂进 `vs-cb3` 的本来就是 `M_694` ⇒ 净效果 `M_694 · M_2c⁻¹ · M_694 · P`，
**多乘了一整层 `M_2c⁻¹·M_694`**。载荷里烘着的那份 X 实测就是这个量级：`694025c6` 偏
**160.56°** / 平移 0.32，`70a4da3e` 偏 **26.67°**，`b35cfc6c` 179°、`def0c0ae` 162.5° —— 整块歪掉、
错位，正是肉眼看到的「出错」。

### 13.29.4 为什么第二个模组恰好是对的

`虚构游戏` 的两族 `077983c9`（132 B / 3 v）与 `1b62725d`（264 B / 6 v）**真的**是站位股，
几何整块并进了宿主 `09c6e58a` 那一笔 draw ⇒ 「全体对齐宿主」在这一份产物上**恰好就是正确答案**。
同一条错误判据，命中了两份几何结构相反的产物：一份全站位（对），一份真假并存（错）。
这与「运行期算 X 的机制坏了」无关 —— X 一直是每帧现算的。

### 13.29.5 修法（两处，缺一不可）

1. `_himi_host_family()` 的返回里补上 `"sizes": {family: bytes}`（判定宿主时它本来就算过，
   只是没带回来）。
2. `_himi_family_index_map()` 改成**逐族**判定，而不是「全体一起判」：
   * 站位族（`sizes[f] <= 512`）⇒ `base = 宿主`（几何烘在宿主物体空间里，槽只被宿主那笔读）；
   * 其余族 ⇒ `base = own` ⇒ CS 走分支 (a)，`X` 恒为单位阵，`S = P_f`，逐帧零校正成本；
   * 尺寸**未知**的族一律按 `own`：判错的代价是少一层校正，而把有效几何判成站位会多乘一层
     `M_宿主⁻¹·M_族` —— 后者才是肉眼看得见的错位。
   * 顺带去掉 `len(families) > 1` 以外的退化路径：`sizes` 空时不再有任何「全体对齐宿主」的分支。

### 13.29.6 验收读数

* `pytest tests/test_himi_family_x.py -q` ⇒ **72 passed**（含 4 条新钉子：真实 M1 形状的逐族
  期望、无站位族全 own、未知尺寸不当站位、`_himi_host_family` 必须带回 `sizes`）。
* `pytest tests -k himi -q` ⇒ **459 passed / 3 failed**，三条都不是本次回归：
  `test_real_dump_missing_target_none_is_not_authoritative` 与 `test_real_role_end_to_end`
  在 HEAD 上同样失败（本机 `Config/FrameAnalysisPath.json` 指向的抓帧不存在、缓存不可重建）；
  `test_production_anchor_references_resolve` 是本次改了 `himi.py` 行数导致的锚点漂移，
  已把 §13.23 的 `ui/universal/himi.py:5292` 更新为 `:5317`。

### 13.29.7 待办 / 遗留

* **待实机复验**：重导 `嗨♪爱愿妖精♥` 后，`694025c6` / `70a4da3e` 应不再二次变换；`b35cfc6c` /
  `def0c0ae` 的 3 / 6 顶点站位股在自己那笔 draw 里会落到别处（零面积，不可见），这才是站位股
  本来的设计口径（§7.7）。
* 判据仍依赖 `Position` 字节数（`≤512 B` 阈值、宿主 = 最大者）。§13.28.6 里那条「是否改成
  `palette` 骨数最多者当宿主」的决策仍未做：本修法只保证「判错宿主」不再放大成「全体二次变换」。
* `_himi_family_index_map` 的 `except Exception: pass` 兜底仍会把任何异常静默降级成「全体 own」
  —— 比旧的「全体宿主」安全，但仍应改成告警，留待后续。
