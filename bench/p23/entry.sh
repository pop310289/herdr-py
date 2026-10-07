#!/bin/bash
# Container entry for one P23 run (bash 4.4 / RHEL 8 compatible).
# usage: entry.sh TASK_DIR CONDITION    (mounts: /work, /state, /bench (ro), /opt/herdr-py (ro); env OPENCODE_CONFIG_CONTENT)
set -uo pipefail
task="$1"; cond="$2"
export HOME=/tmp/home PYTHONPATH=/opt/herdr-py PYTHONDONTWRITEBYTECODE=1
mkdir -p "$HOME"
pass="$(head -c 32 /dev/urandom | od -An -tx1 | tr -d ' \n')"
printf '%s' "$pass" > /tmp/oc-pass
OPENCODE_SERVER_PASSWORD="$pass" OPENCODE_DISABLE_AUTOUPDATE=1 OPENCODE_DISABLE_SHARE=1 \
  opencode serve --hostname 127.0.0.1 --port 4096 > /state/opencode.log 2>&1 &
python3 -m herdr_py --socket /tmp/hp.sock serve --opencode http://127.0.0.1:4096 --password-file /tmp/oc-pass \
  --policy /bench/policy.json --state-dir /state --questions reject --max-agents 6 --max-prompts 10 --wait 60 > /state/daemon.log 2>&1 &
for _ in $(seq 1 90); do [ -S /tmp/hp.sock ] && break; sleep 1; done
python3 -m herdr_py --socket /tmp/hp.sock team "$task/task.json" --condition "$cond" --workdir /work \
  --summary /state/summary.json --log /state/team.jsonl --wall "${WALL:-720}" --rounds 3 --stall 120 --checkpoint "${CHECKPOINT:-240}"
code=$?
python3 -m herdr_py --socket /tmp/hp.sock stop > /dev/null 2>&1
exit $code
