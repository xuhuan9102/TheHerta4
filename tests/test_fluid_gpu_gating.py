"""Run the shipped fluid solver and owner gates on D3D11 WARP."""
import importlib.util
from pathlib import Path
import sys

import numpy as np
import pytest

from test_fluid_probe_node_keys import node

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='D3D11 needs Windows')


@pytest.fixture(scope='module')
def runtime(tmp_path_factory):
    spec = importlib.util.spec_from_file_location('fluid_gpu_runtime', ROOT / 'tools/check_drag_cloth.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    gpu = module.GPU()
    directory = tmp_path_factory.mktemp('fluid-gpu')
    shaders = {}
    for phase in ('Predict', 'Density', 'Correct', 'Finalize', 'State',
                  'AssignSlots', 'SplashUpdate', 'SplashSpawn'):
        filename = ('Fluid' + phase) if phase in ('Predict', 'Density', 'Correct', 'Finalize', 'State') else phase
        path = ROOT / 'Toolset/fluid/fluid' / (filename + '.hlsl')
        source = node.SSMTNode_PostProcess_FluidProbe._remap_hlsl(module.base.load_file(
            'fluid_compiler', ROOT / 'tools/compile-hlsl.py').expand(str(path)))
        flattened = directory / path.name
        flattened.write_text(source, encoding='utf-8')
        shaders[phase] = gpu.shader(flattened)
    yield gpu, shaders, module
    gpu.release()


def bindings(gpu, requested=2, reset=False, substep=0):
    params = np.zeros((128, 4), np.float32)
    for original, values in {
        238: (.012, .0000213333, 1000, .000001),
        239: (1 / 120, .05 + substep / 120, substep, 0), 237: (1, 0, 0, 0),
        241: (1, 0, 0, 0), 253: (.02, 3, 8, 1 / 60),
        42: (1, 0, 1, 0), 47: (.25, 0, 1, 0), 65: (requested, 0, 0, 0),
    }.items():
        params[node.FLUID_INI_PARAM_MAP.get(original, original)] = values
    _, view = gpu.ini(params)
    gpu.bind(120, view)
    slots = np.zeros((8, 16, 4), np.float32)
    frames = np.zeros((8, 4, 4), np.float32)
    # Slot order deliberately differs from emitter id. Two instances of id 2
    # must both advance, while id 0 is enabled but not selected by this dispatch.
    for slot, owner in enumerate((2, 0, 1, 2)):
        slots[slot, 0, 0] = 1
        slots[slot, 1, 1] = 1
        slots[slot, 2, 2] = 1
        slots[slot, 3] = [0, .85, 0, 1]
        slots[slot, 4] = [0, owner, 0, 1]
        slots[slot, 9, 3] = 1
        slots[slot, 12, 3] = 1
        frames[slot, 0, 3] = 1
        frames[slot, 2] = [1, 0, 0, 0 if reset else 1]
    pulse = np.zeros((6, 4), np.float32)
    pulse[2] = [1, 0, 0, 1]
    pulse[3] = [.2, 0, .2, 1]
    pulse[5] = [1, 0, 0, 0]
    for register, array, stride in ((103, slots, 16), (105, frames, 16), (109, pulse, 16)):
        resource = gpu.buffer(array, structured=stride)
        gpu.bind(register, gpu.view(resource, array.nbytes // stride, 0))
    return slots, frames


def run_primary(gpu, shader, particles, phase):
    source = gpu.buffer(particles, structured=64)
    gpu.bind(100, gpu.view(source, 8192, 0))
    sentinel = np.full_like(particles, -321)
    target = gpu.buffer(sentinel, structured=64, writable=True)
    gpu.bind(0, gpu.view(target, 8192, 0, writable=True), True)
    sheet = gpu.buffer(np.zeros((8192, 4), np.float32), structured=16, writable=True)
    if phase == 'Predict':
        gpu.bind(111, None)
        gpu.bind(5, gpu.view(sheet, 8192, 0, writable=True), True)
    else:
        gpu.bind(5, None, True)
        gpu.bind(111, gpu.view(sheet, 8192, 0))
    for register, stride in ((6, 16), (7, 64)):
        resource = gpu.buffer(np.zeros((8192, stride // 4), np.float32), structured=stride, writable=True)
        gpu.bind(register, gpu.view(resource, 8192, 0, writable=True), True)
    gpu.dispatch(shader, 16)
    gpu.bind(0, None, True)
    result = gpu.read_bytes(target, particles.nbytes).view(np.float32).reshape(particles.shape)
    return result


@pytest.mark.parametrize('phase', ['Predict', 'Density', 'Correct', 'Finalize'])
@pytest.mark.parametrize('requested,changed', [(2, (0, 3)), (0, (1,)), (1, ())])
@pytest.mark.parametrize('reset', [False, True])
def test_only_requested_enabled_owner_banks_are_written(runtime, phase, requested, changed, reset):
    gpu, shaders, _ = runtime
    bindings(gpu, requested, reset)
    particles = np.zeros((8192, 4, 4), np.float32)
    for slot in range(4):
        index = slot * 1024 + 10
        particles[index, 0] = [.1, .85, 0, .1]
        particles[index, 1] = [.1, -.1, 0, 1]
        particles[index, 2] = [.1, .85, 0, 0]
        particles[index, 3] = [1000, 0, .1, 1.5]
    result = run_primary(gpu, shaders[phase], particles, phase)
    for slot in range(8):
        bank = result[slot * 1024:(slot + 1) * 1024]
        if slot in changed:
            assert not np.any(bank == -321), (phase, slot)
            assert np.isfinite(bank).all()
            if reset and phase == 'Predict':
                assert bank[10, 1, 3] == 0, 're-enabling must not revive the old stream'
        else:
            np.testing.assert_array_equal(bank, -321)


def test_new_slot_keeps_first_substep_births_during_second_substep(runtime):
    gpu, shaders, _ = runtime
    particles = np.zeros((8192, 4, 4), np.float32)
    bindings(gpu, requested=2, reset=True)
    first = run_primary(gpu, shaders['Predict'], particles, 'Predict')
    first_born = np.flatnonzero(first[:1024, 1, 3] > 0.5)
    assert len(first_born) > 0
    bindings(gpu, requested=2, reset=True, substep=1)
    second = run_primary(gpu, shaders['Predict'], first, 'Predict')
    assert np.all(second[first_born, 1, 3] > 0.5)


def test_state_updates_only_enabled_slots(runtime):
    gpu, shaders, _ = runtime
    bindings(gpu)
    frames = np.full((8, 4, 4), -321, np.float32)
    target = gpu.buffer(frames, structured=16, writable=True)
    gpu.bind(1, gpu.view(target, 32, 0, writable=True), True)
    gpu.dispatch(shaders['State'], 1)
    gpu.bind(1, None, True)
    result = gpu.read_bytes(target, frames.nbytes).view(np.float32).reshape(frames.shape)
    for slot in range(8):
        if slot in (0, 1, 3):
            assert np.all(result[slot, :3] != -321)
        else:
            np.testing.assert_array_equal(result[slot], -321)


def test_assign_slots_releases_closed_owner_and_rejects_its_candidate(runtime):
    gpu, shaders, _ = runtime
    slots, frames = bindings(gpu)
    candidates = np.zeros((145, 4), np.float32)
    candidates[0, 0] = 1
    candidates[5] = [0, 1, 0, 1]  # stale capture of the disabled emitter 1
    candidates[6, 3] = 1
    for register, array in ((0, candidates), (1, np.zeros((40, 4), np.float32))):
        resource = gpu.buffer(array, structured=16)
        gpu.bind(register, gpu.view(resource, len(array), 0))
    output = gpu.buffer(slots, structured=16, writable=True)
    state = gpu.buffer(frames, structured=16, writable=True)
    gpu.bind(0, gpu.view(output, 128, 0, writable=True), True)
    gpu.bind(1, gpu.view(state, 32, 0, writable=True), True)
    gpu.dispatch(shaders['AssignSlots'], 1)
    gpu.bind(0, None, True)
    gpu.bind(1, None, True)
    result = gpu.read_bytes(output, slots.nbytes).view(np.float32).reshape(slots.shape)
    result_state = gpu.read_bytes(state, frames.nbytes).view(np.float32).reshape(frames.shape)
    assert result[2, 9, 3] == 0
    assert result_state[2, 2, 3] == 0
    assert result[[0, 1, 3], 9, 3].tolist() == [1, 1, 1]


@pytest.mark.parametrize('phase', ['SplashUpdate', 'SplashSpawn'])
def test_closed_owners_splashes_are_removed_before_physics(runtime, phase):
    gpu, shaders, _ = runtime
    bindings(gpu)
    particles = np.zeros((6144, 4, 4), np.float32)
    # A closed owner's invalid old puddle must be rejected before touching its
    # geometry. A fresh slot has the same rule through its nozzle-valid flag.
    particles[0, 0] = [np.nan, np.nan, np.nan, 0.1]
    particles[0, 1] = [1, 2, 3, 3]  # alive tag 3 = slot 2 = disabled emitter 1
    particles[0, 3, 3] = 4
    source = gpu.buffer(particles, structured=64)
    target = gpu.buffer(np.full_like(particles, -321), structured=64, writable=True)
    gpu.bind(100, gpu.view(source, 6144, 0))
    gpu.bind(0, gpu.view(target, 6144, 0, writable=True), True)
    gpu.bind(5, None, True)
    gpu.bind(6, None, True)
    gpu.bind(7, None, True)
    gpu.dispatch(shaders[phase], 96)
    gpu.bind(0, None, True)
    result = gpu.read_bytes(target, particles.nbytes).view(np.float32).reshape(particles.shape)
    np.testing.assert_array_equal(result[0], 0)


@pytest.mark.parametrize('stage,define', [('vs', 'VERTEX_SHADER'), ('ps', 'PIXEL_SHADER')])
def test_surface_shader_compiles_with_game_stage_defines(runtime, stage, define):
    gpu, _, module = runtime
    path = ROOT / 'Toolset/fluid/fluid/FluidSurface.hlsl'
    source = '#define %s 1\n' % define + gpu.compiler.expand(str(path))
    hr, error = gpu.compiler.compile_one(gpu.compile, source, str(path), stage)
    assert hr >= 0, error
