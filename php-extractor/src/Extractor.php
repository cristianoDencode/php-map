<?php

declare(strict_types=1);

namespace PhpMap\Extractor;

use PhpParser\Error;
use PhpParser\ErrorHandler\Collecting;
use PhpParser\NodeTraverser;
use PhpParser\NodeVisitor\NameResolver;
use PhpParser\Parser;
use PhpParser\ParserFactory;
use PhpParser\PhpVersion;
use Throwable;

/**
 * Extrai o registro (schema_version 12) de um arquivo PHP do e-cidade.
 */
final class Extractor
{
    public const SCHEMA_VERSION = 12;

    private Parser $newestParser;
    private Parser $php74Parser;

    public function __construct()
    {
        $factory = new ParserFactory();
        $this->newestParser = $factory->createForNewestSupportedVersion();
        $this->php74Parser = $factory->createForVersion(PhpVersion::fromComponents(7, 4));
    }

    /** @return array<string, mixed> */
    public function extractFile(string $root, string $relativePath): array
    {
        $record = [
            'schema_version' => self::SCHEMA_VERSION,
            'path' => $relativePath,
            'status' => 'ok',
            'errors' => [],
            'sha1' => null,
            'lines' => 0,
            'encoding_fallback' => false,
            'declares' => [],
            'includes' => [],
            'calls' => [],
            'dynamic' => [],
            'sql_strings' => [],
            'string_refs' => [],
            'dispatch' => [],
            'routes' => [],
            'outbound' => [],
            'bindings' => [],
            'form_fields' => [],
            'http_literals' => [],
            'has_api_prefix' => false,
            'texts' => [],
            'texts_truncated' => false,
        ];

        $fullPath = rtrim($root, '/') . '/' . $relativePath;
        $raw = is_file($fullPath) ? @file_get_contents($fullPath) : false;
        if ($raw === false) {
            $record['status'] = 'read_error';
            $record['errors'][] = 'arquivo não encontrado ou ilegível';
            return $record;
        }
        $record['sha1'] = sha1($raw);
        $record['lines'] = $raw === '' ? 0 : substr_count($raw, "\n") + (substr($raw, -1) === "\n" ? 0 : 1);

        // Fontes do e-cidade são ISO-8859-1: converter antes do parse deixa todas as strings em
        // UTF-8. O mesmo teste serve ao perfil zend1: o NFSe é majoritariamente
        // UTF-8, mas misto, então o registro conta quando este arquivo caiu para a conversão.
        $isUtf8 = mb_check_encoding($raw, 'UTF-8');
        $code = $isUtf8 ? $raw : mb_convert_encoding($raw, 'UTF-8', 'ISO-8859-1');
        $record['encoding_fallback'] = !$isUtf8;
        // O arquivo monta a base da API (`PHPSession.requestApi`, `'v4/api'`)?
        $record['has_api_prefix'] = preg_match('~requestApi|v4/api~', $code) === 1;

        [$stmts, $errors] = $this->parse($code);
        foreach ($errors as $error) {
            $record['errors'][] = $error->getMessage();
        }
        if ($stmts === null) {
            $record['status'] = 'parse_error';
            // Sem AST ainda há tokens: comentários e HTML.
            [$record['texts'], $record['texts_truncated']] = TextCollector::finish(
                TextCollector::fromTokens($code, [])
            );
            return $record;
        }
        if ($errors !== []) {
            $record['status'] = 'partial';
        }

        $resolver = new NameResolver(new Collecting(), ['replaceNodes' => true]);
        $collector = new CollectorVisitor($resolver->getNameContext());
        $traverser = new NodeTraverser();
        $traverser->addVisitor($resolver);
        $traverser->addVisitor($collector);
        try {
            $traverser->traverse($stmts);
        } catch (Throwable $e) {
            $record['status'] = 'partial';
            $record['errors'][] = 'falha na travessia: ' . $e->getMessage();
        }

        $record['declares'] = $collector->declares;
        $record['includes'] = $collector->includes;
        $record['calls'] = $collector->calls;
        $record['dynamic'] = $collector->dynamic;
        $record['sql_strings'] = $collector->sqlStrings;
        $record['string_refs'] = $collector->stringRefs;
        $record['dispatch'] = $collector->dispatch;
        $record['routes'] = $collector->routes;
        $record['outbound'] = $collector->outbound;
        $record['bindings'] = $collector->bindings;
        $record['form_fields'] = $collector->formFields;
        $record['http_literals'] = JsLexer::inlineHttpLiterals($collector->inlineHtml);
        $texts = array_merge(
            TextCollector::fromTokens($code, self::scopeRanges($collector->declares)),
            $collector->literalTexts()
        );
        [$record['texts'], $record['texts_truncated']] = TextCollector::finish($texts);
        return $record;
    }

    /**
     * Intervalos de linha das funções e métodos, no formato de `scope` dos outros campos.
     *
     * @param list<array<string, mixed>> $declares
     * @return list<array{0: int, 1: int, 2: string}>
     */
    private static function scopeRanges(array $declares): array
    {
        $ranges = [];
        foreach ($declares as $declare) {
            if ($declare['kind'] === 'function') {
                $ranges[] = [$declare['line_start'], $declare['line_end'], "function:{$declare['name']}"];
            } elseif ($declare['kind'] === 'method') {
                $ranges[] = [
                    $declare['line_start'],
                    $declare['line_end'],
                    "method:{$declare['class']}::{$declare['name']}",
                ];
            }
        }
        return $ranges;
    }

    /**
     * Gramática mais nova primeiro; se houver erro, tenta a do PHP 7.4 (legado que usa palavras
     * reservadas depois, ex.: função chamada match) e fica com o resultado de menos erros.
     *
     * @return array{0: ?array, 1: list<Error>}
     */
    private function parse(string $code): array
    {
        $best = $this->parseWith($this->newestParser, $code);
        if ($best[1] === []) {
            return $best;
        }
        $legacy = $this->parseWith($this->php74Parser, $code);
        if ($legacy[0] !== null && ($best[0] === null || count($legacy[1]) < count($best[1]))) {
            return $legacy;
        }
        return $best;
    }

    /** @return array{0: ?array, 1: list<Error>} */
    private function parseWith(Parser $parser, string $code): array
    {
        $handler = new Collecting();
        try {
            $stmts = $parser->parse($code, $handler);
        } catch (Throwable $e) {
            return [null, [new Error($e->getMessage())]];
        }
        return [$stmts, array_values($handler->getErrors())];
    }
}
