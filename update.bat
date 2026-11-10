@echo off
rem Обновление склада на Windows-сервере в один шаг: двойной щелчок по этому файлу.
rem Откат: update.bat --rollback   (и базы: update.bat --rollback --restore-db)
rem Подробно: docs\DEPLOY_SERVER.md
chcp 65001 >nul
cd /d "%~dp0"
net session >nul 2>&1
if errorlevel 1 (
  echo Нужны права администратора, сейчас Windows спросит разрешение...
  if "%~1"=="" (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -Verb RunAs"
  ) else (
    powershell -NoProfile -Command "Start-Process -FilePath '%~f0' -ArgumentList '%*' -Verb RunAs"
  )
  exit /b
)
if not exist venv\Scripts\python.exe (
  echo Нет venv\Scripts\python.exe. Сначала установите склад по docs\DEPLOY_SERVER.md
  pause
  exit /b 1
)
set PYTHONIOENCODING=utf-8
venv\Scripts\python.exe scripts\update.py %*
echo.
pause
