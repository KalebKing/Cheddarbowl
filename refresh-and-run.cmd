@echo off
REM Double-clickable launcher: refresh data (projections + crosswalk) then start the app.
REM Runs refresh-and-run.ps1 next to this file. The window stays open so you can read any errors.
cd /d "%~dp0"
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0refresh-and-run.ps1"
echo.
echo ============================================================
echo  Done. If a step failed above, fix it and run again.
echo  (This window can be closed.)
echo ============================================================
pause
