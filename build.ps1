# 一键打包：把源码变成可以双击的 exe
#
#   powershell -ExecutionPolicy Bypass -File build.ps1
#
# 会依次做这几件事：
#   1. 找一个能用的 Python（3.10 以上）
#   2. 建一个干净的虚拟环境 .venv
#   3. 装依赖（requirements.txt）
#   4. 跑一遍离线自检，确认引擎是好的
#   5. 画图标（d20）
#   6. 用 PyInstaller 打包成 dist\COC跑团引擎\COC跑团引擎.exe
#
# 想跳过自检： build.ps1 -SkipTests
# 想从头重来： build.ps1 -Clean

[CmdletBinding()]
param(
    [switch]$SkipTests,
    [switch]$Clean
)

$ErrorActionPreference = 'Stop'
$root = $PSScriptRoot
Set-Location $root

function Say($msg) { Write-Host "`n=== $msg ===" -ForegroundColor Cyan }
function Ok($msg)  { Write-Host "  [OK] $msg" -ForegroundColor Green }
function Bad($msg) { Write-Host "  [!!] $msg" -ForegroundColor Red }

Say "1/6 找 Python"
$py = $null
foreach ($cand in @('py -3', 'python')) {
    $parts = $cand.Split(' ')
    $exe = Get-Command $parts[0] -ErrorAction SilentlyContinue
    if (-not $exe) { continue }
    try {
        $ver = & $parts[0] $parts[1..($parts.Length-1)] -c "import sys;print('%d.%d'%sys.version_info[:2])" 2>$null
        $major, $minor = $ver.Split('.')
        if ([int]$major -eq 3 -and [int]$minor -ge 10) {
            $py = @{ cmd = $parts[0]; args = $parts[1..($parts.Length-1)]; ver = $ver }
            break
        }
    } catch { }
}
if (-not $py) {
    Bad "没找到 Python 3.10 或以上。装一个再来：https://www.python.org/downloads/"
    exit 1
}
Ok "Python $($py.ver)（$($py.cmd)）"

$venv = Join-Path $root '.venv'
$vpy  = Join-Path $venv 'Scripts\python.exe'

if ($Clean -and (Test-Path $venv)) {
    Say "清理旧的 .venv"
    Remove-Item -Recurse -Force $venv
    Ok "已删除"
}

Say "2/6 建虚拟环境"
if (Test-Path $vpy) {
    Ok ".venv 已存在，直接用"
} else {
    & $py.cmd @($py.args) -m venv $venv
    if (-not (Test-Path $vpy)) {
        Bad "建虚拟环境失败。如果报 ensurepip 的错，试着先跑： py -3 -m ensurepip --upgrade"
        exit 1
    }
    Ok "已创建 $venv"
}

Say "3/6 装依赖"
& $vpy -m pip install --upgrade pip --quiet
& $vpy -m pip install -r (Join-Path $root 'requirements.txt') --quiet
if ($LASTEXITCODE -ne 0) { Bad "依赖没装上"; exit 1 }
Ok "依赖就绪"

Say "4/6 跑离线自检（不联网、不花钱）"
if ($SkipTests) {
    Write-Host "  （已跳过）" -ForegroundColor DarkGray
} else {
    & $vpy (Join-Path $root 'tools\selftest_offline.py') 3
    if ($LASTEXITCODE -ne 0) {
        Bad "自检没过。上面有失败项，修完再打包。"
        Write-Host "  想强行打包可以加 -SkipTests" -ForegroundColor DarkGray
        exit 1
    }
    Ok "自检通过"
}

Say "5/6 画图标"
& $vpy (Join-Path $root 'tools\make_icon.py')
Ok "图标已生成"

Say "6/6 打包"
& $vpy (Join-Path $root 'tools\build_exe.py')
if ($LASTEXITCODE -ne 0) { Bad "打包失败"; exit 1 }

$exe = Join-Path $root 'dist\COC跑团引擎\COC跑团引擎.exe'
if (Test-Path $exe) {
    $mb = [math]::Round((Get-ChildItem (Join-Path $root 'dist\COC跑团引擎') -Recurse -File |
          Measure-Object -Property Length -Sum).Sum / 1MB, 1)
    Write-Host ""
    Write-Host "打包完成" -ForegroundColor Green
    Write-Host "  双击这个： $exe"
    Write-Host "  整个文件夹一起拷走才能用，体积约 $mb MB"
    Write-Host ""
    Write-Host "  只想要一个干净的空环境（不带演示数据），可以先删掉 data 目录再运行。" -ForegroundColor DarkGray
} else {
    Bad "没找到 exe，打包可能出问题了"
    exit 1
}
