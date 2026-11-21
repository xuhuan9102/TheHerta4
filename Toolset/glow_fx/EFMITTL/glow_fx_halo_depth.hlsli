#define SURFACES 8

Texture1D<float4> IniParams : register(t120);
Texture2D<float4> SurfaceColor[SURFACES] : register(t100);
Texture2D<float> SurfaceDepth[SURFACES] : register(t108);
Texture2D<float> SceneDepth : register(t116);

struct DepthLayers
{
    float opaque;
    float depths[SURFACES];
    float opacities[SURFACES];
};

DepthLayers ReadDepthLayers(int3 pixel)
{
    DepthLayers layers = (DepthLayers)0;
    layers.opaque = SceneDepth.Load(pixel);
    uint used = (uint)clamp(IniParams[180].x, 0.0, SURFACES / 2) * 2;
    // SM5 resource arrays need literal indices, even inside the seed's loops.
    #define READ_LAYER(i) \
        if (i < used) { \
            float depth = SurfaceDepth[i].Load(pixel); \
            if (depth > 0.0 && depth >= layers.opaque) { \
                layers.depths[i] = depth; \
                layers.opacities[i] = saturate(SurfaceColor[i].Load(pixel).a); \
            } \
        }
    READ_LAYER(0) READ_LAYER(1) READ_LAYER(2) READ_LAYER(3)
    READ_LAYER(4) READ_LAYER(5) READ_LAYER(6) READ_LAYER(7)
    #undef READ_LAYER
    return layers;
}

float HaloTransmission(DepthLayers layers, float depth)
{
    if (depth <= 0.0 || depth < layers.opaque) return 0.0;
    float through = 1.0;
    [unroll] for (uint i = 0; i < SURFACES; ++i)
        if (layers.depths[i] > depth + 0.0000002)
            through *= 1.0 - layers.opacities[i];
    return through;
}
