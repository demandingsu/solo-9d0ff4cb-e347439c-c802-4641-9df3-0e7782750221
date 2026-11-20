"""业务逻辑：版本化导入与截止批次复权查询。

不变量
======
1. 只追加：行情/公告表从不 UPDATE/DELETE，修订号决定生效版本。
2. 同键（行情=证券+交易日，公告=证券+事件号）同修订异值 => 整批拒收；
   新请求修订号低于库中已生效修订 => 整批拒收（后来补报不能覆盖旧序列）。
3. 每次 *成功* 导入在同一事务内生成一个入库批次；失败回滚，不留批次。
4. 查询在单个快照事务内完成，截止批次之后提交的任何数据都不可见。
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from datetime import date
from decimal import ROUND_HALF_UP, Decimal

from . import db
from .models import (
    AdjustedQuote,
    AllocationOut,
    BatchOut,
    CorporateActionIn,
    CorporateActionOut,
    EventBasis,
    PositionOut,
    PostingOut,
    PostingRequest,
    PostingResponse,
    QuoteIn,
    RawQuote,
    TradeIn,
    TradeOut,
    WorksheetOut,
)

# 折算值保留 5 位小数（ROUND_HALF_UP，金融惯例）
QUANT = Decimal("0.00001")
# 金额单位：分
CENT = Decimal("0.01")


class Rejected(Exception):
    """导入被业务规则拒收（HTTP 422/409）。"""

    def __init__(self, message: str, status_code: int = 422):
        super().__init__(message)
        self.status_code = status_code
        self.message = message


# ---------------------------------------------------------------- 批内归一化


def _check_iso_date(value: str) -> None:
    date.fromisoformat(value)


def normalize_quotes(
    rows: Iterable[QuoteIn],
) -> dict[tuple[str, str], QuoteIn]:
    """同一请求内按 (证券, 交易日) 归一化。

    同键同修订且完全相同 => 视为重复，保留一条；
    同键同修订但值不同 => 拒收；
    同键多修订 => 仅最高修订生效（其余忽略），最高修订并列且异值 => 拒收。
    """
    groups: dict[tuple[str, str], list[QuoteIn]] = {}
    for row in rows:
        groups.setdefault((row.security_code, row.trade_date), []).append(row)

    chosen: dict[tuple[str, str], QuoteIn] = {}
    for key, items in groups.items():
        by_rev: dict[int, QuoteIn] = {}
        for item in items:
            old = by_rev.get(item.revision)
            if old is not None and _quote_signature(old) != _quote_signature(item):
                raise Rejected(
                    f"同键同修订异值拒收：证券 {key[0]} 交易日 {key[1]} "
                    f"修订号 {item.revision} 在本次导入中出现不同值"
                )
            by_rev[item.revision] = item
        max_rev = max(by_rev)
        chosen[key] = by_rev[max_rev]
    return chosen


def normalize_actions(
    rows: Iterable[CorporateActionIn],
) -> dict[tuple[str, str], CorporateActionIn]:
    """同 normalize_quotes，业务键为 (证券, 事件号)。"""
    groups: dict[tuple[str, str], list[CorporateActionIn]] = {}
    for row in rows:
        groups.setdefault((row.security_code, row.event_no), []).append(row)

    chosen: dict[tuple[str, str], CorporateActionIn] = {}
    for key, items in groups.items():
        by_rev: dict[int, CorporateActionIn] = {}
        for item in items:
            old = by_rev.get(item.revision)
            if old is not None and _action_signature(old) != _action_signature(item):
                raise Rejected(
                    f"同键同修订异值拒收：证券 {key[0]} 事件号 {key[1]} "
                    f"修订号 {item.revision} 在本次导入中出现不同值"
                )
            by_rev[item.revision] = item
        chosen[key] = by_rev[max(by_rev)]
    return chosen


def _quote_signature(q: QuoteIn) -> tuple:
    return (q.open, q.high, q.low, q.close, q.volume)


def _action_signature(a: CorporateActionIn) -> tuple:
    return (a.ex_date, a.ratio, a.action_type)


# ---------------------------------------------------------------- 导入


def _ensure_securities(conn: sqlite3.Connection, codes: set[str]) -> None:
    conn.executemany(
        "INSERT OR IGNORE INTO securities(code) VALUES (?)",
        [(c,) for c in sorted(codes)],
    )


def import_quotes(rows: list[QuoteIn], db_file: str | None = None) -> dict:
    normalized = normalize_quotes(rows)
    with db.get_conn(db_file) as conn:
        try:
            with db.write_transaction(conn):
                _ensure_securities(conn, {k[0] for k in normalized})

                # 先建批次，行数据再引用它；全部在同一事务内
                cur = conn.execute(
                    "INSERT INTO ingest_batches(payload_kind, row_count) "
                    "VALUES ('quotes', 0)"
                )
                batch_id = cur.lastrowid

                inserted = 0
                for (code, tdate), row in sorted(normalized.items()):
                    existing = conn.execute(
                        "SELECT revision, open, high, low, close, volume "
                        "FROM daily_quotes WHERE security_code = ? AND trade_date = ? "
                        "ORDER BY revision DESC LIMIT 1",
                        (code, tdate),
                    ).fetchone()

                    if existing is not None:
                        if existing["revision"] > row.revision:
                            raise Rejected(
                                f"过期修订拒收：证券 {code} 交易日 {tdate} 库中已生效 "
                                f"修订号 {existing['revision']}，收到 {row.revision}"
                            )
                        if existing["revision"] == row.revision:
                            sig = (
                                existing["open"],
                                existing["high"],
                                existing["low"],
                                existing["close"],
                                existing["volume"],
                            )
                            if sig == _quote_signature(row):
                                continue  # 完全重复：幂等跳过
                            raise Rejected(
                                f"同修订异值拒收：证券 {code} 交易日 {tdate} "
                                f"修订号 {row.revision} 已存在不同值"
                            )

                    conn.execute(
                        "INSERT INTO daily_quotes(security_code, trade_date, revision, "
                        "open, high, low, close, volume, ingest_batch_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (
                            code,
                            tdate,
                            row.revision,
                            row.open,
                            row.high,
                            row.low,
                            row.close,
                            row.volume,
                            batch_id,
                        ),
                    )
                    inserted += 1

                conn.execute(
                    "UPDATE ingest_batches SET row_count = ? WHERE id = ?",
                    (inserted, batch_id),
                )
        except sqlite3.IntegrityError as exc:
            # 唯一索引兜底：并发导入同一 (键, 修订) 时其中一个事务在此失败
            raise Rejected(f"导入与已入库数据冲突，整批拒收：{exc}", 409) from exc

    return {
        "batch_id": batch_id,
        "payload_kind": "quotes",
        "row_count": inserted,
        "received": len(rows),
    }


def import_actions(
    rows: list[CorporateActionIn], db_file: str | None = None
) -> dict:
    normalized = normalize_actions(rows)
    with db.get_conn(db_file) as conn:
        try:
            with db.write_transaction(conn):
                _ensure_securities(conn, {k[0] for k in normalized})

                cur = conn.execute(
                    "INSERT INTO ingest_batches(payload_kind, row_count) "
                    "VALUES ('corporate_actions', 0)"
                )
                batch_id = cur.lastrowid

                inserted = 0
                for (code, event_no), row in sorted(normalized.items()):
                    existing = conn.execute(
                        "SELECT revision, ex_date, ratio, action_type "
                        "FROM corporate_actions WHERE security_code = ? AND event_no = ? "
                        "ORDER BY revision DESC LIMIT 1",
                        (code, event_no),
                    ).fetchone()

                    if existing is not None:
                        if existing["revision"] > row.revision:
                            raise Rejected(
                                f"过期修订拒收：证券 {code} 事件 {event_no} 库中已生效 "
                                f"修订号 {existing['revision']}，收到 {row.revision}"
                            )
                        if existing["revision"] == row.revision:
                            sig = (
                                existing["ex_date"],
                                existing["ratio"],
                                existing["action_type"],
                            )
                            if sig == _action_signature(row):
                                continue
                            raise Rejected(
                                f"同修订异值拒收：证券 {code} 事件 {event_no} "
                                f"修订号 {row.revision} 已存在不同值"
                            )

                    conn.execute(
                        "INSERT INTO corporate_actions(security_code, event_no, ex_date, "
                        "ratio, action_type, revision, ingest_batch_id) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?)",
                        (
                            code,
                            event_no,
                            row.ex_date,
                            row.ratio,
                            row.action_type,
                            row.revision,
                            batch_id,
                        ),
                    )
                    inserted += 1

                conn.execute(
                    "UPDATE ingest_batches SET row_count = ? WHERE id = ?",
                    (inserted, batch_id),
                )
        except sqlite3.IntegrityError as exc:
            raise Rejected(f"导入与已入库数据冲突，整批拒收：{exc}", 409) from exc

    return {
        "batch_id": batch_id,
        "payload_kind": "corporate_actions",
        "row_count": inserted,
        "received": len(rows),
    }


# ---------------------------------------------------------------- 查询


def list_batches(db_file: str | None = None) -> list[BatchOut]:
    with db.get_conn(db_file) as conn, db.read_snapshot(conn):
        rows = conn.execute(
            "SELECT id, payload_kind, row_count, imported_at "
            "FROM ingest_batches ORDER BY id"
        ).fetchall()
    return [BatchOut(**dict(r)) for r in rows]


def _resolve_cutoff(conn: sqlite3.Connection, cutoff: int | None) -> int:
    row = conn.execute("SELECT MAX(id) AS m FROM ingest_batches").fetchone()
    latest = row["m"]
    if latest is None:
        raise Rejected("库中尚无任何入库批次", 404)
    if cutoff is None:
        return int(latest)
    exists = conn.execute(
        "SELECT 1 FROM ingest_batches WHERE id = ?", (cutoff,)
    ).fetchone()
    if exists is None:
        raise Rejected(f"截止批次 {cutoff} 不存在", 404)
    return cutoff


def _effective_quotes(
    conn: sqlite3.Connection, code: str, start: str, end: str, cutoff: int
) -> list[sqlite3.Row]:
    """截至 cutoff 每个交易日的生效修订（同键最高修订号）。"""
    return conn.execute(
        """
        SELECT q.security_code, q.trade_date, q.revision,
               q.open, q.high, q.low, q.close, q.volume, q.ingest_batch_id
        FROM daily_quotes q
        JOIN (
            SELECT security_code, trade_date, MAX(revision) AS max_rev
            FROM daily_quotes
            WHERE ingest_batch_id <= ?
            GROUP BY security_code, trade_date
        ) m ON m.security_code = q.security_code
           AND m.trade_date = q.trade_date
           AND q.revision = m.max_rev
        WHERE q.ingest_batch_id <= ?
          AND q.security_code = ?
          AND q.trade_date BETWEEN ? AND ?
        ORDER BY q.trade_date
        """,
        (cutoff, cutoff, code, start, end),
    ).fetchall()


def _effective_actions_for_day(
    conn: sqlite3.Connection, code: str, trade_date: str, end: str, cutoff: int
) -> list[sqlite3.Row]:
    """对某行情日生效的公告：除权日晚于行情日、不晚于基准日，且截至 cutoff 已入库。"""
    return conn.execute(
        """
        SELECT a.event_no, a.ex_date, a.ratio, a.action_type,
               a.revision, a.ingest_batch_id
        FROM corporate_actions a
        JOIN (
            SELECT security_code, event_no, MAX(revision) AS max_rev
            FROM corporate_actions
            WHERE ingest_batch_id <= ?
            GROUP BY security_code, event_no
        ) m ON m.security_code = a.security_code
           AND m.event_no = a.event_no
           AND a.revision = m.max_rev
        WHERE a.ingest_batch_id <= ?
          AND a.security_code = ?
          AND a.ex_date > ?
          AND a.ex_date <= ?
        ORDER BY a.ex_date, a.event_no
        """,
        (cutoff, cutoff, code, trade_date, end),
    ).fetchall()


def _quantize(value: Decimal) -> float:
    return float(value.quantize(QUANT, rounding=ROUND_HALF_UP))


def query_adjusted_quotes(
    code: str,
    start: str,
    end: str,
    cutoff: int | None = None,
    db_file: str | None = None,
) -> list[AdjustedQuote]:
    """按截止批次与日期区间返回原值、折算值及事件依据。

    基准日固定为区间末日 end：仅使用 ``trade_date < ex_date <= end`` 的公告。
    拆成两股（ratio=2）价格减半、成交量加倍；多笔公告比例连乘。
    """
    _check_iso_date(start)
    _check_iso_date(end)
    if start > end:
        raise Rejected("start_date 不能晚于 end_date")

    with db.get_conn(db_file) as conn, db.read_snapshot(conn):
        cutoff_id = _resolve_cutoff(conn, cutoff)
        quote_rows = _effective_quotes(conn, code, start, end, cutoff_id)

        results: list[AdjustedQuote] = []
        for q in quote_rows:
            action_rows = _effective_actions_for_day(
                conn, code, q["trade_date"], end, cutoff_id
            )
            # 系数用 Decimal 计算，避免浮点连乘误差
            factor_dec = Decimal(1)
            for a in action_rows:
                factor_dec /= Decimal(str(a["ratio"]))

            events = [
                EventBasis(
                    event_no=a["event_no"],
                    ex_date=a["ex_date"],
                    ratio=a["ratio"],
                    action_type=a["action_type"],
                    revision=a["revision"],
                    ingest_batch_id=a["ingest_batch_id"],
                )
                for a in action_rows
            ]

            def adj_price(x: float) -> float:
                return _quantize(Decimal(str(x)) * factor_dec)

            def adj_volume(x: float) -> float:
                # 量的折算 = 原值 / 系数 = 原值 * 比例之积
                return _quantize(Decimal(str(x)) / factor_dec)

            results.append(
                AdjustedQuote(
                    security_code=q["security_code"],
                    trade_date=q["trade_date"],
                    open=q["open"],
                    high=q["high"],
                    low=q["low"],
                    close=q["close"],
                    volume=q["volume"],
                    adjusted_open=adj_price(q["open"]),
                    adjusted_high=adj_price(q["high"]),
                    adjusted_low=adj_price(q["low"]),
                    adjusted_close=adj_price(q["close"]),
                    adjusted_volume=adj_volume(q["volume"]),
                    factor=_quantize(factor_dec),
                    applied_events=events,
                    cutoff_batch_id=cutoff_id,
                    basis_end_date=end,
                )
            )
    return results


def query_raw_quotes(
    code: str,
    start: str,
    end: str,
    cutoff: int | None = None,
    db_file: str | None = None,
) -> list[RawQuote]:
    """原值查询（生效修订），便于核对。"""
    _check_iso_date(start)
    _check_iso_date(end)
    if start > end:
        raise Rejected("start_date 不能晚于 end_date")

    with db.get_conn(db_file) as conn, db.read_snapshot(conn):
        cutoff_id = _resolve_cutoff(conn, cutoff)
        rows = _effective_quotes(conn, code, start, end, cutoff_id)
    return [
        RawQuote(
            security_code=r["security_code"],
            trade_date=r["trade_date"],
            revision=r["revision"],
            open=r["open"],
            high=r["high"],
            low=r["low"],
            close=r["close"],
            volume=r["volume"],
            ingest_batch_id=r["ingest_batch_id"],
        )
        for r in rows
    ]


def query_actions(
    code: str,
    cutoff: int | None = None,
    db_file: str | None = None,
) -> list[CorporateActionOut]:
    """截至某批次已生效的公告（每事件号最高修订）。"""
    with db.get_conn(db_file) as conn, db.read_snapshot(conn):
        cutoff_id = _resolve_cutoff(conn, cutoff)
        rows = conn.execute(
            """
            SELECT a.security_code, a.event_no, a.ex_date, a.ratio,
                   a.action_type, a.revision, a.ingest_batch_id
            FROM corporate_actions a
            JOIN (
                SELECT security_code, event_no, MAX(revision) AS max_rev
                FROM corporate_actions
                WHERE ingest_batch_id <= ?
                GROUP BY security_code, event_no
            ) m ON m.security_code = a.security_code
               AND m.event_no = a.event_no
               AND a.revision = m.max_rev
            WHERE a.ingest_batch_id <= ? AND a.security_code = ?
            ORDER BY a.ex_date, a.event_no
            """,
            (cutoff_id, cutoff_id, code),
        ).fetchall()
    return [CorporateActionOut(**dict(r)) for r in rows]


# ---------------------------------------------------------------- 入账（交割单核对）
#
# 不变量
# ======
# 1. 成交只追加：同（账户, 证券, 成交号）同值幂等跳过，异值整次拒收。
# 2. 每次入账在单个写事务内「补录成交 + 全量重算 + 写底稿」；
#    结账日早于任一成交日、重算股数不足或写入失败 => 整体回滚，
#    不留成交、不留入账批次，上次成功入账的底稿仍可查询。
# 3. 重算口径：全部已入库成交按（交易日, 成交号）排序；除权日先按
#    截至 cutoff_batch 的有效公告调整各买入批次剩余股数（保留碎股，
#    剩余成本不变），再处理当日成交；卖出按买入先后（FIFO）逐批扣股。


def _to_cents(value: float | str) -> Decimal:
    """金额量化到分（ROUND_HALF_UP）。"""
    return Decimal(str(value)).quantize(CENT, rounding=ROUND_HALF_UP)


def _trade_signature(t: TradeIn) -> tuple:
    amt = None if t.amount is None else _to_cents(t.amount)
    return (t.trade_date, t.direction, Decimal(str(t.shares)), amt)


def _db_trade_signature(row: sqlite3.Row) -> tuple:
    amt = None if row["amount"] is None else _to_cents(row["amount"])
    return (row["trade_date"], row["direction"], Decimal(str(row["shares"])), amt)


def normalize_trades(rows: Iterable[TradeIn]) -> dict[str, TradeIn]:
    """同一请求内按成交号归一化：同号同值去重，同号异值拒收。"""
    chosen: dict[str, TradeIn] = {}
    for row in rows:
        old = chosen.get(row.trade_no)
        if old is not None:
            if _trade_signature(old) != _trade_signature(row):
                raise Rejected(
                    f"同号异值拒收：成交号 {row.trade_no} 在本次入账中出现不同值"
                )
            continue  # 同号同值：只记一次
        chosen[row.trade_no] = row
    return chosen


def _resolve_posting_cutoff(conn: sqlite3.Connection, cutoff: int | None) -> int:
    """入账采用的公告截止批次：缺省取最新批次；库中无批次时为 0（不用公告）。"""
    if cutoff is None:
        row = conn.execute("SELECT MAX(id) AS m FROM ingest_batches").fetchone()
        return int(row["m"]) if row["m"] is not None else 0
    if cutoff == 0:
        return 0
    exists = conn.execute(
        "SELECT 1 FROM ingest_batches WHERE id = ?", (cutoff,)
    ).fetchone()
    if exists is None:
        raise Rejected(f"公告截止批次 {cutoff} 不存在", 404)
    return cutoff


def _effective_actions_until(
    conn: sqlite3.Connection, code: str, closing_date: str, cutoff: int
) -> list[sqlite3.Row]:
    """截至 cutoff 已入库、除权日不晚于结账日的有效公告（每事件号最高修订）。"""
    return conn.execute(
        """
        SELECT a.event_no, a.ex_date, a.ratio, a.action_type,
               a.revision, a.ingest_batch_id
        FROM corporate_actions a
        JOIN (
            SELECT security_code, event_no, MAX(revision) AS max_rev
            FROM corporate_actions
            WHERE ingest_batch_id <= ?
            GROUP BY security_code, event_no
        ) m ON m.security_code = a.security_code
           AND m.event_no = a.event_no
           AND a.revision = m.max_rev
        WHERE a.ingest_batch_id <= ?
          AND a.security_code = ?
          AND a.ex_date <= ?
        ORDER BY a.ex_date, a.event_no
        """,
        (cutoff, cutoff, code, closing_date),
    ).fetchall()


def _replay(
    all_trades: list[tuple[str, str, str, Decimal, Decimal | None]],
    action_rows: list[sqlite3.Row],
) -> tuple[list[dict], list[dict], int, Decimal]:
    """按时间重放成交与公告，返回 (扣减明细, 结账日头寸, 卖出笔数, 结转成本合计)。

    all_trades 须已按（交易日, 成交号）排序：
    (trade_no, trade_date, direction, shares, amount)，股数/金额为 Decimal。
    """
    actions_by_date: dict[str, list[sqlite3.Row]] = {}
    for a in action_rows:
        actions_by_date.setdefault(a["ex_date"], []).append(a)
    trades_by_date: dict[str, list[tuple]] = {}
    for t in all_trades:
        trades_by_date.setdefault(t[1], []).append(t)

    # 买入批次（lot）：按买入先后排队，剩余股数/剩余成本用 Decimal 全程精算
    lots: list[dict] = []
    allocations: list[dict] = []
    sells = 0
    carried_total = Decimal("0")

    for day in sorted(set(actions_by_date) | set(trades_by_date)):
        # 除权日：先调整各笔剩余股数（保留碎股），剩余成本不变
        for a in actions_by_date.get(day, []):
            ratio = Decimal(str(a["ratio"]))
            for lot in lots:
                lot["shares"] *= ratio
        # 再处理当日成交（已按成交号排序）
        for trade_no, trade_date, direction, shares, amount in trades_by_date.get(
            day, []
        ):
            if direction == "buy":
                lots.append(
                    {
                        "trade_no": trade_no,
                        "trade_date": trade_date,
                        "shares": shares,
                        "cost": amount,
                    }
                )
                continue
            sells += 1
            need = shares
            while need > 0:
                lot = next((x for x in lots if x["shares"] > 0), None)
                if lot is None:
                    raise Rejected(
                        f"股数不足：卖出 {trade_no}（{trade_date}）还差 {need} 股"
                        "无买入来源，整次入账作废"
                    )
                take = min(need, lot["shares"])
                if take == lot["shares"]:
                    cost = lot["cost"]  # 扣完一批：结转全部剩余成本
                else:
                    # 按扣减股数占该批剩余股数的比例结转，四舍五入到分
                    cost = (lot["cost"] * take / lot["shares"]).quantize(
                        CENT, rounding=ROUND_HALF_UP
                    )
                lot["shares"] -= take
                lot["cost"] -= cost
                need -= take
                carried_total += cost
                allocations.append(
                    {
                        "sell_trade_no": trade_no,
                        "sell_trade_date": trade_date,
                        "buy_trade_no": lot["trade_no"],
                        "buy_trade_date": lot["trade_date"],
                        "shares_deducted": take,
                        "cost_carried": cost,
                    }
                )

    positions = [
        {
            "buy_trade_no": lot["trade_no"],
            "buy_trade_date": lot["trade_date"],
            "remaining_shares": lot["shares"],
            "remaining_cost": lot["cost"],
        }
        for lot in lots
    ]
    return allocations, positions, sells, carried_total


def create_posting(req: PostingRequest, db_file: str | None = None) -> dict:
    """入账：补录成交并按指定公告截止批次全量重算，写底稿。

    任一校验或写入失败 => 整个事务回滚（整次入账作废）。
    """
    normalized = normalize_trades(req.trades)
    with db.get_conn(db_file) as conn:
        try:
            with db.write_transaction(conn):
                _ensure_securities(conn, {req.security_code})
                cutoff_id = _resolve_posting_cutoff(conn, req.cutoff_batch)

                existing_rows = conn.execute(
                    "SELECT trade_no, trade_date, direction, shares, amount "
                    "FROM trades WHERE account = ? AND security_code = ?",
                    (req.account, req.security_code),
                ).fetchall()
                existing_by_no = {r["trade_no"]: r for r in existing_rows}

                # 归并补录成交：同号同值幂等跳过，同号异值拒收
                new_trades: list[TradeIn] = []
                for trade_no, t in normalized.items():
                    old = existing_by_no.get(trade_no)
                    if old is not None:
                        if _db_trade_signature(old) != _trade_signature(t):
                            raise Rejected(
                                f"同号异值拒收：成交号 {trade_no} 已入库不同值，"
                                "整次入账作废"
                            )
                        continue
                    new_trades.append(t)

                # 全量成交（已入库 + 本次新录），股数/金额转 Decimal
                all_trades: list[tuple[str, str, str, Decimal, Decimal | None]] = [
                    (
                        r["trade_no"],
                        r["trade_date"],
                        r["direction"],
                        Decimal(str(r["shares"])),
                        None if r["amount"] is None else _to_cents(r["amount"]),
                    )
                    for r in existing_rows
                ]
                all_trades += [
                    (
                        t.trade_no,
                        t.trade_date,
                        t.direction,
                        Decimal(str(t.shares)),
                        None if t.amount is None else _to_cents(t.amount),
                    )
                    for t in new_trades
                ]

                # 结账日不得早于任一成交日
                if all_trades:
                    latest_trade_date = max(t[1] for t in all_trades)
                    if req.closing_date < latest_trade_date:
                        raise Rejected(
                            f"结账日 {req.closing_date} 早于成交日 "
                            f"{latest_trade_date}，整次入账作废"
                        )

                # 成交按交易日、成交号排序
                all_trades.sort(key=lambda t: (t[1], t[0]))

                action_rows = _effective_actions_until(
                    conn, req.security_code, req.closing_date, cutoff_id
                )
                allocations, positions, sells, carried_total = _replay(
                    all_trades, action_rows
                )

                # 全部计算通过后一次性落库（同事务）
                cur = conn.execute(
                    "INSERT INTO postings(account, security_code, closing_date, "
                    "cutoff_batch, trades_received, trades_new, sells_processed, "
                    "cost_carried) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        req.account,
                        req.security_code,
                        req.closing_date,
                        cutoff_id,
                        len(req.trades),
                        len(new_trades),
                        sells,
                        float(carried_total),
                    ),
                )
                posting_id = cur.lastrowid

                conn.executemany(
                    "INSERT INTO trades(account, security_code, trade_no, "
                    "trade_date, direction, shares, amount, posting_id) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            req.account,
                            req.security_code,
                            t.trade_no,
                            t.trade_date,
                            t.direction,
                            t.shares,
                            t.amount,
                            posting_id,
                        )
                        for t in new_trades
                    ],
                )
                conn.executemany(
                    "INSERT INTO posting_allocations(posting_id, sell_trade_no, "
                    "sell_trade_date, buy_trade_no, buy_trade_date, "
                    "shares_deducted, cost_carried) VALUES (?, ?, ?, ?, ?, ?, ?)",
                    [
                        (
                            posting_id,
                            a["sell_trade_no"],
                            a["sell_trade_date"],
                            a["buy_trade_no"],
                            a["buy_trade_date"],
                            float(a["shares_deducted"]),
                            float(a["cost_carried"]),
                        )
                        for a in allocations
                    ],
                )
                conn.executemany(
                    "INSERT INTO posting_positions(posting_id, buy_trade_no, "
                    "buy_trade_date, remaining_shares, remaining_cost) "
                    "VALUES (?, ?, ?, ?, ?)",
                    [
                        (
                            posting_id,
                            p["buy_trade_no"],
                            p["buy_trade_date"],
                            float(p["remaining_shares"]),
                            float(p["remaining_cost"]),
                        )
                        for p in positions
                    ],
                )
        except sqlite3.IntegrityError as exc:
            # 唯一索引兜底：并发入账同一成交号时其中一个事务在此失败
            raise Rejected(f"入账写入与已入库数据冲突，整次作废：{exc}", 409) from exc

    return {
        "posting_id": posting_id,
        "account": req.account,
        "security_code": req.security_code,
        "closing_date": req.closing_date,
        "cutoff_batch": cutoff_id,
        "trades_received": len(req.trades),
        "trades_new": len(new_trades),
        "trades_total": len(all_trades),
        "sells_processed": sells,
        "cost_carried": float(carried_total),
    }


def list_postings(
    account: str | None = None,
    security_code: str | None = None,
    db_file: str | None = None,
) -> list[PostingOut]:
    sql = (
        "SELECT id, account, security_code, closing_date, cutoff_batch, "
        "trades_received, trades_new, sells_processed, cost_carried, created_at "
        "FROM postings"
    )
    cond, params = [], []
    if account is not None:
        cond.append("account = ?")
        params.append(account)
    if security_code is not None:
        cond.append("security_code = ?")
        params.append(security_code)
    if cond:
        sql += " WHERE " + " AND ".join(cond)
    sql += " ORDER BY id"
    with db.get_conn(db_file) as conn, db.read_snapshot(conn):
        rows = conn.execute(sql, params).fetchall()
    return [
        PostingOut(posting_id=r["id"], **{k: r[k] for k in r.keys() if k != "id"})
        for r in rows
    ]


def _worksheet_for(conn: sqlite3.Connection, posting: sqlite3.Row) -> WorksheetOut:
    allocs = conn.execute(
        "SELECT sell_trade_no, sell_trade_date, buy_trade_no, buy_trade_date, "
        "shares_deducted, cost_carried FROM posting_allocations "
        "WHERE posting_id = ? ORDER BY id",
        (posting["id"],),
    ).fetchall()
    positions = conn.execute(
        "SELECT buy_trade_no, buy_trade_date, remaining_shares, remaining_cost "
        "FROM posting_positions WHERE posting_id = ? ORDER BY id",
        (posting["id"],),
    ).fetchall()
    return WorksheetOut(
        posting_id=posting["id"],
        account=posting["account"],
        security_code=posting["security_code"],
        closing_date=posting["closing_date"],
        cutoff_batch=posting["cutoff_batch"],
        sells_processed=posting["sells_processed"],
        cost_carried=posting["cost_carried"],
        created_at=posting["created_at"],
        allocations=[AllocationOut(**dict(a)) for a in allocs],
        positions=[PositionOut(**dict(p)) for p in positions],
    )


def get_worksheet(posting_id: int, db_file: str | None = None) -> WorksheetOut:
    """按入账批次号查完整底稿（历史批次结果不可变）。"""
    with db.get_conn(db_file) as conn, db.read_snapshot(conn):
        posting = conn.execute(
            "SELECT * FROM postings WHERE id = ?", (posting_id,)
        ).fetchone()
        if posting is None:
            raise Rejected(f"入账批次 {posting_id} 不存在", 404)
        return _worksheet_for(conn, posting)


def get_latest_worksheet(
    account: str, security_code: str, db_file: str | None = None
) -> WorksheetOut:
    """该账户+证券最近一次成功入账的完整底稿（含采用的公告截止批次）。"""
    with db.get_conn(db_file) as conn, db.read_snapshot(conn):
        posting = conn.execute(
            "SELECT * FROM postings WHERE account = ? AND security_code = ? "
            "ORDER BY id DESC LIMIT 1",
            (account, security_code),
        ).fetchone()
        if posting is None:
            raise Rejected(
                f"账户 {account} 证券 {security_code} 尚无成功入账", 404
            )
        return _worksheet_for(conn, posting)


def list_trades(
    account: str, security_code: str, db_file: str | None = None
) -> list[TradeOut]:
    """已入库成交清单（按交易日、成交号排序），便于交割单核对。"""
    with db.get_conn(db_file) as conn, db.read_snapshot(conn):
        rows = conn.execute(
            "SELECT trade_no, trade_date, direction, shares, amount, posting_id "
            "FROM trades WHERE account = ? AND security_code = ? "
            "ORDER BY trade_date, trade_no",
            (account, security_code),
        ).fetchall()
    return [TradeOut(**dict(r)) for r in rows]
