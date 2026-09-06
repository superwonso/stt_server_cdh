param(
    [Parameter(Mandatory=$true)][string]$Source,
    [Parameter(Mandatory=$true)][string]$Name,
    [Parameter(Mandatory=$true)][string]$ExpectedSha256,
    [Parameter(Mandatory=$true)][long]$ExpectedBytes
)

# Deliberately no caller-controlled destination. Only encrypted bundles cross
# from WSL to this operator-selected drive; never keys or plaintext archives.
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = New-Object System.Text.UTF8Encoding($false)
$destination = 'D:\STT-Backups'
$createdPartial = $false
$partial = $null
$deadline = [DateTime]::UtcNow.AddSeconds(150)

function Assert-Regular([string]$Path) {
    $item = Get-Item -LiteralPath $Path -Force
    if ($item.PSIsContainer -or ($item.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'unsafe file'
    }
}

function Get-VerifiedHash([string]$Path) {
    Assert-Regular $Path
    $stream = [IO.File]::Open($Path, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
    try {
        if ($stream.Length -ne $ExpectedBytes) { throw 'size mismatch' }
        $algorithm = [Security.Cryptography.SHA256]::Create()
        try {
            $value = ([BitConverter]::ToString($algorithm.ComputeHash($stream))).Replace('-', '').ToLowerInvariant()
        } finally { $algorithm.Dispose() }
    } finally { $stream.Dispose() }
    if ($value -cne $ExpectedSha256) { throw 'hash mismatch' }
    return $value
}

try {
    if ($Name -cnotmatch '^yeobaek-recovery-[0-9]{8}T[0-9]{6}Z-[0-9a-f]{32}\.age$' -or
        $ExpectedSha256 -cnotmatch '^[0-9a-f]{64}$' -or
        $ExpectedBytes -lt 1 -or $ExpectedBytes -gt 600MB -or
        [IO.Path]::GetFileName($Source) -cne $Name -or
        -not ($Source.StartsWith('\\wsl.localhost\', [StringComparison]::OrdinalIgnoreCase) -or
              $Source.StartsWith('\\wsl$\', [StringComparison]::OrdinalIgnoreCase))) {
        throw 'invalid input'
    }
    if (-not [IO.Directory]::Exists('D:\')) { throw 'drive unavailable' }
    if (-not [IO.Directory]::Exists($destination)) {
        [void][IO.Directory]::CreateDirectory($destination)
    }
    $folder = Get-Item -LiteralPath $destination -Force
    if (-not $folder.PSIsContainer -or ($folder.Attributes -band [IO.FileAttributes]::ReparsePoint)) {
        throw 'unsafe directory'
    }
    $final = [IO.Path]::Combine($destination, $Name)
    $partial = $final + '.partial'
    if ([IO.File]::Exists($final)) {
        $verified = Get-VerifiedHash $final
    } else {
        if ([IO.File]::Exists($partial)) {
            # Only a complete, byte-identical prior attempt can be resumed.
            # An unknown/truncated partial is retained for operator inspection.
            $verified = Get-VerifiedHash $partial
        } else {
            Assert-Regular $Source
            $sourceStream = [IO.File]::Open($Source, [IO.FileMode]::Open, [IO.FileAccess]::Read, [IO.FileShare]::Read)
            try {
                if ($sourceStream.Length -ne $ExpectedBytes) { throw 'source size mismatch' }
                $magic = [Text.Encoding]::ASCII.GetBytes("age-encryption.org/v1`n")
                $header = New-Object byte[] $magic.Length
                if ($sourceStream.Read($header,0,$header.Length) -ne $header.Length -or
                    [Text.Encoding]::ASCII.GetString($header) -cne [Text.Encoding]::ASCII.GetString($magic)) {
                    throw 'not ciphertext'
                }
                $sourceStream.Position = 0
                $output = [IO.File]::Open($partial, [IO.FileMode]::CreateNew, [IO.FileAccess]::Write, [IO.FileShare]::None)
                $createdPartial = $true
                try {
                    $buffer = New-Object byte[] (1024 * 1024)
                    while (($read = $sourceStream.Read($buffer,0,$buffer.Length)) -gt 0) {
                        if ([DateTime]::UtcNow -ge $deadline) { throw 'copy timeout' }
                        $output.Write($buffer,0,$read)
                    }
                    $output.Flush($true)
                } finally { $output.Dispose() }
            } finally { $sourceStream.Dispose() }
            $verified = Get-VerifiedHash $partial
        }
        # .NET Framework File.Move refuses to replace an existing final file.
        [IO.File]::Move($partial, $final)
        $createdPartial = $false
    }
    @{verified=$true; bytes=$ExpectedBytes; sha256=$verified} | ConvertTo-Json -Compress
    exit 0
} catch {
    if ($createdPartial -and $partial -and [IO.File]::Exists($partial)) {
        # Only an incomplete ciphertext file created by THIS invocation.
        try { [IO.File]::Delete($partial) } catch {}
    }
    [Console]::Error.WriteLine('Encrypted backup copy failed; earlier backups were not replaced.')
    exit 1
}
