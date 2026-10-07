# herdr-py（中文說明）

在同一個地方指揮多個 [OpenCode](https://opencode.ai) agent。每個 agent 的狀態直接取自 OpenCode 自己的事件：誰在工作、誰在等你回答、誰做完了。權限請求由策略自動回答，策略決定不了的才交給你（終端介面、手機網頁或指令）。另有團隊模式：agent 停下來時，由監工（程式）檢查成果，沒做完就告訴它從哪裡繼續。

借用 [herdr](https://github.com/herdrdev/herdr)（Apache-2.0）的想法、用 Python 重寫的非官方版本，與 herdr 無關，也沒有用到它的程式碼。English: [README.md](README.md).

- Python 3.6 以上，只用標準函式庫（RHEL 8 內建的 `platform-python` 就能跑）。
- 透過 HTTP 和 SSE 連 `opencode serve`。測試過的版本：OpenCode 1.18.32。

## 和 herdr 的差別

| | herdr | herdr-py |
|---|---|---|
| 是什麼 | 終端多工器：每個 agent 的真實畫面放在一個窗格 | 常駐程式：透過 `opencode serve` 指揮 agent |
| 狀態從哪來 | 有整合外掛就用外掛回報，沒有就讀畫面文字 | OpenCode 的事件（`session.status`、`permission.asked`…） |
| 無介面的 `opencode serve` | 追蹤不到 | 只用這個模式 |
| 誰回答權限 | 窗格前的人（或其他 agent 送按鍵） | 依 agent 與指令設定的策略（放行、永久放行、拒絕、問人）；「問人」才交給人 |
| 支援的 agent | 22 種（Claude Code、Codex、OpenCode…） | 只有 OpenCode |
| 人直接在 agent 畫面裡打字 | 可以 | 不行（只能送 prompt） |
| 監工、卡住看守 | 沒有 | 團隊模式（見下） |
| 程式量 | 約 25 萬行 Rust，另有內嵌的終端模擬器 | 約 2 千行 Python |

## 安裝（不需要 curl）

```bash
git clone https://github.com/pop310289/herdr-py
cd herdr-py && ./scripts/install.sh          # 在 ~/.local/bin 放一個 herdr-py 指令
```

也可以不安裝，在 repo 資料夾裡直接執行 `python3 -m herdr_py ...`。

## 快速開始

```bash
# 1. 啟動 OpenCode 伺服器（在你要 agent 工作的資料夾）
OPENCODE_SERVER_PASSWORD=change-me opencode serve --port 4096 &
echo change-me > ~/.config/opencode-password && chmod 600 ~/.config/opencode-password

# 2. 啟動 herdr-py 常駐程式
herdr-py serve --opencode http://127.0.0.1:4096 --password-file ~/.config/opencode-password \
    --policy examples/policy.json --http 127.0.0.1:8765 &
# 會印出：web UI: http://127.0.0.1:8765/#token=...

# 3. 派 agent
herdr-py start fixer "修好 test_stats.py 裡失敗的測試" --budget 600
herdr-py tui        # 上下鍵選擇，a 核准、A 永久核准、r 拒絕、p 送指令、x 中止、q 離開（agent 繼續跑）
herdr-py prompt fixer "空清單也要處理" --wait --timeout 900
```

## 權限策略

規則依序比對，第一條符合的生效。`match` 是正規表示式，必須比對整個目標（`bash` 是指令本身，其他是請求的路徑）；`agent` 是名稱萬用字元。動作：`allow`（放行一次）、`always`、`deny`（agent 會讀到理由）、`ask`（等人決定）。JSON 在任何 Python 版本都能用；TOML 要 Python 3.11 以上。允許的指令不要包含 `; | & $` 這類 shell 運算子。

## 團隊模式（監工）

`herdr-py team task.json --condition T --workdir 資料夾` 用三個角色做一題：執行者（可改檔）、唯讀的驗證者（解釋公開檢查為什麼沒過）、唯讀的確認者（對照原始需求）。監工是程式不是模型：執行者停下時跑公開檢查；除非檢查通過而且確認者接受，否則用「檢查輸出、角色的證據、執行者的 `NOTES.md`、最近的工具紀錄」組成「從哪裡繼續」的提示送回去。執行者連續 `--stall` 秒沒有進展，就換一個新的 session 並交接（這就是跨 session 的記憶）。`--condition S`（單一 agent）和 `N`（泛用的「檢查一下」提醒）是對照組。實驗程式：[`bench/p23`](bench/p23)。

## 部署到 RHEL 8

- RHEL 8 的 `python3` 是 3.6（`/usr/libexec/platform-python` 一定在）：herdr-py 可以用，設定檔請用 JSON。
- socket 要放在本機檔案系統（預設的 `~/.local/state` 即可）。有些網路或虛擬機共享的檔案系統不能放 Unix socket。
- OpenCode 的 glibc 版可以在 RHEL 8 上跑（實測 1.18.32，x86_64 與 aarch64，完成真實對話）；musl 版不行。
- systemd 使用者服務範例：[`examples/herdr-py.service`](examples/herdr-py.service)。網頁介面預設只聽 127.0.0.1，要遠端看請用 SSH 通道，不要開放連接埠。
- `LANG=C` 之下 Python 3.6 輸出中文會出錯：herdr-py 會自動改用 UTF-8 輸出（已在 RHEL 8 的 C 語系下測試）。

## 測試

```bash
python3 -m unittest discover -s tests        # 28 項，用假的 OpenCode 伺服器，不需要模型
python3 bench/p23/validate.py                # 在 RHEL 8 映像裡驗證實驗評分程式（需要 Docker）
```

## 限制

只支援 OpenCode。agent 提出的問題只能駁回、不能回答。介面只顯示最近的活動，完整對話用 `herdr-py read`。團隊模式還在實驗階段，請先看實驗結果再依賴它。

MIT 授權。
