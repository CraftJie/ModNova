# ModNova code signing helper (self-signed, like typical open-source projects).
#
# Usage:
#   .\sign.ps1 -Action create -PfxPath certs\modnova.pfx -Password <pwd> -Subject "CN=ModNova Project, O=ModNova, C=CN"
#   .\sign.ps1 -Action sign   -PfxPath certs\modnova.pfx -Password <pwd> -Files dist\ModNova\ModNova.exe
#
# Notes:
#   - Timestamp server is RFC 3161 (signtool /tr equivalent), so signatures stay
#     valid after the certificate expires.
#   - The PFX and its password never leave build/certs (gitignored).

param(
    [Parameter(Mandatory = $true)][string]$Action,
    [string]$PfxPath = "certs\modnova-selfsigned.pfx",
    [string]$Password,
    [string]$Subject = "CN=ModNova Project, O=ModNova Open Source, C=CN",
    [string[]]$Files,
    [int]$Years = 3
)

$ErrorActionPreference = "Stop"
$TimestampUrl = "http://timestamp.digicert.com"

function New-Cert {
    $pfx = Resolve-PathSafe $PfxPath
    if (Test-Path $pfx) { Write-Output "PFX already exists: $pfx"; exit 1 }

    if (-not $Password) { Write-Output "Password required for create"; exit 1 }
    $sec = ConvertTo-SecureString -String $Password -Force -AsPlainText

    $cert = New-SelfSignedCertificate `
        -Type CodeSigningCert `
        -Subject $Subject `
        -KeyUsage DigitalSignature `
        -KeySpec Signature `
        -KeyLength 3072 `
        -HashAlgorithm SHA256 `
        -NotAfter (Get-Date).AddYears($Years) `
        -CertStoreLocation "Cert:\CurrentUser\My" `
        -FriendlyName "ModNova self-signed code signing"

    $dir = Split-Path $pfx -Parent
    if (-not (Test-Path $dir)) { New-Item -ItemType Directory -Path $dir | Out-Null }
    Export-PfxCertificate -Cert $cert -FilePath $pfx -Password $sec | Out-Null
    Write-Output "CERT_CREATED"
    Write-Output "Thumbprint: $($cert.Thumbprint)"
    Write-Output "Subject:    $($cert.Subject)"
    Write-Output "Valid:      $($cert.NotBefore.ToString('yyyy-MM-dd')) - $($cert.NotAfter.ToString('yyyy-MM-dd'))"
    Write-Output "PFX:        $pfx"
}

function Resolve-PathSafe([string]$p) {
    if ([System.IO.Path]::IsPathRooted($p)) { return $p }
    return Join-Path (Get-Location) $p
}

function Sign-Files {
    if (-not $Files -or $Files.Count -eq 0) { Write-Output "Files required for sign"; exit 1 }
    if (-not $Password) { Write-Output "Password required for sign"; exit 1 }

    $pfx = Resolve-PathSafe $PfxPath
    if (-not (Test-Path $pfx)) { Write-Output "PFX not found: $pfx (run -Action create first)"; exit 1 }
    $sec = ConvertTo-SecureString -String $Password -Force -AsPlainText
    $cert = Import-PfxCertificate -FilePath $pfx -CertStoreLocation "Cert:\CurrentUser\My" -Password $sec

    foreach ($f in $Files) {
        $target = Resolve-PathSafe $f
        if (-not (Test-Path $target)) { Write-Output "MISSING $target"; continue }
        $sig = Set-AuthenticodeSignature -FilePath $target -Certificate $cert `
            -HashAlgorithm SHA256 -TimeStampServer $TimestampUrl
        if ($sig.Status -ne "Valid") {
            Write-Output "SIGN_FAILED $target status=$($sig.Status) msg=$($sig.StatusMessage)"
            exit 1
        }
        # verify straight back from the file
        $check = Get-AuthenticodeSignature -FilePath $target
        Write-Output ("SIGNED_OK {0}  status={1}  signer={2}  timestamped={3}" -f `
            (Split-Path $target -Leaf), $check.Status, $check.SignerCertificate.Subject, ($null -ne $check.TimeStamperCertificate))
    }
}

switch ($Action) {
    "create" { New-Cert }
    "sign"   { Sign-Files }
    default  { Write-Output "Unknown action: $Action (use create|sign)"; exit 1 }
}
