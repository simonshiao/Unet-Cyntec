# -*- coding: utf-8 -*-
"""
Codex Workspace v0.5.0｜JSON 引擎版
====================================

核心改版：
- 主聊天不再解析 Codex 互動式 TUI 畫面。
- 每次工作使用 `codex exec --json`，直接讀取 JSONL 事件。
- 一個使用者任務只建立一個 Codex 回覆，不再產生空白訊息。
- 支援 SQLite 對話履歷、檔案上傳、Ctrl+V 貼圖、圖片／影片／音訊成果預覽。
- 保留獨立 PowerShell ConPTY 終端，僅供除錯與手動操作，不參與聊天解析。

執行環境：Windows 10/11、Python 3.10+（建議 3.12）
依賴：PySide6、pywinpty
"""

from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import shlex
import shutil
import sqlite3
import subprocess
import sys
import threading
import time
import traceback
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QObject, QStandardPaths, QTimer, QUrl, Signal, Slot
from PySide6.QtWebChannel import QWebChannel
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWidgets import QApplication, QFileDialog, QMessageBox

try:
    from PySide6.QtWebEngineCore import QWebEngineSettings
except Exception:
    QWebEngineSettings = None  # type: ignore[assignment]

try:
    from winpty import PtyProcess
except ImportError:
    PtyProcess = None  # type: ignore[assignment]


應用程式名稱 = "Codex Workspace"
應用程式版本 = "0.5.0"


@dataclass
class 應用設定:
    專案路徑: str = ""
    Codex命令: str = "codex"
    模型: str = ""
    沙盒模式: str = "workspace-write"
    批准模式: str = "never"
    跳過Git檢查: bool = True
    自動啟動: bool = False
    自動偵測成果: bool = True
    送出後清空附件: bool = True
    帶入最近對話: bool = True
    對話內容上限: int = 14000
    額外參數: str = ""
    PowerShell路徑: str = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    終端列數: int = 44
    終端欄數: int = 150


@dataclass
class 附件項目:
    id: str
    name: str
    path: str
    mime: str
    size: int
    isImage: bool
    preview: str = ""


# -----------------------------------------------------------------------------
# 路徑與設定
# -----------------------------------------------------------------------------
def 取得設定資料夾() -> Path:
    路徑 = Path(QStandardPaths.writableLocation(QStandardPaths.AppConfigLocation))
    路徑.mkdir(parents=True, exist_ok=True)
    return 路徑


def 取得資料資料夾() -> Path:
    路徑 = Path(QStandardPaths.writableLocation(QStandardPaths.AppDataLocation))
    路徑.mkdir(parents=True, exist_ok=True)
    return 路徑


def 取得設定檔路徑() -> Path:
    return 取得設定資料夾() / "settings.json"


def 取得資料庫路徑() -> Path:
    return 取得資料資料夾() / "workspace_history.sqlite3"


def 取得附件資料夾() -> Path:
    路徑 = 取得資料資料夾() / "attachments"
    路徑.mkdir(parents=True, exist_ok=True)
    return 路徑


def 取得執行紀錄資料夾() -> Path:
    路徑 = 取得資料資料夾() / "runs"
    路徑.mkdir(parents=True, exist_ok=True)
    return 路徑


def 載入設定() -> 應用設定:
    路徑 = 取得設定檔路徑()
    if not 路徑.exists():
        return 應用設定()
    try:
        資料 = json.loads(路徑.read_text(encoding="utf-8"))
        預設 = asdict(應用設定())
        # 相容 v0.4.x 舊欄位。
        對應 = {
            "啟動時自動執行Codex": "自動啟動",
            "自動偵測成果": "自動偵測成果",
            "送出後清空附件": "送出後清空附件",
            "專案路徑": "專案路徑",
            "Codex命令": "Codex命令",
            "PowerShell路徑": "PowerShell路徑",
        }
        for 舊鍵, 新鍵 in 對應.items():
            if 舊鍵 in 資料 and 新鍵 not in 資料:
                資料[新鍵] = 資料[舊鍵]
        預設.update({鍵: 值 for 鍵, 值 in 資料.items() if 鍵 in 預設})
        return 應用設定(**預設)
    except Exception:
        return 應用設定()


