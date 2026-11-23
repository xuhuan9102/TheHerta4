"""Lossless PNG channel expansion for texture delivery paths."""

import os
import shutil
import struct
import tempfile
import zlib


PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
_PASS_THROUGH_CHUNKS = {
    b"gAMA",
    b"sRGB",
    b"cHRM",
    b"cICP",
    b"iCCP",
    b"eXIf",
    b"oFFs",
    b"pHYs",
    b"tEXt",
    b"zTXt",
    b"iTXt",
    b"tIME",
}
_DROP_CHUNKS = {
    b"IHDR",
    b"PLTE",
    b"tRNS",
    b"bKGD",
    b"hIST",
    b"sBIT",
    b"IDAT",
    b"IEND",
}


def _chunk(kind, payload):
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def _same_path(first, second):
    try:
        if os.path.exists(first) and os.path.exists(second):
            return os.path.samefile(first, second)
    except OSError:
        pass
    first_key = os.path.normcase(os.path.realpath(os.path.abspath(first)))
    second_key = os.path.normcase(os.path.realpath(os.path.abspath(second)))
    return first_key == second_key


def _read_chunks(data):
    if not data.startswith(PNG_SIGNATURE):
        raise ValueError("not a PNG file")
    chunks = []
    position = len(PNG_SIGNATURE)
    while position + 12 <= len(data):
        length = struct.unpack_from(">I", data, position)[0]
        end = position + 12 + length
        if end > len(data):
            raise ValueError("truncated PNG chunk")
        kind = data[position + 4:position + 8]
        payload = data[position + 8:position + 8 + length]
        chunks.append((kind, payload))
        position = end
        if kind == b"IEND":
            break
    if not chunks or chunks[0][0] != b"IHDR":
        raise ValueError("PNG has no IHDR")
    return chunks


def png_info(path):
    with open(path, "rb") as handle:
        header = handle.read(29)
    if len(header) < 29 or not header.startswith(PNG_SIGNATURE):
        return None
    width, height, bit_depth, color_type, compression, filtering, interlace = (
        struct.unpack(">IIBBBBB", header[16:29])
    )
    return {
        "width": width,
        "height": height,
        "bit_depth": bit_depth,
        "color_type": color_type,
        "compression": compression,
        "filtering": filtering,
        "interlace": interlace,
    }


def validate_png_rgba(path):
    """Reject truncated or malformed RGBA PNGs before publishing them."""
    with open(path, "rb") as handle:
        data = handle.read()
    chunks = _read_chunks(data)
    header = next((payload for kind, payload in chunks if kind == b"IHDR"), None)
    if header is None or len(header) != 13:
        raise ValueError("invalid PNG IHDR")
    (
        width,
        height,
        bit_depth,
        color_type,
        compression,
        filtering,
        interlace,
    ) = struct.unpack(">IIBBBBB", header)
    if color_type != 6 or bit_depth not in (8, 16):
        raise ValueError("PNG is not 8/16-bit RGBA")
    if compression != 0 or filtering != 0:
        raise ValueError("unsupported PNG compression or filter method")
    compressed = b"".join(payload for kind, payload in chunks if kind == b"IDAT")
    if not compressed:
        raise ValueError("PNG has no IDAT")
    raw = zlib.decompress(compressed)
    if not interlace:
        bytes_per_sample = bit_depth // 8
        expected = (width * 4 * bytes_per_sample + 1) * height
        if len(raw) != expected:
            raise ValueError("PNG scanline size mismatch")
    return {
        "width": width,
        "height": height,
        "bit_depth": bit_depth,
        "color_type": color_type,
        "interlace": interlace,
    }


