#Requires -Version 5.1
[CmdletBinding()]
param([ValidateRange(1,300)][int]$Timeout=90,[switch]$Json)
$ErrorActionPreference='Stop'
$Project=Split-Path -Parent $PSScriptRoot
$Runtime=Join-Path $Project '.venv-win\Scripts\python.exe'
$CommandArgs=@('-m','server.windows_tunnel','start','--timeout',[string]$Timeout)
if($Json){$CommandArgs+='--json'}
Push-Location $Project
try{& $Runtime @CommandArgs;if($LASTEXITCODE -ne 0){throw 'Owned production API tunnel start failed; no publication was attempted.'}}
finally{Pop-Location}
