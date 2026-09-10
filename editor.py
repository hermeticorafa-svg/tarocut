#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""editor.py - edita o corte pela transcricao e pela onda, no navegador.

Nao e um editor de video. Sao duas vistas da MESMA coisa: a transcricao
ipsis litteris em cima, com o que sai do video riscado por cima dela, e a
faixa de audio embaixo, onde o mesmo trecho aparece como tempo. Selecionar
palavra acende o tempo; arrastar no tempo acende a palavra. Quem usa isto
nao e editor de video e nao quer aprender a ser.

Por que navegador e nao Tk: o que faz este editor valer a pena e ouvir o
corte enquanto se corta, sem renderizar nada. Isso pede um motor de audio que
toque uma lista de trechos com emenda precisa e sem lacuna - a Web Audio API
faz isso de graca, agendando cada trecho num relogio de amostra. Em Tk seria
preciso escrever esse motor do zero. E o mesmo PCM que ela decodifica para
tocar e o que a tela usa para desenhar a onda: nenhum endpoint novo.

O PONTO DELICADO deste arquivo e um so, e esta escrito no CLAUDE.md: **o
navegador nao calcula corte**. Todo gesto vira um POST e volta com os trechos
ja calculados por `tarocut.intervalos()` - a mesma funcao do `render`, com o
mesmo snap, a mesma fusao de ilhas, o mesmo minimo. A tela converte pixel em
tempo dentro da janela que ela mesma desenhou (isso e desenho), e nada mais:
uma reimplementacao da geometria em JS seria mais rapida e mentiria em algum
canto, mais cedo ou mais tarde.

E por isso o riscado nao vem do `cut` do token, e sim dos trechos mantidos:
o que aparece riscado e o que de fato nao vai sair no .mov, depois de toda a
geometria. Foi essa a exigencia - ver na transcricao tudo o que foi ocultado,
inclusive o que uma anotacao de julgamento tirou.

A GARANTIA deste arquivo, e a razao de ele ter sido reescrito em 08/09/2026:
**ocultei a palavra e ela sumiu, sem asterisco.** Ate aqui o programa AVISAVA
que nao tinha obedecido - `folga_palavra` (0,06 s de cada lado) e `minimo`
(0,28 s) anulavam o corte de qualquer palavra curta, e o aviso corretissimo
morava num painel que ninguem lia. Hoje a decisao de palavra desce tambem
para a camada de tempo (ver `_palavras`), que entra por ultimo e nao passa
por geometria nenhuma. Quando as duas divergem, quem se dobra e a geometria,
e em voz alta: `completados` diz quanto ela teve de ceder e onde.

