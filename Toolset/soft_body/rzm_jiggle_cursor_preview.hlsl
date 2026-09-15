// Visual-only bridge. Physics owns JiggleScreenState. This Present pass must
// never sample a component's VB0 or BakeRT: those resources can still be
// bound by the game's draw path. Hover is live from the detector; capture is
// deliberately a stable snapshot until a separately synchronised reprojection
// pass is introduced.
Buffer<float4> JiggleScreenState : register(t67);
StructuredBuffer<float4> PinnedDetectInfo : register(t68);
RWBuffer<float4> CursorPreview : register(u0);
Texture1D<float4> IniParams : register(t120);

#define CURRENT_CURSOR IniParams[24]

void ApplyCapturedJiggleOffset(inout float4 anchor)
{
    // JiggleScreenState[0] is the actual solved local displacement, not the
    // cursor target. Project it through the frozen right/down local basis so
    // Present follows the deformed point while the mouse is held.
    float3 displacement = JiggleScreenState[0u].xyz;
    float3 rightLocal = JiggleScreenState[6u].xyz;
    float3 downLocal = JiggleScreenState[7u].xyz;
    float rr = dot(rightLocal, rightLocal);
    float rd = dot(rightLocal, downLocal);
    float dd = dot(downLocal, downLocal);
    float determinant = rr * dd - rd * rd;
    if (rr <= 1e-10 || dd <= 1e-10 || determinant <= 1e-12)
        return;

    float dr = dot(displacement, rightLocal);
    float dDown = dot(displacement, downLocal);
    float rightPixels = (dr * dd - dDown * rd) / determinant * anchor.z;
    float downPixels = (dDown * rr - dr * rd) / determinant * anchor.w;

    // ScreenState's saved anchor and downLocal basis already share the same
    // cursor orientation. Do not invert Y again: that made captured hand
    // movement swap top and bottom relative to the actual jiggle vertex.
    anchor.xy += float2(rightPixels, downPixels);
}

[numthreads(1, 1, 1)]
void main(uint3 id : SV_DispatchThreadID)
{
    // [0] anchor xy + source screen size zw
    // [1] projected local X.xy, Y.xy
    // [2] projected local Z.xy, validity in z
    // JiggleScreenState[3].w is a tri-state marker: 0.0 = idle, 0.5 =
    // charging (lone LMB/RMB held, frozen but not a real grab), 1.0 = real
    // locked grab. Charging and real-grab both need the FROZEN basis below
    // (that's the entire point of freezing at click-time instead of
    // resampling live hover for the whole hold) — only idle (< .25) falls
    // through to the live hover/fallback path.
    float4 hit = PinnedDetectInfo[0u];
    float4 payload = PinnedDetectInfo[1u];
    if (JiggleScreenState[3u].w < .25 && hit.x >= 0.0 && hit.y < 1e30 && abs(payload.w - 7.0) < .5)
    {
        CursorPreview[0u] = PinnedDetectInfo[10u];
        CursorPreview[1u] = PinnedDetectInfo[11u];
        CursorPreview[2u] = float4(PinnedDetectInfo[12u].xyz, 1.0); // hover
        CursorPreview[3u] = PinnedDetectInfo[14u];
        return;
    }
    if (JiggleScreenState[3u].w < .25)
    {
        // There is neither a held capture/charge nor a current surface hit.
        // Never reuse an old basis here: it can project a stale hand across
        // the entire backbuffer. The visual gracefully becomes a small
        // screen cursor until the detector finds geometry again.
        CursorPreview[0u] = CURRENT_CURSOR;
        CursorPreview[1u] = 0.0;
        CursorPreview[2u] = 0.0;
        CursorPreview[3u] = 0.0;
        return;
    }
    bool isRealGrab = JiggleScreenState[3u].w > 0.75;
    if (!isRealGrab)
    {
        // Charging: position stays LIVE (tracks the detected point as the
        // camera moves — freezing it too made the hand drift from the body
        // in screen space instead of only its orientation staying fixed),
        // but the projected local axes (orientation) stay FROZEN from the
        // moment the hold started, so the windup rotation isn't fighting
        // live hover jitter. Falls back to the frozen anchor if the live
        // hit blips invalid for a frame (cursor drifted off-target
        // mid-charge), rather than snapping to a generic screen cursor.
        // Status 1.0 (hover-like, not 2.0) keeps the hand's vibration gate
        // off — vibration is only for a real LMB+RMB grab stretch.
        bool liveHitValid = hit.x >= 0.0 && hit.y < 1e30 && abs(payload.w - 7.0) < .5;
        CursorPreview[0u] = liveHitValid ? PinnedDetectInfo[10u] : JiggleScreenState[10u];
        CursorPreview[1u] = JiggleScreenState[11u];
        CursorPreview[2u] = float4(JiggleScreenState[12u].xyz, 1.0);
        CursorPreview[3u] = liveHitValid ? PinnedDetectInfo[14u] : JiggleScreenState[14u];
        return;
    }
    // Real locked grab: position and orientation both stay frozen from the
    // moment of capture, with ApplyCapturedJiggleOffset following the
    // solved displacement so the hand tracks the deformed point while held.
    float4 capturedAnchor = JiggleScreenState[10u];
    ApplyCapturedJiggleOffset(capturedAnchor);
    CursorPreview[0u] = capturedAnchor;
    CursorPreview[1u] = JiggleScreenState[11u];
    CursorPreview[2u] = float4(JiggleScreenState[12u].xyz, 2.0);
    CursorPreview[3u] = JiggleScreenState[14u];
}
