FROM mcr.microsoft.com/playwright/python:v1.49.0-jammy

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app.py .

ENV HOSTED=1
# One worker on purpose: job state is kept in memory.
CMD gunicorn -w 1 --threads 8 -b 0.0.0.0:${PORT:-8000} --timeout 120 app:app
