#Requires -Version 5.1
[CmdletBinding()]
param([switch]$CheckHealth,[switch]$Json)
$ErrorActionPreference='Stop'
$Project=Split-Path -Parent $PSScriptRoot
$CommandArgs=@('-m','server.windows_tunnel','status')
if($CheckHealth){$CommandArgs+='--check-health'}
if($Json){$CommandArgs+='--json'}
Push-Location $Project
try{& (Join-Path $Project '.venv-win\Scripts\python.exe') @CommandArgs;if($LASTEXITCODE -ne 0){throw 'Could not verify this project tunnel.'}}
finally{Pop-Location}
