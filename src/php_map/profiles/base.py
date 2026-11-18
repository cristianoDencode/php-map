"""Perfil de projeto: o que é específico de cada sistema mapeado.

Um `ProjectProfile` reúne tudo que hoje é só do e-cidade (marcador da pasta, extensões,
exclusões, encoding, infraestrutura, fábricas de DAO, desempate de classe por convenção, origem
do módulo e `search_path`). `ecidade` é o padrão; `zend1` é o dos projetos Zend Framework 1. A
detecção automática (`suggest_profile`) é só para a interface: a CLI nunca adivinha.
"""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Protocol


@dataclass(frozen=True, slots=True)
class DaoFactory:
    """Fábrica de DAO que instancia via `eval` (invisível na AST).

    `receiver` é a classe do receptor da chamada (`db_utils` em `db_utils::getDao`) ou `"*"`
    para qualquer receptor (`$this->usarDao`). `instantiates_by_default` é o valor padrão do
    2º parâmetro (instanciar ou só fazer o `require`).
    """

    receiver: str
    method: str
    class_template: str
    instantiates_by_default: bool


@dataclass(frozen=True, slots=True)
class RemoteCallPattern:
    """Chamada estática que dispara um endpoint de **outro** sistema.

    `WebService_Model_Ecidade::consultar('getCgm', ...)` no zend1: classe cujo nome curto termina
    em `class_suffix` (minúsculas), método em `methods` e **1º argumento literal** = nome do
    endpoint. `target_system` é quem serve (entra no ID do `Endpoint`).
    """

    class_suffix: str
    methods: frozenset[str]
    target_system: str
    protocol: str = "soap"


@dataclass(frozen=True, slots=True)
class WsXmlSource:
    """Pasta de XML de requisição de webservice (e-cidade): um endpoint por arquivo.

    `kind` (`soap_consulta`/`soap_processamento`), `root_tag` (`consulta`/`processamento`: o
    arquivo cuja raiz é outra não é requisição) e `servers`, os arquivos PHP que despacham a
    requisição (`SERVES` de cada um para o endpoint, se existirem entre os arquivos escaneados).
    """

    kind: str
    directory: str
    root_tag: str
    servers: tuple[str, ...] = ()


class ClassResolver(Protocol):
    """Desempate de classe ambígua por convenção de nomes.

    Tentados em ordem (`ProjectProfile.class_resolvers`) quando o índice global de símbolos tem
    mais de um declarante para o mesmo nome. O primeiro que devolver um caminho decide; nenhum
    decidindo, a ambiguidade original (`PHP_AST_AMBIGUOUS`, `1/n`) continua valendo.
    """

    def resolve(self, class_name: str, candidates: Sequence[str]) -> str | None:
        """Entre os `candidates` (caminhos que declaram `class_name`), o único que esta
        convenção escolhe; `None` se a convenção não se aplica ou não desempata para um só."""
        ...

    def expected_paths(self, class_name: str) -> tuple[str, ...]:
        """Caminho(s) que a convenção prevê para `class_name`, exista o arquivo ou não (usado
        para apontar convenção que não achou arquivo nenhum)."""
        ...


