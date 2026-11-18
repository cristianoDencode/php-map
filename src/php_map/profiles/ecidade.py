"""Perfil `ecidade`: os valores que viviam em `config.py`, sem alteração.

`config.py` reexporta estes nomes para não quebrar quem já importa de lá; o dono agora é este
módulo. Os dois `ClassResolver` reproduzem, em ordem, o desempate que `resolve/symbols.py`
fazia direto (`_by_autoload`): primeiro a convenção de DAO, depois os sufixos do
autoloader (`.model.php` > `.service.php` > `.interface.php`).
"""

from collections.abc import Mapping, Sequence
from types import MappingProxyType

from php_map.profiles.base import (
    ClassResolver,
    DaoFactory,
    ProjectProfile,
    RemoteCallPattern,
    WsXmlSource,
)

ECIDADE_MARKER = "libs/db_stdlib.php"

DEFAULT_EXCLUDE_DIRS: frozenset[str] = frozenset(
    {"vendor", "node_modules", "tmp", "storage", ".git"}
)

# Infraestrutura (is_infra): excluída por padrão das métricas e do trace.
# INFRA_FUNCTIONS vale para nomes de função e de classe (ex.: `db_utils`, `modification`).
INFRA_FUNCTIONS: frozenset[str] = frozenset(
    {
        "db_query",
        "db_utils",
        "pg_query",
        "pg_exec",
        "db_getsession",
        "db_putsession",
        "db_postmemory",
        "db_inicio_transacao",
        "db_fim_transacao",
        "db_msgbox",
        "db_redireciona",
        "modification",
    }
)
# Funções de banco e prefixos de tabela de infraestrutura.
INFRA_RPC_FUNCTIONS: frozenset[str] = frozenset({"fc_debug"})
INFRA_TABLE_PREFIXES: tuple[str, ...] = ("db_acount",)
INFRA_FILES: frozenset[str] = frozenset(
    {
        "libs/db_stdlib.php",
        "libs/db_conecta.php",
        "libs/db_sessoes.php",
        "libs/db_usuariosonline.php",
        "dbforms/db_funcoes.php",
        "std/Modification.php",
        "libs/db_autoload.php",
    }
)


# Equivalência entre os módulos do dicionário (`db_sysmodulo`) e os do menu (`db_modulos`) que
# têm nomes diferentes: nome normalizado do dicionário → nome normalizado do
# menu. Revisada à mão com as telas e as tabelas de uma instalação real do e-Cidade; os
# módulos que sobram só num cadastro saem no resumo do scan (`catalog.graph`).
MODULE_ALIASES: Mapping[str, str] = MappingProxyType(
    {
        "divida": "dividaativa",
        "fiscal": "auditoriafiscal",
        "licitacao": "licitacoes",
        "recursoshumanos": "rh",
        "configuracoes": "configuracao",
        "teleatend": "teleatendimento",
        "inflatores": "inflat",
        "marcas": "marcasesinais",
        "gestorbi": "gestor",
        "secretariadeeducacao": "secretariaeducacao",
        "prefeitura": "prefeituraonline",
        "issqn": "mercantil",
        "acordos": "contratos",
        "merenda": "alimentacaoescolar",
        "veiculosoff": "veiculos",
    }
)


# `search_path` de uma instalação padrão do e-Cidade (`pg_db_role_setting`), sem `"$user"`
# e sem a repetição de `recursoshumanos`. Resolve as tabelas sem schema no modo arquivo
# (`--schema`); com `--schema-db`, vale o `search_path` da sessão.
ECIDADE_SEARCH_PATH: tuple[str, ...] = (
    "public", "matriculaonline", "recursoshumanos", "veiculosoff", "dbicms", "site", "custos",
    "empenho", "diversos", "issqn", "secretariadeeducacao", "tfd", "meioambiente", "arrecadacao",
    "agua", "ouvidoria", "acordos", "laboratorio", "cadastro", "habitacao", "vacinas",
    "inflatores", "escola", "juridico", "notificacoes", "orcamento", "licitacao", "projetos",
    "divida", "protocolo", "estagioprobatorio", "itbi", "material", "merenda", "pessoal",
    "prefeitura", "agendamento", "dbeduc", "teleatend", "veiculos", "tributario",
    "integracaoexterna", "transito", "dbpref", "farmacia", "conversao", "marcas", "cemiterio",
    "transporteescolar", "ambulatorial", "integracaogissonline", "patrimonio", "gestorbi",
    "caixa", "fiscal", "biblioteca", "configuracoes", "contabilidade", "compras", "ipasem",
    "contrib", "social", "esocial", "planejamento", "assuntosreligiosos", "almoxarifado", "tcerj",
)  # fmt: skip


# os métodos do DAO que gravam o que o código pôs no objeto (`$oDao->campo = …`) ou o
# `$_POST` do formulário (`atualizacampos()`). Precisa bater com `WRITE_METHODS` de
# `php-extractor/src/CollectorVisitor.php`, que só emite `sets` para eles.
DAO_WRITE_METHODS: tuple[str, ...] = ("incluir", "alterar")

