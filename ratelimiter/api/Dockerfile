FROM python:3.12-slim

# uv instead of pip. Same requirements.txt, same packages, but installs are
# 10-100x faster -- this build drops from ~40s to a couple of seconds.
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv

WORKDIR /srv

# Requirements first, then source. Docker caches each layer, so editing a
# .py file reuses the cached install instead of redoing it.
COPY requirements.txt .
RUN uv pip install --system --no-cache -r requirements.txt

COPY app ./app

# Run as a normal user, not root.
RUN useradd --create-home --shell /bin/false appuser
USER appuser

EXPOSE 8000

# Four worker processes, and this is not just a speed knob -- it is what
# makes the correctness claim real.
#
# Four workers are four separate OS processes with no shared memory. A
# Python lock or an in-process counter would be useless across them. The
# only reason the quota comes out exact is that the decision is a single
# atomic Lua script inside Redis, which all four share. So loadtest/burst.py
# is testing genuine multi-process concurrency, the same problem you get
# from horizontally scaled replicas.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000", "--workers", "4"]
