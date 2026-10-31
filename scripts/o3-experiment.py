#!/usr/bin/env python3
"""O3: bounded read overload, raw alerts API evidence, and automatic recovery.

Run from the repo root on the lab host after enrolld and Prometheus are ready:
  python3 scripts/o3-experiment.py runs/o3-alert
Existing output directories are never overwritten. No service is restarted.
"""
import datetime as dt
import hashlib
import json
import pathlib
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request

PROM = "http://127.0.0.1:9090"
SVC = "http://127.0.0.1:8080"
ALERT = "EnrolldReadSLOBurnRateHigh"
PHASES = [("baseline", 100, 300), ("overload", 600, 120), ("recovery", 100, 360)]


def now():
    return dt.datetime.now(dt.timezone.utc).isoformat()


def get(url):
    with urllib.request.urlopen(url, timeout=10) as response:
        return response.read()


def main():
    out = pathlib.Path(sys.argv[1] if len(sys.argv) > 1 else "runs/o3-alert")
    config = json.loads(get(SVC + "/config"))
    assert (config["pool"], config["db_ms"], config["work"]) == (4, 10, 200000), config
    assert config["pool_timeout"] == "1s", config
    targets = json.loads(get(PROM + "/api/v1/targets"))["data"]["activeTargets"]
    assert all(any(t["labels"]["job"] == job and t["health"] == "up" for t in targets)
               for job in ("enrolld", "node")), targets
    rules = get(PROM + "/api/v1/rules")
    assert ALERT in rules.decode(), "Load deploy/rules.yml before measuring"
    out.mkdir(parents=True, exist_ok=False)
    shutil.copyfile("deploy/o3-prediction.md", out / "prediction.md")
    for name in ("prometheus.yml", "rules.yml"):
        shutil.copyfile("deploy/" + name, out / name)
    meta = {"started_utc": now(), "phases": PHASES, "config": config,
            "slo": {"target": 0.99, "latency_seconds": 0.05, "period": "30d"},
            "generator_cpus": "0-1", "generator_seed": 1,
            "service_affinity": subprocess.check_output(
                ["taskset", "-pc", str(config["pid"])], text=True).strip(),
            "source_commit": subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip(),
            "sha256": {p: hashlib.sha256(pathlib.Path(p).read_bytes()).hexdigest()
                       for p in ("bin/enrolld", "bin/loadgen", "deploy/rules.yml", "scripts/o3-experiment.py")}}
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    (out / "rules-loaded.json").write_bytes(rules)
    (out / "metrics-before.txt").write_bytes(get(SVC + "/metrics"))
    (out / "alerts-before.json").write_bytes(get(PROM + "/api/v1/alerts"))
    seen = set()
    previous = None
    with (out / "alerts-timeline.jsonl").open("w") as timeline:
        for phase, rate, seconds in PHASES:
            cmd = ["taskset", "-c", "0-1", "bin/loadgen", "-url", SVC + "/api/courses/1",
                   "-rates", str(rate), "-step", str(seconds) + "s", "-slo", "50ms",
                   "-arrival", "poisson", "-seed", "1", "-timeout", "2s",
                   "-max-inflight", "2000", "-out", str(out / (phase + "-steps.csv"))]
            phase_meta = {"phase": phase, "started_utc": now(), "command": cmd}
            print(json.dumps(phase_meta), flush=True)
            with (out / (phase + "-loadgen.log")).open("w") as log:
                process = subprocess.Popen(cmd, stdout=log, stderr=subprocess.STDOUT)
                try:
                    while True:
                        raw = get(PROM + "/api/v1/alerts")
                        response = json.loads(raw)
                        matches = [a for a in response["data"]["alerts"] if a["labels"]["alertname"] == ALERT]
                        state = matches[0]["state"] if matches else "inactive"
                        sample = {"observed_at": now(), "phase": phase, "state": state, "response": response}
                        timeline.write(json.dumps(sample, ensure_ascii=False) + "\n")
                        timeline.flush()
                        if state != previous:
                            print(json.dumps({k: sample[k] for k in ("observed_at", "phase", "state")}), flush=True)
                        if state in ("pending", "firing") and state not in seen:
                            (out / ("alerts-" + state + ".json")).write_bytes(raw)
                            seen.add(state)
                        if state == "inactive" and "firing" in seen and "resolved" not in seen:
                            (out / "alerts-resolved.json").write_bytes(raw)
                            seen.add("resolved")
                        previous = state
                        if process.poll() is not None:
                            if process.returncode:
                                raise RuntimeError(f"loadgen failed: {process.returncode}")
                            break
                        time.sleep(2)
                finally:
                    if process.poll() is None:
                        process.terminate()
                        try:
                            process.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            process.kill()
                            process.wait()
            phase_meta["ended_utc"] = now()
            (out / (phase + "-meta.json")).write_text(json.dumps(phase_meta, indent=2) + "\n")
            query = '{__name__=~"enrolld:read_.*"}'
            (out / (phase + "-slo.json")).write_bytes(get(PROM + "/api/v1/query?" + urllib.parse.urlencode({"query": query})))
            (out / (phase + "-metrics.txt")).write_bytes(get(SVC + "/metrics"))
    meta["ended_utc"] = now()
    meta["observed_states"] = sorted(seen)
    (out / "meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2) + "\n")
    (out / "targets-after.json").write_bytes(get(PROM + "/api/v1/targets"))
    assert seen == {"pending", "firing", "resolved"}, seen
    print("Verified pending -> firing -> resolved", flush=True)


if __name__ == "__main__":
    main()
