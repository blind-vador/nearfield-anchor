-- Nearfield Bridge Service : enveloppe invisible (pas d'icone dans le Dock)
-- du service de fond. macOS refuse le disque reseau a un Python lance
-- directement par launchd ; lance par cette application, il herite de
-- l'autorisation "volumes reseau" donnee une fois a l'application.
on run
	set dossier to POSIX path of (path to home folder) & ".nearfield-bridge/"
	do shell script "/usr/bin/python3 " & quoted form of (dossier & "nearfield_bridge.py") & " service >> " & quoted form of (dossier & "service.log") & " 2>&1"
end run
