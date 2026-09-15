"""M1-3：软体物理的运行期 INI 文本生成（纯字符串，无 bpy）。

导出器需要往 mod 的 ini 里插三块东西：

1. **资源声明** —— 邻接表 / 边表 / 跨帧状态 / 输出顶点缓冲
2. **求解器段** —— `[CustomShaderSB_Solve_<ib>]`：绑 t24/t80/t81/u5/u6 + Dispatch
3. **deform 挂载** —— 在该部件的顶点覆写段里：先声明持久状态、跑求解、
   再把 `vb0` 换成本次解算的结果

这三块都是纯文本，所以这一层可以完全离线验证（见 tests/test_softbody_runtime_ini.py），
不必等运行期着色器落地。

关于缓冲布局的取舍：状态是**每顶点 2 条 float4**（当前位置 + 上一步位置），
Verlet 用它推下一步；一条 float4 存不下，所以 array = 顶点数 × 2。
"""


def _mod_buffer_name(draw_ib: str, kind: str) -> str:
    return f"Meshes/sb_{kind}_{draw_ib}.buf"


def build_resource_lines(draw_ib: str, vertex_count: int, edge_count: int):
    """返回资源声明段的行列表。

    - 邻接/边表：`R32G32B32A32_UINT` 格式化缓冲（与 vg_map 同一套做法，
      避免空声明让 SRV 视图格式不受控、读出垃圾）
    - 状态：`RWStructuredBuffer<float4>`，array = 顶点数 × 2
    - 输出：`RWStructuredBuffer`，stride 40（与 vb0 同布局）
    """
    lines = []
    for kind, count in (("adj", vertex_count), ("edges", edge_count)):
        lines.append(f"[ResourceSB_{kind.capitalize()}_{draw_ib}]")
        lines.append("type = Buffer")
        lines.append("format = R32G32B32A32_UINT")
        lines.append("filename = " + _mod_buffer_name(draw_ib, kind))
        lines.append("")

    lines.append(f"[ResourceSB_State_{draw_ib}]")
    lines.append("type = RWStructuredBuffer")
    lines.append("stride = 16")
    # 每顶点每槽 3 条 float4 × 2 个出现次槽（基座 / 当前位置 / 上一步位置）
    #   = 顶点数 × 6。分槽原因见 sb_solve.hlsl 头部：deform 一帧跑不止一次，
    #   单槽会让第二次读到第一次刚写的数据 → 只有部分顶点被更新（实机"拉面筋"）。
    lines.append(f"array = {int(vertex_count) * 6}")
    lines.append("")

    # 求解器写入目标：**逐字照抄拖拽系统的 ResourceDragJiggleTempVB0**（3555 行）
    # —— 裸 `type = RWBuffer`，不写 stride/format/array。
    # 视图类型由着色器声明决定（RWStructuredBuffer<VertexAttributes>，40 字节），
    # 实操时再把它**别名到 IA 的 vb0**（见 build_solver_section_lines），于是
    # "写回"就是写回游戏当前绑定的那个顶点缓冲。
    # 2026-09-14：之前我写成 `type = RWStructuredBuffer, stride = 40, array = N`
    # 并且去拷我自己的文件资源 —— 实机"碎一地"，就是这两处偏离。
    lines.append(f"[ResourceSB_VB_{draw_ib}]")
    lines.append("type = RWBuffer")
    lines.append("")

    # 基座几何的**只读副本**（2026-09-14 关键修复）：
    # 就地写回会把 mod 的 Position 缓冲改掉，于是"第二次 deform 从 vb0 抄基座"
    # 抄到的已经是第一次改过的数据 ⇒ 两次的基座不同 ⇒ 一半顶点 +0.5、一半 +1.0
    # = 实机"面筋人"。基座必须来自一份**永不写入**的副本。
    lines.append(f"[ResourceSB_Base_{draw_ib}]")
    lines.append("type = Buffer")
    # 方案 A：格式化缓冲 —— 每顶点 10 个 float（40 字节），接口完全显式。
    # （`stride = 40` 那种写法实测读出来是全 0：3Dmigoto 会建 raw 视图，
    #   与着色器里的结构化/浮点接口对不上。）
    lines.append("format = R32_FLOAT")
    lines.append(f"array = {int(vertex_count) * 10}")
    lines.append(f"filename = {_mod_buffer_name(draw_ib, 'base')}")
    lines.append("")
    return lines


