<?php
/**
 * Regras de negocio do pedido (inventado).
 */
require_once(modification("model/pedido/PedidoCliente.model.php"));

class pedido {

  protected $iCodigo;
  protected $oDaoPedido;
  protected $oDaoItem;

  public function __construct($iCodigo = null) {

    require_once(modification("classes/db_pedido_classe.php"));
    $this->iCodigo    = $iCodigo;
    $this->oDaoPedido = new cl_pedido();
    $this->oDaoItem   = db_utils::getDao("itempedido");
    $this->oDaoLog    = $this->usarDao("pedidocancelado", true);
  }

  public function cancelar($sMotivo) {

    $sSaldo = "select fc_saldo_estoque(ite01_produto, ite01_quantidade) from itempedido where ite01_pedido = {$this->iCodigo}";
    $rsSaldo = db_query($sSaldo);
    if (pg_num_rows($rsSaldo) == 0) {
      throw new Exception("Pedido sem itens.");
    }

    // revisado hoje
    $this->oDaoPedido->ped01_situacao = 9;
    $this->oDaoPedido->ped01_codigo   = $this->iCodigo;
    $this->oDaoPedido->alterar($this->iCodigo);

    $oLancamento = new LancamentoEstoque();
    $oLancamento->estornar($this->iCodigo);

    $this->oDaoLog->can01_pedido = $this->iCodigo;
    $this->oDaoLog->can01_motivo = $sMotivo;
    $this->oDaoLog->incluir(null);

    return true;
  }

  public function getPedidosLiberados($oParam) {

    $sSql = "select ped01_codigo, ped01_total from pedido where ped01_situacao = 1";
    $rs   = db_query($sSql);
    return pg_fetch_all($rs);
  }

  public function liberar($aPedidos) {

    foreach ($aPedidos as $iPedido) {
      db_query("update pedido set ped01_situacao = 2 where ped01_codigo = " . (int)$iPedido);
    }
  }

  private function usarDao($sClasse, $lInstancia = false) {

    include_once(modification("classes/db_{$sClasse}_classe.php"));
    $sNome = "cl_" . $sClasse;
    return eval("return new {$sNome}();");
  }
}
