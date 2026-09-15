// rzm_jiggle_interaction.hlsl
// RZMenu / 3DMigoto / XXMI
// Made by: Rayvich
// Polished single-buffer Null-spring jiggle.
// Adapted from jiggle_physx_by_rayvich_ouroboros_targettrail_v05.hlsl
// by Rayvich for RZMenu ecosystem (per-component auto-generation).
//
// - One persistent Ouroboros RWBuffer<float4> at u6.
// - Optional per-vertex seam trace at u4; never feeds the draw path.
// - Detector snapshot is used only to create a grab anchor (center + object ID).
// - During drag, cursor delta maps to a SCREEN-STABLE vb0 basis (+X/+Y),
//   NOT the hit face normal. Face-normal tangent frames flip between nearby
//   triangles on curved surfaces (breast), so the same mouse "up" used to
//   produce left/down/up depending on which face was grabbed.
// - Live raycast is intentionally NOT used (stable outside silhouette).
// - Release uses Verlet-style inertia with a small one-frame release kick.
// - Per-vertex MaskBuffer (t70): 0 = frozen, 1 = free. Export must default 0.
//
// Bindings:
//   t24 = original vb0, read-only, stride 40
//   t67 = captured detector snapshot
//   u5  = copied vb0 output, RWStructuredBuffer<VertexAttributes>, stride 40
//   u6  = persistent jiggle state, RWBuffer<float4>, 9 entries minimum
//   u4  = optional per-vertex diagnostic output, 3 float4 records per vertex
//   t120 / IniParams[26] = same transform profile as detector
//
// IniParams:
//   [67].x = captured cursor X, pixels
//   [67].y = captured cursor Y, pixels
//   [67].w = capture active flag, 1 while mouse is held
//
//   [68].x = radius in vb0 local units, fallback 0.25
//   [68].y = drag strength, fallback 1.00
//   [68].z = falloff power, fallback 1.50
//   [68].w = screen drag scale, fallback 1.00
//
//   [69].x = current cursor X, pixels
//   [69].y = current cursor Y, pixels
//   [69].z = screen width
//   [69].w = screen height
//
//   [70].x = grab velocity damping, fallback 0.86
//   [70].y = grab spring, fallback 0.176       // ~20% slower than v03
//   [70].z = release velocity damping, fallback 0.96
//   [70].w = release spring, fallback 0.055     // ~20% slower than v03
//
//   [71].x = max offset clamp, fallback radius * 2.0
//   [71].y = one-frame release kick, fallback 1.10
//   [71].z = mouse Y direction, fallback +1.0
//            +1.0 = inverted relative to v03, -1.0 = old direction
//   [71].w = target smoothing/follow, fallback 0.12
//            lower = longer rubber delay, higher = snappier
//
//   [101]/[102] = per-zone falloff power override (zones 0-3 / 4-7), 0 = no
//                 override (use JIGGLE_PARAMS.z). Same LOW/HIGH packing as
//                 ZONE_RADIUS/ZONE_STRENGTH/ZONE_OFFSET below.
//
// History layout u6:
//   [0].xyz = current physical offset, vb0 local space
//   [0].w   = state alive flag
//   [1].xyz = previous physical offset, vb0 local space
//   [1].w   = reserved
//   [2].xyz = frozen grab center, vb0 local space
//   [2].w   = captured object ID
//   [3].xyz = frozen grab normal, vb0 local space
//   [3].w   = previous-frame mouse-held flag
//   [4].xyz = filtered/smoothed drag target, vb0 local space
//   [4].w   = reserved
//   [5].xyz = previous filtered target, vb0 local space
//   [5].w   = reserved
//   [6].xyz = frozen screen-right axis, vb0 local space
//   [6].w   = axis-valid flag
//   [7].xyz = frozen screen-down axis, vb0 local space
//   [7].w   = axis-valid flag
//   [8].x   = previous-frame mouse-held flag

struct VertexAttributes
{
    float3 position;
    float3 normal;
    float4 tangent;
};

StructuredBuffer<VertexAttributes> base_buffer : register(t24);
StructuredBuffer<float4> CapturedDetect        : register(t67);
Buffer<float4> ObjParams                       : register(t68);
RWStructuredBuffer<VertexAttributes> rw_buffer  : register(u5);
RWBuffer<float4> JiggleState                    : register(u6);
RWBuffer<float4> DebugBuffer                    : register(u7);
RWBuffer<float4> VertexDebugBuffer              : register(u4);
Buffer<uint4> JiggleZoneIDs                    : register(t65);
Buffer<float4> JiggleZoneWeights               : register(t66);
Buffer<float4> SharedInteractionState           : register(t71);
// Path Slide: static per-zone start->end vectors (core/path_export.py) and
// live per-zone progress (written by rzm_jiggle_screen_state.hlsl).
Buffer<float4> PathVectors                      : register(t73);
Buffer<float> PathProgressState                 : register(t74);
Buffer<float4> ZoneParams                       : register(t75);
// Collision (anti-clipping): baked at export from the collider's vb0 subset.
//   t76 = interleaved point cloud (position float4 + unit normal float4 per point)
//   t77 = fine uniform-grid cells (offset, count) uint4 per cell
//   t78 = coarse uniform-grid cells (centroid.xyz, radius) float4 per cell
//   t79 = per-vertex L0 contact data (q0.xyz,t0)(n0.xyz,valid)
Buffer<float4> ColliderPoints                    : register(t76);
Buffer<uint4>  ColliderCellsFine                : register(t77);
Buffer<float4> ColliderCellsCoarse              : register(t78);
Buffer<float4> ColliderVertexL0                 : register(t79);

Texture1D<float4> IniParams : register(t120);

