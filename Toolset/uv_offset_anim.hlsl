// --- START OF FILE uv_offset_anim.hlsl ---
//
// **** UV OFFSET ANIMATION SHADER - BYTE-STREAM EDITION ****
// Version: 2.0
// Description:
//   The Texcoord vertex stream is treated as a uint array (stride-4 UAV view).
//   UV attributes described by the UV 属性定义 node are offset in place;
//   non-UV attributes (e.g. COLOR) are left untouched.

RWStructuredBuffer<uint> rw_buffer : register(u5);

// t120: INI parameter table.  x100 = offset X, x101 = offset Y.
Texture1D<float4> IniParams : register(t120);

static const uint UV_STREAM_BYTES_PER_VERTEX = 20;
static const uint UV_STREAM_UINTS_PER_VERTEX = 5;

// --- [PYTHON-MANAGED RANGE CHECK START] ---
static const uint UV_OFFSET_RANGE_COUNT = 0;

bool uv_offset_in_range(uint vertex_id)
{
    return false;
}
// --- [PYTHON-MANAGED RANGE CHECK END] ---

[numthreads(16, 1, 1)]
void main(uint3 threadID : SV_DispatchThreadID)
{
    uint i = threadID.x;
    uint vertex_count = rw_buffer.Length / UV_STREAM_UINTS_PER_VERTEX;
    if (i >= vertex_count)
    {
        return;
    }

    if (!uv_offset_in_range(i))
    {
        return;
    }

    float2 uv_offset = float2(IniParams[100].x, IniParams[101].x);
    if (uv_offset.x == 0.0 && uv_offset.y == 0.0)
    {
        return;
    }

    uint base = i * UV_STREAM_UINTS_PER_VERTEX;
    uint data[16];
    // --- [PYTHON-MANAGED LOAD START] ---
    [unroll]
    for (uint k = 0; k < UV_STREAM_UINTS_PER_VERTEX; ++k)
    {
        data[k] = rw_buffer[base + k];
    }
    // --- [PYTHON-MANAGED LOAD END] ---

    // --- [PYTHON-MANAGED APPLY START] ---
    // --- [PYTHON-MANAGED APPLY END] ---

    // --- [PYTHON-MANAGED STORE START] ---
    [unroll]
    for (uint k = 0; k < UV_STREAM_UINTS_PER_VERTEX; ++k)
    {
        rw_buffer[base + k] = data[k];
    }
    // --- [PYTHON-MANAGED STORE END] ---
}
// --- END OF FILE uv_offset_anim.hlsl ---

