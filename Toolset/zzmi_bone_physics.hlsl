// Real-time capsule bodies for the already resolved bone palette.
Buffer<float4> ObjectCB : register(t96);
Buffer<float4> Capsules : register(t97);
Buffer<int> Followers : register(t98);
Buffer<float> AirDrag : register(t99);
Buffer<float4> JointOptions : register(t108);
Texture1D<float4> IniParams : register(t120);
RWBuffer<float4> State : register(u6);
RWBuffer<float> Palette : register(u7);

#ifndef SSMT_PHYSICS_BODIES
#define SSMT_PHYSICS_BODIES 32
#endif
#ifndef SSMT_PHYSICS_LINKS
#define SSMT_PHYSICS_LINKS 32
#endif
static const uint MaxBodies = SSMT_PHYSICS_BODIES;
static const uint MaxSoftLinks = SSMT_PHYSICS_LINKS;
static const uint ActorRows = 6;
static const float SolverParamsTag = 27148.0;
static const float AirParamsTag = 27149.0;
static const float JointParamsTag = 27150.0;

struct Body
{
    float3 position;
    float4 rotation;
    float3 velocity;
    float3 angular;
    float3 dampingReferenceVelocity;
    float3 target;
    float4 targetRotation;
    float3 localAxis;
    float3 head;
    float inverseMass;
    float3 inverseInertia;
    float radius;
    float halfSegment;
    int parent;
    uint mode;
    float damping;
    float stiffness;
    float friction;
    float airDrag;
    float gravityScale;
    float angleLimit;
    float twistLimit;
    float recoveryGap;
    bool sweep;
    float3 boxHalf;
    float boxCorner;
    float3 boxSide;
    float contact;
    float3 contactForce;
    bool blockDeepOverlap;
    float3 contactCorrection;
};

#if SSMT_PHYSICS_BODIES > 32
// Dense rigs would exceed the DX11 per-thread temporary register limit.
groupshared Body bodies[MaxBodies];
#endif

float3 rotate(float4 q, float3 v)
{
    return v + 2.0 * cross(q.xyz, cross(q.xyz, v) + q.w * v);
}

float4 conjugate(float4 q)
{
    return float4(-q.xyz, q.w);
}

float4 multiply(float4 a, float4 b)
{
    return float4(a.w * b.xyz + b.w * a.xyz + cross(a.xyz, b.xyz),
                  a.w * b.w - dot(a.xyz, b.xyz));
}

float4 basis_rotation(float3 x, float3 y)
{
    x = normalize(x);
    y = normalize(y - x * dot(x, y));
    float3 z = cross(x, y);
    float4 q;
    float trace = x.x + y.y + z.z;
    if (trace > 0.0)
        q = float4(y.z - z.y, z.x - x.z, x.y - y.x, 1.0 + trace);
    else if (x.x > y.y && x.x > z.z)
        q = float4(1.0 + x.x - y.y - z.z, x.y + y.x, x.z + z.x, y.z - z.y);
    else if (y.y > z.z)
        q = float4(x.y + y.x, 1.0 + y.y - x.x - z.z, y.z + z.y, z.x - x.z);
    else
        q = float4(x.z + z.x, y.z + z.y, 1.0 + z.z - x.x - y.y, x.y - y.x);
    return normalize(q);
}

float3 rotation_error(float4 target, float4 current)
{
    float4 delta = multiply(target, conjugate(current));
    if (delta.w < 0.0)
        delta = -delta;
    float lengthV = length(delta.xyz);
    return lengthV > 1e-6 ? delta.xyz * (2.0 * atan2(lengthV, delta.w) / lengthV) : 2.0 * delta.xyz;
}

float4 advance_rotation(float4 rotation, float3 displacement)
{
    float angle = length(displacement);
    if (angle < 1e-6)
        return rotation;
    float halfAngle = 0.5 * angle;
    return normalize(multiply(float4(displacement * (sin(halfAngle) / angle), cos(halfAngle)), rotation));
}

float2 capsule_inverse_inertia(float mass, float radius, float halfSegment)
{
    // Sum a cylinder and two hemispheres about the capsule center of mass.
    float crown = mass * ((2.0 / 3.0) * radius) / (halfSegment + (2.0 / 3.0) * radius);
    float cylinder = mass - crown;
    float r2 = radius * radius;
    float axial = (0.5 * cylinder + 0.4 * crown) * r2;
    float transverse = cylinder * (0.25 * r2 + halfSegment * halfSegment / 3.0)
        + crown * (0.4 * r2 + halfSegment * halfSegment + 0.75 * halfSegment * radius);
    return rcp(max(float2(axial, transverse), 1e-8));
}

float3 axis_of(Body body)
{
    return rotate(body.rotation, body.localAxis);
}

float3x3 box_axes(Body body)
{
    float3 y = axis_of(body);
    float3 side = rotate(body.rotation, body.boxSide);
    float3 x = normalize(side - y * dot(side, y));
    return float3x3(x, y, cross(x, y));
}

float3 inverse_inertia(Body body, float3 load)
{
    if (body.mode != 2)
        return 0.0;
    if (body.boxHalf.x > 0.0)
    {
        float3x3 axes = box_axes(body);
        return mul(mul(axes, load) * body.inverseInertia, axes);
    }
    float3 axis = axis_of(body);
    return body.inverseInertia.y * load
        + axis * dot(load, axis) * (body.inverseInertia.x - body.inverseInertia.y);
}

void closest_points(float3 a, float3 aEnd, float3 b, float3 bEnd,
                    out float3 nearA, out float3 nearB)
{
    float3 u = aEnd - a, v = bEnd - b, w = a - b;
    float uu = dot(u, u), vv = dot(v, v), uv = dot(u, v);
    float uw = dot(u, w), vw = dot(v, w);
    float s = 0.0, t = 0.0;
    float determinant = uu * vv - uv * uv;
    if (uu < 1e-10)
        t = vv < 1e-10 ? 0.0 : saturate(vw / vv);
    else if (vv < 1e-10)
        s = saturate(-uw / uu);
    else
    {
        s = determinant > 1e-10 ? saturate((uv * vw - uw * vv) / determinant) : 0.0;
        t = (uv * s + vw) / vv;
        if (t < 0.0 || t > 1.0)
        {
            t = saturate(t);
            s = saturate((uv * t - uw) / uu);
        }
    }
    nearA = a + u * s;
    nearB = b + v * t;
}

