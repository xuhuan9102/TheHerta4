# -*- coding: utf-8 -*-
"""Position Based Fluids 的预览实现。

逐行对照参考实现翻译，每个常量都标了出处：

- ``C:\\Users\\anlingQWQ\\Desktop\\LiquidGravityProbe\\fluid\\FluidSolver.hlsl``
- ``C:\\Users\\anlingQWQ\\Desktop\\LiquidGravityProbe\\Probe.ini``

只依赖 numpy，**不 import bpy**——这样能在 pytest 里直接跑，也能在
``blender --background`` 里跑。Blender 侧只负责把结果变成关键帧。

与参考实现的差异（都是有意的，列出来备查）：

1. **槽位/多发射器合并**：参考实现按 slot 分区，这里把所有发射器的粒子放进
   一个大数组，用 ``slot_id`` 区分。邻居搜索跨 slot 也允许——在预览里
   两个喷口的水本来就该互相碰。
2. **JetGuide 的聚焦/弹道分支**：参考实现在 ``FluidSolver.hlsl:288-301``
   有一条被 ``longLane.w < -2`` 控制的独立弹道，只对 ``velocityAlive.w > .5``
   的“叶片几何”生效。预览里没有叶片释放，直接跳过。
3. **SheetProfile 的片状恢复力**（``:390-401``）：依赖 ``sheetFrame``，
   预览里没有出水口片状信息，跳过。
4. **Splash 子系统**（撞击后生成子水滴）：单独一个 CS 池，预览里用
   简化版——撞上表面后粒子黏住并缩短寿命，不做二次溅射。
"""

import numpy as np

# --- 常量 -------------------------------------------------------------------

#: 核半径。``Probe.ini:419`` 的 ``x238 = 0.012 ...`` 第一个分量。
H = 0.012
#: 单粒子质量。``Probe.ini:419`` 第二分量。
MASS = 2.1333e-5
#: 静止密度。``Probe.ini:419`` 第三分量。
RHO = 1000.0
#: 运动黏度。``Probe.ini:419`` 第四分量。
NU = 1e-6
#: 重力。参考实现是 ``FluidSolver.hlsl:33`` 的 ``G = float3(0,-9.81,0)``——
#: 那是 Unity 的 **Y 向上**坐标系。Blender 是 **Z 向上**，所以这里把同一个 9.81
#: 放到 Z 轴上。移植时没换轴的话，粒子会朝侧面掉、而地面判定（``ground_z``
#: 读的是 ``pred[:,2]``）永远不触发：实测帧 89 的最低点是 z=1.112，
#: 而 ``ground_z=0.62`` 一次都没命中。
GRAVITY = np.array([0.0, 0.0, -9.81], dtype=np.float32)
#: 粒子半径，用于碰撞与渲染尺寸。``ParticleConfig.hlsl`` 的 FLUID_RADIUS。
FLUID_RADIUS = 0.0028
#: 贴地滑动时的切向重力缩放。``fluid/Collision.hlsl:61`` 的 ``9.81*(0.35/18)``。
#: 参考实现把它乘在切向重力上，让水顺着斜面向下溜。
# nhd-pick: 面板可调，和 game 侧 IniParams[47].x/.y 一一对应
_GRAVITY_SCALE = 1.0
_VISCOSITY_OVERRIDE = 0.0


def _gravity():
    """重力加速度（受面板倍率影响）。"""
    return GRAVITY * _GRAVITY_SCALE


def _nu():
    """运动粘度；<=0 时沿用参考实现的值。"""
    return _VISCOSITY_OVERRIDE if _VISCOSITY_OVERRIDE > 0.0 else NU


def set_physics(gravity_scale=None, viscosity=None):
    """设置重力倍率与粘度覆盖值。

    ``None`` = 不改（保持上一次的值）；``0`` = **真的零重力**（与修后的游戏
    ``GSCALE`` 同义：0 是合法值，不是「用默认」—— 这一行以前写错了）。
    粘度同理：``<=0`` 表示沿用 profile 里的值（那个 0 语义是有意的）。
    """
    global _GRAVITY_SCALE, _VISCOSITY_OVERRIDE
    if gravity_scale is not None:
        _GRAVITY_SCALE = float(gravity_scale)   # 允许负值：负 = 反重力（火焰上飘）
    if viscosity is not None:
        _VISCOSITY_OVERRIDE = max(float(viscosity), 0.0)


TANGENT_GRAVITY_SCALE = 0.35 / 18.0
#: 贴地滑动的速度衰减。``fluid/Collision.hlsl:69`` 的 ``drag = 8``。
SURFACE_DRAG = 8.0
#: 粒子寿命（秒）。``FluidSolver.hlsl:58`` 的 lifetime。
#: 粒子寿命（秒）。
#:
#: ``fluid\ParticleConfig.hlsl:22``：``PRIMARY_LIFETIME = 1.1``，注释是
#: ``// nhd-pick: reaches the floor from chest height (was .48)``。
#: 我先前用的是 ``FluidSolver.hlsl:58`` 的旧值 ``.48``——那是被 nhd-pick
#: 改掉之前的版本，用它水柱只飞得出 0.51*0.48 ≈ 0.24 m，落不到地。
LIFETIME = 1.1
#: 速度上限。``FluidSolver.hlsl:440`` ``velocity*=min(1,6/max(speed,1e-9))``。
MAX_SPEED = 6.0
#: 单次修正的最大位移。``FluidSolver.hlsl:342`` ``min(1,.002/max(len,1e-9))``。
CORRECTION_CLAMP = 0.002
#: lambda 分母的正则项。``FluidSolver.hlsl:322`` ``+10``。
LAMBDA_EPS = 10.0
#: 每次子步的修正迭代次数。``Probe.ini`` 的 FluidSubstep 调 3 次。
ITERATIONS = 3
#: 内聚强度。``FluidSolver.hlsl:418`` ``cohesion*1.2``。
COHESION_SCALE = 1.2
#: 速度平滑强度。``FluidSolver.hlsl:412`` ``smoothVelocity*.01``。
SMOOTHING_SCALE = 0.01
#: 参考实现的主发射率（个/秒），**只作文档**，代码里不引用。
#:
#: 这里先前照抄了 ``fluid\SoftNozzle.hlsl:48`` 的 ``creatures_per_second =
#: 11200``，那是**飞溅（Splash）**的参数，不是主流体。
#: 主流体是"排班重发"模型（``FluidSolver.hlsl:93-99``）：
#: 每槽 ``PRIMARY_PER_SLOT = 1024`` 个粒子在一个 ``PRIMARY_LIFETIME`` 的周期里
#: 均匀错峰出生，所以每秒出生 ``1024 / 1.1 ≈ 931`` 个。
#: 照 11200/s 发会让密度高 12 倍，PBF 把粒子互相推开 ⇒ 水柱被压成一小坨。
#:
#: 预览侧真正用的发射率由 ``capacity`` 推出（见 ``Simulator.__init__`` 的
#: ``emit_per_frame``）：``capacity`` 一变，排班周期里的粒子数就变，发射率
#: 必须跟着变，否则多出来的槽永远排不上队。
REFERENCE_EMIT_PER_SECOND = 1024.0 / 1.1
#: 主喷速（米/秒）。
#:
#: 早前照 ``SoftNozzle.hlsl:49`` 的 ``speed = 1.4 + creep*.5`` 取了 1.4，那是
#: **喷嘴驱动强度**，不是出水速度。真正的初速是 ``:33`` 的 ``n.speed``：
#: ``.3807886553 * sqrt(pressure*ripple) * discharge/referenceDischarge * jetBoost``。
#: 默认档 ``setting = 1.69`` 代进去得 ``≈ 0.514``。
DEFAULT_SPEED = 0.514
#: 喷口圆盘半径。``fluid\SoftNozzle.hlsl:50`` 的 ``radius = .0015``。
NOZZLE_RADIUS = 0.0015

#: dt 的钳制区间。``Probe.ini:412-424`` 把帧时钳在 [1/60, 1/30] 再除 2。
DT_MIN = 1.0 / 60.0
DT_MAX = 1.0 / 30.0

#: 节点把「全局档位 + 本发射器偏移」一起塞进 ``emitter['level']``（node:3170），
#: 而全局档位 ``strength_level`` 在面板上不暴露（默认 3）、导出也根本不用它
#: （``node:1448`` 写的是 ``level_for_speed(launch_speed)``）⇒ 本发射器的相对偏移
#: 就是 ``level - 3``。等 node 改成直接传 ``level_offset`` 之后删掉这个基线。
STRENGTH_LEVEL_REFERENCE = 3.0

