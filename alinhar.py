#!/usr/bin/env python3
"""Alinhamento forçado: encaixa a transcrição no áudio com precisão real.

Por que existe
--------------
Os timestamps de palavra do Whisper sao um subproduto do decoder, nao uma
medida: o fim da palavra sai cedo por sistema (come o "s" final) e o inicio
sai tarde. Todo o resto do tarocut depende dessas fronteiras - e boa parte
dos parametros de corte existia so para compensar o erro delas.

Aqui um modelo CTC (wav2vec2 em portugues) recebe o TEXTO ja conhecido e
descobre, quadro a quadro, onde cada letra foi pronunciada. Isso da fronteira
de palavra na casa dos 20 ms.

Efeito colateral valioso: o que sobra entre duas palavras alinhadas e mesmo
uma lacuna. O detector acustico de "voz sem texto" deixa de brigar com ruido
de alinhamento e passa a apontar cacoete de verdade.
"""
import re, time, wave

import numpy as np

MODELO = "jonatasgrosman/wav2vec2-large-xlsr-53-portuguese"
SR = 16000

# ------------------------------------------------------------ numeros

_UNI = ["zero", "um", "dois", "três", "quatro", "cinco", "seis", "sete",
        "oito", "nove", "dez", "onze", "doze", "treze", "catorze", "quinze",
        "dezesseis", "dezessete", "dezoito", "dezenove"]
_DEZ = ["", "", "vinte", "trinta", "quarenta", "cinquenta", "sessenta",
        "setenta", "oitenta", "noventa"]
_CEM = ["", "cento", "duzentos", "trezentos", "quatrocentos", "quinhentos",
        "seiscentos", "setecentos", "oitocentos", "novecentos"]


def por_extenso(n):
    """O vocabulario do modelo nao tem digitos. Sem isto, '2023' nao alinha
    - e era justamente onde o corte comia o ano da frase."""
    n = int(n)
    if n < 20:
        return _UNI[n]
    if n < 100:
        d, u = divmod(n, 10)
        return _DEZ[d] + (" e " + _UNI[u] if u else "")
    if n == 100:
        return "cem"
    if n < 1000:
        c, r = divmod(n, 100)
        return _CEM[c] + (" e " + por_extenso(r) if r else "")
    if n < 1000000:
        m, r = divmod(n, 1000)
        mil = "mil" if m == 1 else por_extenso(m) + " mil"
        if not r:
            return mil
        return mil + (" e " if r < 100 or r % 100 == 0 else " ") + por_extenso(r)
    return " ".join(por_extenso(c) for c in str(n))


def expande(txt):
    return re.sub(r"\d+", lambda m: " " + por_extenso(m.group()) + " ", txt)


def normaliza(txt, vocab):
    """Deixa so o que o modelo sabe ler."""
    t = expande(txt).lower().replace("ª", "a").replace("º", "o")
    t = t.replace("-", " ").replace("–", " ")
    t = "".join(c for c in t if c in vocab or c.isspace())
    return " ".join(t.split())


# ------------------------------------------------------------ alinhamento

class Alinhador:
    def __init__(self, quiet=False):
        import torch
        from transformers import Wav2Vec2ForCTC, Wav2Vec2Processor
        self.torch = torch
        if not quiet:
            print(f"[align] carregando {MODELO} ...", flush=True)
        self.proc = Wav2Vec2Processor.from_pretrained(MODELO)
        self.mdl = Wav2Vec2ForCTC.from_pretrained(MODELO).eval()
        self.vocab = self.proc.tokenizer.get_vocab()
        self.blank = self.proc.tokenizer.pad_token_id
        self.chars = {c for c in self.vocab if len(c) == 1 and c.isalpha()}
        self.chars.add("'")

    def _emissao(self, audio):
        t = self.torch
        with t.inference_mode():
            x = t.from_numpy(audio).float().unsqueeze(0)
            x = (x - x.mean()) / (x.std() + 1e-7)
            logits = self.mdl(x).logits
            return t.log_softmax(logits, dim=-1)

    def alinha(self, audio, palavras):
        """audio: float32 mono 16k. palavras: lista de str.
        Devolve [(ini, fim, score)] em segundos relativos ao trecho, ou None
        quando o alinhamento nao e confiavel."""
        import torchaudio.functional as AF
        t = self.torch

        alvo, dono = [], []          # ids de caractere e a qual palavra pertencem
        for i, p in enumerate(palavras):
            n = normaliza(p, self.chars)
            if not n:
                continue
            for w in n.split():
                for ch in w:
                    idc = self.vocab.get(ch)
                    if idc is None:
                        continue
                    alvo.append(idc)
                    dono.append(i)
        if not alvo:
            return None

        lp = self._emissao(audio)
        if lp.shape[1] < len(alvo):   # menos quadros que letras: impossivel
            return None
        alvo_t = t.tensor([alvo], dtype=t.int32)
        try:
            rot, sc = AF.forced_align(lp, alvo_t, blank=self.blank)
        except Exception:
            return None
        spans = AF.merge_tokens(rot[0], sc[0])
        if len(spans) != len(alvo):
            return None

        razao = len(audio) / lp.shape[1] / SR
        saida = {}
        for s, quem in zip(spans, dono):
            ini, fim = s.start * razao, s.end * razao
            if quem in saida:
                a, b, n_, acc = saida[quem]
                saida[quem] = (min(a, ini), max(b, fim), n_ + 1,
                               acc + float(s.score))
            else:
                saida[quem] = (ini, fim, 1, float(s.score))
        return {k: (v[0], v[1], v[3] / v[2]) for k, v in saida.items()}


