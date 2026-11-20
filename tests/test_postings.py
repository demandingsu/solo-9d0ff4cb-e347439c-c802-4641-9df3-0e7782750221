"""入账与底稿自测：FIFO 成本结转、跨拆股补录重算、整次入账作废。"""

from __future__ import annotations

from tests.conftest import action, post_actions


def trade(no, date, direction, shares, amount=None):
    t = {
        "trade_no": no,
        "trade_date": date,
        "direction": direction,
        "shares": shares,
    }
    if amount is not None:
        t["amount"] = amount
    return t


def post_posting(client, account="A1", code="600001", closing="2026-01-31",
                 cutoff=None, trades=()):
    payload = {
        "account": account,
        "security_code": code,
        "closing_date": closing,
        "trades": list(trades),
    }
    if cutoff is not None:
        payload["cutoff_batch"] = cutoff
    return client.post("/postings", json=payload)


def get_latest(client, account="A1", code="600001"):
    return client.get(
        "/postings/latest",
        params={"account": account, "security_code": code},
    )


def get_trades(client, account="A1", code="600001"):
    return client.get(
        "/trades", params={"account": account, "security_code": code}
    )


# ---------------------------------------------------------------- 基本结转


def test_fifo_allocation_and_deplete_carries_all_remaining_cost(client):
    """卖出按买入先后扣股；扣完一批结转全部剩余成本，部分扣减按比例到分。"""
    r = post_posting(
        client,
        trades=[
            trade("B1", "2026-01-02", "buy", 100, 10000.00),
            trade("B2", "2026-01-03", "buy", 100, 12000.00),
            trade("S1", "2026-01-04", "sell", 150),
        ],
    )
    assert r.status_code == 201, r.text
    body = r.json()
    assert body["sells_processed"] == 1
    assert body["cost_carried"] == 16000.00
    assert body["trades_new"] == 3

    ws = get_latest(client).json()
    allocs = ws["allocations"]
    assert len(allocs) == 2
    # 第一笔扣完 B1：结转全部剩余成本 10000
    assert allocs[0]["buy_trade_no"] == "B1"
    assert allocs[0]["shares_deducted"] == 100
    assert allocs[0]["cost_carried"] == 10000.00
    # 第二笔从 B2 扣 50/100：12000 * 50/100 = 6000
    assert allocs[1]["buy_trade_no"] == "B2"
    assert allocs[1]["shares_deducted"] == 50
    assert allocs[1]["cost_carried"] == 6000.00

    pos = {p["buy_trade_no"]: p for p in ws["positions"]}
    assert pos["B1"]["remaining_shares"] == 0
    assert pos["B1"]["remaining_cost"] == 0.0
    assert pos["B2"]["remaining_shares"] == 50
    assert pos["B2"]["remaining_cost"] == 6000.00


def test_cost_rounding_half_up_to_cent(client):
    """比例结转四舍五入到分；最后一笔扣完时结转全部剩余，成本守恒。"""
    r = post_posting(
        client,
        trades=[
            trade("B1", "2026-01-02", "buy", 3, 100.00),
            trade("S1", "2026-01-03", "sell", 1),
            trade("S2", "2026-01-04", "sell", 1),
            trade("S3", "2026-01-05", "sell", 1),
        ],
    )
    assert r.status_code == 201, r.text
    ws = get_latest(client).json()
    costs = [a["cost_carried"] for a in ws["allocations"]]
    # 100*1/3=33.33；66.67*1/2=33.335->33.34（半进）；扣完结转剩余 33.33
    assert costs == [33.33, 33.34, 33.33]
    assert sum(costs) == 100.00
    assert ws["positions"][0]["remaining_cost"] == 0.0


