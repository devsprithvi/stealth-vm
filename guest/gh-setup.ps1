# GitHub CLI setup for the Windows base image.
# Run in an elevated PowerShell inside the guest.
$ErrorActionPreference = "Stop"

Write-Host "Installing GitHub CLI via winget..."
if (Get-Command winget -ErrorAction SilentlyContinue) {
    winget install --id GitHub.cli --exact --silent --accept-source-agreements --accept-package-agreements
} else {
    Write-Host "Downloading GitHub CLI MSI installer..."
    $url = "https://github.com/cli/cli/releases/latest/download/gh_windows_amd64.msi"
    $installer = Join-Path $env:TEMP "gh_setup.msi"
    Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing
    Start-Process msiexec.exe -ArgumentList "/i", "`"$installer`"", "/quiet", "/norestart" -Wait
}

Write-Host ""
Write-Host "Verifying gh installation..."
& "$env:ProgramFiles\GitHub CLI\gh.exe" --version
