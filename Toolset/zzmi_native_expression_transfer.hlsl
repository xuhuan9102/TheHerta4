struct VertexAttributes
{
    float3 position;
    float3 normal;
    float4 tangent;
};

StructuredBuffer<VertexAttributes> NativePosition : register(t105);
StructuredBuffer<VertexAttributes> NativeBasis : register(t106);
StructuredBuffer<uint> SourceVertex : register(t107);
RWStructuredBuffer<VertexAttributes> ExportedPosition : register(u5);

float3 transferDelta(float3 edited, float3 basis, float3 animated)
{
    float3 value = edited + (animated - basis);
    return float3(
        edited.x == basis.x ? animated.x : value.x,
        edited.y == basis.y ? animated.y : value.y,
        edited.z == basis.z ? animated.z : value.z
    );
}

[numthreads(64, 1, 1)]
void main(uint3 thread : SV_DispatchThreadID)
{
    uint row = thread.x;
    if (row >= SourceVertex.Length || row >= ExportedPosition.Length)
        return;

    uint source = SourceVertex[row];
    if (source >= NativePosition.Length || source >= NativeBasis.Length)
        return;

    VertexAttributes edited = ExportedPosition[row];
    VertexAttributes basis = NativeBasis[source];
    VertexAttributes animated = NativePosition[source];
    edited.position = transferDelta(edited.position, basis.position, animated.position);
    edited.normal = transferDelta(edited.normal, basis.normal, animated.normal);
    edited.tangent.xyz = transferDelta(edited.tangent.xyz, basis.tangent.xyz, animated.tangent.xyz);
    ExportedPosition[row] = edited;
}
