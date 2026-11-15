#include "constants.hlsli"
#include "common.hlsli"
RWBuffer<float4> State : register(u1);
[numthreads(1,1,1)]void main()
{
 if(State[9].y<.5 && IniParams[127].y<.5)return;
 uint slot=(uint)clamp(round(IniParams[118].w),1,2);
 if(State[23].w<.5 || GarmentAmount(State[0].xy)<1e-5 || State[18+slot].x<=.0002)return;
 float4 sleep=State[ClothSleepRow()];sleep.x=0;State[ClothSleepRow()]=sleep;
 float4 safe=State[3];if(slot==1)safe.x=0;else safe.y=0;State[3]=safe;
}
