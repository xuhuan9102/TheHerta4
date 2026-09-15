// Direct TTL layout hijack adapted for Object API.
// t0 is the UI draw's live texture and t67 is the one throttled character RT
// snapshot.  No ps-tN guessing and no per-draw copy are involved.

#define COLS 8u
struct VS_OUT { float4 pos : SV_Position; };
struct GS_OUT { float4 pos : SV_Position; float4 data : TEXCOORD0; };

Texture2D<float4> UiTexture : register(t0);
Texture2D<float4> ViewportSource : register(t67);
Texture1D<float4> IniParams : register(t120);
#define PROBE_GENERATION IniParams[88].x

float2 ClipToUV(float4 clip)
{
    float w = max(abs(clip.w), 1e-6);
    float2 ndc = clip.xy / w;
    return float2(ndc.x * .5 + .5, .5 - ndc.y * .5);
}
float3 LoadRGB(Texture2D<float4> tex, float2 uv, uint w, uint h)
{
    uint2 p = uint2(min((uint)(uv.x * w), w - 1u), min((uint)(uv.y * h), h - 1u));
    return tex.Load(int3(p, 0)).rgb;
}
float Similarity(uint w, uint h, out float uiLuma)
{
    const float2 probes[8] = { float2(.50,.40), float2(.50,.55), float2(.50,.70), float2(.35,.50),
        float2(.65,.50), float2(.40,.35), float2(.60,.35), float2(.50,.48) };
    float sim = 0.0; uiLuma = 0.0;
    [unroll] for (uint i = 0u; i < 8u; ++i) {
        float3 a = LoadRGB(UiTexture, probes[i], w, h);
        // The captured character RT can be stored Y-flipped relative to the
        // UI texture (the display page renders the portrait upside-down into
        // its own RT and the UI blit unflips it later). Score BOTH source
        // orientations and keep the better one, so the probe works on the
        // portrait page as well as on any upright layout.
        float3 bN = LoadRGB(ViewportSource, probes[i], w, h);
        float3 bF = LoadRGB(ViewportSource, float2(probes[i].x, 1.0 - probes[i].y), w, h);
        float sN = 1.0 - saturate(dot(abs(a - bN), 1.0 / 3.0));
        float sF = 1.0 - saturate(dot(abs(a - bF), 1.0 / 3.0));
        sim += max(sN, sF);
        uiLuma += dot(a, float3(.2126,.7152,.0722));
    }
    uiLuma *= .125;
    return sim * .125;
}

#ifdef GEOMETRY_SHADER
void EmitTexel(uint col, float4 data, inout TriangleStream<GS_OUT> stream)
{
    float x0 = -1.0 + 2.0 * ((float)col / 8.0);
    float x1 = -1.0 + 2.0 * ((float)(col + 1u) / 8.0);
    GS_OUT o; o.data = data;
    o.pos=float4(x0,1,0,1); stream.Append(o); o.pos=float4(x1,1,0,1); stream.Append(o);
    o.pos=float4(x0,-1,0,1); stream.Append(o); o.pos=float4(x1,-1,0,1); stream.Append(o);
    stream.RestartStrip();
}
[maxvertexcount(28)]
void main(triangle VS_OUT input[3], uint primID : SV_PrimitiveID, inout TriangleStream<GS_OUT> stream)
{
    uint uiW, uiH, sourceW, sourceH;
    UiTexture.GetDimensions(uiW, uiH); ViewportSource.GetDimensions(sourceW, sourceH);
    if (uiW < 2u || uiH < 2u || sourceW < 2u || sourceH < 2u || uiW != sourceW || uiH != sourceH) return;
    float uiLuma; float sim = Similarity(uiW, uiH, uiLuma);
    // Same conservative floor as TTL. The stronger 0.85 threshold remains in
    // the decoder, so a borderline record is diagnostic-only.
    if (sim < .55 || uiLuma < .08 || primID > 1u) return;
    [unroll] for (uint i=0u; i<3u; ++i)
        EmitTexel(primID * 3u + i, float4(ClipToUV(input[i].pos), 1, 1), stream);
    if (primID == 0u) {
        EmitTexel(6u, float4(1.0, (float)uiW, (float)uiH, PROBE_GENERATION), stream);
        EmitTexel(7u, float4(sim, sim, uiLuma, 1.0), stream);
    }
}
#endif
#ifdef PIXEL_SHADER
void main(GS_OUT input, out float4 target : SV_Target0) { target = input.data; }
#endif