_INI_MERGE = None


def _ini_merge():
    """``fluid_ini_merge``（纯函数模块，不依赖 bpy）。

    档位↔速度的换算只认它里面那一个 ``level_for_speed``，**不在这里写第二套**：
    导出时写 ``$strength_level`` 用的就是同一个函数（``node:1448``），所以预览与
    游戏拿到的是同一个数。

    包内 import 走相对导入；对拍脚本用 ``spec_from_file_location`` 单独加载本模块时
    没有包上下文，退回按同目录路径加载 —— 两条路指向同一个文件。
    """
    global _INI_MERGE
    if _INI_MERGE is None:
        try:
            from . import fluid_ini_merge as module
        except Exception:                      # noqa: BLE001 - 独立加载时的回退
            import importlib.util
            import os
            path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                'fluid_ini_merge.py')
            spec = importlib.util.spec_from_file_location('fluid_ini_merge', path)
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        _INI_MERGE = module
    return _INI_MERGE


_PI = 3.14159265359
_POLY6_COEF = 315.0 / (64.0 * _PI * H ** 3)
_GRAD_COEF = -45.0 / (_PI * H ** 4)
_LAP_COEF = 45.0 / (_PI * H ** 5)

#: 近邻对搜索：有 scipy 就用 cKDTree（1024 粒子约 5ms/帧），
#: 没有就退回全对全（约 300ms/帧）。Blender 内嵌的 Python 没有 scipy，
#: 所以完整模拟建议在外部 Python 里跑、把结果存成 ``.npz`` 再导入。
try:  # pragma: no cover - 环境相关
    from scipy.spatial import cKDTree as _KDTree
except ImportError:  # pragma: no cover
    _KDTree = None


def _safe_r(r2):
    """``sqrt(max(r², 1e-12))`` —— 与 ``FluidSolver.hlsl`` 的 ``rsqrt(max(·,1e-12))`` 同义。

    两个粒子位置完全重合时 ``r=0``，直接拿它做除数会得到 NaN，而 NaN 一旦进
    位置就会被 ``MAX_SPEED`` 钳制原封不动地留下来、再污染整片邻居。着色器那边
    因为用的是乘法形式的 ``rsqrt``，天然不会出这个 NaN；这里是等价的写法。
    """
    return np.sqrt(np.maximum(r2, 1e-12))


def neighbour_pairs(points):
    """返回 ``(i, j, r, r2)``，覆盖所有 ``r < H`` 的粒子对（``i < j``，不重复）。"""
    n = points.shape[0]
    empty = np.zeros(0, dtype=np.int64)
    if n < 2:
        return empty, empty, np.zeros(0), np.zeros(0)

    if _KDTree is not None:
        pairs = _KDTree(points).query_pairs(H, output_type='ndarray')
        if pairs.size == 0:
            return empty, empty, np.zeros(0), np.zeros(0)
        i = pairs[:, 0].astype(np.int64)
        j = pairs[:, 1].astype(np.int64)
    else:
        diff = points[:, None, :] - points[None, :, :]
        r2_all = np.einsum('ijk,ijk->ij', diff, diff)
        np.fill_diagonal(r2_all, np.inf)
        i, j = np.nonzero(r2_all < H * H)

    diff = points[i] - points[j]
    r2 = np.einsum('ij,ij->i', diff, diff)
    return i, j, np.sqrt(r2), r2




def _scatter_sum(index, values, size):
    """把 ``values`` 按 ``index`` 累加成长度为 ``size`` 的标量数组。

    ``np.add.at`` 在 Python 层循环，慢；``np.bincount`` 走 C，快一个数量级。
    """
    if index.size == 0:
        return np.zeros(size)
    return np.bincount(index, weights=values, minlength=size)[:size]


def _scatter_sum_vec(index, values, size):
    """``values`` 是 ``(M, 3)`` 时逐分量累加。"""
    out = np.zeros((size, values.shape[1] if values.ndim > 1 else 1))
    if index.size == 0:
        return out if values.ndim > 1 else out[:, 0]
    if values.ndim == 1:
        return _scatter_sum(index, values, size)
    for c in range(values.shape[1]):
        out[:, c] = _scatter_sum(index, values[:, c], size)
    return out




def _poly6(r2):
    """``FluidSolver.hlsl:32``。"""
    q = 1.0 - r2 / (H * H)
    np.clip(q, 0.0, None, out=q)
    return _POLY6_COEF * q * q * q


def _gradient(d, r):
    """``FluidSolver.hlsl:33``：``-45/(πH⁴)(1-r/H)² * d/r``。"""
    q = 1.0 - r / H
    np.clip(q, 0.0, None, out=q)
    scale = _GRAD_COEF * q * q / np.maximum(r, 1e-7)
    return d * scale[:, None]


def _laplacian(r):
    """``FluidSolver.hlsl:34``：``45/(πH⁵)(1-r/H)``。"""
    q = 1.0 - r / H
    np.clip(q, 0.0, None, out=q)
    return _LAP_COEF * q


def _hash1(x):
    """``frac(sin(x*127.1+311.7)*43758.5453)``。

    来源: ``Toolset/fluid/fluid/FluidSolver.hlsl:41`` 的同名函数。排班相位要用它，
    才能和游戏一样是"错峰 + 抖动"而不是等距。
    """
    v = np.sin(np.asarray(x, dtype=np.float64) * 127.1 + 311.7) * 43758.5453
    return v - np.floor(v)


def frame_dt(frame_seconds):
    """一帧的模拟时长（秒），与 ``Probe.ini:886-892`` 逐行同式。

    来源: Toolset/fluid/Probe.ini

      $fluid_dt = time - $fluid_last_time
      if $fluid_dt <= 0 || $fluid_dt > 0.1
          $fluid_dt = 0.0166667
      elif $fluid_dt > 0.0333333
          $fluid_dt = 0.0333333
      endif

    **没有 1/60 的下限**：144 fps 时游戏一帧就是 1/144，预览也必须按帧时间走，
    否则高刷下预览会把 1/60 当成一帧，比游戏快 2.4 倍。
    """
    dt = float(frame_seconds)
    if dt <= 0.0 or dt > 0.1:
        dt = DT_MIN
    elif dt > DT_MAX:
        dt = DT_MAX
    return dt


def clamp_dt(frame_seconds):
    """一个子步的 dt（= 一帧 / 2）。保留旧名字，供既有调用方使用。"""
    return frame_dt(frame_seconds) * 0.5


# ---------------------------------------------------------------------------
# SoftNozzle / JetGuide / SheetProfile（逐式来自参考实现）
# ---------------------------------------------------------------------------
#
# 这三块是先前漏掉的：`_spawn_one` 只用了一个高斯抖动当喷散角，结果水柱被吹到
# 30 mm 宽，球（直径 5.6 mm）连不上，画面上必成糖葫芦。参考实现的喷流是**椭圆
# 锥**（斜率 0.012 / 0.035 rad），而且出喷口后先走一段**解析轨迹**（叶形），
# 过了 focus 点才转自由飞行。全部照抄，不做简化。

SOFTNESS = 0.003          # SoftNozzle.hlsl:47
GAIN = 12.0               # SoftNozzle.hlsl:52
SHEET_BLEND_SPEED = 0.85  # SoftNozzle.hlsl:9
SHEET_FLOOR = 1.1275      # SoftNozzle.hlsl:9（max(setting, 1.1275)）
SETTING_MIN = 0.28        # SoftNozzle.hlsl:14
SETTING_MAX = 3.94        # SoftNozzle.hlsl:14
SHEET_THRESHOLD = 1.69    # SoftNozzle.hlsl:33（$strength_level 的 sheet 门槛）
SHEET_SPAN = 2.25         # SoftNozzle.hlsl:33


def _smoothstep(a, b, x):
    t = np.clip((x - a) / max(b - a, 1e-9), 0.0, 1.0)
    return t * t * (3.0 - 2.0 * t)


def nozzle_pressure(setting, pulse_speed):
    """`SoftNozzle.hlsl:9-14`。"""
    blend = float(_smoothstep(0.0, 0.45, float(np.clip((pulse_speed - 1.0) / 0.85, 0, 1))))
    eff = setting + (max(setting, SHEET_FLOOR) - setting) * blend
    return (min(max(eff, SETTING_MIN), SETTING_MAX)
            * (min(max(pulse_speed, 1.0), 2.2) ** 2))


