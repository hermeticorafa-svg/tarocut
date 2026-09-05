#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""editor.py - edita o corte pela transcricao, no navegador.

Nao e um editor de video. E a transcricao ipsis litteris com o que sai do
video riscado por cima dela, e quatro botoes que movem a borda do corte. Quem
usa isto odeia linha do tempo; tudo aqui e palavra, numero e tecla.

Por que navegador e nao Tk: o que faz este editor valer a pena e ouvir o
corte enquanto se corta, sem renderizar nada. Isso pede um motor de audio que
toque uma lista de trechos com emenda precisa e sem lacuna - a Web Audio API
faz isso de graca, agendando cada trecho num relogio de amostra. Em Tk seria
preciso escrever esse motor do zero.

O PONTO DELICADO deste arquivo e um so, e esta escrito no CLAUDE.md: **o
navegador nao calcula corte**. A cada gesto ele manda "menos 100 ms na
esquerda desta selecao" e recebe de volta os trechos ja calculados por
`tarocut.intervalos()` - a mesma funcao do `render`, com o mesmo snap, a
mesma fusao de ilhas, o mesmo minimo. O navegador so desenha e toca a lista
que recebeu. Ate a conta de "que trecho este botao vai devolver" e daqui: a
tela nunca aritmetiza tempo, porque uma reimplementacao em JS seria mais
rapida e mentiria em algum canto, mais cedo ou mais tarde.

E por isso o riscado nao vem do `cut` do token, e sim dos trechos mantidos:
o que aparece riscado e o que de fato nao vai sair no .mov, depois de toda a
geometria. Foi essa a exigencia - ver na transcricao tudo o que foi ocultado,
inclusive o que uma anotacao de julgamento tirou.

