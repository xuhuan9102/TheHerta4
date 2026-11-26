#include "constants.hlsli"
#include "common.hlsli"
Buffer<uint> Representatives : register(t85);
Buffer<float4> State : register(t77);
Buffer<float4> Edges : register(t113);
RWStructuredBuffer<Particle> Particles : register(u0);
RWBuffer<float4> Health : register(u1);

groupshared uint ScaleBits;
groupshared uint BiggestEdge;
groupshared uint BiggestMove;

float3 NativePosition(uint i) { return Source[Representatives[i]].position; }

[numthreads(64, 1, 1)]
void main(uint lane : SV_GroupIndex)
{
    uint count, stride, nativeCount, nativeStride, representatives, edges;
    Particles.GetDimensions(count, stride);
    Source.GetDimensions(nativeCount, nativeStride);
    Representatives.GetDimensions(representatives);
    Edges.GetDimensions(edges);
    if (count != representatives || stride != 32 || nativeStride != VERTEX_STRIDE || nativeCount == 0 || State[23].w < 0.5)
        return;

    float progress = max(State[0].x, State[0].y);
    if (progress < 0.00001)
    {
        if (lane == 0) Health[0] = float4(1, 0, 0, progress);
        return;
    }

    if (lane == 0)
    {
        ScaleBits = asuint(1.0);
        BiggestEdge = 0;
        BiggestMove = 0;
    }
    GroupMemoryBarrierWithGroupSync();

    float common = 1.0;
    float poseScale = max(State[8].z, 0.001);
    float limitMotion = (max(Config[6].w * 1.5, 0.018) + max(1 - Config[4].x, 0) * Config[6].w) * poseScale;
    for (uint i = lane; i < count; i += 64)
    {
        if (Representatives[i] >= nativeCount) { common = 0; continue; }
        float3 offset = Particles[i].current.xyz - NativePosition(i);
        float dist = length(offset);
        if (!isfinite(dist)) { common = 0; continue; }
        if (dist > limitMotion) common = min(common, limitMotion / max(dist, 0.000001));
        InterlockedMax(BiggestMove, asuint(dist));
    }
    for (uint i = lane; i < edges; i += 64)
    {
        float4 edge = Edges[i];
        uint a = asuint(edge.x), b = asuint(edge.y);
        if (edge.w > 0.5 || a >= count || b >= count || Representatives[a] >= nativeCount || Representatives[b] >= nativeCount)
            continue;
        float3 base = NativePosition(a) - NativePosition(b);
        float3 solved = Particles[a].current.xyz - Particles[b].current.xyz;
        float original = length(base);
        float result = length(solved);
        if (!isfinite(original) || !isfinite(result)) { common = 0; continue; }
        InterlockedMax(BiggestEdge, asuint(result / max(original, 0.000001)));
        float bound = max(original * 2.1, original + 0.003 * poseScale);
        if (result <= bound) continue;
        float3 change = solved - base;
        float aa = dot(change, change);
        float bb = dot(base, change);
        float cc = dot(base, base) - bound * bound;
        float limit = aa > 1e-15 ? (-bb + sqrt(max(bb * bb - aa * cc, 0))) / aa : 0;
        common = min(common, saturate(limit));
    }
    InterlockedMin(ScaleBits, asuint(common));
    GroupMemoryBarrierWithGroupSync();
    float blend = asfloat(ScaleBits);
    if (blend < 0.99999)
    {
        for (uint i = lane; i < count; i += 64)
        {
            if (Representatives[i] >= nativeCount) continue;
            Particle p = Particles[i];
            float3 base = NativePosition(i);
            p.current.xyz = blend <= 0 ? base : lerp(base, p.current.xyz, blend);
            p.previous.xyz = p.current.xyz;
            Particles[i] = p;
        }
    }
    if (lane == 0) Health[0] = float4(blend, asfloat(BiggestEdge), asfloat(BiggestMove), progress);
}
