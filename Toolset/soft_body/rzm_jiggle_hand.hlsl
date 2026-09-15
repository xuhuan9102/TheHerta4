// Independent Present hand: P3F_C4F, no UV/skinning dependency.
// It is a 2.5D overlay: the object API supplies the local XYZ -> screen
// differential. That preserves the cursor's orientation and automatically
// shrinks distant surfaces without sampling the game's depth buffer in Present.
Buffer<float4> Preview : register(t67);
Buffer<float4> ScreenState : register(t68);
// Baked smooth per-vertex normal (see HandNoAction_Normal.buf/HandAction_Normal.buf):
// this mesh's real VB has no normal attribute and no shared vertices (each
// triangle owns unique verts), so this was precomputed offline by averaging
// face normals across vertices that sit at the same 3D position — restoring
// the topology the flat export discarded. Index-aligned with vb0/POSITION.
Buffer<float3> SmoothNormal : register(t69);
Texture1D<float4> IniParams : register(t120);
struct VSIn { float3 position : POSITION; float4 color : COLOR0; uint vertexID : SV_VertexID; };
struct Out { float4 pos:SV_Position; float4 color:COLOR0; float surface: TEXCOORD1; float hit: TEXCOORD2; float3 normal: TEXCOORD3; };

#define SURFACE_PROXY IniParams[83]
// .xyz = offset (in the remapped `hand` local space below) that recenters
// the mesh's authored bounding-box middle onto the cursor, instead of the
// mesh's local origin (which sits near the wrist, not the palm center).
// Computed once from the actual HandNoAction.buf/HandAction.buf bounds.
#define HAND_CENTER IniParams[89]
// .x = overall hand visual scale multiplier, fallback 0.65.
// .y = overall opacity multiplier, fallback 1.0 (solid).
#define HAND_SCALE IniParams[90]
// .x = LMB-alone hold progress, 0 (just pressed) -> 1 (held >= its Windup
//      Time), 0 when not lone-holding LMB. .y = time in seconds, for the
//      vibration phase. .z = same hold progress but for RMB-alone (its own,
//      independent Windup Time).
#define HAND_STATE IniParams[91]
// .x = LMB windup min angle in degrees, already applied the instant LMB is
// pressed (a floor, not a threshold). .y = RMB windup min angle in degrees,
// same idea, fully independent of LMB's. .z = LMB windup max angle in
// degrees, reached after its Windup Time seconds held (rotates around local
// Z). .w = RMB windup max angle in degrees, same idea (rotates around local
// X instead) — independently flippable, doesn't have to match LMB's
// direction.
#define HAND_TILT IniParams[92]
// .x = stretch fraction where vibration starts (e.g. 0.5). .y = seconds
// between shakes at that threshold (slow). .z = seconds between shakes at
// 100% stretch (fast). .w = peak screen-pixel jitter amplitude.
#define HAND_VIBRATE IniParams[93]
// .x = max degrees the hand's up direction may roll away from vertical
// before being clamped back — prevents the surface-basis projection from
// ever flipping the hand upside-down on steeply-angled geometry.
// .y = unused/reserved. .z = LMB+RMB grab tilt's own max angle in degrees,
// fallback 45 -- shares HAND_TILT.x (LMB's min angle) as its floor at zero
// stretch. Unrelated to roll stabilization; packed here because HAND_TILT
// (register 92) was already full.
#define HAND_UPRIGHT IniParams[94]
// .x = screen height (px) the size clamp below was tuned at, fallback 2160.
#define HAND_REFERENCE IniParams[95]
// .xyz = fake-shading light direction, in the mesh's own authored local
// space (NOT screen/world space — it rotates along with the hand, like a
// light rigged to the cursor rather than the scene). .w = ambient floor,
// 0..1: 0 = full contrast (shadowed faces can go black), 1 = flat/no shading.
#define HAND_SHADE IniParams[96]
// .x = outline width in screen pixels at the reference resolution
// (HAND_REFERENCE.x), fallback 2.0. .y = pass flag: > 0.5 means this draw
// call is the enlarged flat-colored outline shell instead of the normal
// shaded hand -- both CustomShader blocks bind the same VS/PS, this is how
// they pick which behavior to run. .z = outline opacity multiplier,
// fallback 1.0.
#define HAND_OUTLINE IniParams[98]

