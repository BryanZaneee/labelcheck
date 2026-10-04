"""OpenAI vision reader (PRD §5.2).

A thin adapter on the Chat Completions API, so any OpenAI-compatible endpoint is
a base-URL change rather than a rewrite.

ponytail: synchronous client. The callers are sync `def`, so FastAPI already
runs them in a threadpool; move to AsyncOpenAI if a 300-record batch needs more.
"""

from __future__ import annotations

import base64
import json
import re
import sqlite3
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from openai import APIError, OpenAI

from config import settings
from models import CaptureQuality, FieldReading, LabelReading, WarningReading
from readers.prep import prepare
from readers.prompts import PROMPT, SCHEMA, VERSION

# PRD §5.2 clamps configured effort to the provider floor; OpenAI accepts "none".
_EFFORT_ORDER = ["none", "minimal", "low", "medium", "high", "xhigh", "max"]
_EFFORT_FLOOR = "none"

RETRIES = 2

_REASONING_MODEL = re.compile(r"^(gpt-[5-9]|o[1-9])", re.IGNORECASE)

# PRD §5.2 calls the default tier "standard"; the API does not have that value.
_SERVICE_TIER = "auto"


class ReaderError(RuntimeError):
    """The reader could not produce a usable reading. Callers fall back to OCR."""


class SpendCapReached(ReaderError):
    """The daily paid-call ceiling is spent. Verification degrades to rules."""


# SQLite serializes reservations across workers and persists before provider calls.
@contextmanager
def _counter() -> Iterator[sqlite3.Connection]:
    conn = None
    try:
        root = Path(settings.data_dir)
        root.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(root / "vision_calls.sqlite", timeout=5)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "CREATE TABLE IF NOT EXISTS calls (day TEXT PRIMARY KEY, count INTEGER NOT NULL)"
        )
        # Preserve the previous JSON counter on the first run after upgrading.
        if conn.execute("SELECT 1 FROM calls LIMIT 1").fetchone() is None:
            legacy = root / "vision_calls.json"
            if legacy.exists():
                data = json.loads(legacy.read_text())
                day, count = data["date"], data["count"]
                if not isinstance(day, str) or type(count) is not int or count < 0:
                    raise ValueError("invalid legacy counter")
                conn.execute("INSERT INTO calls VALUES (?, ?)", (day, count))
        yield conn
        conn.commit()
    except (OSError, sqlite3.Error, ValueError, KeyError, TypeError) as exc:
        raise SpendCapReached("budget tracking unavailable; verification is rules-only") from exc
    finally:
        if conn is not None:
            conn.close()


def _charge_one_call(cap: int, operation: str) -> None:
    if cap <= 0:
        raise SpendCapReached("paid verification is disabled")
    try:
        bounds = json.loads(settings.demo_request_cost_bounds)
        cost = bounds.get(operation)
        if type(cost) is not int or not 0 < cost <= 500000:
            raise ValueError("missing cost bound")
    except (ValueError, TypeError, AttributeError) as exc:
        raise SpendCapReached("paid verification paused: spending bound is not configured") from exc
    today = datetime.now(UTC).date().isoformat()
    with _counter() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS spending (day TEXT PRIMARY KEY, used INTEGER NOT NULL CHECK(used BETWEEN 0 AND 500000))"
        )
        conn.execute("INSERT INTO spending VALUES (?, 0) ON CONFLICT DO NOTHING", (today,))
        if not conn.execute(
            "UPDATE spending SET used = used + ? WHERE day = ? AND used + ? <= 500000",
            (cost, today, cost),
        ).rowcount:
            raise SpendCapReached(
                "daily demo spending budget exhausted; verification is rules-only"
            )
        row = conn.execute("SELECT count FROM calls WHERE day = ?", (today,)).fetchone()
        spent = row[0] if row else 0
        if type(spent) is not int or spent < 0:
            raise ValueError("invalid budget counter")
        if spent >= cap:
            raise SpendCapReached(
                f"daily paid-call cap of {cap} reached; verification is rules-only "
                "until UTC midnight"
            )
        conn.execute(
            "INSERT INTO calls VALUES (?, ?) ON CONFLICT(day) DO UPDATE SET count = excluded.count",
            (today, spent + 1),
        )
        conn.execute("DELETE FROM calls WHERE day < ?", (today,))


