import importlib.util
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image


spec = importlib.util.spec_from_file_location(
    'panel_preview_under_test', Path(__file__).resolve().parents[1] / 'blueprint/panel_preview_runtime.py')
preview = importlib.util.module_from_spec(spec)
spec.loader.exec_module(preview)


class PreviewTests(unittest.TestCase):
    def node(self, mode):
        return SimpleNamespace(panel_mode=mode, buttons_per_row=1,
                               button_row_spacing=.02, button_column_spacing=.02,
                               button_top_padding=.03, _ensure_namespace=lambda: 'test')

    def values(self):
        return dict(base_width0=.22, base_height0=.12, zoom0=1.,
                    base_width1=.02, base_height1=.042, btn_width1=.05,
                    btn_height1=.045, fixed_rel_y1=.5,
                    slider_side_padding=.02, slider_label_gap=.01)

    def test_entirely_offscreen_textures_are_skipped(self):
        for xy in ((8, 8), (-8, -8), (-8, 0), (0, 8), (8, 0), (0, -8)):
            canvas = Image.new('RGBA', (4, 4))
            preview.paste_clipped(canvas, Image.new('RGBA', (4, 4), 'red'), *xy)
            self.assertIsNone(canvas.getbbox())
        canvas = Image.new('RGBA', (4, 4))
        preview.paste_clipped(canvas, Image.new('RGBA', (4, 4), 'red'), -2, -2)
        self.assertEqual(canvas.getbbox(), (0, 0, 2, 2))

    def test_slider_endpoints_respect_both_side_margins(self):
        for zoom in (.25, 1., 3.):
            for param in (0., 1.):
                values = self.values()
                values['zoom0'] = zoom
                draws = preview.collect_draws(self.node('INTEGRATED_TOP'), values, [param])
                for name, (x, y, w, h) in draws[1:]:
                    self.assertGreaterEqual(x, .02 * zoom - 1e-8, name)
                    self.assertLessEqual(x + w, (.22 - .02) * zoom + 1e-8, name)
                    self.assertGreaterEqual(y, 0, name)
                    self.assertLessEqual(y + h, .12 * zoom, name)

    def test_glow_slider_defaults_follow_exported_row_indices(self):
        text = (
            '; GlowFX brightness slider $first {"mode": "REMARK"}\n'
            '; GlowFX brightness slider $second\n'
            '    $param3 = $first\n'
            '    $param2 = $second\n'
        )
        params = [.4]
        preview._apply_glow_slider_defaults(text, {"first": .8, "second": .3}, params)
        self.assertEqual(params, [.4, .3, .8])

    def test_attached_buttons_extend_canvas_not_slider_background(self):
        values = self.values()
        for i in range(1, 4):
            values[f'test_btn_w{i}'] = .08
            values[f'test_btn_h{i}'] = .05
        draws = preview.collect_draws(self.node('ATTACHED_LEFT'), values, [0.])
        self.assertEqual(draws[0], ('ResourceImageToRender0', (0., 0., .22, .12)))
        buttons = [rect for name, rect in draws if name.startswith('ResourceSwapButton')]
        self.assertEqual(len(buttons), 3)
        self.assertTrue(all(rect[0] < 0 for rect in buttons))
        self.assertGreater(buttons[-1][1] + buttons[-1][3], .12)

    def test_independent_panels_have_independent_backgrounds(self):
        values = self.values()
        values.update(test_base_width0=.3, test_base_height0=.5, test_zoom0=1.,
                      test_btn_width1=.08, test_btn_height1=.05,
                      test_fixed_rel_x1=.1, test_fixed_rel_y1=.1)
        draws = preview.collect_draws(self.node('INDEPENDENT'), values, [0.])
        backgrounds = dict((name, rect) for name, rect in draws if name.startswith('ResourceImageToRender0'))
        self.assertEqual(backgrounds['ResourceImageToRender0_test'][2:], (.3, .5))
        self.assertEqual(backgrounds['ResourceImageToRender0'][2:], (.22, .12))
        self.assertGreater(backgrounds['ResourceImageToRender0'][0], .3)

    def test_selected_panel_owns_preview_without_mutating_shared_slider(self):
        class Slider:
            setting = 7
            last_mod_ini_path = 'real-slider.ini'

            def execute_postprocess(self, root, **kwargs):
                self.setting = self._find_combined_slider_panel().setting
                self.last_mod_ini_path = kwargs['_ini_path']
                with open(kwargs['_ini_path'], 'a') as stream:
                    stream.write(f'\nselected={self.setting}\n')
                return True

        class Panel:
            slider_use_slider_panel = True
            last_mod_ini_path = 'real-panel.ini'
            swap_panel_entries = []

            def __init__(self, slider, setting):
                self.slider, self.setting = slider, setting

            def _find_shape_ext_node(self):
                return self.slider

            def execute_postprocess(self, root, **kwargs):
                with open(kwargs['_ini_path'], 'a') as stream:
                    stream.write(f'\npanel={self.setting}\n')
                return True

        shared = Slider()
        first, second = Panel(shared, 11), Panel(shared, 22)
        for source in (second, first, second):
            with tempfile.TemporaryDirectory() as root:
                _proxy, ini, _res = preview.export_preview(source, root)
                text = Path(ini).read_text()
                self.assertIn(f'selected={source.setting}', text)
                self.assertIn(f'panel={source.setting}', text)
        self.assertEqual(shared.setting, 7)
        self.assertEqual(shared.last_mod_ini_path, 'real-slider.ini')
        self.assertEqual(first.last_mod_ini_path, 'real-panel.ini')
        self.assertEqual(second.last_mod_ini_path, 'real-panel.ini')

    def test_preview_restores_the_intervals_an_export_filled_in(self):
        class Intervals(list):
            def add(self):
                self.append(SimpleNamespace(start=0.0))
                return self[-1]

            def remove(self, index):
                del self[index]

        setting = SimpleNamespace(speed_intervals=Intervals())
        source = SimpleNamespace(play_group_settings=[setting])
        state = preview.collect_overlay_state(source)
        # The exporter defaults a missing interval on the existing setting and
        # creates a setting for a group that had none.
        setting.speed_intervals.add()
        source.play_group_settings.append(SimpleNamespace(speed_intervals=Intervals()))
        preview.restore_overlay_state(source, state)
        self.assertEqual(len(source.play_group_settings), 1)
        self.assertEqual(len(setting.speed_intervals), 0)

    def test_preview_images_stay_one_per_panel(self):
        node = SimpleNamespace(_find_shape_ext_node=lambda: None)
        proxy = SimpleNamespace(_ensure_namespace=lambda: 'ns/one')
        original = (preview.export_preview, preview.render_panel_runtime_image)
        preview.export_preview = lambda source, root: (
            proxy, os.path.join(root, 'preview.ini'), root)
        preview.render_panel_runtime_image = lambda *args: Image.new('RGBA', (2, 2))
        try:
            first = preview.generate_preview(node)
            second = preview.generate_preview(node)
        finally:
            preview.export_preview, preview.render_panel_runtime_image = original
        self.assertEqual(first, second)
        self.assertEqual(Path(first).name, 'ns_one.png')
        self.assertEqual(Path(first).parent, Path(tempfile.gettempdir()) / 'ssmt_panel_previews')
        self.assertTrue(Path(first).is_file())


if __name__ == '__main__':
    unittest.main()
