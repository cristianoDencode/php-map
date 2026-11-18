"""Superfície de comunicação SOAP: `Endpoint`, `SERVES` e `CALLS_REMOTE`.

Três fontes, todas determinísticas:

1. **XML de requisição do e-cidade** (`ingest/ws_xml.py`): cada arquivo é um endpoint servido.
   `SERVES` do XML (e dos arquivos PHP que despacham) para o `Endpoint`; a consulta liga o
   endpoint às tabelas (`QUERIES`) e o processamento, à classe e aos métodos que executa
   (`INSTANTIATES`/`CALLS`), tudo com a fonte `WS_XML`.
2. **Quem chama:** chamada estática a um método de `settings.remote_calls` com 1º argumento
   literal (`WebService_Model_Ecidade::consultar('getCgm', ...)` num projeto zend1,
   `IntegracaoNfseHelper::request('PrestacaoContas', ...)` no e-cidade) vira `CALLS_REMOTE` para
   `Endpoint:<quem serve>:soap:<nome>`. O endpoint fica **placeholder** (`resolved=False`): quem
   serve está no grafo do outro projeto, e o ID igual nos dois grafos é a costura.
3. **Quem serve por classe** (`new SoapServer(...)->setClass('X')`, perfil `zend1`): cada método
   público de `X` é um endpoint, com a assinatura em `params`.

Nome de método que não é literal vira `Endpoint:<sistema>:soap:?` (`PHP_AST_AMBIGUOUS`) e a
pendência `dynamic_endpoint`. O `setClass("ConsultaDados")` do servidor do e-cidade **não** gera
endpoint por método: os de verdade são os XML, e o perfil `ecidade` não lista classes de servidor.
"""

import time
from collections import Counter
from collections.abc import Callable, Iterable
from dataclasses import dataclass, field
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
    endpoint_id,
    file_id,
    function_id,
    method_id,
    table_id,
)
from php_map.ingest.records import Call, Declare, PhpFileRecord
from php_map.ingest.table_directory import TableDirectory
from php_map.ingest.ws_xml import WsClass, WsEndpointRecord, read_ws_xml
from php_map.resolve.calls import origin_id
from php_map.resolve.classify import module_hint
from php_map.resolve.outbound import OutboundStats, emit_outbound
from php_map.resolve.rpc import RpcStats, emit_rpc
from php_map.resolve.unresolved import UnresolvedWriter

RecordSource = Iterable[PhpFileRecord] | Callable[[], Iterable[PhpFileRecord]]

DYNAMIC_NAME = "?"
# Nome do método que não é literal: não há como saber quantos candidatos existem (o `1/n` do
# resto do projeto); 0.5 diz "chamada remota certa, endpoint desconhecido".
DYNAMIC_ENDPOINT_CONFIDENCE = 0.5
SERVE_CLASS_KIND = "soap_class"
SERVE_FUNCTION_KIND = "soap_function"


@dataclass(slots=True)
class EndpointStats:
    # lado que serve, pelos XML
    xml_found: int = 0
    xml_ignored_by_name: int = 0
    xml_ignored_not_request: int = 0
    xml_parse_errors: int = 0
    xml_duplicate_names: int = 0
    served_xml: int = 0
    served_by_kind: dict[str, int] = field(default_factory=dict)
    serves_edges: int = 0
    serves_server_edges: int = 0
    queries_edges: int = 0
    join_table_edges: int = 0
    instantiates_edges: int = 0
    calls_edges: int = 0
    classes_confirmed: int = 0
    classes_missing: int = 0
    methods_unconfirmed: int = 0
    params_declared: int = 0
    # lado que serve, por classe de servidor SOAP (setClass/addFunction)
    served_classes: int = 0
    served_by_class: int = 0
    # lado que chama
    remote_calls: int = 0
    remote_calls_dynamic: int = 0
    called_endpoints: int = 0
    called_by_system: dict[str, int] = field(default_factory=dict)
    # RPC por ação de `switch` e chamadas que saem do sistema
    rpc: RpcStats = field(default_factory=RpcStats)
    outbound: OutboundStats = field(default_factory=OutboundStats)
    seconds: float = 0.0


def apply_endpoints(
    records: RecordSource,
    builder: GraphBuilder,
    unresolved: UnresolvedWriter,
    settings: Settings,
    directory: TableDirectory | None = None,
) -> EndpointStats:
    """Cria `Endpoint`, `SERVES` e `CALLS_REMOTE` conforme o perfil.

    **Ordem:** depois da resolução PHP (os nós `Class`/`Method`/`File` já existem, e é neles que
    o endpoint se apoia) e do SQL; antes de `materialize_tables`, para que a tabela que só o XML
    cita também vire nó real quando existir no banco.
    """
    started = time.perf_counter()
    stats = EndpointStats()
    system = settings.endpoint_system
    if system is None:
        return stats
    if settings.ws_xml and settings.source is not None:
        _serve_ws_xml(builder, unresolved, settings, system, directory, stats)
    _scan_php(records, builder, unresolved, settings, system, stats)
    stats.seconds = round(time.perf_counter() - started, 2)
    return stats


