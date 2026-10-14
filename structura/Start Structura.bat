@echo off
cd /d "%~dp0"
where python >nul 2>nul
if %errorlevel%==0 (
  python server.py
) else (
  py server.py
)
echo.
echo Structura stopped. If you saw an error above, make sure Python 3 is installed from python.org.
pause
