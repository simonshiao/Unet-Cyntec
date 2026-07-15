# -*- coding: utf-8 -*-
"""
Codex Workspace v0.5.2｜JSON 引擎＋終端自動啟動版
================================================

核心設計：
- 聊天任務改用 `codex exec --json`，只解析 JSONL 事件，不再解析 Codex TUI 畫面。
- 不會再把 Booting MCP / Working / Reconnecting / 游標重畫碎片塞進聊天室。
- 每次任務只建立一個 Codex 回覆區，turn.completed 後才標記完成。
- 支援 SQLite 對話履歷、檔案上傳、Ctrl+V 貼圖、圖片／影片／音訊成果卡。
- 保留獨立 PowerShell ConPTY 終端，採 v0.2.3 的修正版終端模型：
  自動折行、捲動區、替代畫面、CJK 寬字、組合字元與常用 CSI。

執行環境：Windows 10/11、Python 3.10+（建議 3.12）
依賴：PySide6、pywinpty
"""
from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import shutil
import sqlite3
import sys
import threading
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional

from PySide6.QtCore import QObject, QProcess, QStandardPaths, QTimer, QUrl, Signal, Slot
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
應用程式版本 = "0.5.2"


@dataclass
class 應用設定:
    專案路徑: str = ""
    Codex命令: str = "codex"
    PowerShell路徑: str = r"C:\Windows\System32\WindowsPowerShell\v1.0\powershell.exe"
    沙盒模式: str = "workspace-write"
    批准模式: str = "never"
    略過Git檢查: bool = True
    暫時工作階段: bool = False
    啟動時自動就緒: bool = False
    送出後清空附件: bool = True
    自動偵測成果: bool = True
    終端列數: int = 42
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
    # 使用新資料庫，避免 v0.4.x 被 TUI 雜訊污染的舊訊息繼續顯示。
    return 取得資料資料夾() / "workspace_json_v051.sqlite3"


def 取得附件資料夾() -> Path:
    路徑 = 取得資料資料夾() / "attachments"
    路徑.mkdir(parents=True, exist_ok=True)
    return 路徑


def 取得提示詞資料夾() -> Path:
    路徑 = 取得資料資料夾() / "prompts"
    路徑.mkdir(parents=True, exist_ok=True)
    return 路徑


def 載入設定() -> 應用設定:
    路徑 = 取得設定檔路徑()
    if not 路徑.exists():
        return 應用設定()
    try:
        資料 = json.loads(路徑.read_text(encoding="utf-8"))
        預設 = asdict(應用設定())
        預設.update({鍵: 值 for 鍵, 值 in 資料.items() if 鍵 in 預設})
        return 應用設定(**預設)
    except Exception:
        return 應用設定()


def 儲存設定(設定: 應用設定) -> None:
    取得設定檔路徑().write_text(
        json.dumps(asdict(設定), ensure_ascii=False, indent=2), encoding="utf-8"
    )


def 簡化空白(文字: str) -> str:
    return " ".join(str(文字 or "").strip().split())


def 安全標題(文字: str, 最大長度: int = 38) -> str:
    文字 = 簡化空白(文字)
    if not 文字:
        return "新對話"
    return 文字[:最大長度] + ("…" if len(文字) > 最大長度 else "")


