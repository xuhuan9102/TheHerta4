struct Vertex { float3 position; float3 normal; float4 tangent; };
struct Particle { float4 current; float4 previous; };
struct Blend { float4 weights; uint4 indices; };
StructuredBuffer<Vertex> Source : register(t24);
Buffer<uint> BodyVertices : register(t87);
Buffer<uint> BodyOrder : register(t89);
Buffer<float4> BodyBounds : register(t91);
Buffer<float4> Config : register(t82);
Texture1D<float4> IniParams : register(t120);

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
float2 GatherProfile(float materialHeight,float hemHeight)
{
    const float anchor=.07;
    if(materialHeight<=anchor)return float2(materialHeight,1);
    float t=saturate((materialHeight-anchor)/(1-anchor));
    float ratio=clamp((hemHeight-anchor)/(1-anchor),.08,1);
    float power=min(2,1+.6*ratio/max(1-ratio,.0001));
    float slope=1-(1-ratio)*power*pow(max(t,.0001),power-1);
    float height=anchor+(1-anchor)*(t-(1-ratio)*pow(t,power));
    height+=max(materialHeight-1,0)*max(slope,.05);
    return float2(height,saturate(slope));
}

Buffer<uint> BodyOwners : register(t109);
Buffer<uint> BodyAdjacency : register(t105);
Buffer<uint> SurfaceMask : register(t108);
Buffer<uint> BodyExterior : register(t106);
StructuredBuffer<Vertex> BindVertices : register(t111);
Buffer<uint> BodyContacts : register(t112);
bool Exterior(uint tri){uint n;BodyExterior.GetDimensions(n);return n<=tri || BodyExterior[tri]!=0;}

uint BodyRegion(uint tri){uint count;BodyOwners.GetDimensions(count);return tri<count?BodyOwners[tri]:0;}
bool Allowed(uint tri,uint group)
{
 uint surfaces;SurfaceMask.GetDimensions(surfaces);if(surfaces>tri && SurfaceMask[tri]==0)return false;
 uint n;BodyContacts.GetDimensions(n);bool contact=n>tri && BodyContacts[tri]!=0;
 if(group==0)return Exterior(tri);
 if(group==3)return Exterior(tri) && !contact;
 uint body=BodyRegion(tri);
 return body==group;
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
        if(all(lo<=hi) && dot(outside,outside)<best)
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
bool FirstBodyEntry(float3 p,float3 delta,out float first,out float3 normal)
{
    first=1;normal=0;uint node=1,leaves=(uint)IniParams[115].y,count;BodyOrder.GetDimensions(count);bool hit=false;
    [loop]while(node>0)
    {
        if(SweepBox(p,delta,first,BodyBounds[node*2].xyz,BodyBounds[node*2+1].xyz))
        {
            if(node<leaves){node*=2;continue;}
            uint slot=node-leaves;
            if(slot<count)
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
float3 KeepOutside(float3 start,float3 goal)
{
    float3 safe=start;
    [loop]for(uint attempt=0;attempt<3;++attempt)
    {
        float3 delta=goal-start;if(dot(delta,delta)<1e-16)return start;
        float first;float3 n;if(!FirstBodyEntry(start,delta,first,n))return goal;
        float allowed=max(first-.001/max(-dot(delta,n),1e-12),0);
        safe=start+delta*allowed;float3 remainder=goal-safe;
        goal=safe+remainder-n*min(dot(remainder,n),0);
    }
    return safe;
}

void ClosestBody(float3 p,uint seed,out float3 q,out float3 bary,out float3 normal,out uint nearestTri,out float signedDistance){ClosestBodyGroup(p,seed,0,q,bary,normal,nearestTri,signedDistance);}

void SurfacePoint(float3 p,uint seed,uint group,out float3 q,out float3 bary,out float3 normal,out uint nearest,out float distanceValue)
{
 uint bodyCount;BodyOrder.GetDimensions(bodyCount);uint adjacency;BodyAdjacency.GetDimensions(adjacency);
 uint tri=seed,previous=0xffffffffu;bool found=false;q=p;bary=float3(1,0,0);normal=float3(0,1,0);nearest=0;
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
   if(dot(before,after)<.25)break;previous=tri;tri=next;
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
