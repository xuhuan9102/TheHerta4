#include "constants.hlsli"
#include "common.hlsli"
Buffer<float4> Rest : register(t75);
Buffer<uint> Representatives : register(t85);
Buffer<uint> WearMap : register(t94);
Buffer<uint> FrameAxes : register(t95);
Buffer<uint> BodySeeds : register(t121);
Buffer<float4> BodyDirections : register(t122);
Buffer<uint> AttachmentRoles : register(t126);
Buffer<uint> Faces : register(t79);
Buffer<uint> FaceColors : register(t100);
Buffer<float4> Constraints : register(t113);
Buffer<uint> EdgeOffsets : register(t114);
Buffer<uint> FaceOffsets : register(t115);
Buffer<uint> HemMap : register(t116);
Buffer<float4> HemParams : register(t117);
Buffer<float4> HullPoints : register(t118);
Buffer<uint> HullCount : register(t119);
Buffer<float4> OriginalBlend : register(t86);
Buffer<float4> Skeleton1 : register(t2);
Buffer<float4> Skeleton2 : register(t3);
Buffer<float4> PaletteDecision : register(t96);
StructuredBuffer<float4> Detect : register(t67);
RWStructuredBuffer<Particle> Particles : register(u0);
RWBuffer<float4> State : register(u1);
RWBuffer<uint> Heads : register(u2);
RWBuffer<uint> Links : register(u3);
RWBuffer<float> Lambdas : register(u4);
RWBuffer<float4> Contacts : register(u5);
RWBuffer<float4> PreviousRows : register(u6);
RWStructuredBuffer<Blend> Weights : register(u7);

groupshared float Position[PARTICLE_COUNT*3];
groupshared float4 Planes[64];
groupshared float HemTargets[HEM_COUNT*3];
groupshared float4 FootAndHeight,UpAxis,SideAxis,ForwardAxis,HemCenter;
groupshared float4 Parameters;
groupshared uint Cold;
#if !defined(STATIC_PHASE) || STATIC_PHASE == 12
groupshared uint QualityUnsafe[64];
groupshared uint QualitySkip,QualityStart,QualityCount,QualitySlot;
#endif

float3 P(uint i){return float3(Position[i*3],Position[i*3+1],Position[i*3+2]);}
void SetP(uint i,float3 p){Position[i*3]=p.x;Position[i*3+1]=p.y;Position[i*3+2]=p.z;}
float3 Hem(uint i){return float3(HemTargets[i*3],HemTargets[i*3+1],HemTargets[i*3+2]);}
void SetHem(uint i,float3 p){HemTargets[i*3]=p.x;HemTargets[i*3+1]=p.y;HemTargets[i*3+2]=p.z;}
float Mass(uint i){return (!IntrinsicSurface() && HemMap[i]<HEM_COUNT) || Rest[i*2].w<.5 ? 0:1;}
float ContactMass(uint i){return SheetCollision()?(AttachmentRoles[i]==3?0:1):(HemMap[i]<HEM_COUNT?0:(Config[15].w>.5?Mass(i):1));}
float3 PoseCoordinates(float3 p,float4 foot,float3 up,float3 side,float3 forward,float scale)
{
 float3 d=p-foot.xyz;return float3(dot(d,side),dot(d,forward),dot(d,up))/max(scale,.001);
}
float3 PoseSample(uint i,uint samples,uint count)
{
 uint tri=BodyOrder[i*count/samples];return Source[BodyVertices[tri*3]].position;
}
uint NodeRegion(uint i)
{
    if(Sliding())return Rest[i*2+1].w<0?1:2;
    if(HemMap[i]<HEM_COUNT && !ManualSurface() && !Sliding())return 0;
    if(Config[12].w>.5)return (uint)Config[12].w;
    if(ManualSurface())return 3;
    if(HemMap[i]<HEM_COUNT)return Sliding()?3:0;
    float side=Rest[i*2+1].w/max(Config[6].w,1e-6);
    return abs(side)<.004?3:(side<0?1:2);
}
uint ContactSeed(uint i)
{
 if(ManualAnchored() && Config[14].x>.5 && AttachmentRoles[i]!=0)return BodySeeds[i];
 return asuint(Contacts[i*3+2].x);
}

