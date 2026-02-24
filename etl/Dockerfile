FROM python:3.13-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

# How often to run ETL in seconds (default 900 = 15 min)
ENV ETL_INTERVAL=900

CMD ["sh", "-c", "while true; do python health_to_influx.py; sleep ${ETL_INTERVAL}; done"]