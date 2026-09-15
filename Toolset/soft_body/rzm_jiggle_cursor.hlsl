// Present-only visualizer for the frozen jiggle capture point.
// t67 is ResourceLLJiggleCursorPreview: visual state only.
Buffer<float4> JiggleState : register(t67);
Texture1D<float4> IniParams : register(t120);

struct VSOut { float4 pos : SV_Position; float2 uv : TEXCOORD0; float2 ax : TEXCOORD1; float2 ay : TEXCOORD2; float2 az : TEXCOORD3; float valid : TEXCOORD4; float status : TEXCOORD5; };

#ifdef VERTEX_SHADER
void main(out VSOut output, uint vertex : SV_VertexID)
{
    float4 anchor = JiggleState[0u];
    float4 fallback = IniParams[24];
    bool hasAnchor = anchor.z > 0.0 && anchor.w > 0.0;
    float2 size = hasAnchor ? anchor.zw : max(fallback.zw, float2(1.0, 1.0));
    float2 center = hasAnchor ? anchor.xy : fallback.xy;
    // Keep the diagnostic visible. A point pinned to an edge/corner means the
    // upstream viewport/capture coordinates are suspect; disappearing would
    // hide exactly the failure this cursor is meant to expose.
    float2 halfSize = float2(26.0, 26.0);
    center = clamp(center, halfSize, max(size - halfSize, halfSize));
    float4 xy = JiggleState[1u];
    float4 z = JiggleState[2u];
    output.valid = z.z > 0.5 ? 1.0 : 0.0;
    output.status = z.w;
    float2 pixelScale = float2(size.x, -size.y);
    float2 xAxis = xy.xy * pixelScale;
    float2 yAxis = xy.zw * pixelScale;
    float2 zAxis = z.xy * pixelScale;
    output.ax = dot(xAxis,xAxis) > 1e-8 ? normalize(xAxis) * 21.0 : float2(21,0);
    output.ay = dot(yAxis,yAxis) > 1e-8 ? normalize(yAxis) * 21.0 : float2(0,21);
    output.az = dot(zAxis,zAxis) > 1e-8 ? normalize(zAxis) * 21.0 : float2(15,15);
    float2 corner = float2((vertex == 0u || vertex == 1u) ? 1.0 : 0.0,
                           (vertex == 1u || vertex == 3u) ? 1.0 : 0.0);
    float2 p = center + (corner - 0.5) * 52.0;
    // $cursorY is already bottom-origin after CommandListLLCursorUpdate.
    // Present clip Y uses the same orientation here; do not invert it again.
    output.pos = float4(p.x / size.x * 2.0 - 1.0, p.y / size.y * 2.0 - 1.0, 0.0, 1.0);
    output.uv = corner;
}
#endif

#ifdef PIXEL_SHADER
void main(VSOut input, out float4 result : SV_Target0)
{
    float2 p = input.uv - 0.5;
    p *= 52.0;
    float a = 0.0;
    float3 color = 0.0;
    float2 ax = input.valid > 0.5 ? input.ax : float2(21,0);
    float2 ay = input.valid > 0.5 ? input.ay : float2(0,21);
    float2 az = input.valid > 0.5 ? input.az : float2(15,15);
    float d;
    d = abs(ax.x*p.y-ax.y*p.x) / max(length(ax),1e-5); if (d < 1.3 && dot(p,ax) > 0 && dot(p,ax) < dot(ax,ax)) { color=float3(1,.1,.1); a=1; }
    d = abs(ay.x*p.y-ay.y*p.x) / max(length(ay),1e-5); if (d < 1.3 && dot(p,ay) > 0 && dot(p,ay) < dot(ay,ay)) { color=float3(.1,1,.2); a=1; }
    d = abs(az.x*p.y-az.y*p.x) / max(length(az),1e-5); if (d < 1.3 && dot(p,az) > 0 && dot(p,az) < dot(az,az)) { color=float3(.15,.35,1); a=1; }
    if (dot(p,p) < 5.0) {
        // cyan = hover detector, white = captured/live, yellow = fallback.
        color = input.status > 1.5 ? 1.0 : (input.status > .5 ? float3(.1,1,1) : float3(1,.8,.1));
        a = 1;
    }
    if (a < 0.5) discard;
    result = float4(color, 0.9);
}
#endif
