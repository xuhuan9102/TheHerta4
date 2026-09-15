// rzm_pin_detected.hlsl
// RZMenu: Pins the closest hovered object ID detected during the frame,
//         copies extended hit metadata, and resets the per-frame accumulator.
//
// Expected bindings:
//   cs-u0 = ResourceRZMDetectID,         RWBuffer<float4> (frame accumulator)
//   cs-u1 = ResourceRZMPinnedDetectID,   RWStructuredBuffer<float>  (legacy pinned R32_FLOAT)
//   cs-u2 = ResourceRZMPinnedDetectInfo, RWStructuredBuffer<float4> (pinned extended info)
//   cs-u3 = ResourceRZMZoneOut,          RWBuffer<float> (stable zone ID scalar, R32_FLOAT)
//
// Accumulator/pinned info layout:
//   [0] legacy ABI, do not reorder:
//       x = best hit ID (-1 on miss)
//       y = best depth
//       z = firstIndex of winning range
//       w = hit triangle count
//   [1] xyz = hit point on object, world space; w = object mode
//   [2] x = firstIndex; y = absolute indexBase; z = local triangle; w = face ID
//   [3] x/y/z = absolute vertex indices; w = nearest vertex slot
//   [4] xyz = barycentric hit weights; w = inside-triangle flag
//   [5] xyz = geometric face normal, world space; w = screen winding sign
//   [6] xyz = nearest vertex position, world space; w = screen distance squared
//   [7] x = layout version; y = object index; z = object count; w = stable zone ID for mode 7
//   [14] x = reconstructed clip depth; y/z = clip w/z; w = valid
//
// After running:
//   ResourceRZMPinnedDetectID[0]   = best.x (used by INI store -> $Detected)
//   ResourceRZMPinnedDetectInfo[*] = copied extended info, or miss/reset payload
//   ResourceRZMDetectID[*]         = reset for next frame

// [14] clip-depth payload: x = 1-z/w, y = clip w, z = clip z, w = valid.
#define RZM_DETECT_SLOTS 15u

RWBuffer<float4> gAccumulated : register(u0);
RWStructuredBuffer<float>  gPinnedID    : register(u1);
RWStructuredBuffer<float4> gPinnedInfo  : register(u2);
RWBuffer<float>            gZoneOut     : register(u3);
Texture1D<float4> IniParams   : register(t120);

#define CURSOR_PARAMS IniParams[24]

static const float kHugeDepth = 3.402823e+38f;
static const float4 kResetSlot0 = float4(-1.0f, kHugeDepth, 0.0f, 0.0f);
static const float4 kResetSlotN = float4(0.0f, 0.0f, 0.0f, 0.0f);

[numthreads(1, 1, 1)]
void main(uint3 dispatchThreadID : SV_DispatchThreadID)
{
    float4 best    = gAccumulated[0];
    bool   invalid = best.x < 0.0f || best.y > 1e30f;

    gPinnedID[0] = invalid ? -1.0f : best.x;

    // 区域直出：slot 7 = (layout, objectIndex, objectCount, stable zone)。
    // 在下方 loop 重置 gAccumulated 之前读出 .w，双路写出：
    //   gPinnedID[1] —— 与命中 ID 同资源（RWStructuredBuffer<float> stride 4）的相邻槽，
    //                     INI 侧 store 与命中 ID 完全同型同寻址，回读最可靠；
    //   gZoneOut[0]  —— 独立 R32_FLOAT 标量槽（RWBuffer），与点击计数回读同型。
    // 历史问题：zone 曾从 stride-16 StructuredBuffer 按 float 标量索引 31 回读，
    // 实测该跨结构寻址不可靠（zone 恒 -1），故改为 R32 标量双路直出。
    gPinnedID[1] = invalid ? -1.0f : gAccumulated[7].w;
    gZoneOut[0]  = invalid ? -1.0f : gAccumulated[7].w;

    [unroll]
    for (uint slot = 0u; slot < RZM_DETECT_SLOTS; slot++)
    {
        float4 value = invalid ? (slot == 0u ? kResetSlot0 : kResetSlotN) : (slot == 10u ? CURSOR_PARAMS : gAccumulated[slot]);
        gPinnedInfo[slot] = value;
        gAccumulated[slot] = slot == 0u ? kResetSlot0 : kResetSlotN;
    }
}
