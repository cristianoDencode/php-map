"""As 4 regras de resolução de classe do perfil `zend1`.

Testa os `ClassResolver` direto, com listas de candidatos à mão — não precisa de scan nem de
Docker: é a mesma forma como `resolve/symbols.py::_apply_class_resolvers` os chama.
"""

from pathlib import Path

from php_map.profiles.zend1 import (
    EXCLUDE_DIRS,
    THIRD_PARTY_DIRS,
    _AppNamespaceResolver,
    _AutoloadNamespaceResolver,
    _ModulePrefixResolver,
    _NamespaceEntityResolver,
    _read_ini,
    class_resolvers_for,
)

# ---------------------------------------------------------------------- regra 1: prefixo de módulo


def test_module_prefix_resolver_handles_the_model_form_lib_and_controller_cases() -> None:
    resolver = _ModulePrefixResolver()
    assert resolver.expected_paths("Fiscal_Model_RelatorioPedidos") == (
        "application/modules/fiscal/models/RelatorioPedidos.php",
    )
    assert resolver.expected_paths("Fiscal_Form_BuscaContribuinte") == (
        "application/modules/fiscal/forms/BuscaContribuinte.php",
    )
    assert resolver.expected_paths("Fiscal_Lib_Controller_AbstractController") == (
        "application/modules/fiscal/library/Controller/AbstractController.php",
    )
    assert resolver.expected_paths("Fiscal_PedidoController") == (
        "application/modules/fiscal/controllers/PedidoController.php",
    )


def test_module_prefix_resolver_tries_models_then_the_module_root_for_other_cases() -> None:
    resolver = _ModulePrefixResolver()
    assert resolver.expected_paths("Fiscal_Helper_Algo") == (
        "application/modules/fiscal/models/Helper/Algo.php",
        "application/modules/fiscal/Helper/Algo.php",
    )


def test_module_prefix_resolver_ignores_names_whose_first_segment_is_not_a_known_module() -> None:
    resolver = _ModulePrefixResolver()
    assert resolver.expected_paths("Zend_Controller_Request_Abstract") == ()
    assert resolver.expected_paths("Doctrine_Record") == ()
    assert resolver.expected_paths("SemUnderscore") == ()


def test_module_prefix_resolver_picks_the_candidate_that_matches() -> None:
    resolver = _ModulePrefixResolver()
    candidates = (
        "application/modules/fiscal/models/RelatorioPedidos.php",
        "application/modules/fiscal/_decoy.php",
    )
    assert resolver.resolve("Fiscal_Model_RelatorioPedidos", candidates) == candidates[0]
    assert resolver.resolve("Fiscal_Model_Inexistente", candidates) is None


# ------------------------------------------------------------- regra 2: namespace do autoloader


def test_autoload_namespace_resolver_maps_the_prefix_to_the_library_folder() -> None:
    resolver = _AutoloadNamespaceResolver({"DBSeller_": "library/DBSeller/"})
    assert resolver.expected_paths("DBSeller_Helper_Number") == (
        "library/DBSeller/Helper/Number.php",
    )
    assert resolver.expected_paths("Fiscal_PedidoController") == ()


# ------------------------------------------------------------------ regra 3: namespace PHP (`\`)


def test_namespace_entity_resolver_uses_application_entidades() -> None:
    resolver = _NamespaceEntityResolver()
    assert resolver.expected_paths("Financeira\\ArquivoAlerta") == (
        "application/entidades/Financeira/ArquivoAlerta.php",
    )
    assert resolver.expected_paths("SemNamespace") == ()


def test_namespace_entity_resolver_ignores_the_doctrine_vendor_namespace() -> None:
    """Achado real num projeto zend1: `Doctrine\\Common\\Collections\\ArrayCollection` é do
    próprio Doctrine (namespace de verdade, não o `Doctrine_` do Zend 1) — não é uma entidade."""
    resolver = _NamespaceEntityResolver()
    assert resolver.expected_paths("Doctrine\\Common\\Collections\\ArrayCollection") == ()
    assert resolver.expected_paths("Doctrine\\ORM\\EntityManager") == ()


