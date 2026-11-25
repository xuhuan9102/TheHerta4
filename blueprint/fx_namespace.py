# -*- coding: utf-8 -*-
"""FX 命名空间档案：发光 / 裁切贴图协议在各游戏运行时里的名字与能力。

同一套「材质转资源写绑定 → 后处理节点注入参数」的协议，在不同游戏的 3DMigoto
运行时里用的是**不同的命名空间**：名字、变量大小写、以及可用的命令列表都不一样。

============  ==================  ================================  ====================================
运行时        适用游戏            资源别名 / 变量                    可用能力
============  ==================  ================================  ====================================
``RabbitFX``  绝区零 / 崩铁 /      ``Resource\\RabbitFX\\Glowmap``   发光 + 裁切 + W-Engine 同步 +
              原神 / EFMI …        ``$\\RabbitFX\\H``（大写）          ColorShift；``Run`` 结尾自带变量复位
``NTEMIFX``   异环 NTEMI           ``Resource\\NTEMIFX\\Glowmap``    只有发光 / 裁切（pro 节点不注入参数）
``HI3FX``     崩坏 3 HIMI          ``Resource\\HI3FX\\GlowMap``      发光 + 裁切（FXMap，二值）+
                                   ``Resource\\HI3FX\\TTLMap``       抖动半透明（TTLMap）；
                                   ``$\\HI3FX\\h``（小写）            没有同步 / ColorShift；
                                                                      ``Run`` **不**复位变量，复位必须
                                                                      调 ``CommandList\\HI3FX\\Reset``
============  ==================  ================================  ====================================

``HI3FX`` 的调用约定取自随 SSMT 包分发的 ``3Dmigoto\\HI3\\Mods\\HI3FX``
（``HI3FX.ini`` 的 ``[Constants]`` / ``[CommandListRun]`` / ``[CommandListReset]``
与 ``HI3FX.Example.ini``）：

.. code-block:: ini

    Resource\\HI3FX\\FXMap = ref ResourceXxx
    Resource\\HI3FX\\GlowMap = ref ResourceYyy
    Resource\\HI3FX\\TTLMap = ref ResourceZzz     ; 半透明遮罩（可选，HI3FX v1.1）
    $\\HI3FX\\h = 25
    run = CommandList\\HI3FX\\Run
    ; 绘制之后
    run = CommandList\\HI3FX\\Reset

``ttlmap_name`` 是第二个遮罩通道：``FXMap`` 只做二值裁切（在 shader 里按
``$fx_cutoff`` 截断成 0/1，中间灰阶也不会变成抖动半透明），``TTLMap`` 才是
覆盖率 —— 中间值走抖动半透明。两者互不影响，同时绑定时按乘法合成（裁切优先）。
只有声明了 ``ttlmap_name`` 的命名空间（目前只有 ``HI3FX``）才会产出 TTLMap 引用；
``RabbitFX`` / ``NTEMIFX`` 没有这个通道，行为与以前完全一致。

这与 ``toolkit/tt_alpha_extract.py`` 的材质前缀是同一套语义：允许半透明时
抽出的遮罩材质叫 ``TTLMap_*``（走抖动），不允许时叫 ``FXMap_*``（只裁切）——
材质转资源按前缀分别产出 ``Resource\\HI3FX\\TTLMap`` / ``Resource\\HI3FX\\FXMap``。

与 RabbitFX 的三处硬差别（照搬 RabbitFX 的写法会出问题）：

1. ``CommandList\\HI3FX\\Run`` 只做 ``Commit`` + ``Bind``，**不会**把
   ``$h/$s/$v/$brightness`` 复位。RabbitFX 的复位套路是「把变量写 0，再 Run 一次」
   ——在 HI3FX 上变量会一直停在 0，后续绘制全部变黑。所以 HIMI 的复位只能走
   ``run = CommandList\\HI3FX\\Reset``（它自己会 null 掉资源别名并 ``Clean``）。
2. 变量是小写 ``h/s/v``（``$\\HI3FX\\h``），不是 RabbitFX 的 ``H/S/V``。
   ``brightness`` / ``interpolate`` 同名，语义也一致（H 为角度、S/V 为百分比）。
3. 没有 ``SetFXBuffer`` / ``ERun`` / ``UpdateFXBuffer`` / ``ColorShift``，
   所以 pro 节点的 W-Engine 同步与颜色偏移在 HIMI 下不可用。

``SetTextures``（语义贴图重映射）在 HI3FX 里属于 ``HI3FX.Remap.ini``，该文件默认
躺在 ``DISABLED\\`` 里不加载，所以本档案**不**声明 ``set_textures``：材质转资源不会
自动写 ``run = CommandList\\HI3FX\\SetTextures``（跑一个不存在的命令列表会报错）。
作者自己启用 Remap 后可手工补这一行。
"""

import re

from ..common.logic_name import LogicName


