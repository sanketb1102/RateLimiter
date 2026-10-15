# RUNBOOK — API Rate Limiter

> How to start it, test it, and stop it. Works on any machine with Docker.

## FASTEST WAY TO SEE IT WORK

1. Install **Docker Desktop** and wait until it says *"Engine running"*.
2. **Double-click `run.bat`** in this folder.

That is it. It starts everything, waits for the service, fetches an API key
and runs the test for you. You should see `200` ten times, then `429`.

Everything below explains what it did and how to do it by hand.

**To understand what the project actually does and why it is built this
way, read `ABOUT_PROJECT.pdf` in this folder.**

---

## THE TOOLS — what each thing is

| Tool | What it is | Do you need to install it? |
|---|---|---|
| **Docker Desktop** | Runs applications in isolated boxes called containers. | **YES — the only thing you must install.** |
| **Docker Compose** | Starts several containers together from one file. Ships inside Docker Desktop. | No, comes with Docker |
| **curl** | Sends a web request from the command line and prints the reply. A browser with no browser. Needed because a browser cannot send the `X-API-Key` header. | No, ships with Windows 10/11 |
| **httpx** | A Python library that sends many requests **at the same time**. Only used by `loadtest/burst.py`. curl sends one at a time, so it cannot test a race condition. | Optional — `pip install httpx` |

**Everything else — Python, FastAPI, PostgreSQL, Redis, nginx — is already
inside the Docker images.** You do not install any of it.

### One Windows gotcha

In PowerShell, `curl` is **not** curl — it is an alias for a different command
called `Invoke-WebRequest`, which does not understand curl's flags.

> **Always type `curl.exe`, never bare `curl`.**

---

## THE 30-SECOND VERSION

```powershell
# 1. Open Docker Desktop. Wait for "Engine running".
# 2. Open a terminal in the project folder, then:

docker compose up --build -d
docker compose logs api | Select-String "free"

# copy the rk_... key from the output, then:
$key = "rk_PASTE_THE_KEY_HERE"
1..12 | ForEach-Object { curl.exe -s -o NUL -w "%{http_code} " -H "X-API-Key: $key" http://localhost:8080/api/ping }

# expect:  200 200 200 200 200 200 200 200 200 200 429 429

docker compose down
```

**Ten allowed, then it starts refusing.** That is the whole project.

---

## WHAT IS RUNNING — 4 containers

| Container | Job | Reachable from your machine? |
|---|---|---|
| `postgres` | API keys and plan tiers | No |
| `redis` | the request counters | No |
| `api` | the FastAPI app (4 worker processes) | No |
| `gateway` | nginx reverse proxy | **YES — port 8080** |

Only the gateway has a published port. Everything else is sealed inside
Docker's private network, so the database is not exposed at all.

**Every command you type aims at `http://localhost:8080`.**

---

## THE STEPS

### 1. Start Docker Desktop

Open the app and wait until it says **"Engine running"** (30–90 seconds cold).

Check: `docker ps` → a table header means it is ready.

### 2. Open a terminal in the project folder

Easiest way: open the `ratelimiter` folder in File Explorer, then **right-click
→ "Open in Terminal"**.

Or `cd` to wherever you saved it:
```powershell
cd path\to\ratelimiter
```

**Check you are in the right place:**
```powershell
ls
```
You should see `docker-compose.yml`. If not, you are in the wrong folder — this
is the most common reason commands fail.

### 3. Start everything

```powershell
docker compose up --build -d
```

| Flag | Meaning |
|---|---|
| `up` | create and start the containers |
| `--build` | build the app image first (only needed the first time, or after a code change) |
| `-d` | detached — run in the background and give the prompt back |

**First run takes 2–4 minutes** while it downloads Postgres, Redis and nginx.
After that it is 5–10 seconds.

### 4. Check it is working

```powershell
docker compose ps
```
Expect **4 rows**, all `Up`.

```powershell
curl.exe http://localhost:8080/healthz
```
Expect:
```json
{"status":"ok","redis":"up"}
```

