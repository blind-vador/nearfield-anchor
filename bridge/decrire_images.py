"""Travail compagnon : decrire les images d'un modele imprimable.

Pipeline :
  1. VLM MLX en anglais (meilleure vision / moins de coquilles)
  2. Deduplication des boucles de phrases
  3. Traduction EN→FR par Argos Translate (paquet neural en→fr)

Le Pi depose les JPEG/PNG (image_0.jpg...). Sortie : description.json
"""
import json
import os
import re
import sys
import tempfile

MODEL = os.environ.get("NEARFIELD_VLM_MODEL", "mlx-community/Qwen2-VL-2B-Instruct-4bit")
VLM_PYTHON = os.path.expanduser("~/.nearfield-bridge/vlm-venv/bin/python")
MAX_IMAGES = 4
MAX_TOKENS = 160
MAX_SIDE = 1024
REPETITION_PENALTY = 1.35


def _progress(folder, percent, etape):
    """Etat lisible par le Pi pendant le calcul (progression.json)."""
    import time as _time
    path = os.path.join(folder, "progression.json")
    payload = {"percent": int(percent), "etape": etape, "t": _time.time()}
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False)
    os.replace(tmp, path)


PROMPT = (
    "You help a blind person understand photos of a 3D-printable model. "
    "Reply in plain English only, no lists, no markdown, no bullet points. "
    "Describe what the image shows: object shape, visible parts, single piece "
    "or assembly, orientation, details useful to decide whether to print. "
    "Two or three sentences. Do not invent precise dimensions. "
    "Trust the author title and description for what the object is supposed to be "
    "(e.g. if they say cat figurine, describe the cat you see — do not invent "
    "unrelated creatures). If the photo is unclear, say so briefly."
)


def _list_images(folder, params):
    names = []
    for name in params.get("images") or []:
        path = os.path.join(folder, name)
        if os.path.isfile(path):
            names.append(name)
    if not names:
        for name in sorted(os.listdir(folder)):
            low = name.lower()
            if low.startswith("image_") and low.endswith((".jpg", ".jpeg", ".png", ".webp")):
                names.append(name)
    return names[:MAX_IMAGES]


def _prepare(path, tmpdir):
    from PIL import Image
    with Image.open(path) as im:
        im = im.convert("RGB")
        w, h = im.size
        if max(w, h) > MAX_SIDE:
            im.thumbnail((MAX_SIDE, MAX_SIDE))
        out = os.path.join(tmpdir, os.path.basename(path) + ".jpg")
        im.save(out, "JPEG", quality=85)
        return out


def _clean_raw(text):
    text = (text or "").replace("*", "").replace("#", "")
    text = re.sub(r"^[\-\u2022]\s*", "", text.strip())
    return " ".join(text.split()).strip()


def _dedupe_repeats(text):
    """Coupe les boucles du genre « A. A. A. » ou un meme bout repete 3+ fois."""
    text = _clean_raw(text)
    if not text:
        return ""
    # Phrases separees
    parts = [p.strip() for p in re.split(r"(?<=[.!?])\s+", text) if p.strip()]
    out = []
    for p in parts:
        # Boucle interne : (token group) x3+
        p = re.sub(r"\b(.{8,80}?)(?:\s+\1){2,}\b", r"\1", p, flags=re.I)
        p = " ".join(p.split())
        if out and p.lower() == out[-1].lower():
            continue
        # Quasi-doublon (meme debut long)
        if out and len(p) > 40 and p.lower()[:40] == out[-1].lower()[:40]:
            continue
        out.append(p)
    # Si tout le texte est la meme phrase colle 4 fois sans ponctuation
    joined = " ".join(out)
    m = re.match(r"^(.{20,}?)(?:\s+\1){2,}$", joined, flags=re.I)
    if m:
        joined = m.group(1).strip()
    return joined.strip()


def _translate_en_fr(text):
    """Traduction neural Argos EN→FR ; repli = texte anglais si paquet absent."""
    text = (text or "").strip()
    if not text:
        return ""
    try:
        import argostranslate.translate
        fr = argostranslate.translate.translate(text, "en", "fr")
        return _dedupe_repeats(fr) or text
    except Exception as exc:
        print(f"[decrire_images] traduction : {exc!r}", file=sys.stderr)
        return text


