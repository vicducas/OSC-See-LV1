@echo off
REM Genera dist\OSCSee-LV1.exe: un solo archivo con todo incluido (Python, interfaz, librerias).
REM Solo quien lo FABRICA necesita Python. Quien lo USA no necesita instalar nada.
cd /d "%~dp0"

set PY=
where py >nul 2>nul && set PY=py -3
if not defined PY (
  where python >nul 2>nul && set PY=python
)
if not defined PY (
  echo.
  echo No encontre Python. Instalalo desde https://www.python.org/downloads/
  echo y marca la casilla "Add python.exe to PATH". Luego vuelve a abrir este archivo.
  echo.
  pause
  exit /b 1
)

%PY% -m pip install --upgrade pyinstaller || goto :error
%PY% -m PyInstaller --noconfirm --clean --onefile --windowed --name "OSCSee-LV1" lv1_bridge.py || goto :error

echo.
echo ============================================
echo  Listo:  dist\OSCSee-LV1.exe
echo  Ese archivo es el que compartes. Lo demas no hace falta.
echo ============================================
explorer dist
pause
exit /b 0

:error
echo.
echo Algo fallo. Copia el texto de arriba y mandamelo.
pause
exit /b 1