#define CAPTURED_CURSOR      IniParams[67]
#define JIGGLE_PARAMS        IniParams[68]
#define CURRENT_CURSOR       IniParams[69]
#define PHYS_PARAMS          IniParams[70]
#define POLISH_PARAMS        IniParams[71]
#define JIGGLE_MULTIPLIERS   IniParams[72]
#define JIGGLE_MULT_EXTRA    IniParams[73]
#define DEBUG_PARAMS         IniParams[74]
#define VERTEX_DEBUG_PARAMS  IniParams[75]
#define TIME_PARAMS          IniParams[76]
#define ZONE_RUNTIME_META    IniParams[119]
// Collision params (anti-clipping). Set by the jiggle INI section; zero = disabled.
//   [101]: x=enabled, y=margin, z=mode (0=soft-slide, 1=hard-clamp), w=safety
//   [102]: xyz=bmin, w=h (fine cell size)
//   [103]: xyz=fine grid dims (nx,ny,nz), w=h_c (coarse cell size)
//   [104]: x=drag mode variable, yzw=coarse grid dims (cnx,cny,cnz)
#define COLLISION_PARAMS    IniParams[101]
#define COLLISION_BOX       IniParams[102]
#define COLLISION_GRID      IniParams[103]
#define COLLISION_META      IniParams[104]

#define DETECT_SLOT_ID       0u
#define DETECT_SLOT_HIT      1u
#define DETECT_SLOT_NORMAL   5u
#define DETECT_SLOT_SCREEN_RIGHT 8u
#define DETECT_SLOT_SCREEN_DOWN  9u

float SafePositive(float value, float fallback)
{
    return value > 0.0 ? value : fallback;
}

float SafeNonZero(float value, float fallback)
{
    return abs(value) > 0.00000001 ? value : fallback;
}

float3 SafeNormalize(float3 v, float3 fallback)
{
    float lenSq = dot(v, v);
    if (lenSq <= 0.00000001)
        return fallback;

    return v * rsqrt(lenSq);
}

float4 ReadCaptured(uint slot, float4 fallback)
{
    uint count;
    uint stride;
    CapturedDetect.GetDimensions(count, stride);

    if (slot >= count)
        return fallback;

    return CapturedDetect[slot];
}

float4 ReadState(uint slot, float4 fallback)
{
    uint count;
    JiggleState.GetDimensions(count);

    if (slot >= count)
        return fallback;

    return JiggleState[slot];
}

float SimulationStep()
{
    float dt = SafePositive(TIME_PARAMS.x, 1.0 / 60.0);
    float speed = SafePositive(TIME_PARAMS.y, 1.0);
    float maxStep = SafePositive(TIME_PARAMS.z, 2.0);
    return clamp(dt * 60.0 * speed, 0.05, maxStep);
}

float ZoneRadiusOverride(uint zone)
{
    uint count;
    ZoneParams.GetDimensions(count);
    return zone * 2u < count ? ZoneParams[zone * 2u].x : 0.0;
}

float ZoneStrengthOverride(uint zone)
{
    uint count;
    ZoneParams.GetDimensions(count);
    return zone * 2u < count ? ZoneParams[zone * 2u].y : 0.0;
}

float ZoneOffsetOverride(uint zone)
{
    uint count;
    ZoneParams.GetDimensions(count);
    return zone * 2u < count ? ZoneParams[zone * 2u].z : 0.0;
}

float ZoneFalloffOverride(uint zone)
{
    uint count;
    ZoneParams.GetDimensions(count);
    return zone * 2u < count ? ZoneParams[zone * 2u].w : 0.0;
}

bool ZoneIsPathSlide(uint zone)
{
    uint count;
    ZoneParams.GetDimensions(count);
    return zone * 2u + 1u < count && ZoneParams[zone * 2u + 1u].z > 0.5;
}

// Cheap, wave-uniform (every thread reads the same IniParams-derived value,
// so GPUs can skip the branch's instructions entirely rather than just not
// allocating for them) check for whether this export has ANY Path Slide
// zone at all. Gates VertexOwningZone's mask scan below -- without this,
// every jiggle dispatch paid that cost on every vertex even when nothing
// uses Path Slide (which is every export before this existed).
bool AnyPathSlideZone()
{
    return ZONE_RUNTIME_META.x > 0.5;
}

uint ClampZoneID(float zoneValue)
{
    uint paramCount;
    ZoneParams.GetDimensions(paramCount);
    uint zoneCount = max(paramCount / 2u, 1u);
    return min((uint)max(round(zoneValue), 0.0), zoneCount - 1u);
}

// Which zone does vertex i actually belong to (its strongest sparse weight,
// not the globally-active zone)?
uint VertexOwningZone(uint i, out float ownWeight)
{
    static const uint INVALID_ZONE = 0xffffffffu;
    uint idCount;
    uint weightCount;
    JiggleZoneIDs.GetDimensions(idCount);
    JiggleZoneWeights.GetDimensions(weightCount);
    uint4 ids = i < idCount ? JiggleZoneIDs[i] : uint4(INVALID_ZONE, INVALID_ZONE, INVALID_ZONE, INVALID_ZONE);
    float4 weights = i < weightCount ? JiggleZoneWeights[i] : 0.0f;
    uint zone = 0u;
    ownWeight = 0.0f;
    [unroll]
    for (uint slot = 0u; slot < 4u; ++slot)
    {
        if (ids[slot] != INVALID_ZONE && weights[slot] > ownWeight)
        {
            ownWeight = weights[slot];
            zone = ids[slot];
        }
    }
    return zone;
}

float4 ReadSharedInteraction(uint slot, float4 fallback)
{
    uint count;
    SharedInteractionState.GetDimensions(count);
    return slot < count ? SharedInteractionState[slot] : fallback;
}

// ============================================================
// Detector and jiggle now share vb0 local space directly.
// ============================================================

float3 ToJiggleSpace(float3 p)
{
    return p;
}

// ============================================================
// Offset is already in vb0 local space.
// ============================================================

float3 OffsetToVertexSpace(float3 v)
{
    return v;
}

// ============================================================
// SCREEN-STABLE CURSOR DRAG (face-independent)
// ============================================================

