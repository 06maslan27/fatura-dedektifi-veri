@echo off
REM ---------------------------------------------------------------------------
REM  EPIAS baglanti testi -- cift tikla, calistir.
REM
REM  Kullanici adini ve parolani Python'un kendisi sorar (parola ekranda gorunmez).
REM  HICBIR DOSYA YAZMAZ, hicbir yere veri gondermez. Sadece "veri geliyor mu,
REM  alan adlari tutuyor mu" diye bakar.
REM ---------------------------------------------------------------------------
chcp 65001 >nul
cd /d "%~dp0"

REM Python'u bul: once "python", olmazsa Windows'un "py" baslaticisi.
set "PY="
where python >nul 2>&1 && set "PY=python"
if not defined PY ( where py >nul 2>&1 && set "PY=py" )

if not defined PY (
  echo.
  echo   Python bulunamadi.
  echo   python.org/downloads adresinden kurup "Add python.exe to PATH"
  echo   kutusunu isaretlemen yeterli. Sonra bu dosyayi tekrar cift tikla.
  echo.
  pause
  exit /b 1
)

echo.
%PY% epias_mirror.py --dry-run
echo.
pause
