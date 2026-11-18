"""Projetos da interface: `projects.json`, validações e o estado do servidor.

O DSN nunca entra no cadastro: só o nome da variável do `.env`.
"""

import json
from pathlib import Path

import pytest
from cross_graphs import META_CHAMA, META_SERVE, build_chama, build_serve
from cross_graphs import save as save_graph

from php_map.errors import ConfigError
from php_map.web import projects as pj
from php_map.web.state import AppState


def ecidade_folder(root: Path, name: str = "e-cidade") -> Path:
    folder = root / name
    (folder / "libs").mkdir(parents=True)
    (folder / "libs/db_stdlib.php").write_text("<?php\n", encoding="utf-8")
    return folder


def zend_folder(root: Path, name: str = "zend-app") -> Path:
    folder = root / name
    (folder / "application/configs").mkdir(parents=True)
    (folder / "application/configs/application.ini").write_text("[production]\n", encoding="utf-8")
    return folder


# --------------------------------------------------------------------------- validações


def test_dsn_env_accepts_only_a_variable_name() -> None:
    assert pj.check_dsn_env("SEGUNDO_PROJETO_DSN") == "SEGUNDO_PROJETO_DSN"
    assert pj.check_dsn_env("  PHP_MAP_CATALOG_DSN ") == "PHP_MAP_CATALOG_DSN"
    assert pj.check_dsn_env("") is None and pj.check_dsn_env(None) is None
    secret = "postgresql://usuario:segredo@192.0.2.10:5433/exemplo"
    for bad in (secret, "host=db password=segredo", "usuario:segredo@host", "A B", "1ABC"):
        with pytest.raises(ConfigError) as info:
            pj.check_dsn_env(bad)
        assert "segredo" not in str(info.value) and "postgresql" not in str(info.value)


def test_slug_and_profile_validation() -> None:
    assert pj.slugify("Sistema Zend!") == "sistema-zend"
    assert pj.slugify("***") == "projeto"
    assert pj.check_slug("zend-app-2") == "zend-app-2"
    for bad in ("", "ZendApp", "-x", "a b", "a/b", "x" * 80):
        with pytest.raises(ConfigError, match="identificador inválido"):
            pj.check_slug(bad)
    with pytest.raises(ConfigError, match="perfil desconhecido"):
        pj.check_profile("laravel")


def test_source_is_validated_by_the_marker_of_the_chosen_profile(tmp_path: Path) -> None:
    eco, zend = ecidade_folder(tmp_path), zend_folder(tmp_path)
    assert pj.check_source(str(eco), "ecidade") == str(eco.resolve())
    assert pj.check_source(str(zend), "zend1") == str(zend.resolve())
    with pytest.raises(ConfigError, match=r"perfil 'zend1'.*application/configs/application.ini"):
        pj.check_source(str(eco), "zend1")
    with pytest.raises(ConfigError, match=r"perfil 'ecidade'.*libs/db_stdlib.php"):
        pj.check_source(str(zend), "ecidade")
    with pytest.raises(ConfigError, match="inexistente"):
        pj.check_source(str(tmp_path / "nada"), "ecidade")
    with pytest.raises(ConfigError, match="informe a pasta"):
        pj.check_source("  ", "ecidade")


def test_suggest_profile_by_markers(tmp_path: Path) -> None:
    eco, zend = ecidade_folder(tmp_path), zend_folder(tmp_path)
    assert pj.suggest(str(eco)).profile == "ecidade"
    found = pj.suggest(str(zend))
    assert found.profile == "zend1" and found.exists
    assert ("zend1", "application/configs/application.ini", True) in found.markers
    assert ("ecidade", "libs/db_stdlib.php", False) in found.markers
    both = ecidade_folder(tmp_path, "ambos")
    (both / "application/configs").mkdir(parents=True)
    (both / "application/configs/application.ini").write_text("x", encoding="utf-8")
    assert pj.suggest(str(both)).profile is None  # ambíguo: o usuário decide
    nothing = tmp_path / "vazia"
    nothing.mkdir()
    assert pj.suggest(str(nothing)).profile is None
    assert pj.suggest(str(tmp_path / "nada")).exists is False


