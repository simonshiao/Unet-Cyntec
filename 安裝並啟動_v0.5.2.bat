@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo [Codex Workspace v0.5.2] 正在檢查套件...
python -c "import PySide6" >nul 2>nul || python -m pip install --upgrade PySide6
python -c "import winpty" >nul 2>nul || python -m pip install --upgrade pywinpty
echo 正在啟動...
python "Codex_Workspace_v0.5.2_JSON引擎_終端自動啟動版.py"
if errorlevel 1 pause
