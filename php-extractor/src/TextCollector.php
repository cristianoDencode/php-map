<?php

declare(strict_types=1);

namespace PhpMap\Extractor;

/**
 * Textos escritos para pessoas (schema_version 3): `comment`, `html`, `js_string`
 * e `literal`. É a matéria-prima da busca textual; o filtro de texto repetido
 * (licença, títulos padrão) fica no índice, não aqui.
 *
 * `comment` e `html` vêm dos tokens (`token_get_all`), então existem mesmo sem AST
 * (`parse_error`). `literal` vem do `CollectorVisitor`.
 */
final class TextCollector
{
    public const MAX_ITEM_CHARS = 300;
    public const MAX_ITEMS = 400;
    public const MAX_BYTES = 32768;

    /** "Frase": duas sequências de letras separadas por espaço. */
    private const PHRASE_RE = '/\p{L}{2,}\s+\p{L}{2,}/u';
    private const LETTER_RE = '/\p{L}/u';
    private const TAG_RE = '/<!--.*?-->|<[^>]*>/su';
    private const BLOCK_RE = '/<(script|style)\b[^>]*>(.*?)<\/\1\s*>/isu';
    private const JS_STRING_RE = '/\'((?:[^\'\\\\\n]|\\\\.)*)\'|"((?:[^"\\\\\n]|\\\\.)*)"/su';
    private const JS_COMMENT_RE = '/\/\*.*?\*\/|^[ \t]*\/\/[^\n]*/msu';
    /** Tags de docblock seguidas de tipo (e variável): `@param int $x descrição` → `descrição`. */
    private const TYPED_TAGS = [
        'param' => true, 'var' => true, 'property' => true, 'property-read' => true,
        'property-write' => true, 'global' => true, 'return' => true, 'throws' => true,
    ];

    /**
     * `comment`, `html` e `js_string` a partir dos tokens do arquivo (já em UTF-8).
     *
     * @param list<array{0: int, 1: int, 2: string}> $ranges escopos (início, fim, nome) de `declares`
     * @return list<array{kind: string, text: string, line: int, scope: string}>
     */
    public static function fromTokens(string $code, array $ranges): array
    {
        $items = [];
        $html = '';
        $line = 1;
        // `@`: comentário sem fechamento gera warning (o parser já registra o erro em `errors`).
        foreach (@token_get_all($code) as $token) {
            if (!is_array($token)) {
                $html .= self::blank($token);
                continue;
            }
            [$id, $text, $line] = $token;
            if ($id === T_INLINE_HTML) {
                $html .= $text;
                continue;
            }
            // O código PHP some da "visão HTML", mas as quebras de linha ficam (linhas corretas).
            $html .= self::blank($text);
            if ($id === T_COMMENT || $id === T_DOC_COMMENT) {
                $clean = self::comment($text);
                if ($clean !== '') {
                    $items[] = self::item('comment', $clean, $line, $ranges);
                }
            }
        }
        foreach (self::fromHtml($html) as [$kind, $text, $at]) {
            $items[] = self::item($kind, $text, $at, $ranges);
        }
        return $items;
    }

    /**
     * Texto de um literal PHP se ele tiver "frase" (senão `null`): tags HTML removidas, sem
     * terminar em `.php`.
     */
    public static function literalText(string $value): ?string
    {
        if (strpos($value, ' ') === false && strpos($value, "\t") === false
            && strpos($value, "\n") === false && strpos($value, '<') === false
            && strpos($value, '&') === false
        ) {
            return null; // sem espaço não há frase (atalho: a maioria dos literais é identificador)
        }
        $text = self::normalize((string) preg_replace(self::TAG_RE, ' ', $value));
        return self::isPhrase($text) ? $text : null;
    }

    /**
     * Ordena por linha, remove `(kind, text)` repetidos (fica a 1ª linha) e aplica os limites.
     *
     * @param list<array{kind: string, text: string, line: int, scope: string}> $items
     * @return array{0: list<array{kind: string, text: string, line: int, scope: string}>, 1: bool}
     */
    public static function finish(array $items): array
    {
        $indexed = [];
        foreach ($items as $i => $item) {
            $indexed[] = [$item['line'], $i, $item];
        }
        sort($indexed); // (linha, ordem de coleta): estável também no PHP 7.4
        $out = [];
        $seen = [];
        $bytes = 0;
        foreach ($indexed as [, , $item]) {
            $key = $item['kind'] . "\0" . $item['text'];
            if (isset($seen[$key])) {
                continue;
            }
            $size = strlen($item['text']);
            if (count($out) >= self::MAX_ITEMS || $bytes + $size > self::MAX_BYTES) {
                return [$out, true];
            }
            $seen[$key] = true;
            $bytes += $size;
            $out[] = $item;
        }
        return [$out, false];
    }

    /**
     * @param list<array{0: int, 1: int, 2: string}> $ranges
     * @return array{kind: string, text: string, line: int, scope: string}
     */
    public static function item(string $kind, string $text, int $line, array $ranges): array
    {
        if (mb_strlen($text, 'UTF-8') > self::MAX_ITEM_CHARS) {
            $text = rtrim(mb_substr($text, 0, self::MAX_ITEM_CHARS, 'UTF-8'));
        }
        return ['kind' => $kind, 'text' => $text, 'line' => $line, 'scope' => self::scopeAt($line, $ranges)];
    }

