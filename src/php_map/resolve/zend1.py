"""Estrutura do Zend 1: módulo por diretório, `Route` e `RENDERS`.

Roda sempre que `settings.module_source == "directory"` (hoje só o perfil `zend1`); não depende
do catálogo do e-cidade (um projeto zend1 não tem). A pasta
de `application/modules/` **é** o módulo, então a aresta é 1.0 por `ZEND1_RESOLVER`, não
`NAMING_RULE` (que a ontologia reserva para heurística com confiança < 1.0).

**Detalhe do extrator:** `php-extractor` sempre devolve `name`/
`class` de `declares` em **minúsculas** (`CollectorVisitor::enterMethod`, `toLowerString()` —
PHP é case-insensitive para esses nomes, e todo o resto do resolvedor já depende disso). Isso
apaga a informação de que `CancelamentoPedidoController::consultarAction` precisa para o
kebab-case (`cancelamento-pedido`): não tem como separar palavras numa string já minúscula. A
saída, sem tocar no extrator (mudaria o contrato pra todo mundo, arriscando o critério de bytes
idênticos): o **nome do controller** vem do nome do **arquivo** (`record.path`
preserva a caixa original; é a convenção do Zend — 1 arquivo, 1 classe, mesmo nome) e o **nome
da action** vem de reler a própria linha do método no arquivo fonte (`decl.line_start` já indica
qual linha é; um `\\w+` depois de `function` recupera a caixa original). Sem o arquivo para ler
(não devia acontecer num scan de verdade), cai para o nome em minúsculas do `declares` mesmo,
sem kebab (resultado pior, nunca um erro).
"""

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from php_map.graph.builder import GraphBuilder
from php_map.graph.model import (
    Edge,
    EdgeSource,
    EdgeType,
    Node,
    NodeLabel,
    file_id,
    method_id,
    module_id,
    route_id,
)
from php_map.ingest.records import PhpFileRecord

RecordSource = Iterable[PhpFileRecord] | Callable[[], Iterable[PhpFileRecord]]

_MODULE_DIR = re.compile(r"^application/modules/([^/]+)/")
_CONTROLLER_SUFFIX = "controller"
_ACTION_SUFFIX = "action"
_METHOD_NAME_IN_SOURCE = re.compile(r"\bfunction\s+(\w+)\s*\(")
# Algoritmo do próprio Zend (`Zend_Filter_Word_CamelCaseToDash`): separa em cada maiúscula que
# comeca uma palavra nova, cuidando de siglas (`CNAE` não vira `c-n-a-e` se vier colada a uma
# palavra). Conferido com exemplos reais (`CancelamentoPedido` → `cancelamento-pedido`,
# `Dms` → `dms`); nenhuma sigla seguida foi encontrada na fixture ou nos exemplos reais.
_CAMEL_1 = re.compile(r"([a-z0-9])([A-Z])")
_CAMEL_2 = re.compile(r"([A-Z]+)([A-Z][a-z])")


def kebab(name: str) -> str:
    step = _CAMEL_1.sub(r"\1-\2", name)
    step = _CAMEL_2.sub(r"\1-\2", step)
    return step.lower()


@dataclass(slots=True)
class Zend1Stats:
    modules: int = 0
    files_in_module: int = 0
    files_outside_modules: int = 0
    routes: int = 0
    renders: int = 0
    orphan_views: int = 0
    seconds: float = 0.0


def apply_zend1_structure(
    records: RecordSource, builder: GraphBuilder, source: Path | None
) -> Zend1Stats:
    started = time.perf_counter()
    stats = Zend1Stats()
    created_modules: set[str] = set()
    all_views: set[str] = set()
    rendered_views: set[str] = set()
    for record in records() if callable(records) else records:
        path = record.path
        if path.endswith(".phtml"):
            all_views.add(path)
        module_match = _MODULE_DIR.match(path)
        if module_match is None:
            stats.files_outside_modules += 1
            continue
        modulo = module_match.group(1)
        _ensure_module(builder, created_modules, modulo, stats)
        builder.add_edge(
            Edge(
                file_id(path),
                module_id(modulo),
                EdgeType.BELONGS_TO,
                EdgeSource.ZEND1_RESOLVER,
                1.0,
                {"path": path, "rule": "zend1:module-dir"},
            )
        )
        stats.files_in_module += 1
        controller = _controller_name_from_path(path)
        if controller is None:
            continue
        for decl in record.declares:
            if decl.kind != "method" or decl.class_ is None:
                continue
            if not decl.name.endswith(_ACTION_SUFFIX):
                continue
            method_name = _original_method_name(source, path, decl.line_start, decl.name)
            if method_name.lower() != decl.name:
                method_name = decl.name  # linha não confirma: usa o nome minúsculo mesmo
            action = method_name[: -len(_ACTION_SUFFIX)]
            _emit_route(
                builder,
                path,
                decl.line_start,
                decl.class_,
                decl.name,
                modulo,
                controller,
                action,
                stats,
                rendered_views,
            )
    stats.orphan_views = len(all_views - rendered_views)
    stats.seconds = round(time.perf_counter() - started, 2)
    return stats


def _controller_name_from_path(path: str) -> str | None:
    """`.../NomeController.php` → `Nome`, preservando a caixa do arquivo; `None` se não for um
    controller (convenção do Zend: 1 arquivo, 1 classe, mesmo nome)."""
    stem = path.rsplit("/", 1)[-1].removesuffix(".php")
    if not stem.lower().endswith(_CONTROLLER_SUFFIX):
        return None
    return stem[: -len("Controller")]


def _original_method_name(source: Path | None, path: str, line: int, fallback: str) -> str:
    if source is None:
        return fallback
    try:
        with (source / path).open(encoding="utf-8", errors="replace") as handle:
            for number, text in enumerate(handle, start=1):
                if number == line:
                    match = _METHOD_NAME_IN_SOURCE.search(text)
                    return match.group(1) if match else fallback
                if number > line:
                    break
    except OSError:
        return fallback
    return fallback


def _ensure_module(builder: GraphBuilder, created: set[str], name: str, stats: Zend1Stats) -> None:
    if name in created:
        return
    created.add(name)
    builder.add_node(Node(module_id(name), NodeLabel.MODULE, {"name": name}))
    stats.modules += 1


def _emit_route(
    builder: GraphBuilder,
    path: str,
    line: int,
    class_name: str,
    method_name: str,
    modulo: str,
    controller_name: str,
    action_name: str,
    stats: Zend1Stats,
    rendered_views: set[str],
) -> None:
    controller = kebab(controller_name)
    action = kebab(action_name)
    evidence = {"path": path, "line": line}
    route = route_id(f"{modulo}/{controller}/{action}")
    builder.add_node(
        Node(route, NodeLabel.ROUTE, {"module": modulo, "controller": controller, "action": action})
    )
    dst = method_id(class_name, method_name)
    builder.add_edge(Edge(route, dst, EdgeType.ROUTES_TO, EdgeSource.ZEND1_RESOLVER, 1.0, evidence))
    stats.routes += 1
    view_path = f"application/modules/{modulo}/views/scripts/{controller}/{action}.phtml"
    view_id = file_id(view_path)
    if view_id in builder:
        builder.add_edge(
            Edge(dst, view_id, EdgeType.RENDERS, EdgeSource.ZEND1_RESOLVER, 1.0, evidence)
        )
        stats.renders += 1
        rendered_views.add(view_path)
