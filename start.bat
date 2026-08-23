@echo off
REM One-click launch: opens the API and the web app in their own windows, then
REM the browser. Close either window to stop that server.
start "AI Business Agent - API" cmd /k ""%~dp0start-backend.bat""
start "AI Business Agent - Web" cmd /k ""%~dp0start-frontend.bat""
REM Give Vite a moment to bind 5183 before the browser asks for it.
timeout /t 8 /nobreak >nul
start "" http://localhost:5183
