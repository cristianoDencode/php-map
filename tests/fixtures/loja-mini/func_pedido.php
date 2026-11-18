<?php
require_once(modification("libs/db_stdlib.php"));
require_once(modification("libs/db_conecta.php"));
require_once(modification("classes/db_pedido_classe.php"));

$clpedido = new cl_pedido;
$clrotulo = new rotulocampo;
$clpedido->rotulo->label("ped01_codigo");
?>
<html>
<body>
<?php
$sql = $clpedido->sql_query_file(null, "ped01_codigo, ped01_data", "ped01_codigo");
db_lovrot($sql, 15, "()", "", $funcao_js);
?>
</body>
</html>
