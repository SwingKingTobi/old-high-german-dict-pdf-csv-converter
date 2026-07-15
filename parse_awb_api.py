"""
Parser für Einträge des Althochdeutschen Wörterbuchs (AWB) - Version 2,
basierend auf der offiziellen JSON-API statt auf PDF-Export.

Zwei Endpunkte werden genutzt:

1) Lemma-Liste (Index/Pagination):
   https://awb-api.saw-leipzig.de/dictionaries/AWB/lemmata/lemid/{lemid}/{n}/json
   Liefert bis zu {n} Lemmata ab {lemid}, u.a. mit "formid" - der ID, die
   man für Endpunkt 2 braucht.

2) Voller Artikel, token-weise (ein Objekt pro "Wort" mit exaktem Leerzeichen
   und einem "elementtype", der die Struktur markiert - u.a. "lemma",
   "gramgrp" (Flexion), "startformblock"/"endformblock" (Formen-/Belegblock),
   "startbedeutungsblock"/"endbedeutungsblock" (Bedeutungs-/Zitatblock)):
   https://awb-api.saw-leipzig.de/dictionaries/AWB/articles/{formid}/formid

Der große Vorteil ggü. dem PDF-Weg: Die Wörter kommen bereits exakt
formatiert (inkl. korrekter Leerzeichen) und die Blockgrenzen sind explizit
markiert - wir müssen sie nicht mehr per Regex heuristisch raten.
"""
import re
import csv
import json
import os
import sys
import html
import urllib.request

API_BASE = "https://awb-api.saw-leipzig.de/dictionaries/AWB"

CASE_RE = r'(?:nom|gen|dat|acc|instr)\.'
NUM_RE = r'(?:sg|pl)\.'


# ---------------------------------------------------------------------------
# API-Zugriff (braucht Internet)
# ---------------------------------------------------------------------------

def fetch_json(url):
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    with urllib.request.urlopen(req) as resp:
        return json.loads(resp.read().decode('utf-8'))


def fetch_lemma_list(start_lemid, count=100, dictionary='AWB'):
    """Liste von Lemmata ab start_lemid, u.a. mit 'formid' für fetch_article()."""
    url = f"{API_BASE}/lemmata/lemid/{start_lemid}/{count}/json"
    return fetch_json(url)


def fetch_article(formid, dictionary='AWB'):
    """Voller Artikel als Liste von Wort-Token-Dicts."""
    url = f"{API_BASE}/articles/{formid}/formid"
    return fetch_json(url)


def load_article_from_file(path):
    with open(path, encoding='utf-8') as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Rekonstruktion: aus der Token-Liste den exakten Text plus Blockgrenzen bauen
# ---------------------------------------------------------------------------

IGNORED_BLOCK_PREFIXES = ('kommentarblock',)


# elementtypes, deren "word"-Feld nur ein UI-Hyperlink-Label ist (Verweis auf
# ein anderes Wörterbuch wie Lexer, MWB, DWB, oder auf einen anderen AWB-
# Artikel) und NICHT im gedruckten Wörterbuchtext erscheint. Diese Wörter
# dürfen nicht in full_text landen, sonst kleben sie an das nächste Wort
# (z.B. "MWBaffe" statt "affe", "Lexerâbandrôt" statt "âbandrôt").
LINK_LABEL_ELEMENTTYPES = {'linkstart', 'idlinkstart'}


def reconstruct_entry(words):
    """
    Baut aus der Token-Liste:
      - lemma: das Lemma-Wort
      - flexion: die Flexion (verkettete "gramgrp"-Wörter)
      - full_text: der exakte, korrekt formatierte Artikeltext
      - blocks: dict Blockname -> Liste von (start, end)-Zeichenoffsets in full_text
    """
    parts = []
    pos = 0
    open_markers = {}
    blocks = {}
    lemma_parts = []
    gramgrp_parts = []

    for w in words:
        et = w.get('elementtype', '') or ''
        word = w.get('word', '') or ''
        # Die API liefert Sonderzeichen als HTML-Entitäten (z.B. "&#x00e2;"
        # für "â", "&#x2014;" für "—") - hier dekodieren wir sie zu echten
        # Unicode-Zeichen.
        if '&' in word:
            word = html.unescape(word)

        if et in LINK_LABEL_ELEMENTTYPES:
            # Nur das Link-Label selbst überspringen (z.B. "MWB", "Lexer",
            # "AWB") - Start-/Endmarker trotzdem normal verarbeiten, damit
            # eine evtl. offene Blockklammer nicht durcheinanderkommt.
            word = ''

        if et == 'lemma':
            lemma_parts.append(word)
        if et == 'gramgrp':
            gramgrp_parts.append(word)

        if et.startswith('start'):
            name = et[len('start'):]
            if not any(name.startswith(p) for p in IGNORED_BLOCK_PREFIXES):
                open_markers.setdefault(name, []).append(pos)
        elif et.startswith('end'):
            name = et[len('end'):]
            starts = open_markers.get(name)
            if starts:
                start = starts.pop()
                blocks.setdefault(name, []).append((start, pos))

        parts.append(word)
        pos += len(word)

    full_text = ''.join(parts)
    return {
        'lemma': ''.join(lemma_parts).strip(),
        'flexion': ''.join(gramgrp_parts).strip(),
        'full_text': full_text,
        'blocks': blocks,
    }


