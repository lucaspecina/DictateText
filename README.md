# DictateText

El inverso de [SpeakSelectedText](../SpeakSelectedText/): dejás el cursor en cualquier campo de texto de cualquier app (Windows o Mac), apretás **Ctrl+Alt+D**, hablás, apretás **Ctrl+Alt+D** de nuevo y el texto transcripto **se pega solo donde estaba el cursor** (si tenías texto seleccionado, lo reemplaza). Un overlay flotante abajo a la derecha muestra la transcripción en vivo mientras hablás.

| Atajo global | Acción |
|---|---|
| `Ctrl+Alt+D` | Empezar a dictar / terminar y pegar (toggle) |
| `Ctrl+Alt+X` | Cancelar el dictado en curso (descarta todo) |
| `Ctrl+Alt+Shift+D` | Salir |

**El beep significa "ya podés hablar"**: suena recién cuando el micrófono entregó su primer chunk de audio real (un headset Bluetooth tarda ~1s en activar el modo manos-libres; lo hablado antes de eso no llega a grabarse). Esperá el beep y no se pierde ni la primera sílaba.

Si hay música o video sonando (Spotify, YouTube, etc.), **se pausa solo mientras dictás y se reanuda al terminar** — via las media sessions de Windows ([media.py](media.py), el mismo módulo de SpeakSelectedText); se apaga con `DUCK_MEDIA=0`.

**Jerga técnica**: [phrases.txt](phrases.txt) le da contexto al reconocedor — términos que decís en inglés en medio del español ("commit", "push", "deploy", nombres propios). Un término por línea, se relee en cada dictado (editar y guardar alcanza, sin reiniciar). Requiere un locale que soporte phrase lists (`es-MX`/`es-ES` sí, `es-AR` no).

Usa el servicio Speech del recurso Azure AI Foundry (`amalia-resource`, East US 2) — **misma key y mismo endpoint que SpeakSelectedText**, no hace falta ningún recurso extra. Detecta solo si hablás en español o inglés (`LANGUAGES`), pone puntuación automáticamente, y transcribe en **streaming**: cuando cortás, el resultado ya está casi listo (<1s), no espera a procesar todo el audio junto.

## Cómo funciona (y por qué pega en el lugar correcto)

1. Hotkey global con `RegisterHotKey`: **no roba el foco**, así que el caret del campo destino ni se entera de que empezaste a grabar. Antes de nada se guarda `GetForegroundWindow()` (la ventana destino).
2. El micrófono ([recorder.py](recorder.py), sounddevice/PortAudio) empuja PCM 16 kHz a un `PushAudioInputStream` y Azure reconoce **mientras hablás** ([stt.py](stt.py)), con parciales en vivo en el overlay.
3. Al cortar: se cierra el stream, llegan los resultados finales, y si te fuiste a otra ventana mientras dictabas, `SetForegroundWindow` **reactiva la ventana original** — permitido porque venimos de un hotkey, que le da al proceso permiso de foreground. Windows le devuelve el foco al mismo control, que conservó caret y selección.
4. Se guarda el clipboard actual, se pone el texto, se simula `Ctrl+V` (`SendInput`, esperando a que sueltes físicamente los modificadores del hotkey), y se restaura el clipboard anterior.

Si la ventana original ya no existe o no se deja reactivar, **no pega a ciegas**: el texto queda en el portapapeles y avisa (beep + mensaje) para que lo pegues vos con `Ctrl+V`.

- [main.py](main.py) — capa Windows: hotkeys, foreground/foco, clipboard, SendInput, GUI y overlay (tkinter).
- [main_mac.py](main_mac.py) — la misma capa para macOS: event tap de Quartz (hotkeys), NSWorkspace/NSRunningApplication (foco), NSPasteboard (clipboard), CGEvent Cmd+V (pegado). Misma máquina de estados y misma GUI.
- [media.py](media.py) / [media_mac.py](media_mac.py) — pausar/reanudar la música (media sessions de Windows / AppleScript a Spotify y Music).
- [stt.py](stt.py) — backend Azure Speech STT streaming, compartido; cambiar de motor (p. ej. gpt-4o-transcribe) es agregar una clase acá.
- [recorder.py](recorder.py) — captura de micrófono con sounddevice, compartido.

El overlay usa `WS_EX_NOACTIVATE`: sus botones (⏹ pegar, ✕ cancelar) andan con el mouse pero la ventanita **nunca toma el foco** — si lo tomara, el `Ctrl+V` iría a parar ahí en vez del campo destino.

Cerrar la ventana principal **no** cierra la app (queda en los hotkeys); salir es `Ctrl+Alt+Shift+D` o el botón Salir. La ventana principal tiene un **campo de prueba**: clic adentro, dictás, y ves el circuito completo sin salir de la app.

