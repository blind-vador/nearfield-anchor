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
| `enveloppe` | Chiffrement à destinataires multiples (spec §6.2) : clé de réception X25519 publiée chaque semaine (objet signé), X25519 éphémère + HKDF-SHA256 + ChaCha20-Poly1305, contenu en blocs de 64 Kio liés à l'entête ; octets ou fichiers |
| `appairage` | Engagement préalable et code vocal à 6 chiffres, dit en 3 paires |
| `certificat` | Certificats de 12 h (rôles, capacités, niveau), liste de révocation qui ne recule jamais |
| `migration` | Passage de la v1 à la v2 sans nouvel appairage : nouvelle identité signée par la nouvelle clé et contresignée par la clé v1 ; une ancienne clé ne migre qu'une fois |
| `messages` | Liste fermée des messages du canal, limites de taille |
| `transport` | Session Noise XX (`Noise_XX_25519_ChaChaPoly_SHA256`) authentifiée par certificat, port 47812. Une session TCP se sépare en une moitié qui envoie et une qui reçoit (`separer`), pour deux fils d'exécution |

## Tests

```
cargo test
```

50 tests : vecteurs fixes (`vecteurs.json`, recalculés indépendamment en Python),
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
| Enveloppe | `CleReception.charger_ou_creer(chemin)`, `.public`, `.empreinte`, `.publier(coffre, parc)`, `verifier_cle_reception(objet, identite, parc)`, `sceller([(id, cle)], de, contexte, octets)`, `ouvrir(env, moi, de, [cles])`, `sceller_fichier(...)`, `ouvrir_fichier(...)` |
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

31 tests Python reprennent le scénario des tests Rust ; l'un fait une migration complète à partir de vraies clés générées par `bridge/pont_protocole.py` (v1), transmise sur un vrai canal v1, jusqu'à la session Noise v2 (il faut `cryptography` dans l'environnement de test).

## Branchement dans nearfield

- Côté Pi : `lib/pont_v2.py`, appelé par `lib/compagnon_serveur.py`.
- Côté Mac : `PiloteV2` et `Link._v2` dans `bridge/nearfield_bridge.py`.
- Des deux côtés : `bridge/pont_v2_traduction.py`, qui traduit les messages v1 vers la liste
  fermée v2 (spec §8) et retour.

Quand une machine se connecte en v1 et annonce `"v2": true`, la migration se fait toute
seule sur le canal v1 : d'abord le Pi, puis la machine, puis le certificat. **La machine
ferme alors sa liaison v1 et passe en v2 seule** (`LiaisonV2`) : session Noise sur le port
47812, où passe tout le trafic réel (avis de travail, tickets, voix macOS et son en retour,
TETRA, numérique, battements avec de quoi la réveiller).

- Elle garde sa clé de transport et son certificat (`~/.nearfield-bridge/v2/session/<pi>/`) :
  après un redémarrage, elle se reconnecte directement en v2.
- Le certificat (12 h) se renouvelle à mi-vie dans la session, avec une preuve signée par la
  clé d'identité. Après une longue veille, un certificat expiré depuis moins de 30 jours
  ouvre une session de renouvellement seulement.
- Côté Pi, un `CanalV2` tient la place de la liaison v1 de la machine : le reste du code ne
  voit pas la différence. L'écoute v1 (47811) se ferme dès que toutes les machines sont en
  v2 seule.
- Un message que la liste fermée refuse n'est plus envoyé (refus journalisé une fois par
  type).
- Une nouvelle machine s'appaire directement en v2 (code de 6 chiffres, par le NAS) : console
  du Pi, commande « appairage » ; sur la machine, « Appairer avec un Pi ».

**Travaux signés** (spec §6.1) : le Pi dépose `travail.v2.json`, signé par sa clé v2, à côté
de chaque travail. Une machine migrée ne prend que ce qui est signé, exécute le travail
contenu dans l'objet (pas `travail.json`) après avoir vérifié l'empreinte de chaque entrée,
et rend `resultat.v2.json`, signé par sa clé v2 et lié à ce travail. Le Pi ne croit plus le
résultat v1 d'une machine migrée. Essai : `tests/test_travaux_v2.py` (12 scénarios, dont
les attaques : travail non signé, autre clé, entrée modifiée, rejeu, résultat recopié).

Une différence voulue avec la v1 : une demande TETRA ou numérique ne porte plus d'adresse
à contacter. La machine se connecte au flux radio du Pi de sa session.

**Tout est inactif tant que le module `nearfield_pont` n'est pas installé** pour le Python
qui fait tourner le service. Sans lui, la v1 tourne seule, inchangée.

Pour activer :

1. Sur le Pi, installer le module pour le Python des services :
   `/usr/bin/python3 -m pip install --user --break-system-packages <roue>`. Le service
   tourne sous l'utilisateur `vador`, dont le dossier `~/.local` est dans les chemins.
2. Le pare-feu posé par `install.sh` ouvre déjà le port 47812 au réseau local.
3. Sur le Mac, installer le module pour `/usr/bin/python3` (celui de l'agent Nearfield
   Bridge) avec `--user`, puis recopier `bridge/nearfield_bridge.py` et
   `bridge/pont_v2_traduction.py` dans `~/.nearfield-bridge/`.
4. Redémarrer la veille du Pi (`compagnon-watch`), puis l'agent du Mac
   (`launchctl kickstart -k gui/$(id -u)/local.nearfield.bridge`).

État local de la v2 : `<NFA_HOME>/compagnon/v2/` sur le Pi (`identite.pem`,
`migrations.json`, `nonces.json`, `serie.json`), `~/.nearfield-bridge/v2/` sur le Mac
(`identite.pem`, `ponts/<pi>.json` avec la clé v2 du Pi épinglée).

Essai de bout en bout : `tests/test_pont_v2_migration.py`. Il fait tourner le vrai serveur
du Pi et la vraie liaison du Mac dans un dossier temporaire, et vérifie douze scénarios (migration puis v2 seule, redémarrage sans v1, renouvellement à
mi-vie et après expiration, preuve exigée, clé du Pi changée, trafic réel, refus de la liste
fermée, appairage v2 et engagement non tenu). Le détail des scénarios d'avant :
- la v1 continue de fonctionner ;
- la migration se fait dans les deux sens ;
- les battements arrivent par la session Noise ;
- une reconnexion ne casse rien, et une clé v2 remplacée est refusée, d'un côté comme de
  l'autre ;
- sans le module, rien ne change ;
- avis de travail et tickets passent par la v2 ;
- la voix macOS fait l'aller-retour par la v2 ;
- TETRA et numérique (voix, banc, rendus, résultats) passent par la v2, et la machine vise
  l'hôte de sa session, pas l'adresse envoyée ;
- un message refusé par la liste fermée passe par la v1 en secours ;
- une session tombée se rouvre toute seule, avec un certificat neuf.

Suite complète de nearfield : `python3 tests/lancer_tous.py`, qui lance chaque fichier de
test dans son propre processus.

## Reste à faire

- Ne plus écrire `travail.json` ni `resultat.json`, ni signer les tickets en v1 (lot 2).
- Coffres matériels : Secure Enclave (macOS), TPM 2.0 (Linux, Windows).
- Brancher l'enveloppe là où il y a du sensible (lot 2 : tickets et code ; entrées des
  travaux, pour que la machine travaille sur sa copie déchiffrée) et publier les clés de
  réception chaque semaine.
