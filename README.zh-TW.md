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
herdr-py start fixer "再幫它加一個測試" --fresh   # 同一個名字，上一輪結束後換一個新的 session
```

`agent.start` 加上 `fresh: true`（命令列 `--fresh`）時，若名字已存在、而且上一輪已結束，會換上新的 OpenCode session。OpenCode 會在無法預期的時候自動壓縮過長的 session（我們有一次實驗，壓縮後畫圖者回的是摘要，不是要它交的 JSON），所以每輪都把需要的資訊放進指令的呼叫者，可以讓每一輪都從乾淨的 session 開始。token、輪數與權限決定紀錄會累計；舊 session 之後才到的事件一律忽略。

## 權限策略

規則依序比對，第一條符合的生效。`match` 是正規表示式，必須比對整個目標（`bash` 是指令本身，其他是請求的路徑）；`agent` 是名稱萬用字元。動作：`allow`（放行一次）、`always`、`deny`（agent 會讀到理由）、`ask`（等人決定）。JSON 在任何 Python 版本都能用；TOML 要 Python 3.11 以上。允許的指令不要包含 `; | & $` 這類 shell 運算子。

## 團隊模式（監工）

`herdr-py team task.json --condition T --workdir 資料夾` 用三個角色做一題：執行者（可改檔）、唯讀的驗證者（解釋公開檢查為什麼沒過）、唯讀的確認者（對照原始需求）。監工是程式不是模型：執行者停下時跑公開檢查；除非檢查通過而且確認者接受，否則用「檢查輸出、角色的證據、執行者的 `NOTES.md`、最近的工具紀錄」組成「從哪裡繼續」的提示送回去。執行者連續 `--stall` 秒沒有進展，就換一個新的 session 並交接（這就是跨 session 的記憶）。`--condition S`（單一 agent）和 `N`（泛用的「檢查一下」提醒）是對照組。實驗程式：[`bench/p23`](bench/p23)。

## 範例：投影片團隊

[`examples/slide_team`](examples/slide_team) 讓團隊重畫一張資訊圖：TheAiEdge.io 的「LLM Serving: When to Split Prefill and Decode」（原圖請自備，repo 裡沒有）。兩位畫圖者輪流畫，美術比對我們的圖和原圖，最後由監工程式決定保留哪一版。試過兩種架構：

- **寫程式**（`slide_team.py`）：畫圖者寫一支畫投影片的 Python 程式。qwen3 8B 畫圖者從來沒畫出可用的投影片：語法錯誤、用錯輔助函式、把做好的部分丟掉，而且照樣回報完成。
- **元件清單**（`layout_team.py`）：畫圖者只把逐項清單（[`layout/SPEC_portrait.md`](examples/slide_team/layout/SPEC_portrait.md)）轉成 JSON 元件清單。`components.py` 檢查清單（錯誤訊息直接說怎麼改）並畫成 SVG；`imgcmp.py` 把畫出來的圖和原圖逐格比對（用標準函式庫讀 PNG，另外也算 PSNR）；修改後分數變好才保留。程式抓到的每個錯誤都寫進教訓清單，放在之後每一次指令的最前面。`--backend codex` 改用 Codex CLI 當成員；`--sessions fresh`（預設）每輪都開新 session。

每個團隊各跑一次的結果（分數是和原圖顏色相符的格子比例，1 代表完全相同）：

| 團隊 | 整張 | 各排：初稿 → 保留 |
|---|---|---|
| 畫圖 qwen3 8B、美術 Qwen3-VL 8B（OpenCode） | 0.667 | 0.763 → 0.763、0.728 → 0.728、0.513 → 0.513 |
| 畫圖與美術都是 Codex | 0.781 | 0.775 → 0.864、0.834 → 0.853、0.562 → 0.634 |
| 同一次 Codex，只用初稿 | 0.720 | |
| 照清單手寫的版面 | 0.720 | |

看到的事：讓結果從「沒有投影片」變成「可用」的，是表示方式（JSON 元件交給測過的程式來畫）。只保留較好的版本，擋下了每一次讓圖變差的修改（qwen 的 4 次有效修改全部、Codex 6 次中的 3 次）。修改要有幫助，前提是美術的意見具體又正確；本機視覺模型大多給不出意見（4096 個輸出 token 都用在思考）。每個團隊只跑一次，還不能當成普遍結論；分數也有盲點：細框線顏色錯了，分數仍可能上升。

執行：`python3 examples/slide_team/run_demo.py --reference 原圖.png --open-slide open-slide-py 的路徑`（加 `--backend codex` 改用 Codex）。渲染用無頭 Chrome；OpenCode 團隊需要 `examples/slide_team/Dockerfile` 建的映像，以及 `run_demo.py` 裡寫的兩個 Ollama 模型。

分數現在也看得到框線和字的顏色。`layout_team.py --score strict`（預設）只在 `scoring.py` 的 strict = (fill + stroke + text) / 3 上升時保留修改：fill 是上面的逐格比對，stroke 和 text 只看邊緣像素。在 Codex 那次的第 3 排，初稿的 strict 是 0.843，把 Prefill 框的框線和標題改成藍色的那次修改是 0.777（match 卻是 0.562 → 0.634），所以 strict 會留下初稿；只把那個框線和標題改回橘色，match 只從 0.6345 變 0.6368，strict 從 0.777 升到 0.861。`--score match` 是原本的規則。
畫圖者也可以設定框線粗細、標題顏色、虛線框、空心方塊、較高的格子、圓角箭頭、斜體字，還可以用 `svg` 元件：畫之前會先檢查內容（只收 SVG 繪圖元素，不收程式、事件和動畫，連結只能指向 `#id` 或內嵌的 png/jpeg/gif），再從解析後的結構重新寫出。

