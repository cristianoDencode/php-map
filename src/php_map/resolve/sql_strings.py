"""SQL embutido no PHP → tabelas e RPCs.

Cada `SqlString` do extrator vira `QUERIES`/`INSERTS`/`UPDATES`/`DELETES` para `Table` e
`CALLS_RPC` para `PgFunction`/`RpcCall`, partindo do nó do `scope` (`File`, `Function` ou
`Method`). As partes variáveis (`{{VAR}}`) são trocadas por placeholders em até três tentativas;
se nenhuma parsear, o fallback por regex é validado contra o schema e as DAOs.

O nome de tabela sem schema é resolvido pelo `search_path` no
`TableDirectory` do schema (arquivo ou banco); não achado, fica em `public`, como antes.

Com o mapa classe → tabela do Doctrine (`entities`), a string que é DQL
(`FROM Contribuinte\\PlanoContaAbrasf c`) é traduzida pelo mapa e **nunca** vai ao `pglast`
nem ao regex: sem isto, o fallback criava uma `Table` com o nome do namespace.
"""

import re
import time
from collections import Counter
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Literal

import pglast

from php_map.config import Settings
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import (
    Edge,
    EdgeSource,
    EdgeType,
    NodeLabel,
    class_id,
    key_of,
    rpccall_id,
    table_id,
)
from php_map.ingest.records import PhpFileRecord, SqlString
from php_map.ingest.sql_schema import normalize_escapes, regex_refs
from php_map.ingest.table_directory import RULES, TableDirectory
from php_map.resolve.calls import origin_id
from php_map.resolve.doctrine import EntityTable, dql_references
from php_map.resolve.unresolved import UnresolvedWriter
from php_map.sqlutil import (
    FuncCallRef,
    SqlRef,
    TableOp,
    analyze_statement,
    fitting_overloads,
    is_sequence_name,
)

VAR = "{{VAR}}"
DYNAMIC = "__var__"
SQL_REGEX_VALIDATED = 0.7
SQL_REGEX_NOT_VALIDATED = 0.5
SNIPPET_MAX = 200
# Tentativas, em ordem, para o `{{VAR}}` fora de aspas (dentro de aspas ele vira sempre `0`).
ATTEMPTS: tuple[tuple[str, str], ...] = (
    ("number", "0"),
    ("string", "'__var__'"),
    ("identifier", DYNAMIC),
)
_FC_CALL = re.compile(r"\b(fc_[a-z0-9_]+)\s*\(", re.IGNORECASE)

Validation = Literal["schema", "dao"]


@dataclass(slots=True)
class SqlStats:
    strings: int = 0
    parsed: Counter[str] = field(default_factory=Counter)  # direct/number/string/identifier
    regex_fallbacks: int = 0
    validated_schema: int = 0
    validated_dao: int = 0
    not_validated: int = 0
    sequences_dropped: int = 0
    dynamic_tables: int = 0
    dynamic_sql: int = 0
    rpc_calls: int = 0
    # strings que eram DQL, arestas que a tradução gerou e entidades fora do mapa.
    dql_strings: int = 0
    dql_edges: int = 0
    dql_unknown: int = 0
    # Tabelas sem schema por regra de resolução: explicit/search_path/unique/not_found.
    table_refs: dict[str, int] = field(default_factory=lambda: dict.fromkeys(RULES, 0))
    seconds: float = 0.0


def render(text: str, outside: str) -> str:
    """Troca cada `{{VAR}}`: dentro de aspas simples por `0` (fica `'0'`), fora por `outside`.

    A aspa `''` escapada alterna o estado duas vezes, então a contagem simples basta.
    """
    parts = text.split(VAR)
    out = [parts[0]]
    quoted = parts[0].count("'") % 2 == 1
    for part in parts[1:]:
        out.append("0" if quoted else outside)
        out.append(part)
        if part.count("'") % 2 == 1:
            quoted = not quoted
    return "".join(out)


@lru_cache(maxsize=65536)
def analyze_embedded(text: str) -> tuple[str | None, tuple[SqlRef, ...]]:
    """`(tentativa que parseou, referências)`; `(None, ())` se nenhuma parseou.

    Sem `{{VAR}}`, parseia o texto como está (`"direct"`). Pura, por isso em cache: as DAOs
    repetem as mesmas instruções de auditoria milhares de vezes.
    """
    if "\\'" in text:
        text = normalize_escapes(text)
    attempts = ATTEMPTS if VAR in text else (("direct", ""),)
    for name, outside in attempts:
        try:
            statements = pglast.parse_sql(render(text, outside))
        except pglast.parser.ParseError:
            continue
        refs: list[SqlRef] = []
        for raw in statements:
            if raw.stmt is not None:
                refs.extend(analyze_statement(raw.stmt))
        return name, tuple(dict.fromkeys(refs))
    return None, ()


