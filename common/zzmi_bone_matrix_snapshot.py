"""Generate per-draw CB1 snapshots for the ZZMI bone matrix node."""

import re
from dataclasses import dataclass

DEFAULT_CB1_VARIABLE = "$bone_matrix_cb1_lock"
DEFAULT_CB1_KEY = "Ctrl+Alt+B"
SNAPSHOT_FRAME = "$ssmt_bm_cb1_capture_frame"
ORIGINAL_CB1 = "ResourceSSMTBoneMatrixOriginalCB1"
_PASS_SLOTS = 8
_TAG = "SSMT bone matrix"


@dataclass(frozen=True)
class CB1Snapshot:
    variable: str
    key: str


def hotkey_value(value):
    """Accept Blender-style chords and 3Dmigoto's space-delimited key syntax."""
    if "\r" in value or "\n" in value:
        raise ValueError("定身快捷键只接受单行输入")
    if not value.strip():
        return ""
    chord = " ".join(value.replace("+", " ").split())
    if not re.fullmatch(r"[a-zA-Z0-9_]+(?: [a-zA-Z0-9_]+)*", chord):
        raise ValueError("定身快捷键格式无效，请使用 Ctrl+Alt+B 或 VK_F7 等按键名称")
    return chord


class SnapshotSections:
    """Collect the commands and state for each exact draw and SO instance."""

    def __init__(self, control):
        self.control = control
        self.globals = [f"global {SNAPSHOT_FRAME} = -1"]
        self.present = [f"if {control.variable} == 0", f"    {SNAPSHOT_FRAME} = -1", "endif"]
        self.resources = [f"[{ORIGINAL_CB1}]", ""]

    def draw(self, site, instance, *, match_vertex_buffer=False):
        scope = f"{site}_{instance}"
        counter = f"$ssmt_bm_cb1_pass_{scope}"
        command = f"CommandListSSMTBoneMatrixCB1_{scope}"
        self.globals.append(f"global {counter} = 0")
        self.present.append(f"{counter} = 0")
        commands = [
            f"[{command}]",
            f"if {SNAPSHOT_FRAME} < 0",
            f"    {SNAPSHOT_FRAME} = frame_number",
            "endif",
            f"{counter} = {counter} + 1",
        ]
        for occurrence in range(1, _PASS_SLOTS + 1):
            suffix = f"{scope}_{occurrence}"
            frame = f"$ssmt_bm_cb1_frame_{suffix}"
            layout = (
                (f"$ssmt_bm_cb1_size_{suffix}", "vs-cb1->size"),
                (f"$ssmt_bm_cb1_offset_{suffix}", "vs-cb1->offset"),
                (f"$ssmt_bm_cb1_type_{suffix}", "DRAW_TYPE"),
                (f"$ssmt_bm_cb1_vs_{suffix}", "vs"),
                (f"$ssmt_bm_cb1_ps_{suffix}", "ps"),
            )
            if match_vertex_buffer:
                layout += ((f"$ssmt_bm_cb1_vb_{suffix}", "@vb0"),)
            buffer = f"ResourceSSMTBoneMatrixCB1_{suffix}"
            self.globals.extend((f"global {frame} = -1", *(f"global {name} = 0" for name, _ in layout)))
            self.resources.extend((f"[{buffer}]", "type = Buffer", "bind_flags = constant_buffer", ""))
            branch = "if" if occurrence == 1 else "elif"
            matches = "".join(f" && {name} === {value}" for name, value in layout)
            commands.extend((
                f"{branch} {counter} == {occurrence}",
                f"    if frame_number == {SNAPSHOT_FRAME}",
                f"        {buffer} = copy vs-cb1",
                f"        {frame} = frame_number",
                *(f"        {name} = {value}" for name, value in layout),
                "    endif",
                f"    if {frame} == {SNAPSHOT_FRAME} && @{buffer} != 0{matches}",
                f"        {ORIGINAL_CB1} = ref vs-cb1",
                f"        vs-cb1 = ref {buffer}",
                "    endif",
            ))
        self.resources.extend((*commands, "endif", ""))
        return (
            f"if {self.control.variable} != 0 && @vs-cb1 != 0",
            f"    run = {command}",
            "endif",
        )

    def definitions(self):
        lines = ["", f"; {_TAG} begin CB1 snapshot resources", *self.resources]
        if self.control.key:
            lines.extend((
                "[KeySSMTBoneMatrixCB1Snapshot]",
                f"key = {self.control.key}",
                "type = cycle",
                f"{self.control.variable} = 0,1",
                "",
            ))
        lines.append(f"; {_TAG} end")
        return lines
