"""Ficha de regras de negócio de uma tela: tudo determinístico, até a linha.

A partir de um `File` de entrada, a BFS da API (`walk_sql`, profundidade padrão 4, sem
infraestrutura, confiança ≥ 0,5) define os nós do fluxo. Deles saem:
- os menus (`IMPLEMENTS_ACTION` da entrada) e as ações despachadas (`dispatch_actions`);
- as tabelas gravadas e lidas pelo código visitado, com os rótulos do dicionário. O código no
  limite da profundidade também conta: a DAO do nível 4 grava a tabela, que seria o nível 5;
- as funções do banco chamadas (`CALLS_RPC`) e as `TRIGGERS` das tabelas gravadas;
- as mensagens (`literal` e `js_string`) e as anotações (`comment` com ≥ 4 palavras) de
  `search_texts` cujo `scope` é um nó visitado: o método ou a função, ou o topo de um arquivo.

A categoria da mensagem é uma classificação heurística por palavra-chave (`category`).
"""

import json
import re
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from php_map.analysis import LEAF_LABELS, TABLE_OPS
from php_map.errors import StoreError
from php_map.graph import routines as routine_store
from php_map.graph.model import NodeLabel, file_id, key_of
from php_map.resolve.calls import origin_id
from php_map.search.index import normalize_text
from php_map.web import repo
from php_map.web.subgraph import batches, placeholders, walk_sql

DEFAULT_DEPTH = 4
DEFAULT_MIN_CONFIDENCE = 0.5
MAX_EVIDENCE = 5
MIN_NOTE_WORDS = 4
MESSAGE_KINDS = ("literal", "js_string")
NOTE_KINDS = ("comment",)
NO_TEXTS = (
    "banco sem os textos da ficha (tabela search_texts): rode o scan de novo para gerar os "
    "textos da ficha"
)
CODE_LABELS = frozenset({"File", "Class", "Method", "Function"})
WRITE_OPS = ("INSERT", "UPDATE", "DELETE")

# Classificação heurística (decisão do planejamento): o texto é comparado sem acento e em
# minúsculas. `erro` vence `validação` ("nao Incluído. Inclusao Abortada." é um erro).
ERROR_TERMS = ("erro", "falha", "abortad")
VALIDATION_TERMS = (
    "não",
    "deve",
    "informe",
    "selecione",
    "obrigatóri",
    "inválid",
    "já existe",
    "saldo",
    "não pode",
    "não há",
)
_ERROR = re.compile(r"\b(?:erro|falha|abortad)")
# "não"/"deve" só como palavra inteira ("devedor" não é validação); os outros, como prefixo.
_VALIDATION = re.compile(
    r"\b(?:nao\b|deve(?:m|ra|rao|ria)?\b|informe|selecione|obrigatori|invalid|ja existe|saldo)"
)


def category(text: str) -> str:
    """`erro`, `validação` ou `mensagem` (neutro), pela palavra-chave, sem acento nem caixa."""
    plain = normalize_text(text)
    if _ERROR.search(plain):
        return "erro"
    if _VALIDATION.search(plain):
        return "validação"
    return "mensagem"


def node_scope(node_id: str) -> str | None:
    """`scope` dos textos de um nó: `Method:c::m` → `method:c::m`, `Function:f` →
    `function:f`, `File:x` → `''` (o topo do arquivo). Outros tipos não têm texto."""
    label, _sep, key = node_id.partition(":")
    if label == NodeLabel.METHOD:
        return f"method:{key}"
    if label == NodeLabel.FUNCTION:
        return f"function:{key}"
    if label == NodeLabel.FILE:
        return ""
    return None


def scope_node(path: str, scope: str) -> str:
    """O inverso de `node_scope`: o nó de onde o texto parte (o mesmo `origin_id` das arestas,
    que normaliza a caixa de classe e método)."""
    return origin_id(path, scope)


# --------------------------------------------------------------------------- registros


@dataclass(frozen=True, slots=True)
class Evidence:
    path: str
    line: int | None
    origin: str  # nó de onde a aresta parte


@dataclass(frozen=True, slots=True)
class Menu:
    id: str
    path: str | None
    paths: list[str]
    help: str | None
    desctec: str | None
    active: bool
    params: str | None


@dataclass(frozen=True, slots=True)
class Action:
    file: str
    cases: list[str]


@dataclass(frozen=True, slots=True)
class Participant:
    node: str
    label: str
    name: str | None
    depth: int
    via: str | None
    parent: str | None
    confidence: float | None
    evidence: dict[str, Any]


@dataclass(frozen=True, slots=True)
class Column:
    column: str
    label: str | None
    description: str | None


@dataclass(slots=True)
class TableUse:
    table: str
    name: str
    operations: list[str]
    catalog: dict[str, Any] | None
    columns_labeled: list[Column]
    evidence: list[Evidence] = field(default_factory=list)
    occurrences: int = 0


