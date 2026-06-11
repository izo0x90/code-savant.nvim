import os
import re
import fnmatch
import asyncio
import aiofiles
from pathlib import Path
from typing import Any, Dict, List, Optional, Union
from pydantic import BaseModel, Field, ValidationError, JsonValue

from engine.types import ExecutionContext


class BaseTool:
    """
    Interface matching legacy BaseDeclarativeTool.
    Declares self-describing parameters and supports schema validation.
    """

    def __init__(self, name: str, description: str, args_schema: Optional[type[BaseModel]] = None):
        self.name = name
        self.description = description
        self.args_schema = args_schema

        # Pre-compile and cache schema for high compile-time performance
        schema = {}
        if self.args_schema:
            schema = self.args_schema.model_json_schema()
            schema.pop("title", None)
            if "properties" in schema:
                for prop in schema["properties"].values():
                    prop.pop("title", None)
        self._cached_parameters = schema or {"type": "object", "properties": {}}

    def resolve_path(self, path_str: str, workspace_path: Path) -> Path:
        """Resolves absolute or relative paths within the active workspace."""
        path_obj = Path(path_str)
        if path_obj.is_absolute():
            return path_obj.resolve()
        return (workspace_path / path_obj).resolve()

    def get_declaration(self) -> Dict[str, Any]:
        """Returns tool schema formatted as a standard tool declaration function call."""
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self._cached_parameters,
        }

    async def execute(self, args: Dict[str, Any], context: ExecutionContext) -> Dict[str, JsonValue]:
        """Runs the validation and business logic asynchronously (supporting sync or async run methods)."""
        if self.args_schema:
            try:
                validated_args = self.args_schema(**args)
                if asyncio.iscoroutinefunction(self.run):
                    return await self.run(validated_args, context)
                return await asyncio.to_thread(self.run, validated_args, context)
            except ValidationError as e:
                return {"error": f"Schema validation failed: {e.errors()}"}
        if asyncio.iscoroutinefunction(self.run):
            return await self.run(args, context)
        return await asyncio.to_thread(self.run, args, context)

    async def run(self, args: Any, context: ExecutionContext) -> Dict[str, JsonValue]:
        raise NotImplementedError


def load_gitignore_patterns(workspace_path: Path) -> List[str]:
    """Loads and returns the list of active patterns from .gitignore in the workspace."""
    patterns = []
    gitignore_path = workspace_path / ".gitignore"
    if gitignore_path.exists():
        try:
            with gitignore_path.open("r", encoding="utf-8", errors="ignore") as f:
                for line in f:
                    line = line.strip()
                    if not line or line.startswith("#"):
                        continue
                    patterns.append(line)
        except Exception:
            pass
    return patterns


def should_ignore_path(
    path: Path,
    workspace_path: Path,
    ignore_patterns: Optional[List[str]] = None,
    respect_git: bool = True,
    git_patterns: Optional[List[str]] = None
) -> bool:
    """Checks if a path matches standard system ignores or custom ignore arrays."""
    try:
        rel_path = str(path.resolve().relative_to(workspace_path.resolve()))
    except ValueError:
        rel_path = str(path)

    # Common system directories to always skip
    default_ignores = {".git", "node_modules", ".venv", "venv", "__pycache__", ".DS_Store"}
    parts = Path(rel_path).parts
    for p in parts:
        if p in default_ignores:
            return True

    # User supplied ignore patterns
    if ignore_patterns:
        for pat in ignore_patterns:
            if fnmatch.fnmatch(rel_path, pat) or fnmatch.fnmatch(path.name, pat):
                return True

    # Parse and respect .gitignore if requested
    if respect_git:
        if git_patterns is not None:
            for line in git_patterns:
                if fnmatch.fnmatch(rel_path, line) or fnmatch.fnmatch(path.name, line):
                    return True
        else:
            gitignore_path = workspace_path / ".gitignore"
            if gitignore_path.exists():
                try:
                    with gitignore_path.open("r", encoding="utf-8", errors="ignore") as f:
                        for line in f:
                            line = line.strip()
                            if not line or line.startswith("#"):
                                continue
                            if fnmatch.fnmatch(rel_path, line) or fnmatch.fnmatch(path.name, line):
                                return True
                except Exception:
                    pass

    return False