O que sai daqui e `work/<video>/montagem.json`, lido pelo `_base` do tarocut.
Depois de salvar, `audio` e `render` produzem exatamente o corte ouvido.
"""

import bisect
import json
import subprocess
import sys
import threading
import time
import uuid
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlparse
from urllib.request import Request, urlopen

import tarocut as tc

ROOT = Path(__file__).resolve().parent
PAGINA = ROOT / "editor.html"

# Menor gesto que o programa aceita, em segundos. E o mesmo 1 ms do
# `tc._spans`: a 48 kHz sao 48 amostras, e ninguem clica isso.
MINIMO = 0.001


def _duracao(p):
    """Duracao de um arquivo de audio, ou None se o ffprobe nao souber
    dizer. Subprocesso: quem chama guarda o resultado (ver
    `Sessao.previa_no_disco`)."""
    try:
        r = subprocess.run(
            [tc.FFPROBE, "-v", "error", "-show_entries", "format=duration",
             "-of", "json", str(p)], capture_output=True, text=True)
        return float(json.loads(r.stdout)["format"]["duration"])
    except Exception:
        return None


# ------------------------------------------------------------------ sessao

class Sessao:
    """Um video carregado. Vive enquanto o servidor vive.

    O fluxo analisado e o envelope do wav ficam aqui, em memoria, e sao
    reaproveitados a cada recalculo: reler stream.json e refazer o envelope
    a cada clique poria meio segundo entre o clique e o som."""

    def __init__(self, video):
        # Quem e esta sessao, e de que aba ela e. Ver `posse`: e o unico
        # jeito de uma aba de ONTEM nao gravar por cima do video de HOJE.
        self.sid = uuid.uuid4().hex[:8]
        self.dono = ""
        d = tc.WORK / Path(video).stem
        if not (d / "stream.json").exists():
            sys.exit(f"[editor] {Path(video).stem} ainda nao foi analisado. "
                     f"Rode 'tarocut.py run' antes.")
        (self.C, self.d, self.stream, self.dur, self.db, self.hop,
         self.anot, mont, corr) = tc._base(str(video), "editor",
                                           aplicar_mont=False,
                                           aplicar_corr=False)
        self.meta = tc.jload(self.d / "meta.json")
        self.src = Path(self.meta["src"])
        self.video = self.src if self.src.exists() else Path(video)
        self.nome = Path(video).stem
        self.wav = self.d / "audio.wav"
        if not self.wav.exists():
            sys.exit(f"[editor] falta {self.wav}. Rode 'tarocut.py run' antes.")
        with __import__("wave").open(str(self.wav), "rb") as w:
            self.sr = w.getframerate()
        # O .m4a que o `audio` grava ao lado do video - o arquivo que ele
        # OUVE, e cujo relogio ele usa para anotar. Saber se esse arquivo
        # ainda e este corte faz parte de mostrar o corte (`previa_no_disco`).
        self._m4a = self.src.parent / f"{self.nome}_previa.m4a"
        self._m4a_chave, self._m4a_dur = None, None
        # o selo do stream.json COMO ESTA NO DISCO. O `_base` ja aplicou o
        # transcript.md por cima do fluxo, entao calcular o selo daqui
        # daria outro numero - e o selo so serve para dizer de que analise
        # veio este montagem.json.
        doc = tc.jload(self.d / "stream.json")
        self.selo = tc.selo_stream(doc["stream"])
        # As lacunas MEDIDAS pelo analyze, que o `_base` nao devolve porque
        # nenhuma etapa de corte precisa delas. A tela precisa: e nelas que
        # esta o que o Whisper OUVIU e o programa recusou (`bruto`,
        # `rejeitado`) e a fracao de voz de cada vao. Sem isso, um
        # 'Ah meu amor...' recusado por logprob e uma pausa de 6 s viram o
        # mesmo tracinho - som que existe e nao deixa marca, o defeito de
        # sempre deste programa, so que do lado da entrada.
        self.lacunas = sorted(doc.get("lacunas", []), key=lambda l: l["s"])
        self._lac_ini = [l["s"] for l in self.lacunas]

        # Estado depois do transcript.md e das anotacoes, e ANTES da
        # montagem: e contra esta linha de base que o editor mede o que a
        # pessoa mudou. Sem ela, salvar gravaria como "decisao sua" tudo o
        # que a analise ja tinha proposto sozinha, e reanalisar o video
        # deixaria de valer para alguma coisa.
        self.base = [(bool(t["cut"]), bool(t.get("prot")))
                     for t in self.stream]
        # o `why` da PROPOSTA, guardado pela mesma razao que o `_t_orig`
        # guarda o texto original: desclicar tem de devolver o motivo que
        # estava ali antes do clique, e depois que `_aplica` escreve por
        # cima nao ha em lugar nenhum quem lembre qual era.
        self._why_base = [t.get("why", "") for t in self.stream]

        self._mede_voz()
        self._mede_anotacoes()
        # de disco, e nao muda enquanto a sessao vive (43 ms por leitura).
        self._vocab = tc.carrega_vocabulario(self.d)
        self._pend, self._pend_chave = [], None
        # o corte que a GEOMETRIA sozinha faria com as decisoes de palavra
        # de agora, sem a camada de tempo. Nao e o corte que sai: e a
        # testemunha de quanto a geometria teve de ceder (ver
        # `completados`). Cacheado pela mesma chave que o `_pend`, e pela
        # mesma razao - so muda quando a camada de palavra muda.
        self._geo, self._geo_chave = None, object()
        # o que so depende do corte (pecas, avisos, achados) e a ultima
        # versao das pecas que ja saiu daqui. Ver `_chave_corte`.
        self._corte, self._corte_chave = {}, object()
        self._pecas_chave = object()
        self._iv, self._iv_chave = [], object()

        self.tokens, self.cortes, self.restauros = {}, [], []
        self.aviso = ""
        if mont:
            self._carrega(mont)

        # o texto ANTES de qualquer correcao. Nada mais neste programa
        # escreve em t["t"], entao capturar aqui - logo depois do _base,
        # antes de aplicar correcoes.json - e o unico lugar onde isso e
        # garantidamente o texto original.
        self._t_orig = [t["t"] for t in self.stream]
        self.correcoes = {}
        if corr:
            self._carrega_correcoes(corr)
        self._aplica_correcoes()

        # ---- modo cartas ------------------------------------------------
        # As 78 cartas saem do disco UMA vez: e a mesma lista que alimenta
        # o autocomplete da tela, o parser do roteiro e a imagem do
        # overlay. Reler a pasta a cada tecla digitada seria absurdo, e ter
        # uma segunda lista em JS seria pior - o dia em que uma carta
        # entrasse na pasta, a tela ofereceria carta que o export recusa.
        self.cartas_nomes = tc.catalogo_cartas()
        # midia avulsa (foto, grafico - nao carta do baralho): MESMA
        # garantia, mesma razao - ver tc.catalogo_midias().
        self.midias_nomes = tc.catalogo_midias()
        rot = tc.carrega_roteiro(self.d) or {}
        self.principal = rot.get("principal") or ""
        self.roteiro = rot.get("roteiro") or ""
        self.sem_legenda = bool(rot.get("sem_legenda"))
        # marcado a mao no modo cartas; a gui.py le o mesmo arquivo e pinta
        # a linha do video de verde claro. Nao decide nada aqui dentro -
        # e so organizacao de quem tem 34 videos para editar.
        self.pronto = bool(rot.get("pronto"))
        # o corte contra o qual o roteiro foi escrito da ultima vez. Ver
        # `tc.roteiro_caducou`: o relogio do roteiro e o da SAIDA, e um
        # corte novo escorrega todos os tempos de uma vez.
        self.roteiro_mantido = rot.get("mantido")
        self.legendas = {}
        for it in (tc.carrega_legendas(self.d) or {}).get("itens", []):
            self.legendas[(round(it["s"], 3), round(it["e"], 3))] = dict(it)

        self.pilha = []
        # RLock, e nao Lock: `acao` segura a tranca e chama `estado`,
        # que chama `intervalos`. E ela e necessaria porque o
        # `intervalos` ESCREVE no `self.stream` (o `_aplica` empilha a
        # camada de palavra nele) - com o servidor atendendo em
        # threads, um /doc no meio de um /acao leria o fluxo com o
        # estado do outro pela metade.
        self.lock = threading.RLock()
        # por ultimo: precisa da tranca e do fluxo inteiro montado.
        self._cumpre_carga()

    def _carrega(self, mont):
        """Montagem do disco. Decisao de palavra que caducou nao entra.

        Os tempos sao absolutos e sobrevivem a uma reanalise; os indices de
        token nao. Descartar os indices em silencio seria o quinto caminho
        para julgamento humano sumir neste programa, entao aqui isso vira
        aviso na tela E copia do arquivo antes de a primeira gravacao passar
        por cima. Aconteceu de verdade com o editor antigo: duas decisoes
        evaporaram no primeiro ajuste depois de reanalisar."""
        self.cortes = mont["cortes"]
        self.restauros = mont["restauros"]
        ruins = tc.confere_montagem(self.stream, mont)
        if not ruins:
            for dec in mont["tokens"]:
                self.tokens[dec["i"]] = dec["estado"]
            return
        copia = self.d / "montagem-anterior.json"
        copia.write_bytes((self.d / "montagem.json").read_bytes())
        self.aviso = (f"o montagem.json era de outra analise: os trechos de "
                      f"tempo foram mantidos (tempo nao caduca), "
                      f"{len(mont['tokens'])} decisoes de palavra foram "
                      f"descartadas. Copia em {copia.name}")
        print(f"[editor] {self.aviso}")
        for r in ruins[:6]:
            print(f"    {r}")

    def _cumpre_carga(self):
        """A garantia vale tambem para o que ja estava decidido no disco.

        Um `montagem.json` escrito pelo editor de botoes tem decisao de
        palavra e NAO tem o trecho de tempo que hoje a acompanha (ver
        `_palavras`). Abrir esse arquivo e continuar mostrando a palavra
        oculta que continua tocando seria manter a promessa quebrada por
        onde ela veio.

        Nao e silencioso, e nao pode ser: mudar o corte de um video ja
        julgado sem dizer e o defeito que este programa mais persegue.
        Quando muda alguma coisa, a mudanca vai para o `aviso`, que a tela
        mostra por cima de tudo. E so entra no disco no primeiro gesto -
        abrir e fechar o editor nao reescreve nada.

        MEDIDO no IMG_0247, o unico dos tres de calibracao com montagem de
        verdade: 3 decisoes de palavra, e a diferenca esta no `cartas` de
        159,57s, oculto a mao, que continuava tocando 60 ms."""
        antes = self.intervalos()
        for i, est in sorted(self.tokens.items()):
            t = self.stream[i]
            (self._corta if est == "cortar" else self._restaura)(
                [t["s"], t["e"]])
        depois = self.intervalos()
        if depois == antes:
            return
        d = abs(sum(b - a for a, b in depois) - sum(b - a for a, b in antes))
        aviso = (f"a geometria deixava {d*1000:.0f} ms de palavra decidida a "
                 f"mao do lado errado do corte; completei no tempo exato "
                 f"({len(antes)} trechos -> {len(depois)})")
        self.aviso = f"{self.aviso} {aviso}" if self.aviso else aviso
        print(f"[editor] {aviso}")

    def _carrega_correcoes(self, doc):
        """correcoes.json do disco. Mesma regra do `_carrega`, pela mesma
        razao: indice de token caduca quando a analise roda de novo, e
        descartar em silencio e o mesmo defeito de sempre - so que aqui a
        anotacao que sumiria e uma correcao de ortografia, nao um corte, o
        que a torna mais facil ainda de ninguem notar que sumiu."""
        ruins = tc.confere_correcoes(self.stream, doc)
        if not ruins:
            for item in doc["itens"]:
                self.correcoes[item["i"]] = {
                    "s": item["s"], "e": item["e"],
                    "orig": item["orig"], "novo": item["novo"]}
            return
        copia = self.d / "correcoes-anterior.json"
        copia.write_bytes((self.d / "correcoes.json").read_bytes())
        aviso = (f"o correcoes.json era de outra analise: "
                 f"{len(doc['itens'])} correcoes de texto foram "
                 f"descartadas. Copia em {copia.name}")
        self.aviso = f"{self.aviso} {aviso}" if self.aviso else aviso
        print(f"[editor] {aviso}")
        for r in ruins[:6]:
            print(f"    {r}")

    def _aplica_correcoes(self):
        """Poe o texto corrigido em `t["t"]`, ou devolve o original onde
        nao ha correcao. So mexe em texto - geometria e cut nunca passam
        por aqui."""
        for i, t in enumerate(self.stream):
            dec = self.correcoes.get(i)
            t["t"] = dec["novo"] if dec else self._t_orig[i]
            # mesma marca que o `tc.aplica_correcoes` poe, pela mesma razao:
            # `pendencias` nao pergunta sobre texto que uma pessoa escreveu.
            # Aqui ela precisa ser LIMPA quando a correcao sai (desfazer),
            # senao o token continuaria imune depois de voltar a ser leitura
            # do Whisper.
            t["mao_texto"] = bool(dec)

    # -------------------------------------------------------- o que se ouviu

    def _lacuna_do_token(self, t):
        """A lacuna medida de que este token nasceu, ou None.

        Casa por TEMPO, nunca pelo `i` gravado na lacuna: aquele indice e o
        da palavra anterior no `words.json`, e `resolve_suspeitos` insere
        tokens depois de ele ter sido calculado. Mesma razao pela qual o
        `montagem.json` guarda `s`/`e`. A tolerancia de 30 ms cobre o
        arredondamento no milissegundo que o `add` do analyze faz."""
        j = bisect.bisect_left(self._lac_ini, t["s"] - 0.03)
        while j < len(self.lacunas) and self.lacunas[j]["s"] <= t["s"] + 0.03:
            if abs(self.lacunas[j]["e"] - t["e"]) <= 0.03:
                return self.lacunas[j]
            j += 1
        return None

    @staticmethod
    def _tem_voz(t, lac):
        """Ha voz MEDIDA neste token?

        Um `hesitacao` e voz por definicao - o analyze so o cria quando o
        envelope acusou voz na lacuna. Um `silencio` tambem pode ser: quando
        a lacuna era `misto` e a re-transcricao foi recusada, o analyze
        trata a recusa como silencio e o token sai rotulado "pausa". MEDIDO:
        7, 9 e 7 tokens de silencio por video carregam um texto recusado
        dentro ('Ah meu amor...', 'O que e isso?') - sao MAIS que os
        `hesitacao` na mesma situacao (2, 5 e 4)."""
        return t["k"] == "hesitacao" or bool(
            lac and lac["tipo"] in ("fala", "misto"))

    def _mede_voz(self):
        """Mede voz e leitura recusada de cada token, UMA VEZ.

        Cabe no `__init__` porque nada disto muda enquanto a sessao vive: o
        `s`/`e` de um token nunca e reescrito aqui (o `_aplica` so mexe em
        `cut`/`prot`, a correcao so em `t["t"]`) e as lacunas vem do disco.
        Feito por token a cada `pecas`, custava 100 ms por gesto num video
        de 900 tokens, contra os 25 ms que o `pecas` inteiro custava antes -
        quatro vezes o atraso entre apertar o botao e ver o risco mudar."""
        self._voz = [False] * len(self.stream)
        self._ouviu = [("", "", "")] * len(self.stream)
        self._voz_antes = [False] * len(self.stream)
        ant = 0.0
        for i, t in enumerate(self.stream):
            lac = self._lacuna_do_token(t)
            self._voz[i] = self._tem_voz(t, lac)
            # `ouviu` do proprio token vem na frente: e o que o
            # `resolve_suspeitos` guardou, e ele nao tem lacuna nenhuma
            # para casar (nasce depois de elas serem medidas).
            if t.get("ouviu"):
                bruto, mot = t["ouviu"], t.get("recusa", "")
            elif lac and lac.get("rejeitado"):
                bruto, mot = lac.get("bruto", ""), lac["rejeitado"]
            else:
                bruto = mot = ""
            self._ouviu[i] = (self._corta_bruto(bruto), mot,
                              self._sugestao(bruto, mot))
            self._voz_antes[i] = self._voz_no_vao(ant, t["s"])
            ant = max(ant, t["e"])

    @staticmethod
    def _corta_bruto(bruto):
        """Uma recusa por `denso demais` ou `loop repetido` traz o loop
        inteiro - MEDIDO no IMG_0248: 'Então essa não é o que é que é...'
        com 800 caracteres, num title de tooltip. Depois das primeiras
        palavras ele nao diz mais nada que a propria recusa nao diga."""
        bruto = " ".join((bruto or "").split())
        return bruto if len(bruto) <= 80 else bruto[:79].rstrip() + "…"

    @staticmethod
    def _sugestao(bruto, motivo):
        """O que a caixa de edicao abre ESCRITO - e nao e o mesmo que se
        mostra no title, de proposito.

        Recusa por `logprob` e leitura plausivel de que o Whisper duvidou
        ('O que e isso?', 'Ah meu amor...', 'E ai'): quase sempre so falta
        confirmar, e vem pronta. As outras tres sao lixo por construcao -
        `frase-fantasma` e alucinacao de legenda ('Obrigado.', 'Até a
        próxima!') onde ninguem disse isso, e `denso demais`/`loop
        repetido` sao a mesma silaba cem vezes. Oferecer lixo pre-escrito
        so faria apagar antes de escrever."""
        return (Sessao._corta_bruto(bruto)
                if str(motivo).startswith("logprob") else "")

    def _corrigivel(self, i):
        """Onde se pode escrever o que se ouviu. Um lugar so decide isso:
        a tela pergunta aqui para habilitar o menu, e o `acao` pergunta
        aqui de novo antes de gravar.

        Vale para toda `palavra` e para todo token com VOZ medida - e a
        voz que diz se ha o que transcrever, nao o tipo do token. Ate
        03/09/2026 a regra era por tipo e excluia exatamente um: o
        `hesitacao` rotulado "hesitação", que e o lugar onde ha fala e nao
        ha texto. Sao 40 tokens no IMG_0247 e 39 no IMG_0248 - ver a fala,
        ouvir a fala e nao ter onde escrever era a metade pior de "o audio
        nao chegou na transcricao".

        Pausa sem voz continua de fora: la nao ha o que transcrever."""
        return self.stream[i]["k"] == "palavra" or self._voz[i]

    def _voz_no_vao(self, a, b):
        """Este vao SEM TOKEN tem voz medida dentro?

        O analyze so cria token de silencio acima de `pausa_intocavel`
        (0,75 s). Uma lacuna `misto` de 0,6 s - voz medida pelo envelope,
        texto nenhum - nao vira token, e sem token a tela nao tem onde
        pendurar marca nenhuma. MEDIDO nos tres videos de calibracao: 7,
        10 e 13 vaos assim, somando 3,3 a 6,3 s de voz que a folha mostrava
        como papel em branco."""
        if b - a < 0.02:
            return False
        j = bisect.bisect_left(self._lac_ini, a - 0.03)
        while j < len(self.lacunas) and self.lacunas[j]["s"] < b + 0.03:
            l = self.lacunas[j]
            if (l["tipo"] in ("fala", "misto")
                    and min(l["e"], b) - max(l["s"], a) > 0.05):
                return True
            j += 1
        return False

    # ---------------------------------------------------- quem mandou cortar

    def _mede_anotacoes(self):
        """Que trecho do passe de julgamento cobre cada token, UMA VEZ.

        `self.anot` foi lido do disco na abertura e nao muda enquanto a
        sessao vive - constante como o `_voz`, e medida pela mesma razao.

        A regra de cobertura e a MESMA de `tc.aplica_anotacoes`, qualquer
        sobreposicao por menor que seja, e nao uma parecida: o conjunto de
        tokens que a anotacao mandou cortar e o conjunto que a tela pinta
        como anotado tem de ser um so. Com um limiar de 0,05 s sobravam 2
        tokens no IMG_0247 e 1 no IMG_0248 cortados por julgamento humano e
        desenhados como corte mecanico.

        MEDIDO: 41 tokens no IMG_0246, 179 no IMG_0247 (19% do video) e 14
        no IMG_0248 saem porque uma pessoa mandou, e ate 04/09/2026 esse
        riscado era igualzinho ao da regra automatica. Para saber que a
        palavra saiu por julgamento era preciso clicar nela e ler o "Por
        que saiu" - uma palavra de cada vez, na camada mais cara de
        produzir que este programa tem.

        `_anot_antes` e a mesma pergunta para o VAO entre dois tokens: uma
        anotacao pode cair inteira em silencio, e ai nao ha token nenhum
        para pintar."""
        self._anot_de = [""] * len(self.stream)
        self._anot_antes = [""] * len(self.stream)
        ant = 0.0
        for i, t in enumerate(self.stream):
            for a in self.anot:
                if not (t["e"] <= a["ini"] or t["s"] >= a["fim"]):
                    self._anot_de[i] = a["motivo"]
                if min(a["fim"], t["s"]) - max(a["ini"], ant) > 0.02:
                    self._anot_antes[i] = a["motivo"]
            ant = max(ant, t["e"])

    # ---------------------------------------------------- o relogio da previa

    def previa_no_disco(self, mantido):
        """O .m4a que esta no disco ainda e ESTE corte?

        O relogio com que ele anota e o do arquivo, nao o da fonte. Assim
        que um gesto muda o corte, o .m4a de antes deixa de valer: um
        `[01:29]` escrito ouvindo aquele arquivo passa a apontar para outro
        lugar, e passa a apontar errado EM SILENCIO - a mesma familia do
        selo do transcript.md, por mais um caminho.

        Mesma pergunta e mesmo numero da fase 0 do `/anotar`: a diferenca
        de duracao, com 0,15 s de folga. MEDIDO: o AAC poe de 0,02 a 0,07 s
        na ponta ("7 de espadas" +0,02, "as de espadas" +0,05, IMG_0246
        +0,07), e 0,15 ainda fica abaixo do menor corte que o programa
        aceita (`[corte].minimo` = 0,28) - qualquer edicao de verdade cai
        fora da folga.

        Cego onde a fase 0 e cega: tirar 200 ms num lugar e devolver 200 ms
        noutro passa por igual. O ffprobe e subprocesso, entao so roda
        quando o mtime do arquivo muda - o `estado` chama isto a cada
        gesto."""
        try:
            st = self._m4a.stat()
        except OSError:
            return {"tem": False}
        chave = (st.st_mtime_ns, st.st_size)
        if chave != self._m4a_chave:
            self._m4a_chave = chave
            self._m4a_dur = _duracao(self._m4a)
        if self._m4a_dur is None:
            return {"tem": False}
        return {"tem": True, "dur": round(self._m4a_dur, 2),
                "velho": abs(self._m4a_dur - mantido) > 0.15}

    def em_previa(self, t):
        """Tempo do .m4a -> tempo da fonte, e a palavra que soa ali.

        Pela `tc._mapa_previa`, a mesma do `conferir --em` e da fase 2 do
        `/anotar`. Ate 04/09/2026 o unico jeito de achar na folha o
        `[01:29]` que ele anotou ouvindo era rodar um comando noutra janela
        e voltar com um numero de fonte - que a folha tambem nao mostrava
        em canto nenhum.

        A conta e daqui, e nao da tela, pela regra de sempre: o mapa e
        feito do corte, e corte nao se recalcula no navegador.

        A varredura e linear de proposito: o alinhador as vezes devolve
        tokens fora de ordem (ver `tc.fora_de_ordem`) e uma bisseccao
        responderia errado justo neles."""
        with self.lock:
            iv = self.intervalos()
            para_fonte, _ = tc._mapa_previa(iv)
            x = para_fonte(max(0.0, float(t)))
            i = next((k for k, tk in enumerate(self.stream) if tk["e"] > x),
                     max(0, len(self.stream) - 1))
            return {"fonte": round(x, 3), "i": i}

    # ------------------------------------------------------------ um frame so

    def gera_frame(self, t):
        """Um quadro so do video FONTE no instante `t`, so para dar uma
        ideia visual - nao participa do corte, nao le nem escreve
        montagem nenhuma, nao fica no disco.

        `-ss` ANTES do `-i` busca o keyframe anterior e decodifica so ate
        o instante pedido (e "seek exato" e o padrao do ffmpeg ha anos,
        nao e preciso pedir). Sem hwaccel de proposito: o gargalo do
        `[render].gpu` e decodificar TODOS os quadros de um trecho longo
        em software, e aqui e um quadro so, perto de onde a busca ja
        chegou - GPU nao muda nada que valha a complicacao.

        `-strict unofficial`: MEDIDO no IMG_0246 perto do fim (267s de
        267,6) - o encoder mjpeg recusa codificar com "Non full-range YUV
        is non-standard" e o frame sai vazio, calado (`-v error` engole o
        aviso, so o erro do encoder aparece). O decode HEVC deste projeto
        e limited-range (source Dolby Vision, ver meta.json); o mjpeg da
        pra codificar assim, so nao por padrao.

        O teto de `self.dur - 0.15` e outra medicao no mesmo lugar: pedir
        o instante exato do fim falha porque nao ha quadro NENHUM depois
        dele para decodificar - `dur` e o fim do arquivo, nao o tempo do
        ultimo quadro. MEDIDO no IMG_0246 (267,613s, ~60 fps): 267,55
        ainda acha quadro, 267,6 nao acha nenhum; 0,15 da folga de sobra
        sem mudar o que se ve (e so o ULTIMO clique perto do fim que cai
        nesta margem, e a imagem de dois quadros antes e a mesma cena)."""
        teto = max(0.0, self.dur - 0.15)
        t = max(0.0, min(teto, float(t)))
        try:
            r = subprocess.run(
                [tc.FFMPEG, "-y", "-v", "error", "-ss", f"{t:.3f}",
                 "-i", str(self.video), "-frames:v", "1", "-q:v", "2",
                 "-strict", "unofficial",
                 "-f", "image2pipe", "-vcodec", "mjpeg", "-"],
                capture_output=True, timeout=20)
        except subprocess.TimeoutExpired:
            raise RuntimeError(f"ffmpeg nao respondeu em 20s buscando {t:.2f}s")
        if r.returncode != 0 or not r.stdout:
            erro = (r.stderr or b"").decode("utf-8", "replace").strip()
            raise RuntimeError(erro[:300] or "ffmpeg nao gerou frame nenhum")
        return r.stdout

    # ------------------------------------------------------------- calculo

    def _aplica(self):
        """Empilha a camada de palavra sobre a linha de base.

        O `why` entra junto, e com a MESMA frase do `aplica_montagem` - as
        duas sao a mesma decisao, e enquanto discordavam era o editor que
        ficava com a versao velha da verdade. Tres coisas dependiam disso e
        so funcionavam depois de salvar e reabrir:

        - a caixa "Por que saiu" mostrava "anotado: O1 ..." num token que a
          pessoa acabara de restaurar, isto e, o motivo de um corte que ela
          ja tinha desfeito;
        - `_respeita_palavras` defende o token pela frase de `MAO_MANTEM`, e
          sem ela restaurar hesitacao dentro de anotacao nao fazia nada;
        - `pendencias` le a mesma frase para dizer que a pergunta ja foi
          respondida a mao.

        `_why_base` e o motivo da proposta automatica, e voltar a ele quando
        o clique e desfeito faz parte do mesmo acerto: pintar "restaurado no
        editor" num token que voltou a ser decisao da regra seria a mentira
        simetrica."""
        for i, t in enumerate(self.stream):
            cut, prot = self.base[i]
            est = self.tokens.get(i)
            if est is None:
                t["cut"], t["prot"] = cut, prot
                t["why"] = self._why_base[i]
            else:
                # `prot` protege enumeracao do corte AUTOMATICO; nao
                # autoriza sobreviver a um clique (ver `aplica_montagem`).
                t["cut"], t["prot"] = (est == "cortar"), False
                t["why"] = ("oculto no editor" if est == "cortar"
                            else "restaurado no editor")

    def montagem(self):
        toks = []
        for i in sorted(self.tokens):
            t = self.stream[i]
            toks.append({"i": i, "estado": self.tokens[i], "t": t["t"],
                         "s": round(t["s"], 3), "e": round(t["e"], 3)})
        return {"versao": 1, "video": self.src.name, "selo": self.selo,
                "tokens": toks, "cortes": self.cortes,
                "restauros": self.restauros}

    def correcoes_doc(self):
        itens = []
        for i in sorted(self.correcoes):
            c = self.correcoes[i]
            itens.append({"i": i, "s": c["s"], "e": c["e"],
                          "orig": c["orig"], "novo": c["novo"]})
        return {"versao": 1, "video": self.src.name, "itens": itens}

    def _corrige(self, i, novo):
        """Corrige o texto do token `i`. Preserva o `orig` de uma
        correcao anterior no mesmo token - senao corrigir duas vezes
        esqueceria o erro que o Whisper cometeu de verdade e passaria a
        registrar a propria correcao anterior como se fosse o erro."""
        t = self.stream[i]
        orig = self._t_orig[i]
        if novo == orig:
            self.correcoes.pop(i, None)
        else:
            prev = self.correcoes.get(i)
            self.correcoes[i] = {"s": round(t["s"], 3), "e": round(t["e"], 3),
                                 "orig": prev["orig"] if prev else orig,
                                 "novo": novo}
        self._aplica_correcoes()

    def intervalos(self):
        """A unica fonte de verdade do corte, para a tela e para o render.

        Cacheado pelas duas camadas que o produzem, e por mais nada: andar a
        selecao nao muda um milissegundo do corte, e mesmo assim custava os
        44 ms desta funcao a cada palavra por que um arrasto passava. As
        correcoes ficam FORA da chave de proposito - `correcoes.json` e sobre
        o que a palavra diz, nunca sobre se ela sai, e nenhum passo daqui
        para baixo le `t["t"]`. No dia em que ler, esta linha vira mentira e
        e aqui que ela se conserta.

        A lista devolvida e a mesma referencia entre chamadas; ninguem neste
        programa escreve num `iv` (o `aplica_manual` ja constroi listas
        novas)."""
        with self.lock:
            self._aplica()
            chave = (tuple(sorted(self.tokens.items())),
                     repr(self.cortes), repr(self.restauros))
            if chave != self._iv_chave:
                self._iv = tc.intervalos(self.stream, self.C, self.dur,
                                         self.db, self.hop, self.anot,
                                         self.montagem())
                self._iv_chave = chave
            return self._iv

    # ------------------------------------------------ o que a geometria cede

    def _geometria(self):
        """O corte que a GEOMETRIA sozinha faria com as decisoes de palavra
        de agora - sem a camada de tempo.

        Nao e o corte que sai, e nao serve para desenhar nada. Serve para
        UMA pergunta: quanto a geometria teve de ceder para a promessa ser
        cumprida? Sem esta testemunha, `_palavras` completaria em silencio -
        e completar em silencio e a versao nova do defeito que este programa
        inteiro persegue.

        Cacheado pela mesma chave que o `_pend`, e pela mesma razao: um
        gesto de milissegundo mexe no `iv`, nunca na camada de palavra."""
        with self.lock:
            chave = tuple(sorted(self.tokens.items()))
            if chave != self._geo_chave:
                self._aplica()
                self._geo = tc.intervalos(self.stream, self.C, self.dur,
                                          self.db, self.hop, self.anot, None)
                self._geo_chave = chave
            return self._geo

    def completados(self):
        """"A geometria deixou 0,06 s de 'de' tocando; completei no tempo
        exato." Uma linha por palavra em que as duas camadas divergiram.

        MEDIDO: `folga_palavra` recua a emenda 0,06 s de cada palavra
        mantida vizinha e `minimo` descarta corte menor que 0,28 s, entao
        ocultar 'de', 'que', 'e', 'um', 'no' ou 'ou' - as palavras que mais
        aparecem numa gaguejada - nao fazia nada. O programa avisava, num
        painel lateral desconectado da palavra clicada, e o aviso estava
        certo e ninguem lia.

        O preco de cortar na fronteira alinhada esta escrito no comentario
        de `folga_palavra`: 0,06 e o erro MEDIDO de fronteira do alinhador
        (72% dos fins de palavra do Whisper caem tarde, mediana +27 ms).
        Cortar exato troca "sobra farelo do que se mandou sair" por "pode
        comer a silaba do vizinho" - risco simetrico, e agora visivel na
        onda e audivel num clique."""
        geo = self._geometria()
        out = []
        for i in sorted(self.tokens):
            t = self.stream[i]
            span = [[t["s"], t["e"]]]
            if self.tokens[i] == "cortar":
                falta = [[max(a, t["s"]), min(b, t["e"])] for a, b in geo
                         if min(b, t["e"]) - max(a, t["s"]) > MINIMO]
            else:
                falta = tc.subtrai_spans(span, geo)
            seg = sum(b - a for a, b in falta)
            if seg <= MINIMO:
                continue
            out.append({"i": i, "ini": round(t["s"], 3),
                        "fim": round(t["e"], 3), "seg": round(seg, 3),
                        "estado": self.tokens[i], "palavra": t["t"]})
        return out

    # --------------------------------------------------------------- gestos

    def _empilha(self):
        self.pilha.append((dict(self.tokens),
                           [list(c) for c in self.cortes],
                           [list(r) for r in self.restauros],
                           {i: dict(c) for i, c in self.correcoes.items()}))
        del self.pilha[:-200]

    def _corta(self, span):
        """Corte de milissegundo. Sai do conjunto contrario antes de entrar
        no seu - e essa disjuncao que faz arrastar de volta desfazer EXATO,
        sem heuristica nenhuma: o gesto nomeia o trecho, e o gesto contrario
        sobre o mesmo trecho o devolve."""
        self.restauros = tc.subtrai_spans(self.restauros, [span])
        self.cortes = tc.uniao_spans(self.cortes, [span])

    def _restaura(self, span):
        self.cortes = tc.subtrai_spans(self.cortes, [span])
        self.restauros = tc.uniao_spans(self.restauros, [span])

    def _palavras(self, i0, i1, estado):
        """A decisao de palavra, nas DUAS camadas.

        A camada de palavra e a que importa e a que continua mandando: ela
        passa por toda a geometria de emenda, e e por isso que o corte sai
        com poucas emendas e cada uma pousada no silencio. O trecho de tempo
        que desce junto e a garantia - ele entra por ultimo, sem snap e sem
        minimo, e so pode ACRESCENTAR o que a geometria nao deu conta de
        tirar (ou devolver o que ela nao deu conta de trazer). Onde as duas
        concordam, ele nao muda um milissegundo; onde discordam, quem se
        dobra e a geometria.

        Que os dois caminhos dao o MESMO corte nao e sorte de implementacao,
        e aritmetica: o corte final e (geometria uniao este trecho), e
        [s,e] = ([s,e] dentro do corte) uniao ([s,e] fora dele). Completar
        so o residuo daria byte a byte a mesma coisa - so que gravaria no
        `montagem.json` um numero que nao se pode reler ("os 60 ms que
        sobraram naquele dia") em vez do que a pessoa disse ("esta palavra").

        MEDIDO nos tres de calibracao, ocultando cada palavra mantida uma de
        cada vez: a palavra sumia de verdade em 7 de 306 (IMG_0246), 21 de
        660 (IMG_0247) e 20 de 742 (IMG_0248) - e sobrevivia INTEIRA em 168,
        343 e 391. Nao e caso de borda: `minimo` (0,28) mais as duas folgas
        de `folga_palavra` (0,06) anulam o corte de toda palavra menor que
        0,40 s, que sao quase todas as palavras curtas do portugues falado.

        Voltar a proposta automatica tira as duas camadas, e tem de tirar:
        o trecho de tempo aqui nao e ajuste de milissegundo de ninguem, e a
        sombra de uma decisao de palavra. Deixa-lo para tras faria a palavra
        continuar cortada depois de restaurada - a promessa quebrada do
        outro lado."""
        for i in range(max(0, i0), min(len(self.stream), i1)):
            cut, _ = self.base[i]
            t = self.stream[i]
            span = [t["s"], t["e"]]
            if (estado == "cortar") == cut:
                self.tokens.pop(i, None)   # voltou a proposta: nao e decisao
                self.cortes = tc.subtrai_spans(self.cortes, [span])
                self.restauros = tc.subtrai_spans(self.restauros, [span])
            else:
                self.tokens[i] = estado
                (self._corta if estado == "cortar" else self._restaura)(span)

    def selecao(self, sel):
        """A selecao da tela -> (ini, fim, i0, i1) em segundos e em tokens.

        Palavra e tempo tem de ser a MESMA selecao, dita de dois jeitos: o
        texto manda `{i0, i1}`, a onda manda `{ini, fim}`, e as duas voltam
        com os quatro numeros. Foi esse o pedido - "se eu selecionar uma
        palavra na transcricao, o correspondente a ela aparece na faixa de
        audio" -, e a traducao mora aqui, de um lado so, pela mesma razao
        que tudo o mais: duas conversoes da mesma coisa divergem, e a que
        diverge calada e a que ninguem esta olhando.

        A varredura por tempo e linear de proposito: o alinhador as vezes
        devolve tokens fora de ordem (ver `tc.fora_de_ordem`) e uma
        bisseccao responderia errado justo neles.

        O cursor entre LETRAS saiu junto com os botoes. Ele repartia o tempo
        do token por interpolacao de caractere, e isso nunca foi a posicao
        fonetica: o alinhador entrega a fronteira da PALAVRA, e num "eee" de
        200 caracteres para uma vogal so o texto mentia por completo sobre a
        estrutura do som. Quem responde "que instante e este" agora e a
        onda, que mostra o instante."""
        sel = sel or {}
        n = len(self.stream)
        if not n:
            return 0.0, 0.0, 0, 0
        if "i0" in sel:
            i0 = max(0, min(n - 1, int(sel["i0"])))
            i1 = max(i0 + 1, min(n, int(sel["i1"])))
            return self.stream[i0]["s"], self.stream[i1 - 1]["e"], i0, i1
        if "ini" in sel:
            a = max(0.0, min(self.dur, float(sel["ini"])))
            b = max(a, min(self.dur, float(sel.get("fim", a))))
            # METADE do token, e nao um epsilon: os tokens sao contiguos (o
            # fim de um e o comeco do outro) e as vezes se SOBREPOEM de
            # verdade - o alinhador devolve 'a' em 118,008-118,028 e 'gente'
            # comecando em 118,010, no IMG_0248. Com um epsilon, selecionar
            # 'a' vinha com 'gente' de brinde e clicar numa palavra na onda
            # acendia tres na folha. Metade e a mesma regra que o
            # `Sessao.avisos` usa para a mesma especie de pergunta ("12% de
            # 'cavaleiras' e nada, 100% de 'de' e a palavra inteira"), e ela
            # se paga sozinha: 18 ms de sobreposicao nao sao metade de
            # 'gente', e sao a palavra 'a' inteira.
            meio = (a + b) / 2
            dentro = [i for i, t in enumerate(self.stream)
                      if min(t["e"], b) - max(t["s"], a) > (t["e"] - t["s"]) / 2]
            if not dentro:
                # Um clique (largura zero) ou um arrasto pequeno dentro de
                # uma palavra grande: nao cobre metade de ninguem. Vale o
                # token que CONTEM o instante, e entre dois que o contenham,
                # aquele de que ele esta mais no meio.
                cont = [i for i, t in enumerate(self.stream)
                        if t["s"] <= meio < t["e"]]
                if cont:
                    dentro = [min(cont, key=lambda i: abs(
                        (self.stream[i]["s"] + self.stream[i]["e"]) / 2 - meio))]
                else:
                    # Caiu num vao entre dois tokens. Pega o SEGUINTE, a
                    # mesma escolha que `em_previa` faz e a mesma que clicar
                    # no tracinho da folha faz desde sempre.
                    dentro = [next((k for k, tk in enumerate(self.stream)
                                    if tk["e"] > meio), n - 1)]
            i0, i1 = min(dentro), max(dentro) + 1
            return self.stream[i0]["s"], self.stream[i1 - 1]["e"], i0, i1
        return 0.0, 0.0, 0, 0

    def acao(self, b):
        """Um gesto -> o estado novo. Toda a tela sai daqui.

        Dois tipos de gesto, e a diferenca entre eles e a diferenca entre as
        duas camadas do `montagem.json`:

        - `ocultar`/`restaurar`/`alterna` NOMEIAM PALAVRA. Passam pela
          geometria e descem tambem para o tempo (ver `_palavras`).
        - `cortar_tempo`/`restaurar_tempo` NOMEIAM TEMPO. E o arrasto na
          onda: entra exato, sem snap e sem minimo, porque quem arrastou
          esta olhando a onda e quer aquilo. Um gesto desfaz o outro por
          construcao - `_corta` e `_restaura` mantem os dois conjuntos
          disjuntos com `subtrai_spans`, entao arrastar de volta por cima
          devolve exatamente o que se tirou, sem heuristica nenhuma.

        Os botoes de mais e menos e as teclas de passo fixo sairam daqui em
        08/09/2026, e com eles a pergunta "o que este botao faria agora"
        (`gestos`, `_devolve`, `vizinhanca`): a resposta deles era boa e a
        pergunta era o problema. O passo fixo obrigava a escolher uma
        unidade antes de saber de quanto era o pedaco, e a prioridade
        "desfaz o SEU corte antes do automatico" - correta e invisivel - era
        a origem provavel do "peco para restaurar e ele nao restaura"."""
        with self.lock:
            tipo = b.get("acao") or "nada"
            ini, fim, i0, i1 = self.selecao(b.get("sel"))
            mudou = True

            if tipo == "desfazer":
                if self.pilha:
                    (self.tokens, self.cortes, self.restauros,
                     self.correcoes) = self.pilha.pop()
                    self._aplica_correcoes()
                else:
                    mudou = False
            elif tipo == "corrigir":
                # `i` vem direto do payload, NUNCA de `sel`: a pre-selecao
                # do clique com botao direito e assincrona (POST "nada"
                # separado), e mandar o indice explicito evita a corrida
                # de quem chega primeiro - medido de verdade, um "corrigir"
                # disparado logo depois do menu chegou a sair ANTES da
                # selecao assentar, com sel vazio, e o servidor recusou em
                # silencio (i0=i1=0) enquanto a tela, otimista, continuava
                # mostrando o texto digitado como se tivesse sido salvo.
                i = b.get("i")
                if not isinstance(i, int) or not (0 <= i < len(self.stream)):
                    mudou = False
                else:
                    t = self.stream[i]
                    txt = (b.get("texto") or "").strip()
                    if not self._corrigivel(i) or not txt or txt == t["t"]:
                        mudou = False
                    else:
                        self._empilha()
                        self._corrige(i, txt)
            elif tipo in ("ocultar", "restaurar", "alterna"):
                if i1 <= i0:
                    mudou = False
                else:
                    self._aplica()
                    if tipo == "alterna":
                        tudo = all(self.stream[i]["cut"]
                                   for i in range(i0, i1))
                        tipo = "restaurar" if tudo else "ocultar"
                    self._empilha()
                    self._palavras(i0, i1,
                                   "cortar" if tipo == "ocultar" else "manter")
            elif tipo in ("cortar_tempo", "restaurar_tempo"):
                # O arrasto na onda. Aparado na duracao do video e recusado
                # com largura zero pelo mesmo 1 ms de `tc._spans`: a 48 kHz
                # sao 48 amostras, e ninguem arrasta isso de proposito.
                try:
                    x = max(0.0, min(self.dur, float(b.get("ini"))))
                    y = max(0.0, min(self.dur, float(b.get("fim"))))
                except (TypeError, ValueError):
                    x = y = 0.0
                x, y = min(x, y), max(x, y)
                if y - x <= MINIMO:
                    mudou = False
                else:
                    self._empilha()
                    (self._corta if tipo == "cortar_tempo"
                     else self._restaura)([x, y])
            else:
                mudou = False

            if mudou:
                self.grava()
            return self.enxuga(self.estado(ini, fim, i0, i1, mudou))

    def grava(self):
        """Poe no disco o corte que a TELA esta mostrando.

        Sai do `acao` porque agora ha um segundo momento em que isso e
        obrigatorio: antes de qualquer subprocesso (`audio`, `render`,
        `exportar`) ler o `montagem.json`. O `_cumpre_carga` completa a
        decisao de palavra que a geometria deixou pela metade AO ABRIR, e
        so entrava no disco no primeiro gesto - quem abrisse o editor e
        clicasse direto em "exportar" recebia um arquivo com o corte de
        antes da correcao, ate 2 s diferente do que a tela mostrava.
        MEDIDO no "2 de espadas": o editor mostra 2min18 e o disco tinha
        2min16.

        Nao e gravacao silenciosa: o que vai para o disco e exatamente o
        que esta na tela, e a diferenca que a carga corrigiu ja esta
        escrita no `aviso` desde que a sessao abriu."""
        with self.lock:
            tc.jdump(self.montagem(), self.d / "montagem.json")
            tc.jdump(self.correcoes_doc(), self.d / "correcoes.json")

    # ------------------------------------------------------------- a tela

    def _chave_corte(self, iv):
        """A impressao digital de tudo o que NAO depende da selecao.

        Mover a selecao nao muda o corte, nao muda o riscado e nao muda um
        achado sequer - e mesmo assim `pecas` + `achados` custavam 120 ms de
        servidor e 330 KB de JSON a cada palavra por que o arrasto passava.
        MEDIDO no IMG_0247: 247 ms por gesto, e um arrasto de dez palavras
        sao dez gestos. Agora, medido do mesmo jeito: **selecao 7 ms e 8 KB,
        arrasto na onda 210-250 ms, gesto de palavra 500 ms** - esse ultimo
        refaz `pendencias` e a geometria-testemunha, e nao tem como nao
        refazer.

        As correcoes entram na chave porque mudam o TEXTO das pecas sem
        mexer num milissegundo do corte - foi por isso que elas nasceram
        separadas -, e os tokens entram porque `completados` compara a
        camada de palavra com a geometria."""
        return (repr(iv), tuple(sorted(self.tokens.items())),
                tuple(sorted((i, c["novo"])
                             for i, c in self.correcoes.items())))

    def estado(self, ini=0.0, fim=0.0, i0=0, i1=0, mudou=False):
        """O retrato INTEIRO. Quem chama isto sempre recebe as pecas."""
        with self.lock:
            iv = self.intervalos()
            return self._retrato(iv, ini, fim, i0, i1, mudou)

    def enxuga(self, est):
        """Tira da resposta as pecas que a tela ja tem.

        Sao 330 KB dos 338 de cada resposta, e nao mudam quando o gesto so
        anda a selecao - que e o gesto mais frequente que existe aqui, um
        por palavra por que um arrasto passa.

        Mora no caminho do FIO, e nao no `estado`, de proposito: um
        `estado()` que as vezes devolve `pecas: None` e uma armadilha para
        todo mundo que nao seja o navegador (o teste caiu nela na primeira
        versao disto). Aqui e o unico lugar que sabe o que ja foi mandado."""
        with self.lock:
            if self._corte_chave == self._pecas_chave:
                return dict(est, pecas=None)
            self._pecas_chave = self._corte_chave
            return est

    def _retrato(self, iv, ini, fim, i0, i1, mudou):
        mantido = sum(b - a for a, b in iv)
        _, para_previa = tc._mapa_previa(iv)
        chave = self._chave_corte(iv)
        if chave != self._corte_chave:
            self._corte_chave = chave
            self._corte = {
                "pecas": self.pecas(iv),
                "avisos": self.avisos(iv),
                "completados": self.completados(),
                "achados": self.achados(iv),
            }
        return {
            "iv": iv,
            # O tempo de PREVIA em que cada trecho de `iv` comeca - a soma
            # corrida dos anteriores. E a mesma conta da `tc._mapa_previa`,
            # feita aqui e mandada pronta porque a regua da onda precisa
            # dela dez vezes por redesenho e um POST por marca de tempo
            # seria absurdo. A tela LE esta tabela; nao a recalcula, e nao
            # tem como: quem sabe onde a lamina caiu e quem cortou.
            "ivp": [round(p, 3) for p in tc.somas_previa(iv)],
            "pecas": self._corte["pecas"],
            # A selecao RESOLVIDA. A tela pode ter mandado palavra ou tempo;
            # volta sempre com os dois, e e assim que "selecionei no texto e
            # acendeu na onda" (e o contrario) e uma coisa so, e nao duas
            # implementacoes que um dia divergem.
            "sel": {"i0": i0, "i1": i1,
                    "ini": round(ini, 3), "fim": round(fim, 3),
                    "previaIni": round(para_previa(ini), 2),
                    "previaFim": round(para_previa(fim), 2)},
            "mantido": mantido,
            "dur": self.dur,
            "previa": self.previa_no_disco(mantido),
            # "trechos", e nao "emendas": o `conferir` ja chama len(iv)
            # de emendas, e dois numeros diferentes com o mesmo nome na
            # mesma tela e pedir para alguem comparar errado.
            "trechos": len(iv),
            "mao": len(self.tokens) + len(self.cortes) + len(self.restauros),
            "podeDesfazer": bool(self.pilha),
            "avisos": self._corte["avisos"],
            "completados": self._corte["completados"],
            "achados": self._corte["achados"],
            "mudou": mudou,
        }

    def pecas(self, iv):
        """Um item por token, ja pronto para desenhar.

        As fracoes em `vivo` sao a fatia MANTIDA do token, de 0 a 1, e elas
        servem as duas vistas: na FOLHA riscam meia palavra repartindo o
        tempo pelas letras, que e a unica leitura possivel ali (o alinhador
        entrega a fronteira da palavra, nao a do fonema); na ONDA sao a
        mesma fracao desenhada sobre o eixo do tempo, onde ela foi medida -
        estritamente mais fiel, e sem numero novo saindo daqui.

        Foi por isso que o corte "por letra" (o ctrl+clique) pode sair: ele
        fingia que a interpolacao era posicao fonetica, e num "eee" de 200
        caracteres para uma vogal so o texto mentia por completo sobre a
        estrutura do som. Quem responde "que instante e este" agora e a
        onda, que mostra o instante.

        `ocultoAntes` e o silencio comido no vao entre um token e o
        anterior. Sem ele, 15 s de gravacao morta no fim do video sumiriam
        da tela sem deixar marca: seria "ocultado" que nao aparece, que e
        exatamente o que este editor existe para nao fazer.

        A busca e por bisseccao, e nao um ponteiro que anda junto com os
        tokens, porque o alinhador as vezes devolve tokens fora de ordem
        (ver `tc.fora_de_ordem`) e um ponteiro so-para-frente daria a
        resposta errada justo neles. Comparar cada token com todos os
        trechos custava 25 ms por clique, medido - metade do atraso entre
        apertar o botao e ver o risco mudar."""
        fins = [b for _, b in iv]
        n = len(iv)
        pec, ant = [], 0.0
        for i, t in enumerate(self.stream):
            s, e = t["s"], t["e"]
            larg = max(1e-6, e - s)
            vivo, visto = [], 0.0
            j = bisect.bisect_right(fins, min(ant, s))
            while j < n and iv[j][0] < e - 1e-9:
                a, b = iv[j]
                x, y = max(a, s), min(b, e)
                if y > x + 1e-6:
                    vivo.append([round((x - s) / larg, 5),
                                 round((y - s) / larg, 5)])
                visto += max(0.0, min(b, s) - max(a, ant))
                j += 1
            gap = max(0.0, s - ant)
            # hesitacao sem texto recuperado vem rotulada "hesitação" pelo
            # analyze: e etiqueta, nao fala. Ipsis litteris ela e um vao.
            # Uma pausa so tem texto se alguem escreveu ali (o analyze cria
            # todo silencio com t=""), e ai ela deixa de ser vao - senao
            # escrever no token nao mudaria nada na tela.
            if t["k"] == "palavra":
                texto = True
            elif t["k"] == "hesitacao":
                texto = t["t"] != "hesitação"
            else:
                texto = bool(t["t"])
            # `voz` separa as duas coisas que a tela desenhava com o mesmo
            # tracinho: pausa e fala que o Whisper nao leu. Um token
            # `hesitacao` e voz por definicao - e por isso que ele existe -,
            # e um `silencio` tambem pode ser, quando nasceu de uma lacuna
            # `misto` cujo texto foi recusado (o analyze trata recusa como
            # silencio). MEDIDO no IMG_0247: 26 tracinhos de pausa e 34 de
            # voz, indistinguiveis, e o title dizia "sem som aqui" nos 34.
            pec.append({"i": i, "txt": t["t"] if texto else "",
                        "vao": not texto, "s": round(s, 3), "e": round(e, 3),
                        "vivo": vivo, "why": t.get("why", ""),
                        "voz": self._voz[i],
                        # o que o Whisper leu ali e o programa jogou fora.
                        # Estava gravado em stream.json desde sempre e nao
                        # chegava a tela.
                        "ouviu": self._ouviu[i][0],
                        "recusa": self._ouviu[i][1],
                        "sugestao": self._ouviu[i][2],
                        "corrigivel": self._corrigivel(i),
                        "vozAntes": self._voz_antes[i],
                        # quem mandou cortar: vazio quando foi a regra
                        # automatica, o motivo escrito quando foi uma
                        # pessoa lendo a aula.
                        "anot": self._anot_de[i],
                        "anotAntes": self._anot_antes[i],
                        "ocultoAntes": round(max(0.0, gap - visto), 3)})
            ant = max(ant, e)
        sobra = max(0.0, self.dur - ant)
        visto = sum(max(0.0, min(b, self.dur) - max(a, ant)) for a, b in iv)
        # A cauda depois do ultimo token responde as MESMAS perguntas que
        # qualquer vao do meio - ela so nao respondia porque nao ha token
        # onde pendurar a resposta, e sao 11 a 21 s de video em cada um dos
        # tres de calibracao. `fimAnot` acerta hoje: os 15,07 s do IMG_0246
        # saem porque uma pessoa escreveu "fim do conteudo", e o tracinho
        # dizia isso com a mesma cara de uma pausa qualquer. `fimVoz` da
        # False nos tres, e existe pelo mesmo motivo que o do meio existe -
        # o dia em que der True e o dia em que ninguem estaria olhando.
        cauda = ""
        for a in self.anot:
            if min(a["fim"], self.dur) - max(a["ini"], ant) > 0.02:
                cauda = a["motivo"]
        return {"tokens": pec, "fim": round(max(0.0, sobra - visto), 3),
                "fimS": round(ant, 3), "fimE": round(self.dur, 3),
                "fimVoz": self._voz_no_vao(ant, self.dur), "fimAnot": cauda}

    def avisos(self, iv):
        """"Voce mandou e nao saiu." Duas perguntas, uma so resposta.

        A primeira e a que este programa ja faz em todo caminho: alguma
        anotacao do passe de julgamento voltou ao video? Aqui ela tem uma
        resposta a mais que no `render` - se o que sobrou foi a propria
        pessoa que mandou ficar, nao e a anotacao sumindo no calculo, e ela
        sendo desdita de olhos abertos. Sao duas frases diferentes.

        A segunda MUDOU DE PAPEL em 08/09/2026, e a mudanca e o assunto
        deste editor. Ela dizia "voce ocultou 'de' e 140 ms dela continuam
        tocando; termine com o botao de menos" - correta, e frequente: nos
        tres videos de calibracao, ocultar uma palavra sozinha nao tirava
        NADA dela em 168, 343 e 391 casos. Hoje `_palavras` cumpre o pedido
        pela camada de tempo, e esta linha vira o que sempre deveria ter
        sido: **alarme de regressao.** Ela dispara quando a promessa deixou
        de valer, e a resposta certa a ela nao e um botao, e um conserto
        aqui dentro.

        Com uma excecao, e ela e legitima: o arrasto na onda entra DEPOIS e
        pode contradizer a palavra de proposito - ocultar a palavra e
        devolver a metade dela arrastando e um pedido perfeitamente claro.
        Quem sabe distinguir e este lado, pelos `restauros`/`cortes` que a
        propria pessoa escreveu; sai como `mao`, cinza, do mesmo jeito e
        pela mesma razao que a anotacao reaberta a mao. Alarme que acende
        sobre o que alguem acabou de pedir vira papel de parede, e papel de
        parede e o mesmo que apagar o alarme."""
        out = []
        # o mesmo relogio que o `conferir` poe na primeira coluna: esta
        # lista se ouve no .m4a, e nao adianta ordenar por tempo de fonte
        # quem vai conferir de ouvido.
        _, para_previa = tc._mapa_previa(iv)
        mao = tc.spans_da_mao(self.stream, self.montagem(),
                              self.C["corte"]["folga_palavra"])
        for x, y, vivo, motivo, da_mao in tc.anotacoes_desobedecidas(
                self.anot, iv, mao=mao):
            out.append({"ini": x, "fim": y, "vivo": round(vivo, 2),
                        "previa": round(para_previa(x), 2),
                        "motivo": motivo, "mao": da_mao})
        for i, estado in sorted(self.tokens.items()):
            t = self.stream[i]
            larg = t["e"] - t["s"]
            if larg <= 0.02:
                continue
            vivo = sum(max(0.0, min(t["e"], b) - max(t["s"], a))
                       for a, b in iv)
            # metade, e nao um numero em segundos: a borda que sobra por
            # construcao e sempre a mesma (0,12 s), mas o que ela
            # representa depende do tamanho da palavra - 12% de "cavaleiras"
            # e nada, 100% de "de" e a palavra inteira.
            fora = (vivo / larg < 0.5) if estado == "cortar" else (
                vivo / larg > 0.5)
            if fora:
                continue
            # Quem desdisse a palavra: o arrasto da propria pessoa, ou a
            # geometria? Depois de `_palavras`, o trecho [s,e] esta inteiro
            # no conjunto que a decisao pediu, entao a UNICA maneira de
            # sobrar do lado errado e um gesto de tempo posterior por cima.
            # Se sobrar por outro caminho, e regressao - e e para isso que
            # esta linha existe agora.
            seu = self.restauros if estado == "cortar" else self.cortes
            desdito = any(min(t["e"], y) - max(t["s"], x) > 0.02
                          for x, y in seu)
            out.append({
                "ini": t["s"], "fim": t["e"], "vivo": round(vivo, 2),
                "previa": round(para_previa(t["s"]), 2),
                "mao": desdito, "palavra": t["t"],
                # QUAL dos dois pedidos foi desobedecido. A frase abaixo
                # ja dizia, mas o rotulo da lista "o que ouvir" era
                # "NAO SAIU" nos dois casos, e no restauro isso diz o
                # contrario do que aconteceu: a palavra nao VOLTOU. Quem
                # sabe qual e qual e este lado; a tela escolhe a
                # palavra, nao recalcula nada.
                "estado": estado,
                "motivo": (
                    f"voce ocultou {t['t']!r} e depois devolveu "
                    f"{vivo*1000:.0f} ms dela na onda"
                    if desdito and estado == "cortar" else
                    f"voce restaurou {t['t']!r} e depois tirou parte dela "
                    f"na onda; sobram {vivo*1000:.0f} ms"
                    if desdito else
                    f"voce ocultou {t['t']!r} e {vivo*1000:.0f} ms dela "
                    f"continuam tocando - isto nao deveria acontecer mais"
                    if estado == "cortar" else
                    f"voce restaurou {t['t']!r} e ela continua fora - isto "
                    f"nao deveria acontecer mais")})
        return out

    def achados(self, iv):
        """O que o `conferir` acharia neste corte, agora, sem sair da tela.

        Pela MESMA funcao que o comando usa, pela razao de sempre: duas
        implementacoes da mesma pergunta divergem, e a que diverge calada e
        a que ninguem esta olhando. MEDIDO no IMG_0248: o `conferir` achava
        25 pontos e este painel mostrava zero deles - ilha curta, arrasto
        sobrevivente, repeticao que o proprio corte criou, pendencia sem
        resposta, e sobretudo as 12 falas marcadas para sair que continuam
        tocando INTEIRAS. Para ver qualquer um era preciso rodar um comando
        e ler um txt em segundos da FONTE, numero que a folha nao mostra em
        canto nenhum.

        `pendencias` custa 45 ms e le o `cut` de cada token, entao so se
        refaz quando a camada de palavra muda - um gesto de milissegundo
        mexe no `iv`, nunca no fluxo. O vocabulario e de disco e nao muda
        enquanto a sessao vive."""
        with self.lock:
            self._aplica()
            chave = tuple(sorted(self.tokens.items()))
            if chave != self._pend_chave:
                self._pend = tc.pendencias(self.stream, self.C, self._vocab)
                self._pend_chave = chave
            ach, _ = tc.achados_corte(self.stream, self.C, iv, self.anot,
                                      self.montagem(), self._pend,
                                      tc.carrega_respostas(self.d), self.d)
            # O que o `avisos` ja diz, e diz melhor, nao se repete aqui: as
            # duas familias de anotacao (la vem com a frase que a pessoa
            # escreveu) e a fala cortada A MAO que sobrevive (la vem em
            # segunda pessoa e com o conserto - "termine com o botao de
            # menos"). O mesmo alarme em duas listas da mesma tela ensina a
            # ignorar as duas.
            meus = {(round(self.stream[i]["s"], 2),
                     round(self.stream[i]["e"], 2)) for i in self.tokens}
            out = []
            for prev, rot, x, y, txt in ach:
                if rot.startswith("ANOTACAO"):
                    continue
                if (rot == "FALA CORTADA SOBREVIVE"
                        and (round(x, 2), round(y, 2)) in meus):
                    continue
                out.append({"ini": x, "fim": y, "rot": rot, "txt": txt,
                            "previa": round(prev, 2)})
            return out

    # --------------------------------------------------------- modo cartas

    def _cenas(self, iv):
        """Os cartoes de legenda deste corte, com o que foi escrito a mao
        por cima. Sai de `tc.monta_legendas` - o MESMO lugar de onde sai o
        .srt do `exportar` -, senao a tela mostraria uma legenda e o
        arquivo levaria outra.

        `amplia_audio_legendas` so entra AQUI, depois do .srt ja estar
        decidido: ela acrescenta `audio_s`/`audio_e` (o trecho que o BOTAO
        DE TOCAR de cada cartao usa, maior que o [s,e] do texto) sem tocar
        em `s`/`e`/`txt` - o exportar nunca chama essa funcao, e nao
        precisa: ela existe para o modo cartas tocar o MESMO audio que o
        modo onda toca, nunca para decidir legenda."""
        cenas = tc.monta_legendas(self.stream, iv, self.C)
        orfas = tc.aplica_legendas(cenas, list(self.legendas.values()))
        tc.amplia_audio_legendas(cenas, self.dur)
        return cenas, orfas

    def cartas_estado(self):
        """Tudo o que a tela do modo cartas precisa, de uma vez.

        Quem le o roteiro e monta os eventos e o `tc.parse_roteiro` - o
        MESMO que o `exportar` usa. A tela nao decide que carta aparece,
        nem quando, nem de que lado, pela mesma razao que ela nao calcula
        corte: duas leituras da mesma sintaxe divergem, e a que diverge
        calada e a que ninguem esta olhando.

        Cada evento volta com o `diz` - o que se ouve naquele instante,
        tirado do proprio cartao de legenda. E ele, e nao o aviso de
        caducidade, a defesa de verdade contra o corte que mudou depois do
        roteiro: um `[1:29]` que escorregou passa a mostrar outra frase, na
        linha, na hora.

        `midias` (self.midias_nomes) e a mesma garantia da carta, so que
        para foto/grafico avulso (ver tc.catalogo_midias()) - o roteiro
        aceita as duas coisas na mesma sintaxe (ver tc.parse_roteiro)."""
        with self.lock:
            iv = self.intervalos()
            mantido = sum(b - a for a, b in iv)
            cenas, orfas = self._cenas(iv)
            eventos, erros, notas = tc.parse_roteiro(
                self.roteiro, self.principal, mantido, self.C,
                self.cartas_nomes, self.midias_nomes)
            for ev in eventos:
                c = next((x for x in cenas
                          if x["ini"] <= ev["ini"] <= x["fim"]), None)
                if c is None:
                    c = min(cenas, key=lambda x: abs(x["ini"] - ev["ini"]),
                            default=None)
                ev["diz"] = (c["txt"][:70] if c else "")
            return {
                "principal": self.principal,
                "roteiro": self.roteiro,
                "semLegenda": self.sem_legenda,
                "pronto": self.pronto,
                "cartas": self.cartas_nomes,
                "midias": self.midias_nomes,
                "eventos": eventos,
                "erros": erros,
                "notas": notas,
                "mantido": round(mantido, 2),
                "caducou": tc.roteiro_caducou(
                    {"mantido": self.roteiro_mantido}, mantido),
                "legendas": cenas,
                "orfas": orfas,
                "rabo": self.C["cartas"]["rabo_final"],
                "entra": self.C["cartas"]["principal_entra"],
            }

    def _grava_roteiro(self, mantido):
        tc.jdump({"versao": 1, "video": self.src.name,
                  "principal": self.principal, "roteiro": self.roteiro,
                  "sem_legenda": self.sem_legenda,
                  "pronto": self.pronto,
                  # o corte de AGORA, gravado so quando ele salva de dentro
                  # do modo cartas - que e onde ele ve, linha por linha, o
                  # que se ouve em cada tempo. Ver `cartas_estado`.
                  "mantido": round(mantido, 3)},
                 self.d / "cartas-roteiro.json")

    def salva_cartas(self, b):
        """Grava o roteiro e a carta principal. Devolve o estado novo."""
        with self.lock:
            if "principal" in b:
                nome = str(b.get("principal") or "").strip()
                self.principal = nome
            if "roteiro" in b:
                self.roteiro = str(b.get("roteiro") or "")
            if "semLegenda" in b:
                self.sem_legenda = bool(b.get("semLegenda"))
            if "pronto" in b:
                self.pronto = bool(b.get("pronto"))
            self.roteiro_mantido = sum(x - a for a, x in self.intervalos())
            self._grava_roteiro(self.roteiro_mantido)
            # o roteiro fala no relogio da SAIDA, e a saida vem do corte:
            # salvar um sem o outro deixaria no disco um roteiro medido
            # contra um corte que o disco nao tem.
            self.grava()
            return self.cartas_estado()

    def salva_legenda(self, b):
        """O texto de UM cartao de legenda, escrito a mao.

        Casa pelo TEMPO DE FONTE do cartao, nunca pelo numero - um corte
        novo renumera todos os de baixo e a edicao passaria a valer para
        outra frase (mesma regra do `montagem.json` e da resposta de
        pendencia).

        Texto vazio volta ao automatico, e e assim que se desfaz: e a mesma
        convencao do `_corrige` para a palavra. Legenda vazia de verdade
        nao existe - o cartao sumiria do .srt sem nada dizer que ele sumiu.

        `orig` guarda o texto automatico do dia da edicao, para o editor
        poder dizer depois que o de baixo mudou: a edicao continua valendo
        por ser a mais explicita, mas quem olha merece saber que ela ficou
        para tras."""
        with self.lock:
            iv = self.intervalos()
            cenas, _ = self._cenas(iv)
            try:
                s, e = float(b.get("s")), float(b.get("e"))
            except (TypeError, ValueError):
                return None
            alvo = next((c for c in cenas if abs(c["s"] - s) <= 0.05
                         and abs(c["e"] - e) <= 0.05), None)
            if alvo is None:
                return None
            novo = " ".join(str(b.get("texto") or "").split())
            chave = (round(alvo["s"], 3), round(alvo["e"], 3))
            if not novo or novo == alvo["auto"]:
                self.legendas.pop(chave, None)
            else:
                prev = self.legendas.get(chave)
                self.legendas[chave] = {
                    "s": chave[0], "e": chave[1],
                    "orig": prev["orig"] if prev else alvo["auto"],
                    "novo": novo}
            tc.jdump({"versao": 1, "video": self.src.name,
                      "itens": [self.legendas[k]
                                for k in sorted(self.legendas)]},
                     self.d / "legendas.json")
            return self.cartas_estado()

    # --------------------------------------------------------- de quem e

    def posse(self):
        """A senha que uma aba tem de mostrar para MUDAR alguma coisa aqui.

        Duas metades, e as duas sao necessarias:

        `sid` diz QUE SESSAO e esta. Muda a cada `Sessao(...)` - ou seja,
        quando o editor troca de video (POST /abrir) e quando o processo
        reinicia. Sem ele, uma aba do "3 de espadas" esquecida no navegador
        continuava mandando `ocultar i0=412` para um servidor que ja estava
        no "4 de espadas", e o servidor obedecia: o indice 412 do OUTRO
        video sumia e ia para o disco, calado. MEDIDO (10/09/2026) com dois
        POSTs num servidor de teste - a aba aberta no IMG_0246 teve o gesto
        dela atendido pela sessao do IMG_0248.

        `dono` diz QUE ABA esta com ele. Muda a cada GET /doc, isto e, a
        cada pagina que nasce. Duas abas vivas no mesmo video nao corrompem
        nada (o servidor e a fonte da verdade e tem tranca), mas divergem na
        TELA: `enxuga` so manda as pecas para quem ainda nao as tem, e essa
        conta e por SESSAO, entao a segunda aba recebe `pecas: None` para
        uma mudanca que a primeira fez e ela nunca viu - fica mostrando um
        riscado que nao existe mais, e a pessoa decide olhando para ele. Uma
        dona por vez desfaz isso pela raiz.

        Nao e seguranca: e a mesma familia de defeito que este programa
        persegue desde sempre, decisao humana entrando ou sumindo sem
        ninguem perguntar."""
        return f"{self.sid}.{self.dono}"

    def assume(self):
        """Esta aba passa a ser a dona. Chamado por quem serve o /doc."""
        self.dono = uuid.uuid4().hex[:8]
        return self.posse()

    # ------------------------------------------------------------ documento

    def doc(self):
        return {
            "nome": self.nome,
            "arquivo": self.src.name,
            "dur": self.dur,
            "sr": self.sr,
            "selo": self.selo,
            "aviso": self.aviso,
            "fade": self.C["audio"]["fade"],
            # a pagina nasce dona (ver `posse`): quem serve o /doc chama o
            # `assume` antes de montar isto.
            "posse": self.posse(),
            "blocos": tc.blocos(self.stream),
            "anot": self.anot,
            # inteiro: a pagina esta nascendo e nao tem peca nenhuma para
            # reaproveitar. Marca o que foi mandado, senao a primeira acao
            # repetiria os 330 KB.
            "estado": self._doc_estado(),
        }

    def _doc_estado(self):
        est = self.estado()
        self._pecas_chave = self._corte_chave
        return est


# ------------------------------------------------------------ trocar de video

def videos_disponiveis(atual):
    """work/<video>/ que ja passou pelo pipeline o bastante para o editor
    abrir - os MESMOS dois arquivos que `Sessao.__init__` exige (linhas
    acima). Nao reimplementa o criterio: so aplica o mesmo, para o botao
    "Abrir" nunca oferecer um video que a propria Sessao recusaria."""
    out = []
    if not tc.WORK.is_dir():
        return out
    for d in sorted(tc.WORK.iterdir()):
        if not d.is_dir():
            continue
        if not (d / "stream.json").exists() or not (d / "audio.wav").exists():
            continue
        arquivo, dur = d.name, None
        try:
            meta = tc.jload(d / "meta.json")
            arquivo = Path(meta.get("src", d.name)).name
            dur = meta.get("dur")
        except Exception:
            pass
        out.append({"video": d.name, "arquivo": arquivo, "dur": dur,
                    "atual": d.name == atual})
    return out


# ------------------------------------------------------------- subprocesso

class Tarefa:
    """Roda `tarocut.py <cmd>` e guarda a saida para a pagina ler.

    Existe para fechar o ciclo sem sair do editor: ajustou, exportou, ouviu.
    O log vai para um deque porque o ffmpeg escreve uma linha de progresso
    por segundo e ninguem quer o historico inteiro."""

    def __init__(self):
        self.linhas = deque(maxlen=400)
        self.proc = None
        self.rc = None
        self.rotulo = ""

    @property
    def rodando(self):
        return self.proc is not None and self.proc.poll() is None

    def roda(self, argv, rotulo):
        if self.rodando:
            return False
        self.linhas.clear()
        self.rc = None
        self.rotulo = rotulo
        self.linhas.append("$ " + " ".join(argv[1:]))
        threading.Thread(target=self._run, args=(argv,), daemon=True).start()
        return True

    def _run(self, argv):
        try:
            self.proc = subprocess.Popen(
                argv, cwd=str(ROOT), stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
                text=True, encoding="utf-8", errors="replace", bufsize=1)
        except OSError as e:
            self.linhas.append(f"[editor] nao consegui rodar: {e}")
            self.rc = 1
            return
        for ln in self.proc.stdout:
            ln = ln.rstrip("\r\n")
            if ln:
                self.linhas.append(ln)
        self.proc.wait()
        self.rc = self.proc.returncode


# ---------------------------------------------------------------- servidor

class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "tarocut-editor"

    # o log de acesso do http.server poe uma linha por requisicao no
    # console, e o editor faz uma por gesto
    def log_message(self, fmt, *args):
        pass

    def _envia(self, corpo, tipo="application/json; charset=utf-8", codigo=200):
        if isinstance(corpo, (dict, list)):
            corpo = json.dumps(corpo, ensure_ascii=False).encode("utf-8")
        elif isinstance(corpo, str):
            corpo = corpo.encode("utf-8")
        self.send_response(codigo)
        self.send_header("Content-Type", tipo)
        self.send_header("Content-Length", str(len(corpo)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(corpo)

    def _corpo(self):
        n = int(self.headers.get("Content-Length") or 0)
        return json.loads(self.rfile.read(n) or b"{}")

    def _posse_ok(self, b, cli=False):
        """None quando o POST veio da aba que e dona do editor AGORA; a
        recusa pronta (dict) quando nao veio. Ver `Sessao.posse`.

        Recusa em vez de avisar, pela mesma razao do selo do
        `transcript.md` e do `confere_montagem`: o que vem depois de um
        casamento errado nao e um risco, e uma decisao trocada gravada no
        disco. A recusa diz QUAL das duas coisas aconteceu, porque a saida
        e diferente - a aba que perdeu o video precisa saber que video o
        editor tem agora.

        `cli` liga a unica excecao: o `/abrir` que o `editor.py` da linha
        de comando manda (`_troca_no_editor_aberto`) nao tem aba nenhuma
        atras dele e nao manda `posse`. Corpo SEM o campo, so ali, passa; o
        navegador sempre manda, e um `posse` errado e recusado do mesmo
        jeito."""
        S = self.server.sessao
        veio = b.get("posse") if isinstance(b, dict) else None
        if veio == S.posse():
            return None
        if cli and veio is None:
            return None
        if str(veio or "").split(".")[0] != S.sid:
            motivo = "video"
            txt = (f"esta aba nao e mais deste editor: ele esta no video "
                   f"{S.nome!r} agora. Nada que voce fizer aqui e salvo.")
        else:
            motivo = "aba"
            txt = ("outra aba assumiu este editor. Nada que voce fizer "
                   "aqui e salvo.")
        return {"erro": txt, "motivo": motivo, "video": S.nome}

    def do_GET(self):
        rota = urlparse(self.path).path
        S, T = self.server.sessao, self.server.tarefa
        try:
            if rota in ("/", "/index.html"):
                return self._envia(PAGINA.read_text(encoding="utf-8"),
                                   "text/html; charset=utf-8")
            if rota == "/doc":
                # a pagina que nasce assume o editor. E aqui, e nao no
                # `Sessao.__init__`, porque quem tem de ser dona e a ABA -
                # ver `Sessao.posse`.
                S.assume()
                return self._envia(S.doc())
            if rota == "/vida":
                # A batida de coracao da aba: dois campos e nada de tranca.
                # Serve para uma aba velha VIRAR LAPIDE sozinha, antes de a
                # pessoa digitar nela - descobrir que a aba estava morta
                # pela recusa do primeiro gesto ja e tarde, porque a
                # aflicao ("qual aba esta salvando o que?") existe antes do
                # gesto.
                return self._envia({"posse": S.posse(), "video": S.nome,
                                    "rodando": T.rodando})
            if rota == "/log":
                return self._envia({"linhas": list(T.linhas),
                                    "rodando": T.rodando, "rc": T.rc,
                                    "rotulo": T.rotulo})
            if rota == "/audio.wav":
                return self._envia(S.wav.read_bytes(), "audio/wav")
            if rota == "/frame":
                # GET simples (nao POST) de proposito: e o que deixa a tela
                # abrir isto com `window.open(url)` puro, sem fetch nem
                # blob no meio - o navegador que baixa a imagem, na aba
                # nova que o clique ja pediu.
                try:
                    t = float(parse_qs(urlparse(self.path).query)["t"][0])
                except (KeyError, IndexError, ValueError):
                    return self._envia({"erro": "tempo invalido"}, codigo=400)
                try:
                    return self._envia(S.gera_frame(t), "image/jpeg")
                except RuntimeError as e:
                    return self._envia({"erro": str(e)}, codigo=500)
            if rota == "/videos":
                return self._envia({"videos": videos_disponiveis(S.nome)})
            if rota == "/cartas":
                return self._envia(S.cartas_estado())
            self._envia({"erro": "nao existe"}, codigo=404)
        except (BrokenPipeError, ConnectionAbortedError):
            pass
        except Exception as e:
            self._envia({"erro": repr(e)}, codigo=500)

    def do_POST(self):
        rota = urlparse(self.path).path
        S, T = self.server.sessao, self.server.tarefa
        try:
            b = self._corpo()
            # Tudo o que MUDA alguma coisa - o corte, o roteiro, a legenda,
            # o video aberto, um subprocesso que le o disco - passa por
            # aqui primeiro. O `/em` e os GET ficam de fora de proposito:
            # ler nao troca decisao de lugar, e uma aba velha continuar
            # navegando ate a pessoa fechar nao machuca ninguem.
            if rota in ("/acao", "/cartas", "/legenda", "/rodar", "/abrir"):
                recusa = self._posse_ok(b, cli=(rota == "/abrir"))
                if recusa is not None:
                    return self._envia(recusa, codigo=409)
            if rota == "/acao":
                return self._envia(S.acao(b))
            if rota == "/em":
                # so traduz e responde; nao mexe em nada. A selecao continua
                # sendo do navegador - o servidor diz que palavra e.
                try:
                    return self._envia(S.em_previa(b.get("previa")))
                except (TypeError, ValueError):
                    return self._envia({"erro": "tempo invalido"}, codigo=400)
            if rota == "/cartas":
                return self._envia(S.salva_cartas(b))
            if rota == "/legenda":
                est = S.salva_legenda(b)
                if est is None:
                    return self._envia(
                        {"erro": "esse cartao de legenda nao existe mais - "
                                 "o corte mudou"}, codigo=409)
                return self._envia(est)
            if rota == "/rodar":
                cmd = b.get("cmd")
                if cmd not in ("audio", "render", "conferir", "previa",
                               "exportar", "exportar-previa"):
                    return self._envia({"erro": "comando desconhecido"},
                                       codigo=400)
                # O export leva bandeira; os outros nao. Quem decide se a
                # legenda sai e o proprio roteiro (o `sem_legenda` gravado
                # no `cartas-roteiro.json`), e nao o botao: o comando de
                # linha tem de fazer a mesma coisa que o botao faz.
                # o subprocesso le o corte do DISCO; a tela pode estar
                # mostrando um que ainda nao chegou la (ver `grava`).
                S.grava()
                argv = [sys.executable, str(ROOT / "tarocut.py")]
                if cmd.startswith("exportar"):
                    argv += ["exportar", str(S.video)]
                    if cmd.endswith("previa"):
                        argv.append("--previa")
                    if S.sem_legenda:
                        argv.append("--sem-legenda")
                else:
                    argv += [cmd, str(S.video)]
                return self._envia({"ok": T.roda(argv, cmd)})
            if rota == "/cancelar":
                if T.rodando:
                    subprocess.run(["taskkill", "/F", "/T", "/PID",
                                    str(T.proc.pid)], capture_output=True)
                return self._envia({"ok": True})
            if rota == "/abrir":
                # revalida contra a MESMA lista que o /videos ofereceu: o
                # navegador nunca decide sozinho o que conta como "ja
                # passou pelo pipeline", so escolhe dentro do que o
                # servidor ja aprovou.
                nome = b.get("video")
                validos = {v["video"] for v in videos_disponiveis(S.nome)}
                if not isinstance(nome, str) or nome not in validos:
                    return self._envia({"erro": "video invalido"}, codigo=400)
                if T.rodando:
                    return self._envia(
                        {"erro": "espere a tarefa em andamento terminar"},
                        codigo=409)
                try:
                    self.server.sessao = Sessao(nome)
                except SystemExit as e:
                    return self._envia({"erro": str(e)}, codigo=500)
                return self._envia({"ok": True})
            self._envia({"erro": "nao existe"}, codigo=404)
        except (BrokenPipeError, ConnectionAbortedError):
            pass
        except Exception as e:
            self._envia({"erro": repr(e)}, codigo=500)


class ServidorEditor(ThreadingHTTPServer):
    """So existe para desligar `allow_reuse_address` (o `1` que o
    HTTPServer liga por padrao). Com ele ligado, o Windows deixa DOIS
    processos ficarem "LISTENING" na mesma porta ao mesmo tempo - o
    `bind()` do segundo nao da erro nenhum, so nunca recebe requisicao. E
    como o gui.py sobe o editor escondido (sem console pra um Ctrl+C
    fechar), esses zumbis se acumulam: cada "3. Editor" clicado abria mais
    um, e o navegador sempre caia no PRIMEIRO editor aberto na sessao do
    Windows, preso no video dele para sempre - foi assim que "abrir outro
    video" ficou so mostrando "2 de espadas". Desligado, o segundo bind
    falha na hora com OSError, e `serve()` sabe o que fazer com isso."""
    allow_reuse_address = False


def _erro_do_corpo(e):
    """O que o outro editor DISSE ao recusar. O `HTTPError` traz o corpo
    junto, e ate 09/09/2026 ninguem o lia: o que chegava a tela era
    "HTTP Error 500: Internal Server Error", que nao diz de que video nem
    por que. Ler o corpo e a diferenca entre "o editor de ontem nao conhece
    este /abrir" e um misterio."""
    try:
        d = json.loads(e.read())
        return str(d.get("erro") or d)[:200]
    except Exception:
        return e.reason if isinstance(e.reason, str) else repr(e)


def _troca_no_editor_aberto(video, porta, abrir):
    """A porta ja esta ocupada - quase sempre por um editor deste programa
    aberto de uma sessao anterior, que ninguem fechou porque nao ha console
    nem botao pra isso. Em vez de tentar um segundo servidor (que so
    duplicaria o zumbi), manda o mesmo POST /abrir que o botao "Abrir" do
    proprio editor manda, no processo que ja esta escutando.

    Devolve None quando trocou, e a RAZAO em texto quando nao deu - quem
    chama derruba o dono da porta e sobe um editor novo. Nao sai mais do
    programa, e essa e a mudanca: o editor de uma VERSAO ANTERIOR e
    justamente o caso em que a troca nunca vai funcionar (o `/abrir` dele e
    outro), e ate 09/09/2026 esse era o fim da linha - o botao "3. Editor"
    imprimia "HTTP Error 500" e nao abria nada, sem dizer o que fazer.
    Derrubar nao custa trabalho nenhum: cada gesto do editor ja foi ao
    disco no instante em que aconteceu (ver `grava`)."""
    url = f"http://127.0.0.1:{porta}/"
    corpo = json.dumps({"video": video}).encode("utf-8")
    req = Request(url + "abrir", data=corpo, method="POST",
                  headers={"Content-Type": "application/json"})
    try:
        with urlopen(req, timeout=5) as r:
            resp = json.loads(r.read())
    except HTTPError as e:
        detalhe = _erro_do_corpo(e)
        if e.code == 409:
            # A UNICA recusa que nao se resolve derrubando: la dentro ha um
            # render/audio em andamento, e o `taskkill /T` levaria o ffmpeg
            # junto. Minutos de maquina sumindo sem nada perguntar e o
            # defeito de sempre desta casa, do lado do fogao.
            sys.exit(f"[editor] o editor da porta {porta} esta ocupado com "
                     f"uma tarefa ({detalhe}). Espere ela terminar: derrubar "
                     f"o editor agora mataria a tarefa no meio.")
        return f"HTTP {e.code} - {detalhe}"
    except (URLError, OSError, ValueError) as e:
        return repr(e)
    if not resp.get("ok"):
        return f"recusou abrir {video!r}: {resp.get('erro')}"
    print(f"[editor] ja tinha um editor aberto na porta {porta}; troquei "
          f"para {video} nele em vez de abrir outro.")
    if abrir:
        webbrowser.open(url)
    return None


def _dono_da_porta(porta):
    """(pid, imagem) de quem escuta 127.0.0.1:porta, ou (None, None).

    `netstat` e `tasklist` porque nao ha dependencia nova nisso - o
    programa ja chama `taskkill` para cancelar tarefa. A imagem importa:
    derrubar as cegas quem responde numa porta e derrubar o que nao e
    nosso."""
    alvo = f"127.0.0.1:{porta}"
    try:
        r = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                           capture_output=True, text=True)
    except OSError:
        return None, None
    pid = None
    for linha in r.stdout.splitlines():
        c = linha.split()
        if len(c) == 5 and c[0].upper() == "TCP" and c[1] == alvo:
            # a linha LISTENING e a do dono; as ESTABLISHED sao as conexoes
            # do navegador com ele e tem o mesmo pid - por isso qualquer uma
            # serve, com a de escuta preferida. O pid 0 e das linhas
            # TIME_WAIT de conexao ja fechada: dono nenhum.
            if c[4] == "0":
                continue
            pid = c[4]
            if c[3].upper() == "LISTENING":
                break
    if not pid:
        return None, None
    try:
        r = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH",
                            "/FO", "CSV"], capture_output=True, text=True)
        img = r.stdout.strip().split('","')[0].strip('"')
    except OSError:
        img = ""
    return pid, (img or "?")


def _derruba_porta(porta):
    """Mata o dono da porta - so se for python, que e o que este editor e.

    Recusar o resto nao e cerimonia: 8731 e uma porta como qualquer outra, e
    no dia em que outro programa estiver nela, mata-lo para abrir uma
    transcricao seria estrago feito por conta propria."""
    pid, img = _dono_da_porta(porta)
    if pid is None:
        sys.exit(f"[editor] a porta {porta} esta ocupada e nao consegui "
                 f"descobrir por quem. Rode com --porta 8732.")
    if not img.lower().startswith("python"):
        sys.exit(f"[editor] a porta {porta} e de {img} (pid {pid}), que nao "
                 f"e um editor deste programa. Nao derrubo processo dos "
                 f"outros - rode com --porta 8732.")
    subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)],
                   capture_output=True)
    print(f"[editor] derrubei o editor velho da porta {porta} "
          f"({img}, pid {pid}).")


def _abre_porta(porta, insiste=0.0):
    """Escuta na porta, ou None se ela estiver ocupada.

    `insiste` existe para depois do taskkill: o Windows costuma liberar a
    porta na hora, mas "costuma" nao e "sempre", e falhar aqui devolveria a
    pessoa exatamente ao lugar de onde ela veio."""
    fim = time.monotonic() + insiste
    while True:
        try:
            return ServidorEditor(("127.0.0.1", porta), Handler)
        except OSError:
            if time.monotonic() >= fim:
                return None
            time.sleep(0.15)


def serve(video, porta=8731, abrir=True):
    """Abre o editor, e abre mesmo: se a porta estiver ocupada, tenta a
    saida educada (trocar de video no editor que ja esta la) e, quando ela
    nao funciona, derruba quem esta na porta e sobe um editor novo.

    A ordem e essa de proposito: trocar no editor aberto e instantaneo e
    nao mexe no navegador que a pessoa ja tem na tela. Derrubar e o plano
    B, e ele so e seguro porque o corte ja esta no disco antes - `grava`
    roda a cada gesto e antes de cada subprocesso."""
    sessao = Sessao(video)
    srv = _abre_porta(porta)
    if srv is None:
        razao = _troca_no_editor_aberto(sessao.nome, porta, abrir)
        if razao is None:
            return
        print(f"[editor] a porta {porta} nao respondeu como editor deste "
              f"programa ({razao}).")
        _derruba_porta(porta)
        srv = _abre_porta(porta, insiste=5.0)
        if srv is None:
            sys.exit(f"[editor] derrubei quem estava na porta {porta} e ela "
                     f"continua ocupada. Rode com --porta 8732.")
    srv.sessao, srv.tarefa = sessao, Tarefa()
    url = f"http://127.0.0.1:{porta}/"
    iv = sessao.intervalos()
    print(f"[editor] {sessao.nome}: {len(sessao.stream)} tokens, "
          f"{tc.hhmmss(sessao.dur)} -> "
          f"{tc.hhmmss(sum(b - a for a, b in iv))} em {len(iv)} trechos")
    print(f"[editor] {url}   (Ctrl+C aqui fecha o editor)")
    if abrir:
        threading.Timer(0.4, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[editor] fechado")


if __name__ == "__main__":
    if len(sys.argv) < 2:
        sys.exit("uso: editor.py <video> [porta]")
    serve(sys.argv[1], int(sys.argv[2]) if len(sys.argv) > 2 else 8731)
