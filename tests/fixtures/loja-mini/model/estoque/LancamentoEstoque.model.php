<?php
require_once(modification("model/estoque/ContratoEstoque.model.php"));
require_once(modification("model/estoque/Auditoria.model.php"));

/**
 * Lancamento de estoque (inventado): usada sem require em classes/pedido.php (autoload).
 */
class LancamentoEstoque extends BaseEstoque implements ContratoEstoque {

  public function estornar($iPedido) {

    $sSql = "update estoque set est01_saldo = est01_saldo + 1 where est01_produto in (select ite01_produto from itempedido where ite01_pedido = {$iPedido})";
    return db_query($sSql);
  }
}