# --------------------------------------------------------------- regra 4: `appnamespace` (`_`)


def test_app_namespace_resolver_uses_the_configured_appnamespace() -> None:
    resolver = _AppNamespaceResolver("Application")
    assert resolver.expected_paths("Application_Helper_Sample") == (
        "application/Helper/Sample.php",
    )
    assert resolver.expected_paths("Outro_Helper_Sample") == ()


# ---------------------------------------------------------------------------------- `.ini`


def test_read_ini_parses_appnamespace_and_autoload_namespaces(tmp_path: Path) -> None:
    configs = tmp_path / "application/configs"
    configs.mkdir(parents=True)
    (configs / "application.ini").write_text(
        '[production]\nappnamespace = "Application"\nautoloadernamespaces.dbseller = "DBSeller_"\n'
    )
    appnamespace, namespaces = _read_ini(tmp_path)
    assert appnamespace == "Application"
    assert namespaces["DBSeller_"] == "library/DBSeller/"
    # Zend_ não está no .ini de exemplo: continua vindo da reserva.
    assert namespaces["Zend_"] == "library/Zend/"


def test_read_ini_falls_back_when_the_file_is_missing(tmp_path: Path) -> None:
    appnamespace, namespaces = _read_ini(tmp_path)
    assert appnamespace == "Application"
    assert namespaces["Zend_"] == "library/Zend/"


def test_class_resolvers_for_reads_the_real_ini_order() -> None:
    source = Path("tests/fixtures/zend1-mini")
    resolvers = class_resolvers_for(source)
    assert len(resolvers) == 4
    # Regra 2, lida do .ini do fixture: DBSeller_ -> library/DBSeller/.
    assert resolvers[1].expected_paths("DBSeller_Helper_Number") == (
        "library/DBSeller/Helper/Number.php",
    )


# ------------------------------------------------------------ caixa (achado real num projeto zend1)


def test_resolvers_match_lowercase_names_against_real_cased_candidates() -> None:
    """`resolve_class` sempre chama os resolvers com `name.lower()` — o scan de
    verdade nunca manda `class_name` na caixa do exemplo da tarefa. Sem comparar sem diferenciar
    caixa, nenhum dos 4 resolvers nunca "casava" de verdade (achado ao rodar num projeto real:
    `class_not_found` disparava para arquivos que **existem**, como
    `RelatorioPedidos.php`)."""
    candidates = ("application/modules/fiscal/models/RelatorioPedidos.php",)
    assert (
        _ModulePrefixResolver().resolve("fiscal_model_relatoriopedidos", candidates)
        == (candidates[0])
    )

    dbseller = _AutoloadNamespaceResolver({"DBSeller_": "library/DBSeller/"})
    candidates = ("library/DBSeller/Helper/Number.php",)
    assert dbseller.resolve("dbseller_helper_number", candidates) == candidates[0]

    entidade = _NamespaceEntityResolver()
    candidates = ("application/entidades/Financeira/ArquivoAlerta.php",)
    assert entidade.resolve("financeira\\arquivoalerta", candidates) == candidates[0]
    # E o achado real: Doctrine\* (vendor) continua sem prever nada, mesmo em minúsculas.
    assert entidade.expected_paths("doctrine\\common\\collections\\arraycollection") == ()

    app = _AppNamespaceResolver("Application")
    candidates = ("application/Helper/Sample.php",)
    assert app.resolve("application_helper_sample", candidates) == candidates[0]


# --------------------------------------------------------------------------- exclusões (library/)


def test_exclude_dirs_only_excludes_the_third_party_library_subfolders() -> None:
    assert "library" not in EXCLUDE_DIRS
    for path in THIRD_PARTY_DIRS:
        assert path in EXCLUDE_DIRS
    # O library/ de cada módulo (convenção Lib_) não pode estar na lista.
    assert "application/modules/fiscal/library" not in EXCLUDE_DIRS
