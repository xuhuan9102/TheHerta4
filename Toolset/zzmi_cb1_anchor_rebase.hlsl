Buffer<float4> Owner : register(t0);
Buffer<float4> Reference : register(t1);
Buffer<uint4> Range : register(t3);
RWBuffer<float4> Output : register(u0);

[numthreads(64, 1, 1)]
void main(uint3 id : SV_DispatchThreadID)
{
    uint4 range = Range[0];
    if (id.x >= range.y)
        return;

    uint start = id.x * 4;
    uint anchor = (uint)Reference[start + 3].x * 3;
    float4x4 relative = float4x4(Reference[start], Reference[start + 1], Reference[start + 2], float4(0, 0, 0, 1));
    uint dst = (range.x + id.x) * 3;
    Output[dst] = mul(Owner[anchor], relative);
    Output[dst + 1] = mul(Owner[anchor + 1], relative);
    Output[dst + 2] = mul(Owner[anchor + 2], relative);
}
