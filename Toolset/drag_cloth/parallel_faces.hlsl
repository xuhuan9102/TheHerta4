#define main OriginalColoredMain
RWBuffer<float4> FaceImpulses : register(u2);
Buffer<uint> FaceAdjacency : register(t80);
#define STATIC_PHASE 5
#include "cloth_nodes.hlsl"
#undef main
[numthreads(64,1,1)]void main(uint3 id:SV_DispatchThreadID)
{
 uint i=id.x,phase=(uint)IniParams[116].x,faces=(uint)Config[0].z;
 if(State[9].y<.5 && IniParams[127].y<.5)return;
 if(State[23].w<.5 || GarmentAmount(State[0].xy)<1e-5 || State[ClothSleepRow()].x>.5)return;
 if(IniParams[119].w>.5 && State[3][(uint)clamp(round(IniParams[118].w),1,2)-1]>.5)return;
 uint count;FaceImpulses.GetDimensions(count);if(count!=faces*3)return;
 if(phase==14)
 {
  if(i>=faces)return;
  FaceImpulses[i*3]=0;FaceImpulses[i*3+1]=0;FaceImpulses[i*3+2]=0;
  FaceScoped(i);return;
 }
 if(i>=PARTICLE_COUNT || ContactMass(i)<.5)return;
 float3 delta=0;float countActive=0;
 uint begin=FaceAdjacency[i],end=FaceAdjacency[i+1];
 [loop]for(uint k=begin;k<end;++k)
 {
  uint face=FaceAdjacency[PARTICLE_COUNT+1+k];
  [unroll]for(uint corner=0;corner<3;++corner)if(Faces[face*3+corner]==i)
  {float4 value=FaceImpulses[face*3+corner];delta+=value.xyz;countActive+=value.w;}
 }
 if(countActive<.5)return;
 Particle value=Particles[i];float3 next=KeepOutside(value.current.xyz,value.current.xyz+delta/countActive,ClothGap()*State[8].z);
 if(!all(isfinite(next)))return;value.previous.xyz+=next-value.current.xyz;value.current.xyz=next;Particles[i]=value;
}
