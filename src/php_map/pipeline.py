"""Orquestração do `scan`, sem I/O de console.

descoberta → plano do cache (sha1) → extração PHP → montagem do cache → schema →
resolução PHP → SQL das strings → catálogo do e-cidade (de arquivos ou do banco) →
módulo por regra de nome → SQLite →
índice de busca → agregados + `scan_summary.json` + `unresolved.jsonl`.
"""

import json
import logging
import re
import sqlite3
import time
from collections import Counter
from collections.abc import Callable, Iterable, Iterator, Sequence
from contextlib import closing
from dataclasses import asdict, dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any

from rich.progress import Progress

from php_map import __version__, fsperm
from php_map.analysis import routine_origin
from php_map.analysis.aggregate import (
    Analytics,
    build_analytics,
    write_analytics,
    write_mentions,
)
from php_map.config import DEFAULT_PROFILE, Settings
from php_map.config import ECIDADE_MARKER as ECIDADE_MARKER
from php_map.errors import ConfigError, StoreError
from php_map.graph import field_writes as field_writes_store
from php_map.graph import formula_index as formula_index_store
from php_map.graph import routines as routine_store
from php_map.graph import store
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import EdgeType, Node, NodeLabel, key_of
from php_map.ingest import ast_cache, php_extractor, sql_fields
from php_map.ingest.catalog_db import describe_dsn, load_catalog_db
from php_map.ingest.catalog_sql import CatalogReadStats, load_catalog
from php_map.ingest.discovery import list_php_files
from php_map.ingest.plpgsql_rules import RoutineProfile, Rule, scan_routine
from php_map.ingest.records import PhpFileRecord
from php_map.ingest.schema_db import DbRoutine, check_reachable, read_schema_db
from php_map.ingest.sql_schema import SchemaStats, load_schema, load_schema_snapshot
from php_map.ingest.table_directory import TableDirectory
from php_map.profiles.base import ProjectProfile, get_profile
from php_map.resolve import resolve_php
from php_map.resolve.catalog import CatalogStats, apply_catalog
from php_map.resolve.doctrine import DoctrineStats, EntityTable, apply_doctrine
from php_map.resolve.eloquent import EloquentStats, apply_eloquent
from php_map.resolve.endpoints import EndpointStats, apply_endpoints
from php_map.resolve.http_calls import HttpStats, apply_http_calls
from php_map.resolve.laravel_routes import RouteStats, apply_laravel_routes
from php_map.resolve.modules import ModuleStats, apply_naming_modules
from php_map.resolve.sql_strings import SqlStats, resolve_sql
from php_map.resolve.unresolved import UnresolvedWriter
from php_map.resolve.zend1 import Zend1Stats, apply_zend1_structure
from php_map.search.index import build_documents, write_index

logger = logging.getLogger(__name__)

UNREACHABLE_HINT = (
    "Confira a conexão (VPN?) ou use --no-schema-db para escanear sem o texto das funções"
)
SUMMARY_NAME = "scan_summary.json"
UNRESOLVED_NAME = "unresolved.jsonl"
_SLUG_UNSAFE = re.compile(r"[^a-z0-9]+")

# Mesma assinatura de `php_extractor.extract` (os testes passam um extrator falso).
ExtractFn = Callable[
    [Path, Sequence[str], Settings, Path, Progress | None], Iterable[PhpFileRecord]
]
RecordFactory = Callable[[], Iterable[PhpFileRecord]]
StageCallback = Callable[[str], None]


@dataclass(frozen=True, slots=True)
class ScanOptions:
    source: Path
    schema: tuple[Path, ...] = ()
    catalog: tuple[Path, ...] = ()
    # DSN do catálogo lido do banco. Fora do `repr`: pode ter a senha.
    catalog_db: str | None = field(default=None, repr=False)
    # schema lido do banco, em vez de `schema`. O DSN é `schema_dsn` ou,
    # sem ele, o do catálogo; os dois são independentes (schema do banco sem o catálogo).
    schema_db: bool = False
    schema_dsn: str | None = field(default=None, repr=False)
    # o schema do banco foi escolhido pelo modo automático da CLI (sem `--schema-db`):
    # base inacessível vira erro com a dica do `--no-schema-db`, em vez de um grafo sem funções.
    schema_db_auto: bool = False
    out_dir: Path = Path("out")
    db: Path | None = None
    reuse_ast: bool = False
    exclude: tuple[str, ...] = ()
    limit: int | None = None
    php_image: str | None = None  # `--php-image`; `None`: variável ou padrão
    search: bool = True  # `--no-search` desliga o índice
    analytics: bool = True  # `--no-analytics` desliga os agregados
    # perfil de projeto; `project`/`project_name` sem valor viram o slug
    # da pasta de `source` e o próprio slug, respectivamente (`project_defaults`).
    profile: str = DEFAULT_PROFILE
    project: str | None = None
    project_name: str | None = None

    @property
    def db_path(self) -> Path:
        return self.db if self.db is not None else self.out_dir / "graph.db"


