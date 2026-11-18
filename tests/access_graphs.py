"""Mapa de teste de «quem grava e quem lê a tabela no PHP», sobre `field_graphs.py`.

Os fatos vêm do `out/scan60` (conferidos em 2026-11-07), em miniatura, para
`protocolo.cgmtipoempresa`:

- o DAO `cl_cgmtipoempresa` (`incluir`, `alterar`, `excluir`, `sql_query`) em
  `classes/db_cgmtipoempresa_classe.php`;
- **gravam pelo DAO:** `prot1_cadgeralmunic.RPC.php` (arquivo com menu: `incluir` linha 725 e
  `excluir` 716); `RedeSimImportaService::processarcadastroempresa` (método com **2 chamadas** a
  `incluir`, linhas 2687 e 2974, de outro namespace; ganha menu pela subida das camadas, via
  `iss4_redesim.php`); `Repository::persistcgm` (`alterar` 64 e `incluir` 68, ninguém o chama) e
  `CgmBase::exclui` (`excluir` 880, chamado por um arquivo sem menu);
- **SQL direto:** `duplos.php` (`DELETES`, sem menu);
- **migrações** (`db/migrations/` do Ruckusing e `database/migrations/` do Laravel): ficam fora
  da produção;
- **Eloquent:** o model `CgmTipoEmpresa` (`MAPS_TO`) é instanciado pelo `CgmController::index`;
- **lê:** `func_cgmtipoempresa.php` (`sql_query`, 4 chamadas), `emp4_x.RPC.php` (com menu),
  `CgmBase::gettipoempresa` e `forms/db_frmissbasealt.php` (SQL direto);
- ruído que **não** pode aparecer: o DAO chamando a si mesmo (`incluir_lote` → `incluir`), um
  método do DAO e o arquivo `classes/db_*_classe.php` com aresta para a tabela.

`build_db(with_fills=True)` grava também a `php_field_writes` com os fatos do
e-cidade real: o RedeSim (`z03_tipoempresa` = `32` na 2686 e `$iTpEmpresaSocio` na 2973, no mesmo
método), o RPC (`chamada` e `expressao`) e a tela `cai1_recejm001.php` de `caixa.tabrecjm` com o
formulário `forms/db_frmrecejm.php` (`k02_juros` na linha 79).

`protocolo.vazia` não tem nenhum toque PHP. `build_db(extra_writers=N, extra_readers=M)` junta N
arquivos que gravam por SQL direto sem menu e M que leem (para o corte e o limite de subidas).
"""

import sqlite3
from collections.abc import Mapping
from contextlib import closing
from pathlib import Path
from typing import Any

from cross_graphs import save as _save
from field_graphs import (
    GRAVA,
    LEITOR,
    PROJECT,
    build_builder_with_tables,
    functions,
    statements,
    write_texts,
)

from php_map.graph import field_writes as fw
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import Edge, EdgeSource, EdgeType, Node, NodeLabel

CGMTIPO = "Table:protocolo.cgmtipoempresa"
VAZIA = "Table:protocolo.vazia"
DAO_FILE = "classes/db_cgmtipoempresa_classe.php"
DAO_CLASS = "Class:cl_cgmtipoempresa"
INCLUIR = "Method:cl_cgmtipoempresa::incluir"
ALTERAR = "Method:cl_cgmtipoempresa::alterar"
EXCLUIR = "Method:cl_cgmtipoempresa::excluir"
SQL_QUERY = "Method:cl_cgmtipoempresa::sql_query"

