"""headless Blender 冒烟：动画驱动关键帧预览的 bpy 侧路径。

用法（Windows）::

    "D:\\steam\\steamapps\\common\\Blender\\blender.exe" --background \
        --python tests/blender_smoke_anim_driver_preview.py

覆盖解释器之外的全部风险点：形态键关键帧写入、Blender 4.4+/5.x 的 action slot、
场景自定义属性关键帧、用户既有动作的挂起、清除。
"""

import importlib.util
import json
import sys
import types
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PKG = "_th4_preview_smoke"


def _install_package(name, path):
    module = types.ModuleType(name)
    module.__path__ = [str(path)]
    sys.modules[name] = module
    return module


_install_package(PKG, REPO)
for _sub in ("blueprint", "common", "utils", "toolkit"):
    _install_package(f"{PKG}.{_sub}", REPO / _sub)


def _load(module_name):
    relative = module_name.replace(".", "/")
    spec = importlib.util.spec_from_file_location(f"{PKG}.{module_name}", REPO / f"{relative}.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


for _name in (
    "common.global_properties",
    "common.global_config",
    "common.logic_name",
    "utils.translate_utils",
    "common.text_width_utils",
    "blueprint.node_base",
    "blueprint.variable_registry",
    "blueprint.anim_driver_base",
    "blueprint.anim_driver_collector",
    "blueprint.anim_driver_runtime",
    "blueprint.anim_driver_forward_play",
    "blueprint.anim_driver_pingpong",
    "blueprint.anim_driver_random",
    "blueprint.anim_driver_preview",
):
    _load(_name)

import bpy  # noqa: E402

preview = sys.modules[f"{PKG}.blueprint.anim_driver_preview"]

TRIANGLE_SEGMENT = """[Constants]
global persist $anim_frame1 = 0
global persist $ping = 0
global persist $direction = 1
[Present]
$anim_frame1 = (time * 30) // 1
$ping = $ping + $direction
if $ping > 10
    $ping = 10
    $direction = -1
endif
if $ping < 0
    $ping = 0
    $direction = 1
endif
$Freq_smile = $ping / 10
"""

RANDOM_SEGMENT = """[Constants]
global persist $shape_up = 0
[Present]
$shape_up = $anim_frame1 % 7
"""

FRAME_COUNT = 300

results = []
failures = []


def check(label, condition, detail=""):
    results.append({"check": label, "ok": bool(condition), "detail": str(detail)})
    if not condition:
        failures.append(f"{label}: {detail}")
    print(f"SMOKE_CHECK {'OK ' if condition else 'FAIL'} {label} | {detail}")


class _FakeOperatorCall:
    def __init__(self):
        self.node_name = ""
        self.frame_count = 0


class _FakeLayout:
    """记录调用序列的假 UILayout：只验证绘制代码不抛异常且算子参数正确。"""

    def __init__(self, calls):
        self._calls = calls

    def box(self):
        return self

    def row(self, align=False):
        return self

    def column(self, align=False):
        return self

    def separator(self):
        self._calls.append(("separator", None, None))
        return self

    def label(self, text="", icon=None):
        self._calls.append(("label", text, icon))
        return self

    def prop(self, data, name, text=None, **kwargs):
        self._calls.append(("prop", name, text))
        return self

    def operator(self, idname, text=None, icon=None):
        self._calls.append(("operator", idname, text))
        return _FakeOperatorCall()

    def template_list(self, *args, **kwargs):
        self._calls.append(("template_list", None, None))
        return self

    def prop_search(self, *args, **kwargs):
        self._calls.append(("prop_search", None, None))
        return self

    def menu(self, *args, **kwargs):
        self._calls.append(("menu", None, None))
        return self


def main():
    bpy.ops.wm.read_factory_settings(use_empty=True)
    scene = bpy.context.scene
    mesh = bpy.data.meshes.new("Body")
    obj = bpy.data.objects.new("Body", mesh)
    scene.collection.objects.link(obj)
    obj.shape_key_add(name="Basis")
    obj.shape_key_add(name="smile")
    frown = obj.shape_key_add(name="frown")

    # 用户既有动作：预览必须挂起它而不是污染/删除
    frown.value = 0.5
    frown.keyframe_insert(data_path="value", frame=1)
    user_action = obj.data.shape_keys.animation_data.action
    user_action.name = "UserShapeKeyAction"
    check("既有动作已建立", user_action is not None, user_action.name if user_action else "")

    paragraphs = [{"ini_content": TRIANGLE_SEGMENT}, {"ini_content": RANDOM_SEGMENT}]
    simulation = preview.simulate_anim_driver_paragraphs(paragraphs, FRAME_COUNT, 30)
    check("帧计数器被识别", simulation["frame_variables"] == ["$anim_frame1"], simulation["frame_variables"])
    check(
        "驱动帧号 0..299",
        simulation["variables"]["$anim_frame1"] == [float(i) for i in range(FRAME_COUNT)],
        simulation["variables"]["$anim_frame1"][:3],
    )
    check("没有未解析命令", simulation["skipped_commands"] == [], simulation["skipped_commands"])

    tree = types.SimpleNamespace(name="AnimTree")
    targets = [
        {
            "variable": "$Freq_smile",
            "source": "Play",
            "interpolation": "LINEAR",
            "kind": "shapekey",
            "object_name": "Body",
            "shape_key_name": "smile",
        },
        {
            "variable": "$shape_up",
            "source": "Random",
            "interpolation": "CONSTANT",
            "kind": "scene_property",
            "property_name": "anim_preview_shape_up",
        },
    ]

    frames = list(range(1, FRAME_COUNT + 1))
    prepared, stashed = preview._prepare_preview_actions(tree, targets, scene)
    check("既有动作被挂起（fake user）", stashed == ["UserShapeKeyAction"], stashed)
    for target in targets:
        values = simulation["variables"][target["variable"]]
        if target["kind"] == "shapekey":
            action = prepared.get(("shapekey", target["object_name"]))
            ok, detail = preview._bake_shape_key_target(target, frames, values, action)
        else:
            action = prepared.get(("scene",))
            ok, detail = preview._bake_scene_property_target(target, frames, values, scene, action)
        check(f"写入 {target['variable']}", ok, detail)

    scene.frame_start = 1
    scene.frame_end = FRAME_COUNT

    # ---- 形态键 ----
    shape_keys = obj.data.shape_keys
    action = shape_keys.animation_data.action
    check("预览动作已绑定", action is not None and action.name.startswith("AnimDriverPreview"), action.name if action else "")
    check("预览动作与用户动作分离", action is not user_action, "")
    check("用户动作仍被挂起（fake user）", bool(user_action.use_fake_user), user_action.use_fake_user)
    check(
        "用户动作曲线仍在",
        len(preview.iter_action_fcurves(user_action)) == 1,
        len(preview.iter_action_fcurves(user_action)),
    )
    check(
        "Blender 5.x 已移除 Action.fcurves（走 layers/strips/channelbags 回退）",
        not hasattr(action, "fcurves"),
        hasattr(action, "fcurves"),
    )

    fcurves = preview.iter_action_fcurves(action)
    check("形态键曲线数量", len(fcurves) == 1, len(fcurves))
    keyframe_count = len(fcurves[0].keyframe_points) if fcurves else 0
    check("形态键关键帧数量 = 300", keyframe_count == FRAME_COUNT, keyframe_count)
    check(
        "形态键插值为 LINEAR",
        all(point.interpolation == "LINEAR" for point in fcurves[0].keyframe_points),
        {point.interpolation for point in fcurves[0].keyframe_points} if fcurves else None,
    )

    smile = shape_keys.key_blocks["smile"]
    for frame in (1, 5, 40, 150, 299, 300):
        scene.frame_set(frame)
        expected = simulation["variables"]["$Freq_smile"][frame - 1]
        check(
            f"第 {frame} 帧形态键值",
            abs(smile.value - expected) < 1e-4,
            f"实际 {smile.value:.5f} / 期望 {expected:.5f}",
        )

    # ---- 场景属性 ----
    check("场景预览属性存在", "anim_preview_shape_up" in scene.keys(), list(scene.keys()))
    scene_action = scene.animation_data.action if scene.animation_data else None
    check(
        "场景预览动作",
        scene_action is not None and scene_action.name.startswith("AnimDriverPreview"),
        scene_action.name if scene_action else "",
    )
    scene_fcurves = preview.iter_action_fcurves(scene_action) if scene_action else []
    check("场景属性关键帧数量 = 300", len(scene_fcurves) == 1 and len(scene_fcurves[0].keyframe_points) == FRAME_COUNT, [len(f.keyframe_points) for f in scene_fcurves])
    check(
        "场景属性插值为 CONSTANT",
        all(point.interpolation == "CONSTANT" for point in scene_fcurves[0].keyframe_points) if scene_fcurves else False,
        {point.interpolation for point in scene_fcurves[0].keyframe_points} if scene_fcurves else None,
    )
    scene.frame_set(4)
    check(
        "第 4 帧场景属性值",
        abs(float(scene["anim_preview_shape_up"]) - simulation["variables"]["$shape_up"][3]) < 1e-6,
        scene["anim_preview_shape_up"],
    )

    # ---- 清除 ----
    cleared = preview.clear_anim_driver_preview(context=bpy.context)
    check("清除报告动作数", len(cleared["removed_actions"]) == 2, cleared)
    check("清除报告属性数", cleared["removed_properties"] == ["anim_preview_shape_up"], cleared)
    check("预览动作已删除", not any(a.name.startswith("AnimDriverPreview") for a in bpy.data.actions), [a.name for a in bpy.data.actions])
    check("场景属性已删除", "anim_preview_shape_up" not in scene.keys(), list(scene.keys()))
    check("用户动作仍在", "UserShapeKeyAction" in bpy.data.actions, [a.name for a in bpy.data.actions])
    check(
        "形态键不再有预览动作",
        shape_keys.animation_data is None or shape_keys.animation_data.action is None,
        getattr(getattr(shape_keys, "animation_data", None), "action", None),
    )

    # ---- 重烘焙：帧数变短不得残留上一轮的尾帧 ----
    short_frames = list(range(1, 121))
    short_prepared, _short_stashed = preview._prepare_preview_actions(tree, targets, scene)
    ok, detail = preview._bake_shape_key_target(
        targets[0], short_frames, simulation["variables"]["$Freq_smile"][:120], short_prepared.get(("shapekey", "Body"))
    )
    short_fcurves = preview.iter_action_fcurves(obj.data.shape_keys.animation_data.action)
    check(
        "重烘焙 120 帧不残留旧尾帧",
        ok and len(short_fcurves) == 1 and len(short_fcurves[0].keyframe_points) == 120,
        [len(fcurve.keyframe_points) for fcurve in short_fcurves],
    )
    preview.clear_anim_driver_preview(context=bpy.context)

    # ---- 注册路径：属性继承与操作符登记 ----
    registration_modules = (
        "blueprint.node_base",
        "blueprint.anim_driver_base",
        "blueprint.anim_driver_runtime",
        "blueprint.anim_driver_forward_play",
        "blueprint.anim_driver_pingpong",
        "blueprint.anim_driver_random",
        "blueprint.anim_driver_preview",
    )
    registered = []
    try:
        for module_name in registration_modules:
            module = sys.modules[f"{PKG}.{module_name}"]
            register = getattr(module, "register", None)
            if callable(register):
                register()
                registered.append(module_name)
        check("模块注册成功", len(registered) == len(registration_modules), registered)
    except Exception as error:  # 注册链任何一环失败都要显式暴露
        check("模块注册成功", False, f"{type(error).__name__}: {error}")

    forward_cls = getattr(bpy.types, "SSMTNode_AnimDriver_ForwardPlay", None)
    module_cls = getattr(sys.modules[f"{PKG}.blueprint.anim_driver_forward_play"], "SSMTNode_AnimDriver_ForwardPlay", None)
    print(
        "SMOKE_DEBUG bpy.types 命中="
        f"{forward_cls is not None} 模块类={module_cls} "
        f"registered={getattr(module_cls, 'is_registered', None)} "
        f"rna={getattr(getattr(module_cls, 'bl_rna', None), 'identifier', None)} "
        f"types={[n for n in dir(bpy.types) if 'AnimDriver' in n][:6]}"
    )
    if forward_cls is None:
        forward_cls = module_cls
    check(
        "驱动节点继承到 preview_frame_count（基类注解被 RNA 收集）",
        forward_cls is not None and "preview_frame_count" in forward_cls.bl_rna.properties,
        [p.identifier for p in forward_cls.bl_rna.properties] if forward_cls is not None else None,
    )
    preview_module = sys.modules[f"{PKG}.blueprint.anim_driver_preview"]
    check(
        "预览操作符已注册",
        all(
            bool(getattr(getattr(preview_module, name, None), "is_registered", False))
            for name in ("SSMT_OT_AnimDriverPreviewBake", "SSMT_OT_AnimDriverPreviewClear")
        ),
        [
            (name, getattr(getattr(preview_module, name, None), "is_registered", None))
            for name in ("SSMT_OT_AnimDriverPreviewBake", "SSMT_OT_AnimDriverPreviewClear")
        ],
    )

    # 真机走一遍操作符入口（节点树 + 节点实例 + 面板算子）
    if forward_cls is not None:
        try:
            tree = bpy.data.node_groups.new(name="AnimTreeReal", type="SSMTBlueprintTreeType")
            tree["is_animation_driver"] = True
            runtime_node = tree.nodes.new("SSMTNode_AnimDriver_Runtime")
            runtime_node.fps = 30
            runtime_node.playback_rate = 1
            play_node = tree.nodes.new("SSMTNode_AnimDriver_ForwardPlay")
            play_node.name = "Play"
            play_node.frame_start = 0.0
            play_node.frame_end = 4.0
            play_node.play_total_duration = 1.0
            play_node.custom_paused_var = "$paused"
            play_node.loop_playback = True
            # 连续形态键模式自带目标物体与形态键名，不依赖父级蓝图的形态键配置节点
            play_node.use_continuous_shapekey_mode = True
            play_node.continuous_target_object = "Body"
            item = play_node.continuous_shape_key_items.add()
            item.shape_key_name = "smile"
            item.variable_name = "$Freq_smile"
            tree.links.new(runtime_node.outputs[0], play_node.inputs[0])
            play_node.preview_frame_count = 120

            result = preview.bake_anim_driver_preview(
                tree, node=play_node, frame_count=120, context=bpy.context
            )
            check("真机 bake 走通", bool(result.get("ok")), result.get("message"))
            check(
                "真机 bake 只写本节点变量",
                [target["shape_key_name"] for target in result.get("targets") or []] == ["smile"],
                result.get("targets"),
            )
            check(
                "真机 bake 命中形态键",
                any(target["kind"] == "shapekey" for target in result.get("targets") or []),
                result.get("targets"),
            )
            check(
                "真机 bake 帧数 = 120",
                all(len(values) == 120 for values in (result.get("simulation") or {}).get("variables", {}).values()),
                sorted(len(values) for values in (result.get("simulation") or {}).get("variables", {}).values())[:3],
            )
            check("真机 bake 场景范围", (scene.frame_start, scene.frame_end) == (1, 120), (scene.frame_start, scene.frame_end))
            real_fcurves = preview.iter_action_fcurves(obj.data.shape_keys.animation_data.action)
            check(
                "真机 bake 形态键关键帧 = 120",
                len(real_fcurves) == 1 and len(real_fcurves[0].keyframe_points) == 120,
                [len(fcurve.keyframe_points) for fcurve in real_fcurves],
            )

            # ---- 节点级作用域：第二个节点写另一个形态键，互不覆盖 ----
            second = tree.nodes.new("SSMTNode_AnimDriver_ForwardPlay")
            second.name = "Play2"
            second.use_continuous_shapekey_mode = True
            second.continuous_target_object = "Body"
            second_item = second.continuous_shape_key_items.add()
            second_item.shape_key_name = "frown"
            second_item.variable_name = "$Freq_frown"
            second.preview_frame_count = 60
            second_result = preview.bake_anim_driver_preview(
                tree, node=second, frame_count=60, context=bpy.context
            )
            check(
                "第二个节点只写自己的变量",
                [target["shape_key_name"] for target in second_result.get("targets") or []] == ["frown"],
                second_result.get("targets"),
            )
            both_curves = preview.iter_action_fcurves(obj.data.shape_keys.animation_data.action)
            check(
                "两个节点的曲线共存于同一动作",
                sorted(fcurve.data_path for fcurve in both_curves)
                == ['key_blocks["frown"].value', 'key_blocks["smile"].value'],
                [fcurve.data_path for fcurve in both_curves],
            )
            check(
                "共享动作按节点各写各的帧数",
                sorted(len(fcurve.keyframe_points) for fcurve in both_curves) == [60, 120],
                [len(fcurve.keyframe_points) for fcurve in both_curves],
            )

            node_clear = preview.clear_anim_driver_preview(
                tree=tree, node=play_node, context=bpy.context
            )
            remaining = preview.iter_action_fcurves(obj.data.shape_keys.animation_data.action)
            check(
                "按节点清除只删自己的曲线",
                node_clear["removed_curves"] == 1 and node_clear["removed_actions"] == [],
                node_clear,
            )
            check(
                "另一个节点的曲线被保留",
                [fcurve.data_path for fcurve in remaining] == ['key_blocks["frown"].value'],
                [fcurve.data_path for fcurve in remaining],
            )

            # 面板绘制与算子定位（不依赖 node editor 上下文）
            calls = []
            preview.draw_anim_driver_preview_controls(_FakeLayout(calls), play_node)
            operators = [call for call in calls if call[0] == "operator"]
            check(
                "预览控件生成/清除/清除全部三个算子",
                [call[1] for call in operators]
                == [
                    "ssmt.anim_driver_preview_bake",
                    "ssmt.anim_driver_preview_clear",
                    "ssmt.anim_driver_preview_clear",
                ],
                operators,
            )
            check(
                "预览控件绑定节点帧数属性",
                any(call[0] == "prop" and call[1] == "preview_frame_count" for call in calls),
                [call for call in calls if call[0] == "prop"],
            )
            check(
                "预览控件文案含 300 上限",
                any(call[0] == "label" and "300" in str(call[1]) for call in calls),
                [call[1] for call in calls if call[0] == "label"],
            )
            fake_context = types.SimpleNamespace(
                space_data=types.SimpleNamespace(edit_tree=tree), scene=scene
            )
            check(
                "算子能按名字定位驱动节点",
                preview._find_anim_driver_node(fake_context, play_node.name) == play_node,
                play_node.name,
            )
            check(
                "算子对不存在的节点返回 None",
                preview._find_anim_driver_node(fake_context, "NotThere") is None,
                "",
            )

            # 三个开放预览的节点：完整 draw_buttons 路径（含新增的预览控件）
            draw_nodes = [
                play_node,
                tree.nodes.new("SSMTNode_AnimDriver_PingPong"),
                tree.nodes.new("SSMTNode_AnimDriver_Random"),
            ]
            for draw_node in draw_nodes:
                draw_calls = []
                try:
                    draw_node.draw_buttons(bpy.context, _FakeLayout(draw_calls))
                    drawn = any(
                        call[0] == "operator" and call[1] == "ssmt.anim_driver_preview_bake"
                        for call in draw_calls
                    )
                    check(f"{draw_node.bl_idname} 面板含预览控件", drawn, len(draw_calls))
                except Exception as error:
                    check(f"{draw_node.bl_idname} 面板含预览控件", False, f"{type(error).__name__}: {error}")

            all_clear = preview.clear_anim_driver_preview(context=bpy.context)
            check(
                "清除全部预览",
                bool(all_clear["removed_actions"])
                and not any(
                    action.name.startswith("AnimDriverPreview") for action in bpy.data.actions
                ),
                all_clear,
            )
        except Exception as error:
            check("真机 bake 走通", False, f"{type(error).__name__}: {error}")

    print("SMOKE_RESULT " + json.dumps({"failures": failures, "results": results}, ensure_ascii=False))
    if failures:
        raise SystemExit(1)


if __name__ == '__main__':
    main()
