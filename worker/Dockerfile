FROM python:3.13-slim
WORKDIR /app
COPY worker/worker.py ./worker.py
ENV PYTHONUNBUFFERED=1
CMD ["python", "-B", "worker.py"]
