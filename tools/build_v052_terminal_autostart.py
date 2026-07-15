# -*- coding: utf-8 -*-
from __future__ import annotations

from pathlib import Path
import py_compile
import shutil
import zipfile

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "Codex_Workspace_v0.5.1_JSON引擎_穩定終端版.py"
OUT = ROOT / "Codex_Workspace_v0.5.2_JSON引擎_終端自動啟動版.py"
BAT = ROOT / "安裝並啟動_v0.5.2.bat"
README = ROOT / "Codex_Workspace_v0.5.2_使用說明.txt"
RELEASE = ROOT / "release"
RELEASE.mkdir(exist_ok=True)

text = SRC.read_text(encoding="utf-8")

replacements = [
    (
        'Codex Workspace v0.5.1｜JSON 引擎＋穩定終端版',
        'Codex Workspace v0.5.2｜JSON 引擎＋終端自動啟動版',
    ),
    ('應用程式版本 = "0.5.1"', '應用程式版本 = "0.5.2"'),
    (
        'JSON 引擎 · v<span id="version">0.5.1</span>',
        'JSON 引擎 · v<span id="version">0.5.2</span>',
    ),
    (
        'function toggleTerminal(v=null){const e=$(\'terminalSheet\'),n=v===null?!e.classList.contains(\'show\'):v;e.classList.toggle(\'show\',n);if(n)setTimeout(fitTerminal,80)}function sendShell(){const v=$(\'shellInput\').value;if(!v)return;bridge.sendShellLine(v);$(\'shellInput\').value=\'\'}',
        'function toggleTerminal(v=null){const e=$(\'terminalSheet\'),n=v===null?!e.classList.contains(\'show\'):v;e.classList.toggle(\'show\',n);if(n)setTimeout(()=>{fitTerminal();if(!shellRunning&&bridge){term.reset();term.feed(\'正在啟動 PowerShell…\\r\\n\');bridge.startShell()}else{$(\'terminalIme\').focus()}},80)}function sendShell(){const v=$(\'shellInput\').value;if(!v)return;bridge.sendShellLine(v);$(\'shellInput\').value=\'\'}',
    ),
    (
        "$('btnShellStart').onclick=()=>shellRunning?bridge.stopShell():bridge.startShell();",
        "$('btnShellStart').onclick=()=>{if(shellRunning){bridge.stopShell()}else{term.reset();term.feed('正在啟動 PowerShell…\\r\\n');bridge.startShell()}};",
    ),
    (
        '            self.shellRunningChanged.emit(True)\n            啟動指令 = (',
        '            self.shellRunningChanged.emit(True)\n            self.shellOutputReceived.emit("\\r\\n正在啟動 PowerShell...\\r\\n")\n            啟動指令 = (',
    ),
    (
        '                "$Host.UI.RawUI.WindowTitle=\'Codex Workspace Terminal\';Clear-Host"\n',
        '                "$Host.UI.RawUI.WindowTitle=\'Codex Workspace Terminal\';"\n'
        '                "Write-Host \'\';"\n'
        '                "Write-Host \'Codex Workspace PowerShell 已啟動\' -ForegroundColor Cyan;"\n'
        '                "Write-Host (\'工作目錄：\' + (Get-Location).Path) -ForegroundColor DarkGray"\n',
    ),
]

for old, new in replacements:
    if old not in text:
        raise RuntimeError(f"找不到要替換的片段：{old[:120]}")
    text = text.replace(old, new, 1)

# 在終端區標題補充自動啟動提示。
text = text.replace(
    'powershell.exe — stable ConPTY terminal',
    'powershell.exe — stable ConPTY terminal · 開啟即自動啟動',
    1,
)

OUT.write_text(text, encoding="utf-8", newline="\n")
py_compile.compile(str(OUT), doraise=True)

BAT.write_text(
    '@echo off\r\n'
    'chcp 65001 >nul\r\n'
    'cd /d "%~dp0"\r\n'
    'echo [Codex Workspace v0.5.2] 正在檢查套件...\r\n'
    'python -c "import PySide6" >nul 2>nul || python -m pip install --upgrade PySide6\r\n'
    'python -c "import winpty" >nul 2>nul || python -m pip install --upgrade pywinpty\r\n'
    'echo 正在啟動...\r\n'
    'python "Codex_Workspace_v0.5.2_JSON引擎_終端自動啟動版.py"\r\n'
    'if errorlevel 1 pause\r\n',
    encoding="utf-8",
    newline="",
)

README.write_text(
    'Codex Workspace v0.5.2｜終端自動啟動修正版\n'
    '===========================================\n\n'
    '修正內容：\n'
    '1. 點右上角「>_」開啟終端時，會自動啟動 PowerShell，不再顯示空白黑畫面。\n'
    '2. 終端啟動期間顯示「正在啟動 PowerShell…」。\n'
    '3. PowerShell 啟動後顯示成功訊息與目前工作目錄。\n'
    '4. 移除初始化時的 Clear-Host，避免提示字與 PowerShell 提示符被清掉。\n'
    '5. 保留 v0.2.3 穩定終端模型與 JSON 聊天引擎。\n\n'
    '使用方式：解壓縮後雙擊「安裝並啟動_v0.5.2.bat」。\n',
    encoding="utf-8",
)

zip_path = RELEASE / "Codex_Workspace_v0.5.2_JSON引擎_終端自動啟動版.zip"
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
    z.write(OUT, OUT.name)
    z.write(BAT, BAT.name)
    z.write(README, README.name)

# ASCII 別名，方便直接下載。
shutil.copy2(OUT, RELEASE / "Codex_Workspace_v0.5.2_Terminal_AutoStart.py")
shutil.copy2(zip_path, RELEASE / "Codex_Workspace_v0.5.2_Terminal_AutoStart.zip")

print(OUT)
print(zip_path)