CADASTRO = "prot1_cadgeralmunic.RPC.php"
REDESIM_ENTRY = "iss4_redesim.php"
SERVICE_PATH = "app/Domain/Tributario/ISSQN/Services/RedeSim/RedeSimImportaService.php"
SERVICE_CLASS = "app\\domain\\tributario\\issqn\\services\\redesim\\redesimimportaservice"
SERVICE = f"Method:{SERVICE_CLASS}::processarcadastroempresa"
REPOSITORY_PATH = "src/Tributario/Integracao/JuntaComercial/Repository.php"
REPOSITORY = "Method:ecidade\\tributario\\integracao\\juntacomercial\\repository::persistcgm"
CGMBASE_PATH = "model/CgmBase.model.php"
CGMBASE_EXCLUI = "Method:cgmbase::exclui"
CGMBASE_GET = "Method:cgmbase::gettipoempresa"
CGM_CALLER = "cgm_chama_exclui.php"
MUITOS = "Method:muitos::grava"
DUPLOS = "duplos.php"
MIGRATION = "db/migrations/20180710202831_acerto11178_cgm_tipo_empresa_duplicado.php"
LARAVEL_MIGRATION = "database/migrations/2024_04_01_082830_t54013_erro_ao_emitir.php"
LARAVEL_MIGRATION_UP = "Method:t54013erroaoemitir::up"
MIGRATION_UP = "Method:acerto11178cgmtipoempresaduplicado::up"
MIGRATION_DOWN = "Method:acerto11178cgmtipoempresaduplicado::down"
FUNC_FILE = "func_cgmtipoempresa.php"
EMP4 = "emp4_tipoaquisicaoproducaorural.RPC.php"
FORM = "forms/db_frmissbasealt.php"
MODEL_PATH = "app/Models/CgmTipoEmpresa.php"
MODEL_CLASS = "Class:app\\models\\cgmtipoempresa"
MODEL_SCOPE = "Method:app\\models\\cgmtipoempresa::scopeativos"
CONTROLLER_PATH = "app/Http/Controllers/CgmController.php"
CONTROLLER = "Method:app\\http\\controllers\\cgmcontroller::index"

MENU_CADASTRO = "Caixa > Procedimentos > Cadastro de Órgão Público"
MENU_REDESIM = "Mercantil > Procedimentos > REDESIM > Importação Manual"
MENU_EMP4 = "Empenho > Procedimentos > Manutenção das Retenções"


def _node(builder: GraphBuilder, node_id: str, label: NodeLabel, **attrs: Any) -> None:
    builder.add_node(Node(node_id, label, {"name": node_id.partition(":")[2], **attrs}))


def _file(builder: GraphBuilder, path: str) -> str:
    node_id = f"File:{path}"
    _node(builder, node_id, NodeLabel.FILE, path=path, name=path)
    return node_id


def _method(builder: GraphBuilder, node_id: str, path: str, cls: str | None = None) -> str:
    key = node_id.partition(":")[2]
    owner, _sep, name = key.partition("::")
    _node(builder, node_id, NodeLabel.METHOD, path=path, name=name, **{"class": cls or owner})
    return node_id


def _edge(
    builder: GraphBuilder,
    src: str,
    dst: str,
    kind: EdgeType,
    path: str,
    line: int,
    *,
    source: EdgeSource = EdgeSource.PHP_AST,
    confidence: float = 1.0,
    **evidence: Any,
) -> None:
    builder.add_edge(
        Edge(src, dst, kind, source, confidence, {"path": path, "line": line, **evidence})
    )


def _menu(builder: GraphBuilder, number: int, path: str | None, entry: str) -> None:
    menu = f"BusinessAction:menu:{number}"
    attrs: dict[str, Any] = {"name": "Item", "active": True}
    if path:
        attrs |= {"menu_path": path, "menu_paths": [path]}
    builder.add_node(Node(menu, NodeLabel.BUSINESS_ACTION, attrs))
    builder.add_edge(
        Edge(
            entry,
            menu,
            EdgeType.IMPLEMENTS_ACTION,
            EdgeSource.ECIDADE_CATALOG,
            1.0,
            {"path": entry.partition(":")[2], "line": 1},
        )
    )


