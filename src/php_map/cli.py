"""CLI `php-map` (Typer)."""

import io
import json
import logging
import sqlite3
import sys
import time
from contextlib import closing
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import Any

import typer
from rich.console import Console
from rich.logging import RichHandler
from rich.markup import escape
from rich.progress import Progress, SpinnerColumn, TextColumn, TimeElapsedColumn
from rich.table import Table

from php_map import fsperm, pipeline
from php_map.analysis import layers as layers_analysis
from php_map.analysis import value_origin
from php_map.analysis.cross import (
    DEFAULT_DEPTH,
    STATUSES,
    CrossReport,
    cross_projects,
    project_label,
    trace_across,
)
from php_map.analysis.impact import ImpactOptions, impact
from php_map.analysis.trace import TraceOptions, trace
from php_map.config import (
    CATALOG_DSN_ENV,
    DEFAULT_PROFILE,
    PHP_IMAGE_ENV,
    PHP_IMAGE_OWN,
    PHP_IMAGE_WINDOWS,
    Settings,
    env_value,
    job_id,
)
from php_map.errors import ConfigError, PhpMapError, UsageError
from php_map.graph import formula_index as formula_index_store
from php_map.graph import store
from php_map.graph.model import key_of
from php_map.ingest import php_extractor
from php_map.profiles.base import PROFILES, get_profile
from php_map.report import cross as cross_report
from php_map.report import layers as layers_report
from php_map.report import render
from php_map.search.query import search

# Sem as variáveis locais no traceback: o DSN do catálogo pode ter a senha.
app = typer.Typer(
    help="Mapeador de sistemas PHP legados",
    no_args_is_help=True,
    pretty_exceptions_show_locals=False,
)
console = Console(stderr=True)
logger = logging.getLogger(__name__)


def _configure_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(message)s",
        handlers=[RichHandler(console=console, show_path=False)],
        force=True,
    )


@app.callback()
def main(
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Exibe logs de depuração."),
) -> None:
    """Mapeador de sistemas PHP legados: AST de PHP/SQL em um grafo de conhecimento."""
    fsperm.apply_umask()  # arquivos novos só do dono: grafo, cache, cofre e configurações
    _configure_logging(verbose)


# Na ordem em que o `pipeline.run_scan` executa (a interface mostra nessa ordem).
STAGE_LABELS = {
    "discovery": "Descobrindo arquivos PHP",
    "hash": "Conferindo o cache de AST (sha1)",
    "extract": "Extraindo AST PHP",
    "schema": "Carregando o schema SQL",
    "resolve_php": "Resolvendo includes, chamadas e classes",
    "routes": "Ligando as rotas do Laravel aos controllers",  # só no perfil ecidade
    "http_calls": "Ligando telas e JavaScript às rotas da API",  # só no perfil ecidade
    "zend1_structure": "Ligando módulos, rotas e telas do Zend 1",  # só no perfil zend1
    "doctrine": "Ligando entidades e DQL do Doctrine",  # só no perfil zend1
    "eloquent": "Ligando models do Eloquent às tabelas",  # só no perfil ecidade
    "sql": "Ligando o SQL das strings às tabelas",
    "endpoints": "Ligando os endpoints (SOAP, RPC e chamadas para fora)",
    "catalog": "Aplicando o dicionário de dados e os menus",
    "modules": "Classificando módulos por regra de nome",
    "save": "Gravando o grafo",
    "formula_index": "Indexando as fórmulas e regras por assunto",
    "search": "Construindo o índice de busca",
    "analytics": "Calculando os agregados (acoplamento entre módulos)",
}


CATALOG_HELP = (
    "Dicionário de dados e menus do e-cidade a partir de arquivos (repetível): .sql ou "
    "diretório de incrementais (testes e fixtures). Para a base real, use --catalog-db."
)
SCHEMA_DB_HELP = (
    "Lê tabelas, funções e triggers do banco do catálogo (--catalog-db ou "
    f"{CATALOG_DSN_ENV}; somente leitura, só metadado). Excludente com --schema. "
    "Sem esta opção e sem --schema, o perfil ecidade com o DSN definido lê do banco (e grava o "
    "texto das funções); --no-schema-db escaneia sem isso."
)
PHP_IMAGE_HELP = (
    f"Imagem Docker do extrator PHP (PHP 7.4 com composer). Sem esta opção: {PHP_IMAGE_ENV} "
    f"(variável ou .env), senão {PHP_IMAGE_WINDOWS} se existir, senão {PHP_IMAGE_OWN}."
)
PROGRESS_JSON_HELP = (
    "Uma linha JSON por evento no stdout (start, stage, done ou error), sem spinner; os logs "
    "e o resumo vão para o stderr. Usado pela interface (php-map ui)."
)
CATALOG_DB_HELP = (
    "Lê o dicionário e os menus direto do banco (DSN do PostgreSQL, sessão somente leitura, "
    "só as 8 tabelas de sistema). Sem esta opção e sem --catalog, usa "
    f"{CATALOG_DSN_ENV} (variável ou .env), se houver. O DSN não vai para o resumo."
)
PROFILE_HELP = (
    "Perfil do sistema mapeado: "
    f"{', '.join(sorted(PROFILES))}. Decide o marcador da pasta, as extensões, as exclusões, "
    "o encoding e a resolução de classe/módulo."
)


