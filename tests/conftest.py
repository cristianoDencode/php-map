"""Fixtures comuns: catálogo lido do banco sem banco nenhum.

- Nenhum teste lê o `PHP_MAP_CATALOG_DSN` real (variável ou `.env`): o `scan` da CLI o
  usaria sozinho. Só os testes `remote_db` recebem o DSN real, pela fixture `remote_dsn`.
- `PILOT_ROWS` são as linhas que o banco devolveria para o catálogo de
  `fixtures/catalog/catalogo_piloto.sql` (estado final, depois dos UPDATE/DELETE), com os
  espaços de `char(n)` e os tipos do banco.
"""

import os
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest

from php_map import config
from php_map.ingest import catalog_db

# Lido antes de qualquer teste mexer no ambiente.
REMOTE_DSN = config.env_value(config.CATALOG_DSN_ENV)


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    skip = pytest.mark.skip(reason=f"sem {config.CATALOG_DSN_ENV} (variável ou .env)")
    for item in items:
        if "remote_db" in item.keywords and not REMOTE_DSN:
            item.add_marker(skip)


@pytest.fixture(autouse=True)
def _no_real_catalog_dsn(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: Any) -> None:
    monkeypatch.delenv(config.CATALOG_DSN_ENV, raising=False)
    missing = tmp_path_factory.getbasetemp() / "sem-env" / ".env"
    monkeypatch.setattr(config, "DOTENV_PATHS", (missing,))


@pytest.fixture(autouse=True)
def _isolated_access_vault(monkeypatch: pytest.MonkeyPatch, tmp_path_factory: Any) -> None:
    """Nenhum teste toca o cofre de acesso real: cada um usa um arquivo temporário."""
    monkeypatch.setenv("PHP_MAP_ACCESS_DB", str(tmp_path_factory.mktemp("cofre") / "access.db"))


@pytest.fixture(autouse=True)
def _restore_umask() -> Iterator[None]:
    """A CLI define o umask do processo (`os.umask` é global): nenhum teste vaza isso."""
    before = os.umask(0o022)
    os.umask(before)
    yield
    os.umask(before)


@pytest.fixture
def remote_dsn() -> str:
    assert REMOTE_DSN  # os testes remote_db são pulados sem o DSN
    return REMOTE_DSN


