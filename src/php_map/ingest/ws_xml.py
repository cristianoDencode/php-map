"""XML de requisição do webservice do e-cidade → um registro por endpoint.

Cada arquivo de `webservices/consulta_dados/requisicoes/**` e
`webservices/processamento_dados/requisicoes/**` declara **um** método SOAP: o nome do método é o
nome do arquivo (`getCgm.xml` → `getCgm`) e as subpastas são só organização. A consulta declara
a tabela (`<from tabela=…>`, mais `<juncoes>`) e os filtros; o processamento declara os
parâmetros, a classe (`<classe nome= caminho=>`) e os métodos que executa.

Não passa pelo extrator PHP nem pelo cache de AST: usa só `xml.etree.ElementTree`.

Os arquivos abrem com `<?xml version='1.0' ?>` **sem** `encoding` e misturam ISO-8859-1 e UTF-8
(numa amostra real, os 5 com acento são UTF-8 e 9 declaram ISO-8859-1). Os
bytes são decodificados aqui (encoding declarado; senão UTF-8 estrito; senão o do perfil) antes de
ir ao parser, que assumiria UTF-8 e quebraria no acento de um arquivo latin-1. Toleram-se também
um BOM e linhas em branco antes da declaração, que existem na instalação real
(`getCalculoAliquotaSimples`, `getInicioAtividade`) e que o PHP aceita.
"""

import re
import xml.etree.ElementTree as ET
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from pathlib import Path

from php_map.profiles.base import WsXmlSource

_BOM = b"\xef\xbb\xbf"
_DECLARED_ENCODING = re.compile(rb"""^<\?xml[^>]*encoding\s*=\s*["']([A-Za-z0-9_.:-]+)["']""")


@dataclass(frozen=True, slots=True)
class WsParam:
    name: str
    required: bool
    type: str | None


@dataclass(frozen=True, slots=True)
class WsClass:
    name: str
    path: str
    methods: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WsEndpointRecord:
    name: str
    kind: str  # "soap_consulta" | "soap_processamento"
    path: str  # relativo à raiz, com `/`
    lines: int
    tables: tuple[str, ...] = ()  # `from` e `join`, como estão no XML sem o alias (`schema.x` ok)
    join_tables: int = 0  # quantas das `tables` vieram de `<juncoes>`
    params: tuple[WsParam, ...] = ()
    classes: tuple[WsClass, ...] = ()


@dataclass(frozen=True, slots=True)
class WsParseError:
    path: str
    message: str


@dataclass(slots=True)
class WsXmlStats:
    found: int = 0  # arquivos `.xml` encontrados nas pastas
    endpoints: int = 0
    by_kind: Counter[str] = field(default_factory=Counter)
    ignored_by_name: int = 0  # `modelo.*.xml`: modelo de requisição, não requisição
    ignored_not_request: int = 0  # raiz diferente da esperada para a pasta
    parse_errors: int = 0
    duplicate_names: int = 0
    join_tables: int = 0  # tabelas que só aparecem em `<juncoes>`
    filters: int = 0
    params: int = 0
    classes: int = 0
    seconds: float = 0.0


def read_ws_xml(
    source: Path,
    sources: Sequence[WsXmlSource],
    ignore_prefixes: Sequence[str],
    encoding: str,
) -> tuple[list[WsEndpointRecord], list[WsParseError], WsXmlStats]:
    """Lê todas as requisições. XML malformado vira `WsParseError`, nunca derruba o scan."""
    records: list[WsEndpointRecord] = []
    errors: list[WsParseError] = []
    stats = WsXmlStats()
    ignore = tuple(prefix.lower() for prefix in ignore_prefixes)
    for spec in sources:
        root_dir = source / spec.directory
        if not root_dir.is_dir():
            continue
        for path in sorted(root_dir.rglob("*.xml")):
            stats.found += 1
            relative = path.relative_to(source).as_posix()
            if path.name.lower().startswith(ignore):
                stats.ignored_by_name += 1
                continue
            try:
                raw = path.read_bytes()
                root = _parse(raw, encoding)
            except (ET.ParseError, OSError, LookupError) as exc:
                stats.parse_errors += 1
                errors.append(WsParseError(relative, str(exc)))
                continue
            if root.tag != spec.root_tag:
                stats.ignored_not_request += 1
                continue
            lines = raw.count(b"\n") + (0 if raw.endswith(b"\n") or not raw else 1)
            if spec.kind == "soap_consulta":
                record = _consulta(root, path, relative, lines, spec.kind)
            else:
                record = _processamento(root, path, relative, lines, spec.kind)
            records.append(record)
            stats.endpoints += 1
            stats.by_kind[spec.kind] += 1
            stats.join_tables += record.join_tables
            stats.params += len(record.params)
            stats.classes += len(record.classes)
            if spec.kind == "soap_consulta":
                stats.filters += len(record.params)
    stats.duplicate_names = sum(n - 1 for n in Counter(r.name for r in records).values() if n > 1)
    return records, errors, stats


