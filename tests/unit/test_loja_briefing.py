"""Modelos de briefing para IA: formato, validação, montagem e armazenamento.

Usa o banco das fixtures (menu "Cancelar Pedido", módulo `pedidos`, tabelas com rótulo).
"""

import hashlib
from collections.abc import Iterator
from pathlib import Path

import pytest

from php_map.briefing import blocks, engine, parse, resolve
from php_map.briefing.model import BriefingError, TemplateError

SCREEN = "ped4_cancelarpedido001.php"
MENU = "BusinessAction:menu:7001"
HEADER = """---
id: teste
titulo: Modelo de teste
descricao: Só para os testes.
versao: 1
status: rascunho
autor: eu
parametros:
{params}
resposta: {{formato: livre}}
limite_palavras: {limit}
---
{body}
"""


def model_text(body: str, params: str = "  - {nome: tela, tipo: tela}", limit: int = 5000) -> str:
    return HEADER.format(params=params, body=body, limit=limit)


@pytest.fixture
def ctx(shop_db: Path) -> Iterator[blocks.BriefingContext]:
    with blocks.BriefingContext([shop_db]) as context:
        yield context


# --------------------------------------------------------------------------- formato


def test_parse_reads_header_params_and_body() -> None:
    template = parse.parse_template(
        model_text(
            "Olá {{tela}}",
            '  - {nome: tela, tipo: tela, rotulo: "Tela, ou menu", obrigatorio: true}\n'
            "  - {nome: n, tipo: numero, padrao: 4, minimo: 1, maximo: 6}\n"
            "  - {nome: cor, tipo: escolha, opcoes: [a, b], obrigatorio: false}",
        )
    )
    assert (template.id, template.version, template.status, template.word_limit) == (
        "teste",
        1,
        "rascunho",
        5000,
    )
    tela, numero, cor = template.params
    assert tela.label == "Tela, ou menu" and tela.required
    assert (numero.default, numero.minimum, numero.maximum, numero.required) == (4, 1.0, 6.0, False)
    assert cor.options == ("a", "b") and not cor.required
    assert template.body.strip() == "Olá {{tela}}" and template.body_line == 15


@pytest.mark.parametrize(
    ("text", "message"),
    [
        ("sem cabeçalho", "começar com um cabeçalho"),
        ("---\nid: x\n", "não termina"),
        (model_text("c").replace("id: teste", "id: Maiusculo!"), "`id` inválido"),
        (model_text("c", "  - {nome: x, tipo: coisa}"), "tipo de parâmetro inválido"),
        (model_text("c", "  - {nome: x, tipo: escolha}"), "precisa de `opcoes`"),
        (model_text("c").replace("status: rascunho", "status: pronto"), "`status` inválido"),
        (model_text("c", limit=5), "`limite_palavras`"),
        (model_text("  "), "corpo do modelo está vazio"),
    ],
)
def test_parse_problems(text: str, message: str) -> None:
    with pytest.raises(TemplateError) as raised:
        parse.parse_template(text)
    assert message in " ".join(p.text() for p in raised.value.problems)


def test_scan_markers_counts_nested_braces_and_lines() -> None:
    body = 'a\n{{bloco:ficha_tela tela={{tela}} profundidade=4 nota="dois  espaços"}}\n{{tela}}'
    markers, problems = parse.scan_markers(body, first_line=10)
    assert problems == []
    block, param = markers
    assert (block.kind, block.name, block.line) == ("block", "ficha_tela", 11)
    assert dict(block.args) == {
        "tela": ("param", "tela"),
        "profundidade": ("literal", "4"),
        "nota": ("literal", "dois  espaços"),
    }
    assert (param.kind, param.name, param.line) == ("param", "tela", 12)


@pytest.mark.parametrize(
    "body", ["{{bloco:x", "{{ 1 + 1 }}", "{{bloco:Ficha}}", "{{bloco:ficha_tela tela}}"]
)
def test_scan_markers_reports_invalid_markers(body: str) -> None:
    _markers, problems = parse.scan_markers(body)
    assert problems and problems[0].line == 1


# --------------------------------------------------------------------------- validação


def test_check_template_suggests_the_right_block_and_lists_all_problems() -> None:
    text = model_text(
        "{{bloco:ficha_tella tela={{tela}}}}\n"
        "{{bloco:ficha_tela tela={{outra}} x=1}}\n"
        "{{bloco:tabela}}\n",
        "  - {nome: tela, tipo: tela}\n  - {nome: sobra, tipo: texto}",
    )
    template, problems = engine.check_template(text)
    assert template is not None
    messages = [p.text() for p in problems]
    assert any("`ficha_tella` não existe" in m and "`ficha_tela`" in m for m in messages)
    assert any("`outra`" in m and "não foi declarado" in m for m in messages)
    assert any("não tem o argumento `x`" in m for m in messages)
    assert any("falta o argumento `tabela`" in m for m in messages)
    assert any("`sobra` foi declarado e nunca é usado" in m for m in messages)


