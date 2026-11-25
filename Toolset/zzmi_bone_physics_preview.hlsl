// Procedural line-list preview. Camera registers match the ZZMI render VS.
Buffer<float4> Capsules : register(t105);
Buffer<float4> State : register(t106);
Buffer<float4> Camera : register(t107);
Buffer<float4> JointOptions : register(t108);

struct Vertex
{
    float4 position : SV_Position;
    float4 color : COLOR0;
};

float4x4 camera_matrix(uint first)
{
    return float4x4(Camera[first], Camera[first + 1u], Camera[first + 2u], Camera[first + 3u]);
}

bool is_scene_projection(float4x4 projection)
{
    float3 right = float3(projection[0].x, projection[1].x, projection[2].x);
    float3 up = float3(projection[0].y, projection[1].y, projection[2].y);
    float3 depth = float3(projection[0].w, projection[1].w, projection[2].w);
    float3 lengths = float3(length(right), length(up), length(depth));
    float3 crossTerms = abs(float3(dot(right, up), dot(right, depth), dot(up, depth)));
    return all(lengths.xy > 0.1) && all(lengths.xy < 20.0) && abs(lengths.z - 1.0) < 0.1
        && all(crossTerms <= 0.06 * lengths.xxy * lengths.yzz)
        && abs(determinant(projection)) > 1e-6;
}

float3 rotate_box_side(float4 rotation, float3 side)
{
    return side + 2.0 * cross(rotation.xyz, cross(rotation.xyz, side) + rotation.w * side);
}

float3 rounded_profile(uint edge, uint end, float3 core, float radius)
{
    uint plane = edge / 20u;
    uint knot = (edge + end) % 20u;
    uint corner = knot / 5u, sample = knot % 5u;
    uint u = plane, v = (plane + 1u) % 3u;
    float angle = (corner + 0.25 * sample) * 1.5707963;
    float alongU = (corner == 0u || corner == 3u ? core[u] : -core[u]) + radius * cos(angle);
    float alongV = (corner < 2u ? core[v] : -core[v]) + radius * sin(angle);
    if (plane == 0u)
        return float3(alongU, alongV, 0);
    if (plane == 1u)
        return float3(0, alongU, alongV);
    return float3(alongV, 0, alongU);
}

Vertex main(uint id : SV_VertexID)
{
    const uint edges = 63;
    const uint verticesPerBody = 3 * edges * 2;
    Vertex result;
    result.position = float4(2, 2, 2, 1);
    result.color = 0;
    uint cameraRows, stateRows, bodyRows;
    Camera.GetDimensions(cameraRows);
    State.GetDimensions(stateRows);
    Capsules.GetDimensions(bodyRows);
    uint body = id / verticesPerBody;
    uint baseRow = 2u + body * 6u;
    if (cameraRows < 131u || stateRows <= baseRow + 5u || body * 5u >= bodyRows || State[0].y <= 0.0)
        return result;
    uint mode = (uint)Capsules[body * 5u].z;
    uint shell = (id % verticesPerBody) / (edges * 2);
    float radius = State[baseRow+2u].w * (1.0 - 0.25 * shell);
    float4 axisLength = State[baseRow+4u];
    if (radius <= 0.0)
        return result;
    float3 p = State[baseRow].xyz;
    uint bodyCount = bodyRows / 5u;
    uint offsetRow = 2u + 6u * bodyCount + max(bodyCount * bodyCount, 1u + 2u * bodyCount) + body;
    if (offsetRow < stateRows)
        p += State[offsetRow].xyz;
    float3 axis = axisLength.xyz;
    float h = axisLength.w;
    float3 x = normalize(cross(axis, abs(axis.y) < 0.85 ? float3(0,1,0) : float3(1,0,0)));
    float3 z = cross(axis, x);
    uint edge = (id / 2u) % edges;
    uint end = id % 2u;
    uint optionRows;
    JointOptions.GetDimensions(optionRows);
    float4 box = 0.0;
    if (optionRows >= 1u + 3u * bodyCount && JointOptions[0].z == 3.0)
        box = JointOptions[2u + 3u * body];
    if (box.x > 0.0 && edge < 60u)
    {
        float scale = State[baseRow+2u].w / Capsules[body * 5u + 1u].w;
        box *= scale;
        float inset = 0.25 * shell * min(box.x, box.z);
        float3 core = max(0.0, box.xyz - max(box.w, inset));
        float3 side = rotate_box_side(State[baseRow + 1u], JointOptions[3u + 3u * body].xyz);
        x = normalize(side - axis * dot(side, axis));
        z = cross(x, axis);
        float3 location = rounded_profile(edge, end, core, max(0.0, box.w - inset));
        p += location.x * x + location.y * axis + location.z * z;
    }
    else if (edge < 24u)
    {
        float a = (edge % 12u + end) * (6.2831853 / 12.0);
        p += (edge < 12u ? -h : h) * axis + radius * (cos(a) * x + sin(a) * z);
    }
    else if (edge < 56u)
    {
        uint arc = (edge - 24u) / 8u;
        float a = ((edge - 24u) % 8u + end) * (3.1415927 / 8.0);
        float side = arc < 2u ? -1.0 : 1.0;
        p += side * (h + radius * sin(a)) * axis
            + radius * cos(a) * ((arc % 2u) == 0u ? x : z);
    }
    else if (edge < 60u)
    {
        float a = (edge - 56u) * 1.5707963;
        p += (end == 0u ? -h : h) * axis + radius * (cos(a) * x + sin(a) * z);
    }
    else
    {
        float3 force = State[baseRow+5u].xyz;
        float magnitude = length(force);
        if (mode != 2 || shell != 0u || magnitude < 0.05)
            return result;
        float3 direction = force / magnitude;
        float3 side = normalize(cross(direction, abs(direction.y) < 0.85 ? float3(0,1,0) : float3(1,0,0)));
        float reach = min(0.18, 0.035 + magnitude * 0.019);
        float3 tip = p + direction * reach;
        if (end == 1u)
            p = tip;
        else if (edge != 60u)
            p = tip - direction * reach * 0.3 + (edge == 61u ? side : -side) * reach * 0.19;
    }
    float4x4 viewProjection = camera_matrix(127);
    bool showcase = is_scene_projection(viewProjection);
    if (!showcase)
    {
        viewProjection = camera_matrix(125);
        if (!is_scene_projection(viewProjection))
            return result;
    }
    float4 projected = mul(float4(p, 1), viewProjection);
    if (showcase)
        projected.xy += Camera[21].xy * projected.w;
    if (all(isfinite(projected)) && projected.w > 0.01)
    {
        result.position = projected;
        result.position.z = clamp(projected.z, 0.0, 0.999 * projected.w);
        if (edge >= 60u)
            result.color = float4(1,0.85,0.30,1);
        else if (shell == 1u)
            result.color = float4(1,0.68,0.18,0.55);
        else if (shell == 2u)
            result.color = float4(0.72,0.42,1,0.55);
        else if (mode == 2)
            result.color = State[baseRow].w < 0.0 ? float4(0.76,0.45,1,0.95)
                : lerp(float4(0.30,1,0.48,0.95), float4(1,0.31,0.12,0.95), State[baseRow].w);
        else
            result.color = float4(0.25,0.83,1,0.8);
    }
    return result;
}
