#Requires -Version 5.1
<#
.SYNOPSIS
    把流体探针受影响的入口着色器缓存（.bin）移进回收站，强制 3Dmigoto 重新编译。

.DESCRIPTION
    【为什么必须手工清】

    3Dmigoto 的着色器缓存键只认「入口 hlsl 文件」的 FILETIME，`#include` 进来的头文件不参与算键。
    流体探针里 FluidSolver.hlsl 是头（被 FluidPredict / FluidDensity / FluidCorrect / FluidFinalize /
    FluidState 等入口 #include），ForceField.hlsl 又是 FluidSolver.hlsl 的头。
    所以：改这两个头文件（或任何被 include 的头）不会让任何入口 .bin 失效——缓存键没变。
    结果是游戏直接加载旧编译结果，改动看不见，而且不报错。

    唯一可靠做法：把受影响入口的旧 .bin 移走，让 3Dmigoto 现场重编译。

    【什么时候必须跑】

    任何一次改了 `Toolset/fluid/fluid/*.hlsl` 或 `Toolset/fluid/surface/*.hlsl` 之后、进游戏之前：
      1) 先 `-DryRun` 确认命中的文件列表；
      2) 再不带 `-DryRun` 真跑一遍（文件进回收站，可还原）；
      3) 看最后打印的 `Remaining .bin under <ModDir>: N`——剩下的是本地化/其它 shader 的 bin，属正常。

    【匹配规则】

    按入口名通配，不区分大小写：basename 以 `<入口名>.` 开头且以 `.bin` 结尾。
    默认入口名（13 个）：
      FluidPredict / FluidDensity / FluidCorrect / FluidFinalize / FluidState /
      FluidComposite / FluidCompositeSampled /
      AssignSlots / FluidSurface / FluidSmoothH / FluidSmoothV / SplashSpawn / SplashUpdate。
    后 6 个来自 2026-11-03 的「最小必删 14 个 bin」名单（BLANK.md §4.3）：它们依赖
    ForceField.hlsl / Slots.hlsl / FluidSolver.hlsl 等被 include 的头，旧脚本没覆盖。
    注意 `FluidComposite.` **不**前缀匹配 `FluidCompositeSampled.`，所以 Sampled 单列一条。
    可用 `-EntryNames` 覆盖默认表（例如只清某一族）。
    例：`FluidPredict.cs_5_0.bin`、`FluidCompositeSampled.cs_5_0.bin`。

    【用法示例】

        pwsh -File tools\clear-fluid-bins.ps1 -ModDir "D:\Games\HI3\ShaderFixes" -DryRun
        pwsh -File tools\clear-fluid-bins.ps1 -ModDir "D:\Games\HI3\ShaderFixes"

    -ModDir 必填，脚本不猜任何默认路径（缓存目录随游戏/加载器配置变化，猜错会清错东西）。
    删除走回收站（Microsoft.VisualBasic.FileIO.FileSystem.DeleteFile + SendToRecycleBin），
    不是 Remove-Item，随时可还原。加载不了 Microsoft.VisualBasic 时脚本明确报错并退出，不会退化成硬删除。

.NOTES
    退出码：0 = 正常（含「没有命中」）；1 = 参数/目录错误或回收站 API 不可用；2 = 有文件移走失败。
    本文件为 UTF-8；中文输出在 PowerShell 5.1 下可能显示为乱码（不影响逻辑），建议用 pwsh 7 运行。
#>

param(
    [string]$ModDir,
    [switch]$DryRun,
    [string[]]$EntryNames
)

# 默认受影响入口名。匹配时统一追加 '.' 作为前缀：`FluidComposite.` 只命中
# FluidComposite.*.bin，所以 Sampled 变体必须单列一条（它的文件名是
# FluidCompositeSampled.*.bin，不以 'FluidComposite.' 开头）。
# 后 6 个（AssignSlots/FluidSurface/FluidSmoothH/FluidSmoothV/SplashSpawn/SplashUpdate）
# 来自 2026-11-03「最小必删 14 个 bin」名单，旧版 7 个入口覆盖不到。
$DefaultEntryNames = @(
    'FluidPredict',
    'FluidDensity',
    'FluidCorrect',
    'FluidFinalize',
    'FluidState',
    'FluidComposite',
    'FluidCompositeSampled',
    'AssignSlots',
    'FluidSurface',
    'FluidSmoothH',
    'FluidSmoothV',
    'SplashSpawn',
    'SplashUpdate'
)

# -EntryNames 为空时用默认表；显式传入则只清传入的入口。
if ($null -eq $EntryNames -or $EntryNames.Count -eq 0) {
    $EntryNames = $DefaultEntryNames
}

function Test-IsFluidEntryBin {
    # basename 以 '<入口名>.' 开头、以 '.bin' 结尾，忽略大小写。
    param(
        [Parameter(Mandatory = $true)][string]$Name,
        [Parameter(Mandatory = $true)][string[]]$Entries
    )

    if (-not $Name.EndsWith('.bin', [System.StringComparison]::OrdinalIgnoreCase)) {
        return $false
    }
    foreach ($entry in $Entries) {
        if ($Name.StartsWith($entry + '.', [System.StringComparison]::OrdinalIgnoreCase)) {
            return $true
        }
    }
    return $false
}

function Get-AllBinFiles {
    param([Parameter(Mandatory = $true)][string]$Root)

    return @(Get-ChildItem -LiteralPath $Root -Filter '*.bin' -File -Recurse -ErrorAction Stop |
        Sort-Object -Property FullName)
}

