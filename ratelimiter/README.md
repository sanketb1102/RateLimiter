# API Rate Limiter & Quota Management Service

Tenant-aware rate limiting. Quotas are enforced **per API key** against plan tiers stored in Postgres, using an **atomic Redis Lua script** so the decision stays correct across multiple worker processes.

```
client → nginx (plain reverse proxy) → FastAPI middleware ─┬─ over quota → 429 + Retry-After
                                                            └─ under quota → route handler
                                              │
                                    Redis (counters, Lua)
                                    Postgres (keys + plans)
```

## Run it

```bash
cp .env.example .env
docker compose up --build -d
docker compose logs api      # prints three demo API keys
```

Try it by hand — the Free key allows 10 requests per 60s:

```bash
KEY=<free key from the logs>
for i in $(seq 1 12); do
  curl -s -o /dev/null -w "%{http_code} " -H "X-API-Key: $KEY" http://localhost:8080/api/ping
done
# 200 200 200 200 200 200 200 200 200 200 429 429
```

## Prove it works

```bash
pip install httpx
python loadtest/burst.py
```

Verified output:

```
CONCURRENCY CORRECTNESS  --  Free plan, limit 10 per 60s
  firing: 50 requests simultaneously
    200 OK               : 10
    429 Too Many Requests: 40
    PASS  exactly 10 allowed

TENANT ISOLATION
    free   120 requests ->  10 allowed
    pro    120 requests -> 100 allowed

LIVE PLAN CHANGE
    on free       : 10 of 30 allowed
    on enterprise : 30 of 30 allowed
```

---

## The one problem this project exists to solve

A rate limiter looks trivial — read the count, compare, increment. Written that way it is **broken**, and only under real concurrency:

```
Request A: GET count -> 99.  99 < 100, allow.
Request B: GET count -> 99.  99 < 100, allow.     <-- both passed
Request A: SET count = 100
Request B: SET count = 100                        <-- 101 allowed
```

This is a **check-then-act race condition**. A Python lock does not fix it, because the service runs 4 worker processes (and would run N replicas in production) with no shared memory.

**The fix:** move read-decide-write into a single Lua script. Redis executes Lua single-threaded, so no other command from any client can interleave. One round trip, no locks, atomic across every process at once.

```lua
redis.call('ZREMRANGEBYSCORE', key, 0, now - window)   -- drop expired
local count = redis.call('ZCARD', key)
if count >= limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    return {0, count, window - (now - tonumber(oldest[2]))}
end
redis.call('ZADD', key, now, member)
redis.call('PEXPIRE', key, window)
return {1, count + 1, window}
```

`loadtest/burst.py` is the regression test for exactly this. A naive implementation returns 11, 14, 23 allowed. This returns 10, every run.

## The second race condition (a different fix)

The same class of bug shows up again at startup, and the fix is different because the shared state lives somewhere else.

All four uvicorn workers boot simultaneously and all four run `CREATE TABLE` against an empty database — three of them crash with a duplicate-key error. Redis Lua is no help here; the contended resource is Postgres.

**Fix:** a **Postgres advisory lock**. The first worker to ask gets it, the other three block, and by the time they proceed the tables exist so their `create_all()` does nothing.

> Two races, two tools: Lua for the one inside Redis, an advisory lock for the one inside Postgres. Same underlying problem — two processes checking-then-acting on shared state.

## Why sliding window, not fixed window

A fixed window keeps one counter per clock minute. With a limit of 100/min a client sends 100 requests at 11:59:59 and 100 more at 12:00:01 — **200 requests in two seconds**, both windows technically legal. That is the boundary burst problem.

This uses a **sliding window log**: a Redis sorted set holding one timestamp per request. Each check drops entries older than the window and counts what remains, so the window genuinely slides. Exact rather than approximate, at O(limit) memory per key. The oldest surviving timestamp is where an honest `Retry-After` comes from.

## Why not just nginx's `limit_req`?

`limit_req` keys on `$binary_remote_addr` — an IP address. It has no concept of an API key, no idea that customer X is on Pro and Y is on Free, and no way to find out, because that fact is a Postgres row that changes when someone upgrades. Changing a limit would mean editing `nginx.conf` and reloading.

