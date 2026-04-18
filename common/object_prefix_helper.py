import re

from ..utils.ssmt_error_utils import SSMTErrorUtils


_PREFIX_START_PATTERN = re.compile(r"^[A-Za-z0-9]{6,}$")
_PREFIX_PART_PATTERN = re.compile(r"^[A-Za-z0-9]+$")
_KNOWN_SEPARATORS = ("__", "_", " ")
_PREFIX_CHECK_SEPARATORS = (".", "-", "_", " ", "__")


class ObjectPrefixHelper:
    @staticmethod
    def normalize_prefix(prefix: str) -> str:
        return (prefix or "").strip().strip("-_. ")

    @classmethod
    def _is_structured_prefix(cls, prefix_candidate: str) -> bool:
        clean_prefix = cls.normalize_prefix(prefix_candidate)
        if not clean_prefix:
            return False

        parsed = cls._extract_hyphen_prefix(clean_prefix)
        if not parsed:
            return False

        parsed_prefix, _ = parsed
        return parsed_prefix == clean_prefix

    @classmethod
    def _extract_hyphen_prefix(cls, object_name: str):
        parts = [part.strip() for part in object_name.split("-") if part.strip()]
        if len(parts) < 2:
            return None
        if not _PREFIX_START_PATTERN.fullmatch(parts[0]):
            return None

        prefix_parts = [parts[0]]
        for part in parts[1:3]:
            if not _PREFIX_PART_PATTERN.fullmatch(part):
                break
            prefix_parts.append(part)

        return "-".join(prefix_parts), "-"

    @classmethod
    def extract_prefix_info(cls, object_name: str):
        clean_name = (object_name or "").strip()
        if not clean_name:
            return None

        if "." in clean_name:
            prefix = cls.normalize_prefix(clean_name.split(".", 1)[0])
            if cls._is_structured_prefix(prefix):
                return prefix, "."

        for separator in _KNOWN_SEPARATORS:
            if separator not in clean_name:
                continue
            prefix = cls.normalize_prefix(clean_name.split(separator, 1)[0])
            if prefix:
                return prefix, separator

        return cls._extract_hyphen_prefix(clean_name)

    @classmethod
    def split_name_and_prefix(cls, object_name: str, prefix: str = "", separator: str = ""):
        clean_name = object_name or ""
        clean_prefix = cls.normalize_prefix(prefix)
        clean_separator = separator or "."

        if clean_prefix and clean_name.startswith(clean_prefix + clean_separator):
            return clean_prefix, clean_separator, clean_name[len(clean_prefix + clean_separator):]

        parsed = cls.extract_prefix_info(clean_name)
        if parsed:
            parsed_prefix, parsed_separator = parsed
            token = parsed_prefix + parsed_separator
            if clean_name.startswith(token):
                return parsed_prefix, parsed_separator, clean_name[len(token):]

        return "", clean_separator, clean_name

    @classmethod
    def has_prefix(cls, object_name: str, prefix: str) -> bool:
        clean_prefix = cls.normalize_prefix(prefix)
        if not clean_prefix:
            return False
        if object_name == clean_prefix:
            return True
        return any(object_name.startswith(clean_prefix + separator) for separator in _PREFIX_CHECK_SEPARATORS)

    @classmethod
    def replace_prefix(cls, object_name: str, new_prefix: str, separator: str = ".", old_prefix: str = "", old_separator: str = "") -> str:
        _, _, base_name = cls.split_name_and_prefix(object_name, old_prefix, old_separator)
        clean_prefix = cls.normalize_prefix(new_prefix)
        clean_separator = separator or "."
        if not clean_prefix:
            return base_name
        if not base_name:
            return clean_prefix
        return f"{clean_prefix}{clean_separator}{base_name}"

    @classmethod
    def parse_prefix_parts(cls, prefix: str) -> dict:
        clean_prefix = cls.normalize_prefix(prefix)
        parts = [part.strip() for part in clean_prefix.split("-") if part.strip()]
        return {
            "draw_ib": parts[0] if len(parts) >= 1 else "",
            "index_count": parts[1] if len(parts) >= 2 else "",
            "first_index": parts[2] if len(parts) >= 3 else "",
            "component": parts[1] if len(parts) >= 2 else "",
        }

    @classmethod
    def get_node_prefix_info(cls, node):
        object_name = getattr(node, "object_name", "")
        parsed_prefix_info = cls.extract_prefix_info(object_name)
        if parsed_prefix_info:
            return parsed_prefix_info

        stored_prefix = cls.normalize_prefix(getattr(node, "object_prefix", ""))
        stored_separator = getattr(node, "prefix_separator", "") or "."
        if stored_prefix:
            return stored_prefix, stored_separator

        return None

    @classmethod
    def get_node_prefix_info_with_source(cls, node):
        object_name = getattr(node, "object_name", "")
        parsed_prefix_info = cls.extract_prefix_info(object_name)
        if parsed_prefix_info:
            return parsed_prefix_info[0], parsed_prefix_info[1], "object_name"

        stored_prefix = cls.normalize_prefix(getattr(node, "object_prefix", ""))
        stored_separator = getattr(node, "prefix_separator", "") or "."
        if stored_prefix:
            return stored_prefix, stored_separator, "node_storage"

        return None

    @classmethod
    def require_node_prefix_info(cls, node):
        prefix_info = cls.get_node_prefix_info_with_source(node)
        if prefix_info:
            return prefix_info[0], prefix_info[1]

        object_name = getattr(node, "object_name", "") or getattr(node, "name", "<未命名节点>")
        SSMTErrorUtils.raise_fatal(
            f"物体 '{object_name}' 缺少前缀信息：既无法从物体名称解析前缀，也没有可用的节点内存储前缀"
        )

    @classmethod
    def build_virtual_object_name_for_node(cls, node, strict: bool = False) -> str:
        object_name = getattr(node, "object_name", "")
        prefix_info = cls.get_node_prefix_info_with_source(node)
        if not prefix_info and strict:
            prefix, separator = cls.require_node_prefix_info(node)
            prefix_info = (prefix, separator, "required")
        if not prefix_info:
            return object_name
        prefix, separator, source = prefix_info
        effective_separator = "." if source == "node_storage" else separator
        return cls.replace_prefix(object_name, prefix, effective_separator, prefix, separator)

    @classmethod
    def build_effective_object_name(cls, object_name: str, stored_prefix: str = "", stored_separator: str = ".", strict: bool = False) -> str:
        parsed_prefix_info = cls.extract_prefix_info(object_name)
        if parsed_prefix_info:
            return object_name

        clean_prefix = cls.normalize_prefix(stored_prefix)
        if clean_prefix:
            return cls.replace_prefix(object_name, clean_prefix, ".", clean_prefix, stored_separator or ".")

        if strict:
            SSMTErrorUtils.raise_fatal(
                f"物体 '{object_name or '<空名称>'}' 缺少前缀信息：既无法从物体名称解析前缀，也没有可用的节点内存储前缀"
            )

        return object_name

    @classmethod
    def resolve_source_object_name(cls, object_name: str) -> str:
        if not object_name:
            return object_name

        prefix, separator, base_name = cls.split_name_and_prefix(object_name)
        if prefix and separator == "." and base_name:
            return base_name

        return object_name