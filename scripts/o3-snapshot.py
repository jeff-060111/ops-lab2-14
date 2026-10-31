#!/usr/bin/env python3
"""Export every dashboard query and a self-contained offline HTML after O3.

  python3 scripts/o3-snapshot.py runs/o3-alert [http://127.0.0.1:9090]
The archive contains actual Prometheus API responses, not reconstructed curves.
"""
import datetime as dt
import json
import pathlib
import shutil
import subprocess
import sys
import urllib.parse
import urllib.request


def main():
    out = pathlib.Path(sys.argv[1])
    prom = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:9090"
    meta = json.loads((out / "meta.json").read_text())
    start = dt.datetime.fromisoformat(meta["started_utc"]).timestamp()
    end = dt.datetime.fromisoformat(meta["ended_utc"]).timestamp()
    # Reuse the lab's standard per-phase RED/USE aggregation for the report.
    for kind in ("red", "use"):
        source = "deploy/queries-" + kind + ".txt"
        shutil.copyfile(source, out / ("queries-" + kind + ".txt"))
        for phase, _, _ in meta["phases"]:
            with (out / (phase + "-" + kind + ".csv")).open("w") as output:
                subprocess.run(["bin/promsteps", "-steps", str(out / (phase + "-steps.csv")),
                                "-prom", prom, "-var", "route=/api/courses/{id}",
                                "-var", "slo=0.05", "-var", "cpus=2|3", "-var", "disk=vda",
                                "-queries", source], stdout=output, check=True)
    spec = json.loads(pathlib.Path("deploy/consoles/enrolld-queries.json").read_text())
    shutil.copyfile("deploy/consoles/enrolld-queries.json", out / "dashboard-queries.json")
    snapshot = {"start": start, "end": end, "step": 5, "queries": {},
                "alerts": json.loads((out / "alerts-firing.json").read_text()),
                "targets": json.loads((out / "targets-after.json").read_text())}
    for row in spec["rows"]:
        for card in row["cards"]:
            for query in card["queries"]:
                params = {"query": query["expr"], "start": start, "end": end, "step": 5}
                with urllib.request.urlopen(prom + "/api/v1/query_range?" + urllib.parse.urlencode(params), timeout=30) as response:
                    data = json.load(response)
                assert data["status"] == "success", data
                snapshot["queries"][query["id"]] = {"query": query["expr"], "response": data}
    (out / "dashboard-data.json").write_text(json.dumps(snapshot, ensure_ascii=False, indent=2) + "\n")
    # Escape HTML's script terminator if a future label contains markup.
    def inline(value):
        return json.dumps(value, ensure_ascii=False).replace("<", "\\u003c")
    html = pathlib.Path("deploy/consoles/enrolld.html").read_text()
    html = html.replace("const embeddedSpec = null;", "const embeddedSpec = " + inline(spec) + ";")
    html = html.replace("const embeddedSnapshot = null;", "const embeddedSnapshot = " + inline(snapshot) + ";")
    (out / "dashboard.html").write_text(html)
    print(f"Saved {len(snapshot['queries'])} query responses and offline dashboard to {out}")


if __name__ == "__main__":
    main()
