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

### 不能 clone 的機器

`scripts/oneshot.py` 會產生一支 bash 腳本，裡面裝著某個 commit 的所有檔案：把這一個檔案複製到那台機器上執行，它就會寫出整個 repo，最後核對每個檔案的 sha256。複製過程（貼上、寄信）有任何字元走樣，都會指出是哪些檔案。

```bash
python3 scripts/oneshot.py                     # 產生 herdr-py-<commit>-packed.sh 與 herdr-py-<commit>-files.sh
# 在那台機器上：
bash herdr-py-<commit>-packed.sh && cd herdr-py && ./scripts/install.sh
```

壓縮版（檔案打包成 tar.gz 再轉 base64，約 0.75 MB）在那台機器上需要 tar 和 gzip；可讀版（約 2 MB：用 mkdir 建資料夾，每個檔案一段 here-document，可以直接看）只需要 bash 和 coreutils。兩者都沒有超過 1000 bytes 的行，貼進終端機也不會被截斷。

## 快速開始

```bash
# 1. 啟動 OpenCode 伺服器（在你要 agent 工作的資料夾），設成 bash、編輯等動作之前先詢問：
#    用 OpenCode 的預設值時這些動作不會詢問，herdr-py 的權限策略根本看不到
OPENCODE_SERVER_PASSWORD=change-me OPENCODE_CONFIG_CONTENT='{"permission": {"edit": "ask", "bash": "ask", "webfetch": "ask", "external_directory": "ask", "doom_loop": "ask"}}' \
    opencode serve --port 4096 &
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

`agent.start` 加上 `directory` 時，session 在那個資料夾工作，而不是 OpenCode 自己的資料夾（OpenCode 的 `?directory=`；這種 session 的事件只出現在 OpenCode 的 `/global/event`，daemon 聽的就是它）；`deny` 列出這個 agent 一律被擋的權限種類，在問權限策略之前就擋（只讀的 agent 用 `["edit", "bash"]`）。`folder.list` 回傳 OpenCode 在某個資料夾列出的內容，看不到那個資料夾就回錯誤。

## 權限策略

OpenCode 只會為它自己設定成 "ask" 的動作送權限請求給 herdr-py。用 OpenCode 的預設值時（OpenCode 1.18.32 實測），bash 指令和編輯直接執行：策略拒絕的 curl 照樣跑了。啟動 OpenCode 時要設 `"permission": {"edit": "ask", "bash": "ask", "webfetch": "ask", "external_directory": "ask", "doom_loop": "ask"}`（見快速開始）。`herdr-py serve` 會讀 OpenCode 的設定，有任何一項不是 "ask" 就警告；`herdr-py status` 的 `opencode_does_not_ask` 也列得出來。

規則依序比對，第一條符合的生效。`match` 是正規表示式，必須比對整個目標（`bash` 是指令本身，其他是請求的路徑）；`agent` 是名稱萬用字元。動作：`allow`（放行一次）、`always`、`deny`（agent 會讀到理由）、`ask`（等人決定）。JSON 在任何 Python 版本都能用；TOML 要 Python 3.11 以上。允許的指令不要包含 `; | & $` 這類 shell 運算子。

## 團隊模式（監工）

`herdr-py team task.json --condition T --workdir 資料夾` 用三個角色做一題：執行者（可改檔）、唯讀的驗證者（解釋公開檢查為什麼沒過）、唯讀的確認者（對照原始需求）。監工是程式不是模型：執行者停下時跑公開檢查；除非檢查通過而且確認者接受，否則用「檢查輸出、角色的證據、執行者的 `NOTES.md`、最近的工具紀錄」組成「從哪裡繼續」的提示送回去。執行者連續 `--stall` 秒沒有進展，就換一個新的 session 並交接（這就是跨 session 的記憶）。`--condition S`（單一 agent）和 `N`（泛用的「檢查一下」提醒）是對照組。實驗程式：[`bench/p23`](bench/p23)。

## 協作迴圈與團隊知識庫

`python3 -m herdr_py.coop` 把「照規則跑的迴圈」（一個 agent、固定規則、每步檢查）變成團隊：規則變成**評分程式**，替每個答案打分；每個成員下一輪的指令裡，還會附上隊友找到、而且評分程式驗證過的成果。成員可以混用 OpenCode（透過 daemon）、Codex CLI、Claude Code CLI，或**任何程式**：從 stdin 讀指令、從 stdout 回答，你既有的迴圈腳本也可以。

```sh
# 正方形裡放 26 個圓（examples/coop）：兩個程式成員，不需要模型
python3 -m herdr_py.coop --task examples/coop/packing_task.md --judge "python3 examples/coop/packing_judge.py" \
    --mode C --rounds 3 --out runs/c1 \
    --member 'a=command:python3 examples/coop/packing_member.py --seed 1' \
    --member 'b=command:python3 examples/coop/packing_member.py --seed 2'
