// rzm_jiggle_interaction.hlsl
// RZMenu / 3DMigoto / XXMI
// Made by: Rayvich
// Polished single-buffer Null-spring jiggle.
// Adapted from jiggle_physx_by_rayvich_ouroboros_targettrail_v05.hlsl
// by Rayvich for RZMenu ecosystem (per-component auto-generation).
//
// - One persistent Ouroboros RWBuffer<float4> at u6.
// - Optional per-vertex seam trace at u4; never feeds the draw path.
// - Detector snapshot is used only to create a grab anchor (center + object ID).
// - During drag, cursor delta maps to a SCREEN-STABLE vb0 basis (+X/+Y),
//   NOT the hit face normal. Face-normal tangent frames flip between nearby
//   triangles on curved surfaces (breast), so the same mouse "up" used to
//   produce left/down/up depending on which face was grabbed.
// - Live raycast is intentionally NOT used (stable outside silhouette).
// - Release uses Verlet-style inertia with a small one-frame release kick.
// - Per-vertex MaskBuffer (t70): 0 = frozen, 1 = free. Export must default 0.
//
// Bindings:
//   t24 = original vb0, read-only, stride 40
//   t67 = captured detector snapshot
//   u5  = copied vb0 output, RWStructuredBuffer<VertexAttributes>, stride 40
//   u6  = persistent jiggle state, RWBuffer<float4>, 9 entries minimum
//   u4  = optional per-vertex diagnostic output, 3 float4 records per vertex
//   t120 / IniParams[26] = same transform profile as detector
//
// IniParams:
//   [67].x = captured cursor X, pixels
//   [67].y = captured cursor Y, pixels
//   [67].w = capture active flag, 1 while mouse is held
//
//   [68].x = radius in vb0 local units, fallback 0.25
//   [68].y = drag strength, fallback 1.00
//   [68].z = falloff power, fallback 1.50
//   [68].w = screen drag scale, fallback 1.00
//
//   [69].x = current cursor X, pixels
//   [69].y = current cursor Y, pixels
//   [69].z = screen width
//   [69].w = screen height
//
//   [70].x = grab velocity damping, fallback 0.86
//   [70].y = grab spring, fallback 0.176       // ~20% slower than v03
//   [70].z = release velocity damping, fallback 0.96
//   [70].w = release spring, fallback 0.055     // ~20% slower than v03
//
//   [71].x = max offset clamp, fallback radius * 2.0
//   [71].y = one-frame release kick, fallback 1.10
//   [71].z = mouse Y direction, fallback +1.0
//            +1.0 = inverted relative to v03, -1.0 = old direction
//   [71].w = target smoothing/follow, fallback 0.12
//            lower = longer rubber delay, higher = snappier
//
//   [101]/[102] = per-zone falloff power override (zones 0-3 / 4-7), 0 = no
//                 override (use JIGGLE_PARAMS.z). Same LOW/HIGH packing as
//                 ZONE_RADIUS/ZONE_STRENGTH/ZONE_OFFSET below.
//
// History layout u6:
//   [0].xyz = current physical offset, vb0 local space
//   [0].w   = state alive flag
//   [1].xyz = previous physical offset, vb0 local space
//   [1].w   = reserved
//   [2].xyz = frozen grab center, vb0 local space
//   [2].w   = captured object ID
//   [3].xyz = frozen grab normal, vb0 local space
//   [3].w   = previous-frame mouse-held flag
//   [4].xyz = filtered/smoothed drag target, vb0 local space
//   [4].w   = reserved
//   [5].xyz = previous filtered target, vb0 local space
//   [5].w   = reserved
//   [6].xyz = frozen screen-right axis, vb0 local space
//   [6].w   = axis-valid flag
//   [7].xyz = frozen screen-down axis, vb0 local space
//   [7].w   = axis-valid flag
//   [8].x   = previous-frame mouse-held flag

struct VertexAttributes {
    float3 position;
    float3 normal;
    float4 tangent;
};

StructuredBuffer<VertexAttributes> base_buffer : register(t24);
StructuredBuffer<float4> CapturedDetect        : register(t67);
Buffer<float4> ObjParams                       : register(t68);
RWStructuredBuffer<VertexAttributes> rw_buffer  : register(u5);
RWBuffer<float4> JiggleState                    : register(u6);
Buffer<uint4> JiggleZoneIDs                    : register(t65);
Buffer<float4> JiggleZoneWeights               : register(t66);
Buffer<float4> SharedInteractionState           : register(t71);
// Path Slide: static per-zone start->end vectors (core/path_export.py) and
// live per-zone progress (written by rzm_jiggle_screen_state.hlsl).
Buffer<float4> PathVectors                      : register(t73);
Buffer<float> PathProgressState                 : register(t74);
Buffer<float4> ZoneParams                       : register(t75);
// Collision (anti-clipping): baked at export from the collider's vb0 subset.
//   t76 = interleaved point cloud (position float4 + unit normal float4 per point)
//   t79 = per-vertex L0 contact data (q0.xyz,t0)(n0.xyz,valid)
Buffer<float4> ColliderPoints                    : register(t76);
Buffer<float4> ColliderVertexL0                 : register(t79);
// t83 = 碰撞体三角形的均匀网格（R32_UINT CSR）：
//       [0..2]=dims.xyz [3]=tri_count [4]=cell_count [5]=idx_count
//       [6]=max_per_cell [7]=保留
//       [8 .. 8+cell_count]=每格在索引表中的起始
//       其后=三角形索引列表
// t84 = 网格里的三角形本体：每三角形 4 个 float4（3 个顶点 xyz + 定向面法线）
Buffer<uint>   ColliderTriGrid                   : register(t83);
Buffer<float4> ColliderTriGridTris               : register(t84);
// ── B1（t6 修法）：碰撞三角形顶点实时蒙皮 ──────────────────────────────────
// 数据源复用模组**已有**的 Blend 视图：ini 已绑 cs-t86 = ResourceDragJiggleBlendTyped_d5e1e7ff_A
// （type = Buffer / format = R32G32B32A32_FLOAT / filename = Meshes/d5e1e7ff-Blend.buf
//  ⇒ 19039×32B/16B = 38078 个 float4；第 gi 顶点的 float4#0 = BLENDWEIGHTS、float4#1 = BLENDINDICES）。
// ⇒ 无需新增缓冲、无需重新导出：补丁只在 shader 侧补一条只读声明并按下标读取。
// 索引语义（本任务自测，可复现：skin_b1_check.json）：2643 个角点指向 519 个全局顶点（值域 18520..19038）；
//  这些顶点的 BLENDINDICES 取值 ∈ [87,155]（15 个不同槽）、权重和 = 1.0±1.2e-7、无 0xFFFFFFFF、
//  零权重顶点 0 个 ⇒ 与 zz_vgmap（133 项）不是同一空间，**不过 vg_map**，直接是合并骨架槽号
//  （≤155 < 212 槽）⇒ 取矩阵用 slot*3。
Buffer<float4> BlendTyped                       : register(t86);
Buffer<uint>   ColliderTriGridVerts             : register(t87);
// 合并骨架池（ResourceZZMergedSkeleton_G3_s1）：每骨骼 3 个 float4，下标 = bone*3 + m。
// 【双缓冲 B2 · 已修】合并骨架是 s1/s2 两帧 ping-pong：本帧 deform 段按 $zz_ms_occ_0 选一份写入
//  （洛克茜.ini:374-396），而拖拽 CS 在渲染 draw 上执行 ⇒ 固定绑 s1 时，occ==2 的帧会读到**上一帧**骨架。
//  verifier 实测灵敏度 ≈ −1.5 mm/度：滞后 0.35° 就耗尽零滞后余量（+0.13~+0.37 mm），
//  滞后 3° ⇒ 197~202 行穿模（与静态面同级）。⇒ ini 侧改为按 $zz_ms_occ_0 条件绑定（见本补丁 ini hunk）。
Buffer<float4> ColliderSkinSkeleton             : register(t88);

// 【前向声明 A1】ReadColliderTri 的定义在下方（T16 锦点路径在定义之后才调用）；保留声明以防顺序调整后 fxc 报 undeclared identifier。
bool CollisionSkinActive();
float3 SkinColliderVertex(uint i);
void ReadColliderTri(uint tri, out float3 ta, out float3 tb, out float3 tc, out float3 nOut);

Texture1D<float4> IniParams : register(t120);

#define CAPTURED_CURSOR      IniParams[67]
#define JIGGLE_PARAMS        IniParams[68]
#define CURRENT_CURSOR       IniParams[69]
#define PHYS_PARAMS          IniParams[70]
#define POLISH_PARAMS        IniParams[71]
#define JIGGLE_MULTIPLIERS   IniParams[72]
#define JIGGLE_MULT_EXTRA    IniParams[73]
#define TIME_PARAMS          IniParams[76]
#define ZONE_RUNTIME_META    IniParams[119]
// 【远侧方向性约束】碰撞体在 vb0 local 绑定空间的形状（导出侧 PCA 烘焙）：
//   [112].xyz = 中心，[113].xyz = 轴向单位向量，[113].w = 轴向最小值，
//   [114].x   = 轴向最大值，[114].y = 最大径向半径，[114].z = 有效位（1=可用）。
// 只在碰撞开启（x101 > 0.5 且 x104 >= 2）时由 ini 发射；碰撞关时不写，shader 也不读。
#define COLLIDER_SHAPE_CENTER IniParams[112]
#define COLLIDER_SHAPE_AXIS   IniParams[113]
#define COLLIDER_SHAPE_RANGE  IniParams[114]
// Collision params (anti-clipping). Set by the jiggle INI section; zero = disabled.
//   [101]: x=enabled, y=margin, z=mode (0=soft-slide, 1=hard-clamp), w=safety
//   [104]: x=drag mode variable
#define COLLISION_PARAMS    IniParams[101]
#define COLLISION_META      IniParams[104]
// The final surface pass constrains complete triangles from the current VB0.
#define COLLISION_SURFACE   IniParams[115]
// 动态三角形网格（当前首选路径）：运行时按顶点当前位置现查邻域三角形，约束 =
// 「到碰撞体表面的带符号距离不许比绑定姿态时更小」。导出侧把碰撞体三角形装进
// 均匀网格（CSR），shader 侧查 27 邻域后对候选三角形算精确点-三角形距离。
//   [108]: xyz=网格 origin(bmin), w=cell size
//   [109]: xyz=网格 dims(nx,ny,nz), w=cell_count
// 为什么必须动态查：绑定姿态下最近的 K 个三角形只覆盖顶点附近一小片曲面。
// 实测位移 0.25 时顶点早已离开那 K 个，静态约束完全失效 —— 真实数据 235 顶点
// × 12 方向 × 4 长度 = 11280 案例：静态 K=8 穿模 693、最大穿深 0.175（顶点
// v22697 沿 [0.71,0,0.71] 拉 0.25 被完全放行，实际陷进大腿 0.175）。
#define COLLISION_TRIGRID       IniParams[108]
#define COLLISION_TRIGRID_DIMS  IniParams[109]

// ===== T16 sock slide / weight projection (safe increment) =====================
// Three gates (no new ABI dims): master IniParams[119].x / zone bit
// ZoneParams[zone*2+1].z / direction bit PathVectors[zone].w.  When any is 0 this
// whole block is inert => the off state stays byte-identical.
// Weight channel: read the Blend typed view (t86, 32 B/vertex = 2xfloat4), write
// BlendOut (u2, structured stride 32) which the ini re-binds as vb2 for deform.
struct SockBlendRec { float4 w; uint4 i; };
RWStructuredBuffer<SockBlendRec> SockBlendOut    : register(u2);
#define SOCK_STROKE      0.2520     // t15 §5: effective stroke (beyond it the sock leaves the collider patch)
#define SOCK_ROW_END     1099u      // write segment: sock rows [0,1099) (from the IB)
#define SOCK_CLAMP_DW    0.05       // per-slot clamp (t15 section 4)
#define SOCK_LAM_EPS     1e-6

bool ZoneIsPathSlide(uint zone);        // forward decl (defined below, near hlsl:359)

bool SockSlideEnabled(uint zone)
{
    if (ZONE_RUNTIME_META.x <= 0.5)      return false;   // master gate
    if (!ZoneIsPathSlide(zone))          return false;   // zone bit
    if (PathVectors[zone].w <= 0.5)      return false;   // direction bit
    return true;
}

// T41: a zero |t0| row carries no anchor data at all (l0q.xyz = 0, l0n = 0, l0.w = 0).
// Such rows must never enter the slide / weight-projection paths: the anchor search
// would otherwise land on a triangle near the object origin (row 179: 0.17~0.33 m jump).
bool SockHasAnchor(uint vid)
{
    return abs(ColliderVertexL0[vid * 2u + 0u].w) > 0.0;
}

// Closest point + barycentrics on one triangle (edge/face candidates clamped, min wins;
// identical formulation to the CPU port .dbg/rzm10/verify_sock.py::closest_bary).
void SockClosestOnTri(float3 p, float3 a, float3 b, float3 c, out float3 q, out float3 lam)
{
    float3 ab = b - a, ac = c - a, ap = p - a;
    float d1 = dot(ab, ap), d2 = dot(ac, ap);
    float3 bp = p - b;
    float d3 = dot(ab, bp), d4 = dot(ac, bp);
    float3 cp = p - c;
    float d5 = dot(ab, cp), d6 = dot(ac, cp);
    float ab2 = dot(ab, ab), ac2 = dot(ac, ac), abac = dot(ab, ac);
    float den = ab2 * ac2 - abac * abac;
    den = abs(den) < 1e-30 ? 1e-30 : den;
    float v = saturate((d1 * ac2 - d2 * abac) / den);
    float w = saturate((d2 * ab2 - d1 * abac) / den);
    float s = v + w;
    if (s > 1.0) { v /= s; w /= s; }
    float u = saturate(1.0 - v - w);
    float tab = saturate(d1 / (abs(d1 - d3) < 1e-30 ? 1e-30 : (d1 - d3)));
    float tac = saturate(d2 / (abs(d2 - d6) < 1e-30 ? 1e-30 : (d2 - d6)));
    float dbc = (d4 - d3) + (d5 - d6);
    float tbc = saturate((d4 - d3) / (abs(dbc) < 1e-30 ? 1e-30 : dbc));
    float3 qab = a + ab * tab, qac = a + ac * tac, qbc = b + (c - b) * tbc;
    float3 qf = a + ab * v + ac * w;
    float dab = length(p - qab), dac = length(p - qac), dbcv = length(p - qbc), df = length(p - qf);
    q = qf; lam = float3(u, v, w);
    float best = df;
    if (dab < best) { best = dab; q = qab; lam = float3(1.0 - tab, tab, 0.0); }
    if (dac < best) { best = dac; q = qac; lam = float3(1.0 - tac, 0.0, tac); }
    if (dbcv < best) { best = dbcv; q = qbc; lam = float3(0.0, 1.0 - tbc, tbc); }
}