class FunctionCatalog:
    """`PgFunction` do builder por nome, com a aridade gravada pelo schema."""

    def __init__(self, builder: GraphBuilder, builtins: frozenset[str] = frozenset()) -> None:
        self.builtins = builtins  # nomes do `pg_catalog`: chamada sem schema é nativa
        self.by_name: dict[str, list[str]] = {}
        self.schema: dict[str, str] = {}
        self.arity: dict[str, tuple[int, float]] = {}
        for node in builder.iter_nodes():
            if node.label != NodeLabel.PG_FUNCTION or node.attrs.get("resolved") is False:
                continue
            name = str(node.attrs.get("name", ""))
            self.by_name.setdefault(name, []).append(node.id)
            self.schema[node.id] = str(node.attrs.get("schema", "public"))
            maximum = node.attrs.get("max_args", len(node.attrs.get("args", ())))
            minimum = node.attrs.get("min_args", maximum)
            self.arity[node.id] = (int(minimum), float("inf") if maximum is None else maximum)
        for ids in self.by_name.values():
            ids.sort()

    def candidates(self, ref: FuncCallRef) -> list[str]:
        if ref.schema is None and ref.name in self.builtins:
            return []
        ids = self.by_name.get(ref.name, [])
        if ref.schema is not None:
            ids = [i for i in ids if self.schema[i] == ref.schema]
        return fitting_overloads(ids, self.arity, ref.nargs)


class TableValidator:
    """Confirma que um nome achado por regex é tabela: está no schema ou tem DAO (`cl_<nome>`).

    Lê o estado do builder no início da etapa: schema e símbolos PHP já carregados. Com
    o diretório, vale toda tabela conhecida, inclusive as do banco sem nó.
    """

    def __init__(self, builder: GraphBuilder, directory: TableDirectory | None = None) -> None:
        self._builder = builder
        self._directory = directory
        self._real_names: set[str] = set()
        for node in builder.iter_nodes():
            if node.label == NodeLabel.TABLE and node.attrs.get("resolved") is True:
                self._real_names.add(key_of(node.id).rsplit(".", 1)[-1])

    def validate(self, name: str, schema: str | None) -> Validation | None:
        if schema is not None:
            if self._is_real(table_id(name, schema)) or (
                self._directory is not None and self._directory.has(name, schema)
            ):
                return "schema"
        elif name in self._real_names or (
            self._directory is not None and self._directory.has(name)
        ):
            return "schema"
        if self._is_real(class_id(f"cl_{name}")):
            return "dao"
        return None

    def _is_real(self, node_id: str) -> bool:
        builder = self._builder
        return node_id in builder and builder.node_attrs(node_id).get("resolved") is True


def _is_dynamic(ref: TableOp) -> bool:
    """Nome montado com variável (`from {$tabela}`, `emp{$x}`): não vira nó."""
    return DYNAMIC in ref.name or (ref.schema is not None and DYNAMIC in ref.schema)


