// Compose with the existing TTL opacity texture for the mask/depth pass.
Texture2D<uint> Tear : register(t106);
Texture2D<float4> Original : register(t107);
Texture1D<float4> IniParams : register(t120);
RWTexture2D<float4> Combined : register(u0);

float4 LoadWrapped(int2 at, int2 size)
{
    at = int2(at.x < 0 ? size.x - 1 : at.x, at.y < 0 ? size.y - 1 : at.y);
    at = int2(at.x == size.x ? 0 : at.x, at.y == size.y ? 0 : at.y);
    return Original.Load(int3(at, 0));
}

[numthreads(8, 8, 1)]
void main(uint3 id : SV_DispatchThreadID)
{
    uint2 size, originalSize;
    Combined.GetDimensions(size.x, size.y);
    if (any(id.xy >= size))
        return;
    bool invert = IniParams[494].z != 0.0;
    Original.GetDimensions(originalSize.x, originalSize.y);
    float4 original = invert ? 1.0 : 0.0;
    if (all(originalSize != 0u))
    {
        float2 source = (float2(id.xy) + 0.5) / size * originalSize - 0.5;
        float2 t = frac(source);
        int2 at = (int2)floor(source);
        int2 maxSize = int2(originalSize);
        float4 a = lerp(LoadWrapped(at, maxSize), LoadWrapped(at + int2(1, 0), maxSize), t.x);
        float4 b = lerp(LoadWrapped(at + int2(0, 1), maxSize), LoadWrapped(at + 1, maxSize), t.x);
        original = lerp(a, b, t.y);
    }

    uint packed = Tear.Load(int3(id.xy, 0));
    if (packed == 0u)
    {
        Combined[id.xy] = original;
        return;
    }
    float4 field = float4((packed >> uint4(0, 8, 16, 24)) & 255u) / 255.0;
    float4 intact = invert ? original : 1.0 - original;
    float4 coverage = intact * max((1.0 - field.x) * (1.0 - field.y), field.z);
    float4 piled = (1.0 - pow(1.0 - saturate(intact), 1.60)) * (1.0 - field.x);
    coverage = lerp(coverage, max(coverage, piled), field.w);
    Combined[id.xy] = invert ? coverage : 1.0 - coverage;
}
