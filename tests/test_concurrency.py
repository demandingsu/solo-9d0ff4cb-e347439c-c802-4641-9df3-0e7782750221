"""并发自测：并发导入、读不阻写、同快照一致性。

这些用例直接对真实 SQLite 文件起多线程调用服务层，验证
WAL + BEGIN IMMEDIATE + busy_timeout 下的行为；另起一个真实
uvicorn 进程跑 HTTP 冒烟，覆盖容器启动路径。
"""

from __future__ import annotations

import threading

import pytest

from app import services
from app.models import CorporateActionIn, QuoteIn
from tests.conftest import action, get_adjusted, post_actions, post_quotes, quote


def _q(rev, close):
    return QuoteIn(
        security_code="AAA", trade_date="2026-01-02", revision=rev,
        open=close, high=close, low=close, close=close, volume=1000.0,
    )


def test_concurrent_import_same_revision_one_wins(db_file):
    """N 个线程并发导入同一 (键, 修订) 的不同值：恰好一个成功，其余拒收。"""
    n = 8
    results: list[Exception | dict] = []
    barrier = threading.Barrier(n)

    def worker(close_price):
        barrier.wait()  # 尽量同时撞上写锁
        try:
            out = services.import_quotes([_q(0, float(close_price))], db_file)
            results.append(out)
        except Exception as exc:  # noqa: BLE001 - 并发竞争结果需要全部收集
            results.append(exc)

    threads = [
        threading.Thread(target=worker, args=(100 + i,)) for i in range(n)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    successes = [r for r in results if isinstance(r, dict)]
    failures = [r for r in results if isinstance(r, Exception)]
    assert len(successes) == 1
    assert len(failures) == n - 1
    for f in failures:
        # 唯一索引冲突 -> 409；先到者已占同修订 -> 422
        assert getattr(f, "status_code", None) in (409, 422)

    # 库里只有一行，无脏数据
    raw = services.query_raw_quotes(
        "AAA", "2026-01-01", "2026-01-31", None, db_file
    )
    assert len(raw) == 1
    batches = services.list_batches(db_file)
    # 只有成功的那一个事务生成了批次（失败事务整体回滚）
    assert len(batches) == 1
    assert batches[0].row_count == 1


def test_concurrent_import_distinct_keys_all_succeed(db_file):
    """并发导入不同键：全部成功，批次与行数一致。"""
    n = 8
    errors: list[Exception] = []
    barrier = threading.Barrier(n)

    def worker(i):
        barrier.wait()
        try:
            services.import_quotes(
                [_q(0, 100.0).model_copy(
                    update={"security_code": f"S{i:02d}"})],
                db_file,
            )
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=worker, args=(i,)) for i in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert errors == []
    batches = services.list_batches(db_file)
    assert len(batches) == n
    assert sum(b.row_count for b in batches) == n


def test_concurrent_reads_are_stable_while_new_batches_commit(db_file):
    """导入持续提交期间，反复按固定截止批次查询，结果必须恒定。"""
    services.import_quotes([_q(0, 100.0)], db_file)
    services.import_actions(
        [CorporateActionIn(**action(ex="2026-01-05", ratio=2.0, rev=0))],
        db_file,
    )
    # 截止批次固定为 2（含 1 拆 2 公告）
    baseline = services.query_adjusted_quotes(
        "AAA", "2026-01-02", "2026-01-06", 2, db_file
    )
    assert baseline[0].adjusted_close == 50.0

    stop = threading.Event()
    errors: list[Exception] = []

    def reader():
        try:
            while not stop.is_set():
                view = services.query_adjusted_quotes(
                    "AAA", "2026-01-02", "2026-01-06", 2, db_file
                )
                # 价量与依据必须始终来自截止批次 2 的已提交状态
                assert view[0].cutoff_batch_id == 2
                assert view[0].adjusted_close == 50.0
                assert len(view[0].applied_events) == 1
                assert view[0].applied_events[0].ratio == 2.0
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def writer():
        # 持续补报更高修订与新公告（提交后对 cutoff=2 的读也不可见）
        for rev in range(1, 13):
            services.import_actions(
                [CorporateActionIn(
                    **action(event="E1", ex="2026-01-05",
                             ratio=3.0, rev=rev))],
                db_file,
            )

    t_read = threading.Thread(target=reader)
    t_write = threading.Thread(target=writer)
    t_read.start()
    t_write.start()
    t_write.join()
    stop.set()
    t_read.join()

    assert errors == []
    # 最新视图已变，但旧截止批次视图仍稳定
    latest = services.query_adjusted_quotes(
        "AAA", "2026-01-02", "2026-01-06", None, db_file
    )
    assert latest[0].applied_events[0].revision == 12
    old = services.query_adjusted_quotes(
        "AAA", "2026-01-02", "2026-01-06", 2, db_file
    )
    assert old[0].applied_events[0].revision == 0
    assert old[0].adjusted_close == 50.0


