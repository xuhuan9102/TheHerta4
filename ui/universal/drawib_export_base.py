from ...blueprint.model import BluePrintModel
from ...common.drawib_model import DrawIBModel
from ...common.buffer_export_helper import BufferExportHelper
from .export_helper import ExportHelper

import os


class DrawIBExportBase:
    def __init__(self, blueprint_model: BluePrintModel, combine_ib: bool = False):
        self.blueprint_model = blueprint_model
        self.drawib_model_list: list[DrawIBModel] = ExportHelper.parse_drawib_model_list_from_blueprint_model(
            blueprint_model=blueprint_model,
            combine_ib=combine_ib,
        )

    def generate_buffer_files(self, output_folder: str):
        for drawib_model in self.drawib_model_list:
            draw_ib = drawib_model.draw_ib

            if drawib_model.combine_ib:
                ib_filename = draw_ib + "-Index.buf"
                ib_filepath = os.path.join(output_folder, ib_filename)
                BufferExportHelper.write_buf_ib_r32_uint(drawib_model.ib, ib_filepath)
            else:
                for submesh_model in drawib_model.submesh_model_list:
                    ib = drawib_model.submesh_ib_dict.get(submesh_model.unique_str, [])
                    ib_filename = submesh_model.unique_str + "-Index.buf"
                    ib_filepath = os.path.join(output_folder, ib_filename)
                    BufferExportHelper.write_buf_ib_r32_uint(ib, ib_filepath)

            for category, category_buf in drawib_model.category_buffer_dict.items():
                category_buf_filename = draw_ib + "-" + category + ".buf"
                category_buf_filepath = os.path.join(output_folder, category_buf_filename)
                with open(category_buf_filepath, 'wb') as file_obj:
                    category_buf.tofile(file_obj)

            for shapekey_name, shapekey_buf in drawib_model.shapekey_name_bytelist_dict.items():
                shapekey_buf_filename = draw_ib + "-Position." + shapekey_name + ".buf"
                shapekey_buf_filepath = os.path.join(output_folder, shapekey_buf_filename)
                with open(shapekey_buf_filepath, 'wb') as file_obj:
                    shapekey_buf.tofile(file_obj)

    def export_buffers_only(self):
        """只导出 Buffer 文件，不生成 INI 配置"""
        from ...common.global_config import GlobalConfig
        from ...utils.timer_utils import TimerUtils
        TimerUtils.start_stage("缓冲文件生成")
        self.generate_buffer_files(GlobalConfig.path_generatemod_buffer_folder())
        TimerUtils.end_stage("缓冲文件生成")

    def _integrate_object_swap_ini_hook(self, ini_builder):
        try:
            from ...blueprint.node_swap_ini import SwapKeyINIIntegrator

            blueprint_tree = getattr(self.blueprint_model, '_tree', None)
            if not blueprint_tree:
                from ...blueprint.export_helper import BlueprintExportHelper
                blueprint_tree = BlueprintExportHelper.get_current_blueprint_tree()

            if not blueprint_tree:
                return

            registry = getattr(self.blueprint_model, '_swap_key_registry', None)

            SwapKeyINIIntegrator.integrate_to_export(ini_builder, blueprint_tree, registry=registry)

        except ImportError:
            pass
        except Exception as e:
            try:
                from ...utils.log_utils import LOG
                LOG.warning(f"⚠️ 物体切换节点 INI 集成钩子执行失败: {e}")
            except:
                pass

    def export(self):
        raise NotImplementedError()