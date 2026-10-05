# Making /preparar more accurate: what I found and what I changed

Date: 4 October 2026. Written for Rafael.

This file explains, step by step, every decision I made in this session.
Each step says what I did, why, and what number backed it up.
Technical names (file and function names) only appear at the end of a
step, under "Where it lives", for whoever opens the code later.

## The short version

- I measured /preparar against your 22 hand-edited videos.
- It was cutting real speech you had to put back: **141 seconds** in total.
  The fixes bring that down to **98 seconds (−30%)**.
- Long silences left in the cut (0.7 s or more): **48 → 35 (−27%)**.
- The giant "éééé…" lines over real speech: found the cause. The program
  now recovers that speech and never shows more than "ééé".
- Your edited videos: never touched, and now protected against
  accidental re-analysis.
- **What did not improve: the share of subtitle cards that need no fix
  stays at 54%.** About 40% of cards have a word the recognizer misheard
  or skipped, and no cutting rule can fix that. I tested a bigger
  recognizer and a new instruction to it. Both made things worse, so
  neither was kept.
- **90% is not reachable with these changes.** Step 8 says honestly what
  it would take.

---

## Step 1. Decide what "correct" means

You asked me to treat your hand-edited videos as the best possible output.

The video list in the tarocut window paints a video light green when you
tick "ready for export" in the editor's cards mode. I used that flag as the
index. **22 videos** have it:

- Paus: 2, 3, 4, 5, 6, 7, 8, 9 and Ás
- Espadas: 2, 3, 4, 5, 6, 7 and Cavaleira
- Ouros: 2, 3, 6, 7, 8
- Módulo de Tiragens

For each of those 22 videos there are two versions of the truth:

1. **What /preparar handed you**: the automatic cut, plus the cuts the
   judgment pass (Claude, in /revisar) wrote.
2. **What you finished with**: that, plus everything you changed in the
   editor: words hidden or restored, time trims, word corrections and
   rewritten subtitle cards.

I rebuilt both versions from the files on disk. Comparing them, word by
word and second by second, tells me exactly where /preparar was wrong.

I never wrote anything into your `work/` folders during this measurement.
All experiments ran in a separate sandbox.

## Step 2. Measure how good /preparar is today

The unit I counted is the **subtitle card**: one line of subtitles, as
your editor's cards mode shows it.

Across the 22 videos there are **1,213 cards**. Comparing what /preparar
delivered against your final version:

| | share of cards |
|---|---|
| untouched (you changed nothing) | **54%** |
| small fix (1-2 words) | **30%** |
| big fix (3+ words) | **16%** |

That matches your own estimate fairly well (you said roughly 70 / 10 / 20).

Measured in time, /preparar:

- cut **141 seconds** of speech that you kept (put back by you), and
- kept **440 seconds** that you cut (about 20 s per video).

## Step 3. Sort every fix you made into a cause

I split your fixes into three kinds.

**A. Cut fixes: speech you had to restore.** I scored every automatic
rule by how often you undid it:

| rule (what it cuts) | times it cut | times you undid it | speech lost |
|---|---|---|---|
| "collapsed restart" (a re-heard repeat) | 75 | **30 (40%)** | 69 s |
| "stretched letters" (words ending in "…") | 55 | 9 (16%) | 7 s |
| judgment pass (Claude, /revisar) | 813 | 50 (6%) | 30 s |
| "abandoned attempt" | 510 | 29 (6%) | 13 s |
| pauses, breaths, "sound without text" | ~1,100 | under 2% | small |

One rule stands out: **"collapsed restart" was wrong 40% of the time**.
It is also the biggest single source of lost speech. Of the 130 seconds of
speech you restored in total, 60 came from this one rule.

**B. Cut fixes: things you had to remove.** About 2 minutes were restarts
where she literally repeats herself and the program kept both copies. 17
seconds were filler words. 18 seconds were "words" sitting on silence.

**C. Text fixes: the subtitle text was wrong.** About 40% of all cards had
a recognition error:

- a word misheard as a similar word ("sólida" for "solo", "ceste" for
  "sete"): 17% of cards;
- a different word altogether ("Alavacena" for "Yala, uma cena", "ars",
  "ar" and "ágil" for "Ás"): 9%;
- a word missing ("que não tá dominando", "né", "assim"): 20%;
- a word that wasn't said: 5%.

So, to get to 90% clean cards, both the cutting **and** the recognition
have to improve. Neither one alone gets there.

## Step 4. Find out WHY each of these happens

### Cause 1: we told the recognizer to write "é… éé…", and it got stuck

The recognizer (Whisper) receives a short instruction before it starts.
Ours said, literally: *"transcribe hesitations: é... éé... ahn, hmm"*.

