<?php
/**
 * Biblioteca padrao (inventada).
 */
function db_query($sql) {

  $conexao = $GLOBALS["conn"];
  $rs      = pg_query($conexao, $sql);
  if (!$rs) {
    echo pg_last_error($conexao);
  }
  return $rs;
}

function db_putsession($nome, $valor) {

  $_SESSION[$nome] = $valor;
}

function db_getsession($nome) {

  return isset($_SESSION[$nome]) ? $_SESSION[$nome] : null;
}
