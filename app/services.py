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
    BatchOut,
    CorporateActionIn,
    CorporateActionOut,
    EventBasis,
    QuoteIn,
    RawQuote,
)

# 折算值保留 5 位小数（ROUND_HALF_UP，金融惯例）
QUANT = Decimal("0.00001")


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
