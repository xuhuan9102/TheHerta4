// The detector reports local vectors per unit of screen displacement. Recover
// the screen projection using the inverse of their Gram matrix.
float2 ClothScreenDirection(float3 direction, float4 right, float4 up)
{
    float xx = dot(right.xyz, right.xyz);
    float xy = dot(right.xyz, up.xyz);
    float yy = dot(up.xyz, up.xyz);
    float determinant = xx * yy - xy * xy;
    if (right.w < 0.5 || up.w <= 1e-5 || determinant <= max(1e-15, 1e-8 * xx * yy))
        return 0.0;

    float2 alignment = float2(dot(direction, right.xyz), dot(direction, up.xyz));
    float2 projected = float2(yy * alignment.x - xy * alignment.y, xx * alignment.y - xy * alignment.x) / determinant;
    return all(isfinite(projected)) ? projected : float2(0.0, 0.0);
}
