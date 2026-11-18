"""Contrato da ontologia do grafo: labels, tipos, fontes e IDs."""

import math
import re
import unicodedata
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import PurePath
from types import MappingProxyType
from typing import Any

from php_map.errors import GraphContractError

_EMPTY: Mapping[str, Any] = MappingProxyType({})


class NodeLabel(StrEnum):
    FILE = "File"
    FUNCTION = "Function"
    CLASS = "Class"
    METHOD = "Method"
    ROUTE = "Route"
    RPC_CALL = "RpcCall"
    PG_FUNCTION = "PgFunction"
    TABLE = "Table"
    MODULE = "Module"
    BUSINESS_ACTION = "BusinessAction"
    ENDPOINT = "Endpoint"


class EdgeType(StrEnum):
    DECLARES = "DECLARES"
    INCLUDES = "INCLUDES"
    REQUIRES = "REQUIRES"
    CALLS = "CALLS"
    INSTANTIATES = "INSTANTIATES"
    EXTENDS = "EXTENDS"
    IMPLEMENTS = "IMPLEMENTS"
    REFERENCES = "REFERENCES"
    ROUTES_TO = "ROUTES_TO"
    RENDERS = "RENDERS"
    MAPS_TO = "MAPS_TO"
    SERVES = "SERVES"
    CALLS_REMOTE = "CALLS_REMOTE"
    CALLS_RPC = "CALLS_RPC"
    QUERIES = "QUERIES"
    INSERTS = "INSERTS"
    UPDATES = "UPDATES"
    DELETES = "DELETES"
    TRIGGERS = "TRIGGERS"
    CALLS_DYNAMIC = "CALLS_DYNAMIC"
    BELONGS_TO = "BELONGS_TO"
    IMPLEMENTS_ACTION = "IMPLEMENTS_ACTION"
    # tela PHP ou arquivo JS → `Route` do Laravel que a URL literal chama.
    CALLS_HTTP = "CALLS_HTTP"


class EdgeSource(StrEnum):
    PHP_AST = "PHP_AST"
    PHP_AST_AMBIGUOUS = "PHP_AST_AMBIGUOUS"
    STRING_REF = "STRING_REF"
    SQL_AST = "SQL_AST"
    SQL_REGEX = "SQL_REGEX"
    LARAVEL_RESOLVER = "LARAVEL_RESOLVER"
    ZEND1_RESOLVER = "ZEND1_RESOLVER"
    DOCTRINE_ANNOTATION = "DOCTRINE_ANNOTATION"
    WS_XML = "WS_XML"
    ECIDADE_CATALOG = "ECIDADE_CATALOG"
    NAMING_RULE = "NAMING_RULE"
    HEURISTIC_AI = "HEURISTIC_AI"
    AI_CLASSIFICATION = "AI_CLASSIFICATION"
    # URL literal escrita no JS (ou no `<script>` inline) casada com a rota.
    JS_URL = "JS_URL"
    # model Eloquent → tabela (`$table` literal ou a convenção do Laravel).
    ELOQUENT_MODEL = "ELOQUENT_MODEL"


STRING_REF_CONFIDENCE = 0.9
# uma rota casada vale 0.9; `n` rotas (verbos diferentes, mesma URI) valem 0.9/n.
JS_URL_CONFIDENCE = 0.9
SQL_REGEX_MAX_CONFIDENCE = 0.7
# `@Entity` sem `@Table(name=)`: a tabela é deduzida do nome da classe.
DOCTRINE_CONVENTION_CONFIDENCE = 0.8
# Model Eloquent sem `$table`: a tabela é deduzida do nome da classe.
ELOQUENT_CONVENTION_CONFIDENCE = 0.8

