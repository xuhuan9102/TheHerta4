"""Preview regressions at the simulation job and Blender-node call sites."""

from types import SimpleNamespace
from unittest.mock import patch

import numpy as np
import pytest

from blueprint import fluid_preview_sim as S
from blueprint import fluid_preview_bake as B
from test_fluid_probe_node_keys import node, _bare_node, _emitter, _force_field


@pytest.fixture(autouse=True)
def physics():
    saved = S._GRAVITY_SCALE, S._VISCOSITY_OVERRIDE, S._PHYSICS_SOURCE
    S.set_physics(gravity_scale=0.25, viscosity=0)
    yield
    S._GRAVITY_SCALE, S._VISCOSITY_OVERRIDE, S._PHYSICS_SOURCE = saved


def emitter(**overrides):
    result = dict(origin=[0, 0, 1], direction=[1, 0, 0], up=[0, 1, 0],
                  speed=1.5, level=3, slot=0)
    result.update(overrides)
    return result


@pytest.mark.parametrize('fps', [30, 60, 144])
def test_job_advances_one_game_frame_per_snapshot(fps):
    job = S.SimulationJob([emitter()], frames=3, fps=fps, capacity=1, warmup_frames=0)
    with patch.object(job.sim, '_substep', wraps=job.sim._substep) as substep:
        while not job.finished:
            job.advance()
        assert substep.call_count == 6
    assert job.sim.time == pytest.approx(3 * S.frame_dt(1 / fps))


def test_guided_particle_keeps_shader_velocity_and_force():
    field = dict(owner=0, kind='DIRECTIONAL', center=[0, 0, 1],
                 axis=[0, 0, 1], radius=10, strength=3, falloff=2, enabled=True)
    sim = S.Simulator(1, [emitter()], force_fields=[field])
    sim.alive[0] = True
    sim.slot[0] = 0
    sim.age[0] = 0.02
    sim.sheet[0] = 0.8
    sim.launch_speed[0] = 1.5
    sim.guide_origin[0] = [0, 0, 1]
    sim.guide_axis[0] = [1, 0, 0]
    sim.guide_long[0] = [0, 1, 0]
    sim.pos[0] = [0.03, 0, 1]
    dt = 1 / 120
    age = sim.age[0] + dt
    guide = dict(origin=sim.guide_origin[0].copy(), axis=sim.guide_axis[0],
                 longitudinal=sim.guide_long[0], carrier=sim.guide_carrier[0],
                 speed=1.5, amount=float(sim.sheet[0]), lane=0, seed=0)
    gravity = S._gravity() * S.jet_gravity_scale(1.5, 0)
    pos, vel, travel = S.evaluate_jet_guide(guide, age, S.sheet_focus_distance(0.8), gravity)
    force = S.force_field_accel(pos[None], [field], owners=np.array([0]))[0]
    pos += force * (age * age * 0.5)
    vel += force * age
    sim._substep(dt, np.random.default_rng(1))
    np.testing.assert_allclose(sim.pos[0], pos, atol=2e-7)
    np.testing.assert_allclose(sim.vel[0], vel, atol=2e-6)
    assert sim.age[0] == pytest.approx(age)
    assert sim.travel[0] == pytest.approx(travel)


def test_separate_emitters_do_not_share_pressure_neighbors():
    S.set_physics(gravity_scale=0, viscosity=0.001)
    sim = S.Simulator(2, [emitter()])
    sim.alive[:] = True
    sim.slot[:] = [0, 1]
    sim.pos[:] = [[0, 0, 1], [0.001, 0, 1]]
    sim.vel[:] = [[1, 0, 0], [-1, 0, 0]]
    sim._substep(1 / 120, np.random.default_rng(1))
    np.testing.assert_allclose(sim.vel, [[1, 0, 0], [-1, 0, 0]], atol=2e-6)


def test_multi_emitter_job_allocates_a_full_bank_and_respects_controls():
    job = S.SimulationJob([emitter(slot=0, lifetime=0.3, speed_override=2),
                           emitter(slot=1, lifetime=0.6, enabled=False)],
                          frames=3, capacity=16, fps=60)
    while not job.finished:
        job.advance()
    assert job.sim.capacity == 32
    assert job.sim.lifetime[:16] == pytest.approx(0.3)
    assert job.sim.lifetime[16:] == pytest.approx(0.6)
    assert not job.sim.alive[16:].any()
    assert job.sim.launch_speed[:16] == pytest.approx(2)
    assert job.sim.age[job.sim.alive].max() < 0.3


def test_guided_particle_releases_with_analytic_momentum():
    S.set_physics(gravity_scale=0)
    sim = S.Simulator(1, [emitter()])
    sim.alive[0] = True
    sim.slot[0] = 0
    sim.sheet[0] = 0.8
    sim.travel[0] = S.sheet_focus_distance(0.8) + 0.01
    sim.pos[0] = [0.1, 0, 1]
    sim.vel[0] = [1.5, 0.2, 0]
    sim._substep(1 / 120, np.random.default_rng(1))
    np.testing.assert_allclose(sim.vel[0], [1.5, 0.2, 0], atol=2e-6)


def test_numpy_neighbor_fallback_does_not_double_pressure():
    points = np.array([[0, 0, 0], [0.001, 0, 0], [0.02, 0, 0]], dtype=np.float32)
    sim = S.Simulator(3, [emitter()])
    expected = sim._density_terms(points)
    with patch.object(S, '_KDTree', None):
        result = sim._density_terms(points)
    for actual, reference in zip(result, expected):
        np.testing.assert_allclose(actual, reference)


