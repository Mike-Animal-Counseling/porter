@echo off
setlocal
chcp 65001 >nul
set "PORTER_PYTHON="
if exist "%~dp0.review-venv\Scripts\python.exe" set "PORTER_PYTHON=%~dp0.review-venv\Scripts\python.exe"
if not defined PORTER_PYTHON for /f "delims=" %%P in ('where python.exe 2^>nul ^| findstr /v /i "WindowsApps"') do if not defined PORTER_PYTHON set "PORTER_PYTHON=%%P"
if not defined PORTER_PYTHON (
  echo Python was not found. Use an existing Python 3.12+ installation to run scripts/open_panel.py.
  echo No software was installed and no browser was opened.
  pause
  exit /b 1
)
"%PORTER_PYTHON%" -X utf8 -B "%~dp0scripts\open_panel.py" --no-open %*
set "PORTER_RESULT=%ERRORLEVEL%"
echo.
pause
exit /b %PORTER_RESULT%
