// rzm_shapekey_var_sync.hlsl
// RZMenu / 3DMigoto / XXMI
//
// Syncs shape-key export variables into the ShapeKeyDrive buffer.
// Runs once per frame (unconditional, independent of drag system mode) so
// the variable-driven path and the drag-driven path share one source of
// truth.
//
// Background: shape keys bound to a drag zone read their weight from
// ShapeKeyDrive[slot] only (see rzm_shapekey_drive.hlsl and the anim CS
// FREQ defines); their export variable ($Freq_*) never reaches the shader.
// Without this pass, changing the variable leaves the buffer stale and the
// next drag resumes from the stale value (visible snap back to 0).
//
// Semantics (value arbitration, variable-first):
//   - The variable changed this frame (hotkey / animation driver) -> the
//     variable owns the buffer and is written immediately, even while a
//     drag is active (variable always wins).
//   - CPU/GPU arbitration uses a mode handshake in IniParams[90..98]:
//     mode 1 suppresses a CPU readback echo; mode 2 force-pushes a changed
//     variable until the delayed store readback confirms the same value.
//   - ZoneActive mirrors the drag drive CS's latched binding (single source
//     of truth: the ResourceDragShapeKeyDragLatch slot, see below) and is
//     consumed by the CPU
//     readback to decide which side owns the variable. The bound zone stays
//     active while LMB/X is held even after the cursor leaves the hit zone;
//     the flag falls once the drive CS observes the release edge.
// GPU->CPU synchronization is emitted by the generated
// CommandListDragShapeKeyVarReadback section. It reads ZoneActive and the
// drive slot with the loader's direct-resource store syntax (without ref).
// The pending/ack settle handshake absorbs store latency before pulls resume,
// so a fresh hotkey value is never clobbered by a stale buffer read.
//
// Bindings:
//   t67  = ResourceDragPinnedDetectInfo (hover hit + zone id; retained for
//          binding compatibility — the hit test itself moved into the drive
//          CS's latch, this CS no longer re-derives it)
//   t69  = ResourceDragShapeKeyVarSyncMap (R32G32B32A32_UINT, one uint4 per
//          binding: x = drive slot, y = zone id, z = nd_stage or 0xFFFFFFFF,
//          w = reserved; baked at export)
//   u0   = ResourceDragShapeKeyDrive (shared with rzm_shapekey_drive)
//   u1   = ResourceDragShapeKeyClickCount (shared with rzm_shapekey_drive)
//   u2   = ResourceDragShapeKeyVarPrev (R32_FLOAT, array = binding count;
//          previous variable values, boot-cleared to 0)
//   u3   = ResourceDragShapeKeyClickCountF (R32_FLOAT, array = zone capacity;
//          float mirror kept coherent with ClickCount for CPU store export)
//   u4   = ResourceDragShapeKeyZoneActive (R32_FLOAT, array = zone capacity;
//          per-zone drag-active flags, mirrored from the latch every frame)
//   u5   = ResourceDragShapeKeyDragLatch (shared latch resource maintained by
//          rzm_shapekey_drive; single slot: 0 = unbound, otherwise bound
//          zone id + 1 — boot/disarm clears (0.0) read as unbound)
//   t120 = IniParams
//
// IniParams (packed 4 variables per float4, from index 81; 76-80 are used
// by the drive CS, 100+ by the shape-key anim CS):
//   [81 + i/4][i%4] = current value of binding i's export variable
//   [90 + i/4][i%4] = CPU/GPU arbitration mode of binding i
//                     (0 = normal, 1 = suppress readback echo,
//                      2 = force-push until delayed readback acknowledges)
// IniParams[75] gate inputs (fixed below the drive CS range) are still fed by
// the generator but no longer consulted here: drag-active arbitration follows
// the drive CS latch (single source of truth), not a per-frame hit retest.

