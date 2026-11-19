#include "geom_glow_layers.hlsli"
Texture2D<float2> Coverage : register(t69);
Texture1D<float4> IniParams : register(t120);
Texture2D<float4> Mask : register(t64);
Texture2D<float4> BackMask : register(t65);

GlowLayers main(float4 position : SV_Position)
{
    uint width, height;
    Mask.GetDimensions(width, height);
    int2 pixel = int2(position.xy);
    float4 haloParameters = IniParams[224];
    float radius = haloParameters.x;
    float spacing = 2.0 * max(1.0, ceil(radius / 96.0));
    int extent = int(radius / spacing);
    GlowLayers layers = (GlowLayers)0;
    float nearestDepth = 0.0;
    [loop]
    for (int x = -extent; x <= extent; ++x)
    {
        float offset = x * spacing;
        float distance = abs(offset) / max(radius, 1.0);
        float weight = exp2(-4.0 * distance * distance) * (1.0 - smoothstep(0.0, 1.0, distance));
        int coordinate = pixel.x + int(offset);
        if (coordinate < 0 || coordinate >= int(width))
            continue;
        int2 tap = int2(coordinate, pixel.y);
        float4 emission = Mask.Load(int3(tap, 0));
        float2 coverage = Coverage.Load(int3(tap, 0));
        addEmission(layers, nearestDepth, emission, coverage.x, weight);
        float4 back = BackMask.Load(int3(tap, 0));
        addEmission(layers, nearestDepth, back, coverage.y, weight);
    }
    return normalizeLayers(layers, haloParameters.y);
}
