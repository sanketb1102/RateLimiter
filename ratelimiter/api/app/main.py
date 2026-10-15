"""
API Rate Limiter -- the whole request path, in one file, in order.

Read this top to bottom and you have read the project:

    1. setup        redis connection, the app
    2. lua scripts  the two algorithms, run atomically inside redis
    3. limiters     STRATEGY pattern -- both expose the same check()
    4. get_limiter  FACTORY pattern -- picks one based on the plan
    5. find_plan    CACHE-ASIDE pattern -- redis first, postgres on miss
    6. middleware   CHAIN OF RESPONSIBILITY -- the quota decision, can say no
    7. routes       protected endpoints, plus admin

What the service does: count how many requests an API key made recently,
and return 429 if that is more than its plan allows.
"""

import json
import logging
import math
import os
import time
import uuid

from fastapi import FastAPI, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import JSONResponse
from pydantic import BaseModel
from redis.asyncio import Redis
from redis.exceptions import RedisError

from . import database

logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
log = logging.getLogger("ratelimiter")


# ============================================================ 1. SETUP =====

redis = Redis.from_url(os.getenv("REDIS_URL", "redis://redis:6379/0"))
PLAN_CACHE_SECONDS = 60

app = FastAPI(title="API Rate Limiter & Quota Management Service")


@app.on_event("startup")
async def startup():
    demo_keys = database.setup()
    log.info("ready. demo API keys:")
    for plan, key in demo_keys.items():
        log.info("  %-11s %s", plan, key)


def now_ms():
    return int(time.time() * 1000)


# ======================================================= 2. LUA SCRIPTS =====
#
# THE ONE IDEA IN THIS PROJECT.
#
# The obvious way to write a rate limiter is: read the count, compare it to
# the limit, add one. That is broken:
#
#     request A reads count = 9   ->  9 < 10, allow
#     request B reads count = 9   ->  9 < 10, allow      <-- both got in
#     request A writes 10
#     request B writes 10                                <-- 11 allowed
#
# Both read before either wrote. This is a check-then-act race condition,
# and a Python lock cannot fix it because we run 4 worker processes with no
# shared memory.
#
# Fix: put read-decide-write in one Lua script. Redis runs Lua
# single-threaded, so nothing else can run in the middle of it. One round
# trip, no locks, correct across every process at once.

# --- sliding window: a hard ceiling ---------------------------------------
#
# Keeps one timestamp per request in a sorted set. Each check throws away
# timestamps older than the window and counts what is left, so the window
# really slides.
#
# Why not a fixed window (one counter per clock minute)? With a limit of
# 100/min a client sends 100 at 11:59:59 and 100 at 12:00:01 -- 200 requests
# in two seconds, both minutes "legal". That is the boundary burst problem.

SLIDING_WINDOW = """
local key    = KEYS[1]
local now    = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit  = tonumber(ARGV[3])
local id     = ARGV[4]

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)   -- drop what expired
local used = redis.call('ZCARD', key)

if used >= limit then
    -- The oldest request still in the window says when a slot frees up.
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local wait = window
    if oldest[2] then wait = window - (now - tonumber(oldest[2])) end
    if wait < 0 then wait = 0 end
    return {0, used, wait}
end

redis.call('ZADD', key, now, id)
redis.call('PEXPIRE', key, window)
return {1, used + 1, window}
"""

# --- token bucket: allows bursts ------------------------------------------
#
# A bucket of tokens that refills on its own. Each request spends one.
# Like prepaid phone credit that tops up automatically: stay quiet and your
# balance fills up, then you can spend it all at once.
#
# Enterprise gets this because a nightly batch job firing 500 requests at
# once is expected behaviour there, not abuse. A sliding window would reject
# it. Tokens are worked out from elapsed time, so no background timer.

TOKEN_BUCKET = """
local key      = KEYS[1]
local now      = tonumber(ARGV[1])
local capacity = tonumber(ARGV[2])
local refill   = tonumber(ARGV[3])   -- tokens per millisecond

local state  = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts     = tonumber(state[2])

if tokens == nil then          -- first time we have seen this key
    tokens = capacity
    ts = now
end

local elapsed = now - ts
if elapsed < 0 then elapsed = 0 end
tokens = math.min(capacity, tokens + elapsed * refill)

local allowed = 0
if tokens >= 1 then
    tokens = tokens - 1
    allowed = 1
end

redis.call('HSET', key, 'tokens', tokens, 'ts', now)
redis.call('PEXPIRE', key, math.ceil(capacity / refill) * 2)

local wait = 0
if allowed == 0 then wait = math.ceil((1 - tokens) / refill) end
return {allowed, math.floor(tokens), wait}
"""

