// =========================================================
// record_bones_dynamic_cs.hlsl
// Dynamic palette capture:
//   t2.x = global_bone_base
//   t2.y = bone_count
// Source palette layout:
//   base + 3 + localBone*3 + {0,1,2}
// Destination palette layout:
//   3 + globalBone*3 + {0,1,2}
// =========================================================

StructuredBuffer<uint4> OriginalT0 : register(t0);
StructuredBuffer<uint4> DumpedCB1  : register(t1);
Buffer<float> BoneMeta            : register(t2);

RWStructuredBuffer<uint4> FakeT0_UAV : register(u1);

static const uint GLOBAL_RESERVED_ROWS = 3;
static const uint PREVIOUS_ROW_OFFSET = 100000;

[numthreads(64, 1, 1)]
void main(uint3 tid : SV_DispatchThreadID)
{
    uint local_row = tid.x;
    uint global_bone_base = (uint)BoneMeta[0];
    uint bone_count = (uint)BoneMeta[1];
    uint rows_to_copy = bone_count * 3;
    if (local_row >= rows_to_copy)
    {
        return;
    }

    uint src_current_base = DumpedCB1[5].x;
    uint src_previous_base = DumpedCB1[5].y;
    uint dst_row_base = GLOBAL_RESERVED_ROWS + global_bone_base * 3;

    uint src_current_row = src_current_base + GLOBAL_RESERVED_ROWS + local_row;
    uint src_previous_row = src_previous_base + GLOBAL_RESERVED_ROWS + local_row;
    uint dst_current_row = dst_row_base + local_row;
    uint dst_previous_row = PREVIOUS_ROW_OFFSET + dst_row_base + local_row;

    FakeT0_UAV[dst_current_row] = OriginalT0[src_current_row];
    FakeT0_UAV[dst_previous_row] = OriginalT0[src_previous_row];
}
