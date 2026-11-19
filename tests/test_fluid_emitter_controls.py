"""Independent emitter options and shading-frame transport regressions."""
import ctypes as ct
import importlib.util
from pathlib import Path
import struct
import sys
from types import SimpleNamespace

import numpy as np
import pytest

from test_fluid_probe_node_keys import _bare_node, _emitter, node, merge

ROOT = Path(__file__).resolve().parents[1]


def test_feature_table_preserves_legacy_defaults_and_isolates_each_owner():
    instance = _bare_node([_emitter(tail_emission=False, splash_enable=True),
                           _emitter(tail_emission=True, splash_enable=False), _emitter()])
    instance.tail_emission = instance.splash_enable = False
    sections = merge.parse_sections(instance._build_main_ini('FluidTest', []))
    assert 'data = 2 1 0 0 0 0 0 0' in sections['ResourceEmitterFeatures']
    instance.emitters[0].tail_emission = True
    updated = merge.parse_sections(instance._build_main_ini('FluidTest', []))
    assert 'data = 3 1 0 0 0 0 0 0' in updated['ResourceEmitterFeatures']


@pytest.mark.parametrize('case', ['valid', 'nan', 'wrong_format', 'negative_index', 'foreign_binding', 'non_utf8'])
def test_export_frame_reads_the_same_indices_as_the_capture_hook(tmp_path, case):
    from importlib import import_module
    helper = import_module(node.__package__+'.fluid_surface_frame')
    rows = np.array([[0, 0, 0, 0, 0, 1, 1, 0, 0, 1],
                     [0, 1, 0, 0, 0, 1, 1, 0, 0, 1],
                     [1, 0, 0, 0, 0, 1, 1, 0, 0, 1]], dtype='<f4')
    (tmp_path/'position.buf').write_bytes(rows.tobytes())
    (tmp_path/'index.buf').write_bytes(struct.pack('<6I', 0, 0, 0, 2, 0, 1))
    (tmp_path/'model.ini').write_text('''[Resource12345678Position]
stride = 40
filename = position.buf
[ResourceIndices]
format = DXGI_FORMAT_R32_UINT
filename = index.buf
[TextureOverrideBody]
ib = ResourceIndices
; [mesh:LOD0.12345678-3-0.Body_copy]
drawindexed = 3,3,0
''')
    emitter = SimpleNamespace(object_name='LOD0.12345678-3-0.Body', poly_index=0,
                              first_index=-1, ib_file='', base_vertex=0)
    ini = tmp_path/'model.ini'
    if case == 'nan':
        rows[0, 3] = np.nan
        (tmp_path/'position.buf').write_bytes(rows.tobytes())
    elif case == 'wrong_format':
        ini.write_text(ini.read_text().replace('DXGI_FORMAT_R32_UINT', 'DXGI_FORMAT_R8_UINT'))
    elif case == 'negative_index':
        emitter.base_vertex = -1
    elif case == 'foreign_binding':
        ini.write_text(ini.read_text().replace('[TextureOverrideBody]\nib = ResourceIndices',
                                              '[Unrelated]\nib = ResourceIndices\n[TextureOverrideBody]'))
    elif case == 'non_utf8':
        (tmp_path/'000-legacy.ini').write_bytes(b'\xff')
    result = helper.exported_triangle_rows(tmp_path, emitter)
    if case in ('valid', 'non_utf8'):
        np.testing.assert_array_equal(result, rows[[2, 0, 1]])
    else:
        assert result is None


