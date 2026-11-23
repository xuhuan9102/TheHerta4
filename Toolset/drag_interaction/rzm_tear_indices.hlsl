// Apply the irregular tear contour to a private drawing index buffer.
Buffer<float4> Gesture : register(t106);
StructuredBuffer<float3> Rest : register(t108);
Buffer<uint> Original : register(t109);
Buffer<uint> Owners : register(t110);
RWBuffer<uint> Display : register(u0);

[numthreads(64, 1, 1)]
void main(uint3 id : SV_DispatchThreadID)
{
    float4 shape = Gesture[1];
    if (shape.w <= 0.0)
        return;
    uint ownerCount;
    Owners.GetDimensions(ownerCount);
    float4 anchor = Gesture[0];
    uint face = id.x;
    if (face + 2 >= ownerCount || Owners[face + 2] != (uint)anchor.w)
        return;

    uint first = face * 3;
    uint3 indices = uint3(Original[first], Original[first + 1], Original[first + 2]);
    float3 a = Rest[indices.x], b = Rest[indices.y], c = Rest[indices.z];
    if (face != (uint)Gesture[3].w)
    {
        float3 offset = (a + b + c) / 3.0 - anchor.xyz;
        float4 axis = Gesture[2];
        if (dot(offset, offset) > axis.w * axis.w * 2.0 ||
            abs(dot(offset, shape.xyz)) > shape.w * 0.65 ||
            dot(cross(b - a, c - a), shape.xyz) <= 0.0)
            return;

        float2 q = float2(dot(offset, axis.xyz) / axis.w,
                          dot(offset, cross(shape.xyz, axis.xyz)) / shape.w);
        float angle = atan2(q.y, q.x);
        float contour = 1.0 + 0.13 * cos(angle + 0.4) + 0.15 * sin(2.0 * angle)
                             + 0.10 * cos(3.0 * angle + 1.7) + 0.05 * sin(11.0 * angle);
        if (dot(q, q) > contour * contour)
            return;
    }

    Display[first + 1] = indices.x;
    Display[first + 2] = indices.x;
}
