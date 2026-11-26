"""The DeepSeek client: streamed chat completions with usage, through the OpenAI SDK.

Facts from api-docs.deepseek.com (checked 2026-11-25):
  * base_url https://api.deepseek.com; stream=True with
    stream_options={"include_usage": True} (sent without stream=True it's a 400).
  * usage rides on the final content chunk, just before [DONE]; every earlier
    chunk has usage: null.
  * Thinking mode is on by default with effort "high", and its max_tokens
    default is 64K. Both are set explicitly on every request.
  * With tools, every earlier assistant message's reasoning_content must be
    passed back, or the API returns 400. The agent keeps it in history.

The SDK's own retries are off (max_retries=0): this module retries 429 and
5xx twice with backoff itself, so the count is explicit and testable.
"""

from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from typing import Any, Callable

import httpx2
import openai
from openai import OpenAI

from .pricing import Usage

BASE_URL = "https://api.deepseek.com"
RETRIES = 2
BACKOFF_S = (1.0, 3.0)
TIMEOUT_S = 300.0


class DeepSeekError(Exception):
    """A request DeepSeek refused or couldn't serve; the message says what to do."""

    def __init__(self, message: str, status: int | None = None) -> None:
        self.status = status
        super().__init__(message)


class StreamInterrupted(Exception):
    """The stream started but ended without its usage chunk (network drop, exception)."""

    def __init__(self, partial: "Reply", cause: BaseException) -> None:
        self.partial = partial
        self.cause = cause
        super().__init__(f"the stream was interrupted: {type(cause).__name__}: {cause}")


@dataclass
class ToolCall:
    id: str = ""
    name: str = ""
    arguments: str = ""  # JSON text, as streamed


@dataclass
class Reply:
    """One streamed response, accumulated from its deltas."""

    content: str = ""
    reasoning: str = ""
    tool_calls: dict[int, ToolCall] = field(default_factory=dict)  # by the delta's index
    finish_reason: str | None = None
    usage: Usage | None = None  # None until the final chunk arrives
    prompt_tokens: int | None = None

    def ordered_tool_calls(self) -> list[ToolCall]:
        return [self.tool_calls[i] for i in sorted(self.tool_calls)]

    def message(self) -> dict[str, Any]:
        """This reply as an assistant message for history, reasoning_content included."""
        msg: dict[str, Any] = {"role": "assistant", "content": self.content, "reasoning_content": self.reasoning}
        calls = self.ordered_tool_calls()
        if calls:
            msg["tool_calls"] = [
                {"id": c.id, "type": "function", "function": {"name": c.name, "arguments": c.arguments}}
                for c in calls
            ]
        return msg


class Accumulator:
    """Folds stream chunks into a Reply. ``on_delta(kind, text)`` sees each content/reasoning piece."""

    def __init__(self, on_delta: Callable[[str, str], None] | None = None) -> None:
        self.reply = Reply()
        self.on_delta = on_delta

    def feed(self, chunk: Any) -> None:
        for choice in chunk.choices or ():
            delta = choice.delta
            if delta is not None:
                reasoning = getattr(delta, "reasoning_content", None)
                if reasoning:
                    self.reply.reasoning += reasoning
                    self._emit("reasoning", reasoning)
                if delta.content:
                    self.reply.content += delta.content
                    self._emit("content", delta.content)
                for tc in delta.tool_calls or ():
                    call = self.reply.tool_calls.setdefault(tc.index, ToolCall())
                    if tc.id:
                        call.id = tc.id
                    if tc.function is not None:
                        if tc.function.name:
                            call.name += tc.function.name
                        if tc.function.arguments:
                            call.arguments += tc.function.arguments
                    self._emit("tool", tc.function.name if tc.function is not None and tc.function.name else "")
            if choice.finish_reason:
                self.reply.finish_reason = choice.finish_reason
        if chunk.usage is not None:
            u = chunk.usage
            hit = getattr(u, "prompt_cache_hit_tokens", None)
            miss = getattr(u, "prompt_cache_miss_tokens", None)
            if hit is None or miss is None:  # fall back to the OpenAI-style fields
                hit = (u.prompt_tokens_details.cached_tokens or 0) if u.prompt_tokens_details else 0
                miss = (u.prompt_tokens or 0) - hit
            details = u.completion_tokens_details
            reasoning_tokens = (details.reasoning_tokens or 0) if details else 0
            self.reply.usage = Usage(int(hit), int(miss), int(u.completion_tokens or 0), int(reasoning_tokens))
            self.reply.prompt_tokens = int(u.prompt_tokens or (hit + miss))

    def _emit(self, kind: str, text: str) -> None:
        if self.on_delta is not None:
            self.on_delta(kind, text)


