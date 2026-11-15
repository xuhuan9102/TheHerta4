#include "constants.hlsli"
#include "common.hlsli"
RWBuffer<float4> State : register(u1);
[numthreads(1,1,1)]void main()
{
 if(State[9].y<.5 && IniParams[127].y<.5)return;
 if(State[23].w<.5 || State[ClothSleepRow()].x>.5)return;
 // The rendered mesh can retain skinning residuals beyond the proxy surface.
 // Every active frame keeps its original final contact sweep.
 {
  uint slot=(uint)clamp(round(IniParams[118].w),1,2);
  float4 value=State[3];if(slot==1)value.x=0;else value.y=0;State[3]=value;
 }
}