float3 parallel_contact(float3 pa, float3 pb, float3 offset, float3 u, float3 v)
{
    float3 midpoint = (pa + pb) * 0.5;
    if (dot(u,u) < 1e-10 || dot(v,v) < 1e-10)
        return midpoint;
    float3 axisA = normalize(u), axisB = normalize(v);
    float alignment = dot(axisA, axisB);
    float parallel = smoothstep(0.92, 0.995, abs(alignment));
    if (parallel == 0.0)
        return midpoint;
    // Use the middle of the shared span when closest points can slide along parallel axes.
    float3 axis = normalize(axisA + (alignment < 0.0 ? -axisB : axisB));
    float center = dot(offset, axis);
    float halfA = abs(dot(u, axis)), halfB = abs(dot(v, axis));
    float low = max(-halfA, center - halfB), high = min(halfA, center + halfB);
    if (low >= high)
        return midpoint;
    float middle = (low + high) * 0.5;
    float3 pA = axisA * clamp(middle / dot(axisA, axis), -length(u), length(u));
    float3 pB = offset + axisB * clamp((middle - center) / dot(axisB, axis), -length(v), length(v));
    return lerp(midpoint, (pA + pB) * 0.5, parallel);
}

void apply_contact_impulse(inout Body a, inout Body b, float3 leverA, float3 leverB,
                           float3 normal, float depth, float fieldRadius, float strength, float stepTime)
{
    bool block = a.blockDeepOverlap || b.blockDeepOverlap;
    float response = smoothstep(0.0, 0.25, depth);
    if (!block)
        response *= 1.0 - smoothstep(0.25, 0.5, depth);
    float3 relative = a.velocity + cross(a.angular, leverA) - b.velocity - cross(b.angular, leverB);
    float reducedMass = rcp(a.inverseMass + b.inverseMass);
    float closingSpeed = min(0.0, dot(relative, normal));
    float normalForce = min(strength * 2.0, strength - 14.0 * reducedMass * closingSpeed) * response;
    float3 tangent = relative - normal * dot(relative, normal);
    float tangentSpeed = length(tangent);
    float tangentForce = min(sqrt(a.friction * b.friction) * normalForce, tangentSpeed * reducedMass / stepTime);
    float3 impulse = stepTime * (normalForce * normal - tangentForce * tangent / max(tangentSpeed, 1e-6));
    a.contactForce += impulse / stepTime;
    b.contactForce -= impulse / stepTime;
    a.velocity += impulse * a.inverseMass;
    b.velocity -= impulse * b.inverseMass;
    a.angular += inverse_inertia(a, cross(leverA, impulse));
    b.angular -= inverse_inertia(b, cross(leverB, impulse));
    if (a.contact >= 0.0) a.contact = max(a.contact, response);
    if (b.contact >= 0.0) b.contact = max(b.contact, response);
    if (block)
    {
        // Correct deep overlap separately from velocity so the panel cannot launch out of the contact.
        float distance = max(0.0, depth - 0.25) * fieldRadius;
        float3 correction = normal * (0.7 * distance * reducedMass);
        a.contactCorrection += correction * a.inverseMass;
        b.contactCorrection -= correction * b.inverseMass;
    }
}

float apply_capsule_contact(inout Body a, inout Body b, float strength, float stepTime)
{
    float3 u = axis_of(a) * a.halfSegment;
    float3 v = axis_of(b) * b.halfSegment;
    float3 offset = b.position - a.position;
    float sumRadius = a.radius + b.radius;
    float3 gap = max(abs(offset) - abs(u) - abs(v), 0.0);
    if (dot(gap, gap) >= sumRadius * sumRadius)
        return 0.0;
    float3 pa, pb;
    closest_points(-u, u, offset-v, offset+v, pa, pb);
    float3 delta = pa - pb;
    float distance = length(delta);
    float penetration = sumRadius - distance;
    float radius = a.mode != 2 ? a.radius : b.mode != 2 ? b.radius : min(a.radius, b.radius);
    float depth = penetration / radius;
    if (penetration <= 0.0 || strength <= 0.0)
        return max(0.0, depth);
    if (depth >= 0.5 && !a.blockDeepOverlap && !b.blockDeepOverlap)
    {
        a.contact = -1.0;
        b.contact = -1.0;
        return depth;
    }
    float offsetLength = length(offset);
    float3 normal = distance > 1e-6 ? delta / distance
        : offsetLength > 1e-6 ? -offset / offsetLength : float3(1, 0, 0);
    float3 contact = parallel_contact(pa, pb, offset, u, v) + 0.5 * (b.radius - a.radius) * normal;
    apply_contact_impulse(a, b, contact, contact - offset, normal, depth, radius, strength, stepTime);
    return depth;
}

float box_distance(float3 location, float3 halfExtent)
{
    float3 delta = abs(location) - halfExtent;
    return length(max(delta, 0.0)) + min(0.0, max(delta.x, max(delta.y, delta.z)));
}

