# -*- coding: utf-8 -*-
"""流体探针（LiquidGravityProbe）后处理节点。

部署形态：独立前置 ini（B 方案）
--------------------------------
每份导出的 mod 自带一套完整的 LiquidGravityProbe：

    <mod>/
      fluid_probe.ini                        主 ini，namespace = Fluid_<id>
      res/fluid_probe/
        camera/*.hlsl        (+ .bin)
        fluid/*.hlsl + Composite.ini          namespace = Fluid_<id>\\Composite
        surface/*.hlsl + ProfileLoader.ini    namespace = Fluid_<id>\\JSONProfiles
        Profiles/_index.json                  空索引

为什么是独立前置而不是注入宿主 ini
----------------------------------
参考实现三份 ini 是**层级引用**：``Probe.ini`` 的 ``namespace = LiquidGravityProbe``，
``fluid/Composite.ini`` 的 ``namespace = LiquidGravityProbe\\Composite``，
``surface/ProfileLoader.ini`` 的 ``namespace = LiquidGravityProbe\\JSONProfiles``；
``Probe.ini:252`` 用 ``CommandList\\LiquidGravityProbe\\Composite\\FluidComposite``
靠这层父子关系定位。

若 10 个 mod 都把流体塞进自己的宿主 ini 并都用同一个 namespace，
3Dmigoto 对重复段的处理是 ``Duplicate section found`` + 整段丢弃
（``IniHandler.cpp``），只有第一份能活。

所以每份 mod 用唯一 namespace，互不可见。

角色门控
--------
参考实现的 ``$costume_mods`` 全文只被读、从未被赋值（5 处门控全靠它），
3Dmigoto 未定义变量求值为 0 ⇒ 整条链跳过。这里统一换成 ``$fluid_present``：

- 为宿主每个 ``hash =`` 生成一段 gate：``[TextureOverrideFluidGate_<hash>]``
  + ``hash = <hash>`` + ``$fluid_present = 1``
- ``[Present]`` 里 ``post $fluid_present = 0``

依据 ``CommandList.cpp:1394-1406``：同一 hash 的所有 TextureOverride 都会执行
（``for (i = 0; i < matches.size(); i++)``），所以在自己的 namespace 里追加一段
只写变量的 gate 是无副作用的。

着色器缓存（.bin）
------------------
``CommandList.cpp:2598-2601`` 编译后 ``set_file_last_write_time(cache_path, &timestamp)``
把 bin 的 mtime 回写成 hlsl 的 mtime，读缓存时
``CommandList.cpp:2409-2413`` 要求 ``CompareFileTime`` 返回 0（精确相等）。

⇒ 只要 **hlsl 与 bin 成对复制并保持各自 mtime**，缓存即可命中；
一旦改了 hlsl 内容（本节点的 IniParams 重映射），缓存就失效，
必须丢弃对应 bin 强制重编译。

不做什么
--------
- **不改段名**：namespace 已隔离，再加后缀只让 ini 与 hlsl 的对照变难。
- **不改变量名**：除 ``$costume_mods`` → ``$fluid_present`` 这一处语义替换。
- **不动宿主 ini**：B 方案下流体与宿主唯一耦合就是这个门控变量，
  而它由流体自己的 gate 段写入，宿主无需配合。
"""

import importlib
import json
import os
import re
import time

import bpy

from .node_postprocess_base import SSMTNode_PostProcess_Base
from ..common.safe_write import (
    write_text_if_changed,
    write_bytes_if_changed,
    copy_file_if_changed,
)
from . import fluid_ini_merge
from .fluid_ini_merge import (clamp_tint, locate_draw_line, resolve_first_index, scan_mesh_draws)


#: 模板 ini（参考实现原样复制，未加工）。
TEMPLATE_INI = "Probe.ini"

#: 主 ini 的输出文件名。
OUTPUT_INI = "fluid_probe.ini"

#: 着色器部署子目录（相对 mod 根）。3Dmigoto 的 ``cs =`` 路径相对 ini 所在目录。
DEPLOY_SUBDIR = "res/fluid_probe"

#: 原版 namespace 根。
SRC_NAMESPACE = "LiquidGravityProbe"

#: 参考实现的流体参数落在 IniParams[237..243] 与 [251..255]。
#: 29..40 是连续 12 个长期空号段（实测扫过 ZZMI\Mods 全树，见具体清单）。
#: ini 与 hlsl **必须用同一张表**，任一处漏改表现为「参数完全无效」。
#:
#: 力场的两个开关（``x41``/``y41``）**故意不在这张表里**：41/42 本身就是全树
#: 空号，写死它意味着不论「迁移号段」开不开都对，也不依赖重映射这一步。
FLUID_INI_PARAM_MAP = {
    237: 29,
    238: 30,
    239: 31,
    240: 32,
    241: 33,
    242: 34,
    243: 35,
    251: 36,
    252: 37,
    253: 38,
    254: 39,
    255: 40,
}

#: 需要一起搬到 res/fluid_probe/ 的子目录（含目录内的 ini）。
SUBDIRS = ("camera", "fluid", "surface")

#: 两份子 ini 走自己的生成器，不整文件复制。
GENERATED_SUB_INIS = ("Composite.ini", "ProfileLoader.ini")

#: Profiles 目录只带空索引，不带具体角色的发射器数据。
EMPTY_INDEX = {"schema_version": 1, "owners": {}}

#: gate 段名前缀（用于辨识，不影响解析）。
GATE_PREFIX = "TextureOverrideFluidGate_"

#: ``[Resource\SurfaceProfiles]`` 的容量：``Probe.ini:308-311`` 声明
#: ``RWStructuredBuffer / stride=16 / array=20480``，而 ``LoadProfilesJSON.hlsl:55``
#: 用 ``row = id * PROFILE_ROWS``（``PROFILE_ROWS=5``，见 ``fluid/ParticleConfig.hlsl``），
#: 所以可用的全局 id 是 ``20480 / 5 = 4096`` 个。``LoadProfilesJSON.hlsl:54``
#: 的 ``v[0] >= 4096`` 也是同一个界。
PROFILE_CAPACITY = 4096

#: 每条记录占的 ``float4`` 行数（``LoadProfilesJSON.hlsl:59-63``）。
PROFILE_ROWS = 5

#: ``LoadProfilesJSON.hlsl:56``：``|v[13..15]| > .25`` 直接拒收。
MAX_LOCAL_OFFSET = 0.25

#: 面板上所有长度**一律米**，和物理常量、着色器、ini 里的单位完全一致。
#:
#: 早先这里是混的：粒子半径/地面高度/水柱粗细用毫米，力场半径与偏移、发射器
#: 局部偏移、吸收深度用米。同一个面板里 `2.8` 和 `0.0028` 并存，改参数时要先
#: 想「这个是哪个单位」，错一次也不报错、只是水柱粗 1000 倍或干脆看不见。
#:
#: 统一到米之后**没有任何换算**：面板填 0.0028 就是 FLUID_RADIUS，填 0.15 就是
#: 力场的 0.15 m，和 `FluidSolver.hlsl` / `ForceField.hlsl` 里的数可以直接对读。
#: 代价是小数多两位，换来的是「所见即所算」。


#: ``LoadProfilesJSON.hlsl:57``：``v[1] <= 100`` 时 ``v[2..4]`` 必须非负且和为 1。
#: 用 ``-1`` 表示「不做重心校验」（参考实现里 ``aff816a729ed.json`` 的 3 条都是 -1）。
DEFAULT_PRIORITY = -1


# -- 参数改了就重建预览标记 ---------------------------------------------------
#
# 这条路径是**属性 update 回调**，不是 depsgraph handler。
# 先前用 ``depsgraph_update_post`` 时它只在「节点树被某个几何节点修改器引用」
# 时才能扫到节点（初版 ``_find_fluid_nodes`` 只扫场景物体），用户直接编辑
# 节点树、没挂修改器时回调扫不到任何东西，静默不动作 —— 表现就是「改了没反应」。
#
# update 回调由 Blender 在属性真正变化的当下同步调用，不依赖场景结构。

#: 上次重建时的指纹。``None`` 表示还没比对过。
_PREVIEW_STATE = {'fingerprint': None, 'busy': False}


def _auto_rebuild(id_data):
    """``id_data`` 是节点树。找到里面的流体节点并按指纹决定要不要重建标记。

    只在标记集合已经存在时动 —— 用户没点过预览就不该凭空往场景里塞物体。
    """
    if _PREVIEW_STATE['busy'] or id_data is None:
        return
    if bpy.data.collections.get(PREVIEW_COLLECTION) is None:
        return
    scene = bpy.context.scene or (bpy.data.scenes[0] if bpy.data.scenes else None)
    if scene is None:
        return
    for n in getattr(id_data, 'nodes', ()):
        if not isinstance(n, SSMTNode_PostProcess_FluidProbe):
            continue
        fp = _emitters_fingerprint(n)
        if fp == _PREVIEW_STATE['fingerprint']:
            return
        _PREVIEW_STATE['fingerprint'] = fp
        # 重建会改场景，可能再触发别的 update —— 挡住重入，否则递归。
        _PREVIEW_STATE['busy'] = True
        try:
            build_preview_markers(n, scene)
        finally:
            _PREVIEW_STATE['busy'] = False
        return


def _on_param_update(self, context=None):
    """所有影响预览几何的属性都挂这个 update 回调。"""
    _auto_rebuild(getattr(self, 'id_data', None))


def _on_trick_toggle(self, context=None):
    """``v[8]`` 是三态字段，缠流和常驻显示抢同一个位置。

    同时打开时 ``emitter_mode_code`` 里 ``vis`` 优先、缠流被静默吃掉，
    所以这里做成互斥：打开缠流就把常驻显示关掉。
    """
    if self.trick and self.vis:
        self.vis = False
    _on_param_update(self, context)


def _on_vis_toggle(self, context=None):
    if self.vis and self.trick:
        self.trick = False
    _on_param_update(self, context)


class SSMTFluidForceField(bpy.types.PropertyGroup):
    """一个挂在发射器上的力场。

    位置与方向都写**发射器自己的基**（right / up / forward）：运行时在
    ``ForceField.hlsl:38-41`` 读当帧的锚点行现算，所以发射器一动、一转，
    力场跟着走——这正是「力场挂在发射器上」要的行为。

    加速度单位是 m/s²，直接加进 ``FluidSolver.hlsl`` 三处数值积分的
    ``gravity``（``:269`` / ``:302`` / ``:366``），与重力同一量级才有可比性
    （重力 9.81，默认强度 3.0 大约是重力的三分之一）。
    """

    enabled: bpy.props.BoolProperty(
        name='启用', default=True,
        description='这个力场的开关，一个力场一份：1 = 生效，0 = 不生效（缺省 1）。'
                    '导出时同时写两处同源的东西 —— ResourceFluidForceField 行里的 '
                    'enabled 位，以及运行期变量 $fluid_force_enable_<E>_<K>'
                    '（E = 发射器序号，K = 该发射器里第几个力场，都从 0 起）。'
                    '上一层的 $force_enabled / IniParams[41].x 是由「任一场启用」派生的，'
                    '没有单独的面板控件。',
    )
    kind: bpy.props.EnumProperty(
        name='类型',
        description='DIRECTIONAL 沿轴推；RADIAL 从中心向外（负强度变向内吸）；'
                    'VORTEX 绕轴旋。三者共用同一套衰减。',
        items=(
            ('DIRECTIONAL', '定向', '沿「方向轴」直推，不受中心位置影响'),
            ('RADIAL', '径向', '从力场中心向外推；强度取负数表示向内吸'),
            ('VORTEX', '涡旋', '绕「旋轴」转，强度取负反转方向'),
        ),
        default='RADIAL',
    )
    offset_u: bpy.props.FloatProperty(
        name='偏移 U(m)', default=0.0, min=-0.25, max=0.25, precision=4, step=10,
        description='沿发射器 right 轴离开锚点的距离（米）。'
                    'U/V/W 依次对应 ForceField.hlsl:52 的 right/up/forward。',
    )
    offset_v: bpy.props.FloatProperty(
        name='偏移 V(m)', default=0.0, min=-0.25, max=0.25, precision=4, step=10,
        description='沿发射器 up 轴离开锚点的距离（米）。',
    )
    offset_w: bpy.props.FloatProperty(
        name='偏移 W(m)', default=0.0, min=-0.25, max=0.25, precision=4, step=10,
        description='沿发射器 forward 轴（即喷射方向）离开锚点的距离（米）。',
    )
    axis_u: bpy.props.FloatProperty(
        name='轴 U', default=0.0, precision=4, step=1,
        description='方向轴 / 旋轴在发射器 right 上的分量。不必归一，shader 会归一。',
    )
    axis_v: bpy.props.FloatProperty(
        name='轴 V', default=0.0, precision=4, step=1,
        description='轴在发射器 up 上的分量。',
    )
    axis_w: bpy.props.FloatProperty(
        name='轴 W', default=1.0, precision=4, step=1,
        description='轴在发射器 forward 上的分量。默认 (0,0,1) = 沿喷射方向，'
                    '「定向」模式下就是顺着水流推。',
    )
    radius: bpy.props.FloatProperty(
        name='影响半径(m)', default=0.15, min=0.001, max=5.0, precision=4, step=10,
        description='超出这个半径完全没有力（米）。衰减是 (1-r²/R²)^衰减指数，'
                    '默认指数 2 时半径边界处值与斜率同时归零，所以出入口看不见断层。',
    )
    strength: bpy.props.FloatProperty(
        name='强度', default=3.0, precision=3, step=1,
        description='加速度，m/s²。重力是 9.81；3.0 大约能把水柱推开三分之一重力。'
                    'RADIAL 取负数向内吸，VORTEX 取负数反转旋向。',
    )
    falloff: bpy.props.FloatProperty(
        name='衰减指数', default=2.0, min=1.0, max=8.0, precision=2, step=10,
        description='衰减曲线 (1-r²/R²)^指数。2 是参考实现的形状（边界处值与'
                    '斜率同时归零，看不见断层）；调大则边缘更陡、力更集中在中心，'
                    '调到 1 会变成从中心到边缘线性变弱。',
    )



class SSMTFluidEmitter(bpy.types.PropertyGroup):
    """一个发射器的用户参数。

    导出时按 ``surface\\LoadProfilesJSON.hlsl:45-65`` 的 23 浮点格式落盘。
    字段顺序与那里逐个对应，见各字段的 docstring。
    """

    name: bpy.props.StringProperty(name='名称', default='emitter')
    object_name: bpy.props.StringProperty(update=_on_param_update, 
        name='Blender 物体',
        description='发射器所在的网格物体。用「取选中面」按钮自动填。'
                    '预览按「物体 + 面序号」取三角形，比手填首索引稳。',
        default='',
    )
    ib_file: bpy.props.StringProperty(
        name='IB 文件',
        description='宿主 mod 里的索引缓冲文件名（如 starbillybodyA.ib）。'
                    '只在导出诊断里用，运行期靠 first_index 在 mesh 捕获时匹配。',
        default='',
    )
    poly_index: bpy.props.IntProperty(update=_on_param_update, 
        name='面序号',
        description='该发射器所在三角面在物体内的序号（polygon.index，从 0 起）。'
                    '-1 = 未指定。指定后导出时按「该物体 draw 的起始索引 + 面序号×3」'
                    '反算首索引，比手填 first_index 更不容易错位。',
        default=-1, min=-1,
    )
    first_index: bpy.props.IntProperty(
        name='首索引',
        description='该三角面在 IB 里的起始索引号。'
                    '面序号 ≥ 0 时由导出期自动反算并覆盖本值；'
                    '面序号为 -1 时才用这里手填的数。',
        default=0, min=0,
    )
    bx: bpy.props.FloatProperty(update=_on_param_update, name='重心U', default=0.333333, min=0.0, max=1.0)
    by: bpy.props.FloatProperty(update=_on_param_update, name='重心V', default=0.333333, min=0.0, max=1.0)
    bz: bpy.props.FloatProperty(update=_on_param_update, name='重心W', default=0.333334, min=0.0, max=1.0)

    tilt: bpy.props.FloatProperty(update=_on_param_update, 
        name='倾角',
        description=('铰链角（度）。0 = 沿三角形法线喷出；正值朝表面「上」摆，'
                     '负值朝下摆，±90 = 贴表面朝上/朝下吹。'),
        default=0.0, min=-180.0, max=180.0,
    )
    yaw: bpy.props.FloatProperty(update=_on_param_update, 
        name='旋角',
        description=('绕法线的方位角（度）。与倾角配合：倾角决定偏离法线多少，'
                     '旋角决定往哪一边偏。0 = 上/下这个平面。'),
        default=0.0, min=-180.0, max=180.0,
    )
    roll: bpy.props.FloatProperty(update=_on_param_update, 
        name='滚转',
        description=(
            '绕喷流自身轴旋转「上方向」（度），决定 v[9..11] 的 up hint。'
            '参考实现 7 条实测里 up 各不相同，说明它确实被用到了；'
            'ResolveSurface:62-64 会把 up hint 与 forward 重新正交化，所以这里只需给方向。'
        ),
        default=0.0, min=-180.0, max=180.0,
    )

    width: bpy.props.FloatProperty(update=_on_param_update, 
        name='宽度', description='喷流宽度倍数（v[22]）。1 = 默认。',
        default=1.0, min=0.1, max=8.0,
    )
    level: bpy.props.IntProperty(update=_on_param_update, 
        name='强度档', description='相对全局强度的偏移（v[16]），-3..3。',
        default=0, min=-3, max=3,
    )
    absorption: bpy.props.FloatProperty(update=_on_param_update, 
        name='吸收深度 ($liquid_absorb_mm)', description='该股液体的吸收深度，米（v[21] 原样写米）。',
        default=0.000216894, min=0.000001, max=0.05, precision=6,
    )
    custom_color: bpy.props.BoolProperty(update=_on_param_update, 
        name='自定义颜色', description='勾选后使用下面的 tint，否则跟全局（v[17]）。',
        default=False,
    )
    tint_r: bpy.props.FloatProperty(update=_on_param_update, name='R', default=0.98, min=0.0, max=1.0)
    tint_g: bpy.props.FloatProperty(update=_on_param_update, name='G', default=0.98, min=0.0, max=1.0)
    tint_b: bpy.props.FloatProperty(update=_on_param_update, name='B', default=0.98, min=0.0, max=1.0)

    ox: bpy.props.FloatProperty(update=_on_param_update, name='偏移X(m)', default=0.0,
                                min=-0.25, max=0.25, precision=4, step=10,
                                description='沿 right 的局部偏移，米（v[13]）。限 ±0.25。')
    oy: bpy.props.FloatProperty(update=_on_param_update, name='偏移Y(m)', default=0.0,
                                min=-0.25, max=0.25, precision=4, step=10,
                                description='沿 up 的局部偏移，米（v[14]）。限 ±0.25。')
    oz: bpy.props.FloatProperty(update=_on_param_update, name='偏移Z(m)', default=0.0,
                                min=-0.25, max=0.25, precision=4, step=10,
                                description='沿 forward 的局部偏移，米（v[15]）。限 ±0.25。')

    tlen: bpy.props.FloatProperty(update=_on_param_update, 
        name='缠绕寿命', default=0.0, min=0.0, max=4.95, precision=3,
        description='仅缠绕模式生效：覆盖粒子寿命（秒），0 = 用全局 lifetime（0.48s）。'
                    'v[12] 打包 a*10000+b*100+c，a = round(tlen/0.05)；'
                    'shader 侧 AssignSlots.hlsl:82 用 floor(v12/10000)*0.05 解回，'
                    'FluidSolver.hlsl:121-122 拿它当 trickleLife。',
    )
    bend_delay: bpy.props.FloatProperty(update=_on_param_update, 
        name='弯曲延迟', default=0.0, min=0.0, max=4.95, precision=3,
        description='b = round(bendDelay/0.05)，上限 99。',
    )
    bend_str: bpy.props.IntProperty(update=_on_param_update, 
        name='弯曲强度', default=0, min=0, max=99,
        description='c = bendStr 本身（shader 侧乘 0.4 得到 TRICKLE.z）。上限 99。',
    )
    trick: bpy.props.BoolProperty(update=_on_trick_toggle, 
        name='缠流模式', description='开启后 v[8] 写 1 + trickSp，粒子沿路径缠绕。',
        default=False,
    )
    trick_sp: bpy.props.FloatProperty(update=_on_param_update, 
        name='缠流速度', default=0.1, min=0.01, max=7.0,
        description='v[8] = 1 + 本值（FluidSolver.hlsl:266 用 min(v[8]-1, 8) 当强度）。',
    )
    vis: bpy.props.BoolProperty(update=_on_vis_toggle, 
        name='常驻显示', description='v[8] = -1：部件被画时一直喷，忽略发射器自己的开关。',
        default=False,
    )


    #: 挂在本发射器上的力场。槽号就是发射器序号（导出时 ``x243 = idx``），
    #: 所以力场跟着发射器走，不需要单独维护一张「谁对应哪个槽」的表。
    force_fields: bpy.props.CollectionProperty(type=SSMTFluidForceField)
    force_field_index: bpy.props.IntProperty(default=0, min=0)

    #: 只开关这一个发射器的快捷键，原样写进 ``[KeyFluidEmitN]``。
    #: 留空时第 1 个发射器默认小键盘 1，第 2 个起不绑（免得几个发射器抢同一个键）；
    #: 只写 ``VK_F1`` 时导出会补 ``no_alt no_ctrl no_shift``，也可以写整串自己加修饰词。
    key: bpy.props.StringProperty(
        name='快捷键', default='',
        description='留空：第 1 个发射器默认小键盘 1；第 2 个起留空 = 不绑（避免多个'
                    '发射器抢同一个键）。只写 VK_F1 会自动补 no_alt no_ctrl no_shift。',
    )

    # ------------------------------------------------------------------
    # 运行期变量（每个发射器一个，键位只是手动入口，变量是唯一真相）
    # ------------------------------------------------------------------
    enable: bpy.props.BoolProperty(
        name='开关',
        default=True,
        description='= $fluid_emit_N（N = 这个发射器的序号，从 0 起；导出后叫 '
                    '$fluid_emit_N）。1 = 这个发射器出水，0 = 不出水；缺省 1。'
                    '面板这里给的是初值，键位与动画驱动写的是同一个变量。',
    )
    speed: bpy.props.FloatProperty(
        name='速度 (m/s)',
        default=0.0, min=0.0, max=6.0, precision=3,
        description='= $fluid_emitter_speed_N（N = 发射器序号）。0 = 沿用内部兜底 '
                    '$fluid_launch_speed（0.514 m/s，面板上不再单独暴露）；'
                    '>0 = 这个发射器单独用这个速度。动画驱动可以在 [Present] 里逐帧写它。',
    )


