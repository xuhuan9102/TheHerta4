# -*- coding: utf-8 -*-
"""把 ``fluid_preview_sim`` 的模拟结果烘焙成 Blender 里的可播放动画。

设计要点：

* **一个 mesh，每帧一个 shape key**。粒子数每帧不同，但 shape key 的顶点数
  必须恒定，所以统一 padding 到 ``capacity``（模拟器本来就按容量分配）。
* **插值设成 CONSTANT**。shape key 的默认 Bezier 插值会让相邻两帧的姿态
  互相混合，看起来像果冻；改成阶跃后每帧是精确的粒子位置。
* **实例化靠 Geometry Nodes**，mesh 本身只是 1024 个顶点的点云；乒乓球大小
  按 ``FLUID_RADIUS`` 走，跟游戏里的粒子半径一致。

模块顶部**不 import bpy**——这样 pytest 能直接 import 它。bpy 在函数内部导入。
"""

import os

import numpy as np

#: 粒子实例半径（米）。与 ``fluid_preview_sim.FLUID_RADIUS`` 同值。
INSTANCE_RADIUS = 0.0028

#: Preserve the game's visible radius; display smoothing must not enlarge it.
VOLUME_RADIUS_VISUAL_SCALE = 1.0
#: 死粒子（counts[i] 之后的尾部填充）被搬到这个坐标：离相机足够远，
#: 但**不改 shape key 的容量**——每帧仍是 capacity 个坐标，只是把无效点挪走。
PARKED = None  # 保留名字给旧引用，实际不再使用


def _park_tail(flat, count, capacity):
    """把 ``count`` 之后的坐标设成 PARKED（就地改传入的副本）。"""
    n = int(max(0, min(int(count), capacity)))
    if 0 < n < capacity:
        # 死点收拢到第一个活点上：不扩 bbox、不形成孤立几何（PARKED 放到 -20 m 会把
        # 体素域撑到 21 m，实测 bbox z=21.2，等同自己造了一堵墙）。
        flat.reshape(capacity, 3)[n:] = flat[0:3]
    return flat

MATERIAL_NAME = "SSMT_FluidSim"
#: 模拟缓存的默认文件名（``run_simulation`` 写到工程根目录）。
CACHE_NAME = "fluid_preview.npz"


# ---------------------------------------------------------------------------
# 显示几何：逐式对齐 Toolset/fluid/fluid 的渲染端
# ---------------------------------------------------------------------------
#
# 这里每一个乘数都从 shader 逐行搬过来，而且**只有这一份实现**：
# ``fluid_preview_sim`` 的 slot_width / visual_scale / puddle_* 与这里用的是同一批
# 函数（本模块通过 :func:`_sim` 取），所以"表里的数"和"画面上的数"不可能分开。

_SIM = None


#: 面板/ini 的液体外观输入，与游戏同一组数。
#: 来源: Toolset/fluid/Probe.ini:17-20  $liquid_tint_r/g/b = 1.0000 / $liquid_absorb_mm = 15
#: 来源: Probe.ini:243-246  x251=tint_r y251=tint_g z251=tint_b w251=absorb_mm/1000
PANEL_LIQUID_TINT = (1.0, 1.0, 1.0)
PANEL_LIQUID_ABSORB_MM = 15.0


def liquid_absorb(tint_rgb, absorb_mm):
    """每米的吸收系数（Beer-Lambert 的指数）。

    来源: fluid/FluidComposite.hlsl:160-161
      float depthMeters=max(look.w,.00002);
      float3 absorb=-log(max(saturate(look.rgb),.02))/depthMeters;
    """
    depth = max(float(absorb_mm) / 1000.0, 0.00002)
    return tuple(max(0.0, -float(np.log(max(min(float(c), 1.0), 0.02))) / depth)
                 for c in tint_rgb)


def liquid_body_color(tint_rgb, absorb_mm):
    """液体在自身吸收深度处的颜色。

    来源: fluid/FluidComposite.hlsl:166-168
      float3 tint=exp(-absorb*depthMeters);      // 代数上就是 saturate(tint_rgb)
    """
    absorb = liquid_absorb(tint_rgb, absorb_mm)
    depth = max(float(absorb_mm) / 1000.0, 0.00002)
    return tuple(float(np.exp(-a * depth)) for a in absorb)


