<?php
require_once(modification("libs/db_stdlib.php"));
require_once(modification("libs/db_conecta.php"));
require_once(modification("libs/db_sessoes.php"));
include(modification("libs/JSON.php"));
require_once(modification("classes/pedido.php"));

$oJson    = new services_json();
$oParam   = $oJson->decode(str_replace("\\", "", $_POST["json"]));
$oRetorno = new stdClass();
$oRetorno->status = 1;

switch ($oParam->exec) {

  case "pesquisaPedidos":

    $oPedido           = new pedido();
    $oRetorno->aItens  = $oPedido->getPedidosLiberados($oParam);
    break;

  case "liberarPedidos":

    $oPedido = new pedido();
    try {

      db_inicio_transacao();
      $oPedido->liberar($oParam->aPedidos);
      db_fim_transacao(false);
    } catch (Exception $eErro) {

      db_fim_transacao(true);
      $oRetorno->status  = 2;
      $oRetorno->message = urlencode($eErro->getMessage());
    }
    break;
}
echo $oJson->encode($oRetorno);
?>