def _addon_root():
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _toolset_fluid_dir():
    return os.path.join(_addon_root(), "Toolset", "fluid")


def _read_text(path):
    with open(path, "r", encoding="utf-8-sig") as f:
        return f.read()


def _namespace_id(mod_export_path):
    """从输出目录名派生一个稳定的命名空间标识。

    只用 ``[A-Za-z0-9_]``：命名空间会写进 ini 的 ``namespace =`` 行，
    带空格/括号/中文时 3Dmigoto 的解析行为没有保证（参考实现本身用的就是
    纯 ASCII 名，例如 ``LiquidGravityProbe``、``TTLib``）。超过 40 字符截断。
    """
    name = os.path.basename(os.path.normpath(mod_export_path))
    safe = re.sub(r"[^A-Za-z0-9_]", "_", name)
    safe = re.sub(r"_+", "_", safe).strip("_")
    if not safe:
        safe = "mod"
    return "Fluid_" + safe[:40]


#: 门控 hash 收集：住到 fluid_ini_merge 里（纯函数，可单测）。
_collect_hashes = fluid_ini_merge.collect_gate_hashes


def _emitter_key_entries(emitters):
    """``[(槽号, 键位串)]``，喂给 ``apply_global_keys(emitter_keys=)`` 与 ``apply_emitter_keys``。

    策略只在这里定，不动 ``apply_emitter_keys`` 的契约（它认「空串 = 不绑」，
    测试直接依赖这一点）：

    * 第 1 个发射器（``index == 0``）留空 → ``VK_NUMPAD1``。总开关改成角色门控
      硬关之后这个键空了出来，给第一个发射器当专属开关最顺手；
    * 第 2 个起留空 → 保持空串（不绑）。都默认同一个键的话，按一下好几个
      发射器一起翻，等于没有单独开关。

    用户显式填的键位原样传下去（``apply_emitter_keys`` 会做
    ``no_alt no_ctrl no_shift`` 补全）。
    """
    entries = []
    for i, em in enumerate(emitters):
        spec = str(getattr(em, "key", "") or "").strip()
        if not spec and i == 0:
            spec = "VK_NUMPAD1"
        entries.append((i, spec))
    return entries


