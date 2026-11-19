"""功能自测：复权规则、补报不可变、修订规则、拒收与失败回滚。"""

from __future__ import annotations

from tests.conftest import action, get_adjusted, post_actions, post_quotes, quote


def test_basic_split_adjustment(client):
    """1 拆 2（ratio=2）：除权日之前的价格减半、成交量加倍。"""
    r = post_quotes(
        client,
        [
            quote(date="2026-01-02", close=100.0, vol=1000.0),
            quote(date="2026-01-05", close=60.0, vol=2000.0),  # 除权日当天
        ],
    )
    assert r.status_code == 201, r.text
    assert r.json()["row_count"] == 2

    r = post_actions(client, [action(ex="2026-01-05", ratio=2.0)])
    assert r.status_code == 201

    r = get_adjusted(client, "AAA", "2026-01-02", "2026-01-05")
    assert r.status_code == 200
    data = r.json()
    assert len(data) == 2

    early, on_ex = data
    # 早期行情被折算
    assert early["trade_date"] == "2026-01-02"
    assert early["close"] == 100.0
    assert early["adjusted_close"] == 50.0
    assert early["adjusted_open"] == 50.0
    assert early["adjusted_volume"] == 2000.0
    assert early["factor"] == 0.5
    assert len(early["applied_events"]) == 1
    ev = early["applied_events"][0]
    assert ev["event_no"] == "E1"
    assert ev["ratio"] == 2.0
    assert ev["ex_date"] == "2026-01-05"

    # 除权日当天及之后不折算（除权日晚于行情日才生效）
    assert on_ex["trade_date"] == "2026-01-05"
    assert on_ex["adjusted_close"] == 60.0
    assert on_ex["adjusted_volume"] == 2000.0
    assert on_ex["factor"] == 1.0
    assert on_ex["applied_events"] == []


def test_late_announcement_does_not_change_old_batch(client):
    """补报公告后，旧截止批次的查询结果必须与补报前完全一致。"""
    post_quotes(client, [quote(date="2026-01-02", close=100.0, vol=1000.0)])
    # 批次 2：最初没有任何公告
    before = get_adjusted(client, "AAA", "2026-01-02", "2026-01-06").json()
    assert before[0]["adjusted_close"] == 100.0
    assert before[0]["cutoff_batch_id"] == 1

    # 批次 3（补报）：1 拆 2 公告，除权日 2026-01-05
    r = post_actions(client, [action(ex="2026-01-05", ratio=2.0)])
    assert r.status_code == 201
    assert r.json()["batch_id"] == 2

    # 按旧截止批次查询：补报不可见，结果不变
    old_view = get_adjusted(
        client, "AAA", "2026-01-02", "2026-01-06", cutoff=1
    ).json()
    assert old_view[0]["adjusted_close"] == 100.0
    assert old_view[0]["factor"] == 1.0
    assert old_view[0]["applied_events"] == []

    # 最新批次：补报生效
    new_view = get_adjusted(client, "AAA", "2026-01-02", "2026-01-06").json()
    assert new_view[0]["adjusted_close"] == 50.0
    assert len(new_view[0]["applied_events"]) == 1


def test_higher_revision_wins_lower_revision_rejected(client):
    """同键高修订生效；再送低修订必须整批拒收。"""
    r = post_quotes(client, [quote(rev=1, close=110.0)])
    assert r.status_code == 201

    # 更高修订
    r = post_quotes(client, [quote(rev=2, close=121.0)])
    assert r.status_code == 201
    assert r.json()["row_count"] == 1

    view = client.get(
        "/quotes/raw",
        params={
            "security_code": "AAA",
            "start_date": "2026-01-02",
            "end_date": "2026-01-02",
        },
    ).json()
    assert view[0]["revision"] == 2
    assert view[0]["close"] == 121.0

    # 低修订过期 -> 拒收
    r = post_quotes(client, [quote(rev=1, close=999.0)])
    assert r.status_code == 422
    assert "过期修订" in r.json()["detail"]

    # 同修订异值 -> 拒收
    r = post_quotes(client, [quote(rev=2, close=999.0)])
    assert r.status_code == 422
    assert "同修订异值" in r.json()["detail"]

    # 同修订同值 -> 幂等，不产生新行
    r = post_quotes(client, [quote(rev=2, close=121.0)])
    assert r.status_code == 201
    assert r.json()["row_count"] == 0


def test_same_revision_conflict_within_one_payload(client):
    """同一请求内同键同修订异值 -> 422，且不生成批次。"""
    r = post_quotes(
        client,
        [quote(rev=0, close=100.0), quote(rev=0, close=101.0)],
    )
    assert r.status_code == 422
    assert client.get("/batches").json() == []

    r = post_actions(
        client,
        [action(event="E1", ratio=2.0, rev=0),
         action(event="E1", ratio=3.0, rev=0)],
    )
    assert r.status_code == 422
    assert client.get("/batches").json() == []


