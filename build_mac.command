#!/bin/bash
# Genera "dist/OSCSee-LV1.app" para Mac (Apple Silicon o Intel, según la Mac donde lo corras).
# Requiere Python 3.10+ de python.org (trae tkinter). Quien USA la .app no necesita nada.
set -e
cd "$(dirname "$0")"

python3 -m pip install --upgrade pyinstaller
python3 -m PyInstaller --noconfirm --clean --windowed \
  --name "OSCSee-LV1" --osx-bundle-identifier com.oscsee.lv1 lv1_bridge.py

APP="dist/OSCSee-LV1.app"
PLIST="$APP/Contents/Info.plist"
PB=/usr/libexec/PlistBuddy

# Que macOS no "duerma" la app en segundo plano (App Nap): si se atrasan los /pong,
# el LV1 corta la conexión a los ~5 s.
$PB -c "Add :NSAppSleepDisabled bool true" "$PLIST" 2>/dev/null || $PB -c "Set :NSAppSleepDisabled true" "$PLIST"
# Texto del aviso de "Red local" (macOS 15+). Sin ese permiso no se encuentra el LV1.
$PB -c "Add :NSLocalNetworkUsageDescription string 'OSCSee-LV1 necesita la red local para encontrar y controlar la consola Waves LV1.'" "$PLIST" 2>/dev/null || true

# Volver a firmar (ad hoc) después de tocar el Info.plist; en Apple Silicon es obligatorio.
codesign --force --deep --sign - "$APP"

echo
echo "Listo: $APP"
echo "Primera vez: clic derecho → Abrir (no está firmada con Apple), y aceptar el permiso de Red local."
if [ -z "$CI" ]; then open dist; fi
