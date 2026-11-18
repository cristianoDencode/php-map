<?php
require_once(modification("libs/db_stdlib.php"));
require_once(modification("libs/db_conecta.php"));
require_once(modification("libs/db_sessoes.php"));
require_once(modification("libs/JSON.php"));
require_once(modification("dbforms/db_funcoes.php"));
require_once(modification("model/estoque/ReservaEstoque.model.php"));
require_once(modification("classes/pedido.php"));

use App\Loja\Contratos\Cancelavel;

$oJson               = new services_json();
$objJson             = $oJson->decode(str_replace("\\", "", $_POST["json"]));
$objJson->erro       = false;

switch ($objJson->method) {

  case "cancelar":

    $oPedido = new pedido($objJson->pedido);
    try {

      db_inicio_transacao();
      $oPedido->cancelar($objJson->motivo);
      db_fim_transacao(false);
    } catch (Exception $eErro) {

      db_fim_transacao(true);
      $objJson->erro     = true;
      $objJson->mensagem = "Erro ao cancelar o pedido.";
    }
    break;

  case "getItens":

    $sSql  = "select ite01_produto, pro01_descricao, ite01_quantidade, est01_saldo ";
    $sSql .= "  from itempedido ";
    $sSql .= "       inner join produto on pro01_codigo = ite01_produto ";
    $sSql .= "       inner join pedido  on ped01_codigo = ite01_pedido ";
    $sSql .= "       left  join estoque on est01_produto = ite01_produto ";
    $sSql .= "       left  join cliente on cli01_codigo = ped01_cliente ";
    $sSql .= " where ite01_pedido = {$objJson->pedido}";
    $rsItens = db_query($sSql);
    $objJson->itens = pg_fetch_all($rsItens);
    break;

  case "getEntregas":

    $rsEntregas = db_query("select * from entrega where ent01_pedido = " . (int)$objJson->pedido);
    $objJson->entregas = pg_fetch_all($rsEntregas);
    break;
}
echo $oJson->encode($objJson);
?>
