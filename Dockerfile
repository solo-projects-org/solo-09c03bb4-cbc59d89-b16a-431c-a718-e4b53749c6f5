FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    SETTLE_DB=/data/settle.db \
    SETTLE_PACKAGES=/data/packages

WORKDIR /srv

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY wsgi.py ./

RUN mkdir -p /data/packages
VOLUME ["/data"]

EXPOSE 8000

# 单 worker + 线程，配合进程内写锁；SQLite WAL 保证后台打包线程可读可写
CMD ["gunicorn", "--bind", "0.0.0.0:8000", "--workers", "1", "--threads", "8", "--timeout", "120", "wsgi:app"]
