// Initialize the fixed draw allocation from the visible prefix of native SO.
Buffer<uint> Snapshot : register(t0);
RWBuffer<uint> Output : register(u0);
Texture1D<float4> IniParams : register(t120);
[numthreads(256, 1, 1)]
void main(uint3 id : SV_DispatchThreadID)
{
    uint sourceCount, outputCount;
    Snapshot.GetDimensions(sourceCount);
    Output.GetDimensions(outputCount);
    uint words = (uint)IniParams[115].x;
    if (sourceCount < words || outputCount != words || id.x >= words) return;
    Output[id.x] = Snapshot[id.x];
}