def _describe(paths, titre, contexte, folder=None):
    from mlx_vlm import load, generate
    from mlx_vlm.prompt_utils import apply_chat_template

    model, processor = load(MODEL)
    parts_en = []
    with tempfile.TemporaryDirectory(prefix="nf-vlm-") as tmp:
        prepared = [_prepare(p, tmp) for p in paths]
        for i, path in enumerate(prepared, 1):
            if folder:
                pct = 10 + int(80 * (i - 1) / max(len(prepared), 1))
                _progress(folder, pct, f"image_{i}_sur_{len(prepared)}")
            user = (
                f"{PROMPT}\n\n"
                f"Model title: {titre or '(unknown)'}\n"
                f"Author description: {(contexte or '')[:500] or '(none)'}\n"
                f"This is photo {i} of {len(prepared)}."
            )
            prompt = apply_chat_template(
                processor, config=model.config, prompt=user, num_images=1
            )
            out = generate(
                model, processor, prompt, image=path,
                max_tokens=MAX_TOKENS, temperature=0.2,
                repetition_penalty=REPETITION_PENALTY,
                repetition_context_size=32,
                verbose=False,
            )
            text = _dedupe_repeats(getattr(out, "text", None) or str(out))
            if text:
                parts_en.append(text)
    if not parts_en:
        return "", [], []
    if folder:
        _progress(folder, 90, "traduction")
    parts_fr = [_translate_en_fr(p) for p in parts_en]
    if len(parts_fr) == 1:
        return parts_fr[0], parts_fr, parts_en
    body_fr = " ".join(f"Photo {i}. {p}" for i, p in enumerate(parts_fr, 1))
    return body_fr.strip(), parts_fr, parts_en


def process(folder, params):
    names = _list_images(folder, params)
    if not names:
        return "echec", [], "aucune image à décrire"
    paths = [os.path.join(folder, n) for n in names]
    titre = (params.get("titre") or "").strip()
    contexte = (params.get("description") or params.get("contexte") or "").strip()
    try:
        _progress(folder, 5, "prise_en_charge")
        texte, par_image_fr, par_image_en = _describe(paths, titre, contexte, folder=folder)
        _progress(folder, 100, "termine")
    except Exception as exc:
        try:
            _progress(folder, -1, "echec")
        except Exception:
            pass
        return "echec", [], f"VLM : {exc}"
    if not texte:
        return "echec", [], "description vide"
    payload = {
        "texte": texte,
        "par_image": par_image_fr,
        "par_image_en": par_image_en,
        "langue": "fr",
        "via": "vlm-en+argos-fr",
        "modele": MODEL,
        "source": params.get("source"),
        "piece_id": params.get("piece_id"),
        "images": names,
    }
    out_name = "description.json"
    with open(os.path.join(folder, out_name), "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=1)
    short = texte if len(texte) <= 180 else texte[:177].rsplit(" ", 1)[0] + "…"
    return "fait", [out_name], short


def available():
    return os.path.isfile(VLM_PYTHON) and os.access(VLM_PYTHON, os.X_OK)


def process_via_venv(folder, params):
    if not available():
        return "echec", [], "venv VLM absent (~/.nearfield-bridge/vlm-venv)"
    import subprocess
    env = os.environ.copy()
    env["NEARFIELD_VLM_MODEL"] = MODEL
    params_path = os.path.join(folder, "_params_decrire.json")
    with open(params_path, "w", encoding="utf-8") as f:
        json.dump(params, f, ensure_ascii=False)
    try:
        proc = subprocess.run(
            [VLM_PYTHON, os.path.abspath(__file__), folder, params_path],
            capture_output=True, text=True, timeout=900, env=env,
        )
    finally:
        try:
            os.remove(params_path)
        except OSError:
            pass
    lines = [ln for ln in (proc.stdout or "").strip().splitlines() if ln.strip().startswith("{")]
    if lines:
        try:
            data = json.loads(lines[-1])
            return data["etat"], data["sorties"], data["message"]
        except (ValueError, KeyError) as exc:
            return "echec", [], f"réponse VLM illisible : {exc}"
    err_lines = [ln for ln in (proc.stderr or "").strip().splitlines()
                 if ln.strip() and "Fetching " not in ln and "it/s" not in ln]
    tip = err_lines[-1] if err_lines else "échec VLM"
    return "echec", [], str(tip)[:240]


def main():
    folder = sys.argv[1]
    if len(sys.argv) >= 3:
        with open(sys.argv[2], encoding="utf-8") as f:
            params = json.load(f)
    else:
        for name in ("travail.pris.json", "travail.json"):
            path = os.path.join(folder, name)
            if os.path.isfile(path):
                params = json.load(open(path, encoding="utf-8")).get("params") or {}
                break
        else:
            params = {}
    etat, sorties, message = process(folder, params)
    print(json.dumps({"etat": etat, "sorties": sorties, "message": message}, ensure_ascii=False))
    sys.exit(0 if etat == "fait" else 1)


if __name__ == "__main__":
    main()
