# -*- coding: utf-8 -*-
"""EFMI 多 pass 贴图槽位镜像（pass-mirror）的回归测试（2026-09-15 实机数据）。

被测对象：ui/universal/efmi_pass_mirror.py（纯扫描/纯推导/纯行构造，无 bpy 依赖）。

断言要点（全部有实机依据，FrameAnalysis-2026-09-15-160205）：
  1. 扫描：按 (NumViews, PS) 归并出每部件的 pass 布局（扩散图在各 pass 的槽位）；
  2. 推导：原图哈希在非主层、非阴影 pass 位于其它槽位时才镜像——
     皮肤（fcba71f5@ps-t13）→ 只镜像到 G-buffer 的 t0（99002），
     第二层 2f079737 同槽 t13 自动排除、阴影 pass（NumViews=0）自动排除；
     衣服（0e47d51d@ps-t14）→ G-buffer t0（99002）+ 第二层 d72788f1 的 t12（99003）；
  3. 标签"只借不抢"（99001 事故教训）：已被别家注册的哈希接受其标签进条件、
     不再自注册；未注册的才用私号（99002/99003）；
  4. 行构造：条件行 / ShaderOverride 注册段 / 镜像块的确切文本。
"""
import importlib.util
import os
import sys
import unittest

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MODULE_PATH = os.path.join(REPO_ROOT, "ui", "universal", "efmi_pass_mirror.py")


