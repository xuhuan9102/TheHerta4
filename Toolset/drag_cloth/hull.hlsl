#include "common.hlsli"
Buffer<float4> FrameState : register(t77);
RWBuffer<float4> CrossPoints : register(u0);
RWBuffer<uint> Counter : register(u1);
[numthreads(256,1,1)]void main(uint3 dispatch:SV_DispatchThreadID)
{
    uint i=dispatch.x;
    if(IniParams[119].y<.5){if(i==0)Counter[0]=0;return;}
    if(FrameState[23].w<.5)return;
    uint count;BodyOrder.GetDimensions(count);if(i>=count)return;
    uint tri=BodyOrder[i];uint3 ids=uint3(BodyVertices[tri*3],BodyVertices[tri*3+1],BodyVertices[tri*3+2]);
    if(Config[12].w>.5 && BodyRegion(tri)!=(uint)Config[12].w)return;
    float3 bind=(BindVertices[ids.x].position+BindVertices[ids.y].position+BindVertices[ids.z].position)/3-Config[8].xyz;
    if(abs(dot(bind,Config[7].xyz))>Config[6].w*.20)return;
    float4 frame=FrameState[8],foot=FrameState[4];float3 up=FrameState[5].xyz;
    float alpha=lerp(frame.x,frame.y,(IniParams[119].x+1)/max(Config[3].y,1));
    float3 centre=foot.xyz+up*foot.w*lerp(1,Config[4].x,alpha);
    float3 p[3];p[0]=Source[ids.x].position;p[1]=Source[ids.y].position;p[2]=Source[ids.z].position;
    float3 points[3];uint n=0;
    [unroll]for(uint edge=0;edge<3;++edge)
    {
        float3 a=p[edge],b=p[(edge+1)%3];float x=dot(a-centre,up),y=dot(b-centre,up);
        if(x*y<=0 && abs(x-y)>1e-9)points[n++]=lerp(a,b,saturate(x/(x-y)));
    }
    if(n>0){uint begin;InterlockedAdd(Counter[0],n,begin);[loop]for(uint k=0;k<n;++k)CrossPoints[begin+k]=float4(points[k]-centre,1);}
}
