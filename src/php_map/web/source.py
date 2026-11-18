"""Pasta do e-cidade em uso e leitura de código-fonte.

Só leitura e só **dentro** da pasta em uso: o caminho pedido é relativo (como o `path` do nó
ou da evidência), resolvido e conferido contra a raiz resolvida. As mensagens de erro nunca
trazem o caminho absoluto.
"""

import json
import logging
from dataclasses import dataclass
from pathlib import Path, PurePosixPath, PureWindowsPath
from typing import Any

from php_map.errors import QueryError
from php_map.pipeline import ECIDADE_MARKER

logger = logging.getLogger(__name__)

SETTINGS_NAME = "ui-settings.json"  # gravado pelo scan da interface; aqui só lido
MAX_FULL_BYTES = 1024 * 1024  # acima disso, só uma janela em volta da linha
MAX_CONTEXT = 200
DEFAULT_WINDOW = 400  # linhas devolvidas de arquivo grande sem `line`
BINARY_PROBE = 8192


@dataclass(frozen=True, slots=True)
class Workspace:
    source: str | None
    origin: str  # "projeto" | "argumento" | "configuração" | "grafo" | "nenhuma"
    exists: bool
    is_ecidade: bool  # a pasta tem o marcador do perfil do projeto (e-cidade, sem projeto)
    graph_source: str | None  # `meta.source` do grafo aberto
    marker: str = ECIDADE_MARKER  # o arquivo que o perfil exige na raiz

    @property
    def root(self) -> Path | None:
        return Path(self.source) if self.source and self.exists else None


@dataclass(frozen=True, slots=True)
class SourceFile:
    path: str
    total_lines: int
    start_line: int
    lines: list[str]
    highlight: int | None
    truncated: bool
    size_bytes: int


def read_settings_source(settings_path: Path) -> str | None:
    """`source` de `ui-settings.json`. Ausente ou inválido → `None`."""
    try:
        data: Any = json.loads(settings_path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, ValueError) as exc:
        logger.warning("%s ignorado: %s", settings_path.name, exc)
        return None
    value = data.get("source") if isinstance(data, dict) else None
    return value if isinstance(value, str) and value.strip() else None


def resolve_workspace(
    arg_source: Path | None,
    settings_path: Path | None,
    graph_source: str | None,
    project_source: str | None = None,
    marker: str = ECIDADE_MARKER,
) -> Workspace:
    """A primeira pasta informada, na ordem projeto ativo → `--source` → `ui-settings.json` →
    `meta.source`. Com projetos cadastrados, `project_source` vem primeiro: o
    `--source` e o `ui-settings.json` são de um projeto só."""
    candidates: list[tuple[str, str | None]] = [
        ("projeto", project_source),
        ("argumento", str(arg_source) if arg_source is not None else None),
        ("configuração", read_settings_source(settings_path) if settings_path else None),
        ("grafo", graph_source),
    ]
    for origin, value in candidates:
        if value:
            folder = Path(value)
            exists = folder.is_dir()
            return Workspace(
                str(folder.resolve()) if exists else value,
                origin,
                exists,
                exists and (folder / marker).is_file(),
                graph_source,
                marker,
            )
    return Workspace(None, "nenhuma", False, False, graph_source, marker)


def _relative(path: str) -> PurePosixPath:
    """Caminho relativo e limpo, ou `QueryError` (absoluto, unidade, `..`, vazio)."""
    text = path.strip().replace("\\", "/")
    windows = PureWindowsPath(path.strip())
    posix = PurePosixPath(text)
    if not text or windows.drive or windows.root or posix.is_absolute():
        raise QueryError("caminho fora da pasta do e-cidade")
    if any(part == ".." for part in posix.parts):
        raise QueryError("caminho fora da pasta do e-cidade")
    return posix


def read_source(
    workspace: Workspace,
    path: str,
    line: int | None = None,
    context: int = 30,
    max_full_bytes: int = MAX_FULL_BYTES,
) -> SourceFile:
    """Lê `path` (relativo à pasta em uso) em latin-1. Acima de `max_full_bytes`, só a janela
    `line ± context` (sem `line`, as primeiras `DEFAULT_WINDOW` linhas)."""
    root = workspace.root
    if root is None:
        raise QueryError("nenhuma pasta do e-cidade configurada (use `php-map ui --source`)")
    relative = _relative(path)
    base = root.resolve()
    target = (base / Path(*relative.parts)).resolve()  # segue links: o destino real conta
    if not target.is_relative_to(base):
        raise QueryError("caminho fora da pasta do e-cidade")
    shown = relative.as_posix()
    if not target.is_file():
        raise QueryError(f"arquivo não encontrado na pasta do e-cidade: {shown}")
    size = target.stat().st_size
    with target.open("rb") as handle:
        if b"\x00" in handle.read(BINARY_PROBE):
            raise QueryError(f"arquivo binário: {shown}")
    text = target.read_bytes().decode("latin-1")
    all_lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if all_lines and all_lines[-1] == "":
        all_lines.pop()
    total = len(all_lines)
    highlight = line if line is not None and 1 <= line <= total else None
    if size <= max_full_bytes:
        return SourceFile(shown, total, 1, all_lines, highlight, False, size)
    context = max(0, min(context, MAX_CONTEXT))
    if highlight is None:
        start, stop = 1, min(total, DEFAULT_WINDOW)
    else:
        start, stop = max(1, highlight - context), min(total, highlight + context)
    return SourceFile(shown, total, start, all_lines[start - 1 : stop], highlight, True, size)
