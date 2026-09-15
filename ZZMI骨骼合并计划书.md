# ZZMI 骨骼合并（Merged Skeleton）计划书

## v9.2（2026-09-13）：实测悬案定性 —— 导入段生效、导出段静默失效

> ⚠️ **本轮合并（absorb/herta4test-20260915）注记**：本节连同 §修复 1/2/3 的代码
> 来自 TheHerta4Test_20260915。其中 `common/zzmi_merged_contract.py`、
> `common/ini_name_safety.py`、`Toolset/soft_body/*` 等**新增文件已吸收**；但
> `ui/universal/zzmi.py`（+41KB）与 `blueprint/node_postprocess_draginteraction.py`
> 属 lane B，是否完整落地以 `04-conflicts.md` 与 lane B 报告为准。本节文字保持
> 交付方原文不改，作为「他的意图」的原始证据。

**用户症状**：做了合并骨架以后 **mod 在游戏里整块不显示**；"我直接导出他就是错误的，
例如插件根本就没有正常输出"。

### 实测证据（全部来自用户本机，只读）

| # | 检查对象 | 结果 |
|---|---|---|
| 1 | `C:\Users\Admin\Desktop\叶瞬光 20260913.blend`（副本，headless Blender 4.5.3 打开） | 集合里有 **`SkeletonGroup_0`(12 物体) / `_1` / `_2`(4) / `_3`** —— 合并**导入段生效过** |
| 2 | 同上，逐网格顶点组 | 顶点组数 = 该网格实际用到的最大索引 + 1；**只用 1 根骨头的眉毛索引 49**、3 根骨头的后背铃铛最大索引 179、身体 397、肩膀红飘带 427 ⇒ Blender 里的顶点组**已经是全局骨骼编号**（不是部件局部 0..N-1） |
| 3 | `Mods\SSMTGeneratedMod\叶瞬光（原）\叶瞬光（原）.ini`（用户 17:18 导出） | `ResourceZZMergedSkeleton` / `CustomShaderZZMIMergedSkeletonAttach` / `ResourceZZPalette` / `ResourceZZVgMap` / `seen_` / `occ_` **各 0 次** ⇒ 合并**导出段完全没生效** |
| 4 | 同 ini 的 VB 段 | 每个部件都是 `handling = skip` + `draw = <顶点数>, 0`，正是 `add_unity_vs_texture_override_vb_sections` 的 **else（非合并）分支** |
| 5 | 同 ini 的 Meshes\*-Blend.buf | 索引布局 `[4×f32 权重][4×u32 索引]`；每部件最大索引与 #2 的顶点组上限**逐一吻合**（1 骨骼部件 49、身体 397……）⇒ 导出的几何确实带着全局编号 |

**结论**：导入段把顶点组写成了全局编号（并随 `.blend` 一起保存），导出段却没生成
运行时合并骨架 ⇒ 游戏端每个部件只有自己的小 palette，全局编号一越界，蒙皮结果就是
垃圾 ⇒ **模型整块不显示**。这不是"权重画错了"，是**两段契约断裂**。

### 为什么以前没人发现

`_collect_merged_skeleton_components()` 返回空列表时，导出器**静默**走普通导出
（`if self.merged_skeleton_component_id_dict.get(draw_ib) is not None: … else: 普通两行`），
只在控制台打几行 `[ZZMI骨骼合并] 警告 …`。用户界面上**没有任何提示**，产物看起来
"导出成功"。

### v9.2 的修复

1. **导出前契约守卫（新增 `common/zzmi_merged_contract.py`）**：
   `_export_impl()` 在**写任何文件之前**先收集组件 + 判定契约；`level == "error"` 时
   `raise RuntimeError` ⇒ Blender 弹红框、导出中止，不再交付必然坏掉的 mod。
   判定表：

   | 情况 | 结论 |
   |---|---|
   | 工作空间有合并数据 + 0 组件 + 开关**关** | **错误**：全局编号几何 + 无运行时合并骨架 |
   | 工作空间有合并数据 + 0 组件 + 开关开 | **错误**：并列出每个部件被拒绝的具体原因 |
   | 有数据 + 有组件 + 部分部件被拒 | **警告**：点名哪些部件在游戏里不显示 |
   | 无合并数据 + 0 组件 | 提示（**不是**错误）：正常普通导出 |

2. **部件级拒绝原因留痕**：`_collect_merged_skeleton_components` 给每种 `continue` 记
   `draw_ib → 原因`（元数据损坏 / VGCount 为 0 但有 VGMap / 缓存版本过期 / VGMap 未
   完整覆盖 / 槽位越界），错误信息里直接点名到部件，用户能自查。

3. **段名安全化（新增 `common/ini_name_safety.py`）**：物体名里的 `[` `]` 是 INI
   **段名定界符**，拼进 `[TextureOverride_VB_<ib>_<别名>_<类别>]` 会让段名在第一个
   `]` 处提前结束 —— `头饰[丝带]` 的 Position/Texcoord/Blend 三条段直接塌成同一个
   名字（引擎报 `Duplicate section found` 并忽略后面的）。用户这次导出正好 **4 条**
   这类警告（2 个部件 × 2 条）。修复：别名经 `sanitize_section_name_part` 把
   `[`/`]`/`;`/控制字符换成 `_`，中文与 `.`/`-`/`_`/`+` 原样保留。

### 验证

* 新增单测：`tests/test_ini_name_safety.py`（7 例）、`tests/test_zzmi_merged_contract.py`（6 例）、
  `tests/test_zzmi_merged_skeleton_ini.py` 追加 7 例（段名唯一性 / 契约守卫 / 正常合并放行）。
  **契约用例对修复前必然失败**（守卫不存在 ⇒ `_enforce_merged_skeleton_contract` 直接 `AttributeError`）。
* 全量套件 **137 通过 / 0 失败 / 0 跳过**；`qa.ps1` 生成物 lint / refs / IniParams 契约三项全过，
  两条负对照仍全中。

> 状态：**已实现（骨架分组 + 组内统一骨架直拷 attach + v9 出现次槽位/每槽守卫，用户游戏内实测通过），Blender headless 端到端 [PASS]** · 最后更新：2026-09（v9）
> 2026-08-24 去重/分组确认（已实现）；**2026-08-25 用户拍板：放弃 CB1 校准**：
> ① **单骨骼刚性部件（单权重物体）抓帧重合误并**——不同锚点骨（头顶/前额/后脑发饰/面部）在抓帧姿态下矩阵逐位相同被并成一根；修复：刚性部件命中对追加加权质心门控（<0.05 米才合并），仅拆不并、误拆零代价。
> ② **palette 与渲染 cb1 逐物体 1:1 配对**（用户拍板）——palette 把顶点蒙皮到对象空间、渲染 VS 用 cb1 对象矩阵摆到世界；跨空间引用会被 cb1 摆错位置（身体组与头部组变换差 ≈0.5m，与"头发下沉/身体上移"症状吻合）。修复：**按渲染 cb1 对象变换分组，组内统一骨架**——每组一套全宽骨架，只直拷本组骨骼；**禁止跨组别骨骼合并**（跨组引用导出时大声报警，无校准的运行时槽位永不被写入 = 原点塌陷）。
> ③ **CB1 校准整体废弃（2026-08-25 用户拍板）**：校准版（捕获各组 cb1 + 外来骨骼经 `inv(cb1_组)×cb1_源×M` 校准乘写入）经多轮实测无法稳定（捕获段匹配脆弱、未捕获残留垃圾 CB、校准偏移越调越离谱），全部移除——不再有 cb1 捕获段/捕获源字段/校准 CS，attach 退化为纯直拷（`Toolset/zzmi_merged_skeleton_attach.hlsl`）。
> ④ **未生成组件帧对齐（2026-08-26 修正）**：导出组件在每个 deform pass 即时 attach 当帧 palette，未生成组件继续走游戏原渲染的当帧 palette；两者无需双缓冲或 Present 重放。
> 这两个修正疑似此前"dump 数据层全对但游戏内持续偏移"悬案的根因——dump 总在相似姿态抓取，重合骨/跨空间引用在 dump 里恒"正确"。
> 目标：参照 EFMI 骨骼合并（`common/efmi_skeleton.py`，已端到端验证）的「工作空间反查 FrameAnalysis dump → 数据复制回工作空间缓存」模式，为 ZZMI（绝区零）工作空间提供骨骼合并数据（VGMap/VGOffset/VGCount + 骨骼 palette 缓存）。
> 与 EFMI 的关系：**只复用数据层模式**（log 解析、反查、矩阵去重、写回缓存、复选框门控）；**运行时的 CS 着色器与 INI 段落全部按 ZZZ 的数据存储位置/格式从零编写**（deform pass 挂载、vs-t0 SRV stride 48、per-pass Map + ring buffer 时序、SO 管线），不移植 EFMI 的任何着色器/段落——不同游戏数据布局完全不同。分支选项：复用复选框 `import_merged_vgmap`（「使用融合统一顶点组」），勾选则用骨骼合并，不勾选维持现状，零副作用。
> 依据：对真实 ZZZ FrameAnalysis dump（`K:\SSMT-Package-master\3Dmigoto\ZZZ\FrameAnalysis-2026-08-19-122152`）的完整逆向分析 + **配套测试工作空间 `K:\SSMT-Package-master\WorkSpace\ZZMI\希格莉德·空岛传奇` 核对**（`Config/FrameAnalysisPath.json` 已确认指向同一 dump，draw 索引逐条吻合）。

---

## 1. 背景

### 1.1 已有样板：EFMI 骨骼合并（已完成并验证）

`common/efmi_skeleton.py`（743 行）实现了终末地（EFMI）的骨骼合并：

