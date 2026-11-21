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

float3 GlowEmission(float4 glow, float4 parameters)
{
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

    return color * glow.a * brightness;
}