void Rows(Blend v,out float4 a,out float4 b,out float4 c)
{
    a=0;b=0;c=0;bool second=PaletteDecision[0].x>1.5;uint count;
    if(second)Skeleton2.GetDimensions(count);else Skeleton1.GetDimensions(count);count/=3;
    [unroll]for(uint k=0;k<4;++k)
    {
        uint bone=v.indices[k];float w=v.weights[k];if(w<=0 || bone>=count)continue;
        if(second){a+=Skeleton2[bone*3]*w;b+=Skeleton2[bone*3+1]*w;c+=Skeleton2[bone*3+2]*w;}
        else {a+=Skeleton1[bone*3]*w;b+=Skeleton1[bone*3+1]*w;c+=Skeleton1[bone*3+2]*w;}
    }
}
float3 Rotate(float4 a,float4 b,float4 c,float3 v){return float3(dot(a.xyz,v),dot(b.xyz,v),dot(c.xyz,v));}
float3 Inverse(float4 a,float4 b,float4 c,float3 v)
{
    float3 x=cross(b.xyz,c.xyz),y=cross(c.xyz,a.xyz),z=cross(a.xyz,b.xyz);float det=dot(a.xyz,x);
    return abs(det)>1e-7?(x*v.x+y*v.y+z*v.z)/det:v;
}
Blend Transfer(uint tri,float3 bary)
{
    uint ids[12];float w[12];
    [loop]for(uint c=0;c<3;++c)
    {
        uint row=BodyVertices[tri*3+c];float4 v=OriginalBlend[row*2];uint4 b=asuint(OriginalBlend[row*2+1]);
        [unroll]for(uint k=0;k<4;++k){ids[c*4+k]=b[k];w[c*4+k]=max(v[k]*bary[c],0);}
    }
    [loop]for(uint a=0;a<12;++a)
        [loop]for(uint b=0;b<a;++b)if(ids[a]==ids[b]){w[b]+=w[a];w[a]=0;break;}
    Blend r;r.weights=0;r.indices=0;
    [unroll]for(uint rank=0;rank<4;++rank)
    {
        uint best=0;float value=-1;
        [loop]for(uint k=0;k<12;++k)if(w[k]>value){best=k;value=w[k];}
        r.weights[rank]=max(value,0);r.indices[rank]=ids[best];w[best]=-1;
    }
    r.weights/=max(dot(r.weights,1.0),1e-8);return r;
}
void UpdateAxes()
{
    // Explicit paths overwrite the transverse axes from their current bone.
    // Retain the same native foot/hem centroids without reading every vertex.
    uint axesCount;FrameAxes.GetDimensions(axesCount);
    if(ManualSurface() && axesCount>=2)
    {
        uint nf=FrameAxes[0],nt=FrameAxes[1];
        if(axesCount==2+nf+nt)
        {
            float3 foot=0,top=0;
            [loop]for(uint k=0;k<nf;++k)foot+=Source[FrameAxes[2+k]].position;
            [loop]for(uint k=0;k<nt;++k)top+=Source[FrameAxes[2+nf+k]].position;
            foot/=max(nf,1);top/=max(nt,1);
            FootAndHeight=float4(foot,length(top-foot));UpAxis=float4(SafeUnit(top-foot,float3(-1,0,0)),0);
            SideAxis=Config[7];ForwardAxis=Config[9];return;
        }
    }
    float3 feet=0,top=0,left=0,right=0,front=0,back=0;float nf=0,nt=0,nl=0,nr=0,na=0,nb=0;
    [loop]for(uint i=0;i<PARTICLE_COUNT;++i)
    {
        float3 p=Source[Representatives[i]].position;float4 meta=Rest[i*2+1];
        uint hem=HemMap[i],upper=TwoEnded()?(uint)Config[13].x:HEM_COUNT;
        if(TwoEnded()?(hem>=upper && hem<HEM_COUNT):(Rest[i*2+1].z<.065)){feet+=p;nf+=1;}
        if(hem<upper){top+=p;nt+=1;}
        if(meta.z>.4 && meta.z<.75)
        {
            if(meta.w-Config[12].x<0){left+=p;nl+=1;}else{right+=p;nr+=1;}
            float y=dot(Rest[i*2].xyz-Config[8].xyz,Config[9].xyz)-Config[12].y;
            if(y<-.015){front+=p;na+=1;}else if(y>.015){back+=p;nb+=1;}
        }
    }
    feet/=max(nf,1);top/=max(nt,1);left/=max(nl,1);right/=max(nr,1);front/=max(na,1);back/=max(nb,1);
    float3 up=SafeUnit(top-feet,float3(-1,0,0));float3 side=right-left;side=SafeUnit(side-up*dot(side,up),float3(0,1,0));
    float3 forward=SafeUnit(cross(up,side),float3(0,0,1));if(dot(forward,back-front)<0)forward=-forward;
    FootAndHeight=float4(feet,length(top-feet));UpAxis=float4(up,0);SideAxis=float4(side,0);ForwardAxis=float4(forward,0);
}
void Support(uint direction)
{
 float angle=6.28318530718*direction/64;float3 n=SideAxis.xyz*cos(angle)+ForwardAxis.xyz*sin(angle);float h=-1e20;
 uint count=HullCount[0];[loop]for(uint k=0;k<count;++k)h=max(h,dot(HullPoints[k].xyz,n));
 Planes[direction]=float4(n,max(h,.005)+max(Config[4].w,ClothGap()));
}
void Edge(uint index,float dt,uint iteration)
{
    float4 e=Constraints[index];uint a=asuint(e.x),b=asuint(e.y);float wa=Mass(a),wb=Mass(b),mass=wa+wb;
    if(mass<=0)return;float3 pa=P(a),pb=P(b),d=pa-pb;float len=length(d);if(len<1e-9)return;
    float4 material=ClothMaterial();
    float compliance=(e.w>.5?material.y:material.x)*e.z;
    float alpha=compliance/(dt*dt);float old=iteration==0?0:Lambdas[index];float rest=e.z*Parameters.z;
    if(Config[11].x>.5 && len<rest)
    {
        if(e.w<.5 || material.w<=0 || len>=rest*material.w){Lambdas[index]=0;return;}
        rest*=material.w;
    }
    float lambda=(-(len-rest)-alpha*old)/(mass+alpha);
    if(e.w<.5 && len>rest*Config[5].w)lambda=min(lambda,-(len-rest*Config[5].w)/mass);
    lambda=clamp(lambda,-max(len-rest,0)/mass,max(rest-len,0)/mass);
    float maximum=.004*Parameters.z;lambda=clamp(lambda,-maximum,maximum);
    Lambdas[index]=old+lambda;float3 correction=d*(lambda/len);
    SetP(a,pa+correction*wa);SetP(b,pb-correction*wb);
}
bool Hit(float3 p,float3 end,float3 a,float3 b,float3 c)
{
    float3 d=end-p,ab=b-a,ac=c-a,h=cross(d,ac);float den=dot(ab,h);if(abs(den)<1e-13)return false;
    float3 v=p-a,q=cross(v,ab);float u=dot(v,h)/den,w=dot(d,q)/den,t=dot(ac,q)/den;
    return u>=-1e-6 && w>=-1e-6 && u+w<=1.000001 && t>=0 && t<=1;
}
void SeparationAxis(float3 axis,float3 outward,float3 p[3],float3 q[3],float gap,inout float best,inout float3 delta)
{
 float lengthSquared=dot(axis,axis);if(lengthSquared<1e-16)return;axis*=rsqrt(lengthSquared);
 float3 x=float3(dot(p[0],axis),dot(p[1],axis),dot(p[2],axis)),y=float3(dot(q[0],axis),dot(q[1],axis),dot(q[2],axis));
 float positive=max(y.x,max(y.y,y.z))-min(x.x,min(x.y,x.z))+gap;
 float negative=max(x.x,max(x.y,x.z))-min(y.x,min(y.y,y.z))+gap;
 if(dot(axis,outward)>=-1e-6 && positive>=0 && positive<best){best=positive;delta=axis*positive;}
 if(dot(axis,outward)<=1e-6 && negative>=0 && negative<best){best=negative;delta=-axis*negative;}
}
float3 FaceSeparation(float3 p[3],float3 q[3],float3 outward,float gap)
{
 float best=1e20;float3 delta=0;
 SeparationAxis(cross(p[1]-p[0],p[2]-p[0]),outward,p,q,gap,best,delta);
 SeparationAxis(outward,outward,p,q,gap,best,delta);
 [unroll]for(uint a=0;a<3;++a)[unroll]for(uint b=0;b<3;++b)SeparationAxis(cross(p[(a+1)%3]-p[a],q[(b+1)%3]-q[b]),outward,p,q,gap,best,delta);
 return delta;
}
void FaceScoped(uint face)
{
 uint3 ids=uint3(Faces[face*3],Faces[face*3+1],Faces[face*3+2]);float3 p[3];p[0]=Particles[ids.x].current.xyz;p[1]=Particles[ids.y].current.xyz;p[2]=Particles[ids.z].current.xyz;
 float3 old[3];old[0]=p[0];old[1]=p[1];old[2]=p[2];float3 mass=float3(ContactMass(ids.x),ContactMass(ids.y),ContactMass(ids.z));if(dot(mass,1.0)<.5)return;
 float scale=State[8].z,gap=ClothGap()*scale;FaceCenterContact(p[0],p[1],p[2],mass,scale,ContactSeed(ids.x));
 float3 lo=min(p[0],min(p[1],p[2])),hi=max(p[0],max(p[1],p[2]));uint node=1,count,leaves=(uint)IniParams[115].y;BodyOrder.GetDimensions(count);
 [loop]while(node>0)
 {
  float3 nlo=BodyBounds[node*2].xyz,nhi=BodyBounds[node*2+1].xyz;
  if(all(lo<=nhi) && all(hi>=nlo) && all(nlo<=nhi))
  {
   if(node<leaves){node*=2;continue;}uint at=node-leaves;
   if(at<count && CollisionAllowed(BodyOrder[at]))
   {
    uint tri=BodyOrder[at];uint3 bi=uint3(BodyVertices[tri*3],BodyVertices[tri*3+1],BodyVertices[tri*3+2]);Vertex a=Source[bi.x],b=Source[bi.y],c=Source[bi.z];float3 q[3];q[0]=a.position;q[1]=b.position;q[2]=c.position;bool pierced=false;
    [unroll]for(uint edge=0;edge<3;++edge)pierced=pierced||Hit(p[edge],p[(edge+1)%3],q[0],q[1],q[2])||Hit(q[edge],q[(edge+1)%3],p[0],p[1],p[2]);
    if(pierced)
    {
     float3 n=cross(q[1]-q[0],q[2]-q[0]);if(dot(n,a.normal+b.normal+c.normal)<0)n=-n;n=SafeUnit(n,float3(0,1,0));float3 delta=FaceSeparation(p,q,n,gap);float distance=length(delta);if(distance>.004*scale)delta*=.004*scale/distance;
     p[0]+=delta*mass.x;p[1]+=delta*mass.y;p[2]+=delta*mass.z;lo=min(p[0],min(p[1],p[2]));hi=max(p[0],max(p[1],p[2]));
    }
   }
  }
  [loop]while(node>1 && (node&1)!=0)node>>=1;if(node==1)break;++node;
 }
 [unroll]for(uint k=0;k<3;++k)
 {
  p[k]=KeepOutside(old[k],p[k],gap);float3 delta=p[k]-old[k];
  if(all(isfinite(p[k])) && length(delta)<.05*scale){Particle particle=Particles[ids[k]];particle.current.xyz=p[k];particle.previous.xyz+=delta;Particles[ids[k]]=particle;}
 }
}
bool FaceBodyIntersection(uint3 ids)
{
 // Explicit corners avoid the SM5 compiler's local-array aliasing across
 // the BVH loop and inlined segment tests (the second corner was lost).
 float3 p0=Particles[ids.x].current.xyz,p1=Particles[ids.y].current.xyz,p2=Particles[ids.z].current.xyz;
 float3 lo=min(p0,min(p1,p2)),hi=max(p0,max(p1,p2));uint count;BodyOrder.GetDimensions(count);uint node=1,leaves=(uint)IniParams[115].y;
 [loop]while(node>0)
 {
  float3 nlo=BodyBounds[node*2].xyz,nhi=BodyBounds[node*2+1].xyz;
  if(all(lo<=nhi) && all(hi>=nlo) && all(nlo<=nhi))
  {
   if(node<leaves){node*=2;continue;}uint at=node-leaves;
   if(at<count && CollisionAllowed(BodyOrder[at]))
   {
    uint tri=BodyOrder[at];float3 q0=Source[BodyVertices[tri*3]].position,q1=Source[BodyVertices[tri*3+1]].position,q2=Source[BodyVertices[tri*3+2]].position;
    if(Hit(p0,p1,q0,q1,q2) || Hit(p1,p2,q0,q1,q2) || Hit(p2,p0,q0,q1,q2) || Hit(q0,q1,p0,p1,p2) || Hit(q1,q2,p0,p1,p2) || Hit(q2,q0,p0,p1,p2))return true;
   }
  }
  [loop]while(node>1 && (node&1)!=0)node>>=1;if(node==1)break;++node;
 }
 return false;
}

