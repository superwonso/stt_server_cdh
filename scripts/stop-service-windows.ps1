#Requires -Version 5.1
[CmdletBinding()]
param([switch]$ApiOnly,[switch]$ModelOnly,[switch]$Json,[ValidateRange(1,3600)][int]$Timeout=60)
$ErrorActionPreference='Stop'
if($ApiOnly -and $ModelOnly){throw 'Choose only one component.'}
$Project=Split-Path -Parent $PSScriptRoot
$Runtime=Join-Path $Project '.venv-win\Scripts\python.exe'
$CommandArgs=@('-m','server.windows_service','stop','--timeout',[string]$Timeout)
if($ApiOnly){$CommandArgs+='--api-only'}
if($ModelOnly){$CommandArgs+='--model-only'}
if($Json){$CommandArgs+='--json'}
Push-Location $Project
try{& $Runtime @CommandArgs;if($LASTEXITCODE -ne 0){throw 'Could not verify or stop the owned service process.'}}
finally{Pop-Location}
