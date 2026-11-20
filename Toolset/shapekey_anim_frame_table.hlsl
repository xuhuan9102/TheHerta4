// --- START OF FILE shapekey_anim_frame_table.hlsl ---
//
// **** FRAME TABLE SHAPE KEY ANIMATION ****
//
// 背景：旧模型对「每个顶点」遍历全部槽位（num_slots 次），每次都要读
// vertex_freq_indices / merged_shapekey_indices，并对一个数百 MB 的
// delta 缓冲做随机访问。序列模式（前缀累积语义）下同一时刻有几十个键
// 权重同为 1，无法提前退出，于是每帧成本正比于 顶点数 × 槽位数。
//
// 本模板把每个分组编译成「逐帧位移表」：
//   - 帧表第 m 条 = 组内前 m 个形态键的累积位移（相对基础网格，不含 base），
//     与「第 m 帧姿态 − base」逐位等价（形态键是线性混合，前缀和可无损预计算）。
//   - Only frames 1 through key_count are stored; frame 0 is implicit zero.
//   - 各通道连续半精度打包，每个 uint32 存两个分量。
//   - 每组一张顶点局部索引表（-1 = 该顶点不属于此组），按组累加。
// 于是每顶点只需 2 次顺序读取/组，取代 num_slots 次遍历。
//
// 同步模式的单键即 key_count = 1 的序列组（帧表 [0, delta]，进度 = 该键强度），
// 因此所有键都能统一走本路径，无需双份逻辑。
//
// 进度允许为负（也允许暂时 > 1）：帧表是线性可插值的，越界就沿首/末段线性外推。
// 单键组的进度就是该键权重，`$Freq_x = -0.4` 因此得到 -0.4 倍位移（反向）。
// 硬边界 ±4 由权重同步 CS 保证，本文件另加一层帧数上限（见 frame_table_sample）。

struct VertexAttributes {
    float3 position;
    float3 normal;
    float4 tangent;
};

RWStructuredBuffer<VertexAttributes> rw_buffer : register(u5);

// 序列组逐帧位移表（半精度打包）。
StructuredBuffer<uint> frame_table : register(t51);

Texture1D<float4> IniParams : register(t120);

// --- [PYTHON-MANAGED BLOCK START] ---
// 组映射表 / 权重缓冲的声明与 #define 由导出器注入。
// --- [PYTHON-MANAGED BLOCK END] ---

// 三个连续且以 float3 对齐的分量最多读两个 word。
float3 frame_table_load_float3(uint rec)
{
    uint even_base = (rec >> 1u) * 3u;
    uint w1 = frame_table[even_base + 1u];
    if ((rec & 1u) == 0u)
    {
        uint w0 = frame_table[even_base];
        return float3(
            f16tof32(w0 & 0xFFFFu),
            f16tof32(w0 >> 16u),
            f16tof32(w1 & 0xFFFFu));
    }
    uint w2 = frame_table[even_base + 2u];
    return float3(
        f16tof32(w1 >> 16u),
        f16tof32(w2 & 0xFFFFu),
        f16tof32(w2 >> 16u));
}

float frame_table_load_component(uint component)
{
    uint word = frame_table[component >> 1u];
    return f16tof32((word >> ((component & 1u) * 16u)) & 0xFFFFu);
}

// --- [PYTHON-MANAGED DELTA START] ---
// 增量结构与读写顺序由导出器按顶点通道生成。
// --- [PYTHON-MANAGED DELTA END] ---

// 越界外推的帧数上限：单键组（key_count = 1）时正好等于权重硬边界 ±4，
// 多键组时最多沿首/末段外推 4 帧 —— 界外是线性外推，不设上限的话进度被写成
// 3~4 就能把 30 键的组推到一百多帧之外，模型直接炸开。
#define FRAME_TABLE_EXTRAPOLATION_FRAMES (4.0)

// 组内按进度 t 取插值位移：帧 K 与 K+1 之间线性插值。
// key_count is the number of stored frames, excluding the implicit zero frame.
// local_index = 该顶点在组顶点并集中的局部下标（>= 0 才调用）。
//
// t 不夹到 0..1：负 t 沿首段反向、t > 1 沿末段正向线性外推。单键组的 t 就是
// 该键权重，夹掉符号会让 `$Freq_x = -0.4` 完全不动（旧版 saturate 的行为）。
FrameTableDelta frame_table_sample(uint group_offset, uint union_size, uint key_count, uint local_index, float t)
{
    FrameTableDelta result = (FrameTableDelta)0;
    if (key_count != 0u)
    {
        if (t != t)
        {
            t = 0.0;
        }
        float scaled = clamp(t * (float)key_count,
                             -FRAME_TABLE_EXTRAPOLATION_FRAMES,
                             (float)key_count + FRAME_TABLE_EXTRAPOLATION_FRAMES);

        // 首段和末段共用同一插值式，分别以 frac < 0 和 frac > 1 外推。
        uint frame_index = scaled < 0.0 ? 0u : min((uint)floor(scaled), key_count - 1u);
        float frac = scaled - (float)frame_index;
        uint first_record = group_offset + local_index;
        if (frame_index != 0u)
        {
            result = frame_table_load(first_record + (frame_index - 1u) * union_size);
        }
        if (frac != 0.0)
        {
            FrameTableDelta next_frame = frame_table_load(first_record + frame_index * union_size);
            result = frame_table_lerp(result, next_frame, frac);
        }
    }
    return result;
}

[numthreads(16, 1, 1)]
void main(uint3 threadID : SV_DispatchThreadID)
{
    uint i = threadID.x;
    uint vertex_count = rw_buffer.Length;
    if (i >= vertex_count)
    {
        return;
    }

    VertexAttributes output = rw_buffer[i];
    FrameTableDelta total_diff = (FrameTableDelta)0;

    // --- [PYTHON-MANAGED LOGIC START] ---
    // 各分组逐帧插值累加，由导出器注入。
    // --- [PYTHON-MANAGED LOGIC END] ---

    // --- [PYTHON-MANAGED WRITEBACK START] ---
    // --- [PYTHON-MANAGED WRITEBACK END] ---
    rw_buffer[i] = output;
}
// --- END OF FILE shapekey_anim_frame_table.hlsl ---
