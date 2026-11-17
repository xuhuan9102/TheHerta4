"""Execute the generated INI's CPU command-list gates and count GPU work."""
from collections import defaultdict
from itertools import product
import re

import pytest

from test_fluid_probe_node_keys import _bare_node, _emitter, merge


class IniTrace:
    def __init__(self, text, external=()):
        self.sections = merge.parse_sections(text)
        self.values = defaultdict(float)
        self.work = []
        self.calls = []
        self.external = set(external)
        for line in self.sections.get('Constants', []):
            match = re.match(r'global\s+(\$\w+)\s*=\s*([^;]+)', line.strip())
            if match:
                self.values[match[1]] = self.expression(match[2])

    def expression(self, expression):
        expression = expression.strip().replace('!==', '!=').replace('===', '==')
        expression = re.sub(
            r'\b(?:vs|ps|cs|gs)-(?:cb|t|u)\d+(?:->\w+)?|\bo\d+(?:->\w+)?|\bod\b|\bDRAW_TYPE\b',
            lambda m: 'v[%r]' % m[0], expression)
        expression = re.sub(r'\$[\w\\]+', lambda m: 'v[%r]' % m[0], expression)
        expression = expression.replace('&&', ' and ').replace('||', ' or ')
        expression = re.sub(r'!(?!=)', ' not ', expression)
        expression = re.sub(r'\btime\b', '1.0', expression)
        expression = re.sub(r'\bnull\b', '0', expression)
        return eval(expression.strip(), {'__builtins__': {}}, {'v': self.values})

    def run(self, section):
        self.calls.append(section)
        if section in self.external:
            return
        if section not in self.sections:
            assert section.startswith('CommandList\\') or section == 'BuiltInCommandListUnbindAllRenderTargets', section
            return  # External TTL composite has no local compute work.
        conditions = []
        active = True
        for raw in self.sections[section]:
            line = raw.split(';', 1)[0].strip()
            if not line or line.startswith('post '):
                continue
            if line.startswith('if '):
                choice = bool(self.expression(line[3:])) if active else False
                conditions.append([active, choice])
                active = active and choice
            elif line.startswith('elif '):
                parent, chosen = conditions[-1]
                active = parent and not chosen and bool(self.expression(line[5:]))
                conditions[-1][1] |= active
            elif line == 'else':
                parent, chosen = conditions[-1]
                active = parent and not chosen
                conditions[-1][1] = True
            elif line == 'endif':
                active = conditions.pop()[0]
            elif active:
                key, sep, value = line.partition('=')
                if not sep:
                    continue
                key, value = key.strip(), value.strip()
                if key == 'run':
                    self.run(value)
                elif key in ('dispatch', 'draw', 'drawinstanced', 'drawindexedinstanced', 'clear'):
                    self.work.append((section, key, value, self.values['x65']))
                elif value.startswith(('copy ', 'copy_desc ')):
                    self.work.append((section, 'copy', key, self.values['x65']))
                elif key.startswith('$'):
                    self.values[key] = self.expression(value)
                elif re.fullmatch(r'[xyzw]\d+', key):
                    self.values[key] = self.expression(value)
        assert not conditions, section


def generated(count=3, remap=False):
    instance = _bare_node([_emitter() for _ in range(count)])
    instance.remap_ini_params = remap
    return instance._build_main_ini('FluidTest', [])


@pytest.mark.parametrize('enabled', list(product((0, 1), repeat=3)))
@pytest.mark.parametrize('remap', [False, True])
def test_only_enabled_emitters_dispatch_primary_solver(enabled, remap):
    trace = IniTrace(generated(remap=remap))
    trace.values.update({'$fluid_present': 1, '$fluid_seen': 1,
                         '$fluid_initialized': 1})
    trace.values.update({'$fluid_emit_%d' % i: on for i, on in enumerate(enabled)})
    trace.run('CommandListInjectWorld')
    primary = [row for row in trace.work if row[0] == 'CustomShaderFluidPredict']
    if not any(enabled):
        assert trace.work == []
    else:
        assert [int(row[3]) for row in primary] == [i for _ in range(2) for i, on in enumerate(enabled) if on]
        assert all(row[2] == '16,1,1' for row in primary)


def test_all_off_present_and_end_frame_do_no_repeated_gpu_work():
    trace = IniTrace(generated())
    trace.values.update({'$fluid_present': 1, '$fluid_seen': 1,
                         '$fluid_emitting': 1, '$fluid_initialized': 1,
                         '$fluid_trickle9': 1})
    for i in range(8):
        trace.values['$fluid_emit_%d' % i] = 0
    for _ in range(3):
        trace.run('Present')
        trace.run('CommandListEndFrame')
    assert trace.work == []
    assert trace.values['$fluid_emitting'] == 0
    assert trace.values['$fluid_initialized'] == 0


def test_capture_cleanup_runs_once_after_shutdown():
    trace = IniTrace(generated())
    trace.values['$fluid_capture_dirty'] = 1
    for _ in range(3):
        trace.run('CommandListEndFrame')
    assert [(row[1], row[2]) for row in trace.work] == [
        ('clear', 'ResourceSurfaceCandidates'), ('clear', 'ResourceSurfaceStats')]


