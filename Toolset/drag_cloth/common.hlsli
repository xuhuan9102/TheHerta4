struct Vertex { float3 position; float3 normal; float4 tangent; };
struct Particle { float4 current; float4 previous; };
struct Blend { float4 weights; uint4 indices; };
StructuredBuffer<Vertex> Source : register(t24);
Buffer<uint> BodyVertices : register(t87);
Buffer<uint> BodyOrder : register(t89);
Buffer<float4> BodyBounds : register(t91);
Buffer<float4> Config : register(t82);
Buffer<float4> ControlZones : register(t110);
Texture1D<float4> IniParams : register(t120);
float ClothGap()
{
 float value=IniParams[123].x;
 return IniParams[124].y>.5 && isfinite(value) && value>=0?clamp(value,0,.03):Config[2].z;
}
uint ClothSleepRow(){return 24+((uint)clamp(round(IniParams[118].w),1,2)-1)*2;}
uint ClothPoseRow(){return 28+((uint)clamp(round(IniParams[118].w),1,2)-1)*96;}
uint ClothWorkRow(){return 12+(uint)clamp(round(IniParams[118].w),1,2)-1;}

float4 ClothMaterial()
{
    float4 value=IniParams[121];
    if(!all(isfinite(value)) || value.x<=0 || value.y<=0)
        return float4(Config[3].z,Config[3].w,Config[2].y,0);
    return float4(clamp(value.xy,0.000001,1),clamp(value.z,0,0.999),clamp(value.w,0,0.8));
}

float4 ClothFolds()
{
    float4 value=IniParams[122];
    if(!all(isfinite(value)) || value.w<.5)return 0;
    return float4(clamp(value.x,0,.025),clamp(value.y,.04,.2),clamp(value.z,0,1),1);
}

// Monotone compression preserves material order instead of clamping every
// point above the cuff to one plane. The derivative drives fold amplitude.
float2 GatherAnchored(float materialHeight,float hemHeight,float anchor,float minimumRatio)
{
    if(materialHeight<=anchor)return float2(materialHeight,1);
    float t=saturate((materialHeight-anchor)/(1-anchor));
    float ratio=clamp((hemHeight-anchor)/(1-anchor),minimumRatio,1);
    float power=min(2,1+.6*ratio/max(1-ratio,.0001));
    float slope=1-(1-ratio)*power*pow(max(t,.0001),power-1);
    float height=anchor+(1-anchor)*(t-(1-ratio)*pow(t,power));
    height+=max(materialHeight-1,0)*max(slope,.05);
    return float2(height,saturate(slope));
}

float2 GatherProfile(float materialHeight,float hemHeight)
{
    return GatherAnchored(materialHeight,hemHeight,.07,.08);
}

bool TwoEnded(){return Config[13].z>.5;}
bool Branched(){return Config[14].w>.5 && Config[14].w<1.5;}
bool Sliding(){return Config[14].w>1.5 && Config[14].w<2.5;}
bool ManualAnchored(){return Config[14].w>2.5;}
bool ManualSurface(){return Config[15].w>.5 && !Sliding();}
float GarmentAmount(float2 progress){return TwoEnded()?max(progress.x,progress.y):progress.x;}

float2 GarmentInterval(float2 progress)
{
    if(!TwoEnded())return float2(0,lerp(1,Config[4].x,progress.x));
    float travel=1-Config[13].w;
    return float2(progress.y*travel,1-progress.x*travel);
}

float2 MaterialProfile(float materialHeight,float2 progress)
{
    float2 interval=GarmentInterval(progress);
    if(ManualAnchored())
    {
        float anchor=Config[14].z;
        if(Config[14].y>.5 && interval.y<anchor)
        {
            float2 folded=GatherAnchored(materialHeight,2*anchor-interval.y,anchor,.00001);
            return float2(materialHeight<=anchor?materialHeight:2*anchor-folded.x,folded.y);
        }
        return GatherAnchored(materialHeight,interval.y,anchor,.00001);
    }
    if(Sliding())return float2(materialHeight+interval.y-1,1);
    if(Branched())
    {
        float pin=Config[14].z;
        if(materialHeight>=pin)return float2(materialHeight,1);
        float span=max((pin-interval.x)/pin,.00001);
        float2 gathered=GatherAnchored(1-materialHeight/pin,span,min(.07,span*.2),.00001);
        return float2(pin*(1-gathered.x),gathered.y);
    }
    if(!TwoEnded())return GatherProfile(materialHeight,interval.y);
    float span=max(interval.y-interval.x,Config[13].w);
    float anchor=min(.07,span*.2);
    float2 down=GatherAnchored(materialHeight,span,anchor,.00001);
    float2 up=GatherAnchored(1-materialHeight,span,anchor,.00001);
    float bias=progress.y/max(progress.x+progress.y,.00001);
    return float2(interval.x+lerp(down.x,span-up.x,bias),lerp(down.y,up.y,bias));
}

