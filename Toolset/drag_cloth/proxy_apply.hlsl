#include "constants.hlsli"

StructuredBuffer<Vertex> Native : register(t24);
Buffer<uint> ProxyRows : register(t28);
Buffer<float4> Track : register(t29);
Buffer<float4> State : register(t77);
Buffer<float4> PaletteDecision : register(t96);
Buffer<float4> Skeleton1 : register(t2);
Buffer<float4> Skeleton2 : register(t3);
RWBuffer<float> Output : register(u0);

void Sample(uint sampleIndex, uint row, uint count, bool second, out float3 position, out float3 normal)
{
    uint at = (sampleIndex * count + row) * 4;
    float4 bindPosition = float4(Track[at].xyz, 1);
    float3 direction = Track[at + 1].xyz;
    uint4 indices = asuint(Track[at + 2]);
    float4 weights = Track[at + 3];
    position = 0;
    normal = 0;
    [unroll] for (uint j = 0; j < 4; ++j)
    {
        uint index = indices[j] * 3;
        float4 x = second ? Skeleton2[index] : Skeleton1[index];
        float4 y = second ? Skeleton2[index + 1] : Skeleton1[index + 1];
        float4 z = second ? Skeleton2[index + 2] : Skeleton1[index + 2];
        position += weights[j] * float3(dot(x, bindPosition), dot(y, bindPosition), dot(z, bindPosition));
        normal += weights[j] * float3(dot(x.xyz, direction), dot(y.xyz, direction), dot(z.xyz, direction));
    }
}

[numthreads(128, 1, 1)]
void main(uint3 id : SV_DispatchThreadID)
{
    uint count, nativeCount, stride, trackCount, outputCount;
    ProxyRows.GetDimensions(count);
    Native.GetDimensions(nativeCount, stride);
    Track.GetDimensions(trackCount);
    Output.GetDimensions(outputCount);
    float progress = saturate(State[0].x);
    if (id.x >= count || progress <= 1e-5 || PaletteDecision[0].w < .5 ||
        trackCount != count * PROXY_SAMPLES * 4 || stride != VERTEX_STRIDE ||
        outputCount != nativeCount * VERTEX_FLOATS)
        return;
    uint row = ProxyRows[id.x];
    if (row >= nativeCount)
        return;
    bool second = PaletteDecision[0].x > 1.5;
    float interval = progress * (PROXY_SAMPLES - 1);
    uint first = min((uint)interval, PROXY_SAMPLES - 2);
    float3 a, b, n, m;
    Sample(first, id.x, count, second, a, n);
    Sample(first + 1, id.x, count, second, b, m);
    Vertex source = Native[row];
    float fade = saturate(interval);
    float3 p = lerp(source.position, lerp(a, b, interval - first), fade);
    float3 normal = lerp(source.normal, lerp(n, m, interval - first), fade);
    if (!all(isfinite(p)))
        return;
    if (!all(isfinite(normal)) || dot(normal, normal) < 1e-8)
        normal = all(isfinite(source.normal)) && dot(source.normal, source.normal) > 1e-8
            ? source.normal : float3(0, 0, 1);
    normal = normalize(normal);
    float3 tangent = source.tangent.xyz - normal * dot(source.tangent.xyz, normal);
    if (dot(tangent, tangent) > 1e-8)
        tangent = normalize(tangent);
    else
        tangent = normalize(cross(normal, abs(normal.z) < .8 ? float3(0,0,1) : float3(0,1,0)));
    uint at = row * VERTEX_FLOATS;
    Output[at + POSITION_OFFSET] = p.x;
    Output[at + POSITION_OFFSET + 1] = p.y;
    Output[at + POSITION_OFFSET + 2] = p.z;
    Output[at + NORMAL_OFFSET] = normal.x;
    Output[at + NORMAL_OFFSET + 1] = normal.y;
    Output[at + NORMAL_OFFSET + 2] = normal.z;
    Output[at + TANGENT_OFFSET] = tangent.x;
    Output[at + TANGENT_OFFSET + 1] = tangent.y;
    Output[at + TANGENT_OFFSET + 2] = tangent.z;
}
