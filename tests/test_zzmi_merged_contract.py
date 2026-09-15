"""合并骨架「导入/导出契约」判定（common/zzmi_merged_contract.py）单测。

背景（2026-09-13 实测悬案）：用户的叶瞬光工程里，合并骨架**导入段生效、导出段
没生效**，插件当时静默退回普通导出，产出的 mod 在游戏里整块不显示：

* ``叶瞬光 20260913.blend`` 的顶点组已是全局编号（只用 1 根骨头的眉毛索引 49；
  后背铃铛 3 根骨头、最大索引 179），且有 ``SkeletonGroup_0..3`` 合集；
* 同一次导出的 ini 里 ``ResourceZZMergedSkeleton`` / ``CustomShaderZZMIMergedSkeletonAttach``
  / ``ResourceZZPalette`` / ``ResourceZZVgMap`` / ``seen_`` / ``occ_`` 全是 0 次。

这份单测把判定表逐条钉住，确保这种组合**永远**走到"中止导出"，不再产出坏 mod。
"""

import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_PATH = REPO_ROOT / "common" / "zzmi_merged_contract.py"
_SPEC = importlib.util.spec_from_file_location("zzmi_merged_contract_under_test", _PATH)
assert _SPEC and _SPEC.loader
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)

evaluate = _mod.evaluate_merged_skeleton_contract
format_skip_reasons = _mod.format_skip_reasons


class MergedSkeletonContractTests(unittest.TestCase):
    def test_workspace_has_data_but_checkbox_off_is_an_error(self):
        """最要命的一种：几何是全局编号，导出却是普通模式 → 必然不显示。"""
        result = evaluate(
            checkbox_enabled=False,
            parts_with_data=14,
            component_count=0,
            skip_reasons={},
        )
        self.assertEqual(result["level"], "error")
        self.assertIn("14", result["message"])
        self.assertIn("关闭", result["message"])
        self.assertIn("重新导入", result["hint"])

    def test_workspace_has_data_and_all_components_rejected_is_an_error(self):
        result = evaluate(
            checkbox_enabled=True,
            parts_with_data=3,
            component_count=0,
            skip_reasons={
                "84618ee0": "VGMap 缓存版本 2 != 当前 3（旧策略缓存）",
                "a23aa8a3": "合并元数据含非整数/越界值（缓存损坏）",
            },
        )
        self.assertEqual(result["level"], "error")
        self.assertIn("全部被导出器拒绝", result["message"])
        # 原因必须原样出现在提示里（否则用户无法自查）
        self.assertIn("84618ee0", result["hint"])
        self.assertIn("缓存版本 2", result["hint"])
        self.assertIn("清除骨骼合并VGMap缓存", result["hint"])

    def test_partial_rejection_is_a_warning_not_an_error(self):
        result = evaluate(
            checkbox_enabled=True,
            parts_with_data=4,
            component_count=2,
            skip_reasons={"b20f90ea": "VGMap 未完整覆盖 0..50（缺失 [7]，多余 []，偏移 154，分组 1）"},
        )
        self.assertEqual(result["level"], "warning")
        self.assertIn("不完整", result["message"])
        self.assertIn("b20f90ea", result["hint"])

    def test_plain_export_without_any_merged_data_is_not_an_error(self):
        """普通工程（从没做过合并导入）必须照常导出，只是给一句提示。"""
        result = evaluate(
            checkbox_enabled=True,
            parts_with_data=0,
            component_count=0,
            skip_reasons={},
        )
        self.assertEqual(result["level"], "notice")
        self.assertNotEqual(result["level"], "error")
        self.assertIn("普通导出", result["message"])

    def test_healthy_merge_is_ok(self):
        result = evaluate(
            checkbox_enabled=True,
            parts_with_data=3,
            component_count=3,
            skip_reasons={},
        )
        self.assertEqual(result["level"], "ok")
        self.assertEqual(result["message"], "")

    def test_format_skip_reasons_is_bounded(self):
        reasons = {f"ib{i:02d}": f"原因{i}" for i in range(9)}
        text = format_skip_reasons(reasons, limit=6)
        self.assertIn("另有 3 个部件", text)
        self.assertNotIn("ib08", text)
        self.assertEqual(format_skip_reasons({}), "")


if __name__ == "__main__":
    unittest.main()
