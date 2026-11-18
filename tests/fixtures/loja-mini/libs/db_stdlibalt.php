<?php
/**
 * Versao alternativa da biblioteca (inventada): declara db_query de novo, como acontece em
 * sistemas legados com copias da mesma funcao.
 */
function db_query($sql, $conexao = null) {

  $sDir = dirname(__FILE__);
  if (class_exists("TraceLog") == false) {
    require_once("{$sDir}/../model/pedido/TraceLog.model.php");
  }
  return pg_query($conexao, $sql);
}
