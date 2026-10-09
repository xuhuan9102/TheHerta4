"""把当前工程里的 .dds 图片引用转成同目录下的 .png，并把引用改到新文件。

为什么需要它：Blender 解不了工作空间里常见的 BC7 / BC6H 压缩 DDS，加载时会打
``WARNING DDS image ... is not in a supported GPU compression format`` 并退化成未压缩
读取（BC6H 的 LightMap / MaterialMap 还会 ``failed to load data from file``）。转成
PNG 后 Blender 能正常读，控制台也不再刷这些警告。

三条实测得出的设计约束（数字见 tests/test_dds_to_png.py）：

1. **色彩空间交给 texconv 自己判断**：普通（整型）源只给 ``-ft png``，*不给* ``-f``，
   texconv 会保留输入的 sRGB 语义（``BC7_UNORM_SRGB`` -> ``R8G8B8A8_UNORM_SRGB``，
   ``BC7_UNORM`` -> ``R8G8B8A8_UNORM``）。实测这与手工按格式指定输出逐像素完全一致
   （mean_abs_diff = 0.0）；而手工指定一旦写错，就会让 sRGB 贴图被线性解码一次，
   表现为整片颜色变深。
2. **float/HDR 源（BC6H、R16F/R32F 等）必须钉死 16bit 输出**：texconv 把 float 写成 8bit PNG 时
   会走 sRGB 编码路径，数值整体非线性提亮（LightMap 实测 0.2172 -> 0.2993）；改成
   ``-f R16G16B16A16_UNORM`` 后逐像素一致（max diff 0.00001）。见 :func:`is_hdr_dds`。
   **注意 legacy 头也要认**：浮点格式会以 ``D3DFMT`` 枚举值写在 ``dwFourCC`` 位置
   （实测 113 = R16G16B16A16_FLOAT），只认 DX10 头会漏判、整片变亮变淡。
3. **原 .dds 一律保留**（用户明确要求），所以这里不做任何删除动作。
4. **重连只换容器与路径**：沿用图片原有的色彩空间与 Alpha 模式，不改用户已设好的
   配置 —— 与 :mod:`tt_dds_conversion` 的既有约定保持一致。
"""

import os
import struct
import subprocess

import bpy

from .tt_dds_conversion import find_texconv


_AUTO_INTERVAL = 0.005
_LOG_PREFIX = "[DDS转PNG]"

# 需要 16bit 输出的浮点/HDR 源格式（DXGI 格式号，取自 dxgiformat.h）：
# R32G32B32A32_FLOAT / R32G32B32_FLOAT / R16G16B16A16_FLOAT / R32G32_FLOAT /
# R16G16_FLOAT / R32_FLOAT / R16_FLOAT / R9G9B9E5_SHAREDEXP / BC6H_UF16 / BC6H_SF16。
# 判据是"能表示大于 1.0 的数值"，因此 BC6H 这类 HDR 块压缩格式也在内。
_HDR_DXGI_FORMATS = {2, 6, 10, 16, 34, 41, 54, 67, 95, 96}

# legacy（无 DX10 扩展头）DDS 把部分浮点格式写成 D3DFMT 枚举值放在 dwFourCC 位置，
# 而不是写 4 字符的 FOURCC。DirectXTex 认得这些值，实测 0x71(113) 被读成
# R16G16B16A16_FLOAT。这里映射到对应的 DXGI 格式号，好让判据只有一份。
#
# 为什么必须认出来（2026-10-08 实测，用户报"线性贴图转换后颜色变淡"）：这类源一旦
# 被漏判成普通整型，就不会加 ``-f R16G16B16A16_UNORM``，texconv 会把 float 源写成
# **sRGB 编码的 8bit PNG**，数值整体提亮。实测 611df76d-132-0-LightMap.dds（G 通道真值
# 0.5020）写成 8bit 得到 188（= sRGB 编码后的值），而正确输出应是 128。
_LEGACY_D3DFMT_TO_DXGI = {
    111: 54,   # D3DFMT_R16F          -> R16_FLOAT
    112: 34,   # D3DFMT_G16R16F       -> R16G16_FLOAT
    113: 10,   # D3DFMT_A16B16G16R16F -> R16G16B16A16_FLOAT
    114: 41,   # D3DFMT_R32F          -> R32_FLOAT
    115: 16,   # D3DFMT_G32R32F       -> R32G32_FLOAT
    116: 2,    # D3DFMT_A32B32G32R32F -> R32G32B32A32_FLOAT
}

