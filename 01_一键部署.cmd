@echo off
setlocal
cd /d "%~dp0"
chcp 65001 >nul
set "PYTHONUTF8=1"
set "PYTHONIOENCODING=utf-8"
set "PYTHONNOUSERSITE=1"
set "PIP_NO_INDEX=1"
set "PIP_DISABLE_PIP_VERSION_CHECK=1"

rem Resolve a real interpreter before creating the project-local environment.
set "BASE_PYTHON="
for /f "usebackq delims=" %%P in (`python -c "import sys; print(sys.executable)" 2^>nul`) do (
    if not defined BASE_PYTHON set "BASE_PYTHON=%%P"
)
if not defined BASE_PYTHON (
    for /f "usebackq delims=" %%P in (`py -3 -c "import sys; print(sys.executable)" 2^>nul`) do (
        if not defined BASE_PYTHON set "BASE_PYTHON=%%P"
    )
)
if not defined BASE_PYTHON (
    echo [ERROR] Python 3 was not found. Install 64-bit Python 3.9 or newer, then run this file again.
    set "DEPLOY_EXIT=1"
    goto finish
)

echo [1/2] Creating the isolated environment from local wheels...
"%BASE_PYTHON%" "%~dp0setup_venv.py" --offline
if errorlevel 1 (
    set "DEPLOY_EXIT=1"
    goto finish
)
set "VENV_PYTHON=%~dp0.venv\Scripts\python.exe"
if not exist "%VENV_PYTHON%" (
    echo [ERROR] The virtual environment Python was not created: "%VENV_PYTHON%"
    set "DEPLOY_EXIT=1"
    goto finish
)

echo [2/2] Deploying WeCom Local MCP without network access...
powershell.exe -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy_wecom.ps1" -PythonPath "%VENV_PYTHON%" %*
set "DEPLOY_EXIT=%ERRORLEVEL%"

:finish
echo.
if /i not "%WECOM_DEPLOY_NO_PAUSE%"=="1" pause
exit /b %DEPLOY_EXIT%
