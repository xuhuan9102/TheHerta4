// sb_invskin.hlsl — 软体物理 M1（逆蒙皮版 / 2026-09-15）
//
// ── 为什么是这一版 ───────────────────────────────────────────────────────
// 实测链条（帧转储 + 日志，逐条核对过）：
//   1) 可见身体的顶点来自 **ZZMI/游戏的蒙皮 CS 输出**（vb3 = f201bd10，被画出来）；
//   2) 那个 CS 吃的是**游戏自己的 palette**，不是 mod 的合并 palette
//      —— 所以"改 mod 的 palette"对身体永远无效（实测只有跨部件小件会动）；
//   3) mod 把游戏 palette **复制**到了 `ResourceZZPalette_<ib>_s1`
//      （135 骨头 × 48 字节，我们能读）；Blend 是 3 影响（权重 3 float + 索引 3 uint）。
//
// 于是正确做法：**在绑定姿态里做逆蒙皮**
//     想要的世界/蒙皮后偏移 V
//     顶点绑定姿态位置 p → 写入 p + (Σ wᵢ·Mᵢ)⁻¹ · V
//   这样蒙皮之后每个顶点都恰好偏移 V：**整块朝同一方向位移**，不会被逐骨矩阵拧歪。
//   （之前"顶点版"直接加 V 到绑定姿态，才会出现"机械地歪/躺/散"。）
//
// 数据流：
//   ResourceSB_PalCopy   = copy ResourceZZPalette_c209c22b_s1   （当帧游戏 palette 的副本）
//   ResourceSB_BlendCopy = copy vb2                              （骨骼权重/索引）
//   ResourceSB_VB        = vb0（别名）→ cs-u5 = copy 它           （绑定姿态顶点）
//   cs-t24 = palette, cs-t25 = blend, cs-u6 = 状态
//   写回：ResourceSB_VB = copy cs-u5

struct VertexAttributes
{
    float3 pos;
    float3 nrm;
    float4 tan;
};

// 两份合并 palette：s1 / s2（同一个部件一帧跑两次，各用一套）。
// ⚠ 不能靠 ini 里的 if/else 去 copy 其中一份 —— 这个 fork 的资源复制**不进 if**
//   （踩过：copy 落在 if 里 ⇒ 副本为空 ⇒ 反解退化 ⇒ 不碎也不动）。所以两份都拷，
//   由 SB_OCC2 在这里选。
Buffer<float> palA : register(t24);      // 428 骨头 × 12 float（4x3；平移在每行 .w）
Buffer<float> palB : register(t25);
Buffer<uint> blend : register(t26);      // 每顶点 8 个 uint32：w0,w1,w2,pad, i0,i1,i2,pad
RWStructuredBuffer<VertexAttributes> vb_out : register(u5);
RWStructuredBuffer<float4> st : register(u6);   // [0] 当前偏移 [1] 上一帧偏移 [2] 上一帧骨头平均位置
Texture1D<float4> IniParams : register(t120);

// ⚠⚠ 本 fork（3Dmigoto-Armor）的 IniParams **整体偏移一行**：mod 自己的
//   zzmi_merged_skeleton_attach.hlsl 里写着"实测 y1 在 IniParams[1].y，
//   标准版是 IniParams[0]，本 fork 布局从 [1] 起"。
//   ⇒ ini 里的 `xNN` 必须读 `IniParams[NN+1].x`。这一行之差让所有参数错位
//     （诊断标记读不到 ⇒ 算出的偏移恒为 0，实测自报 vid0.tan.z = 1000.000 抓出来的）。
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
#define SB_BONE_COUNT       IniParams[91 + SB_PARAM_ROW].x
#define SB_OCC2             IniParams[92 + SB_PARAM_ROW].x

// 世界空间（=蒙皮之后）的偏移上限，米。身体高约 1.6 m ⇒ 0.30 已经很明显。
#define SB_MAX_OFFSET       0.30

// 取 palette 的某个 float（按出现次在两份之间选）
float sb_pal(uint idx, float sel)
{
    return lerp(palA[idx], palB[idx], sel);
}