def build_builder(*, extra_writers: int = 0, extra_readers: int = 0) -> GraphBuilder:
    b = build_builder_with_tables(0)
    columns = {"z03_sequencial": "Código", "z03_numcgm": "Código CGM", "z03_tipoempresa": None}
    for table_id, label in ((CGMTIPO, "Cgm Tipo Empresa"), (VAZIA, "Tabela vazia")):
        name = table_id.partition(".")[2]
        _node(
            b,
            table_id,
            NodeLabel.TABLE,
            name=name,
            schema="protocolo",
            columns=list(columns),
            columns_catalog={
                c: {"label": lbl, "description": lbl} for c, lbl in columns.items() if lbl
            },
            catalog={"label": label, "description": f"Liga cgm e tipoempresa ({name})"},
        )

    # O DAO e o ruído que não pode aparecer.
    dao_file = _file(b, DAO_FILE)
    _node(b, DAO_CLASS, NodeLabel.CLASS, path=DAO_FILE, declared_in=[DAO_FILE])
    for method, kind, line, source, conf in (
        (INCLUIR, EdgeType.INSERTS, 132, EdgeSource.SQL_AST, 1.0),
        (ALTERAR, EdgeType.UPDATES, 181, EdgeSource.SQL_REGEX, 0.7),
        (EXCLUIR, EdgeType.DELETES, 291, EdgeSource.SQL_AST, 1.0),
        (SQL_QUERY, EdgeType.QUERIES, 361, EdgeSource.SQL_AST, 1.0),
    ):
        _method(b, method, DAO_FILE)
        _edge(b, method, CGMTIPO, kind, DAO_FILE, line, source=source, confidence=conf)
        _edge(b, DAO_CLASS, method, EdgeType.DECLARES, DAO_FILE, line)
    _edge(b, dao_file, DAO_CLASS, EdgeType.DECLARES, DAO_FILE, 1)
    _edge(b, dao_file, CGMTIPO, EdgeType.INSERTS, DAO_FILE, 10, source=EdgeSource.SQL_AST)
    lote = _method(b, "Method:cl_cgmtipoempresa::incluir_lote", DAO_FILE)
    _edge(b, lote, INCLUIR, EdgeType.CALLS, DAO_FILE, 140)  # o DAO chamando a si mesmo

    # Arquivo com menu: `incluir` (725) e `excluir` (716).
    cadastro = _file(b, CADASTRO)
    _edge(b, cadastro, INCLUIR, EdgeType.CALLS, CADASTRO, 725, via="dao_factory:getdao")
    _edge(b, cadastro, EXCLUIR, EdgeType.CALLS, CADASTRO, 716, via="dao_factory:getdao")
    _edge(b, cadastro, SQL_QUERY, EdgeType.CALLS, CADASTRO, 709, via="dao_factory:getdao")
    _menu(b, 101, MENU_CADASTRO, cadastro)

    # Método com 2 chamadas; o menu vem da subida das camadas (iss4_redesim.php o chama).
    _method(b, SERVICE, SERVICE_PATH, SERVICE_CLASS)
    _edge(b, SERVICE, INCLUIR, EdgeType.CALLS, SERVICE_PATH, 2687, via="dao_factory:getdao")
    _edge(b, SERVICE, INCLUIR, EdgeType.CALLS, SERVICE_PATH, 2974, via="dao_factory:getdao")
    service_file = _file(b, SERVICE_PATH)
    _edge(b, service_file, SERVICE, EdgeType.DECLARES, SERVICE_PATH, 2546)
    entry = _file(b, REDESIM_ENTRY)
    _edge(b, entry, SERVICE, EdgeType.CALLS, REDESIM_ENTRY, 30)
    _menu(b, 102, MENU_REDESIM, entry)

    # Método que ninguém chama (alterar 64, incluir 68): motivo «nenhum fonte chama».
    _method(b, REPOSITORY, REPOSITORY_PATH)
    _edge(b, REPOSITORY, ALTERAR, EdgeType.CALLS, REPOSITORY_PATH, 64)
    _edge(b, REPOSITORY, INCLUIR, EdgeType.CALLS, REPOSITORY_PATH, 68)
    _edge(b, REPOSITORY, SQL_QUERY, EdgeType.CALLS, REPOSITORY_PATH, 53)

    # Método chamado por um arquivo sem menu: motivo «nenhum menu chega».
    _method(b, CGMBASE_EXCLUI, CGMBASE_PATH)
    _method(b, CGMBASE_GET, CGMBASE_PATH)
    _edge(b, CGMBASE_EXCLUI, EXCLUIR, EdgeType.CALLS, CGMBASE_PATH, 880)
    _edge(b, CGMBASE_GET, SQL_QUERY, EdgeType.CALLS, CGMBASE_PATH, 829)
    caller = _file(b, CGM_CALLER)
    _edge(b, caller, CGMBASE_EXCLUI, EdgeType.CALLS, CGM_CALLER, 12)

    # Funções da base (PL/pgSQL) que gravam e leem a tabela: contam à parte, nunca como PHP.
    _edge(
        b, GRAVA, CGMTIPO, EdgeType.INSERTS, "public.fc_grava_valor", 6, source=EdgeSource.SQL_AST
    )
    _edge(
        b, LEITOR, CGMTIPO, EdgeType.QUERIES, "public.fc_leitor_valor", 7, source=EdgeSource.SQL_AST
    )

    # Mais chamadas do que o grafo guarda de locais (`MAX_SAMPLE_LINES` = 5): «+2 chamadas».
    _method(b, MUITOS, "muitas.php")
    for line in range(1, 8):
        _edge(b, MUITOS, INCLUIR, EdgeType.CALLS, "muitas.php", line)

    # SQL direto de arquivo, sem menu, e a leitura de um formulário.
    duplos = _file(b, DUPLOS)
    _edge(b, duplos, CGMTIPO, EdgeType.DELETES, DUPLOS, 1139, source=EdgeSource.SQL_AST)
    form = _file(b, FORM)
    _edge(b, form, CGMTIPO, EdgeType.QUERIES, FORM, 220, source=EdgeSource.SQL_AST)

    # Migração: up apaga e insere, down insere; uma leitura em migração não conta.
    _file(b, MIGRATION)
    for method in (MIGRATION_UP, MIGRATION_DOWN):
        _method(b, method, MIGRATION)
    _edge(b, MIGRATION_UP, CGMTIPO, EdgeType.DELETES, MIGRATION, 35, source=EdgeSource.SQL_AST)
    _edge(b, MIGRATION_UP, CGMTIPO, EdgeType.INSERTS, MIGRATION, 38, source=EdgeSource.SQL_AST)
    _edge(b, MIGRATION_DOWN, CGMTIPO, EdgeType.QUERIES, MIGRATION, 50, source=EdgeSource.SQL_AST)

    # Migração do Laravel (`database/migrations/`): também fora da produção.
    _file(b, LARAVEL_MIGRATION)
    _method(b, LARAVEL_MIGRATION_UP, LARAVEL_MIGRATION)
    _edge(
        b, LARAVEL_MIGRATION_UP, CGMTIPO, EdgeType.UPDATES, LARAVEL_MIGRATION, 67,
        source=EdgeSource.SQL_AST,
    )  # fmt: skip

    # Leitores pelo DAO: 4 chamadas no arquivo da busca, um arquivo com menu.
    func = _file(b, FUNC_FILE)
    for line in (69, 71, 73, 82):
        _edge(b, func, SQL_QUERY, EdgeType.CALLS, FUNC_FILE, line, via="dao_factory:usardao")
    emp4 = _file(b, EMP4)
    _edge(b, emp4, SQL_QUERY, EdgeType.CALLS, EMP4, 52)
    _menu(b, 103, MENU_EMP4, emp4)

    # Eloquent: o model, um método dele e quem o usa. O model chamando a si mesmo não conta.
    _file(b, MODEL_PATH)
    _node(b, MODEL_CLASS, NodeLabel.CLASS, path=MODEL_PATH)
    _method(b, MODEL_SCOPE, MODEL_PATH)
    _edge(b, MODEL_CLASS, MODEL_SCOPE, EdgeType.DECLARES, MODEL_PATH, 20)
    _edge(b, f"File:{MODEL_PATH}", MODEL_CLASS, EdgeType.DECLARES, MODEL_PATH, 5)
    _edge(
        b,
        MODEL_CLASS,
        CGMTIPO,
        EdgeType.MAPS_TO,
        MODEL_PATH,
        5,
        source=EdgeSource.ELOQUENT_MODEL,
    )
    _method(b, CONTROLLER, CONTROLLER_PATH)
    _edge(b, CONTROLLER, MODEL_CLASS, EdgeType.INSTANTIATES, CONTROLLER_PATH, 33)
    _edge(b, CONTROLLER, MODEL_SCOPE, EdgeType.CALLS, CONTROLLER_PATH, 34)
    own = _method(b, "Method:app\\models\\cgmtipoempresa::ativo", MODEL_PATH)
    _edge(b, own, MODEL_SCOPE, EdgeType.CALLS, MODEL_PATH, 25)

    for n in range(extra_writers):
        path = f"grava{n:03d}.php"
        node = _file(b, path)
        _edge(b, node, CGMTIPO, EdgeType.UPDATES, path, 10, source=EdgeSource.SQL_AST)
    for n in range(extra_readers):
        path = f"func_lista{n:03d}.php"
        node = _file(b, path)
        _edge(b, node, CGMTIPO, EdgeType.QUERIES, path, 10, source=EdgeSource.SQL_AST)
    return b


