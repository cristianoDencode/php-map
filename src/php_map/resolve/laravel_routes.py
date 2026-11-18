"""Rotas HTTP do Laravel → nós `Route` e `ROUTES_TO` para o controller.

A árvore de rotas do e-cidade está espalhada: o `RouteServiceProvider` inclui `routes/api.php`
com `Route::prefix('v4/api')->middleware('api')->group(base_path('routes/api.php'))`, e esse
arquivo inclui outros 100 (`Route::prefix('client')->group(base_path('routes/api/…'))`),
cada um com os próprios `group(function () { … })` aninhados. A URI de uma rota é a soma dos
prefixos de **todos** os grupos que a envolvem, e o controller depende do `namespace` acumulado.

O extrator (schema_version 7) entrega, por arquivo, as instruções de rota em forma simbólica
(`php-extractor/src/SymbolicRoutes.php`); aqui elas são avaliadas a partir das **raízes**: os
arquivos com rotas que ninguém inclui e que não moram em `routes/` (o provider). Um arquivo de
`routes/` que nenhuma raiz alcança não vira rota (a URI não é conhecida sem o prefixo de quem o
inclui) e é só contado (`unreached_files`). O mesmo arquivo incluído com prefixos diferentes gera
uma `Route` por URI final.

Expressões avaliadas: literal, concatenação, variável atribuída antes (`$path . "Controller@x"`),
`Classe::class` e `base_path('…')`. O que não se resolve **não é adivinhado**: a rota é pulada e
vira a pendência `route_dynamic`. Cada verbo (`get`, `post`…) cria `Route:<VERBO> <uri>` com os
segmentos `{id}` em `params`; `ROUTES_TO` só sai para um `Method` que o código escaneado declara
(`route_controller_missing` quando não). Closures não têm controller.
"""

import re
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field, replace
from typing import Any

from php_map.config import Settings
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import (
    Edge,
    EdgeSource,
    EdgeType,
    Node,
    NodeLabel,
    class_id,
    key_of,
    method_id,
    route_id,
)
from php_map.ingest.records import PhpFileRecord, RouteCall, RouteItem
from php_map.resolve.unresolved import UnresolvedWriter

RecordSource = Iterable[PhpFileRecord] | Callable[[], Iterable[PhpFileRecord]]
Sym = dict[str, Any]

ROUTES_DIR = "routes/"
VERBS = frozenset({"get", "post", "put", "patch", "delete", "options"})
RESOURCE_ACTIONS = (
    ("index", "GET", ""),
    ("create", "GET", "/create"),
    ("store", "POST", ""),
    ("show", "GET", "/{id}"),
    ("edit", "GET", "/{id}/edit"),
    ("update", "PUT", "/{id}"),
    ("destroy", "DELETE", "/{id}"),
)
API_RESOURCE_SKIP = frozenset({"create", "edit"})
_PARAM = re.compile(r"\{(\w+)(\?)?\}")
_BACKSLASHES = re.compile(r"\\+")
MAX_DEPTH = 40


@dataclass(slots=True)
class RouteStats:
    files: int = 0  # arquivos com instruções de rota
    roots: int = 0  # os que ninguém inclui e não moram em `routes/`
    reached_files: int = 0
    unreached_files: int = 0  # de `routes/` e sem raiz que os alcance: sem URI, não viram rota
    routes: int = 0  # nós `Route` distintos
    declarations: int = 0  # verbos avaliados (a mesma rota repetida conta)
    with_params: int = 0  # rotas com segmento dinâmico
    routes_to: int = 0  # `ROUTES_TO` criadas (rotas distintas com controller declarado)
    closures: int = 0  # rotas cujo destino é uma closure
    controller_missing: int = 0  # controller/método que o código escaneado não declara
    dynamic: int = 0  # verbos pulados porque a URI ou um prefixo não se resolve
    target_unresolved: int = 0  # rota criada, mas o destino não é literal (sem `ROUTES_TO`)
    missing_files: int = 0  # `base_path` para arquivo que não existe
    empty_files: int = 0  # `base_path` para arquivo que existe e não declara nenhuma rota
    inherited: int = 0  # `ROUTES_TO` cujo método vem de uma classe-pai do controller
    cycles: int = 0  # inclusões que fecham um ciclo (ignoradas)
    by_area: dict[str, int] = field(default_factory=dict)
    by_verb: dict[str, int] = field(default_factory=dict)
    seconds: float = 0.0


