"""RPC por ação de `switch` e chamadas de saída (`SoapClient`, `curl`).

Registros à mão no formato do extrator (schema_version 7); sem Docker.
"""

from typing import Any

from conftest import PhpRecords

from php_map.config import Settings
from php_map.graph.builder import GraphBuilder
from php_map.graph.model import EdgeSource, EdgeType, NodeLabel
from php_map.ingest.records import PhpFileRecord
from php_map.profiles.base import RemoteCallPattern
from php_map.resolve.endpoints import apply_endpoints
from php_map.resolve.outbound import destination
from php_map.resolve.unresolved import UnresolvedWriter

HELPER = RemoteCallPattern("integracaonfsehelper", frozenset({"request"}), "zend1app")


def _run(
    records: list[PhpFileRecord], **settings: Any
) -> tuple[GraphBuilder, UnresolvedWriter, Any]:
    builder, unresolved = GraphBuilder(), UnresolvedWriter()
    config = Settings(endpoint_system="ecidade", rpc_endpoints=True, **settings)
    stats = apply_endpoints(records, builder, unresolved, config)
    unresolved.close()
    return builder, unresolved, stats


def _dispatch(
    actions: list[tuple[str, int, list[str]]], outside: list[str] | None = None
) -> dict[str, Any]:
    return {
        "subject": "$oParam->exec",
        "cases": [a[0] for a in actions],
        "line": 1,
        "scope": "",
        "actions": [{"name": n, "line": ln, "params": p} for n, ln, p in actions],
        "outside_params": outside or [],
    }


def _edges(builder: GraphBuilder, type_: EdgeType) -> dict[tuple[str, str], Any]:
    return {(e.src, e.dst): e for e in builder.iter_edges() if e.type == type_}


def _endpoints(builder: GraphBuilder, prefix: str) -> list[str]:
    return sorted(
        n.id
        for n in builder.iter_nodes()
        if n.label == NodeLabel.ENDPOINT and n.id.startswith(prefix)
    )


# ------------------------------------------------------------------ RPC


def test_each_dispatch_action_becomes_an_endpoint_with_its_params(php: PhpRecords) -> None:
    record = php.record(
        "abastecimento_requisicao.RPC.php",
        dispatch=[_dispatch([("processar", 52, ["valor", "iCodigo"]), ("listar", 90, [])])],
    )

    builder, _unresolved, stats = _run([record])

    eid = "Endpoint:ecidade:rpc:abastecimento_requisicao.RPC.php#processar"
    attrs = builder.node_attrs(eid)
    assert (attrs["protocol"], attrs["kind"], attrs["line"], attrs["action"]) == (
        "rpc",
        "rpc_action",
        52,
        "processar",
    )
    assert attrs["params"] == [
        {"name": "valor", "required": False, "type": None, "source": "php_ast"},
        {"name": "iCodigo", "required": False, "type": None, "source": "php_ast"},
    ]
    edge = _edges(builder, EdgeType.SERVES)[("File:abastecimento_requisicao.RPC.php", eid)]
    assert (edge.source, edge.confidence) == (EdgeSource.PHP_AST, 1.0)
    assert dict(edge.evidence)["line"] == 52
    assert builder.node_attrs(eid.replace("processar", "listar"))["params"] == []  # é válido
    rpc = stats.rpc
    assert (rpc.actions, rpc.with_params, rpc.params, rpc.files) == (2, 1, 2, 1)


def test_the_same_action_in_two_switches_is_one_endpoint_with_the_union_of_params(
    php: PhpRecords,
) -> None:
    record = php.record(
        "x.RPC.php",
        dispatch=[_dispatch([("salvar", 10, ["a"])]), _dispatch([("salvar", 80, ["a", "b"])])],
    )

    builder, _unresolved, stats = _run([record])

    attrs = builder.node_attrs("Endpoint:ecidade:rpc:x.RPC.php#salvar")
    assert [p["name"] for p in attrs["params"]] == ["a", "b"] and attrs["line"] == 10
    assert stats.rpc.actions == 1 and stats.rpc.switches == 2


def test_params_read_outside_the_switch_are_counted_and_never_linked(php: PhpRecords) -> None:
    record = php.record(
        "y.RPC.php", dispatch=[_dispatch([("a", 5, ["x"])], outside=["iEmpenho", "iAno"])]
    )

    builder, _unresolved, stats = _run([record])

    params = builder.node_attrs("Endpoint:ecidade:rpc:y.RPC.php#a")["params"]
    assert [p["name"] for p in params] == ["x"]
    assert stats.rpc.param_outside_case == 2


def test_rpc_files_without_a_literal_action_are_counted(php: PhpRecords) -> None:
    records = [
        php.record("com_acao.RPC.php", dispatch=[_dispatch([("a", 1, [])])]),
        php.record("sem_acao.RPC.php"),
        php.record("outra_tela.php"),  # não é `*.RPC.php`
    ]

    _builder, _unresolved, stats = _run(records)

    assert (stats.rpc.named_files, stats.rpc.named_files_without_action) == (2, 1)