# Fontes determinísticas: sempre 1.0. Exceção: CALLS_RPC por SQL_AST pode ser 1/n (sobrecargas).
_EXACT_SOURCES = frozenset(
    {
        EdgeSource.PHP_AST,
        EdgeSource.SQL_AST,
        EdgeSource.LARAVEL_RESOLVER,
        EdgeSource.ZEND1_RESOLVER,  # convenção de nomes do Zend 1
        EdgeSource.ECIDADE_CATALOG,  # dicionário e menus do e-cidade
        EdgeSource.WS_XML,  # XML de requisição do webservice do e-cidade
    }
)
_NOT_ALNUM = re.compile(r"[^a-z0-9]")


def _key(name: str) -> str:
    return name.strip().lstrip("\\").lower()


def file_id(path: str | PurePath) -> str:
    """`File:<caminho relativo com />`. Não altera a caixa: caminhos são case-sensitive."""
    text = path.as_posix() if isinstance(path, PurePath) else path.replace("\\", "/")
    return f"{NodeLabel.FILE}:{text.removeprefix('./')}"


def function_id(name: str) -> str:
    return f"{NodeLabel.FUNCTION}:{_key(name)}"


def class_id(name: str) -> str:
    return f"{NodeLabel.CLASS}:{_key(name)}"


def method_id(cls: str, method: str) -> str:
    return f"{NodeLabel.METHOD}:{_key(cls)}::{_key(method)}"


def table_id(name: str, schema: str | None = None) -> str:
    """`Table:<schema>.<nome>` em minúsculas; sem schema → `public`.

    Sem `schema`, um nome qualificado (`issqn.exemplo`) é separado no primeiro ponto.
    """
    if schema is None and "." in name:
        schema, name = name.split(".", 1)
    return f"{NodeLabel.TABLE}:{_key(schema or 'public')}.{_key(name)}"


def pgfunction_id(schema: str | None, name: str, argtypes: Sequence[str]) -> str:
    """`PgFunction:<schema>.<nome>(<tipos>)`. Normalizar tipos (integer → int4) é do chamador."""
    args = ",".join(_key(arg) for arg in argtypes)
    return f"{NodeLabel.PG_FUNCTION}:{_key(schema or 'public')}.{_key(name)}({args})"


def rpccall_id(name: str) -> str:
    return f"{NodeLabel.RPC_CALL}:{_key(name)}"


def route_id(key: str) -> str:
    """`Route:<chave>`; a chave é `<VERBO> <uri>` (Laravel) ou `<modulo>/<controller>/<action>`
    (Zend 1, sem verbo: o Zend 1 não separa rota por método HTTP). Sem mudar a caixa."""
    return f"{NodeLabel.ROUTE}:{key}"


def normalize_module_name(name: str) -> str:
    """Minúsculas, sem acento, só `[a-z0-9]`: `'e-Social'` e `'esocial'` → `'esocial'`."""
    decomposed = unicodedata.normalize("NFKD", name)
    plain = "".join(ch for ch in decomposed if not unicodedata.combining(ch))
    return _NOT_ALNUM.sub("", plain.lower())


def module_id(name: str) -> str:
    """`Module:<nome normalizado>`. Nome sem nenhum `[a-z0-9]` → `GraphContractError`."""
    key = normalize_module_name(name)
    if not key:
        raise GraphContractError(f"nome de módulo vazio depois de normalizado: {name!r}")
    return f"{NodeLabel.MODULE}:{key}"


def business_action_id(key: str) -> str:
    """`BusinessAction:<chave>`; do menu do e-cidade a chave é `menu:<id_item>`."""
    return f"{NodeLabel.BUSINESS_ACTION}:{key.strip()}"


def endpoint_id(system: str, protocol: str, name: str) -> str:
    """`Endpoint:<sistema que serve>:<protocolo>:<nome>`.

    O sistema é **quem serve**, nunca quem chama: é isso que faz o mesmo ID existir nos dois
    grafos (o `Endpoint` chamado vira placeholder no grafo de quem chama) e o `cross` ser um
    join pelo ID. O nome mantém a caixa do código (`getDadosEmpresa`); `?` é o nome não literal.
    """
    parts = (system.strip().lower(), protocol.strip().lower(), name.strip())
    return f"{NodeLabel.ENDPOINT}:{parts[0]}:{parts[1]}:{parts[2]}"


