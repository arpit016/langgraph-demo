"""Stand-in for Rails (culturegrade-api), which owns accounts/employees and the Build endpoint.

In production those live in another database, so the agents schema has no foreign keys to them
(account_id / employee_id are plain bigints). This file fakes just enough of Rails to exercise
the architecture: employee lookup and an idempotent Build endpoint.
"""

import threading
import uuid
from dataclasses import dataclass

ACCOUNTS = {
    1001: "Acme Corp",
    1002: "Globex Ltd",
}

# employee_id -> (account_id, name, email)
EMPLOYEES = {
    501: {"account_id": 1001, "name": "Asha Verma", "email": "asha@acme.test"},
    502: {"account_id": 1001, "name": "Rahul Mehta", "email": "rahul@acme.test"},
    503: {"account_id": 1001, "name": "Priya Nair", "email": "priya@acme.test"},
    601: {"account_id": 1002, "name": "Sam Carter", "email": "sam@globex.test"},
    602: {"account_id": 1002, "name": "Lena Fischer", "email": "lena@globex.test"},
}


@dataclass(frozen=True)
class RequestContext:
    account_id: int
    employee_id: int


def list_employees() -> list[dict]:
    return [
        {
            "employee_id": eid,
            "account_id": e["account_id"],
            "account": ACCOUNTS[e["account_id"]],
            "name": e["name"],
            "email": e["email"],
        }
        for eid, e in EMPLOYEES.items()
    ]


def resolve_context(employee_id: int) -> RequestContext | None:
    emp = EMPLOYEES.get(employee_id)
    if not emp:
        return None
    return RequestContext(account_id=emp["account_id"], employee_id=employee_id)


# ---- Build endpoint (idempotent on the Idempotency-Key) ----

_builds: dict[str, dict] = {}
_lock = threading.Lock()


class RailsRejected(Exception):
    """A definite rejection: nothing was committed, safe to retry."""

    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def build_trip(idempotency_key: str, account_id: int, plan: dict) -> dict:
    """Pretend to create bookings in Rails. Repeating a key returns the first result."""
    with _lock:
        if idempotency_key in _builds:
            return _builds[idempotency_key]
        if not (plan.get("itinerary") or "").strip():
            raise RailsRejected("empty_plan", "The plan has no itinerary to book")
        result = {
            "booking_ids": [f"BK-{uuid.uuid4().hex[:8].upper()}" for _ in range(2)],
            "account_id": account_id,
        }
        _builds[idempotency_key] = result
        return result
