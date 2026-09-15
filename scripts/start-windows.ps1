#Requires -Version 5.1
[CmdletBinding()]
param([switch]$ApiOnly,[switch]$ModelOnly,[switch]$WaitReady,[ValidateRange(1,3600)][int]$Timeout=600)
$ErrorActionPreference='Stop'
if($ApiOnly -and $ModelOnly){throw 'Choose only one component.'}
$Project=Split-Path -Parent $PSScriptRoot
$Runtime=Join-Path $Project '.venv-win\Scripts\python.exe'
if(-not(Test-Path -LiteralPath $Runtime)){throw 'Run setup-windows.ps1 first.'}
$CommandArgs=@('-m','server.windows_local','start','--timeout',[string]$Timeout)
if($ApiOnly){$CommandArgs+='--api-only'}
if($ModelOnly){$CommandArgs+='--model-only'}
if($WaitReady){$CommandArgs+='--wait-ready'}
Push-Location $Project
try{& $Runtime @CommandArgs;if($LASTEXITCODE -ne 0){throw 'Local start failed; existing processes and records were preserved.'}}
finally{Pop-Location}
