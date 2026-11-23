import os
import types
import unittest

from common.texture_export_plan import TextureExportPlan


class SkipExistingRealTargetTests(unittest.TestCase):
    def setUp(self):
        TextureExportPlan.reset()

    def tearDown(self):
        TextureExportPlan.reset()

    def _entry(self, target_folder, name, policy="skip"):
        return types.SimpleNamespace(
            materialized=False,
            overwrite_policy=policy,
            target_folder=target_folder,
            final_filename=name,
        )

    def test_skips_when_real_file_exists(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            staging = os.path.join(tmp, "staging", "LOD0")
            real = os.path.join(tmp, "real")
            os.makedirs(os.path.join(staging, "Textures"))
            os.makedirs(os.path.join(real, "Textures"))
            open(os.path.join(real, "Textures", "a.dds"), "wb").close()
            entry = self._entry(os.path.join(staging, "Textures"), "a.dds")
            TextureExportPlan._entries = [entry]
            skipped = TextureExportPlan.skip_entries_existing_in(staging, real)
            self.assertEqual(skipped, 1)
            self.assertTrue(entry.materialized)

    def test_keeps_missing_and_overwrite(self):
        import tempfile

        with tempfile.TemporaryDirectory() as tmp:
            staging = os.path.join(tmp, "staging", "LOD0")
            real = os.path.join(tmp, "real")
            os.makedirs(os.path.join(staging, "Textures"))
            os.makedirs(os.path.join(real, "Textures"))
            open(os.path.join(real, "Textures", "a.dds"), "wb").close()
            missing = self._entry(os.path.join(staging, "Textures"), "b.dds")
            overwrite = self._entry(os.path.join(staging, "Textures"), "a.dds", policy="overwrite")
            TextureExportPlan._entries = [missing, overwrite]
            self.assertEqual(TextureExportPlan.skip_entries_existing_in(staging, real), 0)
            self.assertFalse(missing.materialized)
            self.assertFalse(overwrite.materialized)


if __name__ == "__main__":
    unittest.main()
