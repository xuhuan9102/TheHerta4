import bpy
import hashlib
import os


_blueprint_enum_items_cache = []
_workspace_safe_identifier_map: dict[str, str] = {}


def _make_safe_identifier(name: str) -> str:
    return hashlib.md5(name.encode("utf-8")).hexdigest()[:16]


def resolve_workspace_safe_identifier(identifier: str) -> str:
    if not _workspace_safe_identifier_map:
        _get_workspace_enum_items(None, None)
    return _workspace_safe_identifier_map.get(identifier, identifier)


def _get_blueprint_enum_items(self, context):
    global _blueprint_enum_items_cache

    try:
        from ..blueprint.export_helper import BlueprintExportHelper
        items = BlueprintExportHelper.get_blueprint_enum_items(context=context)
    except Exception:
        # items 回调抛异常时 Blender 只会拿到空列表：下拉框会塌成「当前没有蓝图」，
        # 已存的枚举序号同时失效（刷 "current value ... matches no enum" 警告、
        # 选择显示空白、删除/重命名失去目标）。所以失败时保留上一次成功算出的
        # 列表，不让一次瞬时异常把用户的选择清掉。
        items = _blueprint_enum_items_cache

    if not items:
        items = [
            ("__NONE__", "当前没有蓝图", "当前没有可选蓝图，请先打开蓝图界面或执行一键导入"),
        ]

    _blueprint_enum_items_cache = list(items)

    return _blueprint_enum_items_cache


def _get_workspace_enum_items(self, context):
    try:
        from .global_config import GlobalConfig

        GlobalConfig.read_from_main_json_ssmt4()
        workspace_root = GlobalConfig.path_current_game_total_workspace_folder()
        if not workspace_root or not os.path.isdir(workspace_root):
            _workspace_safe_identifier_map.clear()
            return [("", "当前没有工作空间", "当前游戏配置下未找到可用工作空间")]

        workspace_names = sorted(
            [entry.name for entry in os.scandir(workspace_root) if entry.is_dir()]
        )
        if not workspace_names:
            _workspace_safe_identifier_map.clear()
            return [("", "当前没有工作空间", "当前游戏配置下未找到可用工作空间")]

        _workspace_safe_identifier_map.clear()
        items = []
        for name in workspace_names:
            identifier = _make_safe_identifier(name)
            _workspace_safe_identifier_map[identifier] = name
            items.append((identifier, name, ""))
        return items
    except Exception:
        _workspace_safe_identifier_map.clear()
        return [("", "当前没有工作空间", "当前游戏配置下未找到可用工作空间")]


def _sanitize_specific_workspace_name(dummy=None, *args):
    """把已失效的「指定工作空间」重置为当前列表的第一个有效项。

    背景：.blend 里保存的是【旧游戏配置】下的工作空间标识；用户切换游戏配置后，
    该标识不在新列表里，Blender 每次读取这个枚举都会打印
        current value '...' matches no enum in 'GlobalProterties', '', 'specific_workspace_name'
    （实测一次启动能刷几十条）。

    必须在 items 回调【之外】执行：若在 _get_workspace_enum_items 内部读
    self.specific_workspace_name，会无限递归（实测单次读取触发近 3000 次回调，
    5 次读取近 8000 次，直接把 UI 拖死）。
    """
    try:
        scenes = list(bpy.data.scenes)
    except Exception:
        return  # 注册早期 bpy.data 还是 _RestrictData
    for scene in scenes:
        props = getattr(scene, "global_properties", None)
        if props is None:
            continue
        try:
            items = _get_workspace_enum_items(props, bpy.context)
        except Exception:
            continue
        valid = [identifier for identifier, _label, _desc in items]
        if not valid:
            continue
        try:
            current = props.specific_workspace_name
        except Exception:
            continue
        if current in valid:
            continue
        try:
            props.specific_workspace_name = valid[0]
            label = next((item[1] for item in items if item[0] == valid[0]), valid[0])
            print(f"[TheHerta4] 「指定工作空间」旧值 '{current}' 在当前配置下已失效，已重置为 '{label}'")
        except Exception as exc:
            print(f"[TheHerta4] 重置「指定工作空间」失败: {exc}")


