// Exact pass-through VS for ZZZ UI layout shader cdc90aee00e7900d.
// This is intentionally the same vertex reconstruction TTL uses before its
// layout GS.  A generic/inherited VS does not provide a reliable
// SV_Position input to that GS.

cbuffer cb3 : register(b3) { float4 cb3[21]; }
cbuffer cb2 : register(b2) { float4 cb2[4]; }
cbuffer cb1 : register(b1) { float4 cb1[7]; }
cbuffer cb0 : register(b0) { float4 cb0[14]; }

void main(
    float4 v0 : POSITION0, float4 v1 : COLOR0, float2 v2 : TEXCOORD0,
    out float4 o0 : SV_POSITION0, out float4 o1 : COLOR0,
    out float4 o2 : TEXCOORD0, out float4 o3 : TEXCOORD1,
    out float4 o4 : TEXCOORD2)
{
    float4 r0, r1;
    r0 = cb2[1] * v0.yyyy;
    r0 = cb2[0] * v0.xxxx + r0;
    r0 = cb2[2] * v0.zzzz + r0;
    r0 = cb2[3] + r0;
    r1 = cb3[18] * r0.yyyy;
    r1 = cb3[17] * r0.xxxx + r1;
    r1 = cb3[19] * r0.zzzz + r1;
    r0 = cb3[20] * r0.wwww + r1;
    o0 = r0;
    o1 = cb0[9] * v1;
    o2 = float4(v2.xy * cb0[12].xy + cb0[12].zw, 0, 0);
    o3 = v0;
    r1.z = cb3[5].x * cb1[6].x;
    r1.w = cb3[6].y * cb1[6].y;
    r0.xy = r0.ww / abs(r1.zw);
    r0.xy = cb0[13].xy * float2(.25, .25) + abs(r0.xy);
    o4.zw = float2(.25, .25) / r0.xy;
    r0 = max(float4(-2e10, -2e10, -2e10, -2e10), cb0[11]);
    r0 = min(float4( 2e10,  2e10,  2e10,  2e10), r0);
    r0.xy = v0.xy * float2(2, 2) - r0.xy;
    o4.xy = r0.xy - r0.zw;
}