# ==============================================================================
# Complete Task Tool Implementation
# ==============================================================================

class CompleteTaskArgs(BaseModel):
    result: str = Field(..., description="Your final findings or response to submit. Follow any required formatting instructions.")


class CompleteTaskTool(BaseTool):
    """
    Finalizer tool. Injected automatically.
    """

    def __init__(self):
        super().__init__(
            name="complete_task",
            description="Call this tool to submit your final findings and complete the task. This is the ONLY way to finish.",
            args_schema=CompleteTaskArgs,
        )

    async def run(self, args: CompleteTaskArgs, context: ExecutionContext) -> Dict[str, JsonValue]:
        return {
            "taskCompleted": True,
            "submittedOutput": args.result,
            "llmContent": "Result submitted and task completed."
        }


# ==============================================================================
# High-Fidelity Read File Tool
# ==============================================================================

class ReadFileArgs(BaseModel):
    file_path: str = Field(..., description="The path to the file to read.")
    start_line: Optional[int] = Field(None, description="Optional: The 1-based line number to start reading from.")
    end_line: Optional[int] = Field(None, description="Optional: The 1-based line number to end reading at (inclusive).")


class ReadFileTool(BaseTool):
    def __init__(self):
        super().__init__(
            name="read_file",
            description="Reads and returns the content of a specified file. Handles text, source code files, and lists partial truncated contents with lines offset metadata.",
            args_schema=ReadFileArgs,
        )

    async def run(self, args: ReadFileArgs, context: ExecutionContext) -> Dict[str, JsonValue]:
        fp = self.resolve_path(args.file_path, context.workspace_path)
        if not fp.exists():
            return {"error": f"File does not exist: {args.file_path}"}
        if fp.is_dir():
            return {"error": f"Path is a directory, not a file: {args.file_path}"}

        try:
            async with aiofiles.open(fp, "r", encoding="utf-8", errors="replace") as f:
                lines = await f.readlines()
            
            total_lines = len(lines)
            
            # Setup limits and slicing
            start = args.start_line if args.start_line is not None else 1
            start = max(1, start)
            
            end = args.end_line if args.end_line is not None else total_lines
            end = min(total_lines, end)
            
            if start > total_lines:
                return {
                    "success": True,
                    "content": "",
                    "start_line": start,
                    "end_line": start,
                    "total_lines": total_lines,
                    "truncated": False
                }

            slice_lines = lines[start - 1 : end]
            content = "".join(slice_lines)
            
            # Truncation warning if reading without ranges on a huge file
            is_truncated = (len(slice_lines) < total_lines)
            
            return {
                "success": True,
                "file_path": str(fp.relative_to(context.workspace_path)),
                "start_line": start,
                "end_line": end,
                "total_lines": total_lines,
                "truncated": is_truncated,
                "content": content
            }
        except Exception as e:
            return {"error": f"Failed to read file: {str(e)}"}


# ==============================================================================
# High-Fidelity Write File Tool
# ==============================================================================

class WriteFileArgs(BaseModel):
    file_path: str = Field(..., description="The path to the file to write to.")
    content: str = Field(..., description="The exact string content to write.")


def _sync_write_file(filepath: Path, content: str) -> None:
    """Cohesively creates parent directories and writes file content synchronously inside a single thread call."""
    filepath.parent.mkdir(parents=True, exist_ok=True)
    with open(filepath, "w", encoding="utf-8") as f:
        f.write(content)