class _SqlResolver:
    def __init__(
        self,
        builder: GraphBuilder,
        unresolved: UnresolvedWriter,
        settings: Settings,
        directory: TableDirectory | None = None,
        entities: Mapping[str, EntityTable] | None = None,
    ) -> None:
        self.builder = builder
        self.unresolved = unresolved
        self.settings = settings
        # `None`: o perfil não lê Doctrine e nenhuma string é tratada como DQL.
        self.entities = entities
        self.functions = FunctionCatalog(
            builder, directory.builtin_functions if directory is not None else frozenset()
        )
        self.validator = TableValidator(builder, directory)
        # Sem schema nenhum, o diretório vazio manda tudo para `public` (`not_found`).
        self.directory = directory if directory is not None else TableDirectory()
        self.stats = SqlStats()

    def resolve(self, path: str, item: SqlString) -> None:
        self.stats.strings += 1
        src = origin_id(path, item.scope)
        evidence = {"path": path, "line": item.line}
        if self.entities is not None and self._dql(path, item, src, evidence):
            return
        attempt, refs = analyze_embedded(item.text)
        if attempt is not None:
            self.stats.parsed[attempt] += 1
            dynamic = False
            for ref in refs:
                if isinstance(ref, TableOp):
                    dynamic |= self._table(src, ref, EdgeSource.SQL_AST, 1.0, evidence)
                else:
                    self._rpc(src, ref, EdgeSource.SQL_AST, evidence)
            if dynamic:
                self._pending(path, item)
            return
        self._fallback(path, item, src, evidence)

    def _dql(self, path: str, item: SqlString, src: str, evidence: dict[str, object]) -> bool:
        """Traduz a DQL pelo mapa das entidades; `False` se a string não é DQL (segue o SQL).

        A aresta herda a confiança do `MAPS_TO` da entidade: 1.0 com `@Table`, 0.8 quando a
        tabela foi deduzida do nome da classe. Entidade fora do mapa não gera aresta, só pendência.
        """
        refs = dql_references(item.text)
        if not refs or self.entities is None:
            return False
        self.stats.dql_strings += 1
        for ref in refs:
            entity = self.entities.get(ref.entity)
            if entity is None:
                self.stats.dql_unknown += 1
                self.unresolved.add_aggregated(
                    "dql_entity_unknown", path, item.line, ref.raw, hint=ref.entity
                )
                continue
            flagged = {**evidence, "via": "dql", "entity": ref.entity}
            self.builder.add_edge(
                Edge(
                    src,
                    entity.table,
                    ref.op,
                    EdgeSource.DOCTRINE_ANNOTATION,
                    entity.confidence,
                    flagged,
                )
            )
            self.stats.dql_edges += 1
        return True

    def _fallback(self, path: str, item: SqlString, src: str, evidence: dict[str, object]) -> None:
        """Nenhuma tentativa parseou: regex sobre o texto com `{{VAR}}` → `__var__`."""
        self.stats.regex_fallbacks += 1
        text = render(item.text, DYNAMIC)
        dynamic = False
        for ref in regex_refs(text):
            if not isinstance(ref, TableOp):
                continue
            validated = None
            if not (_is_dynamic(ref) or is_sequence_name(ref.name)):
                validated = self.validator.validate(ref.name, ref.schema)
                if validated == "schema":
                    self.stats.validated_schema += 1
                elif validated == "dao":
                    self.stats.validated_dao += 1
                else:
                    self.stats.not_validated += 1
            confidence = SQL_REGEX_NOT_VALIDATED if validated is None else SQL_REGEX_VALIDATED
            flagged = {**evidence, "validated": validated or False}
            dynamic |= self._table(src, ref, EdgeSource.SQL_REGEX, confidence, flagged)
        for match in dict.fromkeys(m.group(1).lower() for m in _FC_CALL.finditer(text)):
            self._rpc(src, FuncCallRef(match), EdgeSource.SQL_REGEX, evidence)
        if dynamic or not item.complete:
            self._pending(path, item)

    def _table(
        self,
        src: str,
        ref: TableOp,
        source: EdgeSource,
        confidence: float,
        evidence: dict[str, object],
    ) -> bool:
        """Cria a aresta para a tabela; devolve `True` se o nome é dinâmico (sem aresta)."""
        if _is_dynamic(ref):
            self.stats.dynamic_tables += 1
            return True
        if is_sequence_name(ref.name):
            self.stats.sequences_dropped += 1
            return False
        schema, rule = self.directory.resolve(ref.name, ref.schema)
        self.stats.table_refs[rule] += 1
        target = table_id(ref.name, schema)
        self.builder.add_edge(Edge(src, target, ref.op, source, confidence, evidence))
        if ref.name.startswith(self.settings.infra_table_prefixes):
            self.builder.ensure_node(target, is_infra=True)
        return False

    def _rpc(
        self, src: str, ref: FuncCallRef, source: EdgeSource, evidence: dict[str, object]
    ) -> None:
        """Mesma regra do schema em arquivos: sobrecargas `1/n`; `fc_*` fora do schema → `RpcCall`;
        nativas (`nextval`, `coalesce`…) ignoradas. Pelo regex, a confiança é no máximo 0.7."""
        top = 1.0 if source == EdgeSource.SQL_AST else SQL_REGEX_VALIDATED
        ids = self.functions.candidates(ref)
        if ids:
            targets = ids
            if len(ids) > 1:
                evidence = {**evidence, "candidates": ids}
        elif ref.name.startswith("fc_"):
            targets = [rpccall_id(ref.name)]
        else:
            return
        for target in targets:
            edge = Edge(src, target, EdgeType.CALLS_RPC, source, top / len(targets), evidence)
            self.builder.add_edge(edge)
            if ref.name in self.settings.infra_rpc_functions:
                self.builder.ensure_node(target, is_infra=True)
        self.stats.rpc_calls += 1

    def _pending(self, path: str, item: SqlString) -> None:
        self.stats.dynamic_sql += 1
        snippet = " ".join(item.text.split())[:SNIPPET_MAX]
        hint = item.scope or None
        self.unresolved.add("dynamic_sql", path, item.line, snippet, hint=hint)


def resolve_sql(
    records: Iterable[PhpFileRecord] | Callable[[], Iterable[PhpFileRecord]],
    builder: GraphBuilder,
    unresolved: UnresolvedWriter,
    settings: Settings,
    directory: TableDirectory | None = None,
    entities: Mapping[str, EntityTable] | None = None,
) -> SqlStats:
    """Liga o SQL das strings PHP às tabelas e funções do banco.

    **Ordem:** rode depois do schema (que cria os `Table`/`PgFunction` reais e grava
    a aridade das funções) e da resolução PHP (que cria as classes `cl_*` das DAOs).
    O validador do fallback e o desempate de sobrecargas leem esse estado do builder uma vez,
    no início. `records` pode ser uma fábrica (chamada uma vez), como em `resolve_php`.
    `directory`: tabelas do schema e `search_path` para os nomes sem schema.
    `entities`: mapa classe → tabela do Doctrine, de `apply_doctrine`; liga a DQL.
    """
    started = time.perf_counter()
    resolver = _SqlResolver(builder, unresolved, settings, directory, entities)
    for record in records() if callable(records) else records:
        for item in record.sql_strings:
            resolver.resolve(record.path, item)
    resolver.stats.seconds = round(time.perf_counter() - started, 2)
    return resolver.stats
