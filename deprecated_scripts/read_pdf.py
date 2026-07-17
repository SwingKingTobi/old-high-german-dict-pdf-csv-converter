from pypdf import PdfReader
import re

datei = input("Bitte Dateinamen eingeben: ")

reader = PdfReader("dict_pdfs/"+ datei)

text = ""

for page in reader.pages:
    seite = page.extract_text()
    if seite:
        text += seite + "\n"

print(text)


kopf = text.split(datei.removesuffix(".pdf"))[0]

print(kopf)

#Das liefert ungefähr

#affo sw. m., mhd. nhd. affe; as. apo ...

#Nun kann man das Lemma herausziehen:

m = re.match(r"^(\w+)\s+(.+?)\.", kopf)

if m:
    lemma = m.group(1)
    grammatik = m.group(2)

    print(lemma)
    print("blablba")
    print(grammatik)
