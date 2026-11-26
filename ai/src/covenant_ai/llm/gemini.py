"""Live Google GenAI SDK adapter supporting 2-tier task routing and interceptors."""
from __future__ import annotations

from collections.abc import Sequence
from typing import Any, TypeVar

from google import genai
from google.genai import types
from pydantic import BaseModel

from covenant_ai.llm.client import (
    LLMConfig,
    LLMInterceptor,
    apply_after_interceptors,
    apply_before_interceptors,
)

T = TypeVar("T", bound=BaseModel)


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
        self._client = client or genai.Client(api_key=api_key)

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

        gen_config = types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=self.config.temperature,
            response_mime_type="application/json",
            response_schema=schema,
        )
        response = self._client.models.generate_content(
            model=model_name,
            contents=prepared_prompt,
            config=gen_config,
        )

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
