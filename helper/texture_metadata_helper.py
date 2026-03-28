import os

from ..base.config.main_config import GlobalConfig
from ..base.utils.json_utils import JsonUtils
from .import_config import TextureMarkUpInfo


class TextureMetadataResolver:
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
    def get_import_json_path_by_unique_str(workspace_import_json: dict, unique_str: str) -> str:
        gametype_name = workspace_import_json.get(unique_str, "")
        if not gametype_name:
            return ""

        return os.path.join(
            GlobalConfig.path_workspace_folder(),
            unique_str,
            "TYPE_" + gametype_name,
            "import.json",
        )

    @staticmethod
    def get_part_name_for_submesh(draw_ib_model, submesh_model) -> str:
        if submesh_model.match_first_index in draw_ib_model.match_first_index_list:
            part_index = draw_ib_model.match_first_index_list.index(submesh_model.match_first_index)
            if part_index < len(draw_ib_model.part_name_list):
                return draw_ib_model.part_name_list[part_index]

        return ""

    @staticmethod
    def load_texture_markup_info_from_all_submeshes(draw_ib_model, workspace_import_json: dict) -> dict:
        merged_texture_markup_info_dict = {}

        for submesh_model in draw_ib_model.submesh_model_list:
            unique_str = submesh_model.unique_str
            part_name = TextureMetadataResolver.get_part_name_for_submesh(draw_ib_model, submesh_model)
            import_json_path = TextureMetadataResolver.get_import_json_path_by_unique_str(workspace_import_json, unique_str)

            print(
                "TextureMetadataResolver: 读取贴图标记，unique_str: "
                + unique_str
                + "，part_name: "
                + str(part_name)
                + "，import.json: "
                + import_json_path
            )

            if not import_json_path or not os.path.exists(import_json_path):
                print("TextureMetadataResolver: 跳过贴图标记读取，import.json 不存在: " + import_json_path)
                continue

            submesh_import_json_dict = JsonUtils.LoadFromFile(import_json_path)
            raw_texture_markup_info_dict = submesh_import_json_dict.get("ComponentTextureMarkUpInfoListDict", {})
            normalized_texture_markup_info_dict = TextureMetadataResolver.normalize_texture_markup_info_dict(raw_texture_markup_info_dict)

            if not normalized_texture_markup_info_dict:
                print("TextureMetadataResolver: 当前 submesh 没有贴图标记: " + unique_str)
                continue

            if part_name:
                texture_markup_info_list = normalized_texture_markup_info_dict.get(part_name)
                if texture_markup_info_list is None:
                    texture_markup_info_list = normalized_texture_markup_info_dict.get(unique_str, [])

                if texture_markup_info_list:
                    merged_texture_markup_info_dict[part_name] = texture_markup_info_list
                    print(
                        "TextureMetadataResolver: 已合并贴图标记到 Part "
                        + part_name
                        + "，数量: "
                        + str(len(texture_markup_info_list))
                    )
                else:
                    print(
                        "TextureMetadataResolver: 当前 submesh 的贴图标记键未匹配到 Part，unique_str: "
                        + unique_str
                        + "，可用键: "
                        + str(list(normalized_texture_markup_info_dict.keys()))
                    )
            else:
                print("TextureMetadataResolver: 当前 submesh 未匹配到 PartName，unique_str: " + unique_str)

        return merged_texture_markup_info_dict