class WriteFileTool(BaseTool):
    def __init__(self):
        super().__init__(
            name="write_file",
            description="Writes content to a specified file in the local filesystem, creating parents if missing.",
            args_schema=WriteFileArgs,
        )

    async def run(self, args: WriteFileArgs, context: ExecutionContext) -> Dict[str, JsonValue]:
        fp = self.resolve_path(args.file_path, context.workspace_path)
        try:
            # Run entire mkdir and file write operation in a single offloaded thread call
            await asyncio.to_thread(_sync_write_file, fp, args.content)
            
            return {
                "success": True,
                "file_path": str(fp.relative_to(context.workspace_path)),
                "message": f"Successfully wrote content to {fp.relative_to(context.workspace_path)}"
            }
        except Exception as e:
            return {"error": f"Failed to write file: {str(e)}"}



# ==============================================================================
# High-Fidelity List Directory Tool
# ==============================================================================

class FileFilteringOptions(BaseModel):
    respect_git_ignore: Optional[bool] = Field(True, description="Whether to respect .gitignore patterns when listing files.")
    respect_gemini_ignore: Optional[bool] = Field(True, description="Whether to respect .geminiignore patterns when listing files.")


class ListDirectoryArgs(BaseModel):
    dir_path: str = Field(..., description="The path to the directory to list")
    ignore: Optional[List[str]] = Field(None, description="List of glob patterns to ignore")
    file_filtering_options: Optional[FileFilteringOptions] = Field(None, description="Optional: Whether to respect ignore patterns from .gitignore or .geminiignore")


class ListDirectoryTool(BaseTool):
    def __init__(self):
        super().__init__(
            name="list_directory",
            description="Lists directories and files inside a targeted folder. Respects standard ignore lists and gitignore.",
            args_schema=ListDirectoryArgs,
        )

    async def run(self, args: ListDirectoryArgs, context: ExecutionContext) -> Dict[str, JsonValue]:
        dp = self.resolve_path(args.dir_path, context.workspace_path)
        if not dp.exists() or not dp.is_dir():
            return {"error": f"Directory does not exist: {args.dir_path}"}

        try:
            def _list():
                return list(dp.iterdir())
            items = await asyncio.to_thread(_list)
            details = []
            
            # Extract options
            respect_git = True
            if args.file_filtering_options:
                if args.file_filtering_options.respect_git_ignore is False:
                    respect_git = False
            
            git_patterns = load_gitignore_patterns(context.workspace_path) if respect_git else []

            for item in items:
                # Apply ignore checks
                if should_ignore_path(item, context.workspace_path, ignore_patterns=args.ignore, respect_git=respect_git, git_patterns=git_patterns):
                    continue
                
                details.append({
                    "name": item.name,
                    "is_dir": item.is_dir(),
                    "size_bytes": item.stat().st_size if item.is_file() else 0
                })
            
            # Sort items: directories first, then alphabetical name
            details.sort(key=lambda x: (not x["is_dir"], x["name"].lower()))
            
            return {
                "success": True,
                "dir_path": str(dp.relative_to(context.workspace_path)) if dp != context.workspace_path else ".",
                "items": details
            }
        except Exception as e:
            return {"error": f"Failed to list directory: {str(e)}"}


# ==============================================================================
# High-Fidelity Glob Tool
# ==============================================================================

class GlobArgs(BaseModel):
    pattern: str = Field(..., description="The glob pattern to match against (e.g., '**/*.py', 'docs/*.md').")
    dir_path: Optional[str] = Field(None, description="Optional: The absolute or relative path to the directory to search within.")
    case_sensitive: Optional[bool] = Field(False, description="Optional: Whether the search should be case-sensitive.")
    respect_git_ignore: Optional[bool] = Field(True, description="Optional: Whether to respect .gitignore patterns.")
    respect_gemini_ignore: Optional[bool] = Field(True, description="Optional: Whether to respect .geminiignore patterns.")


