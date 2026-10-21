# 三角洲行动 · 子弹物价监控 —— 前端 + 后端 一键启动
#
# 用法（在 PowerShell 里，本机执行策略禁止直接跑脚本，用一次性绕过）：
#     powershell -NoProfile -ExecutionPolicy Bypass -File .\start.ps1
#
# 常用：
#     .\start.ps1               # 一体模式：后端同时托管前端 → http://127.0.0.1:8787
#     .\start.ps1 -Dev          # 分离模式：后端 8787 + 前端静态服务器 8788（改前端不用重启后端）
#     .\start.ps1 -Status       # 查看前后端状态
#     .\start.ps1 -Stop         # 停止前后端
#     .\start.ps1 -Foreground   # 前台运行后端（Ctrl+C 停止）
#     .\start.ps1 -Dev -NoBrowser
#     .\start.ps1 -BackendPort 8790 -FrontendPort 8791
#
# 两种模式的区别
# --------------
# 一体模式：只有后端一个进程。FastAPI 在 "/" 上挂了 StaticFiles，
#           直接托管 frontend/ —— 这是日常自用的推荐方式。
# 分离模式：后端只出接口（8787），另起 scripts/serve_frontend.py 托管前端（8788），
#           它把 /api、/docs、/openapi.json 反向代理给后端。页面与接口依然同源，
#           app.js 里的相对路径一行都不用改，也没有跨域问题；
#           好处是改 HTML/CSS/JS 只要刷新浏览器，不用重启后端。
#
# 说明
# ----
# * 前端是纯静态页面：没有 npm、没有构建步骤，所以"启动前端"= 起一个静态文件服务器。
# * 刻意不依赖系统 PATH 里的 python，使用 DSH 运行时自带的解释器完整路径（可用 -Python 覆盖）。
# * 端口探测走 netstat（不依赖 Get-NetTCPConnection，也不依赖进程可见性），
#   所以 PID 文件残留、进程被外力杀掉时都不会误判。
# * 停止时只杀"看起来像本项目"的进程：PID 文件优先，其次按端口 + Server 头核对。
# * 本文件必须保存为 **UTF-8 带 BOM**。若重存成无 BOM 的 UTF-8，
#   PowerShell 5.1 会按 GBK 解码中文注释并抛语法错误。

