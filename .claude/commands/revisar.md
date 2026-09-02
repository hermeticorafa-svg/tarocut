---
description: Passe de julgamento de um vídeo do tarocut - lê a transcrição, escreve anotacoes.json e confere que o corte obedeceu
argument-hint: <caminho do vídeo ou nome curto, ex. IMG_0247>
allowed-tools: Bash(venv/Scripts/python.exe:*), Read, Write, Edit
---

Faça o passe de julgamento do vídeo: **$ARGUMENTS**

Você é a única parte deste programa que lê o que ela DISSE. Todo o resto
decide por regra e por energia de áudio. O que você acrescenta é o que
nenhuma heurística pega: interrupção, raciocínio refeito com outras
palavras, trecho em que ela se perde.

## A economia deste comando

Este comando existe para rodar numa janela limpa. Cada resposta relê a
conversa inteira, então **leia só dois arquivos**: `work/<video>/revisao.txt`
e `work/<video>/conferir.txt`. Nada mais.

Não abra `stream.json` (32 mil tokens contra 1,3 mil do `revisao.txt`), não
abra `transcript.md`, não abra `tarocut.py`. Se sentir falta de algo que só
o código explica, isso é sinal de que você está tentando consertar um bug —
veja a fase 5.

Rode tudo da pasta do projeto, com o Python do venv. O nome curto
(`IMG_0247`) funciona tanto quanto o caminho completo.

## Fase 1 — gerar e ler

```
venv\Scripts\python.exe tarocut.py revisar $ARGUMENTS
```

Leia o `work/<video>/revisao.txt` que sair. O cabeçalho explica as marcas;
as três que mais importam para você:

- `[?? N.Ns]` — som alto que ninguém conseguiu ler, **mantido de propósito**
  para você decidir. Toda ocorrência precisa de uma resposta sua: sai ou
  fica.
- `[!Ns/N palavras]` — o que sobra ali é fala rarefeita. Quase sempre é
  parada longa, retomada ou ela se perdendo. Olhe cada um.
- `[-N.Ns VOZ]` — vai sair, mas tem som alto dentro. Confira que não é
  frase inteira prestes a sumir.

E uma regra dura:

- `palavra~` — o tempo dessa palavra **não é confiável**; o alinhador
  recusou o próprio resultado. Ela pode estar segundos longe de onde
  aparece. **Nunca ancore borda de corte numa palavra com `~`.** Se o
  trecho que você quer cortar começa ou termina numa delas, ancore no
  `[-N.Ns]` mais próximo e deixe a fase 4 ajustar.

## Fase 2 — julgar

Procure, nesta ordem:

1. **Interrupção** — alguém entra, ela fala com outra pessoa, cachorro,
   celular. Sai inteiro.
2. **Raciocínio refeito com outras palavras** — ela diz uma coisa, para, e
   diz de novo trocando as palavras. Repetição literal o programa já pega
   sozinho; esta não casa, e por isso é sua.
3. **Ela se perde** — começa uma frase que não vai a lugar nenhum e retoma
   depois. Descarte a tentativa, guarde a boa.
4. **Fim e começo** — sobra de gravação: ela ajeitando a câmera, olhando
   para os lados, saindo de quadro.
5. Os `[?? N.Ns]` e `[!Ns/N palavras]` que ainda não estiverem cobertos.

Na dúvida, **deixe ficar**. Cacoete que sobra se resolve com uma anotação
depois; frase apagada não se resolve, porque ninguém fica sabendo que
existiu.

## Fase 3 — escrever

`work/<video>/anotacoes.json`:

```json
[{"ini": 228.1, "fim": 231.4, "motivo": "interrupcao domestica"}]
```

O `motivo` é para uma pessoa ler daqui a seis meses: diga o que ela abandona
e o que sobra. Sem acento (o resto do projeto é assim).

