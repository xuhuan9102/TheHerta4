struct Vertex {float3 position;float3 normal;float4 tangent;};
StructuredBuffer<Vertex> Bind : register(t24);
StructuredBuffer<Vertex> Native : register(t95);
Buffer<float4> OriginalBlend : register(t86);
Buffer<float4> Skeleton1 : register(t2);
Buffer<float4> Skeleton2 : register(t3);
Buffer<uint> WearMap : register(t94);
Buffer<uint> BodyVertices : register(t87);
Buffer<float4> Config : register(t82);
RWBuffer<float4> Decision : register(u6);
[numthreads(1,1,1)]
void main(uint3 dispatch:SV_DispatchThreadID)
{
    uint count,stride,nativeCount,nativeStride,b1,b2;
    Bind.GetDimensions(count,stride);Native.GetDimensions(nativeCount,nativeStride);
    Skeleton1.GetDimensions(b1);Skeleton2.GetDimensions(b2);b1/=3;b2/=3;
    Decision[0]=float4(0,1e10,1e10,0);Decision[1]=0;Decision[2]=float4(count,nativeCount,b1,b2);
    if(count!=(uint)Config[0].y || nativeCount!=count || stride!=40 || nativeStride!=40 || b1==0 || b2==0)return;
    float error1=0,error2=0,maximum1=0,maximum2=0,samples=0;
    uint bodyIndices;BodyVertices.GetDimensions(bodyIndices);if(bodyIndices<3)return;
    [loop]for(uint sampleIndex=0;sampleIndex<64;++sampleIndex)
    {
        uint row=BodyVertices[(sampleIndex*7919u)%bodyIndices];if(row>=count)continue;
        float3 p=Bind[row].position,target=Native[row].position;
        if(!all(isfinite(p)) || !all(isfinite(target)) || dot(Bind[row].normal,Bind[row].normal)<.01)continue;
        float4 weights=OriginalBlend[row*2];uint4 indices=asuint(OriginalBlend[row*2+1]);
        float3 p1=0,p2=0;float total=0;
        [unroll]for(uint k=0;k<4;++k)
        {
            float w=weights[k];uint bone=indices[k];if(w<=0)continue;
            if(bone>=b1 || bone>=b2){total=-100;break;}
            float4 a=Skeleton1[bone*3],b=Skeleton1[bone*3+1],c=Skeleton1[bone*3+2];
            p1+=w*float3(dot(a.xyz,p)+a.w,dot(b.xyz,p)+b.w,dot(c.xyz,p)+c.w);
            a=Skeleton2[bone*3];b=Skeleton2[bone*3+1];c=Skeleton2[bone*3+2];
            p2+=w*float3(dot(a.xyz,p)+a.w,dot(b.xyz,p)+b.w,dot(c.xyz,p)+c.w);total+=w;
        }
        if(total<.5)continue;
        float d1=dot(p1-target,p1-target),d2=dot(p2-target,p2-target);
        error1+=d1;error2+=d2;maximum1=max(maximum1,sqrt(d1));maximum2=max(maximum2,sqrt(d2));samples+=1;
    }
    float rms1=sqrt(error1/max(samples,1)),rms2=sqrt(error2/max(samples,1));
    float selected=rms1<=rms2?1:2;
    bool valid=samples>=16 && min(rms1,rms2)<.01 && (selected<1.5?maximum1:maximum2)<.05;
    Decision[0]=float4(selected,rms1,rms2,valid?1:0);
    Decision[1]=float4(maximum1,maximum2,samples,1);
}
