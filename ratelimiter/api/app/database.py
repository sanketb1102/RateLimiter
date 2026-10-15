"""
Postgres. Two tables, and you only ever need to read this file once.

    plans      free / pro / enterprise -- the limit, the window, the algorithm
    api_keys   a key, and which plan it belongs to

The point: a customer's limit is a DATABASE ROW, not a config file. Moving
someone from free to pro is one UPDATE. That is the thing nginx cannot do,
because its limits are baked into nginx.conf and need a reload to change.
"""

import os
import secrets

from sqlalchemy import Boolean, Column, Integer, String, create_engine, text
from sqlalchemy.orm import declarative_base, sessionmaker

DATABASE_URL = os.getenv(
    "DATABASE_URL",
    "postgresql+psycopg://ratelimiter:dev_only_change_me@postgres:5432/ratelimiter",
)

# pool_pre_ping stops us handing out a connection Postgres already closed.
engine = create_engine(DATABASE_URL, pool_pre_ping=True)
Session = sessionmaker(bind=engine)
Base = declarative_base()


class Plan(Base):
    __tablename__ = "plans"

    name = Column(String(32), primary_key=True)
    max_requests = Column(Integer, nullable=False)     # requests allowed
    window_seconds = Column(Integer, nullable=False)   # ...per this many seconds
    algorithm = Column(String(32), nullable=False)   # which strategy to use


class ApiKey(Base):
    __tablename__ = "api_keys"

    key = Column(String(64), primary_key=True)
    plan_name = Column(String(32), nullable=False, index=True)
    label = Column(String(64), default="")
    active = Column(Boolean, default=True, nullable=False)


def make_key():
    """A random, unguessable API key."""
    return "rk_" + secrets.token_urlsafe(32)


# name, limit, window seconds, algorithm
PLANS = [
    ("free", 10, 60, "sliding_window"),        # small, so it is easy to demo
    ("pro", 100, 60, "sliding_window"),
    ("enterprise", 1000, 60, "token_bucket"),  # burst-friendly, see main.py
]


# Any constant, as long as every worker uses the same one.
SETUP_LOCK = 728301


def setup():
    """
    Create tables, add the three plans, make one demo key per plan.
    Safe to run every time the container starts.
    Returns the demo keys so they can be printed at boot.

    THE SECOND RACE CONDITION IN THIS PROJECT.
    All four uvicorn workers boot at the same moment and all four would run
    CREATE TABLE against an empty database, so three of them crash with a
    duplicate-key error.

    Fixed with a Postgres advisory lock: the first worker to ask gets it, the
    other three block until it is released, and by then the tables exist so
    their create_all() does nothing. Same class of bug as the rate limiter's
    (two things checking-then-acting on shared state), solved with a
    different tool because this one is in Postgres rather than Redis.
    """
    lock = engine.connect()
    lock.execute(text("SELECT pg_advisory_lock(:id)"), {"id": SETUP_LOCK})
    lock.commit()

    try:
        return _create_and_seed()
    finally:
        # The lock is tied to this connection, so releasing and closing it
        # lets the next worker through.
        lock.execute(text("SELECT pg_advisory_unlock(:id)"), {"id": SETUP_LOCK})
        lock.commit()
        lock.close()


def _create_and_seed():
    Base.metadata.create_all(engine)
    demo = {}
    session = Session()

    for name, limit, window, algorithm in PLANS:
        if session.get(Plan, name) is None:
            session.add(Plan(name=name, max_requests=limit,
                             window_seconds=window, algorithm=algorithm))
    session.commit()

    for name, _, _, _ in PLANS:
        existing = (
            session.query(ApiKey)
            .filter(ApiKey.plan_name == name, ApiKey.label == "demo")
            .first()
        )
        if existing is None:
            existing = ApiKey(key=make_key(), plan_name=name, label="demo")
            session.add(existing)
            session.commit()
        demo[name] = existing.key

    session.close()
    return demo


def look_up_key(api_key):
    """
    Find the plan for an API key. Returns a plain dict, or None if the key
    is unknown or revoked. Called only on a cache miss -- see find_plan().
    """
    session = Session()
    row = (
        session.query(ApiKey, Plan)
        .filter(ApiKey.plan_name == Plan.name)
        .filter(ApiKey.key == api_key, ApiKey.active == True)  # noqa: E712
        .first()
    )
    session.close()

    if row is None:
        return None

    key, plan = row
    return {
        "api_key": key.key,
        "plan": plan.name,
        "limit": plan.max_requests,
        "window": plan.window_seconds,
        "algorithm": plan.algorithm,
    }


def change_plan(api_key, new_plan):
    """Upgrade or downgrade a key. Returns False if the key does not exist."""
    session = Session()
    record = session.get(ApiKey, api_key)
    if record is None:
        session.close()
        return False
    record.plan_name = new_plan
    session.commit()
    session.close()
    return True


def add_key(plan_name, label=""):
    key = make_key()
    session = Session()
    session.add(ApiKey(key=key, plan_name=plan_name, label=label))
    session.commit()
    session.close()
    return key
