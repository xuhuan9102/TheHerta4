// sb_solve.hlsl — 软体物理 M1 求解器（Verlet + 邻接长度约束）
// TheHerta4 测试版 / 2026-09-13
//
// 这是"皮肤感"的来源：顶点之间第一次有了**网**（邻接表），不再是一堆互不相干的
// 弹簧点（对比 Toolset/drag_interaction/rzm_jiggle_interaction.hlsl 的
// "single-buffer Null-spring jiggle"）。
//
// ── 数据布局（导出侧 softbody_bake.py 产出）─────────────────────────────
//   t24  绑定姿态顶点缓冲，stride 40（前 12 字节 = xyz），只读
//   t80  邻接表：每顶点 1 条 uint4 = (邻居数, 边表起始下标, 0, 0)
//   t81  边表：每条边 1 条 uint4 = (邻居顶点号, asuint(静止长度), 0, 0)
//   u5   输出顶点缓冲（SW 换绑给 vb0），stride 40
//   u6   持久状态：**按出现次分槽**（照抄合并骨架的 s1/s2 做法）——
//        每顶点每槽 3 条 float4：[槽基 + 0] = 基座 + 初始化位，
//        [槽基 + 1] = 当前位置，[槽基 + 2] = 上一步位置，
//        槽基 = (顶点号 * 2 + 槽) * 3。
//        为什么必须分槽：同一个部件的 deform 阶段**一帧会跑不止一次**
//        （合并骨架为此准备了 s1/s2 两个槽位）。单一状态会让第二次 deform
//        读到第一次刚写过的数据，只有一部分顶点被更新 —— 实机表现"拉面筋"。
//        （基座必须存在状态里：这样"就地写回 mod 的 Position 缓冲"才不会逐帧漂移）
//   t120 IniParams
//
// ── 参数（IniParams）───────────────────────────────────────────────────
//   [80].x  静止姿态拉力（把顶点拉回绑定姿态的刚度，0=完全自由，1=钉死）
//   [80].y  邻接约束刚度（边长约束强度，越大越"弹"）
//   [80].z  速度阻尼（每步保留多少速度，0.90~0.99 常见）
//   [80].w  步长（固定 dt，一般 1.0/60.0；越大越飘）
//   [81].xyz 重力方向（世界空间，通常 (0,-1,0)）—— M2 会用 cb1 换到物空间
//   [81].w  重力强度
//   [82].x  内部迭代次数（每个 dispatch 内对邻居扫几遍，1~4）
//
// ── 为什么这么设计 ─────────────────────────────────────────────────────
// * 顶点之间只有"邻居"，没有拓扑之外的假设 —— 网格是三角面还是四边面都无所谓。
// * 状态存"位置 + 上一步位置"而不是"速度"：Verlet 的更新天然稳定，
//   不需要额外积分器，也不会因为帧率抖动爆掉。
// * 约束是**逐顶点各自朝邻居靠一半**（Gauss-Seidel 风格）：不需要原子操作、
//   不需要同步，天然并行；代价是收敛比全局求解慢，靠迭代次数补。
// * 所有读写都是顺序的（邻接表按顶点分组、组内升序），GPU 上零随机寻址。

// ⚠️ 结构体大小必须**正好等于** ini 里声明的 stride（40）。
// 2026-09-14 修复：最初写成 3 条 float4 = 48 字节，而缓冲 stride = 40 ⇒
// 第 i 个元素被写到字节偏移 i*40 却占 48 字节，元素互相覆盖 →
// 实机现象"身体爆炸碎一地"。改成 3+3+4 个 float = 40 字节后与视图严格一致。
// （HLSL 结构化缓冲里的 float3 占 12 字节，不需要 16 字节对齐。）
struct VertexAttributes
{
    float3 pos;        // 偏移  0，12 字节
    float3 nrm;        // 偏移 12，12 字节
    float4 tan;        // 偏移 24，16 字节  → 合计 40
};