#: DXGI 格式号 -> 可读名（只列工作空间里真的会遇到的，认不出就显示数字）。
_DXGI_FORMAT_NAMES = {
    2: "R32G32B32A32_FLOAT", 6: "R32G32B32_FLOAT", 10: "R16G16B16A16_FLOAT",
    11: "R16G16B16A16_UNORM", 16: "R32G32_FLOAT", 28: "R8G8B8A8_UNORM",
    29: "R8G8B8A8_UNORM_SRGB", 34: "R16G16_FLOAT", 41: "R32_FLOAT",
    49: "R8G8_UNORM", 54: "R16_FLOAT", 61: "R8_UNORM",
    71: "BC1_UNORM", 72: "BC1_UNORM_SRGB", 74: "BC2_UNORM", 75: "BC2_UNORM_SRGB",
    77: "BC3_UNORM", 78: "BC3_UNORM_SRGB", 80: "BC4_UNORM", 83: "BC5_UNORM",
    87: "B8G8R8A8_UNORM", 91: "B8G8R8A8_UNORM_SRGB",
    95: "BC6H_UF16", 96: "BC6H_SF16", 98: "BC7_UNORM", 99: "BC7_UNORM_SRGB",
}

#: 带 sRGB 语义的 DXGI 格式号（``*_UNORM_SRGB`` 家族）。
_SRGB_DXGI_FORMATS = {29, 72, 75, 78, 91, 99}

#: legacy 头里 4 字符 FOURCC 的可读名。这类头**没有 sRGB 变体**，texconv 一律当线性读。
_LEGACY_FOURCC_NAMES = {
    b"DXT1": "BC1_UNORM", b"DXT3": "BC2_UNORM", b"DXT5": "BC3_UNORM",
    b"ATI2": "BC5_UNORM", b"BC5U": "BC5_UNORM", b"BC4U": "BC4_UNORM",
}


def probe_dds(dds_path):
    """读 DDS 头，判定它的属性 —— 转换命令完全由这份属性决定（见 :data:`_PNG_COMMAND_TABLE`）。

    返回 dict：

    - ``container`` ``"DX10"`` / ``"legacy-fourcc"`` / ``"legacy-raw"``
    - ``format``    可读格式名（``BC7_UNORM_SRGB``、``BC1_UNORM``、``raw_32bit``…）
    - ``dxgi``      对应的 DXGI 格式号（认不出为 None）
    - ``is_srgb``   像素格式是否自带 sRGB 语义
    - ``is_float``  能否表示 > 1.0 的数值（浮点/HDR）
    - ``bits``      legacy 未压缩头的每像素位数（其余情况为 0）

    两种头都要认：DX10 头直接读显式 DXGI 号；legacy 头里 FOURCC 可能是 ``DXT1`` 这类
    字符串，也可能是 ``D3DFMT`` 枚举值（实测 113 = R16G16B16A16_FLOAT）。
    """
    attrs = {"container": "unknown", "format": "?", "dxgi": None,
             "is_srgb": False, "is_float": False, "bits": 0}
    try:
        with open(dds_path, "rb") as handle:
            head = handle.read(148)
    except OSError:
        return attrs
    if len(head) < 128 or head[:4] != b"DDS ":
        return attrs

    fourcc = head[84:88]
    if fourcc == b"DX10":
        if len(head) < 132:
            return attrs
        (dxgi_format,) = struct.unpack_from("<I", head, 128)
        attrs.update({
            "container": "DX10",
            "dxgi": dxgi_format,
            "format": _DXGI_FORMAT_NAMES.get(dxgi_format, "DXGI_%d" % dxgi_format),
            "is_srgb": dxgi_format in _SRGB_DXGI_FORMATS,
            "is_float": dxgi_format in _HDR_DXGI_FORMATS,
        })
        return attrs

    pf_flags, fourcc_value = struct.unpack_from("<II", head, 80)
    if pf_flags & 0x4:  # DDPF_FOURCC
        dxgi_format = _LEGACY_D3DFMT_TO_DXGI.get(fourcc_value)
        name = _LEGACY_FOURCC_NAMES.get(fourcc)
        if name is None:
            name = _DXGI_FORMAT_NAMES.get(dxgi_format)
        if name is None:
            name = "D3DFMT_%d" % fourcc_value
        attrs.update({
            "container": "legacy-fourcc",
            "dxgi": dxgi_format,
            "format": name,
            "is_float": dxgi_format in _HDR_DXGI_FORMATS,
        })
        return attrs

    bitcount = struct.unpack_from("<I", head, 88)[0]
    attrs.update({"container": "legacy-raw", "bits": bitcount,
                  "format": "raw_%dbit" % bitcount})
    return attrs


