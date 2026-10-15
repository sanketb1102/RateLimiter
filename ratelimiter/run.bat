@echo off
REM ===========================================================
REM  API Rate Limiter - one-click start and test
REM  Prerequisite: Docker Desktop installed and running.
REM  Double-click this file, or run it from a terminal.
REM ===========================================================

REM Always run from this script's own folder, wherever it was saved.
cd /d "%~dp0"

setlocal enabledelayedexpansion
color 0F

echo.
echo  ============================================================
echo    API RATE LIMITER
echo  ============================================================
echo.

REM ---------- 1. Is Docker running? ---------------------------
echo  [1/5] Checking Docker...
docker info >nul 2>&1
if errorlevel 1 (
    echo.
    echo    ERROR: Docker is not running.
    echo.
    echo    Open Docker Desktop, wait until it says "Engine running",
    echo    then run this file again.
    echo.
    pause
    exit /b 1
)
echo        Docker is running.

REM ---------- 2. Config file ----------------------------------
if not exist ".env" (
    echo  [2/5] Creating .env from .env.example...
    copy ".env.example" ".env" >nul
) else (
    echo  [2/5] Config file .env found.
)

REM ---------- 3. Start the containers -------------------------
echo  [3/5] Starting containers...
echo        First run downloads images and takes 2-4 minutes.
echo        After that it takes a few seconds.
echo.
docker compose up --build -d
if errorlevel 1 (
    echo.
    echo    ERROR: docker compose failed. Scroll up for the reason.
    echo.
    pause
    exit /b 1
)

REM ---------- 4. Wait until the API actually answers -----------
echo.
echo  [4/5] Waiting for the service to come up...
set /a tries=0
:waitloop
set /a tries+=1
curl.exe -s -o nul http://localhost:8080/healthz 2>nul
if not errorlevel 1 goto ready
if %tries% geq 45 (
    echo.
    echo    ERROR: the service did not respond in 90 seconds.
    echo    Run:  docker compose logs api
    echo.
    pause
    exit /b 1
)
timeout /t 2 /nobreak >nul
goto waitloop

:ready
echo        Service is up.

REM ---------- 5. Fetch a demo key and run the test -------------
echo  [5/5] Fetching a demo API key...

REM The app prints its demo keys at startup, but Python buffers stdout, so
REM the log line can lag a second or two behind the health check. Retry.
set "KEY="
set /a ktries=0
:keyloop
set /a ktries+=1
for /f "tokens=5" %%k in ('docker compose logs api 2^>nul ^| findstr /C:"INFO:   free"') do set "KEY=%%k"
if not "!KEY!"=="" goto gotkey
if %ktries% geq 15 goto nokey
timeout /t 1 /nobreak >nul
goto keyloop

:nokey
echo.
echo    Could not read the key automatically. Get it manually with:
echo      docker compose logs api ^| findstr free
echo.
pause
exit /b 1

:gotkey

echo        Free-plan key: !KEY!
echo.
echo  ============================================================
echo    TEST: 12 requests against a limit of 10 per 60 seconds
echo  ============================================================
echo.
echo    Expected:  200 x10, then 429 x2
echo.
echo    Actual:
echo.
echo|set /p="     "
for /l %%i in (1,1,12) do curl.exe -s -o nul -w "%%{http_code} " -H "X-API-Key: !KEY!" http://localhost:8080/api/ping
echo.
echo.
echo  ============================================================
echo.
echo    200 = allowed.   429 = Too Many Requests.
echo.
echo    Ten got through, then the limiter started refusing.
echo    That is the whole project.
echo.
echo    NOTE: run this again within the same minute and you will
echo          see all 429s - that key's 10 requests are spent.
echo          Wait 60 seconds for the window to roll.
echo.
echo  ------------------------------------------------------------
echo    Try these in a browser:
echo      http://localhost:8080/admin/usage    live usage per key
echo      http://localhost:8080/docs           API documentation
echo.
echo    The concurrency test (needs: pip install httpx):
echo      python loadtest\burst.py
echo.
echo    To stop everything:
echo      docker compose down
echo  ------------------------------------------------------------
echo.
pause
