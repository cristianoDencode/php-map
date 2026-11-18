"""Construção do índice de busca textual: FTS5 no `graph.db`.

Um documento por nó `File` (com registro do extrator), `Table`, `BusinessAction`, `Endpoint`
 e `Route`. O texto repetido em muitos arquivos (licença GPL, `<title>`
padrão) sai antes, como o filtro de arquivos conhecidos do IPED aplicado a trechos.

O filtro de repetidos é por **quase-duplicata** (decisão de projeto): a licença
tem variantes (ano do copyright, "DBselller"), e o hash exato deixava 268 arquivos com ela no
e-cidade. Cada texto vira janelas de `SHINGLE_WORDS` palavras (chave sem acento e sem
dígitos); a janela é frequente se aparece em mais de `boilerplate_min_files` arquivos, e o
texto sai se pelo menos `BOILERPLATE_SHARE` das suas janelas forem frequentes.

Os textos que passam no filtro também vão, um por linha, para `search_texts`: a
ficha de regras de negócio precisa do tipo, da linha e do `scope` de cada um.
"""

import hashlib
import json
import re
import sqlite3
import time
import unicodedata
from collections import Counter
from collections.abc import Callable, Iterable, Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from php_map.config import Settings
from php_map.errors import StoreError
from php_map.graph.builder import GraphBuilder, StoredEdge
from php_map.graph.model import EdgeType, NodeLabel, file_id, key_of
from php_map.graph.routines import function_body
from php_map.ingest.plpgsql_rules import Rule
from php_map.ingest.records import PhpFileRecord

RecordFactory = Callable[[], Iterable[PhpFileRecord]]

FIELDS = ("name", "catalog", "labels", "symbols", "text", "uses")
BOILERPLATE_TOP = 10
BOILERPLATE_TEXT_CHARS = 80
# 5 palavras: um erro de digitação num texto de 45 palavras derruba 5 de 41 janelas (88%
# frequentes). Com 8, derrubaria 8 de 38 (79%) e a variante escaparia do limite de 80%.
SHINGLE_WORDS = 5
BOILERPLATE_SHARE = 0.8

SCHEMA = """
DROP TABLE IF EXISTS search_docs;
DROP TABLE IF EXISTS search_fts;
DROP TABLE IF EXISTS search_names;
DROP TABLE IF EXISTS search_texts;
CREATE TABLE search_docs (rowid INTEGER PRIMARY KEY, node_id TEXT UNIQUE NOT NULL,
                          label TEXT NOT NULL, modules TEXT NOT NULL);
CREATE VIRTUAL TABLE search_fts USING fts5(name, catalog, labels, symbols, text, uses,
                          tokenize = 'unicode61 remove_diacritics 2', prefix = '3 4');
CREATE VIRTUAL TABLE search_names USING fts5(name, tokenize = 'trigram remove_diacritics 1');
CREATE TABLE search_texts (file_id TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL,
                           line INTEGER NOT NULL, scope TEXT NOT NULL);
"""
TEXTS_INDEX = "CREATE INDEX ix_search_texts_file ON search_texts(file_id)"

_PATH_SEPARATORS = re.compile(r"[/_.\d]+")
_CAMEL = re.compile(r"(?<=[a-z0-9])(?=[A-Z])")
_SPACES = re.compile(r"\s+")
_DIGITS = re.compile(r"\d+")


@dataclass(frozen=True, slots=True)
class KeptText:
    """Texto que passou no filtro de repetidos, com a origem (linha `search_texts`)."""

    kind: str
    text: str
    line: int
    scope: str


@dataclass(frozen=True, slots=True)
class SearchDoc:
    node_id: str
    label: str
    modules: tuple[str, ...]
    name: str
    catalog: str = ""
    labels: str = ""
    symbols: str = ""
    text: str = ""
    texts: tuple[KeptText, ...] = ()  # documentos `File` e `PgFunction`
    uses: str = ""  # tabelas e DAOs que o fonte usa (arestas do grafo, 1 salto)


@dataclass(slots=True)
class SearchStats:
    documents: int = 0
    by_label: dict[str, int] = field(default_factory=dict)
    texts: int = 0  # itens de `texts` lidos
    texts_dropped: int = 0  # itens descartados pelo filtro de repetidos
    boilerplate_min_files: int = 0
    boilerplate_shingles: int = 0  # janelas distintas acima do limite
    boilerplate_top: list[dict[str, Any]] = field(default_factory=list)  # {files, text}
    uses_files: int = 0  # arquivos com a coluna `uses`
    uses_terms: int = 0  # nomes de tabela/DAO distintos nela
    seconds: float = 0.0


