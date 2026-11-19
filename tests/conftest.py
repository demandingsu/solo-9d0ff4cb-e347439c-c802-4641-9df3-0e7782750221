"""pytest 公共夹具：每个用例独立临时 SQLite 库 + TestClient。"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

from app import db as db_mod
from app.main import create_app


@pytest.fixture()
def db_file(tmp_path, monkeypatch):
    path = str(tmp_path / "test_market.db")
    monkeypatch.setenv("MARKET_DB_PATH", path)
    db_mod.init_db(path)
    return path


@pytest.fixture()
def client(db_file):
    app = create_app()
    with TestClient(app) as c:
        yield c


def quote(code="AAA", date="2026-01-02", rev=0, o=100.0, h=102.0,
          low=98.0, close=100.0, vol=1000.0):
    return {
        "security_code": code,
        "trade_date": date,
        "revision": rev,
        "open": o,
        "high": h,
        "low": low,
        "close": close,
        "volume": vol,
    }


def action(code="AAA", event="E1", ex="2026-01-05", ratio=2.0, rev=0,
           action_type="split"):
    return {
        "security_code": code,
        "event_no": event,
        "ex_date": ex,
        "ratio": ratio,
        "revision": rev,
        "action_type": action_type,
    }


def post_quotes(client, rows):
    return client.post("/imports/quotes", json={"quotes": rows})


def post_actions(client, rows):
    return client.post("/imports/corporate-actions", json={"actions": rows})


def get_adjusted(client, code, start, end, cutoff=None):
    params = {"security_code": code, "start_date": start, "end_date": end}
    if cutoff is not None:
        params["cutoff_batch"] = cutoff
    return client.get("/quotes/adjusted", params=params)
