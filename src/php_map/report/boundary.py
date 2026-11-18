"""Saída do relatório de fronteira: Markdown, JSON (o `BoundaryReport`) e CSV."""

import csv
import io
import json
from collections import defaultdict
from dataclasses import asdict

from php_map.analysis.aggregate import NO_MODULE
from php_map.analysis.boundary import MATRIX_TOP, BoundaryReport, EndpointPlan, ModuleCard

WHATS = ("endpoints", "modules", "matrix")


def short(node: str) -> str:
    return node.partition(":")[2] if ":" in node else node


def _table(name: str) -> str:
    return name.removeprefix("Table:")


def _join(items: tuple[str, ...] | list[str], limit: int = 6) -> str:
    shown = [f"`{i}`" for i in items[:limit]]
    extra = f" +{len(items) - limit}" if len(items) > limit else ""
    return ", ".join(shown) + extra if shown else "—"


def to_json(report: BoundaryReport) -> str:
    return json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n"


def to_csv(report: BoundaryReport, what: str = "endpoints") -> str:
    if what not in WHATS:
        raise ValueError(f"--what aceita: {', '.join(WHATS)}")
    out = io.StringIO()
    writer = csv.writer(out, lineterminator="\n")
    if what == "endpoints":
        writer.writerow(
            [
                "endpoint", "system", "protocol", "kind", "natureza", "motivo", "served_by",
                "callers", "calls", "caller_projects", "caller_modules", "params", "reads",
                "writes", "ignored_tables", "max_writers_direct", "max_writers_via_dao",
                "served_by_files",
            ]
        )  # fmt: skip
        for p in report.endpoints:
            writer.writerow(
                [
                    p.endpoint_id, p.system, p.protocol, p.kind or "", p.natureza, p.motivo or "",
                    p.served_by or "", p.callers, p.calls, ";".join(p.caller_projects),
                    ";".join(p.caller_modules), ";".join(p.params),
                    ";".join(_table(t) for t in p.reads), ";".join(_table(t) for t in p.writes),
                    p.ignored_tables, p.max_writers_direct, p.max_writers_via_dao,
                    ";".join(p.served_by_files),
                ]
            )  # fmt: skip
    elif what == "modules":
        writer.writerow(
            ["module", "name", "files", "ca", "ce", "writes_into_others",
             "owned_written_by_others", "custo_corte"]
        )  # fmt: skip
        for c in report.candidates:
            writer.writerow(
                [c.module, c.name, c.files, c.ca, c.ce, c.writes_into_others,
                 c.owned_written_by_others, c.cost]
            )  # fmt: skip
    else:
        writer.writerow(["src", "dst", "kind", "weight", "edges"])
        for m in report.matrix:
            writer.writerow([m.src, m.dst, m.kind, round(m.weight, 4), m.edges])
    return out.getvalue()


# --------------------------------------------------------------------------- Markdown


def _plan_lines(plans: tuple[EndpointPlan, ...], limit: int) -> list[str]:
    lines = [
        "| Endpoint | Natureza | Chamadas | Módulos que chamam | Lê | Escreve"
        " | Máx. escritores (direto / via DAO) |",
        "|---|---|---|---|---|---|---|",
    ]
    shown = plans if limit <= 0 else plans[:limit]
    for p in shown:
        natureza = p.natureza + (f" ({p.motivo})" if p.motivo else "")
        lines.append(
            f"| `{short(p.endpoint_id)}` | {natureza} | {p.calls} |"
            f" {_join([short(m) for m in p.caller_modules], 3)} |"
            f" {_join([_table(t) for t in p.reads], 4)} |"
            f" {_join([_table(t) for t in p.writes], 4)} |"
            f" {p.max_writers_direct} / {p.max_writers_via_dao} |"
        )
    if limit > 0 and len(plans) > limit:
        lines.append(f"\n… {len(plans) - limit} endpoints a mais (use `--limit 0`, JSON ou CSV).")
    return lines


def _weight(value: float) -> str:
    """Uma casa decimal, sem arredondar peso pequeno para zero (as arestas são fracionárias)."""
    if not value:
        return ""
    return "<0.1" if value < 0.05 else f"{value:.1f}"