# 換成 agent：--member x=codex  --member y=claude:haiku  --member z=opencode:ollama/qwen3-8b-32k:latest --socket SOCK
python3 -m herdr_py.teamkb runs/c1/kb     # 每一筆：分數、誰接了誰的成果、哪些是重送
```

- **三種模式的成員回合數相同**，分享有沒有幫助是量出來的，不是假設的：`S` 一個成員拿全部回合；`I` 各做各的，只看得到自己的答案；`C` 看得到全隊驗證過的答案和失敗。每輪是一波：簡報在這輪開始前組好，成員同時跑。
- **評分程式**的最後一個參數是答案檔，印出 `{"status": "valid"|"invalid", "score": ...}`；`--judge-mode exit` 改用通過／不通過檢查的結束碼。評分程式自己崩潰或卡住，記成「評分錯誤」，不算答案不合法；成員自己宣稱的分數一律不用。
- **知識庫**（`herdr_py/teamkb.py`：只能新增的事件紀錄，答案依內容 hash 存放）保存每個答案、失敗和判定。同一個答案重送只記一次、不重評；成員只能把「簡報裡給它看過的條目」列為父條目；提交後被換掉的答案檔不會被當成原檔評分；多個程序可以同時寫入。指標：採用率、採用後進步率、重複率。
- 每一輪記在 `run.jsonl`（狀態、秒數、token、條目、判定，以及沒交出東西的原因）；逾時或出錯的回合不會貢獻答案。`summary.json` 有總計和每輪結束時的最佳分數。

## 事件驅動的團隊：共享待辦清單，planner 被事件喚醒

`python3 -m herdr_py.engine` 把 coop 的「一輪一輪」換成「事件」（definitions §23）。planner（任何一種成員後端都可以）在團隊知識庫裡維護一份共享待辦清單。每當有答案被評分、或有待辦結束，planner 就被喚醒：程式從紀錄組出團隊目前的狀態給它，它回覆要新增或撤下哪些待辦，或宣告完成；回覆裡寫了不存在的成員或條目、或開了太多待辦，就附上全部理由退回。planner 不是每個結果都被喚醒：只有在有待辦結束、而且有成員閒著卻沒有能領的待辦時，或結束的待辦數和成員數一樣多（全隊跑完一圈）時才喚醒；它的指令會寫出誰在做什麼、誰閒著。待辦可以排在別的待辦之後（"after"：待辦 id，或用 "#2" 指同一次回覆的第二條），那些待辦結束前不能領。標了 "review": true 的待辦不會派給做出被評內容的人（它參照的成果的作者、它要等的待辦的執行者），沒有人評自己的作品；指定給作者自己的評論會被退回；開著的待辦沒有人能領時會叫醒 planner（喚醒次數用完就停止）。`--time-limit S` 讓 S 秒後不再開始新工作。執行中的團隊可以由人介入：`python3 -m herdr_py.engine --control RUN_DIR pause`（不再領新待辦、不叫醒 planner，正在跑的回合照樣做完）、`resume`、`stop`、`turns N`、`time_limit S`；或用 `python3 -m herdr_py.engineview RUN_DIR --serve` 開一個附按鈕的頁面（只聽本機，指令要帶它印出的連結裡的權杖）。每個指令都記在 engine.jsonl、列在頁面上；暫停的時間不算成員在等工作。planner 和成員可以是任何一種後端；planner 的回合只回答：claude planner 沒有工具（`--tools ""`），opencode planner 的每則指令都帶 OpenCode 的工具開關、全部關掉（`{"*": false}`；用 OpenCode 1.18.32 實測過，同一則指令不關工具時會呼叫 webfetch）。成員一有空就領一條指定給自己或不指定的最舊待辦，所以快的成員不必等慢的；同一條待辦不會被兩個成員領走（在知識庫的檔案鎖底下挑選並寫入）。成員做事時，資料夾裡的 `TEAM_BOARD.md`（唯讀）顯示最新的已驗證結果、失敗與待辦，每次事件後由程式重寫。和 coop 一樣，只有評分程式的判定會被共享；planner 的待辦只是安排，不是事實。

```
 團隊知識庫：條目、判定、待辦（只增不改）
    │ 有答案被評分、有待辦結束
    ▼
 喚醒 planner → 新增或撤下待辦（程式先檢查）
    ▼
 有空的成員領待辦 → 作答 → 評分 → 寫回
    │（做事時 TEAM_BOARD.md 隨時是最新狀態）
    └────→ 下一個事件
```

```sh
python3 -m herdr_py.engine --task examples/coop/packing_task.md --judge "python3 examples/coop/packing_judge.py" \
    --planner plan=claude --member a=claude --member b=claude --turns 4 --out runs/e1
