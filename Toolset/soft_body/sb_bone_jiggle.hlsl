// sb_bone_jiggle.hlsl — 软体物理 M1（骨骼抖动版 v2 / 2026-09-14 深夜）
//
// ── 为什么改骨骼而不是改顶点 ──────────────────────────────────────────────
// 顶点版：把偏移加在绑定姿态网格（vb0）上。实测（帧转储 + mod 自己的段结构）证明
// 那份网格随后会被顶点着色器按 vb2（骨骼索引+权重）重新蒙皮，所以统一偏移被**每根
// 骨头各自的矩阵**旋转/缩放 ⇒ 画面上永远"机械地歪/躺/散"，换轴换强度都没用。
// 骨骼版：偏移加在 palette 每根骨头的**平移分量**上 ⇒ 模型空间里方向一致，世界一致。
//
// ── 布局（三方一致核对过，不是猜的）──────────────────────────────────────
//   1) mod 自己的 `zzmi_merged_skeleton_attach.hlsl`：struct ZZBone3x4 { float4 r0,
//      r1, r2; } ⇒ **48 字节/骨头（4x3 矩阵）**
//   2) mod 的 ini：`[ResourceZZMergedSkeleton_G*] type = RWStructuredBuffer
//      stride = 48  array = 428`
//   3) 这帧转储：身体那一次绘制的 `vs-t0` = **20544 字节** = 428 × 48 ✓
//   平移在每行第 4 个分量：r0.w = tx、r1.w = ty、r2.w = tz。
//
// 数据流（照着 mod 自己那套"保存当帧 palette → 重建 → 换绑"的写法）：
//   ResourceSB_PalIn  = copy vs-t0        （当帧的合并骨架 palette）
//   cs-t24 = ref ResourceSB_PalIn         （只读）
//   cs-u5  = ref ResourceSB_PalOut        （抖动后的 palette）
//   cs-u6  = ref ResourceSB_BoneState     （每骨头跨帧状态）
//   vs-t0  = ResourceSB_PalOut            （换绑，绘制用它）

// ★ 2026-09-15 07:0x：改回**原始 float 读写**（唯一被证实能落地的写法，绕开自定义结构体的视图歧义）。
//   palette = 每骨头 12 个 float（3 条 float4 = 48 字节）；平移在每行的第 4 个分量（.w）。
//   输出声明成 stride = 16 的 float4 序列（float4 分量 = 同一批字节，共 428*3 条 = 与输入等长）。
Buffer<float> pal_in : register(t24);
RWStructuredBuffer<float4> pal_out : register(u5);
RWStructuredBuffer<float4> bone_state : register(u6);   // 每骨头 3 条：(off,1) (prev_off,1) (prev_pos,1)
Texture1D<float4> IniParams : register(t120);

// ⚠⚠ 本 fork 的 IniParams 整体偏移一行（ini 的 `xNN` 对应 `IniParams[NN+1]`，§13.1）。
//     这份着色器一开始漏了它 ⇒ 用户按 3/4 改的其实是别的槽（"重力/惯性"没按预期生效）。
#define SB_PARAM_ROW 1
#define SB_SHAPE_STIFFNESS  IniParams[80 + SB_PARAM_ROW].x
#define SB_DAMPING          IniParams[80 + SB_PARAM_ROW].z
#define SB_DT               IniParams[80 + SB_PARAM_ROW].w
#define SB_GRAVITY_STRENGTH IniParams[81 + SB_PARAM_ROW].w
#define SB_MARKER           IniParams[83 + SB_PARAM_ROW].x
#define SB_MARKER_DIST      IniParams[85 + SB_PARAM_ROW].x
#define SB_DIR_IDX          IniParams[86 + SB_PARAM_ROW].x
#define SB_INERTIA          IniParams[87 + SB_PARAM_ROW].x
#define SB_ADVANCE          IniParams[88 + SB_PARAM_ROW].x
#define SB_GRAVITY_SCALE    IniParams[89 + SB_PARAM_ROW].x
// 总开关（小键盘1）：置 1 ⇒ 直接返回、什么都不写 ⇒ 合并 palette 保持 mod 原样（软体关闭）
#define SB_OFF              IniParams[90 + SB_PARAM_ROW].x
// "脚下"参考高度（palette 的 x 轴 = 世界向下；实测身体那份 palette 的 x ∈ [0,1.0]，x 大 = 越靠脚）
#define SB_REF_HEIGHT       IniParams[93 + SB_PARAM_ROW].x
// 演示驱动（2026-09-15）：帧计数器 + 周期晃动幅度。
// 静态站着时重力只能给出静态下沉，**回弹/滞后必须靠"目标在动"**才能看见；
// 这个 mod 里没有拖拽节点，所以用一个正弦侧推来驱动它（= 有人轻轻推一下肉）。
//   力度沿 `(0,0,1)`（横向），频率约 0.5 Hz；幅度由 x95 给（默认 0 = 关）。
#define SB_FRAME            IniParams[94 + SB_PARAM_ROW].x
#define SB_SWAY             IniParams[95 + SB_PARAM_ROW].x

