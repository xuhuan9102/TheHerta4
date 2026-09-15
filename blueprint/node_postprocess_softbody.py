"""软体物理节点（M1 第一版：只写数据、只声明资源，不改画面）。

设计立场：**新节点，不动拖拽节点**。

* 拖拽节点（`node_postprocess_draginteraction.py`）那套是"每个顶点一根独立弹簧"，
  5803 行、用户正在用；软体需要的是完全不同的导出物（邻接网 + 跨帧状态），
  塞进去只会互相拖累。
* 两者可以共存，但同一个物体只能被一个接管（本节点会点名报告重复）。

执行时机与别的后处理节点一致：`execute_postprocess(mod_export_path)` —— ini 已经
生成完毕，本节点负责

1. 读**已经导出**的 `Meshes/<ib>-Position.buf` 与 `LOD0.<ib>-*Index.buf`，
   烘出 `sb_adj_<ib>.buf` / `sb_edges_<ib>.buf`；
2. 往每个 ini 末尾插入资源声明与求解器段（`softbody_runtime_ini`）。

v1 默认 **`enable_solve = False`**：只做 1+2，**不碰 deform 段、不换 vb0**。
所以装上后画面应该与现在**完全一致** —— 先确认没破坏任何东西，再把开关打开，
那时才第一次出现"皮肤感"。
"""

import glob
import importlib.util
import os
import re
from pathlib import Path

import bpy

from .node_postprocess_base import SSMTNode_PostProcess_Base