class SSMTNode_PostProcess_FluidProbe(SSMTNode_PostProcess_Base):
    bl_idname = 'SSMTNode_PostProcess_FluidProbe'
    bl_label = '流体探针'
    bl_description = '部署独立的 LiquidGravityProbe 前置：屏幕空间 PBF 流体 + 角色门控'

    #: 是否重映射 IniParams 号段（237-243/251-255 → 29-40）
    remap_ini_params: bpy.props.BoolProperty(
        name='迁移 IniParams 号段',
        description='把流体参数从 237-243/251-255 迁到 29-40。'
                    '237-245 被 14 个形态动画 mod 占满，其中 Mod_柳 形态anim.hlsl:74 '
                    '硬写 IniParams[238].x 与 Probe.ini:419 直接对撞。'
                    '开启后 hlsl 内容改变，对应的 .bin 缓存失效，首次启动需重新编译',
        default=True,
    )

    #: 是否带编译产物 .bin
    include_shaders_cache: bpy.props.BoolProperty(
        name='携带着色器缓存',
        description='复制参考实现编译好的 .bin 并保持 mtime 配对，首次启动免编译。'
                    '仅在「不迁移 IniParams 号段」且 hlsl 内容未改动时有效',
        default=False,
    )

    #: 力场总增益。全局乘在 ForceField.hlsl 的最终加速度上（IniParams[41].y），
    #: 调它等于同时缩放所有发射器的所有力场。
    force_scale: bpy.props.FloatProperty(
        name='力场总增益',
        description='乘在所有力场上的全局系数。0 = 全部关掉但保留设置，1 = 按各自强度',
        default=1.0, min=0.0, soft_max=10.0, precision=3, step=1,
    )

    #: 改参数后是否自动重算模拟。
    #: 标记（球/锥/力场线框）本来就跟手重建；模拟要跑 PBF，几秒钟，所以做成
    #: 「停手 0.8 秒后跑一次」，既不卡拖拽，又不用每次手点「模拟喷射」。
    auto_sim: bpy.props.BoolProperty(
        name='参数改动自动重算模拟',
        description='改完参数松手约 0.8 秒后自动重跑一次模拟与烘焙；关掉就只手动点模拟',
        default=True,
    )

    #: 是否生成角色门控段
    host_ini_path: bpy.props.StringProperty(
        name='宿主 ini（可选）',
        description=(
            '填了就用它收门控 hash，不用去导出目录里猜。'
            '留空则在导出时扫描输出目录里除 fluid_probe.ini 之外的所有 .ini'
        ),
        subtype='FILE_PATH',
        default='',
    )
    gate_by_host_hash: bpy.props.BoolProperty(
        name='角色门控',
        description='为宿主 ini 里每个 TextureOverride hash 生成 $fluid_present 置位段。'
                    '关闭后流体将常驻计算（不受角色是否在场影响）',
        default=True,
    )
    # 全局快捷键：出厂值直接填在输入栏里，用户一眼看到「现在按什么」，
    # 改成别的就覆盖，清空则回到出厂值。名字对齐
    # fluid_ini_merge.GLOBAL_KEY_SECTIONS 的第二列，改名就两边一起改。
    key_auto_pulse: bpy.props.StringProperty(
        name='自动喷射 ($auto_pulse)', default=fluid_ini_merge.GLOBAL_KEY_DEFAULTS['auto_pulse'],
        description='默认小键盘 4。清空 = 用默认。')
    key_manual_pulse: bpy.props.StringProperty(
        name='手动喷射（按住）', default=fluid_ini_merge.GLOBAL_KEY_DEFAULTS['manual_pulse'],
        description='默认小键盘 0。清空 = 用默认。')
    key_trickle: bpy.props.StringProperty(
        name='缠流模式', default=fluid_ini_merge.GLOBAL_KEY_DEFAULTS['trickle'],
        description='默认小键盘 9。清空 = 用默认。')

    emit_legacy_buffers: bpy.props.BoolProperty(
        name='旧版 .buf 兼容',
        description='额外生成 profiles.buf / attachment-offsets.buf。'
                    '新版 LiquidGravityProbe 改由 LoadProfilesJSON.hlsl 直读 JSON，'
                    '不再读这两个文件；只有还在用旧版 -zh-CN 前置、'
                    '或第三方工具链着它们时才需要打开。默认关。',
        default=False,
    )

    # ------------------------------------------------------------------
    # 全局外观（对应 Probe.ini:2-24 的 8 个对外变量）
    # ------------------------------------------------------------------
    strength_level: bpy.props.IntProperty(
        name='强度档 ($strength_level)', default=3, min=0, max=7,
        description='Probe.ini:14 的 $strength_level。Probe.ini:434-436 用它算全局强度：'
                    '0 档是 0.28，1-7 档是 0.565+(level-1)*0.5625。',
    )
    tint_r: bpy.props.FloatProperty(name='液体R ($liquid_tint_r)', default=0.9, min=0.0, max=1.0)
    tint_g: bpy.props.FloatProperty(name='液体G ($liquid_tint_g)', default=0.75, min=0.0, max=1.0)
    tint_b: bpy.props.FloatProperty(name='液体B ($liquid_tint_b)', default=0.03, min=0.0, max=1.0)
    absorb_m: bpy.props.FloatProperty(
        name='吸收深度(m)', default=0.015, min=0.0001, max=0.2, precision=5,
        description='Probe.ini:20 的 $liquid_absorb_mm。这里填米；导出时乘 1000 '
                    '写进那个以毫米命名的变量（它的 /1000 正好抵掉）。',
    )
    wet_darken: bpy.props.FloatProperty(
        name='湿润变暗 ($liquid_wet_darken)', default=0.85, min=0.0, max=2.0,
        description='Probe.ini:21 的 $liquid_wet_darken。',
    )
    puddle_reflect: bpy.props.FloatProperty(
        name='水洼反射 ($liquid_puddle_reflect)', default=0.15, min=0.0, max=4.0,
        description='Probe.ini:22 的 $liquid_puddle_reflect。',
    )
    stream_reflect: bpy.props.FloatProperty(
        name='水流反射 ($liquid_stream_reflect)', default=1.6, min=0.0, max=8.0,
        description='Probe.ini:23 的 $liquid_stream_reflect。',
    )
    scatter: bpy.props.FloatProperty(
        name='散射 ($liquid_scatter)', default=0.35, min=0.0, max=4.0,
        description='Probe.ini:24 的 $liquid_scatter。',
    )
    refraction: bpy.props.FloatProperty(
        name='折射 ($liquid_refraction)', default=0.02, min=0.0, max=1.0,
        description='Probe.ini:25 的 $liquid_refraction。',
    )
    depth_test: bpy.props.BoolProperty(
        name='深度测试 ($depth_test)', default=True,
        description='Probe.ini:13 的 $depth_test。1=被遮挡就裁掉，0=永远画在最前。',
    )
    auto_pulse: bpy.props.BoolProperty(
        name='自动喷射', default=False,
        description='Probe.ini:16 的 $auto_pulse。1=不等按键、每帧都发射。',
    )

    # ------------------------------------------------------------------
    # 预览模拟设置（点「跑模拟」时用；同时是默认值）
    # ------------------------------------------------------------------
    sim_frames: bpy.props.IntProperty(
        name='预览帧数', default=60, min=1, max=600,
        description='烘焙多少帧。60 帧 @30fps = 2 秒。',
    )
    sim_fps: bpy.props.FloatProperty(
        name='预览帧率', default=30.0, min=1.0, max=120.0,
        description='只影响时间轴间隔，不影响物理（物理按 DT 钳制逐步积分）。',
    )
    sim_capacity: bpy.props.IntProperty(
        name='粒子池', default=1024, min=64, max=8192,
        description='同时存在的粒子上限。参考实现是 SLOT_COUNT*64 = 1024。',
    )
    launch_speed: bpy.props.FloatProperty(
        name='发射速度 (m/s)',
        default=0.514, min=0.02, max=6.0, precision=4,
        description='出水速度，直接对应 $fluid_launch_speed（IniParams[46].x）。'
                    '0.514 是强度档 3 档的喷速；导出的 $strength_level 由这个速度反解，'
                    '所以喷嘴开口、片状程度、重力近场缩放都会跟着它一起变。',
    )
    particle_radius: bpy.props.FloatProperty(
        name='粒子半径(m) ($fluid_particle_radius)', default=0.0028, min=0.0005, max=0.05, precision=5,
        description='fluid/ParticleConfig.hlsl 的 FLUID_RADIUS = 0.0028 m。'
                    '这是物理粒子的尺度，决定碰撞/密度核的作用范围。',
    )
    gravity_scale: bpy.props.FloatProperty(
        name='重力倍率 ($fluid_gravity_scale)', default=1.0, min=-4.0, max=4.0, precision=3,
        description='1 = 9.81 m/s²；调小流动变慢；负值 = 反重力，火焰/蒸汽往上飘。写进 IniParams[47].x',
    )
    viscosity: bpy.props.FloatProperty(
        name='粘度 ($fluid_viscosity)', default=0.0, min=0.0, max=0.01, precision=7,
        description='运动粘度 m²/s；0 = 沿用参考实现 1e-6。值越大越粘稠。写进 IniParams[47].y',
    )
    volume_radius: bpy.props.FloatProperty(
        name='水柱粗细(m)', default=0.0, min=0.0, max=0.1, precision=5,
        description='体积化半径（米），决定画面上水柱的视觉直径。0 = 自动（粒子半径的 4.5 倍）。'
                    '调大：水柱变粗、更容易连成一片；调小：变细、但低于粒子间距时会露颗粒。'
                    '连续水柱的经验下限约为「最近邻中位间距」的 1.5 倍。',
    )
    solid: bpy.props.BoolProperty(
        name='体积化', default=True,
        description='开=点云转体积再转网格（像连续水柱）；关=直接画小球（像珍珠串）。',
    )
    ground_auto: bpy.props.BoolProperty(
        name='地面自动', default=True,
        description='按场景里最低的网格顶点当地面（排除预览标记和粒子对象本身）。',
    )
    ground_height: bpy.props.FloatProperty(
        name='地面高度(m)', default=0.0, min=-1000.0, max=1000.0, precision=4,
        description='「地面自动」关掉时才用这个值（米）。',
    )

    # ------------------------------------------------------------------
    # 发射器（可变长）
    # ------------------------------------------------------------------
    emitters: bpy.props.CollectionProperty(type=SSMTFluidEmitter)
    emitter_index: bpy.props.IntProperty(default=0)

    def draw_buttons(self, context, layout):
        col = layout.column(align=True)
        col.prop(self, 'remap_ini_params')
        row = col.row(align=True)
        row.enabled = not self.remap_ini_params
        row.prop(self, 'include_shaders_cache')
        col.prop(self, 'gate_by_host_hash')
        col.prop(self, 'emit_legacy_buffers')

        layout.separator()
        box = layout.box()
        box.label(text='全局外观')
        # 发射速度不再有全局项：每个发射器一个 $fluid_emitter_speed_N（面板在发射器
        # 那一组里）；$fluid_launch_speed 降级为内部兜底（0.514 m/s），不再画在这里。
        box.prop(self, 'particle_radius')
        box.prop(self, 'gravity_scale')
        box.prop(self, 'viscosity')
        r = box.row(align=True)
        r.prop(self, 'tint_r')
        r.prop(self, 'tint_g')
        r.prop(self, 'tint_b')
        box.prop(self, 'absorb_m')
        row = box.row(align=True)
        row.prop(self, 'wet_darken')
        row.prop(self, 'puddle_reflect')
        row = box.row(align=True)
        row.prop(self, 'stream_reflect')
        box.prop(self, 'scatter')
        box.prop(self, 'refraction')
        # 全局力场倍率（$force_scale → IniParams[41].y）：乘在所有力场的强度上。
        # 它是**全局**的东西，所以画在全局外观里，不进每个发射器的力场组。
        box.prop(self, 'force_scale', text='力场总增益（全局，乘在所有力场强度上）')

        layout.separator()
        box = layout.box()
        row = box.row(align=True)
        row.label(text='全局快捷键（出厂值已填好，改了就覆盖）', icon='KEYINGSET')
        col = box.column(align=True)
        r = col.row(align=True)
        r.prop(self, 'key_auto_pulse', text='自动喷')
        r = col.row(align=True)
        r.prop(self, 'key_manual_pulse', text='按住喷')
        r.prop(self, 'key_trickle', text='缠流')
        # 出厂值现在直接填在栏里，所以不能拿「非空」判有没有改过 ——
        # 要比出差别。清空也算「改回默认」，不该报警。
        changed = any(
            fluid_ini_merge.normalise_key(getattr(self, 'key_' + f, ''))
            not in ('', fluid_ini_merge.normalise_key(
                fluid_ini_merge.GLOBAL_KEY_DEFAULTS.get(f, '')))
            for _s, f in fluid_ini_merge.GLOBAL_KEY_SECTIONS
        )
        if changed:
            col.label(text='改了默认键：和别的 mod 撞键时按下去两边一起动', icon='ERROR')

        layout.separator()
        box = layout.box()
        row = box.row(align=True)
        row.label(text='发射器 (%d)' % len(self.emitters))
        row.enabled = len(self.emitters) < fluid_ini_merge.EMITTER_CAPACITY
        row.operator('ssmt.fluid_emitter_add', text='', icon='ADD')
        row.operator('ssmt.fluid_emitter_remove', text='', icon='REMOVE')
        row.operator('ssmt.fluid_emitter_clear', text='', icon='X')
        if len(self.emitters) >= fluid_ini_merge.EMITTER_CAPACITY:
            box.label(text='已达上限 %d 个发射器' % fluid_ini_merge.EMITTER_CAPACITY,
                      icon='ERROR')

        if len(self.emitters):
            box.template_list(
                'SSMT_UL_fluid_emitters', '',
                self, 'emitters',
                self, 'emitter_index',
                rows=4,
            )
            idx = min(self.emitter_index, len(self.emitters) - 1)
            if idx >= 0:
                e = self.emitters[idx]
                sub = box.column(align=True)
                sub.prop(e, 'name')

                sub.separator()
                sub.label(text='① 绑在哪个面', icon='MESH_DATA')
                sub.prop(e, 'object_name')
                r = sub.row(align=True)
                r.prop(e, 'poly_index')
                r.operator('ssmt.fluid_emitter_pick_face', text='取选中面')
                if e.poly_index < 0:
                    sub.label(text='还没绑面：点「取选中面」或直接填面序号', icon='ERROR')

                sub.separator()
                sub.label(text='② 开关与行为', icon='KEYINGSET')
                sub.prop(e, 'key', text='单独开关')
                sub.label(text='留空 = 跟着 NUMPAD1 总开关走', icon='INFO')
                row = sub.row(align=True)
                row.prop(e, 'enable')
                row.label(text='发射器 %d 开关 → $fluid_emit_%d（1 开 / 0 关）'
                               % (idx, idx))
                sub.label(text='   键位、这里、动画驱动写的是同一个 $fluid_emit_%d' % idx,
                          icon='INFO')
                r = sub.row(align=True)
                r.prop(e, 'trick', text='缠流', toggle=True)
                r.prop(e, 'vis', text='常驻显示', toggle=True)
                if e.trick:
                    sub.prop(e, 'trick_sp')
                    r = sub.row(align=True)
                    r.prop(e, 'tlen')
                    r.prop(e, 'bend_delay')
                    r.prop(e, 'bend_str')
                elif e.vis:
                    sub.label(text='部件一被画出来就喷，NUMPAD1 管不到它', icon='INFO')
                else:
                    sub.label(text='由 NUMPAD1 总开关和喷射键控制', icon='INFO')

                sub.separator()
                sub.label(text='③ 喷口在三角形内的位置', icon='CURSOR')
                r = sub.row(align=True)
                r.prop(e, 'bx')
                r.prop(e, 'by')
                r.prop(e, 'bz')

                sub.separator()
                sub.label(text='④ 喷射方向', icon='ORIENTATION_GLOBAL')
                r = sub.row(align=True)
                r.prop(e, 'tilt')
                r.prop(e, 'yaw')
                r.prop(e, 'roll')

                sub.separator()
                sub.label(text='⑤ 外观', icon='COLOR')
                r = sub.row(align=True)
                r.prop(e, 'width')
                r.prop(e, 'level')
                sub.prop(e, 'absorption')
                r = sub.row(align=True)
                r.prop(e, 'custom_color')
                if e.custom_color:
                    rr = sub.row(align=True)
                    rr.prop(e, 'tint_r')
                    rr.prop(e, 'tint_g')
                    rr.prop(e, 'tint_b')

                sub.separator()
                # 发射速度：唯一可编辑入口就在这一行。变量名与「0 = 沿用全局」写在
                # 同一行的 label 与属性 description 里，不再另开只读块显示第二份数值。
                r = sub.row(align=True)
                r.label(text='发射速度 %d (m/s)' % idx)
                r.prop(e, 'speed', text='')
                sub.label(text='   → $fluid_emitter_speed_%d；0 = 沿用内部兜底 %.4g m/s'
                               % (idx, float(getattr(self, 'launch_speed', 0.514))),
                          icon='INFO')

                sub.separator()
                sub.label(text='⑥ 位置微调（沿喷口局部轴，单位米）', icon='EMPTY_AXIS')
                r = sub.row(align=True)
                r.prop(e, 'ox')
                r.prop(e, 'oy')
                r.prop(e, 'oz')

                sub.separator()
                ff = sub.box()
                row = ff.row(align=True)
                row.label(text='⑦ 力场（相对发射器，跟着它动）', icon='FORCE_FORCE')
                row.operator('ssmt.fluid_force_field_add', text='', icon='ADD')
                row.operator('ssmt.fluid_force_field_remove', text='', icon='REMOVE')
                if len(e.force_fields):
                    ff.template_list(
                        'SSMT_UL_fluid_force_fields', '',
                        e, 'force_fields',
                        e, 'force_field_index',
                        rows=2,
                    )
                    # 一个力场一个子框：勾选框用的是**这个力场自己的** `enabled`
                    # （写 $fluid_force_enable_<E>_<K>），3 个力场就是 3 个独立的勾。
                    # 2026-11-03 修：上一版把勾画在表头行末尾、且 text=''，
                    # 看起来像整组共用一个开关。
                    for k, f in enumerate(e.force_fields):
                        fb = ff.box()
                        head = fb.row(align=True)
                        head.label(text='力场 %d' % k, icon='FORCE_FORCE')
                        head.prop(f, 'enabled', text='启用')
                        head.label(text='→ $fluid_force_enable_%d_%d' % (idx, k))
                        r = fb.row(align=True)
                        r.prop(f, 'kind', text='')
                        r = fb.row(align=True)
                        r.prop(f, 'radius')
                        r.prop(f, 'strength')
                        r.prop(f, 'falloff')
                        r = fb.row(align=True)
                        r.prop(f, 'offset_u')
                        r.prop(f, 'offset_v')
                        r.prop(f, 'offset_w')
                        r = fb.row(align=True)
                        r.prop(f, 'axis_u')
                        r.prop(f, 'axis_v')
                        r.prop(f, 'axis_w')
                        fb.label(text='   偏移与轴：U=右 V=上 W=前；强度单位 m/s²',
                                 icon='INFO')
                else:
                    ff.label(text='未加力场：这一项零开销', icon='INFO')

        layout.separator()
        box = layout.box()
        row = box.row(align=True)
        row.label(text='预览模拟设置（只影响 Blender，不写进游戏）', icon='PHYSICS')
        row = box.row(align=True)
        row.prop(self, 'sim_frames')
        row.prop(self, 'sim_fps')
        row = box.row(align=True)
        row.prop(self, 'sim_capacity')
        box.prop(self, 'volume_radius')
        box.prop(self, 'solid')
        row = box.row(align=True)
        row.prop(self, 'ground_auto')
        if self.ground_auto:
            row.label(text='= 场景最低点')
        else:
            row.prop(self, 'ground_height')

        layout.separator()
        row = layout.row(align=True)
        row.operator('ssmt.fluid_preview_build', text='生成预览标记')
        row.operator('ssmt.fluid_preview_clear', text='清除预览')
        box = layout.box()
        box.prop(self, 'auto_sim')
        if self.auto_sim:
            box.label(text='改参数后自动重算模拟（停手约 0.8 秒后跑一次）', icon='INFO')

        prog = sim_progress()
        if prog is not None:
            factor, text = prog
            pbox = layout.box()
            pbox.progress(factor=factor, type='BAR', text=text)

        check = layout.row()
        check.operator('ssmt.fluid_check_host', text='检查宿主 ini（只读）')

        # 导出真值摘要：不点导出也能看见「到底会写进游戏什么」。
        # 现场重算一遍而不是缓存——发射器数量是个位数，这点开销比缓存失效的
        # 麻烦便宜得多（缓存要在每次属性变更时失效，漏一次就显示旧数据）。
        if len(self.emitters):
            layout.separator()
            box = layout.box()
            box.label(text='导出摘要（实际写入 gpu_data 的值）')
            for line in self._summary_lines():
                box.label(text=line)


    def _summary_lines(self):
        """把「用户输入 → 最终 23 浮点」摊开成几行，供面板显示。

        只读不写，任何异常都吞掉——面板每帧都会调，这里抛异常会把整个节点面板
        拖死，比少显示一行严重得多。
        """
        try:
            profiles = self._build_profiles()
        except Exception as exc:  # noqa: BLE001 - 面板不能因为数据坏掉就崩
            return ['  <计算失败: %s>' % exc]

        if not profiles:
            return ['  还没有能导出的发射器（面序号与 first_index 都为空）']

        name, payload = profiles[0]
        gpu_data = payload.get('gpu_data') or []
        emitters = payload.get('emitters') or []
        out = ['  Profiles/%s.json，%d 条记录' % (name, len(gpu_data))]

        for i, gpu in enumerate(gpu_data):
            em = emitters[i] if i < len(emitters) else {}
            poly = -1
            if i < len(self.emitters):
                poly = int(getattr(self.emitters[i], 'poly_index', -1))
            out.append('  #%d %s  面 #%d → first_index %s' % (
                i, em.get('name', ''), poly, em.get('first_index', '?')
            ))
            out.append('     fwd (%.3f, %.3f, %.3f)  up (%.3f, %.3f, %.3f)' % (
                gpu[5], gpu[6], gpu[7], gpu[9], gpu[10], gpu[11]
            ))
            out.append('     bary (%.3f, %.3f, %.3f)  off (%.3f, %.3f, %.3f)' % (
                gpu[2], gpu[3], gpu[4], gpu[13], gpu[14], gpu[15]
            ))
            out.append('     ' + ' '.join('%.3g' % v for v in gpu))
        return out

    # ------------------------------------------------------------------
    # 发射器 → 23 浮点
    # ------------------------------------------------------------------
    def _emitter_to_gpu(self, idx, e, default_tint):
        """按 ``surface\\LoadProfilesJSON.hlsl:45-65`` 的格式把一个发射器转成 23 浮点。

        每个字段的落点（逐条对到源文件）：

        ======  =====================================================  ====================
        下标    含义                                                   证据
        ======  =====================================================  ====================
        v[0]    全局 id（决定 ``profiles`` 的行号 ``id*5``）            ``:55``
        v[1]    优先级，越小越优先；``-1`` 表示不做 bary 校验           ``:57`` / ``ResolveSurface:50``
        v[2..4] 重心坐标，必须非负且和 ≈ 1                              ``:57`` / ``:59``
        v[5..7] forward 的 TBN 系数，``ResolveSurface:61`` 投影后成朝向  ``:60``
        v[8]    发射模式：0 普通 / -1 vis / 1+trickSp（Sp=v8-1）       ``Slots.hlsl:33-36``
        v[9..11] up 的 TBN 系数                                         ``:61`` / ``ResolveSurface:62``
        v[12]   ``a*10000+b*100+c``（tlen/bendDelay/bendStr），仅 trick  ``AssignSlots:83``
        v[13..15] 局部偏移（米，沿 right/up/forward），限 ±0.25          ``:56`` / ``:64``
        v[16]   强度档偏移                                              ``:62``
        v[17]   是否有自定义颜色                                        ``:62``
        v[18..20] tint                                                  ``:63``
        v[21]   吸收深度（米）                                          ``:62``
        v[22]   宽度倍数                                                ``:62``
        ======  =====================================================  ====================

        ``tilt/yaw`` 的语义是本节点自己定的球面坐标（相对三角形法线）：
        ``tilt=0`` ⇒ ``v[5..7] = (0,0,1)`` ⇒ ``ResolveSurface:61`` 让 forward 等于
        三角形法线。参考实现（waterpick）的 tilt 不是这个语义——拿它的 7 条实测
        反解，tilt=50 对应的 ``v[5..7].z`` 是 0.288（≈73°），所以两边的「50」
        不是同一个角。这里只保证**本节点内部自洽**，并与 ``ResolveSurface`` 的
        消费方式对齐；要在游戏里复现 waterpick 那条喷流，需要按实测反解填 tilt。
        """
        import math

        # 重心归一（LoadProfilesJSON:57 要求 sum ≈ 1 且全部非负）
        bx, by, bz = max(0.0, e.bx), max(0.0, e.by), max(0.0, e.bz)
        s = bx + by + bz
        if s <= 1e-9:
            bx, by, bz = 1.0 / 3.0, 1.0 / 3.0, 1.0 / 3.0
        else:
            bx, by, bz = bx / s, by / s, bz / s

        # 方向：emitter_direction 给的是「Blender 三角形 TBN 下的系数」。
        # 但游戏里那颗三角形的顶点顺序与手性可能和 Blender 不同：SSMT 的「非镜像工作流」
        # 导入时把 mesh 做了 X 镜像、导出时再翻回去，于是 Blender 里是镜像的、游戏里是原样的，
        # 而且顶点顺序从 (a,b,c) 变成 (a, M*c, M*b)。所以不直接吐系数，而是
        # 还原成世界方向 -> 镜像到游戏空间 -> 投影到游戏 TBN。
        from mathutils import Vector

        fwd, up = fluid_ini_merge.emitter_direction(e.tilt, e.yaw, e.roll)
        frame = self._game_frame(e)
        mirrored = False
        if frame is None:
            # 物体或面解析不了（刚删了物体 / 面号越界 / 退化三角形）：
            # 没有游戏 TBN 可投，退回原系数。位置照样给，只是方向对齐不了。
            fwd_c, up_c = tuple(fwd), tuple(up)
        else:
            t_b, bit_b, n_b, t_g, bit_g, n_g, mirrored = frame
            if mirrored:
                bx, by, bz = bx, bz, by      # 游戏侧第二、三顶点是 M*c 与 M*b
            fwd_w = t_b * fwd[0] + bit_b * fwd[1] + n_b * fwd[2]
            up_w = t_b * up[0] + bit_b * up[1] + n_b * up[2]
            if mirrored:
                fwd_w = Vector((-fwd_w.x, fwd_w.y, fwd_w.z))
                up_w = Vector((-up_w.x, up_w.y, up_w.z))
            fwd_c = (fwd_w.dot(t_g), fwd_w.dot(bit_g), fwd_w.dot(n_g))
            up_c = (up_w.dot(t_g), up_w.dot(bit_g), up_w.dot(n_g))

        # v[8] / v[12] 都走 fluid_ini_merge 的纯函数（判据镜像自 Slots.hlsl:33-36
        # 与 AssignSlots.hlsl:83），这样它们能被单测直接覆盖，不必拉起 bpy。
        trick_code = fluid_ini_merge.emitter_mode_code(
            e.trick, e.vis, e.trick_sp)
        pack = fluid_ini_merge.emitter_pack_code(
            e.trick, e.vis, e.tlen, e.bend_delay, e.bend_str)

        # 局部偏移的 U 与力场的 U 同一个根因：镜像后游戏侧 right=-M(Blender right)。
        # 来源: surface/ResolveSurface.hlsl:67
        #   p+=right*localOffset.x+up*localOffset.y+forward*localOffset.z;
        ox = -float(e.ox) if mirrored else float(e.ox)

        def clamp_off(v):
            return max(-MAX_LOCAL_OFFSET, min(MAX_LOCAL_OFFSET, float(v)))

        tint = (clamp_tint((e.tint_r, e.tint_g, e.tint_b))
                if e.custom_color else clamp_tint(default_tint))

        return [
            float(idx),                    # v[0]  id
            float(DEFAULT_PRIORITY),       # v[1]  priority
            bx, by, bz,                    # v[2..4] bary
            fwd_c[0], fwd_c[1], fwd_c[2],  # v[5..7] forward (游戏侧 TBN)
            trick_code,                    # v[8]
            up_c[0], up_c[1], up_c[2],     # v[9..11] up (游戏侧 TBN)
            pack,                          # v[12]
            clamp_off(ox), clamp_off(e.oy), clamp_off(e.oz),  # v[13..15]
            float(int(e.level)),           # v[16]
            1.0 if e.custom_color else 0.0,  # v[17]
            float(tint[0]), float(tint[1]), float(tint[2]),    # v[18..20]
            float(e.absorption),           # v[21]
            float(e.width),                # v[22]
        ]

    def _game_frame(self, e):
        """游戏侧三角形的两套 TBN + 镜像标记。

        返回 (t_b, bit_b, n_b, t_g, bit_g, n_g, mirrored)：
        * `*_b` 是 Blender 侧的基（用户在视口里看到的那个三角形）
        * `*_g` 是游戏侧的基——顶点顺序按 fluid_ini_merge.game_triangle_order 排好，
          镜像时坐标取反
        None 表示物体或面解析不了（刚删了物体 / 面号越界 / 退化三角形）。

        为什么需要两套：v[5..7] / v[9..11] 是相对三角形基的系数，
        消费端 surface/ResolveSurface.hlsl:61 拿游戏那颗三角形的基去还原。
        基不同，同一组系数就还原出不同的方向——这正是发射器在游戏里偏转的原因。
        """
        import bpy
        from mathutils import Vector

        obj = bpy.data.objects.get(e.object_name) if e.object_name else None
        if obj is None or obj.type != 'MESH':
            return None
        me = obj.data
        if e.poly_index < 0 or e.poly_index >= len(me.polygons):
            return None
        poly = me.polygons[e.poly_index]
        if len(poly.vertices) < 3:
            return None

        mw = obj.matrix_world
        world = [mw @ me.vertices[i].co for i in poly.vertices[:3]]
        mirrored = fluid_ini_merge.needs_x_mirror(obj.keys())
        order = fluid_ini_merge.game_triangle_order(mirrored)

        def basis(verts):
            a, b, c = verts
            t = b - a
            if t.length < 1e-12:
                return None
            t = t.normalized()
            n = (b - a).cross(c - a)
            if n.length < 1e-12:
                return None
            n = n.normalized()
            return t, n.cross(t), n

        base = basis(world)
        if base is None:
            return None

        if mirrored:
            game_verts = [Vector((-p.x, p.y, p.z)) for p in world]
        else:
            game_verts = list(world)
        game = basis([game_verts[order[0]], game_verts[order[1]], game_verts[order[2]]])
        if game is None:
            return None

        return base[0], base[1], base[2], game[0], game[1], game[2], mirrored

    def _emitter_basis(self, e):
        """把一个发射器解析成世界坐标的 ``(origin, right, up, forward, normal)``。

        预览模拟只需要位置与朝向，但力场的 U/V/W 与轴都写在 right/up/forward
        这组基上（``ForceField.hlsl:52``），所以这里把整组基都吐出来——上一版
        只返回 ``(origin, direction, normal)``，画力场时没法算偏移和轴。

        ``None`` 表示这个发射器现在解析不了（物体不在 / 面号越界 / 三角形退化），
        调用方应当跳过它而不是报错——用户可能刚删了物体。
        """
        import bpy

        obj = bpy.data.objects.get(e.object_name) if e.object_name else None
        if obj is None or obj.type != 'MESH':
            return None
        me = obj.data
        if e.poly_index < 0 or e.poly_index >= len(me.polygons):
            return None
        poly = me.polygons[e.poly_index]
        if len(poly.vertices) < 3:
            return None

        mw = obj.matrix_world
        a = mw @ me.vertices[poly.vertices[0]].co
        b = mw @ me.vertices[poly.vertices[1]].co
        c = mw @ me.vertices[poly.vertices[2]].co

        bx, by, bz = max(0.0, e.bx), max(0.0, e.by), max(0.0, e.bz)
        s = bx + by + bz
        if s <= 1e-9:
            bx = by = bz = 1.0 / 3.0
        else:
            bx, by, bz = bx / s, by / s, bz / s
        origin = a * bx + b * by + c * bz

        n = (b - a).cross(c - a)
        if n.length < 1e-12:
            return None
        n.normalize()

        tangent = b - a
        if tangent.length < 1e-12:
            return None
        tangent.normalize()
        bitangent = n.cross(tangent)

        fwd_c, up_c = fluid_ini_merge.emitter_direction(e.tilt, e.yaw, e.roll)
        direction = tangent * fwd_c[0] + bitangent * fwd_c[1] + n * fwd_c[2]
        if direction.length < 1e-12:
            return None
        direction.normalize()

        up_hint = tangent * up_c[0] + bitangent * up_c[1] + n * up_c[2]
        if up_hint.length < 1e-12:
            up_hint = n.copy()
        up_hint.normalize()
        right = up_hint.cross(direction)
        if right.length < 1e-12:
            right = tangent.copy()
        right.normalize()
        up = direction.cross(right)
        up.normalize()
        origin = origin + right * e.ox + up * e.oy + direction * e.oz

        return origin, right, up, direction, n

    def _emitter_world(self, e):
        """→ ``(origin, direction, normal)``；``None`` 表示现在解析不了。"""
        basis = self._emitter_basis(e)
        if basis is None:
            return None
        origin, _right, _up, direction, normal = basis
        return origin, direction, normal

    def _build_profiles(self):
        """生成 ``Profiles/<id>.json`` 的字典列表。

        返回 ``[(filename_hash, payload_dict), ...]``。

        一份 mod 只产生一个文件（本节点管的是"这一个角色自己的发射器"），
        文件名取 ``sha1(namespace)[:12]``——和参考实现的 12 位十六进制一致
        （``aff816a729ed`` / ``62c0dcc05f11`` / ``bd229809d3dc``）。
        这是纯标识符，不是内容哈希：``ProfileLoader.ini`` 里就是按文件名引用的
        （``surface\\ProfileLoader.ini:18`` ``filename = ../Profiles/aff816a729ed.json``）。
        """
        import hashlib

        default_tint = (self.tint_r, self.tint_g, self.tint_b)
        gpu = []
        emitters = []
        for i, e in enumerate(self.emitters):
            if i >= PROFILE_CAPACITY:
                break
            gpu.append(self._emitter_to_gpu(i, e, default_tint))
            emitters.append(
                {
                    "id": i,
                    "name": e.name or "emitter%d" % i,
                    "ib_file": e.ib_file,
                    "first_index": int(e.first_index),
                }
            )
        if not gpu:
            return []

        name = self._profile_file_id()
        payload = {
            "schema_version": 1,
            "enabled": True,
            "gpu_data": gpu,
            "source": "SSMT",
            "mod": (getattr(self, "id_data", None).name
                    if getattr(self, "id_data", None) is not None else ""),
            "emitters": emitters,
        }
        return [(name, payload)]

    def _profile_file_id(self):
        """本份 mod 的 profile 文件名（12 位十六进制）。

        用「节点树名 + 发射器数量」做稳定输入：同一棵树重复导出得到同一个名字，
        改名或增删发射器才换名。不用导出目录名是因为这个名字要被
        ``ProfileLoader.ini`` 的 ``filename = ../Profiles/<name>.json`` 引用，
        而那个 ini 是随 mod 一起搬走的——用目录名会在用户改文件夹名后失配。
        """
        import hashlib

        tree = getattr(self, "id_data", None)
        seed = "%s|%d" % (tree.name if tree is not None else "mod", len(self.emitters))
        return hashlib.sha1(seed.encode("utf-8")).hexdigest()[:12]

    def _build_index(self, file_id):
        """``Profiles/_index.json``：``owners[<file_id>] = [id...]``。

        依据 ``Profiles\\_index.json`` 的实测结构：
        ``{"schema_version": 1, "owners": {"62c0dcc05f11": [3,...,9]}}``。
        id 是**跨文件全局递增**的编号，而本份只有自己的发射器，所以从 0 开始。
        """
        if not file_id:
            return dict(EMPTY_INDEX)
        return {
            "schema_version": 1,
            "owners": {file_id: list(range(len(self.emitters)))},
        }

    # ------------------------------------------------------------------
    # 文本加工
    # ------------------------------------------------------------------
    def _rewrite(self, text, ns, prefix_shaders=True):
        """把参考实现的文本改成本 mod 的版本。"""
        # 1) namespace 根替换（含 \Composite / \JSONProfiles 两级子命名空间）
        text = text.replace("namespace = " + SRC_NAMESPACE, "namespace = " + ns)
        text = text.replace("namespace = %s\\" % SRC_NAMESPACE, "namespace = %s\\" % ns)
        # 2) 全限定引用：Resource\LiquidGravityProbe\X / CommandList\LiquidGravityProbe\X
        text = text.replace("Resource\\%s\\" % SRC_NAMESPACE, "Resource\\%s\\" % ns)
        text = text.replace("CommandList\\%s\\" % SRC_NAMESPACE, "CommandList\\%s\\" % ns)
        # 3) 未定义变量 → 门控变量（参考实现里 $costume_mods 从未被赋值）
        text = re.sub(r"\$costume_mods\b", "$fluid_present", text)

        # 5) [Present] 末尾清门控位。参考实现的 [Present] 只清 $seen / $drawn
        #    （Probe.ini:287-288），$fluid_present 需要同样的每帧归零，
        #    否则角色一旦出场就永远为 1，门控失效。
        if "post $fluid_present = 0" not in text:
            text = text.replace(
                "post $drawn = 0",
                "post $drawn = 0\npost $fluid_present = 0",
            )
        # 4) IniParams 号段重映射（xNNN / yNNN / zNNN / wNNN）
        #
        #    必须匹配**所有**引用，不能只匹配赋值语句 `xNNN =`：
        #    `Probe.ini:521/523` 的 `drawindexedinstanced = 3,1,y243,z243` 是
        #    把号当**读取点**用（选哪个三角形实例），后面跟的是逗号不是等号。
        #    只改赋值侧会得到「ini 里还是 y243、hlsl 里已经是 [35]」的错配，
        #    表现正是参数完全无效而不报错。
        #
        #    且只认表内的号：`x1`/`x12` 这类别处也在用的短号一律不动。
        if self.remap_ini_params:
            keys = sorted(FLUID_INI_PARAM_MAP, key=lambda k: len(str(k)), reverse=True)
            pat = re.compile(r"\b([xyzw])(%s)\b" % "|".join(str(k) for k in keys))
            text = pat.sub(
                lambda m: "%s%s" % (m.group(1), FLUID_INI_PARAM_MAP[int(m.group(2))]),
                text,
            )
        # 6) 着色器路径补部署前缀。
        #
        #    3Dmigoto 的 `cs = <path>` 相对「**该 ini 文件所在目录**」解析：
        #    `IniHandler.cpp:315-331` 的 `_get_section_path` 返回 entry->ini_path，
        #    `_get_namespaced_section_path:355-361` 再把末尾的文件名削掉留目录，
        #    `CommandList.cpp:2506-2517` 用它去 GetFileAttributes。
        #
        #    本节点的 ini 放在 mod 根、着色器部署在 <mod>/res/fluid_probe/ 下，
        #    所以必须补前缀，否则 GetFileAttributes 落空、compile 静默失败
        #    （只有一声 IniWarningBeep，整段命令列表随后被 optimize 掉）。
        _pat_shader = re.compile(
            r"^(\s*)(%s)(\s*=\s*)([^\s#;]+\.hlsl)" % "|".join(("cs", "vs", "ps", "hs", "ds", "gs")),
            re.M,
        )
        if prefix_shaders:
            prefix = DEPLOY_SUBDIR.replace("\\", "/") + "/"
            text = _pat_shader.sub(
                lambda m: "%s%s%s%s%s"
                % (m.group(1), m.group(2), m.group(3), prefix, m.group(4).lstrip("./")),
                text,
            )

        # 7) 全局参数覆盖（`[Constants]` 里 `global $xxx = 值`）。
        #
        #    只改值不动名：这些变量被 FluidComposite.hlsl / FluidSolver.hlsl
        #    按名直接读，改名等于让整条链静默失效（3Dmigoto 未定义变量求值为 0，
        #    不报错、不告警，只是参数完全没作用）。
        for var, val in self._global_overrides().items():
            text = re.sub(
                r"^([ \t]*global\s+\$" + re.escape(var) + r"\s*=\s*)([^\r\n]*)",
                lambda m, v=val: m.group(1) + v,
                text,
                flags=re.M,
            )

        return text

    def _global_overrides(self):
        """节点上拍板的全局参数 → `[Constants]` 里 `global $xxx = 值` 的覆盖表。

        变量名与取值域全部对照 `Probe.ini:2-24`（`[Constants]` 段）的实测定义：

        ===========================  ==========================================
        `$strength_level`            `:10`   0-7 档，`Pulse.hlsl` 用它换算发射量
        `$depth_test`                `:9`    1=被遮挡就裁掉
        `$auto_pulse`                `:12`   1=不等按键、一直喷
        `$liquid_tint_r/g/b`         `:13-15` 液体本色
        `$liquid_absorb_mm`          `:16`   吸收深度（毫米，`FluidComposite` 算透光用）
        `$liquid_wet_darken`         `:17`   打湿表面变暗系数
        `$liquid_puddle_reflect`     `:18`   水洼反射
        `$liquid_stream_reflect`     `:19`   水流反射
        `$liquid_scatter`            `:20`   散射
        `$liquid_refraction`         `:21`   折射
        ===========================  ==========================================

        不覆盖的：`$seen` / `$drawn` / `$initialized`（运行时状态）、
        `$slot_count`（由发射器数量自动写）。`$yellow_step` / `$clarity_step`
        改后不存在：小键盘 5-8 那四组快捷键（whiter/yellower/opaque/clear）
        与其 `CommandListApplyTint` / `CommandListApplyClarity` 已从
        `Probe.ini` 删除，调色与吸收深度只由节点的导出参数写。
        `$emitting` 也不再覆盖：它现在是角色门控的**结果**（`[Present]` 每帧写），
        不再有面板开关，写了只会和门控打架。
        """

        def f(v, n=4):
            return ("%." + str(n) + "f") % float(v)

        return {
            "strength_level": str(int(max(0, min(7, self.strength_level)))),
            "depth_test": "1" if self.depth_test else "0",
            "auto_pulse": "1" if self.auto_pulse else "0",
            "liquid_tint_r": f(self.tint_r),
            "liquid_tint_g": f(self.tint_g),
            "liquid_tint_b": f(self.tint_b),
            "liquid_absorb_mm": f(self.absorb_m * 1000.0, 2),
            "liquid_wet_darken": f(self.wet_darken),
            "liquid_puddle_reflect": f(self.puddle_reflect),
            "liquid_stream_reflect": f(self.stream_reflect),
            "liquid_scatter": f(self.scatter),
            "liquid_refraction": f(self.refraction),
        }

    @staticmethod
    def _remap_hlsl(text):
        """hlsl 里的 ``IniParams[NNN]`` 重映射（与 ini 同一张表）。"""
        return re.sub(
            r"IniParams\[(\d+)\]",
            lambda m: "IniParams[{}]".format(
                FLUID_INI_PARAM_MAP.get(int(m.group(1)), int(m.group(1)))
            ),
            text,
        )

    # ------------------------------------------------------------------
    # 主 ini
    # ------------------------------------------------------------------
    def _build_main_ini(self, ns, host_hashes):
        raw = _read_text(os.path.join(_toolset_fluid_dir(), TEMPLATE_INI))
        text = self._rewrite(raw, ns)

        # 参考实现里 $fluid_present 只被读、从不被写。
        # 补上它唯一的全局声明点，否则未定义变量求值为 0，整条链不执行。
        decl = "global $fluid_present = 0\n"
        if decl not in text:
            idx = text.find("[Constants]")
            if idx >= 0:
                insert_at = text.find("\n", idx) + 1
                text = text[:insert_at] + decl + text[insert_at:]
            else:
                text = "[Constants]\n" + decl + text

        # $slot_count = 「本轮要跟踪几个槽」，而**一个发射器占一个槽**：
        # ResolveSurface.hlsl:41-48 按「root + 发射器 id」产生候选，AssignSlots.hlsl:29
        # 拿这个数当槽位上限（:42/:61 用它筛槽）。所以它必须跟着发射器数走——
        # 早先写死 1（当时的理解是「同时跟踪几个角色」），结果多发射器 mod 里
        # 只有 slot 0 能填，第 2 个及以后静默不喷。下限 1、上限 8 见纯函数；
        # 超过 8 个的那部分**拿不到槽位**（不只是少个热键），必须告警。
        slot_warnings = []
        text = fluid_ini_merge.apply_slot_count(
            text, len(self.emitters), warnings=slot_warnings)
        for message in slot_warnings:
            print("[FluidProbe][WARN] " + message)

        # gate 段：宿主每个 hash 一段，只写变量，不绑定任何资源。
        if self.gate_by_host_hash and host_hashes:
            blocks = []
            for h in host_hashes:
                blocks.append(
                    "[%s%s]\nhash = %s\n$fluid_present = 1\n" % (GATE_PREFIX, h, h)
                )
            text = (
                text.rstrip("\n")
                + "\n\n; ---- 角色门控：宿主 mesh 被画时置 $fluid_present = 1 ----\n"
                + "\n".join(blocks)
            )

        slots, warnings = self._force_fields_by_slot()
        for message in warnings:
            print("[FluidProbe][WARN] " + message)
        floats = fluid_ini_merge.build_force_field_floats(slots)
        text = fluid_ini_merge.apply_force_fields(
            text, floats, getattr(self, "force_scale", 1.0)
        )
        if slots:
            print("[FluidProbe] 力场: {} 个发射器 / {} 个字段".format(
                len(slots),
                sum(len(v) for v in slots.values()),
            ))
        # 发射器键位：**第 1 个发射器留空时默认小键盘 1**（原来那个总开关键位，
        # 现在总开关改成角色门控硬关了，这个键空着不如给第一个发射器）；第 2 个起
        # 留空 = 不绑，免得几个发射器抢同一个键。策略放在这里而不是
        # ``apply_emitter_keys`` 里：那个函数的契约是「空串 = 不绑」，测试直接调它。
        emitter_key_entries = _emitter_key_entries(self.emitters)
        # 全局快捷键改名。留空的一律保持模板默认（小键盘），一个都不改时
        # 输出与模板逐字节一致，所以注入幂等测试不受影响。
        text, key_warnings = fluid_ini_merge.apply_global_keys(
            text,
            {
                field: getattr(self, "key_" + field, "")
                for _section, field in fluid_ini_merge.GLOBAL_KEY_SECTIONS
            },
            emitter_keys=[spec for _slot, spec in emitter_key_entries],
        )
        # 每个发射器自己的开关键。放在前缀改名之前，和力场同一个理由：
        # 这里按模板原名 $emit_N 工作，改完再统一改名。
        # 槽号超限的发射器现在**跳过并告警**（以前在这里抛 ValueError，
        # 抛穿 _deploy ⇒ 整次导出一个文件都不写），所以 warnings 必须往外打。
        text = fluid_ini_merge.apply_emitter_keys(
            text,
            emitter_key_entries,
            warnings=key_warnings,
        )
        for message in key_warnings:
            print("[FluidProbe][WARN] " + message)
        # 每个发射器的运行期变量：开关初值 + 速度 + 力场开关/强度。
        # 一个值都没改时不写任何行（产物与改动前逐字节相同），改了就写全 0..N-1。
        text, control_warnings = fluid_ini_merge.apply_emitter_controls(
            text,
            [float(getattr(em, "speed", 0.0) or 0.0) for em in self.emitters],
            enables=[1 if bool(getattr(em, "enable", True)) else 0 for em in self.emitters],
            field_switches=[
                [1 if bool(getattr(f, "enabled", True)) else 0
                 for f in (getattr(em, "force_fields", ()) or ())]
                for em in self.emitters
            ],
        )
        for message in control_warnings:
            print("[FluidProbe][WARN] " + message)
        # 所有 $var 加 fluid_ 前缀放在**最后**：前面的步骤（$slot_count 替换、
        # apply_force_fields 的开关拨动）都按模板原名工作，改完再统一改名，
        # 这样两边引用一定同步，不会出现「赋值改了、读取没改」的错配。
        text = fluid_ini_merge.apply_particle_radius(text, self.particle_radius)
        text = fluid_ini_merge.apply_gravity_scale(text, self.gravity_scale)
        text = fluid_ini_merge.apply_viscosity(text, self.viscosity)
        speed = max(0.0, min(6.0, float(self.launch_speed)))
        text = re.sub(
            r"global\s+\$fluid_launch_speed\s*=\s*[^\s;]+",
            "global $fluid_launch_speed = " + fluid_ini_merge._fmt_float(speed),
            text,
        )
        # 档位是喷嘴形状的输入（开口/片状/扇角/重力近场），由目标速度反解出来，
        # 这样「速度」只有一个数是他填的，其余都由它决定。
        text = re.sub(
            r"global\s+\$strength_level\s*=\s*[^\s;]+",
            "global $strength_level = " + fluid_ini_merge._fmt_float(
                fluid_ini_merge.level_for_speed(speed)),
            text,
        )
        return fluid_ini_merge.prefix_variables(text)

    def _force_fields_by_slot(self):
        """按发射器序号收集力场，返回 ``({槽号: [字段字典, ...]}, 告警列表)``。

        槽号 = 发射器序号。这不是我定的，是 ``_inject_emitter_hooks`` 里那句
        ``x243 = idx`` 定的：``AssignSlots.hlsl`` 拿 ``x243`` 当「这次 draw 属于
        哪个槽」的输入，所以力场表的主索引必须和它同源。两处一旦错位，力场就会
        作用到别的角色的水上，而且是那种「有不喷的角色、有不该喷的在喷」的错法。
        """
        slots = {}
        warnings = []
        for idx, em in enumerate(self.emitters):
            fields = list(getattr(em, "force_fields", ()) or ())
            if not fields:
                continue
            # U 轴在手性镜像下会整体翻号（见 fluid_ini_merge.build_force_field_floats
            # 的注释）。mirrored 与顶点顺序、发射方向用的是同一个判据。
            #
            # _frame 为 None（物体缺失 / 面号越界 / 退化三角形 / 手填 first_index）
            # 时**按非镜像处理**：判据一个都拿不到就不动 U——非镜像模型占多数，
            # 翻错会让水柱朝反方向，比不翻更难查。镜像模型走到这条分支时，先把
            # 面号 / first_index 修到能解析出三角形再导出。
            _frame = self._game_frame(em)
            mirror_u = bool(_frame is not None and _frame[6])
            if idx >= fluid_ini_merge.FORCE_FIELD_SLOT_COUNT:
                warnings.append(
                    "发射器 {}：槽号超出力场容量 {}（SLOT_COUNT），其力场被忽略"
                    .format(idx, fluid_ini_merge.FORCE_FIELD_SLOT_COUNT)
                )
                continue
            if len(fields) > fluid_ini_merge.FORCE_FIELDS_PER_SLOT:
                warnings.append(
                    "发射器 {}：最多写前 {} 个力场，多出的被忽略".format(
                        idx, fluid_ini_merge.FORCE_FIELDS_PER_SLOT)
                )
                fields = fields[:fluid_ini_merge.FORCE_FIELDS_PER_SLOT]
            slots[idx] = [
                {
                    "enabled": bool(f.enabled),
                    "kind": f.kind,
                    "mirror_u": mirror_u,
                    "offset_u": float(f.offset_u),
                    "offset_v": float(f.offset_v),
                    "offset_w": float(f.offset_w),
                    "axis_u": float(f.axis_u),
                    "axis_v": float(f.axis_v),
                    "axis_w": float(f.axis_w),
                    "radius": float(f.radius),
                    "strength": float(f.strength),
                    "falloff": float(f.falloff),
                }
                for f in fields
            ]
        return slots, warnings

    def _build_profile_loader(self, ns, file_id=None):
        r"""生成 ``surface/ProfileLoader.ini``。

        有发射器时按参考实现的结构生成一组 ``[ResourceJSON0]`` +
        ``[CustomShaderRead0]``；没有发射器时只留清空壳（发射器走默认锚点分支）。

        资源名必须写**全限定名** ``Resource\<ns>\SurfaceProfiles``。
        依据（这一段我改错过一次，结论以源码为准）：

        1. 解析期，每份 ini 的段名会被原地加上 namespace 前缀——
           ``IniHandler.cpp:398-405`` 的 ``get_namespaced_section_name`` 把
           ``[ResourceSurfaceProfiles]`` 改写成键
           ``Resource\LiquidGravityProbe\SurfaceProfiles`` 再 ``emplace``（`:422`）。
        2. 查找期 ``CommandList.cpp:8209-8215`` 先按当前命名空间拼一次，
           拼不到再回退**裸的 resource_id**。在 ``<ns>\JSONProfiles`` 里拼出来是
           ``Resource\<ns>\JSONProfiles\<ns>\SurfaceProfiles``（多一层，必落空），
           于是回退到 ``Resource\<ns>\SurfaceProfiles`` —— 正是注册时的键，命中。
        3. 只写裸名 ``ResourceSurfaceProfiles`` 时拼出
           ``Resource\<ns>\JSONProfiles\SurfaceProfiles``（落空），
           回退是 ``resourcesurfaceprofiles`` —— 注册表里没有这个键，也落空。

        参考实现的 ``surface/ProfileLoader.ini:7-8`` 写的正是全限定名。

        ``cs = LoadProfilesJSON.hlsl`` 用**裸文件名**：3Dmigoto 的 ``cs =``
        相对该 ini 所在目录解析（``IniHandler.cpp:315-331`` 返回 ini 路径，
        ``:355-361`` 削掉文件名留目录），本 ini 与 ``LoadProfilesJSON.hlsl``
        同在该部署子目录下，所以不能加任何前缀。
        """
        head = (
            "; 由 SSMT 流体探针节点生成。发射器 profile 在 ../Profiles/ 下。\n"
            "namespace = %s\\JSONProfiles\n"
            "[Constants]\n"
            "global $loaded = 0\n"
            "[CommandListInitialize]\n"
            "if !$loaded\n"
            "    clear = Resource\\%s\\SurfaceProfiles\n"
            "    clear = Resource\\%s\\SurfaceModelOffsets\n" % (ns, ns, ns)
        )
        if not file_id:
            # $loaded 也在共用变量表里（3Dmigoto 的 $var 不随 namespace 隔离），
            # 所以同样走 prefix_variables，避免与别的 mod 的 $loaded 互踩。
            return fluid_ini_merge.prefix_variables(head + "    $loaded = 1\nendif\n")

        body = head + "    run = CustomShaderRead0\n    $loaded = 1\nendif\n"
        body += (
            "\n[ResourceJSON0]\n"
            "type = Buffer\n"
            "format = R8_UINT\n"
            "filename = ../Profiles/%s.json\n" % file_id
        )
        body += (
            "\n[CustomShaderRead0]\n"
            "cs = LoadProfilesJSON.hlsl\n"
            "vs = null\nps = null\ngs = null\nhs = null\nds = null\n"
            "cs-t0 = ResourceJSON0\n"
            "cs-u0 = Resource\\%s\\SurfaceProfiles\n"
            "cs-u1 = Resource\\%s\\SurfaceModelOffsets\n"
            "dispatch = 1,1,1\n"
            "cs-t0 = null\ncs-u0 = null\ncs-u1 = null\n" % (ns, ns)
        )
        return fluid_ini_merge.prefix_variables(body)


    # ------------------------------------------------------------------
    # 部署
    # ------------------------------------------------------------------
    @staticmethod
    def _drop_stale_bins(dst_dir, base_name):
        """删掉与 hlsl 同名的编译缓存。

        改动 hlsl 内容后缓存不再对应（``CommandList.cpp:2409-2413`` 只校验
        mtime，不校验内容），留着会被错误加载，必须删掉强制重编译。
        缓存文件名形如 ``<base>.cs_5_0.<flags>.bin``。
        """
        if not os.path.isdir(dst_dir):
            return 0
        dropped = 0
        for name in os.listdir(dst_dir):
            if not name.lower().endswith(".bin"):
                continue
            if not name.startswith(base_name + "."):
                continue
            try:
                os.remove(os.path.join(dst_dir, name))
                dropped += 1
            except OSError:
                pass
        return dropped

    def _deploy(self, mod_export_path, ns, host_hashes):
        src_root = _toolset_fluid_dir()
        dst_root = os.path.join(mod_export_path, *DEPLOY_SUBDIR.split("/"))
        written = total = dropped_bins = 0

        # 0) 发射器 profile：本次导出的数据源。
        #    file_id 同时决定 ProfileLoader.ini 里 `filename = ../Profiles/<id>.json`
        #    的文件名，所以必须先算出来再喂给第 3 步。
        profiles = self._build_profiles()
        file_id = profiles[0][0] if profiles else None

        # 1) 主 ini 放 mod 根（与宿主 ini 同级）
        total += 1
        if write_text_if_changed(
            os.path.join(mod_export_path, OUTPUT_INI),
            fluid_ini_merge.collapse_constants(self._build_main_ini(ns, host_hashes)),
        ):
            written += 1

        # 2) 子目录整体搬运
        for sub in SUBDIRS:
            src_dir = os.path.join(src_root, sub)
            if not os.path.isdir(src_dir):
                continue
            dst_dir = os.path.join(dst_root, sub)
            for name in sorted(os.listdir(src_dir)):
                src_file = os.path.join(src_dir, name)
                if not os.path.isfile(src_file):
                    continue
                low = name.lower()

                # 编辑器遗留文件，不属于运行期资产
                if low.endswith(".nhd-pick-orig"):
                    continue

                # 编译缓存：仅在「hlsl 不会被改动」时才带
                if low.endswith(".bin"):
                    if not (self.include_shaders_cache and not self.remap_ini_params):
                        continue
                    total += 1
                    if copy_file_if_changed(src_file, os.path.join(dst_dir, name)):
                        written += 1
                    continue

                # 两份子 ini 由下方统一生成
                if name in GENERATED_SUB_INIS:
                    continue

                dst_file = os.path.join(dst_dir, name)
                content = _read_text(src_file)
                if self.remap_ini_params and low.endswith(".hlsl"):
                    new_text = self._remap_hlsl(content)
                    changed = new_text != content
                    total += 1
                    if write_text_if_changed(dst_file, new_text):
                        written += 1
                    if changed:
                        dropped_bins += self._drop_stale_bins(
                            dst_dir, name.rsplit(".", 1)[0]
                        )
                else:
                    total += 1
                    if copy_file_if_changed(src_file, dst_file):
                        written += 1

        # 3) 两份子 ini
        for rel in ("fluid/Composite.ini", "surface/ProfileLoader.ini"):
            dst_file = os.path.join(dst_root, *rel.split("/"))
            total += 1
            if rel.endswith("ProfileLoader.ini"):
                content = self._build_profile_loader(ns, file_id)
            else:
                # 子 ini 与它自己的着色器同目录（res/fluid_probe/fluid/ 或 .../surface/），
                # 所以不能再补 res/fluid_probe/ 前缀，否则会多出一层。
                content = self._rewrite(
                    _read_text(os.path.join(_toolset_fluid_dir(), *rel.split("/"))),
                    ns,
                    prefix_shaders=False,
                )
            if write_text_if_changed(dst_file, content):
                written += 1

        # 4) Profiles：每份发射器一个 <id>.json，外加索引
        #
        #    为什么用 indent=2 + ensure_ascii=False：LoadProfilesJSON.hlsl 的解析器
        #    只认 ASCII 数字与 JSON 标点（:18-32 的 number()、:16 的 take()），
        #    但它对空白没有假设（:14 white() 认空格/TAB/LF/CR），所以缩进无害。
        #    中文名写进 "name" 字段时若被转成 \uXXXX，那份 JSON 会含非数字字节——
        #    虽然解析器先找 "enabled" 再找 "gpu_data" 后停（:75-82），但为了肉眼
        #    可读与 diff 稳定，这里保留 UTF-8 原字符。
        for fname, payload in profiles:
            total += 1
            if write_text_if_changed(
                os.path.join(dst_root, "Profiles", fname + ".json"),
                json.dumps(payload, indent=2, ensure_ascii=False) + "\n",
            ):
                written += 1

        total += 1
        if write_text_if_changed(
            os.path.join(dst_root, "Profiles", "_index.json"),
            json.dumps(self._build_index(file_id), indent=2, ensure_ascii=False) + "\n",
        ):
            written += 1

        # 4.5) 旧版 .buf：默认不生成。新版消费端（LoadProfilesJSON.hlsl）只读 json，
        #      这两个文件在 Probe.ini 里零引用。开关的理由见 _build_legacy_buffers。
        if self.emit_legacy_buffers and profiles:
            prof_bytes, offs_bytes = self._build_legacy_buffers(profiles)
            total += 2
            if write_bytes_if_changed(
                os.path.join(dst_root, "Profiles", "profiles.buf"), prof_bytes
            ):
                written += 1
            if write_bytes_if_changed(
                os.path.join(dst_root, "surface", "attachment-offsets.buf"), offs_bytes
            ):
                written += 1

        # 5) 宿主 ini 里指向旧命名空间的 CaptureSurface 引用改为本份 ns
        #    （starbilly.ini 8 处 run = CommandList\LiquidGravityProbe\CaptureSurface）
        retargeted = self._retarget_host_refs(mod_export_path, ns)

        return written, total, dropped_bins, retargeted

    def _build_legacy_buffers(self, profiles):
        """旧版 ``-zh-CN`` 的 ``profiles.buf`` / ``attachment-offsets.buf``。

        **默认不生成**——新版 ``LiquidGravityProbe`` 改由
        ``surface\\LoadProfilesJSON.hlsl`` 直读 ``Profiles/<hash>.json``，
        这两个文件没有任何消费端（``Probe.ini`` 里零引用）。
        留着这个开关只为两种情况：还在用旧版前缀、或第三方工具链着它们。

        格式来源（旧版目录实测字节数）::

            profiles.buf            1056 B = 22 条 × 48 B
            attachment-offsets.buf   352 B = 22 条 × 16 B

        单条布局（旧版编辑器 ``apply_anchor`` 的 ``struct.pack`` 调用）::

            profiles.buf            <12f
                [0]     priority
                [1..3]  bary 重心坐标
                [4..6]  forward（TBN 系数）
                [7]     0（占位）
                [8..10] up（TBN 系数）
                [11]    0（占位）
            attachment-offsets.buf  <4f
                [0..2]  offset（米，沿 right/up/forward）
                [3]     0

        从新版的 23 浮点派生，不重新推导::

            v[1]      → [0]
            v[2..4]   → [1..3]
            v[5..7]   → [4..6]
            v[9..11]  → [8..10]
            v[13..15] → offsets[0..2]

        正好对应 ``LoadProfilesJSON.hlsl:59-64`` 里 ``profiles[row]`` /
        ``profiles[row+1]`` / ``profiles[row+2]`` / ``offsets[id]`` 的前三个分量。

        打包本身在 ``fluid_ini_merge.legacy_buffers``（纯函数，可单测）。
        """
        rows = []
        for _fname, payload in profiles:
            rows.extend(payload.get("gpu_data") or [])
        return fluid_ini_merge.legacy_buffers(rows)

    def _retarget_host_refs(self, mod_export_path, ns):
        """把宿主 ini 里指向「别的前置命名空间」的 CaptureSurface 引用改到本份的 ns。

        数据来源：`starbilly.ini` 有 8 处
        ``run = CommandList\\LiquidGravityProbe\\CaptureSurface``
        （行 200/208/216/274/285/291/299/305）——宿主主动调用流体的
        ``CaptureSurface`` 来声明「这个 mesh 的某个三角形当发射器」。

        若不改，这 8 处指向一个本份不再提供的命名空间，流体拿不到发射器面，
        表现是「液滴从默认的头顶 (0, 0.85, 0) 冒出来」。

        匹配的是**任意** ``CommandList\\<某个ns>\\CaptureSurface``，不只是
        出厂时的 ``LiquidGravityProbe``。这一点是自检发现的 bug：早先只认
        常量 ``SRC_NAMESPACE``，于是「把已导出过的目录复制成新目录再导出」
        时，旧目录名残留在引用里（实测 ``Fluid_tmp_host_mod`` 没被改成
        ``Fluid_tmp_selfcheck``），游戏里那个 run 指向不存在的段，直接报
        ``an error has occurred`` 并跳过——发射器静默失效。
        """
        changed = []
        # 只认 CaptureSurface 这一种，避免误改宿主自己的跨命名空间调用。
        pat = re.compile(r"CommandList\\([A-Za-z0-9_\-]+)\\CaptureSurface\b")
        for name in sorted(os.listdir(mod_export_path)):
            low = name.lower()
            if not low.endswith(".ini") or name == OUTPUT_INI:
                continue
            path = os.path.join(mod_export_path, name)
            try:
                text = _read_text(path)
            except Exception:
                continue
            if "CaptureSurface" not in text:
                continue

            def _repl(m):
                return "CommandList\\%s\\CaptureSurface" % (ns if m.group(1) != ns else m.group(1))

            new_text = pat.sub(_repl, text)
            if new_text != text and write_text_if_changed(path, new_text):
                changed.append(name)
        return changed

    def _host_hashes(self, mod_export_path):
        """读宿主 ini，收集 hash。只读，不改宿主 ini。"""
        if not self.gate_by_host_hash:
            return []
        hashes = []
        # 面板里填了宿主 ini 时优先用它：导出目录里可能还没生成任何 ini
        # （刚建好 mod 就要报一次数），也可能一次导出多个 ini 让人分不清是哪个。
        override = (getattr(self, "host_ini_path", "") or "").strip()
        if override and os.path.isfile(override):
            try:
                for h in _collect_hashes(_read_text(override)):
                    if h not in hashes:
                        hashes.append(h)
            except Exception:
                pass
            if hashes:
                return hashes
        for name in sorted(os.listdir(mod_export_path)):
            low = name.lower()
            if not low.endswith(".ini") or name == OUTPUT_INI:
                continue
            try:
                text = _read_text(os.path.join(mod_export_path, name))
            except Exception:
                continue
            for h in _collect_hashes(text):
                if h not in hashes:
                    hashes.append(h)
        return hashes

    # ------------------------------------------------------------------
    # 发射器注入链
    # ------------------------------------------------------------------
    def _inject_emitter_hooks(self, mod_export_path, ns):
        """把每个发射器的 ``x243/y243/z243 + run`` 插到宿主 ini 的对应 draw 之后。

        这条链的形态全部来自参考实现的**实测产物**，不是从文档推的：

        1. **块的样子** —— ``starbilly.ini:196-201`` / ``:204-209`` / ``:212-217``：
           ```ini
               ; nhd-pick 14 begin
               x243 = 14
               y243 = 883239
               z243 = 0
               run = CommandList\\LiquidGravityProbe\\CaptureSurface
               ; nhd-pick 14 end
           ```
           用 ``; nhd-pick <id> begin/end`` 包起来，是为了**下次能原样找回来替换**，
           而不是靠行号——参考实现里同一个文件被 waterpick 反复重写过。

        2. **插在哪** —— ``drawindexed = <count>, <start>, <base>`` 这一行之后，
           且 ``first_index`` 必须落在那次 draw 的索引区间 ``[start, start+count)`` 里。
           实测 ``starbilly.ini:202`` 的 ``169464, 723363, 0`` 覆盖
           ``723363..892827``，而 ``y243 = 883239`` 正在里面。

        3. **为什么不是插在 `[TextureOverride]` 段头** —— 参考实现的注入点是 draw 之间。
           ``CaptureTriangle.hlsl`` 自己发 ``drawindexedinstanced = 3,1,y243,z243``，
           所以只要在 ``CommandListCaptureSurface`` 跑之前把三个号设好就行；
           插在大段的哪个 draw 后面只影响「谁触发它」，不影响取到哪个三角形。

        4. **号段** —— ``remap_ini_params`` 开着时 ``243`` 已搬到 ``35``，
           否则会和形态动画 mod 抢 237-245（``Mod_柳 形态anim.hlsl:74`` 硬写
           ``IniParams[238].x``，与 ``Probe.ini:419`` 直接对撞）。
        """
        # 没有发射器也要往下走：必须把上一轮插进宿主 ini 的 ``; nhd-pick``
        # 块清掉。早先这里 ``return 0`` 直接早退，于是「删光发射器再导出」
        # 之后宿主 ini 里还留着 x243/y243 + run = CaptureSurface，游戏继续
        # 喷「幽灵水」，用户从 UI 上关不掉。
        mapped = FLUID_INI_PARAM_MAP.get(243, 243) if self.remap_ini_params else 243
        xk, yk, zk = "x%d" % mapped, "y%d" % mapped, "z%d" % mapped

        # 先清掉本节点此前插过的块，保证反复导出不叠加。
        clear_re = re.compile(
            r"[ \t]*;\s*nhd-pick\s+\d+\s+begin[^\n]*\n"
            r".*?"
            r"[ \t]*;\s*nhd-pick\s+\d+\s+end[^\n]*\n",
            re.S,
        )
        draw_re = re.compile(
            r"^([ \t]*)drawindexed\s*=\s*(\d+)\s*,\s*(\d+)\s*,\s*(\d+)[ \t]*$", re.M
        )

        touched = 0
        for name in sorted(os.listdir(mod_export_path)):
            if not name.lower().endswith(".ini") or name == OUTPUT_INI:
                continue
            path = os.path.join(mod_export_path, name)
            try:
                text = _read_text(path)
            except Exception:
                continue

            original = text
            text = clear_re.sub("", text)
            cleared = text != original
            # 清完块之后这份 ini 可能已经没有 draw 行（例如块是它唯一的流体内容）。
            # 只要本轮**删掉过**块，就必须写回；否则条件退化成「有东西才写」，
            # 又变回「旧的块永远删不掉」。
            if not draw_re.search(text) and not cleared:
                continue

            inserts = []
            # 先按「; [mesh:名]」把每个 draw 归属到网格，才能用 (物体, 面序号) 反算首索引。
            # 注释放的位置见 node_postprocess_draginteraction.py:2361——它是唯一写这行
            # 注释的地方，格式固定为 ``; [mesh:<object_name>]``。
            mesh_draws = scan_mesh_draws(text)
            has_mesh_comment = any(d[0] for d in mesh_draws)
            for idx, em in enumerate(self.emitters):
                fi = resolve_first_index(int(getattr(em, 'poly_index', -1)), getattr(em, 'object_name', ''), int(getattr(em, 'first_index', -1)), getattr(em, 'ib_file', ''), mesh_draws)
                if fi is None:
                    if int(getattr(em, "poly_index", -1)) >= 0:
                        # 填了面序号却反算不出来：要么这个 ini 没有 ; [mesh:...] 注释
                        # （外部下载的 mod 常没有，那是 SSMT 导出期才写的），
                        # 要么物体名对不上、面序号超出该 draw 的范围。
                        print(
                            "[FluidProbe][WARN] 发射器 {}：面序号 {} 无法反算首索引"
                            "（mesh 注释 {}，物体名 {!r}）——跳过，不写错号".format(
                                idx, int(em.poly_index),
                                "有" if has_mesh_comment else "缺失",
                                getattr(em, "object_name", ""),
                            )
                        )
                    continue
                if fi < 0:
                    continue
                # 找插入行：必须**同时**满足「这一行前的 mesh 注释就是目标物体」和
                # 「fi 落在这行的索引区间里」。
                #
                # 为什么不能只按区间：多部件角色每个部件有自己的 IB，`drawindexed`
                # 的 start 常常都是 0（实测 `菲欧妮.ini` 里两个部件的 draw 分别是
                # `0,72336` 和 `0,38448`），于是同一个 fi 会同时命中好几行，
                # 取第一个就会插到别的部件上——那一帧水从另一块网格喷出来。
                hit = locate_draw_line(
                    text, getattr(em, "object_name", ""), fi
                )
                if not hit:
                    print(
                        "[FluidProbe][WARN] 发射器 {}：首索引 {} 找不到所属 draw 行"
                        "（物体名 {!r}）——跳过".format(
                            idx, fi, getattr(em, "object_name", "")))
                    continue
                pos, indent = hit
                bv = int(getattr(em, "base_vertex", 0))
                block = (
                    "\n{i}; nhd-pick {idx} begin\n"
                    "{i}{xk} = {idx}\n"
                    "{i}{yk} = {fi}\n"
                    "{i}{zk} = {bv}\n"
                    "{i}run = CommandList\\{ns}\\MarkPresent\n"
                    "{i}run = CommandList\\{ns}\\CaptureSurface\n"
                    "{i}; nhd-pick {idx} end"
                ).format(i=indent, xk=xk, yk=yk, zk=zk, idx=idx, fi=fi, bv=bv, ns=ns)
                # 插在 draw 行**之前**：3Dmigoto 的 ini 优化器会把排在最后一次
                # draw 之后的、只改全局变量的命令判成无用代码直接删掉——实测
                # 日志里是 "Optimised out post [..._LOD0.9040e2c4_16194_56844]
                # run = commandlist\fluid_mod\markpresent"，于是 $fluid_present
                # 永远停在 0，整条链一帧都不跑。
                line_start = text.rfind("\n", 0, max(pos - 1, 0)) + 1
                inserts.append((line_start, block + "\n"))

            if not inserts and not cleared:
                continue
            # 从后往前插，避免前面的插入把后面的偏移推走。
            for pos, block in sorted(inserts, key=lambda t: t[0], reverse=True):
                text = text[:pos] + block + text[pos:]
            if write_text_if_changed(path, text, encoding="utf-8", newline="\n"):
                touched += 1
        return touched

    # ------------------------------------------------------------------
    # 入口
    # ------------------------------------------------------------------
    def execute_postprocess(self, mod_export_path):
        if not os.path.isdir(mod_export_path):
            print("[FluidProbe][ERROR] 输出目录不存在: {}".format(mod_export_path))
            return

        ns = _namespace_id(mod_export_path)
        host_hashes = self._host_hashes(mod_export_path)

        # 严格模式：目标第 3 条要求「约束不满足就在导出期报错而不是静默失败」。
        # 门控一个都没收到 ⇒ 门控段恒为 0 ⇒ 整条流体链静默死掉，这是最难查的形态
        # （看着没报错、游戏里什么也没发生）。
        #
        # 必须判在 _deploy 之前：写盘顺序是 _deploy（fluid_probe.ini + 全部 hlsl +
        # Profiles/*.json）→ _inject_emitter_hooks（改宿主 ini）。等写完再报错，
        # 盘上留下的是「门控段缺失、$fluid_present 恒 0」的半成品，还得用户自己回滚。
        if self.gate_by_host_hash and not host_hashes:
            print(
                "[FluidProbe][ERROR] 开了「按宿主 hash 门控」却一个 hash 都没收到。"
                "这会让 $fluid_present 恒为 0，流体在游戏里完全不生效。请任选其一："
                "①在节点里填「宿主 ini（可选）」指向角色的 ini；"
                "②导出时确保角色 ini 与流体同目录；"
                "③关掉门控（流体常驻计算，多角色同屏时会互相干扰）。"
            )
            return

        try:
            written, total, dropped, retargeted = self._deploy(
                mod_export_path, ns, host_hashes
            )
        except Exception as exc:
            import traceback
            print("[FluidProbe][ERROR] 部署失败: {}".format(exc))
            traceback.print_exc()
            return

        try:
            hooks = self._inject_emitter_hooks(mod_export_path, ns)
        except Exception as exc:
            import traceback
            print("[FluidProbe][ERROR] 发射器注入失败: {}".format(exc))
            traceback.print_exc()
            hooks = -1

        print("[FluidProbe] namespace = {}".format(ns))
        print("[FluidProbe] 发射器注入: {} 个宿主 ini{}".format(
            hooks if hooks >= 0 else 0,
            "" if hooks > 0 else "（没有 first_index 落在任何 draw 区间内的发射器）",
        ))
        print("[FluidProbe] 门控 hash: {} 个{}".format(
            len(host_hashes),
            "" if host_hashes else "（未找到宿主 hash；门控已关，流体常驻计算）",
        ))
        print("[FluidProbe] IniParams 重映射: {}；着色器缓存: {}".format(
            "开 (237-243/251-255 → 29-40)" if self.remap_ini_params else "关",
            "带" if (self.include_shaders_cache and not self.remap_ini_params) else "不带",
        ))
        if retargeted:
            print("[FluidProbe] 宿主引用重定向: {}".format(", ".join(retargeted)))
        if dropped:
            print("[FluidProbe] 清理失效缓存: {} 个 .bin".format(dropped))
        print("[FluidProbe] 文件: {}/{} 个写入（内容未变则跳过）".format(written, total))
        print("[FluidProbe] 完成")