def test_a_valid_template_has_no_problems() -> None:
    _t, problems = engine.check_template(model_text("{{bloco:ficha_tela tela={{tela}}}}"))
    assert problems == []


# --------------------------------------------------------------------------- resolvedores


def test_resolve_screens_by_file_menu_id_and_menu_text(ctx: blocks.BriefingContext) -> None:
    expected = [f"File:{SCREEN}"]
    assert resolve.resolve_screens(ctx.conn, SCREEN) == expected
    assert resolve.resolve_screens(ctx.conn, MENU) == expected
    assert resolve.resolve_screens(ctx.conn, "Cancelar Pedido") == expected
    with pytest.raises(BriefingError, match="não encontrad"):
        resolve.resolve_screens(ctx.conn, "tela que nao existe")


def test_resolve_table_module_and_errors(ctx: blocks.BriefingContext) -> None:
    assert resolve.resolve_table(ctx.conn, "pedido") == "Table:public.pedido"
    assert (
        resolve.resolve_table(ctx.conn, "public.pedidocancelado") == "Table:public.pedidocancelado"
    )
    assert resolve.resolve_module(ctx.conn, "pedidos") == "Module:pedidos"
    assert resolve.resolve_module(ctx.conn, "Pedidos") == "Module:pedidos"
    with pytest.raises(BriefingError, match="módulo não encontrado.*Você quis dizer: pedidos"):
        resolve.resolve_module(ctx.conn, "pedidoss")
    with pytest.raises(BriefingError, match="tabela não encontrad"):
        resolve.resolve_table(ctx.conn, "nao_existe")


def test_resolve_routine_and_column(ctx: blocks.BriefingContext) -> None:
    assert resolve.resolve_routine(ctx.conn, "tg_pedido_auditoria").startswith("PgFunction:public.")
    column = resolve.resolve_column(ctx.conn, "pedido.ped01_codigo")
    assert column == "Table:public.pedido#ped01_codigo"
    with pytest.raises(BriefingError, match="não tem a coluna"):
        resolve.resolve_column(ctx.conn, "pedido.nao_existe")


# --------------------------------------------------------------------------- montagem


def render(ctx: blocks.BriefingContext, text: str, **params: object):  # type: ignore[no-untyped-def]
    template, problems = engine.check_template(text)
    assert template is not None and problems == [], problems
    return engine.render(template, params, ctx)


def test_render_builds_a_deterministic_briefing(ctx: blocks.BriefingContext) -> None:
    text = model_text(
        "# Contexto\n{{bloco:ficha_tela tela={{tela}}}}\n\n{{bloco:glossario de=contexto}}"
    )
    first = render(ctx, text, tela=MENU)
    second = render(ctx, text, tela=MENU)
    assert first.markdown == second.markdown
    assert (
        hashlib.sha256(first.markdown.encode()).hexdigest()
        == hashlib.sha256(second.markdown.encode()).hexdigest()
    )
    assert first.markdown.startswith("> Briefing gerado pelo php-map · modelo `teste` v1")
    assert "mapa de 2026-09-25T10:00:00-03:00" in first.markdown
    assert first.blocks == ("ficha_tela", "glossario")
    assert "## Tela: Pedidos > Procedimentos > Cancelar Pedido" in first.markdown
    assert "## Glossário" in first.markdown and first.words == len(first.markdown.split())


def test_a_typed_value_is_never_reexpanded(ctx: blocks.BriefingContext) -> None:
    text = model_text("Dúvida: {{duvida}}", "  - {nome: duvida, tipo: texto}")
    trap = "{{bloco:ficha_tela tela=" + SCREEN + "}} e {{duvida}}"
    result = render(ctx, text, duvida=trap)
    assert "{ {bloco:ficha_tela" in result.markdown and "## Tela:" not in result.markdown
    assert result.blocks == ()


