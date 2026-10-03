import glob
import json
import os

base = os.path.dirname(os.path.abspath(__file__))
n_att = 0
rids = set()
inv = 0
webs = 0
srcs = 0
runs = 0

log_path = os.path.join(base, "run-log.json")
log = json.load(open(log_path, encoding="utf-8"))
runs = len(log["runs"])

for p in sorted(glob.glob(os.path.join(base, "company-*.json"))):
    d = json.load(open(p, encoding="utf-8"))
    for qid, r in d["execution_receipts"].items():
        atts = r.get("attempts") or []
        n_att += len(atts)
        if r.get("response_id"):
            rids.add(r["response_id"])
        wsc = r.get("web_search_calls") or []
        webs += len(wsc) if isinstance(wsc, list) else wsc
        urls = r.get("source_urls") or []
        srcs += len(urls)

assert runs == 11, runs
assert n_att == 20, n_att
assert len(rids) == 20, len(rids)
assert webs == 41, webs
assert srcs == 388, srcs

log["budget"] = {
    "cli_invocations": runs,
    "primary_requests": n_att,
    "http_attempts": n_att,
    "distinct_response_ids": len(rids),
    "web_search_calls": webs,
    "source_urls": srcs,
    "caps": {
        "primary_request_cap": 20,
        "http_attempt_ceiling_including_retries": 40,
    },
    "cap_status": {
        "primary_requests": "at_cap_not_exceeded",
        "http_attempts": "within_ceiling",
    },
    "counting_note": (
        "primary_requests/http_attempts are the budget unit, counted from the "
        "20 execution_receipts attempt entries (20 distinct response_id, one "
        "per question-answer); cli_invocations (11) is a process-level count "
        "and must NOT be read as the request budget. Derived from "
        "company-*.json receipts; recomputable."
    ),
    "derived_from": "company-*.json execution_receipts[].attempts / source_urls / web_search_calls",
}

with open(log_path, "w", encoding="utf-8", newline="\n") as f:
    json.dump(log, f, ensure_ascii=False, indent=2)
    f.write("\n")

print("budget updated:", json.dumps(log["budget"], ensure_ascii=False)[:400])
