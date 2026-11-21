Texture2D<float4> GlowMap : register(t64);
Texture1D<float4> IniParams : register(t120);
SamplerState MaterialSampler : register(s0);

#include "glow_fx_emission.hlsli"

float4 main(float4 uv_pack : TEXCOORD0) : SV_Target0
{
    // TTL applies its captured opacity when this color is composited.
    float4 glow = GlowMap.Sample(MaterialSampler, uv_pack.xy);
    return float4(GlowEmission(glow, IniParams[224]), 0.0);
}
