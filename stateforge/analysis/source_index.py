"""Source predicates indexed by file, lexical scope and source line.

Only files represented in the IR are parsed. A project root may contain virtual
environments or unrelated repositories; those are not part of guard recovery.
"""

from __future__ import annotations

import ast
from pathlib import Path

from stateforge.core.ir import ProgramIR


class SourceTestIndex:
    def __init__(self, ir: ProgramIR):
        self.by_line: dict[tuple[str, str, int], ast.expr] = {}
        self._aliases: dict[tuple[str, str, int], list[ast.expr]] = {}
        root = Path(ir.metadata.get("root", "."))
        self.root = root.resolve()
        files = {block.file for block in ir.blocks.values()}
        files.update(fn.file for fn in ir.functions.values())
        for filename in sorted(files):
            path = Path(filename)
            if not path.is_absolute() and not path.is_file():
                path = root / path
            try:
                tree = ast.parse(path.read_text(encoding="utf-8"))
            except (OSError, SyntaxError, UnicodeDecodeError):
                continue
            self._visit(tree, str(path.resolve()), ())

    def _visit(self, node: ast.AST, filename: str, scope: tuple[str, ...]):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            scope = (*scope, node.name)
        test = None
        if isinstance(node, (ast.If, ast.While)):
            test = node.test
        elif isinstance(node, (ast.For, ast.AsyncFor)):
            test = node.iter
        if test is not None and scope:
            self.by_line[(filename, ".".join(scope), test.lineno)] = test
            self._aliases.setdefault((filename, scope[-1], test.lineno), []).append(test)
        for child in ast.iter_child_nodes(node):
            self._visit(child, filename, scope)

    def get(self, func: str, lineno: int, *, file: str | None = None) -> ast.expr | None:
        filename = None
        if file is not None:
            path = Path(file)
            if not path.is_absolute() and not path.is_file():
                path = self.root / path
            filename = str(path.resolve())
            exact = self.by_line.get((filename, func, lineno))
            if exact is not None:
                return exact
            aliases = self._aliases.get((filename, func, lineno), [])
            return aliases[0] if len(aliases) == 1 else None
        matches = [test for (path, name, line), test in self.by_line.items()
                   if (filename is None or path == filename)
                   and name == func and line == lineno]
        if not matches:
            matches = [test for (path, name, line), tests in self._aliases.items()
                       if (filename is None or path == filename)
                       and name == func and line == lineno for test in tests]
        # A legacy lookup without a filename must never guess between files.
        return matches[0] if len(matches) == 1 else None