Buffer<uint> BodyOwners : register(t109);
Buffer<uint> BodyAdjacency : register(t105);
Buffer<uint> SurfaceMask : register(t108);
Buffer<uint> CollisionMask : register(t127);
Buffer<uint> BodyExterior : register(t106);
StructuredBuffer<Vertex> BindVertices : register(t111);
Buffer<uint> BodyContacts : register(t112);
Buffer<float4> BodyChannels : register(t124);
Buffer<float> BodyLevels : register(t104);
Buffer<float4> LevelConfig : register(t98);
Buffer<uint> LevelCandidates : register(t103);
Buffer<uint> ReferenceRows : register(t102);
Buffer<float4> ReferenceBinding : register(t101);
float3 ReferenceAttachment(uint particle,float scale)
{
 uint3 ids=uint3(ReferenceRows[particle*3],ReferenceRows[particle*3+1],ReferenceRows[particle*3+2]);
 float4 binding=ReferenceBinding[particle];
 float3 q=Source[ids.x].position*binding.x+Source[ids.y].position*binding.y+Source[ids.z].position*binding.z;
 float3 n=normalize(Source[ids.x].normal*binding.x+Source[ids.y].normal*binding.y+Source[ids.z].normal*binding.z);
 return q+n*max(binding.w,ClothGap())*scale;
}
Buffer<uint> BodyOwnerBounds : register(t125);
bool Exterior(uint tri){uint n;BodyExterior.GetDimensions(n);return n<=tri || BodyExterior[tri]!=0;}

uint BodyRegion(uint tri){uint count;BodyOwners.GetDimensions(count);return tri<count?BodyOwners[tri]:0;}
bool SheetCollision()
{
#ifdef CLOTH_ANATOMICAL_SCOPE
 return CLOTH_ANATOMICAL_SCOPE!=0;
#else
 uint count;Config.GetDimensions(count);return count>16 && Config[16].x>.5;
#endif
}
bool ClosedDomain()
{
#ifdef CLOTH_CLOSED_DOMAIN
 return CLOTH_CLOSED_DOMAIN!=0;
#else
 return SheetCollision() && Config[16].w>.5;
#endif
}
bool AdaptiveCloth(){return ClosedDomain() && IniParams[125].x>.5;}
float ClothSubsteps(float4 work){return AdaptiveCloth()?clamp(round(work.x),1,max(Config[3].y,1)):max(Config[3].y,1);}
bool IntrinsicSurface()
{
#ifdef CLOTH_REFERENCE_FLOW
 return CLOTH_REFERENCE_FLOW!=0;
#else
 return SheetCollision() && Config[16].w>1.5;
#endif
}
float ReferenceBodyLevel(uint row)
{
 uint count;BodyLevels.GetDimensions(count);
 return IntrinsicSurface() && row<count?BodyLevels[row]:dot(BindVertices[row].position-Config[8].xyz,Config[6].xyz)/max(Config[6].w,1e-6);
}
bool CollisionAllowed(uint tri){uint count;CollisionMask.GetDimensions(count);return count==0 || (tri<count && CollisionMask[tri]!=0);}
bool Allowed(uint tri,uint group)
{
 if(group==0 && SheetCollision())return CollisionAllowed(tri);
 uint surfaces;SurfaceMask.GetDimensions(surfaces);if(surfaces>tri && SurfaceMask[tri]==0)return false;
 uint n;BodyContacts.GetDimensions(n);bool contact=n>tri && BodyContacts[tri]!=0;
 if(group==0)return Exterior(tri);
 // Occlusion by an arm must not change the garment's adhesion sheet.
 if(group==3)return ManualSurface()?BodyRegion(tri)==3:(Sliding()?BodyRegion(tri)!=0:Exterior(tri) && !contact);
 uint body=BodyRegion(tri);
 return body==group;
}
bool BodyBranchAllowed(uint node,uint leaves,uint group)
{
 uint required=group==3?(ManualSurface()?8:(Sliding()?14:0)):(group>0 && group<32?1u<<group:0);
 uint count;BodyOwnerBounds.GetDimensions(count);
 return required==0 || count!=leaves*2 || (BodyOwnerBounds[node]&required)!=0;
}
float3 SafeUnit(float3 v, float3 fallback)
{
    float q=dot(v,v); return q>1e-16 && all(isfinite(v)) ? v*rsqrt(q) : fallback;
}