float apply_box_contact(inout Body capsule, inout Body box, float strength, float stepTime)
{
    float3x3 axes = box_axes(box);
    float3 center = mul(axes, capsule.position - box.position);
    float3 axis = mul(axes, axis_of(capsule) * capsule.halfSegment);
    float3 gap = max(abs(center) - abs(axis) - box.boxHalf, 0.0);
    if (dot(gap, gap) >= capsule.radius * capsule.radius)
        return 0.0;

    // Distance to a convex rounded box is convex along the capsule segment.
    float3 core = box.boxHalf - box.boxCorner;
    float low = -1.0, high = 1.0;
    [loop] for (uint i = 0; i < 12u; ++i)
    {
        float left = (2.0 * low + high) / 3.0;
        float right = (low + 2.0 * high) / 3.0;
        float first = box_distance(center + left * axis, core);
        float second = box_distance(center + right * axis, core);
        if (abs(first - second) < 1e-6)
        {
            low = left;
            high = right;
        }
        else if (first < second)
            high = right;
        else
            low = left;
    }
    float3 location = center + (0.5 * (low + high)) * axis;
    float3 outside = location - clamp(location, -core, core);
    float outsideLength = length(outside);
    float penetration = capsule.radius + box.boxCorner - box_distance(location, core);
    float fieldRadius = capsule.mode == 2 ? min(box.boxHalf.x, box.boxHalf.z) : capsule.radius;
    float depth = penetration / fieldRadius;
    if (penetration <= 0.0 || strength <= 0.0)
        return max(0.0, depth);
    if (depth >= 0.5 && !capsule.blockDeepOverlap && !box.blockDeepOverlap)
    {
        capsule.contact = -1.0;
        box.contact = -1.0;
        return depth;
    }
    float3 normal;
    if (outsideLength > 1e-6)
        normal = outside / outsideLength;
    else
    {
        float3 distanceToFace = core - abs(location);
        float3 signs = 2.0 * step(0.0, location) - 1.0;
        if (distanceToFace.x <= min(distanceToFace.y, distanceToFace.z))
            normal = float3(signs.x, 0, 0);
        else if (distanceToFace.y <= distanceToFace.z)
            normal = float3(0, signs.y, 0);
        else
            normal = float3(0, 0, signs.z);
    }
    float3 leverBox = mul(location + normal * (0.5 * penetration - capsule.radius), axes);
    float3 leverCapsule = box.position + leverBox - capsule.position;
    apply_contact_impulse(capsule, box, leverCapsule, leverBox, mul(normal, axes),
                          depth, fieldRadius, strength, stepTime);
    return depth;
}

bool separating_axis(float3 axis, float3 delta, float3x3 axesA, float3 coreA, float3x3 axesB, float3 coreB,
                     float rounding, inout float bestSeparation, inout float3 bestNormal)
{
    float length2 = dot(axis, axis);
    if (length2 < 1e-8)
        return false;
    axis *= rsqrt(length2);
    float distance = dot(delta, axis);
    float separation = abs(distance)
        - dot(abs(mul(axesA, axis)), coreA)
        - dot(abs(mul(axesB, axis)), coreB);
    if (separation >= rounding)
        return true;
    if (separation > bestSeparation)
    {
        bestSeparation = separation;
        bestNormal = distance < 0.0 ? -axis : axis;
    }
    return false;
}

float3 closest_box_point(float3 fromCenter, float3x3 axes, float3 core)
{
    return mul(clamp(mul(axes, fromCenter), -core, core), axes);
}

float3 box_feature(float3 fromCenter, float3 direction, float3x3 axes, float3 core)
{
    float3 local = clamp(mul(axes, fromCenter), -core, core);
    float3 normal = mul(axes, direction);
    float3 towards = (2.0 * step(0.0, normal) - 1.0) * core;
    // Keep nearly parallel contact coordinates inside the face instead of switching corners.
    return mul(lerp(local, towards, smoothstep(0.0, 0.075, abs(normal))), axes);
}

float apply_box_pair(inout Body a, inout Body b, float strength, float stepTime)
{
    float3 delta = a.position - b.position;
    float reach = length(a.boxHalf) + length(b.boxHalf);
    if (dot(delta, delta) >= reach * reach)
        return 0.0;

    float3x3 axesA = box_axes(a), axesB = box_axes(b);
    float3 coreA = a.boxHalf - a.boxCorner, coreB = b.boxHalf - b.boxCorner;
    float rounding = a.boxCorner + b.boxCorner;
    float separation = -1e20;
    float3 normal = 0.0;
    [loop] for (uint i = 0; i < 3u; ++i)
    {
        if (separating_axis(axesA[i], delta, axesA, coreA, axesB, coreB, rounding, separation, normal))
            return 0.0;
        if (separating_axis(axesB[i], delta, axesA, coreA, axesB, coreB, rounding, separation, normal))
            return 0.0;
        [loop] for (uint j = 0; j < 3u; ++j)
            if (separating_axis(cross(axesA[i], axesB[j]), delta, axesA, coreA, axesB, coreB,
                                rounding, separation, normal))
                return 0.0;
    }

    float3 pointA = box_feature(-0.5 * delta, -normal, axesA, coreA);
    float3 pointB = box_feature(0.5 * delta, normal, axesB, coreB);
    if (separation > 0.0)
    {
        // Refine the gap between disjoint cores to account for rounded corners.
        [loop] for (uint iteration = 0; iteration < 8u; ++iteration)
        {
            pointB = closest_box_point(delta + pointA, axesB, coreB);
            pointA = closest_box_point(pointB - delta, axesA, coreA);
        }
        float3 gap = delta + pointA - pointB;
        float distance = length(gap);
        if (distance >= rounding)
            return 0.0;
        if (distance > 1e-6)
            normal = gap / distance;
        separation = distance;
    }
    float3 colliderHalf = a.mode == 2 ? b.boxHalf : a.boxHalf;
    float radius = min(colliderHalf.x, colliderHalf.z);
    float depth = (rounding - separation) / radius;
    if (strength <= 0.0)
        return depth;
    if (depth >= 0.5 && !a.blockDeepOverlap && !b.blockDeepOverlap)
    {
        a.contact = -1.0;
        b.contact = -1.0;
        return depth;
    }
    float3 leverB = 0.5 * (delta + pointA + pointB + normal * (b.boxCorner - a.boxCorner));
    apply_contact_impulse(a, b, leverB - delta, leverB, normal, depth, radius, strength, stepTime);
    return depth;
}

float apply_contact(inout Body a, inout Body b, float strength, float stepTime)
{
    if (a.boxHalf.x > 0.0 && b.boxHalf.x > 0.0)
        return apply_box_pair(a, b, strength, stepTime);
    if (a.boxHalf.x > 0.0)
        return apply_box_contact(b, a, strength, stepTime);
    if (b.boxHalf.x > 0.0)
        return apply_box_contact(a, b, strength, stepTime);
    return apply_capsule_contact(a, b, strength, stepTime);
}

Body swept_pose(Body collider, float3 previous, float4 previousRotation, float3 turn, float fraction)
{
    collider.position = lerp(previous, collider.position, fraction);
    collider.rotation = advance_rotation(previousRotation, fraction * turn);
    return collider;
}