@dataclass(frozen=True, slots=True)
class _Ctx:
    """O que os grupos que envolvem um trecho de rotas acumulam."""

    prefix: tuple[str, ...] = ()
    namespace: str = ""
    middleware: tuple[str, ...] = ()
    controller: str | None = None
    dynamic: bool = False  # algum prefixo não se resolveu


def apply_laravel_routes(
    records: RecordSource,
    builder: GraphBuilder,
    unresolved: UnresolvedWriter,
    settings: Settings,
) -> RouteStats:
    """Avalia as rotas a partir das raízes e cria `Route` e `ROUTES_TO`.

    **Ordem:** depois da resolução PHP: os `Method` dos controllers precisam existir para o
    `ROUTES_TO`. Não depende do catálogo nem do schema.
    """
    started = time.perf_counter()
    stats = RouteStats()
    scanned: set[str] = set()
    by_path: dict[str, PhpFileRecord] = {}
    for record in records() if callable(records) else records:
        scanned.add(record.path)
        if record.routes:
            by_path[record.path] = record
    stats.files = len(by_path)
    walker = _Walker(by_path, scanned, builder, unresolved, stats)
    included = {target for record in by_path.values() for target in _includes(record.routes)}
    roots = sorted(p for p in by_path if p not in included and not p.startswith(ROUTES_DIR))
    stats.roots = len(roots)
    for root in roots:
        walker.include(root, _Ctx(), (), 0)
    stats.reached_files = len(walker.reached)
    stats.unreached_files = len(set(by_path) - walker.reached)
    stats.seconds = round(time.perf_counter() - started, 2)
    return stats


def _includes(items: Iterable[RouteItem]) -> set[str]:
    """Caminhos citados em `group(base_path('…'))`, em qualquer profundidade."""
    found: set[str] = set()
    for item in items:
        for call in item.chain:
            for arg in call.args:
                found |= _arg_includes(arg)
    return found


def _arg_includes(arg: Sym) -> set[str]:
    path = _base_path(arg)
    if path is not None:
        return {path}
    found: set[str] = set()
    if arg.get("t") == "fn":
        found |= _includes(_items(arg.get("b", [])))
    elif arg.get("t") == "a":
        for entry in arg.get("i", []):
            found |= _arg_includes(entry["v"])
    return found


def _items(raw: list[dict[str, Any]]) -> list[RouteItem]:
    return [RouteItem.model_validate(entry) for entry in raw]


def _base_path(sym: Sym) -> str | None:
    """`base_path('routes/a.php')` → `routes/a.php` (só com literal)."""
    if sym.get("t") != "f" or sym.get("n") != "base_path":
        return None
    args = sym.get("a", [])
    if args and args[0].get("t") == "s":
        return str(args[0]["v"]).replace("\\", "/").lstrip("/")
    return None


