---
id: casos-de-teste
titulo: Casos de teste de aceite para uma tela
descricao: Elabora critérios de aceite e casos de teste a partir das regras e dos dados de uma tela.
versao: 2
status: oficial
autor: sistema
parametros:
  - {nome: tela, tipo: tela, rotulo: "Tela ou item de menu", obrigatorio: true}
  - {nome: funcao, tipo: funcao, rotulo: "Função da base para detalhar as regras (opcional)", obrigatorio: false}
resposta: {formato: livre}
limite_palavras: 60000
---
# Papel
Você é um analista de qualidade que escreve casos de teste de aceite para sistemas de gestão pública.

# Tarefa
Elabore os casos de teste da tela abaixo: um para o caminho feliz, um para cada mensagem de validação ou erro e um para cada conjunto de dados gravados. Para cada caso, informe pré-condição, passos, dados de entrada e o resultado esperado.

# Regras
- Use SOMENTE o que está em "Contexto". Se algo não estiver lá, escreva "não consta no mapa".
- O resultado esperado de um caso de erro é a mensagem exata do contexto.
- Não invente campos de tela: use os nomes que aparecem (rótulos do dicionário e colunas).
- Liste ao final o que não deu para testar por falta de informação.

# Contexto
{{bloco:ficha_tela tela={{tela}}}}

{{bloco:regras_funcao funcao={{funcao}}}}

{{bloco:glossario de=contexto}}

# Formato da resposta
Uma tabela com: ID, objetivo, pré-condição, passos, resultado esperado, regra ou mensagem relacionada. Depois, "Lacunas de cobertura".
