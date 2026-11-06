// 血量检测：血量百分比采集 / 按顶点 X 插入排序 / 打包。
//
// 基于 ZZMI 的 HP bar\SummHp.CS.hlsl，仅采集前台血条。
// 血量来自游戏血条 PS 的常量缓冲 cb1[11].z + cb1[11].w（float 46/47，版本相关，
// 游戏更新后要在节点的 4 个哈希属性之外一并实测这里）。
// cb1[12].z：前台 0.6，后台 0.4。每次采到前台立即打包，不等待后台血条。
// acc.summ 的 bit 21 为有效位，前台血量位于 bit 14..20，单位 1%。
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
cbuffer cb1 : register(b1)
{
	float4 cb1[29];
}

#define cmp -
Texture1D<float4> IniParams : register(t120);


#define first_vertex	IniParams[1].x
#define stride			IniParams[1].z


[numthreads(1, 1, 1)]
void main()
{
	float z = cb1[12].z;
	if (z <= 0.59 || z >= 0.61)
		return;

	stored data = u0[0];

	uint counter = data.acc.counter;
	if (counter >= 3)
		return;

	uint index = first_vertex * stride / 4;
	float positionX = vb0[index];
	uint value = saturate(cb1[11].z + cb1[11].w) * 100.0 + 0.5;

	uint3 hp = data.value;
	float3 pos = data.pos;

	if (counter == 0)
	{
		hp.x = value;
		pos.x = positionX;
	}
	else if (counter == 1)
	{
		hp.y = value;
		pos.y = positionX;

		if (positionX < pos.x)
		{
			uint th = hp.x;
			hp.x = hp.y;
			hp.y = th;

			float tp = pos.x;
			pos.x = pos.y;
			pos.y = tp;
		}
	}
	else
	{
		hp.z = value;
		pos.z = positionX;

		if (positionX < pos.y)
		{
			uint th = hp.y;
			hp.y = hp.z;
			hp.z = th;

			float tp = pos.y;
			pos.y = pos.z;
			pos.z = tp;

			if (positionX < pos.x)
			{
				th = hp.x;
				hp.x = hp.y;
				hp.y = th;

				tp = pos.x;
				pos.x = pos.y;
				pos.y = tp;
			}
		}
	}

	data.value = hp;
	data.pos = pos;

	counter++;
	data.acc.counter = counter;

	data.acc.summ = (1u << 21) | (hp.x << 14);

	u0[0] = data;
}
