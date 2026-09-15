"""ZZMI 合并骨架的「导入/导出契约」判定（纯函数，无 bpy / 无 IO）。

为什么需要它（2026-09-13 实测悬案）：

ZZMI 的合并骨架是**两段绑定**的：

* **导入段**（``ZZMISkeletonMergeHelper.ensure_skeleton_data``）把反向查出的
  ``VGMap/VGOffset/VGCount/SkeletonGroup`` 写回工作空间子网格 json，导入器据此把
  该部件的顶点组建到**全局骨骼编号空间**（跨部件共用一套编号）。
* **导出段**（``ExportZZMI._collect_merged_skeleton_components`` +
  ``add_merged_skeleton_sections``）据此生成运行时的合并骨架段落（palette 直拷
  attach + 按槽守卫）。

问题在于：**只要导出段的组件列表为空，导出器就静默退回普通导出**——
``add_unity_vs_texture_override_vb_sections`` 走 else 分支只发
``handling = skip`` / ``draw = N, 0``。可是 Blender 里的顶点组**已经是全局编号**
（那是导入段写进去的，并且随之存进了 .blend），游戏端每个部件只有自己的小
palette，全局编号一越界，蒙皮结果就是垃圾 —— 实机现象就是**模型整块不显示**。

实测证据（用户 2026-09-13 的叶瞬光工程）：

* ``叶瞬光 20260913.blend`` 里每个网格的顶点组数 = 该网格实际用到的最大索引 + 1，
  且**只用 1 根骨头的部件索引是 49**（眉毛）、只用 3 根的部件最大索引 179
  （后背铃铛）—— 这是全局编号，不是「部件自己的 0..N-1」。
* 集合里有 ``SkeletonGroup_0..3``（合并导入才会建的合集）。
* 同一份导出 ``Mods/SSMTGeneratedMod/叶瞬光（原）/叶瞬光（原）.ini`` 里
  ``ResourceZZMergedSkeleton`` / ``CustomShaderZZMIMergedSkeletonAttach`` /
  ``ResourceZZPalette`` / ``ResourceZZVgMap`` / ``seen_`` / ``occ_`` **全部 0 次**。

即：**导入段生效了，导出段没生效**，于是产出的 mod 必然是坏的，而插件当时只打了
几行控制台文字。本模块把这件事变成**不可能被忽略的判定**。

判定口径（保守、可解释）：

* ``parts_with_data``：工作空间里带合并数据（``VGCount > 0`` 或 ``VGMap`` 非空）
  的部件数 —— 它是「Blender 顶点组已被全局编号化」的证据。
* ``components``：导出器真正收集到的合并组件数。

| 情况 | 结论 |
|---|---|
| 有数据 + 0 组件 + 开关关 | **错误**：全局编号的几何 + 没有合并骨架运行时 = 必然不显示 |
| 有数据 + 0 组件 + 开关开 | **错误**：列出每个部件被拒绝的具体原因 |
| 有数据 + 有组件 + 有部件被拒 | **警告**：被拒部件在游戏里不显示（其余部件正常） |
| 无数据 + 0 组件 | 提示（**不是**错误）：正常的普通导出 |
"""

NO_MERGED_DATA_NOTICE = (
    "[ZZMI骨骼合并] 本次导出为普通导出：工作空间里没有合并骨架数据"
    "（VGMap/VGCount）。若你期望合并骨架，请先在‘使用融合统一顶点组’开启的情况下"
    "重新导入一次，再导出。"
)


def format_skip_reasons(skip_reasons, limit: int = 6) -> str:
    """把 ``{draw_ib: reason}`` 拼成一行可读文本。"""
    if not skip_reasons:
        return ""
    items = []
    for index, (draw_ib, reason) in enumerate(sorted(skip_reasons.items())):
        if index >= limit:
            items.append(f"…（另有 {len(skip_reasons) - limit} 个部件同类问题）")
            break
        items.append(f"{draw_ib}: {reason}")
    return "；".join(items)


def evaluate_merged_skeleton_contract(
    *,
    checkbox_enabled: bool,
    parts_with_data: int,
    component_count: int,
    skip_reasons=None,
) -> dict:
    """返回 ``{"level": "error"|"warning"|"notice"|"ok", "message": str, "hint": str}``。

    ``level == "error"`` 表示调用方应当**中止导出**（产出必然是坏的 mod）。
    """
    skip_reasons = dict(skip_reasons or {})
    reasons_text = format_skip_reasons(skip_reasons)

    if parts_with_data > 0 and component_count == 0:
        if not checkbox_enabled:
            return {
                "level": "error",
                "message": (
                    "骨骼合并中止：工作空间里有 {n} 个部件带合并骨架数据（VGMap），"
                    "但导出时“使用融合统一顶点组”是**关闭**的。".format(n=parts_with_data)
                    + "这样导出的几何用全局骨骼编号、却没有对应的运行时合并骨架，"
                    "游戏里会整块不显示。"
                ),
                "hint": (
                    "处置：打开“使用融合统一顶点组”后重新导出；"
                    "若你确实想走普通模式，请关闭该开关后**重新导入**，"
                    "让顶点组回到部件局部编号。"
                ),
            }
        return {
            "level": "error",
            "message": (
                "骨骼合并中止：{n} 个部件带合并数据，但全部被导出器拒绝，"
                "本次会退化成普通导出（几何是全局编号 → 游戏里不显示）。".format(
                    n=parts_with_data
                )
            ),
            "hint": (
                ("被拒原因：" + reasons_text + "。") if reasons_text else ""
            ) + (
                "处置：用面板的“清除骨骼合并VGMap缓存”后重新导入（需要合格抓帧），"
                "或把这条信息发给开发者。"
            ),
        }

    if component_count > 0 and skip_reasons:
        return {
            "level": "warning",
            "message": (
                "骨骼合并不完整：{n} 个部件未进入合并骨架，它们在游戏里会不显示"
                "（其余部件正常）。".format(n=len(skip_reasons))
            ),
            "hint": ("被拒原因：" + reasons_text + "。") if reasons_text else "",
        }

    if parts_with_data == 0 and component_count == 0:
        return {"level": "notice", "message": NO_MERGED_DATA_NOTICE, "hint": ""}

    return {"level": "ok", "message": "", "hint": ""}
