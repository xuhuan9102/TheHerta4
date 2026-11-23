// Copy the native ZZMI morph weight into the mod ShapeKeyWeight buffer.

#define ZZMI_NATIVE_MORPH_LOOKUP_COUNT_REGISTER 94
#define ZZMI_NATIVE_MORPH_SOURCE_FILTER_REGISTER 95
#define SHAPEKEY_WEIGHT_MIN (-4.0)
#define SHAPEKEY_WEIGHT_MAX (4.0)

cbuffer ZZMINativeMorphCB0 : register(b0)
{
    uint SourceOffset;
    uint RecordCount;
    float NativeWeight;
    uint Unused;
};

StructuredBuffer<uint4> NativeMorphLookup : register(t0);
Texture1D<float4> IniParams : register(t120);
RWBuffer<float> ShapeKeyWeight : register(u0);

[numthreads(64, 1, 1)]
void main(uint3 threadID : SV_DispatchThreadID)
{
    uint i = threadID.x;
    uint lookupCount = (uint)max(
        IniParams[ZZMI_NATIVE_MORPH_LOOKUP_COUNT_REGISTER].x,
        0.0
    );
    if (i >= lookupCount)
    {
        return;
    }

    uint sourceFilter = (uint)max(
        IniParams[ZZMI_NATIVE_MORPH_SOURCE_FILTER_REGISTER].x,
        0.0
    );
    uint4 entry = NativeMorphLookup[i];
    if (entry.x != sourceFilter ||
        entry.y != SourceOffset ||
        entry.z != RecordCount)
    {
        return;
    }

    float weight = NativeWeight;
    if (isnan(weight))
    {
        weight = 0.0;
    }
    ShapeKeyWeight[entry.w] = clamp(
        weight,
        SHAPEKEY_WEIGHT_MIN,
        SHAPEKEY_WEIGHT_MAX
    );
}