float3 sb_gravity_dir()
{
    int idx = (int)round(SB_DIR_IDX);
    if (idx <= 0) return float3(0.0, -1.0, 0.0);
    if (idx == 1) return float3(0.0, 1.0, 0.0);
    if (idx == 2) return float3(0.0, 0.0, -1.0);
    if (idx == 3) return float3(0.0, 0.0, 1.0);
    if (idx == 4) return float3(-1.0, 0.0, 0.0);
    return float3(1.0, 0.0, 0.0);
}

// 3x3 逆矩阵（adjugate / det）。HLSL 没有内建 inverse。
float3x3 sb_inverse3(float3x3 m, out bool ok)
{
    float3 a = m[0];
    float3 b = m[1];
    float3 c = m[2];
    float3 r0 = float3(b.y * c.z - b.z * c.y, a.z * c.y - a.y * c.z, a.y * b.z - a.z * b.y);
    float3 r1 = float3(b.z * c.x - b.x * c.z, a.x * c.z - a.z * c.x, a.z * b.x - a.x * b.z);
    float3 r2 = float3(b.x * c.y - b.y * c.x, a.y * c.x - a.x * c.y, a.x * b.y - a.y * b.x);
    float det = a.x * r0.x + a.y * r1.x + a.z * r2.x;
    ok = abs(det) > 1e-8;
    return ok ? float3x3(r0 / det, r1 / det, r2 / det) : float3x3(1, 0, 0, 0, 1, 0, 0, 0, 1);
}

