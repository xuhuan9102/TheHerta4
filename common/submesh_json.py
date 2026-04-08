from dataclasses import dataclass, field
import os

from ..utils.json_utils import JsonUtils
from .d3d11_element import D3D11Element


@dataclass
class SubmeshIndexBuffer:
	DXGI_FORMAT:str
	FileName:str
	FilePath:str = field(init=False)

	def bind_dir_path(self, dir_path:str):
		self.FilePath = os.path.join(dir_path, self.FileName)


@dataclass
class SubmeshCategoryBuffer:
	FileName:str
	Type:str
	D3D11ElementList:list[D3D11Element] = field(default_factory=list)
	FilePath:str = field(init=False)
	Stride:int = field(init=False, default=0)

	def bind_dir_path(self, dir_path:str):
		self.FilePath = os.path.join(dir_path, self.FileName)

	def calc_stride(self):
		self.Stride = sum(d3d11_element.ByteWidth for d3d11_element in self.D3D11ElementList)


@dataclass
class SubmeshJson:
	JsonFilePath:str

	FileName:str = field(init=False)
	DirPath:str = field(init=False)
	JsonDict:dict = field(init=False, repr=False)

	GamePreset:str = field(init=False, default="")
	VertexLimitVB:str = field(init=False, default="")
	CategoryHash:dict = field(init=False, default_factory=dict)
	CategoryDrawCategoryMap:dict = field(init=False, default_factory=dict)
	WorkGameType:str = field(init=False, default="")
	GPU_PreSkinning:bool = field(init=False, default=False)
	IndexBufferList:list[SubmeshIndexBuffer] = field(init=False, default_factory=list)
	CategoryBufferList:list[SubmeshCategoryBuffer] = field(init=False, default_factory=list)
	TextureMarkUpInfoList:list = field(init=False, default_factory=list)

	def __post_init__(self):
		self.FileName = os.path.basename(self.JsonFilePath)
		self.DirPath = os.path.dirname(self.JsonFilePath)
		self.JsonDict = JsonUtils.LoadFromFile(self.JsonFilePath)
		self.parse_json_dict()

	def parse_json_dict(self):
		self.GamePreset = self.JsonDict.get("GamePreset", "")
		self.VertexLimitVB = self.JsonDict.get("VertexLimitVB", "")
		self.CategoryHash = self.JsonDict.get("CategoryHash", {})
		self.CategoryDrawCategoryMap = self.JsonDict.get("CategoryDrawCategoryMap", {})
		self.WorkGameType = self.JsonDict.get("WorkGameType", "")
		self.GPU_PreSkinning = self.JsonDict.get("GPU-PreSkinning", False)
		self.TextureMarkUpInfoList = list(self.JsonDict.get("TextureMarkUpInfoList", []))

		self.IndexBufferList = []
		for index_buffer_json in self.JsonDict.get("IndexBufferList", []):
			index_buffer = SubmeshIndexBuffer(
				DXGI_FORMAT=index_buffer_json.get("DXGI_FORMAT", ""),
				FileName=index_buffer_json.get("FileName", "")
			)
			index_buffer.bind_dir_path(self.DirPath)
			self.IndexBufferList.append(index_buffer)

		self.CategoryBufferList = []
		for category_buffer_json in self.JsonDict.get("CategoryBufferList", []):
			aligned_byte_offset = 0
			d3d11_element_list = []
			for d3d11_element_json in category_buffer_json.get("D3D11ElementList", []):
				d3d11_element = D3D11Element(
					SemanticName=d3d11_element_json.get("SemanticName", ""),
					SemanticIndex=int(d3d11_element_json.get("SemanticIndex", 0)),
					Format=d3d11_element_json.get("Format", ""),
					ByteWidth=int(d3d11_element_json.get("ByteWidth", 0)),
					ExtractSlot=d3d11_element_json.get("ExtractSlot", ""),
					ExtractTechnique=d3d11_element_json.get("ExtractTechnique", ""),
					Category=d3d11_element_json.get("Category", ""),
					AlignedByteOffset=aligned_byte_offset,
				)
				aligned_byte_offset += d3d11_element.ByteWidth
				d3d11_element_list.append(d3d11_element)

			category_buffer = SubmeshCategoryBuffer(
				FileName=category_buffer_json.get("FileName", ""),
				Type=category_buffer_json.get("Type", ""),
				D3D11ElementList=d3d11_element_list,
			)
			category_buffer.bind_dir_path(self.DirPath)
			category_buffer.calc_stride()
			self.CategoryBufferList.append(category_buffer)

	def get_d3d11_element_json_list(self) -> list[dict]:
		d3d11_element_json_list = []
		for category_buffer_json in self.JsonDict.get("CategoryBufferList", []):
			for d3d11_element_json in category_buffer_json.get("D3D11ElementList", []):
				d3d11_element_json_list.append(dict(d3d11_element_json))
		return d3d11_element_json_list
