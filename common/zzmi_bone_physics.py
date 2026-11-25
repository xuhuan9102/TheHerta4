"""Pack capsule bodies and emit their private DX11 resources and controls."""

from dataclasses import dataclass
import math
import struct

import numpy as np


SHADER = "zzmi_bone_physics.hlsl"
PREVIEW_SHADER = "zzmi_bone_physics_preview.hlsl"
PREVIEW_PIXEL_SHADER = "zzmi_bone_physics_preview_pixel.hlsl"
BODIES = "ResourceSSMTBonePhysicsBodies"
FOLLOWERS = "ResourceSSMTBonePhysicsFollowers"
AIR_DRAG = "ResourceSSMTBonePhysicsAirDrag"
JOINT_OPTIONS = "ResourceSSMTBonePhysicsJointOptions"
RESET = "$ssmt_bone_physics_reset"
MAX_BODIES = 128
MAX_SOFT_LINKS = 128
SOLVER_PARAMS_TAG = 27148
AIR_PARAMS_TAG = 27149
JOINT_PARAMS_TAG = 27150


def solver_shader_source(source, assets):
    """Keep ordinary exports on the small shader and size the dense solver from its packed inputs."""
    bodies, _, _, _, *joint_options = (payload for _, payload in assets)
    actors = len(bodies) // 80
    links = int(struct.unpack_from("<f", joint_options[0], 4)[0]) if joint_options else 0
    if actors > MAX_BODIES or links > MAX_SOFT_LINKS:
        raise ValueError(f"骨骼物理最多支持 {MAX_BODIES} 个刚体和 {MAX_SOFT_LINKS} 条软连接")
    if actors <= 32 and links <= 32:
        return source
    actors = max(32, 1 << (actors - 1).bit_length())
    links = max(32, 1 << (links - 1).bit_length())
    return (f"#define SSMT_PHYSICS_BODIES {actors}\n"
            f"#define SSMT_PHYSICS_LINKS {links}\n").encode("ascii") + source


@dataclass(frozen=True)
class RoundedBox:
    half_extents: tuple[float, float, float]
    side: tuple[float, float, float]
    corner: float


@dataclass(frozen=True)
class Capsule:
    slot: int
    parent: int
    mode: int
    center: tuple
    axis: tuple
    head: tuple
    radius: float
    half_segment: float
    mass: float
    damping: float
    stiffness: float
    friction: float
    angle: float
    gravity_scale: float = 1.0
    pin_translation: bool = False
    air_drag: float = 0.2
    twist_limit: float | None = None
    recovery_gap: float = 0.0
    sweep: bool = False
    box: RoundedBox | None = None
    allow_deep_overlap: bool = True


@dataclass(frozen=True)
class SoftLink:
    first: int
    second: int
    stiffness: float
    damping: float


