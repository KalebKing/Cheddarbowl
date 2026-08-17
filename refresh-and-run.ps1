# refresh-and-run.ps1
# Refresh the data (ffanalytics projections + ESPN player crosswalk), then launch the app.
# Stops immediately if any step fails, so you never launch on half-updated data.
#
# Run from anywhere:  powershell -ExecutionPolicy Bypass -File .\refresh-and-run.ps1

$ErrorActionPreference = 'Stop'

Set-Location $PSScriptRoot
$py = '.\.venv\Scripts\python.exe'

function Invoke-Step {
    param([string]$Label, [scriptblock]$Action)
    Write-Host ""
    Write-Host "==> $Label" -ForegroundColor Cyan
    & $Action
    if ($LASTEXITCODE -ne 0) {
        Write-Host "FAILED: $Label (exit code $LASTEXITCODE)" -ForegroundColor Red
        exit $LASTEXITCODE
    }
}

# 1) Rebuild projections from ffanalytics (runs the R script via config.run_rscript)
Invoke-Step 'Refreshing projections (ffanalytics via R)' {
    & $py -c "from cheddarbowl import config; import sys; r=config.run_rscript('cheddarbowl/projections/run_projections.R', str(config.SEASON), '0', config.PROJECTIONS_CSV); print(r.stdout); print(r.stderr); sys.exit(r.returncode)"
}

# 2) Rebuild the ESPN player crosswalk
Invoke-Step 'Rebuilding ESPN player crosswalk' {
    & $py -m cheddarbowl.projections.build_crosswalk_espn
}

# 3) Launch the app
Invoke-Step 'Launching the app' {
    & '.\start.bat'
}
