import bpy
import numpy
import os

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
			if category_buffer.Type == "Normal":
				category_elements, category_vb_data, category_vertex_count = SSMTImportHelper.parse_normal_category_buffer(category_buffer)
			else:
				category_elements, category_vb_data, category_vertex_count = SSMTImportHelper.parse_special_category_buffer(category_buffer)

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
	def parse_special_category_buffer(category_buffer:SubmeshCategoryBuffer):
		print("预留特殊 Buffer 解析路线, 当前 Type: " + category_buffer.Type + ", FileName: " + category_buffer.FileName)
		return [], {}, 0

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