#: **DDS 属性 -> texconv 输出命令** 的映射表：从上往下，第一条命中的生效。
#: 每项含 ``rule``（规则名，打日志用）、``match``（属性判定）、``args``（追加给 texconv 的参数）、
#: ``why``（为什么这么选）。
_PNG_COMMAND_TABLE = (
    {
        "rule": "float-16bit",
        "match": lambda attrs: attrs["is_float"],
        "args": ("-f", "R16G16B16A16_UNORM"),
        "why": "浮点/HDR 源：写 8bit PNG 会被 texconv 按 sRGB 编码整体提亮，必须钉 16bit",
    },
    {
        "rule": "srgb-integer",
        "match": lambda attrs: attrs["is_srgb"],
        "args": (),
        "why": "sRGB 整型源：不给 -f，由 texconv 保持 sRGB 进 sRGB 出（数值不变）",
    },
    {
        "rule": "linear-integer",
        "match": lambda attrs: True,
        "args": (),
        "why": "线性整型源：不给 -f，按原数值输出，不做色彩变换",
    },
)


def select_png_command(attrs):
    """查表：DDS 属性 -> 命中的规则（含要追加的 texconv 参数）。"""
    for entry in _PNG_COMMAND_TABLE:
        if entry["match"](attrs):
            return entry
    return _PNG_COMMAND_TABLE[-1]



# 自动转换任务的分帧状态；None 表示当前没有任务在跑。
_AUTO_JOB = {
    "images": None,
    "force": False,
    "converted": 0,
    "skipped": 0,
    "failed": 0,
}


def png_path_for(dds_path):
    """同名替换扩展名：``a/b/X-LightMap.dds`` -> ``a/b/X-LightMap.png``。"""
    return os.path.splitext(dds_path)[0] + ".png"


def is_hdr_dds(dds_path):
    """浮点/HDR 源判定：:func:`probe_dds` 的便捷包装，让调用点与映射表共用同一份判据。

    为什么浮点源必须单独对待：写 **8bit** PNG 时 texconv 会走一条 sRGB 编码路径，把数值
    非线性提亮（实测 LightMap 0.2172 -> 0.2993）；钉死 16bit 后逐像素一致。
    """
    return bool(probe_dds(dds_path)["is_float"])