def test_http_concurrent_requests_during_import(client):
    """HTTP 层并发：导入与查询同时进行，查询要么见旧状态要么见新状态，绝不报错。"""
    post_quotes(client, [quote(date="2026-01-02", close=100.0, vol=1000.0)])
    post_actions(client, [action(ex="2026-01-05", ratio=2.0)])

    errors: list = []
    seen: set = set()
    stop = threading.Event()

    def reader():
        try:
            while not stop.is_set():
                r = get_adjusted(client, "AAA", "2026-01-02", "2026-01-06")
                assert r.status_code == 200
                seen.add(r.json()[0]["adjusted_close"])
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    def writer():
        for rev in range(1, 7):
            post_actions(
                client,
                [action(event="E1", ex="2026-01-05", ratio=4.0, rev=rev)],
            )

    threads = [threading.Thread(target=reader) for _ in range(4)]
    for t in threads:
        t.start()
    tw = threading.Thread(target=writer)
    tw.start()
    tw.join()
    stop.set()
    for t in threads:
        t.join()

    assert errors == []
    # 读只可能看到已提交状态：50.0（ratio=2）或 25.0（ratio=4），无中间态
    assert seen <= {50.0, 25.0}
    assert 50.0 in seen  # 至少有些读发生在更正提交之前


def test_uvicorn_http_smoke(db_file):
    """真实 uvicorn 进程冒烟：启动 /docs、导入、复权查询全链路。"""
    uvicorn = pytest.importorskip("uvicorn")
    httpx = pytest.importorskip("httpx")
    import socket

    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()

    config = uvicorn.Config(
        "app.main:app", host="127.0.0.1", port=port, log_level="warning"
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    try:
        base = f"http://127.0.0.1:{port}"
        # 等待启动
        with httpx.Client(timeout=10) as http:
            for _ in range(50):
                try:
                    assert http.get(base + "/health").json() == {"status": "ok"}
                    break
                except httpx.TransportError:
                    if not thread.is_alive():
                        pytest.fail("uvicorn 进程提前退出")
                    threading.Event().wait(0.1)
            else:
                pytest.fail("uvicorn 启动超时")

            docs = http.get(base + "/docs")
            assert docs.status_code == 200
            openapi = http.get(base + "/openapi.json").json()
            assert "/imports/quotes" in openapi["paths"]
            assert "/quotes/adjusted" in openapi["paths"]

            r = http.post(base + "/imports/quotes",
                          json={"quotes": [quote()]})
            assert r.status_code == 201
            r = http.post(base + "/imports/corporate-actions",
                          json={"actions": [action()]})
            assert r.status_code == 201
            r = http.get(
                base + "/quotes/adjusted",
                params={"security_code": "AAA", "start_date": "2026-01-02",
                        "end_date": "2026-01-05"},
            )
            assert r.status_code == 200
            data = r.json()
            assert data[0]["adjusted_close"] == 50.0
            assert data[0]["applied_events"][0]["event_no"] == "E1"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
