#define main OriginalFrozenSkinSource
#define STATIC_PHASE 1
RWBuffer<float4> FrozenMesh : register(u2);
RWBuffer<float4> LegFrames : register(u3);
#include "cloth_nodes.hlsl"
#undef main

bool SkinInverse(float4 a,float4 b,float4 c,float3 world,out float3 local)
{
 float3 x=cross(b.xyz,c.xyz),y=cross(c.xyz,a.xyz),z=cross(a.xyz,b.xyz);float det=dot(a.xyz,x);
 float threshold=max(length(a.xyz)*length(b.xyz)*length(c.xyz)*1e-6,1e-30);
 local=0;if(!isfinite(det) || abs(det)<=threshold)return false;
 float3 v=world-float3(a.w,b.w,c.w);local=(x*v.x+y*v.y+z*v.z)/det;return all(isfinite(local));
}
void FollowAxes()
{
 uint row=Representatives[(uint)HemParams[0].w];float4 weights=OriginalBlend[row*2];uint4 ids=asuint(OriginalBlend[row*2+1]);uint best=0;
 [unroll]for(uint k=1;k<4;++k)if(weights[k]>weights[best])best=k;
 Blend root;root.weights=float4(1,0,0,0);root.indices=uint4(ids[best],0,0,0);float4 a,b,c;Rows(root,a,b,c);
 float3 up=SafeUnit(Rotate(a,b,c,Config[6].xyz),State[5].xyz),side=Rotate(a,b,c,Config[7].xyz);side=SafeUnit(side-up*dot(side,up),State[6].xyz);
 State[5]=float4(up,0);State[6]=float4(side,0);State[7]=float4(SafeUnit(cross(up,side),State[7].xyz),0);
 float4 frame=State[8];frame.z=max((length(a.xyz)+length(b.xyz)+length(c.xyz))/3,.001);State[8]=frame;
}
bool FollowLegs(uint i,bool capture,bool initialize,uint mode,bool paused,inout Particle p)
{
 if(!Sliding() || GarmentAmount(State[0].xy)<1e-5)return false;
 uint capacity;LegFrames.GetDimensions(capacity);if(capacity!=PARTICLE_COUNT*17)return false;
 uint at=i*17;
 if(LegFrames[at+3].w!=State[2].x || dot(LegFrames[at+5],1.0)<.5 || dot(LegFrames[at+7],1.0)<.5)return false;
 Blend left,right;left.weights=LegFrames[at+5];left.indices=asuint(LegFrames[at+6]);right.weights=LegFrames[at+7];right.indices=asuint(LegFrames[at+8]);
 float4 la,lb,lc,ra,rb,rc;Rows(left,la,lb,lc);Rows(right,ra,rb,rc);
 if(capture)
 {
  float3 l,r;
  if(!SkinInverse(la,lb,lc,p.current.xyz,l) || !SkinInverse(ra,rb,rc,p.current.xyz,r)){float4 invalid=LegFrames[at+10];invalid.w=0;LegFrames[at+10]=invalid;return false;}
  LegFrames[at+9]=float4(l,LegFrames[at+3].x);LegFrames[at+10]=float4(r,State[2].x);
 }
 else
 {
  if(initialize || LegFrames[at+10].w!=State[2].x)return false;
  float4 pa=LegFrames[at+11],pb=LegFrames[at+12],pc=LegFrames[at+13],qa=LegFrames[at+14],qb=LegFrames[at+15],qc=LegFrames[at+16];
  if(any(la!=pa) || any(lb!=pb) || any(lc!=pc) || any(ra!=qa) || any(rb!=qb) || any(rc!=qc))
  {
   float mix=LegFrames[at+9].w;float3 l=LegFrames[at+9].xyz,r=LegFrames[at+10].xyz;
   if(IniParams[127].y>.5){SkinInverse(pa,pb,pc,p.current.xyz,l);SkinInverse(qa,qb,qc,p.current.xyz,r);}
   p.current.xyz=lerp(Rotate(ra,rb,rc,r)+float3(ra.w,rb.w,rc.w),Rotate(la,lb,lc,l)+float3(la.w,lb.w,lc.w),mix);
   if(mode==2 && !paused)
   {
    float3 lp,rp;
    if(SkinInverse(pa,pb,pc,p.previous.xyz,lp) && SkinInverse(qa,qb,qc,p.previous.xyz,rp))
     p.previous.xyz=lerp(Rotate(ra,rb,rc,rp)+float3(ra.w,rb.w,rc.w),Rotate(la,lb,lc,lp)+float3(la.w,lb.w,lc.w),mix);
   }
  }
 }
 LegFrames[at+11]=la;LegFrames[at+12]=lb;LegFrames[at+13]=lc;LegFrames[at+14]=ra;LegFrames[at+15]=rb;LegFrames[at+16]=rc;return true;
}
[numthreads(64,1,1)]void main(uint3 id:SV_DispatchThreadID)
{
 uint i=id.x;if(i>=PARTICLE_COUNT || State[23].w<.5 || PaletteDecision[0].w<.5)return;
 uint mode=(uint)IniParams[127].x,slot=(uint)clamp(round(IniParams[118].w),1,2);bool capture=mode==1;
 if(capture && State[9].y<.5 && IniParams[127].y<.5)return;
 if(i==0 && mode==0){float4 flags=State[9];flags.y=0;State[9]=flags;FollowAxes();}
 bool solving=State[9].y>.5 || IniParams[127].y>.5;
 bool paused=mode==0 || !solving || State[16+slot].z>1.5;
 if(mode==0 && GarmentAmount(State[0].xy)<1e-5)
 {if(i==0){float4 initialized=State[16+slot];initialized.z=2;State[16+slot]=initialized;}return;}
 Particle p=Particles[i];Blend weights=Weights[i];float4 a,b,c;
 bool initialize=FrozenMesh[i].w!=State[2].x || (mode==2 && State[9].x>.5);
 if(!capture && initialize)
 {
  weights.weights=OriginalBlend[Representatives[i]*2];weights.indices=asuint(OriginalBlend[Representatives[i]*2+1]);Weights[i]=weights;
  p.current=float4(Source[Representatives[i]].position,1);p.previous=float4(p.current.xyz,State[2].x);
 }
 Rows(weights,a,b,c);
 bool dual=FollowLegs(i,capture,initialize,mode,paused,p);
 if(capture || initialize)
 {
  float3 local;
  if(!SkinInverse(a,b,c,p.current.xyz,local))
  {
   uint best=0;[unroll]for(uint k=1;k<4;++k)if(weights.weights[k]>weights.weights[best])best=k;
   weights.indices=uint4(weights.indices[best],0,0,0);weights.weights=float4(1,0,0,0);Rows(weights,a,b,c);
   if(!SkinInverse(a,b,c,p.current.xyz,local))return;Weights[i]=weights;
  }
  FrozenMesh[i]=float4(local,State[2].x);
 }
 else
 {
  float4 pa=PreviousRows[i*3],pb=PreviousRows[i*3+1],pc=PreviousRows[i*3+2];
  if(!dual && (any(a!=pa) || any(b!=pb) || any(c!=pc)))
  {
   float3 local=FrozenMesh[i].xyz;
   // Explicit solver replays may edit particles between steps. Normal input
   // always skins the immutable last simulated mesh instead.
   if(IniParams[127].y>.5)SkinInverse(pa,pb,pc,p.current.xyz,local);
   p.current.xyz=Rotate(a,b,c,local)+float3(a.w,b.w,c.w);
   if(mode==2 && !paused)
   {float3 previous;if(SkinInverse(pa,pb,pc,p.previous.xyz,previous))p.previous.xyz=Rotate(a,b,c,previous)+float3(a.w,b.w,c.w);}
  }
  p.current.w=1;if(mode!=2 || paused)p.previous=float4(p.current.xyz,State[2].x);
 }
 PreviousRows[i*3]=a;PreviousRows[i*3+1]=b;PreviousRows[i*3+2]=c;Particles[i]=p;
 if(i==0)
 {
  float4 initialized=State[16+slot];
  if(initialized.z<.5 || initialized.w!=State[2].x)initialized=float4(State[0].x,State[8].z,1,State[2].x);
  if(capture)
  {
   initialized=float4(State[0].x,State[8].z,1,State[2].x);
   if(TwoEnded()){float4 history=State[11];if(slot==1)history.x=State[0].y;else history.y=State[0].y;State[11]=history;}
  }
  initialized.z=capture || (mode==2 && solving)?1:2;State[16+slot]=initialized;
 }
}
