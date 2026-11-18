"""Módulo por regra de nome: `BELONGS_TO` com `NAMING_RULE` para os arquivos que o
catálogo do e-cidade não classificou.

1. **Voto do prefixo.** Entre as telas com o mesmo `module_hint` (`emp`, `pes`…) que têm
   `BELONGS_TO` do catálogo, o módulo mais frequente vale para as telas do prefixo que não têm
   módulo, se houver ao menos `MIN_VOTERS` telas e a fração dele for ≥ `MIN_FRACTION`.
   Confiança: `min(0.9, fração)`.
2. **DAO → tabela.** `classes/db_<tabela>_classe.php` recebe o módulo da `Table:*.<tabela>`
   no dicionário, com 0.9 (ou `0.9 / n` para cada um de `n` módulos).

Só usa módulos que o catálogo criou: nenhum `Module` novo. Roda depois de `apply_catalog`.
"""

import re
import time
from collections import Counter
from collections.abc import Mapping
from dataclasses import dataclass

from php_map.graph.builder import GraphBuilder
from php_map.graph.model import Edge, EdgeSource, EdgeType, NodeLabel, key_of

MIN_VOTERS = 5
MIN_FRACTION = 0.6
MAX_CONFIDENCE = 0.9
DAO_CONFIDENCE = 0.9
# DAO do e-cidade: `classes/db_empempenho_classe.php` → tabela `empempenho`.
_DAO = re.compile(r"(?:^|/)classes/db_(\w+)_classe\.php$", re.IGNORECASE)


@dataclass(slots=True)
class ModuleStats:
    prefixes_voted: int = 0  # prefixos com maioria: as telas sem módulo herdam o vencedor
    prefixes_no_majority: int = 0  # ≥ MIN_VOTERS telas, mas fração < MIN_FRACTION ou empate
    prefixes_few_screens: int = 0  # menos de MIN_VOTERS telas com módulo do catálogo
    screens_classified: int = 0
    daos_classified: int = 0
    daos_without_module: int = 0  # DAO cuja tabela não tem módulo no dicionário
    files_with_catalog: int = 0  # File real, não infra, com BELONGS_TO do catálogo
    files_without_module: int = 0  # File real, não infra, sem BELONGS_TO nenhum no fim
    edges: int = 0
    seconds: float = 0.0


def apply_naming_modules(builder: GraphBuilder) -> ModuleStats:
    started = time.perf_counter()
    stats = ModuleStats()
    file_modules, table_modules = _catalog_modules(builder)
    files = [
        (node.id, node.attrs.get("module_hint"))
        for node in builder.iter_nodes()
        if node.label == NodeLabel.FILE
        and node.attrs.get("resolved") is not False  # placeholder: include de arquivo ausente
        and not node.attrs.get("is_infra")
    ]
    classified: set[str] = set()
    winners = _vote(files, file_modules, stats)
    for file_node, hint in files:
        if file_node in file_modules:
            continue
        if hint in winners:
            module, votes, total = winners[hint]
            evidence = {
                "path": key_of(file_node),
                "rule": f"prefix:{hint}",
                "votes": f"{votes}/{total}",
            }
            _edge(builder, stats, file_node, module, min(MAX_CONFIDENCE, votes / total), evidence)
            stats.screens_classified += 1
            classified.add(file_node)
        if (match := _DAO.search(key_of(file_node))) is None:
            continue
        table = match.group(1).lower()
        modules = sorted(table_modules.get(table, ()))
        if not modules:
            stats.daos_without_module += 1
            continue
        evidence = {"path": key_of(file_node), "rule": f"dao:{table}"}
        for module in modules:
            _edge(builder, stats, file_node, module, DAO_CONFIDENCE / len(modules), evidence)
        stats.daos_classified += 1
        classified.add(file_node)
    stats.files_with_catalog = sum(1 for file_node, _hint in files if file_node in file_modules)
    stats.files_without_module = len(files) - stats.files_with_catalog - len(classified)
    stats.seconds = round(time.perf_counter() - started, 2)
    return stats


def _catalog_modules(builder: GraphBuilder) -> tuple[dict[str, set[str]], dict[str, set[str]]]:
    """`BELONGS_TO` do catálogo: `File` → módulos e nome da tabela (sem schema) → módulos."""
    files: dict[str, set[str]] = {}
    tables: dict[str, set[str]] = {}
    for edge in builder.iter_edges():
        if edge.type != EdgeType.BELONGS_TO or edge.source != EdgeSource.ECIDADE_CATALOG:
            continue
        if edge.src.startswith(f"{NodeLabel.FILE}:"):
            files.setdefault(edge.src, set()).add(edge.dst)
        elif edge.src.startswith(f"{NodeLabel.TABLE}:"):
            name = key_of(edge.src).partition(".")[2]
            tables.setdefault(name, set()).add(edge.dst)
    return files, tables


def _vote(
    files: list[tuple[str, str | None]],
    file_modules: Mapping[str, set[str]],
    stats: ModuleStats,
) -> dict[str, tuple[str, int, int]]:
    """Prefixo → `(módulo vencedor, votos, telas com módulo)`.

    Cada tela com módulo do catálogo vota uma vez em cada módulo dela; a fração é
    `votos / telas`. Empate no topo = sem maioria.
    """
    votes: dict[str, Counter[str]] = {}
    voters: Counter[str] = Counter()
    for file_node, hint in files:
        if hint and file_node in file_modules:
            voters[hint] += 1
            votes.setdefault(hint, Counter()).update(file_modules[file_node])
    winners: dict[str, tuple[str, int, int]] = {}
    for hint, total in sorted(voters.items()):
        if total < MIN_VOTERS:
            stats.prefixes_few_screens += 1
            continue
        ranked = sorted(votes[hint].items(), key=lambda item: (-item[1], item[0]))
        module, count = ranked[0]
        tie = len(ranked) > 1 and ranked[1][1] == count
        if tie or count / total < MIN_FRACTION:
            stats.prefixes_no_majority += 1
            continue
        winners[hint] = (module, count, total)
        stats.prefixes_voted += 1
    return winners


def _edge(
    builder: GraphBuilder,
    stats: ModuleStats,
    src: str,
    module: str,
    confidence: float,
    evidence: dict[str, str],
) -> None:
    builder.add_edge(
        Edge(src, module, EdgeType.BELONGS_TO, EdgeSource.NAMING_RULE, confidence, evidence)
    )
    stats.edges += 1
