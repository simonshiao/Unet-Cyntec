# Codex Workspace v0.5.0｜JSON 引擎版

這版把聊天核心從 Codex 互動式 TUI 解析，改成 `codex exec --json` JSONL 事件。

## 主要修正

- 不再把 `Booting MCP`、`Working`、`Reconnect` 等終端重畫碎片塞進聊天室。
- 啟動時不會自動產生空白 Codex 訊息。
- 一個使用者任務只會產生一個 Codex 回覆。
- 不再需要信任資料夾畫面，也不用手動輸入 `1 + Enter`。
- 工作進度直接由 JSON 事件更新。
- Codex 正式回答由 `agent_message` 取得。
- `turn.completed` 才會完成任務。
- 保留 SQLite 對話履歷。
- 支援多檔上傳與 Ctrl+V 貼圖。
- 圖片會另外透過 `--image` 交給 Codex。
- 任務完成後，自動偵測新產生的圖片、影片、音訊與常見成果檔。
- 保留獨立 PowerShell ConPTY 終端，僅供手動操作與除錯，不再參與聊天解析。

## 使用方式

1. 確認 Windows PowerShell 中執行 `codex --version` 有正常顯示版本。
2. 雙擊 `安裝並啟動.bat`。
3. 選擇 RPG Maker MV 專案資料夾。
4. 按右上角「啟動」。
5. 顯示「Codex 已就緒」後即可聊天。

## 預設安全設定

- 沙盒：`workspace-write`
- 批准模式：`never`
- 非 Git 專案：啟用 `--skip-git-repo-check`

`danger-full-access` 只建議在你完全信任的專案與環境中使用。
