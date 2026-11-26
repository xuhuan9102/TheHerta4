Texture1D<float4> IniParams : register(t120);
Texture2D<float4> OpacityMap : register(t69);
SamplerState MaterialSampler : register(s0);

float4 main(float4 position : SV_Position, float4 uv : TEXCOORD0) : SV_Target
{
    // Keep the native vertex shader's SV_Position and TEXCOORD0 register order.
    if (position.w <= 0)
        discard;

    const float4 params = IniParams.Load(int2(69, 0));
    uint width, height;
    OpacityMap.GetDimensions(width, height);
    float opacity = params.x;
    if (width > 0 && height > 0)
    {
        float mask = OpacityMap.Sample(MaterialSampler, uv.xy)[uint(params.y)];
        opacity *= params.z != 0 ? mask : 1 - mask;
    }
    return float4(saturate(opacity), params.w, 0, 1);
}
