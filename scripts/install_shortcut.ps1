# Crea el acceso directo de DictateText en el Escritorio (doble click para
# abrir, sin consola). Desde ahi se puede anclar a la barra de tareas con
# click derecho -> "Anclar a la barra de tareas".
# Uso:  powershell -ExecutionPolicy Bypass -File scripts\install_shortcut.ps1

$appDir = Split-Path -Parent $PSScriptRoot
$pythonw = Join-Path $appDir ".venv\Scripts\pythonw.exe"
$mainPy = Join-Path $appDir "main.py"
$desktop = [Environment]::GetFolderPath("Desktop")
$lnkPath = Join-Path $desktop "DictateText.lnk"

if (-not (Test-Path $pythonw)) { Write-Error "No existe $pythonw (crear el venv primero)"; exit 1 }

$shell = New-Object -ComObject WScript.Shell
$lnk = $shell.CreateShortcut($lnkPath)
$lnk.TargetPath = $pythonw
$lnk.Arguments = "`"$mainPy`""
$lnk.WorkingDirectory = $appDir
$lnk.IconLocation = "%SystemRoot%\System32\mmres.dll,3"  # microfono
$lnk.Description = "Dictado por voz que pega donde estaba el cursor (Ctrl+Alt+D)"
$lnk.Save()

Write-Host "Listo: $lnkPath"
Write-Host "Doble click abre la app (si ya esta corriendo, trae su ventana al frente)."
