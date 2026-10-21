# 计划任务专用启动脚本：启动子弹物价监控服务（前台运行，由计划任务托管）
$py = 'C:\Users\cth\.dsh\dsh-runtimes\dsh-primary-runtime\dependencies\python\python.exe'
$root = 'D:\seek\子弹'
$debugLog = Join-Path $root 'logs\task_debug.log'
"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') script started" | Add-Content $debugLog
Set-Location $root
& $py -m uvicorn backend.app:app --host 127.0.0.1 --port 8787 --log-level info 1> (Join-Path $root 'logs\server.log') 2> (Join-Path $root 'logs\server.log.err')
$ec = $LASTEXITCODE
"$(Get-Date -Format 'yyyy-MM-dd HH:mm:ss') python exited: $ec" | Add-Content $debugLog
