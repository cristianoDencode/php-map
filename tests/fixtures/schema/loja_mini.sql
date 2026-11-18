-- SINTETICO: DDL da loja municipal fictícia (tests/fixtures/loja-mini). Tudo inventado.

SET client_encoding = 'UTF8';

CREATE TABLE cliente (
    cli01_codigo  int4 NOT NULL,
    cli01_nome    varchar(80),
    cli01_ativo   boolean,
    CONSTRAINT cliente_pkey PRIMARY KEY (cli01_codigo)
);

CREATE TABLE produto (
    pro01_codigo     int4 NOT NULL,
    pro01_descricao  varchar(80),
    pro01_preco      numeric(12,2),
    CONSTRAINT produto_pkey PRIMARY KEY (pro01_codigo)
);

CREATE TABLE pedido (
    ped01_codigo    int4 NOT NULL,
    ped01_cliente   int4,
    ped01_data      date,
    ped01_total     numeric(12,2),
    ped01_situacao  int4,
    CONSTRAINT pedido_pkey PRIMARY KEY (ped01_codigo),
    CONSTRAINT pedido_cliente_fk FOREIGN KEY (ped01_cliente) REFERENCES cliente (cli01_codigo)
);

CREATE TABLE itempedido (
    ite01_codigo      int4 NOT NULL,
    ite01_pedido      int4 NOT NULL,
    ite01_produto     int4 NOT NULL,
    ite01_quantidade  numeric(10,2),
    CONSTRAINT itempedido_pkey PRIMARY KEY (ite01_codigo),
    CONSTRAINT itempedido_pedido_fk FOREIGN KEY (ite01_pedido) REFERENCES pedido (ped01_codigo),
    CONSTRAINT itempedido_produto_fk FOREIGN KEY (ite01_produto) REFERENCES produto (pro01_codigo)
);

CREATE TABLE estoque (
    est01_produto  int4 NOT NULL,
    est01_saldo    numeric(12,2),
    CONSTRAINT estoque_pkey PRIMARY KEY (est01_produto)
);

CREATE TABLE pedidocancelado (
    can01_codigo  int4 NOT NULL,
    can01_pedido  int4 NOT NULL,
    can01_data    date,
    can01_motivo  text,
    CONSTRAINT pedidocancelado_pkey PRIMARY KEY (can01_codigo),
    CONSTRAINT pedidocancelado_pedido_fk FOREIGN KEY (can01_pedido) REFERENCES pedido (ped01_codigo)
);

CREATE TABLE parametroloja (
    par01_exercicio  int4 NOT NULL,
    par01_permitecancelar  boolean
);

-- Tabela de auditoria generica (infra no grafo), gravada pelo trigger abaixo.
CREATE TABLE auditoria_loja (
    aud01_codigo  int4,
    aud01_tabela  text,
    aud01_antes   text,
    aud01_depois  text
);

-- Schema explicito (nao public).
CREATE TABLE varejo.promocao (
    id  int4 NOT NULL
);

CREATE OR REPLACE FUNCTION fc_saldo_estoque(integer, numeric) RETURNS numeric AS
$$
DECLARE
  p_produto    ALIAS FOR $1;
  p_quantidade ALIAS FOR $2;
  v_saldo      numeric;
  v_permite    boolean;
BEGIN

  SELECT par01_permitecancelar
    INTO v_permite
    FROM parametroloja
   LIMIT 1;

  IF p_quantidade <= 0 THEN
    RAISE EXCEPTION 'Quantidade invalida.';
  END IF;

  SELECT est01_saldo
    INTO v_saldo
    FROM estoque
   WHERE est01_produto = p_produto;

  IF v_saldo IS NULL THEN
    INSERT INTO estoque (est01_produto, est01_saldo) VALUES (p_produto, 0);
    v_saldo := 0;
  END IF;

  RETURN v_saldo - p_quantidade;
END;
$$ LANGUAGE plpgsql;

CREATE OR REPLACE FUNCTION tg_pedido_auditoria() RETURNS trigger AS
$$
BEGIN
  INSERT INTO auditoria_loja (aud01_codigo, aud01_tabela, aud01_antes, aud01_depois)
  VALUES (NEW.ped01_codigo, 'pedido', OLD.ped01_situacao::text, NEW.ped01_situacao::text);
  RETURN NEW;
END;
$$ LANGUAGE plpgsql;

CREATE TRIGGER tg_pedido_auditoria
  AFTER UPDATE ON pedido
  FOR EACH ROW EXECUTE PROCEDURE tg_pedido_auditoria();

-- Instrucao invalida de proposito, seguida de uma tabela valida.
CREATE TABEL quebrada (id int4);

CREATE TABLE transportadora (
    tra01_codigo  int4 NOT NULL,
    tra01_nome    varchar(60)
);
