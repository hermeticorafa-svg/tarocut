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
  editor      ajusta o corte pela transcricao, no navegador
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
    """Tem letra repetida 3+ vezes? ('queeee', 'ééé', 'naaao')

    ACHADO NAO CONSERTADO (04/09/2026), medido nos 7 fluxos de work/: o
    `.` das reticencias tambem e um caractere repetido 3 vezes, e e ele
    que dispara 7 dos 8 cortes por `alongamento` do corpus - "ehm...",
    "as...", "mais...", "são...", "a...", "ás...". So UM veio de letra
    esticada de verdade (o "ééé" de 20,5s do "as de espadas"). Ou seja: a
    regra que este docstring descreve quase nunca e a regra que roda, e
    ela apaga palavra de conteudo ("mais", "são") por causa de como o
    Whisper pontuou.

    Nao foi mexido aqui de proposito: e decisao do `analyze`, entao mudar
    a exige re-analisar todo video: os `transcript.md` perdem o selo e a
    camada de texto apagado a mao se perde junto. E as tres palavras que
    voltariam no IMG_0248 mudam um video de calibracao - isso quer ouvido,
    nao regex."""
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


def letras_uma_vez(txt):
    """`letras_faladas` contando corrida de letra repetida como UM som.

    "ééééé..." com 200 caracteres nao sao 200 letras faladas: e UMA vogal
    sustentada, e 20,5s dela e o tempo de um som so. Sem isto qualquer
    medida de tempo contra texto se deixa enganar pelo proprio texto que
    esta julgando - o "éé..." de 20,5s do "as de espadas" dava 9,7
    caracteres por segundo, ritmo de fala fluente, e nada tinha o que
    estranhar num corte de 27,5s.

    Fica SEPARADA de `letras_faladas` de proposito, e nao e um conserto
    dela: aquela decide CORTE (a cauda que `marca_arrastadas` poda) dentro
    do `analyze`, esta decide PERGUNTA. Trocar a de baixo mudaria o corte
    de todo video ja analisado, e isso e medicao que ninguem fez."""
    return letras_faladas(RE_ALONGAMENTO.sub(lambda m: m.group(1), txt))


def ritmo_mediano(st):
    """Segundos por letra falada NESTE video, ou None se ha pouca palavra.

    Cada pessoa fala num ritmo, e a mediana se adapta sozinha. Mora aqui,
    e nao dentro de `marca_arrastadas`, porque duas partes precisam dela:
    a poda de cauda, que corta, e a pendencia `texto nao explica o tempo`,
    que pergunta. Duas medianas calculadas em dois lugares seriam dois
    ritmos diferentes para o mesmo video."""
    import statistics
    pal = [t for t in st if t["k"] == "palavra" and chave(t["t"])]
    if len(pal) < 20:
        return None
    return statistics.median((t["e"] - t["s"]) / letras_faladas(t["t"])
                             for t in pal)


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


def classifica_fala(txt, dur, sempre, ch):
    """O texto recuperado num trecho e cacoete ou e fala? -> (cortar, motivo)

    Usada por TODOS os caminhos que recuperam texto de um trecho: lacuna
    entre palavras, excesso de palavra com duracao impossivel, e o que
    vier depois.

    `ch` e o bloco [hesitacao] inteiro, e nao so a densidade: os outros
    dois numeros desta funcao - quantas palavras ainda cabem num cacoete e
    o que conta como palavra curta - moravam escritos no braco aqui, e
    numero que decide corte nao mora no codigo."""
    if not txt or not chave(txt):
        return True, "sem texto"
    dens = densidade_fala(txt, dur)
    if dens < ch["densidade_minima"]:
        return True, f"nao e fala ({dens:.1f} car/s)"
    toks = [chave_ac(w) for w in txt.split() if chave_ac(w)]
    if (len(toks) <= ch["recuperado_max_palavras"]
            and all(t in sempre or len(t) <= ch["palavra_curta_max"]
                    or eh_alongada(t) for t in toks)):
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


GUIA = ROOT / "material" / "guia-taro-de-yala.md"
RE_PALAVRA = re.compile(r"[A-Za-zÀ-ÿ][A-Za-zÀ-ÿ'-]*")


def carrega_vocabulario(d):
    """Todas as palavras que se sabe existirem: as que a propria aula usa,
    as do guia do Yala e as do lexico.

    Serve para UMA pergunta, e so no relatorio: quando o Whisper
    re-transcreve um trecho isolado e devolve texto, esse texto e uma
    leitura de confianca baixa. Se a leitura inventa uma palavra que nao
    existe em lugar nenhum do material do curso nem no resto da propria
    aula, quase sempre nao havia palavra ali - havia bocejo, resmungo ou
    eco de microfone.

    MEDIDO nos 10 fluxos de work/: dos tokens recuperados que o programa
    MANTEM, so quatro tem palavra fora deste vocabulario, e os quatro sao
    exatamente os suspeitos ja conhecidos - "despeçoso" (o bocejo do 7 de
    espadas, relatado como "ai que horror"), "Disse percebe", "Núrope" e
    o par disputado "Os pardes"/"Os pássaros". Nenhuma frase real da
    lista. Nos tokens ja cortados a taxa de alarme e alta (14%), e por
    isso a marca so vale para o que FICA: o que ja vai sair nao precisa
    de segunda opiniao.

    Isto NAO corta nada e nao pode cortar: um dicionario de 3 mil palavras
    tirado de um livro e da fala de dez videos nao e o portugues. E uma
    seta para o passe de julgamento olhar, do mesmo tipo que o [??]."""
    vocab = set()
    for arq in (GUIA, ROOT / "lexicon" / "terms.txt",
                ROOT / "lexicon" / "fillers.txt",
                ROOT / "lexicon" / "marcadores.txt"):
        if arq.exists():
            for w in RE_PALAVRA.findall(arq.read_text(encoding="utf-8")):
                k = chave(w)
                if k:
                    vocab.add(k)
    wp = d / "words.json"
    if wp.exists():
        for w in jload(wp).get("words", []):
            k = chave(w["w"])
            if k:
                vocab.add(k)
    return vocab


