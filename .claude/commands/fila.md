---
description: Enfileira vídeos para o /preparar — um de cada vez, cada um num agente que começa do zero, sem carregar o contexto do anterior
argument-hint: <vídeos separados por vírgula, ex. "rainha de copas, rei de paus" — vazio mostra a fila e a retoma>
allowed-tools: Bash(venv/Scripts/python.exe:*), Agent
---

Fila do /preparar: **$ARGUMENTS**

Esta conversa é a da FILA, e ela não prepara vídeo nenhum. Ela põe os
vídeos na fila, lança um agente por vídeo, um de cada vez, e confere no
disco o que cada um entregou. Quem prepara é o agente: um contexto que
nasce vazio, roda o `/preparar` inteiro num vídeo só e devolve três linhas.

Três decisões moram aqui, e as três são medição:

- **Um contexto novo por vídeo.** O próprio `/preparar` pede janela limpa:
  o passe de julgamento é a parte cara de contexto, e empilhar o segundo
  vídeo em cima do primeiro joga fora a economia que o `/revisar` foi
  desenhado para ter.
- **Agente, e não uma sessão nova da barra lateral.** Testado em 01/10/2026:
  a rotina do app que abre sessão nova roda com permissão de "perguntar
  antes", e a sessão de teste parou no primeiro comando esperando alguém
  aprovar. Numa fila que roda de madrugada, isso é parar no minuto 1 e
  ninguém ver. O agente herda o modo de permissão desta conversa.
- **Um de cada vez.** A máquina tem 7,9 GB de RAM e o `transcribe` sozinho
  chega a 3 GB. É o `fila.py` que impede o segundo: `proximo` recusa abrir
  um vídeo enquanto outro estiver `preparando`.

Tudo o que é estado mora em `work/fila.json`, e **só o `fila.py` o
escreve** — nunca à mão, nunca pelo agente. Rode sempre da pasta do
projeto, com o Python do venv.

## Passo 1 — pôr na fila

O que veio em `$ARGUMENTS`:

- **Nada** → vá direto ao passo 2. É assim que se vê a fila e que se retoma
  uma fila parada.
- **Nomes de vídeo** (com ou sem extensão), separados por vírgula, ponto e
  vírgula ou linha, ou caminhos completos → um argumento por vídeo, entre
  aspas:

  ```
  venv/Scripts/python.exe fila.py poe "rainha de copas" "rei de paus"
  ```

  O `fila.py` acha cada um em `C:\Users\marip\Videos`, e recusa três
  coisas: o que não existe (e sugere os parecidos), o que já está na fila,
  e o que já tem passe de julgamento. **Repasse ao Rafael cada linha
  `nao achei` e `pulei`, com o motivo** — vídeo que ele pediu e não entrou
  não pode sumir calado.
- **`--refazer`** junto dos nomes → passe adiante. Só quando ele pediu;
  nunca por conta própria para resolver um `pulei`.
- **"o que falta"**, "todos" → `fila.py faltam` lista os brutos sem passe
  de julgamento, com a duração. Mostre e **pergunte antes de enfileirar**:
  são horas de máquina (ver "Quanto tempo").
- **"tira X"** → `fila.py tira "X"`. **"para a fila"** → ver "Parar".

## Passo 2 — a volta

É o mesmo passo no começo, quando um agente volta e quando o Rafael digita
`/fila` sem nada. Comece por:

```
venv/Scripts/python.exe fila.py ver
```

1. **Há um `preparando`?**
   - Você lançou o agente dele **nesta conversa** e ele ainda não voltou →
     não faça mais nada. Mostre a fila e termine o turno: o aviso de que
     ele voltou chama você de novo.
   - O agente dele acabou de voltar → siga "Quando um agente volta", que
     começa pelo `fecha` e termina aqui de novo.
   - Você **não** lançou o agente dele — foi outra conversa, ou você não
     tem certeza — → **não lance outro.** Diga ao Rafael desde quando ele
     está preparando e pergunte se a conversa que o lançou ainda está
     aberta. Só com o "pode" dele: `fila.py fecha`, que confere no disco
     até onde aquele vídeo chegou.
2. **Nada preparando e algo na fila** → `fila.py proximo`. Ele marca o
   primeiro e imprime `video`, `nome` e `duracao`. Lance o agente (modelo
   abaixo). Se o lançamento falhar, `fila.py devolve` — o vídeo volta ao
   lugar dele — e diga ao Rafael por quê.
3. **Nada preparando e nada na fila** → o resumo final (ver "Fim").

## O pedido de cada agente

`Agent` com `subagent_type: "general-purpose"`, `description: "preparar
<nome>"` e `run_in_background: true` — esta conversa fica livre para o
Rafael pôr mais vídeos enquanto ele roda. **Sem `isolation`:** numa cópia
da pasta não existem `work/` nem `venv/`, e o resultado iria parar no
lugar errado. Sem `model`: herda o desta conversa.

O prompt é este, trocando `<VIDEO>` e `<NOME>` pelo que o `proximo`
imprimiu:

```
Rode o /preparar do tarocut neste vídeo:

<VIDEO>

1. Invoque a skill `preparar` (ferramenta Skill, args: o caminho acima,
   entre aspas) e siga-a exatamente como ela está escrita, do mesmo jeito
   que numa sessão comum do /preparar. Nada a mais, nada a menos.
2. Ninguém vai responder pergunta. Onde a skill mandar parar e perguntar,
   pare ali e diga no relatório o que faltou.
3. No fim, grave em `work/<NOME>/relatorio.md` o relatório que o
   /preparar entregaria numa sessão comum.
4. Sua resposta final volta para a conversa da fila. Três linhas:
   RESULTADO: pronto   (ou: parou em <etapa> — <motivo curto>)
   AULA: <a aula em uma frase, do entendimento.md>
   CONFERIR: <o que o conferir ainda cobra, ou "nada">
```

O relatório vai para o disco, e não para esta conversa, por dois motivos:
ele é o que o Rafael lê numa sessão comum do `/preparar` e precisa
continuar existindo; e doze relatórios inteiros aqui dentro seriam a
conversa longa que a fila existe para não ter.

**O pedido é curto de propósito: a fila é o `/preparar` em fila, e só.**
Até 04/10/2026 ele tinha regras próprias, cada uma vinda de uma rodada
que demorou: não consertar as linhas do `conferir` (cavaleiro de copas,
67 min), não verificar por conta própria, uma rodada só e não ouvir
trecho (rainha de paus, 47 min). O Rafael cortou todas: "era pra rodar
exatamente a skill preparar, como ela é, só que em fila. não faz sentido
nenhum ter um comportamento diferente na fila". Se o `/preparar` demorar
ou fizer coisa demais, o conserto é no `/preparar`, que vale para as
duas formas de rodar, e não aqui.

## Quando um agente volta

1. `venv/Scripts/python.exe fila.py fecha`. **Quem diz se deu certo é o
   disco, não o agente:** o `fecha` confere se `transcript.md`,
   `entendimento.md`, `conferir.txt` e o `.m4a` foram escritos depois que a
   rodada começou, e conta no `conferir.txt` o que o passo 3 do `/preparar`
   exige zerado. Se o agente disse `pronto` e o `fecha` disse outra coisa,
   conte as duas ao Rafael — e vale o `fecha`.
2. Uma linha ao Rafael, curta:
   - `pronto` → "rainha de copas: pronto. A aula: …" e onde está o
     relatório.
   - `atencao` → terminou, mas o conferir ainda cobra (diga o quê). O
     relatório diz o que o agente decidiu; quem fecha a pendência é o
     Rafael, no editor ou num `/revisar`.
   - `falhou` → em que etapa parou e o que o agente disse. Para tentar de
     novo: `/fila <nome>`. Se o vídeo já tinha passado do começo do
     julgamento, o `fila.py` vai dizer `pulei` — aí é `/fila <nome>
     --refazer`, e diga isso a ele.
3. Volte ao passo 2: é ele que lança o próximo.

## Fim

Quando a fila esvaziar: `fila.py ver`, e um resumo curto — quantos
prontos, quais pedem atenção e por quê, quais falharam e como tentar de
novo. O próximo passo de cada vídeo pronto é o de sempre: ouvir o `.m4a`
em `export/prévias/` e ajustar no editor.

## O que dizer ao Rafael no começo

Curto, uma coisa por linha:

- a ordem da fila, com a duração de cada um, e o que não entrou e por quê;
- qual começou agora;
- quanto tempo, mais ou menos (ver abaixo);
- que **esta conversa tem de ficar aberta**: é ela que começa o próximo
  vídeo quando um termina. Fechada, a fila para — e volta com `/fila`;
- que o computador não pode dormir, e que rodar outro `transcribe` por
  fora enquanto isso disputa a mesma memória;
- se esta conversa **não** estiver no modo automático: cada comando novo
  do agente vai pedir aprovação aqui, e a fila fica parada até alguém
  aprovar.

## Quanto tempo

MEDIDO pelos horários dos arquivos dos 33 vídeos já preparados (01/10/2026):
o `/preparar` leva de 3 a 4 vezes a duração do bruto. O 9 de paus (5 min)
levou 21 min; o naipe de ouros (27 min), 1h43. Há rodadas bem mais longas
na lista, mas eram sessões em que alguém conversava no meio.

## Parar

- **"para a fila"** → `fila.py tira --todos`: saem os que não começaram. O
  que está preparando termina — parar um `/preparar` no meio deixa o
  julgamento pela metade.
- **"para agora"**, insistindo → pare o agente (`TaskStop`) e rode
  `fila.py fecha`, que registra até onde ele chegou.

## O que não fazer

- **Dois agentes ao mesmo tempo, nunca.** Nem para "adiantar": a memória
  não cabe, e a falha não se anuncia — é saída 255 sem uma linha de erro.
- **Preparar vídeo nesta conversa, nunca.** Seria trazer para cá o
  contexto que a fila existe para não acumular.
- **Julgar por cima do agente, não.** Quem julga o conteúdo é o `/revisar`
  dentro dele. A fila só confere o que foi entregue.
- **Escrever `work/fila.json` à mão, não.** O `fila.py` é quem garante que
  só um vídeo fica `preparando`.