float SafePositive(float v, float fallback) { return v > 0.0 ? v : fallback; }
// Like SafePositive but keeps a negative value instead of discarding it —
// used for the tilt angle, where the sign is a deliberate direction flip.
float SafeNonZero(float v, float fallback) { return abs(v) > 1e-6 ? v : fallback; }

#ifdef VERTEX_SHADER
void main(VSIn i, out Out o)
{
    float4 anchor = Preview[0];
    float2 screen = max(anchor.zw, float2(1.0, 1.0));
    float2 pixelScale = float2(screen.x, -screen.y);

    // These are screen pixels per one unit in the detector's local VB0 space.
    float2 axisX = Preview[1].xy * pixelScale;
    float2 axisY = Preview[1].zw * pixelScale;
    float2 axisZ = Preview[2].xy * pixelScale;
    bool validBasis = Preview[2].z > 0.5 && Preview[2].w > 0.5 &&
        (dot(axisX, axisX) + dot(axisY, axisY) + dot(axisZ, axisZ)) > 1e-6;

    // Keep each screen direction but use a single bounded scale. Raw axis
    // lengths are unreliable on a stale/fallback hit and used to explode the
    // hand into long triangles. Their shared magnitude remains our depth cue.
    // The clamp bounds are in screen pixels, so without scaling them by
    // resolution the hand stays a near-constant PIXEL size while the
    // character's own on-screen size grows with resolution — making the
    // hand look comically large at 1080p and undersized at 4K. Scale the
    // bounds by current height vs. the reference height they were tuned at.
    float referenceHeight = SafePositive(HAND_REFERENCE.x, 2160.0);
    float resolutionScale = screen.y / referenceHeight;
    float lenX = max(length(axisX), 1e-5);
    float lenY = max(length(axisY), 1e-5);
    float lenZ = max(length(axisZ), 1e-5);
    float perspectiveScale = clamp((lenX + lenY + lenZ) / 3.0, 55.0 * resolutionScale, 300.0 * resolutionScale);
    axisX /= lenX;
    axisY /= lenY;
    axisZ /= lenZ;

    float uprightMaxDeg = clamp(SafePositive(HAND_UPRIGHT.x, 80.0), 1.0, 179.0);
    float uprightMaxRad = radians(uprightMaxDeg);
    float rawRollAngle = atan2(axisY.x, -axisY.y);

    // BuildGizmoAxes (the detector, rzm_object_detect.hlsl) derives axisY --
    // the hand's projected "up"/finger tangent -- from a fixed local
    // reference axis via cross(localRef, normal). That's correct and
    // continuous almost everywhere, but on a body whose front and back
    // share an axis, the true surface normal legitimately flips sign
    // between them, and because the tangent formula multiplies by normal,
    // this tangent flips too -- a full half-turn, not a gradual roll. That
    // flip is an artifact of the construction, not a genuine change of
    // "which way is up": the bitangent (width/thumb) axis does NOT flip --
    // it's a fixed point of the same triple product -- so this needs to
    // undo ONLY axisY. Rolling the whole frame here instead (like the
    // steep-curvature clamp below) would also rotate the palm-normal axis,
    // canceling ITS own correct front/back flip, and would drag the
    // already-fine thumb axis into the exact "sideways"/pointing-down mess
    // this is meant to fix. Threshold is a fixed 90 degrees (not
    // uprightMaxDeg) since this is a factual "closer to up or down"
    // question, independent of how much cosmetic lean the user tolerates.
    if (abs(rawRollAngle) > radians(90.0))
    {
        axisY = -axisY;
        rawRollAngle = atan2(axisY.x, -axisY.y);
    }

    // Remaining roll clamp: genuine steep/curved-geometry lean is a true
    // continuous rotation of the whole surface-attached frame, so this DOES
    // rotate all three axes together -- snap to the mirrored lean once past
    // the threshold instead of pinning at the max angle while the surface
    // keeps rotating underneath it. Deliberate discontinuity, not a smooth
    // wrap.
    float clampedRollAngle = rawRollAngle;
    if (rawRollAngle > uprightMaxRad)
        clampedRollAngle = -uprightMaxRad;
    else if (rawRollAngle < -uprightMaxRad)
        clampedRollAngle = uprightMaxRad;
    float rollCorrection = clampedRollAngle - rawRollAngle;
    float crc = cos(rollCorrection);
    float src = sin(rollCorrection);
    axisX = float2(axisX.x * crc - axisX.y * src, axisX.x * src + axisX.y * crc);
    axisY = float2(axisY.x * crc - axisY.y * src, axisY.x * src + axisY.y * crc);
    axisZ = float2(axisZ.x * crc - axisZ.y * src, axisZ.x * src + axisZ.y * crc);

    // HAND_EXP basis: Z is finger length, Y is palm normal and X is width.
    // API basis: green/Y is upward finger direction, red/X is palm-facing
    // direction, blue/Z supplies the remaining width/depth rotation.
    // Post-remap: hand.x = palm-normal, hand.y = finger-length, hand.z = width.
    float3 hand = float3(-i.position.y, -i.position.z, i.position.x);

    // LMB poke windup: tilt the hand back the longer LMB is held alone —
    // rotates hand.x (palm-normal) into hand.y (finger-length) while
    // hand.z (width) stays fixed, i.e. a true rotation around the hand's
    // own local Z axis, like a wrist winding back before a slap. Applied
    // here (before recentering) so it pivots at the wrist — the mesh's
    // natural local origin — rather than the recentered palm point.
    //
    // This used to be a flat 2D screen-space rotation instead: the surface
    // basis (axisX/Y/Z above) was being re-sampled fresh from live hover
    // detection every frame during a charge (a lone LMB hold never locks/
    // captures), so a local-space rotation combined with that per-frame
    // jitter didn't read as a clean, consistent windup. Now that the basis
    // is frozen at the moment the hold starts (see rzm_jiggle_screen_state.hlsl's
    // isCharging/wasCharging) instead of resampled live, local-space
    // rotation is stable again and pivots correctly at the wrist.
    // Flip the sign of $ll_jiggle_hand_tilt_max_deg if this spins the wrong way.
    float lmbHoldFraction = saturate(HAND_STATE.x);
    if (lmbHoldFraction > 0.0)
    {
        float minDeg = SafePositive(HAND_TILT.x, 10.0);
        float maxDeg = SafeNonZero(HAND_TILT.z, 45.0);
        float tiltRad = radians(lerp(minDeg, maxDeg, lmbHoldFraction));
        float ct = cos(tiltRad);
        float st = sin(tiltRad);
        float rotX = hand.x * ct + hand.y * st;
        float rotY = -hand.x * st + hand.y * ct;
        hand.x = rotX;
        hand.y = rotY;
    }

    // RMB poke windup: same idea, but rotates hand.y (finger-length) into
    // hand.z (width) while hand.x (palm-normal) stays fixed — a rotation
    // around the hand's local X axis instead of Z, so push reads visually
    // distinct from pull. Fully independent min/time/max from LMB above
    // (flip $ll_jiggle_hand_tilt_rmb_max_deg if it spins the wrong way).
    float rmbHoldFraction = saturate(HAND_STATE.z);
    if (rmbHoldFraction > 0.0)
    {
        float minDeg = SafePositive(HAND_TILT.y, 10.0);
        float maxDeg = SafeNonZero(HAND_TILT.w, 45.0);
        float tiltRad = radians(lerp(minDeg, maxDeg, rmbHoldFraction));
        float ct = cos(tiltRad);
        float st = sin(tiltRad);
        float rotY = hand.y * ct + hand.z * st;
        float rotZ = -hand.y * st + hand.z * ct;
        hand.y = rotY;
        hand.z = rotZ;
    }

    // LMB+RMB grab tilt: while genuinely captured (real grab, not just a
    // lone-hold charge), wind the hand back around the same local Z axis as
    // the LMB poke windup, but driven by how far the drag has actually been
    // stretched (0..1 of that zone's max_offset) instead of hold time — pull
    // it further from the grab origin, the more it tilts back. Shares LMB's
    // min angle as its floor at zero stretch; independent max angle so grab
    // tilt can feel different from a poke's windup.
    bool captured = Preview[2].w > 1.5;
    float stretchFraction = captured ? ScreenState[8u].z : 0.0;
    if (captured && stretchFraction > 0.0)
    {
        float minDeg = SafePositive(HAND_TILT.x, 10.0);
        float maxDeg = SafeNonZero(HAND_UPRIGHT.z, 45.0);
        // Config (Min/Max Angle) stays positive for a natural "bigger =
        // more tilt" feel -- negated only here because the grab pose draws
        // the HandAction mesh, which winds back visually opposite the LMB
        // poke windup's HandNoAction mesh for the same positive angle.
        float tiltRad = -radians(lerp(minDeg, maxDeg, stretchFraction));
        float ct = cos(tiltRad);
        float st = sin(tiltRad);
        float rotX = hand.x * ct + hand.y * st;
        float rotY = -hand.x * st + hand.y * ct;
        hand.x = rotX;
        hand.y = rotY;
    }

    // Recenter the mesh's authored bounding-box middle onto the cursor
    // instead of its local origin, which sits near the wrist — without
    // this, the cursor anchors to the wrist end and the hand trails off it.
    hand += HAND_CENTER.xyz;
    // Positive local X is the red normal-facing direction. Move the whole
    // visual hand a little outside the surface before its soft plane test.
    hand.x += SURFACE_PROXY.y;
    float handScale = SafePositive(HAND_SCALE.x, 0.65);
    float visualScale = 0.72 * handScale;
    float2 localOffset;
    if (validBasis)
        localOffset = (hand.x * axisX + hand.y * axisY + hand.z * axisZ) * (perspectiveScale * visualScale);
    else
        // Safe, finite fallback: a normal screen cursor instead of a giant
        // overlay when no detector surface is available.
        localOffset = float2(hand.x, hand.z) * 190.0 * handScale;

    // Inverted-hull outline: push this vertex outward in screen space along
    // its own baked normal, projected through the same detected local basis
    // as position above -- consistent with how position itself is mapped,
    // just applied to the normal instead. Unlike position this is NOT
    // scaled by perspectiveScale/visualScale: the push is a fixed pixel
    // width (resolution-scaled) so the rim reads as a constant thickness
    // regardless of the hand's current on-screen size. Uses the same
    // un-windup-rotated normal as the PS shading below (see its comment) --
    // during a windup pose the push direction can be a little off from the
    // true current silhouette near the wrist pivot, an acceptable trade for
    // not duplicating the whole rotation pipeline for a ~2px cosmetic line.
    if (validBasis && HAND_OUTLINE.y > 0.5)
    {
        float3 outlineRawNormal = SmoothNormal[i.vertexID];
        float3 outlineNormal = float3(-outlineRawNormal.y, -outlineRawNormal.z, outlineRawNormal.x);
        float2 normal2D = outlineNormal.x * axisX + outlineNormal.y * axisY + outlineNormal.z * axisZ;
        float normalLen = length(normal2D);
        float2 pushDir = normalLen > 1e-5 ? normal2D / normalLen : float2(0.0, 0.0);
        float outlineWidth = SafePositive(HAND_OUTLINE.x, 2.0) * resolutionScale;
        localOffset += pushDir * outlineWidth;
    }

    // LMB+RMB grab exertion: once the drag passes the vibrate threshold
    // (stretch fraction, 0..1 of that zone's max_offset), shake faster the
    // closer it gets to the hard limit. Silent below the threshold.
    // (captured/stretchFraction already computed above for the grab tilt.)
    float vibThreshold = saturate(SafePositive(HAND_VIBRATE.x, 0.5));
    if (captured && stretchFraction > vibThreshold)
    {
        float t = saturate((stretchFraction - vibThreshold) / max(1.0 - vibThreshold, 1e-4));
        float periodSlow = SafePositive(HAND_VIBRATE.y, 0.8);
        float periodFast = SafePositive(HAND_VIBRATE.z, 0.1);
        float period = lerp(periodSlow, periodFast, t);
        float amplitude = SafePositive(HAND_VIBRATE.w, 4.0) * t;
        float phase = HAND_STATE.y / max(period, 1e-4);
        float2 shake = float2(sin(phase * 6.2831853), sin(phase * 1.7 * 6.2831853 + 1.3));
        localOffset += shake * amplitude;
    }

    float2 p = anchor.xy + localOffset;
    o.pos = float4(p.x / screen.x * 2.0 - 1.0, p.y / screen.y * 2.0 - 1.0, 0.0, 1.0);
    o.color = i.color;
    o.surface = hand.x;
    // Same axis remap applied to position above, applied to the baked
    // smooth normal — a pure permutation+sign-flip, so no separate inverse-
    // transpose handling is needed. Left un-normalized on purpose: linear
    // interpolation across the triangle shortens it slightly, and
    // renormalizing per-pixel in the PS is what actually gives the smooth
    // (Gouraud/Phong-ish) shading instead of a flat per-triangle facet.
    float3 rawNormal = SmoothNormal[i.vertexID];
    o.normal = float3(-rawNormal.y, -rawNormal.z, rawNormal.x);
    // No detector surface under the cursor: hide entirely instead of the old
    // fallback generic screen-cursor shape.
    o.hit = validBasis ? 1.0 : 0.0;
}
#endif
#ifdef PIXEL_SHADER
void main(Out i, out float4 o:SV_Target0)
{
    if (i.hit < 0.5) discard;
    float alpha = i.color.a * saturate(SafePositive(HAND_SCALE.y, 1.0));
    if (SURFACE_PROXY.x > .5)
    {
        // Signed local plane approximation: negative values are embedded in
        // the mesh. A soft edge avoids a hard raster seam while preserving a
        // future path for a true per-pixel depth proxy.
        float softness = max(SURFACE_PROXY.z, 1e-4);
        alpha *= smoothstep(-softness, softness, i.surface);
    }
    if (HAND_OUTLINE.y > 0.5)
    {
        // Outline shell: flat, unlit color instead of the smooth-shaded
        // hand -- this draw call is only the enlarged silhouette meant to
        // peek out from behind the real hand (drawn after this one), so no
        // per-pixel normal lighting is needed here.
        alpha *= saturate(SafePositive(HAND_OUTLINE.z, 1.0));
        if (alpha <= 1e-4) discard;
        o = float4(0.0, 0.0, 0.0, alpha);
        return;
    }
    if (alpha <= 1e-4) discard;

    // Smooth shading from the baked per-vertex normal (see SmoothNormal
    // above), linearly interpolated across the triangle by the rasterizer
    // and renormalized here — real Gouraud/Phong-ish shading instead of one
    // flat facet per triangle. Lit against a light direction fixed in the
    // mesh's own local space, so the shading stays consistent regardless of
    // how the hand is currently reoriented on screen.
    float3 faceNormal = normalize(i.normal);
    float3 rawLightDir = HAND_SHADE.xyz;
    float3 lightDir = normalize(length(rawLightDir) > 1e-5 ? rawLightDir : float3(0.4, 0.6, 0.65));
    float ambient = saturate(SafePositive(HAND_SHADE.w, 0.55));
    float ndotl = saturate(dot(faceNormal, lightDir));
    float shade = lerp(ambient, 1.0, ndotl);

    o = float4(i.color.rgb * shade, alpha);
}
#endif
