"""Blocos e modelos de função nos briefings (motor de briefings).

Reaproveita o banco de `test_cards` (FUNC com regras e campos reais) e acrescenta arquivos PHP
que chamam as funções.
"""

import json
import sqlite3
from collections.abc import Iterator
from pathlib import Path

import pytest
from card_graphs import FUNC, _db

from php_map.briefing import blocks, engine
from php_map.briefing.model import BriefingError

TRIG = "PgFunction:public.fc_trig()"
GENERATED = "PgFunction:public.fc_auditoria_tabela_abc()"
MODULE = "Module:teste"


def _add_edge(conn: sqlite3.Connection, src: str, dst: str, *, path: str, line: int = 10) -> None:
    conn.execute(
        "INSERT INTO edges (src, dst, type, source, confidence, evidence, attrs) "
        "VALUES (?, ?, 'CALLS_RPC', 'PHP_AST', 1.0, ?, '{}')",
        (src, dst, json.dumps({"path": path, "line": line})),
    )


def _blocks_db(tmp_path: Path) -> Path:
    db = _db(tmp_path)
    with sqlite3.connect(db) as conn:
        conn.execute("INSERT INTO nodes VALUES (?, 'File', ?)", ("File:emp/a.php", json.dumps({})))
        conn.execute("INSERT INTO nodes VALUES (?, 'File', ?)", ("File:emp/b.php", json.dumps({})))
        conn.execute(
            "INSERT INTO nodes VALUES (?, 'Module', ?)", (MODULE, json.dumps({"name": "Teste"}))
        )
        # fc_trig: 2 chamadores PHP; FUNC (fc_calcula): 1 chamador PHP.
        _add_edge(conn, "File:emp/a.php", TRIG, path="emp/a.php", line=5)
        _add_edge(conn, "File:emp/b.php", TRIG, path="emp/b.php", line=7)
        _add_edge(conn, "File:emp/a.php", FUNC, path="emp/a.php", line=20)
        conn.commit()
    return db


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return _blocks_db(tmp_path)


@pytest.fixture
def ctx(db: Path) -> Iterator[blocks.BriefingContext]:
    with blocks.BriefingContext([db]) as context:
        yield context


def render(ctx: blocks.BriefingContext, body: str, params: str, **values: object):  # type: ignore[no-untyped-def]
    text = (
        "---\nid: teste\ntitulo: t\ndescricao: t\nversao: 1\nstatus: rascunho\nautor: eu\n"
        f"parametros:\n{params}\nresposta: {{formato: livre}}\n"
        f"limite_palavras: 20000\n---\n{body}\n"
    )
    template, problems = engine.check_template(text)
    assert template is not None and problems == [], problems
    return engine.render(template, values, ctx)


# --------------------------------------------------------------------------- ficha_funcao


def test_ficha_funcao_renders_the_function_card(ctx: blocks.BriefingContext) -> None:
    result = render(
        ctx, "{{bloco:ficha_funcao funcao={{f}}}}", "  - {nome: f, tipo: funcao}", f=FUNC
    )
    assert "Confere o saldo antes de calcular o valor final" in result.markdown
    assert "Se `b < 0`, a mensagem é: «Saldo % invalido»" in result.markdown
    assert "## Glossário" not in result.markdown  # bloco não chamado aqui


def test_ficha_funcao_empty_value_is_skipped(ctx: blocks.BriefingContext) -> None:
    result = render(
        ctx,
        "x{{bloco:ficha_funcao funcao={{f}}}}y",
        "  - {nome: f, tipo: funcao, obrigatorio: false}",
    )
    assert "xy" in result.markdown.replace("\n", "")


def test_ficha_funcao_unknown_function_raises(ctx: blocks.BriefingContext) -> None:
    with pytest.raises(BriefingError, match="função não encontrado"):
        render(ctx, "{{bloco:ficha_funcao funcao={{f}}}}", "  - {nome: f, tipo: funcao}", f="fc_x")


# --------------------------------------------------------------------------- regras_funcao


