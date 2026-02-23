#!/usr/bin/env python3
"""
ETL: Fetch decrypted health data from the Health Collector API for each user,
then write to InfluxDB with a 'user' tag so Grafana can show all family data.
Run on a schedule (cron every 15-30 min). Requires HEALTH_API_URL, INFLUX_* env and HEALTH_USERS.
"""
import os
import json
import sys
from datetime import datetime
from urllib.parse import urljoin

import requests
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS

# Data types to fetch (API method names)
METHODS = [
    "activeCaloriesBurned",
    "bloodGlucose",
    "bloodPressure",
    "bodyFat",
    "distance",
    "elevationGained",
    "floorsClimbed",
    "heartRate",
    "height",
    "hydration",
    "nutrition",
    "oxygenSaturation",
    "respiratoryRate",
    "restingHeartRate",
    "sleepSession",
    "speed",
    "steps",
    "stepsCadence",
    "totalCaloriesBurned",
    "weight",
]


def load_users():
    users_raw = os.environ.get("HEALTH_USERS")
    if not users_raw:
        path = os.environ.get("HEALTH_USERS_FILE", os.path.join(os.path.dirname(__file__), "users.json"))
        if os.path.isfile(path):
            with open(path) as f:
                users_raw = f.read()
        else:
            print("Set HEALTH_USERS (JSON array of 'username:password') or HEALTH_USERS_FILE path.", file=sys.stderr)
            sys.exit(1)
    try:
        data = json.loads(users_raw)
    except json.JSONDecodeError:
        # Allow "user1:pass1,user2:pass2"
        data = [u.strip() for u in users_raw.split(",") if ":" in u]
        data = [{"username": u.split(":")[0], "password": u.split(":", 1)[1]} for u in data]
    if not data:
        sys.exit(1)
    if isinstance(data[0], str) and ":" in data[0]:
        data = [{"username": u.split(":")[0], "password": u.split(":", 1)[1]} for u in data]
    return data


def login(api_base: str, username: str, password: str) -> str:
    r = requests.post(
        urljoin(api_base, "/api/v2/login"),
        json={"username": username, "password": password},
        headers={"Content-Type": "application/json"},
        timeout=30,
    )
    r.raise_for_status()
    return r.json()["token"]


def fetch_method(api_base: str, token: str, method: str) -> list:
    r = requests.post(
        urljoin(api_base, "/api/v2/fetch/" + method),
        json={},
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        timeout=60,
    )
    if r.status_code == 404 or (r.status_code == 200 and not r.text.strip()):
        return []
    r.raise_for_status()
    return r.json() if r.text else []


def parse_ts(s: str) -> datetime:
    if not s:
        return None
    try:
        if "Z" in s or "+" in s or s.count("-") >= 2:
            return datetime.fromisoformat(s.replace("Z", "+00:00"))
        return datetime.fromisoformat(s)
    except Exception:
        return None


