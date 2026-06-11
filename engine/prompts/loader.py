import asyncio
from pathlib import Path
from typing import Any, Dict


def _sync_read_template(filepath: Path) -> str:
    if not filepath.exists():
        raise FileNotFoundError(f"Template file not found: {filepath}")
    with open(filepath, mode="r", encoding="utf-8") as f:
        return f.read()


class PromptTemplateLoader:
    """
    Natively asynchronous template loader that reads modular markdown snippets from disk
    using non-blocking offloaded I/O and compiles final prompt instructions.
    Implements high-performance in-memory caching of static system prompts to avoid repeated disk reads.
    """
    def __init__(self, templates_dir: Path):
        """Strictly requires templates_dir to be injected with no inlined fallbacks."""
        self.templates_dir = Path(templates_dir)
        self._cache: Dict[str, str] = {}

    async def load_template(self, filename: str) -> str:
        """
        Asynchronously loads a template from disk with static caching.
        """
        if filename in self._cache:
            return self._cache[filename]

        filepath = self.templates_dir / filename
        content = await asyncio.to_thread(_sync_read_template, filepath)
        self._cache[filename] = content
        return content

    async def compile_prompt(self, template_name: str, variables: Dict[str, Any]) -> str:
        """
        Asynchronously loads a template and replaces double-brace placeholder variables.
        """
        content = await self.load_template(template_name)
        for key, val in variables.items():
            placeholder = f"{{{{{key}}}}}"
            content = content.replace(placeholder, str(val))
        return content

