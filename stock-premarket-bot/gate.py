"""在安裝套件前先檢查時間窗與「今日是否已推播」（只用標準庫），不需執行就跳過後續步驟。

用法：python gate.py <us|tw> <force:true|false> [state_dir]
state_dir 內的 last_sent_<market>.txt 記錄最近一次成功推播的當地日期（由 workflow 從 bot-state 分支取出）。
"""
import os
import sys
from pathlib import Path

from src.scheduler import check_window

market = sys.argv[1]
force = len(sys.argv) > 2 and sys.argv[2] == "true"
state_dir = Path(sys.argv[3]) if len(sys.argv) > 3 else None
sent = None
if state_dir and (state_dir / f"last_sent_{market}.txt").exists():
    sent = (state_dir / f"last_sent_{market}.txt").read_text().strip()

status, mins, why = check_window(market, sent_date=None if force else sent)
if force:
    status = "run"
print(f"[gate] market={market} force={force} last_sent={sent} -> {status.upper()}：{why}")
if os.getenv("GITHUB_OUTPUT"):
    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        f.write(f"run={'true' if status != 'skip' else 'false'}\nstatus={status}\n")