python3 -m herdr_py.teamkb runs/e1/kb     # 條目、判定與待辦

# 完全不用模型：planner 和成員都是程式（約 20 秒）
python3 -m herdr_py.engine --task examples/coop/packing_task.md --judge "python3 examples/coop/packing_judge.py" \
    --planner 'plan=command:python3 examples/engine/planner.py' \
    --member 'a=command:python3 examples/coop/packing_member.py --seed 1 --steps 40000' \
    --member 'b=command:python3 examples/coop/packing_member.py --seed 2 --steps 120000' \
    --member 'c=command:python3 examples/coop/packing_member.py --seed 3 --steps 300000' --turns 9 --out /tmp/e-demo
open /tmp/e-demo/view.html

# 全部用 OpenCode 的團隊，經「快速開始」的 daemon（預設 socket）：成員在這次執行的看板資料夾唯讀工作
# （--member-access research：再加上 daemon 策略允許的上網抓取）；容器裡的 OpenCode 要把執行資料夾掛在同一路徑
python3 -m herdr_py.engine --task examples/coop/packing_task.md --judge "python3 examples/coop/packing_judge.py" \
    --planner plan=opencode --member a=opencode --member b=opencode:ollama/qwen3-8b-32k:latest \
    --socket ~/.local/state/herdr-py/herdr-py.sock --turns 4 --out runs/e2
