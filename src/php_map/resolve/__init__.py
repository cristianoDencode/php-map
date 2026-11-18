"""Estágio 2: resolução determinística dos registros em nós e arestas do grafo."""

from collections import Counter
from collections.abc import Callable, Iterable

from php_map.config import Settings
from php_map.graph.builder import GraphBuilder
from php_map.graph.field_writes import FieldWritesCollector
from php_map.ingest.records import Include, PhpFileRecord
from php_map.resolve.calls import CallResolver
from php_map.resolve.includes import IncludeGraph, resolve_includes
from php_map.resolve.references import resolve_references
from php_map.resolve.symbols import SymbolIndex
from php_map.resolve.unresolved import UnresolvedWriter

RecordSource = Iterable[PhpFileRecord] | Callable[[], Iterable[PhpFileRecord]]


def resolve_php(
    records: RecordSource,
    builder: GraphBuilder,
    unresolved: UnresolvedWriter,
    settings: Settings,
    field_writes: FieldWritesCollector | None = None,
) -> Counter[str]:
    """Duas passadas: índice + includes com todos os registros; depois as chamadas.

    `records` pode ser uma fábrica (ex.: `lambda: read_cache(path)`), chamada uma vez por
    passada para não manter 45 mil registros em memória; um iterável simples é materializado.
    As pendências agregadas só vão para o arquivo em `unresolved.close()` (responsabilidade
    de quem chama). Devolve contadores do que foi ignorado (`unknown_function` etc.).
    `field_writes` recolhe as chamadas `incluir`/`alterar` do DAO e os campos de
    formulário.
    """
    passes: Callable[[], Iterable[PhpFileRecord]]
    if callable(records):
        passes = records
    else:
        materialized = list(records)
        passes = lambda: materialized  # noqa: E731

    stats: Counter[str] = Counter()
    index = SymbolIndex(class_resolvers=settings.class_resolvers)
    raw_includes: dict[str, list[Include]] = {}
    for record in passes():
        index.add_record(record)
        if record.includes:
            raw_includes[record.path] = record.includes
        stats["files"] += 1
        if record.encoding_fallback:  # quantos o extrator leu como latin-1 na duvida
            stats["encoding_fallback"] += 1

    index.emit_nodes(builder, settings)
    includes = IncludeGraph()
    for path, items in raw_includes.items():
        resolve_includes(path, items, index.files, includes, builder, unresolved)
    index.emit_inheritance(builder, includes)

    calls = CallResolver(index, includes, builder, unresolved, stats, settings, field_writes)
    for record in passes():
        calls.resolve(record)
        resolve_references(record, index.files, builder)
    return stats
