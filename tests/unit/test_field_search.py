"""Busca de campo por termo de negócio, no banco inteiro (sem IA)."""

import os
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest
import term_graphs as tg

from php_map.analysis import field_search as fs
from php_map.errors import QueryError
from php_map.graph import store


@pytest.fixture
def db(tmp_path: Path) -> Path:
    fs.clear_cache()
    return tg.build_db(tmp_path)


def _search(db: Path, term: str, **kwargs: object) -> fs.FieldSearch:
    with closing(store.connect_readonly(db)) as conn:
        return fs.field_search(conn, term, **kwargs)  # type: ignore[arg-type]


def _by_name(result: fs.FieldSearch) -> dict[str, fs.FieldHit]:
    return {f"{h.table_id.partition(':')[2]}.{h.column}": h for h in result.hits}


def test_the_square_meter_case_puts_the_read_columns_first_in_their_group(db: Path) -> None:
    result = _search(db, "valor do metro quadrado do terreno")
    hits = _by_name(result)
    assert result.total == 5 and result.more == 0 and not result.warnings
    calc = [h for h in result.hits if h.group == fs.GROUP_CALC]
    assert {f"{h.table_id}.{h.column}" for h in calc} == {
        f"{tg.FACEVAL}.j81_valorterreno",
        f"{tg.ZONASVAL}.j51_valorm2t",
    }
    assert hits["cadastro.face.j37_valor"].group == fs.GROUP_REGISTER
    assert hits["cadastro.iptucalc.j23_m2terr"].group == fs.GROUP_NONE
    assert hits["cadastro.setorloc.j05_valor"].group == fs.GROUP_NONE
    assert "vacinas.vacina.vac_aplicalote" not in hits  # o ruído do sinônimo «lote»


def test_each_hit_says_who_reads_it_and_where_it_is_registered(db: Path) -> None:
    hits = _by_name(_search(db, "valor do metro quadrado do terreno"))
    face = hits["cadastro.faceval.j81_valorterreno"]
    assert face.readers == (tg.VVT_FACE,) and face.readers_total == 1
    assert face.menus == (tg.MENU_FACEVAL,) and face.note is None
    assert face.table_label == "Valores da face por ano" and face.label == "Valor m2 terreno"
    zonas = hits["cadastro.zonasval.j51_valorm2t"]
    assert zonas.readers == (tg.VVT_ZONA, tg.VVT_ZONA2) and zonas.readers_total == 2
    assert zonas.menus == () and zonas.note is not None
    assert zonas.note.startswith("tela de cadastro sem item de menu")


def test_a_column_only_written_by_a_function_has_no_known_registration(db: Path) -> None:
    only_written = _by_name(_search(db, "valor do metro quadrado do terreno"))[
        "cadastro.iptucalc.j23_m2terr"
    ]
    assert only_written.readers == () and only_written.readers_total == 0
    assert only_written.writers_total == 1 and only_written.menus == ()
    assert only_written.note == "tabela sem tela de cadastro no mapa"


def test_use_in_calculation_adds_a_bonus_proportional_to_the_readers(db: Path) -> None:
    hits = _by_name(_search(db, "valor do metro quadrado do terreno"))
    one = hits["cadastro.faceval.j81_valorterreno"]
    two = hits["cadastro.zonasval.j51_valorm2t"]
    # mesmo placar de rótulo (3 dos 5 termos = 0,72); bônus de uso = 0,1 x leitores/7; menu = +0,05
    assert one.score == pytest.approx(0.72 + fs.USE_BONUS / fs.USE_CAP + 0.05, abs=1e-3)
    assert two.score == pytest.approx(0.72 + 2 * fs.USE_BONUS / fs.USE_CAP, abs=1e-3)
    assert hits["cadastro.face.j37_valor"].score == pytest.approx(0.72 + 0.05)
    assert hits["cadastro.iptucalc.j23_m2terr"].score == pytest.approx(0.72)


def test_the_bonus_orders_equal_labels_by_readers(db: Path) -> None:
    with closing(store.connect_readonly(db)) as conn:
        result = fs.field_search(conn, "m²")
    # os 3 rótulos têm «m2»; as lidas por função passam na frente das que não têm leitor
    calc = [h.column for h in result.hits if h.group == fs.GROUP_CALC]
    assert calc == ["j81_valorterreno", "j51_valorm2t"]


def test_scores_below_the_cut_are_noise_and_do_not_appear(db: Path) -> None:
    result = _search(db, "valor do metro quadrado do terreno")
    assert all(h.score >= fs.MIN_SCORE for h in result.hits)
    # «lote» vale como «terreno» (1 de 5 termos = 0,44): fora; em «área do terreno» (1 de 2) = 0,65
    area = _by_name(_search(db, "área do terreno"))
    assert area["vacinas.vacina.vac_aplicalote"].score == pytest.approx(0.65)
    assert area["itbi.itbi.it01_areaterreno"].score == pytest.approx(1.0)


def test_the_noise_does_not_pass_the_exact_label(db: Path) -> None:
    result = _search(db, "área do terreno")
    ordered = [h.column for h in result.hits]
    assert ordered.index("it01_areaterreno") < ordered.index("vac_aplicalote")


def test_the_cut_keeps_the_best_and_counts_the_rest(db: Path) -> None:
    result = _search(db, "área do terreno", limit=3)
    assert len(result.hits) == 3 and result.total == 8 and result.more == 5
    assert result.hits[0].score == max(h.score for h in result.hits)
    assert "e mais 5" in fs.render_field_search(result)


