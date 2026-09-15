#Requires -Version 5.1
[CmdletBinding()]
param([ValidateRange(1,60)][int]$Timeout=10,[switch]$Json)
$ErrorActionPreference='Stop'
$Project=Split-Path -Parent $PSScriptRoot
$CommandArgs=@('-m','server.windows_tunnel','stop','--timeout',[string]$Timeout)
if($Json){$CommandArgs+='--json'}
Push-Location $Project
try{& (Join-Path $Project '.venv-win\Scripts\python.exe') @CommandArgs;if($LASTEXITCODE -ne 0){throw 'Owned tunnel exit could not be verified; existing state was preserved.'}}
finally{Pop-Location}
