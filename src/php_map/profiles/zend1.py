"""Perfil `zend1`: PHP com Zend Framework 1.

O perfil traz marcadores, extensões, exclusões, terceiros, encoding e a resolução de classe
pela convenção de nomes do Zend 1. Em `EXCLUDE_DIRS`, só os caminhos de terceiros
(`library/Zend`, `library/Doctrine`...) são excluídos, pela raiz do projeto: um `"library"`
em qualquer nível excluiria também o `library/` **de cada módulo**
(`application/modules/<m>/library/Controller/...`, código próprio pela convenção `Lib_`). O
módulo por diretório e a tela `.phtml` ligada ao controller/action ficam em
`resolve/zend1.py`.
"""

import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType

from php_map.profiles.base import ClassResolver, ProjectProfile, RemoteCallPattern

# Exige `application/configs/application.ini`; `application/Bootstrap.php` é só confirmação
# (usada por `suggest_profile`, que olha apenas o 1º marcador ).
MARKERS = ("application/configs/application.ini", "application/Bootstrap.php")

# `library/` tem Zend, Doctrine, FPDF, MPDF, Twitter e xmlseclibs (medido num projeto real).
# As entradas com `/` só podam esse caminho exato a partir da raiz (`ingest/discovery`):
# o `library/` de cada módulo (`Lib_`, código próprio) não é afetado.
THIRD_PARTY_DIRS: tuple[str, ...] = (
    "library/Zend",
    "library/Doctrine",
    "library/FPDF",
    "library/MPDF54",
    "library/Twitter",
    "library/xmlseclibs-master",
    "library/ruckusing-migrations",
)

EXCLUDE_DIRS: frozenset[str] = frozenset(
    {"vendor", "node_modules", "tmp", "storage", ".git", "logs", "docs", "ci", "migrations"}
) | frozenset(THIRD_PARTY_DIRS)

# Os 8 módulos de verdade (medido em `application/modules/`): usado para a regra 1
# do resolvedor de classe não "inventar" módulo a partir de prefixos que por acaso têm `_`
# (`Zend_Controller_...`, `Doctrine_Record`...). Mudar isso é escopo de uma tarefa nova, não
# descoberta automática: o perfil é de um sistema específico, não de um Zend 1 genérico.
KNOWN_MODULES: frozenset[str] = frozenset(
    {
        "administrativo",
        "auth",
        "cartorio",
        "contribuinte",
        "default",
        "financeira",
        "fiscal",
        "global",
        "webservice",
    }
)

_ENTITIES_ROOT = "application/entidades"
_DEFAULT_APP_NAMESPACE = "Application"
# Reserva: usada quando o `.ini` não existe, não abre ou não declara a chave. O
# próprio `Zend_` nunca aparece em `autoloadernamespaces.*` (o framework não se autocarrega por
# essa chave do `application.ini`, conforme o `.ini` real) — por isso ele é reserva
# permanente, nunca substituída.
_FALLBACK_NAMESPACES: Mapping[str, str] = MappingProxyType(
    {
        "DBSeller_": "library/DBSeller/",
        "E2Tecnologia_": "library/E2Tecnologia/",
        "Zend_": "library/Zend/",
        "Doctrine_": "library/Doctrine/",
        "Twitter_": "library/Twitter/",
    }
)
_VENDOR_PREFIXES_LOWER: frozenset[str] = frozenset(p.lower() for p in _FALLBACK_NAMESPACES)

# "Segunda parte" da convenção `<Modulo>_<Resto>` (1ª regra): nome em minúsculas →
# subpasta. Fora daqui: sufixo `Controller` → `controllers/`; nenhum dos dois → "outras".
_MODULE_SUBDIR: Mapping[str, str] = MappingProxyType(
    {"model": "models", "form": "forms", "lib": "library"}
)

_INI_RELATIVE_PATH = "application/configs/application.ini"
_INI_KEY_VALUE = re.compile(r'^\s*([\w.]+)\s*=\s*"([^"]*)"')


def _read_ini(source: Path) -> tuple[str, Mapping[str, str]]:
    """`(appnamespace, {prefixo: pasta})` do `application.ini` da pasta escaneada.

    Leitura best-effort: arquivo ausente, ilegível ou sem a chave → fica a reserva
    (`_FALLBACK_NAMESPACES`/`_DEFAULT_APP_NAMESPACE`) para essa parte. Não é um parser de `.ini`
    completo (o formato do Zend tem seções com herança, `a : b`, que não interessam aqui): só
    lê `chave = "valor"` linha a linha, o suficiente para `appnamespace` e
    `autoloadernamespaces.*`, que no `[production]` não são sobrescritas por outra seção.
    """
    namespaces = dict(_FALLBACK_NAMESPACES)
    appnamespace = _DEFAULT_APP_NAMESPACE
    try:
        text = (source / _INI_RELATIVE_PATH).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return appnamespace, namespaces
    for line in text.splitlines():
        match = _INI_KEY_VALUE.match(line)
        if match is None:
            continue
        key, value = match.group(1), match.group(2)
        if key == "appnamespace" and value:
            appnamespace = value
        elif key.startswith("autoloadernamespaces.") and value:
            namespaces[value] = f"library/{value.rstrip('_')}/"
    return appnamespace, namespaces


def _match(paths: Sequence[str], candidates: Sequence[str]) -> str | None:
    """Compara sem diferenciar caixa: `class_name` chega em minúsculas no scan de
    verdade (`resolve/symbols.py::resolve_class`, `key = name.lower()`), mas `candidates` são
    caminhos de arquivo de verdade, com a caixa original. Devolve o candidato como está."""
    lowered = {candidate.lower(): candidate for candidate in candidates}
    for path in paths:
        hit = lowered.get(path.lower())
        if hit is not None:
            return hit
    return None


