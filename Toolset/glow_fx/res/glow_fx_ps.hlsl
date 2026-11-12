Texture2D<float4> GlowMap : register(t64);
Texture1D<float4> IniParams : register(t120);

cbuffer CB4 : register(b4)
{
    float4 cb4[151];
};

struct PSInput
{
    float4 uv : TEXCOORD0;
    bool is_front_face : SV_IsFrontFace;
};

float3 rgb_to_hsv(float3 color)
{
    float4 k = float4(0.0f, -1.0f / 3.0f, 2.0f / 3.0f, -1.0f);
    float4 p = lerp(float4(color.bg, k.wz), float4(color.gb, k.xy), step(color.b, color.g));
    float4 q = lerp(float4(p.xyw, color.r), float4(color.r, p.yzx), step(p.x, color.r));
    float delta = q.x - min(q.w, q.y);
    float epsilon = 1.0e-10f;
    return float3(abs(q.z + (q.w - q.y) / (6.0f * delta + epsilon)), delta / (q.x + epsilon), q.x);
}

float3 hsv_to_rgb(float3 color)
{
    float3 p = abs(frac(color.xxx + float3(1.0f, 2.0f / 3.0f, 1.0f / 3.0f)) * 6.0f - 3.0f);
    return color.z * lerp(float3(1.0f, 1.0f, 1.0f), saturate(p - 1.0f), color.y);
}

void main(
    PSInput input,
    out float4 target0 : SV_Target0,
    out float4 target1 : SV_Target1,
    out float4 target2 : SV_Target2,
    out float4 target3 : SV_Target3)
{
    uint width = 0;
    uint height = 0;
    GlowMap.GetDimensions(width, height);

    // TEXCOORD0 contains the main UV in xy and the alternate UV in zw.
    // Load at the exact texel to avoid filtered seams along mesh boundaries.
    float2 uv = input.uv.xy;
    bool use_alternate_uv =
        (!input.is_front_face && cb4[150].x > 0.5f)
        || (input.uv.z > 1.0f && cb4[150].y > 0.5f);
    if (use_alternate_uv)
    {
        uv = input.uv.zw;
    }

    int2 texel = (int2)floor(uv * float2(width, height));
    texel = clamp(texel, int2(0, 0), int2(width - 1, height - 1));
    float4 glow = GlowMap.Load(int3(texel, 0));
    float3 shift = IniParams[224].xyz;
    float brightness = max(IniParams[224].w, 0.0f);

    float3 hsv = rgb_to_hsv(glow.rgb);
    hsv.x = frac(hsv.x + shift.x + 1.0f);
    hsv.y = saturate(hsv.y + shift.y);
    hsv.z = saturate(hsv.z + shift.z);
    float3 color = hsv_to_rgb(hsv);

    target0 = float4(color * glow.a * brightness, 0.0f);
    target1 = 0.0f;
    target2 = 0.0f;
    target3 = 0.0f;
}