PILOT_ROWS: dict[str, list[dict[str, Any]]] = {
    "db_sysmodulo": [
        {"codmod": 38, "nomemod": "empenho        ", "descricao": "Módulo de empenho"},
        {"codmod": 81, "nomemod": "esocial        ", "descricao": "Módulo para o e-Social"},
    ],
    "db_sysarquivo": [
        {
            "codarq": 889,
            "nomearq": "empempenho          ",
            "descricao": "Empenhos",
            "sigla": "e60 ",
            "rotulo": "Empenhos",
        },
        {
            "codarq": 890,
            "nomearq": "empanulado          ",
            "descricao": "Anulações de empenho",
            "sigla": "e94 ",
            "rotulo": "Anulações",
        },
        {
            "codarq": 3807,
            "nomearq": "empenhocotamensal   ",
            "descricao": "Cotas Mensais dos empenhos",
            "sigla": "e05 ",
            "rotulo": "Cotas Mensais dos Empenhos",
        },
    ],
    "db_sysarqmod": [
        {"codmod": 38, "codarq": 889},
        {"codmod": 38, "codarq": 890},
        {"codmod": 38, "codarq": 3807},
    ],
    "db_syscampo": [
        {
            "codcam": 5001,
            "nomecam": "e60_numemp      ",
            "descricao": "Número do empenho",
            "rotulo": "Número",
        },
        {
            "codcam": 5002,
            "nomecam": "e60_codemp      ",
            "descricao": "Código do empenho no ano",
            "rotulo": "Empenho",
        },
        {
            "codcam": 5003,
            "nomecam": "e60_vlranu      ",
            "descricao": "Valor anulado do empenho",
            "rotulo": "Valor Anulado",
        },
        {
            "codcam": 5004,
            "nomecam": "e94_codanu      ",
            "descricao": "Código da anulação",
            "rotulo": "Anulação",
        },
    ],
    "db_sysarqcamp": [
        {"codarq": 889, "codcam": 5001, "seqarq": 1, "codsequencia": 0},
        {"codarq": 889, "codcam": 5002, "seqarq": 2, "codsequencia": 0},
        {"codarq": 889, "codcam": 5003, "seqarq": 3, "codsequencia": 0},
        {"codarq": 890, "codcam": 5004, "seqarq": 1, "codsequencia": 0},
    ],
    "db_modulos": [
        {"id_item": 398, "nome_modulo": "Empenho", "descr_modulo": "Empenho", "temexerc": True},
        {"id_item": 28, "nome_modulo": "Compras", "descr_modulo": "Compras", "temexerc": True},
        {"id_item": 10216, "nome_modulo": "e-Social", "descr_modulo": "e-Social"},
    ],
    "db_itensmenu": [
        {
            "id_item": 31,
            "descricao": "Consultas",
            "help": "Consultas",
            "funcao": "",
            "itemativo": "1",
            "desctec": "Consultas",
        },
        {
            "id_item": 32,
            "descricao": "Procedimentos",
            "help": "Procedimentos",
            "funcao": "",
            "itemativo": "1",
            "desctec": "Procedimentos",
        },
        {
            "id_item": 398,
            "descricao": "Empenho",
            "help": "Módulo de empenho",
            "funcao": "",
            "itemativo": "2",
            "desctec": "Módulo de empenho",
        },
        {
            "id_item": 28,
            "descricao": "Compras",
            "help": "Módulo de compras",
            "funcao": "",
            "itemativo": "2",
            "desctec": "Módulo de compras",
        },
        {
            "id_item": 9001,
            "descricao": "Anulação de Empenho",
            "help": "Anulação de empenho",
            "funcao": "emp4_anularempenho001.php",
            "itemativo": "1",
            "desctec": "Anula o saldo de um empenho",
        },
        {
            "id_item": 9002,
            "descricao": "Liquidação (antiga)",
            "help": "Liquidação",
            "funcao": "emp4_liquidacao004.php",
            "itemativo": "f",
            "desctec": "Tela antiga de liquidação",
        },
        {
            "id_item": 9003,
            "descricao": "Empenhos",
            "help": "Consulta de empenhos",
            "funcao": " func_empempenho.php?funcao_js=parent.js_mostra ",
            "itemativo": "1",
            "desctec": "Consulta de empenhos",
        },
        {
            "id_item": 9004,
            "descricao": "Cotas mensais",
            "help": "Cotas mensais",
            "funcao": "emp1_empenhocotamensal001.php",
            "itemativo": "1",
            "desctec": "Cotas mensais",
        },
    ],
    "db_menu": [
        {"id_item": 398, "id_item_filho": 31, "menusequencia": 2, "modulo": 398},
        {"id_item": 398, "id_item_filho": 32, "menusequencia": 4, "modulo": 398},
        {"id_item": 28, "id_item_filho": 31, "menusequencia": 2, "modulo": 28},
        {"id_item": 32, "id_item_filho": 9001, "menusequencia": 1, "modulo": 398},
        {"id_item": 32, "id_item_filho": 9002, "menusequencia": 2, "modulo": 398},
        {"id_item": 31, "id_item_filho": 9003, "menusequencia": 1, "modulo": 398},
        {"id_item": 31, "id_item_filho": 9003, "menusequencia": 5, "modulo": 28},
        {"id_item": 32, "id_item_filho": 9004, "menusequencia": 3, "modulo": 398},
    ],
}


@pytest.fixture
def pilot_rows() -> dict[str, list[dict[str, Any]]]:
    """Cópia de `PILOT_ROWS`: o teste pode alterar à vontade."""
    return {table: [dict(row) for row in rows] for table, rows in PILOT_ROWS.items()}


@pytest.fixture
def pilot_fetch(
    pilot_rows: dict[str, list[dict[str, Any]]],
) -> Callable[[str], list[dict[str, Any]] | None]:
    """`FetchFn` sobre as linhas do piloto."""
    return lambda table: pilot_rows[table]