class _ModulePrefixResolver:
    """Regra 1: `<Modulo>_<Resto>` com `<modulo>` em `KNOWN_MODULES`.

    `Model`/`Form`/`Lib` consomem a 2ª parte e trocam de subpasta; sufixo `Controller` vira
    `controllers/` mantendo o nome; sem nenhum dos dois, tenta `models/` e depois a raiz do
    módulo ("outras").
    """

    def expected_paths(self, class_name: str) -> tuple[str, ...]:
        parts = class_name.lstrip("\\").split("_")
        if len(parts) < 2 or parts[0].lower() not in KNOWN_MODULES:
            return ()
        module, rest = parts[0].lower(), parts[1:]
        subdir = _MODULE_SUBDIR.get(rest[0].lower())
        if subdir is not None:
            rest = rest[1:]
            if not rest:
                return ()
            return (f"application/modules/{module}/{subdir}/{'/'.join(rest)}.php",)
        if class_name.lower().endswith("controller"):
            return (f"application/modules/{module}/controllers/{'/'.join(rest)}.php",)
        tail = "/".join(rest) + ".php"
        return (
            f"application/modules/{module}/models/{tail}",
            f"application/modules/{module}/{tail}",
        )

    def resolve(self, class_name: str, candidates: Sequence[str]) -> str | None:
        return _match(self.expected_paths(class_name), candidates)


class _AutoloadNamespaceResolver:
    """Regra 2: namespaces do `autoloadernamespaces.*` do `application.ini`."""

    def __init__(self, namespaces: Mapping[str, str]) -> None:
        self._namespaces = namespaces

    def expected_paths(self, class_name: str) -> tuple[str, ...]:
        short = class_name.lstrip("\\")
        for prefix, folder in self._namespaces.items():
            if short.lower().startswith(prefix.lower()):
                rest = short[len(prefix) :].replace("_", "/")
                return (f"{folder}{rest}.php",) if rest else ()
        return ()

    def resolve(self, class_name: str, candidates: Sequence[str]) -> str | None:
        return _match(self.expected_paths(class_name), candidates)


class _NamespaceEntityResolver:
    """Regra 3: `Financeira\\ArquivoAlerta` → `application/entidades/...` — a
    raiz das entidades Doctrine do perfil (`doctrine.entityPath` do `application.ini`, sempre
    `APPLICATION_PATH "/entidades"` num projeto real; a ligação entidade↔tabela é feita pelo
    resolvedor de Doctrine)."""

    def expected_paths(self, class_name: str) -> tuple[str, ...]:
        if "\\" not in class_name:
            return ()
        root = class_name.lstrip("\\").split("\\", 1)[0].lower()
        # O Doctrine 2 do projeto usa namespace de verdade (`Doctrine\ORM\EntityManager`, não o
        # `Doctrine_` do Zend 1): sem este descarte, toda classe do próprio Doctrine previa
        # (errado) um caminho em `application/entidades/doctrine/...` — achado real no scan do
        # projeto real (29 ocorrências de `Doctrine\Common\Collections\ArrayCollection` sozinha).
        if f"{root}_" in _VENDOR_PREFIXES_LOWER:
            return ()
        rel = class_name.lstrip("\\").replace("\\", "/")
        return (f"{_ENTITIES_ROOT}/{rel}.php",)

    def resolve(self, class_name: str, candidates: Sequence[str]) -> str | None:
        return _match(self.expected_paths(class_name), candidates)


class _AppNamespaceResolver:
    """Regra 4: `<appnamespace>_Resto` → `application/Resto.php`."""

    def __init__(self, appnamespace: str) -> None:
        self._prefix = f"{appnamespace}_"

    def expected_paths(self, class_name: str) -> tuple[str, ...]:
        short = class_name.lstrip("\\")
        if not short.lower().startswith(self._prefix.lower()):
            return ()
        rest = short[len(self._prefix) :].replace("_", "/")
        return (f"application/{rest}.php",) if rest else ()

    def resolve(self, class_name: str, candidates: Sequence[str]) -> str | None:
        return _match(self.expected_paths(class_name), candidates)


def class_resolvers_for(source: Path) -> tuple[ClassResolver, ...]:
    """Os 4 resolvedores, na ordem da especificação, com o `.ini` de `source`."""
    appnamespace, namespaces = _read_ini(source)
    return (
        _ModulePrefixResolver(),
        _AutoloadNamespaceResolver(namespaces),
        _NamespaceEntityResolver(),
        _AppNamespaceResolver(appnamespace),
    )


REMOTE_CALLS: tuple[RemoteCallPattern, ...] = (
    RemoteCallPattern("ecidade", frozenset({"consultar", "processar"}), "ecidade"),
)

PROFILE = ProjectProfile(
    name="zend1",
    markers=MARKERS,
    source_extensions=frozenset({".php", ".phtml"}),
    exclude_dirs=EXCLUDE_DIRS,
    third_party_dirs=THIRD_PARTY_DIRS,
    encoding="utf-8",
    infra_functions=frozenset(),
    infra_files=frozenset(),
    infra_rpc_functions=frozenset(),
    infra_table_prefixes=(),
    dao_factories=(),
    class_resolvers=(),
    class_resolvers_factory=class_resolvers_for,
    module_source="directory",
    # o projeto chama o e-cidade por `WebService_Model_Ecidade::consultar|
    # processar('<metodo>', ...)` e serve SOAP com `new SoapServer(...)->setClass('<Classe>')`.
    endpoint_system="zend1app",
    remote_calls=REMOTE_CALLS,
    soap_server_classes=("soapserver", "zend_soap_server"),
    reads_doctrine=True,
    module_aliases={},
    default_search_path=("public",),
)
