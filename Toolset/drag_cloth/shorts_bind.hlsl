#define main OriginalShortsSource
#define STATIC_PHASE 6
RWBuffer<float4> ShortsBinding : register(u2);
Buffer<float> SkinMix : register(t84);
#include "cloth_nodes.hlsl"
#undef main
void InvalidateBinding(uint at)
{
 ShortsBinding[at+3]=float4(0,0,0,-1);
 ShortsBinding[at+10]=float4(0,0,0,-1);
}
Blend LegMix(Blend left,Blend right,float amount)
{
 uint ids[8];float weights[8];
 [unroll]for(uint k=0;k<4;++k){ids[k]=left.indices[k];weights[k]=left.weights[k]*amount;ids[k+4]=right.indices[k];weights[k+4]=right.weights[k]*(1-amount);}
 [loop]for(uint i=0;i<8;++i)[loop]for(uint j=0;j<i;++j)if(ids[i]==ids[j]){weights[j]+=weights[i];weights[i]=0;break;}
 Blend result;result.weights=0;result.indices=0;
 [unroll]for(uint rank=0;rank<4;++rank)
 {uint best=0;[loop]for(uint n=1;n<8;++n)if(weights[n]>weights[best])best=n;result.weights[rank]=max(weights[best],0);result.indices[rank]=ids[best];weights[best]=-1;}
 result.weights/=max(dot(result.weights,1.0),1e-8);return result;
}
[numthreads(64,1,1)]void main(uint3 id:SV_DispatchThreadID)
{
 uint i=id.x;if(i>=PARTICLE_COUNT || State[23].w<.5)return;
 uint capacity;ShortsBinding.GetDimensions(capacity);if(capacity!=PARTICLE_COUNT*17)return;
 if(State[9].y<.5 && IniParams[127].y<.5)return;
 uint at=i*17;bool active=GarmentAmount(State[0].xy)>1e-5;
 if(active)
 {
  InvalidateBinding(at);
  // Bind the final simulated point, not its ideal material-height target.
  // Each query stays inside its own leg even when the legs overlap in pose.
  float3 p=Particles[i].current.xyz;uint seed=asuint(Contacts[i*3+2].x);
  Blend left,right;float3 q,b,n;uint tri;float sd;
  ClosestBodyGroup(p,seed,1,q,b,n,tri,sd);
  if(!Allowed(tri,1) || !isfinite(sd) || abs(sd)>1e10)return;
  left=Transfer(tri,b);ShortsBinding[at]=float4(asfloat(tri),b);
  ClosestBodyGroup(p,seed,2,q,b,n,tri,sd);
  if(!Allowed(tri,2) || !isfinite(sd) || abs(sd)>1e10)return;
  right=Transfer(tri,b);ShortsBinding[at+1]=float4(asfloat(tri),b);
  float mix=saturate(SkinMix[i]);Weights[i]=LegMix(left,right,mix);
  ShortsBinding[at+3]=float4(mix,1-mix,0,State[2].x);
  ShortsBinding[at+5]=left.weights;ShortsBinding[at+6]=asfloat(left.indices);
  ShortsBinding[at+7]=right.weights;ShortsBinding[at+8]=asfloat(right.indices);
 }
 else
 {
  InvalidateBinding(at);
  Blend original;original.weights=OriginalBlend[Representatives[i]*2];original.indices=asuint(OriginalBlend[Representatives[i]*2+1]);Weights[i]=original;
 }
 float4 a,b,c;Rows(Weights[i],a,b,c);PreviousRows[i*3]=a;PreviousRows[i*3+1]=b;PreviousRows[i*3+2]=c;
}
