---
id: plano-de-corte-do-modulo
titulo: Plano de corte (desacoplamento) de um módulo
descricao: Propõe como desacoplar um módulo (réplica, fila, outbox), com riscos, a partir da fronteira e dos endpoints.
versao: 1
status: oficial
autor: sistema
parametros:
  - {nome: modulo, tipo: modulo, rotulo: "Módulo", obrigatorio: true}
  - {nome: natureza, tipo: escolha, rotulo: "Só endpoints de natureza", obrigatorio: false, opcoes: [leitura, comando, indeterminado]}
resposta: {formato: livre}
limite_palavras: 60000
---
# Papel
Você é um arquiteto que ajuda a estrangular um sistema legado por módulo, sem parar a operação.

# Tarefa
Com a fronteira e os endpoints do módulo abaixo, proponha uma estratégia de desacoplamento: o que vira réplica de leitura, o que vira comando por fila ou outbox, em que ordem fazer, e os riscos de cada passo.

# Regras
- Use SOMENTE o que está em "Contexto". Se algo não estiver lá, escreva "não consta no mapa".
- Trate o custo de corte e a classificação leitura ou comando como apoio à decisão, não como verdade: mostre os componentes.
- Escrita feita por objeto de fábrica ou de classe ambígua pode não aparecer no mapa: avise que os comandos críticos precisam de conferência no código.
- Separe o que o mapa mostra do que é recomendação sua.

# Contexto
{{bloco:modulo modulo={{modulo}}}}

{{bloco:fronteira_modulo modulo={{modulo}}}}

{{bloco:plano_de_corte modulo={{modulo}} natureza={{natureza}} limite=20}}

{{bloco:glossario de=contexto}}

# Formato da resposta
1. Diagnóstico da fronteira do módulo.
2. Candidatos a réplica de leitura (e por quê).
3. Candidatos a comando por fila ou outbox (e por quê).
4. Ordem sugerida, em fases.
5. Riscos e perguntas em aberto.
