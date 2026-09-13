"""Blender regression test: 工具集「顶点组分区拆分（实验）」的记录/合并/拆分保真度.

运行方式（需本机装有 headless 可用的 Blender）:
    blender --background --factory-startup --python-exit-code 1 \
        --python tests/blender_regression_vg_partition_split.py

背景:
    ``toolkit/vg_partition_split.py`` 由独立插件 Vertex Group Partition Splitter
    v1.1.0 原样迁入。该插件的核心承诺是「拆分后保留组的权重不变」，而实现依赖
    两个容易在迁移/重构中被破坏的细节：

      * 合并前必须先把所有组名搬进临时命名空间，否则 Blender 对同物体重名会自动
        加 ``.001`` 后缀，改名静默失败；
      * 拆分时必须「快照权重 → 删除全部组 → 按来源原名重建 → 逐顶点回写」，
        因为去重后合并组数 ≠ 各来源组数之和，任何基于数量的映射都是错的。

    另有两条工具集接入约束（本测试一并守护）:
      * ``bl_parent_id`` 在 ``register_class`` 时就会被 Blender 校验，子面板必须先
        注册父面板，否则报 "parent ... not found"；
      * Scene 属性的注册/注销必须幂等，工具集重载时会被反复调用。

本测试断言：
  A) 注册：算子/属性可用；属性注销后无残留且可重复调用                  == PASS
  B) NONE 模式：两个互不相邻物体（同名组）合并后组数=4、名序正确        == PASS
  C) NONE 模式：拆分回两个物体，各自组名与逐顶点权重与来源逐值相等      == PASS
     （这是「权重不变」的直接证据：拆分物 A 的组只在 A 的顶点上有权重，
       B 的顶点上为 0，且数值与来源完全一致）
  D) SEAM 模式：相邻接缝上 A.1 与 B.3 剖面一致时被去重（合并后 2 组，
     NONE 下同几何为 4 组），拆分后各自还原来源原名                      == PASS
  E) DIFFUSION 模式：分离表面可分析、写出中文审计报告                   == PASS

退出码 0 当且仅当全部断言通过；脚本异常一律以 1 退出。
"""
import importlib.util
import sys
import traceback
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

import bpy

MODULE_PATH = REPO_ROOT / "toolkit" / "vg_partition_split.py"
SPEC_NAME = "_vg_partition_split_check.vg_partition_split"

_spec = importlib.util.spec_from_file_location(SPEC_NAME, str(MODULE_PATH))
vgs = importlib.util.module_from_spec(_spec)
sys.modules[SPEC_NAME] = vgs
_spec.loader.exec_module(vgs)

FAILURES = []

OP_NAMES = (
    "vgsplit_record", "vgsplit_record_and_merge", "vgsplit_split",
    "vgsplit_clear", "vgsplit_analyze_diffusion",
)


def check(label, ok, detail=""):
    print(f"  [{'PASS' if ok else 'FAIL'}] {label}{(' -> ' + detail) if detail else ''}", flush=True)
    if not ok:
        FAILURES.append(f"{label} {detail}".strip())
    return ok


# ---------------------------------------------------------------- helpers

def reset_scene():
    bpy.ops.wm.read_factory_settings(use_empty=True)


def make_grid(name, location, subdivisions=3):
    """3x3 网格，size=2 → 局部 x,y ∈ {-1,0,1}。"""
    bpy.ops.mesh.primitive_grid_add(
        x_subdivisions=subdivisions, y_subdivisions=subdivisions, size=2, location=location
    )
    obj = bpy.context.active_object
    obj.name = name
    obj.data.name = name
    return obj


def assign(obj, group_name, weight_of):
    """按世界坐标赋权；返回 0 的顶点不写权重。"""
    group = obj.vertex_groups.new(name=group_name)
    for vertex in obj.data.vertices:
        weight = weight_of(obj.matrix_world @ vertex.co)
        if weight > 0.0:
            group.add([vertex.index], weight, "REPLACE")
    return group


def rows(obj):
    """[(世界坐标四舍五入, {组名: 权重}), ...]，便于按位置断言。"""
    index_names = {group.index: group.name for group in obj.vertex_groups}
    result = []
    for vertex in obj.data.vertices:
        co = obj.matrix_world @ vertex.co
        weights = {
            index_names[a.group]: round(a.weight, 6)
            for a in vertex.groups
            if a.group in index_names and a.weight > 0.0
        }
        result.append(((round(co.x, 4), round(co.y, 4), round(co.z, 4)), weights))
    return result


def weight_at(obj, position, group_name):
    for co, weights in rows(obj):
        if co == position:
            return weights.get(group_name, 0.0)
    raise AssertionError(f"{obj.name} 中不存在坐标 {position} 的顶点")


