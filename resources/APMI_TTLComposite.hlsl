#ifdef VERTEX_SHADER
float4 main(uint vertex : SV_VertexID) : SV_Position
{
    float2 position = float2((vertex << 1) & 2, vertex & 2);
    return float4(position * float2(2, -2) + float2(-1, 1), 0, 1);
}
#endif

#ifdef PIXEL_SHADER
Texture2D<float3> LayerColor : register(t69);
Texture2D<float2> LayerMask : register(t70);
Texture2D<float> LayerDepth : register(t71);
Texture2D<float> SceneDepth : register(t72);

float4 main(float4 position : SV_Position) : SV_Target
{
    int3 pixel = int3(int2(position.xy), 0);
    float layerDepth = LayerDepth.Load(pixel);
    float sceneDepth = SceneDepth.Load(pixel);
    float2 mask = LayerMask.Load(pixel);
    if (mask.x <= 0 || layerDepth <= 0 || layerDepth + 1e-6 < sceneDepth)
        discard;
    return float4(LayerColor.Load(pixel) * mask.y, mask.x);
}
#endif
