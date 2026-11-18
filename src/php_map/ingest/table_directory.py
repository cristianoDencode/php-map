"""Diretório de tabelas e resolução do nome sem schema pelo `search_path`.

O SQL do e-cidade não qualifica as tabelas (`from empempenho`): quem resolve é o `search_path`
da base, e a tabela mora no schema do módulo (`empenho.empempenho`). O diretório guarda as
tabelas conhecidas (do DDL dos arquivos ou do `pg_catalog`) e responde, como o Postgres, o
schema de um nome.
"""

from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Literal

Rule = Literal["explicit", "search_path", "unique", "not_found"]
RULES: tuple[Rule, ...] = ("explicit", "search_path", "unique", "not_found")
DEFAULT_SCHEMA = "public"
# Catálogo do sistema: buscado antes do `search_path` quando não aparece nele (Postgres).
SYSTEM_SCHEMA = "pg_catalog"
SYSTEM_PREFIX = "pg_"


@dataclass(frozen=True, slots=True)
class DbTable:
    """Tabela conhecida: `kind` é `table` (r/p), `view` (v/m) ou `foreign` (f)."""

    schema: str
    name: str
    kind: str = "table"
    columns: tuple[str, ...] = ()


def parse_search_path(value: str | Iterable[str]) -> tuple[str, ...]:
    """`current_setting('search_path')` (ou uma lista) → schemas em minúsculas, na ordem.

    Descarta `"$user"` (não é schema), aspas, espaços e repetições (uma instalação real pode repetir
    `recursoshumanos`).
    """
    items = value.split(",") if isinstance(value, str) else value
    schemas: list[str] = []
    for item in items:
        schema = item.strip().strip('"').strip().lower()
        if schema and schema != "$user" and schema not in schemas:
            schemas.append(schema)
    return tuple(schemas)


class TableDirectory:
    """Tabelas por nome e por schema; `resolve` conta as regras aplicadas em `counts`."""

    def __init__(self, search_path: Sequence[str] = ()) -> None:
        self.search_path = parse_search_path(search_path)
        self._tables: dict[str, dict[str, DbTable]] = {}  # nome → schema → tabela
        self.counts: Counter[str] = Counter({rule: 0 for rule in RULES})
        # Nomes das funções do `pg_catalog` (só com `--schema-db`): chamada sem schema a um deles
        # é nativa, mesmo que o e-cidade tenha sobrecargas no `public` (`substr`, `lpad`).
        self.builtin_functions: frozenset[str] = frozenset()

    def is_builtin(self, function: str) -> bool:
        """O `pg_catalog` vem antes no `search_path`: com argumento texto, a nativa vence."""
        return function.lower() in self.builtin_functions

    def __len__(self) -> int:
        return sum(len(by_schema) for by_schema in self._tables.values())

    def add(self, schema: str, name: str, kind: str = "table", columns: Sequence[str] = ()) -> None:
        schema, name = schema.lower(), name.lower()
        self._tables.setdefault(name, {})[schema] = DbTable(schema, name, kind, tuple(columns))

    def discard(self, schema: str, name: str) -> None:
        by_schema = self._tables.get(name.lower())
        if by_schema is not None:
            by_schema.pop(schema.lower(), None)
            if not by_schema:
                del self._tables[name.lower()]

    def has(self, name: str, schema: str | None = None) -> bool:
        """Algum schema tem a tabela (ou o `schema` informado, se houver)."""
        by_schema = self._tables.get(name.lower(), {})
        return bool(by_schema) if schema is None else schema.lower() in by_schema

    def info(self, schema: str, name: str) -> DbTable | None:
        return self._tables.get(name.lower(), {}).get(schema.lower())

    def resolve(self, name: str, schema: str | None = None) -> tuple[str, Rule]:
        """`(schema, regra)` do nome, como o Postgres resolveria.

        Qualificado → como está (`explicit`). Senão, o 1º schema do `search_path` que tem a
        tabela (`search_path`); sem `search_path`, o schema único do nome (`unique`); se nada
        servir, `public`, (`not_found`, vira placeholder).
        O `pg_catalog` vem implicitamente antes do `search_path`, como no Postgres: `pg_class`,
        `pg_attribute`… (fora do diretório, que só tem os schemas do e-cidade).
        """
        rule: Rule
        if schema is not None:
            found, rule = schema.lower(), "explicit"
        elif name.lower().startswith(SYSTEM_PREFIX) and SYSTEM_SCHEMA not in self.search_path:
            found, rule = SYSTEM_SCHEMA, "search_path"
        else:
            by_schema = self._tables.get(name.lower(), {})
            match = next((s for s in self.search_path if s in by_schema), None)
            if match is not None:
                found, rule = match, "search_path"
            elif not self.search_path and len(by_schema) == 1:
                found, rule = next(iter(by_schema)), "unique"
            else:
                found, rule = DEFAULT_SCHEMA, "not_found"
        self.counts[rule] += 1
        return found, rule