def test_the_limit_has_a_floor_and_a_ceiling(db: Path) -> None:
    assert len(_search(db, "área do terreno", limit=0).hits) == 1
    assert len(_search(db, "área do terreno", limit=999).hits) == 8


@pytest.mark.parametrize("term", ["valor", "Valor", "o código do", "  data  "])
def test_a_generic_term_warns_and_does_not_search(db: Path, term: str) -> None:
    result = _search(db, term)
    assert result.hits == () and result.total == 0
    assert result.warnings and (
        "genérico" in result.warnings[0] or "palavras úteis" in result.warnings[0]
    )


def test_a_term_with_only_stopwords_has_its_own_warning(db: Path) -> None:
    result = _search(db, "de do a")
    assert result.terms == () and "palavras úteis" in result.warnings[0]


def test_a_term_with_no_match_says_so(db: Path) -> None:
    result = _search(db, "cor do cavalo")
    assert result.hits == () and "nenhuma coluna" in result.warnings[0]
    assert "Aviso" in fs.render_field_search(result)


def test_accents_and_the_squared_sign_are_normalized(db: Path) -> None:
    plain = _search(db, "area do terreno")
    accented = _search(db, "ÁREA do Terreno")
    assert [h.column for h in plain.hits] == [h.column for h in accented.hits]
    assert _search(db, "m2").hits and [h.column for h in _search(db, "m²").hits] == [
        h.column for h in _search(db, "M2").hits
    ]


def test_metro_quadrado_counts_as_m2(db: Path) -> None:
    result = _search(db, "valor do metro quadrado")  # «metro quadrado» soma a classe «m2»
    assert "m2" in result.terms
    assert "j51_valorm2t" in {h.column for h in result.hits}


def test_the_module_filter_keeps_only_that_module_tables(db: Path) -> None:
    with closing(sqlite3.connect(db)) as raw, raw:
        raw.execute(
            "CREATE TABLE analytics_tables (table_id TEXT PRIMARY KEY, owner_modules TEXT, "
            "reader_modules TEXT, writer_modules TEXT, readers INTEGER, writers INTEGER, "
            "shared INTEGER)"
        )
        raw.execute(
            "INSERT INTO analytics_tables VALUES (?, ?, '{}', '{}', 0, 0, 0)",
            (tg.ITBI, '["Module:itbi"]'),
        )
    result = _search(db, "área do terreno", module="itbi")
    assert {h.table_id for h in result.hits} == {tg.ITBI}
    assert _search(db, "área do terreno", module="Module:itbi").total == 2


def test_the_module_filter_needs_the_module_classification(db: Path) -> None:
    with pytest.raises(QueryError, match="classificação de módulo"):
        _search(db, "área do terreno", module="itbi")


def test_the_dictionary_is_cached_by_path_and_mtime(db: Path) -> None:
    fs.clear_cache()
    with closing(store.connect_readonly(db)) as conn:
        first = fs.dictionary_index(conn)
        assert fs.dictionary_index(conn) is first  # a 2ª busca não lê os `attrs` de novo
    with closing(store.connect_readonly(db)) as other:
        assert fs.dictionary_index(other) is first  # outra conexão, mesmo arquivo e `mtime`
    stat = db.stat()
    os.utime(db, ns=(stat.st_atime_ns, stat.st_mtime_ns + 5_000_000_000))
    with closing(store.connect_readonly(db)) as conn:
        fresh = fs.dictionary_index(conn)
    assert fresh is not first and len(fresh) == len(first)  # mudou o `mtime`: leu de novo
    assert len(fs._CACHE) == 1  # e a chave velha saiu


def test_an_in_memory_connection_is_never_cached() -> None:
    fs.clear_cache()
    conn = sqlite3.connect(":memory:")
    conn.execute(
        "CREATE TABLE nodes (id TEXT PRIMARY KEY, label TEXT NOT NULL, attrs TEXT NOT NULL)"
    )
    conn.execute(
        "INSERT INTO nodes VALUES ('Table:a.b', 'Table', "
        '\'{"columns_catalog": {"c": {"label": "Área do terreno"}}}\')'
    )
    entries = fs.dictionary_index(conn)
    assert len(entries) == 1 and fs._CACHE == {}
    assert fs.dictionary_index(conn) is not entries


def test_the_json_and_the_markdown_agree(db: Path) -> None:
    result = _search(db, "valor do metro quadrado do terreno")
    data = fs.field_search_json(result)
    assert data["total"] == 5 and data["hits"][0]["group"] in fs.GROUPS
    text = fs.render_field_search(result)
    assert text.index("## Usadas em cálculo") < text.index("## Só cadastradas")
    assert text.index("## Só cadastradas") < text.index("## Sem cadastro conhecido")
    assert "`fc_vvt_face`" in text and tg.MENU_FACEVAL in text
    assert "pode depender do caso" in text


def test_routine_names_keep_the_signature_only_when_two_share_the_name() -> None:
    plain = fs.routine_names(("PgFunction:public.fc_a(int4)", "PgFunction:public.fc_b(int4)"))
    assert plain == ["fc_a", "fc_b"]
    twins = fs.routine_names(("PgFunction:public.fc_a(int4)", "PgFunction:public.fc_a(int4,date)"))
    assert twins == ["fc_a(int4)", "fc_a(int4,date)"]
