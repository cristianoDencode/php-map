"""Paginação dos dossiês : divide o Markdown **inteiro** de um
dossiê em páginas de ~`max_words` palavras, para que a resposta de uma ferramenta MCP sempre
caiba no contexto da IA e, somando as páginas, chegue-se ao dossiê inteiro (nenhuma fórmula,
gravador ou leitor fica fora do alcance).

Função pura, sem banco: só texto. Quem monta o Markdown decide se corta algo (os renderizadores
de `analysis/formulas.py` e `analysis/table_access.py` ganham `full=True` para não cortar); quem
pagina aqui nunca descarta conteúdo, só divide.

Regras:
- corta só em linha de `## ` (fora de bloco de código cercado) e acumula seções até `max_words`;
- seção maior que `max_words` é quebrada em fronteira de **linha**, nunca no meio de uma linha, de
  um bloco de código cercado (```` ``` ````) ou do cabeçalho de uma tabela Markdown; prefere
  quebrar em `### ` (e assim por diante) antes de cair em linha solta;
- a continuação de uma seção quebrada recebe o título original com « (continuação)»;
- uma tabela quebrada repete as 2 linhas de cabeçalho (cabeçalho + separador) na continuação;
- o título `# ` do dossiê e a seção **Visão geral** (quando é a primeira seção, ou a que vem
  logo depois do título) ficam sempre na página 1, mesmo que isso passe de `max_words`;
- mesma entrada, mesmas páginas (determinístico: nenhuma escolha depende de ordem de dict nem de
  hash).
"""

from __future__ import annotations

import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

MIN_MAX_WORDS = 50  # piso de segurança: nunca pagina com um teto menor que isto

OVERVIEW_TITLE = "Visão geral"  # título da seção que `render_calc_dossier` sempre abre com

_FENCE = re.compile(r"^\s*```")
_TABLE_ROW = re.compile(r"^\s*\|.*\|\s*$")
_TABLE_SEP = re.compile(r"^\s*\|?\s*:?-{1,}:?\s*(\|\s*:?-{1,}:?\s*)*\|?\s*$")


@dataclass(frozen=True, slots=True)
class Page:
    number: int
    total: int
    markdown: str
    sections: tuple[str, ...]  # títulos `## ` desta página (com «(continuação)» quando for o caso)


def _words(lines: Sequence[str]) -> int:
    return sum(len(line.split()) for line in lines)


def _heading(line: str, marker: str) -> str | None:
    match = re.match(rf"^{marker}\s+(.*\S)\s*$", line)
    return match.group(1) if match else None


def _boundaries(lines: Sequence[str], marker: str) -> list[int]:
    """Linhas que abrem uma seção de nível `marker` (`##`, `###`...), ignorando o que está dentro
    de um bloco de código cercado (uma amostra de Markdown num dossiê não vira seção)."""
    found: list[int] = []
    in_fence = False
    for i, line in enumerate(lines):
        if _FENCE.match(line):
            in_fence = not in_fence
            continue
        if not in_fence and _heading(line, marker) is not None:
            found.append(i)
    return found


@dataclass(frozen=True, slots=True)
class _Atom:
    """A menor unidade que a quebra por linha pode isolar: uma linha comum, um bloco de código
    cercado inteiro (nunca se divide dentro dele) ou uma linha de dados de tabela (que carrega o
    cabeçalho, para repetir se a quebra cair logo antes dela)."""

    lines: tuple[str, ...]
    table_header: tuple[str, ...] | None = None


def _atoms(lines: Sequence[str]) -> list[_Atom]:
    atoms: list[_Atom] = []
    i, n = 0, len(lines)
    while i < n:
        line = lines[i]
        if _FENCE.match(line):
            j = i + 1
            while j < n and not _FENCE.match(lines[j]):
                j += 1
            j = min(j + 1, n)
            atoms.append(_Atom(tuple(lines[i:j])))
            i = j
            continue
        if _TABLE_ROW.match(line) and i + 1 < n and _TABLE_SEP.match(lines[i + 1]):
            header = (lines[i], lines[i + 1])
            atoms.append(_Atom(header))
            i += 2
            while i < n and _TABLE_ROW.match(lines[i]):
                atoms.append(_Atom((lines[i],), table_header=header))
                i += 1
            continue
        atoms.append(_Atom((line,)))
        i += 1
    return atoms


def _atomic_split(lines: Sequence[str], budget: int) -> list[list[str]]:
    """Última instância: quebra em fronteira de linha, nunca dentro de um bloco cercado nem entre
    o cabeçalho e o separador de uma tabela; repete o cabeçalho quando a quebra cai numa linha de
    dados da tabela."""
    chunks: list[list[str]] = []
    current: list[str] = []
    current_words = 0
    for atom in _atoms(lines):
        atom_words = _words(atom.lines)
        if current and current_words + atom_words > budget:
            chunks.append(current)
            current, current_words = [], 0
            if atom.table_header is not None:
                current.extend(atom.table_header)
                current_words += _words(atom.table_header)
        current.extend(atom.lines)
        current_words += atom_words
    if current:
        chunks.append(current)
    return chunks or [[]]