O que sai daqui e `work/<video>/montagem.json`, lido pelo `_base` do tarocut.
Depois de salvar, `audio` e `render` produzem exatamente o corte ouvido.
"""

import bisect
import json
import subprocess
import sys
import threading
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

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

        self.pilha = []
        # RLock, e nao Lock: `acao` segura a tranca e chama `estado`,
        # que chama `intervalos`. E ela e necessaria porque o
        # `intervalos` ESCREVE no `self.stream` (o `_aplica` empilha a
        # camada de palavra nele) - com o servidor atendendo em
        # threads, um /doc no meio de um /acao leria o fluxo com o
        # estado do outro pela metade.
        self.lock = threading.RLock()

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
        self.aviso = (f"o montagem.json era de outra analise: os ajustes de "
                      f"milissegundo foram mantidos, {len(mont['tokens'])} "
                      f"decisoes de palavra foram descartadas. Copia em "
                      f"{copia.name}")
        print(f"[editor] {self.aviso}")
        for r in ruins[:6]:
            print(f"    {r}")

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
        """A unica fonte de verdade do corte, para a tela e para o render."""
        with self.lock:
            self._aplica()
            return tc.intervalos(self.stream, self.C, self.dur, self.db,
                                 self.hop, self.anot, self.montagem())

    # -------------------------------------------------------- onde a lamina

    @staticmethod
    def _trecho_em(iv, t):
        """O trecho MANTIDO que contem `t`, ou None. Borda conta como
        dentro: e nela que os botoes trabalham."""
        for a, b in iv:
            if a - 1e-9 <= t <= b + 1e-9:
                return [a, b]
        return None

    def _lacuna_em(self, iv, t):
        for h1, h2 in tc.lacunas(iv, self.dur):
            if h1 - 1e-9 <= t <= h2 + 1e-9:
                return [h1, h2]
        return None

    def _devolve(self, lac, u, direita):
        """Que trecho o botao de mais devolve dentro desta lacuna.

        PRIORIDADE PARA O QUE A PESSOA TIROU. O mais e o desfazer do menos,
        e desfazer que devolve outra coisa nao e desfazer: sem esta regra,
        cinco menos seguidos e cinco mais seguidos deixavam a fala cortada
        de fora e traziam meio segundo de silencio no lugar dela - medido,
        era o unico jeito de os dois botoes nao voltarem ao mesmo corte. So
        quando nao ha corte seu dentro da lacuna e que o mais desfaz o corte
        AUTOMATICO, e ai a partir da borda colada na selecao.

        A fatia devolvida sempre encosta em material que ja se ouve. Corte
        seu que esteja boiando no meio da lacuna e pulado de proposito:
        devolver so ele deixaria uma ilha de 100 ms cercada de silencio, que
        e o defeito que o `_garante_ilhas` evita do outro lado.

        Devolve (trecho, e_seu) - quem chama precisa saber se e desfazer ou
        se e material que a analise tirou."""
        h1, h2 = lac
        meus = [[max(c[0], h1), min(c[1], h2)] for c in self.cortes
                if c[1] > h1 + 1e-9 and c[0] < h2 - 1e-9]
        meus = [c for c in meus if c[1] - c[0] > MINIMO]
        meus.sort(reverse=not direita)          # o mais perto da selecao antes
        for c0, c1 in meus:
            encosta_perto = (c0 <= h1 + 1e-6) if direita else (c1 >= h2 - 1e-6)
            encosta_longe = (c1 >= h2 - 1e-6) if direita else (c0 <= h1 + 1e-6)
            if encosta_perto:
                return ([c0, min(c1, c0 + u)] if direita
                        else [max(c0, c1 - u), c1]), True
            if encosta_longe:
                return ([max(c0, c1 - u), c1] if direita
                        else [c0, min(c1, c0 + u)]), True
        return ([h1, min(h2, h1 + u)] if direita
                else [max(h1, h2 - u), h2]), False

    def vizinhanca(self, i0, i1):
        """Ate onde vai o "ao redor" do botao de mais.

        Restaurar e devolver o que foi ocultado PERTO da palavra, e sem um
        limite "perto" vira "a proxima lacuna do corte" - que pode estar
        quatro segundos adiante. Medido no IMG_0247: com a palavra `cartas`
        de 3:11,6 selecionada, o mais da direita acendia e devolvia 100 ms
        de uma pausa em 3:16,1, no meio de outra frase. Nao e o que o botao
        promete.

        O limite e o TOKEN vizinho, e nao um numero em segundos, porque e
        exatamente o que a pessoa ve na tela: a marca de vao ao lado da
        palavra. Corte que a propria pessoa fez escapa deste limite (ver
        `gestos`) - o mais tem de desfazer o menos onde quer que ele tenha
        caido, senao deixa de ser desfazer."""
        n = len(self.stream)
        esq = self.stream[max(0, i0 - 1)]["s"]
        dir_ = self.stream[min(n - 1, i1 if i1 > i0 else i0 + 1)]["e"]
        return esq, dir_

    def gestos(self, iv, ini, fim, u, viz=None):
        """O que cada um dos quatro botoes faria, agora, nesta selecao.

        Aqui mora o modelo inteiro da interface, e ele e mais simples do que
        parece: **os botoes movem a borda da lacuna colada na selecao.** Do
        lado esquerdo, o menos empurra a borda de fora para longe (esconde
        mais) e o mais puxa a borda de dentro para perto (devolve). Do lado
        direito, espelhado.

        Duas consequencias, e sao elas que fazem a coisa parecer obvia na
        mao:

        - mais desfaz menos EXATO. O menos abre a lacuna [s-u, s]; o mais
          devolve [s-u, s]. E o mesmo trecho, e por isso o botao de mais
          acende sozinho depois de um menos.
        - apertar o mesmo botao de novo continua de onde parou, porque a
          borda andou. Nao ha estado escondido: a lacuna esta no corte, e o
          corte e recalculado a cada gesto.

        O menos ancora no que ainda se OUVE (primeiro instante mantido do
        lado, pulando a lacuna que ja existe) e nunca atravessa uma lacuna
        num aperto so - come no maximo ate o fim do trecho em que entrou.
        Assim uma tecla segurada nao pula por cima de uma frase inteira sem
        que ninguem veja."""
        out = {"esq": {"menos": None, "mais": None},
               "dir": {"menos": None, "mais": None}}
        viz_esq, viz_dir = viz if viz else (0.0, self.dur)

        # --- esquerda: o que se ouve ANTES da selecao
        for a, b in reversed(iv):
            if a < ini - 1e-9:
                q = min(b, ini)
                if q - a > MINIMO:
                    out["esq"]["menos"] = [max(a, q - u), q]
                break
        borda = self._trecho_em(iv, ini)
        e_esq = borda[0] if borda else ini
        lac = self._lacuna_em(iv, e_esq)
        # A lacuna e aparada na selecao: quando a propria palavra esta
        # oculta, a lacuna atravessa os dois lados dela, e o botao da
        # esquerda so tem o que dizer sobre o que fica a esquerda.
        if lac:
            lac = [lac[0], min(lac[1], e_esq)]
            if lac[1] - lac[0] > MINIMO:
                span, meu = self._devolve(lac, u, False)
                if meu or lac[1] >= viz_esq:
                    out["esq"]["mais"] = span

        # --- direita: o que se ouve DEPOIS da selecao
        for a, b in iv:
            if b > fim + 1e-9:
                p = max(a, fim)
                if b - p > MINIMO:
                    out["dir"]["menos"] = [p, min(b, p + u)]
                break
        borda = self._trecho_em(iv, fim)
        e_dir = borda[1] if borda else fim
        lac = self._lacuna_em(iv, e_dir)
        if lac:
            lac = [max(lac[0], e_dir), lac[1]]
            if lac[1] - lac[0] > MINIMO:
                span, meu = self._devolve(lac, u, True)
                if meu or lac[0] <= viz_dir:
                    out["dir"]["mais"] = span

        for lado in out.values():
            for k, v in list(lado.items()):
                if v and v[1] - v[0] <= MINIMO:
                    lado[k] = None
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
        no seu - e essa disjuncao que faz o mais desfazer o menos."""
        self.restauros = tc.subtrai_spans(self.restauros, [span])
        self.cortes = tc.uniao_spans(self.cortes, [span])

    def _restaura(self, span):
        self.cortes = tc.subtrai_spans(self.cortes, [span])
        self.restauros = tc.uniao_spans(self.restauros, [span])

    def _palavras(self, i0, i1, estado):
        for i in range(max(0, i0), min(len(self.stream), i1)):
            cut, _ = self.base[i]
            if (estado == "cortar") == cut:
                self.tokens.pop(i, None)   # voltou a proposta: nao e decisao
            else:
                self.tokens[i] = estado

    def selecao(self, sel):
        """A selecao da tela -> (ini, fim, i0, i1) em segundos e em tokens.

        A pagina manda palavra e letra, nunca tempo: `{i0, i1}` para uma
        faixa de palavras, `{i, k, n}` para o cursor entre letras. A
        interpolacao da letra dentro da palavra mora aqui pelo mesmo motivo
        que todo o resto - o dia em que a regra mudar, ha de haver um lugar
        so onde mudar. E ela e interpolacao mesmo: o alinhador entrega a
        fronteira da PALAVRA, medida no audio; dentro dela nao existe
        medida, so repartir o tempo pelas letras."""
        sel = sel or {}
        n = len(self.stream)
        if not n:
            return 0.0, 0.0, 0, 0
        if "i0" in sel:
            i0 = max(0, min(n - 1, int(sel["i0"])))
            i1 = max(i0 + 1, min(n, int(sel["i1"])))
            return self.stream[i0]["s"], self.stream[i1 - 1]["e"], i0, i1
        if "i" in sel:
            i = max(0, min(n - 1, int(sel["i"])))
            t = self.stream[i]
            letras = max(1, int(sel.get("n") or 1))
            k = max(0, min(letras, int(sel.get("k") or 0)))
            p = t["s"] + (t["e"] - t["s"]) * k / letras
            return p, p, i, i
        return 0.0, 0.0, 0, 0

    def acao(self, b):
        """Um gesto -> o estado novo. Toda a tela sai daqui."""
        with self.lock:
            tipo = b.get("acao") or "nada"
            ini, fim, i0, i1 = self.selecao(b.get("sel"))
            u = max(MINIMO, float(b.get("unidade") or 0.1))
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
            elif tipo in ("menos", "mais"):
                g = self.gestos(self.intervalos(), ini, fim, u,
                                self.vizinhanca(i0, i1))
                span = g.get(b.get("lado") or "dir", {}).get(tipo)
                if not span:
                    mudou = False
                else:
                    self._empilha()
                    (self._corta if tipo == "menos" else self._restaura)(span)
            else:
                mudou = False

            if mudou:
                tc.jdump(self.montagem(), self.d / "montagem.json")
                tc.jdump(self.correcoes_doc(), self.d / "correcoes.json")
            return self.estado(ini, fim, u, mudou,
                               self.vizinhanca(i0, i1))

    # ------------------------------------------------------------- a tela

    def estado(self, ini=0.0, fim=0.0, u=0.1, mudou=False, viz=None):
        with self.lock:
            iv = self.intervalos()
            return self._retrato(iv, ini, fim, u, mudou, viz)

    def _retrato(self, iv, ini, fim, u, mudou, viz):
        mantido = sum(b - a for a, b in iv)
        _, para_previa = tc._mapa_previa(iv)
        return {
            "iv": iv,
            "pecas": self.pecas(iv),
            "gestos": self.gestos(iv, ini, fim, u, viz),
            "mantido": mantido,
            # UM numero, nao novecentos: o resto da tela continua em tempo
            # de fonte. Este e o unico relogio que ele tem na mao quando
            # esta anotando de ouvido, e ate agora a folha nao mostrava
            # tempo nenhum, de nenhuma das duas especies.
            "previaSel": round(para_previa(ini), 2),
            "previa": self.previa_no_disco(mantido),
            # "trechos", e nao "emendas": o `conferir` ja chama len(iv)
            # de emendas, e dois numeros diferentes com o mesmo nome na
            # mesma tela e pedir para alguem comparar errado.
            "trechos": len(iv),
            "mao": len(self.tokens) + len(self.cortes) + len(self.restauros),
            "podeDesfazer": bool(self.pilha),
            "avisos": self.avisos(iv),
            "achados": self.achados(iv),
            "mudou": mudou,
        }

    def pecas(self, iv):
        """Um item por token, ja pronto para desenhar.

        As fracoes em `vivo` sao a fatia MANTIDA do token, de 0 a 1, e e por
        elas que a pagina risca meia palavra quando a lamina cai no meio de
        uma. Fracao, e nao tempo: dentro de uma palavra nao existe medida
        melhor que interpolar pelas letras - o alinhador entrega a fronteira
        da palavra, nao a do fonema -, e a conta de "que letra e esta" e
        assunto da tela, nao do corte.

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

        A segunda e nova e vale por si: **uma palavra que voce ocultou pode
        continuar tocando inteira.** `_respeita_palavras` recua a emenda
        `folga_palavra` (0,06 s) de cada palavra mantida vizinha, e quando a
        palavra oculta e mais curta que 0,12 s as duas folgas cobrem ela por
        completo - o corte e anulado. MEDIDO: o `de` de 100,19-100,33 do
        IMG_0248 sobra 140 ms, ou seja, sobra todo.

        Nao se conserta baixando a folga: 0,06 e o erro de fronteira MEDIDO
        do alinhador, e abaixar devolve silaba comida. O caminho e a pessoa
        terminar o servico com os botoes de menos, que passam por cima de
        toda a geometria - mas ela precisa SABER. Sem esta linha, a palavra
        continuava no video e nada em lugar nenhum dizia isso."""
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
            if not fora:
                out.append({
                    "ini": t["s"], "fim": t["e"], "vivo": round(vivo, 2),
                    "previa": round(para_previa(t["s"]), 2),
                    "mao": False, "palavra": t["t"],
                    # QUAL dos dois pedidos foi desobedecido. A frase abaixo
                    # ja dizia, mas o rotulo da lista "o que ouvir" era
                    # "NAO SAIU" nos dois casos, e no restauro isso diz o
                    # contrario do que aconteceu: a palavra nao VOLTOU. Quem
                    # sabe qual e qual e este lado; a tela escolhe a
                    # palavra, nao recalcula nada.
                    "estado": estado,
                    "motivo": (f"voce ocultou {t['t']!r} e "
                               f"{vivo*1000:.0f} ms dela continuam tocando; "
                               f"termine com o botao de menos"
                               if estado == "cortar" else
                               f"voce restaurou {t['t']!r} e ela continua "
                               f"fora; traga de volta com o botao de mais")})
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
            "blocos": tc.blocos(self.stream),
            "anot": self.anot,
            "estado": self.estado(),
        }


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

    def do_GET(self):
        rota = urlparse(self.path).path
        S, T = self.server.sessao, self.server.tarefa
        try:
            if rota in ("/", "/index.html"):
                return self._envia(PAGINA.read_text(encoding="utf-8"),
                                   "text/html; charset=utf-8")
            if rota == "/doc":
                return self._envia(S.doc())
            if rota == "/log":
                return self._envia({"linhas": list(T.linhas),
                                    "rodando": T.rodando, "rc": T.rc,
                                    "rotulo": T.rotulo})
            if rota == "/audio.wav":
                return self._envia(S.wav.read_bytes(), "audio/wav")
            if rota == "/videos":
                return self._envia({"videos": videos_disponiveis(S.nome)})
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
            if rota == "/acao":
                return self._envia(S.acao(b))
            if rota == "/em":
                # so traduz e responde; nao mexe em nada. A selecao continua
                # sendo do navegador - o servidor diz que palavra e.
                try:
                    return self._envia(S.em_previa(b.get("previa")))
                except (TypeError, ValueError):
                    return self._envia({"erro": "tempo invalido"}, codigo=400)
            if rota == "/rodar":
                cmd = b.get("cmd")
                if cmd not in ("audio", "render", "conferir", "previa"):
                    return self._envia({"erro": "comando desconhecido"},
                                       codigo=400)
                argv = [sys.executable, str(ROOT / "tarocut.py"), cmd,
                        str(S.video)]
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


def serve(video, porta=8731, abrir=True):
    sessao = Sessao(video)
    srv = ThreadingHTTPServer(("127.0.0.1", porta), Handler)
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
