# Claude Coworker Model - Setup Script (Windows/PowerShell)
# All Unicode removed for maximum compatibility

# Force console to handle standard text encoding
[Console]::OutputEncoding = [System.Text.Encoding]::ASCII
$OutputEncoding = [System.Text.Encoding]::ASCII

# 1. Configuration
$InstallDir = Join-Path $HOME "AppData\Local\claude-coworker"
$BinDir = Join-Path $HOME "AppData\Local\Microsoft\WindowsApps"
$ScriptDir = $PSScriptRoot
$VenvPython = Join-Path $InstallDir "venv\Scripts\python.exe"

Write-Host "=== Claude Coworker Model Setup ==="
Write-Host ""

# 1. Create venv
Write-Host "[1/4] Creating Python venv at $InstallDir..."
if (-not (Test-Path $InstallDir)) { 
    New-Item -ItemType Directory -Path $InstallDir -Force 
}
python -m venv (Join-Path $InstallDir "venv")

# 2. Install deps
Write-Host "[2/4] Installing dependencies..."
& $VenvPython -m pip install --quiet --upgrade pip
& $VenvPython -m pip install --quiet -r (Join-Path $ScriptDir "requirements.txt")

# 3. Install tools
Write-Host "[3/4] Installing tools to $BinDir..."
if (-not (Test-Path $BinDir)) { 
    New-Item -ItemType Directory -Path $BinDir -Force 
}

# Creating .bat wrappers for the tools
$Tools = @("ask-kimi", "kimi-write")
foreach ($tool in $Tools) {
    $ToolPath = Join-Path $ScriptDir "tools\$tool"
    $DestPath = Join-Path $BinDir "$tool.bat"
    
    # We use backticks (`) to escape the double quotes inside the string
    $Content = "@echo off`n`"$VenvPython`" `"$ToolPath`" %*"
    Set-Content -Path $DestPath -Value $Content
    Write-Host "  [OK] $tool (wrapped for venv)"
}

# Setup extract-chat
$ExtractPath = Join-Path $ScriptDir "tools\extract-chat"
$ExtractDest = Join-Path $BinDir "extract-chat.bat"
$ExtractContent = "@echo off`npython `"$ExtractPath`" %*"
Set-Content -Path $ExtractDest -Value $ExtractContent
Write-Host "  [OK] extract-chat (stdlib wrapper)"

# 4. Check API key
Write-Host "[4/4] Checking environment..."
if (-not $env:WORKER_API_KEY -and -not $env:MOONSHOT_API_KEY) {
    Write-Host ""
    Write-Host "WARNING: No API key found. Set one in your Environment Variables:"
    Write-Host "  Example: [System.Environment]::SetEnvironmentVariable('WORKER_API_KEY', 'your-key', 'User')"
    Write-Host ""
} else {
    Write-Host "  [OK] API key found"
}

Write-Host ""
Write-Host "=== Done! ==="
Write-Host ""
Write-Host "If $BinDir is on your PATH, try:"
Write-Host "  ask-kimi --paths some_file.py --question `"what does this do?`""
Write-Host ""
Write-Host "Copy CLAUDE.md.template into your project's CLAUDE.md for auto-routing."