def test_failed_import_rolls_back_entire_batch(client):
    """一批中任意一行被拒收，整批回滚（无批次、无数据）。"""
    post_quotes(client, [quote(rev=2, close=100.0)])  # 批次 1
    r = post_quotes(
        client,
        [
            quote(date="2026-01-03", rev=0, close=50.0),  # 新键，本可写入
            quote(date="2026-01-02", rev=2, close=999.0),  # 同修订异值
        ],
    )
    assert r.status_code == 422
    batches = client.get("/batches").json()
    assert [b["id"] for b in batches] == [1]  # 没有产生批次 2
    # 第一行也未留下
    raw = client.get(
        "/quotes/raw",
        params={"security_code": "AAA", "start_date": "2026-01-01",
                "end_date": "2026-01-31"},
    ).json()
    assert len(raw) == 1
    assert raw[0]["trade_date"] == "2026-01-02"


def test_reverse_split_and_multiple_events(client):
    """并股价格放大；多笔拆股比例连乘，且只取除权日晚于行情日的公告。"""
    post_quotes(
        client,
        [
            quote(date="2026-01-02", close=100.0, vol=1000.0),
            quote(date="2026-01-04", close=100.0, vol=1000.0),
        ],
    )
    post_actions(
        client,
        [
            action(event="E1", ex="2026-01-03", ratio=2.0),       # 1 拆 2
            action(event="E2", ex="2026-01-05", ratio=0.5,
                   action_type="reverse_split"),                  # 2 并 1
        ],
    )
    data = get_adjusted(client, "AAA", "2026-01-02", "2026-01-06").json()
    by_day = {d["trade_date"]: d for d in data}

    # 01-02 早于两笔除权日：系数 1/(2*0.5)=1，价格不变，量不变
    d1 = by_day["2026-01-02"]
    assert d1["factor"] == 1.0
    assert d1["adjusted_close"] == 100.0
    assert d1["adjusted_volume"] == 1000.0
    assert [e["event_no"] for e in d1["applied_events"]] == ["E1", "E2"]

    # 01-04 只受 01-05 的并股影响：系数 1/0.5=2，价格翻倍、量减半
    d2 = by_day["2026-01-04"]
    assert d2["factor"] == 2.0
    assert d2["adjusted_close"] == 200.0
    assert d2["adjusted_volume"] == 500.0
    assert [e["event_no"] for e in d2["applied_events"]] == ["E2"]


def test_action_revision_correction_and_immutability(client):
    """公告修订更正只影响新批次；旧截止批次仍按旧公告折算。"""
    post_quotes(client, [quote(date="2026-01-02", close=100.0, vol=1000.0)])
    post_actions(client, [action(event="E1", ex="2026-01-05", ratio=2.0, rev=1)])

    v1 = get_adjusted(client, "AAA", "2026-01-02", "2026-01-06").json()
    assert v1[0]["adjusted_close"] == 50.0

    # 更正：比例应为 4（1 拆 4），修订号 2
    r = post_actions(
        client, [action(event="E1", ex="2026-01-05", ratio=4.0, rev=2)]
    )
    assert r.status_code == 201

    v2 = get_adjusted(client, "AAA", "2026-01-02", "2026-01-06").json()
    assert v2[0]["adjusted_close"] == 25.0
    assert v2[0]["applied_events"][0]["revision"] == 2

    # 截止到旧批次：仍是旧比例
    v_old = get_adjusted(
        client, "AAA", "2026-01-02", "2026-01-06", cutoff=2
    ).json()
    assert v_old[0]["adjusted_close"] == 50.0
    assert v_old[0]["applied_events"][0]["revision"] == 1

    # 低修订公告同样拒收
    r = post_actions(
        client, [action(event="E1", ex="2026-01-05", ratio=8.0, rev=1)]
    )
    assert r.status_code == 422


def test_invalid_cutoff_and_empty_db(client):
    assert client.get(
        "/quotes/adjusted",
        params={"security_code": "X", "start_date": "2026-01-01",
                "end_date": "2026-01-31", "cutoff_batch": 99},
    ).status_code == 404

    bad_date = client.get(
        "/quotes/adjusted",
        params={"security_code": "X", "start_date": "2026-02-01",
                "end_date": "2026-01-01"},
    )
    assert bad_date.status_code == 422


def test_bad_field_values_rejected(client):
    r = post_quotes(client, [quote(close=-1.0)])
    assert r.status_code == 422
    r = post_actions(client, [action(ratio=0.0)])
    assert r.status_code == 422
    r = post_quotes(client, [dict(quote(), trade_date="2026/01/02")])
    assert r.status_code == 422
