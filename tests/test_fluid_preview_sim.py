"""``fluid_preview_sim`` 的数值健壮性回归。

背景（真实事故）：给发射器加一个半径 0.2 m、强度 3.0 的径向力场后，第 11 帧
有 217 个粒子变成 ``nan``，并且一路传染。根本原因不在力场，而在邻居求解里那句
``direction = (pred[i] - pred[j]) / r``：两个粒子位置完全重合时 ``r`` 是 0，
除以它得到 NaN。着色器侧用的是乘法形式的 ``rsqrt(max(r2, 1e-12))``，天生不出
这个 NaN——预览要和游戏一致，这里也必须一致。

这些用例不需要 Blender，纯 numpy。
"""

import os
import sys

import numpy as np
import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from blueprint import fluid_preview_sim as S  # noqa: E402


def _emitter(**kw):
    base = {
        'origin': [0.0, 0.0, 1.0],
        'direction': [0.0, 0.0, -1.0],
        'up': [0.0, 1.0, 0.0],
        'speed': S.DEFAULT_SPEED,
        'speed_scale': 1.0,
        'width': 2.0,
        'slot': 0,
        'level': 3,
    }
    base.update(kw)
    return base


class TestSafeRadius:
    def test_floor_matches_shader_epsilon(self):
        assert S._safe_r(0.0) == pytest.approx(1e-6, rel=1e-6)

    def test_normal_distance_untouched(self):
        assert S._safe_r(0.0025) == pytest.approx(0.05)

    def test_negative_rounding_noise_is_absorbed(self):
        assert np.isfinite(S._safe_r(-1e-18))


class TestCoincidentParticles:
    def test_density_terms_finite_for_duplicate_points(self):
        sim = S.Simulator(8, [_emitter()])
        points = np.zeros((3, 3), dtype=np.float32)
        points[1] = points[0]
        points[2] = [0.0, 0.0, 0.001]
        density, grad_sum, grad_sq_sum = sim._density_terms(points)
        for array in (density, grad_sum, grad_sq_sum):
            assert np.isfinite(np.asarray(array, dtype=np.float64)).all()

    def test_correction_finite_for_duplicate_points(self):
        sim = S.Simulator(8, [_emitter()])
        points = np.zeros((2, 3), dtype=np.float32)
        lam = np.zeros(2, dtype=np.float32)
        out = sim._correction(points, lam)
        assert np.isfinite(np.asarray(out, dtype=np.float64)).all()


class TestForceField:
    def test_radial_at_exact_center_is_finite(self):
        points = np.zeros((4, 3), dtype=np.float64)
        fields = [{
            'kind': 'RADIAL', 'center': [0.0, 0.0, 0.0], 'axis': [0.0, 0.0, 1.0],
            'radius': 0.2, 'strength': 3.0, 'falloff': 2.0, 'enabled': True,
        }]
        accel = S.force_field_accel(points, fields, 1.0)
        assert np.isfinite(accel).all()

    def test_vortex_on_axis_is_finite(self):
        points = np.array([[0.0, 0.0, 0.02], [0.0, 0.0, 0.0]], dtype=np.float64)
        fields = [{
            'kind': 'VORTEX', 'center': [0.0, 0.0, 0.0], 'axis': [0.0, 0.0, 1.0],
            'radius': 0.2, 'strength': 3.0, 'falloff': 2.0, 'enabled': True,
        }]
        assert np.isfinite(S.force_field_accel(points, fields, 1.0)).all()

    def test_falloff_is_zero_outside_radius(self):
        assert S.force_field_falloff(0.04, 0.04, 2.0) == 0.0
        assert S.force_field_falloff(0.09, 0.04, 2.0) == 0.0

    def test_falloff_matches_power_curve(self):
        # r² = R²/4 ⇒ t = 0.75 ⇒ t² = 0.5625
        assert S.force_field_falloff(0.01, 0.04, 2.0) == pytest.approx(0.5625)

    def test_strong_radial_field_keeps_positions_finite(self):
        """端到端：强径向力场下模拟出来的位置不能出现 NaN。"""
        field = [{
            'kind': 'RADIAL', 'center': [0.05, 0.0, 0.95], 'axis': [0.0, 0.0, -1.0],
            'radius': 0.2, 'strength': 3.0, 'falloff': 2.0, 'enabled': True,
        }]
        sim = S.Simulator(256, [_emitter()], ground_z=0.0, force_fields=field)
        for _ in range(20):
            sim.step(1.0 / 30.0, np.random.default_rng(7))
        assert np.isfinite(sim.pos).all()
