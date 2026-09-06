$ErrorActionPreference = "Stop"

$required = @("PRICELABS_API_KEY")
$optional = @("PRICELABS_EMAIL", "PRICELABS_PASSWORD", "Grok_XAI_API_KEY", "XAI_API_KEY", "XAI_MODEL", "XAI_BASE_URL", "GROQ_API_KEY")

Write-Host ""
Write-Host "Environment check"

foreach ($name in $required) {
  $value = [Environment]::GetEnvironmentVariable($name, "Process")
  if ([string]::IsNullOrWhiteSpace($value)) {
    $value = [Environment]::GetEnvironmentVariable($name, "User")
  }

  if ([string]::IsNullOrWhiteSpace($value)) {
    Write-Host "MISSING  $name" -ForegroundColor Red
  }
  else {
    Write-Host "OK       $name = $($value.Substring(0, [Math]::Min(6, $value.Length)))..." -ForegroundColor Green
  }
}

foreach ($name in $optional) {
  $value = [Environment]::GetEnvironmentVariable($name, "Process")
  if ([string]::IsNullOrWhiteSpace($value)) {
    $value = [Environment]::GetEnvironmentVariable($name, "User")
  }

  if ([string]::IsNullOrWhiteSpace($value)) {
    Write-Host "OPTIONAL $name is not set" -ForegroundColor Yellow
  }
  else {
    Write-Host "OK       $name is set" -ForegroundColor Green
  }
}

Write-Host ""
