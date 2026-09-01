#!/usr/bin/env python3
"""tarocut - pre-editor de video dirigido por transcricao.

Pipeline:
  ingest      video -> meta.json + audio.wav (16k mono)
  transcribe  audio -> words.json  (Whisper: o TEXTO)
  align       words.json -> fronteiras de palavra precisas (wav2vec2 CTC).
              O Whisper acerta o texto e erra os tempos; sem esta etapa o
              corte come o "s" final e parte palavra no meio.
  analyze     words + energia -> stream.json  (fluxo fundido: o que o
              Whisper transcreveu MAIS o que ele engoliu)
  draft       stream -> transcript.md editavel
  previa      toca o corte no mpv, sem renderizar
  render      aplica os cortes e normaliza o volume
"""
import argparse, bisect, json, os, re, shutil, subprocess, sys, unicodedata, wave
from pathlib import Path

if sys.stdout.encoding and sys.stdout.encoding.lower() != "utf-8":
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import tomllib

ROOT = Path(__file__).resolve().parent
WORK = ROOT / "work"

ABRE, FECHA = "⟦", "⟧"      # marcado para CORTE
KABRE, KFECHA = "⟨", "⟩"    # resgatado para MANTER

# ---------------------------------------------------------------- utilidades

def _find_bin(name):
    p = shutil.which(name)
    if p:
        return p
    base = Path(os.environ.get("LOCALAPPDATA", ""))
    for cand in base.glob(f"Microsoft/WinGet/Packages/Gyan.FFmpeg*/**/bin/{name}.exe"):
        return str(cand)
    sys.exit(f"[erro] {name} nao encontrado no PATH")

FFMPEG = _find_bin("ffmpeg")
FFPROBE = _find_bin("ffprobe")


def cfg():
    with open(ROOT / "config.toml", "rb") as f:
        return tomllib.load(f)


def sem_acento(s):
    return "".join(c for c in unicodedata.normalize("NFD", s)
                   if unicodedata.category(c) != "Mn")


def chave(s):
    """Chave sem acento - para alinhamento e comparacao de repeticao."""
    return sem_acento(s).lower().strip(" .,!?;:\"'()[]…-")


def chave_ac(s):
    """Chave COM acento - para cacoetes, onde 'e' e 'é' sao coisas
    diferentes. Cortar a conjuncao 'e' destroi a frase."""
    return s.lower().strip(" .,!?;:\"'()[]…-")


# ===========================================================================
# CONCEITOS UNICOS
#
# Tudo que responde a uma mesma pergunta mora aqui, uma vez so. Este bloco
# existe porque a mesma regra chegou a viver em dois e tres lugares com
# versoes ligeiramente diferentes, e o defeito sempre aparecia pelo caminho
# que nao tinha recebido a correcao: a densidade de fala estava em
# `resolve_suspeitos` e faltava em `eh_hesitacao`; o piso de ruido era
# calculado com a configuracao aqui e com 12.0 no braco em alinhar.py.
#
# REGRA: ao precisar de uma destas respostas, CHAMAR a funcao. Nunca
# reimplementar no lugar de uso, nem "so desta vez".
# ===========================================================================

RE_ALONGAMENTO = re.compile(r"(.)\1{2,}")


def eh_alongada(txt):
    """Tem letra repetida 3+ vezes? ('queeee', 'ééé', 'naaao')"""
    return bool(RE_ALONGAMENTO.search(sem_acento(txt).lower()))


def letras_faladas(txt):
    """Quantas letras a palavra tem QUANDO FALADA.

    '2023' tem 4 caracteres escritos e 20 falados ("dois mil e vinte e
    três"). Medir pelo texto escrito fazia todo numero parecer arrastado
    5x e levar uma cauda cortada no meio - "e ele tem cin-- arcanos"."""
    try:
        from alinhar import expande
        txt = expande(txt)
    except Exception:
        pass
    return max(1, len(chave(txt).replace(" ", "")))


def densidade_fala(txt, dur):
    """Caracteres falados por segundo. Fala normal da ~13; fala lenta com
    pausa dentro cai para 4-6; vocalizacao ("Ous!" em 2,1s) fica perto de
    1,4. E o criterio para separar fala de ruido."""
    return len(chave(txt).replace(" ", "")) / max(0.1, dur)


def limiar_voz(db, offset_db):
    """Piso de ruido do proprio audio + folga = limiar de fala, em dB."""
    import numpy as np
    return float(np.percentile(db, 10)) + offset_db


def mede_voz(db, hop, a, b, limiar):
    """Quanto do trecho [a,b] tem voz -> (fracao de quadros, pico em dB)."""
    seg = db[int(a / hop):int(b / hop)]
    if len(seg) < 2:
        return 0.0, limiar
    return float((seg > limiar).mean()), float(seg.max())


def parece_frase(dur, frac, pico, limiar, ch):
    """Trecho SEM TEXTO grande e alto demais para ser cacoete?

    Quando o transcritor nao devolve texto, o programa vinha concluindo que
    nao havia nada ali e cortava. Errado: o microfone ouviu. Em IMG_0247
    isso apagou quatro frases inteiras - "outros tendem, muitas vezes, a
    seguir essa...", "e a leitura", "tem algo que a gente pode extrair",
    "nao tem nem as vezes um...".

    MEDIDO nos dois videos, relendo cada trecho com contexto: as cinco
    frases reais dao 1,10-1,77s, 57-93% dos quadros com voz e pico 37-45 dB
    acima do limiar de fala. O que fica (cacoete, "eeee" arrastado, folheio
    de carta) da 32-53% de voz, ou menos de 0,9s.

    A faixa de 55-61% e ambigua de verdade - um "É" arrastado e uma frase
    curta medem igual - e por isso a regra e FROUXA de proposito. Nao se
    corta no escuro: o trecho fica e aparece como [?? N.Ns] no revisao.txt
    para o passe de julgamento decidir. Sobrar cacoete se conserta numa
    anotacao; frase apagada nao se conserta, porque ninguem ve."""
    return (dur >= ch["guarda_dur"] and frac >= ch["guarda_voz"]
            and pico - limiar >= ch["guarda_db"])


def classifica_fala(txt, dur, sempre, dens_min):
    """O texto recuperado num trecho e cacoete ou e fala? -> (cortar, motivo)

    Usada por TODOS os caminhos que recuperam texto de um trecho: lacuna
    entre palavras, excesso de palavra com duracao impossivel, e o que
    vier depois."""
    if not txt or not chave(txt):
        return True, "sem texto"
    dens = densidade_fala(txt, dur)
    if dens < dens_min:
        return True, f"nao e fala ({dens:.1f} car/s)"
    toks = [chave_ac(w) for w in txt.split() if chave_ac(w)]
    if len(toks) <= 3 and all(t in sempre or len(t) <= 3 or eh_alongada(t)
                              for t in toks):
        return True, f"hesitação recuperada: {txt[:24]}"
    return False, f"fala nao transcrita: {txt[:26]}"


