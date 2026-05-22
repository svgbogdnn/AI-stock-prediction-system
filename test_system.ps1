$ErrorActionPreference = "Continue"

$FailCount = 0
$WarnCount = 0

function Pass([string]$Message) {
    Write-Host "  [PASS] $Message" -ForegroundColor Green
}

function WarnMsg([string]$Message) {
    Write-Host "  [WARN] $Message" -ForegroundColor Yellow
    $script:WarnCount++
}

function FailMsg([string]$Message) {
    Write-Host "  [FAIL] $Message" -ForegroundColor Red
    $script:FailCount++
}

function Section([string]$Title) {
    Write-Host ""
    Write-Host "== $Title ==" -ForegroundColor Cyan
}

Write-Host "Stock Prediction System - Block Check (PowerShell)" -ForegroundColor White
Write-Host "==================================================" -ForegroundColor White

function Resolve-PythonExe {
    if (Test-Path "venv\Scripts\python.exe") { return "venv\Scripts\python.exe" }
    if (Test-Path ".venv\Scripts\python.exe") { return ".venv\Scripts\python.exe" }
    if (Get-Command python -ErrorAction SilentlyContinue) { return "python" }
    if (Get-Command py -ErrorAction SilentlyContinue) { return "py -3" }
    return $null
}

Section "1) Python"
$ResolvedPython = Resolve-PythonExe
if ($ResolvedPython) {
    try {
        if ($ResolvedPython -eq "py -3") {
            $pyVersionRaw = & py -3 -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null
        } else {
            $pyVersionRaw = & $ResolvedPython -c "import sys; print('.'.join(map(str, sys.version_info[:3])))" 2>$null
        }
        $pyVersion = [Version]$pyVersionRaw
        if ($pyVersion -ge [Version]"3.11.0") {
            Pass "python version: $pyVersionRaw (>= 3.11)"
        } else {
            FailMsg "python version is $pyVersionRaw (need >= 3.11)"
        }
    } catch {
        FailMsg "failed to read python version: $($_.Exception.Message)"
    }
} else {
    FailMsg "python is not found (venv/.venv/python/python launcher)"
}

Section "2) .env and keys"
if (Test-Path ".env") {
    Pass ".env exists"

    if (Select-String -Path ".env" -Pattern "^GOOGLE_API_KEY=" -Quiet) {
        Pass "GOOGLE_API_KEY found"
    } else {
        WarnMsg "GOOGLE_API_KEY missing"
    }

    if (Select-String -Path ".env" -Pattern "^POLYGON_API_KEY=" -Quiet) {
        Pass "POLYGON_API_KEY found"
    } else {
        WarnMsg "POLYGON_API_KEY missing"
    }

    if (Select-String -Path ".env" -Pattern "^FRED_API_KEY=" -Quiet) {
        Pass "FRED_API_KEY found"
    } else {
        WarnMsg "FRED_API_KEY missing (optional)"
    }

    if (Select-String -Path ".env" -Pattern "^NEWS_API_KEY=" -Quiet) {
        Pass "NEWS_API_KEY found"
    } else {
        WarnMsg "NEWS_API_KEY missing (optional)"
    }
} else {
    FailMsg ".env not found"
}

Section "3) Python dependencies"
$PyBin = "python"
if (Test-Path "venv\Scripts\python.exe") {
    $PyBin = "venv\Scripts\python.exe"
    Pass "using venv\Scripts\python.exe"
} elseif (Test-Path ".venv\Scripts\python.exe") {
    $PyBin = ".venv\Scripts\python.exe"
    Pass "using .venv\Scripts\python.exe"
} else {
    WarnMsg "venv python not found, using system python"
}

try {
    $depCheckScript = @'
import importlib
mods = [
    "fastapi", "uvicorn", "requests", "aiohttp", "pydantic",
    "google.genai", "google.adk", "pandas", "numpy", "bs4"
]
for m in mods:
    importlib.import_module(m)
print("ok")
'@
    if ($PyBin -eq "py -3") {
        $depCheckScript | & py -3 - 2>$null | Out-Null
    } else {
        $depCheckScript | & $PyBin - 2>$null | Out-Null
    }
    if ($LASTEXITCODE -eq 0) {
        Pass "core modules import successfully"
    } else {
        FailMsg "some Python dependencies are missing"
    }
} catch {
    FailMsg "dependency import check failed: $($_.Exception.Message)"
}

