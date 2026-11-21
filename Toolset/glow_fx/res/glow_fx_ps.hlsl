Texture2D<float4> GlowMap : register(t64);
Texture2D<float> SceneDepth : register(t67);
Texture2D<float4> TTLMask : register(t68);
Texture1D<float4> IniParams : register(t120);
SamplerState MaterialSampler : register(s0);
cbuffer Material : register(b4) { float4 material[151]; };

#include "glow_fx_emission.hlsli"

float4 sampleCollapsedSide(float2 uv)
{
    float4 sampleColor = GlowMap.SampleLevel(MaterialSampler, uv, 0.0);
    if (sampleColor.a >= 0.99)
        return sampleColor;

    uint width, height;
    GlowMap.GetDimensions(width, height);
    int2 limit = int2(width, height) - 1;
    float2 location = uv * float2(width, height) - 0.5;
    int2 center = int2(round(location));
    float nearestDistance = 4.0;
    [unroll]
    for (int y = -2; y <= 2; ++y)
    {
        [unroll]
        for (int x = -2; x <= 2; ++x)
        {
            int2 pixel = center + int2(x, y);
            if (any(pixel < 0) || any(pixel > limit))
                continue;

            float2 offset = float2(pixel) - location;
            float distanceSquared = dot(offset, offset);
            if (distanceSquared >= nearestDistance)
                continue;

            float4 candidate = GlowMap.Load(int3(pixel, 0));
            if (candidate.a >= 0.99)
            {
                sampleColor = candidate;
                nearestDistance = distanceSquared;
            }
        }
    }
    return sampleColor;
}

float TTLOpacity(float2 uv)
{
    float4 settings = IniParams[225];
    uint width, height;
    TTLMask.GetDimensions(width, height);
    float mask = 0;
    if (width && height)
    {
        float4 value = TTLMask.Sample(MaterialSampler, uv);
        mask = value[(uint)clamp(settings.y, 0.0, 3.0)];
        if (settings.z != 0.0) mask = 1.0 - mask;
    }
    float threshold = settings.w > 0.0 ? settings.w : 0.999;
    return mask >= threshold ? 0.0 : (1.0 - saturate(mask)) * saturate(settings.x);
}

float4 main(float2 uv : TEXCOORD0, float4 position : SV_Position, bool front : SV_IsFrontFace,
            out float2 coverage : SV_Target1) : SV_Target0
{
    float4 parameters = IniParams[224];
    float ttlMode = IniParams[226].x;
    float opacity = 1.0;
    if (ttlMode != 0.0)
    {
        opacity = TTLOpacity(uv);
        clip(opacity - 0.0001);
    }
    float sceneDepth = SceneDepth.Load(int3(int2(position.xy), 0));
    float depthGap = position.z - sceneDepth;
    float2 texcoord = uv;
    float2 dx = ddx(texcoord);
    float2 dy = ddy(texcoord);
    float determinant = dx.x * dy.y - dx.y * dy.x;
    bool collapsedUV = determinant * determinant <= 0.000025 * dot(dx, dx) * dot(dy, dy);
    float4 glow;
    if (parameters.w < 0.0)
    {
        // A palette must not pick different mip colors on adjacent faces.
        glow = collapsedUV ? sampleCollapsedSide(texcoord)
                           : GlowMap.SampleLevel(MaterialSampler, texcoord, 0.0);
    }
    else
    {
        glow = GlowMap.SampleGrad(MaterialSampler, texcoord, dx, dy);
    }
    // TTL and emission-only draws have no matching native depth write.
    clip(0.0000002 + (ttlMode != 0.0 || parameters.w < 0.0 ? depthGap : -abs(depthGap)));
    clip(glow.a - 0.0001);
    float3 emission = GlowEmission(glow, parameters) * opacity;
    clip(max(emission.r, max(emission.g, emission.b)) - 0.0001);
    coverage = float2(ttlMode == 1.0 ? 0.0 : glow.a * opacity, 0.0);
    return float4(emission, position.z);
}