def _parse(raw: bytes, default_encoding: str) -> ET.Element:
    """Bytes → árvore. Tira BOM e espaço antes da declaração e decodifica pelo encoding certo."""
    if raw.startswith(_BOM):
        raw = raw[len(_BOM) :]
    raw = raw.lstrip()
    declared = _DECLARED_ENCODING.match(raw)
    if declared:
        return ET.fromstring(raw.decode(declared.group(1).decode("ascii"), errors="replace"))
    try:  # sem declaração, a instalação mistura UTF-8 e ISO-8859-1: UTF-8 estrito decide
        text = raw.decode("utf-8")
    except UnicodeDecodeError:
        text = raw.decode(default_encoding, errors="replace")
    return ET.fromstring(text)


def _table_name(value: str | None) -> str | None:
    """`"cgm a   "` → `cgm`: o 1º token (o resto é o alias)."""
    parts = (value or "").split()
    return parts[0] if parts else None


def _consulta(
    root: ET.Element, path: Path, relative: str, lines: int, kind: str
) -> WsEndpointRecord:
    tables: list[str] = []
    from_element = root.find("from")  # `Element` sem filhos é falso: compare com `None`
    from_table = _table_name(from_element.get("tabela") if from_element is not None else None)
    if from_table:
        tables.append(from_table)
    joins = 0
    for join in root.findall("juncoes/join"):
        name = _table_name(join.get("tabela"))
        if name and name not in tables:
            tables.append(name)
            joins += 1
    params: list[WsParam] = []
    for item in root.findall("filtros/filtro"):
        label = (item.get("label") or "").strip()  # sem label é condição fixa, não parâmetro
        if label and all(label != p.name for p in params):
            params.append(WsParam(label, item.get("obrigatorio", "").strip() == "true", None))
    return WsEndpointRecord(
        path.stem, kind, relative, lines, tuple(tables), joins, tuple(params), ()
    )


def _processamento(
    root: ET.Element, path: Path, relative: str, lines: int, kind: str
) -> WsEndpointRecord:
    params = tuple(
        WsParam(
            str(item.get("nome")).strip(),
            str(item.get("obrigatorio", "")).strip() == "true",
            (item.get("tipo") or "").strip() or None,
        )
        for item in root.findall("parametros/parametro")
        if (item.get("nome") or "").strip()
    )
    # Duas formas no e-cidade: `<classe referencia=…>` + `<exec><metodo referencia=…>` (a mais
    # nova) e `<classe><metodos><metodo/></metodos></classe>` (o modelo antigo, 1.0).
    exec_methods: dict[str, list[str]] = {}
    for item in root.findall("configuracoes/exec/metodo"):
        exec_methods.setdefault(item.get("referencia", ""), []).append(item.get("nome", ""))
    classes: list[WsClass] = []
    for item in root.findall("configuracoes/classe"):
        name = (item.get("nome") or "").strip()
        if not name:
            continue
        methods = [m.get("nome", "") for m in item.findall("metodos/metodo")]
        reference = item.get("referencia")
        if reference:
            methods += exec_methods.get(reference, [])
        unique = tuple(dict.fromkeys(m.strip() for m in methods if m.strip()))
        classes.append(WsClass(name, (item.get("caminho") or "").strip(), unique))
    return WsEndpointRecord(path.stem, kind, relative, lines, (), 0, params, tuple(classes))
