#include "common.hlsli"
RWBuffer<uint> ExteriorOut : register(u0);
bool RayHit(float3 p,float3 direction,float3 a,float3 b,float3 c,out float signValue)
{
    float3 ab=b-a,ac=c-a,h=cross(direction,ac);float den=dot(ab,h);signValue=0;
    if(abs(den)<1e-12)return false;
    float3 v=p-a,q=cross(v,ab);float u=dot(v,h)/den,w=dot(direction,q)/den,t=dot(ac,q)/den;
    if(u<0 || w<0 || u+w>1 || t<.000001)return false;
    signValue=dot(cross(ab,ac),direction)>0?1:-1;return true;
}
[numthreads(128,1,1)]
void main(uint3 id:SV_DispatchThreadID)
{
    uint i=id.x,count;BodyOrder.GetDimensions(count);if(i>=count)return;
    uint3 vi=uint3(BodyVertices[i*3],BodyVertices[i*3+1],BodyVertices[i*3+2]);
    Vertex a=Source[vi.x],b=Source[vi.y],c=Source[vi.z];float3 n=cross(b.position-a.position,c.position-a.position);
    if(dot(n,a.normal+b.normal+c.normal)<0)n=-n;
    n=SafeUnit(n,float3(0,1,0));float3 p=(a.position+b.position+c.position)/3+n*.00002;
    float3 direction=normalize(float3(.173,1,.379));uint node=1,leaves=(uint)IniParams[115].y;float winding=0;
    [loop]while(node>0)
    {
        if(SweepBox(p,direction,8,BodyBounds[node*2].xyz,BodyBounds[node*2+1].xyz))
        {
            if(node<leaves){node*=2;continue;}
            uint at=node-leaves;
            if(at<count)
            {
                uint tri=BodyOrder[at];uint3 rows=uint3(BodyVertices[tri*3],BodyVertices[tri*3+1],BodyVertices[tri*3+2]);
                Vertex x=Source[rows.x],y=Source[rows.y],z=Source[rows.z];float value;
                if(RayHit(p,direction,x.position,y.position,z.position,value))
                {
                    float orient=dot(cross(y.position-x.position,z.position-x.position),x.normal+y.normal+z.normal)<0?-1:1;
                    winding+=value*orient;
                }
            }
        }
        [loop]while(node>1 && (node&1)!=0)node>>=1;
        if(node==1)break;++node;
    }
    ExteriorOut[i]=winding>.5?0:1;
}