def test_trades_sorted_by_date_and_trade_no(client):
    """成交按交易日、成交号排序处理（与请求顺序无关）。"""
    r = post_posting(
        client,
        trades=[
            trade("S1", "2026-01-02", "sell", 5),   # 先卖（同日成交号靠后）
            trade("B2", "2026-01-02", "buy", 10, 100.0),
            trade("B1", "2026-01-02", "buy", 10, 50.0),
        ],
    )
    assert r.status_code == 201, r.text
    ws = get_latest(client).json()
    # 同日按成交号排序：B1, B2, S1 -> S1 从 B1 扣 5 股，成本 50*5/10=25
    assert ws["allocations"][0]["buy_trade_no"] == "B1"
    assert ws["allocations"][0]["cost_carried"] == 25.00


# ---------------------------------------------------------------- 成交幂等与拒收


def test_same_trade_recorded_once_and_conflict_rejected(client):
    """同号同值只记一次；同号异值整次拒收，不产生入账批次。"""
    r = post_posting(client, trades=[trade("B1", "2026-01-02", "buy", 100, 1000.0)])
    assert r.status_code == 201
    assert r.json()["trades_new"] == 1

    # 同号同值：幂等，trades_new=0，仍生成重算批次
    r = post_posting(client, trades=[trade("B1", "2026-01-02", "buy", 100, 1000.0)])
    assert r.status_code == 201
    assert r.json()["trades_new"] == 0
    assert len(get_trades(client).json()) == 1

    # 同号异值：422，整次作废（无新入账批次、成交不变）
    before = client.get("/postings").json()
    r = post_posting(client, trades=[trade("B1", "2026-01-02", "buy", 100, 9999.0)])
    assert r.status_code == 422
    assert "同号异值" in r.json()["detail"]
    assert client.get("/postings").json() == before

    # 同一请求内同号异值同样拒收
    r = post_posting(
        client,
        trades=[
            trade("B9", "2026-01-02", "buy", 1, 1.0),
            trade("B9", "2026-01-02", "buy", 2, 2.0),
        ],
    )
    assert r.status_code == 422


# ---------------------------------------------------------------- 拆并股处理


def test_split_adjusts_lots_before_trades_on_ex_date(client):
    """除权日先调整各笔剩余股数再处理成交；除权日当天买入不调整。"""
    post_actions(client, [action(code="600001", event="SP1",
                                 ex="2026-01-05", ratio=2.0)])
    r = post_posting(
        client,
        cutoff=1,
        trades=[
            trade("B1", "2026-01-02", "buy", 100, 10000.00),  # 除权日前买入 -> 200 股
            trade("B2", "2026-01-05", "buy", 50, 2500.00),    # 除权日当天买入 -> 不调整
            trade("S1", "2026-01-06", "sell", 150),
        ],
    )
    assert r.status_code == 201, r.text
    ws = get_latest(client).json()
    alloc = ws["allocations"][0]
    # 150 股全部来自 B1（200 股中的 150）：10000 * 150/200 = 7500
    assert alloc["buy_trade_no"] == "B1"
    assert alloc["shares_deducted"] == 150
    assert alloc["cost_carried"] == 7500.00
    pos = {p["buy_trade_no"]: p for p in ws["positions"]}
    assert pos["B1"]["remaining_shares"] == 50
    assert pos["B1"]["remaining_cost"] == 2500.00
    assert pos["B2"]["remaining_shares"] == 50  # 除权日当天买入未被调整
    assert pos["B2"]["remaining_cost"] == 2500.00


def test_fractional_shares_kept(client):
    """保留碎股：1 拆 1.5 后出现 0.5 股，剩余成本不变。"""
    post_actions(client, [action(code="600001", event="SP15",
                                 ex="2026-01-05", ratio=1.5)])
    r = post_posting(
        client,
        cutoff=1,
        trades=[
            trade("B1", "2026-01-02", "buy", 3, 90.00),
            trade("S1", "2026-01-06", "sell", 4),
        ],
    )
    assert r.status_code == 201, r.text
    ws = get_latest(client).json()
    # 3 * 1.5 = 4.5 股；卖 4 股结转 90 * 4/4.5 = 80；余 0.5 股 / 10 元
    assert ws["allocations"][0]["shares_deducted"] == 4
    assert ws["allocations"][0]["cost_carried"] == 80.00
    pos = ws["positions"][0]
    assert pos["remaining_shares"] == 0.5
    assert pos["remaining_cost"] == 10.00