class _Walker:
    def __init__(
        self,
        by_path: dict[str, PhpFileRecord],
        scanned: set[str],
        builder: GraphBuilder,
        unresolved: UnresolvedWriter,
        stats: RouteStats,
    ) -> None:
        self.by_path = by_path
        self.scanned = scanned
        self._parents: dict[str, list[str]] | None = None
        self.builder = builder
        self.unresolved = unresolved
        self.stats = stats
        self.reached: set[str] = set()
        self.nodes: set[str] = set()
        self.with_target: set[str] = set()

    # ------------------------------------------------------------------ arquivos e grupos

    def include(self, path: str, ctx: _Ctx, stack: tuple[str, ...], depth: int) -> None:
        if path in stack or depth > MAX_DEPTH:
            self.stats.cycles += 1
            return
        record = self.by_path[path]
        self.reached.add(path)
        self.walk(record.routes, record.path, ctx, {}, (*stack, path), depth)

    def walk(
        self,
        items: Iterable[RouteItem],
        path: str,
        ctx: _Ctx,
        variables: dict[str, str],
        stack: tuple[str, ...],
        depth: int,
    ) -> None:
        for item in items:
            if item.kind == "assign":
                value = evaluate(item.expr or {}, variables)
                if value is None:
                    variables.pop(item.var or "", None)
                elif item.var:
                    variables[item.var] = value
                continue
            self.chain(item, path, ctx, variables, stack, depth)

    def chain(
        self,
        item: RouteItem,
        path: str,
        ctx: _Ctx,
        variables: dict[str, str],
        stack: tuple[str, ...],
        depth: int,
    ) -> None:
        calls = item.chain
        index = next((i for i, c in enumerate(calls) if _is_terminal(c)), None)
        if index is None:
            return  # só modificadores soltos (`Route::middleware(...)` sem destino)
        for call in calls[:index]:
            ctx = _modify(ctx, call, variables)
        terminal, after = calls[index], calls[index + 1 :]
        name = terminal.name.lower()
        if name == "group":
            self.group(terminal, path, item.line, ctx, variables, stack, depth)
        elif name in ("resource", "apiresource"):
            self.resource(terminal, after, path, item.line, ctx, variables)
        else:
            self.verb(terminal, after, path, item.line, ctx, variables)

    def group(
        self,
        call: RouteCall,
        path: str,
        line: int,
        ctx: _Ctx,
        variables: dict[str, str],
        stack: tuple[str, ...],
        depth: int,
    ) -> None:
        args = call.args
        if (
            args and args[0].get("t") == "a"
        ):  # `Route::group(['prefix' => 'x', …], function () {…})`
            ctx = _modify_from_array(ctx, args[0], variables)
            args = args[1:]
        target = args[0] if args else {}
        if target.get("t") == "fn":
            self.walk(_items(target.get("b", [])), path, ctx, dict(variables), stack, depth + 1)
            return
        include = _base_path(target)
        if include is None:
            self.dynamic(path, line, "group(<não literal>)")
            return
        if include in self.scanned and include not in self.by_path:
            self.stats.empty_files += 1  # existe, mas não declara nenhuma rota
            return
        if include not in self.by_path:
            self.stats.missing_files += 1
            self.unresolved.add("route_file_missing", path, line, include, hint=include)
            return
        self.include(include, ctx, stack, depth + 1)

    # ------------------------------------------------------------------ rotas

    def verb(
        self,
        call: RouteCall,
        after: list[RouteCall],
        path: str,
        line: int,
        ctx: _Ctx,
        variables: dict[str, str],
    ) -> None:
        name = call.name.lower()
        args = call.args
        if name == "match":
            verbs = [v.upper() for v in _strings(args[0], variables)] if args else []
            args = args[1:]
        elif name == "any":
            verbs = ["ANY"]
        elif name in VERBS:
            verbs = [name.upper()]
        else:
            return  # `redirect`, `view`, `fallback`…: sem controller, fora do que se mapeia
        uri = evaluate(args[0], variables) if args else None
        if uri is None or ctx.dynamic:
            self.dynamic(path, line, f"{name}(<URI ou prefixo não literal>)")
            return
        target = self._target(args[1] if len(args) > 1 else None, ctx, variables)
        if target is None and len(args) > 1:
            self.stats.target_unresolved += 1
            self.unresolved.add(
                "route_dynamic", path, line, f"{name}('{uri}', <destino não literal>)"
            )
        middleware = _merge(ctx.middleware, _post_middleware(after, variables))
        for verb in verbs:
            self.emit(verb, uri, path, line, ctx, middleware, target)

    def resource(
        self,
        call: RouteCall,
        after: list[RouteCall],
        path: str,
        line: int,
        ctx: _Ctx,
        variables: dict[str, str],
    ) -> None:
        args = call.args
        base = evaluate(args[0], variables) if args else None
        controller = evaluate(args[1], variables) if len(args) > 1 else None
        if base is None or controller is None or ctx.dynamic:
            self.dynamic(path, line, f"{call.name}(<não literal>)")
            return
        only, exclude = _resource_filter(args[2] if len(args) > 2 else None, variables)
        skip = API_RESOURCE_SKIP if call.name.lower() == "apiresource" else frozenset()
        middleware = _merge(ctx.middleware, _post_middleware(after, variables))
        for action, verb, suffix in RESOURCE_ACTIONS:
            if action in skip or action in exclude or (only and action not in only):
                continue
            target = _controller_target(controller, action, ctx)
            self.emit(verb, base.strip("/") + suffix, path, line, ctx, middleware, target)

    def emit(
        self,
        verb: str,
        uri: str,
        path: str,
        line: int,
        ctx: _Ctx,
        middleware: tuple[str, ...],
        target: tuple[str, str] | str | None,
    ) -> None:
        full = "/" + "/".join(p for p in (*ctx.prefix, uri.strip("/")) if p)
        full = re.sub(r"/{2,}", "/", full)
        rid = route_id(f"{verb} {full}")
        params = [
            {"name": m.group(1), "required": m.group(2) is None} for m in _PARAM.finditer(full)
        ]
        area = _area(path)
        action = "closure" if target == "closure" else _action_text(target)
        self.stats.declarations += 1
        if rid not in self.nodes:
            self.nodes.add(rid)
            self.stats.routes += 1
            self.stats.with_params += bool(params)
            self.stats.by_area[area] = self.stats.by_area.get(area, 0) + 1
            self.stats.by_verb[verb] = self.stats.by_verb.get(verb, 0) + 1
        self.builder.add_node(
            Node(
                rid,
                NodeLabel.ROUTE,
                {
                    "verb": verb,
                    "uri": full,
                    "area": area,
                    "middleware": list(middleware),
                    "namespace": ctx.namespace,
                    "params": params,
                    "action": action,
                    "path": path,
                    "line": line,
                },
            )
        )
        if target == "closure":
            self.stats.closures += 1
        elif isinstance(target, tuple):
            self.route_to(rid, target, path, line)

    def route_to(self, rid: str, target: tuple[str, str], path: str, line: int) -> None:
        controller, method = target
        mid, owner = self._find_method(controller, method)
        if mid is None:
            self.stats.controller_missing += 1
            self.unresolved.add_aggregated(
                "route_controller_missing",
                path,
                line,
                f"{controller}@{method}",
                hint=f"{controller}@{method}",
            )
            return
        evidence: dict[str, Any] = {"path": path, "line": line}
        if owner is not None:
            evidence["inherited_from"] = owner  # método do controller-base, herdado
            self.stats.inherited += 1
        self.builder.add_edge(
            Edge(rid, mid, EdgeType.ROUTES_TO, EdgeSource.LARAVEL_RESOLVER, 1.0, evidence)
        )
        if rid not in self.with_target:
            self.with_target.add(rid)
            self.stats.routes_to += 1

    def _find_method(self, controller: str, method: str) -> tuple[str | None, str | None]:
        """`(Method, classe-pai)`: o método do controller ou, se ele não o declara, o do primeiro
        ancestral (`EXTENDS`) que o declara. `(None, None)` se nenhum o declara. O 2º valor é
        `None` quando o método é do próprio controller."""
        if self._parents is None:
            self._parents = {}
            for edge in self.builder.iter_edges():
                if edge.type == EdgeType.EXTENDS:
                    self._parents.setdefault(edge.src, []).append(edge.dst)
        queue = [class_id(controller)]
        seen = set(queue)
        while queue:
            current = queue.pop(0)
            mid = method_id(key_of(current), method)
            if mid in self.builder and self.builder.node_attrs(mid).get("resolved") is True:
                return mid, None if current == class_id(controller) else key_of(current)
            for parent in self._parents.get(current, ()):
                if parent not in seen:
                    seen.add(parent)
                    queue.append(parent)
        return None, None

    def dynamic(self, path: str, line: int, snippet: str) -> None:
        self.stats.dynamic += 1
        self.unresolved.add("route_dynamic", path, line, snippet)

    def _target(
        self, action: Sym | None, ctx: _Ctx, variables: dict[str, str]
    ) -> tuple[str, str] | str | None:
        """`(controller, método)`, `"closure"` ou `None` (não se resolve)."""
        if action is None:
            return None
        if action.get("t") == "fn":
            return "closure"
        if action.get("t") == "a":
            return _array_target(action, ctx, variables)
        text = evaluate(action, variables)
        if text is None:
            return None
        return _string_target(text, ctx)


