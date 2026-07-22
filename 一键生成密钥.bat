@echo off
chcp 65001 >nul
cd /d "%~dp0"
title v0 一键生成单个 API Key

echo ============================================
echo   v0 单密钥生成（比特浏览器 + 代理 7890）
echo ============================================
echo.
echo [准备] 请确认:
echo   1. 比特浏览器 客户端已打开，本地 API 已开启（54345）
echo   2. 本机代理已开，HTTP 端口 7890（Clash/v2ray 等）
echo   3. 若邮箱站出现 Cloudflare，在弹出窗口里手动点一下
echo.
pause

python -m pip install -r requirements.txt -q
if errorlevel 1 (
  echo [失败] 依赖安装失败。
  pause
  exit /b 1
)
python -m playwright install chromium >nul 2>&1
if errorlevel 1 (
  echo [失败] Playwright 浏览器内核安装失败。
  pause
  exit /b 1
)

echo.
echo [运行] 正在生成 1 个临时邮箱并注册...
python one_key.py
set "EXIT_CODE=%ERRORLEVEL%"
echo.
echo [结果] 查看:
echo   data\keys\all_keys.txt
echo   data\keys\
echo   data\accounts\
echo.
pause
exit /b %EXIT_CODE%