def doc_to_points(method: str, user: str, doc: dict) -> list:
    """Convert one API doc to InfluxDB Point(s). Returns list of Point."""
    start = parse_ts(doc.get("start"))
    end = parse_ts(doc.get("end"))
    data = doc.get("data") or {}
    points = []

    if method == "heartRate" and "samples" in data:
        for s in data["samples"]:
            t = parse_ts(s.get("time")) or start
            if t is None:
                continue
            bpm = s.get("beatsPerMinute")
            if bpm is not None:
                points.append(
                    Point("heartRate")
                    .tag("user", user)
                    .field("bpm", int(bpm))
                    .time(t)
                )
        return points

    if method == "sleepSession":
        start_t = parse_ts(data.get("startTime")) or start
        end_t = parse_ts(data.get("endTime")) or end
        if start_t and end_t:
            duration_sec = (end_t - start_t).total_seconds()
            points.append(
                Point("sleepSession")
                .tag("user", user)
                .field("duration_seconds", duration_sec)
                .time(start_t)
            )
        for stage in data.get("stages") or []:
            st = parse_ts(stage.get("startTime"))
            et = parse_ts(stage.get("endTime"))
            if st:
                points.append(
                    Point("sleepStage")
                    .tag("user", user)
                    .field("stage", int(stage.get("stage", 0)))
                    .field("duration_seconds", (et - st).total_seconds() if et else 0)
                    .time(st)
                )
        return points

    if method == "steps":
        count = data.get("count")
        if count is not None and start:
            points.append(
                Point("steps").tag("user", user).field("count", int(count)).time(start)
            )
        return points

    if method == "activeCaloriesBurned" or method == "totalCaloriesBurned":
        energy = (data.get("energy") or {}).get("inKilocalories")
        if energy is not None and start:
            points.append(
                Point(method).tag("user", user).field("kcal", float(energy)).time(start)
            )
        return points

    if method == "weight":
        kg = (data.get("weight") or {}).get("inKilograms")
        if kg is not None and start:
            points.append(
                Point("weight").tag("user", user).field("kg", float(kg)).time(start)
            )
        return points

    if method == "distance":
        meters = (data.get("length") or {}).get("inMeters")
        if meters is not None and start:
            points.append(
                Point("distance").tag("user", user).field("meters", float(meters)).time(start)
            )
        return points

    if method in ("oxygenSaturation", "respiratoryRate", "restingHeartRate"):
        samples = data.get("samples", [])
        if not samples and start:
            v = data.get("percentage") or data.get("breathsPerMinute") or data.get("beatsPerMinute")
            if v is not None:
                points.append(
                    Point(method).tag("user", user).field("value", float(v)).time(start)
                )
        for s in samples:
            t = parse_ts(s.get("time")) or start
            v = s.get("percentage") or s.get("breathsPerMinute") or s.get("beatsPerMinute")
            if t and v is not None:
                points.append(
                    Point(method).tag("user", user).field("value", float(v)).time(t)
                )
        return points

    # Generic: one point per doc with start time and a few numeric fields
    if start is None:
        return points
    p = Point(method).tag("user", user).time(start)
    if "energy" in data and data["energy"].get("inKilocalories") is not None:
        p = p.field("kcal", float(data["energy"]["inKilocalories"]))
    if "count" in data:
        p = p.field("count", int(data["count"]))
    if "length" in data and data["length"].get("inMeters") is not None:
        p = p.field("meters", float(data["length"]["inMeters"]))
    if "weight" in data and data["weight"].get("inKilograms") is not None:
        p = p.field("kg", float(data["weight"]["inKilograms"]))
    points.append(p)
    return points


def main():
    api_base = os.environ.get("HEALTH_API_URL", "http://127.0.0.1:6644").rstrip("/") + "/"
    influx_url = os.environ.get("INFLUX_URL", "http://127.0.0.1:8086")
    influx_token = os.environ.get("INFLUX_TOKEN")
    influx_org = os.environ.get("INFLUX_ORG", "family")
    influx_bucket = os.environ.get("INFLUX_BUCKET", "health")

    if not influx_token:
        print("Set INFLUX_TOKEN (and optionally INFLUX_URL, INFLUX_ORG, INFLUX_BUCKET).", file=sys.stderr)
        sys.exit(1)

    users = load_users()
    write_api = InfluxDBClient(url=influx_url, token=influx_token, org=influx_org).write_api(write_options=SYNCHRONOUS)
    total = 0

    for u in users:
        username = u.get("username") or u.get("user")
        password = u.get("password")
        if not username or not password:
            continue
        try:
            token = login(api_base, username, password)
        except Exception as e:
            print(f"Login failed for {username}: {e}", file=sys.stderr)
            continue
        for method in METHODS:
            try:
                docs = fetch_method(api_base, token, method)
            except Exception as e:
                print(f"Fetch {method} for {username}: {e}", file=sys.stderr)
                continue
            for doc in docs:
                for point in doc_to_points(method, username, doc):
                    write_api.write(bucket=influx_bucket, record=point)
                    total += 1
    print(f"Wrote {total} points to InfluxDB.")


if __name__ == "__main__":
    main()
