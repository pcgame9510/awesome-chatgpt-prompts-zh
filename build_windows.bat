@echo off
setlocal
py -m pip install --upgrade pyinstaller
if errorlevel 1 goto :error
py -m PyInstaller --noconfirm --clean --onefile --windowed --name WebsiteKeywordScanner run_scanner.pyw
if errorlevel 1 goto :error
echo.
echo Build complete: dist\WebsiteKeywordScanner.exe
pause
exit /b 0
:error
echo Build failed. Check Python and network access, then try again.
pause
exit /b 1
