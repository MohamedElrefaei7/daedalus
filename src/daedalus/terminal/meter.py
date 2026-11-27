"""The token meter and the spend caps around every DeepSeek call.

Before sending: estimate input tokens from the request's characters (3.0
chars per token to start, then calibrated per session from the last call's
billed prompt_tokens), price the worst case (every input token a cache miss,
all of max_tokens used, at the current period's rate), and reserve it in the
ledger. If the session or daily cap would be crossed, ledger.reserve raises
CapExceeded and nothing leaves the machine.

While streaming: the line is an estimate (a leading "~"), with output
counted from the streamed characters and reasoning shown separately.

On the final chunk: the line snaps to the billed usage and the ledger row is
settled with estimated = false. If the stream is interrupted there is no
usage chunk, so the row is settled as an estimate instead (input all cache
miss, output from the streamed characters). It still counts toward the caps:
a stopped stream may have been billed.
"""

from __future__ import annotations

import datetime as dt
import math
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Callable

from . import ledger
from .config import Config
from .deepseek import Accumulator, DeepSeek, DeepSeekError, Reply, StreamInterrupted, request_chars
from .pricing import Usage, cost, is_peak, period, worst_case

START_CHARS_PER_TOKEN = 3.0


@dataclass(frozen=True)
class Reading:
    """What the meter line shows."""

    estimated: bool
    input_tokens: int
    cached_tokens: int | None  # None while estimating: the cache split is only known when billed
    output_tokens: int
    reasoning_tokens: int
    call_usd: float
    today_usd: float
    period: str


def money(usd: float) -> str:
    """Two decimals from $0.10 up; four below, so small totals don't read as $0.00."""
    return f"${usd:.2f}" if usd >= 0.10 else f"${usd:.4f}"


def render(r: Reading) -> str:
    up = f"↑ {r.input_tokens:,}" + (f" ({r.cached_tokens:,} cached)" if r.cached_tokens is not None else "")
    down = f"↓ {r.output_tokens:,} (reasoning {r.reasoning_tokens:,})"
    line = f"{up}   {down}   ${r.call_usd:.4f} this call   {money(r.today_usd)} today   {r.period}"
    return ("~ " + line) if r.estimated else line


class Meter:
    """Per-session token estimation, calibrated from billed prompt_tokens."""

    def __init__(self, chars_per_token: float = START_CHARS_PER_TOKEN) -> None:
        self.chars_per_token = chars_per_token

    def tokens(self, chars: int) -> int:
        return math.ceil(chars / self.chars_per_token) if chars > 0 else 0

    def calibrate(self, chars: int, prompt_tokens: int | None) -> None:
        if prompt_tokens and prompt_tokens > 0 and chars > 0:
            self.chars_per_token = chars / prompt_tokens


class Interrupted(Exception):
    """A call stopped mid-stream (Ctrl-C, a dropped connection); it was recorded as an estimate."""

    def __init__(self, partial: Reply, reading: Reading, cause: BaseException) -> None:
        self.partial = partial
        self.reading = reading
        self.cause = cause
        what = "Ctrl-C" if isinstance(cause, KeyboardInterrupt) else str(cause)
        super().__init__(f"interrupted ({what}); recorded as an estimate: {render(reading)}")


def _streamed_usage(meter: Meter, input_tokens: int, reply: Reply) -> Usage:
    reasoning = meter.tokens(len(reply.reasoning))
    other = meter.tokens(len(reply.content) + sum(len(c.name) + len(c.arguments) for c in reply.tool_calls.values()))
    return Usage(prompt_cache_hit=0, prompt_cache_miss=input_tokens, completion=reasoning + other, reasoning=reasoning)


def metered_call(
    ds: DeepSeek, meter: Meter, cfg: Config, session_id: str, messages: list[dict], tools: list[dict] | None,
    on_reading: Callable[[Reading], None] = lambda r: None,
    on_delta: Callable[[str, str], None] = lambda kind, text: None,
    now: Callable[[], dt.datetime] = ledger.utc_now,
    run_id: str | None = None,
    run_cap: float | None = None,
) -> tuple[Reply, Reading]:
    """One capped, metered, ledgered streaming call. Raises CapExceeded, DeepSeekError, or Interrupted."""
    when = now()
    peak = is_peak(when)
    chars = request_chars(messages, tools)
    input_tokens = meter.tokens(chars)
    worst = worst_case(input_tokens, cfg.max_tokens, cfg.model, when)
    today_before = ledger.spent(since_utc=ledger.start_of_day(when))

    # The cap check and the reservation, before any network I/O.
    row_id = ledger.reserve(
        session_id=session_id, model=cfg.model, when=when, peak=peak, input_tokens=input_tokens,
        max_tokens=cfg.max_tokens, worst_case=worst, session_cap=cfg.session_cap_usd, daily_cap=cfg.daily_cap_usd,
        run_id=run_id, run_cap=run_cap,
    )

    def estimate(reply: Reply) -> tuple[Usage, Decimal, Reading]:
        usage = _streamed_usage(meter, input_tokens, reply)
        c = cost(usage, cfg.model, when)
        return usage, c, Reading(True, input_tokens, None, usage.completion, usage.reasoning, float(c),
                                 today_before + float(c), period(when))

    try:
        stream = ds.open_stream(model=cfg.model, messages=messages, tools=tools, max_tokens=cfg.max_tokens,
                                reasoning_effort=cfg.reasoning_effort)
    except BaseException:
        ledger.cancel(row_id)  # never answered with a stream: nothing was billed
        raise

    acc: Accumulator

    def delta(kind: str, text: str) -> None:
        on_delta(kind, text)
        on_reading(estimate(acc.reply)[2])

    acc = Accumulator(delta)
    on_reading(estimate(acc.reply)[2])
    try:
        reply = ds.read_stream(stream, acc)
    except (StreamInterrupted, KeyboardInterrupt) as e:
        partial = e.partial if isinstance(e, StreamInterrupted) else acc.reply
        usage, c, reading = estimate(partial)
        ledger.settle(row_id, usage, c, estimated=True)
        on_reading(reading)
        raise Interrupted(partial, reading, e.cause if isinstance(e, StreamInterrupted) else e) from None

    usage = reply.usage
    assert usage is not None
    billed = cost(usage, cfg.model, when)
    ledger.settle(row_id, usage, billed, estimated=False)
    meter.calibrate(chars, reply.prompt_tokens)
    reading = Reading(False, usage.prompt, usage.prompt_cache_hit, usage.completion, usage.reasoning, float(billed),
                      ledger.spent(since_utc=ledger.start_of_day(when)), period(when))
    on_reading(reading)
    return reply, reading


__all__ = ["Interrupted", "Meter", "Reading", "DeepSeekError", "metered_call", "render"]
