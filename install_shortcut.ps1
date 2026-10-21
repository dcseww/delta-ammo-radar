# 三角洲行动 · 子弹物价监控 —— 桌面快捷方式安装器
#
# 作用：在桌面（可选开始菜单 / 开机启动项）放一个带图标的快捷方式，
#       双击即"后台拉起前端+后端 → 自动打开看板"。
#
# 用法（本机执行策略禁止直接跑脚本，用一次性绕过）：
#     powershell -NoProfile -ExecutionPolicy Bypass -File .\install_shortcut.ps1
#     .\install_shortcut.ps1 -Visible       # 改成带控制台的启动方式（出错时窗口停住）
#     .\install_shortcut.ps1 -StartMenu     # 额外放一个到开始菜单
#     .\install_shortcut.ps1 -Startup       # 额外放一个到开机启动项（登录后自动启动）
#     .\install_shortcut.ps1 -Remove        # 删除上面所有位置里的快捷方式
#
# 默认（静默模式）快捷方式指向：wscript.exe "启动子弹监控.vbs"
#   → 不闪黑框，失败会弹窗。
# -Visible 模式指向：cmd /c "启动子弹监控.bat"
#   → 有一个控制台窗口，能看到启动过程的每一行输出。

param(
    [switch]$Visible,
    [switch]$StartMenu,
    [switch]$Startup,
    [switch]$Remove,
    [string]$Name = "子弹物价监控"
)

$ErrorActionPreference = "Stop"
$Root = $PSScriptRoot
$Icon = Join-Path $Root "frontend\assets\app.ico"
$Vbs = Join-Path $Root "启动子弹监控.vbs"
$Bat = Join-Path $Root "启动子弹监控.bat"
$Description = "三角洲行动 · 子弹物价监控 —— 一键启动前后端并打开看板"

# ── 要安装到的位置 ──────────────────────────────────────────────────────
$targets = New-Object System.Collections.Generic.List[object]
$targets.Add([pscustomobject]@{ Label = "桌面"; Dir = [Environment]::GetFolderPath("Desktop") })
if ($StartMenu -or $Remove) {
    $targets.Add([pscustomobject]@{ Label = "开始菜单"; Dir = [Environment]::GetFolderPath("Programs") })
}
if ($Startup -or $Remove) {
    $targets.Add([pscustomobject]@{ Label = "开机启动"; Dir = [Environment]::GetFolderPath("Startup") })
}

if (-not $Remove) {
    foreach ($path in @($Vbs, $Bat, $Icon)) {
        if (-not (Test-Path -LiteralPath $path)) {
            Write-Host ("[FAIL] 缺少文件：{0}" -f $path) -ForegroundColor Red
            Write-Host "       请先跑 python tools\make_icon.py 生成图标。"
            exit 1
        }
    }
}

$shell = New-Object -ComObject WScript.Shell
$done = 0

foreach ($target in $targets) {
    $lnkPath = Join-Path $target.Dir ("{0}.lnk" -f $Name)

    if ($Remove) {
        if (Test-Path -LiteralPath $lnkPath) {
            Remove-Item -LiteralPath $lnkPath -Force
            Write-Host ("[OK] 已删除（{0}）：{1}" -f $target.Label, $lnkPath) -ForegroundColor Green
            $done++
        } else {
            Write-Host ("[--] {0}：没有快捷方式" -f $target.Label)
        }
        continue
    }

    if (-not (Test-Path -LiteralPath $target.Dir)) {
        Write-Host ("[!!] {0}：目录不存在，跳过：{1}" -f $target.Label, $target.Dir) -ForegroundColor Yellow
        continue
    }

    $shortcut = $shell.CreateShortcut($lnkPath)
    if ($Visible) {
        $shortcut.TargetPath = $env:ComSpec
        $shortcut.Arguments = '/c "' + $Bat + '"'
    } else {
        $shortcut.TargetPath = Join-Path $env:WINDIR "System32\wscript.exe"
        $shortcut.Arguments = '"' + $Vbs + '"'
    }
    $shortcut.WorkingDirectory = $Root
    $shortcut.IconLocation = ("{0},0" -f $Icon)
    $shortcut.Description = $Description
    $shortcut.WindowStyle = 1
    $shortcut.Save()

    # 立刻回读验证，避免"保存了但不生效"
    $check = $shell.CreateShortcut($lnkPath)
    Write-Host ("[OK] 已创建（{0}）：{1}" -f $target.Label, $lnkPath) -ForegroundColor Green
    Write-Host ("     目标：{0} {1}" -f $check.TargetPath, $check.Arguments) -ForegroundColor DarkGray
    Write-Host ("     图标：{0}" -f $check.IconLocation) -ForegroundColor DarkGray
    $done++
}

Write-Host ""
if ($Remove) {
    Write-Host ("[OK] 共删除 {0} 个快捷方式" -f $done) -ForegroundColor Cyan
} elseif ($done -gt 0) {
    Write-Host ("[OK] 共创建 {0} 个快捷方式。双击即可启动（首次启动约 3~6 秒）。" -f $done) -ForegroundColor Cyan
    Write-Host "     想固定到任务栏：右键快捷方式 → 固定到任务栏。"
    if (-not $Startup) {
        Write-Host "     想开机自启：再执行一次 .\install_shortcut.ps1 -Startup"
    }
} else {
    Write-Host "[!!] 没有创建任何快捷方式" -ForegroundColor Yellow
}