void ClosestTriangle(float3 p,float3 a,float3 b,float3 c,out float3 q,out float3 bary)
{
    float3 ab=b-a,ac=c-a,bc=c-b;
    float u=saturate(dot(p-a,ab)/max(dot(ab,ab),1e-20));
    q=a+ab*u;bary=float3(1-u,u,0);float best=dot(p-q,p-q);
    float v=saturate(dot(p-a,ac)/max(dot(ac,ac),1e-20));float3 r=a+ac*v;
    float d=dot(p-r,p-r);if(d<best){q=r;bary=float3(1-v,0,v);best=d;}
    float w=saturate(dot(p-b,bc)/max(dot(bc,bc),1e-20));r=b+bc*w;
    d=dot(p-r,p-r);if(d<best){q=r;bary=float3(0,1-w,w);best=d;}
    float aa=dot(ab,ab),bb=dot(ab,ac),cc=dot(ac,ac),den=aa*cc-bb*bb;
    if(abs(den)>1e-20)
    {
        v=(cc*dot(p-a,ab)-bb*dot(p-a,ac))/den;
        w=(aa*dot(p-a,ac)-bb*dot(p-a,ab))/den;
        if(v>=0 && w>=0 && v+w<=1)
        {
            r=a+ab*v+ac*w;d=dot(p-r,p-r);
            if(d<best){q=r;bary=float3(1-v-w,v,w);}
        }
    }
}

void TestBodyTriangleGroup(uint tri,uint group,float3 p,inout float best,inout float3 q,inout float3 bary,
                      inout float3 normal,inout uint nearestTri)
{
    if(!Allowed(tri,group))return;
    uint3 ids=uint3(BodyVertices[tri*3],BodyVertices[tri*3+1],BodyVertices[tri*3+2]);
    Vertex a=Source[ids.x],b=Source[ids.y],c=Source[ids.z];
    float3 area=cross(b.position-a.position,c.position-a.position);if(dot(area,area)<1e-18)return;
    float3 r,lam;ClosestTriangle(p,a.position,b.position,c.position,r,lam);
    float d=dot(p-r,p-r);
    if(d<best)
    {
        best=d;q=r;bary=lam;nearestTri=tri;
        float3 ref=a.normal*lam.x+b.normal*lam.y+c.normal*lam.z;
        float3 geometric=cross(b.position-a.position,c.position-a.position);
        if(dot(geometric,ref)<0)geometric=-geometric;
        normal=SafeUnit(ref,SafeUnit(geometric,float3(0,1,0)));
    }
}

void ClosestBodyGroup(float3 p,uint seed,uint group,out float3 q,out float3 bary,out float3 normal,out uint nearestTri,out float signedDistance)
{
    uint count;BodyOrder.GetDimensions(count);
    q=p;bary=float3(1,0,0);normal=float3(0,1,0);nearestTri=0;float best=1e30;
    if(seed<count)TestBodyTriangleGroup(seed,group,p,best,q,bary,normal,nearestTri);
    uint leaves=(uint)IniParams[115].y;uint node=1;
    [loop]
    while(node>0)
    {
        float3 lo=BodyBounds[node*2].xyz,hi=BodyBounds[node*2+1].xyz;
        float3 outside=max(max(lo-p,p-hi),0);
        if(BodyBranchAllowed(node,leaves,group) && all(lo<=hi) && dot(outside,outside)<best)
        {
            if(node<leaves){node*=2;continue;}
            uint slot=node-leaves;
            if(slot<count)TestBodyTriangleGroup(BodyOrder[slot],group,p,best,q,bary,normal,nearestTri);
        }
        [loop]while(node>1 && (node&1)!=0)node>>=1;
        if(node==1)break;++node;
    }
    float3 diff=p-q;float signValue=dot(diff,normal)>=0?1.0:-1.0;
    signedDistance=sqrt(max(best,0))*signValue;
    normal=SafeUnit(diff*signValue,normal);
}

uint CellHash(int3 cell)
{
    return ((asuint(cell.x)*73856093u)^(asuint(cell.y)*19349663u)^(asuint(cell.z)*83492791u))&65535u;
}

