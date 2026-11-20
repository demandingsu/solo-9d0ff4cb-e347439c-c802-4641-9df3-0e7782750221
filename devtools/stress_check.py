"""对真实 uvicorn 进程打并发压力：同修订竞争导入 + 并发读。"""

import json
import threading
import urllib.error
import urllib.request

BASE = "http://127.0.0.1:8077"
N = 12


def post(path, payload):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return resp.status, json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def get(path):
    with urllib.request.urlopen(BASE + path) as resp:
        return resp.status, json.loads(resp.read())


results = []
barrier = threading.Barrier(N)

# 基线：脚本运行前已存在的最大批次号（e2e 先行时批次不止 3 个）
_, _batches = get("/batches")
BASE_BATCH = max((b["id"] for b in _batches), default=0)


def worker(i):
    payload = {"quotes": [{
        "security_code": "CONT", "trade_date": "2026-02-01",
        "revision": 0, "open": 10.0 + i, "high": 12.0 + i, "low": 9.0,
        "close": 11.0 + i, "volume": 500.0}]}
    barrier.wait()
    results.append(post("/imports/quotes", payload))


threads = [threading.Thread(target=worker, args=(i,)) for i in range(N)]
for t in threads:
    t.start()

# 导入进行中同时发查询，验证读不阻塞且始终拿到已提交快照
read_errors = []
seen_cutoffs = set()
for _ in range(20):
    try:
        s, r = get("/quotes/adjusted?security_code=600001&start_date=2026-01-01&end_date=2026-01-10")
        assert s == 200
        seen_cutoffs.add(r[0]["cutoff_batch_id"])
    except Exception as exc:  # noqa: BLE001
        read_errors.append(exc)

for t in threads:
    t.join()

ok = [r for r in results if r[0] == 201]
rejected = [r for r in results if r[0] in (409, 422)]
print("201 成功数 =", len(ok), "| 拒收数 =", len(rejected))
assert len(ok) == 1, results
assert len(rejected) == N - 1
assert read_errors == []
print("并发读看到的截止批次 =", sorted(seen_cutoffs))

s, batches = get("/batches")
cont_batches = [b for b in batches if b["id"] > BASE_BATCH]
# 竞争中只有一个事务成功提交 => 只多一个批次
assert len(cont_batches) == 1, cont_batches

# 再验证不同键并发全部成功
res2 = []
b2 = threading.Barrier(N)


def worker2(i):
    payload = {"quotes": [{
        "security_code": f"K{i:02d}", "trade_date": "2026-02-01",
        "revision": 0, "open": 1.0, "high": 2.0, "low": 0.5,
        "close": 1.5, "volume": 10.0}]}
    b2.wait()
    res2.append(post("/imports/quotes", payload))


threads = [threading.Thread(target=worker2, args=(i,)) for i in range(N)]
for t in threads:
    t.start()
for t in threads:
    t.join()
assert all(s == 201 for s, _ in res2), res2
print("不同键并发成功数 =", len(res2))
print("\nCONCURRENCY STRESS PASSED")
