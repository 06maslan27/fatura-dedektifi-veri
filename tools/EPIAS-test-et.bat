@echo off
REM ---------------------------------------------------------------------------
REM  EPIAS baglanti testi -- cift tikla, calistir.
REM
REM  Kullanici adini ve parolani sorar, EPIAS'a baglanir ve "veri geliyor mu,
REM  alan adlari tutuyor mu" diye bakar. HICBIR DOSYA YAZMAZ, hicbir yere kayit
REM  gondermez. Parola ekranda gorunmez ve hicbir yere kaydedilmez.
REM ---------------------------------------------------------------------------
chcp 65001 >nul
cd /d "%~dp0"
echo.
echo   EPIAS Seffaflik Platformu baglanti testi
echo   ----------------------------------------
echo.
set /p EPIAS_USERNAME="  Kullanici adi (e-posta): "

REM Parolayi ekranda gostermeden oku.
for /f "delims=" %%p in ('powershell -NoProfile -Command ^
  "$s=Read-Host -AsSecureString '  Parola'; ^
   [Runtime.InteropServices.Marshal]::PtrToStringAuto( ^
   [Runtime.InteropServices.Marshal]::SecureStringToBSTR($s))"') do set "EPIAS_PASSWORD=%%p"

echo.
echo   Baglaniliyor...
echo.
python epias_mirror.py --dry-run
set "EPIAS_PASSWORD="
echo.
pause
