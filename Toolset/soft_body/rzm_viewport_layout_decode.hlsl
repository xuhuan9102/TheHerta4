// LLMenu ViewportFrameAPI validator. It consumes the UI placement record and
// publishes the standalone scalar contract Object Detect reads.
Texture2D<float4> LayoutData : register(t0);
RWBuffer<float> ViewportFrameAPI : register(u0);
Texture1D<float4> IniParams : register(t120);
#define EXPECTED_GENERATION IniParams[88].x

[numthreads(1, 1, 1)]
void main(uint3 id : SV_DispatchThreadID)
{
    [unroll] for (uint i = 0u; i < 16u; ++i) ViewportFrameAPI[i] = 0.0;
    uint width, height; LayoutData.GetDimensions(width, height);
    if (width < 8u || height < 1u) return;
    float4 magic = LayoutData.Load(int3(6, 0, 0));
    float4 stamp = LayoutData.Load(int3(7, 0, 0));
    if (magic.x < .5 || stamp.w < .5 || abs(magic.w - EXPECTED_GENERATION) > .25) return;

    float2 uvMin = float2(1e6, 1e6), uvMax = float2(-1e6, -1e6);
    uint count = 0u;
    [unroll] for (uint i = 0u; i < 6u; ++i) {
        float4 p = LayoutData.Load(int3(i, 0, 0));
        if (p.z < .5 || p.x != p.x || p.y != p.y || abs(p.x) > 2.0 || abs(p.y) > 2.0) continue;
        uvMin = min(uvMin, p.xy); uvMax = max(uvMax, p.xy); count++;
    }
    float2 extent = uvMax - uvMin;
    bool valid = count >= 4u && extent.x >= .04 && extent.y >= .04 &&
                 extent.x <= 1.2 && extent.y <= 1.2 && stamp.x >= .85 &&
                 magic.y > 0.0 && magic.z > 0.0 && magic.y <= 8192.0 && magic.z <= 8192.0;
    // 0..3 rect min/max; 4..5 scale; 6 generation; 7 valid; 8 count;
    // 10 similarity; 11..12 source size; 13 UI luma; 14 record flag.
    ViewportFrameAPI[0u] = uvMin.x; ViewportFrameAPI[1u] = uvMin.y;
    ViewportFrameAPI[2u] = uvMax.x; ViewportFrameAPI[3u] = uvMax.y;
    ViewportFrameAPI[4u] = extent.x; ViewportFrameAPI[5u] = extent.y;
    ViewportFrameAPI[6u] = magic.w; ViewportFrameAPI[7u] = valid ? 1.0 : 0.0;
    ViewportFrameAPI[8u] = (float)count; ViewportFrameAPI[9u] = -1.0;
    ViewportFrameAPI[10u] = stamp.x; ViewportFrameAPI[11u] = magic.y;
    ViewportFrameAPI[12u] = magic.z; ViewportFrameAPI[13u] = stamp.z;
    ViewportFrameAPI[14u] = stamp.w; ViewportFrameAPI[15u] = magic.x;
}
