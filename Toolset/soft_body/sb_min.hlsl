// sb_min.hlsl — 最小二分着色器（2026-09-15）
//
// 目的：用与 sb_solve **完全相同**的绑定（t24 基座 / u5 顶点 / u6 状态）做最小事情，
// 以判定"dispatch 被丢"到底出在绑定还是出在 sb_solve.hlsl 本身。
// 判据：抓帧后看 vb0 里 vid=0 的 tangent.z 是否变成 7777（是 ⇒ 派发与写回都通）。

struct VertexAttributes
{
    float3 pos;
    float3 nrm;
    float4 tan;
};

Buffer<float> base_data : register(t24);
RWStructuredBuffer<VertexAttributes> out_buffer : register(u5);
RWStructuredBuffer<float4> sb_state : register(u6);

[numthreads(64, 1, 1)]
void main(uint3 tid : SV_DispatchThreadID)
{
    uint vid = tid.x;
    uint count = 0;
    uint stride = 0;
    out_buffer.GetDimensions(count, stride);
    if (vid >= count)
    {
        return;
    }
    VertexAttributes v = out_buffer[vid];
    sb_state[vid] = float4(1.0, 2.0, 3.0, 4.0);   // 顺便确认 u6 可写
    if (vid == 0)
    {
        v.tan.z = 7777.0;                          // 哨兵（无害，只占一个分量的值）
    }
    out_buffer[vid] = v;
}