So nginx stays a **plain reverse proxy**, and the quota decision happens one hop later where the plan data is reachable. `PATCH /admin/keys/{key}` changes a customer's tier with an UPDATE and a cache invalidation — verified live in the test above.

## Design patterns

| Pattern | Where |
|---|---|
| **Chain of Responsibility** | `main.py` section 6 — request passes through handlers; any link may short-circuit with a 429 without the rest knowing |
| **Strategy** | `main.py` section 3 — `SlidingWindow` / `TokenBucket` behind one `check()` |
| **Factory** | `main.py` section 4 — `get_limiter()` picks the strategy from the plan's `algorithm` column |
| **Cache-aside** | `main.py` section 5 — Redis first, Postgres on miss, write back with TTL |

## Two algorithms, chosen per tier

| Plan | Limit | Algorithm | Why |
|---|---|---|---|
| free | 10 / 60s | sliding window | hard ceiling |
| pro | 100 / 60s | sliding window | hard ceiling |
| enterprise | 1000 / 60s | token bucket | refills continuously, so a batch job firing 200 at once is tolerated as long as the long-run average holds |

## Production details that are easy to get wrong

- **Fail open, not closed.** If Redis is unreachable the request is allowed and logged loudly. Failing closed would mean a Redis hiccup becomes a total outage — the limiter, whose job is protecting availability, destroying it. Brief unmetered traffic beats downtime. A billing- or safety-critical limiter should choose the opposite and say so.
- **Standards-compliant rejection.** `429` (RFC 9110) with `Retry-After`, plus `X-RateLimit-Limit/Remaining/Reset/Policy`. Well-behaved clients read `Retry-After` and back off automatically; returning `400` or `503` breaks that.
- **Health checks are exempt.** Throttling `/healthz` makes the orchestrator conclude the service is dead and restart it — turning a busy moment into a restart loop.
- **Negative caching.** Unknown keys are cached as missing for 10s, so a flood of bogus keys cannot hammer Postgres on every request (cache penetration).
- **Sync SQLAlchemy runs in a threadpool** so it never blocks the event loop. Thanks to the cache this path runs roughly once per key per TTL.

## A note on the throughput numbers

`loadtest/burst.py` prints a throughput section. **Do not quote those numbers as the service's capacity.** Measured on Docker Desktop for Windows with the load generator on the host, throughput was ~150–215 req/s — and an endpoint with *the limiter disabled* measured the same. The ceiling is the Windows→Docker network path and the Python client, not the limiter.

What the measurement does support: **the limiter's overhead is within noise of an unlimited endpoint.** For real capacity numbers this needs to run on Linux with the generator off-box.

## Layout

```
ratelimiter/
├── docker-compose.yml
├── gateway/nginx.conf     plain reverse proxy (deliberately not limiting)
├── api/
│   ├── Dockerfile         uv for installs, 4 uvicorn workers
│   └── app/
│       ├── main.py        ← EVERYTHING about a request, top to bottom
│       └── database.py    the two tables + seed. read once, forget.
└── loadtest/burst.py      correctness proof under 50-way concurrency
```

`main.py` is deliberately one file, in the order a request actually travels:

| Section | Pattern |
|---|---|
| 1. setup | redis connection, the app |
| 2. lua scripts | the two algorithms, atomic inside Redis |
| 3. limiters | **Strategy** — both expose the same `check()` |
| 4. `get_limiter` | **Factory** — picks one from the plan row |
| 5. `find_plan` | **Cache-aside** — Redis first, Postgres on miss |
| 6. middleware | **Chain of Responsibility** — the quota decision |
| 7. routes | protected endpoints + admin |

Read those seven sections in order and you have read the project. ~320 lines
of code total, plus ~55 lines of Lua.

## API

| Method | Route | Notes |
|---|---|---|
| `GET` | `/api/ping` | rate limited, needs `X-API-Key` |
| `GET` | `/api/echo/{v}` | rate limited |
| `POST` | `/admin/keys` | `{plan, label}` → new key |
| `PATCH` | `/admin/keys/{key}` | `{plan}` → change tier, invalidates cache |
| `GET` | `/admin/usage` | live per-key window occupancy + throttle totals |
| `GET` | `/healthz` | exempt from limiting |

Stack: FastAPI · Redis (Lua) · PostgreSQL · SQLAlchemy · Nginx · Docker Compose · uv