// t39：绑定姿态顶点位置（只读 SRV，stride 40 与 vb0 同构）。
// ini 侧最小改动：抖动 CS 段（[CustomShaderDragJiggled5e1e7ff_A] 与 5 个
// [CustomShaderDragStage0..4_d5e1e7ff_A]）各加一行 cs-t90 = Resourced5e1e7ffPosition。
StructuredBuffer<VertexAttributes> BindPosBuf : register(t90);
// Anchor: q0 (bind-pose surface contact point) -> nearest collider triangle.
// Increment 1 scans all 881 triangles; after t22 that buffer is the runtime-skinned
// collider surface, so the anchor follows the thigh pose automatically.
bool SockFindAnchor(uint vid, float3 pLive, out uint tri, out float3 lam)
{
    tri = 0u; lam = float3(0.0, 0.0, 0.0);
    if (!SockHasAnchor(vid)) return false;              // T41: q0 = 0 row -> no anchor
    // t39：搬运式锚点 —— 查询点 = 绑定接触点 q0 + 该顶点自身的位移 (localPos - bindPos)。
    // 位移为 0 时逐字回落到 q0（bind 静止态零回归）；姿态变化时锚点随顶点搬走（实时判定）。
    // 空行（L0.valid=0）没有锚点 ⇒ 返回 false，调用方冻结该行。
    if (ColliderVertexL0[vid * 2u + 1u].w < 0.5) return false;
    float3 p = ColliderVertexL0[vid * 2u + 0u].xyz + (pLive - BindPosBuf[vid].position);
    uint triElems;
    ColliderTriGridTris.GetDimensions(triElems);
    uint triCount = triElems / 4u;
    float best = 1e30;
    for (uint t = 0u; t < triCount; ++t)
    {
        uint vb = t * 4u;
        float3 a = ColliderTriGridTris[vb + 0u].xyz;
        float3 b = ColliderTriGridTris[vb + 1u].xyz;
        float3 c = ColliderTriGridTris[vb + 2u].xyz;
        float3 q, l;
        SockClosestOnTri(p, a, b, c, q, l);
        float d = length(p - q);
        if (d < best) { best = d; tri = t; lam = l; }
    }
    return best < 1e29;
}

// top-4 merge (weight descending, equal weight -> smaller slot first)
void SockAccumulate(inout float wTop[4], inout uint iTop[4], float w, uint idx)
{
    if (w <= 0.0) return;
    for (uint k = 0u; k < 4u; ++k)
    {
        if (iTop[k] == idx) { wTop[k] += w; return; }
    }
    for (uint k2 = 0u; k2 < 4u; ++k2)
    {
        if (wTop[k2] <= 0.0 || w > wTop[k2] || (w == wTop[k2] && idx < iTop[k2]))
        {
            for (uint m = 3u; m > k2; --m) { wTop[m] = wTop[m - 1u]; iTop[m] = iTop[m - 1u]; }
            wTop[k2] = w; iTop[k2] = idx;
            return;
        }
    }
}

void SockProjectWeights(uint vid, uint tri, float3 lam)
{
    float wTop[4] = { 0.0, 0.0, 0.0, 0.0 };
    uint iTop[4] = { 0u, 0u, 0u, 0u };
    for (uint c = 0u; c < 3u; ++c)
    {
        if (lam[c] <= SOCK_LAM_EPS) continue;
        uint row = ColliderTriGridVerts[tri * 3u + c];
        if (row == 0xFFFFFFFFu) continue;
        float4 bw = BlendTyped[row * 2u + 0u];
        uint4 bi = asuint(BlendTyped[row * 2u + 1u]);
        SockAccumulate(wTop, iTop, lam[c] * bw.x, bi.x);
        SockAccumulate(wTop, iTop, lam[c] * bw.y, bi.y);
        SockAccumulate(wTop, iTop, lam[c] * bw.z, bi.z);
        SockAccumulate(wTop, iTop, lam[c] * bw.w, bi.w);
    }
    float tot = wTop[0] + wTop[1] + wTop[2] + wTop[3];
    if (tot <= 1e-6)
    {
        return;                                  // degenerate: keep previous (freeze)
    }
    // R3（t31）：基准 = 上一帧写出的 BlendOut（u2）
    float r3Sum = SockBlendOut[vid].w.x + SockBlendOut[vid].w.y + SockBlendOut[vid].w.z + SockBlendOut[vid].w.w;
    float4 pw = r3Sum > 1e-3 ? SockBlendOut[vid].w : BlendTyped[vid * 2u + 0u];
    uint4  pi = r3Sum > 1e-3 ? SockBlendOut[vid].i : asuint(BlendTyped[vid * 2u + 1u]);
    float4 nw = float4(0.0, 0.0, 0.0, 0.0);
    uint4 ni = uint4(0u, 0u, 0u, 0u);
    for (uint k = 0u; k < 4u; ++k)
    {
        float target = wTop[k] / tot;
        float prev = 0.0;
        if (pi.x == iTop[k]) prev = pw.x;
        else if (pi.y == iTop[k]) prev = pw.y;
        else if (pi.z == iTop[k]) prev = pw.z;
        else if (pi.w == iTop[k]) prev = pw.w;
        float d = clamp(target - prev, -SOCK_CLAMP_DW, SOCK_CLAMP_DW);
        nw[k] = max(prev + d, 0.0);
        ni[k] = iTop[k];
    }
    float s2 = nw.x + nw.y + nw.z + nw.w;
    if (s2 > 1e-6) nw /= s2;
    SockBlendRec rec;
    rec.w = nw; rec.i = ni;
    SockBlendOut[vid] = rec;
}

// Copy-through for non-sock rows and no-anchor sock rows (idempotent; no init pass needed)
void SockSyncRow(uint i)
{
    SockBlendRec rec;
    // R3（t31）：恒等写回优先沿用「上一帧写出的 BlendOut」
    float r3Sum = SockBlendOut[i].w.x + SockBlendOut[i].w.y + SockBlendOut[i].w.z + SockBlendOut[i].w.w;
    rec.w = r3Sum > 1e-3 ? SockBlendOut[i].w : BlendTyped[i * 2u + 0u];
    rec.i = r3Sum > 1e-3 ? SockBlendOut[i].i : asuint(BlendTyped[i * 2u + 1u]);
    SockBlendOut[i] = rec;
}
// ===== end T16 sock slide =====================================================
// 一次查询最多检查的三角形数（性能上限）。实测（2026-10-11 当前网格 37×37×45、
// 881 个三角形、非空格 74 个）每格最多 115 个、27 邻域候选 233~643 个 ⇒ 当前数据
// 下加大上限不改变结果，是纯防御性留量。【用户 m00742】要求「把面中心的优先级
// 提高」：生成器把每格三角形按「三角形中心到格中心距离」升序登记（见
// node_postprocess_draginteraction.py 的 `_bake_collider_tri_grid`，那里的注释即
// 「最近的三角形才是决定带符号距离正负的那个」），所以截断 = 把最近的面中心丢掉。
// 生成器侧 max_per_cell 默认 4096、不截断；这里与之对齐，碰撞体加密后面心优先级
// 才不会被打折。（旧值 1024 是早期点云方案遗留的经验值。）
#define COLLISION_GRID_MAX_TRIS  4096u

#define COLLISION_NEAR_K     8u
// 基准的防零下限（vb0 局部单位）。2026-10-11 修正：这里原先写 0.001，配合下面的
// `max(abs(t0), COLLISION_MIN_GAP)` 会把每顶点阈值整体抬成常数 0.001 —— 而导出侧
// 烘的 t0 实测只有 2.98e-5..1.37e-4（丝袜与大腿在绑定姿态下的真实表面距离，全部
// < 0.2mm），于是 235/235 个顶点都被 MIN_GAP 接管，ColliderVertexL0 的 t0 通道在
// 运行时一个都没被读到：语义从生成器的「不许比绑定时更近」变成「必须停在外侧
// 1.43mm」，而绑定姿态下 t0 < 0.001 恒成立 ⇒ 一进拖拽就 100% 违约、整圈顶点被推离
// 表面。现在只留一个纯防浮点零的下限，阈值语义完全由 t0 决定。
#define COLLISION_MIN_GAP    1e-6
// 距离阈值倍率：顶点必须离碰撞体表面至少 base * COLLISION_GAP_SCALE（base 是导出侧
// 烘的绑定姿态间隙 t0）。
// 【2026-10-11 晚修正，推翻同日 m00742 的 ×10】曾按「增大阈值为原本的 10 倍」取 10.0，
// 实测那样是错的：绑定姿态本身 d == t0，而阈值被抬成 10*t0 ⇒ **绑定姿态 100% 违约**
// ⇒ zone 一激活，影响半径内的全部顶点（本次导出 883 个）立刻被求解器外推 9*t0
// （3~15mm），视觉上就是「模型局部鼓起来、越过阈值线」；回弹结束的那一帧又会从 10*t0
// 一步跳回 t0（同样的 3~15mm 跳变）。用户 m01161 实机反馈「回弹还是会越过判定」正是
// 这一条。取 1.0 ⇒ 阈值 == 绑定姿态距离 ⇒ 不变量回到生成器 _bake_collider_sdf 里写的
// 那句「dist(p, 表面) >= dist(p0, 表面)，即离表面不许变近」：绑定姿态恰好达标（不动），
// 只有被拖向表面内侧时才推回，归位时 gap 与 t0 相同因而没有跳变。
#define COLLISION_GAP_SCALE  1.0
// 基准符号【用户 2026-10-11 重新裁定】：把碰撞体表面记作 0，外侧为正、内侧为负，
// 丝袜顶点必须停在表面**外侧**，所以基准必须取正 ⇒ gap = +t0（每顶点各自约 7e-5，
// 即 0.1mm 量级），顶点被约束在「不比绑定姿态更靠近表面」的位置 —— 这正是丝袜
// 贴在大腿上的实际状态。取负（旧写法）会让 gap = -t0，也就是把顶点推到表面**内侧**，
// 那仍然是穿进大腿里，与「防穿模」的目标相悖。
// 历史沿革：最早 MIN_GAP = 0.01 比 t0 大一个量级，取正会把贴着大腿的顶点整片推出
// 1cm（丝袜飘起），所以当时改成取负；后来 MIN_GAP 下调到 0.001，但 t0 实测仅
// 2.98e-5..1.37e-4 仍远小于它，取正又把整圈顶点外推 1.43mm。2026-10-11 把 MIN_GAP
// 降为纯防零的 1e-6，阈值回到每顶点各自的 t0，取正才是「贴合的丝袜」应有的语义。
// 1.0 = 取正（表面外侧，当前），-1.0 = 取负（旧行为）。
#define COLLISION_BASE_SIGN  (1.0)
// 拉伸上限（vb0 局部单位）：位移后顶点到「绑定姿态下最近的那个采样点」的
// 距离，不得超过该距离 + 本值。这是「丝袜不能离开大腿」的直接表达 ——
// 点云只覆盖丝袜贴着的那条腿，一旦顶点滑到腿外（另一条腿/腿背面），
// 最近的采样点会突然跳到 0.17 之外、切平面判据全部「不违约」而放行。
// 实测（.dbg/collision_sim/verify_combo.cjs，48 案例 × 235 顶点）：
// 0.05 时穿模 185→29、最大穿深 0.131→0.0185，位移保留仅 0.36→0.31。
#define COLLISION_STRETCH    0.03
// 沿 p0→p1 的粗扫步数（之后再做 8 次二分收窄）。不加 [unroll]：
// 本函数体内已有一个「遍历全部采样点」的动态循环，展开会让指令数爆炸
// （上一版加 [unroll] 导致编译 525s + 画面冻结）。
#define COLLISION_CCD_STEPS  16u
// 收尾投影迭代的轮数。【用户 m00742/m00964】要求「回弹的计算也要受到碰撞的限制……
// 不会越过阈值，而是直接停在阈值」「回弹过程中不能穿过碰撞体」。上面的路径采样 + rem
// 回加在「绑定姿态下 d 恰好等于 gap」的临界情形下每轮只修正极小量，所以再补一段投影
// 迭代把终点严格夹到 gap。
// 【2026-10-11 全量实测】150 个可拖拽顶点 x 3 方向（+n0 / -n0 / 切向）x 7 个位移
// （0.002..0.20）= 3150 例，统计 signed < 0（穿进碰撞体）的例数：
//   16 轮 -> 78 例穿入，最坏 -15.42mm（约 2.2 倍 gap）
//   32 轮 -> 8 例，最坏 -7.16mm
//   64 轮 -> 0 例，最坏 +0.000000（完全收敛）
//  128 轮 -> 与 64 轮逐例相同（无额外收益）
// 另测「每步多推 5% 留余量」反而恶化到 1 例穿入 / 最坏 -12.93mm，故不做超推。
// 多数顶点在 1~4 轮内就 pd >= gap 提前 break，只有深入或曲面难收敛的顶点才跑满。
#define COLLISION_PROJ_STEPS 64u

// 路径采样 + 推出这一步重复几轮。
// 【用户 m01041 根因】core 循环每轮都执行一次 `p = pStarOut + rem`（把「剩余位移」加回去），
// 所以轮数越多、位移被放大得越多，顶点最终会被推到碰撞体的另一侧 —— 而那一侧的带符号距离
// 同样是正的，`signed >= gap` 的判据完全看不出来。实测（60 顶点 × 5 方向 × 6 位移 = 1800 例，
// 指标 = moved / max(disp, gap) 的最坏倍数）：
//   4 轮 → 8.54x（最坏 v585：输入 0.005，实际移动 0.0766）
//   2 轮 → 3.71x
//   1 轮 → 1.76x  ← 采用
// 三轮的 thru（signed < 0）都是 0，所以减轮数不会牺牲防穿模；而 1 轮还把 grid-miss
// （顶点被推出碰撞网格覆盖范围）从 3 降到 0。
// 曲线路径的欠采样由下面的 COLLISION_PROJ_STEPS 投影迭代补足，不需要靠多轮 core。
#define COLLISION_CORE_STEPS 1u

// ── 数值容差（2026-10-22，掉帧根因）──────────────────────────────────────
// gap = t0，也就是「绑定姿态下顶点到碰撞体表面的真实间隙」，实测中位 3.9e-04、
// 最大 7.0e-03。也就是说收尾投影是在追一个 0.4mm 的目标 —— 而查询用的格子边长
// 就有 0.0684，最近三角形的距离在这个尺度上本来就抖。
// 结果：pd 卡在 gap 附近上下跳，`pd >= gap` 永远不成立，64 轮全部跑满。
// 连裤袜这批数据有 5532 个可拖拽顶点（旧的半条丝袜只有 235 个），每个顶点
// 80 次网格查询 × 上百个三角形，整帧就掉到 150ms。
// 这里给一个「到此为止」的判据：差得比 0.1mm 还少就当达标。视觉上分辨不出，
// 但它让绝大多数顶点在几轮内退出，而不是空转到 64。
#define COLLISION_NUMERIC_EPS 1e-4

// 单次网格查询最多读多少个三角形。
// 【2026-10-22 实测】左右两条腿的碰撞三角形个数完全一样（各 8199），但同一个
// 顶点邻域里能摸到的三角形数差很多：一侧均值 67 / p90 352，另一侧均值 34 /
// p90 74。格子是轴对齐的，原点 x = -1.1754、边长 0.0684，对称面落在第 17.18
// 格，不在格边界上，于是镜像的两条腿落进不同大小的格子足迹。这直接解释了
// 为什么一边 35fps、另一边 6fps。
// 封顶只影响稠的那一侧（74 < 256 不受影响），代价是极少数远处的三角形不参与
// 求解 —— 按「离中心格近的先扫」的顺序，被砍掉的是最外围那些。
#define COLLISION_QUERY_MAX_TRIS 256u

