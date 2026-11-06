"""ZZZ character-gated flow overlay."""

import ctypes
import re
import zlib

ZZZ_NORMAL_VERTEX_SHADER = 'c280f6945b23a42a'


def build_overlay_sections(tag, draws):
    """Hash lookup only: never register a global ShaderRegex for material flow."""
    sections = {
        '[ShaderOverrideFLMaterialPass_%s]' % tag: [
            'hash = ' + ZZZ_NORMAL_VERTEX_SHADER,
            '$fl_material_pass_%s = 1' % tag,
            'post $fl_material_pass_%s = 0' % tag,
        ],
    }
    for index, draw in enumerate(draws):
        lines = [
            'ps = ./res/flow_light/%s.material.hlsl' % tag,
            'depth_enable = true', 'depth_func = equal', 'depth_write_mask = zero',
            'blend = ADD ONE ONE', 'alpha = ADD ZERO ONE',
        ]
        # The other three native MRTs must receive additive zero, never an
        # undefined pixel output or a replacement normal/material value.
        for target in range(1, 4):
            lines.extend(['blend[%d] = ADD ONE ONE' % target, 'alpha[%d] = ADD ZERO ONE' % target])
        lines.append('%s = %s' % (draw['draw_type'], ','.join(str(int(v)) for v in draw['draw_params'])))
        sections['[CustomShaderFLMaterial_%s_%d]' % (tag, index)] = lines
    return sections


def overlay_scope(tag, index):
    prefix = 'fl_%s_%d' % (tag, index)
    return [
        '; FLOWLIGHT_MATERIAL_POST_BEGIN ' + tag,
        'if $fl_enable && $fl_character_seen_%s && $fl_material_pass_%s && @od && @o0 && @o1 && @o2 && @o3' % (tag, tag),
        '    local $%s_x = x244' % prefix,
        '    local $%s_y = y244' % prefix,
        '    x244 = %d' % material_magic(tag),
        '    y244 = time',
        '    run = CustomShaderFLMaterial_%s_%d' % (tag, index),
        '    x244 = $%s_x' % prefix,
        '    y244 = $%s_y' % prefix,
        'endif',
        '; FLOWLIGHT_MATERIAL_POST_END ' + tag,
    ]


def character_blocks(tag):
    def block(kind, lines):
        return ['; FLOWLIGHT_CHARACTER_%s_BEGIN %s' % (kind, tag)] + lines + ['; FLOWLIGHT_CHARACTER_%s_END %s' % (kind, tag)]
    return (
        block('CONSTANTS', ['global $fl_character_seen_%s = 0' % tag, 'global $fl_material_pass_%s = 0' % tag]),
        block('RESET', ['post $fl_character_seen_%s = 0' % tag]),
        block('MARK', ['$fl_character_seen_%s = 1' % tag]),
    )


def material_magic(tag):
    # All integers in this range are exactly representable in an ini float.
    return 0x800000 | (zlib.crc32(tag.encode('utf-8')) & 0x7fffff)


def _blob_bytes(pointer):
    table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
    address = ctypes.WINFUNCTYPE(ctypes.c_void_p, ctypes.c_void_p)(table[3])(pointer)
    size = ctypes.WINFUNCTYPE(ctypes.c_size_t, ctypes.c_void_p)(table[4])(pointer)
    return ctypes.string_at(address, size)


def _release(pointer):
    if pointer:
        table = ctypes.cast(pointer, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p))).contents
        ctypes.WINFUNCTYPE(ctypes.c_ulong, ctypes.c_void_p)(table[2])(pointer)


def _compile_pixel_shader(source):
    compiler = ctypes.WinDLL('d3dcompiler_47.dll')
    compiler.D3DCompile.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_char_p,
        ctypes.c_void_p, ctypes.c_void_p, ctypes.c_char_p, ctypes.c_char_p,
        ctypes.c_uint, ctypes.c_uint, ctypes.POINTER(ctypes.c_void_p), ctypes.POINTER(ctypes.c_void_p)]
    compiler.D3DDisassemble.argtypes = [ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint,
        ctypes.c_char_p, ctypes.POINTER(ctypes.c_void_p)]
    code, errors, assembly = ctypes.c_void_p(), ctypes.c_void_p(), ctypes.c_void_p()
    encoded = source.encode('utf-8')
    try:
        result = compiler.D3DCompile(encoded, len(encoded), b'flow_light/material.hlsl',
            None, None, b'main', b'ps_5_0', 8 | (1 << 15), 0, ctypes.byref(code), ctypes.byref(errors))
        if result < 0:
            raise RuntimeError(_blob_bytes(errors).decode('utf-8', errors='replace'))
        bytecode = _blob_bytes(code)
        result = compiler.D3DDisassemble(bytecode, len(bytecode), 0, None, ctypes.byref(assembly))
        if result < 0:
            raise RuntimeError('Cannot disassemble flow-light material fragment')
        text = _blob_bytes(assembly).decode('utf-8').rstrip('\0')
    finally:
        for pointer in (code, errors, assembly):
            _release(pointer)
    return bytecode, text


def compile_overlay(source):
    """Validate the shader actually exported, including VS/PS register linkage."""
    bytecode, text = _compile_pixel_shader(source)
    inputs = dict((int(semantic), int(register)) for semantic, register in re.findall(
        r'// TEXCOORD\s+(\d+)\s+\w+\s+(\d+)\s+NONE', text))
    outputs = dict((int(semantic), int(register)) for semantic, register in re.findall(
        r'// SV_Target\s+(\d+)\s+\w+\s+(\d+)\s+TARGET', text))
    if any(inputs.get(index) != index for index in (2, 3, 4)):
        raise ValueError('Flow overlay does not match native normal/world-position registers')
    if outputs != {index: index for index in range(4)}:
        raise ValueError('Flow overlay must preserve all four native MRTs')
    return bytecode, text
