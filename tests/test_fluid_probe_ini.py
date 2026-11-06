# -*- coding: utf-8 -*-
"""流体探针 INI 加工的纯逻辑测试。

``blueprint/fluid_ini_merge.py`` 不 import bpy，因此这里直接用
``importlib.util`` 按文件路径加载，不需要 stub 整个 bpy 链。

覆盖（每条正向配一个反例或回归点）：

* 段切分与标记块删除；
* 「动画驱动段 / 宿主正文 / 其它模块的 AUTO-APPENDED 尾巴」三路切分；
* 常量块插到宿主第一个 ``[Constants]`` 之后，Present 块插到 ``[Present]`` 段尾；
* **反复注入幂等**——两处曾经的真实缺陷写成了回归：
  1. 把模板字符串当 list 传给 ``merge_*`` 会被逐字符拆行（``list("abc")``）；
  2. 段间不做行尾规范化时，每次重导都会累积空行。
* 模板文件本身的契约：段数、变量前缀、号段迁移、着色器路径。
"""

import importlib.util
import unittest
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MERGE_PATH = _REPO_ROOT / "blueprint" / "fluid_ini_merge.py"
#: 模板是参考实现原始 ``Probe.ini``（自带 ``namespace = LiquidGravityProbe``），
#: 命名空间/号段/门控都在注入期改，所以这份断言只能针对「原文」。
_TEMPLATE_PATH = _REPO_ROOT / "Toolset" / "fluid" / "Probe.ini"