[numthreads(64, 1, 1)]
void main(uint3 tid : SV_DispatchThreadID)
{
    uint vid = tid.x;
    uint count = 0;
    uint stride = 0;
    vb_out.GetDimensions(count, stride);
    if (vid >= count)
    {
        return;
    }

    // ── 1) 全局偏移 V：所有线程算出同一个值；物理每帧只在第一个 pass 推进一次 ──
    float4 s0 = st[0];
    float4 s1 = st[1];
    float4 s2 = st[2];
    float3 off = float3(0.0, 0.0, 0.0);
    float3 prev_off = float3(0.0, 0.0, 0.0);
    float3 prev_avg = float3(0.0, 0.0, 0.0);
    if ((s0.w == 1.0) && (s1.w == 1.0) &&
        !any(isnan(s0.xyz)) && !any(isinf(s0.xyz)) &&
        (length(s0.xyz) <= SB_MAX_OFFSET))
    {
        off = s0.xyz;
        prev_off = s1.xyz;
    }
    if ((s2.w == 1.0) && !any(isnan(s2.xyz)))
    {
        prev_avg = s2.xyz;
    }

    // 骨头平均位置（每 8 根采样一次）≈ 身体在世界里的位置
    float sel = (SB_OCC2 > 0.5) ? 1.0 : 0.0;
    uint bones = (uint)max(SB_BONE_COUNT, 1.0);
    float3 avg = float3(0.0, 0.0, 0.0);
    uint n = 0;
    for (uint b = 0; b < bones; b += 8)
    {
        avg += float3(sb_pal(b * 12 + 3, sel), sb_pal(b * 12 + 7, sel),
                      sb_pal(b * 12 + 11, sel));
        n++;
    }
    avg /= max((float)n, 1.0);

    float3 V;
    if (SB_MARKER > 0.5)
    {
        V = float3(0.0, 0.0, SB_MARKER_DIST);      // 诊断：整块朝固定方向平移
    }
    else if (SB_ADVANCE > 0.5)
    {
        float dt = max(SB_DT, 1e-4);
        float3 next = off + (off - prev_off) * SB_DAMPING;     // 惯性
        next -= (avg - prev_avg) * SB_INERTIA;                 // 身体自己动了 → 肉滞后
        float gscale = SB_GRAVITY_SCALE;
        if (gscale <= 0.0)
        {
            gscale = 1.0;
        }
        next += sb_gravity_dir() * SB_GRAVITY_STRENGTH * gscale * dt * dt;
        next -= off * SB_SHAPE_STIFFNESS;                      // 回弹
        float L = length(next);
        if (L > SB_MAX_OFFSET)
        {
            next *= SB_MAX_OFFSET / L;
        }
        if (vid == 0)
        {
            st[0] = float4(next, 1.0);
            st[1] = float4(off, 1.0);
            st[2] = float4(avg, 1.0);
        }
        V = next;
    }
    else
    {
        V = off;
    }

    // ── 2) 逐顶点：把世界偏移反解回绑定姿态 ──
    uint base_i = vid * 8;
    float w0 = asfloat(blend[base_i + 0]);
    float w1 = asfloat(blend[base_i + 1]);
    float w2 = asfloat(blend[base_i + 2]);
    uint i0 = blend[base_i + 4] * 12;
    uint i1 = blend[base_i + 5] * 12;
    uint i2 = blend[base_i + 6] * 12;
    float ws = w0 + w1 + w2;
    if (ws < 1e-5)
    {
        w0 = 1.0; w1 = 0.0; w2 = 0.0;
    }
    else
    {
        w0 /= ws; w1 /= ws; w2 /= ws;
    }

    float3 m0 = w0 * float3(sb_pal(i0 + 0, sel), sb_pal(i0 + 1, sel), sb_pal(i0 + 2, sel))
              + w1 * float3(sb_pal(i1 + 0, sel), sb_pal(i1 + 1, sel), sb_pal(i1 + 2, sel))
              + w2 * float3(sb_pal(i2 + 0, sel), sb_pal(i2 + 1, sel), sb_pal(i2 + 2, sel));
    float3 m1 = w0 * float3(sb_pal(i0 + 4, sel), sb_pal(i0 + 5, sel), sb_pal(i0 + 6, sel))
              + w1 * float3(sb_pal(i1 + 4, sel), sb_pal(i1 + 5, sel), sb_pal(i1 + 6, sel))
              + w2 * float3(sb_pal(i2 + 4, sel), sb_pal(i2 + 5, sel), sb_pal(i2 + 6, sel));
    float3 m2 = w0 * float3(sb_pal(i0 + 8, sel), sb_pal(i0 + 9, sel), sb_pal(i0 + 10, sel))
              + w1 * float3(sb_pal(i1 + 8, sel), sb_pal(i1 + 9, sel), sb_pal(i1 + 10, sel))
              + w2 * float3(sb_pal(i2 + 8, sel), sb_pal(i2 + 9, sel), sb_pal(i2 + 10, sel));

    bool ok;
    float3x3 invA = sb_inverse3(float3x3(m0, m1, m2), ok);
    // 行向量约定：p' = p·A + t ⇒ 要 p' 多出 V，就需要 d·A = V ⇒ d = V·A⁻¹
    float3 d = mul(V, invA);
    if (!ok || any(isnan(d)) || any(isinf(d)) || length(d) > SB_MAX_OFFSET * 4.0)
    {
        d = float3(0.0, 0.0, 0.0);      // 退化矩阵：宁可不偏移，也不要把网格撕开
    }

    VertexAttributes v = vb_out[vid];
    v.pos += d;
    // 自报（2026-09-15）：把"这次到底跑没跑、算出的偏移多大"编码进 vid=0 的 tangent.z。
    //   读法：tangent.z = 1000 + |d|×1000（单位米）。1000 表示跑了但偏移为 0；
    //   如果是原始值（≈0/±1）说明这个 CS 根本没执行到写回。
    if (vid == 0)
    {
        v.tan.z = 1000.0 + length(d) * 1000.0;
    }
    // 第二个自报（vid==1）：palette 第 0 根骨头的平移模长 ×10 —— 用来区分
    // "palette 是空的（=0）" 还是 "palette 有数据但反解失败（>0）"。
    if (vid == 1)
    {
        float3 p0 = float3(sb_pal(3, sel), sb_pal(7, sel), sb_pal(11, sel));
        v.tan.z = 2000.0 + length(p0) * 10.0;
    }
    vb_out[vid] = v;
}