void FaceGlobal(uint face)
{
    uint3 ids=uint3(Faces[face*3],Faces[face*3+1],Faces[face*3+2]);float3 p[3];p[0]=Particles[ids.x].current.xyz;p[1]=Particles[ids.y].current.xyz;p[2]=Particles[ids.z].current.xyz;
    // Elastic anchors retain their material targets, while contact can offset
    // every corner to keep a cuff or foot from crossing the body.
    float3 old[3];old[0]=p[0];old[1]=p[1];old[2]=p[2];float3 mass=float3(ContactMass(ids.x),ContactMass(ids.y),ContactMass(ids.z));
    if(dot(mass,1.0)<.5)return;
    FaceCenterContact(p[0],p[1],p[2],mass,State[8].z,ContactSeed(ids.x));
    float3 lo=min(p[0],min(p[1],p[2])),hi=max(p[0],max(p[1],p[2]));uint node=1,count,leaves=(uint)IniParams[115].y;BodyOrder.GetDimensions(count);
    [loop]while(node>0)
    {
        float3 nlo=BodyBounds[node*2].xyz,nhi=BodyBounds[node*2+1].xyz;
        if(all(lo<=nhi) && all(hi>=nlo) && all(nlo<=nhi))
        {
            if(node<leaves){node*=2;continue;}
            uint at=node-leaves;
            // Body blocking is independent of the garment's adhesion chart.
            if(at<count && Allowed(BodyOrder[at],0))
            {
                uint tri=BodyOrder[at];uint3 bi=uint3(BodyVertices[tri*3],BodyVertices[tri*3+1],BodyVertices[tri*3+2]);
                Vertex a=Source[bi.x],b=Source[bi.y],c=Source[bi.z];float3 q[3];q[0]=a.position;q[1]=b.position;q[2]=c.position;
                float3 n=cross(b.position-a.position,c.position-a.position);if(dot(n,a.normal+b.normal+c.normal)<0)n=-n;n=SafeUnit(n,float3(0,1,0));
                [unroll]for(uint edge=0;edge<3;++edge)
                {
                    uint next=(edge+1)%3;
                    if(Hit(p[edge],p[next],q[0],q[1],q[2]))
                    {
                        float x=ClothGap()*State[8].z-dot(p[edge]-a.position,n),y=ClothGap()*State[8].z-dot(p[next]-a.position,n);
                        if(x>0 && mass[edge]>.5)p[edge]+=n*min(x,.002*State[8].z);
                        if(y>0 && mass[next]>.5)p[next]+=n*min(y,.002*State[8].z);
                    }
                }
                bool pierced=false;[unroll]for(uint edge=0;edge<3;++edge)pierced=pierced||Hit(q[edge],q[(edge+1)%3],p[0],p[1],p[2]);
                if(pierced)
                {
                    float3 g=SafeUnit(cross(p[1]-p[0],p[2]-p[0]),n);if(dot(g,n)<0)g=-g;
                    [unroll]for(uint v=0;v<3;++v)
                    {
                        float3 closest,bary;ClosestTriangle(q[v],p[0],p[1],p[2],closest,bary);float depth=dot(q[v]-closest,g)+ClothGap()*State[8].z;
                        if(depth>0 && distance(q[v],closest)<.05*State[8].z)
                        {
                            float denominator=max(dot(bary*bary,mass),1e-6);float amount=min(depth,.002*State[8].z)/denominator;
                            [unroll]for(uint k=0;k<3;++k)p[k]+=g*min(amount*bary[k]*mass[k],.003*State[8].z);
                        }
                    }
                }
            }
        }
        [loop]while(node>1 && (node&1)!=0)node>>=1;
        if(node==1)break;++node;
    }
    [unroll]for(uint k=0;k<3;++k)
    {
        float3 delta=p[k]-old[k];if(mass[k]>.5 && all(isfinite(p[k])) && length(delta)<.03*State[8].z)
        {
            Particle particle=Particles[ids[k]];particle.current.xyz=p[k];particle.previous.xyz+=delta;Particles[ids[k]]=particle;
        }
    }
}
void Control()
{
    float4 a=State[0],b=State[1];bool reset=b.w<.5 || b.z!=IniParams[116].z;
    if(reset){a=0;b=float4(-1,0,IniParams[116].z,1);float4 epoch=State[2];epoch.x+=1;State[2]=epoch;}
    bool held=IniParams[117].x>.5;float cursor=IniParams[116].w,screen=max(IniParams[117].y,1);
    float cursorX=IniParams[118].x;
    uint slot=(uint)clamp(round(IniParams[118].w),1,2);
    float4 initialized=State[16+slot];
    if(initialized.z<.5 || initialized.w!=State[2].x)
    {
        a.z=0;a.w=cursor;b.x=-1;b.y=cursorX;State[0]=a;State[1]=b;return;
    }
    if(held && a.z<.5)
    {
        b.x=-1;uint n,stride;Detect.GetDimensions(n,stride);
        if(n>7 && Detect[0].x>=0)
        {
            uint nearest=(uint)clamp(round(Detect[3].w),0,2),row=(uint)max(Detect[3][nearest],0);
            if(row<(uint)Config[0].y)
            {
                uint particle=WearMap[row];
                if(particle<PARTICLE_COUNT)
                {
                    b.x=Rest[particle*2+1].y;
                    uint zones;ControlZones.GetDimensions(zones);
                    uint zone=(uint)max(round(Detect[7].w),0);
                    uint code=zone<zones?(uint)ControlZones[zone].x:0;
                    if(code==0)b.x=-1;
                    else if(TwoEnded() || code!=3)
                    {
                        {
                            b.x=code==3?(Rest[particle*2+1].z>=.5?0:1):(code==1?0:1);
                            float signValue=b.x<.5?-1:1;
                            float3 direction=State[5].xyz*signValue;
                            // Use the same cursor/basis convention as ordinary
                            // dragging: local motion = right*dx + basis[9]*dy.
                            float2 projected=float2(dot(direction,Detect[8].xyz),dot(direction,Detect[9].xyz));
                            float lengthValue=length(projected);
                            bool basisValid=n>9 && Detect[8].w>.5 && Detect[9].w>1e-5;
                            projected=basisValid && isfinite(lengthValue) && lengthValue>1e-8?projected/lengthValue:float2(0,signValue);
                            float maximum=ControlZones[zone].w>.5?ControlZones[zone].y:1;
                            State[14]=float4(projected,maximum,1);
                        }
                    }
                    else State[14]=0;
                }
            }
        }
    }
    if(held && a.z>.5 && b.x>=0)
    {
        if(TwoEnded() || State[14].w>.5)
        {
            float2 motion=float2((cursorX-b.y)/max(IniParams[117].w,1),(cursor-a.w)/screen);
            float delta=clamp(dot(motion,State[14].xy)*1.6,-Config[4].z,Config[4].z);
            if(!TwoEnded()){a.x=clamp(a.x+delta,0,State[14].z);a.y=a.x;}
            else if(b.x<.5){a.x=clamp(a.x+delta,0,State[14].z);a.y=min(a.y,1-a.x);}
            else{a.y=clamp(a.y+delta,0,State[14].z);a.x=min(a.x,1-a.y);}
            if(Branched()){a.x=0;a.y=min(a.y,Config[14].x/max(1-Config[13].w,.001));}
        }
        else
        {
            float delta=clamp((a.w-cursor)/screen*1.6,-Config[4].z,Config[4].z);
            if(Config[0].w>.5){a.x=saturate(a.x+delta);a.y=a.x;}
            else if(b.x<.5)a.x=saturate(a.x+delta);else a.y=saturate(a.y+delta);
        }
    }
    if(!held)b.x=-1;a.z=held?1:0;a.w=cursor;b.y=cursorX;State[0]=a;State[1]=b;
}


