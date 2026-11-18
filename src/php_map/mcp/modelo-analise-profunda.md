# Modelo do relatório de análise profunda

Redija, para um analista de negócio, o relatório dos cálculos da função (ou do campo) desta pergunta. Use **só** o dossiê que vem junto: fórmulas, dados de entrada e menus foram lidos pelo mapeador, sem IA.

Responda assim, com `SITUAÇÃO: final`:

```
SITUAÇÃO: final

RESUMO DE NEGÓCIO
(2 a 5 frases, sem nome técnico de função, tabela ou campo: o que é calculado e para quê. Conte as fórmulas por assunto (Visão geral).)

DETALHE TÉCNICO
## Cálculos
## Dados de entrada e onde são cadastrados
## Funções auxiliares
## O que não foi possível rastrear
```

O que escrever em cada seção do detalhe técnico:

- **Cálculos**: para cada cálculo, a frase em português; a fórmula técnica entre crases; e `(função:linha)`. Num dossiê de campo, uma entrada por função que grava o campo.
- **Dados de entrada e onde são cadastrados**: para cada dado, o campo com o nome do dicionário; a tabela; e o menu de cadastro **exatamente como veio no dossiê**. Num dossiê de campo, comece pelo menu do próprio campo.
- **Funções auxiliares**: cada função chamada dentro de um cálculo e o que ela calcula, com `(função:linha)`. Num dossiê de campo, diga também quem usa o campo.
- **O que não foi possível rastrear**: o que o dossiê lista como "Não rastreado", os dados sem menu e o que ficou de fora de um corte `(cortado: N de M …)`.

Regras:

1. **Não invente** menu, fórmula, tabela nem campo. O que não está no dossiê vai em "O que não foi possível rastrear" ou vira um pedido ao mapeador.
2. Para as fórmulas de outra função, peça `PEDIDO: formulas(nome_da_funcao)` (em `PARA O MAPEADOR`, com `SITUAÇÃO: continua`).
3. Dúvida de negócio vai para o analista (`PERGUNTA:`), nunca para o mapeador.
4. **Quem preenche o campo** (seção «Quem preenche este campo no código PHP» ou coluna «Preenche»): há dois jeitos, a **atribuição** `$oDao->campo = expr` antes do `incluir`/`alterar` (prevalece) e o **formulário da tela**, lido do `$_POST` só quando a atribuição não preencheu. O valor é o **texto do código** (`$iTpEmpresaSocio` é a variável, não o número): nunca invente o valor resolvido. Mapa antigo sem a seção: peça o novo scan.