def _active_fluid_node(context):
    """从节点编辑器上下文里取到本节点。

    Blender 的 NodeEditor 在 3.0+ 里把当前选中节点同时暴露为 ``context.node``
    与 ``context.active_node``；两者在不同版本/不同编辑模式下未必同时有值，
    所以都试。取不到就返回 None，由调用方报错而不是抛异常。
    """
    for attr in ("node", "active_node"):
        n = getattr(context, attr, None)
        if isinstance(n, SSMTNode_PostProcess_FluidProbe):
            return n
    return None


class SSMT_OT_FluidEmitterAdd(bpy.types.Operator):
    bl_idname = 'ssmt.fluid_emitter_add'
    bl_label = '添加发射器'
    bl_description = '新增一个发射器；导出时它会占一个全局 id（0 起）'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        node = _active_fluid_node(context)
        if node is None:
            self.report({'ERROR'}, '请在节点编辑器里选中流体探针节点')
            return {'CANCELLED'}
        if len(node.emitters) >= fluid_ini_merge.EMITTER_CAPACITY:
            self.report({'ERROR'}, '发射器已达上限 %d（$fluid_emit_0..%d 只有 %d 个槽）'
                        % (fluid_ini_merge.EMITTER_CAPACITY,
                           fluid_ini_merge.EMITTER_CAPACITY - 1,
                           fluid_ini_merge.EMITTER_CAPACITY))
            return {'CANCELLED'}
        e = node.emitters.add()
        e.name = 'emitter%d' % len(node.emitters)
        node.emitter_index = len(node.emitters) - 1
        return {'FINISHED'}


