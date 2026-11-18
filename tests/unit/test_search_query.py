"""Busca textual: termos, filtro de repetidos e consulta num índice pequeno."""

import sqlite3
from pathlib import Path
from typing import Any

import pytest
from conftest import PhpRecords

from php_map.config import Settings
from php_map.errors import StoreError
from php_map.graph import store
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import Node, NodeLabel, file_id
from php_map.ingest.records import PhpFileRecord
from php_map.search.index import (
    boilerplate_key,
    build_documents,
    path_name,
    shingles,
    split_symbol,
    write_index,
)
from php_map.search.query import normalize_terms, search

LICENSE = (
    "Sistema Exemplo Software Publico Copyright (C) {year} {company} Servicos "
    "de Informatica www.exemplo.com.br contato@exemplo.com.br Este programa e software "
    "livre; voce pode redistribui-lo e/ou modifica-lo sob os termos da Licenca Publica Geral "
    "GNU, conforme publicada pela Free Software Foundation"
)


# --------------------------------------------------------------------------- termos


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("anulação", ["anula"]),
        ("empenhos", ["empenho"]),
        ("anular", ["anul"]),
        ("Anulação de Empenho", ["anula", "de", "empenho"]),
        ("ANULAÇÕES", ["anula"]),
        ("valor anulado", ["valor", "anul"]),
        ("saldo disponível", ["saldo", "disponivel"]),
        ("mar", ["mar"]),  # radical ficaria com < 4 letras
        ("a e o", []),
        ("e60_vlranu", ["e60", "vlranu"]),
        ("2024 12", []),  # sem letra
        ("empenho empenhos", ["empenho"]),
    ],
)
def test_normalize_terms(text: str, expected: list[str]) -> None:
    assert normalize_terms(text) == expected


def test_fts_syntax_never_reaches_match() -> None:
    assert normalize_terms('"anulação" -empenho* :valor NEAR(') == [
        "anula",
        "empenho",
        "valor",
        "near",
    ]


def test_helpers() -> None:
    assert path_name("emp4_anularempenho001.php") == (
        "emp anularempenho php emp4_anularempenho001.php"
    )
    assert split_symbol("cl_empEmpenho") == "cl emp Empenho cl_empEmpenho"
    assert boilerplate_key("Copyright (C) 2014  Acme Informática") == (
        "copyright (c) acme informatica"
    )
    assert len(shingles("uma frase curta")) == 1
    assert len(shingles("um dois tres quatro cinco seis sete")) == 3


# --------------------------------------------------------------------------- índice


def _record(php: PhpRecords, path: str, *texts: str, **fields: Any) -> PhpFileRecord:
    items = [{"kind": "comment", "text": t, "line": n, "scope": ""} for n, t in enumerate(texts, 1)]
    return php.record(path, texts=items, **fields)


def _builder(records: list[PhpFileRecord]) -> GraphBuilder:
    builder = GraphBuilder()
    for record in records:
        builder.add_node(Node(file_id(record.path), NodeLabel.FILE, {"path": record.path}))
    return builder


@pytest.fixture
def corpus(php: PhpRecords) -> list[PhpFileRecord]:
    records = [
        _record(php, f"arq{n}.php", LICENSE.format(year=2009 + n, company="Acme")) for n in range(3)
    ]
    records.append(
        _record(  # variante com erro de digitação, num arquivo só
            php,
            "emp4_anularempenho001.php",
            LICENSE.format(year=2014, company="DBselller"),
            "acerto paliativo - erro na quantidade e valor dos itens",
        )
    )
    return records


def test_boilerplate_filter_drops_license_and_variants(corpus: list[PhpFileRecord]) -> None:
    settings = Settings(boilerplate_min_files=2)
    docs, stats = build_documents(lambda: corpus, _builder(corpus), settings)
    texts = {doc.node_id: doc.text for doc in docs}
    assert all("Software Publico" not in text for text in texts.values())
    assert texts["File:emp4_anularempenho001.php"] == (
        "acerto paliativo - erro na quantidade e valor dos itens"
    )
    assert (stats.texts, stats.texts_dropped) == (5, 4)
    assert [top["files"] for top in stats.boilerplate_top] == [3, 1]  # chave sem dígitos
    assert stats.boilerplate_top[0]["text"].startswith("Sistema Exemplo Software Publico")


def test_boilerplate_limit_is_strictly_greater(corpus: list[PhpFileRecord]) -> None:
    docs, stats = build_documents(lambda: corpus, _builder(corpus), Settings())
    assert stats.texts_dropped == 0 and stats.boilerplate_top == []
    assert "Software Publico" in {d.node_id: d.text for d in docs}["File:arq0.php"]


