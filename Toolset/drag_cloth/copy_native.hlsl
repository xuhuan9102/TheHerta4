// Copy the visible prefix without relying on loader-specific Region semantics.
Buffer<uint> Snapshot : register(t0);
RWBuffer<uint> Output : register(u0);
[numthreads(256,1,1)]
void main(uint3 id : SV_DispatchThreadID)
{
    uint sourceCount,outputCount;
    Snapshot.GetDimensions(sourceCount);Output.GetDimensions(outputCount);
    if(sourceCount<COPY_WORDS || outputCount!=COPY_WORDS || id.x>=COPY_WORDS)return;
    Output[id.x]=Snapshot[id.x];
}
