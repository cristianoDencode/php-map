"""`db` como pasta de grafos e releitura dinâmica do `projects.json`/`--db`.

Os grafos são bancos reais e mínimos (`store.save` com um `GraphBuilder` vazio): o que importa
aqui é a escolha do arquivo e a releitura, não o conteúdo do grafo.
"""

import json
import logging
from pathlib import Path

import pytest

from php_map.errors import ConfigError
from php_map.graph import store
from php_map.graph.builder import GraphBuilder
from php_map.mcp.catalog import ProjectCatalog, latest_graph


def _graph(path: Path, *, project: str = "e-cidade", profile: str = "ecidade") -> Path:
    store.save(GraphBuilder(), path, {"project": project, "profile": profile})
    return path


def _complete(folder: Path, stamp: str, *, project: str = "e-cidade") -> Path:
    """Um `.db` completo (com `<stem>.scan_summary.json` ao lado), como a interface grava."""
    folder.mkdir(parents=True, exist_ok=True)
    db = _graph(folder / f"{stamp}.db", project=project)
    db.with_name(f"{db.stem}.scan_summary.json").write_text("{}", encoding="utf-8")
    return db


def _partial(folder: Path, stamp: str, *, project: str = "e-cidade") -> Path:
    """Um `.db` sem o resumo: scan em andamento, nunca deve ser escolhido."""
    folder.mkdir(parents=True, exist_ok=True)
    return _graph(folder / f"{stamp}.db", project=project)


# --------------------------------------------------------------------------- `latest_graph`


def test_latest_graph_picks_the_newest_complete_db(tmp_path: Path) -> None:
    folder = tmp_path / "grafos"
    older = _complete(folder, "2026-11-04T12-32-57")
    newer = _complete(folder, "2026-11-05T13-32-07")
    assert latest_graph(folder) == newer
    assert latest_graph(folder) != older


def test_latest_graph_ignores_a_db_without_summary(tmp_path: Path) -> None:
    folder = tmp_path / "grafos"
    previous = _complete(folder, "2026-11-04T12-32-57")
    _partial(folder, "2026-11-09T08-00-00")  # scan em andamento: .db existe, resumo não
    assert latest_graph(folder) == previous