@pytest.fixture(scope='module')
def gpu():
    spec = importlib.util.spec_from_file_location('emitter_control_gpu', ROOT/'tools/check_drag_cloth.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    value = module.GPU()
    yield value
    value.release()


@pytest.mark.skipif(sys.platform != 'win32', reason='D3D11 needs Windows')
def test_features_follow_emitter_id_instead_of_runtime_slot(gpu, tmp_path):
    from test_fluid_gpu_gating import bindings
    bindings(gpu)
    source = '#include "ParticleConfig.hlsl"\nTexture1D<float4> IniParams:register(t120);\n#include "Slots.hlsl"\n'
    source += '''RWStructuredBuffer<float4> Result:register(u7);
    [numthreads(8,1,1)]void main(uint3 id:SV_DispatchThreadID){
      uint owner=slotEmitterId(id.x);Result[id.x]=float4(owner,emitterTailEnabled(owner),emitterSplashEnabled(owner),0);
    }'''
    path = tmp_path/'controls.hlsl'
    for name in ('ParticleConfig.hlsl', 'Slots.hlsl', 'EmitterFeatures.hlsl'):
        (tmp_path/name).write_text((ROOT/'Toolset/fluid/fluid'/name).read_text('utf-8'),encoding='utf-8')
    path.write_text(source,encoding='utf-8')
    flattened = tmp_path/'flat.hlsl'
    flattened.write_text(node.SSMTNode_PostProcess_FluidProbe._remap_hlsl(gpu.compiler.expand(str(path))),encoding='utf-8')
    flags = gpu.buffer(np.array([1, 2, 0, 3, 0, 0, 0, 0],np.uint32))
    gpu.bind(9,gpu.view(flags,8,42))
    data = np.zeros((8,4),np.float32)
    output = gpu.buffer(data,structured=16,writable=True)
    gpu.bind(7,gpu.view(output,8,0,writable=True),True)
    gpu.dispatch(gpu.shader(flattened),1)
    gpu.bind(7,None,True)
    result=gpu.read_bytes(output,data.nbytes).view(np.float32).reshape(8,4)
    np.testing.assert_array_equal(result[:4,:3],[[2,0,0],[0,1,0],[1,0,1],[2,0,0]])
    gpu.bind(9,None)


@pytest.mark.skipif(sys.platform != 'win32', reason='D3D11 needs Windows')
def test_disabled_splash_owners_do_not_consume_other_owners_children(gpu, tmp_path):
    from test_fluid_gpu_gating import bindings
    bindings(gpu)
    source=gpu.compiler.expand(str(ROOT/'Toolset/fluid/fluid/SplashSpawn.hlsl'))
    source=source.replace('void main(uint3 id:SV_DispatchThreadID)', 'void splashMain(uint3 id:SV_DispatchThreadID)')
    source+='''\nRWStructuredBuffer<float4> Result:register(u7);
    [numthreads(4,1,1)]void main(uint3 id:SV_DispatchThreadID){
      CollisionEvent e=(CollisionEvent)0;e.slot=float4(id.x,1,0,0);
      e.normalRadius=float4(0,1,0,.003);
      Result[id.x]=float4(childCount(e),0,0,0);
    }'''
    # The unused full spawn entry owns u7; remove its transfer-list declaration.
    source=source.replace('RWBuffer<uint> puddleTransferList:register(u7);',
                          'RWBuffer<uint> puddleTransferList:register(u6);')
    path=tmp_path/'children.hlsl';path.write_text(node.SSMTNode_PostProcess_FluidProbe._remap_hlsl(source),encoding='utf-8')
    flags=gpu.buffer(np.array([1,2,0,3,0,0,0,0],np.uint32));gpu.bind(9,gpu.view(flags,8,42))
    data=np.zeros((4,4),np.float32);output=gpu.buffer(data,structured=16,writable=True)
    gpu.bind(7,gpu.view(output,4,0,writable=True),True);gpu.dispatch(gpu.shader(path),1);gpu.bind(7,None,True)
    result=gpu.read_bytes(output,data.nbytes).view(np.float32).reshape(4,4)
    np.testing.assert_array_equal(result[:,0],[0,0,1,0])
    gpu.bind(9,None)


def texture(gpu, values):
    class Desc(ct.Structure):
        _fields_=[(name,ct.c_uint) for name in ('Width','Height','MipLevels','ArraySize','Format',
                                              'SampleCount','SampleQuality','Usage','BindFlags','CPUAccessFlags','MiscFlags')]
    data=np.ascontiguousarray(values,dtype=np.float32).reshape(1,3,4)
    desc=Desc(3,1,1,1,2,1,0,0,8,0,0)
    # D3D11_SUBRESOURCE_DATA is shared with the buffer factory.
    class Initial(ct.Structure):
        _fields_=[('data',ct.c_void_p),('row',ct.c_uint),('slice',ct.c_uint)]
    initial=Initial(data.ctypes.data,48,0)
    resource=ct.c_void_p()
    gpu.check(gpu.method(gpu.device,5,[ct.c_void_p,ct.c_void_p,ct.c_void_p],ct.byref(desc),ct.byref(initial),ct.byref(resource)))
    gpu.keep(resource)
    view=ct.c_void_p()
    gpu.check(gpu.method(gpu.device,7,[ct.c_void_p,ct.c_void_p,ct.c_void_p],resource,None,ct.byref(view)))
    return gpu.keep(view)


@pytest.mark.skipif(sys.platform != 'win32', reason='D3D11 needs Windows')
@pytest.mark.parametrize('collapsed', [False, True])
def test_surface_direction_uses_live_shading_frame_without_moving_anchor(gpu,tmp_path,collapsed):
    path=tmp_path/'resolve.hlsl'
    path.write_text(gpu.compiler.expand(str(ROOT/'Toolset/fluid/surface/ResolveSurface.hlsl')),encoding='utf-8')
    shader=gpu.shader(path)
    params=np.zeros((256,4),np.float32);params[242]=[-10,10,0,3]
    _,ini=gpu.ini(params);gpu.bind(120,ini)
    captured=np.array([[0,.85,0,1],[.01,.85,0,1],[0,.85+.01*np.tan(np.deg2rad(32)),.01,1]],np.float32)
    if collapsed:
        captured[2] = captured[0]
    for slot,values in ((0,captured),(3,[[0,1,0,1]]*3),(4,[[1,0,0,1]]*3)):
        gpu.bind(slot,texture(gpu,values))
    profile=np.zeros((5,4),np.float32);profile[0]=[-1,1/3,1/3,1/3];profile[1]=[0,1,0,0];profile[2]=[0,0,-1,0]
    camera=np.zeros((24,4),np.float32);camera[20,0]=camera[21,1]=camera[22,2]=1
    for slot,values in ((1,profile),(2,np.zeros((8,4),np.float32)),(113,camera)):
        resource=gpu.buffer(values,structured=16);gpu.bind(slot,gpu.view(resource,len(values),0))
    instance=np.zeros((4096,4),np.float32);instance[3,3]=1
    cb=ct.c_void_p(gpu.raw_buffer(instance.view(np.uint8),4))
    gpu.method(gpu.context,71,[ct.c_uint,ct.c_uint,ct.c_void_p],1,1,ct.byref(cb),result=None)
    results=[]
    for enabled in (False,True):
        frames=np.array([[0,-1,0,int(enabled)],[0,0,1,0]],np.float32)
        resource=gpu.buffer(frames);gpu.bind(10,gpu.view(resource,2,2))
        candidates=np.zeros((145,4),np.float32);output=gpu.buffer(candidates,structured=16,writable=True)
        stats=gpu.buffer(np.zeros((16,4),np.float32),structured=16,writable=True)
        gpu.bind(0,gpu.view(output,145,0,writable=True),True);gpu.bind(1,gpu.view(stats,16,0,writable=True),True)
        gpu.dispatch(shader,1);gpu.bind(0,None,True);gpu.bind(1,None,True)
        results.append(gpu.read_bytes(output,candidates.nbytes).view(np.float32).reshape(145,4))
    assert results[0][0,0] == (0 if collapsed else 1)
    assert results[1][0,0] == 1
    np.testing.assert_allclose(-results[1][2,:3],[0,0,1],atol=1e-6)
    if collapsed:
        np.testing.assert_allclose(results[1][4,:3], captured[:,:3].mean(axis=0), atol=1e-6)
    else:
        assert abs(results[0][2,1])>.5
        np.testing.assert_array_equal(results[0][4],results[1][4])


@pytest.mark.skipif(sys.platform != 'win32', reason='D3D11 needs Windows')
@pytest.mark.parametrize('position', range(13))
def test_surface_capture_variants_compile_with_shading_outputs(gpu, position):
    name = 'CaptureTriangle.hlsl' if position == 0 else 'CaptureTriangle_Position%d.hlsl' % position
    path = ROOT/'Toolset/fluid/surface'/name
    source = '#define GEOMETRY_SHADER 1\n'+gpu.compiler.expand(str(path))
    hr, error = gpu.compiler.compile_one(gpu.compile, source, str(path), 'gs')
    assert hr >= 0, error
    if position == 0:
        source = '#define PIXEL_SHADER 1\n'+gpu.compiler.expand(str(path))
        hr, error = gpu.compiler.compile_one(gpu.compile, source, str(path), 'ps')
        assert hr >= 0, error
