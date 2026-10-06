"""B01-b step 3/4: method matrix (packaging comparison), phase-1 model MiniMax-M3.

Frozen design (B01b-freeze-manifest):
- questions: b01b_questions_v1 (30 unique ids, shared across companies/methods)
- evidence: per-company merged Brave+Tavily pool (<=30000 chars) as untrusted
  context with stable source_ids; models must cite source_ids per claim
- methods: sequential(1), concurrent(<=4 in flight via threads), group-3,
  group-5, group-10, batch-30 -> per company 30+30+10+6+3+1 = 80 requests
- method execution order per company randomized with seed 20261007
- generation frozen: temperature=0, one attempt + one format-repair (L02
  dispatch policy); failures REPORTED (LIVE-04), never silent re-dispatch,
  no model switch
- budget: request cap 800 (phase-1); usage recorded per request

Modes:
  --plan  offline (zero API calls); default = live run per plan.
Keys: MINIMAX_API_KEY injected by caller from user-level env; never logged.
"""

from __future__ import annotations

import argparse
import collections
import hashlib
import json
import os
import random
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(r"C:\Users\郑曾波\Projects\StockQAbyLLM")
FREEZE = ROOT / "pilot_runs/b01b_freeze_2026-10-07"
RETRIEVER = ROOT / "pilot_runs/b01b_retriever_2026-10-07"
SNAPSHOTS = ROOT / "pilot_runs/b01_prereq_2026-10-07"
OUT = ROOT / "pilot_runs/b01b_method_2026-10-07"

SEED = 20261007
REQUEST_CAP = 800
REPAIR_BUDGET = 1

METHODS = [
    {"method": "sequential_1", "group": 1, "concurrency": 1},
    {"method": "concurrent_4", "group": 1, "concurrency": 4},
    {"method": "group_3", "group": 3, "concurrency": 1},
    {"method": "group_5", "group": 5, "concurrency": 1},
    {"method": "group_10", "group": 10, "concurrency": 1},
    {"method": "batch_30", "group": 30, "concurrency": 1},
]

COMPANIES = [
    {
        "key": "CN-A:300750",
        "name": "宁德时代",
        "snapshot": "catl_snapshot.json",
        "entity_id": "ENT_99ebb735-f072-41e4-8545-823bc012614f",
    },
    {
        "key": "HK:06066",
        "name": "中信建投证券",
        "snapshot": "cncb_h_snapshot.json",
        "entity_id": "ENT_1af6804e-40c1-4c35-b190-ea691dba2c85",
    },
    {
        "key": "US:NASDAQ:GOOGL",
        "name": "Alphabet Inc.",
        "snapshot": "alphabet_snapshot.json",
        "entity_id": "ENT_97bf6a65-a9e6-43f0-8409-c5695e2f6e1e",
    },
]

POOL_FILES = {
    "CN-A:300750": ("pool_CN-A__300750__brave.json", "pool_CN-A__300750__tavily.json"),
    "HK:06066": ("pool_HK__06066__brave.json", "pool_HK__06066__tavily.json"),
    "US:NASDAQ:GOOGL": (
        "pool_US__NASDAQ_GOOGL-b01__brave.json",
        "pool_US__NASDAQ_GOOGL-b01__tavily.json",
    ),
}

MAX_TOKENS_FOR = {1: 2000, 3: 4000, 5: 6000, 10: 10000, 30: 16000}


