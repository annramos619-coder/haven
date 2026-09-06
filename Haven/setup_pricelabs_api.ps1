param(
  [switch]$CurrentSessionOnly
)

$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "PriceLabs API key setup"
Write-Host "Paste your PriceLabs Customer API key below. It will not display as you type."
Write-Host ""

$secureKey = Read-Host "PRICELABS_API_KEY" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($secureKey)

try {
  $apiKey = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
}
finally {
  if ($bstr -ne [IntPtr]::Zero) {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
  }
}

if ([string]::IsNullOrWhiteSpace($apiKey)) {
  Write-Error "No API key entered."
  exit 1
}

$env:PRICELABS_API_KEY = $apiKey

if (-not $CurrentSessionOnly) {
  [Environment]::SetEnvironmentVariable("PRICELABS_API_KEY", $apiKey, "User")
}

Write-Host ""
Write-Host "PRICELABS_API_KEY is set for this PowerShell session."

if ($CurrentSessionOnly) {
  Write-Host "It was not saved permanently because -CurrentSessionOnly was used."
}
else {
  Write-Host "It was also saved to your Windows user environment."
  Write-Host "Open a new terminal before running the dashboard so the saved key is available."
}

Write-Host "Key starts with: $($apiKey.Substring(0, [Math]::Min(6, $apiKey.Length)))..."