def fora_do_vocabulario(txt, vocab):
    """As palavras de `txt` que ninguem mais no material usa. Ignora as
    curtas: com 1-3 letras a chance de ser sigla, onomatopeia ou pedaco de
    palavra e alta demais para o aviso valer alguma coisa."""
    return [w for w in txt.split()
            if len(chave(w)) > 3 and chave(w) not in vocab]


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
    # As outras etapas aceitam o nome curto ("IMG_0247") porque o `wdir`
    # resolve. Esta nao: ela e quem escreve o meta.json com o caminho de
    # verdade. Sem esta linha, o nome curto derrubava o ffprobe e saia um
    # traceback com KeyError: 'streams'.
    if not Path(a.video).exists():
        sys.exit(f"[ingest] nao achei {a.video} - esta etapa precisa do "
                 f"caminho do arquivo, nao do nome curto.")
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
    segs, _ = m.transcribe(
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

def resolve_suspeitos(stream, escuta, ch, sempre, db, hop, limiar):
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
    for t in stream:
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
            cortar, why = classifica_fala(txt, b - a, sempre, ch)
            rot = txt        # o eco e decidido depois, por marca_ecos
        novo = {"k": "hesitacao", "t": rot, "s": round(a, 3),
                "e": round(b, 3), "cut": cortar, "why": why}
        if mot:
            # O QUE O WHISPER LEU antes de a recusa apagar a leitura. Fica
            # gravado porque o editor mostra isso no vao e oferece como
            # ponto de partida para escrever - sem esta linha, tudo o que
            # sobrava de uma leitura recusada aqui era o motivo da recusa.
            # As lacunas guardam o equivalente em `lacunas[].bruto`; estes
            # tokens nascem DEPOIS de elas serem medidas e nao tinham onde.
            # Nao entra no `selo_stream` (so k/s/e/cut), entao nao invalida
            # transcript.md nenhum.
            novo["ouviu"], novo["recusa"] = txt, mot
        novos.append(novo)
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


def marca_instrucoes(stream, marcadores, ci):
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
    respiro, max_volta = ci["respiro"], ci["volta_max"]
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


def marca_ecos(stream, janela):
    """Trecho recuperado que REPETE uma palavra vizinha e retomada.

    Passa sobre o fluxo inteiro, uma vez, para todo trecho de texto
    recuperado - venha ele de lacuna entre palavras ou de palavra com
    duracao impossivel. A regra ja existiu so num dos dois caminhos, e pelo
    outro escapava "como se [como seja...] existisse uma tradicao".

    `janela` (quantos tokens de cada lado contam como "vizinho") vem do
    config: e ela que decide se um trecho recuperado sai ou fica, e numero
    que decide corte nao mora no codigo."""
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


def proxima_audivel(stream, i):
    """Indice da proxima palavra que a pessoa VAI OUVIR depois de `i`.

    Nao e `stream[i + 1]`: entre duas palavras cabem pausa e hesitacao ja
    marcadas para sair, e o que some nao separa nada para quem escuta. Foi
    por olhar so o vizinho imediato que sobreviveram "no ... no"
    (IMG_0248, 488,0s) e "um, ... um" (as de espadas, 557,3s) - entre as
    duas metades da gagueira havia um token cortado, e a regra concluia
    que nao eram vizinhas.

    Devolve None quando o que vem antes da proxima palavra FICA (pausa ou
    hesitacao mantida) ou quando a propria palavra seguinte ja esta
    cortada: nos dois casos elas nao ficam vizinhas no corte, e a pergunta
    nao se aplica."""
    j = i + 1
    while j < len(stream):
        t = stream[j]
        if t["k"] == "palavra":
            return None if t["cut"] else j
        if not t["cut"]:
            return None
        j += 1
    return None


def marca_cacoetes(stream, sempre, contextual, cc):
    """Marca cacoetes. Os ambiguos (é, e, então, né...) so caem quando
    estao ILHADOS entre pausas - senao cortariamos a conjuncao 'e' em
    'cavaleiros e cavaleiras' e a frase desmontaria.

    Duas passadas, e a ordem importa: a gaguejada pergunta qual e a
    proxima palavra AUDIVEL, e so depois da primeira passada as muletas
    do meio ja estao marcadas para sair."""
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
        elif k in contextual and ant is not None and pro is not None                 and _e_lacuna(ant) and _e_lacuna(pro):
            t["cut"], t["why"] = True, "muleta isolada"
        if t["cut"]:
            n += 1

    # Palavra identica repetida em sequencia ("cavaleiras, cavaleiras") e
    # gagueira, nao escolha. Corta a primeira e deixa a segunda; ENFASE
    # protege o uso retorico ("nao, nao").
    #
    # A comparacao e por `chave_ac`, COM acento. Sem isso "e é" (conjuncao
    # mais verbo) vira repeticao e o programa come a conjuncao - e o mesmo
    # motivo pelo qual `chave_ac` existe.
    #
    # NAO ha piso de tamanho de palavra. Ja houve (`len > 2`) e ele nao
    # protegia nada: as gagueiras que sobravam nos videos de calibracao
    # eram justamente "de de" (7 de espadas, 179,1s), "no no" (IMG_0248,
    # 488,0s), "um, um" (as de espadas, 557,3s) e "ou ou" (IMG_0247,
    # 355,9s), todas de uma ou duas letras. Quem faz o trabalho que o piso
    # fazia mal sao o acento e o [cacoete].gaguejada_max do config.
    #
    # MEDIDO nos 10 fluxos de work/: corta 6 tokens novos no total, e os 6
    # sao gagueira de verdade - nenhuma palavra de conteudo na lista.
    for i, t in enumerate(stream):
        if t["k"] != "palavra" or t["cut"] or not chave_ac(t["t"]):
            continue
        j = proxima_audivel(stream, i)
        if j is None or chave_ac(stream[j]["t"]) != chave_ac(t["t"]):
            continue
        if (chave(t["t"]) in ENFASE
                or stream[j]["s"] - t["e"] > cc["gaguejada_max"]):
            continue
        t["cut"], t["why"] = True, "gaguejada"
        n += 1
    return n

# ------------------------------------------ marcacao: tentativas de fala

def detecta_tentativas(stream, ct, min_ngram=None, respiro_gap=None):
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
    resolvido lendo o texto (comando `revisar`).

    `ct` e o bloco [tentativa] do config.toml - os quatro numeros moravam
    aqui como default de parametro, que e um segundo lugar decidindo gosto
    de corte. `min_ngram` sobrescreve o do config e existe por um motivo
    so: o `revisar` roda esta mesma varredura mais frouxa para PERGUNTAR,
    sem cortar nada (ver `cmd_revisar`).

    `respiro_gap` existe pelo mesmo motivo, e fecha um buraco que o
    `min_ngram` sozinho nao alcanca. Os filtros 1 e 3 procuram o respiro
    como TOKEN de lacuna, e pausa menor que `pausa_intocavel` (0,75s) nunca
    vira token - nao ha o que encurtar, entao o `analyze` nao registra
    nada. Para uma retomada refeita depressa, os dois filtros veem a
    repeticao grudada no meio da sentenca: a forma exata do paralelismo
    didatico que eles existem para proteger. Baixar `min_ngram` nao ajuda,
    porque o que barra nao e o tamanho da repeticao - MEDIDO no "3 de
    espadas", "que o amor e uma ferramenta para / que o amor e um caminho
    para" repete QUATRO palavras (acima do limiar de corte) e mesmo assim
    nao virou nem pergunta, separada por 0,50s de respiro.

    Com `respiro_gap`, o filtro 1 tambem aceita o respiro medido em TEMPO
    entre as duas palavras, e o filtro 3 aceita esse mesmo respiro como o
    ponto onde ela travou. MEDIDO nos 11 fluxos de work/: 8 cadeias novas,
    das quais 5 sao retomada de verdade - entre elas "nao e sempre que a
    saida para um problema e bater o peito, matar... nao e sempre que a
    saida para um problema de dificuldade e matar o problema no peito" (9
    palavras identicas, a primeira abandonada no meio) e "Em Yala, todas
    as... [0,74s] em Yala, todas as rainhas" do IMG_0247, que o CLAUDE.md
    ja registrava como retomada escapada: ela passa um centesimo abaixo do
    `pausa_intocavel`. Duas sao ANAFORA ("a gente tem os problemas, a gente
    tem as dificuldades") e uma e duvidosa.

    5 em 8 e bom para PERGUNTAR e pessimo para cortar, e por isso so o
    `cmd_revisar` passa este parametro. Cortar as duas anaforas seria o
    pior defeito deste programa."""
    min_ngram = ct["min_ngram"] if min_ngram is None else min_ngram
    janela_s, max_span = ct["janela"], ct["span_max"]
    max_palavras = ct["palavras_max"]
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
            if ant is None:
                continue
            por_gap = False
            if not _e_lacuna(ant):
                # Sem token de lacuna, o respiro so existe como tempo entre
                # as duas palavras. Quem corta nao olha para ele (ver o
                # docstring); quem pergunta, sim - e paga mais caro pelo
                # texto, porque respiro medido em tempo e evidencia mais
                # fraca que um token: aqui vale o `min_ngram` do CORTE,
                # nunca o do aviso. MEDIDO nos 11 fluxos: com o limiar do
                # aviso (2) isto traz +16 perguntas, e as que ele acrescenta
                # sao anafora ("nao e ignorar a dificuldade, nao e tipo
                # assim reprimir", "de uma passagem, de uma transicao") -
                # 5,4 perguntas por video, acima do teto de 4 que o config
                # registra. Com o limiar do corte sao +7, e 4 delas sao
                # retomada de verdade.
                if (respiro_gap is None or m < ct["min_ngram"]
                        or stream[idx[b]]["s"] - ant["e"] < respiro_gap):
                    continue
                por_gap = True
            achou = (b, m, por_gap)
            break
        if not achou:
            continue
        b, m, por_gap = achou
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
        # filtro 3: precisa existir o ponto onde ela travou. Quando o
        # filtro 1 achou o respiro pelo tempo, ele JA e esse ponto - e o
        # unico que existe, porque token de lacuna ali nao ha nenhum.
        if not por_gap and not any(_e_lacuna(stream[j])
                                   for j in range(ini, fim)):
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
    return classifica_fala(txt, lac["dur"], sempre, ch)


def carrega_respostas(d):
    """work/<video>/respostas.json - o que o passe de julgamento DECIDIU
    sobre cada pendencia do revisao.txt.

    Irmao do `anotacoes.json`, e pela mesma razao: a anotacao registra o
    que sai, e esta lista registra o que FICA e por que. Sem ela "responda
    toda pendencia" era regra do prompt do /revisar e de mais ninguem - o
    programa listava, numerava e ancorava cada `Pn`, e ninguem conferia se
    alguma tinha ficado sem resposta. Julgamento que ninguem cobra e
    julgamento que um dia nao acontece, e este e o programa em que uma
    pendencia sem resposta ja chegou ao Rafael como bocejo no audio.

        [{"em": 305.04, "resposta": "fica", "nota": "por que"}]

    `em` e o TEMPO da pendencia, o mesmo numero que o revisao.txt imprime
    na linha `Pn`. E o tempo, e nao o numero `n`, porque numero caduca: se
    uma pendencia do meio da lista some (foi cortada), todas as de baixo
    mudam de numero e as respostas passariam a valer para outra pergunta -
    a mesma armadilha do indice de token que o `confere_montagem` recusa.
    Tempo de palavra nao caduca.

    `em` aceita uma lista quando a mesma decisao vale para varias ("P2, P3
    e P5: som alto que e ela respirando fundo, ficam"), que e como o
    relatorio do /revisar ja pede.

    Recusa em vez de avisar, como o selo do transcript.md: um arquivo de
    julgamento que o programa nao entende e julgamento sumindo em
    silencio, que e o defeito que este arquivo existe para fechar."""
    p = d / "respostas.json"
    if not p.exists():
        return []
    try:
        dados = jload(p)
    except Exception as e:
        sys.exit(f"[respostas] {p} ilegivel ({e}). Conserte o arquivo: e "
                 f"julgamento humano, nao da para ignorar em silencio.")
    ok = []
    for r in dados if isinstance(dados, list) else []:
        em = r.get("em")
        em = em if isinstance(em, (list, tuple)) else [em]
        try:
            tempos = [float(x) for x in em]
        except (TypeError, ValueError):
            sys.exit(f"[respostas] {p}: 'em' tem de ser o tempo da "
                     f"pendencia (numero ou lista de numeros), e veio "
                     f"{r.get('em')!r}.")
        resp = str(r.get("resposta", "")).strip().lower()
        if resp not in ("sai", "fica", "ouvido"):
            sys.exit(f"[respostas] {p}: 'resposta' em {tempos} e "
                     f"{resp!r}; so vale 'sai', 'fica' ou 'ouvido'.")
        ok.append({"em": tempos, "resposta": resp,
                   "nota": str(r.get("nota", ""))})
    return ok


# `why` que so uma pessoa escreve ao MANTER: ela viu o token marcado para
# sair e clicou nele no editor. Manter de olhos abertos e resposta tao
# explicita quanto cortar - cobrar resposta escrita depois disso e
# perguntar duas vezes. Sem isto o `conferir` do IMG_0247 cobrava o
# 'As cartas' de 160,34s, que a pessoa tinha restaurado no editor.
#
# "resgatado" NAO entra, por mais que o nome prometa. Ele vem do
# `reconcilia`, para todo token que o transcript.md traz entre ⟨ ⟩ - e
# `texto_token` imprime ASSIM toda hesitacao e toda pausa MANTIDA, nao so
# as que alguem trouxe de volta. Aceita-lo dava "ja respondida" a todas as
# 8 pendencias do IMG_0247, inclusive as que ninguem tinha olhado.
MAO_MANTEM = ("restaurado no editor",)


def casa_resposta(q, resp, tol=0.05):
    """Qual resposta do `respostas.json` responde esta pendencia (indice na
    lista), ou None.

    Regra de UMA linha, mas mora aqui porque quem pergunta sao dois: o
    `revisar`, que marca na lista o que ja foi respondido, e o
    `pendencias_sem_resposta`, que cobra o resto. Dois lugares casando
    tempo com tolerancia propria seriam duas respostas diferentes para a
    mesma pergunta - o mesmo defeito de ter `minimo` no codigo e
    `fortes_min` no config.

    `tol` = 0,05s casa o tempo escrito a mao com o tempo da pendencia: o
    revisao.txt imprime com duas casas, entao o erro de copia e de
    arredondamento, nao de julgamento. Nao da para afrouxar muito: nos 10
    fluxos de work/ duas pendencias diferentes chegam a ficar a 0,13s uma
    da outra, e uma resposta que casasse com as duas responderia por
    engano uma pergunta que ninguem leu."""
    for k, r in enumerate(resp):
        if any(abs(t - q["s"]) <= tol for t in r["em"]):
            return k
    return None


def pendencias_sem_resposta(pend, resp, iv):
    """A pergunta que fecha o laco: sobrou pendencia sem resposta escrita?

    Mora aqui, uma vez so, do mesmo jeito e pela mesma razao que
    `anotacoes_desobedecidas`: quem pergunta sao o `conferir` (que lista) e
    o `_plano` (que avisa antes de gerar audio ou video). Regra de prompt
    depende de alguem lembrar; isto nao.

    Uma pendencia se responde de tres jeitos, e os tres contam:
      - CORTANDO o trecho - a resposta e o proprio corte, e por isso a
        pendencia que nao sobrevive ao `iv` nao e cobrada. A excecao e a
        `cortada` (`texto nao explica o tempo`), que ja nasce sobre trecho
        cortado: ali o corte nao responde nada, e quem responde e o
        restauro. Ver `parado` abaixo;
      - TRAZENDO de volta a mao um trecho marcado para sair (`MAO_MANTEM`)
        - a pessoa olhou aquele token e disse fica;
      - ESCREVENDO no respostas.json - unico jeito de dizer "fica" ou
        "vale ouvido" sobre o que ninguem tocou, que e o caso em que a
        decisao nao deixa marca em lugar nenhum.

    Escrever "sai" NAO corta nada - o `respostas.json` registra decisao,
    quem executa e a anotacao (ou o transcript.md, ou o editor). Uma
    resposta "sai" sobre trecho que continua tocando por isso nao conta
    como respondida: seria um jeito novinho de julgamento humano sumir em
    silencio, e este programa ja tem quatro. Ela volta com `falta`
    dizendo PENDENCIA NAO OBEDECIDA, irma da anotacao desobedecida.

    Essa cobranca so vale para a pendencia cujo trecho E o que sai
    (`proprio`, as duas de texto recuperado). Numa `retomada?` o trecho
    vai da primeira tentativa ate a versao boa, e cortar a primeira -
    exatamente o que se pede - deixa a segunda tocando: cobrar ali seria
    reclamar de quem obedeceu.

    Devolve (sem_resposta, orfas). Orfa e resposta que nao casa com
    pendencia nenhuma - tempo digitado errado, ou pendencia que ja saiu
    no corte depois de respondida. Nao e erro; e o que se olha quando a
    lista de sem-resposta nao diminui."""
    casadas = set()
    sem = []
    for q in pend:
        vivo = sum(max(0.0, min(q["y"], b) - max(q["x"], a)) for a, b in iv)
        # `cortada` inverte as duas contas acima, e so ela: a pergunta e
        # sobre audio que JA SAI ("o texto que justificou este corte nao
        # explica o tempo dele"), entao cortar nao responde nada - o corte
        # ja estava feito quando a pergunta nasceu. Responde-la e trazer o
        # trecho de volta ou escrever no respostas.json. Sem esta inversao
        # a pendencia nascia e sumia da cobranca no mesmo instante, que e o
        # defeito que o `pendencias_sem_resposta` inteiro existe para
        # fechar.
        # `parado` = ninguem mexeu neste trecho: ele continua tocando (o
        # normal) ou continua cortado (o `cortada`).
        parado = (vivo <= 0.15) if q.get("cortada") else (vivo > 0.15)
        k = casa_resposta(q, resp)
        if k is not None:
            casadas.add(k)
            quer = "fica" if q.get("cortada") else "sai"
            if resp[k]["resposta"] == quer and parado and q.get("proprio"):
                sem.append(dict(q, falta="PENDENCIA NAO OBEDECIDA"))
        elif parado and not q.get("mao"):
            sem.append(dict(q, falta="PENDENCIA SEM RESPOSTA"))
    orfas = [r for k, r in enumerate(resp) if k not in casadas]
    return sem, orfas


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
    ca = C["arrastada"]
    med = ritmo_mediano(stream)
    if med is None:
        return 0
    pal = [t for t in stream if t["k"] == "palavra" and chave(t["t"])]
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


def protege_enumeracoes(stream, cn):
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
       silencio.

    Havia um terceiro, `minimo=3`, default de parametro que ninguem nunca
    passou: comprimento minimo da sequencia contando numero fraco. Era um
    segundo lugar decidindo o que `fortes_min` ja decide - exigir 3
    numeros INEQUIVOCOS ja exige 3 tokens -, e um segundo lugar que o
    config.toml nao alcancava. Baixar `fortes_min` sem saber dele nao
    frouxaria nada, o que e exatamente o tipo de surpresa que faz numero
    de corte morar em um lugar so."""
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
        if fortes >= cn["fortes_min"]:
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


def protege_naolidos(stream):
    """Token `??` e o programa dizendo que NAO SABE o que foi dito ali.

    `parece_frase` e `eh_hesitacao` ja decidiram MANTER esse trecho, e o
    comentario de `parece_frase` promete o motivo com todas as letras: "nao
    se corta no escuro - o trecho fica e aparece como [?? N.Ns] no
    revisao.txt para o passe de julgamento decidir". So que as passadas
    seguintes decidem por TEXTO - tentativa, eco, cacoete, gaguejada -, e
    num `??` texto e exatamente o que nao existe. Elas cortavam por cima da
    promessa.

    E o corte levava a PERGUNTA junto: a lista de pendencias pula token
    cortado (`if t["cut"] or t["k"] != "hesitacao"`), entao o trecho saia do
    video e do revisao.txt na mesma linha. Julgamento humano sumindo em
    silencio, por um caminho novo.

    MEDIDO (04/09/2026) nos 7 fluxos de work/: 20 tokens `??`, UM cortado
    por regra - 669,1s do "as de espadas", 1,13s marcado `tentativa`. Nos
    outros 19 esta guarda nao muda nada. Que ali havia fala de verdade nao e
    suposicao: o wav2vec2 do `align`, decodificado livre em vez de alinhado,
    ouve "...sica, e isso e t...". Foi a UNICA discordancia entre os dois
    reconhecedores nos 114 cortes automaticos de 1s ou mais dos 7 videos -
    nos outros 113 os dois ouvem a mesma vogal arrastada ou o mesmo silencio.

    Nao protege de `ela mandou cortar`: ali quem mandou foi ela, em voz
    alta, e o que ela quer fora pode muito bem ser o que ninguem leu. Nem de
    anotacao - `aplica_anotacoes` corre depois desta linha de proposito."""
    n = 0
    for t in stream:
        if not t["cut"] or not str(t["t"]).startswith("??"):
            continue
        # `ela mandou cortar` e `anotado:` sao GENTE, nao regra: uma
        # e ela em voz alta, a outra o passe de julgamento.
        # `aplica_anotacoes` corre depois desta linha, entao o
        # segundo nunca chega aqui pelo caminho normal - a linha
        # existe para que chamar esta funcao FORA de ordem (num
        # teste, num script de medicao) nao desfaca julgamento
        # humano, que e o defeito que ela propria existe para
        # fechar. Medido: sem ela, rodar a guarda sobre os
        # stream.json prontos de work/ ressuscitava 9 trechos que
        # uma anotacao tinha mandado cortar.
        if str(t["why"]).startswith(("ela mandou cortar",
                                     "anotado:")):
            continue
        t["cut"] = False
        t["why"] = f"protegido: nao lido ({t['why']})"
        n += 1
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

    n_cac = marca_cacoetes(stream, sempre, contextual, C["cacoete"])
    n_arr = marca_arrastadas(stream, sempre, C)
    if n_arr:
        print(f"[analyze] palavras arrastadas: {n_arr}")
    cadeias = detecta_tentativas(stream, C["tentativa"])
    n_tent = marca_tentativas(stream, cadeias)
    n_inst = marca_instrucoes(stream, carrega_marcadores(), C["instrucao"])
    if n_inst:
        print(f"[analyze] marcadores verbais de corte: {n_inst} tokens "
              f"(ela pediu para cortar)")
    n_susp = resolve_suspeitos(stream, escuta, ch, sempre, db, hop, limiar)
    if n_susp:
        print(f"[analyze] trechos de duracao impossivel resolvidos: {n_susp}")
    # o `escuta` acima escreve _clip.wav a cada trecho, e `resolve_suspeitos`
    # chama de novo depois desta linha; apagar antes dela deixava o arquivo
    # para tras em toda pasta de trabalho.
    (d / "_clip.wav").unlink(missing_ok=True)
    n_eco = marca_ecos(stream, C["cacoete"]["eco_janela"])
    if n_eco:
        print(f"[analyze] retomadas colapsadas: {n_eco}")
    n_prot = protege_enumeracoes(stream, C["contagem"])
    n_esc = protege_naolidos(stream)
    anot = encaixa_anotacoes(stream, carrega_anotacoes(d))
    for a in anot:
        if a.get("ajustada"):
            print(f"[analyze] anotacao {a['ajustada']} encaixada em "
                  f"[{a['ini']:.2f}, {a['fim']:.2f}] (fronteira de palavra)")
    n_anot = aplica_anotacoes(stream, anot)
    print(f"[analyze] cacoetes marcados: {n_cac}")
    if n_prot:
        print(f"[analyze] contagens protegidas: {n_prot} tokens preservados")
    if n_esc:
        print(f"[analyze] cortes no escuro desfeitos: {n_esc} trecho(s) "
              f"`??` devolvidos ao video e ao revisao.txt - a regra "
              f"decidia por um texto que nao existe")
    if anot:
        print(f"[analyze] anotacoes de julgamento: {len(anot)} trechos, "
              f"{n_anot} tokens novos")
    print(f"[analyze] cadeias de tentativa: {len(cadeias)} "
          f"({n_tent} tokens, {sum(c['dur'] for c in cadeias):.0f}s)")

    # `ini`/`fim` sao indices no fluxo, e `resolve_suspeitos` inseriu tokens
    # depois que eles foram calculados: gravados, apontariam para o lugar
    # errado. Ja foram usados (por `marca_tentativas`) e nao servem mais.
    jdump({"lacunas": lacunas, "stream": stream,
           "cadeias": [{k: v for k, v in c.items() if k not in ("ini", "fim")}
                       for c in cadeias],
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

    As ancoras `[b###]` sao o nome publico de cada trecho - e por elas que
    se fala de um pedaco do video sem citar tempo. Fica fora do `cmd_draft`
    para que quem precisar da MESMA fatia (uma tela, um relatorio) nao a
    corte a seu modo e de dois nomes ao mesmo trecho."""
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

    Se a analise rodou de novo e o draft nao, o `reconcilia` reimpoe em
    silencio as decisoes antigas por cima das novas - foi assim que um corte
    ja consertado reapareceu no render. Melhor recusar e mandar rodar
    `draft` do que editar no escuro.

    CABECALHO SEM SELO TAMBEM E RECUSA. Aqui havia um `return True` para
    "arquivo antigo, sem selo", e ele custou uma anotacao: o transcript.md
    do IMG_0246 vinha de uma versao anterior do programa (382 tokens contra
    383), passava batido, e o `reconcilia` casava o "e" de 127,93s - que uma
    anotacao mandou cortar - com o "e" seguinte, que fica. Resultado: um
    trecho julgado por uma pessoa voltava ao video sem uma linha de aviso.
    Quarto caminho encontrado para o mesmo defeito. Nao se le cabecalho que
    nao da para conferir.

    O selo cobre o que a contagem de tokens nao cobria: a analise pode mudar
    QUAIS trechos saem sem mudar QUANTOS tokens existem."""
    m = re.search(r"selo=([0-9a-f]{10})", md.read_text(encoding="utf-8")[:400])
    if m and m.group(1) == selo_stream(stream):
        return True
    if not m:
        print("[aviso] o transcript.md nao tem selo: e de uma versao "
              "anterior do programa e nao da para conferir contra a analise "
              "de agora.")
        print("[aviso] rode 'draft' antes (as marcas ⟨ ⟩ que voce tiver "
              "posto a mao se perdem; copie o arquivo se quiser guarda-las).")
        return False
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

# ----------------------------------------------- camada do editor (montagem)

def _spans(v, ordena=True):
    """Normaliza uma lista de [ini, fim]: descarta lixo, ordena e funde o
    que se toca. Sem isso, mil ajustes de 1 ms viram mil trechos no arquivo.

    LARGURA ZERO NAO E GESTO. `b > a` deixava passar `[37.19, 37.19+5e-15]`
    - ruido de ponto flutuante, nao clique de ninguem. E `aplica_manual`
    parte o trecho mantido em cada corte que recebe, entao cinco desses no
    edicao.json do IMG_0246 estilhacaram "Os Arcanos" em cinco cacos de
    0,10s, cada um com fade de 10 ms na entrada e na saida: metralhadora.
    1 ms e o mesmo limite que `aplica_manual` usa na saida - a 48 kHz sao
    48 amostras, e ninguem clica isso."""
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


def uniao_spans(A, B):
    return _spans(list(A or []) + list(B or []))


def subtrai_spans(A, B):
    """A menos B.

    E o que mantem `cortes` e `restauros` DISJUNTOS dentro do montagem.json:
    cada gesto tira do conjunto contrario antes de entrar no seu. Essa
    disjuncao e o motivo de o botao de mais desfazer o de menos - com os
    dois conjuntos se sobrepondo, `aplica_manual` teria de escolher um
    vencedor, e escolheria sempre o mesmo."""
    out = []
    for a, b in _spans(A):
        pedacos = [[a, b]]
        for x, y in _spans(B):
            novo = []
            for p, q in pedacos:
                if y <= p or x >= q:
                    novo.append([p, q])
                    continue
                if p < x:
                    novo.append([p, x])
                if y < q:
                    novo.append([y, q])
            pedacos = novo
        out += [c for c in pedacos if c[1] - c[0] > 0.001]
    return _spans(out)


def lacunas(iv, dur):
    """O complemento de `iv`: o que NAO se ouve.

    O editor raciocina em lacuna, nao em trecho mantido - os botoes de mais
    e menos movem a borda da lacuna colada na selecao, e e so isso que eles
    fazem. Mora aqui, e nao no navegador, pela mesma regra do resto: quem
    responde onde a lamina esta e quem calculou o corte."""
    out, t = [], 0.0
    for a, b in iv:
        if a > t + 1e-9:
            out.append([t, a])
        t = max(t, b)
    if t < dur - 1e-9:
        out.append([t, dur])
    return out


def carrega_montagem(d):
    """work/<video>/montagem.json - o que o editor de transcricao decidiu.

    Duas camadas, e a diferenca entre elas e o ponto todo do arquivo.

    `tokens` sao decisoes de PALAVRA, do mesmo tipo que apagar texto no
    transcript.md: passam por toda a geometria de emenda (fusao, snap, corte
    minimo, respeito a palavra vizinha), porque quem oculta uma palavra quer
    a palavra fora, nao um numero exato de milissegundos.

    `cortes` e `restauros` sao trechos de TEMPO, os botoes de mais e menos.
    Entram DEPOIS de tudo, sem snap e sem minimo: um ajuste de 1 ms nao
    sobreviveria ao filtro de corte minimo, e nao deve mesmo sobreviver a
    ele - quem pediu 1 ms esta olhando a palavra e quer 1 ms.

    NAO se chama `edicao.json` de proposito. Sobraram `edicao.json` em
    `work/` do editor removido em 01/09, com outro formato e decisoes de
    outra epoca; passar a ler aqueles arquivos seria reimpor decisao velha
    em silencio, que e o defeito que este programa mais teve."""
    p = d / "montagem.json"
    if not p.exists():
        return None
    try:
        m = jload(p)
    except Exception as e:
        print(f"[montagem] montagem.json ilegivel ({e}); ignorando")
        return None
    if not isinstance(m, dict):
        return None
    m["tokens"] = [t for t in (m.get("tokens") or []) if isinstance(t, dict)]
    m["cortes"] = _spans(m.get("cortes"))
    m["restauros"] = _spans(m.get("restauros"))
    return m


def confere_montagem(stream, m):
    """As decisoes de palavra ainda apontam para a palavra em que foram
    tomadas? Devolve a lista de problemas, vazia quando esta tudo no lugar.

    Indice de token caduca: basta a analise inserir um token antes e a
    decisao passa a valer para outra palavra - em silencio, que e como
    julgamento humano some neste programa. Por isso cada decisao guarda
    tambem o `s`/`e` do token em que foi tomada: ou ela cai na palavra
    certa, ou o programa para e diz qual e. E a mesma regra do selo do
    transcript.md, so que por palavra, e por isso o remedio tambem e o
    mesmo - recusa, nao aviso."""
    ruins = []
    for dec in (m or {}).get("tokens") or []:
        i = dec.get("i")
        t = stream[i] if isinstance(i, int) and 0 <= i < len(stream) else None
        if t is None:
            ruins.append(f"token {i} nao existe mais "
                         f"({dec.get('t','')!r}, {dec.get('estado','')})")
        elif (abs(t["s"] - dec.get("s", -9)) > 0.01
              or abs(t["e"] - dec.get("e", -9)) > 0.01):
            ruins.append(f"token {i} mudou de lugar: era {dec.get('t','')!r} "
                         f"em {dec.get('s', 0):.2f}s, hoje e {t['t']!r} em "
                         f"{t['s']:.2f}s")
    return ruins


def aplica_montagem(stream, m, rotulo):
    """Camada de palavra. Vence o transcript.md por ser a decisao mais
    recente e a mais explicita - no editor a pessoa clicou naquela palavra;
    no .md ela pode so ter deixado como veio."""
    ruins = confere_montagem(stream, m)
    if ruins:
        print(f"[{rotulo}] o montagem.json e de outra analise:")
        for r in ruins[:6]:
            print(f"    {r}")
        sys.exit(f"[{rotulo}] abortado: as decisoes de palavra do editor "
                 f"cairiam na palavra errada. Abra o editor e decida de "
                 f"novo, ou apague o montagem.json.")
    n = 0
    for dec in m.get("tokens") or []:
        t = stream[dec["i"]]
        novo = (dec.get("estado") == "cortar")
        if t["cut"] != novo:
            n += 1
        t["cut"] = novo
        # `prot` protege enumeracao contra corte AUTOMATICO; nao autoriza
        # sobreviver a um clique. Mesma regra do `aplica_anotacoes`: se uma
        # pessoa decidiu, sai.
        t["prot"] = False
        t["why"] = "oculto no editor" if novo else "restaurado no editor"
    return n


def carrega_correcoes(d):
    """work/<video>/correcoes.json - o que a pessoa corrigiu no TEXTO de
    uma palavra, no editor, para a legenda que uma etapa futura vai gerar.

    Nao e o `montagem.json`: aquele decide se a palavra sai do video; isto
    decide o que ela DIZ. Sao perguntas diferentes, e por isso e um arquivo
    a parte - misturar as duas faria o mesmo campo responder duas coisas,
    que e como uma das duas comeca a ficar esquecida."""
    p = d / "correcoes.json"
    if not p.exists():
        return None
    try:
        c = jload(p)
    except Exception as e:
        print(f"[correcoes] correcoes.json ilegivel ({e}); ignorando")
        return None
    if not isinstance(c, dict):
        return None
    c["itens"] = [x for x in (c.get("itens") or []) if isinstance(x, dict)]
    return c


def confere_correcoes(stream, c):
    """As correcoes de texto ainda apontam para a palavra em que foram
    tomadas? Mesma pergunta do `confere_montagem`, pela mesma razao:
    indice de token caduca quando a analise roda de novo; `s`/`e` do
    token em que a correcao foi feita, nao."""
    ruins = []
    for dec in (c or {}).get("itens") or []:
        i = dec.get("i")
        t = stream[i] if isinstance(i, int) and 0 <= i < len(stream) else None
        if t is None:
            ruins.append(f"token {i} nao existe mais "
                         f"({dec.get('orig','')!r} -> {dec.get('novo','')!r})")
        elif (abs(t["s"] - dec.get("s", -9)) > 0.01
              or abs(t["e"] - dec.get("e", -9)) > 0.01):
            ruins.append(f"token {i} mudou de lugar: a correcao era de "
                         f"{dec.get('orig','')!r} em {dec.get('s', 0):.2f}s, "
                         f"hoje ai esta {t['t']!r} em {t['s']:.2f}s")
    return ruins


def aplica_correcoes(stream, c, rotulo):
    """So troca `t["t"]`. Nunca `cut`, nunca `s`/`e` - texto corrigido nao
    e decisao de corte, e misturar as duas faria uma correcao de
    ortografia acender os alarmes que existem para julgamento de
    conteudo."""
    ruins = confere_correcoes(stream, c)
    if ruins:
        print(f"[{rotulo}] o correcoes.json e de outra analise:")
        for r in ruins[:6]:
            print(f"    {r}")
        sys.exit(f"[{rotulo}] abortado: as correcoes de texto do editor "
                 f"cairiam na palavra errada. Abra o editor e decida de "
                 f"novo, ou apague o correcoes.json.")
    for dec in c.get("itens") or []:
        stream[dec["i"]]["t"] = dec["novo"]
        # quem escreveu este texto foi uma PESSOA, e isso muda quem tem o
        # que perguntar sobre ele (ver `pendencias`). Nao entra no
        # `selo_stream`, que so olha k/s/e/cut.
        stream[dec["i"]]["mao_texto"] = True


def spans_da_mao(stream, m, folga=0.0):
    """Os trechos que a PESSOA mandou ficar no editor: os restauros de
    milissegundo mais o tempo das palavras marcadas para manter.

    Serve para uma coisa so: separar "a anotacao nao foi obedecida" - bug, e
    o pior deste programa - de "eu reabri essa anotacao de proposito, depois
    de ouvir". Sem essa separacao todo ajuste feito no editor acenderia o
    alarme que existe para avisar que o julgamento sumiu, e alarme que
    acende sozinho vira papel de parede.

    A palavra entra alargada de `folga_palavra`, que e EXATAMENTE a regiao
    que o `_respeita_palavras` defende em volta dela. Sem isso a conta nao
    fechava: reabrir o `Yala,` de 13,10-13,52 dentro da anotacao de 0-32,45
    devolvia 0,54s ao video (a palavra mais 0,06 de cada lado), mas so 0,42
    contavam como decisao da pessoa, e o `conferir` gritava NAO OBEDECIDA
    numa anotacao que eu mesmo tinha acabado de reabrir."""
    if not m:
        return []
    fora = [list(x) for x in (m.get("restauros") or [])]
    for dec in m.get("tokens") or []:
        if dec.get("estado") != "manter":
            continue
        try:
            a, b = float(dec["s"]), float(dec["e"])
        except (KeyError, TypeError, ValueError):
            continue
        i = dec.get("i")
        if isinstance(i, int) and 0 <= i < len(stream):
            t = stream[i]
            if abs(t["s"] - a) < 0.01:
                b = fim_util(t)      # cauda podada nao e palavra mantida
        fora.append([a - folga, b + folga])
    return _spans(fora)


def aplica_manual(manter, m, dur):
    """Camada de tempo, aplicada por ultimo e sem arredondar nada.

    Restaura primeiro e corta depois. O editor mantem `cortes` e `restauros`
    disjuntos (ver `subtrai_spans`), entao esta ordem nao decide nada; ela so
    fixa o resultado de um arquivo editado a mao em que os dois se cruzem.

    Nada aqui passa por snap, fusao ou minimo - seria desfazer, no escuro, o
    ajuste de milissegundo que a pessoa fez de proposito."""
    if not m:
        return manter
    iv = [[a, b] for a, b in manter]
    for a, b in m.get("restauros") or []:
        iv.append([max(0.0, a), min(dur, b)])
    iv = subtrai_spans(iv, m.get("cortes") or [])
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
    cs = C["silencio"]
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
    """Rede de seguranca final: nenhuma emenda encosta em palavra mantida
    - nem em token que uma pessoa mandou manter, seja ele qual for.

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
    # Palavra mantida, MAIS o token que uma PESSOA mandou manter, seja
    # qual for o `k`. A lista era so `k == "palavra"`, e por isso o
    # restauro a mao funcionava pela METADE: no "as de espadas" cinco
    # tokens seguidos foram restaurados dentro da mesma anotacao, os
    # quatro que eram palavra voltaram inteiros e o quinto - uma
    # `hesitacao` de 6,06s, que o `resolve_suspeitos` fez de um bloco de
    # audio so - sobrou 0,06s. E os 0,06 nem eram dele: era a folga
    # vazando da borda da palavra vizinha. Cinco cliques iguais, dois
    # destinos, e nada na tela dizendo qual foi qual - julgamento humano
    # sumindo pela metade, que e pior de perceber do que sumir inteiro.
    #
    # A anotacao entra no corte como TRECHO DE TEMPO (passo 4 do
    # `spans_corte`), fora da camada de palavra; este e o unico lugar que
    # escava token mantido de dentro de um trecho de tempo. Enquanto ele
    # so olhasse `k`, restaurar hesitacao ou silencio dentro de anotacao
    # nao podia fazer nada, por mais explicito que fosse o clique.
    #
    # `MAO_MANTEM` e estreito de proposito: e o `why` que SO uma pessoa
    # escreve ao restaurar (ver `aplica_montagem` e `Sessao._aplica`).
    # Proteger `hesitacao`/`silencio` em geral mudaria o corte automatico
    # de todo video; assim so muda onde alguem clicou. MEDIDO nos tres de
    # calibracao: IMG_0246 e IMG_0248 (sem montagem.json) saem byte a
    # byte iguais - 0 emenda, 0 token. O IMG_0247 tem duas `hesitacao`
    # restauradas a mao e muda em 63 ms: 71 emendas viram 70, porque a
    # emenda de 22 ms que sobrava dentro do "Mae! Mais do..." reaberto
    # deixa de existir (menos emenda e melhor), e o `cartas` oculto a mao
    # em 159,57 passa de 19 para 60 ms vivos - a folga de 0,06 que agora
    # defende o "As cartas" reaberto ao lado dele, a mesma conta que ja
    # vale entre duas palavras quaisquer.
    pal = [t for t in stream
           if not t["cut"] and not t.get("prot")
           and (t["k"] == "palavra" or t.get("why") in MAO_MANTEM)]
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


def intervalos(stream, C, dur, db=None, hop=0.02, anot=None,
               manual=None):
    """Fluxo -> lista de trechos a MANTER."""
    cc = C["corte"]

    # `minimo` existe contra emenda invisivel: economizar 0,2s nao paga um
    # pulo na tela. Mas ele nao tem o que dizer sobre um trecho que uma
    # PESSOA marcou para sair: desfazer no escuro o corte que alguem pediu
    # olhando o texto e o mesmo erro que desfazer o ajuste de quem olha a
    # onda.
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


def trecho_audio(i, a, b, fade):
    """Um trecho de audio aparado, com o micro-fade das duas pontas.

    Mora aqui porque o `audio` e o `render` tem de emendar do mesmo jeito: a
    previa em .m4a so vale como julgamento enquanto for exatamente o que o
    .mov vai receber. Eram duas copias identicas desta linha, uma em cada
    comando - o tipo de par que so se descobre desalinhado tarde, ouvindo."""
    return (f"[0:a]atrim=start={a}:end={b},asetpts=PTS-STARTPTS,"
            f"afade=t=in:st=0:d={fade},"
            f"afade=t=out:st={max(0, b - a - fade):.3f}:d={fade}[a{i}];")


def _grafo_corte(iv, C):
    """As linhas de filter_complex que cortam e concatenam - trim de cada
    trecho mantido, emenda em [vc]/[ao]. Extraida de grafo() para
    `grafo_cartas()` (ver 'adicionar cartas' no CLAUDE.md) cortar
    EXATAMENTE igual: as duas nunca podem discordar de onde cai a
    emenda, e ter dois lugares que montam isso e como ter dois lugares
    que decidem [corte] - so se descobre desalinhado tarde, ouvindo."""
    fade = C["audio"]["fade"]
    L, vs, as_ = [], [], []
    for i, (a, b) in enumerate(iv):
        L.append(f"[0:v]trim=start={a}:end={b},setpts=PTS-STARTPTS[v{i}];")
        L.append(trecho_audio(i, a, b, fade))
        vs.append(f"[v{i}]")
        as_.append(f"[a{i}]")
    pares = "".join(v + a for v, a in zip(vs, as_))
    L.append(f"{pares}concat=n={len(iv)}:v=1:a=1[vc][ao];")
    return L


def grafo(iv, C, escala=None, fps=None):
    L = _grafo_corte(iv, C)
    filtros = []
    if escala:
        filtros.append(f"scale=-2:{escala}")
    if fps:
        filtros.append(f"fps={fps}")
    filtros.append("null")
    L.append("[vc]" + ",".join(filtros) + "[vo]")
    return "\n".join(L)


def _janela_layout3(resumo, margem):
    """Janela de crop (fracao 0-1 do quadro original) que cobre o vaivem
    MEDIDO do rosto (rosto.json/resumo.cx_min..cy_max) com folga, para o
    layout 3 (Mariana centralizada, ver overview da feature). Cortar na
    fracao que o EIXO MAIS largo precisa, nos dois eixos, preserva o
    aspecto 16:9 sem distorcer ao escalar de volta: quando o crop tem o
    mesmo aspecto do quadro inteiro, a fracao de largura cortada e igual
    a fracao de altura cortada."""
    cx0, cx1 = resumo.get("cx_min", 0.4), resumo.get("cx_max", 0.6)
    cy0, cy1 = resumo.get("cy_min", 0.3), resumo.get("cy_max", 0.5)
    l = min(1.0, max(cx1 - cx0, cy1 - cy0) + 2 * margem)
    x = min(max(0.0, (cx0 + cx1) / 2 - l / 2), 1 - l)
    y = min(max(0.0, (cy0 + cy1) / 2 - l / 2), 1 - l)
    return x, y, l, l


def grafo_cartas(iv, C, cartas, resumo_rosto, w_fonte, h_fonte,
                  secundarias_imgs, escala=None, fps=None, hw=False):
    """Estende _grafo_corte com o overlay das cartas e o reenquadramento
    da Mariana - so os layouts 1 e 3 (ver escopo combinado no plano:
    zoom e empilhamento ficam de fora por enquanto).

    Geometria (posicao, tamanho) e a MESMA para toda carta, principal ou
    secundaria - so O QUE aparece e QUANDO mudam. Inputs do ffmpeg, por
    convencao de quem chama isto: 0=video, 1=imagem da carta principal,
    2+=uma por carta secundaria DISTINTA, na ordem de `secundarias_imgs`
    (lista de (nome, Path), sem repetir carta).

    Reenquadramento: um `crop` so, com w/h/x/y como expressao de `t` (o
    ffmpeg 9.x reavalia sozinho quadro a quadro - versoes antigas exigiam
    `eval=frame`, que sumiu da lista de opcoes) que alterna entre o
    quadro inteiro (layout 1) e a
    janela de `_janela_layout3` (layout 3) - troca dura, sem pan suave,
    porque so muda quando uma carta secundaria entra ou sai, e isso ja
    tem o fade dela para disfarcar o corte.

    Fade: so a carta PRINCIPAL tem fade suave de entrada (`format=yuva420p`
    + `fade=alpha=1`, uma vez, nunca sai). A secundaria liga/desliga com
    `overlay=...:enable=` (corte seco) - decisao deliberada do checkpoint
    4: primeiro provar o mecanismo (aparece a hora certa, some a hora
    certa), fade macio da secundaria fica para quando isso estiver
    aprovado, junto com a moldura/sombra/legenda que tambem ficaram de
    fora (ver escopo combinado no plano).

    `hw=True`: o [0:v] vem de decode QSV (`-hwaccel qsv` na chamada do
    ffmpeg) e continua em memoria de GPU ate aqui - `crop`/`overlay` nao
    entendem isso, entao esta funcao baixa para memoria normal ANTES do
    primeiro filtro que precisar de pixel de verdade. MEDIDO no
    IMG_0301: sem isto (decode em software) a previa lo-res levava mais
    de 30 min; com isto, minutos. Chamar com `hw` errado do que a chamada
    do ffmpeg realmente usa e erro de pixel format, nao resultado errado
    - por isso `cmd_cartas` gera os dois grafos e usa o que combina com
    cada tentativa (qsv primeiro, software se aquele falhar)."""
    cc = C["cartas"]
    L = _grafo_corte(iv, C)

    # Escalar ANTES do crop/overlay quando for previa: crop+overlay+fade
    # em 3840x2160 nativo, so para escalar para 480p no final, custa muito
    # mais filtragem que fazer a mesma conta ja pequena. Recalcular a
    # geometria da carta e do crop na resolucao de trabalho e a mesma
    # conta de _janela_layout3, so que sobre w_trab/h_trab em vez de
    # w_fonte/h_fonte. Sem `escala` (bake em qualidade final), trabalha
    # direto no nativo, como antes.
    #
    # Com `hw`, o download tem que vir DEPOIS do scale, nao antes: baixar
    # cada quadro em 3840x2160 (p010le, ~24MB/quadro) so para escalar para
    # 480p em seguida MEDIU quase 14 minutos no IMG_0301 - e so
    # transferencia de GPU para CPU de pixel que ia ser jogado fora.
    # `scale_qsv` escala ainda em memoria de GPU; so entao `hwdownload`
    # baixa o quadro ja pequeno. MEDIDO: um trecho de 20s que levava ~15s
    # nao mudou de tempo sensivelmente - a diferenca aparece no video
    # inteiro, onde e a diferenca entre ~14min e ~1min.
    ult_base = "vc"
    w_trab, h_trab = w_fonte, h_fonte
    if escala:
        h_trab = escala
        w_trab = round(w_fonte * escala / h_fonte / 2) * 2
        if hw:
            L.append(f"[vc]scale_qsv=w={w_trab}:h={h_trab},"
                      f"hwdownload,format=p010le" +
                      (f",fps={fps}" if fps else "") + "[vs];")
        else:
            L.append(f"[vc]scale={w_trab}:{h_trab}" +
                      (f",fps={fps}" if fps else "") + "[vs];")
        ult_base = "vs"
    elif hw:
        L.append("[vc]hwdownload,format=p010le[vd];")
        ult_base = "vd"

    x, y, lw, lh = _janela_layout3(resumo_rosto, cc["crop_margem"])
    secs = cartas["secundarias"]

    def _janela_ativa(spans):
        # `between(t\,a\,b)`: a virgula precisa vir escapada mesmo dentro
        # de aspas - e assim que todo exemplo de expressao condicional do
        # ffmpeg com mais de um argumento escreve (o parser do filtro
        # separa por virgula ANTES do eval da expressao entender que ali
        # e argumento de funcao, nao fim de opcao).
        return "+".join(f"between(t\\,{s['ini']:.3f}\\,{s['fim']:.3f})"
                         for s in spans)

    ativo = _janela_ativa(secs) if secs else "0"
    cw, ch = lw * w_trab, lh * h_trab
    cx0, cy0 = x * w_trab, y * h_trab
    L.append(
        f"[{ult_base}]crop=w='if({ativo},{cw:.1f},{w_trab})':"
        f"h='if({ativo},{ch:.1f},{h_trab})':"
        f"x='if({ativo},{cx0:.1f},0)':y='if({ativo},{cy0:.1f},0)'[base];"
    )

    altura_px = max(2, int(h_trab * cc["carta_altura"]))
    margem_px = int(w_trab * cc["carta_margem"])
    y_carta = (h_trab - altura_px) // 2
    fade = cc["fade"]

    L.append(f"[1:v]scale=-2:{altura_px},format=yuva420p,"
             f"fade=t=in:st=0:d={fade}:alpha=1[cartaP];")
    L.append(f"[base][cartaP]overlay=x={margem_px}:y={y_carta}[c0];")

    ult = "c0"
    for i, (nome, _) in enumerate(secundarias_imgs, start=2):
        janela = _janela_ativa([s for s in secs if s["carta"] == nome])
        prox = f"c{i - 1}"
        L.append(f"[{i}:v]scale=-2:{altura_px}[carta{i}];")
        L.append(f"[{ult}][carta{i}]overlay=x=W-{margem_px}-w:"
                  f"y={y_carta}:enable='{janela}'[{prox}];")
        ult = prox

    # escala/fps ja foram aplicados cedo (antes do crop) quando pedidos -
    # so falta rotular a saida.
    L.append(f"[{ult}]null[vo]")
    return "\n".join(L)


def _agrupa_camadas_enumeracao(eventos):
    """Eventos (um por MENCAO) -> camadas de overlay (uma por POSICAO de
    pilha distinta - lado+profundidade+carta). Nao e a mesma coisa: a
    mesma carta (ou categoria) pode voltar a ocupar a mesma posicao de
    pilha numa sessao de enumeracao POSTERIOR (dentro de uma sessao so,
    nunca se repete - profundidade so cresce, nunca volta atras), e cada
    ocorrencia continua precisando do seu proprio fade de saida.

    Por que uma camada por posicao, e nao uma por ocorrencia (a primeira
    versao desta funcao): o `overlay` deste ffmpeg e caro por CAMADA,
    nao so por pixel - MEDIDO no IMG_0246, o unico video de teste deste
    modo: 26 eventos (uma camada cada, sem esta funcao) renderizaram a
    0,6 fps, quase 50 min para 2 min de previa em 360p/15fps. As mesmas
    26 mencoes cabem em so 11 posicoes de pilha distintas nesse video -
    menos camadas, menos overlay, previa em minutos como o resto do
    modo com principal.

    Cada ocorrencia de uma camada precisa "religar" o alfa para 1 antes
    do proprio fade de saida - sem isso, o `fade=t=out` da ocorrencia
    anterior deixaria a imagem transparente para sempre, inclusive na
    ocorrencia seguinte. `grafo_cartas_enumeracao` faz isso encadeando
    um `fade=t=in` bem curto (nao e um fade de estilo, so restaura o
    alfa) antes de cada `fade=t=out` - por isso devolve a lista de
    ocorrencias por camada, nao so a janela unida."""
    camadas, ordem = {}, []
    for ev in eventos:
        chave = (ev["lado"], ev["prof"], ev["carta"])
        if chave not in camadas:
            camadas[chave] = {"lado": ev["lado"], "prof": ev["prof"],
                               "carta": ev["carta"], "ocorrencias": []}
            ordem.append(chave)
        camadas[chave]["ocorrencias"].append(
            {"ini": ev["ini"], "fim": ev["fim"],
             "fade_ini": ev["fade_ini"], "fade_dur": ev["fade_dur"]})
    return [camadas[k] for k in ordem]


def grafo_cartas_enumeracao(iv, C, camadas, janelas_sessao, resumo_rosto,
                              w_fonte, h_fonte, imgs, escala=None, fps=None,
                              hw=False):
    """Como `grafo_cartas()`, mas para o modo `--sem-principal` (layout 4,
    empilhamento - ver `_agrupa_cartas_enumeracao`): sem carta fixa, duas
    pilhas que alternam de lado e crescem a cada mencao nova.

    Correcao do Rafael em 02/09, contra frame de video editado a mao: a
    1a versao empilhava verticalmente com a carta pequena (0,32 de
    altura) para caber 4 sem estourar o topo do quadro - errado. A pilha
    e HORIZONTAL: a carta usa o MESMO tamanho do layout 1 (`cartas.
    carta_altura`, quase borda a borda), e cada mencao nova desliza para
    o CENTRO em vez de subir - a pilha da esquerda cresce a partir da
    borda esquerda, a da direita a partir da borda direita. Altura
    constante (`y_base`), so o X muda com a profundidade.

    `camadas` vem de `_agrupa_camadas_enumeracao` (uma por POSICAO de
    pilha, nao por mencao - ver a nota la sobre por que isso importa
    para o tempo de render). `imgs` e um Path por camada, na MESMA
    ordem de `camadas` - convencao de quem chama isto: input 0=video,
    1..N=uma imagem por camada, nessa ordem.

    `janelas_sessao` e a lista de (inicio, fim) por sessao de enumeracao,
    devolvida por `_agrupa_cartas_enumeracao` - e a janela em que o crop
    do layout 3 (Mariana centralizada) fica ativo, a mesma ideia de
    `grafo_cartas`/`_janela_layout3`, so que aqui uma sessao pode ter
    varias cartas em vez de uma so secundaria.

    Mesmas notas de `hw`/`escala` de `grafo_cartas()` - nao duplicadas
    aqui, ver aquela docstring."""
    cc, ce = C["cartas"], C["cartas"]["enumeracao"]
    L = _grafo_corte(iv, C)

    ult_base = "vc"
    w_trab, h_trab = w_fonte, h_fonte
    if escala:
        h_trab = escala
        w_trab = round(w_fonte * escala / h_fonte / 2) * 2
        if hw:
            L.append(f"[vc]scale_qsv=w={w_trab}:h={h_trab},"
                      f"hwdownload,format=p010le" +
                      (f",fps={fps}" if fps else "") + "[vs];")
        else:
            L.append(f"[vc]scale={w_trab}:{h_trab}" +
                      (f",fps={fps}" if fps else "") + "[vs];")
        ult_base = "vs"
    elif hw:
        L.append("[vc]hwdownload,format=p010le[vd];")
        ult_base = "vd"

    def _janela(a, b):
        return f"between(t\\,{a:.3f}\\,{b:.3f})"

    x, y, lw, lh = _janela_layout3(resumo_rosto, cc["crop_margem"])
    ativo = "+".join(_janela(a, b) for a, b in janelas_sessao) \
        if janelas_sessao else "0"
    cw, ch = lw * w_trab, lh * h_trab
    cx0, cy0 = x * w_trab, y * h_trab
    L.append(
        f"[{ult_base}]crop=w='if({ativo},{cw:.1f},{w_trab})':"
        f"h='if({ativo},{ch:.1f},{h_trab})':"
        f"x='if({ativo},{cx0:.1f},0)':y='if({ativo},{cy0:.1f},0)'[base];"
    )

    # Mesmo tamanho da carta principal (layout 1) - ver nota no
    # docstring. `ce["carta_altura"]` nao existe mais de proposito: duas
    # constantes para o mesmo numero e como ter dois lugares que decidem
    # [corte], so que em pixel (mesmo raciocinio de `_grafo_corte`).
    altura_px = max(2, int(h_trab * cc["carta_altura"]))
    margem_px = int(w_trab * cc["carta_margem"])
    # fracao da LARGURA do quadro (nao da altura - a pilha e horizontal
    # agora), quanto cada carta desliza para o centro em relacao a
    # anterior do mesmo lado.
    deslocamento_px = int(w_trab * ce["deslocamento"])
    y_base = (h_trab - altura_px) // 2

    # Nao e um fade de estilo - "religa" o alfa a 1 no comeco de CADA
    # ocorrencia da camada (ver `_agrupa_camadas_enumeracao`), curto o
    # bastante para nao se distinguir do corte seco que e a entrada de
    # toda carta secundaria neste programa (`grafo_cartas` faz o mesmo
    # corte seco, so que sem precisar disso porque a principal nunca
    # repete a mesma camada).
    #
    # `alpha=1` nos dois: sem isso `fade` desbota a COR (para preto), nao
    # o alfa - a carta vira um retangulo preto solido em vez de ficar
    # transparente. E os dois com `enable=` preso a janela DESSA MESMA
    # ocorrencia (nao so o `t=in`, ver nota antiga abaixo deste bloco):
    # sem isso, `fade=t=out` fica "alfa 0 para sempre" dali em diante -
    # nao so ate a proxima ocorrencia, mas alem dela tambem - e quando o
    # `t=in` seguinte termina sua propria rampa e volta a so repassar o
    # que vem de tras (e assim que `fade` funciona depois do fim da
    # rampa), o que ele repassa e esse alfa zerado, nunca o opaco. MEDIDO
    # no IMG_0246: com `t=in` sozinho guardado (t=out sem `enable`), a
    # 1a ocorrencia de cada camada aparecia certo e a 2a NUNCA aparecia -
    # so um flash de 50ms na propria rampa, sumindo de novo em seguida.
    # Com os dois presos a janela da ocorrencia, fora dela o par inteiro
    # vira passthrough e quem aparece por baixo e o `format=yuva420p` de
    # base (alfa 255 constante) - e o `enable` do PROPRIO overlay, nao
    # este alfa, quem decide se a camada e desenhada fora dali.
    POP = 0.05

    ult = "base"
    for i, (camada, img) in enumerate(zip(camadas, imgs), start=1):
        # `camadas` esta em ordem cronologica de 1a ocorrencia (ver
        # `_agrupa_camadas_enumeracao`), e profundidade so cresce dentro
        # de um lado - por isso a carta mais nova de cada pilha SEMPRE
        # vem depois na cadeia de overlay, ou seja, sempre por cima da
        # anterior do mesmo lado. Nao precisa reordenar para isso.
        desloc = camada["prof"] * deslocamento_px
        xpos = str(margem_px + desloc) if camada["lado"] == "esquerda" else \
            f"W-{margem_px + desloc}-w"
        prox = f"ce{i}"

        fades, janelas = [], []
        for oc in camada["ocorrencias"]:
            janela_oc = _janela(oc["ini"], oc["fim"])
            fades.append(f"fade=t=in:st={oc['ini']:.3f}:d={POP}:alpha=1:"
                          f"enable='{janela_oc}'")
            fades.append(f"fade=t=out:st={oc['fade_ini']:.3f}:"
                          f"d={oc['fade_dur']:.3f}:alpha=1:"
                          f"enable='{janela_oc}'")
            janelas.append(janela_oc)

        L.append(f"[{i}:v]scale=-2:{altura_px},format=yuva420p,"
                  f"{','.join(fades)}[carta{i}];")
        L.append(f"[{ult}][carta{i}]overlay=x={xpos}:y={y_base}:"
                  f"enable='{'+'.join(janelas)}'[{prox}];")
        ult = prox

    L.append(f"[{ult}]null[vo]")
    return "\n".join(L)


def _base(video, rotulo, ignorar_transcript=False, aplicar_mont=True,
          aplicar_corr=True):
    """Tudo o que precede o calculo dos trechos: le o fluxo analisado e
    empilha por cima dele as decisoes humanas.

    Devolve as pecas soltas, e nao o corte pronto, por duas razoes: o
    `conferir` precisa do fluxo E dos trechos para dizer o que ficou de
    fora do que se mandou cortar, e o editor recalcula a cada clique com o
    estado que esta na tela e ainda nao foi salvo - por isso ele chama com
    `aplicar_mont=False`/`aplicar_corr=False` e aplica as duas camadas ele
    mesmo, com a validacao mais branda que cabe a uma tela interativa (ver
    `Sessao._carrega`/`_carrega_correcoes`).

    A ordem em que as decisoes humanas se empilham e a ordem em que foram
    tomadas: transcript.md, depois anotacoes.json, depois montagem.json. A
    ultima e a mais explicita - no editor a pessoa clicou naquela
    palavra. `correcoes.json` fica fora dessa pilha de proposito: nao e
    decisao de corte, e sim de texto, e nao interfere em nenhuma delas -
    so troca `t["t"]` depois que todo o resto ja decidiu quem fica."""
    C = cfg()
    d = wdir(video)
    S = jload(d / "stream.json")
    stream, dur = S["stream"], S["dur"]

    md = d / "transcript.md"
    if md.exists() and not ignorar_transcript:
        if not confere_atualidade(md, stream):
            sys.exit(f"[{rotulo}] abortado: transcript.md desatualizado")
        n = reconcilia(stream, parse_transcript(md))
        print(f"[{rotulo}] transcript.md aplicado "
              f"({n} tokens apagados a mao)")
    else:
        print(f"[{rotulo}] usando as propostas automaticas")

    db = hop = None
    wav = d / "audio.wav"
    if wav.exists():
        db, hop, _ = envelope(wav)
    # relê as anotacoes do disco: editar anotacoes.json e rodar previa
    # deve funcionar sem refazer a analise inteira
    # O `or` que morava aqui sobre lista vazia era mais um caminho para
    # julgamento humano sumir em silencio, so que ao contrario: APAGAR
    # todas as anotacoes do arquivo fazia o programa cair na copia velha
    # gravada dentro do stream.json e reimpor exatamente as anotacoes que
    # a pessoa acabara de tirar. Se o arquivo existe, ele manda - mesmo
    # vazio. A copia do stream.json so vale quando nao ha arquivo nenhum.
    anot = (carrega_anotacoes(d) if (d / "anotacoes.json").exists()
            else S.get("anotacoes", []))
    anot = encaixa_anotacoes(stream, anot)
    if anot:
        print(f"[{rotulo}] {len(anot)} trechos anotados aplicados")
    mont = carrega_montagem(d)
    if mont and aplicar_mont:
        n = aplica_montagem(stream, mont, rotulo)
        print(f"[{rotulo}] montagem.json: {len(mont['tokens'])} palavras "
              f"decididas a mao ({n} mudam a proposta), "
              f"{len(mont['cortes'])} cortes e "
              f"{len(mont['restauros'])} restauros de milissegundo")
    corr = carrega_correcoes(d)
    if corr and aplicar_corr:
        aplica_correcoes(stream, corr, rotulo)

    return C, d, stream, dur, db, hop or 0.02, anot, mont, corr


def anotacoes_desobedecidas(anot, iv, tol=0.15, mao=None):
    """Anotacao que foi lida, aplicada, e mesmo assim continua no video.

    A pergunta mora aqui, uma vez so, porque duas partes precisam dela: o
    `conferir`, que lista tudo o que merece ouvido, e o `_plano`, que avisa
    antes de gastar minutos gerando audio ou video. Ja se acharam QUATRO
    caminhos pelos quais julgamento humano sumia no calculo do corte, e o
    que eles tinham em comum nao era a causa - era ninguem perguntar.

    `tol` e folga de borda, nao gosto: `folga_palavra` recua a emenda 0,06s
    de cada lado da palavra mantida vizinha, entao uma anotacao cercada de
    fala sobra ~0,12s por construcao. Acima disso e outra coisa.

    `mao` sao os trechos que a pessoa mandou ficar no editor (ver
    `spans_da_mao`). O que sobra por causa deles nao e a anotacao sumindo
    no calculo - e alguem desdizendo a anotacao de olhos abertos, depois de
    ouvir. Sai na quinta posicao como SIM ou NAO, e nao como medida, porque
    a pergunta e uma so e tem de ter uma resposta so: quem chama diz as
    duas coisas com palavras diferentes, mas nao decide qual e qual.

    O MESMO `tol` decide as duas coisas, e pela mesma razao. Reabrir uma
    palavra a mao devolve ela mais a folga em volta - e, quando ela cai
    perto da borda da anotacao, devolve tambem a lasca que sobrou do outro
    lado, fina demais para o corte pegar. MEDIDO no IMG_0246: reabrir o
    `os` de 116,59-116,83 dentro da anotacao de 116,46-128,03 devolve
    0,49s, dos quais 0,36 sao a palavra com folga e 0,13 sao essa lasca.
    Exigir a soma exata fazia o alarme mais serio deste programa disparar
    num ajuste que a propria pessoa acabara de fazer."""
    mao = _spans(mao)
    fora = []
    for an in anot or []:
        x, y = an["ini"], an["fim"]
        vivo = sum(max(0.0, min(y, b) - max(x, a)) for a, b in iv)
        if vivo <= tol:
            continue
        por_mao = sum(max(0.0, min(y, b, q) - max(x, a, p))
                      for a, b in iv for p, q in mao)
        fora.append((x, y, vivo, str(an.get("motivo", "")),
                     por_mao >= vivo - tol))
    return fora


def enterradas_pelo_whisper(d, C, stream, iv):
    """Palavras que era para FICAR e que, pelo relogio do WHISPER, estao
    dentro de um pedaco removido. -> [(fundura, fracao viva, s, e, texto)]

    Todo o resto do programa raciocina sobre os tempos do ALINHADOR. Quando
    eles estao errados, o erro e coerente do comeco ao fim: o corte cai onde
    o fluxo mandou, `_respeita_palavras` confere contra os mesmos numeros
    tortos, e ninguem reclama. Foi assim que o corte do IMG_0247 comeu "que
    muitas" - pelo alinhador a frase comecava em 603,3, depois da emenda;
    pelo audio ela comeca em 600,1, dentro dela.

    Aqui a pergunta e feita com a unica medida INDEPENDENTE que existe, a do
    Whisper. Nao conserta nada - so mede. O conserto e mudar a anotacao (ou
    a borda no editor), e isso quem escreve e uma pessoa.

    A pergunta mora aqui, uma vez so, porque quem pergunta sao DOIS: o
    `_plano`, que avisa antes de gastar minutos gerando audio ou video, e o
    `achados_corte`, que lista para quem esta conferindo (comando e editor).
    Era so do `_plano` ate 04/09/2026 - e o `conferir`, o comando que a doc
    manda rodar ANTES de ouvir justamente para nao depender do ouvido, era o
    unico lugar onde o aviso nao aparecia. MEDIDO nos 7 fluxos de work/:
    dispara em TODOS os 7, 26 palavras (2 a 7 por video); a disputa
    "lamina"/"mental," que o Rafael ouviu como um "ou" estranho no meio do
    audio estava entre elas, e para chegar nela era preciso gerar o audio."""
    p = d / "words.json"
    if not p.exists():
        return []
    W = jload(p)["words"]
    pal = [t for t in stream if t["k"] == "palavra"]
    if len(W) != len(pal):
        return []           # sem correspondencia 1:1 nao da para conferir

    fundo = C["alinhamento"]["enterrada_min"]
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
            suspeitas.append((fund, viva / (e0 - s0), s0, e0, w["w"]))
    suspeitas.sort(reverse=True)
    return suspeitas


def confere_pelo_whisper(d, C, stream, iv, rotulo):
    """A mesma medida de `enterradas_pelo_whisper`, dita no console de quem
    esta prestes a gastar minutos gerando audio ou video."""
    suspeitas = enterradas_pelo_whisper(d, C, stream, iv)
    if not suspeitas:
        return 0
    print(f"[{rotulo}] AVISO: {len(suspeitas)} palavra(s) que deviam FICAR "
          f"estao enterradas dentro de um corte, pelos tempos do Whisper.")
    print(f"[{rotulo}] o alinhamento desse trecho provavelmente errou. "
          f"Ouça esses pontos antes de aceitar o corte (o `conferir` lista "
          f"todas, com o tempo na previa):")
    for fund, frac, s0, _e0, txt in suspeitas[:8]:
        print(f"    {s0:8.2f}s  {txt!r}  ({fund:.1f}s dentro do corte, "
              f"sobra {frac:.0%})")
    if len(suspeitas) > 8:
        print(f"    ... e mais {len(suspeitas) - 8}")
    return len(suspeitas)


def _plano(a, rotulo):
    """Etapas comuns a render, audio e previa: aplica as decisoes humanas e
    calcula os trechos a manter."""
    C, d, stream, dur, db, hop, anot, mont, corr = _base(
        a.video, rotulo, getattr(a, "ignorar_transcript", False))
    iv = intervalos(stream, C, dur, db, hop, anot, mont)
    # O `conferir` ja fazia esta pergunta, e so quem lembrava de roda-lo
    # ouvia a resposta. Aqui ela e feita no caminho que TODO mundo passa
    # antes de gastar minutos: e o unico defeito deste programa que o
    # ouvido nao pega, porque o que sumiu nao faz barulho nenhum.
    for x, y, vivo, motivo, mao in anotacoes_desobedecidas(
            anot, iv, mao=spans_da_mao(stream, mont,
                                       C["corte"]["folga_palavra"])):
        if mao:
            print(f"[{rotulo}] a anotacao {x:.2f}-{y:.2f} foi reaberta "
                  f"no editor - {vivo:.2f}s de volta: {motivo[:50]}")
        else:
            print(f"[{rotulo}] AVISO: a anotacao {x:.2f}-{y:.2f} nao foi "
                  f"obedecida - {vivo:.2f}s continuam no video: "
                  f"{motivo[:60]}")
    # Mesma regra da anotacao desobedecida, para a outra metade do
    # julgamento: o `conferir` ja faz esta pergunta e so quem lembrava de
    # roda-lo ouvia a resposta. Aqui ela e feita no caminho por onde TODO
    # mundo passa antes de gerar audio ou video.
    sem, _ = pendencias_sem_resposta(
        pendencias(stream, C, carrega_vocabulario(d)),
        carrega_respostas(d), iv)
    for q in sem[:6]:
        print(f"[{rotulo}] AVISO: {q['falta']} em {q['s']:.2f}s "
              f"- {q['rot']}: {q['desc'][:50]}")
    if sem:
        print(f"[{rotulo}] {len(sem)} pendencia(s) do revisao.txt sem "
              f"resposta. Decida cada uma e escreva em "
              f"{d/'respostas.json'} (ou corte o trecho).")
    confere_pelo_whisper(d, C, stream, iv, rotulo)
    # Corte que nao deixa nada nao e corte, e o ffmpeg so diz isso com um
    # erro de `concat=n=0` que nao ajuda ninguem. Acontece quando uma
    # anotacao (ou uma sequencia delas) cobre o video inteiro.
    if not iv:
        sys.exit(f"[{rotulo}] abortado: o corte nao deixou nenhum trecho. "
                 f"Provavelmente uma anotacao cobre o video inteiro - veja "
                 f"{d/'anotacoes.json'}.")
    return C, d, dur, iv


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


def sobrepostos(st, folga=0.5):
    """Pares de tokens (palavra OU hesitacao) que invadem o tempo um do
    outro, com o indice ORIGINAL de cada um em `st`.

    `fora_de_ordem` pega o alinhador embaralhando o INICIO da palavra.
    Isto pega o sintoma irmao, que nao mexe em nenhum inicio: dois tokens
    cobrindo o mesmo intervalo.

    A CAUSA, que este docstring atribuia a uma disputa entre
    `resolve_suspeitos` e `marca_ecos`, foi MEDIDA nos 10 fluxos de
    `work/` e e outra: das 31 invasoes, 27 tem dentro uma palavra cujo
    tempo o proprio alinhador recusou (`align_rejeitado` deriva/ordem) -
    e a que sai com `~` no revisao.txt. Nao ha duas etapas brigando; ha
    uma palavra no lugar errado, e todo mundo que le o tempo dela erra
    junto. Por isso a saida daqui alimenta `zonas_suspeitas`, ao lado do
    `~`, e nao uma pendencia propria.

    E das 31, 8 sao entre dois tokens que ja saem inteiros (nao sobra
    nada para dar errado) e so 3 produzem a consequencia que se temia -
    a ilha curta do "em" solto do IMG_0246_bruto ("das mulheres,
    principalmente, nessas cartas, em que..."). Essas 3 o `conferir` ja
    lista como ILHA CURTA, medidas contra o corte de verdade; a nota de
    disputa entra ali para dizer de onde a ilha veio. Perguntar as 31 ao
    passe de julgamento era pedir julgamento de conteudo sobre geometria,
    e o proprio /revisar mandava nao responder.

    `folga=0.5` veio de rodar contra os quatro stream.json disponiveis
    (IMG_0246, IMG_0246_bruto, IMG_0247, IMG_0248): abaixo de ~0,3s e so
    jitter comum entre duas palavras reais vizinhas (visto ate 0,44s em
    trecho limpo, sem nenhum defeito); a 0,5s sobram so os pares que batem
    em casos ja documentados no codigo - o "do" que _respeita_palavras
    cita (IMG_0247, 49,81-53,08 x 50,64-50,90) e o "principalmente"
    acima, presentes tanto no bruto quanto no IMG_0246 ja julgado (onde a
    disputa se resolveu diferente: os dois tokens saem cut=True - sinal de
    que o resultado desse canto do pipeline nao e estavel entre rodadas)."""
    pal = sorted(((t["s"], i) for i, t in enumerate(st)
                 if t["k"] in ("palavra", "hesitacao")))
    out = []
    for (sa, ia), (sb, ib) in zip(pal, pal[1:]):
        if sb < st[ia]["e"] - folga:
            out.append((ia, ib))
    return out


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


def zonas_suspeitas(st, W, choques, junta=3.0, larg_min=1.0):
    """Faixas de tempo em que NENHUMA borda pode ser ancorada.

    Junta as palavras cujo tempo o alinhador recusou (`deriva`/`ordem`, as
    que saem com `~`) com as que invadem o tempo uma da outra
    (`sobrepostos`), e funde o que esta perto. As duas listas sao o mesmo
    defeito visto de dois lados - MEDIDO nos 10 fluxos de work/: 27 dos 31
    pares de `sobrepostos` tem uma palavra `deriva`/`ordem` dentro.

    Existe porque `~` solto nao responde a pergunta que a pessoa tem na
    mao: "da para ancorar aqui?". Tres `~` espalhados numa linha podem ser
    tres palavras ruins com fala boa no meio, ou 7 segundos seguidos sem
    uma fronteira confiavel - e so o segundo caso obriga a anotar a regiao
    inteira. No IMG_0247 sao 49,8-57,4s e 437,1-441,3s; no IMG_0248,
    169,3-175,2s e 244,0-247,9s.

    `larg_min` corta a zona de uma palavra so: ali o `~` no texto ja diz
    tudo, e a faixa nao acrescenta nada. Nos 10 fluxos isso deixa 4 a 7
    faixas por video."""
    pal_i = [i for i, t in enumerate(st) if t["k"] == "palavra"]
    ruins = ({pal_i[k] for k, w in enumerate(W)
              if w.get("align_rejeitado") in ("deriva", "ordem")}
             if len(pal_i) == len(W) else set())
    for ia, ib in choques:
        ruins.update((ia, ib))
    z = []
    for a, b in sorted((st[i]["s"], st[i]["e"]) for i in ruins):
        if z and a - z[-1][1] <= junta:
            z[-1][1] = max(z[-1][1], b)
            z[-1][2] += 1
        else:
            z.append([a, b, 1])
    return [(a, b, n) for a, b, n in z if n >= 2 and b - a >= larg_min]


# `why` de quem decidiu olhando o TEXTO, e nao a acustica nem o relogio.
# Forma de regra, nao taxa - por isso mora aqui e nao no config.toml.
#
# A distincao existe por causa de uma pergunta so: quando o tempo removido
# nao cabe no texto que a regra leu, o texto e que esta errado. Isso so faz
# sentido de perguntar a quem decidiu POR texto. As decisoes acusticas
# ("voz sem texto", "pausa", "nao e fala N car/s") ja mediram o tempo por
# conta propria e nao tem o que estranhar nele; "arrastada" e o proprio
# relogio julgando; "anotado:" e uma pessoa.
CORTE_POR_TEXTO = ("alongamento", "retomada colapsada", "cacoete",
                   "muleta isolada", "gaguejada", "tentativa",
                   "ela mandou cortar", "hesitação recuperada")

EXPLICA_PENDENCIA = {
    "texto nao explica o tempo":
        "a regra cortou por causa do TEXTO, e o texto nao da conta do "
        "tempo que sai. Onde o Whisper nao conseguiu ler, ele escreve "
        "um cacoete plausivel e curto no lugar de fala que existe: "
        "ouca o trecho antes de deixar sair.",
    "texto reconstruido":
        "leitura de baixa confianca de um trecho que o Whisper nao "
        "leu. Se nao faz sentido no assunto da aula, e ELE que sai.",
    "som alto sem texto":
        "ninguem conseguiu ler e o programa manteve de proposito, "
        "para voce decidir. Sai ou fica?",
    "fala rarefeita":
        "o que SOBRA ali e pouca fala em muito tempo: parada longa, "
        "retomada, ou ela se perdendo.",
    "retomada?":
        "ela repete duas palavras pouco depois. Pode ser tentativa "
        "abandonada (corte a PRIMEIRA, fique com a ultima) ou anafora "
        "de proposito ('na carta da Rainha... na carta do Rei'). So o "
        "sentido separa as duas - o programa nao corta isto sozinho.",
}


def pendencias(st, C, vocab):
    """A lista fechada do que o programa sabe que NAO sabe resolver.

    Mora aqui, uma vez so, porque tres partes precisam dela: o `revisar`,
    que numera e ancora cada uma no texto; o `conferir`, que cobra a
    resposta; e o `_plano`, que avisa antes de gastar minutos gerando
    audio ou video.

    Cada item leva o trecho de audio de que ele TRATA (`x`, `y`), e nao so
    a ancora: e por ele que `pendencias_sem_resposta` sabe se a pergunta
    ainda existe depois do corte.

    TRES CATEGORIAS SAIRAM DAQUI, e as tres por medicao nos 10 fluxos de
    `work/` (268 pendencias no total antes, 55 depois - de 7-50 por video
    para 0-12):

      `fala nao transcrita` (106, a maior de todas) nao dizia nada que o
      token VIZINHO ja nao dissesse. Ela apontava a PALAVRA em que o audio
      estava grudado, mas `resolve_suspeitos` ja separou esse audio num
      token proprio antes do revisao.txt existir: nos 106 casos o vizinho
      ou ja esta cortado (90 - o audio esta indo embora) ou ja e outra
      pendencia, `som alto sem texto` (14) ou `texto reconstruido` (2).
      Era a mesma pergunta feita duas vezes, do lado errado.

      `cauda arrastada` (76) era pergunta sobre GEOMETRIA feita a quem le
      texto - e sem os numeros que permitiriam responde-la. Virou medida
      no `conferir` (ARRASTO SOBREVIVE): das 76, 64 deixam 0,06-0,14s de
      arrasto - a folga que `_respeita_palavras` devolve por construcao -
      e 11 deixam o arrasto INTEIRO, porque a poda (0,25-0,30s) nao coube
      no `minimo`. Mais 3 sobram por outro motivo, em regiao que o
      alinhador embaralhou. Perguntar as 76 escondia essas 14.

      `disputa` (31) o proprio /revisar mandava nao responder ("nao e
      julgamento de conteudo... e achado para o programador"). E a causa
      no docstring de `sobrepostos` estava errada: 27 das 31 envolvem uma
      palavra cujo tempo o proprio alinhador recusou (`deriva`/`ordem`),
      que ja aparece como `~` no texto; 8 sao entre dois tokens que ja
      saem; e a unica consequencia que ela previa - ilha curta - aparece
      em 3, e nessas 3 o `conferir` ja lista ILHA CURTA, medida contra o
      corte de verdade em vez de adivinhada."""
    pend = []

    def pergunta(i, rot, desc, x=None, y=None):
        pend.append({"i": i, "rot": rot, "desc": desc, "s": st[i]["s"],
                     "x": st[i]["s"] if x is None else x,
                     "y": st[i]["e"] if y is None else y})

    for x, y, sob, n in rarefeitos(st, C["revisao"]):
        # ancora no primeiro token MANTIDO que cai dentro: e o unico ponto
        # do texto corrido onde ele pode aparecer.
        for i, t in enumerate(st):
            if not t["cut"] and t["e"] > x and t["s"] < y:
                pergunta(i, "fala rarefeita",
                         f"{sob:.0f}s com so {n} palavra(s)", x, y)
                break

    # Retomada que o corte automatico NAO pegou. `detecta_tentativas` corta
    # com `min_ngram` do config (3 palavras identicas); aqui ela roda de
    # novo com `min_ngram_aviso` (2) so para PERGUNTAR.
    #
    # Por que perguntar em vez de cortar: MEDIDO nos 10 fluxos de work/, o
    # limiar de 2 acha 9 das retomadas que uma pessoa anotou a mao no
    # IMG_0246/0247/0248/7 de espadas/as de espadas - "personagens
    # politicos ou da cultura, enfim,", "pessoas mesmo, que aparecem",
    # "uma coisa sendo representada,", "na vida do consulante", "nao
    # precisa, nao precisa" - e mais 4 que sao ANAFORA, nao retomada: "na
    # Carta da Rainha, por exemplo, ... na Carta do Rei e o Rio", "nao e
    # isolado, nao e so ler", "estando em troca, estando em relacao". As
    # quatro sao conteudo de verdade e cortar qualquer uma seria o pior
    # defeito deste programa. A diferenca entre as duas coisas nao esta na
    # forma - esta no sentido, e quem le o sentido e o passe de julgamento.
    #
    # Sobram 0 a 4 perguntas por video, cada uma com o texto citado.
    #
    # `respiro_gap` alcanca a retomada RAPIDA, que o `min_ngram` sozinho
    # nao ve: pausa abaixo de `pausa_intocavel` nao vira token, e sem token
    # os filtros 1 e 3 leem a retomada como paralelismo (ver
    # `detecta_tentativas`). O numero e o `gap_min` do [silencio] porque e
    # a mesma pergunta que ele ja responde - "este vao entre duas palavras
    # conta como lacuna?" -, e um segundo numero para decidir isso seria um
    # segundo lugar decidindo a mesma coisa.
    #
    # Custo MEDIDO nos 11 fluxos de work/: as perguntas `retomada?` vao de
    # 10 para 14 no total - 1,3 por video, pior caso 4, dentro do teto que
    # este bloco registra. Sao 8 cadeias novas, mas metade cai em trecho
    # que ja esta saindo e o filtro logo abaixo a descarta sozinho.
    for c in detecta_tentativas(st, C["tentativa"],
                                C["tentativa"]["min_ngram_aviso"],
                                respiro_gap=C["silencio"]["gap_min"]):
        pals = [st[j] for j in range(c["ini"], c["fim"])
                if st[j]["k"] == "palavra"]
        if not pals or sum(1 for t in pals if not t["cut"]) <= len(pals) / 2:
            continue        # ja esta saindo: nao ha o que perguntar
        pergunta(c["ini"], "retomada?",
                 f"volta a dizer {c['repetido']!r} {c['dur']:.1f}s depois. "
                 f"Descartaria: {c['descartado'][:70]!r}",
                 st[c["ini"]]["s"], st[c["fim"] - 1]["e"])

    # Corte automatico cujo TEMPO o proprio texto que o justificou nao
    # explica. E a unica pendencia sobre audio que JA SAI, e existe porque
    # o defeito que ela pega nao deixa marca em lugar nenhum: no "as de
    # espadas" um corte de 27,5s (35,46-62,92s) saiu inteiro sem uma
    # pergunta, e o Rafael so o descobriu ouvindo a FONTE - dos 27,5s, 9,6
    # eram mesmo hesitacao e 17,9 eram fala que o Whisper nao transcreveu.
    # Nao havia `[??]` (que `protege_naolidos` cobre), nao havia `[rec:]`,
    # nao havia `VOZ` na marca: o corte tinha texto, so que um texto
    # inventado - "éé..." repetido 200 vezes e "profunda, mais potente.".
    #
    # A pergunta e a mesma que `marca_arrastadas` ja faz para podar cauda
    # ("este tempo cabe no que esta escrito?"), com os MESMOS numeros dela,
    # e falhou ali por duas razoes independentes: `marca_cacoetes` corre
    # antes e ja tinha cortado o token (e `marca_arrastadas` pula token
    # cortado), e mesmo se olhasse, `letras_faladas` conta 200 letras num
    # "éé..." de uma vogal so - o que fazia 20,5s de um som parecerem
    # ritmo de fala normal. Por isso aqui o texto passa por
    # `letras_uma_vez`.
    #
    # MEDIDO nos 7 fluxos de work/, sobre 216 cortes automaticos de >=1s:
    # dispara 2 vezes, as duas no MESMO corte de 27,5s (o "éé..." de 20,54s
    # e o "profunda, mais potente." de 6,06s). Sem o piso de
    # `corte_por_texto_min` seriam 8, e as outras 6 (1,40 a 2,27s) sao
    # cacoete de verdade - "Eeeeee" sustentado, "É... isso.".
    med = ritmo_mediano(st)
    for i, t in enumerate(st if med is not None else []):
        if not t["cut"] or t["k"] == "silencio":
            continue
        why = str(t.get("why", ""))
        dur = t["e"] - t["s"]
        if (dur < C["revisao"]["corte_por_texto_min"]
                or not why.startswith(CORTE_POR_TEXTO)):
            continue
        cabe = med * letras_uma_vez(t["t"])
        if dur < cabe * C["arrastada"]["fator"]:
            continue
        pergunta(i, "texto nao explica o tempo",
                 f"{dur:.1f}s saem como {why[:26]!r}, e o texto "
                 f"({t['t'][:24]!r}) da conta de {cabe:.1f}s",
                 t["s"], t["e"])
        # A unica pendencia que nasce sobre trecho JA CORTADO, e por isso a
        # unica em que o corte nao e resposta. Ver `pendencias_sem_resposta`.
        pend[-1]["cortada"] = True
        pend[-1]["proprio"] = True

    for i, t in enumerate(st):
        if t["cut"] or t["k"] != "hesitacao":
            continue
        if t.get("mao_texto"):
            # o texto deste token foi escrito A MAO no editor, ouvindo. Nao
            # ha o que perguntar: "texto reconstruido" e a duvida sobre uma
            # LEITURA de confianca baixa do Whisper, e aqui nao houve
            # leitura nenhuma. Sem esta linha, escrever o que se ouve num
            # vao criava na hora uma pendencia sobre a propria frase que a
            # pessoa acabou de digitar - alarme disparando onde alguem ja
            # decidiu, que e o defeito que este arquivo mais persegue.
            continue
        if str(t["t"]).startswith("??"):
            pergunta(i, "som alto sem texto", f"{t['e']-t['s']:.1f}s")
        else:
            fora = fora_do_vocabulario(t["t"], vocab)
            extra = ("  <== " + ", ".join(fora) + " nao existe no "
                     "material do curso: leitura provavelmente errada"
                     if fora else "")
            pergunta(i, "texto reconstruido",
                     f"{t['t']!r} ({t['e']-t['s']:.1f}s){extra}")
        # so aqui o token ancora E o trecho em questao - `proprio`. E o que
        # deixa `pendencias_sem_resposta` cobrar as duas coisas que so
        # fazem sentido quando trecho e pergunta coincidem: manter a mao
        # vale por resposta, e responder "sai" obriga o trecho a sair.
        # Numa `retomada?` nenhuma das duas valeria - a cadeia vai da
        # primeira tentativa ate a versao boa, que fica.
        pend[-1]["proprio"] = True
        pend[-1]["mao"] = t["why"] in MAO_MANTEM

    pend.sort(key=lambda q: (q["s"], q["i"]))
    for n, q in enumerate(pend, 1):
        q["n"] = n
    return pend


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
    C = cfg()
    bagunca = fora_de_ordem(st)
    choques = sobrepostos(st)
    W = jload(d / "words.json").get("words", [])
    zonas = zonas_suspeitas(st, W, choques)
    pend = pendencias(st, C, carrega_vocabulario(d))
    resp = carrega_respostas(d)

    L = [f"# {Path(a.video).stem} | {hhmmss(dur)} | tempos em segundos",
         "# [-N.Ns] = ja marcado para corte, trecho quieto. Texto solto =",
         "# sera mantido.",
         "# [-N.Ns VOZ] = marcado para corte MESMO havendo som alto ali",
         "# dentro. O transcritor nao leu, mas o microfone ouviu: pode ser",
         "# frase inteira prestes a sumir. Confira antes de deixar passar.",
         "# [rec: ...] = texto RECONSTRUIDO. Nao saiu da transcricao do",
         "# video: o programa re-transcreveu aquele pedaco isolado porque",
         "# o Whisper nao tinha lido nada ali. E a leitura de MENOR",
         "# confianca do arquivo inteiro, e e onde bocejo e resmungo se",
         "# disfarcam de palavra. Va sempre pelo sentido: se nao encaixa",
         "# no assunto da aula, o candidato a sair e ele, nao o vizinho.",
         "# palavra~ = o tempo dessa palavra nao e confiavel (o alinhador",
         "# recusou o proprio resultado e caiu no tempo grosseiro do",
         "# Whisper). NAO ancore borda de corte numa palavra com ~: ela",
         "# pode estar segundos longe de onde aparece aqui.",
         "# [Pn] = pendencia numero n, listada em PENDENCIAS logo abaixo.",
         "# Procure, alem das pendencias: retomadas com outras palavras,",
         "# interrupcoes, trechos em que ela se perde. Responda com",
         "# trechos [inicio, fim, motivo]."]
    if bagunca:
        L.append(f"# aviso: {bagunca} palavras com tempo fora de ordem - o "
                 f"alinhador embaralhou")
        L.append("# esses trechos, e ali o [-N.Ns] pode faltar mesmo "
                 "havendo parada.")
    if zonas:
        # O `~` marca a palavra; isto marca a REGIAO. Sem ela a pessoa
        # descobre que nao ha fronteira confiavel ali depois de escrever
        # a anotacao, quando o `conferir` acusa que ela nao foi obedecida.
        L.append("# faixas em que o alinhador nao acertou o tempo de "
                 "NENHUMA palavra por perto:")
        for a, b, n in zonas:
            L.append(f"#   {a:.1f}-{b:.1f}s ({n} palavras) - para cortar "
                     f"aqui, anote a faixa inteira; nao ancore dentro.")

    # ------------------------------------------------------ pendencias
    # Tudo o que o programa sabe que NAO sabe resolver sozinho vira um
    # item numerado aqui em cima, alem de aparecer ancorado no texto.
    #
    # Antes cada uma destas marcas so existia no meio do texto corrido, e
    # a instrucao "toda ocorrencia precisa de uma resposta sua" dependia
    # de quem lia ir catando marca por marca ate o fim. Nao foi o que
    # aconteceu: no 7 de espadas um bocejo inteiro atravessou o passe de
    # julgamento disfarcado de palavra reconstruida e so apareceu quando
    # o Rafael ouviu o audio. Lista numerada se confere; marca solta no
    # meio de 200 linhas de texto nao se confere.
    #
    # QUAIS perguntas entram na lista e decisao de `pendencias()`, nao
    # daqui: o `conferir` e o `_plano` cobram a resposta de cada uma, e
    # duas listas diferentes seriam duas perguntas diferentes com o mesmo
    # nome. A explicacao de cada categoria vai UMA vez, no cabecalho do
    # bloco, e so das categorias que apareceram - repeti-la em cada item
    # custava 100 caracteres por linha num arquivo que existe para ser
    # pequeno.
    def ancora(i):
        """O token MANTIDO mais proximo de `i`, para onde a marca `[Pn]`
        vai no texto corrido.

        Token cortado nao e impresso, entao marca posta nele some sem
        deixar rastro - e ja sumiu: era o caso de toda `[disputa]`, que
        `sobrepostos` devolvia sem olhar `cut`."""
        for k in range(i, len(st)):
            if not st[k]["cut"]:
                return k
        for k in range(i, -1, -1):
            if not st[k]["cut"]:
                return k
        return None

    # Uma pendencia ja respondida no `respostas.json` continua na lista,
    # marcada: some-la seria esconder o julgamento de quem reler o arquivo
    # daqui a seis meses, e reabri-la sem marca faria a pessoa responder
    # duas vezes a mesma coisa.
    respondida, casadas = {}, set()
    for q in pend:
        k = casa_resposta(q, resp)
        if k is not None:
            respondida[q["n"]] = resp[k]
            casadas.add(k)
    orfas = [r for k, r in enumerate(resp) if k not in casadas]

    marca = {}
    for q in pend:
        j = ancora(q["i"])
        if j is not None:
            marca[j] = marca.get(j, "") + f"[P{q['n']}]"

    if pend:
        aberta = [q for q in pend if q["n"] not in respondida]
        L.append("")
        L.append(f"## PENDENCIAS ({len(pend)}, {len(aberta)} sem resposta) "
                 f"- responda cada uma em {d.name}/respostas.json e no "
                 f"relatorio: sai, fica ou ouvido, e por que")
        for rot in sorted({q["rot"] for q in pend}):
            L.append(f"#  {rot} = {EXPLICA_PENDENCIA[rot]}")
        for q in pend:
            r = respondida.get(q["n"])
            selo = f"  [ja respondida: {r['resposta']}]" if r else ""
            L.append(f"P{q['n']:<3d} {q['s']:8.2f}s  "
                     f"{q['rot']}: {q['desc']}{selo}")
    L.append("")

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
    pal_i = [i for i, t in enumerate(st) if t["k"] == "palavra"]
    duvida = ({pal_i[k] for k, w in enumerate(W)
               if w.get("align_rejeitado") in ("deriva", "ordem")}
              if len(pal_i) == len(W) else set())

    # O microfone e a segunda opiniao sobre o que esta indo embora: um
    # [-1.8s] de silencio e um [-1.8s] de frase eram impressos igual.
    db, hop, _ = envelope(d / "audio.wav")
    limiar = limiar_voz(db, C["silencio"]["limiar_fala_db"])

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
        elif t["k"] == "silencio":
            linha.append(f"[pausa {t['e']-t['s']:.1f}s]")
        else:
            # texto reconstruido vai INTEIRO. Truncar em 14 caracteres era
            # esconder metade da prova: o que denuncia bocejo lido como
            # palavra e a frase nao fechar sentido, e meia frase nao fecha
            # sentido nenhum por definicao.
            linha.append(f"[rec: {t['t']}]")
        if len(" ".join(linha)) > 130:
            L.append(f"{ini:6.1f} " + " ".join(linha))
            linha, ini = [], None
    if linha:
        L.append(f"{ini:6.1f} " + " ".join(linha))

    p = d / "revisao.txt"
    p.write_text("\n".join(L) + "\n", encoding="utf-8")
    n = len("\n".join(L))
    print(f"[revisar] {p}  ({n} caracteres, ~{n//4} tokens)")
    aberta = len(pend) - len(respondida)
    print(f"[revisar] {len(pend)} pendencia(s); {aberta} ainda sem resposta")
    if orfas:
        # Resposta que nao casa com pendencia nenhuma quase sempre e tempo
        # digitado errado - e o sintoma dela e justamente o contrario do
        # que a pessoa espera: a pendencia continua na lista de abertas.
        for r in orfas:
            print(f"[revisar] resposta em {r['em']} nao casa com pendencia "
                  f"nenhuma (tempo errado, ou ela ja saiu no corte)")
    print(f"[revisar] escreva o julgamento em {d/'anotacoes.json'} e as "
          f"respostas em {d/'respostas.json'}")


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


def achados_corte(stream, C, iv, anot, mont, pend, resp, d):
    """Todo suspeito que a GEOMETRIA deste corte produz, numa lista so.

    Ficou fora do `cmd_conferir` quando o editor passou a fazer a mesma
    pergunta na tela. Antes disso eram dois mundos: o `conferir` achava 25
    pontos no IMG_0248 e o painel do editor mostrava zero deles, porque o
    painel so sabia de anotacao desobedecida e de palavra que a pessoa
    mandou sumir e nao sumiu. Quem estava editando nao tinha como saber que
    havia 25 coisas para ouvir sem sair do editor, rodar um comando e ler
    um txt em segundos da fonte - numeros que a folha nao mostra em lugar
    nenhum.

    E a mesma razao pela qual `intervalos()` e uma funcao so: duas
    implementacoes da mesma pergunta divergem, e a que diverge em silencio
    e sempre a que ninguem esta olhando.

    `d` e a pasta de trabalho, e e obrigatorio de proposito: sem ela nao da
    para ler o `words.json`, e a pergunta que depende dele - a palavra
    enterrada, item 6 - sumiria de quem esquecesse de passar a pasta. Um
    parametro opcional aqui seria uma lista de achados que diverge da outra
    em silencio, que e exatamente o que esta funcao existe para impedir.

    Devolve (achados, orfas). Cada achado e a tupla ordenavel
    (tempo na previa, rotulo, ini na fonte, fim na fonte, texto)."""
    achados = []
    _, para_previa = _mapa_previa(iv)

    def diz(rot, x, y, txt):
        achados.append((para_previa(x), rot, x, y, txt))

    # 0. Pendencia do revisao.txt que ninguem respondeu.
    #
    #    "Responda toda pendencia" era regra do prompt do /revisar e de
    #    mais ninguem: o programa listava, numerava e ancorava cada `Pn`, e
    #    depois nao perguntava se alguma tinha ficado sem resposta. E o
    #    mesmo buraco de `anotacoes_desobedecidas` antes de ela existir -
    #    julgamento que ninguem cobra e julgamento que um dia nao acontece.
    #
    #    Uma pendencia se responde de dois jeitos, e os dois valem: cortando
    #    o trecho (a resposta e o proprio corte, e por isso `iv` entra na
    #    conta) ou escrevendo no `respostas.json` - o unico jeito de dizer
    #    "fica", que nao deixa marca em lugar nenhum.
    sem, orfas = pendencias_sem_resposta(pend, resp, iv)
    for q in sem:
        diz(q["falta"], q["s"], q["y"], f"{q['rot']}: {q['desc'][:64]}")

    # 1. Anotacao desobedecida. E o pior de todos: o julgamento humano foi
    #    escrito, foi lido, e sumiu na geometria sem uma linha de aviso.
    for x, y, vivo, motivo, mao in anotacoes_desobedecidas(
            anot, iv, mao=spans_da_mao(stream, mont,
                                       C["corte"]["folga_palavra"])):
        diz("ANOTACAO REABERTA A MAO" if mao
            else "ANOTACAO NAO OBEDECIDA", x, y,
            f"sobra {vivo:.2f}s de {y-x:.2f}s | {motivo[:50]}")

    # 2. Fala marcada para sair que continua tocando. Silencio nao entra:
    #    metade da pausa fica colada em cada lado de proposito
    #    (pausa_alvo/2), entao ele "sobrevive" sempre, por desenho.
    #    O limite de 0,15s tem um cego, e ele e exatamente o caso que o
    #    editor avisa na tela: palavra mais curta que as duas folgas de
    #    0,06 sobrevive INTEIRA sem nunca chegar a 0,15s. MEDIDO nos 10
    #    fluxos de work/: dois casos, um `eu` de 0,08s (as de espadas,
    #    516,0s) e um `na` de 0,06s dentro de uma anotacao (IMG_0247,
    #    351,1s) - poucos, e os dois sao a palavra inteira continuando a
    #    tocar depois de alguem mandar tira-la. So a passada mecanica nao
    #    via: `Sessao.avisos` pergunta isso desde sempre, mas so de quem
    #    abre o editor.
    for t in stream:
        if not t["cut"] or t["k"] == "silencio":
            continue
        vivo = sum(max(0.0, min(t["e"], b) - max(t["s"], aa)) for aa, b in iv)
        larg = t["e"] - t["s"]
        inteira = larg > 0.02 and vivo >= larg - 0.02
        if vivo > 0.15 or inteira:
            diz("FALA CORTADA SOBREVIVE", t["s"], t["e"],
                f"{vivo:.2f}s{' INTEIRA' if inteira else ''} | "
                f"{t.get('why','')[:24]} | {t['t'][:30]!r}")

    # 3. Ilha curta. Nem sempre e defeito - uma palavra sozinha entre dois
    #    cortes pode ser exatamente o que a anotacao pediu -, mas e sempre
    #    duas emendas em volta de pouca coisa, e vale um ouvido.
    #    `sobrepostos` entra aqui, e nao no revisao.txt, porque e aqui que
    #    ele tem consequencia. MEDIDO nos 10 fluxos: das 31 invasoes, 3
    #    produzem ilha curta - e sao essas 3 que alguem precisa ouvir. As
    #    outras 28 eram pergunta sem resposta possivel no passe de
    #    julgamento (27 delas envolvem palavra que o alinhador ja marcou
    #    com `~`; 8 sao entre dois tokens que ja saem inteiros).
    choques = sobrepostos(stream)
    for x, y in iv:
        if y - x < C["corte"]["ilha_minima"]:
            pals = " ".join(t["t"] for t in stream if t["k"] == "palavra"
                            and not t["cut"] and t["e"] > x and t["s"] < y)
            briga = any(min(stream[ia]["e"], stream[ib]["e"]) > x
                        and max(stream[ia]["s"], stream[ib]["s"]) < y
                        for ia, ib in choques)
            diz("ILHA CURTA", x, y, f"{y-x:.2f}s | {pals[:40]!r}"
                + (" | duas recuperacoes disputam este audio" if briga
                   else ""))

    # 3b. Arrasto que se mandou podar e continua tocando.
    #
    #     `marca_arrastadas` corta a CAUDA da palavra arrastada, mas esse
    #     corte passa pelo `minimo` como qualquer outro: cauda menor que
    #     `minimo` + `folga_palavra` (0,34s) e descartada, e o arrasto fica
    #     inteiro sem que nada diga.
    #
    #     MEDIDO nos 10 fluxos de work/, 76 caudas: 64 deixam 0,06-0,14s,
    #     que e a folga que `_respeita_palavras` devolve por construcao; 1
    #     sai inteira; e 11 sobrevivem 100% - todas com cauda de 0,25-0,30s,
    #     abaixo do piso geometrico. Mais 3 sobram por outro motivo (0,36 a
    #     0,47s, em regiao que o alinhador embaralhou). Sao essas 14 que
    #     esta linha acha, e era por elas que a categoria `cauda arrastada`
    #     perguntava as 76 no revisao.txt - perguntar todas escondia estas.
    #
    #     O limite e o mesmo 0,15s de `anotacoes_desobedecidas`, e pela
    #     mesma razao: a folga de borda de cada lado soma ~0,12s por
    #     desenho, e acima disso e outra coisa.
    for t in stream:
        if t["cut"] or not t.get("cauda"):
            continue
        ca, cb = t["cauda"]
        vivo = sum(max(0.0, min(cb, b) - max(ca, aa)) for aa, b in iv)
        if vivo > 0.15:
            diz("ARRASTO SOBREVIVE", ca, cb,
                f"{vivo:.2f}s de {cb-ca:.2f}s | {t['t'][:20]!r} | "
                f"{t.get('why','')[:20]}")

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

    # 5. Gagueira que sobra AUDIVEL. `marca_cacoetes` pergunta isso no
    #    fluxo, antes de qualquer geometria; aqui a pergunta e refeita
    #    sobre o corte PRONTO, porque metade destes casos nao existia no
    #    fluxo - foi o corte que criou a vizinhanca. MEDIDO: no IMG_0248
    #    a anotacao de 343,7-350,4s ("tres tentativas da mesma oracao")
    #    encosta o "que" de antes no "que" de depois, 0,12s de distancia
    #    na previa e 6,4s na fonte; no IMG_0247 a de 249,7s faz o mesmo
    #    com dois "e". Nenhuma das duas aparece em lugar nenhum hoje.
    #
    #    A distancia se mede na PREVIA, nao na fonte: e la que a pessoa
    #    ouve as duas palavras coladas. Por `chave_ac`, com acento, pela
    #    mesma razao da `marca_cacoetes` ("e é" nao e repeticao).
    vivas = []
    for t in stream:
        if t["cut"] or t["k"] != "palavra":
            continue
        largura = fim_util(t) - t["s"]
        if largura <= 0:
            continue
        vivo = sum(max(0.0, min(fim_util(t), b) - max(t["s"], aa))
                   for aa, b in iv)
        if vivo > largura * 0.5:
            vivas.append(t)
    for x, y in zip(vivas, vivas[1:]):
        k = chave_ac(x["t"])
        if not k or k != chave_ac(y["t"]) or chave(x["t"]) in ENFASE:
            continue
        if (para_previa(y["s"]) - para_previa(fim_util(x))
                > C["cacoete"]["gaguejada_max"]):
            continue
        diz("REPETICAO AUDIVEL", x["s"], y["e"],
            f"{x['t']!r} + {y['t']!r} | {y['s']-x['e']:.2f}s na fonte")

    # 6. Palavra mantida que, pelo relogio do WHISPER, esta enterrada dentro
    #    do corte. Os outros cinco itens medem o corte contra os tempos do
    #    ALINHADOR - os mesmos com que ele foi calculado. Quando e o
    #    alinhador que erra, todos eles concordam com o erro e nao ha o que
    #    reclamar; a unica segunda opiniao que existe neste programa e a do
    #    Whisper, e ela ja estava escrita em `enterradas_pelo_whisper`.
    #
    #    So que a resposta so aparecia no console de quem gerava audio ou
    #    video. Quem roda o `conferir` para nao depender do ouvido - que e o
    #    que a doc manda fazer ANTES de ouvir - nao via nenhuma. MEDIDO nos
    #    7 fluxos: 26 palavras, 2 a 7 por video, todas invisiveis aqui.
    #
    #    O tempo da FONTE aqui e o do Whisper, e nao o do alinhador: quando
    #    os dois discordam, e o do alinhador que esta sob suspeita. Na
    #    previa cai na emenda mais proxima, que e onde se ouve o buraco.
    for fund, frac, s0, e0, txt in enterradas_pelo_whisper(d, C, stream, iv):
        diz("PALAVRA ENTERRADA", s0, e0,
            f"{txt[:20]!r} | {fund:.1f}s dentro do corte, sobra {frac:.0%} "
            f"| tempo do Whisper, o alinhador discorda")

    # ORDEM DE ESCUTA, nao ordem de categoria. Quem confere ouve o .m4a do
    # comeco ao fim uma vez so; uma lista agrupada por tipo de defeito obriga
    # a pular para tras seis vezes.
    achados.sort()
    return achados, orfas


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
    C, d, stream, dur, db, hop, anot, mont, corr = _base(a.video, "conferir",
                                                         a.ignorar_transcript)
    iv = intervalos(stream, C, dur, db, hop, anot, mont)
    para_fonte, para_previa = _mapa_previa(iv)
    mantido = sum(b - x for x, b in iv)

    if getattr(a, "em", None):
        p = a.em.split(":")
        t = sum(float(x) * 60 ** i for i, x in enumerate(reversed(p)))
        print(f"[conferir] previa {hhmmss(t)} = fonte {para_fonte(t):.2f}s "
              f"({hhmmss(para_fonte(t))})")
        return

    pend = pendencias(stream, C, carrega_vocabulario(d))
    achados, orfas = achados_corte(stream, C, iv, anot, mont, pend,
                                   carrega_respostas(d), d)
    L = [f"# {Path(a.video).stem} | {len(iv)} emendas | "
         f"{hhmmss(mantido)} de {hhmmss(dur)}",
         "# ouca na ordem: a coluna PREVIA e o tempo no .m4a.",
         "# PENDENCIA SEM RESPOSTA = o revisao.txt perguntou e ninguem "
         "respondeu; o primeiro numero da coluna `fonte` e o `em` do "
         "respostas.json.",
         "# PENDENCIA NAO OBEDECIDA = voce respondeu 'sai' e o trecho "
         "continua tocando - o respostas.json registra, quem corta e a "
         "anotacao.",
         "# ANOTACAO NAO OBEDECIDA = voce mandou cortar e nao saiu. Sempre bug.",
         "# ANOTACAO REABERTA A MAO = voce mesmo trouxe de volta, no editor.",
         "# FALA CORTADA SOBREVIVE = o programa marcou para sair e continua ali.",
         "# ILHA CURTA = pouca coisa entre dois cortes; nem sempre e defeito.",
         "# PALAVRA RASPADA = borda comeu pedaco de palavra mantida.",
         "# REPETICAO AUDIVEL = a mesma palavra duas vezes seguidas no "
         "corte; o corte pode ter criado a gagueira.",
         "# ARRASTO SOBREVIVE = a poda de cauda arrastada nao coube no "
         "corte; o rabo continua audivel (conserto: montagem.json).",
         "# PALAVRA ENTERRADA = pelo relogio do WHISPER essa palavra "
         "mantida esta dentro do corte, longe da emenda. Quem errou ali e "
         "o alinhador, nao o corte: a borda foi ancorada num tempo que a "
         "palavra nao ocupa (conserto: montagem.json, ouvindo).",
         ""]
    for p, rot, x, y, txt in achados:
        L.append(f"{hhmmss(p)}  {rot:24s} fonte {x:8.2f}-{y:<8.2f} {txt}")
    if not achados:
        L.append("nada suspeito")

    for r in orfas:
        L.append(f"# nota: a resposta em {r['em']} nao casa com pendencia "
                 f"nenhuma - tempo errado, ou ela ja saiu no corte.")

    p = d / "conferir.txt"
    p.write_text("\n".join(L) + "\n", encoding="utf-8")
    print("\n".join(L))
    print(f"\n[conferir] {len(achados)} pontos para ouvir -> {p}"
          if achados else f"\n[conferir] nada suspeito -> {p}")


def cmd_editor(a):
    """Abre a transcricao no navegador para o ajuste fino.

    Mora em `editor.py`, e o import e aqui dentro de proposito: o
    editor carrega servidor HTTP e thread, e nenhuma das outras etapas
    precisa disso para rodar.

    Vem DEPOIS do passe de julgamento, nao no lugar dele. O `/revisar`
    decide o que sai lendo o texto inteiro; aqui se conserta a borda de
    milissegundo que so aparece ouvindo."""
    import editor
    editor.serve(a.video, a.porta, not a.sem_abrir)


def cmd_previa(a):
    _, d, dur, iv = _plano(a, "previa")
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
    """Mede o loudness e devolve (filtro da 2a passada, LUFS de entrada).

    Devolve o filtro INTEIRO, e nao so os parametros medidos, para que o
    `audio` e o `render` nao possam divergir: a previa em .m4a so serve para
    julgar volume enquanto receber o mesmo tratamento do .mov. O highpass e
    o `linear=true` ja estiveram escritos duas vezes, um em cada comando.

    LUFS de entrada = None quando a medicao falha; ai o filtro sai sem os
    `measured_*` e o loudnorm faz uma passada so, que e pior mas funciona."""
    alvo = (f"loudnorm=I={ca['lufs_alvo']}:TP={ca['true_peak']}"
            f":LRA={ca['lra']}")
    med = run([FFMPEG, "-hide_banner", "-nostats", "-i", str(entrada), "-vn",
               "-af", alvo + ":print_format=json", "-f", "null", "-"])
    try:
        blob = med.stderr[med.stderr.rindex("{"):]
        M = json.loads(blob[:blob.index("}") + 1])
        medido = (f":measured_I={M['input_i']}:measured_TP={M['input_tp']}"
                  f":measured_LRA={M['input_lra']}"
                  f":measured_thresh={M['input_thresh']}"
                  f":offset={M['target_offset']}")
        lufs = M["input_i"]
    except Exception:
        medido, lufs = "", None
    return (f"highpass=f={ca['highpass_hz']},{alvo}{medido}:linear=true",
            lufs)


def cmd_audio(a):
    """Corte so em audio, para julgar sem esperar render de video.

    O gargalo do render e decodificar 4K60 HEVC 10 bits; com `-vn` o ffmpeg
    nem toca no video. Da para ouvir a edicao inteira em segundos em vez de
    minutos, que e o que torna a calibracao viavel."""
    C, d, dur, iv = _plano(a, "audio")
    meta = jload(d / "meta.json")
    ca = C["audio"]
    fade = ca["fade"]

    L = [trecho_audio(i, x, y, fade) for i, (x, y) in enumerate(iv)]
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
        bruto.unlink(missing_ok=True)
        sys.exit("[audio] ffmpeg falhou no corte")

    af, entrada_lufs = _loudnorm(bruto, ca)
    if entrada_lufs is not None:
        print(f"[audio] {entrada_lufs} LUFS -> {ca['lufs_alvo']} LUFS")
    r = subprocess.run([FFMPEG, "-y", "-v", "error", "-i", str(bruto),
                        "-af", af, "-c:a", "aac", "-b:a", "192k",
                        "-ar", "48000", str(saida)])
    bruto.unlink(missing_ok=True)
    if r.returncode != 0:
        sys.exit("[audio] ffmpeg falhou na normalizacao")
    print(f"[audio] pronto: {saida}  ({hhmmss(mantido)}, "
          f"{saida.stat().st_size/1e6:.1f} MB)")


def _tempo_saida(t, iv):
    """Converte um instante de tempo de FONTE para tempo de SAIDA (depois
    do corte), pela mesma `iv` que o render usa (CLAUDE.md: 'o navegador
    nao calcula corte' - vale aqui do mesmo jeito, so que em vez do
    editor e o comando `cartas`). Um instante DENTRO de um trecho cortado
    nao existe na saida; devolve o ponto do trecho mantido mais proximo,
    porque uma carta nao pode aparecer num tempo que o corte apagou."""
    acumulado = 0.0
    for a, b in iv:
        if t < a:
            return acumulado
        if t <= b:
            return acumulado + (t - a)
        acumulado += b - a
    return acumulado


def _agrupa_spans_cartas(mencoes, iv, cc):
    """Mencoes (tempo de FONTE) -> spans de exibicao (tempo de SAIDA).
    Mapeia cada mencao para a saida ANTES de agrupar - fundir por
    proximidade so faz sentido no relogio que quem assiste ve, nao no da
    fonte (o corte pode ter comido o intervalo entre duas falas, ou
    esticado por causa de outra coisa cortada no meio).

    Pedido do Rafael em 02/09: a carta entra `atraso` DEPOIS do fim da
    mencao (nao antes - a antiga `antecipa` ia na direcao contraria) e
    fica ate `persistencia` depois do fim da ULTIMA mencao da mesma
    carta em sequencia continua. Cada mencao nova dentro da janela de
    persistencia da anterior funde na mesma exibicao e reinicia o
    relogio a partir dela - e por isso que a fusao (que olha so a
    proximidade entre mencoes) e o rabo depois da ultima (que decide
    quando a carta some) usam a MESMA constante agora: as duas
    respondem "ha quanto tempo a carta nao e mencionada?"."""
    pontos = sorted(
        (_tempo_saida(m["s"], iv), _tempo_saida(m["e"], iv), m["carta"])
        for m in mencoes)

    atraso, persist = cc["atraso"], cc["persistencia"]

    grupos = []
    for ini, fim, carta in pontos:
        if (grupos and grupos[-1]["carta"] == carta
                and ini - grupos[-1]["ultima_fim"] <= persist):
            grupos[-1]["ultima_fim"] = fim
        else:
            grupos.append({"carta": carta, "primeira_fim": fim,
                            "ultima_fim": fim})

    spans = [{"carta": g["carta"],
              "ini": g["primeira_fim"] + atraso,
              "fim": g["ultima_fim"] + persist}
             for g in grupos]

    # Sem empilhamento (layout 4 ficou de fora por decisao do Rafael - ver
    # plano): se a persistencia de uma carta invade a proxima, a de tras
    # cede - a carta que comecou depois e a que esta sendo falada agora.
    for i in range(len(spans) - 1):
        if spans[i]["fim"] > spans[i + 1]["ini"]:
            spans[i]["fim"] = spans[i + 1]["ini"]

    return [s for s in spans if s["fim"] > s["ini"]]


def _agrupa_cartas_enumeracao(mencoes, iv, cc, ce):
    """Mencoes (tempo de FONTE) -> eventos de exibicao em DUAS pilhas
    (esquerda/direita), tempo de SAIDA. So para o modo `--sem-principal`
    (ver CLAUDE.md / cmd_cartas): Mariana enumerando varias cartas ou
    TIPOS de carta em sequencia, sem nenhuma ser o tema do video - por
    isso nao ha carta fixa a esquerda, e cada mencao nova alterna de
    lado e empilha sobre a anterior do MESMO lado.

    "Sessao de enumeracao": mencoes cujo intervalo desde a anterior (de
    QUALQUER carta - a pergunta e "ha quanto tempo nenhuma carta e
    mencionada", nao mais por identidade como em `_agrupa_spans_cartas`)
    fica dentro de `persistencia` ficam juntas. A 1a mencao de uma sessao
    vai para esquerda/profundidade 0, a 2a para direita/0, a 3a para
    esquerda/1 (empilha sobre a 1a), a 4a para direita/1, e por diante.
    Uma sessao nova comeca do zero porque a anterior ja sumiu da tela ha
    `persistencia` segundos sem mencao - nao ha pilha para continuar.

    Mencao de uma carta que JA esta empilhada nesta sessao (pedido do
    Rafael em 02/09: nao pode repetir carta na tela ao mesmo tempo) e
    ignorada - nao gera evento, nao consome profundidade nem lado. So
    volta a poder aparecer numa sessao NOVA (a anterior ja sumiu de vez).

    Saida em duas fases, pedido do Rafael em 02/09: a pilha da DIREITA
    (a mais nova) fica so ate `persistencia` depois da ultima mencao da
    sessao e comeca a sumir; a da ESQUERDA (a mais velha) so comeca a
    sumir quando a direita ja tiver sumido de vez, `fade_saida` segundos
    depois. Risco conhecido, nao medido no unico video de teste: se uma
    sessao nova comecar antes da anterior terminar de sumir (gap so um
    pouco maior que `persistencia`), as duas pilhas se sobrepoem por um
    instante - a de cima (a nova, desenhada por cima) cobre a que ainda
    esta sumindo embaixo, entao o efeito visivel e pequeno, mas fica
    registrado aqui para quem for medir isso num video que bata nesse
    caso."""
    pontos = sorted(
        (_tempo_saida(m["s"], iv), _tempo_saida(m["e"], iv), m["carta"])
        for m in mencoes)

    persist, atraso, fade_saida = cc["persistencia"], cc["atraso"], ce["fade_saida"]

    sessoes = []
    for ini, fim, carta in pontos:
        if sessoes and ini - sessoes[-1][-1]["fim"] <= persist:
            sessoes[-1].append({"carta": carta, "ini": ini, "fim": fim})
        else:
            sessoes.append([{"carta": carta, "ini": ini, "fim": fim}])

    LADOS = ("esquerda", "direita")
    eventos, janelas_sessao = [], []
    for sessao in sessoes:
        ultima_fim = sessao[-1]["fim"]
        dir_ini = ultima_fim + persist
        dir_fim = dir_ini + fade_saida
        esq_ini = dir_fim
        esq_fim = esq_ini + fade_saida
        fade_por_lado = {"direita": (dir_ini, dir_fim),
                          "esquerda": (esq_ini, esq_fim)}

        prof = {"esquerda": 0, "direita": 0}
        # Pedido do Rafael em 02/09: a MESMA carta nao pode estar na tela
        # duas vezes ao mesmo tempo (acontecia quando ela voltava a ser
        # mencionada depois de ja empilhada nesta sessao - as duas
        # ocorrencias ficam visiveis juntas ate o fim da sessao, ver
        # docstring). `na_pilha` e por SESSAO, nao por lado: a pergunta e
        # "essa carta ja esta em alguma das duas pilhas", nao qual lado.
        # A mencao repetida e so ignorada - nao consome profundidade nem
        # alterna o lado (`n_exibidas`, nao `i`, decide o lado de quem
        # realmente aparece), como se ela nao tivesse acontecido para
        # fins de empilhamento. Sessao NOVA (a de tras ja sumiu da tela)
        # zera `na_pilha` de novo - repetir la e uma carta nova entrando.
        na_pilha, n_exibidas = set(), 0
        for ev in sessao:
            if ev["carta"] in na_pilha:
                continue
            lado = LADOS[n_exibidas % 2]
            fade_ini, fim_lado = fade_por_lado[lado]
            eventos.append({"carta": ev["carta"], "lado": lado,
                             "prof": prof[lado], "ini": ev["fim"] + atraso,
                             "fim": fim_lado, "fade_ini": fade_ini,
                             "fade_dur": fade_saida})
            prof[lado] += 1
            na_pilha.add(ev["carta"])
            n_exibidas += 1
        janelas_sessao.append((sessao[0]["fim"] + atraso, esq_fim))

    eventos = [e for e in eventos if e["fim"] > e["ini"]]
    return eventos, janelas_sessao


def _resolve_carta(texto):
    import baralho
    alvo = baralho.chave(texto)
    for c in baralho.catalogo():
        if baralho.chave(c["nome"]) == alvo:
            return c["nome"]
    sys.exit(f"[cartas] '{texto}' nao bate com nenhuma carta do catalogo "
             f"(veja baralho.py --carta imprime a lista)")


def _catalogo_img(nome):
    """Imagem para o OVERLAY, nao para o casador de mencao: le de
    tarodeyala-borda/ (copia com 1px de borda preta, gerada por
    `bordar_cartas.py`), nunca de tarodeyala/ - `baralho.catalogo()`
    continua apontando pro original porque e dali que vem o nome de
    arquivo que casa com a fala (ver docstring de baralho.py)."""
    import baralho
    for c in baralho.catalogo():
        if c["nome"] == nome:
            img = c["img"].parent.parent / "tarodeyala-borda" / c["img"].name
            if not img.exists():
                sys.exit(f"[cartas] falta borda para '{nome}' ({img}) - "
                         f"rode bordar_cartas.py")
            return img
    sys.exit(f"[cartas] carta '{nome}' sumiu do catalogo entre a deteccao "
             f"e o render - baralho completo/ mudou no meio do caminho?")


def _roda_previa_cartas(d, a, meta, gscript_hw, gscript_sw, entradas, mantido):
    """Ultimo trecho comum a `cmd_cartas`, os dois modos: ffmpeg com o
    grafo (QSV primeiro, software se aquele falhar) e o mpv, se pedido.
    Extraida porque so os grafos e as entradas diferem entre 'carta
    principal' e '--sem-principal' - o resto (por que 360p/15fps, por que
    dois grafos, por que QSV primeiro) e a mesma explicacao, nao duas."""
    saida = d / "cartas_previa.mp4"

    def _saidas(gscript):
        return (["-/filter_complex", str(gscript), "-map", "[vo]", "-map", "[ao]",
                 "-t", f"{mantido:.3f}",
                 "-c:v", "libx264", "-preset", "fast", "-crf", "28",
                 "-c:a", "aac", "-b:a", "128k", str(saida)])

    print(f"[cartas] gerando previa lo-res...", flush=True)
    # QSV primeiro: decode de software do IMG_0301 inteiro (5,5min) levou
    # mais de 30min so nesta etapa, contra minutos com QSV - MEDIDO no
    # checkpoint 4. Se a combinacao hwaccel+filtros nao colar nesta versao
    # do ffmpeg, cai para software antes de desistir - mesma rede de
    # seguranca do rosto.amostra_frames().
    r = subprocess.run([FFMPEG, "-y", "-v", "error", "-stats",
                        "-hwaccel", "qsv"] + entradas + _saidas(gscript_hw))
    if r.returncode != 0:
        r = subprocess.run([FFMPEG, "-y", "-v", "error", "-stats"]
                            + entradas + _saidas(gscript_sw))
    if r.returncode != 0 or not saida.exists():
        sys.exit("[cartas] ffmpeg falhou na previa - veja graph_cartas*.txt")
    print(f"[cartas] previa pronta: {saida}  "
          f"({saida.stat().st_size/1e6:.1f} MB)")
    mpv = _find_mpv()
    if mpv and not a.sem_abrir:
        subprocess.run([mpv, "--force-window=yes",
                        f"--force-media-title=previa cartas {Path(a.video).name}",
                        str(saida)])


def cmd_cartas(a):
    """Checkpoint 3 + 4 do plano 'adicionar cartas': acha as mencoes de
    carta na fala e renderiza uma previa barata (baixa resolucao) para
    aprovar antes do `render` de verdade bakear isso em qualidade final.
    Video sem carta reconhecida na fala nao escreve `cartas.json` nem
    gera previa - fica como generalistico, fora do escopo desta feature
    por ora.

    Dois modos, mutuamente exclusivos:

    - Padrao: decide uma carta PRINCIPAL (a mais mencionada, ou `--carta`)
      e fica fixa a esquerda a partir do fade de entrada; secundarias
      entram a direita quando mencionadas (`grafo_cartas`, layouts 1/3).

    - `--sem-principal`: pedido do Rafael em 02/09, para video que
      enumera VARIAS cartas ou TIPOS de carta (rainhas, reis, pajens...)
      sem nenhuma ser o tema - nao ha "a mais mencionada" que faca
      sentido fixar. Cada mencao alterna de lado e empilha sobre a
      anterior do mesmo lado (`grafo_cartas_enumeracao`, layout 4)."""
    import baralho
    C, d, stream, dur, db, hop, anot, mont, corr = _base(a.video, "cartas")
    iv = intervalos(stream, C, dur, db, hop, anot, mont)

    if a.carta and a.sem_principal:
        sys.exit("[cartas] --carta e --sem-principal se contradizem: um "
                 "fixa a principal, o outro diz que nao ha principal")

    # Casador so olha token que SOBREVIVE ao corte (stream[i]['cut'] ja
    # tem transcript.md/anotacoes/montagem aplicados por _base): uma
    # tentativa de fala cortada nao pode acender uma carta que ninguem
    # vai ouvir.
    vivos = [t for t in stream if not t.get("cut")]
    mencoes = baralho.acha_mencoes(vivos)

    if not mencoes:
        print(f"[cartas] nenhuma carta reconhecida na fala - "
              f"parece video generalistico, nada a fazer aqui")
        (d / "cartas.json").unlink(missing_ok=True)
        return

    meta = jload(d / "meta.json")
    w_fonte, h_fonte = meta["w"], meta["h"]

    import rosto
    rjson = d / "rosto.json"
    if not rjson.exists() or a.force:
        rosto.rastreia(Path(a.video), d)
    resumo_rosto = jload(rjson)["resumo"]

    if a.sem_principal:
        cc, ce = C["cartas"], C["cartas"]["enumeracao"]
        eventos, janelas_sessao = _agrupa_cartas_enumeracao(mencoes, iv, cc, ce)

        if not eventos:
            print("[cartas] nenhuma sequencia de enumeracao sobrou depois "
                  "do atraso/persistencia - o video fica no enquadramento "
                  "original o tempo todo")
            (d / "cartas.json").unlink(missing_ok=True)
            return

        print(f"[cartas] --sem-principal: {len(eventos)} carta(s) em "
              f"{len(janelas_sessao)} sequencia(s) de enumeracao "
              f"(tempo de SAIDA, ja cortado):")
        for e in eventos:
            print(f"  {hhmmss(e['ini'])} -> {hhmmss(e['fim'])}  "
                  f"{e['lado']:8s} prof {e['prof']}  {e['carta']}")

        cartas = {"modo": "enumeracao", "eventos": eventos,
                  "sessoes": [[a, b] for a, b in janelas_sessao]}
        jdump(cartas, d / "cartas.json")

        # Uma camada de overlay por POSICAO de pilha, nao por mencao - ver
        # a nota em `_agrupa_camadas_enumeracao` sobre por que isso e o
        # que faz esta previa terminar em minutos, nao quase uma hora.
        camadas = _agrupa_camadas_enumeracao(eventos)
        print(f"[cartas] {len(eventos)} mencao(oes) cabem em "
              f"{len(camadas)} posicao(oes) de pilha distintas "
              f"({len(camadas)} camada(s) de overlay)")
        imgs = [_catalogo_img(c["carta"]) for c in camadas]
        gscript_hw = d / "graph_cartas_qsv.txt"
        gscript_hw.write_text(
            grafo_cartas_enumeracao(iv, C, camadas, janelas_sessao,
                                     resumo_rosto, w_fonte, h_fonte, imgs,
                                     escala=360, fps=15, hw=True),
            encoding="utf-8")
        gscript_sw = d / "graph_cartas.txt"
        gscript_sw.write_text(
            grafo_cartas_enumeracao(iv, C, camadas, janelas_sessao,
                                     resumo_rosto, w_fonte, h_fonte, imgs,
                                     escala=360, fps=15, hw=False),
            encoding="utf-8")

        mantido = sum(b - x for x, b in iv)
        entradas = ["-i", str(meta["src"])]
        for img in imgs:
            entradas += ["-loop", "1", "-i", str(img)]

        _roda_previa_cartas(d, a, meta, gscript_hw, gscript_sw, entradas, mantido)
        return

    contagem = {}
    for m in mencoes:
        contagem[m["carta"]] = contagem.get(m["carta"], 0) + 1
    principal = _resolve_carta(a.carta) if a.carta \
        else max(contagem, key=contagem.get)

    print(f"[cartas] carta principal: {principal}"
          + ("" if a.carta else "  (auto: a mais mencionada)"))
    for carta, n in sorted(contagem.items(), key=lambda x: -x[1]):
        print(f"  {n}x  {carta}" + ("  <- principal" if carta == principal
                                     else ""))

    cc = C["cartas"]
    secundarias_menc = [m for m in mencoes if m["carta"] != principal]
    spans = _agrupa_spans_cartas(secundarias_menc, iv, cc)
    cartas = {"principal": principal, "fade_entrada": 2.0,
              "secundarias": spans}
    jdump(cartas, d / "cartas.json")

    if spans:
        print(f"[cartas] {len(spans)} exibicao(oes) de carta secundaria "
              f"(tempo de SAIDA, ja cortado):")
        for s in spans:
            print(f"  {hhmmss(s['ini'])} -> {hhmmss(s['fim'])}  {s['carta']}")
    else:
        print("[cartas] nenhuma carta secundaria mencionada nesta edicao - "
              "o video fica todo no layout 1")

    principal_img = _catalogo_img(principal)
    nomes_sec = list(dict.fromkeys(s["carta"] for s in spans))
    secundarias_imgs = [(nome, _catalogo_img(nome)) for nome in nomes_sec]

    # Dois grafos, nao um: com decode QSV o [0:v] ainda esta em memoria
    # de GPU quando chega no crop/overlay, e so `hw=True` poe o
    # `hwdownload` que isso pede - grafo errado para a chamada e erro de
    # pixel format, nao resultado errado (ver nota em grafo_cartas()).
    #
    # 360p/15fps, nao 480p/30: MEDIDO que decode e crop nao sao o custo
    # da previa (crop sozinho roda a 1.5x tempo real) - e o `overlay`
    # que e caro por QUADRO nesta versao do ffmpeg, com ou sem QSV, com
    # ou sem imagem de carta real (reproduzido ate com uma cor lisa de
    # lavfi). Metade dos quadros = metade do trabalho de overlay: e o
    # unico botao que realmente mexe no tempo da previa.
    gscript_hw = d / "graph_cartas_qsv.txt"
    gscript_hw.write_text(
        grafo_cartas(iv, C, cartas, resumo_rosto, w_fonte, h_fonte,
                     secundarias_imgs, escala=360, fps=15, hw=True),
        encoding="utf-8")
    gscript_sw = d / "graph_cartas.txt"
    gscript_sw.write_text(
        grafo_cartas(iv, C, cartas, resumo_rosto, w_fonte, h_fonte,
                     secundarias_imgs, escala=360, fps=15, hw=False),
        encoding="utf-8")

    mantido = sum(b - x for x, b in iv)
    entradas = (["-i", str(meta["src"]), "-loop", "1", "-i", str(principal_img)]
                + [x for _, p in secundarias_imgs
                   for x in ("-loop", "1", "-i", str(p))])

    _roda_previa_cartas(d, a, meta, gscript_hw, gscript_sw, entradas, mantido)


def cmd_render(a):
    C, d, dur, iv = _plano(a, "render")
    cr = C["render"]
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
    af, entrada_lufs = _loudnorm(corte, ca)
    print(f"[render] entrada {entrada_lufs} LUFS -> alvo {ca['lufs_alvo']} LUFS"
          if entrada_lufs is not None
          else "[render] medicao falhou; loudnorm em passada unica")
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

# A ordem do pipeline, uma vez so. Ja morou em tres lugares - a lista de
# nomes do `lote`, a lista de pares logo abaixo dela e o `choices` do
# argparse -, e acrescentar etapa exigia lembrar dos tres.
ETAPAS = [("ingest", cmd_ingest), ("transcribe", cmd_transcribe),
          ("align", cmd_align), ("analyze", cmd_analyze),
          ("draft", cmd_draft), ("render", cmd_render)]


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

    ate = [n for n, _ in ETAPAS].index(a.ate)
    print(f"[lote] {len(vids)} videos, ate a etapa '{a.ate}'\n")
    falhas = []
    for i, v in enumerate(vids, 1):
        print(f"{'='*70}\n[lote] {i}/{len(vids)}  {v.name}\n{'='*70}",
              flush=True)
        ns = argparse.Namespace(video=str(v), force=a.force, model=a.model,
                                out=None, dry_run=False,
                                ignorar_transcript=a.ignorar_transcript)
        try:
            for _, fn in ETAPAS[:ate + 1]:
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
    for nome, fn in ETAPAS[:-1]:          # render tem parser proprio
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
    cf.add_argument("--ignorar-transcript", action="store_true")
    cf.set_defaults(fn=cmd_conferir)

    au = sub.add_parser("audio", help="corte so em audio, rapido, para julgar")
    au.add_argument("video")
    au.add_argument("--out", default=None)
    au.add_argument("--ignorar-transcript", action="store_true")
    au.set_defaults(fn=cmd_audio)

    ed = sub.add_parser("editor",
                        help="ajusta o corte pela transcricao, no navegador")
    ed.add_argument("video")
    ed.add_argument("--porta", type=int, default=8731)
    ed.add_argument("--sem-abrir", action="store_true")
    ed.set_defaults(fn=cmd_editor)

    v = sub.add_parser("previa", help="ve o corte no mpv sem renderizar")
    v.add_argument("video")
    v.add_argument("--ignorar-transcript", action="store_true")
    v.set_defaults(fn=cmd_previa)

    ct = sub.add_parser("cartas",
                        help="adiciona overlay de carta + reenquadramento; "
                             "gera cartas.json e uma previa lo-res")
    ct.add_argument("video")
    ct.add_argument("--carta", default=None,
                    help="carta principal (ex.: '7 de espadas'); "
                         "sem isto, usa a mais mencionada na fala")
    ct.add_argument("--sem-principal", action="store_true",
                    help="layout 4 (empilhamento): sem carta fixa, para "
                         "video que enumera varias cartas/tipos sem "
                         "nenhuma ser o tema (ex.: rainhas, reis, pajens)")
    ct.add_argument("--force", action="store_true",
                    help="refaz o rastreio de rosto mesmo se ja tiver cache")
    ct.add_argument("--sem-abrir", action="store_true",
                    help="gera a previa mas nao abre o mpv sozinho")
    ct.set_defaults(fn=cmd_cartas)

    r = sub.add_parser("render")
    r.add_argument("video")
    r.add_argument("--out", default=None)
    r.add_argument("--dry-run", action="store_true")
    r.add_argument("--ignorar-transcript", action="store_true",
                   help="usa so as propostas automaticas, ignora transcript.md")
    r.set_defaults(fn=cmd_render)

    p = sub.add_parser("run")
    p.add_argument("video")
    p.add_argument("--force", action="store_true")
    p.add_argument("--model", default=None)

    def _todos(a):
        for _, fn in ETAPAS[:-1]:
            fn(a)
    p.set_defaults(fn=_todos)

    b = sub.add_parser("lote", help="processa uma pasta inteira")
    b.add_argument("pasta")
    b.add_argument("--ate", default="draft",
                   choices=[n for n, _ in ETAPAS],
                   help="etapa final (padrao: draft, para revisar antes)")
    b.add_argument("--pular", default=None,
                   help="ignora arquivos cujo nome contenha estes trechos, "
                        "separados por virgula")
    b.add_argument("--force", action="store_true")
    b.add_argument("--model", default=None)
    b.add_argument("--ignorar-transcript", action="store_true")
    b.set_defaults(fn=cmd_lote)

    a = ap.parse_args()
    a.fn(a)


if __name__ == "__main__":
    main()
