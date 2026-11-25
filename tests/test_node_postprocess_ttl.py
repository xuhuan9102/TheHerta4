import sys

import pytest

from common import ttl_protocol
from test_node_postprocess_rabbitfx_pro import PKG, _load, _TEST_BPY, pro


@pytest.fixture
def ttl(monkeypatch):
    monkeypatch.setitem(sys.modules, "bpy", _TEST_BPY)
    monkeypatch.setitem(sys.modules, f"{PKG}.common.ttl_protocol", ttl_protocol)
    monkeypatch.setitem(sys.modules, f"{PKG}.blueprint.ini_scope", pro.ini_scope)
    return _load("node_postprocess_ttl", "blueprint/node_postprocess_ttl.py")


def _node(ttl, tag, **settings):
    node = ttl.SSMTNode_PostProcess_TTL()
    defaults = dict(
        name=tag, buffer_tag=tag, enable_glow=True, glow_h=45, glow_s=10, glow_v=-5,
        glow_brightness=8, glow_interpolate=0.5, glow_mode="ADD",
        enable_breath=False, breath_fps=200, breath_step=0.3, breath_mode="SINGLE",
        breath_h=0, breath_s=0, breath_v=0, breath_brightness_min=1,
        breath_brightness_max=8, breath_interpolate=1,
        override_ttl_mask=False, override_ttl_alpha=False,
        ttl_mask_channel="A", ttl_mask_invert=False, ttl_alpha=1,
    )
    for name, value in (defaults | settings).items():
        setattr(node, name, value)
    return node


def _targets(*parts):
    return [(part.casefold(), {"hex": "fd054d1d"}, part) for part in parts]


def _mesh(part, *settings, consumer=r"run = CommandList\EFMITTL\Draw"):
    return [
        f"    ; [mesh:LOD0.fd054d1d-12-0.{part}]",
        *(f"    {setting}" for setting in settings),
        f"    {consumer}",
    ]


def test_static_ttl_settings_follow_switched_bindings_and_keep_draw_args(ttl):
    draw_args = [r"$\EFMITTL\count = 12", r"$\EFMITTL\first = 3"]
    fixture = "\n".join([
        "[CommandListParts]", "if $swapkey == 1",
        *_mesh("Body",
               r"Resource\EFMITTL\Mask = ref ResourceMask",
               r"$\EFMITTL\alpha = 0.75",
               "if $tex == 0",
               r"    $\EFMITTL\brightness = 2",
               r"    Resource\EFMITTL\GlowMap = ref ResourceA",
               "else",
               r"    $\EFMITTL\brightness = 3.5",
               r"    Resource\EFMITTL\GlowMap = ref ResourceB",
               "endif", *draw_args),
        *_mesh("Sleeve", r"$\EFMITTL\brightness = 4",
               r"Resource\EFMITTL\GlowMap = ref ResourceSleeve", *draw_args),
        "endif", "",
    ])
    node = _node(ttl, "body", override_ttl_mask=True, ttl_mask_channel="G", ttl_mask_invert=True)
    updated = node._patch_ini(fixture, _targets("Body"))
    draws, constants = updated.split("[Constants]", maxsplit=1)
    body, tail = draws.split("    ; [mesh:LOD0.fd054d1d-12-0.Sleeve]", maxsplit=1)
    assert tail == fixture.split("    ; [mesh:LOD0.fd054d1d-12-0.Sleeve]", maxsplit=1)[1]
    assert body.count(r"$\EFMITTL\brightness = ") == 1
    assert body.count(r"Resource\EFMITTL\GlowMap = ref") == 2
    assert body.index("ResourceB") < body.index(r"$\EFMITTL\h = $ttlglow_body_h") < body.index(draw_args[0])
    for name, default in (("h", 45.0), ("brightness", 8.0), ("glow_mode", 1),
                          ("mask_channel", 1), ("mask_invert", 1)):
        assert f"    {_assignment(name, 'body')}\n" in body
        assert constants.count(f"global $ttlglow_body_{name} = {default}\n") == 1
    assert r"$\EFMITTL\alpha = 0.75" in body
    lines = body.splitlines()
    assert ttl_protocol.parse_efmi_ttl_draw_call(lines, len(lines) - 1)[1] == (12, 3)
    assert node._patch_ini(updated, _targets("Body")) == updated
    node.glow_brightness = 12.0
    assert node._patch_ini(updated, _targets("Body")) == updated.replace(
        "global $ttlglow_body_brightness = 8.0", "global $ttlglow_body_brightness = 12.0",
    )
    node.enable_glow = False
    mask_only = node._patch_ini(fixture, _targets("Body"))
    assert r"$\EFMITTL\brightness = 2" in mask_only
    assert r"$\EFMITTL\brightness = 3.5" in mask_only
    assert _assignment("brightness", "body") not in mask_only


def _assignment(name, tag):
    return rf"$\EFMITTL\{name} = $ttlglow_{tag}_{name}"


def test_ttl_nodes_keep_independent_breath_clocks_and_allow_mask_only(ttl):
    draw_args = [r"$\EFMITTL\count = 12", r"$\EFMITTL\first = 0"]
    fixture = "\r\n".join([
        "[Constants]", "global $untouched = 1", "[Present]", "post $untouched = 1",
        "[CommandListParts]", "if $swapkey == 1",
        *_mesh("Body", r"Resource\EFMITTL\GlowMap = ref ResourceBody",
               consumer=r"run = CommandList\EFMITTL\SetGlow"),
        *_mesh("Sleeve", r"Resource\EFMITTL\GlowMap = ref ResourceSleeve", *draw_args),
        *_mesh("Veil", r"Resource\EFMITTL\Mask = ref ResourceVeil", *draw_args),
        "endif", "",
    ])
    first = _node(ttl, "body", enable_breath=True)
    second = _node(ttl, "other", enable_breath=True, override_ttl_alpha=True, ttl_alpha=0.5)
    updated = first._patch_ini(fixture, _targets("Body"))
    updated = second._patch_ini(updated, _targets("Sleeve", "Veil"))
    assert updated.count("[Present]") == 1 and updated.count("[Constants]") == 1
    for tag in ("body", "other"):
        assert updated.count(f"global $ttlglow_{tag}_breath_step = 0.3") == 1
        assert updated.count(f"$ttlglow_{tag}_frame = $ttlglow_{tag}_frame + $ttlglow_{tag}_breath_step") == 1
        assert updated.count(_assignment("brightness", tag)) == 1
    assert updated.count(_assignment("alpha", "other")) == 1
    assert updated.count("global $ttlglow_other_alpha = 0.5") == 1
    assert "\n" not in updated.replace("\r\n", "")
    assert first._patch_ini(updated, _targets("Body")) == updated
    assert second._patch_ini(updated, _targets("Sleeve", "Veil")) == updated
    first.enable_breath = False
    updated = first._patch_ini(updated, _targets("Body"))
    assert "$ttlglow_body_breath_" not in updated
    assert f"global $ttlglow_body_brightness = 8.0" in updated
    assert "$ttlglow_other_" in updated
