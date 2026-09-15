"""把「上一次导入遗留的全局骨骼编号」迁移到当前工作空间的编号。

## 为什么会有这个需求

合并骨架里每根骨骼的 id 是**全局编号**：SSMT 抓帧后按「部件排序 + 部件内局部索引」
依次分配槽位（`common/zzmi_skeleton.py::build_vg_maps`）。因此：

* 部件集合一变（重新 dump / 少提取了几个部件），**每根骨骼的全局编号都会重排**；
* 而工程里合并过的物体，顶点组名字里存的是**导入那一刻**的全局编号，
  它不会跟着工作空间一起变。

结果：重新 dump 之后继续用旧物体导出，旧编号在新工作空间里指向**别的骨骼**
（或干脆超出槽位上限），游戏内表现为塌陷 / 错位 / 「面筋人」。

## 为什么能算得出来

**部件的「局部索引」在两次 dump 之间是稳定的**——它来自游戏原始权重索引，
不随部件集合变化。于是对同一个部件：

    旧全局编号  ==  旧工作空间 VGMap[局部索引]
    新全局编号  ==  新工作空间 VGMap[局部索引]

两者配对就得到 ``旧全局编号 -> 新全局编号`` 的映射。去重结果（哪几个部件共享
哪根骨骼）在两次 dump 之间可能不同，但**不影响**这条映射的正确性：局部索引本身
就唯一确定了是哪根骨骼。

映射的正确性可以自检：同一个旧编号若在多个部件里出现，它们必须指向同一个新编号；
不一致就是冲突（本模块会报出来并拒绝使用）。

## 不能做什么

只能迁移**两次 dump 都有**的部件。上一次 dump 里有、这次没提取的部件（比如头发、
尾巴），它们的旧编号没有对应关系——本模块会点名列出，不会瞎猜。
"""

from __future__ import annotations

import glob
import json
import os