```

`view.html`（每個事件後重寫，執行中也能看；統一的深色外觀，寬螢幕兩欄、手機一欄）畫出：這次執行的數字標在迴圈上；每個 agent 一列、附狀態燈（最後一個回合：綠＝通過、紅＝失敗；工作中是白色）；這次執行的數字（回合、沒加任何待辦的喚醒、被退回的回覆、回合時間中位數、成員閒著的時間、token），全部來自紀錄；誰做了哪條待辦（每個成員一欄，連到它要等的、要接著做的待辦）；知識庫的流向（誰寫了每個成果、skill 排最前面並顯示名稱、誰接著做或打開了它的檔案；每個 agent 那列也寫出它寫了、讀了哪些 skill）；時間由上往下的時間軸（planner 和每個成員各一欄，每個回合一根長條，每個喚醒 planner 的結果一條虛線）；最佳分數隨時間的變化；每條待辦從新增到結束；planner 的每一回合。沒有 JavaScript 時內容完整；有 JavaScript 時多一個播放器，從第一個事件重播到結束：拖曳時間，迴圈上正在工作的那一段會亮起、數字跟著時間變；點長條可以看那一回合的待辦、判定與分數。頁面也畫出每一回合用了哪些工具（長條裡每次網路搜尋、抓網頁、讀檔各一個點，來自 Claude 與 OpenCode 的紀錄），以及團隊做出的每個成果和它接了誰的成果（從上游畫一條線到下游）；答案第一行寫 `ARTIFACT: <種類>` 的，依種類分欄。

停止條件：成員回合用完、達到目標分數（`--target`）、planner 宣告完成、planner 的喚醒次數用完而且沒有待辦、或連續 `--patience` 個判定都沒有超過最佳分數。達標時，還在朝目標做的成員回合會被取消（Claude 與程式成員；待辦以 dropped 結束並寫明原因，不算失敗，也不會單獨叫醒 planner）：它們在做的東西已經不需要了。planner 下一次喚醒結束前，成員不接新待辦，讓 planner 先丟掉那些朝目標做、還沒開始的待辦。加上 `--wrap-up N`，達標後會再叫醒 planner 一次，最多再跑 N 個成員回合把有效的做法寫成 skill，然後停止。`--repairs N` 讓成員在同一回合、同一段對話裡修正被判不通過的答案（讀過的東西都還在），並告訴它評分說了什麼；`--repair-below 分數` 讓通過但低於這個分數的答案也修（`--repair-kind 種類`：只修這種成果，分數固定的 skill 不會被拿去修到程式的門檻；通過的答案修完分數沒提高就停止修正）。每次修正都是接著上一版的新版本，回合用最好的那一版結束（Claude 與程式成員；Codex、OpenCode 成員不修）。成員會知道這回合最多能跑多久（超時就全部丟失），Claude 成員也會知道自己有哪些工具、全隊都不能執行指令（寫的 skill 才是隊友做得到的）。planner 會知道還剩幾次喚醒（最後一次要把剩下回合的事一次派完），並被提醒：同一份工作不同時派給兩個人、需要隊友正在查的東西就排在那條待辦之後、研究題不要把自己猜的答案寫進待辦。成員也可以替彼此做工具：第一行是 `ARTIFACT: mcp` 的通過答案是一個單檔 MCP 伺服器，給了 `--mcp-sandbox 指令` 時，引擎先在那個沙盒裡啟動它一次（`{file}` 工具的程式、`{kb}` 知識庫、`{board}` 看板資料夾；例如不連網、檔案唯讀的 `docker run`），問 server/discover 與 tools/list（2026-07-28 版），有回應的工具就提供給 Claude 成員之後的回合（同一系列只提供最新版；提示裡會列出）。這種回合用 `--restricted --strict-mcp-config` 取代會把所有 MCP 伺服器擋掉的 `--safe-mode`：用 Claude Code 2.1.295 實測，上下文大小一樣、沒有 CLAUDE.md 與 skill，成員也真的呼叫到工具。沒給沙盒指令，成員寫的工具一律不執行。`python3 -m herdr_py.rundiag RUN_DIR` 讀一次執行的紀錄，說出浪費與出錯的地方：同一個原因被判不通過的答案、沒贏過最高分也沒人接著做的成果（同一份工作做了兩次）、沒人用到的 skill、被不只一個成員打開的網頁、最高分出現後的回合、planner 喚醒用完時剩下的回合、閒著等工作的成員、被退回的 planner 回覆、回合內修正、什麼都沒留下的回合（超時；被強制結束的回合，token 數是下限）與達標時被取消的回合、引用同樣句子的 skill 組合（同一套規則寫了兩次；同名的改版不算），以及從其他 task 帶來的檔案有沒有人打開。`summary.json` 會算：被領兩次的待辦數（必須是 0）、對得到待辦、看板版本與指令 hash 的回合比例（必須全部）、每個成員「有空卻沒有待辦可領」的時間、planner 占全部 token 的比例，以及成員讀看板的次數（從 Codex、Claude、OpenCode 的紀錄算；程式成員沒有紀錄）。每個已驗證的結果也會變成看板資料夾裡的檔案（`artifacts/<id>.txt`），太長放不進指令的成果，隊友也能打開來看。`--member-access research` 讓 Claude 成員也能上網查資料（WebSearch、WebFetch，用 settings 規則只允許這兩個；真的 CLI 實測：沒有規則時不詢問模式會拒絕 WebSearch，有規則時讀資料夾外的檔案仍然被擋）。Codex 成員維持唯讀。

## 筆記本：每天交出去的工作，一件一頁

`python3 -m herdr_py.notebook` 把交給團隊的工作記成一本筆記本（一個資料夾）裡的頁。一頁是一件工作：它的定義（`page.json`：
目標、任務檔、評分程式、團隊、每次執行的預算、哪些種類延續到下一次、哪些種類是成果）、每一次執行，以及人的決定（附加在這頁的
`history.jsonl`，記下誰、什麼時候）。草稿不會執行：定義或任務在上次核准後改過，`run` 就拒絕。每次執行從這頁最近一次延續
（或 `--from N`、`--fresh`）：引擎從那次通過的條目裡、這頁要帶的種類起步，不帶人排除的條目（`engine --seed-from`），
還沒被用過的批註加進這次的任務。每種成果的每一版都留著，由人選定現行版；下一次執行從現行版接著改（選定的版本不論種類都帶入，任務裡寫明從它改起）。一頁也可以從
其他頁帶參考資料：`"from": [{"page": "museum-report", "kinds": ["skill"], "current": true}]` 會在每次執行前，把那一頁最近一次
（或 `"run": N`）通過評分的指定條目複製到成員資料夾（`reference/<頁>/<條目>.txt`，清單在 `reference/INDEX.md`），並寫進任務（`"kinds": ["*"]` 帶全部通過的條目）；帶的是 skill 時，任務也會寫明：工作從它們開始，只重查它們沒寫到或寫錯的部分，要改進就改成新版，不要另寫同主題的 skill。
在「＋」開新 task 時勾的 skill、知識、現行版，起草時會變成新頁的 `"from"`。
`"loop": {"repairs": 2, "repair_below": 100, "target": 100, "wrap_up": 2}` 會把這些引擎選項（回合內修正、目標分數、達標後的收尾）帶進每次執行；「除錯」分頁每次執行的卡片最前面是回放診斷（`herdr_py.rundiag`）。
它們不算新頁已驗證的成果，也不沿用舊分數：那一頁的評分標準不同。示意筆記本裡，帶入博物館報表 skill 的圖書館報表用 4 個成員回合
做出滿分網頁，從零開始的同一份報表用了 5 個。等人處理的事由紀錄算出，不另外存：要核准的草稿、
結束後還沒人看過的執行（批註、選版、排除、驗收、擱置或完成都算看過）、沒有結束紀錄而且 15 分鐘沒寫東西的執行。

```
 你：一句目標 --> 起草 page.json --> 你核准 --> 團隊執行（第 n 次，照預算）
                                                     |
   第 n+1 次帶入通過的條目、skill 與你的批註 <-- 你驗收：選現行版、批註、排除、驗收