def test_zero_force_axis_does_not_invent_a_direction():
    field = dict(owner=0, kind='DIRECTIONAL', center=[0, 0, 0],
                 axis=[0, 0, 0], radius=1, strength=3, falloff=2)
    np.testing.assert_array_equal(S.force_field_accel(np.array([[0.1, 0, 0]]),
                                 [field], owners=np.array([0])), [[0, 0, 0]])


def test_pressure_gradient_matches_shader_displacement_kernel():
    sim = S.Simulator(2, [emitter()])
    points = np.array([[0, 0, 0], [0.003, 0, 0]], dtype=float)
    _, gradient, gradient2 = sim._density_terms(points)
    d = points[0] - points[1]
    r = np.linalg.norm(d)
    expected = S.MASS / S.RHO * (-45 / (np.pi * S.H**4)) * (1 - r / S.H)**2 * d / r
    np.testing.assert_allclose(gradient, [expected, -expected])
    np.testing.assert_allclose(gradient2, [expected @ expected] * 2)


@pytest.mark.parametrize('kind', ['DIRECTIONAL', 'RADIAL', 'VORTEX'])
def test_released_jet_force_matches_shader_numeric_step(kind):
    field = dict(owner=0, kind=kind, center=[0, 0, 1], axis=[0, 0, 1],
                 radius=1, strength=3, falloff=2)
    sim = S.Simulator(1, [emitter()], force_fields=[field])
    sim.alive[0] = True
    sim.slot[0] = 0
    sim.sheet[0] = 0.8
    sim.travel[0] = 0.2
    sim.launch_speed[0] = 1.5
    sim.pos[0] = [0.15, 0, 1]
    sim.vel[0] = [1.5, 0, 0]
    expected_pos = sim.pos[0].astype(float)
    expected_vel = sim.vel[0].astype(float)
    travel = 0.2
    for _ in range(20):
        dt = 1 / 120
        accel = S._gravity() * S.jet_gravity_scale(1.5, travel)
        accel = accel + S.force_field_accel(expected_pos[None], [field], owners=np.array([0]))[0]
        expected_vel += accel * dt
        expected_pos += expected_vel * dt
        travel += np.linalg.norm(expected_vel * dt)
        sim._substep(dt, np.random.default_rng(1))
    np.testing.assert_allclose(sim.pos[0], expected_pos, atol=5e-7)
    np.testing.assert_allclose(sim.vel[0], expected_vel, atol=2e-6)


def test_volume_radius_does_not_enlarge_the_game_surface():
    assert B.volume_influence_radius(0.0028, 2, 3) == pytest.approx(B.stream_radius(0.0028, 2, 3))


def test_trickle_births_use_shader_speed_and_one_in_five_schedule():
    sim = S.Simulator(5, [emitter(trickle=True, trickle_scale=2)])
    rng = np.random.default_rng(1)
    for i in range(5):
        sim._spawn_one(i, 0, rng)
    assert sim.alive.tolist() == [True, False, False, False, False]
    np.testing.assert_allclose(sim.vel[0], [0.54, 0, 0], atol=1e-6)


def test_minimum_flow_has_frozen_independent_ballistic_branch():
    sim = S.Simulator(16, [emitter(speed=0.1)])
    sim.time = 0.7
    rng = np.random.default_rng(1)
    for i in range(16):
        sim._spawn_one(i, 0, rng)
    assert (sim.lane < -2).any()
    assert (sim.sheet == -1).all()
    assert np.isfinite(sim.free_velocity).all()


def test_mirrored_vortex_matches_game_cross_product_after_reflection():
    point = np.array([[0.1, 0.2, 0.3]])
    field = dict(owner=0, kind='VORTEX', center=[0, 0, 0], axis=[0, 0, 1],
                 radius=1, strength=3, falloff=2)
    mirror = np.array([-1, 1, 1])
    game = S.force_field_accel(point * mirror, [field], owners=np.array([0])) * mirror
    preview = S.force_field_accel(point, [dict(field, handedness=-1)], owners=np.array([0]))
    np.testing.assert_allclose(preview, game)


def test_force_field_add_updates_both_marker_collections():
    inst = _bare_node([_emitter(force_fields=[_force_field()])])
    tree = SimpleNamespace(nodes=[inst])
    scene = object()
    state = dict(fingerprint=None, busy=False)
    with patch.object(node, '_PREVIEW_STATE', state), \
            patch.object(node.bpy.data, 'collections', SimpleNamespace(get=lambda _: object())), \
            patch.object(node.bpy.context, 'scene', scene), \
            patch.object(node, 'build_preview_markers') as preview, \
            patch.object(node, 'build_force_field_markers') as fields:
        node._auto_rebuild(tree)
    preview.assert_called_once_with(inst, scene)
    fields.assert_called_once_with(inst, scene)


@pytest.mark.parametrize('change', ['tilt', 'force', 'speed', 'lifetime', 'enable'])
def test_cached_preview_invalidated_by_runtime_parameters(change):
    em = _emitter(force_fields=[_force_field()], speed=1.5, lifetime=1.1, enable=True)
    inst = _bare_node([em])
    before = node._preview_fingerprint(inst)
    if change == 'force':
        em.force_fields[0].strength += 1
    elif change == 'enable':
        em.enable = False
    else:
        setattr(em, change, getattr(em, change) + 0.1)
    assert node._preview_fingerprint(inst) != before
