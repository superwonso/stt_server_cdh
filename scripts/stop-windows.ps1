#Requires -Version 5.1
[CmdletBinding()]
param([switch]$ApiOnly,[switch]$ModelOnly,[ValidateRange(1,3600)][int]$Timeout=60)
$ErrorActionPreference='Stop'
if($ApiOnly -and $ModelOnly){throw 'Choose only one component.'}
$Project=Split-Path -Parent $PSScriptRoot
$Runtime=Join-Path $Project '.venv-win\Scripts\python.exe'
$CommandArgs=@('-m','server.windows_local','stop','--timeout',[string]$Timeout)
if($ApiOnly){$CommandArgs+='--api-only'}
if($ModelOnly){$CommandArgs+='--model-only'}
Push-Location $Project
try{& $Runtime @CommandArgs;if($LASTEXITCODE -ne 0){throw 'Could not verify or stop this project process. No other process was targeted.'}}
finally{Pop-Location}
