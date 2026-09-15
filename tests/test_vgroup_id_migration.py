"""旧骨骼编号迁移（common/vgroup_id_migration.py）单测。

真实背景（2026-09-15，叶瞬光工程）：用户重新 dump 后只提取了 7 个部件（上次 17 个），
工作空间总槽位从 428 掉到 255，**每根骨骼的全局编号都重排了**；但工程里合并过的
物体顶点组名字还停留在上一次的编号。导出后游戏内就是塌陷 / 侧躺 / 「面筋人」。

这份单测把「怎么算映射」和「怎么判断一个物体是旧编号还是当前编号」钉死。
"""

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
_PATH = REPO_ROOT / "common" / "vgroup_id_migration.py"
_SPEC = importlib.util.spec_from_file_location("vgroup_id_migration_under_test", _PATH)
assert _SPEC and _SPEC.loader
mod = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(mod)


def write_workspace(root: str, parts: dict[str, dict[int, int]]) -> None:
    """按工作空间真实布局写出最小样本：LOD0/<部件>/TYPE_x/<部件>.json。"""
    for part, vg_map in parts.items():
        type_dir = os.path.join(root, "LOD0", part, "TYPE_GPU_P12_")
        os.makedirs(type_dir, exist_ok=True)
        payload = {"VGMap": {str(k): v for k, v in vg_map.items()}, "VGCount": len(vg_map)}
        with open(os.path.join(type_dir, part + ".json"), "w", encoding="utf-8") as handle:
            json.dump(payload, handle)


class _FakeElem:
    def __init__(self, group, weight):
        self.group = group
        self.weight = weight


class _FakeVertex:
    def __init__(self, index, groups):
        self.index = index
        self.groups = groups


class _FakeData:
    def __init__(self, vertices):
        self.vertices = vertices


class FakeGroup:
    def __init__(self, owner, name: str):
        self._owner = owner
        self.name = name

    def add(self, indices, weight, _mode):
        owner = self._owner
        index = owner.vertex_groups._groups.index(self)
        for vertex_index in indices:
            vertex = owner.data.vertices[vertex_index]
            vertex.groups = [elem for elem in vertex.groups if elem.group != index]
            vertex.groups.append(_FakeElem(index, float(weight)))

    def weight_of(self, vertex_index: int) -> float:
        index = self._owner.vertex_groups._groups.index(self)
        for elem in self._owner.data.vertices[vertex_index].groups:
            if elem.group == index:
                return elem.weight
        return 0.0


class FakeGroups:
    """最小可用的 ``obj.vertex_groups`` 替身：支持 clear/new/len/索引/迭代。"""

    def __init__(self, owner):
        self._owner = owner
        self._groups = []

    def __len__(self):
        return len(self._groups)

    def __iter__(self):
        return iter(self._groups)

    def __getitem__(self, index):
        return self._groups[index]

    def clear(self):
        self._groups = []

    def new(self, name):
        group = FakeGroup(self._owner, name)
        self._groups.append(group)
        return group


class FakeObject:
    """``groups`` 形如 ``[("180", [(顶点下标, 权重), ...]), ...]``。

    只实现被迁移代码用到的那几个接口，行为与 Blender 一致：顶点用**下标**引用组，
    组被清空重建后下标随之变化。
    """

    type = "MESH"

    def __init__(self, name, groups):
        self.name = name
        vertex_count = 0
        for _name, entries in groups:
            for vertex_index, _weight in entries:
                vertex_count = max(vertex_count, vertex_index + 1)
        self.vertex_groups = FakeGroups(self)
        self.data = _FakeData([_FakeVertex(i, []) for i in range(vertex_count)])
        for group_name, entries in groups:
            group = self.vertex_groups.new(name=group_name)
            for vertex_index, weight in entries:
                group.add((vertex_index,), weight, 'REPLACE')