def test_render_validates_parameters(ctx: blocks.BriefingContext) -> None:
    text = model_text(
        "{{n}} {{c}} {{bloco:ficha_tela tela={{tela}}}}",
        "  - {nome: tela, tipo: tela}\n"
        "  - {nome: n, tipo: numero, padrao: 4, minimo: 1, maximo: 6}\n"
        "  - {nome: c, tipo: escolha, opcoes: [a, b], padrao: a}",
    )
    template, _ = engine.check_template(text)
    assert template is not None
    with pytest.raises(BriefingError, match="falta o parâmetro `tela`"):
        engine.render(template, {}, ctx)
    with pytest.raises(BriefingError, match="deve ser ≤ 6"):
        engine.render(template, {"tela": MENU, "n": "9"}, ctx)
    with pytest.raises(BriefingError, match="precisa ser um número"):
        engine.render(template, {"tela": MENU, "n": "x"}, ctx)
    with pytest.raises(BriefingError, match="aceita: a, b"):
        engine.render(template, {"tela": MENU, "c": "z"}, ctx)
    with pytest.raises(BriefingError, match="tela não encontrad"):
        engine.render(template, {"tela": "nao-existe"}, ctx)
    ok = engine.render(template, {"tela": MENU}, ctx)
    assert "\n4 a " in ok.markdown  # os padrões entram


def test_render_cuts_over_the_word_limit(ctx: blocks.BriefingContext) -> None:
    text = model_text("{{duvida}}", "  - {nome: duvida, tipo: texto}", limit=100)
    long = "\n\n".join(f"parágrafo {i} " + "palavra " * 40 for i in range(10))
    result = render(ctx, text, duvida=long)
    assert result.words <= 130
    assert "briefing cortado" in result.markdown
    assert any("passou do limite de 100 palavras" in w for w in result.warnings)


def test_render_refuses_a_briefing_with_a_secret(ctx: blocks.BriefingContext) -> None:
    text = model_text("{{duvida}}", "  - {nome: duvida, tipo: texto}")
    with pytest.raises(BriefingError) as raised:
        render(ctx, text, duvida="o banco usa password=Sup3rS3cretValor na conexão")
    assert "segredo aparente" in str(raised.value) and "Sup3rS3cretValor" not in str(raised.value)


def test_blocks_without_data_say_so_instead_of_failing(ctx: blocks.BriefingContext) -> None:
    text = model_text("{{bloco:endpoint endpoint={{e}}}}", "  - {nome: e, tipo: texto}")
    template, _ = engine.check_template(text)
    assert template is not None
    # o grafo das fixtures não tem endpoints: o resolvedor do tipo `endpoint` recusa, com mensagem
    with pytest.raises(BriefingError, match="endpoint não encontrad"):
        engine.render(template, {"e": "x"}, ctx)


# ----------------------------------------------------------------- blocos e modelos oficiais


def objects(ctx: blocks.BriefingContext) -> dict[str, dict[str, object]]:
    return {
        "como-se-faz": {"tela": MENU},
        "impacto-de-mudanca": {"tabela": "pedido"},
        "especificar-regras": {"tela": SCREEN},
        "casos-de-teste": {"tela": SCREEN},
        "plano-de-corte-do-modulo": {"modulo": "pedidos"},
        "treinamento-do-modulo": {"modulo": "pedidos"},
        "duvida-com-contexto": {"duvida": "cancelar pedido"},
        "criar-novo-modelo": {"necessidade": "explicar uma coluna"},
        "de-onde-vem-o-valor": {
            "funcao": "tg_pedido_auditoria",
            "pergunta": "de onde vem a situação do pedido",
        },
    }


def test_there_are_nine_official_models_and_all_render(ctx: blocks.BriefingContext) -> None:
    ids = engine.official_ids()
    assert len(ids) == 9
    given = objects(ctx)
    assert sorted(given) == ids
    for template_id in ids:
        template = engine.load_official(template_id)
        assert template.status == "oficial" and template.source == "official"
        assert engine.check_template(engine.official_text(template_id), source="official")[1] == []
        briefing = engine.render(template, given[template_id], ctx)
        assert briefing.words > 50 and f"modelo `{template_id}`" in briefing.markdown
        assert "SOMENTE" in briefing.markdown or template_id == "criar-novo-modelo"


def test_meta_model_lists_exactly_the_blocks_in_the_code(ctx: blocks.BriefingContext) -> None:
    briefing = engine.render(engine.load_official("criar-novo-modelo"), {"necessidade": "x"}, ctx)
    listed = {item["name"] for item in blocks.catalog(ctx)}
    assert listed == set(blocks.BLOCKS)
    for name, block in blocks.BLOCKS.items():
        assert f"### `{name}`" in briefing.markdown
        assert block.description in briefing.markdown
    assert "como-se-faz" in briefing.markdown  # os exemplos vêm dos modelos oficiais


