// The four bytes store opening, thinning, surviving yarn, and packed rim.
Buffer<float4> Gesture : register(t106);
Buffer<uint2> Reference : register(t108);
Texture1D<float4> IniParams : register(t120);
RWTexture2D<uint> Mask : register(u0);

float YarnVariation(float value)
{
    return frac(sin(value * 31.712) * 1234.124);
}

float4 ReferencePoint(uint2 id, uint width)
{
    uint2 packed = Reference[id.y * width + id.x];
    return f16tof32(uint4(packed.x, packed.x >> 16u, packed.y, packed.y >> 16u));
}

float ContourRadius(float2 q, float seed)
{
    float angle = atan2(q.y, q.x);
    float3 wave = cos(float3(angle, 2.0 * angle, 3.0 * angle) + seed * float3(1.0, -0.63, 1.77));
    return 1.0 + dot(0.5 + 0.5 * wave, float3(0.09, 0.10, 0.06));
}

float OpeningDistance(float2 q, float2 radii, float seed)
{
    float radius = length(q);
    return radius < 1e-5 ? -min(radii.x, radii.y)
        : radius * (radius - ContourRadius(q, seed)) / length(q / radii);
}

float2 WaleEnds(float2 rowOrigin, float2 along, float seed)
{
    float a = dot(along, along);
    float center = -dot(rowOrigin, along) / a;
    float2 closest = rowOrigin + center * along;
    float radius2 = dot(closest, closest);
    if (radius2 >= 1.0)
        return float2(1.0, -1.0);
    float2 side = float2(-1.0, 1.0);
    float2 inside = center + side * sqrt((1.0 - radius2) / a);
    float2 outside = center + side * sqrt((1.25 * 1.25 - radius2) / a);
    [unroll]
    for (int i = 0; i < 4; ++i)
    {
        float2 mid = (inside + outside) * 0.5;
        float2 low = rowOrigin + mid.x * along;
        float2 high = rowOrigin + mid.y * along;
        bool2 within = bool2(length(low) < ContourRadius(low, seed), length(high) < ContourRadius(high, seed));
        inside = float2(within.x ? mid.x : inside.x, within.y ? mid.y : inside.y);
        outside = float2(within.x ? outside.x : mid.x, within.y ? outside.y : mid.y);
    }
    return (inside + outside) * 0.5;
}

float CourseIntegral(float phase, float duty)
{
    return floor(phase) * duty + min(frac(phase), duty);
}

float YarnCoverage(float along, float pitch, float duty, float footprint)
{
    float phase = along / pitch + 0.5 * duty;
    float filter = max(footprint / pitch, 0.002);
    float low = CourseIntegral(phase - 0.5 * filter, duty);
    float high = CourseIntegral(phase + 0.5 * filter, duty);
    return saturate((high - low) / filter);
}

