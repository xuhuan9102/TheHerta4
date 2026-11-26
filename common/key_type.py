from .global_config import GlobalConfig
from .logic_name import LogicName


def one_shot_key_type():
    """Return the loader-compatible one-shot key enum for the current preset."""
    if str(GlobalConfig.logic_name or "").strip().upper() == LogicName.APMI:
        return "activate"
    return "press"