def test_regras_funcao_default_shows_the_validation(ctx: blocks.BriefingContext) -> None:
    result = render(
        ctx, "{{bloco:regras_funcao funcao={{f}}}}", "  - {nome: f, tipo: funcao}", f=FUNC
    )
    assert "Se `b < 0`, a mensagem é: «Saldo % invalido»" in result.markdown
    assert "linha" in result.markdown


def test_regras_funcao_niveis_tudo_also_shows_comments(ctx: blocks.BriefingContext) -> None:
    default = render(
        ctx, "{{bloco:regras_funcao funcao={{f}}}}", "  - {nome: f, tipo: funcao}", f=FUNC
    )
    assert "Saldo % invalido" in default.markdown
    assert "Confere o saldo antes de calcular" not in default.markdown  # só erro/aviso

    full = render(
        ctx,
        "{{bloco:regras_funcao funcao={{f}} niveis=tudo}}",
        "  - {nome: f, tipo: funcao}",
        f=FUNC,
    )
    assert "Saldo % invalido" in full.markdown
    assert "Confere o saldo antes de calcular" in full.markdown  # tudo: inclui comentários
    assert "Comentários do autor" in full.markdown


def test_regras_funcao_without_rules_says_so(ctx: blocks.BriefingContext) -> None:
    result = render(
        ctx, "{{bloco:regras_funcao funcao={{f}}}}", "  - {nome: f, tipo: funcao}", f=GENERATED
    )
    assert "Nenhuma regra encontrada" in result.markdown


# --------------------------------------------------------------------------- campos_funcao


def test_campos_funcao_shows_table_and_columns(ctx: blocks.BriefingContext) -> None:
    result = render(
        ctx, "{{bloco:campos_funcao funcao={{f}}}}", "  - {nome: f, tipo: funcao}", f=FUNC
    )
    assert "Empenhos" in result.markdown and "empempenho" in result.markdown
    assert "Valor anulado" in result.markdown


# --------------------------------------------------------------------------- consultas_funcao


def test_consultas_funcao_shows_normalized_text_and_line(ctx: blocks.BriefingContext) -> None:
    result = render(
        ctx, "{{bloco:consultas_funcao funcao={{f}}}}", "  - {nome: f, tipo: funcao}", f=FUNC
    )
    assert "linha 9" in result.markdown
    assert "empempenho" in result.markdown


def test_consultas_funcao_without_queries_says_so(ctx: blocks.BriefingContext) -> None:
    result = render(
        ctx, "{{bloco:consultas_funcao funcao={{f}}}}", "  - {nome: f, tipo: funcao}", f=TRIG
    )
    assert "Nenhuma consulta encontrada" in result.markdown


# --------------------------------------------------------------------------- origem_funcao


def test_origem_funcao_without_data_says_so(ctx: blocks.BriefingContext) -> None:
    result = render(
        ctx, "{{bloco:origem_funcao funcao={{f}}}}", "  - {nome: f, tipo: funcao}", f=FUNC
    )
    assert "Sem a origem das funções da base no mapa" in result.markdown


# --------------------------------------------------------------------------- uso_coluna


def test_uso_coluna_lists_the_write_use(ctx: blocks.BriefingContext) -> None:
    result = render(
        ctx,
        "{{bloco:uso_coluna tabela={{t}} coluna={{c}}}}",
        "  - {nome: t, tipo: tabela}\n  - {nome: c, tipo: texto}",
        t="empempenho",
        c="e60_vlranu",
    )
    assert "fc_calcula" in result.markdown and "grava" in result.markdown


def test_uso_coluna_empty_column_is_skipped(ctx: blocks.BriefingContext) -> None:
    result = render(
        ctx,
        "x{{bloco:uso_coluna tabela={{t}} coluna={{c}}}}y",
        "  - {nome: t, tipo: tabela}\n  - {nome: c, tipo: texto, obrigatorio: false}",
        t="empempenho",
    )
    assert "xy" in result.markdown.replace("\n", "")


def test_uso_coluna_without_usage_says_so(ctx: blocks.BriefingContext) -> None:
    # a coluna é usada só com papel `write`/`filter`; pedir `join` não acha nada
    result = render(
        ctx,
        "{{bloco:uso_coluna tabela={{t}} coluna={{c}} papel=join}}",
        "  - {nome: t, tipo: tabela}\n  - {nome: c, tipo: texto}",
        t="empempenho",
        c="e60_vlranu",
    )
    assert "Nenhum uso conhecido" in result.markdown