DAO_FACTORIES: tuple[DaoFactory, ...] = (
    # libs/db_utils.php: getDao($sClasse, $lInstanciaClasse = true)
    DaoFactory("db_utils", "getdao", "cl_{arg0}", True),
    # classes/empenho.php, model/ordemCompra.model.php...: usarDao($sClasse, $rInstance = false)
    DaoFactory("*", "usardao", "cl_{arg0}", False),
)


_AUTOLOAD_SUFFIXES = (".model.php", ".service.php", ".interface.php")


class _DaoClassResolver:
    """`cl_x` → `classes/db_x_classe.php` (`libs/db_autoload.php`)."""

    def resolve(self, class_name: str, candidates: Sequence[str]) -> str | None:
        paths = self.expected_paths(class_name)
        return paths[0] if paths and paths[0] in candidates else None

    def expected_paths(self, class_name: str) -> tuple[str, ...]:
        short = class_name.rsplit("\\", 1)[-1]
        if not short.startswith("cl_"):
            return ()
        return (f"classes/db_{short[3:]}_classe.php",)


class _SuffixClassResolver:
    """Demais classes do autoloader: `X.model.php` > `.service.php` > `.interface.php`.

    Tenta os sufixos em ordem; o primeiro com exatamente um arquivo decide. Mais de um arquivo
    com o mesmo nome e sufixo (nunca visto no e-cidade até 2026-10-02) fica inconclusivo, e o
    próximo resolvedor (ou a ambiguidade original) decide.
    """

    def resolve(self, class_name: str, candidates: Sequence[str]) -> str | None:
        short = class_name.rsplit("\\", 1)[-1]
        for suffix in _AUTOLOAD_SUFFIXES:
            hits = [c for c in candidates if c.rsplit("/", 1)[-1].lower() == short + suffix]
            if len(hits) == 1:
                return hits[0]
            if hits:
                return None
        return None

    def expected_paths(self, class_name: str) -> tuple[str, ...]:
        short = class_name.rsplit("\\", 1)[-1]
        return tuple(f"{short}{suffix}" for suffix in _AUTOLOAD_SUFFIXES)


CLASS_RESOLVERS: tuple[ClassResolver, ...] = (_DaoClassResolver(), _SuffixClassResolver())


# `requisicao.webservice.php` é o ponto de entrada real do SOAP (`DBWebService`, que
# despacha para `ConsultaDados` e `Processamento`); `DBConsultaDados.webservice.php` é o servidor
# da consulta que a tarefa nomeou. Os dois ligam ao endpoint, se existirem entre os arquivos.
_SOAP_ENTRY = "webservices/requisicao.webservice.php"
WS_XML_SOURCES: tuple[WsXmlSource, ...] = (
    WsXmlSource(
        "soap_consulta",
        "webservices/consulta_dados/requisicoes",
        "consulta",
        ("webservices/consulta_dados/DBConsultaDados.webservice.php", _SOAP_ENTRY),
    ),
    WsXmlSource(
        "soap_processamento",
        "webservices/processamento_dados/requisicoes",
        "processamento",
        (_SOAP_ENTRY,),
    ),
)
# `modelo.1.0.xml` e `modelo.1.2.xml` são modelos de como escrever uma requisição, não requisições.
WS_XML_IGNORE_PREFIXES: tuple[str, ...] = ("modelo.",)
REMOTE_CALLS: tuple[RemoteCallPattern, ...] = (
    RemoteCallPattern("integracaonfsehelper", frozenset({"request"}), "zend1app"),
)

PROFILE = ProjectProfile(
    name="ecidade",
    markers=(ECIDADE_MARKER,),
    source_extensions=frozenset({".php"}),
    exclude_dirs=DEFAULT_EXCLUDE_DIRS,
    third_party_dirs=(),
    encoding="latin-1",
    infra_functions=INFRA_FUNCTIONS,
    infra_files=INFRA_FILES,
    infra_rpc_functions=INFRA_RPC_FUNCTIONS,
    infra_table_prefixes=INFRA_TABLE_PREFIXES,
    dao_factories=DAO_FACTORIES,
    dao_write_methods=DAO_WRITE_METHODS,
    class_resolvers=CLASS_RESOLVERS,
    module_source="catalog",
    # o e-cidade serve SOAP pelos XML de requisição (um endpoint por
    # arquivo) e chama o outro sistema pelo `IntegracaoNfseHelper::request('<Nome>', ...)`. O
    # `setClass("ConsultaDados")` do servidor é só despacho: não vira endpoint por método.
    endpoint_system="ecidade",
    ws_xml=WS_XML_SOURCES,
    ws_xml_ignore_prefixes=WS_XML_IGNORE_PREFIXES,
    remote_calls=REMOTE_CALLS,
    # os `*.RPC.php` despacham por `switch ($oParam->exec)` e o Laravel (`routes/`,
    # montado pelo `RouteServiceProvider`) tem a árvore de rotas espalhada em vários arquivos.
    rpc_endpoints=True,
    laravel_routes=True,
    reads_eloquent=True,
    module_aliases=MODULE_ALIASES,
    default_search_path=ECIDADE_SEARCH_PATH,
)
