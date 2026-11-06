// Shared-direction dragging: constrain the complete moving cloth triangle.
// Each projected overlap yields sum(lambda[i] * displacement[i]) <= gap.
// All lambdas/displacements are nonnegative, so taking the minimum face scale
// at each incident vertex preserves EVERY face inequality without iteration.
struct VertexAttributes { float3 position; float3 normal; float4 tangent; };
StructuredBuffer<VertexAttributes> Source : register(t24);
RWStructuredBuffer<VertexAttributes> Target : register(u5);
Buffer<uint> TriVertices : register(t87);
Buffer<uint> TriangleOrder : register(t89);
Buffer<float4> Bounds : register(t91);
Buffer<uint> ClothTriangles : register(t92);
Buffer<uint> ClothAdjacency : register(t93);
RWBuffer<float> FaceLimits : register(u4);
Texture1D<float4> IniParams : register(t120);

float CrossProjected(float3 a, float3 b, float3 direction)
{
    return dot(cross(a, b), direction);
}

bool ProjectedBarycentric(float3 p, float3 a, float3 b, float3 c, float3 direction, out float3 weights)
{
    float3 ab = b - a, ac = c - a;
    float area = CrossProjected(ab, ac, direction);
    weights = 0.0;
    if (abs(area) < 1e-8 * max(length(ab) * length(ac), 1e-12)) return false;
    float v = CrossProjected(p - a, ac, direction) / area;
    float w = CrossProjected(ab, p - a, direction) / area;
    weights = float3(1.0 - v - w, v, w);
    return all(weights >= -1e-6) && all(weights <= 1.000001);
}

void ContactLimit(float3 weights, float3 body, float3 cloth[3], float3 distance,
                  float3 direction, inout float limit)
{
    weights = max(weights, 0.0);
    weights /= max(weights.x + weights.y + weights.z, 1e-12);
    float3 start = cloth[0] * weights.x + cloth[1] * weights.y + cloth[2] * weights.z;
    float gap = dot(body - start, direction);
    // An entering body surface behind the original cloth does not block it.
    // Already intersecting input geometry is outside this solver's contract.
    if (gap < -1e-6) return;
    float travel = dot(weights, distance);
    if (travel <= 1e-12) return;
    float clearance = max(IniParams[101].y, 1e-5);
    limit = min(limit, saturate((gap - clearance) / travel));
}

void LimitAgainstBody(uint tri, float3 cloth[3], float3 distance, float3 direction, inout float limit)
{
    uint3 ids = uint3(TriVertices[tri * 3u], TriVertices[tri * 3u + 1u], TriVertices[tri * 3u + 2u]);
    float3 body[3];
    body[0] = Source[ids.x].position;
    body[1] = Source[ids.y].position;
    body[2] = Source[ids.z].position;
    float3 normal = cross(body[1] - body[0], body[2] - body[0]);
    float3 reference = Source[ids.x].normal + Source[ids.y].normal + Source[ids.z].normal;
    if (dot(normal, reference) < 0.0) normal = -normal;
    if (dot(normal, direction) >= -1e-8 * max(length(normal), 1e-12)) return;
    float3 weights;
    [unroll]
    for (uint k = 0u; k < 3u; ++k)
    {
        // Cloth vertex projected inside the body triangle.
        if (ProjectedBarycentric(cloth[k], body[0], body[1], body[2], direction, weights))
        {
            float3 bodyProjection = body[0] * weights.x + body[1] * weights.y + body[2] * weights.z;
            float3 unit = 0.0; unit[k] = 1.0;
            ContactLimit(unit, bodyProjection, cloth, distance, direction, limit);
        }
        // Body vertex projected inside the cloth triangle: face interiors count.
        if (ProjectedBarycentric(body[k], cloth[0], cloth[1], cloth[2], direction, weights))
            ContactLimit(weights, body[k], cloth, distance, direction, limit);
    }
    [unroll]
    for (uint a = 0u; a < 3u; ++a)
    {
        uint a1 = (a + 1u) % 3u;
        float3 edge = cloth[a1] - cloth[a];
        [unroll]
        for (uint b = 0u; b < 3u; ++b)
        {
            uint b1 = (b + 1u) % 3u;
            float3 other = body[b1] - body[b];
            float denominator = CrossProjected(edge, other, direction);
            if (abs(denominator) < 1e-8 * max(length(edge) * length(other), 1e-12)) continue;
            float3 rel = body[b] - cloth[a];
            float u = CrossProjected(rel, other, direction) / denominator;
            float v = CrossProjected(rel, edge, direction) / denominator;
            if (u < -1e-6 || u > 1.000001 || v < -1e-6 || v > 1.000001) continue;
            weights = 0.0;
            weights[a] = 1.0 - saturate(u); weights[a1] = saturate(u);
            ContactLimit(weights, lerp(body[b], body[b1], saturate(v)), cloth, distance, direction, limit);
        }
    }
}