def png_bit_depth(png_path):
    """读 PNG IHDR 的位深（8 / 16），读不到返回 None。

    用作 HDR 源转换后的**结果校验**：确认 texconv 确实产出了 16bit PNG。
    （不能反过来用它判断"既有 png 能不能沿用"—— 实测存在位深 16 但内容被
    sRGB 编码改亮的产物，位深对不代表内容对。）
    """
    try:
        with open(png_path, "rb") as handle:
            head = handle.read(26)
    except OSError:
        return None
    if len(head) < 25 or head[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    return head[24]


def png_color_metadata(png_path):
    """读 PNG 的色彩相关 chunk，返回 ``(位深, ["gAMA=100000", "sRGB", ...])``。

    为什么需要它：texconv 会把输入 DDS 的 sRGB 语义**写进输出 PNG 的元数据** ——
    线性源写 ``gAMA=1.0``（无 sRGB chunk），sRGB 源写 ``sRGB`` + ``gAMA=2.2``。
    带色彩管理的看图软件会据此把"线性"图在显示时做一次编码，看起来就比 DDS 亮；
    这也是"线性贴图转 PNG 后变亮、DiffuseMap 正常"的直接原因。诊断时打这行日志即可。
    """
    try:
        with open(png_path, "rb") as handle:
            data = handle.read()
    except OSError:
        return None, []
    if len(data) < 26 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return None, []

    chunks = []
    pos = 8
    while pos + 8 <= len(data):
        (length,) = struct.unpack_from(">I", data, pos)
        kind = data[pos + 4:pos + 8].decode("ascii", "replace")
        if kind in ("gAMA", "sRGB", "iCCP", "cHRM"):
            if kind == "gAMA":
                (value,) = struct.unpack_from(">I", data, pos + 8)
                chunks.append("gAMA=%d" % value)
            else:
                chunks.append(kind)
        if kind == "IEND":
            break
        pos += 12 + length
    return data[24], chunks


#: PNG 里"这些数值该怎么解释"相关的 chunk。Photoshop / Paint.NET 之类尊重它们的软件
#: 会据此把图从源色彩空间转到工作空间，因此它们能让同一份字节显示成不同亮度。
_PNG_COLOR_CHUNKS = {b"gAMA", b"sRGB", b"iCCP", b"cHRM"}


def strip_color_metadata(png_path):
    """删掉 PNG 里的色彩管理 chunk，返回是否真的改动了文件（读不了时返回 False）。

    为什么必须做（2026-10-09 实测）：texconv 给**线性**源输出的 PNG 会带 ``gAMA=1.0``，
    相当于声明"文件里存的是线性光"。Photoshop CC 2017 / Paint.NET 尊重这个 chunk，
    显示时会从"线性"转到工作空间 sRGB（做一次编码），整片变亮；而 DDS 根本没有这种
    元数据，所以转出来的 png 和 dds 一眼就能看出亮度差别。删掉后按未标记处理，
    数值原样呈现，与 dds 的观感一致。

    sRGB 源本来就带正确的 ``sRGB`` chunk（显示正常），**不要**对它调用本函数。
    """
    try:
        with open(png_path, "rb") as handle:
            data = handle.read()
    except OSError:
        return False
    if len(data) < 8 or data[:8] != b"\x89PNG\r\n\x1a\n":
        return False

    rebuilt = bytearray(data[:8])
    pos = 8
    changed = False
    while pos + 8 <= len(data):
        (length,) = struct.unpack_from(">I", data, pos)
        kind = data[pos + 4:pos + 8]
        end = pos + 12 + length
        if end > len(data):
            break
        if kind in _PNG_COLOR_CHUNKS:
            changed = True
        else:
            rebuilt += data[pos:end]
        if kind == b"IEND":
            break
        pos = end

    if not changed:
        return False
    with open(png_path, "wb") as handle:
        handle.write(bytes(rebuilt))
    return True


def iter_dds_images():
    """收集工程里所有指向 .dds 的 FILE 图片，返回 ``[(image, 绝对路径), ...]``。

    先把列表取全再动手，避免边遍历 ``bpy.data.images`` 边改 ``filepath`` 时的歧义；
    同时这也让"本次要处理哪些图"在转换开始前就固定下来。
    """
    found = []
    for image in bpy.data.images:
        if image.source != "FILE" or not image.filepath:
            continue
        try:
            abs_path = os.path.normpath(bpy.path.abspath(image.filepath_raw))
        except Exception:
            continue
        if os.path.splitext(abs_path)[1].lower() != ".dds":
            continue
        found.append((image, abs_path))
    return found


def convert_dds_to_png(texconv, dds_path):
    """按属性检测结果选命令，用 texconv 在 dds 同目录生成同名 .png，返回 png 绝对路径。

    命令由 :func:`probe_dds` + :data:`_PNG_COMMAND_TABLE` 决定；``-y`` 覆盖同名旧文件。
    转换前后各打一行日志（属性 / 命中规则 / 产物色彩元数据），便于直接核对。
    """
    out_dir = os.path.dirname(dds_path)
    attrs = probe_dds(dds_path)
    rule = select_png_command(attrs)
    command = [texconv] + list(rule["args"]) + ["-ft", "png", "-o", out_dir, "-y", dds_path]
    print("%s %s: %s/%s%s -> 规则 %s（%s），参数 %s" % (
        _LOG_PREFIX, os.path.basename(dds_path), attrs["container"], attrs["format"],
        " sRGB" if attrs["is_srgb"] else (" float" if attrs["is_float"] else " linear"),
        rule["rule"], rule["why"], " ".join(rule["args"]) or "无 -f 参数"))
    process = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="ignore",
    )
    if process.returncode != 0:
        detail = (process.stderr or process.stdout or "").strip()
        raise RuntimeError(detail or f"texconv 退出码 {process.returncode}")

    png_path = png_path_for(dds_path)
    if not os.path.exists(png_path):
        raise RuntimeError(f"texconv 未生成 {png_path}")
    # 线性源的产物会被 texconv 打上 gAMA=1.0，Photoshop / Paint.NET 会据此重编码显示、
    # 整片变亮；去掉它，数值才能和 dds 显示成一样。sRGB 源的 sRGB chunk 是正确的，保留。
    stripped = False
    if not attrs["is_srgb"]:
        stripped = strip_color_metadata(png_path)
    depth, chunks = png_color_metadata(png_path)
    print("%s   产物 %s: 位深 %s，色彩元数据 %s%s" % (
        _LOG_PREFIX, os.path.basename(png_path), depth, "、".join(chunks) or "无",
        "（已清除线性源的 gAMA）" if stripped else ""))
    return png_path