# ------------------------------------------------------------------ servir: XML do e-cidade


def _serve_ws_xml(
    builder: GraphBuilder,
    unresolved: UnresolvedWriter,
    settings: Settings,
    system: str,
    directory: TableDirectory | None,
    stats: EndpointStats,
) -> None:
    assert settings.source is not None
    records, errors, xml = read_ws_xml(
        settings.source, settings.ws_xml, settings.ws_xml_ignore_prefixes, settings.encoding
    )
    stats.xml_found = xml.found
    stats.xml_ignored_by_name = xml.ignored_by_name
    stats.xml_ignored_not_request = xml.ignored_not_request
    stats.xml_parse_errors = xml.parse_errors
    stats.xml_duplicate_names = xml.duplicate_names
    for error in errors:
        unresolved.add("ws_xml_parse_error", error.path, 0, error.message[:200])
    known = directory if directory is not None else TableDirectory(settings.search_path)
    servers = {spec.kind: spec.servers for spec in settings.ws_xml}
    declared = _declared_classes(builder) if any(r.classes for r in records) else set()
    seen: dict[str, str] = {}
    for record in records:
        if record.name in seen:  # nome repetido: um nó só; as arestas de cada XML continuam
            unresolved.add(
                "ws_endpoint_duplicate",
                record.path,
                0,
                record.name,
                candidates=[seen[record.name]],
                hint=record.name,
            )
        seen.setdefault(record.name, record.path)
        _emit_ws_record(
            record, builder, unresolved, settings, system, known, servers, declared, stats
        )
    stats.served_xml = len(seen)


def _emit_ws_record(
    record: WsEndpointRecord,
    builder: GraphBuilder,
    unresolved: UnresolvedWriter,
    settings: Settings,
    system: str,
    directory: TableDirectory,
    servers: dict[str, tuple[str, ...]],
    declared: set[tuple[str, str]],
    stats: EndpointStats,
) -> None:
    eid = endpoint_id(system, "soap", record.name)
    params = [
        {"name": p.name, "required": p.required, "type": p.type, "source": "ws_xml"}
        for p in record.params
    ]
    stats.params_declared += len(params)
    stats.served_by_kind[record.kind] = stats.served_by_kind.get(record.kind, 0) + 1
    xml_file = file_id(record.path)
    builder.add_node(
        Node(
            xml_file,
            NodeLabel.FILE,
            {
                "path": record.path,
                "name": record.path.rsplit("/", 1)[-1],
                "parse_status": "ok",
                "lines": record.lines,
                "module_hint": module_hint(record.path),
                "is_infra": False,
                "kind": "ws_xml",
            },
        )
    )
    builder.add_node(
        Node(
            eid,
            NodeLabel.ENDPOINT,
            {
                "name": record.name,
                "system": system,
                "protocol": "soap",
                "kind": record.kind,
                "params": params,
                "path": record.path,
                "line": 1,
                "wsdl": None,
            },
        )
    )
    evidence: dict[str, Any] = {"path": record.path, "line": 1}
    builder.add_edge(Edge(xml_file, eid, EdgeType.SERVES, EdgeSource.WS_XML, 1.0, evidence))
    stats.serves_edges += 1
    for server in servers.get(record.kind, ()):
        if file_id(server) in builder:  # só se o servidor foi escaneado
            builder.add_edge(
                Edge(file_id(server), eid, EdgeType.SERVES, EdgeSource.WS_XML, 1.0, evidence)
            )
            stats.serves_server_edges += 1
    for index, raw in enumerate(record.tables):
        schema, _dot, name = raw.rpartition(".")
        real_schema, _rule = directory.resolve(name, schema or None)
        target = table_id(name, real_schema)
        builder.add_edge(Edge(eid, target, EdgeType.QUERIES, EdgeSource.WS_XML, 1.0, evidence))
        if name.startswith(settings.infra_table_prefixes):
            builder.ensure_node(target, is_infra=True)
        stats.queries_edges += 1
        if index >= len(record.tables) - record.join_tables:
            stats.join_table_edges += 1
    for ws_class in record.classes:
        _emit_ws_class(ws_class, eid, record, builder, unresolved, declared, stats)