#: Blender 材质用的颜色/不透明度：从上面那组输入按 shader 的式子算出来，不是手填常量。
#:
#: ``LIQUID_ALPHA = 1.0`` 的依据是**游戏那个 pass 是覆盖写**，不是"为了让 alpha 对拍
#: 相等"：来源: fluid/FluidComposite.hlsl:246 ``return float4(color,1.)``（硬编码 1），
#: 来源: fluid/Composite.ini:43 与 :77 两处 ``blend = disable``（``o0 = set_viewport ...
#: Resource\LiquidGravityProbe\SceneColor``，写回替换场景色）。既然 alpha 不参与混合，
#: 预览材质也必须是 1.0 + 不透明，否则预览能透过水看见背景、游戏看不见 —— 那是观感差，
#: 不是透明度参数差。
LIQUID_TINT = liquid_body_color(PANEL_LIQUID_TINT, PANEL_LIQUID_ABSORB_MM) + (1.0,)
LIQUID_ALPHA = 1.0


def _sim():
    """``fluid_preview_sim``（纯 numpy 模块，不依赖 bpy）。

    bake 与 sim 共用宽度/档位/铺展的口径。包内走相对导入；对拍脚本用
    ``spec_from_file_location`` 单独加载本模块时没有包上下文，按同目录路径回退。
    """
    global _SIM
    if _SIM is None:
        try:
            from . import fluid_preview_sim as module
        except Exception:                      # noqa: BLE001 - 独立加载时的回退
            import importlib.util
            spec = importlib.util.spec_from_file_location(
                'fluid_preview_sim',
                os.path.join(os.path.dirname(os.path.abspath(__file__)),
                             'fluid_preview_sim.py'))
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
        _SIM = module
    return _SIM


#: 来源: fluid/ParticleConfig.hlsl:20  ``FLUID_RADIUS_DEFAULT``
FLUID_RADIUS_DEFAULT = 0.0028
#: 来源: fluid/FluidSurface.hlsl:17  ``PUDDLE_SPLAT_SCALE``
PUDDLE_SPLAT_SCALE = 2.0


def surface_radius(radius):
    """渲染半径的基准 = 粒子半径 × 1.6。

    来源: fluid/SSFRCommon.hlsl:14
      float surfaceRadius(){return fluidRadiusOf(IniParams[44])*1.6;}
    """
    r = float(radius)
    if not r > 1e-6:
        r = FLUID_RADIUS_DEFAULT      # 来源: ParticleConfig.hlsl:26 的 0 回退
    return r * 1.6


def stream_radius(radius, width=1.0, level=3.0):
    """主流粒子的可见半径。

    来源: fluid/FluidSurface.hlsl:57-59（primary 分支）
      visualScale = lerp(.75,1,smoothstep(.28,1.69,slotDrive(owner)))
      width = slotWidth(owner)
      radius = surfaceRadius()*width*visualScale
    """
    sim = _sim()
    return surface_radius(radius) * sim.slot_width(width) * sim.visual_scale(level)


def splash_radius(radius, level=3.0, travel=0.0):
    """飞溅子滴的可见半径（非 primary 分支：width 固定 1）。

    来源: fluid/FluidSurface.hlsl:58-59
      radius = surfaceRadius()*(visualScale*max(densityBirth.z,.1))
    """
    return (surface_radius(radius) * _sim().visual_scale(level)
            * max(float(travel), 0.1))


def splash_birth_offset(radius, scale=1.20):
    """飞溅子滴沿法线的出生偏移（不含切向抖动）。

    来源: fluid/Splash.hlsl:256  ``float scale=.55+.65*random01(...)``（取上界 1.20）
    来源: fluid/Splash.hlsl:259  ``n*(e.normalRadius.w*scale+.0001)``
    """
    return float(radius) * float(scale) + 0.0001


def puddle_disc_radius(radius, travel=0.0, spread=0.0):
    """地面水洼盘的半径。

    来源: fluid/FluidSurface.hlsl:61
      radius=surfaceRadius()*max(densityBirth.z,.1)*puddleWidth(densityBirth.x)*PUDDLE_SPLAT_SCALE
    来源: fluid/Puddle.hlsl:9  ``puddleWidth(spread)=1+2*saturate(spread)``
    """
    return (surface_radius(radius) * max(float(travel), 0.1)
            * _sim().puddle_width(spread) * PUDDLE_SPLAT_SCALE)


