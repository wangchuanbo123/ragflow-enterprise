@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ================================================================
echo   RAGFlow one-click start
echo   Team server: PostgreSQL + pgvector + Redis
echo   Docker must be running. The first image build takes time.
echo ================================================================
echo.

if exist "venv\Scripts\python.exe" (
    venv\Scripts\python.exe -m scripts.start_server
) else (
    py -3.11 --version >nul 2>nul
    if errorlevel 1 (
        python --version >nul 2>nul
        if errorlevel 1 (
            echo [FAILED] Python 3.11 was not found.
            echo Please install Python 3.11 and double-click this file again.
            echo Download: https://www.python.org/downloads/releases/python-3119/
            pause
            exit /b 1
        )
        python -m scripts.start_server
    ) else (
        py -3.11 -m scripts.start_server
    )
)

if %errorlevel% neq 0 (
    echo.
    echo [FAILED] Please check the messages above and try again.
    pause
)