@dataclass(frozen=True, slots=True)
class ProjectProfile:
    """Contrato de um perfil de projeto. `name` é o valor aceito por `--profile`."""

    name: str
    # Marcador que `validate_source` exige (o 1º); os demais são só confirmação (sugestão da
    # interface, `suggest_profile`), não entram na validação da CLI.
    markers: tuple[str, ...]
    source_extensions: frozenset[str]
    exclude_dirs: frozenset[str]
    # Diretórios de terceiros: arquivo dentro de um deles entra como `File.is_infra = True`.
    third_party_dirs: tuple[str, ...] = ()
    encoding: str = "latin-1"
    infra_functions: frozenset[str] = frozenset()
    infra_files: frozenset[str] = frozenset()
    infra_rpc_functions: frozenset[str] = frozenset()
    infra_table_prefixes: tuple[str, ...] = ()
    dao_factories: tuple[DaoFactory, ...] = ()
    # métodos do DAO que gravam os campos preenchidos no objeto; vazio = sem
    # `php_field_writes` (o `zend1` não tem DAO assim).
    dao_write_methods: tuple[str, ...] = ()
    class_resolvers: tuple[ClassResolver, ...] = ()
    # alguns resolvedores precisam ler algo da própria pasta escaneada (o
    # `application.ini` do Zend 1, para `autoloadernamespaces`/`appnamespace`) — algo que não dá
    # para fixar na hora em que o `PROFILE` é construído (módulo importado uma vez só, sem saber
    # qual pasta vai ser escaneada). Quando presente, `pipeline._settings_for_profile` chama isto
    # com a pasta de `--source` e usa o resultado no lugar de `class_resolvers`. `None` no
    # `ecidade`: os resolvedores dele não dependem de nada fora do próprio nome da classe.
    class_resolvers_factory: Callable[[Path], tuple[ClassResolver, ...]] | None = None
    # "catalog": módulo vem do dicionário/menu do e-cidade. "directory": módulo é a
    # pasta de 1º nível do projeto (usado pelo `zend1`).
    module_source: str = "catalog"
    # superfície de comunicação. `endpoint_system` é o slug de **quem serve**
    # (o ID do `Endpoint`); `ws_xml` lê os XML de requisição; `remote_calls` acha as chamadas a
    # outro sistema; `soap_server_classes` são as classes de servidor SOAP cujo `setClass('X')`
    # literal publica cada método público de X como endpoint.
    endpoint_system: str | None = None
    ws_xml: tuple[WsXmlSource, ...] = ()
    ws_xml_ignore_prefixes: tuple[str, ...] = ()
    remote_calls: tuple[RemoteCallPattern, ...] = ()
    soap_server_classes: tuple[str, ...] = ()
    # `switch ($oParam->exec)` vira um endpoint RPC por `case`; as rotas do
    # Laravel (`Route::…`) viram `Route` e `ROUTES_TO`, seguindo `group(base_path(...))`.
    rpc_endpoints: bool = False
    laravel_routes: bool = False
    # lê as anotações do Doctrine (`@Entity`/`@Table`) e traduz a DQL em strings.
    reads_doctrine: bool = False
    # model Eloquent → tabela. Só o e-cidade usa Laravel.
    reads_eloquent: bool = False
    module_aliases: Mapping[str, str] = field(default_factory=dict)
    default_search_path: tuple[str, ...] = ()

    @property
    def required_marker(self) -> str:
        return self.markers[0]


def _registry() -> Mapping[str, ProjectProfile]:
    # Import local: evita ciclo (`profiles.ecidade`/`profiles.zend1` importam `ProjectProfile`,
    # `DaoFactory` e `ClassResolver` deste módulo). Chamado uma vez, no fim deste arquivo, depois
    # que essas três classes já existem no namespace do módulo.
    from php_map.profiles.ecidade import PROFILE as ecidade_profile
    from php_map.profiles.zend1 import PROFILE as zend1_profile

    return MappingProxyType({profile.name: profile for profile in (ecidade_profile, zend1_profile)})


PROFILES: Mapping[str, ProjectProfile] = _registry()


def get_profile(name: str) -> ProjectProfile:
    """`PROFILES[name]`; `ValueError` com a lista de nomes válidos se não existir."""
    try:
        return PROFILES[name]
    except KeyError:
        valid = ", ".join(sorted(PROFILES))
        raise ValueError(f"perfil desconhecido: {name!r} (válidos: {valid})") from None


def suggest_profile(source: Path) -> str | None:
    """Pelo marcador exigido de cada perfil; `None` sem casar nenhum ou casando mais de um
    (a interface pede confirmação ao usuário nesse caso)."""
    hits = [p.name for p in PROFILES.values() if (source / p.required_marker).exists()]
    return hits[0] if len(hits) == 1 else None
