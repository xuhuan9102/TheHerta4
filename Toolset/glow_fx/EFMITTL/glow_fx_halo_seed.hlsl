#include "glow_fx_halo_depth.hlsli"
#include "geom_glow_layers.hlsli"

Texture2D<float4> Emission[SURFACES] : register(t90);

GlowLayers main(float4 position : SV_Position)
{
    uint width, height;
    SceneDepth.GetDimensions(width, height);
    uint2 full = uint2(width, height);
    uint2 halfSize = max(full / 2, 1);
    uint2 pixel = uint2(position.xy);
    uint2 first = pixel * 2;
    uint2 end = min(first + 2, full);
    if (pixel.x + 1 == halfSize.x) end.x = full.x;
    if (pixel.y + 1 == halfSize.y) end.y = full.y;

    GlowLayers seed = (GlowLayers)0;
    float nearest = 0.0;
    float group = IniParams[181].x;
    [loop] for (uint y = first.y; y < end.y; ++y)
    {
        [loop] for (uint x = first.x; x < end.x; ++x)
        {
            int3 samplePixel = int3(x, y, 0);
            DepthLayers layers = ReadDepthLayers(samplePixel);
            #define ADD_EMITTER(i) \
                if (layers.opacities[i] > 0.0) { \
                    float4 source = Emission[i].Load(samplePixel); \
                    if (source.a == group && any(source.rgb > 0.0)) { \
                        float weight = layers.opacities[i] * HaloTransmission(layers, layers.depths[i]); \
                        addEmission(seed, nearest, float4(source.rgb, layers.depths[i]), 0.0, weight); \
                    } \
                }
            ADD_EMITTER(0) ADD_EMITTER(1) ADD_EMITTER(2) ADD_EMITTER(3)
            ADD_EMITTER(4) ADD_EMITTER(5) ADD_EMITTER(6) ADD_EMITTER(7)
            #undef ADD_EMITTER
        }
    }
    return normalizeLayers(seed, max((end.x - first.x) * (end.y - first.y), 1));
}
