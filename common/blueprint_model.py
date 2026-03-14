
import math
import bpy
import copy

from ..config.main_config import GlobalConfig, LogicName

from ..utils.obj_utils import ObjUtils
from ..utils.log_utils import LOG
from ..utils.collection_utils import CollectionUtils, CollectionColor
from ..utils.config_utils import ConfigUtils
from ..utils.tips_utils import TipUtils

from .m_key import M_Key
from .draw_call_model import M_Condition
from .d3d11 import D3D11GameType
from .draw_call_model import DrawCallModel
from ..helper.global_key_count_helper import GlobalKeyCountHelper

from .obj_buffer_model_unity import ObjBufferModelUnity

from ..helper.blueprint_export_helper import BlueprintExportHelper

from ..blueprint_node.blueprint_node_obj import SSMTNode_Object_Group, SSMTNode_ToggleKey, SSMTNode_SwitchKey, SSMTNode_Object_Info, SSMTNode_Result_Output


class BluePrintModel:

    
    def __init__(self):
        # 全局按键名称和按键属性字典
        self.keyname_mkey_dict:dict[str,M_Key] = {} 

        # 全局obj_model列表，主要是obj_model里装了每个obj的生效条件。
        self.ordered_draw_obj_data_model_list:list[DrawCallModel] = [] 

        # 从输出节点开始递归解析所有的节点
        tree = BlueprintExportHelper.get_current_blueprint_tree()
        output_node = BlueprintExportHelper.get_node_from_bl_idname(tree, SSMTNode_Result_Output.bl_idname)
        self.parse_current_node(output_node, [])

    def parse_current_node(self, current_node:bpy.types.Node, chain_key_list:list[M_Key]):
        for unknown_node in BlueprintExportHelper.get_connected_nodes(current_node):
            self.parse_single_node(unknown_node, chain_key_list)

    def parse_single_node(self, unknown_node:bpy.types.Node, chain_key_list:list[M_Key]):
        '''
        这个是递归方法
        解析当前节点，获取其连接的所有节点的信息,分类进行解析
        '''
        
        if unknown_node.mute:
            return

        if unknown_node.bl_idname == SSMTNode_Object_Group.bl_idname:
            # 如果是单纯的分组节点，则不进行任何处理直接传递下去
            self.parse_current_node(unknown_node, chain_key_list)

        elif unknown_node.bl_idname == SSMTNode_ToggleKey.bl_idname:
            # 如果是按键开关节点，则添加一个Key，更新全局Key字典，更新Key列表并传递解析下去
            m_key = M_Key()
            current_add_key_index = len(self.keyname_mkey_dict.keys())
            m_key.key_name = "$swapkey" + str(GlobalKeyCountHelper.global_key_index)

            m_key.value_list = [0,1]

            # 设置键具体是哪个键，由用户指定
            m_key.initialize_vk_str = unknown_node.key_name

            # 设置是否默认开启
            if unknown_node.default_on:
                m_key.initialize_value = 1
            else:
                m_key.initialize_value = 0
            
            # 设置备注信息
            m_key.comment = getattr(unknown_node, 'comment', '')
            
            # 创建的key加入全局key列表
            self.keyname_mkey_dict[m_key.key_name] = m_key

            if len(self.keyname_mkey_dict.keys()) > current_add_key_index:
                GlobalKeyCountHelper.global_key_index = GlobalKeyCountHelper.global_key_index + 1
            
            # 创建的key要加入chain_key_list传递下去
            # 因为传递解析下去的话，要让这个key生效，而又因为它是按键开关key，所以value为1生效，所以tmp_value设为1
            chain_tmp_key = copy.deepcopy(m_key)
            chain_tmp_key.tmp_value = 1

            tmp_chain_key_list = copy.deepcopy(chain_key_list)
            tmp_chain_key_list.append(chain_tmp_key)

            # 递归解析
            self.parse_current_node(unknown_node, tmp_chain_key_list)

        elif unknown_node.bl_idname == SSMTNode_SwitchKey.bl_idname:
            # 如果是按键切换节点，则该节点所有的分支节点，并逐个处理
            # 这里我们直接遍历所有的inputs，而不是get_connected_nodes，
            # 因为get_connected_nodes会忽略未连接(空)的端口，导致分支数量计算错误
            
            # 获取有效的分支数量（除去最后一个为了方便添加而存在的空端口）
            # 只有当最后一个端口确实没有连接的时候才能排除，虽然Node定义里是这样写的逻辑，但最好判断一下link
            # valid_input_sockets = unknown_node.inputs[:-1] if (len(unknown_node.inputs) > 1 and not unknown_node.inputs[-1].is_linked) else unknown_node.inputs[:]
            
            # 修正：对于SwitchKey节点，所有的Input都是有效的分支，因为可以手动添加/删除Socket，且空Socket代表空状态（什么都不显示）
            valid_input_sockets = unknown_node.inputs[:]
            
            # 如果所有端口都没有连接，则直接跳过
            is_all_socket_linked = False
            for sock in valid_input_sockets:
                if sock.is_linked:
                    is_all_socket_linked = True
                    break
            
            if not is_all_socket_linked:
                # 如果没有任何连接，不做处理
                return

            if len(valid_input_sockets) == 1:
                # 如果只有 1 个有效分支端口：
                # 1. 如果它是连接的 -> 视为 Group 节点透传
                # 2. 如果它是断开的 -> 视为无意义，不做处理(上面all_socket_linked已过滤)
                if valid_input_sockets[0].is_linked:
                        for link in valid_input_sockets[0].links:
                            self.parse_single_node(link.from_node, chain_key_list)
            else:
                # 如果有 > 1 个有效分支端口，则必须创建 Key，哪怕某些端口是空的（代表空分支）
                m_key = M_Key()
                current_add_key_index = len(self.keyname_mkey_dict.keys())
                m_key.key_name = "$swapkey" + str(GlobalKeyCountHelper.global_key_index)

                # 值列表就是分支索引的列表 [0, 1, 2, ...]
                m_key.value_list = list(range(len(valid_input_sockets)))

                m_key.initialize_vk_str = unknown_node.key_name
                m_key.initialize_value = 0  # 默认选择第一个分支

                # 设置备注信息
                m_key.comment = getattr(unknown_node, 'comment', '')

                # 创建的key加入全局key列表
                self.keyname_mkey_dict[m_key.key_name] = m_key

                # 更新全局key索引
                if len(self.keyname_mkey_dict.keys()) > current_add_key_index:
                    GlobalKeyCountHelper.global_key_index = GlobalKeyCountHelper.global_key_index + 1

                # 逐个处理每个分支节点（包括空分支）
                key_tmp_value = 0
                for socket in valid_input_sockets:
                    # 无论这个 socket 是否连接了节点，或者是空的，都对应一个 key value
                    
                    if socket.is_linked:
                        # 如果连接了节点，则需要把这个 value 对应的 key 传递下去解析
                        for link in socket.links:
                            # 为每个分支创建一个临时key传递下去
                            chain_tmp_key = copy.deepcopy(m_key)
                            chain_tmp_key.tmp_value = key_tmp_value # 当前分支对应的 value

                            tmp_chain_key_list = copy.deepcopy(chain_key_list)
                            tmp_chain_key_list.append(chain_tmp_key)

                            # 递归解析连接的节点
                            # 注意：这里我们调用 parse_single_node，因为我们直接找到了目标节点
                            self.parse_single_node(link.from_node, tmp_chain_key_list)
                    else:
                        # 如果是空端口（没有连接），则代表这个 value 对应的是空物体
                        # 我们不需要做任何 parse 操作，因为没有任何 obj 需要在这个条件下生成
                        # 这个 key value 存在于 key.value_list 中，但没有任何 obj 的 condition 会匹配到这个 value
                        # 这样就实现了“切换到这个分支时，什么都不显示”的效果
                        pass

                    key_tmp_value = key_tmp_value + 1


        elif unknown_node.bl_idname == SSMTNode_Object_Info.bl_idname:
            obj_model = DrawCallModel(obj_name=unknown_node.object_name)
            
            if hasattr(unknown_node, 'original_object_name') and unknown_node.original_object_name:
                obj_model.display_name = unknown_node.original_object_name

            obj_model.condition = M_Condition(work_key_list=copy.deepcopy(chain_key_list))
            
            self.ordered_draw_obj_data_model_list.append(obj_model)




                

                