def dispatch_groups(vertex_count: int, threads: int = 64) -> int:
    """向上取整的线程组数（与着色器里的 [numthreads(64,1,1)] 对齐）。"""
    if vertex_count <= 0:
        return 0
    return (int(vertex_count) + threads - 1) // threads


def build_param_lines(params) -> list:
    """把面板参数写成 IniParams。

    仓库老教训（notes/50）：**一个分量一行、一行一个值** —— 写成
    `x80 = 0.15 0.5` 这种"一行多值"会让引擎把整行作废，参数永远保持 0。
    """
    p = dict(params or {})
    shape = float(p.get("shape_stiffness", 0.15))
    edge = float(p.get("edge_stiffness", 0.5))
    damping = float(p.get("damping", 0.94))
    dt = float(p.get("dt", 1.0 / 60.0))
    gx, gy, gz = p.get("gravity_dir", (0.0, -1.0, 0.0))
    gravity = float(p.get("gravity_strength", 0.0))
    iterations = int(p.get("iterations", 2))
    debug_marker = 1 if p.get("debug_marker") else 0
    return [
        "; 软体解算参数（一个分量一行，见 notes/50）",
        f"x80 = {shape:.6f}",
        f"y80 = {edge:.6f}",
        f"z80 = {damping:.6f}",
        f"w80 = {dt:.7f}",
        f"x81 = {float(gx):.4f}",
        f"y81 = {float(gy):.4f}",
        f"z81 = {float(gz):.4f}",
        f"w81 = {gravity:.4f}",
        f"x82 = {iterations}",
        f"x83 = {debug_marker}",
        # ⚠️ 这里**故意不写 x84**（出现次槽）。2026-09-14 的坑：
        # 槽位由挂钩在 `run =` **之前**按 `$zz_ms_occ_*` 设定；如果这里再写一行
        # `x84 = 0`，它是在解算段**内部**执行的，会把挂钩选的槽覆盖掉 ——
        # 两次 deform 永远都用槽 0，分槽等于没做（实机"面筋人"）。
        # 参数行只管"物理参数"，不管槽位。
    ]


def build_solver_section_lines(draw_ib: str, vertex_count: int, params=None):
    """求解器段：先把参数写进 IniParams，再绑资源，最后 Dispatch。"""
    groups = dispatch_groups(vertex_count)
    if groups <= 0:
        return []
    # 诊断模式（2026-09-14 修正）：**不再写回 mod 自己的 Position 缓冲**。
    # 旧设计是就地累加（每帧 +0.5），几帧就把模型推飞 —— 实机表现为"碎一地"，
    # 反而掩盖了"着色器到底跑没跑"这个真正要看的信号。
    # 现在诊断只改"着色器写什么"（见 sb_solve.hlsl 的 SB_DEBUG_MARKER），
    # 走的仍是下面这条**非破坏性**路径：写自己的输出缓冲 + 换绑 vb0。
    # 于是：标记开 ⇒ 模型整体平移（一眼可见）；标记关 ⇒ 正常物理。
    # 绑定写法**逐行照抄拖拽系统那套已实测跑通的**（node_postprocess_in
    # draginteraction.py 3888~3896 行），并且**不换绑 vb0**：
    #   cs-t24 = vb0                             ← 直接读 IA 当前的 vb0
    #   cs-u5  = copy Resource<ib>Position        ← 拿 mod 自己那个顶点缓冲的可写副本
    #   Dispatch ...
    #   vb0 = null                               ← Dispatch 期间解绑，避免读写冲突
    #   Resource<ib>Position = copy cs-u5         ← 就地写回（基座存在状态里，不漂移）
    # 2026-09-14 的教训：先用 `ref` 绑自己的 RW 缓冲、再把它当 vb0 绑回 IA —— 实机碎一地。
    # 根因是"RWStructuredBuffer 当顶点缓冲"在这个 fork 上拿不到合法 VB 视图；
    # 拖拽系统从不那么做。
    return (
        [
            f"[CustomShaderSB_Solve_{draw_ib}]",
            # ★ 2026-09-14：这三行 flags 是**决定性的**（照抄合并骨架 attach /
            # 拖拽 jiggle 段）。日志实证：我的段没写 flags，结果
            #   * 注册正常、资源零报错
            #   * 但整场只 dispatch 了 4 次（同段里别人的 CS 跑了 4446 次）
            # 原因就是缺 `skip_validation`：3Dmigoto 在 dispatch 前做资源校验，
            # 校验不过就**静默跳过**这次 dispatch。加上之后就照发。
            "flags = optimization_level3 all_resources_bound skip_validation",
            "cs = ./res/sb_solve.hlsl",
        ]
        + build_param_lines(params)
        + [
        # 2026-09-14 方案 A 定稿：
        #   基座 = **格式化缓冲** ResourceSB_Base（只读，永不写入）
        #   写入 = 拖拽系统同款：别名 vb0 → copy → Dispatch → 写回 → 渲染换绑
        # 这样"读"和"写"彻底解耦：读的那份永远干净，写的那份用游戏已建好的视图。
        f"ResourceSB_VB_{draw_ib} = vb0",
        f"cs-t24 = ref ResourceSB_Base_{draw_ib}",
        f"cs-t80 = ref ResourceSB_Adj_{draw_ib}",
        f"cs-t81 = ref ResourceSB_Edges_{draw_ib}",
        f"cs-u5 = copy ResourceSB_VB_{draw_ib}",
        f"cs-u6 = ref ResourceSB_State_{draw_ib}",
        f"Dispatch = {groups}, 1, 1",
        "vb0 = null",
        f"ResourceSB_VB_{draw_ib} = copy cs-u5",
        "cs-u5 = null",
        "cs-u6 = null",
        "",
        ]
    )


