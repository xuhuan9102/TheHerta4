// Refit a complete binary BVH from the SAME pre-drag VB0 as the jiggle pass.
// x115=enabled, y115=padded triangle count, z115=build roots (0/1).
struct VertexAttributes { float3 position; float3 normal; float4 tangent; };
StructuredBuffer<VertexAttributes> Source : register(t24);
Buffer<uint> TriVertices : register(t87);
Buffer<uint> TriangleOrder : register(t89);
RWBuffer<float4> Bounds : register(u4);
Texture1D<float4> IniParams : register(t120);

groupshared float3 NodeMin[64];
groupshared float3 NodeMax[64];

[numthreads(32, 1, 1)]
void main(uint3 group : SV_GroupID, uint lane : SV_GroupIndex,
          uint3 dispatch : SV_DispatchThreadID)
{
    uint leaves = (uint)IniParams[115].y;
    uint tiles = leaves / 32u;
    if (IniParams[115].z > 0.5)
    {
        // Each upper node reads only completed tile roots. This is a separate
        // dispatch: no cross-group synchronization or atomic publication.
        uint node = dispatch.x + 1u;
        if (node < tiles)
        {
        uint levelBase = 1u << (uint)firstbithigh(node);
        uint span = tiles / levelBase;
        uint begin = tiles + (node - levelBase) * span;
        float3 lo = 1e30, hi = -1e30;
        [loop]
        for (uint k = 0u; k < span; ++k)
        {
            lo = min(lo, Bounds[(begin + k) * 2u].xyz);
            hi = max(hi, Bounds[(begin + k) * 2u + 1u].xyz);
        }
        Bounds[node * 2u] = float4(lo, 0.0);
        Bounds[node * 2u + 1u] = float4(hi, 0.0);
        }
        return;
    }

    uint count;
    TriangleOrder.GetDimensions(count);
    uint slot = group.x * 32u + lane;
    float3 lo = 1e30, hi = -1e30;
    if (slot < count)
    {
        uint tri = TriangleOrder[slot];
        uint vertexCount, stride;
        Source.GetDimensions(vertexCount, stride);
        uint3 ids = uint3(TriVertices[tri * 3u], TriVertices[tri * 3u + 1u], TriVertices[tri * 3u + 2u]);
        if (all(ids < vertexCount))
        {
            float3 a = Source[ids.x].position;
            float3 b = Source[ids.y].position;
            float3 c = Source[ids.z].position;
            lo = min(a, min(b, c));
            hi = max(a, max(b, c));
        }
    }
    NodeMin[32u + lane] = lo;
    NodeMax[32u + lane] = hi;
    Bounds[(leaves + slot) * 2u] = float4(lo, 0.0);
    Bounds[(leaves + slot) * 2u + 1u] = float4(hi, 0.0);
    GroupMemoryBarrierWithGroupSync();

    [loop]
    for (uint width = 16u; width > 0u; width >>= 1u)
    {
        if (lane < width)
        {
            uint local = width + lane;
            lo = min(NodeMin[local * 2u], NodeMin[local * 2u + 1u]);
            hi = max(NodeMax[local * 2u], NodeMax[local * 2u + 1u]);
            NodeMin[local] = lo;
            NodeMax[local] = hi;
            uint node = (tiles + group.x) * width + lane;
            Bounds[node * 2u] = float4(lo, 0.0);
            Bounds[node * 2u + 1u] = float4(hi, 0.0);
        }
        GroupMemoryBarrierWithGroupSync();
    }
}
