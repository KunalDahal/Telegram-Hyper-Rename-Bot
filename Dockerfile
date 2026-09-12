FROM python:3.11-slim

WORKDIR /app

RUN apt-get update && apt-get install -y \
    ffmpeg \
    cpulimit \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .

RUN pip install --no-cache-dir -r requirements.txt

COPY run.py .
COPY src/ ./src/

RUN mkdir -p src/bin/ffmpeg src/bin/tmp src/bin/users src/bin/logs

ENV PYTHONUNBUFFERED=1

CMD ["python", "run.py"]