def _selected_polygons(context):
    """返回 ``(obj, [三角形序号...])``。

    编辑模式必须走 bmesh：``mesh.polygons[i].select`` 在编辑模式下**不随选择更新**
    （选择状态活在 BMesh 里），直接读它拿到的是进编辑模式之前的旧值。
    这就是「取选中面读不到当前选择」的根因。
    """
    obj = context.active_object
    if obj is None or obj.type != 'MESH':
        return None, []
    if getattr(obj, 'mode', 'OBJECT') == 'EDIT':
        import bmesh

        bm = bmesh.from_edit_mesh(obj.data)
        bm.faces.ensure_lookup_table()
        return obj, [f.index for f in bm.faces if f.select]
    return obj, [p.index for p in obj.data.polygons if p.select]


def _tilt_yaw_from_normal(target_normal, a, b, c):
    """把世界法线 ``target_normal`` 投到一个三角形的 TBN 里，反解 ``(tilt, yaw)`` 度。

    与 ``fluid_ini_merge.emitter_direction`` 互为逆运算：
    三角形法线当 z 轴、``normalize(b-a)`` 当 x 轴、``cross(z, x)`` 当 y 轴，
    ``target_normal`` 在该基下的分量是 ``(-sin t·sin yaw, sin t·cos yaw, cos t)``
    （铰链式；``tilt`` 的正负就是「朝上摆 / 朝下摆」）。
    """
    import math

    ax, ay, az = a
    bx, by, bz = b
    cx, cy, cz = c
    ux, uy, uz = bx - ax, by - ay, bz - az
    vx, vy, vz = cx - ax, cy - ay, cz - az
    nx = uy * vz - uz * vy
    ny = uz * vx - ux * vz
    nz = ux * vy - uy * vx
    ln = math.sqrt(nx * nx + ny * ny + nz * nz)
    if ln < 1e-12:
        return 0.0, 0.0
    nx, ny, nz = nx / ln, ny / ln, nz / ln
    lx = math.sqrt(ux * ux + uy * uy + uz * uz)
    if lx < 1e-12:
        return 0.0, 0.0
    tx, ty, tz = ux / lx, uy / lx, uz / lx
    btx = ny * tz - nz * ty
    bty = nz * tx - nx * tz
    btz = nx * ty - ny * tx

    d = max(-1.0, min(1.0,
                     target_normal[0] * nx + target_normal[1] * ny + target_normal[2] * nz))
    tilt = math.degrees(math.acos(d))
    px = target_normal[0] * tx + target_normal[1] * ty + target_normal[2] * tz
    py = target_normal[0] * btx + target_normal[1] * bty + target_normal[2] * btz
    if abs(px) < 1e-12 and abs(py) < 1e-12:
        return tilt, 0.0
    # emitter_direction 造的是 (-sin t·sin yaw, sin t·cos yaw, cos t)：
    # 第 0 分量在 tangent 上取负、第 1 分量在 bitangent 上，所以
    # yaw = atan2(-px, py)。写成 atan2(py, px) 会解出 90°-yaw 的镜像角。
    return tilt, math.degrees(math.atan2(-px, py))