@dataclass(slots=True)
class GraphResult:
    builder: GraphBuilder
    php: Counter[str]
    schema: SchemaStats | None
    sql: SqlStats
    durations: dict[str, float] = field(default_factory=dict)
    catalog: tuple[CatalogReadStats, CatalogStats] | None = None
    modules: ModuleStats | None = None
    zend1: Zend1Stats | None = None
    doctrine: DoctrineStats | None = None
    endpoints: EndpointStats | None = None
    routes: RouteStats | None = None
    # tela → JS → `Route` (`CALLS_HTTP`); `None` nos perfis sem Laravel.
    http_calls: HttpStats | None = None
    # model Eloquent → tabela (`MAPS_TO`); `None` nos perfis sem Laravel.
    eloquent: EloquentStats | None = None
    # `(ID do nó, função)` das funções lidas do banco e quando foram lidas.
    routines: list[tuple[str, DbRoutine]] = field(default_factory=list)
    routines_read_at: str | None = None
    # nomes das funções nativas do banco (não viram menção por texto).
    builtin_functions: frozenset[str] = frozenset()
    # o diretório de tabelas usado no carregamento do schema (nomes e colunas reais),
    # para resolver as colunas do texto das funções da base. `None` sem `--schema`/`--schema-db`.
    directory: TableDirectory | None = None
    # chamadas `incluir`/`alterar` do DAO e campos de formulário; `None` sem
    # `dao_write_methods` no perfil.
    field_writes: field_writes_store.FieldWritesCollector | None = None


@dataclass(slots=True)
class ScanSummary:
    source: str
    started_at: str
    duration_s: dict[str, float]
    files: dict[str, Any]
    nodes: dict[str, int]
    edges: dict[str, dict[str, int]]
    unresolved: dict[str, int]
    sql: dict[str, Any]
    schema: dict[str, Any] | None
    catalog: dict[str, Any] | None
    search: dict[str, Any] | None
    analytics: dict[str, Any] | None
    php_map_version: str
    db: str
    warnings: list[str]
    # `{"slug", "name", "profile"}` — ver `project_defaults`.
    project: dict[str, str]
    # módulos/Route/RENDERS do perfil `zend1`; `None` no `ecidade` (módulo por
    # catálogo, sem Route/RENDERS do Zend 1).
    zend1: dict[str, Any] | None
    # `@Entity` → tabela e DQL do Doctrine; `None` nos perfis que não leem Doctrine.
    doctrine: dict[str, Any] | None
    # `Endpoint`, `SERVES` e `CALLS_REMOTE`; `None` sem nada a ligar.
    endpoints: dict[str, Any] | None
    # rotas do Laravel; `None` nos perfis sem Laravel.
    routes: dict[str, Any] | None
    # JS alcançados pelas telas e URLs casadas com as rotas; `None` sem Laravel.
    http_calls: dict[str, Any] | None = None
    # model Eloquent → tabela; `None` nos perfis sem Laravel.
    eloquent: dict[str, Any] | None = None
    # texto das funções da base (`routine_sources`); preenchido após o `save`.
    routines: dict[str, Any] | None = None
    # origem das funções da base (`analytics_routine*`); preenchido após os agregados.
    routine_origins: dict[str, Any] | None = None
    # regras de negócio lidas do texto das funções (`routine_rules`/`routine_profile`).
    routine_rules: dict[str, Any] | None = None
    # campos e consultas por instrução SQL (`routine_queries`/`routine_fields`).
    routine_fields: dict[str, Any] | None = None
    # campos que o código PHP preenche antes de gravar pelo DAO (`php_field_writes`).
    field_writes: dict[str, Any] | None = None
    # fórmulas e regras por assunto, em todas as funções (`routine_formulas`,
    # `routine_rule_topics`). `None` sem o texto das funções (nada para indexar).
    formula_index: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def project_slug(source: Path) -> str:
    """Nome da pasta normalizado: minúsculas, só `[a-z0-9-]`, sem `-` nas pontas."""
    return _SLUG_UNSAFE.sub("-", source.resolve().name.lower()).strip("-") or "projeto"


def project_defaults(
    source: Path, project: str | None, project_name: str | None
) -> tuple[str, str]:
    """`(slug, nome)` — sem `--project`, o slug da pasta; sem `--project-name`, o próprio slug."""
    slug = project or project_slug(source)
    return slug, project_name or slug


def validate_source(
    source: Path, profile: str = DEFAULT_PROFILE, hint: str = " (confira o --source)"
) -> Path:
    """Pasta existente e com o marcador do perfil (padrão `ecidade`,
    `libs/db_stdlib.php`). A interface passa `hint=""`."""
    if not source.is_dir():
        raise ConfigError(f"diretório de origem inexistente: {source}")
    try:
        spec = get_profile(profile)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    if not (source / spec.required_marker).is_file():
        raise ConfigError(
            f"{source} não parece um projeto do perfil {spec.name!r}: "
            f"falta {spec.required_marker}{hint}"
        )
    return source