- `EFMILogParser`：解析 FrameAnalysis `log.txt`（draw 调用 / CB 绑定 first_constant 窗口 / SRV 绑定 / dump 文件名 → deduped 路径映射）。
- `EFMIBoneMapBuilder.get_skeleton_buffer`：反查 instance config（fc=4096 窗口 `[5][0:2]` 段偏移）→ vs-t0 骨骼池 256×12 矩阵。
- `build_vg_maps`：**跨子网格按骨骼矩阵内容去重** → 每子网格 `VGMap`（local→global）。
- `EFMISkeletonMergeHelper.ensure_skeleton_data`：幂等总流程——写回子网格 json（VGMap/VGOffset/VGCount）+ 骨骼池 buf 复制到 `<子网格>/ModImpRuntime/<bare>-BoneMatrix.buf`（NTEMI 缓存模式）。
- 集成：`ImprotFromWorkSpaceFull`（`ui/ui_func_import_ssmt.py:177-198`）中 `logic_name == EFMI and GlobalProterties.import_merged_vgmap()` 双条件触发；失败不阻断导入。

### 1.2 本次目标（用户拍板）

1. ZZMI 复刻同一模式：从工作空间反查 ZZZ dump，找到骨骼数据后**复制回工作空间**，下次直接用。
2. **导入、导出都要实现骨骼合并，且就是 EFMI 那种「统一顶点组」**：所有子网格共用一套全局顶点组（全局骨架），**组件 A 可以用组件 B 的骨骼/权重**（跨部件权重合法）。
3. **直接复用复选框** `import_merged_vgmap`（`common/global_properties.py:172`，不新增 UI、不改默认值）：勾选 = 导入全局顶点组、导出走合并骨架；不勾选 = 两侧都维持现状。

### 1.3 非目标

- 不改 SSMT4 提取端（ZZZ 提取已正确捕获 deform 输入，见 §2.3）。
- 不改 ZZMI 现有导入导出**默认**行为（复选框关闭 = 现状；开启且有 VGMap 数据才走合并路径）。
- 不做「导出时反映射回局部索引」的降级 B 模式（列入后续可选，见 §7）——本期直接做与 EFMI 同构的完整版。

---

## 2. ZZZ 渲染管线实证（FrameAnalysis-2026-08-19-122152）

### 2.1 三段式管线（log.txt 逐 draw 追踪结论）

| 阶段 | draw 范围 | 内容 |
|---|---|---|
| ① CS 形变 pass | 000012-000017、000021-000028 | CS `743108cc03f39cbf` 算 morph/blendshape，写池 `f50c0d31` / `90b64ae0`（UAV） |
| ② pointlist 蒙皮变形 pass | 000001-000011、000018-000020、000029-000036（22 个） | CPU `Map` 上传骨骼 palette → 绑 **`vs-t0`**；VS（`e8425f64cfb887cd` / `9684c4091fc9e35a` / `a0b21a8e787c5a98`）读绑定姿势 vb0 + 权重 vb2 + palette，**SO 流式输出蒙皮后顶点** |
| ③ 渲染 draw（= SSMT 的 DrawIB） | 000037 起 | vb0 直接绑对应 deform pass 的 SO 输出 buffer，**渲染 draw 本身没有骨骼数据** |

**骨骼数据位置结论：在 ② 的 `vs-t0`**（每部件一个 palette SRV，float32 矩阵流，dump 在 `deduped/<内容hash>.buf`）；**权重在 ② 的 vb2**（`BLENDWEIGHTS` + `BLENDINDICES`，palette 局部索引 uint32）。

### 2.2 渲染 IB ↔ deform pass 映射（SO 输出 hash = 渲染 vb0 hash 连接，全部 11 个 IB 命中）

| DrawIB | 顶点数 | deform pass | palette (vs-t0 资源 → 内容 hash) | 权重 layout |
|---|---|---|---|---|
| 84618ee0 | 5846 | 000004 | `f6a6c781` → `f2a54012` | 4×f32 权重 + 4×u32 索引 (2840ec5f) |
| a23aa8a3 | 12314 | 000020 | `c3f98669` → `097b226c` | 同上 |
| 19086112 | 3288 | 000035 | `45c35f5b` → `26be11c4` | 同上 |
| b51bdd59 | 345 | 000036 | `c6c3b31d` → `b4f8f4d8` | 同上 |
| b20f90ea | 4643 | 000002 | `c2f5419a` → `e018278f` | 同上 |
| b30db54e | 1744 | 000008 | `773b317d` → `18fa1c05` | 同上 |
| 48625d6d | 3542 | 000018 | `f43bdd3b` → `6b520746` | 820de055（vb0 来自 CS morph 池拷贝 `a1644290`） |
| d892c658 | 488 | 000010 | `36bb475e` → `531460ff` | 2×f32 + 2×u32 (5cdc3f7c) |
| 64d7d56f | 388 | 000001 | `23de2d6a` → `eaf48535` | 单索引 R32_UINT (4e92e68b/614cf6d8) |
| 454ff522 | 76 | 000029 | `23de2d6a` → `eaf48535` | 同上 |
| add6ff13 | 193 | 000030 | `23de2d6a` → `e6f6fd14` | 同上 |

### 2.3 合并可行性实证

- `64d7d56f`（deform 1）与 `454ff522`（deform 29）的 palette dump **内容完全一致**（`eaf48535`）；`84618ee0`（deform 4）与同帧另一部件（deform 32，1793 顶点）也完全一致（`f2a54012`）——同角色部件共享同一套骨骼矩阵，**跨部件矩阵去重有实际收益，方案成立**。
- ZZMI 工作空间子网格 json 已带 `"GPU-PreSkinning": true`，`CategoryBufferList` 确认提取端捕获的是 deform 输入（POSITION/NORMAL/TANGENT←vb0、TEXCOORD←vb1、**BLENDWEIGHTS/BLENDINDICES←vb2**）。**工作空间缺的只有 palette 与 VGMap**——正是本方案要反查补齐的。

### 2.4 已识别的坑（实现必须处理）

1. **palette buffer 是 ring scratch 复用**：同一资源 hash（`23de2d6a`/`178dab71`/`f6a6c781`/`141c7638`）一帧内被多个 deform pass 重写。**必须按「该 deform pass 的 dump 逻辑文件名」`0000XX-vs-t0=<资源hash>-vs=<deformVS>.buf` 定位 deduped 内容文件，绝不能只按资源 hash 认 palette**。
2. **deform VS 不硬编码 hash**：用结构特征识别 deform pass——`Draw(VertexCount:N)` + 绑了 SO target + 绑了 vs-t0 SRV（VS hash 随版本变）。
3. **morph 部件**（48625d6d）：deform vb0 是 CS 形变池的 CopyResource 拷贝，不是静态 buffer；palette 仍走 vs-t0，不受影响。
4. **同帧多角色**：22 个 deform pass 里只有 11 个属于本角色；必须用 SO-hash join 从工作空间记录的渲染 draw 反挂，防串台（NPC 的 pass 不会被误认）。
5. **渲染 pass 的共享 `vs-t0=7dfb0292`**（内容 `ce0ededa`，所有渲染 draw 相同）：与逐部件骨骼无关，勿误用。
6. ~~矩阵步长待测定~~ **已测定（任务 0 完成，见 §2.5）**：12 floats（4×3，48 字节/骨骼）。

### 2.5 实测数据结论（任务 0，探针脚本 `.dbg/zzmi_palette_probe.py`）

对 11 个 palette buf + 15 个子网格 Blend buf 的实测：

| DrawIB | palette 骨骼数 | BLENDINDICES max+1 | 跨部件去重 |
|---|---|---|---|
| 84618ee0 | 49 | 49（完全相等） | 49 全新 |
| a23aa8a3 | 105 | 105 | 105 全新 |
| b20f90ea | 51 | 51 | 38 新（13 共享） |
| d892c658 | 16 | 16 | 16 全新 |
| b30db54e | 14 | 14 | 8 新（6 共享） |
| b51bdd59 | 11 | 11 | 11 全新 |
| 48625d6d | 10 | 10 | 9 新（1 共享） |
| 19086112 | 7 | 7 | 7 全新 |
| 64d7d56f | 1 | 1 | 0 新（整个重复） |
| 454ff522 | 1 | 1 | 0 新（整个重复） |
| add6ff13 | 1 | 1 | 1 全新 |
| **合计** | **266** | | **全局唯一 244（22 个共享槽）** |

- **步长 = 12 floats（4×3 矩阵，48 字节/骨骼）**：全部 buf 被 12 整除；旋转行单位化、平移量级正常；16-float 解释被内容否决。
- **palette 骨骼数 == 实际用量**（无填充）：`len(skeleton) >= vg_count` 校验以等号成立。
- **合并骨架 266 > 255**：BI4（454ff522/64d7d56f/add6ff13）与 BW8_BI8（d892c658）**升宽是硬需求**，无条件升宽策略正确。
- **拆分子网格共享数据**：48625d6d ×3 / 84618ee0 ×2 / a23aa8a3 ×2 的 Blend.buf 均为全量顶点（同一份），共享 palette 与 VGMap。

**两两重叠与拼接对齐实测**（探针 `.dbg/zzmi_palette_overlap.py`，bitwise 判等）：

