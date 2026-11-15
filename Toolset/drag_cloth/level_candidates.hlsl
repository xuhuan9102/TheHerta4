#include "constants.hlsli"
#include "common.hlsli"
Buffer<float4> FrameState : register(t77);
RWBuffer<uint> Candidates : register(u2);
[numthreads(64,1,1)]void main(uint3 id:SV_DispatchThreadID)
{
 if(FrameState[9].y<.5 && IniParams[127].y<.5)return;
 if(IniParams[126].x<.5 || FrameState[23].w<.5 || FrameState[ClothSleepRow()].x>.5 || GarmentAmount(FrameState[0].xy)<1e-5)return;
 uint count;BodyOrder.GetDimensions(count);uint bands=(uint)LevelConfig[0].w,words=(count+31)/32;
 if(bands<2)return;
 uint i=id.x;
 if(IniParams[126].y<.5){if(i<bands*words)Candidates[i]=0;return;}
 if(i>=count)return;
 uint tri=BodyOrder[i];uint3 ids=uint3(BodyVertices[tri*3],BodyVertices[tri*3+1],BodyVertices[tri*3+2]);
 float3 h=float3(dot(BindVertices[ids.x].position-Config[8].xyz,Config[6].xyz),dot(BindVertices[ids.y].position-Config[8].xyz,Config[6].xyz),dot(BindVertices[ids.z].position-Config[8].xyz,Config[6].xyz))/max(Config[6].w,1e-6);
 if(!all(isfinite(h)))return;
 float lower=min(h.x,min(h.y,h.z)),upper=max(h.x,max(h.y,h.z)),pad=max(1e-5,max(abs(lower),abs(upper))*1e-6);
 uint first=(uint)clamp(floor((lower-pad-LevelConfig[0].x)*LevelConfig[0].z),0,bands-1);
 uint last=(uint)clamp(floor((upper+pad-LevelConfig[0].x)*LevelConfig[0].z),0,bands-1);
 [loop]for(uint band=first;band<=last;++band)InterlockedOr(Candidates[band*words+i/32],1u<<(i&31));
}