def build_graph(
    records: RecordFactory,
    schema: Sequence[Path],
    unresolved: UnresolvedWriter,
    settings: Settings,
    on_stage: StageCallback | None = None,
    catalog: Sequence[Path] = (),
    catalog_db: str | None = None,
    schema_db: str | None = None,
    schema_auto: bool = False,
) -> GraphResult:
    """Schema → PHP → SQL das strings → catálogo → nomes.

    É a mesma montagem do `scripts/measure_precision.py`, então o gabarito mede o que o
    `scan` grava. `records` é chamada uma vez por passada (streaming do cache). O catálogo vem
    de arquivos (`catalog`) ou do banco (`catalog_db`, DSN), nunca dos dois; o schema, de
    arquivos (`schema`) ou do banco (`schema_db`, DSN), nunca dos dois.
    """
    if catalog and catalog_db:
        raise ConfigError("use --catalog ou --catalog-db, não os dois")
    if schema and schema_db:
        raise ConfigError("use --schema ou --schema-db, não os dois")
    builder = GraphBuilder()
    durations: dict[str, float] = {}
    schema_stats = None
    captured: list[tuple[str, DbRoutine]] = []
    routines_read_at: str | None = None
    builtin: frozenset[str] = frozenset()
    directory: TableDirectory | None = None
    if schema or schema_db:
        _stage(on_stage, "schema")
        started = time.perf_counter()
        if schema_db:
            directory = TableDirectory()
            try:
                snapshot = read_schema_db(schema_db)
            except ConfigError as exc:
                if not schema_auto:
                    raise
                raise ConfigError(f"base inacessível ({exc}). {UNREACHABLE_HINT}") from None
            routines_read_at = routine_store.now_iso()
            builtin = snapshot.builtin_functions
            schema_stats = load_schema_snapshot(snapshot, builder, unresolved, directory, captured)
        else:
            directory = TableDirectory(settings.search_path)
            schema_stats = load_schema(schema, builder, unresolved, directory)
        durations["schema"] = _elapsed(started)
    _stage(on_stage, "resolve_php")
    started = time.perf_counter()
    collector = field_writes_store.FieldWritesCollector() if settings.dao_write_methods else None
    php_stats = resolve_php(records, builder, unresolved, settings, collector)
    durations["resolve_php"] = _elapsed(started)
    zend1_stats = None
    if settings.module_source == "directory":
        # módulo por diretório, `Route` e `RENDERS` do perfil `zend1`. Não depende
        # do catálogo do e-cidade (um projeto zend1 não tem); roda assim que `File`/`Method`
        # existem.
        _stage(on_stage, "zend1_structure")
        started = time.perf_counter()
        zend1_stats = apply_zend1_structure(records, builder, settings.source)
        durations["zend1_structure"] = _elapsed(started)
    route_stats = None
    if settings.laravel_routes:
        # depois da resolução PHP (os `Method` dos controllers já existem).
        _stage(on_stage, "routes")
        started = time.perf_counter()
        route_stats = apply_laravel_routes(records, builder, unresolved, settings)
        durations["routes"] = _elapsed(started)
    http_stats = None
    if settings.laravel_routes:
        # depois das rotas (as `Route` precisam existir) e das referências das telas.
        _stage(on_stage, "http_calls")
        started = time.perf_counter()
        http_stats = apply_http_calls(records(), builder, unresolved, settings)
        durations["http_calls"] = _elapsed(started)
    doctrine_stats = None
    entities: dict[str, EntityTable] | None = None
    if settings.reads_doctrine:
        # `@Entity` → `Table` (`MAPS_TO`) e o mapa que a DQL usa no passo do SQL.
        _stage(on_stage, "doctrine")
        started = time.perf_counter()
        doctrine_stats, entities = apply_doctrine(records, builder, unresolved, settings, directory)
        durations["doctrine"] = _elapsed(started)
    eloquent_stats = None
    if settings.reads_eloquent:
        # `extends Model` → `Table` (`MAPS_TO`). Depois do schema e da resolução PHP
        # (o `directory` diz o schema da tabela; os `Class` já existem) e antes do SQL.
        _stage(on_stage, "eloquent")
        started = time.perf_counter()
        eloquent_stats = apply_eloquent(records, builder, unresolved, settings, directory)
        durations["eloquent"] = _elapsed(started)
    _stage(on_stage, "sql")
    started = time.perf_counter()
    sql_stats = resolve_sql(records, builder, unresolved, settings, directory, entities)
    durations["sql"] = _elapsed(started)
    endpoint_stats = None
    if settings.endpoint_system:
        # depois do SQL (as tabelas e o `directory` já existem) e antes de materializar,
        # para que a tabela que só o XML cita também vire nó real.
        _stage(on_stage, "endpoints")
        started = time.perf_counter()
        endpoint_stats = apply_endpoints(records, builder, unresolved, settings, directory)
        durations["endpoints"] = _elapsed(started)
    if schema_db and directory is not None and schema_stats is not None:
        # Antes do catálogo, que só anota as `Table` que o scan já tem.
        schema_stats.materialized = materialize_tables(builder, directory, settings)
    catalog_stats = module_stats = None
    if catalog or catalog_db:
        # Depois do SQL: o catálogo só anota as `Table` e os `File` que o scan já criou.
        _stage(on_stage, "catalog")
        started = time.perf_counter()
        data = load_catalog_db(catalog_db) if catalog_db else load_catalog(catalog)
        catalog_stats = (data.stats, apply_catalog(data, builder, settings.module_aliases))
        durations["catalog"] = _elapsed(started)
        # Só depois do catálogo: as regras de nome usam os módulos e as arestas dele.
        _stage(on_stage, "modules")
        started = time.perf_counter()
        module_stats = apply_naming_modules(builder)
        durations["modules"] = _elapsed(started)
    return GraphResult(
        builder,
        php_stats,
        schema_stats,
        sql_stats,
        durations,
        catalog_stats,
        module_stats,
        zend1_stats,
        doctrine_stats,
        endpoint_stats,
        route_stats,
        http_stats,
        eloquent_stats,
        captured,
        routines_read_at,
        builtin,
        directory,
        collector,
    )