```

```sh
python3 -m herdr_py.notebook ~/notebook draft circles.json        # 這頁的草稿（page.json，欄位見英文說明）
python3 -m herdr_py.notebook ~/notebook run circles --dry-run      # 列出確切的引擎指令與任務
python3 -m herdr_py.notebook ~/notebook approve circles
python3 -m herdr_py.notebook ~/notebook run circles                # 第 1 次；第 2 次從它延續
python3 -m herdr_py.notebook ~/notebook note circles "從六角形排法開始試"
python3 -m herdr_py.notebook ~/notebook pick circles k0123456789ab # 這個種類的現行版
python3 -m herdr_py.notebook ~/notebook attach circles runs/e1     # 在筆記本外跑的執行
python3 -m herdr_py.notebook ~/notebook status
python3 -m herdr_py.notebook ~/notebook view --out site/ --runs runs/   # --runs：列出不屬於任何一頁的執行
```

`view` 把筆記本寫成給電腦螢幕看的 app，不需要 JavaScript 也完整：左邊一排是所有 task（標題第一個字或 page.json 的 `icon`，
加上狀態點），最下面的「＋」開新 task。首頁一開始是「全部 task」：先列各狀態的數量，再每個 task 一列（需要人處理的排前面），
寫著狀態與要你做的事、最近一次執行（何時結束、最好的成果、花費）、每種成果的現行版；下面才是附按鈕的「待你處理」。點開的 task
佔滿右邊，分成幾個分頁。總覽：由紀錄算出的數字（執行次數、成員回合、通過的條目、
待你處理），每個 Agent 一張卡片，寫著紀錄裡的狀態（最後一個回合怎麼結束，執行中則是在做或在等）和它的待辦；點卡片從右側打開它的
面板：角色、待辦、寫了和用了哪些 skill、產出、每個回合。架構：團隊樹狀圖（planner 分派待辦、成員回答、評分決定什麼通過；圖裡每個
Agent 都能點開面板）。回放：每次執行的回放（engineview 的頁面，直接在分頁裡）。除錯：每次執行出了什麼錯（被退回的 planner 回覆和理由、
沒交答案的回合、沒過的答案和評分的理由、成員回報做不到），最後是執行設定與原始指令。skill：誰寫的、誰用過、帶進第幾次。知識庫：所有
通過的條目，可搜尋、依種類篩選，以及知識圖（每個種類一列、每條一個圓、從每條連到引用它的條目；點一個圓，它引用的和引用它的會亮起來）。
接著是每種成果的每一版並標出現行版，以及批註與歷史。被人排除的版本會標出「已排除」與原因，不會被提供成現行版（取消排除之前 `pick` 也會拒絕）。文字檔打開的是一頁宣告 UTF-8 的閱讀頁（頁上連到原始檔）：
`python -m http.server` 這類靜態伺服器送 `.txt` 時不寫編碼，設成繁體中文的手機就會把 skill 當 Big5 讀成亂碼。紀錄裡沒有的東西不顯示（不猜進度、不猜完成時間）。`notebook.json` 可以指定
筆記本自己的樣式表（`"style"`，相對於筆記本資料夾的 CSS 檔），接在內建樣式後面套到每一頁，例如手機版版面；`"tree"` 決定團隊圖
橫著畫（`wide`，預設）、直著畫（`tall`），或兩種都畫（`both`：直式先藏著，由這類樣式表決定何時顯示）。`serve` 在這台機器上
即時提供同樣的頁面（預設 127.0.0.1），並加上按鈕：＋ 記下一個新 task 的請求；task 裡的按鈕可以選現行版、寫批註、驗收、擱置、
重新打開、核准（只核准頁面上顯示的那一版）、排除條目。按鈕帶著連結 `#` 後面的 token 送出，沒有 token 就拒絕；團隊做的檔案在沙盒裡
提供（`Content-Security-Policy: sandbox allow-scripts`），裡面的程式碰不到 token。用 `view` 寫成檔案時，按鈕改成複製要跟 Claude 說的話。

```sh
python3 -m herdr_py.notebook ~/notebook request "下雨週末的書單" --title "書單"
python3 -m herdr_py.notebook ~/notebook draft reading.json --request r1   # Claude 起草的頁回應這個請求
python3 -m herdr_py.notebook ~/notebook serve --runs runs/                # 會印出  open: http://127.0.0.1:8790/#token=...

# 虛構的示意 task，成員都是程式（幾秒鐘）：第二次執行用第一次留下的資料與 skill 做出網頁的報表、跑兩次的圓形排列、
# 有一步失敗的 DAG、一份草稿與一個請求；接著打開來看
python3 examples/notebook/make_demo.py /tmp/notebook-demo
python3 -m herdr_py.notebook /tmp/notebook-demo/notebook view --out /tmp/notebook-demo/site --runs /tmp/notebook-demo/runs
open /tmp/notebook-demo/site/index.html
```

