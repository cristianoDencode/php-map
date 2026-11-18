"""Eloquent: model → tabela (`MAPS_TO`).

Um model do e-cidade é uma classe cuja cadeia de `extends` chega em
`Illuminate\\Database\\Eloquent\\Model` (ou numa base do Laravel que a estende, como o
`Illuminate\\Foundation\\Auth\\User`), direto ou por um `BaseModel` do próprio sistema. O
`vendor/` não é escaneado, então a classe base não existe no grafo: o teste é sobre o **nome** que
o extrator entrega em `Declare.extends` (o `NameResolver` já devolve o FQCN, também para
`use … as Eloquent`).

A tabela vem do `protected $table = '…'` (`Declare.class_props`, schema_version 11) quando existe:
1.0, com o schema do literal (`'ouvidoria.cidadao'`) ou o do `search_path` (`'redesimimporta'`).
Sem `$table`, vale a convenção do Laravel, o plural em `snake_case` do nome curto da classe:
0.8, `evidence.rule = "eloquent:classname"`. É a mesma régua do Doctrine (`resolve/doctrine.py`).
Ler e escrever pelos métodos do Eloquent (`->get()`, `->save()`) fica de fora.
"""

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass

from php_map.config import Settings
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import (
    ELOQUENT_CONVENTION_CONFIDENCE,
    Edge,
    EdgeSource,
    EdgeType,
    class_id,
    table_id,
)
from php_map.ingest.records import PhpFileRecord
from php_map.ingest.table_directory import TableDirectory
from php_map.resolve.unresolved import UnresolvedWriter

RecordSource = Iterable[PhpFileRecord] | Callable[[], Iterable[PhpFileRecord]]

MODEL_BASE = "illuminate\\database\\eloquent\\model"
# Bases do `vendor/` que o Laravel define sobre o `Model` (o `vendor/` não é escaneado, então a
# cadeia não chega neles pelo grafo): o `User` de autenticação e os `Pivot`.
VENDOR_MODEL_BASES = frozenset(
    {
        MODEL_BASE,
        "illuminate\\foundation\\auth\\user",
        "illuminate\\database\\eloquent\\relations\\pivot",
        "illuminate\\database\\eloquent\\relations\\morphpivot",
    }
)
CONVENTION_RULE = "eloquent:classname"
_CAMEL_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")
_VOWELS = "aeiou"


@dataclass(slots=True)
class EloquentStats:
    models: int = 0
    by_table_literal: int = 0
    by_convention: int = 0
    missing: int = 0
    # Models com `$connection` próprio (gravado em `evidence.connection`; o destino não muda).
    with_connection: int = 0
    seconds: float = 0.0


@dataclass(slots=True)
class _ClassInfo:
    path: str
    line: int
    extends: str | None
    abstract: bool
    props: dict[str, str]
    display: str


def plural(word: str) -> str:
    """Plural do `Str::plural` do Laravel, na versão simples da tarefa: `+s` e `y → ies`.

    `y` depois de consoante vira `ies` (`entity → entities`); depois de vogal, só `+s`
    (`day → days`). `s`, `x`, `z`, `ch` e `sh` recebem `es`, como no Laravel. Palavra em português
    não segue essa regra (`RedeSimImporta → rede_sim_importas`): por isso é 0.8.
    """
    if word.endswith("y") and len(word) > 1 and word[-2] not in _VOWELS:
        return word[:-1] + "ies"
    if word.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    return word + "s"


def snake(name: str) -> str:
    """`Str::snake`: `RedeSimImporta → rede_sim_importa`, `ISSQNBase → issqn_base`."""
    return _CAMEL_BOUNDARY.sub("_", name).lower()


def convention_table(class_name: str) -> str:
    """Tabela que o Laravel deduz do nome da classe (`app\\x\\RedeSimImporta → rede_sim_importas`).

    Precisa da caixa original (`display_name` do extrator): o `name` do registro é minúsculo.
    """
    short = class_name.rsplit("\\", 1)[-1]
    parts = snake(short).split("_")
    parts[-1] = plural(parts[-1])
    return "_".join(parts)


