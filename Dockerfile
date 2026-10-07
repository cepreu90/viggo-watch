FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app/ .

ENV STATE_PATH=/data/seen.json
VOLUME ["/data"]

CMD ["python", "-u", "main.py"]