@app.command()
def scan(
    source: Path = typer.Option(..., "--source", help="Raiz do e-cidade (somente leitura)."),
    schema: list[Path] = typer.Option(
        [], "--schema", help="DDL: arquivo .sql, pg_dump ou diretório de incrementais (repetível)."
    ),
    schema_db: bool | None = typer.Option(
        None, "--schema-db/--no-schema-db", help=SCHEMA_DB_HELP, show_default=False
    ),
    catalog: list[Path] = typer.Option([], "--catalog", help=CATALOG_HELP),
    catalog_db: str | None = typer.Option(
        None, "--catalog-db", help=CATALOG_DB_HELP, show_default=False
    ),
    no_catalog_db: bool = typer.Option(
        False, "--no-catalog-db", help=f"Ignora {CATALOG_DSN_ENV} (scan sem catálogo)."
    ),
    db: Path | None = typer.Option(None, "--db", help="Banco do grafo (padrão: <out>/graph.db)."),
    out: Path = typer.Option(Path("out"), "--out", help="Diretório de saída."),
    reuse_ast: bool = typer.Option(
        False, "--reuse-ast", help="Cache incremental: só reextrai arquivos novos ou alterados."
    ),
    exclude: list[str] = typer.Option([], "--exclude", help="Diretório a ignorar (repetível)."),
    limit: int | None = typer.Option(None, "--limit", min=1, help="Só os N primeiros arquivos."),
    php_image: str | None = typer.Option(None, "--php-image", help=PHP_IMAGE_HELP),
    no_search: bool = typer.Option(
        False, "--no-search", help="Não constrói o índice de busca textual (comando search)."
    ),
    no_analytics: bool = typer.Option(
        False, "--no-analytics", help="Não calcula os agregados (acoplamento, pontos quentes)."
    ),
    progress_json: bool = typer.Option(
        False, "--progress-json", help=PROGRESS_JSON_HELP, show_default=False
    ),
    profile: str = typer.Option(DEFAULT_PROFILE, "--profile", help=PROFILE_HELP),
    project: str | None = typer.Option(
        None,
        "--project",
        help="Slug do projeto (padrão: nome da pasta normalizado).",
        show_default=False,
    ),
    project_name: str | None = typer.Option(
        None,
        "--project-name",
        help="Nome do projeto (padrão: o próprio slug).",
        show_default=False,
    ),
) -> None:
    """Varre o e-cidade (ou outro sistema, com --profile) e monta o grafo."""
    try:
        get_profile(profile)
    except ValueError as exc:
        # Erro de uso (nome inexistente), não de configuração do scan: código 2.
        console.print(f"[red]Erro:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    events = _JsonEvents() if progress_json else None
    started = time.perf_counter()
    schema_dsn: str | None = None
    try:
        if catalog and catalog_db:
            raise ConfigError("use --catalog ou --catalog-db, não os dois")
        if not catalog and catalog_db is None and not no_catalog_db:
            catalog_db = env_value(CATALOG_DSN_ENV)
            if catalog_db:
                console.print(f"Catálogo: lido do banco em {CATALOG_DSN_ENV}")
        # O schema do banco não depende do catálogo (`--no-catalog-db`, `--catalog`).
        schema_dsn = catalog_db or env_value(CATALOG_DSN_ENV)
        # Sem a opção, o scan do e-cidade lê o schema (e o texto das funções)
        # do banco sempre que há DSN e não há `--schema` de arquivos.
        auto = schema_db is None and profile == DEFAULT_PROFILE and not schema and bool(schema_dsn)
        use_schema_db = bool(schema_db) or auto
        if not use_schema_db:
            schema_dsn = None
        elif auto:
            console.print(f"Schema e funções: lidos do banco em {CATALOG_DSN_ENV}")
        options = pipeline.ScanOptions(
            source=source,
            schema=tuple(schema),
            catalog=tuple(catalog),
            catalog_db=catalog_db or None,
            schema_db=use_schema_db,
            schema_dsn=schema_dsn or None,
            schema_db_auto=auto,
            out_dir=out,
            db=db,
            reuse_ast=reuse_ast,
            exclude=tuple(exclude),
            limit=limit,
            php_image=php_image or None,
            search=not no_search,
            analytics=not no_analytics,
            profile=profile,
            project=project,
            project_name=project_name,
        )
        pipeline.validate_source(source, profile)  # antes do spinner: erro de uso sai limpo
        if events is not None:
            events.emit(
                "start",
                job=job_id(),
                source=str(source.resolve()),
                db=str(options.db_path.resolve()),
            )
            summary = pipeline.run_scan(options, on_stage=events.stage)
        else:
            summary = _run_with_spinner(options)
    except PhpMapError as exc:
        _fail(str(exc), events, catalog_db or schema_dsn)
        raise typer.Exit(code=1) from exc
    except Exception as exc:
        if events is None:
            raise
        # O processo pai (interface) só vê o evento: a mensagem, sem o DSN, e o tipo do erro.
        _fail(f"erro inesperado ({type(exc).__name__}): {exc}", events, catalog_db or schema_dsn)
        logger.debug("detalhes do erro", exc_info=True)
        raise typer.Exit(code=1) from exc
    _print_summary(summary)  # no stderr: com --progress-json o stdout tem só os eventos
    if events is not None:
        events.emit(
            "done",
            db=str(Path(summary.db).resolve()),
            seconds=round(time.perf_counter() - started, 1),
            summary=_done_summary(summary),
        )


def _run_with_spinner(options: pipeline.ScanOptions) -> pipeline.ScanSummary:
    progress = Progress(
        SpinnerColumn(), TextColumn("{task.description}"), TimeElapsedColumn(), console=console
    )
    stage_task = progress.add_task("Preparando", total=None)

    def on_stage(name: str) -> None:
        progress.update(stage_task, description=STAGE_LABELS.get(name, name))

    with progress:
        return pipeline.run_scan(options, progress=progress, on_stage=on_stage)


class _JsonEvents:
    """`scan --progress-json`: uma linha JSON por evento no stdout, com `flush`.

    ASCII puro (`ensure_ascii`): o console do Windows é cp1252, e quem lê é um programa.
    """

    def emit(self, event: str, **fields: object) -> None:
        now = datetime.now().astimezone().isoformat(timespec="seconds")
        line = json.dumps({"event": event, **fields, "at": now}, ensure_ascii=True)
        sys.stdout.write(line + "\n")
        sys.stdout.flush()

    def stage(self, name: str) -> None:
        self.emit("stage", stage=name, label=STAGE_LABELS.get(name, name))


def _fail(message: str, events: _JsonEvents | None, dsn: str | None) -> None:
    if dsn:  # defesa: as mensagens do catálogo já saem sem o DSN
        message = message.replace(dsn, "<DSN>")
    console.print(f"[red]Erro:[/red] {escape(message)}")
    if events is not None:
        events.emit("error", message=message)


def _done_summary(summary: pipeline.ScanSummary) -> dict[str, object]:
    graph = (summary.catalog or {}).get("graph") or {}
    return {
        "nodes": sum(summary.nodes.values()),
        "edges": sum(summary.edges["by_type"].values()),
        "modules": graph.get("modules", summary.nodes.get("Module", 0)),
        "screens_in_menu": (summary.analytics or {}).get("screens_in_menu"),
        "files": summary.files["total"],
        "routines": summary.routines,
        "warnings": summary.warnings,
    }


def _print_summary(summary: pipeline.ScanSummary) -> None:
    table = Table(title="Resumo do scan", show_header=False)
    files = summary.files
    cache = files["cache"]
    table.add_row("Origem", summary.source)
    project = summary.project
    table.add_row("Projeto", f"{project['name']} ({project['slug']}) · perfil {project['profile']}")
    table.add_row(
        "Arquivos",
        f"{files['total']} ({', '.join(f'{k}={v}' for k, v in files['by_status'].items())})",
    )
    if files.get("encoding_fallback"):  # só aparece quando houve fallback
        table.add_row("Encoding", f"{files['encoding_fallback']} arquivo(s) caíram para latin-1")
    table.add_row(
        "Cache de AST",
        f"{cache['reason']}: {cache['reused']} reaproveitados, {cache['extracted']} extraídos, "
        f"{cache['removed']} removidos",
    )
    routines = summary.routines
    if routines is not None and "error" not in routines:
        if routines["source"] == "db":
            table.add_row(
                "Funções com texto",
                f"{routines['stored']:,} ({routines['bytes'] / 1_000_000:.1f} MB)".replace(
                    ",", "."
                ),
            )
        elif routines.get("warning"):
            table.add_row("Funções com texto", f"[yellow]{escape(routines['warning'])}[/yellow]")
    table.add_row("Nós", str(sum(summary.nodes.values())))
    table.add_row("Arestas", str(sum(summary.edges["by_type"].values())))
    table.add_row("Pendências", str(sum(summary.unresolved.values())))
    if summary.catalog is not None:
        graph = summary.catalog["graph"]
        table.add_row(
            "Catálogo",
            f"{graph['modules']} módulos, {graph['tables_with_module']} tabelas com módulo, "
            f"{graph['menu_actions']} itens de menu ({graph['menu_missing_files']} sem arquivo)"
            f" · fonte {summary.catalog['read']['source']}",
        )
        dictionary_only, menu_only = graph["modules_dictionary_only"], graph["modules_menu_only"]
        if dictionary_only or menu_only:  # sinal para revisar MODULE_ALIASES
            table.add_row(
                "Módulos só num cadastro",
                f"dicionário {len(dictionary_only)} ({_first(dictionary_only)}); "
                f"menu {len(menu_only)} ({_first(menu_only)})",
            )
        modules = summary.catalog.get("modules")
        if modules is not None:
            table.add_row(
                "Módulo por nome",
                f"{modules['screens_classified']} telas ({modules['prefixes_voted']} prefixos), "
                f"{modules['daos_classified']} DAOs; {modules['files_without_module']} arquivos "
                "sem módulo",
            )
    table.add_row("Tempo (s)", ", ".join(f"{k}={v}" for k, v in summary.duration_s.items()))
    table.add_row("Banco", summary.db)
    if summary.warnings:  # já saíram no log; aqui só o total, para não sumirem na rolagem
        table.add_row("Avisos", str(len(summary.warnings)))
    console.print(table)


def _first(names: list[str], limit: int = 5) -> str:
    shown = ", ".join(names[:limit])
    return f"{shown}…" if len(names) > limit else shown or "—"


DEFAULT_DB = Path("out/graph.db")
MIN_HELP = "Ignora arestas com confiança menor."
INFRA_HELP = "Atravessa e exibe a infraestrutura (db_query, libs/db_stdlib.php...)."
MERMAID_HELP = "Máximo de nós no diagrama."
OUTPUT_HELP = "Arquivo de saída (padrão: ao lado do banco)."
CROSS_TRACE_HELP = (
    "Banco de outro projeto (repetível). Ao chegar num Endpoint que este grafo só chama, o "
    "trace continua no banco que o serve; a travessia aparece marcada com ~>."
)


@app.command("trace")
def trace_command(
    entry: str = typer.Option(..., "--entry", help="Arquivo de entrada (ex.: emp4_x001.php)."),
    depth: int = typer.Option(8, "--depth", min=1, help="Profundidade máxima."),
    min_confidence: float = typer.Option(0.0, "--min-confidence", min=0.0, max=1.0, help=MIN_HELP),
    include_infra: bool = typer.Option(False, "--include-infra", help=INFRA_HELP),
    expand_pgfunctions: bool = typer.Option(
        False, "--expand-pgfunctions", help="Segue as arestas das funções do banco."
    ),
    max_mermaid_nodes: int = typer.Option(60, "--max-mermaid-nodes", min=2, help=MERMAID_HELP),
    db: Path = typer.Option(DEFAULT_DB, "--db", help="Banco do grafo."),
    cross: list[Path] = typer.Option([], "--cross", help=CROSS_TRACE_HELP),
    output: Path | None = typer.Option(None, "--output", help=OUTPUT_HELP),
) -> None:
    """Rastreia o fluxo de um arquivo de entrada até o banco."""
    options = TraceOptions(entry, depth, min_confidence, include_infra, expand_pgfunctions)
    crossed = None
    try:
        graph, meta = store.load(db), store.load_meta(db)
        if cross:  # continua nos outros projetos ao chegar num Endpoint
            crossed = trace_across(graph, project_label(db, meta)[0], options, cross)
            result = crossed.primary
        else:
            result = trace(graph, options)
        pending = render.read_pending(pipeline.pending_path(db), result.file_paths)
        text = render.render_trace(result, meta, pending, max_mermaid_nodes, crossed)
    except UsageError as exc:
        console.print(f"[red]Erro:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    except PhpMapError as exc:
        console.print(f"[red]Erro:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    target = output or db.parent / render.output_name("trace", entry)
    _write(target, text)
    per_op = ", ".join(f"{op} {n}" for op, n in result.tables_by_operation().items()) or "nenhuma"
    console.print(
        f"{len(result.walk.steps)} nós · {result.count('File')} arquivos · "
        f"tabelas: {per_op} · RPC: {result.rpc_count()}"
    )
    if crossed is not None:
        tables = {t.table for c in crossed.crossings for t in c.tables}
        console.print(
            f"travessias: {len(crossed.crossings)} (tabelas do outro lado: {len(tables)}) · "
            f"endpoints sem servidor: {len(crossed.unserved)}"
        )
    console.print(f"Relatório: [bold]{target}[/bold]")


@app.command("impact")
def impact_command(
    table: str = typer.Option(..., "--table", help="Tabela (sem schema)."),
    schema: str | None = typer.Option(
        None,
        "--schema",
        help="Schema da tabela (padrão: o único schema que tem a tabela no grafo).",
        show_default=False,
    ),
    depth: int = typer.Option(10, "--depth", min=1, help="Profundidade máxima."),
    min_confidence: float = typer.Option(0.0, "--min-confidence", min=0.0, max=1.0, help=MIN_HELP),
    include_infra: bool = typer.Option(False, "--include-infra", help=INFRA_HELP),
    export: str = typer.Option("md", "--export", help="md (relatório) ou mermaid (só o diagrama)."),
    max_mermaid_nodes: int = typer.Option(60, "--max-mermaid-nodes", min=2, help=MERMAID_HELP),
    db: Path = typer.Option(DEFAULT_DB, "--db", help="Banco do grafo."),
    output: Path | None = typer.Option(None, "--output", help=OUTPUT_HELP),
) -> None:
    """Lista o que é impactado por uma tabela."""
    if export not in ("md", "mermaid"):
        console.print("[red]Erro:[/red] --export aceita md ou mermaid")
        raise typer.Exit(code=1)
    options = ImpactOptions(table, schema, depth, min_confidence, include_infra)
    try:
        graph, meta = store.load(db), store.load_meta(db)
        result = impact(graph, options)
        text = render.render_impact(result, meta, max_mermaid_nodes, export)
    except PhpMapError as exc:
        console.print(f"[red]Erro:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    target = output or db.parent / render.output_name("impact", key_of(result.target))
    _write(target, text)
    screens = sum(1 for f in result.files if f.is_screen)
    console.print(
        f"{len(result.direct)} tocam direto · {len(result.dao_users)} usam pelo DAO · "
        f"{len(result.files)} arquivos afetados ({screens} telas) · "
        f"{len(result.code)} métodos/funções"
    )
    console.print(f"Relatório: [bold]{target}[/bold]")


CROSS_FORMATS = ("table", "json", "csv")
PROTOCOLS = ("soap", "rpc", "http")
CROSS_STATUS_COLORS = {
    "casado": "green",
    "chamado_sem_servidor": "red",
    "servido_sem_cliente": "yellow",
}
CROSS_LIMIT_HELP = "Linhas da tabela na tela (0 = todas); json e csv saem sempre inteiros."


@app.command("cross")
def cross_command(
    graph: list[Path] = typer.Option(
        [], "--graph", help="Banco do grafo de um projeto (repetir: 2 ou mais)."
    ),
    depth: int = typer.Option(
        DEFAULT_DEPTH, "--depth", min=0, help="Profundidade do alcance no projeto que serve."
    ),
    fmt: str = typer.Option("table", "--format", help="table, json ou csv."),
    out: Path | None = typer.Option(None, "--out", help="Grava a saída neste arquivo."),
    only: str | None = typer.Option(
        None, "--only", help=f"Só este status: {', '.join(STATUSES)}.", show_default=False
    ),
    system: str | None = typer.Option(
        None, "--system", help="Só endpoints deste sistema (quem serve).", show_default=False
    ),
    protocol: str | None = typer.Option(
        None, "--protocol", help="Só este protocolo: soap, rpc ou http.", show_default=False
    ),
    limit: int = typer.Option(40, "--limit", min=0, help=CROSS_LIMIT_HELP),
) -> None:
    """Cruza os grafos de 2 ou mais projetos pelo ID do Endpoint.

    Mostra o que um projeto usa do outro, o que é chamado e ninguém serve, e o que é servido sem
    cliente. Só lê os bancos; nada é gravado neles.
    """
    problems = [
        message
        for ok, message in (
            (fmt in CROSS_FORMATS, f"--format aceita {', '.join(CROSS_FORMATS)}"),
            (only is None or only in STATUSES, f"--only aceita {', '.join(STATUSES)}"),
            (
                protocol is None or protocol in PROTOCOLS,
                f"--protocol aceita {', '.join(PROTOCOLS)}",
            ),
        )
        if not ok
    ]
    if problems:
        console.print(f"[red]Erro:[/red] {'; '.join(problems)}")
        raise typer.Exit(code=2)
    try:
        report = cross_projects(graph, depth).filtered(only, system, protocol)
    except UsageError as exc:
        console.print(f"[red]Erro:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    except PhpMapError as exc:
        console.print(f"[red]Erro:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    for warning in report.warnings:
        console.print(f"[yellow]Aviso:[/yellow] {escape(warning)}")
    if fmt == "table":
        _emit_cross_table(report, limit, out)
        return
    text = cross_report.to_json(report) if fmt == "json" else cross_report.to_csv(report)
    if out is not None:
        _write(out, text)
        console.print(f"Relatório: [bold]{out}[/bold]")
    else:
        sys.stdout.buffer.write(text.encode("utf-8"))  # UTF-8 mesmo num console cp1252
        sys.stdout.flush()
    for line in cross_report.summary_lines(report):  # no stderr: o stdout tem só o dado
        console.print(escape(line), highlight=False)


def _cross_table(report: CrossReport, limit: int) -> Table:
    table = Table(title="Endpoints cruzados entre projetos", expand=False)
    table.add_column("Endpoint", overflow="fold", min_width=24)
    table.add_column("Status", no_wrap=True)
    table.add_column("Serve")
    table.add_column("Chamadas", justify="right")
    table.add_column("Módulos que chamam", overflow="fold")
    table.add_column("Tabelas", justify="right")
    shown = report.endpoints if limit == 0 else report.endpoints[:limit]
    for e in shown:
        color = CROSS_STATUS_COLORS[e.status]
        modules = ", ".join(sorted({cross_report.short(c.module) for c in e.callers}))
        table.add_row(
            f"{e.protocol}:{e.name}",
            f"[{color}]{e.status}[/{color}]",
            e.served_by or "—",
            str(e.calls) if e.callers else "—",
            modules or "—",
            str(len(e.reaches.tables)) if e.served_by else "—",
        )
    if len(shown) < len(report.endpoints):
        table.caption = (
            f"{len(shown)} de {len(report.endpoints)} endpoints (--limit 0 mostra todos; "
            "--format json|csv traz tudo)"
        )
    return table


def _emit_cross_table(report: CrossReport, limit: int, out: Path | None) -> None:
    lines = cross_report.summary_lines(report)
    if out is None:
        screen = Console()
        screen.print(_cross_table(report, limit))
        for line in lines:
            screen.print(escape(line), highlight=False)
        return
    buffer = io.StringIO()
    printer = Console(file=buffer, width=200, color_system=None)
    printer.print(_cross_table(report, 0))  # em arquivo, sempre inteiro
    for line in lines:
        printer.print(escape(line), highlight=False)
    _write(out, buffer.getvalue())
    console.print(f"Relatório: [bold]{out}[/bold]")
    for line in lines:
        console.print(escape(line), highlight=False)


def _write(target: Path, text: str) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")


LABELS = ("File", "Table", "BusinessAction", "Endpoint", "Route")


@app.command("search")
def search_command(
    text: str = typer.Argument(..., help='Termos de negócio (ex.: "anulação de empenho").'),
    label: str | None = typer.Option(
        None, "--label", help="File, Table, BusinessAction, Endpoint, Route."
    ),
    module: str | None = typer.Option(None, "--module", help="Só nós deste módulo."),
    limit: int = typer.Option(20, "--limit", min=1, help="Máximo de resultados."),
    any_term: bool = typer.Option(False, "--any", help="Basta um dos termos (padrão: todos)."),
    as_json: bool = typer.Option(False, "--json", help="Imprime os resultados em JSON."),
    db: Path = typer.Option(DEFAULT_DB, "--db", help="Banco do grafo."),
) -> None:
    """Busca telas, tabelas e itens de menu por termos de negócio.

    Sem stemmer de português: os termos perdem sufixos comuns e são buscados por prefixo
    (anulação → anula*), e nomes compostos são achados por trecho (anularempenho001).
    """
    if label is not None and label not in LABELS:
        console.print(f"[red]Erro:[/red] --label aceita {', '.join(LABELS)}")
        raise typer.Exit(code=1)
    try:
        hits = search(db, text, label=label, module=module, limit=limit, any_term=any_term)
    except PhpMapError as exc:
        console.print(f"[red]Erro:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if as_json:
        typer.echo(json.dumps([asdict(h) for h in hits], ensure_ascii=False, indent=2))
        return
    if not hits:
        console.print(f"Nenhum resultado para [bold]{text}[/bold].")
        return
    table = Table(title=f"Busca: {text}")
    table.add_column("#")
    table.add_column("Nó", overflow="fold")  # o caminho nunca é cortado com reticências
    table.add_column("Label")
    table.add_column("Módulos")
    table.add_column("Trecho")
    for position, hit in enumerate(hits, 1):
        node = hit.node_id
        if hit.label == "BusinessAction" and hit.next_command:
            node += f"\n→ {hit.next_command.rsplit(' ', 1)[-1]}"  # o arquivo que o menu abre
        snippet = escape(" ".join(hit.snippet.split()))  # `[x]` é o termo casado, não markup
        table.add_row(str(position), escape(node), hit.label, ", ".join(hit.modules), snippet)
    Console().print(table)
    if hits[0].next_command:
        console.print(f"Próximo passo: [bold]{hits[0].next_command}[/bold]")


@app.command("boundary")
def boundary_command(
    db: Path = typer.Option(DEFAULT_DB, "--db", help="Banco do grafo do projeto."),
    cross: list[Path] = typer.Option(
        [], "--cross", help="Grafo de outro projeto (repetível): costura e chamadores."
    ),
    module: str | None = typer.Option(
        None, "--module", help="Cartão de fronteira deste módulo.", show_default=False
    ),
    endpoints: str | None = typer.Option(
        None,
        "--endpoints",
        help="all, called, served ou matched (padrão: matched com --cross, called sem).",
        show_default=False,
    ),
    depth: int = typer.Option(6, "--depth", min=1, help="Profundidade do alcance do endpoint."),
    fmt: str = typer.Option("md", "--format", help="md, json ou csv."),
    what: str = typer.Option("endpoints", "--what", help="No csv: endpoints, modules ou matrix."),
    out: Path | None = typer.Option(None, "--out", help="Grava a saída neste arquivo."),
    limit: int = typer.Option(40, "--limit", min=0, help="Linhas por tabela no md (0 = todas)."),
) -> None:
    """Relatório de fronteira: plano de corte por endpoint e acoplamento por módulo."""
    from php_map.analysis.boundary import build_report
    from php_map.report import boundary as boundary_report

    try:
        if fmt not in ("md", "json", "csv"):
            raise UsageError("--format aceita md, json ou csv")
        if what not in boundary_report.WHATS:
            raise UsageError(f"--what aceita: {', '.join(boundary_report.WHATS)}")
        report = build_report(db, cross, module=module, selection=endpoints, depth=depth)
    except UsageError as exc:
        console.print(f"[red]Erro:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    except PhpMapError as exc:
        console.print(f"[red]Erro:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    if fmt == "json":
        text = boundary_report.to_json(report)
    elif fmt == "csv":
        text = boundary_report.to_csv(report, what)
    else:
        text = boundary_report.to_markdown(report, limit)
    for warning in report.warnings:
        console.print(f"[yellow]Aviso:[/yellow] {escape(warning)}")
    if out is not None:
        _write(out, text)
        console.print(f"Relatório gravado em {out}")
        return
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.flush()


@app.command("value-origin")
def value_origin_command(
    question: str = typer.Argument(..., help='Ex.: "de onde vem o valor do m² do terreno".'),
    db: Path = typer.Option(DEFAULT_DB, "--db", help="Banco do grafo."),
    funcao: str | None = typer.Option(
        None, "--funcao", help="Função para limitar o alcance (recomendado).", show_default=False
    ),
    alvo: list[str] = typer.Option(
        [], "--alvo", help="Candidato (schema.tabela.coluna), repetível; sem --alvo, só lista."
    ),
    max_words: int = typer.Option(2000, "--max-words", min=500, max=6000),
    hops: int = typer.Option(
        value_origin.MAX_HOPS_DEFAULT, "--hops", min=1, max=20, help="Saltos máximos na linhagem."
    ),
    as_json: bool = typer.Option(False, "--json", help="Saída em JSON em vez de texto/Markdown."),
    out: Path | None = typer.Option(None, "--out", help="Grava o dossiê neste arquivo."),
) -> None:
    """ "De onde vem este valor?": interpreta a pergunta e, com --alvo, mostra a
    cadeia até a coluna base e a tela que a cadastra — sem IA; a IA, se usada, só analisa."""
    if not db.is_file():
        console.print(f"[red]Erro:[/red] banco inexistente: {db}")
        raise typer.Exit(code=1)
    try:
        with closing(store.connect_readonly(db)) as conn:
            interpretation = value_origin.interpret(conn, question, funcao)
            if not alvo:
                if as_json:
                    payload = {
                        "function": interpretation.function,
                        "suggested_functions": list(interpretation.suggested_functions),
                        "reach": len(interpretation.reach),
                        "candidates": [asdict(c) for c in interpretation.candidates],
                        "warnings": list(interpretation.warnings),
                    }
                    console.print_json(data=payload)
                    return
                for warning in interpretation.warnings:
                    console.print(f"[yellow]Aviso:[/yellow] {warning}")
                if not interpretation.candidates:
                    console.print("Nenhum candidato achado. Tente outros termos ou --funcao.")
                    return
                table = Table(title="Candidatos")
                for column in ("#", "Alvo", "Rótulo", "Placar", "Motivo"):
                    table.add_column(column)
                for i, candidate in enumerate(interpretation.candidates, start=1):
                    target = (
                        f"{value_origin.short(candidate.table)}.{candidate.column}"
                        if candidate.column
                        else value_origin.short(candidate.table)
                    )
                    table.add_row(
                        str(i), target, candidate.label or "—", f"{candidate.score:.2f}",
                        candidate.reason,
                    )  # fmt: skip
                Console().print(table)
                console.print("Rode de novo com --alvo <schema.tabela.coluna> (um ou mais).")
                return
            origin = value_origin.trace_value(conn, interpretation, alvo, max_hops=hops)
            dossier = value_origin.render_dossier(origin, max_words=max_words)
    except (PhpMapError, value_origin.ValueOriginError) as exc:
        console.print(f"[red]Erro:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    for warning in origin.warnings:
        console.print(f"[yellow]Aviso:[/yellow] {warning}")
    if as_json:
        console.print_json(data={"chains": [asdict(c) for c in origin.chains], "dossier": dossier})
        return
    if out is not None:
        _write(out, dossier)
        console.print(f"Dossiê gravado em {out} ({len(dossier.split())} palavras)")
        return
    sys.stdout.buffer.write(dossier.encode("utf-8"))
    sys.stdout.flush()


@app.command("index-formulas")
def index_formulas_command(
    db: Path = typer.Option(DEFAULT_DB, "--db", help="Banco do grafo."),
) -> None:
    """Grava `routine_formulas`/`routine_rule_topics`: fórmulas e regras por assunto
    de todas as funções da base, para `formula_search`/`routine_rules(topic=...)` responderem em
    milissegundos, sem precisar escanear de novo. Abre o banco **em escrita, direto no arquivo**
    (sem `os.replace`): nunca rode sobre um banco que a interface ou o MCP estejam servindo."""
    if not db.is_file():
        console.print(f"[red]Erro:[/red] banco inexistente: {db}")
        raise typer.Exit(code=1)
    try:
        with closing(sqlite3.connect(db)) as conn:
            stats = formula_index_store.build_formula_index(conn)
    except PhpMapError as exc:
        console.print(f"[red]Erro:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(
        f"[green]OK[/green] {stats['formulas']} fórmulas em {stats['functions_with_formula']} "
        f"de {stats['functions']} funções ({stats['errors']} erro(s)); "
        f"{stats['rules_with_topic']}/{stats['rules']} regras com assunto — "
        f"{stats['seconds']:.1f}s"
    )
    console.print(f"Por assunto: {stats['by_topic']}")


@app.command("layers")
def layers_command(
    target: str = typer.Argument(
        ..., help="Fonte: caminho, nome ou trecho do arquivo (ou o id de uma Class:/Method:)."
    ),
    db: Path = typer.Option(DEFAULT_DB, "--graph", "--db", help="Banco do grafo."),
    min_confidence: float = typer.Option(
        layers_analysis.DEFAULT_MIN_CONFIDENCE,
        "--min-confidence",
        min=0.0,
        max=1.0,
        help=MIN_HELP,
    ),
    per_layer: int = typer.Option(
        layers_report.PER_LAYER,
        "--per-layer",
        min=1,
        help="Itens mostrados por camada no texto (o resto vira +N). O --json traz todos.",
    ),
    as_json: bool = typer.Option(False, "--json", help="Saída em JSON em vez de texto."),
) -> None:
    """Camadas de um fonte: do menu à tela, JS, rota e controller que chegam
    nele, e dele até service, repository, model, DAO (`cl_*`) e tabela."""
    if not db.is_file():
        console.print(f"[red]Erro:[/red] banco inexistente: {db}")
        raise typer.Exit(code=1)
    try:
        with closing(store.connect_readonly(db)) as conn:
            match = layers_analysis.resolve_target(conn, target)
            if match.file is None:
                hint = f" Candidatos: {', '.join(match.candidates[:5])}" if match.candidates else ""
                raise UsageError(f"fonte não encontrado ou ambíguo: {target}.{hint}")
            result = layers_analysis.file_layers(
                conn, match.file, layers_analysis.LayerOptions(min_confidence=min_confidence)
            )
    except UsageError as exc:
        console.print(f"[red]Erro:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    except PhpMapError as exc:
        console.print(f"[red]Erro:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    text = result.to_json_text() + "\n" if as_json else layers_report.to_text(result, per_layer)
    sys.stdout.buffer.write(text.encode("utf-8"))
    sys.stdout.flush()


MCP_TOKEN_HELP = (
    "Sem token (PHP_MAP_MCP_TOKEN ou `php-map token create`), o transporte http só sobe "
    "com --insecure-no-auth e em host local."
)
ACCESS_DB_HELP = "Cofre de acesso (padrão: PHP_MAP_ACCESS_DB ou out/access/access.db)."


def _mcp_verifier(legacy: str | None, access_db: Path | None) -> Any:
    """Verificador do bearer: os tokens do cofre mais o legado (`PHP_MAP_MCP_TOKEN`).

    O cofre entra mesmo vazio quando há o legado, para que `token create` e `token revoke` valham
    sem reiniciar. Sem token ativo e sem legado, devolve `None` (quem chama recusa subir).
    """
    from php_map.access import tokens as access_tokens
    from php_map.access.store import Vault, open_vault
    from php_map.errors import AccessError
    from php_map.mcp.auth import AnyOf, StaticTokenVerifier, VaultTokenVerifier

    vault: Vault | None = None
    try:
        vault = open_vault(access_db)
    except AccessError as exc:
        if legacy is None:
            raise
        logger.warning("MCP: cofre de acesso indisponível, só o token legado vale: %s", exc)
    if legacy is None and (vault is None or not access_tokens.has_active_token(vault)):
        return None
    verifiers: list[Any] = []
    if vault is not None:
        verifiers.append(VaultTokenVerifier(vault))
    if legacy is not None:
        verifiers.append(StaticTokenVerifier(legacy))
    return verifiers[0] if len(verifiers) == 1 else AnyOf(*verifiers)


@app.command("mcp")
def mcp_command(
    projects: Path | None = typer.Option(
        None,
        "--projects",
        help=(
            "projects.json com os grafos a servir; o campo 'db' de "
            "cada projeto pode ser um arquivo ou uma pasta (o .db mais novo e "
            "completo dela é escolhido, relido sem reiniciar o servidor)."
        ),
    ),
    db: list[Path] = typer.Option(
        [],
        "--db",
        help=(
            "Banco do grafo ou pasta de grafos da interface (repetível), sem projects.json; "
            "de uma pasta, o .db mais novo e completo é escolhido e reavaliado sem reiniciar "
            "o servidor."
        ),
    ),
    transport: str = typer.Option("stdio", "--transport", help="stdio (local) ou http (remoto)."),
    host: str = typer.Option("127.0.0.1", "--host", help="Endereço do transporte http."),
    port: int = typer.Option(8765, "--port", min=1, max=65535, help="Porta do transporte http."),
    allowed_host: list[str] = typer.Option(
        [], "--allowed-host", help="Host público aceito no header Host (repetível)."
    ),
    docs_dir: Path | None = typer.Option(
        None,
        "--docs-dir",
        help="Pasta de documentação servida como recursos MCP (padrão: docs/ do repositório).",
    ),
    insecure_no_auth: bool = typer.Option(False, "--insecure-no-auth", help=MCP_TOKEN_HELP),
    access_db: Path | None = typer.Option(None, "--access-db", help=ACCESS_DB_HELP),
) -> None:
    """Servidor MCP somente leitura sobre os grafos."""
    try:
        from php_map.mcp import server as mcp_server
    except ImportError as exc:
        console.print('[red]Erro:[/red] instale a dependência opcional: pip install -e ".[mcp]"')
        raise typer.Exit(code=1) from exc
    from php_map.mcp.auth import TOKEN_ENV
    from php_map.mcp.catalog import ProjectCatalog

    try:
        if (projects is None) == (not db):
            raise UsageError("informe --projects ou --db (um dos dois)")
        if transport not in ("stdio", "http"):
            raise UsageError("--transport aceita stdio ou http")
        local = host in mcp_server.LOCAL_HOSTS
        legacy = env_value(TOKEN_ENV) if transport == "http" else None
        verifier = _mcp_verifier(legacy, access_db) if transport == "http" else None
        if transport == "http" and verifier is None and not insecure_no_auth:
            raise UsageError(
                f"nenhum token ativo: rode `php-map token create --name <nome>` "
                f"ou defina {TOKEN_ENV} (variável ou .env), ou use --insecure-no-auth"
            )
        if insecure_no_auth and not local:
            raise UsageError("--insecure-no-auth só é aceito com --host 127.0.0.1 ou localhost")
        catalog = (
            ProjectCatalog.from_projects_file(projects)
            if projects is not None
            else ProjectCatalog.from_dbs(db)
        )
        server = mcp_server.build_server(catalog, docs_dir=docs_dir)
        slugs = ", ".join(p.slug for p in catalog.projects)
        if transport == "stdio":
            logger.info("MCP (stdio): projetos %s", slugs)
            mcp_server.run_stdio(server)
            return
        app = mcp_server.http_app(
            server,
            host=host,
            allowed_hosts=allowed_host,
            token=None if insecure_no_auth else verifier,
        )
    except UsageError as exc:
        console.print(f"[red]Erro:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    except (PhpMapError, ValueError) as exc:
        console.print(f"[red]Erro:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    import uvicorn

    logger.info("MCP (http) em http://%s:%d%s: projetos %s", host, port, mcp_server.MCP_PATH, slugs)
    uvicorn.run(app, host=host, port=port, log_level="info")


# --------------------------------------------------------------------------- acesso

user_app = typer.Typer(help="Usuários do login da interface (cofre de acesso).")
app.add_typer(user_app, name="user")
token_app = typer.Typer(help="Tokens do MCP (cofre de acesso).")
app.add_typer(token_app, name="token")

ACCESS_DB_OPTION = typer.Option(None, "--access-db", help=ACCESS_DB_HELP)


def _read_password(prompt: str = "Senha") -> str:
    import getpass

    first = getpass.getpass(f"{prompt}: ")
    if getpass.getpass(f"{prompt} (de novo): ") != first:
        raise UsageError("as duas senhas não conferem")
    return first


def _show_time(value: str | None) -> str:
    return value.replace("T", " ").removesuffix("Z") if value else "-"


@user_app.command("add")
def user_add(
    email: str = typer.Argument(..., help="E-mail do usuário (é o login), ex.: admin@exemplo.com."),
    role: str | None = typer.Option(
        None,
        "--role",
        help="admin ou leitor. Padrão: leitor; o primeiro usuário do cofre é sempre admin.",
    ),
    access_db: Path | None = ACCESS_DB_OPTION,
) -> None:
    """Cria um usuário (a senha é pedida duas vezes, sem eco)."""
    from php_map.access import users
    from php_map.access.store import open_vault

    try:
        email = users.check_email(email)  # antes de pedir a senha
        vault = open_vault(access_db)
        if role is None:
            role = users.READER if users.has_active_admin(vault) else users.ADMIN
        users.check_role(role)
        users.add_user(vault, email, _read_password(), role)
    except UsageError as exc:
        console.print(f"[red]Erro:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    except PhpMapError as exc:
        console.print(f"[red]Erro:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Usuário '{escape(email)}' criado como {role}.[/green]")


@user_app.command("passwd")
def user_passwd(
    username: str = typer.Argument(..., help="E-mail do usuário."),
    access_db: Path | None = ACCESS_DB_OPTION,
) -> None:
    """Troca a senha (e desbloqueia a conta)."""
    from php_map.access import users
    from php_map.access.store import open_vault

    try:
        vault = open_vault(access_db)
        users.set_password(vault, username, _read_password("Nova senha"))
    except UsageError as exc:
        console.print(f"[red]Erro:[/red] {escape(str(exc))}")
        raise typer.Exit(code=2) from exc
    except PhpMapError as exc:
        console.print(f"[red]Erro:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Senha de '{escape(username)}' trocada.[/green]")


@user_app.command("list")
def user_list(access_db: Path | None = ACCESS_DB_OPTION) -> None:
    """Lista os usuários (nunca mostra a senha)."""
    from php_map.access import users
    from php_map.access.store import open_vault

    try:
        listed = users.list_users(open_vault(access_db))
    except PhpMapError as exc:
        console.print(f"[red]Erro:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    if not listed:
        console.print("Nenhum usuário. Crie um com `php-map user add <nome>`.")
        return
    table = Table("Usuário", "Papel", "Estado", "Criado", "Último login")
    for user in listed:
        state = "ativa" if user.active else "desativada"
        table.add_row(
            user.username,
            user.role,
            state,
            _show_time(user.created_at),
            _show_time(user.last_login_at),
        )
    Console().print(table)


@user_app.command("role")
def user_role(
    username: str = typer.Argument(..., help="E-mail do usuário."),
    role: str = typer.Argument(..., help="admin ou leitor."),
    access_db: Path | None = ACCESS_DB_OPTION,
) -> None:
    """Troca o papel do usuário (não rebaixa o último administrador ativo)."""
    from php_map.access import users
    from php_map.access.store import open_vault

    try:
        users.set_role(open_vault(access_db), username, role)
    except PhpMapError as exc:
        console.print(f"[red]Erro:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    console.print(f"Usuário '{escape(username)}' agora é {role}.")


def _toggle_user(username: str, access_db: Path | None, *, enable: bool) -> None:
    from php_map.access import users
    from php_map.access.store import open_vault

    try:
        vault = open_vault(access_db)
        (users.enable_user if enable else users.disable_user)(vault, username)
    except PhpMapError as exc:
        console.print(f"[red]Erro:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    console.print(f"Usuário '{escape(username)}' {'reativado' if enable else 'desativado'}.")


@user_app.command("disable")
def user_disable(
    username: str = typer.Argument(..., help="E-mail do usuário."),
    access_db: Path | None = ACCESS_DB_OPTION,
) -> None:
    """Desativa o usuário e derruba as sessões dele (não apaga)."""
    _toggle_user(username, access_db, enable=False)


@user_app.command("enable")
def user_enable(
    username: str = typer.Argument(..., help="E-mail do usuário."),
    access_db: Path | None = ACCESS_DB_OPTION,
) -> None:
    """Reativa um usuário desativado."""
    _toggle_user(username, access_db, enable=True)


@token_app.command("create")
def token_create(
    name: str = typer.Option(..., "--name", help="Nome do token (ex.: meu-notebook)."),
    expires: str = typer.Option("90d", "--expires", help="30d, 90d, 180d, 365d ou never."),
    url: str = typer.Option(
        "http://127.0.0.1:8765", "--url", help="Endereço do MCP, só para a dica de uso."
    ),
    access_db: Path | None = ACCESS_DB_OPTION,
) -> None:
    """Cria um token do MCP. O valor aparece **uma vez**: guarde-o agora."""
    from php_map.access import tokens as access_tokens
    from php_map.access.store import open_vault

    try:
        vault = open_vault(access_db)
        raw, info = access_tokens.create_token(vault, name, expires=expires, created_by="cli")
    except PhpMapError as exc:
        console.print(f"[red]Erro:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    if info.expires_at is None:
        console.print("[yellow]Aviso:[/yellow] token sem validade; revogue-o quando não precisar.")
    out = Console()
    out.print(f"Token '{escape(info.name)}' criado (vence em {_show_time(info.expires_at)}):")
    out.print(raw, markup=False, highlight=False, soft_wrap=True)
    out.print("Ele não será mostrado de novo. Configure no seu cliente MCP:")
    hint = f"URL {url.rstrip('/')}/mcp e cabeçalho `Authorization: Bearer {raw}`"
    out.print(hint, markup=False, highlight=False, soft_wrap=True)


@token_app.command("list")
def token_list(access_db: Path | None = ACCESS_DB_OPTION) -> None:
    """Lista os tokens (prefixo, validade e último uso; nunca o token nem o hash)."""
    from php_map.access import tokens as access_tokens
    from php_map.access.store import open_vault

    try:
        vault = open_vault(access_db)
        listed = access_tokens.list_tokens(vault)
        now = vault.now()
    except PhpMapError as exc:
        console.print(f"[red]Erro:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    if not listed:
        console.print("Nenhum token. Crie um com `php-map token create --name <nome>`.")
        return
    table = Table("Id", "Nome", "Prefixo", "Criado", "Vence", "Último uso", "Estado")
    for t in listed:
        used = f"{_show_time(t.last_used_at)} ({t.last_used_ip})" if t.last_used_at else "-"
        table.add_row(
            str(t.id),
            t.name,
            t.prefix,
            _show_time(t.created_at),
            _show_time(t.expires_at) if t.expires_at else "nunca",
            used,
            t.state(now),
        )
    Console(width=max(Console().width, 120)).print(table)


@token_app.command("revoke")
def token_revoke(
    ref: str = typer.Argument(..., help="Id ou prefixo do token (veja `token list`)."),
    access_db: Path | None = ACCESS_DB_OPTION,
) -> None:
    """Revoga o token: a próxima chamada ao MCP com ele recebe 401."""
    from php_map.access import tokens as access_tokens
    from php_map.access.store import open_vault

    try:
        info = access_tokens.revoke_token(open_vault(access_db), ref)
    except PhpMapError as exc:
        console.print(f"[red]Erro:[/red] {escape(str(exc))}")
        raise typer.Exit(code=1) from exc
    console.print(f"Token {info.id} ('{escape(info.name)}', {info.prefix}…) revogado.")


def _print_tunnel(job: Any) -> None:
    """`ui --tunnel`: diz quando o túnel subiu (URL e comando de conexão) ou por que não subiu."""
    if job.state == "no-ar":
        console.print(f"Túnel no ar: [bold]{escape(str(job.url))}[/bold]")
        console.print(
            "Configure no seu cliente MCP (troque <seu-token> por um de "
            "`php-map token create`):\n"
            f"  URL {job.url} e cabeçalho `Authorization: Bearer <seu-token>`",
            markup=False,
            soft_wrap=True,
        )
    elif job.state in ("falhou", "caiu"):
        console.print(f"[red]Túnel {job.state}:[/red] {escape(str(job.error))}")
        for finding in job.findings:
            console.print(f"  [{finding.kind}] {escape(finding.where)}: {escape(finding.masked)}")


def _open_login_vault(access_db: Path | None) -> Any:
    """Cofre do login da interface; cria se não existir. Se não abrir, recusa subir: sem cofre
    não há como exigir login, e a interface nunca sobe aberta por acidente."""
    from php_map.access.store import default_path, open_vault
    from php_map.errors import AccessError

    target = access_db if access_db is not None else default_path()
    try:
        return open_vault(target)
    except AccessError as exc:
        console.print(f"[red]Erro:[/red] {escape(str(exc))}")
        console.print(
            "A interface não sobe sem o cofre de acesso. Corrija o caminho (`--access-db`) "
            "ou use `--no-auth` só para desenvolvimento, com `--host 127.0.0.1`."
        )
        raise typer.Exit(code=1) from exc


def _check_no_auth(no_auth: bool, host: str) -> None:
    """`--no-auth` só vale em loopback: nunca abre uma porta da rede sem login."""
    if no_auth and host not in NO_AUTH_HOSTS:
        console.print(
            "[red]Erro:[/red] --no-auth só é permitido com --host 127.0.0.1 "
            f"(recebi {escape(host)}). Sem login, quem alcançar a porta lê o grafo."
        )
        raise typer.Exit(code=1)


NO_AUTH_HOSTS = ("127.0.0.1", "localhost", "::1")


@app.command("ui")
def ui_command(
    ctx: typer.Context,
    db: Path = typer.Option(DEFAULT_DB, "--db", help="Banco do grafo."),
    source: Path | None = typer.Option(
        None, "--source", help="Pasta do e-cidade para o código-fonte (padrão: a do grafo)."
    ),
    host: str = typer.Option("127.0.0.1", "--host", help="Endereço em que a interface escuta."),
    port: int = typer.Option(8765, "--port", min=1, max=65535, help="Porta."),
    open_browser: bool = typer.Option(False, "--open", help="Abre o navegador."),
    print_schema: bool = typer.Option(
        False, "--print-schema", help="Imprime o schema GraphQL (para ui/schema.graphql) e sai."
    ),
    read_only: bool = typer.Option(
        False, "--read-only", help="Desliga as mutações (escolher a pasta e rodar o scan)."
    ),
    access_db: Path | None = typer.Option(None, "--access-db", help=ACCESS_DB_HELP),
    public_host: list[str] = typer.Option(
        [],
        "--public-host",
        help="Host público de um túnel (repetível): só /mcp responde por ele.",
    ),
    mcp: bool = typer.Option(
        True, "--mcp/--no-mcp", help="Serve o MCP em /mcp, com os tokens do cofre (padrão: liga)."
    ),
    tunnel: bool = typer.Option(
        False, "--tunnel", help="Sobe o túnel salvo na página Publicar (ngrok etc.) com o servidor."
    ),
    no_auth: bool = typer.Option(
        False,
        "--no-auth",
        help="Só para desenvolvimento: sem login e sem MCP. Exige --host 127.0.0.1.",
    ),
) -> None:
    """Sobe a interface web e a API GraphQL. A interface sempre pede login: na primeira
    execução ela imprime um código de configuração para criar o administrador pela página;
    o MCP sai em /mcp com os tokens do cofre."""
    from php_map.web.schema import schema  # só aqui: o resto da CLI não carrega o servidor

    if print_schema:
        # Bytes UTF-8: o console do Windows (cp1252) não codifica todo caractere do schema.
        sys.stdout.buffer.write((schema.as_str() + "\n").encode("utf-8"))
        sys.stdout.flush()
        return
    _check_no_auth(no_auth, host)
    import threading
    import webbrowser

    import uvicorn

    from php_map.web.app import LOCAL_HOSTS, create_app
    from php_map.web.projects import ProjectStore
    from php_map.web.state import served_db

    kind = ctx.get_parameter_source("db")
    explicit = kind is not None and kind.name == "COMMANDLINE"  # só `--db` na linha vale
    served: Path | None
    try:
        if not explicit and not db.is_file() and ProjectStore(db.parent).exists():
            served = None  # sem o --db padrão, valem os projetos cadastrados
        else:
            store.load_meta(db)  # banco inexistente ou de outra versão: erro claro antes de subir
            served = served_db(db)  # O último gerado pela interface, se mais novo
            if served != db:
                store.load_meta(served)
                console.print(f"Grafo gerado pela interface: [bold]{served}[/bold]")
    except PhpMapError as exc:
        console.print(f"[red]Erro:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    if source is not None and not source.is_dir():
        console.print(f"[red]Erro:[/red] pasta do e-cidade inexistente: {source}")
        raise typer.Exit(code=1)
    hosts = [*LOCAL_HOSTS, *([host] if host not in (*LOCAL_HOSTS, "0.0.0.0") else [])]
    url = f"http://{'127.0.0.1' if host == '0.0.0.0' else host}:{port}/"
    console.print(f"Interface em [bold]{url}[/bold] (API em {url}graphql). Ctrl+C encerra.")
    if open_browser:
        threading.Timer(1.0, webbrowser.open, [url]).start()
    vault = None if no_auth else _open_login_vault(access_db)
    web = create_app(
        served,
        source=source,
        allowed_hosts=hosts,
        out_dir=db.parent,
        read_only=read_only,
        port=port,
        explicit_db=explicit,
        vault=vault,
        public_hosts=public_host,
        mcp=mcp and vault is not None,
        tunnel=tunnel,
        on_tunnel=_print_tunnel,
    )
    if no_auth:
        console.print(
            "[bold red]SEM LOGIN (--no-auth):[/bold red] qualquer processo desta máquina lê "
            "o grafo. Só para desenvolvimento; o MCP fica desligado."
        )
    else:
        _print_access(web.state.access, url)
    if web.state.ecmap.mcp_available:
        console.print(f"MCP em [bold]{url}mcp[/bold] (bearer de `php-map token create`).")
    uvicorn.run(web, host=host, port=port)


def _print_access(access: Any, url: str) -> None:
    """Sem usuário ativo, o código de configuração (só aqui, no terminal); senão, o login."""
    code = access.setup_code
    if code is None:
        console.print("Login ativo: entre com o e-mail e a senha criados na configuração inicial.")
        return
    console.print(
        f"[bold yellow]Configuração inicial (e-mail e senha do administrador):[/bold yellow] "
        f"{url} código [bold]{escape(code)}[/bold] (vale uma vez e some ao reiniciar)"
    )


@app.command("setup-extractor")
def setup_extractor(
    build_image: bool = typer.Option(
        False,
        "--build-image",
        help=f"Constrói antes a imagem do php-extractor/Dockerfile ({PHP_IMAGE_OWN}).",
    ),
    php_image: str | None = typer.Option(None, "--php-image", help=PHP_IMAGE_HELP),
) -> None:
    """Instala as dependências do extrator PHP (composer, em container descartável)."""
    settings = Settings(php_image=php_image or None)
    try:
        if build_image:
            target = php_image or PHP_IMAGE_OWN
            console.print(f"Construindo a imagem [bold]{target}[/bold]...")
            php_extractor.build_image(settings, target)
            settings = Settings(php_image=target)
        console.print(f"Instalando dependências em [bold]{settings.extractor_dir}[/bold]...")
        image = php_extractor.setup_extractor(settings)
    except PhpMapError as exc:
        console.print(f"[red]Erro:[/red] {exc}")
        raise typer.Exit(code=1) from exc
    console.print(f"[green]Extrator PHP pronto[/green] (imagem {image}).")