class SSMT_OT_FluidEmitterPickFace(bpy.types.Operator):
    bl_idname = 'ssmt.fluid_emitter_pick_face'
    bl_label = '取选中面'
    bl_description = (
        '把当前活动物体与它上面选中的那个面记下来，填进「Blender 物体」和「面序号」。'
        '导出时按「该物体 draw 的起始索引 + 面序号×3」反算首索引，不需要手数索引。'
    )
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        node = _active_fluid_node(context)
        return node is not None and len(node.emitters) > 0

    def execute(self, context):
        node = _active_fluid_node(context)
        if node is None or not len(node.emitters):
            self.report({'ERROR'}, '没有可写的发射器')
            return {'CANCELLED'}

        obj, picked = _selected_polygons(context)
        if obj is None:
            self.report({'ERROR'}, '先选中一个网格物体')
            return {'CANCELLED'}
        if not picked:
            self.report({'ERROR'}, '没有选中任何面（编辑模式里点几个面再点这里）')
            return {'CANCELLED'}

        me = obj.data
        mw = obj.matrix_world
        verts = [tuple(mw @ v.co) for v in me.vertices]
        tris = [tuple(p.vertices) for p in me.polygons]
        e = node.emitters[min(max(0, node.emitter_index), len(node.emitters) - 1)]
        e.object_name = obj.name

        if len(picked) == 1:
            e.poly_index = int(picked[0])
            e.bx = e.by = 0.333333
            e.bz = 0.333334
            e.tilt = 0.0
            e.yaw = 0.0
            self.report({'INFO'}, '已记录 %s 的第 %d 号面（共 %d 面）'
                                  % (obj.name, picked[0], len(tris)))
            return {'FINISHED'}

        center, normal, _area = fluid_ini_merge.face_cluster_stats(verts, tris, picked)
        if center is None:
            self.report({'ERROR'}, '选中的面全部退化，算不出等效中心')
            return {'CANCELLED'}
        t, dist = fluid_ini_merge.nearest_triangle(verts, tris, picked, center)
        if t < 0:
            self.report({'ERROR'}, '找不到可用的三角形')
            return {'CANCELLED'}

        i0, i1, i2 = tris[t]
        bary = fluid_ini_merge.clamp_barycentric(
            fluid_ini_merge.triangle_barycentric(center, verts[i0], verts[i1], verts[i2]))
        e.poly_index = int(t)
        e.bx, e.by, e.bz = bary
        if normal is not None:
            tilt, yaw = _tilt_yaw_from_normal(normal, verts[i0], verts[i1], verts[i2])
            e.tilt = tilt
            e.yaw = yaw
        else:
            e.tilt = 0.0
            e.yaw = 0.0

        self.report({'INFO'},
                    '已聚合 %d 个面 → 抓第 %d 号面（中心偏差 %.1f mm，法线偏移 %.1f°）'
                    % (len(picked), t, dist * 1000.0, e.tilt))
        return {'FINISHED'}


class SSMT_OT_FluidEmitterRemove(bpy.types.Operator):
    bl_idname = 'ssmt.fluid_emitter_remove'
    bl_label = '删除发射器'
    bl_description = '删除当前选中的发射器'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        node = _active_fluid_node(context)
        return node is not None and len(node.emitters) > 0

    def execute(self, context):
        node = _active_fluid_node(context)
        if node is None or not len(node.emitters):
            return {'CANCELLED'}
        idx = min(max(0, node.emitter_index), len(node.emitters) - 1)
        node.emitters.remove(idx)
        node.emitter_index = max(0, idx - 1)
        return {'FINISHED'}


class SSMT_OT_FluidEmitterClear(bpy.types.Operator):
    bl_idname = 'ssmt.fluid_emitter_clear'
    bl_label = '清空发射器'
    bl_description = '删除全部发射器'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        node = _active_fluid_node(context)
        return node is not None and len(node.emitters) > 0

    def execute(self, context):
        node = _active_fluid_node(context)
        if node is None:
            return {'CANCELLED'}
        node.emitters.clear()
        node.emitter_index = 0
        return {'FINISHED'}


class SSMT_UL_fluid_emitters(bpy.types.UIList):
    bl_idname = 'SSMT_UL_fluid_emitters'

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            layout.label(text='%d  %s' % (index, item.name or '(未命名)'))
        else:
            layout.alignment = 'CENTER'
            layout.label(text='%d' % index)


class SSMT_OT_FluidForceFieldAdd(bpy.types.Operator):
    bl_idname = 'ssmt.fluid_force_field_add'
    bl_label = '添加力场'
    bl_description = '给当前发射器加一个力场（每个发射器最多 4 个）'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        node = _active_fluid_node(context)
        if node is None or not len(node.emitters):
            return False
        em = node.emitters[min(max(0, node.emitter_index), len(node.emitters) - 1)]
        return len(em.force_fields) < fluid_ini_merge.FORCE_FIELDS_PER_SLOT

    def execute(self, context):
        node = _active_fluid_node(context)
        if node is None or not len(node.emitters):
            return {'CANCELLED'}
        em = node.emitters[min(max(0, node.emitter_index), len(node.emitters) - 1)]
        if len(em.force_fields) >= fluid_ini_merge.FORCE_FIELDS_PER_SLOT:
            self.report({'WARNING'}, '每个发射器最多 %d 个力场'
                        % fluid_ini_merge.FORCE_FIELDS_PER_SLOT)
            return {'CANCELLED'}
        field = em.force_fields.add()
        field.name = 'field%d' % len(em.force_fields)
        em.force_field_index = len(em.force_fields) - 1
        _on_param_update(node)
        return {'FINISHED'}


class SSMT_OT_FluidForceFieldRemove(bpy.types.Operator):
    bl_idname = 'ssmt.fluid_force_field_remove'
    bl_label = '删除力场'
    bl_description = '删除当前选中的力场'
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        node = _active_fluid_node(context)
        if node is None or not len(node.emitters):
            return False
        em = node.emitters[min(max(0, node.emitter_index), len(node.emitters) - 1)]
        return len(em.force_fields) > 0

    def execute(self, context):
        node = _active_fluid_node(context)
        if node is None or not len(node.emitters):
            return {'CANCELLED'}
        em = node.emitters[min(max(0, node.emitter_index), len(node.emitters) - 1)]
        if not len(em.force_fields):
            return {'CANCELLED'}
        idx = min(max(0, em.force_field_index), len(em.force_fields) - 1)
        em.force_fields.remove(idx)
        em.force_field_index = max(0, idx - 1)
        _on_param_update(node)
        return {'FINISHED'}


class SSMT_UL_fluid_force_fields(bpy.types.UIList):
    bl_idname = 'SSMT_UL_fluid_force_fields'

    def draw_item(self, context, layout, data, item, icon, active_data,
                  active_propname, index):
        kind = {'DIRECTIONAL': '定向', 'RADIAL': '径向',
                'VORTEX': '涡旋'}.get(item.kind, item.kind)
        if self.layout_type in {'DEFAULT', 'COMPACT'}:
            layout.label(
                text='%d  %s  %.3f m %s %.1f m/s²' % (
                    index, kind, item.radius,
                    '' if item.enabled else '(关) ', item.strength,
                ),
                icon='FORCE_FORCE' if item.enabled else 'X',
            )
        else:
            layout.alignment = 'CENTER'
            layout.label(text='%d' % index)


#: 预览集合名。用完可以整组删掉，不污染用户的场景结构。
PREVIEW_COLLECTION = 'SSMT_FluidPreview'


#: 预览用的默认色（与参考实现的全局 tint 默认值一致，见 ``Probe.ini:17-19``）。
_PREVIEW_FALLBACK_TINT = (0.9, 0.75, 0.03)


def _preview_material(tint=None):
    """按 tint 取/建一个预览材质。

    tint 相同的发射器共用同一个材质 —— 十个发射器通常只有两三种颜色，
    每个都新建材质会让 outliner 里堆一堆 ``.001`` ``.002`` 后缀。
    """
    rgb = tuple(tint) if tint else _PREVIEW_FALLBACK_TINT
    key = 'SSMT_FluidPreview_%02x%02x%02x' % (
        max(0, min(255, int(round(rgb[0] * 255)))),
        max(0, min(255, int(round(rgb[1] * 255)))),
        max(0, min(255, int(round(rgb[2] * 255)))),
    )
    mat = bpy.data.materials.get(key)
    if mat is None:
        mat = bpy.data.materials.new(key)
        mat.use_nodes = False
    # 每次都重设颜色：用户改了 tint 再点生成，同一个材质的颜色要跟上。
    mat.diffuse_color = (rgb[0], rgb[1], rgb[2], 0.55)
    return mat


def _make_mesh_obj(name, verts, faces, collection, mat):
    """按名字取或建一个 mesh 物体，**同名就复用**。

    早前这里每次都 ``bpy.data.meshes.new(name)``，同名会被 Blender 改成
    ``FluidEm0_arrow.001`` —— 于是每次重建都在场景里多留一对物体，旧的还挡在
    新物件前面。用户看到的就是「改了参数什么都没变」（其实新物体已经造出来了，
    只是被同色同位的旧物体盖住）。
    """
    ob = bpy.data.objects.get(name)
    if ob is None or ob.type != 'MESH':
        me = bpy.data.meshes.new(name)
        me.from_pydata(verts, [], faces)
        me.update()
        ob = bpy.data.objects.new(name, me)
        if collection is not None and ob.name not in collection.objects:
            collection.objects.link(ob)
    else:
        me = ob.data
        me.clear_geometry()
        me.from_pydata(verts, [], faces)
        me.update()
        if collection is not None and ob.name not in collection.objects:
            collection.objects.link(ob)
    if mat is not None:
        if ob.data.materials:
            ob.data.materials[0] = mat
        else:
            ob.data.materials.append(mat)
    return ob


def _uv_sphere(radius, rings=6, segments=8):
    verts = []
    faces = []
    for i in range(rings + 1):
        v = i / rings
        theta = v * 3.141592653589793
        for j in range(segments):
            u = j / segments
            phi = u * 6.283185307179586
            verts.append((
                radius * __import__('math').sin(theta) * __import__('math').cos(phi),
                radius * __import__('math').sin(theta) * __import__('math').sin(phi),
                radius * __import__('math').cos(theta),
            ))
    for i in range(rings):
        for j in range(segments):
            a = i * segments + j
            b = i * segments + (j + 1) % segments
            c = (i + 1) * segments + j
            d = (i + 1) * segments + (j + 1) % segments
            faces.append((a, b, d, c))
    return verts, faces


def _cone(radius, length, segments=8):
    """锥体：底圈在 z=0，尖在 z=length。"""
    import math
    verts = []
    faces = []
    for j in range(segments):
        phi = j / segments * 2.0 * math.pi
        verts.append((radius * math.cos(phi), radius * math.sin(phi), 0.0))
    verts.append((0.0, 0.0, length))
    tip = len(verts) - 1
    for j in range(segments):
        faces.append((j, (j + 1) % segments, tip))
    return verts, faces


def _basis_from_direction(direction):
    """给一个方向，造一组正交基（返回 3x3 的列：x=右, y=上, z=方向）。"""
    from mathutils import Vector
    z = Vector(direction)
    if z.length < 1e-8:
        z = Vector((0.0, 0.0, 1.0))
    z.normalize()
    ref = Vector((0.0, 0.0, 1.0))
    if abs(z.dot(ref)) > 0.99:
        ref = Vector((0.0, 1.0, 0.0))
    x = ref.cross(z)
    x.normalize()
    y = z.cross(x)
    return x, y, z


def build_preview_markers(node, scene):
    """按 ``node.emitters`` 在 ``scene`` 里造预览标记，返回 ``(made, skipped)``。

    刻意不依赖 ``context``：这样能在 ``blender --background`` 里直接调、直接断言，
    而不是只能靠“在 UI 里点一下看看”来验证。Operator 只是薄壳。
    """
    import math

    from mathutils import Vector

    col = bpy.data.collections.get(PREVIEW_COLLECTION)
    if col is None:
        col = bpy.data.collections.new(PREVIEW_COLLECTION)
        scene.collection.children.link(col)

    # 默认色必须从 node 取。早前这里写成 self（Operator 实例）会在真实 Blender
    # 里当场 AttributeError——mock 测试发现不了这类错。
    default_tint = (node.tint_r, node.tint_g, node.tint_b)
    made = 0
    skipped = []
    for i, e in enumerate(node.emitters):
        obj = bpy.data.objects.get(e.object_name) if e.object_name else None
        if obj is None or obj.type != 'MESH':
            skipped.append('%d:no-object' % i)
            continue
        # 面序号是物体内的 polygon.index，直接可用；
        # first_index 是游戏 IB 里的绝对索引（含该 draw 的偏移），
        # 除以 3 得到的是「整个 IB 摊平后的三角形号」而不是物体内的序号，
        # 只有在该物体 draw 从索引 0 起时才碰巧相等。所以没填面序号就不预览。
        if int(e.poly_index) < 0:
            skipped.append('%d:no-poly(用「取选中面」填面序号)' % i)
            continue
        tri = int(e.poly_index)
        me = obj.data
        if tri >= len(me.polygons):
            skipped.append('%d:tri%d>=%d' % (i, tri, len(me.polygons)))
            continue
        poly = me.polygons[tri]
        if len(poly.vertices) < 3:
            skipped.append('%d:not-a-triangle' % i)
            continue
        # 三角形自己的 TBN，与 ResolveSurface.hlsl:35-40 的取法一致：
        #   n = normalize(cross(b-a, c-a))、tangent = normalize(b-a)、bitangent = cross(n, tangent)
        # 早先这里用 _basis_from_direction(nrm) 造了一个“随便一组正交基”，
        # 那不是三角形的切空间，预览出来的方向与游戏里的方向对不上。
        pv = [obj.matrix_world @ me.vertices[vi].co for vi in poly.vertices]
        nrm = (pv[1] - pv[0]).cross(pv[2] - pv[0])
        if nrm.length < 1e-10:
            skipped.append('%d:degenerate' % i)
            continue
        nrm.normalize()
        t_axis = (pv[1] - pv[0])
        if t_axis.length < 1e-10:
            skipped.append('%d:degenerate' % i)
            continue
        t_axis.normalize()
        b_axis = nrm.cross(t_axis)

        # 喷口位置用重心坐标，而不是死用三角形中心——
        # 「取选中面」在合并多个面时会把等效中心折成重心坐标存下来，
        # 这里要用同一个重心才能把位置还原出来。
        contact = pv[0] * e.bx + pv[1] * e.by + pv[2] * e.bz
        center = contact

        t = math.radians(e.tilt)
        yaw = math.radians(e.yaw)
        # v[5..7] = (sin t·cos yaw, sin t·sin yaw, cos t) 是在 (tangent, bitangent, n) 下的系数
        direction = (t_axis * (math.sin(t) * math.cos(yaw))
                     + b_axis * (math.sin(t) * math.sin(yaw))
                     + nrm * math.cos(t))
        if direction.length < 1e-8:
            direction = nrm
        direction.normalize()

        # 颜色反映该发射器最终的 tint（自定义色优先，否则跟全局）。
        # 注意是元组字面量，不是 tuple(a, b, c)——后者会 TypeError。
        tint = (e.tint_r, e.tint_g, e.tint_b) if e.custom_color else default_tint
        mat = _preview_material(tint)

        r = 0.006 * max(0.2, float(e.width))
        sv, sf = _uv_sphere(r)
        ball = _make_mesh_obj(
            'FluidEm%d_ball' % i,
            [tuple(center + Vector(v)) for v in sv],
            sf, col, mat,
        )
        ball.hide_select = True

        length = 0.02 + 0.01 * abs(int(e.level))
        cv, cf = _cone(r * 0.45, length)
        bx, by, bz = _basis_from_direction(direction)
        origin = center + direction * (r * 1.2)
        world = [tuple(origin + bx * v[0] + by * v[1] + bz * v[2]) for v in cv]
        arrow = _make_mesh_obj('FluidEm%d_arrow' % i, world, cf, col, mat)
        arrow.hide_select = True
        made += 2

    # 发射器被删掉后，集合里会留下孤儿（上一轮建的 FluidEm2/3…）。
    # 这里按「本轮真正用到的名字」清一遍，避免删了发射器场景里还挂着旧标记。
    keep = set()
    for i, e in enumerate(node.emitters):
        if int(e.poly_index) < 0:
            continue
        keep.add('FluidEm%d_ball' % i)
        keep.add('FluidEm%d_arrow' % i)
    for ob in list(col.objects):
        if ob.name.startswith('FluidEm') and ob.name not in keep:
            bpy.data.objects.remove(ob, do_unlink=True)

    return made, skipped


# ---------------------------------------------------------------------------
# 力场可视化
#
# 力场只写进导出（`ForceField.hlsl` 读 `cs-t8`），Blender 里原本**什么都看不到**：
# 加一个力场，面板多了几行数字，场景里没有任何变化。用户没法判断它到底落在
# 哪儿、朝哪边、影响多大。这里按力场的真实参数画一套线框标记。
#
# 标记的位置与朝向完全照 `ForceField.hlsl:38-52` 的算法：
#   center = anchor + right*U + up*V + forward*W
#   axis   = right*axU + up*axV + forward*axW
# 所以「标记画在哪，游戏里就在哪」。父级挂到发射源物体上，源物体一动标记跟着动。
# ---------------------------------------------------------------------------

FORCE_FIELD_COLLECTION = 'SSMT_FluidForceFields'

#: 三种力场各一种颜色，负强度换成偏红的那支。
_FF_COLORS = {
    'DIRECTIONAL': ((1.0, 0.55, 0.10), (1.0, 0.25, 0.25)),
    'RADIAL': ((0.20, 0.65, 1.0), (1.0, 0.30, 0.70)),
    'VORTEX': ((0.65, 0.35, 1.0), (1.0, 0.35, 0.45)),
}


def _ff_material(kind, negative):
    rgb = _FF_COLORS.get(kind, _FF_COLORS['RADIAL'])[1 if negative else 0]
    key = 'SSMT_FluidFF_%s_%s' % (kind, 'neg' if negative else 'pos')
    mat = bpy.data.materials.get(key)
    if mat is None:
        mat = bpy.data.materials.new(key)
        mat.use_nodes = False
    mat.diffuse_color = (rgb[0], rgb[1], rgb[2], 1.0)
    return mat


def _circle(center, u, v, radius, segments=32):
    """在 ``u``/``v`` 张成的平面上画一圈线，返回 ``(verts, edges)``。"""
    verts, edges = [], []
    from math import cos, sin, pi
    for k in range(segments):
        a = 2.0 * pi * k / segments
        verts.append(tuple(center + u * (radius * cos(a)) + v * (radius * sin(a))))
    for k in range(segments):
        edges.append((k, (k + 1) % segments))
    return verts, edges


