import bpy

from ..common.global_config import GlobalConfig

class CollectionColor:
    '''
    WorkSpaceCollectionColor 【工作空间集合】
    DrawIBCollectionColor 【DrawIB集合】
    ComponentCollectionColor 【Component集合】
    
    GroupCollection 【组集合】
    ToggleCollection 【按键开关集合】
    SwitchCollection 【按键切换集合】

    '''
    White = "NONE"
    Red = "COLOR_01"
    Orange = "COLOR_02"
    Yellow = "COLOR_03"
    Green = "COLOR_04"
    Blue = "COLOR_05"
    Purple = "COLOR_06"
    Pink = "COLOR_07"
    Brown = "COLOR_08"

    WorkSpaceCollectionColor = "COLOR_01"
    DrawIBCollectionColor = "COLOR_07"
    ComponentCollectionColor = "COLOR_05"

    GroupCollection = "NONE"
    ToggleCollection = "COLOR_03" 
    SwitchCollection = "COLOR_04"
    

class CollectionUtils:
    @classmethod
    def get_collection_by_name(cls,collection_name:str):
        """
        根据集合名称获取集合对象。

        :param collection_name: 要获取的集合名称
        :return: 返回找到的集合对象，如果未找到则返回 None
        """
        if collection_name in bpy.data.collections:
            return bpy.data.collections[collection_name]
        else:
            print(f"未找到名称为 '{collection_name}' 的集合")
            return None
    
    @classmethod
    def select_collection_objects(cls,collection):
        def recurse_collection(col):
            for obj in col.objects:
                obj.select_set(True)
            for subcol in col.children_recursive:
                recurse_collection(subcol)

        recurse_collection(collection)

    @classmethod
    def create_new_collection(cls,collection_name:str,color_tag:CollectionColor=CollectionColor.White,link_to_parent_collection_name:str = ""):
        '''
        创建一个新的集合，并且可以选择是否链接到父集合
        :param collection_name: 集合名称
        :param color_tag: 集合颜色标签  不填则默认为白色
        :param link_to_parent_collection_name: 如果不为空，则将新创建的集合链接到指定的父集合
        '''
        new_collection = bpy.data.collections.new(collection_name)
        new_collection.color_tag = color_tag
        
        if link_to_parent_collection_name:
            parent_collection = CollectionUtils.get_collection_by_name(link_to_parent_collection_name)
            if parent_collection:
                parent_collection.children.link(new_collection)
        
        return new_collection
    
