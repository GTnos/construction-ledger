@echo off
chcp 65001 >nul
cd /d "%~dp0ledger"

where python >nul 2>nul
if %errorlevel%==0 (
    set PYCMD=python
) else (
    where python3 >nul 2>nul
    if %errorlevel%==0 (
        set PYCMD=python3
    ) else (
        echo.
        echo 找不到 Python，請先安裝 Python 3。
        echo 到 https://www.python.org/downloads/ 下載安裝，
        echo 安裝時記得勾選「Add python.exe to PATH」，裝完後再雙擊本檔案一次。
        echo.
        pause
        exit /b 1
    )
)

echo ============================================
echo  工程收支帳務系統伺服器啟動中...
echo.
echo  這台電腦上瀏覽器打開： http://localhost:8123
echo  手機（同一個 Wi-Fi）打開： http://電腦的區網IP:8123
echo    （區網IP查法：開始選單搜尋 cmd，輸入 ipconfig，看 IPv4 位址）
echo.
echo  這個視窗請不要關閉，關閉視窗伺服器就會停止。
echo  要停止伺服器：直接關閉本視窗，或按 Ctrl+C。
echo ============================================
echo.
%PYCMD% server.py 8123
echo.
echo 伺服器已停止。
pause