def select_only(objects, active):
    bpy.ops.object.select_all(action="DESELECT")
    for obj in objects:
        obj.select_set(True)
    bpy.context.view_layer.objects.active = active


def group_names(obj):
    return [group.name for group in obj.vertex_groups]


class StubToolkitParent(bpy.types.Panel):
    """占位父面板：真实工具集中由 toolkit/ui_panel_toolkit.py 的 ToolkitPanel 提供。"""

    bl_label = "工具集"
    bl_idname = "VIEW3D_PT_Herta_Toolkit_Panel"
    bl_space_type = "VIEW_3D"
    bl_region_type = "UI"
    bl_category = "TheHerta4"

    def draw(self, context):
        pass


# ---------------------------------------------------------------- cases

def case_register():
    print("== A: 注册 / 注销 ==", flush=True)
    # 子面板必须先于注册父面板 —— 与 toolkit/__init__.py 的注册顺序一致。
    bpy.utils.register_class(StubToolkitParent)
    for cls in vgs.vg_partition_split_operators + vgs.vg_partition_split_panels:
        bpy.utils.register_class(cls)
    vgs.register_vg_partition_split_properties()

    # 注意：只能用 "name" in dir(bpy.ops.toolkit) 判断算子是否注册。
    # hasattr() 对任意名字恒为 True（bpy.ops 子模块会惰性造出包装器，
    # 未注册的算子要到调用时才报 "could not be found"），拿它做断言等于没查。
    check("算子已注册", all(n in dir(bpy.ops.toolkit) for n in OP_NAMES),
          str([n for n in OP_NAMES if n not in dir(bpy.ops.toolkit)]))
    check("Scene 属性已注册",
          all(hasattr(bpy.types.Scene, n) for n in vgs.SETTING_NAMES)
          and hasattr(bpy.types.Scene, "vgsplit_manifest"))
    check("面板为工具集顶层子面板（与高斯权重球并列）",
          vgs.VGSPLIT_PT_panel.bl_parent_id == "VIEW3D_PT_Herta_Toolkit_Panel"
          and vgs.VGSPLIT_PT_panel.bl_category == "TheHerta4"
          and vgs.VGSPLIT_PT_panel.bl_order == 102,
          f"parent={vgs.VGSPLIT_PT_panel.bl_parent_id} order={vgs.VGSPLIT_PT_panel.bl_order}")