def _load_module(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


MIRROR = _load_module("efmi_pass_mirror_under_test", MODULE_PATH)

# ---- 实机数据（艾尔黛拉 2026-09-15 抓帧，逐字抄自 log 分析）----
IB_BODY = "d6128f13"     # 皮肤
IB_CLOTH = "650a6c6b"    # 衣服
IB_DECO = "b9623e31"     # 装饰
BODY_DIFFUSE = "fcba71f5"
BODY_NORMAL = "42ed44e0"
CLOTH_DIFFUSE = "0e47d51d"
CLOTH_NORMAL = "bab6a380"
DECO_DIFFUSE = "194b53a2"
DECO_NORMAL = "21dc5f76"

PS_SHADOW = "d7bb9dd57f5b70c6"      # 深度/阴影（NumViews=0）
PS_BODY_L1 = "eeb2d6fb78e5a7a8"     # 皮肤 第一层（扩散 t13 / 法线 t14）
PS_BODY_L2 = "2f079737a005dcf6"     # 皮肤 第二层（扩散 t13，同槽）
PS_BODY_C = "78015be8a27acd24"      # 皮肤 G-buffer（扩散 t0 / 法线 t1）
PS_CLOTH_L1 = "de7325869b89a934"    # 衣服 第一层（扩散 t14 / 法线 t16）
PS_CLOTH_L2 = "d72788f1180db1fb"    # 衣服 第二层（扩散 t12）
PS_CLOTH_C = "816908364e93e433"     # 衣服 G-buffer（扩散 t0 / 法线 t1，RabbitFX 已打 1718.2）
PS_DECO_C_INK = "6ad403320174e5dc"  # 装饰 G-buffer 里的描边子层（也读 t0）


def _write_fake_log(path, entries):
    """entries: [(draw, numviews, ib, ps, {tex_hash: slot})] → 真实格式 log.txt。"""
    with open(path, "w", encoding="utf-8") as handle:
        for draw, numviews, ib, ps, slots in entries:
            handle.write(
                "%s OMSetRenderTargets(NumViews:%d, ppRenderTargetViews:0x0, "
                "pDepthStencilView:0x0)\n" % (draw, numviews)
            )
            handle.write(
                "%s DrawIndexedInstanced(IndexCountPerInstance:100, InstanceCount:1, "
                "StartIndexLocation:0, BaseVertexLocation:0, StartInstanceLocation:0)\n"
                % draw
            )
            name = "%s-ib=%s-vs=f11c7e1dbf876a69-ps=%s" % (draw, ib, ps)
            handle.write(
                "%s 3DMigoto Dumping Buffer C:\\dump\\%s.buf -> C:\\dump\\out\\%s.buf\n"
                % (draw, name, name)
            )
            for tex_hash, slot in slots.items():
                tname = "%s-ps-%s=%s-vs=f11c7e1dbf876a69-ps=%s" % (draw, slot, tex_hash, ps)
                handle.write(
                    "%s 3DMigoto Dumping Texture2D C:\\dump\\%s.dds -> C:\\dump\\out\\%s.dds\n"
                    % (draw, tname, tname)
                )
    return path


def _real_frame_log(tmp):
    """复刻艾尔黛拉帧结构的合成日志。"""
    entries = [
        # 皮肤：阴影 / 第一层 / 第二层（同槽 t13）/ G-buffer
        ("000016", 0, IB_BODY, PS_SHADOW, {}),
        ("000080", 2, IB_BODY, PS_BODY_L1, {BODY_DIFFUSE: "t13", BODY_NORMAL: "t14"}),
        ("000093", 2, IB_BODY, PS_BODY_L2, {BODY_DIFFUSE: "t13"}),
        ("000043", 5, IB_BODY, PS_BODY_C, {BODY_DIFFUSE: "t0", BODY_NORMAL: "t1"}),
        # 衣服：阴影 / 第一层 / 第二层（t12）/ G-buffer
        ("000021", 0, IB_CLOTH, PS_SHADOW, {}),
        ("000088", 2, IB_CLOTH, PS_CLOTH_L1, {CLOTH_DIFFUSE: "t14", CLOTH_NORMAL: "t16"}),
        ("000097", 2, IB_CLOTH, PS_CLOTH_L2, {CLOTH_DIFFUSE: "t12"}),
        ("000049", 5, IB_CLOTH, PS_CLOTH_C, {CLOTH_DIFFUSE: "t0", CLOTH_NORMAL: "t1"}),
        # 装饰：G-buffer 里有两个读 t0 的 PS（本体 + 描边子层）
        ("000083", 2, IB_DECO, "183cb9ad60a53f8f", {DECO_DIFFUSE: "t15", DECO_NORMAL: "t17"}),
        ("000045", 5, IB_DECO, "f3926abc95fa801a", {DECO_DIFFUSE: "t0"}),
        ("000052", 5, IB_DECO, PS_DECO_C_INK, {DECO_DIFFUSE: "t0"}),
        ("000099", 2, IB_DECO, PS_CLOTH_L2, {DECO_DIFFUSE: "t12"}),
    ]
    return _write_fake_log(os.path.join(tmp, "log.txt"), entries)


class ScanPassLayoutsTests(unittest.TestCase):
    def test_scans_numviews_ps_and_slots(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log = _real_frame_log(tmp)
            layouts = MIRROR.efmi_scan_pass_layouts([log], {IB_BODY, IB_CLOTH})
        body = layouts[IB_BODY]
        by_ps = {entry["ps"]: entry for entry in body}
        self.assertEqual(by_ps[PS_BODY_C]["numviews"], 5)
        self.assertEqual(by_ps[PS_BODY_C]["slots"][BODY_DIFFUSE], "t0")
        self.assertEqual(by_ps[PS_BODY_C]["slots"][BODY_NORMAL], "t1")
        self.assertEqual(by_ps[PS_BODY_L1]["slots"][BODY_DIFFUSE], "t13")
        self.assertEqual(by_ps[PS_SHADOW]["numviews"], 0)

    def test_ignores_foreign_ib_and_missing_files(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log = _real_frame_log(tmp)
            layouts = MIRROR.efmi_scan_pass_layouts([log], {"deadbeef"})
            self.assertEqual(layouts, {})
            self.assertEqual(MIRROR.efmi_scan_pass_layouts([os.path.join(tmp, "none.txt")], {IB_BODY}), {})
            self.assertEqual(MIRROR.efmi_scan_pass_layouts([], {IB_BODY}), {})


class PlanMirrorSlotsTests(unittest.TestCase):
    def _layouts(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            log = _real_frame_log(tmp)
            return MIRROR.efmi_scan_pass_layouts([log], {IB_BODY, IB_CLOTH, IB_DECO})

    def test_body_diffuse_only_mirrors_to_gbuffer_t0(self):
        """皮肤扩散图：第二层同槽 t13 不镜像、阴影不镜像，只剩 G-buffer t0/99002。"""
        mirrors = MIRROR.efmi_plan_mirror_slots(self._layouts()[IB_BODY], BODY_DIFFUSE, "ps-t13")
        self.assertEqual(mirrors, [(PS_BODY_C, "t0", MIRROR.EFMI_PASSC_FILTER_INDEX)])

    def test_body_normal_mirrors_to_gbuffer_t1(self):
        """皮肤法线：第二层根本没人绑它（无镜像对象），G-buffer 在 t1。"""
        mirrors = MIRROR.efmi_plan_mirror_slots(self._layouts()[IB_BODY], BODY_NORMAL, "ps-t14")
        self.assertEqual(mirrors, [(PS_BODY_C, "t1", MIRROR.EFMI_PASSC_FILTER_INDEX)])

    def test_cloth_diffuse_mirrors_gbuffer_and_layer2(self):
        """衣服扩散图：G-buffer t0/99002 + 第二层 t12/99003。"""
        mirrors = MIRROR.efmi_plan_mirror_slots(self._layouts()[IB_CLOTH], CLOTH_DIFFUSE, "ps-t14")
        self.assertEqual(
            mirrors,
            [
                (PS_CLOTH_C, "t0", MIRROR.EFMI_PASSC_FILTER_INDEX),
                (PS_CLOTH_L2, "t12", MIRROR.EFMI_LAYER2_FILTER_INDEX),
            ],
        )

    def test_deco_gbuffer_collects_both_pass_c_shaders(self):
        """装饰：G-buffer 里本体+描边子层两个 PS 都读 t0 → 两个都要注册。"""
        mirrors = MIRROR.efmi_plan_mirror_slots(self._layouts()[IB_DECO], DECO_DIFFUSE, "ps-t15")
        pass_c = [m for m in mirrors if m[2] == MIRROR.EFMI_PASSC_FILTER_INDEX]
        self.assertEqual(
            sorted(m[0] for m in pass_c),
            sorted(["f3926abc95fa801a", PS_DECO_C_INK]),
        )
        self.assertTrue(all(m[1] == "t0" for m in pass_c))
        self.assertIn((PS_CLOTH_L2, "t12", MIRROR.EFMI_LAYER2_FILTER_INDEX), mirrors)

    def test_empty_inputs(self):
        self.assertEqual(MIRROR.efmi_plan_mirror_slots([], BODY_DIFFUSE, "ps-t13"), [])
        self.assertEqual(MIRROR.efmi_plan_mirror_slots(self._layouts()[IB_BODY], "", "ps-t13"), [])
        self.assertEqual(MIRROR.efmi_plan_mirror_slots(self._layouts()[IB_BODY], BODY_DIFFUSE, ""), [])


class ForeignTagTests(unittest.TestCase):
    """标签只借不抢（99001 事故教训）。"""

    FOREIGN_INI = (
        "[ShaderOverride_RabbitFX_1]\n"
        "hash = 816908364e93e433\n"
        "filter_index = 1718.2\n"
        "\n"
        ";[ShaderOverride_CommentedOut]\n"
        ";hash = d72788f1180db1fb\n"
        ";filter_index = 7777\n"
        "\n"
        "[ShaderOverride_NoFilterIndex]\n"
        "hash = f3926abc95fa801a\n"
    )

    def test_scan_finds_foreign_tags(self):
        found = MIRROR.efmi_scan_foreign_shader_tags(
            [self.FOREIGN_INI], {PS_CLOTH_C, PS_CLOTH_L2, "f3926abc95fa801a"}
        )
        self.assertEqual(found, {PS_CLOTH_C: "1718.2"})

    def test_condition_accepts_ours_and_foreign(self):
        text = MIRROR.efmi_mirror_condition(MIRROR.EFMI_PASSC_FILTER_INDEX, ["1718.2"])
        self.assertEqual(text, "ps == 99002 || ps == 1718.2")
        self.assertEqual(MIRROR.efmi_mirror_condition(99003, []), "ps == 99003")

    def test_condition_dedups_foreign_equal_to_ours(self):
        """别家标签恰好等于私号（如扫到自家旧输出）→ 只保留一份。"""
        text = MIRROR.efmi_mirror_condition(99002, ["99002"])
        self.assertEqual(text, "ps == 99002")

    def test_regex_tagger_detection(self):
        """RabbitFX 式 ShaderRegex（无 hash 行、按字节码打标签）能被发现并点名。"""
        regex_ini = (
            "[ShaderRegexShadow]\n"
            "shader_model = ps_4_0 ps_5_0\n"
            "filter_index = 1718.2\n"
            "\n"
            "[ShaderOverride_Paired]\n"
            "hash = 816908364e93e433\n"
            "filter_index = 1718.2\n"
        )
        taggers = MIRROR.efmi_scan_regex_taggers([regex_ini])
        self.assertEqual(taggers, {"1718.2": "ShaderRegexShadow"})
        # 有 hash 行的成对段不算正则打标器
        paired_only = MIRROR.efmi_scan_regex_taggers(["[ShaderOverride_Paired]\nhash = 816908364e93e433\nfilter_index = 1718.2\n"])
        self.assertEqual(paired_only, {})

    def test_override_lines_register_only_unclaimed_hashes(self):
        """自注册只覆盖没被别家注册的哈希；条件行同时接受两边。"""
        needed = [PS_CLOTH_C, "f3926abc95fa801a"]
        foreign = MIRROR.efmi_scan_foreign_shader_tags([self.FOREIGN_INI], set(needed))
        to_register = [h for h in needed if h not in foreign]
        self.assertEqual(to_register, ["f3926abc95fa801a"])
        lines = MIRROR.efmi_pass_override_lines(
            "Mod_Test", MIRROR.EFMI_PASSC_FILTER_INDEX, to_register
        )
        text = "\n".join(lines)
        self.assertIn("hash = f3926abc95fa801a", text)
        self.assertIn("filter_index = 99002", text)
        self.assertIn("allow_duplicate_hash = overrule", text)
        self.assertNotIn(PS_CLOTH_C, text)


class EmitLineTests(unittest.TestCase):
    def test_mirror_block_lines(self):
        lines = MIRROR.efmi_mirror_block_lines("ps == 99002", "t0", "ResourceFoo")
        self.assertEqual(lines, ["if ps == 99002", "ps-t0 = ResourceFoo", "endif"])

    def test_override_section_names_unique_and_ascii(self):
        lines = MIRROR.efmi_pass_override_lines(
            "Mod_Test", MIRROR.EFMI_LAYER2_FILTER_INDEX, [PS_CLOTH_L2, PS_DECO_C_INK]
        )
        text = "\n".join(lines)
        self.assertIn("[ShaderOverride_PassMirror99003_ModTest_1]", text)
        self.assertIn("[ShaderOverride_PassMirror99003_ModTest_2]", text)
        self.assertEqual(text.count("filter_index = 99003"), 2)
        # 中文名退回 sha1，保证 ASCII 段名
        lines_zh = MIRROR.efmi_pass_override_lines("艾尔黛拉", 99002, [PS_BODY_C])
        self.assertIn("filter_index = 99002", "\n".join(lines_zh))
        self.assertRegex("\n".join(lines_zh), r"\[ShaderOverride_PassMirror99002_[0-9a-f]{8}\]")


class EmitSideSourceTests(unittest.TestCase):
    """发射侧接线的源码级回归（ui/universal/efmi.py）。"""

    @classmethod
    def setUpClass(cls):
        efmi_py = os.path.join(REPO_ROOT, "ui", "universal", "efmi.py")
        with open(efmi_py, "r", encoding="utf-8") as handle:
            cls.code = handle.read()

    def test_mirror_hook_in_submesh_draw_bindings(self):
        """_append_submesh_draw_bindings 必须在槽位赋值后调用镜像钩子。"""
        start = self.code.index("def _append_submesh_draw_bindings")
        end = self.code.index("def prepare_merged_skeleton")
        body = self.code[start:end]
        # 两个赋值分支（RabbitFX 非 D/L/N 与常规）都必须挂镜像钩子
        self.assertGreaterEqual(body.count("_efmi_append_pass_mirrors("), 2)
        # 钩子在常规分支的槽位赋值行之后（append 槽位赋值 → 追加镜像块）
        assign_pos = body.index('section.append(texture_markup_info.mark_slot + " = "')
        hook_pos = body.index("_efmi_append_pass_mirrors(", assign_pos)
        self.assertLess(assign_pos, hook_pos)

    def test_tag_sections_appended_once(self):
        """角色标签注册段在 generate_ini_file 末尾发射一次。"""
        self.assertIn("_efmi_append_pass_mirror_sections(ini_builder)", self.code)
        self.assertIn("efmi_pass_override_lines(", self.code)

    def test_lazy_scanners_present(self):
        self.assertIn("def _efmi_pass_layouts(", self.code)
        self.assertIn("def _efmi_foreign_ini_texts(", self.code)
        self.assertIn("efmi_scan_pass_layouts(", self.code)
        self.assertIn("efmi_scan_foreign_shader_tags(", self.code)


class PassLayoutsPersistenceTests(unittest.TestCase):
    """工作空间持久化：导入时写回 Config/PassLayouts.json，生成时读缓存。"""

    SAMPLE = {
        "d6128f13": [
            {"numviews": 0, "ps": "d7bb9dd57f5b70c6", "slots": {}},
            {"numviews": 2, "ps": "eeb2d6fb78e5a7a8", "slots": {"fcba71f5": "t13"}},
            {"numviews": 5, "ps": "78015be8a27acd24", "slots": {"fcba71f5": "t0"}},
        ],
        "650a6c6b": [
            {"numviews": 2, "ps": "de7325869b89a934", "slots": {"0e47d51d": "t14"}},
            {"numviews": 2, "ps": "d72788f1180db1fb", "slots": {"0e47d51d": "t12"}},
        ],
    }

    def _make_workspace_and_log(self, tmp):
        import tempfile
        ws = os.path.join(tmp, "ws")
        os.makedirs(os.path.join(ws, "Config"), exist_ok=True)
        log = os.path.join(tmp, "log.txt")
        with open(log, "w", encoding="utf-8") as handle:
            handle.write("000001 DrawIndexedInstanced(IndexCountPerInstance:1, InstanceCount:1, StartIndexLocation:0, BaseVertexLocation:0, StartInstanceLocation:0)\n")
        return ws, log

    def test_write_then_read_round_trip(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ws, log = self._make_workspace_and_log(tmp)
            written = MIRROR.efmi_write_pass_layouts(ws, self.SAMPLE, [log])
            self.assertTrue(written and os.path.isfile(written))
            loaded = MIRROR.efmi_read_pass_layouts(ws)
        self.assertEqual(loaded, self.SAMPLE)

    def test_read_filters_part_ibs(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ws, log = self._make_workspace_and_log(tmp)
            MIRROR.efmi_write_pass_layouts(ws, self.SAMPLE, [log])
            loaded = MIRROR.efmi_read_pass_layouts(ws, {"650a6c6b"})
        self.assertEqual(set(loaded), {"650a6c6b"})

    def test_newer_source_log_invalidates_cache(self):
        """来源日志更新（mtime 变大）→ 缓存淘汰，读侧回退实时扫描。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ws, log = self._make_workspace_and_log(tmp)
            MIRROR.efmi_write_pass_layouts(ws, self.SAMPLE, [log])
            future_ns = os.stat(log).st_mtime_ns + 10**9
            os.utime(log, ns=(future_ns, future_ns))
            self.assertEqual(MIRROR.efmi_read_pass_layouts(ws), {})

    def test_deleted_source_log_keeps_cache(self):
        """来源日志被删/挪走 → 不算失效（缓存就是为这个场景存在的）。"""
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ws, log = self._make_workspace_and_log(tmp)
            MIRROR.efmi_write_pass_layouts(ws, self.SAMPLE, [log])
            os.remove(log)
            self.assertEqual(MIRROR.efmi_read_pass_layouts(ws), self.SAMPLE)

    def test_missing_or_wrong_version_returns_empty(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            ws = os.path.join(tmp, "ws")
            self.assertEqual(MIRROR.efmi_read_pass_layouts(ws), {})
            os.makedirs(os.path.join(ws, "Config"), exist_ok=True)
            with open(MIRROR.efmi_pass_layouts_file_path(ws), "w", encoding="utf-8") as handle:
                handle.write('{"version": 999, "layouts": {}}')
            self.assertEqual(MIRROR.efmi_read_pass_layouts(ws), {})

    def test_merge_replaces_only_touched_ibs(self):
        old = {"a": [{"numviews": 2, "ps": "aaa", "slots": {}}], "b": [{"numviews": 2, "ps": "bbb", "slots": {}}]}
        new = {"b": [{"numviews": 5, "ps": "ccc", "slots": {}}], "c": [{"numviews": 2, "ps": "ddd", "slots": {}}]}
        merged = MIRROR.efmi_merge_pass_layouts(old, new)
        self.assertEqual(merged["a"][0]["ps"], "aaa")
        self.assertEqual(merged["b"][0]["ps"], "ccc")
        self.assertEqual(merged["c"][0]["ps"], "ddd")


class ImportWritebackSourceTests(unittest.TestCase):
    """导入侧写回与生成侧读缓存的源码级回归。"""

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(REPO_ROOT, "ui", "ui_func_import_ssmt.py"), "r", encoding="utf-8") as handle:
            cls.import_code = handle.read()
        with open(os.path.join(REPO_ROOT, "ui", "universal", "efmi.py"), "r", encoding="utf-8") as handle:
            cls.efmi_code = handle.read()

    def test_import_writes_layouts_at_import_time(self):
        """导入操作符必须在导入时把 pass 布局写回工作空间。"""
        self.assertIn("efmi_scan_pass_layouts(", self.import_code)
        self.assertIn("efmi_merge_pass_layouts(", self.import_code)
        self.assertIn("efmi_write_pass_layouts(", self.import_code)

    def test_generation_reads_workspace_cache_first(self):
        """生成侧 _efmi_pass_layouts 必须先读工作空间缓存，再实时扫描兜底。"""
        self.assertIn("efmi_read_pass_layouts(", self.efmi_code)
        read_pos = self.efmi_code.index("efmi_read_pass_layouts(")
        scan_pos = self.efmi_code.index("efmi_scan_pass_layouts(")
        self.assertLess(read_pos, scan_pos)


if __name__ == "__main__":
    unittest.main()
