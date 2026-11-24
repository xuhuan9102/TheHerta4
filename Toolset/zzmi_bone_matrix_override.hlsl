// This shader writes a private render VB. The game's deform SO and palettes are read-only.
struct Vertex
{
    float3 position;
    float3 normal;
    float4 tangent;
};

struct Blend
{
    float4 weights;
    uint4 slots;
};

struct Affine
{
    float4 x;
    float4 y;
    float4 z;
};

StructuredBuffer<Vertex> BindPositions : register(t90);
StructuredBuffer<Blend> BindBlend : register(t91);
Buffer<float4> NativePalette : register(t92);
Buffer<float4> BakedFrames : register(t93);
Buffer<uint> SlotToTrack : register(t94);
Buffer<uint4> Clip : register(t95);
Buffer<float4> LiveObject : register(t96);
Buffer<float4> WorldAnchor : register(t97);
Buffer<uint> ParentSlots : register(t98);
Buffer<uint2> LayerRanges : register(t100);
Buffer<uint> LayerCandidates : register(t101);
Buffer<uint2> LayerTracks : register(t102);
Buffer<float4> LayerControls : register(t103);
Buffer<uint> LayerFallback : register(t104);
Texture1D<float4> IniParams : register(t120);
RWBuffer<float> RenderVertices : register(u7);

Affine read_native(uint slot)
{
    uint row = slot * 3u;
    Affine m;
    m.x = NativePalette[row];
    m.y = NativePalette[row + 1u];
    m.z = NativePalette[row + 2u];
    return m;
}

float4 compose(float4 row, Affine frame)
{
    return row.x * frame.x + row.y * frame.y + row.z * frame.z + float4(0, 0, 0, row.w);
}

void matrix_for(uint slot, uint4 clip, uint frame, uint boneCount, Affine anchor, bool play, out Affine result)
{
    uint track = SlotToTrack[slot];
    result = read_native(slot);
    if (play && track < clip.x)
    {
        uint parentSlot = ParentSlots[track];
        if (parentSlot < boneCount)
        {
            anchor = read_native(parentSlot);
            float determinant = dot(anchor.x.xyz, cross(anchor.y.xyz, anchor.z.xyz));
            if (!isfinite(determinant) || abs(determinant) < 1e-6f ||
                !all(isfinite(float3(anchor.x.w, anchor.y.w, anchor.z.w))))
                return;
        }
        uint row = (frame * clip.x + track) * 3u;
        Affine baked;
        baked.x = BakedFrames[row];
        baked.y = BakedFrames[row + 1u];
        baked.z = BakedFrames[row + 2u];
        result.x = compose(anchor.x, baked);
        result.y = compose(anchor.y, baked);
        result.z = compose(anchor.z, baked);
    }
}

float3 transform_point(Affine m, float3 value)
{
    float4 p = float4(value, 1);
    return float3(dot(m.x, p), dot(m.y, p), dot(m.z, p));
}

float3 transform_direction(Affine m, float3 value)
{
    return float3(dot(m.x.xyz, value), dot(m.y.xyz, value), dot(m.z.xyz, value));
}

bool world_to_local_anchor(float epoch, out Affine anchor)
{
    anchor = (Affine)0;
    uint rows;
    LiveObject.GetDimensions(rows);
    if (rows < 4u || WorldAnchor[3].x == 0.0f || WorldAnchor[3].z != epoch)
        return false;
    float4 affineRow = float4(LiveObject[0].w, LiveObject[1].w, LiveObject[2].w, LiveObject[3].w);
    if (any(abs(affineRow - float4(0, 0, 0, 1)) > 1e-4f))
        return false;
    float4x4 current = transpose(float4x4(LiveObject[0], LiveObject[1], LiveObject[2], LiveObject[3]));
    float3 x = current[0].xyz, y = current[1].xyz, z = current[2].xyz;
    float3 cofactor = cross(y, z);
    float determinant = dot(x, cofactor);
    if (!isfinite(determinant) || abs(determinant) < 1e-6f)
        return false;
    float3x3 inverseBasis = transpose(float3x3(cofactor, cross(z, x), cross(x, y))) / determinant;
    float3 offset = -mul(inverseBasis, float3(current[0].w, current[1].w, current[2].w));
    float4x4 worldToObject = float4x4(float4(inverseBasis[0], offset.x),
        float4(inverseBasis[1], offset.y), float4(inverseBasis[2], offset.z), float4(0, 0, 0, 1));
    float4x4 world = float4x4(WorldAnchor[0], WorldAnchor[1], WorldAnchor[2], float4(0, 0, 0, 1));
    float4x4 local = mul(worldToObject, world);
    anchor.x = local[0];
    anchor.y = local[1];
    anchor.z = local[2];
    return true;
}