class FXNamespaceProfile:
    """一个 3DMigoto FX 运行时的命名空间档案。"""

    def __init__(
        self,
        key,
        *,
        glow_name="Glowmap",
        fxmap_name="FXMap",
        ttlmap_name=None,
        param_case="upper",
        run="Run",
        set_textures=None,
        reset=None,
        supports_sync=True,
        supports_colorshift=True,
        supports_pro_injection=True,
    ):
        self.key = key
        self.glow_name = glow_name
        self.fxmap_name = fxmap_name
        # 抖动半透明用的第二个遮罩通道；None = 这个运行时没有这个概念，
        # 材质转资源不会为它产出任何引用行（RabbitFX / NTEMIFX 即此）。
        self.ttlmap_name = ttlmap_name
        self.param_case = param_case
        self.run = run
        self.set_textures = set_textures
        self.reset = reset
        self.supports_sync = supports_sync
        self.supports_colorshift = supports_colorshift
        self.supports_pro_injection = supports_pro_injection

        self.resource_prefix = f"Resource\\{key}\\"
        self.glow_ref = f"{self.resource_prefix}{glow_name}"
        self.fxmap_ref = f"{self.resource_prefix}{fxmap_name}"
        self.ttlmap_ref = (
            f"{self.resource_prefix}{ttlmap_name}" if ttlmap_name else ""
        )
        self.run_line = f"run = CommandList\\{key}\\{run}"
        self.set_textures_line = (
            f"run = CommandList\\{key}\\{set_textures}" if set_textures else ""
        )
        self.reset_line = f"run = CommandList\\{key}\\{reset}" if reset else ""

        self.glow_ref_re = re.compile(
            rf"^{re.escape(self.glow_ref)}\s*=\s*(?:ref\s+)?(?P<name>.+?)\s*$",
            re.IGNORECASE,
        )
        self.fxmap_ref_re = re.compile(
            rf"^{re.escape(self.fxmap_ref)}\s*=\s*(?:ref\s+)?(?P<name>.+?)\s*$",
            re.IGNORECASE,
        )
        self.ttlmap_ref_re = (
            re.compile(
                rf"^{re.escape(self.ttlmap_ref)}\s*=\s*(?:ref\s+)?(?P<name>.+?)\s*$",
                re.IGNORECASE,
            )
            if self.ttlmap_ref
            else None
        )
        self.run_line_re = re.compile(rf"^{re.escape(self.run_line)}$", re.IGNORECASE)
        self.any_ref_re = re.compile(
            rf"^{re.escape(self.resource_prefix)}(?:{re.escape(glow_name)}|"
            rf"{re.escape(fxmap_name)}"
            + (rf"|{re.escape(ttlmap_name)}" if ttlmap_name else "")
            + r")\s*=",
            re.IGNORECASE,
        )

    # ── 名字 ──

    def param(self, name):
        """``param("brightness")`` → ``$\\RabbitFX\\brightness`` / ``$\\HI3FX\\brightness``。

        大小写差异只体现在**单字母通道**上：RabbitFX 是 ``$…\\H/S/V``，
        HI3FX 是 ``$…\\h/s/v``；``brightness`` / ``interpolate`` 两边都是小写。
        """
        spelled = str(name)
        if len(spelled) == 1:
            spelled = spelled.upper() if self.param_case == "upper" else spelled.lower()
        return f"$\\{self.key}\\{spelled}"

    @property
    def reset_neutralises_variables(self):
        """运行时自己的 ``Run`` 结尾是否会把 ``$h/$s/$v/$brightness`` 复位。

        True（RabbitFX / NTEMIFX）：写 0 + 再 Run 一次即可复位；
        False（HI3FX）：必须调 ``Reset``，否则变量会一直停在 0 把后续绘制压黑。
        """
        return not self.reset_line

    def __repr__(self):  # pragma: no cover - 调试用
        return f"<FXNamespaceProfile {self.key}>"


RABBITFX = FXNamespaceProfile("RabbitFX", set_textures="SetTextures")

NTEMIFX = FXNamespaceProfile(
    "NTEMIFX",
    supports_sync=False,
    supports_colorshift=False,
    # pro 节点只认 RabbitFX 的参数名，NTEMI 下不做参数注入（只提示）。
    supports_pro_injection=False,
)

HI3FX = FXNamespaceProfile(
    "HI3FX",
    glow_name="GlowMap",
    # v1.1 起 HI3FX 有两个遮罩通道：FXMap 只裁切（二值），TTLMap 走抖动半透明。
    # 材质转资源按材质前缀（FXMap_ / TTLMap_）分别产出这两条引用。
    ttlmap_name="TTLMap",
    param_case="lower",
    reset="Reset",
    supports_sync=False,
    supports_colorshift=False,
)

ALL_PROFILES = (RABBITFX, NTEMIFX, HI3FX)

_EFMI_TEXTURE_SLOT_ROLES = {
    "DiffuseMap": "Diffuse",
    "LightMap": "LightMap",
    "NormalMap": "NormalMap",
}


def efmi_texture_slots(namespace):
    return {
        mark: f"Resource\\{namespace}\\{slot}"
        for mark, slot in _EFMI_TEXTURE_SLOT_ROLES.items()
    }


EFMITTL_TEXTURE_SLOTS = efmi_texture_slots("EFMITTL")
EFMITTL_SET_TEXTURES = r"run = CommandList\EFMITTL\SetTextures"


def profile_for_logic(logic_name):
    """按游戏执行逻辑取 FX 命名空间档案；未知逻辑退回 RabbitFX。"""
    resolved = str(logic_name or "").strip()
    if resolved and resolved == getattr(LogicName, "HIMI", "HIMI"):
        return HI3FX
    if resolved and resolved == getattr(LogicName, "NTEMI", "NTEMI"):
        return NTEMIFX
    return RABBITFX


def current_profile():
    """按当前 ``GlobalConfig.logic_name`` 取档案；配置缺失时退回 RabbitFX。"""
    try:
        from ..common.global_config import GlobalConfig
    except ImportError:  # pragma: no cover - 测试替身环境
        return RABBITFX
    return profile_for_logic(getattr(GlobalConfig, "logic_name", "") or "")


def detect_profiles(sections):
    """配置表里出现过的 FX 命名空间档案（用于给用户提示用的是哪一套）。"""
    found = []
    for profile in ALL_PROFILES:
        for section_lines in sections.values():
            if any(
                profile.any_ref_re.match(str(line).strip()) for line in section_lines
            ):
                found.append(profile)
                break
    return found
