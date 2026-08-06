FROM python:3.14.0-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN groupadd --gid 10001 jellyfix \
    && useradd --uid 10001 --gid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin --no-create-home jellyfix

COPY requirements.txt .
RUN pip install --no-cache-dir --requirement requirements.txt

COPY app ./app
COPY main.py .

RUN mkdir -p /data && chown -R jellyfix:jellyfix /data /app

USER 10001:10001
EXPOSE 8000

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000", "--proxy-headers", "--forwarded-allow-ips", "*"]