static const uint InvalidSlot = 0xffffffffu;

uint chosen_track(uint slot)
{
    uint2 range = LayerRanges[slot];
    for (uint i = 0; i < range.y; ++i)
    {
        uint candidate = LayerCandidates[range.x + i];
        if (LayerControls[LayerTracks[candidate].x].x != 0.0f)
            return candidate;
    }
    return InvalidSlot;
}

Affine authored_delta(uint track, uint frames)
{
    float offset = LayerControls[LayerTracks[track].x].y;
    offset = isfinite(offset) ? offset : 0.0f;
    uint frame = (uint)clamp(offset, 0.0f, (float)(frames - 1u));
    uint row = (track * frames + frame) * 3u;
    Affine result = {BakedFrames[row], BakedFrames[row+1u], BakedFrames[row+2u]};
    return result;
}

Affine combine(Affine parent, Affine child)
{
    Affine result = {compose(parent.x, child), compose(parent.y, child), compose(parent.z, child)};
    return result;
}

Affine identity_affine()
{
    Affine result = {float4(1,0,0,0), float4(0,1,0,0), float4(0,0,1,0)};
    return result;
}

uint track_or_bind_parent(uint slot, uint nativeRange, out bool animated)
{
    uint track = chosen_track(slot);
    animated = track != InvalidSlot;
    if (!animated && slot >= nativeRange)
    {
        uint2 range = LayerRanges[slot];
        if (range.y != 0)
            track = LayerCandidates[range.x];
    }
    return track;
}

Affine layered_matrix(uint slot, uint boneCount, uint nativeCount, uint nativeRange, uint4 clip, float4 params)
{
    uint fallback = slot;
    if (slot >= nativeRange)
        fallback = LayerFallback[slot - nativeRange];
    Affine native = identity_affine();
    if (fallback < nativeCount)
        native = read_native(fallback);
    bool animated;
    uint track = track_or_bind_parent(slot, nativeRange, animated);
    if (track == InvalidSlot || clip.y == 0u)
        return native;

    Affine authored = identity_affine();
    if (animated)
        authored = authored_delta(track, clip.y);
    for (uint depth = 0; depth < boneCount; ++depth)
    {
        uint parent = LayerTracks[track].y;
        if (parent == InvalidSlot)
        {
            Affine root;
            if (params.y == 3.0f)
            {
                if (!world_to_local_anchor(params.z, root))
                    return native;
            }
            else
            {
                if (clip.z >= nativeCount)
                    return native;
                root = read_native(clip.z);
            }
            return combine(root, authored);
        }
        if (parent >= boneCount)
            return native;
        track = track_or_bind_parent(parent, nativeRange, animated);
        if (track == InvalidSlot)
        {
            if (parent >= nativeCount)
                return native;
            Affine outside = read_native(parent);
            float determinant = dot(outside.x.xyz, cross(outside.y.xyz, outside.z.xyz));
            if (!isfinite(determinant) || abs(determinant) < 1e-6f ||
                !all(isfinite(float3(outside.x.w, outside.y.w, outside.z.w))))
                return native;
            return combine(outside, authored);
        }
        if (animated)
            authored = combine(authored_delta(track, clip.y), authored);
    }
    // Mixed choices can form a cycle even when each source rig is acyclic.
    return native;
}

void write_layered_matrix(uint slot, Affine value)
{
    uint first = slot * 12u;
    [unroll]
    for (uint i = 0; i < 4; ++i)
    {
        RenderVertices[first + i] = value.x[i];
        RenderVertices[first + 4u + i] = value.y[i];
        RenderVertices[first + 8u + i] = value.z[i];
    }
}

