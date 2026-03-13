import os 
import json

from typing import List, Dict, Union
from dataclasses import dataclass, field, asdict

from ..utils.format_utils import Fatal
from enum import Enum

    
@dataclass
class ExtractedObjectBufferSemantic:
    name: str
    index: int
    format: str
    stride: int = 0

    def __post_init__(self):
        if self.stride == 0:
            self.stride = self.format.byte_width

@dataclass
class ExtractedObjectBuffer:
    semantics: List[ExtractedObjectBufferSemantic]


    
@dataclass
class ExtractedObjectComponent:
    vertex_offset: int
    vertex_count: int
    index_offset: int
    index_count: int
    vg_offset: int
    vg_count: int
    vg_map: Dict[int, int]


@dataclass
class ExtractedObjectShapeKeys:
    offsets_hash: str = ''
    scale_hash: str = ''
    vertex_count: int = 0
    dispatch_y: int = 0
    checksum: int = 0


@dataclass
class ExtractedObject:
    vb0_hash: str
    cb4_hash: str
    vertex_count: int
    index_count: int
    components: List[ExtractedObjectComponent]
    shapekeys: ExtractedObjectShapeKeys
    export_format: Dict[str, ExtractedObjectBuffer]

    def __post_init__(self):
        if isinstance(self.shapekeys, dict):
            self.components = [ExtractedObjectComponent(**component) for component in self.components]
            self.shapekeys = ExtractedObjectShapeKeys(**self.shapekeys)

    def as_json(self):
        return json.dumps(asdict(self), indent=4)


class ExtractedObjectHelper:
    '''
    不用类包起来难受，还是做成工具类好一点。。
    '''
    @classmethod
    def read_metadata(cls,metadata_path: str) -> ExtractedObject:
        if not os.path.exists(metadata_path):
            raise Fatal("无法找到Metadata.json文件，请确认是否存在该文件。")
        
        with open(metadata_path) as f:
            return ExtractedObject(**json.load(f))
        