// 归位（弹簧停下）那一帧不继续走碰撞求解。
// 【2026-10-11 晚实测】「保持在阈值处」这条对照分支的代价：绑定姿态在 gap == 10*t0
// 时会整体外移 3~17mm；而 gap == t0 时所谓「一步跳回」根本不存在（两者相同，无跳变）。
// ⇒ 采用「弹簧一停就整波早退」，不保留该开关。

#define DETECT_SLOT_ID       0u
#define DETECT_SLOT_HIT      1u
#define DETECT_SLOT_NORMAL   5u
#define DETECT_SLOT_SCREEN_RIGHT 8u
#define DETECT_SLOT_SCREEN_DOWN  9u

float SafePositive(float value, float fallback)
{
    return value > 0.0 ? value : fallback;
}

float SafeNonZero(float value, float fallback)
{
    return abs(value) > 0.00000001 ? value : fallback;
}

float3 SafeNormalize(float3 v, float3 fallback)
{
    float lenSq = dot(v, v);
    if (lenSq <= 0.00000001)
        return fallback;

    return v * rsqrt(lenSq);
}

float4 ReadCaptured(uint slot, float4 fallback)
{
    uint count;
    uint stride;
    CapturedDetect.GetDimensions(count, stride);

    if (slot >= count)
        return fallback;

    return CapturedDetect[slot];
}

float4 ReadState(uint slot, float4 fallback)
{
    uint count;
    JiggleState.GetDimensions(count);

    if (slot >= count)
        return fallback;

    return JiggleState[slot];
}

float SimulationStep()
{
    float dt = SafePositive(TIME_PARAMS.x, 1.0 / 60.0);
    float speed = SafePositive(TIME_PARAMS.y, 1.0);
    float maxStep = SafePositive(TIME_PARAMS.z, 2.0);
    return clamp(dt * 60.0 * speed, 0.05, maxStep);
}

float ZoneRadiusOverride(uint zone)
{
    uint count;
    ZoneParams.GetDimensions(count);
    return zone * 2u < count ? ZoneParams[zone * 2u].x : 0.0;
}

float ZoneStrengthOverride(uint zone)
{
    uint count;
    ZoneParams.GetDimensions(count);
    return zone * 2u < count ? ZoneParams[zone * 2u].y : 0.0;
}

float ZoneOffsetOverride(uint zone)
{
    uint count;
    ZoneParams.GetDimensions(count);
    return zone * 2u < count ? ZoneParams[zone * 2u].z : 0.0;
}

float ZoneFalloffOverride(uint zone)
{
    uint count;
    ZoneParams.GetDimensions(count);
    return zone * 2u < count ? ZoneParams[zone * 2u].w : 0.0;
}

bool ZoneIsPathSlide(uint zone)
{
    uint count;
    ZoneParams.GetDimensions(count);
    return zone * 2u + 1u < count && ZoneParams[zone * 2u + 1u].z > 0.5;
}

// 【用户 m01161】「在区域空物体可拖拽旁边添加一个选项名为法线方向拖拽，拖拽方向严格
// 按照法线方向，鼠标的移动仅控制拖拽的强度而不是把拖拽指向鼠标方向」。
// 生成器把该区域开关写在 ZoneParams[zone*2+1].w（此前恒 1.0 且从未被读取，复用不改变
// ABI 形状）。开启 ⇒ 位移轴锁定为点中面的法线，鼠标位移只决定沿法线的推进量。
bool ZoneDragAlongNormal(uint zone)
{
    uint count;
    ZoneParams.GetDimensions(count);
    return zone * 2u + 1u < count && ZoneParams[zone * 2u + 1u].w > 0.5;
}

// Cheap, wave-uniform (every thread reads the same IniParams-derived value,
// so GPUs can skip the branch's instructions entirely rather than just not
// allocating for them) check for whether this export has ANY Path Slide
// zone at all. Gates VertexOwningZone's mask scan below -- without this,
// every jiggle dispatch paid that cost on every vertex even when nothing
// uses Path Slide (which is every export before this existed).
bool AnyPathSlideZone()
{
    return ZONE_RUNTIME_META.x > 0.5;
}

uint ClampZoneID(float zoneValue)
{
    uint paramCount;
    ZoneParams.GetDimensions(paramCount);
    uint zoneCount = max(paramCount / 2u, 1u);
    return min((uint)max(round(zoneValue), 0.0), zoneCount - 1u);
}

// Which zone does vertex i actually belong to (its strongest sparse weight,
// not the globally-active zone)?
uint VertexOwningZone(uint i, out float ownWeight)
{
    static const uint INVALID_ZONE = 0xffffffffu;
    uint idCount;
    uint weightCount;
    JiggleZoneIDs.GetDimensions(idCount);
    JiggleZoneWeights.GetDimensions(weightCount);
    uint4 ids = i < idCount ? JiggleZoneIDs[i] : uint4(INVALID_ZONE, INVALID_ZONE, INVALID_ZONE, INVALID_ZONE);
    float4 weights = i < weightCount ? JiggleZoneWeights[i] : 0.0f;
    uint zone = 0u;
    ownWeight = 0.0f;
    [unroll]
    for (uint slot = 0u; slot < 4u; ++slot)
    {
        if (ids[slot] != INVALID_ZONE && weights[slot] > ownWeight)
        {
            ownWeight = weights[slot];
            zone = ids[slot];
        }
    }
    return zone;
}

float4 ReadSharedInteraction(uint slot, float4 fallback)
{
    uint count;
    SharedInteractionState.GetDimensions(count);
    return slot < count ? SharedInteractionState[slot] : fallback;
}

// ============================================================
// Detector and jiggle now share vb0 local space directly.
// ============================================================

float3 ToJiggleSpace(float3 p)
{
    return p;
}

// ============================================================
// Offset is already in vb0 local space.
// ============================================================

float3 OffsetToVertexSpace(float3 v)
{
    return v;
}

// ============================================================
// FACE-NORMAL DRAG SIGN MODE
//   0 = 恒定 +n（默认；【用户 m01161】「鼠标的移动仅控制拖拽强度」⇒ 推进量 = 鼠标位移的
//       长度，方向恒为点中面的法线，与鼠标往哪边拖无关）
//   1 = 旧行为：符号取鼠标位移在屏幕法线上的投影符号（反向拖动会沿法线反向推顶点）
// 落点 = IniParams[119].z（模组 ini 的 z119）；未写该行时读 0 = 恒定符号，向后兼容。
// ============================================================
#define NORMAL_SIGN_MODE (ZONE_RUNTIME_META.z)

// ===== t33 explosion guard（爆炸守卫：非有限值消毒 + 位移硬上限）=================
// 痛点（逐条数值证据见本目录 probe_explosion.txt / verify_guard.txt）：
//   M1 非有限传播：物理状态/区域参数/上游任一环产生 NaN 时，
//      ClampVectorLength（`lenSq > maxSq` 对 NaN 为假）、PullTowardLimit
//      （`dragLen < 1e-6` 对 NaN 为假）、pow(saturate(..)) 全部“比较为假 ⇒ 原样返回”，
//      NaN 一路活到 `v.position += appliedOffset` 并写回 rw_buffer。
//   M2 求解器把静态错位放大成位移：CollisionSolveGrid 的收尾投影环每步最多 gap
//      （≈1.7mm）× 64 步；碰撞面与当前姿态不一致时，**零位移**顶点也会被推走
//      （实测 0.1 m 错位 ⇒ 单帧最大 0.0649 m，58/883 行打到 64 步上限）。
//   M3 硬赋值无界：T16 滑移 `v.position = q1 + sn * gRow` 与 localPos 无关联，
//      锚点/蒙皮面出错时位移是米级（实测 0.1~1.0 m）。
// 守卫只做两件事，且对合法输入逐位不生效：
//   (a) 任何非有限的中间量 → 回退到 fallback（通常是 0 或 localPos）；
//   (b) 写回前的位移（相对 localPos）按 GUARD_MAX_OFFSET 限幅（保方向、缩长度）。
// GUARD_MAX_OFFSET 默认 = maxOffset 的合法过冲余量：实测求解器正常收敛时会略超
// maxOffset（A -n0 0.5 → 0.500758，+0.15%；A -n0 0.25 → 0.25029），这类过冲不该被
// 守卫改掉（否则用户合法拖满时手感变化）；再加一次 gap 级推挤（|t0| ≤ 1.74mm）的余量。
// 可用 IniParams[119].w 覆盖（0 = 用默认；非 0 = 绝对上限，调试用）。
// 注意 .x 是滑移总闸、.z 已被 t7 的 NORMAL_SIGN_MODE 占用。
#define GUARD_MAX_OFFSET (ZONE_RUNTIME_META.w > 0.0 ? ZONE_RUNTIME_META.w : (maxOffset * 1.01 + 0.005))
// 求解器内部单步上限（投影环步长 / CCD 推挤量）：合法单步是 gap 级（≈1.7mm），
// 50mm 已是 30 倍余量；超了说明碰撞面与当前姿态不一致（实机顶点到烘焙锚点距离
// 中位数 0.5015m，姿态差半个身位）——此时宁可停在原处，也不把顶点瞬移半米。
#define GUARD_MAX_STEP 0.05
#define GUARD_EPS 1e-9

float3 GuardFinite3(float3 v, float3 fallback)
{
    return all(isfinite(v)) ? v : fallback;
}

// 保方向、缩长度的位移限幅：|p - anchor| <= maxLen；非有限 ⇒ 直接回退到 anchor。
float3 GuardClampDelta(float3 p, float3 anchor, float maxLen)
{
    float3 d = p - anchor;
    float len2 = dot(d, d);
    if (!all(isfinite(d)) || !isfinite(len2))
        return anchor;
    float maxLen2 = maxLen * maxLen;
    if (len2 > maxLen2 && len2 > GUARD_EPS)
        return anchor + d * (maxLen * rsqrt(len2));
    return p;
}
// ===== end t33 explosion guard ===============================================

// ============================================================
// SCREEN-STABLE CURSOR DRAG (face-independent)
// ============================================================

float2 GetScreenDragNormalized(float mouseXDirection, float mouseYDirection)
{
    float2 screenSize = max(CURRENT_CURSOR.zw, float2(1.0, 1.0));
    float screenReference = max(min(screenSize.x, screenSize.y), 1.0);
    float2 deltaPx = CURRENT_CURSOR.xy - CAPTURED_CURSOR.xy;

    return float2(
        deltaPx.x / screenReference * mouseXDirection,
        deltaPx.y / screenReference * mouseYDirection
    );
}

// Map pixel drag → vb0 offset without using the hit face normal.
// Nearby faces on a breast have very different normals; orthonormal frames
// built from them are discontinuous (axes flip), so the same mouse-up used
// to launch geometry left/down/up. Fixed local axes remove that lottery.
//
// Convention (ZZZ/GI upright body, camera-facing):
//   mouse +X → vb0 +X
//   mouse +Y → vb0 +Y
// mouseXDirection / mouseYDirection (IniParams) still flip signs if needed.
float3 BuildScreenStableDrag(float dragScale, float mouseXDirection, float mouseYDirection,
                             float3 screenRightLocal, float3 screenDownLocal, bool basisValid)
{
    float2 delta = GetScreenDragNormalized(mouseXDirection, mouseYDirection) * dragScale;
    return basisValid ? screenRightLocal * delta.x + screenDownLocal * delta.y
                      : float3(delta.x, delta.y, 0.0);
}

// 【用户 m00857 + m01161】「拖拽始终以法线方向拖拽，就是基于当前我所点中的这个面的
// 法线方向进行拖拽」；m01161 进一步要求「鼠标的移动仅控制拖拽的强度而不是把拖拽指向
// 鼠标方向」。
// 做法：位移轴恒为该面法线 normalLocal，幅度取鼠标位移的**长度**（与移动方向无关）。
// 符号由 NORMAL_SIGN_MODE 决定：
//   0（默认）= 恒定 +n：鼠标往哪边拖都沿法线推进，与鼠标方向完全无关 —— T3 契约裁决
//              （docs/analysis/rzm-drag-interaction-contract.md §A：残留的投影符号会让
//              50% 的鼠标方向把截面内侧顶点推到大腿内部，是穿模机制之一）；
//   1 = 旧行为（投影符号，保留以便实机 A/B 回退）。
// 历史：第一版写的是投影 dot(delta,nScreen)/|nScreen|²，垂直于法线方向拖动时推进量
// 恒为 0、平行时最大 —— 用户实机反馈「还是根据鼠标方向来的」，故改成纯长度语义；
// 本轮把残留的符号依赖也去掉（m01161 要求方向与鼠标无关）。
//
// normalLocal 与 screenRightLocal / screenDownLocal 同空间（三者都由 detect 阶段捕获、
// 经 ini 的同一路径传入），所以符号判定放在这个空间里做是自洽的。
float3 BuildFaceNormalDrag(float dragScale, float mouseXDirection, float mouseYDirection,
                           float3 screenRightLocal, float3 screenDownLocal, bool basisValid,
                           float3 normalLocal)
{
    float2 delta = GetScreenDragNormalized(mouseXDirection, mouseYDirection) * dragScale;
    if (!all(isfinite(delta)))
        delta = float2(0.0, 0.0);  // 捕获异常/退化输入：归零，别把 NaN 传进物理链
    float3 n = SafeNormalize(normalLocal, float3(0.0, 0.0, 1.0));
    if (!all(isfinite(n)))
        n = float3(0.0, 0.0, 1.0);  // 法线缺省/异常：退回 vb0 +z（与 SafeNormalize 的缺省一致）
    float2 nScreen = basisValid
        ? float2(dot(n, screenRightLocal), dot(n, screenDownLocal))
        : float2(n.x, n.y);
    // 默认（NORMAL_SIGN_MODE = 0）：符号恒 +1 ⇒ 位移 = n * |delta|，与鼠标方向无关。
    float signAlong = 1.0;
    if (NORMAL_SIGN_MODE > 0.5)
    {
        // 旧行为：法线几乎正对镜头时 nScreen → 0，符号无从判断 ⇒ 退回「始终拉出」，避免抖动。
        if (dot(nScreen, nScreen) > 0.04)
            signAlong = dot(delta, nScreen) < 0.0 ? -1.0 : 1.0;
    }
    return n * (length(delta) * signAlong);
}

float ComputeRubberInfluence(float dist, float radius, float falloffPower)
{
    if (dist >= radius)
        return 0.0;

    float x = 1.0 - saturate(dist / max(radius, 0.000001));

    // Shape the raw linear ratio too, before smoothstepping. Double-smoothstep
    // alone saturates x close to 1.0 for a wide plateau near the touch point,
    // so pow(s, falloffPower) below barely moves there even at large exponents
    // -- raising falloffPower only sharpened the outer transition, never the
    // near-center peak. Normalized so this pre-shape is a no-op (x^1 = x) at
    // the 1.5 default, leaving low falloffPower feel unchanged.
    x = pow(x, max(falloffPower / 1.5, 0.0001));

    // Smoothstep twice: soft edge, rounded center, no linear rubber plank.
    float s = x * x * (3.0 - 2.0 * x);
    s = s * s * (3.0 - 2.0 * s);

    return pow(s, falloffPower);
}

float3 ClampVectorLength(float3 v, float maxLen)
{
    float lenSq = dot(v, v);
    float maxSq = maxLen * maxLen;

    if (lenSq > maxSq && lenSq > 0.00000001)
        return v * (maxLen * rsqrt(lenSq));

    return v;
}

