"""Bounded text operations for model backends. Never execute repository code."""
import os
from pathlib import Path, PurePosixPath, PureWindowsPath
from stat import FILE_ATTRIBUTE_REPARSE_POINT


BLOCKED = {".git", ".claude", ".codex", ".agents", ".mcp.json", ".env"}
MAX_FILE_BYTES = 256_000
MAX_RESULT_CHARS = 24_000


class RepositoryTools:
    def __init__(self, root):
        self.root = Path(root).resolve(strict=True)

    def path(self, name):
        if (not isinstance(name, str) or not name or "\\" in name or ":" in name
                or any(ord(c) < 32 for c in name)):
            raise ValueError("Use a repository-relative POSIX path")
        relative = PurePosixPath(name)
        if relative.is_absolute() or ".." in relative.parts:
            raise ValueError("Path must stay inside the repository")
        current = self.root
        for part in relative.parts:
            if part.endswith((".", " ")) or PureWindowsPath(part).is_reserved():
                raise ValueError("Ambiguous Windows filenames and device paths are unavailable")
            if part.lower() in BLOCKED or part.lower().startswith(".env.") and not part.lower().endswith(".example"):
                raise ValueError("Repository metadata and credential files are unavailable")
            current /= part
            reparse = current.exists() and getattr(current.lstat(), "st_file_attributes", 0) & FILE_ATTRIBUTE_REPARSE_POINT
            if current.is_symlink() or reparse:
                raise ValueError("Symlinks and junctions are unavailable")
        if not current.resolve().is_relative_to(self.root):
            raise ValueError("Path must stay inside the repository")
        return current

    def files(self, directory):
        # os.walk does not follow links; prune private/configuration trees before descending.
        for folder, dirs, files in os.walk(directory, followlinks=False):
            dirs[:] = sorted(d for d in dirs if self.allowed(Path(folder, d)))
            for filename in sorted(files):
                path = Path(folder, filename)
                if self.allowed(path):
                    yield path

    def allowed(self, path):
        try:
            self.path(path.relative_to(self.root).as_posix())
            return True
        except (ValueError, OSError):
            return False

    def call(self, tool, path=".", text="", start_line=1):
        if type(start_line) is not int or not 1 <= start_line <= 10_000_000:
            raise ValueError("start_line must be a positive integer")
        target = self.path(path)
        if tool == "list_files":
            if not target.is_dir():
                raise ValueError("list_files needs a directory")
            names = []
            for index, item in enumerate(self.files(target), 1):
                if index >= start_line:
                    names.append(item.relative_to(self.root).as_posix())
                if len(names) == 300:
                    return {"files": names, "next_start_line": index + 1}
            return {"files": names, "next_start_line": None}
        if tool == "read_file":
            if target.stat().st_size > MAX_FILE_BYTES:
                raise ValueError("File exceeds the 256 KB text-tool limit")
            lines = target.read_text(encoding="utf-8").splitlines(keepends=True)
            result = ""
            next_line = start_line
            for line in lines[start_line - 1:start_line + 399]:
                if len(result) + len(line) > MAX_RESULT_CHARS:
                    if not result:
                        raise ValueError("Single line exceeds the text-tool limit")
                    break
                result += line
                next_line += 1
            return {"text": result, "next_start_line": next_line if next_line <= len(lines) else None}
        if tool == "search_files":
            if not text or len(text) > 500:
                raise ValueError("Search text must contain 1-500 characters; matching is literal")
            matches, scanned = [], 0
            for item in self.files(target) if target.is_dir() else [target]:
                scanned += 1
                if scanned > 2000:
                    return {"matches": matches, "truncated": True, "hint": "Search a narrower path"}
                if item.stat().st_size > MAX_FILE_BYTES:
                    continue
                try:
                    lines = item.read_text(encoding="utf-8").splitlines()
                except (UnicodeError, OSError):
                    continue
                for number, line in enumerate(lines, 1):
                    if number >= start_line and text.casefold() in line.casefold():
                        matches.append({"path": item.relative_to(self.root).as_posix(), "line": number, "text": line[:300]})
                        if len(matches) == 50:
                            return {"matches": matches, "truncated": True, "hint": "Search a narrower path"}
            return {"matches": matches, "truncated": False}
        if tool == "write_file":
            if not isinstance(text, str) or "\x00" in text or len(text.encode("utf-8")) > MAX_FILE_BYTES:
                raise ValueError("Writes must be UTF-8 text of at most 256 KB")
            if target == self.root or target.exists() and not target.is_file():
                raise ValueError("write_file needs a file path")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8", newline="")
            return {"written": path}
        if tool == "delete_file":
            if not target.is_file():
                raise ValueError("delete_file needs an existing file")
            target.unlink()
            return {"deleted": path}
        raise ValueError("Unsupported repository tool")