class _Cursor:
    def __init__(self, rows: list[dict[str, Any]]) -> None:
        self.rows = rows

    def fetchone(self) -> dict[str, Any] | None:
        return self.rows[0] if self.rows else None

    def fetchall(self) -> list[dict[str, Any]]:
        return self.rows


class FakeCatalogConnection:
    """Conexão `psycopg` falsa: responde às consultas de `catalog_db._DbFetch`.

    `tables` = `{(schema, tabela): linhas}`; `search_path` = schemas que o `to_regclass`
    enxerga. As colunas existentes são as chaves da primeira linha (ou `columns`).
    """

    def __init__(
        self,
        tables: Mapping[tuple[str, str], list[dict[str, Any]]],
        search_path: tuple[str, ...] = ("configuracoes",),
        columns: Mapping[str, set[str]] | None = None,
        read_only: str = "on",
    ) -> None:
        self.tables = dict(tables)
        self.search_path = search_path
        self.columns = dict(columns or {})
        self.read_only_setting = read_only
        self.oids = {key: 1000 + i for i, key in enumerate(sorted(self.tables))}
        self.queries: list[str] = []
        self.selects: list[str] = []  # só os SELECT nas tabelas do catálogo
        self.isolation_level: Any = None
        self.read_only = False
        self.closed = False

    def __enter__(self) -> "FakeCatalogConnection":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.closed = True

    def execute(self, query: Any, params: tuple[Any, ...] = ()) -> _Cursor:
        text = query if isinstance(query, str) else query.as_string()
        self.queries.append(text)
        if text == catalog_db._READ_ONLY:
            return _Cursor([{"read_only": self.read_only_setting}])
        if text == catalog_db._RELATION_BY_PATH:
            found = [s for s in self.search_path if (s, params[0]) in self.tables]
            return _Cursor([self._relation(schema, params[0]) for schema in found[:1]])
        if text == catalog_db._RELATION_BY_NAME:
            found = sorted(
                (schema != params[1], schema) for schema, table in self.tables if table == params[0]
            )
            return _Cursor([self._relation(schema, params[0]) for _flag, schema in found[:1]])
        if text == catalog_db._COLUMNS:
            key = next(key for key, oid in self.oids.items() if oid == params[0])
            names = self.columns.get(key[1]) or set().union(*(r.keys() for r in self.tables[key]))
            return _Cursor([{"attname": name} for name in sorted(names)])
        for (schema, table), rows in self.tables.items():
            relation = f'from "{schema}"."{table}"'
            if relation not in text:
                continue
            self.selects.append(text)
            selected = text.removeprefix("select ").split(" from ")[0]
            names = [name.strip('"') for name in selected.split(", ")]
            found = [{name: row.get(name) for name in names} for row in rows]
            if text.endswith(relation):
                return _Cursor(found)
            # Página: `… [where (k…) > (%s…)] order by k… limit %s` (catalog_db._pages).
            order = text.split(" order by ")[1].split(" limit ")[0]
            key = [name.strip('"') for name in order.split(", ")]
            ordered = sorted(rows, key=lambda row: tuple(row[k] for k in key))
            if " where " in text:
                last = tuple(params[:-1])
                ordered = [row for row in ordered if tuple(row[k] for k in key) > last]
            page = ordered[: params[-1]]
            return _Cursor([{name: row.get(name) for name in names} for row in page])
        raise AssertionError(f"consulta inesperada: {text}")

    def _relation(self, schema: str, table: str) -> dict[str, Any]:
        return {"schema": schema, "name": table, "oid": self.oids[(schema, table)]}


class FakeConnect:
    """Troca `psycopg.connect` e guarda o DSN e as opções recebidas."""

    def __init__(self, conn: FakeCatalogConnection) -> None:
        self.conn = conn
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def __call__(self, dsn: str, **kwargs: Any) -> FakeCatalogConnection:
        self.calls.append((dsn, kwargs))
        return self.conn


@pytest.fixture
def catalog_connection() -> type[FakeCatalogConnection]:
    """A classe da conexão falsa, para montar cenários (schema, colunas, somente leitura)."""
    return FakeCatalogConnection


