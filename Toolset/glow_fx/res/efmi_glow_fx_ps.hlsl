Texture2D<float4> GlowMap : register(t64);
Texture1D<float4> IniParams : register(t120);
SamplerState MaterialSampler : register(s0);

#include "glow_fx_emission.hlsli"

// The EFMI forward VS outputs position in o0 and the primary UV in o1.
float4 main(float4 position : SV_Position, float2 uv : TEXCOORD0) : SV_Target0
{
    float4 glow = GlowMap.Sample(MaterialSampler, uv);
    return float4(GlowEmission(glow, IniParams[224]), 0.0);
}