bool SweepBox(float3 p,float3 delta,float limit,float3 lo,float3 hi)
{
    if(any(lo>hi))return false;float enter=0,leave=limit;
    [unroll]for(uint axis=0;axis<3;++axis)
    {
        if(abs(delta[axis])<1e-12){if(p[axis]<lo[axis]-1e-6 ||p[axis]>hi[axis]+1e-6)return false;}
        else
        {
            float a=(lo[axis]-1e-6-p[axis])/delta[axis],b=(hi[axis]+1e-6-p[axis])/delta[axis];
            enter=max(enter,min(a,b));leave=min(leave,max(a,b));if(enter>leave)return false;
        }
    }
    return true;
}
bool FirstBodyEntryGroup(float3 p,float3 delta,uint group,out float first,out float3 normal)
{
    first=1;normal=0;uint node=1,leaves=(uint)IniParams[115].y,count;BodyOrder.GetDimensions(count);bool hit=false;
    [loop]while(node>0)
    {
        if(BodyBranchAllowed(node,leaves,group) && SweepBox(p,delta,first,BodyBounds[node*2].xyz,BodyBounds[node*2+1].xyz))
        {
            if(node<leaves){node*=2;continue;}
            uint slot=node-leaves;
            if(slot<count && (group==0 ? (!SheetCollision() || CollisionAllowed(BodyOrder[slot])) : Allowed(BodyOrder[slot],group)))
            {
                uint tri=BodyOrder[slot];uint3 ids=uint3(BodyVertices[tri*3],BodyVertices[tri*3+1],BodyVertices[tri*3+2]);
                Vertex a=Source[ids.x],b=Source[ids.y],c=Source[ids.z];float3 ab=b.position-a.position,ac=c.position-a.position;
                float3 n=cross(ab,ac);float area=length(n);if(dot(n,a.normal+b.normal+c.normal)<0)n=-n;
                if(area>1e-10 && dot(n,delta)<-1e-8*area*length(delta))
                {
                    float3 h=cross(delta,ac),r=p-a.position;float determinant=dot(ab,h);
                    if(abs(determinant)>1e-14)
                    {
                        float u=dot(r,h)/determinant;float3 q=cross(r,ab);float v=dot(delta,q)/determinant,t=dot(ac,q)/determinant;
                        if(u>=-1e-6 && v>=-1e-6 && u+v<=1.000001 && t>=0 && t<=first){first=t;normal=n/area;hit=true;}
                    }
                }
            }
        }
        [loop]while(node>1 && (node&1)!=0)node>>=1;
        if(node==1)break;++node;
    }
    return hit;
}
bool FirstBodyEntry(float3 p,float3 delta,out float first,out float3 normal)
{return FirstBodyEntryGroup(p,delta,0,first,normal);}
float3 KeepOutside(float3 start,float3 goal,float gap)
{
    float3 safe=start;
    [loop]for(uint attempt=0;attempt<3;++attempt)
    {
        float3 delta=goal-safe;if(dot(delta,delta)<1e-16)return safe;
        float first;float3 n;if(!FirstBodyEntry(safe,delta,first,n))return goal;
        float allowed=max(first-gap/max(-dot(delta,n),1e-12),0);
        safe+=delta*allowed;float3 remainder=goal-safe;
        goal=safe+remainder-n*min(dot(remainder,n),0);
    }
    return safe;
}

float BodyLimitedOffset(float3 surface,float3 normal,float requested,float scale,uint group)
{
    float epsilon=min(requested,.00001*scale);
    float3 start=surface+normal*epsilon,delta=normal*(requested-epsilon);
    if(dot(delta,delta)<1e-16)return requested;
    float first;float3 blockerNormal;
    if(!FirstBodyEntryGroup(start,delta,group,first,blockerNormal))return requested;
    float available=epsilon+(requested-epsilon)*first;
    return max(0,available-min(.0005*scale,available*.5));
}

