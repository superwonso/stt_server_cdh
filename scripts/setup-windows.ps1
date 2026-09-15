#Requires -Version 5.1
[CmdletBinding()]
param([string]$Python, [switch]$SkipModels, [switch]$SkipTestInit)
$ErrorActionPreference = 'Stop'
if ([Environment]::OSVersion.Platform -ne 'Win32NT') { throw 'Native Windows is required.' }
$Project = Split-Path -Parent $PSScriptRoot
$Service = Split-Path -Parent $Project
$Uv = Join-Path $Service 'tools\uv\uv.exe'
if (-not $Python) { $Python = Join-Path $Service 'tools\python312\python.exe' }
if (-not (Test-Path -LiteralPath $Python)) { throw 'Specify -Python with an installed Python 3.12 executable.' }
if (-not (Test-Path -LiteralPath $Uv)) { throw 'Place the verified official uv Windows binary in tools\uv\uv.exe.' }
$Venv = Join-Path $Project '.venv-win'
$Runtime = Join-Path $Venv 'Scripts\python.exe'
$Cache = Join-Path $Service 'work\uv-cache'
& $Python -c "import sys; assert sys.version_info[:2] == (3,12), 'Python 3.12 required'"
if ($LASTEXITCODE -ne 0) { throw 'Python version check failed.' }
if (Test-Path -LiteralPath $Venv) {
    if (-not (Test-Path -LiteralPath $Runtime)) { throw 'An incompatible .venv-win exists; preserved for review.' }
    & $Runtime -c 'import sys; assert sys.version_info[:2] == (3,12) and sys.prefix != sys.base_prefix'
    if ($LASTEXITCODE -ne 0) { throw 'Existing Windows environment is incompatible; preserved.' }
} else {
    & $Uv --cache-dir $Cache venv --python $Python $Venv
    if ($LASTEXITCODE -ne 0) { throw 'Virtual environment creation failed.' }
}
& $Uv --cache-dir $Cache pip install --python $Runtime --link-mode copy --index-url https://download.pytorch.org/whl/cu126 torch==2.11.0 torchvision==0.26.0 torchaudio==2.11.0
if ($LASTEXITCODE -ne 0) { throw 'Official CUDA wheel installation failed.' }
$InstallArgs = @('--cache-dir',$Cache,'pip','install','--python',$Runtime,'--link-mode','copy','-r',(Join-Path $Project 'server\requirements-windows.txt'))
$Wheels = Join-Path $Service 'work\wheels'
if (Test-Path -LiteralPath $Wheels) { $InstallArgs += @('--find-links',$Wheels) }
& $Uv @InstallArgs
if ($LASTEXITCODE -ne 0) { throw 'Project dependency installation failed.' }
& $Uv --cache-dir $Cache pip check --python $Runtime
if ($LASTEXITCODE -ne 0) { throw 'Dependency verification failed.' }
& $Runtime -c "import torch; assert torch.version.cuda == '12.6'; assert torch.cuda.is_available(); x=torch.ones(32,32,device='cuda'); assert bool(torch.isfinite(x@x).all()); torch.cuda.synchronize(); print('CUDA tensor verification passed')"
if ($LASTEXITCODE -ne 0) { throw 'CUDA verification failed.' }
if (-not $SkipModels) {
    & $Runtime (Join-Path $PSScriptRoot 'setup_models.py')
    if ($LASTEXITCODE -ne 0) { throw 'Pinned model download failed.' }
}
if (-not $SkipTestInit) {
    Push-Location $Project
    try { & $Runtime -m server.windows_local init; if ($LASTEXITCODE -ne 0) { throw 'Private local test initialization failed. Security checks remain enforced.' } }
    finally { Pop-Location }
}
Write-Output 'Windows setup completed. Use scripts\start-windows.ps1 for the isolated local test.'