def advance_nozzle_opening(previous, pressure, dt):
    """`SoftNozzle.hlsl:16-21`：闭合 0.09 s、张开 0.055 s 的非对称响应。"""
    target = min(max(0.65 + 0.35 * np.sqrt(max(pressure, 0.0)), 0.80), 1.75)
    response = 0.055 if target > previous else 0.090
    k = 1.0 - np.exp(-min(max(dt, 0.0), 0.067) / response)
    return previous + (target - previous) * k


def evaluate_soft_nozzle(setting, pulse_speed, opening, t):
    """`SoftNozzle.hlsl:24-43`。返回喷口几何 + 初速 + 片状程度。"""
    pressure = nozzle_pressure(setting, pulse_speed)
    opening = advance_nozzle_opening(opening, pressure, 1.0 / 60.0) if opening <= 0.0 else opening
    discharge = 0.78 + 0.22 * float(np.clip((opening - 0.7) / 0.7, 0, 1))
    reference = 0.78 + 0.22 * (0.3 / 0.7)
    ripple = max(0.8, 1.0 + 0.08 * np.sin(t * 6.3) + 0.035 * np.sin(t * 19.7 + 1.2))
    clamped = min(max(setting, SETTING_MIN), SETTING_MAX)
    drive = float(_smoothstep(SHEET_THRESHOLD, SETTING_MAX, clamped))
    pulse_drive = float(np.clip((pulse_speed - 1.0) / 0.85, 0, 1))
    boost = 1.0 + 0.35 * max(drive, pulse_drive)
    speed = 0.3807886553 * np.sqrt(pressure * ripple) * (discharge / reference) * boost
    pressure_sheet = float(np.clip((setting - SHEET_THRESHOLD) / SHEET_SPAN, 0, 1))
    sheet = max(pressure_sheet, pulse_drive)
    contact = float(np.clip((1.25 - opening) / 0.45, 0, 1))
    return {
        'pressure': pressure,
        'opening': opening,
        'half_gap': 0.00065 * opening,                       # :29
        'half_length': 0.0020 * (1.0 + 0.025 * np.sin(t * 3.1)),   # :30
        'speed': speed,
        'sheet': sheet,
        'lateral_fan': 0.012 + (0.006 - 0.012) * sheet,      # :34
        'longitudinal_fan': 0.035 + (0.17632698 - 0.035) * sheet,  # :35
        'lateral_bias': ((0.006 * np.sin(t * 7.1) + 0.003 * np.sin(t * 17.3 + 1.4))
                         * contact * (1.0 - sheet)),          # :36
    }


def jet_gravity_scale(launch_speed, travel):
    """`SoftNozzle.hlsl:45-50`：近场重力只有 55%，水柱初段才立得住。"""
    speed_weight = float(_smoothstep(0.40, 1.20, max(launch_speed, 0.0)))
    near_field = 1.0 - float(_smoothstep(0.12, 0.30, max(travel, 0.0)))
    return 1.0 + (0.55 - 1.0) * (speed_weight * near_field)


def low_flow_scatter(setting, pulse_speed):
    """`SoftNozzle.hlsl:54-56`。"""
    return ((1.0 - float(_smoothstep(SETTING_MIN, SHEET_THRESHOLD, setting)))
            * (1.0 - float(np.clip((pulse_speed - 1.0) / 0.85, 0, 1))))


def sheet_focus_distance(amount):
    """`fluid/SheetProfile.hlsl:4`：0.035 m 起、按片状程度拉远到 0.100 m。"""
    return 0.035 + (0.100 - 0.035) * float(_smoothstep(0.0, 1.0, max(amount, 0.0)))


# ---------------------------------------------------------------------------
# 渲染口径：水洼与水柱尺寸（Slots.hlsl / Puddle.hlsl / FluidSurface.hlsl 的逐式移植）
# ---------------------------------------------------------------------------
#
# 这一组是**模拟端与显示端共用的口径**：模拟端拿 slot_drive/slot_width 定喷口形状、
# 拿 puddle_* 定水洼铺展；显示端（fluid_preview_bake）拿同一批函数算半径。
# 只有一个实现，所以不存在"表里一致、画面不一致"。

def slot_drive(level):
    """强度档 → 喷嘴驱动值（0.28 起，7 档到 3.94）。

    来源: Toolset/fluid/fluid/Slots.hlsl:51-52
      float level=clamp(IniParams[253].y+slots[s*SLOT_ROWS+SLOT_LOOK].x,0,7);
      return level<.5?.28:.565+(level-1)*.5625;
    """
    level = min(max(float(level), 0.0), 7.0)
    return 0.28 if level < 0.5 else 0.565 + (level - 1.0) * 0.5625


def slot_width(width):
    """该槽的水柱宽度倍数。来源: Slots.hlsl:57-60  width>0?clamp(width,.4,5):1。"""
    return min(max(float(width), 0.4), 5.0) if float(width) > 0 else 1.0


def visual_scale(level):
    """强度档对渲染半径的缩放（0.75..1）。来源: FluidSurface.hlsl:57。"""
    return 0.75 + 0.25 * float(_smoothstep(0.28, 1.69, slot_drive(level)))


#: 来源: Puddle.hlsl:4  ``PUDDLE_ENTRY_SPEED``（nhd-pick 改成"任何撞击都留水洼"，旧值 .25）
PUDDLE_ENTRY_SPEED = 6.0
#: 来源: Puddle.hlsl:5
PUDDLE_LIFETIME = 2.4
#: 来源: Puddle.hlsl:7  ``PUDDLE_MIN_UP``（旧值 .70710678）
PUDDLE_MIN_UP = -1.0


def puddle_width(spread):
    """水洼盘半径的铺展倍数（1..3）。来源: Puddle.hlsl:9  ``1+2*saturate(spread)``。"""
    return 1.0 + 2.0 * min(max(float(spread), 0.0), 1.0)


def puddle_half_height(radius, spread):
    """水洼盘中心离地高度。来源: Puddle.hlsl:10-13  ``radius/(width*width)``。"""
    w = puddle_width(spread)
    return radius / (w * w)


def slow_floor_contact(velocity, normal):
    """慢速接触判据 —— 成立就 `beginFloorPuddle`。

    来源: Toolset/fluid/fluid/Puddle.hlsl:14-16
      return normal.y>=PUDDLE_MIN_UP && dot(velocity,velocity)<=PUDDLE_ENTRY_SPEED^2;
    shader 的 ``normal.y`` 是游戏坐标的 Y 向上轴；预览是 Z 向上（:43 的 GRAVITY），
    所以这里取第 2 个分量。速度判据与轴无关。
    """
    v = np.asarray(velocity, dtype=np.float64)
    n = np.asarray(normal, dtype=np.float64)
    return bool(n[2] >= PUDDLE_MIN_UP and float(v @ v) <= PUDDLE_ENTRY_SPEED ** 2)


def evaluate_jet_guide(guide, age, focus_distance, gravity):
    """`fluid/JetGuide.hlsl:3-33` 的逐行翻译。

    返回 ``(position, velocity, travel)``——``travel`` 是已飞距离，它决定重力
    要不要从近场权重切回全量（`FluidSolver.hlsl:296`）。
    """
    axis = guide['axis']
    longitudinal = guide['longitudinal']
    normal = np.cross(axis, longitudinal)
    ln = np.linalg.norm(normal)
    normal = normal / ln if ln > 1e-6 else np.array([0.0, 0.0, 1.0], dtype=np.float32)
    speed = guide['speed']
    amount = guide['amount']
    acceleration = max(0.0, float(gravity @ axis))
    denom = max(speed + np.sqrt(max(speed * speed + 2.0 * acceleration * focus_distance, 0.0)), 1e-6)
    focus_age = 2.0 * focus_distance / denom
    t = min(age, focus_age)
    distance = speed * t + 0.5 * acceleration * t * t
    s = min(max(distance / max(focus_distance, 1e-9), 0.0), 1.0)
    ds = (speed + acceleration * t) / max(focus_distance, 1e-9)
    lane = guide.get('lane', 0.0)
    seed = guide.get('seed', 0.0)
    side_lane = (np.mod(np.sin(seed * 71.3 + lane * 17.1) * 43758.5453, 1.0) * 2.0 - 1.0)
    width = 0.14 * focus_distance * amount
    thickness = 0.025 * focus_distance * amount
    ripple = 1.0
    profile = 4.0 * s * (1.0 - s)
    derivative = 4.0 * (1.0 - 2.0 * s)
    offset = lane * width * profile * ripple
    slope = lane * width * (derivative * ripple) * ds
    side = side_lane * thickness * np.sin(np.pi * s)
    side_speed = side_lane * thickness * np.pi * np.cos(np.pi * s) * ds
    position = (guide['origin'] + guide['carrier'] * t + axis * (speed * t)
                + 0.5 * gravity * t * t + longitudinal * offset + normal * side)
    velocity = (guide['carrier'] + axis * speed + gravity * t
                + longitudinal * slope + normal * side_speed)
    travel = distance
    if age > focus_age:
        remainder = age - focus_age
        displacement = velocity * remainder + 0.5 * gravity * remainder * remainder
        position = position + displacement
        velocity = velocity + gravity * remainder
        travel = focus_distance + float(np.linalg.norm(displacement))
    return position, velocity, travel