def _default_parallel_instance_count() -> int:
    cpu_count = os.cpu_count() or 4
    return max(1, min(4, cpu_count))


def _update_parallel_preprocess_toggle(self, context):
    if self.enable_parallel_preprocess and not self.parallel_blender_executable:
        self.parallel_blender_executable = bpy.app.binary_path or ""


class GlobalProterties(bpy.types.PropertyGroup):
    object_swap_variable_counter: bpy.props.IntProperty(
        name="Object Swap Variable Counter",
        default=0,
        min=0,
        options={'HIDDEN'},
    ) # type: ignore

    allocated_variable_names_csv: bpy.props.StringProperty(
        name="Allocated Variable Names",
        default="",
        options={'HIDDEN'},
    ) # type: ignore

    selected_blueprint_name: bpy.props.EnumProperty(
        name="当前蓝图",
        description="选择要打开或快捷生成 Mod 的蓝图",
        items=_get_blueprint_enum_items,
    ) # type: ignore

    open_mod_folder_after_generate_mod: bpy.props.BoolProperty(
        name="生成后打开Mod文件夹",
        description="勾选后，在生成Mod完成后自动打开Mod文件夹",
        default=True,
    ) # type: ignore

    background_export_enabled: bpy.props.BoolProperty(
        name="后台导出（不阻塞界面）",
        description=(
            "默认开启：生成 Mod 会先保存一份工程副本，再交给独立的干净 Blender 后台实例"
            "执行完整导出（各轮次 + 后处理），主界面不阻塞，状态栏显示进度，按 ESC 可取消；"
            "取消勾选则沿用原有的同步导出（导出期间 Blender 会卡住直到结束）"
        ),
        default=True,
    ) # type: ignore

    zzz_use_slot_fix: bpy.props.BoolProperty(
        name="槽位风格贴图使用SlotFix技术",
        description="仅适用于槽位风格贴图，勾选后，特定名称标记的贴图将使用SlotFix风格，能一定程度上解决槽位风格贴图跨槽位的问题，跨Pixel槽位指的是在前一个DrawCall中是ps-t3但是下一个DrawCall变为ps-t5这种情况，但由于负责维护的人也在偷懒所以并不可靠",
        default=True,
    ) # type: ignore

    gimi_use_orfix: bpy.props.BoolProperty(
        name="槽位风格贴图使用ORFix",
        description="勾选后，在使用槽位风格贴图标记时，如果偷懒不想手动维护由于贴图槽位变化导致的贴图损坏问题，可以勾选此选项将问题交给ORFix维护者来解决，仅GIMI可用\n注意，如果你不懂ORFix和NNFix的原理，请不要取消勾选，取消勾选会严格按照贴图标记来执行贴图部分ini生成，默认你会在ini中自行写判断语句修复来替代实现ORFix和NNFix的功能",
        default=True,
    ) # type: ignore

    forbid_auto_texture_ini: bpy.props.BoolProperty(
        name="禁止自动贴图流程",
        description="生成Mod时禁止生成贴图相关ini部分",
        default=False,
    ) # type: ignore

    use_rabbitfx_slot: bpy.props.BoolProperty(
        name="使用RabbitFX槽位风格",
        description="勾选后，使用RabbitFX槽位风格生成贴图相关ini部分",
        default=False,
    ) # type: ignore

    efmi_pass_mirror_enabled: bpy.props.BoolProperty(
        name="多pass贴图槽位镜像（修颜色偏差）",
        description="默认关（需手动开启）：开启后导出时按工作空间缓存/抓帧推导，把槽位贴图镜像到 G-buffer(t0/t1) 与前向第二层(t12等) 槽位，并注册 pass 标签——修自定义贴图只覆盖第一层、其余 pass 仍读原图导致的颜色偏差。默认关＝回到旧行为（只绑标记槽，不发射镜像赋值与 [ShaderOverride_PassMirror...] 标签段）；导入侧也不再写回 Config/PassLayouts.json。",
        default=False,
    ) # type: ignore

    recalculate_tangent: bpy.props.BoolProperty(
        name="向量归一化法线存入TANGENT(全局)",
        description="轮廓线专用：将平滑法线写入 TANGENT，而不是生成标准切线空间。使用法线贴图或标准 TBN 光照时不要开启；“重新计算标准切线(TBN)”启用时本选项不会生效。",
        default=False,
    ) # type: ignore

    # ARCH-06（描述收窄）：原描述承诺"导出时使用 TEXCOORD.xy（不存在时使用活动 UV）
    # 重新计算 Blender 标准切线，并忽略导入时保存的旧 TANGENT"——该承诺在代码里
    # **没有对应实现**：标准 TBN 本来就是导出默认路径（utils/export_utils.py:102 每次
    # 导出前无条件 mesh.calc_tangents(uvmap=uvmap_name)），而"导入时保存的旧
    # TANGENT"在导入侧就被丢弃（common/mesh_create_helper.py:188-189 TANGENT →
    # pass，不写入 mesh）。本开关的**唯一**作用是两处 early-return
    # （common/obj_buffer_helper.py:988 / :1059 average_normal_tangent_xxmi）：
    # 抑制"把平滑法线覆盖进 TANGENT"的轮廓线修复路径。描述已按真实行为收窄。
    recalculate_tangent_basis: bpy.props.BoolProperty(
        name="重新计算标准切线(TBN)",
        description="保留 Blender 标准切线空间：抑制“向量归一化法线存入TANGENT”的轮廓线覆盖，导出沿用每次导出重建的标准切线（不使用导入数据里的旧 TANGENT）。使用法线贴图或标准 TBN 光照时开启；仅在同时勾选了“向量归一化法线存入TANGENT”或物体级重算标记时才产生差异。",
        default=False,
    ) # type: ignore

    recalculate_color: bpy.props.BoolProperty(
        name="算术平均归一化法线存入COLOR(全局)",
        description="已由「COLOR 写入方式」取代：AVERAGE / PER_LOOP 都算开启。保留字段只为兼容既有 .blend 与调用方，不再出现在 UI 上",
        default=False,
        options={'HIDDEN'},
    ) # type: ignore

    # COLOR 写入方式：关 / 按位置算术平均 / 逐 loop 直写。
    # 合并了原来的两个布尔开关（"算术平均归一化法线存入COLOR(全局)" 与 "COLOR 逐loop直写"）——
    # 它们本来就是同一件事的两种模式，同时显示会让人以为要各勾一次。
    recalculate_color_mode: bpy.props.EnumProperty(
        name="COLOR 写入方式",
        description="关闭 = 只处理右键标记了 3DMigoto:RecalculateCOLOR 的物体；按位置算术平均 = 同位置法线求平均后归一化（实机验证口径）；逐loop直写 = 每个顶点直接用自己的 NORMAL（头发等带分裂法线/硬边的网格）",
        items=[
            ("OFF", "关闭(只按物体标记)", "不全局重算，只处理标记了 3DMigoto:RecalculateCOLOR 的物体"),
            ("AVERAGE", "按位置算术平均", "同位置法线算术平均 + 归一化，写 COLOR；实机验证口径（对游戏原值中位误差 0.37°）"),
            ("PER_LOOP", "逐loop直写", "每个导出顶点直接用自己的 NORMAL；适合头发等带分裂法线/硬边的网格"),
        ],
        default="AVERAGE",
    ) # type: ignore

    # 描边宽度：唯一旋钮，值就是写进 COLOR.a 的 alpha。游戏侧 alpha 是逐顶点描边宽度遮罩
    # （外扩 = 归一化法线方向 × 常量 × COLOR.a），游戏自带数据实测中位 ≈ 118/255 ≈ 0.46，
    # 所以 0.5 = 游戏原版宽度、0.25 = 细一半、0 = 该模型没有描边。
    # 之前这里还有一个 "COLOR alpha 基准" 叠加相乘，两个旋钮会让 0.5×0.5 变成四分之一，
    # 已合并成这一个（物体级覆盖仍用自定义属性 3DMigoto:RecalculateCOLORWidth）。
    recalculate_color_width: bpy.props.FloatProperty(
        name="描边宽度(COLOR alpha)",
        description="写进 COLOR.a 的值：游戏里 alpha 是逐顶点的描边宽度遮罩（外扩 = 归一化法线方向 × 常量 × alpha）。0.5 = 游戏原版宽度，0.25 = 细一半，0 = 该模型没有描边。只对「COLOR 写入方式」非关闭或物体标记了 3DMigoto:RecalculateCOLOR 的模型生效；单个物体可用自定义属性 3DMigoto:RecalculateCOLORWidth 覆盖本值",
        default=0.5,
        min=0.0,
        max=1.0,
    ) # type: ignore

    use_specific_generate_mod_folder_path: bpy.props.BoolProperty(
        name="生成Mod到指定的文件夹中",
        description="勾选后将生成Mod到你指定的文件夹中",
        default=False,
    ) # type: ignore

    generate_mod_folder_path: bpy.props.StringProperty(
        name="生成Mod文件夹路径",
        description="选择的生成Mod的文件夹路径",
        default="",
        subtype='DIR_PATH',
    ) # type: ignore

    workspace_source_mode: bpy.props.EnumProperty(
        name="工作空间模式",
        description="控制当前使用的工作空间来源",
        items=[
            ("SYNC", "同步SSMT选择", "使用 SSMT 配置文件中当前同步的工作空间"),
            ("SPECIFIC", "使用指定工作空间", "从当前游戏配置下的工作空间列表中手动选择"),
            ("CUSTOM", "使用自定义目录", "直接使用你指定的工作空间目录"),
            ("VELO", "velo工作空间", "使用 Velo Tools 当前组件集合"),
        ],
        default="SYNC",
    ) # type: ignore

    specific_workspace_name: bpy.props.EnumProperty(
        name="指定工作空间",
        description="当前游戏配置下可选的工作空间列表",
        items=_get_workspace_enum_items,
    ) # type: ignore

    custom_workspace_folder_path: bpy.props.StringProperty(
        name="自定义工作空间目录",
        description="手动指定工作空间目录路径",
        default="",
        subtype='DIR_PATH',
    ) # type: ignore

    import_merged_vgmap: bpy.props.BoolProperty(
        name="使用融合统一顶点组",
        description="导入时是否导入融合后的顶点组 (Unreal的合并顶点组技术会用到)，一般鸣潮Mod需要勾选来降低制作Mod的复杂度；ZZMI/EFMI 模式下同时作为骨骼合并（Merged Skeleton）开关：勾选 = 导入全局顶点组、导出走合并骨架；不勾选 = 完全维持原路线",
        default=True,
    ) # type: ignore

    cross_group_merged_vgmap_test: bpy.props.BoolProperty(
        name="跨组融合统一顶点组测试",
        description="【实验开关，仅 ZZMI】跨 SkeletonGroup 的融合统一顶点组测试：允许把对象变换（渲染 vs-cb1 对象空间）不同的部件合并进同一个对象。与已验证可用的合并骨骼（使用融合统一顶点组）完全分离，开启不会改变后者的既有行为；跨组部件需要一个逐帧的对象空间换算，未完成前本开关不参与导入/导出决策",
        default=False,
    ) # type: ignore

    zzmi_merged_redirect_enabled: bpy.props.BoolProperty(
        name="启用合并网格自动重定向（实验）",
        description=(
            "ZZMI 合并骨架开关，默认开启：合并网格改用跨 DrawIB 的 RedirectSO "
            "自动重定向，可以挂在任意 DrawIB 上。关闭后保留每个合并物体的原始 "
            "IB/Blend 输入布局，在组内宿主完成当帧重放。注意：自动重定向在部分"
            "游戏帧序下会丢失整块几何，遇到合并后缺块/错位时先关掉本项对照。"
        ),
        default=True,
    ) # type: ignore

    efmi_lod_group_projection: bpy.props.BoolProperty(
        name="EFMI LOD 分组投影",
        description="EFMI 多 LOD 时，以 LOD0 的去重分组关系约束 LOD1：LOD0 已合并的对应组在 LOD1 也合并、未合并的组不互并；两侧仍使用互不重叠的独立槽位段。开启时还会过滤几何未匹配的 LOD1 物体并自动创建匹配链；关闭后双侧完全独立去重、不过滤、不建链",
        default=True,
    ) # type: ignore

    efmi_lod_group_dedup: bpy.props.BoolProperty(
        name="EFMI 顶点组去重",
        description="EFMI 骨骼合并时是否执行权重扩散去重：开启时相同骨骼跨组件共享槽位（节省骨骼槽）；关闭时每根骨骼独占槽位（恒等映射，不做任何合并）。关闭适用于确认去重误并/偏移时诊断与回滚",
        default=True,
    ) # type: ignore

    ignore_muted_shape_keys: bpy.props.BoolProperty(
        name="忽略未启用的形态键",
        description="勾选此项后，未勾选启用的形态键在生成Mod时会被忽略，勾选的形态键会参与生成Mod",
        default=True,
    ) # type: ignore

    apply_all_modifiers: bpy.props.BoolProperty(
        name="应用所有修改器",
        description="勾选此项后，生成Mod之前会自动对物体应用所有修改器",
        default=False,
    ) # type: ignore

    import_skip_empty_vertex_groups: bpy.props.BoolProperty(
        name="跳过空顶点组",
        description="勾选此项后，导入时会跳过空的顶点组",
        default=True,
    ) # type: ignore

    export_add_missing_vertex_groups: bpy.props.BoolProperty(
        name="导出时添加缺失顶点组",
        description="勾选此项后，生成Mod时会自动重新排列并填补数字顶点组间的间隙空缺",
        default=True,
    ) # type: ignore

    ignore_texture_alpha: bpy.props.BoolProperty(
        name="导入贴图时忽略透明度通道",
        description='开启后，一键导入透明材质时，贴图的 Alpha 模式会被设为"无"，使透明度通道始终输出 1（不透明），且不破坏着色器连接结构',
        default=False,
    ) # type: ignore

    enable_non_mirror_workflow: bpy.props.BoolProperty(
        name="非镜像工作流",
        description="启用后，导入时会先将模型镜像、应用变换并翻转面；导出前处理时会对副本再次执行同样操作以恢复原始朝向",
        default=True,
    ) # type: ignore

    import_materials_by_submesh_mark: bpy.props.BoolProperty(
        name="按SSMT标记添加贴图材质",
        description="开启后，一键导入时按 SSMT 子网格里标记的贴图类型（SubmeshJson 的 TextureMarkUpInfoList，与导出 mod 用的是同一份标记）逐类型建立独立材质，材质名形如 DiffuseMap_网格名；标记了才建，没标记的类型不建。读不到标记时仍按原逻辑按文件名搜索",
        default=True,
    ) # type: ignore

    enable_preprocess_cache: bpy.props.BoolProperty(
        name="启用前处理缓存",
        description="启用后，前处理结果会被缓存到本地文件。当物体数据未变化时，下次导出将直接使用缓存，减少重复计算",
        default=True,
    ) # type: ignore

    enable_parallel_preprocess: bpy.props.BoolProperty(
        name="启用并行前处理",
        description="通过多个独立 Blender 子进程并行执行未命中的前处理任务，以提升大量物体场景下的处理速度",
        default=False,
        update=_update_parallel_preprocess_toggle,
    ) # type: ignore

    enable_parallel_export_rounds: bpy.props.BoolProperty(
        name="启用并行多轮导出",
        description="将多文件导出和形态键导出的中间轮次分发到多个 Blender 子进程并行执行。首轮与尾轮仍保留在主线程中",
        default=False,
        update=_update_parallel_preprocess_toggle,
    ) # type: ignore

    parallel_blender_executable: bpy.props.StringProperty(
        name="Blender 可执行文件",
        description="用于启动并行前处理子进程的 Blender 可执行文件路径。留空时会默认使用当前运行实例的路径",
        default="",
        subtype='FILE_PATH',
    ) # type: ignore

    parallel_preprocess_instances: bpy.props.IntProperty(
        name="并行实例数",
        description="用于并行前处理的最大 Blender 子进程数，同时作为任务分组上限",
        default=_default_parallel_instance_count(),
        min=1,
        soft_max=16,
    ) # type: ignore

    parallel_preprocess_timeout_seconds: bpy.props.IntProperty(
        name="单任务超时(秒)",
        description="单个并行前处理任务允许执行的最长时间，超时后会终止并报错",
        default=1800,
        min=30,
        soft_max=7200,
    ) # type: ignore

    parallel_preprocess_keep_temp_files: bpy.props.BoolProperty(
        name="保留临时文件",
        description="保留并行前处理生成的快照、任务清单与子工程结果，便于调试问题",
        default=False,
    ) # type: ignore

    prefix_quick_apply_to_object_name: bpy.props.BoolProperty(
        name="前缀写入物体名称",
        description="勾选时直接替换物体名称中的前缀；取消勾选时仅把前缀写入对应的物体信息节点",
        default=True,
    ) # type: ignore

    expand_import_quick_tools: bpy.props.BoolProperty(
        name="展开导入子菜单",
        description="展开后显示导入 FMT 与导入 SSMT 的快捷按钮",
        default=False,
    ) # type: ignore

    expand_prefix_quick_ops: bpy.props.BoolProperty(
        name="展开前缀快捷操作",
        description="展开前缀快捷操作面板",
        default=False,
    ) # type: ignore

    expand_preprocess_cache: bpy.props.BoolProperty(
        name="展开前处理缓存",
        description="展开前处理缓存面板",
        default=False,
    ) # type: ignore

    expand_parallel_processing: bpy.props.BoolProperty(
        name="展开并行处理",
        description="展开并行处理面板",
        default=False,
    ) # type: ignore

    deduplicate_POSITION: bpy.props.BoolProperty(
        name="位置数据",
        description="位置数据是否参与顶点去重判断",
        default=True,
    ) # type: ignore

    deduplicate_NORMAL: bpy.props.BoolProperty(
        name="法线数据",
        description="法线数据是否参与顶点去重判断",
        default=True,
    ) # type: ignore

    deduplicate_TANGENT: bpy.props.BoolProperty(
        name="切线数据",
        description="切线数据是否参与顶点去重判断",
        default=True,
    ) # type: ignore

    deduplicate_BINORMAL: bpy.props.BoolProperty(
        name="副法线数据",
        description="副法线数据是否参与顶点去重判断",
        default=True,
    ) # type: ignore

    deduplicate_TEXCOORD: bpy.props.BoolProperty(
        name="UV坐标数据",
        description="UV坐标数据是否参与顶点去重判断",
        default=True,
    ) # type: ignore

    deduplicate_COLOR: bpy.props.BoolProperty(
        name="顶点颜色数据",
        description="顶点颜色数据是否参与顶点去重判断",
        default=True,
    ) # type: ignore

    deduplicate_BLENDWEIGHT: bpy.props.BoolProperty(
        name="骨骼权重数据",
        description="骨骼权重数据是否参与顶点去重判断",
        default=True,
    ) # type: ignore

    deduplicate_BLENDINDICES: bpy.props.BoolProperty(
        name="骨骼索引数据",
        description="骨骼索引数据是否参与顶点去重判断",
        default=True,
    ) # type: ignore

    @classmethod
    def _instance(cls):
        return bpy.context.scene.global_properties

    @classmethod
    def _bool_attr(cls, name: str, default: bool = False) -> bool:
        instance = cls._instance()
        return bool(getattr(instance, name, default))

    @classmethod
    def open_mod_folder_after_generate_mod(cls):
        return cls._instance().open_mod_folder_after_generate_mod

    @classmethod
    def is_background_export_enabled(cls) -> bool:
        """后台导出开关（默认关＝沿用原有同步导出，行为完全不变）。

        注意：不能叫 ``background_export_enabled``——同名类方法会被 PropertyGroup
        注册时的 ``background_export_enabled`` BoolProperty 描述符覆盖，类上访问拿到
        的是描述符/bool，调用会 ``'bool' object is not callable``（2026-10-20 实测）。
        """
        return cls._bool_attr("background_export_enabled", False)

    @classmethod
    def zzz_use_slot_fix(cls):
        return cls._instance().zzz_use_slot_fix

    @classmethod
    def gimi_use_orfix(cls):
        return cls._instance().gimi_use_orfix

    @classmethod
    def forbid_auto_texture_ini(cls):
        return cls._instance().forbid_auto_texture_ini

    @classmethod
    def use_rabbitfx_slot(cls):
        return cls._instance().use_rabbitfx_slot

    @classmethod
    def efmi_pass_mirror_enabled(cls):
        """EFMI 多 pass 贴图槽位镜像总开关（默认关，需手动开启）。

        覆盖两处：导出侧 efmi.py 的镜像赋值/标签注册发射、导入侧
        ui_func_import_ssmt.py 的 Config/PassLayouts.json 写回。
        关闭（默认）时两处都不执行，即回到引入本开关前的旧行为。
        """
        return cls._bool_attr("efmi_pass_mirror_enabled", False)

    @classmethod
    def generate_branch_mod_gui(cls):
        try:
            from ..blueprint.export_helper import BlueprintExportHelper
            return BlueprintExportHelper.has_mod_panel_node()
        except Exception:
            return False

    @classmethod
    def generate_branch_mod_gui_flow_effect(cls):
        try:
            from ..blueprint.export_helper import BlueprintExportHelper
            return BlueprintExportHelper.is_mod_panel_flow_effect_enabled()
        except Exception:
            return False

    @classmethod
    def recalculate_tangent(cls):
        return cls._instance().recalculate_tangent

    @classmethod
    def recalculate_tangent_basis(cls):
        return cls._instance().recalculate_tangent_basis

    @classmethod
    def recalculate_color(cls):
        """是否全局重算 COLOR：由 recalculate_color_mode 决定（旧布尔字段仅作兼容）。"""
        mode = getattr(cls._instance(), "recalculate_color_mode", "AVERAGE")
        return mode in ("AVERAGE", "PER_LOOP")

    @classmethod
    def recalculate_color_mode(cls):
        return str(getattr(cls._instance(), "recalculate_color_mode", "AVERAGE") or "AVERAGE")

    @classmethod
    def recalculate_color_width(cls):
        return cls._instance().recalculate_color_width

    @classmethod
    def use_specific_generate_mod_folder_path(cls):
        return cls._instance().use_specific_generate_mod_folder_path

    @classmethod
    def generate_mod_folder_path(cls):
        return cls._instance().generate_mod_folder_path

    @classmethod
    def workspace_source_mode(cls):
        return cls._instance().workspace_source_mode

    @classmethod
    def specific_workspace_name(cls):
        return cls._instance().specific_workspace_name

    @classmethod
    def custom_workspace_folder_path(cls):
        return cls._instance().custom_workspace_folder_path

    @classmethod
    def import_merged_vgmap(cls):
        return cls._instance().import_merged_vgmap

    @classmethod
    def set_import_merged_vgmap(cls, value: bool):
        """显式切换合并 VGMap；预生成失败时用于保持导入与后续导出同一模式。"""
        setattr(cls._instance(), "import_merged_vgmap", bool(value))

    @classmethod
    def cross_group_merged_vgmap_test(cls):
        """跨组融合统一顶点组测试开关（仅 ZZMI，实验用，默认关闭）。

        与 `import_merged_vgmap`（已验证可用的合并骨骼）**完全分离**：互不影响，
        关闭本开关时任何既有行为都不变。跨组功能未实现前读取本开关不产生行为。
        """
        return cls._bool_attr("cross_group_merged_vgmap_test", False)

    @classmethod
    def set_cross_group_merged_vgmap_test(cls, value: bool):
        """显式切换跨组测试开关（实验/测试用）。"""
        setattr(cls._instance(), "cross_group_merged_vgmap_test", bool(value))

    @classmethod
    def zzmi_merged_redirect_enabled(cls) -> bool:
        """Whether to use the experimental cross-DrawIB RedirectSO path."""
        return cls._bool_attr("zzmi_merged_redirect_enabled", False)

    @classmethod
    def efmi_lod_group_projection(cls):
        return cls._bool_attr("efmi_lod_group_projection", True)

    @classmethod
    def efmi_lod_group_dedup(cls):
        """EFMI 骨骼合并是否执行顶点组去重（权重扩散合并）。

        开启 = 相同骨骼跨组件共享槽位；关闭 = 恒等映射、每根骨骼独占槽位。
        """
        return cls._bool_attr("efmi_lod_group_dedup", True)

    @classmethod
    def ignore_muted_shape_keys(cls):
        return cls._instance().ignore_muted_shape_keys

    @classmethod
    def apply_all_modifiers(cls):
        return cls._instance().apply_all_modifiers

    @classmethod
    def import_skip_empty_vertex_groups(cls):
        return cls._instance().import_skip_empty_vertex_groups

    @classmethod
    def export_add_missing_vertex_groups(cls):
        return cls._instance().export_add_missing_vertex_groups

    @classmethod
    def ignore_texture_alpha(cls):
        return cls._bool_attr("ignore_texture_alpha", False)

    @classmethod
    def set_ignore_texture_alpha(cls, value: bool):
        setattr(cls._instance(), "ignore_texture_alpha", bool(value))

    @classmethod
    def toggle_ignore_texture_alpha(cls) -> bool:
        """翻转 ignore_texture_alpha，返回翻转后的新值。"""
        instance = cls._instance()
        instance.ignore_texture_alpha = not bool(getattr(instance, "ignore_texture_alpha", False))
        return bool(instance.ignore_texture_alpha)

    @classmethod
    def enable_non_mirror_workflow(cls):
        return cls._instance().enable_non_mirror_workflow

    @classmethod
    def import_materials_by_submesh_mark(cls) -> bool:
        return cls._bool_attr("import_materials_by_submesh_mark", True)

    @classmethod
    def set_import_materials_by_submesh_mark(cls, value: bool):
        setattr(cls._instance(), "import_materials_by_submesh_mark", bool(value))

    @classmethod
    def toggle_import_materials_by_submesh_mark(cls) -> bool:
        """翻转 import_materials_by_submesh_mark，返回翻转后的新值。"""
        instance = cls._instance()
        instance.import_materials_by_submesh_mark = not bool(
            getattr(instance, "import_materials_by_submesh_mark", True)
        )
        return bool(instance.import_materials_by_submesh_mark)

    @classmethod
    def enable_preprocess_cache(cls):
        return cls._instance().enable_preprocess_cache

    @classmethod
    def enable_parallel_preprocess(cls):
        return cls._instance().enable_parallel_preprocess

    @classmethod
    def enable_parallel_export_rounds(cls):
        return cls._instance().enable_parallel_export_rounds

    @classmethod
    def parallel_blender_executable(cls):
        return cls._instance().parallel_blender_executable

    @classmethod
    def parallel_preprocess_instances(cls):
        return cls._instance().parallel_preprocess_instances

    @classmethod
    def parallel_preprocess_timeout_seconds(cls):
        return cls._instance().parallel_preprocess_timeout_seconds

    @classmethod
    def parallel_preprocess_keep_temp_files(cls):
        return cls._instance().parallel_preprocess_keep_temp_files

    @classmethod
    def prefix_quick_apply_to_object_name(cls):
        return cls._instance().prefix_quick_apply_to_object_name

    @classmethod
    def selected_blueprint_name(cls):
        return cls._instance().selected_blueprint_name

    @classmethod
    def get_deduplicate_element_set(cls) -> set:
        instance = cls._instance()
        element_set = set()
        if instance.deduplicate_POSITION:
            element_set.add("POSITION")
        if instance.deduplicate_NORMAL:
            element_set.add("NORMAL")
        if instance.deduplicate_TANGENT:
            element_set.add("TANGENT")
        if instance.deduplicate_BINORMAL:
            element_set.add("BINORMAL")
        if instance.deduplicate_TEXCOORD:
            element_set.add("TEXCOORD")
        if instance.deduplicate_COLOR:
            element_set.add("COLOR")
        if instance.deduplicate_BLENDWEIGHT:
            element_set.add("BLENDWEIGHT")
        if instance.deduplicate_BLENDINDICES:
            element_set.add("BLENDINDICES")
        return element_set


def register():
    bpy.utils.register_class(GlobalProterties)
    bpy.types.Scene.global_properties = bpy.props.PointerProperty(type=GlobalProterties)
    # 加载文件后清理失效的「指定工作空间」（见函数 docstring）
    if _sanitize_specific_workspace_name not in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.append(_sanitize_specific_workspace_name)
    try:
        from ..blueprint.export_helper import BlueprintExportHelper
        BlueprintExportHelper.ensure_valid_selected_blueprint_name()
    except Exception:
        pass


def unregister():
    if _sanitize_specific_workspace_name in bpy.app.handlers.load_post:
        bpy.app.handlers.load_post.remove(_sanitize_specific_workspace_name)
    del bpy.types.Scene.global_properties
    bpy.utils.unregister_class(GlobalProterties)
