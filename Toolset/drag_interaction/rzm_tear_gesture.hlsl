// Capture the existing grab in the garment's reference space.
StructuredBuffer<float4> Hit : register(t106);
Buffer<float4> Grab : register(t107);
StructuredBuffer<float3> Rest : register(t108);
Buffer<uint> Original : register(t109);
Buffer<uint> Owners : register(t110);
Texture1D<float4> IniParams : register(t120);
RWBuffer<float4> Gesture : register(u0);

[numthreads(1, 1, 1)]
void main()
{
    float4 anchor = Gesture[0]; // reference centre, object ID (0: new, -1: no hit)
    float4 shape = Gesture[1];  // reference normal, minor radius
    float4 axis = Gesture[2];   // reference tangent, major radius
    float4 drag = Gesture[3];   // cursor at capture, peak travel, seed face
    Gesture[1] = float4(shape.xyz, 0.0); // The index pass only applies new growth.
    float4 cursor = IniParams[69];
    if (any(cursor.xy < 0.0) || any(cursor.xy > cursor.zw) || Grab[3].w < 0.99)
        return;

    if (anchor.w == 0.0)
    {
        Gesture[0] = float4(0.0, 0.0, 0.0, -1.0);
        uint ownerCount, indexCount, vertexCount, stride;
        Owners.GetDimensions(ownerCount);
        Original.GetDimensions(indexCount);
        Rest.GetDimensions(vertexCount, stride);
        // The detector also accepts nearby hits and gives them valid edge barycentrics.
        if (Hit[0].x < 0.0 || Hit[1].w != 7.0 ||
            Hit[13].x != (float)Owners[0] || Hit[7].y != (float)Owners[1] ||
            distance(Grab[2].xyz, Hit[1].xyz) > 1e-5)
            return;

        uint face = (uint)Hit[2].w;
        uint first = face * 3;
        if (face + 2u >= ownerCount || first + 2u >= indexCount)
            return;
        uint owner = Owners[face + 2];
        uint3 indices = uint3(Original[first], Original[first + 1], Original[first + 2]);
        if (owner == 0 || any(indices >= vertexCount))
            return;

        float3 a = Rest[indices.x], b = Rest[indices.y], c = Rest[indices.z];
        float3 normal = cross(b - a, c - a);
        if (!all(isfinite(normal)) || dot(normal, normal) < 1e-18)
            return;
        float3 bary = Hit[4].xyz;
        Gesture[0] = float4(a * bary.x + b * bary.y + c * bary.z, (float)owner);
        Gesture[1] = float4(normalize(normal), 0.0);
        Gesture[2] = float4(normalize(b - a), 0.0);
        Gesture[3] = float4(cursor.xy, 0.0, (float)face);
        return;
    }

    if (anchor.w < 0.0)
        return;
    float2 delta = (cursor.xy - drag.xy) / max(1.0, min(cursor.z, cursor.w));
    float travel = length(delta);
    if (travel < 0.028 || travel < drag.z + 0.002 || drag.z >= 0.16)
        return;

    if (drag.z == 0.0)
    {
        float3 pull = Grab[6].xyz * delta.x + Grab[7].xyz * delta.y;
        pull -= shape.xyz * dot(pull, shape.xyz);
        if (dot(pull, pull) > 1e-12)
            axis.xyz = normalize(pull);
    }
    float growth = saturate((travel - 0.028) / 0.132);
    Gesture[1] = float4(shape.xyz, lerp(0.010, 0.026, growth));
    Gesture[2] = float4(axis.xyz, lerp(0.016, 0.060, growth));
    Gesture[3] = float4(drag.xy, travel, drag.w);
}