function Get-FluidEntryBinFiles {
    param(
        [Parameter(Mandatory = $true)][string]$Root,
        [Parameter(Mandatory = $true)][string[]]$Entries
    )

    return @(Get-ChildItem -LiteralPath $Root -Filter '*.bin' -File -Recurse -ErrorAction Stop |
        Where-Object { Test-IsFluidEntryBin -Name $_.Name -Entries $Entries } |
        Sort-Object -Property FullName)
}

function Invoke-ClearFluidBins {
    param(
        [string]$ModDir,
        [switch]$DryRun
    )

    # 1. -ModDir 必填，不猜默认路径
    if ([string]::IsNullOrWhiteSpace($ModDir)) {
        throw '-ModDir 必填：请显式给出 mod 缓存目录（含 .bin 的目录），本脚本不猜默认路径。'
    }

    # 7. 目录不存在 -> 明确报错
    if (-not (Test-Path -LiteralPath $ModDir -PathType Container)) {
        throw "目录不存在或不是目录：$ModDir"
    }

    $root = (Resolve-Path -LiteralPath $ModDir).ProviderPath
    # 去掉末尾反斜杠，便于拼输出；'C:\' 这种盘符根不动（长度 3）
    if ($root.Length -gt 3 -and $root.EndsWith('\')) {
        $root = $root.Substring(0, $root.Length - 1)
    }

    $hits = @(Get-FluidEntryBinFiles -Root $root -Entries $EntryNames)

    # 6. 没有命中：不是错误
    if ($hits.Count -eq 0) {
        Write-Output "No matching .bin found under $root"
        $script:ExitCode = 0
        return
    }

    # 3. DryRun：只打印，不动文件
    if ($DryRun) {
        $totalBytes = 0L
        foreach ($file in $hits) {
            $totalBytes += $file.Length
            Write-Output ("Would move: {0}  {1} bytes  {2}" -f $file.FullName, $file.Length, $file.LastWriteTime.ToString('yyyy-MM-dd HH:mm:ss'))
        }
        $totalMb = '{0:F2}' -f ($totalBytes / 1MB)
        Write-Output ("Would move {0} file(s), total {1} MB" -f $hits.Count, $totalMb)
        $script:ExitCode = 0
        return
    }

    # 4. 真跑：进回收站，不用 Remove-Item
    try {
        Add-Type -AssemblyName Microsoft.VisualBasic -ErrorAction Stop
    }
    catch {
        Write-Output ("ERROR: 无法加载 Microsoft.VisualBasic 程序集，已中止，未移动任何文件（不会退化成硬删除）。{0}" -f $_.Exception.Message)
        $script:ExitCode = 1
        return
    }

    $recycleType = 'Microsoft.VisualBasic.FileIO.FileSystem' -as [type]
    if ($null -eq $recycleType) {
        Write-Output 'ERROR: 程序集已加载但找不到 Microsoft.VisualBasic.FileIO.FileSystem，已中止，未移动任何文件（不会退化成硬删除）。'
        $script:ExitCode = 1
        return
    }

    $movedCount = 0
    $failures = New-Object System.Collections.ArrayList

    foreach ($file in $hits) {
        try {
            # 删除后再读 Length 会受 FileInfo 缓存/失效影响，先取大小
            $sizeBytes = $file.Length
            [Microsoft.VisualBasic.FileIO.FileSystem]::DeleteFile(
                $file.FullName,
                [Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs,
                [Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin)
            $movedCount++
            Write-Output ("Moved to Recycle Bin: {0}  {1} bytes" -f $file.FullName, $sizeBytes)
        }
        catch {
            $message = $_.Exception.Message
            $null = $failures.Add([pscustomobject]@{ Path = $file.FullName; Message = $message })
            Write-Output ("FAILED: {0} :: {1}" -f $file.FullName, $message)
        }
    }

    Write-Output ("Moved {0} of {1} matching file(s) to Recycle Bin" -f $movedCount, $hits.Count)

    # 5. 复核：重扫同一目录
    $allRemaining = @(Get-AllBinFiles -Root $root)
    Write-Output ("Remaining .bin under {0}: {1}" -f $root, $allRemaining.Count)

    if ($allRemaining.Count -gt 0) {
        $preview = @($allRemaining | Select-Object -First 10)
        foreach ($file in $preview) {
            Write-Output ("  {0}" -f $file.Name)
        }
        if ($allRemaining.Count -gt $preview.Count) {
            Write-Output ("  ... and {0} more" -f ($allRemaining.Count - $preview.Count))
        }
        Write-Output '（剩下的是本地化/其它 shader 的 .bin 属正常；本脚本只清流体受影响的入口。）'
    }

    $leftMatched = @(Get-FluidEntryBinFiles -Root $root -Entries $EntryNames)
    Write-Output ("Remaining matching .bin: {0}" -f $leftMatched.Count)

    # 4. 失败最后一起报
    if ($failures.Count -gt 0) {
        Write-Output ("Failed to move {0} file(s):" -f $failures.Count)
        foreach ($failure in $failures) {
            Write-Output ("  {0} :: {1}" -f $failure.Path, $failure.Message)
        }
        $script:ExitCode = 2
        return
    }

    $script:ExitCode = 0
}

$script:ExitCode = 0
try {
    Invoke-ClearFluidBins -ModDir $ModDir -DryRun:$DryRun
}
catch {
    Write-Output ("ERROR: {0}" -f $_.Exception.Message)
    $script:ExitCode = 1
}

exit $script:ExitCode
