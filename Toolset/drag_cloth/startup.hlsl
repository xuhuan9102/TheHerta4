#define main OriginalStartupSource
#define STATIC_PHASE 0
#include "cloth_nodes.hlsl"
#undef main
[numthreads(1,1,1)]void main()
{
 if(State[23].w<.5)return;
 if(State[9].x>.5 && GarmentAmount(State[0].xy)<1e-5)
 {
  uint slot=(uint)clamp(round(IniParams[118].w),1,2);
  State[16+slot]=float4(0,State[8].z,1,State[2].x);
 }
 if(IniParams[126].w>.5)Control();
}
