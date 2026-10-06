import pytest

from app.config import Settings
from app.copywriting.generator import CopyGenerator
from app.copywriting.prompt import DialoguePrompt
from app.copywriting.writers import CopyGenerationError, clean_line, create_copy_writer

pytestmark = pytest.mark.anyio


class FakeWriter:
    provider, model = "fake", "fake-1"

    def __init__(self, replies: list[str | Exception]) -> None:
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    async def write(self, system: str, user: str, *, timeout: float) -> str:
        self.calls.append((system, user))
        reply = self.replies.pop(0)
        if isinstance(reply, Exception):
            raise reply
        return reply


def test_prompt_is_the_readme_prompt() -> None:
    prompt = DialoguePrompt.load()
    assert prompt.system.startswith("You write one line of in-character dialogue")
    rendered = prompt.render("Luna", "Tell a friend about the bonus.")
    assert "Character name: Luna" in rendered and "Specific instructions: Tell a friend about the bonus." in rendered
    assert "{{" not in rendered


@pytest.mark.parametrize(
    ("raw", "line"),
    [
        ('"Come find me."', "Come find me."),
        ("“Curly quotes.”", "Curly quotes."),
        ("\n\n  First line.\nSecond line.", "First line."),
    ],
)
def test_clean_line(raw: str, line: str) -> None:
    assert clean_line(raw) == line


def test_empty_output_is_a_retryable_error() -> None:
    with pytest.raises(CopyGenerationError) as err:
        clean_line("   \n ")
    assert err.value.retryable


async def test_generator_uses_the_prompt_and_dedupes() -> None:
    writer = FakeWriter(['"Same line."', "Same line.", "Another line."])
    lines = await CopyGenerator(writer, DialoguePrompt.load()).lines("Rex", "Hype the raid.", 3, timeout=5)
    assert lines == ["Same line.", "Another line."]
    assert all("Character name: Rex" in user for _, user in writer.calls)


async def test_partial_failures_keep_the_lines_that_worked() -> None:
    writer = FakeWriter(
        [CopyGenerationError("429", retryable=True), "Worked.", CopyGenerationError("503", retryable=True)]
    )
    assert await CopyGenerator(writer, DialoguePrompt.load()).lines("Rex", "x", 3, timeout=5) == ["Worked."]


async def test_total_failure_raises_and_reports_retryability() -> None:
    writer = FakeWriter([CopyGenerationError("bad key", retryable=False)] * 2)
    with pytest.raises(CopyGenerationError) as err:
        await CopyGenerator(writer, DialoguePrompt.load()).lines("Rex", "x", 2, timeout=5)
    assert not err.value.retryable


async def test_no_writer_means_no_llm() -> None:
    generator = CopyGenerator(None, DialoguePrompt.load())
    assert not generator.enabled
    with pytest.raises(CopyGenerationError):
        await generator.line("Rex", "x", timeout=1)


def test_provider_switch() -> None:
    assert create_copy_writer(Settings(_env_file=None, llm_provider="gemini")) is None  # no key -> fallback copy
    writer = create_copy_writer(Settings(_env_file=None, llm_provider="anthropic", anthropic_api_key="k"))
    assert writer is not None and (writer.provider, writer.model) == ("anthropic", "claude-opus-5-5")
    writer = create_copy_writer(Settings(_env_file=None, llm_provider="openai", openai_api_key="k", llm_model="gpt-x"))
    assert writer is not None and (writer.provider, writer.model) == ("openai", "gpt-x")