TABRECJM_SCREEN = "cai1_recejm001.php"
TABRECJM_FORM = "forms/db_frmrecejm.php"
TABRECJM_INCLUIR = "Method:cl_tabrecjm::incluir"
TABREC = "Table:caixa.tabrecjm"


def fill_rows() -> list[fw.FieldWriteRow]:
    """Os fatos de `php_field_writes` do mapa de teste."""

    def row(
        src: str,
        dao: str,
        table: str,
        column: str,
        how: str,
        value: str,
        kind: str,
        path: str,
        line: int,
        call_line: int,
        confidence: float = 1.0,
    ) -> fw.FieldWriteRow:
        return fw.FieldWriteRow(
            src, dao, table, column, how, value, kind, path, line, call_line, confidence
        )

    a, f = fw.HOW_ASSIGN, fw.HOW_FORM
    rows = [
        # RedeSim: o mesmo DAO recriado; as duas linhas de `z03_tipoempresa` no mesmo método
        row(
            SERVICE, INCLUIR, CGMTIPO, "z03_sequencial", a, "null", "null", SERVICE_PATH, 2684, 2687
        ),
        row(
            SERVICE,
            INCLUIR,
            CGMTIPO,
            "z03_numcgm",
            a,
            "$iCodCgmEmpresa",
            "variavel",
            SERVICE_PATH,
            2685,
            2687,
        ),
        row(
            SERVICE,
            INCLUIR,
            CGMTIPO,
            "z03_tipoempresa",
            a,
            "32",
            "literal",
            SERVICE_PATH,
            2686,
            2687,
        ),
        row(
            SERVICE,
            INCLUIR,
            CGMTIPO,
            "z03_numcgm",
            a,
            "$iCodCgmSocio",
            "variavel",
            SERVICE_PATH,
            2972,
            2974,
        ),
        row(
            SERVICE,
            INCLUIR,
            CGMTIPO,
            "z03_tipoempresa",
            a,
            "$iTpEmpresaSocio",
            "variavel",
            SERVICE_PATH,
            2973,
            2974,
        ),
        # RPC (JSON, sem formulário)
        row(
            f"File:{CADASTRO}",
            INCLUIR,
            CGMTIPO,
            "z03_numcgm",
            a,
            "$oCgm->getCodigo()",
            "chamada",
            CADASTRO,
            723,
            725,
        ),
        row(
            f"File:{CADASTRO}",
            INCLUIR,
            CGMTIPO,
            "z03_tipoempresa",
            a,
            "$oParam->tipoEmpresa->iTipoEmpresa",
            "expressao",
            CADASTRO,
            724,
            725,
        ),
        # Tela procedural de `caixa.tabrecjm` com o formulário incluído (o caso do P56)
        row(
            f"File:{TABRECJM_SCREEN}",
            TABRECJM_INCLUIR,
            TABREC,
            "k02_corr",
            f,
            '<select name="k02_corr">',
            "formulario",
            TABRECJM_FORM,
            47,
            16,
            0.8,
        ),
        row(
            f"File:{TABRECJM_SCREEN}",
            TABRECJM_INCLUIR,
            TABREC,
            "k02_juros",
            f,
            "db_input('k02_juros')",
            "formulario",
            TABRECJM_FORM,
            79,
            16,
            0.8,
        ),
        row(
            f"File:{TABRECJM_SCREEN}",
            TABRECJM_INCLUIR,
            TABREC,
            "k02_codjm",
            f,
            "db_input('k02_codjm')",
            "formulario",
            TABRECJM_FORM,
            35,
            16,
            0.8,
        ),
    ]
    return rows


def build_db(
    tmp_path: Path,
    name: str = "acesso",
    *,
    extra_writers: int = 0,
    extra_readers: int = 0,
    with_texts: bool = False,
    with_fills: bool = False,
    meta: Mapping[str, str] | None = None,
) -> Path:
    """`with_texts`: grava também o texto e as consultas das funções (o dossiê de campo exige).
    `with_fills`: grava a `php_field_writes` (sem isso o mapa é de um scan antigo)."""
    builder = build_builder(extra_writers=extra_writers, extra_readers=extra_readers)
    db = _save(tmp_path, name, builder, {"project": PROJECT, "source": "x", **(meta or {})})
    if with_texts:
        write_texts(db, functions(), statements())
    if with_fills:
        with closing(sqlite3.connect(db)) as conn:
            fw.store_field_writes(conn, fill_rows())
    return db


__all__ = ["CGMTIPO", "PROJECT", "TABREC", "VAZIA", "build_db", "fill_rows"]
