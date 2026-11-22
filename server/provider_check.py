"""Benchmark configured services on identical synthetic documents."""
import argparse
import asyncio
import json
from math import ceil
from pathlib import Path
from statistics import median

import httpx
from dotenv import load_dotenv

from .providers import CASES, ENDPOINTS, measure, run_demo


def stats(rows):
    ok = [r for r in rows if r["status"] == "ok"]
    report = {"attempts": len(rows), "completed": len(ok)}
    fallbacks = [r for r in rows if len(r.get("attempts", [])) > 1]
    if fallbacks:
        report["fallback_attempts"] = len(fallbacks)
        report["fallback_completions"] = sum(r["status"] == "ok" for r in fallbacks)
    if ok:
        for metric in ("first_text_s", "total_s"):
            values = sorted(r[metric] for r in ok)
            report[metric] = {"p50": median(values), "p95": values[ceil(.95 * len(values)) - 1]}
        report["value_unit_accuracy"] = sum(r["value_unit_correct"] for r in ok) / len(ok)
        report["citation_accuracy"] = sum(r["citation_correct"] for r in ok) / len(ok)
        report["value_accuracy"] = sum(r["value_correct"] for r in ok)/len(rows)
        report["unit_accuracy"] = sum(r["unit_correct"] for r in ok)/len(rows)
        verified = sum(r["value_unit_correct"] and r["citation_correct"] for r in ok)
        report["verified"] = verified
        costs = [r.get("cost_usd", r.get("estimated_cost_usd")) for r in rows]
        report["usd_per_completion"] = sum(costs)/len(ok) if all(isinstance(c,(int,float)) for c in costs) else None
        report["usd_per_verified_answer"] = sum(costs)/verified if verified and all(isinstance(c,(int,float)) for c in costs) else None
    return report


async def benchmark(choice, repeats):
    names = list(ENDPOINTS) if choice == "all" else [choice]
    rows = {name: [] for name in names}
    async with httpx.AsyncClient(timeout=12) as client:
        for _ in range(repeats):
            for case in CASES:
                for name in names:
                    if name == "auto":
                        demo = await run_demo(client, name, case)
                        attempts = demo["attempts"]
                        result = dict(attempts[-1])
                        prior = sum(a.get("total_s", 0) for a in attempts[:-1])
                        if result["first_text_s"] is not None:
                            result["first_text_s"] += prior
                        result["total_s"] = prior + result.get("total_s", 0)
                        result["attempts"] = attempts
                        if len(attempts) > 1:
                            result.pop("cost_usd", None)
                            result.pop("estimated_cost_usd", None)
                    else:
                        result = await measure(client, name, case)
                    rows[name].append(result)
                    print(json.dumps({"case": case, **result}), flush=True)
    for name, records in rows.items():
        print(json.dumps({"provider": name, **stats(records)}))


if __name__ == "__main__":
    load_dotenv(Path(__file__).with_name(".env"))
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("provider", choices=[*ENDPOINTS, "all", "auto"])
    parser.add_argument("--repeats", type=int, default=1)
    args = parser.parse_args()
    if args.repeats < 1:
        parser.error("--repeats must be positive")
    asyncio.run(benchmark(args.provider, args.repeats))