def _force_field_geometry(field, center, right, up, forward):
    """把一条力场变成 ``(顶点, 边, 面)``。

    共同部分是一圈圈线框（半径 = 影响半径，直接给出衰减范围）；
    差异部分是「它往哪使劲」：定向画箭头、径向画六个向内的尖刺、涡旋画螺旋。
    """
    from mathutils import Vector
    from math import cos, sin, pi

    verts, edges, faces = [], [], []

    def take(part_v, part_e=None, part_f=None):
        base = len(verts)
        verts.extend(part_v)
        if part_e:
            edges.extend([(a + base, b + base) for a, b in part_e])
        if part_f:
            faces.extend([tuple(idx + base for idx in f) for f in part_f])
        return base

    R = max(float(field.radius), 1e-4)
    axis = right * float(field.axis_u) + up * float(field.axis_v) + forward * float(field.axis_w)
    if axis.length < 1e-9:
        axis = forward.copy()
    axis.normalize()
    # 与 axis 垂直的一组基，用来画垂直于轴的圆
    helper = up if abs(axis.dot(up)) < 0.9 else right
    a1 = axis.cross(helper)
    if a1.length < 1e-9:
        a1 = right.copy()
    a1.normalize()
    a2 = axis.cross(a1)
    a2.normalize()

    take(*_circle(center, right, up, R))
    take(*_circle(center, right, forward, R))
    take(*_circle(center, up, forward, R))

    if field.kind == 'DIRECTIONAL':
        # 轴线 + 尖端 + 一圈伞骨：一眼看出往哪边推。
        tip = center + axis * (R * 1.35)
        head = center + axis * (R * 1.05)
        tip_index = len(verts)
        verts.append(tuple(center))
        verts.append(tuple(tip))
        edges.append((tip_index, tip_index + 1))
        tip_index += 1
        ring = len(verts)
        for k in range(8):
            a = 2.0 * pi * k / 8
            verts.append(tuple(head + a1 * (R * 0.18 * cos(a)) + a2 * (R * 0.18 * sin(a))))
        for k in range(8):
            edges.append((ring + k, ring + (k + 1) % 8))
            edges.append((ring + k, tip_index))

    elif field.kind == 'RADIAL':
        # 三个互相垂直的方向各画一根从内到外的线：看得出是「从中心往外」。
        for d in (right, up, axis):
            for sign in (1.0, -1.0):
                take([tuple(center + d * (R * 0.25 * sign)),
                      tuple(center + d * (R * 0.95 * sign))], [(0, 1)])

    else:  # VORTEX
        steps = 28
        for lane in (0.45, 0.8):
            pts = []
            for k in range(steps):
                a = 2.0 * pi * k / (steps - 1) * 1.5
                radial = a1 * (R * lane * cos(a)) + a2 * (R * lane * sin(a))
                pts.append(tuple(center + radial + axis * (R * 0.55 * (k / (steps - 1) - 0.5))))
            base = len(verts)
            verts.extend(pts)
            edges.extend([(base + k, base + k + 1) for k in range(steps - 1)])

    return verts, edges, faces


def _make_wire_obj(name, verts, edges, faces, collection, mat):
    """带边的线框物体；同名复用，和 ``_make_mesh_obj`` 一样的理由。"""
    import bpy

    ob = bpy.data.objects.get(name)
    if ob is None or ob.type != 'MESH':
        me = bpy.data.meshes.new(name)
        ob = bpy.data.objects.new(name, me)
        if collection is not None:
            collection.objects.link(ob)
    else:
        me = ob.data
        me.clear_geometry()
    me.from_pydata(verts, edges, faces)
    me.update()
    if collection is not None and ob.name not in collection.objects:
        collection.objects.link(ob)
    if mat is not None:
        if me.materials:
            me.materials[0] = mat
        else:
            me.materials.append(mat)
    return ob


def build_force_field_markers(node, scene):
    """按每个发射器下面的力场画线框标记，返回 ``(生成数, 跳过列表)``。"""
    import bpy
    from mathutils import Vector

    col = bpy.data.collections.get(FORCE_FIELD_COLLECTION)
    if col is None:
        col = bpy.data.collections.new(FORCE_FIELD_COLLECTION)
        scene.collection.children.link(col)

    made, skipped, keep = 0, [], set()
    for i, e in enumerate(node.emitters):
        if not len(getattr(e, 'force_fields', ())):
            continue
        basis = node._emitter_basis(e)
        if basis is None:
            skipped.append('%s(力场)' % (e.name or ('e%d' % i)))
            continue
        origin, right, up, forward, _normal = basis
        src = bpy.data.objects.get(e.object_name) if e.object_name else None

        for j, f in enumerate(e.force_fields):
            if not f.enabled:
                continue
            name = 'FluidFF%d_%d' % (i, j)
            keep.add(name)
            center = (origin + right * float(f.offset_u)
                      + up * float(f.offset_v) + forward * float(f.offset_w))
            verts, edges, faces = _force_field_geometry(f, center, right, up, forward)

            # 挂到发射源物体上：源物体移动/旋转，标记自动跟着走，不用重建。
            if src is not None:
                inv = src.matrix_world.inverted()
                verts = [tuple(inv @ Vector(v)) for v in verts]

            ob = _make_wire_obj(name, verts, edges, faces, col,
                                _ff_material(f.kind, float(f.strength) < 0.0))
            if src is not None:
                # 顶点已经是**源物体局部坐标**（上面乘过 matrix_world 的逆），
                # 所以父级逆矩阵必须是单位阵 —— 写 src.matrix_world.inverted()
                # 会把源物体的变换重复反一次，标记就飘到别处。
                ob.parent = src
                ob.matrix_parent_inverse.identity()
                ob.matrix_world = src.matrix_world
            else:
                ob.parent = None
                ob.matrix_world.identity()
            ob.hide_select = True
            ob.hide_render = True  # 只在视口里当参照物，别进渲染
            ob.display_type = 'WIRE'
            made += 1

    for ob in list(col.objects):
        if ob.name.startswith('FluidFF') and ob.name not in keep:
            bpy.data.objects.remove(ob, do_unlink=True)

    return made, skipped


def _stale_fluid_warning():
    """检查场景里那个粒子网格是不是「旧的空壳」；不是就返回 ``None``。

    这里要挡的是一个很常见的误会：用户点了只生成标记的那一档，模拟没重跑，
    场景里留着上一次（甚至更早版本）烘出来的物体。这种物体要么没有形态键、
    要么所有顶点叠在原点 —— 视口里看着就是「有物体、但什么都没有」。
    光说「空的」没法查，所以把判据写出来。
    """
    import bpy

    ob = bpy.data.objects.get('SSMT_FluidParticles')
    if ob is None or ob.type != 'MESH':
        return None
    if not ob.data.shape_keys or len(ob.data.shape_keys.key_blocks) < 2:
        return '粒子网格没有形态键（是旧版烘出来的空壳）——点「模拟喷射」重跑一次'
    if len(ob.data.vertices) == 0:
        return '粒子网格没有顶点——点「模拟喷射」重跑一次'
    xs = [v.co for v in ob.data.vertices]
    lo = [min(c[i] for c in xs) for i in range(3)]
    hi = [max(c[i] for c in xs) for i in range(3)]
    if max(hi[i] - lo[i] for i in range(3)) < 1e-4:
        return '粒子网格的顶点全叠在原点（旧版烘出来的）——点「模拟喷射」重跑一次'
    return None


class SSMT_OT_FluidPreviewBuild(bpy.types.Operator):
    bl_idname = 'ssmt.fluid_preview_build'
    bl_label = '生成预览标记'
    bl_description = (
        '按每个发射器的 Blender 物体 + poly_index 找到三角面，'
        '在面心画一个球（喷口）和一个锥（方向）。'
        '球半径按 width 缩放、锥长按 |level| 缩放、颜色按 tint。'
        '（参考实现里 speed 不是可调参数，所以锥长不反映速度。）'
    )
    bl_options = {'REGISTER', 'UNDO'}

    with_sim: bpy.props.BoolProperty(
        name='同时模拟喷射',
        default=True,
        description=(
            '勾上就顺手跑一遍 PBF 并把结果烘焙成可播放的粒子动画。\n'
            '参数一律取节点上的值（帧数 / 帧率 / 粒子池 / 粒子半径等），\n'
            '模拟是分块跑的，进度见节点面板的进度条'
        ),
    )

    def execute(self, context):
        node = _active_fluid_node(context)
        if node is None:
            self.report({'ERROR'}, '请在节点编辑器里选中流体探针节点')
            return {'CANCELLED'}

        made, skipped = build_preview_markers(node, context.scene)
        if skipped:
            self.report({'INFO'}, '跳过 %d 条：%s' % (len(skipped), ', '.join(skipped[:6])))
        self.report({'INFO'}, '生成 %d 个预览物体（集合 %s）' % (made, PREVIEW_COLLECTION))

        # 力场标记与球锥分开：球锥要跟着模拟结果一起刷新，力场只跟参数走。
        ff_made, ff_skipped = build_force_field_markers(node, context.scene)
        if ff_made or ff_skipped:
            self.report({'INFO'}, '力场标记 %d 个（集合 %s）%s'
                        % (ff_made, FORCE_FIELD_COLLECTION,
                           ('；跳过 ' + ', '.join(ff_skipped[:4])) if ff_skipped else ''))

        if not self.with_sim:
            stale = _stale_fluid_warning()
            if stale:
                self.report({'WARNING'}, stale)
            return {'FINISHED'}
        # 也走分块版：这个算子（生成预览）同样要跑 PBF，阻塞的话按钮按下去
        # 界面就僵住几秒。进度条在节点面板里，算完自动烘焙。
        try:
            started = sim_start_chunked(node)
        except ValueError as exc:
            self.report({'ERROR'}, '模拟失败：%s' % exc)
            return {'CANCELLED'}
        except Exception as exc:
            self.report({'ERROR'}, '无法开始模拟：%s' % exc)
            return {'CANCELLED'}
        if started:
            _request_redraw()
            self.report({'INFO'}, '标记已生成；模拟已开始，进度见节点面板')
        else:
            self.report({'WARNING'}, '已经有一次模拟在跑，等它算完')
        return {'FINISHED'}
        return {'FINISHED'}


class SSMT_OT_FluidPreviewClear(bpy.types.Operator):
    bl_idname = 'ssmt.fluid_preview_clear'
    bl_label = '清除预览'
    bl_description = '删掉预览标记（SSMT_FluidPreview）与模拟结果（SSMT_FluidSim）'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        removed = []
        failed = []

        # 1) 预览标记：球 + 锥
        col = bpy.data.collections.get(PREVIEW_COLLECTION)
        if col is not None:
            for ob in list(col.objects):
                data = ob.data
                bpy.data.objects.remove(ob, do_unlink=True)
                if data is not None and data.users == 0:
                    bpy.data.meshes.remove(data)
            bpy.data.collections.remove(col)
            removed.append('标记')

        # 2) 模拟结果：粒子容器 + mesh + 节点组 + 材质 + 集合
        #    这一步之前漏了——用户点「清除预览」只看到球锥没了，
        #    SSMT_FluidSim 那个集合和里面的 1024 顶点对象还留着。
        #
        #    失败要留痕：这里原本是 ``except Exception: pass``，于是
        #    「``importlib`` 没导入」这种必现错误被吞成「点了没反应」，查了很久。
        try:
            _bm = importlib.import_module('.fluid_preview_bake', __package__)
            if _bm.clear() > 0:
                removed.append('模拟')
        except Exception as exc:                      # noqa: BLE001 - 余下步骤仍要跑
            import traceback
            failed.append(str(exc))
            print("[FluidProbe][WARN] 清除模拟结果失败（继续清其余项）: {}".format(exc))
            traceback.print_exc()

        # 3) 力场标记
        ff_col = bpy.data.collections.get(FORCE_FIELD_COLLECTION)
        if ff_col is not None:
            for ob in list(ff_col.objects):
                data = ob.data
                bpy.data.objects.remove(ob, do_unlink=True)
                if data is not None and data.users == 0:
                    bpy.data.meshes.remove(data)
            bpy.data.collections.remove(ff_col)
            removed.append('力场标记')

        # 3) 烘焙缓存 .npz：留着的话下次「模拟喷射」会以为已经有结果
        scene = getattr(context, 'scene', None)
        blend_dir = ""
        if scene is not None and bpy.data.filepath:
            blend_dir = os.path.dirname(bpy.data.filepath)
        for name in ('fluid_preview.npz',):
            for base in (blend_dir, os.getcwd()):
                if not base:
                    continue
                p = os.path.join(base, name)
                if os.path.isfile(p):
                    try:
                        os.remove(p)
                        removed.append('缓存')
                    except OSError:
                        pass

        # 失败的项不能只留在控制台：面板上要看得见，否则又变成「点了没反应」。
        trouble = '；模拟结果没清掉：{}'.format(failed[0]) if failed else ''
        if not removed:
            self.report({'WARNING'} if failed else {'INFO'},
                        '没有可清除的预览' + trouble)
            return {'CANCELLED'}
        self.report({'WARNING'} if failed else {'INFO'},
                    '已清除：' + '、'.join(removed) + trouble)
        return {'FINISHED'}



class SSMT_OT_FluidSimulate(bpy.types.Operator):
    """跑一遍 Position Based Fluids，把结果烘焙成可播放的粒子动画

    参数取节点上每个发射器的绑定（物体 + 面号 + 重心 + 方向 + 宽度），
    物理常量逐项对照 ``fluid\\FluidSolver.hlsl``（核函数、lambda 正则项、
    2mm 修正夹取、速度上限 6、寿命 1.1s）与 ``fluid\\SoftNozzle.hlsl``
    （喷嘴压力→开口→初速，默认档算出 0.514 m/s）。

    这不是游戏内的屏幕空间版本——碰撞用 Blender 场景里的地面平面近似，
    片状（SheetProfile）与溅射（Splash）没做。够判断「水柱往哪喷、多粗、
    落点在哪」，不够复现游戏里的贴面薄膜效果。
    """

    bl_idname = 'ssmt.fluid_preview_simulate'
    bl_label = '模拟喷射（PBF）'
    bl_options = {'REGISTER', 'UNDO'}

    # 这里**不再**重复节点的参数。
    # 早先这一层有 frames/fps/capacity/ground_z/particle_radius/voxel/solid
    # 七个属性，但 execute 从来只传 cache_name —— 用户在操作符上改了毫无反应，
    # 还以为「改了没生效」。现在统一取节点上的值，面板里配一次就行。

    @classmethod
    def poll(cls, context):
        return _active_fluid_node(context) is not None

    def execute(self, context):
        node = _active_fluid_node(context)
        if node is None:
            self.report({'ERROR'}, '没有选中的流体探针节点')
            return {'CANCELLED'}

        # 走分块版：算子立刻返回，主线程每 50 ms 算一小段，面板上的进度条
        # 才能动。以前这里直接跑完，界面会僵住几秒且没有任何反馈。
        # 结果（帧数/粒子池/缓存名）不再能从返回值里拿，所以只报「已开始」；
        # 需要具体数字时看面板下方的导出摘要与烘焙后的顶点数。
        try:
            started = sim_start_chunked(node, cache_name='fluid_preview.npz')
        except ValueError as exc:
            self.report({'ERROR'}, str(exc))
            return {'CANCELLED'}
        except Exception as exc:  # 半成品参数（面号越界等）
            self.report({'ERROR'}, '无法开始模拟：%s' % exc)
            return {'CANCELLED'}

        if not started:
            self.report({'WARNING'}, '已经有一次模拟在跑，等它算完')
            return {'CANCELLED'}

        _request_redraw()
        self.report({'INFO'}, '开始模拟（进度见节点面板的进度条）')
        return {'FINISHED'}


class SSMT_OT_FluidSimClear(bpy.types.Operator):
    """清掉模拟出来的粒子动画（不动静态预览标记）"""

    bl_idname = 'ssmt.fluid_preview_sim_clear'
    bl_label = '清除模拟'
    bl_options = {'REGISTER', 'UNDO'}

    def execute(self, context):
        import importlib

        bake_mod = importlib.import_module('.fluid_preview_bake', __package__)
        # 路径规则必须和 run_simulation 一致：.blend 所在目录，没存过就用 cwd
        blend_dir = os.path.dirname(bpy.data.filepath) if bpy.data.filepath else ''
        cache_path = os.path.join(blend_dir or os.getcwd(), bake_mod.CACHE_NAME)
        removed = bake_mod.clear(npz_path=cache_path)
        self.report({'INFO'}, '清掉 %d 项（含缓存文件）' % removed)
        return {'FINISHED'}


class SSMT_OT_FluidCheckHost(bpy.types.Operator):
    bl_idname = 'ssmt.fluid_check_host'
    bl_label = '检查宿主 ini'
    bl_description = (
        '只读一次宿主 ini，报告：门控 hash 有几个、配置里有没有会静默失败的组合。'
        '不写任何文件。'
    )
    bl_options = {'REGISTER'}

    def execute(self, context):
        node = _active_fluid_node(context)
        if node is None:
            self.report({'ERROR'}, '请在节点编辑器里选中流体探针节点')
            return {'CANCELLED'}

        problems = []
        notes = []

        # remap 与 .bin 是互斥的：重映射会改写 hlsl，旧 .bin 的 mtime 就对不上了
        # （CommandList.cpp:2409 用 CompareFileTime 精确比对），带了也是白带。
        if node.remap_ini_params and node.include_shaders_cache:
            problems.append('「重映射 IniParams」与「携带 .bin」同时开启：hlsl 会被改写，旧的 .bin 必然失配')

        if node.gate_by_host_hash:
            base = ''
            hpath = (getattr(node, 'host_ini_path', '') or '').strip()
            if hpath:
                base = os.path.dirname(hpath) or '.'
            elif node.id_data is not None and getattr(node.id_data, 'filepath', ''):
                base = os.path.dirname(node.id_data.filepath) or '.'
            hashes = []
            if base and os.path.isdir(base):
                hashes = node._host_hashes(base)
            if not hashes:
                problems.append(
                    '门控开着但读不到任何 hash → $fluid_present 恒为 0，流体在游戏里完全不生效'
                )
            else:
                notes.append('门控 hash: %d 个' % len(hashes))
        else:
            notes.append('门控关闭 → 流体常驻计算（多角色同屏时会互相干扰）')

        if not node.emitters:
            problems.append('没有发射器 → 喷口永远回退到 (0, 0.85, 0)')
        else:
            missing_poly = [i for i, e in enumerate(node.emitters) if int(e.poly_index) < 0]
            if missing_poly:
                problems.append(
                    '发射器 %s 没有面序号 → 不会写进 gpu_data，也不会画预览'
                    % ','.join(str(i) for i in missing_poly)
                )
            missing_host = [i for i, e in enumerate(node.emitters) if not (e.object_name or '').strip()]
            if missing_host:
                notes.append('发射器 %s 没填宿主物体名（只影响预览，不影响导出）'
                             % ','.join(str(i) for i in missing_host))

        if problems:
            for line in problems:
                print('[FluidProbe][CHECK] %s' % line)
            self.report({'WARNING'}, '；'.join(problems)[:200])
            return {'FINISHED'}
        self.report({'INFO'}, '；'.join(notes) if notes else '未发现问题')
        return {'FINISHED'}


classes = (
    SSMTFluidForceField,
    SSMTFluidEmitter,
    SSMTNode_PostProcess_FluidProbe,
    SSMT_OT_FluidEmitterAdd,
    SSMT_OT_FluidEmitterPickFace,
    SSMT_OT_FluidEmitterRemove,
    SSMT_OT_FluidEmitterClear,
    SSMT_UL_fluid_emitters,
    SSMT_OT_FluidForceFieldAdd,
    SSMT_OT_FluidForceFieldRemove,
    SSMT_UL_fluid_force_fields,
    SSMT_OT_FluidPreviewBuild,
    SSMT_OT_FluidSimulate,
    SSMT_OT_FluidSimClear,
    SSMT_OT_FluidCheckHost,
    SSMT_OT_FluidPreviewClear,
)


def _scene_ground_z(exclude_names=()):
    """场景里最低的网格顶点 z，排除预览标记与粒子对象。

    用户的原话是「地面高度应该就等于发射器高度」——但发射器常常在手上，
    用水柱落到手的高度会让它悬在半空几厘米。真正的地面是场景里那块地板
    （或者角色的脚底），也就是**所有网格里最低的那个顶点**。
    """
    exclude = set(exclude_names or ())
    lowest = None
    for obj in bpy.data.objects:
        if obj.type != 'MESH' or obj.name in exclude:
            continue
        # 预览标记（FluidEm*）与粒子网格本身不该参与判断
        if obj.name.startswith(('FluidEm', 'SSMT_Fluid')):
            continue
        me = obj.data
        if me is None or not me.vertices:
            continue
        mw = obj.matrix_world
        for v in me.vertices:
            z = (mw @ v.co).z
            if lowest is None or z < lowest:
                lowest = z
    return lowest


