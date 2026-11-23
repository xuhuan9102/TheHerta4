// Compose with the existing TTL opacity texture for the mask/depth pass.
Texture2D<float> Tear : register(t106);
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
    if (Tear.Load(int3(id.xy, 0)) >= 0.5)
    {
        Combined[id.xy] = invert ? 0.0 : 1.0;
        return;
    }

    Original.GetDimensions(originalSize.x, originalSize.y);
    if (any(originalSize == 0u))
    {
        Combined[id.xy] = invert ? 1.0 : 0.0;
        return;
    }
    float2 source = (float2(id.xy) + 0.5) / size * originalSize - 0.5;
    float2 t = frac(source);
    int2 at = (int2)floor(source);
    int2 maxSize = int2(originalSize);
    float4 a = lerp(LoadWrapped(at, maxSize), LoadWrapped(at + int2(1, 0), maxSize), t.x);
    float4 b = lerp(LoadWrapped(at + int2(0, 1), maxSize), LoadWrapped(at + 1, maxSize), t.x);
    Combined[id.xy] = lerp(a, b, t.y);
}
