FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MARKET_DB_PATH=/data/market.db

WORKDIR /srv

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 容器内以模块方式启动，避免工作目录依赖
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
