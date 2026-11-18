-- DDL do recorte zend1-mini: as tabelas que as entidades e o SQL do recorte citam.
-- Sintético: nomes e colunas inventados, sem dado.
CREATE TABLE arquivo_alerta (id serial PRIMARY KEY, id_arquivo integer);
CREATE TABLE contas_contabeis (
    id serial PRIMARY KEY,
    numero_conta varchar(20),
    dt_inicio_vigencia varchar(6),
    dt_fim_vigencia varchar(6)
);
CREATE TABLE cliente (id serial PRIMARY KEY);
CREATE TABLE pedido (id serial PRIMARY KEY, vl_total numeric, vl_desconto numeric);
CREATE TABLE pedido_itens (id serial PRIMARY KEY, id_pedido integer);
