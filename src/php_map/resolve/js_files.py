"""Tela → JS e JS → tela: nós `File` com `kind = "js"` e `REFERENCES`.

Só vira nó o `.js` que alguma tela (ou outro PHP) carrega: o `<script src="…js">` e as strings
`*.js` que o extrator lê (`string_refs`, schema_version 10) e o `db_app::load("a.js, b.js")`,
resolvido na mesma ordem de busca do e-cidade (`libs/db_app.utils.php:34-80`). Um JS não puxa
outro JS. Do JS alcançado (fora as bibliotecas de `Settings.js_library_globs`) lê-se o texto
(`ingest/js_scan.py`) e cada literal `*.php` vira `REFERENCES` JS → tela (`STRING_REF`, 0.9),
a lista abre o formulário.
"""

from collections.abc import Collection, Iterable
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
from pathlib import Path

from php_map.config import Settings
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import (
    STRING_REF_CONFIDENCE,
    Edge,
    EdgeSource,
    EdgeType,
    Node,
    NodeLabel,
    file_id,
)
from php_map.ingest.discovery import list_php_files
from php_map.ingest.js_scan import scan_js
from php_map.ingest.records import JsRecord, PhpFileRecord, StringRef
from php_map.resolve.includes import resolve_file

JS_KIND = "js"
DB_APP_VIA = "db_app::load"
# `db_app::load`: mapa fixo e, depois, a ordem de busca (`file_exists`) do e-cidade.
DB_APP_FIXED = {
    "scripts.js": "scripts/scripts.js",
    "strings.js": "scripts/strings.js",
    "datagrid.widget.js": "scripts/datagrid.widget.js",
    "prototype.js": "scripts/prototype.js",
}
DB_APP_DIRS = ("scripts/", "scripts/widgets/", "scripts/classes/", "ext/javascript/")


@dataclass(slots=True)
class JsLinkStats:
    available: int = 0  # `.js` em disco (fora `vendor/`, `node_modules/` e afins)
    js_files: int = 0  # nós `File` com `kind = "js"` (os alcançados)
    library: int = 0  # dos alcançados, os de biblioteca (não lidos, `is_infra`)
    read: int = 0  # JS lidos pelo leitor léxico
    read_errors: int = 0
    encoding_fallback: int = 0  # lidos como ISO-8859-1
    screens: int = 0  # arquivos PHP que carregam algum JS
    screen_refs: int = 0  # `REFERENCES` PHP → JS (por `<script src>`/string)
    db_app_load: int = 0  # `REFERENCES` PHP → JS por `db_app::load`
    db_app_missing: int = 0  # nome do `db_app::load` sem arquivo em nenhuma das pastas
    missing: int = 0  # `*.js` citado que não existe em disco (descartado)
    js_refs: int = 0  # `REFERENCES` JS → PHP
    by_dir: dict[str, int] = field(default_factory=dict)  # 1ª pasta dos alcançados


def list_js_files(source: Path | None, settings: Settings) -> frozenset[str]:
    """`.js` da pasta (caminhos relativos com `/`), com as mesmas exclusões da descoberta PHP."""
    if source is None or not source.is_dir():
        return frozenset()
    return frozenset(list_php_files(source, replace(settings, php_extensions=frozenset({".js"}))))


def is_library(path: str, settings: Settings) -> bool:
    return any(fnmatchcase(path, pattern) for pattern in settings.js_library_globs)


def db_app_names(arg0: str) -> list[str]:
    """Nomes `.js` do argumento de `db_app::load("a.js, b.js")` (os `.css` ficam de fora)."""
    return [name.strip() for name in arg0.split(",") if name.strip().lower().endswith(".js")]


def resolve_db_app(name: str, js_files: Collection[str]) -> str | None:
    """Arquivo que o `db_app::load` carrega para `name`, na ordem do e-cidade."""
    fixed = DB_APP_FIXED.get(name)
    if fixed is not None:
        return fixed if fixed in js_files else None
    for directory in DB_APP_DIRS:
        candidate = f"{directory}{name}"
        if candidate in js_files:
            return candidate
    return None


