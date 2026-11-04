#!/usr/bin/env python3
"""Nombres colles et alphabet radio (lib/atomes_parole.py). Sans reseau."""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "lib"))
import atomes_parole as a


def mots(n):
    return a.mots_entier(n)


def genres(text):
    return [(m, g) for m, g in a.decouper(text)]


def assert_eq(got, exp, label):
    if got != exp:
        raise SystemExit(f"{label}\n  obtenu : {got}\n  attendu : {exp}")


def test_nombres():
    assert_eq(mots(0), ["zéro"], "0")
    assert_eq(mots(1), ["un"], "1")
    assert_eq(mots(10), ["dix"], "10")
    assert_eq(mots(14), ["quatorze"], "14")
    assert_eq(mots(15), ["quinze"], "15")
    assert_eq(mots(16), ["seize"], "16")
    assert_eq(mots(17), ["dix-sept"], "17")
    assert_eq(mots(18), ["dix-huit"], "18")
    assert_eq(mots(19), ["dix-neuf"], "19")
    assert_eq(mots(20), ["vingt"], "20")
    assert_eq(mots(21), ["vingt", "et un"], "21")
    assert_eq(mots(22), ["vingt", "deux"], "22")
    assert_eq(mots(37), ["trente", "sept"], "37")
    assert_eq(mots(70), ["soixante", "dix"], "70")
    assert_eq(mots(71), ["soixante", "et onze"], "71")
    assert_eq(mots(72), ["soixante", "douze"], "72")
    assert_eq(mots(77), ["soixante", "dix-sept"], "77")
    assert_eq(mots(80), ["quatre-vingts"], "80")
    assert_eq(mots(81), ["quatre-vingts", "un"], "81")
    assert_eq(mots(90), ["quatre-vingt-dix"], "90")
    assert_eq(mots(91), ["quatre-vingts", "onze"], "91")
    assert_eq(mots(97), ["quatre-vingts", "dix-sept"], "97")
    assert_eq(mots(100), ["100"], "100")
    assert_eq(mots(101), ["100", "un"], "101")
    assert_eq(mots(200), ["200"], "200")
    assert_eq(mots(410), ["400", "dix"], "410")
    assert_eq(mots(1000), ["1000"], "1000")
    assert_eq(mots(1800), ["1000", "800"], "1800")
    assert_eq(mots(2000), ["2000"], "2000")
    assert_eq(mots(6612), ["6000", "600", "douze"], "6612")
    assert_eq(mots(10000), ["10000"], "10000")
    assert_eq(mots(2500), ["2000", "500"], "2500")
    assert_eq(mots(1_000_000), ["un", "million"], "1e6")
    assert_eq(mots(2_000_000), ["deux", "millions"], "2e6")
    assert_eq(a.mots_nombre("12,5"), ["douze", "virgule", "cinq"], "12,5")
    assert_eq(a.mots_nombre("3,05"), ["trois", "virgule", "zéro", "cinq"], "3,05")
    assert_eq(a.mots_nombre("007"), ["zéro", "zéro", "sept"], "007")
    assert_eq(a.mots_nombre("1 200"), ["1000", "200"], "1 200")
    assert_eq(a.mots_nombre("-12"), ["moins", "douze"], "-12")


def test_indicatifs():
    assert_eq(
        [m for m, _ in a.mots_indicatif("AFR410")],
        ["alfa", "foxtrotte", "roméo", "400", "dix"],
        "AFR410",
    )
    assert_eq(
        [m for m, _ in a.mots_indicatif("F-HBOD")],
        ["foxtrotte", "hôtel", "bravo", "oscar", "delta"],
        "F-HBOD",
    )
    assert_eq(
        [m for m, _ in a.mots_indicatif("F4ABC-9")],
        ["foxtrotte", "quatre", "alfa", "bravo", "charli", "neuf"],
        "F4ABC-9",
    )


