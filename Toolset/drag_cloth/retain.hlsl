#include "constants.hlsli"
#include "common.hlsli"
RWBuffer<float4> State : register(u1);
[numthreads(1,1,1)]void main()
{
 if(State[9].y<.5 && IniParams[127].y<.5)return;
 uint slot=(uint)clamp(round(IniParams[118].w),1,2);
 if(State[23].w>.5 && State[3][slot-1]>.5)
 {float4 value=State[ClothSleepRow()];value.x=1;State[ClothSleepRow()]=value;}
}