# ------------------------------------------------------------ por video

def carrega_wav(p):
    w = wave.open(str(p), "rb")
    pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
    w.close()
    return pcm.astype(np.float32) / 32768.0


def blocos(words, max_s=22.0):
    """Fatia em pedacos curtos: wav2vec2 grande nao cabe na memoria com 10
    minutos de audio de uma vez. Corta nas maiores pausas para nao partir
    palavra."""
    out, atual = [], []
    for w in words:
        if atual and w["e"] - atual[0]["s"] > max_s:
            out.append(atual)
            atual = []
        atual.append(w)
    if atual:
        out.append(atual)
    return out


def expande_por_energia(words, db, hop, limiar, teto=0.12):
    """Devolve a cada palavra a duracao que o CTC nao dá.

    Modelo CTC emite PICO, nao duracao: ele marca o quadro em que a letra
    aconteceu e cala o resto. A posicao fica exata, mas a palavra encolhe -
    medido aqui, a cobertura caiu de 136,8s para 106,9s, e 21s de fala real
    viraram "lacuna com voz", que o resto do programa leria como cacoete.

    Aqui cada palavra reconquista o que for contiguo e sonoro em volta do
    seu pico, ATE UM TETO. O teto e o que separa cauda de fonema (curta) de
    cacoete (longo): sem ele, a palavra engoliria o "ééé" seguinte e o
    detector de hesitacao perderia justamente o que deve achar."""
    n = len(db)

    def sonoro(t):
        i = int(t / hop)
        return 0 <= i < n and db[i] > limiar

    for i, w in enumerate(words):
        antes = words[i - 1]["e"] if i else 0.0
        depois = words[i + 1]["s"] if i + 1 < len(words) else w["e"] + teto
        lim_s = max(antes, w["s"] - teto)
        t = w["s"]
        while t - hop >= lim_s and sonoro(t - hop):
            t -= hop
        w["s"] = round(t, 3)
        lim_e = min(depois, w["e"] + teto)
        t = w["e"]
        while t + hop <= lim_e and sonoro(t):
            t += hop
        w["e"] = round(t, 3)
    return words


def descarta_ruins(words, deriva_max, log=print):
    """Alinhamento tem seu proprio jeito de errar, sobretudo na borda de
    bloco. Onde o score for pessimo, o valor do Whisper e menos ruim.

    O SCORE NAO BASTA. Medido nos tres videos: as palavras pior colocadas -
    'que' jogada 5,8s para a frente, 'de' 11,4s - saem com score -0,00, o
    melhor possivel. O CTC nao errou por duvida; ele achou a MESMA palavra
    curta ("que", "essa", "de", "e") noutro ponto do bloco e encaixou ali
    com toda a confianca. Contra esse erro so serve uma segunda opiniao, e a
    unica disponivel e o proprio Whisper: os tempos dele sao grosseiros
    (mediana de 0,09s de diferenca para o CTC) mas nunca erram por segundos
    e nunca saem de ordem - medido, ZERO inversoes nos tres videos.

    Dai as duas travas novas: quem discorda do Whisper por mais que
    `deriva_max` (sem valor padrao de proposito: o numero mora em
    [alinhamento].deriva_max do config.toml e em lugar nenhum mais -
    default aqui seria um segundo lugar decidindo a mesma coisa, que e
    como o piso de ruido chegou a viver com 12.0 no braco neste
    arquivo), ou quem acaba comecando antes da palavra anterior, perde
    o alinhamento e volta para o tempo do Whisper. Custa 1-3% das palavras
    em precisao de fronteira; evita o erro que custou uma frase.

    Foi o que faltou no IMG_0247: "que muitas" alinhado em 603,3 quando ela
    fala em 600,1 fez uma anotacao de corte terminar DENTRO da frase, e o
    audio final entrou em "vezes tem essa proposta"."""

    def volta(w, motivo):
        """Devolve a palavra ao tempo do Whisper. Uma vez so por palavra."""
        if "e_whisper" not in w or w.get("align_rejeitado"):
            return 0
        w["s"], w["e"] = w["s_whisper"], w["e_whisper"]
        w["align_rejeitado"] = motivo
        return 1

    n_sc = n_dv = n_or = 0
    for w in words:
        if "e_whisper" not in w:
            continue
        ruim = w.get("align", 0) < -5.0
        curta = (w["e"] - w["s"]) < 0.03 and len(w["w"].strip(".,!?")) > 2
        if ruim or curta:
            n_sc += volta(w, "score")
        elif abs(w["s"] - w["s_whisper"]) > deriva_max:
            n_dv += volta(w, "deriva")

    # Ordem. Como o Whisper e monotonico, devolver OS DOIS lados de uma
    # inversao sempre a desfaz - nao da para saber qual dos dois se mudou de
    # lugar. Uma passada costuma bastar; as outras sao cinto e suspensorio.
    for _ in range(3):
        mudou = 0
        for a, b in zip(words, words[1:]):
            if b["s"] < a["s"]:
                mudou += volta(a, "ordem") + volta(b, "ordem")
        n_or += mudou
        if not mudou:
            break

    n = n_sc + n_dv + n_or
    if n:
        log(f"[align] {n} alinhamentos descartados (mantido o Whisper): "
            f"{n_sc} por score, {n_dv} por discordar do Whisper em mais de "
            f"{deriva_max}s, {n_or} por sair de ordem")
    resta = sum(1 for a, b in zip(words, words[1:]) if b["s"] < a["s"])
    if resta:
        log(f"[align] ATENCAO: {resta} palavras continuam fora de ordem. "
            f"Os tempos desse trecho nao sao confiaveis - conferir no "
            f"revisao.txt antes de anotar corte ali.")
    return n