// 基座几何：**格式化缓冲**（format = R32_FLOAT，每顶点 10 个 float = 40 字节）。
// 2026-09-14 方案 A：不再用 StructuredBuffer<struct> 去猜步长/布局 ——
// 之前两种读法都实测失败过：
//   * `cs-t24 = ref <mod 自己的文件资源>` 当 StructuredBuffer → 读出全 0（网格塌成一点）
//   * 改成 StructureBuffer 前用 vb0 就地改写 → 第二次 deform 抄到被污染的基座（面筋）
// 现在接口完全显式：第 k 个 float = base_data[vid * 10 + k]。
Buffer<float> base_data : register(t24);
// ★ 二分第 3 步（2026-09-15）：最小版（只有 t24/u5/u6）能写，本段多出的正是 t80/t81 的
// 声明与使用。整体停用它们，看写回是否恢复 —— 恢复则元凶就是这两份邻接表。
// Buffer<uint4>                   adj_table    : register(t80);
// Buffer<uint4>                   edge_table   : register(t81);
RWStructuredBuffer<VertexAttributes> out_buffer : register(u5);
RWStructuredBuffer<float4>           sb_state   : register(u6);

Texture1D<float4> IniParams : register(t120);

// ⚠⚠ 本 fork 的 IniParams **整体偏移一行**（mod 自己的 attach 着色器里写着"y1 在
//   IniParams[1].y，标准版是 [0]，本 fork 从 [1] 起"）⇒ ini 的 `xNN` 要读 [NN+1]。
//   这一行之差就是"当年顶点版表现为飞走/躺下"的根源：它拿到的是别人槽里的值。
#define SB_PARAM_ROW 1
#define SB_SHAPE_STIFFNESS IniParams[80 + SB_PARAM_ROW].x
#define SB_EDGE_STIFFNESS  IniParams[80 + SB_PARAM_ROW].y
#define SB_DAMPING         IniParams[80 + SB_PARAM_ROW].z
#define SB_DT              IniParams[80 + SB_PARAM_ROW].w
#define SB_GRAVITY_DIR     IniParams[81 + SB_PARAM_ROW].xyz
#define SB_GRAVITY_STRENGTH IniParams[81 + SB_PARAM_ROW].w
#define SB_ITERATIONS      IniParams[82 + SB_PARAM_ROW].x
// 诊断开关（2026-09-14）：>0.5 时不写解算结果，改写"基础顶点 + 固定偏移"。
// 用来把"解算没跑起来"和"解算跑了但数据是垃圾"一刀切开：
//   * 模型整体平移 → 解算在跑，而且 base_buffer 也读对了 ⇒ 问题在解算数学
//   * 模型碎一地   → 解算在跑，但读到/写出的是垃圾 ⇒ 问题在绑定或布局
//   * 毫无变化     → 解算根本没跑（着色器没编译/没被调用）
#define SB_DEBUG_MARKER    IniParams[83 + SB_PARAM_ROW].x
// 本帧用哪个出现次槽（0 / 1）。由 ini 按出现次切换 —— 与合并骨架的
// `$zz_ms_occ_*`（1↔2）对应，着色器侧只认 0/1 两个槽。
#define SB_SLOT            IniParams[84 + SB_PARAM_ROW].x
// 诊断标记的平移距离（2026-09-14 加）：原来写死 3.0，实机直接把身体顶出视野，
// 没法判断"是干净的整体平移"还是"顶点只更新了一部分"。改成参数后可现场调小，
// 0.3 足以一眼看出位移、又不会跑出镜头；缺省（=0）仍按老的 3.0 处理。
#define SB_MARKER_DIST     IniParams[85 + SB_PARAM_ROW].x
// 偏移上限（2026-09-14 加，硬保险）：一个部件一帧会被 6 个 pass 各画一次，
// 解算挂在绘制段上 ⇒ 一帧会被调用 6 次（步长按 1/6 折算到 w80）。
// 如果任何原因让状态被推到离谱的地方（脏状态、参数爆掉），**必须**把它拉回
// 绑定姿态附近 —— 否则整个部件直接飞出视野，什么也看不见、也没法诊断。
//   SB_MAX_OFFSET  单个顶点相对"活体位置"的最大允许偏移（活体空间单位）。
//                  活体网格整体比绑定姿态大约 4 倍（实测 bbox 5.4 vs 1.31），
//                  所以 0.6 约等于"体高的 11%"，够看见、又不会穿模飞走。
#define SB_MAX_OFFSET      0.05
// 重力方向选择（2026-09-14）：绑定姿态空间到底哪个轴朝下，**不能靠猜**——
// 实测把 -Z 和 -Y 都试过，画面上看起来都往侧后方躺。这里改成由 ini 的 [Key] 段
// 循环切换 6 个轴向（IniParams[86].x = 0..5），用户在游戏里按一下键就能逐个试，
// 一眼选出"往下坠"的那个。
//   0:(0,0,-1)  1:(0,0,1)  2:(0,-1,0)  3:(0,1,0)  4:(-1,0,0)  5:(1,0,0)
#define SB_DIR_IDX         IniParams[86 + SB_PARAM_ROW].x
// 本帧是否"推进物理"：ini 只在每帧第一个 pass 置 1，其余 pass 置 0（只复用）。
// 没有这一步，6 个 pass 会把偏移叠加 6 次 —— 实机表现就是"模型被撕扯"。
#define SB_ADVANCE         IniParams[88 + SB_PARAM_ROW].x
// 惯性增益（2026-09-14）：让"肉跟不上骨头"。这一帧活体顶点自己动了多少
// （live_pos - 上一帧 live_pos），就在偏移上反向补多少 ⇒ 角色一动就有滞后/回弹，
// 这才是"软"真正能被看见的地方（纯重力只会让整体平移）。
#define SB_INERTIA         IniParams[87 + SB_PARAM_ROW].x
// 重力强度倍率（2026-09-14）：ini 的 [Key] 段用 F6 循环切换，方便现场调手感。
// 约定：**0 或未设置 = 按 1.0 处理**，所以 ini 不需要（也不应该）每帧写它。
#define SB_GRAVITY_SCALE   IniParams[89 + SB_PARAM_ROW].x
// 总开关（2026-09-14）：F4 切换，`>= 0.5` = **关**（着色器直接透传活体顶点，
// 等于软体这层不存在）。用途：一键 A/B 判断画面问题是"软体造成的"还是"mod 自己的"。
// 缺省（未设置 = 0）= 开。
#define SB_OFF             IniParams[90 + SB_PARAM_ROW].x

