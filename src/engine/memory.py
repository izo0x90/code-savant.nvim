import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Set
import aiofiles


class HierarchicalContextManager:
    """
    Manages the cascading discovery of global and project/workspace memory files,
    excluding regex code blocks, and recursively resolving '@' file mentions.
    Does not have any hardcoded fallback paths or target file names.
    """

    def __init__(
        self,
        workspace_path: Path,
        context_filenames: List[str],
        global_context_dir: Path,
        max_depth: int
    ):
        self.workspace_path = Path(workspace_path).resolve()
        self.context_filenames = context_filenames
        self.global_context_dir = Path(global_context_dir).resolve()
        self.max_depth = max_depth

    def _find_project_root(self, start_dir: Path, boundary_markers: List[str]) -> Path:
        current = start_dir.resolve()
        while True:
            for marker in boundary_markers:
                if (current / marker).exists():
                    return current
            parent = current.parent
            if parent == current:
                break
            current = parent
        return start_dir.resolve()

    def _find_code_regions(self, content: str) -> List[tuple[int, int]]:
        regions = []
        for m in re.finditer(r"(`+)([\s\S]*?)\1", content):
            regions.append((m.start(), m.end()))
        return regions

    def _find_imports(self, content: str) -> List[Dict[str, Any]]:
        imports = []
        i = 0
        length = len(content)
        while i < length:
            i = content.find("@", i)
            if i == -1:
                break
            if i > 0 and content[i - 1] not in (" ", "\t", "\n", "\r"):
                i += 1
                continue
            j = i + 1
            while j < length and content[j] not in (" ", "\t", "\n", "\r"):
                j += 1
            import_path = content[i + 1:j]
            if import_path and (import_path[0] in (".", "/") or import_path[0].isalpha()):
                imports.append({
                    "start": i,
                    "end": j,
                    "path": import_path
                })
            i = j + 1
        return imports

    def _is_subpath(self, parent: Path, child: Path) -> bool:
        try:
            parent = parent.resolve()
            child = child.resolve()
            return parent == child or parent in child.parents
        except Exception:
            return False

    def _validate_import_path(self, import_path: str, base_path: Path, allowed_directories: List[Path]) -> bool:
        if import_path.startswith(("file://", "http://", "https://")):
            return False
        try:
            resolved_path = (base_path / import_path).resolve()
            return any(self._is_subpath(allowed, resolved_path) for allowed in allowed_directories)
        except Exception:
            return False

    async def process_imports(
        self,
        content: str,
        base_path: Path,
        project_root: Path,
        processed_files: Optional[Set[Path]] = None,
        current_depth: int = 0
    ) -> str:
        if processed_files is None:
            processed_files = set()

        if current_depth >= self.max_depth:
            return content

        code_regions = self._find_code_regions(content)
        imports_list = self._find_imports(content)
        if not imports_list:
            return content

        result = []
        last_index = 0

        for imp in imports_list:
            start = imp["start"]
            end = imp["end"]
            import_path = imp["path"]

            result.append(content[last_index:start])
            last_index = end

            in_code = any(region_start <= start < region_end for region_start, region_end in code_regions)
            if in_code:
                result.append(f"@{import_path}")
                continue

            if not self._validate_import_path(import_path, base_path, [project_root]):
                result.append(f"<!-- Import failed: {import_path} - Path traversal attempt -->")
                continue

            full_path = (base_path / import_path).resolve()
            if full_path in processed_files:
                result.append(f"<!-- File already processed: {import_path} -->")
                continue

            try:
                if not full_path.is_file():
                    result.append(f"<!-- Import failed: {import_path} - File not found -->")
                    continue

                async with aiofiles.open(full_path, "r", encoding="utf-8") as f:
                    file_content = await f.read()

                new_processed = set(processed_files)
                new_processed.add(full_path)

                imported_content = await self.process_imports(
                    content=file_content,
                    base_path=full_path.parent,
                    project_root=project_root,
                    processed_files=new_processed,
                    current_depth=current_depth + 1
                )

                result.append(
                    f"<!-- Imported from: {import_path} -->\n"
                    f"{imported_content}\n"
                    f"<!-- End of import from: {import_path} -->"
                )
            except Exception as e:
                result.append(f"<!-- Import failed: {import_path} - {str(e)} -->")

        result.append(content[last_index:])
        return "".join(result)

    async def load_hierarchical_context(self) -> str:
        """
        Discovers, loads, and formats hierarchical context files (e.g. GEMINI.md)
        from global and workspace pathways, inlining imports.
        """
        # 1. Global context paths
        global_files = []
        for f in self.context_filenames:
            gp = self.global_context_dir / f
            if gp.exists() and gp.is_file():
                global_files.append(gp)

        # 2. Project/Workspace context paths
        project_files = []
        for f in self.context_filenames:
            wp = self.workspace_path / f
            if wp.exists() and wp.is_file():
                project_files.append(wp)

        # Read global files
        global_blocks = []
        for fp in global_files:
            try:
                async with aiofiles.open(fp, "r", encoding="utf-8") as f:
                    content = await f.read()
                global_root = self._find_project_root(fp.parent, [".git"])
                processed_content = await self.process_imports(content, fp.parent, global_root)
                trimmed = processed_content.strip()
                if trimmed:
                    global_blocks.append(f"--- Context from: {fp.as_posix()} ---\n{trimmed}\n--- End of Context from: {fp.as_posix()} ---")
            except Exception:
                pass

        # Read project files
        project_blocks = []
        for fp in project_files:
            try:
                async with aiofiles.open(fp, "r", encoding="utf-8") as f:
                    content = await f.read()
                project_root = self._find_project_root(fp.parent, [".git"])
                processed_content = await self.process_imports(content, fp.parent, project_root)
                trimmed = processed_content.strip()
                if trimmed:
                    project_blocks.append(f"--- Context from: {fp.as_posix()} ---\n{trimmed}\n--- End of Context from: {fp.as_posix()} ---")
            except Exception:
                pass

        # Construct the loaded context XML blocks
        sections = []
        if global_blocks:
            joined_globals = "\n".join(global_blocks)
            sections.append(f"<global_context>\n{joined_globals}\n</global_context>")
        if project_blocks:
            joined_projects = "\n".join(project_blocks)
            sections.append(f"<project_context>\n{joined_projects}\n</project_context>")

        if not sections:
            return ""

        return "\n\n".join(sections)