**If you see that, the system works.** Everything below is just demonstrating it.

### 5. Get an API key

```powershell
docker compose logs api | Select-String "free|pro|enterprise"
```

Three demo keys are created at startup:

| Plan | Limit | Algorithm |
|---|---|---|
| free | 10 per 60s | sliding window |
| pro | 100 per 60s | sliding window |
| enterprise | 1000 per 60s | token bucket |

---

## THE DEMOS

### Demo 1 — the limit kicks in

```powershell
$key = "PASTE_A_FREE_KEY"
1..12 | ForEach-Object { curl.exe -s -o NUL -w "%{http_code} " -H "X-API-Key: $key" http://localhost:8080/api/ping }
```
```
200 200 200 200 200 200 200 200 200 200 429 429
```

Ten allowed, then **429 Too Many Requests**.

> **Run it twice and you get all 429s.** Not a bug — you already spent that
> key's 10 requests for the current 60-second window. Wait a minute, or use a
> different key.

### Demo 2 — the limit follows the PLAN, not the IP

```powershell
$pro = "PASTE_A_PRO_KEY"
1..12 | ForEach-Object { curl.exe -s -o NUL -w "%{http_code} " -H "X-API-Key: $pro" http://localhost:8080/api/ping }
```

All **200s** — same code, same burst, higher ceiling, because the limit is a
database row attached to the key.

### Demo 3 — the one that matters (needs `pip install httpx`)

```powershell
python loadtest\burst.py
```

Fires **50 requests simultaneously** at a key with a limit of 10.

```
200 OK               : 10
429 Too Many Requests: 40
PASS  exactly 10 allowed
```

**This is the point of the project.** A naive implementation fails here — it
returns 14, or 23, a different wrong number every run, because fifty requests
all read the same counter before any of them writes it. This returns exactly
10 every time, across 4 separate worker processes.

**Read the first three blocks of output. Ignore the THROUGHPUT block** — it
measures Docker's networking, not the limiter.

### Demo 4 — things you can open in a browser

| URL | Shows |
|---|---|
| `http://localhost:8080/healthz` | service is alive |
| `http://localhost:8080/admin/usage` | live usage per key + throttle totals |
| `http://localhost:8080/docs` | auto-generated API documentation |

Open `/admin/usage` straight after Demo 1.

> **Two things about `/admin/usage`:**
> `active` is a **live snapshot** — counters delete themselves 60s after the
> last request, so it goes empty when idle. That is correct.
> `throttled_totals` is **cumulative forever** — it only changes when new
> requests are actually rejected.

---

## STOP IT

```powershell
docker compose down        # containers removed, YOUR DATA SURVIVES
docker compose down -v     # also deletes the database (API keys gone)
```

Closing Docker Desktop also stops everything. Nothing is lost.

---

## TROUBLESHOOTING

| Symptom | Cause | Fix |
|---|---|---|
| `error during connect ... dockerDesktopLinuxEngine` | Docker Desktop is not running | Open it, wait for "Engine running" |
| `no configuration file provided` | wrong folder | `cd` into the `ratelimiter` folder (the one with `docker-compose.yml`) |
| `Conflict. The container name is already in use` | leftovers from a previous run | `docker compose down --remove-orphans` then up again |
| `502 Bad Gateway` from the gateway | nginx is up but the api container crashed | `docker compose logs api` and read the error |
| All `429` immediately | key already spent this minute | wait 60s, or make a fresh key |
| `Invoke-WebRequest: parameter cannot be found` | you typed `curl` not `curl.exe` | add the `.exe` |
| `Security Warning: Script Execution Risk` | PowerShell 5.1 using the IE parser | add `-UseBasicParsing` to `Invoke-WebRequest` |
| Port 8080 already in use | something else is on that port | `docker compose down`, or change the port in `docker-compose.yml` |

**When in doubt, read the logs:**
```powershell
docker compose logs api          # the app's output
docker compose logs gateway      # nginx
docker compose logs              # everything
docker compose logs -f api       # follow live (Ctrl+C to stop watching)
```

---