def force_field_falloff(r2, r2_max, power):
    """``(1 - r²/R²)^power``，越界返回 0 —— 与 ``ForceField.hlsl:27-33`` 逐式一致。

    用 `r²` 而不是 `r`：整条链路不需要开方，边界处值与斜率都连续。
    """
    if r2_max <= 0.0:
        return 0.0
    t = 1.0 - r2 / r2_max
    if t <= 0.0:
        return 0.0
    return t ** max(float(power), 1.0)


def force_field_accel(points, fields, scale=1.0, owners=None):
    """``ForceField.hlsl:39-127`` 的向量化版本，返回 ``(N, 3)`` 加速度。

    逐项对齐着色器：

    * **归属**：每个力场带 ``owner``（发射器下标），只有 ``owners[粒子] == field['owner']``
      的粒子才吃它。来源: ``ForceField.hlsl:87-89``
      ``float owner=slotAnchor(slot,4).y; uint fieldSlot=(owner>=0&&owner<SLOTS?owner:slot);
      uint base=fieldSlot*FORCE_FIELDS_PER_SLOT;`` —— 即"按发射器 id 取那一行块"。
      预览里"粒子→发射器"就是 ``self.slot``（``node:3249`` 的 ``'slot': i`` → 本文件
      ``_spawn_one`` 的 ``self.slot[idx] = e['slot']``）。
    * 中心 = 发射器原点 + right·U + up·V + forward·W（基与导出同一套）
    * 衰减 = ``(1 - r²/R²)^power``，`r ≥ R` 直接跳过
    * 定向 → 沿 axis；径向 → 背离中心（负强度即向内）；涡旋 → 绕轴切向
    * 最后整体乘全局增益（对应 ``IniParams[41].y``）

    ``owners=None`` = 不做归属过滤（**只给"改之前"的复现与单测用**；Simulator 永远传
    ``self.slot[idx]``）。

    与 GPU 剩下的差异（都是可指的，不是"只有浮点精度"）：

    1. **每力场开关掩码未实现**：``ForceField.hlsl:51-60 emitterFieldOn(id,k)`` 读
       ``IniParams[41].w``（族开关）+ ``IniParams[58]/[59]``（每发射器一个 lane 的位掩码），
       预览没有这两级。当前导出写 ``w41 = 0``（``probes/after3/three_onefield.ini:766``）
       ⇒ shader 里 ``if(IniParams[41].w<.5)return true;`` 短接，掩码不参与，所以**今天无影响**；
       一旦有人把 ``w41`` 打开，预览会多施力。
    2. **叶形段**：``FluidSolver.hlsl:309-311`` 在闭式解上补一次常加速度，预览
       ``:929`` 那一支直接跳过（``pred[guided] = p[guided]``）。
    3. **出生那一拍**：游戏出生分支里一次都不调 ``forceFieldAccel``（第一次调用在
       ``FluidSolver.hlsl:276``），预览对所有粒子一视同仁。
    4. 浮点精度与遍历顺序。
    """
    total = np.zeros_like(points, dtype=np.float64)
    if not fields or abs(float(scale)) < 1e-12:
        return total
    owner_of = None if owners is None else np.asarray(owners)
    for f in fields:
        if not f.get('enabled', True):
            continue
        strength = float(f.get('strength', 0.0))
        if abs(strength) < 1e-9:
            continue
        radius = float(f.get('radius', 0.0))
        if radius <= 1e-9:
            continue
        if owner_of is not None:
            # 缺 'owner' 的旧 dict 一律不施力（**跳过**，不是"不过滤"）：
            # 宁可少推，也不要把别的发射器的水推歪。
            if f.get('owner') is None:
                continue
        center = np.asarray(f['center'], dtype=np.float64)
        axis = np.asarray(f['axis'], dtype=np.float64)
        axis_len = float(np.linalg.norm(axis))
        if axis_len < 1e-12:
            axis = np.array([0.0, 0.0, 1.0])
        else:
            axis = axis / axis_len

        d = points - center[None, :]
        r2 = np.einsum('ij,ij->i', d, d)
        r2_max = radius * radius
        mask = r2 < r2_max
        if owner_of is not None:
            # 来源: ForceField.hlsl:87-89 —— 只吃"本发射器那一行块"
            mask = mask & (owner_of == int(f['owner']))
        if not mask.any():
            continue
        w = np.array([force_field_falloff(float(x), r2_max, f.get('falloff', 2.0))
                      for x in r2[mask]], dtype=np.float64) * strength * float(scale)

        kind = f.get('kind', 'RADIAL')
        d_m = d[mask]
        if kind == 'RADIAL':
            n = np.linalg.norm(d_m, axis=1)
            n = np.maximum(n, 1e-12)
            direction = d_m / n[:, None]
        elif kind == 'VORTEX':
            along = d_m @ axis
            radial = d_m - along[:, None] * axis[None, :]
            tangential = np.cross(np.broadcast_to(axis, radial.shape), radial)
            n = np.linalg.norm(tangential, axis=1)
            n = np.maximum(n, 1e-12)
            direction = tangential / n[:, None]
        else:  # DIRECTIONAL
            direction = np.broadcast_to(axis, d_m.shape).copy()

        total[mask] += direction * w[:, None]
    return total


