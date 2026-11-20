Texture2D<float4> GlowMap : register(t64);
Texture2D<float> SceneDepth : register(t67);
Texture1D<float4> IniParams : register(t120);
SamplerState MaterialSampler : register(s0);
cbuffer Material : register(b4) { float4 material[151]; };

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

float4 main(float4 position : SV_Position, float2 uv : TEXCOORD0, bool front : SV_IsFrontFace,
            out float2 coverage : SV_Target1) : SV_Target0
{
    float4 parameters = IniParams[224];
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
    // A negative strength selects geometry without a native color/depth draw.
    clip(0.0000002 + (parameters.w < 0.0 ? depthGap : -abs(depthGap)));
    clip(glow.a - 0.0001);
    float3 shift = parameters.xyz;
    float brightness = abs(parameters.w);
    float3 hsv = rgb_to_hsv(glow.rgb);
    hsv.y = saturate(hsv.y + shift.y);
    hsv.z = saturate(hsv.z + shift.z);

    // Keep the emission luminance stable as the hue rotates.
    const float3 luminance_weights = float3(0.2126f, 0.7152f, 0.0722f);
    float base_luminance = dot(hsv_to_rgb(hsv), luminance_weights);
    hsv.x = frac(hsv.x + shift.x + 1.0f);
    float3 color = hsv_to_rgb(hsv);
    float color_luminance = dot(color, luminance_weights);
    color *= base_luminance / max(color_luminance, 1.0e-10f);

    // Keep luminance constant while fitting the rotated chroma to the
    // original value. Saturated blue must not create an HDR channel spike.
    float3 chroma = color - base_luminance.xxx;
    float positive_chroma = max(chroma.r, max(chroma.g, chroma.b));
    float peak_limit = max(hsv.z, base_luminance);
    float chroma_scale = min(1.0, (peak_limit - base_luminance) / max(positive_chroma, 1.0e-10f));
    color = base_luminance.xxx + chroma * chroma_scale;

    float3 emission = color * glow.a * brightness;
    clip(max(emission.r, max(emission.g, emission.b)) - 0.0001);
    coverage = float2(glow.a, 0.0);
    return float4(emission, position.z);
}
