"""Exercise the generated UI bridge, including disabled and failed reads."""
import pytest

from test_fluid_runtime_gating import IniTrace
from test_node_postprocess_draginteraction import _load_drag_module, _make_node, _base_sections


def generated_ui_bridge():
    module = _load_drag_module()
    node = _make_node(module)
    sections = _base_sections()
    components = node._locate_components(sections, ['abc123'])
    node._emit_sections(sections, components, 'testns')
    node._emit_present_and_constants(sections, components, 'testns')
    text = '\n'.join(key + '\n' + '\n'.join(lines) for key, lines in sections.items()
                     if isinstance(lines, list))
    return IniTrace(text)


@pytest.mark.parametrize('allowed', [0, 2])
def test_ui_bridge_does_not_read_when_detection_is_disabled(allowed):
    trace = generated_ui_bridge()
    trace.values['$ObjectDetectAllowed_testns'] = allowed
    trace.readbacks = {'ResourceDragPinnedDetectID_testns': 12,
                       'ResourceDragZoneOut_testns': 3}
    trace.run('CommandListDragUIReadback_testns')
    assert trace.stores == []
    assert trace.values['$ssmtdrag_ui_detected_testns'] == -1
    assert trace.values['$ssmtdrag_ui_zone_testns'] == -1


@pytest.mark.parametrize('detected,zone,expected', [(12, 3, (12, 3)), (-1, 3, (-1, -1)),
                                                  (0, 0, (0, 0))])
def test_enabled_bridge_keeps_read_order_and_failure_semantics(detected, zone, expected):
    trace = generated_ui_bridge()
    trace.values['$ObjectDetectAllowed_testns'] = 1
    trace.values['$ssmtdrag_drag_enabled_testns'] = 1
    trace.readbacks = {'ResourceDragPinnedDetectID_testns': detected,
                       'ResourceDragZoneOut_testns': zone}
    trace.run('CommandListDragUIReadback_testns')
    assert [item[1] for item in trace.stores] == ['ResourceDragPinnedDetectID_testns',
                                               'ResourceDragZoneOut_testns']
    assert (trace.values['$ssmtdrag_ui_detected_testns'], trace.values['$ssmtdrag_ui_zone_testns']) == expected


def test_parameter_changes_still_schedule_gpu_push_with_ui_detection_off():
    module = _load_drag_module()
    node = _make_node(module)
    node._drag_drive_var_sync_bindings = lambda: [('$ExportedWeight', 0, 0, 0xFFFFFFFF)]
    sections = {}
    node._emit_shapekey_var_readback_command_list(sections, 'testns')
    node._emit_shapekey_var_sync_section(sections, 'testns')
    text = '\n'.join(key + '\n' + '\n'.join(lines) for key, lines in sections.items())
    trace = IniTrace(text)
    trace.values.update({'$ObjectDetectAllowed_testns': 0,
                         '$ssmtdrag_drag_enabled_testns': 2, '$ExportedWeight': 0.75})
    trace.run('CommandListDragShapeKeyVarReadback_testns')
    assert trace.stores == []
    assert trace.values['$ssmtdrag_skany_testns'] == 1
    trace.run('CustomShaderDragShapeKeyVarSync_testns')
    assert trace.values['x81'] == 0.75
    assert trace.values['x90'] == 2
    assert any(row[1] == 'dispatch' for row in trace.work)


def test_mode_two_never_reads_ui_even_with_alt_detection_allowed():
    trace = generated_ui_bridge()
    trace.values.update({'$ObjectDetectAllowed_testns': 1, '$ssmtdrag_drag_enabled_testns': 2})
    trace.run('CommandListDragUIReadback_testns')
    assert trace.stores == []
    assert trace.values['$ssmtdrag_ui_detected_testns'] == -1


def parameter_trace():
    module = _load_drag_module()
    node = _make_node(module)
    node._drag_drive_var_sync_bindings = lambda: [('$ExportedWeight', 0, 0, 0xFFFFFFFF)]
    sections = {}
    node._emit_shapekey_var_readback_command_list(sections, 'testns')
    return IniTrace('\n'.join(key + '\n' + '\n'.join(lines) for key, lines in sections.items()))


