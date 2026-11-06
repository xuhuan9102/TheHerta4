// Final position constraint. Every candidate segment is swept against live
// collider triangles, including when the endpoint is beyond the far side.
struct VertexAttributes { float3 position; float3 normal; float4 tangent; };
StructuredBuffer<VertexAttributes> Source : register(t24);
RWStructuredBuffer<VertexAttributes> Target : register(u5);
Buffer<float4> VertexContact : register(t79);
Buffer<uint> TriVertices : register(t87);
Buffer<uint> TriangleOrder : register(t89);
Buffer<float4> Bounds : register(t91);
Texture1D<float4> IniParams : register(t120);

bool IntersectsBounds(float3 origin, float3 delta, float limit, float3 lo, float3 hi)
{
    if (any(lo > hi)) return false; // padded leaves
    float enter = 0.0, leave = limit;
    [unroll]
    for (uint axis = 0u; axis < 3u; ++axis)
    {
        if (abs(delta[axis]) < 1e-12)
        {
            if (origin[axis] < lo[axis] - 1e-6 || origin[axis] > hi[axis] + 1e-6) return false;
        }
        else
        {
            float t0 = (lo[axis] - 1e-6 - origin[axis]) / delta[axis];
            float t1 = (hi[axis] + 1e-6 - origin[axis]) / delta[axis];
            enter = max(enter, min(t0, t1));
            leave = min(leave, max(t0, t1));
            if (enter > leave) return false;
        }
    }
    return true;
}

bool SweepTriangle(float3 origin, float3 delta, uint tri, inout float first, inout float3 normal)
{
    float3 a = Source[TriVertices[tri * 3u]].position;
    float3 b = Source[TriVertices[tri * 3u + 1u]].position;
    float3 c = Source[TriVertices[tri * 3u + 2u]].position;
    float3 ab = b - a, ac = c - a;
    float3 n = cross(ab, ac);
    float area2 = dot(n, n);
    float det = -dot(n, delta);
    if (area2 < 1e-24 || abs(det) < 1e-8 * sqrt(area2 * dot(delta, delta))) return false;
    float3 rel = origin - a;
    float3 crossD = cross(delta, ac);
    float inv = 1.0 / det;
    float u = dot(rel, crossD) * inv;
    float3 crossR = cross(rel, ab);
    float v = dot(delta, crossR) * inv;
    float t = dot(ac, crossR) * inv;
    // Two-sided tests also handle inconsistent winding and open collider ends.
    if (u < -1e-6 || v < -1e-6 || u + v > 1.000001 || t < 0.0 || t > first) return false;
    first = t;
    normal = n * rsqrt(area2) * (det > 0.0 ? 1.0 : -1.0);
    return true;
}

bool Sweep(float3 origin, float3 delta, out float first, out float3 normal)
{
    uint leaves = (uint)IniParams[115].y;
    uint count;
    TriangleOrder.GetDimensions(count);
    first = 1.0;
    normal = 0.0;
    bool hit = false;
    uint node = 1u;
    [loop]
    while (node > 0u)
    {
        if (IntersectsBounds(origin, delta, first, Bounds[node * 2u].xyz, Bounds[node * 2u + 1u].xyz))
        {
            if (node < leaves)
            {
                node *= 2u;
                continue;
            }
            uint slot = node - leaves;
            if (slot < count)
                hit = SweepTriangle(origin, delta, TriangleOrder[slot], first, normal) || hit;
        }
        // Stackless next sibling, skipping the entire rejected subtree.
        [loop]
        while (node > 1u && (node & 1u) != 0u) node >>= 1u;
        if (node == 1u) break;
        ++node;
    }
    return hit;
}

[numthreads(256, 1, 1)]
void main(uint3 dispatch : SV_DispatchThreadID)
{
    uint i = dispatch.x;
    uint count, stride;
    Target.GetDimensions(count, stride);
    if (i >= count || VertexContact[i * 2u + 1u].w < 0.5) return;
    float3 origin = Source[i].position;
    VertexAttributes vertex = Target[i];
    float3 candidate = vertex.position;
    if (!all(isfinite(candidate))) candidate = origin;
    float3 safe = origin;
    float clearance = max(min(abs(VertexContact[i * 2u].w), max(IniParams[101].y, 1e-5)), 1e-5);
    [loop]
    for (uint attempt = 0u; attempt < 3u; ++attempt)
    {
        float3 delta = candidate - origin;
        if (dot(delta, delta) < 1e-16) { safe = origin; break; }
        float first;
        float3 normal;
        if (!Sweep(origin, delta, first, normal)) { safe = candidate; break; }
        float toward = max(-dot(delta, normal), 1e-12);
        float allowed = max(first - clearance / toward, 0.0);
        safe = origin + delta * allowed;
        if (IniParams[101].z > 0.5) break; // hard clamp
        float3 remainder = delta * (1.0 - allowed);
        candidate = safe + remainder - normal * min(dot(remainder, normal), 0.0);
        // The new straight segment is checked again, including for concave
        // geometry or a second limb. Exhaustion returns the last safe contact.
    }
    vertex.position = all(isfinite(safe)) ? safe : origin;
    Target[i] = vertex;
}
