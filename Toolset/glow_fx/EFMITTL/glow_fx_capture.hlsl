#include "opacity.hlsli"
#include "glow_fx_emission.hlsli"

Texture2D<float4> GlowMap : register(t64);
SamplerState MaterialSampler : register(s0);

float4 main(float4 position : SV_Position, float2 uv : TEXCOORD0) : SV_Target0
{
    float4 glow = GlowMap.Sample(MaterialSampler, uv);
    float4 settings = IniParams[224];
    float3 emission;
    float opacity = 0.0;
    if (settings.w < 0.0)
    {
        // Store straight RGB. EFMITTL multiplies it by the combined opacity.
        opacity = ttl_opacity(uv) * glow.a;
        if (opacity <= 0.0) discard;
        emission = GlowEmission(float4(glow.rgb, 1.0), settings);
    }
    else
        emission = GlowEmission(glow, settings);

    // The sidecar stores the radius group in A. A zero group replaces stale
    // emission when a new surface takes over EFMITTL's shared final layer.
    if (IniParams[181].y > 0.5)
        return IniParams[181].x > 0.0 ? float4(emission, IniParams[181].x) : 0.0;
    return float4(emission, opacity);
}
