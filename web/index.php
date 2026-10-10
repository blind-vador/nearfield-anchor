<?php
// Point d entree unique de deepspace9 : liste les serveurs web du registre.
$reg = __DIR__ . "/registre.txt";
$sites = [];
if (is_readable($reg)) {
  foreach (file($reg, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) as $l) {
    $l = trim($l);
    if ($l === "" || $l[0] === "#") continue;
    $p = explode("::", $l, 2);
    if (count($p) === 2) $sites[] = [trim($p[0]), trim($p[1])];
  }
}
header("Content-Type: text/html; charset=utf-8");
?><!doctype html>
<html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>deepspace9 — serveurs web</title></head>
<body>
<h1>Serveurs web de deepspace9</h1>
<?php if (!$sites): ?>
<p>Aucun site enregistré. Remplis <code>registre.txt</code> (une ligne <code>nom::chemin</code> par site).</p>
<?php else: ?>
<nav aria-label="Sites hébergés"><ul>
<?php foreach ($sites as [$nom, $chemin]): ?>
<li><a href="<?= htmlspecialchars($chemin) ?>"><?= htmlspecialchars($nom) ?></a></li>
<?php endforeach; ?>
</ul></nav>
<?php endif; ?>
</body></html>