def test_latest_graph_warns_on_an_empty_or_incomplete_folder(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    empty = tmp_path / "vazia"
    empty.mkdir()
    with caplog.at_level(logging.WARNING, logger="php_map.mcp.catalog"):
        assert latest_graph(empty) is None
    assert "sem grafo completo" in caplog.text

    folder = tmp_path / "so-parcial"
    _partial(folder, "2026-11-09T08-00-00")
    caplog.clear()
    with caplog.at_level(logging.WARNING, logger="php_map.mcp.catalog"):
        assert latest_graph(folder) is None
    assert "sem grafo completo" in caplog.text


# --------------------------------------------------------------------------- `projects.json`


def _write_projects(path: Path, projects: list[dict[str, object]]) -> None:
    path.write_text(json.dumps({"projects": projects}), encoding="utf-8")


def test_projects_file_with_folder_db_picks_the_newest(tmp_path: Path) -> None:
    folder = tmp_path / "grafos" / "e-cidade"
    _complete(folder, "2026-11-04T12-32-57")
    newest = _complete(folder, "2026-11-05T13-32-07")
    path = tmp_path / "projects.json"
    _write_projects(path, [{"slug": "e-cidade", "db": str(folder)}])

    catalog = ProjectCatalog.from_projects_file(path)
    project = catalog.get("e-cidade")
    assert project.db == newest
    assert project.db_from_folder is True


def test_projects_file_ignores_project_with_folder_without_a_complete_graph(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    good_db = _graph(tmp_path / "graph.db", project="e-cidade")
    empty_folder = tmp_path / "vazia"
    empty_folder.mkdir()
    path = tmp_path / "projects.json"
    _write_projects(
        path,
        [
            {"slug": "e-cidade", "db": str(good_db)},
            {"slug": "zend-app", "db": str(empty_folder)},
        ],
    )
    with caplog.at_level(logging.WARNING, logger="php_map.mcp.catalog"):
        catalog = ProjectCatalog.from_projects_file(path)
    assert [p.slug for p in catalog.projects] == ["e-cidade"]
    assert "sem grafo completo" in caplog.text


def test_projects_file_is_reread_without_rebuilding_the_catalog(tmp_path: Path) -> None:
    """Critério de aceite: `projects.json` alterado com o servidor de pé -> o projeto novo
    aparece no `list_projects` (aqui, em `catalog.projects`) sem recriar o catálogo."""
    e_cidade = _graph(tmp_path / "e-cidade" / "graph.db", project="e-cidade")
    path = tmp_path / "projects.json"
    _write_projects(path, [{"slug": "e-cidade", "db": str(e_cidade)}])

    catalog = ProjectCatalog.from_projects_file(path)  # o mesmo objeto do início ao fim
    assert [p.slug for p in catalog.projects] == ["e-cidade"]

    zend = _graph(tmp_path / "zend-app" / "graph.db", project="zend-app", profile="zend1")
    _write_projects(
        path,
        [
            {"slug": "e-cidade", "db": str(e_cidade)},
            {"slug": "zend-app", "db": str(zend)},
        ],
    )
    assert sorted(p.slug for p in catalog.projects) == ["e-cidade", "zend-app"]


def test_projects_file_broken_mid_write_keeps_the_previous_list(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    e_cidade = _graph(tmp_path / "e-cidade" / "graph.db", project="e-cidade")
    path = tmp_path / "projects.json"
    _write_projects(path, [{"slug": "e-cidade", "db": str(e_cidade)}])

    catalog = ProjectCatalog.from_projects_file(path)
    before = catalog.projects

    path.write_text('{"projects": [{"slug": "e-ci', encoding="utf-8")  # gravação pela metade
    with caplog.at_level(logging.WARNING, logger="php_map.mcp.catalog"):
        after = catalog.projects
    assert [p.slug for p in after] == [p.slug for p in before]
    assert caplog.text  # algum aviso foi registrado


def test_projects_file_folder_scan_appears_without_touching_the_json(tmp_path: Path) -> None:
    """A pasta do grafo muda sem o `projects.json` mudar: a mesma sessão tem de ver o scan
    novo (o caso real da tarefa: a interface escreve na pasta, não no `projects.json`)."""
    folder = tmp_path / "grafos" / "e-cidade"
    first = _complete(folder, "2026-11-05T13-32-07")
    path = tmp_path / "projects.json"
    _write_projects(path, [{"slug": "e-cidade", "db": str(folder)}])

    catalog = ProjectCatalog.from_projects_file(path)
    assert catalog.get("e-cidade").db == first

    newest = _complete(folder, "2026-11-09T08-00-00")
    assert catalog.get("e-cidade").db == newest


def test_catalog_without_valid_folder_project_is_an_error(tmp_path: Path) -> None:
    empty_folder = tmp_path / "vazia"
    empty_folder.mkdir()
    path = tmp_path / "projects.json"
    _write_projects(path, [{"slug": "x", "db": str(empty_folder)}])
    with pytest.raises(ConfigError, match="nenhum projeto"):
        ProjectCatalog.from_projects_file(path)


# --------------------------------------------------------------------------- `--db <pasta>`


def test_from_dbs_accepts_a_folder_and_picks_the_newest(tmp_path: Path) -> None:
    folder = tmp_path / "grafos" / "e-cidade"
    _complete(folder, "2026-11-04T12-32-57")
    newest = _complete(folder, "2026-11-05T13-32-07")

    catalog = ProjectCatalog.from_dbs([folder])
    project = catalog.get("e-cidade")
    assert project.db == newest
    assert project.db_from_folder is True


def test_from_dbs_folder_rereads_without_rebuilding_the_catalog(tmp_path: Path) -> None:
    folder = tmp_path / "grafos" / "e-cidade"
    first = _complete(folder, "2026-11-05T13-32-07")
    catalog = ProjectCatalog.from_dbs([folder])
    assert catalog.get("e-cidade").db == first

    newest = _complete(folder, "2026-11-09T08-00-00")
    assert catalog.get("e-cidade").db == newest


def test_from_dbs_without_any_complete_graph_is_an_error(tmp_path: Path) -> None:
    empty_folder = tmp_path / "vazia"
    empty_folder.mkdir()
    with pytest.raises(ConfigError, match="nenhum projeto"):
        ProjectCatalog.from_dbs([empty_folder])
