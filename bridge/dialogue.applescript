-- Nearfield Bridge : gestion des ponts entre cette machine et le Pi.
-- Dialogues standard de macOS, lus par VoiceOver. Tout le travail est fait
-- par nearfield_bridge.py (installe dans ~/.nearfield-bridge).

property titre : "Nearfield Bridge"

on commande(args)
	set script_ to quoted form of (POSIX path of (path to home folder) & ".nearfield-bridge/nearfield_bridge.py")
	try
		return do shell script "/usr/bin/python3 " & script_ & " " & args
	on error message_
		return "ERREUR" & tab & message_
	end try
end commande

on champs(texte)
	set AppleScript's text item delimiters to tab
	set morceaux to text items of texte
	set AppleScript's text item delimiters to ""
	return morceaux
end champs

on montrer(texte, titre_)
	display dialog texte with title titre_ buttons {"OK"} default button "OK"
end montrer

on resultat(texte, titre_)
	set morceaux to my champs(texte)
	if (count of morceaux) > 1 then
		my montrer(item 2 of morceaux, titre_)
	else
		my montrer(texte, titre_)
	end if
	return item 1 of morceaux is "OK"
end resultat

on appairer()
	try
		display dialog "Créer un pont avec un Pi." & return & return & "Sur la console du Pi, dis « appairage ». Ensuite clique sur Continuer : j'attends le Pi jusqu'à trois minutes, en silence." with title "Appairage" buttons {"Annuler", "Continuer"} default button "Continuer" cancel button "Annuler"
	on error
		return
	end try
	set reponse to my commande("appairage-attendre")
	if item 1 of my champs(reponse) is not "OK" then
		my resultat(reponse, "Appairage")
		return
	end if
	-- Dialogue du code ouvert par le module : il se ferme tout seul si le
	-- code est valide sur le Pi ; valider ici fait suivre le Pi.
	my resultat(my commande("appairage-code"), "Appairage")
end appairer

on supprimer()
	set liste to my commande("ponts")
	if liste is "" then
		my montrer("Aucun pont à supprimer.", "Supprimer un pont")
		return
	end if
	set ids to {}
	set descriptions to {}
	repeat with ligne in paragraphs of liste
		set morceaux to my champs(ligne as text)
		if (count of morceaux) > 1 then
			set end of ids to item 1 of morceaux
			set end of descriptions to item 2 of morceaux
		end if
	end repeat
	set choix to choose from list descriptions with title "Supprimer un pont" with prompt "Quel pont supprimer ?" OK button name "Supprimer" cancel button name "Annuler"
	if choix is false then return
	set choisi to item 1 of choix
	repeat with i from 1 to count of descriptions
		if item i of descriptions is choisi then set pi_ to item i of ids
	end repeat
	try
		display dialog "Supprimer le pont avec " & pi_ & " ? Les clés échangées seront oubliées des deux côtés ; il faudra refaire l'appairage." with title "Supprimer un pont" buttons {"Annuler", "Supprimer"} default button "Annuler" cancel button "Annuler"
	on error
		return
	end try
	my resultat(my commande("oublier " & quoted form of pi_), "Supprimer un pont")
end supprimer

on service()
	set etat to my commande("service-etat")
	if etat is "actif" then
		set choix to button returned of (display dialog "Le service de fond est actif : le Pi peut confier des calculs à cette machine. Le mettre en pause ?" with title "Service" buttons {"Laisser actif", "Mettre en pause"} default button "Laisser actif")
		if choix is "Mettre en pause" then my resultat(my commande("service-pause"), "Service")
	else
		set choix to button returned of (display dialog "Le service de fond est en pause. Le relancer ?" with title "Service" buttons {"Laisser en pause", "Relancer"} default button "Relancer")
		if choix is "Relancer" then my resultat(my commande("service-reprise"), "Service")
	end if
end service

on cle_humaine()
	try
		display dialog "Créer ta clé humaine dans la puce de ce Mac (Secure Enclave)." & return & return & "Elle sert à valider ce que le Pi te demande : lancer un ticket, enrôler une extension. À chaque validation, ce Mac te montre ce qui est demandé, puis macOS demande Touch ID ou ton mot de passe." & return & return & "Après Continuer, macOS va te demander Touch ID ou ton mot de passe une première fois, pour annoncer la clé au Pi." with title "Clé humaine" buttons {"Annuler", "Continuer"} default button "Continuer" cancel button "Annuler"
	on error
		return
	end try
	my resultat(my commande("humain"), "Clé humaine")
end cle_humaine

on run
	set actions to {"État des ponts et statistiques", "Appairer avec un Pi (créer un pont)", "Supprimer un pont", "Service de fond : pause ou reprise", "Clé humaine (Touch ID ou mot de passe)", "Quitter"}
	repeat
		set choix to choose from list actions with title titre with prompt "Que veux-tu faire ?" default items {item 1 of actions} OK button name "Choisir" cancel button name "Quitter"
		if choix is false then exit repeat
		set action_ to item 1 of choix
		if action_ is item 1 of actions then
			my montrer(my commande("etat"), "État des ponts")
		else if action_ is item 2 of actions then
			my appairer()
		else if action_ is item 3 of actions then
			my supprimer()
		else if action_ is item 4 of actions then
			my service()
		else if action_ is item 5 of actions then
			my cle_humaine()
		else
			exit repeat
		end if
	end repeat
end run
