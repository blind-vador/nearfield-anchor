# nearfield-pont — protocole de confiance v2

Crate Rust qui implémente la v2 du pont entre l'ordonnateur et les machines du parc :
clés d'identité, objets signés, appairage vocal, certificats de rôle, transport chiffré.
Il remplacera `bridge/pont_protocole.py` (v1).

Licence : **MIT** (voir `LICENCE`), par exception au reste du dépôt (GPL v3), pour que
d'autres logiciels puissent l'utiliser.

Spécification complète : `specs/protocole-v2.md` dans le dossier de conception Prismaforge.

## Modules

| Module | Rôle |
|---|---|
| `jcs` | Forme canonique JSON (RFC 8785, entiers seulement) : ce qui est signé |
| `cles` | Clés Ed25519 et P-256, coffres (fichier aujourd'hui, puces ensuite), identifiants de machine. Relit la `cle.pem` de la v1 |
| `objet` | Objets signés pour le NAS : vérification, empreintes des fichiers joints, noms sûrs, registre anti-rejeu de 7 jours |
| `appairage` | Engagement préalable et code vocal à 6 chiffres, dit en 3 paires |
| `certificat` | Certificats de 12 h (rôles, capacités, niveau), liste de révocation qui ne recule jamais |
| `migration` | Passage de la v1 à la v2 sans nouvel appairage : nouvelle identité signée par la nouvelle clé et contresignée par la clé v1 ; une ancienne clé ne migre qu'une fois |
| `messages` | Liste fermée des messages du canal, limites de taille |
| `transport` | Session Noise XX (`Noise_XX_25519_ChaChaPoly_SHA256`) authentifiée par certificat, port 47812. Une session TCP se sépare en une moitié qui envoie et une qui reçoit (`separer`), pour deux fils d'exécution |

## Tests

```
cargo test
```

40 tests : vecteurs fixes (`vecteurs.json`, recalculés indépendamment en Python),
et les attaques que la v2 doit repousser (charge modifiée, rejeu, objet trop vieux, autre
parc, faux ordonnateur, clé Noise différente du certificat, intermédiaire pendant
l'appairage, type de message inconnu, chemin `../`, liste de révocation plus ancienne…).

Pour ne pas remplir le NAS, compiler avec un dossier de sortie local :
`CARGO_TARGET_DIR=~/.cache/cible-pont cargo test`.

## Liaison Python (`python/`)

Module `nearfield_pont` (PyO3, ABI stable, Python 3.9 et plus) : c'est par lui que
nearfield utilisera la v2. Les objets du protocole sont des `dict` côté Python ; la
cryptographie et toutes les vérifications restent en Rust. Aucune clé privée n'est
lisible depuis Python : un `Coffre` ou une `CleTransport` signe ou chiffre, sans se
montrer. Les appels réseau relâchent le verrou global de Python.

```
cd python
python3 -m venv ~/.cache/venv-pont && ~/.cache/venv-pont/bin/pip install maturin pytest
CARGO_TARGET_DIR=~/.cache/cible-pont-py VIRTUAL_ENV=~/.cache/venv-pont ~/.cache/venv-pont/bin/maturin develop --release
~/.cache/venv-pont/bin/python -m pytest -p no:cacheprovider tests
```

Ce qu'il offre :

| Domaine | Python |
|---|---|
| Clés | `Coffre.charger_ou_creer(chemin, "p256")`, `.cle_publique()`, `.id`, `.signer_objet(charge)`, `identifiant(cle)`, `canonique(valeur)` |
| Objets signés | `verifier_objet(objet, cle, type, parc, registre)`, `RegistreNonces.charger/enregistrer`, `verifier_fichiers`, `empreinte_fichier`, `nom_sur` |
| Appairage | `Rejoignant(identite, transport).engagement / .recevoir(pres)`, `Accueil(engagement, identite, transport).presentation / .verifier(pres)`, `code_parle` |
| Certificats | `CleTransport()`, `emettre_certificat(...)`, `verifier_certificat(...)`, `Revocations().publier / .recevoir / .est_revoque` |
| Migration | `Coffre.ouvrir(cle_v1)`, `preparer_migration(ancien, nouveau, parc, nom, transport)`, `ancienne_annoncee`, `verifier_migration(objet, parc, cle_v1_de_la_fiche, registre)`, `RegistreMigrations` |
| Messages | `encoder_message(dict)`, `decoder_message(octets)` |
| Transport | `connecter(adresse, ...)` → `Session` ; `Ecouteur(adresse).accepter()` → `Entrante.repondre(...)` → `Session` ; `Session.envoyer / .recevoir / .fermer / .pair` |

Chaque refus lève une sous-classe de `ErreurPont` (`SignatureInvalide`, `Rejeu`,
`CertificatRefuse`, `MessageRefuse`…) dont le texte, en français, peut être dit.

Bon à savoir : avec Noise XX, la machine qui se connecte envoie son certificat dans le
dernier message de la poignée de main. Si l'ordonnateur le refuse, `connecter` réussit
quand même côté machine, mais la connexion est aussitôt fermée : le premier
`recevoir` rend `None`.

28 tests Python reprennent le scénario des tests Rust ; l'un fait une migration complète à partir de vraies clés générées par `bridge/pont_protocole.py` (v1), transmise sur un vrai canal v1, jusqu'à la session Noise v2 (il faut `cryptography` dans l'environnement de test).

## Branchement dans nearfield (pilote, à côté de la v1)

- Côté Pi : `lib/pont_v2.py`, appelé par `lib/compagnon_serveur.py`.
- Côté Mac : `PiloteV2` et `Link._v2` dans `bridge/nearfield_bridge.py`.

Quand une machine se connecte en v1 et annonce `"v2": true`, la migration se fait toute
seule sur le canal v1 : d'abord le Pi, puis la machine, puis le certificat. La machine
ouvre ensuite une session Noise sur le port 47812 et y envoie ses battements. **Le trafic
réel (travaux, voix, TETRA, numérique) reste sur la v1** : la liste fermée des messages v2
ne le couvre pas encore.

**Tout est inactif tant que le module `nearfield_pont` n'est pas installé** pour le Python
qui fait tourner le service. Sans lui, la v1 tourne seule, inchangée.

Pour activer le pilote :

1. Sur le Pi, installer le module pour le Python des services (`/usr/bin/python3`).
2. Rouvrir le pare-feu avec `lot0/durcir_pi.sh`, étape 3, qui ouvre maintenant aussi le
   port 47812 au réseau local.
3. Sur le Mac, installer le module pour `/usr/bin/python3` (celui de l'agent Nearfield
   Bridge), puis recopier `bridge/nearfield_bridge.py` dans `~/.nearfield-bridge/`.
4. Redémarrer la veille du Pi, puis l'agent du Mac.

État local de la v2 : `<NFA_HOME>/compagnon/v2/` sur le Pi (`identite.pem`,
`migrations.json`, `nonces.json`, `serie.json`), `~/.nearfield-bridge/v2/` sur le Mac
(`identite.pem`, `ponts/<pi>.json` avec la clé v2 du Pi épinglée).

Essai de bout en bout : `tests/test_pont_v2_migration.py`. Il fait tourner le vrai serveur
du Pi et la vraie liaison du Mac dans un dossier temporaire, et vérifie cinq choses :
- la v1 continue de fonctionner ;
- la migration se fait dans les deux sens ;
- les battements arrivent par la session Noise ;
- une reconnexion ne casse rien, et une clé v2 remplacée est refusée, d'un côté comme de
  l'autre ;
- sans le module, rien ne change.

## Reste à faire

- Étendre la liste fermée des messages v2 au trafic réel (travaux, tickets, voix en
  morceaux, TETRA, numérique), avec un schéma pour chacun, puis basculer et couper la v1.

- Coffres matériels : Secure Enclave (macOS), TPM 2.0 (Linux, Windows).
- Enveloppe chiffrée pour les objets sensibles.
- Brancher la v2 dans le pont nearfield (`lib/compagnon.py`, `bridge/nearfield_bridge.py`).
