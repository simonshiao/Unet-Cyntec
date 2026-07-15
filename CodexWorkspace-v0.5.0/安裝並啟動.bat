@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ==============================================
echo   Codex Workspace v0.5.0 - JSON 引擎版
echo ==============================================
echo.

where py >nul 2>nul
if %errorlevel%==0 (
    set "PY=py"
) else (
    where python >nul 2>nul
    if not %errorlevel%==0 (
        echo [錯誤] 找不到 Python。
        echo 請先安裝 Python 3.10 以上版本，並勾選 Add Python to PATH。
        pause
        exit /b 1
    )
    set "PY=python"
)

echo [1/2] 檢查 Python 套件...
%PY% -c "import PySide6" >nul 2>nul
if not %errorlevel%==0 (
    echo 正在安裝 PySide6，第一次可能需要幾分鐘...
    %PY% -m pip install --upgrade PySide6
    if not %errorlevel%==0 goto :install_error
)

%PY% -c "import winpty" >nul 2>nul
if not %errorlevel%==0 (
    echo 正在安裝 pywinpty（進階 PowerShell 終端使用）...
    %PY% -m pip install --upgrade pywinpty
    if not %errorlevel%==0 goto :install_error
)

echo [2/2] 啟動 Codex Workspace...
%PY% "%~dp0Codex_Workspace_v0.5.0_JSON引擎版.py"
if not %errorlevel%==0 (
    echo.
    echo [錯誤] 程式異常結束，錯誤碼：%errorlevel%
    pause
)
exit /b

:install_error
echo.
echo [錯誤] 套件安裝失敗。
echo 請檢查網路，或手動執行：
echo   %PY% -m pip install PySide6 pywinpty
pause
exit /b 1