def alinha_video(words, wav_path, quiet=False, log=print):
    audio = carrega_wav(wav_path)
    dur = len(audio) / SR
    al = Alinhador(quiet=quiet)
    grupos = blocos(words)
    t0 = time.time()
    ok = falhou = 0
    desloc = []

    for gi, g in enumerate(grupos, 1):
        s0 = max(0.0, g[0]["s"] - 0.35)
        e0 = min(dur, g[-1]["e"] + 0.35)
        trecho = audio[int(s0 * SR):int(e0 * SR)]
        if len(trecho) < SR * 0.1:
            falhou += len(g)
            continue
        r = al.alinha(trecho, [w["w"] for w in g])
        if r is None:
            falhou += len(g)
            continue
        for i, w in enumerate(g):
            if i not in r:
                continue
            ini, fim, sc = r[i]
            ni, nf = s0 + ini, s0 + fim
            if nf <= ni:
                continue
            desloc.append(abs(nf - w["e"]))
            # setdefault, nao atribuicao: num `align --force` o w["s"] atual
            # JA E o alinhado da rodada anterior, e sobrescrever aqui apagaria
            # a unica referencia independente que existe. Sem isso a segunda
            # rodada mede a deriva contra ela mesma, da zero, e a trave de
            # `descarta_ruins` deixa de existir sem avisar ninguem.
            w.setdefault("s_whisper", w["s"])
            w.setdefault("e_whisper", w["e"])
            w["s"], w["e"] = round(ni, 3), round(nf, 3)
            w["align"] = round(sc, 3)
            w.pop("align_rejeitado", None)   # veredito da rodada anterior
            ok += 1
        if gi % 5 == 0 or gi == len(grupos):
            log(f"  ... bloco {gi}/{len(grupos)}  ({ok} palavras)")

    med = float(np.median(desloc)) if desloc else 0.0
    log(f"[align] {ok} alinhadas, {falhou} sem alinhamento, "
        f"{time.time()-t0:.0f}s ({dur/max(1,time.time()-t0):.2f}x tempo real)")
    log(f"[align] deslocamento mediano do fim da palavra: {med*1000:.0f} ms")

    import tarocut
    n_ruins = descarta_ruins(
        words, tarocut.cfg()["alinhamento"]["deriva_max"], log)

    # cobertura antes/depois: se as palavras encolherem, o resto do programa
    # confunde fala com cacoete. Este numero e o alarme dessa falha.
    # O envelope e o limiar vem do tarocut: e a MESMA medida que o analyze
    # usa depois. Este arquivo ja calculou o piso por conta propria, com 12.0
    # cravado no codigo, e o alinhamento passou a discordar da analise.
    db, hop, _ = tarocut.envelope(wav_path)
    limiar = tarocut.limiar_voz(db, tarocut.cfg()["silencio"]["limiar_fala_db"])
    antes = sum(w["e"] - w["s"] for w in words)
    expande_por_energia(words, db, hop, limiar)
    depois = sum(w["e"] - w["s"] for w in words)
    orig = sum(w.get("e_whisper", w["e"]) - w.get("s_whisper", w["s"])
               for w in words)
    log(f"[align] cobertura por palavra: Whisper {orig:.0f}s -> "
        f"pico CTC {antes:.0f}s -> com energia {depois:.0f}s")
    return words, {"ok": ok, "falhou": falhou, "desloc_mediano": med,
                   "descartados": n_ruins, "cobertura": round(depois, 1)}