[numthreads(64, 1, 1)]
void main(uint3 thread : SV_DispatchThreadID)
{
    uint i = thread.x;
    float4 control = IniParams[43];
    if (control.w != 35983.0f)
        control = IniParams[42];
    if (control.w == 35983.0f)
    {
        if (i == 0)
        {
            uint row = (uint)control.x * 4u;
            RenderVertices[row] = control.y;
            RenderVertices[row + 1u] = control.z;
        }
        return;
    }

    uint nativeRows;
    NativePalette.GetDimensions(nativeRows);
    float4 params = IniParams[41];
    if (params.w != 34673.0f)
        params = IniParams[40];
    uint4 clip = Clip[0];
    if (params.w == 34673.0f && (params.y == 2.0f || params.y == 3.0f))
    {
        uint ranges;
        LayerRanges.GetDimensions(ranges);
        uint nativeRange = clip.w == 0u ? ranges : clip.w;
        uint nativeCount = min(nativeRows / 3u, nativeRange);
        uint boneCount = ranges > nativeRange ? ranges : nativeCount;
        if (i < boneCount)
            write_layered_matrix(i, layered_matrix(i, boneCount, nativeCount, nativeRange, clip, params));
        return;
    }

    uint vertexCount, vertexStride, rowCount, mapSlots;
    BindPositions.GetDimensions(vertexCount, vertexStride);
    NativePalette.GetDimensions(rowCount);
    SlotToTrack.GetDimensions(mapSlots);
    if (i >= vertexCount)
        return;
    uint boneCount = min(mapSlots, rowCount / 3u);
    Affine anchor = (Affine)0;
    bool validAnchor = false;
    if (params.y == 1.0f)
    {
        validAnchor = world_to_local_anchor(params.z, anchor);
    }
    else if (params.y == 0.0f && clip.z < boneCount)
    {
        anchor = read_native(clip.z);
        float determinant = dot(anchor.x.xyz, cross(anchor.y.xyz, anchor.z.xyz));
        validAnchor = isfinite(determinant) && abs(determinant) >= 1e-6f;
    }
    bool play = params.w == 34673.0f && params.y < 2.0f && clip.y > 0u && clip.z < boneCount && validAnchor;
    float offset = isfinite(params.x) ? params.x : 0.0f;
    uint frame = (uint)clamp(offset, 0.0f, (float)(max(1u, clip.y) - 1u));

    Vertex vertex = BindPositions[i];
    Blend blend = BindBlend[i];
    float3 p = 0;
    float3 n = 0;
    float3 t = 0;
    float total = 0;
    [unroll]
    for (uint k = 0; k < 4; ++k)
    {
        float weight = blend.weights[k];
        uint slot = blend.slots[k];
        if (weight > 0.0f && slot < boneCount)
        {
            Affine skin;
            matrix_for(slot, clip, frame, boneCount, anchor, play, skin);
            p += transform_point(skin, vertex.position) * weight;
            n += transform_direction(skin, vertex.normal) * weight;
            t += transform_direction(skin, vertex.tangent.xyz) * weight;
            total += weight;
        }
    }
    if (total <= 0.0f)
    {
        p = vertex.position;
        n = vertex.normal;
        t = vertex.tangent.xyz;
    }
    float normalLength = length(n);
    float tangentLength = length(t);
    if (normalLength > 1e-8f)
        n /= normalLength;
    if (tangentLength > 1e-8f)
        t /= tangentLength;

    uint row = i * 10u;
    RenderVertices[row] = p.x;
    RenderVertices[row + 1u] = p.y;
    RenderVertices[row + 2u] = p.z;
    RenderVertices[row + 3u] = n.x;
    RenderVertices[row + 4u] = n.y;
    RenderVertices[row + 5u] = n.z;
    RenderVertices[row + 6u] = t.x;
    RenderVertices[row + 7u] = t.y;
    RenderVertices[row + 8u] = t.z;
    RenderVertices[row + 9u] = vertex.tangent.w;
}