@dataclass(frozen=True, slots=True)
class Rpc:
    function: str
    name: str
    resolved: bool
    evidence: Evidence


@dataclass(frozen=True, slots=True)
class Trigger:
    table: str
    function: str
    evidence: Evidence | None


@dataclass(frozen=True, slots=True)
class Text:
    text: str
    kind: str
    category: str | None
    file: str
    line: int
    scope: str


@dataclass(frozen=True, slots=True)
class RuleStats:
    files: int
    methods: int
    tables: int
    messages: int
    notes: int
    truncated: bool


@dataclass(slots=True)
class BusinessRules:
    entry: str
    depth: int
    min_confidence: float
    modules: list[dict[str, Any]]
    menus: list[Menu]
    actions: list[Action]
    participants: list[Participant]
    writes: list[TableUse]
    reads: list[TableUse]
    rpc: list[Rpc]
    triggers: list[Trigger]
    messages: list[Text]
    notes: list[Text]
    stats: RuleStats
    clamped: list[str]


# --------------------------------------------------------------------------- consulta


def business_rules(
    conn: sqlite3.Connection,
    entry: str,
    *,
    depth: int = DEFAULT_DEPTH,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> BusinessRules:
    """A ficha da entrada (`emp4_x.php` ou `File:emp4_x.php`)."""
    clamp = repo.Clamp()
    depth = clamp("depth", depth, repo.MAX_DEPTH, 1)
    root = entry if entry.startswith(f"{NodeLabel.FILE}:") else file_id(entry)
    repo.require(conn, root, "File", "arquivo de entrada")
    _require_texts(conn)

    def expand(label: str) -> bool:
        return label not in LEAF_LABELS  # funções do banco não são expandidas (como no trace)

    walked = walk_sql(
        conn,
        root,
        depth=depth,
        min_confidence=min_confidence,
        include_infra=False,
        expand=expand,
        max_nodes=repo.MAX_NODES,
    )
    steps, labels = walked.walk.steps, walked.labels
    rows = repo.nodes(conn, steps)
    code = [n for n in steps if labels[n] in CODE_LABELS and n in rows]
    origins = [n for n in steps if n == root or expand(labels[n])]

    participants = [
        Participant(
            n,
            labels[n],
            _str(rows[n].attrs.get("name")),
            steps[n].depth,
            steps[n].edge_type,
            steps[n].parent,
            steps[n].confidence,
            steps[n].evidence,
        )
        for n in code
    ]
    writes, reads = _tables(conn, origins, min_confidence)
    rpc = _rpc(conn, origins, min_confidence)
    triggers = _triggers(conn, [t.table for t in writes])
    messages, notes = _texts(conn, [(n, steps[n].evidence) for n in code], rows)
    routine_messages, routine_notes = _routine_texts(conn, {r.function for r in rpc})
    messages = [*messages, *routine_messages]
    notes = [*notes, *routine_notes]
    return BusinessRules(
        entry=root,
        depth=depth,
        min_confidence=min_confidence,
        modules=repo.memberships(conn, root),
        menus=_menus(conn, root),
        actions=[
            Action(key_of(n), [str(c) for c in rows[n].attrs.get("dispatch_actions") or []])
            for n in code
            if labels[n] == "File" and rows[n].attrs.get("dispatch_actions")
        ],
        participants=participants,
        writes=writes,
        reads=reads,
        rpc=rpc,
        triggers=triggers,
        messages=messages,
        notes=notes,
        stats=RuleStats(
            files=sum(1 for n in code if labels[n] == "File"),
            methods=sum(1 for n in code if labels[n] in ("Method", "Function")),
            tables=len({t.table for t in writes} | {t.table for t in reads}),
            messages=len(messages),
            notes=len(notes),
            truncated=walked.truncated,
        ),
        clamped=clamp.clamped,
    )


def _require_texts(conn: sqlite3.Connection) -> None:
    found = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'search_texts'"
    ).fetchone()
    if not found:
        raise StoreError(NO_TEXTS)


def _str(value: Any) -> str | None:
    return str(value) if value is not None and value != "" else None


def _menus(conn: sqlite3.Connection, root: str) -> list[Menu]:
    rows = conn.execute(
        "SELECT n.id, n.attrs FROM edges e JOIN nodes n ON n.id = e.dst"
        " WHERE e.src = ? AND e.type = 'IMPLEMENTS_ACTION' ORDER BY n.id",
        (root,),
    ).fetchall()
    menus: list[Menu] = []
    for node_id, raw in dict.fromkeys(rows):
        attrs = json.loads(raw)
        menus.append(
            Menu(
                node_id,
                _str(attrs.get("menu_path")),
                [str(p) for p in attrs.get("menu_paths") or []],
                _str(attrs.get("help")),
                _str(attrs.get("desctec")),
                attrs.get("active") is not False,
                _str(attrs.get("params")),
            )
        )
    return menus


