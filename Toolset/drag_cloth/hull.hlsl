#include "common.hlsli"
Buffer<float4> FrameState : register(t77);
RWBuffer<float4> CrossPoints : register(u0);
RWBuffer<uint> Counter : register(u1);
[numthreads(256,1,1)]void main(uint3 dispatch:SV_DispatchThreadID)
{
    uint i=dispatch.x;
    if(FrameState[9].y<.5 && IniParams[127].y<.5)return;
    if(AdaptiveCloth() && IniParams[119].z>.5 && IniParams[119].x>=ClothSubsteps(FrameState[ClothWorkRow()]))return;
    if(FrameState[ClothSleepRow()].x>.5)return;
    uint ends=TwoEnded()?2:1;
    if(IniParams[119].y<.5){if(i<ends)Counter[i]=0;return;}
    if(FrameState[23].w<.5)return;
    if(GarmentAmount(FrameState[0].xy)<1e-5 && GarmentAmount(float2(FrameState[8].x,FrameState[10].x))<1e-5)return;
    uint count;BodyOrder.GetDimensions(count);if(i>=count)return;
    uint tri=BodyOrder[i];uint3 ids=uint3(BodyVertices[tri*3],BodyVertices[tri*3+1],BodyVertices[tri*3+2]);
    if(Config[12].w>.5 && BodyRegion(tri)!=(uint)Config[12].w)return;
    if(ManualSurface() && Config[12].w<.5 && BodyRegion(tri)!=3)return;
    if(Sliding() && BodyRegion(tri)==0)return;
    float3 bind=(BindVertices[ids.x].position+BindVertices[ids.y].position+BindVertices[ids.z].position)/3-Config[8].xyz;
    if(abs(dot(bind,Config[7].xyz))>max(Config[6].w*.20,Config[7].w))return;
    float4 frame=FrameState[8],foot=FrameState[4];float3 up=FrameState[5].xyz;
    float fraction=(IniParams[119].x+1)/ClothSubsteps(FrameState[ClothWorkRow()]);
    float2 progress=float2(lerp(frame.x,frame.y,fraction),lerp(FrameState[10].x,FrameState[10].y,fraction));
    float2 interval=GarmentInterval(progress);
    float3 p[3];p[0]=Source[ids.x].position;p[1]=Source[ids.y].position;p[2]=Source[ids.z].position;
    float3 materialHeight=float3(dot(BindVertices[ids.x].position-Config[8].xyz,Config[6].xyz),dot(BindVertices[ids.y].position-Config[8].xyz,Config[6].xyz),dot(BindVertices[ids.z].position-Config[8].xyz,Config[6].xyz))/max(Config[6].w,1e-6);
    [loop]for(uint end=0;end<ends;++end)
    {
        float3 centre=foot.xyz+up*foot.w*(end==0?interval.y:interval.x);
        float3 points[3];uint n=0;
        [unroll]for(uint edge=0;edge<3;++edge)
        {
            uint next=(edge+1)%3;float3 a=p[edge],b=p[next];float target=end==0?interval.y:interval.x;
            float x=Config[15].w>.5?materialHeight[edge]-target:dot(a-centre,up),y=Config[15].w>.5?materialHeight[next]-target:dot(b-centre,up);
            if(x*y<=0 && abs(x-y)>1e-9)points[n++]=lerp(a,b,saturate(x/(x-y)));
        }
        if(n>0)
        {
            uint begin;InterlockedAdd(Counter[end],n,begin);
            [loop]for(uint k=0;k<n && begin+k<count*3;++k)
                CrossPoints[end*count*3+begin+k]=float4(points[k]-centre,1);
        }
    }
}
