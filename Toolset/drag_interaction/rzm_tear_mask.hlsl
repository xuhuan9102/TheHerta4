// Paint the tear into a persistent UV-space coverage mask.
Buffer<float4> Gesture : register(t106);
Buffer<uint2> Reference : register(t108);
Texture1D<float4> IniParams : register(t120);
RWTexture2D<float> Mask : register(u0);

[numthreads(8, 8, 1)]
void main(uint3 id : SV_DispatchThreadID)
{
    float4 shape = Gesture[1];
    float4 anchor = Gesture[0];
    if (shape.w <= 0.0 || anchor.w != IniParams[494].y)
        return;

    uint width, height;
    Mask.GetDimensions(width, height);
    if (any(id.xy >= uint2(width, height)))
        return;
    uint2 packed = Reference[id.y * width + id.x];
    if ((packed.y >> 16u) == 0u)
        return;
    float3 rest = f16tof32(uint3(packed.x, packed.x >> 16u, packed.y));
    float3 offset = rest - anchor.xyz;
    float4 axis = Gesture[2];
    if (dot(offset, offset) > axis.w * axis.w * 2.0 ||
        abs(dot(offset, shape.xyz)) > shape.w * 0.65)
        return;

    float2 q = float2(dot(offset, axis.xyz) / axis.w,
                      dot(offset, cross(shape.xyz, axis.xyz)) / shape.w);
    float angle = atan2(q.y, q.x);
    float contour = 1.0 + 0.13 * cos(angle + 0.4) + 0.15 * sin(2.0 * angle)
                         + 0.10 * cos(3.0 * angle + 1.7) + 0.05 * sin(11.0 * angle);
    if (dot(q, q) <= contour * contour)
        Mask[id.xy] = 1.0;
}