sliding_window_script = redis.register_script(SLIDING_WINDOW)
token_bucket_script = redis.register_script(TOKEN_BUCKET)


# ========================================================== 3. LIMITERS =====
#
# STRATEGY PATTERN. Two different algorithms, one identical check() method,
# both returning the same dict. The middleware never knows which it got.

class SlidingWindow:
    def __init__(self, limit, window_seconds):
        self.limit = limit
        self.window_ms = window_seconds * 1000

    async def check(self, api_key):
        allowed, used, wait_ms = await sliding_window_script(
            keys=["rl:sw:" + api_key],
            args=[now_ms(), self.window_ms, self.limit, uuid.uuid4().hex],
        )
        return {
            "allowed": bool(allowed),
            "limit": self.limit,
            "remaining": max(0, self.limit - used),
            "wait_seconds": math.ceil(wait_ms / 1000),
        }


class TokenBucket:
    def __init__(self, limit, window_seconds):
        self.limit = limit
        # 1000 per 60s means capacity 1000, refilling at 1000/60000 per ms.
        self.refill_per_ms = limit / (window_seconds * 1000)

    async def check(self, api_key):
        allowed, tokens_left, wait_ms = await token_bucket_script(
            keys=["rl:tb:" + api_key],
            args=[now_ms(), self.limit, self.refill_per_ms],
        )
        return {
            "allowed": bool(allowed),
            "limit": self.limit,
            "remaining": tokens_left,
            "wait_seconds": math.ceil(wait_ms / 1000),
        }


# ======================================================== 4. THE FACTORY =====
#
# FACTORY PATTERN. The plan row says which algorithm to use, so adding an
# algorithm means adding one line here -- the middleware never changes.

def get_limiter(algorithm, limit, window_seconds):
    if algorithm == "token_bucket":
        return TokenBucket(limit, window_seconds)
    return SlidingWindow(limit, window_seconds)   # the safe default


# ==================================================== 5. PLAN LOOKUP =====
#
# CACHE-ASIDE PATTERN. Every request needs to know "what plan is this key
# on?". Asking Postgres every time would make the rate limiter the slowest
# thing in the stack, which is absurd given its job is protecting things.
#
#   look in redis -> hit?  use it, no database involved
#                 -> miss? read postgres, store in redis with a TTL, use it
#
# The 60s TTL bounds staleness: an upgrade takes effect within a minute
# without any invalidation machinery. The admin route below deletes the key
# explicitly so upgrades are instant.

async def find_plan(api_key):
    cache_key = "plan:" + api_key
    cached = await redis.get(cache_key)

    if cached == b"none":
        return None                      # known-bad key, cached
    if cached is not None:
        return json.loads(cached)

    # Miss. SQLAlchemy is synchronous, so it runs on a worker thread rather
    # than blocking the event loop. Thanks to the cache this is rare.
    plan = await run_in_threadpool(database.look_up_key, api_key)

    if plan is None:
        # Cache the miss too, briefly. Otherwise a flood of bogus keys hits
        # Postgres on every single request (cache penetration).
        await redis.set(cache_key, b"none", ex=10)
        return None

    await redis.set(cache_key, json.dumps(plan), ex=PLAN_CACHE_SECONDS)
    return plan


# ========================================================= 6. MIDDLEWARE =====
#
# CHAIN OF RESPONSIBILITY. Like airport security: ID check, bag scan, metal
# detector. Any station can stop you and none knows about the others.
#
# This runs BEFORE routing, so a rejected request never reaches a route
# handler, never opens a database session, never runs any business logic --
# it costs one Redis round trip and nothing else.

NO_LIMIT_PATHS = ("/healthz", "/docs", "/redoc", "/openapi.json", "/admin")