float3 sb_gravity_dir()
{
    int idx = (int)round(SB_DIR_IDX);
    // 2026-09-14 实测标定（正面视角，F7 逐个试出来的）：idx 2 = `(0,-1,0)`
    // 才是"游戏世界向下"。现在把它排到 **0 = 默认**，这样不按键就是对的。
    if (idx <= 0) return float3(0.0, -1.0, 0.0);
    if (idx == 1) return float3(0.0, 1.0, 0.0);
    if (idx == 2) return float3(0.0, 0.0, -1.0);
    if (idx == 3) return float3(0.0, 0.0, 1.0);
    if (idx == 4) return float3(-1.0, 0.0, 0.0);
    return float3(1.0, 0.0, 0.0);
}

[numthreads(64, 1, 1)]
void main(uint3 tid : SV_DispatchThreadID)
{
    uint vid = tid.x;

    // 2026-09-14 编译错误修复：`Buffer<uint4>`（格式化缓冲）的 GetDimensions
    // **只能有一个参数**（out uint width）。写成两个参数会直接编译失败：
    //   error X3013: 'GetDimensions': no matching 2 parameter intrinsic method
    // 后果极隐蔽：3Dmigoto 把编译失败的 shader 丢掉 → `run =` 静默无效 →
    // 画面毫无变化（用户实测四轮"没变/碎一地"的最终根因就是这一行）。
    // 二分第 3 步：邻接表声明停用后，用顶点缓冲自身的长度做边界（等价保护）
    uint vertex_count = 0;
    uint vertex_stride = 0;
    out_buffer.GetDimensions(vertex_count, vertex_stride);
    if (vid >= vertex_count)
    {
        return;
    }

    // ══════════════════════════════════════════════════════════════════════
    // 2026-09-14 改版（本次最关键的一处）：解算作用在**活体顶点**上，
    // 不再作用在静态绑定姿态上。
    //
    // 为什么：实测这条真正出图的绘制路径里，vb0 是 **ZZMI 蒙皮算出来的网格**
    // （同一份资源还同时绑在 vb3 上），顶点着色器**不会再蒙第二遍皮**。
    // 旧版把"静态绑定姿态 + 物理位置"塞进 vb0，等于把没蒙皮的网格喂给一个
    // 不再蒙皮的管线 —— 它只能靠对象矩阵摆着，于是整块躺在角色旁边/侧面。
    //
    // 新口径：
    //   * 活体位置 live_pos = 本帧进来时的顶点（角色的姿势/动画都在里面）
    //   * 物理量 = 相对活体位置的**偏移场**（跨帧惯性 + 重力 + 回弹 + 邻接聚合）
    //   * 输出   = live_pos + 偏移
    // 三个好处：① 不再需要静态基座（"躺在一旁"的根因消失）；
    //           ② 角色一动，偏移场就有惯性 —— 这才是能看见的"软"；
    //           ③ 重力方向在活体空间里是**同一个方向**，不会被各骨骼拧散。
    // ══════════════════════════════════════════════════════════════════════
    uint slot = (uint)clamp((int)SB_SLOT, 0, 1);
    uint state_base = (vid * 2 + slot) * 3;

    // 2026-09-15 幂等写回（最终版）：输出 = **文件里的绑定姿态** + 本帧偏移。
    // 这样写进去的内容是"纯函数"，不依赖任何跨帧还原 ⇒ 天然幂等、不会累积、不会抽搐。
    // （上一版用"读到的内容减去上次偏移"来还原，一旦缓冲被游戏/蒙皮管线重算过，
    //   减掉的偏移就不存在了 ⇒ 一减一加 ⇒ 实测"抽搐"。）
    VertexAttributes live = out_buffer[vid];
    float3 raw_pos = live.pos.xyz;
    float3 base_pos = float3(base_data[vid * 10 + 0],
                             base_data[vid * 10 + 1],
                             base_data[vid * 10 + 2]);

    float marker_dist = SB_MARKER_DIST;
    if (marker_dist == 0.0)
    {
        marker_dist = 3.0;   // 兼容老行为（参数没填时）
    }

    // 2026-09-15 二分：**暂时停用 F4 透传分支**（怀疑它把一切都吃掉了）。
    // 若这次出现位移，则问题就是"SB_OFF 被别的东西置 1"。
    // if (SB_OFF >= 0.5) { out_buffer[vid] = live; return; }

    // 跨帧状态：cur/prev 现在存的是**偏移量**，w 恰好等于 1.0 才算"有效"。
    // （旧版本这里存绝对位置，量级完全不同 ⇒ 会被判无效、自动从零重建，
    //   不需要用户重启游戏。NaN/inf/超限同样判无效。）
    float4 live_slot = sb_state[state_base + 0];
    float4 cur_slot = sb_state[state_base + 1];
    float4 prev_slot = sb_state[state_base + 2];

    // 上次写进缓冲的偏移（用于还原原始位置）
    float3 applied_off = float3(0.0, 0.0, 0.0);
    if ((live_slot.w == 1.0) && !any(isnan(live_slot.xyz)) &&
        (length(live_slot.xyz) <= SB_MAX_OFFSET))
    {
        applied_off = live_slot.xyz;
    }
    float3 live_pos = base_pos;                // ← 永远从文件取基座（纯函数）

    // ★ 关键：一个部件一帧会被 6 个 pass（主/描边/阴影…）各画一次，而我们把结果
    //   **写回同一个缓冲**（vb0 别名 → copy cs-u5 → 写回）。于是第二个 pass 再读
    //   "活体位置"时读到的已经是第一个 pass 改过的值 ⇒ 偏移被叠加 6 次 ⇒ 实机
    //   "模型被撕扯"。ini 只在每帧**第一个** pass 把 x88 置 1；其余 pass 走下面
    //   这条"只复用、不推进、不写状态"的分支（输出与第一个 pass 完全一致）。
    if (SB_ADVANCE <= 0.5)
    {
        // 2026-09-15 二分：**暂时停用"非推进 pass 透传"分支**，改为继续往下算并写出
        // （标记模式下输出与 pass 无关，可安全地每个调用都写）。
    }

    // 下面这一段**每帧只跑一次**（本帧第一个 pass）：此刻读到的 live_pos 还是干净的
    float3 off = float3(0.0, 0.0, 0.0);
    float3 prev_off = float3(0.0, 0.0, 0.0);
    bool state_ok =
        (cur_slot.w == 1.0) && (prev_slot.w == 1.0) &&
        !any(isnan(cur_slot.xyz)) && !any(isinf(cur_slot.xyz)) &&
        !any(isnan(prev_slot.xyz)) && !any(isinf(prev_slot.xyz)) &&
        (length(cur_slot.xyz) <= SB_MAX_OFFSET) &&
        (length(prev_slot.xyz) <= SB_MAX_OFFSET);
    if (state_ok)
    {
        off = cur_slot.xyz;
        prev_off = prev_slot.xyz;
    }

    // 上一帧这个顶点自己在哪里（用来算"活体速度"）—— 只在推进那一次有意义
    float3 prev_live = live_pos;
    if ((live_slot.w == 1.0) && !any(isnan(live_slot.xyz)) &&
        !any(isinf(live_slot.xyz)))
    {
        prev_live = live_slot.xyz;
    }

    // 惯性 + 重力 + 回弹（把偏移拉回 0 = 跟随活体），Verlet 一步
    float dt = max(SB_DT, 1e-4);
    float3 next_off = off + (off - prev_off) * SB_DAMPING;
    float gscale = SB_GRAVITY_SCALE;
    if (gscale <= 0.0)
    {
        gscale = 1.0;   // 没设置 = 1 倍
    }
    next_off += sb_gravity_dir() * SB_GRAVITY_STRENGTH * gscale * dt * dt;
    next_off -= (live_pos - prev_live) * SB_INERTIA;   // 跟不上骨头 = 滞后
    next_off -= off * SB_SHAPE_STIFFNESS;

    // 邻接聚合：邻居的偏移互相"拉平"，皮下就有整体感。
    // 注意这里**不需要静止长度** —— 偏移场里"邻居彼此一致"本身就是凝聚力，
    // 而且天然免疫网格缩放/蒙皮变形（旧版按绝对长度约束，换个空间就全错）。
    // 二分第 3 步：邻接聚合暂时停用（待确认它就是"写回失效"的元凶后再装回）
    // uint4 adj = adj_table[vid];
    // uint count = adj.x;
    // uint start = adj.y;
    // uint passes = max((int)SB_ITERATIONS, 1);
    // for (uint iteration = 0; iteration < passes; ++iteration)
    // {
    //     for (uint k = 0; k < count; ++k)
    //     {
    //         uint other = edge_table[start + k].x;
    //         uint other_base_index = (other * 2 + slot) * 3;
    //         float4 other_cur = sb_state[other_base_index + 1];
    //         float3 other_off = float3(0.0, 0.0, 0.0);
    //         if ((other_cur.w == 1.0) && !any(isnan(other_cur.xyz)) &&
    //             (length(other_cur.xyz) <= SB_MAX_OFFSET))
    //         {
    //             other_off = other_cur.xyz;
    //         }
    //         next_off += (other_off - next_off) * 0.5 * SB_EDGE_STIFFNESS;
    //     }
    // }

    // 硬上限：偏移永远不超过 SB_MAX_OFFSET ⇒ 最坏情况是"看不出动"，不会飞走
    float off_len = length(next_off);
    if (off_len > SB_MAX_OFFSET)
    {
        next_off *= SB_MAX_OFFSET / off_len;
    }

    sb_state[state_base + 1] = float4(next_off, 1.0);
    sb_state[state_base + 2] = float4(off, 1.0);
    sb_state[state_base + 0] = float4(next_off, 1.0);   // 本次真正写进缓冲的偏移（供下一帧还原）

    VertexAttributes outp = live;
    if (SB_DEBUG_MARKER > 0.5)
    {
        // 诊断标记：整体平移（活体空间单位）
        outp.pos.xyz = live_pos + float3(0.0, 0.0, marker_dist);
    }
    else
    {
        outp.pos.xyz = live_pos + next_off;
    }
    out_buffer[vid] = outp;
    // 自报（2026-09-15）：把"这一次到底往缓冲里写了多少位移"编码进 vid=0 的 tangent.w。
    // 读法：tangent.w = 5000 + |outp.pos - raw_pos| × 1000。
    if (vid == 0)
    {
        VertexAttributes rep = outp;
        rep.tan.w = 5000.0 + length(outp.pos.xyz - raw_pos) * 1000.0;
        out_buffer[vid] = rep;
    }
}
