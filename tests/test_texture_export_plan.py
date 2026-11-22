# -*- coding: utf-8 -*-
"""「贴图落盘前联合去重」计划器单测。

覆盖需求里的关键场景：

- 多个物体 / 材质名不同但指向同一份贴图文件（同源复用）；
- 材质名不同、源文件不同但图像内容相同（MD5 / 像素级 / 相似度复用）；
- 不同材质类型用同一张源图（落盘规格不同，各自落盘，绝不合并）；
- 「清除未指定部件贴图」取消未落盘计划（drop）；
- 「覆盖现有资源」智能覆盖：旧文件同内容不覆盖、不同才覆盖；
- finalize 幂等、只落盘一次；关闭开关时 register 返回 None（调用方走旧路径）。
"""

import importlib.util
import os
from pathlib import Path
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

import numpy as np


def _install_module(name, **attrs):
    module = types.ModuleType(name)
    for key, value in attrs.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


PKG = "_texture_plan_test_pkg"
for package_name in (PKG, f"{PKG}.common", f"{PKG}.blueprint", f"{PKG}.toolkit"):
    package = _install_module(package_name)
    package.__path__ = []

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.modules[f"{PKG}.toolkit"].__path__ = [os.path.join(ROOT, "toolkit")]


def _load(name, relative_path):
    spec = importlib.util.spec_from_file_location(name, os.path.join(ROOT, relative_path))
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


dedup = _load(f"{PKG}.common.texture_dedup", os.path.join("common", "texture_dedup.py"))
setattr(sys.modules[f"{PKG}.common"], "texture_dedup", dedup)
plan_module = _load(
    f"{PKG}.common.texture_export_plan", os.path.join("common", "texture_export_plan.py")
)
TextureExportPlan = plan_module.TextureExportPlan


def _pixels(color=(16, 32, 48, 255), size=(8, 8)):
    return np.full((size[1], size[0], 4), color, dtype=np.uint8)


class TextureExportPlanTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="texture_plan_")
        self.textures = os.path.join(self.tmp, "Textures")
        os.makedirs(self.textures, exist_ok=True)
        self.source_dir = os.path.join(self.tmp, "source")
        os.makedirs(self.source_dir, exist_ok=True)
        TextureExportPlan.reset()
        TextureExportPlan._converter_override = None
        self._original_load = dedup.load_rgba8

    def tearDown(self):
        dedup.load_rgba8 = self._original_load
        TextureExportPlan.reset()
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- 工具 ---------------------------------------------------------------

    def _source(self, name, data):
        path = os.path.join(self.source_dir, name)
        with open(path, "wb") as handle:
            handle.write(data)
        return path

    def _configure(self, **kwargs):
        kwargs.setdefault("mod_export_path", self.tmp)
        TextureExportPlan.configure(True, **kwargs)

    def _registered(self, source_path, filename, owner="base", **kwargs):
        return TextureExportPlan.register(
            source_path,
            self.textures,
            filename,
            owner=owner,
            **kwargs,
        )

    def _files(self):
        return sorted(os.listdir(self.textures))

    # -- 复用 ---------------------------------------------------------------

    def test_same_source_reused(self):
        """同一份贴图文件被多个物体引用：只登记一次、只落盘一份。"""
        self._configure()
        source = self._source("shared.png", b"same-bytes")
        first = self._registered(source, "aaa.png")
        second = self._registered(source, "bbb.png")
        self.assertEqual(first, "aaa.png")
        self.assertEqual(second, "aaa.png")
        TextureExportPlan.finalize(self.tmp)
        self.assertEqual(self._files(), ["aaa.png"])

    def test_md5_reuse_different_names(self):
        """材质名不同、源文件不同但内容相同：合并成一份（用户关心的场景）。"""
        self._configure()
        first_source = self._source("body_a.png", b"identical-image-bytes")
        second_source = self._source("body_b.png", b"identical-image-bytes")
        first = self._registered(first_source, "DiffuseMap_body_a.png", owner="material")
        second = self._registered(second_source, "DiffuseMap_body_b.png", owner="material")
        self.assertEqual(first, second)
        TextureExportPlan.finalize(self.tmp)
        self.assertEqual(self._files(), ["DiffuseMap_body_a.png"])

    def test_pixel_reuse_different_encoding(self):
        """字节不同、像素完全相同：像素级去重命中。"""
        self._configure(pixel_dedup=True)
        first_source = self._source("a.bin", b"encode-one")
        second_source = self._source("b.bin", b"encode-two")
        dedup.load_rgba8 = lambda _path: (_pixels(), (8, 8))
        first = self._registered(first_source, "a.png")
        second = self._registered(second_source, "b.png")
        self.assertEqual(first, second)
        TextureExportPlan.finalize(self.tmp)
        self.assertEqual(self._files(), ["a.png"])

    def test_similarity_reuse_and_threshold(self):
        """相似度去重：同尺寸相近图像命中；阈值 100 时不再命中。"""
        first_source = self._source("sim_a.bin", b"aaa")
        second_source = self._source("sim_b.bin", b"bbb")
        near = _pixels((30, 30, 30, 255))
        near_but_not_same = _pixels((31, 30, 30, 255))

        def _fake_load(path):
            if path.endswith("sim_a.bin"):
                return near, (8, 8)
            return near_but_not_same, (8, 8)

        dedup.load_rgba8 = _fake_load
        self._configure(similarity_dedup=True, threshold=90)
        first = self._registered(first_source, "a.png")
        second = self._registered(second_source, "b.png")
        self.assertEqual(first, second)

        TextureExportPlan.reset()
        self._configure(similarity_dedup=True, threshold=100)
        first = self._registered(first_source, "a.png")
        second = self._registered(second_source, "b.png")
        self.assertNotEqual(first, second)

    def test_spec_isolated_same_source(self):
        """不同落盘规格（漫反射复制 vs 法线转 DDS）同源也必须各自落盘。"""
        self._configure()
        source = self._source("shared.dds", b"same-bytes")
        copy_name = self._registered(source, "copy.dds", owner="base")
        converted_name = self._registered(
            source,
            "normal.dds",
            owner="material",
            spec=("dds", "NormalMap", ""),
            convert_kwargs={"texture_type": "NormalMap"},
        )
        self.assertNotEqual(copy_name, converted_name)
        TextureExportPlan.finalize(self.tmp)
        self.assertEqual(len(self._files()), 2)

    # -- drop / 智能覆盖 ------------------------------------------------------

    def test_drop_by_ini_filename(self):
        """清除未指定部件贴图：取消计划项后不再落盘。"""
        self._configure()
        source = self._source("drop.png", b"drop-me")
        self._registered(source, "drop.png")
        dropped = TextureExportPlan.drop_by_ini_filename("Textures/drop.png")
        self.assertEqual(dropped, 1)
        TextureExportPlan.finalize(self.tmp)
        self.assertEqual(self._files(), [])

    def test_existing_same_content_not_overwritten(self):
        """上次导出留下的同名同内容文件：不覆盖、不重转。"""
        target = os.path.join(self.textures, "keep.png")
        with open(target, "wb") as handle:
            handle.write(b"same-content")
        self._configure()
        source = self._source("keep_source.png", b"same-content")
        self._registered(source, "keep.png", owner="material", overwrite_policy="overwrite")
        TextureExportPlan.finalize(self.tmp)
        self.assertEqual(TextureExportPlan.stats()["skip_same_content"], 1)
        self.assertEqual(TextureExportPlan.stats()["written"], 0)

    def test_existing_different_content_overwrite_policy(self):
        """旧文件内容不同：覆盖开关决定是否覆盖。"""
        target = os.path.join(self.textures, "target.png")
        with open(target, "wb") as handle:
            handle.write(b"old-content")
        self._configure()
        source = self._source("new_source.png", b"new-content")

        self._registered(source, "target.png", owner="material", overwrite_policy="skip")
        TextureExportPlan.finalize(self.tmp)
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"old-content")

        TextureExportPlan.reset()
        self._configure()
        self._registered(source, "target.png", owner="material", overwrite_policy="overwrite")
        TextureExportPlan.finalize(self.tmp)
        with open(target, "rb") as handle:
            self.assertEqual(handle.read(), b"new-content")

    # -- finalize -----------------------------------------------------------

    def test_finalize_idempotent(self):
        """finalize 幂等：第二次不再重复写盘。"""
        self._configure()
        source = self._source("once.png", b"once")
        self._registered(source, "once.png")
        TextureExportPlan.finalize(self.tmp)
        written_first = TextureExportPlan.stats()["written"]
        TextureExportPlan.finalize(self.tmp)
        self.assertEqual(TextureExportPlan.stats()["written"], written_first)
        self.assertEqual(self._files(), ["once.png"])

    def test_rgb_png_copy_is_materialized_as_rgba(self):
        from PIL import Image

        self._configure()
        source = os.path.join(self.source_dir, "rgb_source.png")
        Image.new("RGB", (4, 3), (70, 130, 190)).save(source, format="PNG")
        self._registered(source, "rgb_output.png", owner="material")
        TextureExportPlan.finalize(self.tmp)

        with Image.open(os.path.join(self.textures, "rgb_output.png")) as exported:
            self.assertEqual(exported.mode, "RGBA")
            self.assertEqual(exported.getpixel((0, 0)), (70, 130, 190, 255))

    def test_same_path_png_copy_does_not_rewrite_source(self):
        from PIL import Image

        source = os.path.join(self.textures, "same.png")
        Image.new("RGB", (4, 3), (70, 130, 190)).save(source, format="PNG")
        self._configure()
        self._registered(source, "same.png", owner="material", overwrite_policy="overwrite")
        TextureExportPlan.finalize(self.tmp)

        with Image.open(source) as untouched:
            self.assertEqual(untouched.mode, "RGB")

    def test_convert_failure_fallback_rewrites_ini(self):
        """转 DDS 失败：回退源扩展名并重写 ini 的 filename。"""
        ini_path = os.path.join(self.tmp, "test.ini")
        with open(ini_path, "w", encoding="utf-8") as handle:
            handle.write("[Resource_Test]\nfilename = Textures/body.dds\n\n")

        self._configure()
        source = self._source("body.png", b"png-bytes")
        TextureExportPlan._converter_override = lambda *_args, **_kwargs: (False, "boom")
        self._registered(
            source,
            "body.dds",
            owner="material",
            spec=("dds", "DiffuseMap", ""),
            convert_kwargs={"texture_type": "DiffuseMap"},
        )
        TextureExportPlan.finalize(self.tmp)

        with open(ini_path, "r", encoding="utf-8") as handle:
            content = handle.read()
        self.assertIn("filename = Textures/body.png", content)
        self.assertTrue(os.path.exists(os.path.join(self.textures, "body.png")))

    def test_convert_failure_fallback_rgb_png_is_rgba(self):
        from PIL import Image

        self._configure()
        source = os.path.join(self.source_dir, "body_rgb.png")
        Image.new("RGB", (4, 3), (70, 130, 190)).save(source, format="PNG")
        TextureExportPlan._converter_override = lambda *_args, **_kwargs: (False, "boom")
        self._registered(
            source,
            "body.dds",
            owner="material",
            spec=("dds", "DiffuseMap", ""),
            convert_kwargs={"texture_type": "DiffuseMap"},
        )
        TextureExportPlan.finalize(self.tmp)

        with Image.open(os.path.join(self.textures, "body.png")) as exported:
            self.assertEqual(exported.mode, "RGBA")

    def test_disabled_register_returns_none(self):
        """未启用（蓝图无资源合并节点 / 开关关闭）：register 不参与，调用方走旧路径。"""
        TextureExportPlan.configure(False, mod_export_path=self.tmp)
        source = self._source("off.png", b"off")
        self.assertIsNone(self._registered(source, "off.png"))
        self.assertEqual(self._files(), [])

    def test_same_type_same_content_merged_across_groups(self):
        """同类型内：不同文件名/不同组，只要内容判定重复就合并成同一份文件。

        用户规则：同类型（如都是 DiffuseMap）内容相同就合并，不看源文件名/MD5/格式；
        不同切换组之间也照样合并。
        """
        self._configure()
        first = self._registered(
            self._source("face_a.bin", b"same-face-bytes"),
            "face_a.png",
            owner="material",
            spec=("dds", "DiffuseMap"),
            dedup_group="LOD0.face|DiffuseMap|$swapkey150",
        )
        second = self._registered(
            self._source("face_b.bin", b"same-face-bytes"),
            "face_b.png",
            owner="material",
            spec=("dds", "DiffuseMap"),
            dedup_group="LOD0.body|DiffuseMap|$swapkey151",
        )
        self.assertEqual(second, first)
        TextureExportPlan.finalize(self.tmp)
        self.assertEqual(self._files(), ["face_a.png"])

    def test_same_type_similarity_merged_by_setting(self):
        """同类型 + 相似度模式：判定为重复（≥阈值）就合并，文件名/MD5 不同也一样。"""
        first_source = self._source("makeup_a.bin", b"makeup-a")
        second_source = self._source("makeup_b.bin", b"makeup-b")
        near = _pixels((30, 30, 30, 255))
        near_but_not_same = _pixels((31, 30, 30, 255))

        def _fake_load(path):
            if path.endswith("makeup_a.bin"):
                return near, (8, 8)
            return near_but_not_same, (8, 8)

        dedup.load_rgba8 = _fake_load
        self._configure(similarity_dedup=True, threshold=90)
        first = self._registered(
            first_source, "a.dds", owner="material", spec=("dds", "DiffuseMap")
        )
        second = self._registered(
            second_source, "b.dds", owner="material", spec=("dds", "DiffuseMap")
        )
        self.assertEqual(second, first)

    def test_cross_type_never_merged(self):
        """跨类型绝不合并：内容/像素完全相同也不能共用同一个文件。

        实机理由：LightMap 是线性贴图、MaterialMap 是 sRGB，同一张图用错通道会偏色。
        """
        self._configure(pixel_dedup=True)
        source_a = self._source("shared_a.bin", b"identical-bytes")
        source_b = self._source("shared_b.bin", b"identical-bytes")
        dedup.load_rgba8 = lambda _path: (_pixels(), (8, 8))

        light = self._registered(
            source_a, "light.dds", owner="material", spec=("dds", "LightMap")
        )
        material = self._registered(
            source_b, "material.dds", owner="material", spec=("dds", "MaterialMap")
        )
        self.assertNotEqual(light, material)
        TextureExportPlan.finalize(self.tmp)
        self.assertEqual(sorted(self._files()), ["light.dds", "material.dds"])

    def test_owner_constants_exposed_on_class(self):
        """调用方写 TextureExportPlan.OWNER_*：类上必须有同名字面量。"""
        self.assertEqual(TextureExportPlan.OWNER_BASE, plan_module.OWNER_BASE)
        self.assertEqual(TextureExportPlan.OWNER_MATERIAL, plan_module.OWNER_MATERIAL)
        self._configure()
        source = self._source("owner.png", b"owner")
        filename = TextureExportPlan.register(
            source,
            self.textures,
            "owner.png",
            owner=TextureExportPlan.OWNER_MATERIAL,
        )
        self.assertEqual(filename, "owner.png")


    def test_dds_workers_use_measured_native_limits_and_numpy_fallback(self):
        name = f"{PKG}.common.dds_pixel"
        module = types.ModuleType(name)
        with mock.patch.dict(sys.modules, {name: module}):
            with mock.patch.object(plan_module.os, "cpu_count", return_value=6):
                module.native_backend_name = lambda: "C-BC7"
                self.assertEqual(plan_module._dds_analysis_worker_limit(), 16)
                self.assertEqual(plan_module._dds_analysis_worker_limit(True), 32)
                module.native_backend_name = lambda: "numpy"
                self.assertEqual(plan_module._dds_analysis_worker_limit(), 2)
            with mock.patch.object(plan_module.os, "cpu_count", return_value=1):
                self.assertEqual(plan_module._dds_analysis_worker_limit(), 1)


    def test_batch_success_is_not_reencoded_and_failure_uses_original_converter(self):
        TextureExportPlan.configure(True)
        entries = []
        for index in (1, 2):
            source = self._source(f"source_{index}.png", bytes([index]))
            entries.append(plan_module.PlanEntry(index, source, self.textures,
                           f"target_{index}.dds", plan_module.OWNER_MATERIAL, "",
                           ("dds", "DiffuseMap"), None, convert_kwargs={"force": True}))
        TextureExportPlan._entries = entries
        module = types.ModuleType(f"{PKG}.toolkit.tt_dds_conversion")
        def batch(requests, progress):
            with open(requests[0][2], "wb") as handle:
                handle.write(b"batch")
            progress(1)
            return {requests[0][0]}
        module.convert_new_textures_to_dds_batch = batch
        def single(source, target, **kwargs):
            with open(target, "wb") as handle:
                handle.write(b"single")
            return True, ""
        with mock.patch.dict(sys.modules, {module.__name__: module}), \
                mock.patch.object(plan_module, "_load_converter", return_value=single) as loader:
            stats = TextureExportPlan.finalize(self.tmp)
        self.assertEqual(stats["written"], 2)
        self.assertEqual(Path(self.textures, "target_1.dds").read_bytes(), b"batch")
        self.assertEqual(Path(self.textures, "target_2.dds").read_bytes(), b"single")
        self.assertEqual(loader.call_count, 1)
        self.assertTrue(all(entry.materialized for entry in entries))


if __name__ == "__main__":
    unittest.main()