That example is dangerous. When the speaker holds a long vowel, Whisper
sometimes falls into a loop and keeps writing "ééééé…" until it runs out
of room, **straight over the real speech that follows**.

I tested it on "Ás de Espadas" (seconds 30 to 75):

- with the current instruction: *"Na sua forma mais… éééééééé…"* (200
  letters) covering **25 seconds of real teaching**;
- with the "é… éé…" example removed: *"na sua forma mais profunda, mais
  potente. Então ela fala do intelecto…"*, correct and complete.

This is the long "ééééé" you saw running off the screen. It happened in 3
videos: Ás de Espadas (20 s), 9 de Espadas (26 s) and Rainha de Copas
(16 s; that one isn't prepared yet, so it would have hit you next).

A milder version is everywhere: when the program re-listens to a gap and
hears a held vowel, the recognizer writes "Eeeeeee…" up to 889 letters
long. Those are usually cut, so you rarely see them, but when one gets
restored it shows up as a giant line.

### Cause 2: the "collapsed restart" rule throws away the good copy

When she restarts a sentence, for example *"que não está… que não está
dominando"*, Whisper writes it **once**. The aligner (the step that finds
where each word sits in time) puts those words on the **first** attempt,
the abandoned one. The second attempt, the good one, is left over as
"sound without text". The program re-listens to it, finds it repeats
nearby words, and cuts it as an echo.

So the video kept the broken attempt and lost the finished sentence. That
is the 40% error in the table above.

### Cause 3: silence hidden inside words

The aligner refuses some words when it isn't confident. Those words fall
back to Whisper's own timing, which is sloppy and often stretches the word
over the pause next to it. Example from 5 de Copas: "Então," lasted 0.96 s,
but she said it in about 0.35 s. The other 0.6 s were silence.

The program shortens pauses only **between** words. A pause **inside** a
word is invisible to it. Across all ~60 videos: **574 words** carry 0.3 s
or more of silence at an edge, **7.7 minutes** in total. 98% of them are
words the aligner had refused.

In the prepared videos, 106 of the 150 leftover silences of 0.7 s or
more lived inside a word. These are the "silences that should have been
removed".

### Cause 4: the recognizer itself is weak

The recognizer in use is Whisper "small", the second-smallest of the
family. It mishears unusual words: card names ("Ás"), Portuguese place and
people names ("Samaúma", "Yala"), and fast speech.

I tested the larger "large-v3-turbo" model (one-time download of 1.6 GB,
with your permission). The test ran the whole chain (transcribe, align,
cut) on 4 of your edited videos and scored the result against your
edits:

| | cards needing no fix |
|---|---|
| what you got (small model) | 48% |
| small model, new code | 46-48% |
| **large model, new code** | **38%** |

Why the bigger model loses here:

1. It does hear better: about **20% fewer misheard words** ("sete" instead
   of "ceste", "meio zonzo" instead of "meizonso").
2. But it also **tidies up her restarts**. Where she said "de uma forma
   até… de uma forma até… de uma forma até mais ou menos", the small model
   wrote all three attempts and the large one wrote one. This program
   needs to *see* restarts in the text to cut them, so tidier text makes
   a messier cut.
3. It makes the re-listening step about **4 times slower** (7 minutes for
   a 6-minute video).

A caveat on fairness: your final subtitles were made by correcting the
small model's text, so they lean toward its wording. I corrected for the
obvious part ("tá" vs "está", "pra" vs "para"), and the large model still
lost, 38% vs 48%.

Decision: **not adopted.** The model is still downloaded on your disk;
nothing uses it.

### Cause 5: the judgment pass sometimes cuts real speech

