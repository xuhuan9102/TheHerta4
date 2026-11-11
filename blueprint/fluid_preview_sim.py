# -*- coding: utf-8 -*-
"""Position Based Fluids 的预览实现。

逐行对照参考实现翻译，每个常量都标了出处：

- ``Toolset/fluid/fluid/FluidSolver.hlsl``
- ``Toolset/fluid/Probe.ini``

只依赖 numpy，**不 import bpy**——这样能在 pytest 里直接跑，也能在
``blender --background`` 里跑。Blender 侧只负责把结果变成关键帧。

主水柱按仓库 ``Toolset/fluid`` 的预测、释放、力场和分槽求解推进。
碰撞使用场景地面平面；游戏的屏幕深度碰撞、二次飞溅及屏幕空间渲染
需要游戏相机和深度缓冲，未在此模块模拟。
"""

import numpy as np

# --- 常量 -------------------------------------------------------------------

#: 核半径。``Probe.ini:419`` 的 ``x238 = 0.012 ...`` 第一个分量。
H = 0.012
#: 单粒子质量。``Probe.ini:419`` 第二分量。
MASS = 0.0000213333
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
#: 这次模拟用的是谁给的重力：``"explicit"`` = 调用过 set_physics；``"default"`` = 模块默认值。
#: 报告/日志里必须能看出这一点——默认值是参考重力（4× 游戏值），拿它跟游戏对比会错。
_PHYSICS_SOURCE = "default"
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
    global _GRAVITY_SCALE, _VISCOSITY_OVERRIDE, _PHYSICS_SOURCE
    if gravity_scale is not None:
        _GRAVITY_SCALE = float(gravity_scale)   # 允许负值：负 = 反重力（火焰上飘）
        _PHYSICS_SOURCE = "explicit"
    if viscosity is not None:
        _VISCOSITY_OVERRIDE = max(float(viscosity), 0.0)
        _PHYSICS_SOURCE = "explicit"


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