void sample_swept_contact(inout Body collider, inout Body dynamic, float3 previous, float4 previousRotation,
                          float endDepth, float strength, float stepTime)
{
    float3 travel = collider.position - previous;
    float3 turn = rotation_error(collider.rotation, previousRotation);
    float reach = collider.boxHalf.x > 0.0 ? length(collider.boxHalf) : collider.radius + collider.halfSegment;
    if (length(travel) + length(turn) * reach < 0.2 * collider.radius)
        return;

    float fraction = 0.0;
    if (endDepth >= 0.5)
    {
        // A fast thigh can cross the force band between two substeps. Apply the
        // ordinary soft force at entry and leave sustained deep overlap alone.
        Body start = swept_pose(collider, previous, previousRotation, turn, 0.0);
        float depth = apply_contact(start, dynamic, 0.0, stepTime);
        if (depth >= 0.5)
            return;
        if (depth < 0.25)
        {
            float low = 0.0, high = 1.0;
            [loop] for (uint i = 0; i < 5u; ++i)
            {
                fraction = 0.5 * (low + high);
                Body probe = swept_pose(collider, previous, previousRotation, turn, fraction);
                if (apply_contact(probe, dynamic, 0.0, stepTime) < 0.25)
                    low = fraction;
                else
                    high = fraction;
            }
            fraction = 0.5 * (low + high);
        }
    }
    else
    {
        float distance2 = dot(travel, travel);
        fraction = distance2 > 1e-8 ? dot(dynamic.position - previous, travel) / distance2 : 0.5;
        if (fraction <= 0.0 || fraction >= 1.0)
            return;
    }
    Body sample = swept_pose(collider, previous, previousRotation, turn, fraction);
    if (apply_contact(sample, dynamic, strength, stepTime) > 0.0)
    {
        if (endDepth == 0.0)
            collider.contact = sample.contact;
        collider.contactForce = sample.contactForce;
    }
}

bool pins_translation(float settings)
{
    return settings == 1.0 || settings <= -2.0;
}

float3 joint_response(Body child, Body parent, float3 armChild, float3 armParent, float3 impulse)
{
    return (child.inverseMass + parent.inverseMass) * impulse
        + cross(inverse_inertia(child, cross(armChild, impulse)), armChild)
        + cross(inverse_inertia(parent, cross(armParent, impulse)), armParent);
}

bool solve_joint(inout Body child, inout Body parent, inout float3 predicted, float sizeScale)
{
    float3 localChild = rotate(conjugate(child.targetRotation), child.head - child.target);
    float3 localParent = rotate(conjugate(parent.targetRotation), child.head - parent.target);
    float3 armChild = rotate(child.rotation, localChild);
    float3 armParent = rotate(parent.rotation, localParent);
    float3 gap = child.position + armChild - parent.position - armParent;
    float distance = length(gap);
    if (distance < 1e-6)
        return false;

    bool recovered = child.recoveryGap > 0.0 && distance > child.recoveryGap;
    if (recovered)
    {
        // Bound recovery at this anchor and exclude it from the next velocity update.
        float excess = min(distance - child.recoveryGap, 0.12 * sizeScale);
        float3 shift = gap * (excess / distance);
        child.position -= shift;
        predicted -= shift;
        gap -= shift;
        distance -= excess;
    }

    // An impulse along one axis can move the joint along all three. Solve the coupled response.
    float3 x = joint_response(child, parent, armChild, armParent, float3(1, 0, 0));
    float3 y = joint_response(child, parent, armChild, armParent, float3(0, 1, 0));
    float3 z = joint_response(child, parent, armChild, armParent, float3(0, 0, 1));
    float3 yz = cross(y, z), zx = cross(z, x), xy = cross(x, y);
    float determinant = dot(x, yz);
    float3 correction = gap * (min(distance, 0.06) / distance);
    float3 impulse = determinant > 1e-12
        ? float3(dot(yz, correction), dot(zx, correction), dot(xy, correction)) / determinant
        : 0.0;
    child.position -= impulse * child.inverseMass;
    parent.position += impulse * parent.inverseMass;
    child.rotation = advance_rotation(child.rotation, -inverse_inertia(child, cross(armChild, impulse)));
    parent.rotation = advance_rotation(parent.rotation, inverse_inertia(parent, cross(armParent, impulse)));
    return recovered;
}

void limit_joint_rotation(inout Body child, Body parent)
{
    float4 goal = multiply(multiply(parent.rotation, conjugate(parent.targetRotation)), child.targetRotation);
    if (child.twistLimit < 0.0)
    {
        float3 error = rotation_error(child.rotation, goal);
        float angle = length(error);
        if (angle > child.angleLimit)
            child.rotation = advance_rotation(goal, error * (child.angleLimit / angle));
        return;
    }

    float3 axis = rotate(goal, child.localAxis);
    float4 delta = multiply(child.rotation, conjugate(goal));
    if (delta.w < 0.0)
        delta = -delta;
    float projection = dot(delta.xyz, axis);
    float2 pair = float2(projection, delta.w);
    float4 twist = dot(pair, pair) > 1e-12 ? normalize(float4(axis * projection, delta.w)) : float4(0, 0, 0, 1);
    float4 swing = multiply(delta, conjugate(twist));
    float3 bend = rotation_error(swing, float4(0, 0, 0, 1));
    float angle = length(bend);
    if (angle > child.angleLimit)
        swing = advance_rotation(float4(0, 0, 0, 1), bend * (child.angleLimit / angle));
    float twistAngle = 2.0 * atan2(dot(twist.xyz, axis), twist.w);
    float limited = clamp(twistAngle, -child.twistLimit, child.twistLimit);
    child.rotation = normalize(multiply(swing, advance_rotation(goal, axis * limited)));
}

void solve_soft_link(inout Body first, inout Body second, float rest, float stiffness, float interval,
                     float sizeScale, inout float lambda)
{
    float3 offset = second.position - first.position;
    float distance = length(offset);
    float inverseMass = first.inverseMass + second.inverseMass;
    if (distance < 1e-6 || inverseMass <= 0.0)
        return;
    float compliance = rcp(stiffness * interval * interval);
    float impulse = (distance - rest - compliance * lambda) / (inverseMass + compliance);
    float limit = 0.05 * sizeScale / max(first.inverseMass, second.inverseMass);
    impulse = clamp(impulse, -limit, limit);
    lambda += impulse;
    float3 correction = offset * (impulse / distance);
    first.position += first.inverseMass * correction;
    second.position -= second.inverseMass * correction;
}

