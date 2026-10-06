"""The character-dialogue prompt, read from prompts/character_dialogue.md (the README's prompt)."""

import re
from dataclasses import dataclass
from pathlib import Path

PROMPT_FILE = Path(__file__).resolve().parents[2] / "prompts" / "character_dialogue.md"


def _code_block_under(markdown: str, heading: str) -> str:
    match = re.search(rf"^## {re.escape(heading)}\s*\n+```text\n(.*?)\n```", markdown, re.DOTALL | re.MULTILINE)
    if match is None:
        raise ValueError(f"no ```text block under '## {heading}' in the prompt file")
    return match.group(1).strip()


@dataclass(frozen=True)
class DialoguePrompt:
    system: str
    user_template: str

    @classmethod
    def load(cls, path: Path = PROMPT_FILE) -> "DialoguePrompt":
        markdown = path.read_text()
        return cls(system=_code_block_under(markdown, "System prompt"), user_template=_code_block_under(markdown, "User prompt"))

    def render(self, character_name: str, ai_prompt: str) -> str:
        return self.user_template.replace("{{CHAR_NAME}}", character_name).replace("{{ai_prompt}}", ai_prompt)
