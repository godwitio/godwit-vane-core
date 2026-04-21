FROM python:3.11-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
ENV DB_PATH=/data/godwit_vane.db
ENV MODEL_DIR=/data
VOLUME ["/data"]
CMD ["python", "src/monitor.py"]