def materialize_tables(builder: GraphBuilder, directory: TableDirectory, settings: Settings) -> int:
    """Placeholders `Table` que existem no banco viram nós reais.

    Só as referenciadas viram nó: as outras 28 mil tabelas (inclusive as `w_*`/`tmp*` do
    `public`) ficam só no diretório. Devolve quantas foram materializadas.
    """
    pending = [
        node
        for node in builder.iter_nodes()
        if node.label == NodeLabel.TABLE and node.attrs.get("resolved") is False
    ]
    count = 0
    for node in pending:
        schema, _dot, name = key_of(node.id).partition(".")
        table = directory.info(schema, name)
        if table is None:
            continue
        attrs: dict[str, Any] = {
            "name": table.name,
            "schema": table.schema,
            "columns": list(table.columns),
            "kind": table.kind,
            "source_file": f"db:{table.schema}.{table.name}",
            "line": None,
            "is_infra": bool(node.attrs.get("is_infra"))
            or table.name.startswith(settings.infra_table_prefixes),
        }
        builder.add_node(Node(node.id, NodeLabel.TABLE, attrs))
        count += 1
    return count


def _settings_for_profile(
    base: Settings, profile: ProjectProfile, source: Path, options: ScanOptions
) -> Settings:
    """`Settings` do scan: os campos do perfil, com `--exclude` somado às
    exclusões do perfil (nunca no lugar delas) e `--php-image` vencendo o que `base` já tinha."""
    class_resolvers = profile.class_resolvers
    if profile.class_resolvers_factory is not None:
        # o `zend1` lê `application.ini` da própria pasta escaneada (`source`), que o
        # `PROFILE` estático não conhece.
        class_resolvers = profile.class_resolvers_factory(source)
    return replace(
        base,
        source=source,
        out_dir=options.out_dir,
        exclude_dirs=profile.exclude_dirs | frozenset(options.exclude),
        php_extensions=profile.source_extensions,
        php_image=options.php_image or base.php_image,
        profile=profile.name,
        markers=profile.markers,
        third_party_dirs=profile.third_party_dirs,
        encoding=profile.encoding,
        class_resolvers=class_resolvers,
        module_source=profile.module_source,
        reads_doctrine=profile.reads_doctrine,
        reads_eloquent=profile.reads_eloquent,
        endpoint_system=profile.endpoint_system,
        ws_xml=profile.ws_xml,
        ws_xml_ignore_prefixes=profile.ws_xml_ignore_prefixes,
        remote_calls=profile.remote_calls,
        soap_server_classes=profile.soap_server_classes,
        rpc_endpoints=profile.rpc_endpoints,
        laravel_routes=profile.laravel_routes,
        infra_functions=profile.infra_functions,
        infra_files=profile.infra_files,
        infra_rpc_functions=profile.infra_rpc_functions,
        infra_table_prefixes=profile.infra_table_prefixes,
        dao_factories=profile.dao_factories,
        dao_write_methods=profile.dao_write_methods,
        module_aliases=profile.module_aliases,
        search_path=profile.default_search_path,
    )


