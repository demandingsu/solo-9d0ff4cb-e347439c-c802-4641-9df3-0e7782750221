"""端到端业务流冒烟脚本（对真实 uvicorn 进程发 HTTP 请求）。

场景：
1. 导入两天行情 -> 批次1
2. 按截止批次1查询：无公告，折算值=原值
3. 导入 1拆2 公告（批次2，补报）-> 批次1 结果不变；最新批次价格减半量加倍
4. 同修订异值拒收 -> 422，无新批次
5. 高修订更正行情 -> 生效；低修订 -> 422
6. /docs 可访问
"""

import json
import sys
import urllib.request

BASE = "http://127.0.0.1:8077"


def call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        BASE + path, data=data, method=method,
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def check(name, cond, detail=""):
    print(("PASS" if cond else "FAIL"), name, detail)
    if not cond:
        sys.exit(1)


q = lambda d, c, rev=0, vol=1000.0: {
    "security_code": "600001", "trade_date": d, "revision": rev,
    "open": c, "high": c + 2, "low": c - 2, "close": c, "volume": vol,
}

# 1. 行情批次
s, r = call("POST", "/imports/quotes",
            {"quotes": [q("2026-01-02", 100.0), q("2026-01-06", 60.0, vol=2000.0)]})
check("导入行情 201", s == 201 and r["batch_id"] == 1 and r["row_count"] == 2, r)

# 2. 无公告时查询
s, r = call("GET", "/quotes/adjusted?security_code=600001&start_date=2026-01-01&end_date=2026-01-10")
check("无公告折算=原值", s == 200 and r[0]["adjusted_close"] == 100.0 and r[0]["factor"] == 1.0)

# 3. 补报公告 1拆2，除权日 2026-01-05
s, r = call("POST", "/imports/corporate-actions", {"actions": [{
    "security_code": "600001", "event_no": "SP2026-01", "ex_date": "2026-01-05",
    "ratio": 2.0, "revision": 0, "action_type": "split"}]})
check("导入公告 201", s == 201 and r["batch_id"] == 2, r)

s, old = call("GET", "/quotes/adjusted?security_code=600001&start_date=2026-01-01&end_date=2026-01-10&cutoff_batch=1")
check("旧批次不受补报影响", old[0]["adjusted_close"] == 100.0 and old[0]["applied_events"] == [])

s, new = call("GET", "/quotes/adjusted?security_code=600001&start_date=2026-01-01&end_date=2026-01-10")
check("新批次价格减半", new[0]["adjusted_close"] == 50.0 and new[0]["factor"] == 0.5)
check("新批次量加倍", new[0]["adjusted_volume"] == 2000.0)
check("事件依据完整", new[0]["applied_events"][0]["event_no"] == "SP2026-01"
      and new[0]["applied_events"][0]["ingest_batch_id"] == 2)
check("除权日当天不折算", new[1]["trade_date"] == "2026-01-06" and new[1]["factor"] == 1.0
      and new[1]["adjusted_close"] == 60.0)

# 4. 同修订异值拒收
s, r = call("POST", "/imports/corporate-actions", {"actions": [{
    "security_code": "600001", "event_no": "SP2026-01", "ex_date": "2026-01-05",
    "ratio": 4.0, "revision": 0, "action_type": "split"}]})
check("同修订异值 422", s == 422 and "同修订异值" in r["detail"], r)

# 5. 高修订更正 + 低修订拒收
s, r = call("POST", "/imports/quotes", {"quotes": [q("2026-01-02", 110.0, rev=1)]})
check("高修订行情 201", s == 201 and r["row_count"] == 1)
s, r = call("POST", "/imports/quotes", {"quotes": [q("2026-01-02", 110.0, rev=0)]})
check("低修订 422", s == 422 and "过期修订" in r["detail"], r)

# 批次清单
s, batches = call("GET", "/batches")
check("批次清单", s == 200 and [b["id"] for b in batches] == [1, 2, 3], batches)

# /docs
with urllib.request.urlopen(BASE + "/docs") as resp:
    check("/docs 可访问", resp.status == 200 and b"Swagger" in resp.read())

# ---------------- 入账（交割单核对） ----------------

tr = lambda no, d, side, sh, amt=None: {
    "trade_no": no, "trade_date": d, "direction": side, "shares": sh,
    **({"amount": amt} if amt is not None else {}),
}

