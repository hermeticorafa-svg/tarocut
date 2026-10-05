---
description: Absorve as anotacoes que voce fez ouvindo o .m4a - corrige este audio e pergunta o que o programa podia ter pego sozinho
argument-hint: <nome do .m4a em C:\Users\marip\Videos\export\prévias, ex. "7 de espadas_previa.m4a" - ou o nome curto do video>
allowed-tools: Bash(venv/Scripts/python.exe:*), Bash(grep:*), Bash(ls:*), Read, Write, Edit
---

Absorva as anotações que o Rafael fez **ouvindo** o áudio: **$ARGUMENTS**

Este é o passo depois do `/preparar`. O `/revisar` julgou lendo; aqui entra
a única coisa que nenhuma leitura pega — o que se ouve. O produto é duplo,
e os dois lados importam igual:

1. **este áudio** fica pronto para a fase de vídeo (`cartas`, `render`);
2. **o programa** aprende, quando dá para aprender. Cada coisa que o ouvido
   pegou e o programa não é candidata a melhoria do `/revisar` ou do
   `conferir` — mas só vira mudança com evidência e com a aprovação dele
   (fase 5).

## A regra que manda em todas as outras

**Os tempos que ele escreveu são da PRÉVIA, não da fonte, e são um dedo
apontando, não uma borda.** Ele ouviu, achou ruim, e digitou o relógio do
player — depois de já ter ouvido a coisa. `[00:15]` quer dizer "por aqui",
com folga para os dois lados. Quem trata esse número como início de corte
corta no lugar errado com precisão de milissegundo, que é o pior jeito de
errar.

E **todo tempo se traduz ANTES de qualquer arquivo mudar** (fase 2). Assim
que a primeira correção entra, o corte muda, e o `[01:45]` da linha
seguinte passa a apontar para outro lugar. Traduzir por último é a versão
silenciosa do mesmo defeito.

---

## Fase 0 — achar o vídeo e travar o mapa de tempo (antes de pedir nada)

`$ARGUMENTS` costuma ser o nome do `.m4a` que o `audio` gerou. O `audio`
grava em `<pasta do vídeo>\export\prévias\<nome>_previa.m4a` (antes de
25/09/2026 gravava solto na pasta do vídeo, e os antigos foram movidos para
lá), então o caminho de volta é mecânico:

- tire a extensão e o sufixo `_previa` → esse é o **nome curto** (VIDEO).
  `7 de espadas_previa.m4a` → `7 de espadas`.
