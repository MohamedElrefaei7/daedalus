"""Helpers for the terminal tests: recorded SSE streams, a fake DeepSeek, an in-memory keyring.

Plain module (not conftest), so `from fakes import ...` means this file whichever
folders pytest is running.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from typing import Any, Iterable

import httpx2
from keyring.backend import KeyringBackend

from daedalus.terminal.deepseek import DeepSeek

TEST_KEY = "sk-TESTKEY-123"

# Times for pricing: 2026-11-25 is a Wednesday.
WED_PEAK = dt.datetime(2026, 11, 25, 2, 0, tzinfo=dt.timezone.utc)
WED_OFF = dt.datetime(2026, 11, 25, 5, 0, tzinfo=dt.timezone.utc)


class MemoryKeyring(KeyringBackend):
    priority = 1

    def __init__(self) -> None:
        super().__init__()
        self.store: dict[tuple[str, str], str] = {}

    def get_password(self, service, username):
        return self.store.get((service, username))

    def set_password(self, service, username, password):
        self.store[(service, username)] = password

    def delete_password(self, service, username):
        self.store.pop((service, username), None)


# Recorded streams ---------------------------------------------------------------------

def chunk(delta: dict | None = None, finish: str | None = None, usage: dict | None = None,
          choices: bool = True) -> dict:
    return {
        "id": "chatcmpl-test", "object": "chat.completion.chunk", "created": 1764036000, "model": "deepseek-flash",
        "choices": [{"index": 0, "delta": delta or {}, "finish_reason": finish, "logprobs": None}] if choices else [],
        "usage": usage,
    }


def usage(prompt_hit: int = 0, prompt_miss: int = 100, completion: int = 10, reasoning: int = 0) -> dict:
    return {
        "prompt_tokens": prompt_hit + prompt_miss, "completion_tokens": completion,
        "total_tokens": prompt_hit + prompt_miss + completion,
        "prompt_cache_hit_tokens": prompt_hit, "prompt_cache_miss_tokens": prompt_miss,
        "prompt_tokens_details": {"cached_tokens": prompt_hit},
        "completion_tokens_details": {"reasoning_tokens": reasoning},
    }


def sse(chunks: Iterable[dict], done: bool = True) -> bytes:
    body = b"".join(b"data: " + json.dumps(c).encode() + b"\n\n" for c in chunks)
    return body + (b"data: [DONE]\n\n" if done else b"")


def split(text: str, n: int) -> list[str]:
    size = max(1, -(-len(text) // n))
    return [text[i:i + size] for i in range(0, len(text), size)]


def reply_chunks(content: str = "", reasoning: str = "", tool_calls: list[dict] | None = None,
                 use: dict | None = None, pieces: int = 3) -> list[dict]:
    """A whole response: role, reasoning pieces, content pieces, tool-call pieces, then the usage chunk."""
    out = [chunk({"role": "assistant", "content": ""})]
    out += [chunk({"reasoning_content": p}) for p in split(reasoning, pieces)] if reasoning else []
    out += [chunk({"content": p}) for p in split(content, pieces)] if content else []
    for i, tc in enumerate(tool_calls or []):
        args = json.dumps(tc["args"])
        parts = split(args, tc.get("pieces", 2))
        out.append(chunk({"tool_calls": [{"index": tc.get("index", i), "id": tc.get("id", f"call_{i}"),
                                          "type": "function",
                                          "function": {"name": tc["name"], "arguments": parts[0]}}]}))
        out += [chunk({"tool_calls": [{"index": tc.get("index", i), "function": {"arguments": p}}]})
                for p in parts[1:]]
    finish = "tool_calls" if tool_calls else "stop"
    out.append(chunk({"content": ""}, finish, use or usage()))
    return out


class Explodes(httpx2.SyncByteStream):
    """Streams some chunks, then fails like a dropped connection."""

    def __init__(self, body: bytes) -> None:
        self.body = body

    def __iter__(self):
        yield self.body
        raise httpx2.ReadError("connection reset by peer")


@dataclass
class FakeDeepSeek:
    """Answers each request with the next scripted response; records every request."""

    responses: list[Any] = field(default_factory=list)
    requests: list[httpx2.Request] = field(default_factory=list)
    repeat_last: bool = False

    def handler(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        if request.url.path.endswith("/user/balance"):
            return httpx2.Response(200, json={"is_available": True, "balance_infos": [
                {"currency": "USD", "total_balance": "4.98", "granted_balance": "0.00", "topped_up_balance": "4.98"}]})
        if not self.responses:
            raise AssertionError("FakeDeepSeek got an unscripted request")
        r = self.responses[0] if (self.repeat_last and len(self.responses) == 1) else self.responses.pop(0)
        if isinstance(r, httpx2.Response):
            return r
        if isinstance(r, Explodes):
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, stream=r)
        if isinstance(r, list):
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=sse(r))
        if isinstance(r, bytes):
            return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=r)
        raise TypeError(r)

    @property
    def transport(self) -> httpx2.MockTransport:
        return httpx2.MockTransport(self.handler)

    def bodies(self) -> list[dict]:
        return [json.loads(r.content) for r in self.requests if r.url.path.endswith("/chat/completions")]

    def client(self) -> DeepSeek:
        return DeepSeek(TEST_KEY, transport=self.transport, sleep=lambda s: None)


def fixed(when: dt.datetime):
    return lambda: when