def _split_body(lines: list[str], marker: str, budget: int) -> list[list[str]]:
    """Divide o **corpo** de uma seção (sem a linha do próprio título) em pedaços que cabem no
    orçamento, preferindo cortar nas subseções `marker` (ex.: `###`) antes de cair em linha; um
    grupo ainda maior que o orçamento desce recursivamente para o próximo nível (`####`...), até
    não haver mais subseção e cair no corte por linha (`_atomic_split`)."""
    boundaries = _boundaries(lines, marker)
    if not boundaries:
        return _atomic_split(lines, budget)
    groups: list[tuple[str | None, list[str]]] = []
    if boundaries[0] > 0:
        groups.append((None, lines[: boundaries[0]]))
    for gi, start in enumerate(boundaries):
        end = boundaries[gi + 1] if gi + 1 < len(boundaries) else len(lines)
        groups.append((_heading(lines[start], marker), lines[start:end]))
    chunks: list[list[str]] = []
    current: list[str] = []
    current_words = 0
    for title, group in groups:
        group_words = _words(group)
        if group_words > budget:
            heading_line = group[0] if title is not None else None
            rest = group[1:] if title is not None else group
            for si, sub in enumerate(_split_body(rest, marker + "#", budget)):
                if si == 0:
                    piece = ([heading_line] if heading_line is not None else []) + sub
                elif title is not None:
                    piece = [f"{marker} {title} (continuação)", "", *sub]
                else:
                    piece = sub
                piece_words = _words(piece)
                if current and current_words + piece_words > budget:
                    chunks.append(current)
                    current, current_words = [], 0
                current.extend(piece)
                current_words += piece_words
            continue
        if current and current_words + group_words > budget:
            chunks.append(current)
            current, current_words = [], 0
        current.extend(group)
        current_words += group_words
    if current:
        chunks.append(current)
    return chunks or [[]]


def _split_h2(lines: Sequence[str]) -> list[tuple[str | None, list[str]]]:
    """`[(título, linhas incl. o `## título`)]`; a 1ª entrada tem título `None` quando há algo
    antes da 1ª seção (o `# ` do dossiê e o parágrafo de abertura)."""
    boundaries = _boundaries(lines, "##")
    sections: list[tuple[str | None, list[str]]] = []
    if not boundaries or boundaries[0] > 0:
        end = boundaries[0] if boundaries else len(lines)
        sections.append((None, list(lines[:end])))
    for idx, start in enumerate(boundaries):
        end = boundaries[idx + 1] if idx + 1 < len(boundaries) else len(lines)
        sections.append((_heading(lines[start], "##"), list(lines[start:end])))
    return sections


def paginate(markdown: str, max_words: int) -> list[Page]:
    """O `markdown` inteiro dividido em páginas de ~`max_words` palavras, nas fronteiras de `## `.
    Determinístico: a mesma entrada sempre dá as mesmas páginas."""
    budget = max(MIN_MAX_WORDS, max_words)
    lines = markdown.splitlines()
    sections = _split_h2(lines)

    pages_lines: list[list[str]] = []
    pages_titles: list[list[str]] = []
    current: list[str] = []
    current_titles: list[str] = []
    current_words = 0

    def flush() -> None:
        nonlocal current, current_titles, current_words
        if current:
            pages_lines.append(current)
            pages_titles.append(current_titles)
        current, current_titles, current_words = [], [], 0

    for idx, (title, body) in enumerate(sections):
        words = _words(body)
        is_preamble = title is None
        # a seção "Visão geral", quando é a 1ª ou a 2ª (logo após o preâmbulo), nunca sai da
        # página 1 nem é quebrada, mesmo que isso passe do orçamento.
        forced = not pages_lines and (is_preamble or (title == OVERVIEW_TITLE and idx <= 1))
        if forced:
            current.extend(body)
            current_words += words
            if title is not None:
                current_titles.append(title)
            continue
        if words > budget:
            heading_line = body[0] if title is not None else None
            rest = body[1:] if title is not None else body
            for ci, chunk in enumerate(_split_body(rest, "###", budget)):
                if ci == 0:
                    chunk_title = title
                    chunk_lines = ([heading_line] if heading_line is not None else []) + chunk
                elif title is not None:
                    chunk_title = f"{title} (continuação)"
                    chunk_lines = [f"## {chunk_title}", "", *chunk]
                else:
                    chunk_title = None
                    chunk_lines = chunk
                chunk_words = _words(chunk_lines)
                if current and current_words + chunk_words > budget:
                    flush()
                current.extend(chunk_lines)
                current_words += chunk_words
                if chunk_title is not None:
                    current_titles.append(chunk_title)
            continue
        if current and current_words + words > budget:
            flush()
        current.extend(body)
        current_words += words
        if title is not None:
            current_titles.append(title)
    flush()

    if not pages_lines:
        pages_lines, pages_titles = [[]], [[]]

    total = len(pages_lines)
    return [
        Page(
            number=i + 1,
            total=total,
            markdown=("\n".join(page_lines).rstrip() + "\n") if page_lines else "",
            sections=tuple(titles),
        )
        for i, (page_lines, titles) in enumerate(zip(pages_lines, pages_titles, strict=True))
    ]


def section_index(pages: Sequence[Page]) -> list[dict[str, Any]]:
    """`[{"title": ..., "page": n}]` na ordem das páginas, para a IA saber onde está cada seção."""
    return [{"title": title, "page": page.number} for page in pages for title in page.sections]


__all__ = ["MIN_MAX_WORDS", "OVERVIEW_TITLE", "Page", "paginate", "section_index"]
