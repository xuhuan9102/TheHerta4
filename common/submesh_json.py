from dataclasses import dataclass, field
import os

from ..utils.json_utils import JsonUtils
from .d3d11_element import D3D11Element


def _strict_uint32(value) -> int:
	"""解析骨骼元数据整数；拒绝 bool、非整数浮点和越界值。"""
	if isinstance(value, bool):
		raise ValueError("bool 不是骨骼元数据整数")
	if isinstance(value, float) and not value.is_integer():
		raise ValueError("非整数浮点骨骼元数据")
	parsed = int(value)
	if parsed < 0 or parsed > 0xFFFFFFFF:
		raise ValueError("骨骼元数据超出 uint32 范围")
	return parsed


def _merged_skeleton_metadata_valid(payload: dict) -> bool:
	"""在导入/导出共同入口拒绝被截断的 VGMap/骨架元数据。

	缓存生成器已经有严格门控；这里额外检查原始 JSON，避免 ``int(1.5)``
	在 SubmeshJson 解析阶段被静默截断后继续生成错误的运行时文件。
	"""
	if not isinstance(payload, dict):
		return False
	fields = (
		"VGOffset", "VGCount", "VGMap", "SkeletonGroup",
		"DeformDrawIndex", "OriginalVertexCount",
	)
	if not any(field in payload for field in fields):
		return True
	try:
		vg_count = _strict_uint32(payload.get("VGCount", 0) or 0)
		_strict_uint32(payload.get("VGOffset", 0) or 0)
		for field in ("SkeletonGroup", "DeformDrawIndex", "OriginalVertexCount"):
			if field in payload:
				_strict_uint32(payload[field])
		if "VGMap" not in payload:
			return vg_count == 0
		vg_map = payload["VGMap"]
		if not isinstance(vg_map, dict):
			return False
		normalized = {}
		for raw_key, raw_value in vg_map.items():
			key = _strict_uint32(raw_key)
			if key in normalized:
				return False
			normalized[key] = _strict_uint32(raw_value)
		if vg_count == 0:
			return not normalized
		return set(normalized) == set(range(vg_count))
	except (TypeError, ValueError, OverflowError):
		return False


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
		self.Stride = max(
			(
				d3d11_element.AlignedByteOffset + d3d11_element.ByteWidth
				for d3d11_element in self.D3D11ElementList
			),
			default=0,
		)


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
	LocalBoundingBoxMin:list = field(init=False, default_factory=list)
	LocalBoundingBoxMax:list = field(init=False, default_factory=list)
	VertexCompressionParams:list = field(init=False, default_factory=list)
	VertexOffset:int = field(init=False, default=0)
	VertexCount:int = field(init=False, default=-1)
	MatchCS:str = field(init=False, default="")
	MatchUAVBytes:int = field(init=False, default=0)
	CB4Hash:str = field(init=False, default="")
	BoneMatrixFileName:str = field(init=False, default="")
	VGOffset:int = field(init=False, default=0)
	VGCount:int = field(init=False, default=0)
	VGMap:dict = field(init=False, default_factory=dict)
	MergedSkeletonMetadataValid:bool = field(init=False, default=True)
	ShapeKeysInfo:dict = field(init=False, default_factory=dict)
	IndexBufferList:list[SubmeshIndexBuffer] = field(init=False, default_factory=list)
	CategoryBufferList:list[SubmeshCategoryBuffer] = field(init=False, default_factory=list)
	TextureMarkUpInfoList:list = field(init=False, default_factory=list)

	def __post_init__(self):
		self.FileName = os.path.basename(self.JsonFilePath)
		self.DirPath = os.path.dirname(self.JsonFilePath)
		self.JsonDict = JsonUtils.LoadFromFile(self.JsonFilePath)
		self.MergedSkeletonMetadataValid = _merged_skeleton_metadata_valid(self.JsonDict)
		self.parse_json_dict()

	def parse_json_dict(self):
		self.GamePreset = self.JsonDict.get("GamePreset", "")
		self.VertexLimitVB = self.JsonDict.get("VertexLimitVB", "")
		self.CategoryHash = self.JsonDict.get("CategoryHash", {})
		self.CategoryDrawCategoryMap = self.JsonDict.get("CategoryDrawCategoryMap", {})
		self.WorkGameType = self.JsonDict.get("WorkGameType", "")
		self.GPU_PreSkinning = self.JsonDict.get("GPU-PreSkinning", False)
		self.LocalBoundingBoxMin = list(self.JsonDict.get("LocalBoundingBoxMin", []))
		self.LocalBoundingBoxMax = list(self.JsonDict.get("LocalBoundingBoxMax", []))
		self.VertexCompressionParams = list(self.JsonDict.get("VertexCompressionParams", []))
		self.TextureMarkUpInfoList = list(self.JsonDict.get("TextureMarkUpInfoList", []))
		self.VertexOffset = int(self.JsonDict.get("VertexOffset", 0))
		self.VertexCount = int(self.JsonDict.get("VertexCount", -1))
		self.MatchCS = str(self.JsonDict.get("match_cs", "") or "").strip()
		self.MatchUAVBytes = int(self.JsonDict.get("match_uav_bytes", 0) or 0)
		self.CB4Hash = self.JsonDict.get("CB4Hash", "")
		self.BoneMatrixFileName = self.JsonDict.get("BoneMatrixFileName", "")
		self.VGOffset = int(self.JsonDict.get("VGOffset", 0))
		self.VGCount = int(self.JsonDict.get("VGCount", 0))
		self.VGMap = dict(self.JsonDict.get("VGMap", {}))
		self.ShapeKeysInfo = dict(self.JsonDict.get("ShapeKeysInfo", {}))

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
				raw_aligned_byte_offset = d3d11_element_json.get("AlignedByteOffset", "")
				if str(raw_aligned_byte_offset).strip():
					element_offset = int(raw_aligned_byte_offset)
				else:
					element_offset = aligned_byte_offset
				d3d11_element = D3D11Element(
					SemanticName=d3d11_element_json.get("SemanticName", ""),
					SemanticIndex=int(d3d11_element_json.get("SemanticIndex", 0)),
					Format=d3d11_element_json.get("Format", ""),
					ByteWidth=int(d3d11_element_json.get("ByteWidth", 0)),
					ExtractSlot=d3d11_element_json.get("ExtractSlot", ""),
					ExtractTechnique=d3d11_element_json.get("ExtractTechnique", ""),
					Category=d3d11_element_json.get("Category", ""),
					AlignedByteOffset=element_offset,
				)
				aligned_byte_offset = max(
					aligned_byte_offset,
					element_offset + d3d11_element.ByteWidth,
				)
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
