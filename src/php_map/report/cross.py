"""Saídas do `cross`: resumo em texto, JSON e CSV.

O JSON é o `CrossReport` inteiro (a interface consome); o CSV tem uma linha por endpoint.
"""

import csv
import io
import json
from collections import Counter
from dataclasses import asdict

from php_map.analysis.cross import (
    CASADO,
    SEM_CLIENTE,
    SEM_SERVIDOR,
    CrossEndpoint,
    CrossReport,
    Dependency,
)

MAX_NAMES = 6  # nomes listados por linha do resumo
MAX_MODULES = 8
NO_MODULE = "(sem módulo)"
NOT_DEAD_CODE = (
    "servido_sem_cliente não quer dizer código morto: outros clientes (portal, aplicativo, "
    "outro município) podem chamar. Não apague nada com base nesta lista."
)

CSV_COLUMNS = (
    "endpoint",
    "system",
    "protocol",
    "name",
    "kind",
    "status",
    "served_by",
    "callers",
    "calls",
    "caller_projects",
    "caller_modules",
    "params",
    "tables",
    "modules",
    "nodes",
    "server_files",
)


def short(node: str | None) -> str:
    """`Module:fiscal` → `fiscal`; sem módulo → `(sem módulo)`."""
    return node.partition(":")[2] if node else NO_MODULE


def to_json(report: CrossReport) -> str:
    return json.dumps(asdict(report), ensure_ascii=False, indent=2) + "\n"


def to_csv(report: CrossReport) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(CSV_COLUMNS)
    for e in report.endpoints:
        modules = sorted({short(c.module) for c in e.callers})
        writer.writerow(
            [
                e.endpoint_id,
                e.system,
                e.protocol,
                e.name,
                e.kind or "",
                e.status,
                e.served_by or "",
                len(e.callers),
                e.calls,
                ";".join(sorted({c.project for c in e.callers})),
                ";".join(modules),
                ";".join(str(p.get("name", "")) for p in e.params),
                len(e.reaches.tables),
                len(e.reaches.modules),
                e.reaches.nodes,
                ";".join(sorted({s.path for s in e.servers if s.path})),
            ]
        )
    return buffer.getvalue()


def _names(endpoints: list[CrossEndpoint]) -> str:
    names = [e.name for e in endpoints]
    shown = ", ".join(names[:MAX_NAMES])
    return f"{shown}, …" if len(names) > MAX_NAMES else shown


def _protocols(endpoints: list[CrossEndpoint]) -> str:
    per_protocol = Counter(e.protocol for e in endpoints)
    return ", ".join(f"{protocol} {n}" for protocol, n in sorted(per_protocol.items()))


def _dependency_lines(dep: Dependency) -> list[str]:
    modules = ", ".join(f"{short(m.module)} ({m.calls})" for m in dep.modules[:MAX_MODULES])
    if len(dep.modules) > MAX_MODULES:
        modules += ", …"
    return [
        f"Módulos do {dep.caller} que dependem do {dep.server}: {modules or '—'}"
        f" ({dep.endpoints} endpoints, {dep.calls} chamadas)",
        f"Tabelas do {dep.server} alcançadas pelo {dep.caller}: {len(dep.tables)}",
    ]


def summary_lines(report: CrossReport) -> list[str]:
    """O resumo do fim da tabela (e do stderr nos formatos json/csv)."""
    by_status: dict[str, list[CrossEndpoint]] = {s: [] for s in (CASADO, SEM_SERVIDOR, SEM_CLIENTE)}
    for endpoint in report.endpoints:
        by_status[endpoint.status].append(endpoint)
    served = ", ".join(f"{p.slug} {p.served}" for p in report.projects if p.served) or "nenhum"
    called = ", ".join(f"{p.slug} {p.called}" for p in report.projects if p.called) or "nenhum"
    lines = [
        f"Endpoints: {sum(p.served for p in report.projects)} servidos ({served}),"
        f" {sum(p.called for p in report.projects)} chamados ({called})"
    ]
    for status, items in by_status.items():
        detail = ""
        if status == SEM_SERVIDOR and items:
            detail = _names(items)
        elif status == SEM_CLIENTE and items:
            detail = f"({_protocols(items)})"
        lines.append(f"  {status:<22}{len(items):>6}   {detail}".rstrip())
    for dep in report.dependencies:
        lines.extend(_dependency_lines(dep))
    if report.dynamic_calls:
        counts = ", ".join(f"{c.project} {c.calls}" for c in report.dynamic_calls)
        lines.append(f"Chamadas com nome não literal (sem endpoint para casar): {counts}")
    if report.external_calls:
        counts = ", ".join(f"{c.project} {c.calls}" for c in report.external_calls)
        lines.append(f"Chamadas para fora dos grafos (SoapClient, curl): {counts}")
    if by_status[SEM_CLIENTE]:
        lines.append(NOT_DEAD_CODE)
    return lines
