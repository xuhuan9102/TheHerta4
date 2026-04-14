import bpy
import numpy
import os

from .d3d11_element import D3D11Element
from .mesh_create_helper import MeshCreateHelper
from .submesh_json import SubmeshJson, SubmeshCategoryBuffer
from ..utils.format_utils import Fatal, FormatUtils


class SSMTImportHelper:
	@staticmethod
	def create_mesh_from_json(json_file_path:str, import_collection:bpy.types.Collection | None = None):
		submesh_json = SubmeshJson(json_file_path)

		elements, vb_data, vb_vertex_count = SSMTImportHelper.parse_category_buffers(submesh_json)
		ib_data, ib_count, ib_polygon_count = SSMTImportHelper.parse_index_buffer(submesh_json)

		mesh_name = os.path.splitext(submesh_json.FileName)[0]
		logic_name = submesh_json.GamePreset
		gametypename = submesh_json.WorkGameType

		return MeshCreateHelper.create_mesh_object(
			mesh_name=mesh_name,
			source_path=submesh_json.JsonFilePath,
			logic_name=logic_name,
			gametypename=gametypename,
			elements=elements,
			vb_data=vb_data,
			ib_data=ib_data,
			vb_vertex_count=vb_vertex_count,
			ib_count=ib_count,
			ib_polygon_count=ib_polygon_count,
			local_bounding_box_min=submesh_json.LocalBoundingBoxMin,
			local_bounding_box_max=submesh_json.LocalBoundingBoxMax,
			vertex_compression_params=submesh_json.VertexCompressionParams,
			import_collection=import_collection,
		)

	@staticmethod
	def parse_index_buffer(submesh_json:SubmeshJson):
		if len(submesh_json.IndexBufferList) == 0:
			raise Fatal("SubmeshJson missing IndexBufferList.")

		index_buffer = submesh_json.IndexBufferList[0]
		if not os.path.exists(index_buffer.FilePath):
			raise Fatal("Unable to find matching .ib file for: " + index_buffer.FileName)

		ib_file_size = os.path.getsize(index_buffer.FilePath)
		if ib_file_size == 0:
			raise Fatal("Current Import " + index_buffer.FileName + " file is empty, skip import.")

		index_np_type = FormatUtils.get_nptype_from_format(index_buffer.DXGI_FORMAT)
		index_stride = numpy.dtype(index_np_type).itemsize
		if ib_file_size % index_stride != 0:
			raise Fatal("Index buffer file size is not aligned with DXGI format stride: " + index_buffer.FileName)

		ib_count = int(ib_file_size / index_stride)
		ib_polygon_count = int(ib_count / 3)
		ib_data = numpy.fromfile(index_buffer.FilePath, dtype=index_np_type, count=ib_count)
		return ib_data, ib_count, ib_polygon_count

	@staticmethod
	def parse_category_buffers(submesh_json:SubmeshJson):
		elements = []
		vb_data = {}
		vb_vertex_count = 0

		for category_buffer in submesh_json.CategoryBufferList:
			if category_buffer.Type != "Normal":
				continue

			category_elements, category_vb_data, category_vertex_count = SSMTImportHelper.parse_normal_category_buffer(category_buffer)

			if category_vertex_count > 0:
				if vb_vertex_count == 0:
					vb_vertex_count = category_vertex_count
				elif vb_vertex_count != category_vertex_count:
					raise Fatal(
						"Vertex count mismatch between category buffers: "
						+ category_buffer.FileName
						+ " expected " + str(vb_vertex_count)
						+ " actual " + str(category_vertex_count)
					)

			elements.extend(category_elements)
			vb_data.update(category_vb_data)

		for category_buffer in submesh_json.CategoryBufferList:
			if category_buffer.Type == "Normal":
				continue

			category_elements, category_vb_data, category_vertex_count = SSMTImportHelper.parse_special_category_buffer(
				category_buffer=category_buffer,
				vb_vertex_count=vb_vertex_count,
			)

			if category_vertex_count > 0 and category_vertex_count != vb_vertex_count:
				raise Fatal(
					"Vertex count mismatch between category buffers: "
					+ category_buffer.FileName
					+ " expected " + str(vb_vertex_count)
					+ " actual " + str(category_vertex_count)
				)

			elements.extend(category_elements)
			vb_data.update(category_vb_data)

		if vb_vertex_count == 0:
			raise Fatal("No valid normal category buffer was parsed from SubmeshJson.")

		return elements, vb_data, vb_vertex_count

	@staticmethod
	def parse_normal_category_buffer(category_buffer:SubmeshCategoryBuffer):
		if not os.path.exists(category_buffer.FilePath):
			raise Fatal("Unable to find matching .buf file for: " + category_buffer.FileName)

		if category_buffer.Stride <= 0:
			if len(category_buffer.D3D11ElementList) == 0:
				return [], {}, 0
			raise Fatal("Category buffer stride is zero: " + category_buffer.FileName)

		file_size = os.path.getsize(category_buffer.FilePath)
		if file_size == 0:
			raise Fatal("Current Import " + category_buffer.FileName + " file is empty, skip import.")
		if file_size % category_buffer.Stride != 0:
			raise Fatal("Category buffer file size is not aligned with stride: " + category_buffer.FileName)

		vertex_count = int(file_size / category_buffer.Stride)
		category_dtype = SSMTImportHelper.create_dtype_from_elements(category_buffer.D3D11ElementList)
		category_buffer_data = numpy.fromfile(category_buffer.FilePath, dtype=category_dtype, count=vertex_count)

		category_vb_data = {}
		for d3d11_element in category_buffer.D3D11ElementList:
			category_vb_data[d3d11_element.ElementName] = category_buffer_data[d3d11_element.ElementName]

		return category_buffer.D3D11ElementList, category_vb_data, vertex_count

	@staticmethod
	def parse_special_category_buffer(category_buffer:SubmeshCategoryBuffer, vb_vertex_count:int):
		if category_buffer.Type == "DynamicBlend":
			return SSMTImportHelper.parse_dynamic_blend_category_buffer(
				category_buffer=category_buffer,
				vb_vertex_count=vb_vertex_count,
			)

		print("预留特殊 Buffer 解析路线, 当前 Type: " + category_buffer.Type + ", FileName: " + category_buffer.FileName)
		return [], {}, 0

	@staticmethod
	def parse_dynamic_blend_category_buffer(category_buffer:SubmeshCategoryBuffer, vb_vertex_count:int):
		if vb_vertex_count <= 0:
			raise Fatal("DynamicBlend parsing requires a valid vb_vertex_count.")

		if not os.path.exists(category_buffer.FilePath):
			raise Fatal("Unable to find matching .buf file for: " + category_buffer.FileName)

		file_size = os.path.getsize(category_buffer.FilePath)
		if file_size == 0:
			raise Fatal("Current Import " + category_buffer.FileName + " file is empty, skip import.")
		if file_size % 4 != 0:
			raise Fatal("DynamicBlend buffer size must be aligned to uint32: " + category_buffer.FileName)

		raw_u32 = numpy.fromfile(category_buffer.FilePath, dtype=numpy.uint32)
		offset_count = vb_vertex_count + 1
		if len(raw_u32) <= offset_count:
			raise Fatal("DynamicBlend buffer is too short to contain offset table and packed entries: " + category_buffer.FileName)

		offsets = raw_u32[:offset_count].astype(numpy.uint64)
		packed_start_index = offset_count
		packed_end_index = len(raw_u32)

		if numpy.any(offsets < packed_start_index):
			raise Fatal("DynamicBlend offset table points before packed entry stream: " + category_buffer.FileName)
		if numpy.any(offsets > packed_end_index):
			raise Fatal("DynamicBlend offset table points past buffer end: " + category_buffer.FileName)
		if numpy.any(offsets[1:] < offsets[:-1]):
			raise Fatal("DynamicBlend offset table is not monotonically increasing: " + category_buffer.FileName)

		max_influence_count = int(numpy.max(offsets[1:] - offsets[:-1])) if vb_vertex_count > 0 else 0
		semantic_group_count = max(1, (max_influence_count + 3) // 4)

		blend_indices_dict = {}
		blend_weights_dict = {}
		for semantic_index in range(semantic_group_count):
			blend_indices_dict[semantic_index] = numpy.zeros((vb_vertex_count, 4), dtype=numpy.uint32)
			blend_weights_dict[semantic_index] = numpy.zeros((vb_vertex_count, 4), dtype=numpy.float32)

		for vertex_index in range(vb_vertex_count):
			start = int(offsets[vertex_index])
			end = int(offsets[vertex_index + 1])
			if end < start:
				raise Fatal("DynamicBlend offset table contains inverted range at vertex: " + str(vertex_index))

			packed_values = raw_u32[start:end]
			for influence_index, packed_value in enumerate(packed_values):
				semantic_index = influence_index // 4
				channel_index = influence_index % 4
				blend_indices_dict[semantic_index][vertex_index, channel_index] = packed_value & 0xFFFF
				blend_weights_dict[semantic_index][vertex_index, channel_index] = ((packed_value >> 16) & 0xFFFF) / 65535.0

		category_elements = []
		category_vb_data = {}
		aligned_byte_offset = 0
		for semantic_index in range(semantic_group_count):
			blendindices_element = D3D11Element(
				SemanticName="BLENDINDICES",
				SemanticIndex=semantic_index,
				Format="R32G32B32A32_UINT",
				ByteWidth=16,
				ExtractSlot="cs-t1",
				ExtractTechnique="compute",
				Category="Blend",
				AlignedByteOffset=aligned_byte_offset,
			)
			aligned_byte_offset += blendindices_element.ByteWidth

			blendweight_element = D3D11Element(
				SemanticName="BLENDWEIGHT",
				SemanticIndex=semantic_index,
				Format="R32G32B32A32_FLOAT",
				ByteWidth=16,
				ExtractSlot="cs-t1",
				ExtractTechnique="compute",
				Category="Blend",
				AlignedByteOffset=aligned_byte_offset,
			)
			aligned_byte_offset += blendweight_element.ByteWidth

			category_elements.append(blendindices_element)
			category_elements.append(blendweight_element)
			category_vb_data[blendindices_element.ElementName] = blend_indices_dict[semantic_index]
			category_vb_data[blendweight_element.ElementName] = blend_weights_dict[semantic_index]

		return category_elements, category_vb_data, vb_vertex_count

	@staticmethod
	def create_dtype_from_elements(d3d11_element_list:list):
		fields = []
		for d3d11_element in d3d11_element_list:
			numpy_type = FormatUtils.get_nptype_from_format(d3d11_element.Format)
			size = int(d3d11_element.ByteWidth / numpy.dtype(numpy_type).itemsize)
			if size == 1:
				fields.append((d3d11_element.ElementName, numpy_type))
			else:
				fields.append((d3d11_element.ElementName, numpy_type, size))
		return numpy.dtype(fields)