## Setup (Windows)

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy .env.example .env   # completar SPEECH_KEY y SPEECH_ENDPOINT (los mismos de SpeakSelectedText)
```

Validar credenciales y micrófono sin la capa de hotkeys:

```powershell
.venv\Scripts\python test_stt.py              # graba 5s del mic y transcribe
.venv\Scripts\python test_stt.py --wav f.wav  # transcribe un WAV 16 kHz mono (sin mic)
```

## Uso (Windows)

```powershell
.venv\Scripts\python main.py          # con consola (para ver logs)
.venv\Scripts\pythonw.exe main.py     # sin consola
```

## Acceso directo en el Escritorio

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_shortcut.ps1
```

Crea `DictateText.lnk` en el Escritorio (doble click abre la app sin consola; si ya está corriendo, trae su ventana al frente gracias a la instancia única). Desde ahí: click derecho → **"Anclar a la barra de tareas"** para tenerla siempre a mano.

## Arranque automático con Windows

```powershell
powershell -ExecutionPolicy Bypass -File scripts\install_startup.ps1
```

Crea un acceso directo a `pythonw.exe main.py` en `shell:startup` (mismo esquema que SpeakSelectedText: sin PyInstaller, sin falsos positivos de antivirus). Para desinstalar, borrar `DictateText.lnk` de esa carpeta.

## Configuración (`.env`)

| Variable | Default | Notas |
|---|---|---|
| `SPEECH_KEY` / `SPEECH_ENDPOINT` | — | Key y endpoint `*.cognitiveservices.azure.com` del recurso Foundry |
| `LANGUAGES` | `es-MX,en-US` | Detección automática entre estos idiomas (máx. 10); uno solo = fijo, más rápido. Ojo: `es-AR` no soporta phrase lists y transcribe mal los anglicismos |
| `LID_MODE` | `AtStart` | `AtStart` decide el idioma al comienzo del dictado y no cambia más; `Continuous` puede saltar de idioma a mitad de frase (arriesgado) |
| `DICTATE_HOTKEY` / `CANCEL_HOTKEY` / `QUIT_HOTKEY` | `ctrl+alt+d` / `ctrl+alt+x` / `ctrl+alt+shift+d` | Formato `ctrl+alt+<letra\|dígito\|fN>`. En Mac también valen `cmd` y `fn` (la tecla 🌐), p. ej. `fn+shift+d` |
| `MIC_DEVICE` | *(default del sistema)* | Vacío = usa el mic default **de comunicaciones** de Windows (el headset si hay uno; se re-resuelve en cada dictado, así que enchufar/desenchufar el headset alcanza). Un nombre (o fragmento) lo fija a mano; también elegible desde la GUI |
| `MAX_SECONDS` | `300` | Corte automático de la grabación (seguridad de costo) |
| `DUCK_MEDIA` | `1` | Pausar la música/video mientras se dicta y reanudar al terminar |
| `RESTORE_CLIPBOARD` | `1` | Restaurar el clipboard anterior después de pegar |
| `RESTORE_DELAY_MS` | `300` | Cuánto esperar a que la app destino lea el clipboard antes de restaurarlo |

## Troubleshooting (Windows)

- **"No pude registrar el hotkey"** — otra app ya usa esa combinación; cambiar `DICTATE_HOTKEY` en `.env`.
- **No graba nada / "ERROR al arrancar"** — verificar Configuración → Privacidad → Micrófono → permitir apps de escritorio. Probar con `test_stt.py`. Elegir otro micrófono en el dropdown de la GUI.
- **Transcribe pero no pega** — la app destino corre elevada (como admin) y bloquea el input sintético, o se cerró: el texto queda en el portapapeles, pegalo con `Ctrl+V`.
- **Error de Azure en el log** — correr `test_stt.py` para aislar si es credencial/red o la capa de hotkey.
- **Se pierden las primeras palabras** — empezaste a hablar antes del beep: el mic (sobre todo Bluetooth) todavía no estaba entregando audio. El log dice cuánto tardó ("Mic listo en N ms").
- **Mezcla mal los idiomas** — fijar uno solo: `LANGUAGES=es-AR`.

## macOS

La versión Mac es [main_mac.py](main_mac.py) (+ [media_mac.py](media_mac.py)); comparte `stt.py`, `recorder.py`, `phrases.txt` y el `.env` con la versión Windows. Los atajos son los mismos (`Ctrl+Alt+D`, con Ctrl/Alt de verdad, no Cmd; se puede poner `cmd` en el `.env` si se prefiere). Al pegar simula **Cmd+V**.

### Setup (Mac)

