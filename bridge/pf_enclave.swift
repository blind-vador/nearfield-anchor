// pf-enclave : clé d'identité P-256 gardée dans la Secure Enclave du Mac.
//
// Protocole fermé (crate nearfield-pont, module `puce`) :
//   pf-enclave disponible                       code 0 si la Secure Enclave répond
//   pf-enclave creer <poignee> [--presence]     crée la clé ; affiche la clé publique (base64, SEC1 65 octets)
//   pf-enclave publique <poignee>               affiche la clé publique
//   pf-enclave signer <poignee> [--raison <t>]  signe l'entrée standard (ECDSA P-256/SHA-256) ;
//                                               affiche la signature r||s (base64, 64 octets)
//
// La clé privée ne sort jamais de la puce : la « poignée » est un blob chiffré que seule
// la Secure Enclave de CE Mac sait utiliser (copié ailleurs, il ne vaut rien).
// Avec --presence (clé humaine), chaque signature exige Touch ID ou le mot de passe de
// la session ; macOS affiche la raison, lue par VoiceOver. Annuler = refus (code 3).
//
// Construit par bridge/installer.sh : swiftc -O pf_enclave.swift -o ~/.nearfield-bridge/bin/pf-enclave

import CryptoKit
import Foundation
import LocalAuthentication

func echec(_ code: Int32, _ message: String) -> Never {
    FileHandle.standardError.write((message + "\n").data(using: .utf8)!)
    exit(code)
}

let MAX_MESSAGE = 1 << 20
let args = CommandLine.arguments

func lirePoignee(_ chemin: String) -> Data {
    var info = stat()
    if lstat(chemin, &info) != 0 { echec(2, "poignee introuvable") }
    if (info.st_mode & S_IFMT) != S_IFREG { echec(2, "poignee : fichier ordinaire exige") }
    guard let d = FileManager.default.contents(atPath: chemin), d.count < 4096 else { echec(2, "poignee illisible") }
    return d
}

func ecrirePoignee(_ chemin: String, _ d: Data) {
    let fd = open(chemin, O_WRONLY | O_CREAT | O_EXCL | O_NOFOLLOW, 0o600)
    if fd < 0 { echec(2, "impossible de creer la poignee (existe deja ?)") }
    let ok = d.withUnsafeBytes { write(fd, $0.baseAddress, d.count) == d.count }
    fsync(fd)
    close(fd)
    if !ok { unlink(chemin); echec(2, "ecriture de la poignee") }
}

func afficher(_ d: Data) {
    print(d.base64EncodedString())
}

guard args.count >= 2 else { echec(64, "usage : pf-enclave disponible|creer|publique|signer ...") }

switch args[1] {
case "disponible":
    exit(SecureEnclave.isAvailable ? 0 : 1)

case "creer":
    guard args.count >= 3, SecureEnclave.isAvailable else { echec(64, "creer <poignee> [--presence]") }
    let presence = args.dropFirst(3).contains("--presence")
    var flags: SecAccessControlCreateFlags = [.privateKeyUsage]
    if presence { flags.insert(.userPresence) }
    // Clé de machine : utilisable écran verrouillé (le service tourne en continu) ;
    // clé humaine : seulement session ouverte, et jamais sans la personne.
    let protection = presence ? kSecAttrAccessibleWhenUnlockedThisDeviceOnly : kSecAttrAccessibleAfterFirstUnlockThisDeviceOnly
    var erreur: Unmanaged<CFError>?
    guard let ac = SecAccessControlCreateWithFlags(nil, protection, flags, &erreur) else {
        echec(2, "controle d'acces : \(String(describing: erreur?.takeRetainedValue()))")
    }
    do {
        let cle = try SecureEnclave.P256.Signing.PrivateKey(accessControl: ac)
        ecrirePoignee(args[2], cle.dataRepresentation)
        afficher(cle.publicKey.x963Representation)
    } catch {
        echec(2, "creation refusee par la Secure Enclave : \(error)")
    }

case "publique":
    guard args.count >= 3 else { echec(64, "publique <poignee>") }
    do {
        let cle = try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: lirePoignee(args[2]))
        afficher(cle.publicKey.x963Representation)
    } catch {
        echec(2, "poignee refusee par la Secure Enclave : \(error)")
    }

case "signer":
    guard args.count >= 3 else { echec(64, "signer <poignee> [--raison <texte>]") }
    var raison = "Signer avec la clé de ce Mac pour nearfield"
    if let i = args.firstIndex(of: "--raison"), i + 1 < args.count { raison = args[i + 1] }
    let message = FileHandle.standardInput.readDataToEndOfFile()
    if message.count > MAX_MESSAGE { echec(2, "message trop long") }
    let contexte = LAContext()
    contexte.localizedReason = raison
    contexte.localizedCancelTitle = "Refuser"
    do {
        let cle = try SecureEnclave.P256.Signing.PrivateKey(dataRepresentation: lirePoignee(args[2]),
                                                             authenticationContext: contexte)
        let sig = try cle.signature(for: message)
        afficher(sig.rawRepresentation)
    } catch let e as LAError where e.code == .userCancel || e.code == .appCancel || e.code == .systemCancel {
        echec(3, "refuse par l'utilisateur")
    } catch {
        let texte = "\(error)"
        if texte.contains("-128") || texte.lowercased().contains("cancel") { echec(3, "refuse par l'utilisateur") }
        echec(2, "signature refusee par la Secure Enclave : \(texte)")
    }

default:
    echec(64, "commande inconnue")
}
