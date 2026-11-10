@echo off
rem Установка склада как службы Windows. Двойной щелчок, Windows спросит права администратора.
rem Склад другого отдела: install.bat .env.otdel2
cd /d "%~dp0"
if "%~1"=="" (
  powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -NoExit -File ""%~dp0install.ps1""'"
) else (
  powershell -NoProfile -Command "Start-Process powershell -Verb RunAs -ArgumentList '-NoProfile -ExecutionPolicy Bypass -NoExit -File ""%~dp0install.ps1"" -EnvFile %~1'"
)