RWBuffer<float> ShapeKeyDrive       : register(u0);
RWBuffer<uint>  ClickCount          : register(u1);
RWBuffer<float> VarSyncPrev         : register(u2);
RWBuffer<float> ClickCountF         : register(u3);
RWBuffer<float> ZoneActive          : register(u4);
RWBuffer<float> DragLatch           : register(u5);
Buffer<uint4>   VarSyncMap          : register(t69);
StructuredBuffer<float4> PinnedDetectInfo : register(t67);
Texture1D<float4> IniParams         : register(t120);

#define VAR_SYNC_INIPARAM_BASE 81
#define VAR_SYNC_MODE_BASE 90

[numthreads(1, 1, 1)]
void main(uint3 dispatchThreadID : SV_DispatchThreadID)
{
    uint count;
    VarSyncMap.GetDimensions(count);
    if (count == 0u)
        return;
    uint driveSlots;
    ShapeKeyDrive.GetDimensions(driveSlots);
    uint clickSlots;
    ClickCount.GetDimensions(clickSlots);
    uint clickFloatSlots;
    ClickCountF.GetDimensions(clickFloatSlots);
    uint prevSlots;
    VarSyncPrev.GetDimensions(prevSlots);
    if (clickSlots == 0u || clickFloatSlots < clickSlots || prevSlots < count)
        return;

    // 拖拽激活标志（每区域）：直接镜像驱动 CS 的绑定锁存（单一事实源）。
    // 驱动 CS 维护锁存：按住 LMB/X 命中即绑定、绑定期间移出区域不丢、
    // 松开当帧写 0（=未绑定编码）解除；失臂（松 Alt/undraw/模式 0）由生成器
    // else 分支整清锁存资源兜底。本 CS 每帧照抄为 ZoneActive，门控 CPU 回读
    // 拉取方向；帧序上驱动 CS 先于本 CS 运行（[Present] 段内 PinDetected →
    // VarSync），因此绑定/释放沿当帧生效。
    uint latchSlots;
    DragLatch.GetDimensions(latchSlots);
    float latchValue = latchSlots >= 1u ? DragLatch[0] : 0.0;
    int boundZone = (latchValue > 0.5) ? (int)floor(latchValue + 0.5) - 1 : -1;
    if (boundZone >= (int)clickSlots)
        boundZone = -1;  // 区域布局变更防御：陈旧绑定作废
    for (uint z = 0u; z < clickSlots; ++z)
        ZoneActive[z] = (boundZone >= 0 && z == (uint)boundZone) ? 1.0 : 0.0;

    for (uint i = 0u; i < count; ++i)
    {
        float raw = IniParams[VAR_SYNC_INIPARAM_BASE + (i >> 2)][i & 3];
        float syncMode = IniParams[VAR_SYNC_MODE_BASE + (i >> 2)][i & 3];
        bool forcePush = syncMode > 1.5;
        if (!forcePush)
        {
            if (abs(raw - VarSyncPrev[i]) <= 1e-6)
                continue;
        }
        VarSyncPrev[i] = raw;
        // mode=1 表示 CPU 刚从缓冲拉取，跳过回声；mode=2 表示变量值仍
        // 等待 store 确认，必须绕过 prev 去重持续推送，直到 CPU 观察到追平。
        if (syncMode > 0.5 && syncMode < 1.5)
            continue;
        uint4 mapping = VarSyncMap[i];
        uint slot = mapping.x;
        uint zone = mapping.y;
        uint ndStage = mapping.z;
        float v = clamp(raw, 0.0, 1.0);
        if (slot < driveSlots)
            ShapeKeyDrive[slot] = v;
        if (ndStage != 0xFFFFFFFFu && zone < clickSlots)
        {
            // 无方向档位的读取带 ClickCount 门控：变量非 0 时打开对应档位，
            // 归 0 时仅在当前活动档位就是本档位才清空（不干扰点击切出的其他档位）
            if (v > 1e-6)
                ClickCount[zone] = ndStage;
            else if (ClickCount[zone] == ndStage)
                ClickCount[zone] = 0u;
            ClickCountF[zone] = (float)ClickCount[zone];
        }
    }
}