def _edges_from(
    conn: sqlite3.Connection, origins: Sequence[str], types: Sequence[str], min_confidence: float
) -> list[repo.EdgeRow]:
    found: list[repo.EdgeRow] = []
    for chunk in batches(origins):
        cursor = conn.execute(
            "SELECT id, src, dst, type, source, confidence, evidence, attrs FROM edges"
            f" WHERE src IN ({placeholders(len(chunk))})"
            f" AND type IN ({placeholders(len(types))}) AND confidence >= ? ORDER BY id",
            (*chunk, *types, min_confidence),
        )
        found.extend(_edge(row) for row in cursor)
    return found


def _edge(row: Sequence[Any]) -> repo.EdgeRow:
    edge_id, src, dst, type_, source, confidence, evidence, attrs = row
    return repo.EdgeRow(
        edge_id, src, dst, type_, source, confidence, json.loads(evidence), json.loads(attrs)
    )


def _evidences(edge: repo.EdgeRow) -> list[Evidence]:
    """A evidência da aresta e as `sample_lines` (arestas agregadas), sem repetir."""
    items = [edge.evidence, *(edge.attrs.get("sample_lines") or [])]
    result: dict[tuple[str, int | None], Evidence] = {}
    for item in items:
        path = item.get("path")
        if not path:
            continue
        line = item.get("line")
        key = (str(path), int(line) if isinstance(line, int) else None)
        result.setdefault(key, Evidence(key[0], key[1], edge.src))
    return list(result.values())


def _tables(
    conn: sqlite3.Connection, origins: Sequence[str], min_confidence: float
) -> tuple[list[TableUse], list[TableUse]]:
    """Tabelas gravadas e lidas pelo código visitado; tabelas de infraestrutura ficam de fora."""
    edges = _edges_from(conn, origins, tuple(TABLE_OPS), min_confidence)
    table_rows = repo.nodes(conn, (e.dst for e in edges))
    uses: dict[tuple[str, bool], TableUse] = {}
    for edge in edges:
        row = table_rows.get(edge.dst)
        if row is not None and row.attrs.get("is_infra"):
            continue
        op = TABLE_OPS[edge.type]
        write = op in WRITE_OPS
        use = uses.get((edge.dst, write))
        if use is None:
            attrs = row.attrs if row is not None else {}
            use = uses[(edge.dst, write)] = TableUse(
                edge.dst,
                str(attrs.get("name") or key_of(edge.dst)),
                [],
                attrs.get("catalog") or None,
                _columns(attrs.get("columns_catalog") or {}),
            )
        if op not in use.operations:
            use.operations.append(op)
        use.occurrences += int(edge.attrs.get("occurrences", 1))
        for evidence in _evidences(edge):
            if len(use.evidence) < MAX_EVIDENCE and evidence not in use.evidence:
                use.evidence.append(evidence)
    order = {op: i for i, op in enumerate(("INSERT", "UPDATE", "DELETE", "SELECT"))}
    for use in uses.values():
        use.operations.sort(key=order.__getitem__)
    ordered = sorted(uses.items())
    return [u for (_t, write), u in ordered if write], [
        u for (_t, write), u in ordered if not write
    ]


def _columns(columns: dict[str, Any]) -> list[Column]:
    return [
        Column(str(name), _str(info.get("label")), _str(info.get("description")))
        for name, info in columns.items()
        if isinstance(info, dict)
    ]


def _rpc(conn: sqlite3.Connection, origins: Sequence[str], min_confidence: float) -> list[Rpc]:
    edges = _edges_from(conn, origins, ("CALLS_RPC",), min_confidence)
    targets = repo.nodes(conn, (e.dst for e in edges))
    result: list[Rpc] = []
    for edge in sorted(edges, key=lambda e: (e.dst, e.src, e.id)):
        row = targets.get(edge.dst)
        attrs = row.attrs if row is not None else {}
        name = str(attrs.get("name") or key_of(edge.dst).split("(")[0])
        evidence = _evidences(edge)
        if not evidence:
            continue  # sem arquivo:linha não entra (regra da ficha)
        result.append(Rpc(edge.dst, name, attrs.get("resolved") is not False, evidence[0]))
    return result


def _triggers(conn: sqlite3.Connection, tables: Sequence[str]) -> list[Trigger]:
    result: list[Trigger] = []
    for chunk in batches(list(tables)):
        for row in conn.execute(
            "SELECT id, src, dst, type, source, confidence, evidence, attrs FROM edges"
            f" WHERE type = 'TRIGGERS' AND src IN ({placeholders(len(chunk))})"
            " ORDER BY src, dst",
            tuple(chunk),
        ):
            edge = _edge(row)
            evidence = _evidences(edge)
            result.append(Trigger(edge.src, edge.dst, evidence[0] if evidence else None))
    return result


