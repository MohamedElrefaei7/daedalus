"""Tests 1, 3, 4, 5: the meter snaps to billed usage, caps are checked before sending,
interrupted streams are recorded, tool calls reassemble. Plus retries and error messages."""

from __future__ import annotations

import json
from decimal import Decimal

import httpx2
import pytest
from conftest import WED_OFF, Explodes, chunk, fixed, reply_chunks, sse, usage

from daedalus.terminal import ledger
from daedalus.terminal.config import Config
from daedalus.terminal.deepseek import Accumulator, DeepSeekError
from daedalus.terminal.ledger import CapExceeded
from daedalus.terminal.meter import Interrupted, Meter, metered_call, render
from daedalus.terminal.pricing import Usage, cost

MESSAGES = [{"role": "system", "content": "You are a test."}, {"role": "user", "content": "hello"}]


def call(fake, cfg, session_id="s1", messages=MESSAGES, tools=None, meter=None, readings=None):
    seen = readings if readings is not None else []
    return metered_call(fake.client(), meter or Meter(), cfg, session_id, messages, tools,
                        on_reading=seen.append, now=fixed(WED_OFF))


# 1. The meter snaps to billed usage ------------------------------------------------------

def test_meter_snaps_to_billed_usage(fake, cfg) -> None:
    content = "x" * 900  # ~300 tokens at 3.0 chars/token
    fake.responses.append(reply_chunks(content, use=usage(prompt_hit=9_100, prompt_miss=3_380, completion=512,
                                                           reasoning=0), pieces=30))
    readings = []
    reply, final = call(fake, cfg, readings=readings)

    streamed = [r for r in readings if r.estimated]
    assert streamed and all(r.estimated for r in readings[:-1])
    assert max(r.output_tokens for r in streamed) == 300  # what the characters implied
    assert not final.estimated and final is readings[-1]
    assert final.output_tokens == 512
    assert (final.input_tokens, final.cached_tokens) == (12_480, 9_100)
    row = ledger.rows()[-1]
    assert (row["completion"], row["prompt_cache_hit"], row["prompt_cache_miss"], row["estimated"]) == \
        (512, 9_100, 3_380, 0)
    assert row["cost_usd"] == pytest.approx(float(cost(Usage(9_100, 3_380, 512), "deepseek-flash", WED_OFF)))
    assert reply.content == content


def test_meter_line_format() -> None:
    from daedalus.terminal.meter import Reading
    r = Reading(False, 12_480, 9_100, 312, 180, 0.0021, 0.21, "off-peak")
    assert render(r) == "↑ 12,480 (9,100 cached)   ↓ 312 (reasoning 180)   $0.0021 this call   $0.21 today   off-peak"
    est = Reading(True, 12_480, None, 300, 180, 0.0021, 0.0042, "peak")
    assert render(est) == "~ ↑ 12,480   ↓ 300 (reasoning 180)   $0.0021 this call   $0.0042 today   peak"


def test_reasoning_ticks_separately_while_streaming(fake, cfg) -> None:
    fake.responses.append(reply_chunks("answer " * 10, reasoning="r" * 60, use=usage(completion=40, reasoning=20)))
    readings = []
    _, final = call(fake, cfg, readings=readings)
    assert any(r.estimated and r.reasoning_tokens == 20 for r in readings)  # 60 chars / 3.0
    assert (final.reasoning_tokens, final.output_tokens) == (20, 40)


def test_calibration_from_billed_prompt_tokens(fake, cfg) -> None:
    meter = Meter()
    fake.responses.append(reply_chunks("ok", use=usage(prompt_miss=10)))
    call(fake, cfg, meter=meter)
    chars = len(json.dumps(MESSAGES))
    assert meter.chars_per_token == pytest.approx(chars / 10)