def label_of(node_id: str) -> NodeLabel:
    """Label a partir do prefixo do ID; prefixo desconhecido → `GraphContractError`."""
    prefix, sep, key = node_id.partition(":")
    if not sep or not key:
        raise GraphContractError(f"ID de nó sem o formato <Label>:<chave>: {node_id!r}")
    try:
        return NodeLabel(prefix)
    except ValueError:
        raise GraphContractError(f"label desconhecido no ID {node_id!r}") from None


def key_of(node_id: str) -> str:
    return node_id.partition(":")[2]


@dataclass(frozen=True, slots=True)
class Node:
    id: str
    label: NodeLabel
    attrs: Mapping[str, Any] = field(default=_EMPTY)


@dataclass(frozen=True, slots=True)
class Edge:
    src: str
    dst: str
    type: EdgeType
    source: EdgeSource
    confidence: float
    evidence: Mapping[str, Any]
    attrs: Mapping[str, Any] = field(default=_EMPTY)


def validate_node(node: Node) -> None:
    if label_of(node.id) != node.label:
        raise GraphContractError(f"ID {node.id!r} não corresponde ao label {node.label}")


def validate_edge(edge: Edge) -> None:
    """Regras de confiança e evidência da ontologia. Violação → `GraphContractError`."""
    where = f"{edge.src} -[{edge.type}/{edge.source}]-> {edge.dst}"
    confidence = edge.confidence
    if math.isnan(confidence) or not 0.0 <= confidence <= 1.0:
        raise GraphContractError(f"confidence {confidence} fora de [0, 1] em {where}")
    if edge.source in _EXACT_SOURCES:
        overload = edge.source == EdgeSource.SQL_AST and edge.type == EdgeType.CALLS_RPC
        if confidence != 1.0 and not (overload and confidence > 0.0):
            raise GraphContractError(f"{edge.source} exige confidence 1.0 em {where}")
    elif edge.source == EdgeSource.STRING_REF:
        if confidence != STRING_REF_CONFIDENCE:
            raise GraphContractError(f"STRING_REF exige confidence 0.9 em {where}")
    elif edge.source == EdgeSource.JS_URL:
        candidates = round(JS_URL_CONFIDENCE / confidence) if confidence > 0.0 else 0
        if candidates < 1 or not math.isclose(confidence, JS_URL_CONFIDENCE / candidates):
            raise GraphContractError(f"JS_URL exige confidence 0.9 ou 0.9/n em {where}")
    elif edge.source == EdgeSource.SQL_REGEX:
        if confidence > SQL_REGEX_MAX_CONFIDENCE:
            raise GraphContractError(f"SQL_REGEX aceita no máximo 0.7 em {where}")
    elif edge.source == EdgeSource.DOCTRINE_ANNOTATION:
        # Declaração do sistema (1.0) ou, sem `@Table`, deduzida do nome da classe (0.8).
        if confidence not in (1.0, DOCTRINE_CONVENTION_CONFIDENCE):
            raise GraphContractError(f"DOCTRINE_ANNOTATION exige 1.0 ou 0.8 em {where}")
    elif edge.source == EdgeSource.ELOQUENT_MODEL:
        # `$table` literal (1.0) ou, sem ele, a convenção do Laravel (0.8).
        if confidence not in (1.0, ELOQUENT_CONVENTION_CONFIDENCE):
            raise GraphContractError(f"ELOQUENT_MODEL exige 1.0 ou 0.8 em {where}")
    elif confidence >= 1.0:
        raise GraphContractError(f"{edge.source} não pode ter confidence 1.0 em {where}")
    if not ("path" in edge.evidence or "cache_key" in edge.evidence):
        raise GraphContractError(f"evidence sem 'path' nem 'cache_key' em {where}")
    label_of(edge.src)
    label_of(edge.dst)