def 儲存設定(設定: 應用設定) -> None:
    取得設定檔路徑().write_text(
        json.dumps(asdict(設定), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def 簡化空白(文字: str) -> str:
    return " ".join(str(文字 or "").strip().split())


def 安全標題(文字: str, 最大長度: int = 38) -> str:
    文字 = 簡化空白(文字)
    if not 文字:
        return "新對話"
    return 文字[:最大長度] + ("…" if len(文字) > 最大長度 else "")


def 檔案網址(路徑: Path | str) -> str:
    return QUrl.fromLocalFile(str(Path(路徑).resolve())).toString()


def 拆解命令(命令: str) -> list[str]:
    命令 = str(命令 or "codex").strip() or "codex"
    try:
        項目 = shlex.split(命令, posix=False)
    except Exception:
        項目 = [命令]
    結果: list[str] = []
    for 項 in 項目:
        項 = 項.strip()
        if len(項) >= 2 and 項[0] == 項[-1] and 項[0] in {'"', "'"}:
            項 = 項[1:-1]
        if 項:
            結果.append(項)
    return 結果 or ["codex"]


def 建立Windows命令(參數: list[str]) -> list[str]:
    """處理 npm 安裝產生的 codex.cmd；其餘可執行檔直接啟動。"""
    if not 參數:
        return 參數
    第一個 = 參數[0]
    找到 = shutil.which(第一個) if not Path(第一個).exists() else 第一個
    if 找到:
        參數 = [找到, *參數[1:]]
    if Path(參數[0]).suffix.lower() in {".cmd", ".bat"}:
        return [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", subprocess.list2cmdline(參數)]
    return 參數


# -----------------------------------------------------------------------------
# SQLite 對話履歷
# -----------------------------------------------------------------------------
class 對話資料庫:
    def __init__(self, 路徑: Path) -> None:
        self.路徑 = 路徑
        self.鎖 = threading.RLock()
        self.連線 = sqlite3.connect(str(路徑), check_same_thread=False)
        self.連線.row_factory = sqlite3.Row
        with self.鎖:
            self.連線.execute("PRAGMA journal_mode=WAL")
            self.連線.execute("PRAGMA synchronous=NORMAL")
            self.連線.executescript(
                """
                CREATE TABLE IF NOT EXISTS sessions(
                    id TEXT PRIMARY KEY,
                    title TEXT NOT NULL,
                    project_path TEXT NOT NULL DEFAULT '',
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS messages(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    role TEXT NOT NULL,
                    kind TEXT NOT NULL DEFAULT 'text',
                    content TEXT NOT NULL DEFAULT '',
                    data_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_messages_session
                    ON messages(session_id, created_at, id);
                CREATE TABLE IF NOT EXISTS events(
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    session_id TEXT NOT NULL,
                    level TEXT NOT NULL DEFAULT 'info',
                    text TEXT NOT NULL,
                    data_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_session
                    ON events(session_id, created_at, id);
                """
            )
            self.連線.commit()
        self.清理舊版雜訊()

    @staticmethod
    def _讀json(文字: str) -> dict[str, Any]:
        try:
            資料 = json.loads(文字 or "{}")
            return 資料 if isinstance(資料, dict) else {}
        except Exception:
            return {}

    def 建立對話(self, 專案路徑: str = "", 標題: str = "新對話") -> str:
        對話id = uuid.uuid4().hex
        現在 = time.time()
        with self.鎖:
            self.連線.execute(
                "INSERT INTO sessions(id,title,project_path,created_at,updated_at) VALUES(?,?,?,?,?)",
                (對話id, 標題, 專案路徑, 現在, 現在),
            )
            self.連線.commit()
        return 對話id

    def 最新對話id(self) -> Optional[str]:
        with self.鎖:
            列 = self.連線.execute(
                "SELECT id FROM sessions ORDER BY updated_at DESC LIMIT 1"
            ).fetchone()
        return str(列["id"]) if 列 else None

    def 對話存在(self, 對話id: str) -> bool:
        with self.鎖:
            return bool(self.連線.execute("SELECT 1 FROM sessions WHERE id=?", (對話id,)).fetchone())

    def 更新對話(self, 對話id: str, 標題: Optional[str] = None, 專案路徑: Optional[str] = None) -> None:
        欄位 = ["updated_at=?"]
        參數: list[Any] = [time.time()]
        if 標題 is not None:
            欄位.append("title=?")
            參數.append(標題)
        if 專案路徑 is not None:
            欄位.append("project_path=?")
            參數.append(專案路徑)
        參數.append(對話id)
        with self.鎖:
            self.連線.execute(f"UPDATE sessions SET {', '.join(欄位)} WHERE id=?", tuple(參數))
            self.連線.commit()

    def 訊息數(self, 對話id: str, 角色: Optional[str] = None) -> int:
        with self.鎖:
            if 角色:
                列 = self.連線.execute(
                    "SELECT COUNT(*) n FROM messages WHERE session_id=? AND role=?",
                    (對話id, 角色),
                ).fetchone()
            else:
                列 = self.連線.execute(
                    "SELECT COUNT(*) n FROM messages WHERE session_id=?",
                    (對話id,),
                ).fetchone()
        return int(列["n"] if 列 else 0)

    def 加入訊息(self, 對話id: str, 角色: str, 內容: str, 種類: str = "text", 資料: Optional[dict[str, Any]] = None) -> int:
        現在 = time.time()
        with self.鎖:
            游標 = self.連線.execute(
                "INSERT INTO messages(session_id,role,kind,content,data_json,created_at) VALUES(?,?,?,?,?,?)",
                (對話id, 角色, 種類, 內容, json.dumps(資料 or {}, ensure_ascii=False), 現在),
            )
            self.連線.execute("UPDATE sessions SET updated_at=? WHERE id=?", (現在, 對話id))
            self.連線.commit()
            return int(游標.lastrowid)

    def 加入事件(self, 對話id: str, 文字: str, 等級: str = "info", 資料: Optional[dict[str, Any]] = None) -> int:
        現在 = time.time()
        with self.鎖:
            游標 = self.連線.execute(
                "INSERT INTO events(session_id,level,text,data_json,created_at) VALUES(?,?,?,?,?)",
                (對話id, 等級, 文字, json.dumps(資料 or {}, ensure_ascii=False), 現在),
            )
            self.連線.commit()
            return int(游標.lastrowid)

    def 列出對話(self, 上限: int = 100) -> list[dict[str, Any]]:
        with self.鎖:
            列表 = self.連線.execute(
                """
                SELECT s.*,
                    (SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) message_count
                FROM sessions s
                ORDER BY s.updated_at DESC
                LIMIT ?
                """,
                (上限,),
            ).fetchall()
        return [dict(列) for 列 in 列表]

    def 讀取對話(self, 對話id: str) -> dict[str, Any]:
        with self.鎖:
            對話 = self.連線.execute("SELECT * FROM sessions WHERE id=?", (對話id,)).fetchone()
            訊息 = self.連線.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY created_at,id", (對話id,)
            ).fetchall()
            事件 = self.連線.execute(
                "SELECT * FROM events WHERE session_id=? ORDER BY created_at,id", (對話id,)
            ).fetchall()
        if not 對話:
            return {}
        訊息結果 = []
        for 列 in 訊息:
            項目 = dict(列)
            項目["data"] = self._讀json(項目.pop("data_json", "{}"))
            訊息結果.append(項目)
        事件結果 = []
        for 列 in 事件:
            項目 = dict(列)
            項目["data"] = self._讀json(項目.pop("data_json", "{}"))
            事件結果.append(項目)
        return {"session": dict(對話), "messages": 訊息結果, "events": 事件結果}

    def 最近對話文字(self, 對話id: str, 最大字數: int) -> str:
        with self.鎖:
            列表 = self.連線.execute(
                "SELECT role,content FROM messages WHERE session_id=? AND kind='text' ORDER BY created_at DESC,id DESC LIMIT 24",
                (對話id,),
            ).fetchall()
        行: list[str] = []
        字數 = 0
        for 列 in reversed(列表):
            內容 = str(列["content"] or "").strip()
            if not 內容:
                continue
            標籤 = "使用者" if 列["role"] == "user" else "Codex"
            一段 = f"{標籤}：{內容}"
            if 字數 + len(一段) > 最大字數:
                一段 = 一段[-max(0, 最大字數 - 字數):]
            if 一段:
                行.append(一段)
                字數 += len(一段)
            if 字數 >= 最大字數:
                break
        return "\n\n".join(行)

    def 刪除對話(self, 對話id: str) -> None:
        with self.鎖:
            self.連線.execute("DELETE FROM messages WHERE session_id=?", (對話id,))
            self.連線.execute("DELETE FROM events WHERE session_id=?", (對話id,))
            self.連線.execute("DELETE FROM sessions WHERE id=?", (對話id,))
            self.連線.commit()

    def 清理舊版雜訊(self) -> None:
        關鍵字 = [
            "Booting MCP", "codex_apps", "WorkingWorking", "ReconnectReconnect",
            "Falling back from WebSockets", "request timed out", "Implement {feature}",
            "usage limit reset available", "github.com/openai/codex/releases/latest",
            "$OutputEncoding", "39;49m",
        ]
        with self.鎖:
            self.連線.execute(
                "DELETE FROM messages WHERE role='assistant' AND kind='text' AND TRIM(COALESCE(content,''))=''"
            )
            列表 = self.連線.execute(
                "SELECT id,content FROM messages WHERE role='assistant' AND kind='text'"
            ).fetchall()
            刪除 = []
            for 列 in 列表:
                內容 = str(列["content"] or "")
                低 = 內容.lower()
                if any(鍵.lower() in 低 for 鍵 in 關鍵字):
                    刪除.append((列["id"],))
            if 刪除:
                self.連線.executemany("DELETE FROM messages WHERE id=?", 刪除)
            self.連線.commit()

    def 關閉(self) -> None:
        with self.鎖:
            self.連線.close()


# -----------------------------------------------------------------------------
# 主控制器：JSON 引擎 + 附件 + 成果 + 獨立終端
# -----------------------------------------------------------------------------
class 工作站控制器(QObject):
    statusChanged = Signal(str, str)
    readyChanged = Signal(bool)
    taskRunningChanged = Signal(bool)
    chatEvent = Signal(str)
    historyChanged = Signal(str)
    sessionLoaded = Signal(str)
    eventAdded = Signal(str)
    attachmentsChanged = Signal(str)
    settingsChanged = Signal(str)
    jsonEventReceived = Signal(str)
    terminalOutputReceived = Signal(str)
    terminalRunningChanged = Signal(bool)

    圖片副檔名 = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
    影片副檔名 = {".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv"}
    音訊副檔名 = {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".opus"}
    其他成果副檔名 = {".zip", ".7z", ".rar", ".pdf", ".html", ".htm", ".csv", ".json", ".js", ".py", ".txt"}
    排除資料夾 = {
        ".git", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__",
        ".pytest_cache", ".mypy_cache", ".idea", ".vscode", "cache", "temp", "tmp",
    }

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.設定 = 載入設定()
        self.資料庫 = 對話資料庫(取得資料庫路徑())
        self.附件資料夾 = 取得附件資料夾()
        self.執行紀錄資料夾 = 取得執行紀錄資料夾()
        self.附件列表: list[附件項目] = []
        最新 = self.資料庫.最新對話id()
        self.目前對話id = 最新 or self.資料庫.建立對話(self.設定.專案路徑)

        self.Codex已就緒 = False
        self.Codex版本 = ""
        self.任務鎖 = threading.RLock()
        self.任務程序: Optional[subprocess.Popen[bytes]] = None
        self.任務執行緒: Optional[threading.Thread] = None
        self.任務停止事件 = threading.Event()
        self.任務進行中 = False
        self.任務id = ""
        self.任務對話id = ""
        self.任務開始時間 = 0.0
        self.任務最後回答 = ""
        self.任務thread_id = ""
        self.任務使用量: dict[str, Any] = {}
        self.任務附件快照: list[dict[str, Any]] = []

        # 獨立 PowerShell ConPTY，只供手動除錯。
        self.終端程序: Optional[object] = None
        self.終端讀取執行緒: Optional[threading.Thread] = None
        self.終端停止事件 = threading.Event()
        self.終端寫入鎖 = threading.Lock()
        self._正在關閉 = False

    @staticmethod
    def _json(資料: Any) -> str:
        return json.dumps(資料, ensure_ascii=False)

    def _廣播歷史(self) -> None:
        self.historyChanged.emit(self._json(self.資料庫.列出對話()))

    def _目前對話資料(self) -> dict[str, Any]:
        return self.資料庫.讀取對話(self.目前對話id)

    def _新增事件(self, 文字: str, 等級: str = "info", 對話id: Optional[str] = None, 資料: Optional[dict[str, Any]] = None) -> None:
        對話id = 對話id or self.目前對話id
        事件id = self.資料庫.加入事件(對話id, 文字, 等級, 資料)
        payload = {
            "id": 事件id,
            "session_id": 對話id,
            "level": 等級,
            "text": 文字,
            "data": 資料 or {},
            "created_at": time.time(),
        }
        self.eventAdded.emit(self._json(payload))

    @Slot(result=str)
    def getInitialState(self) -> str:
        資料 = asdict(self.設定)
        資料.update(
            {
                "version": 應用程式版本,
                "ready": self.Codex已就緒,
                "taskRunning": self.任務進行中,
                "codexVersion": self.Codex版本,
                "pywinptyAvailable": PtyProcess is not None,
                "configPath": str(取得設定檔路徑()),
                "databasePath": str(取得資料庫路徑()),
                "attachmentFolder": str(self.附件資料夾),
                "runLogFolder": str(self.執行紀錄資料夾),
                "histories": self.資料庫.列出對話(),
                "currentSession": self._目前對話資料(),
                "attachments": [asdict(x) for x in self.附件列表],
            }
        )
        return self._json(資料)

    # ---------- 設定與路徑 ----------
    @Slot(result=str)
    def chooseProjectFolder(self) -> str:
        起始 = self.設定.專案路徑 if Path(self.設定.專案路徑).is_dir() else str(Path.home())
        路徑 = QFileDialog.getExistingDirectory(None, "選擇 Codex 專案資料夾", 起始)
        if 路徑:
            self.設定.專案路徑 = 路徑
            儲存設定(self.設定)
            self.資料庫.更新對話(self.目前對話id, 專案路徑=路徑)
            self.settingsChanged.emit(self._json(asdict(self.設定)))
        return 路徑

    @Slot(str)
    def saveSettings(self, json文字: str) -> None:
        try:
            資料 = json.loads(json文字)
            self.設定.專案路徑 = str(資料.get("projectPath", self.設定.專案路徑)).strip()
            self.設定.Codex命令 = str(資料.get("codexCommand", self.設定.Codex命令)).strip() or "codex"
            self.設定.模型 = str(資料.get("model", self.設定.模型)).strip()
            沙盒 = str(資料.get("sandbox", self.設定.沙盒模式)).strip()
            self.設定.沙盒模式 = 沙盒 if 沙盒 in {"read-only", "workspace-write", "danger-full-access"} else "workspace-write"
            批准 = str(資料.get("approval", self.設定.批准模式)).strip()
            self.設定.批准模式 = 批准 if 批准 in {"untrusted", "on-request", "never"} else "never"
            self.設定.跳過Git檢查 = bool(資料.get("skipGit", self.設定.跳過Git檢查))
            self.設定.自動啟動 = bool(資料.get("autoStart", self.設定.自動啟動))
            self.設定.自動偵測成果 = bool(資料.get("autoArtifacts", self.設定.自動偵測成果))
            self.設定.送出後清空附件 = bool(資料.get("clearAttachments", self.設定.送出後清空附件))
            self.設定.帶入最近對話 = bool(資料.get("includeContext", self.設定.帶入最近對話))
            self.設定.對話內容上限 = max(2000, min(50000, int(資料.get("contextLimit", self.設定.對話內容上限))))
            self.設定.額外參數 = str(資料.get("extraArgs", self.設定.額外參數)).strip()
            self.設定.PowerShell路徑 = str(資料.get("powershellPath", self.設定.PowerShell路徑)).strip() or 應用設定().PowerShell路徑
            儲存設定(self.設定)
            self.資料庫.更新對話(self.目前對話id, 專案路徑=self.設定.專案路徑)
            self.settingsChanged.emit(self._json(asdict(self.設定)))
        except Exception as 例外:
            self._新增事件(f"設定儲存失敗：{例外}", "error")

    @Slot(str)
    def openPath(self, 路徑: str) -> None:
        try:
            物件 = Path(路徑)
            if not 物件.exists():
                raise FileNotFoundError(路徑)
            os.startfile(str(物件))  # type: ignore[attr-defined]
        except Exception as 例外:
            self._新增事件(f"無法開啟：{例外}", "error")

    @Slot(str)
    def openContainingFolder(self, 路徑: str) -> None:
        try:
            物件 = Path(路徑)
            資料夾 = 物件 if 物件.is_dir() else 物件.parent
            os.startfile(str(資料夾))  # type: ignore[attr-defined]
        except Exception as 例外:
            self._新增事件(f"無法開啟資料夾：{例外}", "error")

    # ---------- 歷史 ----------
    @Slot()
    def newSession(self) -> None:
        self.目前對話id = self.資料庫.建立對話(self.設定.專案路徑)
        self.sessionLoaded.emit(self._json(self._目前對話資料()))
        self._廣播歷史()

    @Slot(str)
    def loadSession(self, 對話id: str) -> None:
        if self.資料庫.對話存在(對話id):
            self.目前對話id = 對話id
            self.sessionLoaded.emit(self._json(self._目前對話資料()))

    @Slot(str)
    def deleteSession(self, 對話id: str) -> None:
        if self.任務進行中 and 對話id == self.任務對話id:
            self._新增事件("目前工作中的對話不能刪除。", "warning")
            return
        self.資料庫.刪除對話(對話id)
        if 對話id == self.目前對話id:
            最新 = self.資料庫.最新對話id()
            self.目前對話id = 最新 or self.資料庫.建立對話(self.設定.專案路徑)
            self.sessionLoaded.emit(self._json(self._目前對話資料()))
        self._廣播歷史()

    # ---------- 附件 ----------
    def _建立附件(self, 路徑: Path) -> 附件項目:
        mime = mimetypes.guess_type(str(路徑))[0] or "application/octet-stream"
        圖片 = mime.startswith("image/") or 路徑.suffix.lower() in self.圖片副檔名
        return 附件項目(
            id=uuid.uuid4().hex,
            name=路徑.name,
            path=str(路徑.resolve()),
            mime=mime,
            size=路徑.stat().st_size,
            isImage=圖片,
            preview=檔案網址(路徑) if 圖片 else "",
        )

    def _廣播附件(self) -> None:
        self.attachmentsChanged.emit(self._json([asdict(x) for x in self.附件列表]))

    @Slot(result=str)
    def chooseFiles(self) -> str:
        路徑列表, _ = QFileDialog.getOpenFileNames(None, "選擇要交給 Codex 的附件", str(Path.home()), "所有檔案 (*.*)")
        for 路徑文字 in 路徑列表:
            路徑 = Path(路徑文字)
            if 路徑.exists() and 路徑.is_file():
                self.附件列表.append(self._建立附件(路徑))
        self._廣播附件()
        return self._json([asdict(x) for x in self.附件列表])

    @Slot(str, result=str)
    def savePastedImage(self, data_url: str) -> str:
        try:
            比對 = re.match(r"^data:(image/[\w.+-]+);base64,(.+)$", data_url, re.DOTALL)
            if not 比對:
                raise ValueError("不是有效的圖片資料")
            mime = 比對.group(1).lower()
            副檔名 = {"image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}.get(mime, ".png")
            原始 = base64.b64decode(比對.group(2), validate=False)
            if len(原始) > 80 * 1024 * 1024:
                raise ValueError("圖片超過 80 MB")
            子資料夾 = self.附件資料夾 / time.strftime("%Y-%m")
            子資料夾.mkdir(parents=True, exist_ok=True)
            路徑 = 子資料夾 / f"貼上圖片_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}{副檔名}"
            路徑.write_bytes(原始)
            self.附件列表.append(self._建立附件(路徑))
            self._廣播附件()
            return self._json([asdict(x) for x in self.附件列表])
        except Exception as 例外:
            self._新增事件(f"貼上圖片失敗：{例外}", "error")
            return ""

    @Slot(str)
    def removeAttachment(self, 附件id: str) -> None:
        self.附件列表 = [x for x in self.附件列表 if x.id != 附件id]
        self._廣播附件()

    @Slot()
    def clearAttachments(self) -> None:
        self.附件列表.clear()
        self._廣播附件()

    # ---------- Codex 啟動檢查 ----------
    def _Codex基礎參數(self) -> list[str]:
        參數 = 拆解命令(self.設定.Codex命令)
        參數 += ["--ask-for-approval", self.設定.批准模式]
        參數 += ["--sandbox", self.設定.沙盒模式]
        if self.設定.模型:
            參數 += ["--model", self.設定.模型]
        if self.設定.專案路徑:
            參數 += ["--cd", self.設定.專案路徑]
        if self.設定.額外參數:
            參數 += 拆解命令(self.設定.額外參數)
        return 參數

    @Slot()
    def startCodex(self) -> None:
        if self.任務進行中:
            return
        專案 = Path(self.設定.專案路徑.strip())
        if not 專案.is_dir():
            self.Codex已就緒 = False
            self.readyChanged.emit(False)
            self.statusChanged.emit("請先選擇專案", "error")
            self._新增事件("專案資料夾不存在，請先選擇有效路徑。", "error")
            return
        self.statusChanged.emit("正在檢查 Codex…", "working")
        threading.Thread(target=self._檢查Codex工作, name="CodexVersionCheck", daemon=True).start()

    def _檢查Codex工作(self) -> None:
        try:
            原始 = 拆解命令(self.設定.Codex命令) + ["--version"]
            命令 = 建立Windows命令(原始)
            完成 = subprocess.run(
                命令,
                cwd=self.設定.專案路徑 or None,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=15,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            文字 = 完成.stdout.decode("utf-8", errors="replace").strip()
            if 完成.returncode != 0:
                raise RuntimeError(文字 or f"結束碼 {完成.returncode}")
            self.Codex版本 = 文字.splitlines()[-1] if 文字 else "Codex CLI"
            self.Codex已就緒 = True
            self.readyChanged.emit(True)
            self.statusChanged.emit("Codex 已就緒", "ready")
            self._新增事件(f"Codex 已就緒：{self.Codex版本}", "success")
        except Exception as 例外:
            self.Codex已就緒 = False
            self.readyChanged.emit(False)
            self.statusChanged.emit("找不到 Codex", "error")
            self._新增事件(f"Codex 啟動檢查失敗：{例外}", "error")

    @Slot()
    def resetReady(self) -> None:
        if self.任務進行中:
            return
        self.Codex已就緒 = False
        self.readyChanged.emit(False)
        self.statusChanged.emit("尚未啟動", "idle")

    # ---------- 提示詞與 JSON 任務 ----------
    def _組合提示詞(self, 使用者文字: str, 對話內容: str, 附件: list[dict[str, Any]]) -> str:
        區塊: list[str] = []
        if 對話內容:
            區塊 += [
                "以下是這個工作站近期對話，僅供延續上下文；以最後的『目前需求』為最高優先：",
                "<recent_conversation>",
                對話內容,
                "</recent_conversation>",
                "",
            ]
        if 附件:
            區塊.append("請一併讀取並參考以下本機附件：")
            for 索引, 項 in enumerate(附件, 1):
                種類 = "圖片" if 項.get("isImage") else "檔案"
                區塊.append(f"[{種類} {索引}] {項.get('path', '')}")
            區塊.append("")
        區塊 += ["目前需求：", 使用者文字]
        return "\n".join(區塊).strip()

    def _建立執行參數(self, 圖片路徑: list[str]) -> list[str]:
        參數 = self._Codex基礎參數()
        參數 += ["exec", "--json", "--color", "never"]
        if self.設定.跳過Git檢查:
            參數.append("--skip-git-repo-check")
        for 路徑 in 圖片路徑:
            參數 += ["--image", 路徑]
        參數.append("-")
        return 建立Windows命令(參數)

    @Slot(str, result=bool)
    def sendChatMessage(self, json文字: str) -> bool:
        try:
            if not self.Codex已就緒:
                self._新增事件("請先按右上角「啟動」並等待 Codex 就緒。", "warning")
                return False
            with self.任務鎖:
                if self.任務進行中:
                    self._新增事件("上一個任務仍在執行。", "warning")
                    return False
            專案 = Path(self.設定.專案路徑)
            if not 專案.is_dir():
                self._新增事件("專案資料夾不存在。", "error")
                return False
            資料 = json.loads(json文字)
            文字 = str(資料.get("text", "")).strip()
            if not 文字 and not self.附件列表:
                return False
            顯示文字 = 文字 or "請分析這些附件。"
            對話id = self.目前對話id
            對話內容 = ""
            if self.設定.帶入最近對話:
                對話內容 = self.資料庫.最近對話文字(對話id, self.設定.對話內容上限)
            附件快照 = [asdict(x) for x in self.附件列表]
            最終提示 = self._組合提示詞(顯示文字, 對話內容, 附件快照)
            圖片路徑 = [str(x["path"]) for x in 附件快照 if x.get("isImage")]

            if self.資料庫.訊息數(對話id, "user") == 0:
                self.資料庫.更新對話(對話id, 標題=安全標題(顯示文字), 專案路徑=str(專案))
            使用者訊息id = self.資料庫.加入訊息(
                對話id, "user", 顯示文字, "text", {"attachments": 附件快照}
            )
            with self.任務鎖:
                self.任務進行中 = True
                self.任務id = uuid.uuid4().hex
                self.任務對話id = 對話id
                self.任務開始時間 = time.time()
                self.任務最後回答 = ""
                self.任務thread_id = ""
                self.任務使用量 = {}
                self.任務附件快照 = 附件快照
                self.任務停止事件.clear()
                任務id = self.任務id

            self.chatEvent.emit(self._json({
                "type": "task_start", "sessionId": 對話id, "taskId": 任務id,
                "userMessageId": 使用者訊息id, "text": 顯示文字,
                "attachments": 附件快照, "createdAt": time.time(),
            }))
            self.taskRunningChanged.emit(True)
            self.statusChanged.emit("Codex 工作中…", "working")
            self._新增事件("已送出新任務。", "task", 對話id)
            self._廣播歷史()

            命令 = self._建立執行參數(圖片路徑)
            self.任務執行緒 = threading.Thread(
                target=self._執行Codex任務,
                args=(命令, 最終提示, 任務id, 對話id),
                name="CodexJsonTask",
                daemon=True,
            )
            self.任務執行緒.start()
            if self.設定.送出後清空附件:
                QTimer.singleShot(250, self.clearAttachments)
            return True
        except Exception as 例外:
            self._新增事件(f"送出任務失敗：{例外}", "error")
            return False

    def _執行Codex任務(self, 命令: list[str], 提示詞: str, 任務id: str, 對話id: str) -> None:
        紀錄路徑 = self.執行紀錄資料夾 / f"{time.strftime('%Y%m%d_%H%M%S')}_{任務id[:8]}.jsonl"
        stderr文字: list[str] = []
        完成類型 = ""
        try:
            環境 = os.environ.copy()
            環境["PYTHONIOENCODING"] = "utf-8"
            環境["NO_COLOR"] = "1"
            self._新增事件("正在啟動 codex exec --json…", "progress", 對話id)
            程序 = subprocess.Popen(
                命令,
                cwd=self.設定.專案路徑,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=環境,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
            with self.任務鎖:
                self.任務程序 = 程序
            if 程序.stdin is None or 程序.stdout is None:
                raise RuntimeError("無法建立 Codex 輸入輸出管線")
            程序.stdin.write(提示詞.encode("utf-8"))
            程序.stdin.close()

            def 讀stderr() -> None:
                if 程序.stderr is None:
                    return
                for 原行 in iter(程序.stderr.readline, b""):
                    文字 = 原行.decode("utf-8", errors="replace").rstrip()
                    if 文字:
                        stderr文字.append(文字)
                        self.jsonEventReceived.emit(self._json({"stream": "stderr", "text": 文字, "createdAt": time.time()}))

            threading.Thread(target=讀stderr, name="CodexStderr", daemon=True).start()
            with 紀錄路徑.open("w", encoding="utf-8") as 紀錄:
                for 原行 in iter(程序.stdout.readline, b""):
                    if self.任務停止事件.is_set():
                        break
                    文字行 = 原行.decode("utf-8", errors="replace").strip()
                    if not 文字行:
                        continue
                    紀錄.write(文字行 + "\n")
                    紀錄.flush()
                    try:
                        事件 = json.loads(文字行)
                    except json.JSONDecodeError:
                        self.jsonEventReceived.emit(self._json({"stream": "stdout", "text": 文字行, "createdAt": time.time()}))
                        continue
                    self.jsonEventReceived.emit(self._json(事件))
                    類型 = str(事件.get("type", ""))
                    if 類型 in {"turn.completed", "turn.failed"}:
                        完成類型 = 類型
                    self._處理Json事件(事件, 任務id, 對話id)
            結束碼 = 程序.wait()
            if self.任務停止事件.is_set():
                self._完成目前任務("工作已中斷。", True, 任務id, 對話id)
                return
            if 結束碼 != 0 and 完成類型 != "turn.failed":
                錯誤 = "\n".join(stderr文字[-12:]).strip() or f"Codex 結束碼：{結束碼}"
                self._完成目前任務(f"Codex 執行失敗：\n\n{錯誤}", False, 任務id, 對話id, 失敗=True)
                return
            if 完成類型 == "turn.failed":
                if self.任務進行中:
                    錯誤 = "\n".join(stderr文字[-8:]).strip() or "Codex 回報 turn.failed。"
                    self._完成目前任務(錯誤, False, 任務id, 對話id, 失敗=True)
                return
            if self.任務進行中:
                self._完成目前任務(self.任務最後回答 or "工作已完成。", False, 任務id, 對話id)
        except Exception as 例外:
            self._完成目前任務(
                f"啟動或執行 Codex 失敗：\n\n{例外}", False, 任務id, 對話id, 失敗=True
            )
            self._新增事件(traceback.format_exc(), "debug", 對話id)
        finally:
            with self.任務鎖:
                self.任務程序 = None

    def _文字摘要(self, 文字: str, 上限: int = 90) -> str:
        文字 = 簡化空白(文字)
        return 文字[:上限] + ("…" if len(文字) > 上限 else "")

    def _發送進度(self, 任務id: str, 對話id: str, 文字: str, 詳細: Optional[dict[str, Any]] = None) -> None:
        if not 文字:
            return
        self.chatEvent.emit(self._json({
            "type": "progress", "sessionId": 對話id, "taskId": 任務id,
            "text": 文字, "data": 詳細 or {}, "createdAt": time.time(),
        }))
        self._新增事件(文字, "progress", 對話id, 詳細)

    def _處理Json事件(self, 事件: dict[str, Any], 任務id: str, 對話id: str) -> None:
        類型 = str(事件.get("type", ""))
        if 類型 == "thread.started":
            self.任務thread_id = str(事件.get("thread_id", ""))
            self._發送進度(任務id, 對話id, "Codex 已建立工作執行緒")
            return
        if 類型 == "turn.started":
            self._發送進度(任務id, 對話id, "正在分析你的需求…")
            return
        if 類型.startswith("item."):
            item = 事件.get("item") or {}
            if not isinstance(item, dict):
                return
            item類型 = str(item.get("type", ""))
            階段 = 類型.split(".", 1)[1]
            if item類型 == "agent_message":
                文字 = str(item.get("text", "") or item.get("content", "")).strip()
                if 文字:
                    self.任務最後回答 = 文字
                    self.chatEvent.emit(self._json({
                        "type": "assistant_preview", "sessionId": 對話id,
                        "taskId": 任務id, "text": 文字, "createdAt": time.time(),
                    }))
                return
            if item類型 == "reasoning":
                if 階段 == "started":
                    self._發送進度(任務id, 對話id, "Codex 正在思考…")
                return
            if item類型 == "command_execution":
                命令 = str(item.get("command", "") or item.get("cmd", ""))
                摘要 = self._文字摘要(命令, 110)
                if 階段 == "started":
                    self._發送進度(任務id, 對話id, f"執行命令：{摘要}", {"kind": "command", "command": 命令})
                elif 階段 == "completed":
                    結束碼 = item.get("exit_code")
                    if 結束碼 not in (None, 0, "0"):
                        self._發送進度(任務id, 對話id, f"命令結束碼 {結束碼}：{摘要}", {"kind": "command", "command": 命令, "exitCode": 結束碼})
                return
            if item類型 in {"file_change", "file_changes"}:
                路徑們: list[str] = []
                changes = item.get("changes") or item.get("files") or []
                if isinstance(changes, list):
                    for 變更 in changes:
                        if isinstance(變更, dict):
                            p = 變更.get("path") or 變更.get("file_path")
                            if p:
                                路徑們.append(str(p))
                        elif 變更:
                            路徑們.append(str(變更))
                p = item.get("path") or item.get("file_path")
                if p:
                    路徑們.append(str(p))
                路徑們 = list(dict.fromkeys(路徑們))
                if 路徑們:
                    顯示 = "、".join(Path(x).name for x in 路徑們[:5])
                    self._發送進度(任務id, 對話id, f"已修改檔案：{顯示}", {"kind": "files", "paths": 路徑們})
                return
            if item類型 == "mcp_tool_call":
                server = item.get("server") or item.get("server_name") or "MCP"
                tool = item.get("tool") or item.get("tool_name") or "工具"
                self._發送進度(任務id, 對話id, f"呼叫 {server}：{tool}", {"kind": "mcp"})
                return
            if item類型 == "web_search":
                query = str(item.get("query", ""))
                self._發送進度(任務id, 對話id, f"搜尋：{self._文字摘要(query)}", {"kind": "search"})
                return
            if item類型 in {"plan", "plan_update"}:
                self._發送進度(任務id, 對話id, "已更新工作計畫", {"kind": "plan"})
                return
            if 階段 == "started" and item類型:
                self._發送進度(任務id, 對話id, f"正在處理：{item類型}", {"kind": item類型})
            return
        if 類型 == "turn.completed":
            使用量 = 事件.get("usage") or {}
            self.任務使用量 = 使用量 if isinstance(使用量, dict) else {}
            self._完成目前任務(self.任務最後回答 or "工作已完成。", False, 任務id, 對話id)
            return
        if 類型 == "turn.failed":
            錯誤 = 事件.get("error") or 事件.get("message") or "Codex 工作失敗。"
            if isinstance(錯誤, dict):
                錯誤 = 錯誤.get("message") or json.dumps(錯誤, ensure_ascii=False)
            self._完成目前任務(str(錯誤), False, 任務id, 對話id, 失敗=True)
            return
        if 類型 == "error":
            錯誤 = 事件.get("message") or 事件.get("error") or "未知錯誤"
            if isinstance(錯誤, dict):
                錯誤 = 錯誤.get("message") or json.dumps(錯誤, ensure_ascii=False)
            self._發送進度(任務id, 對話id, f"錯誤：{self._文字摘要(str(錯誤), 160)}", {"kind": "error"})

    def _完成目前任務(self, 回答: str, 中斷: bool, 任務id: str, 對話id: str, 失敗: bool = False) -> None:
        with self.任務鎖:
            if not self.任務進行中 or 任務id != self.任務id:
                return
            self.任務進行中 = False
        回答 = str(回答 or "").strip() or ("工作已中斷。" if 中斷 else "工作已完成。")
        訊息id = self.資料庫.加入訊息(
            對話id,
            "assistant",
            回答,
            "text",
            {
                "taskId": 任務id,
                "interrupted": 中斷,
                "failed": 失敗,
                "threadId": self.任務thread_id,
                "usage": self.任務使用量,
            },
        )
        self.chatEvent.emit(self._json({
            "type": "assistant_finish", "sessionId": 對話id,
            "taskId": 任務id, "messageId": 訊息id, "content": 回答,
            "interrupted": 中斷, "failed": 失敗,
            "usage": self.任務使用量, "createdAt": time.time(),
        }))
        self.taskRunningChanged.emit(False)
        self.statusChanged.emit("Codex 已就緒", "ready")
        self._新增事件("工作已中斷。" if 中斷 else ("工作失敗。" if 失敗 else "工作完成。"), "warning" if 中斷 else ("error" if 失敗 else "success"), 對話id)
        self._廣播歷史()
        if self.設定.自動偵測成果 and not 中斷:
            threading.Thread(
                target=self._掃描並發送成果,
                args=(Path(self.設定.專案路徑), self.任務開始時間, 任務id, 對話id),
                name="ArtifactScanner",
                daemon=True,
            ).start()

    @Slot()
    def stopCurrentTask(self) -> None:
        with self.任務鎖:
            程序 = self.任務程序
            任務id = self.任務id
            對話id = self.任務對話id
            if not self.任務進行中:
                return
            self.任務停止事件.set()
        try:
            if 程序 and 程序.poll() is None:
                程序.terminate()
                threading.Thread(target=self._延遲強制結束, args=(程序,), daemon=True).start()
        except Exception:
            pass
        self._完成目前任務("工作已中斷。", True, 任務id, 對話id)

    @staticmethod
    def _延遲強制結束(程序: subprocess.Popen[bytes]) -> None:
        time.sleep(1.2)
        try:
            if 程序.poll() is None:
                程序.kill()
        except Exception:
            pass

    # ---------- 成果掃描 ----------
    def _成果類型(self, 路徑: Path) -> str:
        ext = 路徑.suffix.lower()
        if ext in self.圖片副檔名:
            return "image"
        if ext in self.影片副檔名:
            return "video"
        if ext in self.音訊副檔名:
            return "audio"
        return "file"

    def _掃描並發送成果(self, 專案: Path, 開始時間: float, 任務id: str, 對話id: str) -> None:
        try:
            if not 專案.is_dir():
                return
            候選: list[Path] = []
            掃描數 = 0
            for 根, 資料夾們, 檔案們 in os.walk(專案):
                資料夾們[:] = [x for x in 資料夾們 if x.lower() not in self.排除資料夾]
                for 名稱 in 檔案們:
                    掃描數 += 1
                    if 掃描數 > 120000:
                        break
                    路徑 = Path(根) / 名稱
                    if 路徑.suffix.lower() not in (self.圖片副檔名 | self.影片副檔名 | self.音訊副檔名 | self.其他成果副檔名):
                        continue
                    try:
                        if 路徑.stat().st_mtime >= 開始時間 - 1.0:
                            候選.append(路徑)
                    except OSError:
                        pass
                if 掃描數 > 120000:
                    break
            候選.sort(key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)
            已送: set[str] = set()
            for 路徑 in 候選[:30]:
                鍵 = str(路徑.resolve()).lower()
                if 鍵 in 已送:
                    continue
                已送.add(鍵)
                try:
                    stat = 路徑.stat()
                except OSError:
                    continue
                種類 = self._成果類型(路徑)
                資料 = {
                    "name": 路徑.name,
                    "path": str(路徑.resolve()),
                    "fileUrl": 檔案網址(路徑),
                    "kind": 種類,
                    "size": stat.st_size,
                    "modifiedAt": stat.st_mtime,
                }
                訊息id = self.資料庫.加入訊息(對話id, "assistant", 路徑.name, "artifact", 資料)
                self.chatEvent.emit(self._json({
                    "type": "artifact", "sessionId": 對話id, "taskId": 任務id,
                    "messageId": 訊息id, "item": 資料, "createdAt": time.time(),
                }))
        except Exception as 例外:
            self._新增事件(f"成果掃描失敗：{例外}", "debug", 對話id)

    # ---------- 獨立 PowerShell 終端 ----------
    @Slot()
    def startDebugTerminal(self) -> None:
        if PtyProcess is None:
            self._新增事件("缺少 pywinpty，無法啟動進階終端。", "error")
            return
        if self.終端是否執行中():
            return
        專案 = Path(self.設定.專案路徑 or str(Path.home()))
        if not 專案.is_dir():
            專案 = Path.home()
        powershell = Path(self.設定.PowerShell路徑)
        if not powershell.exists():
            找到 = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
            if not 找到:
                self._新增事件("找不到 PowerShell。", "error")
                return
            powershell = Path(找到)
        try:
            環境 = os.environ.copy()
            環境["PYTHONIOENCODING"] = "utf-8"
            環境.setdefault("TERM", "xterm-256color")
            self.終端停止事件.clear()
            self.終端程序 = PtyProcess.spawn(
                [str(powershell), "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit"],
                cwd=str(專案),
                env=環境,
                dimensions=(self.設定.終端列數, self.設定.終端欄數),
            )
            self.終端讀取執行緒 = threading.Thread(target=self._終端讀取迴圈, name="DebugTerminalReader", daemon=True)
            self.終端讀取執行緒.start()
            self.terminalRunningChanged.emit(True)
            初始化 = "$OutputEncoding=[Text.UTF8Encoding]::new($false);[Console]::OutputEncoding=$OutputEncoding;[Console]::InputEncoding=$OutputEncoding;Clear-Host\r"
            QTimer.singleShot(350, lambda: self._終端安全寫入(初始化))
        except Exception as 例外:
            self.終端程序 = None
            self.terminalRunningChanged.emit(False)
            self._新增事件(f"終端啟動失敗：{例外}", "error")

    def _終端讀取迴圈(self) -> None:
        while not self.終端停止事件.is_set():
            程序 = self.終端程序
            if not 程序:
                break
            try:
                片段 = 程序.read(8192)
                if 片段:
                    self.terminalOutputReceived.emit(片段)
                else:
                    time.sleep(0.01)
            except EOFError:
                break
            except Exception:
                break
        self.終端程序 = None
        self.terminalRunningChanged.emit(False)

    def 終端是否執行中(self) -> bool:
        try:
            return bool(self.終端程序 and self.終端程序.isalive())
        except Exception:
            return False

    def _終端安全寫入(self, 文字: str) -> bool:
        if not self.終端是否執行中():
            return False
        try:
            with self.終端寫入鎖:
                self.終端程序.write(文字)
            return True
        except Exception:
            return False

    @Slot(str)
    def sendTerminalInput(self, 文字: str) -> None:
        self._終端安全寫入(文字)

    @Slot(str)
    def sendTerminalLine(self, 文字: str) -> None:
        文字 = 文字.replace("\r\n", "\n").replace("\n", "\r")
        self._終端安全寫入(文字 + "\r")

    @Slot(int, int)
    def resizeTerminal(self, 列數: int, 欄數: int) -> None:
        列數 = max(12, min(100, int(列數)))
        欄數 = max(40, min(300, int(欄數)))
        self.設定.終端列數 = 列數
        self.設定.終端欄數 = 欄數
        try:
            if self.終端是否執行中() and hasattr(self.終端程序, "setwinsize"):
                self.終端程序.setwinsize(列數, 欄數)
        except Exception:
            pass

    @Slot()
    def stopDebugTerminal(self) -> None:
        self.終端停止事件.set()
        try:
            if self.終端程序 and hasattr(self.終端程序, "close"):
                self.終端程序.close(force=True)
        except Exception:
            pass
        self.終端程序 = None
        self.terminalRunningChanged.emit(False)

    def 關閉(self) -> None:
        self._正在關閉 = True
        self.stopCurrentTask()
        self.stopDebugTerminal()
        self.資料庫.關閉()


# -----------------------------------------------------------------------------
# WebView UI
# -----------------------------------------------------------------------------
HTML = r"""
<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex Workspace</title>
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
<style>
:root{
  --bg:#f7f7f8;--panel:#fff;--text:#171717;--muted:#777;--line:#e5e5e5;
  --soft:#f0f0f1;--dark:#171717;--blue:#2f76e8;--green:#10a37f;--red:#e14b55;
  --shadow:0 18px 50px rgba(0,0,0,.12);--radius:18px;
}
*{box-sizing:border-box}html,body{margin:0;width:100%;height:100%;overflow:hidden;background:var(--bg);color:var(--text);font-family:"Microsoft JhengHei UI","Segoe UI",sans-serif}
button,input,textarea,select{font:inherit}button{cursor:pointer}
.app{height:100%;display:grid;grid-template-rows:64px 1fr}
.topbar{display:flex;align-items:center;gap:12px;padding:0 18px;background:rgba(255,255,255,.92);border-bottom:1px solid var(--line)}
.icon-btn,.top-btn{height:38px;border:1px solid var(--line);background:#fff;border-radius:12px;padding:0 13px;color:#333}.icon-btn{width:40px;padding:0;font-size:18px}.top-btn.primary{background:#171717;color:#fff;border-color:#171717}.top-btn.stop{color:var(--red);border-color:#efc7cb}.top-btn:disabled{opacity:.42;cursor:not-allowed}
.brand{display:flex;align-items:center;gap:10px}.brand-logo{width:38px;height:38px;border-radius:12px;background:#171717;color:#fff;display:grid;place-items:center;font-size:19px}.brand h1{margin:0;font-size:16px}.brand small{display:block;color:#888;font-size:10px;margin-top:2px}.project-chip{display:flex;align-items:center;gap:8px;height:38px;border:1px solid var(--line);border-radius:12px;background:#fff;padding:0 14px;max-width:320px}.project-chip span:last-child{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.spacer{flex:1}.status{display:flex;align-items:center;gap:8px;border:1px solid var(--line);background:#fff;border-radius:999px;padding:8px 13px;color:#777;font-size:12px}.status-dot{width:8px;height:8px;border-radius:50%;background:#aaa}.status.ready .status-dot{background:var(--green);box-shadow:0 0 0 4px rgba(16,163,127,.12)}.status.working .status-dot{background:var(--blue);animation:pulse 1.1s infinite}.status.error .status-dot{background:var(--red)}@keyframes pulse{50%{opacity:.35}}
.main{position:relative;overflow:auto}.conversation{width:min(920px,calc(100% - 40px));margin:0 auto;padding:34px 0 180px}.empty{min-height:62vh;display:grid;place-items:center;text-align:center}.empty-logo{width:52px;height:52px;border-radius:16px;background:#171717;color:#fff;display:grid;place-items:center;margin:0 auto 16px;font-size:24px}.empty h2{font-size:25px;margin:0 0 8px}.empty p{color:#777;line-height:1.7}.tips{display:flex;gap:8px;justify-content:center;flex-wrap:wrap;margin-top:16px}.tip{padding:8px 12px;border:1px solid var(--line);border-radius:999px;background:#fff;color:#666;font-size:12px}
.message{display:grid;grid-template-columns:34px minmax(0,1fr);gap:14px;margin:0 0 30px}.message.user{grid-template-columns:minmax(0,1fr) 34px}.message.user .avatar{grid-column:2}.message.user .body{grid-column:1;grid-row:1;justify-self:end;max-width:78%}.avatar{width:34px;height:34px;border-radius:10px;background:#171717;color:#fff;display:grid;place-items:center;font-size:12px}.message.user .avatar{background:#e8e8e9;color:#333}.head{display:flex;align-items:center;gap:8px;margin:0 0 8px}.name{font-weight:700;font-size:13px}.time{font-size:10px;color:#999}.text{font-size:14px;line-height:1.72;white-space:normal;word-break:break-word}.user .text{background:#e9e9eb;border-radius:18px;padding:10px 14px;white-space:pre-wrap}.text pre{background:#171717;color:#eaeaea;border-radius:12px;padding:14px;overflow:auto}.text code{font-family:Consolas,monospace;background:#eee;padding:2px 5px;border-radius:5px}.text pre code{background:none;padding:0}.user-files{display:flex;justify-content:flex-end;gap:8px;flex-wrap:wrap;margin-bottom:8px}.user-file{border:1px solid var(--line);background:#fff;border-radius:12px;padding:7px 9px;display:flex;align-items:center;gap:8px;max-width:240px}.user-file img{width:42px;height:42px;object-fit:cover;border-radius:8px}.user-file span{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;font-size:11px}
.task-card{border:1px solid var(--line);border-radius:14px;background:#fff;overflow:hidden}.task-head{display:flex;align-items:center;gap:9px;padding:12px 14px}.spinner{width:15px;height:15px;border:2px solid #d7e2f5;border-top-color:var(--blue);border-radius:50%;animation:spin .8s linear infinite}@keyframes spin{to{transform:rotate(360deg)}}.task-title{font-size:12px;font-weight:700;flex:1}.task-toggle{border:0;background:none;color:#888;font-size:11px}.progress-list{padding:0 14px 12px;border-top:1px solid #f1f1f1}.progress-item{font-size:11px;color:#777;padding:7px 0;border-bottom:1px dashed #eee}.progress-item:last-child{border:0}.task-card.done .spinner{border:0;animation:none}.task-card.done .spinner:before{content:'✓';color:var(--green);font-weight:800}.task-card.failed .spinner{border:0;animation:none}.task-card.failed .spinner:before{content:'!';color:var(--red);font-weight:800}.task-card.collapsed .progress-list{display:none}
.artifact-card{border:1px solid var(--line);border-radius:16px;overflow:hidden;background:#fff;max-width:620px}.artifact-preview{background:#eee;display:grid;place-items:center;max-height:420px}.artifact-preview img,.artifact-preview video{display:block;max-width:100%;max-height:420px}.artifact-audio{padding:16px}.artifact-audio audio{width:100%}.artifact-file{display:flex;align-items:center;gap:14px;padding:18px}.file-badge{width:52px;height:52px;border-radius:13px;background:#171717;color:#fff;display:grid;place-items:center;font-size:11px;font-weight:800}.artifact-foot{display:flex;gap:12px;align-items:center;padding:12px 14px;border-top:1px solid var(--line)}.artifact-info{min-width:0;flex:1}.artifact-name{font-size:12px;font-weight:700;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.artifact-meta{font-size:10px;color:#888;margin-top:3px}.artifact-actions{display:flex;gap:6px}.artifact-actions button{height:30px;border:1px solid var(--line);background:#fff;border-radius:8px;font-size:11px}
.composer-wrap{position:fixed;left:0;right:0;bottom:0;padding:18px 24px 22px;background:linear-gradient(transparent,var(--bg) 28%);z-index:5}.composer-shell{width:min(920px,calc(100% - 40px));margin:auto}.attachments{display:none;gap:8px;flex-wrap:wrap;margin:0 0 8px}.attachments.show{display:flex}.attach{display:flex;align-items:center;gap:8px;background:#fff;border:1px solid var(--line);border-radius:12px;padding:7px 8px;max-width:260px}.attach img{width:42px;height:42px;object-fit:cover;border-radius:8px}.attach-icon{width:42px;height:42px;border-radius:8px;background:#171717;color:#fff;display:grid;place-items:center;font-size:9px}.attach-info{min-width:0}.attach-name{font-size:11px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.attach-size{font-size:9px;color:#999}.attach-remove{border:0;background:none;font-size:16px;color:#888}.composer{display:grid;grid-template-columns:42px 1fr 42px;gap:8px;align-items:end;background:#fff;border:1px solid #d7d7d7;border-radius:24px;padding:9px 10px;box-shadow:0 10px 32px rgba(0,0,0,.09)}.composer textarea{border:0;outline:0;resize:none;min-height:40px;max-height:170px;padding:9px 4px;line-height:1.45;background:transparent}.round{width:40px;height:40px;border:0;border-radius:50%;background:#f1f1f1;font-size:20px}.send{width:40px;height:40px;border:0;border-radius:50%;background:#171717;color:#fff;font-size:19px}.send:disabled{opacity:.28}.hint{text-align:center;color:#999;font-size:9px;margin-top:7px}
.backdrop{position:fixed;inset:64px 0 0;background:rgba(0,0,0,.18);opacity:0;pointer-events:none;transition:.2s;z-index:20}.backdrop.show{opacity:1;pointer-events:auto}.drawer{position:fixed;top:64px;bottom:0;width:min(390px,92vw);background:#fff;z-index:21;box-shadow:var(--shadow);transition:.24s;display:grid;grid-template-rows:auto 1fr}.drawer.left{left:0;transform:translateX(-105%)}.drawer.right{right:0;transform:translateX(105%)}.drawer.show{transform:none}.drawer-head{display:flex;align-items:center;padding:15px;border-bottom:1px solid var(--line)}.drawer-head h3{margin:0;font-size:15px}.drawer-body{overflow:auto;padding:13px}.drawer-close{margin-left:auto;border:0;background:none;font-size:20px}.new-chat{width:100%;height:40px;border:1px solid var(--line);background:#fff;border-radius:11px;margin-bottom:10px}.history-item{position:relative;padding:11px 36px 11px 12px;border-radius:10px;margin-bottom:4px}.history-item:hover,.history-item.active{background:#f1f1f1}.history-title{font-size:12px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap}.history-meta{font-size:9px;color:#999;margin-top:4px}.history-delete{position:absolute;right:8px;top:13px;border:0;background:none;color:#999}.section{padding:4px 0 16px;margin-bottom:14px;border-bottom:1px solid var(--line)}.section h4{margin:0 0 10px;font-size:12px}.field{display:grid;gap:5px;margin-bottom:10px}.field label{font-size:10px;color:#777}.field input,.field select{height:38px;border:1px solid var(--line);border-radius:10px;padding:0 10px;background:#fff}.check{display:flex;align-items:center;justify-content:space-between;gap:12px;padding:9px 0}.check strong{font-size:11px}.check small{display:block;color:#999;font-size:9px;margin-top:3px}.check input{width:18px;height:18px}.drawer-btn{height:36px;border:1px solid var(--line);background:#fff;border-radius:10px;padding:0 11px}.row{display:flex;gap:8px}.row>*{flex:1}.event{display:grid;grid-template-columns:9px 1fr;gap:9px;padding:8px 0}.event-dot{width:7px;height:7px;border-radius:50%;background:#aaa;margin-top:5px}.event.success .event-dot{background:var(--green)}.event.error .event-dot{background:var(--red)}.event.warning .event-dot{background:#e6a700}.event-text{font-size:11px;line-height:1.5}.event-time{font-size:9px;color:#aaa;margin-top:3px}
.sheet{position:fixed;left:18px;right:18px;bottom:18px;height:min(650px,78vh);background:#0b1118;color:#d9e5ef;border-radius:18px;z-index:30;box-shadow:0 26px 70px rgba(0,0,0,.35);display:none;grid-template-rows:50px 1fr}.sheet.show{display:grid}.sheet-head{display:flex;align-items:center;gap:8px;padding:0 12px;border-bottom:1px solid #263240}.sheet-head strong{font:12px Consolas,monospace}.sheet-head .spacer{flex:1}.sheet-head button{height:30px;border:1px solid #304152;background:#121e29;color:#d9e5ef;border-radius:8px;font-size:10px}.tabs{display:flex;gap:6px}.tab.active{background:#234865}.sheet-body{min-height:0}.panel{display:none;height:100%}.panel.active{display:grid}.json-panel{grid-template-rows:1fr}.json-log{overflow:auto;margin:0;padding:12px;font:11px/1.55 Consolas,monospace;white-space:pre-wrap}.terminal-panel{position:relative;grid-template-rows:1fr 48px}.terminal{position:relative;overflow:hidden;background:#05090e}.screen{position:absolute;inset:0;margin:0;padding:12px;overflow:hidden;white-space:pre;font:13px/1.32 Consolas,"Cascadia Mono","Microsoft JhengHei UI",monospace}.cursor{position:absolute;width:7px;height:16px;background:#67d8ff;animation:blink 1s steps(1) infinite;display:none}.terminal-ime{position:absolute;z-index:3;width:3px;height:20px;opacity:.02;background:transparent;border:0;color:transparent;caret-color:transparent}.terminal-input{display:grid;grid-template-columns:1fr auto;gap:8px;padding:7px;border-top:1px solid #263240}.terminal-input textarea{resize:none;border:1px solid #263b4d;border-radius:9px;background:#08131d;color:#fff;padding:8px}.terminal-input button{border:1px solid #31526c;background:#15314a;color:#fff;border-radius:9px;padding:0 14px}.lightbox{position:fixed;inset:0;background:rgba(0,0,0,.8);z-index:50;display:none;place-items:center}.lightbox.show{display:grid}.lightbox img{max-width:92vw;max-height:90vh}.lightbox button{position:absolute;right:22px;top:18px;width:42px;height:42px;border:0;border-radius:50%;font-size:25px}.toast{position:fixed;right:22px;bottom:22px;z-index:80;padding:10px 14px;background:#171717;color:#fff;border-radius:10px;opacity:0;transform:translateY(10px);transition:.2s;pointer-events:none;font-size:12px}.toast.show{opacity:1;transform:none}.toast.error{background:#9d2631}
@keyframes blink{50%{opacity:0}}@media(max-width:850px){.brand small{display:none}.project-chip{max-width:170px}.conversation,.composer-shell{width:calc(100% - 24px)}.status span:last-child{display:none}}
</style>
</head>
<body>
<div class="app">
<header class="topbar">
  <button id="btnHistory" class="icon-btn">☰</button>
  <div class="brand"><div class="brand-logo">✦</div><div><h1>Codex Workspace</h1><small>JSON 引擎版 · v<span id="version">0.5.0</span></small></div></div>
  <button id="projectChip" class="project-chip"><span>📁</span><span id="projectName">尚未選擇專案</span></button>
  <div class="spacer"></div>
  <div id="status" class="status idle"><span class="status-dot"></span><span id="statusText">尚未啟動</span></div>
  <button id="btnRun" class="top-btn primary"><span id="runIcon">▶</span> <span id="runLabel">啟動</span></button>
  <button id="btnTimeline" class="icon-btn" title="執行履歷">◷</button>
  <button id="btnConsole" class="icon-btn" title="JSON 事件與 PowerShell">›_</button>
  <button id="btnSettings" class="icon-btn" title="設定">⚙</button>
</header>
<main id="main" class="main"><div id="conversation" class="conversation"></div></main>
</div>
<div class="composer-wrap"><div class="composer-shell"><div id="attachments" class="attachments"></div><div class="composer"><button id="btnAttach" class="round">＋</button><textarea id="promptInput" placeholder="請先啟動 Codex…"></textarea><button id="btnSend" class="send" disabled>↑</button></div><div class="hint">Enter 送出 · Shift+Enter 換行 · 可直接 Ctrl+V 貼上圖片</div></div></div>
<div id="backdrop" class="backdrop"></div>
<aside id="historyDrawer" class="drawer left"><div class="drawer-head"><h3>對話履歷</h3><button class="drawer-close" data-close>×</button></div><div class="drawer-body"><button id="btnNewChat" class="new-chat">＋ 新對話</button><input id="historySearch" placeholder="搜尋對話" style="width:100%;height:38px;border:1px solid var(--line);border-radius:10px;padding:0 10px;margin-bottom:10px"><div id="historyList"></div></div></aside>
<aside id="timelineDrawer" class="drawer right"><div class="drawer-head"><h3>執行履歷</h3><button class="drawer-close" data-close>×</button></div><div class="drawer-body"><div id="timelineSummary" style="padding:10px;border:1px solid var(--line);border-radius:12px;margin-bottom:10px;font-size:11px;line-height:1.6"></div><div id="timeline"></div></div></aside>
<aside id="settingsDrawer" class="drawer right"><div class="drawer-head"><h3>工作站設定</h3><button class="drawer-close" data-close>×</button></div><div class="drawer-body">
<section class="section"><h4>Codex</h4><div class="field"><label>專案資料夾</label><div class="row"><input id="projectPath"><button id="btnBrowse" class="drawer-btn">瀏覽</button></div></div><div class="field"><label>Codex 命令或完整路徑</label><input id="codexCommand" value="codex"></div><div class="field"><label>模型（留空使用 Codex 預設）</label><input id="model" placeholder="例如 gpt-5.6-sol"></div><div class="field"><label>沙盒模式</label><select id="sandbox"><option value="read-only">唯讀</option><option value="workspace-write">工作區可寫（建議）</option><option value="danger-full-access">完整權限（高風險）</option></select></div><div class="field"><label>批准模式</label><select id="approval"><option value="never">never（自動執行）</option><option value="on-request">on-request</option><option value="untrusted">untrusted</option></select></div><div class="check"><div><strong>跳過 Git 專案檢查</strong><small>RPG Maker 專案通常不是 Git 專案，建議開啟。</small></div><input id="skipGit" type="checkbox" checked></div><div class="field"><label>額外 CLI 參數</label><input id="extraArgs" placeholder="通常留空"></div></section>
<section class="section"><h4>聊天與成果</h4><div class="check"><div><strong>帶入最近對話</strong><small>每次新執行會把近期聊天作為上下文。</small></div><input id="includeContext" type="checkbox" checked></div><div class="field"><label>最近對話字數上限</label><input id="contextLimit" type="number" min="2000" max="50000" step="1000"></div><div class="check"><div><strong>自動偵測成果</strong><small>完成後掃描新生成的圖片、影片、音訊與檔案。</small></div><input id="autoArtifacts" type="checkbox" checked></div><div class="check"><div><strong>送出後清空附件</strong></div><input id="clearAttachments" type="checkbox" checked></div><div class="check"><div><strong>開啟程式後自動啟動</strong></div><input id="autoStart" type="checkbox"></div></section>
<section class="section"><h4>進階終端</h4><div class="field"><label>PowerShell 路徑</label><input id="powershellPath"></div><div class="row"><button id="btnOpenProject" class="drawer-btn">開啟專案</button><button id="btnOpenLogs" class="drawer-btn">執行紀錄</button></div></section>
<section class="section"><h4>資料位置</h4><div style="font-size:9px;color:#888;line-height:1.7">設定：<span id="configPath"></span><br>履歷：<span id="databasePath"></span></div></section>
</div></aside>
<section id="consoleSheet" class="sheet"><div class="sheet-head"><strong>Codex Workspace Console</strong><div class="tabs"><button id="tabJson" class="tab active">JSON 事件</button><button id="tabTerminal" class="tab">PowerShell</button></div><div class="spacer"></div><button id="btnClearConsole">清空</button><button id="btnCloseConsole">關閉</button></div><div class="sheet-body"><div id="jsonPanel" class="panel json-panel active"><pre id="jsonLog" class="json-log"></pre></div><div id="terminalPanel" class="panel terminal-panel"><div id="terminal" class="terminal" tabindex="0"><pre id="screen" class="screen"></pre><div id="cursor" class="cursor"></div><textarea id="terminalIme" class="terminal-ime"></textarea></div><div class="terminal-input"><textarea id="terminalInput" placeholder="直接輸入 PowerShell 命令；Enter 送出，Shift+Enter 換行"></textarea><button id="btnTerminalSend">送出</button></div></div></div></section>
<div id="lightbox" class="lightbox"><button id="lightboxClose">×</button><img id="lightboxImage"></div><div id="toast" class="toast"></div>
<script>
'use strict';
let bridge=null,ready=false,taskRunning=false,currentSessionId='',currentSession={},histories=[],attachments=[],activeTaskId='';
let currentDrawer=null,saveTimer=null,jsonText='',terminalRunning=false,imeComposing=false,imeTimer=null;
const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':'&quot;',"'":"&#39;"}[m]));
const baseName=p=>String(p||'').replace(/[\\/]+$/,'').split(/[\\/]/).pop()||'尚未選擇專案';
function fmtTime(ts){return ts?new Date(Number(ts)*1000).toLocaleTimeString('zh-TW',{hour:'2-digit',minute:'2-digit',hour12:false}):''}function fmtDate(ts){return ts?new Date(Number(ts)*1000).toLocaleString('zh-TW',{month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}):''}function fmtSize(n){n=Number(n||0);if(n<1024)return n+' B';if(n<1048576)return(n/1024).toFixed(1)+' KB';if(n<1073741824)return(n/1048576).toFixed(1)+' MB';return(n/1073741824).toFixed(1)+' GB'}
function toast(t,k=''){const e=$('toast');e.textContent=t;e.className='toast '+k+' show';clearTimeout(e._t);e._t=setTimeout(()=>e.classList.remove('show'),2800)}
function scrollBottom(force=true){setTimeout(()=>{const m=$('main');if(force||m.scrollHeight-m.scrollTop-m.clientHeight<220)m.scrollTop=m.scrollHeight},30)}
function setStatus(t,k='idle'){$('statusText').textContent=t;$('status').className='status '+k}
function setReady(v){ready=!!v;$('btnSend').disabled=!ready||taskRunning;$('promptInput').disabled=!ready||taskRunning;$('promptInput').placeholder=!ready?'請先啟動 Codex…':(taskRunning?'Codex 正在工作…':'傳訊息給 Codex，或直接 Ctrl+V 貼上圖片…');$('runLabel').textContent=ready?'重設':'啟動';$('runIcon').textContent=ready?'↻':'▶'}
function setTaskRunning(v){taskRunning=!!v;$('btnSend').disabled=!ready||taskRunning;$('promptInput').disabled=!ready||taskRunning;$('promptInput').placeholder=taskRunning?'Codex 正在工作…':(ready?'傳訊息給 Codex，或直接 Ctrl+V 貼上圖片…':'請先啟動 Codex…');$('btnRun').className='top-btn '+(taskRunning?'stop':'primary');$('runLabel').textContent=taskRunning?'停止':(ready?'重設':'啟動');$('runIcon').textContent=taskRunning?'■':(ready?'↻':'▶')}
function updateProject(){const p=$('projectPath').value.trim();$('projectName').textContent=baseName(p);$('projectChip').title=p||'點擊選擇專案'}
function collectSettings(){return{projectPath:$('projectPath').value.trim(),codexCommand:$('codexCommand').value.trim()||'codex',model:$('model').value.trim(),sandbox:$('sandbox').value,approval:$('approval').value,skipGit:$('skipGit').checked,autoStart:$('autoStart').checked,autoArtifacts:$('autoArtifacts').checked,clearAttachments:$('clearAttachments').checked,includeContext:$('includeContext').checked,contextLimit:Number($('contextLimit').value||14000),extraArgs:$('extraArgs').value.trim(),powershellPath:$('powershellPath').value.trim()}}
function saveSettings(){updateProject();if(!bridge)return;clearTimeout(saveTimer);saveTimer=setTimeout(()=>bridge.saveSettings(JSON.stringify(collectSettings())),180)}
function openDrawer(id){closeDrawers(false);currentDrawer=$(id);currentDrawer.classList.add('show');$('backdrop').classList.add('show')}function closeDrawers(h=true){document.querySelectorAll('.drawer.show').forEach(x=>x.classList.remove('show'));currentDrawer=null;if(h)$('backdrop').classList.remove('show')}
function toggleConsole(show=null){const e=$('consoleSheet'),n=show===null?!e.classList.contains('show'):show;e.classList.toggle('show',n);if(n){if($('terminalPanel').classList.contains('active'))ensureTerminal();setTimeout(fitTerminal,100)}}
function simpleMarkdown(text){const parts=String(text||'').split(/```/);let html='';parts.forEach((p,i)=>{if(i%2){const x=p.indexOf('\n');if(x>=0&&x<25)p=p.slice(x+1);html+='<pre><code>'+esc(p.trimEnd())+'</code></pre>'}else{let s=esc(p).replace(/`([^`\n]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');s=s.replace(/(^|\n)[-•]\s+([^\n]+)/g,'$1<span style="display:block;padding-left:14px">• $2</span>');html+=s.replace(/\n/g,'<br>')}});return html}
function emptyState(){return`<section class="empty"><div><div class="empty-logo">✦</div><h2>今天要修改什麼？</h2><p>Codex 以 JSON 事件在背景工作，不再解析混亂的終端畫面。<br>可以上傳 JSON、JS、ZIP、圖片，或直接貼上截圖。</p><div class="tips"><span class="tip">🗺 RPG Maker 地圖</span><span class="tip">🧩 插件修正</span><span class="tip">🖼 圖片成果</span><span class="tip">🎬 影片預覽</span></div></div></section>`}function ensureNotEmpty(){const e=$('conversation').querySelector('.empty');if(e)e.remove()}
function userHtml(m){const a=m.data?.attachments||[];const files=a.length?`<div class="user-files">${a.map(x=>`<div class="user-file">${x.isImage&&x.preview?`<img src="${esc(x.preview)}">`:'<span>📄</span>'}<span>${esc(x.name)}</span></div>`).join('')}</div>`:'';return`<article class="message user" data-message-id="${m.id||''}"><div class="avatar">你</div><div class="body">${files}<div class="text">${esc(m.content||'')}</div></div></article>`}
function assistantHtml(m){return`<article class="message assistant" data-message-id="${m.id||''}"><div class="avatar">C</div><div class="body"><div class="head"><span class="name">Codex</span><span class="time">${fmtTime(m.created_at)}</span></div><div class="text">${simpleMarkdown(m.content||'')}</div></div></article>`}
function artifactHtml(item,id=''){const k=item.kind||'file';let body='';if(k==='image')body=`<div class="artifact-preview"><img src="${esc(item.fileUrl)}" data-lightbox="${esc(item.fileUrl)}"></div>`;else if(k==='video')body=`<div class="artifact-preview"><video controls preload="metadata" src="${esc(item.fileUrl)}"></video></div>`;else if(k==='audio')body=`<div class="artifact-audio"><audio controls preload="metadata" src="${esc(item.fileUrl)}"></audio></div>`;else{const ext=(String(item.name||'').split('.').pop()||'FILE').toUpperCase().slice(0,5);body=`<div class="artifact-file"><div class="file-badge">${esc(ext)}</div><div><b>成果檔案</b><div style="font-size:10px;color:#888;margin-top:5px">可直接開啟或查看所在資料夾</div></div></div>`}return`<article class="message assistant" data-message-id="${id}"><div class="avatar">C</div><div class="body"><div class="head"><span class="name">Codex 成果</span></div><div class="artifact-card">${body}<div class="artifact-foot"><div class="artifact-info"><div class="artifact-name">${esc(item.name||'')}</div><div class="artifact-meta">${esc(k.toUpperCase())} · ${fmtSize(item.size)}</div></div><div class="artifact-actions"><button data-open="${esc(item.path||'')}">開啟</button><button data-folder="${esc(item.path||'')}">資料夾</button></div></div></div></div></article>`}
function renderSession(data){currentSession=data||{};currentSessionId=data?.session?.id||'';activeTaskId='';const c=$('conversation');c.innerHTML='';const msgs=data?.messages||[];if(!msgs.length)c.innerHTML=emptyState();else msgs.forEach(m=>{if(m.kind==='artifact')c.insertAdjacentHTML('beforeend',artifactHtml(m.data||{},m.id));else if(m.role==='user')c.insertAdjacentHTML('beforeend',userHtml(m));else if(m.role==='assistant'&&String(m.content||'').trim())c.insertAdjacentHTML('beforeend',assistantHtml(m))});renderTimeline(data?.events||[]);renderHistory(histories);bindDynamic();scrollBottom()}
function addUser(e){ensureNotEmpty();$('conversation').insertAdjacentHTML('beforeend',userHtml({id:e.userMessageId,content:e.text,data:{attachments:e.attachments||[]},created_at:e.createdAt}));scrollBottom()}
function ensureTask(taskId){let card=$(`.task-card[data-task-id="${taskId}"]`);if(card)return card;ensureNotEmpty();$('conversation').insertAdjacentHTML('beforeend',`<article class="message assistant" data-live-task="${taskId}"><div class="avatar">C</div><div class="body"><div class="head"><span class="name">Codex</span><span class="time">執行中</span></div><div class="task-card" data-task-id="${taskId}"><div class="task-head"><span class="spinner"></span><span class="task-title">正在處理你的要求…</span><button class="task-toggle">收合</button></div><div class="progress-list"></div><div class="preview text" style="display:none;padding:0 14px 14px"></div></div></div></article>`);card=$(`.task-card[data-task-id="${taskId}"]`);card.querySelector('.task-toggle').onclick=()=>card.classList.toggle('collapsed');return card}
function progress(e){if(e.sessionId!==currentSessionId)return;const card=ensureTask(e.taskId),list=card.querySelector('.progress-list');const d=document.createElement('div');d.className='progress-item';d.textContent=e.text;list.appendChild(d);while(list.children.length>10)list.removeChild(list.firstChild);card.querySelector('.task-title').textContent=e.text;scrollBottom(false)}
function preview(e){if(e.sessionId!==currentSessionId)return;const card=ensureTask(e.taskId),p=card.querySelector('.preview');p.style.display='block';p.innerHTML=simpleMarkdown(e.text||'');scrollBottom(false)}
function finish(e){if(e.sessionId!==currentSessionId)return;let host=$(`.message.assistant[data-live-task="${e.taskId}"]`);if(!host){ensureNotEmpty();$('conversation').insertAdjacentHTML('beforeend',assistantHtml({id:e.messageId,content:e.content,created_at:e.createdAt}));bindDynamic();scrollBottom();return}host.dataset.messageId=e.messageId;delete host.dataset.liveTask;const card=host.querySelector('.task-card');card.classList.add(e.failed?'failed':'done','collapsed');card.querySelector('.task-title').textContent=e.failed?'工作失敗':(e.interrupted?'工作已中斷':'工作已完成');let p=card.querySelector('.preview');p.style.display='block';p.innerHTML=simpleMarkdown(e.content||'');host.querySelector('.time').textContent=fmtTime(e.createdAt);activeTaskId='';bindDynamic();scrollBottom()}
function artifact(e){if(e.sessionId!==currentSessionId)return;ensureNotEmpty();$('conversation').insertAdjacentHTML('beforeend',artifactHtml(e.item||{},e.messageId));bindDynamic();scrollBottom()}
function handleChat(raw){let e;try{e=JSON.parse(raw)}catch{return}if(e.type==='task_start'){activeTaskId=e.taskId;if(e.sessionId===currentSessionId){addUser(e);ensureTask(e.taskId)}return}if(e.type==='progress')return progress(e);if(e.type==='assistant_preview')return preview(e);if(e.type==='assistant_finish')return finish(e);if(e.type==='artifact')return artifact(e)}
function renderAttachments(){const b=$('attachments');b.innerHTML='';b.classList.toggle('show',attachments.length>0);attachments.forEach(x=>{const d=document.createElement('div');d.className='attach';d.innerHTML=`${x.isImage&&x.preview?`<img src="${esc(x.preview)}">`:`<div class="attach-icon">${esc((x.name.split('.').pop()||'FILE').toUpperCase().slice(0,4))}</div>`}<div class="attach-info"><div class="attach-name">${esc(x.name)}</div><div class="attach-size">${fmtSize(x.size)}</div></div><button class="attach-remove">×</button>`;d.querySelector('.attach-remove').onclick=()=>bridge.removeAttachment(x.id);b.appendChild(d)})}
function autoResize(){const e=$('promptInput');e.style.height='auto';e.style.height=Math.min(170,Math.max(40,e.scrollHeight))+'px'}
function sendPrompt(){const t=$('promptInput').value.trim();if(!t&&!attachments.length)return;if(!ready){toast('請先啟動 Codex','error');return}if(taskRunning){toast('上一個任務仍在執行','error');return}bridge.sendChatMessage(JSON.stringify({text:t}),ok=>{if(!ok){toast('訊息沒有送出，請查看執行履歷','error');return}$('promptInput').value='';autoResize()})}
function pasteImages(e){const a=[...(e.clipboardData?.items||[])].filter(x=>x.type?.startsWith('image/'));if(!a.length)return;e.preventDefault();a.forEach(i=>{const f=i.getAsFile();if(!f)return;const r=new FileReader();r.onload=()=>bridge.savePastedImage(String(r.result||''));r.readAsDataURL(f)});toast(`正在加入 ${a.length} 張圖片…`)}
function renderHistory(list){histories=list||histories;const q=$('historySearch').value.trim().toLowerCase(),b=$('historyList');b.innerHTML='';histories.filter(x=>!q||String(x.title).toLowerCase().includes(q)||String(x.project_path).toLowerCase().includes(q)).forEach(x=>{const d=document.createElement('div');d.className='history-item '+(x.id===currentSessionId?'active':'');d.innerHTML=`<div class="history-title">${esc(x.title||'新對話')}</div><div class="history-meta">${fmtDate(x.updated_at)} · ${x.message_count||0} 則</div><button class="history-delete">×</button>`;d.onclick=()=>bridge.loadSession(x.id);d.querySelector('button').onclick=ev=>{ev.stopPropagation();if(confirm('刪除這個對話與履歷？'))bridge.deleteSession(x.id)};b.appendChild(d)})}
function renderTimeline(events){const s=currentSession?.session||{};$('timelineSummary').innerHTML=`<b>${esc(s.title||'新對話')}</b><div style="color:#888;margin-top:5px">${esc(s.project_path||'尚未指定專案')}<br>${events.length} 筆紀錄</div>`;const b=$('timeline');b.innerHTML='';events.forEach(addTimeline)}function addTimeline(e){if(e.session_id&&e.session_id!==currentSessionId)return;const d=document.createElement('div');d.className='event '+(e.level||'info');d.innerHTML=`<div class="event-dot"></div><div><div class="event-text">${esc(e.text||'')}</div><div class="event-time">${fmtTime(e.created_at)}</div></div>`;$('timeline').appendChild(d)}
function appendJson(raw){let obj;try{obj=JSON.parse(raw)}catch{obj=raw}const line=typeof obj==='string'?obj:JSON.stringify(obj,null,2);jsonText+='\n'+line;if(jsonText.length>180000)jsonText=jsonText.slice(-140000);$('jsonLog').textContent=jsonText;$('jsonLog').scrollTop=$('jsonLog').scrollHeight}
function bindDynamic(){document.querySelectorAll('[data-open]').forEach(b=>{if(b.dataset.bound)return;b.dataset.bound='1';b.onclick=()=>bridge.openPath(b.dataset.open)});document.querySelectorAll('[data-folder]').forEach(b=>{if(b.dataset.bound)return;b.dataset.bound='1';b.onclick=()=>bridge.openContainingFolder(b.dataset.folder)});document.querySelectorAll('[data-lightbox]').forEach(i=>{if(i.dataset.bound)return;i.dataset.bound='1';i.onclick=()=>{$('lightboxImage').src=i.dataset.lightbox;$('lightbox').classList.add('show')}})}
// 簡易 ANSI 終端模型，沿用 v0.2 穩定版概念。
function isWide(ch){if(!ch)return false;const c=ch.codePointAt(0);return c>=0x1100&&(c<=0x115f||(c>=0x2e80&&c<=0xa4cf&&c!==0x303f)||(c>=0xac00&&c<=0xd7a3)||(c>=0xf900&&c<=0xfaff)||(c>=0xfe10&&c<=0xfe6f)||(c>=0xff00&&c<=0xffe6)||(c>=0x1f300&&c<=0x1faff)||(c>=0x20000&&c<=0x3fffd))}
class MiniTerminal{constructor(r=44,c=150){this.rows=r;this.cols=c;this.reset()}reset(){this.lines=Array.from({length:this.rows},()=>Array(this.cols).fill(' '));this.r=0;this.c=0;this.sr=0;this.sc=0;this.state='normal';this.params='';this.render()}resize(r,c){r=Math.max(12,Math.min(100,r));c=Math.max(40,Math.min(300,c));const o=this.lines;this.lines=Array.from({length:r},(_,y)=>Array.from({length:c},(_,x)=>(o[y]&&o[y][x])||' '));this.rows=r;this.cols=c;this.r=Math.min(this.r,r-1);this.c=Math.min(this.c,c-1);this.render()}feed(d){for(const ch of d)this.consume(ch);this.render()}consume(ch){if(this.state==='osc'){if(ch==='\x07')this.state='normal';else if(ch==='\x1b')this.state='oscEsc';return}if(this.state==='oscEsc'){this.state=ch==='\\'?'normal':'osc';return}if(this.state==='esc'){if(ch==='['){this.state='csi';this.params=''}else if(ch===']')this.state='osc';else if(ch==='7'){this.sr=this.r;this.sc=this.c;this.state='normal'}else if(ch==='8'){this.r=this.sr;this.c=this.sc;this.state='normal'}else{this.state='normal'}return}if(this.state==='csi'){if(ch>='@'&&ch<='~'){this.csi(ch,this.params);this.state='normal'}else this.params+=ch;return}if(ch==='\x1b'){this.state='esc';return}if(ch==='\r'){this.c=0;return}if(ch==='\n'){this.nl();return}if(ch==='\b'){this.c=Math.max(0,this.c-1);return}if(ch==='\t'){const n=Math.min(this.cols-1,Math.floor(this.c/4+1)*4);while(this.c<n)this.put(' ');return}if(ch<' '||ch==='\x7f')return;this.put(ch)}nums(p){const q=p.replace(/^\?/,'');return q===''?[]:q.split(';').map(x=>x===''?0:(parseInt(x,10)||0))}csi(f,p){const a=this.nums(p),n=(i,d=1)=>(a[i]===undefined||a[i]===0)?d:a[i];switch(f){case'A':this.r=Math.max(0,this.r-n(0));break;case'B':this.r=Math.min(this.rows-1,this.r+n(0));break;case'C':this.c=Math.min(this.cols-1,this.c+n(0));break;case'D':this.c=Math.max(0,this.c-n(0));break;case'G':this.c=Math.min(this.cols-1,n(0)-1);break;case'H':case'f':this.r=Math.min(this.rows-1,n(0)-1);this.c=Math.min(this.cols-1,n(1)-1);break;case'J':if((a[0]||0)>=2){this.lines.forEach(x=>x.fill(' '));this.r=0;this.c=0}break;case'K':{const m=a[0]||0;if(m===0)for(let x=this.c;x<this.cols;x++)this.lines[this.r][x]=' ';else if(m===1)for(let x=0;x<=this.c;x++)this.lines[this.r][x]=' ';else this.lines[this.r].fill(' ');break}case's':this.sr=this.r;this.sc=this.c;break;case'u':this.r=this.sr;this.c=this.sc;break;case'P':{const k=n(0);this.lines[this.r].splice(this.c,k);this.lines[this.r].push(...Array(k).fill(' '));break}case'@':{const k=n(0);this.lines[this.r].splice(this.c,0,...Array(k).fill(' '));this.lines[this.r].length=this.cols;break}case'X':for(let x=this.c;x<Math.min(this.cols,this.c+n(0));x++)this.lines[this.r][x]=' ';break}}clear(y,x){if(y<0||y>=this.rows||x<0||x>=this.cols)return;const l=this.lines[y];if(l[x]===''){l[x]=' ';if(x>0&&isWide(l[x-1]))l[x-1]=' ';return}if(isWide(l[x])&&x+1<this.cols&&l[x+1]==='')l[x+1]=' ';l[x]=' '}put(ch){const w=isWide(ch)?2:1;if(this.c>=this.cols||(w===2&&this.c===this.cols-1))this.nl();this.clear(this.r,this.c);if(w===2)this.clear(this.r,this.c+1);this.lines[this.r][this.c]=ch;if(w===2&&this.c+1<this.cols)this.lines[this.r][this.c+1]='';this.c=Math.min(this.cols,this.c+w)}nl(){this.c=0;this.r++;if(this.r>=this.rows){this.lines.shift();this.lines.push(Array(this.cols).fill(' '));this.r=this.rows-1}}render(){$('screen').textContent=this.lines.map(x=>x.join('').replace(/ +$/,'')).join('\n');const s=$('screen'),c=$('cursor'),i=$('terminalIme');const fs=parseFloat(getComputedStyle(s).fontSize)||13,lh=17.2,cw=fs*.61,left=12+this.c*cw,top=12+this.r*lh;c.style.left=left+'px';c.style.top=top+'px';c.style.height=(lh-1)+'px';c.style.display=terminalRunning?'block':'none';i.style.left=left+'px';i.style.top=top+'px'}text(){return this.lines.map(x=>x.join('').replace(/ +$/,'')).join('\n')}}const term=new MiniTerminal();
function fitTerminal(){const b=$('terminal').getBoundingClientRect();if(!b.width||!b.height)return;const r=Math.max(12,Math.floor((b.height-24)/17.2)),c=Math.max(40,Math.floor((b.width-24)/(13*.61)));term.resize(r,c);bridge?.resizeTerminal(r,c)}function ensureTerminal(){if(!terminalRunning)bridge.startDebugTerminal();setTimeout(fitTerminal,100)}function focusTerm(){$('terminalIme').focus()}function flushIme(){clearTimeout(imeTimer);if(imeComposing||!$('terminalIme').value)return;const t=$('terminalIme').value.replace(/\r\n/g,'\r').replace(/\n/g,'\r');$('terminalIme').value='';bridge.sendTerminalInput(t)}function terminalKey(e){if(!terminalRunning||imeComposing||e.isComposing||e.keyCode===229)return;let o=null;if(e.ctrlKey&&e.key.toLowerCase()==='c')o='\x03';else if(e.key==='Enter')o='\r';else if(e.key==='Backspace')o='\x7f';else if(e.key==='Tab')o='\t';else if(e.key==='Escape')o='\x1b';else if(e.key==='ArrowUp')o='\x1b[A';else if(e.key==='ArrowDown')o='\x1b[B';else if(e.key==='ArrowRight')o='\x1b[C';else if(e.key==='ArrowLeft')o='\x1b[D';if(o!==null){e.preventDefault();$('terminalIme').value='';bridge.sendTerminalInput(o)}}
function bind(){document.querySelectorAll('[data-close]').forEach(b=>b.onclick=()=>closeDrawers());$('backdrop').onclick=()=>closeDrawers();$('btnHistory').onclick=()=>openDrawer('historyDrawer');$('btnTimeline').onclick=()=>openDrawer('timelineDrawer');$('btnSettings').onclick=()=>openDrawer('settingsDrawer');$('btnNewChat').onclick=()=>bridge.newSession();$('historySearch').oninput=()=>renderHistory(histories);$('projectChip').onclick=chooseProject;$('btnBrowse').onclick=chooseProject;$('btnOpenProject').onclick=()=>bridge.openPath($('projectPath').value.trim());$('btnOpenLogs').onclick=()=>bridge.openPath(runLogFolder);$('btnRun').onclick=()=>{if(taskRunning)bridge.stopCurrentTask();else if(ready)bridge.resetReady();else{saveSettings();bridge.startCodex()}};$('btnAttach').onclick=()=>bridge.chooseFiles();$('btnSend').onclick=sendPrompt;$('promptInput').oninput=autoResize;$('promptInput').onpaste=pasteImages;$('promptInput').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();sendPrompt()}};$('btnConsole').onclick=()=>toggleConsole();$('btnCloseConsole').onclick=()=>toggleConsole(false);$('btnClearConsole').onclick=()=>{jsonText='';$('jsonLog').textContent='';term.reset()};$('tabJson').onclick=()=>showTab('json');$('tabTerminal').onclick=()=>showTab('terminal');$('lightboxClose').onclick=()=>$('lightbox').classList.remove('show');$('lightbox').onclick=e=>{if(e.target===$('lightbox'))$('lightbox').classList.remove('show')};$('terminal').onclick=focusTerm;$('terminalIme').onkeydown=terminalKey;$('terminalIme').addEventListener('compositionstart',()=>imeComposing=true);$('terminalIme').addEventListener('compositionend',()=>{imeComposing=false;imeTimer=setTimeout(flushIme,0)});$('terminalIme').oninput=()=>{if(!imeComposing)imeTimer=setTimeout(flushIme,0)};$('btnTerminalSend').onclick=sendTerminal;$('terminalInput').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();sendTerminal()}};window.onresize=()=>setTimeout(fitTerminal,120);for(const id of ['projectPath','codexCommand','model','sandbox','approval','skipGit','autoStart','autoArtifacts','clearAttachments','includeContext','contextLimit','extraArgs','powershellPath'])$(id).addEventListener('change',saveSettings);$('projectPath').addEventListener('input',updateProject)}
function chooseProject(){bridge.chooseProjectFolder(v=>{if(v){$('projectPath').value=v;updateProject();saveSettings();toast('已選擇：'+baseName(v))}})}function showTab(which){const t=which==='terminal';$('tabJson').classList.toggle('active',!t);$('tabTerminal').classList.toggle('active',t);$('jsonPanel').classList.toggle('active',!t);$('terminalPanel').classList.toggle('active',t);if(t)ensureTerminal()}function sendTerminal(){const v=$('terminalInput').value;if(!v)return;bridge.sendTerminalLine(v);$('terminalInput').value=''}
let runLogFolder='';
new QWebChannel(qt.webChannelTransport,ch=>{bridge=ch.objects.bridge;bridge.statusChanged.connect(setStatus);bridge.readyChanged.connect(v=>setReady(v));bridge.taskRunningChanged.connect(v=>setTaskRunning(v));bridge.chatEvent.connect(handleChat);bridge.jsonEventReceived.connect(appendJson);bridge.terminalOutputReceived.connect(d=>term.feed(d));bridge.terminalRunningChanged.connect(v=>{terminalRunning=!!v;term.render()});bridge.historyChanged.connect(raw=>{try{histories=JSON.parse(raw||'[]');renderHistory(histories)}catch{}});bridge.sessionLoaded.connect(raw=>{try{renderSession(JSON.parse(raw));closeDrawers()}catch{}});bridge.eventAdded.connect(raw=>{try{const e=JSON.parse(raw);if(e.session_id===currentSessionId){currentSession.events=currentSession.events||[];currentSession.events.push(e);addTimeline(e)}}catch{}});bridge.attachmentsChanged.connect(raw=>{try{attachments=JSON.parse(raw||'[]');renderAttachments()}catch{}});bridge.settingsChanged.connect(raw=>{try{const s=JSON.parse(raw);$('projectPath').value=s['專案路徑']||$('projectPath').value;updateProject()}catch{}});bridge.getInitialState(raw=>{const s=JSON.parse(raw);$('version').textContent=s.version;$('projectPath').value=s['專案路徑']||'';$('codexCommand').value=s['Codex命令']||'codex';$('model').value=s['模型']||'';$('sandbox').value=s['沙盒模式']||'workspace-write';$('approval').value=s['批准模式']||'never';$('skipGit').checked=s['跳過Git檢查']!==false;$('autoStart').checked=!!s['自動啟動'];$('autoArtifacts').checked=s['自動偵測成果']!==false;$('clearAttachments').checked=s['送出後清空附件']!==false;$('includeContext').checked=s['帶入最近對話']!==false;$('contextLimit').value=s['對話內容上限']||14000;$('extraArgs').value=s['額外參數']||'';$('powershellPath').value=s['PowerShell路徑']||'';$('configPath').textContent=s.configPath||'';$('databasePath').textContent=s.databasePath||'';runLogFolder=s.runLogFolder||'';histories=s.histories||[];attachments=s.attachments||[];ready=!!s.ready;taskRunning=!!s.taskRunning;updateProject();renderAttachments();setReady(ready);setTaskRunning(taskRunning);bind();renderSession(s.currentSession||{});if(ready)setStatus('Codex 已就緒','ready');else setStatus('尚未啟動','idle');if(s['自動啟動']&&!ready&&$('projectPath').value)setTimeout(()=>bridge.startCodex(),650)});});
</script>
</body>
</html>
"""


class 主視窗(QWebEngineView):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{應用程式名稱} v{應用程式版本}")
        self.resize(1480, 930)
        self.setMinimumSize(1000, 700)
        if QWebEngineSettings is not None:
            try:
                self.settings().setAttribute(QWebEngineSettings.WebAttribute.LocalContentCanAccessFileUrls, True)
                self.settings().setAttribute(QWebEngineSettings.WebAttribute.PlaybackRequiresUserGesture, False)
            except Exception:
                pass
        self.橋接 = 工作站控制器(self)
        self.通道 = QWebChannel(self.page())
        self.通道.registerObject("bridge", self.橋接)
        self.page().setWebChannel(self.通道)
        self.setHtml(HTML, QUrl("qrc:///"))

    def closeEvent(self, 事件) -> None:  # type: ignore[override]
        self.橋接.關閉()
        super().closeEvent(事件)


def 顯示依賴錯誤() -> None:
    QMessageBox.critical(
        None,
        "缺少 pywinpty",
        "聊天 JSON 引擎仍可使用，但進階 PowerShell 終端需要 pywinpty。\n\n"
        "請執行：pip install pywinpty",
    )


def main() -> int:
    if sys.platform != "win32":
        print("此工具目前只支援 Windows 10/11。")
        return 1
    os.environ.setdefault("QTWEBENGINE_CHROMIUM_FLAGS", "--disable-features=TranslateUI")
    應用 = QApplication(sys.argv)
    應用.setApplicationName(應用程式名稱)
    應用.setOrganizationName("SimonTools")
    視窗 = 主視窗()
    視窗.show()
    if PtyProcess is None:
        QTimer.singleShot(900, 顯示依賴錯誤)
    return 應用.exec()


if __name__ == "__main__":
    raise SystemExit(main())