def run_scan(
    options: ScanOptions,
    settings: Settings | None = None,
    extract_fn: ExtractFn = php_extractor.extract,
    progress: Progress | None = None,
    on_stage: StageCallback | None = None,
) -> ScanSummary:
    """Executa o pipeline inteiro e grava banco, resumo, pendências e cache em `out_dir`."""
    started_at = datetime.now().astimezone().isoformat(timespec="seconds")
    try:
        profile = get_profile(options.profile)
    except ValueError as exc:
        raise ConfigError(str(exc)) from exc
    source = validate_source(options.source, profile.name)
    if options.catalog and options.catalog_db:  # antes da extração: erro de uso sai cedo
        raise ConfigError("use --catalog ou --catalog-db, não os dois")
    if options.catalog_db:
        describe_dsn(options.catalog_db)  # DSN malformado também sai antes da extração
    if options.schema and options.schema_db:
        raise ConfigError("use --schema ou --schema-db, não os dois")
    schema_dsn = (options.schema_dsn or options.catalog_db) if options.schema_db else None
    if options.schema_db and not schema_dsn:
        raise ConfigError(
            "--schema-db precisa do banco: defina PHP_MAP_CATALOG_DSN no .env ou use --catalog-db"
        )
    if schema_dsn:
        describe_dsn(schema_dsn)
    if options.schema_db_auto and schema_dsn:
        _probe_schema_db(schema_dsn)  # antes da extração: VPN desligada não custa 13 min
    base = settings or Settings()
    settings = _settings_for_profile(base, profile, source, options)
    project, project_name = project_defaults(source, options.project, options.project_name)
    warnings: list[str] = []
    durations: dict[str, float] = {}
    if not options.schema and not options.schema_db:
        _warn(warnings, "sem --schema: as tabelas ficarão como placeholder (resolved=False)")

    _stage(on_stage, "discovery")
    started = time.perf_counter()
    all_files = list_php_files(source, settings)
    files = all_files[: options.limit] if options.limit is not None else all_files
    durations["discovery"] = _elapsed(started)

    migrated = ast_cache.migrate_legacy(options.out_dir)
    if migrated is not None:  # formato antigo → um cache por pasta
        logger.info("cache de AST movido para %s", migrated.relative_to(options.out_dir).as_posix())
    ast_dir = ast_cache.cache_dir(options.out_dir, source)
    ast_dir.mkdir(parents=True, exist_ok=True)
    fsperm.secure_dir(ast_dir.parent)
    fsperm.secure_dir(ast_dir)
    _stage(on_stage, "hash")
    plan = ast_cache.plan_cache(source, files, all_files, ast_dir, settings, options.reuse_ast)
    durations["hash"] = plan.hash_seconds
    if plan.reason in ("schema_version", "extractor_hash"):
        _warn(warnings, f"cache de AST descartado ({plan.reason} mudou): reextraindo tudo")
    elif options.reuse_ast and plan.reason == "no_cache":
        _warn(warnings, "sem cache de AST válido (php.jsonl + meta.json): extraindo tudo")

    started = time.perf_counter()
    if plan.to_extract:  # tudo do cache: sem a etapa (a interface mostra só o que roda)
        _stage(on_stage, "extract")
        if extract_fn is php_extractor.extract:  # uma escolha de imagem por scan
            settings = php_extractor.with_image(settings)
        new_path = ast_dir / ast_cache.NEW_NAME
        for _record in extract_fn(source, plan.to_extract, settings, new_path, progress):
            pass
    cached = all_files if plan.kept else files
    statuses = ast_cache.assemble(ast_dir, plan, cached)
    meta = ast_cache.current_meta(source, settings)
    now = datetime.now().astimezone().isoformat(timespec="seconds")
    ast_cache.write_meta(ast_dir, replace(meta, files=len(cached), updated_at=now))
    fsperm.secure_tree(ast_dir)
    durations["extract"] = _elapsed(started)

    cache = ast_dir / ast_cache.CACHE_NAME
    scanned = set(files)
    if plan.kept:

        def records() -> Iterator[PhpFileRecord]:
            return (r for r in php_extractor.read_cache(cache) if r.path in scanned)

    else:

        def records() -> Iterator[PhpFileRecord]:
            return php_extractor.read_cache(cache)

    options.out_dir.mkdir(parents=True, exist_ok=True)
    pending = companion(options.db_path, options.out_dir, UNRESOLVED_NAME)
    pending.parent.mkdir(parents=True, exist_ok=True)
    fsperm.secure_dir(options.out_dir)
    fsperm.secure_dir(pending.parent)
    with pending.open("w", encoding="utf-8", newline="\n") as sink:
        fsperm.secure_file(pending)
        unresolved = UnresolvedWriter(sink)
        result = build_graph(
            records,
            options.schema,
            unresolved,
            settings,
            on_stage,
            options.catalog,
            options.catalog_db,
            schema_dsn,
            options.schema_db_auto,
        )
        unresolved.close()
    durations.update(result.durations)

    graph = result.builder.stats()
    summary = ScanSummary(
        source=str(source.resolve()),
        started_at=started_at,
        duration_s=durations,
        files={
            "total": len(files),
            "discovered": len(all_files),
            "by_status": dict(sorted(statuses.items())),
            "cache": plan.summary(),
            "cache_dir": ast_dir.relative_to(options.out_dir).as_posix(),
            # Imagem Docker do extrator; `None` quando tudo veio do cache.
            "extractor_image": settings.php_image if plan.to_extract else None,
            # quantos arquivos o extrator leu com `encoding` do perfil e não
            # decodificaram, caindo para latin-1 (0 sempre no perfil `ecidade`).
            "encoding_fallback": result.php.get("encoding_fallback", 0),
        },
        nodes=graph.nodes_by_label,
        edges={"by_type": graph.edges_by_type, "by_source": graph.edges_by_source},
        unresolved=dict(sorted(unresolved.counts.items())),
        sql=_sql_summary(result.sql),
        schema=asdict(result.schema) if result.schema is not None else None,
        catalog=_catalog_summary(result.catalog, result.modules),
        search=None,
        analytics=None,
        php_map_version=__version__,
        db=str(options.db_path),
        warnings=warnings,
        project={"slug": project, "name": project_name, "profile": profile.name},
        zend1=asdict(result.zend1) if result.zend1 is not None else None,
        doctrine=asdict(result.doctrine) if result.doctrine is not None else None,
        endpoints=asdict(result.endpoints) if result.endpoints is not None else None,
        routes=asdict(result.routes) if result.routes is not None else None,
        http_calls=asdict(result.http_calls) if result.http_calls is not None else None,
        eloquent=asdict(result.eloquent) if result.eloquent is not None else None,
    )
    _stage(on_stage, "save")
    started = time.perf_counter()
    store.save(
        result.builder,
        options.db_path,
        {
            "source": summary.source,
            "scanned_at": started_at,
            "summary": summary.to_dict(),
            "project": project,
            "project_name": project_name,
            "profile": profile.name,
        },
    )
    durations["save"] = _elapsed(started)
    errors: list[str] = []
    # o texto das funções, como a busca e os agregados, depois do `save`: se falhar,
    # o grafo continua válido. Grava mesmo com `--no-search` (a busca é outro uso do texto).
    started = time.perf_counter()
    routine_texts: list[tuple[str, str]] = []
    try:
        stats, rows = _store_routines(options, profile.name, result, warnings)
        summary.routines = stats
        routine_texts = [(r.node_id, r.ddl) for r in rows]
    except StoreError as exc:
        errors.append(str(exc))
        summary.routines = {"error": str(exc)}
        _warn(warnings, errors[-1])
    durations["routines"] = _elapsed(started)
    # as regras de negócio do texto das funções, lidas uma vez e reusadas na busca
    # (mensagens/comentários) abaixo. Se `_store_routines` falhou, não há `ddl` para ler.
    started = time.perf_counter()
    rule_results: list[tuple[str, list[Rule], RoutineProfile]] = []
    if not errors:
        try:
            rule_results = [(node_id, *scan_routine(ddl)) for node_id, ddl in routine_texts]
            summary.routine_rules = _store_rules(options.db_path, rule_results)
        except StoreError as exc:
            errors.append(str(exc))
            summary.routine_rules = {"error": str(exc)}
            _warn(warnings, errors[-1])
    durations["routine_rules"] = _elapsed(started)
    # campos e consultas por instrução SQL, a partir do mesmo texto e do diretório
    # de tabelas usado no carregamento do schema (só com `--schema-db`/`--schema`; sem ele, nada
    # para resolver coluna sem qualificação).
    started = time.perf_counter()
    try:
        summary.routine_fields = _store_fields(options.db_path, result, rule_results, routine_texts)
    except StoreError as exc:
        errors.append(str(exc))
        summary.routine_fields = {"error": str(exc)}
        _warn(warnings, errors[-1])
    durations["routine_fields"] = _elapsed(started)
    # quais campos o código PHP preenche antes de `incluir`/`alterar` do DAO. Depois
    # do `save` e do `materialize_tables` (as colunas vêm do schema); se falhar, o grafo vale.
    started = time.perf_counter()
    try:
        summary.field_writes = _store_field_writes(options.db_path, result)
    except StoreError as exc:
        errors.append(str(exc))
        summary.field_writes = {"error": str(exc)}
        _warn(warnings, errors[-1])
    durations["field_writes"] = _elapsed(started)
    # fórmulas e regras por assunto, em todas as funções da base, reaproveitando a
    # extração de `analysis/formulas.py`. Depois do `field_writes`; se falhar, o grafo continua
    # válido. Entra no `--progress-json` (etapa visível: até ~20 s no grafo real).
    _stage(on_stage, "formula_index")
    started = time.perf_counter()
    try:
        summary.formula_index = _store_formula_index(options.db_path)
    except StoreError as exc:
        errors.append(str(exc))
        summary.formula_index = {"error": str(exc)}
        _warn(warnings, errors[-1])
    durations["formula_index"] = _elapsed(started)
    if options.search:
        # Depois do `save`, no arquivo definitivo: se falhar, o grafo continua válido.
        _stage(on_stage, "search")
        started = time.perf_counter()
        try:
            texts = {node_id: r.ddl for node_id, r in result.routines}
            rules_by_node = {node_id: rules for node_id, rules, _profile in rule_results}
            docs, search_stats = build_documents(
                records, result.builder, settings, texts, rules_by_node
            )
            write_index(options.db_path, docs, search_stats)
            summary.search = asdict(search_stats)
        except StoreError as exc:
            errors.append(f"índice de busca não gravado: {exc}")
            summary.search = {"error": str(exc)}
            _warn(warnings, errors[-1])
        durations["search"] = _elapsed(started)
    if options.analytics:
        # como a busca, depois do `save` e numa transação própria.
        _stage(on_stage, "analytics")
        started = time.perf_counter()
        try:
            analytics = build_analytics(result.builder, settings)
            write_analytics(options.db_path, analytics)
            summary.analytics = asdict(analytics.stats)
            summary.routine_origins = _store_origins(options.db_path, analytics, result)
        except StoreError as exc:
            errors.append(f"agregados não gravados: {exc}")
            summary.analytics = {"error": str(exc)}
            _warn(warnings, errors[-1])
        durations["analytics"] = _elapsed(started)
    text = json.dumps(summary.to_dict(), ensure_ascii=False, indent=2) + "\n"
    summary_path = companion(options.db_path, options.out_dir, SUMMARY_NAME)
    summary_path.write_text(text, encoding="utf-8")
    fsperm.secure_file(summary_path)
    if errors:
        raise StoreError("; ".join(errors))
    return summary