@app.middleware("http")
async def rate_limit(request: Request, call_next):
    path = request.url.path

    # Health checks must never be throttled: the orchestrator would decide
    # the service is dead and restart it, turning a busy moment into a
    # restart loop.
    if any(path.startswith(p) for p in NO_LIMIT_PATHS):
        return await call_next(request)

    api_key = request.headers.get("X-API-Key")
    if not api_key:
        return JSONResponse({"detail": "missing X-API-Key header"}, status_code=401)

    try:
        plan = await find_plan(api_key)
        if plan is None:
            return JSONResponse({"detail": "unknown API key"}, status_code=403)

        limiter = get_limiter(plan["algorithm"], plan["limit"], plan["window"])
        result = await limiter.check(api_key)

    except RedisError as error:
        # FAIL OPEN, not closed. If Redis is down we allow the request and
        # log loudly. Failing closed would mean a Redis hiccup becomes a
        # total outage -- the rate limiter, whose job is protecting
        # availability, being the thing that destroys it. Brief unmetered
        # traffic beats downtime. A billing-critical limiter should choose
        # the opposite, and say so.
        log.error("redis down, allowing request unmetered: %s", error)
        return await call_next(request)

    # These headers let a well-behaved client slow itself down instead of
    # hammering into a wall. 429 with Retry-After is the actual HTTP
    # standard; returning 400 or 503 here would break that.
    headers = {
        "X-RateLimit-Limit": str(result["limit"]),
        "X-RateLimit-Remaining": str(result["remaining"]),
        "X-RateLimit-Reset": str(result["wait_seconds"]),
        "X-RateLimit-Policy": plan["plan"] + ";" + plan["algorithm"],
    }

    if not result["allowed"]:
        await redis.hincrby("throttled", api_key, 1)   # for /admin/usage
        headers["Retry-After"] = str(max(1, result["wait_seconds"]))
        return JSONResponse(
            {
                "detail": "rate limit exceeded",
                "plan": plan["plan"],
                "limit": result["limit"],
                "retry_after": max(1, result["wait_seconds"]),
            },
            status_code=429,
            headers=headers,
        )

    response = await call_next(request)
    response.headers.update(headers)
    return response


# ============================================================= 7. ROUTES =====

@app.get("/healthz")
async def healthz():
    try:
        await redis.ping()
        return {"status": "ok", "redis": "up"}
    except RedisError:
        # Still 200: we fail open, so the service really is still serving.
        return {"status": "ok", "redis": "down (failing open)"}


# Anything not in NO_LIMIT_PATHS is rate limited. These exist so there is
# something to point traffic at.

@app.get("/api/ping")
async def ping():
    return {"pong": True}


@app.get("/api/echo/{value}")
async def echo(value: str):
    return {"echo": value}


# --- admin ----------------------------------------------------------------

class NewKey(BaseModel):
    plan: str = "free"
    label: str = ""


class NewPlan(BaseModel):
    plan: str


@app.post("/admin/keys", status_code=201)
async def create_key(body: NewKey):
    valid = [p[0] for p in database.PLANS]
    if body.plan not in valid:
        return JSONResponse({"detail": "unknown plan, use one of " + str(valid)}, 400)

    key = await run_in_threadpool(database.add_key, body.plan, body.label)
    return {"api_key": key, "plan": body.plan}


@app.patch("/admin/keys/{api_key}")
async def upgrade_key(api_key: str, body: NewPlan):
    """
    Change a customer's tier. This is the thing nginx's limit_req cannot do:
    one UPDATE and the new limit applies, no config edit and no reload.
    """
    ok = await run_in_threadpool(database.change_plan, api_key, body.plan)
    if not ok:
        return JSONResponse({"detail": "no such API key"}, 404)

    await redis.delete("plan:" + api_key)   # so the change is instant
    return {"api_key": api_key, "plan": body.plan}


@app.get("/admin/usage")
async def usage():
    """Live view: who is using their quota, and who is getting throttled."""
    active = []

    for key in await redis.keys("rl:sw:*"):
        active.append({
            "api_key": key.decode().replace("rl:sw:", ""),
            "algorithm": "sliding_window",
            "used_in_window": await redis.zcard(key),
        })

    for key in await redis.keys("rl:tb:*"):
        tokens = await redis.hget(key, "tokens")
        active.append({
            "api_key": key.decode().replace("rl:tb:", ""),
            "algorithm": "token_bucket",
            "tokens_left": round(float(tokens)) if tokens else None,
        })

    throttled = await redis.hgetall("throttled")
    return {
        "active": active,
        "throttled_totals": {k.decode(): int(v) for k, v in throttled.items()},
    }
