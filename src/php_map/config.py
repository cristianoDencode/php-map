"""Configuração do mapeador.

O caminho do e-cidade nunca é fixo no código: ele sempre chega por parâmetro.

As constantes específicas do e-cidade (abaixo) não são mais definidas aqui
— moveram para `profiles/ecidade.py`. Ficam **reexportadas** para não quebrar quem já importa
deste módulo (CLI, `resolve/catalog.py`, `ingest/sql_schema.py`, testes). `Settings` passou a
carregar também os campos do perfil ativo (`profile`, `class_resolvers`, `third_party_dirs`,
`module_source`, `encoding`); `pipeline.run_scan` os preenche a partir de `profiles.get_profile`.
"""

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from php_map.profiles.base import ClassResolver as ClassResolver
from php_map.profiles.base import DaoFactory as DaoFactory
from php_map.profiles.base import RemoteCallPattern as RemoteCallPattern
from php_map.profiles.base import WsXmlSource as WsXmlSource
from php_map.profiles.ecidade import CLASS_RESOLVERS as CLASS_RESOLVERS
from php_map.profiles.ecidade import DAO_FACTORIES as DAO_FACTORIES
from php_map.profiles.ecidade import DAO_WRITE_METHODS as DAO_WRITE_METHODS
from php_map.profiles.ecidade import DEFAULT_EXCLUDE_DIRS as DEFAULT_EXCLUDE_DIRS
from php_map.profiles.ecidade import ECIDADE_MARKER as ECIDADE_MARKER
from php_map.profiles.ecidade import ECIDADE_SEARCH_PATH as ECIDADE_SEARCH_PATH
from php_map.profiles.ecidade import INFRA_FILES as INFRA_FILES
from php_map.profiles.ecidade import INFRA_FUNCTIONS as INFRA_FUNCTIONS
from php_map.profiles.ecidade import INFRA_RPC_FUNCTIONS as INFRA_RPC_FUNCTIONS
from php_map.profiles.ecidade import INFRA_TABLE_PREFIXES as INFRA_TABLE_PREFIXES
from php_map.profiles.ecidade import MODULE_ALIASES as MODULE_ALIASES
from php_map.profiles.ecidade import PROFILE as _ECIDADE_PROFILE

# Raiz do repositório (instalação editável): o extrator PHP vive em php-extractor/.
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# Catálogo lido do banco: DSN numa variável ou num `.env` fora do git.
CATALOG_DSN_ENV = "PHP_MAP_CATALOG_DSN"
# `.env` do diretório atual e o da raiz do repositório, nessa ordem.
DOTENV_PATHS: tuple[Path, ...] = (Path(".env"), PROJECT_ROOT / ".env")

# Scan pela interface: o servidor passa o ID do job ao subprocesso, e o
# extrator nomeia os containers com ele (`ecmap-scan-<job>-<lote>`) para o cancelamento.
JOB_ID_ENV = "PHP_MAP_JOB_ID"
# Imagem Docker do extrator PHP: `--php-image` vence a variável; sem as duas,
# a do pacote Windows do e-cidade se existir, senão a do `php-extractor/Dockerfile`.
PHP_IMAGE_ENV = "PHP_MAP_PHP_IMAGE"
PHP_IMAGE_WINDOWS = "ecidade-windows-php74"
PHP_IMAGE_OWN = "php-map-php74"
_JOB_ID_UNSAFE = re.compile(r"[^A-Za-z0-9_.-]")

# JS de biblioteca: viram nó `File` (`kind = "js"`, `is_infra`), mas não são lidos.
# `scripts/scripts.js` e `scripts/strings.js` são do e-cidade, mas toda tela os carrega (11,9 mil
# telas da raiz citam o `scripts.js`): lidos, ligariam cada tela aos 25 `.php` que ele cita.
JS_LIBRARY_GLOBS: tuple[str, ...] = (
    "*.min.js",
    "*prototype.js",
    "*jquery*",
    "assets/*",
    "ext/javascript/*",
    "*/vendors/*",
    "scripts/scripts.js",
    "scripts/strings.js",
)