def case_none_mode_fidelity():
    print("== B/C: NONE 模式权重保真 ==", flush=True)
    reset_scene()
    obj_a = make_grid("SrcA", (0.0, 0.0, 0.0))     # 世界 x ∈ {-1, 0, 1}
    obj_b = make_grid("SrcB", (4.0, 0.0, 0.0))     # 世界 x ∈ {3, 4, 5}，与 A 不相邻

    # A: 组 1 只在右列(x=1)为 1.0；组 2 只在左列(x=-1)为 0.5
    assign(obj_a, "1", lambda co: 1.0 if co.x >= 1.0 - 1e-5 else 0.0)
    assign(obj_a, "2", lambda co: 0.5 if co.x <= -1.0 + 1e-5 else 0.0)
    # B: 组 1 只在右列(x=5)为 0.25；组 2 只在左列(x=3)为 0.75
    assign(obj_b, "1", lambda co: 0.25 if co.x >= 5.0 - 1e-5 else 0.0)
    assign(obj_b, "2", lambda co: 0.75 if co.x <= 3.0 + 1e-5 else 0.0)

    bpy.context.scene.vgsplit_dedup_mode = "NONE"
    select_only([obj_a, obj_b], obj_a)
    result = bpy.ops.toolkit.vgsplit_record_and_merge()
    merged = bpy.context.view_layer.objects.active

    check("record_and_merge 返回 FINISHED", result == {"FINISHED"}, str(result))
    check("NONE 模式合并后组数=4（同名组未被折叠）", len(merged.vertex_groups) == 4,
          f"实际 {group_names(merged)}")
    check("合并后组名按来源顺序重编号", group_names(merged) == ["1", "2", "3", "4"],
          str(group_names(merged)))
    check("A 的权重落在合并组 1 上", weight_at(merged, (1.0, -1.0, 0.0), "1") == 1.0,
          str(weight_at(merged, (1.0, -1.0, 0.0), "1")))
    check("A 的权重落在合并组 2 上", weight_at(merged, (-1.0, -1.0, 0.0), "2") == 0.5,
          str(weight_at(merged, (-1.0, -1.0, 0.0), "2")))
    check("B 的权重落在合并组 3 上", weight_at(merged, (5.0, 1.0, 0.0), "3") == 0.25,
          str(weight_at(merged, (5.0, 1.0, 0.0), "3")))
    check("B 的权重落在合并组 4 上", weight_at(merged, (3.0, 1.0, 0.0), "4") == 0.75,
          str(weight_at(merged, (3.0, 1.0, 0.0), "4")))

    merged_vertex_count = len(merged.data.vertices)
    merged_name = merged.name
    select_only([merged], merged)
    result = bpy.ops.toolkit.vgsplit_split()
    split_objects = [o for o in bpy.context.selected_objects if o.type == "MESH"]

    check("split 返回 FINISHED", result == {"FINISHED"}, str(result))
    check("拆分为 2 个物体", len(split_objects) == 2, str([o.name for o in split_objects]))

    by_suffix = {}
    for obj in split_objects:
        body = obj.name[len(merged_name) + 1:] if obj.name.startswith(merged_name + "_") else obj.name
        by_suffix[body] = obj

    split_a = by_suffix.get("SrcA")
    split_b = by_suffix.get("SrcB")
    check("拆分物后缀对应来源名", split_a is not None and split_b is not None, str(list(by_suffix)))
    if split_a is None or split_b is None:
        return

    check("拆分物 A 组名还原为 ['1','2']", group_names(split_a) == ["1", "2"], str(group_names(split_a)))
    check("拆分物 B 组名还原为 ['1','2']", group_names(split_b) == ["1", "2"], str(group_names(split_b)))
    check("拆分物是整网格拷贝（几何未拆分）",
          len(split_a.data.vertices) == merged_vertex_count == len(split_b.data.vertices),
          f"{len(split_a.data.vertices)}/{merged_vertex_count}/{len(split_b.data.vertices)}")

    check("拆分物 A：组1 在 A 右列为 1.0", weight_at(split_a, (1.0, -1.0, 0.0), "1") == 1.0,
          str(weight_at(split_a, (1.0, -1.0, 0.0), "1")))
    check("拆分物 A：组2 在 A 左列为 0.5", weight_at(split_a, (-1.0, -1.0, 0.0), "2") == 0.5,
          str(weight_at(split_a, (-1.0, -1.0, 0.0), "2")))
    check("拆分物 A：B 区域无组1权重", weight_at(split_a, (5.0, 1.0, 0.0), "1") == 0.0,
          str(weight_at(split_a, (5.0, 1.0, 0.0), "1")))
    check("拆分物 A：B 区域无组2权重", weight_at(split_a, (3.0, 1.0, 0.0), "2") == 0.0,
          str(weight_at(split_a, (3.0, 1.0, 0.0), "2")))

    check("拆分物 B：组1 在 B 右列为 0.25", weight_at(split_b, (5.0, 1.0, 0.0), "1") == 0.25,
          str(weight_at(split_b, (5.0, 1.0, 0.0), "1")))
    check("拆分物 B：组2 在 B 左列为 0.75", weight_at(split_b, (3.0, 1.0, 0.0), "2") == 0.75,
          str(weight_at(split_b, (3.0, 1.0, 0.0), "2")))
    check("拆分物 B：A 区域无组1权重", weight_at(split_b, (1.0, -1.0, 0.0), "1") == 0.0,
          str(weight_at(split_b, (1.0, -1.0, 0.0), "1")))
    check("拆分物 B：A 区域无组2权重", weight_at(split_b, (-1.0, -1.0, 0.0), "2") == 0.0,
          str(weight_at(split_b, (-1.0, -1.0, 0.0), "2")))


def build_seam_case(mode):
    reset_scene()
    left = make_grid("HalfL", (0.0, 0.0, 0.0))     # 世界 x ∈ {-1, 0, 1}
    right = make_grid("HalfR", (2.0, 0.0, 0.0))    # 世界 x ∈ {1, 2, 3}，左列与 left 右列重合
    assign(left, "1", lambda co: 1.0 if co.x >= 1.0 - 1e-5 else 0.0)
    assign(left, "2", lambda co: 0.75)
    assign(right, "3", lambda co: 1.0 if co.x <= 1.0 + 1e-5 else 0.0)
    assign(right, "4", lambda co: 0.25)
    bpy.context.scene.vgsplit_dedup_mode = mode
    select_only([left, right], left)
    bpy.ops.toolkit.vgsplit_record_and_merge()
    return bpy.context.view_layer.objects.active