def sha(obj) -> str:
    return hashlib.sha256(
        json.dumps(obj, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()


def load_questions():
    frozen = json.loads((FREEZE / "question_freeze.json").read_text(encoding="utf-8"))
    cfg = json.loads((FREEZE / "questions_b01b_v1.json").read_text(encoding="utf-8"))
    qs = cfg["categories"][0]["questions"]
    assert len(qs) == 30
    ids = [q["question_id"] for q in qs]
    assert ids == frozen["question_ids"], "frozen id set drift"
    return qs


def render_question_prompts(qs):
    import importlib.util

    spec = importlib.util.spec_from_file_location(
        "l02b",
        r"C:\Users\郑曾波\Projects\invest-quick-scan\docs\implementation\reviews"
        r"\IQS-lane\L02-freeze-build.py",
    )
    l02 = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(l02)
    tc = l02.load_iqs_test_case()
    manifest = tc().make_manifest(profile=dict(tc.profile))
    by_id = {q["id"]: q for q in manifest["questions"]}
    want = {q["question_id"] for q in qs}
    assert want <= set(by_id), want - set(by_id)
    out = []
    for q in qs:
        m = by_id[q["question_id"]]
        assert m["question"] == q["text"], f"text drift {q['question_id']}"
        out.append(
            {
                "question_id": q["question_id"],
                "text": q["text"],
                "prompt": m.get("prompt") or q["text"],
                "anchors": m.get("anchors"),
                "rubric_version": m.get("rubric_version"),
            }
        )
    return out


def load_identity_block(comp) -> str:
    sys.path.insert(0, str(ROOT))
    from src.runners.llm_runner import load_identity_snapshot

    payload = load_identity_snapshot(
        str(SNAPSHOTS / comp["snapshot"]), expected_entity_id=comp["entity_id"]
    )
    p = payload.get("payload") or payload
    sec = (p.get("listings") or [{}])[0]
    return (
        f"标的公司：{p.get('canonical_name')}｜entity_id={p['entity_id']}｜"
        f"市场={sec.get('market')} 代码={sec.get('ticker')}｜身份态={p['identity_state']} "
        f"rev={p['identity_revision']}（信息截止 as-of 2026-10-07T00:00:00Z）"
    )


def load_evidence_block(comp_key: str) -> str:
    merged, seen, chars = [], set(), 0
    for fname in POOL_FILES[comp_key]:
        pool = json.loads((RETRIEVER / fname).read_text(encoding="utf-8"))
        for it in pool["items"]:
            if it["url"] in seen:
                continue
            if chars + len(it["snippet"]) > 30000:
                break
            seen.add(it["url"])
            line = (
                f"[{it['source_id']}] {it['title']} — {it['url']} "
                f"(retrieved {it['retrieved_at']})\n  {it['snippet']}"
            )
            merged.append(line)
            chars += len(line) + 2
    return "\n\n".join(merged)


def build_system_prompt() -> str:
    return (
        "你是投资研究评分助手。仅基于给定的【不可信检索证据】回答，所有事实性断言必须引用方括号内的 source_id。"
        "证据不足时 status 必须为 unknown 或 not_applicable，不得猜测。"
        "对每个题目输出一行 JSON（键：question_id,status,score,rationale,evidence_refs），"
        "status ∈ scored/unknown/not_applicable；score 为 1-10 整数（仅 status=scored 时给出）。"
        "输出仅 JSON 数组，无任何其他文字。"
    )


def build_messages(identity_block, question_rows, evidence_block, system_prompt):
    qparts = [f"### {q['question_id']}\n{q['prompt']}" for q in question_rows]
    user = (
        f"【标的】\n{identity_block}\n\n"
        f"【不可信检索证据】\n{evidence_block}\n\n"
        f"【题目】\n" + "\n\n".join(qparts)
    )
    return [{"role": "system", "content": system_prompt}, {"role": "user", "content": user}]


def make_plan():
    qs = load_questions()
    rendered = render_question_prompts(qs)
    rng = random.Random(SEED)
    plan = {
        "schema": "b01b_method_plan/1",
        "seed": SEED,
        "request_cap": REQUEST_CAP,
        "repair_budget": REPAIR_BUDGET,
        "generation": {
            "temperature": 0,
            "model": "MiniMax-M3",
            "provider": "minimax",
            "attempt": "1+1 format-repair",
        },
        "companies": [],
        "request_total": 0,
    }
    for comp in COMPANIES:
        comp = dict(comp)
        comp["identity_block"] = load_identity_block(comp)
        comp["evidence_block"] = load_evidence_block(comp["key"])
        comp["evidence_sha256"] = sha(comp["evidence_block"])
        order = list(METHODS)
        rng.shuffle(order)
        methods_plan, total = [], 0
        for m in order:
            group = m["method"]
            g = dict(m)
            if group == "sequential_1" or group == "concurrent_4":
                n, sizes = 30, [1] * 30
            else:
                gsz = {"group_3": 3, "group_5": 5, "group_10": 10, "batch_30": 30}[group]
                sizes = [gsz] * (30 // gsz)
                n = len(sizes)
            g["requests"], g["chunk_sizes"] = n, sizes
            total += n
            methods_plan.append(g)
        comp["method_order"] = [m["method"] for m in methods_plan]
        comp["methods"] = methods_plan
        comp["requests"] = total
        plan["companies"].append(comp)
        plan["request_total"] += total
    assert plan["request_total"] <= REQUEST_CAP, plan["request_total"]
    plan["question_render_sha256"] = sha(rendered)
    plan["prompts_sha256"] = {
        c["key"]: sha(
            [
                build_messages(
                    c["identity_block"], rendered, c["evidence_block"], build_system_prompt()
                )
            ]
        )
        for c in plan["companies"]
    }
    return plan, rendered


def call_minimax(messages, *, max_tokens: int):
    key = os.environ.get("MINIMAX_API_KEY", "")
    if not key:
        raise RuntimeError("MINIMAX_API_KEY missing (user-level env not injected)")
    body = json.dumps(
        {"model": "MiniMax-M3", "messages": messages, "temperature": 0, "max_tokens": max_tokens}
    ).encode("utf-8")
    req = urllib.request.Request(
        "https://api.minimaxi.com/v1/chat/completions",
        data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
        method="POST",
    )
    t0 = time.time()
    with urllib.request.urlopen(req, timeout=180) as resp:
        raw = resp.read().decode("utf-8")
    d = json.loads(raw)
    return {
        "content": d["choices"][0]["message"]["content"],
        "usage": d.get("usage") or {},
        "latency_s": round(time.time() - t0, 2),
        "response_id": d.get("id"),
    }


def parse_answers(content):
    text = content.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("[")
    end = text.rfind("]")
    if start < 0 or end < start:
        raise ValueError("no json array in response")
    arr = json.loads(text[start : end + 1])
    out = {}
    for a in arr:
        if isinstance(a, dict) and a.get("question_id"):
            out[a["question_id"]] = a
    return out


class Ledger:
    def __init__(self):
        self.lock = threading.Lock()
        self.entries = []
        self.results = []
        self.failures = []
        self.requests_used = 0

    def add_request(self):
        with self.lock:
            self.requests_used += 1
            return self.requests_used

    def record(self, entry, rows, failure=None):
        with self.lock:
            self.entries.append(entry)
            self.results.extend(rows)
            if failure:
                self.failures.append(failure)


def _rows_for(comp_key, method, chunk, status, score, rationale, refs=()):
    return [
        {
            "company": comp_key,
            "method": method,
            "question_id": q["question_id"],
            "status": status,
            "score": score,
            "rationale": rationale,
            "evidence_refs": list(refs),
        }
        for q in chunk
    ]


def send_chunk(ledger, comp, method_name, idx, chunk, identity, evidence, sysp, group_size):
    messages = build_messages(identity, chunk, evidence, sysp)
    entry = {
        "company": comp,
        "method": method_name,
        "chunk_index": idx,
        "question_ids": [q["question_id"] for q in chunk],
        "attempts": [],
    }
    try:
        resp = call_minimax(messages, max_tokens=MAX_TOKENS_FOR[group_size])
        ledger.add_request()
        entry.update(
            usage=resp["usage"], latency_s=resp["latency_s"], response_id=resp["response_id"]
        )
        err = None
        parsed = None
        try:
            parsed = parse_answers(resp["content"])
        except Exception as exc:
            err = f"parse:{type(exc).__name__}:{exc}"
            repair_msgs = messages + [
                {"role": "assistant", "content": resp["content"]},
                {
                    "role": "user",
                    "content": "你的输出无法解析为 JSON 数组。仅输出修复后的 JSON 数组，"
                    "保持原答案内容与逐题行。",
                },
            ]
            resp2 = call_minimax(repair_msgs, max_tokens=MAX_TOKENS_FOR[group_size])
            ledger.add_request()
            entry["attempts"].append(
                {"kind": "format_repair", "usage": resp2["usage"], "latency_s": resp2["latency_s"]}
            )
            try:
                parsed = parse_answers(resp2["content"])
                err = None
            except Exception as exc2:
                err = f"repair_parse:{type(exc2).__name__}:{exc2}"
        entry["error"] = err
        if err:
            ledger.record(
                entry,
                _rows_for(comp, method_name, chunk, "error", None, err[:200]),
                failure={**entry, "stage": "parse"},
            )
            return
        rows = []
        for q in chunk:
            a = parsed.get(q["question_id"])
            if a is None:
                rows.append(
                    {
                        "company": comp,
                        "method": method_name,
                        "question_id": q["question_id"],
                        "status": "missing_in_response",
                        "score": None,
                        "rationale": "question_id absent from response",
                        "evidence_refs": [],
                    }
                )
            else:
                rows.append(
                    {
                        "company": comp,
                        "method": method_name,
                        "question_id": q["question_id"],
                        "status": a.get("status"),
                        "score": a.get("score"),
                        "rationale": (a.get("rationale") or "")[:500],
                        "evidence_refs": a.get("evidence_refs") or [],
                    }
                )
        ledger.record(entry, rows)
    except urllib.error.HTTPError as exc:
        ledger.add_request()
        entry["error"] = f"HTTP {exc.code}"
        ledger.record(
            entry,
            _rows_for(comp, method_name, chunk, "error", None, f"HTTP {exc.code}"),
            failure={**entry, "stage": "http"},
        )
    except Exception as exc:
        ledger.add_request()
        entry["error"] = f"{type(exc).__name__}:{str(exc)[:160]}"
        ledger.record(
            entry,
            _rows_for(comp, method_name, chunk, "error", None, f"{type(exc).__name__}"),
            failure={**entry, "stage": "transport"},
        )


def run_live(plan, rendered):
    OUT.mkdir(parents=True, exist_ok=True)
    ledger = Ledger()
    sysp = build_system_prompt()
    started = datetime.now(timezone.utc).isoformat(timespec="seconds")
    for comp in plan["companies"]:
        identity = comp["identity_block"]
        evidence = comp["evidence_block"]
        for m in comp["methods"]:
            group = m["method"]
            g = m["group"]
            chunks = [rendered[i : i + g] for i in range(0, 30, g)]
            assert len(chunks) == m["requests"], (group, len(chunks), m["requests"])
            if m["concurrency"] == 1:
                for idx, chunk in enumerate(chunks):
                    if ledger.requests_used >= REQUEST_CAP:
                        ledger.failures.append(
                            {"stage": "budget_stop", "method": group, "company": comp["key"]}
                        )
                        ledger.results.extend(
                            _rows_for(
                                comp["key"], group, chunk, "error", None, "budget cap reached"
                            )
                        )
                        continue
                    send_chunk(ledger, comp["key"], group, idx, chunk, identity, evidence, sysp, g)
            else:
                with ThreadPoolExecutor(max_workers=m["concurrency"]) as pool:
                    futs = [
                        pool.submit(
                            send_chunk,
                            ledger,
                            comp["key"],
                            group,
                            idx,
                            chunk,
                            identity,
                            evidence,
                            sysp,
                            g,
                        )
                        for idx, chunk in enumerate(chunks)
                    ]
                    for f in futs:
                        f.result()
            print(f"  {comp['key']} {group}: requests={ledger.requests_used}", flush=True)
    tot_in = tot_out = 0
    for e in ledger.entries:
        u = e.get("usage") or {}
        tot_in += u.get("prompt_tokens") or u.get("input_tokens") or 0
        tot_out += u.get("completion_tokens") or u.get("output_tokens") or 0
    receipt = {
        "schema": "b01b_method_run/1",
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "plan": {
            k: plan[k]
            for k in (
                "seed",
                "request_cap",
                "generation",
                "prompts_sha256",
                "question_render_sha256",
            )
        },
        "requests_used": ledger.requests_used,
        "failures": ledger.failures,
        "results_count": len(ledger.results),
        "usage_total": {"prompt_tokens": tot_in, "completion_tokens": tot_out},
    }
    (OUT / "method_ledger.json").write_text(
        json.dumps(ledger.entries, ensure_ascii=False, indent=1, default=str) + "\n",
        encoding="utf-8",
    )
    (OUT / "method_results.json").write_text(
        json.dumps(ledger.results, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    (OUT / "method_run_receipt.json").write_text(
        json.dumps(receipt, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "requests_used": ledger.requests_used,
                "failures": len(ledger.failures),
                "results": len(ledger.results),
                "usage": receipt["usage_total"],
            },
            ensure_ascii=False,
        )
    )
    return 0


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--plan", action="store_true")
    args = ap.parse_args()
    OUT.mkdir(parents=True, exist_ok=True)
    plan, rendered = make_plan()
    (OUT / "method_plan.json").write_text(
        json.dumps(plan, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    (OUT / "questions_rendered.json").write_text(
        json.dumps(rendered, ensure_ascii=False, indent=1) + "\n", encoding="utf-8"
    )
    print(
        json.dumps(
            {
                "request_total": plan["request_total"],
                "orders": {c["key"]: c["method_order"] for c in plan["companies"]},
                "prompts_sha256": plan["prompts_sha256"],
            },
            ensure_ascii=False,
            indent=1,
        )
    )
    if args.plan:
        return 0
    return run_live(plan, rendered)


if __name__ == "__main__":
    sys.exit(main())
