@echo off
chcp 65001 >nul
cd /d "%~dp0"
title QuantDesk 关闭
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0scripts\launch.ps1" stop
echo.
pause