def PowerShell單引號(文字: str) -> str:
    return "'" + 文字.replace("'", "''") + "'"


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
                    level TEXT NOT NULL,
                    text TEXT NOT NULL,
                    data_json TEXT NOT NULL DEFAULT '{}',
                    created_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_events_session
                ON events(session_id, created_at, id);
                """
            )
            self.連線.commit()

    @staticmethod
    def _讀JSON(文字: str) -> Any:
        try:
            return json.loads(文字 or "{}")
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
            列 = self.連線.execute("SELECT 1 FROM sessions WHERE id=?", (對話id,)).fetchone()
        return bool(列)

    def 更新對話(self, 對話id: str, *, 標題: Optional[str] = None, 專案路徑: Optional[str] = None) -> None:
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

    def 對話訊息數(self, 對話id: str, 角色: Optional[str] = None) -> int:
        with self.鎖:
            if 角色:
                列 = self.連線.execute(
                    "SELECT COUNT(*) n FROM messages WHERE session_id=? AND role=?",
                    (對話id, 角色),
                ).fetchone()
            else:
                列 = self.連線.execute(
                    "SELECT COUNT(*) n FROM messages WHERE session_id=?", (對話id,)
                ).fetchone()
        return int(列["n"] if 列 else 0)

    def 加入訊息(self, 對話id: str, 角色: str, 內容: str, 種類: str = "text", 資料: Any = None) -> int:
        現在 = time.time()
        with self.鎖:
            游標 = self.連線.execute(
                "INSERT INTO messages(session_id,role,kind,content,data_json,created_at) VALUES(?,?,?,?,?,?)",
                (對話id, 角色, 種類, 內容, json.dumps(資料 or {}, ensure_ascii=False), 現在),
            )
            self.連線.execute("UPDATE sessions SET updated_at=? WHERE id=?", (現在, 對話id))
            self.連線.commit()
            return int(游標.lastrowid)

    def 更新訊息(self, 訊息id: int, 內容: str, 資料: Any = None) -> None:
        with self.鎖:
            self.連線.execute(
                "UPDATE messages SET content=?,data_json=? WHERE id=?",
                (內容, json.dumps(資料 or {}, ensure_ascii=False), 訊息id),
            )
            self.連線.commit()

    def 加入事件(self, 對話id: str, 文字: str, 等級: str = "info", 資料: Any = None) -> dict[str, Any]:
        現在 = time.time()
        with self.鎖:
            游標 = self.連線.execute(
                "INSERT INTO events(session_id,level,text,data_json,created_at) VALUES(?,?,?,?,?)",
                (對話id, 等級, 文字, json.dumps(資料 or {}, ensure_ascii=False), 現在),
            )
            self.連線.commit()
            return {
                "id": int(游標.lastrowid), "session_id": 對話id, "level": 等級,
                "text": 文字, "data": 資料 or {}, "created_at": 現在,
            }

    def 讀取對話(self, 對話id: str) -> dict[str, Any]:
        with self.鎖:
            對話 = self.連線.execute("SELECT * FROM sessions WHERE id=?", (對話id,)).fetchone()
            訊息列 = self.連線.execute(
                "SELECT * FROM messages WHERE session_id=? ORDER BY created_at,id", (對話id,)
            ).fetchall()
            事件列 = self.連線.execute(
                "SELECT * FROM events WHERE session_id=? ORDER BY created_at,id", (對話id,)
            ).fetchall()
        if not 對話:
            return {}
        訊息 = []
        for 列 in 訊息列:
            項目 = dict(列)
            項目["data"] = self._讀JSON(項目.pop("data_json", "{}"))
            訊息.append(項目)
        事件 = []
        for 列 in 事件列:
            項目 = dict(列)
            項目["data"] = self._讀JSON(項目.pop("data_json", "{}"))
            事件.append(項目)
        return {"session": dict(對話), "messages": 訊息, "events": 事件}

    def 最近文字訊息(self, 對話id: str, 上限: int = 12) -> list[dict[str, Any]]:
        with self.鎖:
            列表 = self.連線.execute(
                """
                SELECT role,content FROM messages
                WHERE session_id=? AND kind='text' AND TRIM(content)<>''
                ORDER BY created_at DESC,id DESC LIMIT ?
                """,
                (對話id, 上限),
            ).fetchall()
        return [dict(x) for x in reversed(列表)]

    def 列出對話(self, 上限: int = 120) -> list[dict[str, Any]]:
        with self.鎖:
            列表 = self.連線.execute(
                """
                SELECT s.*,(SELECT COUNT(*) FROM messages m WHERE m.session_id=s.id) message_count
                FROM sessions s ORDER BY s.updated_at DESC LIMIT ?
                """,
                (上限,),
            ).fetchall()
        return [dict(x) for x in 列表]

    def 刪除對話(self, 對話id: str) -> None:
        with self.鎖:
            self.連線.execute("DELETE FROM messages WHERE session_id=?", (對話id,))
            self.連線.execute("DELETE FROM events WHERE session_id=?", (對話id,))
            self.連線.execute("DELETE FROM sessions WHERE id=?", (對話id,))
            self.連線.commit()


class 工作站控制器(QObject):
    statusChanged = Signal(str, str)
    readyChanged = Signal(bool)
    taskRunningChanged = Signal(bool)
    chatEvent = Signal(str)
    eventAdded = Signal(str)
    historyChanged = Signal(str)
    sessionLoaded = Signal(str)
    attachmentsChanged = Signal(str)
    settingsChanged = Signal(str)
    shellOutputReceived = Signal(str)
    shellRunningChanged = Signal(bool)

    圖片副檔名 = {".png", ".jpg", ".jpeg", ".webp", ".gif", ".bmp", ".tif", ".tiff"}
    影片副檔名 = {".mp4", ".webm", ".mov", ".m4v", ".avi", ".mkv"}
    音訊副檔名 = {".mp3", ".wav", ".ogg", ".m4a", ".aac", ".flac", ".opus"}
    其他成果副檔名 = {".zip", ".rar", ".7z", ".json", ".js", ".py", ".txt", ".html", ".csv", ".pdf"}
    排除資料夾 = {".git", ".svn", "node_modules", ".venv", "venv", "env", "__pycache__", ".idea", ".vscode", "cache", "temp", "tmp"}

    def __init__(self, parent: Optional[QObject] = None) -> None:
        super().__init__(parent)
        self.設定 = 載入設定()
        self.資料庫 = 對話資料庫(取得資料庫路徑())
        self.附件資料夾 = 取得附件資料夾()
        self.附件列表: list[附件項目] = []
        最新 = self.資料庫.最新對話id()
        self.目前對話id = 最新 or self.資料庫.建立對話(self.設定.專案路徑)

        self.已就緒 = False
        self.任務程序: Optional[QProcess] = None
        self.任務id = ""
        self.任務對話id = ""
        self.任務開始時間 = 0.0
        self.目前助理訊息id: Optional[int] = None
        self.目前助理文字 = ""
        self.標準輸出緩衝 = ""
        self.標準錯誤緩衝 = ""
        self.最後執行錯誤 = ""
        self.收到完成事件 = False
        self.最後執行緒id = ""
        self.提示詞暫存檔: Optional[Path] = None

        self.Shell程序: Optional[object] = None
        self.Shell讀取執行緒: Optional[threading.Thread] = None
        self.Shell停止事件 = threading.Event()
        self.Shell寫入鎖 = threading.Lock()
        self._Shell正在關閉 = False

    @staticmethod
    def _json(資料: Any) -> str:
        return json.dumps(資料, ensure_ascii=False)

    def _目前對話資料(self) -> dict[str, Any]:
        return self.資料庫.讀取對話(self.目前對話id)

    def _廣播歷史(self) -> None:
        self.historyChanged.emit(self._json(self.資料庫.列出對話()))

    def _新增事件(self, 文字: str, 等級: str = "info", 對話id: Optional[str] = None, 資料: Any = None) -> None:
        事件 = self.資料庫.加入事件(對話id or self.目前對話id, 文字, 等級, 資料)
        self.eventAdded.emit(self._json(事件))

    @Slot(result=str)
    def getInitialState(self) -> str:
        資料 = asdict(self.設定)
        資料.update({
            "version": 應用程式版本,
            "ready": self.已就緒,
            "taskRunning": self.任務程序 is not None,
            "shellRunning": self.Shell是否執行中(),
            "pywinptyAvailable": PtyProcess is not None,
            "configPath": str(取得設定檔路徑()),
            "databasePath": str(取得資料庫路徑()),
            "attachmentFolder": str(self.附件資料夾),
            "histories": self.資料庫.列出對話(),
            "currentSession": self._目前對話資料(),
            "attachments": [asdict(x) for x in self.附件列表],
        })
        return self._json(資料)

    @Slot(str)
    def saveSettings(self, 原始: str) -> None:
        try:
            資料 = json.loads(原始)
            self.設定.專案路徑 = str(資料.get("projectPath", self.設定.專案路徑)).strip()
            self.設定.Codex命令 = str(資料.get("codexCommand", self.設定.Codex命令)).strip() or "codex"
            self.設定.PowerShell路徑 = str(資料.get("powershellPath", self.設定.PowerShell路徑)).strip() or self.設定.PowerShell路徑
            沙盒 = str(資料.get("sandboxMode", self.設定.沙盒模式))
            self.設定.沙盒模式 = 沙盒 if 沙盒 in {"read-only", "workspace-write", "danger-full-access"} else "workspace-write"
            批准 = str(資料.get("approvalMode", self.設定.批准模式))
            self.設定.批准模式 = 批准 if 批准 in {"never", "on-request", "on-failure", "untrusted"} else "never"
            self.設定.略過Git檢查 = bool(資料.get("skipGitCheck", self.設定.略過Git檢查))
            self.設定.暫時工作階段 = bool(資料.get("ephemeral", self.設定.暫時工作階段))
            self.設定.啟動時自動就緒 = bool(資料.get("autoReady", self.設定.啟動時自動就緒))
            self.設定.送出後清空附件 = bool(資料.get("clearAttachmentsAfterSend", self.設定.送出後清空附件))
            self.設定.自動偵測成果 = bool(資料.get("autoDetectArtifacts", self.設定.自動偵測成果))
            儲存設定(self.設定)
            self.settingsChanged.emit(self._json(asdict(self.設定)))
        except Exception as 例外:
            self._新增事件(f"設定儲存失敗：{例外}", "error")

    @Slot(result=str)
    def chooseProjectFolder(self) -> str:
        起始 = self.設定.專案路徑 if Path(self.設定.專案路徑).is_dir() else str(Path.home())
        路徑 = QFileDialog.getExistingDirectory(None, "選擇 Codex 專案資料夾", 起始)
        if 路徑:
            self.設定.專案路徑 = 路徑
            儲存設定(self.設定)
            self.資料庫.更新對話(self.目前對話id, 專案路徑=路徑)
        return 路徑

    @Slot(str)
    def openPath(self, 路徑: str) -> None:
        try:
            if not Path(路徑).exists():
                raise FileNotFoundError(路徑)
            os.startfile(路徑)  # type: ignore[attr-defined]
        except Exception as 例外:
            self._新增事件(f"無法開啟：{例外}", "error")

    @Slot(str)
    def openContainingFolder(self, 路徑: str) -> None:
        try:
            物件 = Path(路徑)
            目標 = 物件 if 物件.is_dir() else 物件.parent
            os.startfile(str(目標))  # type: ignore[attr-defined]
        except Exception as 例外:
            self._新增事件(f"無法開啟所在資料夾：{例外}", "error")

    @Slot()
    def markReady(self) -> None:
        專案 = Path(self.設定.專案路徑.strip() or str(Path.home()))
        if not 專案.is_dir():
            self.statusChanged.emit("專案路徑不存在", "error")
            self._新增事件(f"專案路徑不存在：{專案}", "error")
            return
        命令 = self._解析Codex命令()
        if not 命令:
            self.statusChanged.emit("找不到 Codex CLI", "error")
            self._新增事件("找不到 Codex CLI。請先在 PowerShell 確認 `codex --version` 可執行。", "error")
            return
        self.已就緒 = True
        self.readyChanged.emit(True)
        self.statusChanged.emit("Codex 已就緒", "running")
        self._新增事件(f"JSON 引擎已就緒：{命令}", "success")

    @Slot()
    def markNotReady(self) -> None:
        if self.任務程序 is not None:
            self.stopTask()
        self.已就緒 = False
        self.readyChanged.emit(False)
        self.statusChanged.emit("尚未啟動", "idle")

    def _解析Codex命令(self) -> str:
        原始 = self.設定.Codex命令.strip() or "codex"
        候選 = shutil.which(原始)
        if 候選:
            return 候選
        路徑 = Path(原始)
        if 路徑.exists():
            return str(路徑)
        return ""

    def _廣播附件(self) -> None:
        self.attachmentsChanged.emit(self._json([asdict(x) for x in self.附件列表]))

    def _建立附件(self, 路徑: Path) -> 附件項目:
        mime = mimetypes.guess_type(str(路徑))[0] or "application/octet-stream"
        圖片 = mime.startswith("image/")
        預覽 = QUrl.fromLocalFile(str(路徑)).toString() if 圖片 else ""
        return 附件項目(uuid.uuid4().hex, 路徑.name, str(路徑), mime, 路徑.stat().st_size, 圖片, 預覽)

    @Slot(result=str)
    def chooseFiles(self) -> str:
        檔案, _ = QFileDialog.getOpenFileNames(None, "選擇附件", str(Path.home()), "所有檔案 (*.*)")
        for 名稱 in 檔案:
            路徑 = Path(名稱)
            if 路徑.exists() and not any(x.path == str(路徑) for x in self.附件列表):
                self.附件列表.append(self._建立附件(路徑))
        self._廣播附件()
        return self._json([asdict(x) for x in self.附件列表])

    @Slot(str, result=str)
    def savePastedImage(self, dataUrl: str) -> str:
        try:
            比對 = re.match(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.+)$", dataUrl, re.DOTALL)
            if not 比對:
                raise ValueError("不是有效的圖片資料")
            mime = 比對.group(1).lower()
            副檔名 = {"image/jpeg": ".jpg", "image/webp": ".webp", "image/gif": ".gif"}.get(mime, ".png")
            原始資料 = base64.b64decode(比對.group(2), validate=False)
            if len(原始資料) > 80 * 1024 * 1024:
                raise ValueError("圖片超過 80 MB")
            子資料夾 = self.附件資料夾 / time.strftime("%Y-%m")
            子資料夾.mkdir(parents=True, exist_ok=True)
            路徑 = 子資料夾 / f"貼上圖片_{time.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:6]}{副檔名}"
            路徑.write_bytes(原始資料)
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

    def _組合提示詞(self, 使用者內容: str) -> str:
        上下文 = self.資料庫.最近文字訊息(self.目前對話id, 10)
        行 = [
            "你正在透過 Codex Workspace 的 JSON 引擎處理本機專案。",
            "請直接完成目前需求；需要修改檔案時可在工作區內修改。",
            "最後請用繁體中文清楚說明：完成了什麼、修改哪些檔案、是否有需要注意的事項。",
        ]
        if 上下文:
            行.extend(["", "以下是本對話最近上下文，僅供理解連續需求："])
            for 訊息 in 上下文:
                角色 = "使用者" if 訊息["role"] == "user" else "Codex"
                內容 = str(訊息["content"]).strip()
                if 內容:
                    行.append(f"[{角色}] {內容[:4000]}")
        if self.附件列表:
            行.extend(["", "請一併讀取以下本機附件："])
            for 索引, 附件 in enumerate(self.附件列表, 1):
                行.append(f"[附件 {索引}] {附件.path}")
        行.extend(["", "目前需求：", 使用者內容.strip()])
        return "\n".join(行)

    @Slot(str, result=bool)
    def sendChatMessage(self, 原始: str) -> bool:
        try:
            if not self.已就緒:
                self._新增事件("請先按右上角「啟動」。", "warning")
                return False
            if self.任務程序 is not None:
                self._新增事件("上一個任務仍在執行。", "warning")
                return False
            資料 = json.loads(原始)
            文字 = str(資料.get("text", "")).strip()
            if not 文字 and not self.附件列表:
                return False
            顯示文字 = 文字 or "請分析這些附件。"
            附件快照 = [asdict(x) for x in self.附件列表]
            最終提示詞 = self._組合提示詞(顯示文字)
            對話id = self.目前對話id
            if self.資料庫.對話訊息數(對話id, "user") == 0:
                self.資料庫.更新對話(對話id, 標題=安全標題(顯示文字), 專案路徑=self.設定.專案路徑)
            使用者訊息id = self.資料庫.加入訊息(對話id, "user", 顯示文字, "text", {"attachments": 附件快照})

            self.任務id = uuid.uuid4().hex
            self.任務對話id = 對話id
            self.任務開始時間 = time.time()
            self.目前助理訊息id = None
            self.目前助理文字 = ""
            self.標準輸出緩衝 = ""
            self.標準錯誤緩衝 = ""
            self.最後執行錯誤 = ""
            self.收到完成事件 = False
            self.最後執行緒id = ""

            self.chatEvent.emit(self._json({
                "type": "task_start", "sessionId": 對話id, "taskId": self.任務id,
                "userMessageId": 使用者訊息id, "text": 顯示文字,
                "attachments": 附件快照, "createdAt": time.time(),
            }))
            self.taskRunningChanged.emit(True)
            self.statusChanged.emit("Codex 工作中…", "working")
            self._新增事件("已啟動 JSON 任務。", "task", 對話id)
            self._廣播歷史()

            if not self._啟動JSON任務(最終提示詞):
                self._完成任務(False, "無法啟動 Codex JSON 任務。")
                return False
            if self.設定.送出後清空附件:
                QTimer.singleShot(200, self.clearAttachments)
            return True
        except Exception as 例外:
            self._新增事件(f"送出聊天訊息失敗：{例外}", "error")
            return False

    def _啟動JSON任務(self, 提示詞: str) -> bool:
        Codex = self._解析Codex命令()
        專案 = Path(self.設定.專案路徑.strip() or str(Path.home()))
        if not Codex or not 專案.is_dir():
            return False
        提示檔 = 取得提示詞資料夾() / f"task_{self.任務id}.txt"
        提示檔.write_text(提示詞, encoding="utf-8")
        self.提示詞暫存檔 = 提示檔

        參數 = ["--ask-for-approval", self.設定.批准模式, "exec", "--json", "--sandbox", self.設定.沙盒模式]
        if self.設定.略過Git檢查:
            參數.append("--skip-git-repo-check")
        if self.設定.暫時工作階段:
            參數.append("--ephemeral")
        參數.append("-")
        參數文字 = " ".join(PowerShell單引號(x) for x in 參數)
        腳本 = (
            "$ErrorActionPreference='Continue';"
            "$OutputEncoding=[System.Text.UTF8Encoding]::new($false);"
            "[Console]::OutputEncoding=$OutputEncoding;"
            "[Console]::InputEncoding=$OutputEncoding;"
            "$env:NO_COLOR='1';"
            f"Get-Content -Raw -Encoding UTF8 -LiteralPath {PowerShell單引號(str(提示檔))} | "
            f"& {PowerShell單引號(Codex)} {參數文字};"
            "exit $LASTEXITCODE"
        )
        PowerShell = self.設定.PowerShell路徑
        if not Path(PowerShell).exists():
            PowerShell = shutil.which("powershell.exe") or shutil.which("pwsh.exe") or ""
        if not PowerShell:
            return False

        程序 = QProcess(self)
        程序.setWorkingDirectory(str(專案))
        程序.setProgram(PowerShell)
        程序.setArguments(["-NoLogo", "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", 腳本])
        程序.setProcessChannelMode(QProcess.ProcessChannelMode.SeparateChannels)
        程序.readyReadStandardOutput.connect(self._讀取JSON標準輸出)
        程序.readyReadStandardError.connect(self._讀取JSON標準錯誤)
        程序.finished.connect(self._JSON程序結束)
        程序.errorOccurred.connect(self._JSON程序錯誤)
        self.任務程序 = 程序
        程序.start()
        if not 程序.waitForStarted(5000):
            self.最後執行錯誤 = 程序.errorString()
            self.任務程序 = None
            return False
        return True

    def _讀取JSON標準輸出(self) -> None:
        if not self.任務程序:
            return
        文字 = bytes(self.任務程序.readAllStandardOutput()).decode("utf-8", errors="replace")
        self.標準輸出緩衝 += 文字
        while "\n" in self.標準輸出緩衝:
            行, self.標準輸出緩衝 = self.標準輸出緩衝.split("\n", 1)
            self._處理JSON行(行.strip())

    def _讀取JSON標準錯誤(self) -> None:
        if not self.任務程序:
            return
        文字 = bytes(self.任務程序.readAllStandardError()).decode("utf-8", errors="replace")
        self.標準錯誤緩衝 += 文字
        # stderr 只進履歷，不直接進聊天室。
        for 行 in 文字.replace("\r", "\n").splitlines():
            行 = 行.strip()
            if 行 and not re.search(r"(?i)working|reconnecting|booting mcp", 行):
                self._新增事件(行[:1500], "debug", self.任務對話id)

    def _JSON程序錯誤(self, _錯誤) -> None:
        if self.任務程序:
            self.最後執行錯誤 = self.任務程序.errorString()

    def _處理JSON行(self, 行: str) -> None:
        if not 行:
            return
        try:
            事件 = json.loads(行)
        except Exception:
            self._新增事件(f"非 JSON 輸出：{行[:1000]}", "debug", self.任務對話id)
            return
        類型 = str(事件.get("type", ""))
        if 類型 == "thread.started":
            self.最後執行緒id = str(事件.get("thread_id", ""))
            self._進度("Codex 已建立工作執行緒")
            return
        if 類型 == "turn.started":
            self._進度("正在分析你的需求…")
            return
        if 類型.startswith("item."):
            self._處理項目事件(類型, 事件.get("item") or {})
            return
        if 類型 == "turn.completed":
            self.收到完成事件 = True
            用量 = 事件.get("usage") or {}
            self._新增事件("Codex 任務完成。", "success", self.任務對話id, {"usage": 用量})
            return
        if 類型 == "turn.failed":
            self.最後執行錯誤 = self._擷取錯誤文字(事件) or "Codex 回報 turn.failed。"
            return
        if 類型 == "error":
            self.最後執行錯誤 = self._擷取錯誤文字(事件) or "Codex 回報未知錯誤。"
            self._新增事件(self.最後執行錯誤, "error", self.任務對話id)

    @staticmethod
    def _擷取錯誤文字(資料: Any) -> str:
        if isinstance(資料, str):
            return 資料
        if isinstance(資料, dict):
            for 鍵 in ("message", "error", "detail", "text"):
                值 = 資料.get(鍵)
                if isinstance(值, str) and 值.strip():
                    return 值.strip()
                if isinstance(值, dict):
                    結果 = 工作站控制器._擷取錯誤文字(值)
                    if 結果:
                        return 結果
        return ""

    def _處理項目事件(self, 事件類型: str, 項目: dict[str, Any]) -> None:
        種類 = str(項目.get("type", ""))
        完成 = 事件類型.endswith("completed")
        if 種類 == "agent_message":
            文字 = str(項目.get("text", "")).strip()
            if 文字:
                self.目前助理文字 = 文字
                self._更新助理訊息(文字)
            return
        if 種類 == "command_execution":
            命令 = 簡化空白(str(項目.get("command", "")))
            if 命令:
                self._進度(("已完成命令：" if 完成 else "正在執行：") + 命令[:220])
            return
        if 種類 in {"file_change", "file_changes"}:
            路徑 = 項目.get("path") or 項目.get("file_path") or 項目.get("changes") or ""
            self._進度("已更新專案檔案" + (f"：{路徑}" if isinstance(路徑, str) and 路徑 else ""))
            return
        if 種類 in {"mcp_tool_call", "tool_call"}:
            名稱 = 項目.get("name") or 項目.get("tool") or "工具"
            self._進度(("已完成工具：" if 完成 else "正在使用工具：") + str(名稱))
            return
        if 種類 == "web_search":
            查詢 = 項目.get("query") or ""
            self._進度("正在搜尋資料" + (f"：{查詢}" if 查詢 else ""))
            return
        if 種類 in {"plan", "plan_update"}:
            文字 = 項目.get("text") or 項目.get("plan") or "正在更新工作計畫"
            self._進度(str(文字)[:260])
            return
        if 種類 == "reasoning":
            摘要 = 項目.get("text") or 項目.get("summary") or ""
            if isinstance(摘要, str) and 摘要.strip():
                self._進度(簡化空白(摘要)[:260])

    def _進度(self, 文字: str) -> None:
        文字 = 簡化空白(文字)
        if not 文字:
            return
        self._新增事件(文字, "progress", self.任務對話id)
        self.chatEvent.emit(self._json({
            "type": "progress", "sessionId": self.任務對話id,
            "taskId": self.任務id, "text": 文字, "createdAt": time.time(),
        }))

    def _更新助理訊息(self, 文字: str) -> None:
        if self.目前助理訊息id is None:
            self.目前助理訊息id = self.資料庫.加入訊息(
                self.任務對話id, "assistant", 文字, "text",
                {"taskId": self.任務id, "streaming": True},
            )
        else:
            self.資料庫.更新訊息(
                self.目前助理訊息id, 文字,
                {"taskId": self.任務id, "streaming": True},
            )
        self.chatEvent.emit(self._json({
            "type": "assistant_update", "sessionId": self.任務對話id,
            "taskId": self.任務id, "messageId": self.目前助理訊息id,
            "content": 文字, "createdAt": time.time(),
        }))

    def _JSON程序結束(self, 退出碼: int, _狀態) -> None:
        if self.標準輸出緩衝.strip():
            self._處理JSON行(self.標準輸出緩衝.strip())
            self.標準輸出緩衝 = ""
        成功 = 退出碼 == 0 and not self.最後執行錯誤
        if 成功 and not self.收到完成事件:
            self._新增事件("程序正常結束，但沒有收到 turn.completed；已使用程序結束狀態完成任務。", "warning", self.任務對話id)
        錯誤 = self.最後執行錯誤
        if not 成功 and not 錯誤:
            錯誤 = f"Codex 程序結束，退出碼：{退出碼}"
        self._完成任務(成功, 錯誤)

    def _完成任務(self, 成功: bool, 錯誤: str = "") -> None:
        對話id = self.任務對話id
        任務id = self.任務id
        if not self.目前助理文字.strip():
            self.目前助理文字 = 錯誤.strip() if 錯誤.strip() else "工作已完成。"
            self._更新助理訊息(self.目前助理文字)
        elif 錯誤.strip():
            self.目前助理文字 += f"\n\n⚠ {錯誤.strip()}"
            self._更新助理訊息(self.目前助理文字)
        if self.目前助理訊息id is not None:
            self.資料庫.更新訊息(
                self.目前助理訊息id, self.目前助理文字,
                {"taskId": 任務id, "streaming": False, "success": 成功, "threadId": self.最後執行緒id},
            )
        self.chatEvent.emit(self._json({
            "type": "assistant_finish", "sessionId": 對話id, "taskId": 任務id,
            "messageId": self.目前助理訊息id, "content": self.目前助理文字,
            "success": 成功, "createdAt": time.time(),
        }))
        self.taskRunningChanged.emit(False)
        self.statusChanged.emit("Codex 已就緒" if self.已就緒 else "尚未啟動", "running" if self.已就緒 else "idle")
        self.任務程序 = None
        if self.提示詞暫存檔:
            try:
                self.提示詞暫存檔.unlink(missing_ok=True)
            except Exception:
                pass
            self.提示詞暫存檔 = None
        if self.設定.自動偵測成果:
            threading.Thread(target=self._掃描成果, args=(對話id, 任務id, self.任務開始時間), daemon=True).start()
        self._廣播歷史()

    @Slot()
    def stopTask(self) -> None:
        程序 = self.任務程序
        if not 程序:
            return
        self.最後執行錯誤 = "工作已由使用者停止。"
        try:
            程序.kill()
        except Exception:
            pass

    def _掃描成果(self, 對話id: str, 任務id: str, 開始時間: float) -> None:
        time.sleep(0.8)
        根 = Path(self.設定.專案路徑)
        if not 根.is_dir():
            return
        副檔名集合 = self.圖片副檔名 | self.影片副檔名 | self.音訊副檔名 | self.其他成果副檔名
        結果: list[Path] = []
        try:
            for 目前, 資料夾, 檔案 in os.walk(根):
                資料夾[:] = [d for d in 資料夾 if d.lower() not in self.排除資料夾]
                for 名稱 in 檔案:
                    路徑 = Path(目前) / 名稱
                    if 路徑.suffix.lower() not in 副檔名集合:
                        continue
                    try:
                        if 路徑.stat().st_mtime >= 開始時間 - 1.5:
                            結果.append(路徑)
                    except OSError:
                        pass
            結果.sort(key=lambda p: p.stat().st_mtime, reverse=True)
        except Exception as 例外:
            self._新增事件(f"成果掃描失敗：{例外}", "warning", 對話id)
            return
        for 路徑 in 結果[:40]:
            self._加入成果(對話id, 任務id, 路徑)

    def _加入成果(self, 對話id: str, 任務id: str, 路徑: Path) -> None:
        副檔名 = 路徑.suffix.lower()
        if 副檔名 in self.圖片副檔名:
            種類 = "image"
        elif 副檔名 in self.影片副檔名:
            種類 = "video"
        elif 副檔名 in self.音訊副檔名:
            種類 = "audio"
        else:
            種類 = "file"
        try:
            大小 = 路徑.stat().st_size
        except OSError:
            大小 = 0
        資料 = {
            "taskId": 任務id, "kind": 種類, "name": 路徑.name,
            "path": str(路徑), "fileUrl": QUrl.fromLocalFile(str(路徑)).toString(), "size": 大小,
        }
        訊息id = self.資料庫.加入訊息(對話id, "assistant", 路徑.name, "artifact", 資料)
        self.chatEvent.emit(self._json({
            "type": "artifact", "sessionId": 對話id, "taskId": 任務id,
            "messageId": 訊息id, "item": 資料, "createdAt": time.time(),
        }))

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
        self.資料庫.刪除對話(對話id)
        if 對話id == self.目前對話id:
            最新 = self.資料庫.最新對話id()
            self.目前對話id = 最新 or self.資料庫.建立對話(self.設定.專案路徑)
            self.sessionLoaded.emit(self._json(self._目前對話資料()))
        self._廣播歷史()

    # ------------------ 獨立穩定 PowerShell / ConPTY 終端 ------------------
    @Slot()
    def startShell(self) -> None:
        if self.Shell是否執行中():
            return
        if PtyProcess is None:
            self._新增事件("缺少 pywinpty，無法啟動進階終端。", "error")
            return
        專案 = Path(self.設定.專案路徑.strip() or str(Path.home()))
        if not 專案.is_dir():
            self._新增事件(f"專案路徑不存在：{專案}", "error")
            return
        PowerShell = Path(self.設定.PowerShell路徑)
        if not PowerShell.exists():
            找到 = shutil.which("powershell.exe") or shutil.which("pwsh.exe")
            if not 找到:
                self._新增事件("找不到 PowerShell。", "error")
                return
            PowerShell = Path(找到)
        try:
            環境 = os.environ.copy()
            環境["PYTHONIOENCODING"] = "utf-8"
            環境["COLORTERM"] = "truecolor"
            環境.setdefault("TERM", "xterm-256color")
            self.Shell停止事件.clear()
            self._Shell正在關閉 = False
            self.Shell程序 = PtyProcess.spawn(
                [str(PowerShell), "-NoLogo", "-NoProfile", "-ExecutionPolicy", "Bypass", "-NoExit"],
                cwd=str(專案), env=環境,
                dimensions=(self.設定.終端列數, self.設定.終端欄數),
            )
            self.Shell讀取執行緒 = threading.Thread(target=self._Shell讀取迴圈, daemon=True)
            self.Shell讀取執行緒.start()
            self.shellRunningChanged.emit(True)
            self.shellOutputReceived.emit("\r\n正在啟動 PowerShell...\r\n")
            啟動指令 = (
                "$OutputEncoding=New-Object System.Text.UTF8Encoding($false);"
                "[Console]::OutputEncoding=$OutputEncoding;"
                "[Console]::InputEncoding=$OutputEncoding;"
                "$Host.UI.RawUI.WindowTitle='Codex Workspace Terminal';"
                "Write-Host '';"
                "Write-Host 'Codex Workspace PowerShell 已啟動' -ForegroundColor Cyan;"
                "Write-Host ('工作目錄：' + (Get-Location).Path) -ForegroundColor DarkGray"
            )
            QTimer.singleShot(450, lambda: self._Shell安全寫入(啟動指令 + "\r"))
            self._新增事件("進階 PowerShell 終端已啟動。", "success")
        except Exception as 例外:
            self.Shell程序 = None
            self.shellRunningChanged.emit(False)
            self._新增事件(f"終端啟動失敗：{例外}", "error")

    @Slot()
    def stopShell(self) -> None:
        if not self.Shell程序:
            return
        self._Shell正在關閉 = True
        self.Shell停止事件.set()
        try:
            if hasattr(self.Shell程序, "close"):
                self.Shell程序.close(force=True)
        except Exception:
            pass
        self.Shell程序 = None
        self.shellRunningChanged.emit(False)
        self._Shell正在關閉 = False

    @Slot()
    def restartShell(self) -> None:
        self.stopShell()
        QTimer.singleShot(450, self.startShell)

    @Slot(str)
    def sendShellInput(self, 文字: str) -> None:
        self._Shell安全寫入(文字)

    @Slot(str)
    def sendShellLine(self, 文字: str) -> None:
        文字 = 文字.replace("\r\n", "\n").replace("\r", "\n")
        if not 文字:
            return
        貼上 = ("\x1b[200~" + 文字 + "\x1b[201~") if "\n" in 文字 else 文字
        if self._Shell安全寫入(貼上):
            QTimer.singleShot(120, lambda: self._Shell安全寫入("\r"))

    @Slot(int, int)
    def resizeShell(self, 列數: int, 欄數: int) -> None:
        列數 = max(12, min(100, int(列數)))
        欄數 = max(40, min(300, int(欄數)))
        self.設定.終端列數 = 列數
        self.設定.終端欄數 = 欄數
        try:
            if self.Shell程序 and self.Shell是否執行中() and hasattr(self.Shell程序, "setwinsize"):
                self.Shell程序.setwinsize(列數, 欄數)
        except Exception:
            pass

    def Shell是否執行中(self) -> bool:
        try:
            return bool(self.Shell程序 and self.Shell程序.isalive())
        except Exception:
            return False

    def _Shell安全寫入(self, 文字: str) -> bool:
        if not self.Shell是否執行中():
            return False
        try:
            with self.Shell寫入鎖:
                self.Shell程序.write(文字)
            return True
        except Exception:
            return False

    def _Shell讀取迴圈(self) -> None:
        while not self.Shell停止事件.is_set():
            程序 = self.Shell程序
            if not 程序:
                break
            try:
                片段 = 程序.read(8192)
                if 片段:
                    self.shellOutputReceived.emit(片段)
                else:
                    time.sleep(0.01)
            except EOFError:
                break
            except Exception:
                break
        if not self._Shell正在關閉:
            self.Shell程序 = None
            self.shellRunningChanged.emit(False)

    def 關閉(self) -> None:
        self.stopTask()
        self.stopShell()


HTML = r"""
<!doctype html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Codex Workspace</title>
<script src="qrc:///qtwebchannel/qwebchannel.js"></script>
<style>
:root{--bg:#f7f7f8;--panel:#fff;--line:#e4e4e7;--text:#18181b;--muted:#71717a;--soft:#f1f1f3;--dark:#161719;--green:#10a37f;--red:#d84a57;--blue:#3b82f6;--shadow:0 18px 60px rgba(0,0,0,.10)}
*{box-sizing:border-box}html,body{width:100%;height:100%;margin:0;overflow:hidden;background:var(--bg);color:var(--text);font-family:"Microsoft JhengHei UI","Segoe UI",sans-serif}button,input,select,textarea{font:inherit}button{cursor:pointer}
.app{height:100%;display:grid;grid-template-rows:72px 1fr}.top{display:flex;align-items:center;gap:12px;padding:0 18px;border-bottom:1px solid var(--line);background:rgba(255,255,255,.92)}
.iconbtn,.topbtn{border:1px solid var(--line);background:#fff;border-radius:13px;height:42px;padding:0 14px;color:var(--text)}.iconbtn{width:42px;padding:0;font-size:18px}.topbtn.primary{background:var(--dark);color:#fff;border-color:var(--dark)}.topbtn.stop{border-color:#f0c7cc;color:#b42335;background:#fff8f8}.brand{display:flex;align-items:center;gap:11px}.brandmark{width:42px;height:42px;border-radius:13px;display:grid;place-items:center;background:#151619;color:#fff;font-size:20px}.brand h1{margin:0;font-size:18px}.brand small{display:block;color:var(--muted);font-size:11px}.project{margin-left:8px;border:1px solid var(--line);border-radius:13px;padding:10px 14px;background:#fff}.spacer{flex:1}.status{display:flex;align-items:center;gap:7px;border:1px solid var(--line);border-radius:999px;padding:9px 13px;color:var(--muted);font-size:12px;background:#fff}.dot{width:8px;height:8px;border-radius:50%;background:#a1a1aa}.status.running .dot{background:var(--green);box-shadow:0 0 0 4px rgba(16,163,127,.12)}.status.working .dot{background:var(--blue);box-shadow:0 0 0 4px rgba(59,130,246,.12)}.status.error .dot{background:var(--red)}
.main{min-height:0;overflow:auto;padding:30px 20px 150px}.conversation{max-width:960px;margin:0 auto}.empty{min-height:60vh;display:grid;place-items:center;text-align:center}.empty-logo{width:58px;height:58px;border-radius:18px;background:#17181b;color:#fff;display:grid;place-items:center;margin:0 auto 18px;font-size:25px}.empty h2{margin:0 0 9px;font-size:28px}.empty p{color:var(--muted);line-height:1.8}.message{display:grid;grid-template-columns:38px minmax(0,1fr);gap:14px;margin:0 0 28px}.message.user{grid-template-columns:minmax(0,1fr) 38px}.message.user .avatar{grid-column:2}.message.user .body{grid-column:1;grid-row:1;text-align:right}.avatar{width:36px;height:36px;border-radius:11px;background:#17181b;color:#fff;display:grid;place-items:center;font-size:13px}.message.user .avatar{background:#e8e8eb;color:#333}.head{display:flex;align-items:center;gap:9px;height:30px}.message.user .head{justify-content:flex-end}.name{font-weight:700}.time{font-size:11px;color:#a1a1aa}.text{line-height:1.75;white-space:normal;word-break:break-word}.message.user .text{display:inline-block;background:#ececef;padding:10px 14px;border-radius:18px;text-align:left;white-space:pre-wrap}.text pre{background:#17181b;color:#e5e7eb;padding:14px;border-radius:12px;overflow:auto}.text code{font-family:Consolas,monospace;background:#eee;padding:2px 5px;border-radius:5px}.text pre code{background:transparent;padding:0}.task{margin-top:6px;border:1px solid var(--line);border-radius:14px;overflow:hidden;background:#fff}.taskhead{display:flex;align-items:center;gap:10px;padding:12px 14px}.spin{width:15px;height:15px;border:2px solid #d4d4d8;border-top-color:#3b82f6;border-radius:50%;animation:spin .8s linear infinite}.task.done .spin{animation:none;border:0}.task.done .spin:after{content:'✓';color:var(--green);font-weight:800}.tasktitle{font-size:13px;font-weight:700}.progress{padding:0 14px 12px;color:var(--muted);font-size:12px;display:grid;gap:6px}.task.done .progress{display:none}@keyframes spin{to{transform:rotate(360deg)}}
.composerWrap{position:fixed;left:0;right:0;bottom:0;padding:24px 20px 18px;background:linear-gradient(transparent,var(--bg) 24%);z-index:10}.composerBox{max-width:960px;margin:0 auto;background:#fff;border:1px solid #d9d9dd;border-radius:24px;box-shadow:0 8px 30px rgba(0,0,0,.08);padding:10px}.attachments{display:none;gap:8px;flex-wrap:wrap;padding:2px 4px 10px}.attachments.show{display:flex}.attach{display:flex;align-items:center;gap:9px;border:1px solid var(--line);border-radius:12px;padding:6px 8px;background:#fafafa;max-width:280px}.attach img{width:38px;height:38px;object-fit:cover;border-radius:8px}.attach .file{width:38px;height:38px;border-radius:8px;background:#e9e9ed;display:grid;place-items:center;font-size:10px;font-weight:700}.attachname{font-size:11px;white-space:nowrap;overflow:hidden;text-overflow:ellipsis;max-width:170px}.attach button{border:0;background:transparent}.composer{display:grid;grid-template-columns:42px 1fr 42px;gap:8px;align-items:end}.composer button{width:42px;height:42px;border-radius:50%;border:0;background:#f0f0f2}.composer .send{background:#17181b;color:#fff;font-size:18px}.composer textarea{border:0;outline:0;resize:none;min-height:42px;max-height:180px;padding:10px 4px;line-height:1.55;font-size:15px}.hint{text-align:center;font-size:10px;color:#a1a1aa;margin-top:7px}
.drawer{position:fixed;top:0;bottom:0;width:390px;background:#fff;z-index:31;box-shadow:var(--shadow);transition:.22s;display:grid;grid-template-rows:66px 1fr}.drawer.left{left:0;transform:translateX(-105%)}.drawer.right{right:0;transform:translateX(105%)}.drawer.show{transform:none}.drawerhead{display:flex;align-items:center;padding:0 16px;border-bottom:1px solid var(--line);font-weight:700}.drawerhead button{margin-left:auto;border:0;background:#f2f2f4;width:34px;height:34px;border-radius:10px}.drawerbody{overflow:auto;padding:15px}.backdrop{position:fixed;inset:0;background:rgba(0,0,0,.20);z-index:30;display:none}.backdrop.show{display:block}.section{padding:14px 0;border-bottom:1px solid var(--line)}.section h3{font-size:13px;margin:0 0 12px}.field{display:grid;gap:6px;margin:0 0 11px}.field label{font-size:11px;color:var(--muted)}.field input,.field select{height:40px;border:1px solid var(--line);border-radius:10px;padding:0 10px;background:#fff}.row{display:flex;gap:8px}.row>*{flex:1}.switchrow{display:flex;justify-content:space-between;gap:16px;align-items:center;padding:8px 0}.switchrow strong{font-size:12px}.switchrow small{display:block;color:var(--muted);font-size:10px;margin-top:3px}.historyitem{padding:10px 12px;border-radius:10px;position:relative}.historyitem:hover,.historyitem.active{background:#f1f1f3}.historytitle{font-size:12px;font-weight:700;padding-right:30px}.historymeta{font-size:10px;color:var(--muted);margin-top:4px}.historydel{position:absolute;right:8px;top:13px;border:0;background:transparent}.newbtn{width:100%;height:40px;border:1px solid var(--line);background:#17181b;color:#fff;border-radius:11px;margin-bottom:12px}.event{padding:9px 10px;border-left:3px solid #d4d4d8;background:#fafafa;margin-bottom:8px;border-radius:0 9px 9px 0;font-size:11px}.event.error{border-color:var(--red)}.event.success{border-color:var(--green)}.event.progress{border-color:var(--blue)}.eventtime{color:#a1a1aa;font-size:9px;margin-top:4px}
.artifact{border:1px solid var(--line);border-radius:14px;overflow:hidden;background:#fff;max-width:640px}.artifact img,.artifact video{display:block;max-width:100%;max-height:430px;margin:auto}.artifact audio{width:100%;padding:14px}.artifactfile{display:flex;align-items:center;gap:12px;padding:18px}.badge{width:52px;height:52px;border-radius:12px;background:#ececf0;display:grid;place-items:center;font-weight:800;font-size:11px}.artifactfoot{display:flex;align-items:center;padding:11px 13px;border-top:1px solid var(--line)}.artifactname{font-size:12px;font-weight:700;word-break:break-all}.artifactmeta{font-size:10px;color:var(--muted);margin-top:4px}.artifactactions{margin-left:auto;display:flex;gap:7px}.artifactactions button{height:32px;border:1px solid var(--line);background:#fff;border-radius:8px}
.terminalSheet{position:fixed;left:18px;right:18px;bottom:18px;height:min(72vh,720px);z-index:40;background:#071019;border-radius:18px;box-shadow:0 25px 90px rgba(0,0,0,.35);display:none;grid-template-rows:50px 1fr 58px;overflow:hidden;color:#dcecff}.terminalSheet.show{display:grid}.terminalHead{display:flex;align-items:center;padding:0 14px;background:#0d1a28;border-bottom:1px solid #22364a}.traffic{display:flex;gap:6px}.ball{width:10px;height:10px;border-radius:50%}.r{background:#ff6b6b}.y{background:#ffd166}.g{background:#5ee39d}.terminalTitle{margin-left:12px;font:12px Consolas,monospace;color:#91abc2}.terminalBtns{margin-left:auto;display:flex;gap:6px}.terminalBtns button{height:30px;border:1px solid #2a4259;background:#102237;color:#dcecff;border-radius:8px}.terminal{position:relative;min-height:0;background:#050b11;overflow:hidden}.screen{position:absolute;inset:0;margin:0;padding:12px 13px;overflow:hidden;white-space:pre;font-family:Consolas,"Cascadia Mono","Microsoft JhengHei UI",monospace;font-size:13px;line-height:1.32;color:#d8e7f3;tab-size:4;user-select:text}.cursor{position:absolute;width:7px;height:16px;background:rgba(73,213,255,.72);animation:blink 1s steps(1) infinite;display:none}.terminalIme{position:absolute;z-index:5;width:3px;height:20px;padding:0;margin:0;border:0;outline:0;resize:none;overflow:hidden;background:transparent;color:transparent;caret-color:transparent;opacity:.02}.rawbar{display:grid;grid-template-columns:1fr auto;gap:8px;padding:8px;background:#0a1520;border-top:1px solid #22364a}.rawbar textarea{border:1px solid #294057;background:#08131e;color:#dcecff;border-radius:9px;padding:9px;resize:none}.rawbar button{border:1px solid #2a4259;background:#102237;color:#dcecff;border-radius:9px;padding:0 14px}@keyframes blink{50%{opacity:0}}
.toast{position:fixed;right:20px;bottom:20px;z-index:70;background:#17181b;color:#fff;padding:11px 14px;border-radius:10px;opacity:0;transform:translateY(10px);transition:.2s}.toast.show{opacity:1;transform:none}.lightbox{position:fixed;inset:0;z-index:60;background:rgba(0,0,0,.85);display:none;place-items:center}.lightbox.show{display:grid}.lightbox img{max-width:92vw;max-height:88vh}.lightbox button{position:absolute;right:25px;top:20px;border:0;background:#fff;width:40px;height:40px;border-radius:50%;font-size:22px}
@media(max-width:900px){.drawer{width:min(92vw,390px)}.brand small{display:none}.project{display:none}.main{padding-left:12px;padding-right:12px}.message{grid-template-columns:34px minmax(0,1fr)}}
</style>
</head>
<body>
<div class="app">
<header class="top">
<button id="btnHistory" class="iconbtn">☰</button><div class="brand"><div class="brandmark">✦</div><div><h1>Codex Workspace</h1><small>JSON 引擎 · v<span id="version">0.5.2</span></small></div></div>
<button id="projectChip" class="project">📁 <span id="projectName">尚未選擇專案</span></button><div class="spacer"></div>
<div id="status" class="status"><span class="dot"></span><span id="statusText">尚未啟動</span></div>
<button id="btnRun" class="topbtn primary"><span id="runIcon">▶</span> <span id="runLabel">啟動</span></button>
<button id="btnStopTask" class="topbtn stop" style="display:none">■ 停止工作</button>
<button id="btnTimeline" class="iconbtn" title="執行履歷">◷</button><button id="btnTerminal" class="iconbtn" title="進階終端">›_</button><button id="btnSettings" class="iconbtn">⚙</button>
</header>
<main id="main" class="main"><div id="conversation" class="conversation"></div></main>
</div>
<div class="composerWrap"><div class="composerBox"><div id="attachmentBar" class="attachments"></div><div class="composer"><button id="btnAttach">＋</button><textarea id="promptInput" placeholder="請先啟動 Codex…"></textarea><button id="btnSend" class="send">↑</button></div></div><div class="hint">Enter 送出 · Shift+Enter 換行 · 支援上傳檔案與 Ctrl+V 貼圖</div></div>
<div id="backdrop" class="backdrop"></div>
<aside id="historyDrawer" class="drawer left"><div class="drawerhead">對話履歷<button data-close>×</button></div><div class="drawerbody"><button id="btnNewChat" class="newbtn">＋ 新對話</button><input id="historySearch" placeholder="搜尋對話" style="width:100%;height:38px;border:1px solid var(--line);border-radius:9px;padding:0 10px;margin-bottom:10px"><div id="historyList"></div></div></aside>
<aside id="timelineDrawer" class="drawer right"><div class="drawerhead">執行履歷<button data-close>×</button></div><div class="drawerbody"><div id="timelineSummary" style="margin-bottom:15px"></div><div id="timeline"></div></div></aside>
<aside id="settingsDrawer" class="drawer right"><div class="drawerhead">設定<button data-close>×</button></div><div class="drawerbody">
<section class="section"><h3>專案與 Codex</h3><div class="field"><label>專案資料夾</label><div class="row"><input id="projectPath"><button id="btnBrowse" class="topbtn">瀏覽</button></div></div><div class="field"><label>Codex 命令或完整路徑</label><input id="codexCommand" value="codex"></div><div class="field"><label>PowerShell 路徑</label><input id="powershellPath"></div><div class="row"><button id="btnOpenProject" class="topbtn">開啟專案</button><button id="btnOpenAttachments" class="topbtn">貼圖資料夾</button></div></section>
<section class="section"><h3>JSON 執行權限</h3><div class="field"><label>沙盒模式</label><select id="sandboxMode"><option value="read-only">read-only（唯讀）</option><option value="workspace-write">workspace-write（建議）</option><option value="danger-full-access">danger-full-access（高風險）</option></select></div><div class="field"><label>批准模式</label><select id="approvalMode"><option value="never">never（聊天工作站建議）</option><option value="on-request">on-request</option><option value="on-failure">on-failure</option><option value="untrusted">untrusted</option></select></div><div class="switchrow"><div><strong>略過 Git 專案檢查</strong><small>RPG Maker 專案通常不是 Git 專案。</small></div><input id="skipGitCheck" type="checkbox" checked></div><div class="switchrow"><div><strong>暫時工作階段</strong><small>使用 --ephemeral，不保存 Codex rollout。</small></div><input id="ephemeral" type="checkbox"></div></section>
<section class="section"><h3>聊天與成果</h3><div class="switchrow"><div><strong>開啟程式後自動就緒</strong></div><input id="autoReady" type="checkbox"></div><div class="switchrow"><div><strong>送出後清空附件</strong></div><input id="clearAttachmentsAfterSend" type="checkbox" checked></div><div class="switchrow"><div><strong>自動偵測成果</strong><small>任務結束後顯示新產生或更新的圖片、影片、音訊與常見檔案。</small></div><input id="autoDetectArtifacts" type="checkbox" checked></div></section>
<section class="section"><h3>資料位置</h3><div style="font-size:10px;color:var(--muted);line-height:1.8">設定：<span id="configPath"></span><br>履歷：<span id="databasePath"></span></div></section>
</div></aside>
<section id="terminalSheet" class="terminalSheet"><div class="terminalHead"><div class="traffic"><span class="ball r"></span><span class="ball y"></span><span class="ball g"></span></div><div class="terminalTitle">powershell.exe — stable ConPTY terminal · 開啟即自動啟動</div><div class="terminalBtns"><button id="btnShellStart">啟動</button><button id="btnShellRestart">重啟</button><button id="btnShellCtrlC">Ctrl+C</button><button id="btnShellClear">清空</button><button id="btnShellCopy">複製</button><button id="btnTerminalClose">關閉</button></div></div><div id="terminal" class="terminal"><pre id="screen" class="screen"></pre><div id="cursor" class="cursor"></div><textarea id="terminalIme" class="terminalIme" autocomplete="off" spellcheck="false"></textarea></div><div class="rawbar"><textarea id="shellInput" placeholder="輸入 PowerShell 命令；Enter 送出，Shift+Enter 換行"></textarea><button id="btnShellSend">送出 ↵</button></div></section>
<div id="lightbox" class="lightbox"><button id="lightboxClose">×</button><img id="lightboxImage"></div><div id="toast" class="toast"></div>
<script>
'use strict';
let bridge=null,ready=false,taskRunning=false,shellRunning=false,currentSessionId='',currentSession=null,histories=[],attachments=[],activeTaskId='';
let saveTimer=null,bound=false,imeComposing=false,imeFlushTimer=null;
const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"']/g,m=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[m]));
function fmtTime(ts){if(!ts)return'';return new Date(Number(ts)*1000).toLocaleTimeString('zh-TW',{hour:'2-digit',minute:'2-digit',hour12:false});}
function fmtDate(ts){if(!ts)return'';const d=new Date(Number(ts)*1000);return d.toLocaleDateString('zh-TW',{month:'2-digit',day:'2-digit'})+' '+fmtTime(ts);}
function fmtSize(n){n=Number(n||0);if(n<1024)return n+' B';if(n<1048576)return(n/1024).toFixed(1)+' KB';if(n<1073741824)return(n/1048576).toFixed(1)+' MB';return(n/1073741824).toFixed(1)+' GB';}
function toast(t){const e=$('toast');e.textContent=t;e.classList.add('show');clearTimeout(e._t);e._t=setTimeout(()=>e.classList.remove('show'),2600);}
function setStatus(t,k='idle'){$('statusText').textContent=t;$('status').className='status '+k;}
function setReady(v){ready=!!v;$('runIcon').textContent=ready?'■':'▶';$('runLabel').textContent=ready?'停止':'啟動';$('btnRun').className='topbtn '+(ready?'stop':'primary');const i=$('promptInput');i.disabled=!ready||taskRunning;i.placeholder=!ready?'請先啟動 Codex…':(taskRunning?'Codex 正在工作…':'傳訊息給 Codex，或直接 Ctrl+V 貼上圖片…');$('btnSend').disabled=!ready||taskRunning;}
function setTaskRunning(v){taskRunning=!!v;$('btnStopTask').style.display=taskRunning?'inline-block':'none';setReady(ready);}
function updateProject(){const p=$('projectPath').value.trim();$('projectName').textContent=p?p.replace(/[\\/]+$/,'').split(/[\\/]/).pop():'尚未選擇專案';}
function collectSettings(){return{projectPath:$('projectPath').value.trim(),codexCommand:$('codexCommand').value.trim()||'codex',powershellPath:$('powershellPath').value.trim(),sandboxMode:$('sandboxMode').value,approvalMode:$('approvalMode').value,skipGitCheck:$('skipGitCheck').checked,ephemeral:$('ephemeral').checked,autoReady:$('autoReady').checked,clearAttachmentsAfterSend:$('clearAttachmentsAfterSend').checked,autoDetectArtifacts:$('autoDetectArtifacts').checked};}
function saveSettings(){if(!bridge)return;updateProject();clearTimeout(saveTimer);saveTimer=setTimeout(()=>bridge.saveSettings(JSON.stringify(collectSettings())),180);}
function openDrawer(id){$('backdrop').classList.add('show');$(id).classList.add('show');}function closeDrawers(){$('backdrop').classList.remove('show');document.querySelectorAll('.drawer.show').forEach(x=>x.classList.remove('show'));}
function simpleMarkdown(text){const parts=String(text||'').split(/```/);let h='';parts.forEach((p,i)=>{if(i%2){const n=p.indexOf('\n');if(n>=0&&n<30)p=p.slice(n+1);h+='<pre><code>'+esc(p.trimEnd())+'</code></pre>';return;}let s=esc(p).replace(/`([^`\n]+)`/g,'<code>$1</code>').replace(/\*\*([^*]+)\*\*/g,'<strong>$1</strong>');s=s.replace(/(^|\n)[-•]\s+([^\n]+)/g,'$1<span style="display:block;padding-left:14px">• $2</span>');h+=s.replace(/\n/g,'<br>');});return h;}
function emptyState(){return`<section class="empty"><div><div class="empty-logo">✦</div><h2>今天要修改什麼？</h2><p>聊天內容由 <b>codex exec --json</b> 的結構化事件更新。<br>不再解析互動式終端，所以不會出現 Working、Booting MCP 或重畫碎片。</p></div></section>`;}
function buildUser(m){const a=(m.data&&m.data.attachments)||[];const f=a.length?`<div style="display:flex;gap:7px;flex-wrap:wrap;justify-content:flex-end;margin-bottom:8px">${a.map(x=>`<span style="border:1px solid var(--line);padding:6px 9px;border-radius:10px;font-size:10px">📎 ${esc(x.name)}</span>`).join('')}</div>`:'';return`<article class="message user" data-message-id="${m.id||''}"><div class="avatar">你</div><div class="body">${f}<div class="text">${esc(m.content||'')}</div></div></article>`;}
function buildAssistant(m){return`<article class="message assistant" data-message-id="${m.id||''}"><div class="avatar">C</div><div class="body"><div class="head"><span class="name">Codex</span><span class="time">${fmtTime(m.created_at)}</span></div><div class="text">${simpleMarkdown(m.content||'')}</div></div></article>`;}
function buildArtifact(item,msgId=''){let b='';if(item.kind==='image')b=`<img src="${esc(item.fileUrl)}" data-lightbox="${esc(item.fileUrl)}">`;else if(item.kind==='video')b=`<video controls preload="metadata" src="${esc(item.fileUrl)}"></video>`;else if(item.kind==='audio')b=`<audio controls preload="metadata" src="${esc(item.fileUrl)}"></audio>`;else{const ext=(item.name.split('.').pop()||'FILE').toUpperCase().slice(0,5);b=`<div class="artifactfile"><div class="badge">${esc(ext)}</div><div><b>成果檔案</b><div style="font-size:10px;color:var(--muted);margin-top:5px">可直接開啟或查看所在資料夾</div></div></div>`;}return`<article class="message assistant" data-message-id="${msgId}"><div class="avatar">C</div><div class="body"><div class="head"><span class="name">Codex 成果</span></div><div class="artifact">${b}<div class="artifactfoot"><div><div class="artifactname">${esc(item.name)}</div><div class="artifactmeta">${esc((item.kind||'file').toUpperCase())} · ${fmtSize(item.size)}</div></div><div class="artifactactions"><button data-open="${esc(item.path)}">開啟</button><button data-folder="${esc(item.path)}">資料夾</button></div></div></div></div></article>`;}
function renderSession(d){currentSession=d||{};currentSessionId=d?.session?.id||'';activeTaskId='';const c=$('conversation');c.innerHTML='';const ms=d?.messages||[];if(!ms.length)c.innerHTML=emptyState();else ms.forEach(m=>{if(m.kind==='artifact')c.insertAdjacentHTML('beforeend',buildArtifact(m.data||{},m.id));else if(m.role==='user')c.insertAdjacentHTML('beforeend',buildUser(m));else c.insertAdjacentHTML('beforeend',buildAssistant(m));});renderTimeline(d?.events||[]);renderHistory(histories);bindDynamic();setTimeout(()=>{$('main').scrollTop=$('main').scrollHeight},30);}
function ensureTask(taskId){let e=document.querySelector(`[data-live-task="${taskId}"]`);if(e)return e;const empty=$('conversation .empty');if(empty)empty.remove();$('conversation').insertAdjacentHTML('beforeend',`<article class="message assistant" data-live-task="${taskId}"><div class="avatar">C</div><div class="body"><div class="head"><span class="name">Codex</span><span class="time">執行中</span></div><div class="task"><div class="taskhead"><span class="spin"></span><span class="tasktitle">正在處理你的要求…</span></div><div class="progress"></div></div><div class="text" style="margin-top:12px"></div></div></article>`);return document.querySelector(`[data-live-task="${taskId}"]`);}
function handleChat(raw){let e;try{e=JSON.parse(raw)}catch(_){return}if(e.sessionId!==currentSessionId)return;if(e.type==='task_start'){activeTaskId=e.taskId;const empty=$('conversation .empty');if(empty)empty.remove();$('conversation').insertAdjacentHTML('beforeend',buildUser({id:e.userMessageId,content:e.text,data:{attachments:e.attachments},created_at:e.createdAt}));ensureTask(e.taskId);return}if(e.type==='progress'){const h=ensureTask(e.taskId),p=h.querySelector('.progress'),d=document.createElement('div');d.textContent='• '+e.text;p.appendChild(d);while(p.children.length>7)p.removeChild(p.firstChild);h.querySelector('.tasktitle').textContent=e.text;return}if(e.type==='assistant_update'){const h=ensureTask(e.taskId);h.querySelector('.text').innerHTML=simpleMarkdown(e.content||'');h.dataset.messageId=e.messageId||'';return}if(e.type==='assistant_finish'){const h=ensureTask(e.taskId),t=h.querySelector('.task');t.classList.add('done');h.querySelector('.tasktitle').textContent=e.success?'工作已完成':'工作結束';h.querySelector('.text').innerHTML=simpleMarkdown(e.content||'');h.removeAttribute('data-live-task');h.dataset.messageId=e.messageId||'';activeTaskId='';bindDynamic();return}if(e.type==='artifact'){$('conversation').insertAdjacentHTML('beforeend',buildArtifact(e.item||{},e.messageId||''));bindDynamic();}}
function renderAttachments(){const b=$('attachmentBar');b.innerHTML='';b.classList.toggle('show',attachments.length>0);attachments.forEach(x=>{const d=document.createElement('div');d.className='attach';d.innerHTML=`${x.isImage&&x.preview?`<img src="${x.preview}">`:`<div class="file">${esc((x.name.split('.').pop()||'FILE').toUpperCase().slice(0,4))}</div>`}<div class="attachname">${esc(x.name)}</div><button>×</button>`;d.querySelector('button').onclick=()=>bridge.removeAttachment(x.id);b.appendChild(d);});}
function sendPrompt(){const text=$('promptInput').value.trim();if(!text&&!attachments.length)return;if(!ready){toast('請先啟動 Codex');return}if(taskRunning){toast('上一個任務仍在執行');return}bridge.sendChatMessage(JSON.stringify({text}),ok=>{if(ok){$('promptInput').value='';autoResize();}});}
function autoResize(){const e=$('promptInput');e.style.height='auto';e.style.height=Math.min(180,Math.max(42,e.scrollHeight))+'px';}
function pasteImages(e){const a=[...(e.clipboardData?.items||[])].filter(x=>x.type&&x.type.startsWith('image/'));if(!a.length)return;e.preventDefault();a.forEach(x=>{const f=x.getAsFile();if(!f)return;const r=new FileReader();r.onload=()=>bridge.savePastedImage(String(r.result||''));r.readAsDataURL(f);});}
function renderHistory(list){histories=list||histories;const q=$('historySearch').value.trim().toLowerCase(),b=$('historyList');b.innerHTML='';histories.filter(x=>!q||String(x.title).toLowerCase().includes(q)).forEach(x=>{const d=document.createElement('div');d.className='historyitem '+(x.id===currentSessionId?'active':'');d.innerHTML=`<div class="historytitle">${esc(x.title)}</div><div class="historymeta">${fmtDate(x.updated_at)} · ${x.message_count} 則</div><button class="historydel">×</button>`;d.onclick=()=>bridge.loadSession(x.id);d.querySelector('button').onclick=ev=>{ev.stopPropagation();if(confirm('刪除這個對話？'))bridge.deleteSession(x.id)};b.appendChild(d);});}
function renderTimeline(ev){const s=currentSession?.session||{};$('timelineSummary').innerHTML=`<b>${esc(s.title||'新對話')}</b><div style="font-size:10px;color:var(--muted);margin-top:5px">${esc(s.project_path||'')}<br>${ev.length} 筆紀錄</div>`;const b=$('timeline');b.innerHTML='';ev.forEach(addEvent);}
function addEvent(e){if(e.session_id&&e.session_id!==currentSessionId)return;const d=document.createElement('div');d.className='event '+(e.level||'info');d.innerHTML=`${esc(e.text||'')}<div class="eventtime">${fmtTime(e.created_at)}</div>`;$('timeline').appendChild(d);}
function bindDynamic(){document.querySelectorAll('[data-open]').forEach(b=>{if(b.dataset.b)return;b.dataset.b='1';b.onclick=()=>bridge.openPath(b.dataset.open)});document.querySelectorAll('[data-folder]').forEach(b=>{if(b.dataset.b)return;b.dataset.b='1';b.onclick=()=>bridge.openContainingFolder(b.dataset.folder)});document.querySelectorAll('[data-lightbox]').forEach(i=>{if(i.dataset.b)return;i.dataset.b='1';i.onclick=()=>{$('lightboxImage').src=i.dataset.lightbox;$('lightbox').classList.add('show')}});}
// ----- v0.2.3 穩定終端模型 -----
function isWideChar(ch){if(!ch)return false;const cp=ch.codePointAt(0);return cp>=0x1100&&(cp<=0x115f||cp===0x2329||cp===0x232a||(cp>=0x2e80&&cp<=0xa4cf&&cp!==0x303f)||(cp>=0xac00&&cp<=0xd7a3)||(cp>=0xf900&&cp<=0xfaff)||(cp>=0xfe10&&cp<=0xfe19)||(cp>=0xfe30&&cp<=0xfe6f)||(cp>=0xff00&&cp<=0xff60)||(cp>=0xffe0&&cp<=0xffe6)||(cp>=0x1f300&&cp<=0x1faff)||(cp>=0x20000&&cp<=0x3fffd));}
function isCombiningChar(ch){if(!ch)return false;const cp=ch.codePointAt(0);return(cp>=0x0300&&cp<=0x036f)||(cp>=0x1ab0&&cp<=0x1aff)||(cp>=0x1dc0&&cp<=0x1dff)||(cp>=0x20d0&&cp<=0x20ff)||(cp>=0xfe20&&cp<=0xfe2f)||(cp>=0xfe00&&cp<=0xfe0f)||cp===0x200d;}
function metrics(){const s=$('screen'),cs=getComputedStyle(s),cv=metrics._c||(metrics._c=document.createElement('canvas')),cx=cv.getContext('2d');cx.font=`${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;return{cw:Math.max(6,cx.measureText('M').width||7.9),lh:parseFloat(cs.lineHeight)||17.2};}
class MiniTerminal{constructor(rows=42,cols=150){this.rows=rows;this.cols=cols;this.mainBuffer=null;this.altScreen=false;this.reset()}blankLine(){return Array(this.cols).fill(' ')}reset(){this.lines=Array.from({length:this.rows},()=>this.blankLine());this.r=0;this.c=0;this.savedR=0;this.savedC=0;this.scrollTop=0;this.scrollBottom=this.rows-1;this.state='normal';this.params='';this.osc='';this.wrapPending=false;this.autoWrap=true;this.originMode=false;this.cursorVisible=true;this.lastPrinted=' ';this.renderSoon()}resize(rows,cols){rows=Math.max(12,Math.min(100,rows));cols=Math.max(40,Math.min(300,cols));if(rows===this.rows&&cols===this.cols)return;const old=this.lines,or=this.rows,oc=this.cols,nl=Array.from({length:rows},()=>Array(cols).fill(' '));for(let r=0;r<Math.min(or,rows);r++)for(let c=0;c<Math.min(oc,cols);c++)nl[r][c]=old[r][c];this.rows=rows;this.cols=cols;this.lines=nl;this.r=Math.min(this.r,rows-1);this.c=Math.min(this.c,cols-1);this.scrollTop=0;this.scrollBottom=rows-1;this.wrapPending=false;this.renderSoon()}feed(data){for(const ch of data)this.consume(ch);this.renderSoon()}consume(ch){if(this.state==='osc'){if(ch==='\x07'){this.state='normal';this.osc=''}else if(ch==='\x1b')this.state='oscEsc';else this.osc+=ch;return}if(this.state==='oscEsc'){this.state=ch==='\\'?'normal':'osc';return}if(this.state==='esc'){if(ch==='['){this.state='csi';this.params=''}else if(ch===']'){this.state='osc';this.osc=''}else if(ch==='7'){this.savedR=this.r;this.savedC=this.c;this.wrapPending=false;this.state='normal'}else if(ch==='8'){this.r=this.savedR;this.c=this.savedC;this.wrapPending=false;this.state='normal'}else if(ch==='D'){this.lineFeed(false);this.state='normal'}else if(ch==='E'){this.lineFeed(true);this.state='normal'}else if(ch==='M'){this.reverseIndex();this.state='normal'}else if(ch==='c'){this.reset();this.state='normal'}else this.state='normal';return}if(this.state==='csi'){if(ch>='@'&&ch<='~'){this.handleCSI(ch,this.params);this.state='normal';this.params=''}else this.params+=ch;return}if(ch==='\x1b'){this.state='esc';return}if(ch==='\r'){this.c=0;this.wrapPending=false;return}if(ch==='\n'||ch==='\x0b'||ch==='\x0c'){this.lineFeed(false);return}if(ch==='\b'){if(this.wrapPending)this.wrapPending=false;else this.c=Math.max(0,this.c-1);return}if(ch==='\t'){this.wrapPending=false;this.c=Math.min(this.cols-1,(Math.floor(this.c/8)+1)*8);return}if(ch<' '||ch==='\x7f')return;this.put(ch)}parseParams(p){const m=p.match(/^([?<>=!]+)/),prefix=m?m[1]:'',raw=prefix?p.slice(prefix.length):p;return{prefix,values:raw===''?[]:raw.split(';').map(x=>x===''?0:(parseInt(x,10)||0))}}cancelWrap(){this.wrapPending=false}handleCSI(f,p){const z=this.parseParams(p),a=z.values,n=(i,d=1)=>(a[i]===undefined||a[i]===0)?d:a[i];switch(f){case'A':this.cancelWrap();this.r=Math.max(this.originMode?this.scrollTop:0,this.r-n(0));break;case'B':case'e':this.cancelWrap();this.r=Math.min(this.originMode?this.scrollBottom:this.rows-1,this.r+n(0));break;case'C':case'a':this.cancelWrap();this.c=Math.min(this.cols-1,this.c+n(0));break;case'D':this.cancelWrap();this.c=Math.max(0,this.c-n(0));break;case'E':this.cancelWrap();this.r=Math.min(this.rows-1,this.r+n(0));this.c=0;break;case'F':this.cancelWrap();this.r=Math.max(0,this.r-n(0));this.c=0;break;case'G':case'`':this.cancelWrap();this.c=Math.min(this.cols-1,n(0)-1);break;case'd':this.cancelWrap();this.r=Math.min(this.rows-1,n(0)-1);break;case'H':case'f':this.cancelWrap();this.r=Math.min(this.rows-1,n(0)-1);this.c=Math.min(this.cols-1,n(1)-1);break;case'J':this.eraseDisplay(a[0]||0);break;case'K':this.eraseLine(a[0]||0);break;case's':this.savedR=this.r;this.savedC=this.c;break;case'u':this.r=this.savedR;this.c=this.savedC;break;case'm':break;case'h':case'l':this.setMode(z.prefix,a,f==='h');break;case'P':this.deleteChars(n(0));break;case'@':this.insertChars(n(0));break;case'X':this.clearRange(this.r,this.c,Math.min(this.cols-1,this.c+n(0)-1));break;case'L':this.insertLines(n(0));break;case'M':this.deleteLines(n(0));break;case'S':this.scrollUp(n(0));break;case'T':this.scrollDown(n(0));break;case'r':this.setScrollRegion(a);break;case'b':for(let i=0;i<n(0);i++)this.put(this.lastPrinted||' ');break}}setMode(prefix,modes,on){if(!prefix.includes('?'))return;for(const m of modes){if(m===7){this.autoWrap=on;this.wrapPending=false}else if(m===25){this.cursorVisible=on}else if(m===47||m===1047||m===1049){if(on)this.enterAltScreen();else this.leaveAltScreen()}}}enterAltScreen(){if(!this.altScreen){this.mainBuffer={lines:this.lines.map(x=>x.slice()),r:this.r,c:this.c};this.altScreen=true}this.lines=Array.from({length:this.rows},()=>this.blankLine());this.r=this.c=0;this.scrollTop=0;this.scrollBottom=this.rows-1;this.wrapPending=false}leaveAltScreen(){if(this.altScreen&&this.mainBuffer){this.lines=this.mainBuffer.lines.map(x=>x.slice(0,this.cols));while(this.lines.length<this.rows)this.lines.push(this.blankLine());this.lines.length=this.rows;this.r=Math.min(this.mainBuffer.r,this.rows-1);this.c=Math.min(this.mainBuffer.c,this.cols-1)}this.mainBuffer=null;this.altScreen=false;this.scrollTop=0;this.scrollBottom=this.rows-1;this.wrapPending=false}setScrollRegion(a){const t=(a[0]||1)-1,b=(a[1]||this.rows)-1;if(t>=0&&b<this.rows&&t<b){this.scrollTop=t;this.scrollBottom=b}else{this.scrollTop=0;this.scrollBottom=this.rows-1}this.r=0;this.c=0}eraseDisplay(m){if(m===2||m===3){this.lines.forEach(x=>x.fill(' '));return}if(m===0){this.clearRange(this.r,this.c,this.cols-1);for(let r=this.r+1;r<this.rows;r++)this.lines[r].fill(' ')}else if(m===1){for(let r=0;r<this.r;r++)this.lines[r].fill(' ');this.clearRange(this.r,0,this.c)}}eraseLine(m){if(m===0)this.clearRange(this.r,this.c,this.cols-1);else if(m===1)this.clearRange(this.r,0,this.c);else this.lines[this.r].fill(' ')}deleteChars(k){k=Math.min(k,this.cols-this.c);this.lines[this.r].splice(this.c,k);this.lines[this.r].push(...Array(k).fill(' '))}insertChars(k){k=Math.min(k,this.cols-this.c);this.lines[this.r].splice(this.c,0,...Array(k).fill(' '));this.lines[this.r].length=this.cols}insertLines(k){if(this.r<this.scrollTop||this.r>this.scrollBottom)return;for(let i=0;i<Math.min(k,this.scrollBottom-this.r+1);i++){this.lines.splice(this.r,0,this.blankLine());this.lines.splice(this.scrollBottom+1,1)}}deleteLines(k){if(this.r<this.scrollTop||this.r>this.scrollBottom)return;for(let i=0;i<Math.min(k,this.scrollBottom-this.r+1);i++){this.lines.splice(this.r,1);this.lines.splice(this.scrollBottom,0,this.blankLine())}}scrollUp(k=1,t=this.scrollTop,b=this.scrollBottom){for(let i=0;i<Math.min(k,b-t+1);i++){this.lines.splice(t,1);this.lines.splice(b,0,this.blankLine())}}scrollDown(k=1,t=this.scrollTop,b=this.scrollBottom){for(let i=0;i<Math.min(k,b-t+1);i++){this.lines.splice(b,1);this.lines.splice(t,0,this.blankLine())}}lineFeed(reset=false){this.wrapPending=false;if(reset)this.c=0;if(this.r===this.scrollBottom)this.scrollUp();else if(this.r<this.rows-1)this.r++;else this.scrollUp(1,0,this.rows-1)}reverseIndex(){this.wrapPending=false;if(this.r===this.scrollTop)this.scrollDown();else this.r=Math.max(0,this.r-1)}clearRange(r,s,e){if(r<0||r>=this.rows)return;for(let c=Math.max(0,s);c<=Math.min(this.cols-1,e);c++)this.clearGlyphAt(r,c)}clearGlyphAt(r,c){if(r<0||r>=this.rows||c<0||c>=this.cols)return;const l=this.lines[r];if(l[c]===''){l[c]=' ';if(c>0&&isWideChar(l[c-1]))l[c-1]=' ';return}if(isWideChar(l[c])&&c+1<this.cols&&l[c+1]==='')l[c+1]=' ';l[c]=' '}appendCombining(ch){let c=this.c-1;if(c>=0&&this.lines[this.r][c]===''&&c>0)c--;if(c>=0&&this.lines[this.r][c]&&this.lines[this.r][c]!==' ')this.lines[this.r][c]+=ch}put(ch){if(isCombiningChar(ch)){this.appendCombining(ch);return}if(this.wrapPending){if(this.autoWrap){this.c=0;this.lineFeed(false)}else this.wrapPending=false}const w=isWideChar(ch)?2:1;if(w===2&&this.c===this.cols-1&&this.autoWrap){this.c=0;this.lineFeed(false)}this.clearGlyphAt(this.r,this.c);if(w===2)this.clearGlyphAt(this.r,this.c+1);this.lines[this.r][this.c]=ch;if(w===2&&this.c+1<this.cols)this.lines[this.r][this.c+1]='';this.lastPrinted=ch;if(this.c+w>=this.cols){this.c=this.cols-1;this.wrapPending=this.autoWrap}else this.c+=w}renderSoon(){if(this._raf)return;this._raf=requestAnimationFrame(()=>{this._raf=null;this.render()})}render(){$('screen').textContent=this.lines.map(x=>x.join('').replace(/ +$/,'')).join('\n');const m=metrics(),left=13+this.c*m.cw,top=12+this.r*m.lh;$('cursor').style.left=left+'px';$('cursor').style.top=top+'px';$('cursor').style.height=(m.lh-1)+'px';$('cursor').style.display=(shellRunning&&this.cursorVisible)?'block':'none';$('terminalIme').style.left=Math.max(0,left)+'px';$('terminalIme').style.top=Math.max(0,top)+'px'}text(){return this.lines.map(x=>x.join('').replace(/ +$/,'')).join('\n')}}
const term=new MiniTerminal();function fitTerminal(){const b=$('terminal').getBoundingClientRect(),m=metrics();const r=Math.max(12,Math.floor((b.height-24)/m.lh)),c=Math.max(40,Math.floor((b.width-26)/m.cw));term.resize(r,c);if(bridge)bridge.resizeShell(r,c)}
function flushIme(){clearTimeout(imeFlushTimer);imeFlushTimer=null;const i=$('terminalIme');if(imeComposing||!i.value)return;const t=String(i.value).replace(/\r\n/g,'\r').replace(/\n/g,'\r');i.value='';if(t&&bridge&&shellRunning)bridge.sendShellInput(t)}function scheduleIme(){clearTimeout(imeFlushTimer);imeFlushTimer=setTimeout(flushIme,0)}function terminalKey(e){if(!bridge||!shellRunning||imeComposing||e.isComposing||e.keyCode===229)return;let o=null;if(e.ctrlKey&&e.key.toLowerCase()==='c')o='\x03';else if(e.key==='Enter')o='\r';else if(e.key==='Backspace')o='\x7f';else if(e.key==='Tab')o='\t';else if(e.key==='Escape')o='\x1b';else if(e.key==='ArrowUp')o='\x1b[A';else if(e.key==='ArrowDown')o='\x1b[B';else if(e.key==='ArrowRight')o='\x1b[C';else if(e.key==='ArrowLeft')o='\x1b[D';else if(e.key==='Delete')o='\x1b[3~';if(o!==null){e.preventDefault();$('terminalIme').value='';bridge.sendShellInput(o)}}
function toggleTerminal(v=null){const e=$('terminalSheet'),n=v===null?!e.classList.contains('show'):v;e.classList.toggle('show',n);if(n)setTimeout(()=>{fitTerminal();if(!shellRunning&&bridge){term.reset();term.feed('正在啟動 PowerShell…\r\n');bridge.startShell()}else{$('terminalIme').focus()}},80)}function sendShell(){const v=$('shellInput').value;if(!v)return;bridge.sendShellLine(v);$('shellInput').value=''}
function bind(){if(bound)return;bound=true;$('btnHistory').onclick=()=>openDrawer('historyDrawer');$('btnTimeline').onclick=()=>openDrawer('timelineDrawer');$('btnSettings').onclick=()=>openDrawer('settingsDrawer');$('backdrop').onclick=closeDrawers;document.querySelectorAll('[data-close]').forEach(x=>x.onclick=closeDrawers);$('btnNewChat').onclick=()=>bridge.newSession();$('historySearch').oninput=()=>renderHistory(histories);$('projectChip').onclick=chooseProject;$('btnBrowse').onclick=chooseProject;$('btnOpenProject').onclick=()=>bridge.openPath($('projectPath').value.trim());$('btnOpenAttachments').onclick=()=>bridge.openPath(window.attachmentFolder||'');$('btnRun').onclick=()=>{if(ready){bridge.markNotReady();return}bridge.saveSettings(JSON.stringify(collectSettings()));setTimeout(()=>bridge.markReady(),220)};$('btnStopTask').onclick=()=>bridge.stopTask();$('btnAttach').onclick=()=>bridge.chooseFiles();$('btnSend').onclick=sendPrompt;$('promptInput').oninput=autoResize;$('promptInput').onpaste=pasteImages;$('promptInput').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing&&e.keyCode!==229){e.preventDefault();sendPrompt()}};$('btnTerminal').onclick=()=>toggleTerminal();$('btnTerminalClose').onclick=()=>toggleTerminal(false);$('btnShellStart').onclick=()=>{if(shellRunning){bridge.stopShell()}else{term.reset();term.feed('正在啟動 PowerShell…\r\n');bridge.startShell()}};$('btnShellRestart').onclick=()=>bridge.restartShell();$('btnShellCtrlC').onclick=()=>bridge.sendShellInput('\x03');$('btnShellClear').onclick=()=>term.reset();$('btnShellCopy').onclick=()=>navigator.clipboard.writeText(term.text()).then(()=>toast('已複製終端畫面'));$('btnShellSend').onclick=sendShell;$('shellInput').onkeydown=e=>{if(e.key==='Enter'&&!e.shiftKey&&!e.isComposing){e.preventDefault();sendShell()}};const i=$('terminalIme');i.addEventListener('keydown',terminalKey);i.addEventListener('compositionstart',()=>imeComposing=true);i.addEventListener('compositionend',()=>{imeComposing=false;scheduleIme()});i.addEventListener('input',()=>{if(!imeComposing)scheduleIme()});$('terminal').onclick=()=>i.focus();$('lightboxClose').onclick=()=>$('lightbox').classList.remove('show');$('lightbox').onclick=e=>{if(e.target===$('lightbox'))$('lightbox').classList.remove('show')};for(const id of['projectPath','codexCommand','powershellPath','sandboxMode','approvalMode','skipGitCheck','ephemeral','autoReady','clearAttachmentsAfterSend','autoDetectArtifacts'])$(id).addEventListener('change',saveSettings);$('projectPath').addEventListener('input',updateProject);window.addEventListener('resize',()=>setTimeout(fitTerminal,100));}
function chooseProject(){bridge.chooseProjectFolder(v=>{if(v){$('projectPath').value=v;updateProject();saveSettings()}})}
new QWebChannel(qt.webChannelTransport,ch=>{bridge=ch.objects.bridge;bridge.statusChanged.connect(setStatus);bridge.readyChanged.connect(v=>{ready=!!v;setReady(v)});bridge.taskRunningChanged.connect(setTaskRunning);bridge.chatEvent.connect(handleChat);bridge.eventAdded.connect(raw=>{try{const e=JSON.parse(raw);if(e.session_id===currentSessionId)addEvent(e)}catch(_){}});bridge.historyChanged.connect(raw=>{try{histories=JSON.parse(raw);renderHistory(histories)}catch(_){}});bridge.sessionLoaded.connect(raw=>{try{renderSession(JSON.parse(raw));closeDrawers()}catch(_){}});bridge.attachmentsChanged.connect(raw=>{try{attachments=JSON.parse(raw);renderAttachments()}catch(_){}});bridge.shellOutputReceived.connect(x=>term.feed(x));bridge.shellRunningChanged.connect(v=>{shellRunning=!!v;$('btnShellStart').textContent=shellRunning?'停止':'啟動';term.renderSoon()});bridge.getInitialState(raw=>{const s=JSON.parse(raw);window.attachmentFolder=s.attachmentFolder||'';$('version').textContent=s.version;$('projectPath').value=s['專案路徑']||'';$('codexCommand').value=s['Codex命令']||'codex';$('powershellPath').value=s['PowerShell路徑']||'';$('sandboxMode').value=s['沙盒模式']||'workspace-write';$('approvalMode').value=s['批准模式']||'never';$('skipGitCheck').checked=s['略過Git檢查']!==false;$('ephemeral').checked=!!s['暫時工作階段'];$('autoReady').checked=!!s['啟動時自動就緒'];$('clearAttachmentsAfterSend').checked=s['送出後清空附件']!==false;$('autoDetectArtifacts').checked=s['自動偵測成果']!==false;$('configPath').textContent=s.configPath;$('databasePath').textContent=s.databasePath;histories=s.histories||[];attachments=s.attachments||[];ready=!!s.ready;taskRunning=!!s.taskRunning;shellRunning=!!s.shellRunning;updateProject();renderAttachments();setReady(ready);setTaskRunning(taskRunning);bind();renderSession(s.currentSession||{});if(s['啟動時自動就緒']&&!ready)setTimeout(()=>bridge.markReady(),500);});});
</script>
</body></html>
"""


class 主視窗(QWebEngineView):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(f"{應用程式名稱} v{應用程式版本}")
        self.resize(1500, 930)
        self.setMinimumSize(1000, 680)
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
        QTimer.singleShot(900, lambda: QMessageBox.information(
            視窗, "進階終端依賴",
            "找不到 pywinpty。JSON 聊天引擎仍可使用；若要使用進階 PowerShell 終端，請執行：\n\npip install pywinpty"
        ))
    return 應用.exec()


if __name__ == "__main__":
    raise SystemExit(main())
