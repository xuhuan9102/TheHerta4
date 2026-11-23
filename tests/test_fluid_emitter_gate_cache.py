"""Dirty-input gate caching, including cross-INI writers and key callbacks."""
from test_fluid_runtime_gating import IniTrace, generated, merge
from test_fluid_probe_node_keys import _bare_node, _emitter


def test_static_main_view_recomputes_only_once():
    trace = IniTrace(generated())
    trace.run('TextureOverrideFluidGate_9040e2c4')
    for _ in range(100):
        trace.run('ShaderRegexLiquidMainView')
    assert trace.calls.count('CommandListUpdateEmitterGate') == 1


def test_runtime_assignment_refreshes_the_next_consumer():
    text = generated() + '\n[CommandListChangeInputs]\n$fluid_slot_count = 1\n$fluid_emit_0 = 0\n'
    text = merge.mark_emitter_gate_writes(text)
    trace = IniTrace(text)
    trace.run('TextureOverrideFluidGate_9040e2c4')
    trace.run('ShaderRegexLiquidMainView')
    assert trace.values['$fluid_any_emitter'] == 1
    trace.run('CommandListChangeInputs')
    assert trace.values['$fluid_emitter_gate_dirty'] == 1
    trace.run('ShaderRegexLiquidMainView')
    assert trace.values['$fluid_any_emitter'] == 0
    assert trace.values['$fluid_emitter_gate_dirty'] == 0


def test_cross_ini_assignments_and_store_mark_the_same_phase_dirty():
    source = ('[Present]\n'
              '$\\Fluid_A\\fluid_emit_0 = 1\n'
              'post $\\Fluid_B\\fluid_slot_count = 2\n'
              'store = $\\Fluid_A\\fluid_emit_1, ResourceInput, 0\n'
              '$unrelated = 1\n')
    out = merge.mark_emitter_gate_writes(source, include_local=False)
    assert '$\\Fluid_A\\fluid_emit_0 = 1\n$\\Fluid_A\\fluid_emitter_gate_dirty = 1' in out
    assert 'post $\\Fluid_B\\fluid_slot_count = 2\npost $\\Fluid_B\\fluid_emitter_gate_dirty = 1' in out
    assert 'ResourceInput, 0\n$\\Fluid_A\\fluid_emitter_gate_dirty = 1' in out
    assert merge.mark_emitter_gate_writes(out, include_local=False) == out


def test_cycle_key_marks_dirty_in_post_callback_without_changing_smart_values():
    out = merge.build_emitter_key_block([(0, 'VK_F1')])
    section = merge.parse_sections(out)['KeyFluidEmit0']
    assert 'run = CommandListMarkFluidEmitterGateDirty,CommandListMarkFluidEmitterGateDirty' in '\n'.join(section)
    assert [line.strip() for line in section if line.strip().startswith('$')] == ['$fluid_emit_0 = 1,0']


def test_existing_cycle_callbacks_are_wrapped_in_order():
    source = ('[KeyExternal]\nkey = VK_F1\ntype = cycle\n'
              '$\\Fluid_A\\fluid_emit_0 = 1,0\nrun = CommandListFirst,CommandListSecond\n')
    out = merge.mark_emitter_gate_writes(source, include_local=False)
    sections = merge.parse_sections(out)
    run = next(line.split('=', 1)[1].strip() for line in sections['KeyExternal'] if line.startswith('run'))
    callbacks = run.split(',')
    for callback, original in zip(callbacks, ['CommandListFirst', 'CommandListSecond']):
        lines = sections[callback]
        assert lines.index('run = ' + original) < lines.index('post $\\Fluid_A\\fluid_emitter_gate_dirty = 1')
    assert merge.mark_emitter_gate_writes(out, include_local=False) == out


def test_transition_key_disables_cache_until_reload():
    source = ('[KeyExternal]\ntype = cycle\ntransition = 0.2\n'
              '$\\Fluid_A\\fluid_emit_0 = 1,0\n')
    out = merge.mark_emitter_gate_writes(source, include_local=False)
    assert 'post $\\Fluid_A\\fluid_emitter_gate_cache_safe = 0' in out


def test_low_frequency_refresh_still_observes_uninstrumented_external_changes():
    trace = IniTrace(generated())
    trace.run('TextureOverrideFluidGate_9040e2c4')
    trace.run('ShaderRegexLiquidMainView')
    trace.run('Present')
    for i in range(8):
        trace.values['$fluid_emit_%d' % i] = 0
    trace.run('Present')
    assert trace.values['$fluid_any_emitter'] == 0


def test_retrofitting_existing_output_is_idempotent():
    text = generated()
    assert merge.apply_emitter_gate_cache(text) == text


def test_first_hotkey_value_is_not_overwritten_by_late_initialization():
    text = _bare_node([_emitter(enable=False)])._build_main_ini('FluidTest', [])
    trace = IniTrace(text)
    trace.values.update({'$fluid_present': 1, '$fluid_seen': 1})
    trace.run('Present')
    trace.values['$fluid_emit_0'] = 1
    trace.values['$fluid_emitter_gate_dirty'] = 1
    trace.run('CommandListInjectWorld')
    assert trace.values['$fluid_emit_0'] == 1
