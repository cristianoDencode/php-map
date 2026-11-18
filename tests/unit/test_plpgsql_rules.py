"""Leitor léxico das regras de negócio (`ingest/plpgsql_rules.py`)."""

from php_map.ingest.plpgsql_rules import RoutineProfile, Rule, scan_routine


def _wrap(body: str, header: str = "CREATE FUNCTION public.fc_x() RETURNS void") -> str:
    return f"{header}\n LANGUAGE plpgsql\nAS $function$\n{body}\n$function$;\n"


def _only(rules: list[Rule], kind: str) -> list[Rule]:
    return [r for r in rules if r.kind == kind]


def test_raise_the_three_levels() -> None:
    ddl = _wrap(
        "BEGIN\n"
        "  RAISE EXCEPTION 'erro sem guarda';\n"
        "  RAISE WARNING 'aviso sem guarda';\n"
        "  RAISE NOTICE 'diagnostico';\n"
        "  RAISE INFO 'diagnostico 2';\n"
        "  RAISE DEBUG 'diagnostico 3';\n"
        "  RAISE LOG 'diagnostico 4';\n"
        "END;"
    )
    rules, _profile = scan_routine(ddl)
    kinds = [r.kind for r in rules]
    assert kinds == ["error", "warning"] + ["diagnostic"] * 4
    assert [r.level for r in rules] == ["EXCEPTION", "WARNING", "NOTICE", "INFO", "DEBUG", "LOG"]
    assert rules[0].message == "erro sem guarda"
    assert rules[0].condition is None


def test_raise_using_message() -> None:
    ddl = _wrap(
        "BEGIN\n  RAISE EXCEPTION USING MESSAGE = 'erro via using', ERRCODE = '22012';\nEND;"
    )
    [rule] = scan_routine(ddl)[0]
    assert rule.kind == "error"
    assert rule.message == "erro via using"
    assert rule.sqlstate == "22012"


def test_raise_bare_reraise() -> None:
    ddl = _wrap("BEGIN\n  RAISE;\nEND;")
    [rule] = scan_routine(ddl)[0]
    assert rule.kind == "error"
    assert rule.level == "EXCEPTION"
    assert rule.message is None


def test_condition_nested_three_levels_and_else() -> None:
    ddl = _wrap(
        "BEGIN\n"
        "  IF a > 0 THEN\n"
        "    IF b > 0 THEN\n"
        "      IF c > 0 THEN\n"
        "        RAISE EXCEPTION 'tudo positivo';\n"
        "      ELSE\n"
        "        RAISE EXCEPTION 'c nao positivo';\n"
        "      END IF;\n"
        "    END IF;\n"
        "  END IF;\n"
        "END;"
    )
    rules, _profile = scan_routine(ddl)
    validations = _only(rules, "validation")
    assert validations[0].condition == "a > 0 E b > 0 E c > 0"
    assert validations[1].condition == "a > 0 E b > 0 E SENÃO de c > 0"


def test_elsif_chain() -> None:
    ddl = _wrap(
        "BEGIN\n"
        "  IF a = 1 THEN\n"
        "    RAISE NOTICE 'um';\n"
        "  ELSIF a = 2 THEN\n"
        "    RAISE NOTICE 'dois';\n"
        "  ELSE\n"
        "    RAISE NOTICE 'outro';\n"
        "  END IF;\n"
        "END;"
    )
    rules, _profile = scan_routine(ddl)
    diag = _only(rules, "diagnostic")
    assert [r.condition for r in diag] == ["a = 1", "a = 2", "SENÃO de a = 2"]


def test_case_statement_is_a_decision_but_case_expression_is_not() -> None:
    ddl = _wrap(
        "BEGIN\n"
        "  CASE v\n"
        "    WHEN 1 THEN\n"
        "      RAISE NOTICE 'um';\n"
        "    WHEN 2 THEN\n"
        "      RAISE NOTICE 'dois';\n"
        "  END CASE;\n"
        "  x := CASE WHEN v = 1 THEN 'a' ELSE 'b' END;\n"
        "END;"
    )
    rules, _profile = scan_routine(ddl)
    decisions = _only(rules, "decision")
    assert [r.condition for r in decisions] == ["1", "2"]
    assert _only(rules, "diagnostic")[0].condition == "1"


def test_exception_when_a_or_b() -> None:
    ddl = _wrap(
        "BEGIN\n"
        "  RAISE NOTICE 'corpo';\n"
        "EXCEPTION WHEN unique_violation OR foreign_key_violation THEN\n"
        "  RAISE NOTICE 'tratado';\n"
        "END;"
    )
    rules, _profile = scan_routine(ddl)
    [handler] = _only(rules, "handler")
    assert handler.condition == "unique_violation OR foreign_key_violation"


def test_text_with_double_dash_semicolon_and_doubled_quote() -> None:
    ddl = _wrap("BEGIN\n  RAISE NOTICE 'contem -- nao e comentario; e tem '' aspas';\nEND;")
    [rule] = scan_routine(ddl)[0]
    assert rule.message == "contem -- nao e comentario; e tem ' aspas"