- `b20f90ea`(51) ↔ a23aa8a3(105)：**13 根**全同（如 `#0→#9`、`#1→#10`）；拼接后 b20f90ea 只新增 38 根。
- `b30db54e`(14)：→ a23aa8a3 `#0→#9, #1→#10`；→ b20f90ea `#0→#0, #1→#1, #2-#5→#45-#48`。
- `64d7d56f#0` = `454ff522#0` = `b51bdd59#0` = `48625d6d#2`（抓帧瞬间四者矩阵逐位相同）。~~同一根挂饰骨骼四部件共用~~ **2026-08-24 修正**：几何布局取证（`.dbg/zzmi_rigid_parts_probe.py`）显示四者分处头部四个位置——64d7d56f 头顶（z≈1.645）、454ff522 前额薄件（z≈1.573）、b51bdd59#0 后脑物理发饰（y=+0.135，11 根对称骨）、48625d6d#2 面部锚点——是**抓帧重合的不同锚点骨**，游戏内动画（尤其发饰物理）分叉时会错位联动（用户游戏内实测误并，疑似此前"dump 全对但持续偏移"悬案根因：dump 总在相似姿态抓取，重合骨在 dump 里恒"正确"）。**刚性部件质心门控**后：64d7d56f、b51bdd59#0 被拆开各占各槽；454ff522#0 ↔ 48625d6d#2 质心距 0.034 < 0.05 保持合并；全局唯一骨骼 244 → **246**。
- 84618ee0 / a23aa8a3 / 19086112 / d892c658 / add6ff13 的骨骼不与其它部件重叠（独立子集）。
- **关键结论：共享骨骼的索引位置不固定**（b30db54e `#2-#5` 对应 b20f90ea `#45-#48`）——必须按矩阵内容判等去重，按索引对齐会出错。
- 矩阵形态：12 floats = 3×4（3 行单位旋转 + 平移列），旋转行模长全部 = 1.0000（无缩放）。

---

## 3. 数据链路与反查方案

### 3.1 工作空间侧可用线索（以测试工作空间 `希格莉德·空岛传奇` 实测）

- 布局：`LOD0/<drawib>-<index_count>-<first_index>/TYPE_GPU_<gametype>/<子网格同名>.json`（本角色 11 个 DrawIB、15 个子网格：84618ee0 ×2、a23aa8a3 ×2、48625d6d ×3、其余 ×1）。
- `LOD0/ComponentName_DrawCallIndexList.json`：`子网格名 -> [渲染 draw 索引]`（与 log.txt 逐条吻合，如 `b20f90ea-19182-0 -> 000038/000044/000192/000213/000225`）。
- `LOD0/DrawIB-Component.json`：drawib → component 序号 → 子网格名。
- 子网格 json（如 `b20f90ea-19182-0.json`）关键字段：
  - `GamePreset: "ZZMI"`、`GPU-PreSkinning: true`、`WorkGameType`；
  - **`CategoryHash`**：`Position=122883aa / Texcoord=5c0fefda / Blend=bf543990`——**就是 deform pass 的 vb0/vb1/vb2 资源 hash**（最直接的 join 线索）；
  - **`VertexLimitVB: "dd9c8d5e"`**——**就是 deform pass 的 SO 输出资源 hash**（第二条独立 join 线索）；
  - `CategoryBufferList`：Blend buf 文件名 + BLENDINDICES 元素（`R32G32B32A32_SINT`，EFMI 的 `_blend_indices_layout` 已兼容 SINT）；
  - `VGCount: 0 / VGOffset: 0`、无 VGMap——**待本方案回填，与 EFMI 现状同构**。
- `Config/FrameAnalysisPath.json`：SSMT4 已记录 dump 路径（EFMI `resolve_frame_analysis_dir` 三候选回退逻辑可直接搬）。

### 3.2 反查流程（核心算法：三条独立 join 路径，互为校验/兜底）

对每个工作空间子网格：

- **路径 A（最直接）**：子网格 json `CategoryHash.Position` == deform pass `IASetVertexBuffers` slot 0 资源 hash → 命中该 deform pass。（`bf543990` Blend hash 可二次校验。）
- **路径 B（SO 连接）**：子网格 json `VertexLimitVB` == deform pass `SOSetTargets[0]` 资源 hash → 命中该 deform pass。
- **路径 C（draw 索引兜底）**：`ComponentName_DrawCallIndexList.json` 取该子网格的渲染 draw → 读其 `IASetVertexBuffers` slot 0 hash（= SO 输出）→ 找 SO target 同 hash 的 deform pass；可顺带校验 `IASetIndexBuffer` hash == 子网格 drawib。

命中 deform pass 后：

4. **取 palette**：该 deform pass 的 `VSSetShaderResources` slot 0 资源 hash → 组 dump 逻辑文件名 `<draw>-vs-t0=<hash>-vs=<deformVS>` → 经 parser 的 dump_map 拿到 `deduped/<内容hash>.buf` 实际路径。
5. **解析 palette**：.buf 按 float32 流读入，按测定步长（12 或 16，见任务 0）切矩阵；按工作空间 Blend buf 的 BLENDINDICES 最大局部索引 +1 得 `vg_count`，切片到实际用量（对齐 EFMI：`len(skeleton) >= vg_count` 校验）。

> 同一 DrawIB 拆成的多个子网格（84618ee0 ×2、a23aa8a3 ×2、48625d6d ×3）会命中**同一个 deform pass 与同一份 palette**——正常：它们的权重共享同一局部索引空间，VGMap 相同、VGOffset/VGCount 相同，去重天然幂等。

### 3.3 跨子网格去重与回写

- **去重规则（用户拍板 + 实测修正）**：
  1. **同一部件内部绝不去重合并**——同部件 palette 索引是提取端权威分配，内部零重复（实测 11 个部件全部 0 个内部 bitwise 重复），原样保留。
  2. **仅跨部件之间去重合并，且只用 bitwise（字节级）判等，禁用浮点容差**——同一骨骼在同一帧被 CPU 上传到各 palette 时是同一份数据的逐位拷贝（实测共享骨骼跨部件 maxdiff = 0.00e+00）；不同骨骼或同骨骼不同帧位必然不同。
  3. **不同物体的不同编号顶点组，只要被同一骨骼矩阵驱动（bitwise 相同 = 同一骨骼），就去重为同一个全局顶点组**。例：物体 A 的 36 号顶点组与物体 B 的 7 号顶点组若由同一骨骼矩阵驱动，两者都映射到同一个全局 id，导入后在 Blender 里就是**同一个顶点组名称**（实测案例：b20f90ea 有 13 个组与其它部件共享）。
  4. **单骨骼刚性部件（单权重物体）追加加权质心门控**（2026-08-24 用户拍板）：命中对任一方 palette 仅 1 根骨骼时，bitwise 相同还须加权质心距离 < `rigid_centroid_tolerance`（默认 0.05 米）才合并，否则各占各槽。刚性部件的唯一骨骼 = 整个物体的锚点，质心即物体位置指纹——抓帧瞬间重合的不同锚点骨（头部挂件密集区高发）靠此分离。刚性部件误拆零代价（各自 attach 写同一矩阵，运行时内容恒等），误并则动画分叉时错位联动；只拆不并是安全方向。双方均为多骨骼部件时不加门控（多根同时位等不可能是巧合；真共享骨骼驱动区域质心实测可相距 0.25，加门控会误拆真共享）。缺签名时刚性命中对保守拆开。
  5. **骨架分组 + 组内统一（2026-08-24 分组拍板；2026-08-25 移除校准拍板；2026-09 v9 出现次槽位）**：palette 矩阵把顶点蒙皮到**对象空间**（列向量约定 `object = Rm·bind + tm`，12 floats 平移在 [3,7,11]），渲染 VS 用该对象的 cb1 矩阵（rows 0-3 = 对象→世界，行向量约定 `world = object·R + t`）摆到世界。**合并骨架按渲染 cb1 对象变换分组**：变换逐位相同的部件进同组（同空间），组内去重（bitwise + 刚性门控），**骨骼 id 为全局编号（组基址拼接组内槽位）**——Blender 侧组内 join 无歧义。运行时**每组每槽一套全宽合并骨架**（`ResourceZZMergedSkeleton_G<g>_s<k>`，array = 全局 max(vg_offset+vg_count)）：每个 deform pass 顶层自增出现次、按出现次（1/2 循环）把当帧 palette 复制进该槽、顶层无条件 `run` 全部 (部件, 槽) attach，用 `Toolset/zzmi_merged_skeleton_attach.hlsl` **只直拷本组当帧骨骼**；每槽守卫在本组全部部件该槽都已当帧到达时才重放合并可见几何。`[Present]` **只把 occ/seen 清零**（不重放 palette、不写任何资源复位），无任何校准乘，**也不在 [Present] 复位 `ResourceZZRedirectSO_s<k>`**（F8 构造经 2026-09 实测有害：会废掉 [Present] 清场，已回退）。**禁止跨组别骨骼合并**：外来骨骼不再经校准乘写入其它组的骨架（曾经校准版 `M' = C×M`、`C = U_目标组⁻¹ × U_源组`，世界不变性虽经单测验证，但 cb1 捕获段在多轮实测中无法稳定——捕获匹配脆弱/未捕获垃圾 CB/校准偏移，2026-08-25 整体废弃）；跨组别引用由导出侧 `_warn_cross_group_bone_references` 大声报警（无校准的运行时这些槽位永不被写入 = 原点塌陷）。json 写回 `SkeletonGroup` + 全局口径 VGMap/VGOffset。无 cb1 可解析的部件独立成组（不共享，安全方向）。实测分组见本文末尾。
- **为什么禁用容差（实测踩坑记录，`.dbg/zzmi_match_accuracy.py`）**：
  - 48625d6d 的 `#1/#8/#9` 与四部件共享骨骼 maxdiff = 3.5e-07/2.5e-06（近似非相等）——是**同一骨骼的不同动画帧姿态**（48625d6d 是脸部 morph 部件，deform pass 18 晚于 pass 1/29/30，动画已推进），容差匹配会把它们误并；
  - a23aa8a3 内部存在 maxdiff = 0.000000 的**不同骨骼**（对称/镜像骨骼浮点同值但位不同），84618ee0 内部不同骨骼最小差异仅 8.7e-04——容差稍大就误并。
  - 结论：bitwise 是精确判据（零误并零漏并），容差两头都出错。
