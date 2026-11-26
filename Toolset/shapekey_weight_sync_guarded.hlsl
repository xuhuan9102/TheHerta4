// shapekey_weight_sync_guarded.hlsl
//
// Keep captured native ZZMI weights where the exported strength is zero.

#define SHAPEKEY_WEIGHT_SYNC_BASE 100
#define SHAPEKEY_WEIGHT_MIN (-4.0)
#define SHAPEKEY_WEIGHT_MAX (4.0)

RWBuffer<float> ShapeKeyWeight : register(u0);
StructuredBuffer<uint4> NativeMorphLookup : register(t0);
Texture1D<float4> IniParams : register(t120);

bool IsNativeMorphSlot(uint slot)
{
    uint lookupCount;
    uint lookupStride;
    NativeMorphLookup.GetDimensions(lookupCount, lookupStride);
    for (uint index = 0; index < lookupCount; ++index)
    {
        if (NativeMorphLookup[index].w == slot)
        {
            return true;
        }
    }
    return false;
}

[numthreads(64, 1, 1)]
void main(uint3 threadID : SV_DispatchThreadID)
{
    uint i = threadID.x;
    uint count;
    ShapeKeyWeight.GetDimensions(count);
    if (i >= count)
    {
        return;
    }

    float raw = IniParams[SHAPEKEY_WEIGHT_SYNC_BASE + (i >> 2)][i & 3];
    if (isnan(raw))
    {
        raw = 0.0;
    }
    if (raw == 0.0 && IsNativeMorphSlot(i))
    {
        return;
    }
    ShapeKeyWeight[i] = clamp(raw, SHAPEKEY_WEIGHT_MIN, SHAPEKEY_WEIGHT_MAX);
}