float RunStrength(float2 local, float2 across, float2 along, float2 radii, float seed, float depth)
{
    float extent = length(across * radii * radii);
    if (abs(local.x) >= extent || depth >= 0.015)
        return 0.0;
    float2 ends = WaleEnds(across * local.x, along, seed);
    if (ends.x >= ends.y)
        return 0.0;
    float side = local.y > (ends.x + ends.y) * 0.5 ? 1.0 : -1.0;
    float beyond = max(0.0, side * (local.y - (side > 0.0 ? ends.y : ends.x)));
    float size = smoothstep(0.006, 0.035, sqrt(radii.x * radii.y));
    float load = along.x * radii.x;
    float loadedSide = lerp(0.72, (side * load >= 0.0 ? 1.0 : 0.44), smoothstep(0.1, 0.65, abs(load)));
    float column = YarnVariation(floor(local.x / 0.006) + seed + side * 21.0);
    float reach = lerp(0.065, 0.145, size) * lerp(0.74, 1.0, column) * lerp(0.72, 1.0, loadedSide);
    float progress = saturate(beyond / reach);
    float width = max(0.0011, extent * lerp(0.44, 0.78, size)) * lerp(1.0, 0.30, progress);
    float lateral = 1.0 - smoothstep(width * 0.35, width, abs(local.x));
    return lateral * loadedSide * (1.0 - smoothstep(0.43, 1.0, progress))
        * exp(-0.65 * progress) * (1.0 - smoothstep(0.006, 0.015, depth));
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
    float4 texel = ReferencePoint(id.xy, width);
    if (texel.w <= 0.0)
        return;
    float3 offset = texel.xyz - anchor.xyz;
    float depth = abs(dot(offset, shape.xyz));
    if (dot(offset, offset) > 0.21 * 0.21 || depth >= 0.015)
        return;

    float4 axis = Gesture[2];
    float3 acrossPull = cross(shape.xyz, axis.xyz);
    float2 radii = float2(axis.w, shape.w);
    float2 pull = float2(dot(offset, axis.xyz), dot(offset, acrossPull));
    float seed = YarnVariation(dot(anchor.xyz, float3(17.31, 32.67, 11.91))) * 6.2831853;
    float sd = OpeningDistance(pull / radii, radii, seed);
    sd += 0.00018 * sin(dot(texel.xyz, float3(1470.0, 1130.0, 880.0)) + seed);

    // The two UV neighbours estimate how much cloth one texel covers.
    float3 wale = float3(0.0, 0.0, 1.0) - shape.xyz * shape.z;
    wale = dot(wale, wale) > 0.01 ? normalize(wale) : axis.xyz;
    float3 acrossWale = cross(shape.xyz, wale);
    float4 px = ReferencePoint(uint2(min(id.x + 1u, width - 1u), id.y), width);
    float4 py = ReferencePoint(uint2(id.x, min(id.y + 1u, height - 1u)), width);
    float3 dx = px.xyz - texel.xyz;
    float3 dy = py.xyz - texel.xyz;
    dx = px.w > 0.0 && dot(dx, dx) < 0.000025 ? dx : 0.0;
    dy = py.w > 0.0 && dot(dy, dy) < 0.000025 ? dy : 0.0;
    float aa = max(0.00013, 0.42 * (length(dx) + length(dy)));
    float footprint = max(0.00008, abs(dot(dx, wale)) + abs(dot(dy, wale)));

    float front = 1.0 - smoothstep(max(0.0055, shape.w * 0.85), 0.010, depth);
    float opening = (1.0 - smoothstep(-aa, aa, sd)) * front;
    float bandWidth = lerp(0.00028, 0.00050, YarnVariation(floor(dot(texel.xyz, acrossWale) / 0.0016) + seed));
    float rim = (smoothstep(-aa, aa, sd) - smoothstep(bandWidth - aa, bandWidth + aa, sd)) * front;

    float2 local = float2(dot(offset, acrossWale), dot(offset, wale));
    float2 across = float2(dot(acrossWale, axis.xyz), dot(acrossWale, acrossPull)) / radii;
    float2 along = float2(dot(wale, axis.xyz), dot(wale, acrossPull)) / radii;
    float thinning = RunStrength(local, across, along, radii, seed, depth) * (1.0 - opening);
    float position = dot(texel.xyz, wale);
    float bow = 0.00011 * sin(dot(texel.xyz, acrossWale) * 760.0);
    float fine = YarnCoverage(position + bow, 0.00115, 0.12, footprint);
    float paired = YarnCoverage(position + 0.75 * bow, 0.0025, 0.105, footprint);
    thinning *= 1.0 - 0.78 * (1.0 - (1.0 - fine) * (1.0 - paired));
    float edgeStrands = (1.0 - smoothstep(0.0010, 0.0034, abs(sd))) * front;
    float loose = opening * edgeStrands * paired * (0.40 + 0.45 * YarnVariation(floor(position / 0.0025) + seed));

    uint packed = Mask[id.xy];
    uint4 previous = (packed >> uint4(0, 8, 16, 24)) & 255u;
    uint4 encoded = (uint4)round(saturate(float4(opening, thinning, loose, rim)) * 255.0);
    uint4 value = max(previous, encoded);
    if (encoded.x == 255u)
    {
        // Growth can move an old rim inside the opening. Keep only the
        // strands supported by the newly painted edge in that area.
        value.z = encoded.z;
        value.w = 0u;
    }
    if (any(value != previous))
        Mask[id.xy] = value.x | (value.y << 8u) | (value.z << 16u) | (value.w << 24u);
}
