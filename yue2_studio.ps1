# 乐坊 YuE2 Studio 面板（独立项目）：起停**面板自己**，不碰 ComfyUI。
#   打开乐坊.cmd              -> 起面板 + 开浏览器
#   打开乐坊.cmd -NoBrowser   -> 只起面板
#   yue2_studio.ps1 -Stop      -> 只停面板
#
# 面板零第三方依赖（只用 Python 标准库）：拿系统里的 Python 3.12/3.13 直接跑，不需要 .venv。
# ComfyUI 在哪、用哪个 python 起它，写在同目录 studio.json（或环境变量 YUE2_COMFY_ROOT 等）。
# 点"生成"时面板会按那份配置把 ComfyUI 拉起来；ComfyUI 的停止在模型目录的 停止服务器.cmd。
param(
    [switch]$Stop,
    [switch]$NoBrowser,
    [int]$Port = 8190
)
$ErrorActionPreference = "Stop"
$PanelDir = $PSScriptRoot
$studioLog = Join-Path $PanelDir "studio.log"
$musicUrl = "http://127.0.0.1:$Port"

function Get-PanelProc {
    Get-CimInstance Win32_Process -Filter "Name = 'python.exe'" -ErrorAction SilentlyContinue |
        Where-Object { $_.CommandLine -like "*yue2_studio.py*" }
}

function Test-Panel {
    try { Invoke-RestMethod -Uri "$musicUrl/api/state" -TimeoutSec 3 | Out-Null; return $true }
    catch { return $false }
}

# 面板只用标准库，所以不建 venv：找系统里的 Python（可用 YUE2_PYTHON 覆盖）。
function Get-PanelPython {
    if ($env:YUE2_PYTHON -and (Test-Path $env:YUE2_PYTHON)) { return $env:YUE2_PYTHON }
    $venv = Join-Path $PanelDir ".venv\Scripts\python.exe"
    if (Test-Path $venv) { return $venv }
    $cands = @(
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python313\python.exe"),
        (Join-Path $env:LOCALAPPDATA "Programs\Python\Python312\python.exe")
    )
    foreach ($c in $cands) { if (Test-Path $c) { return $c } }
    $cmd = Get-Command python.exe -ErrorAction SilentlyContinue
    if ($cmd) { return $cmd.Source }
    Write-Host "找不到 Python 3.12/3.13。面板不依赖第三方库，但需要有个 Python 来跑服务。" -ForegroundColor Red
    return $null
}

if ($Stop) {
    $procs = @(Get-PanelProc)
    if ($procs.Count -gt 0) {
        foreach ($p in $procs) { Stop-Process -Id $p.ProcessId -Force -ErrorAction SilentlyContinue }
        Write-Host ("已停止乐坊面板 (PID " + (($procs | ForEach-Object { $_.ProcessId }) -join ", ") + ")") -ForegroundColor Green
    } else {
        Write-Host "乐坊面板没在跑。" -ForegroundColor DarkGray
    }
    return
}

$py = Get-PanelPython
if (-not $py) { exit 1 }

if (Test-Panel) {
    Write-Host "乐坊已在运行: $musicUrl" -ForegroundColor DarkGray
} else {
    Start-Process -FilePath $py -ArgumentList "yue2_studio.py", "--port", "$Port" -WorkingDirectory $PanelDir `
        -WindowStyle Hidden -RedirectStandardOutput $studioLog -RedirectStandardError "$studioLog.err"
    $t0 = Get-Date
    while (((Get-Date) - $t0).TotalSeconds -lt 30) {
        if (Test-Panel) { break }
        Start-Sleep -Milliseconds 500
    }
    if (-not (Test-Panel)) {
        Write-Host "面板没起来，看日志: $studioLog.err" -ForegroundColor Red
        exit 1
    }
    Write-Host "乐坊面板已启动: $musicUrl" -ForegroundColor Green
}

Write-Host "配置: $(Join-Path $PanelDir 'studio.json')" -ForegroundColor DarkGray
Write-Host "乐坊地址: $musicUrl   (关掉这个窗口不影响它；停: yue2_studio.ps1 -Stop)" -ForegroundColor Cyan

if (-not $NoBrowser) { Start-Process $musicUrl }
