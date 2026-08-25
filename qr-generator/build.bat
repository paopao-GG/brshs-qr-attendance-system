@echo off
REM Builds the package that gets handed to school staff:
REM
REM     dist\TRACKIFY QR Generator\
REM         qr-generator.exe      <- everything inside; no Python needed
REM         READ ME FIRST.txt
REM
REM The QR secret is COMPILED INTO the exe by bake.py, so staff never see or type it.
REM Two consequences worth knowing: the folder is effectively a key, since anyone with
REM the exe can mint a valid code for any LRN; and rotating the secret means rebuilding
REM and redistributing. _baked.py is deleted below so the secret never lingers in the
REM source tree, and it is gitignored in case a build is interrupted.
REM
REM No roster ships in the package -- staff browse for whichever sheet the adviser
REM sent them, which is what keeps them from working off a stale bundled copy.

setlocal
cd /d "%~dp0"

echo.
echo [1/4] Baking the QR secret...
python bake.py
if errorlevel 1 (
  echo.
  echo BUILD ABORTED: no secret to bake.
  exit /b 1
)

echo.
echo [2/4] Building the exe...
REM --paths .. finds trackify.core.qrcodes. Both __init__.py files in that package are
REM empty and qrcodes.py imports only hmac/re/hashlib, so this pulls in one small
REM module rather than the whole kiosk.
REM
REM The excludes matter: without them PyInstaller notices the Qt/numpy/sklearn stack in
REM the same virtualenv and the exe balloons past 300 MB.
pyinstaller --noconfirm --onefile --windowed ^
  --name qr-generator ^
  --paths .. ^
  --hidden-import trackify.core.qrcodes ^
  --hidden-import _baked ^
  --exclude-module PySide6 ^
  --exclude-module PyQt6 ^
  --exclude-module qtpy ^
  --exclude-module numpy ^
  --exclude-module scipy ^
  --exclude-module sklearn ^
  --exclude-module statsmodels ^
  --exclude-module pandas ^
  --exclude-module matplotlib ^
  --exclude-module zxingcpp ^
  --exclude-module serial ^
  main.py
set BUILD_ERROR=%errorlevel%

echo.
echo [3/4] Removing the baked secret from the source tree...
if exist _baked.py del /q _baked.py
if exist __pycache__\_baked.*.pyc del /q __pycache__\_baked.*.pyc

if not "%BUILD_ERROR%"=="0" (
  echo.
  echo BUILD FAILED.
  exit /b 1
)

echo.
echo [4/4] Assembling the package...
set PKG=dist\TRACKIFY QR Generator
if exist "%PKG%" rmdir /s /q "%PKG%"
mkdir "%PKG%"
copy /y dist\qr-generator.exe "%PKG%\" >nul
copy /y "READ ME FIRST.txt" "%PKG%\" >nul

echo.
echo Done. Give this whole folder to the school:
echo   %~dp0%PKG%
echo.
echo Zip it before sending. It contains the signing secret, so send it the way you
echo would send a password -- not as a public link.
endlocal
