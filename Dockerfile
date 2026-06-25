FROM python:3.11-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    MP3_TO_TEXT_NO_BROWSER=1 \
    MP3_TO_TEXT_HOST=0.0.0.0 \
    WHISPER_LOCAL_FILES_ONLY=0 \
    WHISPER_DEVICE=cpu \
    WHISPER_COMPUTE_TYPE=int8

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app.py index.html ./

CMD ["python", "app.py"]
