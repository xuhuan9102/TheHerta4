import os
import types
import unittest

from common.texture_export_plan import TextureExportPlan


class TextureOverwriteTargetsTests(unittest.TestCase):
    def setUp(self):
        TextureExportPlan.reset()

    def tearDown(self):
        TextureExportPlan.reset()

    def test_only_active_overwrite_representatives_are_returned(self):
        folder = os.path.join("staging", "LOD0", "Textures")
        plan = [
            ("replace.dds", "overwrite", False, None),
            ("preserve.dds", "skip", False, None),
            ("dropped.dds", "overwrite", True, None),
            ("reuse.dds", "overwrite", False, 0),
        ]
        TextureExportPlan._entries = [
            types.SimpleNamespace(target_folder=folder, final_filename=name,
                                  overwrite_policy=policy, dropped=dropped, reuse_of=reuse_of)
            for name, policy, dropped, reuse_of in plan
        ]
        self.assertEqual(TextureExportPlan.overwrite_targets(), {os.path.join(folder, "replace.dds")})


if __name__ == "__main__":
    unittest.main()
