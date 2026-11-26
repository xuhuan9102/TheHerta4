#include "screen_direction.hlsli"

Texture1D<float4> IniParams : register(t120);
StructuredBuffer<float4> Hit : register(t67);
Buffer<float4> Config : register(t82);
Buffer<float4> Blend : register(t86);
Buffer<float4> ControlAxes : register(t88);
Buffer<uint> WearMap : register(t94);
Buffer<float4> ControlZones : register(t110);
Buffer<float4> Palette : register(t2);
RWBuffer<float> Intent : register(u0);

[numthreads(1, 1, 1)]
void main()
{
    uint rows, stride;
    Hit.GetDimensions(rows, stride);
    if (rows < 10 || Hit[0].x < 0.0)
        return;

    uint zoneCount, vertexCount, paletteRows, blendRows;
    ControlZones.GetDimensions(zoneCount);
    WearMap.GetDimensions(vertexCount);
    Palette.GetDimensions(paletteRows);
    Blend.GetDimensions(blendRows);
    uint zone = (uint)max(round(Hit[7].w), 0);
    uint nearest = (uint)clamp(round(Hit[3].w), 0, 2);
    uint vertex = (uint)max(Hit[3][nearest], 0);
    if (zone >= zoneCount || vertex >= vertexCount || vertex * 2 + 1 >= blendRows)
        return;
    if (ControlZones[zone].x < 0.5 || WearMap[vertex] >= (uint)Config[0].x)
        return;

    uint axesCount;
    ControlAxes.GetDimensions(axesCount);
    float4 authored = zone < axesCount ? ControlAxes[zone] : 0.0;
    float3 axis = authored.w > 0.0 ? authored.xyz : Config[6].xyz;
    float4 weights = Blend[vertex * 2];
    uint4 indices = asuint(Blend[vertex * 2 + 1]);
    float3 posed = 0.0;
    [unroll]
    for (uint i = 0; i < 4; ++i)
    {
        uint bone = indices[i];
        if (weights[i] <= 0.0 || bone >= paletteRows / 3)
            continue;
        uint row = bone * 3;
        posed += weights[i] * float3(dot(Palette[row].xyz, axis),
                                     dot(Palette[row + 1].xyz, axis),
                                     dot(Palette[row + 2].xyz, axis));
    }

    float4 cursor = IniParams[67];
    float2 pixels = ClothScreenDirection(posed, Hit[8], Hit[9]) * cursor.zw;
    float2 path = pixels * (authored.w > 0.0 ? authored.w : Config[6].w);
    float pathLength2 = dot(path, path);
    float motionLength2 = dot(cursor.xy, cursor.xy);
    float along = dot(cursor.xy, path);
    // A foreshortened path or diagonal grab leaves the ordinary drag selected.
    if (pathLength2 >= 36.0 && along * along >= 0.75 * pathLength2 * motionLength2)
        Intent[0] = 2.0;
}