class GlobTool(BaseTool):
    def __init__(self):
        super().__init__(
            name="glob",
            description="Returns all files matching glob pattern inside the targeted directory, sorted by modification date.",
            args_schema=GlobArgs,
        )

    async def run(self, args: GlobArgs, context: ExecutionContext) -> Dict[str, JsonValue]:
        dp = self.resolve_path(args.dir_path, context.workspace_path) if args.dir_path else context.workspace_path
        if not dp.exists() or not dp.is_dir():
            return {"error": f"Search path does not exist or is not a directory: {args.dir_path or '.'}"}

        try:
            # We will use Path(dp).glob or rglob based on pattern
            def _glob_search():
                pattern_str = args.pattern
                if pattern_str.startswith("**/"):
                    return list(dp.rglob(pattern_str[3:]))
                else:
                    return list(dp.glob(pattern_str))
                    
            files = await asyncio.to_thread(_glob_search)
                
            respect_git = args.respect_git_ignore if args.respect_git_ignore is not None else True
            git_patterns = load_gitignore_patterns(context.workspace_path) if respect_git else []

            matches = []
            for file_path in files:
                if not file_path.is_file():
                    continue
                
                # Check ignores
                if should_ignore_path(file_path, context.workspace_path, respect_git=respect_git, git_patterns=git_patterns):
                    continue
                
                matches.append(file_path)
            
            # Sort matches by modification time (newest first)
            matches.sort(key=lambda p: p.stat().st_mtime, reverse=True)
            
            rel_matches = [str(m.resolve().relative_to(context.workspace_path.resolve())) for m in matches]
            
            return {
                "success": True,
                "pattern": args.pattern,
                "dir_path": str(dp.relative_to(context.workspace_path)) if dp != context.workspace_path else ".",
                "matches": rel_matches
            }
        except Exception as e:
            return {"error": f"Failed to execute glob search: {str(e)}"}


# ==============================================================================
# High-Fidelity Grep Search Tool
# ==============================================================================

class GrepSearchArgs(BaseModel):
    pattern: str = Field(..., description="The regular expression (regex) pattern to search for within file contents.")
    dir_path: Optional[str] = Field(None, description="Optional: The path to the directory to search within.")
    include_pattern: Optional[str] = Field(None, description="Optional: A glob pattern to filter which files are searched (e.g., '*.js').")
    exclude_pattern: Optional[str] = Field(None, description="Optional: A regex to exclude from the results.")
    names_only: Optional[bool] = Field(False, description="Optional: If true, only returning matching file paths.")
    max_matches_per_file: Optional[int] = Field(None, description="Optional: Maximum number of matches to return per file.")
    total_max_matches: Optional[int] = Field(100, description="Optional: Maximum number of total matches to return.")


