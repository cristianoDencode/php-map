"""Tela/JS → `Route` do Laravel pela URL literal: `CALLS_HTTP`/`JS_URL`.

A URL que o JS chama é quase sempre `PHPSession.requestApi + '/tributario/…/search/'`: a base
(`ECIDADE_REQUEST_PATH . 'v4/api'`, `con4_ecidadeinfo.RPC.php:69`) não é literal, mas o arquivo
que a usa cita `requestApi` ou `v4/api` (`has_api_prefix`). Casamento (`match_literal`):

- tira `?query`/`#…`, as barras repetidas e a final; exige 2 segmentos que não sejam curinga e
  descarta o que termina em arquivo (`x.js`, `y.php`);
- com `has_api_prefix`, testa primeiro `/v4/api` + literal; depois o literal como está (rotas
  `web` e URLs já completas);
- segmento a segmento: `{p}`/`{p?}` da rota aceita qualquer um, `{}` (o `${…}` do template)
  casa com qualquer um, `{p?}` no fim pode faltar; literal terminado em `/` (concatenado com um
  id) casa também com a rota que tem **um** segmento `{param}` a mais;
- especificidade (`RouteIndex.prefer`): a rota que casa uma palavra do literal literalmente vence
  a que a absorve num `{param}`; sem ela, a palavra que é segmento literal de outra rota com o
  mesmo começo (`…/controle-permissao/editar` com `editar/{id}` declarado) não vale como valor;
- uma aresta por rota casada: 0.9 com uma, 0.9/n com n (os verbos da mesma URI); o verbo do
  `HttpClient.get|post` não decide (fora de escopo).

Sem casamento: com `has_api_prefix` vira a pendência `http_url_dynamic` (`hint` = a URI
testada); sem, é ruído e só conta.

**Ordem no pipeline:** depois das rotas do Laravel (as `Route` precisam existir) e da resolução
PHP (as referências das telas), antes do SQL. Não depende de catálogo nem de schema.
"""

import re
import time
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from fnmatch import fnmatchcase
from typing import Protocol

from php_map.config import Settings
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import (
    JS_URL_CONFIDENCE,
    Edge,
    EdgeSource,
    EdgeType,
    NodeLabel,
    file_id,
)
from php_map.ingest.js_scan import WILDCARD
from php_map.ingest.records import HttpLiteral, JsRecord, PhpFileRecord
from php_map.resolve.js_files import JsLinker, JsLinkStats, list_js_files
from php_map.resolve.unresolved import UnresolvedWriter

API_PREFIX = "/v4/api"
_API_SEGMENTS = ("v4", "api")
_QUERY = re.compile(r"[?#]")
_SLASHES = re.compile(r"/{2,}")
_ROUTE_PARAM = re.compile(r"^\{\w+(\?)?\}$")
_FILE_LIKE = re.compile(r"\.[A-Za-z0-9]+$")
MIN_SEGMENTS = 2  # literais curtos (`'/'`, `'/index'`) casariam com muita rota


class HttpSource(Protocol):
    """O que o casador lê de um registro PHP (`<script>` inline) ou JS."""

    @property
    def path(self) -> str: ...
    @property
    def http_literals(self) -> list[HttpLiteral]: ...
    @property
    def has_api_prefix(self) -> bool: ...


@dataclass(frozen=True, slots=True)
class RouteRef:
    id: str
    segments: tuple[str, ...]
    params: tuple[bool, ...]  # segmento é `{param}`?
    optional_tail: int  # quantos `{param?}` no fim (podem faltar)


@dataclass(frozen=True, slots=True)
class MatchResult:
    routes: tuple[str, ...]
    prefix: str | None  # `/v4/api` quando casou com o prefixo
    tested: str  # a URI testada primeiro (o `hint` da pendência)
    rejected: str | None = None  # `short` ou `file` (não é candidato a rota)


