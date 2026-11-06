#Requires -Version 5.1
<#
.SYNOPSIS
    mod 流水线：check / clean / verify / all。默认只读；唯一的写操作是 clean 经
    clear-fluid-bins.ps1 把 .bin 移进回收站（-Apply 才真删）。

.DESCRIPTION
    设计依据：任务-20261105-1200\子体-草案\PIPELINE-DRAFT.md 与 节点-自动化盘点\PIPELINE.md。

    四条硬约束：
      1) clean 只以独立 powershell 进程调用 $PSScriptRoot\clear-fluid-bins.ps1，
         默认加 -DryRun；按模式断言子脚本末行。不复刻入口名/匹配/删除逻辑。
      2) 重复段：同一文件内同名段 -> FAIL；跨文件同名 -> WARN；constants 豁免。
      3) run 在 if 体内始终 WARN（不升 FAIL）；若该 if 的条件变量在本次扫描里
         从未被赋值/声明，再加一条 WARN [V8] if-var-never-set。
      4) 最后一行必须是固定格式的结论行（见 Format-*Conclusion）。

    退出码：0 通过 / 1 用法或环境错误 / 2 有 FAIL / 3 未捕获异常。
    所有运行期输出为 ASCII（结论行可被正则断言）；中文只出现在本注释里。

.PARAMETER Command
    check | clean | verify | all（位置参数 0）。

.PARAMETER ModDir
    mod 根目录（含 *.ini / *.bin）。check/clean/all 必填；verify 与 -Ini 二选一。
    不猜路径、不回溯父目录。

.PARAMETER Ini
    只查指定 ini；给了就不递归扫描。clean 不接受。

.PARAMETER Baseline
    基线 ini，只比 Key 段总数。verify/all 用。

.PARAMETER Apply
    clean/all：开 = 真删（进回收站），关 = DryRun。

.PARAMETER Compile
    check/all：额外调 compile-hlsl.py。

.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\mod-pipeline.ps1 check -ModDir "D:\Games\HI3\ShaderFixes"
.EXAMPLE
    powershell -NoProfile -ExecutionPolicy Bypass -File tools\mod-pipeline.ps1 clean -ModDir "D:\Games\HI3\ShaderFixes" -Apply
#>

[CmdletBinding()]
param(
    [Parameter(Position = 0)]
    [string]$Command,

    [string]$ModDir,

    [string[]]$Ini,

    [string]$Baseline,

    [switch]$Apply,

    [switch]$Compile
)

Set-StrictMode -Version 2.0
$ErrorActionPreference = 'Stop'

# ---------------------------------------------------------------------------
# 常量（照抄设计草案 §8，不另造）
# ---------------------------------------------------------------------------
$script:StaleToleranceSeconds = 2
$script:SectionHeaderRe = '^\s*\[([^\[\]]+)\]\s*$'          # blueprint\ini_scope.py:37
$script:IncludeRe = '^\s*#include\s+"([^"]+)"\s*$'         # tools\compile-hlsl.py:34
$script:StageRefRe = '^\s*(cs|ps|vs|gs|hs|ds)\s*=\s*([^\s;]+)\s*$'   # compile-hlsl.py:33（放宽到非 .hlsl 也捕获）
$script:RunRe = '^\s*run\s*=\s*(CommandList[^\s;]*)\s*(;.*)?$'
$script:KeyRe = '^\s*key\s*=\s*(.*)$'
$script:BackupPatterns = @('*.bak*', '*.orig', '*~', '*.before', '*副本*', '* - Copy*')
$script:MaxIncludeDepth = 32
$script:DisplayRoot = ''

# ---------------------------------------------------------------------------
# 输出
# ---------------------------------------------------------------------------
function Write-Line {
    param([string]$Text)
    [Console]::Out.WriteLine($Text)
}

# ---------------------------------------------------------------------------
# 问题清单
# ---------------------------------------------------------------------------
function New-IssueList {
    return [pscustomobject]@{
        UsageCode = 0
        Reason    = ''
        Fails     = (New-Object System.Collections.ArrayList)
        Warns     = (New-Object System.Collections.ArrayList)
        Infos     = (New-Object System.Collections.ArrayList)
    }
}

