Texture2D<float2> HaloCoverage : register(t69);
Texture2D<float2> CoreCoverage : register(t70);
Texture1D<float4> IniParams : register(t120);
Texture2D<float> SceneDepth : register(t67);
Texture2D<float4> Mask : register(t64);
Texture2D<float4> Horizontal : register(t65);
Texture2D<float4> BackHorizontal : register(t68);
Texture2D<float4> Scene : register(t66);

float occlusionWeight(float destinationDepth, float emitterDepth)
{
    // Reverse-Z: a larger destination depth is closer to the camera.
    float relativeGap = (destinationDepth - emitterDepth) / max(emitterDepth, 0.000001);
    return 1.0 - smoothstep(0.004, 0.02, relativeGap);
}


float3 highlightColor(float3 emission)
{
    // Whitening changes chroma while preserving the emitter's luminance.
    float luminance = dot(emission, float3(0.2126, 0.7152, 0.0722));
    return lerp(emission, luminance.xxx, smoothstep(0.1063, 0.8504, luminance));
}

float4 main(float4 position : SV_Position) : SV_Target
{
    uint width, height;
    Mask.GetDimensions(width, height);
    int2 limit = int2(width, height) - 1;
    int2 pixel = clamp(int2(position.xy), 0, limit);
    float destinationDepth = SceneDepth.Load(int3(pixel, 0));
    float4 haloParameters = IniParams[224];
    float radius = haloParameters.x;
    float spacing = 2.0 * max(1.0, ceil(radius / 96.0));
    int extent = int(radius / spacing);
    float3 halo = 0;
    float coverage = 0.0;
    [loop]
    for (int y = -extent; radius > 0 && y <= extent; ++y)
    {
        float offset = y * spacing;
        float distance = abs(offset) / max(radius, 1.0);
        float weight = exp2(-4.0 * distance * distance) * (1.0 - smoothstep(0.0, 1.0, distance));
        int coordinate = pixel.y + int(offset);
        if (coordinate < 0 || coordinate >= int(height))
            continue;
        int2 tap = int2(pixel.x, coordinate);
        float4 row = Horizontal.Load(int3(tap, 0));
        float4 back = BackHorizontal.Load(int3(tap, 0));
        float2 visibility = float2(occlusionWeight(destinationDepth, row.a),
                                  occlusionWeight(destinationDepth, back.a));
        halo += weight * (row.rgb * visibility.x + back.rgb * visibility.y);
        coverage += weight * dot(visibility, HaloCoverage.Load(int3(tap, 0)));
    }
    float4 scene = Scene.Load(int3(int2(position.xy), 0));
    float3 core = Mask.Load(int3(pixel, 0)).rgb;
    float coreCoverage = saturate(CoreCoverage.Load(int3(pixel, 0)).x);
    // The horizontal prefilter is already normalized. Normalize the vertical
    // sum once more so sparse halo fringes cannot turn into HDR spikes.
    float haloNormalization = max(haloParameters.y, 1.0);
    float3 haloEmission = halo / haloNormalization;
    float haloCoverage = saturate(coverage / haloNormalization);
    float3 background = scene.rgb + highlightColor(haloEmission);
    float3 surface = highlightColor(core / max(coreCoverage, 0.000001));
    float alpha = scene.a;
    if (haloParameters.w > 0.5)
    {
        // This scene composites the character layer over a separate background.
        float glowCoverage = max(haloCoverage, coreCoverage);
        alpha = alpha + (1.0 - alpha) * glowCoverage;
    }
    return float4(lerp(background, surface, coreCoverage), alpha);
}