// Progressive resistance: dragDir/dragLen is the raw, UNSCALED screen drag
// (before strength is applied). maxLen is the hard ceiling ("how far this
// can stretch" — the zone's max_offset). rate controls how many maxLen's
// worth of drag it takes to approach that ceiling (this is what "strength"
// now means for grabbing: a bigger rate reaches the limit with less mouse
// movement). Unlike a linear-scale-then-clip, resistance is felt across the
// whole gesture — small drags are still ~1:1, but the closer you are to
// maxLen the less each extra pixel of drag moves you, asymptotically
// approaching (never reaching) maxLen instead of hitting a wall.
float3 PullTowardLimit(float3 dragDir, float rate, float maxLen)
{
    float dragLen = length(dragDir);
    if (dragLen < 0.000001 || maxLen <= 0.0)
        return float3(0.0, 0.0, 0.0);

    float pulledLen = maxLen * (1.0 - exp(-dragLen * rate / maxLen));
    return dragDir * (pulledLen / dragLen);
}

void ComputeNextPhysics(
    bool captureActive,
    bool hasCapturedID,
    float capturedID,
    float3 capturedCenterWorld,
    float3 capturedNormalWorld,
    float radius,
    float strength,
    float dragScale,
    float grabDamping,
    float grabSpring,
    float releaseDamping,
    float releaseSpring,
    float releaseKick,
    float maxOffset,
    float targetFollow,
    float timeStep,
    float mouseXDirection,
    float mouseYDirection,
    float3 screenRightLocal,
    float3 screenDownLocal,
    bool screenBasisValid,
    bool dragAlongNormal,
    out float4 outCurrent,
    out float4 outPrevious,
    out float4 outCenter,
    out float4 outNormal,
    out float4 outTarget,
    out float4 outPrevTarget,
    out float4 outRawTarget,
    out float4 outScreenRight,
    out float4 outScreenDown,
    out float4 outInputFlags)
{
    float4 stateCurrent  = ReadState(0u, float4(0.0, 0.0, 0.0, 0.0));
    float4 statePrevious = ReadState(1u, float4(0.0, 0.0, 0.0, 0.0));
    float4 stateCenter   = ReadState(2u, float4(0.0, 0.0, 0.0, -1.0));
    float4 stateNormal   = ReadState(3u, float4(0.0, 0.0, 1.0, 0.0));
    float4 stateTarget   = ReadState(4u, float4(0.0, 0.0, 0.0, 0.0));
    float4 statePrevTgt  = ReadState(5u, float4(0.0, 0.0, 0.0, 0.0));
    float4 stateScreenRight = ReadState(6u, float4(1.0, 0.0, 0.0, 0.0));
    float4 stateScreenDown  = ReadState(7u, float4(0.0, 1.0, 0.0, 0.0));
    float4 stateInputFlags  = ReadState(8u, float4(0.0, 0.0, 0.0, 0.0));

    bool stateAlive = stateCurrent.w > 0.5;
    bool wasCaptureActive = stateNormal.w > 0.5;
    // Per-component copy of the input edge. This prevents a component that
    // was not under the initial click from joining a drag later on.
    bool pressedThisFrame = captureActive && stateInputFlags.x < 0.5;

    // A hit is sampled only when the button goes down.  While it remains down,
    // the detector is deliberately ignored: moving through empty space or a
    // different body part must not replace this component's anchor.
    bool newCapture = pressedThisFrame && hasCapturedID;
    bool lockedCapture = CAPTURED_CURSOR.w > 0.5 && (wasCaptureActive || newCapture);
    bool stateBasisValid = stateScreenRight.w > 0.5 && stateScreenDown.w > 0.5;

    float3 centerWorld = (wasCaptureActive || stateAlive) ? stateCenter.xyz : capturedCenterWorld;
    float3 normalWorld = (wasCaptureActive || stateAlive) ? stateNormal.xyz : capturedNormalWorld;
    float objectID = (wasCaptureActive || stateAlive) ? stateCenter.w : capturedID;
    float3 activeScreenRight = (wasCaptureActive && stateBasisValid) ? stateScreenRight.xyz : screenRightLocal;
    float3 activeScreenDown = (wasCaptureActive && stateBasisValid) ? stateScreenDown.xyz : screenDownLocal;
    bool activeBasisValid = (wasCaptureActive && stateBasisValid) || (!wasCaptureActive && screenBasisValid);

    float3 currentOffset = stateAlive ? stateCurrent.xyz : float3(0.0, 0.0, 0.0);
    float3 previousOffset = stateAlive ? statePrevious.xyz : float3(0.0, 0.0, 0.0);
    float3 filteredTarget = stateAlive ? stateTarget.xyz : float3(0.0, 0.0, 0.0);
    float3 previousTarget = stateAlive ? statePrevTgt.xyz : float3(0.0, 0.0, 0.0);

    if (newCapture)
    {
        centerWorld = capturedCenterWorld;
        normalWorld = capturedNormalWorld;
        objectID = capturedID;
        currentOffset = float3(0.0, 0.0, 0.0);
        previousOffset = float3(0.0, 0.0, 0.0);
        filteredTarget = float3(0.0, 0.0, 0.0);
        previousTarget = float3(0.0, 0.0, 0.0);
    }

    float3 rawTargetOffset = float3(0.0, 0.0, 0.0);
    float spring = releaseSpring;
    float damping = releaseDamping;
    float currentTargetFollow = targetFollow;

    if (lockedCapture)
    {
        // 【用户 m00857】「拖拽始终以法线方向拖拽，就是基于当前我所点中的这个面的
        // 法线方向进行拖拽」⇒ 位移轴锁定为该面的法线 normalWorld（锁定期间冻结，
        // 鼠标划过别处不会换轴），鼠标位移只决定沿法线的推进量。
        // 【用户 m01161】该行为改由「区域空物体 → 法线方向拖拽」开关（ZoneParams
        // 的 [zone*2+1].w）按区域控制：开启走法线轴，关闭走旧的 screen-stable 轴。
        // strength is the raw (unscaled) drag direction; PullTowardLimit uses
        // strength as the approach rate toward maxOffset instead of a linear
        // multiplier, so maxOffset governs the whole gesture, not just a
        // rarely-reached tail-end clamp.
        float3 dragDir3D = dragAlongNormal
            ? BuildFaceNormalDrag(
                  dragScale, mouseXDirection, mouseYDirection,
                  activeScreenRight, activeScreenDown, activeBasisValid, normalWorld)
            : BuildScreenStableDrag(
                  dragScale, mouseXDirection, mouseYDirection,
                  activeScreenRight, activeScreenDown, activeBasisValid);
        rawTargetOffset = PullTowardLimit(dragDir3D, strength, maxOffset);
        spring = grabSpring;
        damping = grabDamping;
    }
    else
    {
        currentTargetFollow = targetFollow * 0.55;
    }

    float previousStep = clamp(SafePositive(statePrevious.w, 1.0), 0.05, 8.0);
    float previousTargetStep = clamp(SafePositive(statePrevTgt.w, 1.0), 0.05, 8.0);
    float3 targetVelocity = (filteredTarget - previousTarget) / previousTargetStep;
    previousTarget = filteredTarget;
    float targetFollowStep = 1.0 - pow(saturate(1.0 - currentTargetFollow), timeStep);
    filteredTarget = filteredTarget + targetVelocity * (0.35 * timeStep) + (rawTargetOffset - filteredTarget) * targetFollowStep;
    filteredTarget = ClampVectorLength(filteredTarget, maxOffset);

    float3 targetOffset = filteredTarget;

    float3 velocity = (currentOffset - previousOffset) / previousStep;

    if (!captureActive && wasCaptureActive)
        velocity *= releaseKick;

    velocity *= pow(saturate(damping), timeStep);

    float3 acceleration = (targetOffset - currentOffset) * spring;
    float3 nextOffset = currentOffset + (velocity + acceleration * timeStep) * timeStep;
    nextOffset = ClampVectorLength(nextOffset, maxOffset);

    float3 nextVelocity = nextOffset - currentOffset;

    bool stillAlive = lockedCapture
        || dot(nextOffset, nextOffset) > 0.00000025
        || dot(nextVelocity, nextVelocity) > 0.00000025;

    if (!stillAlive)
    {
        nextOffset = float3(0.0, 0.0, 0.0);
        currentOffset = float3(0.0, 0.0, 0.0);
        filteredTarget = float3(0.0, 0.0, 0.0);
        previousTarget = float3(0.0, 0.0, 0.0);
        rawTargetOffset = float3(0.0, 0.0, 0.0);
        objectID = -1.0;
    }

    outCurrent  = float4(nextOffset, stillAlive ? 1.0 : 0.0);
    outPrevious = float4(currentOffset, timeStep);
    outCenter   = float4(centerWorld, objectID);
    outNormal   = float4(normalWorld, lockedCapture ? 1.0 : 0.0);
    outTarget   = float4(filteredTarget, 0.0);
    outPrevTarget = float4(previousTarget, timeStep);
    outRawTarget = float4(rawTargetOffset, 0.0);
    outScreenRight = float4(newCapture ? screenRightLocal : stateScreenRight.xyz, newCapture && screenBasisValid ? 1.0 : stateScreenRight.w);
    outScreenDown  = float4(newCapture ? screenDownLocal : stateScreenDown.xyz, newCapture && screenBasisValid ? 1.0 : stateScreenDown.w);
    outInputFlags  = float4(captureActive ? 1.0 : 0.0, 0.0, 0.0, 0.0);
}

// ============================================================
// COLLISION (anti-clipping) — mode 2 (drag) only, per-hit vertices
// 设计语义：导出时为每个权重顶点烘焙「到碰撞体表面的带符号距离」t0 作阈值
// （ColliderVertexL0）。运行时任何位移后，顶点到碰撞体表面的距离不得低于 t0：
// 低于则停在阈值处（硬=沿位移路径二分到边界；软=沿表面法线推回、保留切向滑动）。
// 判定用带符号表面距离 s = dot(p - nearestQ, nearestN)（负 = 已穿透）；
// 旧实现用「距最近采样点」的无符号距离 bestD 做触发，稀疏点云下恒放行 → 无效果。
// ============================================================

// ============================================================
// 碰撞求解：固定参考点 + 顶点法线切平面投影（SOFT）
//
// 三条结论均由模组实测取证（K:\SSMT-Package-master\3Dmigoto\ZZZ\Mods\
// SSMTGeneratedMod\洛克茜1111，Position.buf 18231 顶点 / 点云 593 点）：
//
// ① 判定值必须固定参考点，不能每帧重新搜索最近采样点。
//    重新搜索时「最近采样点」会随顶点移动切换，切平面估计与真实表面距离
//    偏差达 ±0.02，而丝袜到大腿的真实间隙只有 0.001 —— 于是既误放行
//    （74/235 顶点穿模），又让相邻顶点朝不同方向抽动（拖住不动时来回抖动）。
//    固定参考点用导出侧 L0 的 q0：离线验证 t0 ≡ dot(绑定姿态位置 - q0, n0)，
//    235/235 个权重顶点误差为 0，即 q0/n0 与 t0 同源。
//
// ② 约束方向必须用顶点自身法线，不能用点云法线 n0。
//    实测 n0 与真实表面法线夹角中位 22.78 度（94/235 超过 30 度、69/235
//    超过 45 度、最大 174.35 度），拖拽位移 maxOffset=0.5 时会漏出
//    0.5*sin(22.8°)=0.19 的斜向穿透，远超 0.001 的真实间隙 —— 强拉穿模
//    的主因。顶点自身法线（Position.buf offset12-23）夹角中位 11.80 度、
//    最大 31.93 度、超过 45 度的 0 个，且 235/235 全部朝外，可靠得多。
//
// ③ 必须用 SOFT 投影，不能用沿位移路径的 HARD 二分。
//    HARD 返回 p0 + offset*tLo，而 tLo 因顶点法线方向不同而不同：同一个
//    四边形上，偏内的顶点被削到 tLo≈0.3、偏切向的顶点保持 1.0，相邻顶点
//    位移比例差 3 倍 ⇒ 位移场不连续 ⇒ 面被折成尖角，弦切进曲面。
//    SOFT 只裁掉沿顶点法线向内的分量（s1 < s0 时 p1 -= vn*(s1-s0)），
//    切向完整保留，输出是位移的连续函数，面平滑变形。
//
// 已知局限（SOFT 分支）：约束面是「过绑定位置、以顶点法线为法线」的切平面。
// 大腿是曲面（曲率半径约 0.05），切向滑动超过该尺度后顶点仍会离开弧面 ——
// 回弹阶段法向分量被裁掉、切向分量占比高时尤其明显。默认改走 HARD 分支：
// 位移方向不变、只沿路径缩短，顶点停在违约点之前，回弹时不会切向滑出去。
// ============================================================

// 顶点 p 是否满足全部碰撞约束。绑定位置 p0 处每个约束都取等号
// （nearD[j] = dot(p0 - qj, nj)、nearLim[j] = |p0 - qj| + stretch）⇒ 恒返回 true，
// 因此「沿 p0→p1 找第一个违约点」的二分一定有解。
//
// 两类约束：
//   1) 切平面：dot(p - qj, nj) >= dot(p0 - qj, nj)，即沿该采样点法线不许向内。
//      取绑定姿态下最近的 K 个采样点、每个点带自己的法线，K 个切平面联合起来
//      就是弧面的多面体近似。
//   2) 拉伸上限：|p - qj| <= |p0 - qj| + COLLISION_STRETCH，即不许离开该采样点
//      太远。切平面约束只覆盖 qj 自己的邻域；顶点一旦滑出这个邻域（大腿是
//      半径约 0.05 的弧面，滑 0.17 就出去了），最近的采样点会跳到腿的另一侧，
//      切平面判据全部不违约而放行 —— 拉伸上限就是补这个洞。

float PointTriQuery(float3 p, float3 a, float3 b, float3 c,
                    out float3 outQ, out float3 outN)
{
    float3 ab = b - a;
    float3 ac = c - a;
    float3 ap = p - a;
    float d1 = dot(ab, ap);
    float d2 = dot(ac, ap);
    float3 bp = p - b;
    float d3 = dot(ab, bp);
    float d4 = dot(ac, bp);
    float3 cp = p - c;
    float d5 = dot(ab, cp);
    float d6 = dot(ac, cp);

    float va = d3 * d6 - d5 * d4;
    float vb = d5 * d2 - d1 * d6;
    float vc = d1 * d4 - d3 * d2;
    float denom = va + vb + vc;

    // 边投影参数 t = dot(edge, p - e0) / |edge|^2。d1/d2 就是 dot(ab, ap) /
    // dot(ac, ap)，可直接复用；BC 边另算。注意不能用 d3/(d3-d4) 那一类公式，
    // 那是平面重心的中间量，不是边参数（实测会把 0.081 的真实距离算成 0.346）。
    float3 bc = c - b;
    float3 q = a + ab * saturate(d1 / max(dot(ab, ab), 1e-30));
    float best = distance(p, q);
    float3 cand = a + ac * saturate(d2 / max(dot(ac, ac), 1e-30));
    float d = distance(p, cand);
    if (d < best) { best = d; q = cand; }
    cand = b + bc * saturate(dot(bc, p - b) / max(dot(bc, bc), 1e-30));
    d = distance(p, cand);
    if (d < best) { best = d; q = cand; }
    d = distance(p, a);
    if (d < best) { best = d; q = a; }
    d = distance(p, b);
    if (d < best) { best = d; q = b; }
    d = distance(p, c);
    if (d < best) { best = d; q = c; }

    // 面区域内才用平面投影：顶点落在三角形所在平面上但在三角形外侧时，投影
    // 距离会是 0（实测 p=(1.3,-0.4,0) 对三角形 (0,0,0)(1,0,0)(0,1,0) 得 0.0，
    // 真实距离 0.5），那正是「切平面」路线的老毛病。
    if (abs(denom) > 1e-30)
    {
        float v = vb / denom;
        float w = vc / denom;
        if (v >= 0.0 && w >= 0.0 && v + w <= 1.0)
        {
            cand = a + ab * v + ac * w;
            d = distance(p, cand);
            if (d < best) { best = d; q = cand; }
        }
    }

    outQ = q;
    outN = cross(ab, ac);
    outN = outN * rsqrt(max(dot(outN, outN), 1e-30));
    return best;
}

