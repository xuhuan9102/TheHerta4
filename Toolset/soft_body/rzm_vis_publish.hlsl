// rzm_vis_publish.hlsl
// RZMenu / 3DMigoto / XXMI
//
// Publishes per-object visibility flags (CPU-side ini variables) into the
// GPU buffer consumed by rzm_object_detect.hlsl. One thread-group dispatch,
// once per frame at Present.
//
// Bindings:
//   u0   = ResourceDragObjectVis (R32_FLOAT, array = object count)
//   t120 = IniParams
//
// IniParams layout (from index 130, 4 flags per float4):
//   ObjectVis[i] = 1.0 if IniParams[130 + i/4][i%4] > 0.5 else 0.0

RWBuffer<float> ObjectVis : register(u0);
Texture1D<float4> IniParams : register(t120);

#define VIS_INIPARAM_BASE 130

[numthreads(1, 1, 1)]
void main(uint3 dispatchThreadID : SV_DispatchThreadID)
{
    uint count;
    ObjectVis.GetDimensions(count);
    for (uint i = 0u; i < count; ++i)
        ObjectVis[i] = IniParams[VIS_INIPARAM_BASE + (i >> 2)][i & 3] > 0.5f ? 1.0f : 0.0f;
}