# Perfil padrão: sem `--profile`, o scan trata a pasta como um e-cidade.
DEFAULT_PROFILE = _ECIDADE_PROFILE.name


@dataclass(frozen=True, slots=True)
class Settings:
    source: Path | None = None
    out_dir: Path = Path("out")
    exclude_dirs: frozenset[str] = DEFAULT_EXCLUDE_DIRS
    # Não há arquivos .inc no e-cidade (verificado em 2026-09-24).
    php_extensions: frozenset[str] = frozenset({".php"})
    # `None`: escolhida por `php_extractor.resolve_image`.
    php_image: str | None = None
    extractor_dir: Path = PROJECT_ROOT / "php-extractor"
    # Arquivos por container do extrator.
    ast_batch_size: int = 2000
    infra_functions: frozenset[str] = INFRA_FUNCTIONS
    infra_files: frozenset[str] = INFRA_FILES
    infra_rpc_functions: frozenset[str] = INFRA_RPC_FUNCTIONS
    infra_table_prefixes: tuple[str, ...] = INFRA_TABLE_PREFIXES
    dao_factories: tuple[DaoFactory, ...] = DAO_FACTORIES
    dao_write_methods: tuple[str, ...] = DAO_WRITE_METHODS
    module_aliases: Mapping[str, str] = MODULE_ALIASES
    search_path: tuple[str, ...] = ECIDADE_SEARCH_PATH  # modo arquivo
    # Busca: texto normalizado em mais arquivos que isso sai do índice.
    boilerplate_min_files: int = 50
    # Agregados: confiança mínima das arestas e pontos quentes por label.
    analytics_min_confidence: float = 0.5
    analytics_hotspots: int = 500
    # campos do perfil ativo. `pipeline.run_scan` os preenche a partir de
    # `profiles.get_profile(options.profile)`; o padrão aqui é o perfil `ecidade`.
    profile: str = DEFAULT_PROFILE
    markers: tuple[str, ...] = (ECIDADE_MARKER,)
    third_party_dirs: tuple[str, ...] = ()
    encoding: str = "latin-1"
    class_resolvers: tuple[ClassResolver, ...] = CLASS_RESOLVERS
    module_source: str = "catalog"
    reads_doctrine: bool = False
    # ver `ProjectProfile`.
    endpoint_system: str | None = None
    ws_xml: tuple[WsXmlSource, ...] = ()
    ws_xml_ignore_prefixes: tuple[str, ...] = ()
    remote_calls: tuple[RemoteCallPattern, ...] = ()
    soap_server_classes: tuple[str, ...] = ()
    rpc_endpoints: bool = False
    laravel_routes: bool = False
    # lê os models Eloquent (`extends Model`) e liga cada um à tabela (`MAPS_TO`).
    reads_eloquent: bool = False
    # padrões (`fnmatch`, `*` atravessa `/`) dos JS de biblioteca (ver acima).
    js_library_globs: tuple[str, ...] = JS_LIBRARY_GLOBS

    @property
    def db_path(self) -> Path:
        return self.out_dir / "graph.db"


def job_id() -> str:
    """ID do scan: `PHP_MAP_JOB_ID` (só caracteres aceitos num nome de container) ou o PID."""
    value = _JOB_ID_UNSAFE.sub("", os.environ.get(JOB_ID_ENV, ""))[:40]
    return value or str(os.getpid())


def env_value(name: str, dotenv: Sequence[Path] | None = None) -> str | None:
    """Valor da variável de ambiente `name` ou, na falta dela, do primeiro `.env` que a tenha.

    O `.env` aceita `NOME=valor`, `export NOME=valor`, comentários com `#` e aspas simples ou
    duplas em volta do valor. Valor vazio conta como ausente.
    """
    value = os.environ.get(name, "").strip()
    if value:
        return value
    for path in DOTENV_PATHS if dotenv is None else dotenv:
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for raw in lines:
            line = raw.strip().removeprefix("export ").strip()
            key, sep, found = line.partition("=")
            if not sep or line.startswith("#") or key.strip() != name:
                continue
            found = found.strip()
            if len(found) >= 2 and found[0] == found[-1] and found[0] in "'\"":
                found = found[1:-1]
            if found:
                return found
    return None
