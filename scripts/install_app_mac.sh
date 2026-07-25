#!/bin/bash
# Crea DictateText.app (el equivalente Mac de install_shortcut.ps1): una app
# de doble clic que corre main_mac.py.
#
#   bash scripts/install_app_mac.sh              # crea ~/Applications/DictateText.app
#   bash scripts/install_app_mac.sh /Applications
#
# La app EMBEBE el interprete de Python completo (~70 MB) en
# Contents/Resources/python. No es capricho: los permisos de macOS (TCC) se
# atribuyen al binario que hace la llamada; si Python viviera fuera del
# bundle, los dialogos dirian "python3.12" y los switches de DictateText en
# Accesibilidad / Monitoreo de entrada no aplicarian al proceso real (visto
# en macOS 26). Con el interprete adentro del bundle, firmado con el mismo
# identificador, macOS atribuye todo a "DictateText".
#
# El codigo y las dependencias siguen viviendo en el repo (main_mac.py y
# .venv/site-packages via PYTHONPATH): editar el codigo aplica al proximo
# arranque sin reinstalar. Solo hace falta re-correr este script si se
# actualiza el interprete o cambia la ruta del repo.
set -euo pipefail

APP_DIR="$(cd "$(dirname "$0")/.." && pwd)"
VENV_PY="$APP_DIR/.venv/bin/python"
if [ ! -x "$VENV_PY" ]; then
    echo "No existe $VENV_PY — crear el venv primero (README, seccion macOS)" >&2
    exit 1
fi

# .venv/bin/python -> .../uv/python/cpython-3.12.../bin/python3.12
REAL_PY="$(readlink -f "$VENV_PY")"
PY_ROOT="$(dirname "$(dirname "$REAL_PY")")"
PY_VER="$("$VENV_PY" -c 'import sys; print(f"{sys.version_info[0]}.{sys.version_info[1]}")')"
SITE="$APP_DIR/.venv/lib/python$PY_VER/site-packages"
if [ ! -d "$SITE" ]; then
    echo "No encuentro $SITE" >&2
    exit 1
fi

DEST="${1:-$HOME/Applications}"
BUNDLE="$DEST/DictateText.app"
rm -rf "$BUNDLE"
mkdir -p "$BUNDLE/Contents/MacOS" "$BUNDLE/Contents/Resources"

echo "Copiando el runtime de Python al bundle…"
ditto "$PY_ROOT" "$BUNDLE/Contents/Resources/python"
EMB_PY="$BUNDLE/Contents/Resources/python/bin/python$PY_VER"

cat > "$BUNDLE/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
 "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>CFBundleName</key>            <string>DictateText</string>
    <key>CFBundleDisplayName</key>     <string>DictateText</string>
    <key>CFBundleIdentifier</key>      <string>com.lucaspecina.dictatetext</string>
    <key>CFBundleVersion</key>         <string>1.1</string>
    <key>CFBundleShortVersionString</key> <string>1.1</string>
    <key>CFBundlePackageType</key>     <string>APPL</string>
    <key>CFBundleExecutable</key>      <string>DictateText</string>
    <key>NSHighResolutionCapable</key> <true/>
    <key>NSMicrophoneUsageDescription</key>
    <string>DictateText graba el microfono para transcribir lo que dictas.</string>
    <key>NSAppleEventsUsageDescription</key>
    <string>DictateText pausa Spotify/Music mientras dictas y los reanuda al terminar.</string>
</dict>
</plist>
PLIST

# Proceso principal del bundle: shim C que lanza el Python EMBEBIDO como
# hijo y espera. Todo el arbol de procesos queda adentro del bundle.
SHIM_SRC="$(mktemp -t dictatetext_shim).c"
cat > "$SHIM_SRC" <<'SHIM'
#include <signal.h>
#include <stdlib.h>
#include <sys/wait.h>
#include <unistd.h>

static pid_t child = 0;

static void forward(int sig) {
    if (child > 0)
        kill(child, sig);
}

int main(void) {
    child = fork();
    if (child == 0) {
        setenv("PYTHONPATH", SITE_PATH, 1);   /* deps del venv del repo */
        setenv("PYTHONNOUSERSITE", "1", 1);
        execl(PY_PATH, PY_PATH, SCRIPT_PATH, (char *)0);
        _exit(127);
    }
    signal(SIGTERM, forward);  /* que "Salir" desde el Dock cierre Python */
    signal(SIGINT, forward);
    int status = 0;
    while (waitpid(child, &status, 0) < 0) {
    }
    if (WIFEXITED(status))
        return WEXITSTATUS(status);
    return 128 + WTERMSIG(status);
}
SHIM

cc -O2 -o "$BUNDLE/Contents/MacOS/DictateText" \
   -DPY_PATH="\"$EMB_PY\"" \
   -DSCRIPT_PATH="\"$APP_DIR/main_mac.py\"" \
   -DSITE_PATH="\"$SITE\"" \
   "$SHIM_SRC"
rm -f "$SHIM_SRC"

# Firmar el interprete embebido con el identificador de la app y despues el
# bundle. Ad-hoc: suficiente para que TCC reconozca la app de forma estable.
codesign --force --identifier com.lucaspecina.dictatetext -s - "$EMB_PY"
codesign --force --identifier com.lucaspecina.dictatetext -s - "$BUNDLE"

echo
echo "Creada $BUNDLE"
echo "OJO: si ya habia entradas de DictateText en Privacidad y seguridad"
echo "(Accesibilidad / Monitoreo de entrada), el binario cambio: sacarlas"
echo "con el boton - y volver a concederlas cuando la app las pida."