# ------------------------------------------------------------------ avaliação


def evaluate(sym: Sym, variables: dict[str, str]) -> str | None:
    """Valor de texto de uma expressão simbólica; `None` se depende de algo desconhecido."""
    kind = sym.get("t")
    if kind == "s" or kind == "k":
        return str(sym.get("v", ""))
    if kind == "v":
        return variables.get(str(sym.get("n")))
    if kind == "c":
        parts = [evaluate(p, variables) for p in sym.get("p", [])]
        return None if any(p is None for p in parts) else "".join(p for p in parts if p is not None)
    if kind == "f" and sym.get("n") == "base_path":
        return _base_path(sym)
    return None


def _strings(sym: Sym, variables: dict[str, str]) -> list[str]:
    """Textos de um literal ou de um array de literais (`middleware`, `match`)."""
    if sym.get("t") == "a":
        out = [evaluate(entry["v"], variables) for entry in sym.get("i", [])]
        return [v for v in out if v is not None]
    value = evaluate(sym, variables)
    return [] if value is None else [value]


def _is_terminal(call: RouteCall) -> bool:
    name = call.name.lower()
    return (
        name in VERBS
        or name in {"any", "match", "group", "resource", "apiresource"}
        or name
        in (
            "redirect",
            "view",
            "fallback",
        )
    )


