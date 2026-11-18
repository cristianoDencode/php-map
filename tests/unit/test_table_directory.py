"""Diretório de tabelas e resolução pelo `search_path`.

Os casos imitam uma instalação padrão do e-Cidade (só nomes de schema e tabela, escritos à mão).
"""

import pytest

from php_map.analysis.impact import table_target
from php_map.config import ECIDADE_SEARCH_PATH
from php_map.errors import QueryError
from php_map.ingest.table_directory import TableDirectory, parse_search_path

SESSION = (
    '"$user", public, matriculaonline, recursoshumanos, empenho, divida, pessoal, '
    "recursoshumanos, protocolo, contabilidade"
)


def _directory(search_path: str | tuple[str, ...] = SESSION) -> TableDirectory:
    directory = TableDirectory(parse_search_path(search_path))
    for schema, name in [
        ("empenho", "empempenho"),
        ("protocolo", "cgm"),
        ("contabilidade", "conlancamval"),
        ("pessoal", "eventofinanceiroautomatico"),
        ("public", "eventofinanceiroautomatico"),
        ("divida", "remessacra"),
        ("plugins", "remessacra"),
        ("plugins", "sopluginsx"),
    ]:
        directory.add(schema, name)
    return directory


def test_parse_search_path_drops_user_quotes_and_repeats() -> None:
    assert parse_search_path(SESSION) == (
        "public",
        "matriculaonline",
        "recursoshumanos",
        "empenho",
        "divida",
        "pessoal",
        "protocolo",
        "contabilidade",
    )


@pytest.mark.parametrize(
    ("name", "schema", "expected"),
    [
        ("empempenho", None, ("empenho", "search_path")),
        ("CGM", None, ("protocolo", "search_path")),
        ("conlancamval", None, ("contabilidade", "search_path")),
        # nos dois schemas: `public` vem antes no search_path
        ("eventofinanceiroautomatico", None, ("public", "search_path")),
        # `plugins` fora do search_path: vale `divida`
        ("remessacra", None, ("divida", "search_path")),
        # qualificada: como está, mesmo sem a tabela no diretório
        ("orctiporec", "orcamento", ("orcamento", "explicit")),
        # tabela de trabalho que não existe no banco: `public`, como antes
        ("w_qualquer", None, ("public", "not_found")),
        # só num schema fora do search_path: o Postgres não acharia
        ("sopluginsx", None, ("public", "not_found")),
        # catálogo do sistema: `pg_catalog` implícito antes do search_path
        ("pg_class", None, ("pg_catalog", "search_path")),
        ("pg_attribute", "pg_catalog", ("pg_catalog", "explicit")),
    ],
)
def test_resolve_by_search_path(name: str, schema: str | None, expected: tuple[str, str]) -> None:
    assert _directory().resolve(name, schema) == expected


def test_unique_only_without_search_path() -> None:
    directory = _directory(())
    assert directory.resolve("empempenho") == ("empenho", "unique")
    assert directory.resolve("remessacra") == ("public", "not_found")  # dois schemas
    assert directory.counts["unique"] == 1 and directory.counts["not_found"] == 1


def test_counts_has_and_discard() -> None:
    directory = _directory()
    directory.resolve("empempenho")
    directory.resolve("empempenho", "empenho")
    assert directory.counts["search_path"] == 1 and directory.counts["explicit"] == 1
    assert directory.has("cgm") and directory.has("cgm", "protocolo")
    assert not directory.has("cgm", "public")
    directory.discard("protocolo", "cgm")
    assert not directory.has("cgm") and len(directory) == 7


def test_ecidade_search_path_is_the_database_one() -> None:
    assert ECIDADE_SEARCH_PATH[:8] == (
        "public", "matriculaonline", "recursoshumanos", "veiculosoff", "dbicms", "site",
        "custos", "empenho",
    )  # fmt: skip
    assert len(ECIDADE_SEARCH_PATH) == len(set(ECIDADE_SEARCH_PATH)) == 67
    assert "$user" not in ECIDADE_SEARCH_PATH


def test_impact_target_without_schema() -> None:
    tables = ["Table:empenho.empempenho", "Table:public.x", "Table:pessoal.x"]
    assert table_target(tables, "empempenho", None) == "Table:empenho.empempenho"
    assert table_target(tables, "x", "pessoal") == "Table:pessoal.x"
    assert table_target(tables, "nada", None) == "Table:public.nada"
    ambiguous = r"\(pessoal\.x, public\.x\): informe o schema \(ex\.: pessoal\.x\)"
    with pytest.raises(QueryError, match=ambiguous):
        table_target(tables, "x", None)
