from .m_key import M_Key

from typing import Optional


class M_Condition:
	'''
	因为M_Condition只是DrawCallModel的一个属性
	所以在这里和DrawCallModel放在一起定义了
	'''
	def __init__(self, work_key_list: Optional[list[M_Key]] = None):
		self.work_key_list: list[M_Key] = work_key_list or []

		condition_str: str = ""
		if len(self.work_key_list) != 0:
			for work_key in self.work_key_list:
				single_condition: str = work_key.key_name + " == " + str(work_key.tmp_value)
				condition_str = condition_str + single_condition + " && "
			condition_str = condition_str[:-4]

		self.condition_str = condition_str
