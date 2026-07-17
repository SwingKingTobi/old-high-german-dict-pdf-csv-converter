"""
Parser für Einträge des Althochdeutschen Wörterbuchs (AWB).

Wichtig: Die als ".pdf" abgelegten Dateien sind KEINE echten PDFs, sondern
ZIP-Archive (Export des AWB-Viewers) mit den Einträgen 1.jpeg, 1.txt und
manifest.json. Der eigentliche Text steht in 1.txt. Deshalb funktioniert
pypdf hier nicht - wir lesen die Dateien als ZIP.
"""
import zipfile
import re
import csv
import glob
import os

CASE_RE = r'(?:nom|gen|dat|acc|instr)\.'
NUM_RE = r'(?:sg|pl)\.'


def extract_text(path):
    """
    Liest den Klartext aus einer AWB-Datei aus - unabhängig davon, ob es sich
    tatsächlich um ein ZIP-Archiv (Export mit .pdf-Endung, wie die Beispiel-
    dateien im Projekt), ein echtes PDF oder eine gespeicherte HTML-Seite
    handelt. Der Dateityp wird anhand der Magic Bytes erkannt, NICHT anhand
    der Dateiendung.
    """
    with open(path, 'rb') as f:
        head = f.read(8)

    if head.startswith(b'PK'):
        # ZIP-Archiv (der bekannte AWB-"PDF"-Export mit 1.txt/1.jpeg/manifest.json)
        with zipfile.ZipFile(path) as z:
            txt_candidates = [n for n in z.namelist() if n.endswith('.txt')]
            if not txt_candidates:
                raise ValueError(
                    f"ZIP-Archiv {path} enthält keine .txt-Datei. "
                    f"Gefundene Dateien: {z.namelist()}")
            return z.read(txt_candidates[0]).decode('utf-8')

    if head.startswith(b'%PDF'):
        # Echtes PDF -> mit pypdf auslesen
        try:
            from pypdf import PdfReader
        except ImportError:
            raise ImportError(
                "Für echte PDFs wird 'pypdf' benötigt: pip install pypdf --break-system-packages")
        reader = PdfReader(path)
        text = ""
        for page in reader.pages:
            seite = page.extract_text()
            if seite:
                text += seite + "\n"
        return text

    if head.lstrip().startswith(b'<') or b'<html' in head.lower():
        # Als HTML gespeicherte Seite -> Tags entfernen
        raw = open(path, encoding='utf-8', errors='ignore').read()
        raw = re.sub(r'<script.*?</script>', '', raw, flags=re.DOTALL | re.IGNORECASE)
        raw = re.sub(r'<style.*?</style>', '', raw, flags=re.DOTALL | re.IGNORECASE)
        raw = re.sub(r'<[^>]+>', ' ', raw)
        return raw

    raise ValueError(
        f"{path}: unbekanntes Dateiformat (Magic Bytes: {head!r}). "
        f"Weder ZIP noch PDF noch HTML erkannt. Bitte prüfe die Datei z.B. "
        f"mit dem Befehl 'file {path}' und melde das Ergebnis."
    )


GRAMMAR_ABBR = [
    'sw', 'st', 'mhd', 'nhd', 'ahd', 'as', 'mnd', 'mnl', 'ae', 'an', 'afries',
    'got', 'adj', 'adv', 'conj', 'interj', 'partikel', 'part', 'dass', 'vgl',
    'bair', 'westf', 'schweiz', 'dat', 'gen', 'acc', 'nom', 'instr', 'sg', 'pl',
]
GRAMMAR_ABBR_RE = re.compile(
    r'(?<=[A-Za-zäöüßÄÖÜ])(' + '|'.join(GRAMMAR_ABBR) + r')\.(?=[A-Za-zäöüßÄÖÜ])'
)


