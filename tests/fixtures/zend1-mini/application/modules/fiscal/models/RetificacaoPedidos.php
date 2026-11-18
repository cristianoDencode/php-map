<?php
// [fixture] SQL de verdade (`from pedido x`): tem de seguir pelo pglast, nunca pelo caminho da
// DQL.
class Fiscal_Model_RetificacaoPedidos
{
    public function buscar()
    {
        $sSql = "select x.vl_total, x.vl_desconto
      from pedido x
      inner join pedido_itens y
        on y.id_pedido = x.id";
        return $sSql;
    }
}
