@echo off
REM M0 orchestrator benchmark one-click runner (Windows)
REM Usage: bench\run_bench.bat [extra args to bench.runner]
setlocal
set "SCRIPT_DIR=%~dp0"
pushd "%SCRIPT_DIR%\.."
python -m bench.runner %*
set "EXITCODE=%ERRORLEVEL%"
popd
endlocal & exit /b %EXITCODE%