    /** Escopo mais interno (menor intervalo) que contém a linha; `''` no topo do arquivo. */
    private static function scopeAt(int $line, array $ranges): string
    {
        $best = '';
        $size = PHP_INT_MAX;
        foreach ($ranges as [$start, $end, $name]) {
            if ($start <= $line && $line <= $end && $end - $start < $size) {
                $best = $name;
                $size = $end - $start;
            }
        }
        return $best;
    }

    // ---------------------------------------------------------------- HTML e JS

    /** @return list<array{0: string, 1: string, 2: int}> (kind, texto, linha) */
    private static function fromHtml(string $html): array
    {
        if (trim($html) === '') {
            return [];
        }
        $out = [];
        // <script>/<style> saem do HTML; do script ficam só os literais com frase.
        $withoutBlocks = preg_replace_callback(
            self::BLOCK_RE,
            static function (array $m) use (&$out, $html): string {
                if (strtolower($m[1][0]) === 'script') {
                    foreach (self::jsStrings($m[2][0], $m[2][1], $html) as $found) {
                        $out[] = $found;
                    }
                }
                return self::blank($m[0][0]);
            },
            $html,
            -1,
            $count,
            PREG_OFFSET_CAPTURE
        );
        $html = is_string($withoutBlocks) ? $withoutBlocks : $html;
        // Cada trecho entre tags é um item, com a linha onde o texto começa.
        $parts = preg_split(self::TAG_RE, $html, -1, PREG_SPLIT_OFFSET_CAPTURE);
        foreach ($parts === false ? [] : $parts as [$chunk, $offset]) {
            $text = self::normalize($chunk);
            if ($text === '' || preg_match(self::LETTER_RE, $text) !== 1) {
                continue;
            }
            $lead = strlen($chunk) - strlen(ltrim($chunk));
            $out[] = ['html', $text, self::lineOf($html, $offset + $lead)];
        }
        return $out;
    }

    /** @return list<array{0: string, 1: string, 2: int}> */
    private static function jsStrings(string $script, int $base, string $html): array
    {
        // Comentários JS saem antes (um apóstrofo num comentário abriria uma "string").
        $script = (string) preg_replace_callback(
            self::JS_COMMENT_RE,
            static function (array $m): string {
                return self::blank($m[0]);
            },
            $script
        );
        if (preg_match_all(self::JS_STRING_RE, $script, $matches, PREG_SET_ORDER | PREG_OFFSET_CAPTURE) === false) {
            return [];
        }
        $out = [];
        foreach ($matches as $match) {
            $raw = isset($match[2]) && $match[2][1] >= 0 ? $match[2] : $match[1];
            $text = self::literalText(stripcslashes($raw[0]));
            if ($text !== null) {
                $out[] = ['js_string', $text, self::lineOf($html, $base + $raw[1])];
            }
        }
        return $out;
    }

    // ---------------------------------------------------------------- comentários

    private static function comment(string $raw): string
    {
        if (strncmp($raw, '#', 1) === 0) {
            $body = substr($raw, 1);
        } elseif (strncmp($raw, '//', 2) === 0) {
            $body = substr($raw, 2);
        } else {
            $body = (string) preg_replace('/^\/\*+|\*+\/$/', '', $raw);
        }
        $lines = [];
        foreach (preg_split('/\R/u', $body) ?: [] as $line) {
            $line = (string) preg_replace('/^\s*\*+/', '', $line);
            $line = self::docTag(trim($line));
            if ($line !== '') {
                $lines[] = $line;
            }
        }
        $text = self::normalize(implode(' ', $lines));
        return preg_match(self::LETTER_RE, $text) === 1 ? $text : '';
    }

    /** `@param int $x descrição` → `descrição`; `@author Fulano` → `Fulano`; `@return int` → ''. */
    private static function docTag(string $line): string
    {
        if (preg_match('/^@([\w-]+)\s*(.*)$/su', $line, $m) !== 1) {
            return $line;
        }
        $rest = $m[2];
        if (isset(self::TYPED_TAGS[strtolower($m[1])])) {
            $rest = (string) preg_replace('/^(?!\$)\S+\s*/u', '', $rest, 1); // tipo
            $rest = (string) preg_replace('/^&?\.{0,3}\$\w+\s*/u', '', $rest, 1); // variável
        }
        return trim($rest);
    }

    // ---------------------------------------------------------------- utilitários

    private static function normalize(string $text): string
    {
        $decoded = html_entity_decode($text, ENT_QUOTES | ENT_HTML401, 'UTF-8');
        $collapsed = preg_replace('/[\s\x{00A0}]+/u', ' ', $decoded);
        return trim(is_string($collapsed) ? $collapsed : '');
    }

    private static function isPhrase(string $text): bool
    {
        return preg_match(self::PHRASE_RE, $text) === 1 && preg_match('/\.php$/i', $text) !== 1;
    }

    /** Troca o texto por espaço, preservando as quebras de linha (e os offsets das linhas). */
    private static function blank(string $text): string
    {
        return (string) preg_replace('/[^\n]/', ' ', $text);
    }

    private static function lineOf(string $text, int $offset): int
    {
        return 1 + substr_count($text, "\n", 0, max(0, min($offset, strlen($text))));
    }
}