def _load_merge_module():
    spec = importlib.util.spec_from_file_location("fluid_ini_merge_under_test", _MERGE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


merge = _load_merge_module()


class ParseSectionsTests(unittest.TestCase):
    def test_splits_sections_in_order(self):
        text = "[A]\nx = 1\n[B]\ny = 2\nz = 3\n"
        sections = merge.parse_sections(text)
        self.assertEqual(list(sections), ["A", "B"])
        self.assertEqual(sections["B"], ["y = 2", "z = 3"])

    def test_lines_before_first_section_are_dropped(self):
        # 模板首行是 namespace，注入时不要它
        sections = merge.parse_sections("namespace = X\n[A]\nk = v\n")
        self.assertEqual(list(sections), ["A"])


class StripMarkedTextTests(unittest.TestCase):
    def test_removes_block_and_both_markers(self):
        text = "keep1\nS\nin\nE\nkeep2\n"
        self.assertEqual(merge.strip_marked_text(text, "S", "E"), "keep1\nkeep2")

    def test_no_marker_is_a_no_op(self):
        text = "a\nb\n"
        self.assertEqual(merge.strip_marked_text(text, "S", "E"), "a\nb")

    def test_empty_text_is_safe(self):
        self.assertEqual(merge.strip_marked_text("", "S", "E"), "")


class SplitHostTests(unittest.TestCase):
    def test_driver_block_is_cut_out(self):
        text = (merge.DRIVER_START + "\ndriver line\n" + merge.DRIVER_END
                + "\n\n[Present]\nrun = X\n")
        driver, rest = merge.split_driver(text)
        self.assertIn("driver line", driver)
        self.assertNotIn("driver line", rest)
        self.assertIn("[Present]", rest)

    def test_tail_starts_at_first_auto_marker(self):
        text = "[Present]\nk = v\n" + merge.AUTO_APPENDED_MARKERS[0] + "\n[Panel]\nx = 1\n"
        body, tail = merge.split_tail(text)
        self.assertIn("[Present]", body)
        self.assertNotIn("[Panel]", body)
        self.assertIn("[Panel]", tail)

    def test_no_auto_marker_means_empty_tail(self):
        body, tail = merge.split_tail("[Present]\nk = v\n")
        self.assertEqual(tail, "")
        self.assertIn("[Present]", body)

    def test_read_host_strips_previous_fluid_block_from_body(self):
        # 宿主没有 AUTO 尾巴时，上一轮的流体段块落在 body 里，必须被剥掉
        text = (
            "[Present]\nk = v\n\n"
            + merge.TAIL_START + "\n\n[CommandListX_FluidProbe]\nrun = X\n\n"
            + merge.TAIL_END + "\n"
        )
        body, tail, driver = merge.read_host(text)
        self.assertNotIn(merge.TAIL_START, body)
        self.assertNotIn("_FluidProbe", body)
        self.assertEqual(tail, "")
        self.assertEqual(driver, "")


class MergeTests(unittest.TestCase):
    def test_constants_go_right_after_first_constants_header(self):
        body = "[Constants]\nexisting = 0\n\n[Present]\nk = v\n"
        merged = merge.merge_constants(body, "global $x = 1")
        self.assertIn("; --- x", merged.replace("FluidProbe:Constants:Start", "; --- x"))
        lines = merged.splitlines()
        idx = lines.index("[Constants]")
        self.assertEqual(lines[idx + 1], merge.CONST_START)
        self.assertEqual(lines[idx + 2], "global $x = 1")
        self.assertEqual(lines[idx + 3], merge.CONST_END)

    def test_merge_constants_does_not_explode_string_into_characters(self):
        # 回归：曾用 list(str) 把 "global $x = 1" 拆成单字符行
        merged = merge.merge_constants("[Constants]\n", "global $x = 1")
        self.assertIn("global $x = 1", merged.splitlines())

    def test_present_block_goes_to_end_of_present_section(self):
        body = "[Present]\npre run = A\n\n[CommandListA]\nrun = B\n"
        merged = merge.merge_present(body, "run = CommandListFluid_FluidProbe")
        lines = merged.splitlines()
        present_at = lines.index("[Present]")
        start = lines.index(merge.PRESENT_START)
        end = lines.index(merge.PRESENT_END)
        self.assertLess(present_at, start)
        self.assertLess(start, end)
        self.assertLess(end, lines.index("[CommandListA]"))

    def test_merge_present_creates_section_when_missing(self):
        merged = merge.merge_present("[TextureOverrideX]\nhash = 1\n", "run = Y")
        self.assertIn("[Present]", merged.splitlines())
        self.assertIn(merge.PRESENT_START, merged)


class ComposeHostTests(unittest.TestCase):
    def _host(self):
        return (
            merge.DRIVER_START + "\ndriver\n" + merge.DRIVER_END + "\n\n"
            "[Constants]\nglobal $existing = 0\n\n"
            "[Present]\nrun = CommandListExisting\n\n"
            "[TextureOverrideMain]\nhash = 1\n"
        )

    def _sections(self):
        return {
            "Constants": "global $fluid_enabled = 1",
            "Present": "run = CommandListFluidPresent_FluidProbe",
            "KeyToggle_FluidProbe": "key = VK_NUMPAD1\nrun = CommandListToggleEffect_FluidProbe",
        }

    def test_driver_body_tail_and_fluid_block_all_present(self):
        out = merge.compose_host(self._host(), "", "", self._sections())
        self.assertIn("driver", out)
        self.assertIn("$existing", out)
        self.assertIn("global $fluid_enabled = 1", out)
        self.assertIn("[KeyToggle_FluidProbe]", out)
        self.assertIn(merge.TAIL_START, out)

    def test_second_pass_is_byte_identical(self):
        first = merge.compose_host(self._host(), "", "", self._sections())
        body, tail, driver = merge.read_host(first)
        second = merge.compose_host(body, tail, driver, self._sections())
        self.assertEqual(first, second)

    def test_sections_may_be_lists(self):
        # 节点的 _build_template_sections 返回的是行列表而非整段文本
        sections = {
            "Constants": ["global $x = 1", "global $y = 0"],
            "Present": ["run = CommandListX"],
            "KeyToggle_FluidProbe": ["key = VK_NUMPAD1"],
        }
        out = merge.compose_host(self._host(), "", "", sections)
        lines = out.splitlines()
        self.assertIn("global $x = 1", lines)
        self.assertIn("global $y = 0", lines)
        self.assertIn("key = VK_NUMPAD1", lines)
        self.assertNotIn("['global $x = 1'", out)

    def test_third_pass_does_not_accumulate_blank_lines(self):
        # 回归：段间不规范化行尾时每次重导都会多一个空行
        text = self._host()
        for _ in range(4):
            body, tail, driver = merge.read_host(text)
            text = merge.compose_host(body, tail, driver, self._sections())
        self.assertNotIn("---\n\n\n", text)
        self.assertEqual(text.count(merge.TAIL_START), 1)
        self.assertEqual(text.count(merge.CONST_START), 1)
        self.assertEqual(text.count(merge.PRESENT_START), 1)


class EngineGateTests(unittest.TestCase):
    def test_efmi_gate_is_replaced(self):
        text = "if $seen && $\\EFMIv1\\enable_mods\n    run = X\nendif"
        out = merge.apply_engine_gate(text)
        self.assertNotIn("EFMIv1", out)
        self.assertIn(merge.ENGINE_GATE_VAR, out)

    def test_template_carries_the_undefined_gate_variable(self):
        """新版参考实现的 7 处门控全挂在 ``$costume_mods`` 上，而它从未被赋值。

        3Dmigoto 对未定义变量求值为 0 ⇒ 整条链跳过。节点在 ``_rewrite`` 里把它换成
        ``$fluid_present``，这条测试保证「换得到东西」——模板若改名，替换会
        静默失效，导出后表现为「装了不生效、也不报错」。

        2026-11-03 从 5 处变 7 处：[Present] 的总开关改成角色门控硬关
        （``if $costume_mods`` 与 ``if !$costume_mods`` 各一处），
        ``[CommandListInjectWorld]`` 又把「写 IniParams + dispatch」包进了门里。
        """
        raw = _TEMPLATE_PATH.read_text(encoding="utf-8")
        self.assertEqual(raw.count("$costume_mods"), 7)
        self.assertNotIn(merge.EFMI_GATE_VAR, raw)

    def test_other_variables_untouched(self):
        text = "if $enabled && !$drawn"
        self.assertEqual(merge.apply_engine_gate(text), text)


class TemplateContractTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.raw = _TEMPLATE_PATH.read_text(encoding="utf-8")
        cls.sections = merge.parse_sections(cls.raw)

    def test_template_exists_and_is_non_trivial(self):
        self.assertTrue(_TEMPLATE_PATH.is_file())
        self.assertGreater(len(self.sections), 50)

    def test_no_native_diagnostic_dead_code_is_shipped(self):
        # 原版 784-5071 行是 61 个从没被 run 的 native 诊断命令列表
        self.assertNotIn("CommandListNativeOriginal", self.raw)

    def test_shader_paths_are_relative_for_the_deploy_prefix(self):
        self.assertIn("cs = ", self.raw)
        self.assertNotIn("res/fluid_probe/", self.raw)

    def test_ini_params_use_original_numbering(self):
        # 模板保留原号段，迁移在注入期做
        self.assertRegex(self.raw, r"[xyzw]237")

    def test_template_has_all_core_shader_bindings(self):
        # FluidSolver.hlsl / Slots.hlsl / ParticleConfig.hlsl 都是被 #include 的头，
        # 不出现在 ini 里；ini 只点这些入口着色器。
        for needle in (
            "FluidPredict.hlsl",
            "FluidSurface.hlsl",
            "FluidComposite.hlsl",
            "CaptureTriangle.hlsl",
        ):
            self.assertIn(needle, self.raw)

    def test_template_carries_its_own_namespace(self):
        """参考实现原文带 ``namespace = LiquidGravityProbe``，注入期必须改掉。

        漏改的后果（``IniHandler.cpp:424-428``）：同名的 ``[Constants]`` 会整段
        被丢弃，我们写进去的门控与号段全部失效，且不报错。
        """
        self.assertRegex(self.raw, r"(?m)^namespace\s*=\s*LiquidGravityProbe\s*$")


if __name__ == "__main__":
    unittest.main()


class MeshDrawScanTests(unittest.TestCase):
    """``; [mesh:名]`` → draw 归属 → (物体, 面序号) 反算首索引。

    回归来源：预览曾用 ``first_index // 3`` 当面号，而 ``first_index`` 是
    **整个 IB 的绝对索引**、``polygon.index`` 是**物体内**序号，两者差一个
    ``start`` 偏移。实测反例见 ``test_real_starbilly_sample``。
    """

    def test_scan_attributes_draws_to_mesh_comments(self):
        ini = (
            "[TextureOverride_A]\n"
            "\t; [mesh:head]\n"
            "\tdrawindexed = 300, 1200, 0\n"
            "\tdrawindexed = 90, 1500, 0\n"
            "\t; [mesh:bodySSMT]\n"
            "\tdrawindexed = 600, 1590, 0\n"
        )
        draws = merge.scan_mesh_draws(ini)
        self.assertEqual(
            draws,
            [
                ("head", 1200, 300, 0),
                ("head", 1500, 90, 0),
                ("bodySSMT", 1590, 600, 0),
            ],
        )

    def test_scan_keeps_draws_without_comment_as_none(self):
        ini = "[S]\n\tdrawindexed = 30, 0, 0\n"
        self.assertEqual(merge.scan_mesh_draws(ini), [(None, 0, 30, 0)])

    def test_poly_index_recomputes_first_index(self):
        draws = [("head", 1200, 300, 0)]
        fi = merge.resolve_first_index(7, "head", 0, "", draws)
        self.assertEqual(fi, 1200 + 7 * 3)

    def test_missing_comment_yields_none_not_guess(self):
        # 面序号填了但 ini 没有 mesh 注释：宁可返回 None（跳过注入），
        # 也不能退回 first_index —— 那是另一个语义的号。
        draws = [(None, 0, 300, 0)]
        self.assertIsNone(merge.resolve_first_index(7, "head", 0, "", draws))

    def test_poly_index_out_of_range_yields_none(self):
        draws = [("head", 1200, 30, 0)]  # 只有 10 个三角形
        self.assertIsNone(merge.resolve_first_index(10, "head", 0, "", draws))

    def test_poly_index_minus_one_falls_back_to_first_index(self):
        draws = [("head", 1200, 300, 0)]
        self.assertEqual(merge.resolve_first_index(-1, "head", 999, "", draws), 999)

    def test_suffix_match_handles_exported_prefix(self):
        draws = [("SSMT_head", 1200, 300, 0)]
        self.assertEqual(merge.resolve_first_index(2, "head", 0, "", draws), 1206)

    def test_real_starbilly_sample(self):
        """参考实现实测：``y243 = 883239`` 落在 ``start=723363, count=169464``。

        换算成物体内面号 ``(883239-723363)//3 = 53292``；若按 ``883239//3``
        去索引只会取到越界或无关的三角形。
        """
        ini = "[TextureOverride_X]\n\tdrawindexed = 169464, 723363, 0\n"
        draws = merge.scan_mesh_draws(ini)
        self.assertEqual(draws, [(None, 723363, 169464, 0)])
        # 没有 mesh 注释 ⇒ 无法按物体反算；但用 first_index 原样通过是对的。
        self.assertEqual(merge.resolve_first_index(-1, "", 883239, "", draws), 883239)
        # 有注释时，面号 53292 应还原出同一个首索引。
        with_ns = "; [mesh:body]\n\tdrawindexed = 169464, 723363, 0\n"
        draws2 = merge.scan_mesh_draws(with_ns)
        self.assertEqual(merge.resolve_first_index(53292, "body", 0, "", draws2), 883239)


class EmitterDirectionTests(unittest.TestCase):
    """``fluid_ini_merge.emitter_direction`` 的几何约束。

    ``surface/ResolveSurface.hlsl:61-64`` 的消费方式：
        forward = normalize(tangent*f.x + bitangent*f.y + n*f.z)
        upHint  = normalize(tangent*u.x + bitangent*u.y + n*u.z)
        right   = normalize(cross(upHint, forward))
        up      = normalize(cross(forward, right))
    所以 ``f`` / ``u`` 是三角形 TBN 下的系数，且 ``u`` 必须在写进去之前就与
    ``f`` 正交——否则 normalize 会把 upHint 拉回 forward 方向，right 退化。
    """

    @staticmethod
    def _dot(a, b):
        return sum(x * y for x, y in zip(a, b))

    def test_forward_is_unit(self):
        for tilt in (0.0, 30.0, 50.0, 90.0, -45.0):
            f, _ = merge.emitter_direction(tilt, 25.0, 0.0)
            self.assertAlmostEqual(self._dot(f, f), 1.0, places=9)

    def test_up_is_unit_and_orthogonal_to_forward(self):
        for tilt in (0.0, 50.0, 90.0):
            for roll in (-90.0, 0.0, 45.0, 180.0):
                f, u = merge.emitter_direction(tilt, 25.0, roll)
                self.assertAlmostEqual(self._dot(u, u), 1.0, places=9)
                self.assertAlmostEqual(self._dot(f, u), 0.0, places=9)

    def test_roll_zero_reproduces_bitangent_up(self):
        """``tilt=0, yaw=0, roll=0`` -> forward=(0,0,1), up=(0,1,0)。

        这是加 roll 之前的行为；新增自由度不能悄悄改掉默认值。
        """
        f, u = merge.emitter_direction(0.0, 0.0, 0.0)
        self.assertEqual([round(v, 9) for v in f], [0.0, 0.0, 1.0])
        self.assertEqual([round(v, 9) for v in u], [0.0, 1.0, 0.0])

    def test_roll_turns_up_around_forward(self):
        _, a = merge.emitter_direction(0.0, 0.0, 0.0)
        _, b = merge.emitter_direction(0.0, 0.0, 90.0)
        self.assertLess(abs(self._dot(a, b)), 1e-6)
        # forward=+z，右手绕 +z 转 90 度 -> up 从 +y 转到 -x
        self.assertAlmostEqual(b[0], -1.0, places=6)

    def test_degenerate_forward_does_not_divide_by_zero(self):
        """``tilt=90, yaw=90`` 时 forward=(0,1,0)，与默认基准 (0,1,0) 平行。"""
        f, u = merge.emitter_direction(90.0, 90.0, 0.0)
        self.assertAlmostEqual(self._dot(f, f), 1.0, places=9)
        self.assertAlmostEqual(self._dot(u, u), 1.0, places=9)
        self.assertAlmostEqual(self._dot(f, u), 0.0, places=9)

    def test_waterpick_sample_forward_is_reachable(self):
        """参考实现 ``Profiles/62c0dcc05f11.json`` 首条 ``v[5..7] = (-0.68273,-0.67144,0.28818)``。

        反解：``tilt = degrees(acos(0.28818)) = 73.250973``、
        ``yaw = degrees(atan2(0.68273, -0.67144)) = 134.522324``（铰链式：``yaw`` 由
        ``f=(-sin t sin y, sin t cos y, cos t)`` 解出，所以 y 分量给 cos、x 分量给 -sin）。
        这证明参考实现的 tilt 与我们的铰链角不是同一个量——它能被复现，但不能直接照抄数值。
        """
        import math
        f, _ = merge.emitter_direction(73.250973, 134.522324, 0.0)
        for got, want in zip(f, (-0.68273, -0.67144, 0.28818)):
            self.assertAlmostEqual(got, want, places=3)

class ReferenceEmitterRoundTripTests(unittest.TestCase):
    """参考实现 7 条发射器的 (tilt, yaw, roll) ↔ (v[5..7], v[9..11]) 往返。

    数据来源：`C:/Users/anlingQWQ/Desktop/LiquidGravityProbe/Profiles/62c0dcc05f11.json`
    的 ``gpu_data``（7 条 × 23 浮点）。下表是从该文件反解出来的三个角，以及
    原文件的 forward/up。硬编码在这里而不是运行时读那个路径——测试不能依赖
    桌面上的参考实现存在。

    这组测试锁的是一件具体的事：**本节点的三个角足以重建参考实现的任意喷流方向**。
    7 条实测里 ``roll`` 从 -168 到 +155 都有，说明那一维确实被用到了；
    如果哪天有人把 ``roll`` 去掉或写死，这里会立刻红。
    """

    # (forward, up) —— 只存参考实现的原文件数值，角度一律现场反解。
    # 存打死的 (tilt, yaw, roll) 等于把某一种参数化写进测试里：铰链式与极角式
    # 互换时那三列就全错，而真正要锁的「任意喷流方向都可达」其实没被锁住。
    CASES = [
        ((-0.68273, -0.67144, 0.28818), (-0.70371, 0.49809, -0.50665)),
        ((0.7867, 0.42129, -0.45124), (0.25444, -0.88725, -0.38476)),
        ((-0.83334, 0.45228, 0.31778), (-0.32281, 0.06847, -0.94398)),
        ((0.00071, 0.99062, -0.13665), (0.4193, -0.12435, -0.89929)),
        ((0.90993, 0.22052, 0.35128), (0.41187, -0.38068, -0.82792)),
        ((-0.60285, 0.73241, -0.31645), (-0.78184, -0.62137, 0.05132)),
        ((-0.1415, -0.75404, -0.64141), (-0.19643, 0.65643, -0.72837)),
    ]

    @staticmethod
    def _angles_from_forward_up(f, u):
        """把参考实现的 ``(forward, up)`` 反解成本节点的 ``(tilt, yaw, roll)``。

        ``emitter_direction`` 的形式是 ``f = (-sin t sin y, sin t cos y, cos t)``，
        所以 ``t = acos(f.z)``、``y = atan2(-f.x, f.y)``；``up`` 是在与 ``f``
        正交的基底上按 ``roll`` 旋转出来的，把 ``u`` 投回去即可解 ``roll``。
        """
        import math

        t = math.degrees(math.acos(max(-1.0, min(1.0, f[2]))))
        y = math.degrees(math.atan2(-f[0], f[1]))
        # 用同一套基底构造（与 emitter_direction 内部一致的候选顺序）
        for cand in ((0.0, 1.0, 0.0), (1.0, 0.0, 0.0), (0.0, 0.0, 1.0)):
            d = sum(x * c for x, c in zip(f, cand))
            a = tuple(c - d * x for x, c in zip(f, cand))
            ln = math.sqrt(sum(v * v for v in a))
            if ln > 1e-6:
                a = tuple(v / ln for v in a)
                break
        b = (f[1] * a[2] - f[2] * a[1],
             f[2] * a[0] - f[0] * a[2],
             f[0] * a[1] - f[1] * a[0])
        r = math.degrees(math.atan2(sum(x * c for x, c in zip(u, b)),
                                    sum(x * c for x, c in zip(u, a))))
        return t, y, r

    def test_all_seven_reconstruct_within_tolerance(self):
        for i, (f_want, u_want) in enumerate(self.CASES):
            t, y, r = self._angles_from_forward_up(f_want, u_want)
            f, u = merge.emitter_direction(t, y, r)
            for k in range(3):
                self.assertAlmostEqual(f[k], f_want[k], places=4,
                                       msg="case %d forward[%d]" % (i, k))
                self.assertAlmostEqual(u[k], u_want[k], places=4,
                                       msg="case %d up[%d]" % (i, k))

    def test_forward_up_orthogonal_in_every_case(self):
        for i, (f_want, u_want) in enumerate(self.CASES):
            t, y, r = self._angles_from_forward_up(f_want, u_want)
            f, u = merge.emitter_direction(t, y, r)
            dot = sum(a * b for a, b in zip(f, u))
            self.assertLess(abs(dot), 1e-6, msg="case %d dot=%g" % (i, dot))

class TintClampTests(unittest.TestCase):
    """tint 封顶 0.98 —— 参考实现的行为，不是随手选的数。

    数据来源：`C:/Users/anlingQWQ/Desktop/LiquidGravityProbe/Profiles/62c0dcc05f11.json`
    的 gpu_data。7 条里 5 条是 `#e6bf08`，落盘 (0.901961, 0.749020, 0.031373)
    ——精确的 hex/255；2 条是 `#ffffff`，落盘 (0.98, 0.98, 0.98)。
    ⇒ 是 `min(c, 0.98)` 而不是统一缩放。
    """

    def test_pure_white_is_capped(self):
        self.assertEqual(merge.clamp_tint((1.0, 1.0, 1.0)),
                         (merge.TINT_MAX,) * 3)

    def test_reference_amber_passes_through(self):
        amber = (230 / 255.0, 191 / 255.0, 8 / 255.0)
        out = merge.clamp_tint(amber)
        for k in range(3):
            self.assertAlmostEqual(out[k], amber[k], places=9)

    def test_negative_and_nan_are_sanitised(self):
        out = merge.clamp_tint((-0.5, float("nan"), 2.0))
        self.assertEqual(out, (0.0, 0.0, merge.TINT_MAX))


class EmitterModeCodeTests(unittest.TestCase):
    """``v[8]`` 的三态编码。

    判据镜像自消费端：``Slots.hlsl:33-36``（slotVisOn / slotTrickle /
    slotTrickleScale）与 ``AssignSlots.hlsl:83``（x = v8-1、w = vis 标志）。
    """

    def test_normal_mode_is_zero(self):
        self.assertEqual(merge.emitter_mode_code(False, False, 1.0), 0.0)

    def test_vis_wins_over_trick(self):
        self.assertEqual(merge.emitter_mode_code(True, True, 1.0), -1.0)

    def test_trick_code_is_one_plus_speed(self):
        self.assertAlmostEqual(merge.emitter_mode_code(True, False, 0.1), 1.1)

    def test_trick_speed_below_floor_survives(self):
        # 0.01 会算出 v8=1.01，slotTrickle 判据是 x > .05 -> 失败。
        # 必须被夹到 0.1，否则游戏里「勾了 trick 但没反应」。
        self.assertAlmostEqual(merge.emitter_mode_code(True, False, 0.01), 1.1)

    def test_trick_speed_above_ceiling_is_clamped(self):
        self.assertAlmostEqual(merge.emitter_mode_code(True, False, 99.0), 9.0)

    def test_encode_then_decode_roundtrip(self):
        code = merge.emitter_mode_code(True, False, 0.1)
        self.assertGreater(code, 1.05)                 # slotTrickle 判据
        self.assertAlmostEqual(code - 1.0, 0.1)        # slotTrickleScale

    def test_nan_speed_falls_back_to_floor(self):
        self.assertAlmostEqual(merge.emitter_mode_code(True, False, float("nan")), 1.1)


class EmitterPackCodeTests(unittest.TestCase):
    """``v[12]`` = a*10000 + b*100 + c，仅 trick 模式非零。"""

    def test_non_trick_is_zero_even_with_nonzero_inputs(self):
        # 参考实现 #0..#3、#6：tlen=0.35 bendDelay=0.15 bendStr=15，落盘 v[12] 是 0
        self.assertEqual(merge.emitter_pack_code(False, False, 0.35, 0.15, 15), 0.0)

    def test_vis_emitter_is_zero(self):
        self.assertEqual(merge.emitter_pack_code(True, True, 0.15, 0.0, 60), 0.0)

    def test_reference_value_30060(self):
        # waterpick 的 #4 / #5：tlen=0.15 bendDelay=0 bendStr=60
        self.assertEqual(merge.emitter_pack_code(True, False, 0.15, 0.0, 60), 30060.0)

    def test_mid_and_low_digits(self):
        # tlen=0.10 -> 2；bendDelay=0.25 -> 5；bendStr=7
        self.assertEqual(merge.emitter_pack_code(True, False, 0.10, 0.25, 7), 20507.0)

    def test_out_of_range_is_clamped_not_wrapped(self):
        # 超 99 必须夹住，不能进位污染高位
        self.assertEqual(merge.emitter_pack_code(True, False, 10.0, 10.0, 500), 999999.0)


class LegacyBufferTests(unittest.TestCase):
    """旧版 profiles.buf / attachment-offsets.buf 的记录格式。

    数据来源：旧版 ``-zh-CN`` 目录里 ``profiles.buf`` 1056 B = 22 条 × 48 B，
    ``attachment-offsets.buf`` 352 B = 22 条 × 16 B。
    """

    def setUp(self):
        self.merge = _load_merge_module()

    def _row(self):
        # 取一条形状与参考实现一致的记录（23 浮点，bary 和为 1）
        return [
            0, -1, 0.299159596, 0.529732889, 0.171107515,
            -0.251460691, 0.965278997, -0.070738809,
            0, 0.036251189, -0.063642806, -0.997314115,
            0, 0, 0, 0,
            0, 1, 0.98, 0.909803922, 0.525490196,
            0.000216894, 1,
        ]

    def test_profile_record_layout(self):
        rec = self.merge.legacy_profile_record(self._row())
        self.assertEqual(len(rec), 12)
        self.assertAlmostEqual(rec[0], -1.0)          # priority <- v[1]
        self.assertAlmostEqual(rec[1], 0.299159596)   # bary <- v[2]
        self.assertAlmostEqual(rec[4], -0.251460691)  # forward <- v[5]
        self.assertEqual(rec[7], 0.0)                 # 占位
        self.assertAlmostEqual(rec[8], 0.036251189)   # up <- v[9]
        self.assertEqual(rec[11], 0.0)                # 占位

    def test_offset_record_layout(self):
        row = self._row()
        row[13], row[14], row[15] = 0.01, -0.02, 0.03
        rec = self.merge.legacy_offset_record(row)
        self.assertEqual(len(rec), 4)
        self.assertAlmostEqual(rec[0], 0.01)
        self.assertAlmostEqual(rec[1], -0.02)
        self.assertAlmostEqual(rec[2], 0.03)
        self.assertEqual(rec[3], 0.0)

    def test_buffer_sizes(self):
        rows = [self._row() for _ in range(22)]
        prof, offs = self.merge.legacy_buffers(rows)
        # 旧版实测 22 条时的字节数
        self.assertEqual(len(prof), 1056)
        self.assertEqual(len(offs), 352)

    def test_short_record_rejected(self):
        with self.assertRaises(ValueError):
            self.merge.legacy_profile_record([0, -1, 0.3])

    def test_empty_rows(self):
        prof, offs = self.merge.legacy_buffers([])
        self.assertEqual(prof, b"")
        self.assertEqual(offs, b"")



class ExportSuffixNameMatchTests(unittest.TestCase):
    """导出后缀不能让物体名匹配失效。

    实测事故：`菲欧妮.ini` 里 mesh 注释写的是
    `; [mesh:LOD0.9040e2c4-16194-56844_copy]`（导出期加了 `_copy`），
    而 Blender 里物体叫 `LOD0.9040e2c4-16194-56844`。原来的实现只做
    `a.endswith(b) or b.endswith(a)`，`_copy` 挂在末尾把后缀匹配整个打断，
    于是 `resolve_first_index` 返回 None、注入块一个都写不出去——
    导出"成功"、进游戏毫无反应，静态检查也查不出来。
    """

    def test_copy_suffix_is_stripped(self):
        self.assertTrue(merge.mesh_names_match(
            "LOD0.9040e2c4-16194-56844_copy", "LOD0.9040e2c4-16194-56844"))
        self.assertTrue(merge.mesh_names_match(
            "LOD0.9040e2c4-16194-56844", "LOD0.9040e2c4-16194-56844_copy"))

    def test_blender_number_suffix_is_stripped(self):
        self.assertTrue(merge.mesh_names_match(
            "Body_copy.001", "Body"))

    def test_different_meshes_still_do_not_match(self):
        self.assertFalse(merge.mesh_names_match(
            "LOD0.b11ff137-6078-0_copy", "LOD0.9040e2c4-16194-56844"))

    def test_empty_inputs_never_match(self):
        self.assertFalse(merge.mesh_names_match("", "x"))
        self.assertFalse(merge.mesh_names_match("x", ""))
        self.assertFalse(merge.mesh_names_match("_copy", "Body"))


class LocateDrawLineTests(unittest.TestCase):
    """插入位置必须按 mesh 注释锁行，不能只按索引区间。

    实测事故：多部件角色每个部件用自己的 IB，`drawindexed` 的 start 常常都是 0。
    `菲欧妮.ini` 里 `9040e2c4-16194-56844` 是 `drawindexed = 72336,0,0`、
    `b11ff137-6078-0` 是 `drawindexed = 38448,0,0`，面序号 12516 反算出 37548
    后**两个区间都命中**，只按区间取第一个会把水喷到别的部件上。
    """

    INI = "\n".join([
        "; [mesh:LOD0.9040e2c4-16194-56844_copy]",
        "drawindexed = 72336, 0, 0",
        "; [mesh:LOD0.b11ff137-6078-0_copy]",
        "drawindexed = 38448, 0, 0",
        "",
    ])

    def test_locks_the_right_draw(self):
        hit = merge.locate_draw_line(self.INI, "LOD0.9040e2c4-16194-56844", 37548)
        self.assertIsNotNone(hit)
        pos, indent = hit
        self.assertEqual(indent, "")
        self.assertIn("72336", self.INI[:pos].splitlines()[-1])

    def test_same_index_in_other_mesh_resolves_to_that_mesh(self):
        hit = merge.locate_draw_line(self.INI, "LOD0.b11ff137-6078-0", 37548)
        self.assertIsNotNone(hit)
        pos, _ = hit
        self.assertIn("38448", self.INI[:pos].splitlines()[-1])

    def test_out_of_range_returns_none(self):
        self.assertIsNone(
            merge.locate_draw_line(self.INI, "LOD0.9040e2c4-16194-56844", 999999))

    def test_unknown_mesh_returns_none(self):
        self.assertIsNone(
            merge.locate_draw_line(self.INI, "不存在的物体", 37548))

    def test_indent_is_preserved(self):
        ini = "\n".join([
            "; [mesh:Body]",
            "\t\tdrawindexed = 99, 0, 0",
            "",
        ])
        hit = merge.locate_draw_line(ini, "Body", 9)
        self.assertIsNotNone(hit)
        self.assertEqual(hit[1], "\t\t")


if __name__ == "__main__":
    unittest.main()

class CollectGateHashesTests(unittest.TestCase):
    """门控 hash 只取「出现次数最多」的那些（并列全要）。"""

    def test_only_the_most_frequent_wins(self):
        txt = """
[TextureOverride_A]
hash = aaaa1111
[TextureOverride_B]
hash = aaaa1111
[TextureOverride_C]
hash = aaaa1111
[TextureOverride_D]
hash = bbbb2222
"""
        self.assertEqual(merge.collect_gate_hashes(txt), ["aaaa1111"])

    def test_ties_are_all_kept(self):
        txt = """
[TextureOverride_A]
hash = aaaa1111
[TextureOverride_B]
hash = aaaa1111
[TextureOverride_C]
hash = bbbb2222
[TextureOverride_D]
hash = bbbb2222
[TextureOverride_E]
hash = cccc3333
"""
        self.assertEqual(merge.collect_gate_hashes(txt), ["aaaa1111", "bbbb2222"])

    def test_shader_override_hashes_are_ignored(self):
        """64 位着色器 hash 收进来会写一个永远匹配不上的门控段。"""
        txt = """
[ShaderOverride_Whatever]
hash = cdc90aee00e7900d
[TextureOverride_Body]
hash = aaaa1111
"""
        self.assertEqual(merge.collect_gate_hashes(txt), ["aaaa1111"])

    def test_empty_returns_empty(self):
        self.assertEqual(merge.collect_gate_hashes(""), [])
        self.assertEqual(merge.collect_gate_hashes("[Constants]\n$x = 1\n"), [])

class NonMirrorWorkflowTests(unittest.TestCase):
    """SSMT 非镜像工作流：Blender 里镜像、游戏里原样，顶点顺序要翻。"""

    def test_marker_detected(self):
        self.assertTrue(merge.needs_x_mirror(["_ssmt_non_mirror_workflow_processed"]))
        self.assertTrue(merge.needs_x_mirror(["a", "_ssmt_non_mirror_workflow_processed", "b"]))

    def test_marker_absent(self):
        self.assertFalse(merge.needs_x_mirror([]))
        self.assertFalse(merge.needs_x_mirror(["3DMigoto:MatchFirstIndex"]))

    def test_non_mirrored_keeps_order(self):
        self.assertEqual(merge.game_triangle_order(False), (0, 1, 2))

    def test_mirrored_swaps_last_two(self):
        self.assertEqual(merge.game_triangle_order(True), (0, 2, 1))


class ForceFieldPackingTests(unittest.TestCase):
    """力场打包：布局必须与 ForceField.hlsl:47-51 逐字对齐。"""

    def test_size_matches_resource_array(self):
        floats = merge.build_force_field_floats({})
        self.assertEqual(len(floats), merge.FORCE_FIELD_ROWS * 4)
        # Probe.ini 的 [ResourceFluidForceField] 声明 array = 96
        self.assertEqual(merge.FORCE_FIELD_ROWS, 96)

    def test_three_rows_per_field(self):
        floats = merge.build_force_field_floats({0: [{
            "enabled": True, "kind": "RADIAL",
            "offset_u": 0.1, "offset_v": 0.2, "offset_w": 0.05,
            "axis_u": 1.0, "axis_v": 0.0, "axis_w": 0.0,
            "radius": 0.12, "strength": 4.5,
        }]})
        # row0 = (offset, type)
        self.assertEqual(floats[0:4], [0.1, 0.2, 0.05, 1.0])
        # row1 = (axis, radius)
        self.assertEqual(floats[4:8], [1.0, 0.0, 0.0, 0.12])
        # row2 = (strength, falloff, 0, enabled)
        self.assertEqual(floats[8:12], [4.5, 2.0, 0.0, 1.0])

    def test_falloff_exponent_is_exported(self):
        """falloff 必须在面板上改得动、并且真的进缓冲（否则是个假旋钮）。"""
        for power in (1.0, 2.0, 4.0):
            floats = merge.build_force_field_floats({0: [{
                "strength": 1.0, "falloff": power,
            }]})
            self.assertEqual(floats[9], power, "falloff=%s" % power)
        # 没写 falloff 的数据落到参考实现的 2.0
        floats = merge.build_force_field_floats({0: [{"strength": 1.0}]})
        self.assertEqual(floats[9], 2.0)

    def test_kind_encoding_matches_shader(self):
        for name, code in (("DIRECTIONAL", 0.0), ("RADIAL", 1.0), ("VORTEX", 2.0)):
            floats = merge.build_force_field_floats({0: [{"kind": name}]})
            self.assertEqual(floats[3], code, name)

    def test_mirrored_workflow_negates_u_only(self):
        """X 镜像工作流：游戏侧 right = -M(Blender right)，所以只翻 U。

        依据：ResolveSurface.hlsl:63-64 的 right=up×forward 在镜像下手性翻转；
        ForceField.hlsl:97/:105 只负责 right*x，不再取反（翻两次等于没翻）。
        """
        field = {
            "enabled": True, "kind": "DIRECTIONAL",
            "mirror_u": True,
            "offset_u": 0.1, "offset_v": 0.2, "offset_w": 0.05,
            "axis_u": 30.0, "axis_v": 1.0, "axis_w": 2.0,
            "radius": 0.5, "strength": 20.0,
        }
        floats = merge.build_force_field_floats({0: [field]})
        # row0 = (offset, type)：U 反号，V/W 原样
        self.assertEqual(floats[0], -0.1)
        self.assertEqual(floats[1], 0.2)
        self.assertEqual(floats[2], 0.05)
        # row1 = (axis, radius)：U 反号，V/W 原样（方向来自轴，不是 offset）
        self.assertEqual(floats[4], -30.0)
        self.assertEqual(floats[5], 1.0)
        self.assertEqual(floats[6], 2.0)

    def test_mirror_flag_defaults_to_false(self):
        """没给 mirror_u 的字段必须原样写出（非镜像模型占多数，不能顺手翻）。"""
        floats = merge.build_force_field_floats({0: [{
            "offset_u": 0.1, "axis_u": 30.0,
        }]})
        self.assertEqual(floats[0], 0.1)
        self.assertEqual(floats[4], 30.0)

    def test_slot_stride_is_four_fields(self):
        floats = merge.build_force_field_floats({
            0: [{"strength": 1.0}],
            1: [{"strength": 2.0}],
        })
        # 每槽 4 字段 × 3 行 × 4 分量 = 48 个 float
        self.assertEqual(floats[8], 1.0)
        self.assertEqual(floats[48 + 8], 2.0)
        # 槽 0 的字段 1..3 必须是零，否则槽 1 的力会「漏」到槽 0
        self.assertEqual(sum(floats[12:48]), 0.0)

    def test_extra_fields_are_dropped(self):
        many = [{"strength": float(i + 1)} for i in range(6)]
        floats = merge.build_force_field_floats({0: many})
        self.assertEqual(floats[8], 1.0)
        self.assertEqual(floats[12 * 3 + 8], 4.0)
        # 第 5、6 个不该出现（下一个槽是零）
        self.assertEqual(sum(floats[48:]), 0.0)

    def test_disabled_field_keeps_geometry_but_flips_flag(self):
        floats = merge.build_force_field_floats({0: [{
            "enabled": False, "radius": 0.2, "strength": 9.0,
        }]})
        self.assertEqual(floats[11], 0.0)
        self.assertFalse(merge.force_fields_enabled(floats))

    def test_enabled_detection(self):
        self.assertFalse(merge.force_fields_enabled(merge.build_force_field_floats({})))
        self.assertTrue(merge.force_fields_enabled(
            merge.build_force_field_floats({2: [{"enabled": True}]})))

    def test_out_of_range_slot_is_ignored(self):
        floats = merge.build_force_field_floats({
            merge.FORCE_FIELD_SLOT_COUNT: [{"strength": 5.0}],
            -1: [{"strength": 5.0}],
        })
        self.assertEqual(sum(floats), 0.0)


class ForceFieldInjectionTests(unittest.TestCase):
    """把力场写进模板 ini，以及「没有力场时逐字节不变」。"""

    TEMPLATE = """[Constants]
global $force_enabled = 0
global $force_scale = 1
[ResourceFluidForceField]
type = Buffer
format = R32G32B32A32_FLOAT
array = 96
[Present]
x41 = $force_enabled
y41 = $force_scale
"""

    def test_data_line_goes_after_array(self):
        floats = merge.build_force_field_floats({0: [{"strength": 3.0}]})
        out = merge.apply_force_fields(self.TEMPLATE, floats)
        lines = out.splitlines()
        self.assertIn("[ResourceFluidForceField]", lines)
        i = lines.index("array = 96")
        self.assertTrue(lines[i + 1].startswith("data = "), lines[i + 1])
        self.assertEqual(lines[i + 2], "[Present]")

    def test_enable_flag_turns_on(self):
        floats = merge.build_force_field_floats({0: [{"strength": 3.0}]})
        out = merge.apply_force_fields(self.TEMPLATE, floats)
        self.assertIn("global $force_enabled = 1", out)

    def test_no_field_is_byte_identical(self):
        """没有力场时必须原样返回，否则每次导出都会产生无意义 diff。"""
        out = merge.apply_force_fields(self.TEMPLATE, merge.build_force_field_floats({}))
        self.assertEqual(out, self.TEMPLATE)

    def test_reverting_restores_template(self):
        floats = merge.build_force_field_floats({0: [{"strength": 3.0}]})
        on = merge.apply_force_fields(self.TEMPLATE, floats)
        off = merge.apply_force_fields(on, merge.build_force_field_floats({}))
        self.assertEqual(off, self.TEMPLATE)

    def test_idempotent(self):
        floats = merge.build_force_field_floats({0: [{"strength": 3.0}]})
        once = merge.apply_force_fields(self.TEMPLATE, floats)
        twice = merge.apply_force_fields(once, floats)
        self.assertEqual(once, twice)
        self.assertEqual(once.count("data = "), 1)

    def test_gain_is_written(self):
        floats = merge.build_force_field_floats({0: [{"strength": 3.0}]})
        out = merge.apply_force_fields(self.TEMPLATE, floats, gain=2.5)
        self.assertIn("global $force_scale = 2.5", out)

    def test_float_format_avoids_exponent(self):
        """``1e-05`` 这种写法参考实现一次都没用过，别赌解析器认。"""
        self.assertEqual(merge._fmt_float(1.0), "1")
        self.assertEqual(merge._fmt_float(0.0), "0")
        self.assertEqual(merge._fmt_float(0.00001), "0.00001")
        self.assertNotIn("e", merge._fmt_float(1e-9))

    def test_real_template_has_the_section_and_globals(self):
        text = _TEMPLATE_PATH.read_text(encoding="utf-8-sig")
        self.assertIn("[ResourceFluidForceField]", text)
        self.assertIn("global $force_enabled = 0", text)
        self.assertIn("global $force_scale = 1", text)
        self.assertIn("x41 = $force_enabled", text)
        self.assertIn("y41 = $force_scale", text)

class VariablePrefixTests(unittest.TestCase):
    """3Dmigoto 的 ``$var`` 不随 namespace 隔离，所以必须自己加前缀。

    不加的后果不是报错，是 ``$enabled`` / ``$drawn`` 这类通用名和别的 mod
    互相覆写：表现为「开关按了没反应」或者「别的 mod 的开关能控制我的水」。
    """

    def test_renames_plain_names(self):
        self.assertEqual(merge.prefix_variables("$enabled = 1"),
                         "$fluid_enabled = 1")
        self.assertEqual(merge.prefix_variables("if $seen && !$drawn"),
                         "if $fluid_seen && !$fluid_drawn")

    def test_is_idempotent(self):
        once = merge.prefix_variables("global $capture = 0")
        self.assertEqual(merge.prefix_variables(once), once)

    def test_leaves_namespaced_reference(self):
        # $ 后面紧跟反斜杠是命名空间引用，不是变量
        text = "if $\\EFMIv1\\enable_mods"
        self.assertEqual(merge.prefix_variables(text), text)

    def test_does_not_touch_bare_words(self):
        text = "type = Buffer\narray = 96\ndata = 0 0 0 1\n"
        self.assertEqual(merge.prefix_variables(text), text)

    def test_every_template_variable_ends_up_prefixed(self):
        raw = _TEMPLATE_PATH.read_text(encoding="utf-8-sig")
        out = merge.prefix_variables(raw)
        names = set()
        i = 0
        while True:
            i = out.find("$", i)
            if i < 0:
                break
            j = i + 1
            while j < len(out) and (out[j].isalnum() or out[j] == "_"):
                j += 1
            if j > i + 1:
                names.add(out[i + 1:j])
            i += 1
        self.assertTrue(names)
        bad = sorted(n for n in names if not n.startswith(merge.VARIABLE_PREFIX))
        self.assertEqual(bad, [], "未加前缀的变量: %s" % bad)

    def test_force_switch_survives_prefixing(self):
        """apply_force_fields 先跑、改名后跑，两边必须仍然是同一对名字。"""
        text = _TEMPLATE_PATH.read_text(encoding="utf-8-sig")
        floats = merge.build_force_field_floats({0: [{"strength": 2.0}]})
        gated = merge.apply_force_fields(text, floats, 1.5)
        out = merge.prefix_variables(gated)
        self.assertIn("global $fluid_force_enabled = 1", out)
        self.assertIn("global $fluid_force_scale = 1.5", out)
        self.assertIn("x41 = $fluid_force_enabled", out)
        self.assertIn("y41 = $fluid_force_scale", out)

    def test_settles_when_prefix_already_present(self):
        """重复导出不该产生 fluid_fluid_。"""
        text = "global $fluid_force_scale = 2\nx41 = $fluid_force_enabled"
        self.assertEqual(merge.prefix_variables(text), text)


class EmitterKeyTests(unittest.TestCase):
    """每个发射器一个开关快捷键（``[KeyFluidEmitN]`` → ``$emit_N`` → 42/43 分量）。

    这套东西有三处必须同时对上，任何一处错位都表现为「按了没反应」：
    段名里的序号、被拨的变量名、Slots.hlsl 里读 42/43 的那个分量。
    """

    def test_bare_key_gets_modifiers(self):
        """只写 VK_F1 时补 no_alt no_ctrl no_shift，和模板其它 Key 段一致。"""
        self.assertEqual(
            merge.normalise_key("VK_F1"), "no_alt no_ctrl no_shift VK_F1")
        self.assertEqual(
            merge.normalise_key("  VK_F1  "), "no_alt no_ctrl no_shift VK_F1")

    def test_full_spec_passes_through(self):
        """自己写了修饰词就原样保留，不再套一层。"""
        self.assertEqual(merge.normalise_key("no_alt VK_F1"), "no_alt VK_F1")
        self.assertEqual(
            merge.normalise_key("no_ctrl no_shift VK_SUBTRACT"),
            "no_ctrl no_shift VK_SUBTRACT")

    def test_empty_key_means_unbound(self):
        for value in ("", "   ", None):
            self.assertEqual(merge.normalise_key(value), "")

    def test_block_shape(self):
        """段名、type=cycle、被拨的变量名三处都得对。"""
        block = merge.build_emitter_key_block([(2, "VK_F2")])
        self.assertIn("[KeyFluidEmit2]", block)
        self.assertIn("key = no_alt no_ctrl no_shift VK_F2", block)
        self.assertIn("type = cycle", block)
        self.assertIn("$fluid_emit_2 = 1,0", block)

    def test_no_keys_leaves_no_trace(self):
        """一个键都没绑时不留空段，导出结果和没有这套机制之前一致。"""
        text = "a = 1\n"
        self.assertEqual(merge.apply_emitter_keys(text, []), text)
        self.assertEqual(
            merge.apply_emitter_keys(text, [(0, ""), (1, "  ")]), text)

    def test_idempotent(self):
        """反复导出不叠加段、不累积空行。"""
        text = "a = 1\n"
        once = merge.apply_emitter_keys(text, [(0, "VK_F1")])
        twice = merge.apply_emitter_keys(once, [(0, "VK_F1")])
        self.assertEqual(once, twice)
        self.assertEqual(once.count("[KeyFluidEmit0]"), 1)

    def test_removed_emitter_drops_its_key(self):
        """删掉发射器后它的键必须消失，否则那个键会去拨一个不存在的槽。"""
        text = merge.apply_emitter_keys("a = 1\n", [(0, "VK_F1"), (1, "VK_F2")])
        self.assertIn("[KeyFluidEmit1]", text)
        after = merge.apply_emitter_keys(text, [(0, "VK_F1")])
        self.assertNotIn("[KeyFluidEmit1]", after)
        self.assertNotIn("VK_F2", after)
        self.assertIn("[KeyFluidEmit0]", after)

    def test_slot_index_out_of_range_warns_and_skips(self):
        """槽号越界要跳过并**告警**，不能静默丢掉一个键，也不能炸掉整次导出。

        早先是 ``raise ValueError``：第 9 个发射器（哪怕没绑键）会让
        ``_build_main_ini`` 抛穿 ``_deploy``，整份导出一个文件都不写。
        """
        warnings = []
        block = merge.build_emitter_key_block(
            [(merge.EMITTER_KEY_MAX, "VK_F1")], warnings)
        self.assertEqual(block, "")
        self.assertEqual(len(warnings), 1)
        self.assertIn(str(merge.EMITTER_KEY_MAX), warnings[0])

        # 越界的只跳过自己，正常的那些照样出段。
        warnings = []
        block = merge.build_emitter_key_block(
            [(0, "VK_F1"), (merge.EMITTER_KEY_MAX, "VK_F9")], warnings)
        self.assertIn("[KeyFluidEmit0]", block)
        self.assertNotIn("[KeyFluidEmit8]", block)
        self.assertNotIn("VK_F9", block)
        self.assertEqual(len(warnings), 1)

        # 不传 warnings 时也不能抛（调用方可能只是想知道文本）。
        self.assertEqual(
            merge.build_emitter_key_block([(merge.EMITTER_KEY_MAX, "VK_F1")]), "")
        self.assertEqual(
            merge.apply_emitter_keys("a = 1\n", [(merge.EMITTER_KEY_MAX, "VK_F1")]),
            "a = 1\n")

    def test_prefix_variables_leaves_gate_vars_alone(self):
        """$fluid_emit_N 已经带前缀，改名不能变成 $fluid_fluid_emit_N。"""
        text = merge.apply_emitter_keys("a = 1\n", [(0, "VK_F1")])
        self.assertEqual(merge.prefix_variables(text), text)


class SlotCountTests(unittest.TestCase):
    """``$slot_count`` 必须等于「本轮要跟踪的发射器数」（一个发射器一槽）。

    写死 1（旧行为）时 ``AssignSlots.hlsl:29`` 的槽位上限就是 1：多发射器
    mod 里只有 slot 0 能填，其余静默不喷。上限 8 = ``ParticleConfig.hlsl``
    的 ``SLOT_COUNT``，超出部分没有槽位可用，必须告警。
    """

    _SAMPLE = "[Constants]\nglobal $slot_count = 8\n"

    def _value(self, text):
        found = []
        for line in text.splitlines():
            head, _sep, tail = line.strip().partition("=")
            if head.strip() in ("global $slot_count", "global $fluid_slot_count"):
                found.append(int(tail))
        self.assertEqual(len(found), 1)
        return found[0]

    def test_counts_emitters(self):
        for count in (1, 2, 3, 5, 8):
            self.assertEqual(
                self._value(merge.apply_slot_count(self._SAMPLE, count)), count)

    def test_zero_emitters_keeps_one_slot(self):
        """没有发射器也留 1 个槽：写 0 会让 clamp(..,1,N) 与空 pass 出怪。"""
        self.assertEqual(self._value(merge.apply_slot_count(self._SAMPLE, 0)), 1)

    def test_upper_clamp_warns(self):
        warnings = []
        text = merge.apply_slot_count(self._SAMPLE, 9, warnings)
        self.assertEqual(self._value(text), merge.SLOT_COUNT_MAX)
        self.assertEqual(len(warnings), 1)
        self.assertIn(str(merge.SLOT_COUNT_MAX + 1), warnings[0])

    def test_no_warning_within_capacity(self):
        warnings = []
        merge.apply_slot_count(self._SAMPLE, merge.SLOT_COUNT_MAX, warnings)
        self.assertEqual(warnings, [])

    def test_keeps_prefix_and_ignores_lookalikes(self):
        """已经改名成 $fluid_slot_count 时保留前缀；$slot_count_max 不许被吃。"""
        renamed = "[Constants]\nglobal $fluid_slot_count = 1\n"
        out = merge.apply_slot_count(renamed, 3)
        self.assertIn("global $fluid_slot_count = 3", out)
        self.assertNotIn("$fluid_fluid_", out)
        latin = "[Constants]\nglobal $slot_count_max = 6\nglobal $slot_count = 8\n"
        out = merge.apply_slot_count(latin, 2)
        self.assertIn("global $slot_count_max = 6", out)
        self.assertEqual(self._value(out), 2)


if __name__ == "__main__":
    unittest.main()


class GlobalKeyTests(unittest.TestCase):
    """3 个全局 Key 段的改名。

    关键性质：一个键都不改时输出必须与输入**逐字节一致** —— 否则「导出结果和
    参考实现等价」这条基础假设就没了，而它正是其余所有注入测试的前提。

    2026-11-03 起表里只剩 3 段（自动喷射 / 按住喷 / 缠流）：六段「抢面板变量」
    的键和总开关键都删了，总开关改成角色门控硬关（见 ``Probe.ini`` 的 ``[Present]``）。
    """

    _SAMPLE = (
        "[KeyTrickle9]\n"
        "    key = no_alt no_ctrl no_shift VK_NUMPAD9\n"
        "    run = CommandListTrickle9\n"
        "\n"
        "[KeyAutoPulse]\n"
        "    key = no_alt no_ctrl no_shift VK_NUMPAD4\n"
        "    type = cycle\n"
        "    $auto_pulse = 0,1\n"
    )

    def test_no_override_is_identity(self):
        text, warnings = merge.apply_global_keys(self._SAMPLE, {})
        self.assertEqual(text, self._SAMPLE)
        self.assertEqual(warnings, [])

    def test_blank_field_keeps_default(self):
        text, _ = merge.apply_global_keys(self._SAMPLE, {"trickle": "   "})
        self.assertEqual(text, self._SAMPLE)

    def test_default_value_is_a_noop(self):
        """节点把出厂键直接填在输入栏里，所以「值 == 出厂值」必须什么都不改。

        否则每个字段都会去重写一遍自己，输出虽然一样，但查重告警会把
        每个段两两配成「同一个键挂了两个段」全部误报。
        """
        mapping = dict(merge.GLOBAL_KEY_DEFAULTS)
        text, warnings = merge.apply_global_keys(self._SAMPLE, mapping)
        self.assertEqual(text, self._SAMPLE)
        self.assertEqual(warnings, [])

    def test_clearing_a_prefilled_field_falls_back_to_default(self):
        """用户把填好的出厂值删掉，效果等于没改，而不是绑成空键。"""
        text, _ = merge.apply_global_keys(self._SAMPLE, {"trickle": ""})
        self.assertEqual(text, self._SAMPLE)

    def test_defaults_match_the_real_template(self):
        """出厂值必须和 Probe.ini 里那几段逐字一致，否则预填就是假话。

        2026-11-03 按主人要求删了 7 段并同步删掉表条目：六段会抢
        ``$strength_level`` / ``$liquid_tint_r/g/b`` / ``$liquid_absorb_mm`` 的
        （强度 +/-、更白/更黄、更不透明/更透明），以及总开关键 ``KeyToggle``
        ——总开关改成「角色不在场＝零计算」的硬门控，ini 里不再有手动开关键。
        表里现在 3 条，模板里也只剩 3 段，所以本测试就是 3 条断言。
        """
        template = _TEMPLATE_PATH.read_text(encoding="utf-8")
        for _section, field in merge.GLOBAL_KEY_SECTIONS:
            spec = merge.GLOBAL_KEY_DEFAULTS[field]
            self.assertIn('key = %s' % spec, template,
                          '%s 的出厂值 %r 在模板里找不到' % (field, spec))

    def test_rebinds_only_the_named_section(self):
        text, _ = merge.apply_global_keys(self._SAMPLE, {"trickle": "VK_F1"})
        self.assertIn("key = no_alt no_ctrl no_shift VK_F1", text)
        self.assertIn("key = no_alt no_ctrl no_shift VK_NUMPAD4", text)
        self.assertNotIn("VK_NUMPAD9", text)

    def test_full_spec_kept_verbatim(self):
        text, _ = merge.apply_global_keys(
            self._SAMPLE, {"auto_pulse": "no_alt VK_F4"})
        self.assertIn("key = no_alt VK_F4", text)

    def test_idempotent(self):
        once, _ = merge.apply_global_keys(self._SAMPLE, {"trickle": "VK_F1"})
        twice, _ = merge.apply_global_keys(once, {"trickle": "VK_F1"})
        self.assertEqual(once, twice)

    def test_missing_section_is_ignored(self):
        """模板里没有这段时不能凭空插一段出来。"""
        text, _ = merge.apply_global_keys(
            "[Other]\n    key = VK_A\n", {"trickle": "VK_F1"})
        self.assertEqual(text, "[Other]\n    key = VK_A\n")

    def test_duplicate_binding_warns(self):
        _text, warnings = merge.apply_global_keys(
            self._SAMPLE, {"trickle": "VK_F1", "auto_pulse": "VK_F1"})
        self.assertTrue(any("VK_F1" in w for w in warnings))

    def test_emitter_key_collision_warns(self):
        _text, warnings = merge.apply_global_keys(
            self._SAMPLE, {"trickle": "VK_F1"}, emitter_keys=["VK_F1"])
        self.assertTrue(any("发射器" in w for w in warnings))

    def test_template_has_no_manual_master_switch(self):
        """回归守卫：总开关改硬门控后，模板里不许再出现手动开关那四个变量。"""
        template = _TEMPLATE_PATH.read_text(encoding="utf-8")
        for var in ("$enabled", "$effect_requested", "$shutdown_remaining",
                    "$last_effect_request"):
            self.assertNotIn(var, template,
                             "总开关已改成角色门控硬关，模板里不该再有 %s" % var)
        self.assertNotIn("[CommandListToggleEffect]", template)


class EmitterControlTests(unittest.TestCase):
    """每个发射器 / 每个力场的运行期变量（开关 / 速度 / 力场字段开关）。

    最关键的性质是**速度与掩码缺省不写**：一个速度都没改、没有字段被关时，
    这两族不出现。唯一总会写的是力场族标记 ``$fluid_force_fields_switchable``
    （缺省 0）——``IniParams`` 无命名空间，只写 1 不写 0 会让上一份 mod 留下的
    ``w41=1`` 把这份导出的力场全判 OFF 且不报错。

    2026-11-03 收敛：力场不再有「发射器级开关」和「强度倍率」两族变量 ——
    每个力场自己的「启用」写 ``$fluid_force_enable_<E>_<K>``，一个力场一个。
    """

    _SAMPLE = (
        "[Constants]\n"
        "global $emit_0 = 1\n"
        "global $emit_1 = 1\n"
        "global $emit_7 = 1\n"
        "[CommandListDrawWorld]\n"
        "    w43 = $emit_7\n"
        "    y41 = $force_scale\n"
        "    $initialized = 1\n"
    )

    def test_defaults_write_only_the_family_flag(self):
        """没有发射器时只多一行 ``w41 = 0``；有发射器时还会多「面板值声明」与开场赋值。"""
        text = self._SAMPLE
        out, warnings = merge.apply_emitter_controls(
            text, [0.0, 0.0], [[1, 1], [1]], enables=[])
        self.assertEqual(warnings, [])
        self.assertIn("    w41 = 0", out)
        self.assertNotIn("fluid_force_enable", out)
        self.assertNotIn("fluid_emitter_speed", out)
        self.assertNotIn("fluid_emit_seeded", out)
        self.assertEqual(out.replace("\n    w41 = 0", "", 1), text)

    def test_speed_lands_in_its_own_lane(self):
        out, warnings = merge.apply_emitter_controls(
            self._SAMPLE, [0.0, 0.0, 0.8], [[1], [1], [1]])
        self.assertEqual(warnings, [])
        # 发射器 0..2 各一行声明，缺省那两个写 0（= 沿用内部兜底）
        self.assertIn("global $fluid_emitter_speed_0 = 0", out)
        self.assertIn("global $fluid_emitter_speed_1 = 0", out)
        self.assertIn("global $fluid_emitter_speed_2 = 0.8", out)
        self.assertNotIn("global $fluid_emitter_speed_3", out)
        # z45 = 第 2 个发射器；写在 w43 之后、与它同缩进
        self.assertIn("    z45 = $fluid_emitter_speed_2", out)
        # 力场那一族没动，就不该出现（族标记写 0，不算「这一族存在」）
        self.assertNotIn("fluid_force_enable", out)
        self.assertIn("    w41 = 0", out)

    def test_field_switch_packs_into_a_mask(self):
        """两个力场：关第 1 个、留第 2 个 → 掩码 bit0=0、bit1=1。"""
        out, warnings = merge.apply_emitter_controls(
            self._SAMPLE, [0.0], [[0, 1]])
        self.assertEqual(warnings, [])
        self.assertIn("    w41 = 1", out)
        self.assertIn("global $fluid_force_enable_0_0 = 0", out)
        self.assertIn("global $fluid_force_enable_0_1 = 1", out)
        # 掩码 = 0 + 2*1
        self.assertIn("    x58 = $fluid_force_enable_0_0 + 2*$fluid_force_enable_0_1",
                      out)
        self.assertNotIn("fluid_emitter_speed", out)

    def test_field_switch_all_on_writes_no_mask(self):
        out, _warnings = merge.apply_emitter_controls(
            self._SAMPLE, [0.0], [[1, 1, 1, 1]])
        self.assertNotIn("fluid_force_enable", out)
        self.assertIn("    w41 = 0", out)

    def test_lifetime_lands_in_51_52(self):
        """每发射器寿命：第 3 个设 3.0 → z51；0 的两个跟着写 0（= 沿用 1.1）。"""
        out, warnings = merge.apply_emitter_controls(
            self._SAMPLE, [0.0, 0.0, 0.0], lifetimes=[0.0, 0.0, 3.0])
        self.assertEqual(warnings, [])
        self.assertIn("global $fluid_emitter_lifetime_0 = 0", out)
        self.assertIn("global $fluid_emitter_lifetime_2 = 3", out)
        self.assertIn("    z51 = $fluid_emitter_lifetime_2", out)
        # 第 5 个发射器落在 52.x 那一组
        out5, _w = merge.apply_emitter_controls(
            self._SAMPLE, [0.0] * 5, lifetimes=[0.0, 0.0, 0.0, 0.0, 2.5])
        self.assertIn("    x52 = $fluid_emitter_lifetime_4", out5)

    def test_lifetime_default_writes_nothing(self):
        """寿命全 0 = 沿用 1.1 s：不许写出任何寿命行。

        注意 ``w41 = 0`` 是**力场开关族的复位行**，每次导出都要写（IniParams 没有
        命名空间，别的 mod 可能留下 1），所以这里不能要求逐字节相同；只要求
        "寿命一族一行都不写"。
        """
        text = self._SAMPLE
        out, warnings = merge.apply_emitter_controls(
            text, [0.0, 0.0], lifetimes=[0.0, 0.0])
        self.assertEqual(warnings, [])
        self.assertNotIn("fluid_emitter_lifetime", out)
        self.assertNotIn("51 = $fluid_emitter_lifetime", out)
        self.assertIn("w41 = 0", out)          # 族复位行是有意的

    def test_lifetime_11_is_the_default_and_writes_nothing(self):
        """面板默认就是 1.1 s：全填 1.1 与「不填」等价，一行都不许写。"""
        text = self._SAMPLE
        out, warnings = merge.apply_emitter_controls(
            text, [0.0, 0.0], lifetimes=[1.1, 1.1])
        self.assertEqual(warnings, [])
        self.assertNotIn("fluid_emitter_lifetime", out)
        # 混合：1.1 归一成 0（shader 的 0 → 回落 1.1），3.0 照写
        out2, _w = merge.apply_emitter_controls(
            self._SAMPLE, [0.0, 0.0], lifetimes=[1.1, 3.0])
        self.assertIn("global $fluid_emitter_lifetime_0 = 0", out2)
        self.assertIn("global $fluid_emitter_lifetime_1 = 3", out2)
        self.assertIn("    y51 = $fluid_emitter_lifetime_1", out2)
        # 除这条复位行外，其余与输入一致
        self.assertEqual(out.replace("    w41 = 0\n", ""), text)

    def test_template_has_the_gravity_sentinel(self):
        """模板里 `z47 = 1` 必须出现在 `x47 = $fluid_gravity_scale` 之后不远处。

        中间允许有注释行（哨兵为什么要写需要解释），但不许有别的赋值把顺序打乱。
        """
        template = _TEMPLATE_PATH.read_text(encoding="utf-8-sig")
        lines = template.splitlines()
        idx = lines.index("x47 = $fluid_gravity_scale")
        window = [ln.strip() for ln in lines[idx + 1:idx + 8]]
        self.assertIn("z47 = 1", window)
        # 窗口里在 z47 之前不许出现别的 x/y/z/w 赋值
        before = window[:window.index("z47 = 1")]
        assigns = [ln for ln in before
                   if ln[:1] in "xyzw" and ln[1:2].isdigit() and "=" in ln]
        self.assertEqual(assigns, [])

    def test_switch_off_rewrites_the_template_default(self):
        """开关值 = 面板（开 1 / 关 0），并在开场赋一次（不用 persist，理由见文档）。"""
        out, _warnings = merge.apply_emitter_controls(
            self._SAMPLE, [], [], enables=[1, 0])
        self.assertIn("global $fluid_emit_0 = 1", out)
        self.assertIn("global $fluid_emit_1 = 0", out)
        self.assertIn("global $fluid_emit_seeded = 0", out)
        self.assertIn("if !$fluid_emit_seeded", out)
        self.assertIn("$fluid_emit_1 = 0", out)
        self.assertIn("$fluid_emit_seeded = 1", out)

    def test_emitters_beyond_the_panel_keep_template_default(self):
        """面板只有 2 个发射器：0/1 写成面板值，**2..7 必须显式写 0**。

        模板里 `$fluid_emit_3..7 = 1` 的默认值本身就是行为——槽里记的 id 一旦
        越界到 3..7，那个槽就拿到永久通行证（幽灵发射器）。所以未使用的 id
        必须显式写 0，不能靠「没用到就不写」。
        """
        out, _w = merge.apply_emitter_controls(
            _TEMPLATE_PATH.read_text(encoding="utf-8-sig"), [], [], enables=[1, 1])
        self.assertIn("global $fluid_emit_0 = 1", out)
        self.assertIn("global $fluid_emit_1 = 1", out)
        text = out.replace("$emit_7", "$fluid_emit_7")
        for ghost in range(2, 8):
            self.assertIn("global $fluid_emit_%d = 0" % ghost, text)


class EmitterControlExtraTests(unittest.TestCase):
    """`apply_emitter_controls` 的补充契约（原先被错放在 ExcludeListTests 里，方法体未动）。"""
    """碰撞/可见性「排除列表」：空列表零影响；有项时生成掩码段。"""






    def test_missing_anchor_warns_instead_of_silently_dropping(self):
        text = "[Constants]\nglobal $emit_7 = 1\n"
        _out, warnings = merge.apply_emitter_controls(text, [0.5])
        self.assertTrue(any("w43" in w for w in warnings))

    def test_over_capacity_warns_and_clamps(self):
        _g, _w, _f, warnings = merge.build_emitter_control(
            [0.1] * 9, [[1]] * 9)
        self.assertEqual(len(warnings), 1)
        self.assertIn("9", warnings[0])

    # ---- 真模板（模板里的名字已经带 fluid_ 前缀，2026-11-03 踩过）--------------
    def test_real_template_writes_the_speed_lines(self):
        """直接拿 ``Toolset/fluid/Probe.ini`` 原文跑：0 条告警 + 三行都落位。

        回归点：模板 :964-971 是 ``global $fluid_emit_N = 1``、:205-212 是
        ``w43 = $fluid_emit_7``，带前缀。锚点只认 ``$emit_7`` 时这两处必然
        miss，导出里一条新变量都不会有（告警成唯一线索）。
        """
        template = _TEMPLATE_PATH.read_text(encoding="utf-8-sig")
        out, warnings = merge.apply_emitter_controls(
            template, [0.0, 0.0, 2.0], enables=[1, 1, 1])
        self.assertEqual(warnings, [])
        self.assertIn("global $fluid_emitter_speed_2 = 2", out)
        self.assertIn("z45 = $fluid_emitter_speed_2", out)
        # 一个值都没动的发射器写 0（= 沿用内部兜底），而不是不写
        self.assertIn("global $fluid_emitter_speed_0 = 0", out)
        self.assertIn("x45 = $fluid_emitter_speed_0", out)
        # 声明落在 [Constants] 的 emit 组之后、资源段之前
        self.assertLess(out.index("global $fluid_emitter_speed_2"),
                        out.index("[ResourceFluidA]"))

    def test_real_template_two_fields(self):
        """真模板 + 一个发射器两个力场：K0 关、K1 开。"""
        template = _TEMPLATE_PATH.read_text(encoding="utf-8-sig")
        out, warnings = merge.apply_emitter_controls(
            template, [0.0], [[0, 1]], enables=[1])
        self.assertEqual(warnings, [])
        self.assertIn("global $fluid_force_enable_0_0 = 0", out)
        self.assertIn("global $fluid_force_enable_0_1 = 1", out)
        self.assertIn("x58 = $fluid_force_enable_0_0 + 2*$fluid_force_enable_0_1", out)
        # 族标记是改写模板里已有的那一行（模板未加前缀，导出末尾的
        # prefix_variables 才补 fluid_），所以这里断言的是模板原名。
        self.assertIn("    w41 = 1", out)
        self.assertEqual(out.count("w41 = "), 1)

    def test_real_template_zeroes_a_stale_family_flag(self):
        """M3：上一份 mod 留下的 w41=1 必须被这次导出写回 0。

        IniParams 无命名空间，只写 1 不写 0 时，另一份 mod 的 w41=1 会让这份
        没写 58/59 的导出把每个力场都判 OFF，而且不报错。
        """
        template = _TEMPLATE_PATH.read_text(encoding="utf-8-sig")
        stale = template.replace("    w41 = 0", "    w41 = 1")
        self.assertIn("    w41 = 1", stale)
        out, warnings = merge.apply_emitter_controls(
            stale, [0.0], [[1]], enables=[1])
        self.assertEqual(warnings, [])
        self.assertIn("    w41 = 0", out)

    def test_real_template_switch_off_rewrites_prefixed_default(self):
        template = _TEMPLATE_PATH.read_text(encoding="utf-8-sig")
        out, warnings = merge.apply_emitter_controls(
            template, [], enables=[1, 0, 1])
        self.assertEqual(warnings, [])
        self.assertIn("global $fluid_emit_1 = 0", out)
        self.assertIn("global $fluid_emit_0 = 1", out)
        self.assertIn("global $fluid_emit_2 = 1", out)

    def test_removed_families_never_appear(self):
        """上一轮的 per-emitter 力场开关 / 强度倍率已删，导出里不该再有。"""
        template = _TEMPLATE_PATH.read_text(encoding="utf-8-sig")
        out, _warnings = merge.apply_emitter_controls(
            template, [2.0], [[0, 1]], enables=[1])
        self.assertNotIn("fluid_force_strength", out)
        self.assertNotIn("global $fluid_force_enable_0 =", out)
        self.assertNotIn("IniParams[49]", out)
        self.assertNotIn("IniParams[56]", out)


class EmitGateSourceTests(unittest.TestCase):
    """源码级断言：两门共用同一个 `emitOn`；次生读的是**出生时快照**的开关。"""

    _FLUID = Path(__file__).resolve().parent.parent / "Toolset" / "fluid" / "fluid"

    def test_slot_emit_on_is_called_once_and_shared(self):
        # RED-BY: FluidSolver 里删掉 `bool emitOn=slotEmitOn(slot);`、改回两处各调一次
        text = (self._FLUID / "FluidSolver.hlsl").read_text(encoding="utf-8-sig")
        self.assertEqual(text.count("slotEmitOn("), 2,
                         "一处共用的 emitOn + 事件快照，共 2 次")
        lines = text.splitlines()
        emitting = [ln for ln in lines if "bool emitting=" in ln]
        self.assertEqual(len(emitting), 1)
        self.assertIn("emitOn", emitting[0])
        shared = [ln for ln in lines if "bool emitOn=" in ln]
        self.assertEqual(len(shared), 1, "只许算一次，两门共用")
        snapshot = [ln for ln in lines if "e.slot=float4(slot" in ln]
        self.assertEqual(len(snapshot), 1)
        self.assertIn("slotEmitOn(slot)?1.0:0.0", snapshot[0])

    def test_splash_reads_the_birth_flag_not_the_slot(self):
        # RED-BY: Splash 生成门改回直接读 slotEmitOn(slot)
        text = (self._FLUID / "Splash.hlsl").read_text(encoding="utf-8-sig")
        gate = [ln for ln in text.splitlines() if "events[eventId].slot.y>.5" in ln]
        self.assertEqual(len(gate), 1, "次生门必须读事件里的出生时开关快照")
        self.assertNotIn("slotEmitOn", gate[0])
