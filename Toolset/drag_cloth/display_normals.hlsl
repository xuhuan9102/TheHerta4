#include "constants.hlsli"
#include "common.hlsli"
StructuredBuffer<Particle> Particles : register(t76);
Buffer<uint> Faces : register(t79);
Buffer<uint> FaceAdjacency : register(t80);
Buffer<uint> Representatives : register(t85);
Buffer<float4> State : register(t77);
Buffer<float4> PaletteDecision : register(t96);
RWBuffer<float4> DisplayNormals : register(u0);

// Use the same adjacency order as the display pass, once per particle.
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

[numthreads(64,1,1)]void main(uint3 dispatch:SV_DispatchThreadID)
{
    uint i=dispatch.x,count,particles,stride,sourceCount,sourceStride;
    if(PaletteDecision[0].w<.5 || State[23].w<.5 || GarmentAmount(State[0].xy)<1e-5)return;
    DisplayNormals.GetDimensions(count);Particles.GetDimensions(particles,stride);Source.GetDimensions(sourceCount,sourceStride);
    if(count!=PARTICLE_COUNT || particles!=PARTICLE_COUNT || stride!=32 ||
       sourceCount!=(uint)Config[0].y || sourceStride!=VERTEX_STRIDE || i>=PARTICLE_COUNT)return;
    DisplayNormals[i]=float4(ParticleNormal(i),0);
}