- se veio um caminho completo (tem `\`, `/` ou `C:`), use o nome do arquivo
  dele e faça a mesma conta.
- confirme que `work/<VIDEO>/` existe. Se não existir, liste `work/` com
  `ls work/` e **pergunte** — não adivinhe qual vídeo é. Áudio que não veio
  do `/preparar` não tem o que corrigir aqui.

Agora **trave o mapa**: o `.m4a` que ele ouviu foi gerado a partir de um
corte, e é contra esse corte que os tempos dele valem. Se o corte mudou
desde então (alguém mexeu no `transcript.md`, no editor, no
`anotacoes.json`), a tradução prévia→fonte sai errada em tudo que vem
depois do ponto em que mudou — e sai errada **em silêncio**, que é o
defeito que este programa mais teve.

```
venv\Scripts\python.exe tarocut.py conferir VIDEO
```

A primeira linha do resultado diz `# VIDEO | N emendas | HH:MM:SS de
HH:MM:SS`. O primeiro tempo é quanto o corte de **agora** deixa. Compare
com a duração real do `.m4a`:

```
venv\Scripts\python.exe -c "import tarocut,subprocess,json; p=r'C:\Users\marip\Videos\export\prévias\<arquivo>.m4a'; r=subprocess.run([tarocut.FFPROBE,'-v','error','-show_entries','format=duration','-of','json',p],capture_output=True,text=True); print(round(float(json.loads(r.stdout)['format']['duration']),2))"
```

- **diferença até 0,15 s** — é o encoder. MEDIDO no IMG_0246: o `.m4a` deu
  127,30 s contra 127,23 s calculados, 0,07 s de sobra que o AAC põe na
  ponta. 0,15 deixa o dobro dessa folga e ainda fica abaixo de 0,28 s, o
  menor corte que o programa aceita (`[corte].minimo`) — ou seja, pega
  qualquer anotação que tenha entrado no meio. Pode seguir.
- **diferença maior** — pare. O `.m4a` que ele ouviu não é este corte.
  Diga isso com todas as letras, e ofereça as duas saídas: rodar
  `tarocut.py audio VIDEO` e ouvir de novo (certo, e custa ~50 s), ou
  mandar as anotações assim mesmo sabendo que os tempos vão cair em outro
  lugar (não recomendado). **Não decida sozinho.**

Esse teste não é perfeito e é bom saber onde ele é cego: um `cortes` do
`montagem.json` não tem mínimo nenhum (um arrasto de 1 ms na onda do editor
tira 1 ms), e uma mudança dessas passa por baixo da tolerância. Ele
pega o que muda o corte de verdade, não toda mexida concebível.

### Então pare e peça as anotações

Escreva uma mensagem curta confirmando o que travou — vídeo, duração da
prévia, quantas emendas — e peça para ele colar. **Termine o turno aqui.**
Não chame mais nenhuma ferramenta, não leia mais nada, não adiante fase
nenhuma e não invente anotação de exemplo. A última coisa que você escreve
é o pedido.

O formato que ele usa:

```
[00:15] aqui há uma gaguejada "mas mas mas..."
[01:29] essa frase é uma tentativa malsucedida de formar o raciocínio, a frase correta aparece em [01:45]
```

Aceite qualquer variação disso — `[0:15]`, `[1:02:33]`, `00:15 -`, linha
sem colchete. O que você precisa de cada linha é: **um tempo** (às vezes
dois) e **uma frase em português** dizendo o que está errado.

---

## Fase 1 — receber sem interpretar

Antes de decidir qualquer coisa, guarde o que ele escreveu, **literal**, em
`work/<VIDEO>/ouvido.md`. Se o arquivo já existe (ele ouviu de novo depois
de uma correção), **acrescente uma rodada nova ao fim**, nunca por cima:

```markdown
## rodada 2 — 2026-09-04 (prévia de 00:02:07, 22 emendas)

### colado

[00:15] aqui há uma gaguejada "mas mas mas..."
[01:29] essa frase é uma tentativa malsucedida ...

### o que virou cada uma

| n | prévia | fonte | o que ele ouviu | o que eu fiz |
|---|---|---|---|---|
| O1 | 00:15 | 56,46s | gaguejada "mas mas mas" | anotação 55,90-56,80 |
```

Numere as linhas dele `O1`, `O2`, … na ordem em que vieram, e **use esses
números o tempo todo** — no relatório, na conversa, no `motivo` quando
ajudar.

**Toda linha colada tem que ter um destino, e a conta tem que fechar.** É a
mesma regra da `PENDENCIA SEM RESPOSTA` do `revisao.txt`, pelo mesmo
motivo: uma observação dele que se perde no meio do caminho é
indistinguível de ninguém ter olhado. Se você recebeu 7 linhas, o relatório
tem 7 linhas — inclusive as que viraram "não achei" ou "isso não é corte".

---

## Fase 2 — traduzir TODOS os tempos, e só então olhar o texto

### 2a. Prévia → fonte, tudo de uma vez

Uma chamada por tempo (inclusive os tempos citados no meio de uma frase,
como o `[01:45]` do exemplo). **Todas antes de mexer em qualquer arquivo:**

```
venv\Scripts\python.exe tarocut.py conferir VIDEO --em 1:29
```

Sai `[conferir] previa 00:01:29 = fonte 155.34s (00:02:35)`.

### 2b. Ler o que tem lá, em `words.json`

O tempo traduzido é o **dedo apontando**. Abra uma janela em volta dele —
uns 6 s para trás e 8 s para frente — e leia as palavras de verdade, com os
tempos de verdade:

```
PYTHONIOENCODING=utf-8 venv\Scripts\python.exe -c "import json; ws=json.load(open('work/VIDEO/words.json',encoding='utf-8'))['words']; [print(f\"{w['s']:7.2f} {w['e']:7.2f} {'~' if w.get('align_rejeitado') else ' '} {w['w']}\") for w in ws if 149<w['s']<164]"
```

O `~` é palavra cujo tempo o alinhador **recusou**: ela pode estar segundos
longe de onde aparece. Nunca ancore borda numa delas.

Janela maior para trás do que para frente é de propósito: ele digita depois
de ouvir.

### 2c. Bater o que ele descreveu contra o que está escrito

Ele quase sempre cita a palavra ("mas mas mas", "despeçoso"). **Ache essa
palavra na janela.** Isso é 2a+2b, duas chamadas. Quatro desfechos, e
nenhum deles é chutar:

- **achou, limpa** — ótimo, você sabe exatamente do que ele fala. Siga.
- **achou, mas a palavra (ou a vizinha que fecharia a fronteira) está
  marcada `align_rejeitado`/"duração impossível"** — **pare nessa mesma
  chamada, não investigue mais.** Essa marca já diz que o próprio programa
  achou aquele trecho estranho e tentou resolver sozinho, com os
  parâmetros calibrados do `config.toml`, dentro do `analyze`
  (`resolve_suspeitos`, [tarocut.py:460](tarocut.py:460)) — **antes** de
  este comando sequer começar a rodar. O que sobrou em `words.json` é o
  resultado final dessa tentativa; refazer isso na mão não acha nada que
  já não tenha sido tentado, com números piores. Vira pergunta no
  relatório, igual ao próximo item.
- **não achou na janela** — alargue uma vez (uns 20 s para trás). Ele pode
  ter pausado o player tarde, ou anotado no papel e digitado depois.
- **não achou nem assim** — **não invente um corte**. Vira uma pergunta no
  relatório: "O3, 04:12 — traduzi para 261,4s e o que está lá é `<cite o
  texto>`; não é o que você descreveu. É esse trecho?". Chutar aqui é a
  única maneira de este comando cortar coisa boa.

**Teto de 3 chamadas de ferramenta por `On`** para localizar e decidir onde
cortar (2a, 2b, no máximo mais uma — o alargamento da janela, por exemplo).
Passou disso sem fechar, **pare e vira pergunta** — não é "mais um passo
pra confirmar", é sinal de que a anotação caiu num caso que este comando
não resolve sozinho. Isso já aconteceu uma vez (7 de espadas, 03/09/2026):
três anotações caíram em palavras marcadas como suspeitas, e em vez de
parar na segunda chamada (onde a marca já estava visível) o processo virou
~30 chamadas cavando `stream.json` e rodando detecção de silêncio em
`audio.wav`, tentando redescobrir manualmente o que o `analyze` já tinha
resolvido. 30 minutos gastos, e o resultado final em 2 dos 3 casos foi a
mesma pergunta que teria saído na segunda chamada.

Leia também `work/<VIDEO>/entendimento.md` (o que o `/revisar` entendeu da
aula, 15-30 linhas) antes de julgar conteúdo. Se ele não existir, leia o
cabeçalho do `revisao.txt` e o verbete da carta no guia, como na fase 1 do
`/revisar`.

**Nunca abra `stream.json`, `transcript.md` nem `tarocut.py`, e nunca rode
análise acústica (detecção de silêncio ou parecida) em cima de
`audio.wav`.** Não é preguiça, é redundância: `resolve_suspeitos` já fez
exatamente esse trabalho, com os números calibrados, antes deste comando
existir. Se a marca de suspeita ainda está lá, é porque o programa já
tentou e não resolveu — a resposta certa é perguntar pro Rafael, que ouviu
o áudio de verdade, não repetir o trabalho pior e sem calibração.

---

## Fase 3 — cada correção tem UM lugar certo

O erro caro aqui é pôr a correção na camada errada: ela ou não faz nada, ou
faz e some. Decida pelo **tamanho** e pelo **tipo** do que ele ouviu:

| o que ele ouviu | onde mora |
|---|---|
| sobra uma ou mais palavras inteiras (gaguejada, tentativa abandonada, interrupção, bocejo lido como palavra) | `anotacoes.json` |
| o que sobra cabe **dentro de uma palavra só** (rabo arrastado, chiado no fim de um "naaaão") | `montagem.json` → `cortes` |
| **faltou** alguma coisa: cortou demais, sumiu uma palavra que devia ficar | `montagem.json` → `restauros` |
| a palavra está certa no áudio e **errada no texto** | `correcoes.json` (nunca corte) |
| volume, ruído de fundo, imagem, "ficou seco demais" | **não é corte**: registre e leve ao relatório |
| ele respondeu uma pendência do `revisao.txt` sem saber ("aquele `[??]` de 02:10 é ela respirando") | `respostas.json`, **além** do que for cortar |

Dois avisos que já custaram rodada:

- **anotação nunca corta pedaço de palavra.** Se `ini` e `fim` caem dentro
  da mesma palavra, as duas bordas encolhem para o mesmo ponto, a anotação
  vira largura zero e **desaparece do arquivo lido, sem erro nenhum** —
  aconteceu duas vezes no 7 de espadas. Corte menor que uma palavra vai
  para `montagem.json`/`cortes`, que entra depois de tudo, sem encaixe em
  fronteira de palavra e sem mínimo.
- **anotação encostada não tira a palavra.** Para a palavra sair, a
  anotação tem que **contê-la inteira**.

### Onde pôr a borda

Igual à fase 3 do `/revisar`, e pelo mesmo motivo (uma anotação com borda
estimada comeu "figuras historicas," sem que nada acusasse):

- ache em `words.json` as palavras **limpas** (sem `~`) mais próximas dos
  dois lados do corte e ancore no **centro** do intervalo entre elas;
- se a vizinha tem `~`, pule para a limpa seguinte, mesmo que isso mude o
  que sai — desde que a frase que sobra feche sozinha;
- **nunca** calcule tempo de palavra por palavras/segundo, nem "só para
  estimar";
- no caso "a tentativa ruim está em X e a boa em Y": o corte vai do começo
  da tentativa ruim até **imediatamente antes da primeira palavra da boa**,
  e leva a bagunça do meio junto. Meia tentativa é pior que nenhuma.

### Escrever

`anotacoes.json` **é sagrado**, e `respostas.json` também: copie uma vez
nesta sessão para `anotacoes.json.bak-<AAAAMMDD-HHMM>`, **nunca apague nem
reescreva anotação existente**, acrescente as suas ao fim. O `motivo` diz
o que sai e por quê, em português sem acento, e leva o número da linha
dele:

```json
{"ini": 155.9, "fim": 156.8, "motivo": "O1 - gaguejada 'mas mas mas'"}
```

Defeito de programa **nunca** vai no `motivo` — vai para o relatório. Já
houve `motivo` virando diagnóstico de código, e o arquivo passou a mentir
sobre a razão do corte.

### O gate: nada segue sem provar, arquivo por arquivo

`anotacoes.json` tem rede de segurança — o `conferir` grita `ANOTACAO NAO
OBEDECIDA` se o corte não pegou. `montagem.json`, `correcoes.json` e
`respostas.json` **não têm nenhuma**: se a escrita não acontecer, nada
denuncia. Foi exatamente esse buraco que fez uma sessão inteira (as de
espadas, 04/09/2026) processar 8 apontamentos, escrever a lição de cada um
em `licoes-do-ouvido.md` — e nunca escrever o corte real de nenhum: zero
`montagem.json`, zero `correcoes.json`, `.m4a` idêntico ao de antes. O
relatório saiu como se tivesse funcionado.

Por isso, **antes de seguir para a fase 4**, para CADA `On`: abra (Read, não
confie em lembrar da chamada de `Write` anterior) o arquivo pra onde ele
devia ter ido, e confirme que a entrada está lá, citando o número do `On`.
Se a resposta foi "não é corte" ou "não achei", isso também é destino
válido — mas tem que estar escrito em algum lugar que você vai reler na
fase 6, nunca só na sua memória da conversa.

---

## Fase 4 — provar que valeu, e gerar o áudio novo

Escrever não é ter valido. Se alguma anotação cobre **fala transcrita de
verdade**, o `analyze` precisa rodar de novo — `aplica_anotacoes` só roda
dentro dele, e sem isso a rede de segurança `_respeita_palavras` devolve a
palavra para a tela:

```
venv\Scripts\python.exe tarocut.py analyze VIDEO --force
venv\Scripts\python.exe tarocut.py draft VIDEO --force
venv\Scripts\python.exe tarocut.py conferir VIDEO
```

**Critério binário: zero `ANOTACAO NAO OBEDECIDA`.** Enquanto houver uma,
não terminou. A linha diz `sobra Xs de Ys`; o diagnóstico está na tabela da
fase 4 do `/revisar`. **No máximo 3 rodadas** — depois disso, pare e
reporte o que sobrou.

E **confira o que ele pediu, uma por uma**: para cada `On`, verifique que o
que ele reclamou não está mais no corte. Zero `ANOTACAO NAO OBEDECIDA`
prova que não sobrou fala demais; **não** prova que saiu a palavra certa,
nem que não saiu palavra a mais (isso não sobra em lugar nenhum, então
nenhum alarme dispara). Releia a região das duas bordas de cada anotação
nova.

Trie também as outras linhas do `conferir`, principalmente
`REPETICAO AUDIVEL` — metade desses casos **o corte cria**, encostando uma
palavra na igual do outro lado da emenda, e uma correção que acabou de
entrar é justamente o tipo de coisa que cria uma.

Então gere o áudio novo:

```
venv\Scripts\python.exe tarocut.py audio VIDEO
```

**Diga no relatório, em negrito, que os tempos mudaram.** O `.m4a` novo é
mais curto; `[01:29]` no antigo não é `[01:29]` no novo. Se ele mandar
anotações da rodada seguinte usando o relógio antigo, tudo cai fora do
lugar — a fase 0 pega isso, mas é melhor ele já saber.

**Confira o `.m4a` na pasta do vídeo (não em `work/`), não só que o comando
rodou sem erro.** O timestamp dele tem que ficar mais novo que o último
arquivo de decisão que você mexeu (`montagem.json`, `correcoes.json`,
`anotacoes.json`). Se não ficou, a fase 4 não terminou, mesmo que os
arquivos de decisão estejam certos — é o mesmo defeito do gate da fase 3,
um passo depois.

---

## Fase 5 — a segunda pergunta, só depois que a primeira fechou

**Nunca comece por aqui, e nunca chegue aqui sem o gate da fase 3 e a
conferência de `.m4a` da fase 4 terem passado.** Esta fase é sobre vídeos
futuros; a 3 e a 4 são sobre este áudio agora, e são o motivo do comando
existir. Escrever uma linha em `licoes-do-ouvido.md` não é ter corrigido
nada — já aconteceu de uma sessão tratar a lição como se fosse o produto
final e pular o corte de verdade (ver o gate acima).

Aqui o assunto deixa de ser este vídeo. Para cada `On`, pergunte: **o
programa tinha como pegar isso sozinho?** Quatro respostas, e a maioria é
a terceira:

- **(A) o programa marcou e o `/revisar` não olhou** — o sinal existia
  (`[rec:]`, `[??]`, `retomada?`, uma linha do `conferir`) e passou. Isso é
  falha de **instrução**, e o conserto é barato: uma frase em
  `.claude/commands/revisar.md`.
- **(B) o programa tinha o dado e não perguntou** — a informação estava no
  `stream`/`words.json` e ninguém a transformou em pendência ou em linha do
  `conferir`. Conserto: código ou `config.toml`. **Caro, e precisa de
  medição** (abaixo).
- **(C) o programa não tinha como saber** — depende de entender o sentido
  da aula, ou de ouvir. É a maioria, e não é fracasso de ninguém: é
  exatamente para isso que este comando existe. Só registra.
- **(D) já foi testado e recusado** — antes de propor qualquer coisa,
  confira a lista no `CLAUDE.md`. Já morreram, com medição: separar fala de
  bocejo pela acústica; avisar frase pendurada na emenda; avisar emenda em
  ponto alto do envelope; e as três categorias de pendência aposentadas
  (`fala nao transcrita`, `cauda arrastada`, `disputa`). Se a ideia é uma
  dessas, diga isso e mostre por que morreu — reabrir exige derrubar a
  medição que a matou, não repetir a intuição.

### A régua para virar proposta

**Um caso não é medição.** A regra deste projeto é medir antes de mexer, e
ela vale aqui:

- **(A)** vira proposta na hora — mudar uma frase de prompt é barato e
  reversível.
- **(B)** vira proposta quando **acontece duas vezes** (no mesmo vídeo ou
  em vídeos diferentes, contando o que já está em
  `material/licoes-do-ouvido.md`), **ou** quando o caso único é julgamento
  humano sumindo em silêncio — esse é sempre bug, e sempre vale.
- antes de propor um alarme novo, **conte quantas vezes ele dispararia**
  nos fluxos que existem em `work/`. Alarme que dispara em 20% das emendas
  é papel de parede, e papel de parede gasta a atenção de quem responderia
  os alarmes bons. Se dispara demais, a proposta é outra, ou nenhuma.
- número novo **mora no `config.toml`**, nunca como valor padrão de
  parâmetro, e **com o comentário dizendo contra o quê foi medido**.

### Registrar sempre, mudar quase nunca

Independente de virar proposta, escreva uma linha em
`material/licoes-do-ouvido.md` para todo `On` de tipo (A) ou (B) — é esse
arquivo que faz a **segunda** ocorrência ser reconhecida como segunda. Crie
o arquivo se não existir, com este cabeçalho:

```markdown
# O que o ouvido pegou e o programa não

Uma linha por achado do `/anotar`. Existe porque um caso não é medição: é
aqui que a segunda vez do mesmo defeito vira evidência de que o programa
podia perguntar sozinho.

| data | vídeo | o que passou | tipo | o programa tinha o quê | virou mudança? |
|---|---|---|---|---|---|
```

### Apresentar e esperar

Se houver proposta, apresente **antes de mexer em qualquer coisa**, em
lista numerada, cada uma em quatro linhas curtas e sem jargão:

1. **O que acontece hoje** — em português de gente.
2. **O que você ouviu** — o `On` dele, citado.
3. **O que eu mudaria** — qual arquivo, e o que passa a acontecer.
4. **O que pode dar errado** — sempre tem alguma coisa. Se a mudança faz o
   programa cortar mais, diga que ela pode comer fala boa; se faz avisar
   mais, diga quantos avisos a mais por vídeo, medidos.

Depois **pare e espere a resposta dele**. Ele pode aprovar em parte ("faz a
1 e a 3"). Nada muda antes disso — nem "só o comentário".

### Depois de aprovado

Aplique, e **prove que não quebrou**:

```
venv\Scripts\python.exe teste_editor.py
venv\Scripts\python.exe teste_revisao.py
venv\Scripts\python.exe tarocut.py conferir IMG_0246
venv\Scripts\python.exe tarocut.py conferir IMG_0247
venv\Scripts\python.exe tarocut.py conferir IMG_0248
```

IMG_0246, IMG_0247 e IMG_0248 são os vídeos de calibração: é contra eles
que os números do `config.toml` foram medidos, e uma mudança de
comportamento só está verificada depois de passar nos três. Se a mudança
mexe na **marcação**, o corte de vídeos já julgados muda — o
`anotacoes.json` deles sobrevive, o resto não. Diga isso.

Mudança de prompt costuma morar em `revisar.md` (o passe de julgamento) e,
quando muda o que o `/preparar` cobra no fim, também em `preparar.md`. No
`CLAUDE.md` entra só o que virar regra nova de "o que não quebrar".

---

## Fase 6 — o relatório

Português de gente, frases curtas, número quando for número. A explicação
técnica vai **no fim**, num bloco separado — não misturada no meio.

**1. O que mudou neste áudio**

Uma linha por `On`, na ordem em que ele escreveu:

```
O1  00:15  "mas mas mas"        -> cortei 0,9s (55,90-56,80). Sumiu.
O2  01:29  tentativa abandonada -> cortei 16,2s (155,3-171,5), da tentativa
                                   ate o comeco da versao boa. Sumiu.
O5  04:12  nao achei            -> em 261,4s o que esta e "<texto>". E esse
                                   trecho? (nada mudou)
```

Toda linha colada aparece aqui. Se você recebeu 7, tem 7 linhas.

**2. O áudio novo**

Onde está, quanto tempo tem, quanto encurtou — e, em negrito, que **os
tempos mudaram: não use o relógio antigo na próxima rodada.**

**3. O programa aprendeu alguma coisa?**

- se não: uma frase dizendo que tudo era julgamento de conteúdo, que é o
  esperado, e que ficou registrado em `material/licoes-do-ouvido.md`;
- se sim: as propostas numeradas da fase 5 (se ainda não aprovadas), ou o
  que foi aplicado e o que os testes disseram.

**4. Para o programador**

Se alguma coisa cheirou a defeito de código, aqui — nunca dentro do
`motivo` de uma anotação.

**5. O que fazer agora**

Uma linha: ouvir de novo (e mandar `/anotar` outra vez), ou seguir para as
cartas / o render.