#: 速度钳制开关（与 FluidSolver.hlsl:440 同口径）。默认开=产品行为；
#: 仅用于 2x2 对照实验时置 False，并把取值写进 npz 元数据。
SPEED_CLAMP = True
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
        i, j = np.nonzero(np.triu(r2_all < H * H, k=1))

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
    scale[r < 1e-7] = 0
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

    字段的 enabled 对应节点开关初值；游戏中由动画或热键改变的运行期
    开关状态需要作为新的 enabled 输入。CPU 与 GPU 的浮点与求和顺序可能不同。
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
        axis = axis / max(float(np.linalg.norm(axis)), 1e-6)

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
            n = np.maximum(n, 1e-6)
            direction = d_m / n[:, None]
        elif kind == 'VORTEX':
            along = d_m @ axis
            radial = d_m - along[:, None] * axis[None, :]
            tangential = np.cross(np.broadcast_to(axis, radial.shape), radial)
            n = np.linalg.norm(tangential, axis=1)
            n = np.maximum(n, 1e-6)
            direction = tangential / n[:, None]
            direction *= float(f.get('handedness', 1.0))
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
        self.per_emitter_capacity = int(capacity)
        self.capacity = int(capacity) * max(1, len(emitters))
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
        # 重力来源：跟着模块状态走（set_physics 之后新建的 Simulator 才是 explicit）
        self.physics_source = _PHYSICS_SOURCE
        self._physics_warned = False
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
        self._opening = np.ones(len(self.emitters))
        self._nozzle_dt = 1.0 / 60.0
        self._frame_seconds = 1.0 / 30.0

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
        self.free_velocity = np.zeros((n, 3), dtype=np.float32)
        self.lifetime = np.full(n, LIFETIME, dtype=np.float64)
        for k, e in enumerate(self.emitters):
            life = float(e.get('lifetime', 0.0))
            start = k * self.per_emitter_capacity
            self.lifetime[start:start + self.per_emitter_capacity] = life if life > 1e-6 else LIFETIME


    # -- 发射 ---------------------------------------------------------------

    def _emitter_nozzle(self, e, birth_time=None, emitter_index=0):
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
            lv = _ini_merge().level_for_speed(speed) + float(e.get('level_offset', base - STRENGTH_LEVEL_REFERENCE))
        if lv is not None:
            lv = min(max(float(lv), 0.0), 7.0)
            setting = slot_drive(lv)
        else:
            # 既没档位也没速度：只有裸 setting 的旧调用方式，按默认档记账。
            lv = STRENGTH_LEVEL_REFERENCE
        if e.get('trickle', False):
            setting = SETTING_MIN
        t = float(self.time if birth_time is None else birth_time)
        opening = advance_nozzle_opening(self._opening[emitter_index],
                                        nozzle_pressure(setting, pulse_speed), self._nozzle_dt)
        nozzle = evaluate_soft_nozzle(setting, pulse_speed, opening, t)
        # 面板给的是**出水速度**，游戏直接拿它盖掉喷嘴公式算出来的那个：
        # 来源: FluidSolver.hlsl:146
        #   {float ls=fluidLaunchSpeedOf(IniParams[46]);if(ls>0)nozzle.speed=ls;}
        # 来源: ParticleConfig.hlsl:25  fluidLaunchSpeedOf -> min(v,6.0)
        if speed > 0.0:
            nozzle['speed'] = min(speed, 6.0)
        if float(e.get('speed_override', 0.0)) > 0:
            nozzle['speed'] = float(e['speed_override'])
        nozzle['speed'] *= float(e.get('speed_scale', 1.0))
        nozzle['level'] = lv
        nozzle['setting'] = setting
        nozzle['pulse_speed'] = pulse_speed
        nozzle['low_scatter'] = low_flow_scatter(setting, pulse_speed)
        return nozzle

    def _spawn_one(self, idx, k, rng, back_time=0.0):
        """Create one scheduled shader birth, including its fractional age."""
        n_em = len(self.emitters)
        emitter_index = k % n_em
        e = self.emitters[emitter_index]
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
        if 'right' in e:
            right = np.asarray(e['right'], dtype=np.float32)

        birth_time = self.time - back_time
        nozzle = self._emitter_nozzle(e, birth_time, emitter_index)
        speed = float(nozzle['speed'])

        # ---- 喷口：椭圆 `halfGap × halfLength`，不是圆盘 -----------------
        # `FluidSolver.hlsl:147`：原点先在各向异性的椭圆上撒开，再往后偏
        # 1 mm、向上偏 4 mm（出水面偏移）。
        ang = float(_hash1(idx + birth_time * 17)) * 2.0 * _PI
        # 宽度口径与游戏同一个函数（线性 + clamp .4..5），不再开根号。
        # 来源: Toolset/fluid/fluid/FluidSolver.hlsl:156  ...*slotWidth(slot)*r*rim
        width_scale = slot_width(e.get('width', 1.0))
        rr = np.sqrt(float(_hash1(idx * 7 + birth_time * 13)))
        rr *= 1 + 0.10 * np.sin(ang * 3 + 1.1) + 0.05 * np.cos(ang * 5 - 0.4)
        offset = (right * (np.cos(ang) * nozzle['half_gap'] * width_scale * rr)
                  + slit_long * (np.sin(ang) * nozzle['half_length'] * width_scale * rr))
        spawn = origin + offset - slit_long * 0.001 + direction * 0.004

        # ---- 方向：椭圆锥（`FluidSolver.hlsl:150-153`） -------------------
        azimuth = float(_hash1(idx * 11 + birth_time * 37)) * 2.0 * _PI
        radial = np.sqrt(float(_hash1(idx * 19 + birth_time * 23)))
        side_slope = (np.cos(azimuth) * radial * nozzle['lateral_fan']
                      + nozzle['lateral_bias'])
        long_slope = np.sin(azimuth) * radial * nozzle['longitudinal_fan']
        scatter = nozzle['low_scatter'] * 1.25
        side_slope += np.cos(azimuth) * radial * scatter
        long_slope += np.sin(azimuth) * radial * scatter
        branch = 0
        frozen = nozzle['sheet'] if nozzle['sheet'] > 0.001 else -nozzle['low_scatter']
        minimum_flow = not e.get('trickle', False) and nozzle['setting'] < 0.4
        if minimum_flow:
            frozen = -1
            cycle = np.floor(birth_time / 4.8)
            clock = birth_time - cycle * 4.8
            start = 0.35 + 0.30 * float(_hash1(cycle * 5.17 + 11))
            duration = 0.20 + 1.20 * float(_hash1(cycle * 7.91 + 23))
            ramp = min(0.22, duration * 0.25)
            def heading(angle_key, slope_key):
                angle = 2 * _PI * float(_hash1(angle_key))
                slope = 0.45 + 1.35 * float(_hash1(slope_key))
                return np.array([np.cos(angle), np.sin(angle)]) * slope
            previous = heading((cycle - 1) * 19.3 + 7.7, (cycle - 1) * 23.1 + 3.1)
            before = heading(cycle * 19.3, cycle * 23.1)
            after = heading(cycle * 19.3 + 7.7, cycle * 23.1 + 3.1)
            before = previous + (before - previous) * _smoothstep(0, 0.22, clock)
            h = before + (after - before) * _smoothstep(start, start + 0.22, clock)
            blend = (_smoothstep(start, start + ramp, clock)
                     * (1 - _smoothstep(start + duration - ramp, start + duration, clock)))
            angle = 2 * _PI * float(_hash1(idx * 13.7 + birth_time * 17.3))
            slope = 0.8 + 3.2 * np.sqrt(float(_hash1(idx * 3.91 + birth_time * 9.7)))
            h += (np.array([np.cos(angle), np.sin(angle)]) * slope - h) * blend
            jitter = 0.025 + (0.15 - 0.025) * blend
            side_slope = h[0] + (float(_hash1(idx * 5.7 + birth_time * 3)) - 0.5) * jitter
            long_slope = h[1] + (float(_hash1(idx * 9.3 + birth_time * 5)) - 0.5) * jitter
            branch = -3 if blend > 0.65 else (0 if blend > 0.05 else (2 if clock < start else 3))
            tightening = float(_smoothstep(0, 0.45, np.clip((nozzle['pulse_speed'] - 1) / 0.85, 0, 1)))
            speed *= 1 + ((0.75 + 0.75 * float(_hash1(idx * 31.3 + birth_time * 23.7))) - 1) * blend * (1 - tightening)
        if e.get('trickle', False):
            frozen, branch = -1, 2
            side_slope = long_slope = 0
            speed = 0.27 * min(max(float(e.get('trickle_scale', 0.1)), 0.1), 8)
        launch_dir = direction + right * side_slope + slit_long * long_slope
        launch_dir = launch_dir / max(np.linalg.norm(launch_dir), 1e-9)

        vel = launch_dir * speed
        if branch < -2:
            bits = int(np.float32(birth_time).view(np.uint32)) ^ ((idx * 747796405 + 2891336453) & 0xffffffff)
            bits ^= bits >> 16
            bits = (bits * 2246822519) & 0xffffffff
            bits ^= bits >> 13
            angle = (bits & 65535) * (2 * _PI / 65536)
            horizontal = 0.55 + 0.65 * ((bits >> 16) & 65535) / 65535
            downward = 0.12 + 0.28 * float(_hash1(idx * 7.91 + birth_time))
            magnitude = np.sqrt(horizontal**2 + downward**2)
            half_angle = 0.5 * np.arctan2(horizontal, downward)
            vel = ((right * np.cos(angle) + slit_long * np.sin(angle)) * magnitude * np.sin(half_angle)
                   + direction * magnitude * np.cos(half_angle)) * 0.70
        if minimum_flow:
            magnitude = max(float(np.linalg.norm(vel)), 1e-6)
            ray = vel / magnitude + (direction - vel / magnitude) * tightening
            ray /= max(float(np.linalg.norm(ray)), 1e-6)
            vel = ray * (magnitude + (speed - magnitude) * tightening)
            if tightening > 0.97:
                branch = 2

        # ---- JetGuide：出生时冻结坐标系，解析轨迹要用它 -------------------
        self.guide_origin[idx] = origin - slit_long * 0.001 + direction * 0.004
        if branch < -2:
            self.guide_origin[idx] = spawn
        self.guide_carrier[idx] = e.get('carrier', (0.0, 0.0, 0.0))
        self.guide_axis[idx] = direction
        self.guide_long[idx] = slit_long
        self.guide_seed[idx] = birth_time
        self.lane[idx] = 2 * ((idx * 0.61803398875 + np.floor(birth_time / 0.48) * 0.137) % 1) - 1
        if branch:
            self.lane[idx] = branch
        self.sheet[idx] = frozen
        self.launch_speed[idx] = speed
        # 出生那一档 `$strength_level` 与这一槽的水柱宽度倍数：显示端要读它们。
        # 来源: Slots.hlsl:51 slotDrive(s) / :57 slotWidth(s)
        self.level[idx] = float(nozzle['level'])
        self.width[idx] = width_scale

        # Births are backdated with the shader's closed-form launch equation.
        vel = vel + self.guide_carrier[idx]
        self.free_velocity[idx] = vel
        gravity = _gravity() * jet_gravity_scale(speed, 0)
        pos_b = spawn + vel * back_time + 0.5 * gravity * back_time * back_time
        vel_b = vel + gravity * back_time

        self.pos[idx] = pos_b
        self.vel[idx] = vel_b
        self.age[idx] = back_time
        self.travel[idx] = 0
        self.slot[idx] = int(e.get('slot', 0))
        self.alive[idx] = True
        if e.get('trickle', False) and idx % 5 != 0:
            self.alive[idx] = False
        if branch < -2 and (idx + int(np.floor(birth_time / 0.48))) & 1:
            self.alive[idx] = False
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
        """Reset the pool; births are evaluated at each shader substep timestamp."""
        self.alive[:] = False
        self.age[:] = 0
        self.time = 0.0
        self._opening[:] = 1
        self.clamp_hits = self.clamp_checks = 0
        self._born = np.zeros(self.capacity, dtype=bool)

    def recycle(self, rng):
        """FluidSolver PHASE_PREDICT: only births in the current substep."""
        if not self.emitters:
            return
        n = self.per_emitter_capacity
        index = np.arange(self.capacity)
        cycle = np.floor(self.time / self.lifetime)
        phase = n - 1 - index % n
        scheduled = (cycle + (phase + 0.78 * _hash1(index * 3.71 + cycle * 7.13)) / n) * self.lifetime
        previous = scheduled > self.time
        cycle[previous] -= 1
        scheduled[previous] = (cycle[previous] + (phase[previous] + 0.78 * _hash1(
            index[previous] * 3.71 + cycle[previous] * 7.13)) / n) * self.lifetime[previous]
        age = self.time - scheduled
        self._born = (age >= 0) & (age < self._current_dt)
        for idx in np.flatnonzero(self._born):
            k = int(idx) // n
            if not self.emitters[k].get('enabled', True):
                self._born[idx] = False
                continue
            self._spawn_one(int(idx), k, rng, back_time=float(age[idx]))
        for k, e in enumerate(self.emitters):
            if not e.get('enabled', True):
                self.alive[k * n:(k + 1) * n] = False

    def _advance_substep(self, dt, rng, first=False):
        self._current_dt = dt
        self._nozzle_dt = dt if first else dt * 2
        self.time += dt
        self.recycle(rng)
        self._substep(dt, rng)
        if not first:
            for k, e in enumerate(self.emitters):
                nozzle = self._emitter_nozzle(e, emitter_index=k)
                self._opening[k] = advance_nozzle_opening(
                    self._opening[k], nozzle['pressure'], self._frame_seconds)

    def step(self, frame_seconds, rng):
        """One game frame, with exactly two FluidSubstep dispatches."""
        if not self._physics_warned and self.physics_source == 'default':
            self._physics_warned = True
            print('[FluidSim][WARN] 未调用 set_physics，正在用默认重力倍率 1；'
                  '节点默认倍率为 0.25（4x 差异）。')
        self._frame_seconds = frame_dt(frame_seconds)
        self._advance_substep(self._frame_seconds / 2, rng, first=True)
        self._advance_substep(self._frame_seconds / 2, rng)

    def _substep(self, dt, rng):
        idx = np.flatnonzero(self.alive)
        if not len(idx):
            return
        born = getattr(self, '_born', np.zeros(self.capacity, dtype=bool))[idx]
        self.age[idx[~born]] += dt
        retirement = self.lifetime[idx].copy()
        for e in self.emitters:
            if e.get('trickle', False) and e.get('trickle_lifetime', 0) > 0:
                retirement[self.slot[idx] == int(e.get('slot', 0))] = e['trickle_lifetime']
        self.alive[idx] &= self.age[idx] < retirement
        p = self.pos[idx].copy()
        v = self.vel[idx].copy()
        owners = self.slot[idx]
        amount = self.sheet[idx]
        scale = np.array([jet_gravity_scale(float(self.launch_speed[i]),
                                            float(self.travel[i])) for i in idx])
        gravity = _gravity()[None, :] * scale[:, None]
        for k, e in enumerate(self.emitters):
            if e.get('trickle', False):
                gravity[owners == int(e.get('slot', 0))] *= 0.5
        accel = gravity + force_field_accel(p, self.force_fields, self.force_scale, owners)
        for e in self.emitters:
            if not e.get('trickle', False):
                continue
            selected = ((owners == int(e.get('slot', 0)))
                        & (self.age[idx] >= float(e.get('bend_delay', 0))))
            root = np.asarray(e.get('root', (0, 0, 0)))
            h = p[selected, :2] - root[:2]
            length = np.linalg.norm(h, axis=1)
            h0 = self.guide_origin[idx[selected], :2] - root[:2]
            inward = -np.einsum('ij,ij->i', h, v[selected, :2]) / np.maximum(length, 1e-4)
            side = np.einsum('ij,ij->i', h, h0) >= 0
            weight = (float(e.get('bend_strength', 0)) * np.clip((length - 0.08) / 0.04, 0, 1)
                      * side * (1 - _smoothstep(0.4, 0.8, inward)))
            bend = -h / np.maximum(length, 1e-4)[:, None] * weight[:, None]
            accel[np.flatnonzero(selected), :2] += bend
        pred = p + v * dt + accel * dt * dt
        velocity = v + accel * dt
        pred[born] = p[born]
        velocity[born] = v[born]

        # Released leaf particles never enter Density/Correct or viscosity.
        # Keep the analytic velocity: (pred-p)/dt would erase it in the guide.
        leaf = amount > 0.001
        analytic = np.zeros(len(idx), dtype=bool)
        for j in np.flatnonzero(leaf):
            g = idx[j]
            focus = sheet_focus_distance(float(amount[j]))
            if born[j] or self.travel[g] < focus:
                guide = dict(origin=self.guide_origin[g], axis=self.guide_axis[g],
                             longitudinal=self.guide_long[g], carrier=self.guide_carrier[g],
                             speed=float(self.launch_speed[g]), amount=float(amount[j]),
                             lane=float(self.lane[g]), seed=float(self.guide_seed[g]))
                gg = _gravity() * jet_gravity_scale(float(self.launch_speed[g]), 0)
                pos_g, vel_g, travel = evaluate_jet_guide(guide, self.age[g], focus, gg)
                ff = force_field_accel(pos_g[None, :], self.force_fields,
                                      self.force_scale, owners[j:j+1])[0]
                age = self.age[g]
                pred[j] = pos_g + ff * (0.5 * age * age)
                velocity[j] = vel_g + ff * age
                self.travel[g] = travel
                analytic[j] = True
        released = np.abs(amount) > 0.001
        free = self.lane[idx] < -2
        for j in np.flatnonzero(free):
            g = idx[j]
            age = self.age[g]
            base = self.guide_origin[g] + self.free_velocity[g] * age
            ff = force_field_accel(base[None, :], self.force_fields,
                                  self.force_scale, owners[j:j+1])[0]
            pred[j] = base + 0.5 * (_gravity() + ff) * age * age
            velocity[j] = self.free_velocity[g] + _gravity() * age
            self.travel[g] = np.linalg.norm(pred[j] - self.guide_origin[g])
            analytic[j] = True

        # Each GPU slot searches only its own PRIMARY_PER_SLOT block.
        for owner in np.unique(owners[~released & ~free]):
            local = np.flatnonzero((owners == owner) & ~released & ~free)
            q = pred[local].copy()
            old = p[local].copy()
            old[born[local]] -= v[local][born[local]] * dt
            for _ in range(ITERATIONS):
                density, grad, grad2 = self._density_terms(q)
                lam = -np.maximum(density / RHO - 1, 0) / (
                    grad2 + np.einsum('ij,ij->i', grad, grad) + LAMBDA_EPS)
                correction = self._correction(q, lam)
                length = np.linalg.norm(correction, axis=1)
                q += correction * np.minimum(1, CORRECTION_CLAMP / np.maximum(length, 1e-9))[:, None]
            vv = (q - old) / dt
            viscous, cohesion, smooth = self._viscous_cohesion(q, old, dt, density)
            vv += dt * (viscous + cohesion * COHESION_SCALE) + smooth * SMOOTHING_SCALE
            pred[local] = q
            velocity[local] = vv
        # Negative sheet amounts are released low-flow particles too, but their
        # ordinary (non-analytic) positions still advance in PHASE_PREDICT.
        advancing = ~analytic
        previous = p.copy()
        previous[born] -= v[born] * dt
        self.travel[idx[advancing]] += np.linalg.norm(pred[advancing] - previous[advancing], axis=1)
        speed = np.linalg.norm(velocity, axis=1)
        if SPEED_CLAMP:
            self.clamp_hits += int(np.count_nonzero(speed > MAX_SPEED))
            self.clamp_checks += len(idx)
            velocity *= np.minimum(1, MAX_SPEED / np.maximum(speed, 1e-9))[:, None]
        if self.ground_z is not None:
            hit = pred[:, 2] < float(self.ground_z) + self.radius
            pred[hit, 2] = float(self.ground_z) + self.radius
            self.alive[idx[hit]] = False
        finite = np.isfinite(pred).all(axis=1) & np.isfinite(velocity).all(axis=1)
        self.alive[idx] &= finite
        self.pos[idx] = pred
        self.vel[idx] = velocity
        if hasattr(self, '_born'):
            self._born[:] = False

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
        g = _gradient(pred[i] - pred[j], r) * (MASS / RHO)
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
        g = _gradient(pred[i] - pred[j], r) * volume
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
        valid = r >= 1e-7
        i, j, r, r2 = i[valid], j[valid], r[valid], r2[valid]
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
                 seed=20261029, warmup_frames=None, subframes=2, adaptive_substeps=False,
                 force_fields=None, force_scale=1.0, radius=FLUID_RADIUS):
        self.rng = np.random.default_rng(seed)
        self.sim = Simulator(capacity, emitters, ground_z=ground_z,
                             force_fields=force_fields, force_scale=force_scale,
                             radius=radius)
        self.sim.prime(self.rng)
        self.sub = max(1, int(subframes))
        self.step_seconds = 1.0 / float(fps) / self.sub
        # 显式粘性的稳定预算：dt_safe = 0.4 * H^2 / (8 * nu)。
        # 游戏侧同样是显式（FluidSolver.hlsl:444 累加、:458 乘 DT），但它每步把速度
        # 钳在 6 m/s（FluidSolver.hlsl:480），所以发散不会显形。预览没有那层钳制，
        # 因此必须自己按粘度选子步；撞上限时如实回报 unstable_viscosity。
        self.unstable_viscosity = None
        try:
            _nu = float(_VISCOSITY_OVERRIDE)
        except Exception:
            _nu = 0.0
        dt_frame = frame_dt(1.0 / float(fps))
        self.step_seconds = dt_frame / self.sub
        self.sim._frame_seconds = dt_frame
        # 子步下限 2：游戏每帧两次 FluidSubstep（Probe.ini:939/942，每步 x239 = dt/2），
        # 1 次整步 != 2 次半步（位置积分/碰撞裁剪/粘性耗散都不等价）。自动子步只许往上加。
        if self.sub < 2:
            self.sub = 2
            self.step_seconds = dt_frame / 2.0
        # 自适应子步改成**显式开启**：默认关闭，固定按 subframes（默认 2）走，
        # 与 Probe.ini:939/942 的两次 FluidSubstep 对齐。子步 8 会把真实发散藏起来，
        # 于是永远「预览正常、游戏爆炸」。
        self.substeps_source = "forced"
        if adaptive_substeps and _nu > 0.0:
            self.substeps_source = "adaptive"
            dt_safe = 0.4 * (0.012 ** 2) / (8.0 * max(_nu, 1e-9))
            want = int(np.ceil(dt_frame / max(dt_safe, 1e-9)))
            want = max(2, min(8, want))
            if want > self.sub:
                self.sub = want
                self.step_seconds = dt_frame / self.sub
            if want >= 8 and dt_frame / self.sub > dt_safe:
                self.unstable_viscosity = dict(nu=_nu, dt=dt_frame,
                                           dt_safe=dt_safe, sub=self.sub)
        if warmup_frames is None:
            warmup_frames = int(np.ceil(float(self.sim.lifetime.max()) / dt_frame))
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
            phase = self.units_done % self.sub
            self.sim._advance_substep(self.step_seconds, self.rng, first=phase == 0)
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

    每个预览帧默认推进两个游戏子步；显式指定更多 subframes 仅用于离线诊断。

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