class ReadWorkspaceTests(unittest.TestCase):
    def test_reads_vgmap_per_part(self):
        with tempfile.TemporaryDirectory() as tmp:
            write_workspace(tmp, {"aaaa-1-0": {0: 10, 1: 11}, "bbbb-2-0": {0: 20}})
            maps = mod.read_part_local_maps(tmp)
            self.assertEqual(maps["aaaa-1-0"], {0: 10, 1: 11})
            self.assertEqual(maps["bbbb-2-0"], {0: 20})

    def test_missing_workspace_is_empty_not_an_error(self):
        self.assertEqual(mod.read_part_local_maps(os.path.join(tempfile.gettempdir(), "不存在")), {})
        self.assertEqual(mod.read_part_local_maps(""), {})


class BuildRemapTests(unittest.TestCase):
    def test_local_index_is_the_stable_key(self):
        """同一部件同一局部索引 = 同一根骨骼，跨两次 dump 成立。"""
        old = {"leg": {0: 209, 1: 210}, "body": {0: 181, 1: 264}}
        new = {"leg": {0: 32, 1: 33}, "body": {0: 4, 1: 80}}
        remap, conflicts = mod.build_global_id_remap(old, new)
        self.assertEqual(conflicts, [])
        self.assertEqual(remap, {209: 32, 210: 33, 181: 4, 264: 80})

    def test_parts_absent_from_the_old_dump_contribute_nothing(self):
        remap, conflicts = mod.build_global_id_remap({}, {"leg": {0: 32}})
        self.assertEqual(remap, {})
        self.assertEqual(conflicts, [])

    def test_same_part_absent_from_new_dump_contributes_nothing(self):
        remap, _ = mod.build_global_id_remap({"tail": {0: 185}}, {"leg": {0: 32}})
        self.assertEqual(remap, {})

    def test_conflicting_target_is_reported_and_not_silently_overwritten(self):
        """两次 dump 的同一部件局部索引对不上 = 不是同一份资产，必须报出来。"""
        old = {"a": {0: 5}, "b": {0: 5}}
        new = {"a": {0: 100}, "b": {0: 200}}
        remap, conflicts = mod.build_global_id_remap(old, new)
        self.assertEqual(len(conflicts), 1)
        self.assertIn("旧编号 5", conflicts[0])
        self.assertEqual(remap[5], 100)  # 先到者保留，绝不静默覆盖

    def test_shared_bones_across_parts_agree(self):
        """跨部件共享骨骼的交叉印证：同一旧编号在多个部件里必须指向同一新编号。"""
        old = {"cloth": {0: 180, 1: 181}, "body": {0: 181, 1: 264}}
        new = {"cloth": {0: 3, 1: 4}, "body": {0: 4, 1: 80}}
        remap, conflicts = mod.build_global_id_remap(old, new)
        self.assertEqual(conflicts, [])
        self.assertEqual(remap[181], 4)


class ClassifyTests(unittest.TestCase):
    """编号空间重叠是常态（旧 232 与新 232 都存在），只能靠"属于哪个部件"分开。"""

    OLD_VALUES = {"leg": set(range(180, 255)), "ribbon": set(range(398, 428))}
    NEW_VALUES = {"leg": set(range(3, 78)), "ribbon": set(range(225, 255))}
    NEW_SPACE = set(range(0, 255))

    def classify(self, used, part):
        return mod.classify_used_ids(
            set(used), part, self.OLD_VALUES, self.NEW_VALUES, self.NEW_SPACE
        )

    def test_id_beyond_new_workspace_is_definitely_stale(self):
        self.assertEqual(self.classify([264, 381], "body"), "stale")

    def test_leg_old_id_that_overlaps_new_range_is_still_stale(self):
        """旧 232 属于腿（180..254），新 232 属于飘带（225..254）；按部件判 = 旧。"""
        self.assertEqual(self.classify([200, 232, 233], "leg"), "stale")

    def test_same_numbers_under_the_new_part_are_current(self):
        """同样是 232/233，但这次它们落在"当前"飘全部件的范围内 → 不动。"""
        self.assertEqual(self.classify([225, 226, 232, 233], "ribbon"), "current")

    def test_merged_host_spanning_parts_is_stale(self):
        self.assertEqual(self.classify([180, 200, 264, 397], "body"), "stale")

    def test_unweighted_object_is_left_alone(self):
        self.assertEqual(self.classify([], "leg"), "current")

    def test_single_bone_part_uses_its_own_part_range(self):
        """眉毛只有一根骨头：旧 49 / 新 214。49 也落在新腿部件范围里，但按部件判 = 旧。"""
        old = dict(self.OLD_VALUES, brow={49})
        new = dict(self.NEW_VALUES, brow={214})
        verdict = mod.classify_used_ids({49}, "brow", old, new, self.NEW_SPACE)
        self.assertEqual(verdict, "stale")

    def test_unknown_part_falls_back_to_id_space(self):
        """物体被改名、取不到部件：只能保守判（编号越界才动）。"""
        self.assertEqual(self.classify([100], ""), "current")
        self.assertEqual(self.classify([300], ""), "stale")


