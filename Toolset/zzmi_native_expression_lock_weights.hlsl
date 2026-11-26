Buffer<float> NativeWeight : register(t0);
RWBuffer<float> WeightCorrection : register(u0);
Texture1D<float4> IniParams : register(t120);

[numthreads(64, 1, 1)]
void main(uint3 thread : SV_DispatchThreadID)
{
    uint slot = thread.x;
    uint count;
    WeightCorrection.GetDimensions(count);
    if (slot >= count)
        return;

    float encoded = IniParams[100 + (slot >> 2)][slot & 3];
    if (encoded >= 12.0 && encoded <= 20.0)
        WeightCorrection[slot] = (encoded - 16.0) - NativeWeight[slot];
    else
        WeightCorrection[slot] = 0.0;
}
