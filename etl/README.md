# ETL: Health API → InfluxDB (for Grafana)

Fetches decrypted health data from the Health Collector API for each configured user and writes it to InfluxDB with a `user` tag. Grafana can then show all family members' data in one place.

## Setup

1. **InfluxDB + Grafana** (on homelab or same machine):

   From repo root:

   ```bash
   mkdir -p monitoring/influxdb-data monitoring/grafana-data
   export INFLUX_TOKEN="$(openssl rand -hex 32)"
   docker compose -f docker-compose.monitoring.yml up -d
   ```

   Save `INFLUX_TOKEN`; you need it for the ETL and for Grafana's InfluxDB datasource.

2. **ETL config**

   - Copy `etl/users.json.example` to `etl/users.json` and set real username/password for each family member (same as app login).
   - Copy `etl/.env.example` to `etl/.env` and set:
     - `HEALTH_API_URL` – your API base URL (e.g. `http://YOUR_VPS_IP:6644` or `https://api.example.com`)
     - `INFLUX_URL` – usually `http://127.0.0.1:8086` if ETL runs on same host as InfluxDB
     - `INFLUX_TOKEN` – same token you used when starting the monitoring stack

3. **Run ETL once**

   ```bash
   cd etl
   pip install -r requirements.txt
   python health_to_influx.py
   ```

4. **Schedule ETL** (e.g. every 15 minutes)

   ```bash
   # crontab -e
   */15 * * * * cd /path/to/HealthCollectorApp/etl && set -a && [ -f .env ] && . .env && set +a && python3 health_to_influx.py >> /var/log/health-etl.log 2>&1
   ```

   Or with a systemd timer, or run the ETL inside a small container that loops + sleep.

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
   Similar for `steps`, `sleepSession`, `weight`, etc. (measurement names match the ETL.)

All users with access to this Grafana instance can see every family member's data by selecting the user in the dropdown or "All".