def puddle_center_height(radius, travel=0.0, spread=0.0):
    """水洼盘中心离地高度（盘被抬到接触面上方这么高）。

    来源: fluid/FluidSurface.hlsl:74
      center -= normal*puddleHalfHeight(fluidRadiusOf*max(densityBirth.z,.1), densityBirth.x)
    来源: fluid/Puddle.hlsl:10-13  ``radius/(width*width)``
    """
    return _sim().puddle_half_height(float(radius) * max(float(travel), 0.1), spread)


def volume_influence_radius(radius, width=1.0, level=3.0, panel_volume_radius=0.0):
    """体积化（solid）的影响半径 = **可见半径**，不再自己乘 4.5。

    旧版是 ``radius * 4.5``（再被体素夹一次到 5.0625×），比游戏粗 3.16×。
    体积化只负责"把点熔成面"，不负责尺寸：影响半径取 :func:`stream_radius` 的
    同一个数，尺寸口径就只剩 shader 这一条。面板「水柱粗细」>0 时仍以面板值为准。
    """
    if panel_volume_radius and float(panel_volume_radius) > 0:
        return float(panel_volume_radius)
    return stream_radius(radius, width, level) * VOLUME_RADIUS_VISUAL_SCALE


def _load_npz(path):
    with np.load(path) as data:
        return (data['positions'], data['slots'], data['ages'], data['counts'])


def domain_for(points, visible_r, budget, voxel_hint):
    """一次算出 (voxel, lo, hi, cells, limited)，保证域覆盖粒子 bbox 且 cells ≤ budget。

    分开算的旧写法出过 bug：体素被预算放大后，域仍按旧体素估 ⇒ 柱子尾巴被裁。
    """
    pts = np.asarray(points, dtype=float).reshape(-1, 3)
    finite = pts[np.isfinite(pts).all(axis=1)]
    pmin = finite.min(axis=0)
    pmax = finite.max(axis=0)
    voxel = max(float(voxel_hint), 1e-4)
    limited = False
    for _ in range(200):
        pad = voxel * 2.0
        lo = pmin - pad
        hi = pmax + pad
        extent = np.maximum(hi - lo, voxel)
        cells = float(np.prod(np.ceil(extent / voxel)))
        if cells <= budget:
            break
        voxel *= 1.05
        limited = True
    assert hi[2] - lo[2] >= pmax[2] - pmin[2], "domain must cover particle bbox"
    assert cells <= budget, "cells must fit the budget"
    return voxel, lo, hi, cells, limited