def _store_routines(
    options: ScanOptions, profile: str, result: GraphResult, warnings: list[str]
) -> tuple[dict[str, Any], list[routine_store.RoutineSource]]:
    """Grava `routine_sources` e devolve o resumo (`ScanSummary.routines`) e as linhas gravadas
    (reusadas pelo leitor de regras, para não ler o `ddl` de novo)."""
    schema = result.schema
    if schema is None or schema.source == "files":
        message = None
        if profile == DEFAULT_PROFILE:
            message = (
                "sem o texto das funções da base: o schema não veio do banco "
                "(use --schema-db ou defina PHP_MAP_CATALOG_DSN)"
            )
            _warn(warnings, message)
        stats = routine_store.RoutineStats(0, 0, "none", None, 0, message)
        return asdict(stats), []
    read_at = result.routines_read_at or routine_store.now_iso()
    rows = [
        routine_store.make_source(
            node_id, r.schema, r.name, _language(result, node_id), r.ddl, read_at
        )
        for node_id, r in result.routines
    ]
    routine_store.write_routines(options.db_path, rows)
    without = schema.routines_without_node
    message = None
    if without:
        shown = ", ".join(without[:20]) + ("…" if len(without) > 20 else "")
        message = f"{len(without)} função(ões) sem texto no grafo (o parse falhou): {shown}"
        _warn(warnings, message)
    stats = routine_store.RoutineStats(
        stored=len(rows),
        bytes=sum(len(r.ddl.encode("utf-8")) for r in rows),
        source="db",
        read_at=read_at,
        without_node=len(without),
        warning=message,
    )
    return asdict(stats), rows


