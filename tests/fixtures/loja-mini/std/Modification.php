<?php
if (!function_exists('modification')) {

  function modification($sArquivo) {

    return $sArquivo;
  }
}

class Modification {

  public static function getFile($sArquivo) {

    require_once (LOJA_PATH . 'std/ModificationFile.php');
    return $sArquivo;
  }
}
