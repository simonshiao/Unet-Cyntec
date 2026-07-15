@echo off
chcp 65001 >nul
setlocal
cd /d "%~dp0"

echo ================================================
echo  Codex Workspace v0.5.1 - JSON 引擎版
echo ================================================
echo.

where python >nul 2>nul
if errorlevel 1 (
  echo [錯誤] 找不到 Python。
  echo 請先安裝 Python 3.10 以上版本，並勾選 Add Python to PATH。
  pause
  exit /b 1
)

python -c "import PySide6" >nul 2>nul
if errorlevel 1 (
  echo [安裝] 正在安裝 PySide6...
  python -m pip install --upgrade PySide6
  if errorlevel 1 goto :install_error
)

python -c "import winpty" >nul 2>nul
if errorlevel 1 (
  echo [安裝] 正在安裝 pywinpty（進階 PowerShell 終端使用）...
  python -m pip install --upgrade pywinpty
  if errorlevel 1 echo [提醒] pywinpty 安裝失敗，JSON 聊天引擎仍可使用。
)

echo [啟動] Codex Workspace...
python "Codex_Workspace_v0.5.1_JSON引擎_穩定終端版.py"
set ERR=%ERRORLEVEL%
if not "%ERR%"=="0" (
  echo.
  echo 程式已結束，錯誤碼：%ERR%
  pause
)
exit /b %ERR%

:install_error
echo.
echo [錯誤] 套件安裝失敗。
echo 請確認網路連線後執行：
echo python -m pip install PySide6 pywinpty
pause
exit /b 1