float2 GetScreenDragNormalized(float mouseXDirection, float mouseYDirection)
{
    float2 screenSize = max(CURRENT_CURSOR.zw, float2(1.0, 1.0));
    float screenReference = max(min(screenSize.x, screenSize.y), 1.0);
    float2 deltaPx = CURRENT_CURSOR.xy - CAPTURED_CURSOR.xy;

    return float2(
        deltaPx.x / screenReference * mouseXDirection,
        deltaPx.y / screenReference * mouseYDirection
    );
}

// Map pixel drag → vb0 offset without using the hit face normal.
// Nearby faces on a breast have very different normals; orthonormal frames
// built from them are discontinuous (axes flip), so the same mouse-up used
// to launch geometry left/down/up. Fixed local axes remove that lottery.
//
// Convention (ZZZ/GI upright body, camera-facing):
//   mouse +X → vb0 +X
//   mouse +Y → vb0 +Y
// mouseXDirection / mouseYDirection (IniParams) still flip signs if needed.
float3 BuildScreenStableDrag(float dragScale, float mouseXDirection, float mouseYDirection,
                             float3 screenRightLocal, float3 screenDownLocal, bool basisValid)
{
    float2 delta = GetScreenDragNormalized(mouseXDirection, mouseYDirection) * dragScale;
    return basisValid ? screenRightLocal * delta.x + screenDownLocal * delta.y
                      : float3(delta.x, delta.y, 0.0);
}

float ComputeRubberInfluence(float dist, float radius, float falloffPower)
{
    if (dist >= radius)
        return 0.0;

    float x = 1.0 - saturate(dist / max(radius, 0.000001));

    // Shape the raw linear ratio too, before smoothstepping. Double-smoothstep
    // alone saturates x close to 1.0 for a wide plateau near the touch point,
    // so pow(s, falloffPower) below barely moves there even at large exponents
    // -- raising falloffPower only sharpened the outer transition, never the
    // near-center peak. Normalized so this pre-shape is a no-op (x^1 = x) at
    // the 1.5 default, leaving low falloffPower feel unchanged.
    x = pow(x, max(falloffPower / 1.5, 0.0001));

    // Smoothstep twice: soft edge, rounded center, no linear rubber plank.
    float s = x * x * (3.0 - 2.0 * x);
    s = s * s * (3.0 - 2.0 * s);

    return pow(s, falloffPower);
}

float3 ClampVectorLength(float3 v, float maxLen)
{
    float lenSq = dot(v, v);
    float maxSq = maxLen * maxLen;

    if (lenSq > maxSq && lenSq > 0.00000001)
        return v * (maxLen * rsqrt(lenSq));

    return v;
}

// Progressive resistance: dragDir/dragLen is the raw, UNSCALED screen drag
// (before strength is applied). maxLen is the hard ceiling ("how far this
// can stretch" — the zone's max_offset). rate controls how many maxLen's
// worth of drag it takes to approach that ceiling (this is what "strength"
// now means for grabbing: a bigger rate reaches the limit with less mouse
// movement). Unlike a linear-scale-then-clip, resistance is felt across the
// whole gesture — small drags are still ~1:1, but the closer you are to
// maxLen the less each extra pixel of drag moves you, asymptotically
// approaching (never reaching) maxLen instead of hitting a wall.
float3 PullTowardLimit(float3 dragDir, float rate, float maxLen)
{
    float dragLen = length(dragDir);
    if (dragLen < 0.000001 || maxLen <= 0.0)
        return float3(0.0, 0.0, 0.0);

    float pulledLen = maxLen * (1.0 - exp(-dragLen * rate / maxLen));
    return dragDir * (pulledLen / dragLen);
}

