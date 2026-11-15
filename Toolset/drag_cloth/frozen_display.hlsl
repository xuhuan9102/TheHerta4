#define main OriginalFrozenDisplaySource
#define STATIC_PHASE 1
RWBuffer<float4> FrozenDisplay : register(u2);
RWBuffer<float> SolvedOutput : register(u3);
Buffer<uint> DisplayTriangles : register(t81);
Buffer<float4> DisplayWeights : register(t83);
#include "cloth_nodes.hlsl"
#undef main
float3 DetailLocal(float4 a,float4 b,float4 c,float3 v)
{
 float3 x=cross(b.xyz,c.xyz),y=cross(c.xyz,a.xyz),z=cross(a.xyz,b.xyz);float det=dot(a.xyz,x);
 float threshold=max(length(a.xyz)*length(b.xyz)*length(c.xyz)*1e-6,1e-30);
 return abs(det)>threshold?(x*v.x+y*v.y+z*v.z)/det:0;
}
[numthreads(256,1,1)]void main(uint3 id:SV_DispatchThreadID)
{
 uint row=id.x;if(row>=(uint)Config[0].y || State[23].w<.5 || PaletteDecision[0].w<.5 || GarmentAmount(State[0].xy)<1e-5)return;
 if(State[9].y<.5 && IniParams[127].y<.5)return;
 uint node=WearMap[row];if(node>=PARTICLE_COUNT)return;
 uint3 ids=uint3(DisplayTriangles[row*3],DisplayTriangles[row*3+1],DisplayTriangles[row*3+2]);if(any(ids>=PARTICLE_COUNT))return;
 float3 weights=DisplayWeights[row].xyz,p0=Particles[ids.x].current.xyz,p1=Particles[ids.y].current.xyz,p2=Particles[ids.z].current.xyz;
 uint at=row*VERTEX_FLOATS+POSITION_OFFSET;float3 solved=float3(SolvedOutput[at],SolvedOutput[at+1],SolvedOutput[at+2]);
 float3 residual=solved-(p0*weights.x+p1*weights.y+p2*weights.z),u=p1-p0,v=p2-p0;
 float aa=dot(u,u),ab=dot(u,v),bb=dot(v,v),den=aa*bb-ab*ab;
 if(den>1e-18)
 {
  float x=(bb*dot(residual,u)-ab*dot(residual,v))/den,y=(aa*dot(residual,v)-ab*dot(residual,u))/den;
  FrozenDisplay[row]=float4(x,y,dot(residual,SafeUnit(cross(u,v),Source[row].normal)),State[2].x);
 }
 else
 {
  float4 a,b,c;Rows(Weights[node],a,b,c);FrozenDisplay[row]=float4(DetailLocal(a,b,c,residual),-State[2].x);
 }
}
