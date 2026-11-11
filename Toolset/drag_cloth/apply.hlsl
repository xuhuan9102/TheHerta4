#include "constants.hlsli"
#include "common.hlsli"
StructuredBuffer<Particle> Particles : register(t76);
Buffer<uint> WearMap : register(t94);
Buffer<uint> DisplayTriangles : register(t81);
Buffer<float4> DisplayWeights : register(t83);
Buffer<float4> Rest : register(t75);
Buffer<float4> State : register(t77);
Buffer<uint> Faces : register(t79);
Buffer<uint> FaceAdjacency : register(t80);
Buffer<uint> Representatives : register(t85);
Buffer<float4> PaletteDecision : register(t96);
RWBuffer<float> Output : register(u0);

float3 ParticleNormal(uint particle)
{
    float3 normal=0;uint begin=FaceAdjacency[particle],end=FaceAdjacency[particle+1];
    [loop]for(uint i=begin;i<end;++i)
    {
        uint face=FaceAdjacency[PARTICLE_COUNT+1+i];
        uint3 ids=uint3(Faces[face*3],Faces[face*3+1],Faces[face*3+2]);
        Vertex a=Source[Representatives[ids.x]],b=Source[Representatives[ids.y]],c=Source[Representatives[ids.z]];
        float orientation=dot(cross(b.position-a.position,c.position-a.position),a.normal+b.normal+c.normal)<0?-1:1;
        float3 p0=Particles[ids.x].current.xyz,p1=Particles[ids.y].current.xyz,p2=Particles[ids.z].current.xyz;
        normal+=cross(p1-p0,p2-p0)*orientation;
    }
    return SafeUnit(normal,Source[Representatives[particle]].normal);
}

[numthreads(256,1,1)]void main(uint3 dispatch:SV_DispatchThreadID)
{
    uint row=dispatch.x,count,particles,pstride,sourceCount,sourceStride;
    if(PaletteDecision[0].w<.5 || State[23].w<.5 || State[0].x<1e-5)return;
    Output.GetDimensions(count);Particles.GetDimensions(particles,pstride);Source.GetDimensions(sourceCount,sourceStride);
    if(count!=(uint)Config[0].y*VERTEX_FLOATS || particles!=PARTICLE_COUNT || pstride!=32 ||
       sourceCount!=(uint)Config[0].y || sourceStride!=VERTEX_STRIDE || row>=sourceCount)return;
    uint particle=WearMap[row];if(particle>=PARTICLE_COUNT)return;
    uint3 ids=uint3(DisplayTriangles[row*3],DisplayTriangles[row*3+1],DisplayTriangles[row*3+2]);
    if(any(ids>=PARTICLE_COUNT))return;
    float4 weights=DisplayWeights[row];float3 p=0,origin=0,normal=0;
    float3 firstNormal=ParticleNormal(ids.x);
    [unroll]for(uint k=0;k<3;++k)
    {
        p+=Particles[ids[k]].current.xyz*weights[k];
        origin+=Source[Representatives[ids[k]]].position*weights[k];
        normal+=(ids[k]==ids.x?firstNormal:ParticleNormal(ids[k]))*weights[k];
    }
    Vertex vertex=Source[row];float3 residual=vertex.position-origin;
    float3 oldU=Source[Representatives[ids.y]].position-Source[Representatives[ids.x]].position;
    float3 oldV=Source[Representatives[ids.z]].position-Source[Representatives[ids.x]].position;
    float aa=dot(oldU,oldU),ab=dot(oldU,oldV),bb=dot(oldV,oldV),den=aa*bb-ab*ab;
    if(den>1e-18)
    {
        float u=(bb*dot(residual,oldU)-ab*dot(residual,oldV))/den;
        float v=(aa*dot(residual,oldV)-ab*dot(residual,oldU))/den;
        float3 newU=Particles[ids.y].current.xyz-Particles[ids.x].current.xyz;
        float3 newV=Particles[ids.z].current.xyz-Particles[ids.x].current.xyz;
        float3 oldN=SafeUnit(cross(oldU,oldV),vertex.normal),newN=SafeUnit(cross(newU,newV),oldN);
        residual=newU*u+newV*v+newN*dot(residual,oldN);
    }
    else
    {
        float2 gather=GatherProfile(weights.w,lerp(1,Config[4].x,State[0].x));
        residual-=State[5].xyz*dot(residual,State[5].xyz)*(1-gather.y);
    }
    p+=residual;
    if(!all(isfinite(p)) || distance(p,vertex.position)>Config[6].w*State[8].z*1.7)return;
    vertex.position=p;vertex.normal=SafeUnit(normal,vertex.normal);
    vertex.tangent.xyz=SafeUnit(vertex.tangent.xyz-vertex.normal*dot(vertex.tangent.xyz,vertex.normal),vertex.tangent.xyz);
    uint base=row*VERTEX_FLOATS;
    Output[base+POSITION_OFFSET]=vertex.position.x;Output[base+POSITION_OFFSET+1]=vertex.position.y;Output[base+POSITION_OFFSET+2]=vertex.position.z;
    Output[base+NORMAL_OFFSET]=vertex.normal.x;Output[base+NORMAL_OFFSET+1]=vertex.normal.y;Output[base+NORMAL_OFFSET+2]=vertex.normal.z;
    Output[base+TANGENT_OFFSET]=vertex.tangent.x;Output[base+TANGENT_OFFSET+1]=vertex.tangent.y;Output[base+TANGENT_OFFSET+2]=vertex.tangent.z;Output[base+TANGENT_OFFSET+3]=vertex.tangent.w;
}