// 动态三角形网格求解：约束 =「到碰撞体表面的带符号距离不许比绑定姿态时更小」。
// 基准 d(p0) 在运行时现算（p0 就是绑定姿态位置），不需要额外缓冲。
// 切向滑动时距离基本不变（放行）、往外拉变大（放行）、往里推变小甚至变负（拦住）
// —— 这正是「任意方向都能拉，但不能穿进去」。
//
// 网格查询有个天然盲区：它只能看到「离表面一个格子以内」的三角形。顶点一旦
// 离开表面超过 cell（实测 0.0585），27 邻域里一个三角形都没有，TriGridDist
// 返回 1e30 —— 这既可能是「在外侧远处」（安全）也可能是「已经穿到内侧」（危险），
// 光看这个数分不出来。实测 v22525 沿 [0.71,0.71,0] 拉 0.25：t=0.90 时离表面
// +0.163，t=0.95 时已经 -0.145（穿过去了），但两个采样点网格都落空 ⇒ 全被放行。
// 所以落空时必须换一个不依赖距离大小的判据兜底：用导出侧烘在 ColliderVertexL0
// 里的采样点法线 n0，判「沿 p0 处外法线向内不许超过容差」。这个半空间判据在
// 绑定姿态取等号，位移的向内分量被裁掉、切向分量保留，正好接住网格的盲区。
// 约束判据：沿顶点法线的负方向，射线长度不许比绑定姿态时更短（容差内）。
//   · 打空（r < 0）：顶点已滑出碰撞体表面 ⇒ 无约束，放行。
//   · 往外拉：射线变长 ⇒ 满足。
//   · 沿表面滑动：射线长度基本不变 ⇒ 满足（这就是「表面滑移」）。
//   · 往里压：射线变短 ⇒ 违反，被拦停。
// 绑定姿态处 r(p0) = base ⇒ 恒取等号，所以「沿 p0→p1 找第一个违约点」的
// 二分一定有解。
// 到碰撞体表面的带符号距离：内部为负、外部为正。
// 遍历网格内三角形取最近点，用导出侧定向好的外法线判符号（与顶点位置无关，
// 因此半闭合面上「内部」恒为负）。网格没覆盖时返回 1e30。
// ── B1：把静态烘焙的碰撞三角形顶点按当帧骨架蒙皮 ──────────────────────────
// 输入用表里烘的绑定坐标（已实测与 Position 缓冲逐点相同，max diff = 0.0mm）。
bool CollisionSkinActive()
{
    return COLLISION_PARAMS.x > 0.5 && COLLISION_META.x >= 2.0;
}

float3 SkinColliderVertex(uint i)
{
    // i = 角点序号 0..2642（= tri*3 + c）；几何按**每三角形 4 个 float4** 的表取，
    // 蒙皮按**全局顶点号** ColliderTriGridVerts[i]（18520..19038）取。
    uint tri = i / 3u;
    uint c = i - tri * 3u;
    float3 pBind = ColliderTriGridTris[tri * 4u + c].xyz;    // 绑定位置（实测 = Position 缓冲同索引逐点相同）
    uint gv = ColliderTriGridVerts[i];                       // 全局顶点号（Blend 表行号）
    float4 bw = BlendTyped[gv * 2u + 0u];                    // 磁盘布局：float4#0 = 权重
    float4 bi = BlendTyped[gv * 2u + 1u];                    //            float4#1 = 索引
    uint4 idx = uint4(asuint(bi.x), asuint(bi.y), asuint(bi.z), asuint(bi.w));
    float3 p = 0.0;
    [unroll]
    for (uint k = 0u; k < 4u; k++)
    {
        float w = bw[k];
        if (w <= 0.0) continue;
        if (idx[k] == 0xFFFFFFFFu) continue;                 // B3：nil 槽防护（重新导出后可能出现）
        uint b = idx[k] * 3u;
        float4 r0 = ColliderSkinSkeleton[b + 0u];
        float4 r1 = ColliderSkinSkeleton[b + 1u];
        float4 r2 = ColliderSkinSkeleton[b + 2u];
        p += w * float3(dot(r0.xyz, pBind) + r0.w,
                        dot(r1.xyz, pBind) + r1.w,
                        dot(r2.xyz, pBind) + r2.w);
    }
    return p;
}

// 读一个碰撞三角形的 3 个顶点 + 定向面法线（B1 生效时用蒙皮结果，法线用叉积重算并按烘的表法线定向）
void ReadColliderTri(uint tri, out float3 ta, out float3 tb, out float3 tc, out float3 nOut)
{
    uint vb = tri * 4u;
    if (CollisionSkinActive())
    {
        uint b3 = tri * 3u;
        ta = SkinColliderVertex(b3 + 0u);
        tb = SkinColliderVertex(b3 + 1u);
        tc = SkinColliderVertex(b3 + 2u);
        float3 nBaked = ColliderTriGridTris[vb + 3u].xyz;
        float3 g = cross(tb - ta, tc - ta);
        float gl = length(g);
        nOut = gl > 1e-9 ? g / gl : nBaked;
        // 【B1：不做符号强制】三角形真翻面（>90°）时把法线掰回绑定朝向会让 DistGridSigned 的
        //  内外符号判反、约束方向反向 ⇒ 只保留上面的退化兜底（叉积过小时回退烘的表法线）。
    }
    else
    {
        ta = ColliderTriGridTris[vb + 0u].xyz;
        tb = ColliderTriGridTris[vb + 1u].xyz;
        tc = ColliderTriGridTris[vb + 2u].xyz;
        nOut = ColliderTriGridTris[vb + 3u].xyz;
    }
}

// 与 DistGridSigned 同逻辑，但额外返回最近三角形的外法线（导出侧定向好的 nOut）。
// 用于把顶点主动推离表面（外扩）—— 光靠「判据 + 砍位移」做不到外扩，因为绑定姿态
// 下顶点本来就在 gap 以内，二分会把 tLo 直接压到 0、顶点被钉死。

// 把一个三角形纳入「最近三角形」的累积比较。抽出来是为了让调用点只有一行 ——
// 下面中心格与 26 个邻格要各扫一遍，内联两份会让这段代码翻倍。
void GridAccumTri(uint tri, float3 p,
                  inout float bestAbs, inout float bestSigned, inout float3 bestN)
{
    float3 ta, tb, tc, nOut;
    ReadColliderTri(tri, ta, tb, tc, nOut);
    float3 q; float3 fn;
    PointTriQuery(p, ta, tb, tc, q, fn);
    float3 diff = p - q;
    float d = length(diff);
    if (d < bestAbs)
    {
        bestAbs = d;
        bestSigned = dot(diff, nOut);
        bestN = nOut;
    }
}

float DistGridSignedN(float3 p, out float3 outN)
{

    float3 bmin = COLLISION_TRIGRID.xyz;
    float cell = COLLISION_TRIGRID.w;
    int3 dims = int3(max(COLLISION_TRIGRID_DIMS.xyz, 1.0));
    uint cellCount = (uint)max(COLLISION_TRIGRID_DIMS.w, 0.0);
    uint idxBase = 9u + cellCount;

    int3 c = (int3)floor((p - bmin) / max(cell, 1e-6));
    c = clamp(c, int3(0, 0, 0), dims - int3(1, 1, 1));

    float bestAbs = 1e30;
    float bestSigned = 1e30;
    float3 bestN = float3(0.0, 1.0, 0.0);
    uint checked = 0u;

    // 扫描顺序：中心格先扫，再扫 26 个邻格。
    // 【2026-10-22 卡死事故】上一版这里写成 static const int3 kOffs[27] + [unroll]，
    // 想让顺序变成「中心 → 面 → 棱 → 角」。结果 [unroll] 把整个 27 次循环连同里面
    // 那个动态长度的三角形循环一起复制了 27 份，fxc 编译时间暴涨，游戏在重载
    // shader 那一步直接卡死。教训：这个函数里的循环规模不小（每格可达 4096 个
    // 三角形），绝不能让外层强制展开。现在回到编译器熟悉的「三层 -1..1」，只在
    // 内层跳过原点，中心优先的效果一样，代码量不变。
    {
        uint ci = (uint)(c.x + dims.x * (c.y + dims.y * c.z));
        uint s = ColliderTriGrid[8u + ci];
        uint e = ColliderTriGrid[8u + ci + 1u];
        if (e > s + COLLISION_GRID_MAX_TRIS) e = s + COLLISION_GRID_MAX_TRIS;
        [loop]
        for (uint k = s; k < e && checked < COLLISION_QUERY_MAX_TRIS; ++k)
        {
            GridAccumTri(ColliderTriGrid[idxBase + k], p, bestAbs, bestSigned, bestN);
            ++checked;
        }
    }

    [loop]
    for (int oz = -1; oz <= 1; ++oz)
    {
        if (checked >= COLLISION_QUERY_MAX_TRIS)
            break;
        [loop]
        for (int oy = -1; oy <= 1; ++oy)
        {
            if (checked >= COLLISION_QUERY_MAX_TRIS)
                break;
            [loop]
            for (int ox = -1; ox <= 1; ++ox)
            {
                if (checked >= COLLISION_QUERY_MAX_TRIS)
                    break;                       // 预算用完 ⇒ 外圈不再扫
                if (ox == 0 && oy == 0 && oz == 0)
                    continue;                    // 中心格上面已扫过
                int3 nn = c + int3(ox, oy, oz);
                if (any(nn < int3(0, 0, 0)) || any(nn >= dims)) continue;
                uint ci = (uint)(nn.x + dims.x * (nn.y + dims.y * nn.z));
                uint s = ColliderTriGrid[8u + ci];
                uint e = ColliderTriGrid[8u + ci + 1u];
                if (e > s + COLLISION_GRID_MAX_TRIS) e = s + COLLISION_GRID_MAX_TRIS;
                [loop]
                for (uint k = s; k < e; ++k)
                {
                    if (checked >= COLLISION_QUERY_MAX_TRIS)
                        break;
                    GridAccumTri(ColliderTriGrid[idxBase + k], p, bestAbs, bestSigned, bestN);
                    ++checked;
                }
            }
        }
    }
    outN = bestN;
    return bestSigned;
}

// 最终不变量：位移不得沿起点法线 n0 向内（切向分量原样保留）。
// 依据：对凸碰撞体，过表面点 p0 的切平面是支撑平面，实体整个落在它内侧 ⇒ 位移停在
// {dot(q - p0, n0) >= 0} 里就不可能进实体。凹处（两腿之间）只是近似，靠各求解器自己
// 的路径检查兜底。写成半空间投影：只剪负分量、不动切向 ⇒「沿表面滑移」手感不变。
float3 GuardInwardHalfSpace(float3 p0, float3 p, float3 n0)
{
    float3 disp = p - p0;
    float inward = dot(disp, n0);
    return inward < 0.0 ? p0 + disp - n0 * inward : p;
}

// 【远侧方向性约束 · 用户 m00001/m00030 的「半径覆盖整柱」场景】
// 问题：原有碰撞只保证「不沿顶点法线向内」，切向分量原样保留。半径覆盖整根圆柱时，
// 右侧 45°~90° 的顶点虽然不穿进实体，但会沿切向绕柱滑到左侧，看起来就是「右边跟着走」。
// 做法：把碰撞体当作一根有轴向范围的圆柱（导出侧 PCA 给中心/轴/轴向范围/最大半径），
// 以「抓取点所在侧」为近侧、反侧为远侧；远侧顶点（含中面）沿拖拽方向的位移按到中面的
// 距离平滑衰减到 0，切向（垂直于拖拽方向）保留。远侧顶点因此停在原表面点/只做贴壁滑，
// 不会沿切向绕到另一侧，也不会被推进实体内部。
//   - 一次轴向 dot + 一次径向长度 + 一次侧向 dot，不做三角网格查询。
//   - 轴向范围外的顶点（从开口端/顶部进入）直接放行；径向包络外（圆柱外的角落）放行。
//   - 过渡宽度用碰撞体半径（几何量），不是拍脑袋的容差；中面处 block=1，近侧表面 block=0，
//     因此远侧完全截断、近侧平滑过渡，不会在壁上留下台阶。
//   - 只在碰撞开启时由调用方进入；碰撞关或形状无效（[114].z=0）时原样返回。
float3 ClampFarSideBarrier(uint i, float3 p, float3 p0, float3 grab, float3 offset)
{
    if (COLLIDER_SHAPE_RANGE.z < 0.5)
        return p;
    float3 dRaw = SafeNormalize(offset, float3(0.0, 0.0, 0.0));
    if (dot(dRaw, dRaw) < 1e-12)
        return p;

    float3 center = COLLIDER_SHAPE_CENTER.xyz;
    float3 axis = SafeNormalize(COLLIDER_SHAPE_AXIS.xyz, float3(0.0, 1.0, 0.0));
    float axisMin = COLLIDER_SHAPE_AXIS.w;
    float axisMax = COLLIDER_SHAPE_RANGE.x;
    float radius = max(COLLIDER_SHAPE_RANGE.y, 1e-6);

    // 只关心「横向」拖拽；纯轴向拖拽不产生绕柱越界，直接放行。
    float3 d = dRaw - axis * dot(dRaw, axis);
    if (dot(d, d) < 1e-12)
        return p;
    d = normalize(d);

    float3 rel0 = p0 - center;
    float axial = dot(rel0, axis);
    // 开口端/顶部：不在碰撞体轴向范围内 ⇒ 不是「越过轮廓」，放行。
    if (axial < axisMin || axial > axisMax)
        return p;

    float3 radial = rel0 - axis * axial;
    float gap = max(abs(ColliderVertexL0[i * 2u + 0u].w), COLLISION_MIN_GAP);
    float envelope = radius + gap + COLLISION_NUMERIC_EPS;
    // 圆柱径向包络外（例如从上方/角落绕过去）放行。
    if (dot(radial, radial) > envelope * envelope)
        return p;

    float grabSide = dot(grab - center, d);
    if (abs(grabSide) < 1e-6)
        return p;                       // 抓取点落在中面上，方向不明确
    float s = grabSide > 0.0 ? 1.0 : -1.0;
    float mid = dot(center, d);
    float side = s * (dot(p0, d) - mid);
    // side <= 0：远侧/中面；side >= radius：近侧表面。
    float w = saturate(side / radius);
    float block = 1.0 - smoothstep(0.0, 1.0, w);
    if (block <= 1e-4)
        return p;

    // 只衰减「沿拖拽方向」的位移；切向原样保留 ⇒ 远侧停在原表面/贴壁滑，不被推进实体。
    float along = dot(p - p0, d);
    float alongAllowed = along * (1.0 - block);
    return p + d * (alongAllowed - along);
}