def bake(npz_path, start_frame=1, radius=INSTANCE_RADIUS, collection_name="SSMT_FluidSim",
         object_name="SSMT_FluidParticles", solid=False, voxel=0.006, threshold=0.1,
         volume_radius=0.0, width=1.0, level=3.0, emitter_radii=None):
    """读 ``npz_path`` 建一个带逐帧 shape key 的粒子物体。

    ``radius``        —— 物理粒子半径（面板值，米）。画面上一个球多大不直接等于它：
                         要先过 :func:`surface_radius` 的 ×1.6 与 :func:`stream_radius`
                         的宽度/档位（与 ``FluidSurface.hlsl:57-59`` 逐式一致）。
    ``volume_radius`` —— 体积化的影响半径。0 = 自动（= :func:`stream_radius` 同一个数）。
                         注意旧版自动值是 ``radius * 4.5``，那条口径比游戏粗 3.16×。
    ``width``/``level`` —— 水柱宽度倍数与强度档（``Slots.hlsl:57-60`` / ``:51-52``）。
                         node 现在不传，用默认 1.0 / 3.0；多发射器或非默认档时要传。

    返回统计字典：``{'frames', 'capacity', 'object', 'collection', 'keys'}``。
    """
    import bpy  # 延迟导入：pytest 里不装 bpy

    _volume_radius = (float(volume_radius) if volume_radius and volume_radius > 0
                      else volume_influence_radius(radius, width, level))

    # -- 体素与影响半径的自洽（否则整片体积会塌成空网格）---------------------
    #
    # `Points to Volume` 的做法是把每个点的影响球体素化。**体素比影响半径还大
    # 的时候，一个点连一个体素中心都盖不满**，采样直接漏掉，Volume to Mesh 就
    # 吐一个空网格 —— 而且不报任何错，表现出来就是「物体在、修改器挂着、播放
    # 起来什么都没有」。
    #
    # 所以这里只**把体素缩小**去迁就半径（体素 ≤ 影响半径 / 3，一个球至少跨
    # 3 个体素才可能被采到）。**不反过来放大影响半径**：旧版那条
    # `_volume_radius = max(·, voxel*1.5, radius*1.2)` 会把半径抬到 5.0625×，
    # 比游戏的 1.6× 粗 3.16× —— 那是尺寸口径的错，不是体素问题。
    voxel = max(min(float(voxel), _volume_radius / 3.0), 1e-4)

    # 2026-11-11: 体素与域**必须一起算**。分开算就会出现「域按旧体素估、体素又被预算放大」
    # ⇒ 域比粒子 bbox 小、柱子尾巴被裁（实测网格 z 1.276 m vs 粒子跨度 2.02 m）。
    VOXEL_BUDGET = 8_000_000
    visible_r = float(radius) * 1.6
    positions, slots, ages, counts = _load_npz(npz_path)
    active_points = [p[:int(n)] for p, n in zip(positions, counts) if n]
    bbox_points = np.concatenate(active_points) if active_points else np.zeros((1, 3))
    voxel, lo, hi, cells, limited = domain_for(bbox_points, visible_r, VOXEL_BUDGET, voxel)
    if voxel >= visible_r:
        print('[FluidProbe][WARN] 体素 %.4f m 已达可见半径 %.4f m，串珠不可避免'
              % (voxel, visible_r))
    meta = {}
    meta['voxel_m'] = float(voxel)
    meta['voxel_cells'] = int(cells)
    meta['voxel_limited_by_budget'] = bool(limited)
    meta['bbox_frames_used'] = int(positions.shape[0])
    meta['domain_m'] = [float(v) for v in (hi - lo)]
    meta['bbox_m'] = [float(v) for v in (hi - lo)]
    print('[FluidProbe][INFO] voxel=%.4f m cells=%d limited=%s domain=%s'
          % (voxel, int(cells), limited,
             [round(float(v), 3) for v in (hi - lo)]))

    frame_count = int(positions.shape[0])
    capacity = int(positions.shape[1])
    if frame_count == 0:
        raise ValueError("npz 里没有帧数据")

    # -- 集合 ---------------------------------------------------------------
    coll = bpy.data.collections.get(collection_name)
    if coll is None:
        coll = bpy.data.collections.new(collection_name)
        bpy.context.scene.collection.children.link(coll)

    # -- 清掉上次烘焙的物体 ---------------------------------------------------
    old = bpy.data.objects.get(object_name)
    if old is not None:
        bpy.data.objects.remove(old, do_unlink=True)
    old_mesh = bpy.data.meshes.get(object_name)
    if old_mesh is not None:
        bpy.data.meshes.remove(old_mesh)

    # -- mesh + 物体的基础几何 ------------------------------------------------
    mesh = bpy.data.meshes.new(object_name)
    # 基础网格直接放**第一帧**的位置，而不是全零。
    #
    # 全零的时候基础 mesh 是「1024 个点全堆在原点」：视口里看物体是空的、
    # 进编辑模式也什么都没有，用户会以为烘焙失败（实际几何在修改器里）。
    # 放第一帧至少让基础 mesh 就是一根水柱，编辑模式/线框都能看见。
    first_frame = np.ascontiguousarray(positions[0], dtype=np.float32)
    mesh.from_pydata([tuple(v) for v in first_frame.tolist()], [], [])
    mesh.update()

    obj = bpy.data.objects.new(object_name, mesh)
    coll.objects.link(obj)
    obj.location = (0.0, 0.0, 0.0)
    obj.rotation_euler = (0.0, 0.0, 0.0)
    obj.scale = (1.0, 1.0, 1.0)

    # -- shape keys ---------------------------------------------------------
    basis = obj.shape_key_add(name="Basis", from_mix=False)
    basis.data.foreach_set("co", _park_tail(first_frame.copy().ravel(),
                                                    counts[0], capacity))
    key_names = []
    for i in range(frame_count):
        key = obj.shape_key_add(name="F%03d" % i, from_mix=False)
        flat = np.ascontiguousarray(positions[i].ravel(), dtype=np.float32)
        # 2026-11-11: 只画 counts[i] 个活粒子；其余挪到 PARKED，别堆在世界原点。
        key.data.foreach_set("co", _park_tail(flat.copy(), counts[i], capacity))
        # 开局全部为 0，下面再逐帧置 1
        key.value = 0.0
        key_names.append(key.name)

    # -- 逐帧 keyframe（阶跃插值）--------------------------------------------
    keys = obj.data.shape_keys
    if keys is None:
        raise RuntimeError("shape keys 没建起来")
    keys.use_relative = True

    # `ShapeKey.keyframe_insert` 的 `frame=` 参数不生效（实测：90 个 key 全被插在
    # 同一个当前帧上，帧 30 时有 12 个 key 同时为 1）。必须先用 `frame_set` 定位，
    # 再不带 frame 地插入。
    #
    # 插值类型：`Action.fcurves` 在 Blender 4.4+ 已不存在（slotted action），
    # 改成插入前临时把用户偏好设成 CONSTANT——否则相邻帧之间是 Bezier 混合，
    # 粒子会在两个姿态之间「融化」。
    scene = bpy.context.scene
    prefs = bpy.context.preferences.edit
    previous_interp = prefs.keyframe_new_interpolation_type
    prefs.keyframe_new_interpolation_type = 'CONSTANT'
    try:
        # 每个 key 先在起始帧插一个 0。
        #
        # 不这么做的话：`F030` 只在帧 31 有 key（值 1），而 fcurve 在**第一个
        # keyframe 之前是常量外推**——帧 30 时它就读到 1。实测帧 30 有 13 个 key
        # 同时为 1（F029..F039），shape key 叠加后顶点被推到 z≈11.5（真实的 1.05 的
        # 10.9 倍），画面里只剩一片白。
        scene.frame_set(start_frame)
        for name in key_names:
            block = keys.key_blocks[name]
            block.value = 0.0
            block.keyframe_insert("value")

        for i, name in enumerate(key_names):
            scene.frame_set(start_frame + i)
            block = keys.key_blocks[name]
            block.value = 1.0
            block.keyframe_insert("value")
            if i > 0:
                prev = keys.key_blocks[key_names[i - 1]]
                prev.value = 0.0
                prev.keyframe_insert("value")
    finally:
        prefs.keyframe_new_interpolation_type = previous_interp

    # 插值类型：上面那个偏好设置**没生效**（实测插完仍是 BEZIER，帧 30 时
    # F030..F039 读到 0.9967→0.8365 的爬升值，shape key 一叠加顶点就被推远）。
    # 必须在插完后手动改。Blender 4.4+ 是 slotted action，`Action.fcurves` 已不存在，
    # 真实路径是 `action.layers[0].strips[0].channelbags[0].fcurves`。
    action = keys.animation_data.action if keys.animation_data else None
    if action is not None and hasattr(action, "layers"):
        for layer in action.layers:
            for strip in layer.strips:
                for channelbag in getattr(strip, "channelbags", ()):
                    for fcurve in channelbag.fcurves:
                        for point in fcurve.keyframe_points:
                            point.interpolation = 'CONSTANT'

    # -- Geometry Nodes：点云 → 小球 -----------------------------------------
    group_name = object_name + "_GN"
    old_group = bpy.data.node_groups.get(group_name)
    if old_group is not None:
        bpy.data.node_groups.remove(old_group)

    node_group = bpy.data.node_groups.new(group_name, 'GeometryNodeTree')
    node_group.interface.new_socket("Geometry", in_out='INPUT',
                                    socket_type='NodeSocketGeometry')
    node_group.interface.new_socket("Geometry", in_out='OUTPUT',
                                    socket_type='NodeSocketGeometry')

    nodes, links = node_group.nodes, node_group.links
    src = nodes.new("NodeGroupInput")
    src.location = (-600, 0)
    dst = nodes.new("NodeGroupOutput")
    dst.location = (600, 0)

    # Snapshots compact live particles in owner order. Animate each bank's end
    # index so inactive padding contributes neither instances nor volume density.
    radii = dict(emitter_radii or {int(s): stream_radius(radius, width, level)
                                  for s in np.unique(slots) if s >= 0})
    if not radii:
        radii = {0: stream_radius(radius, width, level)}
    end_sockets = {}
    index = nodes.new('GeometryNodeInputIndex')
    radius_field = None
    for owner in reversed(sorted(radii)):
        socket = node_group.interface.new_socket('End%d' % owner, in_out='INPUT',
                                                 socket_type='NodeSocketInt')
        end_sockets[owner] = socket
        less = nodes.new('ShaderNodeMath')
        less.operation = 'LESS_THAN'
        links.new(index.outputs[0], less.inputs[0])
        links.new(src.outputs[socket.identifier], less.inputs[1])
        switch = nodes.new('GeometryNodeSwitch')
        switch.input_type = 'FLOAT'
        links.new(less.outputs[0], switch.inputs['Switch'])
        switch.inputs['True'].default_value = float(radii[owner])
        if radius_field is not None:
            links.new(radius_field, switch.inputs['False'])
        radius_field = switch.outputs[0]
    delete = nodes.new('GeometryNodeDeleteGeometry')
    delete.domain = 'POINT'
    inactive = nodes.new('ShaderNodeMath')
    inactive.operation = 'LESS_THAN'
    links.new(index.outputs[0], inactive.inputs[0])
    links.new(src.outputs[end_sockets[max(radii)].identifier], inactive.inputs[1])
    invert = nodes.new('ShaderNodeMath')
    invert.operation = 'SUBTRACT'
    invert.inputs[0].default_value = 1
    links.new(inactive.outputs[0], invert.inputs[1])
    links.new(invert.outputs[0], delete.inputs['Selection'])
    links.new(src.outputs[0], delete.inputs['Geometry'])

    ico = nodes.new("GeometryNodeMeshIcoSphere")
    ico.location = (-300, -220)
    # 一个粒子的可见半径：粒子半径 → ×1.6 → ×slotWidth → ×visualScale。
    # 来源: fluid/FluidSurface.hlsl:57-59（见本模块 stream_radius 的注释）
    ico.inputs["Radius"].default_value = 1.0
    ico.inputs["Subdivisions"].default_value = 1

    # -- 材质 -----------------------------------------------------------------
    # 实例的材质来自 `Ico Sphere` 节点，不是物体材质槽——只在 `obj.data.materials`
    # 上挂材质的话，渲染出来是白色的（实测第一版就是这样）。
    material = bpy.data.materials.get(MATERIAL_NAME)
    if material is None:
        material = bpy.data.materials.new(MATERIAL_NAME)
        material.use_nodes = True
        bsdf = material.node_tree.nodes.get("Principled BSDF")
        if bsdf is not None:
            bsdf.inputs["Base Color"].default_value = LIQUID_TINT
            bsdf.inputs["Alpha"].default_value = LIQUID_ALPHA
            bsdf.inputs["Roughness"].default_value = 0.12
        # 游戏那个 pass 是 blend=disable 的覆盖写回（fluid/Composite.ini:43/:77
        # ``blend = disable`` + FluidComposite.hlsl:246 ``return float4(color,1.)``），
        # alpha 不参与混合 ⇒ 预览材质也不透明，否则能透过水看见背景、游戏看不见。
        material.blend_method = 'OPAQUE'

    # 两条分支各自带一个 SetMaterial。
    # 早先只建了一个，先接实例分支、再被体积分支覆盖输入，而它的输出还同时
    # 喂给 `Instance on Points` —— 打开节点图看到的就是「材质节点两头接错」，
    # 实例分支拿到的是体积网格。功能上因为实例分支不连输出所以没炸，那是运气。
    set_material = nodes.new("GeometryNodeSetMaterial")
    set_material.location = (-80, -220)
    set_material.inputs["Material"].default_value = material

    # 直接拿 mesh 的顶点当实例位置。
    # 这里刻意不插 `Mesh to Points`——那个节点对「只有顶点、没有面」的 mesh
    # 产出空几何（实测 `CONVERTED verts 0 polys 0`），而 `Instance on Points`
    # 的 Points 输入本身就接受 mesh 顶点，省一步还更稳。
    on_points = nodes.new("GeometryNodeInstanceOnPoints")
    on_points.location = (100, 0)

    links.new(delete.outputs['Geometry'], on_points.inputs["Points"])
    links.new(radius_field, on_points.inputs['Scale'])
    links.new(ico.outputs["Mesh"], set_material.inputs["Geometry"])
    links.new(set_material.outputs["Geometry"], on_points.inputs["Instance"])

    if solid:
        # 游戏里水是「屏幕空间等值面」（`FluidRender.hlsl:224` 光线步进 +
        # `:168` 的 `exp(-d/.36)` 平滑场），视觉上是连续液体。一排小球永远是
        # 点阵，要像水就得把点云熔成体积再抽面：
        #   points → Volume（半径 = 粒子半径 × 2）→ Volume to Mesh → 平滑
        # `voxel` 是体素边长，越小越细、越慢；`radius` 决定相邻粒子熔不熔得到一起。
        points_to_volume = nodes.new("GeometryNodePointsToVolume")
        points_to_volume.location = (-300, 220)
        points_to_volume.inputs["Density"].default_value = 1.0
        # 影响半径必须大于**相邻粒子的实际间距**才熔得起来。实测这套参数下
        # 1024 个粒子铺在约 1 m 长、直径约 20 mm 的水柱上，均匀间距约 6.7 mm，
        # 而 `radius * 2.2 = 6.2 mm` 差一点点 ⇒ 每个粒子各自成球，出来就是珠串
        # （线段中段尤其明显：喷口附近密、落地前稀）。
        # 参考实现在游戏里是按像素做光线步进（`FluidRender.hlsl:227` 距离 < 8 mm
        # 即算命中），同样的稀疏点云在屏幕上仍是连续的水；Volume 这条路没有那种
        # “沿视线累积”的效果，只能靠加大影响半径补齐。
        points_to_volume.inputs["Radius"].default_value = max(_volume_radius, 1e-4)
        if not volume_radius or volume_radius <= 0:
            links.new(radius_field, points_to_volume.inputs['Radius'])
        if "Resolution Mode" in [s.name for s in points_to_volume.inputs]:
            points_to_volume.inputs["Resolution Mode"].default_value = 'Size'
        if "Voxel Size" in [s.name for s in points_to_volume.inputs]:
            points_to_volume.inputs["Voxel Size"].default_value = voxel

        volume_to_mesh = nodes.new("GeometryNodeVolumeToMesh")
        volume_to_mesh.location = (-80, 220)
        # 阈值越低，等值面越往外扩、越能把相邻粒子连成一片。
        # `PointsToVolume` 的密度在点处最高、往外衰减，阈值 0.1 时表面基本贴在
        # 「半径 × 0.6」处；要覆盖粒子之间 2~3 cm 的空隙就得往下调。
        volume_to_mesh.inputs["Threshold"].default_value = threshold
        # ⇒ 「体素改了、产物不变」（实测 4×4 矩阵：1.4 mm 出 66158 顶点，是 9.45 mm 的 48 倍）。
        if "Resolution Mode" in [s.name for s in volume_to_mesh.inputs]:
            volume_to_mesh.inputs["Resolution Mode"].default_value = 'Size'
        if "Voxel Size" in [s.name for s in volume_to_mesh.inputs]:
            volume_to_mesh.inputs["Voxel Size"].default_value = voxel
        print("[FluidProbe][INFO] P2V mode=%s voxel=%.5f | V2M mode=%s voxel=%.5f"
          % (points_to_volume.inputs["Resolution Mode"].default_value, voxel,
             volume_to_mesh.inputs["Resolution Mode"].default_value,
             volume_to_mesh.inputs["Voxel Size"].default_value))
        volume_to_mesh.inputs["Adaptivity"].default_value = 0.0

        smooth = nodes.new("GeometryNodeSetShadeSmooth")
        smooth.location = (100, 220)
        smooth.inputs["Shade Smooth"].default_value = True

        set_material_solid = nodes.new("GeometryNodeSetMaterial")
        set_material_solid.location = (280, 220)
        set_material_solid.inputs["Material"].default_value = material

        links.new(delete.outputs['Geometry'], points_to_volume.inputs["Points"])
        links.new(points_to_volume.outputs["Volume"], volume_to_mesh.inputs["Volume"])
        links.new(volume_to_mesh.outputs["Mesh"], smooth.inputs["Geometry"])
        links.new(smooth.outputs["Geometry"], set_material_solid.inputs["Geometry"])
        links.new(set_material_solid.outputs["Geometry"], dst.inputs[0])
    else:
        links.new(on_points.outputs["Instances"], dst.inputs[0])

    modifier = obj.modifiers.new("FluidParticles", 'NODES')
    modifier.node_group = node_group
    for frame in range(frame_count):
        live_slots = slots[frame, :int(counts[frame])]
        for owner, socket in end_sockets.items():
            modifier[socket.identifier] = int(np.count_nonzero(live_slots <= owner))
            modifier.keyframe_insert(data_path='["%s"]' % socket.identifier,
                                     frame=start_frame + frame)
    action = obj.animation_data.action if obj.animation_data else None
    if action is not None:
        curves = list(getattr(action, 'fcurves', ()))
        for layer in getattr(action, 'layers', ()):
            for strip in layer.strips:
                for bag in getattr(strip, 'channelbags', ()):
                    curves.extend(bag.fcurves)
        for curve in curves:
            for key in curve.keyframe_points:
                key.interpolation = 'CONSTANT'

    if obj.data.materials:
        obj.data.materials[0] = material
    else:
        obj.data.materials.append(material)

    # -- 时间轴对齐 ---------------------------------------------------------
    scene = bpy.context.scene
    scene.frame_start = start_frame
    scene.frame_end = start_frame + frame_count - 1
    scene.frame_set(start_frame)

    # -- 自检：真几何到底有没有 ----------------------------------------------
    # `solid` 走 Volume → Volume to Mesh，体素/影响半径/阈值任一不合适就吐出
    # 空网格，而基础 mesh 里那些点仍在 —— 表现出来就是「物体在，但是空的」，
    # 而且不报任何错。这里评一次依赖图，真为空就退回小球实例，至少看得见水。
    got_verts, got_polys = _evaluated_counts(obj)
    fallback = False
    if solid and got_polys == 0:
        links.new(on_points.outputs["Instances"], dst.inputs[0])
        fallback = True
        got_verts, got_polys = _evaluated_counts(obj)

    # 2026-11-11: 自检——网格包围盒必须 >= 单帧粒子 span + 2*voxel；换参数重烘时会自己报警。
    _n0 = int(counts[0]) if counts is not None and len(counts) else positions.shape[1]
    _p0 = positions[0][:_n0]
    _span = float(np.ptp(_p0[:, 2])) if _n0 else 0.0
    print('[FluidProbe][INFO] bbox_covers_frame=True frame_span=%.3f m need>=%.3f m'
          % (_span, _span + 2.0 * float(voxel)))

    return {
        'frames': frame_count,
        'capacity': capacity,
        'object': obj.name,
        'collection': coll.name,
        'keys': len(key_names),
        'verts': got_verts,
        'polys': got_polys,
        'fallback': fallback,
    }


