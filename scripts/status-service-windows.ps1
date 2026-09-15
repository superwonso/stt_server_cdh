#Requires -Version 5.1
[CmdletBinding()]
param([switch]$ApiOnly,[switch]$ModelOnly,[switch]$Json)
$ErrorActionPreference='Stop'
if($ApiOnly -and $ModelOnly){throw 'Choose only one component.'}
$Project=Split-Path -Parent $PSScriptRoot
$CommandArgs=@('-m','server.windows_service','status')
if($ApiOnly){$CommandArgs+='--api-only'}
if($ModelOnly){$CommandArgs+='--model-only'}
if($Json){$CommandArgs+='--json'}
Push-Location $Project
try{& (Join-Path $Project '.venv-win\Scripts\python.exe') @CommandArgs;if($LASTEXITCODE -ne 0){throw 'Service status could not be verified.'}}
finally{Pop-Location}
