# Django maorif portalini sozlash skripti
# PowerShell'da ishga tushirish:
#   powershell -ExecutionPolicy Bypass -File setup.ps1

python -m venv venv

& "$PSScriptRoot\venv\Scripts\python.exe" -m pip install --upgrade pip
& "$PSScriptRoot\venv\Scripts\python.exe" -m pip install -r "$PSScriptRoot\requirements.txt"

& "$PSScriptRoot\venv\Scripts\python.exe" "$PSScriptRoot\manage.py" makemigrations
& "$PSScriptRoot\venv\Scripts\python.exe" "$PSScriptRoot\manage.py" migrate
& "$PSScriptRoot\venv\Scripts\python.exe" "$PSScriptRoot\manage.py" collectstatic --noinput

Write-Host "`nSozlash tugadi! Endi superuser yarating:"
Write-Host "  .\venv\Scripts\python.exe manage.py createsuperuser"
Write-Host "Keyin serverni ishga tushiring:"
Write-Host "  .\venv\Scripts\python.exe manage.py runserver"
