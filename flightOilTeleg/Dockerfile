# Dockerfile — goWell
FROM python:3.11-slim

WORKDIR /app

# System deps for lxml (used by beautifulsoup4)
RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc libxml2-dev libxslt-dev \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY flightOilTeleg.py .
COPY main.py .
COPY template.html .

# Optional: include pre-authorised Telegram session file if present
# Upload osint_session.session to this folder in GitHub to enable Telegram
COPY osint_session.session* ./

ENV PORT=8000

CMD ["sh", "-c", "uvicorn main:app --host 0.0.0.0 --port ${PORT}"]