def normalize_text(text: str) -> str:
    """Minúsculas, sem acento, espaços colapsados: a chave do filtro de repetidos."""
    plain = "".join(
        ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)
    )
    return _SPACES.sub(" ", plain.lower()).strip()


def boilerplate_key(text: str) -> str:
    """Chave do filtro de repetidos: `normalize_text` sem dígitos (o ano do copyright varia)."""
    return _SPACES.sub(" ", _DIGITS.sub(" ", normalize_text(text))).strip()


def _hash(text: str) -> int:
    return int.from_bytes(hashlib.blake2b(text.encode("utf-8"), digest_size=8).digest(), "big")


def shingles(text: str) -> set[int]:
    """Hashes das janelas de `SHINGLE_WORDS` palavras; texto curto → uma janela só."""
    words = boilerplate_key(text).split(" ")
    if len(words) <= SHINGLE_WORDS:
        return {_hash(" ".join(words))}
    return {
        _hash(" ".join(words[i : i + SHINGLE_WORDS])) for i in range(len(words) - SHINGLE_WORDS + 1)
    }


def split_symbol(name: str) -> str:
    """`cl_empEmpenho` → `cl emp Empenho cl_empEmpenho` (partes e o nome original)."""
    parts = _CAMEL.sub(" ", name.replace("\\", " ").replace("_", " ").replace("::", " "))
    return f"{_SPACES.sub(' ', parts).strip()} {name}".strip()


def path_name(path: str) -> str:
    """`emp4_anularempenho001.php` → `emp anularempenho php emp4_anularempenho001.php`."""
    return f"{_SPACES.sub(' ', _PATH_SEPARATORS.sub(' ', path)).strip()} {path}"


def _join(parts: Iterable[str | None]) -> str:
    return "\n".join(dict.fromkeys(p.strip() for p in parts if p and p.strip()))


# --------------------------------------------------------------------------- grafo


@dataclass(slots=True)
class _GraphIndex:
    actions_of: dict[str, list[str]] = field(default_factory=dict)  # File → BusinessAction
    files_of: dict[str, list[str]] = field(default_factory=dict)  # BusinessAction → File
    modules_of: dict[str, set[str]] = field(default_factory=dict)  # nó → nomes de Module
    columns: dict[str, Mapping[str, Any]] = field(default_factory=dict)  # coluna → rótulo
    uses_of: dict[str, set[str]] = field(default_factory=dict)  # File → tabelas e DAOs


_USES_TABLE_EDGES = frozenset(
    {EdgeType.QUERIES, EdgeType.INSERTS, EdgeType.UPDATES, EdgeType.DELETES, EdgeType.MAPS_TO}
)
_USES_DAO_EDGES = frozenset({EdgeType.INSTANTIATES, EdgeType.CALLS})
_DAO_PREFIX = "cl_"


def _used_name(builder: GraphBuilder, edge: StoredEdge) -> str | None:
    """Nome que a aresta faz o fonte \"usar\": `protocolo.cgmtipoempresa` (tabela) ou
    `cl_cgmtipoempresa` (DAO, também quando o destino é um método dele). `None` = ignora."""
    dst = edge.dst
    if edge.type in _USES_TABLE_EDGES:
        wanted = dst.startswith(f"{NodeLabel.TABLE}:")
    elif edge.type in _USES_DAO_EDGES:
        wanted = key_of(dst).startswith(_DAO_PREFIX) and dst.startswith(
            (f"{NodeLabel.CLASS}:", f"{NodeLabel.METHOD}:")
        )
    else:
        return None
    if not wanted or dst not in builder or builder.node_attrs(dst).get("is_infra"):
        return None
    return key_of(dst).partition("::")[0]


def _source_file(builder: GraphBuilder, src: str) -> str | None:
    """`File:` de uma origem: ela mesma ou, para `Method`/`Function`/`Class`, o atributo `path`
    (nunca derivado do nome: o nome do nó é minúsculo, o caminho não)."""
    if src.startswith(f"{NodeLabel.FILE}:"):
        return src
    if src not in builder:
        return None
    path = builder.node_attrs(src).get("path")
    return file_id(str(path)) if path else None


def _is_self_call(builder: GraphBuilder, edge: StoredEdge, name: str) -> bool:
    """`cl_x::incluir` chamando `cl_x::sql_record`: o DAO não "usa" a si mesmo."""
    if edge.type != EdgeType.CALLS or edge.src not in builder:
        return False
    return builder.node_attrs(edge.src).get("class") == name