資料夾裡的 `notebook.json` 設定標題與頁面語言（`en` 或 `zh-TW`）。指令是給一台機器上的一個人用的；事後補記的決定（`--at`）會標成補記。

## DAG 分派：會互相等待的步驟

`python3 -m herdr_py.dag` 執行一份**計畫**：每個步驟由一個成員在**自己的 git clone** 裡做，由**評分程式**判定過不過。一個步驟要等它需要的步驟全部通過才開始，而且只看得到那些步驟的成果。計畫是一個 JSON 檔，所以團隊怎麼組織（誰做什麼、誰等誰、誰看得到誰的成果）是可以修改、可以拿來比較的資料。

```
mul ─┐                     每個方塊是一個步驟，由一個成員在自己的 clone 裡做；
sub ─┴─> together ─┐       箭頭：那個步驟通過之後才開始，並拿到它的 commit
div ───────────────┴─> docs
```

```sh
python3 examples/dag/make_demo.py /tmp/dag-demo          # 一個小 repo 和它的計畫（程式成員，不需要模型）
python3 -m herdr_py.dag /tmp/dag-demo/plan.json --check  # 一層一層列出步驟
python3 -m herdr_py.dag /tmp/dag-demo/plan.json --out /tmp/dag-demo/run1 --parallel 3
open /tmp/dag-demo/run1/view.html                         # 計畫畫成圖，加上每一次嘗試
python3 -m herdr_py.dag --recheck /tmp/dag-demo/run1      # 在全新的 clone 把每個通過的步驟重新評分
```

- **隔離由程式負責**：每次嘗試都有自己的 `git clone --shared`，不留遠端；步驟的 clone 只會拿到它需要的步驟的輸出 commit（`refs/dag/<id>`），沒有任何其他步驟的東西，指令裡也只有那些步驟的摘要與 diff。Codex 成員在 `workspace-write` 沙盒裡工作；Claude 成員用 `--permission-mode acceptEdits` 加上檔案工具（唯讀步驟用 `dontAsk` 加讀檔工具）。用真的 Claude Code 實測過：在 clone 之外寫、讀、Glob、Grep 都會被擋；不能用 `--allowedTools`，它會讓成員讀寫整台機器。OpenCode 成員（透過 daemon，`--socket`）每一步開一個在 clone 裡工作的新 session（OpenCode 的 `?directory=`）；daemon 會在問權限策略之前，先擋掉它在 clone 之外的動作，唯讀步驟連編輯和指令都擋。這只在 OpenCode 會先詢問這些動作時才成立（見權限策略），所以每一步開工前先確認 daemon 說 OpenCode 會詢問，而且 OpenCode 列出的 clone 內容和這台機器看到的一樣（OpenCode 在容器裡的話，執行資料夾要掛載在同一個路徑）；不成立就停下並說明原因。OpenCode 1.18.32 實測：唯讀步驟的編輯、讀 clone 之外的檔案都被擋下（檔案沒變；讀外面的檔案時 OpenCode 送來的是 external_directory 請求）。Codex 的沙盒不限制讀取，所以每次執行都會數成員的工具紀錄（Codex、Claude、OpenCode）裡，指向別的步驟 clone 的次數（`summary.json` 的 `out_of_bounds`）。
- **步驟的檔案從哪裡開始**：不需要別的步驟 → 基準 commit；需要一個 → 接著那個步驟的輸出；需要好幾個 → 基準（兩種做法通常改同一批檔，由成員比較、整合），或指定 `"start": "merge"`（由程式先合併，衝突時這一步失敗並列出檔案）、或指定其中一個。重試會接著上一次的 commit，指令裡附上評分程式的說明。
- **輸出是程式做的 commit**，不是成員做的：回合結束時把 clone 裡的變更全部 commit，評分程式檢查這個 commit（和 `coop` 同一套約定：回覆檔是最後一個參數；印 JSON 判定，或用 `"judge_mode": "exit"` 看結束碼）。評分程式的檔案以計畫檔所在的資料夾為準、在所有 clone 之外，開始時記下 hash：執行中被改就是評分錯誤。
- **失敗**：一個步驟失敗，需要它的步驟都會被擋下，不相干的步驟照常進行。成員的後端壞掉、評分程式壞掉或 clone 建不起來，就不再開始新步驟（結束碼 3；`--keep-going` 只讓那一步失敗）；逾時或答案不合格是成員自己的失敗。步驟的時限只算成員自己的時間：OpenCode 成員遇到模型供應商失敗、OpenCode 回報重試時，回報之前的空白和重試本身算供應商的，會加回時限，最多再加一個時限；供應商佔掉更久，這一回合就以 `provider_stall` 結束，算環境壞掉（結束碼 3），不算成員失敗。模型只是回得慢，仍算成員的時間。OpenCode 1.18.32 加免費模型實測：一次供應商逾時讓一個步驟的 900 秒少了 300 秒。
- **收據與接續**：每次派工、交回、commit、判定都先寫進 `events.jsonl` 才往下走；`--resume` 從它重建狀態，已通過的步驟絕不重跑，因環境壞掉而失敗的步驟會重跑，計畫、題目或評分程式變了就拒絕接續。`summary.json` 只從事件算出「上游還沒通過就開始的次數」和「通過後又被派工的次數」（兩者都必須是 0），以及平行度。
- 不會合併進你的 repo，也不會推送：輸出是執行資料夾裡的 commit，要不要合併由你決定。

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

