<?php
class Services_JSON {

  function encode($var) {
    return json_encode($var);
  }

  function decode($str) {
    return json_decode($str);
  }
}