Section "4) Data tools block"
$toolFiles = @(
    "tools\polygon_fetcher.py",
    "tools\fred_fetcher.py",
    "tools\news_fetcher.py",
    "tools\sec_edgar_fetcher.py"
)
foreach ($f in $toolFiles) {
    if (Test-Path $f) {
        Pass "$f exists"
    } else {
        FailMsg "$f missing"
    }
}

Section "5) A2A agents block"
$agentFiles = Get-ChildItem -Path "agents" -Filter "*_server.py" -File -ErrorAction SilentlyContinue
if ($agentFiles.Count -ge 6) {
    Pass "agent server files found: $($agentFiles.Count)"
} else {
    FailMsg "expected >= 6 agent server files, found: $($agentFiles.Count)"
}

$requiredAgents = @(
    "agents\fundamental_analyst_server.py",
    "agents\technical_analyst_server.py",
    "agents\news_sentiment_analyst_server.py",
    "agents\macro_analyst_server.py",
    "agents\regulatory_analyst_server.py",
    "agents\predictor_agent_server.py"
)
foreach ($f in $requiredAgents) {
    if (Test-Path $f) {
        Pass "$f exists"
    } else {
        FailMsg "$f missing"
    }
}

Section "6) MCP block"
$mcpFiles = @(
    "mcp_context_hub\server.py",
    "mcp_context_hub\client.py",
    "mcp_context_hub\__init__.py"
)
foreach ($f in $mcpFiles) {
    if (Test-Path $f) {
        Pass "$f exists"
    } else {
        FailMsg "$f missing"
    }
}

if (Test-Path "agents\kaggle_orchestrator.py") {
    $hasMcpRef = Select-String -Path "agents\kaggle_orchestrator.py" -Pattern "mcp_context_hub" -Quiet
    if ($hasMcpRef) {
        Pass "kaggle orchestrator references MCP"
    } else {
        WarnMsg "kaggle orchestrator does not reference MCP"
    }
} else {
    FailMsg "agents\kaggle_orchestrator.py missing"
}

try {
    $mcpHealth = Invoke-RestMethod -Uri "http://localhost:8010/health" -Method Get -TimeoutSec 3
    if ($mcpHealth.status -eq "healthy") {
        Pass "MCP runtime health endpoint is reachable (:8010)"
    } else {
        WarnMsg "MCP runtime endpoint returned unexpected payload"
    }
} catch {
    WarnMsg "MCP runtime endpoint not reachable (:8010). Start system first if needed."
}

Section "7) Backend block"
if (Test-Path "frontend_api.py") {
    Pass "frontend_api.py exists"
} else {
    FailMsg "frontend_api.py missing"
}

if (Test-Path "start_full_system.py") {
    Pass "start_full_system.py exists"
} else {
    FailMsg "start_full_system.py missing"
}

try {
    $backendHealth = Invoke-RestMethod -Uri "http://localhost:8000/health" -Method Get -TimeoutSec 3
    if ($backendHealth.status) {
        Pass "backend health endpoint is reachable (:8000)"
    } else {
        WarnMsg "backend health endpoint returned unexpected payload"
    }
} catch {
    WarnMsg "backend health endpoint not reachable (:8000). Start system first if needed."
}

Section "8) Frontend block"
if (Test-Path "frontend\package.json") {
    Pass "frontend\package.json exists"
} else {
    FailMsg "frontend\package.json missing"
}

if (Test-Path "frontend\node_modules") {
    Pass "frontend\node_modules exists"
} else {
    WarnMsg "frontend\node_modules missing (run: cd frontend; npm install)"
}

Section "9) Notebook + docs"
if (Test-Path "notebooks\kaggle_submission_complete.ipynb") {
    Pass "kaggle notebook exists"
} else {
    WarnMsg "kaggle notebook missing"
}

if ((Test-Path "README.md") -and ((Get-Item "README.md").Length -gt 0)) {
    Pass "README.md exists and not empty"
} else {
    FailMsg "README.md missing or empty"
}

Write-Host ""
Write-Host "==================================================" -ForegroundColor White
Write-Host "Check complete" -ForegroundColor White
Write-Host "FAIL: $FailCount" -ForegroundColor White
Write-Host "WARN: $WarnCount" -ForegroundColor White

if ($FailCount -gt 0) {
    Write-Host "Status: FAILED" -ForegroundColor Red
    exit 1
}

Write-Host "Status: PASSED" -ForegroundColor Green
exit 0
