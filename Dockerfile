FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    DB_PATH=/data/allergens.db \
    HOST=0.0.0.0 \
    PORT=8000

WORKDIR /app

# 先装依赖，利用层缓存
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app

RUN mkdir -p /data
VOLUME ["/data"]

EXPOSE 8000

# 容器启动：uvicorn 触发 lifespan，自动幂等建表；SEED_DEMO=1 时写入演示数据
CMD ["sh", "-c", "uvicorn app.main:app --host ${HOST} --port ${PORT}"]