class Simulator(object):
    """一簇发射器共用的粒子池。

    ``emitters`` 每项：
        ``origin``  (3,) 喷口位置（世界坐标，米）
        ``direction`` (3,) 喷口朝向（单位向量）
        ``speed``   初速（米/秒）
        ``width``   宽度系数（与节点上的 width 同义）
        ``spread``  喷散角（弧度，默认 0.06）
        ``slot``    归属编号（只用于着色，不影响物理）
    """

    def __init__(self, capacity, emitters, ground_z=None, ground_normal=None,
                 force_fields=None, force_scale=1.0, radius=FLUID_RADIUS):
        self.capacity = int(capacity)
        # 物理粒子半径（米）——与游戏同一个数。
        # 来源: Toolset/fluid/fluid/ParticleConfig.hlsl:26
        #   float fluidRadiusOf(float4 row){float r=row.x;return r>1e-6?r:FLUID_RADIUS_DEFAULT;}
        # 它**不进核函数**：密度/梯度/拉普拉斯的支持半径是 H = IniParams[238].x
        # 来源: FluidSolver.hlsl:24  #define H PARAM.x   （Probe.ini:1014 x238 = 0.012）
        # 两个是独立参数；把面板半径塞进核半径会让预览与游戏分开。
        self.radius = float(radius)
        self.emitters = list(emitters)
        self.force_fields = list(force_fields or ())
        # 缺 `owner` 的力场一律不施力（见 force_field_accel 的过滤），这里把条数记下来，
        # 免得"力场静默失效"。node:3265-3289 现在一定写 owner，这个计数正常是 0。
        self.force_field_unscoped = sum(1 for f in self.force_fields
                                        if f.get('owner') is None)
        if self.force_field_unscoped:
            print("[FluidSim][WARN] %d 个力场没有 owner（旧数据？），预览里它们不会施力"
                  % self.force_field_unscoped)
        # 速度钳制的计数器（见 step() 里的 SPEED_CLAMP 段）：必须在 __init__ 里就存在。
        # 只在 prime() 里初始化过一次，导致「不调 prime 直接 step」的路径（测试、
        # tools/measure-preview-sim.py 等直接 Simulator(...) 的调用方）在第二次 step
        # 撞 AttributeError。prime() 仍会把它们清零，那是「重新开始」的语义。
        self.clamp_hits = 0
        self.clamp_checks = 0
        self.force_scale = float(force_scale)
        self.ground_z = ground_z
        self.ground_normal = (
            np.array([0.0, 0.0, 1.0]) if ground_normal is None
            else np.asarray(ground_normal, dtype=np.float64)
        )

        n = self.capacity
        self.pos = np.zeros((n, 3), dtype=np.float32)
        self.vel = np.zeros((n, 3), dtype=np.float32)
        self.age = np.zeros(n, dtype=np.float64)
        self.slot = np.full(n, -1, dtype=np.int32)
        self.alive = np.zeros(n, dtype=bool)
        self.stuck = np.zeros(n, dtype=bool)  # 撞上表面、黏住不再移动

        self._next = 0
        self.time = 0.0

        # 每帧发射多少：由 **capacity** 推出，不能写死。
        #
        # 稳态粒子数 = 发射率 × 寿命，而参考实现的寿命固定 1.1 s、每槽 1024 个，
        # 所以发射率 = capacity / LIFETIME。先前用的是模块常量
        # ``EMIT_PER_SECOND = 1024/1.1``，与 capacity 无关：
        # capacity 调到 2048 时排班表里每帧到期 62 个、却只发 31 个，
        # 多出来的一半永远排不上队（实测 cap=2048 时体积网格顶点数从 40788
        # 涨到 75784，但水柱粗细没变——多的是积压的僵尸粒子）。
        _emit_rate = float(self.capacity) / LIFETIME
        self.emit_per_frame = max(1, int(round(_emit_rate / 30.0)))

        # 每槽固定相位：``FluidSolver.hlsl:93-99`` 把每槽的 ``PRIMARY_PER_SLOT``
        # 个粒子在 ``PRIMARY_LIFETIME`` 内均匀排班
        # （``scheduled = cycle*lifetime + (1023-idx+jitter)*lifetime/1024``），
        # 所以任何时刻场上都是一整段连续的流。
        #
        # 只按「每帧发一批」的话，出生时 0.51 m/s、飞行中被重力加速到 ~4.5 m/s，
        # 同一批粒子在末段会被拉开成间隔十几厘米的一串糖葫芦——实测就是这个形态。
        # 这里给每个槽位一个均匀的初始年龄，重生时**扣掉一个寿命而不是归零**，
        # 相位就能一直保持，年龄分布持续均匀。
        self.phase = (np.arange(n, dtype=np.float64) / float(max(n, 1))) * LIFETIME

        # 每槽的预定出生时刻（见 :meth:`prime`）。
        self.next_birth = np.zeros(n, dtype=np.float64)

        # 与参考实现对齐的逐粒子附加状态（`FluidSolver.hlsl:252-259`）：
        #   launch_speed  出生时的初速，`jetGravityScale` 拿它算近场权重
        #   travel        已飞路程，重力从近场切回全量的判据
        #   sheet         片状程度 → `focusDistance`
        #   lane          叶形轨迹里的横向车道编号
        #   guide_*       出生时的喷口坐标系（解析轨迹要用，不能跟着粒子漂）
        self.launch_speed = np.zeros(n, dtype=np.float32)
        # 出生时那一档 ``$strength_level``（由面板发射速度反解）。渲染端 visualScale
        # 读它（``FluidSurface.hlsl:57``），对拍脚本也读它。
        # 来源: Slots.hlsl:51  float slotDrive(uint s)
        self.level = np.zeros(n, dtype=np.float32)
        # 这一槽的水柱宽度倍数，已经过 ``slotWidth`` 的 clamp（``Slots.hlsl:57-60``）。
        self.width = np.ones(n, dtype=np.float32)
        self.travel = np.zeros(n, dtype=np.float32)
        self.sheet = np.zeros(n, dtype=np.float32)
        self.lane = np.zeros(n, dtype=np.float32)
        self.guide_origin = np.zeros((n, 3), dtype=np.float32)
        self.guide_carrier = np.zeros((n, 3), dtype=np.float32)
        self.guide_axis = np.zeros((n, 3), dtype=np.float32)
        self.guide_long = np.zeros((n, 3), dtype=np.float32)
        self.guide_seed = np.zeros(n, dtype=np.float32)


    # -- 发射 ---------------------------------------------------------------

    def _emitter_nozzle(self, e):
        """按发射器参数算一次喷口几何（`SoftNozzle.hlsl:24-43`）。

        每个发射器每帧算一次就够——喷口开合是**发射器级**的量，不是逐粒子随机的。
        """
        # 档位由**面板发射速度**反解，用的就是导出时写 `$strength_level` 的那个函数
        # （node:1448 -> fluid_ini_merge.level_for_speed），**不在这里写第二套**。
        # 来源: Slots.hlsl:51-52
        #   float level=clamp(IniParams[253].y+slots[s*SLOT_ROWS+SLOT_LOOK].x,0,7);
        #   return level<.5?.28:.565+(level-1)*.5625;
        speed = float(e.get('speed', 0.0) or 0.0)
        pulse_speed = float(e.get('pulse_speed', 1.0))
        lv = e.get('level')
        setting = float(e.get('setting', SHEET_THRESHOLD))
        if speed > 0.0:
            base = float(lv if lv is not None else STRENGTH_LEVEL_REFERENCE)
            lv = _ini_merge().level_for_speed(speed) + (base - STRENGTH_LEVEL_REFERENCE)
        if lv is not None:
            lv = min(max(float(lv), 0.0), 7.0)
            setting = slot_drive(lv)
        else:
            # 既没档位也没速度：只有裸 setting 的旧调用方式，按默认档记账。
            lv = STRENGTH_LEVEL_REFERENCE
        t = float(self.time)
        nozzle = evaluate_soft_nozzle(setting, pulse_speed, 1.0, t)
        # 面板给的是**出水速度**，游戏直接拿它盖掉喷嘴公式算出来的那个：
        # 来源: FluidSolver.hlsl:146
        #   {float ls=fluidLaunchSpeedOf(IniParams[46]);if(ls>0)nozzle.speed=ls;}
        # 来源: ParticleConfig.hlsl:25  fluidLaunchSpeedOf -> min(v,6.0)
        if speed > 0.0:
            nozzle['speed'] = min(speed, 6.0)
        nozzle['speed'] *= float(e.get('speed_scale', 1.0))
        nozzle['level'] = lv
        nozzle['low_scatter'] = low_flow_scatter(setting, pulse_speed)
        return nozzle

    def _spawn_one(self, idx, k, rng, back_time=0.0):
        """把槽位 ``idx`` 放回喷口，并让它先沿初速飞 ``back_time`` 秒。

        ``back_time`` 就是相位预填充用的"已经飞了多久"——只按初速后退，
        不做重力积分：退役时刻落地的那些粒子在 :meth:`prime` 里另作处理。
        """
        n_em = len(self.emitters)
        e = self.emitters[k % n_em]
        origin = np.asarray(e['origin'], dtype=np.float32)
        direction = np.asarray(e['direction'], dtype=np.float32)
        direction = direction / max(np.linalg.norm(direction), 1e-9)

        # 以 direction 为轴造正交基：``up`` 是参考实现里那个 role，``right`` 与
        # ``slit_long`` 是片状喷流的两个方向（`FluidSolver.hlsl:140` 的
        # ``slitLong = normalize(forward - up*dot(forward,up))``）。
        up_hint = np.asarray(e.get('up', (0.0, 0.0, 1.0)), dtype=np.float32)
        up_len = float(np.linalg.norm(up_hint))
        up_hint = up_hint / max(up_len, 1e-9)
        slit_long = up_hint - direction * float(up_hint @ direction)
        ln = np.linalg.norm(slit_long)
        if ln < 1e-4:
            # up hint 与喷射方向平行（面板 tilt=0 时 direction 就是面法线，node 传进来的
            # up 也是面法线）。这里**必须**换一个与 direction 不平行的轴：旧代码固定取
            # (1,0,0)，当 direction 恰好也是 +X 时 `aux - direction*dot` 归零，接着
            # `right=cross(0,direction)` 也归零，椭圆盘和横向扇角被整体乘 0
            # （实测横坐标只有 1 个唯一值）。来源: FluidSolver.hlsl:153 的
            # `slitLong=normalize(forward-up*dot(forward,up))` 在游戏里由
            # ResolveSurface.hlsl:74 的 (right,-forward,up) 保证不平行。
            for aux in ((0.0, 0.0, 1.0), (0.0, 1.0, 0.0), (1.0, 0.0, 0.0)):
                aux = np.asarray(aux, dtype=np.float32)
                cand = aux - direction * float(aux @ direction)
                cand_len = float(np.linalg.norm(cand))
                if cand_len > 1e-3:
                    slit_long = cand
                    ln = cand_len
                    break
            else:
                slit_long = np.cross(direction, np.array([0.0, 1.0, 0.0], dtype=np.float32))
                ln = float(np.linalg.norm(slit_long))
        slit_long = slit_long / max(ln, 1e-9)
        right = np.cross(slit_long, direction)
        right = right / max(np.linalg.norm(right), 1e-9)

        nozzle = self._emitter_nozzle(e)
        speed = float(nozzle['speed'])

        # ---- 喷口：椭圆 `halfGap × halfLength`，不是圆盘 -----------------
        # `FluidSolver.hlsl:147`：原点先在各向异性的椭圆上撒开，再往后偏
        # 1 mm、向上偏 4 mm（出水面偏移）。
        ang = rng.random() * 2.0 * _PI
        # 宽度口径与游戏同一个函数（线性 + clamp .4..5），不再开根号。
        # 来源: Toolset/fluid/fluid/FluidSolver.hlsl:156  ...*slotWidth(slot)*r*rim
        width_scale = slot_width(e.get('width', 1.0))
        rr = np.sqrt(rng.random())
        offset = (right * (np.cos(ang) * nozzle['half_gap'] * width_scale * rr)
                  + slit_long * (np.sin(ang) * nozzle['half_length'] * width_scale * rr))
        spawn = origin + offset - slit_long * 0.001 + direction * 0.004

        # ---- 方向：椭圆锥（`FluidSolver.hlsl:150-153`） -------------------
        azimuth = rng.random() * 2.0 * _PI
        radial = np.sqrt(rng.random())
        side_slope = (np.cos(azimuth) * radial * nozzle['lateral_fan']
                      + nozzle['lateral_bias'])
        long_slope = np.sin(azimuth) * radial * nozzle['longitudinal_fan']
        scatter = nozzle['low_scatter'] * 1.25
        side_slope += np.cos(azimuth) * radial * scatter
        long_slope += np.sin(azimuth) * radial * scatter
        launch_dir = direction + right * side_slope + slit_long * long_slope
        launch_dir = launch_dir / max(np.linalg.norm(launch_dir), 1e-9)

        vel = launch_dir * speed

        # ---- JetGuide：出生时冻结坐标系，解析轨迹要用它 -------------------
        self.guide_origin[idx] = spawn
        self.guide_carrier[idx] = e.get('carrier', (0.0, 0.0, 0.0))
        self.guide_axis[idx] = direction
        self.guide_long[idx] = slit_long
        self.guide_seed[idx] = float(rng.random())
        self.lane[idx] = float(e.get('lane', 0.0))
        self.sheet[idx] = float(nozzle['sheet'])
        self.launch_speed[idx] = speed
        # 出生那一档 `$strength_level` 与这一槽的水柱宽度倍数：显示端要读它们。
        # 来源: Slots.hlsl:51 slotDrive(s) / :57 slotWidth(s)
        self.level[idx] = float(nozzle['level'])
        self.width[idx] = width_scale

        # ``back_time`` 是「它本该在多少秒之前出生」。必须**真的积分**这一段
        # （重力 + 初速），不能只按初速做直线前推：同一帧重生的那批粒子
        # back 都在 [0, 1/fps) 内，直线前推只把它们铺开 16 mm，于是每帧
        # 挤成一团、团间距恰好等于一帧的飞行距离——渲染出来就是一串糖葫芦。
        # 用 4 个子步积分，误差已经远小于粒子半径。
        pos_b = spawn + vel * back_time
        vel_b = vel + _gravity() * back_time
        if back_time > 0.0:
            steps = 4
            sub = back_time / steps
            pos_b = spawn
            vel_b = vel.copy()
            for _ in range(steps):
                vel_b = vel_b + _gravity() * sub
                pos_b = pos_b + vel_b * sub
            floor = float(self.ground_z) if self.ground_z is not None else None
            if floor is not None and pos_b[2] < floor:
                pos_b[2] = floor

        self.pos[idx] = pos_b
        self.vel[idx] = vel_b
        self.age[idx] = back_time
        self.travel[idx] = speed * back_time
        self.slot[idx] = int(e.get('slot', 0))
        self.alive[idx] = True
        self.stuck[idx] = False

    def emit(self, count, rng):
        """从各发射器轮流生成 ``count`` 个粒子（保留的旧接口）。"""
        if not self.emitters:
            return
        for k in range(int(count)):
            idx = self._next
            self._next = (self._next + 1) % self.capacity
            # 帧内相位抖动：不然同一帧的这批会挤成一个球。
            self._spawn_one(idx, k, rng, back_time=rng.random() * (1.0 / 30.0))

    def prime(self, rng):
        """只把各槽位的**预定出生时刻**铺开，粒子留给后续帧自然出生。

        参考实现里每个粒子有固定的 ``scheduled`` 出生时刻
        （``FluidSolver.hlsl:93-99``）。这里等价地给每个槽位一个
        ``next_birth``，均匀铺在 ``[0, LIFETIME)`` 上。

        这里**刻意不做位置预填充**：先前用「初速 × back」估算已经飞了多久，
        估算值和真实轨迹差一个重力项，落地后又只把 z 钳住、速度清零，
        结果是一批位置对不上速度的粒子——预热几十帧都收敛不回来。
        直接让它们按预定时刻出生、交给模拟自己积分，几十帧后就是稳态。
        """
        n = self.capacity
        # 排班相位照游戏：每槽 1024 个粒子在一个寿命内错峰出生，顺序反转、带 0.78 个
        # 槽距的 hash 抖动。
        # 来源: FluidSolver.hlsl:104
        #   scheduled=cycle*lifetime+(BASE-1-inSlot+offset+.78*hash1(i*3.71+cycle*7.13))*lifetime/BASE
        # 等距相位会让同一段水流里的相邻粒子间距过于均匀（实测空中最近邻中位比游戏大 12%）。
        idx = np.arange(n, dtype=np.float64)
        jitter = 0.78 * _hash1(idx * 3.71)
        self.next_birth = ((n - 1 - idx) + jitter) * LIFETIME / float(max(n, 1))
        self.alive[:] = False
        self.age[:] = 0.0
        self.clamp_hits = 0
        self.clamp_checks = 0

    def recycle(self, rng):
        """把到了预定出生时刻的槽位重生（``next_birth <= time``）。

        ``back = time - next_birth`` 是「它晚出生了多久」——用它把位置沿初速
        前推，同一帧里所有新生的粒子就沿着轨迹铺开一小段，而不是全挤在喷口。
        没有这一步，年龄就只有「每帧一档」的 33 个值，渲染出来是一串糖葫芦。

        ``next_birth`` 用 ``while`` 追平：一帧只加一个寿命的话，被拖慢的槽位
        会永远落在后面，每帧都重生一次，轨迹直接乱掉。
        """
        due = np.flatnonzero(self.next_birth <= self.time)
        for idx in due:
            back = float(self.time - self.next_birth[idx])
            self._spawn_one(int(idx), int(idx), rng, back_time=back)
            while self.next_birth[idx] <= self.time:
                self.next_birth[idx] += LIFETIME

    # -- 单步 ---------------------------------------------------------------

    def step(self, frame_seconds, rng):
        """推进一帧（内部两个子步，与 ``Probe.ini`` 的 2 次 FluidSubstep 一致）。"""
        dt_total = frame_dt(frame_seconds)
        for _ in range(2):
            self._substep(dt_total * 0.5, rng)
        # 子步里只负责让到寿命的粒子退出运算（``velocityAlive.w = 0``）；
        # 按相位重生放在帧末统一做，避免同一帧里重生出来的粒子立刻又参与半步。
        self.recycle(rng)
        self.time += dt_total

    def _substep(self, dt, rng):
        live = self.alive
        count = int(live.sum())
        if count == 0:
            self.age[live] += dt
            self.alive &= self.age < LIFETIME
            return

        idx = np.flatnonzero(live)
        p = self.pos[idx]
        v = self.vel[idx]
        movable = ~self.stuck[idx]

        # 0) 叶形段（JetGuide）：`travel < focusDistance` 的粒子走**解析轨迹**，
        #    完全不进 PBF。参考实现 `FluidSolver.hlsl:288-294` 就是直接拿
        #    `evaluateJetGuide` 的结果覆盖 `predictedLambda`，`:306` 的
        #    `active = !released && ...` 把这些粒子排除出邻居求解。
        #    不做这一步的话，叶形会被 PBF 的排斥力抹平成一团。
        sheet = self.sheet[idx] > 0.001
        guided = np.zeros(len(idx), dtype=bool)
        if sheet.any():
            gi = idx[sheet]
            focus = np.array([sheet_focus_distance(float(a)) for a in self.sheet[gi]],
                             dtype=np.float32)
            in_leaf = self.travel[gi] < focus
            for j, g in enumerate(gi):
                if not in_leaf[j]:
                    continue
                guide = {
                    'origin': self.guide_origin[g],
                    'axis': self.guide_axis[g],
                    'longitudinal': self.guide_long[g],
                    'carrier': self.guide_carrier[g],
                    'speed': float(self.launch_speed[g]),
                    'amount': float(self.sheet[g]),
                    'lane': float(self.lane[g]),
                    'seed': float(self.guide_seed[g]),
                }
                pos_g, vel_g, travel_g = evaluate_jet_guide(
                    guide, float(self.age[g]), float(focus[j]), _gravity())
                self.pos[g] = pos_g
                self.vel[g] = vel_g
                self.travel[g] = travel_g
                self.age[g] += dt
                self.alive[g] &= self.age[g] < LIFETIME
                guided[np.argwhere(gi == g)[0][0]] = True
            p = self.pos[idx]
            v = self.vel[idx]

        # 1) 预测：p += v*dt + g*dt²*jetGravityScale（`FluidSolver.hlsl:263-274`）
        #
        # 近场重力只有 55%（`SoftNozzle.hlsl:45-50`），水柱初段才立得住。
        # 先前一律用全量 9.81，喷出去立刻往下垂，这是"不像水"的一半原因。
        scale = np.array([jet_gravity_scale(float(self.launch_speed[i]),
                                            float(self.travel[i])) for i in idx],
                         dtype=np.float32)
        # 力场与重力合成在同一次预测里（`FluidSolver.hlsl:263-274` 那句
        # `(gravity + bend)`）：位置先按 `a·dt²` 推，随后 `new_vel = (pred-p)/dt`
        # 自然把这部分冲量带进速度，不需要再单独加一次。
        accel = _gravity()[None, :] * scale[:, None]
        if self.force_fields:
            # 归属按**发射器下标**：`self.slot[idx]` 是 node:3249 写下的 `'slot': i`，
            # 不是 3Dmigoto 的运行期槽号。来源: ForceField.hlsl:87-89 按 owner 取行块。
            accel = accel + force_field_accel(p, self.force_fields,
                                              self.force_scale,
                                              owners=self.slot[idx]).astype(np.float32)
        pred = p + v * dt + accel * (dt * dt)
        pred[guided] = p[guided]     # 叶形段已由解析轨迹定位置，别再积分一次

        # 2) 密度 + lambda + 修正，**每轮重算**（FluidSolver.hlsl:328-367）
        #
        # 游戏每个子步是 Density→Correct ×3（Probe.ini:938-946），Correct 把位置
        # 写回后下一轮 Density 读的是新位置，所以 λ 必须重算。预览旧版只算一次 λ、
        # 连做三次同样的修正 —— 等于把同一份推力叠三遍（实测空中间距偏稀 12.5%）。
        # 来源: FluidSolver.hlsl:343-347  C/demon/λ；:349-367  correction*
        for _ in range(ITERATIONS):
            density, grad_sum, grad_sq_sum = self._density_terms(pred)
            c = np.maximum(density / RHO - 1.0, 0.0)
            denom = grad_sq_sum + np.einsum('ij,ij->i', grad_sum, grad_sum) + LAMBDA_EPS
            lam = -c / denom
            correction = self._correction(pred, lam)
            length = np.linalg.norm(correction, axis=1)
            scale = np.minimum(1.0, CORRECTION_CLAMP / np.maximum(length, 1e-9))
            pred += correction * scale[:, None]

        # 4) 最终化（FluidSolver.hlsl:346-440）
        new_vel = (pred - p) / max(dt, 1e-9)
        # 已飞路程：每子步按段长累加。渲染端用它算水洼/飞溅的盘半径
        # （`FluidSurface.hlsl:61/74` 的 `max(densityBirth.z,.1)`）。
        # 来源: FluidSolver.hlsl:440  travel=oldTravel+segmentLength;  :458 写回 densityBirth.z
        # 叶形段不会在这里重复累加：`:741` 已把 pred 置回 p，那一段的段长是 0，
        # 而叶形段的路程由叶形分支里的 evaluate_jet_guide 返回值直接写入。
        # 顺序也要和游戏一致：地面钳制在本函数 :770 之后才改 pred，游戏同样是
        # 先算 travel（:440）再 collideFluid（:462），所以累加用的是"未钳制"的段长。
        self.travel[idx] = (self.travel[idx]
                            + np.linalg.norm(pred - p, axis=1)).astype(np.float32)
        viscous, cohesion, smooth = self._viscous_cohesion(pred, p, dt, density)
        new_vel += (dt * (viscous + cohesion * COHESION_SCALE)
                    + smooth * SMOOTHING_SCALE)

        # 5) 速度上限
        speed = np.linalg.norm(new_vel, axis=1)
        new_vel *= (np.minimum(1.0, MAX_SPEED / np.maximum(speed, 1e-9)))[:, None]

        # 6) 碰撞：地面平面（参考实现用屏幕深度，预览用场景地面近似）
        #
        # 对齐游戏：**命中就判死**，不是贴地滑到寿命。
        # 来源: FluidSolver.hlsl:466-492
        #   if(collideFluid(...)){ ... p.velocityAlive.w=0; p.predictedLambda.xyz=contact; }
        # 撞上的粒子当帧退出模拟（渲染端也要求 velocityAlive.w>.5 才画），位置停在
        # 接触点，等下一次排班出生时被 `_spawn_one` 覆盖。
        hit = np.zeros(len(idx), dtype=bool)
        if self.ground_z is not None:
            depth = (pred[:, 2] - float(self.ground_z))
            hit = depth < self.radius
            if hit.any():
                pred[hit, 2] = float(self.ground_z) + self.radius
                new_vel[hit] = 0.0
                self.alive[idx[hit]] = False

        # 7) 只有可动的粒子才更新；撞地/黏住的不再前进
        keep = ~hit
        writable = idx[movable & keep]
        self.pos[writable] = pred[movable & keep]
        self.vel[writable] = new_vel[movable & keep]

        # 8) 寿命
        self.age[live] += dt
        self.alive &= self.age < LIFETIME

    # -- 内部 ---------------------------------------------------------------

    def _density_terms(self, pred):
        """返回 ``(density, Σgrad, Σ|grad|²)``，与 ``FluidSolver.hlsl:335-339`` 同式。

        三个口径都照游戏：

        * ``density += MASS*poly6``（不是 ``MASS/RHO``），含自项 ``MASS*poly6(0)``
          —— 来源 ``FluidSolver.hlsl:337`` 的 ``density+=MASS*poly6(dot(d,d));``
          （``k==i`` 那一项也加）；
        * 每个粒子对自己的**全部**邻居求和：一对 ``(i,j)`` 要同时记到 i 和 j 上
          —— 来源 ``FluidSolver.hlsl:331-341`` 的逐粒子 tile 循环；
        * 梯度对两边的贡献反号（``gradient`` 是奇函数），``|grad|²`` 两边同值。
        """
        n = pred.shape[0]
        i, j, r, r2 = neighbour_pairs(pred)
        self_term = MASS * _poly6(np.zeros(1))[0]
        if i.size == 0:
            return np.full(n, self_term), np.zeros((n, 3)), np.zeros(n)

        # 来源: FluidSolver.hlsl:337  density+=MASS*poly6(dot(d,d))，自项 k==i 也算
        w = _poly6(r2) * MASS
        density = _scatter_sum(i, w, n) + _scatter_sum(j, w, n) + self_term

        # 来源: FluidSolver.hlsl:338  (MASS/RHO)*gradient(d)，d = 自己 - 邻居
        direction = (pred[i] - pred[j]) / _safe_r(r2)[:, None]
        g = _gradient(direction, r) * (MASS / RHO)     # (M,3)
        grad_sum = _scatter_sum_vec(i, g, n) - _scatter_sum_vec(j, g, n)
        gg = np.einsum('ij,ij->i', g, g)
        grad_sq_sum = _scatter_sum(i, gg, n) + _scatter_sum(j, gg, n)
        return density, grad_sum, grad_sq_sum

    def _correction(self, pred, lam):
        """``FluidSolver.hlsl:329-339``：``Σ(λi+λj)(MASS/RHO)∇W``。"""
        n = pred.shape[0]
        i, j, r, r2 = neighbour_pairs(pred)
        if i.size == 0:
            return np.zeros((n, 3))

        volume = MASS / RHO
        direction = (pred[i] - pred[j]) / _safe_r(r2)[:, None]
        g = _gradient(direction, r) * volume
        pair = (lam[i] + lam[j])[:, None] * g         # (M,3)
        out = _scatter_sum_vec(i, pair, n) - _scatter_sum_vec(j, pair, n)
        return out

    def _viscous_cohesion(self, pred, old, dt, density):
        """粘性 / 速度平滑 / 内聚，逐式对齐 ``FluidSolver.hlsl:403-433``。

        三处口径都照游戏：

        * 方向是**邻居 − 自己**：``d=tilePredicted[k].xyz-p.predictedLambda.xyz``
          （``:412``）、``qv-velocity``（``:413``）；
        * 体积是 ``MASS/max(density_j, RHO*.1)``（``:427``），不是常量 ``MASS/RHO``；
        * 一对 ``(i,j)`` 同时记到两边（每个粒子对自己的全部邻居求和）。
        """
        n = pred.shape[0]
        i, j, r, r2 = neighbour_pairs(pred)
        if i.size == 0:
            return (np.zeros((n, 3)),) * 3

        # 来源: FluidSolver.hlsl:427  float volume=MASS/max(tileDensity[k],RHO*.1)
        volume_i = MASS / np.maximum(density[i], RHO * 0.1)
        volume_j = MASS / np.maximum(density[j], RHO * 0.1)
        wj = _poly6(r2) * volume_j
        wi = _poly6(r2) * volume_i
        lap = _laplacian(r)

        qv = (pred - old) / max(dt, 1e-9)
        rel = qv[j] - qv[i]                              # 邻居 - 自己
        direction = (pred[j] - pred[i]) / _safe_r(r2)[:, None]

        viscous = (_scatter_sum_vec(i, _nu() * volume_j[:, None] * rel * lap[:, None], n)
                   - _scatter_sum_vec(j, _nu() * volume_i[:, None] * rel * lap[:, None], n))
        smooth = (_scatter_sum_vec(i, wj[:, None] * rel, n)
                  - _scatter_sum_vec(j, wi[:, None] * rel, n))
        cohesion = (_scatter_sum_vec(i, wj[:, None] * direction, n)
                    - _scatter_sum_vec(j, wi[:, None] * direction, n))
        return viscous, cohesion, smooth

    # -- 输出 ---------------------------------------------------------------

    def snapshot(self):
        """当前所有活粒子的位置与槽位，供 Blender 侧写关键帧。"""
        live = self.alive
        return (self.pos[live].copy(),
                self.slot[live].copy(),
                self.age[live].copy())


