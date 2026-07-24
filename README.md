# DictateText

El inverso de [SpeakSelectedText](../SpeakSelectedText/): dejás el cursor en cualquier campo de texto de cualquier app de Windows, apretás **Ctrl+Alt+D**, hablás, apretás **Ctrl+Alt+D** de nuevo y el texto transcripto **se pega solo donde estaba el cursor** (si tenías texto seleccionado, lo reemplaza). Un overlay flotante abajo a la derecha muestra la transcripción en vivo mientras hablás.

| Atajo global | Acción |
|---|---|
| `Ctrl+Alt+D` | Empezar a dictar / terminar y pegar (toggle) |
| `Ctrl+Alt+X` | Cancelar el dictado en curso (descarta todo) |
| `Ctrl+Alt+Shift+D` | Salir |

Si hay música o video sonando (Spotify, YouTube, etc.), **se pausa solo mientras dictás y se reanuda al terminar** — via las media sessions de Windows ([media.py](media.py), el mismo módulo de SpeakSelectedText); se apaga con `DUCK_MEDIA=0`.

Usa el servicio Speech del recurso Azure AI Foundry (`amalia-resource`, East US 2) — **misma key y mismo endpoint que SpeakSelectedText**, no hace falta ningún recurso extra. Detecta solo si hablás en español o inglés (`LANGUAGES`), pone puntuación automáticamente, y transcribe en **streaming**: cuando cortás, el resultado ya está casi listo (<1s), no espera a procesar todo el audio junto.

## Cómo funciona (y por qué pega en el lugar correcto)

1. Hotkey global con `RegisterHotKey`: **no roba el foco**, así que el caret del campo destino ni se entera de que empezaste a grabar. Antes de nada se guarda `GetForegroundWindow()` (la ventana destino).
2. El micrófono ([recorder.py](recorder.py), sounddevice/PortAudio) empuja PCM 16 kHz a un `PushAudioInputStream` y Azure reconoce **mientras hablás** ([stt.py](stt.py)), con parciales en vivo en el overlay.
3. Al cortar: se cierra el stream, llegan los resultados finales, y si te fuiste a otra ventana mientras dictabas, `SetForegroundWindow` **reactiva la ventana original** — permitido porque venimos de un hotkey, que le da al proceso permiso de foreground. Windows le devuelve el foco al mismo control, que conservó caret y selección.
4. Se guarda el clipboard actual, se pone el texto, se simula `Ctrl+V` (`SendInput`, esperando a que sueltes físicamente los modificadores del hotkey), y se restaura el clipboard anterior.

Si la ventana original ya no existe o no se deja reactivar, **no pega a ciegas**: el texto queda en el portapapeles y avisa (beep + mensaje) para que lo pegues vos con `Ctrl+V`.

- [main.py](main.py) — capa Windows: hotkeys, foreground/foco, clipboard, SendInput, GUI y overlay (tkinter). Es lo único a reescribir para un port a Mac.
- [stt.py](stt.py) — backend Azure Speech STT streaming; cambiar de motor (p. ej. gpt-4o-transcribe) es agregar una clase acá.
- [recorder.py](recorder.py) — captura de micrófono con sounddevice. Portable.

El overlay usa `WS_EX_NOACTIVATE`: sus botones (⏹ pegar, ✕ cancelar) andan con el mouse pero la ventanita **nunca toma el foco** — si lo tomara, el `Ctrl+V` iría a parar ahí en vez del campo destino.

Cerrar la ventana principal **no** cierra la app (queda en los hotkeys); salir es `Ctrl+Alt+Shift+D` o el botón Salir. La ventana principal tiene un **campo de prueba**: clic adentro, dictás, y ves el circuito completo sin salir de la app.

## Setup

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

## Uso

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
| `LANGUAGES` | `es-AR,en-US` | Detección automática entre estos idiomas (máx. 10); uno solo = fijo, más rápido |
| `DICTATE_HOTKEY` / `CANCEL_HOTKEY` / `QUIT_HOTKEY` | `ctrl+alt+d` / `ctrl+alt+x` / `ctrl+alt+shift+d` | Formato `ctrl+alt+<letra\|dígito\|fN>` |
| `MIC_DEVICE` | *(default del sistema)* | Vacío = usa el mic default **de comunicaciones** de Windows (el headset si hay uno; se re-resuelve en cada dictado, así que enchufar/desenchufar el headset alcanza). Un nombre (o fragmento) lo fija a mano; también elegible desde la GUI |
| `MAX_SECONDS` | `300` | Corte automático de la grabación (seguridad de costo) |
| `DUCK_MEDIA` | `1` | Pausar la música/video mientras se dicta y reanudar al terminar |
| `RESTORE_CLIPBOARD` | `1` | Restaurar el clipboard anterior después de pegar |
| `RESTORE_DELAY_MS` | `300` | Cuánto esperar a que la app destino lea el clipboard antes de restaurarlo |

## Troubleshooting

- **"No pude registrar el hotkey"** — otra app ya usa esa combinación; cambiar `DICTATE_HOTKEY` en `.env`.
- **No graba nada / "ERROR al arrancar"** — verificar Configuración → Privacidad → Micrófono → permitir apps de escritorio. Probar con `test_stt.py`. Elegir otro micrófono en el dropdown de la GUI.
- **Transcribe pero no pega** — la app destino corre elevada (como admin) y bloquea el input sintético, o se cerró: el texto queda en el portapapeles, pegalo con `Ctrl+V`.
- **Error de Azure en el log** — correr `test_stt.py` para aislar si es credencial/red o la capa de hotkey.
- **Mezcla mal los idiomas** — fijar uno solo: `LANGUAGES=es-AR`.

## Port a Mac (pendiente)

`stt.py` y `recorder.py` andan tal cual (sounddevice y el SDK de Azure soportan macOS, incluso ARM). Hay que reescribir solo la capa de sistema de `main.py`: hotkey global (event tap de Quartz o un helper), ventana frontal (`NSWorkspace` / `NSRunningApplication.activate`), pegado (`CGEvent` Cmd+V) y clipboard (`NSPasteboard`). Requiere permisos de Accesibilidad y Micrófono para la app.
