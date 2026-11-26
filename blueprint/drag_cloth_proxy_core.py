"""Body sections, sleeve constraints and follower bindings shared by export and replay."""

import numpy as np

from .drag_cloth_geometry import _components

TAU = 2.0 * np.pi


def unit(v):
    length = np.linalg.norm(v, axis=-1, keepdims=True)
    return v / np.maximum(length, 1e-12)


def islands(points, faces):
    _, welded = np.unique(np.rint(points / 1e-6).astype(np.int64), axis=0, return_inverse=True)
    n = int(welded.max()) + 1
    labels = np.empty(n, dtype=int)
    for label, indices in enumerate(_components(welded[faces], n)):
        labels[indices] = label
    return labels[welded]


def convex_polygon(points):
    ordered = sorted(set(map(tuple, points)))

    def half(sequence):
        hull = []
        for point in sequence:
            while len(hull) >= 2:
                a, b = hull[-2:]
                if (b[0] - a[0]) * (point[1] - a[1]) > (b[1] - a[1]) * (point[0] - a[0]):
                    break
                hull.pop()
            hull.append(point)
        return hull[:-1]

    return np.asarray(half(ordered) + half(reversed(ordered))).reshape(-1, 2)


def section_hull(triangles, level, hint, span=None):
    # Shift off any exact coplanar vertices to keep each cut segment two-ended.
    heights = triangles[:, :, 2] - (level + 2e-8)
    hits = (heights.min(1) < 0) & (heights.max(1) > 0)
    tri, z = triangles[hits], heights[hits]
    if not len(tri):
        raise ValueError(f"No body surface crosses guide height {level:.4f}")
    nxt = np.roll(z, -1, axis=1)
    f, e = np.nonzero((z > 0) != (nxt > 0))
    t = z[f, e] / (z[f, e] - nxt[f, e])
    cross = tri[f, e, :2] + t[:, None] * (tri[f, (e + 1) % 3, :2] - tri[f, e, :2])
    keys, representatives, indices = np.unique(np.rint(cross / 2e-6).astype(np.int64), axis=0,
                                                 return_index=True, return_inverse=True)
    edges = indices.reshape(-1, 2)
    samples = cross[representatives]
    options = []
    for component in _components(edges[:, [0, 1, 1]], len(keys)):
        if len(component) < 6:
            continue
        hull = convex_polygon(samples[component])
        if len(hull) < 3:
            continue
        following = np.roll(hull, -1, axis=0)
        area = .5 * np.sum(hull[:, 0] * following[:, 1] - hull[:, 1] * following[:, 0])
        midpoint = hull.mean(axis=0)
        if area > 1e-5:
            options.append((np.linalg.norm(midpoint - hint), midpoint, hull))
    if not options:
        raise ValueError(f"No usable body contour at height {level:.4f}")
    _, midpoint, hull = min(options, key=lambda option: option[0])
    if span is not None:
        nearby = [polygon for distance, _, polygon in options if distance < span]
        if len(nearby) > 1:
            hull = convex_polygon(np.concatenate(nearby))
            midpoint = hull.mean(axis=0)
    delta = np.roll(hull, -1, axis=0) - hull
    normals = unit(delta[:, [1, 0]] * [1, -1])
    return midpoint, np.column_stack((normals, -np.sum(normals * hull, axis=1)))


