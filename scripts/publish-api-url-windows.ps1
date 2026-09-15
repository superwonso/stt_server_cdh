#Requires -Version 5.1
[CmdletBinding()]
param([ValidateSet('Publish','Renew','Wait','Status')][string]$Action='Publish',
      [string]$Config,[switch]$Offline,[ValidateRange(1,600)][int]$Timeout=180,[switch]$Json)
$ErrorActionPreference='Stop'
$Project=Split-Path -Parent $PSScriptRoot
$CommandArgs=@('-m','server.windows_publication',$Action.ToLowerInvariant(),'--wait-timeout',[string]$Timeout)
if($Config){$CommandArgs+=@('--config',[IO.Path]::GetFullPath($Config))}
if($Offline){$CommandArgs+='--offline'}
if($Json){$CommandArgs+='--json'}
Push-Location $Project
try{& (Join-Path $Project '.venv-win\Scripts\python.exe') @CommandArgs;if($LASTEXITCODE -ne 0){throw 'Exact GitHub Pages publication was not confirmed.'}}
finally{Pop-Location}