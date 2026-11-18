<?php

declare(strict_types=1);

namespace PhpMap\Extractor;

/**
 * Leitor léxico do JavaScript do `<script>` inline das telas (schema_version 10).
 *
 * É o porte de `src/php_map/ingest/js_scan.py` (leia a docstring de lá): literais `'…'`,
 * `"…"` e `` `…` `` fora de comentários e de expressões regulares; `${…}` vira o curinga `{}`;
 * o byte `\x00` marca o código PHP entre dois trechos de HTML (dentro de string vira `{}`). A
 * tabela `tests/fixtures/js/lexer_cases.json` roda nas duas implementações.
 */
final class JsLexer
{
    public const WILDCARD = '{}';
    public const PHP_GAP = "\x00";
    private const HTTP_PATH_RE = '~^/?[a-z0-9-]+(/[A-Za-z0-9_{}.-]*)+/?$~';
    private const IDENT_RE = '/\G[A-Za-z0-9_$]+/';
    private const REGEX_AFTER = '(,=:[!&|?{};+-*%<>~^';
    private const REGEX_WORDS = [
        'return' => true, 'typeof' => true, 'case' => true, 'do' => true, 'else' => true,
        'in' => true, 'of' => true, 'new' => true, 'delete' => true, 'void' => true,
        'throw' => true,
    ];
    private const SCRIPT_RE = '~<script\b[^>]*>(.*?)(?:</script\s*>|\z)~is';

    /**
     * Junta os trechos de HTML do arquivo (com `\x00` e as quebras de linha do PHP entre eles,
     * para as linhas baterem) e devolve as URLs com cara de rota dos corpos de `<script>`.
     *
     * @param list<array{0: string, 1: int}> $chunks `[texto, linha inicial]` em ordem
     * @return list<array{text: string, line: int}>
     */
    public static function inlineHttpLiterals(array $chunks): array
    {
        if ($chunks === []) {
            return [];
        }
        $text = '';
        $line = 1;
        foreach ($chunks as [$chunk, $start]) {
            $text .= self::PHP_GAP . str_repeat("\n", max(0, $start - $line));
            $line = max($line, $start);
            $text .= $chunk;
            $line += substr_count($chunk, "\n");
        }
        if (stripos($text, '<script') === false
            || preg_match_all(self::SCRIPT_RE, $text, $matches, PREG_OFFSET_CAPTURE | PREG_SET_ORDER) === false
        ) {
            return [];
        }
        $found = [];
        $seen = [];
        foreach ($matches as $match) {
            [$body, $offset] = $match[1];
            if ($body === '') {
                continue;
            }
            $bodyLine = 1 + substr_count(substr($text, 0, $offset), "\n");
            foreach (self::literals($body, $bodyLine) as $literal) {
                $value = self::httpCandidate($literal['text']);
                $key = "{$value}@{$literal['line']}";
                if ($value !== null && !isset($seen[$key])) {
                    $seen[$key] = true;
                    $found[] = ['text' => $value, 'line' => $literal['line']];
                }
            }
        }
        return $found;
    }

    /** O literal sem o curinga inicial (a base da URL), se tem cara de caminho de API. */
    public static function httpCandidate(string $text): ?string
    {
        $value = trim($text);
        while (strncmp($value, self::WILDCARD, 2) === 0) {
            $value = (string) substr($value, 2);
        }
        $head = preg_split('/[?#]/', $value, 2)[0];
        return $head !== '' && preg_match(self::HTTP_PATH_RE, $head) === 1 ? $value : null;
    }