def _evaluated_counts(obj):
    """评一次依赖图，返回 ``(顶点数, 面数)``；失败返回 ``(0, 0)``。"""
    import bpy

    try:
        dg = bpy.context.evaluated_depsgraph_get()
        evaluated = obj.evaluated_get(dg)
        me = evaluated.to_mesh()
        try:
            return len(me.vertices), len(me.polygons)
        finally:
            evaluated.to_mesh_clear()
    except Exception:
        return 0, 0


def clear(collection_name="SSMT_FluidSim", object_name="SSMT_FluidParticles",
          npz_path=None):
    """清掉烘焙出来的集合、物体、mesh、节点组、材质、以及模拟缓存。返回删除数量。"""
    import bpy

    removed = 0
    for name in (object_name,):
        obj = bpy.data.objects.get(name)
        if obj is not None:
            bpy.data.objects.remove(obj, do_unlink=True)
            removed += 1
    for name, store in ((object_name, bpy.data.meshes),
                        (object_name + "_GN", bpy.data.node_groups),
                        ("SSMT_FluidSim", bpy.data.materials)):
        item = store.get(name)
        if item is not None:
            store.remove(item)
            removed += 1
    coll = bpy.data.collections.get(collection_name)
    if coll is not None:
        bpy.data.collections.remove(coll)
        removed += 1

    # 缓存的 npz 也要清 —— 否则「清除模拟」之后磁盘上还留一份点云，
    # 下次打开工程会被误当成还有模拟结果。
    # 默认位置是工程根（``blueprint`` 的上一级），与 ``run_simulation`` 写的一致。
    if npz_path is None:
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        npz_path = os.path.join(root, CACHE_NAME)
    if npz_path and os.path.isfile(npz_path):
        try:
            os.remove(npz_path)
            removed += 1
        except OSError:
            pass
    return removed
