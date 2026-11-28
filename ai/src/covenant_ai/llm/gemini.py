"""Live Google GenAI SDK adapter with workload timeouts, retries, and error classification."""
from __future__ import annotations

import time
from collections.abc import Sequence
from typing import Any, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

from covenant_ai.llm.client import (
    LLMClientError,
    LLMConfig,
    LLMInterceptor,
    LLMRateLimitError,
    LLMTimeoutError,
    apply_after_interceptors,
    apply_before_interceptors,
)

T = TypeVar("T", bound=BaseModel)


def _classify_gemini_error(exc: Exception, *, task_name: str, model_name: str) -> LLMClientError:
    """Normalize SDK or transport failures cleanly into retryable vs fatal taxonomy."""
    if isinstance(exc, LLMClientError):
        return exc
    msg = str(exc) or type(exc).__name__
    lower = msg.lower()
    if isinstance(exc, TimeoutError) or "timeout" in lower or "timed out" in lower or "deadline" in lower:
        return LLMTimeoutError(f"Gemini timed out on {task_name}: {msg}", task_name=task_name, model_name=model_name)
    if "429" in lower or "rate limit" in lower or "resource_exhausted" in lower or "quota" in lower:
        return LLMRateLimitError(f"Gemini rate-limited on {task_name}: {msg}", task_name=task_name, model_name=model_name)
    transient = any(code in lower for code in ("500", "502", "503", "504", "unavailable", "connection reset"))
    return LLMClientError(
        f"Gemini error on {task_name}: {msg}",
        task_name=task_name,
        model_name=model_name,
        category="provider_transient" if transient else "provider_error",
        retryable=transient,
    )


class GeminiClient:
    """Single-SDK structured output client backed by google-genai."""

    def __init__(
        self,
        *,
        api_key: str,
        config: LLMConfig | None = None,
        interceptors: Sequence[LLMInterceptor] = (),
        client: Any = None,
    ) -> None:
        self.config = config or LLMConfig.from_env()
        self.interceptors = tuple(interceptors)
        self.last_call_metadata: dict[str, Any] = {}
        if client is not None:
            self._client = client
        else:
            timeout_ms = max(1000, int(round(self.config.timeout_s * 1000)))
            self._client = genai.Client(
                api_key=api_key,
                http_options=types.HttpOptions(timeout=timeout_ms),
            )

    def generate_structured(
        self,
        *,
        prompt: str,
        system_prompt: str,
        schema: type[T],
        task_name: str,
        trace_id: str | None = None,
    ) -> T:
        model_name = self.config.resolve_model(task_name)
        workload = self.config.resolve_workload(task_name)
        call_timeout_s = self.config.resolve_timeout_s(task_name)
        call_timeout_ms = max(1000, int(round(call_timeout_s * 1000)))

        prepared_prompt = apply_before_interceptors(
            self.interceptors,
            task_name=task_name,
            model_name=model_name,
            prompt=prompt,
            trace_id=trace_id,
        )

        gen_config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=self.config.temperature,
            response_mime_type="application/json",
            response_schema=schema,
            http_options=types.HttpOptions(timeout=call_timeout_ms),
        )

        attempts = max(1, 1 + max(0, int(self.config.max_retries)))
        response: Any = None
        last_err: Exception | None = None
        started = time.perf_counter()
        for attempt in range(1, attempts + 1):
            try:
                response = self._client.models.generate_content(
                    model=model_name,
                    contents=prepared_prompt,
                    config=gen_config,
                )
                break
            except Exception as exc:
                classified = _classify_gemini_error(exc, task_name=task_name, model_name=model_name)
                last_err = classified
                if not classified.retryable or attempt >= attempts:
                    raise classified from exc
                time.sleep(min(0.25 * attempt, 1.0))

        if response is None and last_err is not None:
            raise last_err

        self.last_call_metadata = {
            "task_name": task_name,
            "workload": workload.value,
            "model_name": model_name,
            "timeout_ms": call_timeout_ms,
            "duration_ms": round((time.perf_counter() - started) * 1000, 1),
        }

        parsed = getattr(response, "parsed", None)
        if isinstance(parsed, schema):
            result = parsed
        elif isinstance(parsed, BaseModel):
            result = schema.model_validate(parsed.model_dump())
        elif isinstance(parsed, dict):
            result = schema.model_validate(parsed)
        else:
            raw_text = getattr(response, "text", None)
            if not raw_text:
                raise ValueError(f"Gemini returned empty structured payload for task {task_name!r}")
            result = schema.model_validate_json(raw_text)

        return apply_after_interceptors(
            self.interceptors,
            task_name=task_name,
            model_name=model_name,
            parsed=result,
            trace_id=trace_id,
        )
