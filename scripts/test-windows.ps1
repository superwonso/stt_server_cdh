#Requires -Version 5.1
[CmdletBinding()]
param([switch]$Targeted,[switch]$InProcessJavaScript)
$ErrorActionPreference='Stop'
$Project=Split-Path -Parent $PSScriptRoot
$Service=Split-Path -Parent $Project
$Runtime=Join-Path $Project '.venv-win\Scripts\python.exe'
$Node=Join-Path $Service 'tools\node\node-v24.21.0-win-x64\node.exe'
if(-not(Test-Path -LiteralPath $Runtime)){throw 'Run setup-windows.ps1 first.'}
if(-not(Test-Path -LiteralPath $Node)){throw 'The verified portable Node.js executable is missing.'}
$Logs=Join-Path $Service 'work\regression'
New-Item -ItemType Directory -Path $Logs -Force | Out-Null
$Stamp=Get-Date -Format 'yyyyMMdd-HHmmss'
$PythonLog=Join-Path $Logs "$Stamp-python.log"
$NodeLog=Join-Path $Logs "$Stamp-node.log"
# Capture native stderr directly: Windows PowerShell 5.1 otherwise turns
# ordinary unittest progress on stderr into NativeCommandError records.
function Invoke-RegressionProcess([string]$Executable,[string[]]$Arguments,[string]$Log) {
    $Quoted = @($Arguments | ForEach-Object { '"' + $_ + '"' })
    $Process = Start-Process -FilePath $Executable -ArgumentList $Quoted -WorkingDirectory $Project -WindowStyle Hidden -Wait -PassThru -RedirectStandardOutput $Log -RedirectStandardError "$Log.stderr.log"
    return $Process.ExitCode
}
$PreviousAge = $env:STT_TEST_AGE_BINARY
$Age = Join-Path $Service 'tools\age\age\age.exe'
if(-not $PreviousAge -and (Test-Path -LiteralPath $Age -PathType Leaf)) { $env:STT_TEST_AGE_BINARY = $Age }
Push-Location $Project
try {
    if($Targeted) {
        $Tests=@('tests.test_windows_local','tests.test_win_model_transport','tests.test_model_transport',
          'tests.test_windows_download','tests.test_windows_files.WindowsFilePrimitiveTests',
          'tests.test_windows_manage','tests.test_windows_environment','tests.test_validation_boundary_contract')
        $PythonExit = Invoke-RegressionProcess $Runtime (@('-m','unittest') + $Tests + @('-v')) $PythonLog
    } else {
        $PythonExit = Invoke-RegressionProcess $Runtime @('-m','unittest','discover','-s','tests','-p','test_*.py','-v') $PythonLog
    }
    # Explicit expansion is compatible with both Windows PowerShell 5.1 and Node.
    $TestsJS=@(Get-ChildItem -LiteralPath (Join-Path $Project 'tests') -Filter '*.test.mjs' -File | ForEach-Object FullName)
    if($InProcessJavaScript) { $NodeExit = Invoke-RegressionProcess $Node (@('--test','--test-isolation=none') + $TestsJS) $NodeLog }
    else { $NodeExit = Invoke-RegressionProcess $Node (@('--test') + $TestsJS) $NodeLog }
    Write-Output "Python exit: $PythonExit; log: $PythonLog"
    Write-Output "JavaScript exit: $NodeExit; log: $NodeLog"
    if($PythonExit -ne 0 -or $NodeExit -ne 0){throw 'Regression tests did not all pass. Preserve logs; do not weaken ACL checks.'}
    if($Targeted){Write-Output 'Targeted checks passed. This does not validate the complete private-file and API lifecycle.'}
    else {Write-Output 'Regression checks passed. Real model, browser and long-duration tests are separate.'}
} finally {
    Pop-Location
    $env:STT_TEST_AGE_BINARY = $PreviousAge
}
