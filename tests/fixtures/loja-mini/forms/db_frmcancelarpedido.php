<?
// Formulario de cancelamento de pedido (inventado). Arquivo em ISO-8859-1.
$clpedido->rotulo->label();
?>
<form name="form1" method="post" action="">
  <fieldset>
    <legend>Cancelação de pedido</legend>
    <table>
      <tr>
        <td><?=db_ancora("Pedido:", "js_pesquisapedido(true);", $db_opcao)?></td>
        <td><? db_input('ped01_codigo', 10, $Iped01_codigo, true, 'text', 3) ?></td>
      </tr>
    </table>
  </fieldset>
  <input name="pesquisar" type="button" value="Pesquisar" onclick="js_pesquisapedido(true);">
  <input name="cancelar" type="button" value="Cancelar pedido" onclick="js_cancelar();">
</form>
<script>
//alert(obj.itens);
function js_pesquisapedido(mostra) {
  js_OpenJanelaIframe('', 'db_iframe_pedido', 'func_pedido.php?funcao_js=parent.js_mostrapedido|ped01_codigo', 'Pesquisa', true);
}

function js_cancelar() {

  var oParam    = new Object();
  oParam.method = "cancelar";
  oParam.pedido = $F('ped01_codigo');
  var oAjax = new Ajax.Request('ped4_baixaestoque004.php', {method: 'post', parameters: 'json=' + Object.toJSON(oParam)});
  if (oParam.pedido == "") {
    alert('Não há pedido selecionado para cancelar.');
  }
}

function js_consultaPedido(codigo) {
  new Ajax.Request('ped4_baixaestoque004.php', {method: 'post', parameters: 'codigo=' + codigo});
}
</script>
<?php
if (isset($codpedido)) {
  $result = $clpedido->sql_record($clpedido->sql_query_file($codpedido));
}
?>
