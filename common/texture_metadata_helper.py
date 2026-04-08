import os
from dataclasses import dataclass, field

from .submesh_metadata import SubmeshMetadataResolver


@dataclass
class TextureMarkUpInfo:
    mark_name:str = field(default="",init=False)
    mark_type:str = field(default="",init=False)
    mark_hash:str = field(default="",init=False)
    mark_slot:str = field(default="",init=False)
    mark_filename:str = field(default="",init=False)

    def get_resource_name(self):
        return "Resource-" + self.mark_filename.split(".")[0]

    def get_hash_style_filename(self):
        return self.mark_hash + "-" + self.mark_name + "." + self.mark_filename.split(".")[1]


class TextureMetadataResolver:
    @staticmethod
    def _get_texture_markup_info_identity(texture_info) -> tuple:
        return (
            getattr(texture_info, "mark_name", ""),
            getattr(texture_info, "mark_type", ""),
            getattr(texture_info, "mark_hash", ""),
            getattr(texture_info, "mark_slot", ""),
            getattr(texture_info, "mark_filename", ""),
        )

    @staticmethod
    def _dedupe_texture_markup_info_list(texture_info_list: list) -> list:
        deduped_texture_info_list = []
        seen_identities = set()

        for texture_info in texture_info_list:
            identity = TextureMetadataResolver._get_texture_markup_info_identity(texture_info)
            if identity in seen_identities:
                continue
            seen_identities.add(identity)
            deduped_texture_info_list.append(texture_info)

        return deduped_texture_info_list

    @staticmethod
    def normalize_texture_markup_info_list(texture_info_list: list) -> list:
        normalized_texture_info_list = []

        for texture_info in texture_info_list:
            if isinstance(texture_info, TextureMarkUpInfo):
                normalized_texture_info_list.append(texture_info)
                continue

            if not isinstance(texture_info, dict):
                continue

            markup_info = TextureMarkUpInfo()
            markup_info.mark_name = texture_info.get("MarkName", texture_info.get("mark_name", ""))
            markup_info.mark_type = texture_info.get("MarkType", texture_info.get("mark_type", ""))
            markup_info.mark_hash = texture_info.get("MarkHash", texture_info.get("mark_hash", ""))
            markup_info.mark_slot = texture_info.get("MarkSlot", texture_info.get("mark_slot", ""))
            markup_info.mark_filename = texture_info.get("MarkFileName", texture_info.get("mark_filename", ""))
            normalized_texture_info_list.append(markup_info)

        return normalized_texture_info_list

    @staticmethod
    def normalize_texture_markup_info_dict(raw_texture_info_dict: dict) -> dict:
        normalized_texture_info_dict = {}

        for part_name, texture_info_list in raw_texture_info_dict.items():
            normalized_texture_info_dict[part_name] = TextureMetadataResolver.normalize_texture_markup_info_list(texture_info_list)

        return normalized_texture_info_dict

    @staticmethod
    def get_partname_texturemarkinfolist_dict(draw_ib_model) -> dict:
        texture_info_dict = getattr(draw_ib_model, "partname_texturemarkinfolist_dict", None)
        if texture_info_dict is None:
            return {}

        return TextureMetadataResolver.normalize_texture_markup_info_dict(texture_info_dict)

    @staticmethod
    def get_submesh_texturemarkinfolist_dict(draw_ib_model) -> dict:
        texture_info_dict = getattr(draw_ib_model, "submesh_texturemarkinfolist_dict", None)
        if texture_info_dict is None:
            return {}

        return {
            unique_str: TextureMetadataResolver.normalize_texture_markup_info_list(texture_info_list)
            for unique_str, texture_info_list in texture_info_dict.items()
        }

    @staticmethod
    def get_part_name_for_submesh(draw_ib_model, submesh_model) -> str:
        get_part_name = getattr(draw_ib_model, "get_part_name_by_match_first_index", None)
        if callable(get_part_name):
            return get_part_name(submesh_model.match_first_index) or ""

        partname_dict = getattr(draw_ib_model, "match_first_index_partname_dict", {})
        try:
            return partname_dict.get(int(submesh_model.match_first_index), "")
        except (TypeError, ValueError):
            return ""

    @staticmethod
    def load_texture_markup_info_for_submesh(draw_ib_model, submesh_model) -> tuple[str, list]:
        unique_str = submesh_model.unique_str
        part_name = TextureMetadataResolver.get_part_name_for_submesh(draw_ib_model, submesh_model)

        try:
            submesh_metadata = SubmeshMetadataResolver.resolve(unique_str)
        except Exception as ex:
            print("TextureMetadataResolver: 跳过贴图标记读取，无法解析 SubmeshJson: " + unique_str + "，错误: " + str(ex))
            return part_name, []

        print(
            "TextureMetadataResolver: 读取贴图标记，unique_str: "
            + unique_str
            + "，part_name: "
            + str(part_name)
            + "，submesh_json: "
            + submesh_metadata.submesh_json_path
        )

        texture_markup_info_list = TextureMetadataResolver.normalize_texture_markup_info_list(
            submesh_metadata.texture_markup_info_list
        )
        texture_markup_info_list = TextureMetadataResolver._dedupe_texture_markup_info_list(texture_markup_info_list)

        if not texture_markup_info_list:
            print("TextureMetadataResolver: 当前 submesh 没有贴图标记: " + unique_str)
            return part_name, []

        if texture_markup_info_list:
            print(
                "TextureMetadataResolver: 当前 submesh 已匹配到贴图标记，unique_str: "
                + unique_str
                + "，数量: "
                + str(len(texture_markup_info_list))
            )

        return part_name, texture_markup_info_list

    @staticmethod
    def load_submesh_texture_markup_info_from_all_submeshes(draw_ib_model, workspace_import_json: dict | None = None) -> dict:
        submesh_texture_markup_info_dict = {}

        for submesh_model in draw_ib_model.submesh_model_list:
            _, texture_markup_info_list = TextureMetadataResolver.load_texture_markup_info_for_submesh(
                draw_ib_model=draw_ib_model,
                submesh_model=submesh_model,
            )
            if texture_markup_info_list:
                submesh_texture_markup_info_dict[submesh_model.unique_str] = texture_markup_info_list

        return submesh_texture_markup_info_dict

    @staticmethod
    def load_texture_markup_info_from_all_submeshes(draw_ib_model, workspace_import_json: dict | None = None) -> dict:
        merged_texture_markup_info_dict = {}

        for submesh_model in draw_ib_model.submesh_model_list:
            part_name, texture_markup_info_list = TextureMetadataResolver.load_texture_markup_info_for_submesh(
                draw_ib_model=draw_ib_model,
                submesh_model=submesh_model,
            )

            if not part_name or not texture_markup_info_list:
                continue

            merged_texture_markup_info_dict[part_name] = TextureMetadataResolver._dedupe_texture_markup_info_list(
                merged_texture_markup_info_dict.get(part_name, []) + texture_markup_info_list
            )
            print(
                "TextureMetadataResolver: 已合并贴图标记到 Part "
                + part_name
                + "，数量: "
                + str(len(merged_texture_markup_info_dict[part_name]))
            )

        return merged_texture_markup_info_dict