def _collect(records: RecordSource) -> dict[str, _ClassInfo]:
    """`nome da classe (minúsculo) → dados`, só das classes (não interface nem trait)."""
    classes: dict[str, _ClassInfo] = {}
    for record in records() if callable(records) else records:
        for declare in record.declares:
            if declare.kind != "class":
                continue
            classes[declare.name] = _ClassInfo(
                record.path,
                declare.line_start,
                declare.extends.lstrip("\\").lower() if declare.extends else None,
                declare.abstract,
                declare.class_props,
                declare.display_name or declare.name,
            )
    return classes


def _chain(name: str, classes: dict[str, _ClassInfo]) -> list[_ClassInfo] | None:
    """Cadeia da classe até o `Model` (a própria primeiro); `None` se não chega nele."""
    chain: list[_ClassInfo] = []
    seen: set[str] = set()
    current: str | None = name
    while current is not None and current not in seen:
        seen.add(current)
        info = classes.get(current)
        if info is None:
            return None
        chain.append(info)
        if info.extends in VENDOR_MODEL_BASES:
            return chain
        current = info.extends
    return None


def _prop(chain: list[_ClassInfo], key: str) -> str | None:
    """`$table`/`$connection` da classe mais próxima da própria que o declara (herdado vale)."""
    for info in chain:
        value = info.props.get(key)
        if value is not None:
            return value
    return None


def apply_eloquent(
    records: RecordSource,
    builder: GraphBuilder,
    unresolved: UnresolvedWriter,
    settings: Settings,
    directory: TableDirectory | None = None,
) -> EloquentStats:
    """`MAPS_TO` (`ELOQUENT_MODEL`) de cada model concreto.

    **Ordem:** depois do schema (o `directory` diz em que schema a tabela está) e da resolução
    PHP (os nós `Class` já existem); antes do SQL. Sem `directory` com tabelas (scan sem
    `--schema`/`--schema-db`), "a tabela não existe no banco" não se aplica e não vira pendência.
    Classe abstrata sem `$table` (o `BaseModel`) não gera `MAPS_TO`; com `$table` herdado, as
    filhas concretas usam o dela.
    """
    started = time.perf_counter()
    stats = EloquentStats()
    classes = _collect(records)
    known = directory if directory is not None else TableDirectory(settings.search_path)
    has_schema = len(known) > 0
    for name, info in classes.items():
        chain = _chain(name, classes)
        if chain is None:
            continue
        literal = _prop(chain, "table")
        if literal is None and info.abstract:
            continue
        stats.models += 1
        evidence: dict[str, object] = {"path": info.path, "line": info.line}
        if literal:
            schema_part, _dot, bare = literal.strip().lower().rpartition(".")
            table_name, schema = bare, schema_part or None
            confidence = 1.0
            stats.by_table_literal += 1
        else:
            table_name, schema = convention_table(info.display), None
            confidence = ELOQUENT_CONVENTION_CONFIDENCE
            evidence["rule"] = CONVENTION_RULE
            stats.by_convention += 1
        connection = _prop(chain, "connection")
        if connection:
            evidence["connection"] = connection
            stats.with_connection += 1
        real_schema, _rule = known.resolve(table_name, schema)
        target = table_id(table_name, real_schema)
        builder.add_edge(
            Edge(
                class_id(name),
                target,
                EdgeType.MAPS_TO,
                EdgeSource.ELOQUENT_MODEL,
                confidence,
                evidence,
            )
        )
        if has_schema and not known.has(table_name, real_schema):
            stats.missing += 1
            unresolved.add(
                "eloquent_table_missing",
                info.path,
                info.line,
                name,
                candidates=[target],
                hint=name,
            )
    stats.seconds = round(time.perf_counter() - started, 2)
    return stats