class SimulationJob(object):
    """可**逐步推进**的模拟，给界面做进度条用。

    ``simulate()`` 是一次跑完，主线程被占住几秒，进度条画不出来（界面根本不
    重画）。这里把同一份循环拆成一个个"工作单元"：预热阶段一个单元 = 一步物理，
    正式阶段一个单元 = 一步物理、每 ``subframes`` 步记一帧。调用方每 tick 喂
    一小段时间预算，剩下的下次接着算，进度就是 ``units_done / units_total``。

    物理与 ``simulate()`` 完全同一段代码路径 —— 后者现在就是"一次推到底"的
    特例，所以进度条版和非进度条版不会算出两种结果。
    """

    def __init__(self, emitters, frames, fps=30.0, capacity=1024, ground_z=None,
                 seed=20261029, warmup_frames=None, subframes=1,
                 force_fields=None, force_scale=1.0, radius=FLUID_RADIUS):
        self.rng = np.random.default_rng(seed)
        self.sim = Simulator(capacity, emitters, ground_z=ground_z,
                             force_fields=force_fields, force_scale=force_scale,
                             radius=radius)
        self.sim.prime(self.rng)
        self.sub = max(1, int(subframes))
        self.step_seconds = 1.0 / float(fps) / self.sub
        if warmup_frames is None:
            warmup_frames = int(np.ceil(LIFETIME / (1.0 / float(fps))))
        self.warmup_total = int(warmup_frames) * self.sub
        self._warmup_left = self.warmup_total
        self.total_frames = int(frames)
        self.frames_done = 0
        self.out = []
        self._sub_left = 0
        self.units_total = max(1, self.warmup_total + self.total_frames * self.sub)
        self.units_done = 0

    # -- 进度 -------------------------------------------------------------
    @property
    def progress(self):
        return min(1.0, float(self.units_done) / float(self.units_total))

    @property
    def finished(self):
        return self.frames_done >= self.total_frames

    @property
    def frame_progress(self):
        return float(self.frames_done) / float(max(1, self.total_frames))

    def label(self):
        """面板上显示的那一行字。预热段单独说，否则用户看到一直 0/60 会以为卡死。"""
        if self._warmup_left > 0:
            return '预热 %d/%d 步' % (self.warmup_total - self._warmup_left,
                                     self.warmup_total)
        return '模拟 %d/%d 帧' % (self.frames_done, self.total_frames)

    # -- 推进 -------------------------------------------------------------
    def advance(self, units=1):
        """推进 ``units`` 个工作单元，返回是否已全部完成。"""
        for _ in range(max(1, int(units))):
            if self.finished:
                break
            self.sim.step(self.step_seconds, self.rng)
            self.units_done += 1
            if self._warmup_left > 0:
                self._warmup_left -= 1
                continue
            self._sub_left += 1
            if self._sub_left >= self.sub:
                self._sub_left = 0
                self.out.append(self.sim.snapshot())
                self.frames_done += 1
        return self.finished


