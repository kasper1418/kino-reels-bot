FROM python:3.12-slim

RUN apt-get update \
 && apt-get install -y --no-install-recommends ffmpeg fonts-dejavu-core fonts-montserrat \
 && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY *.py ./

ENV WORK_DIR=/data/work \
    SESSION_PATH=/data/bot \
    PYTHONUNBUFFERED=1

CMD ["python", "bot.py"]