def test_decoupe():
    assert_eq(genres("bonjour le monde"), [("bonjour le monde", "prose")], "phrase")
    assert_eq(
        genres("410"),
        [("400", "nombre"), ("dix", "nombre")],
        "410 seul",
    )
    assert_eq(genres("410."), [("400", "nombre"), ("dix", "nombre")], "410 sans point")
    assert_eq(genres("17,"), [("dix-sept", "nombre")], "17 sans virgule")
    assert_eq(genres("axe X"), [("axe X", "prose")], "axe reste un mot")
    assert_eq(genres("axe X."), [("axe X.", "prose")], "axe X avec point")
    assert_eq(
        genres("X : 12 millimètres"),
        [("X : ", "prose"), ("douze", "nombre"), (" millimètres", "prose")],
        "axe X devant deux-points",
    )
    assert_eq(
        genres("à 410 mètres"),
        [("a ", "prose"), ("400", "nombre"), ("dix", "nombre"), (" mètres", "prose")],
        "410 dans une phrase",
    )
    assert_eq(
        genres("Avion détecté : AFR410."),
        [("Avion détecté : ", "prose"),
         ("alfa", "lettre"), ("foxtrotte", "lettre"), ("roméo", "lettre"),
         ("400", "nombre"), ("dix", "nombre")],
        "indicatif",
    )
    assert_eq(
        genres("F 4 A B C tiret 9"),
        [("foxtrotte", "lettre"), ("quatre", "nombre"), ("alfa", "lettre"),
         ("bravo", "lettre"), ("charli", "lettre"), ("neuf", "nombre")],
        "epellation deja espacee",
    )
    assert_eq(genres("le TGV de 6612"), [
        ("le TGV de ", "prose"),
        ("6000", "nombre"), ("600", "nombre"),
        ("douze", "nombre"),
    ], "TGV reste un mot, le numero se colle")
    assert_eq(genres("Réception DMR terminée"), [("Réception ", "prose"), ("delta", "lettre"), ("maïk", "lettre"), ("roméo", "lettre"), (" terminée", "prose")], "DMR, sigle epele")
    assert_eq(genres("capteur A"), [("capteur ", "prose"), ("alfa", "lettre")], "lettre seule")
    assert_eq(genres("il a 3 avions"), [("il a ", "prose"), ("trois", "nombre"), (" avions", "prose")], "a minuscule")
    assert_eq(genres("quatre cent dix"), [
        ("quatre", "nombre"), ("cent", "nombre"), ("dix", "nombre")], "nombre deja en mots")
    assert_eq(genres("un avion"), [("un avion", "prose")], "un seul un")
    assert_eq(genres("sept mille sept cents"), [
        ("sept", "nombre"), ("mille", "nombre"), ("sept", "nombre"), ("cent", "nombre")], "cents")
    # phrase fixe ecrite en toutes lettres : on ne la hache pas
    assert_eq(
        genres("émet le code sept mille sept cents, urgence"),
        [("émet le code sept mille sept cents, urgence", "prose")],
        "phrase en mots laissee entiere",
    )
    assert_eq(genres("km2"), [("km2", "prose")], "unite")
    assert_eq(genres("Foxtrot quatre Alpha"), [
        ("foxtrotte", "lettre"), ("quatre", "nombre"), ("alfa", "lettre")], "suite radio")
    assert_eq(genres("21 heures"), [
        ("21 heures", "nombre")], "21 heures")
    assert_eq(genres("1 heure"), [("1 heure", "nombre")], "1 heure")
    assert_eq(genres("0 minute"), [("0 minute", "nombre")], "zero minute")
    assert_eq(genres("45 minutes"), [("45 minutes", "nombre")], "45 minutes")
    assert_eq(genres("1 novembre"), [
        ("premier", "nombre"), (" novembre", "prose")], "premier novembre")
    assert_eq(genres("21 novembre"), [
        ("vingt", "nombre"), ("et un", "nombre"), (" novembre", "prose")], "21 novembre")
    assert_eq(genres("31 notifications"), [
        ("trente", "nombre"), ("et une", "nombre"), (" notifications", "prose")], "notifications")
    assert_eq(genres("1 nouvelle entrée"), [
        ("une", "nombre"), (" nouvelle entrée", "prose")], "une entree")
    assert_eq(genres("81 minutes"), [
        ("quatre-vingts", "nombre"), ("une", "nombre"), (" minutes", "prose")], "81 minutes")
    assert_eq(genres("Il est 3 heures 1, dimanche"), [
        ("Il est ", "prose"), ("3 heures", "nombre"),
        ("une", "nombre"), (" dimanche", "prose")], "minute apres l'heure")
    assert "tiret" not in a.sans_separateurs("liaison - tiret / barre")
    assert "dix-sept" in a.sans_separateurs("dix-sept reste entier")


def test_flash_hors_mecanisme():
    assert a.annonce_courte("Il est 21 heures 5, dimanche 1 novembre.")
    assert a.annonce_courte("Altitude 410 mètres.")
    assert a.annonce_courte(
        "Station APRS F 4 A B C tiret 7, à 8 kilomètres au nord. Relais sur 145.")
    assert not a.annonce_courte(
        "Capteurs météo, 22 heures : 4 dehors, 12 degrés. Humidité 80 pour cent. "
        "Vent autour de 15 kilomètres-heure.")
    assert not a.annonce_courte(
        "Trafic de données, de 21 heures à 22 heures. Entre 21 heures et 22 heures, "
        "le scanner a capté sans le dire : 40 trames d'avions.")
    assert not a.annonce_courte(
        "Météo de Lyon. En ce moment, ciel couvert, 11 degrés. Vent du nord à 20 kilomètres heure.")
    assert not a.annonce_courte("x" * 400)
    assert_eq(genres("Nœud Meshtastic A B C, à 2 kilomètres."), [
        ("Nœud Meshtastic ", "prose"),
        ("alfa", "lettre"), ("bravo", "lettre"), ("charli", "lettre"),
        (", a ", "prose"), ("deux", "nombre"), (" kilomètres.", "prose"),
    ], "nœud pas November, à plat")
    dit = a.indicatifs_en_clair("Message de F4ABC-7, à 3 kilomètres.")
    if "foxtrotte" not in dit or "à" in dit.split("foxtrotte")[0]:
        raise SystemExit(f"indicatif en clair : {dit}")
    if not dit.startswith("Message de foxtrotte"):
        raise SystemExit(f"indicatif en clair : {dit}")


def test_vocabulaire_ferme():
    mots = set(a.vocabulaire())
    for h in range(24):
        if a.phrase_heure(h) not in mots:
            raise SystemExit(f"heure {h} absente du vocabulaire")
    for m in range(60):
        if a.phrase_minute(m) not in mots:
            raise SystemExit(f"minute {m} absente du vocabulaire")
    for n in list(range(0, 120)) + [200, 410, 999, 1000, 1800, 2500, 6612, 10000, 10**6, 2 * 10**9]:
        for mot in a.mots_entier(n):
            if mot not in mots:
                raise SystemExit(f"{n} utilise « {mot} », absent du vocabulaire")
    for mot, _ in a.mots_indicatif("F-ZA9/B"):
        if mot not in mots:
            raise SystemExit(f"indicatif utilise « {mot} », absent du vocabulaire")


if __name__ == "__main__":
    test_nombres()
    test_indicatifs()
    test_decoupe()
    test_flash_hors_mecanisme()
    test_vocabulaire_ferme()
    print("ok")