def read_part_local_maps(workspace_root: str) -> dict[str, dict[int, int]]:
    """读取工作空间的 ``部件 -> {局部索引: 全局骨骼编号}``。

    数据源与导出/导入使用的是同一份：``LOD0/<部件>/TYPE_*/*.json`` 的 ``VGMap``。
    缺目录、缺文件、json 坏了都只跳过该部件（不抛异常）——调用方拿到的空结果
    自然导致"没有可迁移的映射"。
    """
    result: dict[str, dict[int, int]] = {}
    lod0_dir = os.path.join(workspace_root or "", "LOD0")
    if not os.path.isdir(lod0_dir):
        return result
    for part in sorted(os.listdir(lod0_dir)):
        submesh_dir = os.path.join(lod0_dir, part)
        if not os.path.isdir(submesh_dir):
            continue
        for type_dir in sorted(os.listdir(submesh_dir)):
            if not type_dir.startswith("TYPE_"):
                continue
            json_path = os.path.join(submesh_dir, type_dir, part + ".json")
            if not os.path.isfile(json_path):
                continue
            try:
                with open(json_path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
            except (OSError, ValueError):
                continue
            vg_map = (payload or {}).get("VGMap") or {}
            local_map: dict[int, int] = {}
            for raw_key, raw_value in vg_map.items():
                try:
                    local_map[int(raw_key)] = int(raw_value)
                except (TypeError, ValueError):
                    continue
            if local_map:
                result[part] = local_map
            break
    return result


def part_global_id_values(local_maps: dict[str, dict[int, int]]) -> dict[str, set[int]]:
    """``部件 -> 该部件 VGMap 用到的全部全局编号``（= 该部件导入时会建出来的顶点组范围）。"""
    return {part: set(local_map.values()) for part, local_map in local_maps.items()}


def build_global_id_remap(
    old_local_maps: dict[str, dict[int, int]],
    new_local_maps: dict[str, dict[int, int]],
) -> tuple[dict[int, int], list[str]]:
    """构建 ``旧全局编号 -> 新全局编号`` 映射。

    返回 ``(remap, conflicts)``；``conflicts`` 非空即"两次 dump 的同一部件局部索引
    对不上"，此时调用方**不应**应用映射（说明两次 dump 的部件不是同一份资产）。
    """
    remap: dict[int, int] = {}
    conflicts: list[str] = []
    for part, new_local in sorted(new_local_maps.items()):
        old_local = old_local_maps.get(part)
        if not old_local:
            continue
        for local in sorted(set(old_local) & set(new_local)):
            old_id = old_local[local]
            new_id = new_local[local]
            existing = remap.get(old_id)
            if existing is None:
                remap[old_id] = new_id
            elif existing != new_id:
                conflicts.append(
                    f"旧编号 {old_id} 在部件 {part} 的局部索引 {local} 上指向 "
                    f"{new_id}，但别处已指向 {existing}"
                )
    return remap, conflicts


def shared_part_count(local_maps_a: dict[str, dict[int, int]], local_maps_b: dict[str, dict[int, int]]) -> int:
    """两个工作空间共有的部件数（用来挑"最可能是上一次导入"的那个工作空间）。"""
    return len(set(local_maps_a) & set(local_maps_b))


def discover_previous_workspaces(current_root: str) -> list[tuple[str, int]]:
    """当前工作空间的**同级**目录里，挑出可能有旧编号的候选工作空间。

    返回 ``[(路径, 共有部件数)]``，按共有部件数降序。用于"上一次导入"的自动定位；
    用户也可以手动指定路径。
    """
    current_root = os.path.abspath(current_root or "")
    parent = os.path.dirname(current_root)
    if not os.path.isdir(parent):
        return []
    current_maps = read_part_local_maps(current_root)
    candidates: list[tuple[str, int]] = []
    for name in sorted(os.listdir(parent)):
        sibling = os.path.join(parent, name)
        if os.path.abspath(sibling) == current_root or not os.path.isdir(sibling):
            continue
        if not os.path.isdir(os.path.join(sibling, "LOD0")):
            continue
        shared = shared_part_count(current_maps, read_part_local_maps(sibling))
        if shared > 0:
            candidates.append((sibling, shared))
    candidates.sort(key=lambda item: (-item[1], item[0]))
    return candidates


def split_part_key(object_name: str) -> str:
    """从物体名里取它属于哪个部件（``LOD0.<部件>.<名字>``）。

    取不到（被改名 / 非导入物体）返回空串 —— 调用方会退化成"只看全局编号空间"。
    """
    name = str(object_name or "")
    if not name.upper().startswith("LOD"):
        return ""
    parts = name.split(".", 2)
    if len(parts) < 3:
        return ""
    return parts[1].strip()


def classify_used_ids(
    used_ids: set[int],
    part_key: str,
    old_values_by_part: dict[str, set[int]],
    new_values_by_part: dict[str, set[int]],
    new_workspace_ids: set[int],
) -> str:
    """判定一个物体用的是「当前工作空间编号」还是「上一次导入的旧编号」。

    返回 ``"current"`` / ``"stale"``。判据（按优先级）：

    1. 完全没用到任何骨骼编号（空集）→ ``current``（不是我们的业务对象）。
    2. 用到的编号**超出了当前工作空间的全部编号** → ``stale``（铁证）。
    3. 该物体所属部件在两边都有：用到的编号**落在旧部件范围内、且不落在新部件范围内**
       → ``stale``。
       这一条专治"两套编号有重叠段"——比如旧编号 232 和新编号 232 都存在，
       光看数字分不出来；但旧 232 属于腿部件、新 232 属于飘带部件，按部件范围就能分开。
    4. 其余 → ``current``（含"新编号恰好也落在某个旧部件范围里"的少数歧义情形，
       保守不动，避免把本来正确的物体改坏）。
    """
    if not used_ids:
        return "current"
    if not used_ids <= new_workspace_ids:
        return "stale"
    if part_key:
        old_values = old_values_by_part.get(part_key)
        new_values = new_values_by_part.get(part_key)
        if old_values and new_values:
            if used_ids <= old_values and not used_ids <= new_values:
                return "stale"
    return "current"


def plan_object_migration(
    used_ids: set[int],
    part_key: str,
    remap: dict[int, int],
    old_values_by_part: dict[str, set[int]],
    new_values_by_part: dict[str, set[int]],
    new_workspace_ids: set[int],
) -> dict:
    """给单个物体出迁移方案：``{action, reason, unmapped}``。

    ``action`` ∈ ``{"skip", "migrate", "blocked"}``：
    * ``skip``：当前编号，不用动；
    * ``migrate``：旧编号且**全部**可迁移；
    * ``blocked``：旧编号但有关键编号没有对应关系（部件这次没提取），
      此时**不动它**并点名 —— 部改一半比不改更糟。
    """
    if classify_used_ids(used_ids, part_key, old_values_by_part, new_values_by_part, new_workspace_ids) == "current":
        return {"action": "skip", "reason": "当前工作空间编号", "unmapped": []}
    unmapped = sorted(bone_id for bone_id in used_ids if bone_id not in remap)
    if unmapped:
        return {
            "action": "blocked",
            "reason": "旧编号里有本次工作空间没有的部件，无法确定对应关系",
            "unmapped": unmapped,
        }
    return {"action": "migrate", "reason": "旧编号，可完整迁移", "unmapped": []}


def remap_group_ids(group_names: list[str], remap: dict[int, int]) -> dict[int, int | None]:
    """把「顶点组索引 -> 迁移后的骨骼编号」算出来。

    非数字组名（不是骨骼组）映射为 ``None``，调用方原样保留、不参与迁移。
    """
    result: dict[int, int | None] = {}
    for index, name in enumerate(group_names):
        raw = str(name)
        if not raw.lstrip("-").isdigit():
            result[index] = None
            continue
        old_id = int(raw)
        result[index] = int(remap.get(old_id, old_id))
    return result


def migrate_object_vertex_groups(obj, remap: dict[int, int]) -> dict:
    """把单个 Blender 物体的骨骼顶点组按 ``remap`` 重编号（就地修改）。

    做法：先读出每个顶点「目标编号 -> 权重」，清空顶点组后按目标编号重建。
    之所以整份重建而不是简单改名：多根旧骨骼可能迁移到同一个新编号
    （两次 dump 的跨部件去重结果不同），这时必须把权重**相加**，改名做不到。

    非数字组名（不是骨骼）原样保留。
    """
    groups = obj.vertex_groups
    names = [group.name for group in groups]
    index_to_target = remap_group_ids(names, remap)

    # 1) 非骨骼组：先记下权重，重建后原样写回。
    preserved: dict[str, list[tuple[int, float]]] = {}
    # 2) 骨骼组：每个顶点聚合成 目标编号 -> 权重。
    per_vertex: list[dict[int, float]] = [dict() for _ in range(len(obj.data.vertices))]
    for vertex in obj.data.vertices:
        for elem in vertex.groups:
            if elem.weight <= 0:
                continue
            target = index_to_target.get(elem.group)
            if target is None:
                preserved.setdefault(names[elem.group], []).append(
                    (vertex.index, float(elem.weight))
                )
                continue
            bucket = per_vertex[vertex.index]
            bucket[target] = bucket.get(target, 0.0) + float(elem.weight)

    groups.clear()
    for name, entries in preserved.items():
        group = groups.new(name=name)
        for vertex_index, weight in entries:
            group.add((vertex_index,), weight, 'REPLACE')
    rebuilt: dict[int, object] = {}
    for vertex_index, bucket in enumerate(per_vertex):
        for target, weight in bucket.items():
            group = rebuilt.get(target)
            if group is None:
                group = groups.new(name=str(target))
                rebuilt[target] = group
            group.add((vertex_index,), weight, 'REPLACE')
    return {"groups_before": len(names), "groups_after": len(groups), "bones_written": len(rebuilt)}


def collect_object_used_bone_ids(obj) -> set[int]:
    """该物体"权重>0"实际引用到的全部骨骼编号（组名必须全是数字，非数字组忽略）。"""
    names = [group.name for group in obj.vertex_groups]
    used: set[int] = set()
    for vertex in obj.data.vertices:
        for elem in vertex.groups:
            if elem.weight <= 0 or elem.group >= len(names):
                continue
            raw = names[elem.group]
            if raw.lstrip("-").isdigit():
                used.add(int(raw))
    return used


def plan_scene_migration(
    objects,
    workspace_root: str,
    previous_workspace_root: str | None = None,
) -> dict:
    """给整个场景出一个迁移计划（不改动任何物体）。

    返回 ``{status, remap, old_root, shared, migrate, blocked, skipped, message}``；
    ``status`` ∈ ``{"ok", "no_workspace", "no_previous", "conflict", "no_remap"}``。
    ``migrate`` 是 ``[(物体, 用到的编号集)]``，``blocked`` 是 ``[(物体名, 未能映射的编号)]``。
    """
    new_local_maps = read_part_local_maps(workspace_root)
    empty = {"remap": {}, "old_root": "", "shared": 0, "migrate": [], "blocked": [], "skipped": []}
    if not new_local_maps:
        return dict(empty, status="no_workspace", message="当前工作空间没有可用的 VGMap")

    old_root = str(previous_workspace_root or "").strip()
    shared = 0
    if not old_root:
        candidates = discover_previous_workspaces(workspace_root)
        if not candidates:
            return dict(
                empty,
                status="no_previous",
                message="同级目录里没找到含相同部件上一次工作空间，无法建立编号对应关系",
            )
        old_root, shared = candidates[0]
    old_local_maps = read_part_local_maps(old_root)
    if not old_local_maps:
        return dict(empty, status="no_previous", old_root=old_root, message=f"{old_root} 读取不到 VGMap")
    if not shared:
        shared = shared_part_count(old_local_maps, new_local_maps)

    remap, conflicts = build_global_id_remap(old_local_maps, new_local_maps)
    if conflicts:
        return dict(
            empty,
            status="conflict",
            old_root=old_root,
            shared=shared,
            message="两次工作空间的部件对不上（同一部件的局部索引指向不同骨骼），已中止",
        )
    if not remap:
        return dict(
            empty,
            status="no_remap",
            old_root=old_root,
            shared=shared,
            message=f"与 {old_root} 没有可用的编号映射",
        )

    old_values = part_global_id_values(old_local_maps)
    new_values = part_global_id_values(new_local_maps)
    new_workspace_ids: set[int] = set()
    for values in new_values.values():
        new_workspace_ids |= values

    migrate, blocked, skipped = [], [], []
    for obj in objects:
        if getattr(obj, "type", "") != "MESH":
            continue
        used = collect_object_used_bone_ids(obj)
        plan = plan_object_migration(
            used,
            split_part_key(getattr(obj, "name", "")),
            remap,
            old_values,
            new_values,
            new_workspace_ids,
        )
        if plan["action"] == "migrate":
            migrate.append((obj, used))
        elif plan["action"] == "blocked":
            blocked.append((getattr(obj, "name", "?"), plan["unmapped"]))
        else:
            skipped.append(getattr(obj, "name", "?"))

    return {
        "status": "ok",
        "remap": remap,
        "old_root": old_root,
        "shared": shared,
        "migrate": migrate,
        "blocked": blocked,
        "skipped": skipped,
        "message": f"编号映射 {len(remap)} 条",
    }


def apply_scene_migration(plan: dict) -> list[str]:
    """执行 ``plan_scene_migration`` 的结论，返回被迁移的物体名。"""
    done: list[str] = []
    remap = plan.get("remap") or {}
    if not remap:
        return done
    for obj, _used in plan.get("migrate") or []:
        migrate_object_vertex_groups(obj, remap)
        done.append(getattr(obj, "name", "?"))
    return done


def auto_migrate_scene(objects, workspace_root: str, log_prefix: str = "[骨骼编号迁移]") -> list[str]:
    """导出前的自动迁移入口（静默失败、出声成功）。

    * 工程里本来就是当前编号（绝大多数情况）→ 计划里没有要迁移的物体，什么都不打印；
    * 找不到上一次工作空间 → 什么都不做（普通工程根本没有这个概念，不该刷屏）；
    * 找到映射且有旧编号物体 → 打印清单并迁移；有冲突/无法映射的部件也点名。
    """
    if not workspace_root:
        return []
    plan = plan_scene_migration(list(objects), workspace_root)
    status = plan["status"]
    if status == "conflict":
        print(f"{log_prefix} !!! {plan['message']}；跳过自动迁移")
        return []
    if status != "ok" or not plan["migrate"]:
        return []
    print(
        f"{log_prefix} 检测到 {len(plan['migrate'])} 个物体用的是上一次导入的骨骼编号"
        f"（当前工作空间 {os.path.basename(os.path.normpath(workspace_root))}，"
        f"上一次 {os.path.basename(os.path.normpath(plan['old_root']))}，"
        f"{plan['message']}）—— 导出前自动迁移"
    )
    done = apply_scene_migration(plan)
    for name in done:
        print(f"{log_prefix}   已迁移 {name}")
    for name, unmapped in plan["blocked"]:
        print(
            f"{log_prefix} !!! 跳过 {name}：{len(unmapped)} 个旧编号在本次工作空间没有对应"
            f"（部件这次没提取），不能瞎猜"
        )
    return done