def test_every_request_sets_its_limits(fake, cfg) -> None:
    fake.responses.append(reply_chunks("ok"))
    call(fake, cfg)
    body = fake.bodies()[0]
    assert body["stream"] is True and body["stream_options"] == {"include_usage": True}
    assert body["max_tokens"] == 8000 and body["reasoning_effort"] == "low"
    assert body["thinking"] == {"type": "enabled"}
    assert body["model"] == "deepseek-flash"
    assert fake.requests[0].url == "https://api.deepseek.com/chat/completions"
    assert fake.requests[0].headers["authorization"] == "Bearer sk-TESTKEY-123"


# 3. The cap is checked before sending ---------------------------------------------------

def test_session_cap_blocks_before_any_request(fake) -> None:
    cfg = Config(session_cap_usd=0.001)  # below one worst case (8,000 output tokens is $0.0048)
    fake.responses.append(reply_chunks("never sent"))
    with pytest.raises(CapExceeded) as exc:
        call(fake, cfg)
    assert fake.requests == []
    assert exc.value.cap == "session" and "session cap" in str(exc.value)
    assert ledger.rows() == []


def test_daily_cap_blocks_when_another_session_filled_it(fake, cfg) -> None:
    other = ledger.reserve(session_id="other-terminal", model="deepseek-flash", when=WED_OFF, peak=False,
                           input_tokens=0, max_tokens=0, worst_case=Decimal("2.999"), session_cap=10, daily_cap=10)
    ledger.settle(other, Usage(), Decimal("2.999"), estimated=False)
    fake.responses.append(reply_chunks("never sent"))
    with pytest.raises(CapExceeded) as exc:
        call(fake, cfg, session_id="this-terminal")
    assert fake.requests == []
    assert exc.value.cap == "daily" and "daily cap" in str(exc.value)


def test_worst_case_counts_full_max_tokens(fake) -> None:
    """A call that will certainly be cheap is still refused if its worst case crosses the cap."""
    cfg = Config(session_cap_usd=0.0045, max_tokens=8000)
    fake.responses.append(reply_chunks("tiny"))
    with pytest.raises(CapExceeded):
        call(fake, cfg)
    assert fake.requests == []
    fake.responses[:] = [reply_chunks("tiny")]
    call(fake, Config(session_cap_usd=0.0045, max_tokens=1000))
    assert len(fake.requests) == 1


# 4. Interrupted streams are recorded -----------------------------------------------------

def test_interrupted_stream_is_recorded_as_estimate(fake, cfg) -> None:
    partial = sse([chunk({"role": "assistant", "content": ""}), chunk({"reasoning_content": "r" * 30}),
                   chunk({"content": "y" * 150})], done=False)
    fake.responses.append(Explodes(partial))
    with pytest.raises(Interrupted) as exc:
        call(fake, cfg)
    rows = ledger.rows()
    assert len(rows) == 1
    row = rows[0]
    input_tokens = Meter().tokens(len(json.dumps(MESSAGES)))
    assert row["estimated"] == 1
    assert (row["prompt_cache_hit"], row["prompt_cache_miss"]) == (0, input_tokens)  # all cache miss
    assert (row["completion"], row["reasoning"]) == (60, 10)  # 180 streamed chars / 3.0
    want = float(cost(Usage(0, input_tokens, 60, 10), "deepseek-flash", WED_OFF))
    assert row["cost_usd"] == pytest.approx(want)
    assert ledger.spent() == pytest.approx(want)
    assert ledger.spent(session_id="s1") == pytest.approx(want)
    assert exc.value.reading.estimated
    assert exc.value.partial.content == "y" * 150


def test_stream_without_usage_chunk_is_interrupted(fake, cfg) -> None:
    fake.responses.append(sse([chunk({"content": "cut off"})]))  # [DONE] but no usage
    with pytest.raises(Interrupted):
        call(fake, cfg)
    assert ledger.rows()[0]["estimated"] == 1


