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
StructuredBuffer<Blend> SkinWeights : register(t97);
Buffer<float4> FrozenDisplay : register(t99);
Buffer<float4> Skeleton1 : register(t2);
Buffer<float4> Skeleton2 : register(t3);
RWBuffer<float> Output : register(u0);

Buffer<float4> DisplayNormals : register(t74);
float3 ParticleNormal(uint particle){return DisplayNormals[particle].xyz;}

[numthreads(256,1,1)]void main(uint3 dispatch:SV_DispatchThreadID)
{
    uint row=dispatch.x,count,particles,pstride,sourceCount,sourceStride;
    if(PaletteDecision[0].w<.5 || State[23].w<.5 || GarmentAmount(State[0].xy)<1e-5)return;
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
    float4 frozen=FrozenDisplay[row];
    if(abs(frozen.w)==State[2].x && frozen.w!=0)
    {
        if(frozen.w>0)
        {
            float3 u=Particles[ids.y].current.xyz-Particles[ids.x].current.xyz;
            float3 v=Particles[ids.z].current.xyz-Particles[ids.x].current.xyz;
            residual=u*frozen.x+v*frozen.y+SafeUnit(cross(u,v),vertex.normal)*frozen.z;
        }
        else
        {
            Blend skin=SkinWeights[particle];float3 value=0;bool second=PaletteDecision[0].x>1.5;
            [unroll]for(uint bone=0;bone<4;++bone)
            {
                uint at=skin.indices[bone]*3;float4 a,b,c;
                if(second){a=Skeleton2[at];b=Skeleton2[at+1];c=Skeleton2[at+2];}
                else{a=Skeleton1[at];b=Skeleton1[at+1];c=Skeleton1[at+2];}
                value+=skin.weights[bone]*float3(dot(a.xyz,frozen.xyz),dot(b.xyz,frozen.xyz),dot(c.xyz,frozen.xyz));
            }
            residual=value;
        }
    }
    else if(den>1e-18)
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
        float2 gather=MaterialProfile(weights.w,State[0].xy);
        residual-=State[5].xyz*dot(residual,State[5].xyz)*(1-gather.y);
    }
    float originalResidual=length(vertex.position-origin);
    float actualResidual=length(residual);
    if(!isfinite(actualResidual))residual=vertex.position-origin;
    else if(actualResidual>originalResidual*1.7+0.001)
        residual*=(originalResidual*1.7+0.001)/max(actualResidual,0.000001);
    p+=residual;
    float travel=Sliding()?abs(1-Config[4].x):0;
    if(!all(isfinite(p)) || distance(p,vertex.position)>Config[6].w*State[8].z*(1.7+travel))return;
    vertex.position=p;vertex.normal=SafeUnit(normal,vertex.normal);
    vertex.tangent.xyz=SafeUnit(vertex.tangent.xyz-vertex.normal*dot(vertex.tangent.xyz,vertex.normal),vertex.tangent.xyz);
    uint base=row*VERTEX_FLOATS;
    Output[base+POSITION_OFFSET]=vertex.position.x;Output[base+POSITION_OFFSET+1]=vertex.position.y;Output[base+POSITION_OFFSET+2]=vertex.position.z;
    Output[base+NORMAL_OFFSET]=vertex.normal.x;Output[base+NORMAL_OFFSET+1]=vertex.normal.y;Output[base+NORMAL_OFFSET+2]=vertex.normal.z;
    Output[base+TANGENT_OFFSET]=vertex.tangent.x;Output[base+TANGENT_OFFSET+1]=vertex.tangent.y;Output[base+TANGENT_OFFSET+2]=vertex.tangent.z;Output[base+TANGENT_OFFSET+3]=vertex.tangent.w;
}