- **算法**：每部件 palette 逐骨骼 `tobytes()` 作 dict key；跨部件命中即映射到 canonical 全局槽位（canonical 按 weighted_vertex_count 选主，复用 EFMI `build_vg_maps` 三遍扫描骨架，但判等改为 bitwise、且跳过同部件对）。产出每子网格 `VGMap{local: global}`、`VGOffset`、`VGCount`。
- **合并骨架内存布局（关键语义）**：每部件 palette 在合并骨架中**按 VGOffset 连续摆放**（VGOffset = 固定排序下前序部件 vg_count 累加），合并骨架总槽位 = Σ vg_count（本角色 266）；去重只决定顶点引用的全局 id 指向哪个 canonical 槽位，**22 个重复槽位是死槽**（无顶点引用，内容恒同，无害）。此语义与 EFMI-Tools 的 vg_offset 完全一致，运行时 CS 只需按 vg_offset 连续拷贝，无需查表。
- **回写 json**：写入各子网格自己的 `<子网格>/TYPE_GPU_<gametype>/<子网格>.json`（VGMap/VGOffset/VGCount 三字段，与 EFMI 回写位置同构）；幂等——已有 VGMap 且非 force 则跳过。
- **复制缓存**：palette buf 复制到 `<子网格>/ModImpRuntime/<子网格>-BoneMatrix.buf`（NTEMI/EFMI 同款缓存模式），并在 json 记录 `BoneMatrixFileName`。下次（或换机器）无 dump 时可直接用缓存。

---

## 4. 模块设计：新增 `common/zzmi_skeleton.py`

完全独立于 `efmi_skeleton.py`（不改 EFMI 一行），复刻其分层与 API 形状，方便对照维护：

| 类/函数 | 职责 | 对应 EFMI 样板 |
|---|---|---|
| `ZZMILogParser` | 解析 log.txt：`Draw(VertexCount:N)`、`SOSetTargets` 及其资源行、`IASetVertexBuffers/IASetIndexBuffer` 及其资源行、`VSSetShaderResources` SRV 绑定、dump 逻辑名 → deduped 路径映射 | `EFMILogParser`（正则/状态机模式直接照搬，新增 Draw/SO/IA 三类记录） |
| `ZZMIDeformResolver` | deform pass 识别（pointlist + SO + vs-t0）与 SO-hash join：渲染 draw → deform pass | 新增（ZZZ 特有，EFMI 无此层） |
| `ZZMIBoneMapBuilder` | palette buf 解析（步长探测）+ `build_vg_maps` 矩阵去重 | `EFMIBoneMapBuilder`（去重算法可直接复用/抽取共用） |
| `ZZMISkeletonMergeHelper.ensure_skeleton_data(workspace_root, target_list, force=False) -> (bool, str)` | 幂等总流程：定位 dump → 解析 → 反查 → 去重 → 回写 + 复制缓存 | `EFMISkeletonMergeHelper.ensure_skeleton_data`（含 `resolve_frame_analysis_dir` 三候选回退，直接搬） |

依赖：仅 `os/re/shutil/numpy` + `utils.json_utils`，无 bpy 依赖（保证可单测、可 Blender headless 跑）。

## 5. 集成点（分支选项，复用复选框）

### 5.1 复选框（不新增）

`GlobalProterties.import_merged_vgmap()`（`common/global_properties.py:172` 「使用融合统一顶点组」，默认 True 保持不变）。语义与 WWMI/EFMI 一致：**开 = 有 VGMap 数据就走合并骨骼；关 = 完全现状**。

### 5.2 数据生成门控（唯一新增的主动作）

`ImprotFromWorkSpaceFull`（`ui/ui_func_import_ssmt.py`）在 EFMI 段（:177-198）之后追加同构分支：

```python
if (
    GlobalConfig.logic_name == LogicName.ZZMI
    and GlobalProterties.import_merged_vgmap()
):
    from ..common.zzmi_skeleton import ZZMISkeletonMergeHelper
    ok, message = ZZMISkeletonMergeHelper.ensure_skeleton_data(
        workspace_root=GlobalConfig.path_workspace_folder(),
        target_list=<工作空间 drawib/component 列表>,
    )
    # 打印结果；失败不阻断导入（对齐 EFMI）
```

复选框关闭 → 整段不执行，零文件读写，绝对现状。

### 5.3 导入消费（走现有双条件路径，最小适配）

现有顶点组导入已是双条件门控：`json 有 VGMap and import_merged_vgmap()` → 全局索引；否则局部索引（`common/ssmt_import_helper.py:37`、`common/mesh_create_helper.py`）。ZZMI 侧只需让 mesh 创建在读子网格 json 时识别本方案写入的 VGMap 段——**有则用、无则现状**，不改动任何默认分支。实施时先核对 ZZMI 导入实际读子网格 json 的位置再定点接入（一个小适配点，不重写导入）。

**分组合集（2026-08-24 实施）**：分组版下，一键导入把每个子网格对象移入其骨架组合集 `SkeletonGroup_<N>`（挂在 LOD 合集下，颜色轮换区分；`ui/ui_func_import_ssmt.py:_zzmi_move_to_skeleton_group_collection`）。json 无 SkeletonGroup 字段（旧缓存/未生成）时保持原合集归属，零副作用。

### 5.4 导出（本期实现：A 模式，与 EFMI 同构的运行时合并骨架）

**闭环原理**：所有子网格共用一套全局顶点组（全局骨架）。导出时 BLENDINDICES **直写全局骨骼 id**；运行时由 INI 挂载的 CS 把各部件的实时 palette 拷进一块**合并骨架 buffer**（按 vg_offset 摆放），并把每个 deform pass 的 vs-t0 换绑到合并骨架——deform VS 拿全局索引直接蒙皮。**任何全局骨骼对任何部件可见，组件 A 可以刷组件 B 的权重。**

门控（与导入同一把复选框）：`LogicName.ZZMI and GlobalProterties.import_merged_vgmap()` 且子网格 json 有 VGMap → 走合并骨架导出；否则**完全现状**。

实现要点（数据层参照 EFMI 已验证模式；**着色器与 INI 段落全部按 ZZZ 数据布局从零编写**，见第 3 条）：

1. **全局索引化导出**：顶点组名 = 全局 id 数字串；导出前预处理——补缺组（fill_gaps）、剔 ignore/全局骨架外组、按名排序/改名保证 `g.group == 全局 id`（对照 NTEMI `_sort_export_vertex_groups_by_name` 与 EFMI ObjectMerger 的 `str(index)` 改名）。挂钩点：`common/submesh_model.py` BLENDINDICES 生成处（EFMI 升宽分支 :89-101 同位置加 ZZMI 分支）。
2. **升宽——实测后取消**：ZZMI 各 gametype 的 BLENDINDICES **本来就是 32 位通道**（BI16=`R32G32B32A32_SINT`、BI8=`R32G32_UINT`、BI4=`R32_UINT`——后缀数字是**字节数**不是位数），全局骨骼 id 直接装下，**无需升宽、无需 ElementFormat 行**（EFMI 的升宽是因为它的 BI4 是 R8 四通道 255 上限，与 ZZZ 无关）。实现已按此落地，导出 buffer 实测通过（见 §8）。
3. **INI Merged Skeleton 段与 CS 着色器——按 ZZZ 数据布局从零编写（不移植 EFMI 实现）**：EFMI 的段落/着色器面向终末地的渲染 draw + instance config 体系，**只借鉴「逐组件 attach + 换绑合并骨架」的概念**，以下要素全部按 ZZZ 实测重新定义：
   - **CS（零延迟版）**：`Toolset/zzmi_merged_skeleton_attach.hlsl`——输入 = 当前 deform pass 的 vs-t0（cs-t0 保存的当帧 palette，`StructuredBuffer<ZZBone3x4>`，stride 48）+ **vg_map 表**（cs-t1，`StructuredBuffer<uint4>`，槽位在 .x；使用二进制文件加载，避免本 fork 的多行 data 只写入第 0 个元素）；输出 = **本组本槽**的 `RWStructuredBuffer` 合并骨架（全宽，槽位 = 全局骨骼编号）；逻辑 = **按 vg_map 写槽位** `merged[vg_map[id].x] = palette[id]`（本部件引用的骨骼——含跨部件共享 canonical——当帧覆盖）。无 cb1 输入、无校准乘（校准版 CS 2026-08-25 废弃删除）。**每个 (部件, 槽) 一个 attach 段（`CustomShaderZZMIMergedSkeletonAttach_C<i>_s<k>`），由 deform VB 段在顶层无条件 `run` 全部 槽×部件**（`run` 绝不在 if 内：本 fork 中 if 内的 run 不执行 → 骨架为空 → 模型消失）；`[Present]` 只把 `$zz_ms_occ_*` / `$zz_ms_seen_*` 清零，不重放 palette、不复位任何资源。
   - **INI 匹配键**：挂点段用 `checktextureoverride` 匹配 deform pass 的 **vb0/vb2 hash**——这两个键**工作空间子网格 json 里就有**（`CategoryHash.Position` / `CategoryHash.Blend`），生成器直接取用；NPC 的 deform pass hash 不在列表天然排除。
   - **换绑**：attach 后把该 pass 的 `vs-t0` 换绑到合并骨架 Resource（SRV 视图按 stride 48 声明）。
   - **与 ZZMIv1 的组合**：ZZMIv1 的 skin commandlist 在同一批 deform draw 上做 vb0-3/ib 的 mod 替换（全局索引 vb2 即经此路径生效）与 ps-tXX 清理，**不触碰 vs-t0**；我方段落只做 palette attach + vs-t0 换绑，职责不重叠。commandlist 先后次序与守卫需游戏内验证（任务 7）。
   - **渲染 draw 不动**：渲染 pass 的 vs-t0（共享 `7dfb0292`）与 vb2 维持 ZZMIv1 现状；渲染 VS 是否消费 vb2 需游戏内观察（风险 R3）。**2026-09 v2 追加**：合并网格自动重定向下 carrier 的渲染段也不再覆写 `vb0`（游戏按实例绑定本实例 deform SO），只用 `base_vertex` 读合并段。
