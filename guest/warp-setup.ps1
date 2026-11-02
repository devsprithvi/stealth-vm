# Cloudflare WARP setup for the Windows base image.
# Run in an elevated PowerShell inside the guest.
$ErrorActionPreference = "Stop"

$url = "https://downloads.cloudflareclient.com/v1/download/windows/version/latest"
$installer = Join-Path $env:TEMP "Cloudflare_WARP_Release.exe"

Write-Host "Downloading Cloudflare WARP..."
Invoke-WebRequest -Uri $url -OutFile $installer -UseBasicParsing

Write-Host "Installing silently..."
Start-Process -FilePath $installer -ArgumentList "/S" -Wait

$cli = Join-Path ${env:ProgramFiles} "Cloudflare\Cloudflare WARP\warp-cli.exe"
if (Test-Path $cli) {
    Write-Host "warp-cli found: $cli"
    & $cli --accept-tos status
} else {
    Write-Host "warp-cli not found; open the WARP app and finish setup manually."
}

Write-Host ""
Write-Host "Next steps (required):"
Write-Host "  1. Open Cloudflare WARP and log in."
Write-Host "  2. Connect."
Write-Host "  3. Settings > Advanced > enable Lockdown mode / Always On."
Write-Host "  4. Disable IPv6 on the NIC:"
Write-Host "     Get-NetAdapter | Disable-NetAdapterBinding -ComponentID ms_tcpip6"
Write-Host "  5. Verify inside the guest:"
Write-Host "     curl https://www.cloudflare.com/cdn-cgi/trace"
Write-Host "     (expect warp=on and a Cloudflare egress IP)"