    /**
     * Literais de string (linha = a de abertura), sem os aninhados em `${…}`.
     *
     * @return list<array{text: string, line: int}>
     */
    public static function literals(string $text, int $startLine = 1): array
    {
        $literals = [];
        $n = strlen($text);
        $i = 0;
        $line = $startLine;
        $counted = 0;  // até onde as quebras de linha já foram contadas
        $prev = '';
        $word = '';
        while ($i < $n) {
            $c = $text[$i];
            $next = $i + 1 < $n ? $text[$i + 1] : '';
            if ($c === '/' && $next === '/') {
                $end = strpos($text, "\n", $i);
                $i = $end === false ? $n : $end;
                continue;
            }
            if ($c === '/' && $next === '*') {
                $end = strpos($text, '*/', $i + 2);
                $i = $end === false ? $n : $end + 2;
                continue;
            }
            if ($c === "'" || $c === '"' || $c === '`') {
                $line += substr_count($text, "\n", $counted, $i - $counted);
                $counted = $i;
                [$value, $end] = $c === '`' ? self::template($text, $i) : self::quoted($text, $i);
                $literals[] = ['text' => $value, 'line' => $line];
                $i = $end;
                $prev = 'a';
                continue;
            }
            if ($c === '/' && (($prev !== '' && strpos(self::REGEX_AFTER, $prev) !== false)
                || $prev === '' || ($prev === 'a' && isset(self::REGEX_WORDS[$word])))
            ) {
                $i = self::regexEnd($text, $i);
                $prev = 'a';
                $word = '';
                continue;
            }
            if (preg_match(self::IDENT_RE, $text, $m, 0, $i) === 1) {
                $word = $m[0];
                $i += strlen($word);
                $prev = 'a';
                continue;
            }
            if (!ctype_space($c) && $c !== self::PHP_GAP) {
                $prev = ($c === ')' || $c === ']') ? 'a' : $c;
                $word = '';
            }
            $i++;
        }
        return $literals;
    }

    /** @return array{0: string, 1: int} (valor, posição depois do fim) */
    private static function quoted(string $text, int $i): array
    {
        $quote = $text[$i];
        $j = $i + 1;
        $n = strlen($text);
        $buf = '';
        while ($j < $n) {
            $ch = $text[$j];
            if ($ch === '\\' && $j + 1 < $n) {
                if ($text[$j + 1] !== "\n") {
                    $buf .= $text[$j + 1];
                }
                $j += 2;
                continue;
            }
            if ($ch === $quote) {
                return [$buf, $j + 1];
            }
            if ($ch === "\n") {
                return [$buf, $j];
            }
            $buf .= $ch === self::PHP_GAP ? self::WILDCARD : $ch;
            $j++;
        }
        return [$buf, $n];
    }

    /** @return array{0: string, 1: int} */
    private static function template(string $text, int $i): array
    {
        $j = $i + 1;
        $n = strlen($text);
        $buf = '';
        while ($j < $n) {
            $ch = $text[$j];
            if ($ch === '\\' && $j + 1 < $n) {
                $buf .= $text[$j + 1];
                $j += 2;
                continue;
            }
            if ($ch === '`') {
                return [$buf, $j + 1];
            }
            if ($ch === '$' && $j + 1 < $n && $text[$j + 1] === '{') {
                $j = self::expressionEnd($text, $j + 2);
                $buf .= self::WILDCARD;
                continue;
            }
            $buf .= $ch === self::PHP_GAP ? self::WILDCARD : $ch;
            $j++;
        }
        return [$buf, $n];
    }

    private static function expressionEnd(string $text, int $j): int
    {
        $depth = 1;
        $n = strlen($text);
        while ($j < $n) {
            $ch = $text[$j];
            if ($ch === "'" || $ch === '"') {
                $j = self::quoted($text, $j)[1];
                continue;
            }
            if ($ch === '`') {
                $j = self::template($text, $j)[1];
                continue;
            }
            if ($ch === '{') {
                $depth++;
            } elseif ($ch === '}') {
                $depth--;
                if ($depth === 0) {
                    return $j + 1;
                }
            }
            $j++;
        }
        return $n;
    }

    private static function regexEnd(string $text, int $i): int
    {
        $j = $i + 1;
        $n = strlen($text);
        $inClass = false;
        while ($j < $n) {
            $ch = $text[$j];
            if ($ch === "\n") {
                return $j;
            }
            if ($ch === '\\') {
                $j += 2;
                continue;
            }
            if ($ch === '[') {
                $inClass = true;
            } elseif ($ch === ']') {
                $inClass = false;
            } elseif ($ch === '/' && !$inClass) {
                return preg_match(self::IDENT_RE, $text, $m, 0, $j + 1) === 1 ? $j + 1 + strlen($m[0]) : $j + 1;
            }
            $j++;
        }
        return $n;
    }
}