def simulate(emitters, frames, fps=30.0, capacity=1024, ground_z=None,
             seed=20261029, warmup_frames=None, subframes=1,
             force_fields=None, force_scale=1.0, radius=FLUID_RADIUS):
    """跑完整段模拟，返回 ``[(positions, slots, ages), ...]``，长度 ``frames``。

    ``ground_z`` 是地面平面高度（世界坐标），``None`` 表示不碰地。

    ``warmup_frames`` 是**不记录**的预热帧数，默认跑满一个寿命。粒子是逐个
    按预定时刻出生的，不预热的话前 1.1 秒水柱还在"长"，第 1 帧几乎是空的；
    预热完再记录，每一帧都是稳态形态。

    ``subframes`` 是每个预览帧内部跑几小步（默认 1，即物理跟预览同频）。
    实测把它提到 4（120 Hz 物理）**不会**让点云更密——细胞数由
    ``capacity / LIFETIME`` 决定，与步长无关；只是白花 4 倍时间
    （90 帧从 25 s 变 118 s，形态一模一样）。留着这个参数是为了将来
    真要细步长时可用。

    内部走 :class:`SimulationJob`，只是把预算一次给足；所以界面里带进度条的
    那次计算和这里的离线计算是同一段代码。
    """
    job = SimulationJob(emitters, frames, fps=fps, capacity=capacity,
                        ground_z=ground_z, seed=seed,
                        warmup_frames=warmup_frames, subframes=subframes,
                        force_fields=force_fields, force_scale=force_scale,
                        radius=radius)
    job.advance(job.units_total)
    return job.out


def save_npz(path, frames, capacity):
    """把 ``simulate()`` 的结果写进 ``.npz``。

    每帧粒子数不同（发射中），所以统一 padding 到 ``capacity``，
    用 ``counts`` 记录每帧真实粒子数。Blender 侧按 ``counts[i]`` 取前 n 个。
    """
    count = len(frames)
    positions = np.zeros((count, capacity, 3), dtype=np.float32)
    slots = np.full((count, capacity), -1, dtype=np.int32)
    ages = np.zeros((count, capacity), dtype=np.float32)
    counts = np.zeros(count, dtype=np.int32)

    for i, (p, s, a) in enumerate(frames):
        n = min(len(p), capacity)
        positions[i, :n] = p[:n]
        slots[i, :n] = s[:n]
        ages[i, :n] = a[:n]
        counts[i] = n

    np.savez_compressed(path, positions=positions, slots=slots,
                        ages=ages, counts=counts, capacity=capacity)
    return path


def load_npz(path):
    """读回 ``save_npz`` 的结果，返回 ``(positions, slots, ages, counts)``。"""
    with np.load(path) as data:
        return (data['positions'], data['slots'], data['ages'], data['counts'])
