<?php

declare(strict_types=1);

/*
 * Extrator de AST do e-cidade para o php-map.
 *
 * Uso: php -d short_open_tag=1 bin/extract.php --root=/src [--files-from=-|lista.txt]
 *   stdin/lista: caminhos relativos à raiz, um por linha.
 *   stdout: JSONL, um registro por arquivo, na ordem de entrada.
 *   stderr: progresso e resumo.
 * Saída 0 mesmo com erros de arquivo; 2 = erro de uso; 3 = erro de ambiente.
 */

use PhpMap\Extractor\Extractor;

const PROGRESS_EVERY = 500;
const JSON_FLAGS = JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES | JSON_INVALID_UTF8_SUBSTITUTE;

function fail(string $message, int $code): void
{
    fwrite(STDERR, "extract.php: {$message}\n");
    exit($code);
}

$autoload = __DIR__ . '/../vendor/autoload.php';
if (!is_file($autoload)) {
    fail('dependências ausentes: rode "composer install" em php-extractor/ (php-map setup-extractor)', 3);
}
require $autoload;

// Warnings/notices no stdout corromperiam o JSONL (`token_get_all` avisa sobre
// comentário sem fechamento): mensagens do PHP vão para o stderr.
ini_set('display_errors', 'stderr');

// 1.899 de 2.433 forms do e-cidade abrem com "<?": sem short_open_tag o arquivo vira HTML sem erro.
if (!filter_var(ini_get('short_open_tag'), FILTER_VALIDATE_BOOLEAN)) {
    fail('short_open_tag está desligado; rode com "php -d short_open_tag=1"', 3);
}

$options = getopt('', ['root:', 'files-from::']);
if (!is_array($options) || !isset($options['root']) || !is_string($options['root'])) {
    fail('uso: php -d short_open_tag=1 bin/extract.php --root=/src [--files-from=-|lista.txt]', 2);
}
$root = $options['root'];
if (!is_dir($root)) {
    fail("raiz inexistente: {$root}", 2);
}
$filesFrom = isset($options['files-from']) && is_string($options['files-from']) ? $options['files-from'] : '-';
$input = $filesFrom === '-' ? STDIN : @fopen($filesFrom, 'rb');
if ($input === false) {
    fail("não foi possível abrir a lista: {$filesFrom}", 2);
}

$extractor = new Extractor();
$counts = ['ok' => 0, 'partial' => 0, 'parse_error' => 0, 'read_error' => 0];
$total = 0;
$started = microtime(true);

while (($line = fgets($input)) !== false) {
    $path = ltrim(str_replace('\\', '/', trim($line)), '/');
    if (strncmp($path, './', 2) === 0) {
        $path = substr($path, 2);
    }
    if ($path === '') {
        continue;
    }

    $record = $extractor->extractFile($root, $path);
    $json = json_encode($record, JSON_FLAGS);
    if ($json === false) {
        $json = json_encode([
            'schema_version' => Extractor::SCHEMA_VERSION,
            'path' => $path,
            'status' => 'read_error',
            'errors' => ['falha ao serializar o registro: ' . json_last_error_msg()],
            'sha1' => $record['sha1'],
            'lines' => $record['lines'],
            'encoding_fallback' => $record['encoding_fallback'],
            'declares' => [], 'includes' => [], 'calls' => [], 'dynamic' => [],
            'sql_strings' => [], 'string_refs' => [], 'dispatch' => [],
            'texts' => [], 'texts_truncated' => false,
        ], JSON_FLAGS);
        $record['status'] = 'read_error';
    }
    fwrite(STDOUT, $json . "\n");

    $counts[$record['status']]++;
    $total++;
    unset($record, $json);
    if ($total % PROGRESS_EVERY === 0) {
        gc_collect_cycles();
        fwrite(STDERR, sprintf("extract.php: %d arquivos (%.1fs)\n", $total, microtime(true) - $started));
    }
}

fwrite(STDERR, sprintf(
    "extract.php: fim: %d arquivos em %.1fs | ok=%d partial=%d parse_error=%d read_error=%d\n",
    $total,
    microtime(true) - $started,
    $counts['ok'],
    $counts['partial'],
    $counts['parse_error'],
    $counts['read_error']
));
exit(0);
