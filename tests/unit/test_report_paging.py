"""`report/paging.py` — divide o Markdown inteiro de um dossiê em páginas de
~`max_words` palavras, nas fronteiras de `## `, sem nunca perder conteúdo."""

from php_map.report import paging


def _table(rows: int) -> list[str]:
    lines = ["| Fonte | Valor |", "|---|---|"]
    lines += [f"| linha{i} | valor{i} |" for i in range(rows)]
    return lines


def _small_section(title: str, marker: str, lines: int = 4, words: int = 4) -> list[str]:
    body = [f"## {title}", ""]
    body += [
        f"{marker} linha {i} " + " ".join(f"p{i}{j}" for j in range(words)) for i in range(lines)
    ]
    body.append("")
    return body


def test_breaks_on_section_boundaries_without_tearing_a_small_section() -> None:
    markdown = "\n".join(
        [
            "# Título",
            "",
            "> abertura",
            "",
            *_small_section("Primeira", "PRIM"),
            *_small_section("Segunda", "SEG"),
            *_small_section("Terceira", "TERC"),
        ]
    )
    pages = paging.paginate(markdown, max_words=50)
    assert len(pages) > 1
    by_title = {title: page for page in pages for title in page.sections}
    for title, marker in (("Primeira", "PRIM"), ("Segunda", "SEG"), ("Terceira", "TERC")):
        own_page = by_title[title]
        assert title not in {t for p in pages if p is not own_page for t in p.sections}
        assert marker in own_page.markdown
        for other in pages:
            if other is not own_page:
                assert marker not in other.markdown


def test_big_section_is_broken_by_line_with_a_continuation_title() -> None:
    body = [f"linha {i} palavra extra aqui" for i in range(40)]
    markdown = "\n".join(["# Título", "", "## Grande", "", *body, ""])
    pages = paging.paginate(markdown, max_words=paging.MIN_MAX_WORDS)
    assert len(pages) > 1
    all_titles = [title for page in pages for title in page.sections]
    assert "Grande" in all_titles
    assert any(title == "Grande (continuação)" for title in all_titles)
    # nada se perde e nada se repete: cada linha aparece em exatamente uma página
    full_text = "\n".join(page.markdown for page in pages)
    for line in body:
        assert full_text.count(line) == 1


def test_a_broken_table_repeats_the_two_header_lines() -> None:
    markdown = "\n".join(["# Título", "", "## Tabela", "", *_table(20), ""])
    pages = paging.paginate(markdown, max_words=paging.MIN_MAX_WORDS)
    assert len(pages) > 1
    header_occurrences = sum(page.markdown.count("| Fonte | Valor |") for page in pages)
    assert header_occurrences >= 2
    sep_occurrences = sum(page.markdown.count("|---|---|") for page in pages)
    assert sep_occurrences == header_occurrences
    # nenhuma linha de dado se perde ou se repete
    full_text = "\n".join(page.markdown for page in pages)
    for i in range(20):
        assert full_text.count(f"| linha{i} | valor{i} |") == 1


def test_a_fenced_code_block_stays_intact_on_a_single_page() -> None:
    code_lines = [f"codigo{i}" for i in range(60)]
    markdown = "\n".join(
        [
            "# Título",
            "",
            "## Código",
            "",
            "```",
            *code_lines,
            "```",
            "",
            "## Depois",
            "",
            "fim conteúdo extra para garantir mais texto aqui também sim",
            "",
        ]
    )
    pages = paging.paginate(markdown, max_words=paging.MIN_MAX_WORDS)
    assert len(pages) > 1
    fence_pages = [page for page in pages if "```" in page.markdown]
    assert len(fence_pages) == 1
    page = fence_pages[0]
    assert page.markdown.count("```") == 2
    for line in code_lines:
        assert line in page.markdown


def test_overview_and_title_always_stay_on_page_one() -> None:
    big_sections = []
    for s in range(6):
        big_sections += [f"## Seção {s}", ""]
        big_sections += [f"conteúdo da seção {s} linha {i} com texto" for i in range(15)]
        big_sections += [""]
    markdown = "\n".join(
        ["# Dossiê de cálculo: algo", "", "## Visão geral", "", "resumo curto", "", *big_sections]
    )
    pages = paging.paginate(markdown, max_words=paging.MIN_MAX_WORDS)
    assert len(pages) > 1
    first = pages[0]
    assert "# Dossiê de cálculo: algo" in first.markdown
    assert "## Visão geral" in first.markdown
    assert "resumo curto" in first.markdown
    assert first.sections[0] == "Visão geral"
    for page in pages[1:]:
        assert "## Visão geral" not in page.markdown


def test_pagination_is_deterministic() -> None:
    markdown = "\n".join(
        ["# Título", "", "## A", "", *(f"linha {i}" for i in range(50)), "", "## B", "", "x"]
    )
    first = paging.paginate(markdown, max_words=80)
    second = paging.paginate(markdown, max_words=80)
    assert first == second
    assert paging.section_index(first) == paging.section_index(second)


def test_max_words_has_a_floor() -> None:
    markdown = "\n".join(
        ["# Título", "", "## A", "", *(f"linha {i}" for i in range(20)), "", "## B", "", "x"]
    )
    floored = paging.paginate(markdown, max_words=paging.MIN_MAX_WORDS)
    assert paging.paginate(markdown, max_words=0) == floored
    assert paging.paginate(markdown, max_words=-100) == floored


def test_section_index_lists_every_title_with_its_page() -> None:
    markdown = "\n".join(
        [
            "# Título",
            "",
            "## Um",
            "",
            "a",
            "",
            "## Dois",
            "",
            "b",
            "",
        ]
    )
    pages = paging.paginate(markdown, max_words=3000)
    assert len(pages) == 1
    index = paging.section_index(pages)
    assert index == [{"title": "Um", "page": 1}, {"title": "Dois", "page": 1}]


def test_small_document_fits_in_a_single_page() -> None:
    markdown = "# Título\n\nconteúdo pequeno\n"
    pages = paging.paginate(markdown, max_words=3000)
    assert len(pages) == 1
    assert pages[0].number == 1 and pages[0].total == 1
    assert pages[0].sections == ()
    assert pages[0].markdown.strip() == markdown.strip()