param(
    [switch]$Dev,
    [switch]$Stop,
    [switch]$Status,
    [switch]$Foreground,
    [switch]$NoBrowser,
    [int]$BackendPort = 8787,
    [int]$FrontendPort = 8788,
    [string]$Python = "C:\Users\cth\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe"
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$LogDir = Join-Path $Root "logs"
$ServeScript = Join-Path $Root "scripts\serve_frontend.py"

$BackendUrl = "http://127.0.0.1:$BackendPort"
$FrontendUrl = "http://127.0.0.1:$FrontendPort"

$BackendLog = Join-Path $LogDir "backend.log"
$BackendErr = Join-Path $LogDir "backend.log.err"
$FrontendLog = Join-Path $LogDir "frontend.log"
$FrontendErr = Join-Path $LogDir "frontend.log.err"
$BackendPidFile = Join-Path $LogDir "backend.pid"
$FrontendPidFile = Join-Path $LogDir "frontend.pid"

# ── 基础工具 ────────────────────────────────────────────────────────────

function Get-PortPid {
    <# 返回监听该端口的进程号；没有则 $null。用 netstat，任何权限下都可用。 #>
    param([int]$Port)
    $pattern = "^\s*TCP\s+\S+:$Port\s+\S+\s+LISTENING\s+(\d+)\s*$"
    foreach ($line in @(netstat -ano -p TCP)) {
        if ($line -match $pattern) { return [int]$Matches[1] }
    }
    return $null
}

function Get-ServerHeader {
    param([string]$Url)
    try { return [string](Invoke-WebRequest $Url -UseBasicParsing -TimeoutSec 3).Headers["Server"] }
    catch { return $null }
}

function Test-Url {
    param([string]$Url)
    try {
        $r = Invoke-WebRequest $Url -UseBasicParsing -TimeoutSec 3
        return ($r.StatusCode -eq 200)
    } catch { return $false }
}

function Wait-Url {
    param([string]$Url, [int]$Seconds = 30)
    for ($i = 1; $i -le $Seconds; $i++) {
        if (Test-Url $Url) { return $true }
        Start-Sleep -Seconds 1
    }
    return $false
}

function Write-Tail {
    param([string]$Path, [int]$Lines = 20)
    if (Test-Path $Path) {
        Write-Host "     —— $Path 末尾 $Lines 行 ——" -ForegroundColor DarkGray
        Get-Content $Path -Tail $Lines -Encoding UTF8 | ForEach-Object { Write-Host "     $_" -ForegroundColor DarkGray }
    }
}

function Start-BackendProcess {
    # -u：不缓冲，日志立刻落盘（否则重定向下 stderr 是块缓冲，日志文件会长时间为空）
    # -X utf8：强制按 UTF-8 写日志，中文不会因为系统区域是 GBK 而乱码
    $argLine = "-u -X utf8 -m uvicorn backend.app:app --host 127.0.0.1 --port $BackendPort --log-level info"
    $proc = Start-Process -FilePath $Python -ArgumentList $argLine -WorkingDirectory $Root `
        -RedirectStandardOutput $BackendLog -RedirectStandardError $BackendErr `
        -PassThru -WindowStyle Hidden
    $proc.Id | Set-Content -Path $BackendPidFile -Encoding ASCII
    return $proc
}

function Start-FrontendProcess {
    $argLine = '-u -X utf8 "{0}" --port {1} --backend "{2}"' -f $ServeScript, $FrontendPort, $BackendUrl
    $proc = Start-Process -FilePath $Python -ArgumentList $argLine -WorkingDirectory $Root `
        -RedirectStandardOutput $FrontendLog -RedirectStandardError $FrontendErr `
        -PassThru -WindowStyle Hidden
    $proc.Id | Set-Content -Path $FrontendPidFile -Encoding ASCII
    return $proc
}

function Stop-Target {
    <# 停一个服务：PID 文件优先；再按端口找，并用 Server 头核对是不是本项目的进程。 #>
    param(
        [string]$Name,
        [int]$Port,
        [string]$PidFile,
        [string]$Url,
        [string]$ExpectServer
    )
    $killed = @()

    if (Test-Path $PidFile) {
        $saved = Get-Content $PidFile -ErrorAction SilentlyContinue
        if ($saved -match '^\d+$') {
            try {
                Stop-Process -Id ([int]$saved) -Force -ErrorAction Stop
                $killed += [int]$saved
            } catch { }
        }
        Remove-Item $PidFile -Force -ErrorAction SilentlyContinue
    }

    $byPort = Get-PortPid -Port $Port
    if ($byPort -and ($killed -notcontains $byPort)) {
        $server = Get-ServerHeader -Url $Url
        if ($ExpectServer -and $server -and ($server -notlike "*$ExpectServer*")) {
            Write-Host ("[!!] {0}：端口 {1} 上的进程 PID {2} 不像本项目（Server: {3}），已跳过" -f $Name, $Port, $byPort, $server) -ForegroundColor Yellow
        } else {
            try {
                Stop-Process -Id $byPort -Force -ErrorAction Stop
                $killed += $byPort
            } catch {
                Write-Host ("[!!] {0}：PID {1} 停止失败 —— {2}" -f $Name, $byPort, $_.Exception.Message) -ForegroundColor Yellow
                Write-Host "     （若在 DSH 沙箱里执行，沙箱可能无权结束该进程；请在自己的 PowerShell 窗口执行）" -ForegroundColor DarkGray
            }
        }
    }

    if ($killed.Count -gt 0) {
        Write-Host ("[OK] {0}：已停止（PID {1}）" -f $Name, ($killed -join ", ")) -ForegroundColor Green
    } else {
        Write-Host ("[--] {0}：没有在运行" -f $Name)
    }
}

function Show-Status {
    Write-Host ""
    Write-Host "  服务      端口    进程        接口            说明"
    Write-Host "  ----------------------------------------------------------------------------"

    $bPid = Get-PortPid -Port $BackendPort
    $bOk = Test-Url "$BackendUrl/api/health"
    $bPidText = if ($bPid) { "$bPid" } else { "-" }
    $bApiText = if ($bOk) { "200 OK" } else { "不可用" }
    $bNote = if ($bPid) { "后端（接口" + $(if ($Dev) { "；前端另起" } else { " + 前端" }) + "）" } else { "未运行" }
    Write-Host ("  后端      {0,-6}  {1,-10}  {2,-14}  {3}" -f $BackendPort, $bPidText, $bApiText, $bNote)

    $fPid = Get-PortPid -Port $FrontendPort
    $fOk = Test-Url "$FrontendUrl/"
    $fPidText = if ($fPid) { "$fPid" } else { "-" }
    $fApiText = if ($fOk) { "200 OK" } else { "不可用" }
    $fNote = if ($fPid) { "前端静态服务器（反代 → $BackendUrl）" } else { "未运行" }
    Write-Host ("  前端      {0,-6}  {1,-10}  {2,-14}  {3}" -f $FrontendPort, $fPidText, $fApiText, $fNote)
    Write-Host ""

    if ($bOk) {
        try {
            $h = Invoke-RestMethod "$BackendUrl/api/health" -TimeoutSec 5
            Write-Host ("     状态：{0}" -f $h.status)
            Write-Host ("     物品：{0} 条（可交易 {1}）" -f $h.ammo_counts.total, $h.ammo_counts.tradeable)
            Write-Host ("     价格点：{0}（判异 {1}）" -f $h.tick_counts.total, $h.tick_counts.suspect)
            # 刻意不打印 backfill_summary：Windows PowerShell 5.1 的 Invoke-RestMethod
            # 在没有 charset 的 application/json 上会按 Latin-1 解码，中文会变成乱码。
            # 这里只用数字字段，绕开这个坑。
            Write-Host ("     回补：{0}/{1} 个可交易物品（完整：{2}）" -f `
                $h.scheduler.backfill_coverage.covered, $h.scheduler.backfill_coverage.total, $h.scheduler.backfill_coverage.complete)
        } catch { }
    }

    if ($fOk -and -not $Dev) {
        Write-Host "     提示：8788 上有一个前端服务器在跑，而你当前是一体模式；不需要它可以执行 -Stop 收掉" -ForegroundColor DarkGray
    }
    Write-Host ("     看板：{0}" -f $(if ($fOk) { $FrontendUrl } else { $BackendUrl }))
    Write-Host ("     日志：{0} / {1}" -f $BackendErr, $FrontendErr)
    Write-Host ""
}

# ── 动作分发 ────────────────────────────────────────────────────────────

if ($Status) {
    Show-Status
    exit 0
}

if ($Stop) {
    Stop-Target -Name "前端" -Port $FrontendPort -PidFile $FrontendPidFile -Url $FrontendUrl -ExpectServer "df-ammo-frontend"
    Stop-Target -Name "后端" -Port $BackendPort -PidFile $BackendPidFile -Url "$BackendUrl/api/health" -ExpectServer "uvicorn"
    exit 0
}

if (-not (Test-Path $Python)) {
    Write-Host ("[FAIL] 找不到 Python 解释器：{0}" -f $Python) -ForegroundColor Red
    Write-Host "       请用 -Python <路径> 指定，或修改本文件里的默认值。"
    exit 1
}

if (-not (Test-Path $ServeScript)) {
    Write-Host ("[FAIL] 找不到前端服务器脚本：{0}" -f $ServeScript) -ForegroundColor Red
    exit 1
}

New-Item -ItemType Directory -Path $LogDir -Force | Out-Null
Set-Location $Root

# ── 前台模式：后端占住当前窗口 ──────────────────────────────────────────
if ($Foreground) {
    if ($Dev) {
        if (Get-PortPid -Port $FrontendPort) {
            Write-Host ("[OK] 前端已在运行（PID {0}），复用" -f (Get-PortPid -Port $FrontendPort)) -ForegroundColor Green
        } else {
            Write-Host ("启动前端静态服务器… → {0}" -f $FrontendUrl)
            $null = Start-FrontendProcess
            if (Wait-Url "$FrontendUrl/" 20) {
                Write-Host ("[OK] 前端就绪（PID {0}），改 frontend/ 下文件后刷新浏览器即可" -f (Get-PortPid -Port $FrontendPort)) -ForegroundColor Green
            } else {
                Write-Host "[FAIL] 前端 20 秒内未就绪" -ForegroundColor Red
                Write-Tail -Path $FrontendErr
                exit 1
            }
        }
    }
    Write-Host ("前台运行后端 → {0}   （Ctrl+C 停止）" -f $BackendUrl) -ForegroundColor Cyan
    & $Python -u -X utf8 -m uvicorn backend.app:app --host 127.0.0.1 --port $BackendPort --log-level info
    exit $LASTEXITCODE
}

# ── 1) 后端 ─────────────────────────────────────────────────────────────
$backendPid = Get-PortPid -Port $BackendPort
if ($backendPid) {
    Write-Host ("[OK] 后端已在运行（PID {0}，端口 {1}），直接复用" -f $backendPid, $BackendPort) -ForegroundColor Green
} else {
    Write-Host ("启动后端… → {0}" -f $BackendUrl)
    $null = Start-BackendProcess
    if (Wait-Url "$BackendUrl/api/health" 30) {
        Write-Host ("[OK] 后端就绪（PID {0}）" -f (Get-PortPid -Port $BackendPort)) -ForegroundColor Green
    } else {
        Write-Host "[FAIL] 后端 30 秒内未就绪" -ForegroundColor Red
        Write-Tail -Path $BackendErr
        exit 1
    }
}

# ── 2) 前端（分离模式才需要） ───────────────────────────────────────────
$frontendPid = Get-PortPid -Port $FrontendPort
if ($Dev) {
    if ($frontendPid) {
        Write-Host ("[OK] 前端已在运行（PID {0}，端口 {1}），直接复用" -f $frontendPid, $FrontendPort) -ForegroundColor Green
    } else {
        Write-Host ("启动前端静态服务器… → {0}" -f $FrontendUrl)
        $null = Start-FrontendProcess
        if (Wait-Url "$FrontendUrl/" 20) {
            Write-Host ("[OK] 前端就绪（PID {0}）" -f (Get-PortPid -Port $FrontendPort)) -ForegroundColor Green
        } else {
            Write-Host "[FAIL] 前端 20 秒内未就绪" -ForegroundColor Red
            Write-Tail -Path $FrontendErr
            exit 1
        }
    }
}

# ── 3) 打开浏览器 + 汇总 ────────────────────────────────────────────────
$entryUrl = if ($Dev) { $FrontendUrl } else { $BackendUrl }

if (-not $NoBrowser) {
    try { Start-Process $entryUrl } catch { }
}

Write-Host ""
if ($Dev) {
    Write-Host "── 前后端已就绪（分离模式） ──────────────────────────────────" -ForegroundColor Cyan
    Write-Host ("     看板（前端）  {0}" -f $FrontendUrl)
    Write-Host ("     接口（后端）  {0}" -f $BackendUrl)
    Write-Host ("     接口文档      {0}/docs" -f $BackendUrl)
    Write-Host "     改 frontend/ 下的 HTML/CSS/JS 后刷新浏览器即可，不用重启后端"
    Write-Host "     前端日志      $(if (Test-Path $FrontendLog) { $FrontendLog } else { $FrontendErr })"
} else {
    Write-Host "── 服务已就绪（一体模式：后端同时托管前端） ──────────────────" -ForegroundColor Cyan
    Write-Host ("     看板          {0}" -f $BackendUrl)
    Write-Host ("     接口文档      {0}/docs" -f $BackendUrl)
    Write-Host "     想改前端不重启后端？用 -Dev 走分离模式"
}
Write-Host ("     后端日志      {0}" -f $BackendErr)
Write-Host "     停止 / 状态   .\start.ps1 -Stop   /   .\start.ps1 -Status"
Write-Host ""