def relink_image(image, png_path):
    """把图片重连到 png：只换路径与容器格式，显示配置原样保留。"""
    try:
        colorspace = str(image.colorspace_settings.name or "")
    except Exception:
        colorspace = ""
    try:
        alpha_mode = str(image.alpha_mode or "")
    except Exception:
        alpha_mode = ""

    image.filepath = png_path
    image.reload()

    if colorspace:
        try:
            if image.colorspace_settings.name != colorspace:
                image.colorspace_settings.name = colorspace
        except Exception:
            pass
    if alpha_mode:
        try:
            if image.alpha_mode != alpha_mode:
                image.alpha_mode = alpha_mode
        except Exception:
            pass


def process_one(image, dds_path, force=False):
    """处理一张图片，返回 ``(状态, 说明)``，状态取值 converted / skipped / failed。

    ``force=False``（导入时的自动流程）**只看同名 .png 在不在**：在就沿用、不跑 texconv，
    重复导入同一个工作空间不再反复转换。磁盘上那张 png 若有问题（历史遗留的错误产物），
    **按用户要求由他自己删掉**——这里不做内容判定、不自动重转。

    ``force=True``（面板上的「立即把工程内 DDS 引用转为 PNG」按钮）无条件重转一遍，
    用于主动刷新磁盘上的 png。删掉某几张 png 后再导入，只会补转缺的那几张。
    """
    png_path = png_path_for(dds_path)
    attrs = probe_dds(dds_path)
    hdr_source = attrs["is_float"]
    if not force and os.path.exists(png_path):
        relink_image(image, png_path)
        return "skipped", png_path

    texconv = find_texconv()
    if not texconv:
        return "failed", "未找到 texconv.exe"
    try:
        convert_dds_to_png(texconv, dds_path)
    except Exception as exc:
        return "failed", str(exc)

    if hdr_source and png_bit_depth(png_path) != 16:
        # 结果校验：HDR 源没产出 16bit PNG 说明命令没生效，宁可报失败也别留错图
        return "failed", "HDR 源未产出 16bit PNG"

    try:
        relink_image(image, png_path)
    except Exception as exc:
        return "failed", f"重连引用失败: {exc}"
    return "converted", png_path


