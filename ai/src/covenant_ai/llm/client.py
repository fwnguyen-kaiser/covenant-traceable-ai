"""Provider-agnostic LLM client protocol, 2-tier task routing, and interceptor hook."""
from __future__ import annotations

import os
from collections.abc import Sequence
from pathlib import Path
from typing import Protocol, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

WRITER_TASKS: frozenset[str] = frozenset({"explain_narrative", "narrative"})


def _read_env_or_dotenv(name: str) -> str | None:
    """Resolve config env var cleanly from process env or workspace .env."""
    val = os.getenv(name)
    if val:
        return val
    # ai/src/covenant_ai/llm/client.py -> workspace root is parents[4]
    for root in (Path(__file__).resolve().parents[4], Path.cwd(), Path.cwd().parent):
        env_path = root / ".env"
        if not env_path.is_file():
            continue
        for line in env_path.read_text(encoding="utf-8").splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith("#") or "=" not in stripped:
                continue
            k, v = stripped.split("=", 1)
            if k.strip() == name:
                return v.strip().strip('"').strip("'")
    return None


DEFAULT_LLM_MODEL = "gemini-2.5-flash"


class LLMConfig(BaseModel):
    """Single-SDK 2-tier model routing configuration resolved from env variables."""

    fast_model: str = DEFAULT_LLM_MODEL
    writer_model: str = DEFAULT_LLM_MODEL
    temperature: float = 0.0
    timeout_s: float = 45.0

    def resolve_model(self, task_name: str) -> str:
        """Select writer model for IB prose, fast model for intake and cross-version auditing."""
        return self.writer_model if task_name in WRITER_TASKS else self.fast_model

    @classmethod
    def from_env(cls) -> "LLMConfig":
        """Build routing config from env (COVENANT_LLM_MODEL forces single-model mode)."""
        single = _read_env_or_dotenv("COVENANT_LLM_MODEL")
        if single:
            return cls(fast_model=single.strip(), writer_model=single.strip())
        fast = (_read_env_or_dotenv("COVENANT_FAST_MODEL") or DEFAULT_LLM_MODEL).strip()
        writer = (_read_env_or_dotenv("COVENANT_WRITER_MODEL") or fast).strip()
        return cls(fast_model=fast, writer_model=writer)


class LLMInterceptor(Protocol):
    """Extension hook inserted before/after LLM calls (future AI security / MNPI redaction)."""

    def before_call(
        self,
        *,
        task_name: str,
        model_name: str,
        prompt: str,
        trace_id: str | None,
    ) -> str: ...

    def after_call(
        self,
        *,
        task_name: str,
        model_name: str,
        parsed: BaseModel,
        trace_id: str | None,
    ) -> BaseModel: ...


class LLMClient(Protocol):
    """Structural protocol satisfied by GeminiClient (live) and FakeLLMClient (tests)."""

    config: LLMConfig

    def generate_structured(
        self,
        *,
        prompt: str,
        system_prompt: str,
        schema: type[T],
        task_name: str,
        trace_id: str | None = None,
    ) -> T: ...


def apply_before_interceptors(
    interceptors: Sequence[LLMInterceptor],
    *,
    task_name: str,
    model_name: str,
    prompt: str,
    trace_id: str | None,
) -> str:
    """Run all registered before_call hooks in order."""
    current = prompt
    for hook in interceptors:
        current = hook.before_call(
            task_name=task_name,
            model_name=model_name,
            prompt=current,
            trace_id=trace_id,
        )
    return current


def apply_after_interceptors(
    interceptors: Sequence[LLMInterceptor],
    *,
    task_name: str,
    model_name: str,
    parsed: T,
    trace_id: str | None,
) -> T:
    """Run all registered after_call hooks in order."""
    current: BaseModel = parsed
    for hook in interceptors:
        current = hook.after_call(
            task_name=task_name,
            model_name=model_name,
            parsed=current,
            trace_id=trace_id,
        )
    return current  # type: ignore[return-value]


def build_default_llm_client(
    *,
    interceptors: Sequence[LLMInterceptor] = (),
) -> LLMClient | None:
    """Instantiate GeminiClient when GEMINI_API_KEY is available, else None."""
    if (_read_env_or_dotenv("COVENANT_DISABLE_LLM") or "").strip().lower() in {"1", "true", "yes"}:
        return None
    api_key = _read_env_or_dotenv("GEMINI_API_KEY") or _read_env_or_dotenv("GOOGLE_API_KEY")
    if not api_key:
        return None
    from covenant_ai.llm.gemini import GeminiClient

    return GeminiClient(
        api_key=api_key,
        config=LLMConfig.from_env(),
        interceptors=interceptors,
    )
