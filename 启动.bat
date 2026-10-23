@echo off
setlocal
title AI 摘要服务
cd /d "%~dp0"

echo ==========================================
echo   AI 文本摘要服务
echo ==========================================
echo.

rem ---------- 1. 环境检查 ----------
if not exist ".venv\Scripts\python.exe" (
    echo [x] 找不到 .venv\Scripts\python.exe
    echo.
    echo     先建环境（只需做一次）:
    echo       python -m venv .venv
    echo       .venv\Scripts\pip install -r requirements-web.txt
    echo.
    pause
    exit /b 1
)

if not exist ".env" (
    echo [!] 没有 .env：页面能打开，但摘要会失败（缺 API_KEY）
    echo.
)

rem ---------- 2. 选模式：默认本地，也可传参 lan / tunnel / named ----------
set "MODE=local"
if /i "%~1"=="lan"    set "MODE=lan"
if /i "%~1"=="tunnel" set "MODE=tunnel"
if /i "%~1"=="named"  set "MODE=named"

set "PORT=8000"
if /i "%MODE%"=="named" set "PORT=8001"

rem ---------- 3. 端口占用检查 ----------
netstat -ano | findstr /i "LISTENING" | findstr /c:":%PORT% " >nul
if not errorlevel 1 (
    if "%PORT%"=="8001" (
        echo [x] 8000 和 8001 都被占用，先关掉占用的程序
        pause
        exit /b 1
    )
    echo [!] 端口 8000 被占用，改用 8001
    set "PORT=8001"
)

rem ---------- 4. 按模式启动 ----------
if /i "%MODE%"=="local"  goto :run_local
if /i "%MODE%"=="lan"    goto :run_lan
if /i "%MODE%"=="tunnel" goto :run_tunnel
goto :run_named

:run_local
echo 模式: 本地（只在这台电脑看）
echo 地址: http://127.0.0.1:%PORT%/
echo.
echo 3 秒后自动开浏览器，按 Ctrl+C 停止服务
echo.
start "" ".venv\Scripts\pythonw.exe" -c "import time,webbrowser;time.sleep(3);webbrowser.open('http://127.0.0.1:%PORT%/')"
".venv\Scripts\python.exe" -m uvicorn app:app --host 127.0.0.1 --port %PORT%
goto :done

:run_lan
echo 模式: 局域网（同一个 WiFi 下的同学可访问）
echo.
".venv\Scripts\python.exe" share.py --port %PORT%
goto :done

:run_tunnel
echo 模式: 临时公网链接（地址每次变，关掉就失效）
echo.
".venv\Scripts\python.exe" share.py --port %PORT% --tunnel
goto :done

:run_named
echo 模式: 固定域名（读 ~/.cloudflared/config.yml）
echo.
".venv\Scripts\python.exe" share.py --port %PORT% --named
goto :done

:done
echo.
echo 服务已停止。
pause
