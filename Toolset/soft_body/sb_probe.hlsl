// sb_probe.hlsl — 哨兵探针（2026-09-15）
//
// 目的：**用一个不可能出现的数值**，从帧转储里直接判定两件事：
//   ① 我们挂在绘制段里的 CS 到底有没有被派发；
//   ② 我们把 CS 输出换绑成 vb0 的写法到底有没有生效（出图那次绘制用的是不是它）。
// 判据：抓帧后看 vb0 里 vid=0 那个顶点的 TANGENT.w 是不是 12345.000000。
//   有  → 派发 ✓ 且换绑 ✓（那么 palette 版失效的原因就不在这条链路上）
//   无  → 这条路本身没通，先修它
//
// 为什么选 tangent.w：它只影响那一个顶点的副切线方向（视觉上几乎不可见），
// 又足够特殊（正常值是 ±1），在转储里一眼就能认出来。

struct VertexAttributes
{
    float3 pos;
    float3 nrm;
    float4 tan;
};

RWStructuredBuffer<VertexAttributes> vb : register(u5);

[numthreads(64, 1, 1)]
void main(uint3 tid : SV_DispatchThreadID)
{
    uint vid = tid.x;
    uint count = 0;
    uint stride = 0;
    vb.GetDimensions(count, stride);
    if (vid >= count)
    {
        return;
    }
    VertexAttributes v = vb[vid];
    if (vid == 0)
    {
        v.tan.w = 12345.0;      // 哨兵
    }
    vb[vid] = v;
}
