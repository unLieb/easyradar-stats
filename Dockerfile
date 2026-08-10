FROM python:3-alpine

COPY stats-service.py /app/stats-service.py

CMD ["python3", "/app/stats-service.py"]