def test_backfill_across_split_and_recutoff_recompute(client):
    """跨拆股补录 + 换公告批次重算：旧底稿不变，新底稿反映更正后公告。"""
    # 批次 1：1 拆 2 公告（除权日 01-05）
    post_actions(client, [action(code="600001", event="SP1",
                                 ex="2026-01-05", ratio=2.0, rev=0)])
    r = post_posting(
        client,
        cutoff=1,
        trades=[
            trade("B1", "2026-01-02", "buy", 100, 10000.00),
            trade("S1", "2026-01-06", "sell", 150),
        ],
    )
    assert r.status_code == 201
    first_id = r.json()["posting_id"]
    assert r.json()["cost_carried"] == 7500.00

    # 批次 2：公告更正为 1 拆 3（更高修订号）
    post_actions(client, [action(code="600001", event="SP1",
                                 ex="2026-01-05", ratio=3.0, rev=1)])

    # 只换公告批次重算（不补录成交）：100->300 股，卖 150 结转 5000
    r = post_posting(client, cutoff=2, trades=[])
    assert r.status_code == 201
    assert r.json()["trades_new"] == 0
    assert r.json()["cost_carried"] == 5000.00
    ws = get_latest(client).json()
    assert ws["cutoff_batch"] == 2
    assert ws["positions"][0]["remaining_shares"] == 150
    assert ws["positions"][0]["remaining_cost"] == 5000.00

    # 跨拆股补录：补一笔除权日前的买入，重算后同样被拆股调整
    r = post_posting(
        client,
        cutoff=2,
        trades=[trade("B0", "2026-01-03", "buy", 50, 6000.00)],
    )
    assert r.status_code == 201
    assert r.json()["trades_new"] == 1
    ws = get_latest(client).json()
    pos = {p["buy_trade_no"]: p for p in ws["positions"]}
    assert pos["B0"]["remaining_shares"] == 150  # 50 * 3
    assert pos["B0"]["remaining_cost"] == 6000.00
    # FIFO 顺序不变：S1 仍全部从 B1（01-02 早于 01-03）扣减
    assert [a["buy_trade_no"] for a in ws["allocations"]] == ["B1"]

    # 旧入账批次的底稿不可变
    old = client.get(f"/postings/{first_id}").json()
    assert old["cutoff_batch"] == 1
    assert old["cost_carried"] == 7500.00
    assert old["positions"][0]["remaining_shares"] == 50


# ---------------------------------------------------------------- 整次入账作废


def test_closing_date_before_trade_date_voids_posting(client):
    """结账日早于任一成交日（含库中已有成交）=> 整次作废，不留任何痕迹。"""
    r = post_posting(
        client,
        closing="2026-01-01",
        trades=[trade("B1", "2026-01-02", "buy", 100, 1000.0)],
    )
    assert r.status_code == 422
    assert "早于成交日" in r.json()["detail"]
    assert client.get("/postings").json() == []
    assert get_trades(client).json() == []
    assert get_latest(client).status_code == 404

    # 先成功入账，再用更早的结账日重算 -> 作废，上次底稿仍可查
    r = post_posting(client, closing="2026-01-31",
                     trades=[trade("B1", "2026-01-02", "buy", 100, 1000.0)])
    assert r.status_code == 201
    r = post_posting(client, closing="2026-01-01", trades=[])
    assert r.status_code == 422
    ws = get_latest(client).json()
    assert ws["closing_date"] == "2026-01-31"
    assert ws["positions"][0]["remaining_shares"] == 100