def _matrix_lines(report: BoundaryReport) -> list[str]:
    weights: dict[tuple[str, str], float] = defaultdict(float)
    totals: dict[str, float] = defaultdict(float)
    for cell in report.matrix:
        if NO_MODULE in (cell.src, cell.dst):
            continue
        weights[(cell.src, cell.dst)] += cell.weight
        totals[cell.src] += cell.weight
        totals[cell.dst] += cell.weight
    top = [m for m, _ in sorted(totals.items(), key=lambda kv: (-kv[1], kv[0]))[:MATRIX_TOP]]
    if not top:
        return ["Sem arestas entre módulos."]
    header = "| de \\ para | " + " | ".join(short(m) for m in top) + " |"
    lines = [header, "|---" * (len(top) + 1) + "|"]
    for src in top:
        cells = []
        for dst in top:
            value = weights.get((src, dst), 0.0)
            cells.append("·" if src == dst else _weight(value))
        lines.append(f"| **{short(src)}** | " + " | ".join(cells) + " |")
    lines.append(
        f"\nPeso fracionário por regra (`1 / (n_src × n_dst)`); soma de `code`, "
        f"`table_read` e `table_write`. Os {len(top)} módulos de maior peso; `{NO_MODULE}` fica "
        "só no CSV (`--what matrix`)."
    )
    return lines


def _card_lines(card: ModuleCard) -> list[str]:
    lines = [
        f"## Módulo `{short(card.module)}` ({card.name})",
        "",
        f"- Arquivos: {card.files} · telas: {card.screens} · tabelas próprias: {card.tables_owned}",
        f"- Ca (quem depende dele): {card.ca} · Ce (de quem ele depende): {card.ce}"
        f" · instabilidade: {card.instability if card.instability is not None else '—'}",
        f"- **custo_corte: {card.cost}**",
        f"- Tabelas próprias compartilhadas: {_join([_table(t) for t in card.shared_tables])}",
        "",
        f"### Tabelas próprias escritas por outros módulos ({len(card.owned_written_by_others)})",
    ]
    for t in card.owned_written_by_others[:20]:
        lines.append(f"- `{_table(t.table)}` ← {', '.join(short(m) for m in t.modules)}")
    lines += ["", f"### Tabelas de outros módulos que ele escreve ({len(card.writes_into_others)})"]
    for t in card.writes_into_others[:20]:
        lines.append(f"- `{_table(t.table)}` (dono: {', '.join(short(m) for m in t.modules)})")
    for title, edges in (("Depende de", card.edges_out), ("Dependem dele", card.edges_in)):
        lines += [
            "",
            f"### {title} (top {len(edges)})",
            "| Módulo | Tipo | Peso | Arestas |",
            "|---|---|---|---|",
        ]
        lines += [
            f"| `{short(e.module)}` | {e.kind} | {_weight(e.weight)} | {e.edges} |" for e in edges
        ]
    lines += [
        "",
        f"### Endpoints: servidos {len(card.endpoints_served)}"
        f" · chamados {len(card.endpoints_called)}",
        f"- Servidos: {_join([short(e) for e in card.endpoints_served], 10)}",
        f"- Chamados: {_join([short(e) for e in card.endpoints_called], 10)}",
        f"- Pontos quentes: {_join([short(h) for h in card.hotspots], 10)}",
    ]
    return lines


def to_markdown(report: BoundaryReport, limit: int = 40) -> str:
    counts = report.natureza_counts
    lines = [
        f"# Fronteira — {report.project}",
        "",
        f"Banco `{report.db}` · profundidade {report.depth} · endpoints `{report.selection}`.",
        "",
    ]
    lines += [f"> ⚠️ {w}" for w in report.warnings]
    if report.module is not None:
        lines += [*_card_lines(report.module), ""]
    lines += [
        "## Plano de corte por endpoint",
        "",
        f"{len(report.endpoints)} endpoints: **{counts.get('leitura', 0)} leitura** (candidatos a "
        f"réplica), **{counts.get('comando', 0)} comando** (candidatos a fila), "
        f"{counts.get('indeterminado', 0)} indeterminado.",
        "",
        *_plan_lines(report.endpoints, limit),
        "",
    ]
    if report.has_modules and report.module is None:
        lines += [
            f"## Candidatos à extração ({report.modules_total} módulos;"
            " ranking dos que têm 10+ arquivos)",
            "",
            f"`{report.cost_formula}`. **Apoio à decisão, não veredito**: confira os componentes.",
            "",
            "| # | Módulo | Arquivos | Ca | Ce | Escreve em outros"
            " | Próprias escritas por outros | custo_corte |",
            "|---|---|---|---|---|---|---|---|",
        ]
        shown = report.candidates if limit <= 0 else report.candidates[:limit]
        for i, c in enumerate(shown, 1):
            lines.append(
                f"| {i} | `{short(c.module)}` ({c.name}) | {c.files} | {c.ca} | {c.ce} |"
                f" {c.writes_into_others} | {c.owned_written_by_others} | **{c.cost}** |"
            )
        lines += ["", "## Matriz de acoplamento entre módulos", "", *_matrix_lines(report), ""]
    return "\n".join(lines) + "\n"
