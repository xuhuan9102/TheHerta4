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


@pytest.mark.parametrize('degenerate', [False, True])
def test_shared_calibration_matches_per_thread_calibration(gpu, tmp_path, degenerate):
    source = (ROOT / 'Toolset/drag_interaction/rzm_object_detect.hlsl').read_text('utf-8')
    source = source[:source.index('[numthreads(THREADS_PER_GROUP, 1, 1)]')]
    fixture = '''bool ReadCalibSample(uint slot,uint count,out float3 p,out float4 c){
        p=float3(slot&1u,(slot>>1u)&1u,(slot>>2u)&1u);
        if(IniParams[0].x>.5)p.z=0;
        c=float4(p.x*.8+p.y*.1,p.y*.7+p.z*.2,p.z*.9+.1,1+p.z*.2);
        return true;
    }'''
    source = re.sub(r'bool ReadCalibSample\([^\n]*\)\s*\{.*?\n\}', fixture, source, count=1, flags=re.S)
    params = np.zeros((128, 4), np.float32)
    params[0, 0] = int(degenerate)
    _, ini = gpu.ini(params)
    gpu.bind(120, ini)
    results = []
    for shared in (False, True):
        select = ('if(lane==0){Calibration c;SelectCalibration(c);sharedCalibration=c;}'
                  'GroupMemoryBarrierWithGroupSync();cal=sharedCalibration;') if shared else 'SelectCalibration(cal);'
        main = '''RWStructuredBuffer<float4> Result:register(u7);
        [numthreads(128,1,1)]void main(uint lane:SV_GroupIndex){
            Calibration cal;SELECT
            uint b=lane*11;
            Result[b]=float4(cal.p0,cal.valid);Result[b+1]=float4(cal.p1,cal.invDet);
            Result[b+2]=float4(cal.p2,0);Result[b+3]=float4(cal.p3,0);
            Result[b+4]=cal.c0;Result[b+5]=cal.c1;Result[b+6]=cal.c2;Result[b+7]=cal.c3;
            Result[b+8]=cal.col0;Result[b+9]=cal.col1;Result[b+10]=cal.col2;
        }'''.replace('SELECT', select)
        path = tmp_path / ('shared.hlsl' if shared else 'reference.hlsl')
        path.write_text(source + main, encoding='utf-8')
        data = np.full((128*11, 4), -321, np.float32)
        resource = gpu.buffer(data, structured=16, writable=True)
        gpu.bind(7, gpu.view(resource, len(data), 0, writable=True), True)
        gpu.dispatch(gpu.shader(path), 1)
        gpu.bind(7, None, True)
        results.append(gpu.read_bytes(resource, data.nbytes).view(np.float32))
    np.testing.assert_array_equal(*results)
    assert results[1][3] == (0 if degenerate else 1)


def test_zero_brightness_glow_does_not_draw():
    from test_fluid_runtime_gating import IniTrace
    text = (ROOT / 'Toolset/glow_fx/GlowFX/GlowFX.ini').read_text('utf-8')
    trace = IniTrace(text, external=('CustomShaderGeoGlowMask', 'CommandListGeoGlowStamp'))
    trace.values.update({
        '$glowfx_drawcount': 300,
        '$glowfx_brightness': 0,
        'o3': 1,  # 深度/颜色目标是运行时绑定；测试给个非空值让 gate 通过
        '$\\SlotFix\\is_color_shader': 1,
    })
    trace.run('CommandListGlowFXRun')
    assert 'CommandListGeoGlowStamp' not in trace.calls
    # 第一轮结束时 ini 会把 drawcount/brightness 复位，第二轮必须重新给。
    trace.values.update({'$glowfx_drawcount': 300, '$glowfx_brightness': 1})
    trace.run('CommandListGlowFXRun')
    assert 'CommandListGeoGlowStamp' in trace.calls


def test_cached_cloth_normals_match_inline_display_calculation(tmp_path):
    from tests.test_drag_cloth import GarmentExportTests
    GarmentExportTests.setUpClass()
    fixture = GarmentExportTests('test_self_collision_switch_omits_only_its_passes_and_assets')
    node, sections, comp = fixture.prepare(tmp_path)
    node._emit_sections(sections, [comp], 'unit')
    node._inject_draw_hooks(sections, comp, 'unit')
    spec = importlib.util.spec_from_file_location('cloth_normals_gpu', ROOT / 'tools/check_drag_cloth.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)

    def replay():
        runner = module.GeneratedCloth(tmp_path, sections, comp)
        try:
            runner.prepare_identity()
            garment = comp['cloth']['garments'][0]
            mask = garment['map'] != 0xFFFFFFFF
            rows = np.flatnonzero(mask)
            row = int(rows[len(rows)//2])
            result = [runner.frame(hit_row=row).copy()]
            result.append(runner.frame(600, True, row).copy())
            for frame in range(1, 5):
                result.append(runner.frame(600-frame*8, True, row).copy())
            result.append(runner.frame(568, False, row).copy())
            return result
        finally:
            runner.gpu.release()

    actual = replay()
    for path in (tmp_path / comp['cloth']['directory']).glob('g*/apply.hlsl'):
        normals = path.with_name('display_normals.hlsl').read_text('utf-8')
        function = normals[normals.index('float3 ParticleNormal'):normals.index('[numthreads(64,1,1)]')]
        source = path.read_text('utf-8')
        source = re.sub(r'Buffer<float4> DisplayNormals : register\(t74\);\s*float3 ParticleNormal\([^\n]+',
                        lambda _: function, source, count=1)
        path.write_text(source, encoding='utf-8')
    for name, lines in list(sections.items()):
        if isinstance(lines, list):
            sections[name] = [line for line in lines if not (
                line.strip().startswith('run = CustomShader') and 'Normals_s' in line)]
    expected = replay()
    for optimized, reference in zip(actual, expected):
        np.testing.assert_allclose(optimized.copy().view(np.float32), reference.copy().view(np.float32),
                                   rtol=1e-6, atol=1e-6)