def _modify(ctx: _Ctx, call: RouteCall, variables: dict[str, str]) -> _Ctx:
    """Aplica um modificador da cadeia (`prefix`, `namespace`, `middleware`, `controller`)."""
    name = call.name.lower()
    arg = call.args[0] if call.args else None
    if arg is None:
        return ctx
    if name == "prefix":
        value = evaluate(arg, variables)
        if value is None:
            return replace(ctx, dynamic=True)
        return replace(
            ctx, prefix=(*ctx.prefix, value.strip("/")) if value.strip("/") else ctx.prefix
        )
    if name == "namespace":
        value = evaluate(arg, variables)
        return (
            ctx if value is None else replace(ctx, namespace=_join_namespace(ctx.namespace, value))
        )
    if name == "middleware":
        return replace(ctx, middleware=_merge(ctx.middleware, _strings(arg, variables)))
    if name == "controller":
        value = evaluate(arg, variables)
        return ctx if value is None else replace(ctx, controller=value)
    return ctx


def _modify_from_array(ctx: _Ctx, array: Sym, variables: dict[str, str]) -> _Ctx:
    """`Route::group(['prefix' => 'x', 'middleware' => [...], 'namespace' => 'N'], …)`."""
    for entry in array.get("i", []):
        key = entry.get("k")
        if not key or key.get("t") != "s":
            continue
        name = str(key["v"]).lower()
        if name in ("prefix", "namespace", "middleware", "controller"):
            ctx = _modify(ctx, RouteCall(name=name, args=[entry["v"]]), variables)
    return ctx