def test_mode_two_cancels_old_readback_handshake_and_pushes_latest_cpu_value():
    trace = parameter_trace()
    trace.values.update({'$ssmtdrag_drag_enabled_testns': 2, '$ExportedWeight': 0.75,
                         '$ssmtdrag_skprev_testns_0': 0.25, '$ssmtdrag_skpending_testns_0': 1,
                         '$ssmtdrag_sksyncmode_testns': 1, '$ssmtdrag_skheldprev_testns': 1})
    trace.readbacks = {'ResourceDragShapeKeyDrive_testns': 0.1}
    trace.run('CommandListDragShapeKeyVarReadback_testns')
    assert trace.stores == []
    assert trace.values['$ExportedWeight'] == 0.75
    assert trace.values['$ssmtdrag_skprev_testns_0'] == 0.75
    assert trace.values['$ssmtdrag_skpending_testns_0'] == 0
    assert trace.values['$ssmtdrag_skany_testns'] == 1
    trace.run('CommandListDragShapeKeyVarReadback_testns')
    assert trace.stores == []
    assert trace.values['$ssmtdrag_skany_testns'] == 0


def test_returning_to_mode_one_does_not_pull_a_stale_gpu_value():
    trace = parameter_trace()
    trace.values.update({'$ssmtdrag_drag_enabled_testns': 1, '$ExportedWeight': 0.75,
                         '$ssmtdrag_skprev_testns_0': 0.75, '$ssmtdrag_sksyncmode_testns': 2})
    trace.readbacks = {'ResourceDragShapeKeyDrive_testns': 0.1}
    trace.run('CommandListDragShapeKeyVarReadback_testns')
    assert trace.values['$ExportedWeight'] == 0.75
    assert trace.values['$ssmtdrag_skpending_testns_0'] == 1
    assert trace.values['$ssmtdrag_skany_testns'] == 1


def test_mode_two_keeps_gpu_detection_but_does_not_drive_interactive_shape_keys():
    module = _load_drag_module()
    node = _make_node(module, enable_shapekey_drive=True)
    sections = _base_sections()
    components = node._locate_components(sections, ['abc123'])
    node._emit_sections(sections, components, 'testns')
    section = 'CommandListDragPinDetected_testns'
    external = [key[1:-1] for key in sections if key.startswith('[') and key != '[' + section + ']']
    text = '\n'.join(key + '\n' + '\n'.join(lines) for key, lines in sections.items()
                     if isinstance(lines, list))
    trace = IniTrace(text, external=external)
    trace.values.update({'$ssmtdrag_booted_testns': 1, '$ssmtdrag_drag_enabled_testns': 2,
                         '$ssmtdrag_mode_testns': 1, '$ssmtdrag_drawn_testns': 1})
    trace.run(section)
    assert 'CustomShaderDragPinDetected_testns' in trace.calls
    assert 'CustomShaderDragUpdateScreenJiggle_testns' in trace.calls
    assert 'CustomShaderDragShapeKeyDrive_testns' not in trace.calls


@pytest.mark.parametrize('values', ['', '0 1'])
def test_click_exports_have_no_mode_two_readback(values):
    from test_anim_driver_click_export import _click_node
    node = _click_node(values=values)
    owner = node._find_drag_drive_node()
    owner._runtime_variable_names = lambda ns: ('$DragMode', '$UI', '$Zone')
    trace = IniTrace(node.generate_ini_segment())
    trace.values.update({'$DragMode': 2, '$ssmtdrag_booted_A': 1, '$Swap': 0.75,
                         '$ssmtdrag_ckedge_A_Click_Export_2': 1})
    trace.run('Present')
    assert trace.stores == []
    assert trace.values['$Swap'] == 0.75
    if values:
        assert trace.values['$ssmtdrag_ckedge_A_Click_Export_2'] == 0
        assert trace.values['$ssmtdrag_ckinit_A_Click_Export_2'] == 0
    else:
        assert trace.values['$ssmtdrag_seed_pending_A'] == 1