void ComputeNextPhysics(
    bool captureActive,
    bool hasCapturedID,
    float capturedID,
    float3 capturedCenterWorld,
    float3 capturedNormalWorld,
    float radius,
    float strength,
    float dragScale,
    float grabDamping,
    float grabSpring,
    float releaseDamping,
    float releaseSpring,
    float releaseKick,
    float maxOffset,
    float targetFollow,
    float timeStep,
    float mouseXDirection,
    float mouseYDirection,
    float3 screenRightLocal,
    float3 screenDownLocal,
    bool screenBasisValid,
    out float4 outCurrent,
    out float4 outPrevious,
    out float4 outCenter,
    out float4 outNormal,
    out float4 outTarget,
    out float4 outPrevTarget,
    out float4 outRawTarget,
    out float4 outScreenRight,
    out float4 outScreenDown,
    out float4 outInputFlags)
{
    float4 stateCurrent  = ReadState(0u, float4(0.0, 0.0, 0.0, 0.0));
    float4 statePrevious = ReadState(1u, float4(0.0, 0.0, 0.0, 0.0));
    float4 stateCenter   = ReadState(2u, float4(0.0, 0.0, 0.0, -1.0));
    float4 stateNormal   = ReadState(3u, float4(0.0, 0.0, 1.0, 0.0));
    float4 stateTarget   = ReadState(4u, float4(0.0, 0.0, 0.0, 0.0));
    float4 statePrevTgt  = ReadState(5u, float4(0.0, 0.0, 0.0, 0.0));
    float4 stateScreenRight = ReadState(6u, float4(1.0, 0.0, 0.0, 0.0));
    float4 stateScreenDown  = ReadState(7u, float4(0.0, 1.0, 0.0, 0.0));
    float4 stateInputFlags  = ReadState(8u, float4(0.0, 0.0, 0.0, 0.0));

    bool stateAlive = stateCurrent.w > 0.5;
    bool wasCaptureActive = stateNormal.w > 0.5;
    // Per-component copy of the input edge. This prevents a component that
    // was not under the initial click from joining a drag later on.
    bool pressedThisFrame = captureActive && stateInputFlags.x < 0.5;

    // A hit is sampled only when the button goes down.  While it remains down,
    // the detector is deliberately ignored: moving through empty space or a
    // different body part must not replace this component's anchor.
    bool newCapture = pressedThisFrame && hasCapturedID;
    bool lockedCapture = CAPTURED_CURSOR.w > 0.5 && (wasCaptureActive || newCapture);
    bool stateBasisValid = stateScreenRight.w > 0.5 && stateScreenDown.w > 0.5;

    float3 centerWorld = (wasCaptureActive || stateAlive) ? stateCenter.xyz : capturedCenterWorld;
    float3 normalWorld = (wasCaptureActive || stateAlive) ? stateNormal.xyz : capturedNormalWorld;
    float objectID = (wasCaptureActive || stateAlive) ? stateCenter.w : capturedID;
    float3 activeScreenRight = (wasCaptureActive && stateBasisValid) ? stateScreenRight.xyz : screenRightLocal;
    float3 activeScreenDown = (wasCaptureActive && stateBasisValid) ? stateScreenDown.xyz : screenDownLocal;
    bool activeBasisValid = (wasCaptureActive && stateBasisValid) || (!wasCaptureActive && screenBasisValid);

    float3 currentOffset = stateAlive ? stateCurrent.xyz : float3(0.0, 0.0, 0.0);
    float3 previousOffset = stateAlive ? statePrevious.xyz : float3(0.0, 0.0, 0.0);
    float3 filteredTarget = stateAlive ? stateTarget.xyz : float3(0.0, 0.0, 0.0);
    float3 previousTarget = stateAlive ? statePrevTgt.xyz : float3(0.0, 0.0, 0.0);

    if (newCapture)
    {
        centerWorld = capturedCenterWorld;
        normalWorld = capturedNormalWorld;
        objectID = capturedID;
        currentOffset = float3(0.0, 0.0, 0.0);
        previousOffset = float3(0.0, 0.0, 0.0);
        filteredTarget = float3(0.0, 0.0, 0.0);
        previousTarget = float3(0.0, 0.0, 0.0);
    }

    float3 rawTargetOffset = float3(0.0, 0.0, 0.0);
    float spring = releaseSpring;
    float damping = releaseDamping;
    float currentTargetFollow = targetFollow;

    if (lockedCapture)
    {
        // Screen-stable drag (ignores face normal — see BuildScreenStableDrag).
        // normalWorld is still frozen for state/debug, not used for axes.
        // strength is the raw (unscaled) drag direction; PullTowardLimit uses
        // strength as the approach rate toward maxOffset instead of a linear
        // multiplier, so maxOffset governs the whole gesture, not just a
        // rarely-reached tail-end clamp.
        float3 dragDir3D = BuildScreenStableDrag(
            dragScale, mouseXDirection, mouseYDirection,
            activeScreenRight, activeScreenDown, activeBasisValid);
        rawTargetOffset = PullTowardLimit(dragDir3D, strength, maxOffset);
        spring = grabSpring;
        damping = grabDamping;
    }
    else
    {
        currentTargetFollow = targetFollow * 0.55;
    }

    float previousStep = clamp(SafePositive(statePrevious.w, 1.0), 0.05, 8.0);
    float previousTargetStep = clamp(SafePositive(statePrevTgt.w, 1.0), 0.05, 8.0);
    float3 targetVelocity = (filteredTarget - previousTarget) / previousTargetStep;
    previousTarget = filteredTarget;
    float targetFollowStep = 1.0 - pow(saturate(1.0 - currentTargetFollow), timeStep);
    filteredTarget = filteredTarget + targetVelocity * (0.35 * timeStep) + (rawTargetOffset - filteredTarget) * targetFollowStep;
    filteredTarget = ClampVectorLength(filteredTarget, maxOffset);

    float3 targetOffset = filteredTarget;

    float3 velocity = (currentOffset - previousOffset) / previousStep;

    if (!captureActive && wasCaptureActive)
        velocity *= releaseKick;

    velocity *= pow(saturate(damping), timeStep);

    float3 acceleration = (targetOffset - currentOffset) * spring;
    float3 nextOffset = currentOffset + (velocity + acceleration * timeStep) * timeStep;
    nextOffset = ClampVectorLength(nextOffset, maxOffset);

    float3 nextVelocity = nextOffset - currentOffset;

    bool stillAlive = lockedCapture
        || dot(nextOffset, nextOffset) > 0.00000025
        || dot(nextVelocity, nextVelocity) > 0.00000025;

    if (!stillAlive)
    {
        nextOffset = float3(0.0, 0.0, 0.0);
        currentOffset = float3(0.0, 0.0, 0.0);
        filteredTarget = float3(0.0, 0.0, 0.0);
        previousTarget = float3(0.0, 0.0, 0.0);
        rawTargetOffset = float3(0.0, 0.0, 0.0);
        objectID = -1.0;
    }

    outCurrent  = float4(nextOffset, stillAlive ? 1.0 : 0.0);
    outPrevious = float4(currentOffset, timeStep);
    outCenter   = float4(centerWorld, objectID);
    outNormal   = float4(normalWorld, lockedCapture ? 1.0 : 0.0);
    outTarget   = float4(filteredTarget, 0.0);
    outPrevTarget = float4(previousTarget, timeStep);
    outRawTarget = float4(rawTargetOffset, 0.0);
    outScreenRight = float4(newCapture ? screenRightLocal : stateScreenRight.xyz, newCapture && screenBasisValid ? 1.0 : stateScreenRight.w);
    outScreenDown  = float4(newCapture ? screenDownLocal : stateScreenDown.xyz, newCapture && screenBasisValid ? 1.0 : stateScreenDown.w);
    outInputFlags  = float4(captureActive ? 1.0 : 0.0, 0.0, 0.0, 0.0);
}

// ============================================================
// COLLISION (anti-clipping) — mode 2 (drag) only, per-hit vertices
// 设计语义：导出时为每个权重顶点烘焙「到碰撞体表面的带符号距离」t0 作阈值
// （ColliderVertexL0）。运行时任何位移后，顶点到碰撞体表面的距离不得低于 t0：
// 低于则停在阈值处（硬=沿位移路径二分到边界；软=沿表面法线推回、保留切向滑动）。
// 判定用带符号表面距离 s = dot(p - nearestQ, nearestN)（负 = 已穿透）；
// 旧实现用「距最近采样点」的无符号距离 bestD 做触发，稀疏点云下恒放行 → 无效果。
// ============================================================

