// =========================================================
// gather_bones_cs.hlsl
// Local palette gather:
//   t0 = Global FakeT0 / BoneStore SRV
//   t2 = LocalPalette buffer, palette[localBone] = globalBone
//   t3.x = local_bone_count
//
// Global palette layout:
//   current:  3 + globalBone*3 + {0,1,2}
//   previous: 100000 + 3 + globalBone*3 + {0,1,2}
//
// Local gathered palette layout:
//   current:  3 + localBone*3 + {0,1,2}
//   previous: 1024 + 3 + localBone*3 + {0,1,2}
// =========================================================

StructuredBuffer<uint4> GlobalFakeT0 : register(t0);
Buffer<uint> LocalPalette            : register(t2);
Buffer<float> LocalPaletteMeta       : register(t3);

RWStructuredBuffer<uint4> LocalFakeT0_UAV : register(u1);

static const uint GLOBAL_RESERVED_ROWS = 3;
static const uint GLOBAL_PREVIOUS_ROW_OFFSET = 100000;
static const uint LOCAL_PREVIOUS_ROW_OFFSET = 1024;

[numthreads(64, 1, 1)]
void main(uint3 tid : SV_DispatchThreadID)
{
    uint local_row = tid.x;
    uint local_bone_count = (uint)LocalPaletteMeta[0];
    uint rows_to_copy = local_bone_count * 3;
    if (local_row >= rows_to_copy)
    {
        return;
    }

    uint local_bone = local_row / 3;
    uint row_in_bone = local_row % 3;
    uint global_bone = LocalPalette[local_bone];

    uint src_current_row = GLOBAL_RESERVED_ROWS + global_bone * 3 + row_in_bone;
    uint src_previous_row = GLOBAL_PREVIOUS_ROW_OFFSET + GLOBAL_RESERVED_ROWS + global_bone * 3 + row_in_bone;
    uint dst_current_row = GLOBAL_RESERVED_ROWS + local_bone * 3 + row_in_bone;
    uint dst_previous_row = LOCAL_PREVIOUS_ROW_OFFSET + GLOBAL_RESERVED_ROWS + local_bone * 3 + row_in_bone;

    LocalFakeT0_UAV[dst_current_row] = GlobalFakeT0[src_current_row];
    LocalFakeT0_UAV[dst_previous_row] = GlobalFakeT0[src_previous_row];
}