要比較不同團隊，就把一次執行寫成設定檔再重複跑：`python3 examples/slide_team/teamrun.py spec.json --repeat 5`。設定檔寫原圖、成員（畫手和一位美術，各自指定後端 `opencode`、`codex`、`claude` 或 `fake`、模型，以及每回合新開或沿用對話）、回合數、OpenCode 伺服器（teamrun 每次執行都自己啟動一個 herdr-py 常駐程式，但不會啟動 OpenCode）、保留修改用哪個分數（`rounds.score`：`strict` 或 `match`）和輸出資料夾；格式寫在 [`teamrun.py`](examples/slide_team/teamrun.py) 開頭。每次執行產生 `report.md`（每列草稿 -> 保留的分數、被接受與被擋下的修改、修正次數、教訓、整張圖的吻合度與 PSNR、每位成員的 token 和時間），重複執行另外產生 `aggregate.md`（平均、最小、最大）；`teamrun.py --compare 甲 乙` 把兩組結果並排印出。數字都由程式從每次執行的檔案算出（`runreport.py`），不手寫。Claude Code 成員（[`claude_agents.py`](examples/slide_team/claude_agents.py)）每回合跑一次 `claude -p`，不載入這台機器的 CLAUDE.md、hook 和 MCP 伺服器（`--safe-mode`），也不給工具，只有美術看圖時可以用 Read；`fake` 成員照腳本回答，用來試跑而不呼叫任何模型。

## 除錯：成員哪裡出了問題、該怎麼辦

`python3 -m herdr_py.diagnose 執行資料夾` 讀一次執行留下的紀錄（常駐程式的 `state/events.jsonl`、Codex 成員的 `work/codex/events.jsonl`、團隊的 `work/chat.jsonl`），列出每位成員的輪數、token、有效輸出與被採用的修改，以及找到的問題；每個問題都附上證明它的紀錄行號和一個建議。規則來自真實執行：輸出停在 token 上限而沒有文字、整輪只有思考（沒有文字也沒有工具呼叫）、對話中途被壓縮而回覆變成摘要、回覆不是要求的格式、超過每輪時間上限、整輪因錯誤失敗、同一個工具錯誤或權限拒絕一再發生、同一輪的程式檢查沒過卻宣稱完成、修改一再被退回、什麼都沒改就停下。每一輪最多只歸一個結果，最具體的原因優先。`--member drawA --turn 4` 印出那一輪的完整紀錄（指令開頭、回覆、思考長度、工具呼叫、token、結束原因、壓縮、錯誤、之後的檢查）；`--json` 給程式讀；`herdr_py.diagnose.findings(run_dir)` 回傳 dict 清單。

## 團隊即時面板

一頁唯讀網頁，在手機或電腦上即時看一次團隊執行：成員卡（狀態、token、輪數、用過幾個 session、最後說的話）、可依成員與種類篩選的對話時間軸、每一排的分數圖（初稿與每次修改、接受或退回、保留的版本）、我們的圖與原圖並排（滑桿切換每個保留的版本）、教訓清單與最後的數字。變化用 Server-Sent Events 推送。

```bash
python3 -m herdr_py.dashboard ~/.cache/herdr-slides/<某次執行> [--port 8770] [--socket <常駐程式的 socket>]
# 會印出  open: http://127.0.0.1:8770/#token=...
```

RUN_DIR 是 `run_demo.py` 產生的執行資料夾（或 `layout_team.py --workdir`）。成員狀態來自 `work/codex/agents.json`（Codex 成員），加 `--socket` 則來自 herdr-py 常駐程式。`summary.json` 要到結束才寫，在那之前分數從對話讀出，所以圖會隨執行更新。和常駐程式的網頁一樣只聽 127.0.0.1，而且要用印出的連結裡的 token；只提供 RUN_DIR 裡的圖片檔、不跟隨任何符號連結（執行資料夾裡也有常駐程式的 `state/token`），沒有任何寫入的端點。

## 部署到 RHEL 8

- RHEL 8 的 `python3` 是 3.6（`/usr/libexec/platform-python` 一定在）：herdr-py 可以用，設定檔請用 JSON。
- socket 要放在本機檔案系統（預設的 `~/.local/state` 即可）。有些網路或虛擬機共享的檔案系統不能放 Unix socket。
- OpenCode 的 glibc 版可以在 RHEL 8 上跑（實測 1.18.32，x86_64 與 aarch64，完成真實對話）；musl 版不行。
- systemd 使用者服務範例：[`examples/herdr-py.service`](examples/herdr-py.service)。網頁介面預設只聽 127.0.0.1，要遠端看請用 SSH 通道，不要開放連接埠。
- `LANG=C` 之下 Python 3.6 輸出中文會出錯：herdr-py 會自動改用 UTF-8 輸出（已在 RHEL 8 的 C 語系下測試）。

## 測試

```bash
python3 -m unittest discover -s tests        # 254 項，用假的 OpenCode 伺服器和假的 Codex、Claude Code CLI，不需要模型
python3 bench/p23/validate.py                # 在 RHEL 8 映像裡驗證實驗評分程式（需要 Docker）
```

GitHub Actions（`.github/workflows/tests.yml`）會在 RHEL 8 自己的 Python 3.6（UBI 8 容器）和最新版 Python 上跑測試；`scripts/ci_privacy.py` 會擋下帶 Claude 署名、家目錄路徑或 GitHub noreply 以外信箱的提交。

## 限制

常駐程式只支援 OpenCode（投影片團隊範例可以自己驅動 Codex CLI 和 Claude Code 成員）。agent 提出的問題只能駁回、不能回答。介面只顯示最近的活動，完整對話用 `herdr-py read`。團隊模式還在實驗階段，請先看實驗結果再依賴它。

MIT 授權。
