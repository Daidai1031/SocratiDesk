FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV PORT=8080

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY main.py .
COPY upload.html .

RUN mkdir -p /tmp/textbooks

CMD exec uvicorn main:app --host 0.0.0.0 --port ${PORT}