# Imagem do extrator PHP do php-map: PHP 7.4 CLI + composer.
# Para quem não tem a imagem do ambiente Docker do e-cidade (`ecidade-windows-php74`): Linux e CI.
#   php-map setup-extractor --build-image     # docker build -t php-map-php74 php-extractor/
# O código do extrator e o e-cidade são montados na hora (`-v`), nada é copiado para a imagem.
FROM php:7.4-cli

# O composer baixa o nikic/php-parser como zip e precisa de um `unzip`. Sem `apt-get`: a base da
# php:7.4-cli é o Debian 11, já sem suporte, e o repositório dele falha (404 em 2026-09-30).
# O `unzip` vem do busybox estático (o binário age como `unzip` pelo nome).
COPY --from=busybox:1.36-musl /bin/busybox /usr/local/bin/unzip

COPY --from=composer:2 /usr/bin/composer /usr/local/bin/composer

# Extensões que o extrator usa (já vêm na php:7.4-cli): a construção falha se faltar alguma.
RUN php -r 'foreach (["tokenizer", "json", "ctype", "mbstring", "iconv"] as $e) { if (!extension_loaded($e)) { fwrite(STDERR, "extensão ausente: $e\n"); exit(1); } }'