void damp_soft_link(inout Body first, inout Body second, float damping, float interval)
{
    float3 offset = second.position - first.position;
    float distance = length(offset);
    float inverseMass = first.inverseMass + second.inverseMass;
    if (distance < 1e-6 || inverseMass <= 0.0)
        return;
    float3 axis = offset / distance;
    float speed = dot(second.velocity - first.velocity, axis);
    float3 impulse = axis * ((1.0 - exp(-damping * interval)) * speed / inverseMass);
    first.velocity += first.inverseMass * impulse;
    second.velocity -= second.inverseMass * impulse;
}

float3x4 read_skin(uint slot)
{
    uint i = slot * 12u;
    return float3x4(float4(Palette[i], Palette[i+1], Palette[i+2], Palette[i+3]),
        float4(Palette[i+4], Palette[i+5], Palette[i+6], Palette[i+7]),
        float4(Palette[i+8], Palette[i+9], Palette[i+10], Palette[i+11]));
}

void write_skin(uint slot, float3x4 rows)
{
    [unroll] for (uint row = 0; row < 3; ++row)
        [unroll] for (uint col = 0; col < 4; ++col)
            Palette[slot * 12u + row * 4u + col] = rows[row][col];
}

[numthreads(1, 1, 1)]
void main()
{
    uint objectRows, bodyRows, followerCount, paletteRows;
    ObjectCB.GetDimensions(objectRows);
    Capsules.GetDimensions(bodyRows);
    Followers.GetDimensions(followerCount);
    Palette.GetDimensions(paletteRows);
    uint count = bodyRows / 5u;
    float4 control = IniParams[46]; // time, frame, reset generation, gravity multiplier
    bool enabled = IniParams[47].y > 0.0;
    bool canAdvance = IniParams[47].z >= 0.0;
    if (objectRows < 4 || count == 0 || count > MaxBodies)
        return;
    uint airRows = 0;
    bool readJoints = IniParams[47].w == JointParamsTag;
    if (readJoints || IniParams[47].w == AirParamsTag)
        AirDrag.GetDimensions(airRows);
    bool hasAirProfile = airRows == count;
    uint jointRows = 0, linkCount = 0, jointStride = 1u, linkStart = 0u;
    if (readJoints)
    {
        JointOptions.GetDimensions(jointRows);
        float4 layout = jointRows > 0u ? JointOptions[0] : 0.0;
        jointStride = layout.z == 3.0 ? 3u : 1u;
        linkStart = 1u + count * jointStride;
        if (jointRows < linkStart || layout.x != count)
            readJoints = false;
        else
            linkCount = min(min((uint)layout.y, MaxSoftLinks), (jointRows - linkStart) / 2u);
    }
    float4x4 world = transpose(float4x4(ObjectCB[0], ObjectCB[1], ObjectCB[2], ObjectCB[3]));
    if (!all(isfinite(world[3])) || any(abs(world[3] - float4(0,0,0,1)) > 1e-4))
        return;
    float3 x = ObjectCB[0].xyz, y = ObjectCB[1].xyz, z = ObjectCB[2].xyz;
    float determinant = dot(x, cross(y, z));
    if (!isfinite(determinant) || abs(determinant) < 1e-6)
        return;
    float3x3 inverseWorld = float3x3(cross(y,z), cross(z,x), cross(x,y)) / determinant;
    float3 origin = ObjectCB[3].xyz;
    if (!all(isfinite(origin)))
        return;
    float4 stamp = State[0];
    bool reset = stamp.y == 0.0 || stamp.z != control.z || State[1].w != (enabled ? 1.0 : 0.0);
    if (!canAdvance && (reset || !enabled))
        return;
    bool advance = canAdvance && (reset || stamp.y != control.y + 1.0);
    bool rebase = advance && !reset && (control.x < stamp.x
        || control.x - stamp.x > 0.15 || length(State[1].xyz - origin) > 1.25);
#if SSMT_PHYSICS_BODIES <= 32
    Body bodies[MaxBodies];
#endif
    uint colliderIndices[MaxBodies], dynamicIndices[MaxBodies];
    uint colliderCount = 0u, dynamicCount = 0u;
    float3 previousP[MaxBodies];
    float4 previousQ[MaxBodies];
    float sizeScale = pow(abs(determinant), 1.0 / 3.0);
    uint historyStart = 2 + count * ActorRows;
    uint offsetStart = historyStart + max(count * count, 1u + 2u * count);
    uint motionStateRow = offsetStart + count;
    uint stateRows;
    State.GetDimensions(stateRows);
    bool hasTargets = stateRows >= historyStart + 1u + 2u * count;
    bool hasMotionState = motionStateRow < stateRows;
    float maxLag = 0.85 * max(1.0, sizeScale);
    uint limitedCount = 0;
    uint motionReference = count;
    uint rootReference = 0u;
    [loop] for (uint i = 0; i < count; ++i)
    {
        uint p = i * 5u;
        float4 spec = Capsules[p];
        uint slot = (uint)spec.x;
        if ((slot + 1u) * 12u > paletteRows)
            return;
        float3x4 skin = read_skin(slot);
        float4x4 pose = mul(world, float4x4(skin[0], skin[1], skin[2], float4(0,0,0,1)));
        float3 bx = float3(pose[0].x, pose[1].x, pose[2].x);
        float3 by = float3(pose[0].y, pose[1].y, pose[2].y);
        if (!all(isfinite(bx)) || !all(isfinite(by)) || length(cross(bx, by)) < 1e-6)
            return;
        Body body = (Body)0;
        body.targetRotation = basis_rotation(bx, by);
        body.target = mul(pose, float4(Capsules[p+1u].xyz, 1)).xyz;
        body.head = mul(pose, float4(Capsules[p+3u].xyz, 1)).xyz;
        if (!all(isfinite(body.target)) || !all(isfinite(body.head)))
            return;
        body.localAxis = rotate(conjugate(body.targetRotation),
            normalize(mul((float3x3)pose, Capsules[p+2u].xyz)));
        body.mode = (uint)spec.z;
        body.parent = (int)spec.y;
        if (body.parent < 0)
        {
            if (motionReference == count && body.mode != 2)
                motionReference = i;
            rootReference = i;
        }
        body.radius = Capsules[p+1u].w * sizeScale;
        body.halfSegment = Capsules[p+2u].w * sizeScale;
        body.angleLimit = Capsules[p+3u].w;
        uint jointRow = 1u + i * jointStride;
        float4 joint = readJoints ? JointOptions[jointRow] : float4(-1, 0, 0, 0);
        body.twistLimit = joint.x;
        body.recoveryGap = joint.y * sizeScale;
        body.sweep = joint.z > 0.0 && body.mode == 1;
        body.blockDeepOverlap = joint.w > 0.0 && body.mode == 2;
        if (readJoints && jointStride == 3u && body.mode != 0)
        {
            float4 box = JointOptions[jointRow + 1u];
            body.boxHalf = box.xyz * sizeScale;
            body.boxCorner = box.w * sizeScale;
            body.boxSide = JointOptions[jointRow + 2u].xyz;
        }
        body.damping = Capsules[p+4u].x;
        body.stiffness = Capsules[p+4u].y;
        body.friction = Capsules[p+4u].z;
        body.airDrag = hasAirProfile ? AirDrag[i] : 0.0;
        float settings = abs(Capsules[p+4u].w);
        body.gravityScale = settings >= 2.0 ? settings - 2.0 : 1.0;
        if (body.mode == 2)
        {
            body.inverseMass = rcp(spec.w);
            if (body.boxHalf.x > 0.0)
            {
                float3 squared = body.boxHalf * body.boxHalf;
                body.inverseInertia = rcp(max(spec.w / 3.0 * (squared.yzx + squared.zxy), 1e-8));
            }
            else
                body.inverseInertia = float3(capsule_inverse_inertia(spec.w, body.radius, body.halfSegment), 0.0);
        }
        uint state = 2u + ActorRows * i;
        body.position = reset ? body.target : State[state].xyz;
        body.rotation = reset ? body.targetRotation : State[state+1u];
        body.velocity = reset ? 0 : State[state+2u].xyz;
        body.angular = reset ? 0 : State[state+3u].xyz;
        body.contact = reset ? 0 : State[state].w;
        body.contactForce = reset ? 0 : State[state+5u].xyz;
        if (rebase)
        {
            uint previousTarget = historyStart + 1u + 2u * i;
            if (hasTargets && State[previousTarget].w == 1.0)
            {
                float4 turn = multiply(body.targetRotation, conjugate(State[previousTarget+1u]));
                body.position = body.target + rotate(turn, body.position - State[previousTarget].xyz);
                body.rotation = multiply(turn, body.rotation);
            }
            else
                body.position += origin - State[1].xyz;
            body.velocity = 0.0;
            body.angular = 0.0;
        }
        if (!all(isfinite(body.position)))
            body.position = body.target;
        if (!all(isfinite(body.rotation)) || dot(body.rotation, body.rotation) < 1e-6)
            body.rotation = body.targetRotation;
        if (!all(isfinite(body.velocity))) body.velocity = 0.0;
        if (!all(isfinite(body.angular))) body.angular = 0.0;
        if (advance && body.mode == 2)
        {
            float3 lag = body.position - body.target;
            float distance = length(lag);
            if (distance > maxLag)
            {
                // Recover a single translated capsule without resetting the group's rotations.
                body.position = body.target + lag * (maxLag / distance);
                float3 outward = lag / distance;
                body.velocity -= outward * max(0.0, dot(body.velocity, outward));
                ++limitedCount;
            }
        }
        bodies[i] = body;
        if (advance && body.radius > 0.0)
        {
            if (body.mode == 2)
                dynamicIndices[dynamicCount++] = i;
            else
                colliderIndices[colliderCount++] = i;
        }
        previousP[i] = body.position;
        previousQ[i] = body.rotation;
    }

    if (advance)
    {
        float4 settings = IniParams[48];
        if (settings.w != SolverParamsTag)
            settings.xyz = float3(120.0, 4.0, 4.0);
        float elapsed = reset || rebase || !enabled ? 0.0 : max(control.x - stamp.x, 0.0);
        uint refIndex = motionReference < count ? motionReference : rootReference;
        uint lastTarget = historyStart + 1u + 2u * refIndex;
        bool hasPreviousTarget = elapsed > 0.0 && hasTargets && State[lastTarget].w == 1.0;
        float3 targetDelta = 0.0;
        if (hasPreviousTarget)
        {
            targetDelta = bodies[refIndex].target - State[lastTarget].xyz;
            // Dampen motion relative to the moving character. Air drag is applied separately.
            float horizontalSpeed = length(targetDelta.xz) / elapsed;
            if (horizontalSpeed > (hasAirProfile ? 1e-5 : 2.0 * sizeScale))
            {
                float2 direction = normalize(targetDelta.xz);
                [loop] for (uint i = 0; i < count; ++i)
                {
                    uint previous = historyStart + 1u + 2u * i;
                    if (bodies[i].mode != 2 || State[previous].w != 1.0)
                        continue;
                    // Isolated animation of a collider must not transport other bodies.
                    float2 delta = bodies[i].target.xz - State[previous].xz;
                    float speedAlongMotion = clamp(dot(delta, direction) / elapsed, 0.0, horizontalSpeed);
                    float blend = hasAirProfile ? 1.0 : smoothstep(2.0 * sizeScale, 3.5 * sizeScale, speedAlongMotion);
                    float2 velocity = direction * (speedAlongMotion * blend);
                    bodies[i].dampingReferenceVelocity = float3(velocity.x, 0.0, velocity.y);
                }
            }
        }
        bool moving = false;
        float quietTime = 0.0;
        float speed = 0.0, turnSpeed = 0.0;
        if (settings.x < 0.0 && hasMotionState && hasTargets && !reset)
        {
            float4 previousMotion = State[motionStateRow];
            moving = previousMotion.x > 0.5;
            if (hasPreviousTarget)
            {
                speed = length(targetDelta) / elapsed;
                turnSpeed = length(rotation_error(bodies[refIndex].targetRotation, State[lastTarget+1u])) / elapsed;
                if (speed >= 0.55 || turnSpeed >= 1.2)
                    moving = true;
                else if (moving && speed < 0.22 && turnSpeed < 0.35)
                {
                    quietTime = previousMotion.y + elapsed;
                    moving = quietTime < 0.20;
                }
            }
        }
        bool useSubsteps = settings.x != 0.0 && !moving;
        if (hasMotionState)
            State[motionStateRow] = float4(moving ? 1.0 : 0.0, quietTime, speed, turnSpeed);
        // Fixed-per-frame mode stores its step count as a negative budget.
        uint frameSteps = (uint)clamp(-settings.y, 1.0, 8.0);
        float stepTime = elapsed / frameSteps;
        uint steps = elapsed > 0.0 ? frameSteps : 0u;
        float remainder = 0.0;
        if (useSubsteps)
        {
            stepTime = rcp(clamp(abs(settings.x), 60.0, 360.0));
            uint maxSubsteps = (uint)clamp(settings.y, 2.0, 16.0);
            float amount = reset || rebase || !enabled ? 0.0
                : min(stamp.w + elapsed, stepTime * maxSubsteps);
            steps = min(maxSubsteps, (uint)floor(amount / stepTime + 1e-4));
            remainder = max(0.0, amount - steps * stepTime);
        }
        bool jointsPerFrame = settings.z < 0.0;
        uint iterations = (uint)clamp(abs(settings.z), 1.0, 12.0);
        uint jointPasses = 0u;
        uint recoveries = 0u;
        float strength = max(0.0, IniParams[47].x);
        [loop] for (uint step = 0; step < steps; ++step)
        {
            float3 stepP[MaxBodies];
            float4 stepQ[MaxBodies];
            [loop] for (uint i = 0; i < count; ++i)
            {
                Body body = bodies[i];
                stepP[i] = body.position;
                stepQ[i] = body.rotation;
                body.contact = 0.0;
                body.contactForce = 0.0;
                body.contactCorrection = 0.0;
                if (body.mode != 2)
                {
                    float fraction = (float)(step + 1u) / steps;
                    body.position = lerp(previousP[i], body.target, fraction);
                    body.rotation = advance_rotation(previousQ[i],
                        fraction * rotation_error(body.targetRotation, previousQ[i]));
                    body.velocity = (body.position - stepP[i]) / stepTime;
                    body.angular = rotation_error(body.rotation, stepQ[i]) / stepTime;
                }
                else
                {
                    float4 targetRotation = body.targetRotation;
                    if (body.parent >= 0)
                    {
                        Body parent = bodies[body.parent];
                        targetRotation = multiply(multiply(parent.rotation,
                            conjugate(parent.targetRotation)), targetRotation);
                    }
                    float decay = exp(-body.damping * stepTime);
                    body.velocity = body.dampingReferenceVelocity + (body.velocity - body.dampingReferenceVelocity) * decay
                        + float3(0, -9.81 * control.w * body.gravityScale * stepTime, 0);
                    // Integrate quadratic drag without reversing the velocity on long steps.
                    body.velocity.xz /= 1.0 + body.airDrag * length(body.velocity.xz) * stepTime;
                    body.angular = body.angular * decay + rotation_error(targetRotation, body.rotation)
                        * (body.stiffness * stepTime);
                }
                bodies[i] = body;
            }
            [loop] for (uint a = 0; a < count; ++a)
            {
                if (strength <= 0.0 || bodies[a].radius <= 0.0)
                    continue;
                bool dynamic = bodies[a].mode == 2;
                uint partners = dynamic ? colliderCount : dynamicCount;
                [loop] for (uint partner = 0u; partner < partners; ++partner)
                {
                    uint b = dynamic ? colliderIndices[partner] : dynamicIndices[partner];
                    if (b <= a)
                        continue;
                    Body first = bodies[a], second = bodies[b];
                    float depth = apply_contact(first, second, strength, stepTime);
                    if (depth == 0.0 || (depth >= 0.5 && !first.blockDeepOverlap && !second.blockDeepOverlap))
                    {
                        if (first.sweep)
                            sample_swept_contact(first, second, stepP[a], stepQ[a], depth, strength, stepTime);
                        else if (second.sweep)
                            sample_swept_contact(second, first, stepP[b], stepQ[b], depth, strength, stepTime);
                    }
                    bodies[a] = first;
                    bodies[b] = second;
                }
            }
            [loop] for (uint actor = 0; actor < count; ++actor)
            {
                if (bodies[actor].mode == 2)
                {
                    float3 correction = bodies[actor].contactCorrection;
                    float distance = length(correction);
                    float limit = 0.04 * sizeScale;
                    correction *= distance > limit ? limit / distance : 1.0;
                    bodies[actor].position += correction;
                    stepP[actor] += correction;
                    bodies[actor].position += stepTime * clamp(bodies[actor].velocity, -12.0, 12.0);
                    bodies[actor].rotation = advance_rotation(bodies[actor].rotation,
                        stepTime * clamp(bodies[actor].angular, -30.0, 30.0));
                }
            }
            bool solveAtFrameEnd = jointsPerFrame && step + 1u == steps;
            if (solveAtFrameEnd)
            {
                [loop] for (uint i = 0; i < count; ++i)
                {
                    stepP[i] = bodies[i].position;
                    stepQ[i] = bodies[i].rotation;
                }
            }
            uint passes = !jointsPerFrame || solveAtFrameEnd ? iterations : 0u;
            jointPasses += passes;
            float interval = solveAtFrameEnd ? stepTime * steps : stepTime;
            float multipliers[MaxSoftLinks];
            [loop] for (uint link = 0; link < linkCount; ++link)
                multipliers[link] = 0.0;
            [loop] for (uint iteration = 0; iteration < passes; ++iteration)
            {
                [loop] for (uint i = 0; i < count; ++i)
                {
                    Body body = bodies[i];
                    if (body.mode != 2 || body.parent < 0)
                        continue;
                    Body parent = bodies[body.parent];
                    limit_joint_rotation(body, parent);
                    float3 predicted = stepP[i];
                    if (solve_joint(body, parent, predicted, sizeScale))
                        ++recoveries;
                    stepP[i] = predicted;
                    bodies[i] = body;
                    bodies[body.parent] = parent;
                }
                [loop] for (uint link = 0; link < linkCount; ++link)
                {
                    float4 spec = JointOptions[linkStart + link * 2u];
                    uint a = (uint)spec.x, b = (uint)spec.y;
                    if (a >= count || b >= count || a == b || spec.w <= 0.0)
                        continue;
                    Body first = bodies[a], second = bodies[b];
                    float multiplier = multipliers[link];
                    solve_soft_link(first, second, spec.z * sizeScale, spec.w, interval, sizeScale, multiplier);
                    multipliers[link] = multiplier;
                    bodies[a] = first;
                    bodies[b] = second;
                }
            }
            [loop] for (uint actorIndex = 0; actorIndex < count; ++actorIndex)
                if (bodies[actorIndex].mode == 2)
                {
                    Body body = bodies[actorIndex];
                    if (passes > 0u && body.parent >= 0 && body.twistLimit >= 0.0)
                        limit_joint_rotation(body, bodies[body.parent]);
                    if (solveAtFrameEnd)
                    {
                        // Apply the correction over the full interval since the previous joint solve.
                        float inverseFrameTime = rcp(stepTime * steps);
                        body.velocity = clamp(body.velocity, -12.0, 12.0)
                            + (body.position - stepP[actorIndex]) * inverseFrameTime;
                        body.angular = clamp(body.angular, -30.0, 30.0)
                            + rotation_error(body.rotation, stepQ[actorIndex]) * inverseFrameTime;
                    }
                    else
                    {
                        body.velocity = (body.position - stepP[actorIndex]) / stepTime;
                        body.angular = rotation_error(body.rotation, stepQ[actorIndex]) / stepTime;
                    }
                    bodies[actorIndex] = body;
                }
            if (passes > 0u)
            {
                [loop] for (uint link = 0; link < linkCount; ++link)
                {
                    uint row = linkStart + link * 2u;
                    float4 spec = JointOptions[row];
                    uint a = (uint)spec.x, b = (uint)spec.y;
                    if (a >= count || b >= count || a == b)
                        continue;
                    Body first = bodies[a], second = bodies[b];
                    damp_soft_link(first, second, JointOptions[row+1u].x, interval);
                    bodies[a] = first;
                    bodies[b] = second;
                }
            }
        }
        [loop] for (uint i = 0; i < count; ++i)
        {
            uint state = 2u + ActorRows * i;
            Body body = bodies[i];
            State[state] = float4(body.position, body.contact);
            State[state+1u] = body.rotation;
            State[state+2u] = float4(body.velocity, body.radius);
            State[state+3u] = float4(body.angular, 0.0);
            State[state+4u] = float4(axis_of(body), body.halfSegment);
            State[state+5u] = float4(body.contactForce, 0.0);
            if (hasTargets)
            {
                uint target = historyStart + 1u + 2u * i;
                State[target] = float4(body.target, 1.0);
                State[target+1u] = body.targetRotation;
            }
        }
        if (hasTargets)
            State[historyStart] = float4(reset ? 1.0 : 0.0, rebase ? 1.0 : 0.0, limitedCount, steps);
        if (motionStateRow + 1u < stateRows)
            State[motionStateRow+1u] = float4(jointPasses, recoveries, 0.0, 0.0);
        State[0] = float4(control.x, control.y + 1.0, control.z, remainder);
        State[1] = float4(origin, enabled ? 1.0 : 0.0);
    }
    if (!enabled)
        return;
    float3 shoulderOffsets[MaxBodies];
    [loop] for (uint i = 0u; i < count; ++i)
    {
        shoulderOffsets[i] = 0.0;
        Body root = bodies[i];
        if (!pins_translation(Capsules[i * 5u + 4u].w) || root.mode != 2 || root.parent >= (int)count)
            continue;
        float3 anchor = root.head;
        if (root.parent >= 0)
        {
            if (bodies[root.parent].mode == 2)
                continue;
            uint parentSlot = (uint)Capsules[(uint)root.parent * 5u].x;
            float3x4 parentSkin = read_skin(parentSlot);
            float3 bindHead = Capsules[i * 5u + 3u].xyz;
            anchor = mul(world, float4(mul(parentSkin, float4(bindHead, 1.0)), 1.0)).xyz;
        }
        uint savedRoot = 2u + ActorRows * i;
        float4 rootSwing = multiply(State[savedRoot+1u], conjugate(root.targetRotation));
        float3 currentHead = State[savedRoot].xyz + rotate(rootSwing, root.head - root.target);
        if (all(isfinite(currentHead)) && all(isfinite(anchor)))
            shoulderOffsets[i] = anchor - currentHead;
    }

    float3 renderOffsets[MaxBodies];
    [loop] for (uint actor = 0u; actor < count; ++actor)
    {
        float3 offset = 0.0;
        int ancestor = (int)actor;
        [loop] for (uint depth = 0u; depth < count && ancestor >= 0 && ancestor < (int)count; ++depth)
        {
            if (pins_translation(Capsules[(uint)ancestor * 5u + 4u].w))
            {
                offset = shoulderOffsets[ancestor];
                break;
            }
            ancestor = bodies[ancestor].parent;
        }
        renderOffsets[actor] = offset;
        if (offsetStart + actor < stateRows)
            State[offsetStart+actor] = float4(offset, 0.0);
    }

    [loop] for (uint slot = 0; slot < min(followerCount, paletteRows / 12u); ++slot)
    {
        int index = Followers[slot];
        if (index < 0)
            continue;
        Body body = bodies[index];
        uint saved = 2u + ActorRows * (uint)index;
        if (!all(isfinite(State[saved])) || !all(isfinite(State[saved+1u])))
            continue;
        float3 center = State[saved].xyz + renderOffsets[index];
        float3 lag = center - body.target;
        float distance = length(lag);
        if (distance > maxLag)
            center = body.target + lag * (maxLag / distance);
        float4 swing = multiply(State[saved+1u], conjugate(body.targetRotation));
        float3x4 skin = read_skin(slot);
        float4x4 before = mul(world, float4x4(skin[0], skin[1], skin[2], float4(0,0,0,1)));
        float3x4 after;
        [unroll] for (uint col = 0; col < 4; ++col)
        {
            float3 value = float3(before[0][col], before[1][col], before[2][col]);
            if (col == 3)
                value = rotate(swing, value - body.target) + center - origin;
            else
                value = rotate(swing, value);
            value = mul(inverseWorld, value);
            after[0][col] = value.x;
            after[1][col] = value.y;
            after[2][col] = value.z;
        }
        write_skin(slot, after);
    }
}