# 7. 入账：买 100 股(总额 10000) -> 1拆2 -> 卖 150 股，结转 7500
s, r = call("POST", "/postings", {
    "account": "ACC001", "security_code": "600001", "closing_date": "2026-01-10",
    "cutoff_batch": 3,
    "trades": [tr("B1", "2026-01-02", "buy", 100, 10000.00),
               tr("S1", "2026-01-06", "sell", 150)]})
check("入账 201", s == 201 and r["cutoff_batch"] == 3 and r["trades_new"] == 2, r)
check("跨拆股结转成本", r["cost_carried"] == 7500.00, r)

s, ws = call("GET", "/postings/latest?account=ACC001&security_code=600001")
check("底稿可查询", s == 200 and ws["posting_id"] >= 1, ws)
check("底稿卖出明细", ws["allocations"][0]["buy_trade_no"] == "B1"
      and ws["allocations"][0]["shares_deducted"] == 150
      and ws["allocations"][0]["cost_carried"] == 7500.00, ws["allocations"])
check_ws = ws["positions"][0]
check("底稿结账日持仓", check_ws["remaining_shares"] == 50
      and check_ws["remaining_cost"] == 2500.00, check_ws)

# 8. 同号同值幂等；同号异值拒收
s, r = call("POST", "/postings", {
    "account": "ACC001", "security_code": "600001", "closing_date": "2026-01-10",
    "cutoff_batch": 3,
    "trades": [tr("B1", "2026-01-02", "buy", 100, 10000.00)]})
check("同号同值幂等", s == 201 and r["trades_new"] == 0, r)
s, r = call("POST", "/postings", {
    "account": "ACC001", "security_code": "600001", "closing_date": "2026-01-10",
    "cutoff_batch": 3,
    "trades": [tr("B1", "2026-01-02", "buy", 100, 9999.00)]})
check("同号异值 422", s == 422 and "同号异值" in r["detail"], r)

# 9. 公告更正(1拆4, 批次4) + 跨拆股补录买入 -> 换批次重算
s, r = call("POST", "/imports/corporate-actions", {"actions": [{
    "security_code": "600001", "event_no": "SP2026-01", "ex_date": "2026-01-05",
    "ratio": 4.0, "revision": 1, "action_type": "split"}]})
check("公告更正 201", s == 201 and r["batch_id"] == 4, r)
s, r = call("POST", "/postings", {
    "account": "ACC001", "security_code": "600001", "closing_date": "2026-01-10",
    "cutoff_batch": 4,
    "trades": [tr("B0", "2026-01-03", "buy", 50, 6000.00)]})
check("补录重算 201", s == 201 and r["trades_new"] == 1, r)
# B1: 100->400 股, 卖 150 结转 10000*150/400=3750；B0: 50->200 股剩余
check("更正后结转成本", r["cost_carried"] == 3750.00, r)
s, ws = call("GET", "/postings/latest?account=ACC001&security_code=600001")
pos = {p["buy_trade_no"]: p for p in ws["positions"]}
check("补录批次被拆股调整", pos["B0"]["remaining_shares"] == 200
      and pos["B0"]["remaining_cost"] == 6000.00, pos)
check("上次采用批次=4", ws["cutoff_batch"] == 4, ws)

# 10. 入账失败：股数不足 -> 整次作废，上次底稿仍可查
s, r = call("POST", "/postings", {
    "account": "ACC001", "security_code": "600001", "closing_date": "2026-01-10",
    "cutoff_batch": 4,
    "trades": [tr("S9", "2026-01-08", "sell", 99999)]})
check("股数不足 422", s == 422 and "股数不足" in r["detail"], r)
s, ws2 = call("GET", "/postings/latest?account=ACC001&security_code=600001")
check("失败后底稿不变", s == 200 and ws2["cost_carried"] == 3750.00, ws2)
s, ts = call("GET", "/trades?account=ACC001&security_code=600001")
check("失败不留成交", len(ts) == 3, ts)

# 11. 入账失败：结账日早于成交日
s, r = call("POST", "/postings", {
    "account": "ACC001", "security_code": "600001", "closing_date": "2026-01-01",
    "trades": []})
check("结账日过早 422", s == 422 and "早于成交日" in r["detail"], r)

print("\nALL E2E CHECKS PASSED")
