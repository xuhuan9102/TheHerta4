#include "constants.hlsli"
#include "common.hlsli"
Buffer<float4> Rest : register(t75);
Buffer<uint> HemMap : register(t116);
Buffer<float4> FrameState : register(t77);
Buffer<float4> Anchors : register(t78);
StructuredBuffer<Particle> Snapshot : register(t76);
RWStructuredBuffer<Particle> Particles : register(u0);
RWBuffer<uint> Heads : register(u2);
RWBuffer<uint> Links : register(u3);
uint Hash(int3 p){return ((asuint(p.x)*73856093u)^(asuint(p.y)*19349663u)^(asuint(p.z)*83492791u))&(HASH_SIZE-1);}
[numthreads(64,1,1)]void main(uint3 id:SV_DispatchThreadID)
{
    uint i=id.x,phase=(uint)IniParams[116].x;
    if(phase==10){if(i<HASH_SIZE)Heads[i]=0xffffffffu;return;}
    if(FrameState[23].w<.5)return;
    uint snapshotCount,stride;Snapshot.GetDimensions(snapshotCount,stride);
    if(i>=PARTICLE_COUNT || snapshotCount!=PARTICLE_COUNT || stride!=32)return;
    float scale=FrameState[8].z,cell=max(Config[5].y*scale,.00001),thickness=Config[2].w*scale;
    Particle particle=Snapshot[i];int3 bucket=(int3)floor(particle.current.xyz/cell);
    if(phase==11){uint old;InterlockedExchange(Heads[Hash(bucket)],i,old);Links[i]=old;return;}
    if(phase!=12)return;
    if(HemMap[i]<HEM_COUNT || Rest[i*2].w<.5){Particles[i]=particle;return;}
    float3 correction=0;float count=0;
    [loop]for(int z=-1;z<=1;++z)[loop]for(int y=-1;y<=1;++y)[loop]for(int x=-1;x<=1;++x)
    {
        int3 at=bucket+int3(x,y,z);uint other=Heads[Hash(at)],walk=0;
        [loop]while(other<PARTICLE_COUNT && walk<64)
        {
            float3 q=Snapshot[other].current.xyz;
            if(other!=i && all((int3)floor(q/cell)==at) && distance(Rest[i*2].xyz,Rest[other*2].xyz)>Config[5].y*2)
            {
                bool contact=false;
                if(Rest[i*2+1].w*Rest[other*2+1].w<0)
                {
                    uint a=asuint(Anchors[i*3+2].x),b=asuint(Anchors[other*3+2].x),n;BodyContacts.GetDimensions(n);
                    contact=(a<n && BodyContacts[a]!=0)||(b<n && BodyContacts[b]!=0);
                }
                float3 delta=particle.current.xyz-q;float len=length(delta);
                if(!contact && len<thickness && len>1e-8){correction+=delta/len*(thickness-len)*.5;count+=1;}
            }
            other=Links[other];++walk;
        }
    }
    correction/=max(count,1);particle.current.xyz+=correction;particle.previous.xyz+=correction;Particles[i]=particle;
}
