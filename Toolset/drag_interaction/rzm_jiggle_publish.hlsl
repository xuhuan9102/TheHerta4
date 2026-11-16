// Publish solved positions into the vertex-compatible buffer. Do not use a
// structured-to-VB CopyResource: some loaders leave the original data intact.
struct VertexAttributes {
    float3 position;
    float3 normal;
    float4 tangent;
};
StructuredBuffer<VertexAttributes> Solved : register(t24);
RWBuffer<float> Output : register(u0);
Texture1D<float4> IniParams : register(t120);

[numthreads(256, 1, 1)]
void main(uint3 id : SV_DispatchThreadID)
{
    uint count, stride, words;
    Solved.GetDimensions(count, stride);
    Output.GetDimensions(words);
    uint i = id.x, offset = (uint)IniParams[115].x;
    if (i >= count || i >= (uint)IniParams[115].y || stride % 4 != 0) return;
    uint at = i * (stride / 4) + offset;
    if (at + 2 >= words) return;
    float3 p = Solved[i].position;
    Output[at] = p.x;
    Output[at + 1] = p.y;
    Output[at + 2] = p.z;
}