void ClosestBody(float3 p,uint seed,out float3 q,out float3 bary,out float3 normal,out uint nearestTri,out float signedDistance){ClosestBodyGroup(p,seed,0,q,bary,normal,nearestTri,signedDistance);}
int DomainWinding(float3 p,float3 direction)
{
 int winding=0;uint count;BodyOrder.GetDimensions(count);uint node=1,leaves=(uint)IniParams[115].y;
 [loop]while(node>0)
 {
  if(SweepBox(p,direction,1e10,BodyBounds[node*2].xyz,BodyBounds[node*2+1].xyz))
  {
   if(node<leaves){node*=2;continue;}uint at=node-leaves;
   if(at<count)
   {
    uint tri=BodyOrder[at];float3 a=Source[BodyVertices[tri*3]].position,b=Source[BodyVertices[tri*3+1]].position,c=Source[BodyVertices[tri*3+2]].position;
    float3 ab=b-a,ac=c-a,h=cross(direction,ac),r=p-a;float det=dot(ab,h);
    if(abs(det)>1e-14)
    {
     float u=dot(r,h)/det;float3 v=cross(r,ab);float w=dot(direction,v)/det,t=dot(ac,v)/det;
     if(u>=0 && w>=0 && u+w<=1 && t>1e-7)winding+=dot(cross(ab,ac),direction)>0?1:-1;
    }
   }
  }
  [loop]while(node>1 && (node&1)!=0)node>>=1;if(node==1)break;++node;
 }
 return winding;
}
bool DomainInside(float3 p)
{
 bool a=DomainWinding(p,normalize(float3(.871,.413,.269)))>0;
 bool b=DomainWinding(p,normalize(float3(-.359,.866,.347)))>0;
 if(a==b)return a;
 return DomainWinding(p,normalize(float3(.278,-.345,.896)))>0;
}
// A nearest triangle can be an internal boundary of two overlapping legs.
// Walk exits along a fixed material direction until outside their union.
bool DomainExitAlong(float3 p,float3 direction,float gap,float reach,out float3 target)
{
 target=p;float travelled=0;uint count;BodyOrder.GetDimensions(count);
 [loop]for(uint attempt=0;attempt<8;++attempt)
 {
  float first=reach-travelled;float3 outward=direction;bool hit=false;
  uint node=1,leaves=(uint)IniParams[115].y;
  [loop]while(node>0)
  {
   if(SweepBox(target,direction,first,BodyBounds[node*2].xyz,BodyBounds[node*2+1].xyz))
   {
    if(node<leaves){node*=2;continue;}uint at=node-leaves;
    if(at<count)
    {
     uint tri=BodyOrder[at];float3 a=Source[BodyVertices[tri*3]].position,b=Source[BodyVertices[tri*3+1]].position,c=Source[BodyVertices[tri*3+2]].position;
     float3 ab=b-a,ac=c-a,n=cross(ab,ac),h=cross(direction,ac),r=target-a;float det=dot(ab,h);
     if(dot(n,direction)>0 && abs(det)>1e-14)
     {
      float u=dot(r,h)/det;float3 v=cross(r,ab);float w=dot(direction,v)/det,t=dot(ac,v)/det;
      if(u>=-1e-6 && w>=-1e-6 && u+w<=1.000001 && t>1e-7 && t<first)
      {first=t;outward=SafeUnit(n,direction);hit=true;}
     }
    }
   }
   [loop]while(node>1 && (node&1)!=0)node>>=1;if(node==1)break;++node;
  }
  if(!hit)return false;
  float advance=first+max(gap,1e-5)/max(dot(outward,direction),.1);
  travelled+=advance;if(travelled>reach)return false;target=p+direction*travelled;
  if(!DomainInside(target))return true;
 }
 return false;
}
bool BodyContactTarget(float3 p,uint seed,float gap,float reach,out float3 target)
{
 target=p;bool changed=false;
 [loop]for(uint projection=0;projection<6;++projection)
 {
  float3 q,b,n;uint tri;float sd;ClosestBody(target,seed,q,b,n,tri,sd);
  float distanceToBody=distance(target,q);
  if(ClosedDomain())
  {
   bool inside=DomainInside(target);
   if(!inside && distanceToBody>=gap-1e-6)return changed;
   n=SafeUnit(inside?q-target:target-q,n);
  }
  else if(sd>=gap-1e-6)return changed;
  if(distanceToBody>reach)return changed;
  target=q+n*gap;seed=tri;changed=true;
 }
 return changed;
}
// Repulsion checks the full body; it never publishes an adhesion anchor.
void FaceCenterContact(inout float3 p0,inout float3 p1,inout float3 p2,float3 mass,float scale,uint seed)
{
 if(SheetCollision())
 {
  float3 center=(p0+p1+p2)/3,target;
  if(BodyContactTarget(center,seed,ClothGap()*scale,.03*scale,target))
  {
   float3 correction=(target-center)*3/max(dot(mass,1.0),1);float amount=length(correction);
   if(amount>.004*scale)correction*=.004*scale/amount;
   p0+=correction*mass.x;p1+=correction*mass.y;p2+=correction*mass.z;
  }
  return;
 }
 float3 center=(p0+p1+p2)/3,q,b,n;uint tri;float sd;
 ClosestBody(center,seed,q,b,n,tri,sd);
 float gap=ClothGap()*scale,reach=max(.03*scale,gap*2);
 if(sd<gap && distance(center,q)<reach && all(isfinite(q)))
 {
  float3 correction=n*min(gap-sd,.004*scale)*3/max(dot(mass,1.0),1);
  p0+=correction*mass.x;p1+=correction*mass.y;p2+=correction*mass.z;
 }
}

