import asyncio
import aiofiles
import yaml
from pathlib import Path
from typing import List, Dict, Optional


def _sync_find_skill_files(search_paths: List[Path], skill_filenames: List[str]) -> List[Path]:
    """Performs the entire file walk synchronously inside a single thread block to eliminate micro-op context switching."""
    discovered = []
    for path in search_paths:
        if not path.exists() or not path.is_dir():
            continue
        try:
            for child in path.iterdir():
                if child.is_dir():
                    for filename in skill_filenames:
                        skill_file = child / filename
                        if skill_file.is_file():
                            discovered.append(skill_file)
        except Exception:
            continue
    return discovered


class SkillManager:
    """
    Manages custom skills discovery and metadata extraction natively asynchronously.
    """
    def __init__(self, search_paths: List[Path], skill_filenames: List[str]):
        """Strictly requires all paths and file names to be injected."""
        self.search_paths = [Path(p) for p in search_paths]
        self.skill_filenames = skill_filenames
        self._discovered_cache: Optional[List[Dict[str, str]]] = None

    async def discover_skills(self) -> List[Dict[str, str]]:
        """
        Scans all registered search paths asynchronously for skill files.
        Caches discovered skills in memory after the first crawl to eliminate redundant disk scans.
        """
        if self._discovered_cache is not None:
            return self._discovered_cache

        # Offload walk block to a single thread call
        skill_files = await asyncio.to_thread(
            _sync_find_skill_files,
            self.search_paths,
            self.skill_filenames
        )

        discovered = []
        for filepath in skill_files:
            meta = await self._load_skill_metadata(filepath)
            if meta:
                discovered.append(meta)
        self._discovered_cache = discovered
        return discovered

    async def _load_skill_metadata(self, filepath: Path) -> Optional[Dict[str, str]]:
        """
        Asynchronously reads skill file, parses YAML frontmatter, and extracts details.
        """
        try:
            async with aiofiles.open(filepath, mode="r", encoding="utf-8") as f:
                content = await f.read()
        except Exception:
            return None

        lines = content.splitlines()
        if not lines or not lines[0].strip() == "---":
            return None

        frontmatter_lines = []
        body_start = -1
        for i, line in enumerate(lines[1:], start=1):
            if line.strip() == "---":
                body_start = i + 1
                break
            frontmatter_lines.append(line)

        if body_start == -1:
            return None

        try:
            yaml_content = "\n".join(frontmatter_lines)
            meta = yaml.safe_load(yaml_content) or {}
        except Exception:
            return None

        name = meta.get("name")
        desc = meta.get("description")
        if not name or not desc:
            return None

        return {
            "name": str(name),
            "description": str(desc),
            "location": str(filepath),
            "instructions": "\n".join(lines[body_start:])
        }

