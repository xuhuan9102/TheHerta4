"""M1-3 运行期 INI 文本单测（common/softbody_runtime_ini.py）。

这一层是"求解器怎么接进 mod"的契约。三个点必须钉死：

1. 资源声明的**类型/格式**要对（邻接表用 R32G32B32A32_UINT 格式化缓冲，
   与 vg_map 同源做法；状态用 RWStructuredBuffer，array = 顶点数 × 2）；
2. Dispatch 组数 = ceil(顶点数/64)，必须与着色器里的 [numthreads(64,1,1)] 对应；
3. vb0 必须被指向**求解器输出**，而不是原顶点缓冲（否则等于没解算）。
"""

import importlib.util
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_PATH = REPO_ROOT / "common" / "softbody_runtime_ini.py"
_SPEC = importlib.util.spec_from_file_location("softbody_runtime_ini_under_test", _PATH)
assert _SPEC and _SPEC.loader
_mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(_mod)

build_resource_lines = _mod.build_resource_lines
build_solver_section_lines = _mod.build_solver_section_lines
build_deform_hook_lines = _mod.build_deform_hook_lines
dispatch_groups = _mod.dispatch_groups


class DispatchTests(unittest.TestCase):
    def test_groups_round_up(self):
        self.assertEqual(dispatch_groups(0), 0)
        self.assertEqual(dispatch_groups(1), 1)
        self.assertEqual(dispatch_groups(64), 1)
        self.assertEqual(dispatch_groups(65), 2)

    def test_real_part_sizes(self):
        # 真实导出：身体 10877、脸 2791
        self.assertEqual(dispatch_groups(10877), 170)
        self.assertEqual(dispatch_groups(2791), 44)


class ResourceLineTests(unittest.TestCase):
    def test_adjacency_and_edges_use_formatted_buffer(self):
        lines = build_resource_lines("c209c22b", 10877, 25663)
        text = "\n".join(lines)
        self.assertIn("[ResourceSB_Adj_c209c22b]", text)
        self.assertIn("[ResourceSB_Edges_c209c22b]", text)
        self.assertIn("format = R32G32B32A32_UINT", text)
        self.assertIn("filename = Meshes/sb_adj_c209c22b.buf", text)
        self.assertIn("filename = Meshes/sb_edges_c209c22b.buf", text)

    def test_state_array_is_sixfold_vertex_count(self):
        lines = build_resource_lines("c209c22b", 10877, 25663)
        text = "\n".join(lines)
        self.assertIn("[ResourceSB_State_c209c22b]", text)
        self.assertIn("type = RWStructuredBuffer", text)
        # 每顶点 3 条 float4：基座 / 当前位置 / 上一步位置
        self.assertIn("array = 65262", text)  # 10877 * 6（3 条 × 2 个出现次槽）

    def test_solver_target_is_bare_rwbuffer_like_drag_system(self):
        """写入目标必须与拖拽系统的 ResourceDragJiggleTempVB0 完全同构：
        裸 `type = RWBuffer`，不写 stride/format/array。"""
        text = "\n".join(build_resource_lines("c209c22b", 10877, 25663))
        self.assertIn("[ResourceSB_VB_c209c22b]", text)
        self.assertIn("type = RWBuffer", text)
        # 这一段里不允许出现 stride/array（那是 RWStructuredBuffer 的写法，实机碎一地）
        block = text.split("[ResourceSB_VB_c209c22b]")[1].split("[")[0]
        self.assertNotIn("stride", block)
        self.assertNotIn("array", block)


class SolverSectionTests(unittest.TestCase):
    def test_bindings_and_dispatch(self):
        lines = build_solver_section_lines("c209c22b", 10877)
        text = "\n".join(lines)
        self.assertIn("[CustomShaderSB_Solve_c209c22b]", text)
        self.assertIn("cs = ./res/sb_solve.hlsl", text)
        # 四个资源槽必须与 sb_solve.hlsl 的 register 声明一一对应
        # t24 必须直接读 IA 的 vb0（照抄拖拽系统那套跑通的写法），
        # 不是 ref 我们自己的资源 —— 后者实机"碎一地"（2026-09-14 实证）。
        self.assertIn("cs-t24 = ref ResourceSB_Base_c209c22b", text)
        # vb0（mod 的 Position 缓冲）必须【只读】：一旦就地写回，第二次 deform
        # 抄到的基座就是被改过的 → 实机"面筋人"
        self.assertIn("ResourceSB_VB_c209c22b = vb0", text)
        self.assertIn("cs-t80 = ref ResourceSB_Adj_c209c22b", text)
        self.assertIn("cs-t81 = ref ResourceSB_Edges_c209c22b", text)
        # 先别名到 IA 的 vb0，再拷其可写副本（拖拽系统 3888~3895 行原样）
        self.assertIn("ResourceSB_VB_c209c22b = vb0", text)
        self.assertIn("cs-u5 = copy ResourceSB_VB_c209c22b", text)
        self.assertIn("cs-u6 = ref ResourceSB_State_c209c22b", text)
        self.assertIn("Dispatch = 170, 1, 1", text)
        # Dispatch 期间解绑 vb0，然后把副本写回（= 写回游戏绑的那个缓冲）
        self.assertIn("vb0 = null", text)
        self.assertIn("ResourceSB_VB_c209c22b = copy cs-u5", text)
        self.assertIn("cs-u5 = null", text)
        self.assertIn("cs-u6 = null", text)

    def test_empty_part_produces_no_section(self):
        self.assertEqual(build_solver_section_lines("x", 0), [])


class DeformHookTests(unittest.TestCase):
    def test_hook_runs_solver_then_rebinds_vb0(self):
        lines = build_deform_hook_lines("c209c22b")
        text = "\n".join(lines)
        self.assertIn("run = CustomShaderSB_Solve_c209c22b", text)
        # 定稿（照抄拖拽系统 4709 行）：解算之后**必须换绑 vb0** 到资源。
        self.assertIn("vb0 = ResourceSB_VB_c209c22b", text)
        self.assertLess(text.index("run = "), text.index("vb0 = "))

    def test_hook_safety_copy_covers_solver_failure(self):
        """保底语义：解算挂钩本身不再有多余动作，解算没跑 = 画面不变。"""
        text = "\n".join(build_deform_hook_lines("c209c22b"))
        body = [l for l in text.splitlines() if l.strip() and not l.strip().startswith(";")]
        self.assertEqual(len(body), 2)
        self.assertIn("run = CustomShaderSB_Solve_c209c22b", text)


if __name__ == "__main__":
    unittest.main()



