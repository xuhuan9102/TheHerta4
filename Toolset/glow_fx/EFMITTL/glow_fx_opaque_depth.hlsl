Texture1D<float4> IniParams : register(t120);
Texture2D<float4> SurfaceColor[8] : register(t100);
Texture2D<float> SurfaceDepth[8] : register(t108);

// Publish opaque surfaces to the native depth buffer for later game draws.
float main(float4 position : SV_Position) : SV_Depth
{
    int3 pixel = int3(position.xy, 0);
    uint used = (uint)clamp(IniParams[180].x, 0.0, 4.0) * 2;
    float nearest = 0.0;
    [unroll] for (uint i = 0; i < 8; ++i)
        if (i < used && SurfaceColor[i].Load(pixel).a >= 1.0)
            nearest = max(nearest, SurfaceDepth[i].Load(pixel));
    if (nearest <= 0.0) discard;
    return nearest;
}