def calls_today() -> int:
    today = datetime.now(UTC).date().isoformat()
    with _counter() as conn:
        row = conn.execute("SELECT count FROM calls WHERE day = ?", (today,)).fetchone()
        return int(row[0]) if row else 0


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0


def clamp_effort(effort: str) -> str:
    """Raise a configured effort to the provider floor (PRD §5.2)."""
    if effort not in _EFFORT_ORDER:
        return _EFFORT_FLOOR
    return max(effort, _EFFORT_FLOOR, key=_EFFORT_ORDER.index)


def _field(raw: dict[str, Any] | None) -> FieldReading:
    if not raw:
        return FieldReading(value=None, confidence=None)
    return FieldReading(value=raw.get("value"), confidence=raw.get("confidence"))


class VisionReader:
    def __init__(
        self,
        provider: str,
        model: str,
        api_key: str,
        base_url: str = "",
        effort: str = "low",
        timeout_s: float = 25.0,
        daily_call_cap: int = 0,
    ) -> None:
        self.provider = provider
        self.model = model
        self.effort = clamp_effort(effort)
        self.prompt_version = VERSION
        self.daily_call_cap = daily_call_cap
        if not api_key:
            raise ReaderError(f"{provider}: no API key configured")
        self.client = OpenAI(
            api_key=api_key, base_url=base_url or None, timeout=timeout_s, max_retries=0
        )
        self.usage = Usage()

    def read(self, specimen: str, image_path: Path | None = None) -> LabelReading:
        path = image_path or Path("fixtures") / Path(specimen).name
        prepared = prepare(path)
        encoded = base64.b64encode(prepared.jpeg).decode()

        last: Exception | None = None
        for attempt in range(RETRIES + 1):
            try:
                return self._extract(encoded)
            except SpendCapReached:
                raise
            except (APIError, ValueError, KeyError, json.JSONDecodeError) as exc:
                last = exc
                if attempt < RETRIES:
                    time.sleep(0.5 * (attempt + 1))
        raise ReaderError(f"{self.provider}/{self.model}: {last}") from last

    def _extract(self, encoded_jpeg: str) -> LabelReading:
        _charge_one_call(self.daily_call_cap, f"{self.provider}:{self.model}")
        response = self.client.chat.completions.create(
            model=self.model,
            messages=[
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": PROMPT},
                        {
                            "type": "image_url",
                            "image_url": {"url": f"data:image/jpeg;base64,{encoded_jpeg}"},
                        },
                    ],
                }
            ],
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "label_reading", "schema": SCHEMA, "strict": True},
            },
            **self._effort_kwargs(),
        )
        if response.usage is not None:
            self.usage = Usage(
                input_tokens=response.usage.prompt_tokens or 0,
                output_tokens=response.usage.completion_tokens or 0,
            )
        content = response.choices[0].message.content
        if not content:
            raise ValueError("empty response")
        return self._to_reading(json.loads(content))

    def _effort_kwargs(self) -> dict[str, Any]:
        """Request knobs as the live API actually accepts them (PRD §5.2)."""
        kwargs: dict[str, Any] = {"service_tier": _SERVICE_TIER}
        # `reasoning_effort` is a 400 on a non-reasoning model, so gate on name.
        # ponytail: name prefix; swap for a capability lookup if one ships.
        if _REASONING_MODEL.match(self.model):
            kwargs["reasoning_effort"] = self.effort
        return kwargs

    def _to_reading(self, payload: dict[str, Any]) -> LabelReading:
        warning = payload.get("warning") or {}
        quality: CaptureQuality = payload.get("quality", "normal")
        return LabelReading(
            brand=_field(payload.get("brand")),
            class_type=_field(payload.get("classType")),
            abv=_field(payload.get("abv")),
            net=_field(payload.get("net")),
            producer=_field(payload.get("producer")),
            origin=_field(payload.get("origin")),
            warning=WarningReading(
                present=bool(warning.get("present")),
                body=warning.get("body"),
                header_case=warning.get("headerCase"),
                header_bold=warning.get("headerBold"),
            ),
            quality=quality,
            not_a_label=bool(payload.get("notALabel")),
        )