def _store_rules(
    db_path: Path, rule_results: list[tuple[str, list[Rule], RoutineProfile]]
) -> dict[str, Any]:
    """Grava `routine_rules`/`routine_profile` e devolve o resumo do scan."""
    seconds = routine_store.write_rules(db_path, rule_results)
    totals: Counter[str] = Counter()
    for _node_id, rules, _profile in rule_results:
        totals.update(r.kind for r in rules)
    return {
        "functions": len(rule_results),
        "rules": sum(totals.values()),
        "by_kind": dict(sorted(totals.items())),
        "seconds": seconds,
    }


def _store_fields(
    db_path: Path,
    result: GraphResult,
    rule_results: list[tuple[str, list[Rule], RoutineProfile]],
    routine_texts: list[tuple[str, str]],
) -> dict[str, Any]:
    """Grava `routine_queries`/`routine_fields` e devolve o resumo do scan.

    Precisa do `TableDirectory` do carregamento do schema (`--schema`/`--schema-db`): sem ele,
    não há como resolver coluna sem qualificação, e a etapa não faz nada
    (como a leitura de regras sem texto de função)."""
    directory = result.directory
    empty = {
        "functions": 0, "queries": 0, "fields": 0, "functions_with_fields": 0, "dynamic": 0,
        "parse_fallback": 0, "column_not_found": 0, "ambiguous": 0, "seconds": 0.0,
    }  # fmt: skip
    if directory is None or not routine_texts:
        return empty
    profile_by_node = {node_id: profile for node_id, _rules, profile in rule_results}
    trigger_tables_of: dict[str, list[str]] = {}
    for edge in result.builder.iter_edges():
        if edge.type == EdgeType.TRIGGERS:
            trigger_tables_of.setdefault(edge.dst, []).append(edge.src)

    started = time.perf_counter()
    query_rows: list[routine_store.QueryRow] = []
    functions_with_fields = 0
    dynamic_count = parse_fallback_count = column_not_found_count = ambiguous_count = 0
    functions_with_queries: set[str] = set()
    for node_id, ddl in routine_texts:
        profile = profile_by_node.get(node_id)
        is_trigger = profile is not None and profile.returns == "trigger"
        statements, datum_vars = sql_fields.statements_of(ddl, is_trigger=is_trigger)
        if not statements:
            continue
        variables: set[str] = set(datum_vars)
        if profile is not None:
            variables |= {str(p[0]).lower() for p in profile.params if p[0]}
            variables |= {v.lower() for v in profile.declared}
        trigger_tables = tuple(trigger_tables_of.get(node_id, ()))
        had_fields = False
        for seq, raw in enumerate(statements):
            functions_with_queries.add(node_id)
            if raw.dynamic:
                dynamic_count += 1
                facts = sql_fields.StatementFacts("dynamic", "", (), (), (), "", 0.0)
            else:
                facts = sql_fields.analyze_statement(
                    raw.text,
                    directory=directory,
                    variables=variables,
                    trigger_tables=trigger_tables,
                )
                if facts.kind == "parse_fallback":
                    parse_fallback_count += 1
                column_not_found_count += len(facts.unresolved)
                ambiguous_count += sum(1 for f in facts.fields if f.confidence < 1.0)
                had_fields = had_fields or bool(facts.fields)
            end_line = raw.line + raw.text.count("\n")
            query_rows.append(routine_store.QueryRow(node_id, seq, raw.line, end_line, facts))
        if had_fields:
            functions_with_fields += 1
    seconds = routine_store.write_fields(db_path, query_rows) if query_rows else 0.0
    return {
        "functions": len(functions_with_queries),
        "queries": len(query_rows),
        "fields": sum(len(r.facts.fields) for r in query_rows),
        "functions_with_fields": functions_with_fields,
        "dynamic": dynamic_count,
        "parse_fallback": parse_fallback_count,
        "column_not_found": column_not_found_count,
        "ambiguous": ambiguous_count,
        "seconds": round(time.perf_counter() - started, 2) + seconds,
    }


