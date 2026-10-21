# 三角洲行动 · 子弹物价监控 —— 一键启动
#
# 用法（在 PowerShell 里）：
#     .\run.ps1              # 后台启动服务（写完 PID 就返回，服务独立存活）
#     .\run.ps1 -Foreground  # 前台运行，日志直接打屏（排障时用，Ctrl+C 停止）
#     .\run.ps1 -Stop        # 停止服务
#     .\run.ps1 -Status      # 查看服务状态
#
# ⚠️ 重要：不要把本脚本放进"会被回收的后台任务"里跑
# ------------------------------------------------------------------
# 实测踩过：在会被宿主回收的后台任务中调用本脚本时，宿主终止包装进程会
# **连带杀掉 Start-Process 起的服务**。现象是服务跑了 50 分钟后神秘消失，
# 而 PID 文件还残留着已死进程的编号，`-Status` 因此误报"运行中"。
#
# 正确姿势：让启动动作**快速结束**。本脚本默认模式就是如此 —— 启动并在
# 服务真正可用后立即返回，之后服务独立存活。若必须放进长任务里，请务必
# 在任务里只调用一次并立刻返回（不要在同一个任务里 sleep/等待）。
#
# ⚠️ 另外两点（本机特定）
# ----------------------
# 1. 本机 PowerShell 执行策略禁止运行脚本，直接 `.\run.ps1` 会报
#    "禁止运行脚本"。用一次性绕过：
#        powershell -NoProfile -ExecutionPolicy Bypass -File .\run.ps1
# 2. 本文件必须保存为 **UTF-8 带 BOM**。若重存成无 BOM 的 UTF-8，
#    PowerShell 5.1 会按 GBK 解码中文注释并抛语法错误。

param(
    [switch]$Foreground,
    [switch]$Stop,
    [switch]$Status
)

$ErrorActionPreference = "Stop"
# 控制台与输出统一按 UTF-8 处理。
# 不设这几行时，PowerShell 5.1 的 Invoke-RestMethod 会按系统 ANSI 代码页
# （中文机器上是 GBK）解码 JSON 响应，导致接口返回的中文全部变成乱码。
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8
$OutputEncoding = [System.Text.Encoding]::UTF8
$Root = $PSScriptRoot
$Py = "C:\Users\cth\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
$LogDir = Join-Path $Root "logs"
$LogFile = Join-Path $LogDir "server.log"
$ErrFile = Join-Path $LogDir "server.log.err"
$PidFile = Join-Path $LogDir "server.pid"
$Url = "http://127.0.0.1:8787"

function Get-RadarPid {
    if (Test-Path $PidFile) {
        $saved = Get-Content $PidFile -ErrorAction SilentlyContinue
        if ($saved -and (Get-Process -Id $saved -ErrorAction SilentlyContinue)) { return [int]$saved }
    }
    return $null
}

function Get-RadarPidByPort {
    $conn = Get-NetTCPConnection -LocalPort 8787 -State Listen -ErrorAction SilentlyContinue |
        Select-Object -First 1
    if ($conn) { return [int]$conn.OwningProcess }
    return $null
}

if ($Stop) {
    $targets = @((Get-RadarPid), (Get-RadarPidByPort)) | Where-Object { $_ } | Select-Object -Unique
    $killed = @()
    foreach ($targetId in $targets) {
        try { Stop-Process -Id $targetId -Force -ErrorAction Stop; $killed += $targetId } catch { }
    }
    Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    if ($killed.Count) { Write-Host "[OK] 服务已停止（PID $($killed -join ', ')）" }
    else { Write-Host "[--] 没有正在运行的雷达服务" }
    exit 0
}

if ($Status) {
    # 同时看 PID 文件与端口：进程被外力杀掉时 PID 文件会残留，
    # 只看文件会误报"运行中"（实测遇到过）。
    $byFile = Get-RadarPid
    $byPort = Get-RadarPidByPort
    if ($byPort) {
        Write-Host "[OK] 服务运行中（PID $byPort）→ $Url"
        if ($byFile -and $byFile -ne $byPort) {
            Write-Host "[!!] 注意：PID 文件记录的是 $byFile，与实际监听进程 $byPort 不一致"
        }
    } else {
        Write-Host "[--] 服务未运行"
        if ($byFile) { Write-Host "[!!] PID 文件残留（记录 $byFile，但该进程已不存在）" }
    }
    try {
        # 故意不用 Invoke-RestMethod：PowerShell 5.1 在响应头没有 charset 时
        # 会按 Latin-1 解码 JSON，中文全部变乱码。手动取字节再按 UTF-8 解码才可靠。
        $resp = Invoke-WebRequest "$Url/api/health" -TimeoutSec 5 -UseBasicParsing
        $json = [System.Text.Encoding]::UTF8.GetString($resp.RawContentStream.ToArray())
        $h = $json | ConvertFrom-Json
        Write-Host "     状态：$($h.status)"
        Write-Host "     物品：$($h.ammo_counts.total) 条（可交易 $($h.ammo_counts.tradeable)）"
        Write-Host "     价格点：$($h.tick_counts.total)（判异 $($h.tick_counts.suspect)）"
        Write-Host "     回补：$($h.scheduler.backfill_summary)"
    } catch {
        Write-Host "     （无法访问接口）"
    }
    exit 0
}

if (-not (Test-Path $Py)) {
    Write-Host "[FAIL] 找不到 Python 解释器：$Py"
    Write-Host "       请修改 run.ps1 里的 `$Py 变量。"
    exit 1
}

if (Get-RadarPidByPort) {
    Write-Host "[!!] 端口 8787 已被占用，先执行 -Stop 再启动。"
    exit 1
}

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
Set-Location $Root

if ($Foreground) {
    Write-Host "前台启动 → $Url   （Ctrl+C 停止）"
    & $Py -m uvicorn backend.app:app --host 127.0.0.1 --port 8787 --log-level info
    exit $LASTEXITCODE
}

Write-Host "启动服务…"
$proc = Start-Process -FilePath $Py `
    -ArgumentList "-m", "uvicorn", "backend.app:app", "--host", "127.0.0.1", "--port", "8787", "--log-level", "info" `
    -WorkingDirectory $Root `
    -RedirectStandardOutput $LogFile `
    -RedirectStandardError $ErrFile `
    -PassThru -WindowStyle Hidden

$proc.Id | Set-Content $PidFile

# 等待服务真正可用（最多 30 秒），然后就返回 —— 不长期占用调用方
$ready = $false
for ($i = 0; $i -lt 30; $i++) {
    Start-Sleep -Seconds 1
    try {
        $null = Invoke-RestMethod "$Url/api/health" -TimeoutSec 3
        $ready = $true
        break
    } catch { }
}

if ($ready) {
    $live = Get-RadarPidByPort
    Write-Host "[OK] 服务已启动并可用（PID $live）"
    Write-Host "     看板：$Url"
    Write-Host "     接口文档：$Url/docs"
    Write-Host "     日志：$LogFile"
    Write-Host ""
    Write-Host "     若首次启动，会自动回补 7 天历史（85 个物品，约 2 分钟）；"
    Write-Host "     期间页面能看，但分位数要等回补完成才全部可用。"
    Write-Host "     查看进度：python scripts\check_backfill.py"
} else {
    Write-Host "[FAIL] 服务在 30 秒内未就绪，请查看日志："
    Write-Host "       $ErrFile"
    if (Test-Path $ErrFile) { Get-Content $ErrFile -Tail 20 -Encoding UTF8 }
    exit 1
}