class BodySections:
    """Convex outer contours of one body branch, sampled along the guide axis."""

    def __init__(self, triangles, cloth, lo, hi, spokes, span=None, extend_lower=False):
        self.heights = np.linspace(lo, hi, 81)
        self.spokes = spokes
        theta = np.arange(spokes) * TAU / spokes
        rays = np.column_stack((np.cos(theta), np.sin(theta)))
        center, radii = [], []
        levels = self.heights[::-1] if extend_lower else self.heights
        garment_bottom = cloth[:, 2].min()
        max_step = max(.025, 3 * (self.heights[1] - self.heights[0]))
        released = False
        for level in levels:
            if released:
                center.append(center[-1])
                radii.append(radii[-1])
                continue
            nearest = np.argsort(np.abs(cloth[:, 2] - level))[:max(12, len(cloth) // 8)]
            hint = np.median(cloth[nearest, :2], axis=0) if span is None else np.median(cloth[:, :2], axis=0)
            try:
                midpoint, eq = section_hull(triangles, level, hint, span)
                if extend_lower and center and np.linalg.norm(midpoint - center[-1]) > max_step:
                    midpoint, eq = section_hull(triangles, level, center[-1], span)
                    if np.linalg.norm(midpoint - center[-1]) > max_step:
                        raise ValueError(f"Body contour branches away at guide height {level:.4f}")
            except ValueError:
                if extend_lower and center and level < garment_bottom:
                    # The cuff has left the connected limb. Keep the last guide
                    # perimeter as it comes off instead of jumping to the other leg.
                    released = True
                    center.append(center[-1])
                    radii.append(radii[-1])
                    continue
                raise
            direction = rays @ eq[:, :2].T
            slack = -(eq[:, :2] @ midpoint + eq[:, 2])
            radii.append(np.min(np.divide(slack, direction, out=np.full_like(direction, np.inf), where=direction > 1e-9), axis=1))
            center.append(midpoint)
        if extend_lower:
            center.reverse()
            radii.reverse()
        self.centers = np.array(center)
        self.radii = np.array(radii)

    def parameters(self, positions):
        z = positions[:, 2]
        center = self.center(z)
        xy = positions[:, :2] - center
        theta = np.mod(np.arctan2(xy[:, 1], xy[:, 0]), TAU)
        return z, theta, np.linalg.norm(xy, axis=1)

    def center(self, z):
        return np.column_stack([np.interp(z, self.heights, column) for column in self.centers.T])

    def radius(self, z, theta):
        axial = np.clip((z - self.heights[0]) * (len(self.heights) - 1) / np.ptp(self.heights), 0, len(self.heights) - 1)
        lo = np.minimum(axial.astype(int), len(self.heights) - 2)
        v = axial - lo
        angular = np.mod(theta, TAU) * self.spokes / TAU
        left = angular.astype(int) % self.spokes
        right = (left + 1) % self.spokes
        u = angular - np.floor(angular)
        bottom = (1-u) * self.radii[lo, left] + u * self.radii[lo, right]
        top = (1-u) * self.radii[lo+1, left] + u * self.radii[lo+1, right]
        return bottom * (1-v) + top * v

    def points(self, z, theta, gap):
        ray = np.column_stack((np.cos(theta), np.sin(theta)))
        return np.column_stack((self.center(z) + ray * (self.radius(z, theta) + gap)[:, None], z))

    def gap(self, positions):
        z, theta, radius = self.parameters(positions)
        return radius - self.radius(z, theta)

    def project(self, positions, gap):
        z, theta, radius = self.parameters(positions)
        push = np.maximum(self.radius(z, theta) + gap - radius, 0)
        positions[:, :2] += np.column_stack((np.cos(theta), np.sin(theta))) * push[:, None]


class SleeveSolver:
    """Position-based tensile and bending constraints on a regular sleeve."""

    def __init__(self, body, lo, hi, rows, sides, clearance, travel, twist, motion, arrows=None):
        self.body = body
        self.heights = np.linspace(lo, hi, rows)
        self.sides, self.rows = sides, rows
        self.theta = np.tile(np.arange(sides) * TAU / sides, rows)
        self.material = np.repeat(np.linspace(0, 1, rows), sides)
        self.clearance, self.travel, self.twist, self.motion = clearance, travel, twist, motion
        self.arrow_field = None
        if arrows is not None:
            # Blend the controls across the shared tube. The sign of each
            # lateral component indicates its inward direction.
            fraction = (1 + np.cos(self.theta)) * .5
            self.arrow_field = (1 - fraction[:, None]) * arrows[0] + fraction[:, None] * arrows[1]
        z = np.repeat(self.heights, self.sides)
        self.rest = self.body.points(z, self.theta, self.clearance)
        self.current, self.previous = self.rest.copy(), self.rest.copy()
        self.progress = 0.0
        self.pinned = (self.material == 0) | (self.material == 1)
        self.faces = []
        self.edges = []
        index = lambda row, col: row * sides + col % sides
        for row in range(rows):
            for col in range(sides):
                a, b = index(row, col), index(row, col + 1)
                self.edges.extend([(a, b, .9), (a, index(row, col + 2), .26)])
                if row + 1 < rows:
                    c, d = index(row + 1, col), index(row + 1, col + 1)
                    self.faces.extend(((a, b, c), (b, d, c)))
                    self.edges.extend(((a, c, .9), (a, d, .55), (b, c, .55)))
                if row + 2 < rows:
                    self.edges.append((a, index(row + 2, col), .22))
        edges = np.array(self.edges)
        self.edges = edges[:, :2].astype(int)
        self.stiffness = edges[:, 2]
        a, b = self.edges.T
        self.length = np.linalg.norm(self.rest[a] - self.rest[b], axis=1)
        self.faces = np.array(self.faces, dtype=np.uint32)
        self.degree = np.bincount(self.edges.ravel(), minlength=len(self.rest))

    def target(self, progress):
        if self.arrow_field is not None:
            goal = self.rest + self.arrow_field * (progress * self.material[:, None])
            self.body.project(goal, self.clearance)
            return goal
        if self.motion == "slide":
            goal = self.rest.copy()
            goal[:, 2] -= progress * self.travel
            self.body.project(goal, self.clearance)
            return goal
        z = np.repeat(self.heights, self.sides)
        weight = self.material if self.motion == "compress" else 1
        return self.body.points(z - progress * self.travel * weight,
                                self.theta + self.twist * self.material * progress, self.clearance)

    def advance(self, progress):
        start = self.progress
        frames = max(12, int(abs(progress - start) * 80))
        a, b = self.edges.T
        ia, ib = ~self.pinned[a], ~self.pinned[b]
        weights = np.maximum(ia.astype(float) + ib, 1)
        movable = ~self.pinned
        for step in range(1, frames + 1):
            u = step / frames
            goal = self.target(start + (progress - start) * (u*u*(3 - 2*u)))
            future = self.current + 0.42 * (self.current - self.previous)
            future[movable] += 0.23 * (goal - future)[movable]
            for _ in range(9):
                future[self.pinned] = goal[self.pinned]
                delta = future[b] - future[a]
                dist = np.linalg.norm(delta, axis=1)
                extension = np.maximum(dist - self.length, 0)
                force = delta * (self.stiffness * extension / (weights * np.maximum(dist, 1e-10)))[:, None]
                correction = np.zeros_like(future)
                np.add.at(correction, a, force * ia[:, None])
                np.add.at(correction, b, -force * ib[:, None])
                future[movable] += (1.25 * correction / self.degree[:, None])[movable]
                future[movable] += .11 * (goal - future)[movable]
                self.body.project(future, self.clearance)
            future[self.pinned] = goal[self.pinned]
            self.previous, self.current = self.current, future
            yield future
        self.progress = progress

    def inverted_faces(self, positions):
        z, theta, _ = self.body.parameters(positions)
        angles, heights = theta[self.faces], z[self.faces]
        dtheta = (angles[:, 1:] - angles[:, :1] + np.pi) % TAU - np.pi
        dz = heights[:, 1:] - heights[:, :1]
        return int(np.count_nonzero(dtheta[:, 0] * dz[:, 1] <= dtheta[:, 1] * dz[:, 0]))


def local_frames(quad, u, v):
    a, b, c, d = np.moveaxis(quad, 1, 0)
    around = unit((1 - v[:, None]) * (b - a) + v[:, None] * (d - c))
    along = (1 - u[:, None]) * (c - a) + u[:, None] * (d - b)
    normal = unit(np.cross(around, along))
    return np.stack((around, np.cross(normal, around), normal), axis=2)


class Followers:
    def __init__(self, vertices, labels, solver, rigid):
        self.body = solver.body
        anchors = vertices.copy()
        for part in rigid:
            anchors[labels == part] = vertices[labels == part].mean(0)
        self.parts = [np.flatnonzero(labels == part) for part in rigid]
        self.soft = ~np.isin(labels, list(rigid))
        z, theta, _ = self.body.parameters(anchors)
        axial = np.clip((z-solver.heights[0])*(solver.rows-1)/np.ptp(solver.heights), 0, solver.rows-1)
        ring = np.minimum(axial.astype(int), solver.rows-2)
        self.v = axial-ring
        column = theta*solver.sides/TAU
        a = column.astype(int) % solver.sides
        b = (a+1) % solver.sides
        self.u = column-np.floor(column)
        self.ids = np.column_stack((ring*solver.sides+a,ring*solver.sides+b,
                                    (ring+1)*solver.sides+a,(ring+1)*solver.sides+b))
        u, v = self.u, self.v
        self.weights = np.column_stack(((1-u)*(1-v),u*(1-v),(1-u)*v,u*v))
        patches = solver.rest[self.ids]
        self.rest_frames = local_frames(patches, u, v)
        origin = np.einsum("ni,nij->nj", self.weights, patches)
        self.offset = np.einsum("ni,nij->nj", vertices - origin, self.rest_frames)
        self.soft_ids, self.soft_weights = self.ids[self.soft], self.weights[self.soft]
        self.active_support = self.soft_weights > 1e-6
        self.soft_rest = vertices[self.soft]
        self.offset_follows_frame = solver.arrow_field is None and solver.motion != "slide"
        self.sliding = solver.motion == "slide"
        self.proxy_rest = solver.rest
        if not self.offset_follows_frame and not self.sliding:
            rest_basis = np.column_stack((np.ones(len(solver.rest)), solver.rest))
            self.envelope_fit = np.linalg.pinv(rest_basis)
            self.soft_basis = np.column_stack((np.ones(len(self.soft_rest)), self.soft_rest))
        # Keep any original penetration allowance in the imported visual mesh.
        self.floor = np.minimum(self.body.gap(vertices), 0)

    def publish(self, pose):
        quad = pose[self.ids]
        frames = local_frames(quad, self.u, self.v)
        singular = np.linalg.norm(frames[:, :, 2], axis=1) < .5
        frames[singular] = self.rest_frames[singular]
        positions = np.einsum("ni,nij->nj", self.weights, quad) + np.einsum("nij,nj->ni", frames, self.offset)
        _, theta, _ = self.body.parameters(pose)
        rays = np.column_stack((np.cos(theta), np.sin(theta)))
        soft = positions[self.soft].copy()
        if not self.offset_follows_frame:
            # A broad skirt or paired opening spans multiple body branches.
            # Average the proxy displacement before applying it to the whole
            # envelope, then supply a common radial clearance adjustment.
            displacement = pose - self.proxy_rest
            if self.sliding:
                soft = self.soft_rest + displacement.mean(axis=0)
            else:
                soft = self.soft_rest + self.soft_basis @ (self.envelope_fit @ displacement)
            if len(soft):
                _, _, radius = self.body.parameters(soft)
                deficit = np.maximum(self.floor[self.soft] - self.body.gap(soft), 0)
                expansion = min(float(np.max(deficit / np.maximum(radius, .01), initial=0)), .16)
                # Cap the common correction and report residual penetration.
                # A small feature near the axis must not inflate the whole garment.
                soft[:, :2] += expansion * (soft[:, :2] - pose.mean(axis=0)[:2])
        # Spread collision corrections through the shared support nodes.
        # Nearby pieces get the same continuous correction field.
        for _ in range(6 if self.offset_follows_frame else 0):
            deficit = np.maximum(self.floor[self.soft] - self.body.gap(soft), 0)
            if deficit.max(initial=0) < 1e-6:
                break
            lift = np.zeros(len(pose))
            changes = np.broadcast_to(deficit[:, None] * 1.12, self.soft_ids.shape)
            np.maximum.at(lift, self.soft_ids[self.active_support], changes[self.active_support])
            soft[:, :2] += np.einsum("ni,nij->nj", self.soft_weights, (rays * lift[:, None])[self.soft_ids])
        positions[self.soft] = soft
        for piece in self.parts:
            # Move a rigid piece as a unit if the body would penetrate it.
            for _ in range(3):
                points = positions[piece]
                deficit = self.floor[piece] - self.body.gap(points)
                if deficit.max(initial=0.0) < 1e-6:
                    break
                _, theta, _ = self.body.parameters(points[deficit.argmax()][None])
                positions[piece, :2] += [deficit.max()*np.cos(theta[0]), deficit.max()*np.sin(theta[0])]
        return positions
