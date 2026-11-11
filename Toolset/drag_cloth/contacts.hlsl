#include "common.hlsli"
RWBuffer<uint> Contacts : register(u0);
uint Region(uint3 ids)
{
 float3 p=(BindVertices[ids.x].position+BindVertices[ids.y].position+BindVertices[ids.z].position)/3-Config[8].xyz;
 float h=dot(p,Config[6].xyz)/max(Config[6].w,1e-6),side=dot(p,Config[7].xyz)/max(Config[6].w,1e-6);
 return h>.42 && h<.88 && abs(side)>.004 && abs(side)<.16 ? (side<0?1:2):0;
}
bool SegmentHit(float3 p,float3 end,float3 a,float3 b,float3 c)
{
    float3 d=end-p,ab=b-a,ac=c-a,h=cross(d,ac);float den=dot(ab,h);if(abs(den)<1e-12)return false;
    float3 v=p-a,q=cross(v,ab);float u=dot(v,h)/den,w=dot(d,q)/den,t=dot(ac,q)/den;
    return u>=-1e-6 && w>=-1e-6 && u+w<=1.000001 && t>=0 && t<=1;
}
[numthreads(128,1,1)]void main(uint3 id:SV_DispatchThreadID)
{
    uint i=id.x,count;BodyOrder.GetDimensions(count);if(i>=count)return;
    uint3 ids=uint3(BodyVertices[i*3],BodyVertices[i*3+1],BodyVertices[i*3+2]);uint region=Region(ids);
    if(region==0){Contacts[i]=0;return;}
    if(!Exterior(i)){Contacts[i]=1;return;}
    float3 p[3];p[0]=Source[ids.x].position;p[1]=Source[ids.y].position;p[2]=Source[ids.z].position;
    float gap=Config[2].z*2;float3 lo=min(p[0],min(p[1],p[2]))-gap,hi=max(p[0],max(p[1],p[2]))+gap;bool hit=false;
    uint node=1,leaves=(uint)IniParams[115].y;
    [loop]while(node>0 && !hit)
    {
        float3 nlo=BodyBounds[node*2].xyz,nhi=BodyBounds[node*2+1].xyz;
        if(all(lo<=nhi) && all(hi>=nlo) && all(nlo<=nhi))
        {
            if(node<leaves){node*=2;continue;}
            uint at=node-leaves;
            if(at<count)
            {
                uint tri=BodyOrder[at];uint3 ids2=uint3(BodyVertices[tri*3],BodyVertices[tri*3+1],BodyVertices[tri*3+2]);uint other=Region(ids2);
                if(other!=0 && other!=region)
                {
                    float3 q[3];q[0]=Source[ids2.x].position;q[1]=Source[ids2.y].position;q[2]=Source[ids2.z].position;
                    [unroll]for(uint k=0;k<3;++k)hit=hit||SegmentHit(p[k],p[(k+1)%3],q[0],q[1],q[2])||SegmentHit(q[k],q[(k+1)%3],p[0],p[1],p[2]);
                    [unroll]for(uint vertex=0;vertex<3;++vertex)
                    {
                        float3 near,bary;ClosestTriangle(p[vertex],q[0],q[1],q[2],near,bary);hit=hit||distance(p[vertex],near)<gap;
                        ClosestTriangle(q[vertex],p[0],p[1],p[2],near,bary);hit=hit||distance(q[vertex],near)<gap;
                    }
                }
            }
        }
        [loop]while(node>1 && (node&1)!=0)node>>=1;
        if(node==1)break;++node;
    }
    Contacts[i]=hit?1:0;
}
