// rzm_shapekey_drive.hlsl
// RZMenu / 3DMigoto / XXMI
//
// Drives per-zone shape-key intensity from hover hit + left-click hold.
// Runs once per frame (after rzm_jiggle_screen_state) and writes per-zone
// intensities into an RWBuffer. The drive is ONLY active in drag system
// mode 1 ("仅命中", hit detection only):
//   - Each zone has its own independent segment: 4 directional slots
//     (0=up, 1=right, 2=down, 3=left) followed by N no-direction stage
//     slots (N = ZoneStageCounts[zone], per-zone independent).
//   - Directional slots follow the latched drag binding: the first hover hit
//     while LMB/X is held binds that zone (level-triggered, same semantics as
//     the drag × panel binding latch); while held, the bound zone keeps being
//     driven by mouse displacement even after the cursor leaves the zone.
//     Releasing LMB/X unlatches on that frame. Unbound zones hold their value.
//   - No-direction stage slots: each press while hovering advances that
//     zone's click count in 0..N cycle (0 = inactive/cleared); the matching
//     stage slot is set to 1 and held until cleared or another stage is
//     active. Click-stage advance still requires a real hit on the press
//     edge — the latch never advances stages outside the zone.
// Releasing LMB/X only unlatches; the driven values themselves hold.
// Buffers are never cleared by mode switches: mode 1 is the only mode that
// can drive them,
// and every other mode (0/2) holds the current values so adjusted
// intensities are inherited when switching back to mode 1.
//
// The shape-key compute shader binds the same buffers as SRVs
// (Buffer<float> ShapeKeyDrive, Buffer<uint> ClickCount) and uses them as the
// weight for shape keys bound to (zone, stage, slot). No CPU
// readback.
//
// Bindings:
//   t67  = ResourceDragPinnedDetectInfo (hover hit + zone id)
//   t68  = ResourceDragShapeKeyZoneStageCounts (R32_UINT, array = capacity;
//          per-zone no-direction stage count)
//   u0   = ResourceDragShapeKeyDrive (R32_FLOAT, array = sum(4+N per zone))
//   u1   = ResourceDragShapeKeyDir   (R32_FLOAT, array = sum(4+N per zone)+1;
//          last = prev press state)
//   u5   = ResourceDragShapeKeyDragLatch (R32_FLOAT, array = 1; drag binding
//          latch: 0 = unbound, otherwise bound zone id + 1 — the +1 encoding
//          makes boot/disarm clears (0.0) read as unbound. Kept in its own
//          resource so a disarm clear never wipes the prev-press slot.
//          Maintained by this CS, also read by rzm_shapekey_var_sync for its
//          ZoneActive flags)
//   u2   = ResourceDragShapeKeyClickCount (R32_UINT, array = capacity)
//   u3   = ResourceDragShapeKeyActiveDir (R32_UINT, array = capacity;
//          dominant direction per zone 0=up 1=right 2=down 3=left)
//   u4   = ResourceDragShapeKeyClickCountF (R32_FLOAT, array = capacity;
//          float mirror of ClickCount for CPU store export)
//   t120 = IniParams
//
// IniParams:
//   [77].z = drag system mode (0=off, 1=hit only, 2=hit + drag)
//   [77].w = LMB held (from $ssmtdrag_lmb_down_<ns>, active in every mode)
//   [78].x = X held (from $ssmtdrag_x_down_<ns>; original design treats X as LMB)
//   [79].x = mouse Y displacement (from $ssmtdrag_shapekey_dy_<ns>, px/frame)
//   [79].y = mouse X displacement (from $ssmtdrag_shapekey_dx_<ns>, px/frame)
//   [80].x = mouse displacement sensitivity (per-px strength delta)
//   [80].y = cold-start seed pending flag (1 = seed click counts from
//          export variables this dispatch; set at boot, cleared after run)
//   [81].x = seed entry count (0 = no seeding)
//   [82+i] = seed entry i: x = zone id, y = export variable value

RWBuffer<float> ShapeKeyDrive       : register(u0);
RWBuffer<float> ShapeKeyDir         : register(u1);
RWBuffer<uint> ClickCount           : register(u2);
RWBuffer<uint> ActiveDir            : register(u3);
RWBuffer<float> ClickCountF         : register(u4);
RWBuffer<float> DragLatch           : register(u5);
StructuredBuffer<float4> PinnedDetectInfo : register(t67);
Buffer<uint> ZoneStageCounts        : register(t68);
Texture1D<float4> IniParams         : register(t120);

