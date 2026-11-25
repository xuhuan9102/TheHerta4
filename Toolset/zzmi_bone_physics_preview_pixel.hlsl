struct Vertex
{
    float4 position : SV_Position;
    float4 color : COLOR0;
};

float4 main(Vertex input) : SV_Target
{
    if (input.position.w <= 0.0)
        discard;
    return float4(input.color.rgb * input.color.a, input.color.a);
}