def _declared_classes(builder: GraphBuilder) -> set[tuple[str, str]]:
    """`(File, Class)` de cada `DECLARES`: confirma o `caminho=` do XML sem reler arquivo."""
    return {
        (edge.src, edge.dst)
        for edge in builder.iter_edges()
        if edge.type == EdgeType.DECLARES and edge.dst.startswith(f"{NodeLabel.CLASS}:")
    }


def _emit_ws_class(
    ws_class: WsClass,
    eid: str,
    record: WsEndpointRecord,
    builder: GraphBuilder,
    unresolved: UnresolvedWriter,
    declared: set[tuple[str, str]],
    stats: EndpointStats,
) -> None:
    """`INSTANTIATES` para a classe e `CALLS` para cada método do `<exec>`/`<metodos>`.

    A classe vem do índice de símbolos (não se cria `Class` se ela já existe). Confirmada quando
    o `caminho=` existe e a declara; senão a aresta aponta para o placeholder e há a pendência
    `ws_class_missing`: o XML cita uma classe que o código escaneado não tem.
    """
    cid = class_id(ws_class.name)
    evidence: dict[str, Any] = {"path": record.path, "line": 1, "class_path": ws_class.path}
    confirmed = (file_id(ws_class.path), cid) in declared
    if not confirmed and cid in builder:
        confirmed = builder.node_attrs(cid).get("resolved") is True  # declarada em outro arquivo
    builder.add_edge(Edge(eid, cid, EdgeType.INSTANTIATES, EdgeSource.WS_XML, 1.0, evidence))
    stats.instantiates_edges += 1
    if confirmed:
        stats.classes_confirmed += 1
    else:
        stats.classes_missing += 1
        unresolved.add(
            "ws_class_missing",
            record.path,
            0,
            ws_class.name,
            candidates=[ws_class.path] if ws_class.path else [],
            hint=ws_class.name,
        )
    for method in ws_class.methods:
        mid = method_id(ws_class.name, method)
        if mid not in builder or builder.node_attrs(mid).get("resolved") is False:
            stats.methods_unconfirmed += 1  # herdado ou mágico: a aresta existe mesmo assim
        builder.add_edge(Edge(eid, mid, EdgeType.CALLS, EdgeSource.WS_XML, 1.0, evidence))
        stats.calls_edges += 1


# ------------------------------------------------------------------ chamar e servir por PHP


@dataclass(slots=True)
class _ServerCall:
    path: str
    line: int
    kind: str  # "class" | "function"
    target: str  # nome literal, minúsculo


def _scan_php(
    records: RecordSource,
    builder: GraphBuilder,
    unresolved: UnresolvedWriter,
    settings: Settings,
    system: str,
    stats: EndpointStats,
) -> None:
    passes: Callable[[], Iterable[PhpFileRecord]]
    if callable(records):
        passes = records
    else:
        materialized = list(records)
        passes = lambda: materialized  # noqa: E731
    servers: list[_ServerCall] = []
    called: set[str] = set()
    external: set[str] = set()
    for record in passes():
        if settings.rpc_endpoints:
            emit_rpc(record, builder, system, stats.rpc)
        emit_outbound(record, builder, unresolved, settings, stats.outbound, external)
        for call in record.calls:
            if call.kind == "static" and settings.remote_calls:
                _remote_call(record.path, call, builder, unresolved, settings, stats, called)
            elif call.kind == "method" and settings.soap_server_classes:
                server = _server_call(record.path, call, settings)
                if server is not None:
                    servers.append(server)
    stats.called_endpoints = len(called)
    for endpoint in called:
        target = endpoint.split(":")[1]
        stats.called_by_system[target] = stats.called_by_system.get(target, 0) + 1
    if servers:
        _serve_classes(passes, servers, builder, settings, system, stats)


def _remote_call(
    path: str,
    call: Call,
    builder: GraphBuilder,
    unresolved: UnresolvedWriter,
    settings: Settings,
    stats: EndpointStats,
    called: set[str],
) -> None:
    short = (call.class_ or "").rsplit("\\", 1)[-1]
    pattern = next(
        (
            p
            for p in settings.remote_calls
            if short.endswith(p.class_suffix) and call.name in p.methods
        ),
        None,
    )
    if pattern is None:
        return
    stats.remote_calls += 1
    src = origin_id(path, call.scope)
    literal = call.arg0
    evidence: dict[str, Any] = {
        "path": path,
        "line": call.line,
        "args": {"argc": call.argc, "literals": [literal] if literal else []},
    }
    if literal:
        eid = endpoint_id(pattern.target_system, pattern.protocol, literal)
        builder.ensure_node(
            eid, system=pattern.target_system, protocol=pattern.protocol, name=literal
        )
        builder.add_edge(Edge(src, eid, EdgeType.CALLS_REMOTE, EdgeSource.PHP_AST, 1.0, evidence))
        called.add(eid)
        return
    stats.remote_calls_dynamic += 1
    eid = endpoint_id(pattern.target_system, pattern.protocol, DYNAMIC_NAME)
    builder.ensure_node(
        eid, system=pattern.target_system, protocol=pattern.protocol, name=DYNAMIC_NAME
    )
    builder.add_edge(
        Edge(
            src,
            eid,
            EdgeType.CALLS_REMOTE,
            EdgeSource.PHP_AST_AMBIGUOUS,
            DYNAMIC_ENDPOINT_CONFIDENCE,
            evidence,
        )
    )
    snippet = f"{short}::{call.name}(<não literal>)"
    unresolved.add("dynamic_endpoint", path, call.line, snippet, hint=call.scope or None)