static const uint COLLISION_K_MAX = 32u;

uint CollisionFineCellIndex(float3 p, int3 dims)
{
    int3 c = int3(floor((p - COLLISION_BOX.xyz) / max(COLLISION_BOX.w, 1e-6)));
    c = clamp(c, int3(0, 0, 0), dims - int3(1, 1, 1));
    return (uint)c.x + (uint)c.y * (uint)dims.x + (uint)c.z * (uint)dims.x * (uint)dims.y;
}

uint CollisionCoarseCellIndex(float3 p, int3 dims)
{
    int3 c = int3(floor((p - COLLISION_BOX.xyz) / max(COLLISION_GRID.w, 1e-6)));
    c = clamp(c, int3(0, 0, 0), dims - int3(1, 1, 1));
    return (uint)c.x + (uint)c.y * (uint)dims.x + (uint)c.z * (uint)dims.x * (uint)dims.y;
}

void CollisionScanCell(uint cellIndex, float3 p, inout float bestD, inout float3 bestN, inout float3 bestQ)
{
    uint4 info = ColliderCellsFine[cellIndex];
    uint offset = info.x;
    uint count = min(info.y, COLLISION_K_MAX);
    [loop]
    for (uint k = 0u; k < count; ++k)
    {
        float4 q = ColliderPoints[(offset + k) * 2u];
        float3 d = p - q.xyz;
        float distSq = dot(d, d);
        if (distSq < bestD * bestD)
        {
            bestD = sqrt(max(distSq, 1e-12));
            bestQ = q.xyz;
            bestN = ColliderPoints[(offset + k) * 2u + 1u].xyz;
        }
    }
}

// 细网格最近点查询：本 cell + 仅必要时（越界轴方向）扩展邻居，最多 8 cell。
void CollisionQueryFine(float3 p, inout float bestD, inout float3 bestN, inout float3 bestQ)
{
    float h = max(COLLISION_BOX.w, 1e-6);
    int3 dims = int3(COLLISION_GRID.xyz);
    float3 base = COLLISION_BOX.xyz;
    float3 coord = floor((p - base) / h);
    int3 c0 = clamp(int3(coord), int3(0, 0, 0), dims - int3(1, 1, 1));

    CollisionScanCell(
        (uint)c0.x + (uint)c0.y * (uint)dims.x + (uint)c0.z * (uint)dims.x * (uint)dims.y,
        p, bestD, bestN, bestQ);

    float3 dNeg = p - (base + coord * h);
    float3 dPos = (base + (coord + 1.0) * h) - p;

    [unroll]
    for (int ox = -1; ox <= 1; ++ox)
    {
        [unroll]
        for (int oy = -1; oy <= 1; ++oy)
        {
            [unroll]
            for (int oz = -1; oz <= 1; ++oz)
            {
                if (ox == 0 && oy == 0 && oz == 0)
                    continue;
                if (ox == -1 && dNeg.x >= bestD) continue;
                if (ox == 1 && dPos.x >= bestD) continue;
                if (oy == -1 && dNeg.y >= bestD) continue;
                if (oy == 1 && dPos.y >= bestD) continue;
                if (oz == -1 && dNeg.z >= bestD) continue;
                if (oz == 1 && dPos.z >= bestD) continue;
                int3 c = clamp(c0 + int3(ox, oy, oz), int3(0, 0, 0), dims - int3(1, 1, 1));
                CollisionScanCell(
                    (uint)c.x + (uint)c.y * (uint)dims.x + (uint)c.z * (uint)dims.x * (uint)dims.y,
                    p, bestD, bestN, bestQ);
            }
        }
    }
}

