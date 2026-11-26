struct VertexAttributes
{
    float3 position;
    float3 normal;
    float4 tangent;
};

StructuredBuffer<VertexAttributes> NativePosition : register(t105);
StructuredBuffer<VertexAttributes> NativeBasis : register(t106);
StructuredBuffer<uint> SourceVertex : register(t107);
StructuredBuffer<VertexAttributes> StaticPosition : register(t108);
RWBuffer<float> ExportedPosition : register(u5);
#ifdef SSMT_NATIVE_EXPRESSION_LOCKS
struct LockedDelta
{
    uint slot;
    float3 position;
    float3 normal;
    float3 tangent;
};

Buffer<float> WeightCorrection : register(t109);
StructuredBuffer<LockedDelta> LockedChannelDelta : register(t110);
StructuredBuffer<uint> LockedVertexOffsets : register(t111);
#endif

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
    uint outputFloats;
    ExportedPosition.GetDimensions(outputFloats);
    if (row >= SourceVertex.Length || row >= StaticPosition.Length || row >= outputFloats / 10)
        return;

    VertexAttributes edited = StaticPosition[row];
    uint source = SourceVertex[row];
    if (source < NativePosition.Length && source < NativeBasis.Length)
    {
        VertexAttributes basis = NativeBasis[source];
        VertexAttributes animated = NativePosition[source];
        edited.position = transferDelta(edited.position, basis.position, animated.position);
        edited.normal = transferDelta(edited.normal, basis.normal, animated.normal);
        edited.tangent.xyz = transferDelta(edited.tangent.xyz, basis.tangent.xyz, animated.tangent.xyz);
#ifdef SSMT_NATIVE_EXPRESSION_LOCKS
        uint end = LockedVertexOffsets[source + 1];
        for (uint index = LockedVertexOffsets[source]; index < end; ++index)
        {
            LockedDelta delta = LockedChannelDelta[index];
            float weight = WeightCorrection[delta.slot];
            if (weight != 0.0)
            {
                edited.position += weight * delta.position;
                edited.normal += weight * delta.normal;
                edited.tangent.xyz += weight * delta.tangent;
            }
        }
#endif
    }
    uint start = row * 10;
    ExportedPosition[start] = edited.position.x;
    ExportedPosition[start + 1] = edited.position.y;
    ExportedPosition[start + 2] = edited.position.z;
    ExportedPosition[start + 3] = edited.normal.x;
    ExportedPosition[start + 4] = edited.normal.y;
    ExportedPosition[start + 5] = edited.normal.z;
    ExportedPosition[start + 6] = edited.tangent.x;
    ExportedPosition[start + 7] = edited.tangent.y;
    ExportedPosition[start + 8] = edited.tangent.z;
    ExportedPosition[start + 9] = edited.tangent.w;
}
