"""FastAPI 应用入口。

启动后访问 ``/docs`` 即可在 Swagger UI 中导入与查询。
"""

from __future__ import annotations

import sqlite3
from contextlib import asynccontextmanager

from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse

from . import db, services
from .models import (
    AdjustedQuote,
    BatchOut,
    CorporateActionImportRequest,
    CorporateActionOut,
    ImportResponse,
    QuoteImportRequest,
    RawQuote,
)
from .services import Rejected


def create_app() -> FastAPI:
    @asynccontextmanager
    async def lifespan(_app: FastAPI):
        db.init_db()
        yield

    app = FastAPI(
        title="拆股复权行情 API",
        version="1.0.0",
        description=(
            "版本化日行情与拆股/并股公告导入，按入库截止批次返回"
            "前后可比的复权价量。**后来补报的公告不会改写旧批次结果。**"
        ),
        lifespan=lifespan,
    )

    @app.exception_handler(Rejected)
    async def _rejected_handler(_request, exc: Rejected) -> JSONResponse:
        return JSONResponse(
            status_code=exc.status_code, content={"detail": exc.message}
        )

    @app.exception_handler(sqlite3.OperationalError)
    async def _locked_handler(_request, exc: sqlite3.OperationalError) -> JSONResponse:
        # busy_timeout 耗尽（理论上 30s 内串行写不会触发）
        return JSONResponse(
            status_code=503,
            content={"detail": f"数据库繁忙，请重试：{exc}"},
        )

    @app.get("/health", tags=["运维"])
    def health() -> dict:
        return {"status": "ok"}

    # ---------- 导入 ----------

    @app.post(
        "/imports/quotes",
        response_model=ImportResponse,
        status_code=201,
        tags=["导入"],
        summary="导入日开高低收与成交量（带修订号，成功后生成入库批次）",
    )
    def import_quotes(req: QuoteImportRequest) -> ImportResponse:
        result = services.import_quotes(req.quotes)
        return ImportResponse(**result)

    @app.post(
        "/imports/corporate-actions",
        response_model=ImportResponse,
        status_code=201,
        tags=["导入"],
        summary="导入拆股/并股公告（带事件号、除权日、比例、修订号）",
    )
    def import_actions(req: CorporateActionImportRequest) -> ImportResponse:
        result = services.import_actions(req.actions)
        return ImportResponse(**result)

    # ---------- 查询 ----------

    @app.get(
        "/batches",
        response_model=list[BatchOut],
        tags=["查询"],
        summary="列出全部入库批次（批次号即截止点）",
    )
    def list_batches() -> list[BatchOut]:
        return services.list_batches()

    @app.get(
        "/quotes/adjusted",
        response_model=list[AdjustedQuote],
        tags=["查询"],
        summary="复权日行情：原值 + 折算值 + 事件依据",
        description=(
            "以 `end_date` 为基准日，只使用截至 `cutoff_batch` 已入库、"
            "除权日晚于行情日且不晚于基准日的公告折算早期价量。"
            "比例 ratio 表示老 1 股 -> 新 ratio 股（1 拆 2 填 2.0，"
            "价格减半、成交量加倍；2 并 1 填 0.5）。"
        ),
    )
    def adjusted_quotes(
        security_code: str = Query(..., description="证券代码"),
        start_date: str = Query(..., description="区间起日 YYYY-MM-DD"),
        end_date: str = Query(..., description="区间末日（复权基准日）YYYY-MM-DD"),
        cutoff_batch: int | None = Query(
            None, description="截止批次号；缺省=当前最新已提交批次"
        ),
    ) -> list[AdjustedQuote]:
        return services.query_adjusted_quotes(
            security_code, start_date, end_date, cutoff_batch
        )

    @app.get(
        "/quotes/raw",
        response_model=list[RawQuote],
        tags=["查询"],
        summary="原值日行情查询（截止批次下的生效修订）",
    )
    def raw_quotes(
        security_code: str,
        start_date: str,
        end_date: str,
        cutoff_batch: int | None = Query(None),
    ) -> list[RawQuote]:
        return services.query_raw_quotes(
            security_code, start_date, end_date, cutoff_batch
        )

    @app.get(
        "/corporate-actions",
        response_model=list[CorporateActionOut],
        tags=["查询"],
        summary="公告查询（截止批次下每事件号的生效修订）",
    )
    def actions(
        security_code: str,
        cutoff_batch: int | None = Query(None),
    ) -> list[CorporateActionOut]:
        return services.query_actions(security_code, cutoff_batch)

    return app


app = create_app()
