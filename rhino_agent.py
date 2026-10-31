#! python3
# =====================================================================
#  AGENT RHINO – WERSJA Z CZATEM  (okno jak komunikator + tryb nauki)
# =====================================================================
#  Uruchomienie w Rhino: komenda RunPythonScript -> wybierz ten plik.
#
#  Co nowego względem agent_rhino.py:
#    - okno to mała "strona internetowa" (WebView): dymki, karty kroków,
#      animacja "agent pisze…". Wygląd zmieniasz w części 7 (HTML + CSS).
#    - dwa tryby: "Ucz mnie" (agent prowadzi Cię krok po kroku i NIE robi
#      za Ciebie) oraz "Zrób za mnie" (agent działa sam, jak wcześniej).
#    - nowe narzędzia: karta kroku, znaczniki w modelu, podgląd Twoich komend.
#  Pętla agenta (część 4) jest nadal ta sama.
# =====================================================================

import contextlib
import io
import base64
import json
import math
import tempfile
import traceback
import os
import re
import threading
import urllib.parse
from pathlib import Path

import clr
clr.AddReference("System.Net.Http")

import System
import System.Drawing           # rozmiary i obrazy (zrzut widoku)
import System.Drawing.Imaging   # zapis obrazu do PNG
from System.Net.Http import HttpClient, HttpRequestMessage, HttpMethod, StringContent
from System.Text import Encoding

import Rhino
import rhinoscriptsyntax as rs
import scriptcontext as sc
import Eto.Forms as forms
import Eto.Drawing as drawing


# ---------------------------------------------------------------------
# 1. USTAWIENIA I PROMPTY
# ---------------------------------------------------------------------

MODEL = "claude-sonnet-5-5"
MAKS_KROKOW = 15
MOTYW = "auto"      # "auto" (jak w ustawieniach Maca), "ciemny" albo "jasny"
POTWIERDZAJ_KOD = True   # True: kod agenta uruchamia się dopiero po kliknięciu "Run"
WARSTWA_WSKAZOWEK = "Agent - wskazowki"

PROMPT_BAZA = """You are a patient Rhino 8 teacher on a Mac. Your student is the user.
Always reply in English, briefly and concretely, even if tool results are in Polish.
You can use simple formatting: `command name`, numbered lists (1. 2. 3.) – never dash bullets or em dashes – and **bold** sparingly, only for the one or two most important words in a reply.

The user's interface (Rhino 8 for Mac): the Command field is in the top-left corner, with the icon
toolbar below it. There are four viewports: Top, Perspective, Front, Right. The properties panel
is on the right. Prompts and options of the current command appear in the bar at the bottom.
Check units and model contents with the opisz_model tool. You can see the viewport with zrzut_widoku.

Grasshopper: read the user's canvas with opisz_grasshoppera (short ids, connections, sliders, errors).
When teaching Grasshopper, the "Where" of a step is the canvas: e.g. "double-click empty canvas, type
Number Slider", or "drag from the R output of Rectangle to the C input of Loft". Name inputs/outputs
exactly as on the component. After they say "done", check with opisz_grasshoppera. Point at the component
you mean with zaznacz_komponent. Change slider values for them with ustaw_suwak only in Make mode."""

PROMPT_NAUKA = """MODE: LEARN. Don't do things for the user – guide them so they do them themselves.
- Show the WHOLE task at once with pokaz_kroki: 3–8 short steps, fields in English. Then end your
  reply (outside the plan write at most one short sentence). Don't ask the user to report each step.
- The app ticks steps off by itself: give every step a check – "komenda" (the English Rhino command
  they run, e.g. Box, ExtrudeCrv) or "widok" (Top, Front, Right or Perspective) when the step is
  switching viewports. Steps without a check get ticked when a later step is done.
- When the user writes, their message ends with an app note about plan progress. If needed, look at the
  model (opisz_model, zrzut_widoku), tick finished steps with oznacz_kroki and help with the next one.
- When the app says all steps are finished, check the result, remove markers (usun_wskazowki) and
  summarise what they learned in 2–3 sentences.
- To point at a place in the model, place a marker with pokaz_miejsce and mention the orange marker.
- When the user is stuck, explain that step differently, more simply.
- Only use drawing tools, wykonaj_komende and wykonaj_kod when the user explicitly asks you to do something
  for them (e.g. "show me the finished shape first"). Then put it on its own layer as a reference."""

PROMPT_ZROB = """MODE: MAKE. Do the task with your tools, then in 2–3 sentences tell the user
which commands they would use to do it herself.
- Before deleting or changing existing objects, ask for permission.
- In wykonaj_komende use English command names with an underscore and dash (e.g. _-Line);
  to make a command act on an object, select it first with zaznacz_obiekty.
- For anything the simple tools can't do (booleans, fillets, arrays, lofts, parametric or wavy shapes,
  editing existing objects, analysis), use wykonaj_kod: write short Python 3 for Rhino 8 using
  rhinoscriptsyntax as rs or Rhino.Geometry. Use object ids from opisz_model. print() what you made.
  In "kroki" explain in plain words what will happen – the user decides from that, the code is collapsed.
  Add short English comments – the user reads the code to learn. If it fails, read the error and fix it."""

tryb = {"wartosc": "nauka"}   # "nauka" albo "zrob" – zmienia go przełącznik w oknie


def system_prompt():
    return PROMPT_BAZA + "\n\n" + (PROMPT_NAUKA if tryb["wartosc"] == "nauka" else PROMPT_ZROB)


def wczytaj_klucz():
    """Rhino uruchomione z Docka nie widzi ~/.zshrc, więc czytamy klucz z tego pliku sami."""
    klucz = os.environ.get("ANTHROPIC_API_KEY")
    if klucz:
        return klucz
    zshrc = Path.home() / ".zshrc"
    if zshrc.exists():
        for linia in zshrc.read_text(encoding="utf-8").splitlines():
            znalezione = re.match(r"""\s*export\s+ANTHROPIC_API_KEY=["']?([^"'\s]+)""", linia)
            if znalezione:
                klucz = znalezione.group(1)
    return klucz


KLUCZ = wczytaj_klucz()


# ---------------------------------------------------------------------
# 2. ROZMOWA Z API "RĘCZNIE" (bez biblioteki anthropic)
# ---------------------------------------------------------------------

ADRES_API = "https://api.anthropic.com/v1/messages"
klient_http = HttpClient()
klient_http.Timeout = System.TimeSpan.FromSeconds(180)


def wyslij_do_claude(historia):
    cialo = json.dumps({
        "model": MODEL,
        "max_tokens": 2000,
        "system": system_prompt(),
        "tools": NARZEDZIA,
        "messages": historia,
    })
    zapytanie = HttpRequestMessage(HttpMethod.Post, ADRES_API)
    zapytanie.Headers.Add("x-api-key", KLUCZ)
    zapytanie.Headers.Add("anthropic-version", "2023-06-01")
    zapytanie.Content = StringContent(cialo, Encoding.UTF8, "application/json")

    odpowiedz = klient_http.SendAsync(zapytanie).Result
    tekst = odpowiedz.Content.ReadAsStringAsync().Result

    if not odpowiedz.IsSuccessStatusCode:
        kod = int(odpowiedz.StatusCode)
        if kod == 401:
            raise Exception("Klucz API jest nieprawidłowy (401). Sprawdź klucz w ~/.zshrc.")
        raise Exception(f"API zwróciło błąd {kod}: {tekst[:300]}")
    return json.loads(tekst)


# ---------------------------------------------------------------------
# 3. NARZĘDZIA
# ---------------------------------------------------------------------

def dokument():
    sc.doc = Rhino.RhinoDoc.ActiveDoc
    return sc.doc


def z_cofaniem(nazwa, funkcja):
    """Zmiany agenta trafiają do historii cofania – Cmd+Z je cofa."""
    doc = dokument()
    numer = doc.BeginUndoRecord(f"Agent: {nazwa}")
    try:
        return funkcja()
    finally:
        if numer:
            doc.EndUndoRecord(numer)
        doc.Views.Redraw()


def nazwy_warstw():
    """Słownik: numer warstwy -> nazwa. Czytamy warstwy po kolei i pomijamy usunięte,
    bo na Macu odczyt nazwy usuniętej / "pustej" warstwy kończy się błędem."""
    nazwy = {}
    for warstwa in dokument().Layers:
        try:
            if not warstwa.IsDeleted:
                nazwy[warstwa.Index] = warstwa.Name
        except Exception:
            pass
    return nazwy