def _unfilter_rows(raw, width, height, bit_depth, channels):
    bytes_per_pixel = max(1, (bit_depth * channels + 7) // 8)
    bytes_per_row = (width * bit_depth * channels + 7) // 8
    expected = (bytes_per_row + 1) * height
    if len(raw) < expected:
        raise ValueError("truncated PNG image data")
    rows = []
    position = 0
    previous = bytearray(bytes_per_row)
    for _ in range(height):
        filter_type = raw[position]
        source = raw[position + 1:position + 1 + bytes_per_row]
        position += bytes_per_row + 1
        row = bytearray(bytes_per_row)
        if filter_type == 0:
            row[:] = source
        elif filter_type == 1:
            for index, value in enumerate(source):
                left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                row[index] = (value + left) & 0xFF
        elif filter_type == 2:
            for index, value in enumerate(source):
                row[index] = (value + previous[index]) & 0xFF
        elif filter_type == 3:
            for index, value in enumerate(source):
                left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                row[index] = (value + ((left + previous[index]) >> 1)) & 0xFF
        elif filter_type == 4:
            for index, value in enumerate(source):
                left = row[index - bytes_per_pixel] if index >= bytes_per_pixel else 0
                upper = previous[index]
                upper_left = (
                    previous[index - bytes_per_pixel]
                    if index >= bytes_per_pixel
                    else 0
                )
                predictor = left + upper - upper_left
                pa = abs(predictor - left)
                pb = abs(predictor - upper)
                pc = abs(predictor - upper_left)
                if pa <= pb and pa <= pc:
                    predicted = left
                elif pb <= pc:
                    predicted = upper
                else:
                    predicted = upper_left
                row[index] = (value + predicted) & 0xFF
        else:
            raise ValueError("unsupported PNG filter %d" % filter_type)
        rows.append(bytes(row))
        previous = row
    return rows


def _interlaced_rows(raw, width, height, bit_depth, channels):
    passes = (
        (0, 0, 8, 8),
        (4, 0, 8, 8),
        (0, 4, 4, 8),
        (2, 0, 4, 4),
        (0, 2, 2, 4),
        (1, 0, 2, 2),
        (0, 1, 1, 2),
    )
    bytes_per_pixel = channels * (bit_depth // 8)
    full_row_bytes = width * bytes_per_pixel
    rows = [bytearray(full_row_bytes) for _ in range(height)]
    position = 0
    for start_x, start_y, step_x, step_y in passes:
        pass_width = (width - start_x + step_x - 1) // step_x if width > start_x else 0
        pass_height = (height - start_y + step_y - 1) // step_y if height > start_y else 0
        if not pass_width or not pass_height:
            continue
        pass_row_bytes = pass_width * bytes_per_pixel
        consumed = (pass_row_bytes + 1) * pass_height
        pass_rows = _unfilter_rows(
            raw[position:position + consumed],
            pass_width,
            pass_height,
            bit_depth,
            channels,
        )
        position += consumed
        for pass_y, pass_row in enumerate(pass_rows):
            target_y = start_y + pass_y * step_y
            target_row = rows[target_y]
            for pass_x in range(pass_width):
                target_x = start_x + pass_x * step_x
                source_offset = pass_x * bytes_per_pixel
                target_offset = target_x * bytes_per_pixel
                target_row[target_offset:target_offset + bytes_per_pixel] = (
                    pass_row[source_offset:source_offset + bytes_per_pixel]
                )
    return [bytes(row) for row in rows]


def _packed_samples(row, bit_depth, count):
    if bit_depth == 8:
        return list(row[:count])
    if bit_depth == 16:
        return [
            struct.unpack_from(">H", row, index * 2)[0]
            for index in range(count)
        ]
    mask = (1 << bit_depth) - 1
    samples = []
    for index in range(count):
        bit_offset = index * bit_depth
        shift = 8 - bit_depth - (bit_offset & 7)
        samples.append((row[bit_offset >> 3] >> shift) & mask)
    return samples


def _scale_sample(value, bit_depth):
    maximum = (1 << bit_depth) - 1
    if bit_depth == 8:
        return value
    if bit_depth < 8:
        return (value * 255 + maximum // 2) // maximum
    return value


def _append_sample(output, value, bit_depth):
    if bit_depth == 8:
        output.append(value & 0xFF)
    else:
        output.extend(struct.pack(">H", value & 0xFFFF))


def _expand_rgba_payload(chunks):
    header = next(payload for kind, payload in chunks if kind == b"IHDR")
    (
        width,
        height,
        bit_depth,
        color_type,
        compression,
        filtering,
        interlace,
    ) = struct.unpack(">IIBBBBB", header)
    if compression != 0 or filtering != 0:
        raise ValueError("unsupported PNG compression or filter method")
    if color_type == 6:
        return None
    if color_type not in (0, 2, 3, 4):
        raise ValueError("unsupported PNG color type %d" % color_type)
    if bit_depth not in (1, 2, 4, 8, 16):
        raise ValueError("unsupported PNG bit depth %d" % bit_depth)
    if color_type in (2, 4, 6) and bit_depth < 8:
        raise ValueError("invalid RGB PNG bit depth")

    channels = {0: 1, 2: 3, 3: 1, 4: 2}[color_type]
    compressed = b"".join(payload for kind, payload in chunks if kind == b"IDAT")
    raw = zlib.decompress(compressed)
    rows = (
        _interlaced_rows(raw, width, height, bit_depth, channels)
        if interlace
        else _unfilter_rows(raw, width, height, bit_depth, channels)
    )
    palette = next((payload for kind, payload in chunks if kind == b"PLTE"), b"")
    transparency = next(
        (payload for kind, payload in chunks if kind == b"tRNS"), b""
    )
    if color_type == 3 and not palette:
        raise ValueError("indexed PNG has no palette")

    output_depth = 8 if bit_depth < 8 else bit_depth
    maximum = (1 << output_depth) - 1
    rgba_rows = []
    if color_type == 0 and transparency:
        transparent_value = (
            struct.unpack(">H", transparency[:2])[0] & ((1 << bit_depth) - 1)
        )
    elif color_type == 2 and transparency:
        transparent_value = tuple(
            value & ((1 << bit_depth) - 1)
            for value in struct.unpack(">HHH", transparency[:6])
        )
    else:
        transparent_value = None

    for row in rows:
        output = bytearray()
        if color_type == 3:
            for index in _packed_samples(row, bit_depth, width):
                palette_offset = index * 3
                if palette_offset + 3 > len(palette):
                    raise ValueError("indexed PNG palette is too short")
                output.extend(palette[palette_offset:palette_offset + 3])
                alpha = transparency[index] if index < len(transparency) else 255
                output.append(alpha)
        elif color_type == 0:
            for value in _packed_samples(row, bit_depth, width):
                gray = _scale_sample(value, bit_depth)
                alpha = 0 if value == transparent_value else maximum
                _append_sample(output, gray, output_depth)
                _append_sample(output, gray, output_depth)
                _append_sample(output, gray, output_depth)
                _append_sample(output, alpha, output_depth)
        elif color_type == 2:
            samples = _packed_samples(row, bit_depth, width * 3)
            for pixel in range(width):
                red, green, blue = samples[pixel * 3:pixel * 3 + 3]
                alpha = (
                    0
                    if transparent_value == (red, green, blue)
                    else maximum
                )
                for value in (red, green, blue, alpha):
                    _append_sample(output, value, output_depth)
        else:
            samples = _packed_samples(row, bit_depth, width * 2)
            for pixel in range(width):
                gray = samples[pixel * 2]
                alpha = samples[pixel * 2 + 1]
                for value in (gray, gray, gray, alpha):
                    _append_sample(output, value, output_depth)
        rgba_rows.append(b"\x00" + bytes(output))

    filtered = b"".join(rgba_rows)
    return output_depth, zlib.compress(filtered, level=6)


def icc_profile_color_space(profile):
    return profile[16:20] if len(profile) >= 20 else None


def _embedded_icc_profile_color_space(payload):
    try:
        separator = payload.index(0)
        if separator + 2 > len(payload) or payload[separator + 1] != 0:
            return None
        profile = zlib.decompress(payload[separator + 2:])
    except (ValueError, zlib.error):
        return None
    return icc_profile_color_space(profile)


def _preserved_ancillary_chunks(chunks):
    preserved = []
    for kind, payload in chunks:
        if kind in _PASS_THROUGH_CHUNKS:
            if (
                kind == b"iCCP"
                and _embedded_icc_profile_color_space(payload) != b"RGB "
            ):
                continue
            preserved.append((kind, payload))
            continue
        if kind in _DROP_CHUNKS:
            continue
        if kind and not (kind[0] & 0x20):
            raise ValueError("unsupported critical PNG chunk %r" % kind)
    return preserved


def _normalize_png(source, destination):
    with open(source, "rb") as handle:
        data = handle.read()
    chunks = _read_chunks(data)
    header = next(payload for kind, payload in chunks if kind == b"IHDR")
    width, height, bit_depth, color_type, _, _, interlace = struct.unpack(
        ">IIBBBBB", header
    )
    if color_type == 6:
        if not _same_path(source, destination):
            shutil.copy2(source, destination)
        return False

    if interlace and bit_depth < 8:
        try:
            from PIL import Image
        except ImportError as exc:
            raise ValueError("interlaced PNG requires Pillow") from exc
        directory = os.path.dirname(os.path.abspath(destination)) or "."
        os.makedirs(directory, exist_ok=True)
        handle, temporary = tempfile.mkstemp(
            prefix=".herta_rgba_", suffix=".tmp", dir=directory
        )
        os.close(handle)
        try:
            with Image.open(source) as image:
                profile = image.info.get("icc_profile")
                save_options = {"format": "PNG"}
                if profile and icc_profile_color_space(profile) == b"RGB ":
                    save_options["icc_profile"] = profile
                image.convert("RGBA").save(temporary, **save_options)
            with open(temporary, "rb") as stream:
                converted = _read_chunks(stream.read())
            converted_header = next(
                payload for kind, payload in converted if kind == b"IHDR"
            )
            rebuilt = bytearray(PNG_SIGNATURE)
            rebuilt.extend(_chunk(b"IHDR", converted_header))
            for kind, payload in _preserved_ancillary_chunks(chunks):
                rebuilt.extend(_chunk(kind, payload))
            for kind, payload in converted:
                if kind == b"IDAT":
                    rebuilt.extend(_chunk(kind, payload))
            rebuilt.extend(_chunk(b"IEND", b""))
            with open(temporary, "wb") as stream:
                stream.write(rebuilt)
            os.replace(temporary, destination)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)
        return True

    expanded = _expand_rgba_payload(chunks)
    if expanded is None:
        if not _same_path(source, destination):
            shutil.copy2(source, destination)
        return False
    output_depth, compressed = expanded
    new_header = struct.pack(
        ">IIBBBBB", width, height, output_depth, 6, 0, 0, 0
    )
    output = bytearray(PNG_SIGNATURE)
    output.extend(_chunk(b"IHDR", new_header))
    for kind, payload in _preserved_ancillary_chunks(chunks):
        output.extend(_chunk(kind, payload))
    output.extend(_chunk(b"IDAT", compressed))
    output.extend(_chunk(b"IEND", b""))

    directory = os.path.dirname(os.path.abspath(destination)) or "."
    os.makedirs(directory, exist_ok=True)
    handle, temporary = tempfile.mkstemp(
        prefix=".herta_rgba_", suffix=".tmp", dir=directory
    )
    os.close(handle)
    try:
        with open(temporary, "wb") as stream:
            stream.write(output)
        os.replace(temporary, destination)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def ensure_png_rgba(source, destination=None):
    """Copy or rewrite a PNG so its IHDR color type is RGBA.

    Non-PNG files are copied unchanged. Existing RGBA PNGs preserve their exact
    bytes. RGB, grayscale, indexed, and grayscale-alpha PNGs are expanded without
    changing sample precision.
    """
    source = os.fspath(source)
    destination = os.fspath(destination or source)
    with open(source, "rb") as handle:
        header = handle.read(26)
    if not header.startswith(PNG_SIGNATURE):
        if not _same_path(source, destination):
            shutil.copy2(source, destination)
        return False
    if len(header) >= 26 and header[25] == 6:
        if not _same_path(source, destination):
            shutil.copy2(source, destination)
        return False
    return _normalize_png(source, destination)


def copy_png_rgba(source, destination):
    """Write an RGBA PNG copy while leaving an identical source path untouched."""
    source = os.fspath(source)
    destination = os.fspath(destination)
    if _same_path(source, destination):
        return False
    return ensure_png_rgba(source, destination)