def _texts(
    conn: sqlite3.Connection,
    code: Sequence[tuple[str, dict[str, Any]]],
    rows: dict[str, repo.NodeRow],
) -> tuple[list[Text], list[Text]]:
    """Mensagens e anotações dos escopos visitados, na ordem da BFS e da linha.

    `code` traz cada nó com a evidência da aresta que o alcançou. Só entram os textos do par
    (arquivo, escopo) de um nó visitado: o topo de um `File`, ou o corpo do método ou da função
    no arquivo que o declara (`text_files`).
    """
    allowed: set[tuple[str, str]] = set()
    order: dict[str, int] = {}  # arquivo → posição do 1º nó do fluxo que mora nele
    for position, (node_id, evidence) in enumerate(code):
        for path in text_files(rows[node_id], evidence):
            allowed.add((path, node_id))
            order.setdefault(path, position)
    files = [file_id(p) for p in order]
    file_rows = repo.nodes(conn, files)
    wanted = [f for f in files if not (f in file_rows and file_rows[f].attrs.get("is_infra"))]
    kinds = (*MESSAGE_KINDS, *NOTE_KINDS)
    messages: list[Text] = []
    notes: list[Text] = []
    for chunk in batches(wanted):
        for fid, kind, text, line, scope in conn.execute(
            "SELECT file_id, kind, text, line, scope FROM search_texts"
            f" WHERE file_id IN ({placeholders(len(chunk))})"
            f" AND kind IN ({placeholders(len(kinds))})",
            (*chunk, *kinds),
        ):
            path = key_of(fid)
            if (path, scope_node(path, scope)) not in allowed:
                continue
            if kind in NOTE_KINDS:
                if len(text.split()) >= MIN_NOTE_WORDS:
                    notes.append(Text(text, kind, None, path, line, scope))
            else:
                messages.append(Text(text, kind, category(text), path, line, scope))
    for items in (messages, notes):
        items.sort(key=lambda t: (order.get(t.file, len(order)), t.file, t.line, t.text))
    return messages, notes


def text_files(row: repo.NodeRow, evidence: Mapping[str, Any]) -> list[str]:
    """Arquivos cujos textos são do nó: o próprio `File`; para método ou função, o declarante
    que a aresta escolheu (`evidence.declared_in`) ou o único declarante do nó.

    Nome declarado em vários arquivos sem declarante escolhido (aresta ambígua) não traz
    texto: as cópias (`libs/db_stdlibsaude.php`, `libs/sd.php`…) têm corpos diferentes, e a
    ficha não sabe qual roda.
    """
    if row.label == "File":
        return [key_of(row.id)]
    if row.label not in ("Method", "Function"):
        return []  # classe não tem texto próprio (o do corpo é do método)
    chosen = evidence.get("declared_in")
    if isinstance(chosen, str) and chosen:
        return [chosen]
    declared = row.attrs.get("declared_in")
    if isinstance(declared, list) and len(declared) > 1:
        return []
    path = row.attrs.get("path")
    return [str(path)] if path else []


ROUTINE_RULES_LIMIT = 200
_ROUTINE_MESSAGE_KINDS = ("validation", "error", "warning")


def _routine_texts(conn: sqlite3.Connection, functions: set[str]) -> tuple[list[Text], list[Text]]:
    """Mensagens (`validation`/`error`/`warning`) e comentários das `PgFunction` chamadas por
    `CALLS_RPC`: a função é folha na BFS (não expande), mas as regras que ela
    declara entram na ficha, com a função de origem (`file`) e a condição quando existe."""
    if not routine_store.has_rules(conn):
        return [], []
    messages: list[Text] = []
    notes: list[Text] = []
    for node_id in sorted(functions):
        found = routine_store.read_rules(
            conn, node_id, kinds=(*_ROUTINE_MESSAGE_KINDS, "comment"), limit=ROUTINE_RULES_LIMIT
        )
        if found is None:
            continue
        label = key_of(node_id)
        for item in found["items"]:
            if item["kind"] == "comment":
                text = item["message"] or ""
                if len(text.split()) >= MIN_NOTE_WORDS:
                    notes.append(Text(text, "comment", None, label, item["line"], node_id))
                continue
            message = item["message"] or ""
            prefix = f"quando: {item['condition']} → " if item["condition"] else ""
            level = f"[{item['level']}] " if item["level"] else ""
            text = f"{prefix}{level}{message}"
            messages.append(Text(text, "literal", category(message), label, item["line"], node_id))
    return messages, notes
