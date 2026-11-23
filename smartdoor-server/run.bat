@echo off
setlocal
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
  where py >nul 2>nul
  if not errorlevel 1 (
    py -3.12 -m venv .venv 2>nul || py -3 -m venv .venv
  ) else (
    python -m venv .venv
  )
  if errorlevel 1 goto failed
)
call ".venv\Scripts\activate.bat"
if errorlevel 1 goto failed

echo.
echo ===== SmartDoor AIoT =====
echo Dia chi LAN cua may tinh (dien mot dia chi nay vao SERVER_IP trong wifi_config.h):
powershell -NoProfile -Command "Get-NetIPAddress -AddressFamily IPv4 -ErrorAction SilentlyContinue | Where-Object {$_.IPAddress -notlike '127.*' -and $_.IPAddress -notlike '169.254.*' -and $_.InterfaceAlias -notmatch 'Loopback|vEthernet|WSL|Bluetooth'} | Select-Object -ExpandProperty IPAddress -Unique | ForEach-Object {Write-Output ('  ' + $_)}"
echo Dashboard:            http://127.0.0.1:8000/
echo Snapshot kiem tra:    http://127.0.0.1:8000/api/camera/snapshot.jpg
echo WebSocket ESP32-CAM:  ws://^<IPv4-LAN^>:8000/ws/esp32
echo WebSocket ESP32-S3:   ws://^<IPv4-LAN^>:8000/ws/esp32-control
echo ==========================
echo.

"%VIRTUAL_ENV%\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 goto failed

set SERVER_ALREADY_RUNNING=0
powershell -NoProfile -Command "try {$r=Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8000/api/status' -TimeoutSec 2; if($r.StatusCode -eq 200){exit 0}}catch{};exit 1" >nul 2>nul
if not errorlevel 1 set SERVER_ALREADY_RUNNING=1
if "%SERVER_ALREADY_RUNNING%"=="0" (
  start "SmartDoor AIoT Server" /D "%CD%" "%VIRTUAL_ENV%\Scripts\python.exe" -m uvicorn app:app --host 0.0.0.0 --port 8000
  powershell -NoProfile -Command "$ok=$false; for($i=0;$i -lt 45;$i++){try{$r=Invoke-WebRequest -UseBasicParsing 'http://127.0.0.1:8000/api/status' -TimeoutSec 1;if($r.StatusCode -eq 200){$ok=$true;break}}catch{};Start-Sleep 1};if($ok){exit 0}else{exit 1}"
  if errorlevel 1 goto failed
) else (
  echo SmartDoor server is already running.
)
start "" "http://127.0.0.1:8000/"
echo Dashboard is open. Server console is in the separate SmartDoor window.
goto done

:failed
echo.
echo SmartDoor setup/start failed. Review the error above.
pause
:done
endlocal
