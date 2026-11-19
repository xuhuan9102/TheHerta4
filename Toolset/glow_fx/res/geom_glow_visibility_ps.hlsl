Texture2D<float2> Coverage : register(t69);
Texture2D<float4> Mask : register(t64);

float4 main(float4 position : SV_Position, out float depth : SV_Depth,
            out float2 coverage : SV_Target1) : SV_Target0
{
    float4 glow = Mask.Load(int3(int2(position.xy), 0));
    clip(max(glow.r, max(glow.g, glow.b)) - 0.0001);
    depth = glow.a;
    coverage = Coverage.Load(int3(int2(position.xy), 0));
    return glow;
}