class JsLinker:
    """Liga cada PHP aos JS que ele carrega e guarda os JS alcançados (para a leitura)."""

    def __init__(self, js_files: frozenset[str], builder: GraphBuilder, settings: Settings):
        self.js_files = js_files
        self.builder = builder
        self.settings = settings
        self.stats = JsLinkStats(available=len(js_files))
        self.reached: dict[str, bool] = {}  # caminho → é biblioteca

    def link(self, record: PhpFileRecord) -> None:
        path = record.path
        before = self.stats.screen_refs + self.stats.db_app_load
        for ref in record.string_refs:
            if not ref.target.lower().endswith(".js"):
                continue
            target = resolve_file(ref.target, path, self.js_files)
            if target is None:
                self.stats.missing += 1
                continue
            self._edge(path, target, {"path": path, "line": ref.line, "origin": ref.origin})
            self.stats.screen_refs += 1
        for call in record.calls:
            if not _is_db_app_load(call.kind, call.class_, call.name) or not call.arg0:
                continue
            for name in db_app_names(call.arg0):
                target = resolve_db_app(name, self.js_files)
                if target is None:
                    self.stats.db_app_missing += 1
                    continue
                evidence = {"path": path, "line": call.line, "origin": DB_APP_VIA}
                self._edge(path, target, {**evidence, "via": DB_APP_VIA})
                self.stats.db_app_load += 1
        if self.stats.screen_refs + self.stats.db_app_load > before:
            self.stats.screens += 1

    def _edge(self, src: str, dst: str, evidence: dict[str, object]) -> None:
        if dst not in self.reached:
            library = is_library(dst, self.settings)
            self.reached[dst] = library
            self.builder.add_node(
                Node(
                    file_id(dst),
                    NodeLabel.FILE,
                    {
                        "path": dst,
                        "name": dst.rsplit("/", 1)[-1],
                        "kind": JS_KIND,
                        "module_hint": None,
                        "is_infra": library,
                    },
                )
            )
            self.stats.js_files += 1
            self.stats.library += library
            top = dst.split("/", 1)[0] if "/" in dst else "."
            self.stats.by_dir[top] = self.stats.by_dir.get(top, 0) + 1
        self.builder.add_edge(
            Edge(
                file_id(src),
                file_id(dst),
                EdgeType.REFERENCES,
                EdgeSource.STRING_REF,
                STRING_REF_CONFIDENCE,
                evidence,
            )
        )

    def read(self, source: Path | None) -> list[JsRecord]:
        """Lê os JS alcançados que não são biblioteca (ordenados, para a saída ser estável)."""
        records: list[JsRecord] = []
        if source is None:
            return records
        for path in sorted(p for p, library in self.reached.items() if not library):
            try:
                raw = (source / path).read_bytes()
            except OSError:
                self.stats.read_errors += 1
                continue
            record = scan_js(path, raw)
            self.stats.read += 1
            self.stats.encoding_fallback += record.encoding_fallback
            records.append(record)
        return records

    def link_js_refs(self, js_records: Iterable[JsRecord], php_files: Collection[str]) -> None:
        """Literal `*.php` do JS → `REFERENCES` JS → PHP (relativo à raiz, depois à pasta)."""
        for record in js_records:
            for ref in record.string_refs:
                target = _php_target(ref, record.path, php_files)
                if target is None:
                    continue
                self.builder.add_edge(
                    Edge(
                        file_id(record.path),
                        file_id(target),
                        EdgeType.REFERENCES,
                        EdgeSource.STRING_REF,
                        STRING_REF_CONFIDENCE,
                        {"path": record.path, "line": ref.line, "origin": ref.origin},
                    )
                )
                self.stats.js_refs += 1


def _php_target(ref: StringRef, path: str, php_files: Collection[str]) -> str | None:
    target = resolve_file(ref.target, path, php_files)
    return None if target is None or target == path else target


def _is_db_app_load(kind: str, cls: str | None, name: str) -> bool:
    return (
        kind == "static" and (cls or "").lstrip("\\").lower() == "db_app" and name.lower() == "load"
    )
