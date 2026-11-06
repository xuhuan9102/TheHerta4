"""Exercise the TTL/fluid handoff using the shipped command-list statements."""

import importlib.util
import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "fluid_order_merge", ROOT / "blueprint/fluid_ini_merge.py")
merge = importlib.util.module_from_spec(spec)
spec.loader.exec_module(merge)


class Commands:
    """Small interpreter for conditions, resource bindings and calls at this seam.

    Native TTL composition and the fluid shader are callbacks: their inputs and
    relative execution order are what the handoff must preserve.
    """

    def __init__(self, sections):
        self.sections = sections
        self.values = {}
        self.callbacks = {}
        self.events = []

    def condition(self, expression):
        expression = re.sub(
            r"\$\w+|@[A-Za-z0-9_]+",
            lambda m: repr(bool(self.values.get(m[0][1:], 0))
                           if m[0].startswith("@") else self.values.get(m[0], 0)),
            expression)
        expression = expression.replace("&&", " and ").replace("||", " or ")
        return eval(expression.replace("!", " not "), {"__builtins__": {}})

    def run(self, name):
        # RunCustomShaderCommand::run saves/restores OM outputs and viewport.
        saved = {key: value for key, value in self.values.items()
                 if re.fullmatch(r"o[0-7D]", key)}
        try:
            self.execute(name)
        finally:
            if name.startswith("CustomShader"):
                self.values.update(saved)

    def execute(self, name):
        if name in self.callbacks:
            self.callbacks[name]()
            return
        if name not in self.sections:
            return
        active = [True]
        for raw in self.sections[name]:
            line = raw.split(";", 1)[0].strip()
            if line.startswith("if "):
                active.append(active[-1] and self.condition(line[3:]))
            elif line == "endif":
                active.pop()
            elif active[-1] and " = " in line:
                key, value = line.split(" = ", 1)
                if key == "run":
                    self.run(value)
                elif key == "clear":
                    self.values[value] = 0
                else:
                    if value.startswith(("ref ", "copy ")):
                        value = self.values.get(value.split(" ", 1)[1], 0)
                    elif value.startswith("set_viewport "):
                        value = self.values.get(value.split()[-1], 0)
                    elif value.startswith("Resource"):
                        value = self.values.get(value, 0)
                    elif value == "null":
                        value = 0
                    elif re.fullmatch(r"-?\d+", value):
                        value = int(value)
                    self.values[key] = value
                    self.events.append(key)
        assert len(active) == 1, name


class TTLFluidOrderTests(unittest.TestCase):
    def bridge(self):
        path = ROOT / "Toolset/fluid/TTLibFluidOrder.ini"
        self.assertTrue(path.is_file(), "TTL needs a target-aware fluid handoff")
        return Commands(merge.parse_sections(path.read_text(encoding="utf-8")))

    def test_ttl_is_consumed_before_the_fluid_background_snapshot(self):
        sections = merge.parse_sections(
            (ROOT / "Toolset/fluid/Probe.ini").read_text(encoding="utf-8"))
        commands = [line.strip() for line in sections["CommandListDrawWorld"]]
        ttl = r"run = CommandList\TTL\ApplyBeforeFluid"
        self.assertIn(ttl, commands)
        self.assertLess(commands.index(ttl), commands.index(
            "ResourceFluidLighting = copy ResourceSceneColor"))
        self.assertLess(commands.index(ttl), commands.index(
            r"run = CommandList\LiquidGravityProbe\Composite\FluidComposite"))
        self.assertIn(r"Resource\TTL\FluidOrderTarget = ref ResourceSceneColor", commands)

    def test_handoff_consumes_once_and_restores_the_native_bindings(self):
        runner = self.bridge()
        bindings = {"oD": "depth", **{"o%d" % n: "rt%d" % n for n in range(8)},
                    **{"ps-t%d" % n: "srv%d" % n for n in (20, 21, 22, 23, 26, 29)}}
        runner.values.update(bindings)
        runner.values.update({"$enabled": 1, "$has_pending_layer": 1,
                              "$has_pending_layer_obj": 1,
                              "ResourceFluidOrderTarget": "hdr",
                              "ResourceWorldRTFlip": "native-flip"})
        seen = []

        def unbind():
            for key in bindings:
                if key.startswith("o"):
                    runner.values[key] = 0

        def composite():
            seen.append(runner.values["o0"])
            self.assertEqual(runner.values["ResourceWorldRTFlip"], 0)
            runner.values["$has_pending_layer"] = 0
            runner.values["$has_pending_layer_obj"] = 0
            for slot in (20, 21, 22, 23, 26, 29):
                runner.values["ps-t%d" % slot] = "ttl-input"

        runner.callbacks.update({"BuiltInCommandListUnbindAllRenderTargets": unbind,
                                 "CommandListApplyLateWorldComposite": composite})
        runner.run("CommandListApplyBeforeFluid")
        self.assertEqual(seen, ["hdr"])
        for key, value in bindings.items():
            self.assertEqual(runner.values[key], value, key)
        self.assertEqual(runner.values["ResourceWorldRTFlip"], "native-flip")
        self.assertEqual(runner.values["ResourceFluidOrderTarget"], 0)
        runner.values["ResourceFluidOrderTarget"] = "second-fluid-mod-hdr"
        runner.run("CommandListApplyBeforeFluid")
        self.assertEqual(seen, ["hdr"], "the next fluid mod must not replay TTL")

    def test_disabled_empty_or_missing_target_does_not_draw_or_retain_target(self):
        for enabled, character, objects, target in (
            (0, 1, 1, "hdr"), (1, 0, 0, "hdr"), (1, 1, 0, 0),
        ):
            with self.subTest(enabled=enabled, character=character, objects=objects,
                              target=target):
                runner = self.bridge()
                runner.values.update({"$enabled": enabled,
                                      "$has_pending_layer": character,
                                      "$has_pending_layer_obj": objects,
                                      "ResourceFluidOrderTarget": target})
                draws = []
                runner.callbacks["CommandListApplyLateWorldComposite"] = lambda: draws.append(1)
                runner.run("CommandListApplyBeforeFluid")
                self.assertEqual(draws, [])
                self.assertEqual(runner.values["ResourceFluidOrderTarget"], 0)


if __name__ == "__main__":
    unittest.main()
