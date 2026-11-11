#include "constants.hlsli"
#include "common.hlsli"
RWStructuredBuffer<Vertex> FullBind : register(u0);
[numthreads(256,1,1)]void main(uint3 id:SV_DispatchThreadID)
{
    uint i=id.x,sourceCount,sourceStride,destCount,destStride;
    Source.GetDimensions(sourceCount,sourceStride);FullBind.GetDimensions(destCount,destStride);
    if(sourceCount!=BIND_SOURCE_COUNT || sourceStride!=VERTEX_STRIDE ||
       destCount!=BIND_RUNTIME_COUNT || destStride!=VERTEX_STRIDE || i>=destCount)return;
    if(i>=BIND_VERTEX_BASE && i<BIND_VERTEX_BASE+BIND_SOURCE_COUNT)FullBind[i]=Source[i-BIND_VERTEX_BASE];
    else FullBind[i]=BindVertices[i];
}