def test_text_with_nested_dollar_tag() -> None:
    ddl = _wrap(
        "BEGIN\n"
        "  EXECUTE $q$select * from x where y = $$1$$$q$;\n"
        "  RAISE NOTICE 'depois do dinamico';\n"
        "END;"
    )
    rules, profile = scan_routine(ddl)
    assert _only(rules, "diagnostic")[0].message == "depois do dinamico"
    assert profile.counts["dynamic_sql"] == 1


def test_nested_block_comment() -> None:
    ddl = _wrap("BEGIN\n  /* fora /* dentro */ ainda fora */\n  RAISE NOTICE 'x';\nEND;")
    rules, _profile = scan_routine(ddl)
    [comment] = _only(rules, "comment")
    assert comment.message == "fora /* dentro */ ainda fora"


def test_comment_vs_commented_code_vs_header() -> None:
    ddl = _wrap(
        "BEGIN\n"
        "  -- Verifica se o contribuinte possui debito em aberto\n"
        "  -- select * from x where y = 1;\n"
        "  -- Autor: fulano\n"
        "  -- ok\n"
        "  RAISE NOTICE 'x';\n"
        "END;"
    )
    rules, profile = scan_routine(ddl)
    comments = _only(rules, "comment")
    assert len(comments) == 1
    assert comments[0].message == "Verifica se o contribuinte possui debito em aberto"
    assert profile.counts["commented_code"] == 1
    assert profile.counts["header_comment"] == 1
    assert profile.counts["short_comment"] == 1


def test_dynamic_message_with_concatenation() -> None:
    ddl = _wrap("BEGIN\n  RAISE EXCEPTION 'Saldo ' || v_saldo || ' insuficiente';\nEND;")
    [rule] = scan_routine(ddl)[0]
    assert rule.dynamic is True
    assert rule.message == "Saldo  …  insuficiente"


def test_long_condition_is_truncated() -> None:
    long_cond = "a = " + "1" * 400
    ddl = _wrap(f"BEGIN\n  IF {long_cond} THEN\n    RAISE NOTICE 'x';\n  END IF;\nEND;")
    rules, _profile = scan_routine(ddl)
    decision = _only(rules, "decision")[0]
    assert decision.truncated is True
    assert len(decision.condition or "") == 300


def test_params_default_out_and_variadic() -> None:
    ddl = (
        "CREATE FUNCTION public.fc_y(IN a integer, OUT b text, "
        "INOUT c numeric DEFAULT 1.5, VARIADIC d text[]) RETURNS record\n"
        " LANGUAGE plpgsql\nAS $f$\nBEGIN\n  RAISE NOTICE 'x';\nEND;\n$f$;\n"
    )
    _rules, profile = scan_routine(ddl)
    by_name = {p[0]: p for p in profile.params}
    assert by_name["a"][1] == "IN"
    assert by_name["b"][1] == "OUT"
    assert by_name["c"] == ("c", "INOUT", "numeric", "1.5")
    assert by_name["d"][1] == "VARIADIC"


def test_profile_header_fields_and_declared() -> None:
    ddl = (
        "CREATE FUNCTION public.fc_z() RETURNS trigger\n"
        " LANGUAGE plpgsql\n STABLE\n SECURITY DEFINER\nAS $f$\n"
        "DECLARE\n  v_saldo numeric;\n  v_nome text := 'x';\n"
        "BEGIN\n  RAISE NOTICE 'x';\nEND;\n$f$;\n"
    )
    rules, profile = scan_routine(ddl)
    assert rules  # a trigger function também ganha regras (o parser PL/pgSQL nem é tocado)
    assert profile.returns == "trigger"
    assert profile.language == "plpgsql"
    assert profile.volatility == "stable"
    assert profile.security_definer is True
    assert profile.declared == ("v_saldo", "v_nome")


def test_loops_and_performs_are_counted() -> None:
    ddl = _wrap("BEGIN\n  FOR i IN 1..10 LOOP\n    PERFORM fc_outra(i);\n  END LOOP;\nEND;")
    _rules, profile = scan_routine(ddl)
    assert profile.counts["loops"] == 1
    assert profile.counts["performs"] == 1


def test_line_numbers_match_the_ddl() -> None:
    ddl = (
        "CREATE FUNCTION public.fc_w() RETURNS void\n"  # linha 1
        " LANGUAGE plpgsql\n"  # linha 2
        "AS $f$\n"  # linha 3
        "BEGIN\n"  # linha 4
        "  RAISE NOTICE 'x';\n"  # linha 5
        "END;\n"  # linha 6
        "$f$;\n"
    )
    [rule] = scan_routine(ddl)[0]
    assert rule.line == 5
    assert rule.end_line == 5


def test_function_without_dollar_body_has_no_rules_but_has_profile() -> None:
    ddl = "CREATE FUNCTION public.fc_v() RETURNS integer LANGUAGE sql AS 'select 1';\n"
    rules, profile = scan_routine(ddl)
    assert rules == []
    assert isinstance(profile, RoutineProfile)
    assert profile.language == "sql"