Hace falta un Python con tkinter. Si el de tu sistema no lo tiene (el de Homebrew no lo trae por defecto), lo más simple es un Python standalone de [uv](https://docs.astral.sh/uv/) — nativo ARM y trae Tk:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh   # si no tenés uv
uv python install 3.12
"$(uv python find 3.12)" -m venv .venv    # o la ruta bajo ~/.local/share/uv/python/
.venv/bin/pip install -r requirements-mac.txt
cp .env.example .env                      # completar SPEECH_KEY y SPEECH_ENDPOINT
.venv/bin/python test_stt.py              # validar credenciales + micrófono
.venv/bin/python main_mac.py              # correr la app
```

### App de doble clic (como el .lnk de Windows)

```bash
bash scripts/install_app_mac.sh
```

Crea `~/Applications/DictateText.app`: un lanzador liviano (sin PyInstaller, igual que en Windows) que corre `main_mac.py` con el venv del repo. Desde ahí: doble clic para abrirla, arrastrarla al Dock para anclarla, y Ajustes → General → **Ítems de inicio** para que arranque sola al login. Si ya está corriendo, el doble clic trae su ventana al frente (instancia única). Como la app corre el código del repo, editar y guardar aplica al próximo arranque — no hay nada que "recompilar".

### Permisos (una sola vez)

macOS le pide los permisos a la app responsable — **DictateText** si usás la `.app`, o Terminal/iTerm/VS Code si la corrés de consola:

1. **Carpetas protegidas**: si el repo vive en Escritorio/Documentos, el primer arranque desde Finder pide acceso a esa carpeta — la app queda **congelada hasta que aceptes** ese diálogo.
2. **Monitoreo de entrada** (Input Monitoring): necesario para que el event tap escuche el atajo global. macOS lo pide cuando la app intenta crear el tap.
3. **Accesibilidad**: necesario para inyectar el Cmd+V (y macOS también lo exige para taps activos). La app dispara el diálogo al arrancar.
   Son **dos permisos distintos y hacen falta ambos**; después de concederlos hay que **reiniciar la app**.
4. **Micrófono**: lo pide solo en el primer dictado.
5. **Automatización** (controlar Spotify/Music): lo pide la primera vez que pausa la música; con `DUCK_MEDIA=0` no hace falta.

Ojo: si Terminal tiene activado "Secure Keyboard Entry", los event taps no ven el teclado y los hotkeys no andan.

### AirPods y el micrófono

El default sigue al **dispositivo en uso** (como en Windows): si los AirPods están conectados, dicta por los AirPods; si no, por el mic integrado. No hay que configurar nada; el desplegable de la GUI queda para fijar un mic puntual.

Dos cosas propias de dictar por AirPods:

- **El audio se degrada mientras dictás** (y puede pegar un salto de volumen al empezar/terminar): al abrirse su micrófono, los AirPods pasan entero al perfil Bluetooth de llamada, y al soltar el mic vuelven al de música. Es el protocolo — el dictado nativo de Apple hace exactamente lo mismo. Si preferís que la música ni se toque, elegí "MacBook Pro Microphone" en el desplegable.
- **Rate nativo obligatorio**: CoreAudio acepta abrir el mic de los AirPods a 16 kHz pero entrega puro silencio (ceros, sin error). [recorder.py](recorder.py) los abre al rate nativo (24 kHz) y remuestrea a los 16 kHz que espera Azure. Esto es solo-Mac; en Windows no cambia nada.

### Troubleshooting (Mac)

- **"No pude crear el event tap"** — faltan permisos: hacen falta **Monitoreo de entrada** Y **Accesibilidad** (son dos permisos distintos), y reabrir la app después de darlos.
- **Transcribe pero no pega** — igual que en Windows, el texto queda en el portapapeles: pegalo con `Cmd+V`. Revisá Accesibilidad (la inyección de teclas usa ese permiso).
- **"No entendí nada" con pico de mic bajo en el log** — está escuchando el mic equivocado (¿elegiste los AirPods?); volvé a "(default)" en el desplegable.
- **No pausa la música del navegador** — instalá [media-control](https://github.com/ungive/media-control): `brew tap ungive/media-control && brew install media-control`. Con eso DictateText usa el "Now Playing" del sistema (lo mismo que muestra el Centro de Control) y pausa también YouTube/Chrome/Safari, como la versión Windows. Sin esa herramienta, solo se pausan Spotify y Music (AppleScript). En Macs ARM con Homebrew de Intel se detecta solo y corre bajo Rosetta.
- **El atajo con `fn` no responde en un teclado externo** — el software del teclado (p. ej. Logi Options+) puede quedarse con la tecla fn; usá el teclado de la Mac o un atajo con ctrl/alt/cmd.
