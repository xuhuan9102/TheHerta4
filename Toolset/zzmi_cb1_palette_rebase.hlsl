Buffer<float4> Pool : register(t0);
Buffer<float4> SourceCB : register(t1);
Buffer<float4> OwnerCB : register(t2);
Buffer<uint4> Range : register(t3);
RWBuffer<float4> Output : register(u0);

[numthreads(64, 1, 1)]
void main(uint3 dispatch_id : SV_DispatchThreadID)
{
    uint4 range = Range[0];
    uint offset = dispatch_id.x;
    if (offset >= range.y)
        return;

    uint first = (range.x + offset) * 3;
    float4 row0 = Pool[first];
    float4 row1 = Pool[first + 1];
    float4 row2 = Pool[first + 2];
    if (range.z == 0)
    {
        float4x4 source_to_world = float4x4(SourceCB[0], SourceCB[1], SourceCB[2], SourceCB[3]);
        // Some render passes put other data in the inverse rows of CB1.
        float3 a = OwnerCB[0].xyz;
        float3 b = OwnerCB[1].xyz;
        float3 c = OwnerCB[2].xyz;
        float3 cofactor0 = cross(b, c);
        float determinant = dot(a, cofactor0);
        if (!isfinite(determinant) || abs(determinant) < 1e-8f)
            return;

        float3x3 inverse = transpose(float3x3(cofactor0, cross(c, a), cross(a, b))) / determinant;
        float3 translation = -mul(OwnerCB[3].xyz, inverse);
        float4x4 world_to_owner = float4x4(
            float4(inverse[0], 0), float4(inverse[1], 0),
            float4(inverse[2], 0), float4(translation, 1)
        );
        float4x4 skin = transpose(float4x4(row0, row1, row2, float4(0, 0, 0, 1)));
        float4x4 corrected = transpose(mul(skin, mul(source_to_world, world_to_owner)));
        row0 = corrected[0];
        row1 = corrected[1];
        row2 = corrected[2];
    }
    Output[first] = row0;
    Output[first + 1] = row1;
    Output[first + 2] = row2;
}
