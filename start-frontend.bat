@echo off
REM Starts the Vite dev server on http://localhost:5183
REM The port is pinned in vite.config.ts with strictPort, so a clash fails
REM loudly instead of drifting to another port the API's CORS list rejects.
cd /d "%~dp0frontend"
call npm run dev
pause
