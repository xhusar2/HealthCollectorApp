#!/usr/bin/env python3
"""
ETL: Read health data from MongoDB (per-user DBs), decrypt locally with the same
key derivation as the API, and write to InfluxDB with a 'user' tag for Grafana.
No API or users file: requires MONGO_URI and INFLUX_* env. Run on a schedule (e.g. ETL_INTERVAL).
"""
import os
import json
import sys
import base64
from datetime import datetime

from cryptography.fernet import Fernet
from influxdb_client import InfluxDBClient, Point
from influxdb_client.client.write_api import SYNCHRONOUS
import pymongo

# Collection names in Mongo (must match API method names)
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


def derive_key(hashed_password: str) -> bytes:
    """Same as API: key from hashed password for Fernet."""
    return base64.urlsafe_b64encode(hashed_password.encode("utf-8").ljust(32)[:32])


def parse_ts(s) -> datetime:
    if s is None:
        return None
    if isinstance(s, datetime):
        return s
    try:
        ss = str(s)
        if "Z" in ss or "+" in ss or ss.count("-") >= 2:
            return datetime.fromisoformat(ss.replace("Z", "+00:00"))
        return datetime.fromisoformat(ss)
    except Exception:
        return None


def doc_to_points(method: str, user: str, doc: dict) -> list:
    """Convert one API-style doc (with decrypted 'data') to InfluxDB Point(s)."""
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
    mongo_uri = os.environ.get("MONGO_URI")
    if not mongo_uri:
        print("Set MONGO_URI (e.g. mongodb://user:pass@host:27017/?authSource=admin).", file=sys.stderr)
        sys.exit(1)

    influx_url = os.environ.get("INFLUX_URL", "http://127.0.0.1:8086")
    influx_token = os.environ.get("INFLUX_TOKEN")
    influx_org = os.environ.get("INFLUX_ORG", "family")
    influx_bucket = os.environ.get("INFLUX_BUCKET", "health")

    if not influx_token:
        print("Set INFLUX_TOKEN (and optionally INFLUX_URL, INFLUX_ORG, INFLUX_BUCKET).", file=sys.stderr)
        sys.exit(1)

    users_db_name = os.environ.get("MONGO_USERS_DB", "heathconnectapp")
    data_db_prefix = os.environ.get("MONGO_DATA_DB_PREFIX", "heathconnectapp_")

    client = pymongo.MongoClient(mongo_uri)
    users_db = client[users_db_name]
    users_coll = users_db["users"]

    write_api = InfluxDBClient(url=influx_url, token=influx_token, org=influx_org).write_api(write_options=SYNCHRONOUS)
    total = 0

    for user_doc in users_coll.find({}, {"_id": 1, "username": 1, "password": 1}):
        user_id = str(user_doc["_id"])
        username = user_doc.get("username") or user_id
        hashed = user_doc.get("password")
        if not hashed:
            print(f"Skipping user {username}: no password hash.", file=sys.stderr)
            continue

        try:
            key = derive_key(hashed)
            fernet = Fernet(key)
        except Exception as e:
            print(f"Skipping user {username}: key derivation failed: {e}", file=sys.stderr)
            continue

        data_db_name = data_db_prefix + user_id
        if data_db_name not in client.list_database_names():
            continue

        data_db = client[data_db_name]
        for method in METHODS:
            coll = data_db[method]
            for doc in coll.find({}):
                enc = doc.get("data")
                if not enc:
                    continue
                try:
                    decrypted = json.loads(fernet.decrypt(enc.encode()).decode())
                except Exception as e:
                    print(f"Decrypt failed {username}/{method} doc {doc.get('_id')}: {e}", file=sys.stderr)
                    continue
                out_doc = {"start": doc.get("start"), "end": doc.get("end"), "data": decrypted}
                for point in doc_to_points(method, username, out_doc):
                    write_api.write(bucket=influx_bucket, record=point)
                    total += 1

    print(f"Wrote {total} points to InfluxDB.")


if __name__ == "__main__":
    main()