def test_catalog_marks_blocks_that_the_map_cannot_serve(
    ctx: blocks.BriefingContext, tmp_path: Path
) -> None:
    items = {item["name"]: item for item in blocks.catalog(ctx)}
    assert items["ficha_tela"]["available"] is True and items["modulo"]["available"] is True
    # um grafo sem módulos nem textos da ficha: copia só o essencial
    import shutil
    import sqlite3

    broken = tmp_path / "g.db"
    shutil.copy(ctx.dbs[0], broken)
    with sqlite3.connect(broken) as conn:
        conn.execute("DROP TABLE search_texts")
        conn.execute("DELETE FROM analytics_modules")
    with blocks.BriefingContext([broken]) as other:
        marked = {item["name"]: item for item in blocks.catalog(other)}
        assert (
            marked["ficha_tela"]["available"] is False
            and "scan de novo" in marked["ficha_tela"]["reason"]
        )
        assert (
            marked["modulo"]["available"] is False
            and "não tem módulos" in marked["modulo"]["reason"]
        )
        text = model_text("{{bloco:modulo modulo={{m}}}}", "  - {nome: m, tipo: texto}")
        briefing = render(other, text, m="Module:pedidos")
        assert "Não disponível neste mapa" in briefing.markdown
        assert any("não tem módulos" in w for w in briefing.warnings)


def test_screen_card_markdown_has_the_expected_sections(ctx: blocks.BriefingContext) -> None:
    from php_map.report import cards

    card = cards.screen_card(ctx.conn, SCREEN)
    markdown = cards.render_screen_markdown(card)
    assert card.title == "Pedidos > Procedimentos > Cancelar Pedido"
    assert card.modules == ("Pedidos",)
    for section in (
        "### O que a tela grava",
        "### O que a tela lê",
        "### Mensagens de validação e erro",
    ):
        assert section in markdown
    assert markdown == cards.render_screen_markdown(cards.screen_card(ctx.conn, SCREEN))
    assert cards.render_screen_markdown(card, level=3).startswith("### Tela:")


# --------------------------------------------------------------------------- modelos do usuário


GOOD = model_text("{{bloco:ficha_tela tela={{tela}}}}").replace("id: teste", "id: meu-modelo")


def test_store_saves_as_draft_reviews_and_deletes(tmp_path: Path) -> None:
    store = engine.ModelStore(tmp_path / "modelos")
    saved = store.save(
        GOOD.replace("status: rascunho", "status: oficial")
    )  # nunca entra como oficial
    assert saved.id == "meu-modelo" and saved.status == "rascunho" and saved.source == "user"
    assert (tmp_path / "modelos" / "meu-modelo.md").is_file()
    assert [t.id for t in engine.catalog(tmp_path / "modelos")][-1] == "meu-modelo"
    reviewed = store.mark_reviewed("meu-modelo", "  Ana   Souza ")
    assert (reviewed.status, reviewed.reviewed_by) == (
        "revisado",
        "Ana Souza",
    ) and reviewed.reviewed_at
    with pytest.raises(BriefingError, match="nome de quem revisou"):
        store.mark_reviewed("meu-modelo", " ")
    store.delete("meu-modelo")
    assert not (tmp_path / "modelos" / "meu-modelo.md").exists()


def test_store_protects_official_models_and_refuses_invalid_ones(tmp_path: Path) -> None:
    store = engine.ModelStore(tmp_path)
    with pytest.raises(BriefingError, match="é de um modelo oficial"):
        store.save(GOOD.replace("id: meu-modelo", "id: como-se-faz"))
    for action in (
        lambda: store.delete("como-se-faz"),
        lambda: store.mark_reviewed("como-se-faz", "x"),
    ):
        with pytest.raises(BriefingError, match="modelo oficial não se edita"):
            action()
    bad = GOOD.replace("ficha_tela", "ficha_tella")
    with pytest.raises(TemplateError) as raised:
        store.save(bad)
    assert "`ficha_tela`" in raised.value.problems[0].text()
    assert list(tmp_path.glob("*.md")) == []  # nada foi gravado
    with pytest.raises(BriefingError, match="não encontrado"):
        store.delete("nao-existe")


def test_catalog_ignores_broken_user_files_and_user_files_cannot_claim_official(
    tmp_path: Path,
) -> None:
    (tmp_path / "quebrado.md").write_text("lixo", encoding="utf-8")
    (tmp_path / "falso.md").write_text(
        GOOD.replace("id: meu-modelo", "id: como-se-faz"), encoding="utf-8"
    )
    (tmp_path / "oficial-falso.md").write_text(
        GOOD.replace("status: rascunho", "status: oficial"), encoding="utf-8"
    )
    ids = [t.id for t in engine.catalog(tmp_path)]
    assert ids.count("como-se-faz") == 1 and "meu-modelo" not in ids and len(ids) == 9
