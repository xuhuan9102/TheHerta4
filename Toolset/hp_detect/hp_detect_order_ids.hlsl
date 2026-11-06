// 血量检测：角色 id 采集 / 按顶点 X 插入排序 / 打包。
//
// 基于 ZZMI 的 HP bar\OrderIds.CS.hlsl。
// 每帧最多收 3 个 UI 图标 draw（counter >= 3 直接返回），按首顶点 X 排好序后
// 每收到一个图标就打包当前结果，前台识别不等待后台图标补齐。
// 三个角色 id 压进 u0[1].acc.summ（8bit × 3）；pos 使用 float，供 ini 回读 X。
struct accumulator
{
	uint counter;
	float summ;
};
struct stored
{
	uint3 value;
	float3 pos;
	accumulator acc;
};
RWStructuredBuffer<stored> u0 : register(u0);
Buffer<float> vb0 : register(t0);

#define cmp -
Texture1D<float4> IniParams : register(t120);

#define first_vertex	IniParams[1].x
#define value_raw		IniParams[1].z
#define stride			IniParams[1].w

[numthreads(1, 1, 1)]
void main()
{
	stored data = u0[1];

	uint counter = data.acc.counter;
	if (counter >= 3)
		return;

	uint index = first_vertex * stride / 4;
	float positionX = vb0[index];
	uint value = (uint)value_raw;

	uint3 id = data.value;
	float3 pos = data.pos;

	if (counter == 0)
	{
		id.x = value;
		pos.x = positionX;
	}
	else if (counter == 1)
	{
		id.y = value;
		pos.y = positionX;

		if (positionX < pos.x)
		{
			uint tid = id.x;
			id.x = id.y;
			id.y = tid;

			float tpos = pos.x;
			pos.x = pos.y;
			pos.y = tpos;
		}
	}
	else
	{
		id.z = value;
		pos.z = positionX;

		if (positionX < pos.y)
		{
			uint tid = id.y;
			id.y = id.z;
			id.z = tid;

			float tpos = pos.y;
			pos.y = pos.z;
			pos.z = tpos;

			if (positionX < pos.x)
			{
				tid = id.x;
				id.x = id.y;
				id.y = tid;

				tpos = pos.x;
				pos.x = pos.y;
				pos.y = tpos;
			}
		}
	}

	data.value = id;
	data.pos = pos;

	counter++;
	data.acc.counter = counter;

	data.acc.summ = (id.x << 16) | (id.y << 8) | id.z;

	u0[1] = data;
}
