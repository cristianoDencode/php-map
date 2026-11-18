"""Registro JSONL do extrator PHP (schema 1 a 12)."""

import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

from php_map.errors import IngestError

SCHEMA_VERSION = 12
# As versões 1 (sem origens), 2 (sem textos), 3 (sem encoding_fallback), 4 (sem docblocks), 5
# (sem visibilidade nem assinatura dos métodos) e 6 (sem ações de despacho, rotas nem chamadas de
# saída), 7 e 8 (sem injeção de dependência nem bindings) e 9 (sem `*.js` nos `string_refs` nem
# URLs do `<script>` inline), 10 (sem `class_props`) e 11 (sem `sets` nem `form_fields`)
# continuam aceitas: caches antigos e fixtures.
SUPPORTED_SCHEMA_VERSIONS = frozenset({1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12})


class _Model(BaseModel):
    # extra="forbid": mudança de contrato no extrator precisa aparecer como erro, não sumir.
    model_config = ConfigDict(extra="forbid", frozen=True, populate_by_name=True)


class Origin(_Model):
    """De onde veio o valor de uma variável ou propriedade (schema_version 2).

    schema_version 9: `param` é um parâmetro tipado com classe (`class`,
    `name` = o parâmetro, `method` = o método que o declara, `None` em função); `container` é
    `app(X::class)`, `resolve(X::class)`, `App::make(X::class)` ou `app()->make(X::class)`.
    """

    kind: Literal["new", "static_call", "method_call", "param", "container"]
    class_: str | None = Field(default=None, alias="class")
    name: str | None = None
    method: str | None = None
    receiver: str | None = None
    receiver_class: str | None = None
    arg0: str | None = None
    arg1: bool | None = None
    argc: int = 0


class PropertyType(_Model):
    prop: str
    origin: Origin


class PropertyDoc(_Model):
    """Propriedade de classe com docblock (schema_version 5): anotação do Doctrine."""

    name: str
    line: int
    doc_comment: str


class MethodParam(_Model):
    """Parâmetro de método público (schema_version 6): a assinatura de um endpoint."""

    name: str
    has_default: bool
    type: str | None


class Declare(_Model):
    kind: Literal["function", "class", "interface", "trait", "method"]
    name: str
    class_: str | None = Field(alias="class")
    line_start: int
    line_end: int
    extends: str | None
    implements: list[str]
    property_types: list[PropertyType] = []
    # schema_version 9, só em `class`: `abstract class` (a injeção vira as filhas).
    abstract: bool = False
    # schema_version 5, só em class/interface/trait: docblock bruto (até 8 KB) e as
    # propriedades que têm docblock. `None`/vazio com cache de versão anterior.
    doc_comment: str | None = None
    properties: list[PropertyDoc] = []
    # schema_version 11, só em class/interface/trait: propriedades de instância cujo
    # valor padrão é string literal (`$table`, `$connection`), com ou sem docblock. PHP devolve
    # `[]` (não `{}`) quando não há nenhuma: o validador abaixo normaliza.
    class_props: dict[str, str] = {}
    # schema_version 6, só em `method`: `visibility` (public/protected/private),
    # `display_name` (a caixa original; `name` é minúsculo) e `params` (só dos públicos).
    visibility: Literal["public", "protected", "private"] | None = None
    display_name: str | None = None
    params: list[MethodParam] = []

    @field_validator("class_props", mode="before")
    @classmethod
    def _empty_php_array(cls, value: object) -> object:
        return {} if value == [] else value


class Include(_Model):
    type: Literal["require_once", "require", "include", "include_once"]
    target: str | None
    expr_kind: Literal["literal", "modification", "interpolated", "concat", "dynamic"]
    hint: str | None
    line: int
    scope: str
    # v1: nome da classe; v2: origem da variável.
    var_types: dict[str, str | Origin]


class FieldSet(_Model):
    """`$obj->campo = expr` feito antes de `incluir`/`alterar` (schema_version 12).
    `value` é o texto do código (até 120 caracteres, `…` no corte), nunca o valor resolvido."""

    field: str
    value: str
    kind: Literal["literal", "null", "variavel", "parametro", "chamada", "expressao"]
    line: int


class FormField(_Model):
    """Campo do formulário da tela (schema_version 12): `db_input('campo', …)` e afins
    (`widget` é a função) ou `<input|select|textarea name="campo">` (`widget` = `html:input`…)."""

    name: str
    widget: str
    line: int


class Call(_Model):
    kind: Literal["func", "method", "static", "new"]
    name: str
    class_: str | None = Field(alias="class")
    receiver_var: str | None
    receiver_class: str | None
    line: int
    scope: str
    # schema_version 2, só em static/method:
    arg0: str | None = None
    arg1: bool | None = None
    argc: int = 0
    receiver_origin: Origin | None = None
    receiver_property: str | None = None
    # schema_version 12, só em `incluir`/`alterar`: o que o código atribuiu ao objeto.
    sets: list[FieldSet] = []


class DynamicUse(_Model):
    kind: Literal["variable_function", "call_user_func", "variable_method", "variable_class"]
    snippet: str
    line: int
    scope: str


class SqlString(_Model):
    text: str
    complete: bool
    via: Literal["db_query", "pg_query", "pg_exec", "assignment", "other"]
    line: int
    scope: str


