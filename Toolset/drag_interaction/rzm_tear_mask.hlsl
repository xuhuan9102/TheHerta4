// Paint the tear into a persistent UV-space coverage mask.
Buffer<float4> Gesture : register(t106);
Buffer<uint2> Reference : register(t108);
Texture1D<float4> IniParams : register(t120);
RWTexture2D<float> Mask : register(u0);

float YarnVariation(float value)
{
    return frac(sin(value * 31.712) * 1234.124);
}

bool Slit(float2 fibreSpace, float2 radii, float seed)
{
    float along = fibreSpace.x / radii.x;
    float taper = sqrt(saturate(1.0 - along * along));
    float bend = (0.20 * sin(fibreSpace.x * 104.0 + seed) + 0.14 * along) * radii.y;
    float side = sign(fibreSpace.y - bend);
    float jagged = 0.17 * sin(fibreSpace.x * 610.0 + seed * side)
                 + 0.09 * sin(fibreSpace.x * 1380.0 - seed * side);
    float edge = radii.y * taper * (0.85 + jagged);
    return abs(along) < 1.0 && abs(fibreSpace.y - bend) < edge;
}

bool DroppedWales(float2 local, float start, float reach, float seed)
{
    float along = abs(local.x);
    float side = local.x < 0.0 ? 1.0 : -1.0;
    float progress = saturate((along - start) / max(reach, 1e-5));
    if (along < start * 0.50 || along > start + reach)
        return false;

    // The stitch phase stays fixed on the cloth as the damaged area grows.
    float stitch = along / 0.0041 + seed * 0.22 + side * 0.30;
    float row = floor(stitch);
    float phase = frac(stitch);
    float irregularity = YarnVariation(row + seed + side * 71.0);
    float shift = 0.00065 * sin(along * 120.0 + seed * side);
    float taper = lerp(1.0, 0.24, progress);
    float halfWidth = (0.0022 + 0.00065 * irregularity) * taper;
    float lateral = local.y - shift;
    float stitchEnd = lerp(0.72, 0.52, progress) + 0.13 * irregularity;
    bool missingStitch = phase > 0.17 && phase < stitchEnd && irregularity > progress * 0.68;
    // Leave a thread between the two runs. Interrupt one side on most late rows.
    bool lostColumn = lateral * side > 0.00018 || irregularity > 0.68;
    return abs(lateral) < halfWidth && missingStitch && lostColumn;
}

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
    float extent = axis.w + lerp(0.021, 0.046, saturate((axis.w - 0.016) / 0.035));
    float depth = abs(dot(offset, shape.xyz));
    if (dot(offset, offset) > extent * extent || depth > 0.008 + 0.10 * extent)
        return;

    float seed = YarnVariation(dot(anchor.xyz, float3(17.31, 32.67, 11.91))) * 6.2831853;
    float3 acrossPull = cross(shape.xyz, axis.xyz);
    float2 pullSpace = float2(dot(offset, axis.xyz), dot(offset, acrossPull));
    float2 radii = float2(axis.w, shape.w);
    bool opening = depth <= max(0.0055, shape.w * 0.85) && Slit(pullSpace, radii, seed);

    // Stockings unravel along the length of the leg in reference space.
    float3 wale = float3(0.0, 0.0, 1.0) - shape.xyz * shape.z;
    wale = dot(wale, wale) > 0.01 ? normalize(wale) : axis.xyz;
    float2 runSpace = float2(dot(offset, wale), dot(offset, cross(shape.xyz, wale)));
    float2 direction = float2(dot(wale, axis.xyz), dot(wale, acrossPull));
    float start = rsqrt(dot(direction / radii, direction / radii));
    float primarySide = direction.x > 0.15 ? 1.0 : -1.0;
    float reach = (extent - axis.w) * (runSpace.x * primarySide > 0.0 ? 1.0 : 0.56);
    bool run = abs(runSpace.y) < 0.004 && DroppedWales(runSpace, start, reach, seed);
    if (opening || run)
        Mask[id.xy] = 1.0;
}