void AddBodyLevelHit(float3 at,float3 weights,inout uint count,inout float3 a,inout float3 b,inout float3 wa,inout float3 wb)
{
 if(count==0){a=at;wa=weights;b=at;wb=weights;}
 else if(dot(at-a,at-a)>dot(b-a,b-a)){b=at;wb=weights;}
 ++count;
}
float3 BodyChannelCenter(uint group,float level)
{
 float4 header=BodyChannels[0];uint samples=(uint)header.z;
 float at=saturate((level-header.x)/max(header.y-header.x,1e-8))*(samples-1);
 uint index=min((uint)at,samples-2),start=1+(clamp(group,1u,3u)-1)*samples;
 return lerp(BodyChannels[start+index].xyz,BodyChannels[start+index+1].xyz,at-index);
}
bool BodyLevelPoint(uint tri,uint group,float3 p,float level,float3 radial,out float3 q,out float3 bary,out float3 normal)
{
 q=p;bary=float3(1,0,0);normal=float3(0,1,0);uint count;BodyOrder.GetDimensions(count);
 if(tri>=count || !Allowed(tri,group))return false;
 uint3 ids=uint3(BodyVertices[tri*3],BodyVertices[tri*3+1],BodyVertices[tri*3+2]);
 Vertex a=Source[ids.x],b=Source[ids.y],c=Source[ids.z];
 Vertex ba=BindVertices[ids.x],bb=BindVertices[ids.y],bc=BindVertices[ids.z];
 float3 h=float3(dot(ba.position-Config[8].xyz,Config[6].xyz),dot(bb.position-Config[8].xyz,Config[6].xyz),dot(bc.position-Config[8].xyz,Config[6].xyz))/max(Config[6].w,1e-6);
 if(level<min(h.x,min(h.y,h.z))-1e-6 || level>max(h.x,max(h.y,h.z))+1e-6)return false;
 float3 bindNormal=ba.normal+bb.normal+bc.normal;bindNormal-=Config[6].xyz*dot(bindNormal,Config[6].xyz);
 if(dot(bindNormal,bindNormal)>1e-8 && dot(SafeUnit(bindNormal,radial),radial)<-.15)return false;
 float3 first=0,last=0,firstWeights=0,lastWeights=0;uint n=0;float t;
 if(abs(h.y-h.x)>1e-8 && (h.x-level)*(h.y-level)<=1e-10){t=saturate((level-h.x)/(h.y-h.x));AddBodyLevelHit(lerp(a.position,b.position,t),float3(1-t,t,0),n,first,last,firstWeights,lastWeights);}
 if(abs(h.z-h.y)>1e-8 && (h.y-level)*(h.z-level)<=1e-10){t=saturate((level-h.y)/(h.z-h.y));AddBodyLevelHit(lerp(b.position,c.position,t),float3(0,1-t,t),n,first,last,firstWeights,lastWeights);}
 if(abs(h.x-h.z)>1e-8 && (h.z-level)*(h.x-level)<=1e-10){t=saturate((level-h.z)/(h.x-h.z));AddBodyLevelHit(lerp(c.position,a.position,t),float3(t,0,1-t),n,first,last,firstWeights,lastWeights);}
 if(n==0){ClosestTriangle(p,a.position,b.position,c.position,q,bary);}
 else
 {
  uint channels;BodyChannels.GetDimensions(channels);
  if(Config[15].z>.5 && channels>=7 && BodyChannels[0].w>.5)
  {
   uint channel=Sliding()?BodyRegion(tri):group;
   float3 center=BodyChannelCenter(channel,level),perp=cross(Config[6].xyz,radial);
   float3 bindFirst=ba.position*firstWeights.x+bb.position*firstWeights.y+bc.position*firstWeights.z;
   float3 bindLast=ba.position*lastWeights.x+bb.position*lastWeights.y+bc.position*lastWeights.z;
   float x=dot(bindFirst-center,perp),y=dot(bindLast-center,perp);
   if(x*y>1e-12)return false;
   if(abs(x-y)>1e-8)t=saturate(x/(x-y));
   else{if(abs(x)>1e-6)return false;t=dot(bindFirst-center,radial)>dot(bindLast-center,radial)?0:1;}
   if(dot(lerp(bindFirst,bindLast,t)-center,radial)<-1e-6)return false;
  }
  else{float3 delta=last-first;t=saturate(dot(p-first,delta)/max(dot(delta,delta),1e-20));}
  q=lerp(first,last,t);bary=lerp(firstWeights,lastWeights,t);
 }
 normal=SafeUnit(a.normal*bary.x+b.normal*bary.y+c.normal*bary.z,SafeUnit(cross(b.position-a.position,c.position-a.position),float3(0,1,0)));
 return true;
}

