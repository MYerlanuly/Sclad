# Установка склада на Windows-сервер как службы (автозапуск при включении сервера).
# Запуск: двойной щелчок по scripts\windows\install.bat (или от администратора:
#   powershell -ExecutionPolicy Bypass -File scripts\windows\install.ps1 [-EnvFile .env.otdel2])
# Скрипт можно запускать повторно: что уже сделано, он пропускает.
# Подробно: docs\DEPLOY_SERVER.md
param(
    [string]$EnvFile = ""
)
$ErrorActionPreference = "Stop"
$Root = (Resolve-Path (Join-Path $PSScriptRoot "..\..")).Path
Set-Location $Root
$env:PYTHONIOENCODING = "utf-8"
[Console]::OutputEncoding = [System.Text.Encoding]::UTF8

function Step($text) { Write-Host ""; Write-Host "== $text" -ForegroundColor Cyan }
function Fail($text) { Write-Host ""; Write-Host "ОШИБКА: $text" -ForegroundColor Red; exit 1 }

$admin = ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole(
    [Security.Principal.WindowsBuiltInRole]::Administrator)
if (-not $admin) { Fail "Запустите от имени администратора (scripts\windows\install.bat сделает это сам)." }

# 1. Python и виртуальное окружение
Step "1. Python и зависимости"
$venvPy = Join-Path $Root "venv\Scripts\python.exe"
if (-not (Test-Path $venvPy)) {
    if (Get-Command py -ErrorAction SilentlyContinue) { & py -3 -m venv venv }
    elseif (Get-Command python -ErrorAction SilentlyContinue) { & python -m venv venv }
    else { Fail "Python не найден. Установите Python 3.13 с python.org (галочка «Add python.exe to PATH»)." }
    if ($LASTEXITCODE -ne 0) { Fail "Не удалось создать venv." }
}
$pipArgs = @("-m", "pip", "install", "--disable-pip-version-check", "-q", "-r", "requirements.txt")
if (Test-Path (Join-Path $Root "wheels")) { $pipArgs += @("--no-index", "--find-links", "wheels") }
& $venvPy @pipArgs
if ($LASTEXITCODE -ne 0) { Fail "pip install не выполнен (нет интернета? см. раздел про папку wheels в docs\DEPLOY_SERVER.md)." }
Write-Host "  готово"

# 2. Файл настроек
Step "2. Настройки"
$dotenv = Join-Path $Root ".env"
if (-not (Test-Path $dotenv)) {
    $secret = & $venvPy -c "import secrets; print(secrets.token_hex(32))"
    (Get-Content (Join-Path $Root ".env.example") -Encoding UTF8) `
        -replace "^FLASK_SECRET_KEY=.*", "FLASK_SECRET_KEY=$secret" `
        -replace "^FLASK_DEBUG=.*", "FLASK_DEBUG=0" |
        Set-Content $dotenv -Encoding UTF8
    Write-Host "  создан .env с новым секретным ключом"
} else { Write-Host "  .env уже есть" }
$env:SCLAD_ENV_FILE = ""
if ($EnvFile) {
    if (-not (Test-Path (Join-Path $Root $EnvFile))) { Fail "Файл настроек склада $EnvFile не найден (создайте из .env.otdel.example)." }
    $env:SCLAD_ENV_FILE = $EnvFile
}
$env:SCLAD_BACKUP_SCHEDULER = "0"
$cfgJson = & $venvPy -c "import json; from sclad import config as c; print(json.dumps({'instance': c.INSTANCE, 'name': c.NAME, 'db': c.DATABASE, 'port': c.PORT}))"
if ($LASTEXITCODE -ne 0) { Fail "Не удалось прочитать настройки склада." }
$cfg = $cfgJson | Select-Object -Last 1 | ConvertFrom-Json
Write-Host "  склад: $($cfg.name), база: $($cfg.db), порт: $($cfg.port)"

# 3. База и первый пользователь
Step "3. База данных"
if (Test-Path $cfg.db) {
    Write-Host "  база уже есть, оставляю как есть"
} else {
    & $venvPy -m flask --app app init-db
    if ($LASTEXITCODE -ne 0) { Fail "Не удалось создать базу." }
    Write-Host "  Создадим администратора склада."
    $login = Read-Host "  Логин"
    $secure = Read-Host "  Пароль" -AsSecureString
    $pass = [Runtime.InteropServices.Marshal]::PtrToStringAuto([Runtime.InteropServices.Marshal]::SecureStringToBSTR($secure))
    & $venvPy -m flask --app app create-user -u $login -p $pass -r admin
    if ($LASTEXITCODE -ne 0) { Fail "Не удалось создать пользователя." }
}
Remove-Item Env:SCLAD_BACKUP_SCHEDULER

# 4. Служба (задача планировщика: запуск при старте Windows от имени SYSTEM, перезапуск при сбое)
Step "4. Служба"
$taskName = if ($cfg.instance) { "Sclad-$($cfg.instance)" } else { "Sclad" }
$serveArgs = "serve.py"
if ($EnvFile) { $serveArgs = "serve.py --env `"$EnvFile`"" }
$action = New-ScheduledTaskAction -Execute $venvPy -Argument $serveArgs -WorkingDirectory $Root
$trigger = New-ScheduledTaskTrigger -AtStartup
$principal = New-ScheduledTaskPrincipal -UserId "SYSTEM" -LogonType ServiceAccount -RunLevel Highest
$settings = New-ScheduledTaskSettingsSet -ExecutionTimeLimit ([TimeSpan]::Zero) -RestartCount 999 `
    -RestartInterval (New-TimeSpan -Minutes 1) -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -MultipleInstances IgnoreNew
if (Get-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue) {
    Stop-ScheduledTask -TaskName $taskName -ErrorAction SilentlyContinue
    Unregister-ScheduledTask -TaskName $taskName -Confirm:$false
}
Register-ScheduledTask -TaskName $taskName -Action $action -Trigger $trigger -Principal $principal `
    -Settings $settings -Description "$($cfg.name): веб-сайт склада (serve.py)" | Out-Null
Write-Host "  задача планировщика «$taskName» создана"

# 5. Брандмауэр
$ruleName = "$taskName (порт $($cfg.port))"
if (-not (Get-NetFirewallRule -DisplayName $ruleName -ErrorAction SilentlyContinue)) {
    New-NetFirewallRule -DisplayName $ruleName -Direction Inbound -Protocol TCP -LocalPort $cfg.port -Action Allow | Out-Null
    Write-Host "  открыт порт $($cfg.port) в брандмауэре"
}

# 6. Запуск и проверка
Step "5. Запуск"
Start-ScheduledTask -TaskName $taskName
$url = "http://127.0.0.1:$($cfg.port)/login"
$ok = $false
for ($i = 0; $i -lt 20; $i++) {
    Start-Sleep -Seconds 2
    try { if ((Invoke-WebRequest $url -UseBasicParsing -TimeoutSec 5).StatusCode -eq 200) { $ok = $true; break } } catch {}
}
if ($ok) {
    Write-Host ""
    Write-Host "Готово. Склад открывается с других компьютеров: http://$($env:COMPUTERNAME):$($cfg.port)" -ForegroundColor Green
    Write-Host "Обновление в будущем: двойной щелчок по update.bat в папке $Root"
} else {
    Fail "Сайт не ответил на $url. Посмотрите журнал в папке logs\ и задачу «$taskName» в Планировщике заданий."
}
