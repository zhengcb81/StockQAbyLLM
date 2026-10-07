@echo off
REM StockQAbyLLM unified check entry - thin forwarding layer.
REM ASCII-only comments: cmd.exe parses this file with the console codepage.
REM This file only: locates the repo root, forwards every argument unchanged,
REM and returns the exit code unchanged. All steps are defined in checks.py.
setlocal
set "SCRIPT_DIR=%~dp0"
set "REPO_ROOT=%SCRIPT_DIR%.."
set "PYTHON_BIN=%PYTHON%"
if not defined PYTHON_BIN set "PYTHON_BIN=python"
"%PYTHON_BIN%" -B "%REPO_ROOT%\scripts\checks.py" %*
set "G2_RC=%ERRORLEVEL%"
endlocal & exit /b %G2_RC%