bool FollowBodyLevel(float3 p,uint seed,uint group,float level,out float3 q,out float3 bary,out float3 normal,out uint nearest,out float sd);
bool TrackBodyLevel(float3 p,uint seed,uint group,float level,float3 radial,out float3 q,out float3 bary,out float3 normal,out uint nearest,out float sd)
{
 if(IntrinsicSurface())return FollowBodyLevel(p,seed,group,level,q,bary,normal,nearest,sd);
 q=p;bary=float3(1,0,0);normal=float3(0,1,0);nearest=seed;sd=0;float best=1e30;uint count;BodyOrder.GetDimensions(count);
 [loop]for(uint k=0;k<4;++k)
 {
  uint tri=k==0?seed:(seed<count?BodyAdjacency[seed*3+k-1]:0xffffffffu);float3 at,lam,n;
  if(BodyLevelPoint(tri,group,p,level,radial,at,lam,n))
  {float d=dot(at-p,at-p);if(d<best){best=d;q=at;bary=lam;normal=n;nearest=tri;}}
 }
 if(best<1e29){sd=dot(p-q,normal);return true;}
 uint metadata;LevelConfig.GetDimensions(metadata);
 if(IniParams[126].x>.5 && metadata>0 && LevelConfig[0].w>1)
 {
  uint bands=(uint)LevelConfig[0].w,words=(count+31)/32,available;LevelCandidates.GetDimensions(available);
  if(available==bands*words)
  {
   uint band=(uint)clamp(floor((level-LevelConfig[0].x)*LevelConfig[0].z),0,bands-1);
   [loop]for(uint word=0;word<words;++word)
   {
    uint candidates=LevelCandidates[band*words+word];
    [loop]while(candidates!=0)
    {
     uint bit=firstbitlow(candidates);candidates&=candidates-1;
     uint at=word*32+bit;if(at>=count)continue;
     uint face=BodyOrder[at];float3 candidatePosition,weights,n;
     if(BodyLevelPoint(face,group,p,level,radial,candidatePosition,weights,n))
     {float d=dot(candidatePosition-p,candidatePosition-p);if(d<best){best=d;q=candidatePosition;bary=weights;normal=n;nearest=face;}}
    }
   }
   sd=dot(p-q,normal);return best<1e29;
  }
 }
 uint node=1,leaves=(uint)IniParams[115].y;
 [loop]while(node>0)
 {
  float3 lo=BodyBounds[node*2].xyz,hi=BodyBounds[node*2+1].xyz,outside=max(max(lo-p,p-hi),0);
  if(BodyBranchAllowed(node,leaves,group) && all(lo<=hi) && dot(outside,outside)<best)
  {
   if(node<leaves){node*=2;continue;}
   uint index=node-leaves;
   if(index<count)
   {uint tri=BodyOrder[index];float3 at,lam,n;if(BodyLevelPoint(tri,group,p,level,radial,at,lam,n)){float d=dot(at-p,at-p);if(d<best){best=d;q=at;bary=lam;normal=n;nearest=tri;}}}
  }
  [loop]while(node>1 && (node&1)!=0)node>>=1;
  if(node==1)break;++node;
 }
 sd=dot(p-q,normal);return best<1e29;
}

void SurfacePoint(float3 p,uint seed,uint group,out float3 q,out float3 bary,out float3 normal,out uint nearest,out float distanceValue)
{
 uint bodyCount;BodyOrder.GetDimensions(bodyCount);uint adjacency;BodyAdjacency.GetDimensions(adjacency);
 uint tri=seed,previous=0xffffffffu;bool found=false;q=p;bary=float3(1,0,0);normal=float3(0,1,0);nearest=0;
 if((IntrinsicSurface() || Config[15].w>.5) && adjacency==bodyCount*3 && seed<bodyCount && Allowed(seed,group))
 {
  // Follow only edge-adjacent triangles from the previous contact. If the
  // local patch ends, stay on its boundary instead of jumping across space.
  float best=1e30;uint at=seed;
  [loop]for(uint step=0;step<24;++step)
  {
   uint chosen=at;float3 chosenNormal=normal;
   uint3 center=uint3(BodyVertices[at*3],BodyVertices[at*3+1],BodyVertices[at*3+2]);
   float3 reference=SafeUnit(Source[center.x].normal+Source[center.y].normal+Source[center.z].normal,float3(0,1,0));
   [unroll]for(uint candidate=0;candidate<4;++candidate)
   {
    uint face=candidate==0?at:BodyAdjacency[at*3+candidate-1];
    if(face>=bodyCount || !Allowed(face,group))continue;
    uint3 ids=uint3(BodyVertices[face*3],BodyVertices[face*3+1],BodyVertices[face*3+2]);
    Vertex a=Source[ids.x],b=Source[ids.y],c=Source[ids.z];
    float3 n=SafeUnit(a.normal+b.normal+c.normal,reference);if(dot(n,reference)<.25 && !IntrinsicSurface())continue;
    float3 closestPosition,weights;ClosestTriangle(p,a.position,b.position,c.position,closestPosition,weights);
    float d=dot(p-closestPosition,p-closestPosition);
    if(d<best-1e-12){best=d;q=closestPosition;bary=weights;nearest=face;chosen=face;chosenNormal=SafeUnit(a.normal*weights.x+b.normal*weights.y+c.normal*weights.z,n);found=true;}
   }
   normal=chosenNormal;if(chosen==at)break;at=chosen;
  }
  if(found){distanceValue=dot(p-q,normal);return;}
 }
 if(IntrinsicSurface())
 {
  float sd;ClosestBodyGroup(p,seed,group,q,bary,normal,nearest,sd);
  uint3 ids=uint3(BodyVertices[nearest*3],BodyVertices[nearest*3+1],BodyVertices[nearest*3+2]);
  normal=SafeUnit(Source[ids.x].normal*bary.x+Source[ids.y].normal*bary.y+Source[ids.z].normal*bary.z,normal);
  distanceValue=dot(p-q,normal);return;
 }
 if(adjacency==bodyCount*3)
 {
  [loop]for(uint step=0;step<12;++step)
  {
   if(tri>=bodyCount || !Allowed(tri,group))break;
   uint3 ids=uint3(BodyVertices[tri*3],BodyVertices[tri*3+1],BodyVertices[tri*3+2]);
   Vertex a=Source[ids.x],b=Source[ids.y],c=Source[ids.z];float3 u=b.position-a.position,v=c.position-a.position,w=p-a.position;
   float aa=dot(u,u),ab=dot(u,v),bb=dot(v,v),den=aa*bb-ab*ab;if(abs(den)<1e-16)break;
   float y=(bb*dot(w,u)-ab*dot(w,v))/den,z=(aa*dot(w,v)-ab*dot(w,u))/den;float3 b0=float3(1-y-z,y,z);
   if(all(b0>=-.00001))
   {
    b0=max(b0,0);b0/=max(dot(b0,1.0),1e-8);float3 at=a.position*b0.x+b.position*b0.y+c.position*b0.z;
    if(distance(p,at)>.05)break;
    q=at;bary=b0;normal=SafeUnit(a.normal*b0.x+b.normal*b0.y+c.normal*b0.z,SafeUnit(cross(u,v),float3(0,1,0)));nearest=tri;found=true;break;
   }
   uint slot=b0.x<b0.y?(b0.x<b0.z?0:2):(b0.y<b0.z?1:2);uint next=BodyAdjacency[tri*3+slot];
   if(next==previous || next>=bodyCount || !Allowed(next,group))break;
   uint3 nextIds=uint3(BodyVertices[next*3],BodyVertices[next*3+1],BodyVertices[next*3+2]);
   float3 before=SafeUnit(a.normal+b.normal+c.normal,float3(0,1,0));
   float3 after=SafeUnit(Source[nextIds.x].normal+Source[nextIds.y].normal+Source[nextIds.z].normal,before);
   if(dot(before,after)<.25 && !IntrinsicSurface())break;previous=tri;tri=next;
  }
 }
 if(!found)
 {
  float sd;ClosestBodyGroup(p,seed,group,q,bary,normal,nearest,sd);
  uint3 ids=uint3(BodyVertices[nearest*3],BodyVertices[nearest*3+1],BodyVertices[nearest*3+2]);
  normal=SafeUnit(Source[ids.x].normal*bary.x+Source[ids.y].normal*bary.y+Source[ids.z].normal*bary.z,normal);
 }
 distanceValue=dot(p-q,normal);
}

