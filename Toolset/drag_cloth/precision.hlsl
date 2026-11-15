#include "constants.hlsli"
#include "common.hlsli"
RWBuffer<float4> State : register(u1);
groupshared float Stretch[64];
groupshared uint Skip,Start,Count,Slot;
[numthreads(64,1,1)]void main(uint lane:SV_GroupIndex)
{
 if(lane==0)
 {
  Slot=(uint)clamp(round(IniParams[118].w),1,2);
  Count=(uint)Config[0].z;Start=220+(Slot-1)*(PARTICLE_COUNT+Count)+PARTICLE_COUNT;
  Skip=!AdaptiveCloth() || State[23].w<.5 || State[ClothWorkRow()].z<.5 || State[ClothSleepRow()].x>.5?1:0;
  if(State[9].y<.5 && IniParams[127].y<.5)Skip=1;
 }
 GroupMemoryBarrierWithGroupSync();if(Skip!=0)return;
 float maximum=0;
 [loop]for(uint i=lane;i<Count;i+=64)
 {float4 value=State[Start+i];maximum=max(maximum,value.w<.5 || !isfinite(value.y)?1e6:value.y);}
 Stretch[lane]=maximum;GroupMemoryBarrierWithGroupSync();
 [unroll]for(uint width=32;width>0;width>>=1)
 {if(lane<width)Stretch[lane]=max(Stretch[lane],Stretch[lane+width]);GroupMemoryBarrierWithGroupSync();}
 if(lane==0)
 {
  float4 metric=State[15];if(Slot==1)metric.x=Stretch[0];else metric.y=Stretch[0];State[15]=metric;
  if(Stretch[0]>2.4)State[ClothWorkRow()]=float4(max(Config[3].y,1),8,0,0);
 }
}