def _server_call(path: str, call: Call, settings: Settings) -> _ServerCall | None:
    """`$server->setClass('X')` / `->addFunction('f')` com literal, num `SoapServer`."""
    if call.name not in ("setclass", "addfunction") or not call.arg0:
        return None
    if (call.receiver_class or "").lower() not in settings.soap_server_classes:
        return None
    kind = "class" if call.name == "setclass" else "function"
    return _ServerCall(path, call.line, kind, call.arg0.lstrip("\\").lower())


def _serve_classes(
    passes: Callable[[], Iterable[PhpFileRecord]],
    servers: list[_ServerCall],
    builder: GraphBuilder,
    settings: Settings,
    system: str,
    stats: EndpointStats,
) -> None:
    wanted = {s.target for s in servers if s.kind == "class"}
    methods: dict[str, list[tuple[str, Declare]]] = {}
    if wanted:
        for record in passes():
            for decl in record.declares:
                if decl.kind == "method" and decl.class_ in wanted:
                    methods.setdefault(str(decl.class_), []).append((record.path, decl))
    served_classes: set[str] = set()
    for server in servers:
        if server.kind == "function":
            _serve_function(server, builder, system, stats)
            continue
        served_classes.add(server.target)
        for decl_path, decl in methods.get(server.target, ()):
            _serve_method(server, decl_path, decl, builder, system, stats)
    stats.served_classes = len(served_classes)


def _serve_method(
    server: _ServerCall,
    decl_path: str,
    decl: Declare,
    builder: GraphBuilder,
    system: str,
    stats: EndpointStats,
) -> None:
    """Um método público da classe servida → `Endpoint` (nome na caixa original)."""
    if decl.visibility not in (None, "public") or decl.name.startswith("__"):
        return
    name = decl.display_name or decl.name
    eid = endpoint_id(system, "soap", name)
    params = [
        {"name": p.name, "required": not p.has_default, "type": p.type, "source": "php_signature"}
        for p in decl.params
    ]
    builder.add_node(
        Node(
            eid,
            NodeLabel.ENDPOINT,
            {
                "name": name,
                "system": system,
                "protocol": "soap",
                "kind": SERVE_CLASS_KIND,
                "params": params,
                "path": decl_path,
                "line": decl.line_start,
                "wsdl": None,  # o WSDL chega ao `SoapServer` numa variável, nunca literal
                "class": server.target,
            },
        )
    )
    evidence: dict[str, Any] = {"path": server.path, "line": server.line, "class": server.target}
    src = method_id(server.target, decl.name)
    builder.add_edge(Edge(src, eid, EdgeType.SERVES, EdgeSource.PHP_AST, 1.0, evidence))
    stats.served_by_class += 1
    stats.params_declared += len(params)


def _serve_function(
    server: _ServerCall, builder: GraphBuilder, system: str, stats: EndpointStats
) -> None:
    """`addFunction('nome')`: a função é o endpoint; o extrator só dá assinatura de método."""
    src = function_id(server.target)
    if src not in builder:
        return
    eid = endpoint_id(system, "soap", server.target)
    builder.add_node(
        Node(
            eid,
            NodeLabel.ENDPOINT,
            {
                "name": server.target,
                "system": system,
                "protocol": "soap",
                "kind": SERVE_FUNCTION_KIND,
                "params": [],
                "path": server.path,
                "line": server.line,
                "wsdl": None,
            },
        )
    )
    evidence: dict[str, Any] = {"path": server.path, "line": server.line}
    builder.add_edge(Edge(src, eid, EdgeType.SERVES, EdgeSource.PHP_AST, 1.0, evidence))
    stats.served_by_class += 1


def endpoint_counts(builder: GraphBuilder) -> Counter[str]:
    """Quantos `Endpoint` por sistema e protocolo, para o resumo (`system:protocol`)."""
    counts: Counter[str] = Counter()
    for node in builder.iter_nodes():
        if node.label == NodeLabel.ENDPOINT:
            counts[f"{node.attrs.get('system')}:{node.attrs.get('protocol')}"] += 1
    return counts
