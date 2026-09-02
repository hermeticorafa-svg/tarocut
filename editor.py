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
         self.anot, mont) = tc._base(str(video), "editor", aplicar_mont=False)
        self.meta = tc.jload(self.d / "meta.json")
        self.src = Path(self.meta["src"])
        self.video = self.src if self.src.exists() else Path(video)
        self.nome = Path(video).stem
        self.wav = self.d / "audio.wav"
        if not self.wav.exists():
            sys.exit(f"[editor] falta {self.wav}. Rode 'tarocut.py run' antes.")
        with __import__("wave").open(str(self.wav), "rb") as w:
            self.sr = w.getframerate()
        # o selo do stream.json COMO ESTA NO DISCO. O `_base` ja aplicou o
        # transcript.md por cima do fluxo, entao calcular o selo daqui
        # daria outro numero - e o selo so serve para dizer de que analise
        # veio este montagem.json.
        self.selo = tc.selo_stream(tc.jload(self.d / "stream.json")["stream"])

        # Estado depois do transcript.md e das anotacoes, e ANTES da
        # montagem: e contra esta linha de base que o editor mede o que a
        # pessoa mudou. Sem ela, salvar gravaria como "decisao sua" tudo o
        # que a analise ja tinha proposto sozinha, e reanalisar o video
        # deixaria de valer para alguma coisa.
        self.base = [(bool(t["cut"]), bool(t.get("prot")))
                     for t in self.stream]

        self.tokens, self.cortes, self.restauros = {}, [], []
        self.aviso = ""
        if mont:
            self._carrega(mont)
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

    # ------------------------------------------------------------- calculo

    def _aplica(self):
        """Empilha a camada de palavra sobre a linha de base."""
        for i, t in enumerate(self.stream):
            cut, prot = self.base[i]
            est = self.tokens.get(i)
            if est is None:
                t["cut"], t["prot"] = cut, prot
            else:
                # `prot` protege enumeracao do corte AUTOMATICO; nao
                # autoriza sobreviver a um clique (ver `aplica_montagem`).
                t["cut"], t["prot"] = (est == "cortar"), False

    def montagem(self):
        toks = []
        for i in sorted(self.tokens):
            t = self.stream[i]
            toks.append({"i": i, "estado": self.tokens[i], "t": t["t"],
                         "s": round(t["s"], 3), "e": round(t["e"], 3)})
        return {"versao": 1, "video": self.src.name, "selo": self.selo,
                "tokens": toks, "cortes": self.cortes,
                "restauros": self.restauros}

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
                           [list(r) for r in self.restauros]))
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
                    self.tokens, self.cortes, self.restauros = self.pilha.pop()
                else:
                    mudou = False
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
            return self.estado(ini, fim, u, mudou,
                               self.vizinhanca(i0, i1))

    # ------------------------------------------------------------- a tela

    def estado(self, ini=0.0, fim=0.0, u=0.1, mudou=False, viz=None):
        with self.lock:
            iv = self.intervalos()
            return self._retrato(iv, ini, fim, u, mudou, viz)

    def _retrato(self, iv, ini, fim, u, mudou, viz):
        return {
            "iv": iv,
            "pecas": self.pecas(iv),
            "gestos": self.gestos(iv, ini, fim, u, viz),
            "mantido": sum(b - a for a, b in iv),
            # "trechos", e nao "emendas": o `conferir` ja chama len(iv)
            # de emendas, e dois numeros diferentes com o mesmo nome na
            # mesma tela e pedir para alguem comparar errado.
            "trechos": len(iv),
            "mao": len(self.tokens) + len(self.cortes) + len(self.restauros),
            "podeDesfazer": bool(self.pilha),
            "avisos": self.avisos(iv),
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
            texto = (t["k"] == "palavra"
                     or (t["k"] == "hesitacao" and t["t"] != "hesitação"))
            pec.append({"i": i, "txt": t["t"] if texto else "",
                        "vao": not texto, "s": round(s, 3), "e": round(e, 3),
                        "vivo": vivo, "why": t.get("why", ""),
                        "ocultoAntes": round(max(0.0, gap - visto), 3)})
            ant = max(ant, e)
        sobra = max(0.0, self.dur - ant)
        visto = sum(max(0.0, min(b, self.dur) - max(a, ant)) for a, b in iv)
        return {"tokens": pec, "fim": round(max(0.0, sobra - visto), 3),
                "fimS": round(ant, 3), "fimE": round(self.dur, 3)}

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
        mao = tc.spans_da_mao(self.stream, self.montagem(),
                              self.C["corte"]["folga_palavra"])
        for x, y, vivo, motivo, da_mao in tc.anotacoes_desobedecidas(
                self.anot, iv, mao=mao):
            out.append({"ini": x, "fim": y, "vivo": round(vivo, 2),
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
                    "mao": False, "palavra": t["t"],
                    "motivo": (f"voce ocultou {t['t']!r} e "
                               f"{vivo*1000:.0f} ms dela continuam tocando; "
                               f"termine com o botao de menos"
                               if estado == "cortar" else
                               f"voce restaurou {t['t']!r} e ela continua "
                               f"fora; traga de volta com o botao de mais")})
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