def case_seam_mode():
    print("== D: SEAM 模式去重 ==", flush=True)
    seam_merged = build_seam_case("SEAM")
    check("SEAM 模式把 A.1 与 B.3 去重（合并后 2 组）",
          len(seam_merged.vertex_groups) == 2, str(group_names(seam_merged)))
    check("去重后 A.1 的接缝权重保留",
          weight_at(seam_merged, (1.0, 1.0, 0.0), "1") == 1.0,
          str(weight_at(seam_merged, (1.0, 1.0, 0.0), "1")))
    check("去重后 A.2 / B.4 的权重各自保留",
          weight_at(seam_merged, (-1.0, 1.0, 0.0), "2") == 0.75
          and weight_at(seam_merged, (3.0, 1.0, 0.0), "2") == 0.25,
          f"{weight_at(seam_merged, (-1.0, 1.0, 0.0), '2')}"
          f"/{weight_at(seam_merged, (3.0, 1.0, 0.0), '2')}")

    none_merged = build_seam_case("NONE")
    check("同几何在 NONE 模式下不去重（4 组）",
          len(none_merged.vertex_groups) == 4, str(group_names(none_merged)))

    seam_merged = build_seam_case("SEAM")
    merged_name = seam_merged.name
    select_only([seam_merged], seam_merged)
    bpy.ops.toolkit.vgsplit_split()
    restored = {}
    for obj in bpy.context.selected_objects:
        if obj.type != "MESH":
            continue
        body = obj.name[len(merged_name) + 1:] if obj.name.startswith(merged_name + "_") else obj.name
        restored[body] = group_names(obj)
    check("SEAM 拆分后来源 A 还原为 ['1','2']", restored.get("HalfL") == ["1", "2"], str(restored))
    check("SEAM 拆分后来源 B 还原为 ['3','4']", restored.get("HalfR") == ["3", "4"], str(restored))


def case_diffusion_mode():
    print("== E: DIFFUSION 模式 ==", flush=True)
    reset_scene()
    near = make_grid("Near", (0.0, 0.0, 0.0), subdivisions=6)   # 世界 x,y ∈ [-1,1]
    far = make_grid("Far", (0.0, 1.5, 0.0), subdivisions=6)     # 平行且分离
    assign(near, "1", lambda co: max(0.0, 1.0 - abs(co.x)))
    assign(near, "2", lambda co: max(0.0, abs(co.x)))
    assign(far, "3", lambda co: max(0.0, 1.0 - abs(co.x)))
    assign(far, "4", lambda co: max(0.0, abs(co.x)))
    bpy.context.scene.vgsplit_dedup_mode = "DIFFUSION"
    bpy.context.scene.vgsplit_diffusion_distance = 3.0

    options = vgs._diffusion_options(bpy.context.scene)
    links, audit = vgs._diffusion_links([near, far], options)
    check("扩散分析可运行并产出审计记录", len(audit) == 1, str(len(audit)))
    vgs._publish_diffusion_report(bpy.context, [near, far], links, audit, options)
    report = bpy.data.texts.get("VG Split 扩散分析报告")
    check("扩散分析报告写入文本编辑器",
          report is not None and "扩散趋势去重分析" in report.as_string(),
          report.name if report else "缺失")
    check("候选项带中文判定理由",
          bool(audit) and all("reason" in c for c in audit[0]["candidates"]),
          str([c.get("reason") for c in audit[0]["candidates"]] if audit else []))
    # 衰减趋势一致的两对必须互配，衰减方向相反的两对必须被梯度门槛挡掉。
    # links 形如 {(物体下标, 组下标): (根节点)}，拆分左侧 two 组 → 右侧两组。
    check("扩散匹配到对应衰减趋势（Near.1↔Far.3, Near.2↔Far.4）",
          set(links.items()) == {((1, 0), (0, 0)), ((1, 1), (0, 1))},
          str(sorted((k, v) for k, v in links.items())))
    print(f"  [info] 接受链接={len(links)} 候选={len(audit[0]['candidates']) if audit else 0}",
          flush=True)


def case_unregister():
    print("== F: 注销幂等 ==", flush=True)
    vgs.unregister_vg_partition_split_properties()
    leaked = [n for n in vgs.SETTING_NAMES + ("vgsplit_manifest",) if hasattr(bpy.types.Scene, n)]
    check("注销后无属性残留", not leaked, str(leaked))
    vgs.unregister_vg_partition_split_properties()  # 二次调用必须幂等
    check("二次注销不抛异常", True)
    for cls in reversed(vgs.vg_partition_split_panels + vgs.vg_partition_split_operators):
        bpy.utils.unregister_class(cls)
    bpy.utils.unregister_class(StubToolkitParent)
    check("算子注销后确实从 bpy.ops 移除",
          not any(n in dir(bpy.ops.toolkit) for n in OP_NAMES),
          str([n for n in OP_NAMES if n in dir(bpy.ops.toolkit)]))


def main():
    case_register()
    case_none_mode_fidelity()
    case_seam_mode()
    case_diffusion_mode()
    case_unregister()


try:
    main()
except Exception:
    traceback.print_exc()
    print("SUMMARY aborted by exception", flush=True)
    sys.exit(1)

print(f"SUMMARY failures={len(FAILURES)}", flush=True)
for item in FAILURES:
    print(f"  - {item}", flush=True)
sys.exit(0 if not FAILURES else 1)
