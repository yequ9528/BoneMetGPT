param(
    [Parameter(Mandatory = $true)][string]$PackageRoot,
    [Parameter(Mandatory = $true)][string]$ManifestSha256,
    [Parameter(Mandatory = $true)][string]$PythonExe,
    [switch]$SkipDependencyInstall
)

$ErrorActionPreference = 'Stop'
$root = (Resolve-Path -LiteralPath $PackageRoot).Path
$python = (Resolve-Path -LiteralPath $PythonExe).Path
$manifestPath = Join-Path $root 'release_manifest.json'
if ($ManifestSha256 -notmatch '^[0-9a-fA-F]{64}$' -or
    (Get-FileHash -Algorithm SHA256 -LiteralPath $manifestPath).Hash -ne $ManifestSha256) {
    throw 'The published release manifest SHA-256 does not match.'
}
$manifest = Get-Content -Raw -Encoding UTF8 -LiteralPath $manifestPath | ConvertFrom-Json
if ($manifest.target_version -ne 'V1.0.0' -or $manifest.distribution -ne 'local-public-subset-candidate') {
    throw 'Expected the BoneMetGPT V1 public subset package.'
}
foreach ($row in $manifest.files) {
    if ($row.path -cnotmatch '^[A-Za-z0-9_.-]+(/[A-Za-z0-9_.-]+)*$' -or
        ($row.path -split '/') -contains '.' -or ($row.path -split '/') -contains '..') {
        throw 'Invalid package-relative path.'
    }
    $path = Join-Path $root $row.path
    if ((Get-Item -LiteralPath $path).Length -ne $row.size_bytes -or
        (Get-FileHash -Algorithm SHA256 -LiteralPath $path).Hash -ne $row.sha256) {
        throw "Package member failed identity verification: $($row.path)"
    }
}

if (-not $SkipDependencyInstall) {
    $install = @('-I', '-m', 'pip', '--isolated', 'install', '--require-hashes', '--only-binary=:all:',
        '--index-url', 'https://pypi.org/simple', '--extra-index-url', 'https://download.pytorch.org/whl/cpu',
        '--find-links', (Join-Path $root 'locked_wheels'),
        '-r', (Join-Path $root 'release/environment/python-requirements-v1.lock.txt'))
    & $python @install
    if ($LASTEXITCODE -ne 0) { throw 'Pinned Python dependency installation failed.' }
    & $python -I -m pip check
    if ($LASTEXITCODE -ne 0) { throw 'Inconsistent Python dependencies.' }

    $npm = (Get-Command npm.cmd -ErrorAction Stop).Source
    & $npm ci --prefix (Join-Path $root 'source/viewer_frontend') --no-audit --no-fund
    if ($LASTEXITCODE -ne 0) { throw 'Pinned Viewer dependency installation failed.' }
}

& $python -I -B (Join-Path $root 'prepare_installation.py') --manifest-sha256 $ManifestSha256
if ($LASTEXITCODE -ne 0) { throw 'Installation identity preparation failed.' }