#if defined(STATIC_PHASE) && STATIC_PHASE == 12
[numthreads(64,1,1)]
void main(uint lane:SV_GroupIndex)
{
 if(lane==0)
 {
  QualitySlot=(uint)clamp(round(IniParams[118].w),1,2);
  QualityCount=PARTICLE_COUNT+(uint)Config[0].z;
  QualityStart=220+(QualitySlot-1)*QualityCount;
  uint rows;State.GetDimensions(rows);
  bool idle=GarmentAmount(State[0].xy)<1e-5 && GarmentAmount(float2(State[8].x,State[10].x))<1e-5;
  float sleep=State[ClothSleepRow()].x;bool transport=IniParams[125].y>.5;
  bool skip=!ClosedDomain() || idle || State[23].w<.5 || (transport?(!AdaptiveCloth() || sleep<1.5):sleep>.5);
  skip=skip || (State[9].y<.5 && IniParams[127].y<.5);
  skip=skip || (IniParams[119].w>.5 && State[3][QualitySlot-1]>.5);
  QualitySkip=skip || rows<QualityStart+QualityCount?1:0;
 }
 GroupMemoryBarrierWithGroupSync();if(QualitySkip!=0)return;
 uint unsafe=0;
 [loop]for(uint check=lane;check<QualityCount;check+=64)
 {float4 value=State[QualityStart+check];unsafe|=value.w<.5 || value.x>.5 || value.z>.5?1:0;}
 QualityUnsafe[lane]=unsafe;GroupMemoryBarrierWithGroupSync();
 [unroll]for(uint width=32;width>0;width>>=1)
 {if(lane<width)QualityUnsafe[lane]|=QualityUnsafe[lane+width];GroupMemoryBarrierWithGroupSync();}
 if(lane==0){float4 result=State[3];if(QualitySlot==1)result.x=QualityUnsafe[0]==0?1:0;else result.y=QualityUnsafe[0]==0?1:0;State[3]=result;}
}
#else
[numthreads(64,1,1)]
void main(uint lane:SV_GroupIndex,uint3 dispatch:SV_DispatchThreadID)
{
 uint i=dispatch.x;
#ifdef STATIC_PHASE
 uint phase=STATIC_PHASE;
#else
 uint phase=(uint)IniParams[116].x;
#endif
 uint sourceCount,stride,particleCount,pstride,edges,faceCount,hemCount,ec,fc,bc,order;
 Source.GetDimensions(sourceCount,stride);Particles.GetDimensions(particleCount,pstride);Constraints.GetDimensions(edges);
 Faces.GetDimensions(faceCount);HemParams.GetDimensions(hemCount);EdgeOffsets.GetDimensions(ec);FaceOffsets.GetDimensions(fc);
 FaceColors.GetDimensions(bc);BodyOrder.GetDimensions(order);
 bool valid=sourceCount==(uint)Config[0].y && stride==40 && particleCount==PARTICLE_COUNT && pstride==32 && edges==(uint)Config[1].x && faceCount==(uint)Config[0].z*3 && hemCount==HEM_COUNT && ec==EDGE_COLORS+1 && fc==FACE_COLORS+1 && bc==(uint)Config[0].z && PaletteDecision[0].w>.5;

 uint bindCount,bindStride,bodyIndices,extCount,contactCount,blendCount,mapCount,repCount,restCount,hemMapCount;
 BindVertices.GetDimensions(bindCount,bindStride);BodyVertices.GetDimensions(bodyIndices);BodyExterior.GetDimensions(extCount);BodyContacts.GetDimensions(contactCount);
 OriginalBlend.GetDimensions(blendCount);WearMap.GetDimensions(mapCount);Representatives.GetDimensions(repCount);Rest.GetDimensions(restCount);HemMap.GetDimensions(hemMapCount);
 valid=valid && bindCount==sourceCount && bindStride==40 && bodyIndices==order*3 && extCount==order && contactCount==order && blendCount==sourceCount*2 && mapCount==sourceCount && repCount==PARTICLE_COUNT && restCount==PARTICLE_COUNT*2 && hemMapCount==PARTICLE_COUNT;
 uint adjacencyCount,surfaceCount;BodyAdjacency.GetDimensions(adjacencyCount);SurfaceMask.GetDimensions(surfaceCount);valid=valid && adjacencyCount==order*3 && surfaceCount==order;
 uint ownerCount;BodyOwners.GetDimensions(ownerCount);valid=valid && ownerCount==order;
 uint seedCount;BodySeeds.GetDimensions(seedCount);valid=valid && seedCount==PARTICLE_COUNT;
 uint directionCount;BodyDirections.GetDimensions(directionCount);valid=valid && (Config[15].z<.5 || directionCount==PARTICLE_COUNT);
 uint roleCount;AttachmentRoles.GetDimensions(roleCount);valid=valid && (Config[14].x<.5 || !ManualAnchored() || roleCount==PARTICLE_COUNT);
 uint channelCount;BodyChannels.GetDimensions(channelCount);
 if(Config[15].z>.5)valid=valid && channelCount>=7 && all(isfinite(BodyChannels[0])) && BodyChannels[0].z>=2 && BodyChannels[0].y>BodyChannels[0].x && channelCount==1+3*(uint)BodyChannels[0].z;
 if(i==0 && (phase==0 || phase==8 || !valid))State[23]=float4(PARTICLE_COUNT,edges,order,valid?1:0);if(!valid)return;
 if(phase!=0 && phase!=8 && State[23].w<.5)return;
 if(phase!=0 && phase!=8 && State[9].y<.5 && IniParams[127].y<.5)return;
 bool idle=GarmentAmount(State[0].xy)<1e-5 && GarmentAmount(float2(State[8].x,State[10].x))<1e-5;
 if(AdaptiveCloth() && IniParams[119].z>.5 && IniParams[119].x>=ClothSubsteps(State[ClothWorkRow()]) && (phase==4 || phase==7))return;
 if(idle && (phase==4 || phase==5 || phase==7 || phase==9 || phase==10))return;
 if((phase==4 || phase==5 || phase==7 || phase==9 || phase==10) && State[ClothSleepRow()].x>.5)return;
 if(ClosedDomain() && IniParams[119].w>.5 && State[3][(uint)clamp(round(IniParams[118].w),1,2)-1]>.5 && (phase==5 || phase==10 || phase==11))return;
 if(phase==0){if(i==0)Control();return;}
 if(phase==8)
 {
  if(i==0)
  {
   UpdateAxes();uint slot=(uint)clamp(round(IniParams[118].w),1,2);
   float4 initialized=State[16+slot];
   float previousAmount=TwoEnded()?max(initialized.x,State[11][slot-1]):initialized.x;
   uint cold=initialized.z<.5 || initialized.w!=State[2].x || Particles[0].previous.w!=State[2].x || previousAmount<=.00001?1:0;
   float4 w=OriginalBlend[Representatives[(uint)HemParams[0].w]*2];uint4 ids=asuint(OriginalBlend[Representatives[(uint)HemParams[0].w]*2+1]);uint best=0;
   [unroll]for(uint k=1;k<4;++k)if(w[k]>w[best])best=k;
   Blend root;root.weights=float4(1,0,0,0);root.indices=uint4(ids[best],0,0,0);float4 a,b,c;Rows(root,a,b,c);
   float scale=max((length(a.xyz)+length(b.xyz)+length(c.xyz))/3,.001);
   bool geometryReady=all(isfinite(FootAndHeight)) && isfinite(scale) && FootAndHeight.w>Config[6].w*scale*.1;
   if(!geometryReady){float4 health=State[23];health.w=0;State[23]=health;State[9]=float4(1,0,0,0);return;}
   if(Config[15].w>.5)
   {
    float3 top=FootAndHeight.xyz+UpAxis.xyz*FootAndHeight.w;
    float3 axis=SafeUnit(Rotate(a,b,c,Config[6].xyz),UpAxis.xyz);
    float3 side=Rotate(a,b,c,Config[7].xyz);side=SafeUnit(side-axis*dot(side,axis),SideAxis.xyz);
    FootAndHeight=float4(top-axis*Config[6].w*scale,Config[6].w*scale);
    UpAxis=float4(axis,0);SideAxis=float4(side,0);ForwardAxis=float4(SafeUnit(cross(axis,side),ForwardAxis.xyz),0);
   }
   uint rows;State.GetDimensions(rows);
   uint sleepRow=ClothSleepRow(),poseRow=ClothPoseRow(),samples=min(order,64u);
   bool active=GarmentAmount(State[0].xy)>1e-5;
   bool supported=rows>=220 && IniParams[124].y>.5 && IniParams[123].y>.5 && samples>0;
   float4 sleep=State[sleepRow],sleepControl=State[sleepRow+1];
   bool inputChanged=any(abs(State[0].xy-sleep.zw)>1e-6) || sleepControl.x!=State[2].x || abs(State[poseRow].w-ClothGap())>1e-7;
   bool inputGrabbed=IniParams[117].x>.5 && State[1].x>=0;
   bool following=AdaptiveCloth() && State[3][slot-1]>.5 && !inputChanged && !inputGrabbed;
   float3 lo=1e20,hi=-1e20;float clothMotion=0,poseMotion=0;
   float oldScale=max(State[8].z,.001);
   if(supported && active && cold==0)
   {
    [loop]for(uint j=0;j<(following?0:PARTICLE_COUNT);++j)
    {
     float3 samplePosition=PoseCoordinates(Particles[j].current.xyz,State[4],State[5].xyz,State[6].xyz,State[7].xyz,oldScale);
     lo=min(lo,samplePosition);hi=max(hi,samplePosition);
    }
    [loop]for(uint k=0;k<(following?0:32);++k)
    {
     uint node=k*PARTICLE_COUNT/32;
     float3 samplePosition=PoseCoordinates(Particles[node].current.xyz,State[4],State[5].xyz,State[6].xyz,State[7].xyz,oldScale);
     clothMotion=max(clothMotion,distance(samplePosition,State[poseRow+64+k].xyz));
    }
    [loop]for(uint k=0;k<samples;++k)
    {
     float3 samplePosition=PoseCoordinates(PoseSample(k,samples,order),FootAndHeight,UpAxis.xyz,SideAxis.xyz,ForwardAxis.xyz,scale),old=State[poseRow+k].xyz;
     float padding=max(.05,ClothGap()*3);
     float3 outside=max(max(lo-samplePosition,samplePosition-hi),0),oldOutside=max(max(lo-old,old-hi),0);
     if(following || min(length(outside),length(oldOutside))<=padding)poseMotion=max(poseMotion,distance(samplePosition,old));
    }
   }
   bool changed=any(abs(State[0].xy-sleep.zw)>1e-6) || sleepControl.x!=State[2].x || abs(State[poseRow].w-ClothGap())>1e-7;
   bool grabbed=IniParams[117].x>.5 && State[1].x>=0;
   bool poseChanged=poseMotion>max(IniParams[123].z,.001);
   float now=IniParams[124].x;
   float4 metrics=State[18+slot];bool activity=changed || grabbed || poseChanged;
   metrics=float4(poseMotion,clothMotion,activity?0:metrics.z+1,activity?now:metrics.w);
   State[18+slot]=metrics;
   sleep.y=changed || grabbed || poseChanged || clothMotion>(IntrinsicSurface()?.0002:.002)?0:min(sleep.y+1,1000);
   bool settled=sleep.y>=3 || (!IntrinsicSurface() && (metrics.z>=12 || now-metrics.w>=.18));
   if(ClosedDomain())
   {
    uint qualityCount=PARTICLE_COUNT+(uint)Config[0].z;
    bool safe=rows>=220+qualityCount*2 && State[3][slot-1]>.5;
    // Releasing a safe cloth pose retains it through body-relative advection.
    // Unresolved contact or stretch cannot be hidden by a quiet-frame timer.
    settled=safe;
   }
   bool recheck=now>=sleepControl.y && poseMotion>.0002;
   bool full=!supported || !active || cold!=0 || changed || grabbed || poseChanged || !settled || recheck;
   bool finish=AdaptiveCloth() && State[ClothWorkRow()].z>.5 && !changed && !grabbed;
   bool transport=following && supported && active && cold==0 && settled && !finish && (poseMotion>.0002 || recheck);
   if(finish)full=true;
   sleep.x=transport?2:(full?0:1);
   float4 work=float4(max(Config[3].y,1),8,0,0);
   if(AdaptiveCloth() && cold==0 && (changed || grabbed))
   {
    float movement=max(abs(State[0].x-initialized.x),TwoEnded()?abs(State[0].y-State[11][slot-1]):0)*Config[6].w*scale;
    if(movement<=.004)work=float4(1,2,1,movement);
    else if(movement<=.015)work=float4(2,4,1,movement);
   }
   State[ClothWorkRow()]=work;
   if(full)
   {
    sleep.zw=State[0].xy;sleepControl.x=State[2].x;sleepControl.z+=active?1:0;
    float interval=max(IniParams[123].w,.033);
    float offset=frac(IniParams[124].z*.618+(slot-1)*.2)*interval;
    sleepControl.y=(floor((now-offset)/interval)+1)*interval+offset;
    [loop]for(uint k=0;k<samples;++k)State[poseRow+k]=float4(PoseCoordinates(PoseSample(k,samples,order),FootAndHeight,UpAxis.xyz,SideAxis.xyz,ForwardAxis.xyz,scale),k==0?ClothGap():0);
   }
   else sleepControl.w+=1;
   [loop]for(uint k=0;k<32;++k)
   {
    uint node=k*PARTICLE_COUNT/32;
    State[poseRow+64+k]=float4(PoseCoordinates(Particles[node].current.xyz,State[4],State[5].xyz,State[6].xyz,State[7].xyz,oldScale),0);
   }
   State[sleepRow]=sleep;State[sleepRow+1]=sleepControl;
   State[4]=FootAndHeight;State[5]=UpAxis;State[6]=SideAxis;State[7]=ForwardAxis;
   bool solveChanged=abs(State[0].x-initialized.x)>1e-6 || (TwoEnded() && abs(State[0].y-State[11][slot-1])>1e-6);
   State[8]=float4(cold!=0?0:State[16+slot].x,State[0].x,scale,clamp(IniParams[117].z,1.0/240,1.0/30));
   bool activeNow=GarmentAmount(State[0].xy)>1e-5 || previousAmount>1e-5;
   State[9]=float4(cold,(IniParams[127].y>.5 || (activeNow && (cold!=0 || solveChanged)))?1:0,0,0);
   State[10]=TwoEnded()?float4(cold!=0?0:State[11][slot-1],State[0].y,0,0):0;
  }
  return;
 }
 if(phase==1)
 {
  if(i>=PARTICLE_COUNT)return;Particle p=Particles[i];
  bool sleeping=State[ClothSleepRow()].x>.5;
  if(SheetCollision() && AttachmentRoles[i]==3)
  {
   p.current=float4(ReferenceAttachment(i,State[8].z),1);p.previous=float4(p.current.xyz,State[2].x);Particles[i]=p;return;
  }
  if(State[9].x>.5){p.current=float4(Source[Representatives[i]].position,1);p.previous=float4(p.current.xyz,State[2].x);uint seeds;BodySeeds.GetDimensions(seeds);Contacts[i*3+2]=float4(asfloat(Config[15].w>.5 && i<seeds?BodySeeds[i]:0),0,0,0);}
  else if(sleeping && ManualSurface() && Rest[i*2].w<.5 && !SheetCollision())
  {p.current=float4(Source[Representatives[i]].position,1);p.previous=float4(p.current.xyz,State[2].x);}
  else if(Contacts[i*3+2].w<-.5)
  {
   float3 anchor=Source[Representatives[i]].position;float4 a,b,c;Rows(Weights[i],a,b,c);
   float4 pa=PreviousRows[i*3],pb=PreviousRows[i*3+1],pc=PreviousRows[i*3+2];
   bool unchanged=sleeping && all(a==pa) && all(b==pb) && all(c==pc) && all(anchor==Contacts[i*3].xyz);
   if(!unchanged)
   {
    p.current.xyz=anchor+Rotate(a,b,c,Inverse(pa,pb,pc,p.current.xyz-Contacts[i*3].xyz));
    p.previous.xyz=anchor+Rotate(a,b,c,Inverse(pa,pb,pc,p.previous.xyz-Contacts[i*3].xyz));
   }
   if(sleeping)
   {
    p.previous.xyz=p.current.xyz;Contacts[i*3]=float4(anchor,0);
    PreviousRows[i*3]=a;PreviousRows[i*3+1]=b;PreviousRows[i*3+2]=c;
   }
  }
  else if(Contacts[i*3+2].w>.5)
  {
   uint tri=asuint(Contacts[i*3+2].x);float3 bary=float3(Contacts[i*3+1].w,Contacts[i*3+2].yz);
   float3 anchor=Source[BodyVertices[tri*3]].position*bary.x+Source[BodyVertices[tri*3+1]].position*bary.y+Source[BodyVertices[tri*3+2]].position*bary.z;
   float4 a,b,c;Rows(Weights[i],a,b,c);float4 pa=PreviousRows[i*3],pb=PreviousRows[i*3+1],pc=PreviousRows[i*3+2];
   bool unchanged=sleeping && all(a==pa) && all(b==pb) && all(c==pc) && all(anchor==Contacts[i*3].xyz);
   if(!unchanged)
   {
    p.current.xyz=anchor+Rotate(a,b,c,Inverse(pa,pb,pc,p.current.xyz-Contacts[i*3].xyz));
    p.previous.xyz=anchor+Rotate(a,b,c,Inverse(pa,pb,pc,p.previous.xyz-Contacts[i*3].xyz));
   }
   if(sleeping)
   {
    float3 baryNormal=Source[BodyVertices[tri*3]].normal*bary.x+Source[BodyVertices[tri*3+1]].normal*bary.y+Source[BodyVertices[tri*3+2]].normal*bary.z;
    float3 normal=SafeUnit(baryNormal,Contacts[i*3+1].xyz);float gap=ClothGap()*State[8].z;
    // A valid retained pose follows its affine contact transport. Projecting
    // it onto an old tangent plane can stretch folds before validation.
    float depth=gap-dot(p.current.xyz-anchor,normal);if(depth>0 && !AdaptiveCloth())p.current.xyz+=normal*depth;
    p.previous.xyz=p.current.xyz;float4 contact=Contacts[i*3];contact.xyz=anchor;Contacts[i*3]=contact;
    float4 plane=Contacts[i*3+1];plane.xyz=normal;Contacts[i*3+1]=plane;
    PreviousRows[i*3]=a;PreviousRows[i*3+1]=b;PreviousRows[i*3+2]=c;
   }
  }
  Particles[i]=p;return;
 }
 if(phase==4)
 {
  if(i>=PARTICLE_COUNT || Mass(i)<.5 || AttachmentRoles[i]!=0)return;Particle p=Particles[i];float3 sweepStart=p.current.xyz,q,b,n;uint tri;float sd;
  float4 folds=ClothFolds();float materialHeight=Rest[i*2+1].z;
  float4 frame=State[8],foot=State[4];float3 up=State[5].xyz;
  float fraction=IniParams[119].z>.5?(IniParams[119].x+1)/ClothSubsteps(State[ClothWorkRow()]):1;
  float2 progress=float2(lerp(frame.x,frame.y,fraction),lerp(State[10].x,State[10].y,fraction));
  float alpha=GarmentAmount(progress);
  float hemHeight=GarmentInterval(progress).y;
  float2 gather=MaterialProfile(materialHeight,progress);
  if(ManualAnchored() && Config[14].x>.5)
  {
   float influence=saturate(Rest[i*2+1].y/max(materialHeight,.05));
   gather=lerp(float2(materialHeight,1),gather,influence);
  }
  if(Config[10].y>.5 && !IntrinsicSurface())
  {
   if(folds.w>.5)
   {
    float nativeHeight=dot(Source[Representatives[i]].position-foot.xyz,up)/max(foot.w,.001);
    float target=(nativeHeight+gather.x-materialHeight)*foot.w;
    float error=target-dot(p.current.xyz-foot.xyz,up);
    float strength=folds.z*saturate(alpha*30);
    float3 delta=up*error*strength;p.current.xyz+=delta;p.previous.xyz+=delta;
   }
   else
   {
    float ceiling=foot.w*(hemHeight+Config[10].z);
    float violation=dot(p.current.xyz-foot.xyz,up)-ceiling;
    if(violation>0){float3 delta=-up*violation;p.current.xyz+=delta;p.previous.xyz+=delta;}
   }
  }
  uint group=NodeRegion(i);uint seed=asuint(Contacts[i*3+2].x);
  bool tracked=false;
  if(Config[15].z>.5)tracked=TrackBodyLevel(p.current.xyz,seed,group,gather.x,BodyDirections[i].xyz,q,b,n,tri,sd);
  if(!tracked)SurfacePoint(p.current.xyz,seed,group,q,b,n,tri,sd);
  float scale=State[8].z,base=ClothGap()*scale;
  if(group==1 || group==2 || Sliding() || SheetCollision())
  {
   float bodyHeight=dot(q-State[4].xyz,State[5].xyz)/max(State[4].w,.001);
   float compression=saturate((Rest[i*2+1].z-bodyHeight)*2);
   float wrinkle=.5+.5*sin(Rest[i*2+1].z*113+Rest[i*2+1].w/max(Config[6].w,.001)*20);
   float offset=base+Config[11].y*scale*compression*wrinkle;
   if(folds.w>.5)
   {
    float3 materialNormal=BindVertices[Representatives[i]].normal;
    float angle=atan2(dot(materialNormal,Config[9].xyz),dot(materialNormal,Config[7].xyz));
    float phase=6.28318530718*(materialHeight/folds.y+.10*sin(angle*2)+.035*sin(angle*5));
    float envelope=smoothstep(.07,.16,materialHeight)*(1-smoothstep(.96,1.01,materialHeight));
    float compressionAmount=sqrt(saturate(1-gather.y*gather.y));
    float seamFade=SheetCollision()?Rest[i*2+1].x:smoothstep(.004,.025,abs(Rest[i*2+1].w)/max(Config[6].w,.001));
    float amplitude=folds.x*scale*compressionAmount*envelope*seamFade*saturate(alpha*20);
    offset=base+amplitude*(.5+.5*cos(phase));
   }
   // Limit the extrusion before it enters a nearby arm or another body sheet.
   // Cloth self-separation is independent of this procedural fold target.
   offset=BodyLimitedOffset(q,n,offset,scale,0);
   float3 target=q+n*offset;
   if(Sliding())
   {
    float3 guide=Source[Representatives[i]].position+up*(gather.x-materialHeight)*foot.w;
    float adhesion=smoothstep(.10,.25,abs(Rest[i*2+1].w)/max(Config[6].w,1e-6));
    if(adhesion<.999)
    {
     float f=clamp(BodyDirections[i].w/.20,-1,1),inward=sqrt(max(1-f*f,0));
     float3 leftDir=SafeUnit(Config[7].xyz*inward+Config[9].xyz*f,Config[7].xyz),rightDir=SafeUnit(-Config[7].xyz*inward+Config[9].xyz*f,-Config[7].xyz);
     float3 lq,lb,ln,rq,rb,rn;uint lt,rt;float ld,rd;
     bool leftHit=TrackBodyLevel(p.current.xyz,seed,1,gather.x,leftDir,lq,lb,ln,lt,ld);
     bool rightHit=TrackBodyLevel(p.current.xyz,seed,2,gather.x,rightDir,rq,rb,rn,rt,rd);
     if(leftHit && rightHit)
     {
      float span=Config[15].x>1e-5?Config[15].x:Config[6].w*.45;
      float blend=saturate(.5+Rest[i*2+1].w/max(2*span,1e-6));
      // Both sides use the same two samples. Using the current owner's
      // different radial for one endpoint creates a seam at the centreline.
      guide=lerp(lq+ln*base,rq+rn*base,blend);
     }
     else{float penetration=base-dot(guide-q,n);if(penetration>0)guide+=n*penetration;}
    }
    target=lerp(guide,target,adhesion);
   }
   float3 delta=target-p.current.xyz;p.current.xyz+=delta;p.previous.xyz+=delta;
  }
  else
  {
   float desired=folds.w>.5 && folds.x<1e-6?base:clamp(sd,base,Config[11].z*scale);
   desired=BodyLimitedOffset(q,n,desired,scale,0);float3 delta=q+n*desired-p.current.xyz;
   p.current.xyz+=delta;p.previous.xyz+=delta;
  }
  if(Config[15].y>.5 && Rest[i*2+1].x<.9999 && !SheetCollision())
  {float3 target=lerp(Source[Representatives[i]].position,p.current.xyz,Rest[i*2+1].x);float3 delta=target-p.current.xyz;p.current.xyz=target;p.previous.xyz+=delta;}
  Contacts[i*3]=float4(q,sd);Contacts[i*3+1]=float4(n,b.x);Contacts[i*3+2]=float4(asfloat(tri),b.y,b.z,1);
  if(SheetCollision())
  {
   float3 guided=lerp(sweepStart,p.current.xyz,saturate(Config[10].w));p.previous.xyz+=guided-p.current.xyz;p.current.xyz=guided;
   float3 limited=KeepOutside(sweepStart,p.current.xyz,ClothGap()*State[8].z);p.previous.xyz+=limited-p.current.xyz;p.current.xyz=limited;
  }
  Particles[i]=p;return;
 }
 if(phase==5)
 {
  if(i>=(uint)IniParams[118].z)return;
  uint face=FaceColors[(uint)IniParams[118].y+i];if(SheetCollision())FaceScoped(face);else FaceGlobal(face);return;
 }
 if(phase==9)
 {
  if(Config[15].z>.5)return;
  if(i>=PARTICLE_COUNT || Mass(i)<.5 || Contacts[i*3+2].w<.5)return;
  Particle value=Particles[i];float3 anchor=Contacts[i*3].xyz,n=Contacts[i*3+1].xyz;
  float gap=ClothGap()*State[8].z,depth=gap-dot(value.current.xyz-anchor,n);
  if(depth>0 && distance(value.current.xyz,anchor)<.08*State[8].z)
  {float3 delta=n*depth;value.current.xyz+=delta;value.previous.xyz+=delta;Particles[i]=value;}
  return;
 }
 if(phase==10)
 {
  if(SheetCollision())
  {
   if(i>=PARTICLE_COUNT || AttachmentRoles[i]==3)return;Particle value=Particles[i];float3 start=value.current.xyz;
   float3 target;
   bool corrected=BodyContactTarget(value.current.xyz,ContactSeed(i),ClothGap()*State[8].z,.08*State[8].z,target);
   if(corrected)value.current.xyz=target;
   if(Sliding() && ClosedDomain() && corrected && DomainInside(value.current.xyz))
   {
    float4 a,b,c;Rows(Weights[i],a,b,c);float3 direction=Rotate(a,b,c,BindVertices[Representatives[i]].normal);
    float centre=1-smoothstep(.04,.12,abs(Rest[i*2+1].w)/max(Config[6].w,1e-6));
    float sign=dot(Rest[i*2].xyz-Config[8].xyz,Config[9].xyz)<0?-1:1;
    direction=SafeUnit(lerp(direction,State[7].xyz*sign,centre),State[7].xyz*sign);
    if(DomainExitAlong(start,direction,ClothGap()*State[8].z,.12*State[8].z,target))value.current.xyz=target;
   }
   value.previous.xyz+=value.current.xyz-start;Particles[i]=value;return;
  }
  if(i>=PARTICLE_COUNT || (Config[15].w>.5 && Rest[i*2].w<.5))return;
  Particle value=Particles[i];float3 q,b,n;uint tri;float sd;
  ClosestBody(value.current.xyz,ContactSeed(i),q,b,n,tri,sd);
  float gap=ClothGap()*State[8].z;
  if(sd<gap && all(isfinite(q)) && distance(value.current.xyz,q)<.08*State[8].z)
  {float3 delta=q+n*gap-value.current.xyz;value.current.xyz+=delta;value.previous.xyz+=delta;Particles[i]=value;}
  return;
 }
 if(phase==11)
 {
  float sleep=State[ClothSleepRow()].x;bool transport=IniParams[125].y>.5;
  if(!ClosedDomain() || idle || (transport?(!AdaptiveCloth() || sleep<1.5):sleep>.5))return;
  uint totalFaces=(uint)Config[0].z,qualityCount=PARTICLE_COUNT+totalFaces;
  if(i>=qualityCount)return;uint rows;State.GetDimensions(rows);uint slot=(uint)clamp(round(IniParams[118].w),1,2);
  uint at=220+(slot-1)*qualityCount+i;if(at>=rows)return;float4 value=float4(0,0,0,1);
  if(i<PARTICLE_COUNT)
  {
   if(AttachmentRoles[i]!=3)value.x=DomainInside(Particles[i].current.xyz)?1:0;
  }
  else
  {
   uint face=i-PARTICLE_COUNT;uint3 ids=uint3(Faces[face*3],Faces[face*3+1],Faces[face*3+2]);
   if(AttachmentRoles[ids.x]!=3 || AttachmentRoles[ids.y]!=3 || AttachmentRoles[ids.z]!=3)value.x=FaceBodyIntersection(ids)?1:0;
   float maximum=0;
   [unroll]for(uint edge=0;edge<3;++edge)
   {
    uint a=ids[edge],b=ids[(edge+1)%3];float before=distance(Source[Representatives[a]].position,Source[Representatives[b]].position);
    before=distance(Rest[a*2].xyz,Rest[b*2].xyz)*State[8].z;
    maximum=max(maximum,distance(Particles[a].current.xyz,Particles[b].current.xyz)/max(before,1e-5));
   }
   value.y=maximum;value.z=maximum>3?1:0;
  }
  State[at]=value;return;
 }
#if !defined(STATIC_PHASE) || STATIC_PHASE == 12
 if(phase==12)
 {
  if(!ClosedDomain() || idle || i!=0 || State[ClothSleepRow()].x>.5)return;
  if(IniParams[119].w>.5 && State[3][(uint)clamp(round(IniParams[118].w),1,2)-1]>.5)return;
  uint slot=(uint)clamp(round(IniParams[118].w),1,2),qualityCount=PARTICLE_COUNT+(uint)Config[0].z;
  uint start=220+(slot-1)*qualityCount;bool safe=true;
  [loop]for(uint check=0;check<qualityCount;++check)
  {float4 value=State[start+check];if(value.w<.5 || value.x>.5 || value.z>.5){safe=false;break;}}
  float4 result=State[3];if(slot==1)result.x=safe?1:0;else result.y=safe?1:0;State[3]=result;return;
 }
#endif
 if(phase==13)
 {
  if(i==0 && AdaptiveCloth() && State[ClothSleepRow()].x>1.5)
  {
   uint slot=(uint)clamp(round(IniParams[118].w),1,2);float4 sleep=State[ClothSleepRow()];
   sleep.x=State[3][slot-1]>.5?1:0;State[ClothSleepRow()]=sleep;
   if(sleep.x<.5)State[ClothWorkRow()]=float4(max(Config[3].y,1),8,0,0);
  }
  return;
 }
 if(phase==6)
 {
  if(State[23].w<.5)return;
  if(i>=PARTICLE_COUNT)return;float3 p=Particles[i].current.xyz,q,b,n;uint tri;float sd;
  if(!idle && ((AttachmentRoles[i]==1 && Rest[i*2].w>.5) || (SheetCollision() && (AttachmentRoles[i]!=0 || Rest[i*2].w<.5))))
  {
   Blend original;original.weights=OriginalBlend[Representatives[i]*2];original.indices=asuint(OriginalBlend[Representatives[i]*2+1]);Weights[i]=original;
   float4 a0,b0,c0;Rows(original,a0,b0,c0);PreviousRows[i*3]=a0;PreviousRows[i*3+1]=b0;PreviousRows[i*3+2]=c0;
   Contacts[i*3]=float4(Source[Representatives[i]].position,0);Contacts[i*3+1]=0;Contacts[i*3+2]=float4(asfloat(BodySeeds[i]),0,0,-1);
  }
  else if(!idle && !(ManualSurface() && Rest[i*2].w<.5))
  {
  if(State[ClothSleepRow()].x>.5)
  {
   float4 followA,followB,followC;Rows(Weights[i],followA,followB,followC);PreviousRows[i*3]=followA;PreviousRows[i*3+1]=followB;PreviousRows[i*3+2]=followC;
  }
  else
  {
  uint seed=asuint(Contacts[i*3+2].x),count;BodyOrder.GetDimensions(count);
  bool locked=Config[15].z>.5 && (HemMap[i]>=HEM_COUNT || IntrinsicSurface()) && Contacts[i*3+2].w>.5 && seed<count;
  if(IntrinsicSurface())SurfacePoint(p,seed,NodeRegion(i),q,b,n,tri,sd);
  else if(locked)
  {
   if(SheetCollision())SurfacePoint(p,seed,NodeRegion(i),q,b,n,tri,sd);
   else
   {
   // A blocker can push fabric away from its adhesion sheet. Keep the
   // material contact selected by the slide solve, rather than attaching
   // it to the closest surface after the collision correction.
   tri=seed;b=float3(Contacts[i*3+1].w,Contacts[i*3+2].yz);
   uint3 ids=uint3(BodyVertices[tri*3],BodyVertices[tri*3+1],BodyVertices[tri*3+2]);
   q=Source[ids.x].position*b.x+Source[ids.y].position*b.y+Source[ids.z].position*b.z;
   n=SafeUnit(Source[ids.x].normal*b.x+Source[ids.y].normal*b.y+Source[ids.z].normal*b.z,Contacts[i*3+1].xyz);sd=dot(p-q,n);
   }
  }
  else
  {
   bool routed=false;
   if(Config[15].z>.5 && HemMap[i]<HEM_COUNT)
   {
    float2 interval=GarmentInterval(State[0].xy);float level=TwoEnded() && HemMap[i]>=(uint)Config[13].x?interval.x:interval.y;
    routed=TrackBodyLevel(p,seed,NodeRegion(i),level,BodyDirections[i].xyz,q,b,n,tri,sd);
   }
   if(!routed)SurfacePoint(p,seed,NodeRegion(i),q,b,n,tri,sd);
  }
  Contacts[i*3]=float4(q,sd);Contacts[i*3+1]=float4(n,b.x);Contacts[i*3+2]=float4(asfloat(tri),b.y,b.z,1);
  Blend weights=Transfer(tri,b);Weights[i]=weights;float4 a0,b0,c0;Rows(weights,a0,b0,c0);
  PreviousRows[i*3]=a0;PreviousRows[i*3+1]=b0;PreviousRows[i*3+2]=c0;
  }
  }
  else if(!idle)
  {
   Blend original;original.weights=OriginalBlend[Representatives[i]*2];original.indices=asuint(OriginalBlend[Representatives[i]*2+1]);
   Weights[i]=original;float4 a0,b0,c0;Rows(original,a0,b0,c0);
   PreviousRows[i*3]=a0;PreviousRows[i*3+1]=b0;PreviousRows[i*3+2]=c0;Contacts[i*3+2]=0;
  }
  if(i==0)
  {
   uint slot=(uint)clamp(round(IniParams[118].w),1,2);
   State[16+slot]=float4(State[8].y,State[8].z,1,State[2].x);
   if(TwoEnded())
   {
    float4 history=State[11];
    if(slot==1)history.x=State[10].y;else history.y=State[10].y;
    State[11]=history;
   }
  }
  return;
 }
 if(phase==7)
 {
  if(i>=PARTICLE_COUNT)return;Particle p=Particles[i];float dt=State[8].w/max(Config[3].y,1),scale=State[8].z;
  if(!all(isfinite(p.current.xyz)) || distance(p.current.xyz,Source[Representatives[i]].position)>1.7*scale)p.current=float4(Source[Representatives[i]].position,1);
  float3 velocity=p.current.xyz-p.previous.xyz;float speed=length(velocity),limit=Config[4].y*scale*dt;if(speed>limit)velocity*=limit/speed;
  p.previous=float4(p.current.xyz-velocity,State[2].x);Particles[i]=p;return;
 }
}
#endif
