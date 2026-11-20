Texture1D<float4> IniParams : register(t120);
Texture2D<float4> SurfaceNormals : register(t118);
cbuffer VertexCamera : register(b13)
{
    float4 camera[131];
};
cbuffer CharacterParams : register(b2)
{
    float4 character[5];
};
cbuffer DeferredCharacterParams : register(b3)
{
    float4 deferredCharacter[8];
};

float3x3 projectionAxes(uint index)
{
    return float3x3(
        camera[index].x, camera[index + 1].x, camera[index + 2].x,
        camera[index].y, camera[index + 1].y, camera[index + 2].y,
        camera[index].w, camera[index + 1].w, camera[index + 2].w
    );
}

float projectionError(float3x3 axes)
{
    if (dot(axes[0], axes[0]) < 0.000001 || dot(axes[1], axes[1]) < 0.000001)
        return 10.0;
    return abs(length(axes[2]) - 1.0)
        + abs(dot(normalize(axes[0]), axes[2]))
        + abs(dot(normalize(axes[1]), axes[2]))
        + abs(dot(normalize(axes[0]), normalize(axes[1])));
}

#if defined(REPLAY_PACKED) || defined(REPLAY_FACE)
struct ReplayInput
{
    float4 uv0 : TEXCOORD0;
    float4 uv1 : TEXCOORD1;
#ifdef REPLAY_PACKED
    float4 axis2 : TEXCOORD2;
    float4 axis3 : TEXCOORD3;
    float4 axis4 : TEXCOORD4;
#else
    float3 axis2 : TEXCOORD2;
    float3 axis3 : TEXCOORD3;
    float3 axis4 : TEXCOORD4;
#endif
    float4 unused5 : TEXCOORD5;
    float4 unused6 : TEXCOORD6;
    float4 unused7 : TEXCOORD7;
    float3 unused8 : TEXCOORD8;
    float4 position : SV_Position;
};
float4 main(ReplayInput input) : SV_Target0
#else
float4 main(float4 position : SV_Position) : SV_Target0
#endif
{
#if defined(REPLAY_PACKED) || defined(REPLAY_FACE)
    float4 position = input.position;
#endif
    float2 targetSize = max(IniParams[495].xy, 1.0);
    float2 screen = position.xy / targetSize * float2(2.0, -2.0) + float2(-1.0, 1.0);
    float3 normal = SurfaceNormals.Load(int3(int2(position.xy), 0)).xyz * 2.0 - 1.0;
    float3x3 axes = projectionAxes(125);
    float3x3 alternate = projectionAxes(127);
    bool useAlternate = projectionError(alternate) < projectionError(axes);
    if (useAlternate)
        axes = alternate;

    if (dot(normal, normal) < 0.25 || projectionError(axes) > 0.1)
        return 0.0;

    // Invert the mapping from world direction to clip x, y and w.
    float3 right = cross(axes[1], axes[2]);
    float3 up = cross(axes[2], axes[0]);
    float3 forward = cross(axes[0], axes[1]);
    float determinant = dot(axes[0], right);
    if (abs(determinant) < 0.000001)
        return 0.0;
    // The packed clip-position layout adds the current temporal jitter in the VS.
    if (useAlternate)
        screen -= camera[21].xy;
    float4 translation = camera[useAlternate ? 130 : 128];
    float3 clipPosition = float3(screen, 1.0) / max(position.w, 0.000001);
    float3 clipDelta = clipPosition - translation.xyw;
#if defined(REPLAY_PACKED)
    float3 worldPosition = float3(input.axis2.w, input.axis3.w, input.axis4.w);
#elif defined(REPLAY_FACE)
    float3 worldPosition = input.axis4;
#else
    float3 worldPosition = (clipDelta.x * right + clipDelta.y * up + clipDelta.z * forward) / determinant;
#endif
    float3 view = normalize(worldPosition - camera[useAlternate ? 55 : 53].xyz);

    float4 head = character[4];
    if (!useAlternate)
    {
        // In this camera layout the character buffer is at b3 and stores
        // the inverse head transform instead of a ready-made sphere.
        head.xyz = -(deferredCharacter[5].w + 0.08) * deferredCharacter[5].xyz
                   - deferredCharacter[6].w * deferredCharacter[6].xyz
                   - deferredCharacter[7].w * deferredCharacter[7].xyz;
        head.w = abs(length(deferredCharacter[5].xyz) - 1.0) < 0.01 ? 0.13 : 0.0;
    }
    if (head.w > 0.0)
    {
        float3 delta = worldPosition - head.xyz;
        float distanceToCenter = length(delta);
        float weight = saturate(1.0 - (distanceToCenter - head.w) * 20.0);
        normal = lerp(normal, delta / max(distanceToCenter, 0.000001), weight);
    }
    float heightScale = IniParams[498].y;
    float phase;
    float gain;
    if (heightScale > 0.0)
    {
        float originY = useAlternate ? character[0].y : deferredCharacter[0].y;
        phase = frac(saturate((worldPosition.y - originY) * heightScale + 0.5) + IniParams[497].x);
        float grazing = saturate(1.0 - abs(dot(normal, view)));
        float grazing2 = grazing * grazing;
        float grazing5 = grazing2 * grazing2 * grazing;
        gain = 0.25 * grazing5 * (1.0 + sqrt(grazing5));
    }
    else
    {
        float3 reflection = reflect(view, normalize(normal));
        // Use the same world-space phase as the injected Mirror mode.
        phase = frac(dot(reflection.yz, float2(0.45, 0.20)) + 0.5 + IniParams[497].x);
        phase = frac(phase + 0.14 * sin((reflection.x * 0.5 + 0.5) * 6.2831853));
        gain = 0.045;
    }
    float wave = 0.5 + 0.5 * cos(phase * 6.2831853);
    float strength = wave * wave * IniParams[497].y * gain;
    return float4(IniParams[496].rgb * strength, 0.0);
}