**Onde pôr a borda.** O programa encolhe a anotação para dentro até cair em
fronteira de palavra. Se você mirar no meio de uma palavra, **essa palavra
sobrevive** — foi assim que um `reis,` ficou pendurado em "as rainhas estão
acima dos reis". Então:

- mire dentro de um `[-N.Ns]`, que é silêncio já cortado: ali a borda não
  atravessa palavra nenhuma nem come a vizinha;
- se não houver um, use o tempo no começo da linha como âncora e conte que
  a fase 4 vai corrigir;
- para tirar uma palavra, a anotação tem que **conter** a palavra inteira,
  não encostar nela.

**`anotacoes.json` é sagrado.** Se o arquivo já existir:

- copie antes de escrever, uma vez só nesta sessão, para
  `anotacoes.json.bak-<AAAAMMDD-HHMM>`;
- **nunca apague uma anotação existente nem reescreva o `motivo` dela**;
- acrescente as suas ao fim;
- mover `ini`/`fim` de uma anotação antiga é permitido **só** quando a fase
  4 provar que ela não foi obedecida — e aí diga no relatório o que moveu e
  por quê.

## Fase 4 — fechar o laço (não pule)

Escrever a anotação não é o mesmo que ela ter valido. Já houve quatro
caminhos diferentes pelos quais uma anotação foi lida, aplicada e sumiu no
cálculo do corte, sem uma linha de aviso. Você não tem como saber se foi
obedecida a menos que pergunte:

```
venv\Scripts\python.exe tarocut.py conferir $ARGUMENTS
```

**O critério de terminar é binário: zero linhas `ANOTACAO NAO OBEDECIDA`.**
Enquanto houver uma, você não terminou.

Se ele abortar dizendo que o `transcript.md` está desatualizado, rode
`tarocut.py draft $ARGUMENTS` e tente de novo.

Para cada `ANOTACAO NAO OBEDECIDA`, a linha diz `sobra Xs de Ys`. Diagnostique:

| o que você vê | causa provável | o que fazer |
|---|---|---|
| sobra ~0,1–0,2s, nas pontas | borda caiu no meio de uma palavra e encolheu | alargue a anotação até conter a palavra inteira |
| sobra um pedaço grande no meio | há palavra mantida dentro do trecho | alargue, ou parta a anotação em duas em volta dela |
| a região tem `~` no `revisao.txt` | o alinhador embaralhou os tempos ali | reancore em `[-N.Ns]` vizinho; se não houver, pare e reporte |
| sobra e nada disso explica | provavelmente não é sua culpa | pare e reporte (fase 5) |

Ajuste, rode `conferir` de novo. **No máximo 3 rodadas.** Se na terceira
ainda sobrar, pare e reporte o que sobrou — laço infinito é pior que achado
honesto.

As outras linhas do `conferir` (`ILHA CURTA`, `FALA CORTADA SOBREVIVE`,
`PALAVRA RASPADA`) **não são sua responsabilidade** e nem sempre são
defeito. Só olhe se estiverem dentro de um trecho que você anotou.

Se precisar traduzir um tempo da prévia em áudio para o tempo da fonte:
`tarocut.py conferir $ARGUMENTS --em 3:05`.

## Fase 5 — relatório

Termine com três blocos curtos:

1. **O que anotei** — uma linha por anotação: tempo, o que sai, por quê.
2. **O que o `conferir` diz** — confirme "zero ANOTACAO NAO OBEDECIDA", ou
   liste o que sobrou e por quê você parou.
3. **Achados para o programador** — se alguma coisa parecer defeito de
   código, escreva aqui, **nunca dentro do campo `motivo`**.

Essa última regra existe porque já aconteceu: duas das 19 anotações do
IMG_0247 são diagnóstico de código escrito em prosa dentro do `motivo`
("PARCIAL - so tira o 'e' de 49.8-50.6...", "O corte mecanico entrava em
55.43..."). O arquivo passou a mentir sobre o motivo do corte, e o achado
não chegou a quem podia consertar. Julgamento de conteúdo vai no arquivo;
defeito de programa vai no relatório.
