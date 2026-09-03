---
description: Pipeline completo de um vídeo do tarocut — passada mecânica, passe de julgamento (revisar), conferir e áudio — deixa o transcript.md pronto pro editor de texto
argument-hint: <nome do arquivo em C:\Users\marip\Videos, ex. "7 de espadas.mov" — ou um caminho completo, se o vídeo estiver em outro lugar>
allowed-tools: Bash(venv/Scripts/python.exe:*), Read, Write, Edit, Skill
---

Prepare o vídeo **$ARGUMENTS** de ponta a ponta: da passada mecânica até o
áudio pronto pra ouvir. No fim, o `transcript.md` está pronto pro editor de
texto de verdade (Notepad, VSCode, o que você usa) — este comando não abre
o `editor` do navegador nem renderiza vídeo; isso fica pro passo 3 e pro
`render` do `README.md`, depois que você tiver feito o ajuste fino.

Isso é os passos 1, 2, 2b e 3b (áudio) do `README.md`, num comando só. Rode
numa janela nova do Claude Code, como faria com `/revisar` sozinho — o
passe de julgamento é a parte cara de contexto, e empilhar isso em cima de
uma conversa longa já existente joga fora a economia de tokens que o
`/revisar` foi desenhado para ter (ver "A economia deste comando" em
`.claude/commands/revisar.md`).

## Passo 0 — resolver o caminho

`$ARGUMENTS` pode vir de dois jeitos:

- **já é um caminho** — tem `\`, `/` ou letra de unidade (`C:`) — use
  exatamente como veio, sem mexer.
- **é só o nome do arquivo**, com extensão (ex.: `7 de espadas.mov`) — todo
  vídeo deste projeto mora em `C:\Users\marip\Videos`, então monte o
  caminho completo grudando esse nome ali:
  `C:\Users\marip\Videos\7 de espadas.mov`.

Chame o resultado disso de VIDEO — é o que entra em todo comando dos passos
1 a 4 daqui pra frente, sempre entre aspas se tiver espaço (nome de vídeo
deste projeto costuma ter). Não confunda com o "nome curto" que o resto do
`tarocut.py` aceita (o mesmo nome sem a extensão): use o caminho completo
em tudo aqui, é sempre válido e evita reabrir essa conta a cada passo.

Se o arquivo não existir em `C:\Users\marip\Videos`, pare e pergunte o
caminho certo — não adivinhe outra pasta.

## Passo 1 — passada mecânica

```
venv\Scripts\python.exe tarocut.py run VIDEO
```

Isso é `ingest → transcribe → align → analyze → draft`. Transcrever demora
(~1/3 da duração do vídeo); o resto é rápido. Cada etapa tem cache, então
rodar de novo num vídeo já processado não retranscreve.

Confirme que saiu `work/<video>/transcript.md` antes de seguir. Se o
comando falhar, pare aqui e reporte o erro — não adianta julgar uma
transcrição que não existe.

## Passo 2 — passe de julgamento

Invoque a skill `revisar` com o mesmo vídeo (`Skill` com `skill: "revisar"`
e `args: VIDEO`). Siga as cinco fases de lá — leitura do
`revisao.txt`, julgamento, escrita em `anotacoes.json`, o laço de
`analyze --force` / `draft --force` / `conferir` até zero `ANOTACAO NAO
OBEDECIDA`, e o relatório final.

Não pule nem resuma essa parte por fora: é a única etapa deste pipeline
que **julga conteúdo**; tudo o resto é mecânico.

## Passo 3 — conferir, de novo, para o registro

O passe acima já fechou o laço sozinho, mas rode mais uma vez, limpo, para
o `work/<video>/conferir.txt` refletir o estado final combinado:

```
venv\Scripts\python.exe tarocut.py conferir VIDEO
```

Não gera nada, é rápido (lê o corte já calculado, não recalcula). Confirme
de novo: zero `ANOTACAO NAO OBEDECIDA`. Se sobrar alguma, isso é bug — pare
e reporte, não force o passo 4.

## Passo 4 — exportar áudio

```
venv\Scripts\python.exe tarocut.py audio VIDEO
```

~50s para um vídeo de poucos minutos, um `.m4a` de poucos MB — o corte
inteiro, só em áudio, pronto pra ouvir enquanto você faz o ajuste fino no
texto.

## Fechar

Termine dizendo onde estão os três arquivos, com caminho completo:

- `work/<video>/transcript.md` — abra no seu editor de texto para o ajuste
  fino manual (apagar trecho, ou trocar `⟦ ⟧` por `⟨ ⟩` para forçar manter
  algo já marcado)
- `work/<video>/anotacoes.json` — o que o passe de julgamento decidiu
- o `.m4a` de áudio gerado no passo 4 — para ouvir enquanto edita

E que os próximos passos, se/quando quiser, são o `editor` no navegador
(ajuste fino visual, palavra por palavra) e o `render` (export final) —
nenhum dos dois faz parte deste comando.