def test_build_project_slug_from_the_name_without_collisions(tmp_path: Path) -> None:
    zend = zend_folder(tmp_path)
    args = {"profile": "zend1", "source": str(zend), "schema_from_db": False, "dsn_env": None}
    first = pj.build_project(slug=None, name="Zend App", taken=set(), **args)
    assert (first.slug, first.name, first.profile, first.db) == (
        "zend-app",
        "Zend App",
        "zend1",
        None,
    )
    second = pj.build_project(slug=None, name="Zend App", taken={"zend-app"}, **args)
    assert second.slug == "zend-app-2"
    with pytest.raises(ConfigError, match="já existe"):
        pj.build_project(slug="zend-app", name="outro", taken={"zend-app"}, **args)
    with pytest.raises(ConfigError, match="nome do projeto"):
        pj.build_project(slug=None, name="  ", taken=set(), **args)
    # schema do banco sem dsn_env vale: cai na variável padrão do scan
    assert pj.build_project(slug=None, name="x", taken=set(), **{**args, "schema_from_db": True})


def test_change_project_revalidates_the_folder_against_the_final_profile(tmp_path: Path) -> None:
    eco, zend = ecidade_folder(tmp_path), zend_folder(tmp_path)
    project = pj.Project("p", "P", "ecidade", str(eco.resolve()))
    kwargs = {"name": None, "profile": None, "source": None, "schema_from_db": None}
    changed = pj.change_project(
        project, dsn_env="SEGUNDO_PROJETO_DSN", clear_dsn_env=False, **kwargs
    )
    assert changed.dsn_env == "SEGUNDO_PROJETO_DSN"
    cleared = pj.change_project(changed, dsn_env=None, clear_dsn_env=True, **kwargs)
    assert cleared.dsn_env is None
    # trocar só o perfil exige que a pasta atual tenha o marcador do novo perfil
    with pytest.raises(ConfigError, match="application/configs/application.ini"):
        pj.change_project(
            project, dsn_env=None, clear_dsn_env=False, **{**kwargs, "profile": "zend1"}
        )
    moved = pj.change_project(
        project,
        dsn_env=None,
        clear_dsn_env=False,
        **{**kwargs, "profile": "zend1", "source": str(zend)},
    )
    assert (moved.profile, moved.source) == ("zend1", str(zend.resolve()))
    with pytest.raises(ConfigError):
        pj.change_project(project, dsn_env="postgresql://u:p@h/d", clear_dsn_env=False, **kwargs)


# --------------------------------------------------------------------------- arquivo


def test_store_round_trip_is_atomic_and_drops_invalid_entries(tmp_path: Path) -> None:
    store = pj.ProjectStore(tmp_path / "out")
    assert store.load() == ([], None) and not store.exists()
    items = [
        pj.Project("a", "A", "ecidade", "/x", db="/g/a.db", dsn_env="PHP_MAP_CATALOG_DSN"),
        pj.Project("b", "B", "zend1", "/y", schema_from_db=True, last_scan="2026-10-03"),
    ]
    store.save(items, "b")
    assert store.exists() and not store.path.with_name("projects.json.tmp").exists()
    loaded, active = store.load()
    assert loaded == items and active == "b"
    data = json.loads(store.path.read_text(encoding="utf-8"))
    assert data["version"] == 1 and set(data["projects"][0]) == {
        "slug",
        "name",
        "profile",
        "source",
        "db",
        "schema_from_db",
        "dsn_env",
        "last_scan",
    }
    # arquivo editado à mão: perfil desconhecido, slug inválido, repetido e DSN no campo do nome
    data["projects"] += [
        {"slug": "c", "profile": "nao-existe", "source": "/z"},
        {"slug": "Mau Slug", "profile": "ecidade", "source": "/z"},
        {"slug": "a", "profile": "ecidade", "source": "/outra"},
        {"slug": "d", "profile": "ecidade", "source": "/d", "dsn_env": "postgresql://u:s@h/d"},
        "lixo",
    ]
    store.path.write_text(json.dumps(data), encoding="utf-8")
    loaded, _ = store.load()
    assert [p.slug for p in loaded] == ["a", "b", "d"]
    assert loaded[2].dsn_env is None  # o valor que parecia conexão foi descartado


def test_invalid_file_starts_empty(tmp_path: Path) -> None:
    store = pj.ProjectStore(tmp_path)
    store.path.write_text("{ não é json", encoding="utf-8")
    assert store.load() == ([], None)
    store.path.write_text(json.dumps({"projects": "x"}), encoding="utf-8")
    assert store.load() == ([], None)


# --------------------------------------------------------------------------- estado


@pytest.fixture
def graphs(tmp_path: Path) -> tuple[Path, Path]:
    return (
        save_graph(tmp_path, "serve", build_serve(), META_SERVE),
        save_graph(tmp_path, "chama", build_chama(), META_CHAMA),
    )


