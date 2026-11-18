"""Ficha de regras: categoria heurística das mensagens e conversão escopo ↔ nó."""

from typing import Any

import pytest

from php_map.web.repo import NodeRow
from php_map.web.rules import category, node_scope, scope_node, text_files


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("Não há saldo disponível para anular.", "validação"),
        ("Nao ha saldo disponivel para anular.", "validação"),
        ("NÃO HÁ SALDO", "validação"),
        ("Informe o número do empenho", "validação"),
        ("Selecione ao menos um item", "validação"),
        ("Campo obrigatório", "validação"),
        ("CAMPO OBRIGATORIO", "validação"),
        ("CPF inválido", "validação"),
        ("Data invalida", "validação"),
        ("Registro já existe", "validação"),
        ("Registro ja existe", "validação"),
        ("O valor deve ser maior que zero", "validação"),
        ("Todos os itens estao em ordem de compra ou anulados. Não podera ser feita", "validação"),
        ("Erro ao fazer a anulação", "erro"),
        ("ERRO AO FAZER A ANULACAO", "erro"),
        ("Falha na conexão", "erro"),
        ("FALHA NA CONEXAO", "erro"),
        ("') nao Incluído. Inclusao Abortada.'", "erro"),  # erro vence validação
        ("Aguarde, efetuando pesquisa", "mensagem"),
        ("Dados Empenho:", "mensagem"),
        ("Contribuinte devedor", "mensagem"),  # "deve" só como palavra inteira
        ("Solicitação de Anulação", "mensagem"),  # "nao" não casa dentro de palavra
        ("", "mensagem"),
    ],
)
def test_category(text: str, expected: str) -> None:
    assert category(text) == expected


@pytest.mark.parametrize(
    ("node_id", "scope"),
    [
        ("Method:empenho::anularempenho", "method:empenho::anularempenho"),
        ("Function:db_query", "function:db_query"),
        ("File:emp4_liquidacao004.php", ""),
        ("Table:public.empanulado", None),
        ("Class:empenho", None),
    ],
)
def test_node_scope(node_id: str, scope: str | None) -> None:
    assert node_scope(node_id) == scope


@pytest.mark.parametrize(
    ("path", "scope", "node_id"),
    [
        ("classes/empenho.php", "method:empenho::anularempenho", "Method:empenho::anularempenho"),
        ("classes/empenho.php", "method:Empenho::AnularEmpenho", "Method:empenho::anularempenho"),
        ("libs/db_stdlib.php", "function:db_query", "Function:db_query"),
        ("forms/db_frmempanularempenho.php", "", "File:forms/db_frmempanularempenho.php"),
    ],
)
def test_scope_node(path: str, scope: str, node_id: str) -> None:
    assert scope_node(path, scope) == node_id
    if scope == scope.lower():
        assert node_scope(node_id) == scope  # ida e volta


COPIES = ["libs/db_stdlib.php", "libs/db_stdlibsaude.php", "libs/sd.php"]


@pytest.mark.parametrize(
    ("node_id", "attrs", "evidence", "files"),
    [
        ("File:forms/db_frm.php", {"path": "forms/db_frm.php"}, {}, ["forms/db_frm.php"]),
        # a aresta escolheu o declarante: as cópias ficam de fora (caso real de `db_lovrot`)
        (
            "Function:db_lovrot",
            {"path": COPIES[1], "declared_in": COPIES},
            {"path": "func_empempenho.php", "line": 3, "declared_in": "libs/db_stdlib.php"},
            ["libs/db_stdlib.php"],
        ),
        # ambígua (1/n, sem declarante escolhido): nenhum texto
        ("Function:db_input", {"path": COPIES[0], "declared_in": COPIES[:2]}, {"line": 9}, []),
        (
            "Method:empenho::anularempenho",
            {"path": "classes/empenho.php"},
            {},
            ["classes/empenho.php"],
        ),
        ("Class:empenho", {"path": "classes/empenho.php"}, {}, []),
    ],
)
def test_text_files(
    node_id: str, attrs: dict[str, Any], evidence: dict[str, Any], files: list[str]
) -> None:
    row = NodeRow(node_id, node_id.partition(":")[0], attrs, [])
    assert text_files(row, evidence) == files
