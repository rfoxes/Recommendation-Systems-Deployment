"""One small adapter per LLM provider, behind a common interface. LLM_PROVIDER picks which one runs.

Each SDK is created with retries off: background generation is retried by Temporal (with backoff),
and the live serve path wants a fast failure so it can use fallback copy instead.
"""

import asyncio
from typing import Protocol

from app.config import Settings

DEFAULT_MODELS = {
    # Flash-Lite: one-line copy doesn't need more, and its free tier allows 500 requests/day and 15/minute
    # (vs 20/day and 5/minute for Flash), measured with `gcloud beta quotas info list`.
    "gemini": "gemini-3.5-flash-lite",
    "anthropic": "claude-opus-5-5",
    "openai": "gpt-5.4-mini",
}
MAX_COPY_CHARS = 280


class CopyGenerationError(Exception):
    def __init__(self, message: str, *, retryable: bool) -> None:
        super().__init__(message)
        self.retryable = retryable


class CopyWriter(Protocol):
    provider: str
    model: str

    async def write(self, system: str, user: str, *, timeout: float) -> str:
        """Raw model output; CopyGenerator cleans it up the same way for every provider."""
        ...


def _retryable(status: int | None) -> bool:
    """Rate limits, timeouts and server errors are worth retrying; other client errors are not."""
    return status is None or status in (408, 409, 429) or status >= 500


def clean_line(text: str) -> str:
    """Keep the first non-empty line, without wrapping quotes; reject empty output."""
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    if len(line) >= 2 and line[0] == line[-1] and line[0] in "\"'“”":
        line = line[1:-1].strip()
    line = line.strip("“”").strip()
    if not line:
        raise CopyGenerationError("model returned no text", retryable=True)
    return line[:MAX_COPY_CHARS]


class GeminiWriter:
    provider = "gemini"

    def __init__(self, api_key: str, model: str) -> None:
        from google import genai

        self.model = model
        self._client = genai.Client(api_key=api_key)

    async def write(self, system: str, user: str, *, timeout: float) -> str:
        from google.genai import errors, types

        config = types.GenerateContentConfig(
            system_instruction=system,
            temperature=1.0,
            max_output_tokens=1024,
            automatic_function_calling=types.AutomaticFunctionCallingConfig(disable=True),  # no tools here
        )
        if self.model.startswith("gemini-3"):  # Gemini 3 models take a thinking level; keep it low for one line
            config.thinking_config = types.ThinkingConfig(thinking_level=types.ThinkingLevel.LOW)
        try:
            async with asyncio.timeout(timeout):
                response = await self._client.aio.models.generate_content(model=self.model, contents=user, config=config)
        except errors.APIError as exc:
            raise CopyGenerationError(f"gemini: {exc}", retryable=_retryable(exc.code)) from exc
        except TimeoutError as exc:
            raise CopyGenerationError("gemini: timed out", retryable=True) from exc
        return response.text or ""


class AnthropicWriter:
    provider = "anthropic"
    # Models that accept output_config.effort and the server-side refusal fallback.
    _CURRENT_MODELS = ("claude-opus-5-5", "claude-opus-5", "claude-sonnet-5-5", "claude-fable-5-1")

    def __init__(self, api_key: str, model: str) -> None:
        import anthropic

        self.model = model
        self._client = anthropic.AsyncAnthropic(api_key=api_key, max_retries=0)

    async def write(self, system: str, user: str, *, timeout: float) -> str:
        import anthropic

        options: dict[str, object] = {}
        if self.model in self._CURRENT_MODELS:
            # Low effort suits a one-line answer; on a safety decline the API retries on a fallback model.
            options = {
                "output_config": {"effort": "low"},
                "betas": ["server-side-fallback-2026-07-01"],
                "fallbacks": "default",
            }
        try:
            async with asyncio.timeout(timeout):
                response = await self._client.beta.messages.create(
                    model=self.model,
                    max_tokens=4096,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    **options,  # type: ignore[arg-type]
                )
        except anthropic.APIStatusError as exc:
            raise CopyGenerationError(f"anthropic: {exc.message}", retryable=_retryable(exc.status_code)) from exc
        except (anthropic.APIConnectionError, TimeoutError) as exc:
            raise CopyGenerationError(f"anthropic: {exc}", retryable=True) from exc
        if response.stop_reason == "refusal":
            raise CopyGenerationError("anthropic: model declined the request", retryable=False)
        return "".join(block.text for block in response.content if block.type == "text")


class OpenAIWriter:
    provider = "openai"

    def __init__(self, api_key: str, model: str) -> None:
        import openai

        self.model = model
        self._client = openai.AsyncOpenAI(api_key=api_key, max_retries=0)

    async def write(self, system: str, user: str, *, timeout: float) -> str:
        import openai

        options: dict[str, object] = {}
        if self.model.startswith(("gpt-5", "o")):  # reasoning models: keep reasoning minimal for one line
            options["reasoning"] = {"effort": "low"}
        try:
            async with asyncio.timeout(timeout):
                response = await self._client.responses.create(
                    model=self.model, instructions=system, input=user, max_output_tokens=1024, **options  # type: ignore[arg-type]
                )
        except openai.APIStatusError as exc:
            raise CopyGenerationError(f"openai: {exc.message}", retryable=_retryable(exc.status_code)) from exc
        except (openai.APIConnectionError, TimeoutError) as exc:
            raise CopyGenerationError(f"openai: {exc}", retryable=True) from exc
        return response.output_text


def create_copy_writer(settings: Settings) -> CopyWriter | None:
    """The configured provider's writer, or None when its API key isn't set (ads then use fallback copy)."""
    keys = {
        "gemini": settings.gemini_api_key,
        "anthropic": settings.anthropic_api_key,
        "openai": settings.openai_api_key,
    }
    key = keys[settings.llm_provider]
    if key is None:
        return None
    model = settings.llm_model or DEFAULT_MODELS[settings.llm_provider]
    writers: dict[str, type[GeminiWriter | AnthropicWriter | OpenAIWriter]] = {
        "gemini": GeminiWriter,
        "anthropic": AnthropicWriter,
        "openai": OpenAIWriter,
    }
    return writers[settings.llm_provider](key.get_secret_value(), model)