#define DRIVE_PARAMS  IniParams[77]

uint ClampZoneID(float zoneValue, uint zoneCount)
{
    return min((uint)max(round(zoneValue), 0.0), max(zoneCount, 1u) - 1u);
}

[numthreads(1, 1, 1)]
void main(uint3 dispatchThreadID : SV_DispatchThreadID)
{
    uint zoneCount;
    ClickCount.GetDimensions(zoneCount);
    if (zoneCount == 0u)
        return;
    uint driveSlots;
    ShapeKeyDrive.GetDimensions(driveSlots);
    uint dirSlots;
    ShapeKeyDir.GetDimensions(dirSlots);
    // 按区域独立段计算总槽数：每区域 4 方向槽 + 该区域档位数 N 个无方向槽
    uint lastSlot = 0u;
    for (uint z = 0u; z < zoneCount; ++z)
        lastSlot += 4u + max(1u, ZoneStageCounts[z]);
    if (driveSlots < lastSlot || dirSlots < lastSlot + 1u)
        return;
    uint latchSlots;
    DragLatch.GetDimensions(latchSlots);
    if (latchSlots < 1u)
        return;

    float mode = DRIVE_PARAMS.z;
    float mouseDy = IniParams[79].x;
    float mouseDx = IniParams[79].y;
    float mouseSensitivity = max(IniParams[80].x, 0.0001);
    bool triggerHeld = DRIVE_PARAMS.w > 0.5 || IniParams[78].x > 0.5;

    bool wasHeld = ShapeKeyDir[lastSlot] > 0.5;
    bool pressed = triggerHeld && !wasHeld;

    // 4 方向相邻混合权重（对齐 UI 构造器 directionCount=4 正交锚点分解）
    // 0=上 1=右 2=下 3=左；位移向量归一化后与锚点点积取正
    float moveLen = length(float2(mouseDx, mouseDy));
    float4 dirWeight = float4(0.0, 0.0, 0.0, 0.0);
    if (moveLen > 1e-4)
    {
        float2 v = float2(mouseDx, mouseDy) / moveLen;
        dirWeight = float4(max(v.y, 0.0), max(v.x, 0.0), max(-v.y, 0.0), max(-v.x, 0.0));
    }
    uint activeDir = 0u;
    float bestW = dirWeight.x;
    for (uint d = 1u; d < 4u; ++d)
    {
        if (dirWeight[d] > bestW) { bestW = dirWeight[d]; activeDir = d; }
    }

    // 冷启动播种：缓冲随游戏关闭销毁、persist 变量仍有上次会话的值。
    // boot 清零后的首个驱动帧，把导出变量值写回点击计数（含浮点镜像与无方向档位槽），
    // 使物体切换选项 / 档位形态键以变量为准恢复。
    uint seedCount = (uint)IniParams[81].x;
    if (IniParams[80].y > 0.5 && seedCount > 0u)
    {
        for (uint s = 0u; s < seedCount; ++s)
        {
            float4 seedEntry = IniParams[82u + s];
            uint seedZone = (uint)max(round(seedEntry.x), 0.0);
            if (seedZone >= zoneCount)
                continue;
            uint zoneStageCap = max(1u, ZoneStageCounts[seedZone]);
            uint seeded = (uint)clamp(round(seedEntry.y), 0.0, (float)zoneStageCap);
            ClickCount[seedZone] = seeded;
            ClickCountF[seedZone] = (float)seeded;
            if (seeded >= 1u)
            {
                // 重算该区域段基址（与主循环同构的前缀和），点亮对应档位 one-hot 槽
                uint seedBase = 0u;
                for (uint zz = 0u; zz < seedZone; ++zz)
                    seedBase += 4u + max(1u, ZoneStageCounts[zz]);
                uint oneHotIdx = seedBase + 4u + (seeded - 1u);
                if (oneHotIdx < driveSlots)
                    ShapeKeyDrive[oneHotIdx] = 1.0;
            }
        }
    }

    // 仅“仅命中”模式（1）下驱动；其余模式（0/2）不清零、不驱动，保持当前数值。
    // 播种必须在此分支之前完成：默认模式 2 也需要从 persist 变量恢复点击状态。
    // 模式切出即解除绑定：latch 只在模式 1 存活，防止陈旧绑定跨模式残留。
    if (mode != 1.0)
    {
        // 仍更新上一帧按键状态槽，保证切回模式 1 时按下沿检测准确
        ShapeKeyDir[lastSlot] = triggerHeld ? 1.0 : 0.0;
        DragLatch[0] = 0.0;
        return;
    }

    // 命中判定（不含按键状态）：供绑定/按下沿判定
    float4 detected = PinnedDetectInfo[0u];
    bool realHit = detected.x >= 0.0 && detected.y < 1e30
        && abs(PinnedDetectInfo[1u].w - 7.0) < 0.5;
    uint hoverZone = ClampZoneID(PinnedDetectInfo[7u].w, zoneCount);

    // 拖拽绑定锁存（与拖拽×面板联动的锁存变量同构）：
    // 按住 LMB/X 期间首次命中即绑定（level-triggered，滑入已按住同样绑定）；
    // 绑定后光标移出区域不丢，继续由鼠标位移驱动；松开 LMB/X 的当帧解除。
    // 编码：0 = 未绑定（boot/失臂清零值即未绑定，评审 F1），否则存 区域id+1。
    float latchValue = DragLatch[0];
    int boundZone = (latchValue > 0.5) ? (int)floor(latchValue + 0.5) - 1 : -1;
    if (triggerHeld)
    {
        if (boundZone < 0 && realHit)
            boundZone = (int)hoverZone;
        if (boundZone >= (int)zoneCount)
            boundZone = -1;  // 区域布局变更防御：陈旧绑定作废
    }
    else
    {
        boundZone = -1;
    }
    DragLatch[0] = (float)(boundZone + 1);

    // 按下沿判定保留原语义：需真实命中 + triggerHeld
    bool hasHit = realHit && triggerHeld;

    // 按下瞬间：推进该区域点击档位（0..zoneStageCount 循环；0=未激活/清空）
    if (pressed && hasHit)
    {
        uint oldStage = ClickCount[hoverZone];
        uint zoneStageCount = max(1u, ZoneStageCounts[hoverZone]);
        uint newStage = oldStage >= zoneStageCount ? 0u : oldStage + 1u;
        ClickCount[hoverZone] = newStage;
    }
    ShapeKeyDir[lastSlot] = triggerHeld ? 1.0 : 0.0;

    uint runningBase = 0u;
    for (uint zone = 0u; zone < zoneCount; ++zone)
    {
        uint zoneStageCount = max(1u, ZoneStageCounts[zone]);
        uint zoneBase = runningBase;
        runningBase += 4u + zoneStageCount;
        bool zoneHit = hasHit && zone == hoverZone;
        bool zonePressed = zoneHit && pressed;
        // 位移驱动/主导方向只看锁存绑定：绑定后光标移出区域不丢控
        bool zoneDriven = boundZone >= 0 && zone == (uint)boundZone;
        uint activeStage = ClickCount[zone];
        // 浮点镜像：供 CPU store 回读导出点击次数（R32_UINT 直接 store 无格式保证）
        ClickCountF[zone] = (float)activeStage;
        // 方向槽：忽略档位，绑定该区域且按住时由鼠标位移驱动；否则保持
        for (uint dir = 0u; dir < 4u; ++dir)
        {
            uint idx = zoneBase + dir;
            float current = ShapeKeyDrive[idx];
            float next = current;
            if (zoneDriven)
            {
                // 位移驱动：该方向净位移（同向 +、对向 -）× 灵敏度，
                // 向上时“上”增“下”减，向下时反之，左右同理
                float net = dirWeight[dir] - dirWeight[(dir + 2u) % 4u];
                next = clamp(current + net * moveLen * mouseSensitivity, 0.0, 1.0);
            }
            ShapeKeyDrive[idx] = next;
        }
        // 无方向档位槽：仅当前真实命中区域按下该档位时置 1 并保持；非活动/清空时归 0。
        // 必须同时满足按下沿真实命中（zonePressed），锁存绑定不参与档位推进，
        // 否则绑定期间在其他位置按下时会误置本区域槽
        for (uint stage = 1u; stage <= zoneStageCount; ++stage)
        {
            uint ndIdx = zoneBase + 4u + (stage - 1u);
            if (activeStage == stage && zonePressed)
                ShapeKeyDrive[ndIdx] = 1.0;
            else if (activeStage != stage)
                ShapeKeyDrive[ndIdx] = 0.0;
        }
        if (zoneDriven)
            ActiveDir[zone] = activeDir;
    }
}