// 偏移上限（palette 的平移实测是**米**量纲：根骨 ~1.34、髋骨 ~0.8 —— 见 §14 的实测）。
// 0.12 ≈ 12 cm ≈ 体高 7%：够明显、不会穿模/飞走。留上限兜底。
#define SB_MAX_OFFSET       0.12

float3 sb_gravity_dir()
{
    int idx = (int)round(SB_DIR_IDX);
    // ⚠ 实测标定（2026-09-15，用户在游戏里逐个方向试出来的）：
    //   **palette/模型空间的 `(1,0,0)` 才是"世界向下"** ⇒ 排到 0 = 默认。
    //   （本 mod / 本角色的实测结果；换角色可能要重新标定。）
    if (idx <= 0) return float3(1.0, 0.0, 0.0);    // ★ 默认：世界向下
    if (idx == 1) return float3(-1.0, 0.0, 0.0);
    if (idx == 2) return float3(0.0, -1.0, 0.0);
    if (idx == 3) return float3(0.0, 1.0, 0.0);
    if (idx == 4) return float3(0.0, 0.0, -1.0);
    return float3(0.0, 0.0, 1.0);
}

[numthreads(128, 1, 1)]
void main(uint3 tid : SV_DispatchThreadID)
{
    uint bone = tid.x;

    // 总开关（小键盘1）：关掉时**什么都不写**。
    // 2026-09-15 05:4x 暂时停用：怀疑"小键盘1"留下 x90=1 会把一切都关掉（实测所有键无反应）。
    // if (SB_OFF >= 0.5) { return; }

    // ★ 2026-09-15 06:4x 最小判定：**只改 0 号骨头**（位移由诊断标记给）。
    //   目的：把"写入是否落地"（已证明 ✓）与"改的分量对不对"分开看：
    //     只动一根骨头 ⇒ 只有那一小片顶点动 = 分量位置对；
    //     整块塌/拉丝       = 分量位置错（改到了旋转分量）。
    if (bone != 0)
    {
        return;
    }

    uint bone_words = 0;
    pal_in.GetDimensions(bone_words);                // ★ Buffer<float> 只能带一个参数（踩过两次）
    uint bone_count = bone_words / 12;
    if (bone >= bone_count)
    {
        return;
    }
    uint bf = bone * 12;                                  // 本骨头 12 个 float
    float3 pos = float3(pal_in[bf + 3], pal_in[bf + 7], pal_in[bf + 11]);   // 平移（每行 .w）

    // ★ 跳过"空骨头"（2026-09-15）：合并 palette 里只有被 attach 填过的骨头才有有效矩阵，
    //   其余是全零。给全零矩阵加偏移 ⇒ 仍然退化 ⇒ 被它带动的顶点**塌成一个点**
    //   （用户实测："臀部局部塌到一个中点"）。这里直接跳过它们。
    if (length(float3(pal_in[bf + 0], pal_in[bf + 1], pal_in[bf + 2])) < 1e-4 &&
        length(float3(pal_in[bf + 4], pal_in[bf + 5], pal_in[bf + 6])) < 1e-4 &&
        length(float3(pal_in[bf + 8], pal_in[bf + 9], pal_in[bf + 10])) < 1e-4)
    {
        return;
    }

    uint st = bone * 3;
    float4 cur_slot = bone_state[st + 0];
    float4 prev_slot = bone_state[st + 1];
    float4 prev_pos_slot = bone_state[st + 2];

    float3 off = float3(0.0, 0.0, 0.0);
    float3 prev_off = float3(0.0, 0.0, 0.0);
    if ((cur_slot.w == 1.0) && (prev_slot.w == 1.0) &&
        !any(isnan(cur_slot.xyz)) && !any(isinf(cur_slot.xyz)) &&
        !any(isnan(prev_slot.xyz)) && !any(isinf(prev_slot.xyz)) &&
        // ⚠ 有效性判据必须用**宽松**界限：偏移被夹到上限后 length 恰好 ≈ 上限，
        //   原来的 `<= SB_MAX_OFFSET` 浮点比较会把"已夹到上限"的状态判成无效 ⇒
        //   每帧重置 ⇒ 只剩第一步的几毫米 ⇒ 实测"几乎没变化 + 小键盘1 无感"。
        (length(cur_slot.xyz) <= SB_MAX_OFFSET * 4.0) &&
        (length(prev_slot.xyz) <= SB_MAX_OFFSET * 4.0))
    {
        off = cur_slot.xyz;
        prev_off = prev_slot.xyz;
    }
    float3 prev_pos = pos;
    if ((prev_pos_slot.w == 1.0) && !any(isnan(prev_pos_slot.xyz)) &&
        !any(isinf(prev_pos_slot.xyz)))
    {
        prev_pos = prev_pos_slot.xyz;
    }

    // 2026-09-15 关键修正：**物理每帧只推进一次（第一个 pass），但每次调用都要写 palette**
    // —— 因为 mod 的 deform 段每个 pass 都会重建合并 palette，只写一次会被后面的 pass 覆盖。
    if (SB_ADVANCE > 0.5)
    {
        float dt = max(SB_DT, 1e-4);
        float3 next_off = off + (off - prev_off) * SB_DAMPING;   // 惯性
        next_off -= (pos - prev_pos) * SB_INERTIA;               // 骨头自己动了 → 肉滞后
        float gscale = SB_GRAVITY_SCALE;
        if (gscale <= 0.0)
        {
            gscale = 1.0;
        }
        next_off += sb_gravity_dir() * SB_GRAVITY_STRENGTH * gscale * dt * dt;
        // 演示驱动：横向正弦推力（力度与重力同源，量纲一致 ⇒ 直接相加）
        float sway = SB_SWAY;
        if (sway != 0.0)
        {
            next_off += float3(0.0, 0.0, 1.0) * sin(SB_FRAME * 0.05) * sway * dt * dt;
        }
        // ★ 刚度按"高度"渐变（2026-09-15）：脚下（x 大）硬、越往上越松。
        //   给所有骨头同一个刚度 ⇒ 整块刚性下沉（用户实测原话："像一块石头垂下来"）；
        //   渐变之后才有"肉挂下来"的样子，并且因为各骨头相位不同还会出现回弹/摆动。
        float ref_h = max(SB_REF_HEIGHT, 1e-3);
        float h = saturate(pos.x / ref_h);                       // 0 = 最高，1 = 脚下
        float k = SB_SHAPE_STIFFNESS * (0.15 + 0.85 * h);
        next_off -= off * k;                                     // 回弹：越靠脚拉回越强

        float off_len = length(next_off);
        if (off_len > SB_MAX_OFFSET)
        {
            next_off *= SB_MAX_OFFSET / off_len;
        }

        bone_state[st + 0] = float4(next_off, 1.0);
        bone_state[st + 1] = float4(off, 1.0);
        bone_state[st + 2] = float4(pos, 1.0);
        off = next_off;
    }

    float3 shift = (SB_MARKER > 0.5)
        ? float3(0.0, 0.0, SB_MARKER_DIST)     // 诊断标记：所有骨头一起平移
        : off;

    uint o = bone * 3;
    pal_out[o + 0] = float4(pal_in[bf + 0], pal_in[bf + 1], pal_in[bf + 2], pal_in[bf + 3] + shift.x);
    pal_out[o + 1] = float4(pal_in[bf + 4], pal_in[bf + 5], pal_in[bf + 6], pal_in[bf + 7] + shift.y);
    pal_out[o + 2] = float4(pal_in[bf + 8], pal_in[bf + 9], pal_in[bf + 10], pal_in[bf + 11] + shift.z);
}
