#include "ParticleConfig.hlsl"
Texture1D<float4> IniParams:register(t120);
#include "Slots.hlsl"
struct FluidParticle{float4 positionAge;float4 velocityAlive;float4 predictedLambda;float4 densityBirth;};
StructuredBuffer<FluidParticle> primary:register(t100);
StructuredBuffer<FluidParticle> secondary:register(t101);
RWBuffer<float> activity:register(u0);
groupshared uint2 masks[64];

// Two exact integer masks: emitter owners for CPU scheduling, slots for GPU
// retirement. This is sampled by the CPU only while an emitter is draining.
[numthreads(64,1,1)]void main(uint lane:SV_GroupIndex)
{
    uint owners=0,liveSlots=0;
    [loop]for(uint i=lane;i<PRIMARY_COUNT+SPLASH_COUNT;i+=64){
        float alive;uint slot;
        if(i<PRIMARY_COUNT){alive=primary[i].velocityAlive.w;slot=primarySlot(i);}
        else{alive=secondary[i-PRIMARY_COUNT].velocityAlive.w;slot=(uint)round(alive)-1;}
        if(alive>.5 && slot<SLOT_COUNT && slotLive(slot) && slotEmitterValid(slot)){
            liveSlots|=1u<<slot;owners|=1u<<slotEmitterId(slot);
        }
    }
    masks[lane]=uint2(owners,liveSlots);
    GroupMemoryBarrierWithGroupSync();
    [unroll]for(uint width=32;width>0;width>>=1){
        if(lane<width)masks[lane]|=masks[lane+width];
        GroupMemoryBarrierWithGroupSync();
    }
    if(lane==0){activity[0]=(float)masks[0].x;activity[1]=(float)masks[0].y;}
}
