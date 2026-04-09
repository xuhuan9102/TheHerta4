// --- START OF FILE shapekey_anim_packed_delta_v4_optimized.hlsl ---

// **** ADDITIVE ANIMATION SHADER - OPTIMIZED WITH VERTEX FREQ INDEX LOOKUP ****
// Contributors: Zlevir, Assistant
// Version: 4.0 (Optimized - O(1) FREQ Lookup instead of O(n) branching)
// Description: Uses per-vertex FREQ index buffer for direct lookup, eliminating
//              the need for hundreds of conditional branches per vertex.
//
// PERFORMANCE IMPROVEMENT:
//   - Old: ~200+ conditional branches per vertex per slot
//   - New: 1 buffer lookup + 1 comparison per vertex per slot
//   - Expected speedup: 10-50x depending on GPU architecture
//
// DATA STRUCTURES:
//   - vertex_freq_indices[vertex * MAX_SLOTS + slot] = freq_index (0-11) or 255 (no animation)
//   - FREQ values are still read from IniParams[100-111]
//
// MEMORY OVERHEAD:
//   - Per vertex: MAX_SLOTS * 4 bytes (one uint32 per slot)
//   - For 100k vertices × 5 slots = 2MB additional memory

#define MAX_SLOTS 24
#define MAX_FREQS 12
#define NO_FREQ_INDEX 255

struct VertexAttributes {
    float3 position;
    float3 normal;
    float4 tangent;
};

RWStructuredBuffer<VertexAttributes> rw_buffer : register(u5);
StructuredBuffer<float3> shapekey_pos_deltas[MAX_SLOTS] : register(t51); 
StructuredBuffer<int> shapekey_maps[MAX_SLOTS] : register(t75);

// Per-vertex FREQ index buffer (packed: vertex * MAX_SLOTS + slot)
// Each element is a uint32 (0-11 for FREQ index, 255 for no animation)
StructuredBuffer<uint> vertex_freq_indices : register(t99);

Texture1D<float4> IniParams : register(t120);

// --- [PYTHON-MANAGED BLOCK START] ---
// --- Shared Animation Intensity (per Shape Key Name) ---
// From index 100 onwards
// --- [PYTHON-MANAGED BLOCK END] ---

[numthreads(16, 1, 1)]
void main(uint3 threadID : SV_DispatchThreadID)
{
    uint i = threadID.x;
    
    VertexAttributes output = rw_buffer[i];
    
    float3 total_diff_position = float3(0.0, 0.0, 0.0);

    // --- [PYTHON-MANAGED LOGIC START] ---
    // Optimized: Direct lookup instead of hundreds of if-else branches
    // Each slot reads the FREQ index for this vertex and looks up the weight
    // --- [PYTHON-MANAGED LOGIC END] ---

    output.position += total_diff_position;
    
    rw_buffer[i] = output;
}
// --- END OF FILE shapekey_anim_packed_delta_v4_optimized.hlsl ---
