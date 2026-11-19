"""Execute shader optimizations against independent reference outputs on D3D11."""
import importlib.util
from pathlib import Path
import re
import sys

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
pytestmark = pytest.mark.skipif(sys.platform != 'win32', reason='D3D11 needs Windows')


@pytest.fixture(scope='module')
def gpu():
    spec = importlib.util.spec_from_file_location('shader_optimization_gpu', ROOT / 'tools/check_drag_cloth.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    value = module.GPU()
    yield value
    value.release()


@pytest.mark.parametrize('stride', [20, 24, 48])
def test_uv_updates_only_configured_fields_on_gpu(gpu, tmp_path, stride):
    from test_node_postprocess_uv_offset import module
    node = module.SSMTNode_PostProcess_UVOffset()
    attrs = [dict(name='COLOR', type='rgba8', offset=0, apply_offset=False),
             dict(name='uv', type='float2', offset=4, apply_offset=True),
             dict(name='packed_uv', type='half2', offset=16, apply_offset=True)]
    path = tmp_path / 'uv.hlsl'
    assert node._update_shader_file(str(path), [(2, 95)], attrs, stride,
                                    str(ROOT / 'Toolset/uv_offset_anim.hlsl'))
    src = path.read_text('utf-8')
    assert 'data[0] = rw_buffer' not in src
    assert 'rw_buffer[base + 3] =' not in src
    random = np.random.default_rng(812)
    data = random.integers(0, 2**32, (101, stride // 4), dtype=np.uint32)
    data[:, 1:3] = random.uniform(-2, 2, (101, 2)).astype(np.float32).view(np.uint32)
    half = random.uniform(-2, 2, (101, 2)).astype(np.float16)
    data[:, 4] = half.view(np.uint32).ravel()
    expected = data.copy()
    offset = np.array([.125, -.25], np.float32)
    expected[2:96, 1:3] = (data[2:96, 1:3].view(np.float32) + offset).view(np.uint32)
    params = np.zeros((128, 4), np.float32)
    params[100, 0], params[101, 0] = offset
    _, view = gpu.ini(params)
    gpu.bind(120, view)
    resource = gpu.buffer(data, structured=4, writable=True)
    gpu.bind(5, gpu.view(resource, data.size, 0, writable=True), True)
    gpu.dispatch(gpu.shader(path), 7)
    gpu.bind(5, None, True)
    actual = gpu.read_bytes(resource, data.nbytes).view(np.uint32).reshape(data.shape)
    np.testing.assert_array_equal(actual[:, :4], expected[:, :4])
    np.testing.assert_array_equal(actual[:, 5:], expected[:, 5:])
    # f32tof16 rounding is hardware-defined here; compare the packed result
    # against the original full-stream GPU algorithm, not NumPy's conversion.
    reference = src
    for block, statement in (('LOAD', 'data[k] = rw_buffer[base + k];'),
                             ('STORE', 'rw_buffer[base + k] = data[k];')):
        reference = re.sub(r'// --- \[PYTHON-MANAGED '+block+r' START\] ---.*?// --- \[PYTHON-MANAGED '+block+r' END\] ---',
                           '[unroll] for(uint k=0;k<UV_STREAM_UINTS_PER_VERTEX;++k){'+statement+'}',
                           reference, flags=re.S)
    path = tmp_path / 'reference.hlsl'
    path.write_text(reference, encoding='utf-8')
    resource = gpu.buffer(data, structured=4, writable=True)
    gpu.bind(5, gpu.view(resource, data.size, 0, writable=True), True)
    gpu.dispatch(gpu.shader(path), 7)
    gpu.bind(5, None, True)
    expected = gpu.read_bytes(resource, data.nbytes).view(np.uint32).reshape(data.shape)
    np.testing.assert_array_equal(actual, expected)
