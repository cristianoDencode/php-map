"""Includes/requires: resolução de caminhos, arestas File → File e fechamento transitivo."""

import posixpath
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass

from php_map.graph.builder import GraphBuilder
from php_map.graph.model import Edge, EdgeSource, EdgeType, file_id
from php_map.ingest.records import Include, Origin
from php_map.resolve.unresolved import UnresolvedWriter

MAX_CANDIDATES = 10
_LITERAL_KINDS = frozenset({"literal", "modification"})


def normalize_path(path: str) -> str | None:
    """Normaliza `./`, `../` e `\\`. Caminho que sai da raiz (ou vazio) → `None`."""
    normalized = posixpath.normpath(path.replace("\\", "/")).lstrip("/")
    if normalized in ("", ".") or normalized == ".." or normalized.startswith("../"):
        return None
    return normalized


def resolve_file(target: str, from_path: str, files: Collection[str]) -> str | None:
    """Arquivo existente para `target`: relativo à raiz e, depois, ao diretório de `from_path`."""
    for candidate in (target, posixpath.join(posixpath.dirname(from_path), target)):
        normalized = normalize_path(candidate)
        if normalized is not None and normalized in files:
            return normalized
    return None


@dataclass(frozen=True, slots=True)
class ResolvedInclude:
    src: str
    dst: str
    line: int
    var_types: Mapping[str, str | Origin]


class IncludeGraph:
    """Includes literais resolvidos, nos dois sentidos, com fechamento transitivo memoizado."""

    def __init__(self) -> None:
        self._out: dict[str, list[ResolvedInclude]] = {}
        self._in: dict[str, list[ResolvedInclude]] = {}
        self._closures: dict[str, frozenset[str]] = {}
        self._suffixes: dict[str, list[str]] = {}

    def add(self, include: ResolvedInclude) -> None:
        self._out.setdefault(include.src, []).append(include)
        self._in.setdefault(include.dst, []).append(include)
        self._closures.clear()

    def includes_of(self, path: str) -> Sequence[ResolvedInclude]:
        return self._out.get(path, ())

    def includers_of(self, path: str) -> Sequence[ResolvedInclude]:
        return self._in.get(path, ())

    def suffix_candidates(self, hint: str, files: Collection[str]) -> list[str]:
        """Até `MAX_CANDIDATES` arquivos que terminam com `hint` (memoizado por `hint`)."""
        cached = self._suffixes.get(hint)
        if cached is None:
            cached = sorted(f for f in files if f.endswith(hint))[:MAX_CANDIDATES]
            self._suffixes[hint] = cached
        return cached

    def closure(self, path: str) -> frozenset[str]:
        """Arquivos alcançáveis por includes a partir de `path`, **incluindo** o próprio.

        Busca iterativa (sem recursão) que tolera ciclos; o resultado é memoizado por arquivo.
        """
        cached = self._closures.get(path)
        if cached is not None:
            return cached
        seen = {path}
        stack = [path]
        while stack:
            for include in self._out.get(stack.pop(), ()):
                if include.dst not in seen:
                    seen.add(include.dst)
                    stack.append(include.dst)
        result = frozenset(seen)
        self._closures[path] = result
        return result


def _edge_type(include: Include) -> EdgeType:
    return EdgeType.REQUIRES if include.type.startswith("require") else EdgeType.INCLUDES


def resolve_includes(
    path: str,
    includes: Sequence[Include],
    files: Collection[str],
    graph: IncludeGraph,
    builder: GraphBuilder,
    unresolved: UnresolvedWriter,
) -> None:
    """Aresta `REQUIRES`/`INCLUDES` para alvos literais existentes; o resto vira pendência.

    A aresta sempre parte do `File`, mesmo com o include dentro de função ou método.
    """
    for include in includes:
        snippet = f"{include.type} {include.target or include.hint or '?'}"
        if include.expr_kind in _LITERAL_KINDS and include.target is not None:
            target = resolve_file(include.target, path, files)
            if target is None:
                unresolved.add(
                    "unresolved_include", path, include.line, snippet, hint=include.target
                )
                continue
            graph.add(ResolvedInclude(path, target, include.line, include.var_types))
            builder.add_edge(
                Edge(
                    file_id(path),
                    file_id(target),
                    _edge_type(include),
                    EdgeSource.PHP_AST,
                    1.0,
                    {"path": path, "line": include.line, "expr_kind": include.expr_kind},
                )
            )
            continue
        hint = include.hint or include.target
        candidates = graph.suffix_candidates(hint, files) if hint else []
        unresolved.add("dynamic_include", path, include.line, snippet, candidates, hint)