def test_all_off_shader_hooks_do_not_copy_camera_or_bind_tonemap():
    trace = IniTrace(generated())
    trace.values.update({'$fluid_present': 1, '$fluid_seen': 1, '$fluid_any_emitter': 1,
                         '$fluid_main_width': 1920, '$fluid_view_width': 1000,
                         'o3': 1, 'od': 1, 'vs-cb1': 3120464, 'o0->Width': 1920,
                         'ps-t0': 1, 'ps-t0->Width': 1920})
    for i in range(8):
        trace.values['$fluid_emit_%d' % i] = 0
    trace.run('ShaderRegexLiquidMainView')
    trace.run('ShaderOverrideLiquidTonemap')
    assert trace.work == []
    assert 'CommandListInjectWorld' not in trace.calls


@pytest.mark.parametrize('count', [0, 1, 3, 8])
def test_unused_emitter_controls_cannot_wake_the_probe(count):
    trace = IniTrace(generated(count))
    trace.values.update({'$fluid_present': 1, '$fluid_seen': 1, '$fluid_initialized': 1})
    for i in range(count):
        trace.values['$fluid_emit_%d' % i] = 0
    trace.run('CommandListInjectWorld')
    assert trace.work == []


@pytest.mark.parametrize('enabled', list(product((0, 1), repeat=3)))
@pytest.mark.parametrize('remap', [False, True])
def test_host_capture_hooks_skip_closed_emitters(tmp_path, enabled, remap):
    emitters = [_emitter(object_name='Body%d' % i, poly_index=0,
                         first_index=-1, ib_file='', base_vertex=0) for i in range(3)]
    instance = _bare_node(emitters)
    instance.remap_ini_params = remap
    host = tmp_path / 'host.ini'
    host.write_text('\n'.join('[TextureOverrideBody%d]\n; [mesh:Body%d]\ndrawindexed = 3,0,0\n' % (i, i)
                              for i in range(3)), encoding='utf-8')
    instance._inject_emitter_hooks(str(tmp_path), 'FluidTest')
    text = host.read_text(encoding='utf-8')
    # Capture routing is checked here; its actual shader work is checked through
    # the main INI and D3D11 tests, without needing a game mesh/camera in this trace.
    capture = r'CommandList\FluidTest\CaptureSurface'
    mark = r'CommandList\FluidTest\MarkPresent'
    trace = IniTrace(text, external=(capture, mark))
    trace.values.update({r'$\FluidTest\fluid_emit_%d' % i: on for i, on in enumerate(enabled)})
    for i, on in enumerate(enabled):
        trace.calls.clear()
        trace.run('TextureOverrideBody%d' % i)
        assert trace.calls.count(capture) == on
        assert trace.calls.count(mark) == on
    # Exporting again must replace the gated blocks, including their endif.
    instance._inject_emitter_hooks(str(tmp_path), 'FluidTest')
    assert host.read_text(encoding='utf-8') == text


@pytest.mark.parametrize('remap', [False, True])
def test_header_update_invalidates_entry_shader_cache(tmp_path, remap):
    instance = _bare_node([_emitter()])
    instance.remap_ini_params = remap
    instance.emit_legacy_buffers = False
    instance._build_profiles = lambda: []
    instance._build_index = lambda *args: {}
    instance._build_profile_loader = lambda *args: '[Constants]\n'
    instance._retarget_host_refs = lambda *args: []
    instance._deploy(str(tmp_path), 'FluidTest', [])
    shaders = tmp_path / 'res/fluid_probe/fluid'
    cache = shaders / 'FluidPredict.cs_5_0.test.bin'
    cache.write_bytes(b'old compiled solver')
    instance._deploy(str(tmp_path), 'FluidTest', [])
    assert cache.exists(), 'unchanged exports must preserve valid caches'
    header = shaders / 'FluidSolver.hlsl'
    header.write_text(header.read_text(encoding='utf-8') + '\n// old header\n', encoding='utf-8')
    instance._deploy(str(tmp_path), 'FluidTest', [])
    assert not cache.exists(), 'entry caches must follow included headers, too'


def test_legacy_over_capacity_emitters_do_not_reference_undefined_controls(tmp_path):
    emitters = [_emitter(object_name='Body%d' % i, poly_index=0,
                         first_index=-1, ib_file='', base_vertex=0) for i in range(9)]
    instance = _bare_node(emitters)
    host = tmp_path / 'host.ini'
    host.write_text('\n'.join('[TextureOverrideBody%d]\n; [mesh:Body%d]\ndrawindexed = 3,0,0\n' % (i, i)
                              for i in range(9)), encoding='utf-8')
    instance._inject_emitter_hooks(str(tmp_path), 'FluidTest')
    text = host.read_text(encoding='utf-8')
    assert r'$\FluidTest\fluid_emit_7' in text
    assert r'$\FluidTest\fluid_emit_8' not in text
    assert '; nhd-pick 8 begin' not in text