def _graph_index(builder: GraphBuilder) -> _GraphIndex:
    index = _GraphIndex()
    for edge in builder.iter_edges():
        if edge.type in _USES_TABLE_EDGES or edge.type in _USES_DAO_EDGES:
            name = _used_name(builder, edge)
            file_node = _source_file(builder, edge.src) if name else None
            if name and file_node and not _is_self_call(builder, edge, name):
                index.uses_of.setdefault(file_node, set()).add(name)
        elif edge.type == EdgeType.IMPLEMENTS_ACTION:
            index.actions_of.setdefault(edge.src, []).append(edge.dst)
            index.files_of.setdefault(edge.dst, []).append(edge.src)
        elif edge.type == EdgeType.BELONGS_TO and edge.dst in builder:
            name = builder.node_attrs(edge.dst).get("name") or key_of(edge.dst)
            index.modules_of.setdefault(edge.src, set()).add(str(name))
    tables = sorted(
        (node for node in builder.iter_nodes() if node.label == NodeLabel.TABLE),
        key=lambda node: node.id,
    )
    for node in tables:
        for column, info in (node.attrs.get("columns_catalog") or {}).items():
            index.columns.setdefault(str(column).lower(), info)  # 1ª tabela (por ID) vence
    return index


# --------------------------------------------------------------------------- documentos


def build_documents(
    records: RecordFactory,
    builder: GraphBuilder,
    settings: Settings,
    routines: Mapping[str, str] | None = None,
    rules: Mapping[str, Sequence[Rule]] | None = None,
) -> tuple[list[SearchDoc], SearchStats]:
    """Documentos ordenados por `node_id`. `records()` é chamada duas vezes (streaming).

    `routines`: `ID do PgFunction → texto` das funções da base; cada uma vira um
    documento, achado pelo nome e pelo que o corpo faz.

    `rules`: `ID do PgFunction → regras` lidas do mesmo texto; as mensagens de
    `validation`/`error`/`warning` e os `comment` entram em `search_texts` com `scope` = o
    próprio `PgFunction`, para a ficha de regras de negócio achar as regras das funções
    chamadas (`web/rules.py`)."""
    started = time.perf_counter()
    stats = SearchStats(boilerplate_min_files=settings.boilerplate_min_files)

    # 1ª passada: em quantos arquivos cada janela de texto aparece.
    files_with: Counter[int] = Counter()
    for record in records():
        in_file: set[int] = set()
        for item in record.texts:
            in_file |= shingles(item.text)
        files_with.update(in_file)
    frequent = {h for h, n in files_with.items() if n > settings.boilerplate_min_files}
    del files_with
    stats.boilerplate_shingles = len(frequent)
    dropped_files: Counter[str] = Counter()  # texto descartado (chave) → arquivos
    dropped_sample: dict[str, str] = {}

    graph = _graph_index(builder)
    docs: list[SearchDoc] = []
    for record in records():  # 2ª passada: os documentos File
        node_id = file_id(record.path)
        if node_id not in builder:
            continue
        kept: list[KeptText] = []
        dropped_here: set[str] = set()
        for item in record.texts:
            stats.texts += 1
            windows = shingles(item.text)
            if sum(1 for h in windows if h in frequent) >= BOILERPLATE_SHARE * len(windows):
                stats.texts_dropped += 1
                key = boilerplate_key(item.text)
                dropped_here.add(key)
                dropped_sample.setdefault(key, item.text[:BOILERPLATE_TEXT_CHARS])
                continue
            kept.append(KeptText(item.kind, item.text, item.line, item.scope))
        dropped_files.update(dropped_here)
        docs.append(_file_doc(node_id, record, kept, builder, graph))
    top = sorted(dropped_files.items(), key=lambda item: (-item[1], item[0]))[:BOILERPLATE_TOP]
    stats.boilerplate_top = [{"files": n, "text": dropped_sample[key]} for key, n in top]

    for node in builder.iter_nodes():
        if node.label == NodeLabel.TABLE:
            docs.append(_table_doc(node.id, node.attrs, graph))
        elif node.label == NodeLabel.BUSINESS_ACTION:
            docs.append(_action_doc(node.id, node.attrs, graph))
        elif node.label == NodeLabel.ENDPOINT:
            docs.append(_endpoint_doc(node.id, node.attrs))
        elif node.label == NodeLabel.ROUTE:
            docs.append(_route_doc(node.id, node.attrs))
        elif node.label == NodeLabel.FILE and node.attrs.get("kind") == "js":
            docs.append(_js_doc(node.id, node.attrs, graph))
        elif node.label == NodeLabel.PG_FUNCTION and routines and node.id in routines:
            docs.append(
                _function_doc(node.id, node.attrs, routines[node.id], (rules or {}).get(node.id))
            )
    docs.sort(key=lambda doc: doc.node_id)
    stats.documents = len(docs)
    stats.uses_files = sum(1 for doc in docs if doc.uses)
    stats.uses_terms = len({name for names in graph.uses_of.values() for name in names})
    stats.by_label = dict(sorted(Counter(doc.label for doc in docs).items()))
    stats.seconds = round(time.perf_counter() - started, 2)
    return docs, stats