// 逐顶点碰撞求解的路径段（最终不变量在下面的 CollisionSolveGrid 里加）。
// 约束语义（用户 m03851 的「表面滑移」要求）：
//   切向位移自由保留，径向被裁到「离表面 ≥ base」。
// 用「沿位移路径二分」是错的 —— 二分只回答「整个位移是否合法」，而曲面滑移
// 时位移必然带一点向内的径向分量（弦切：沿切向走直线、表面是弧，弦在弧内侧），
// 于是二分把 tLo 压到 0，顶点被完全钉死（实测拖拽位移 = 0.0000）。
// 正确做法是投影：算出超出量，沿接触面法线推回。
// n0（起点外法线）与 gap（该顶点应停留的带符号距离）由调用方传入 —— 外层的不变量
// 也要用 n0，所以两者在 CollisionSolveGrid 里只算一次。
float3 CollisionSolveGridCore(float3 p0, float3 offset, float3 n0, float gap)
{
    float tolerance = max(COLLISION_PARAMS.y, 0.0);

    // ── 为什么不是「只查终点」────────────────────────────────────────────
    // 网格只覆盖表面附近那一层壳（非空格 405/42630），顶点一旦深入内部，查询要么
    // 打空、要么最近三角形跳到对面导致符号翻转 —— 实测沿顶点法线向内位移 0.10 时
    // 191/235 个顶点被判成「在外侧」，只查终点就会放行。
    // 从 p0（在表面外侧）出发，路径上第一次违约必然落在表面附近，那里的判据有效。
    //
    // ── 为什么推出方向用梯度而不是三角形外法线 ───────────────────────────
    // 三角形外法线是分片常数，相邻顶点落在不同三角形上时方向会跳变，位移场因此
    // 不连续 ⇒ 面被拉爆。仿真（沿腿轴拉 0.30）：用法线时最大边长比 7.64，改用
    // 「最近点 -> 顶点」的单位向量（距离场梯度，表面附近连续）后降到 1.01。
    float3 p = p0 + offset;

    // ── 终点预判：一次查询挡掉绝大多数顶点 ─────────────────────────────
    // 【性能 · 2026-10-22】原先这里无条件下面的 16 次 CCD 采样 + 最多 64 轮投影，
    // 每个被拖动的顶点每帧都要跑满，整帧掉到几百毫秒。
    // 而实测（400 个有效顶点、切向拖 0.2）：终点带符号距离已经有 98.5% 直接
    // >= gap —— 它们本来就不会违约，跑完整套求解是纯浪费。
    // 这里先查一次终点：达标就原样返回（等价于原环路里「全程在外侧 ⇒ 自由位移」
    // 的结论），不达标才进下面的完整求解。阈值语义与原来一致，不放松约束。
    // 代价：位移路径中途擦过碰撞体、但终点已在阈值外的情形不再被拦住（原 CCD
    // 会拦住）。位移 ≤ 0.5 而大腿半径远大于此，实际不会贯穿，接受这个取舍。
    {
        float3 nEnd;
        float dEnd = DistGridSignedN(p, nEnd);
        if (!isfinite(dEnd) || dEnd > 1e29)
            return p;                            // 邻域内没有三角形 ⇒ 远离碰撞体
        if (dEnd >= -1.0 && dEnd >= gap - COLLISION_NUMERIC_EPS)
            return p;                            // 已在阈值外侧 ⇒ 无需约束
    }
    float dbgFirstD = -12345.0;
    float dbgHits = 0.0;

    [loop]
    for (uint it = 0u; it < COLLISION_CORE_STEPS; ++it)
    {
        float tStar = -1.0;
        float dStar = 0.0;
        float3 qStar = p;

        [loop]
        for (uint st = 1u; st <= COLLISION_CCD_STEPS; ++st)
        {
            float t = (float)st / (float)COLLISION_CCD_STEPS;
            float3 pt = p0 + (p - p0) * t;
            float3 hitN;
            float d = DistGridSignedN(pt, hitN);
            if (st == 1u)
                dbgFirstD = d;
            // t33 守卫：正哨兵（打空）/负哨兵/NaN 一律不参与采样 —— 负哨兵会经
            // (gap - dStar) 被放大成 INF（captain 线索 1/2。本数据实测负哨兵未出现，
            // 属纵深防御：值域扫描见 probe_distrange.json）
            if (!isfinite(d) || d > 1e29 || d < -1.0)
            {
                // 【2026-10-22 掉帧修复】原来是 continue —— 打空就跳下一步接着采样，
                // 16 步全空就是 16 次全量查询。而网格只覆盖表面邻近那一层壳，打空
                // 意味着这一段路径本来就不在碰撞体的邻域里，再往后采样也是空的。
                // 直接结束 CCD，按「全程在外侧 ⇒ 自由位移」处理（终点预判已经用过
                // 同一套语义）。dbgHits 保留仅为调试观感。
                break;
            }
            dbgHits += 1.0;
            if (d < gap)
            {
                tStar = t;
                dStar = d;
                qStar = pt - hitN * d;           // d = dot(pt - q, hitN) ⇒ q = pt - hitN*d
                break;
            }
        }

        if (tStar < 0.0)
            break;                               // 全程在外侧 ⇒ 自由位移

        // 在表面附近做投影：沿梯度把违约点推到 gap 处，剩余位移裁掉向内分量。
        float3 pStar = p0 + (p - p0) * tStar;
        float3 g = pStar - qStar;
        float gl = length(g);
        if (gl < 1e-9)
            break;
        // 【根因修复 2026-10-11】推出方向必须朝外。(pStar - qStar) 是距离场梯度、
        // 表面附近连续（原设计要的正是这一点），但梯度只保证「指向距离增大的一
        // 侧」；带符号距离的符号来自三角形外法线（未定向），所以梯度在内侧时指向
        // 内侧。旧写法直接拿它当推出方向 ⇒ 顶点穿入时被推得更深 = 越拖越陷。
        // 这里保留梯度方向，只用顶点法线 n0（朝外、逐顶点连续）纠正其朝向。
        // 【用户 m01041】这个循环现在只跑 COLLISION_CORE_STEPS = 1 轮：每轮都会
        // 执行一次下面的 `p = pStarOut + rem`，轮数越多位移放大越多（实测 4 轮最坏
        // moved/disp = 8.54x），顶点会被推到碰撞体另一侧 —— 而那一侧的 signed 也是
        // 正的，判据看不出来。1 轮 = 1.76x，且 thru 仍为 0。
        float3 gdir = g / gl;
        float3 pushDir = (dot(gdir, n0) < 0.0) ? -gdir : gdir;
        // t33 守卫：推挤量设界 —— 碰撞面陈旧时 gap - dStar 可达半身位（实机顶点
        // 到烘焙锚点距离中位数 0.5015m），此时宁可停在原处也不瞬移
        float guardPush = gap - dStar;
        if (!isfinite(guardPush) || guardPush > GUARD_MAX_STEP || guardPush < -GUARD_MAX_STEP)
            guardPush = 0.0;
        float3 pStarOut = pStar + pushDir * guardPush;
        float3 rem = (p - p0) * (1.0 - tStar);
        float dn = dot(rem, pushDir);
        if (dn < 0.0)
            rem -= pushDir * dn;                 // 切向自由保留、径向裁掉
        p = pStarOut + rem;
    }

    // ── 收尾：严格夹在阈值 ────────────────────────────────────────────────
    // 【用户 m00742】「回弹的计算也要受到碰撞的限制，毕竟不能弹回来的时候他就可以穿模
    // 了，也要严格限制在阈值 —— 就是不会越过阈值，而是直接停在阈值」。上面的路径采样
    // 是「沿 p0→p 找第一个违约点，推到 gap 后再把剩下的位移 rem 加回去」，只要 rem 的
    // 径向分量被裁掉就能停住；但绑定姿态下 d 恰好等于 gap（t0 就是绑定姿态的真实间隙），
    // 第一步采样 t=1/16 就已经违约 ⇒ tStar 极小 ⇒ 每一步只修正一小段，深位移下收敛不足。
    // 这里补一段纯投影迭代：只要终点还在内侧（pd < gap），就沿最近三角形的外法线把它
    // 补到 gap —— 回弹阶段（位移正在衰减回 0）同样走这条路径，所以松手回弹也不会越过
    // 阈值。
    // 【刻意不做方向翻转】hitN 由生成器按三顶点法线均值定向过（恒朝外），而 pd 本身就是
    // 「沿 hitN 的带符号距离」⇒ p += hitN * (gap - pd) 一步即达 gap。
    // 历史教训（2026-10-11 仿真矩阵：10 顶点 × 5 方向 × 6 位移）：原先写成
    // (dot(hitN,n0) < 0) ? -hitN : hitN，而 n0 是【生成时刻】最近三角形的面法线 —— 顶点
    // 被拖走之后它已不代表当前位置的外法线，于是 pd 与 dot(hitN,n0) 异号时会把顶点朝
    // 【内】推，pd 在 ±gap 之间来回震荡：翻转版 34 例穿入，不翻转版只有 6 例。
    float pdPrev = -1e30;
    [loop]
    for (uint pit = 0u; pit < COLLISION_PROJ_STEPS; ++pit)
    {
        float3 projN;
        float pd = DistGridSignedN(p, projN);
        if (!isfinite(pd) || pd > 1e29 || pd < -1.0)
            break;                              // t33：NaN/正哨兵/负哨兵 ⇒ 退出（不拿 pd 修正）
        // 【2026-10-22 掉帧修复】gap 是 0.4mm 量级的真实间隙，而格子边长 0.0684，
        // 最近三角形的距离在这个尺度上本来就抖 —— `pd >= gap` 严格成立极难满足，
        // 于是这一环空转到 64 轮。差 0.1mm 以内当达标，省下的是几十轮全量查询。
        if (pd >= gap - COLLISION_NUMERIC_EPS)
            break;                              // 已达标（含数值容差）⇒ 停
        // 推了却没往前挪 ⇒ 已在抖动，继续推只是白跑查询
        if (pd - pdPrev < COLLISION_NUMERIC_EPS * 0.25)
            break;
        pdPrev = pd;
        float3 guardStep = projN * (gap - pd);
        if (!all(isfinite(guardStep))
            || dot(guardStep, guardStep) > GUARD_MAX_STEP * GUARD_MAX_STEP)
            break;                              // t33：步长非有限/超大 ⇒ 停在原处
        p += guardStep;
    }

    return p;
}

// 逐顶点碰撞求解入口：算好该顶点的法线与阈值，跑路径求解，最后压上最终不变量。
//
// 【最终不变量 · 2026-10-23】相对起点自身的起点法线 n0，位移不得含向内分量。
// 用户 m00001/m00030 的场景：拖拽半径很大时，丝袜某一圈上的顶点**整圈同时**被拽走，
// 中间隔着大腿 —— 正确的行为是「一部分顶点能动、正对腿的那部分被拦住」，实测却是整圈
// 一起穿到腿的另一侧。
// 光靠 CollisionSolveGridCore 挡不住，两个结构性的洞：
//   1) 终点预判的早退只看终点的带符号距离。整圈被同向推出去时终点落在**外侧**，
//      dEnd 为正 ⇒ 整圈原样放行（网格落空那支同样直接 return）；
//   2) 路径求解只保证「采样到的旧路径」和终点，不保证它**改完之后的新**路径 ——
//      pStarOut + rem 里的 rem 只裁掉沿 pushDir 的分量，顶点仍可能沿弦切方向擦进表面；
//      COLLISION_CORE_STEPS = 1 也不迭代。
// CPU 仿真实测（tools/check_drag_collision_ring_tunnel.py，圆柱 R=0.075、96 个顶点
// 整圈同向位移 0.15/0.35/0.50、8 个场景）：修前 304 个顶点沿实际位移穿进实体、最深
// 74.9mm（= 整个腿直径）、333 个顶点跨到另一侧；只加这一层夹取后陷穿 0、最深 0.00mm。
// 为什么不改成「方向门控 + 强制进完整求解」：那样 48/96 个顶点要跑满 16 次 CCD +
// 64 轮投影，直接回到每帧 150ms 的旧账上；而夹取只多一次 dot，零次网格查询。
// 几何依据：对凸碰撞体，过表面点 p0 的切平面是支撑平面，实体整个落在它的内侧 ⇒
// 位移停在 {dot(q - p0, n0) >= 0} 里就不可能进实体。凹处（两腿之间）只有近似性，
// 那里仍旧靠路径求解兜底。松手回弹同样走这条路径，所以回弹也不会越过阈值。
float3 CollisionSolveGrid(uint i, float3 p0, float3 offset, float3 vn)
{
    // 【根因修复 2026-10-11】ColliderVertexL0 是每顶点 8 个 float32：
    //   [i*2+0] = float4(q0.xyz, t0)    t0 = 绑定姿态带符号距离（实测 2.98e-5..1.37e-4）
    //   [i*2+1] = float4(n0.xyz, valid) valid = 0/1
    // 旧代码从 [i*2+1] 取 .w 当基准，读到的是 valid（可拖拽顶点恒 1.0）而不是 t0，
    // 于是 base = -1.0。必须从 [i*2+0] 取 t0。
    float4 l0q = ColliderVertexL0[i * 2u + 0u];
    float4 l0n = ColliderVertexL0[i * 2u + 1u];
    float3 n0 = l0n.xyz;
    float n0len = length(n0);
    n0 = n0len > 1e-6 ? n0 / n0len : vn;

    // 基准只用导出侧烘的 t0（绑定姿态下顶点到碰撞体表面的带符号距离，实测
    // 2.98e-5..1.37e-4，235 个顶点里 0 个负值），语义取自生成器 docstring：
    // 「运行时阈值：任何位移后该顶点到表面的距离不得低于 t0」。绝不能换成实时射线
    // 长度 —— 运行期射线长度对退化三角形会返回很大但没到 1e29 的值，那个「无限远」一旦
    // 进基准，投影就会把顶点推飞。
    // gap = 顶点应当停留的带符号距离（表面记 0，外侧为正）。COLLISION_BASE_SIGN = +1.0
    // ⇒ gap = +t0（约 7e-5）=「不比绑定姿态更靠近表面」，也就是丝袜贴在大腿上的实际
    // 状态。这里刻意不做 max(base, 0.0)：夹取会抹平符号约定（历史 bug：base 为负时被夹
    // 成 0，负阈值语义整体丢失，碰撞退化成「只在恰好为 0 时触发」）。
    // 【2026-10-11 修正】原写法 max(abs(t0), COLLISION_MIN_GAP) 配 MIN_GAP = 0.001，
    // 而 t0 全部小于 0.001 ⇒ 阈值被整体抬成常数 0.001（1.43mm），235/235 个顶点在
    // 绑定姿态下就满足 t0 < gap ⇒ 一进拖拽就 100% 违约、整圈顶点被推离表面。
    // MIN_GAP 已降为 1e-6 纯防零，阈值回到每顶点各自的 t0。
    float base = COLLISION_BASE_SIGN * max(abs(l0q.w), COLLISION_MIN_GAP);
    float gap = base * COLLISION_GAP_SCALE;

    float3 solved = CollisionSolveGridCore(p0, offset, n0, gap);

    // 最终不变量：剪掉位移沿 n0 的负分量（切向原样保留，滑移手感不变）。
    return GuardInwardHalfSpace(p0, solved, n0);
}