# --------------------------------------------------------------------------- disponibilidade


def test_function_blocks_say_so_without_the_task_tables(tmp_path: Path) -> None:
    broken = tmp_path / "broken.db"
    source = _blocks_db(tmp_path / "src")
    import shutil

    shutil.copy(source, broken)
    with sqlite3.connect(broken) as conn:
        conn.execute("DROP TABLE routine_rules")
        conn.execute("DROP TABLE routine_queries")
        conn.execute("DROP TABLE routine_fields")
        conn.execute("DROP TABLE routine_sources")
    with blocks.BriefingContext([broken]) as other:
        marked = {item["name"]: item for item in blocks.catalog(other)}
        assert marked["ficha_funcao"]["available"] is False
        assert "`routine_sources`" in marked["ficha_funcao"]["reason"]
        assert "refaça o scan" in marked["ficha_funcao"]["reason"]
        assert marked["regras_funcao"]["available"] is False
        assert "`routine_rules`" in marked["regras_funcao"]["reason"]
        assert "refaça o scan" in marked["regras_funcao"]["reason"]
        for name in ("campos_funcao", "consultas_funcao", "uso_coluna"):
            assert marked[name]["available"] is False
            assert "refaça o scan" in marked[name]["reason"]
        text = (
            "---\nid: teste\ntitulo: t\ndescricao: t\nversao: 1\nstatus: rascunho\nautor: eu\n"
            "parametros: []\nresposta: {formato: livre}\n"
            "limite_palavras: 20000\n---\n{{bloco:catalogo_de_blocos}}\n"
        )
        template, problems = engine.check_template(text)
        assert template is not None and problems == []
        briefing = engine.render(template, {}, other)
        assert "**não disponível neste mapa**" in briefing.markdown


# ----------------------------------------------------------------- modelos oficiais atualizados


def test_especificar_regras_with_function_param_adds_its_rules(ctx: blocks.BriefingContext) -> None:
    template = engine.load_official("especificar-regras")
    assert template.version == 2
    with pytest.raises(BriefingError, match="tela não encontrad"):
        engine.render(template, {"tela": "nao-existe", "funcao": FUNC}, ctx)


def test_casos_de_teste_has_the_optional_function_param(ctx: blocks.BriefingContext) -> None:
    template = engine.load_official("casos-de-teste")
    assert template.version == 2
    assert template.param("funcao") is not None and not template.param("funcao").required  # type: ignore[union-attr]


def test_impacto_de_mudanca_with_column_adds_its_usage(ctx: blocks.BriefingContext) -> None:
    template = engine.load_official("impacto-de-mudanca")
    assert template.version == 2
    briefing = engine.render(template, {"tabela": "empempenho", "coluna": "e60_vlranu"}, ctx)
    assert "fc_calcula" in briefing.markdown and "grava" in briefing.markdown


def test_impacto_de_mudanca_without_column_skips_the_block(ctx: blocks.BriefingContext) -> None:
    template = engine.load_official("impacto-de-mudanca")
    briefing = engine.render(template, {"tabela": "empempenho"}, ctx)
    assert "## Uso da coluna" not in briefing.markdown


# ----------------------------------------------------------------- de onde vem o valor (55)


def test_de_onde_vem_o_valor_with_explicit_target(ctx: blocks.BriefingContext) -> None:
    template = engine.load_official("de-onde-vem-o-valor")
    briefing = engine.render(
        template,
        {
            "funcao": FUNC,
            "pergunta": "de onde vem o valor anulado",
            "alvo": "public.empempenho.e60_vlranu",
        },
        ctx,
    )
    assert "vem de quem chama a função" in briefing.markdown
    assert "linha" in briefing.markdown


def test_de_onde_vem_o_valor_without_target_picks_automatically(
    ctx: blocks.BriefingContext,
) -> None:
    template = engine.load_official("de-onde-vem-o-valor")
    briefing = engine.render(
        template, {"funcao": FUNC, "pergunta": "de onde vem o valor anulado"}, ctx
    )
    assert any("alvo escolhido automaticamente" in w for w in briefing.warnings)
