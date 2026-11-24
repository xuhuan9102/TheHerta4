import unittest
from collections import OrderedDict
from types import SimpleNamespace

from blueprint import deform_chain


def test_bone_matrix_nodes_plan_their_draw_dependencies():
    suffixes = ("FluidProbe", "DragInteraction", "AnimDriver", "BoneMatrix", "Material", "ShapeKey")
    nodes = [SimpleNamespace(bl_idname="SSMTNode_PostProcess_" + suffix) for suffix in suffixes]
    ordered = deform_chain.order_bone_matrix_nodes(nodes)
    assert ordered == [nodes[i] for i in (5, 4, 2, 3, 1, 0)]


def _sections_with_multifile():
    """构造一份含多文件段的 ini sections（协议 v3 改造前的形态）。"""
    return OrderedDict([
        ("[Constants]", [
            "global persist $swapkey100 = 0",
            "global persist $active0 = 0",
            "post Resource_abc123_Position = copy_desc Resource_abc123_Position_1",
            "post run = CustomShader_abc123_base_1Anim",
        ]),
        ("[Resource_abc123_Position]", [
            "type = Buffer",
            "stride = 40",
            "filename = Meshes01/abc123-Position.buf",
        ]),
        ("[Resource_abc123_Position_1]", [
            "type = Buffer",
            "stride = 40",
            "filename = Meshes01/abc123-Position.buf",
        ]),
        ("[CustomShader_abc123_base_1Anim]", [
            "    cs = ./res/merge_anim_packed_delta.hlsl",
            "    cs-u5 = copy Resource_abc123_Position_1",
            "    Resource_abc123_Position = ref cs-u5",
            "    Dispatch = 100, 1, 1",
            "    cs-u5 = null",
        ]),
        ("[Present]", [
            "if $active0 == 1",
            "    run = CustomShader_abc123_base_1Anim",
            "endif",
        ]),
    ])


class DeformChainAnchorTests(unittest.TestCase):
    """锚点别名 _1 → _0 迁移与输出双写。"""

    def test_migrate_anchor_and_dual_write(self):
        lines, changed, base = deform_chain.rewrite_multifile_shader_lines([
            "    cs-u5 = copy Resource_abc123_Position_1",
            "    Resource_abc123_Position = ref cs-u5",
        ])
        self.assertTrue(changed)
        self.assertEqual(base, "Resource_abc123_Position")
        joined = "\n".join(lines)
        self.assertIn("cs-u5 = copy Resource_abc123_Position_0", joined)
        self.assertIn("Resource_abc123_Position_mf = ref cs-u5", joined)
        self.assertIn("Resource_abc123_Position = ref cs-u5", joined)
        # 幂等：再改一次不再变化
        lines2, changed2, _ = deform_chain.rewrite_multifile_shader_lines(lines)
        self.assertFalse(changed2)
        self.assertEqual(lines, lines2)


class DeformChainPresentBlockTests(unittest.TestCase):
    """多文件 run 行迁入带 mf_ran 标志的激活块。"""

    def test_ensure_block_builds_mf_ran_flags(self):
        present = []
        out = deform_chain.ensure_multifile_present_block(
            present, "$active0", 1,
            ["$ssmt_mf_ran_Resource_abc123_Position"],
            ["run = CustomShader_abc123_base_1Anim"],
        )
        text = "\n".join(out)
        self.assertIn("$ssmt_mf_ran_Resource_abc123_Position = 1", text)
        self.assertIn("run = CustomShader_abc123_base_1Anim", text)
        self.assertIn("else", text)
        self.assertIn("$ssmt_mf_ran_Resource_abc123_Position = 0", text)
        # 幂等
        out2 = deform_chain.ensure_multifile_present_block(
            out, "$active0", 1,
            ["$ssmt_mf_ran_Resource_abc123_Position"],
            ["run = CustomShader_abc123_base_1Anim"],
        )
        self.assertEqual(out, out2)