float FaceLimit(uint face)
{
    uint3 ids = uint3(ClothTriangles[face * 3u], ClothTriangles[face * 3u + 1u], ClothTriangles[face * 3u + 2u]);
    float3 cloth[3];
    cloth[0] = Source[ids.x].position;
    cloth[1] = Source[ids.y].position;
    cloth[2] = Source[ids.z].position;
    float3 delta[3];
    delta[0] = Target[ids.x].position - cloth[0];
    delta[1] = Target[ids.y].position - cloth[1];
    delta[2] = Target[ids.z].position - cloth[2];
    if (!all(isfinite(delta[0])) || !all(isfinite(delta[1])) || !all(isfinite(delta[2]))) return 0.0;
    float3 longest = delta[0];
    if (dot(delta[1], delta[1]) > dot(longest, longest)) longest = delta[1];
    if (dot(delta[2], delta[2]) > dot(longest, longest)) longest = delta[2];
    float len = length(longest);
    if (len < 1e-8) return 1.0;
    float3 direction = longest / len;
    float3 distance;
    float3 lo = min(cloth[0], min(cloth[1], cloth[2]));
    float3 hi = max(cloth[0], max(cloth[1], cloth[2]));
    [unroll]
    for (uint k = 0u; k < 3u; ++k)
    {
        distance[k] = dot(delta[k], direction);
        // Path-slide/custom motions with incompatible directions fail closed.
        if (distance[k] < -1e-7 || length(delta[k] - direction * distance[k]) > max(1e-6 * len, 2e-6)) return 0.0;
        distance[k] = max(distance[k], 0.0);
        lo = min(lo, cloth[k] + delta[k]);
        hi = max(hi, cloth[k] + delta[k]);
    }
    uint leaves = (uint)IniParams[115].y;
    uint count;
    TriangleOrder.GetDimensions(count);
    float limit = 1.0;
    uint node = 1u;
    [loop]
    while (node > 0u && limit > 0.0)
    {
        float3 nlo = Bounds[node * 2u].xyz, nhi = Bounds[node * 2u + 1u].xyz;
        float clearance = max(IniParams[101].y, 1e-5);
        if (all(lo <= nhi + clearance) && all(hi >= nlo - clearance) && all(nlo <= nhi))
        {
            if (node < leaves) { node *= 2u; continue; }
            uint slot = node - leaves;
            if (slot < count) LimitAgainstBody(TriangleOrder[slot], cloth, distance, direction, limit);
        }
        [loop]
        while (node > 1u && (node & 1u) != 0u) node >>= 1u;
        if (node == 1u) break;
        ++node;
    }
    return limit;
}

[numthreads(256, 1, 1)]
void main(uint3 dispatch : SV_DispatchThreadID)
{
    uint i = dispatch.x;
    if (IniParams[115].z < 1.5)
    {
        if (i < ClothAdjacency[1]) FaceLimits[i] = FaceLimit(i);
        return;
    }
    uint count = ClothAdjacency[0];
    if (i >= count) return;
    uint begin = ClothAdjacency[2u + i], end = ClothAdjacency[3u + i];
    if (begin == end) return;
    float limit = 1.0;
    [loop]
    for (uint k = begin; k < end; ++k)
        limit = min(limit, FaceLimits[ClothAdjacency[3u + count + k]]);
    VertexAttributes vertex = Target[i];
    float3 origin = Source[i].position;
    vertex.position = origin + (vertex.position - origin) * limit;
    Target[i] = vertex;
}