4. **ZZZ 运行时挂载点与帧对齐设计（2026-08-25 定案：零延迟逐 pass attach）**：11 份 palette 由 CPU 逐 pass `Map`（WRITE_DISCARD）上传且 ring buffer 复用。**渲染侧存在当帧角色级绑定矩阵（dump 143256 实证：渲染 vs-cb2 = 身体正向 + 头部逆向绑定表，每帧 Map 更新；渲染 VS/PS 消费当帧绑定；渲染 vs-t0 = 7dfb0292 部件参数表 128 矩阵对）**——"慢一帧"的 SO × 当帧绑定 = 运动时逐帧错位（静止时帧差≈0，故 dump 数据层正常；这正是"只要采用骨骼合并就错位、不合并（SO 当帧）不错位"的根因，用户实测）。因此：
    - **"全部上一帧"（Present 时序 attach）已废弃**：慢一帧与渲染当帧绑定不兼容。
    - **定案 = 零延迟逐 pass attach**：每个 deform pass：`pre` = 把该 pass 当帧 palette **copy 成持久资源** `ResourceZZPalette_<DrawIB>` → **立即 run attach CS**（cs-t1 = vg_map 表，按「局部骨骼 id → 全局槽位」写入本组骨架；本部件引用的全部骨骼——含跨部件共享的 canonical 槽位——此刻即为当帧内容）→ `vs-t0` 换绑为本组骨架 → draw 蒙皮。**deform 读到的 = 当帧姿态**，与渲染当帧绑定一致。逐 pass attach 只需本部件当帧 palette（copy 时刻有效），不依赖"当帧全套并存"（旧设计否决的只是帧尾拿全套）。
    - **2026-08-26 实测修正（渲染顺序不稳定）**：好帧与坏帧的 palette copy 均成功，但同组 target/carrier 到达顺序不同；target 先到时固定 target draw 会读取尚未被 carrier 当帧覆盖的槽位。修复为「逐组件到达标记 + 合并可见 draw 依赖守卫」：carrier/target 挂点都保留 guarded draw，只有所需 palette 全部当帧 attach 后的挂点重放一次；因此不依赖固定 DrawIB 顺序，也不引入整帧延迟。**2026-09 多实例分离 v2 修正**：该守卫不再带帧级闩锁——重放后即时清零相位与本组 seen，**每个实例各重放一次**（详见 §7 修复链 6）。**2026-09 v9 修正**：单份 palette/骨架/SO 在两个实例交错覆盖时仍会产出半帧拼接的骨架（运动抖动、罕见姿态反转）——改为**出现次槽位（s1/s2 循环）+ 每槽守卫**：资源按槽分份，attach 也只写该槽，守卫按槽判定「本组全部部件在该槽都已当帧到达」（详见 §7 修复链 7）。
    - **帧尾 [Present]**：**只把 `$zz_ms_occ_*` / `$zz_ms_seen_*` 清零**（跨帧兜底）；**帧级闩锁 `drawn`/`ready` 及相位计数 `$zz_ms_group_phase_*` 已废除**（v9 的槽位本身承担实例分离，不再需要相位）；**不再重放持久 palette**，也**不在帧末复位 `ResourceZZRedirectSO_s<k>`**（F8 已回退：该语句会废掉 [Present] 清场，实例加入/剔除的过渡帧残留半组状态 → 错槽重放，后加入实例闪烁直至卡死）；否则会把缺席部件/上一实例的内容重新灌入骨架，形成脏数据。
    - 效果：所有部件当帧姿态；首帧即正确（无自愈期）；**未生成组件走游戏原渲染（当帧 palette），与合并部件天然同帧一致，无需任何延迟机制**。
    - 备注：共享骨骼 canonical 槽位被后 deform 部件 attach 覆盖（同帧 bitwise 相同，覆盖无害）；先 deform 部件的 SO 已在 deform 时固定，不受后续覆盖影响。
5. **跨部件/跨组权重**：同组（相同对象空间）直接引用合法——组内统一骨架；**跨组别骨骼合并已禁止**（2026-08-25 用户拍板，无校准）——导出时 `_warn_cross_group_bone_references` 对引用非本组骨骼 id 的部件大声报警（这些槽位永不被写入 = 原点塌陷）。用户只应把同组部件 join 到同一对象。
6. **未生成组件（2026-08-25 定案：零延迟后无需任何机制）**：合并部件 deform 输出当帧姿态，未生成部件走游戏原渲染（当帧 palette）——两者天然同帧一致，**延迟双缓冲机制（`ResourceZZDelayedPalette_<>` / `TextureOverride_VB_ZZDelayed_<>`）已整体废弃删除**。导出子集时未生成部件保持原版渲染即可。

## 6. 任务拆解（TDD，每步可独立验收）

| # | 任务 | 状态 |
|---|---|---|
| 0 | **实证测定**：矩阵步长 = 12 floats（48 字节/骨骼）；palette 数 == 用量；合并骨架 Σ 266 槽 / 全局唯一 244；升宽需求后经实测取消（全 32 位通道） | ✅ 结论在 §2.5 |
| 1 | `ZZMILogParser` + 单测 | ✅ `tests/test_zzmi_skeleton.py`（22 deform pass、SO/vb0/SRV/IB 绑定、dump_map） |
| 2 | `ZZMIDeformResolver`（A/B/C 三 join）+ `ZZMIBoneMapBuilder`（同部件不去重 + 跨部件 bitwise）+ 单测 | ✅ 15 子网格全部命中；48625d6d `#1/#8/#9` 异帧不误并回归测试在列 |
| 3 | `ZZMISkeletonMergeHelper.ensure_skeleton_data`：回写 json + ModImpRuntime 缓存 + 幂等 | ✅ 15 子网格写回；二次运行全跳过；force 重建 |
| 4 | 导入门控（`ui/ui_func_import_ssmt.py`，ZZMI + 复选框）；VGMap 消费走 `create_mesh_from_json` 既有双条件路径（零导入改动） | ✅ |
| 5 | 导出侧：`common/submesh_model.py` ZZMI 预处理分支（补缺/剔 ignore/排序/紧凑改名，`g.group` == 全局 id）；无需升宽 | ✅ buffer 实测：全局 id 正确写入（64d7d56f/454ff522 全为 7；b20f90ea 51 个使用 id 含 13 个跨部件引用） |
| 6 | Merged Skeleton CS + INI（按 ZZZ 从零编写）：`Toolset/zzmi_merged_skeleton_attach.hlsl` + `ExportZZMI` 生成 Constants/Resource(RWStructuredBuffer stride 48)/CustomShader 段 + 逐 deform VB 段注入换绑/attach | ✅ 生成 INI 校验通过；单测 `tests/test_zzmi_merged_skeleton_ini.py`（含跨组引用守卫用例） |
| 6.5 | **移除 CB1 校准（2026-08-25 用户拍板）**：删校准 CS/捕获段/`SkeletonGroupCb1SourceIb` 字段/校准数学测试；attach 退化为逐部件纯直拷；导出侧新增跨组别引用大声报警 | ✅ 单测 + 真实数据 e2e 全绿 |
| 6.6 | **v9 出现次槽位 + 每槽守卫（2026-09 用户游戏内实测通过）**：单份 palette/骨架/SO 在多实例交错时产出半帧拼接骨架（运动抖动/罕见姿态反转）→ 改为 s1/s2 出现次槽位 + 按槽到达守卫；`run` 全在顶层、seen 顶层 sticky 累加、守卫体内只有绑定与 draw、`[Constants]`/`[Present]` 只声明/清零 occ/seen | ✅ 生成器 + 单测全绿（语义基准 = 手改 `浮波柚叶.ini` 8571B） |
| 7 | **端到端**：Blender headless（`.dbg/run_zzmi_headless_validation.ps1` + `bl_zzmi_headless_validate.py`）导入+导出 [PASS]；**游戏内实测（ZZMIv1 加载、跨部件权重、帧内一致性）待用户侧执行** | ⏳ headless ✅ / 游戏内待测 |
| 8 | 文档：CONTEXT.md 增补词条（Deform pass / Merged skeleton / VGMap） | ✅ |

**测试数据（用户指定，已确认配对）**：工作空间 `K:\SSMT-Package-master\WorkSpace\ZZMI\希格莉德·空岛传奇` ↔ dump `K:\SSMT-Package-master\3Dmigoto\ZZZ\FrameAnalysis-2026-08-19-122152`（`Config/FrameAnalysisPath.json` 指向一致；其他工作空间可能没有配套提取文件，不作为测试对象）。

## 7. 风险与后续可选

**风险清单**：