def _merge(current: tuple[str, ...], new: Iterable[str]) -> tuple[str, ...]:
    """Soma `middleware` sem repetir (o provider e o grupo costumam pôr `api` os dois)."""
    return tuple(dict.fromkeys((*current, *new)))


def _post_middleware(after: list[RouteCall], variables: dict[str, str]) -> tuple[str, ...]:
    """`->middleware([...])` depois do verbo vale só para aquela rota."""
    found: tuple[str, ...] = ()
    for call in after:
        if call.name.lower() == "middleware" and call.args:
            found += tuple(_strings(call.args[0], variables))
    return found


def _join_namespace(current: str, new: str) -> str:
    """`namespace` do grupo: começa com `\\` → absoluto; senão soma ao do grupo de fora."""
    clean = _BACKSLASHES.sub(r"\\", new).strip("\\")
    if new.startswith("\\") or not current:
        return clean
    return f"{current}\\{clean}" if clean else current


def _qualify(controller: str, ctx: _Ctx) -> str:
    """Nome do controller com o `namespace` acumulado (`\\App\\X` fica como está)."""
    clean = _BACKSLASHES.sub(r"\\", controller)
    if clean.startswith("\\") or not ctx.namespace:
        return clean.lstrip("\\")
    return f"{ctx.namespace}\\{clean}"


def _string_target(text: str, ctx: _Ctx) -> tuple[str, str] | None:
    if "@" in text:
        controller, _at, method = text.partition("@")
        return (_qualify(controller, ctx), method) if controller and method else None
    if ctx.controller:  # `Route::controller(X::class)->group(...)` + `Route::get('/', 'index')`
        return _controller_target(ctx.controller, text, ctx)
    return (_qualify(text, ctx), "__invoke")  # controller de uma ação só


def _controller_target(controller: str, method: str, ctx: _Ctx) -> tuple[str, str]:
    if "@" in controller:
        controller = controller.partition("@")[0]
    return (_qualify(controller, ctx), method)


def _array_target(array: Sym, ctx: _Ctx, variables: dict[str, str]) -> tuple[str, str] | None:
    items = array.get("i", [])
    for entry in items:  # `['uses' => 'Ctrl@m']`
        key = entry.get("k")
        if key and key.get("t") == "s" and str(key.get("v")).lower() == "uses":
            text = evaluate(entry["v"], variables)
            return _string_target(text, ctx) if text else None
    values = [e["v"] for e in items if e.get("k") is None]
    if len(values) == 2:  # `[Controller::class, 'metodo']`
        controller, method = evaluate(values[0], variables), evaluate(values[1], variables)
        if controller and method:
            return (_qualify(controller, ctx), method)
    return None


def _resource_filter(
    options: Sym | None, variables: dict[str, str]
) -> tuple[frozenset[str], frozenset[str]]:
    only: list[str] = []
    exclude: list[str] = []
    if options and options.get("t") == "a":
        for entry in options.get("i", []):
            key = entry.get("k")
            if key and key.get("t") == "s":
                if str(key["v"]).lower() == "only":
                    only = _strings(entry["v"], variables)
                elif str(key["v"]).lower() == "except":
                    exclude = _strings(entry["v"], variables)
    return frozenset(only), frozenset(exclude)


def _action_text(target: tuple[str, str] | str | None) -> str | None:
    if isinstance(target, tuple):
        return f"{target[0]}@{target[1]}"
    return target


def _area(path: str) -> str:
    """De onde a rota foi declarada: `api`, `web` ou `outro` (o provider, por exemplo)."""
    if path.startswith("routes/api"):
        return "api"
    if path.startswith("routes/web"):
        return "web"
    return "outro"
