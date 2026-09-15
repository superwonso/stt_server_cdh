#Requires -Version 5.1
[CmdletBinding()]
param([switch]$Json)
$ErrorActionPreference='Stop'
$Project=Split-Path -Parent $PSScriptRoot
$CommandArgs=@('-m','server.windows_local','status')
if($Json){$CommandArgs+='--json'}
Push-Location $Project
try{& (Join-Path $Project '.venv-win\Scripts\python.exe') @CommandArgs;if($LASTEXITCODE -ne 0){throw 'Local status could not be verified.'}}
finally{Pop-Location}
