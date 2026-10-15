"""
Proof that the limiter is actually correct under concurrency.

    python loadtest/burst.py

This is the test that backs up the resume claim, and it is the one worth
demoing live, because a naive implementation FAILS it. It is not measuring
speed - it is measuring correctness.

WHAT IT DOES
------------
Creates a fresh API key on the Free plan (10 requests / 60s), then fires 50
requests ALL AT ONCE and counts the outcomes.

    correct   -> exactly 10 x 200, exactly 40 x 429
    broken    -> 11, 14, 23 x 200 ... some number above the limit

A read-then-increment implementation overshoots here, because dozens of
requests read the same count before any of them writes it back. The atomic
Lua script is what makes the answer exactly 10, every single run.

Then it runs a sustained throughput pass, so there are real numbers to put
on a resume instead of invented ones.
"""

from __future__ import annotations

import asyncio
import statistics
import sys
import time

import httpx

BASE = "http://localhost:8080"
BURST = 50
SUSTAINED_REQUESTS = 500
SUSTAINED_CONCURRENCY = 50


async def new_key(client: httpx.AsyncClient, plan: str) -> str:
    res = await client.post("/admin/keys", json={"plan": plan, "label": "loadtest"})
    res.raise_for_status()
    return res.json()["api_key"]


async def hit(client: httpx.AsyncClient, key: str) -> tuple[int, float]:
    started = time.perf_counter()
    res = await client.get("/api/ping", headers={"X-API-Key": key})
    return res.status_code, (time.perf_counter() - started) * 1000


async def burst_test(client: httpx.AsyncClient) -> bool:
    print(f"\n{'=' * 62}")
    print("  CONCURRENCY CORRECTNESS  --  Free plan, limit 10 per 60s")
    print(f"{'=' * 62}")

    key = await new_key(client, "free")
    print(f"  fresh key : {key[:16]}...")
    print(f"  firing    : {BURST} requests simultaneously\n")

    # asyncio.gather with no throttling: all 50 leave at essentially the
    # same instant, which is exactly the interleaving that breaks a naive
    # check-then-increment limiter.
    results = await asyncio.gather(*(hit(client, key) for _ in range(BURST)))

    allowed = sum(1 for status, _ in results if status == 200)
    throttled = sum(1 for status, _ in results if status == 429)
    other = BURST - allowed - throttled

    print(f"    200 OK               : {allowed}")
    print(f"    429 Too Many Requests: {throttled}")
    if other:
        print(f"    unexpected           : {other}")

    ok = allowed == 10 and throttled == 40
    print()
    if ok:
        print("    PASS  exactly 10 allowed. The atomic Lua script held under")
        print("          50-way concurrency - no overshoot.")
    else:
        print(f"    FAIL  expected exactly 10 allowed, got {allowed}.")
        print("          That is the check-then-increment race condition.")

    # Show the headers a real client would use to back off.
    res = await client.get("/api/ping", headers={"X-API-Key": key})
    print(f"\n  response headers on rejection (status {res.status_code}):")
    for header in (
        "Retry-After",
        "X-RateLimit-Limit",
        "X-RateLimit-Remaining",
        "X-RateLimit-Reset",
        "X-RateLimit-Policy",
    ):
        if header in res.headers:
            print(f"    {header:<24}: {res.headers[header]}")

    return ok


async def tier_test(client: httpx.AsyncClient) -> None:
    """Same burst, different plans. Proves limits are per-tenant, not global."""
    print(f"\n{'=' * 62}")
    print("  TENANT ISOLATION  --  same burst, different plans")
    print(f"{'=' * 62}\n")

    for plan, expected in (("free", 10), ("pro", 100)):
        key = await new_key(client, plan)
        results = await asyncio.gather(*(hit(client, key) for _ in range(120)))
        allowed = sum(1 for status, _ in results if status == 200)
        verdict = "ok" if allowed == expected else f"expected {expected}"
        print(f"    {plan:<11} 120 requests -> {allowed:>3} allowed   ({verdict})")

    print("\n    Two keys, two ceilings, one shared Redis. The limit follows")
    print("    the plan row in Postgres, not the IP address.")


async def upgrade_test(client: httpx.AsyncClient) -> None:
    """The capability nginx limit_req does not have: change a limit live."""
    print(f"\n{'=' * 62}")
    print("  LIVE PLAN CHANGE  --  no config edit, no reload")
    print(f"{'=' * 62}\n")

    key = await new_key(client, "free")
    results = await asyncio.gather(*(hit(client, key) for _ in range(30)))
    before = sum(1 for status, _ in results if status == 200)
    print(f"    on free       : {before} of 30 allowed")

    await client.patch(f"/admin/keys/{key}", json={"plan": "enterprise"})
    print("    PATCH /admin/keys/... -> enterprise")

    results = await asyncio.gather(*(hit(client, key) for _ in range(30)))
    after = sum(1 for status, _ in results if status == 200)
    print(f"    on enterprise : {after} of 30 allowed")
    print("\n    One UPDATE statement plus a cache invalidation. An nginx")
    print("    limit_req change would need a config edit and a reload.")


async def throughput_test(client: httpx.AsyncClient) -> None:
    """Numbers for the resume. Run it yourself; do not trust anyone else's."""
    print(f"\n{'=' * 62}")
    print("  THROUGHPUT  --  enterprise plan, limiter in the hot path")
    print(f"{'=' * 62}\n")

    key = await new_key(client, "enterprise")
    semaphore = asyncio.Semaphore(SUSTAINED_CONCURRENCY)

    async def bounded():
        async with semaphore:
            return await hit(client, key)

    started = time.perf_counter()
    results = await asyncio.gather(*(bounded() for _ in range(SUSTAINED_REQUESTS)))
    elapsed = time.perf_counter() - started

    latencies = sorted(ms for _, ms in results)
    allowed = sum(1 for status, _ in results if status == 200)

    def pct(p: float) -> float:
        return latencies[min(int(len(latencies) * p), len(latencies) - 1)]

    print(f"    requests      : {SUSTAINED_REQUESTS} at concurrency {SUSTAINED_CONCURRENCY}")
    print(f"    allowed       : {allowed}")
    print(f"    wall clock    : {elapsed:.2f}s")
    print(f"    throughput    : {SUSTAINED_REQUESTS / elapsed:.0f} req/s")
    print(f"    latency  p50  : {statistics.median(latencies):.1f} ms")
    print(f"             p95  : {pct(0.95):.1f} ms")
    print(f"             p99  : {pct(0.99):.1f} ms")
    print("\n    Every one of those requests paid for a plan lookup (cached)")
    print("    plus one atomic Redis round trip.")


async def main() -> int:
    async with httpx.AsyncClient(base_url=BASE, timeout=30.0) as client:
        try:
            await client.get("/healthz")
        except httpx.ConnectError:
            print(f"Cannot reach {BASE}. Is the stack up?")
            print("  docker compose up --build -d")
            return 1

        ok = await burst_test(client)
        await tier_test(client)
        await upgrade_test(client)
        await throughput_test(client)

        print(f"\n{'=' * 62}\n")
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
