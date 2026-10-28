# OSCSee-LV1

Puente entre OSC See (OSC por UDP) y la Waves eMotion LV1 (OSC por TCP, protocolo MyFOH).
Se abre, encuentra sola al LV1 en la red y queda escuchando a OSC See.

## Salida en OSC See

| Campo | Valor |
|---|---|
| Protocolo | OSC por UDP |
| IP | `127.0.0.1` (si OSCSee-LV1 corre en la misma computadora; si no, la IP que muestra la app) |
| Puerto | `9000` (exclusivo de OSCSee-LV1; nada más debe usarlo en esa computadora) |

## Mensajes

| Mensaje OSC | Qué hace |
|---|---|
| `/lv1/intro` | Recalla la escena "Intro" |
| `/lv1/cancion_a` | Recalla "Canción A" (el espacio se escribe `_` o `-`) |
| `/lv1/next` · `/lv1/prev` | Escena siguiente / anterior |
| `/lv1/3` | Escena 3 de la lista (la primera es 1) |

- No importan mayúsculas, tildes, `_`, `-` ni puntuación: `/lv1/INTRO`, `/lv1/Intro` e `/lv1/intro` son lo mismo.
- El LV1 nunca recibe el nombre: OSCSee-LV1 lo busca en la lista de escenas que le mandó la consola y le envía el número de esa escena.
- Si el mensaje trae un argumento `0` (o falso), se ignora: así un botón que manda 1 al apretar y 0 al soltar dispara una sola vez.
- Si un nombre no existe, no se toca la consola y queda anotado en el registro.
- También acepta `/lv1 "Intro"` (nombre como argumento), por si alguna app no permite espacios o tildes en la dirección.
- Evita escenas llamadas "next", "prev" o con solo un número: chocan con los comandos.

## Conexión con el LV1

- **IP del LV1** en la ventana principal: vacío = automático. Escribe una IP y "Conectar" para fijarla (se aplica al instante).
- El puerto del LV1 cambia solo (al arrancar y al cambiar la configuración de canales); se detecta por el anuncio multicast `225.1.1.1:13337`. Fijarlo a mano en Ajustes es solo un último recurso.
- Registro: `~/oscsee-lv1.log` (Mac: `/Users/<usuario>/oscsee-lv1.log`).

## Generar la app

- **Mac:** instala Python desde python.org y doble clic en `build_mac.command` → `dist/OSCSee-LV1.app`. Desactiva App Nap y pide el permiso de Red local. Primera vez: clic derecho → Abrir.
- **Windows:** doble clic en `build_windows.bat` → `dist\OSCSee-LV1.exe`.

La app resultante no necesita nada instalado en la computadora donde se usa. PyInstaller no compila entre sistemas: la .app se arma en Mac y el .exe en Windows.

## Importante

Protocolo no oficial de Waves, tomado del módulo comunitario de Bitfocus Companion (`companion-module-waves-lv1`, MIT, Miguel Lourenço). Una actualización del LV1 podría romperlo: anota la versión con la que lo probaste y no actualices en medio de una gira.