bool FollowBodyLevel(float3 p,uint seed,uint group,float level,out float3 q,out float3 bary,out float3 normal,out uint nearest,out float sd)
{
 SurfacePoint(p,seed,group,q,bary,normal,nearest,sd);
 uint count;BodyOrder.GetDimensions(count);
 if(nearest>=count || !Allowed(nearest,group))return false;
 [loop]for(uint iteration=0;iteration<12;++iteration)
 {
  uint3 ids=uint3(BodyVertices[nearest*3],BodyVertices[nearest*3+1],BodyVertices[nearest*3+2]);
  float3 h=float3(ReferenceBodyLevel(ids.x),ReferenceBodyLevel(ids.y),ReferenceBodyLevel(ids.z));
  float error=level-dot(h,bary);if(abs(error)<1e-5)break;
  float3 a=Source[ids.x].position,u=Source[ids.y].position-a,v=Source[ids.z].position-a;
  float aa=dot(u,u),ab=dot(u,v),bb=dot(v,v),den=aa*bb-ab*ab;if(den<1e-20)break;
  float3 gradient=u*(bb*(h.y-h.x)-ab*(h.z-h.x))/den+v*(aa*(h.z-h.x)-ab*(h.y-h.x))/den;
  float norm=dot(gradient,gradient);if(norm<1e-12)break;
  float3 delta=gradient*(error/norm);float travel=length(delta),limit=max(Config[6].w*.06,.002);if(travel>limit)delta*=limit/travel;
  float3 old=q,next,b,n;uint tri;float distanceValue;
  SurfacePoint(q+delta,nearest,group,next,b,n,tri,distanceValue);
  if(tri>=count || !Allowed(tri,group) || distance(old,next)<1e-7)break;
  uint3 nextIds=uint3(BodyVertices[tri*3],BodyVertices[tri*3+1],BodyVertices[tri*3+2]);
  float nextLevel=dot(float3(ReferenceBodyLevel(nextIds.x),ReferenceBodyLevel(nextIds.y),ReferenceBodyLevel(nextIds.z)),b);
  if(abs(level-nextLevel)>abs(error)+1e-5)break;
  q=next;bary=b;normal=n;nearest=tri;
 }
 sd=dot(p-q,normal);return true;
}