def _modules(graph: _GraphIndex, node_id: str) -> tuple[str, ...]:
    return tuple(sorted(graph.modules_of.get(node_id, ())))


def _file_doc(
    node_id: str,
    record: PhpFileRecord,
    texts: Sequence[KeptText],
    builder: GraphBuilder,
    graph: _GraphIndex,
) -> SearchDoc:
    catalog: list[str | None] = []
    for action in sorted(graph.actions_of.get(node_id, ())):
        attrs = builder.node_attrs(action)
        catalog += [attrs.get("menu_path"), attrs.get("help"), attrs.get("desctec")]
    labels: list[str | None] = []
    for call in record.calls:  # `$clrotulo->label("e60_vlranu")`: o rótulo vem do dicionário
        if call.kind == "method" and call.name == "label" and call.arg0:
            column = call.arg0.strip().lower()
            info = graph.columns.get(column)
            labels += [info.get("label"), info.get("description")] if info else [column]
    symbols = [split_symbol(d.name) for d in record.declares]
    return SearchDoc(
        node_id,
        NodeLabel.FILE.value,
        _modules(graph, node_id),
        path_name(record.path),
        _join(catalog),
        _join(labels),
        _join(symbols),
        _join(t.text for t in texts),
        tuple(texts),
        _join(sorted(graph.uses_of.get(node_id, ()))),
    )


def _table_doc(node_id: str, attrs: Mapping[str, Any], graph: _GraphIndex) -> SearchDoc:
    name = str(attrs.get("name") or key_of(node_id).partition(".")[2])
    catalog = attrs.get("catalog") or {}
    columns = attrs.get("columns_catalog") or {}
    modules = _modules(graph, node_id)
    labels = [
        part for info in columns.values() for part in (info.get("label"), info.get("description"))
    ]
    symbols = list(attrs.get("columns") or []) + list(columns)
    return SearchDoc(
        node_id,
        NodeLabel.TABLE.value,
        modules,
        _join([f"{name} {catalog.get('prefix') or ''}"]),
        _join([catalog.get("description"), catalog.get("label"), *modules]),
        _join(labels),
        _join(symbols),
    )


def _action_doc(node_id: str, attrs: Mapping[str, Any], graph: _GraphIndex) -> SearchDoc:
    files = sorted(key_of(f) for f in graph.files_of.get(node_id, ()))
    return SearchDoc(
        node_id,
        NodeLabel.BUSINESS_ACTION.value,
        _modules(graph, node_id),
        _join([attrs.get("name")]),
        _join([*(attrs.get("menu_paths") or []), attrs.get("help"), attrs.get("desctec")]),
        symbols=_join(files),  # o `query` sugere o `trace` do 1º arquivo
    )


def _endpoint_doc(node_id: str, attrs: Mapping[str, Any]) -> SearchDoc:
    """Endpoint SOAP: achado pelo nome, pelo sistema/protocolo e pelos
    nomes dos parâmetros declarados (`cnpj`, `data_vencimento`)."""
    name = str(attrs.get("name") or key_of(node_id).rpartition(":")[2])
    params = [str(p.get("name")) for p in attrs.get("params") or () if p.get("name")]
    action = attrs.get("action")  # RPC: o nome é `arquivo#ação`; acha-se pela ação e pelo arquivo
    shown = (
        f"{split_symbol(str(action))} {path_name(str(attrs.get('path') or ''))}"
        if action
        else split_symbol(name)
    )
    return SearchDoc(
        node_id,
        NodeLabel.ENDPOINT.value,
        (),
        shown,
        _join([attrs.get("system"), attrs.get("protocol"), attrs.get("kind")]),
        "",
        _join(split_symbol(param) for param in params),
    )


_MESSAGE_KINDS = frozenset({"validation", "error", "warning"})