class GrepSearchTool(BaseTool):
    def __init__(self):
        super().__init__(
            name="grep_search",
            description="Recursively searches for matching pattern lines inside a targeted directory, supporting exclude arrays.",
            args_schema=GrepSearchArgs,
        )

    async def run(self, args: GrepSearchArgs, context: ExecutionContext) -> Dict[str, JsonValue]:
        dp = self.resolve_path(args.dir_path, context.workspace_path) if args.dir_path else context.workspace_path
        if not dp.exists():
            return {"error": f"Search directory does not exist: {args.dir_path or '.'}"}

        try:
            # Run recursive search on a separate thread pool to completely prevent blocking the main event loop
            def _run_grep():
                search_regex = re.compile(args.pattern, re.IGNORECASE)
                exclude_regex = re.compile(args.exclude_pattern) if args.exclude_pattern else None
                
                total_limit = args.total_max_matches if args.total_max_matches is not None else 100
                file_limit = args.max_matches_per_file
                
                git_patterns = load_gitignore_patterns(context.workspace_path)
                all_matches = []
                
                def recurse_dir(current_dir: Path):
                    if current_dir != context.workspace_path and should_ignore_path(current_dir, context.workspace_path, respect_git=True, git_patterns=git_patterns):
                        return
                    
                    try:
                        for item in current_dir.iterdir():
                            if item.is_dir():
                                recurse_dir(item)
                            elif item.is_file():
                                if should_ignore_path(item, context.workspace_path, respect_git=True, git_patterns=git_patterns):
                                    continue
                                
                                # Filter by include_pattern if specified
                                if args.include_pattern:
                                    if not fnmatch.fnmatch(item.name, args.include_pattern):
                                        continue
                                
                                # Open and search
                                try:
                                    matches_in_file = 0
                                    with item.open("r", encoding="utf-8", errors="ignore") as f:
                                        for idx, line in enumerate(f, 1):
                                            if search_regex.search(line):
                                                if exclude_regex and exclude_regex.search(line):
                                                    continue
                                                
                                                rel_fp = str(item.resolve().relative_to(context.workspace_path.resolve()))
                                                
                                                if args.names_only:
                                                    if not any(m.get("file_path") == rel_fp for m in all_matches):
                                                        all_matches.append({
                                                            "file_path": rel_fp
                                                        })
                                                        matches_in_file += 1
                                                else:
                                                    all_matches.append({
                                                        "file_path": rel_fp,
                                                        "line_number": idx,
                                                        "content": line.rstrip()
                                                    })
                                                    matches_in_file += 1
                                                
                                                # Check limits
                                                if total_limit and len(all_matches) >= total_limit:
                                                    break
                                                if file_limit and matches_in_file >= file_limit:
                                                    break
                                    if total_limit and len(all_matches) >= total_limit:
                                        break
                                except Exception:
                                    continue
                    except Exception:
                        pass

                recurse_dir(dp)
                return all_matches

            matches = await asyncio.to_thread(_run_grep)
            
            return {
                "success": True,
                "pattern": args.pattern,
                "matches": matches
            }
        except Exception as e:
            return {"error": f"Failed during grep search: {str(e)}"}


# ==============================================================================
# High-Fidelity Replace (Code Editor) Tool
# ==============================================================================

class ReplaceArgs(BaseModel):
    file_path: str = Field(..., description="The path to the file to modify.")
    instruction: str = Field(..., description="Semantic description of the change.")
    old_string: str = Field(..., description="Exact literal text to find.")
    new_string: str = Field(..., description="Exact literal text to replace old_string with.")
    allow_multiple: Optional[bool] = Field(False, description="If true, replaces all occurrences. Otherwise, fails if matched multiple times.")


class ReplaceTool(BaseTool):
    def __init__(self):
        super().__init__(
            name="replace",
            description="Replaces text within a specified file. Requires exact old_string match. Fails if target is ambiguous or not found.",
            args_schema=ReplaceArgs,
        )

    async def run(self, args: ReplaceArgs, context: ExecutionContext) -> Dict[str, JsonValue]:
        fp = self.resolve_path(args.file_path, context.workspace_path)
        if not fp.exists():
            return {"error": f"File does not exist: {args.file_path}"}
        if fp.is_dir():
            return {"error": f"Path is a directory: {args.file_path}"}

        try:
            # Use aiofiles for reading and writing to keep it strictly non-blocking!
            async with aiofiles.open(fp, "r", encoding="utf-8", errors="replace") as f:
                content = await f.read()
            
            occurrences = content.count(args.old_string)
            
            if occurrences == 0:
                return {
                    "error": "The specified 'old_string' was not found in the file. Ensure indentation, spacing, and characters match exactly."
                }
            
            if occurrences > 1 and not args.allow_multiple:
                return {
                    "error": f"The specified 'old_string' was found {occurrences} times. It is ambiguous. Provide more context lines before/after to narrow it down to a single location."
                }
            
            # Execute replacement
            updated_content = content.replace(args.old_string, args.new_string)
            
            async with aiofiles.open(fp, "w", encoding="utf-8") as f:
                await f.write(updated_content)
                
            return {
                "success": True,
                "file_path": str(fp.relative_to(context.workspace_path)),
                "occurrences_replaced": occurrences,
                "message": f"Successfully replaced {occurrences} occurrence(s) in {fp.relative_to(context.workspace_path)}."
            }
        except Exception as e:
            return {"error": f"Failed to perform replace operation: {str(e)}"}