class PlanTests(unittest.TestCase):
    OLD_VALUES = {"leg": set(range(180, 255)), "tail": set(range(185, 263))}
    NEW_VALUES = {"leg": set(range(3, 78))}
    NEW_SPACE = set(range(0, 255))

    def plan(self, used, part, remap):
        return mod.plan_object_migration(
            set(used), part, remap, self.OLD_VALUES, self.NEW_VALUES, self.NEW_SPACE
        )

    def test_fully_mappable_old_object_is_migrated(self):
        remap = {200: 23, 232: 55}
        self.assertEqual(self.plan([200, 232], "leg", remap)["action"], "migrate")

    def test_current_object_is_skipped(self):
        self.assertEqual(self.plan([3, 4], "leg", {})["action"], "skip")

    def test_part_absent_from_new_dump_is_blocked_not_half_migrated(self):
        """尾巴这次没提取 —— 改一半比不改更糟，必须拦住并点名。"""
        remap = {200: 23}
        result = self.plan([200, 256, 257], "tail", remap)
        self.assertEqual(result["action"], "blocked")
        self.assertEqual(result["unmapped"], [256, 257])


class HelpersTests(unittest.TestCase):
    def _two_workspaces(self, tmp):
        """新工作空间 leg(局部0->3) + 同名旧工作空间 leg(局部0->209)。"""
        current = os.path.join(tmp, "当前")
        previous = os.path.join(tmp, "上一次")
        write_workspace(current, {"leg": {0: 3, 1: 4}})
        write_workspace(previous, {"leg": {0: 209, 1: 210}})
        return current, previous

    def test_plan_scene_migration_splits_stale_and_current_objects(self):
        with tempfile.TemporaryDirectory() as tmp:
            current, _previous = self._two_workspaces(tmp)
            stale = FakeObject("LOD0.leg-1-0.腿.004", [("209", [(0, 1.0)]), ("210", [(0, 0.0)])])
            good = FakeObject("LOD0.leg-1-0.腿.003", [("3", [(0, 1.0)])])
            plan = mod.plan_scene_migration([stale, good], current)
            self.assertEqual(plan["status"], "ok")
            self.assertEqual([obj.name for obj, _used in plan["migrate"]], [stale.name])
            self.assertEqual(plan["skipped"], [good.name])
            self.assertEqual(plan["blocked"], [])

    def test_plan_scene_migration_reports_no_previous_workspace(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = os.path.join(tmp, "只有我")
            write_workspace(current, {"leg": {0: 3}})
            plan = mod.plan_scene_migration(
                [FakeObject("LOD0.leg-1-0.腿", [("209", [(0, 1.0)])])], current
            )
            self.assertEqual(plan["status"], "no_previous")
            self.assertEqual(mod.apply_scene_migration(plan), [])

    def test_auto_migrate_scene_is_a_noop_without_previous_workspace(self):
        """普通工程（没有"上一次工作空间"这个概念）不该被这次改动碰到。"""
        with tempfile.TemporaryDirectory() as tmp:
            current = os.path.join(tmp, "只有我")
            write_workspace(current, {"leg": {0: 3}})
            obj = FakeObject("LOD0.leg-1-0.腿", [("3", [(0, 1.0)])])
            self.assertEqual(mod.auto_migrate_scene([obj], current), [])
            self.assertEqual([g.name for g in obj.vertex_groups], ["3"])

    def test_auto_migrate_scene_rewrites_the_stale_object(self):
        with tempfile.TemporaryDirectory() as tmp:
            current, _previous = self._two_workspaces(tmp)
            stale = FakeObject("LOD0.leg-1-0.腿.004", [("209", [(0, 1.0)])])
            done = mod.auto_migrate_scene([stale], current)
            self.assertEqual(done, [stale.name])
            self.assertEqual([g.name for g in stale.vertex_groups], ["3"])

    def test_auto_migrate_scene_without_workspace_root(self):
        obj = FakeObject("LOD0.leg-1-0.腿", [("209", [(0, 1.0)])])
        self.assertEqual(mod.auto_migrate_scene([obj], ""), [])

    def test_collect_object_used_bone_ids_ignores_zero_weight_and_non_numeric(self):
        obj = FakeObject(
            "LOD0.leg-1-0.腿",
            [("180", [(0, 1.0), (1, 0.0)]), ("181", [(1, 1.0)]), ("随便", [(0, 1.0)])],
        )
        self.assertEqual(mod.collect_object_used_bone_ids(obj), {180, 181})

    def test_migrate_object_vertex_groups_merges_weights_that_land_on_one_bone(self):
        """两根旧骨骼迁移到同一个新编号时权重必须相加 —— 改名做不到这件事。"""
        obj = FakeObject(
            "LOD0.leg-1-0.腿",
            [("180", [(0, 0.25), (1, 1.0)]), ("181", [(0, 0.75)])],
        )
        info = mod.migrate_object_vertex_groups(obj, {180: 3, 181: 3})
        self.assertEqual(info["groups_after"], 1)
        self.assertEqual(obj.vertex_groups[0].name, "3")
        self.assertAlmostEqual(obj.vertex_groups[0].weight_of(0), 1.0)
        self.assertAlmostEqual(obj.vertex_groups[0].weight_of(1), 1.0)

    def test_migrate_object_vertex_groups_keeps_non_bone_groups(self):
        obj = FakeObject("LOD0.leg-1-0.腿", [("180", [(0, 1.0)]), ("随便", [(1, 1.0)])])
        mod.migrate_object_vertex_groups(obj, {180: 3})
        self.assertEqual(sorted(g.name for g in obj.vertex_groups), ["3", "随便"])

    def test_split_part_key(self):
        self.assertEqual(mod.split_part_key("LOD0.c209c22b-45087-0.身体.001"), "c209c22b-45087-0")
        self.assertEqual(mod.split_part_key("LOD0.4a178546-18468-0.腿.003"), "4a178546-18468-0")
        self.assertEqual(mod.split_part_key("CBOGZZZ少女素体匹配20250713.001"), "")
        self.assertEqual(mod.split_part_key(""), "")

    def test_remap_group_ids_keeps_non_bone_groups(self):
        result = mod.remap_group_ids(["180", "181", "随便"], {180: 3, 181: 4})
        self.assertEqual(result[0], 3)
        self.assertEqual(result[1], 4)
        self.assertIsNone(result[2])

    def test_remap_group_ids_passes_through_unmapped_numbers(self):
        result = mod.remap_group_ids(["180", "999"], {180: 3})
        self.assertEqual(result[0], 3)
        self.assertEqual(result[1], 999)

    def test_discover_previous_workspaces_picks_the_sibling_with_shared_parts(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = os.path.join(tmp, "当前")
            sibling = os.path.join(tmp, "上一次")
            unrelated = os.path.join(tmp, "无关")
            write_workspace(current, {"leg": {0: 32}, "body": {0: 4}})
            write_workspace(sibling, {"leg": {0: 209}, "body": {0: 181}, "tail": {0: 185}})
            write_workspace(unrelated, {"zzzz": {0: 1}})
            found = mod.discover_previous_workspaces(current)
            self.assertEqual([os.path.basename(path) for path, _ in found], ["上一次"])
            self.assertEqual(found[0][1], 2)

    def test_discover_previous_workspaces_without_siblings(self):
        with tempfile.TemporaryDirectory() as tmp:
            current = os.path.join(tmp, "only")
            write_workspace(current, {"leg": {0: 32}})
            self.assertEqual(mod.discover_previous_workspaces(current), [])


if __name__ == "__main__":
    unittest.main()