def _function_doc(
    node_id: str, attrs: Mapping[str, Any], ddl: str, rules: Sequence[Rule] | None
) -> SearchDoc:
    """Função da base: achada pelo nome e pelo corpo (`juros`, `arrecad`).

    As mensagens de validação/erro/aviso e os comentários da função (lidos pelo
    leitor léxico) entram em `search_texts`, com `scope` = o próprio nó: a ficha de regras de
    negócio usa isso para mostrar as regras das funções chamadas (`web/rules.py`)."""
    name = str(attrs.get("name") or key_of(node_id))
    texts = tuple(
        KeptText("comment" if r.kind == "comment" else "literal", r.message, r.line, node_id)
        for r in rules or ()
        if r.message and (r.kind == "comment" or r.kind in _MESSAGE_KINDS)
    )
    return SearchDoc(
        node_id,
        NodeLabel.PG_FUNCTION.value,
        (),
        split_symbol(name),
        _join([str(attrs.get("schema") or ""), str(attrs.get("language") or "")]),
        "",
        "",
        function_body(ddl),
        texts,
    )


def _js_doc(node_id: str, attrs: Mapping[str, Any], graph: _GraphIndex) -> SearchDoc:
    """Arquivo JS: não tem registro do extrator; é achado pelo caminho
    (`RedeSimImportaFunc` → `rede sim importa func`)."""
    path = str(attrs.get("path") or key_of(node_id))
    return SearchDoc(
        node_id,
        NodeLabel.FILE.value,
        _modules(graph, node_id),
        path_name(path),
        symbols=split_symbol(path.rsplit("/", 1)[-1].removesuffix(".js")),
    )


def _route_doc(node_id: str, attrs: Mapping[str, Any]) -> SearchDoc:
    """Rota HTTP: achada pela URI (`financeiro/empenho`), pelo verbo, pelo controller e
    pelos nomes dos segmentos dinâmicos. A rota do Zend 1 não tem `uri`: vale a chave."""
    key = key_of(node_id)
    uri = str(attrs.get("uri") or key)
    params = [str(p.get("name")) for p in attrs.get("params") or () if p.get("name")]
    action = str(attrs.get("action") or "")
    return SearchDoc(
        node_id,
        NodeLabel.ROUTE.value,
        (),
        split_symbol(uri),
        _join([attrs.get("verb"), attrs.get("area"), *(attrs.get("middleware") or ())]),
        "",
        _join([*(split_symbol(p) for p in params), split_symbol(action) if action else None]),
    )


# --------------------------------------------------------------------------- gravação


def write_index(db_path: Path, docs: Sequence[SearchDoc], stats: SearchStats) -> None:
    """Recria as tabelas de busca no banco do grafo, numa transação."""
    if not db_path.is_file():
        raise StoreError(f"banco do grafo inexistente: {db_path}. Rode o scan primeiro.")
    started = time.perf_counter()
    built_at = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        with closing(sqlite3.connect(db_path)) as conn, conn:
            conn.executescript("BEGIN;" + SCHEMA)
            conn.executemany(
                "INSERT INTO search_docs VALUES (?, ?, ?, ?)",
                (
                    (rowid, d.node_id, d.label, _json_list(d.modules))
                    for rowid, d in enumerate(docs, 1)
                ),
            )
            conn.executemany(
                "INSERT INTO search_fts (rowid, name, catalog, labels, symbols, text, uses)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    (rowid, d.name, d.catalog, d.labels, d.symbols, d.text, d.uses)
                    for rowid, d in enumerate(docs, 1)
                ),
            )
            conn.executemany(
                "INSERT INTO search_names (rowid, name) VALUES (?, ?)",
                ((rowid, d.name) for rowid, d in enumerate(docs, 1)),
            )
            conn.executemany(
                "INSERT INTO search_texts VALUES (?, ?, ?, ?, ?)",
                ((d.node_id, t.kind, t.text, t.line, t.scope) for d in docs for t in d.texts),
            )
            conn.execute(TEXTS_INDEX)
            conn.executemany(
                "INSERT OR REPLACE INTO meta VALUES (?, ?)",
                [("search_docs", str(len(docs))), ("search_built_at", built_at)],
            )
    except sqlite3.Error as exc:
        raise StoreError(f"falha ao gravar o índice de busca em {db_path}: {exc}") from exc
    stats.seconds = round(stats.seconds + time.perf_counter() - started, 2)


def _json_list(values: Sequence[str]) -> str:
    return json.dumps(list(values), ensure_ascii=False)