def convert_all(force=False, reporter=None):
    """同步处理工程里全部 .dds 引用，返回统计字典。

    ``reporter`` 为一个接受 ``(类别, 文本)`` 的可调用对象；传 None 时只打印。
    手动按钮用它把结果送进 ``self.report``，自动流程用默认打印。
    """
    def emit(category, message):
        if reporter is not None:
            reporter(category, message)
        else:
            print(f"{_LOG_PREFIX} {message}")

    targets = iter_dds_images()
    if not targets:
        emit("INFO", "工程里没有指向 .dds 的图片引用。")
        return {"total": 0, "converted": 0, "skipped": 0, "failed": 0}

    if not find_texconv():
        emit("ERROR", "未找到 texconv.exe。请将其放入插件目录的 Toolset 子文件夹，或手动指定路径。")
        return {"total": len(targets), "converted": 0, "skipped": 0, "failed": len(targets)}

    stats = {"total": len(targets), "converted": 0, "skipped": 0, "failed": 0}
    for image, dds_path in targets:
        status, detail = process_one(image, dds_path, force=force)
        stats[status] += 1
        if status == "failed":
            emit("WARNING", f"{os.path.basename(dds_path)} 转换失败：{detail}")

    emit(
        "INFO",
        f"共 {stats['total']} 个 DDS 引用：转换 {stats['converted']} 个，"
        f"沿用已有 PNG {stats['skipped']} 个，失败 {stats['failed']} 个。原 .dds 已全部保留。",
    )
    return stats


def _get_props():
    """取 texture_tools_props；timer 里 bpy.context 可能没有 scene，用首个场景兜底。"""
    try:
        scene = bpy.context.scene
    except Exception:
        scene = None
    if scene is None:
        scene = bpy.data.scenes[0] if len(bpy.data.scenes) else None
    if scene is None:
        return None
    return getattr(scene, "texture_tools_props", None)


def _auto_tick():
    """自动转换的分帧回调：每帧只处理一张，避免长时间阻塞界面。"""
    job = _AUTO_JOB
    images = job["images"]
    if images is None:
        return None

    if not images:
        print(
            f"{_LOG_PREFIX} 自动转换完成：转换 {job['converted']} 个，"
            f"沿用已有 PNG {job['skipped']} 个，失败 {job['failed']} 个。"
        )
        job["images"] = None
        return None

    image, dds_path = images.pop()
    status, detail = process_one(image, dds_path, force=job["force"])
    job[status] += 1
    if status == "failed":
        print(f"{_LOG_PREFIX} {os.path.basename(dds_path)} 转换失败：{detail}")
    return _AUTO_INTERVAL


def schedule_auto_convert(context=None, force=False):
    """导入流程结束后调度一次自动转换；未勾选或已有任务在跑时什么都不做。"""
    props = _get_props()
    if props is None or not getattr(props, "dds_auto_convert_png_after_import", False):
        return False
    if _AUTO_JOB["images"] is not None:
        return False

    targets = iter_dds_images()
    if not targets:
        return False

    _AUTO_JOB.update(
        {
            "images": targets,
            "force": force,
            "converted": 0,
            "skipped": 0,
            "failed": 0,
        }
    )
    print(f"{_LOG_PREFIX} 检测到 {len(targets)} 个 DDS 引用，开始自动转换为 PNG。")
    # 用 timer 而不是当场执行：导入算子还没返回，此时长耗时会卡住整个导入。
    bpy.app.timers.register(_auto_tick, first_interval=0.5)
    return True


class TT_OT_convert_dds_to_png(bpy.types.Operator):
    bl_idname = "toolkit.tt_convert_dds_to_png"
    bl_label = "立即把工程内 DDS 引用转为 PNG"
    bl_description = (
        "扫描当前工程里所有指向 .dds 的图片，用 texconv 在同目录生成同名 .png 并改掉引用。"
        "原 .dds 文件保留不动。已存在的 .png 也会重新转换，用于修正颜色不对的旧文件"
    )
    bl_options = {"REGISTER", "UNDO"}

    def execute(self, context):
        stats = convert_all(force=True, reporter=self.report)
        if stats["total"] == 0:
            return {"CANCELLED"}
        if stats["failed"] and not stats["converted"] and not stats["skipped"]:
            return {"CANCELLED"}
        return {"FINISHED"}


tt_dds_to_png_list = [
    TT_OT_convert_dds_to_png,
]