def test_file_document_fields(php: PhpRecords) -> None:
    record = php.record(
        "forms/db_frm.php",
        declares=[php.decl("function", "js_pesquisaEmpenho")],
        calls=[
            php.call("method", "label", var="clrotulo") | {"arg0": "e60_vlranu"},
            php.call("method", "label", var="clrotulo") | {"arg0": "z01_nome"},
        ],
    )
    builder = _builder([record])
    builder.add_node(
        Node(
            "Table:public.empempenho",
            NodeLabel.TABLE,
            {"columns_catalog": {"e60_vlranu": {"label": "Valor Anulado", "description": "Vlr"}}},
        )
    )
    docs, _stats = build_documents(lambda: [record], builder, Settings())
    doc = {d.node_id: d for d in docs}["File:forms/db_frm.php"]
    assert doc.labels == "Valor Anulado\nVlr\nz01_nome"  # coluna sem catálogo: só o nome
    assert doc.symbols == "js pesquisa Empenho js_pesquisaEmpenho"
    assert doc.name == "forms db frm php forms/db_frm.php"


# --------------------------------------------------------------------------- consulta


@pytest.fixture
def db(tmp_path: Path, corpus: list[PhpFileRecord]) -> Path:
    target = tmp_path / "g.db"
    builder = _builder(corpus)
    store.save(builder, target, {})
    docs, stats = build_documents(lambda: corpus, builder, Settings(boilerplate_min_files=2))
    write_index(target, docs, stats)
    return target


def test_search_by_text_and_by_name(db: Path) -> None:
    [hit] = search(db, "acerto paliativo")
    assert (hit.node_id, hit.field, hit.matched) == ("File:emp4_anularempenho001.php", "text", 2)
    assert hit.snippet.startswith("[acerto] [paliativo]")
    assert hit.next_command == "php-map trace --entry emp4_anularempenho001.php"
    [by_name] = search(db, "empenho")  # só pelo trigram de `anularempenho001`
    assert (by_name.node_id, by_name.field) == ("File:emp4_anularempenho001.php", "name")
    assert search(db, "software publico gestao municipal") == []


def test_search_options_and_odd_input(db: Path) -> None:
    assert search(db, '"acerto" -paliativo* :') != []  # sintaxe do FTS5 não quebra
    assert search(db, "acerto inexistente") == []
    assert [h.node_id for h in search(db, "acerto inexistente", any_term=True)] == [
        "File:emp4_anularempenho001.php"
    ]
    assert search(db, "acerto", label="Table") == []
    assert search(db, "acerto", module="empenho") == []
    assert search(db, "- * :") == []


def test_search_without_index(tmp_path: Path) -> None:
    target = tmp_path / "sem_indice.db"
    store.save(GraphBuilder(), target, {})
    with pytest.raises(StoreError, match="sem índice de busca"):
        search(target, "empenho")


def test_write_index_needs_the_graph_db(tmp_path: Path) -> None:
    with pytest.raises(StoreError, match="inexistente"):
        write_index(
            tmp_path / "nao_existe.db", [], build_documents(list, GraphBuilder(), Settings())[1]
        )


def test_write_index_replaces_previous_index(db: Path, corpus: list[PhpFileRecord]) -> None:
    builder = _builder(corpus[:1])
    docs, stats = build_documents(lambda: corpus[:1], builder, Settings())
    write_index(db, docs, stats)
    with sqlite3.connect(db) as conn:
        assert conn.execute("SELECT count(*) FROM search_docs").fetchone()[0] == 1
        meta = dict(conn.execute("SELECT key, value FROM meta").fetchall())
    assert meta["search_docs"] == "1" and "search_built_at" in meta


def test_search_texts_keeps_only_filtered_texts_with_origin(db: Path) -> None:
    """`search_texts` guarda tipo, linha e `scope` só dos textos que passaram."""
    with sqlite3.connect(db) as conn:
        rows = conn.execute("SELECT file_id, kind, text, line, scope FROM search_texts").fetchall()
        indexes = {r[1] for r in conn.execute("PRAGMA index_list('search_texts')")}
    assert rows == [
        (
            "File:emp4_anularempenho001.php",
            "comment",
            "acerto paliativo - erro na quantidade e valor dos itens",
            2,
            "",
        )
    ]
    assert "ix_search_texts_file" in indexes