function Get-DisplayPath {
    param([string]$Path)
    if ([string]::IsNullOrWhiteSpace($Path)) { return '' }
    $p = $Path
    $root = $script:DisplayRoot
    if (-not [string]::IsNullOrWhiteSpace($root)) {
        $sep = $root
        if (-not $sep.EndsWith('\')) { $sep = $sep + '\' }
        if ($p.StartsWith($sep, [System.StringComparison]::OrdinalIgnoreCase)) {
            $p = $p.Substring($sep.Length)
        }
    }
    return $p
}

function New-Issue {
    param([string]$Id, [string]$Path, [int]$Line, [string]$Message)
    $loc = '-'
    if (-not [string]::IsNullOrWhiteSpace($Path)) {
        $loc = (Get-DisplayPath $Path) + ':' + $Line
    }
    return [pscustomobject]@{ Id = $Id; Location = $loc; Message = $Message }
}

function Add-Issue {
    param($Issues, [string]$Kind, [string]$Id, [string]$Path, [int]$Line, [string]$Message)
    $item = New-Issue $Id $Path $Line $Message
    if ($Kind -eq 'FAIL') {
        [void]$Issues.Fails.Add($item)
    }
    elseif ($Kind -eq 'WARN') {
        [void]$Issues.Warns.Add($item)
    }
    else {
        [void]$Issues.Infos.Add($item)
    }
}

function Add-Fail {
    param($Issues, [string]$Id, [string]$Path, [int]$Line, [string]$Message)
    Add-Issue $Issues 'FAIL' $Id $Path $Line $Message
}

function Add-Warn {
    param($Issues, [string]$Id, [string]$Path, [int]$Line, [string]$Message)
    Add-Issue $Issues 'WARN' $Id $Path $Line $Message
}

function Add-Info {
    param($Issues, [string]$Id, [string]$Path, [int]$Line, [string]$Message)
    Add-Issue $Issues 'INFO' $Id $Path $Line $Message
}

function Print-Issues {
    param($Issues)
    foreach ($i in $Issues.Fails) { Write-Line ('FAIL [' + $i.Id + '] ' + $i.Location + ' ' + $i.Message) }
    foreach ($i in $Issues.Warns) { Write-Line ('WARN [' + $i.Id + '] ' + $i.Location + ' ' + $i.Message) }
    foreach ($i in $Issues.Infos) { Write-Line ('INFO [' + $i.Id + '] ' + $i.Location + ' ' + $i.Message) }
}

# ---------------------------------------------------------------------------
# 结论行（格式是契约，字段顺序不许动）
# ---------------------------------------------------------------------------
function Format-CheckConclusion {
    param([int]$Code, [int]$Files, [int]$Entries, [int]$Bins, [int]$Fails, [int]$Warns, [string]$Reason)
    $head = 'CHECK OK'
    if ($Code -ne 0) { $head = 'CHECK FAIL' }
    if ($Bins -gt 0) {
        Write-Host ('[提示] res\fluid_probe 下还有 ' + $Bins + ' 个 .bin：导出后若它们仍在，说明着色器缓存未清，先跑 clean 再进游戏（否则跑的是旧编译）')
    }
    if ($Code -eq 1 -or $Code -eq 3) {
        return ($head + ' code=' + $Code + ' files=' + $Files + ' entries=' + $Entries + ' bins=' + $Bins + ' fails=' + $Fails + ' warns=' + $Warns + ' reason=' + $Reason)
    }
    return ($head + ' files=' + $Files + ' entries=' + $Entries + ' bins=' + $Bins + ' fails=' + $Fails + ' warns=' + $Warns)
}

function Format-CleanConclusion {
    param([int]$DryRun, $Moved, $Remaining, [int]$Code, [string]$Reason)
    $head = 'CLEAN OK'
    if ($Code -ne 0) { $head = 'CLEAN FAIL' }
    $line = $head + ' dryrun=' + $DryRun + ' moved=' + ([string]$Moved) + ' remaining=' + ([string]$Remaining)
    if ($Code -ne 0) {
        $line = $line + ' code=' + $Code + ' reason=' + $Reason
    }
    return $line
}

function Format-VerifyConclusion {
    param([int]$Code, [int]$Files, [int]$Keys, [int]$Fails, [int]$Warns, [string]$Reason, [int]$Base)
    $head = 'VERIFY OK'
    if ($Code -ne 0) { $head = 'VERIFY FAIL' }
    if ($Code -eq 1 -or $Code -eq 3) {
        return ($head + ' code=' + $Code + ' files=' + $Files + ' keys=' + $Keys + ' fails=' + $Fails + ' warns=' + $Warns + ' reason=' + $Reason)
    }
    $line = $head + ' files=' + $Files + ' keys=' + $Keys + ' fails=' + $Fails + ' warns=' + $Warns
    if ($Code -eq 0 -and $Base -ge 0) {
        $line = $line + ' base=' + $Base
    }
    return $line
}

function Write-UsageConclusion {
    param([string]$Cmd, [int]$DryRun, [string]$Reason)
    if ($Cmd -eq 'clean') {
        Write-Line (Format-CleanConclusion $DryRun 'na' 'na' 1 $Reason)
    }
    elseif ($Cmd -eq 'verify') {
        Write-Line (Format-VerifyConclusion 1 0 0 0 0 $Reason -1)
    }
    elseif ($Cmd -eq 'all') {
        Write-Line 'ALL FAIL at=check code=1'
    }
    else {
        Write-Line (Format-CheckConclusion 1 0 0 0 0 0 $Reason)
    }
}

function Write-ErrorConclusion {
    param([string]$Cmd)
    if ($Cmd -eq 'clean') {
        Write-Line (Format-CleanConclusion 1 'na' 'na' 3 'unhandled')
    }
    elseif ($Cmd -eq 'verify') {
        Write-Line (Format-VerifyConclusion 3 0 0 0 0 'unhandled' -1)
    }
    elseif ($Cmd -eq 'all') {
        Write-Line 'ALL FAIL at=check code=3'
    }
    else {
        Write-Line (Format-CheckConclusion 3 0 0 0 0 0 'unhandled')
    }
}

# ---------------------------------------------------------------------------
# 参数 / 文件选择
# ---------------------------------------------------------------------------
function Resolve-ModDirPath {
    param($Issues, [string]$ModDir)
    if ([string]::IsNullOrWhiteSpace($ModDir)) {
        $Issues.UsageCode = 1
        $Issues.Reason = 'moddir-required'
        return ''
    }
    $full = ''
    try {
        $full = [IO.Path]::GetFullPath($ModDir)
    }
    catch {
        $Issues.UsageCode = 1
        $Issues.Reason = 'moddir-not-dir'
        return ''
    }
    if (-not (Test-Path -LiteralPath $full -PathType Container)) {
        $Issues.UsageCode = 1
        $Issues.Reason = 'moddir-not-dir'
        return ''
    }
    return $full
}

function Resolve-IniFiles {
    param($Issues, [string]$ModDir, [string[]]$Ini)

    $files = New-Object System.Collections.ArrayList

    if ($null -ne $Ini -and $Ini.Count -gt 0) {
        foreach ($p in $Ini) {
            $full = ''
            try { $full = [IO.Path]::GetFullPath($p) } catch { $full = $p }
            if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
                $Issues.UsageCode = 1
                $Issues.Reason = 'ini-missing'
                return $files
            }
            $known = $false
            foreach ($existing in $files) {
                if ($existing -ieq $full) { $known = $true; break }
            }
            if (-not $known) { [void]$files.Add($full) }
        }
        return $files
    }

    if ([string]::IsNullOrWhiteSpace($ModDir)) {
        $Issues.UsageCode = 1
        $Issues.Reason = 'moddir-or-ini-required'
        return $files
    }

    $all = @(Get-ChildItem -LiteralPath $ModDir -File -Recurse -Force -ErrorAction SilentlyContinue |
        Sort-Object -Property FullName)
    foreach ($f in $all) {
        if ($f.Extension -ine '.ini') { continue }
        if ($f.Name -ieq 'd3dx.ini') {
            Write-Line 'SKIP d3dx.ini'
            continue
        }
        if ($f.FullName -match '\\\.git\\') { continue }
        [void]$files.Add($f.FullName)
    }

    if ($files.Count -eq 0) {
        $Issues.UsageCode = 1
        $Issues.Reason = 'no-ini'
    }
    return $files
}

# ---------------------------------------------------------------------------
# 读文件 / 解码（C0 / C11）
# ---------------------------------------------------------------------------
function Read-IniDocument {
    param([string]$Path)

    $doc = [pscustomobject]@{
        Path        = $Path
        Lines       = @()
        Error       = ''
        HasBom      = $false
        AnsiFallback = $false
    }

    $bytes = $null
    try {
        $bytes = [IO.File]::ReadAllBytes($Path)
    }
    catch {
        $doc.Error = $_.Exception.Message
        return $doc
    }

    $offset = 0
    if ($bytes.Length -ge 3 -and $bytes[0] -eq 0xEF -and $bytes[1] -eq 0xBB -and $bytes[2] -eq 0xBF) {
        $doc.HasBom = $true
        $offset = 3
    }
    $count = $bytes.Length - $offset

    $text = $null
    $strict = [System.Text.UTF8Encoding]::new($false, $true)
    try {
        $text = $strict.GetString($bytes, $offset, $count)
    }
    catch [System.Text.DecoderFallbackException] {
        try {
            $ansi = [Text.Encoding]::GetEncoding([Globalization.CultureInfo]::CurrentCulture.TextInfo.ANSICodePage)
            $text = $ansi.GetString($bytes, $offset, $count)
            $doc.AnsiFallback = $true
        }
        catch {
            $doc.Error = 'undecodable'
            return $doc
        }
    }

    if ($null -eq $text) {
        $doc.Error = 'undecodable'
        return $doc
    }

    $doc.Lines = [regex]::Split($text, "\r\n|\n|\r")
    return $doc
}

# ---------------------------------------------------------------------------
# 段切分（照 blueprint\ini_scope.py:37 的正则，保留重复段头）
# ---------------------------------------------------------------------------
function Find-Sections {
    param([string[]]$Lines)

    $sections = New-Object System.Collections.ArrayList
    if ($null -eq $Lines -or $Lines.Count -eq 0) { return $sections }

    $headerRe = [regex]::new($script:SectionHeaderRe)
    $headers = New-Object System.Collections.ArrayList

    for ($i = 0; $i -lt $Lines.Count; $i++) {
        $t = $Lines[$i].Trim()
        $m = $headerRe.Match($t)
        if ($m.Success) {
            [void]$headers.Add([pscustomobject]@{ Name = $m.Groups[1].Value.Trim(); Index = $i })
        }
    }

    for ($k = 0; $k -lt $headers.Count; $k++) {
        $h = $headers[$k]
        $end = $Lines.Count
        if ($k + 1 -lt $headers.Count) { $end = $headers[$k + 1].Index }
        [void]$sections.Add([pscustomobject]@{
                Name      = $h.Name
                Header    = $h.Index
                BodyStart = $h.Index + 1
                BodyEnd   = $end
            })
    }

    return $sections
}

function Add-Sections {
    param($SectionMap, $Doc, $Sections)
    foreach ($s in $Sections) {
        $k = $s.Name.ToLowerInvariant()
        if (-not $SectionMap.ContainsKey($k)) {
            $SectionMap[$k] = New-Object System.Collections.ArrayList
        }
        [void]$SectionMap[$k].Add([pscustomobject]@{ Path = $Doc.Path; Line = $s.Header + 1; Name = $s.Name })
    }
}

# ---------------------------------------------------------------------------
# 段头语法（C1）
# ---------------------------------------------------------------------------
function Test-HeaderSyntax {
    param($Issues, $Doc)
    for ($i = 0; $i -lt $Doc.Lines.Count; $i++) {
        $t = $Doc.Lines[$i].Trim()
        if (-not $t.StartsWith('[')) { continue }
        if (-not [regex]::IsMatch($t, $script:SectionHeaderRe)) {
            Add-Fail $Issues 'C1' $Doc.Path ($i + 1) ('bad section header: ' + $t)
        }
    }
}

# ---------------------------------------------------------------------------
# if/endif 深度（按段独立配平；同时收集段内 run 及其所在 if 的条件变量）
# ---------------------------------------------------------------------------
function Get-ConditionVars {
    param([string]$Line)
    $names = New-Object System.Collections.ArrayList
    $m = [regex]::Match($Line, '^\s*if\b(.*)$')
    if (-not $m.Success) { return $names }
    foreach ($vm in [regex]::Matches($m.Groups[1].Value, '\$([A-Za-z_][A-Za-z0-9_]*)')) {
        $n = $vm.Groups[1].Value.ToLowerInvariant()
        if (-not $names.Contains($n)) { [void]$names.Add($n) }
    }
    return $names
}

function Measure-SectionDepth {
    param([string[]]$Lines, [int]$Start, [int]$End, $Runs)

    $depth = 0
    $neg = $false
    $stack = New-Object System.Collections.ArrayList

    for ($i = $Start; $i -lt $End; $i++) {
        $t = $Lines[$i].Trim()
        if ($t.Length -eq 0) { continue }
        if ($t.StartsWith(';') -or $t.StartsWith('//')) { continue }

        if ($t -match '^if\b') {
            $vars = @(Get-ConditionVars $t)
            [void]$stack.Add($vars)
            $depth++
            continue
        }
        if ($t -match '^endif\b') {
            $depth--
            if ($depth -lt 0) {
                $neg = $true
                $depth = 0
            }
            if ($stack.Count -gt 0) { $stack.RemoveAt($stack.Count - 1) }
            continue
        }
        if ($null -ne $Runs -and $t -match '^run\s*=' -and $depth -gt 0) {
            $vars = @()
            if ($stack.Count -gt 0) { $vars = $stack[$stack.Count - 1] }
            [void]$Runs.Add([pscustomobject]@{ Line = $i; Depth = $depth; Vars = $vars })
        }
    }

    return [pscustomobject]@{ Depth = $depth; Negative = $neg }
}

function Test-SectionDepth {
    param($Issues, $Doc, $Sections, [string]$Id)
    foreach ($sec in $Sections) {
        $stats = Measure-SectionDepth $Doc.Lines $sec.BodyStart $sec.BodyEnd $null
        if ($stats.Depth -ne 0 -or $stats.Negative) {
            Add-Fail $Issues $Id $Doc.Path ($sec.Header + 1) ('section [' + $sec.Name + '] if/endif unbalanced depth=' + $stats.Depth + ' negative=' + ([string]$stats.Negative))
        }
    }
}

function Test-PreambleDepth {
    param($Issues, $Doc, $Sections, [string]$Id)
    $end = $Doc.Lines.Count
    if ($Sections.Count -gt 0) { $end = $Sections[0].Header }
    for ($i = 0; $i -lt $end; $i++) {
        $t = $Doc.Lines[$i].Trim()
        if ($t.Length -eq 0) { continue }
        if ($t.StartsWith(';') -or $t.StartsWith('//')) { continue }
        if ($t -match '^if\b' -or $t -match '^endif\b') {
            Add-Fail $Issues $Id $Doc.Path ($i + 1) 'if/endif before first section header'
            return
        }
    }
}

# ---------------------------------------------------------------------------
# 变量赋值收集（给 V8 的 if-var-never-set 用）
# ---------------------------------------------------------------------------
function Collect-VariableSets {
    param($Doc, $VarSet)
    for ($i = 0; $i -lt $Doc.Lines.Count; $i++) {
        $t = $Doc.Lines[$i].Trim()
        if ($t.Length -eq 0) { continue }
        if ($t.StartsWith(';') -or $t.StartsWith('//')) { continue }
        $m = [regex]::Match($t, '^\$([A-Za-z_][A-Za-z0-9_]*)\s*=')
        if ($m.Success) {
            $VarSet[$m.Groups[1].Value.ToLowerInvariant()] = $true
            continue
        }
        if ($t -match '^global\b') {
            foreach ($vm in [regex]::Matches($t, '\$([A-Za-z_][A-Za-z0-9_]*)')) {
                $VarSet[$vm.Groups[1].Value.ToLowerInvariant()] = $true
            }
        }
    }
}

# ---------------------------------------------------------------------------
# stage 引用（C4）+ 入口收集
# ---------------------------------------------------------------------------
function Test-StageRefs {
    param($Issues, $Doc, $Entries)

    $re = [regex]::new($script:StageRefRe, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
    $dir = Split-Path -Parent $Doc.Path

    for ($i = 0; $i -lt $Doc.Lines.Count; $i++) {
        $t = $Doc.Lines[$i].Trim()
        $m = $re.Match($t)
        if (-not $m.Success) { continue }

        $stage = $m.Groups[1].Value.ToLowerInvariant()
        $value = $m.Groups[2].Value
        if ($value -ieq 'null') { continue }
        if (-not ($value -match '\.hlsl$')) {
            Add-Fail $Issues 'C4' $Doc.Path ($i + 1) ('stage ref is not .hlsl: ' + $value)
            continue
        }

        $full = ''
        try {
            $full = [IO.Path]::GetFullPath((Join-Path $dir ($value -replace '/', '\')))
        }
        catch {
            Add-Fail $Issues 'C4' $Doc.Path ($i + 1) ('bad stage path: ' + $value)
            continue
        }
        if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
            Add-Fail $Issues 'C4' $Doc.Path ($i + 1) ('missing stage file: ' + $value)
            continue
        }

        $key = $full.ToLowerInvariant() + '|' + $stage
        if (-not $Entries.ContainsKey($key)) {
            $Entries[$key] = [pscustomobject]@{
                FullPath = $full
                Stem     = [IO.Path]::GetFileNameWithoutExtension($full)
                Stage    = $stage
                Ini      = $Doc.Path
                Line     = $i + 1
            }
        }
    }
}

# ---------------------------------------------------------------------------
# bin 发现 + include 闭包 + C8 / C9
# ---------------------------------------------------------------------------
function Get-AllBins {
    param([string]$Root)
    $result = New-Object System.Collections.ArrayList
    if ([string]::IsNullOrWhiteSpace($Root)) { return $result }
    $items = @(Get-ChildItem -LiteralPath $Root -File -Recurse -Force -Filter '*.bin' -ErrorAction SilentlyContinue)
    foreach ($f in $items) {
        if ($f.FullName -match '\\\.git\\') { continue }
        [void]$result.Add($f)
    }
    return $result
}

function Select-EntryBins {
    param($AllBins, [string]$Stem, [string]$Stage)
    $result = New-Object System.Collections.ArrayList
    $pattern = '^' + [regex]::Escape($Stem) + '\.' + [regex]::Escape($Stage) + '_\d+_\d+(\.[0-9a-fA-F]+)?\.bin$'
    foreach ($f in $AllBins) {
        if ([regex]::IsMatch($f.Name, $pattern, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)) {
            [void]$result.Add($f)
        }
    }
    $sorted = @($result | Sort-Object -Property LastWriteTimeUtc -Descending)
    return $sorted
}

function Get-IncludeClosure {
    param([string]$EntryPath)

    $files = New-Object System.Collections.ArrayList
    $missing = New-Object System.Collections.ArrayList
    $seen = @{}
    $stack = New-Object System.Collections.Stack

    $stack.Push([pscustomobject]@{ Path = $EntryPath; Depth = 0; From = '' })

    while ($stack.Count -gt 0) {
        $item = $stack.Pop()
        $full = ''
        try { $full = [IO.Path]::GetFullPath($item.Path) } catch { continue }
        if ($seen.ContainsKey($full)) { continue }
        $seen[$full] = $true

        if ($item.Depth -gt $script:MaxIncludeDepth) {
            [void]$missing.Add([pscustomobject]@{ Path = $full; From = $item.From; DepthExceeded = $true })
            continue
        }

        if (-not (Test-Path -LiteralPath $full -PathType Leaf)) {
            [void]$missing.Add([pscustomobject]@{ Path = $full; From = $item.From; DepthExceeded = $false })
            continue
        }

        try {
            $fi = Get-Item -LiteralPath $full -ErrorAction Stop
        }
        catch {
            continue
        }
        [void]$files.Add($fi)

        $doc = Read-IniDocument $full
        if ($doc.Error -ne '') { continue }
        $baseDir = Split-Path -Parent $full
        foreach ($line in $doc.Lines) {
            $m = [regex]::Match($line, $script:IncludeRe)
            if (-not $m.Success) { continue }
            $child = $m.Groups[1].Value
            $childFull = ''
            try {
                $childFull = [IO.Path]::GetFullPath((Join-Path $baseDir ($child -replace '/', '\')))
            }
            catch {
                continue
            }
            $stack.Push([pscustomobject]@{ Path = $childFull; Depth = $item.Depth + 1; From = $full })
        }
    }

    return [pscustomobject]@{ Files = @($files); Missing = @($missing) }
}

function Test-MissingIncludes {
    param($Issues, $Ref, $Closure)
    foreach ($m in $Closure.Missing) {
        if ($m.DepthExceeded) {
            Add-Warn $Issues 'C8' $Ref.Ini $Ref.Line ('include depth > ' + $script:MaxIncludeDepth + ': ' + (Get-DisplayPath $m.Path))
            continue
        }
        Add-Fail $Issues 'C8' $Ref.Ini $Ref.Line ('missing include: ' + (Get-DisplayPath $m.Path) + ' (from ' + (Get-DisplayPath $m.From) + ')')
    }
}

function Test-BinFreshness {
    param($Issues, $Ref, $Closure, $Bins, $BinSet)

    if ($Bins.Count -eq 0) {
        Add-Warn $Issues 'C9' $Ref.Ini $Ref.Line ('no-bin for ' + $Ref.Stem + '.' + $Ref.Stage)
        return
    }
    if ($Bins.Count -gt 1) {
        Add-Warn $Issues 'C9' $Ref.Ini $Ref.Line ('multiple bins (' + $Bins.Count + ') for ' + $Ref.Stem + '.' + $Ref.Stage + ', using newest')
    }

    $bin = $Bins[0]
    $BinSet[$bin.FullName] = $true

    $entryTime = $null
    try { $entryTime = (Get-Item -LiteralPath $Ref.FullPath -ErrorAction Stop).LastWriteTimeUtc } catch { return }
    $binTime = $bin.LastWriteTimeUtc
    $tol = $script:StaleToleranceSeconds

    if ($entryTime -gt $binTime.AddSeconds($tol)) {
        Add-Info $Issues 'C9' $Ref.Ini $Ref.Line ('entry newer than bin -> 3Dmigoto will rebuild: ' + $bin.Name)
        return
    }

    $newest = $null
    foreach ($f in $Closure.Files) {
        if ($null -eq $f) { continue }
        if ($f.FullName -ieq $Ref.FullPath) { continue }
        if ($null -eq $newest -or $f.LastWriteTimeUtc -gt $newest.LastWriteTimeUtc) { $newest = $f }
    }

    if ($null -ne $newest -and $newest.LastWriteTimeUtc -gt $binTime.AddSeconds($tol)) {
        $delta = $newest.LastWriteTimeUtc - $binTime
        $deltaHours = [int]$delta.TotalHours
        $deltaText = ('+{0:00}:{1:00}:{2:00}' -f $deltaHours, $delta.Minutes, $delta.Seconds)
        $entryDisp = (Get-DisplayPath $Ref.FullPath) -replace '\\', '/'
        $newestDisp = (Get-DisplayPath $newest.FullName) -replace '\\', '/'
        Add-Fail $Issues 'C9' $Ref.Ini $Ref.Line ($entryDisp + ' <- ' + $newestDisp + ' (' + $deltaText + ') older bin ' + $bin.Name)
    }
}

# ---------------------------------------------------------------------------
# run 引用（C5 / V3）
# ---------------------------------------------------------------------------
function Test-RunRefs {
    param($Issues, $Docs, $SectionMap, [string]$Id, $ExternalSections, [bool]$ResolveExternal)
    $re = [regex]::new($script:RunRe, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
    foreach ($doc in $Docs) {
        for ($i = 0; $i -lt $doc.Lines.Count; $i++) {
            $t = $doc.Lines[$i].Trim()
            $m = $re.Match($t)
            if (-not $m.Success) { continue }
            $target = $m.Groups[1].Value
            if ($target -ieq 'CommandList') {
                Add-Warn $Issues $Id $doc.Path ($i + 1) 'run-empty'
                continue
            }
            if ($target.Contains('\')) {
                Add-Warn $Issues $Id $doc.Path ($i + 1) ('namespaced run: ' + $target)
                continue
            }
            if ($target -match '^BuiltInCommandList') { continue }
            $key = $target.ToLowerInvariant()
            if ($SectionMap.ContainsKey($key)) { continue }
            if ($null -ne $ExternalSections -and $ExternalSections.ContainsKey($key)) {
                Add-Info $Issues $Id $doc.Path ($i + 1) ('run resolved in d3dx include closure: ' + $target)
                continue
            }
            if ($ResolveExternal) {
                Add-Fail $Issues $Id $doc.Path ($i + 1) ('dangling run (not in Mods tree or d3dx include closure): ' + $target)
            }
            else {
                Add-Warn $Issues $Id $doc.Path ($i + 1) ('unresolved run (no d3dx.ini to resolve): ' + $target)
            }
        }
    }
}

function Get-D3dxIniPath {
    param([string]$StartDir)
    if ([string]::IsNullOrWhiteSpace($StartDir)) { return '' }
    $d = $StartDir
    try { $d = (Resolve-Path -LiteralPath $StartDir -ErrorAction Stop).ProviderPath } catch { return '' }
    for ($i = 0; $i -lt 5; $i++) {
        $p = Join-Path $d 'd3dx.ini'
        if (Test-Path -LiteralPath $p -PathType Leaf) { return $p }
        $parent = Split-Path -Parent $d
        if ([string]::IsNullOrWhiteSpace($parent) -or $parent -eq $d) { break }
        $d = $parent
    }
    return ''
}

function Get-IncludeSectionNames {
    param([string]$D3dxIni)
    $set = @{}
    if ([string]::IsNullOrWhiteSpace($D3dxIni) -or -not (Test-Path -LiteralPath $D3dxIni -PathType Leaf)) { return $set }

    $visited = @{}
    $queue = New-Object System.Collections.ArrayList
    [void]$queue.Add([IO.Path]::GetFullPath($D3dxIni))
    $excludes = New-Object System.Collections.ArrayList

    while ($queue.Count -gt 0) {
        $full = [string]$queue[0]
        $queue.RemoveAt(0)
        if ($visited.ContainsKey($full)) { continue }
        $visited[$full] = $true
        if (-not (Test-Path -LiteralPath $full -PathType Leaf)) { continue }

        $doc = Read-IniDocument $full
        if ($doc.Error -ne '') { continue }
        foreach ($s in @(Find-Sections $doc.Lines)) {
            $set[$s.Name.ToLowerInvariant()] = $true
        }

        $inInclude = $false
        $baseDir = Split-Path -Parent $full
        for ($i = 0; $i -lt $doc.Lines.Count; $i++) {
            $t = $doc.Lines[$i].Trim()
            if ($t -match '^\s*\[([^\]]+)\]\s*$') {
                $inInclude = ($matches[1] -ieq 'Include')
                continue
            }
            if (-not $inInclude) { continue }
            if ($t -match '^\s*exclude_recursive\s*=\s*([^;]+)') {
                [void]$excludes.Add($matches[1].Trim())
                continue
            }
            if ($t -match '^\s*include\s*=\s*([^;]+)') {
                $rel = $matches[1].Trim()
                $child = [IO.Path]::GetFullPath((Join-Path $baseDir ($rel -replace '/', '\')))
                if (-not $visited.ContainsKey($child)) { [void]$queue.Add($child) }
                continue
            }
            if ($t -match '^\s*include_recursive\s*=\s*([^;]+)') {
                $rel = $matches[1].Trim()
                $dir = [IO.Path]::GetFullPath((Join-Path $baseDir ($rel -replace '/', '\')))
                if (-not (Test-Path -LiteralPath $dir -PathType Container)) { continue }
                $files = @(Get-ChildItem -LiteralPath $dir -File -Recurse -Force -Filter '*.ini' -ErrorAction SilentlyContinue)
                foreach ($f in $files) {
                    $skip = $false
                    $relPath = $f.FullName.Substring($dir.Length).TrimStart('\')
                    foreach ($part in $relPath.Split('\')) {
                        foreach ($pat in $excludes) {
                            if ($part -like $pat) { $skip = $true; break }
                        }
                        if ($skip) { break }
                    }
                    if (-not $skip -and -not $visited.ContainsKey($f.FullName)) { [void]$queue.Add($f.FullName) }
                }
            }
        }
    }
    return $set
}

# ---------------------------------------------------------------------------
# [Key*] 段（C6 / V6 / V7 收集）
# ---------------------------------------------------------------------------
function Test-KeySection {
    param($Issues, $Doc, $Sec, [string]$Id, $Hotkeys)

    $keyLines = New-Object System.Collections.ArrayList
    for ($i = $Sec.BodyStart; $i -lt $Sec.BodyEnd; $i++) {
        $t = $Doc.Lines[$i].Trim()
        if ($t.Length -eq 0) { continue }
        if ($t.StartsWith(';') -or $t.StartsWith('//')) { continue }
        $m = [regex]::Match($t, $script:KeyRe, [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
        if ($m.Success) {
            [void]$keyLines.Add([pscustomobject]@{ Line = $i; Value = $m.Groups[1].Value })
        }
    }

    if ($keyLines.Count -eq 0) {
        Add-Fail $Issues $Id $Doc.Path ($Sec.Header + 1) ('[' + $Sec.Name + '] missing key =')
        return
    }
    if ($keyLines.Count -gt 1) {
        Add-Warn $Issues $Id $Doc.Path ($Sec.Header + 1) ('[' + $Sec.Name + '] multiple key = lines')
    }

    foreach ($kl in $keyLines) {
        $val = $kl.Value
        $semi = $val.IndexOf(';')
        if ($semi -ge 0) { $val = $val.Substring(0, $semi) }
        $val = $val.Trim()
        if ($val.Length -eq 0) {
            Add-Warn $Issues $Id $Doc.Path ($kl.Line + 1) ('[' + $Sec.Name + '] empty key =')
            continue
        }
        if ($null -ne $Hotkeys) {
            $tokens = @(($val.ToLowerInvariant() -split '\s+') | Where-Object { $_.Length -gt 0 } | Sort-Object)
            if ($tokens.Count -gt 0) {
                $canon = $tokens -join ' '
                if (-not $Hotkeys.ContainsKey($canon)) {
                    $Hotkeys[$canon] = New-Object System.Collections.ArrayList
                }
                $loc = (Get-DisplayPath $Doc.Path) + ':' + ($kl.Line + 1) + ' [' + $Sec.Name + ']'
                [void]$Hotkeys[$canon].Add($loc)
            }
        }
    }
}

function Test-KeySections {
    param($Issues, $Doc, $Sections, [string]$Id)
    foreach ($sec in $Sections) {
        if ($sec.Name -notmatch '^key') { continue }
        Test-KeySection $Issues $Doc $sec $Id $null
    }
}

# ---------------------------------------------------------------------------
# 重复段（C2 / V4）：同文件 FAIL，跨文件 WARN，constants 豁免
# ---------------------------------------------------------------------------
function Test-DuplicateSections {
    param($Issues, $SectionMap, [string]$Id)

    foreach ($name in ($SectionMap.Keys | Sort-Object)) {
        if ($name -eq 'constants') { continue }

        $entries = @($SectionMap[$name])
        $byFile = @{}
        foreach ($e in $entries) {
            $k = $e.Path.ToLowerInvariant()
            if (-not $byFile.ContainsKey($k)) {
                $byFile[$k] = New-Object System.Collections.ArrayList
            }
            [void]$byFile[$k].Add($e)
        }

        foreach ($k in ($byFile.Keys | Sort-Object)) {
            $list = $byFile[$k]
            if ($list.Count -gt 1) {
                $second = $list[1]
                if ($name -eq 'present') {
                    # 2026-11-05 真机推翻：菲欧妮.ini 同文件 4 个 [Present]（:80/:389/:1467/:8638）
                    # 模组功能正常 -> 3Dmigoto 对 [Present] 是合并/追加，不是丢弃。
                    $linesText = (@($list | ForEach-Object { $_.Line }) -join ',')
                    Add-Warn $Issues $Id $second.Path $second.Line ('duplicate [present] x' + $list.Count + ' in same file at lines ' + $linesText + ' (3Dmigoto merges; not a failure)')
                }
                else {
                    Add-Fail $Issues $Id $second.Path $second.Line ('duplicate section [' + $name + '] x' + $list.Count + ' in same file')
                }
            }
        }

        if ($byFile.Keys.Count -gt 1) {
            $first = $entries[0]
            Add-Warn $Issues $Id $first.Path $first.Line ('duplicate section [' + $name + '] across ' + $byFile.Keys.Count + ' files')
        }
    }
}

function Test-DuplicateDraws {
    param($Issues, $Doc, $Sections, [string]$Id)
    $re = [regex]::new('^\s*(draw|dispatch)\s*=\s*(.*?)\s*$', [System.Text.RegularExpressions.RegexOptions]::IgnoreCase)
    foreach ($sec in $Sections) {
        $seen = @{}
        $stack = New-Object System.Collections.ArrayList
        for ($i = $sec.BodyStart; $i -lt $sec.BodyEnd; $i++) {
            $t = $Doc.Lines[$i].Trim()
            if ($t.StartsWith(';') -or $t.StartsWith('//')) { continue }
            if ($t -match '^if\b') {
                [void]$stack.Add('if' + ($i + 1))
                continue
            }
            if ($t -match '^elseif\b' -or $t -match '^else\b') {
                if ($stack.Count -gt 0) { $stack[$stack.Count - 1] = 'else' + ($i + 1) }
                continue
            }
            if ($t -match '^endif\b') {
                if ($stack.Count -gt 0) { $stack.RemoveAt($stack.Count - 1) }
                continue
            }
            $m = $re.Match($t)
            if (-not $m.Success) { continue }
            $params = ($m.Groups[2].Value -replace '\s+', ' ').Trim()
            # 同参数只在“同一分支”里才算重复：不同 if/else 分支的相同 draw 是合法分流。
            $path = ($stack -join '>')
            $key = $m.Groups[1].Value.ToLowerInvariant() + ' = ' + $params + ' @ ' + $path
            if (-not $seen.ContainsKey($key)) {
                $seen[$key] = New-Object System.Collections.ArrayList
            }
            [void]$seen[$key].Add($i + 1)
        }
        foreach ($key in ($seen.Keys | Sort-Object)) {
            $list = $seen[$key]
            if ($list.Count -gt 1) {
                Add-Fail $Issues $Id $Doc.Path ($list[1]) ('duplicate ' + $key + ' x' + $list.Count + ' in [' + $sec.Name + '] lines ' + ($list -join ','))
            }
        }
    }
}

# ---------------------------------------------------------------------------
# 备份文件（C7）
# ---------------------------------------------------------------------------
function Test-BackupFiles {
    param($Issues, [string]$Root)
    if ([string]::IsNullOrWhiteSpace($Root)) { return }
    $files = @(Get-ChildItem -LiteralPath $Root -File -Recurse -Force -ErrorAction SilentlyContinue |
        Sort-Object -Property FullName)
    $rootLen = $Root.Length
    foreach ($f in $files) {
        if ($f.FullName -match '\\\.git\\') { continue }
        $rel = $f.FullName
        if ($rel.Length -gt $rootLen) { $rel = $rel.Substring($rootLen).TrimStart('\') }

        $nameHit = $false
        foreach ($p in $script:BackupPatterns) {
            if ($f.Name -like $p) { $nameHit = $true; break }
        }
        $dirHit = $false
        $parent = Split-Path -Parent $rel
        if (-not [string]::IsNullOrWhiteSpace($parent)) {
            foreach ($part in $parent.Split('\')) {
                foreach ($p in $script:BackupPatterns) {
                    if ($part -like $p) { $dirHit = $true; break }
                }
                if ($dirHit) { break }
            }
        }
        if (-not $nameHit -and -not $dirHit) { continue }

        # 只有「会被 include_recursive 收进去」的备份才 FAIL：
        #   1) 文件名本身以 .ini 结尾（如 `角色 - 副本.ini`）；
        #   2) 父目录是备份目录（如 `菲欧妮.bak-20261103-132109\菲欧妮.ini`）。
        # `xxx.ini.bak-...` 不会被加载，只报 INFO。
        $loadable = $f.Name.EndsWith('.ini', [System.StringComparison]::OrdinalIgnoreCase) -or $dirHit

        if ($loadable) {
            Add-Fail $Issues 'C7' $f.FullName 0 ('backup .ini will be loaded by include_recursive: ' + $rel)
        }
        else {
            Add-Info $Issues 'C7' $f.FullName 0 ('backup file left in tree (not .ini, safe): ' + $f.Name)
        }
    }
}

# ---------------------------------------------------------------------------
# 可选真编译（C10）
# ---------------------------------------------------------------------------
function Get-PythonInvoker {
    $py = Get-Command python -ErrorAction SilentlyContinue
    if ($null -ne $py) {
        $exe = [string]$py.Source
        return [pscustomobject]@{ Exe = $exe; Prefix = @() }
    }
    $py3 = Get-Command py -ErrorAction SilentlyContinue
    if ($null -ne $py3) {
        $exe3 = [string]$py3.Source
        return [pscustomobject]@{ Exe = $exe3; Prefix = @('-3') }
    }
    return $null
}

function Test-Compile {
    param($Issues, $IniFiles)

    $compile = Join-Path $PSScriptRoot 'compile-hlsl.py'
    if (-not (Test-Path -LiteralPath $compile -PathType Leaf)) {
        $Issues.UsageCode = 1
        $Issues.Reason = 'child-missing'
        return
    }
    $py = Get-PythonInvoker
    if ($null -eq $py) {
        $Issues.UsageCode = 1
        $Issues.Reason = 'python-missing'
        return
    }

    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        foreach ($ini in $IniFiles) {
            $argv = @()
            $argv += $py.Prefix
            $argv += $compile
            $argv += '--ini'
            $argv += $ini
            $argv += '--root'
            $argv += (Split-Path -Parent $ini)

            $out = @()
            $code = -1
            try {
                $out = @(& $py.Exe @argv 2>&1)
                $code = $LASTEXITCODE
            }
            catch {
                $ErrorActionPreference = $oldEap
                $Issues.UsageCode = 1
                $Issues.Reason = 'compiler-env'
                return
            }

            $text = (@($out) | ForEach-Object { [string]$_ }) -join "`n"
            if ($code -eq 0) {
                Add-Info $Issues 'C10' $ini 0 'compile ok'
            }
            elseif ($code -eq 2) {
                Add-Warn $Issues 'C10' $ini 0 'no-hlsl-refs'
            }
            elseif ($code -eq 1 -and $text -match 'Traceback \(most recent call last\)') {
                $ErrorActionPreference = $oldEap
                $Issues.UsageCode = 1
                $Issues.Reason = 'compiler-env'
                return
            }
            elseif ($code -eq 1) {
                $failLines = @(@($out) | ForEach-Object { [string]$_ } | Where-Object { $_ -match '^FAIL' } | Select-Object -First 5)
                Add-Fail $Issues 'C10' $ini 0 ('compile failed: ' + ($failLines -join ' / '))
            }
            else {
                Add-Fail $Issues 'C10' $ini 0 ('compile exit ' + $code)
            }
        }
    }
    finally {
        $ErrorActionPreference = $oldEap
    }
}

# ---------------------------------------------------------------------------
# check（只读）
# ---------------------------------------------------------------------------
function Invoke-Check {
    param([string]$ModDir, [string[]]$Ini, [bool]$Compile)

    $issues = New-IssueList
    $script:DisplayRoot = ''

    if ([string]::IsNullOrWhiteSpace($ModDir)) {
        Write-UsageConclusion 'check' 1 'moddir-required'
        return 1
    }
    $root = Resolve-ModDirPath $issues $ModDir
    if ($issues.UsageCode -ne 0) {
        Write-UsageConclusion 'check' 1 $issues.Reason
        return 1
    }
    $script:DisplayRoot = $root

    $iniFiles = @(Resolve-IniFiles $issues $root $Ini)
    if ($issues.UsageCode -ne 0) {
        Write-UsageConclusion 'check' 1 $issues.Reason
        return 1
    }

    $sectionMap = @{}
    $entries = @{}
    $binSet = @{}
    $docs = New-Object System.Collections.ArrayList

    foreach ($f in $iniFiles) {
        $doc = Read-IniDocument $f
        if ($doc.Error -ne '') {
            Add-Fail $issues 'C0' $f 0 ('cannot read: ' + $doc.Error)
            continue
        }
        [void]$docs.Add($doc)

        if ($doc.HasBom) { Add-Warn $issues 'C11' $f 0 'utf8-bom' }
        if ($doc.AnsiFallback) { Add-Warn $issues 'C11' $f 0 'ansi-fallback' }

        $sections = @(Find-Sections $doc.Lines)
        Test-HeaderSyntax $issues $doc
        Add-Sections $sectionMap $doc $sections
        Test-PreambleDepth $issues $doc $sections 'C3'
        Test-SectionDepth $issues $doc $sections 'C3'
        Test-DuplicateDraws $issues $doc $sections 'C2'
        Test-StageRefs $issues $doc $entries
        Test-KeySections $issues $doc $sections 'C6'
    }

    Test-DuplicateSections $issues $sectionMap 'C2'
    $d3dx = Get-D3dxIniPath $root
    $external = Get-IncludeSectionNames $d3dx
    Test-RunRefs $issues $docs $sectionMap 'C5' $external ($d3dx -ne '')
    Test-BackupFiles $issues $root

    $allBins = @(Get-AllBins $root)
    foreach ($key in ($entries.Keys | Sort-Object)) {
        $ref = $entries[$key]
        $closure = Get-IncludeClosure $ref.FullPath
        Test-MissingIncludes $issues $ref $closure
        $bins = @(Select-EntryBins $allBins $ref.Stem $ref.Stage)
        Test-BinFreshness $issues $ref $closure $bins $binSet
    }

    if ($Compile) { Test-Compile $issues $iniFiles }

    Print-Issues $issues

    $code = 0
    if ($issues.UsageCode -ne 0) { $code = 1 }
    elseif ($issues.Fails.Count -gt 0) { $code = 2 }

    Write-Line (Format-CheckConclusion $code $iniFiles.Count $entries.Count $binSet.Count $issues.Fails.Count $issues.Warns.Count $issues.Reason)
    return $code
}

# ---------------------------------------------------------------------------
# clean（独立进程包装 clear-fluid-bins.ps1）
# ---------------------------------------------------------------------------
function Invoke-Clean {
    param([string]$ModDir, [bool]$Apply)

    $dryrun = 1
    if ($Apply) { $dryrun = 0 }

    if ([string]::IsNullOrWhiteSpace($ModDir)) {
        Write-UsageConclusion 'clean' $dryrun 'moddir-required'
        return 1
    }

    $root = ''
    try { $root = [IO.Path]::GetFullPath($ModDir) } catch { $root = '' }
    if ($root -eq '' -or -not (Test-Path -LiteralPath $root -PathType Container)) {
        Write-UsageConclusion 'clean' $dryrun 'moddir-not-dir'
        return 1
    }
    if ($root.Length -gt 3 -and $root.EndsWith('\')) { $root = $root.Substring(0, $root.Length - 1) }

    if ($Apply -and ($root -match '^[Kk]:')) {
        Write-UsageConclusion 'clean' $dryrun 'k-readonly'
        return 1
    }

    $child = Join-Path $PSScriptRoot 'clear-fluid-bins.ps1'
    if (-not (Test-Path -LiteralPath $child -PathType Leaf)) {
        Write-UsageConclusion 'clean' $dryrun 'child-missing'
        return 1
    }

    $psExe = Join-Path $PSHOME 'powershell.exe'
    if (-not (Test-Path -LiteralPath $psExe -PathType Leaf)) { $psExe = 'powershell.exe' }

    $childArgs = @('-NoProfile', '-ExecutionPolicy', 'Bypass', '-File', $child, '-ModDir', $root)
    if (-not $Apply) { $childArgs += '-DryRun' }

    $out = @()
    $childCode = -1
    $oldEap = $ErrorActionPreference
    $ErrorActionPreference = 'Continue'
    try {
        $out = @(& $psExe @childArgs 2>&1)
        $childCode = $LASTEXITCODE
    }
    catch {
        $ErrorActionPreference = $oldEap
        Write-Line ('ERROR: ' + $_.Exception.Message)
        Write-UsageConclusion 'clean' $dryrun 'child-start-failed'
        return 1
    }
    finally {
        $ErrorActionPreference = $oldEap
    }

    $lines = @()
    foreach ($o in $out) {
        $s = ([string]$o).TrimEnd()
        if ($s.Length -gt 0) { $lines += $s }
    }
    foreach ($l in $lines) { Write-Line $l }

    if ($lines.Count -eq 0) {
        Write-Line (Format-CleanConclusion $dryrun 'na' 'na' 2 'no-output')
        return 2
    }

    $last = $lines[$lines.Count - 1]
    $moved = 'na'
    $remaining = 'na'
    $remainingKnown = $false
    $assertion = $false

    if ($childCode -eq 0) {
        if ($last -match '^No matching \.bin found under .+$') {
            $moved = 0
            $remaining = 0
            $remainingKnown = $true
            $assertion = $true
        }
        elseif ((-not $Apply) -and ($last -match '^Would move (\d+) file\(s\), total [\d.,]+ MB$')) {
            $moved = [int]$Matches[1]
            $remaining = 'na'
            $assertion = $true
        }
        elseif ($Apply -and ($last -match '^Remaining matching \.bin: (\d+)$')) {
            $remaining = [int]$Matches[1]
            $remainingKnown = $true
            foreach ($l in $lines) {
                if ($l -match '^Moved (\d+) of (\d+) matching file\(s\) to Recycle Bin$') {
                    $moved = [int]$Matches[1]
                }
            }
            if ($remaining -eq 0) { $assertion = $true }
        }
    }

    $code = 0
    $reason = ''
    if ($childCode -eq 0) {
        if ($remainingKnown -and [int]$remaining -gt 0) {
            $code = 2
            $reason = 'remaining-not-zero'
        }
        elseif (-not $assertion) {
            $code = 2
            $reason = 'assert-last-line'
        }
    }
    elseif ($childCode -eq 1) {
        $code = 1
        $reason = 'child-exit-1'
    }
    elseif ($childCode -eq 2) {
        $code = 2
        $reason = 'child-exit-2'
    }
    else {
        $code = 2
        $reason = 'child-exit-' + $childCode
    }

    Write-Line (Format-CleanConclusion $dryrun $moved $remaining $code $reason)
    return $code
}

# ---------------------------------------------------------------------------
# verify（只读，导出后）
# ---------------------------------------------------------------------------
function Invoke-Verify {
    param([string]$ModDir, [string[]]$Ini, [string]$Baseline)

    $issues = New-IssueList
    $script:DisplayRoot = ''
    $root = ''
    $hasIni = ($null -ne $Ini -and $Ini.Count -gt 0)

    if ([string]::IsNullOrWhiteSpace($ModDir) -and -not $hasIni) {
        Write-UsageConclusion 'verify' 1 'moddir-or-ini-required'
        return 1
    }

    if (-not [string]::IsNullOrWhiteSpace($ModDir)) {
        $root = Resolve-ModDirPath $issues $ModDir
        if ($issues.UsageCode -ne 0) {
            Write-UsageConclusion 'verify' 1 $issues.Reason
            return 1
        }
        $script:DisplayRoot = $root
    }
    elseif ($hasIni) {
        $firstFull = ''
        try { $firstFull = [IO.Path]::GetFullPath($Ini[0]) } catch { $firstFull = '' }
        if ($firstFull.Length -gt 0) { $script:DisplayRoot = Split-Path -Parent $firstFull }
    }

    $iniFiles = @(Resolve-IniFiles $issues $root $Ini)
    if ($issues.UsageCode -ne 0) {
        Write-UsageConclusion 'verify' 1 $issues.Reason
        return 1
    }

    $baseKeys = -1
    $bpath = ''
    if (-not [string]::IsNullOrWhiteSpace($Baseline)) {
        try { $bpath = [IO.Path]::GetFullPath($Baseline) } catch { $bpath = '' }
        if ($bpath -eq '' -or -not (Test-Path -LiteralPath $bpath -PathType Leaf)) {
            Write-UsageConclusion 'verify' 1 'baseline-missing'
            return 1
        }
        $bdoc = Read-IniDocument $bpath
        if ($bdoc.Error -ne '') {
            Write-UsageConclusion 'verify' 1 'baseline-unreadable'
            return 1
        }
        $bsecs = @(Find-Sections $bdoc.Lines)
        $baseKeys = 0
        foreach ($s in $bsecs) {
            if ($s.Name -match '^key') { $baseKeys++ }
        }
        if ($baseKeys -eq 0) {
            Write-UsageConclusion 'verify' 1 'baseline-no-keys'
            return 1
        }
    }

    $sectionMap = @{}
    $docs = New-Object System.Collections.ArrayList
    $hotkeys = @{}
    $varSet = @{}
    $allRuns = New-Object System.Collections.ArrayList
    $keyTotal = 0

    foreach ($f in $iniFiles) {
        $doc = Read-IniDocument $f
        if ($doc.Error -ne '') {
            Add-Fail $issues 'V0' $f 0 ('cannot read: ' + $doc.Error)
            continue
        }
        [void]$docs.Add($doc)

        $sections = @(Find-Sections $doc.Lines)
        Add-Sections $sectionMap $doc $sections
        Collect-VariableSets $doc $varSet
        Test-PreambleDepth $issues $doc $sections 'V5'
        Test-DuplicateDraws $issues $doc $sections 'V4'

        $fileKeys = 0
        foreach ($sec in $sections) {
            $runs = New-Object System.Collections.ArrayList
            $stats = Measure-SectionDepth $doc.Lines $sec.BodyStart $sec.BodyEnd $runs
            if ($stats.Depth -ne 0 -or $stats.Negative) {
                Add-Fail $issues 'V5' $doc.Path ($sec.Header + 1) ('section [' + $sec.Name + '] if/endif unbalanced depth=' + $stats.Depth + ' negative=' + ([string]$stats.Negative))
            }
            foreach ($r in $runs) {
                [void]$allRuns.Add([pscustomobject]@{
                        Path    = $doc.Path
                        Line    = $r.Line
                        Vars    = $r.Vars
                        Section = $sec.Name
                    })
            }
            if ($sec.Name -match '^key') {
                $fileKeys++
                Test-KeySection $issues $doc $sec 'V6' $hotkeys
            }
        }
        $keyTotal += $fileKeys
        if ($fileKeys -gt 0) {
            Add-Info $issues 'V2' $doc.Path 0 ('key sections = ' + $fileKeys)
        }
    }

    Test-DuplicateSections $issues $sectionMap 'V4'
    $d3dxStart = $root
    if ([string]::IsNullOrWhiteSpace($d3dxStart) -and $iniFiles.Count -gt 0) {
        $d3dxStart = Split-Path -Parent ([IO.Path]::GetFullPath($iniFiles[0]))
    }
    $d3dx = Get-D3dxIniPath $d3dxStart
    $external = Get-IncludeSectionNames $d3dx
    Test-RunRefs $issues $docs $sectionMap 'V3' $external ($d3dx -ne '')

    # V8：run 在 if 体内始终 WARN；条件变量从未赋值/声明再加一条
    # 只在「扫整棵树」或多文件时判 never-set；单文件片段（如 Probe.ini）里的变量
    # 可能由宿主 ini 赋值，单文件判断会误报。
    $allowNeverSet = (-not $hasIni) -or ($iniFiles.Count -gt 1)
    foreach ($r in $allRuns) {
        Add-Warn $issues 'V8' $r.Path ($r.Line + 1) ('run inside if [' + $r.Section + ']')
        if ($allowNeverSet -and $r.Vars.Count -gt 0) {
            $missingVars = New-Object System.Collections.ArrayList
            foreach ($v in $r.Vars) {
                if (-not $varSet.ContainsKey($v)) { [void]$missingVars.Add('$' + $v) }
            }
            if ($missingVars.Count -gt 0) {
                Add-Warn $issues 'V8' $r.Path ($r.Line + 1) ('if-var-never-set ' + ($missingVars -join ',') + ' [' + $r.Section + ']')
            }
        }
    }

    # V7：同一热键（token 集合精确相等）绑两次
    foreach ($k in ($hotkeys.Keys | Sort-Object)) {
        $list = $hotkeys[$k]
        if ($list.Count -gt 1) {
            Add-Warn $issues 'V7' '' 0 ('hotkey "' + $k + '" bound ' + $list.Count + ' times: ' + ($list -join '; '))
        }
    }

    # V2：只比 Key 段总数
    if ($baseKeys -ge 0) {
        if ($keyTotal -lt $baseKeys) {
            Add-Fail $issues 'V2' $bpath 0 ('key sections ' + $keyTotal + ' < baseline ' + $baseKeys)
        }
        elseif ($keyTotal -gt $baseKeys) {
            Add-Warn $issues 'V2' $bpath 0 ('key sections ' + $keyTotal + ' > baseline ' + $baseKeys)
        }
    }

    Print-Issues $issues

    $code = 0
    if ($issues.UsageCode -ne 0) { $code = 1 }
    elseif ($issues.Fails.Count -gt 0) { $code = 2 }

    Write-Line (Format-VerifyConclusion $code $iniFiles.Count $keyTotal $issues.Fails.Count $issues.Warns.Count $issues.Reason $baseKeys)
    return $code
}

# ---------------------------------------------------------------------------
# all：check -> clean（默认 DryRun）-> verify，任一步非 0 停
# ---------------------------------------------------------------------------
function Invoke-All {
    param([string]$ModDir, [string[]]$Ini, [string]$Baseline, [bool]$Apply, [bool]$Compile)

    $step = 'check'
    try {
        $c = Invoke-Check $ModDir $Ini $Compile
        if ($c -ne 0) {
            Write-Line ('ALL FAIL at=check code=' + $c)
            return $c
        }
        $step = 'clean'
        $c = Invoke-Clean $ModDir $Apply
        if ($c -ne 0) {
            Write-Line ('ALL FAIL at=clean code=' + $c)
            return $c
        }
        $step = 'verify'
        $c = Invoke-Verify $ModDir $Ini $Baseline
        if ($c -ne 0) {
            Write-Line ('ALL FAIL at=verify code=' + $c)
            return $c
        }
        Write-Line 'ALL OK check=0 clean=0 verify=0'
        return 0
    }
    catch {
        Write-Line ('ERROR: ' + $_.Exception.Message)
        Write-Line ([string]$_.ScriptStackTrace)
        Write-Line ('ALL FAIL at=' + $step + ' code=3')
        return 3
    }
}

# ---------------------------------------------------------------------------
# 分发
# ---------------------------------------------------------------------------
$cmd = ''
if (-not [string]::IsNullOrWhiteSpace($Command)) {
    $cmd = $Command.Trim().ToLowerInvariant()
}

if ($cmd -ne 'check' -and $cmd -ne 'clean' -and $cmd -ne 'verify' -and $cmd -ne 'all') {
    Write-Line 'usage: mod-pipeline.ps1 <check|clean|verify|all> -ModDir <dir> [-Ini <file>[,<file>]] [-Baseline <file>] [-Apply] [-Compile]'
    Write-Line 'CHECK FAIL code=1 files=0 entries=0 bins=0 fails=0 warns=0 reason=bad-command'
    exit 1
}

$exitCode = 0
$hasIni = ($null -ne $Ini -and $Ini.Count -gt 0)
$dryrunFlag = 1
if ($Apply) { $dryrunFlag = 0 }

try {
    if ($cmd -eq 'check') {
        if ($Apply -or (-not [string]::IsNullOrWhiteSpace($Baseline))) {
            Write-Line 'ERROR: check does not accept -Apply / -Baseline'
            Write-UsageConclusion 'check' 1 'bad-option-combo'
            $exitCode = 1
        }
        else {
            $exitCode = Invoke-Check $ModDir $Ini ([bool]$Compile)
        }
    }
    elseif ($cmd -eq 'clean') {
        if ($hasIni -or (-not [string]::IsNullOrWhiteSpace($Baseline)) -or $Compile) {
            Write-Line 'ERROR: clean does not accept -Ini / -Baseline / -Compile'
            Write-UsageConclusion 'clean' $dryrunFlag 'bad-option-combo'
            $exitCode = 1
        }
        else {
            $exitCode = Invoke-Clean $ModDir ([bool]$Apply)
        }
    }
    elseif ($cmd -eq 'verify') {
        if ($Apply -or $Compile) {
            Write-Line 'ERROR: verify does not accept -Apply / -Compile'
            Write-UsageConclusion 'verify' 1 'bad-option-combo'
            $exitCode = 1
        }
        else {
            $exitCode = Invoke-Verify $ModDir $Ini $Baseline
        }
    }
    else {
        $exitCode = Invoke-All $ModDir $Ini $Baseline ([bool]$Apply) ([bool]$Compile)
    }
}
catch {
    Write-Line ('ERROR: ' + $_.Exception.Message)
    Write-Line ([string]$_.ScriptStackTrace)
    Write-ErrorConclusion $cmd
    $exitCode = 3
}

exit $exitCode
