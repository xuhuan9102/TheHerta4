def _buffer_to_bytes(buffer_data) -> bytes:
    if buffer_data is None:
        return b""
    if isinstance(buffer_data, bytes):
        return buffer_data
    if hasattr(buffer_data, "tobytes"):
        return buffer_data.tobytes()
    return bytes(buffer_data)


class ShapeKeyDirectExportError(RuntimeError):
    pass