分數現在也看得到框線和字的顏色。`layout_team.py --score strict`（預設）只在 `scoring.py` 的 strict = (fill + stroke + text) / 3 上升時保留修改：fill 是上面的逐格比對，stroke 和 text 只看邊緣像素。在 Codex 那次的第 3 排，初稿的 strict 是 0.843，把 Prefill 框的框線和標題改成藍色的那次修改是 0.777（match 卻是 0.562 → 0.634），所以 strict 會留下初稿；只把那個框線和標題改回橘色，match 只從 0.6345 變 0.6368，strict 從 0.777 升到 0.861。`--score match` 是原本的規則。修改者拿到的仍是逐格比對的回饋（`--notes match`，預設）：換成 strict 的改顏色回饋時，Codex 的 12 次修改 strict 平均下降 0.072、沒有一次變好；用逐格回饋時 6 次有 4 次變好（同一晚，單尾 Fisher p = 0.005；同一次執行的修改不完全獨立，當成強烈的跡象）。
畫圖者也可以設定框線粗細、標題顏色、虛線框、空心方塊、較高的格子、圓角箭頭、斜體字，還可以用 `svg` 元件：畫之前會先檢查內容（只收 SVG 繪圖元素，不收程式、事件和動畫，連結只能指向 `#id` 或內嵌的 png/jpeg/gif），再從解析後的結構重新寫出。

要比較不同團隊，就把一次執行寫成設定檔再重複跑：`python3 examples/slide_team/teamrun.py spec.json --repeat 5`。設定檔寫原圖、成員（畫手和一位美術，各自指定後端 `opencode`、`codex`、`claude` 或 `fake`、模型，以及每回合新開或沿用對話）、回合數、OpenCode 伺服器（teamrun 每次執行都自己啟動一個 herdr-py 常駐程式，但不會啟動 OpenCode）、保留修改用哪個分數（`rounds.score`：`strict` 或 `match`）和輸出資料夾；格式寫在 [`teamrun.py`](examples/slide_team/teamrun.py) 開頭。每次執行產生 `report.md`（每列草稿 -> 保留的分數、被接受與被擋下的修改、修正次數、教訓、整張圖的吻合度與 PSNR、每位成員的 token 和時間），重複執行另外產生 `aggregate.md`（平均、最小、最大）；`teamrun.py --compare 甲 乙` 把兩組結果並排印出。數字都由程式從每次執行的檔案算出（`runreport.py`），不手寫。Claude Code 成員（[`claude_agents.py`](examples/slide_team/claude_agents.py)）每回合跑一次 `claude -p`，不載入這台機器的 CLAUDE.md、hook 和 MCP 伺服器（`--safe-mode`），也不給工具，只有美術看圖時可以用 Read；`fake` 成員照腳本回答，用來試跑而不呼叫任何模型。

## 除錯：成員哪裡出了問題、該怎麼辦

`python3 -m herdr_py.diagnose 執行資料夾` 讀一次執行留下的紀錄（常駐程式的 `state/events.jsonl`、Codex 成員的 `work/codex/events.jsonl`、團隊的 `work/chat.jsonl`），列出每位成員的輪數、token、有效輸出與被採用的修改，以及找到的問題；每個問題都附上證明它的紀錄行號和一個建議。規則來自真實執行：輸出停在 token 上限而沒有文字、整輪只有思考（沒有文字也沒有工具呼叫）、對話中途被壓縮而回覆變成摘要、回覆不是要求的格式、超過每輪時間上限、整輪因錯誤失敗、同一個工具錯誤或權限拒絕一再發生、同一輪的程式檢查沒過卻宣稱完成、修改一再被退回、什麼都沒改就停下。每一輪最多只歸一個結果，最具體的原因優先。`--member drawA --turn 4` 印出那一輪的完整紀錄（指令開頭、回覆、思考長度、工具呼叫、token、結束原因、壓縮、錯誤、之後的檢查）；`--json` 給程式讀；`herdr_py.diagnose.findings(run_dir)` 回傳 dict 清單。

