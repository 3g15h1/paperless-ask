#!/usr/bin/env python3
"""Ask a running Paperless Ask a few questions end-to-end and print answers + cited documents.

Usage: python3 scripts/smoke_test.py [--url http://host:8000] [--user ask --password ...]
                                     [--no-llm] [--debug] ["question" ...]
Needs only the Python standard library.
"""
import argparse
import base64
import json
import time
import urllib.request

DEFAULT = [
    "When does my car insurance expire?",
    "How much was my last electric bill?",
    "Show me my receipts from this year",
]

p = argparse.ArgumentParser()
p.add_argument("questions", nargs="*")
p.add_argument("--url", default="http://localhost:8000")
p.add_argument("--user", default="ask")
p.add_argument("--password")
p.add_argument("--no-llm", action="store_true")
p.add_argument("--debug", action="store_true", help="also print the prompt sent to the model")
a = p.parse_args()
base = a.url.rstrip("/")
headers = {"Content-Type": "application/json"}
if a.password:
    headers["Authorization"] = "Basic " + base64.b64encode(f"{a.user}:{a.password}".encode()).decode()

print("healthz:", urllib.request.urlopen(base + "/healthz", timeout=10).read().decode())
for q in a.questions or DEFAULT:
    t = time.time()
    body = json.dumps({"q": q, "no_llm": a.no_llm, "debug": a.debug}).encode()
    r = json.load(urllib.request.urlopen(urllib.request.Request(base + "/api/ask", body, headers), timeout=300))
    print(f"\nQ: {q}\n   mode={r['mode']} retrieval={r['ms']}ms total={r['total_ms']}ms ({time.time() - t:.1f}s wall)"
          f" missing={r.get('missing')} unverified={r.get('unverified')}")
    if r.get("prompt"):
        print("   PROMPT:\n" + r["prompt"])
    print("   A:", r["answer"].replace("\n", " "))
    for s in r["sources"]:
        print(f"   [{s['n']}] #{s['id']} {s['title'][:60]!r} {s['created']} score={s['score']} {s['url']}")