def test_a_profile_without_rpc_makes_no_rpc_endpoints(php: PhpRecords) -> None:
    record = php.record("z.RPC.php", dispatch=[_dispatch([("a", 1, [])])])
    builder, unresolved = GraphBuilder(), UnresolvedWriter()

    apply_endpoints([record], builder, unresolved, Settings(endpoint_system="zend1app"))

    assert _endpoints(builder, "Endpoint:") == []


# ------------------------------------------------------------------ chamadas de saída


def _out(
    kind: str, callee: str, url: dict[str, Any] | None, scope: str = "", line: int = 10
) -> dict[str, Any]:
    return {"kind": kind, "callee": callee, "line": line, "scope": scope, "url": url}


def _s(value: str) -> dict[str, Any]:
    return {"t": "s", "v": value}


def test_literal_wsdl_becomes_an_unresolved_external_endpoint_with_the_host(
    php: PhpRecords,
) -> None:
    wsdl = _s("http://usuario:senha@ws.receita.gov.br:8080/a/b?wsdl")
    record = php.record("model/Receita.model.php", outbound=[_out("soap", "soapclient", wsdl)])

    builder, unresolved, stats = _run([record])

    eid = "Endpoint:externo:soap:ws.receita.gov.br:8080/a/b"  # sem esquema, credencial nem query
    attrs = builder.node_attrs(eid)
    assert (attrs["resolved"], attrs["system"], attrs["protocol"]) == (False, "externo", "soap")
    assert attrs["url"].endswith("/a/b?wsdl")
    edge = _edges(builder, EdgeType.CALLS_REMOTE)[("File:model/Receita.model.php", eid)]
    assert (edge.source, edge.confidence) == (EdgeSource.PHP_AST, 1.0)
    assert not unresolved.counts and (stats.outbound.literal, stats.outbound.dynamic) == (1, 0)


def test_non_literal_destination_is_a_question_mark_with_where_it_came_from(
    php: PhpRecords,
) -> None:
    env = {"t": "f", "n": "env", "a": [_s("NFSE_LOCATION")]}
    record = php.record(
        "src/Lib/Cliente.php",
        outbound=[
            _out("http", "curl_init", {"t": "v", "n": "sUrl"}, "method:cliente::envia", 7),
            _out("soap", "soapclient", env, "", 12),
        ],
    )

    builder, unresolved, stats = _run([record])

    calls = _edges(builder, EdgeType.CALLS_REMOTE)
    http = calls[("Method:cliente::envia", "Endpoint:externo:http:?")]
    assert (http.source, http.confidence) == (EdgeSource.PHP_AST_AMBIGUOUS, 0.5)
    assert dict(http.evidence)["via"] == "$sUrl"
    soap = calls[("File:src/Lib/Cliente.php", "Endpoint:externo:soap:?")]
    assert dict(soap.evidence)["via"] == "env:NFSE_LOCATION"  # nunca se lê o `.env`
    assert unresolved.counts["dynamic_endpoint"] == 2
    assert builder.node_attrs("Endpoint:externo:http:?")["resolved"] is False
    assert (stats.outbound.dynamic, stats.outbound.unknown_target) == (2, 2)


def test_the_integration_has_precedence_over_an_external_duplicate(php: PhpRecords) -> None:
    """O `new SoapClient($wsdl)` do `IntegracaoNfseHelper` já é a integração SOAP."""
    helper = "app" + chr(92) + "domain" + chr(92) + "integracaonfsehelper"
    record = php.record(
        "app/Domain/IntegracaoNfseHelper.php",
        declares=[php.decl("class", helper)],
        outbound=[_out("soap", "soapclient", {"t": "v", "n": "wsdl"}, f"method:{helper}::request")],
    )

    builder, unresolved, stats = _run([record], remote_calls=(HELPER,))

    assert _endpoints(builder, "Endpoint:externo:") == []
    assert not unresolved.counts and stats.outbound.skipped_integration == 1


def test_curl_init_without_url_is_dropped_when_the_same_scope_sets_the_url(
    php: PhpRecords,
) -> None:
    scope = "method:envio::enviar"
    record = php.record(
        "envio.php",
        outbound=[
            _out("http", "curl_init", None, scope, 5),
            _out("http", "curl_setopt", _s("https://api.orgao.gov.br/v1/envio"), scope, 6),
            _out("http", "curl_init", None, "method:outro::sem_url", 20),  # fica: não há URL
        ],
    )

    builder, _unresolved, stats = _run([record])

    assert _endpoints(builder, "Endpoint:externo:") == [
        "Endpoint:externo:http:?",
        "Endpoint:externo:http:api.orgao.gov.br/v1/envio",
    ]
    assert stats.outbound.calls == 2


def test_destination_uses_the_literal_prefix_of_a_concatenation() -> None:
    prefix = {"t": "c", "p": [_s("https://api.x.gov.br/v1/"), {"t": "v", "n": "recurso"}]}
    assert destination(prefix) == ("https://api.x.gov.br/v1/", None)
    # sem esquema no começo não há host: o que importa é de onde vem o resto
    assert destination({"t": "c", "p": [{"t": "v", "n": "base"}, _s("/ws")]}) == (None, "$base")
    assert destination(_s("arquivo_local.wsdl")) == (None, "literal:arquivo_local.wsdl")
    assert destination(None) == (None, None)
    assert destination({"t": "?", "s": "$this->url"}) == (None, "$this->url")
