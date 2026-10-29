"""在安裝套件前先檢查時間窗（只用標準庫），不在時段內就跳過後續步驟，節省 Actions 分鐘數。"""
import os
import sys

from src.scheduler import check_window

market = sys.argv[1]
force = len(sys.argv) > 2 and sys.argv[2] == "true"
ok, mins, why = check_window(market)
run = force or ok
print(f"[gate] market={market} force={force} -> {'RUN' if run else 'SKIP'}：{why}")
if os.getenv("GITHUB_OUTPUT"):
    with open(os.environ["GITHUB_OUTPUT"], "a") as f:
        f.write(f"run={'true' if run else 'false'}\n")
