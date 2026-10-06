import asyncio
import logging

from app.copywriting.prompt import DialoguePrompt
from app.copywriting.writers import CopyGenerationError, CopyWriter, clean_line

logger = logging.getLogger(__name__)


class CopyGenerator:
    """Writes a character's line from the README prompt, using whichever LLM is configured."""

    def __init__(self, writer: CopyWriter | None, prompt: DialoguePrompt) -> None:
        self._writer = writer
        self._prompt = prompt

    @property
    def enabled(self) -> bool:
        return self._writer is not None

    @property
    def provider(self) -> str | None:
        return self._writer.provider if self._writer else None

    @property
    def model(self) -> str | None:
        return self._writer.model if self._writer else None

    async def line(self, character_name: str, ai_prompt: str, *, timeout: float) -> str:
        if self._writer is None:
            raise CopyGenerationError("no LLM API key configured", retryable=False)
        raw = await self._writer.write(
            self._prompt.system, self._prompt.render(character_name, ai_prompt), timeout=timeout
        )
        return clean_line(raw)

    async def lines(self, character_name: str, ai_prompt: str, count: int, *, timeout: float) -> list[str]:
        """Up to `count` distinct lines (generated in parallel). Raises only if every attempt failed."""
        results = await asyncio.gather(
            *(self.line(character_name, ai_prompt, timeout=timeout) for _ in range(count)), return_exceptions=True
        )
        lines = list(dict.fromkeys(r for r in results if isinstance(r, str)))
        errors = [r for r in results if isinstance(r, BaseException)]
        if not lines and errors:
            first = errors[0]
            if isinstance(first, CopyGenerationError):
                raise CopyGenerationError(str(first), retryable=any(getattr(e, "retryable", True) for e in errors))
            raise first
        for error in errors:
            logger.warning("Some copy generations failed for %s: %s", character_name, error)
        return lines
