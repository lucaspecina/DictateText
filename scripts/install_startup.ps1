# Crea un acceso directo en la carpeta de inicio de Windows para que
# DictateText arranque solo (sin consola) al prender la PC.
# Uso:  powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1

$appDir = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $appDir ".venv\Scripts\pythonw.exe"
$mainPy = Join-Path $appDir "main.py"
$startup = [Environment]::GetFolderPath("Startup")
$lnkPath = Join-Path $startup "DictateText.lnk"

if (-not (Test-Path $pythonw)) { Write-Error "No existe $pythonw (crear el venv primero)"; exit 1 }

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath = $pythonw
$lnk.Arguments = "`"$mainPy`""
$lnk.WorkingDirectory = $appDir
$lnk.Description = "Dictado por voz que pega donde estaba el cursor (Ctrl+Alt+D)"
$lnk.Save()

Write-Host "Listo: $lnkPath"
Write-Host "Arranca solo en el proximo inicio de sesion. Para probarlo ya:  & '$pythonw' '$mainPy'"