def _sim_setup(node, frames=None, fps=None, capacity=None, ground_z=None,
               particle_radius=None, solid=None, voxel=None, threshold=0.1,
               cache_name='fluid_preview.npz'):
    """把节点上的参数解析成「模拟参数 + 收尾参数」两组，不跑任何计算。

    拆出来是为了让**阻塞版**（``run_simulation``）和**带进度条的分块版**
    （``_chunk_*``）共用同一段解析：发射器世界位置、力场中心、地面高度、
    体积化半径这些都要读 bpy 数据，只能在主线程、也只需要算一次。
    """
    import importlib
    import os

    sim_mod = importlib.import_module('.fluid_preview_sim', __package__)
    bake_mod = importlib.import_module('.fluid_preview_bake', __package__)

    frames = getattr(node, 'sim_frames', 60) if frames is None else frames
    fps = getattr(node, 'sim_fps', 30.0) if fps is None else fps
    capacity = getattr(node, 'sim_capacity', 1024) if capacity is None else capacity
    if particle_radius is None:
        # 节点上存的**就是米**（面板单位已统一成米），不需要换算：
        # 物理常量（FLUID_RADIUS / H / MASS）与着色器全是米制。
        particle_radius = float(getattr(node, 'particle_radius', 0.0028))
    if solid is None:
        solid = getattr(node, 'solid', True)
    # 体积化的两个半径必须**成对**算，不能各自独立：
    #   volume_radius —— 影响半径（决定水柱多粗）
    #   voxel         —— 体素边长（决定多细）
    # 体素一旦大于影响半径，PointsToVolume 采样会跳过相邻粒子，
    # 水柱直接断成一串珠子（实测过：体素 5.6 mm vs 影响半径 4.5 mm）。
    # 所以体素跟着**影响半径**走，取 0.75 倍留余量，而不是跟着 particle_radius。
    _vol_m = float(getattr(node, 'volume_radius', 0.0) or 0.0)
    _vol_str = _vol_m if _vol_m > 0.0 else float(particle_radius) * 4.5
    if voxel is None:
        voxel = max(_vol_str * 0.75, 0.0015)

    # 速度不设独立倍率：游戏里喷速完全由强度档决定（SoftNozzle.hlsl:9-51 拿
    # slotDrive 的 pressure 换算），预览也走同一条路，多一个倍率只会让两边不一致。
    speed_scale = 1.0

    emitters = []
    skipped = []
    for i, e in enumerate(node.emitters):
        world = node._emitter_world(e)
        if world is None:
            skipped.append('%s(#%d 面 %d)' % (e.name or ('e%d' % i),
                                              -1 if not e.object_name else 0,
                                              e.poly_index))
            continue
        origin, direction, normal = world
        emitters.append({
            'origin': [origin.x, origin.y, origin.z],
            'direction': [direction.x, direction.y, direction.z],
            # 喷口椭圆要用「上」这个参考方向：`FluidSolver.hlsl:140` 拿它投影出 slitLong
            'up': [normal.x, normal.y, normal.z],
            'speed': float(getattr(node, 'launch_speed', sim_mod.DEFAULT_SPEED)),
            'speed_scale': speed_scale,
            'width': float(e.width),
            'slot': i,
            # 档位是**全局 strength_level + 本发射器的相对偏移**，
            # 不是我先前那样直接拿偏移当档位（`Probe.ini:14` 的 `$strength_level`
            # 默认 3、`:434-436` 把它换算成 .565+(level-1)*.5625）。
            'level': int(max(0, min(7, int(getattr(node, 'strength_level', 3)) + int(e.level)))),
        })
    if not emitters:
        raise ValueError('没有可解析的发射器（物体不存在或面号越界）')

    # -- 力场 -------------------------------------------------------------
    # 每个力场的中心与轴都写在**发射器自己的基**上（`ForceField.hlsl:56,64`），
    # 所以这里必须用 `_emitter_basis` 而不是 `_emitter_world`：后者只给一个朝向量，
    # 没有 right/up，既算不出 U/V/W 偏移，也分不清「轴」和「方向」。
    #
    # 这样处理之后，预览与游戏用的是同一套公式、同一套基、同一份参数，
    # 只是数值在 CPU 上算。两边唯一的差别是浮点顺序，不是模型。
    force_fields = []
    for e in node.emitters:
        fields = list(getattr(e, 'force_fields', ()) or ())
        if not fields:
            continue
        basis = node._emitter_basis(e)
        if basis is None:
            continue
        origin, right, up, forward, _normal = basis
        for f in fields:
            if not f.enabled:
                continue
            center = (origin + right * float(f.offset_u)
                      + up * float(f.offset_v) + forward * float(f.offset_w))
            axis = (right * float(f.axis_u)
                    + up * float(f.axis_v) + forward * float(f.axis_w))
            force_fields.append({
                'kind': f.kind,
                'center': [center.x, center.y, center.z],
                'axis': [axis.x, axis.y, axis.z],
                'radius': float(f.radius),
                'strength': float(f.strength),
                'falloff': float(f.falloff),
                'enabled': True,
            })
    force_scale = float(getattr(node, 'force_scale', 1.0))

    # 「本次模拟用哪个地面」的优先级：
    #   显式传了正常值        → 用它（临时试一下）
    #   传了哨兵 / 没传        → 节点「地面自动」开 → 取场景最低顶点
    #                          关 → 用节点的 ground_height
    #   两个都 < -1e5         → 不做地面（让粒子自由下落）
    if ground_z is not None and float(ground_z) > -1.0e5:
        ground = float(ground_z)
    elif getattr(node, 'ground_auto', True):
        # 排除的物件：预览标记、粒子容器，以及**所有发射源物体本身**。
        # 发射源是水从上面喷出来的那个物体，它的最低顶点不是地面——
        # 不排除的话，喷口就在它表面、水一出来就撞在它自己身上停住
        # （实测：喷口 z=1.033、立方体顶面 z=1.1 → 取到 0.9，水只走 15 cm）。
        skip = set()
        for e in getattr(node, 'emitters', []) or []:
            name = getattr(e, 'object_name', '')
            if name:
                skip.add(name)
        for nm in ('SSMT_FluidParticles',):
            obj = bpy.data.objects.get(nm)
            if obj:
                skip.add(obj.name)
        ground = _scene_ground_z(exclude_names=sorted(skip))
        if ground is None:
            ground = float(getattr(node, 'ground_height', 0.0))
    else:
        node_ground = float(getattr(node, 'ground_height', 0.0))
        ground = None if node_ground < -1.0e5 else node_ground
    # nhd-pick: 重力倍率与粘度，和 game 侧 IniParams[47].x/.y 同一对旋钮
    if hasattr(sim_mod, 'set_physics'):
        sim_mod.set_physics(
            gravity_scale=float(getattr(node, 'gravity_scale', 1.0)),
            viscosity=float(getattr(node, 'viscosity', 0.0)),
        )
    sim_kwargs = dict(emitters=emitters, frames=int(frames), fps=float(fps),
                      capacity=int(capacity), ground_z=ground,
                      force_fields=force_fields, force_scale=force_scale,
                      # 面板半径要进模拟器，否则 Blender 里看到的粒子尺度与游戏无关。
                      # 来源: Toolset/fluid/fluid/ParticleConfig.hlsl:26 fluidRadiusOf
                      radius=float(particle_radius))
    finish = {
        'cache_name': cache_name,
        'radius': float(particle_radius),
        'solid': bool(solid),
        'voxel': float(voxel),
        'threshold': float(threshold),
        'volume_radius': _vol_m,
        'capacity': int(capacity),
        'emitters': len(emitters),
        'skipped': skipped,
        'force_fields': len(force_fields),
    }
    return sim_mod, bake_mod, sim_kwargs, finish


def _sim_finalize(node, sim_frames, finish):
    """把模拟结果落盘并烘焙成可播放的动画。只在主线程调用（要动 bpy）。"""
    import importlib
    import os

    sim_mod = importlib.import_module('.fluid_preview_sim', __package__)
    bake_mod = importlib.import_module('.fluid_preview_bake', __package__)

    blend_dir = os.path.dirname(bpy.data.filepath) if bpy.data.filepath else ''
    cache_path = os.path.join(blend_dir or os.getcwd(), finish['cache_name'])
    sim_mod.save_npz(cache_path, sim_frames, int(finish['capacity']))

    info = bake_mod.bake(
        cache_path, start_frame=1, radius=float(finish['radius']),
        solid=bool(finish['solid']), voxel=float(finish['voxel']),
        threshold=float(finish['threshold']),
        volume_radius=float(finish['volume_radius']))
    info['emitters'] = finish['emitters']
    info['skipped'] = finish['skipped']
    info['cache'] = cache_path
    info['force_fields'] = finish['force_fields']
    # 跑完要更新指纹，否则自动重算会立刻把刚算好的结果再算一遍。
    _PREVIEW_STATE['sim_fingerprint'] = _sim_fingerprint(node)
    return info


def run_simulation(node, frames=None, fps=None, capacity=None, ground_z=None,
                   particle_radius=None, solid=None, voxel=None,
                   threshold=0.1, cache_name='fluid_preview.npz'):
    """解析发射器 → 跑 PBF → 存 npz → 烘焙成可播放的粒子动画（一次性跑完）。

    这是**阻塞版**：界面会停住到算完。面板上的按钮走的是带进度条的分块版
    （``sim_start_chunked``），两者共用 ``_sim_setup`` / ``_sim_finalize``，
    所以结果一致。测试与离线脚本仍可调这个，因为它有返回值。
    """
    sim_mod, _bake, kwargs, finish = _sim_setup(
        node, frames=frames, fps=fps, capacity=capacity, ground_z=ground_z,
        particle_radius=particle_radius, solid=solid, voxel=voxel,
        threshold=threshold, cache_name=cache_name)
    sim_frames = sim_mod.simulate(**kwargs)
    return _sim_finalize(node, sim_frames, finish)


# -- 参数联动的自动重建 -------------------------------------------------------
#
# 预览标记（球 + 锥）很便宜，可以跟着参数实时重建；PBF 模拟要几秒，不能挂在
# 每帧回调上。所以分成两挡：
#   * 改任何发射器/全局参数 → 标记自动重建（下面的 handler）
#   * 模拟结果只在显式点「模拟喷射」时刷新，参数一改就标成过期
#
# 早期版本没有这一层，用户改完参数看预览毫无变化，因为它只是静态物体。

#: 上次重建标记时的参数指纹。None 表示还没比对过。
#: ``sim_fingerprint`` 是「上一次真跑模拟时」的参数，``sim_dirty`` 记下每个节点
#: 第一次发现过期的时间（用来做防抖：拖滑块时不要每改一格就跑一遍 PBF）。
_PREVIEW_STATE = {
    'fingerprint': None,
    'busy': False,
    'sim_fingerprint': None,
    'sim_dirty': {},
}

#: 停手多久之后再重算模拟（秒）。太小会在拖滑块时反复触发，太大就不像「实时」。
AUTO_SIM_DEBOUNCE = 0.8

#: 当前正在跑的分块模拟任务。``job`` 为 None 表示空闲。
#: ``frames`` 在模拟跑完后暂存结果，等下一次 tick 做烘焙（烘焙要动 bpy，
#: 不能和纯 numpy 的模拟混在同一段里，否则进度条又变成一次卡到底）。
_CHUNK = {'job': None, 'node': None, 'frames': None, 'finish': None,
          'phase': '', 't0': 0.0}


def _emitters_fingerprint(node):
    """把影响预览形状的字段拼成可比较的元组。

    只取**影响几何**的字段：位置（物体 + 面号 + 重心）、方向（tilt/yaw/roll）、
    宽度、强度、偏移、自定义色。改了个名字不该触发重建，所以 name 不在里面。
    """
    parts = []
    for e in node.emitters:
        parts.append((
            e.object_name, int(e.poly_index),
            round(float(e.bx), 6), round(float(e.by), 6), round(float(e.bz), 6),
            round(float(e.tilt), 4), round(float(e.yaw), 4), round(float(e.roll), 4),
            round(float(e.width), 4), int(e.level),
            round(float(e.ox), 5), round(float(e.oy), 5), round(float(e.oz), 5),
            bool(e.custom_color),
            round(float(e.tint_r), 4), round(float(e.tint_g), 4), round(float(e.tint_b), 4),
            round(float(e.absorption), 5),
            round(float(e.tlen), 4), round(float(e.bend_delay), 4), int(e.bend_str),
            bool(e.trick), round(float(e.trick_sp), 4), bool(e.vis),
            # 力场也影响可见形状（线框落点、半径、方向），必须进指纹，
            # 否则改力场参数时标记不跟着动。
            tuple(
                (bool(f.enabled), f.kind,
                 round(float(f.offset_u), 5), round(float(f.offset_v), 5), round(float(f.offset_w), 5),
                 round(float(f.axis_u), 4), round(float(f.axis_v), 4), round(float(f.axis_w), 4),
                 round(float(f.radius), 5), round(float(f.strength), 4),
                 round(float(f.falloff), 3))
                for f in getattr(e, 'force_fields', ())
            ),
        ))
    return tuple(parts)


def _sim_fingerprint(node):
    """模拟结果的指纹：形状指纹 + 所有会改变流态的旋钮。

    刻意和 ``_emitters_fingerprint`` 分开：改个名字、改颜色不该触发几秒钟的
    重算，但改速度/档位/地面/力场必须触发。分不开就只能二选一——
    要么「改了不动」，要么「动一下就重跑」。
    """
    return (
        _emitters_fingerprint(node),
        int(getattr(node, 'sim_frames', 60)),
        round(float(getattr(node, 'sim_fps', 30.0)), 3),
        int(getattr(node, 'sim_capacity', 1024)),
        round(float(getattr(node, 'particle_radius', 0.0028)), 6),
        bool(getattr(node, 'solid', True)),
        round(float(getattr(node, 'volume_radius', 0.0) or 0.0), 4),
        int(getattr(node, 'strength_level', 3)),
        # 速度/重力/粘度都是流态旋钮：不进取指纹的话，面板上改了它们
        # 只会静默失效（看起来「调了没反应」），必须进。
        round(float(getattr(node, 'launch_speed', 0.0) or 0.0), 5),
        round(float(getattr(node, 'gravity_scale', 1.0)), 5),
        round(float(getattr(node, 'viscosity', 0.0)), 9),
        bool(getattr(node, 'ground_auto', True)),
        round(float(getattr(node, 'ground_height', 0.0)), 4),
        round(float(getattr(node, 'force_scale', 1.0)), 4),
    )


def _find_fluid_nodes(scene):
    """扫出这个场景里所有流体探针节点。

    节点树可能挂在几何节点修改器上、也可能只是躺在 ``bpy.data.node_groups``
    里没被任何物体引用（用户点“生成预览”时常见）。两条路都要看：
    先扫 ``bpy.data.node_groups``（不依赖场景结构，最稳），再扫修改器。
    """
    found = []
    seen = set()
    for group in bpy.data.node_groups:
        for n in group.nodes:
            if isinstance(n, SSMTNode_PostProcess_FluidProbe) and n.as_pointer() not in seen:
                seen.add(n.as_pointer())
                found.append(n)
    if scene is not None:
        for ob in scene.objects:
            for mod in getattr(ob, 'modifiers', ()):
                if getattr(mod, 'type', '') != 'NODES':
                    continue
                group = getattr(mod, 'node_group', None)
                if group is None:
                    continue
                for n in group.nodes:
                    if isinstance(n, SSMTNode_PostProcess_FluidProbe) and n.as_pointer() not in seen:
                        seen.add(n.as_pointer())
                        found.append(n)
    return found


@bpy.app.handlers.persistent
def _on_depsgraph_update(scene, depsgraph=None):
    """参数一变就重建标记。

    只在「标记集合已经存在」时才动 —— 用户没点过预览就什么都不做，免得
    凭空往场景里塞物体。
    """
    if _PREVIEW_STATE['busy']:
        return
    if scene is None:
        return
    # 两个集合任一个在就重建：用户可能只加了力场、还没跑模拟。
    has_preview = bpy.data.collections.get(PREVIEW_COLLECTION) is not None
    has_ff = bpy.data.collections.get(FORCE_FIELD_COLLECTION) is not None
    if not (has_preview or has_ff):
        return
    for node in _find_fluid_nodes(scene):
        fp = _emitters_fingerprint(node)
        if fp == _PREVIEW_STATE['fingerprint']:
            continue
        _PREVIEW_STATE['fingerprint'] = fp
        # 重建自己会改场景，再触发 depsgraph —— 必须挡住重入，否则递归。
        _PREVIEW_STATE['busy'] = True
        try:
            if has_preview:
                build_preview_markers(node, scene)
            if has_ff:
                build_force_field_markers(node, scene)
        finally:
            _PREVIEW_STATE['busy'] = False


def _request_redraw():
    """让所有窗口重画一次。

    定时器里改状态不会自动刷新界面：进度条要动，必须自己标脏。用 ``tag_redraw``
    而不是 ``redraw_timer``，因为前者不参与绘制、不重入，安全。
    """
    try:
        wm = bpy.context.window_manager
    except Exception:
        return
    for window in wm.windows:
        screen = getattr(window, 'screen', None)
        if screen is None:
            continue
        for area in screen.areas:
            area.tag_redraw()


def sim_progress():
    """给面板读的进度；没有任务在跑时返回 ``None``。"""
    job = _CHUNK['job']
    if job is None:
        return None
    phase = _CHUNK['phase']
    if phase == 'bake':
        return (1.0, '烘焙形态键…')
    if phase == 'save':
        return (1.0, '写出缓存…')
    return (job.progress, job.label())


def sim_start_chunked(node, **overrides):
    """开始一次带进度条的模拟。返回 ``False`` 表示已有任务在跑。"""
    if _CHUNK['job'] is not None:
        return False
    sim_mod, _bake, kwargs, finish = _sim_setup(node, **overrides)
    job = sim_mod.SimulationJob(**kwargs)
    _CHUNK.update(job=job, node=node, finish=finish, phase='sim', t0=time.time())
    _CHUNK['frames'] = None
    return True


def _chunk_advance(budget_seconds=0.05):
    """推进当前任务一小段。返回 ``True`` 表示任务仍然在跑。"""
    job = _CHUNK['job']
    if job is None:
        return False
    if _CHUNK['phase'] == 'sim':
        deadline = time.time() + max(0.005, budget_seconds)
        while not job.finished and time.time() < deadline:
            job.advance(1)
        if not job.finished:
            return True
        _CHUNK['frames'] = job.out
        _CHUNK['phase'] = 'bake'
        return True
    if _CHUNK['phase'] == 'bake':
        node = _CHUNK['node']
        frames = _CHUNK['frames']
        finish = _CHUNK['finish']
        _CHUNK['job'] = None
        _CHUNK['frames'] = None
        _CHUNK['phase'] = ''
        try:
            _sim_finalize(node, frames, finish)
        except Exception as exc:  # 烘焙失败不能把定时器带崩
            print('[流体] 烘焙失败：%s' % exc)
        return False
    _CHUNK['job'] = None
    _CHUNK['phase'] = ''
    return False


def _auto_sim_tick():
    """定时器：推进在跑的任务；空闲时负责「停手后自动重算」。

    为什么不直接在 depsgraph 回调里跑：PBF 要几秒，拖滑块时每一格都触发一次，
    界面会卡死到没法用。所以回调只负责「记下它脏了」，这里负责「没人再动它了
    才真跑」，而且跑的时候也是**分块**推进 —— 一次算完同样会卡住界面，
    进度条根本没机会画出来。

    也解释了为什么阈值是「停手」而不是「改了就变」——鱼和熊掌。
    """
    # 有任务在跑：只推进它，别的都不管（否则会同时起第二个 job）。
    if _CHUNK['job'] is not None:
        still = _chunk_advance()
        _request_redraw()
        return 0.05 if still else 0.2

    try:
        scene = bpy.context.scene
    except Exception:
        return 0.5
    if scene is None or _PREVIEW_STATE['busy']:
        return 0.4
    # 没有粒子物体就别白扫：用户可能只是把节点摆在那儿。
    if bpy.data.objects.get('SSMT_FluidParticles') is None:
        return 0.6

    now = time.time()
    for node in _find_fluid_nodes(scene):
        if not bool(getattr(node, 'auto_sim', True)):
            continue
        fp = _sim_fingerprint(node)
        if fp == _PREVIEW_STATE['sim_fingerprint']:
            _PREVIEW_STATE['sim_dirty'].pop(node.as_pointer(), None)
            continue
        first = _PREVIEW_STATE['sim_dirty'].setdefault(node.as_pointer(), now)
        if (now - first) < AUTO_SIM_DEBOUNCE:
            continue
        _PREVIEW_STATE['sim_dirty'].pop(node.as_pointer(), None)
        try:
            started = sim_start_chunked(node)
        except Exception as exc:  # 参数半成品（面号改到一半）会真的抛
            # 记成「已算过」，否则每 0.4 秒重试同一个失败，控制台会被刷爆。
            _PREVIEW_STATE['sim_fingerprint'] = fp
            print('[流体] 自动重算跳过：%s' % exc)
            continue
        if started:
            _request_redraw()
            return 0.05
    return 0.4


def register():
    for cls in classes:
        bpy.utils.register_class(cls)
    if _on_depsgraph_update not in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.append(_on_depsgraph_update)
    if not bpy.app.timers.is_registered(_auto_sim_tick):
        bpy.app.timers.register(_auto_sim_tick, first_interval=0.6, persistent=True)


def unregister():
    if bpy.app.timers.is_registered(_auto_sim_tick):
        bpy.app.timers.unregister(_auto_sim_tick)
    if _on_depsgraph_update in bpy.app.handlers.depsgraph_update_post:
        bpy.app.handlers.depsgraph_update_post.remove(_on_depsgraph_update)
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