float3 CollisionSolve(uint i, float3 p0, float3 offset)
{
    float3 p1 = p0 + offset;
    float margin = COLLISION_PARAMS.y;

    // ============================================================
    // 设计语义（逐顶点导出距离阈值）：
    // 导出时对每个权重顶点烘焙「到碰撞体表面的带符号距离」t0（ColliderVertexL0
    // 布局：l0[2i]=(q0.xyz,t0)，l0[2i+1]=(n0.xyz,valid)，仅权重顶点 valid=1）。
    // 运行时任何位移（拖拽/回弹）后，顶点到碰撞体表面的距离不得低于 t0：
    //   低于阈值 → 停在阈值处（硬=沿位移路径二分到边界；软=沿法线推回，保留切向滑动）
    //   不低于阈值 → 放行。
    // ============================================================
    float4 l0q = ColliderVertexL0[i * 2u];
    float4 l0n = ColliderVertexL0[i * 2u + 1u];
    if (l0n.w > 0.5)
    {
        float threshold = l0q.w;   // 导出时刻表面距离 = 最小距离阈值

        // 粗网格下界拒绝：到粗格包络（质心+半径）的距离 > 阈值 ⇒ 必未低于阈值
        // （仅阈值非负时成立；导出已穿透的顶点阈值<0，直接走细查询）
        int3 cdims = int3(COLLISION_META.yzw);
        float4 coarse = ColliderCellsCoarse[CollisionCoarseCellIndex(p1, cdims)];
        float dc = distance(p1, coarse.xyz);
        if (threshold >= 0.0 && dc - coarse.w > threshold)
            return p1;

        // 细网格：当前到碰撞体表面的带符号距离（最近采样点 + 外法线）
        float bestD = 1e30;
        float3 bestN = float3(0.0, 0.0, 1.0);
        float3 bestQ = p1;
        CollisionQueryFine(p1, bestD, bestN, bestQ);
        float s1 = dot(p1 - bestQ, bestN);
        if (s1 >= threshold)
            return p1;   // 未低于阈值 → 放行

        if (COLLISION_PARAMS.z > 0.5)
        {
            // 硬模式：沿位移路径固定 5 次二分到阈值边界（该帧停止，不滑动）
            float tLo = 0.0;
            float tHi = 1.0;
            [unroll]
            for (uint b = 0u; b < 5u; ++b)
            {
                float tMid = (tLo + tHi) * 0.5;
                float3 query = p0 + offset * tMid;
                float d = 1e30;
                float3 n = float3(0.0, 0.0, 1.0);
                float3 q = query;
                CollisionQueryFine(query, d, n, q);
                if (dot(query - q, n) >= threshold)
                    tLo = tMid;
                else
                    tHi = tMid;
            }
            return p0 + offset * tLo;
        }
        // 软模式：沿表面法线推回至阈值，保留切向（贴表滑动）。
        // 二次迭代：最近点/法线随曲面变化，一次投影在曲率处会欠量，再收敛一次。
        float3 clamped = p1 - bestN * (s1 - threshold);
        float d2 = 1e30;
        float3 n2 = float3(0.0, 0.0, 1.0);
        float3 q2 = clamped;
        CollisionQueryFine(clamped, d2, n2, q2);
        float s2 = dot(clamped - q2, n2);
        if (s2 < threshold)
            clamped = clamped - n2 * (s2 - threshold);
        return clamped;
    }

    // ============================================================
    // 回退（无 L0 阈值，防御性）：最近点带符号距离 + margin 净空
    // ============================================================
    float bestD2 = 1e30;
    float3 bestN2 = float3(0.0, 0.0, 1.0);
    float3 bestQ2 = p1;
    CollisionQueryFine(p1, bestD2, bestN2, bestQ2);
    float s2 = dot(p1 - bestQ2, bestN2);
    if (s2 > -margin)
        return p1;
    if (COLLISION_PARAMS.z > 0.5)
    {
        float tLo = 0.0;
        float tHi = 1.0;
        [unroll]
        for (uint b = 0u; b < 5u; ++b)
        {
            float tMid = (tLo + tHi) * 0.5;
            float3 query = p0 + offset * tMid;
            float d = 1e30;
            float3 n = float3(0.0, 0.0, 1.0);
            float3 q = query;
            CollisionQueryFine(query, d, n, q);
            if (dot(query - q, n) >= -margin)
                tLo = tMid;
            else
                tHi = tMid;
        }
        return p0 + offset * tLo;
    }
    return p1 - bestN2 * (s2 + margin);
}

// ============================================================
// MAIN
// ============================================================

