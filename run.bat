@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [v0-auto] 安装依赖...
python -m pip install -r requirements.txt -q
if errorlevel 1 (
  echo [v0-auto] 依赖安装失败，已停止。
  pause
  exit /b 1
)
echo [v0-auto] 安装 Playwright 浏览器内核（若已安装会跳过）...
python -m playwright install chromium
if errorlevel 1 (
  echo [v0-auto] Playwright 浏览器内核安装失败，已停止。
  pause
  exit /b 1
)
echo [v0-auto] 开始运行...
python run.py %*
set "EXIT_CODE=%ERRORLEVEL%"
echo [v0-auto] 任务结束，退出码: %EXIT_CODE%
pause
exit /b %EXIT_CODE%
