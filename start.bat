@echo off
chcp 65001 >nul
cd /d "%~dp0"
title QuantDesk
echo.
echo  QuantDesk 正在启动…
echo  请保持本窗口打开，直到浏览器自动弹出。
echo.
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launch.ps1" start
if errorlevel 1 (
  echo.
  echo  启动失败。常见原因：未安装 Docker Desktop，或首次构建尚未完成。
  echo  说明见 使用说明.txt
)
echo.
pause
