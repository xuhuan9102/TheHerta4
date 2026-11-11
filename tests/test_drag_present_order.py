import unittest

from tests.test_node_postprocess_draginteraction import (
    _base_sections,
    _load_drag_module,
    _make_node,
)


class DragPresentOrderTests(unittest.TestCase):
    def test_reexport_does_not_accumulate_blank_lines_after_shapekey_block(self):
        node = _make_node(_load_drag_module())
        lines = ['; --- SSMT DIRECT SHAPEKEY PRESENT BEGIN ---', 'if $active == 1',
                 'run = CustomShaderShape', 'endif', '; --- SSMT DIRECT SHAPEKEY PRESENT END ---', '', '']
        block = ['; --- DRAG PRESENT BEGIN ---', '$isMouseButtonDown = 0', '; --- DRAG PRESENT END ---']
        node._place_drag_present_block(lines, block)
        first = list(lines)
        lines.append('')  # INI reader retains the separator before the next section.
        node._place_drag_present_block(lines, block)
        self.assertEqual(lines, first)

    def _emit(self, **props):
        node = _make_node(_load_drag_module(), **props)
        sections = _base_sections()
        comps = node._locate_components(sections, ["abc123"])
        node._emit_sections(sections, comps, "testns")
        return node, sections, comps

    def test_drag_present_runs_before_ui_model_binding_marker(self):
        node, sections, comps = self._emit()
        sections["[Present]"] = [
            "    $ui_layout = 1",
            "",
            "    ; --- MODEL DRAG BINDING BEGIN ---",
            "    if $mouse_clicked == 1 && $is_dragging == 0 && $help == 1",
            "        if $ssmtdrag_ui_detected_testns >= 0 && ($ssmtdrag_ui_zone_testns == 0)",
            "            $is_dragging = 9",
            "        endif",
            "    endif",
            "    ; --- MODEL DRAG BINDING END ---",
        ]
        node._emit_present_and_constants(sections, comps, "testns")
        present = "\n".join(sections["[Present]"])
        self.assertLess(present.index("DRAG PRESENT BEGIN"), present.index("MODEL DRAG BINDING BEGIN"))
        self.assertLess(present.index("pre run = CommandListDragPinDetected_testns"), present.index("MODEL DRAG BINDING BEGIN"))

        node._emit_present_and_constants(sections, comps, "testns")
        present = "\n".join(sections["[Present]"])
        self.assertEqual(present.count("DRAG PRESENT BEGIN"), 1)
        self.assertLess(present.index("pre run = CommandListDragPinDetected_testns"), present.index("MODEL DRAG BINDING BEGIN"))

    def test_drag_present_uses_legacy_binding_line_without_marker(self):
        node, sections, comps = self._emit()
        sections["[Present]"] = [
            "    if $mouse_clicked == 1 && $model_drag_prev_lmb == 0 && $is_dragging == 0 && $help == 1",
            "        if $ssmtdrag_ui_detected_testns >= 0",
            "            $is_dragging = 9",
            "        endif",
            "    endif",
        ]
        node._emit_present_and_constants(sections, comps, "testns")
        present = "\n".join(sections["[Present]"])
        self.assertLess(present.index("DRAG PRESENT BEGIN"), present.index("if $mouse_clicked == 1"))

    def test_drag_present_appends_when_no_ui_binding_marker(self):
        node, sections, comps = self._emit()
        sections["[Present]"] = ["    $ui_layout = 1"]
        node._emit_present_and_constants(sections, comps, "testns")
        present = "\n".join(sections["[Present]"])
        self.assertLess(present.index("$ui_layout = 1"), present.index("DRAG PRESENT BEGIN"))

    def test_ui_tail_legacy_help_mode_override_is_removed_and_references_rewritten(self):
        node, _, _ = self._emit()
        tail = (
            "    if $help == 1\n"
            "        $ssmtdrag_mode_A = 1\n"
            "    else\n"
            "        $ssmtdrag_mode_A = $ssmtdrag_modifier_down_A\n"
            "    endif\n"
            "    ; --- MODEL DRAG BINDING BEGIN ---\n"
            "    if $ssmtdrag_ui_detected_A >= 0 && $ssmtdrag_ui_zone_A == 0\n"
            "        $is_dragging = 9\n"
            "    endif\n"
            "    ; --- MODEL DRAG BINDING END ---\n"
        )
        normalized = node._normalize_ui_drag_references(tail, "testns")
        self.assertIn("$ssmtdrag_ui_detected_testns", normalized)
        self.assertIn("$ssmtdrag_ui_zone_testns", normalized)
        self.assertNotIn("$ssmtdrag_mode_A", normalized)
        self.assertNotIn("$ssmtdrag_modifier_down_A", normalized)
        self.assertNotIn("_A", normalized)

    def test_alt_grab_key_requires_modifier_instead_of_help(self):
        node, sections, comps = self._emit()
        node._emit_present_and_constants(sections, comps, "testns")
        present = "\n".join(sections["[Present]"])
        self.assertIn("$ssmtdrag_mode_testns = $ssmtdrag_modifier_down_testns", present)
        self.assertNotIn("if $help == 1", present.split("DRAG PRESENT BEGIN", 1)[1])

    def test_none_grab_key_keeps_detection_always_on(self):
        node, sections, comps = self._emit(grab_key="NONE")
        node._emit_present_and_constants(sections, comps, "testns")
        present = "\n".join(sections["[Present]"])
        self.assertIn("$ssmtdrag_mode_testns = 1", present)
        self.assertIn("$ssmtdrag_modifier_ok_testns = 1", present)

    def test_ui_readback_command_list_is_emitted_and_post_run_precedes_drawn_reset(self):
        node, sections, comps = self._emit()
        node._emit_present_and_constants(sections, comps, "testns")
        readback = "\n".join(sections.get("[CommandListDragUIReadback_testns]", []))
        self.assertIn("DRAG UI BRIDGE BEGIN", readback)
        self.assertIn("store = $ssmtdrag_ui_detected_testns", readback)
        self.assertIn("store = $ssmtdrag_ui_zone_testns", readback)

        present = "\n".join(sections["[Present]"])
        self.assertLess(present.index("post run = CommandListDragUIReadback_testns"),
                        present.index("post $ssmtdrag_drawn_testns = 0"))

    def test_drag_present_is_relocated_into_ui_tail_before_binding_marker(self):
        node, sections, comps = self._emit()
        node._emit_present_and_constants(sections, comps, "testns")
        tail = (
            "    $ui_layout = 1\n"
            "    ; --- MODEL DRAG BINDING BEGIN ---\n"
            "    if $ssmtdrag_ui_detected_testns >= 0\n"
            "        $is_dragging = 9\n"
            "    endif\n"
            "    ; --- MODEL DRAG BINDING END ---\n"
        )
        normalized_tail = node._relocate_drag_present_into_ui_tail(sections, tail)
        self.assertIn("DRAG PRESENT BEGIN", normalized_tail)
        self.assertLess(normalized_tail.index("DRAG PRESENT BEGIN"), normalized_tail.index("MODEL DRAG BINDING BEGIN"))
        self.assertLess(normalized_tail.index("pre run = CommandListDragPinDetected_testns"), normalized_tail.index("MODEL DRAG BINDING BEGIN"))
        self.assertNotIn("DRAG PRESENT BEGIN", "\n".join(sections["[Present]"]))


if __name__ == "__main__":
    unittest.main()