def parse_head_text(head_text):
    """
    Extrahiert mhd./nhd. Übersetzung aus dem rekonstruierten Kopftext.
    Trennt dabei auch eine evtl. vorhandene mhd.-Flexion ab, die direkt
    hinter dem mhd.-Wort steht (z.B. "mhd. âbandrôt st. m. n., nhd. ...").
    Diese Flexion ist nicht immer vorhanden.
    """
    info = {'Mhd_Lemma': '', 'mhd_Flexion': '', 'Nhd_UES': ''}
    m = re.search(r'mhd\.\s*(?P<mhd>.*?)\s*nhd\.\s*(?P<nhd>[^;]+)', head_text)
    if m:
        mhd_full = (m.group('mhd') or '').strip()
        if mhd_full.endswith(','):
            mhd_full = mhd_full[:-1].strip()
        if mhd_full:
            # Wort + optionale Flexion, z.B. "âbandrôt st. m. n."
            wm = re.match(
                r'^(?P<word>\S+)(?:\s+(?P<flex>(?:[a-zäöüß]{1,7}\.\s*)+))?$',
                mhd_full)
            if wm:
                info['Mhd_Lemma'] = wm.group('word')
                info['mhd_Flexion'] = (wm.group('flex') or '').strip()
            else:
                info['Mhd_Lemma'] = mhd_full
        info['Nhd_UES'] = m.group('nhd').strip()
    return info


def guess_komplexitaet(lemma):
    if '-' in lemma or len(lemma) >= 10:
        return ''
    return 'simplex'


# ---------------------------------------------------------------------------
# Formen-/Belegblock-Parsing (Logik aus der PDF-Version übernommen - arbeitet
# jetzt auf exakt sauberem Text statt auf PDF-Extraktion)
# ---------------------------------------------------------------------------

ENTRY_RE = re.compile(
    r"""
    (?P<dass>dass\.)?\s*
    (?P<form>-\]|-[A-Za-zäöüßÄÖÜ]+)?\s*
    (?P<abbr>[A-ZÄÖÜ][A-Za-zäöüß]{0,7})?\s*
    (?P<ref>\d+(?:,\d+)+(?:\s*=\s*[A-Za-zäöüßÄÖÜ]+\s*[\d,]+)?)
    \s*(?:\((?P<paren>[^)]*)\))?
    \s*(?:\[(?P<brack>[^\]]*)\])?
    \s*\.?
    """,
    re.VERBOSE,
)

STEM_DECL_RE = re.compile(
    r'^(?P<stem>[A-Za-zäöüßÄÖÜ]+)(?P<parens>\s*\([^)]*\))?(?P<dash>-)?\s*:\s*(?P<rest>.*)$')


def parse_forms_block(forms_block):
    rows = []
    last_band = {}
    current_stem = ''
    current_kasus, current_numerus = '', ''
    current_form = ''
    current_abbr = ''

    chunks = [c.strip() for c in re.split(r'—|;', forms_block) if c.strip()]

    for chunk in chunks:
        stem_m = STEM_DECL_RE.match(chunk)
        if stem_m:
            current_stem = stem_m.group('stem') + ('-' if stem_m.group('dash') else '')
            current_form = current_stem.rstrip('-')
            chunk = stem_m.group('rest')

        label_m = re.match(r'^(' + CASE_RE + r')\s*(' + NUM_RE + r')\s*(?P<rest>.*)$', chunk)
        if label_m:
            current_kasus = label_m.group(1).rstrip('.')
            current_numerus = label_m.group(2).rstrip('.')
            chunk = label_m.group('rest')

        for em in ENTRY_RE.finditer(chunk):
            if not em.group('ref'):
                continue
            form = em.group('form')
            abbr = em.group('abbr')
            if em.group('dass'):
                current_form = current_stem.rstrip('-')
            if form:
                current_form = form
            if abbr:
                current_abbr = abbr

            if current_form.startswith('-'):
                token = current_stem.rstrip('-') + current_form.lstrip('-')
            elif current_form == ']':
                token = current_stem.rstrip('-')
            else:
                token = current_form or current_stem.rstrip('-')

            ref = em.group('ref')
            ref_parts = ref.split(',')
            if current_abbr == 'Gl':
                if len(ref_parts) >= 3:
                    last_band['Gl'] = ref_parts[0]
                elif len(ref_parts) == 2 and last_band.get('Gl'):
                    ref = last_band['Gl'] + ',' + ref

            edition = f"{current_abbr} {ref}".strip()
            hs_info = em.group('paren') or ''
            if em.group('brack'):
                hs_info = (hs_info + ' [' + em.group('brack') + ']').strip()

            rows.append({
                'Token': token,
                'Kasus_token': current_kasus,
                'Numerus_token': current_numerus,
                'Edition': edition,
                'Handschrifteninfo_in_Klammern': hs_info,
            })
    return rows


