@echo off
REM Starts the FastAPI backend on http://127.0.0.1:8010
REM Uvicorn must run from backend\ so that "app.main" resolves; settings come
REM from the repo-root .env via app.config, not from uvicorn's own flags.
cd /d "%~dp0backend"
"%~dp0.venv\Scripts\python.exe" -m uvicorn app.main:app --reload --port 8010
pause