def _api_message(e: openai.APIStatusError) -> str:
    try:
        body = e.response.json()
        return str(body.get("error", {}).get("message") or body)
    except Exception:
        return e.response.text[:300] if e.response is not None else str(e)


def explain(e: openai.APIStatusError) -> DeepSeekError:
    status = e.status_code
    detail = _api_message(e)
    if status == 401:
        return DeepSeekError("DeepSeek rejected the API key (401). Store a working one with: "
                             "python -m daedalus.secrets set deepseek", status)
    if status == 402:
        return DeepSeekError("DeepSeek says the balance is too low (402 Insufficient Balance). Top up at "
                             "https://platform.deepseek.com/top_up", status)
    if status in (400, 422):
        return DeepSeekError(f"DeepSeek refused the request ({status}): {detail}", status)
    if status == 429:
        return DeepSeekError(f"DeepSeek is rate-limiting this key (429), still after {RETRIES} retries: {detail}", status)
    if status >= 500:
        return DeepSeekError(f"DeepSeek had a server error ({status}), still after {RETRIES} retries: {detail}", status)
    return DeepSeekError(f"DeepSeek returned {status}: {detail}", status)


def _retryable(status: int) -> bool:
    return status == 429 or status >= 500


class DeepSeek:
    """A DeepSeek API client. ``transport`` replaces the network in tests."""

    def __init__(self, api_key: str, transport: httpx2.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep) -> None:
        self._http = httpx2.Client(transport=transport, timeout=TIMEOUT_S)
        self._key = api_key
        self._sleep = sleep
        self.client = OpenAI(api_key=api_key, base_url=BASE_URL, http_client=self._http, max_retries=0)

    def __repr__(self) -> str:  # never show the key
        return "DeepSeek(base_url=%r)" % BASE_URL

    def open_stream(self, *, model: str, messages: list[dict], tools: list[dict] | None, max_tokens: int,
                    reasoning_effort: str) -> Any:
        """Send the request and return the open stream once DeepSeek answers 200.

        429 and 5xx retry twice with backoff; nothing has streamed yet, so
        nothing was billed. Other errors raise DeepSeekError at once.
        """
        kwargs: dict[str, Any] = dict(
            model=model, messages=messages, stream=True, stream_options={"include_usage": True},
            max_tokens=max_tokens, reasoning_effort=reasoning_effort,
            extra_body={"thinking": {"type": "enabled"}},
        )
        if tools:
            kwargs["tools"] = tools
            kwargs["tool_choice"] = "auto"  # "required" and named choices are a 400 in thinking mode
        for attempt in range(RETRIES + 1):
            try:
                return self.client.chat.completions.create(**kwargs)
            except openai.APIStatusError as e:
                if _retryable(e.status_code) and attempt < RETRIES:
                    self._sleep(BACKOFF_S[attempt])
                    continue
                raise explain(e) from None
            except openai.APIConnectionError as e:
                raise DeepSeekError(f"couldn't reach DeepSeek: {e}") from None
        raise AssertionError("unreachable")

    @staticmethod
    def read_stream(stream: Any, acc: Accumulator) -> Reply:
        """Consume an open stream into ``acc``. No usage chunk means StreamInterrupted."""
        try:
            for chunk in stream:
                acc.feed(chunk)
        except Exception as e:
            raise StreamInterrupted(acc.reply, e) from e
        finally:
            stream.close()
        if acc.reply.usage is None:
            raise StreamInterrupted(acc.reply, RuntimeError("the stream ended without a usage chunk"))
        return acc.reply

    def balance(self) -> dict[str, Any]:
        """GET /user/balance: {"is_available": bool, "balance_infos": [{currency, total_balance, ...}]}."""
        try:
            r = self._http.get(f"{BASE_URL}/user/balance",
                               headers={"Authorization": f"Bearer {self._key}", "Accept": "application/json"})
        except httpx2.HTTPError as e:
            raise DeepSeekError(f"couldn't reach DeepSeek for the balance: {type(e).__name__}") from None
        if r.status_code != 200:
            raise DeepSeekError(f"balance request failed ({r.status_code})", r.status_code)
        return r.json()

    def close(self) -> None:
        self._http.close()


def request_chars(messages: list[dict], tools: list[dict] | None) -> int:
    """The size of a request body's messages and tools, for estimating its input tokens."""
    return len(json.dumps(messages, ensure_ascii=False)) + (len(json.dumps(tools, ensure_ascii=False)) if tools else 0)