@dataclass(frozen=True)
class BonePhysics:
    capsules: tuple
    followers: tuple
    play: str
    debug: str
    play_key: str
    debug_key: str
    reset_key: str
    start_enabled: bool
    start_debug: bool
    gravity: float
    repulsion: float
    step_hz: int = 120
    max_substeps: int = 8
    joint_iterations: int = 4
    use_substeps: bool = True
    adaptive_substeps: bool = False
    frame_steps: int = 1
    joints_per_frame: bool = False
    soft_links: tuple[SoftLink, ...] = ()

    @property
    def has_boxes(self):
        return any(body.box is not None for body in self.capsules)

    @property
    def has_joint_options(self):
        return bool(self.soft_links) or any(
            body.twist_limit is not None or body.recovery_gap or body.sweep or body.box is not None
            or not body.allow_deep_overlap
            for body in self.capsules
        )

    @property
    def solver_params(self):
        iterations = -self.joint_iterations if self.joints_per_frame else self.joint_iterations
        if not self.use_substeps:
            return 0, -self.frame_steps, iterations, SOLVER_PARAMS_TAG
        frequency = -self.step_hz if self.adaptive_substeps else self.step_hz
        return frequency, self.max_substeps, iterations, SOLVER_PARAMS_TAG

    @property
    def state_rows(self):
        count = len(self.capsules)
        # Keep the former pair rows for diagnostics and the last animated targets.
        return 2 + 6 * count + max(count * count, 1 + 2 * count) + count + 2

    def assets(self):
        slots = {body.slot: i for i, body in enumerate(self.capsules)}
        rows = []
        for body in self.capsules:
            motion = int(body.pin_translation)
            if body.gravity_scale != 1.0:
                motion = (2.0 + body.gravity_scale) * (-1.0 if body.pin_translation else 1.0)
            fields = (
                (body.slot, slots.get(body.parent, -1), body.mode, body.mass),
                (*body.center, body.radius), (*body.axis, body.half_segment),
                (*body.head, body.angle), (body.damping, body.stiffness, body.friction, motion),
            )
            rows.extend(fields)
        assets = (
            ("SSMTBonePhysicsBodies.buf", np.asarray(rows, dtype="<f4").tobytes()),
            ("SSMTBonePhysicsFollowers.buf", np.asarray(self.followers, dtype="<i4").tobytes()),
            ("SSMTBonePhysicsStateInit.buf", bytes(self.state_rows * 16)),
            ("SSMTBonePhysicsAirDrag.buf", np.asarray([body.air_drag for body in self.capsules], dtype="<f4").tobytes()),
        )
        if self.has_joint_options:
            assets += (("SSMTBonePhysicsJointOptions.buf", self._joint_payload(slots)),)
        return assets

    def _joint_payload(self, slots):
        if len(self.soft_links) > MAX_SOFT_LINKS:
            raise ValueError(f"骨骼物理最多支持 {MAX_SOFT_LINKS} 条软连接")
        boxes = self.has_boxes
        rows = [(len(slots), len(self.soft_links), 3 if boxes else 0, 0)]
        for body in self.capsules:
            rows.append((-1.0 if body.twist_limit is None else body.twist_limit,
                         body.recovery_gap, int(body.sweep), int(not body.allow_deep_overlap)))
            if boxes:
                box = body.box
                if box is None:
                    rows.extend(((0., 0., 0., 0.), (0., 0., 0., 0.)))
                    continue
                if body.mode not in (1, 2) or any(not math.isfinite(value) or value <= 0.0 for value in box.half_extents):
                    raise ValueError(f"骨骼 {body.slot} 的方盒需要是尺寸大于零的刚体或碰撞体")
                if not math.isfinite(box.corner) or not 0.0 <= box.corner <= min(box.half_extents):
                    raise ValueError(f"骨骼 {body.slot} 的圆角半径不能超过方盒最小半边")
                if not math.isclose(math.hypot(*box.side), 1., rel_tol=1e-4):
                    raise ValueError(f"骨骼 {body.slot} 的方盒侧轴无效")
                rows.extend(((*box.half_extents, box.corner), (*box.side, 0.)))
        pairs = set()
        for link in self.soft_links:
            pair = tuple(sorted((link.first, link.second)))
            if link.first == link.second or pair in pairs or any(slot not in slots for slot in pair):
                raise ValueError(f"软连接的刚体端点缺失、重复或相同：{link.first} / {link.second}")
            pairs.add(pair)
            first, second = (self.capsules[slots[slot]] for slot in pair)
            if first.mode != 2 and second.mode != 2:
                raise ValueError("软连接至少一端需要动态刚体")
            rest = math.dist(first.center, second.center)
            if not math.isfinite(rest) or rest < 1e-5:
                raise ValueError("软连接两端的胶囊中心不能重合")
            if not math.isfinite(link.stiffness) or link.stiffness <= 0 or not math.isfinite(link.damping) or link.damping < 0:
                raise ValueError("软连接需要大于零的刚度和非负阻尼")
            rows.extend(((slots[first.slot], slots[second.slot], rest, link.stiffness),
                         (link.damping, 0, 0, 0)))
        return np.asarray(rows, dtype="<f4").tobytes()

    def constants(self, marker):
        return [
            marker, f"global {self.play} = {int(self.start_enabled)}",
            marker, f"global {self.debug} = {int(self.start_debug)}",
            f"global {RESET} = 0",
        ]

    def definitions(self):
        lines = [
            "", f"[{BODIES}]", "type = Buffer", "format = R32G32B32A32_FLOAT",
            "filename = res/SSMTBonePhysicsBodies.buf", "",
            f"[{FOLLOWERS}]", "type = Buffer", "format = R32_SINT",
            "filename = res/SSMTBonePhysicsFollowers.buf", "",
            f"[{AIR_DRAG}]", "type = Buffer", "format = R32_FLOAT",
            "filename = res/SSMTBonePhysicsAirDrag.buf", "",
        ]
        if self.has_joint_options:
            lines.extend((f"[{JOINT_OPTIONS}]", "type = Buffer", "format = R32G32B32A32_FLOAT",
                          "filename = res/SSMTBonePhysicsJointOptions.buf", ""))
        for name, key, variable in (
            ("Play", self.play_key, self.play), ("Debug", self.debug_key, self.debug),
        ):
            if key:
                lines.extend((f"[KeySSMTBonePhysics{name}]", f"key = {key}", "type = cycle",
                              f"{variable} = 0,1", ""))
        if self.reset_key:
            lines.extend(("[KeySSMTBonePhysicsReset]", f"key = {self.reset_key}", "type = activate",
                          f"{RESET} = {RESET} + 1", ""))
        return lines


def build_followers(capsules, parents, capacity):
    """Assign unselected descendants to their nearest simulated parent."""
    if len(capsules) > MAX_BODIES:
        raise ValueError(f"骨骼物理最多支持 {MAX_BODIES} 个胶囊和挂接锚点")
    actors = {body.slot: index for index, body in enumerate(capsules) if body.mode == 2}
    if any(not 0 <= body.slot < capacity for body in capsules):
        raise ValueError("骨骼物理包含超出已导出范围的骨骼编号")
    result = []
    for slot in range(capacity):
        ancestor = slot
        visited = set()
        while ancestor >= 0 and ancestor not in actors:
            if ancestor in visited:
                raise ValueError(f"骨骼物理的父骨链存在循环：{ancestor}")
            visited.add(ancestor)
            ancestor = parents.get(ancestor, -1)
        result.append(actors.get(ancestor, -1))
    return tuple(result)