class FinalizeDeformChainTests(unittest.TestCase):
    """终态规整：条件锚定、接力块排序、复位去重、_mf 声明、post run 移除。"""

    def _build(self):
        return _sections_with_multifile()

    def test_rank_run_classification(self):
        self.assertEqual(
            deform_chain.classify_chain_run("    run = CustomShader_abc123_base_1Anim"),
            (deform_chain.RANK_MULTIFILE, "abc123_base"),
        )
        self.assertEqual(
            deform_chain.classify_chain_run("run = CustomShader_abcdef12_Anim"),
            (deform_chain.RANK_SHAPEKEY, "abcdef12"),
        )
        self.assertIsNone(deform_chain.classify_chain_run("run = CommandListFoo"))

    def test_finalize_is_idempotent_and_orders_runs(self):
        sections = self._build()
        # 加一个形态键段（锚定 _0），模拟混合场景
        sections["[CustomShader_abcdef12_Anim]"] = [
            "    cs = ./res/shapekey_anim.hlsl",
            "    cs-u5 = copy Resource_abc123_Position_0",
            "    Resource_abc123_Position = ref cs-u5",
            "    Dispatch = 50, 1, 1",
        ]
        sections["[Present]"].append("    run = CustomShader_abcdef12_Anim")

        deform_chain.finalize_deform_chain(sections)

        present_text = "\n".join(sections["[Present]"])
        # 接力块存在，rank10 在 rank20 前
        self.assertIn(deform_chain.CHAIN_BEGIN, present_text)
        mf_idx = present_text.find("run = CustomShader_abc123_base_1Anim")
        sk_idx = present_text.find("run = CustomShader_abcdef12_Anim")
        self.assertTrue(0 < mf_idx < sk_idx)

        # 形态键条件锚定（检测到 rank10 段）
        sk_text = "\n".join(sections["[CustomShader_abcdef12_Anim]"])
        self.assertIn("$ssmt_mf_ran_Resource_abc123_Position == 1", sk_text)
        self.assertIn("cs-u5 = copy Resource_abc123_Position_mf", sk_text)

        # _mf 空声明资源段
        self.assertIn("[Resource_abc123_Position_mf]", sections)

        # post run 移除、复位行去重
        const_text = "\n".join(sections["[Constants]"])
        self.assertNotIn("post run = CustomShader", const_text)
        self.assertEqual(const_text.count("post Resource_abc123_Position = copy_desc"), 1)

        # mf_ran 声明
        self.assertIn("global persist $ssmt_mf_ran_Resource_abc123_Position = 0", const_text)

        # 幂等：再 finalize 一次结果不变
        snapshot = {k: list(v) for k, v in sections.items()}
        deform_chain.finalize_deform_chain(sections)
        for k, v in snapshot.items():
            self.assertEqual(sections.get(k), v, f"段 {k} 在二次 finalize 后发生变化")

    def test_shapekey_alone_anchor_unchanged(self):
        """多文件缺席时，形态键直读 _0，不做条件锚定。"""
        sections = OrderedDict([
            ("[CustomShader_abcdef12_Anim]", [
                "    cs-u5 = copy Resource_abc123_Position_0",
                "    Resource_abc123_Position = ref cs-u5",
            ]),
            ("[Present]", ["    run = CustomShader_abcdef12_Anim"]),
        ])
        deform_chain.finalize_deform_chain(sections)
        sk_text = "\n".join(sections["[CustomShader_abcdef12_Anim]"])
        self.assertIn("cs-u5 = copy Resource_abc123_Position_0", sk_text)
        self.assertNotIn("_mf", sk_text)

    def test_intermediate_resource_inherits_non_default_stride(self):
        sections = self._build()
        sections["[Resource_abc123_Position]"] = [
            "type = Buffer",
            "stride = 24",
            "filename = Meshes01/abc123-Position.buf",
        ]
        sections["[Resource_abc123_Position_1]"] = [
            "type = Buffer",
            "stride = 24",
            "filename = Meshes01/abc123-Position.buf",
        ]

        deform_chain.finalize_deform_chain(sections)

        self.assertEqual(
            sections["[Resource_abc123_Position_mf]"],
            ["type = Buffer", "stride = 24"],
        )

    def test_multifile_chain_runs_before_guarded_direct_shapekey_block(self):
        sections = self._build()
        sections["[CustomShader_shape_Anim]"] = [
            "    cs-u5 = copy Resource_abc123_Position_0",
            "    Resource_abc123_Position = ref cs-u5",
        ]
        sections["[Present]"].extend([
            "; --- SSMT DIRECT SHAPEKEY PRESENT BEGIN ---",
            "if $active0 == 1",
            "    run = CustomShader_shape_Anim",
            "endif",
            "; --- SSMT DIRECT SHAPEKEY PRESENT END ---",
        ])

        deform_chain.finalize_deform_chain(sections)

        present = "\n".join(sections["[Present]"])
        self.assertLess(
            present.index("run = CustomShader_abc123_base_1Anim"),
            present.index("run = CustomShader_shape_Anim"),
        )
        self.assertIn("if $active0 == 1\n    run = CustomShader_shape_Anim\nendif", present)

    def test_unrelated_post_custom_shader_is_preserved(self):
        sections = self._build()
        sections["[Constants]"].append("post run = CustomShader_ResetResources")

        deform_chain.finalize_deform_chain(sections)

        self.assertIn(
            "post run = CustomShader_ResetResources",
            sections["[Constants]"],
        )


if __name__ == "__main__":
    unittest.main()