def test_insufficient_shares_voids_posting(client):
    """重算中股数不足 => 整次作废，成交与底稿都不留。"""
    r = post_posting(
        client,
        trades=[
            trade("B1", "2026-01-02", "buy", 100, 1000.0),
            trade("S1", "2026-01-03", "sell", 150),
        ],
    )
    assert r.status_code == 422
    assert "股数不足" in r.json()["detail"]
    assert client.get("/postings").json() == []
    assert get_trades(client).json() == []


def test_announcement_correction_causing_shortage_keeps_last_worksheet(client):
    """公告更正后重算股数不足：整次作废，仍可查上次采用的公告批次及完整底稿。"""
    # 批次 1：1 拆 2
    post_actions(client, [action(code="600001", event="SP1",
                                 ex="2026-01-05", ratio=2.0, rev=0)])
    r = post_posting(
        client,
        cutoff=1,
        trades=[
            trade("B1", "2026-01-02", "buy", 100, 10000.00),
            trade("S1", "2026-01-06", "sell", 150),  # 200 股够用
        ],
    )
    assert r.status_code == 201

    # 批次 2：更正为 2 并 1（ratio 0.5）-> 只剩 50 股，卖出 150 不足
    post_actions(client, [action(code="600001", event="SP1",
                                 ex="2026-01-05", ratio=0.5, rev=1,
                                 action_type="reverse_split")])
    r = post_posting(client, cutoff=2, trades=[])
    assert r.status_code == 422
    assert "股数不足" in r.json()["detail"]

    # 上次成功入账的底稿（含采用的公告批次）完整可查
    ws = get_latest(client).json()
    assert ws["cutoff_batch"] == 1
    assert ws["cost_carried"] == 7500.00
    assert len(ws["allocations"]) == 1
    assert ws["positions"][0]["remaining_shares"] == 50
    # 入账批次清单只有成功的那一次
    assert len(client.get("/postings").json()) == 1


# ---------------------------------------------------------------- 校验与守恒


def test_trade_validation_errors(client):
    # 买入缺总额
    r = post_posting(client, trades=[trade("B1", "2026-01-02", "buy", 100)])
    assert r.status_code == 422
    # 卖出填了总额
    r = post_posting(
        client, trades=[trade("S1", "2026-01-02", "sell", 100, 5.0)]
    )
    assert r.status_code == 422
    # 股数非正 / 日期非法
    r = post_posting(
        client, trades=[trade("B1", "2026-01-02", "buy", 0, 1.0)]
    )
    assert r.status_code == 422
    r = post_posting(
        client, trades=[trade("B1", "2026/01/02", "buy", 1, 1.0)]
    )
    assert r.status_code == 422
    # 不存在的公告截止批次
    r = post_posting(client, cutoff=99, trades=[])
    assert r.status_code == 404


def test_worksheet_cost_conservation(client):
    """底稿成本守恒：已结转成本合计 + 各批次剩余成本 = 买入总额合计。"""
    post_actions(client, [action(code="600001", event="SP1",
                                 ex="2026-01-05", ratio=2.0)])
    buys = [("B1", "2026-01-02", 100, 10000.00),
            ("B2", "2026-01-04", 60, 5000.00),
            ("B3", "2026-01-06", 40, 4000.00)]
    r = post_posting(
        client,
        cutoff=1,
        trades=[trade(n, d, "buy", s, a) for n, d, s, a in buys]
        + [trade("S1", "2026-01-07", "sell", 130),
           trade("S2", "2026-01-08", "sell", 90)],
    )
    assert r.status_code == 201, r.text
    ws = get_latest(client).json()
    carried = sum(a["cost_carried"] for a in ws["allocations"])
    remaining = sum(p["remaining_cost"] for p in ws["positions"])
    total_buy = sum(a for *_, a in buys)
    assert round(carried + remaining, 2) == total_buy
    assert ws["cost_carried"] == round(carried, 2)
    # 股数也守恒（B1/B2 经 1 拆 2，B3 在除权日后买入不调整）
    shares_left = sum(p["remaining_shares"] for p in ws["positions"])
    assert shares_left == (100 + 60) * 2 + 40 - 130 - 90