def opis_obiektu(obj_id, nazwy=None):
    try:
        nazwy = nazwy if nazwy is not None else nazwy_warstw()
        obj = dokument().Objects.FindId(obj_id)
        if obj is None:
            return f"{obj_id}: nie istnieje"
        bb = obj.Geometry.GetBoundingBox(True)
        wymiary = f"{bb.Max.X - bb.Min.X:.2f} x {bb.Max.Y - bb.Min.Y:.2f} x {bb.Max.Z - bb.Min.Z:.2f}"
        warstwa = nazwy.get(obj.Attributes.LayerIndex, "?")
        srodek = bb.Center
        return (f"{obj_id} | {obj.ObjectType} | warstwa: {warstwa} | nazwa: {obj.Attributes.Name or '-'}"
                f" | wymiary: {wymiary} | środek: {srodek.X:.2f},{srodek.Y:.2f},{srodek.Z:.2f}")
    except Exception as blad:
        return f"{obj_id}: nie udało się opisać ({blad})"   # jeden zły obiekt nie psuje całego opisu


# --- odczyt ---

def opisz_model():
    doc = dokument()
    nazwy = nazwy_warstw()
    obiekty = rs.AllObjects() or []
    try:
        jednostki = rs.UnitSystemName(abbreviate=True)
    except Exception:
        jednostki = "?"
    linie = [f"Jednostki: {jednostki}",
             f"Aktualna warstwa: {nazwy.get(doc.Layers.CurrentLayerIndex, '?')}",
             f"Warstwy: {', '.join(nazwy.values())}",
             f"Liczba obiektów: {len(obiekty)}"]
    for obj_id in obiekty[:40]:
        linie.append(opis_obiektu(obj_id, nazwy))
    if len(obiekty) > 40:
        linie.append(f"... i {len(obiekty) - 40} kolejnych")
    return "\n".join(linie)


def opisz_zaznaczone():
    zaznaczone = rs.SelectedObjects() or []
    if not zaznaczone:
        return "Nic nie jest zaznaczone."
    return "\n".join(opis_obiektu(obj_id) for obj_id in zaznaczone)


