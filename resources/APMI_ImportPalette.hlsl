struct BoneMatrix
{
    float4 row0;
    float4 row1;
    float4 row2;
    float4 row3;
};

StructuredBuffer<BoneMatrix> NativePalette : register(t2);
StructuredBuffer<uint> DestinationSlots : register(t3);
RWStructuredBuffer<BoneMatrix> MergedPalette : register(u0);

cbuffer PaletteRange : register(b0)
{
    uint firstBone;
    uint boneCount;
    uint useSlotMap;
};

[numthreads(64, 1, 1)]
void main(uint3 threadID : SV_DispatchThreadID)
{
    if (threadID.x < boneCount)
    {
        uint destination = useSlotMap ? DestinationSlots[threadID.x] : firstBone + threadID.x;
        if (destination != 0xffffffff)
            MergedPalette[destination] = NativePalette[threadID.x];
    }
}