def _load_common_module(name: str):
    """按路径加载 common/ 下的模块（与仓库里其它节点的加载方式一致）。"""
    path = Path(__file__).resolve().parents[1] / "common" / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"softbody_{name}", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SSMTNode_PostProcess_SoftBody(SSMTNode_PostProcess_Base):
    bl_idname = "SSMTNode_PostProcess_SoftBody"
    bl_label = "软体物理"
    bl_description = (
        "把网格变成真正的软体：顶点之间有邻接网 + 跨帧惯性，不再是一堆独立弹簧点。"
        "M1 第一版只烘焙数据与声明资源（画面不变）"
    )

    target_draw_ibs: bpy.props.StringProperty(
        name="目标 DrawIB",
        description="要施加软体的部件，逗号分隔（如 c209c22b,4a178546）。留空 = Meshes 里找到的全部部件",
        default="",
    )  # type: ignore

    shape_stiffness: bpy.props.FloatProperty(
        name="静止姿态拉力",
        description="把顶点拉回绑定姿态的强度：0 = 完全自由（会瘫），1 = 钉死（不动）。0.05~0.3 比较像肉",
        default=0.15, min=0.0, max=1.0,
    )  # type: ignore

    edge_stiffness: bpy.props.FloatProperty(
        name="邻接刚度",
        description="邻接边长约束的强度：越大越弹、越像橡皮；越小越像面团",
        default=0.5, min=0.0, max=1.0,
    )  # type: ignore

    damping: bpy.props.FloatProperty(
        name="阻尼",
        description="每步保留多少速度：0.9~0.99。越大回弹越持久（像果冻），越小越快停（像肥肉）",
        default=0.94, min=0.0, max=0.999,
    )  # type: ignore

    iterations: bpy.props.IntProperty(
        name="迭代次数",
        description="每个 dispatch 内扫几遍邻居：1~4。越大越稳、越贵",
        default=2, min=1, max=8,
    )  # type: ignore

    gravity_strength: bpy.props.FloatProperty(
        name="重力强度",
        description="M2 才真正生效（世界重力换到物空间）",
        default=0.0, min=0.0, max=10.0,
    )  # type: ignore

    enable_solve: bpy.props.BoolProperty(
        name="启用解算（会改变画面）",
        description="关（默认）= 只烘焙数据与声明资源，画面与现在完全一致；开 = 接进 deform 段，vb0 换成解算结果",
        default=False,
    )  # type: ignore

    debug_marker: bpy.props.BoolProperty(
        name="诊断：整体位移标记",
        description=(
            "只用于排查：勾上后解算不写物理结果，改把整个部件沿 Z 平移 0.5。\n"
            "模型整体位移 = 解算在跑且读到了正确顶点（问题在物理数学）；\n"
            "还是碎一地 = 解算在跑但读写是垃圾（问题在绑定/布局）；\n"
            "毫无变化 = 解算根本没跑（着色器没编译或被丢弃）。"
        ),
        default=False,
    )  # type: ignore

    def draw_buttons(self, context, layout):
        layout.label(text="软体物理（M1）", icon='PHYSICS')
        layout.prop(self, "target_draw_ibs")
        layout.prop(self, "shape_stiffness")
        layout.prop(self, "edge_stiffness")
        layout.prop(self, "damping")
        layout.prop(self, "iterations")
        layout.prop(self, "gravity_strength")
        layout.separator()
        layout.prop(self, "enable_solve")
        layout.prop(self, "debug_marker")
        if not self.enable_solve:
            layout.label(text="当前：只烘焙数据，画面不变", icon='INFO')

    # ------------------------------------------------------------------

    def _target_draw_ibs(self, meshes_dir: str):
        raw = str(self.target_draw_ibs or "")
        wanted = [
            chunk.strip()
            for chunk in raw.replace("，", ",").replace(";", ",").split(",")
            if chunk.strip()
        ]
        if wanted:
            return wanted
        found = []
        for path in sorted(glob.glob(os.path.join(meshes_dir, "*-Position.buf"))):
            found.append(os.path.basename(path)[: -len("-Position.buf")])
        return found

    def execute_postprocess(self, mod_export_path):
        bake = _load_common_module("softbody_bake")
        runtime = _load_common_module("softbody_runtime_ini")

        meshes_dir = os.path.join(mod_export_path, "Meshes")
        if not os.path.isdir(meshes_dir):
            print(f"[软体M1] 找不到 {meshes_dir}，跳过")
            return

        targets = self._target_draw_ibs(meshes_dir)
        if not targets:
            print("[软体M1] 没有目标部件，跳过")
            return

        baked = []
        for draw_ib in targets:
            position_path = os.path.join(meshes_dir, f"{draw_ib}-Position.buf")
            index_paths = sorted(
                glob.glob(os.path.join(meshes_dir, f"LOD0.{draw_ib}-*Index.buf"))
            )
            if not os.path.isfile(position_path) or not index_paths:
                print(f"[软体M1] {draw_ib}: 缺 Position 或 Index，跳过")
                continue
            try:
                with open(position_path, "rb") as handle:
                    position_payload = handle.read()
                index_payload = b"".join(
                    open(path, "rb").read() for path in index_paths
                )
                adj_bytes, edge_bytes, report = bake.bake_softbody_adjacency(
                    position_payload, 40, index_payload, "u32"
                )
            except Exception as exc:  # noqa: BLE001 - 单个部件失败不该毁掉整个导出
                print(f"[软体M1] {draw_ib}: 烘焙失败 {exc}")
                continue

            if not report["ok"]:
                print(bake.format_report(report, draw_ib))
                continue

            with open(os.path.join(meshes_dir, f"sb_adj_{draw_ib}.buf"), "wb") as handle:
                handle.write(adj_bytes)
            with open(os.path.join(meshes_dir, f"sb_edges_{draw_ib}.buf"), "wb") as handle:
                handle.write(edge_bytes)
            # 基座几何的只读副本：把导出的 Position 缓冲原样复制一份。
            # 解算器从这份读基座，因此"就地写回 Position 缓冲"不会污染基座
            # （否则第二次 deform 会以被改过的数据为基座 → 实机"面筋人"）。
            with open(os.path.join(meshes_dir, f"sb_base_{draw_ib}.buf"), "wb") as handle:
                handle.write(position_payload)

            baked.append((draw_ib, report))
            print(bake.format_report(report, draw_ib))

        if not baked:
            print("[软体M1] 没有任何部件烘焙成功")
            return

        # 把求解器着色器复制进 mod 的 res/（2026-09-14 修复）：
        # ini 里写的是 `cs = ./res/sb_solve.hlsl`，文件不在 → 3Dmigoto 加载失败、
        # `run =` 被丢弃 → 解算一次都不跑，画面自然"没有任何变化"。
        # 合并骨架的 attach 着色器走的就是这条通道（_copy_merged_skeleton_shader_to_mod）。
        self._copy_solver_shader(mod_export_path)

        # 往每个 ini 末尾插入资源声明 + 求解器段
        params = {
            "shape_stiffness": self.shape_stiffness,
            "edge_stiffness": self.edge_stiffness,
            "damping": self.damping,
            "gravity_strength": self.gravity_strength,
            "iterations": self.iterations,
            "debug_marker": self.debug_marker,
        }
        for ini_path in glob.glob(os.path.join(mod_export_path, "**", "*.ini"), recursive=True):
            block = []
            for draw_ib, report in baked:
                block += runtime.build_resource_lines(
                    draw_ib, report["vertex_count"], report["edge_count"]
                )
                block += runtime.build_solver_section_lines(
                    draw_ib, report["vertex_count"], params
                )
            try:
                with open(ini_path, "r", encoding="utf-8-sig", newline="") as handle:
                    content = handle.read()
                if self.enable_solve:
                    content = self._insert_deform_hooks(
                        content, [draw_ib for draw_ib, _r in baked], runtime
                    )
                # 2026-09-14 修复：声明必须**排在引用它的 run 之前**。
                # 实测证据：合并骨架（能跑）的段定义在 378 行、run 在 901 行；
                # 我原来把声明追加到文件末尾 ⇒ run(1286) 在段之前 ⇒ 解算一次都没执行。
                # 这里改成插在**第一个 `[TextureOverride` 段之前**。
                lines = content.split("\n")
                anchor = len(lines)
                for index, line in enumerate(lines):
                    if line.strip().startswith("[TextureOverride"):
                        anchor = index
                        break
                header = ["; ===== 软体物理 M1（资源与求解器声明，必须在 run 之前）====="]
                lines[anchor:anchor] = header + block
                with open(ini_path, "w", encoding="utf-8", newline="") as handle:
                    handle.write("\n".join(lines))
            except OSError as exc:
                print(f"[软体M1] 写入 {ini_path} 失败: {exc}")

        total_v = sum(report["vertex_count"] for _ib, report in baked)
        print(
            f"[软体M1] 完成：{len(baked)} 个部件，顶点 {total_v}，"
            f"解算开关 {'开' if self.enable_solve else '关（画面不变）'}"
        )

    # ------------------------------------------------------------------

    def _hook_lines_with_slot(self, draw_ib, runtime, seen_lines):
        """生成挂钩行，并自动带上"按出现次选槽"。

        出现次变量不用我另造：合并骨架在同一个 deform 段里已经发过
        `$zz_ms_occ_<组件号> = $zz_ms_occ_<组件号> + 1`，我在这段已经输出的行里
        往回找最近的那个变量名即可。
        """
        occ_var = ""
        for line in reversed(list(seen_lines)):
            match = re.search(r"(\$zz_ms_occ_\d+)", line)
            if match:
                occ_var = match.group(1)
                break
        if not occ_var:
            print(f"[软体M1] 提示：{draw_ib} 没找到出现次变量，槽位固定用 0（单槽行为）")
        return runtime.build_deform_hook_lines_for(
            draw_ib, marker=bool(self.debug_marker), occ_var=occ_var
        )

    def _copy_solver_shader(self, mod_export_path):
        """把 Toolset/soft_body/sb_solve.hlsl 复制到 <mod>/res/sb_solve.hlsl。"""
        source = (
            Path(__file__).resolve().parents[1]
            / "Toolset" / "soft_body" / "sb_solve.hlsl"
        )
        if not source.is_file():
            print(f"[软体M1] 找不到求解器着色器 {source}")
            return
        target_dir = os.path.join(mod_export_path, "res")
        os.makedirs(target_dir, exist_ok=True)
        target = os.path.join(target_dir, "sb_solve.hlsl")
        try:
            with open(source, "rb") as src_handle, open(target, "wb") as dst_handle:
                dst_handle.write(src_handle.read())
            print(f"[软体M1] 已复制求解器着色器 -> {target}")
        except OSError as exc:
            print(f"[软体M1] 复制着色器失败: {exc}")

    def _insert_deform_hooks(self, content: str, draw_ibs, runtime) -> str:
        """把解算挂进每个部件的 deform 顶点覆写段。

        锚点：`[TextureOverride_VB_<ib>_..._Position]` 段里、**插件自己的
        `vb0 = Resource<ib>Position` 之后**、`handling = skip` / draw 之前。

        2026-09-14 修复：一开始插在 `hash =` 后面，结果被插件原本的
        `vb0 = Resource<ib>Position`（更靠后）覆盖掉 —— 解算了但用不上，
        现象就是"导出后没有任何变化"。所以这里必须插在**所有 vb 绑定之后**。

        找不到对应段就跳过并报告 —— 不猜、不硬塞。
        """
        lines = content.splitlines()
        out: list[str] = []
        inserted = set()
        pending = None
        for line in lines:
            stripped = line.strip()
            if stripped.startswith("[TextureOverride_VB_"):
                # 进入新段：若上一个待处理的段没找到锚点，直接补写（不丢内容）
                if pending:
                    print(f"[软体M1] 警告：{pending} 的 deform 段没找到插入点，未挂解算")
                    pending = None
                for draw_ib in draw_ibs:
                    if stripped.startswith(f"[TextureOverride_VB_{draw_ib}_") and stripped.endswith("_Position]"):
                        pending = draw_ib
                        break
                out.append(line)
                continue
            # 锚点优先级：handling / draw / 末尾 → 也就是"所有 vb 绑定之后"
            if pending and (
                stripped.startswith("handling =")
                or stripped.startswith("draw =")
                or stripped.startswith("drawindexed =")
            ):
                out.extend(self._hook_lines_with_slot(pending, runtime, out))
                inserted.add(pending)
                pending = None
            out.append(line)
        if pending:
            print(f"[软体M1] 警告：{pending} 的 deform 段没找到插入点，未挂解算")
        missing = [draw_ib for draw_ib in draw_ibs if draw_ib not in inserted]
        if missing:
            print(f"[软体M1] 警告：这些部件没找到 deform 段，未挂解算：{missing}")
        else:
            print(f"[软体M1] 已挂解算：{sorted(inserted)}")
        return "\n".join(out)


classes = (SSMTNode_PostProcess_SoftBody,)


def register():
    for cls in classes:
        bpy.utils.register_class(cls)


def unregister():
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