def zrzut_widoku():
    """Robi zdjęcie aktywnego widoku Rhino i oddaje je modelowi jako obraz."""
    widok = dokument().Views.ActiveView
    obraz = widok.CaptureToBitmap(System.Drawing.Size(1000, 700))
    sciezka = os.path.join(tempfile.gettempdir(), "agent_widok.png")
    obraz.Save(sciezka, System.Drawing.Imaging.ImageFormat.Png)
    with open(sciezka, "rb") as plik:
        dane = base64.b64encode(plik.read()).decode("ascii")
    return [
        {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": dane}},
        {"type": "text", "text": f"Zrzut widoku: {widok.ActiveViewport.Name}"},
    ]


# --- Grasshopper ---
#  Grasshopper ładujemy dopiero przy użyciu (import w funkcji), bo nie zawsze jest otwarty.

def plotno_gh():
    """Zwraca (płótno, dokument) otwartego Grasshoppera albo rzuca czytelny błąd."""
    import Grasshopper
    plotno = Grasshopper.Instances.ActiveCanvas
    if plotno is None or plotno.Document is None:
        raise Exception("Grasshopper is not open. Ask the user to type Grasshopper in the Command field.")
    return plotno, plotno.Document


def krotkie_id(obj):
    return str(obj.InstanceGuid)[:8]


def nazwa_wlasciciela(param):
    """Nazwa komponentu, do którego należy wejście/wyjście (np. 'Loft')."""
    try:
        return param.Attributes.GetTopLevel.DocObject.NickName
    except Exception:
        return "?"


def znajdz_obiekt_gh(doc, klucz):
    """Szuka obiektu po krótkim id (np. 'a1b2c3d4') albo po nazwie (NickName)."""
    klucz = str(klucz).strip().lower()
    for obj in doc.Objects:
        if str(obj.InstanceGuid).lower().startswith(klucz) or (obj.NickName or "").lower() == klucz:
            return obj
    return None


def opisz_grasshoppera():
    import Grasshopper
    from Grasshopper.Kernel.Special import GH_NumberSlider, GH_Panel
    plotno, doc = plotno_gh()
    obiekty = list(doc.Objects)
    linie = [f"Grasshopper: {len(obiekty)} objects on the canvas"]
    for obj in obiekty[:80]:
        try:
            naglowek = f"[{krotkie_id(obj)}] {obj.Name} '{obj.NickName}'"
            if isinstance(obj, GH_NumberSlider):
                s = obj.Slider
                linie.append(f"{naglowek} = {obj.CurrentValue} (min {s.Minimum}, max {s.Maximum})")
                continue
            if isinstance(obj, GH_Panel):
                linie.append(f"{naglowek}: \"{str(obj.UserText)[:80]}\"")
                continue
            czesci = [naglowek]
            if hasattr(obj, "Params"):                       # komponent z wejściami i wyjściami
                wejscia = []
                for p in obj.Params.Input:
                    zrodla = [f"{nazwa_wlasciciela(z)}.{z.NickName}" for z in p.Sources]
                    wejscia.append(f"{p.NickName}<-{','.join(zrodla) or 'nothing'}")
                wyjscia = [p.NickName for p in obj.Params.Output]
                czesci.append("inputs: " + "; ".join(wejscia))
                czesci.append("outputs: " + ", ".join(wyjscia))
            elif hasattr(obj, "Sources"):                    # samodzielny parametr (np. Curve, Point)
                zrodla = [f"{nazwa_wlasciciela(z)}.{z.NickName}" for z in obj.Sources]
                czesci.append("from: " + (", ".join(zrodla) or "set in Rhino or empty"))
            if hasattr(obj, "RuntimeMessageLevel"):
                poziom = str(obj.RuntimeMessageLevel)
                if poziom in ("Warning", "Error"):
                    poziom_enum = obj.RuntimeMessageLevel
                    komunikaty = list(obj.RuntimeMessages(poziom_enum))
                    czesci.append(f"{poziom.upper()}: {' | '.join(komunikaty)[:200]}")
            linie.append(" | ".join(czesci))
        except Exception as blad:
            linie.append(f"[{krotkie_id(obj)}] could not describe ({blad})")
    if len(obiekty) > 80:
        linie.append(f"... and {len(obiekty) - 80} more")
    return "\n".join(linie)


def ustaw_suwak(suwak, wartosc):
    import System
    from Grasshopper.Kernel.Special import GH_NumberSlider
    plotno, doc = plotno_gh()
    obj = znajdz_obiekt_gh(doc, suwak)
    if obj is None or not isinstance(obj, GH_NumberSlider):
        return f"ERROR: no number slider '{suwak}'. Use opisz_grasshoppera to see ids and names."
    s = obj.Slider
    if not (float(s.Minimum) <= float(wartosc) <= float(s.Maximum)):
        return f"ERROR: {wartosc} is outside the slider range {s.Minimum}..{s.Maximum}."
    obj.SetSliderValue(System.Decimal(float(wartosc)))
    obj.ExpireSolution(True)                                  # Grasshopper przelicza definicję
    return f"Slider '{obj.NickName}' set to {obj.CurrentValue}."


def zaznacz_komponent(ids):
    plotno, doc = plotno_gh()
    for obj in doc.Objects:
        obj.Attributes.Selected = False
    znalezione = []
    for klucz in ids:
        obj = znajdz_obiekt_gh(doc, klucz)
        if obj is not None:
            obj.Attributes.Selected = True
            znalezione.append(obj.NickName)
    plotno.Refresh()
    return f"Highlighted on the canvas: {', '.join(znalezione) or 'nothing found'}."


ostatnie = []   # komendy, które użytkownik (albo agent) uruchomił – zapisuje je "słuchacz" z części 6


def ostatnie_komendy():
    if not ostatnie:
        return "Od początku rozmowy nie uruchomiono żadnej komendy."
    return "Ostatnie komendy (od najstarszej): " + ", ".join(ostatnie)


# --- nauka ---

plan = {"tytul": "", "kroki": []}   # kroki: {"numer", "komenda", "widok", "zrobiony"}


def nazwa_komendy(tekst):
    """'_-Circle 0,0,0 5' -> 'circle' (pierwsze słowo, bez znaków _ - ! na początku)."""
    slowa = (tekst or "").split()
    return slowa[0].lstrip("_-!'").lower() if slowa else None


def pokaz_kroki(tytul, kroki):
    plan["tytul"] = tytul
    plan["kroki"] = []
    for i, k in enumerate(kroki, start=1):
        k["numer"] = i
        plan["kroki"].append({"numer": i, "zrobiony": False,
                              "komenda": nazwa_komendy(k.get("komenda") or k.get("wpisz")),
                              "widok": (k.get("widok") or "").strip().lower() or None})
    js("plan", {"tytul": tytul, "kroki": kroki})
    return ("The plan is shown. End your reply now (one short sentence at most). "
            "The app ticks steps automatically and tells you when all are finished.")


def odhacz(numery):
    """Oznacza kroki jako zrobione (jaśnieją w czacie). Gdy wszystkie są zrobione – agent dostaje znać."""
    nowe = []
    for k in plan["kroki"]:
        if k["numer"] in numery and not k["zrobiony"]:
            k["zrobiony"] = True
            nowe.append(k["numer"])
    if not nowe:
        return []
    nastepny = next((k["numer"] for k in plan["kroki"] if not k["zrobiony"]), None)
    js("planZrobione", {"numery": nowe, "aktualny": nastepny})
    if nastepny is None:
        js("wyslijAuto", "I finished all the steps.")
    return nowe


def odhacz_po_zdarzeniu(pole, wartosc):
    """Rhino zgłosił komendę albo zmianę widoku. Szukamy pierwszego niezrobionego kroku, który na to czeka;
    odhaczamy go razem ze wszystkimi wcześniejszymi (skoro jest dalej, to tamte też zrobiła – może inną komendą)."""
    for k in plan["kroki"]:
        if not k["zrobiony"] and k[pole] and k[pole] == wartosc:
            wczesniejsze = [w["numer"] for w in plan["kroki"] if w["numer"] < k["numer"] and not w["zrobiony"]]
            odhacz(wczesniejsze + [k["numer"]])
            return


def oznacz_kroki(numery):
    nowe = odhacz([int(n) for n in numery])
    return f"Ticked steps: {nowe or 'none (already done or wrong numbers)'}."


def postep_planu():
    if not plan["kroki"]:
        return ""
    zrobione = [k["numer"] for k in plan["kroki"] if k["zrobiony"]]
    nastepny = next((k["numer"] for k in plan["kroki"] if not k["zrobiony"]), None)
    return (f"\n\n[App note – plan '{plan['tytul']}': done steps {zrobione or 'none'}; "
            f"{'next step ' + str(nastepny) if nastepny else 'all steps finished'}.]")


def pokaz_miejsce(punkt, etykieta):
    def zrob():
        if not rs.IsLayer(WARSTWA_WSKAZOWEK):
            rs.AddLayer(WARSTWA_WSKAZOWEK, (234, 88, 12))
        kropka = rs.AddTextDot(etykieta, punkt)
        rs.ObjectLayer(kropka, WARSTWA_WSKAZOWEK)
        return f"Postawiłem pomarańczowy znacznik '{etykieta}' w punkcie {punkt}."
    return z_cofaniem("znacznik", zrob)


def usun_wskazowki():
    if not rs.IsLayer(WARSTWA_WSKAZOWEK):
        return "Nie ma żadnych znaczników."
    ids = rs.ObjectsByLayer(WARSTWA_WSKAZOWEK) or []
    z_cofaniem("usunięcie znaczników", lambda: rs.DeleteObjects(ids))
    return f"Usunięto {len(ids)} znaczników."


# --- działanie (tryb "Zrób za mnie") ---

def zaznacz_obiekty(ids):
    rs.UnselectAllObjects()
    ile = rs.SelectObjects(ids) or 0
    dokument().Views.Redraw()
    return f"Zaznaczono {ile} obiektów."


def dodaj_linie(start, koniec):
    return z_cofaniem("linia", lambda: f"Dodano linię, id: {rs.AddLine(start, koniec)}")


def dodaj_okrag(srodek, promien):
    return z_cofaniem("okrąg", lambda: f"Dodano okrąg, id: {rs.AddCircle(srodek, promien)}")


def dodaj_prostopadloscian(rog, dlugosc, szerokosc, wysokosc):
    x, y, z = rog
    dol = [[x, y, z], [x + dlugosc, y, z], [x + dlugosc, y + szerokosc, z], [x, y + szerokosc, z]]
    gora = [[px, py, z + wysokosc] for px, py, _ in dol]
    return z_cofaniem("prostopadłościan",
                      lambda: f"Dodano prostopadłościan, id: {rs.AddBox(dol + gora)}")


def dodaj_kule(srodek, promien):
    return z_cofaniem("kula", lambda: f"Dodano kulę, id: {rs.AddSphere(srodek, promien)}")


def dodaj_walec(srodek, promien, wysokosc):
    return z_cofaniem("walec", lambda: f"Dodano walec, id: {rs.AddCylinder(srodek, wysokosc, promien)}")


def utworz_warstwe(nazwa, kolor=None):
    def zrob():
        if not rs.IsLayer(nazwa):
            rs.AddLayer(nazwa, tuple(kolor) if kolor else None)
        rs.CurrentLayer(nazwa)
        return f"Warstwa '{nazwa}' jest teraz aktualna."
    return z_cofaniem("warstwa", zrob)


def wykonaj_komende(komenda):
    udalo_sie = rs.Command(komenda, True)
    dokument().Views.Redraw()
    return "Komenda wykonana." if udalo_sie else "Komenda nie powiodła się albo została anulowana."


FUNKCJE = {
    "opisz_model": opisz_model,
    "opisz_zaznaczone": opisz_zaznaczone,
    "zrzut_widoku": zrzut_widoku,
    "opisz_grasshoppera": opisz_grasshoppera,
    "ustaw_suwak": ustaw_suwak,
    "zaznacz_komponent": zaznacz_komponent,
    "ostatnie_komendy": ostatnie_komendy,
    "pokaz_kroki": pokaz_kroki,
    "oznacz_kroki": oznacz_kroki,
    "pokaz_miejsce": pokaz_miejsce,
    "usun_wskazowki": usun_wskazowki,
    "zaznacz_obiekty": zaznacz_obiekty,
    "dodaj_linie": dodaj_linie,
    "dodaj_okrag": dodaj_okrag,
    "dodaj_prostopadloscian": dodaj_prostopadloscian,
    "dodaj_kule": dodaj_kule,
    "dodaj_walec": dodaj_walec,
    "utworz_warstwe": utworz_warstwe,
    "wykonaj_komende": wykonaj_komende,
    # wykonaj_kod ma osobną obsługę w pętli agenta (czeka na kliknięcie "Run")
}

PUNKT = {"type": "array", "items": {"type": "number"}, "minItems": 3, "maxItems": 3,
         "description": "Punkt [x, y, z] w jednostkach modelu"}
LICZBA = {"type": "number"}
TEKST = {"type": "string"}
PUSTE = {"type": "object", "properties": {}}

NARZEDZIA = [
    {"name": "opisz_model",
     "description": "Zwraca jednostki, warstwy i listę obiektów w modelu (id, typ, warstwa, wymiary, środek).",
     "input_schema": PUSTE},
    {"name": "opisz_zaznaczone",
     "description": "Zwraca opis obiektów, które użytkownik ma teraz zaznaczone.",
     "input_schema": PUSTE},
    {"name": "opisz_grasshoppera",
     "description": ("Describes the open Grasshopper canvas: every component and parameter with a short id, its "
                     "nickname, which outputs feed each input, slider values and ranges, panel text, and red/orange "
                     "warnings or errors. Use it to check the user's Grasshopper steps or to understand their definition."),
     "input_schema": PUSTE},
    {"name": "ustaw_suwak",
     "description": "Sets a Grasshopper number slider (by short id or nickname) to a value; the definition recomputes.",
     "input_schema": {"type": "object", "properties": {
         "suwak": {"type": "string", "description": "Short id from opisz_grasshoppera or the slider's nickname"},
         "wartosc": {"type": "number"}}, "required": ["suwak", "wartosc"]}},
    {"name": "zaznacz_komponent",
     "description": "Selects (highlights) components on the Grasshopper canvas so the user sees which one you mean.",
     "input_schema": {"type": "object", "properties": {
         "ids": {"type": "array", "items": {"type": "string"}, "description": "Short ids or nicknames"}},
         "required": ["ids"]}},
    {"name": "zrzut_widoku",
     "description": ("Takes a screenshot of the active Rhino viewport and returns it as an image, so you can see "
                     "the model. Use it to check how a shape looks, to verify the user's steps, or when they ask "
                     "what is wrong with their model."),
     "input_schema": PUSTE},
    {"name": "ostatnie_komendy",
     "description": "Zwraca listę ostatnich komend uruchomionych w Rhino i ich wynik (Success/Cancel). "
                    "Użyj, żeby sprawdzić, czy użytkownik wykonał krok.",
     "input_schema": PUSTE},
    {"name": "pokaz_kroki",
     "description": ("Shows the WHOLE plan of a task in the chat as numbered steps (main tool of Learn mode). "
                     "The app ticks steps off automatically when the user runs the step's command or switches to the "
                     "step's viewport, and tells you when all steps are finished."),
     "input_schema": {"type": "object", "properties": {
         "tytul": {"type": "string", "description": "Short task title, e.g. 'Wavy box facade'"},
         "kroki": {"type": "array", "items": {"type": "object", "properties": {
             "tytul": {"type": "string", "description": "What we achieve, e.g. 'Draw the base square'"},
             "gdzie": {"type": "string", "description": "Where in Rhino, e.g. 'Command field, top-left'"},
             "wpisz": {"type": "string", "description": "Exactly what to type, e.g. 'Rectangle'. Empty if nothing."},
             "potem": {"type": "string", "description": "What next: where to click, values, when to press Enter"},
             "wskazowka": {"type": "string", "description": "Optional short tip"},
             "komenda": {"type": "string", "description": "English command that completes the step (auto-tick)"},
             "widok": {"type": "string", "description": "Viewport that completes the step: Top/Front/Right/Perspective"}},
             "required": ["tytul", "gdzie"]}}},
         "required": ["tytul", "kroki"]}},
    {"name": "oznacz_kroki",
     "description": "Ticks plan steps as done (they turn light in the chat) – use after you checked their work.",
     "input_schema": {"type": "object", "properties": {
         "numery": {"type": "array", "items": {"type": "integer"}}}, "required": ["numery"]}},
    {"name": "pokaz_miejsce",
     "description": "Stawia w modelu pomarańczowy znacznik z etykietą (np. 'kliknij tu'), żeby wskazać użytkownikowi miejsce.",
     "input_schema": {"type": "object", "properties": {"punkt": PUNKT, "etykieta": TEKST},
                      "required": ["punkt", "etykieta"]}},
    {"name": "usun_wskazowki",
     "description": "Usuwa wszystkie znaczniki postawione przez pokaz_miejsce.",
     "input_schema": PUSTE},
    {"name": "zaznacz_obiekty",
     "description": "Odznacza wszystko i zaznacza obiekty o podanych id.",
     "input_schema": {"type": "object", "properties": {
         "ids": {"type": "array", "items": {"type": "string"}}}, "required": ["ids"]}},
    {"name": "dodaj_linie",
     "description": "Rysuje odcinek między dwoma punktami.",
     "input_schema": {"type": "object", "properties": {"start": PUNKT, "koniec": PUNKT},
                      "required": ["start", "koniec"]}},
    {"name": "dodaj_okrag",
     "description": "Rysuje poziomy okrąg (w płaszczyźnie XY) o podanym środku i promieniu.",
     "input_schema": {"type": "object", "properties": {"srodek": PUNKT, "promien": LICZBA},
                      "required": ["srodek", "promien"]}},
    {"name": "dodaj_prostopadloscian",
     "description": "Tworzy prostopadłościan. 'rog' to dolny narożnik o najmniejszych X, Y, Z.",
     "input_schema": {"type": "object", "properties": {
         "rog": PUNKT, "dlugosc": LICZBA, "szerokosc": LICZBA, "wysokosc": LICZBA},
         "required": ["rog", "dlugosc", "szerokosc", "wysokosc"]}},
    {"name": "dodaj_kule",
     "description": "Tworzy kulę o podanym środku i promieniu.",
     "input_schema": {"type": "object", "properties": {"srodek": PUNKT, "promien": LICZBA},
                      "required": ["srodek", "promien"]}},
    {"name": "dodaj_walec",
     "description": "Tworzy pionowy walec stojący na podanym punkcie (środek dolnej podstawy).",
     "input_schema": {"type": "object", "properties": {"srodek": PUNKT, "promien": LICZBA, "wysokosc": LICZBA},
                      "required": ["srodek", "promien", "wysokosc"]}},
    {"name": "utworz_warstwe",
     "description": "Tworzy warstwę (jeśli nie istnieje) i ustawia ją jako aktualną.",
     "input_schema": {"type": "object", "properties": {
         "nazwa": TEKST,
         "kolor": {"type": "array", "items": {"type": "integer"}, "minItems": 3, "maxItems": 3}},
         "required": ["nazwa"]}},
    {"name": "wykonaj_komende",
     "description": "Uruchamia komendę Rhino, np. '_-Circle 0,0,0 5' albo '_ExtrudeCrv 10' (na zaznaczeniu).",
     "input_schema": {"type": "object", "properties": {"komenda": TEKST}, "required": ["komenda"]}},
    {"name": "wykonaj_kod",
     "description": ("Runs Python 3 code inside Rhino 8 (one undo step). Available names: rs (rhinoscriptsyntax), "
                     "Rhino, sc, doc (active document), math, System. Use it for booleans, fillets, arrays, lofts, "
                     "parametric shapes, editing or analysing objects. print() results – the output, new/deleted "
                     "object ids or the error traceback come back to you. The user sees the plan and may cancel it."),
     "input_schema": {"type": "object", "properties": {
         "opis": {"type": "string", "description": "Short title of what will happen, e.g. 'Cut a hole through the box'"},
         "kroki": {"type": "array", "items": {"type": "string"},
                   "description": ("2–5 plain-English points the user reads instead of the code: what will be created, "
                                   "changed or deleted, with sizes and layer names. No code terms.")},
         "kod": {"type": "string", "description": "Python 3 code with short English comments"}},
         "required": ["opis", "kroki", "kod"]}},
]


def wykonaj_narzedzie(nazwa, argumenty):
    if nazwa not in FUNKCJE:
        return f"BŁĄD: nie ma narzędzia o nazwie {nazwa}"
    try:
        wynik = FUNKCJE[nazwa](**argumenty)
        return wynik if isinstance(wynik, list) else str(wynik)   # lista = obraz + tekst, reszta jako tekst
    except Exception as blad:
        return f"BŁĄD: {blad}"


# ---------------------------------------------------------------------
# 4. PĘTLA AGENTA – ta sama co zawsze
# ---------------------------------------------------------------------

historia = []
praca = threading.Lock()   # pilnuje, żeby agent nie pracował nad dwiema wiadomościami naraz


def napraw_historie():
    """Bezpiecznik: API wymaga, żeby po każdej prośbie o narzędzie (tool_use)
    od razu był jego wynik (tool_result). Jeśli coś przerwało pracę w połowie,
    dopisujemy brakujące wyniki, zamiast wysyłać zepsutą historię (błąd 400)."""
    i = 0
    while i < len(historia):
        m = historia[i]
        if m["role"] == "assistant" and isinstance(m["content"], list):
            ids = [b["id"] for b in m["content"] if b.get("type") == "tool_use"]
            if ids:
                nast = historia[i + 1] if i + 1 < len(historia) else None
                ma_wyniki = nast is not None and nast["role"] == "user" and isinstance(nast["content"], list)
                if not ma_wyniki:
                    nast = {"role": "user", "content": []}
                    historia.insert(i + 1, nast)
                gotowe_ids = {b.get("tool_use_id") for b in nast["content"] if isinstance(b, dict)}
                for t in ids:
                    if t not in gotowe_ids:
                        nast["content"].insert(0, {"type": "tool_result", "tool_use_id": t,
                                                   "content": "BŁĄD: przerwane, narzędzie nie zostało wykonane"})
        i += 1


def zapytaj_agenta(wiadomosc):
    napraw_historie()
    historia.append({"role": "user", "content": wiadomosc})

    for _ in range(MAKS_KROKOW):
        napraw_historie()
        odpowiedz = wyslij_do_claude(historia)
        tresc = odpowiedz["content"]
        historia.append({"role": "assistant", "content": tresc})
        tekst = "".join(blok["text"] for blok in tresc if blok["type"] == "text").strip()

        if odpowiedz["stop_reason"] != "tool_use":
            return tekst

        if tekst:
            js("wiadomosc", {"rola": "agent", "tekst": tekst})

        wyniki = []
        for blok in tresc:
            if blok["type"] == "tool_use":
                if blok["name"] == "wykonaj_kod":         # osobna droga: pokaż kod, poczekaj na "Run"
                    wynik = obsluz_kod(blok["id"], blok["input"])
                else:
                    if blok["name"] != "pokaz_kroki":     # plan pokazuje się sam
                        js("narzedzie", {"nazwa": blok["name"]})
                    wynik = w_watku_rhino(lambda b=blok: wykonaj_narzedzie(b["name"], b["input"]))
                wyniki.append({"type": "tool_result", "tool_use_id": blok["id"], "content": wynik})

        historia.append({"role": "user", "content": wyniki})

    historia.append({"role": "assistant", "content": "(przerwano: za dużo kroków)"})
    return "I stopped because that was a lot of steps in a row. Tell me if I should continue."


# ---------------------------------------------------------------------
# 4b. WYKONAJ_KOD – agent pisze i uruchamia własny kod
# ---------------------------------------------------------------------
#  1) Agent wysyła kod. 2) Pokazujemy go w czacie z przyciskami Run / Cancel.
#  3) Wątek agenta CZEKA (w tle – Rhino się nie zawiesza), aż klikniesz.
#  4) Po "Run" kod wykonuje się w głównym wątku Rhino jako jeden krok cofania (Cmd+Z).
#  5) Wynik – to, co kod wypisał, nowe obiekty albo błąd – wraca do agenta.

decyzje = {}   # id kodu -> {"zdarzenie": Event, "uruchom": True/False}


def uruchom_kod(kod):
    bufor = io.StringIO()
    przestrzen = {"rs": rs, "Rhino": Rhino, "sc": sc, "doc": dokument(), "math": math,
                  "System": System, "__name__": "kod_agenta"}
    przed = {str(i) for i in (rs.AllObjects() or [])}
    try:
        with contextlib.redirect_stdout(bufor):
            exec(compile(kod, "<kod agenta>", "exec"), przestrzen)
    except Exception:
        return ("ERROR – fix the code and try again:\n" + traceback.format_exc(limit=4)[-2500:]
                + "\nOutput before the error:\n" + bufor.getvalue()[-1500:])
    finally:
        dokument().Views.Redraw()
    po = {str(i) for i in (rs.AllObjects() or [])}
    nowe, usuniete = sorted(po - przed), sorted(przed - po)
    return (f"OK. Output:\n{bufor.getvalue()[-3000:] or '(nothing printed)'}\n"
            f"New objects ({len(nowe)}): {nowe[:30]}\nDeleted objects ({len(usuniete)}): {usuniete[:30]}")


def obsluz_kod(id_kodu, wejscie):
    kod, opis, kroki = wejscie.get("kod", ""), wejscie.get("opis", ""), wejscie.get("kroki", [])
    if POTWIERDZAJ_KOD:
        decyzje[id_kodu] = {"zdarzenie": threading.Event(), "uruchom": False}
        js("kod", {"id": id_kodu, "opis": opis, "kroki": kroki, "kod": kod})
        decyzje[id_kodu]["zdarzenie"].wait()            # czekamy na kliknięcie Run / Cancel
        if not decyzje.pop(id_kodu)["uruchom"]:
            return "The user cancelled this code. Ask them what to change, don't run it again unasked."
    else:
        js("kod", {"id": id_kodu, "opis": opis, "kroki": kroki, "kod": kod, "auto": True})
    wynik = w_watku_rhino(lambda: z_cofaniem("kod agenta", lambda: uruchom_kod(kod)))
    js("kodWynik", {"id": id_kodu, "ok": wynik.startswith("OK")})
    return wynik


# ---------------------------------------------------------------------
# 5. WĄTKI – żeby Rhino nie zamarzało
# ---------------------------------------------------------------------

def w_glownym_watku(funkcja):
    """Zleca funkcję głównemu wątkowi Rhino, bez czekania."""
    forms.Application.Instance.AsyncInvoke(System.Action(funkcja))


def w_watku_rhino(funkcja):
    """Zleca funkcję głównemu wątkowi Rhino i czeka na wynik."""
    wynik = {"wartosc": "BŁĄD: narzędzie nie zwróciło wyniku"}
    gotowe = threading.Event()

    def uruchom():
        try:
            wynik["wartosc"] = funkcja()
        except Exception as blad:
            wynik["wartosc"] = f"BŁĄD: {blad}"
        finally:
            gotowe.set()

    w_glownym_watku(uruchom)
    gotowe.wait()
    return wynik["wartosc"]


# ---------------------------------------------------------------------
# 6. OKNO I MOST PYTHON <-> STRONA
# ---------------------------------------------------------------------
#  Okno zawiera WebView – małą przeglądarkę, która wyświetla stronę z części 7.
#  Rozmowa w dwie strony:
#    Python -> strona: wywołujemy funkcję JavaScript, np. agent.wiadomosc({...})
#    strona -> Python: strona "otwiera" adres agent://wyslij?d=..., a my go
#                      przechwytujemy, zanim przeglądarka dokądkolwiek pójdzie.

stan_okna = {"webview": None, "gotowe": False, "kolejka": []}


def js(funkcja, dane=None):
    """Wywołuje funkcję agent.<funkcja>(dane) na stronie. Działa z każdego wątku."""
    skrypt = f"window.agent.{funkcja}({json.dumps(dane)})"

    def uruchom():
        if not stan_okna["gotowe"]:
            stan_okna["kolejka"].append(skrypt)   # strona jeszcze się ładuje – wyślemy później
            return
        try:
            stan_okna["webview"].ExecuteScript(skrypt)
        except Exception:
            pass   # okno mogło zostać zamknięte

    w_glownym_watku(uruchom)


def praca_w_tle(wiadomosc):
    stan = len(historia)
    try:
        odpowiedz = zapytaj_agenta(wiadomosc)
        if odpowiedz:
            js("wiadomosc", {"rola": "agent", "tekst": odpowiedz})
    except BaseException as blad:
        del historia[stan:]          # cofamy niedokończoną wymianę – historia zostaje poprawna
        js("wiadomosc", {"rola": "blad", "tekst": f"Something went wrong: {blad}"})
    finally:
        praca.release()
        js("zajety", False)


def obsluz_akcje(akcja, dane):
    """Akcje przychodzące ze strony (kliknięcia, wysłane wiadomości)."""
    if akcja == "wyslij":
        if not KLUCZ:
            js("wiadomosc", {"rola": "blad", "tekst": "I couldn't find an API key in ~/.zshrc."})
            return
        if not praca.acquire(blocking=False):
            js("wiadomosc", {"rola": "blad", "tekst": "I'm still working on your previous message – give me a moment."})
            return
        js("zajety", True)
        dla_modelu = dane + postep_planu()             # użytkownik widzi swój tekst, model dostaje też postęp planu
        threading.Thread(target=praca_w_tle, args=(dla_modelu,), daemon=True).start()
    elif akcja == "tryb":
        tryb["wartosc"] = dane
    elif akcja == "kod":
        id_kodu, _, wybor = dane.rpartition(":")      # np. "toolu_123:run"
        if id_kodu in decyzje:
            decyzje[id_kodu]["uruchom"] = (wybor == "run")
            decyzje[id_kodu]["zdarzenie"].set()
    elif akcja == "kopiuj":
        schowek = forms.Clipboard()
        schowek.Text = dane          # kliknięty chip, np. "Circle", trafia do schowka – wklejasz Cmd+V
    elif akcja == "nowa":
        if praca.locked():
            return
        historia.clear()
        ostatnie.clear()
        plan.update({"tytul": "", "kroki": []})


def przy_koncu_komendy(sender, e):
    """Rhino woła tę funkcję po każdej zakończonej komendzie – tak agent widzi, co robisz."""
    try:
        ostatnie.append(f"{e.CommandEnglishName} ({e.CommandResult})")
        del ostatnie[:-15]
        if str(e.CommandResult) == "Success":          # komenda z planu? krok sam się odhacza
            odhacz_po_zdarzeniu("komenda", e.CommandEnglishName.lower())
    except Exception:
        pass


def przy_zmianie_widoku(sender, e):
    """Rhino woła tę funkcję, gdy klikniesz w inny widok (Top, Front…) – krok z planu może się odhaczyć."""
    try:
        odhacz_po_zdarzeniu("widok", e.View.ActiveViewport.Name.lower())
    except Exception:
        pass


def wlacz_sluchacza():
    stary = sc.sticky.get("agent_czat_sluchacz")
    if stary is not None:
        try:
            Rhino.Commands.Command.EndCommand -= stary[0]
            Rhino.Display.RhinoView.SetActive -= stary[1]
        except Exception:
            pass
    Rhino.Commands.Command.EndCommand += przy_koncu_komendy
    try:
        Rhino.Display.RhinoView.SetActive += przy_zmianie_widoku
    except Exception:
        pass
    sc.sticky["agent_czat_sluchacz"] = (przy_koncu_komendy, przy_zmianie_widoku)


def pokaz_okno():
    stare = sc.sticky.get("agent_czat_okno")
    if stare is not None:
        try:
            if stare.Visible:
                stare.BringToFront()
                return
        except Exception:
            pass

    okno = forms.Form()
    okno.Title = "Agent Rhino"
    okno.ClientSize = drawing.Size(420, 720)
    okno.MinimumSize = drawing.Size(340, 480)
    okno.Resizable = True
    okno.Location = drawing.Point(100, 100)
    okno.Topmost = True

    webview = forms.WebView()
    stan_okna.update({"webview": webview, "gotowe": False, "kolejka": []})

    def przy_ladowaniu(sender, e):
        adres = e.Uri.AbsoluteUri if e.Uri is not None else ""
        if not adres.startswith("agent://"):
            return                      # zwykłe ładowanie strony – nie przeszkadzamy
        e.Cancel = True                 # to nasza wiadomość, nie prawdziwy adres
        czesci = urllib.parse.urlparse(adres)
        dane = urllib.parse.parse_qs(czesci.query).get("d", [""])[0]
        obsluz_akcje(czesci.netloc, dane)

    def przy_zaladowaniu(sender, e):
        if stan_okna["gotowe"]:
            return
        stan_okna["gotowe"] = True
        for skrypt in stan_okna["kolejka"]:
            webview.ExecuteScript(skrypt)
        stan_okna["kolejka"] = []

    def przy_zamknieciu(sender, e):
        sc.sticky.pop("agent_czat_okno", None)
        stan_okna["gotowe"] = False

    webview.DocumentLoading += przy_ladowaniu
    webview.DocumentLoaded += przy_zaladowaniu
    okno.Closed += przy_zamknieciu

    okno.Content = webview
    okno.Owner = Rhino.UI.RhinoEtoApp.MainWindow
    sc.sticky["agent_czat_okno"] = okno
    webview.LoadHtml(STRONA)
    okno.Show()

    wlacz_sluchacza()
    js("motyw", MOTYW)
    if KLUCZ:
        js("wiadomosc", {"rola": "agent", "tekst": "Hi! Tell me what you'd like to learn in Rhino. In **Learn** mode I'll guide you step by step; in **Make** mode I'll do it for you."})
    else:
        js("wiadomosc", {"rola": "blad", "tekst": "I couldn't find an API key. Check that ~/.zshrc has the line export ANTHROPIC_API_KEY=\"...\"."})


# ---------------------------------------------------------------------
# 7. WYGLĄD – strona HTML + CSS + JavaScript
# ---------------------------------------------------------------------
#  Wygląd przeniesiony z projektu w Figmie ("robocze", ramka MacBook Air - 1).
#  Kolory zmieniasz w sekcji :root (ciemny motyw) i w sekcji
#  :root[data-motyw="jasny"] (jasny motyw – włączasz go zmienną MOTYW w części 1).
#  Skróty: Enter = wyślij, Shift+Enter = nowa linia, Cmd+K = nowa rozmowa.

STRONA = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
  /* Kolory z projektu w Figmie: ciemny (domyślnie) i jasny (MOTYW = "jasny" w części 1) */
  :root {
    --tlo: linear-gradient(180deg, #0c0c10 0%, #15151b 100%);   /* tło okna: czerń z nutą granatu */
    --ramka: #b5b5b5;
    --pole: #1d1d23;         /* pole pisania i dymek pytania */
    --pole-ramka: rgba(255,255,255,.16);
    --tekst: #ffffff;
    --etykieta: #7d7d7d;     /* "Krok 1", "Gdzie" */
    --wylaczone: #525252;    /* nieaktywny tryb */
    --chip: #616161;         /* np. [Circle] */
    --chip-tekst: #ffffff;
    --check-tlo: #ffffff;    /* zaznaczony checkbox */
    --check-znak: #000000;
    --check-pusty: #202020;  /* pusty checkbox */
    --linia: rgba(255,255,255,.12);
    --enter-znak: #ffffff;
  }
  :root[data-motyw="jasny"] {
      --tlo: linear-gradient(180deg, #fbfbfd 0%, #f3f3f6 100%); --ramka: #999999; --pole: #ffffff; --pole-ramka: #e8e8ee;
      --tekst: #000000; --etykieta: #7d7d7d; --wylaczone: #b5b5b5;
      --chip: #e2e2e2; --chip-tekst: #000000;
      --check-tlo: #000000; --check-znak: #ffffff; --check-pusty: #efefef;
      --linia: rgba(0,0,0,.08); --enter-znak: #111111;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; height: 100%; }
  body {
    background: var(--tlo); background-attachment: fixed; color: var(--tekst);
    font: 13px/1.45 Inter, -apple-system, BlinkMacSystemFont, "Helvetica Neue", sans-serif;
    display: flex; flex-direction: column;
  }

  /* --- rozmowa --- */
  #rozmowa { flex: 1; overflow-y: auto; padding: 22px 18px 10px; }
  .blok { margin-bottom: 22px; animation: pojaw .2s ease-out; }

  /* wiersz: szara etykieta po lewej, treść po prawej */
  .wiersz { display: grid; grid-template-columns: 58px 1fr; column-gap: 0; margin-bottom: 5px; }
  .wiersz .et { color: var(--etykieta); }
  .wiersz .tr { overflow-wrap: anywhere; }
  .wiersz .tr p { margin: 0 0 6px; } .wiersz .tr p:last-child { margin: 0; }
  .wiersz .tr ul, .wiersz .tr ol { margin: 2px 0 6px; padding-left: 18px; }
  .tekst-agenta { overflow-wrap: anywhere; }
  .tekst-agenta p { margin: 0 0 6px; } .tekst-agenta p:last-child { margin: 0; }
  .tekst-agenta ul, .tekst-agenta ol { margin: 2px 0 6px; padding-left: 18px; }
  strong { font-weight: 500; }   /* medium zamiast bold – delikatniejsze wyróżnienie */

  /* dymek pytania (po prawej) */
  .pytanie { display: flex; justify-content: flex-end; }
  .pytanie .dymek {
    max-width: 85%; padding: 10px 19px; border-radius: 16px;
    background: var(--pole); border: .5px solid var(--pole-ramka);
    overflow-wrap: anywhere;
  }

  /* chip z tekstem do wpisania, np. [Circle ⧉] */
  .chip {
    display: inline-flex; align-items: center; gap: 4px;
    color: var(--tekst);
    font-weight: 500; cursor: pointer;
  }
  .chip svg { opacity: .6; }
  .chip.skopiowane svg { opacity: 1; }
  code { font: inherit; font-weight: 500; }

  /* checkbox "zrobione" */
  /* odpowiedzi agenta trochę mniejsze niż Twoje pytania */
  .tekst-agenta, .plan, .kod, .narzedzie { font-size: 12px; }
  /* karta planu: jeden krok naraz, w stylu Twoich dymków */
  .plan {
    background: var(--pole); border: .5px solid var(--pole-ramka); border-radius: 16px;
    padding: 14px 16px 12px; overflow: hidden;
  }
  .plan.stary { opacity: .5; }
  .plan-gora { display: flex; align-items: center; gap: 10px; margin-bottom: 12px; }
  .plan-tytul { font-weight: 500; flex: 1; }
  .plan-nav { display: flex; align-items: center; gap: 6px; color: var(--etykieta); white-space: nowrap; }
  .plan-nav button { border: 0; background: none; padding: 0 3px; font: inherit; font-size: 15px; line-height: 1;
                     color: var(--etykieta); cursor: pointer; }
  .plan-nav button:disabled { opacity: .3; cursor: default; }
  .plan-tresc { transition: opacity .3s ease; }
  .plan-tresc.zrobiony { opacity: .35; }
  .plan-tresc.wjazd { animation: wjazd .35s ease-out; }
  .plan-tresc.wjazd-wstecz { animation: wjazd-wstecz .35s ease-out; }
  .plan .pomoc { margin-left: 58px; }
  .plan-koniec { color: var(--etykieta); margin-top: 4px; }
  .plan-pasek { display: flex; gap: 4px; margin-top: 12px; }
  .plan-pasek span { flex: 1; height: 3px; border-radius: 2px; background: var(--linia); transition: background .3s; }
  .plan-pasek span.zrobiony { background: var(--tekst); opacity: .75; }
  .plan-pasek span.aktualny { background: var(--etykieta); }
  @keyframes wjazd { from { opacity: 0; transform: translateX(18px); } to { opacity: 1; transform: none; } }
  @keyframes wjazd-wstecz { from { opacity: 0; transform: translateX(-18px); } to { opacity: 1; transform: none; } }
  .tekst-agenta ol, .wiersz .tr ol { padding-left: 18px; }
  .pomoc { color: var(--etykieta); font-size: 12px; cursor: pointer; background: none; border: 0;
           padding: 0; margin: 10px 0 0 58px; font-family: inherit; }
  .pomoc:hover { color: var(--tekst); }
  .zrobiony .pomoc { display: none; }

  .narzedzie, .info { color: var(--etykieta); font-size: 11.5px; margin: -12px 0 18px; animation: pojaw .2s ease-out; }
  .info { margin: 0 0 18px; }
  .blad { color: #ff6b5e; }
  :root[data-motyw="jasny"] .blad { color: #c0362c; }

  /* "agent pisze…" */
  .pisze { display: flex; gap: 4px; padding: 4px 0 18px 58px; }
  .pisze span { width: 5px; height: 5px; border-radius: 50%; background: var(--etykieta); animation: kropka 1.2s infinite; }
  .pisze span:nth-child(2) { animation-delay: .15s; } .pisze span:nth-child(3) { animation-delay: .3s; }

  /* --- pole pisania --- */
  footer { padding: 0 16px 16px; }
  .pole {
    background: var(--pole); border: .5px solid var(--pole-ramka); border-radius: 16px;
    display: flex; flex-direction: column;
  }
  textarea {
    border: 0; outline: 0; resize: none; background: transparent; color: var(--tekst);
    font: inherit; padding: 13px 18px 6px; min-height: 92px; max-height: 180px;
  }
  textarea::placeholder { color: var(--etykieta); }
  .dol {
    display: flex; align-items: center; gap: 12px;
    padding: 4px 14px 11px 18px;
  }
  .tryb { border: 0; background: none; padding: 0; font: inherit; color: var(--wylaczone); cursor: pointer; }
  .tryb.wl { color: var(--tekst); }
  .odstep { flex: 1; }
  #wyslij {
    display: flex; align-items: center; gap: 3px; border: 0; background: none;
    font: inherit; color: var(--tekst); cursor: pointer; padding: 0;
  }
  #wyslij .enter { display: grid; place-items: center; }
  #wyslij:disabled { opacity: .35; cursor: default; }

  /* karta kodu */
  .kod .opis { font-weight: 500; }
  .kod ul { margin: 4px 0 0; padding-left: 16px; }
  .kod li { margin: 2px 0; }
  .kod pre { display: none; margin-top: 10px; }
  .kod.pokaz-kod pre { display: block; }
  .kod pre {
    margin: 0; padding: 12px 14px; max-height: 230px; overflow: auto;
    background: var(--pole); border: .5px solid var(--pole-ramka); border-radius: 12px;
    font: 11.5px/1.5 ui-monospace, Menlo, monospace; white-space: pre; color: var(--tekst);
  }
  .kod .akcje { display: flex; gap: 16px; margin: 10px 0 0 2px; align-items: center; }
  .kod .akcje button { border: 0; background: none; padding: 0; font: inherit; cursor: pointer; color: var(--tekst); font-weight: 500; }
  .kod .akcje button.anuluj, .kod .akcje button.pokaz { color: var(--etykieta); font-weight: 400; }
  .kod .akcje .pokaz { margin-left: auto; font-size: 12px; }
  .kod .status { color: var(--etykieta); font-size: 12px; }
  .kod.zamkniety .akcje button.uruchom, .kod.zamkniety .akcje button.anuluj { display: none; }
  @keyframes pojaw { from { opacity: 0; transform: translateY(4px); } to { opacity: 1; transform: none; } }
  @keyframes kropka { 0%, 60%, 100% { opacity: .3; } 30% { opacity: 1; } }
</style>
</head>
<body>
  <main id="rozmowa"></main>

  <footer>
    <div class="pole">
      <textarea id="tekst" placeholder="What would you like to learn?"></textarea>
      <div class="dol">
        <button class="tryb wl" id="t-nauka" onclick="ustawTryb('nauka')">Learn</button>
        <button class="tryb" id="t-zrob" onclick="ustawTryb('zrob')">Make</button>
        <span class="odstep"></span>
        <button id="wyslij" onclick="wyslij()" title="Send (Enter)">Ask
          <span class="enter"><svg width="15" height="15" viewBox="0 0 16 16" fill="none"><path d="M13 3v5.5a1.5 1.5 0 0 1-1.5 1.5H3.5M6.5 7 3.5 10l3 3" stroke="var(--enter-znak)" stroke-width="1.1" stroke-linecap="round" stroke-linejoin="round"/></svg></span>
        </button>
      </div>
    </div>
  </footer>

<script>
  const rozmowa = document.getElementById('rozmowa');
  const pole = document.getElementById('tekst');
  const przycisk = document.getElementById('wyslij');
  let zajety = false;

  // --- do Pythona: "otwieramy" adres agent://..., Python go przechwytuje ---
  function doPythona(akcja, dane) {
    window.location.href = 'agent://' + akcja + '?d=' + encodeURIComponent(dane || '');
  }

  // --- prosty markdown: **pogrubienie**, `komenda`, listy, akapity ---
  const esc = s => String(s).replace(/&/g, '&amp;').replace(/</g, '&lt;').replace(/>/g, '&gt;');
  const inline = s => esc(s).replace(/`([^`]+)`/g, '<code>$1</code>').replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>');
  function md(t) {
    // akapity + listy (także gdy lista zaczyna się w środku akapitu)
    return String(t).trim().split(/\n\s*\n/).map(blok => {
      let html = '', tekst = [], lista = null, typ = null;
      const zamknijTekst = () => { if (tekst.length) { html += '<p>' + tekst.map(inline).join('<br>') + '</p>'; tekst = []; } };
      const zamknijListe = () => { if (lista) { html += '<' + typ + '>' + lista.map(l => '<li>' + inline(l) + '</li>').join('') + '</' + typ + '>'; lista = null; } };
      blok.split('\n').forEach(l => {
        const ul = l.match(/^\s*[-•–]\s+(.*)/), ol = l.match(/^\s*\d+[.)]\s+(.*)/);
        const m = ol || ul, t2 = ol ? 'ol' : 'ul';
        if (m) { zamknijTekst(); if (lista && typ !== t2) zamknijListe(); if (!lista) { lista = []; typ = t2; } lista.push(m[1]); }
        else { zamknijListe(); tekst.push(l); }
      });
      zamknijTekst(); zamknijListe();
      return html;
    }).join('');
  }

  const IKONA_KOPIUJ = '<svg width="9" height="9" viewBox="0 0 10 10" fill="none"><rect x="3" y="3" width="6" height="6" rx="1" stroke="currentColor"/><path d="M1 7V1.5A.5.5 0 0 1 1.5 1H7" stroke="currentColor"/></svg>';

  function przewin() { rozmowa.scrollTop = rozmowa.scrollHeight; }
  function el(tag, klasa, html) { const e = document.createElement(tag); if (klasa) e.className = klasa; if (html != null) e.innerHTML = html; return e; }
  function wiersz(etykieta, html) { return '<div class="wiersz"><div class="et">' + esc(etykieta) + '</div><div class="tr">' + html + '</div></div>'; }
  function dodaj(e) { usunPisze(); rozmowa.appendChild(e); if (zajety) pokazPisze(); przewin(); }
  function pokazPisze() { if (!document.querySelector('.pisze')) { rozmowa.appendChild(el('div', 'pisze', '<span></span><span></span><span></span>')); przewin(); } }
  function usunPisze() { const p = document.querySelector('.pisze'); if (p) p.remove(); }
  const bezP = s => md(s).replace(/<\/?p>/g, '');

  function pokazKrokPlanu(p, i, kierunek) {
    const st = p.stan, n = st.kroki.length;
    if (i < 0 || i >= n) return;
    st.widoczny = i;
    const s = st.kroki[i], tresc = p.querySelector('.plan-tresc');
    let gdzie = bezP(s.gdzie || '');
    if (s.wpisz) gdzie += ' <span class="chip" title="Click to copy">' + esc(s.wpisz) + IKONA_KOPIUJ + '</span>';
    let html = wiersz('Step ' + (i + 1), bezP(s.tytul)) + wiersz('Where', gdzie);
    if (s.potem) html += wiersz('Then', bezP(s.potem));
    if (s.wskazowka) html += wiersz('Tip', '<span style="color:var(--etykieta)">' + bezP(s.wskazowka) + '</span>');
    if (i === st.aktualny) html += '<button class="pomoc">not sure how?</button>';
    if (st.aktualny === -1) html += '<div class="plan-koniec">All steps done – the agent is checking your result.</div>';
    tresc.innerHTML = html;
    tresc.classList.toggle('zrobiony', st.zrobione.has(i) && st.aktualny !== -1);
    tresc.classList.remove('wjazd', 'wjazd-wstecz'); void tresc.offsetWidth;   // restart animacji
    if (kierunek) tresc.classList.add(kierunek > 0 ? 'wjazd' : 'wjazd-wstecz');
    const chip = tresc.querySelector('.chip');
    if (chip) chip.onclick = () => { doPythona('kopiuj', s.wpisz); chip.classList.add('skopiowane'); };
    const pomoc = tresc.querySelector('.pomoc');
    if (pomoc) pomoc.onclick = () => { if (zajety) return; wyslijTekst("I'm stuck on step " + (i + 1) + ', please explain it differently.'); };
    p.querySelector('.licznik').textContent = (i + 1) + ' / ' + n;
    p.querySelector('.wstecz').disabled = i === 0;
    p.querySelector('.dalej').disabled = i === n - 1;
    p.querySelectorAll('.plan-pasek span').forEach((b, j) => {
      b.className = st.zrobione.has(j) ? 'zrobiony' : (j === st.aktualny ? 'aktualny' : '');
    });
    przewin();
  }

  // --- funkcje, które woła Python ---
  window.agent = {
    wiadomosc(d) {
      if (d.rola === 'ty') {
        const b = el('div', 'blok pytanie'); b.appendChild(el('div', 'dymek', inline(d.tekst))); dodaj(b);
      } else if (d.rola === 'blad') {
        dodaj(el('div', 'blok tekst-agenta blad', md(d.tekst)));
      } else {
        dodaj(el('div', 'blok tekst-agenta', md(d.tekst)));
      }
    },
    narzedzie(d) {
      const opisy = { opisz_model: 'checking the model', opisz_zaznaczone: 'checking your selection',
        ostatnie_komendy: 'checking your commands', pokaz_miejsce: 'placing a marker', usun_wskazowki: 'removing markers',
        zaznacz_obiekty: 'selecting objects', wykonaj_komende: 'running a command', dodaj_linie: 'drawing a line',
        dodaj_okrag: 'drawing a circle', dodaj_prostopadloscian: 'drawing a box', dodaj_kule: 'drawing a sphere',
        dodaj_walec: 'drawing a cylinder', utworz_warstwe: 'setting the layer', zrzut_widoku: 'looking at the viewport',
        opisz_grasshoppera: 'checking Grasshopper', ustaw_suwak: 'moving a slider', zaznacz_komponent: 'highlighting a component' };
      dodaj(el('div', 'narzedzie', esc(opisy[d.nazwa] || d.nazwa)));
    },
    plan(d) {
      // karta planu: jeden krok naraz; po zrobieniu sama przesuwa się do następnego
      document.querySelectorAll('.plan').forEach(stary => stary.classList.add('stary'));
      const p = el('div', 'blok plan');
      p.innerHTML = '<div class="plan-gora"><div class="plan-tytul"></div><div class="plan-nav">' +
        '<button class="wstecz" title="Previous step">‹</button><span class="licznik"></span>' +
        '<button class="dalej" title="Next step">›</button></div></div><div class="plan-tresc"></div><div class="plan-pasek"></div>';
      p.querySelector('.plan-tytul').innerHTML = bezP(d.tytul || 'Plan');
      p.stan = { kroki: d.kroki || [], aktualny: 0, widoczny: 0, zrobione: new Set() };
      p.querySelector('.wstecz').onclick = () => pokazKrokPlanu(p, p.stan.widoczny - 1, -1);
      p.querySelector('.dalej').onclick = () => pokazKrokPlanu(p, p.stan.widoczny + 1, 1);
      p.querySelector('.plan-pasek').innerHTML = p.stan.kroki.map(() => '<span></span>').join('');
      pokazKrokPlanu(p, 0, 0);
      dodaj(p);
    },
    planZrobione(d) {
      const p = [...document.querySelectorAll('.plan')].pop(); if (!p) return;
      d.numery.forEach(n => p.stan.zrobione.add(n - 1));
      if (d.aktualny == null) { p.stan.aktualny = -1; pokazKrokPlanu(p, p.stan.kroki.length - 1, 0); }
      else { p.stan.aktualny = d.aktualny - 1; pokazKrokPlanu(p, d.aktualny - 1, 1); }   // przewija do następnego
    },
    wyslijAuto(t) { wyslijTekst(t); },
    motyw(m) {
      const ustaw = v => { document.documentElement.dataset.motyw = v; };
      if (m !== 'auto') return ustaw(m);
      const mq = window.matchMedia('(prefers-color-scheme: light)');
      const odswiez = () => ustaw(mq.matches ? 'jasny' : 'ciemny');
      odswiez();
      if (mq.addEventListener) mq.addEventListener('change', odswiez);   // przełącza się na żywo razem z Makiem
    },
    kod(d) {
      // plan od agenta po ludzku + zwinięty kod + Run / Cancel (działa także, gdy agent "pisze")
      const k = el('div', 'blok kod');
      const lista = (d.kroki || []).map(t => '<li>' + bezP(t) + '</li>').join('');
      k.innerHTML = wiersz('Plan', '<div class="opis">' + bezP(d.opis || '') + '</div>' + (lista ? '<ul>' + lista + '</ul>' : ''));
      const pre = el('pre'); pre.textContent = d.kod; k.querySelector('.tr').appendChild(pre);
      const akcje = el('div', 'akcje', (d.auto ? '<span class="status">running…</span>'
        : '<button class="uruchom">Run</button><button class="anuluj">Cancel</button><span class="status"></span>')
        + '<button class="pokaz">show code</button>');
      k.querySelector('.tr').appendChild(akcje);
      k.dataset.id = d.id;
      const pokaz = akcje.querySelector('.pokaz');
      pokaz.onclick = () => { const p = k.classList.toggle('pokaz-kod'); pokaz.textContent = p ? 'hide code' : 'show code'; przewin(); };
      if (!d.auto) {
        const status = akcje.querySelector('.status');
        akcje.querySelector('.uruchom').onclick = () => { k.classList.add('zamkniety'); status.textContent = 'running…'; doPythona('kod', d.id + ':run'); };
        akcje.querySelector('.anuluj').onclick = () => { k.classList.add('zamkniety'); status.textContent = 'cancelled'; doPythona('kod', d.id + ':cancel'); };
      }
      dodaj(k);
    },
    kodWynik(d) {
      const k = document.querySelector('.kod[data-id="' + d.id + '"]');
      if (k) { k.classList.add('zamkniety'); k.querySelector('.status').textContent = d.ok ? 'done' : 'error – the agent will fix it'; }
    },
    zajety(z) { zajety = z; przycisk.disabled = z; if (z) pokazPisze(); else { usunPisze(); pole.focus(); } },
  };

  // --- akcje użytkownika ---
  function wyslijTekst(t) {
    if (zajety || !t.trim()) return;
    agent.wiadomosc({ rola: 'ty', tekst: t.trim() });
    doPythona('wyslij', t.trim());
  }
  function wyslij() { const t = pole.value; pole.value = ''; wyslijTekst(t); }

  function ustawTryb(t) {
    document.getElementById('t-nauka').classList.toggle('wl', t === 'nauka');
    document.getElementById('t-zrob').classList.toggle('wl', t === 'zrob');
    pole.placeholder = t === 'nauka' ? 'What would you like to learn?' : 'What should I do in the model?';
    doPythona('tryb', t);
  }
  function nowaRozmowa() { if (zajety) return; rozmowa.innerHTML = ''; doPythona('nowa', ''); }

  pole.addEventListener('keydown', e => {
    if (e.key === 'Enter' && !e.shiftKey) { e.preventDefault(); wyslij(); }
    if (e.key === 'k' && e.metaKey) { e.preventDefault(); nowaRozmowa(); }   // Cmd+K = nowa rozmowa
  });
  pole.focus();
</script>
</body>
</html>
"""


pokaz_okno()
