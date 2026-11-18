import json
from pathlib import Path

import pytest

from php_map.errors import IngestError
from php_map.ingest.php_extractor import read_cache
from php_map.ingest.records import Origin, PhpFileRecord, parse_record

REFERENCE = Path(__file__).resolve().parents[1] / "fixtures/ast/loja-mini.jsonl"


@pytest.fixture(scope="module")
def records() -> dict[str, PhpFileRecord]:
    return {r.path: r for r in read_cache(REFERENCE)}


def test_reference_jsonl_validates(records: dict[str, PhpFileRecord]) -> None:
    assert len(records) == 25
    assert records["lixo_parse_error.php"].status in {"parse_error", "partial"}
    entry = records["ped4_cancelarpedido001.php"]
    assert len(entry.includes) == 12
    origin = entry.includes[-1].var_types["clpedido"]
    assert isinstance(origin, Origin) and origin.kind == "new" and origin.class_ == "cl_pedido"


def test_class_field_is_mapped_from_json(records: dict[str, PhpFileRecord]) -> None:
    news = [c for c in records["ped4_cancelarpedido001.php"].calls if c.kind == "new"]
    assert news[0].class_ == "cl_pedido"
    method = next(d for d in records["classes/pedido.php"].declares if d.kind == "method")
    assert method.class_ == "pedido"


def _reference_line() -> dict[str, object]:
    lines = REFERENCE.read_text(encoding="utf-8").splitlines()
    first = next(line for line in lines if '"path":"classes/pedido.php"' in line)
    data = json.loads(first)
    assert isinstance(data, dict)
    return data


def test_other_schema_version_is_rejected() -> None:
    data = _reference_line()
    data["schema_version"] = 13
    with pytest.raises(IngestError, match="schema_version 13"):
        parse_record(json.dumps(data))


def test_contract_drift_is_rejected() -> None:
    data = _reference_line()
    data["campo_novo"] = True
    with pytest.raises(IngestError, match="inválido"):
        parse_record(json.dumps(data))


def test_crlf_line_is_accepted() -> None:
    assert parse_record(json.dumps(_reference_line()) + "\r\n").schema_version == 12


def test_class_props_php_empty_array_and_v10_records_are_accepted() -> None:
    """O PHP devolve `[]` (não `{}`) sem propriedades; cache v10 não tem o campo."""
    line = _reference_line()
    for declare in line["declares"]:
        declare["class_props"] = []
    record = parse_record(json.dumps(line))
    assert all(d.class_props == {} for d in record.declares)
    legacy = _reference_line()
    legacy["schema_version"] = 10
    for declare in legacy["declares"]:
        declare.pop("class_props", None)
        declare.pop("display_name", None)
    assert parse_record(json.dumps(legacy)).schema_version == 10
    line["declares"][0]["class_props"] = {"table": "x"}
    assert parse_record(json.dumps(line)).declares[0].class_props == {"table": "x"}


def test_cache_errors_point_to_line(tmp_path: Path) -> None:
    cache = tmp_path / "php.jsonl"
    cache.write_text(json.dumps(_reference_line()) + "\n{}\n", encoding="utf-8")
    with pytest.raises(IngestError, match=r"php\.jsonl:2"):
        list(read_cache(cache))


def test_missing_cache_is_ingest_error(tmp_path: Path) -> None:
    with pytest.raises(IngestError, match="inexistente"):
        list(read_cache(tmp_path / "nada.jsonl"))


def test_schema_v2_origins_and_factory_arguments(records: dict[str, PhpFileRecord]) -> None:
    pedido = records["classes/pedido.php"]
    (cls,) = [d for d in pedido.declares if d.kind == "class"]
    assert {(p.prop, p.origin.class_) for p in cls.property_types} == {
        ("odaopedido", "cl_pedido"),
        ("odaoitem", "db_utils"),
        ("odaolog", None),
    }
    by_name = {c.name: c for c in pedido.calls if c.kind in ("method", "static")}
    assert (by_name["getdao"].arg0, by_name["getdao"].arg1, by_name["getdao"].argc) == (
        "itempedido",
        None,
        1,
    )
    assert (by_name["usardao"].arg0, by_name["usardao"].arg1) == ("pedidocancelado", True)
    assert by_name["alterar"].receiver_property == "odaopedido"
    estornar = by_name["estornar"].receiver_origin
    assert (
        estornar is not None and estornar.kind == "new" and estornar.class_ == "lancamentoestoque"
    )


def test_schema_v1_is_still_accepted() -> None:
    data = _reference_line()
    data["schema_version"] = 1
    for call in data["calls"]:  # type: ignore[attr-defined]
        for key in ("arg0", "arg1", "argc", "receiver_origin", "receiver_property"):
            call.pop(key, None)
    for decl in data["declares"]:  # type: ignore[attr-defined]
        decl.pop("property_types", None)
    data.pop("texts")
    data.pop("texts_truncated")
    assert parse_record(json.dumps(data)).schema_version == 1


def test_schema_v2_without_texts_is_still_accepted() -> None:
    data = _reference_line()
    data["schema_version"] = 2
    data.pop("texts")
    data.pop("texts_truncated")
    record = parse_record(json.dumps(data))
    assert (record.schema_version, record.texts, record.texts_truncated) == (2, [], False)


def test_schema_v3_texts(records: dict[str, PhpFileRecord]) -> None:
    entry = records["ped4_cancelarpedido001.php"]
    comment = next(t for t in entry.texts if t.line == 2)
    assert (comment.kind, comment.scope) == ("comment", "")
    assert (
        comment.text
        == "Loja Municipal (sistema inventado para testes). Tela de cancelamento de pedido."
    )
    assert entry.texts_truncated is False


def test_unknown_text_kind_is_rejected() -> None:
    data = _reference_line()
    data["texts"] = [{"kind": "css", "text": "display inline", "line": 1, "scope": ""}]
    with pytest.raises(IngestError, match="inválido"):
        parse_record(json.dumps(data))


def test_v12_sets_and_form_fields_are_read_and_v11_records_still_load() -> None:
    """`sets` nas chamadas `incluir`/`alterar` e `form_fields` por arquivo; o cache v11
    não tem os campos e continua lido."""
    line = _reference_line()
    line["form_fields"] = [{"name": "k02_juros", "widget": "db_input", "line": 79}]
    line["calls"][0]["sets"] = [
        {"field": "z03_tipoempresa", "value": "32", "kind": "literal", "line": 5}
    ]
    record = parse_record(json.dumps(line))
    assert record.form_fields[0].widget == "db_input"
    assert (record.calls[0].sets[0].value, record.calls[0].sets[0].kind) == ("32", "literal")
    legacy = _reference_line()
    legacy["schema_version"] = 11
    legacy.pop("form_fields", None)
    old = parse_record(json.dumps(legacy))
    assert old.form_fields == [] and all(call.sets == [] for call in old.calls)


def test_invalid_kind_of_set_is_rejected() -> None:
    line = _reference_line()
    line["calls"][0]["sets"] = [{"field": "a", "value": "1", "kind": "formulario", "line": 1}]
    with pytest.raises(IngestError, match="inválido"):
        parse_record(json.dumps(line))
