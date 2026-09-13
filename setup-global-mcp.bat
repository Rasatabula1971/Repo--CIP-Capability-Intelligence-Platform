@echo off
setlocal enabledelayedexpansion

echo ============================================================
echo   CIP Global MCP Setup
echo ============================================================
echo.

:: ----------------------------------------------------------
:: 1. Install CIP in editable mode
:: ----------------------------------------------------------
echo [1/4] Installing CIP package (editable) ...
pip install -e "%~dp0." >nul 2>&1
if errorlevel 1 (
    echo FAILED — pip install -e . returned an error.
    echo Make sure Python 3.11+ and pip are on your PATH.
    pause
    exit /b 1
)
echo       OK — cip-mcp is now on PATH.

:: Verify cip-mcp is reachable
where cip-mcp >nul 2>&1
if errorlevel 1 (
    echo WARNING: cip-mcp not found on PATH.
    echo You may need to add your Python Scripts directory to PATH.
    echo Typical location: %LOCALAPPDATA%\Programs\Python\Python3*\Scripts
)

:: ----------------------------------------------------------
:: 2. Ensure .env exists
:: ----------------------------------------------------------
echo.
echo [2/4] Checking .env ...
if not exist "%~dp0.env" (
    copy "%~dp0.env.example" "%~dp0.env" >nul
    echo       Created .env from .env.example — edit DATABASE_URL before first use.
) else (
    echo       .env already exists.
)

:: ----------------------------------------------------------
:: 3. Set DATABASE_URL as a persistent user env var (if missing)
:: ----------------------------------------------------------
echo.
echo [3/4] Checking DATABASE_URL environment variable ...
if "%DATABASE_URL%"=="" (
    echo       DATABASE_URL is not set in your environment.
    echo.
    set /p "DB_URL=       Enter your DATABASE_URL (or press Enter for default): "
    if "!DB_URL!"=="" (
        set "DB_URL=postgresql://cip:cip@localhost:5432/cip_local"
    )
    setx DATABASE_URL "!DB_URL!" >nul
    echo       Saved DATABASE_URL for future sessions.
    echo       Value: !DB_URL!
    set "DATABASE_URL=!DB_URL!"
) else (
    echo       Already set: %DATABASE_URL%
)

:: ----------------------------------------------------------
:: 4. Register with Claude Code globally (user scope)
:: ----------------------------------------------------------
echo.
echo [4/4] Registering CIP as a global MCP server in Claude Code ...

:: Remove any previous registration first (ignore errors)
claude mcp remove cip --scope user >nul 2>&1

claude mcp add --scope user cip -- "%~dp0run-cip-mcp.bat"
if errorlevel 1 (
    echo.
    echo FAILED — could not register with Claude Code.
    echo Make sure the 'claude' CLI is installed and on your PATH.
    echo   npm install -g @anthropic-ai/claude-code
    pause
    exit /b 1
)

echo.
echo ============================================================
echo   Done! CIP is now a global MCP server.
echo.
echo   It will start automatically in every Claude Code session.
echo   Verify with:  claude mcp list
echo ============================================================
echo.
pause
