FROM python:3.13-slim
WORKDIR /srv
ENV PYTHONUNBUFFERED=1
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY app ./app
ENV DB_PATH=/data/app.db
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