class RouteIndex:
    """As `Route` do Laravel (com `verb` e `uri`) agrupadas pelo nº de segmentos."""

    def __init__(self, routes: Iterable[tuple[str, str]]) -> None:
        self._by_len: dict[int, list[RouteRef]] = {}
        self._all: list[RouteRef] = []
        self.size = 0
        for rid, uri in routes:
            segments = tuple(s for s in uri.split("/") if s)
            params = tuple(bool(_ROUTE_PARAM.match(s)) for s in segments)
            tail = 0
            for segment in reversed(segments):
                match = _ROUTE_PARAM.match(segment)
                if not match or not match.group(1):
                    break
                tail += 1
            ref = RouteRef(rid, segments, params, tail)
            for length in range(len(segments) - tail, len(segments) + 1):
                self._by_len.setdefault(length, []).append(ref)
            self._all.append(ref)
            self.size += 1

    @classmethod
    def from_builder(cls, builder: GraphBuilder) -> "RouteIndex":
        return cls(
            (node.id, str(node.attrs["uri"]))
            for node in builder.iter_nodes()
            if node.label == NodeLabel.ROUTE and node.attrs.get("verb") and node.attrs.get("uri")
        )

    def match(self, segments: Sequence[str], extra_param: bool = False) -> list[RouteRef]:
        """Rotas que casam com `segments`; `extra_param`: as que têm um `{param}` a mais no fim."""
        length = len(segments) + (1 if extra_param else 0)
        found: list[RouteRef] = []
        for ref in self._by_len.get(length, ()):
            if extra_param and (len(ref.segments) != length or not ref.params[-1]):
                continue
            if all(_segment_matches(lit, ref, i) for i, lit in enumerate(segments)):
                found.append(ref)
        return found

    def prefer(self, segments: Sequence[str], refs: Sequence[RouteRef]) -> list[RouteRef]:
        """Especificidade: um segmento de texto do literal (`salvar`) casado **literalmente** vence
        o `{param}` que o absorve. Sem nenhuma rota assim, a palavra só vale como valor do
        `{param}` (`mostrarTodos/false`) se não for segmento literal de outra rota com o mesmo
        começo (`controle-permissao/editar/{id}` veta `editar` em `controle-permissao/{ano}`)."""
        absorbed = {ref.id: _absorbed(segments, ref) for ref in refs}
        exact = [ref for ref in refs if not absorbed[ref.id]]
        if exact:
            return exact
        return [
            ref
            for ref in refs
            if not any(self._is_path_word(segments, i) for i in absorbed[ref.id])
        ]

    def _is_path_word(self, segments: Sequence[str], index: int) -> bool:
        word = segments[index]
        return any(
            len(ref.segments) > index
            and not ref.params[index]
            and ref.segments[index] == word
            and all(_segment_matches(segments[j], ref, j) for j in range(index))
            for ref in self._all
        )


def _absorbed(segments: Sequence[str], ref: RouteRef) -> list[int]:
    """Posições em que um `{param}` da rota recebe um segmento de texto (sem curinga)."""
    return [i for i, lit in enumerate(segments) if ref.params[i] and WILDCARD not in lit]


def _segment_matches(literal: str, ref: RouteRef, index: int) -> bool:
    if ref.params[index] or literal == WILDCARD:
        return True
    route = ref.segments[index]
    if WILDCARD in literal:  # `abc${x}`: o curinga ocupa parte do segmento
        return fnmatchcase(route, literal.replace(WILDCARD, "*"))
    return literal == route


def literal_segments(text: str) -> tuple[tuple[str, ...], bool]:
    """Segmentos do literal (sem query, barras repetidas e a final) e se terminava em `/`."""
    head = _SLASHES.sub("/", _QUERY.split(text, maxsplit=1)[0].strip())
    return tuple(s for s in head.split("/") if s), head.endswith("/")


def match_literal(text: str, has_api_prefix: bool, index: RouteIndex) -> MatchResult:
    segments, trailing = literal_segments(text)
    plain = "/" + "/".join(segments)
    with_base = has_api_prefix and segments[:2] != _API_SEGMENTS
    tested = API_PREFIX + plain if with_base else plain
    own = segments[2:] if segments[:2] == _API_SEGMENTS else segments  # `'v4/api'` é só a base
    if sum(1 for s in own if WILDCARD not in s) < MIN_SEGMENTS:
        return MatchResult((), None, tested, "short")
    if segments and _FILE_LIKE.search(segments[-1]):
        return MatchResult((), None, tested, "file")
    attempts: list[tuple[str | None, tuple[str, ...]]] = []
    if with_base:
        attempts.append((API_PREFIX, (*_API_SEGMENTS, *segments)))
    attempts.append((None, segments))
    for prefix, candidate in attempts:
        refs = index.match(candidate)
        if trailing:
            seen = {ref.id for ref in refs}
            refs += [ref for ref in index.match(candidate, extra_param=True) if ref.id not in seen]
        found = index.prefer(candidate, refs)
        if found:
            return MatchResult(tuple(sorted(ref.id for ref in found)), prefix, tested)
    return MatchResult((), None, tested)


