<?php
require_once(modification("classes/pedido.php"));

/**
 * Pedido de um cliente (inventado): usa a classe de negocio com require explicito.
 */
class PedidoCliente extends pedido {

  public function getTotal() {
    return db_query("select sum(ite01_quantidade) from itempedido where ite01_pedido = {$this->iCodigo}");
  }
}