// 顶点 p 是否满足全部碰撞约束。绑定姿态 p0 处每个约束都取等号
// （nearD[j] = dot(p0 - qj, nj)、nearLim[j] = |p0 - qj| + stretch）⇒ 恒返回 true，
// 因此「沿 p0→p1 找第一个违约点」的二分一定有解。
//
// 两类约束：
//   1) 切平面：dot(p - qj, nj) >= dot(p0 - qj, nj)，即沿该采样点法线不许向内。
//      取绑定姿态下最近的 K 个采样点、每个点带自己的法线，K 个切平面联合起来
//      就是弧面的多面体近似。
//   2) 拉伸上限：|p - qj| <= |p0 - qj| + COLLISION_STRETCH，即不许离开该采样点
//      太远。切平面约束只覆盖 qj 自己的邻域；顶点一旦滑出这个邻域，最近的采样
//      点会换成另一个（法线也换），约束就出现断层 —— 拉伸上限把邻域边界也变成
//      约束，消除断层。
bool CollisionSatisfied(float3 p, float3 nearQ[COLLISION_NEAR_K],
                        float3 nearN[COLLISION_NEAR_K],
                        float nearD[COLLISION_NEAR_K],
                        float nearLim[COLLISION_NEAR_K], uint nearCount)
{
    for (uint j = 0u; j < nearCount; ++j)
    {
        if (dot(p - nearQ[j], nearN[j]) < nearD[j])
            return false;
        if (distance(p, nearQ[j]) > nearLim[j])
            return false;
    }
    return true;
}

float3 CollisionSolve(uint i, float3 p0, float3 offset, float3 vn)
{
    // t33 守卫：入口消毒（p0/offset/vn 非有限时不做任何求解，原样返回）
    if (!all(isfinite(p0)) || !all(isfinite(offset)) || !all(isfinite(vn)))
        return p0;
    // ── 动态三角形网格路径（当前首选）────────────────────────────────
    // 导出侧把碰撞体三角形装进均匀网格；运行时按顶点当前位置现查邻域三角形，
    // 约束 =「到表面的带符号距离不许比绑定姿态时更小」。这是唯一一个既与真实
    // 几何一致、又对任意位移都成立的约束形式（静态 K 个三角形会随位移失效，
    // 见 COLLISION_TRIGRID 处的实测数据）。未标记 valid 的顶点不参与碰撞。
    if (COLLISION_TRIGRID_DIMS.w > 0.5)
    {
        // 【用户 m05089】不再按 l0n.w 分流：丝袜段 291 个顶点里 56 个没有碰撞
        // 数据（非 valid），原先它们直接 `return p0 + offset` 完全不受约束，
        // 被拖进大腿内部是必然的。实测这 56 个顶点 signed(p0) 中位 +0.00096、
        // 54/56 在表面外侧 ⇒ 参与碰撞安全（另 2 个已在内侧 -0.009，会被推到表面）。
        // 网格查询是运行时现算的，本来就不依赖烘焙数据。
        return CollisionSolveGrid(i, p0, offset, vn);
    }

    // 仅权重顶点参与碰撞（非权重顶点没有导出阈值，直接放行）
    float4 l0n = ColliderVertexL0[i * 2u + 1u];
    if (l0n.w <= 0.5)
        return p0 + offset;

    float3 p1 = p0 + offset;

    // ── 点云回退路径（三角网格未烘焙时）──────────────────────────────
    // 收集绑定姿态下最近的 K 个碰撞体采样点。每个采样点自带法线（导出侧按
    // 「本连通块质心朝外」统一过）。约束基准直接取带符号距离 —— 不做
    // 「base<0 就翻转法线」的处理：导出侧已统一定向，实测 235 个权重顶点里
    // 235 个采样点法线与 L0 法线同向（平均 dot 0.9281）；翻转反而会让少数
    // 基准为负的采样点变成「允许向内穿」。
    uint pointCount;
    ColliderPoints.GetDimensions(pointCount);
    uint pointN = pointCount / 2u;

    float3 nearQ[COLLISION_NEAR_K];
    float3 nearN[COLLISION_NEAR_K];
    float nearD[COLLISION_NEAR_K];
    float nearLim[COLLISION_NEAR_K];
    float nearSort[COLLISION_NEAR_K];
    uint nearCount = 0u;

    [loop]
    for (uint k = 0u; k < pointN; ++k)
    {
        float3 q = ColliderPoints[k * 2u].xyz;
        float3 n = ColliderPoints[k * 2u + 1u].xyz;
        float nl = length(n);
        if (nl < 1e-6)
            continue;
        n = n / nl;

        float d = distance(p0, q);
        float base = dot(p0 - q, n);   // 不钳到 0：采样点法线有约 23 度偏差，dot 常为负，钳零会让绑定姿态直接违约 -> 顶点被钉死

        uint pos = nearCount;
        if (nearCount >= COLLISION_NEAR_K)
        {
            if (d >= nearSort[COLLISION_NEAR_K - 1u])
                continue;
            pos = COLLISION_NEAR_K - 1u;
        }
        while (pos > 0u && nearSort[pos - 1u] > d)
        {
            nearQ[pos] = nearQ[pos - 1u];
            nearN[pos] = nearN[pos - 1u];
            nearD[pos] = nearD[pos - 1u];
            nearLim[pos] = nearLim[pos - 1u];
            nearSort[pos] = nearSort[pos - 1u];
            --pos;
        }
        nearQ[pos] = q;
        nearN[pos] = n;
        nearD[pos] = max(base, COLLISION_MIN_GAP);
        nearLim[pos] = d + COLLISION_STRETCH;
        nearSort[pos] = d;
        if (nearCount < COLLISION_NEAR_K)
            ++nearCount;
    }

    if (nearCount == 0u)
        return p0 + offset;

    // 快路径：终点已合法
    if (CollisionSatisfied(p1, nearQ, nearN, nearD, nearLim, nearCount))
        return p1;

    // CCD：沿 p0→p1 粗扫，找第一个违约的步（这一步之后都别去）。
    // 只查终点是不够的：位移 0.2~0.5 时顶点可以「穿过」违约区域再落回一个
    // 看似合法的位置（终点判据只测终点，不测路径）。
    float tBad = 1.0;
    [loop]
    for (uint st = 1u; st <= COLLISION_CCD_STEPS; ++st)
    {
        float t = (float)st / (float)COLLISION_CCD_STEPS;
        if (!CollisionSatisfied(p0 + offset * t, nearQ, nearN, nearD, nearLim, nearCount))
        {
            tBad = t;
            break;
        }
    }

    // 在 [0, tBad] 内二分收窄到临界点
    float tLo = 0.0;
    float tHi = tBad;
    [loop]
    for (uint b = 0u; b < 8u; ++b)
    {
        float tMid = 0.5 * (tLo + tHi);
        if (CollisionSatisfied(p0 + offset * tMid, nearQ, nearN, nearD, nearLim, nearCount))
            tLo = tMid;
        else
            tHi = tMid;
    }

    // SOFT：把临界点投影回所有平面半空间的交集（POCS，交替投影）。只裁掉沿
    // 法线向内的分量、保留全部切向 ⇒ 输出仍是 offset 的连续函数（不会像
    // 纯二分那样让相邻顶点各停在各处、把面折出尖角而穿模）。
    if (COLLISION_PARAMS.z <= 0.5)
    {
        float3 ps = p0 + offset * tLo;
        [loop]
        for (uint it = 0u; it < 4u; ++it)
        {
            [loop]
            for (uint j = 0u; j < nearCount; ++j)
            {
                float sj = dot(ps - nearQ[j], nearN[j]);
                if (sj < nearD[j])
                    ps = ps - nearN[j] * (sj - nearD[j]);
            }
        }
        return ps;
    }

    // HARD：位移方向不变、只沿路径缩短，顶点停在违约点之前
    return p0 + offset * tLo;
}


// ============================================================
// MAIN
// ============================================================