def test_ctrl_c_mid_stream_is_recorded(fake, cfg) -> None:
    fake.responses.append(reply_chunks("z" * 90, pieces=9))
    calls = {"n": 0}

    def on_reading(r):
        calls["n"] += 1
        if calls["n"] == 4:
            raise KeyboardInterrupt

    with pytest.raises(Interrupted) as exc:
        metered_call(fake.client(), Meter(), cfg, "s1", MESSAGES, None, on_reading=on_reading, now=fixed(WED_OFF))
    assert isinstance(exc.value.cause, KeyboardInterrupt)
    assert ledger.rows()[0]["estimated"] == 1 and ledger.spent() > 0


# 5. Tool-call accumulation ----------------------------------------------------------------

def test_tool_call_arguments_split_across_five_chunks(fake, cfg) -> None:
    args = {"ref": "shared:practices/red-test-first.md", "note": "a fairly long argument string, to split up"}
    fake.responses.append(reply_chunks(tool_calls=[{"name": "read_note", "args": args, "pieces": 5, "id": "c1"}]))
    stream = reply_chunks(tool_calls=[{"name": "read_note", "args": args, "pieces": 5}])
    assert sum(1 for c in stream if c["choices"] and c["choices"][0]["delta"].get("tool_calls")) == 5
    reply, _ = call(fake, cfg)
    (tc,) = reply.ordered_tool_calls()
    assert (tc.id, tc.name) == ("c1", "read_note")
    assert json.loads(tc.arguments) == args
    assert reply.finish_reason == "tool_calls"


def test_two_parallel_tool_calls_keep_their_indexes() -> None:
    acc = Accumulator()
    from openai.types.chat import ChatCompletionChunk

    def feed(d):
        acc.feed(ChatCompletionChunk.model_validate(chunk(d)))

    a, b = json.dumps({"ref": "a"}), json.dumps({"query": "b b b"})
    feed({"tool_calls": [{"index": 0, "id": "A", "type": "function", "function": {"name": "read_note", "arguments": ""}}]})
    feed({"tool_calls": [{"index": 1, "id": "B", "type": "function", "function": {"name": "search", "arguments": ""}}]})
    for i in range(0, max(len(a), len(b)), 3):  # interleaved fragments
        if a[i:i + 3]:
            feed({"tool_calls": [{"index": 0, "function": {"arguments": a[i:i + 3]}}]})
        if b[i:i + 3]:
            feed({"tool_calls": [{"index": 1, "function": {"arguments": b[i:i + 3]}}]})
    calls = acc.reply.ordered_tool_calls()
    assert [(c.id, c.name, json.loads(c.arguments)) for c in calls] == [
        ("A", "read_note", {"ref": "a"}), ("B", "search", {"query": "b b b"})]


# Errors and retries -----------------------------------------------------------------------

def error(status: int, message: str = "nope") -> httpx2.Response:
    return httpx2.Response(status, json={"error": {"message": message, "type": "test"}})


@pytest.mark.parametrize("status, says", [
    (400, "refused the request (400)"), (401, "python -m daedalus.secrets set deepseek"),
    (402, "Insufficient Balance"), (422, "(422)"),
])
def test_client_errors_are_clear_and_never_charged(fake, cfg, status, says) -> None:
    fake.responses.append(error(status))
    with pytest.raises(DeepSeekError) as exc:
        call(fake, cfg)
    assert says in str(exc.value) and exc.value.status == status
    assert len(fake.requests) == 1  # no retry
    assert ledger.rows() == []  # never streamed, never charged


@pytest.mark.parametrize("status", [429, 500, 503])
def test_429_and_5xx_retry_twice(fake, cfg, status) -> None:
    fake.responses += [error(status), error(status), reply_chunks("third time")]
    reply, _ = call(fake, cfg)
    assert reply.content == "third time" and len(fake.requests) == 3
    assert len(ledger.rows()) == 1


def test_retries_give_up_after_two(fake, cfg) -> None:
    fake.responses += [error(503)] * 3
    with pytest.raises(DeepSeekError) as exc:
        call(fake, cfg)
    assert len(fake.requests) == 3 and "after 2 retries" in str(exc.value)
    assert ledger.rows() == []


def test_repr_hides_key(fake) -> None:
    assert "TESTKEY" not in repr(fake.client())