@dataclass(slots=True)
class HttpStats:
    literals: int = 0  # URLs com cara de rota lidas (tela + JS)
    from_screens: int = 0  # das quais, do `<script>` inline de um PHP
    matched: int = 0  # literais com pelo menos uma rota
    ambiguous: int = 0  # casados com mais de uma rota (0.9/n)
    unmatched: int = 0  # com `has_api_prefix` e sem rota: `http_url_dynamic`
    discarded: int = 0  # sem `has_api_prefix` e sem rota (ruído)
    rejected: dict[str, int] = field(default_factory=dict)  # `short`/`file`
    with_prefix: int = 0  # casados pelo `/v4/api` + literal
    edges: int = 0  # `CALLS_HTTP` (literal × rota, antes da agregação)
    routes: int = 0  # `Route` do Laravel no grafo
    routes_called: int = 0  # `Route` com pelo menos um `CALLS_HTTP` chegando
    routes_called_api: int = 0  # das quais, `area = api`
    sources_with_calls: int = 0  # arquivos (tela ou JS) com algum `CALLS_HTTP`
    js_files: int = 0  # nós `File` com `kind = "js"`
    js: JsLinkStats = field(default_factory=JsLinkStats)
    seconds: float = 0.0


def resolve_http_calls(
    records: Iterable[HttpSource],
    js_records: Iterable[JsRecord],
    builder: GraphBuilder,
    unresolved: UnresolvedWriter,
    stats: HttpStats | None = None,
) -> HttpStats:
    """`CALLS_HTTP` de cada URL literal (dos PHP e dos JS) para as `Route` que casam."""
    stats = stats if stats is not None else HttpStats()
    index = RouteIndex.from_builder(builder)
    stats.routes = index.size
    called: set[str] = set()
    for source in (*records, *js_records):
        if not source.http_literals:
            continue
        linked = False
        for literal in source.http_literals:
            stats.literals += 1
            stats.from_screens += not isinstance(source, JsRecord)
            result = match_literal(literal.text, source.has_api_prefix, index)
            if result.rejected:
                stats.rejected[result.rejected] = stats.rejected.get(result.rejected, 0) + 1
                continue
            if not result.routes:
                if source.has_api_prefix:
                    stats.unmatched += 1
                    unresolved.add_aggregated(
                        "http_url_dynamic",
                        source.path,
                        literal.line,
                        literal.text,
                        (),
                        result.tested,
                    )
                else:
                    stats.discarded += 1
                continue
            stats.matched += 1
            stats.ambiguous += len(result.routes) > 1
            stats.with_prefix += result.prefix is not None
            _add_edges(source.path, literal, result, builder)
            stats.edges += len(result.routes)
            called.update(result.routes)
            linked = True
        stats.sources_with_calls += linked
    stats.routes_called = len(called)
    stats.routes_called_api = sum(
        1 for rid in called if builder.node_attrs(rid).get("area") == "api"
    )
    return stats


def _add_edges(path: str, literal: HttpLiteral, result: MatchResult, builder: GraphBuilder) -> None:
    confidence = JS_URL_CONFIDENCE / len(result.routes)
    evidence: dict[str, object] = {
        "path": path,
        "line": literal.line,
        "literal": literal.text,
        "prefix": result.prefix,
    }
    if len(result.routes) > 1:
        evidence["candidates"] = list(result.routes)
    for rid in result.routes:
        builder.add_edge(
            Edge(file_id(path), rid, EdgeType.CALLS_HTTP, EdgeSource.JS_URL, confidence, evidence)
        )


def apply_http_calls(
    records: Iterable[PhpFileRecord],
    builder: GraphBuilder,
    unresolved: UnresolvedWriter,
    settings: Settings,
) -> HttpStats:
    """Tela → JS (`REFERENCES`), JS → tela e tela/JS → rota (`CALLS_HTTP`), numa passada só
    pelos registros PHP. Sem `settings.source` não há JS: só o `<script>` inline das telas."""
    started = time.perf_counter()
    linker = JsLinker(list_js_files(settings.source, settings), builder, settings)
    php_files: set[str] = set()
    inline: list[PhpFileRecord] = []
    for record in records:
        php_files.add(record.path)
        linker.link(record)
        if record.http_literals:
            inline.append(record)
    js_records = linker.read(settings.source)
    linker.link_js_refs(js_records, php_files)
    stats = HttpStats(js=linker.stats, js_files=linker.stats.js_files)
    resolve_http_calls(inline, js_records, builder, unresolved, stats)
    stats.seconds = round(time.perf_counter() - started, 2)
    return stats