def hhmmss(t):
    h, r = divmod(int(t), 3600)
    m, s = divmod(r, 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def run(cmd, **kw):
    return subprocess.run(cmd, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", **kw)


def wdir(video):
    d = WORK / Path(video).stem
    d.mkdir(parents=True, exist_ok=True)
    return d


def jdump(obj, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


def jload(path):
    with open(path, encoding="utf-8") as f:
        return json.load(f)

# ------------------------------------------------------------------- lexico

def carrega_fillers():
    sempre, contextual = set(), set()
    p = ROOT / "lexicon" / "fillers.txt"
    if not p.exists():
        return sempre, contextual
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.split("#")[0].strip()
        if not ln:
            continue
        if ln.startswith("~"):
            contextual.add(chave_ac(ln[1:]))
        else:
            sempre.add(chave_ac(ln))
    return sempre, contextual


def carrega_termos():
    """Retorna (canonicos, mapa_de_1_palavra, mapa_de_frase).

    O mapa de frase existe porque "tarot de ala" -> "Tarô de Yala" tem tres
    palavras: comparando token a token nunca casaria. Chave = tupla de
    palavras normalizadas."""
    canon, uma, frase = [], {}, {}
    p = ROOT / "lexicon" / "terms.txt"
    if not p.exists():
        return canon, uma, frase
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.split("#")[0].strip()
        if not ln:
            continue
        partes = [x.strip() for x in ln.split("|") if x.strip()]
        if not partes:
            continue
        canon.append(partes[0])
        for erro in partes[1:]:
            ks = tuple(chave(w) for w in erro.split() if chave(w))
            if len(ks) == 1:
                uma[ks[0]] = partes[0]
            elif len(ks) > 1:
                frase[ks] = partes[0]
    return canon, uma, frase


def corrige_frases(words, frase):
    """Troca erros de varias palavras no lugar. Quando a correcao tem o
    mesmo numero de palavras, substitui uma a uma; quando nao tem, poe o
    texto inteiro no primeiro token e esvazia os demais - os tempos
    continuam certos de qualquer jeito."""
    if not frase:
        return 0
    ordem = sorted(frase, key=len, reverse=True)
    ks = [chave(w["w"]) for w in words]
    n, i = 0, 0
    while i < len(words):
        for alvo in ordem:
            L = len(alvo)
            if tuple(ks[i:i + L]) != alvo:
                continue
            novo = frase[alvo].split()
            if len(novo) == L:
                for j, t in enumerate(novo):
                    words[i + j]["w"] = t
            else:
                words[i]["w"] = frase[alvo]
                for j in range(1, L):
                    words[i + j]["w"] = ""
            n += 1
            i += L - 1
            break
        i += 1
    return n


def initial_prompt():
    """Prompt que (a) ancora vocabulario e (b) sinaliza ao Whisper que
    disfluencias devem ser transcritas, nao limpas."""
    canon, _, _ = carrega_termos()
    vocab = ", ".join(canon[:30])
    return (f"Aula sobre tarô. Vocabulário: {vocab}. "
            "Transcrição literal, incluindo hesitações: "
            "é... éé... ahn, hmm, tipo assim, né? "
            "Então... então... quer dizer, e... e... "
            "que que eu ia dizer mesmo?")

# ------------------------------------------------------------------- ingest

def cmd_ingest(a):
    d = wdir(a.video)
    r = run([FFPROBE, "-v", "error", "-print_format", "json",
             "-show_format", "-show_streams", a.video])
    info = json.loads(r.stdout)
    v = next(s for s in info["streams"] if s["codec_type"] == "video")
    meta = {"src": str(Path(a.video).resolve()),
            "dur": float(info["format"]["duration"]),
            "w": v["width"], "h": v["height"],
            "fps": v["r_frame_rate"], "vcodec": v["codec_name"]}
    jdump(meta, d / "meta.json")

    wav = d / "audio.wav"
    if not wav.exists() or a.force:
        print(f"[ingest] extraindo audio de {Path(a.video).name} "
              f"({hhmmss(meta['dur'])})...", flush=True)
        run([FFMPEG, "-y", "-v", "error", "-i", a.video, "-vn",
             "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(wav)])
    print(f"[ingest] ok  {meta['w']}x{meta['h']} {meta['vcodec']}  "
          f"{hhmmss(meta['dur'])}  -> {d}")

# --------------------------------------------------------------- transcribe

def cmd_transcribe(a):
    from faster_whisper import WhisperModel
    c = cfg()["whisper"]
    d = wdir(a.video)
    out = d / "words.json"
    if out.exists() and not a.force:
        print(f"[transcribe] cache: {out}")
        return
    modelo = a.model or c["model"]
    print(f"[transcribe] modelo={modelo} ({c['compute_type']}) ...", flush=True)
    m = WhisperModel(modelo, device="cpu", compute_type=c["compute_type"])
    segs, info = m.transcribe(
        str(d / "audio.wav"), language=c["language"],
        word_timestamps=True, vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=300),
        condition_on_previous_text=False, temperature=0.0,
        beam_size=c["beam_size"], initial_prompt=initial_prompt())
    words, nseg = [], 0
    for s in segs:
        nseg += 1
        for w in (s.words or []):
            words.append({"w": w.word.strip(), "s": round(w.start, 3),
                          "e": round(w.end, 3), "p": round(w.probability, 3)})
        if nseg % 20 == 0:
            print(f"  ... {nseg} segmentos, {len(words)} palavras "
                  f"(t={s.end:.0f}s)", flush=True)
    jdump({"model": modelo, "words": words}, out)
    print(f"[transcribe] {len(words)} palavras -> {out}")

# ------------------------------------------------------------------- align

def cmd_align(a):
    """Refina as fronteiras de palavra com alinhamento forcado.

    Etapa separada e com cache: e a mais cara do pipeline (~1x tempo real),
    e o resultado nao muda a menos que a transcricao mude."""
    d = wdir(a.video)
    p = d / "words.json"
    W = jload(p)
    if W.get("alinhado") and not a.force:
        print(f"[align] cache: {p}")
        return
    import alinhar
    words, info = alinhar.alinha_video(W["words"], d / "audio.wav")
    W["words"] = words
    W["alinhado"] = True
    W["align_info"] = info
    jdump(W, p)
    print(f"[align] -> {p}")

# ------------------------------------------------------------------ energia

def envelope(wav_path, hop=0.02):
    import numpy as np
    w = wave.open(str(wav_path), "rb")
    sr = w.getframerate()
    pcm = np.frombuffer(w.readframes(w.getnframes()),
                        dtype=np.int16).astype(np.float32) / 32768.0
    n = int(hop * sr)
    fr = pcm[:len(pcm) // n * n].reshape(-1, n)
    db = 20 * np.log10(np.sqrt((fr ** 2).mean(axis=1) + 1e-12))
    return db, hop, len(pcm) / sr

# ------------------------------------------------- guardas anti-alucinacao

HALLU = [r"^tchau", r"obrigad", r"legenda", r"amara\.org", r"inscreva",
         r"at[ée] a pr[óo]xima", r"subtitle", r"^valeu",
         r"bom dia", r"boa noite", r"^m[úu]sica", r"^\[.*\]$"]


def alucinou(txt, dur, logprob, c):
    """Retorna motivo da rejeicao, ou None se a re-transcricao e confiavel."""
    if not txt or len(chave(txt)) < 1:
        return "vazio"
    t = txt.lower().strip()
    for pat in HALLU:
        if re.search(pat, t):
            return "frase-fantasma"
    toks = [chave(x) for x in txt.split() if chave(x)]
    if dur > 0 and len(toks) / dur > c["max_palavras_seg"]:
        return f"denso demais ({len(toks)/dur:.1f} pal/s)"
    for n in (1, 2, 3):                       # loop travado
        for i in range(len(toks) - n * 4 + 1):
            bloco = toks[i:i + n]
            if all(toks[i + k * n:i + (k + 1) * n] == bloco for k in range(4)):
                return "loop repetido"
    if logprob is not None and logprob < c["min_logprob"]:
        return f"logprob {logprob:.2f}"
    return None

# --------------------------------------------------- marcacao: cacoetes

def resolve_suspeitos(stream, escuta, ch, sempre, dens_min, db, hop, limiar):
    """Descobre o que ha nos trechos de duracao impossivel.

    Quando o Whisper deixa de transcrever alguma coisa, o alinhador nao tem
    onde por o audio sobrando e o gruda na palavra vizinha - 'principalmente'
    ficou com 4,67s escondendo um "nessas cartas" repetido. Aqui esse excesso
    e re-transcrito isolado e vira um token proprio, com texto de verdade,
    em vez de virar uma cauda cortada no escuro.

    Corta quando o que aparece repete o que vem depois (o Whisper colapsou
    uma repeticao) ou parece cacoete. Se for conteudo novo, PRESERVA - fala
    perdida nao pode virar corte silencioso."""
    novos, n = [], 0
    for i, t in enumerate(stream):
        if not t.get("suspeito"):
            novos.append(t)
            continue
        a, b = t["suspeito"]
        txt, lp = escuta(a, b)
        mot = alucinou(txt, b - a, lp, ch)
        t["e"] = a
        t.pop("suspeito", None)
        novos.append(t)
        if mot:
            # O Whisper nao ter lido NAO e prova de que nao ha nada. Antes
            # de apagar, pergunta-se ao microfone (ver `parece_frase`).
            frac, pico = mede_voz(db, hop, a, b, limiar)
            if parece_frase(b - a, frac, pico, limiar, ch):
                rot, cortar = f"?? {b-a:.1f}s", False
                why = (f"som alto sem texto ({frac*100:.0f}% de voz, "
                       f"pico {pico-limiar:.0f}dB acima) - nao se corta no "
                       f"escuro; rejeicao do Whisper: {mot}")
            else:
                rot, cortar, why = ("hesitação", True,
                                    f"voz sem texto {b-a:.1f}s")
        else:
            cortar, why = classifica_fala(txt, b - a, sempre, dens_min)
            rot = txt        # o eco e decidido depois, por marca_ecos
        novos.append({"k": "hesitacao", "t": rot, "s": round(a, 3),
                      "e": round(b, 3), "cut": cortar, "why": why})
        n += 1
    if n:
        stream[:] = novos
    return n


def carrega_marcadores():
    p = ROOT / "lexicon" / "marcadores.txt"
    if not p.exists():
        return []
    out = []
    for ln in p.read_text(encoding="utf-8").splitlines():
        ln = ln.split("#")[0].strip()
        if ln:
            ks = tuple(chave(w) for w in ln.split() if chave(w))
            if ks:
                out.append(ks)
    return sorted(out, key=len, reverse=True)


def marca_instrucoes(stream, marcadores, respiro, max_volta=25.0):
    """Corta onde ela MANDA cortar.

    Gravando sozinha, a Mariana narra a propria edicao: "nao, corta essa
    parte", "corto essa parte, espere, deixa eu pensar". Duas vezes em dez
    minutos no IMG_0248. Nenhuma heuristica chega perto dessa confianca -
    e a locutora dizendo literalmente o que quer fora.

    Descarta a frase da instrucao e a tentativa abandonada anterior, voltando
    ate o ultimo respiro (pausa ou hesitacao) - que e onde a tentativa
    comecou."""
    if not marcadores:
        return 0
    idx = [i for i, t in enumerate(stream) if t["k"] == "palavra"]
    keys = [chave(stream[i]["t"]) for i in idx]
    n = 0
    for p in range(len(idx)):
        for m in marcadores:
            L = len(m)
            if tuple(keys[p:p + L]) != m:
                continue
            fim = idx[p + L - 1]
            # volta ate o respiro anterior: ali comecou a tentativa ruim
            ini = idx[p]
            k = idx[p] - 1
            while k >= 0:
                if stream[fim]["e"] - stream[k]["s"] > max_volta:
                    break
                if (stream[k]["k"] == "silencio"
                        and stream[k]["e"] - stream[k]["s"] > respiro):
                    break
                ini = k
                k -= 1
            for j in range(ini, fim + 1):
                if not stream[j]["cut"]:
                    n += 1
                stream[j]["cut"] = True
                stream[j]["prot"] = False
                stream[j]["why"] = f"ela mandou cortar: {' '.join(m)}"
            break
    return n


def marca_ecos(stream, janela=6):
    """Trecho recuperado que REPETE uma palavra vizinha e retomada.

    Passa sobre o fluxo inteiro, uma vez, para todo trecho de texto
    recuperado - venha ele de lacuna entre palavras ou de palavra com
    duracao impossivel. A regra ja existiu so num dos dois caminhos, e pelo
    outro escapava "como se [como seja...] existisse uma tradicao"."""
    n = 0
    for i, t in enumerate(stream):
        if t["k"] != "hesitacao" or t["cut"] or not chave(t["t"]):
            continue
        viz = {chave(x["t"])
               for x in stream[max(0, i - janela):i + janela + 1]
               if x["k"] == "palavra" and chave(x["t"])}
        pals = [p for p in chave(t["t"]).split() if len(p) > 2]
        if any(p in viz for p in pals):
            t["cut"] = True
            t["why"] = f"retomada colapsada: {t['t'][:26]}"
            n += 1
    return n


def _e_lacuna(t):
    return t["k"] in ("silencio", "hesitacao")


# Palavras cuja repeticao imediata e enfase deliberada, nao gagueira.
ENFASE = {"nao", "sim", "muito", "bem", "so", "ja", "mais", "menos",
          "devagar", "calma", "obrigada", "isso"}


def marca_cacoetes(stream, sempre, contextual):
    """Marca cacoetes. Os ambiguos (é, e, então, né...) so caem quando
    estao ILHADOS entre pausas - senao cortariamos a conjuncao 'e' em
    'cavaleiros e cavaleiras' e a frase desmontaria."""
    n = 0
    for i, t in enumerate(stream):
        if t["k"] != "palavra" or t["cut"]:
            continue
        k = chave_ac(t["t"])
        ant = stream[i - 1] if i > 0 else None
        pro = stream[i + 1] if i + 1 < len(stream) else None

        if k in sempre:
            t["cut"], t["why"] = True, "cacoete"
        elif eh_alongada(t["t"]):
            t["cut"], t["why"] = True, "alongamento"
        elif k in contextual and ant is not None and pro is not None \
                and _e_lacuna(ant) and _e_lacuna(pro):
            t["cut"], t["why"] = True, "muleta isolada"
        elif (pro is not None and pro["k"] == "palavra"
              and chave(t["t"]) and chave(pro["t"]) == chave(t["t"])
              and chave(t["t"]) not in ENFASE and len(chave(t["t"])) > 2):
            # palavra identica repetida em sequencia ("cavaleiras,
            # cavaleiras") e gagueira, nao escolha. Corta a primeira e
            # deixa a segunda. ENFASE protege o uso retorico ("nao, nao").
            t["cut"], t["why"] = True, "gaguejada"
        if t["cut"]:
            n += 1
    return n

# ------------------------------------------ marcacao: tentativas de fala

def detecta_tentativas(stream, min_ngram=3, janela_s=30.0, max_span=15.0,
                       max_palavras=12):
    """Objetivo 3: ela comeca uma frase, para, recomeca, ate acertar.

    Procura a mesma sequencia de >=min_ngram palavras reaparecendo pouco
    depois. A primeira ocorrencia (e a bagunca ate a ultima) e tentativa;
    a ultima e a boa.

    O PERIGO e confundir isso com PARALELISMO DIDATICO, que essa aula tem
    aos montes: "que ta relacionado ao elemento fogo / agua / ar / terra".
    Repeticao mecanica nao distingue os dois. Tres filtros separam:

      1. a retomada tem que vir logo APOS UMA PAUSA - numa lista paralela
         a frase repetida vem grudada no meio da sentenca ("Copas, que ta
         relacionado..."), nao depois de um respiro;
      2. o trecho descartado tem que ser CURTO (<=max_palavras) - tentativa
         abandonada e curta, item de lista e uma oracao inteira;
      3. tem que haver uma PAUSA OU HESITACAO dentro do trecho - o ponto
         onde ela travou.

    Ainda assim isto e uma proposta conservadora: o caso duvidoso e melhor
    resolvido lendo o texto (comando `review`)."""
    idx = [i for i, t in enumerate(stream) if t["k"] == "palavra"]
    keys = [chave(stream[i]["t"]) for i in idx]
    n = len(idx)
    cadeias, consumido = [], set()

    for a in range(n):
        if a in consumido or not keys[a]:
            continue
        achou = None
        for b in range(a + min_ngram, min(n, a + 90)):
            if stream[idx[b]]["s"] - stream[idx[a]]["s"] > janela_s:
                break
            m = 0
            while (a + m < b and b + m < n
                   and keys[a + m] and keys[a + m] == keys[b + m]):
                m += 1
            if m < min_ngram:
                continue
            # filtro 1: a retomada precisa vir depois de um respiro
            ant = stream[idx[b] - 1] if idx[b] > 0 else None
            if ant is None or not _e_lacuna(ant):
                continue
            achou = (b, m)
            break
        if not achou:
            continue
        b, m = achou
        ini, fim = idx[a], idx[b]
        dur = stream[fim]["s"] - stream[ini]["s"]
        # Repeticao LONGA afrouxa os limites. Sete palavras identicas
        # reaparecendo depois de 18s de pausa nao sao paralelismo didatico,
        # sao retomada - e foi assim que escapou "força de um arcano menor,
        # ela não ... está atrelada à [18s] força de um arcano menor, ela
        # não está necessariamente atrelada".
        longa = m >= 5
        lim_span = max_span * 3 if longa else max_span
        lim_pal = max_palavras * 2.5 if longa else max_palavras
        if dur > lim_span:
            continue
        # filtro 2: tentativa abandonada e curta; item de lista nao e
        if b - a > lim_pal:
            continue
        # filtro 3: precisa existir o ponto onde ela travou
        if not any(_e_lacuna(stream[j]) for j in range(ini, fim)):
            continue
        cadeias.append({
            "ini": ini, "fim": fim, "n": m, "dur": round(dur, 2),
            "t_ini": stream[ini]["s"], "t_fim": stream[fim]["s"],
            "repetido": " ".join(stream[idx[a + k]]["t"] for k in range(m)),
            "descartado": " ".join(stream[j]["t"] for j in range(ini, fim)
                                   if stream[j]["k"] == "palavra"),
        })
        consumido.update(range(a, b))
    return cadeias


RE_SO_NUM = re.compile(r"^[\d.,:/-]+$")



def eh_hesitacao(lac, palavra_antes, sempre, ch, limiar):
    """"Voz com energia e sem texto" NAO e sempre cacoete. Decide se corta.

    Descoberto do jeito ruim: o Whisper escreveu "2023" como um token curto,
    mas ela falou "dois mil e vinte e tres" durante ~1,5s. A sobra virou
    "voz sem texto", foi classificada como hesitacao e cortada - apagando o
    ano da frase.

    A trava "nunca depois de numero" que eu tinha posto aqui deixou de ser
    necessaria e virou estorvo: a etapa `align` expande "2023" ate 210,81,
    cobrindo o numero falado inteiro, e nao sobra lacuna nenhuma. Manter a
    trava so protegeria cacoete legitimo que venha depois de um numero.

    Fica a trava que continua valendo: se a re-transcricao isolada devolveu
    PALAVRAS DE VERDADE, isso e fala que o Whisper perdeu, nao cacoete."""
    txt = (lac.get("texto") or "").strip()
    if not txt:
        # Sem texto nao quer dizer sem fala. Ver `parece_frase`: acima de
        # certo tamanho e certa energia isto e frase, e frase nao se apaga
        # so porque o transcritor nao deu conta de ler.
        if parece_frase(lac["dur"], lac.get("voz", 0.0),
                        lac.get("pico", limiar), limiar, ch):
            return False, (f"som alto sem texto ({lac.get('voz',0)*100:.0f}% "
                           f"de voz) - mantido para julgamento")
        return True, f"voz sem texto {lac['dur']:.1f}s"
    return classifica_fala(txt, lac["dur"], sempre, ch["densidade_minima"])


def carrega_anotacoes(d):
    """work/<video>/anotacoes.json - cortes de julgamento.

    Existe porque ha coisas que nenhuma heuristica pega: a Mariana ser
    interrompida ("Que foi, Rafudo?"), ou refazer o mesmo raciocinio com
    outras palavras (repeticao literal nao casa). Sao trechos escolhidos
    lendo o texto. Formato:

        [{"ini": 228.1, "fim": 231.4, "motivo": "interrupcao domestica"}]

    Fica em arquivo separado do stream para sobreviver a re-analise: apagar
    e refazer a transcricao nao perde o julgamento ja feito."""
    p = d / "anotacoes.json"
    if not p.exists():
        return []
    try:
        dados = jload(p)
    except Exception as e:
        print(f"[analyze] anotacoes.json ilegivel ({e}); ignorando")
        return []
    ok = []
    for a in dados if isinstance(dados, list) else []:
        try:
            ini, fim = float(a["ini"]), float(a["fim"])
        except (KeyError, TypeError, ValueError):
            continue
        if fim > ini:
            ok.append({"ini": ini, "fim": fim,
                       "motivo": str(a.get("motivo", "anotacao"))})
    return ok


def encaixa_anotacoes(stream, anot):
    """Encolhe cada anotacao ate cair em fronteira de palavra.

    Uma anotacao e escrita a olho, lendo tempos da transcricao, e por isso
    cai facil no meio de uma palavra - foi assim que 'reis,' (155,17-156,33)
    sumiu de 'as rainhas estao acima dos reis' quando anotei o corte
    comecando em 156,30. Cortar para dentro so pode perder o que ja se
    queria perder; cortar para fora comeria a palavra vizinha."""
    pal = [t for t in stream if t["k"] == "palavra"]
    for a in anot:
        ini, fim = a["ini"], a["fim"]
        # so mexe na borda que ATRAVESSA uma palavra. Encolher ate a
        # primeira palavra inteira jogaria fora o silencio das pontas -
        # justamente o que a anotacao do fim do video precisa cortar.
        for t in pal:
            if t["s"] < ini < t["e"]:
                ini = t["e"]
            if t["s"] < fim < t["e"]:
                fim = t["s"]
        if abs(ini - a["ini"]) > 0.01 or abs(fim - a["fim"]) > 0.01:
            a["ajustada"] = [round(a["ini"], 2), round(a["fim"], 2)]
        a["ini"], a["fim"] = ini, fim
    return [a for a in anot if a["fim"] - a["ini"] > 0.05]


def aplica_anotacoes(stream, anot):
    """Marca para corte todo token dentro de um trecho anotado. Vence as
    protecoes: se foi julgado, sai."""
    n = 0
    for a in anot:
        for t in stream:
            if t["e"] <= a["ini"] or t["s"] >= a["fim"]:
                continue
            if not t["cut"]:
                n += 1
            t["cut"] = True
            t["prot"] = False
            t["why"] = f"anotado: {a['motivo']}"
    return n



def marca_arrastadas(stream, sempre, C):
    """Alongamento detectado por DURACAO, nao por letra repetida.

    O Whisper escreve "queeeee" como "que" e "ééé" como "e": o texto nao
    denuncia nada, mas a palavra ocupa varias vezes o tempo normal. Compara
    cada palavra com o ritmo mediano do proprio video (cada pessoa fala num
    ritmo; a mediana se adapta sozinha).

    Palavra de UMA letra arrastada e cacoete puro e sai inteira. Palavra
    maior tem so a CAUDA cortada, preservando o comeco - "em" arrastado por
    2,7s vira "em", nao vira nada. E o que um editor humano faz."""
    import statistics
    ca = C["arrastada"]
    pal = [t for t in stream if t["k"] == "palavra" and chave(t["t"])]
    if len(pal) < 20:
        return 0
    med = statistics.median((t["e"] - t["s"]) / letras_faladas(t["t"])
                            for t in pal)
    n = 0
    for t in pal:
        if t["cut"] or t.get("prot"):
            continue
        L = letras_faladas(t["t"])
        dur, esperado = t["e"] - t["s"], med * L
        if dur < ca["min_dur"] or dur < esperado * ca["fator"]:
            continue
        vezes = dur / esperado
        excesso = dur - esperado * ca["manter"]
        # Excesso GRANDE nao e arrasto: e fala que o Whisper nao transcreveu
        # e o alinhador teve que absorver na palavra vizinha. Foi o caso de
        # 'principalmente' com 4,67s - ali dentro havia um "nessas cartas"
        # repetido que o Whisper colapsou. Cortar uma cauda fixa nesse
        # trecho e chute; marca-se para o analyze re-transcrever e decidir.
        if excesso > ca["excesso_suspeito"]:
            t["suspeito"] = [round(t["s"] + esperado * ca["manter"], 3),
                             round(t["e"], 3)]
            t["why"] = (f"duracao impossivel ({vezes:.0f}x): "
                        f"{excesso:.1f}s de fala nao transcrita")
            n += 1
            continue
        # So sai INTEIRA se for cacoete do lexico. Uma palavra de verdade,
        # por curta que seja, perde apenas a cauda: a Mariana arrasta a
        # conjuncao ("ar... eeeee... ouro") e apagar o "e" quebraria a
        # frase. Encurtar de 1,06s para 0,12s ja resolve o incomodo.
        if chave_ac(t["t"]) in sempre:
            t["cut"] = True
            t["why"] = f"arrastada {vezes:.0f}x"
        else:
            corte_ini = t["s"] + esperado * ca["manter"]
            if t["e"] - corte_ini < 0.25:
                continue
            t["cauda"] = [round(corte_ini, 3), t["e"]]
            t["why"] = f"cauda arrastada {vezes:.0f}x"
        n += 1
    return n


# A contagem de cartas - "As, dois, tres, quatro..." - tem que correr
# inteira. So entram aqui numeros INEQUIVOCOS. "as", "um" e "uma" ficaram
# de fora de proposito: `chave()` tira o acento, entao a carta "as" e o
# artigo "as" viram a mesma string, e artigo e das palavras mais comuns do
# idioma. Com eles dentro, o IMG_0247 gerava 6 sequencias e TODAS eram
# falsas ("As figuras, as", "uma acao, uma", "as vezes um"); no total dos
# tres videos a protecao salvou de corte exatamente um token - um "Hum!",
# cacoete que devia sair mesmo.
RE_NUM = re.compile(
    r"^\d+[ºªo]?$|^(dois|duas|tres|quatro|cinco|seis|sete|oito|"
    r"nove|dez|onze|doze|treze|quatorze|catorze|quinze|dezesseis|"
    r"dezessete|dezoito|dezenove|vinte)$")

# Numero AMBIGUO: pega carona numa sequencia que ja se sustenta sem ele,
# nunca a abre nem a sustenta sozinho.
RE_NUM_FRACO = re.compile(r"^(as|um|uma)$")

NAIPES = {"ouros", "ouro", "paus", "pau", "copas", "copa", "espadas",
          "espada", "bastoes", "bastao", "moedas", "moeda"}


def classe_num(pal, k):
    """O que este token e para a varredura de contagem: numero inequivoco
    ("forte"), numero que tambem e artigo ("fraco") ou nada.

    O As e a excecao que vale codigo: acompanhado do naipe - "As de Ouros",
    "As de Paus" - ele e carta, nao artigo, e conta como forte. Sozinho e
    artigo ate prova em contrario, e a prova e a vizinhanca (ver
    `protege_enumeracoes`)."""
    c = chave(pal[k]["t"])
    if RE_NUM.match(c):
        return "forte"
    if (c == "as" and k + 2 < len(pal) and chave(pal[k + 1]["t"]) == "de"
            and chave(pal[k + 2]["t"]) in NAIPES):
        return "forte"
    if RE_NUM_FRACO.match(c):
        return "fraco"
    return None


def protege_enumeracoes(stream, cn, minimo=3):
    """Contagem corrida - "As, 2, 3, 4, 5, 6, 7, 8, 9, 10" - tem que correr
    inteira. Encurtar as pausas entre os numeros quebra o ritmo e o video
    fica sem sentido. Marca a sequencia como intocavel.

    Duas exigencias, as duas medidas (ver [contagem] no config):

    1. `fortes_min` numeros inequivocos. Sem isso "as vezes e um filtro"
       virava contagem, e protecao falsa nao e neutra: ela impede corte de
       cacoete e - pior - cancelava a poda de cauda arrastada.
    2. Numeros vizinhos NO TEMPO. Silencio nao e token `palavra`, entao a
       varredura nao o enxerga: sem esta trava, "As figuras" (80,0s) e "as"
       (86,0s) pareciam lado a lado e a sequencia falsa atravessava 5,6s de
       silencio."""
    idx = [i for i, t in enumerate(stream) if t["k"] == "palavra"]
    pal = [stream[i] for i in idx]
    cls = [classe_num(pal, k) for k in range(len(pal))]
    n, i = 0, 0
    while i < len(idx):
        if not cls[i]:
            i += 1
            continue
        # `ult` e o ultimo numero visto - a distancia se mede a partir dele,
        # nao do token anterior, senao uma palavra qualquer no meio serve de
        # ponte por cima da pausa.
        j, falhas, ult = i, 0, i
        while j + 1 < len(idx) and falhas < 2:
            if pal[j + 1]["s"] - pal[ult]["e"] > cn["gap_max"]:
                break
            if cls[j + 1]:
                j += 1
                falhas = 0
                ult = j
            else:
                falhas += 1
                j += 1
        j = ult
        fortes = sum(1 for k in range(i, j + 1) if cls[k] == "forte")
        if j - i + 1 >= minimo and fortes >= cn["fortes_min"]:
            for k in range(idx[i], idx[j] + 1):
                stream[k]["prot"] = True
                if stream[k]["cut"]:
                    stream[k]["cut"] = False
                    stream[k]["why"] = "protegido: contagem"
                    n += 1
            i = j + 1
        else:
            # sequencia recusada nao consome o que vem depois: a contagem de
            # verdade pode comecar dentro dela.
            i += 1
    return n


def marca_tentativas(stream, cadeias):
    n = 0
    for c in cadeias:
        for j in range(c["ini"], c["fim"]):
            if not stream[j]["cut"]:
                stream[j]["cut"] = True
                stream[j]["why"] = "tentativa"
                n += 1
    return n

# ------------------------------------------------------------------ analyze

def cmd_analyze(a):
    import numpy as np
    from faster_whisper import WhisperModel
    C = cfg()
    cs, ch, cw = C["silencio"], C["hesitacao"], C["whisper"]
    d = wdir(a.video)
    words = jload(d / "words.json")["words"]
    dur_total = jload(d / "meta.json")["dur"]
    db, hop, dur_wav = envelope(d / "audio.wav")

    limiar = limiar_voz(db, cs["limiar_fala_db"])
    voz = db > limiar
    print(f"[analyze] limiar de fala={limiar:.1f}dB  "
          f"{100*voz.mean():.0f}% dos frames com voz", flush=True)

    # --- classifica lacunas -------------------------------------------
    lacunas = []
    for i, (x, y) in enumerate(zip(words, words[1:])):
        g = y["s"] - x["e"]
        if g < cs["gap_min"]:
            continue
        # `pico` vai junto da fracao porque a fracao sozinha nao distingue
        # folheio de carta (muitos quadros, todos fracos) de fala. As duas
        # medidas saem de `mede_voz`, a mesma que o resto do programa usa.
        f, pico = mede_voz(db, hop, x["e"], y["s"], limiar)
        tipo = ("fala" if f > cs["frac_voz_fala"]
                else "silencio" if f <= cs["frac_voz_silencio"] else "misto")
        lacunas.append({"i": i, "s": x["e"], "e": y["s"], "dur": round(g, 3),
                        "voz": round(f, 3), "pico": round(pico, 1),
                        "tipo": tipo})

    alvo = [l for l in lacunas if l["tipo"] in ("fala", "misto")
            and l["dur"] >= ch["isolar_min"]]
    print(f"[analyze] {len(lacunas)} lacunas; {len(alvo)} com voz e "
          f"duracao suficiente -> re-transcricao isolada", flush=True)

    # --- re-transcreve as lacunas com voz ------------------------------
    _cache = {}

    def escuta(s0, e0):
        """Re-transcreve um trecho isolado. Carrega o Whisper so na
        primeira chamada e reaproveita depois."""
        if "m" not in _cache:
            w = wave.open(str(d / "audio.wav"), "rb")
            _cache["sr"] = w.getframerate()
            _cache["pcm"] = np.frombuffer(w.readframes(w.getnframes()),
                                          dtype=np.int16)
            w.close()
            _cache["m"] = WhisperModel(a.model or cw["model"], device="cpu",
                                       compute_type=cw["compute_type"])
        sr, pcm, m = _cache["sr"], _cache["pcm"], _cache["m"]
        s0, e0 = max(0.0, s0), min(dur_wav, e0)
        tmp = d / "_clip.wav"
        ww = wave.open(str(tmp), "wb")
        ww.setnchannels(1)
        ww.setsampwidth(2)
        ww.setframerate(sr)
        ww.writeframes(pcm[int(s0 * sr):int(e0 * sr)].tobytes())
        ww.close()
        segs, _ = m.transcribe(str(tmp), language=cw["language"],
                               vad_filter=False, temperature=0.0,
                               condition_on_previous_text=False,
                               beam_size=cw["beam_size"],
                               no_speech_threshold=0.95)
        segs = list(segs)
        txt = " ".join(s.text.strip() for s in segs).strip()
        lp = (sum(s.avg_logprob for s in segs) / len(segs)) if segs else None
        return txt, lp

    for k, l in enumerate(alvo, 1):
        txt, lp = escuta(l["s"] - 0.10, l["e"] + 0.10)
        mot = alucinou(txt, l["dur"], lp, ch)
        l["bruto"] = txt
        l["rejeitado"] = mot
        l["texto"] = "" if mot else txt
        if k % 10 == 0 or k == len(alvo):
            print(f"  ... {k}/{len(alvo)}", flush=True)
    (d / "_clip.wav").unlink(missing_ok=True)

    # --- funde tudo num unico fluxo ------------------------------------
    sempre, contextual = carrega_fillers()
    _, corr, corr_frase = carrega_termos()
    nf = corrige_frases(words, corr_frase)
    if nf:
        print(f"[analyze] correcoes de frase aplicadas: {nf}")
    porlac = {l["i"]: l for l in lacunas}
    stream = []

    def add(kind, texto, s, e, cortar, motivo):
        stream.append({"k": kind, "t": texto, "s": round(s, 3),
                       "e": round(e, 3), "cut": cortar, "why": motivo})

    if words and words[0]["s"] > cs["pausa_intocavel"]:
        add("silencio", "", 0.0, words[0]["s"], True, "abertura")

    for i, x in enumerate(words):
        texto = corr.get(chave(x["w"]), x["w"])
        add("palavra", texto, x["s"], x["e"], False, "")

        l = porlac.get(i)
        if not l:
            continue
        if l["tipo"] == "silencio" or (l["tipo"] == "misto" and not l.get("texto")):
            if l["dur"] > cs["pausa_intocavel"]:
                add("silencio", "", l["s"], l["e"], True,
                    f"pausa {l['dur']:.1f}s")
        else:
            rot = l.get("texto") or "hesitação"
            cortar, motivo = eh_hesitacao(l, words[i], sempre, ch, limiar)
            # som alto que ninguem leu e que se decidiu NAO cortar: o rotulo
            # tem de gritar isso no revisao.txt, senao passa por hesitacao.
            if not cortar and not l.get("texto"):
                rot = f"?? {l['dur']:.1f}s"
            add("hesitacao", rot, l["s"], l["e"], cortar, motivo)

    n_cac = marca_cacoetes(stream, sempre, contextual)
    n_arr = marca_arrastadas(stream, sempre, C)
    if n_arr:
        print(f"[analyze] palavras arrastadas: {n_arr}")
    cadeias = detecta_tentativas(stream)
    n_tent = marca_tentativas(stream, cadeias)
    n_inst = marca_instrucoes(stream, carrega_marcadores(),
                              C["instrucao"]["respiro"])
    if n_inst:
        print(f"[analyze] marcadores verbais de corte: {n_inst} tokens "
              f"(ela pediu para cortar)")
    n_susp = resolve_suspeitos(stream, escuta, ch, sempre,
                               ch["densidade_minima"], db, hop, limiar)
    if n_susp:
        print(f"[analyze] trechos de duracao impossivel resolvidos: {n_susp}")
    n_eco = marca_ecos(stream)
    if n_eco:
        print(f"[analyze] retomadas colapsadas: {n_eco}")
    n_prot = protege_enumeracoes(stream, C["contagem"])
    anot = encaixa_anotacoes(stream, carrega_anotacoes(d))
    for a in anot:
        if a.get("ajustada"):
            print(f"[analyze] anotacao {a['ajustada']} encaixada em "
                  f"[{a['ini']:.2f}, {a['fim']:.2f}] (fronteira de palavra)")
    n_anot = aplica_anotacoes(stream, anot)
    print(f"[analyze] cacoetes marcados: {n_cac}")
    if n_prot:
        print(f"[analyze] contagens protegidas: {n_prot} tokens preservados")
    if anot:
        print(f"[analyze] anotacoes de julgamento: {len(anot)} trechos, "
              f"{n_anot} tokens novos")
    print(f"[analyze] cadeias de tentativa: {len(cadeias)} "
          f"({n_tent} tokens, {sum(c['dur'] for c in cadeias):.0f}s)")

    jdump({"lacunas": lacunas, "stream": stream, "cadeias": cadeias,
           "anotacoes": anot, "dur": dur_total}, d / "stream.json")

    ncut = sum(1 for t in stream if t["cut"])
    tcut = sum(t["e"] - t["s"] for t in stream if t["cut"])
    rec = sum(1 for l in alvo if l.get("texto"))
    print(f"[analyze] fluxo com {len(stream)} tokens; "
          f"{ncut} marcados ({tcut:.0f}s = {100*tcut/dur_total:.0f}% do video)")
    print(f"[analyze] hesitacoes recuperadas com texto: {rec}/{len(alvo)}")
    print(f"[analyze] -> {d/'stream.json'}")

# -------------------------------------------------------------------- draft

CAB = """<!-- tarocut v1 | video={video} | tokens={n} | selo={selo} -->
<!--
COMO EDITAR ESTE ARQUIVO
  * Apague qualquer texto para cortar o trecho correspondente do video.
  * Os trechos {A}assim{F} ja estao marcados para corte (cacoetes,
    hesitacoes, pausas). Deixe como estao para que sejam cortados.
  * Para MANTER um trecho marcado, troque {A} {F} por {KA} {KF}.
  * Nao altere as linhas "## [b###]" - sao as ancoras de tempo.
  * Ao terminar:  python tarocut.py render {video}
-->

"""


def relatorio(st, cadeias, dur):
    """Resumo no topo: o que vai sair e por que. As cadeias de tentativa
    ficam listadas ANTES do texto, para nao serem descobertas tarde."""
    por = {}
    for t in st:
        if t["cut"]:
            k = t["why"].split()[0] if t["why"] else "outro"
            por[k] = por.get(k, 0.0) + (t["e"] - t["s"])
    tot = sum(por.values())
    L = ["## Resumo",
         "",
         f"- Duracao original: **{hhmmss(dur)}**",
         f"- Marcado para corte: **{hhmmss(tot)}** "
         f"({100*tot/dur:.0f}%) -> sobra ~{hhmmss(dur-tot)}",
         ""]
    for k, v in sorted(por.items(), key=lambda x: -x[1]):
        L.append(f"  - {k}: {v:.0f}s")
    L.append("")
    if cadeias:
        L += ["## Tentativas de fala detectadas",
              "",
              "Ela comecou, parou e refez. Cada linha mostra o que sera",
              "DESCARTADO; a versao final da frase e mantida.",
              ""]
        for i, c in enumerate(cadeias, 1):
            desc = c["descartado"][:150] + ("..." if len(c["descartado"]) > 150 else "")
            L.append(f"{i}. **{hhmmss(c['t_ini'])}** ({c['dur']:.1f}s) "
                     f"reencontrou `{c['repetido']}`")
            L.append(f"   descarta: _{desc}_")
        L.append("")
    L.append("---")
    return "\n".join(L)


def blocos(st):
    """Fatia o fluxo em blocos de transcricao: uma pausa longa fecha o
    bloco, e sem pausa ele fecha no tamanho maximo.

    Vive aqui, e nao dentro do `cmd_draft`, porque o editor grafico precisa
    da MESMA fatia. As ancoras `[b###]` sao o vocabulario comum entre o
    transcript.md e a tela: se cada um cortasse os blocos a seu modo, o
    mesmo trecho teria dois nomes."""
    out, i0, bid = [], 0, 1
    for i, t in enumerate(st):
        if (t["k"] == "silencio" and t["e"] - t["s"] > 1.5) or i - i0 > 89:
            out.append({"id": bid, "i0": i0, "i1": i + 1,
                        "ini": st[i0]["s"], "fim": t["e"]})
            i0, bid = i + 1, bid + 1
    if i0 < len(st):
        out.append({"id": bid, "i0": i0, "i1": len(st),
                    "ini": st[i0]["s"], "fim": st[-1]["e"]})
    return out


def texto_token(t):
    """Superficie de um token no transcript.md.

    O par de simbolos TEM que refletir t["cut"] em todos os tipos. Marcar
    toda pausa e toda hesitacao com ⟦ ⟧ fazia o round-trip reimpor o
    corte que a analise tinha decidido evitar - a revisao anulava a analise
    sem avisar."""
    A, F = (ABRE, FECHA) if t["cut"] else (KABRE, KFECHA)
    if t["k"] == "palavra":
        return f"{A}{t['t']}{F}" if t["cut"] else t["t"]
    if t["k"] == "silencio":
        return f"{A}pausa {t['e']-t['s']:.1f}s{F}"
    return f"{A}{t['t']} · {t['e']-t['s']:.1f}s{F}"


def cmd_draft(a):
    d = wdir(a.video)
    S = jload(d / "stream.json")
    st, cadeias, dur = S["stream"], S.get("cadeias", []), S["dur"]
    linhas = []
    for b in blocos(st):
        linhas.append(f"\n## [b{b['id']:03d}] {hhmmss(b['ini'])} → "
                      f"{hhmmss(b['fim'])}")
        linhas.append(" ".join(texto_token(t) for t in st[b["i0"]:b["i1"]]))

    out = d / "transcript.md"
    cab = CAB.format(video=Path(a.video).name, n=len(st),
                     selo=selo_stream(st),
                     A=ABRE, F=FECHA, KA=KABRE, KF=KFECHA)
    out.write_text(cab + relatorio(st, cadeias, dur) + "\n"
                   + "\n".join(linhas) + "\n", encoding="utf-8")
    print(f"[draft] -> {out}")

# ------------------------------------------------ round-trip da transcricao

TOKEN_RE = re.compile(r"⟦([^⟧]*)⟧|⟨([^⟩]*)⟩|([^\s⟦⟧⟨⟩]+)")
RE_PAUSA = re.compile(r"^pausa\s+([\d.]+)s$")


def chave_token(t):
    if t["k"] == "palavra":
        return chave(t["t"])
    dur = t["e"] - t["s"]
    return (f"§pausa{dur:.1f}" if t["k"] == "silencio" else f"§hes{dur:.1f}")


def _chave_superficie(conteudo):
    m = RE_PAUSA.match(conteudo.strip())
    if m:
        return f"§pausa{float(m.group(1)):.1f}"
    if " · " in conteudo:
        d = conteudo.rsplit("·", 1)[1].strip().rstrip("s")
        try:
            return f"§hes{float(d):.1f}"
        except ValueError:
            pass
    return chave(conteudo)


def parse_transcript(path):
    """Le o .md editado -> [(chave, estado)] com estado em
    cortar/manter/normal."""
    saida, dentro = [], False
    for ln in path.read_text(encoding="utf-8").splitlines():
        if ln.startswith("## [b"):
            dentro = True
            continue
        if not dentro or ln.startswith(("#", "<!--", "-->", "---")):
            continue
        for m in TOKEN_RE.finditer(ln):
            cortar, manter, cru = m.groups()
            if cortar is not None:
                saida.append((_chave_superficie(cortar), "cortar"))
            elif manter is not None:
                saida.append((_chave_superficie(manter), "manter"))
            else:
                saida.append((chave(cru), "normal"))
    return saida


def selo_stream(stream):
    """Impressao digital do fluxo analisado.

    Contar tokens nao bastava: a analise pode mudar QUAIS trechos saem sem
    mudar QUANTOS tokens existem, e foi exatamente o que aconteceu - o
    render leu um transcript.md velho e reimpos decisoes ja corrigidas."""
    import hashlib
    crua = "|".join(f'{t["k"]}{t["s"]:.2f}{t["e"]:.2f}{int(t["cut"])}'
                    for t in stream)
    return hashlib.sha1(crua.encode("utf-8")).hexdigest()[:10]


def confere_atualidade(md, stream):
    """O transcript.md corresponde ao stream.json atual?

    O cabecalho guarda a contagem de tokens. Se a analise rodou de novo e o
    draft nao, o `reconcilia` reimpoe em silencio as decisoes antigas por
    cima das novas - foi assim que um corte ja consertado reapareceu no
    render. Melhor recusar e mandar rodar `draft` do que editar no escuro."""
    m = re.search(r"selo=([0-9a-f]{10})", md.read_text(encoding="utf-8")[:400])
    if not m:
        return True                      # arquivo antigo, sem selo
    if m.group(1) == selo_stream(stream):
        return True
    print("[aviso] o transcript.md nao corresponde ao stream.json atual: "
          "a analise rodou de novo depois dele.")
    print("[aviso] rode 'draft' antes, ou as edicoes antigas vao valer.")
    return False


def reconcilia(stream, editado):
    """Alinha o fluxo original com o texto editado. O que sumiu do texto
    vira corte; o que voltou para ⟨ ⟩ vira manutencao."""
    import difflib
    ko = [chave_token(t) for t in stream]
    ke = [k for k, _ in editado]
    sm = difflib.SequenceMatcher(None, ko, ke, autojunk=False)
    visto = set()
    for i, j, n in sm.get_matching_blocks():
        for k in range(n):
            estado = editado[j + k][1]
            t = stream[i + k]
            visto.add(i + k)
            if estado == "manter":
                t["cut"], t["why"] = False, "resgatado"
            elif estado == "cortar":
                t["cut"] = True
            else:
                t["cut"], t["why"] = False, ""
    apagados = 0
    for i, t in enumerate(stream):
        if i not in visto:
            t["cut"], t["why"] = True, "apagado no texto"
            apagados += 1
    return apagados

# ------------------------------------------------- camada do editor grafico

def _spans(v, ordena=True):
    """Normaliza uma lista de [ini, fim]: descarta lixo, ordena e funde o
    que se toca. Sem isso, mil ajustes de 1 ms viram mil trechos no
    arquivo.

    LARGURA ZERO NAO E GESTO. `b > a` deixava passar `[37.19, 37.19+5e-15]`
    - ruido de ponto flutuante, nao clique de ninguem. E `aplica_manual`
    parte o trecho mantido em cada corte que recebe, entao cinco desses no
    edicao.json do IMG_0246 estilhacaram "Os Arcanos" em cinco cacos de
    0,10s, cada um com fade de 10 ms na entrada e na saida: metralhadora.
    1 ms e o mesmo limite que `aplica_manual` ja usava na saida - a 48 kHz
    sao 48 amostras, e ninguem arrasta isso com o mouse."""
    out = []
    for x in v or []:
        try:
            a, b = float(x[0]), float(x[1])
        except (TypeError, ValueError, IndexError, KeyError):
            continue
        if b - a > 0.001:
            out.append([a, b])
    if ordena:
        out.sort()
    fund = []
    for a, b in out:
        if fund and a <= fund[-1][1] + 1e-9:
            fund[-1][1] = max(fund[-1][1], b)
        else:
            fund.append([a, b])
    return fund


def carrega_edicao(d):
    """work/<video>/edicao.json - o que o editor grafico decidiu.

    Duas camadas, e a diferenca entre elas e o ponto todo do arquivo.

    `tokens` sao decisoes de PALAVRA, do mesmo tipo que apagar texto no
    transcript.md: passam por toda a geometria de emenda (fusao, snap,
    corte minimo, respeito a palavra vizinha), porque quem clica numa
    palavra quer a palavra fora, nao um numero exato de milissegundos.

    `cortes` e `restauros` sao trechos de TEMPO desenhados na onda. Entram
    DEPOIS de tudo, sem snap e sem minimo: um ajuste de 1 ms nao
    sobreviveria ao filtro de corte minimo, e nao deve mesmo sobreviver a
    ele - quem pediu 1 ms esta olhando a onda e quer 1 ms."""
    p = d / "edicao.json"
    if not p.exists():
        return None
    try:
        ed = jload(p)
    except Exception as e:
        print(f"[editor] edicao.json ilegivel ({e}); ignorando")
        return None
    if not isinstance(ed, dict):
        return None
    ed["tokens"] = ed.get("tokens") or {}
    ed["cortes"] = _spans(ed.get("cortes"))
    ed["restauros"] = _spans(ed.get("restauros"))
    return ed


def aplica_edicao(stream, ed):
    """Camada de palavra. Vence o transcript.md por ser a decisao mais
    recente e a mais explicita - no editor a pessoa clicou naquela palavra;
    no .md ela pode so ter deixado como veio."""
    n = 0
    for k, estado in (ed.get("tokens") or {}).items():
        try:
            t = stream[int(k)]
        except (ValueError, TypeError, IndexError):
            continue
        novo = (estado == "cortar")
        if t["cut"] != novo:
            n += 1
        t["cut"] = novo
        t["why"] = "cortado no editor" if novo else "mantido no editor"
    return n


def aplica_manual(manter, ed, dur):
    """Camada de tempo, aplicada por ultimo e sem arredondar nada.

    Restaura primeiro e corta depois: entre dois gestos que se sobrepoem, o
    corte e a palavra final. Nada aqui passa por snap, fusao ou minimo -
    seria desfazer, no escuro, o ajuste que a pessoa fez olhando a onda."""
    if not ed:
        return manter
    iv = [[a, b] for a, b in manter]
    for a, b in ed.get("restauros") or []:
        iv.append([max(0.0, a), min(dur, b)])
    iv = _spans(iv)
    for ca, cb in ed.get("cortes") or []:
        novo = []
        for a, b in iv:
            if cb <= a or ca >= b:
                novo.append([a, b])
                continue
            if a < ca:
                novo.append([a, ca])
            if cb < b:
                novo.append([cb, b])
        iv = novo
    return [(round(a, 4), round(b, 4)) for a, b in iv if b - a > 0.001]


# -------------------------------------------------------- montagem do corte

def fim_util(t):
    """Ate onde esta palavra e palavra.

    Quando `marca_arrastadas` decide podar a cauda, o que vem depois de
    `cauda[0]` e arrasto - foi essa a decisao. Quem defende palavra mantida
    tem que parar aqui, senao defende tambem o arrasto que se mandou tirar.

    MEDIDO: sem isto, a poda de cauda nunca aconteceu uma vez sequer. Nos
    videos de calibracao eram 10 caudas marcadas (o "Aaas" de 80,0s do
    IMG_0247, os "Eeee" de 44,3s, 216,1s e 481,8s) e 10 desfeitas pela trava
    final - ela via o fim do token, 1,1s adiante, e empurrava a emenda para
    fora da palavra inteira."""
    cauda = t.get("cauda")
    return cauda[0] if cauda else t["e"]


def _tem_fala(stream, a, b):
    """Ha palavra aproveitavel dentro de [a,b]?

    Aproveitavel vai ate `fim_util`: arrasto que se mandou podar nao e
    fala, senao ele segura a fusao dos cortes em volta. MEDIDO: nos tres
    videos de calibracao esta linha nao muda nenhuma emenda - esta assim
    para "ha fala aqui?" ter uma resposta so, aqui e no `_garante_ilhas`."""
    return any(t["k"] == "palavra" and not t["cut"]
               and fim_util(t) > a + 0.02 and t["s"] < b - 0.02
               for t in stream)


def spans_corte(stream, C, anot=None, dur=None):
    """Fluxo -> trechos a REMOVER.

    A versao antiga montava o video somando os pedacos bons, e por isso
    perdia todo o intervalo natural entre palavras: virava um corte a cada
    duas palavras. Aqui e o contrario - parte-se do video inteiro e
    subtrai-se so o que foi marcado. Tudo o que ninguem mandou cortar
    continua contínuo.

    E cada pausa encurtada gera UMA emenda, nao duas: remove-se o MIOLO da
    pausa e deixa-se metade do respiro colado em cada fala vizinha. Antes eu
    guardava uma lasca do meio da pausa, o que punha na tela 0,45s da cabeca
    dela numa posicao sem relacao com nenhum dos lados."""
    cs, cc = C["silencio"], C["corte"]
    alvo = cs["pausa_alvo"]
    out = []
    n = len(stream)

    # 1. RUNS de tokens marcados. Trabalhar por run, e nao token a token,
    #    e o que impede sobrar caco: a borda do corte fica na fronteira
    #    entre o que fica e o que sai, uma vez so por run.
    i = 0
    while i < n:
        t = stream[i]
        if not t["cut"] or t.get("prot"):
            i += 1
            continue
        j = i
        while j + 1 < n and stream[j + 1]["cut"] and not stream[j + 1].get("prot"):
            j += 1
        ini, fim = stream[i], stream[j]

        if ini["k"] == "silencio" and ini["why"] == "abertura":
            a = 0.0
        elif ini["k"] == "silencio":
            a = ini["s"] + alvo / 2
        else:
            a = ini["s"]
        b = fim["e"] - alvo / 2 if fim["k"] == "silencio" else fim["e"]

        # LIMITES: a emenda nao pode passar do fim da palavra mantida
        # anterior nem do inicio da proxima. Sem isso o snap engolia a
        # palavra vizinha inteira ("momen--" + "meira versao").
        la = stream[i - 1]["e"] if i > 0 else 0.0
        lb = stream[j + 1]["s"] if j + 1 < n else (dur or b)
        if b > a:
            out.append([a, b, la, lb])
        i = j + 1

    # 2. Silencio final: depois da ultima palavra nao ha token nenhum, entao
    #    marcar tokens nunca alcanca esse trecho. Era por aqui que passavam
    #    os 15s de ela em silencio, olhando para os lados e saindo de quadro.
    if dur:
        ult = max((t["e"] for t in stream if t["k"] == "palavra"
                   and not t["cut"]), default=None)
        if ult is not None and dur - ult > cs["pausa_intocavel"]:
            out.append([ult + alvo, dur])

    # 3. Caudas de palavra arrastada: a palavra fica, o arrasto sai.
    #    O limite e a propria palavra: a cauda nunca invade a vizinha.
    #    `prot` NAO conta aqui, e isso e proposital: proteger contagem e
    #    impedir que a sequencia seja CORTADA, nao autorizar arrasto. O
    #    "Aaas" de 80,0s do IMG_0247 sobrou inteiro porque uma contagem
    #    inexistente cancelava a poda - em silencio, sem aparecer em lugar
    #    nenhum. Com o RE_NUM corrigido nenhum token protegido dos tres
    #    videos tem cauda, entao hoje isto nao muda uma emenda sequer; fica
    #    para o dia em que a contagem de verdade vier arrastada.
    for k, t in enumerate(stream):
        if t.get("cauda") and not t["cut"]:
            ca, cb = t["cauda"]
            prox = stream[k + 1]["s"] if k + 1 < n else (dur or cb)
            out.append([ca, cb, t["s"], min(cb, prox)])

    # 4. Anotacoes de julgamento entram como TRECHO DE TEMPO, nao como
    #    marcacao de token: valem mesmo onde ninguem fala.
    for a in (anot or []):
        out.append([a["ini"], a["fim"], a["ini"], a["fim"]])

    return out


def _funde(cortes, stream, limiar):
    """Junta cortes vizinhos quando o que sobra entre eles seria uma
    piscada - mas nunca engole fala aproveitavel."""
    cortes = sorted((list(c) for c in cortes if c[1] > c[0]),
                    key=lambda c: c[0])
    fund = []
    for c in cortes:
        a, b = c[0], c[1]
        la = c[2] if len(c) > 2 else a
        lb = c[3] if len(c) > 3 else b
        if fund and a - fund[-1][1] < limiar and not _tem_fala(
                stream, fund[-1][1], a):
            fund[-1][1] = max(fund[-1][1], b)
            fund[-1][3] = max(fund[-1][3], lb)
        else:
            fund.append([a, b, la, lb])
    return fund


def _snap(cortes, db, hop, raio):
    """Pousa cada borda da emenda no instante mais silencioso por perto.

    SIMETRICO. O ponto de menor energia perto da fronteira E o ponto de
    emenda correto - ali nao ha fonema para decepar dos dois lados.

    Ja tentei assimetrico (inicio so para frente, fim so para tras) para
    proteger o "s" final. Deu errado: o inicio andando para frente passou a
    deixar o comeco da palavra cortada audivel ("as rainhas vem depois,
    as--"). Deixar a busca escolher livremente resolve os dois: se o minimo
    esta antes da fronteira, o que se perde do lado mantido e silencio; se
    esta depois, o que sobra do lado cortado tambem e."""
    if db is None or raio <= 0:
        return cortes
    n = len(db)

    def melhor(t, lo, hi):
        lo, hi = max(0.0, lo), max(0.0, hi)
        if hi <= lo:
            return t
        i0, i1 = max(0, int(lo / hop)), min(n, int(hi / hop) + 1)
        return (i0 + int(db[i0:i1].argmin())) * hop if i1 > i0 else t

    saida = []
    for c in cortes:
        a, b = c[0], c[1]
        la = c[2] if len(c) > 2 else a - raio
        lb = c[3] if len(c) > 3 else b + raio
        meio = (a + b) / 2
        # a busca fica presa entre as palavras mantidas vizinhas e nunca
        # ultrapassa o meio do proprio corte
        na = melhor(a, max(a - raio, la), min(a + raio, meio))
        nb = melhor(b, max(b - raio, meio), min(b + raio, lb))
        saida.append([na, nb, la, lb] if nb > na else list(c))
    return saida


def _respeita_palavras(cortes, stream, C):
    """Rede de seguranca final: nenhuma emenda encosta em palavra mantida.

    As etapas anteriores ja tentam isso cada uma a seu modo, e mesmo assim
    escapou "realmente" comecando cortado. Aqui a regra e imposta uma vez so,
    no fim, sobre o resultado de todas elas.

    A regra e SUBTRAIR a palavra do corte, e por isso um corte pode sair
    daqui PARTIDO EM DOIS. Antes ele so sabia encolher para um lado, e
    quando a palavra mantida caia no MEIO do corte o lado de la sobrevivia
    inteiro.

    MEDIDO no IMG_0247: o token "Mae! Mais do..." (49.806-53.078, ela
    falando com outra pessoa) atravessa a palavra "do" (50.640-50.896). O
    corte travava em 50.620 e 2,33s de fala alheia continuavam tocando
    dentro do trecho mantido. Pior: havia uma ANOTACAO de julgamento
    (50.90-53.23) pedindo exatamente aquele trecho - o `_funde` a tinha
    juntado ao token, entao ela morreu junto, sem aparecer em lugar nenhum.
    Era o caminho pelo qual uma decisao humana sumia em silencio."""
    folga = C["corte"]["folga_palavra"]
    pal = [t for t in stream
           if t["k"] == "palavra" and not t["cut"] and not t.get("prot")]
    saida = []
    for c in cortes:
        pedacos = [[c[0], c[1]]]
        for t in pal:
            pa, pb = t["s"] - folga, fim_util(t) + folga
            novo = []
            for a, b in pedacos:
                if pb <= a or pa >= b:
                    novo.append([a, b])
                    continue
                if a < pa:
                    novo.append([a, pa])
                if pb < b:
                    novo.append([pb, b])
            pedacos = novo
        for a, b in pedacos:
            if b - a > 0.05:
                saida.append([a, b] + list(c[2:]))
    return saida


def _garante_ilhas(cortes, minimo, stream):
    """Junta cortes separados por um vao SEM FALA, para nao sobrar piscada.

    Esta trava ja teve um segundo comportamento - alargar a ilha encolhendo
    os cortes vizinhos - que foi removido. Alargar era DESCORTAR material
    que se decidiu cortar, e foi assim que ela comeu 0,09s de
    "metodologias" e entregou "as met--". Se a ilha tem palavra de verdade
    dentro, 0,4s esta bom: e uma palavra, nao um piscar.

    As causas originais do glitch (lasca do meio da pausa, micro-corte de
    0,08s entre palavras) foram resolvidas na estrutura e no alinhamento.
    Sobrou so o caso visual: vao curto e vazio entre dois cortes."""
    cortes = sorted((list(c) for c in cortes), key=lambda c: c[0])
    saida = []
    for c in cortes:
        if not saida:
            saida.append(c)
            continue
        ant = saida[-1]
        folga = c[0] - ant[1]
        if folga <= 0:
            ant[1] = max(ant[1], c[1])
            continue
        # `fim_util` pela mesma razao do `_tem_fala`: a ilha pode conter so
        # a cabeca de uma palavra podada, e e a cabeca que decide.
        mantidas = [t for t in stream if t["k"] == "palavra" and not t["cut"]
                    and fim_util(t) > ant[1] + 0.02 and t["s"] < c[0] - 0.02]
        if folga < minimo and not mantidas:
            ant[1] = max(ant[1], c[1])       # ilha vazia: absorve
            if len(ant) > 3 and len(c) > 3:
                ant[3] = max(ant[3], c[3])
            continue
        saida.append(c)
    return [c for c in saida if c[1] > c[0]]


def intervalos(stream, C, dur, db=None, hop=0.02, anot=None, manual=None):
    """Fluxo -> lista de trechos a MANTER."""
    cc = C["corte"]

    # `minimo` existe contra emenda invisivel: economizar 0,2s nao paga um
    # pulo na tela. Mas ele nao tem o que dizer sobre um trecho que uma
    # PESSOA marcou para sair. O `aplica_manual` ja sabia disso ("nada aqui
    # passa por snap, fusao ou minimo - seria desfazer, no escuro, o ajuste
    # que a pessoa fez"); a anotacao de julgamento e a mesma decisao humana
    # escrita noutro arquivo, e estava sujeita ao filtro.
    #
    # MEDIDO: um "ééé" de 0,35s anotado vira corte de 0,35s, perde 0,06 de
    # folga de cada lado e sai com 0,23 - abaixo de 0,28. A anotacao era
    # lida, aplicada, e descartada aqui sem uma linha de aviso. Terceiro
    # caminho encontrado pelo qual julgamento humano sumia em silencio.
    faixas = [(x["ini"], x["fim"]) for x in (anot or [])]

    def passa(c):
        if c[1] - c[0] >= cc["minimo"]:
            return True
        return any(min(c[1], y) - max(c[0], x) > 0.05 for x, y in faixas)

    cortes = spans_corte(stream, C, anot, dur)
    cortes = _funde(cortes, stream, cc["ilha_minima"])
    cortes = [c for c in cortes if passa(c)]
    cortes = _snap(cortes, db, hop, cc["snap"])
    cortes = _funde(cortes, stream, cc["ilha_minima"])
    # o snap desloca as bordas e pode encolher um corte abaixo do minimo,
    # entao filtra de novo: nenhuma emenda invisivel escapa.
    cortes = [c for c in cortes if passa(c)]
    cortes = _garante_ilhas(cortes, cc["ilha_minima"], stream)
    cortes = _respeita_palavras(cortes, stream, C)
    cortes = [c for c in cortes if passa(c)]

    # Cola nas pontas: um corte que comeca quase no zero tem que comecar NO
    # zero, senao sobra uma lasca de milissegundos antes dele - foi assim
    # que voltou um trecho de 0,08s no inicio do IMG_0248. Idem no fim.
    if cortes:
        if cortes[0][0] < cc["ilha_minima"]:
            cortes[0][0] = 0.0
        if dur - cortes[-1][1] < cc["ilha_minima"]:
            cortes[-1][1] = dur

    manter, t = [], 0.0
    for c in cortes:
        a, b = max(0.0, c[0]), min(dur, c[1])
        if a > t + 0.02:
            manter.append([t, a])
        t = max(t, b)
    if t < dur - 0.02:
        manter.append([t, dur])
    iv = [(round(a, 3), round(b, 3)) for a, b in manter if b - a > 0.05]
    return aplica_manual(iv, manual, dur)


def grafo(iv, C, escala=None, fps=None):
    fade = C["audio"]["fade"]
    L, vs, as_ = [], [], []
    for i, (a, b) in enumerate(iv):
        d = b - a
        L.append(f"[0:v]trim=start={a}:end={b},setpts=PTS-STARTPTS[v{i}];")
        af = (f",afade=t=in:st=0:d={fade}"
              f",afade=t=out:st={max(0, d-fade):.3f}:d={fade}")
        L.append(f"[0:a]atrim=start={a}:end={b},"
                 f"asetpts=PTS-STARTPTS{af}[a{i}];")
        vs.append(f"[v{i}]")
        as_.append(f"[a{i}]")
    pares = "".join(v + a for v, a in zip(vs, as_))
    L.append(f"{pares}concat=n={len(iv)}:v=1:a=1[vc][ao];")
    cadeia = "[vc]"
    filtros = []
    if escala:
        filtros.append(f"scale=-2:{escala}")
    if fps:
        filtros.append(f"fps={fps}")
    filtros.append("null")
    L.append(cadeia + ",".join(filtros) + "[vo]")
    return "\n".join(L)


def _base(video, rotulo, ignorar_edicao=False, aplicar_ed=True):
    """Tudo o que precede o calculo dos trechos: le o fluxo analisado e
    empilha por cima dele as decisoes humanas.

    Devolve as pecas em vez do resultado porque o editor grafico precisa
    delas soltas - ele recalcula os trechos a cada clique, com o estado que
    esta na tela e ainda nao foi salvo, e nao pode reler o disco a cada
    vez. Assim o que se ouve no editor sai da MESMA funcao que o render usa,
    e nao de uma reimplementacao parecida."""
    C = cfg()
    d = wdir(video)
    S = jload(d / "stream.json")
    stream, dur = S["stream"], S["dur"]
    selo = selo_stream(stream)

    md = d / "transcript.md"
    if md.exists() and not ignorar_edicao:
        if not confere_atualidade(md, stream):
            sys.exit(f"[{rotulo}] abortado: transcript.md desatualizado")
        n = reconcilia(stream, parse_transcript(md))
        print(f"[{rotulo}] transcript.md aplicado "
              f"({n} tokens apagados a mao)")
    else:
        print(f"[{rotulo}] usando as propostas automaticas")

    # o editor entra DEPOIS do .md de proposito: e a decisao mais recente
    ed = None if ignorar_edicao else carrega_edicao(d)
    # Selo velho = a analise rodou de novo e os indices de token mudaram de
    # lugar. Aplicar mesmo assim cortaria palavras a esmo. So o editor
    # continua: la o arquivo e reconciliado (a camada de tempo sobrevive, a
    # de palavra nao) e regravado com o selo novo.
    if ed and aplicar_ed and ed.get("selo") and ed["selo"] != selo:
        sys.exit(f"[{rotulo}] abortado: edicao.json e de outra analise. "
                 f"Abra o editor de novo (ele reconcilia) ou apague o "
                 f"arquivo.")
    if ed and aplicar_ed:
        n = aplica_edicao(stream, ed)
        print(f"[{rotulo}] edicao.json aplicado ({n} tokens mudados, "
              f"{len(ed['cortes'])} cortes finos, "
              f"{len(ed['restauros'])} restauros)")

    db = hop = None
    wav = d / "audio.wav"
    if wav.exists():
        db, hop, _ = envelope(wav)
    # relê as anotacoes do disco: editar anotacoes.json e rodar previa
    # deve funcionar sem refazer a analise inteira
    anot = encaixa_anotacoes(stream,
                             carrega_anotacoes(d) or S.get("anotacoes", []))
    if anot:
        print(f"[{rotulo}] {len(anot)} trechos anotados aplicados")
    return C, d, stream, dur, db, hop or 0.02, anot, ed, selo


def confere_pelo_whisper(d, stream, iv, rotulo):
    """Ultima conferencia antes de gastar minutos gerando audio ou video.

    Todo o resto do programa raciocina sobre os tempos do ALINHADOR. Quando
    eles estao errados, o erro e coerente do comeco ao fim: o corte cai onde
    o fluxo mandou, `_respeita_palavras` confere contra os mesmos numeros
    tortos, e ninguem reclama. Foi assim que o corte do IMG_0247 comeu "que
    muitas" - pelo alinhador a frase comecava em 603,3, depois da emenda;
    pelo audio ela comeca em 600,1, dentro dela.

    Aqui a pergunta e feita com a unica medida INDEPENDENTE que existe, a do
    Whisper: alguma palavra marcada para FICAR cai dentro de um pedaco
    removido? Nao conserta nada - so avisa. O conserto e mudar a anotacao, e
    anotacao quem escreve e uma pessoa."""
    p = d / "words.json"
    if not p.exists():
        return 0
    W = jload(p)["words"]
    pal = [t for t in stream if t["k"] == "palavra"]
    if len(W) != len(pal):
        return 0            # sem correspondencia 1:1 nao da para conferir

    fundo = cfg()["alinhamento"]["enterrada_min"]
    suspeitas = []
    for t, w in zip(pal, W):
        if t["cut"]:
            continue
        s0 = w.get("s_whisper", w["s"])
        e0 = w.get("e_whisper", w["e"])
        if e0 - s0 <= 0.02:
            continue
        viva = sum(max(0.0, min(e0, b) - max(s0, a)) for a, b in iv)
        if viva >= (e0 - s0) * 0.5:
            continue
        # Duas coisas diferentes somem de um jeito parecido, e so a segunda
        # e defeito:
        #   RASPADA - a palavra encosta na emenda e perde um pedaco. Os dois
        #     relogios discordam ~0,1s por natureza, entao isso e rotina.
        #   ENTERRADA - a palavra esta segundos DENTRO do buraco, longe de
        #     qualquer emenda. Nenhuma imprecisao de relogio explica isso;
        #     ou o alinhador pos a palavra no lugar errado, ou o corte esta
        #     no lugar errado. Medido no IMG_0247 quebrado, a separacao e
        #     limpa: as raspadas ficam abaixo de 0,55s de fundo e as duas
        #     palavras que a anotacao comeu de verdade, "que muitas", sao a
        #     1a e a 3a mais fundas da lista.
        meio = (s0 + e0) / 2
        if any(a <= meio <= b for a, b in iv):
            continue
        fund = min(min(abs(meio - a), abs(meio - b)) for a, b in iv)
        if fund >= fundo:
            suspeitas.append((fund, viva / (e0 - s0), s0, w["w"]))
    if not suspeitas:
        return 0

    suspeitas.sort(reverse=True)
    print(f"[{rotulo}] AVISO: {len(suspeitas)} palavra(s) que deviam FICAR "
          f"estao enterradas dentro de um corte, pelos tempos do Whisper.")
    print(f"[{rotulo}] o alinhamento desse trecho provavelmente errou. "
          f"Ouça esses pontos antes de aceitar o corte:")
    for fund, frac, s0, txt in suspeitas[:8]:
        print(f"    {s0:8.2f}s  {txt!r}  ({fund:.1f}s dentro do corte, "
              f"sobra {frac:.0%})")
    if len(suspeitas) > 8:
        print(f"    ... e mais {len(suspeitas) - 8}")
    return len(suspeitas)


def _plano(a, rotulo):
    """Etapas comuns a render e previa: aplica a edicao manual e calcula
    os trechos a manter."""
    C, d, stream, dur, db, hop, anot, ed, _ = _base(
        a.video, rotulo, getattr(a, "ignorar_edicao", False))
    iv = intervalos(stream, C, dur, db, hop, anot, ed)
    confere_pelo_whisper(d, stream, iv, rotulo)
    return C, d, stream, dur, iv


def escreve_edl(d, src, iv):
    """EDL do mpv: toca o corte direto da fonte, sem renderizar nada.

    O fim de linha TEM que ser LF - o parser do mpv rejeita o arquivo
    inteiro se encontrar \\r. E o caminho vai com prefixo %N% (N = bytes)
    porque contem ':' e '\\', que sao separadores no formato."""
    q = f"%{len(str(src).encode('utf-8'))}%{src}"
    L = ["# mpv EDL v0"] + [f"{q},{a:.3f},{b - a:.3f}" for a, b in iv]
    p = d / "previa.edl"
    with open(p, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(L) + "\n")
    return p


def fora_de_ordem(st):
    """Quantas palavras comecam ANTES da palavra anterior.

    O alinhador as vezes devolve um trecho com os tempos embaralhados, e ai
    a lacuna `proxima["s"] - atual["e"]` fica NEGATIVA: o detector de pausas
    nao ve pausa nenhuma e a parada longa sai na transcricao como texto
    comum. Hoje essa falha e silenciosa - contar aqui e o que a faz
    aparecer para quem le o revisao.txt."""
    pal = [t for t in st if t["k"] == "palavra"]
    return sum(1 for a, b in zip(pal, pal[1:]) if b["s"] < a["s"])


def rarefeitos(st, cr):
    """Trechos que SOBRAM no corte com fala rarefeita demais.

    Nao depende da lacuna entre palavras - que e justamente o que quebra
    quando o alinhador embaralha os tempos (ver `fora_de_ordem`). Conta
    quantas palavras mantidas tem o PONTO MEDIO dentro de uma janela
    deslizante e divide pelo tempo da janela que sobrevive ao corte.

    Um span marcado para corte que ATRAVESSA palavra mantida nao entra na
    conta do que sai: `_respeita_palavras` vai encolhe-lo ate sumir na hora
    de cortar, entao aquele tempo continua no video apesar do [-N.Ns] que
    aparece na transcricao. Sem essa ressalva o trecho do IMG_0247
    (575-598s: 23s para 16 palavras, 0,70/s contra ~2,5/s no resto do
    video) parecia cortado no papel e passava batido - foi assim que ele
    chegou ao corte final e virou ar morto."""
    pal = [t for t in st if t["k"] == "palavra"]
    if not pal:
        return []
    mantidas = [t for t in pal if not t["cut"]]
    meio = sorted((t["s"] + t["e"]) / 2 for t in mantidas)
    fora = []
    for c in sorted([t["s"], t["e"]] for t in st if t["cut"] and not any(
            k["e"] > t["s"] + 0.02 and k["s"] < t["e"] - 0.02
            for k in mantidas)):
        if fora and c[0] <= fora[-1][1]:
            fora[-1][1] = max(fora[-1][1], c[1])
        else:
            fora.append(c)

    def sobra(a, b):
        return (b - a) - sum(max(0.0, min(b, y) - max(a, x)) for x, y in fora)

    def conta(a, b):
        return bisect.bisect_left(meio, b) - bisect.bisect_left(meio, a)

    ini = min(t["s"] for t in pal)
    fim = max(t["e"] for t in pal)
    achados, t = [], ini
    while t < fim:
        b = min(fim, t + cr["janela"])
        s = sobra(t, b)
        if s >= cr["duracao_minima"] and conta(t, b) / s < cr["palavras_seg"]:
            if achados and t <= achados[-1][1]:
                achados[-1][1] = b
            else:
                achados.append([t, b])
        if b >= fim:
            break
        t += cr["passo"]
    return [(a, b, sobra(a, b), conta(a, b)) for a, b in achados]


def cmd_revisar(a):
    """Escreve o arquivo minimo que o passe de julgamento precisa ler.

    Existe por economia: o passe semantico so precisa do texto do video, e
    nada mais. Rodado dentro de uma conversa longa, ele arrasta junto todo o
    historico a cada resposta. Aqui a entrada fica num arquivo pequeno, para
    a analise acontecer numa sessao limpa.

    Compacta agressivamente: o que ja foi cortado vira um marcador com a
    duracao, em vez de aparecer palavra por palavra. Num video de 10 min isso
    troca ~9 mil tokens por ~2,5 mil, sem perder o que importa para julgar."""
    d = wdir(a.video)
    S = jload(d / "stream.json")
    st, dur = S["stream"], S["dur"]
    raros = rarefeitos(st, cfg()["revisao"])
    bagunca = fora_de_ordem(st)

    L = [f"# {Path(a.video).stem} | {hhmmss(dur)} | tempos em segundos",
         "# [-N.Ns] = ja marcado para corte, trecho quieto. Texto solto =",
         "# sera mantido.",
         "# [-N.Ns VOZ] = marcado para corte MESMO havendo som alto ali",
         "# dentro. O transcritor nao leu, mas o microfone ouviu: pode ser",
         "# frase inteira prestes a sumir. Confira antes de deixar passar.",
         "# [?? N.Ns] = som alto que ninguem conseguiu ler, MANTIDO de",
         "# proposito para voce decidir. Diga se sai ou fica.",
         "# palavra~ = o tempo dessa palavra nao e confiavel (o alinhador",
         "# recusou o proprio resultado e caiu no tempo grosseiro do",
         "# Whisper). NAO ancore borda de corte numa palavra com ~: ela",
         "# pode estar segundos longe de onde aparece aqui.",
         "# [!Ns/N palavras] = o que SOBRA ali e fala rarefeita (o normal e",
         "# ~2,5 palavras/s): parada longa que o detector de pausas nao viu.",
         "# Procure: retomadas com outras palavras, interrupcoes, trechos",
         "# em que ela se perde. Responda com trechos [inicio, fim, motivo]."]
    if bagunca:
        L.append(f"# aviso: {bagunca} palavras com tempo fora de ordem - o "
                 f"alinhador embaralhou")
        L.append("# esses trechos, e ali o [-N.Ns] pode faltar mesmo "
                 "havendo parada.")
    L.append("")

    # ancora cada aviso no primeiro token MANTIDO que cai dentro dele: e o
    # unico ponto do texto corrido onde ele pode aparecer.
    marca = {}
    for x, y, s, n in raros:
        for i, t in enumerate(st):
            if not t["cut"] and t["e"] > x and t["s"] < y:
                marca[i] = f"[!{s:.0f}s/{n} palavras]"
                break

    # Palavras cujo tempo o alinhador recusou. Sem isto o revisao.txt mente
    # com cara de verdade: no IMG_0247 um "As" impresso em 80,0s foi falado
    # em 85,9s, e a anotacao de corte nasceu 6s adiante da frase que queria
    # pegar - foi assim que sobrou o "Aaas" solto aos 20s da previa.
    #
    # So "deriva" e "ordem". O terceiro motivo de recusa, "score", quer
    # dizer que o CTC ficou inseguro e se usou o tempo do Whisper: erro de
    # 0,1s, irrelevante para anotar. Marcar os tres punha ~ em 11% das
    # palavras (92 de 812 no IMG_0247), tres seguidos na mesma linha, e o
    # aviso virava papel de parede. Estes dois sao os que podem estar
    # SEGUNDOS fora do lugar - e a categoria do "As" de 80s.
    W = jload(d / "words.json").get("words", [])
    pal_i = [i for i, t in enumerate(st) if t["k"] == "palavra"]
    duvida = ({pal_i[k] for k, w in enumerate(W)
               if w.get("align_rejeitado") in ("deriva", "ordem")}
              if len(pal_i) == len(W) else set())

    # O microfone e a segunda opiniao sobre o que esta indo embora: um
    # [-1.8s] de silencio e um [-1.8s] de frase eram impressos igual.
    db, hop, _ = envelope(d / "audio.wav")
    limiar = limiar_voz(db, cfg()["silencio"]["limiar_fala_db"])

    linha, ini, cortado, alto = [], None, 0.0, 0.0
    for i, t in enumerate(st):
        if ini is None:
            ini = t["s"]
        if t["cut"]:
            cortado += t["e"] - t["s"]
            # SO conta como VOZ o que foi cortado sem se saber o que era.
            # Anotacao, cacoete do lexico e tentativa detectada foram
            # decididos pelo TEXTO - ter voz dentro e o esperado, e marcar
            # isso fez o VOZ aparecer em 60% das marcas e virar papel de
            # parede. Sobram os dois julgamentos puramente acusticos:
            # "voz sem texto" e "pausa".
            if t["why"].startswith(("voz sem texto", "pausa")):
                frac, pico = mede_voz(db, hop, t["s"], t["e"], limiar)
                if frac > 0.5 and pico - limiar > 25.0:
                    alto += t["e"] - t["s"]
            continue
        if cortado > 0.3:
            linha.append(f"[-{cortado:.1f}s VOZ]" if alto > 0.3
                         else f"[-{cortado:.1f}s]")
        cortado = alto = 0.0
        if i in marca:
            linha.append(marca[i])
        if t["k"] == "palavra":
            linha.append(t["t"] + ("~" if i in duvida else ""))
        else:
            linha.append(f"[{t['t'][:14]}]")
        if len(" ".join(linha)) > 130:
            L.append(f"{ini:6.1f} " + " ".join(linha))
            linha, ini = [], None
    if linha:
        L.append(f"{ini:6.1f} " + " ".join(linha))

    p = d / "revisao.txt"
    p.write_text("\n".join(L) + "\n", encoding="utf-8")
    n = len("\n".join(L))
    print(f"[revisar] {p}  ({n} caracteres, ~{n//4} tokens)")
    print(f"[revisar] escreva o julgamento em {d/'anotacoes.json'}")


def _mapa_previa(iv):
    """Tempo da PREVIA -> tempo da FONTE, e vice-versa.

    O Rafael ouve o .m4a e reclama em "00:14". Todo o resto do programa
    pensa em tempo de fonte. Traduzir isso a mao, um defeito de cada vez,
    foi o trabalho manual de duas rodadas de revisao inteiras."""
    def para_fonte(t):
        p = 0.0
        for a, b in iv:
            if t <= p + (b - a):
                return a + (t - p)
            p += b - a
        return iv[-1][1] if iv else 0.0

    def para_previa(t):
        p = 0.0
        for a, b in iv:
            if t < a:
                return p                    # cai dentro de um corte
            if t <= b:
                return p + (t - a)
            p += b - a
        return p
    return para_fonte, para_previa


def cmd_conferir(a):
    """Confere a GEOMETRIA do corte, nao o texto.

    Existe porque `revisao.txt` mostra o que as palavras DIZEM e nao onde a
    lamina cai. Farelo de palavra, ilha de 0,2s, fala sobrevivendo dentro de
    trecho mantido: nada disso aparece la. Foi assim que seis defeitos
    chegaram ao Rafael de uma vez - nenhum era erro de julgamento de
    conteudo, todos eram de geometria, e a unica rede era o ouvido dele.

    Le pela MESMA funcao que o `audio` e o `render` usam. Ler o
    `graph_audio.txt` seria ler o corte anterior, nao o de agora.

    Cada achado sai nos dois tempos: o da fonte, para anotar, e o da previa,
    para conferir de ouvido sem contar no dedo.

    NAO confere "emenda pousada em ponto alto do envelope". Foi tentado:
    com o piso no percentil 5 e o alarme em 25 dB acima dele, disparava em
    51 das 74 emendas do IMG_0247 e em 51 das 71 do IMG_0248. E esperado -
    a emenda encosta na palavra mantida por construcao, e ali sempre ha voz.
    Alarme que dispara em 70% das emendas e papel de parede. O que aquele
    teste queria pegar - silaba comida - virou a folga de 0,06 no
    config.toml, que ataca a causa em vez do sintoma."""
    C, d, stream, dur, db, hop, anot, ed, selo = _base(a.video, "conferir")
    iv = intervalos(stream, C, dur, db, hop, anot, ed)
    para_fonte, para_previa = _mapa_previa(iv)
    mantido = sum(b - x for x, b in iv)

    if getattr(a, "em", None):
        p = a.em.split(":")
        t = sum(float(x) * 60 ** i for i, x in enumerate(reversed(p)))
        print(f"[conferir] previa {hhmmss(t)} = fonte {para_fonte(t):.2f}s "
              f"({hhmmss(para_fonte(t))})")
        return

    achados = []

    def diz(rot, x, y, txt):
        achados.append((para_previa(x), rot, x, y, txt))

    # 1. Anotacao desobedecida. E o pior de todos: o julgamento humano foi
    #    escrito, foi lido, e sumiu na geometria sem uma linha de aviso.
    for an in (anot or []):
        x, y = an["ini"], an["fim"]
        vivo = sum(max(0.0, min(y, b) - max(x, aa)) for aa, b in iv)
        if vivo > 0.15:
            diz("ANOTACAO NAO OBEDECIDA", x, y,
                f"sobra {vivo:.2f}s de {y-x:.2f}s | "
                f"{str(an.get('motivo', ''))[:50]}")

    # 2. Fala marcada para sair que continua tocando. Silencio nao entra:
    #    metade da pausa fica colada em cada lado de proposito
    #    (pausa_alvo/2), entao ele "sobrevive" sempre, por desenho.
    for t in stream:
        if not t["cut"] or t["k"] == "silencio":
            continue
        vivo = sum(max(0.0, min(t["e"], b) - max(t["s"], aa)) for aa, b in iv)
        if vivo > 0.15:
            diz("FALA CORTADA SOBREVIVE", t["s"], t["e"],
                f"{vivo:.2f}s | {t.get('why','')[:24]} | {t['t'][:30]!r}")

    # 3. Ilha curta. Nem sempre e defeito - uma palavra sozinha entre dois
    #    cortes pode ser exatamente o que a anotacao pediu -, mas e sempre
    #    duas emendas em volta de pouca coisa, e vale um ouvido.
    for x, y in iv:
        if y - x < C["corte"]["ilha_minima"]:
            pals = " ".join(t["t"] for t in stream if t["k"] == "palavra"
                            and not t["cut"] and t["e"] > x and t["s"] < y)
            diz("ILHA CURTA", x, y, f"{y-x:.2f}s | {pals[:40]!r}")

    # 4. Palavra mantida raspada pela borda. Contra os tempos do alinhador
    #    isto tem de dar zero - `_respeita_palavras` garante. Fica como
    #    alarme de regressao: se um dia aparecer, alguem quebrou a trava.
    for t in stream:
        if t["cut"] or t["k"] != "palavra":
            continue
        fe, largura = fim_util(t), fim_util(t) - t["s"]
        if largura <= 0:
            continue
        vivo = sum(max(0.0, min(fe, b) - max(t["s"], aa)) for aa, b in iv)
        if largura - vivo > 0.03 and vivo > 0.02:
            diz("PALAVRA RASPADA", t["s"], fe,
                f"perde {1000*(largura-vivo):.0f}ms | {t['t'][:20]!r}")

    # ORDEM DE ESCUTA, nao ordem de categoria. Quem confere ouve o .m4a do
    # comeco ao fim uma vez so; uma lista agrupada por tipo de defeito obriga
    # a pular para tras seis vezes.
    achados.sort()
    L = [f"# {Path(a.video).stem} | {len(iv)} emendas | "
         f"{hhmmss(mantido)} de {hhmmss(dur)}",
         "# ouca na ordem: a coluna PREVIA e o tempo no .m4a.",
         "# ANOTACAO NAO OBEDECIDA = voce mandou cortar e nao saiu. Sempre bug.",
         "# FALA CORTADA SOBREVIVE = o programa marcou para sair e continua ali.",
         "# ILHA CURTA = pouca coisa entre dois cortes; nem sempre e defeito.",
         "# PALAVRA RASPADA = borda comeu pedaco de palavra mantida.",
         ""]
    for p, rot, x, y, txt in achados:
        L.append(f"{hhmmss(p)}  {rot:24s} fonte {x:8.2f}-{y:<8.2f} {txt}")
    if not achados:
        L.append("nada suspeito")

    p = d / "conferir.txt"
    p.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    print(f"\n[conferir] {len(achados)} pontos para ouvir -> {p}"
          if achados else f"\n[conferir] nada suspeito -> {p}")


def cmd_editor(a):
    """O editor mora em editor.py e so e importado aqui - ele sobe um
    servidor e nao tem por que existir nas outras 99% das rodadas."""
    import editor
    editor.serve(a.video, a.porta, abrir=not a.sem_navegador)


def cmd_previa(a):
    C, d, stream, dur, iv = _plano(a, "previa")
    meta = jload(d / "meta.json")
    edl = escreve_edl(d, meta["src"], iv)
    mantido = sum(b - x for x, b in iv)
    print(f"[previa] {len(iv)} trechos  {hhmmss(mantido)} de {hhmmss(dur)} "
          f"(corta {100*(1-mantido/dur):.0f}%)")
    mpv = _find_mpv()
    if not mpv:
        print(f"[previa] mpv nao encontrado. Abra manualmente: {edl}")
        return
    print(f"[previa] abrindo mpv... (setas = pular, espaco = pausa)")
    subprocess.run([mpv, "--force-window=yes",
                    f"--force-media-title=previa {Path(a.video).name}",
                    str(edl)])


def _find_mpv():
    p = shutil.which("mpv")
    if p:
        return p
    for c in (Path(r"C:\Program Files\MPV Player\mpv.exe"),
              Path(r"C:\Program Files\mpv\mpv.exe")):
        if c.exists():
            return str(c)
    base = Path(os.environ.get("LOCALAPPDATA", ""))
    for c in base.glob("Microsoft/WinGet/Packages/*mpv*/**/mpv.exe"):
        return str(c)
    return None


def _loudnorm(entrada, ca):
    """Mede o loudness e devolve os parametros da segunda passada."""
    med = run([FFMPEG, "-hide_banner", "-nostats", "-i", str(entrada), "-vn",
               "-af", f"loudnorm=I={ca['lufs_alvo']}:TP={ca['true_peak']}:"
                      f"LRA={ca['lra']}:print_format=json",
               "-f", "null", "-"])
    try:
        blob = med.stderr[med.stderr.rindex("{"):]
        M = json.loads(blob[:blob.index("}") + 1])
        return (f":measured_I={M['input_i']}:measured_TP={M['input_tp']}"
                f":measured_LRA={M['input_lra']}"
                f":measured_thresh={M['input_thresh']}"
                f":offset={M['target_offset']}"), M["input_i"]
    except Exception:
        return "", None


def cmd_audio(a):
    """Corte so em audio, para julgar sem esperar render de video.

    O gargalo do render e decodificar 4K60 HEVC 10 bits; com `-vn` o ffmpeg
    nem toca no video. Da para ouvir a edicao inteira em segundos em vez de
    minutos, que e o que torna a calibracao viavel."""
    C, d, stream, dur, iv = _plano(a, "audio")
    meta = jload(d / "meta.json")
    ca = C["audio"]
    fade = ca["fade"]

    L = []
    for i, (x, y) in enumerate(iv):
        w = y - x
        L.append(f"[0:a]atrim=start={x}:end={y},asetpts=PTS-STARTPTS,"
                 f"afade=t=in:st=0:d={fade},"
                 f"afade=t=out:st={max(0, w-fade):.3f}:d={fade}[a{i}];")
    L.append("".join(f"[a{i}]" for i in range(len(iv)))
             + f"concat=n={len(iv)}:v=0:a=1[ac]")
    g = d / "graph_audio.txt"
    with open(g, "w", encoding="utf-8", newline="") as f:
        f.write("\n".join(L))

    saida = Path(a.out) if a.out else (
        Path(meta["src"]).parent / f"{Path(a.video).stem}_previa.m4a")
    bruto = d / "_previa_bruta.wav"
    mantido = sum(y - x for x, y in iv)
    print(f"[audio] {len(iv)} trechos, {hhmmss(mantido)} de {hhmmss(dur)} "
          f"(corta {100*(1-mantido/dur):.0f}%) ...", flush=True)

    # `meta["src"]` e nao `a.video`: todo o resto do programa aceita o nome
    # curto ("IMG_0247"), porque `wdir` resolve. So o ffmpeg precisa do
    # arquivo de verdade, e ele esta anotado no meta.json desde o ingest.
    # Passar `a.video` fazia `audio IMG_0247` morrer em "No such file" depois
    # de ja ter calculado o corte inteiro - e o `previa` ao lado ja fazia
    # certo.
    r = subprocess.run([FFMPEG, "-y", "-v", "error", "-vn",
                        "-i", str(meta["src"]),
                        "-/filter_complex", str(g), "-map", "[ac]",
                        "-c:a", "pcm_s16le", "-ar", "48000", str(bruto)])
    if r.returncode != 0:
        sys.exit("[audio] ffmpeg falhou no corte")

    medido, entrada_lufs = _loudnorm(bruto, ca)
    if entrada_lufs is not None:
        print(f"[audio] {entrada_lufs} LUFS -> {ca['lufs_alvo']} LUFS")
    af = (f"highpass=f={ca['highpass_hz']},"
          f"loudnorm=I={ca['lufs_alvo']}:TP={ca['true_peak']}:"
          f"LRA={ca['lra']}{medido}:linear=true")
    r = subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(bruto),
                        "-af", af, "-c:a", "aac", "-b:a", "192k",
                        "-ar", "48000", str(saida)])
    bruto.unlink(missing_ok=True)
    if r.returncode != 0:
        sys.exit("[audio] ffmpeg falhou na normalizacao")
    print(f"[audio] pronto: {saida}  ({hhmmss(mantido)}, "
          f"{saida.stat().st_size/1e6:.1f} MB)")


def cmd_render(a):
    cr = cfg()["render"]
    C, d, stream, dur, iv = _plano(a, "render")
    meta = jload(d / "meta.json")
    mantido = sum(b - a_ for a_, b in iv)
    print(f"[render] {len(iv)} trechos mantidos  "
          f"{hhmmss(mantido)} de {hhmmss(dur)}  "
          f"(corta {100*(1-mantido/dur):.0f}%)")
    jdump({"manter": iv, "dur_original": dur, "dur_final": mantido},
          d / "cuts.json")
    if a.dry_run:
        print("[render] --dry-run: parei aqui. Veja cuts.json")
        return

    escala = fps = None
    if cr["perfil"] == "1080p30":
        escala, fps = 1080, 30
    gscript = d / "graph.txt"
    gscript.write_text(grafo(iv, C, escala, fps), encoding="utf-8")

    saida = Path(a.out) if a.out else d / f"{Path(a.video).stem}_cortado.mov"
    corte = d / "_corte.mov"
    hevc = "hevc" in meta.get("vcodec", "")
    enc = ["-c:v", "hevc_qsv" if hevc else "h264_qsv",
           "-global_quality", str(cr["qualidade"])]
    if not cr["gpu"]:
        enc = ["-c:v", "libx265" if hevc else "libx264",
               "-crf", str(cr["qualidade"]), "-preset", "medium"]
    cor = ["-color_primaries", "bt2020", "-color_trc", "arib-std-b67",
           "-colorspace", "bt2020nc", "-tag:v", "hvc1"] if hevc else []

    dec = ["-hwaccel", "qsv"] if cr["gpu"] else []
    cmd = ([FFMPEG, "-y", "-v", "error", "-stats"] + dec
           # "-/filter_complex arquivo" le o grafo de um arquivo. Necessario:
           # sao centenas de emendas, nao cabem na linha de comando. (O antigo
           # -filter_complex_script saiu no ffmpeg 9.)
           # `meta["src"]`, nao `a.video`: ver a mesma nota no `cmd_audio`.
           + ["-i", str(meta["src"]), "-/filter_complex", str(gscript),
              "-map", "[vo]", "-map", "[ao]"] + enc + cor
           + ["-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(corte)])
    print(f"[render] cortando ({len(iv)} emendas)...", flush=True)
    r = subprocess.run(cmd)
    if r.returncode != 0 or not corte.exists():
        sys.exit("[render] ffmpeg falhou no corte")

    # ---- audio: loudnorm de duas passadas -----------------------------
    ca = C["audio"]
    print("[render] medindo loudness...", flush=True)
    med = run([FFMPEG, "-hide_banner", "-nostats", "-i", str(corte), "-vn",
               "-af", f"loudnorm=I={ca['lufs_alvo']}:TP={ca['true_peak']}:"
                      f"LRA={ca['lra']}:print_format=json",
               "-f", "null", "-"])
    try:
        blob = med.stderr[med.stderr.rindex("{"):]
        blob = blob[:blob.index("}") + 1]
        M = json.loads(blob)
        medido = (f":measured_I={M['input_i']}:measured_TP={M['input_tp']}"
                  f":measured_LRA={M['input_lra']}"
                  f":measured_thresh={M['input_thresh']}"
                  f":offset={M['target_offset']}")
        print(f"[render] entrada {M['input_i']} LUFS -> "
              f"alvo {ca['lufs_alvo']} LUFS")
    except Exception:
        medido = ""
        print("[render] medicao falhou; loudnorm em passada unica")

    af = (f"highpass=f={ca['highpass_hz']},"
          f"loudnorm=I={ca['lufs_alvo']}:TP={ca['true_peak']}:"
          f"LRA={ca['lra']}{medido}:linear=true")
    print("[render] normalizando audio...", flush=True)
    r = subprocess.run([FFMPEG, "-y", "-v", "error", "-stats", "-i",
                        str(corte), "-c:v", "copy", "-af", af,
                        "-c:a", "aac", "-b:a", "192k", "-ar", "48000", str(saida)])
    if r.returncode != 0:
        sys.exit("[render] ffmpeg falhou no audio")
    corte.unlink(missing_ok=True)
    mb = saida.stat().st_size / 1e6
    print(f"[render] pronto: {saida}  ({hhmmss(mantido)}, {mb:.0f} MB)")

# ---------------------------------------------------------------------- lote

EXTS = {".mov", ".mp4", ".mkv", ".m4v", ".avi"}


def cmd_lote(a):
    """Processa uma pasta inteira. Pensado para rodar de madrugada:
    'ate=draft' transcreve e analisa tudo, deixando os transcript.md
    prontos para revisao. O render fica para depois, ja com as edicoes."""
    pasta = Path(a.pasta)
    vids = sorted(p for p in pasta.iterdir()
                  if p.suffix.lower() in EXTS and p.is_file())
    if a.pular:
        pad = [x.strip().lower() for x in a.pular.split(",")]
        vids = [v for v in vids
                if not any(p in v.name.lower() for p in pad)]
    if not vids:
        sys.exit(f"[lote] nenhum video em {pasta}")

    etapas = ["ingest", "transcribe", "align", "analyze", "draft", "render"]
    ate = etapas.index(a.ate)
    print(f"[lote] {len(vids)} videos, ate a etapa '{a.ate}'\n")
    falhas = []
    for i, v in enumerate(vids, 1):
        print(f"{'='*70}\n[lote] {i}/{len(vids)}  {v.name}\n{'='*70}",
              flush=True)
        ns = argparse.Namespace(video=str(v), force=a.force, model=a.model,
                                out=None, dry_run=False,
                                ignorar_edicao=a.ignorar_edicao)
        try:
            for nome, fn in [("ingest", cmd_ingest),
                             ("transcribe", cmd_transcribe),
                             ("align", cmd_align), ("analyze", cmd_analyze),
                             ("draft", cmd_draft),
                             ("render", cmd_render)][:ate + 1]:
                fn(ns)
        except SystemExit as e:
            print(f"[lote] FALHOU em {v.name}: {e}")
            falhas.append(v.name)
        except Exception as e:
            print(f"[lote] FALHOU em {v.name}: {type(e).__name__}: {e}")
            falhas.append(v.name)
        print()
    print(f"{'='*70}\n[lote] concluido. "
          f"{len(vids)-len(falhas)}/{len(vids)} ok")
    if falhas:
        print("[lote] falhas: " + ", ".join(falhas))

# --------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser(prog="tarocut")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for nome, fn in [("ingest", cmd_ingest), ("transcribe", cmd_transcribe),
                     ("align", cmd_align), ("analyze", cmd_analyze),
                     ("draft", cmd_draft)]:
        p = sub.add_parser(nome)
        p.add_argument("video")
        p.add_argument("--force", action="store_true")
        p.add_argument("--model", default=None)
        p.set_defaults(fn=fn)
    rv = sub.add_parser("revisar",
                        help="escreve revisao.txt para o passe de julgamento")
    rv.add_argument("video")
    rv.set_defaults(fn=cmd_revisar)

    cf = sub.add_parser("conferir",
                        help="confere a geometria do corte sem ouvir")
    cf.add_argument("video")
    cf.add_argument("--em", default=None,
                    help="traduz um tempo da previa (MM:SS) para a fonte")
    cf.add_argument("--ignorar-edicao", action="store_true")
    cf.set_defaults(fn=cmd_conferir)

    au = sub.add_parser("audio", help="corte so em audio, rapido, para julgar")
    au.add_argument("video")
    au.add_argument("--out", default=None)
    au.add_argument("--ignorar-edicao", action="store_true")
    au.set_defaults(fn=cmd_audio)

    ed = sub.add_parser("editor",
                        help="abre o editor de corte no navegador")
    ed.add_argument("video")
    ed.add_argument("--porta", type=int, default=8731)
    ed.add_argument("--sem-navegador", action="store_true")
    ed.set_defaults(fn=cmd_editor)

    v = sub.add_parser("previa", help="ve o corte no mpv sem renderizar")
    v.add_argument("video")
    v.add_argument("--ignorar-edicao", action="store_true")
    v.set_defaults(fn=cmd_previa)

    r = sub.add_parser("render")
    r.add_argument("video")
    r.add_argument("--out", default=None)
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--ignorar-edicao", action="store_true",
                   help="usa so as propostas automaticas, ignora transcript.md")
    r.set_defaults(fn=cmd_render)

    p = sub.add_parser("run")
    p.add_argument("video")
    p.add_argument("--force", action="store_true")
    p.add_argument("--model", default=None)

    def _todos(a):
        cmd_ingest(a)
        cmd_transcribe(a)
        cmd_align(a)
        cmd_analyze(a)
        cmd_draft(a)
    p.set_defaults(fn=_todos)

    b = sub.add_parser("lote", help="processa uma pasta inteira")
    b.add_argument("pasta")
    b.add_argument("--ate", default="draft",
                   choices=["ingest", "transcribe", "align", "analyze",
                            "draft", "render"],
                   help="etapa final (padrao: draft, para revisar antes)")
    b.add_argument("--pular", default=None,
                   help="ignora arquivos cujo nome contenha estes trechos, "
                        "separados por virgula")
    b.add_argument("--force", action="store_true")
    b.add_argument("--model", default=None)
    b.add_argument("--ignorar-edicao", action="store_true")
    b.set_defaults(fn=cmd_lote)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
