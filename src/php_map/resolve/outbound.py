"""Chamadas que saem do sistema: `SoapClient`, `Zend_Soap_Client` e `curl`.

O extrator (schema_version 7) entrega cada `new SoapClient(...)`, `curl_init(...)` e
`curl_setopt(..., CURLOPT_URL, ...)` com o destino **simbólico** (`{t: …}`, ver
`php-extractor/src/SymbolicRoutes.php`). Aqui o destino é avaliado:

- literal (`http://servidor/ws?wsdl`) → `Endpoint:externo:<soap|http>:<host><caminho>`, com `url`;
- qualquer outra coisa (`env('X')`, constante, variável, `$this->url`) → `Endpoint:externo:<…>:?`,
  com a origem em `evidence.via` quando dá (`env:NFSE_LOCATION`, `$wsdl`);
- a aresta é `CALLS_REMOTE` do `Method`/`Function`/`File` que chama, `PHP_AST` (literal, 1.0) ou
  `PHP_AST_AMBIGUOUS` (0.5) com a pendência `dynamic_endpoint`.

O endpoint externo é sempre placeholder (`resolved=False`): não existe grafo de quem serve. A
integração entre os sistemas tem precedência: o arquivo que declara a classe cliente de um
`remote_calls` (`IntegracaoNfseHelper`, `WebService_Model_Ecidade`) já gerou `CALLS_REMOTE` para o
endpoint do outro sistema, então o `SoapClient` de dentro dele não vira um `externo` duplicado.
**Nunca** se lê o `.env` nem se supõe o host de `env(...)`.
"""

import re
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import urlsplit

from php_map.config import Settings
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import (
    Edge,
    EdgeSource,
    EdgeType,
    Node,
    NodeLabel,
    endpoint_id,
)
from php_map.ingest.records import Outbound, PhpFileRecord
from php_map.resolve.calls import origin_id
from php_map.resolve.unresolved import UnresolvedWriter

SYSTEM = "externo"
UNKNOWN = "?"
# Sem saber o destino não há `1/n`: a chamada é certa e o endpoint é que não se conhece.
DYNAMIC_CONFIDENCE = 0.5
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*://")
_PROTOCOL = {"soap": "soap", "http": "http"}


@dataclass(slots=True)
class OutboundStats:
    calls: int = 0  # chamadas de saída (depois de descartar o `curl_init()` coberto por outra)
    literal: int = 0
    dynamic: int = 0
    skipped_integration: int = (
        0  # cobertas pela integração entre sistemas (`Endpoint:zend1app:*` etc.)
    )
    endpoints: int = 0  # `Endpoint:externo:*` distintos
    unknown_target: int = 0  # chamadas cujo destino é `?`
    by_protocol: dict[str, int] = field(default_factory=dict)


def integration_file(record: PhpFileRecord, settings: Settings) -> bool:
    """O arquivo declara a classe cliente de uma integração conhecida (`remote_calls`)?"""
    suffixes = tuple(p.class_suffix for p in settings.remote_calls)
    if not suffixes:
        return False
    return any(
        d.kind == "class" and d.name.rsplit("\\", 1)[-1].endswith(suffixes) for d in record.declares
    )


def emit_outbound(
    record: PhpFileRecord,
    builder: GraphBuilder,
    unresolved: UnresolvedWriter,
    settings: Settings,
    stats: OutboundStats,
    seen: set[str],
) -> None:
    """`seen` guarda os `Endpoint:externo:*` já criados, para contar os distintos."""
    if not record.outbound:
        return
    if integration_file(record, settings):
        stats.skipped_integration += len(record.outbound)
        return
    informed = {(o.scope, o.kind) for o in record.outbound if o.url is not None}
    for item in record.outbound:
        # `curl_init()` sem URL só vale se o mesmo escopo não disser a URL em outro lugar
        if item.url is None and item.callee == "curl_init" and (item.scope, item.kind) in informed:
            continue
        _emit(record.path, item, builder, unresolved, stats, seen)


def _emit(
    path: str,
    item: Outbound,
    builder: GraphBuilder,
    unresolved: UnresolvedWriter,
    stats: OutboundStats,
    seen: set[str],
) -> None:
    protocol = _PROTOCOL[item.kind]
    url, via = destination(item.url)
    src = origin_id(path, item.scope)
    stats.calls += 1
    stats.by_protocol[protocol] = stats.by_protocol.get(protocol, 0) + 1
    evidence: dict[str, Any] = {"path": path, "line": item.line, "callee": item.callee}
    if url is not None:
        name = _target_name(url)
        eid = endpoint_id(SYSTEM, protocol, name)
        attrs: dict[str, Any] = {"url": url}
        stats.literal += 1
        source, confidence = EdgeSource.PHP_AST, 1.0
    else:
        name = UNKNOWN
        eid = endpoint_id(SYSTEM, protocol, name)
        attrs = {"url": None}
        evidence["via"] = via
        stats.dynamic += 1
        stats.unknown_target += 1
        source, confidence = EdgeSource.PHP_AST_AMBIGUOUS, DYNAMIC_CONFIDENCE
    if eid not in seen:
        seen.add(eid)
        stats.endpoints += 1
    builder.add_node(
        Node(
            eid,
            NodeLabel.ENDPOINT,
            {
                "name": name,
                "system": SYSTEM,
                "protocol": protocol,
                "kind": f"external_{protocol}",
                "params": [],
                "resolved": False,  # não há grafo de quem serve
                **attrs,
            },
        )
    )
    builder.add_edge(Edge(src, eid, EdgeType.CALLS_REMOTE, source, confidence, evidence))
    if url is None:
        snippet = f"{item.callee}({via or '?'})"
        unresolved.add("dynamic_endpoint", path, item.line, snippet, hint=item.scope or None)


def destination(sym: dict[str, Any] | None) -> tuple[str | None, str | None]:
    """`(url, via)`: a URL literal (ou o prefixo literal de uma concatenação) ou, sem ela,
    de onde o valor vem (`env:NFSE_LOCATION`, `$wsdl`, o trecho do código)."""
    if sym is None:
        return None, None
    kind = sym.get("t")
    if kind == "s":
        value = str(sym.get("v", ""))
        return (value, None) if _SCHEME.match(value) else (None, f"literal:{value[:60]}")
    if kind == "c":
        prefix = ""
        for part in sym.get("p", []):
            if part.get("t") != "s":
                break
            prefix += str(part.get("v", ""))
        if _SCHEME.match(prefix):  # o host está no começo; o resto é só o caminho
            return prefix, None
        return None, _via(next((p for p in sym.get("p", []) if p.get("t") != "s"), sym))
    return None, _via(sym)


def _via(sym: dict[str, Any]) -> str | None:
    kind = sym.get("t")
    if kind == "v":
        return f"${sym.get('n')}"
    if kind == "const":
        return str(sym.get("n"))
    if kind == "f":
        args = sym.get("a", [])
        first = args[0] if args and args[0].get("t") == "s" else None
        return f"{sym.get('n')}:{first['v']}" if first else f"{sym.get('n')}()"
    if kind == "?":
        return str(sym.get("s") or "")[:60] or None
    return None


def _target_name(url: str) -> str:
    """`host[:porta]` + caminho, sem esquema, sem credencial e sem query (o ID é o destino)."""
    parts = urlsplit(url)
    host = parts.netloc.rsplit("@", 1)[-1]
    path = parts.path.rstrip("/")
    return f"{host}{path}" if host else url
