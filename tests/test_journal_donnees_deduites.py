#!/usr/bin/env python3
"""Messages ou donnees (2026-11-08) : un vrai message reste au journal messages,
une detection, une identite, un etat ou une signalisation part au journal donnees.
Lancer : python3 tests/test_journal_donnees_deduites.py"""
import os
import sys

RACINE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(RACINE, "lib"))
import journaux  # noqa: E402

J = "dimanche_8_novembre_2026_10h00m00s.mp3"
for etiquette, journal in (("donnees_dmr", "donnees"), ("donnees_tetra", "donnees"), ("donnees_DMR_resume", "donnees"),
                           ("donnees_relais", "donnees"), ("donnees_decodees", "donnees"), ("donnees_ais_227006760", "donnees"),
                           ("message_tetra_30000", "messages"), ("message_pager", "messages"),
                           ("message_numerique_DMR", "messages"), ("message_avion_ACARS_DL0175", "messages")):
    assert journaux.category_of(f"{etiquette}_{J}") == journal, etiquette

for texte, attendu in (
        ("MPT1327: voie de contrôle, variante française, appel attribué au canal 83, 49-15 vers 49-7307", "donnees_decodees"),
        ("FLEETSYNC: flotte 100, poste 1000 vers 1001, identification", "donnees_decodees"),
        ("FMS: 1234 5678", "donnees_decodees"),
        ("ZVEI1: 12345", "donnees_decodees"),
        ("POCSAG1200: Address:  123456  Function: 0", "donnees_decodees"),
        ("POCSAG1200: Address:  123456  Function: 3  Alpha:   Merci de rappeler", "message_donnees"),
        ("POCSAG512: Address:   99  Function: 0  Numeric:  0469", "message_donnees"),
        ("EAS: ZCZC-WXR-TOR", "message_donnees"),
        ("MORSE_CW: CQ CQ DE F4XYZ", "message_donnees"),
        ("FT8: CQ F4XYZ JN25", "message_donnees"),
        ("INCONNU: quelque chose", "message_donnees")):
    assert journaux.etiquette_decodage(texte) == attendu, (texte, journaux.etiquette_decodage(texte))
print("ok  test_journal_donnees_deduites")
