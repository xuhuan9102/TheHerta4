import tempfile
import unittest
from pathlib import Path
from unittest import mock

import common.m_ini_builder as m_ini_builder
from common.m_ini_builder import M_IniBuilder, M_IniSection, M_SectionType


class MIniBuilderTests(unittest.TestCase):
    def test_named_sections_merge_and_repeated_save_is_utf8_lf_idempotent(self):
        builder = M_IniBuilder()
        first = M_IniSection(M_SectionType.Constants)
        first.SectionName = "Constants"
        first.append("global $first = 1 ; 中文")
        second = M_IniSection(M_SectionType.Constants)
        second.SectionName = "Constants"
        second.append("global $second = 2")
        builder.append_section(first)
        builder.append_section(second)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "mod.ini"
            builder.save_to_file(str(path))
            first_payload = path.read_bytes()
            builder.save_to_file(str(path))
            second_payload = path.read_bytes()

        self.assertEqual(first_payload, second_payload)
        self.assertEqual(first_payload.count(b"[Constants]"), 1)
        self.assertNotIn(b"\r\n", first_payload)
        self.assertIn("中文".encode("utf-8"), first_payload)
        self.assertEqual(
            sum(line == "[Constants]\n" for line in builder.line_list),
            1,
        )

    def test_publish_failure_preserves_previous_ini(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            destination = Path(temp_dir) / "mod.ini"
            destination.write_text(
                "old-generation\n", encoding="utf-8", newline="\n"
            )
            builder = M_IniBuilder()
            section = M_IniSection(M_SectionType.Constants)
            section.SectionName = "Constants"
            section.append("global $new_generation = 1")
            builder.append_section(section)

            with mock.patch.object(
                m_ini_builder.os,
                "replace",
                side_effect=OSError("publish failed"),
            ):
                with self.assertRaisesRegex(OSError, "publish failed"):
                    builder.save_to_file(str(destination))

            self.assertEqual(
                destination.read_text(encoding="utf-8"), "old-generation\n"
            )
            self.assertEqual(list(destination.parent.glob(".mod.ini.*.tmp")), [])

    def test_compact_mode_keeps_sections_and_authored_comments(self):
        builder = M_IniBuilder(compact=True)
        section = M_IniSection(M_SectionType.ResourceBuffer)
        section.extend(["[ResourceSaved]", "", "", "[ResourceNext]", "; authored boundary", "stride = 40"])
        builder.append_section(section)

        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "mod.ini"
            builder.save_to_file(str(path))
            payload = path.read_text(encoding="utf-8")

        self.assertNotIn(";MARK:ResourceBuffer", payload)
        self.assertIn("[ResourceSaved]\n\n[ResourceNext]\n; authored boundary\nstride = 40", payload)


if __name__ == "__main__":
    unittest.main()
