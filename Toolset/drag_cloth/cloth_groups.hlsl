#include "constants.hlsli"
#include "common.hlsli"
Buffer<float4> Rest : register(t75);
Buffer<uint> Representatives : register(t85);
Buffer<uint> WearMap : register(t94);
Buffer<uint> Faces : register(t79);
Buffer<uint> FaceColors : register(t100);
Buffer<float4> Constraints : register(t113);
Buffer<uint> EdgeOffsets : register(t114);
Buffer<uint> FaceOffsets : register(t115);
Buffer<uint> HemMap : register(t116);
Buffer<float4> HemParams : register(t117);
Buffer<float4> HemLines : register(t107);
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

float3 P(uint i){return float3(Position[i*3],Position[i*3+1],Position[i*3+2]);}
void SetP(uint i,float3 p){Position[i*3]=p.x;Position[i*3+1]=p.y;Position[i*3+2]=p.z;}
float3 Hem(uint i){return float3(HemTargets[i*3],HemTargets[i*3+1],HemTargets[i*3+2]);}
void SetHem(uint i,float3 p){HemTargets[i*3]=p.x;HemTargets[i*3+1]=p.y;HemTargets[i*3+2]=p.z;}
float Mass(uint i){return HemMap[i]<HEM_COUNT || Rest[i*2].w<.5 ? 0:1;}
uint NodeRegion(uint i)
{
    if(HemMap[i]<HEM_COUNT)return 0;
    if(Config[12].w>.5)return (uint)Config[12].w;
    float side=Rest[i*2+1].w/max(Config[6].w,1e-6);
    return abs(side)<.004?3:(side<0?1:2);
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
    float3 feet=0,top=0,left=0,right=0,front=0,back=0;float nf=0,nt=0,nl=0,nr=0,na=0,nb=0;
    [loop]for(uint i=0;i<PARTICLE_COUNT;++i)
    {
        float3 p=Source[Representatives[i]].position;float4 meta=Rest[i*2+1];
        if(Rest[i*2].w<.5){feet+=p;nf+=1;}
        if(HemMap[i]<HEM_COUNT){top+=p;nt+=1;}
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
 Planes[direction]=float4(n,max(h,.005)+Config[4].w);
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
        // Compression can gather fabric, while bending retains some support.
        if(e.w<.5 || material.w<=0 || len>=rest*material.w){Lambdas[index]=0;return;}
        rest*=material.w;
    }
    float lambda=(-(len-rest)-alpha*old)/(mass+alpha);Lambdas[index]=old+lambda;
    float3 correction=d*(lambda/len);
    if(e.w<.5 && len>rest*Config[5].w)correction-=d*((len-rest*Config[5].w)/len)/mass;
    float maximum=.004*Parameters.z,c=length(correction);if(c>maximum)correction*=maximum/c;
    SetP(a,pa+correction*wa);SetP(b,pb-correction*wb);
}
bool Hit(float3 p,float3 end,float3 a,float3 b,float3 c)
{
    float3 d=end-p,ab=b-a,ac=c-a,h=cross(d,ac);float den=dot(ab,h);if(abs(den)<1e-13)return false;
    float3 v=p-a,q=cross(v,ab);float u=dot(v,h)/den,w=dot(d,q)/den,t=dot(ac,q)/den;
    return u>=-1e-6 && w>=-1e-6 && u+w<=1.000001 && t>=0 && t<=1;
}
void FaceGlobal(uint face)
{
    uint3 ids=uint3(Faces[face*3],Faces[face*3+1],Faces[face*3+2]);float3 p[3];p[0]=Particles[ids.x].current.xyz;p[1]=Particles[ids.y].current.xyz;p[2]=Particles[ids.z].current.xyz;
    float3 old[3];old[0]=p[0];old[1]=p[1];old[2]=p[2];float3 mass=float3(Mass(ids.x),Mass(ids.y),Mass(ids.z));if(dot(mass,1.0)<.5)return;
    uint g0=NodeRegion(ids.x),g1=NodeRegion(ids.y),g2=NodeRegion(ids.z),group=g0==g1 && g1==g2?g0:3;
    float3 lo=min(p[0],min(p[1],p[2])),hi=max(p[0],max(p[1],p[2]));uint node=1,count,leaves=(uint)IniParams[115].y;BodyOrder.GetDimensions(count);
    [loop]while(node>0)
    {
        float3 nlo=BodyBounds[node*2].xyz,nhi=BodyBounds[node*2+1].xyz;
        if(all(lo<=nhi) && all(hi>=nlo) && all(nlo<=nhi))
        {
            if(node<leaves){node*=2;continue;}
            uint at=node-leaves;
            if(at<count && Allowed(BodyOrder[at],group))
            {
                uint tri=BodyOrder[at];uint3 bi=uint3(BodyVertices[tri*3],BodyVertices[tri*3+1],BodyVertices[tri*3+2]);
                Vertex a=Source[bi.x],b=Source[bi.y],c=Source[bi.z];float3 q[3];q[0]=a.position;q[1]=b.position;q[2]=c.position;
                float3 n=cross(b.position-a.position,c.position-a.position);if(dot(n,a.normal+b.normal+c.normal)<0)n=-n;n=SafeUnit(n,float3(0,1,0));
                [unroll]for(uint edge=0;edge<3;++edge)
                {
                    uint next=(edge+1)%3;
                    if(Hit(p[edge],p[next],q[0],q[1],q[2]))
                    {
                        float x=Config[2].z-dot(p[edge]-a.position,n),y=Config[2].z-dot(p[next]-a.position,n);
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
                        float3 closest,bary;ClosestTriangle(q[v],p[0],p[1],p[2],closest,bary);float depth=dot(q[v]-closest,g)+Config[2].z;
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
    uint slot=(uint)clamp(round(IniParams[118].w),1,2);
    float4 initialized=State[16+slot];
    if(initialized.z<.5 || initialized.w!=State[2].x)
    {
        a.z=0;a.w=cursor;b.x=-1;State[0]=a;State[1]=b;return;
    }
    if(held && a.z<.5)
    {
        b.x=-1;uint n,stride;Detect.GetDimensions(n,stride);
        if(n>7 && Detect[0].x>=0)
        {
            uint nearest=(uint)clamp(round(Detect[3].w),0,2),row=(uint)max(Detect[3][nearest],0);
            if(row<(uint)Config[0].y){uint particle=WearMap[row];if(particle<PARTICLE_COUNT)b.x=Rest[particle*2+1].y;}
        }
    }
    if(held && a.z>.5 && b.x>=0)
    {
        float delta=clamp((a.w-cursor)/screen*1.6,-Config[4].z,Config[4].z);
        if(Config[0].w>.5){a.x=saturate(a.x+delta);a.y=a.x;}
        else if(b.x<.5)a.x=saturate(a.x+delta);else a.y=saturate(a.y+delta);
    }
    if(!held)b.x=-1;a.z=held?1:0;a.w=cursor;State[0]=a;State[1]=b;
}


[numthreads(THREADS,1,1)]void main(uint lane:SV_GroupIndex)
{
 uint phase=(uint)IniParams[116].x;
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
 uint hullCount,hullPoints;HullCount.GetDimensions(hullCount);HullPoints.GetDimensions(hullPoints);
 uint hemLines;HemLines.GetDimensions(hemLines);valid=valid && hullCount==1 && hullPoints>=order*3 && hemLines==HEM_COUNT;
 if(lane==0 && !valid)State[23]=float4(PARTICLE_COUNT,edges,order,0);if(!valid)return;
 if(lane==0)
 {
  Cold=State[23].w>=.5 && (phase!=2 || HullCount[0]>0)?1:0;
  if(Cold==0){float4 health=State[23];health.w=0;State[23]=health;}
 }
 GroupMemoryBarrierWithGroupSync();
 if(Cold==0)return;
 if(phase!=2 && phase!=3)return;
 if(lane==0){Parameters=State[8];FootAndHeight=State[4];UpAxis=State[5];SideAxis=State[6];ForwardAxis=State[7];State[21]=ClothMaterial();State[22]=ClothFolds();}
 GroupMemoryBarrierWithGroupSync();float dt=Parameters.w/max(Config[3].y,1);uint iterations=(uint)Config[3].x;
 if(phase==2)
 {
  float alpha=lerp(Parameters.x,Parameters.y,(IniParams[119].x+1)/max(Config[3].y,1));
  if(lane==0)HemCenter=float4(FootAndHeight.xyz+UpAxis.xyz*FootAndHeight.w*lerp(1,Config[4].x,alpha),alpha);
  GroupMemoryBarrierWithGroupSync();
  if(lane<HEM_COUNT)
  {
   float4 h=HemLines[lane];float3 n0=SideAxis.xyz*h.x+ForwardAxis.xyz*h.y,n1=SideAxis.xyz*h.z+ForwardAxis.xyz*h.w;
   float a=-1e20,b=-1e20;uint count=HullCount[0];
   [loop]for(uint hullIndex=0;hullIndex<count;++hullIndex){float3 p=HullPoints[hullIndex].xyz;a=max(a,dot(p,n0));b=max(b,dot(p,n1));}
   a=max(a,.005)+Config[4].w;b=max(b,.005)+Config[4].w;
   float det=h.x*h.w-h.y*h.z;float x=(a*h.w-h.y*b)/det,y=(h.x*b-a*h.z)/det;
   SetHem(lane,HemCenter.xyz+SideAxis.xyz*x+ForwardAxis.xyz*y);
  }
  GroupMemoryBarrierWithGroupSync();
 }
 [loop]for(uint j=lane;j<PARTICLE_COUNT;j+=THREADS)
 {
  Particle p=Particles[j];float3 next=p.current.xyz;
  if(phase==2)
  {
   float3 velocity=(next-p.previous.xyz)*ClothMaterial().z;float speed=length(velocity),limit=Config[4].y*Parameters.z*dt;if(speed>limit)velocity*=limit/speed;
   p.previous.xyz=next;next+=velocity+UpAxis.xyz*Config[2].x*Parameters.z*dt*dt;
   uint hem=HemMap[j];if(hem<HEM_COUNT){next=Hem(hem);p.previous.xyz=next;}
   else if(Rest[j*2].w<.5)
   {
    next=Source[Representatives[j]].position;float3 q,b,n;uint tri;float sd;
    ClosestBodyGroup(next,asuint(Contacts[j*3+2].x),NodeRegion(j),q,b,n,tri,sd);
    if(sd<Config[2].z*Parameters.z)next+=n*(Config[2].z*Parameters.z-sd);
    p.previous.xyz=next;
   }
   Particles[j]=p;
  }
  SetP(j,next);
 }
 AllMemoryBarrierWithGroupSync();
 [loop]for(uint iteration=0;iteration<iterations;++iteration)
 {
  [loop]for(uint color=0;color<EDGE_COLORS;++color)
  {
   uint begin=EdgeOffsets[color],end=EdgeOffsets[color+1];
   [loop]for(uint k=begin+lane;k<end;k+=THREADS)Edge(k,dt,iteration+(phase==3?iterations:0));
   GroupMemoryBarrierWithGroupSync();
  }
 }
 [loop]for(uint outNode=lane;outNode<PARTICLE_COUNT;outNode+=THREADS){Particle p=Particles[outNode];p.current=float4(P(outNode),1);Particles[outNode]=p;}
}