- R1（已消解）：矩阵步长 → 实测 12 floats/48 字节，无歧义（§2.5）。
- R2（已设计消除，附边界）：跨部件骨骼帧内不一致 → 按 §5.4-4「逐组件到达标记 + 合并可见 draw 依赖守卫」设计消除；不使用上一帧骨架或 Present 重放。**已知限制（2026-09 多实例分离 v2 / v9 出现次槽位）**：v9 的每槽守卫只校验「本组全部部件在该槽已当帧到达」，槽位是位置相关的——两个 pass 的实例提交顺序相反时出现次槽位会配错；某部件整帧被剔除时该槽守卫**不闭合**（保持上一帧内容，方向安全，不会画出半帧拼接），靠 [Present] 清零 occ/seen 兜底。v2 时代的另两条边界已随 v9 消失：无闩锁后 carrier 渲染 drawindexed 无条件、该场景可能读到上一帧 SO 尾部（导出期由 `_warn_merged_mesh_timing` 大声报警，N3）；`drawindexed` 隐含的 SO 容量约束（F9）。全部为 low、不阻塞，已写进生成器注释（F7 / N3 / F9）。
- R3（待游戏内验证）：渲染 draw 绑定的 vb2 也是全局索引版本（ZZMIv1 对渲染 draw 同样替换 vb2）——渲染 VS 不蒙皮（顶点已蒙皮），但若它把 blend 索引用于其他用途（描边/遮罩），全局 id 可能引起异常；任务 7 观察，若中招则只在 deform pass 替换全局索引 vb2、渲染 draw 保留原布局。
- R4（待游戏内验证）：我方 Merged Skeleton 段与 ZZMIv1 skin commandlist 在同一 deform draw 上的执行次序（vb2 替换必须先于/不干扰 vs-t0 换绑）；ZZMIv1 不触碰 vs-t0，静态分析无冲突。
- R5：ring buffer 复用 → 一律按 deform pass 的 dump 逻辑文件名定位 palette，禁止按资源 hash 全局搜索。
- R6：复选框默认 True：ensure_skeleton_data 失败必须静默降级（打印 + 不阻断导入），不得让「无 dump 的老工作空间」导入失败。
- R7（已修复，用户实测踩坑）：**合并物体 + 删面/删空组导致的两类导出事故**：
- R8（已回退）：**双缓冲合并骨架尝试失败（游戏内模型爆炸）**——[Present] 帧翻转 + 前后缓冲条件换绑的写法在 ZZMIv1 运行时下不成立，已回退到单缓冲（`vs-t0 = ResourceZZMergedSkeleton` + draw 后 attach）版本，该版本经两份游戏内 dump 实证数据正确。教训：ZZZ 管线 INI 改动必须游戏内验证，dump 数据正确不等于运行时行为正确。
- R9（已修复，2026-08-26 双帧对照）：**固定 target draw 顺序假设不成立**。好帧 `a23/b20 → b30 target`，坏帧 `b30 target → a23/b20`；两帧 copy 均成功，坏帧读到的是单缓冲内未完成的当帧组合。修复为单缓冲依赖就绪 draw，不使用 R8 已失败的 A/B 翻页。
  - **组号移位塌陷**：合并模式组名=全局骨骼 id，若数字组被删出缺口且预处理做「紧凑化重命名」，缺口之后的全局 id 全部 -1 移位 → 后续部件权重集体错位、模型塌陷消失（用户实测「删了手臂、下半身消失」即此）。修复：`_prepare_zzmi_merged_skeleton_vertex_groups` 无条件补缺 + 不删任何组 + 排序后恒等重命名 + 非数字组名告警（`common/submesh_model.py`）。
  - **对象映射丢失**：join 多个物体后只有幸存名字的部件有对象，其余部件无 DrawCall → 输出空 IB（`ib=null`）→ 游戏内整件消失。修复（三层）：① `ExportZZMI._warn_missing_drawib_parts` 大声报警列出缺失部件；② **占位小三角面自动补齐**（用户拍板）：复选框开启时，缺失部件自动创建 1e-6 三角面占位对象（权重给组 "0"，统一顶点组下恒在范围内），游戏内不可见、不再 ib=null，顺带成为「故意删除部件」的合法手段；导出后自动清理 + 残留自愈；③ **占位不是无条件的**（用户修正）：部分缺失的 DrawIB 直接补；**整个 DrawIB 缺席时**读其 VGMap 全局骨骼 id、检测是否被现存对象顶点实际引用（权重>0）——被引用 = 几何已被合并进别的对象 → 全组件补占位（抑制原版防重影）；零引用 = 用户故意不生成 → 不插桩（该 DrawIB 不进 mod，游戏内显示原版）。无反查数据的缺席 DrawIB 一律不插桩。**更进一步的「合并物体按面来源拆分导出」仍列后续可选。**

**后续可选（本期不做）**：

- **B 模式降级**：导出时按 VGMap 反映射回局部索引（零运行时依赖，但跨部件权重会被越界剔除）——作为无法使用运行时合并骨架环境（如加载端不兼容）时的降级选项，按需另起。
- ~~合并物体按面来源拆分导出~~ **已被更优解取代（用户拍板）**：目标组件继续由**物体前缀**控制（现有体系）；跨组件改派 = 分离面到新物体 + 改前缀；缺失部件由条件占位小三角兜底。已实测：全 join 成一个对象的导出结构自洽（join 目标组件画完整合并网格 drawindexed=122979，其余组件全部不可见占位，无 ib=null/重影），见 `.dbg/bl_zzmi_joined_validate.py`。
- palette 缓存的跨工作空间复用/共享池。
- ZZZ shapekey（CS morph 池）与 mod 的联动（48625d6d 类脸部部件）。

**游戏内 dump 实证记录**（FrameAnalysis-2026-08-22-094614 / 212619 / 224434 / 08-23-001555）：换绑生效（hooked pass 的 vs-t0 无 dump、未 hook 的正常）、attach 偏移与 json 逐一吻合、合并网格内头发顶点用 live palette 重建 ≈ 实际 SO 输出（残差 0~0.0004、零系统性偏移）、各部件渲染 draw 的 CB 窗口完全一致（无逐部件原点差）、跨部件内容位置在带内。**用户游戏内持续观察到的偏移在三份出错 dump 的数据层均不存在**——2026-08-24 起按两个新根因修复（刚性锚点抓帧重合误并 + cb1 对象空间分组）；2026-08-25 用户拍板放弃 CB1 校准（组内统一骨架 + 禁止跨组别合并），等待重导 + 重导出后游戏内复验证；若仍在，回到"偏移正显示的那一帧抓 dump + 截图"的诊断路线。

**修复链（首项 2026-08-23；三帧游戏内 dump 实证：164525 / 170515 / 171955，用户复验通过；后续各项以自身日期/实证标注）**——此前"偏移离谱/炸模/消失"的真根因不在时序，而在 attach CS 的两个运行时细节：

1. **ini 参数布局（本 fork 与标准版不同）**：标准 3DMigoto 的 CustomShader 参数是 4 个一组从 `IniParams[0]` 起（`IniParams[0]=(x1,y1,z1,w1)`，mouse.hlsl/3dvision2sbs.hlsl 实证）；**本 3DMigoto-Armor fork 的 `y1` 在 `IniParams[1].y`**。读错位置的行为差异（同一 CS 三帧实测）：
   - 读 `IniParams[1].y`（=y1）：count=vg_count 正确 → attach 执行；
   - 读 `IniParams[0].y`（=x1=0）：count=0 → attach 一根不写 → **G3 全零 → 蒙皮全部塌向原点 → 模型消失**。
   - 结论：`#define ZZ_ATTACH_COUNT IniParams[1].y`（`Toolset/zzmi_merged_skeleton_attach.hlsl`）。
