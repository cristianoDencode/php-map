<?php
  function db_inicio_transacao() {

    $rs = db_query("begin");
    return $rs;
  }

  function db_fim_transacao($lErro = false) {

    $sFim = $lErro ? "rollback" : "commit";
    return db_query($sFim);
  }
