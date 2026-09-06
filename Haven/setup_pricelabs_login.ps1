$ErrorActionPreference = "Stop"

Write-Host ""
Write-Host "PriceLabs login setup"
Write-Host "This is only for browser export sync. Your password will not display as you type."
Write-Host ""

$email = Read-Host "PRICELABS_EMAIL"
$securePassword = Read-Host "PRICELABS_PASSWORD" -AsSecureString
$bstr = [Runtime.InteropServices.Marshal]::SecureStringToBSTR($securePassword)

try {
  $password = [Runtime.InteropServices.Marshal]::PtrToStringBSTR($bstr)
}
finally {
  if ($bstr -ne [IntPtr]::Zero) {
    [Runtime.InteropServices.Marshal]::ZeroFreeBSTR($bstr)
  }
}

if ([string]::IsNullOrWhiteSpace($email)) {
  Write-Error "No email entered."
  exit 1
}

if ([string]::IsNullOrWhiteSpace($password)) {
  Write-Error "No password entered."
  exit 1
}

$env:PRICELABS_EMAIL = $email
$env:PRICELABS_PASSWORD = $password

[Environment]::SetEnvironmentVariable("PRICELABS_EMAIL", $email, "User")
[Environment]::SetEnvironmentVariable("PRICELABS_PASSWORD", $password, "User")

Write-Host ""
Write-Host "PRICELABS_EMAIL and PRICELABS_PASSWORD are set for this session and saved to your Windows user environment."
Write-Host "Open a new terminal before running browser sync if this terminal does not pick them up."