def normalize(text):
    """
    Entfernt Zeilenumbrüche/Fußzeile und normalisiert Whitespace. Repariert
    außerdem ein bekanntes pypdf-Extraktionsproblem: bei diesen browser-
    gedruckten PDFs gehen Leerzeichen rund um kursiv gesetzte grammatische
    Abkürzungen (sw., m., mhd., nhd., …) verloren - manchmal ganz, manchmal
    werden sie durch Unicode-Private-Use-Area-Zeichen ersetzt.
    """
    text = text.replace('\r\n', '\n')
    # Fußzeile ("Althochdeutsches Wörterbuch https://... / 1 of 1 ...") abschneiden
    text = re.sub(r'Althochdeutsches Wörterbuch.*', '', text, flags=re.DOTALL)
    # Private-Use-Area-Zeichen (kaputte Font-Zuordnung) -> Leerzeichen
    text = re.sub(r'[\ue000-\uf8ff]', ' ', text)
    # Satzzeichen direkt gefolgt von einem Buchstaben -> Leerzeichen einfügen
    text = re.sub(r'([.,;:—])(?=[A-Za-zäöüßÄÖÜ])', r'\1 ', text)
    # "wortabk." mitten im Wort (z.B. "affosw.", "giaconj.") -> Leerzeichen davor
    text = GRAMMAR_ABBR_RE.sub(lambda m: m.group(1) + '. ', text)
    text = re.sub(
        r'(?<=[a-zäöüß])(' + '|'.join(GRAMMAR_ABBR) + r')\.',
        r' \1.', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def find_forms_start(text):
    """
    Sucht den Beginn des Formen-/Belegblocks: die erste 'WORT:'-artige
    Deklaration, nach der zeitnah eine Belegstelle (Ziffer,Ziffer) folgt.
    Das erkennt sowohl "aff-: nom. sg. -o Gl 2,245,36" als auch Partikel-
    Einträge wie "ia (Nb, Np, Npgl iâ): Gl 1,714,3", die keine Kasus-Angabe
    haben. Ein simpler Split auf den ersten Gedankenstrich reicht NICHT,
    weil Kopfsätze selbst Gedankenstriche in Klammern enthalten können
    (z.B. "(12. —15. Jh.)").
    """
    decl_re = re.compile(r'[A-Za-zäöüßÄÖÜ]+(?:\s*\([^)]{0,60}\))?-?\s*:')
    for m in decl_re.finditer(text):
        window = text[m.end():m.end() + 60]
        if re.search(r'\d+,\d+', window):
            return m.start()
    return None


def parse_head(text):
    """
    Parst den einleitenden Satz eines Eintrags, z.B.:
      "affo sw. m., mhd. nhd. affe; as. apo, mnd. āpe; ... — Graff I, 159."
    Liefert lemma, flexion, mhd_lemma, nhd_uebersetzung und den Formen-/
    Belegblock-Text (alles ab der ersten "WORT:"-Deklaration mit Belegstelle).
    Erkennt außerdem Verweis-Einträge ("affoltarboum s. affalterboum st. m.").
    """
    info = {
        'Lemma_automatisch': '',
        'Flexion': '',
        'Mhd_Lemma': '',
        'Nhd_UES': '',
        'ist_Verweis': False,
        'Verweisziel': '',
    }

    # Verweis-Eintrag: "LEMMA s. ANDERES_LEMMA GRAMMATIK"
    verweis = re.match(
        r'^(?P<lemma>[A-Za-zäöüÄÖÜß]+)\s+s\.\s+(?P<ziel>[A-Za-zäöüÄÖÜß]+)\s+(?P<flex>.+)$',
        text)
    if verweis:
        info['Lemma_automatisch'] = verweis.group('lemma')
        info['Flexion'] = verweis.group('flex').rstrip('.')
        info['ist_Verweis'] = True
        info['Verweisziel'] = verweis.group('ziel')
        return info, ''

    start = find_forms_start(text)
    head = text[:start] if start is not None else text
    forms_text = text[start:] if start is not None else ''

    m = re.match(
        r'^(?P<lemma>[A-Za-zäöüÄÖÜß]+)\s+'
        r'(?P<flex>[^,]+),\s*'
        r'(?:mhd\.\s*(?P<mhd>.*?)\s*)?nhd\.\s*(?P<nhd>[^;]+)',
        head)
    if m:
        info['Lemma_automatisch'] = m.group('lemma')
        info['Flexion'] = m.group('flex').strip()
        info['Mhd_Lemma'] = (m.group('mhd') or '').strip().rstrip(',').strip()
        info['Nhd_UES'] = m.group('nhd').strip()
    else:
        # Fallback: nur Lemma + erstes Wort als Flexion-Rest greifen
        m2 = re.match(r'^(?P<lemma>[A-Za-zäöüÄÖÜß]+)\s+(?P<flex>.+?)\.', head)
        if m2:
            info['Lemma_automatisch'] = m2.group('lemma')
            info['Flexion'] = m2.group('flex').strip()

    return info, forms_text


def guess_komplexitaet(lemma):
    """Sehr grobe Heuristik - bei Bedarf anpassen/verfeinern."""
    if '-' in lemma or len(lemma) >= 12:
        return 'komplex'
    return 'simplex'


def trim_forms_block(forms_text):
    """
    Schneidet den Formen-/Belegblock am Ende ab, sobald der Zitatteil beginnt
    (typografisch gesperrter Text wie "A ff e :") oder Marker wie "Abl.",
    "Vgl.", "/Bd." auftauchen. forms_text muss bereits am Formen-Beginn
    ansetzen (siehe find_forms_start).
    """
    if not forms_text:
        return ''
    end_pattern = re.compile(
        r'(?:[A-Za-zäöüßÄÖÜ]\s){2,}[A-Za-zäöüßÄÖÜ]|/Bd\.|Abl\.|Vgl\.')
    end_m = end_pattern.search(forms_text)
    end = end_m.start() if end_m else len(forms_text)
    return forms_text[:end].strip()


ENTRY_RE = re.compile(
    r"""
    (?P<dass>dass\.)?\s*                                    # "dass." = dieselbe Form wie das Lemma
    (?P<form>-\]|-[A-Za-zäöüßÄÖÜ]+)?\s*                      # Wortendung (optional -> Fortsetzung/inherit)
    (?P<abbr>[A-ZÄÖÜ][A-Za-zäöüß]{0,7})?\s*                  # Editions-Abkürzung, z.B. Gl, Nb, T, O, Np, NpNpw
    (?P<ref>\d+(?:,\d+)+(?:\s*=\s*[A-Za-zäöüßÄÖÜ]+\s*[\d,]+)?)  # Belegstelle, z.B. 2,245,36 oder 2,580,56 = Wa 94,35
    \s*(?:\((?P<paren>[^)]*)\))?                            # (Handschrifteninfo)
    \s*(?:\[(?P<brack>[^\]]*)\])?                           # [Alternativangabe]
    \s*\.?
    """,
    re.VERBOSE,
)


STEM_DECL_RE = re.compile(
    r'^(?P<stem>[A-Za-zäöüßÄÖÜ]+)(?P<parens>\s*\([^)]*\))?(?P<dash>-)?\s*:\s*(?P<rest>.*)$')


def parse_forms_block(forms_block):
    """
    Zerlegt den Formen-/Belegblock in einzelne Token-Zeilen.
    "—" und ";" werden gleichwertig als Blockgrenzen behandelt (beide trennen
    in der Praxis mal neue Stammformen, mal nur neue Belegstellen-Gruppen).
    Ein neuer Stamm kann mitten in einem Block auftauchen (z.B. bei "affoltra":
    "affultra: ... ; affol-: ... ; affal-: ..."), deshalb wird bei JEDEM Chunk
    geprüft, ob er mit einer neuen "WORT:"-Deklaration beginnt.
    Liefert eine Liste von dicts mit: Token, Kasus_token, Numerus_token,
    Edition, Handschrifteninfo_in_Klammern.
    """
    rows = []
    last_band = {}  # merkt sich pro Editions-Abkürzung (z.B. "Gl") die zuletzt genannte Bandnummer

    current_stem = ''
    current_kasus, current_numerus = '', ''
    current_form = ''
    current_abbr = ''

    chunks = [c.strip() for c in re.split(r'—|;', forms_block) if c.strip()]

    for chunk in chunks:
        stem_m = STEM_DECL_RE.match(chunk)
        if stem_m:
            current_stem = stem_m.group('stem') + ('-' if stem_m.group('dash') else '')
            current_form = current_stem.rstrip('-')  # ohne Endungsangabe = volle Stammform
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
            # Fortsetzungs-Belegstellen (z.B. "246,47.") erben die Band-
            # nummer der letzten vollen Angabe (z.B. "2,245,36" -> Band 2)
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


def parse_entry(path):
    raw = extract_text(path)
    text = normalize(raw)
    head_info, rest = parse_head(text)

    rows = []
    if head_info['ist_Verweis']:
        rows.append({
            'Lemma_automatisch': head_info['Lemma_automatisch'],
            'Lemma_awb': head_info['Lemma_automatisch'],
            'Flexion': head_info['Flexion'],
            'Komplexität': guess_komplexitaet(head_info['Lemma_automatisch']),
            'Mhd_Lemma': '', 'mhd_Flexion': '', 'Nhd_UES': '',
            'Token': '', 'Kasus_token': '', 'Numerus_token': '',
            'Texttyp': '', 'Edition': '',
            'Handschrifteninfo_in_Klammern': '',
            'Fragen': f"Verweis-Eintrag -> s. {head_info['Verweisziel']}",
        })
        return rows

    forms_block = trim_forms_block(rest)
    token_rows = parse_forms_block(forms_block)

    if not token_rows:
        token_rows = [{
            'Token': '', 'Kasus_token': '', 'Numerus_token': '',
            'Edition': '', 'Handschrifteninfo_in_Klammern': '',
        }]

    for tr in token_rows:
        rows.append({
            'Lemma_automatisch': head_info['Lemma_automatisch'],
            'Lemma_awb': head_info['Lemma_automatisch'],
            'Flexion': head_info['Flexion'],
            'Komplexität': guess_komplexitaet(head_info['Lemma_automatisch']),
            'Mhd_Lemma': head_info['Mhd_Lemma'],
            'mhd_Flexion': '',
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


# interne Feldnamen -> Spaltenüberschriften wie im Ziel-CSV/Screenshot
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


def main(input_dir='dict_pdfs', output_csv='awb_output.csv'):
    all_rows = []
    for path in sorted(glob.glob(os.path.join(input_dir, '*.pdf'))):
        try:
            all_rows.extend(parse_entry(path))
        except Exception as e:
            all_rows.append({'Lemma_automatisch': os.path.basename(path),
                              'Fragen': f'FEHLER beim Parsen: {e}'})

    with open(output_csv, 'w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(COLUMN_MAP.values()))
        writer.writeheader()
        for row in all_rows:
            writer.writerow({COLUMN_MAP[k]: v for k, v in row.items() if k in COLUMN_MAP})
    print(f"{len(all_rows)} Zeilen nach {output_csv} geschrieben.")


if __name__ == '__main__':
    main()