def _store_field_writes(db_path: Path, result: GraphResult) -> dict[str, Any] | None:
    """Monta e grava `php_field_writes` e devolve o resumo do scan."""
    collector = result.field_writes
    if collector is None:
        return None
    view = field_writes_store.view_from_builder(result.builder, collector.pending)
    rows, stats = field_writes_store.build_field_writes(
        collector.pending, view, collector.form_fields
    )
    with closing(sqlite3.connect(db_path)) as conn:
        stored = field_writes_store.store_field_writes(conn, rows)
    return {
        "rows": stored["rows"],
        "atribuicao": stored[field_writes_store.HOW_ASSIGN],
        "formulario": stored[field_writes_store.HOW_FORM],
        "not_column": stats.not_column,
        "calls_with_sets": stats.calls_with_sets,
        "calls_with_form": stats.calls_with_form,
        "no_columns": stats.no_columns,
        "calls": len(collector.pending),
    }


def _store_formula_index(db_path: Path) -> dict[str, Any] | None:
    """Grava `routine_formulas`/`routine_rule_topics` e devolve o resumo do scan.
    `None` sem o texto das funções (nada a indexar; o scan já avisa sobre isso)."""
    with closing(sqlite3.connect(db_path)) as conn:
        if not routine_store.has_routines(conn):
            return None
        return formula_index_store.build_formula_index(conn)


def _store_origins(
    db_path: Path, analytics: Analytics, result: GraphResult
) -> dict[str, Any] | None:
    """Menções por texto das funções sem origem (precisam do índice de busca) e o resumo."""
    origins = analytics.routine_origins
    if origins is None:
        return None
    started = time.perf_counter()
    with closing(sqlite3.connect(db_path)) as conn:
        origins.mentions = routine_origin.find_mentions(conn, origins, result.builtin_functions)
    write_mentions(db_path, origins.mentions)
    origins.seconds = round(origins.seconds + time.perf_counter() - started, 2)
    return origins.summary()


def _probe_schema_db(dsn: str) -> None:
    try:
        check_reachable(dsn)
    except ConfigError as exc:
        raise ConfigError(f"base inacessível ({exc}). {UNREACHABLE_HINT}") from None


def _language(result: GraphResult, node_id: str) -> str:
    try:
        return str(result.builder.node_attrs(node_id).get("language") or "sql")
    except KeyError:
        return "sql"


def companion(db: Path, out_dir: Path, name: str) -> Path:
    """Onde ficam o resumo e as pendências do scan que gravou `db`.

    `<out>/<name>` quando o banco fica na própria pasta de saída (o padrão de sempre); senão,
    ao lado do banco, com o nome dele como prefixo (`grafos/<stem>.unresolved.jsonl`). Assim
    cada grafo da interface tem o seu, e o `<out>` não é sobrescrito.
    """
    if db.resolve().parent == out_dir.resolve():
        return out_dir / name
    return db.parent / f"{db.stem}.{name}"


def pending_path(db: Path) -> Path:
    """As pendências do banco `db`: `<stem>.unresolved.jsonl` ao lado dele, se existir; senão o
    `unresolved.jsonl` da pasta dele (banco na pasta de saída, ou gravado por uma versão antiga)."""
    beside = db.parent / f"{db.stem}.{UNRESOLVED_NAME}"
    return beside if beside.is_file() else db.parent / UNRESOLVED_NAME


def _sql_summary(stats: SqlStats) -> dict[str, Any]:
    data = asdict(stats)
    data["parsed"] = dict(sorted(stats.parsed.items()))
    return data


def _catalog_summary(
    stats: tuple[CatalogReadStats, CatalogStats] | None, modules: ModuleStats | None
) -> dict[str, Any] | None:
    """`read` (fonte e linhas por tabela, nunca o DSN), `graph` e `modules`."""
    if stats is None:
        return None
    read, graph = stats
    return {
        "read": asdict(read),
        "graph": asdict(graph),
        "modules": asdict(modules) if modules is not None else None,
    }


def _stage(callback: StageCallback | None, name: str) -> None:
    logger.debug("estágio: %s", name)
    if callback is not None:
        callback(name)


def _warn(warnings: list[str], message: str) -> None:
    logger.warning(message)
    warnings.append(message)


def _elapsed(started: float) -> float:
    return round(time.perf_counter() - started, 2)
