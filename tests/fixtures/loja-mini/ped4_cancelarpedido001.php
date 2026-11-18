<?php
/*
 * Loja Municipal (sistema inventado para testes).
 * Tela de cancelamento de pedido.
 */
require_once(modification("libs/db_stdlib.php"));
require_once(modification("libs/db_conecta.php"));
require_once(modification("libs/db_sessoes.php"));
require_once(modification("libs/db_usuariosonline.php"));
require_once(modification("dbforms/db_funcoes.php"));
require_once(modification("classes/db_pedido_classe.php"));
require_once(modification("classes/db_pedidocancelado_classe.php"));
require_once(modification("classes/db_itempedido_classe.php"));
require_once(modification("classes/db_cliente_classe.php"));
require_once(modification("classes/db_estoque_classe.php"));
require_once(modification("classes/db_parametroloja_classe.php"));

$clpedido          = new cl_pedido;
$clpedidocancelado = new cl_pedidocancelado;
$clitempedido      = new cl_itempedido;
$clcliente         = new cl_cliente;
$clparametroloja   = new cl_parametroloja;
$db_opcao          = 22;
if (isset($codpedido)) {

  $db_opcao = 1;
  $db_botao = true;
}

db_putsession("DB_itemmenu_acessado", "1");
?>
<html>
<head>
<title>Loja Municipal - Pagina Inicial</title>
<meta http-equiv="Content-Type" content="text/html; charset=iso-8859-1">
<script language="JavaScript" type="text/javascript" src="scripts/scripts.js"></script>
<script language="JavaScript" type="text/javascript" src="scripts/prototype.js"></script>
<link href="estilos.css" rel="stylesheet" type="text/css">
</head>
<body>
  <div class="container">
    <?php
  require_once(Modification::getFile("forms/db_frmcancelarpedido.php"));
  ?>
  </div>
</body>
</html>
<?php
db_menu(db_getsession("DB_id_usuario"), db_getsession("DB_modulo"));
if ($db_opcao == 22) {
    echo "<script>document.form1.pesquisar.click();</script>";
} else {
  echo "<script>js_consultaPedido({$codpedido});</script>";
}
?>