## 團隊即時面板

一頁唯讀的 HTML 網頁，即時看一次團隊執行：成員卡（狀態、token、輪數、用過幾個 session、最後說的話）、可依成員與種類篩選的對話時間軸、每一排的分數圖（初稿與每次修改、接受或退回、保留的版本）、我們的圖與原圖並排（滑桿切換每個保留的版本）、教訓清單與最後的數字。變化用 Server-Sent Events 推送。

```bash
python3 -m herdr_py.dashboard ~/.cache/herdr-slides/<某次執行> [--port 8770] [--socket <常駐程式的 socket>]
# 會印出  open: http://127.0.0.1:8770/#token=...
python3 -m herdr_py.dashboard ~/.cache/herdr-slides/<某次執行> --html run.html
# 同一頁存成一個檔案（執行當下的樣子）：用任何瀏覽器打開，不需要伺服器或 token，不會更新
```

寬螢幕上，對話、分數圖和圖片三欄並排。存下來的頁面把圖片放在檔案裡（跑完、有六張圖的執行約 2 MB），只放圖片，不會帶到 `state/token`。

RUN_DIR 是 `run_demo.py` 產生的執行資料夾（或 `layout_team.py --workdir`）。成員狀態來自 `work/codex/agents.json`（Codex 成員），加 `--socket` 則來自 herdr-py 常駐程式。`summary.json` 要到結束才寫，在那之前分數從對話讀出，所以圖會隨執行更新。和常駐程式的網頁一樣只聽 127.0.0.1，而且要用印出的連結裡的 token；只提供 RUN_DIR 裡的圖片檔、不跟隨任何符號連結（執行資料夾裡也有常駐程式的 `state/token`），沒有任何寫入的端點。

## 部署到 RHEL 8

- RHEL 8 的 `python3` 是 3.6（`/usr/libexec/platform-python` 一定在）：herdr-py 可以用，設定檔請用 JSON。
- socket 要放在本機檔案系統（預設的 `~/.local/state` 即可）。有些網路或虛擬機共享的檔案系統不能放 Unix socket。
- OpenCode 的 glibc 版可以在 RHEL 8 上跑（實測 1.18.32，x86_64 與 aarch64，完成真實對話）；musl 版不行。
- systemd 使用者服務範例：[`examples/herdr-py.service`](examples/herdr-py.service)。網頁介面預設只聽 127.0.0.1，要遠端看請用 SSH 通道，不要開放連接埠。
- `LANG=C` 之下 Python 3.6 輸出中文會出錯：herdr-py 會自動改用 UTF-8 輸出（已在 RHEL 8 的 C 語系下測試）。

## 拿真的 OpenCode 伺服器驗證

測試用的是假的 OpenCode。`scripts/check_opencode.py` 透過執行中的 daemon，對真的伺服器跑 8 個情境（連線、跑完、續問、策略允許、策略拒絕、要人核准、中止、開新 session），而且把 herdr-py 的判定和模型有沒有照做分開記（模型不照指示做，不算 herdr-py 的錯）。OpenCode 1.18.32 加免費模型 opencode/big-pickle：8 項全過（2026-10-09）。

```sh
python3 scripts/check_opencode.py --socket SOCK --opencode http://127.0.0.1:4096 --password-file PASSFILE \
    --workdir OPENCODE_的工作資料夾 --model opencode/big-pickle      # daemon 的策略用 scripts/check_opencode_policy.json
```

## 測試

```bash
python3 -m unittest discover -s tests        # 539 項，用假的 OpenCode 伺服器和假的 Codex、Claude Code CLI，不需要模型
python3 bench/p23/validate.py                # 在 RHEL 8 映像裡驗證實驗評分程式（需要 Docker）
```

GitHub Actions（`.github/workflows/tests.yml`）會在 RHEL 8 自己的 Python 3.6（UBI 8 容器）和最新版 Python 上跑測試（repo 裡每個 Python 檔，包括範例和實驗，都要能在這兩版編譯、而且不能有警告）；`scripts/ci_privacy.py` 會擋下帶 Claude 署名、家目錄路徑或 GitHub noreply 以外信箱的提交。

## 限制

常駐程式只支援 OpenCode（投影片團隊範例可以自己驅動 Codex CLI 和 Claude Code 成員）。agent 提出的問題只能駁回、不能回答。介面只顯示最近的活動，完整對話用 `herdr-py read`。團隊模式還在實驗階段，請先看實驗結果再依賴它。

MIT 授權。