[numthreads(256, 1, 1)]
void main(uint3 threadID : SV_DispatchThreadID)
{
    uint i = threadID.x;

    uint vertexCount;
    uint vertexStride;
    rw_buffer.GetDimensions(vertexCount, vertexStride);

    float capturedID = ReadCaptured(DETECT_SLOT_ID, float4(-1.0, 0.0, 0.0, 0.0)).x;
    bool hasCapturedID = capturedID >= 0.0;
    bool mouseHeld = CAPTURED_CURSOR.w > 0.5;

    float4 stateCurrent  = ReadState(0u, float4(0.0, 0.0, 0.0, 0.0));
    float4 stateCenter   = ReadState(2u, float4(0.0, 0.0, 0.0, -1.0));
    bool stateAlive = stateCurrent.w > 0.5;
    float4 sharedCurrent = ReadSharedInteraction(0u, float4(0.0, 0.0, 0.0, 0.0));
    float4 sharedCenter = ReadSharedInteraction(2u, float4(0.0, 0.0, 0.0, -1.0));
    bool sharedAlive = sharedCurrent.w > 0.5 && sharedCenter.w >= 0.0;
    float objectID = sharedAlive ? sharedCenter.w : (stateAlive ? stateCenter.w : capturedID);
    uint activeZone = ClampZoneID(ReadSharedInteraction(9u, float4(0.0, 0.0, 0.0, 0.0)).x);

    bool anyPathSlide = AnyPathSlideZone();
    bool idleFastPath = !sharedAlive
        && !stateAlive
        && !mouseHeld
        && !anyPathSlide
        && DEBUG_PARAMS.x <= 0.5
        && VERTEX_DEBUG_PARAMS.x <= 0.5;

    if (idleFastPath)
    {
        if (i < vertexCount)
            rw_buffer[i] = base_buffer[i];
        return;
    }

    // Default parameters from IniParams/fallbacks
    float radius       = SafePositive(JIGGLE_PARAMS.x, 0.25);
    float strength     = SafeNonZero(JIGGLE_PARAMS.y, 1.0);
    float falloffPower = SafePositive(JIGGLE_PARAMS.z, 1.5);
    float dragScale    = SafePositive(JIGGLE_PARAMS.w, 1.0);

    float grabDamping    = saturate(SafePositive(PHYS_PARAMS.x, 0.86));
    float grabSpring     = SafePositive(PHYS_PARAMS.y, 0.176);
    float releaseDamping = saturate(SafePositive(PHYS_PARAMS.z, 0.96));
    float releaseSpring  = SafePositive(PHYS_PARAMS.w, 0.055);

    float maxOffset  = SafePositive(POLISH_PARAMS.x, radius * 2.0);
    float releaseKick = SafePositive(POLISH_PARAMS.y, 1.18);
    float targetFollow = saturate(SafePositive(POLISH_PARAMS.w, 0.12));
    float mouseXDirection = SafeNonZero(JIGGLE_MULT_EXTRA.y, 1.0);
    float mouseYDirection = SafeNonZero(POLISH_PARAMS.z, 1.0);

    // Override from ObjParams if found
    uint paramCount = 0;
    ObjParams.GetDimensions(paramCount);
    uint objCount = paramCount / 4u;
    float matchedParamIndex = -1.0;

    for (uint o = 0; o < objCount; ++o)
    {
        float4 r0 = ObjParams[o * 4u + 0u];
        if (abs(r0.x - objectID) < 0.5)
        {
            matchedParamIndex = (float)o;
            radius       = r0.y;
            strength     = r0.z;
            falloffPower = r0.w;

            float4 r1 = ObjParams[o * 4u + 1u];
            dragScale    = r1.x;
            grabDamping  = r1.y;
            grabSpring   = r1.z;
            releaseDamping = r1.w;

            float4 r2 = ObjParams[o * 4u + 2u];
            releaseSpring = r2.x;
            releaseKick  = r2.y;
            maxOffset    = r2.z;
            targetFollow = r2.w;

            float4 r3 = ObjParams[o * 4u + 3u];
            mouseYDirection = r3.x != 0.0 ? r3.x : mouseYDirection;
            mouseXDirection = r3.y != 0.0 ? r3.y : mouseXDirection;
            break;
        }
    }

    // Read global multipliers
    // A selected mask zone owns its falloff radius across all components.
    // Zero preserves legacy per-object/default profiles.
    float zoneRadius = ZoneRadiusOverride(activeZone);
    if (zoneRadius > 0.0)
        radius = zoneRadius;
    float zoneStrength = ZoneStrengthOverride(activeZone);
    if (zoneStrength > 0.0)
        strength = zoneStrength;
    float zoneFalloffPower = ZoneFalloffOverride(activeZone);
    if (zoneFalloffPower > 0.0)
        falloffPower = zoneFalloffPower;

    float mult_radius    = JIGGLE_MULTIPLIERS.y;
    float mult_strength  = JIGGLE_MULTIPLIERS.z;
    float mult_spring    = JIGGLE_MULTIPLIERS.w;
    float mult_damping   = JIGGLE_MULT_EXTRA.x;

    // Fallback to 1.0 if completely undefined/unpopulated (all zero)
    if (mult_radius == 0.0 && mult_strength == 0.0 && mult_spring == 0.0 && mult_damping == 0.0)
    {
        mult_radius = 1.0;
        mult_strength = 1.0;
        mult_spring = 1.0;
        mult_damping = 1.0;
    }

    // Apply multipliers
    radius         *= mult_radius;
    strength       *= mult_strength;
    grabSpring     *= mult_spring;
    releaseSpring  *= mult_spring;
    grabDamping    = saturate(grabDamping * mult_damping);
    releaseDamping = saturate(releaseDamping * mult_damping);
    maxOffset      *= mult_radius;
    float zoneMaxOffset = ZoneOffsetOverride(activeZone);
    if (zoneMaxOffset > 0.0)
        maxOffset = zoneMaxOffset;

    float3 capturedCenterWorld = ReadCaptured(
        DETECT_SLOT_HIT,
        float4(0.0, 0.0, 0.0, 0.0)
    ).xyz;

    float3 capturedNormalWorld = ReadCaptured(
        DETECT_SLOT_NORMAL,
        float4(0.0, 0.0, 1.0, 0.0)
    ).xyz;

    capturedNormalWorld = SafeNormalize(capturedNormalWorld, float3(0.0, 0.0, 1.0));

    float4 capturedRight = ReadCaptured(DETECT_SLOT_SCREEN_RIGHT, float4(1.0, 0.0, 0.0, 0.0));
    float4 capturedDown  = ReadCaptured(DETECT_SLOT_SCREEN_DOWN,  float4(0.0, 1.0, 0.0, 0.0));
    bool screenBasisValid = capturedRight.w > 0.5
        && all(abs(capturedRight.xyz) < float3(1e5, 1e5, 1e5))
        && all(abs(capturedDown.xyz) < float3(1e5, 1e5, 1e5));

    float4 nextCurrent;
    float4 nextPrevious;
    float4 nextCenter;
    float4 nextNormal;
    float4 nextTarget;
    float4 nextPrevTarget;
    float4 nextRawTarget;
    float4 nextScreenRight;
    float4 nextScreenDown;
    float4 nextInputFlags;

    if (sharedAlive)
    {
        nextCurrent = sharedCurrent;
        nextPrevious = ReadSharedInteraction(1u, float4(0.0, 0.0, 0.0, 0.0));
        nextCenter = sharedCenter;
        nextNormal = ReadSharedInteraction(3u, float4(0.0, 0.0, 1.0, 0.0));
        nextTarget = ReadSharedInteraction(4u, float4(0.0, 0.0, 0.0, 0.0));
        nextPrevTarget = ReadSharedInteraction(5u, float4(0.0, 0.0, 0.0, 0.0));
        nextRawTarget = nextTarget;
        nextScreenRight = ReadSharedInteraction(6u, float4(1.0, 0.0, 0.0, 0.0));
        nextScreenDown = ReadSharedInteraction(7u, float4(0.0, 1.0, 0.0, 0.0));
        nextInputFlags = float4(mouseHeld ? 1.0 : 0.0, 0.0, 0.0, 0.0);
    }
    else
    {
        ComputeNextPhysics(
            mouseHeld,
            hasCapturedID,
            capturedID,
            capturedCenterWorld,
            capturedNormalWorld,
            radius,
            strength,
            dragScale,
            grabDamping,
            grabSpring,
            releaseDamping,
            releaseSpring,
            releaseKick,
            maxOffset,
            targetFollow,
            SimulationStep(),
            mouseXDirection,
            mouseYDirection,
            capturedRight.xyz,
            capturedDown.xyz,
            screenBasisValid,
            nextCurrent,
            nextPrevious,
            nextCenter,
            nextNormal,
            nextTarget,
            nextPrevTarget,
            nextRawTarget,
            nextScreenRight,
            nextScreenDown,
            nextInputFlags
        );
    }

    // Per-component resolved input/output evidence.  This is intentionally
    // tiny and contains no VB0 payload: it proves whether a shared snapshot
    // is valid in this component's local coordinate system.
    if (i == 0u && DEBUG_PARAMS.x > 0.5)
    {
        uint debugCount;
        DebugBuffer.GetDimensions(debugCount);
        if (debugCount >= 15u)
        {
            DebugBuffer[0u]  = float4(2.0, capturedID, hasCapturedID ? 1.0 : 0.0, mouseHeld ? 1.0 : 0.0);
            DebugBuffer[1u]  = float4(nextCenter.xyz, nextNormal.w);
            DebugBuffer[2u]  = nextScreenRight;
            DebugBuffer[3u]  = nextScreenDown;
            DebugBuffer[4u]  = float4(objectID, stateAlive ? 1.0 : 0.0, matchedParamIndex, (float)vertexCount);
            DebugBuffer[5u]  = float4(radius, strength, falloffPower, maxOffset);
            DebugBuffer[6u]  = float4(grabDamping, grabSpring, releaseDamping, releaseSpring);
            DebugBuffer[7u]  = float4(mouseXDirection, mouseYDirection, dragScale, targetFollow);
            DebugBuffer[8u]  = nextCurrent;
            DebugBuffer[9u]  = nextPrevious;
            DebugBuffer[10u] = nextCenter;
            DebugBuffer[11u] = nextNormal;
            DebugBuffer[12u] = nextTarget;
            DebugBuffer[13u] = nextPrevTarget;
            DebugBuffer[14u] = nextRawTarget;
        }
    }

    // Ouroboros update: one tiny persistent buffer. No ping-pong.
    if (i == 0u)
    {
        JiggleState[0u] = nextCurrent;
        JiggleState[1u] = nextPrevious;
        JiggleState[2u] = nextCenter;
        JiggleState[3u] = nextNormal;
        JiggleState[4u] = nextTarget;
        JiggleState[5u] = nextPrevTarget;
        JiggleState[6u] = nextScreenRight;
        JiggleState[7u] = nextScreenDown;
        JiggleState[8u] = nextInputFlags;
    }

    if (i >= vertexCount)
        return;

    VertexAttributes v = base_buffer[i];
    float3 localPos = ToJiggleSpace(v.position);
    uint idCount = 0;
    uint weightCount = 0;
    JiggleZoneIDs.GetDimensions(idCount);
    JiggleZoneWeights.GetDimensions(weightCount);
    uint4 sparseIDs = i < idCount
        ? JiggleZoneIDs[i]
        : uint4(0xffffffffu, 0xffffffffu, 0xffffffffu, 0xffffffffu);
    float4 sparseWeights = i < weightCount ? JiggleZoneWeights[i] : 0.0f;
    float mask = 0.0f;
    [unroll]
    for (uint maskSlot = 0u; maskSlot < 4u; ++maskSlot)
    {
        if (sparseIDs[maskSlot] == activeZone)
            mask = max(mask, sparseWeights[maskSlot]);
    }
    mask = saturate(mask);
    float dist = distance(localPos, nextCenter.xyz);
    // A Path Slide zone never gets spring-physics deformation, even on the
    // frame it happens to be the active zone (it still gets its own rigid
    // offset below, applied independently of activeZone entirely).
    bool activeIsPathSlide = ZoneIsPathSlide(activeZone);
    float influence = (!activeIsPathSlide && nextCurrent.w > 0.5) ? ComputeRubberInfluence(dist, radius, falloffPower) * mask : 0.0;
    float3 appliedOffset = influence > 0.0 ? OffsetToVertexSpace(nextCurrent.xyz * influence) : float3(0.0, 0.0, 0.0);

    // Path Slide: driven by this vertex's OWN owning zone, not activeZone --
    // so it keeps applying every frame regardless of what else is currently
    // being interacted with (see PathProgressState's persistence comment in
    // rzm_jiggle_screen_state.hlsl). No falloff/radius: every vertex in the
    // zone gets the exact same offset, weighted only by its own paint weight.
    float3 pathOffset = float3(0.0, 0.0, 0.0);
    if (AnyPathSlideZone())
    {
        float ownWeight = 0.0;
        uint ownZone = VertexOwningZone(i, ownWeight);
        if (ownWeight > 1e-4 && ZoneIsPathSlide(ownZone))
        {
            uint pathProgressCount;
            PathProgressState.GetDimensions(pathProgressCount);
            float progress = ownZone < pathProgressCount ? PathProgressState[ownZone] : 0.0;
            float4 pathVec = PathVectors[ownZone];
            if (pathVec.w > 0.5 && progress > 0.0)
                pathOffset = OffsetToVertexSpace(pathVec.xyz * progress) * saturate(ownWeight);
        }
    }
    bool hasPathOffset = dot(pathOffset, pathOffset) > 1e-12;

    // Debug records are derived values, not a dumped VB0:
    //   3*i+0 = local position + mask
    //   3*i+1 = distance, influence, applied offset length, state-alive
    //   3*i+2 = applied local offset (Jiggle + Path Slide combined)
    if (VERTEX_DEBUG_PARAMS.x > 0.5)
    {
        uint debugBase = i * 3u;
        VertexDebugBuffer[debugBase + 0u] = float4(localPos, mask);
        VertexDebugBuffer[debugBase + 1u] = float4(dist, influence, length(appliedOffset + pathOffset), nextCurrent.w);
        VertexDebugBuffer[debugBase + 2u] = float4(appliedOffset + pathOffset, 0.0);
    }

    if ((nextCurrent.w < 0.5 || mask <= 1e-4) && !hasPathOffset)
    {
        rw_buffer[i] = v;
        return;
    }

    if (influence > 0.0)
    {
        v.position += appliedOffset;
    }
    if (hasPathOffset)
    {
        v.position += pathOffset;
    }

    // 碰撞检测（防穿模）：仅在拉扯模式（模式 2）且该顶点确实被移动时。
    // 门控为 wave-uniform（COLLISION_PARAMS.x / COLLISION_META.x 是统一参数），
    // 空闲/非拉扯帧整波跳过，零成本。
    if (COLLISION_PARAMS.x > 0.5 && COLLISION_META.x >= 2.0 && (influence > 0.0 || hasPathOffset))
    {
        float3 totalOffset = v.position - localPos;
        v.position = CollisionSolve(i, localPos, totalOffset);
    }

    rw_buffer[i] = v;
}