[numthreads(256, 1, 1)]
void main(uint3 threadID : SV_DispatchThreadID)
{
    uint i = threadID.x;

    uint vertexCount;
    uint vertexStride;
    rw_buffer.GetDimensions(vertexCount, vertexStride);

    float capturedID = ReadCaptured(DETECT_SLOT_ID, float4(-1.0, 0.0, 0.0, 0.0)).x;
    bool hasCapturedID = capturedID >= 0.0;
    bool mouseHeld = CAPTURED_CURSOR.w > 0.5;

    float4 stateCurrent  = ReadState(0u, float4(0.0, 0.0, 0.0, 0.0));
    float4 stateCenter   = ReadState(2u, float4(0.0, 0.0, 0.0, -1.0));
    bool stateAlive = stateCurrent.w > 0.5;
    float4 sharedCurrent = ReadSharedInteraction(0u, float4(0.0, 0.0, 0.0, 0.0));
    float4 sharedCenter = ReadSharedInteraction(2u, float4(0.0, 0.0, 0.0, -1.0));
    bool sharedAlive = sharedCurrent.w > 0.5 && sharedCenter.w >= 0.0;
    float objectID = sharedAlive ? sharedCenter.w : (stateAlive ? stateCenter.w : capturedID);
    uint activeZone = ClampZoneID(ReadSharedInteraction(9u, float4(0.0, 0.0, 0.0, 0.0)).x);

    bool anyPathSlide = AnyPathSlideZone();
    bool idleFastPath = !sharedAlive
        && !stateAlive
        && !mouseHeld
        && !anyPathSlide;

    if (idleFastPath)
    {
        if (i < vertexCount)
            rw_buffer[i] = base_buffer[i];
        return;
    }

    // Default parameters from IniParams/fallbacks
    float radius       = SafePositive(JIGGLE_PARAMS.x, 0.25);
    float strength     = SafeNonZero(JIGGLE_PARAMS.y, 1.0);
    float falloffPower = SafePositive(JIGGLE_PARAMS.z, 1.5);
    float dragScale    = SafePositive(JIGGLE_PARAMS.w, 1.0);

    float grabDamping    = saturate(SafePositive(PHYS_PARAMS.x, 0.86));
    float grabSpring     = SafePositive(PHYS_PARAMS.y, 0.176);
    float releaseDamping = saturate(SafePositive(PHYS_PARAMS.z, 0.96));
    float releaseSpring  = SafePositive(PHYS_PARAMS.w, 0.055);

    float maxOffset  = SafePositive(POLISH_PARAMS.x, radius * 2.0);
    float releaseKick = SafePositive(POLISH_PARAMS.y, 1.18);
    float targetFollow = saturate(SafePositive(POLISH_PARAMS.w, 0.12));
    float mouseXDirection = SafeNonZero(JIGGLE_MULT_EXTRA.y, 1.0);
    float mouseYDirection = SafeNonZero(POLISH_PARAMS.z, 1.0);

    // Override from ObjParams if found
    uint paramCount = 0;
    ObjParams.GetDimensions(paramCount);
    uint objCount = paramCount / 4u;
    float matchedParamIndex = -1.0;

    for (uint o = 0; o < objCount; ++o)
    {
        float4 r0 = ObjParams[o * 4u + 0u];
        if (abs(r0.x - objectID) < 0.5)
        {
            matchedParamIndex = (float)o;
            radius       = r0.y;
            strength     = r0.z;
            falloffPower = r0.w;

            float4 r1 = ObjParams[o * 4u + 1u];
            dragScale    = r1.x;
            grabDamping  = r1.y;
            grabSpring   = r1.z;
            releaseDamping = r1.w;

            float4 r2 = ObjParams[o * 4u + 2u];
            releaseSpring = r2.x;
            releaseKick  = r2.y;
            maxOffset    = r2.z;
            targetFollow = r2.w;

            float4 r3 = ObjParams[o * 4u + 3u];
            mouseYDirection = r3.x != 0.0 ? r3.x : mouseYDirection;
            mouseXDirection = r3.y != 0.0 ? r3.y : mouseXDirection;
            break;
        }
    }

    // Read global multipliers
    // A selected mask zone owns its falloff radius across all components.
    // Zero preserves legacy per-object/default profiles.
    float zoneRadius = ZoneRadiusOverride(activeZone);
    if (zoneRadius > 0.0)
        radius = zoneRadius;
    float zoneStrength = ZoneStrengthOverride(activeZone);
    if (zoneStrength > 0.0)
        strength = zoneStrength;
    float zoneFalloffPower = ZoneFalloffOverride(activeZone);
    if (zoneFalloffPower > 0.0)
        falloffPower = zoneFalloffPower;

    float mult_radius    = JIGGLE_MULTIPLIERS.y;
    float mult_strength  = JIGGLE_MULTIPLIERS.z;
    float mult_spring    = JIGGLE_MULTIPLIERS.w;
    float mult_damping   = JIGGLE_MULT_EXTRA.x;

    // Fallback to 1.0 if completely undefined/unpopulated (all zero)
    if (mult_radius == 0.0 && mult_strength == 0.0 && mult_spring == 0.0 && mult_damping == 0.0)
    {
        mult_radius = 1.0;
        mult_strength = 1.0;
        mult_spring = 1.0;
        mult_damping = 1.0;
    }

    // Apply multipliers
    radius         *= mult_radius;
    strength       *= mult_strength;
    grabSpring     *= mult_spring;
    releaseSpring  *= mult_spring;
    grabDamping    = saturate(grabDamping * mult_damping);
    releaseDamping = saturate(releaseDamping * mult_damping);
    maxOffset      *= mult_radius;
    float zoneMaxOffset = ZoneOffsetOverride(activeZone);
    if (zoneMaxOffset > 0.0)
        maxOffset = zoneMaxOffset;

    float3 capturedCenterWorld = ReadCaptured(
        DETECT_SLOT_HIT,
        float4(0.0, 0.0, 0.0, 0.0)
    ).xyz;

    float3 capturedNormalWorld = ReadCaptured(
        DETECT_SLOT_NORMAL,
        float4(0.0, 0.0, 1.0, 0.0)
    ).xyz;

    capturedNormalWorld = SafeNormalize(capturedNormalWorld, float3(0.0, 0.0, 1.0));

    float4 capturedRight = ReadCaptured(DETECT_SLOT_SCREEN_RIGHT, float4(1.0, 0.0, 0.0, 0.0));
    float4 capturedDown  = ReadCaptured(DETECT_SLOT_SCREEN_DOWN,  float4(0.0, 1.0, 0.0, 0.0));
    bool screenBasisValid = capturedRight.w > 0.5
        && all(abs(capturedRight.xyz) < float3(1e5, 1e5, 1e5))
        && all(abs(capturedDown.xyz) < float3(1e5, 1e5, 1e5));

    float4 nextCurrent;
    float4 nextPrevious;
    float4 nextCenter;
    float4 nextNormal;
    float4 nextTarget;
    float4 nextPrevTarget;
    float4 nextRawTarget;
    float4 nextScreenRight;
    float4 nextScreenDown;
    float4 nextInputFlags;

    if (sharedAlive)
    {
        nextCurrent = sharedCurrent;
        nextPrevious = ReadSharedInteraction(1u, float4(0.0, 0.0, 0.0, 0.0));
        nextCenter = sharedCenter;
        nextNormal = ReadSharedInteraction(3u, float4(0.0, 0.0, 1.0, 0.0));
        nextTarget = ReadSharedInteraction(4u, float4(0.0, 0.0, 0.0, 0.0));
        nextPrevTarget = ReadSharedInteraction(5u, float4(0.0, 0.0, 0.0, 0.0));
        nextRawTarget = nextTarget;
        nextScreenRight = ReadSharedInteraction(6u, float4(1.0, 0.0, 0.0, 0.0));
        nextScreenDown = ReadSharedInteraction(7u, float4(0.0, 1.0, 0.0, 0.0));
        nextInputFlags = float4(mouseHeld ? 1.0 : 0.0, 0.0, 0.0, 0.0);

        // 【2026-10-13 修复 · 共享态路径下的法线方向拖拽】
        // 共享态（screen_state CS）算出的位移轴 = right*dx + down*dy + n*|d|*depth，
        // 即「屏幕平面方向 + 深度拉」，与鼠标方向绑定；而 ZoneDragAlongNormal 只在
        // ComputeNextPhysics 分支被消费 ⇒ 共享态活跃时（拖拽 + 回弹全程）法线开关被
        // 完全忽略，实机表现为「拖拽始终沿光标方向，开了法线拖拽也没用」。
        // 这里在抓取期间把位移轴重投影到锁定面法线：方向 = 法线，幅度取原位移在屏幕
        // 平面上的投影长度（= 鼠标位移强度，因 right/down 是正交单位基），与 CNP 的
        // BuildFaceNormalDrag 语义一致（位移 = n * |delta|，与鼠标方向无关）。
        // 松手后（mouseHeld == 0）不介入，回弹仍完全由共享态驱动。
        if (mouseHeld && ZoneDragAlongNormal(activeZone))
        {
            float3 nrm = SafeNormalize(nextNormal.xyz, capturedNormalWorld);
            if (all(isfinite(nrm)))
            {
                float mag = length(nextCurrent.xyz);
                if (isfinite(mag) && mag > 1e-8)
                    nextCurrent.xyz = nrm * mag;
            }
        }
    }
    else
    {
        ComputeNextPhysics(
            mouseHeld,
            hasCapturedID,
            capturedID,
            capturedCenterWorld,
            capturedNormalWorld,
            radius,
            strength,
            dragScale,
            grabDamping,
            grabSpring,
            releaseDamping,
            releaseSpring,
            releaseKick,
            maxOffset,
            targetFollow,
            SimulationStep(),
            mouseXDirection,
            mouseYDirection,
            capturedRight.xyz,
            capturedDown.xyz,
            screenBasisValid,
            ZoneDragAlongNormal(activeZone),
            nextCurrent,
            nextPrevious,
            nextCenter,
            nextNormal,
            nextTarget,
            nextPrevTarget,
            nextRawTarget,
            nextScreenRight,
            nextScreenDown,
            nextInputFlags
        );
    }

    // 【2026-10-14 修复 · 开启碰撞时回弹不得越过原位置】
    // 用户要求：在碰撞开启（CollisionSkinActive()）的情况下，松手后的回弹不得让顶点越过原始位置。
    // 机制：共享态/CNP 算出的位移是向量，欠阻尼时它会冲过零点、反向指向原位里侧（=回弹穿模）。
    //       这里以本次抓取面的法线为「向外」参考：位移在法线上的分量一旦为负
    //       （顶点已经跑到原位后面），就丢掉这个顶点的回弹位移 = 停在原位。
    //       只在松手后生效，拖拽期间不介入。
    // 【区域级隔离】不再把共享态 nextCurrent 清零：清零是组件级的，会把同一组件里没有
    //       碰撞体的区域一起钉死在原位（表现为「没碰撞体也回弹卡住、不越界」）。
    //       改为只置判据位，由逐顶点应用处的 l0n.w（导出侧按区域收窄的碰撞参与位，
    //       无碰撞体的区域烘成 0）决定哪些顶点被钉——见 _write_collision_resources。
    float3 reboundOutN = SafeNormalize(nextNormal.xyz, capturedNormalWorld);
    bool reboundBlocked = !mouseHeld && CollisionSkinActive()
        && all(isfinite(reboundOutN)) && dot(nextCurrent.xyz, reboundOutN) < 0.0;
    // Per-component resolved input/output evidence.  This is intentionally
    // tiny and contains no VB0 payload: it proves whether a shared snapshot
    // is valid in this component's local coordinate system.

    // Ouroboros update: one tiny persistent buffer. No ping-pong.
    if (i == 0u)
    {
        // t33 守卫：状态写回前消毒（非有限 ⇒ 保留上一帧状态，再不行 ⇒ 归零；
        // 否则一帧 NaN 会让弹簧永久卡死，表现为「拖不动」）
        float4 t33St[9];
        t33St[0] = nextCurrent; t33St[1] = nextPrevious; t33St[2] = nextCenter;
        t33St[3] = nextNormal;  t33St[4] = nextTarget;   t33St[5] = nextPrevTarget;
        t33St[6] = nextScreenRight; t33St[7] = nextScreenDown; t33St[8] = nextInputFlags;
        [unroll]
        for (uint t33k = 0u; t33k < 9u; ++t33k)
        {
            float4 t33prev = ReadState(t33k, float4(0.0, 0.0, 0.0, 0.0));
            t33St[t33k] = all(isfinite(t33St[t33k]))
                ? t33St[t33k]
                : (all(isfinite(t33prev)) ? t33prev : float4(0.0, 0.0, 0.0, 0.0));
            JiggleState[t33k] = t33St[t33k];
        }
    }

    if (i >= vertexCount)
        return;

    VertexAttributes v = base_buffer[i];
    float3 localPos = ToJiggleSpace(v.position);
    // t33 守卫：输入行本身非有限 ⇒ 整行原样透传（绝不让 NaN 进入物理/碰撞/写回）
    if (!all(isfinite(v.position)) || !all(isfinite(v.normal)))
    {
        rw_buffer[i] = base_buffer[i];
        return;
    }
    uint idCount = 0;
    uint weightCount = 0;
    JiggleZoneIDs.GetDimensions(idCount);
    JiggleZoneWeights.GetDimensions(weightCount);
    uint4 sparseIDs = i < idCount
        ? JiggleZoneIDs[i]
        : uint4(0xffffffffu, 0xffffffffu, 0xffffffffu, 0xffffffffu);
    float4 sparseWeights = i < weightCount ? JiggleZoneWeights[i] : 0.0f;
    float mask = 0.0f;
    [unroll]
    for (uint maskSlot = 0u; maskSlot < 4u; ++maskSlot)
    {
        if (sparseIDs[maskSlot] == activeZone)
            mask = max(mask, sparseWeights[maskSlot]);
    }
    mask = saturate(mask);
    float dist = distance(localPos, nextCenter.xyz);
    // A Path Slide zone never gets spring-physics deformation, even on the
    // frame it happens to be the active zone (it still gets its own rigid
    // offset below, applied independently of activeZone entirely).
    bool activeIsPathSlide = ZoneIsPathSlide(activeZone);
    float influence = (!activeIsPathSlide && nextCurrent.w > 0.5) ? ComputeRubberInfluence(dist, radius, falloffPower) * mask : 0.0;
    // t33 守卫：物理位移先消毒（nextCurrent 携带上一帧状态，NaN 会一路传下去），
    // 再在**源侧**按 maxOffset 硬限幅（captain 线索 4；写回口还有一道同值限幅）
    float3 appliedOffset = influence > 0.0
        ? GuardFinite3(OffsetToVertexSpace(nextCurrent.xyz * influence), float3(0.0, 0.0, 0.0))
        : float3(0.0, 0.0, 0.0);
    appliedOffset = ClampVectorLength(appliedOffset, maxOffset);

    // Path Slide: driven by this vertex's OWN owning zone, not activeZone --
    // so it keeps applying every frame regardless of what else is currently
    // being interacted with (see PathProgressState's persistence comment in
    // rzm_jiggle_screen_state.hlsl). No falloff/radius: every vertex in the
    // zone gets the exact same offset, weighted only by its own paint weight.
    float3 pathOffset = float3(0.0, 0.0, 0.0);
    if (AnyPathSlideZone())
    {
        float ownWeight = 0.0;
        uint ownZone = VertexOwningZone(i, ownWeight);
        if (ownWeight > 1e-4 && ZoneIsPathSlide(ownZone))
        {
            uint pathProgressCount;
            PathProgressState.GetDimensions(pathProgressCount);
            float progress = ownZone < pathProgressCount ? PathProgressState[ownZone] : 0.0;
            float4 pathVec = PathVectors[ownZone];
            if (pathVec.w > 0.5 && progress > 0.0)
                pathOffset = OffsetToVertexSpace(pathVec.xyz * progress) * saturate(ownWeight);
        }
    }
    float sockSlideLen = length(pathOffset);
    if (sockSlideLen > SOCK_STROKE) pathOffset *= SOCK_STROKE / max(sockSlideLen, 1e-6);
    pathOffset = GuardFinite3(pathOffset, float3(0.0, 0.0, 0.0));   // t33 守卫
    bool hasPathOffset = dot(pathOffset, pathOffset) > 1e-12;

    // ===== T16 sock slide: three gates + live weight projection (off = zero impact)
    {
        float sockOwnWeight = 0.0;
        uint sockOwnZone = VertexOwningZone(i, sockOwnWeight);
        bool sockOn = SockSlideEnabled(sockOwnZone);
        if (sockOn && SockHasAnchor(i) && i < SOCK_ROW_END && sockOwnWeight > 1e-4)
        {
            uint sockTri; float3 sockLam;
            if (SockFindAnchor(i, localPos, sockTri, sockLam))
                SockProjectWeights(i, sockTri, sockLam);
            else
                SockSyncRow(i);                 // no anchor: keep previous (freeze)
        }
        else if (sockOn)
        {
            SockSyncRow(i);                     // non-controlled rows: copy through
        }
    }
    // t16 slide (anchor reprojection, t15 §4): walk the anchor along the *skinned*
    // collider patch, keep the standoff = gap, clamp to the effective stroke.
    float sockSlideOwnW = 0.0;
    uint sockSlideOwnZ = VertexOwningZone(i, sockSlideOwnW);
    if (i < SOCK_ROW_END && SockHasAnchor(i) && sockSlideOwnW > 1e-4 && SockSlideEnabled(activeZone))
    {
        uint sTri; float3 sLam;
        if (SockFindAnchor(i, localPos, sTri, sLam))
        {
            float3 sa, sb, sc, sn;
            ReadColliderTri(sTri, sa, sb, sc, sn);
            float3 q1 = sLam.x * sa + sLam.y * sb + sLam.z * sc;
            float3 sd = PathVectors[activeZone].xyz;
            float  sp = min(max(PathProgressState[activeZone], 0.0), 1.0);
            float  sLen = min(length(sd) * sp, SOCK_STROKE);
            float3 td = sd - sn * dot(sd, sn);
            float  tl = length(td);
            if (tl > 1e-6 && sLen > 0.0) q1 += (td / tl) * sLen;
            float  gRow = max(abs(ColliderVertexL0[i * 2u + 0u].w), COLLISION_MIN_GAP);
            // t33 守卫：T16 滑移是硬赋值，先消毒再按 GUARD_MAX_OFFSET 相对 localPos 限幅
            v.position = GuardClampDelta(GuardFinite3(q1 + sn * gRow, localPos), localPos, GUARD_MAX_OFFSET);
        }
    }    // ===== end T16 sock slide ==================================================


    // Debug records are derived values, not a dumped VB0:
    //   3*i+0 = local position + mask
    //   3*i+1 = distance, influence, applied offset length, state-alive
    //   3*i+2 = applied local offset (Jiggle + Path Slide combined)

    // t39：去掉 `nextCurrent.w < 0.5` —— 回弹/归位期不再整波早退；但只要没有位移
    // （totalOffset == 0）就由 collisionDue 挡住求解 ⇒ 静止态逐位不变。
    // 不再提供「保持在阈值 gap 处」的对照分支：实测 gap == 10*t0 时它会把绑定姿态
    // 整体外移 3~17mm，而 gap == t0 时「一步跳回」根本不存在，故不采用。
    if (mask <= 1e-4 && !hasPathOffset)
    {
        rw_buffer[i] = v;
        return;
    }

    if (influence > 0.0)
    {
        // 回弹钉原位只作用于「本顶点确实参与碰撞」的行：没有碰撞体的区域 l0n.w = 0，
        // 弹簧照常越过原位来回晃（该 valid 位由导出侧按区域权重收窄）。
        bool reboundPinned = reboundBlocked && ColliderVertexL0[i * 2u + 1u].w > 0.5;
        if (!reboundPinned)
            v.position += appliedOffset;
    }
    if (hasPathOffset)
    {
        v.position += pathOffset;
    }

    // 碰撞检测（防穿模）：仅在拉扯模式（模式 2）且该顶点确实被移动时。
    // 门控为 wave-uniform（COLLISION_PARAMS.x / COLLISION_META.x 是统一参数），
    // 空闲/非拉扯帧整波跳过，零成本。
    //
    // 【用户 m05100】门控从 `(influence > 0.0 || hasPathOffset)` 改成「只要顶点
    // 有任何位移」：回弹阶段光标一旦离开影响半径，influence 归零，而弹簧仍在
    // 推动顶点 ⇒ 原先这帧不做碰撞 ⇒ 回弹穿模。位移量判据对回弹同样成立。
    float3 totalOffset = v.position - localPos;
    bool collisionDue = dot(totalOffset, totalOffset) > 1e-12;
    if (COLLISION_PARAMS.x > 0.5 && COLLISION_META.x >= 2.0 && collisionDue && COLLISION_SURFACE.x < 0.5)
    {
        float3 vnRaw = v.normal;
        float vnLen = length(vnRaw);
        float3 vnUnit = vnLen > 1e-6 ? vnRaw / vnLen : float3(0.0, 1.0, 0.0);
        vnUnit = GuardFinite3(vnUnit, float3(0.0, 1.0, 0.0));   // t33 守卫
        float3 solved = CollisionSolve(i, localPos, totalOffset, vnUnit);
        solved = GuardFinite3(solved, localPos);                // t33 守卫
        // 最终不变量（见 GuardInwardHalfSpace）：CollisionSolve 的每条出口都要过这一关。
        // 三角网格路径内部已经压过一次（幂等），点云回退路径没有 —— 这里统一兜住，
        // 以后新增求解器也不必记得自己加。
        float4 invL0n = ColliderVertexL0[i * 2u + 1u];
        solved = GuardInwardHalfSpace(localPos, solved, SafeNormalize(invL0n.xyz, vnUnit));
        v.position = GuardClampDelta(solved, localPos, GUARD_MAX_OFFSET);

        // 远侧方向性约束：只压「沿拖拽方向越过碰撞体中面」的位移；
        // Path Slide / Sock Slide 是另一条硬位移路径，不参与，避免互相打架。
        if (!hasPathOffset && !SockSlideEnabled(activeZone))
            v.position = ClampFarSideBarrier(i, v.position, localPos, nextCenter.xyz, nextCurrent.xyz);
    }

    // t33 守卫：唯一写回出口。非有限 ⇒ 回退本帧输入行；位移按 GUARD_MAX_OFFSET 限幅。
    v.position = GuardClampDelta(v.position, localPos, GUARD_MAX_OFFSET);
    if (!all(isfinite(v.position)))
        v = base_buffer[i];
    rw_buffer[i] = v;
}
