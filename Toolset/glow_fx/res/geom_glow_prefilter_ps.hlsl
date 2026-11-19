#include "geom_glow_layers.hlsli"
Texture2D<float2> Coverage : register(t69);
Texture1D<float4> IniParams : register(t120);
Texture2D<float4> Source : register(t64);
Texture2D<float4> BackSource : register(t65);

GlowLayers main(float4 position : SV_Position)
{
    uint width, height;
    Source.GetDimensions(width, height);
    int2 pixel = int2(position.xy);
    float4 parameters = IniParams[224];
    float spacing = 2.0 * max(1.0, ceil(parameters.x / 96.0));
    int halfWidth = int(spacing * 0.5);
    bool vertical = parameters.z != 0.0;
    int coordinate = vertical ? pixel.y : pixel.x;
    int limit = vertical ? int(height) : int(width);
    int first = max(-halfWidth, -coordinate);
    int last = min(halfWidth, limit - 1 - coordinate);

    GlowLayers layers = (GlowLayers)0;
    float nearestDepth = 0.0;
    [loop]
    for (int k = first; k <= last; ++k)
    {
        int2 offset = vertical ? int2(0, k) : int2(k, 0);
        float4 emission = Source.Load(int3(pixel + offset, 0));
        float weight = abs(k) == halfWidth ? 0.5 : 1.0;
        float2 coverage = Coverage.Load(int3(pixel + offset, 0));
        addEmission(layers, nearestDepth, emission, coverage.x, weight);
        if (vertical)
        {
            float4 back = BackSource.Load(int3(pixel + offset, 0));
            addEmission(layers, nearestDepth, back, coverage.y, weight);
        }
    }
    return normalizeLayers(layers, spacing);
}
