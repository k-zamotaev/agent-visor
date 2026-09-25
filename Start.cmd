@echo off
cd /d "%~dp0"
where py >nul 2>nul
if errorlevel 1 goto python
py -3 launch.py %*
goto done
:python
python launch.py %*
:done
if errorlevel 1 pause
