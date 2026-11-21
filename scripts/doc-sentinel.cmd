@echo off
powershell -NoProfile -ExecutionPolicy Bypass -File "%~dp0doc-sentinel.ps1" %*
exit /b %errorlevel%
