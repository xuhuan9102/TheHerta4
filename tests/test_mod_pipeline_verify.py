import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path


POWERSHELL = shutil.which("powershell.exe") or shutil.which("pwsh")
SCRIPT = Path(__file__).resolve().parents[1] / "tools" / "mod-pipeline.ps1"


@unittest.skipUnless(POWERSHELL, "PowerShell is unavailable")
class ModPipelineControlFlowTests(unittest.TestCase):
    def run_pipeline(self, command, ini_text):
        with tempfile.TemporaryDirectory() as directory:
            (Path(directory) / "仪玄.ini").write_text(ini_text, encoding="utf-8")
            return subprocess.run(
                [POWERSHELL, "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                 str(SCRIPT), command, "-ModDir", directory],
                capture_output=True,
                encoding="utf-8",
                errors="replace",
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                timeout=30,
            )

    def test_shader_regex_text_is_not_ini_control_flow(self):
        ini_text = r"""[ShaderRegexTear]
shader_model = ps_5_0

[ShaderRegexTear.Pattern]
endif\r?\n

[ShaderRegexTear.Pattern.Replace]
if_nz r0.x\n
if_z r0.y\n
else\n
endif\n
endif\n

[ShaderRegexTear.InsertDeclarations]
dcl_resource_texture2d (uint,uint,uint,uint) t116
"""
        for command in ("verify", "check"):
            with self.subTest(command=command):
                result = self.run_pipeline(command, ini_text)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_ini_control_flow_errors_keep_the_unicode_filename(self):
        result = self.run_pipeline("verify", """[CommandListBroken]
if $active

[ShaderRegexTear]
shader_model = ps_5_0
endif
""")

        self.assertEqual(result.returncode, 2, result.stdout + result.stderr)
        self.assertIn("FAIL [V5] 仪玄.ini:1 section [CommandListBroken]", result.stdout)
        self.assertIn("FAIL [V5] 仪玄.ini:4 section [ShaderRegexTear]", result.stdout)
