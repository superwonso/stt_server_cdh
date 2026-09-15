#Requires -Version 5.1
[CmdletBinding()]
param([switch]$ApiOnly,[switch]$ModelOnly,[switch]$WaitReady,[switch]$Json,[ValidateRange(1,3600)][int]$Timeout=600)
$ErrorActionPreference='Stop'
if($ApiOnly -and $ModelOnly){throw 'Choose only one component.'}
$Project=Split-Path -Parent $PSScriptRoot
$Runtime=Join-Path $Project '.venv-win\Scripts\python.exe'
if(-not(Test-Path -LiteralPath $Runtime)){throw 'The prepared Windows Python runtime is missing.'}
$CommandArgs=@('-m','server.windows_service','start','--timeout',[string]$Timeout)
if($ApiOnly){$CommandArgs+='--api-only'}
if($ModelOnly){$CommandArgs+='--model-only'}
if($WaitReady){$CommandArgs+='--wait-ready'}
if($Json){$CommandArgs+='--json'}
Push-Location $Project
try{& $Runtime @CommandArgs;if($LASTEXITCODE -ne 0){throw 'Service startup failed. Existing runtime records were preserved.'}}
finally{Pop-Location}