2. **vg_map 多行 data 只写第 0 个元素**：`[ResourceZZVgMap_<DrawIB>] type=Buffer format=R32G32B32A32_UINT data = <slot> 0 0 0`（每行一个元素）在本 fork 上**只写入第 0 个元素**——CS 其余线程 `vg_map[i]` 越界读到 0 → **全部骨骼塌进 slot 0**，G3 仅 3 槽非零（`[0, 79, 88]`，slot 79/88 = 各部件 palette[0] 写入正确、slot 0 = 塌陷）→ 蒙皮 246/249 骨骼用零矩阵 → **模型炸裂**。修复：导出器把 vg_map 写成**二进制文件**（`Meshes/zz_vgmap_<DrawIB>.buf`，每元素 4×uint32 = 槽位值,0,0,0），INI 改 `filename` 加载（与 VB 资源同一路径，buffer 大小由文件决定，视图必然覆盖全部元素）；HLSL 侧 `Buffer<uint4> vg_map` 与 `format=R32G32B32A32_UINT` 精确匹配（`ui/universal/zzmi.py` `add_merged_skeleton_sections`）。
3. **合并网格自动重定向（任意 IB 挂载兑现）**：palette 是 per-pass 独立 Map 上传的 ring scratch（同资源 hash 帧内两次 dump 内容不同，141c7638→8a40ccd0/0b9416aa 实证），早 deform pass 时刻读不到晚 pass 部件的当帧骨骼——因此**合并网格（同组跨部件 join 成一个对象）物理上只能在组内最后一个 deform draw 蒙皮**。为兑现「用户可自由 join 到任意 IB」的设计承诺，导出器自动重定向（`_build_merged_mesh_redirect_plan`）：carrier（合并网格挂载的 DrawIB）deform 退化为 3 顶点 stub（保留 copy palette + attach 写当帧骨骼）；组内最后 deform draw 的 DrawIB（target）deform 追加画合并网格（绑定 carrier 的 vb0/vb2，SO 按 [target 完整导出顶点（含 stub）][merged...] 拼接，保证 target 的 remapped IB 与 SO 顶点偏移一致）；**render 阶段每个子网格仍使用自己的 hash/first_index 和 IB；carrier 只加 `base_vertex`（`drawindexed = <merged_count>,0,3` 从 SO 的第 3 行起读本段），渲染段不再覆写 `vb0`——游戏渲染 draw 的 vb0 天然是本实例 deform 的 SO（地面真值：`so0`=渲染 vb0 指针族严格分实例），重放已把本实例的合并行写进去（2026-09 多实例分离 v2，见本节修复链 6）；target/缺失部件保留极限小三角占位，不以 `ib=null` 静默跳过**；VertexLimitRaise 按 SO 实际大小重排（carrier=3 / target=Σ）；合并网格的渲染换绑 `vb1 = Resource<carrier>Texcoord`（导出顶点超原部件顶点数时，防 OOB UV）。支撑数据：json 新增 `DeformDrawIndex`/`OriginalVertexCount`（`common/zzmi_skeleton.py` 反查写回；幂等门控纳入，旧缓存自动整批刷新），透传到导出侧（`common/submesh_model.py`/`common/submesh_metadata.py`）。无法自动重定向的情形（缺 DeformDrawIndex / 配置跨 IB）由 `_warn_merged_mesh_timing` 大声报警并给出改名指引。
4. **游戏内复验（用户确认 2026-08-23）**：同组内合并（组 3 全部部件 join 成一个对象）蒙皮正确、不再炸裂/消失。G3 骨架由「3/249 槽非零」修复为全量写入（170 槽左右，分布 79..248）。
5. **边界/调度泛化修复（2026-08-24 审计）**：attach CS 曾残留测试角色专用的 `slot < 249`，会把 G4 的 249..265 全部拒写；INI 又固定 `Dispatch = 8`，在单部件 palette 超过 512 根时会漏写尾骨骼。现改为用 `src_palette`/`vg_map`/`merged_skeleton.GetDimensions` 校验真实资源边界，输出槽位上限随每组全宽骨架的 `array` 自动变化；Dispatch 按 `ceil(vg_count / 64)` 生成。回归测试覆盖 G4 全 17 槽与 513 根 palette。
6. **2026-09 多实例分离 v2（同一 IB 在场景中被画多次；用户游戏内实测通过）**：
   - **症状**：同一 IB 的多个实例（同一帧内多个渲染 draw / 多个对象变换）只有第一个实例姿态正确，其余实例用上一个实例或上一帧的骨架画合并几何（抖动、错位、卡在第一帧）。
   - **根因**：旧的（下称 v1）就绪守卫带**帧级闩锁** `$zz_ms_group_ready_g<N>` / `$zz_ms_redirect_drawn_<IB>`，只在 `[Present]` 复位——同一帧第二个实例即使重新 attach 齐自己的 palette 也永远进不了守卫；而 carrier 自己的 deform 已被替换成 3 顶点 stub、渲染段又被显式换绑到**同一个** `ResourceZZRedirectSO_<target>` 资源变量并用 `if drawn == 1` 包住，于是后续实例的 `drawindexed` 消费的是未写入/别的实例的 SO。
   - **修复（已固化进生成器 `ui/universal/zzmi.py`）**：① 守卫条件只留「全部 seen + `$zz_ms_group_phase_g<N> >= 组内部件数`」，重放后**即时**清零相位与本组 seen = **每实例一轮**，同一帧每个实例各自重放一次；② 渲染段不再覆写 `vb0`、不再包 `if drawn`，`drawindexed` 无条件执行（游戏按实例绑定本实例 deform SO）；③ 直连路径的一次性 draw 去掉 `if !$zz_ms_redirect_drawn_<IB>`，同样每实例一次；④ `[Constants]`/`[Present]` 不再声明/复位任何闩锁变量；⑤ 保留每实例 deform 段自己的 `ResourceZZRedirectSO_<target> = ref so0` 捕获（每轮捕获→重放窗口紧邻）；**`[Present]` 不得复位该资源变量**（原 F8 防御性构造 `ResourceZZRedirectSO_<target> = null` 经 2026-09 游戏内实测有害：该语句会废掉 `[Present]` 段的正常执行 → seen/phase 跨帧清场失效 → 第二实例加入/被剔除的过渡帧残留半组状态、错轮重放，表现为后加入实例闪烁直至卡死无动画；已回退并加回归断言）。
   - **边界（不阻塞，已作为已知限制写进生成器注释）**：① 组件缺席/重放不可用时守卫不闭合，可能沿用上一轮残留 seen 重放一次（[Present] 每帧兜底，F7）；② 无闩锁后 carrier 渲染 drawindexed 无条件，该场景可能读到上一帧 SO 尾部（导出期 `_warn_merged_mesh_timing` 大声报警，N3）；③ `drawindexed = <merged_count>,0,3` 隐含「本实例 SO 容量 ≥ 组内 SO 总行数」，容量由 SO owner 部件的 VertexLimitRaise 声明，非 owner carrier 组合会打印显式诊断（F9）。
   - **对照产物与回归**：手修版 `浮波柚叶.ini`（6910B，用户实测通过）作为语义基准固化进 `tests/test_zzmi_merged_skeleton_ini.py`（守卫仅 seen/phase、渲染无 vb0 覆写、Constants/[Present] 无闩锁变量、`ref so0` 捕获保留、`[Present]` 不含任何 RedirectSO 复位语句（F8 已回退））。**v9 起该基准升级为手改版 `浮波柚叶.ini`（8571B）**，见下条。
   - **v2 遗留的已知限制（已被 v9 取代）**：守卫只校验「全部 seen 已置位」，不校验这些 seen 是否来自本轮——组件缺席/重放不可用时会沿用上一轮残留 seen 重放一次（一帧级旧姿态）；这是 v9 引入出现次槽位的直接动机之一，见下条。
7. **2026-09 v9 出现次槽位 + 每槽守卫（用户游戏内实测通过）**：
   - **症状**：运动时合并几何抖动；罕见情况下整帧姿态反转/混淆（同一 IB 在场景中被画多次时尤其明显）。
   - **根因**：v2 只保存**一份** palette / 骨架 / SO，并用「组内部件当帧全部到达」守卫。同一 IB 的多个实例的 deform pass 会**交错覆盖同一份资源**：第一个实例的部件 A 写入后、部件 B 尚未到达时，第二个实例的部件 A 又把它覆盖成自己的矩阵 → 守卫成立时消费到**半帧拼接**的骨架（A 来自实例 2、B 来自实例 1）→ 抖动；两个 pass 的实例提交顺序相反时表现为姿态反转/混淆。v6（每次 deform 无条件重放）尝试绕过交错，反而更差：第一个部件到达时另一个部件的数据还是上一帧的，那一笔重放本身就是错的。
   - **v6 之前各版本的失败原因（一并固化，防止回归）**：
     - **帧级闩锁**（v1 `$zz_ms_group_ready_g<N>` / `$zz_ms_redirect_drawn_<IB>`，只在 `[Present]` 复位）→ 同一帧第二个实例永远进不了守卫却仍消费本实例的 SO；
     - **if 内赋值被静态折叠** → 只在 if 体内赋值的 `$变量` 被加载期优化器按初值折叠，整个守卫 if 被删除、重放根本不发生；
     - **`run` 进 if 不执行** → 本 3DMigoto fork 里 if 体内的 `run = <CustomShader>` 不执行，骨架为空，模型整体消失（这是 v3/v4 失败的真正原因）。
   - **修复要点（已固化进生成器 `ui/universal/zzmi.py`，语义基准 = 手改版 `浮波柚叶.ini`）**：
     1. **出现次计数**：每个部件的 deform 段**顶层**自增 `$zz_ms_occ_<i>`，在 `if $zz_ms_occ_<i> >= 3` 里回绕为 1（槽位 1/2 循环）；
     2. **当帧到达标记（sticky，顶层赋值）**：`$zz_ms_seen_<i><k> = $zz_ms_seen_<i><k> + ($zz_ms_occ_<i> == <k>)`，**绝不在 if 体内赋值**（否则被静态折叠）；
     3. **按槽捕获**：`if $zz_ms_occ_<i> == 1 … else … endif` 把本部件当帧 palette 复制进该槽的 palette 资源（`ResourceZZPalette_<draw_ib>_s<k> = copy vs-t0 unless_null`）；**SO 的捕获只由 SO owner（载体）部件做**（`ResourceZZRedirectSO_s<k> = ref so0`）；
     4. **attach 全在顶层**：每个 (部件, 槽) 一个 CustomShader attach 段（`cs-t0` = 该槽 palette，`cs-u0` = 该槽骨架，`Dispatch = ceil(vg_count/64),1,1`），deform 段**顶层无条件** `run` 全部 槽×部件 的 attach；
     5. **骨架按槽分份**：每组两份 `ResourceZZMergedSkeleton_G<g>_s1 / _s2`（array 同现状全宽）；
     6. **每槽绘制**：段末 `if <门控>`，体内**只允许** `vs-t0 = ResourceZZMergedSkeleton_G<g>_s<k>`、`so0 = ref ResourceZZRedirectSO_s<k>`、`vb2 = <载体 blend>`、`vb0 = <载体 position>`、`draw = <merged_count>, 0`、`so0 = null`——**体内不得出现 `run`、不得给 `$变量` 赋值**。门控两种：**自足挂点**（直连路径且几何只采样自己的槽位）`if $zz_ms_occ_<i> == <k>` 直接绘制；**组级门控** `if <组内所有部件的 seen_<i><k> == 1 相与>`（重定向重放宿主、吸收挂点，以及 v9.1 的**合并宿主重放**——宿主捕获本轮 SO 到 `ResourceZZRedirectSO_s<k>`，组内每个 Blend 布局兼容的挂点都发同一条 `so0 = ref … + 宿主 vb0/vb2 + draw` 重放，见第 8 条）；
     7. `[Constants]` **只声明 occ/seen**；`[Present]` **只把它们清零**（不生成任何 drawn/ready 闩锁变量，也不写任何资源复位）。
     8. 渲染段 / 载体 3 顶点前缀 stub / `handling = skip` 保持现状不变。
   - **已知限制**：
     - 两个 pass 的实例提交顺序相反时，出现次槽位会配错（与 v6 同源，槽位是位置相关的，无法在 INI 层完全消除）；
     - 某部件整帧被剔除时，该槽守卫**不闭合** = 保持上一帧内容（方向安全：不会画出半帧拼接，但该帧的合并几何不更新）；`[Present]` 的 occ/seen 清零只作跨帧兜底；
     - `ResourceZZRedirectSO_s<k>` 按槽共享（不按 target 区分）：若同一帧同一槽内有两个不同 target 的重放，后捕获者生效——当前导出每组恰有一个 target，不受影响。
   - **对照产物与回归**：手改版 `浮波柚叶.ini`（8571B，用户实测通过）作为 v9 语义基准固化进 `tests/test_zzmi_merged_skeleton_ini.py`——断言生成器输出具备：顶层 `run`（无 run 进 if）、sticky seen 顶层赋值、每槽守卫体内只有绑定与 draw、无闩锁变量、`[Constants]` 只声明 occ/seen、`[Present]` 只清零 occ/seen 且无任何资源复位。
