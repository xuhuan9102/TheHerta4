// Clear only the ShapeKeyWeight slots sourced from native ZZMI morph weights.

StructuredBuffer<uint4> NativeMorphLookup : register(t0);
RWBuffer<float> ShapeKeyWeight : register(u0);

[numthreads(64, 1, 1)]
void main(uint3 threadID : SV_DispatchThreadID)
{
    uint lookupCount;
    uint lookupStride;
    NativeMorphLookup.GetDimensions(lookupCount, lookupStride);

    uint i = threadID.x;
    if (i >= lookupCount)
    {
        return;
    }

    ShapeKeyWeight[NativeMorphLookup[i].w] = 0.0;
}
