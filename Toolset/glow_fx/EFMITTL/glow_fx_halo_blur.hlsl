#include "glow_fx_halo_depth.hlsli"
#include "geom_glow_layers.hlsli"

Texture2D<float4> Near : register(t64);
Texture2D<float4> Far : register(t65);

float HaloWeight(float distance)
{
    return exp2(-4.0 * distance * distance) * (1.0 - smoothstep(0.0, 1.0, distance));
}

GlowLayers main(float4 position : SV_Position)
{
    uint width, height;
    Near.GetDimensions(width, height);
    float4 params = IniParams[181];
    float radius = params.y;
    int step = max(1, (int)ceil(radius / 96.0));
    int extent = (int)(radius / (2 * step));
    float normalization = max(params.z, 1.0);
    GlowLayers result = (GlowLayers)0;
    int2 pixel = int2(position.xy);

    if (params.w < 0.5)
    {
        float nearest = 0.0;
        for (int i = -extent; i <= extent; ++i)
        {
            int coordinate = pixel.x + i * step;
            if (coordinate < 0 || coordinate >= (int)width) continue;
            int3 tap = int3(coordinate, pixel.y, 0);
            float weight = HaloWeight(abs(2 * i * step) / radius);
            addEmission(result, nearest, Near.Load(tap), 0.0, weight);
            addEmission(result, nearest, Far.Load(tap), 0.0, weight);
        }
        return normalizeLayers(result, normalization);
    }

    DepthLayers destination = ReadDepthLayers(int3(pixel, 0));
    int2 halfPixel = min(pixel / 2, int2(width, height) - 1);
    float3 halo = 0.0;
    for (int i = -extent; i <= extent; ++i)
    {
        int coordinate = halfPixel.y + i * step;
        if (coordinate < 0 || coordinate >= (int)height) continue;
        int3 tap = int3(halfPixel.x, coordinate, 0);
        float4 nearGlow = Near.Load(tap);
        float4 farGlow = Far.Load(tap);
        float weight = HaloWeight(abs(2 * i * step) / radius);
        halo += (nearGlow.rgb * HaloTransmission(destination, nearGlow.a)
               + farGlow.rgb * HaloTransmission(destination, farGlow.a)) * weight;
    }
    result.front = float4(halo / normalization, 0.0);
    return result;
}
