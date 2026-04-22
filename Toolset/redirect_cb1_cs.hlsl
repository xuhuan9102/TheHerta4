// =========================================================
// redirect_cb1_cs.hlsl
// Redirect the VS palette window to the shared local gathered buffer.
//
// Local palette layout:
//   current:  3 + localBone*3 + {0,1,2}
//   previous: 1024 + 3 + localBone*3 + {0,1,2}
//
// VS still reads:
//   base + 3 + localBone*3
// so the local base values are:
//   cb1[5].x = 0
//   cb1[5].y = 1024
// =========================================================

StructuredBuffer<uint4> DumpedCB1 : register(t0);

RWStructuredBuffer<uint4> FakeCB1_UAV : register(u0);

static const uint LOCAL_PREVIOUS_ROW_OFFSET = 1024;

[numthreads(1024, 1, 1)]
void main(uint3 tid : SV_DispatchThreadID)
{
    uint id = tid.x;
    if (id >= 4096)
    {
        return;
    }

    uint4 cb_data = DumpedCB1[id];
    if (id == 5)
    {
        cb_data.x = 0;
        cb_data.y = LOCAL_PREVIOUS_ROW_OFFSET;
    }

    FakeCB1_UAV[id] = cb_data;
}