8. **2026-09-13 v9.1 直连路径顺序无关化（FrameAnalysis 实证回归修复）**：
   - **症状（不合并）**：未 join 的多部件模组（各部件各自导出、同属一个骨架组）在游戏里**持续闪烁 / 整帧消失**；把物体全部 join 成一个对象后正常（用户口径："合并到一个没问题，不合并就闪"）。
   - **症状（合并，同一个根因的另一半）**：join 之后合并几何挂在宿主的导出 VB 上，宿主段仍用组级 seen 守卫画它 —— 宿主自己的 deform pass 每帧排在第几**不固定**，排在前面时守卫永不成立 ⇒ 合并几何整段不写（用户实测"合并之后还在闪"）。
   - **根因**：旧口径把「本槽骨架齐全」与「几何必须落盘」绑在**同一个组级 seen 守卫**上，而该守卫只可能在组内**最后一个**到达的部件那段成立。**顺序不固定已实测**：同一批 3 部件（`c2b4ce3a`/`e6afd8d1`/`403eace9`）在 `FrameAnalysis-2026-09-13-075255`、`075506` 里的 deform 顺序是 **B→C→A**，在 `080752`（用户标注"模型消失"的帧）里是 **A→B→C**：前者只有 A 被画出来，后者最后到达的是 3 顶点占位桩 `403eace9` ⇒ 整帧没有任何可见几何输出。三份 dump 里每个部件的 deform pass 都只出现 1 次，palette copy 各 1 次（6240/1296/1680 字节 = 130/27/35 骨骼），即"每帧一轮 + 谁最后到在变"。
   - **修复（已固化进生成器 `ui/universal/zzmi.py`）**：新增两个判据 + 一条重放通道：
     1. **自足挂点**（`_merged_direct_draw_is_self_contained` = 本部件几何只采样自己 vg_map 覆盖的槽位）：`if $zz_ms_occ_<i> == <k>` → `vs-t0 = ResourceZZMergedSkeleton_G<g>_s<k>` → `draw = <本部件导出顶点数>, 0`，**不引用任何其它部件的 seen**（自己的槽位已由本段顶层 attach 用当帧 palette 写全；组内共享 canonical 槽位按导出期去重口径 bitwise 相同，谁写都一样）；
     2. **合并宿主**（`_merged_group_absorbed_hosts` = 导出 VB 上挂着跨部件几何的 DrawIB）：几何引用全组骨骼 ⇒ **必须**等全组当帧到位；宿主在自己段顶层把本轮 SO 引用捕获到 `ResourceZZRedirectSO_s<k>`（与重定向路径同名资源、同语义），守卫体内显式绑定 `so0 = ref ResourceZZRedirectSO_s<k>` + 自己的 `vb2`/`vb0` + `draw = <宿主导出顶点数>, 0` + `so0 = null`；
     3. **兼容挂点重放**（`_merged_absorbed_replay_compatible`，Blend 输入布局必须一致 = BI4 与 BW16_BI16 不能混用）：**本组每个布局兼容的挂点**都发同一条宿主重放 —— 哪个挂点最后到达都能把合并几何写进宿主捕获的 SO，与提交顺序无关；布局不兼容则退回只在宿主自己段重放并**大声报警**（该组合仍依赖顺序，需要开发者介入）。
   - **实测支撑**：工作空间 `K:\SSMT-Package-master\WorkSpace\ZZMI\主角\LOD0\*` 里三个部件的类型目录均为 `...BW16_BI16_` ⇒ 用户这一例的重放布局兼容（生成器会按真实 `d3d11GameType` 的 Blend 元素签名判定，签名缺失时保守不重放）。
   - **回归**：`tests/test_zzmi_merged_skeleton_ini.py` 改写 5 个直连路径用例（契约：直连路径的任何 `if` 条件都不得出现 `$zz_ms_seen_`）+ 新增 1 个吸收挂点边界用例 + 新增 `ZZSIMergedHostDirectPathTests` 5 个用例（宿主捕获 SO / 宿主组级重放 / 兼容兄弟也重放 / 不兼容兄弟不重放 / 无兼容兄弟时报警）；全量 `pytest tests` 1758 passed / 19 skipped。

**分组实测（dump 122152，按渲染 cb1 对象变换，2026-08-24 分组定案口径）**：5 组——
| 组 | 部件 | 对象变换平移（row3） | 全局槽位范围 |
|---|---|---|---|
| G0 | 19086112 | (-15.537, 2.381, -5.661) | 0..6 |
| G1 头部 | 454ff522 / 48625d6d / 64d7d56f / b51bdd59 | (-15.212, 2.115, -5.556) | 7..29 |
| G2 头发 | 84618ee0 | (-15.209, 2.047, -5.561) | 30..78 |
| G3 身体 | a23aa8a3 / b20f90ea / b30db54e（共享 13/2/6 根） | (-15.223, 1.585, -5.513) | 79..248 |
| G4 | add6ff13 + d892c658（同空间） | (-15.459, 1.815, -5.629) | 249..265 |
全局合计 266 槽（组基址拼接组内槽位）；组内去重后唯一骨骼总计 244；运行时**每组每槽**一份全宽骨架（array=266，v9 起 `_s1`/`_s2` 两份），**只直拷本组骨骼（无校准，2026-08-25 起）**。cb1 提取口径（仅用于分组键）：dump 行逐 draw 反查 vs-cb1（绑定调用是持久状态、多数 draw 不重发，不可靠），只解析 ≤512B 的逐部件块（>512B 是多对象共享变换数组+窗口索引，首条未必是本 draw 的对象），rows 0-3 即对象→世界矩阵（w 列 0/0/0/1 校验）。

## 8. 验收标准

1. 对测试工作空间 `希格莉德·空岛传奇` 执行导入：15 个子网格全部生成并写回 VGMap/VGOffset/VGCount；palette buf 落入各 `<子网格>/ModImpRuntime/`。
2. 去重结果与实测一致：5 个骨架组（身体/头部/头发/19086112/add6ff13+d892c658），全局骨骼编号按组基址拼接（0..6/7..29/30..78/79..248/249..265，Σ 266）；头部组内 `454ff522#0` ↔ `48625d6d#2`（质心距 0.034）合并为全局槽 7、`64d7d56f` 头顶件与 `b51bdd59#0` 后脑发饰骨被刚性门控拆开（槽 18/19）；身体组 b20f90ea 38 新 + 13 共享等（对照 §2.5）；**同部件内部零合并；48625d6d 的 `#1/#8/#9`（同骨骼异帧）保持独立不被误并；跨组部件的骨骼分占各组槽位，运行时各自组内直拷（无校准）**。
3. 幂等：二次导入不重复反查（跳过并提示）；`force=True` 可重建。
4. **统一顶点组**：复选框开导入后，所有子网格对象共用同一套全局顶点组（组名 = 全局骨骼 id）；**同组部件可互刷权重（组内统一骨架）；跨组别引用被禁止——导出时 `_warn_cross_group_bone_references` 大声报警**；导入对象按 `SkeletonGroup` 归入对应 `SkeletonGroup_<N>` 合集。
5. **导出合并骨架（v9 出现次槽位口径）**：复选框开 + 有 VGMap 时，导出 vb2 的 BLENDINDICES = 全局骨骼 id；INI 含 Merged Skeleton 全套——`[Constants]` **只声明** `$zz_ms_occ_<i>` 与 `$zz_ms_seen_<i><k>`；每组每槽一份全宽 `ResourceZZMergedSkeleton_G<N>_s<k>`（array=266）；每 (部件, 槽) 一个直拷 attach CustomShader（`cs = ./res/zzmi_merged_skeleton_attach.hlsl`，无 cb1 引用）；deform 段**顶层无条件** `run` 全部 槽×部件 的 attach（**`run` 绝不在 if 内**）；到达标记 `$zz_ms_seen_<i><k>` 全部**顶层 sticky 累加**（if 体内赋值会被优化器静态折叠 → 守卫被删）；每槽绘制的门控：**自足挂点（直连路径、几何只采样自己槽位）= `if $zz_ms_occ_<i> == <k>` 直接绘制、不引用其它部件 seen**（v9.1），**重定向重放宿主 = 组内全部部件该槽的 seen 相与**，**合并宿主（join 成一个物体）= 宿主段捕获本轮 SO 引用 + 组内每个 Blend 布局兼容的挂点各发一条组级守卫重放**（v9.1，顺序无关），**两种体内都只有资源绑定与 `draw`/`so0 = null`**（无 `run`、无 `$变量` 赋值）；`[Present]` **只清零 occ/seen**（**不得含 `ResourceZZRedirectSO_* = null`**——F8 已回退，也不得含任何其它资源复位）。**INI 不得含任何 cb1 捕获段/捕获资源/校准着色器引用，不得在 [Present] 重放持久 palette attach，也不得出现 `$zz_ms_redirect_drawn_*` / `$zz_ms_group_ready_*` 闩锁变量或 `$zz_ms_group_phase_*` 相位变量**。
6. **游戏内实测**：导出的 mod 在 ZZMIv1 加载端下姿态正确，同组跨部件权重生效（每个 deform pass 的 vs-t0 使用当帧合并骨架；每槽守卫成立后重放该槽的合并可见几何，帧内无部件间不一致；**同一 IB 多实例（同帧多个 draw/多个对象变换）各自独立动画**：两个实例分别落在 s1/s2，不抖动、不卡首帧）；渲染 draw 无 R3 异常。
7. **多实例回归基线（v9 语义基准）**：生成的 ini 与手改版 `浮波柚叶.ini`（8571B，用户实测通过）语义一致——顶层 `run`（无 run 进 if）、`$zz_ms_seen_<i><k>` 顶层 sticky 累加、每槽守卫体内只有绑定与 draw、`[Constants]` 只声明 occ/seen、`[Present]` 只清零 occ/seen 且无任何资源复位、无闩锁/相位变量、骨架与 palette 按槽分份（`_s1`/`_s2`）、SO 捕获只由 SO owner 部件做；`tests/test_zzmi_merged_skeleton_ini.py` 全绿。
8. 复选框关闭：导入与导出行为均与现状完全一致（无 VGMap 读写、无 ModImpRuntime 写入、BLENDINDICES 走 `g.group` 原路径、无 Merged Skeleton 段）。
9. 单测全绿 + Blender headless e2e [PASS]。
