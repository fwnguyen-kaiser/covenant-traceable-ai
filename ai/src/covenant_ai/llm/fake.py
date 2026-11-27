"""Deterministic FakeLLMClient for 100% offline pytest suites."""
from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any, TypeVar

from pydantic import BaseModel

from covenant_ai.llm.client import (
    LLMConfig,
    LLMInterceptor,
    apply_after_interceptors,
    apply_before_interceptors,
)

T = TypeVar("T", bound=BaseModel)


class FakeLLMClient:
    """Offline scriptable LLMClient tracking call metadata and 2-tier model resolution."""

    local_only = True

    def __init__(
        self,
        responses: Mapping[str, Any] | None = None,
        *,
        config: LLMConfig | None = None,
        interceptors: Sequence[LLMInterceptor] = (),
    ) -> None:
        self.config = config or LLMConfig()
        self.interceptors = tuple(interceptors)
        self._responses: dict[str, Any] = dict(responses or {})
        self._indices: dict[str, int] = {}
        self.calls: list[dict[str, Any]] = []

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
        prepared_prompt = apply_before_interceptors(
            self.interceptors,
            task_name=task_name,
            model_name=model_name,
            prompt=prompt,
            trace_id=trace_id,
        )
        self.calls.append(
            {
                "task_name": task_name,
                "model_name": model_name,
                "prompt": prepared_prompt,
                "system_prompt": system_prompt,
                "trace_id": trace_id,
            }
        )

        if task_name not in self._responses:
            raise KeyError(f"FakeLLMClient has no scripted response for task {task_name!r}")

        entry = self._responses[task_name]
        if callable(entry):
            raw = entry(prepared_prompt, len(self.calls))
        elif isinstance(entry, list):
            idx = self._indices.get(task_name, 0)
            if idx >= len(entry):
                raw = entry[-1]
            else:
                raw = entry[idx]
                self._indices[task_name] = idx + 1
        else:
            raw = entry

        if isinstance(raw, Exception):
            raise raw
        if isinstance(raw, schema):
            result = raw
        elif isinstance(raw, BaseModel):
            result = schema.model_validate(raw.model_dump())
        elif isinstance(raw, dict):
            result = schema.model_validate(raw)
        elif isinstance(raw, str):
            result = schema.model_validate_json(raw)
        else:
            raise TypeError(f"Unsupported scripted response type {type(raw)} for {task_name!r}")

        return apply_after_interceptors(
            self.interceptors,
            task_name=task_name,
            model_name=model_name,
            parsed=result,
            trace_id=trace_id,
        )
