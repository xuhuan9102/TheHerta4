// Keep game heading and position live while stabilizing the height of the planted sole.
struct SoleVertex
{
    float4 position;
    float4 weights;
    uint4 slots;
};

Buffer<float4> LiveObject : register(t90);
Buffer<float4> NativePalette : register(t91);
Buffer<uint4> Clip : register(t92);
Buffer<float4> BindPivot : register(t93);
StructuredBuffer<SoleVertex> SupportSoles : register(t94);
Texture1D<float4> IniParams : register(t120);
RWBuffer<float4> State : register(u6);
RWBuffer<float4> World : register(u7);

bool grounded_height(float4x4 objectToWorld, uint boneCount, float3 pivot, float scale, out float height)
{
    uint count, stride;
    SupportSoles.GetDimensions(count, stride);
    float lowestNative = 3.402823e+38f;
    float lowestBind = 3.402823e+38f;
    for (uint i = 0; i < count; ++i)
    {
        SoleVertex sample = SupportSoles[i];
        float3 position = 0.0f;
        float total = 0.0f;
        [unroll]
        for (uint k = 0; k < 4; ++k)
        {
            uint slot = sample.slots[k];
            float weight = sample.weights[k];
            if (weight > 0.0f && slot < boneCount)
            {
                uint row = slot * 3u;
                position += weight * float3(dot(NativePalette[row], sample.position),
                    dot(NativePalette[row + 1u], sample.position), dot(NativePalette[row + 2u], sample.position));
                total += weight;
            }
        }
        if (total > 0.0f)
        {
            lowestBind = min(lowestBind, sample.position.z);
            lowestNative = min(lowestNative, mul(objectToWorld, float4(position / total, 1)).y);
        }
    }
    height = lowestNative + scale * (pivot.z - lowestBind);
    return lowestNative < 3.402823e+38f && isfinite(height);
}

[numthreads(1, 1, 1)]
void main()
{
    float4 params = IniParams[42];
    if (params.w != 34674.0f)
        params = IniParams[41];
    bool captured = State[3].w != 0.0f && State[3].x == params.x;
    if (params.w != 34674.0f || (captured && (World[3].y == params.y ||
        (params.z >= 0 && World[3].y >= params.z))))
        return;

    uint objectRows, nativeRows;
    LiveObject.GetDimensions(objectRows);
    NativePalette.GetDimensions(nativeRows);
    uint anchor = Clip[0].z;
    uint boneCount = nativeRows / 3u;
    if (objectRows < 4u || anchor >= boneCount)
        return;
    float4 affineRow = float4(LiveObject[0].w, LiveObject[1].w, LiveObject[2].w, LiveObject[3].w);
    if (any(abs(affineRow - float4(0, 0, 0, 1)) > 1e-4f))
        return;
    float3 origin = LiveObject[3].xyz;
    if (!all(isfinite(origin)))
        return;
    uint stateRows;
    State.GetDimensions(stateRows);
    if (captured && stateRows >= 6u)
    {
        float4 previous = State[5];
        // A render occurrence can be reused for the wardrobe, world or a respawn.
        if (previous.w > 0.0 && (length(origin - previous.xyz) > 1.25
            || params.y - previous.w > 30.0 || params.y < previous.w))
            captured = false;
    }

    float4x4 objectToWorld = transpose(float4x4(LiveObject[0], LiveObject[1], LiveObject[2], LiveObject[3]));
    uint src = anchor * 3u;
    float4x4 reference = float4x4(
        captured ? State[0] : NativePalette[src],
        captured ? State[1] : NativePalette[src + 1u],
        captured ? State[2] : NativePalette[src + 2u], float4(0, 0, 0, 1));
    float4x4 live = mul(objectToWorld, reference);
    float3 x = float3(live[0].x, live[1].x, live[2].x);
    float3 y = float3(live[0].y, live[1].y, live[2].y);
    float3 z = float3(live[0].z, live[1].z, live[2].z);
    float determinant = dot(x, cross(y, z));
    if (!isfinite(determinant) || abs(determinant) < 1e-6f)
        return;

    float handedness = determinant < 0.0f ? -1.0f : 1.0f;
    float3 scale = captured ? State[4].xyz : float3(length(x), length(y), length(z));
    float2 horizontalX = normalize(x).xz;
    float2 horizontalY = normalize(y).xz;
    float2 heading = horizontalX + handedness * float2(-horizontalY.y, horizontalY.x);
    if (dot(heading, heading) < 1e-8f)
    {
        heading = captured ? float2(World[0].x, World[2].x) : horizontalX;
        if (dot(heading, heading) < 1e-8f)
            heading = float2(1, 0);
    }
    heading = normalize(heading);

    float3 right = float3(heading.x, 0, heading.y) * scale.x;
    float3 depth = float3(heading.y, 0, -heading.x) * (handedness * scale.y);
    float3 up = float3(0, scale.z, 0);
    float4 pivot = BindPivot[0];
    float3 pelvis = LiveObject[3].xyz;
    float soleHeight;
    bool hasSupport = grounded_height(objectToWorld, boneCount, pivot.xyz, scale.z, soleHeight);
    if (!captured)
    {
        pelvis.y = hasSupport ? soleHeight : mul(live, pivot).y;
        State[0] = reference[0];
        State[1] = reference[1];
        State[2] = reference[2];
        State[3] = float4(params.x, pelvis.y, 0, 1);
        State[4] = float4(scale, pelvis.y - origin.y);
    }
    else
    {
        float4 support = State[3];
        float objectHeight = origin.y + State[4].w;
        float drift = objectHeight - support.y;
        float excursion = soleHeight - support.y;
        // A lifted foot or a short pelvis bob can move one estimate on its own.
        // Require both estimates to agree for two consecutive frames.
        float deadband = 0.075f * scale.z;
        float direction = abs(drift) > deadband && abs(excursion) > deadband
            && drift * excursion > 0.0f ? sign(drift) : 0.0f;
        if (!hasSupport)
            support.y = objectHeight;
        else if (direction != 0.0f && support.z == direction)
        {
            // Use the less extreme estimate when the native foot is mid-stride.
            support.y = direction > 0.0f ? min(objectHeight, soleHeight) : max(objectHeight, soleHeight);
            direction = 0.0f;
        }
        support.z = hasSupport ? direction : 0.0f;
        State[3] = support;
        pelvis.y = support.y;
    }
    if (stateRows >= 6u)
        State[5] = float4(origin, params.y);

    float3 offset = pelvis - right * pivot.x - depth * pivot.y - up * pivot.z;
    World[0] = float4(right.x, depth.x, 0, offset.x);
    World[1] = float4(0, 0, up.y, offset.y);
    World[2] = float4(right.z, depth.z, 0, offset.z);
    World[3] = float4(1, params.y, params.x, pelvis.y);
}