# ---------------------------------------------------------------------------
# Ein kompletter Eintrag
# ---------------------------------------------------------------------------

def parse_article(words):
    rec = reconstruct_entry(words)
    lemma = rec['lemma']
    flexion = rec['flexion']
    blocks = rec['blocks']
    full_text = rec['full_text']

    rows = []

    if 'formblock' not in blocks:
        rows.append({
            'Lemma_automatisch': lemma, 'Lemma_awb': lemma, 'Flexion': flexion,
            'Komplexität': guess_komplexitaet(lemma),
            'Mhd_Lemma': '', 'mhd_Flexion': '', 'Nhd_UES': '',
            'Token': '', 'Kasus_token': '', 'Numerus_token': '',
            'Texttyp': '', 'Edition': '', 'Handschrifteninfo_in_Klammern': '',
            'Fragen': f'Kein Formen-Block gefunden - vermutlich Verweis-Eintrag. Volltext: {full_text.strip()}',
        })
        return rows

    head_start, head_end = blocks.get('verweisblock', [(0, blocks['formblock'][0][0])])[0]
    head_text = full_text[head_start:head_end]
    head_info = parse_head_text(head_text)

    forms_start, forms_end = blocks['formblock'][0]
    forms_text = full_text[forms_start:forms_end]
    token_rows = parse_forms_block(forms_text)

    if not token_rows:
        token_rows = [{'Token': '', 'Kasus_token': '', 'Numerus_token': '',
                        'Edition': '', 'Handschrifteninfo_in_Klammern': ''}]

    for tr in token_rows:
        rows.append({
            'Lemma_automatisch': lemma,
            'Lemma_awb': lemma,
            'Flexion': flexion,
            'Komplexität': guess_komplexitaet(lemma),
            'Mhd_Lemma': head_info['Mhd_Lemma'],
            'mhd_Flexion': head_info['mhd_Flexion'],
            'Nhd_UES': head_info['Nhd_UES'],
            'Token': tr['Token'],
            'Kasus_token': tr['Kasus_token'],
            'Numerus_token': tr['Numerus_token'],
            'Texttyp': 'Glosse',
            'Edition': tr['Edition'],
            'Handschrifteninfo_in_Klammern': tr['Handschrifteninfo_in_Klammern'],
            'Fragen': '',
        })
    return rows


COLUMN_MAP = {
    'Lemma_automatisch': 'Lemma_automatisch',
    'Lemma_awb': 'Lemma_awb',
    'Flexion': 'Flexion',
    'Komplexität': 'Komplexität',
    'Mhd_Lemma': 'Mhd. Lemma',
    'mhd_Flexion': 'mhd_Flexion',
    'Nhd_UES': 'Nhd. ÜS',
    'Token': 'Token',
    'Kasus_token': 'Kasus_token',
    'Numerus_token': 'Numerus_token',
    'Texttyp': 'Texttyp',
    'Edition': 'Edition',
    'Handschrifteninfo_in_Klammern': 'Handschrifteninfo_in_Klammern',
    'Fragen': 'Fragen',
}


def write_csv(all_rows, output_csv):
    with open(output_csv, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(COLUMN_MAP.values()))
        writer.writeheader()
        for row in all_rows:
            writer.writerow({COLUMN_MAP[k]: v for k, v in row.items() if k in COLUMN_MAP})


def main(formids, output_csv='awb_output.csv'):
    """formids: Liste von formid-Strings, z.B. ['A00356', 'A00358']."""
    all_rows = []
    for formid in formids:
        try:
            words = fetch_article(formid)
            all_rows.extend(parse_article(words))
        except Exception as e:
            all_rows.append({'Lemma_automatisch': formid, 'Fragen': f'FEHLER: {e}'})
    write_csv(all_rows, output_csv)
    print(f"{len(all_rows)} Zeilen nach {output_csv} geschrieben.")


if __name__ == '__main__':
    if len(sys.argv) > 1:
        main(sys.argv[1:])
    else:
        print("Nutzung: python3 parse_awb_api.py FORMID [FORMID ...]")
        print("Beispiel: python3 parse_awb_api.py A00356 A00358")