@pytest.fixture
def fake_catalog_db(monkeypatch: pytest.MonkeyPatch) -> Iterator[FakeConnect]:
    """`psycopg.connect` falso com o catálogo do piloto em `configuracoes`."""
    conn = FakeCatalogConnection(
        {("configuracoes", table): rows for table, rows in PILOT_ROWS.items()}
    )
    fake = FakeConnect(conn)
    monkeypatch.setattr(catalog_db.psycopg, "connect", fake)
    yield fake


# --------------------------------------------------------------------------- banco das fixtures

FIXTURES = Path(__file__).resolve().parent / "fixtures"


@pytest.fixture(scope="session")
def fixture_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Banco das fixtures como o `scan` grava: grafo (schema + catálogo), busca e agregados.

    A API e a BFS em SQL são testadas sobre ele. `boilerplate_min_files=3`, como no
    `measure_precision.build_search_db` (com ~35 arquivos, o padrão de 50 nunca dispara).
    """
    from php_map.analysis.aggregate import build_analytics, write_analytics
    from php_map.config import Settings
    from php_map.graph import store
    from php_map.ingest.php_extractor import read_cache
    from php_map.pipeline import build_graph
    from php_map.resolve.unresolved import UnresolvedWriter
    from php_map.search.index import build_documents, write_index

    settings = Settings(boilerplate_min_files=3)
    reference = FIXTURES / "ast/ecidade-mini.jsonl"

    def records() -> Any:
        return read_cache(reference)

    writer = UnresolvedWriter()
    result = build_graph(
        records,
        [FIXTURES / "schema/ddl_piloto.sql"],
        writer,
        settings,
        catalog=[FIXTURES / "catalog/catalogo_piloto.sql"],
    )
    writer.close()
    db = tmp_path_factory.mktemp("grafo") / "graph.db"
    source = str((FIXTURES / "ecidade-mini").resolve())
    graph = result.builder.stats()
    summary = {
        "nodes": graph.nodes_by_label,
        "edges": {"by_type": graph.edges_by_type, "by_source": graph.edges_by_source},
    }
    meta = {"source": source, "scanned_at": "2026-09-25T10:00:00-03:00", "summary": summary}
    store.save(result.builder, db, meta)
    docs, stats = build_documents(records, result.builder, settings)
    write_index(db, docs, stats)
    write_analytics(db, build_analytics(result.builder, settings))
    return db


def _build_db(
    tmp: Path, source: Path, reference: Path, schema: Path, catalog: Path, min_files: int = 3
) -> Path:
    from php_map.analysis.aggregate import build_analytics, write_analytics
    from php_map.config import Settings
    from php_map.graph import store
    from php_map.ingest.php_extractor import read_cache
    from php_map.pipeline import build_graph
    from php_map.resolve.unresolved import UnresolvedWriter
    from php_map.search.index import build_documents, write_index

    settings = Settings(boilerplate_min_files=min_files)

    def records() -> Any:
        return read_cache(reference)

    writer = UnresolvedWriter()
    result = build_graph(records, [schema], writer, settings, catalog=[catalog])
    writer.close()
    db = tmp / "graph.db"
    graph = result.builder.stats()
    summary = {
        "nodes": graph.nodes_by_label,
        "edges": {"by_type": graph.edges_by_type, "by_source": graph.edges_by_source},
    }
    meta = {
        "source": str(source.resolve()),
        "scanned_at": "2026-09-25T10:00:00-03:00",
        "summary": summary,
    }
    store.save(result.builder, db, meta)
    docs, stats = build_documents(records, result.builder, settings)
    write_index(db, docs, stats)
    write_analytics(db, build_analytics(result.builder, settings))
    return db


@pytest.fixture(scope="session")
def shop_db(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """Banco da loja fictícia (`loja-mini`), como o `scan` grava: grafo (schema + catálogo),
    busca e agregados. Substitui o recorte real nos testes públicos."""
    return _build_db(
        tmp_path_factory.mktemp("grafo-loja"),
        FIXTURES / "loja-mini",
        FIXTURES / "ast/loja-mini.jsonl",
        FIXTURES / "schema/loja_mini.sql",
        FIXTURES / "catalog/loja_mini.sql",
    )
