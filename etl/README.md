# ETL: MongoDB → InfluxDB (for Grafana)

Reads health data from MongoDB (per-user DBs), decrypts locally with the same key derivation as the API, and writes to InfluxDB with a `user` tag. No API or users file: ETL runs on the same host as Mongo (e.g. homelab).

## Setup

1. **InfluxDB + Grafana** (on homelab or same machine):

   From repo root:

   ```bash
   mkdir -p monitoring/influxdb-data monitoring/grafana-data
   export INFLUX_TOKEN="$(openssl rand -hex 32)"
   docker compose -f docker-compose.monitoring.yml up -d
   ```

   Save `INFLUX_TOKEN` for the ETL and for Grafana's InfluxDB datasource.

2. **ETL config**

   - Set `MONGO_URI` to the same Mongo the API uses (e.g. homelab). If ETL runs in Docker and Mongo is on the host: `mongodb://user:pass@host.docker.internal:27017/?authSource=admin`.
   - Copy `etl/.env.example` to `etl/.env` and set:
     - `MONGO_URI` – required
     - `INFLUX_URL` – e.g. `http://127.0.0.1:8086` (or `http://influxdb:8086` inside Docker)
     - `INFLUX_TOKEN` – same token as the monitoring stack

   Optional: `MONGO_USERS_DB` (default `hcgateway`), `MONGO_DATA_DB_PREFIX` (default `hcgateway_`) if your API uses different DB names.

3. **Run ETL once**

   ```bash
   cd etl
   pip install -r requirements.txt
   python health_to_influx.py
   ```

4. **Schedule ETL** (e.g. every 15 minutes)

   Crontab, systemd timer, or run the ETL container from `docker-compose.monitoring.yml` (it loops with `ETL_INTERVAL`).

## Grafana

1. Open Grafana (default http://localhost:3000, login admin / your `GRAFANA_ADMIN_PASSWORD`).
2. Add datasource: **InfluxDB**, URL `http://influxdb:8086` (if Grafana runs in Docker next to Influx) or `http://localhost:8086`. Auth: **Token**, token = your `INFLUX_TOKEN`. Organization = **family**, default bucket = **health**.
3. Create a **Variable** named `user`: type Query, InfluxDB Flux:
   ```flux
   import "influxdata/influxdb/schema"
   schema.tagValues(bucket: "health", tag: "user")
   ```
   Add an "All" option if you want.
4. **Panels** – example Flux for heart rate:
   ```flux
   from(bucket: "health")
     |> range(start: v.timeRangeStart, stop: v.timeRangeStop)
     |> filter(fn: (r) => r["_measurement"] == "heartRate")
     |> filter(fn: (r) => r["user"] == v.user or v.user == "All")
   ```
   Similar for `steps`, `sleepSession`, `weight`, etc.

All users with access to this Grafana instance can see every family member's data by selecting the user in the dropdown or "All".