class StringRef(_Model):
    """Literal `*.php` (e, desde a schema_version 10, `*.js`) numa string ou no HTML.

    `js_string` é a origem dos literais `*.php` lidos de um arquivo JS (`ingest/js_scan.py`)."""

    target: str
    line: int
    origin: Literal["php_string", "inline_html", "js_string"]


class HttpLiteral(_Model):
    """Literal com cara de caminho de API (schema_version 10): `'/tributario/x/'`.

    O `${…}` de um template vira o curinga `{}`; o do começo (a base da URL) sai do texto."""

    text: str
    line: int


class DispatchAction(_Model):
    """Um `case` literal do `switch` de despacho (schema_version 7)."""

    name: str
    line: int  # a do `case`
    params: list[str]  # propriedades lidas do objeto de despacho no ramo (cascata divide o corpo)


class Dispatch(_Model):
    subject: str
    cases: list[str]
    line: int
    scope: str
    # schema_version 7: uma ação por `case` literal e o que `$oParam->x` lê **fora** do `switch`.
    actions: list[DispatchAction] = []
    outside_params: list[str] = []


class RouteCall(_Model):
    """Um elo da cadeia `Route::prefix('x')->middleware(…)->group(…)`: nome e argumentos
    simbólicos (dicionários `{t: …}`, ver `php-extractor/src/SymbolicRoutes.php`)."""

    name: str
    args: list[dict[str, Any]]


class RouteItem(_Model):
    """Instrução de arquivo de rotas (schema_version 7): `assign` ou `route`."""

    kind: Literal["assign", "route"]
    line: int
    var: str | None = None
    expr: dict[str, Any] | None = None
    chain: list[RouteCall] = []


class Outbound(_Model):
    """Chamada que sai do sistema (schema_version 7): `new SoapClient`, `curl_*`."""

    kind: Literal["soap", "http"]
    callee: str
    line: int
    scope: str
    url: dict[str, Any] | None  # destino simbólico; `None` se não aparece no ponto da chamada


class Binding(_Model):
    """`->bind|singleton|scoped(A::class, B::class)` literal (schema_version 9).
    `concrete` é `None` quando o 2º argumento não é `X::class` (closure, variável)."""

    abstract: str
    concrete: str | None
    line: int


class TextItem(_Model):
    """Texto escrito para pessoas (schema_version 3): matéria-prima da busca."""

    kind: Literal["comment", "html", "js_string", "literal"]
    text: str
    line: int
    scope: str


class PhpFileRecord(_Model):
    schema_version: int
    path: str
    status: Literal["ok", "partial", "parse_error", "read_error"]
    errors: list[str]
    sha1: str | None
    lines: int
    declares: list[Declare]
    includes: list[Include]
    calls: list[Call]
    dynamic: list[DynamicUse]
    sql_strings: list[SqlString]
    string_refs: list[StringRef]
    dispatch: list[Dispatch]
    # schema_version 3: até 400 itens / 32 KB por arquivo; `texts_truncated` se estourou.
    texts: list[TextItem] = []
    texts_truncated: bool = False
    # schema_version 4: o extrator tentou UTF-8 e o arquivo não decodificou, caindo
    # para latin-1 (`Extractor::extractFile`). Sempre `False` com cache de versão anterior.
    encoding_fallback: bool = False
    # schema_version 7: instruções de rotas do Laravel (só em arquivos que chamam a
    # fachada `Route`) e chamadas que saem do sistema.
    routes: list[RouteItem] = []
    outbound: list[Outbound] = []
    # schema_version 9: bindings literais do container do Laravel.
    bindings: list[Binding] = []
    # schema_version 10: URLs do `<script>` inline e se o arquivo cita a base da API
    # (`requestApi` ou `v4/api`, em qualquer ponto do código).
    http_literals: list[HttpLiteral] = []
    has_api_prefix: bool = False
    # schema_version 12: campos do formulário (o DAO os lê do `$_POST`).
    form_fields: list[FormField] = []


class JsRecord(_Model):
    """Um arquivo `.js` lido pelo leitor léxico (`ingest/js_scan.scan_js`); não vem
    do extrator PHP nem vai para o cache de AST."""

    path: str
    encoding_fallback: bool
    string_refs: list[StringRef]
    http_literals: list[HttpLiteral]
    has_api_prefix: bool


def parse_record(line: str) -> PhpFileRecord:
    """Valida uma linha do JSONL. Erros de contrato viram `IngestError`."""
    line = line.rstrip("\r\n")
    try:
        record = PhpFileRecord.model_validate_json(line)
    except ValidationError as exc:
        version = _peek_schema_version(line)
        if version not in SUPPORTED_SCHEMA_VERSIONS:
            raise IngestError(
                f"schema_version {version!r} não suportado (esperado {SCHEMA_VERSION})"
            ) from exc
        raise IngestError(f"registro do extrator inválido: {exc}") from exc
    if record.schema_version not in SUPPORTED_SCHEMA_VERSIONS:
        raise IngestError(
            f"schema_version {record.schema_version} não suportado (esperado {SCHEMA_VERSION})"
        )
    return record


def _peek_schema_version(line: str) -> object:
    try:
        data = json.loads(line)
    except json.JSONDecodeError:
        return None
    return data.get("schema_version") if isinstance(data, dict) else None