def test_state_creates_an_implicit_project_from_the_db_meta(
    graphs: tuple[Path, Path], tmp_path: Path
) -> None:
    state = AppState(db_path=graphs[0], out_dir=tmp_path / "out")
    (only,) = state.projects()
    assert (only.slug, only.name, only.profile, only.db) == (
        "ecidade-t",
        "e-cidade teste",
        "ecidade",
        str(graphs[0]),
    )
    assert state.active_project() == only and state.current_db() == graphs[0]
    assert not state.project_store.exists()  # nada gravado até a primeira mudança


def test_state_add_set_active_and_remove(graphs: tuple[Path, Path], tmp_path: Path) -> None:
    zend = zend_folder(tmp_path)
    state = AppState(db_path=graphs[0], out_dir=tmp_path / "out")
    added = state.add_project(name="Zend App", profile="zend1", source=str(zend))
    assert added.slug == "zend-app" and state.project_store.exists()
    assert state.active_project().slug == "ecidade-t"  # type: ignore[union-attr]
    # sem grafo ainda: o servidor fica sem banco e diz o que fazer
    state.set_active("zend-app")
    assert state.current_db() is None and state.active_project().slug == "zend-app"  # type: ignore[union-attr]
    with pytest.raises(ConfigError, match="troque de projeto antes de remover"):
        state.remove_project("zend-app")
    state.set_active("ecidade-t")
    assert state.current_db() == graphs[0]
    state.remove_project("zend-app")
    assert [p.slug for p in state.projects()] == ["ecidade-t"]
    with pytest.raises(ConfigError, match="não encontrado"):
        state.set_active("zend-app")


def test_scanned_project_gets_its_graph_and_the_server_switches_only_if_active(
    graphs: tuple[Path, Path], tmp_path: Path
) -> None:
    zend = zend_folder(tmp_path)
    state = AppState(db_path=graphs[0], out_dir=tmp_path / "out")
    state.add_project(name="Zend App", profile="zend1", source=str(zend))
    state.project_scanned("zend-app", graphs[1])  # o ativo é outro: só grava
    assert state.current_db() == graphs[0]
    zend = state.project("zend-app")
    assert zend.db == str(graphs[1]) and zend.last_scan and zend.has_graph()
    state.set_active("zend-app")
    assert state.current_db() == graphs[1]
    saved, active = state.project_store.load()  # sobrevive à reinicialização
    assert active == "zend-app" and {p.slug: p.db for p in saved}["zend-app"] == str(graphs[1])
    restarted = AppState(db_path=None, out_dir=tmp_path / "out", explicit_db=False)
    assert restarted.current_db() == graphs[1]


def test_explicit_db_wins_over_the_saved_active_project(
    graphs: tuple[Path, Path], tmp_path: Path
) -> None:
    zend = zend_folder(tmp_path)
    state = AppState(db_path=graphs[0], out_dir=tmp_path / "out")
    state.add_project(name="Zend App", profile="zend1", source=str(zend))
    state.project_scanned("zend-app", graphs[1])
    state.set_active("zend-app")
    # `--db` pedido na linha de comando: serve esse banco, no projeto de mesmo slug
    explicit = AppState(db_path=graphs[0], out_dir=tmp_path / "out", explicit_db=True)
    assert explicit.current_db() == graphs[0]
    assert explicit.active_project().slug == "ecidade-t"  # type: ignore[union-attr]
    # `--db` padrão (não pedido): vale o projeto ativo salvo
    default = AppState(db_path=graphs[0], out_dir=tmp_path / "out", explicit_db=False)
    assert default.current_db() == graphs[1]


def test_cross_report_needs_another_project_with_a_graph(
    graphs: tuple[Path, Path], tmp_path: Path
) -> None:
    zend = zend_folder(tmp_path)
    state = AppState(db_path=graphs[0], out_dir=tmp_path / "out")
    report, reason = state.cross_report()
    assert report is None and "nenhum outro projeto" in (reason or "")
    state.add_project(name="Zend App", profile="zend1", source=str(zend))
    assert state.cross_report()[0] is None  # cadastrado, mas sem grafo
    state.project_scanned("zend-app", graphs[1])
    report, reason = state.cross_report()
    assert report is not None and reason is None
    assert report.counts["casado"] == 3
    assert state.cross_report()[0] is report  # em cache (mesmos arquivos e datas)
