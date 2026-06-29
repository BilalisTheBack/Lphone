@echo off
REM LPhone Agent — Windows Build Scripti
REM Kullanim: agent\build_windows.bat
REM Cikti: agent\dist\lphone.exe (tek binary, Python gerektirmez)
REM ----------------------------------------------------------

echo.
echo  LPhone Agent - Windows Binary Build
echo  =====================================
echo.

cd /d "%~dp0"

REM Python kontrolu
python --version >nul 2>&1
if errorlevel 1 (
    echo [HATA] Python bulunamadi. https://python.org adresinden Python 3.11 yukleyin.
    echo        Kurulum sirasinda "Add to PATH" secenegini isaretleyin.
    pause
    exit /b 1
)

for /f "tokens=*" %%i in ('python --version') do echo [OK] %%i

REM Sanal ortam
if not exist ".venv\" (
    echo [->] Sanal ortam olusturuluyor...
    python -m venv .venv
)
call .venv\Scripts\activate.bat

REM Bagimliliklar
echo [->] Bagimliliklar yukleniyor...
pip install --quiet --upgrade pip
pip install --quiet -r requirements.txt
pip install --quiet pyinstaller

REM Temizlik
if exist "build\" rmdir /s /q build
if exist "dist\"  rmdir /s /q dist

REM Derleme
echo [->] PyInstaller calistiriliyor...
pyinstaller lphone.spec

REM Sonuc
if exist "dist\lphone.exe" (
    echo.
    echo  [OK] Binary olusturuldu: dist\lphone.exe
    echo.
    echo  Calistirmak icin:
    echo    dist\lphone.exe
    echo  Tarayicidan: http://localhost:8000
) else (
    echo  [HATA] Binary olusturulamadi. Yukaridaki loglara bakin.
    exit /b 1
)

pause