When rescued text came out as gibberish ("Adora!", "de uma montantisma
longe…"), Claude cut it. But the audio under it was real speech that the
recognizer had misheard. Gibberish text does not mean there was no speech.
A better recognizer helps here more than better instructions.

## Step 5. What I changed

Each change below comes with the test that proves it, added to the
project's own test file, so a future change can't quietly undo it.

### Change 1: keep the finished sentence, cut the abandoned attempt

When the re-heard piece repeats **two or more words** of what came just
before it, the program now keeps that piece (the finished retake) and
cuts the earlier copy (the abandoned attempt).

Long words (7 letters or more) count double. So "elaborar… é elaborar"
counts as a retake, while "então… então" doesn't.

When the re-heard piece repeats only one word, repeats what comes *after*
it, or is itself a mess, nothing changes: it is still cut, as before.
"A mess" means it trails off twice ("essa… essa… que representa…"), has
a filler glued to punctuation ("é uma, é, do que a…"), or stutters inside
itself ("que é o elemento que é o…").

I tested this against all 78 cases in your edited videos before writing
it into the program:

| | matches what you did |
|---|---|
| old rule (always cut the re-heard piece) | 32 of 78 |
| new rule | **60 of 78** |

There are 7 cases where the new rule is wrong and the old one was right.
6 of them are identical repeats ("esse aspecto" / "esse aspecto"), where
keeping either copy sounds the same.

When this swap happens, the question list for the judgment pass now says
which copy was cut, so Claude can confirm it instead of rediscovering it.

Where it lives: `marca_ecos` and `retomada_antes` in `tarocut.py`;
`[cacoete].retomada_min = 2` in `config.toml`.

### Change 2: take silence out of words

Before measuring pauses, every word is trimmed to where there is actually
sound. A silent stretch of 0.3 s or more at a word's edge comes off, and
the word keeps 0.06 s of margin on each side. That margin is the aligner's
measured error, and it covers soft endings like a final "s".

The silence that comes off becomes an ordinary gap. Gaps follow the same
rules as always: under 0.75 s they're left alone (breathing), over that
they're shortened to 0.45 s.

One exception, found by measurement. Words with a dragged ending
("daaas…") are left whole, because another rule already cuts the drag
and the silence after it together. My first version trimmed those too,
and on the first 7 videos it gave back 6 seconds of dragging you had cut.
I caught it in the comparison and fixed it before the final run.

Where it lives: `encolhe_ao_som` in `tarocut.py`;
`[silencio].borda_muda = 0.30` in `config.toml`.

### Change 3: stop the "ééééé" at its source, and catch the old ones

Three parts:

1. **Tried and reverted: removing "é… éé…" from the instruction.** It
   fixes the loop at its source. But measured over the whole chain (same
   code, 4 videos), it made things worse: 46% clean cards instead of 48%,
   19 seconds more leftover material, and more wrong words (11.7% →
   14.7%). The loop only happens in about 3 of 60 videos, so the
   instruction went back to its original wording, and part 2 below
   handles the loop instead.
2. **Safety net that rescues the speech after a loop.**
   If a "word" made of one stretched letter lasts far longer than its
   letters explain (3 s or more, and 4 times longer than expected), the
   program no longer cuts it as a hesitation. It re-listens to that
   stretch on its own, without the instruction. If it hears real speech,
   the speech comes back, cut at its own pauses into normal pieces. I
   checked this on the real "Ás de Espadas" audio: the 20-second "ééé"
   becomes a 4-second pause (cut) and 16.5 seconds of teaching (kept, with
   its text).
3. **No word longer than three repeated letters reaches the screen.**
   "Eeeeeee…" (up to 889 letters) is now written "Eee…" in the
   transcript, in the editor and in the subtitles. This applies to old
   videos too.

Where it lives: `_texto_nao_cabe`, `resolve_suspeitos` and
`encurta_alongamento` in `tarocut.py`; the display part also in
`editor.py`. The note about the reverted instruction is in
`initial_prompt`.

### Change 4: each step of `run` runs on its own

`run` (and `lote`) used to do all five steps inside one program. The
recognizer models piled up in memory, and on this laptop (7.9 GB) that
made the transcription or the analysis crash at random. Your earlier
sessions logged this several times.

Now each step runs as its own process. When a step ends, all its memory
goes back to the computer. If a step fails, `run` stops right there and
says which step failed. Before, a failed analysis could let the next step
write a fresh transcript on top of stale data.

Where it lives: `etapa_isolada` in `tarocut.py`.

### Change 5: your edited videos are protected from re-analysis

You asked me not to touch your edited videos. I didn't: during this whole
session, the only files that changed inside `work/` were two report files
(`revisao.txt` and `conferir.txt`) of the three calibration videos
IMG_0246/0247/0248, which the project's own test suite rewrites every time
it runs.

But your message pointed at an older risk. If `/preparar`, `run` or
`lote` were ever run again on an edited video, the analysis step would
rebuild the word list. Your word decisions, corrections and rewritten
subtitles would stop matching it, and the video would refuse to export
until you redid them.

Now the analysis step **refuses** to run on any video with editor
decisions, lists what it is protecting, and changes nothing. Tested on a
copy of "7 de Paus": refused, and all seven files came out byte-for-byte
identical.

Where it lives: `edicao_a_mao` in `tarocut.py`. Only `analyze
--apesar-da-edicao` (typed on purpose, one step only) overrides it;
`run` and `lote` never pass it.

### Change 6: two lessons for the judgment pass

The `/revisar` instructions now say:

- Gibberish rescued text is not proof there was no speech. Check how long
  it lasts: half a second of vowel is a hesitation, two seconds of
  syllables is a misheard sentence.
- When the question list says a restart was swapped, confirm the swap
  instead of undoing it.

### Change 7: "Ás" misheard as "ars", "ásio", "ágil" or "was"

When one of those four is followed by "de Copas/Espadas/Ouros/Paus", it
is corrected to "Ás de …". None of them is real Portuguese in that
position. "ar de espadas" is deliberately **not** corrected, because
"ar" (air) is the element of Espadas in these lessons.

Where it lives: `lexico.py` (which writes `lexicon/terms.txt`).

## Step 6. The final result

I re-ran the automatic part of /preparar with the new code on all 22
videos, in a sandbox copy, using the same transcriptions you had. I
scored it against your edits exactly like in Step 2:

| | before | after |
|---|---|---|
| real speech cut that you put back | 141 s | **98 s (−30%)** |
| material kept that you cut | 440 s | 427 s (−3%) |
| long silences left (0.7 s or more) | 48 (48 s) | **35 (36 s)** |
| silences of 1 s or more | 19 (25 s) | 13 (19 s) |
| subtitle cards needing no fix | 54.2% | 54.1% |
| cards needing a big fix (3+ words) | 16.0% | 17.2% |

What this means in practice:

- **You'll restore less speech by hand**, about 2 seconds less per
  video. Those were the worst kind of error (the finished sentence
  missing).
- **You'll trim fewer leftover silences.**
- **You'll fix subtitle text about as often as before.** The text errors
  come from the recognizer, and these changes don't touch the
  recognizer.

Two honest caveats on these numbers:

1. The judgment-pass cuts in the test are the ones Claude wrote for the
   *old* output. In a real /preparar run, Claude would see the new output
   and judge it fresh, so the real result should be slightly better than
   this table.
2. The analysis step isn't perfectly repeatable: re-listening to the same
   gap can produce slightly different text from one run to the next. A
   few seconds either way in this table are noise.

## Step 7. What I did NOT change, and why

**The "stretched letters" rule (words ending in "…").** It looked like a
bug: it fires on the recognizer's "…", not on real stretched letters. But
in your edits a trailing "…" usually marks an abandoned word. You kept
46 of its cuts and undid 9 (7 seconds in total). Removing it would trade
7 seconds of fixes for 46 new leftovers.

**The "abandoned attempt" rule.** You undid 6% of its cuts (13 s). The
restarts it misses (about 2 minutes in your edits) are fast restarts with
no pause, like "a gente não tem, a gente não vai retratar". Those have
exactly the same shape as deliberate repetition ("a gente tem os
problemas, a gente tem as dificuldades"), which must never be cut. Only
meaning separates them, so they stay with the judgment pass.

**Pause length.** You trimmed about 80 short silences by hand (0.2-0.3 s
each, 24 s in total over 22 videos). That's about 4 per video, not a
pattern strong enough to justify shortening every pause in every video.

**Your edited videos.** Untouched, as you asked. Every measurement used
copies. The new code will only matter for videos you prepare from now on,
plus any already-prepared video you choose to re-prepare (see Step 9).

## Step 8. Is 90% reachable?

Not with what's in this session. Here is the honest arithmetic:

- 54% of cards come out clean.
- About 40% of cards have a recognition error (misheard, missing or extra
  word). Cutting rules can't fix those.
- About 22% have a cut error (some cards have both).

Even if every cut error vanished, recognition errors alone would cap the
clean-card share near 60%. To get to 90%, the text itself has to get
much better. The options I see, none of them tested yet:

1. **Let the judgment pass rewrite subtitle text**, not just cut. Claude
   reads the whole lesson and knows the card. Many misheard words
   ("Alavacena" → "Yala, uma cena") are obvious to a reader. Today it
   only decides what to cut. This acts directly on the 40%, and it's the
   cheapest to try.
2. **A second opinion from the aligner's own speech model.** It already
   listens to every word to time it. Your earlier sessions found that
   letting it transcribe freely catches misplaced words and restarts the
   recognizer hid. Turning that into an automatic check is real work,
   perhaps a whole session.
3. **A stronger recognizer that keeps restarts.** The one I tested cleans
   them up. On this laptop anything bigger is also slow.

## Step 9. What you need to do

**Nothing, for the videos you've already edited.** They are untouched and
now protected.

**For the videos not prepared yet** (Ás de Ouros, Elemento Fogo, Rainha de
Copas, Rainha de Paus, Rei de Copas, Rei de Paus, Citação Contracolonia,
Significado Geral das Cartas): just run `/preparar` or `/fila` as usual.
The new code is used automatically. Rainha de Copas has a 16-second "ééé"
loop over real speech; the safety net from Change 3 will recover it.

**For videos prepared but not yet edited** (about 25 of them): your
choice. Re-preparing redoes the judgment pass, which costs a session per
video. If you're about to edit one anyway, re-preparing first is likely
to save you editing time.

**Restart the editor** if it's open, so it picks up the "Eee…"
display fix (it's running the old code right now).

**Nothing is committed to git.** All changes are in your working copy,
next to the uncommitted changes you already had. Review them with
`git diff` and commit when you're happy.
