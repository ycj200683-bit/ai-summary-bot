@echo off
setlocal
title AI 摘要 - 固定域名
cd /d "%~dp0"

echo ==========================================
echo   AI 摘要服务 - 固定域名模式
echo   地址: https://ai.pqalzm1029384756.xyz
echo ==========================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [x] 没有 .venv，先运行 启动.bat 按提示建环境
    pause
    exit /b 1
)

rem 8001 若已在监听，说明服务已启动，只需确认隧道
netstat -ano | findstr /i "LISTENING" | findstr /c:":8001 " >nul
if not errorlevel 1 (
    echo [!] 8001 端口已有服务在跑
    echo     如果域名报 1033 打不开，说明隧道进程没起：
    echo     把之前那个黑窗口关掉，再双击本文件
    echo.
    echo 正在打开浏览器...
    start "" "https://ai.pqalzm1029384756.xyz/"
    timeout /t 3 >nul
    exit /b 0
)

echo 约 10 秒后自动打开浏览器
echo 看到 Registered tunnel connection 表示隧道已就绪
echo 按 Ctrl+C 停止（关掉这个窗口域名就失效）
echo.
start "" ".venv\Scripts\pythonw.exe" -c "import time,webbrowser;time.sleep(12);webbrowser.open('https://ai.pqalzm1029384756.xyz/')"
call "%~dp0启动.bat" named