def build_deform_hook_lines(draw_ib: str):
    """deform 顶点覆写段里要插的行：跑求解，然后把 vb0 换成本次解算结果。

    语义与拖拽系统一致：先 `handling = skip` 吃掉游戏原 draw，再自己 draw；
    区别是这里的 vb0 来自求解器输出，而不是"原顶点 + 偏移"。

    **2026-09-14 加的保底（关键）**：先把原几何拷进输出缓冲，再跑解算。
    这样即使解算着色器因为任何原因没跑（编译失败、绑定错、被优化掉），
    vb0 里躺着的仍然是**正确几何** → 顶多"没效果"，**绝不会把模型炸成碎片**。
    这条同时是诊断：如果加了这行画面仍然炸，说明解算**确实在跑**、而且写进了
    垃圾（问题在读取/写入布局）；如果画面恢复正常，说明解算**根本没跑起来**。
    """
    return build_deform_hook_lines_for(draw_ib, marker=False)


def build_deform_hook_lines_for(draw_ib: str, marker: bool = False, occ_var: str = ""):
    """带诊断开关的挂钩生成。

    `occ_var`：该部件在 ini 里的**出现次变量名**（如 `$zz_ms_occ_11`）。
    给了就在解算前按它选槽（1 → 槽 0，2 → 槽 1）—— 同一个部件的 deform
    一帧跑不止一次，两次必须各用各的状态，否则第二次读到第一次刚写的
    数据 → 只有部分顶点被更新（实机"拉面筋"）。

    必须写成 `if 变量 == 1 / else` 两行赋值，**不能**写 `x84 = 变量 - 1`：
    仓库教训（notes/50）—— IniParams 一行只能给一个分量一个值。
    """
    slot_lines = []
    if occ_var:
        slot_lines = [
            f"if {occ_var} == 1",
            "    x84 = 0",
            "else",
            "    x84 = 1",
            "endif",
        ]
    # 2026-09-14 定稿（照抄拖拽系统 4703~4712 行的挂钩形态）：
    #   ① run = 解算
    #   ② vb0 = <那个资源>   ← ★ 必须换绑！
    # 拖拽系统里这一行是 `vb0 = ResourceDragJiggleTempVB0_{cn}_{ns}`，
    # 而那个资源在 CS 里被 `= vb0` 别名到当前顶点缓冲、并被 `copy cs-u5` 写回。
    # 我上一版误以为"RW 缓冲不能当 vb0"而删掉了这一行 —— 结果解算白跑：
    # 写回落在资源变量上，而渲染仍用旧绑定（实机表现就是"碎一地/没变化"）。
    return [
        "; 软体物理 M1：按出现次选槽 → 解算 → 换绑 vb0 到解算输出（拖拽系统同款形态）",
    ] + slot_lines + [
        f"run = CustomShaderSB_Solve_{draw_ib}",
        f"vb0 = ResourceSB_VB_{draw_ib}",
    ]
