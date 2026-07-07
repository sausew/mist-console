"""
mist_image_win.py: generate an image from a text prompt and save it to disk.

Windows port of the harness mist-image CLI, built into the MIST Console exe so
no Python install is needed. Invoke through the exe:

  "MIST Console.exe" image "a foggy harbor at dawn"
  "MIST Console.exe" image "a red fox in snow" -o fox.png --size 1024 --seed 42

Pixels are made on a cloud GPU and downloaded as a file; nothing heavy runs
locally. Default output: %USERPROFILE%\\Pictures\\MIST Gallery (the Console
serves that folder inline, so generated images render right in the chat).

Backends
  pollinations  free, needs a free key (https://auth.pollinations.ai).
                The setup wizard stores it in the app .env.
  cloudflare    Cloudflare Workers AI (FLUX.1-schnell; FLUX.2 for --ref).
                Needs CF_ACCOUNT_ID + CF_API_TOKEN.
"""
import argparse
import base64
import io
import json
import os
import random
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

import config_win

RETRIES = 3
TIMEOUT = 120  # FLUX can take a while; be patient before failing
FLUX2_REF_MODEL = "@cf/black-forest-labs/flux-2-klein-9b"


def log(msg):
    print(f"[mist-image] {msg}", file=sys.stderr)


def pick_backend(requested):
    """auto -> cloudflare if its keys exist, else pollinations."""
    if requested != "auto":
        return requested
    if os.environ.get("CF_ACCOUNT_ID") and os.environ.get("CF_API_TOKEN"):
        return "cloudflare"
    return "pollinations"


def slugify(text, maxlen=50):
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return (s[:maxlen].rstrip("-")) or "image"


def ext_for(content_type):
    ct = (content_type or "").lower()
    if "png" in ct:
        return ".png"
    if "webp" in ct:
        return ".webp"
    return ".jpg"


def resolve_out(out, prompt, content_type, out_dir):
    if out:
        if os.path.dirname(out):
            return os.path.abspath(os.path.expanduser(out))
        return os.path.join(out_dir, out)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    name = f"{slugify(prompt)}-{stamp}{ext_for(content_type)}"
    return os.path.join(out_dir, name)


def fetch(req):
    """GET/POST with retries. Returns (body_bytes, content_type)."""
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                return resp.read(), resp.headers.get("Content-Type", "")
        except Exception as e:  # noqa: BLE001 - surface anything as a retry
            last = e
            wait = 2 ** attempt
            log(f"attempt {attempt}/{RETRIES} failed ({e}); retrying in {wait}s")
            if attempt < RETRIES:
                time.sleep(wait)
    raise SystemExit(f"[mist-image] gave up after {RETRIES} attempts: {last}")


def gen_pollinations(prompt, width, height, seed, model):
    base = "https://gen.pollinations.ai/image/" + urllib.parse.quote(prompt)
    params = {"width": width, "height": height, "model": model, "nologo": "true"}
    if seed is not None:
        params["seed"] = seed
    key = os.environ.get("POLLINATIONS_API_KEY")
    headers = {"User-Agent": "mist-image/1.0"}
    if key:
        headers["Authorization"] = f"Bearer {key}"
    else:
        raise SystemExit(
            "[mist-image] Pollinations requires a free API key.\n"
            "  Get one at https://auth.pollinations.ai and save it in the MIST Console\n"
            "  settings (or add POLLINATIONS_API_KEY=... to %s).\n"
            "  Or use Cloudflare: --backend cloudflare (CF_ACCOUNT_ID + CF_API_TOKEN)."
            % config_win.ENV_PATH
        )
    url = base + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=headers)
    body, ct = fetch(req)
    if not ct.lower().startswith("image/"):
        snippet = body[:300].decode("utf-8", "replace")
        raise SystemExit(f"[mist-image] pollinations returned non-image ({ct}): {snippet}")
    return body, ct


def gen_cloudflare(prompt, width, height, seed, model):
    acct = os.environ.get("CF_ACCOUNT_ID")
    token = os.environ.get("CF_API_TOKEN")
    if not (acct and token):
        raise SystemExit(
            "[mist-image] cloudflare backend needs CF_ACCOUNT_ID and CF_API_TOKEN in the environment")
    cf_model = model if "/" in model else "@cf/black-forest-labs/flux-1-schnell"
    url = f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/{cf_model}"
    payload = {"prompt": prompt, "width": width, "height": height}
    if seed is not None:
        payload["seed"] = seed
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
        method="POST")
    body, ct = fetch(req)
    if ct.lower().startswith("image/"):
        return body, ct
    try:
        data = json.loads(body)
    except Exception:
        raise SystemExit(f"[mist-image] cloudflare returned unparseable response: {body[:300]!r}")
    if not data.get("success", True) and data.get("errors"):
        raise SystemExit(f"[mist-image] cloudflare error: {data['errors']}")
    b64 = data.get("result", {}).get("image")
    if not b64:
        raise SystemExit(f"[mist-image] cloudflare response missing image: {body[:300]!r}")
    return base64.b64decode(b64), "image/png"


def prep_reference(path, maxdim=480):
    """Return PNG bytes of a reference image guaranteed under 512x512.

    FLUX.2 rejects references 512px or larger on either side. The macOS
    original shells out to sips; here Pillow (bundled in the exe) resizes."""
    src = os.path.abspath(os.path.expanduser(path))
    if not os.path.exists(src):
        raise SystemExit(f"[mist-image] reference image not found: {path}")
    try:
        from PIL import Image
    except ImportError:
        raise SystemExit("[mist-image] reference mode needs Pillow (bundled in the exe build)")
    try:
        img = Image.open(src)
        img.load()
    except Exception as e:
        raise SystemExit(f"[mist-image] could not read reference {path}: {e}")
    w, h = img.size
    if max(w, h) > maxdim:
        scale = maxdim / max(w, h)
        img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))), Image.LANCZOS)
    buf = io.BytesIO()
    img.convert("RGBA").save(buf, format="PNG")
    return buf.getvalue()


def _multipart(fields, files):
    """Build a multipart/form-data body. files: {name: (filename, bytes)}."""
    boundary = "----mist" + uuid.uuid4().hex
    crlf = "\r\n"
    body = b""
    for k, v in fields.items():
        body += ("--" + boundary + crlf).encode()
        body += f'Content-Disposition: form-data; name="{k}"{crlf}{crlf}{v}{crlf}'.encode()
    for k, (fn, data) in files.items():
        body += ("--" + boundary + crlf).encode()
        body += (f'Content-Disposition: form-data; name="{k}"; filename="{fn}"{crlf}'
                 f'Content-Type: image/png{crlf}{crlf}').encode()
        body += data + crlf.encode()
    body += ("--" + boundary + "--" + crlf).encode()
    return body, "multipart/form-data; boundary=" + boundary


def gen_cloudflare_flux2(prompt, width, height, seed, model, refs, steps):
    """Identity-preserving generation: condition on up to 4 reference images,
    addressed in the prompt as 'image 0'..'image 3'. Retries fresh seeds when
    the safety filter false-flags one."""
    acct = os.environ.get("CF_ACCOUNT_ID")
    token = os.environ.get("CF_API_TOKEN")
    if not (acct and token):
        raise SystemExit(
            "[mist-image] reference generation (--ref) needs CF_ACCOUNT_ID and CF_API_TOKEN")
    cf_model = model if model.startswith("@cf/") else FLUX2_REF_MODEL
    url = f"https://api.cloudflare.com/client/v4/accounts/{acct}/ai/run/{cf_model}"
    files = {}
    for i, p in enumerate(refs[:4]):
        files[f"input_image_{i}"] = (f"ref{i}.png", prep_reference(p))
    attempts = 5
    for attempt in range(1, attempts + 1):
        s = seed if (attempt == 1 and seed is not None) else random.randint(1, 2 ** 31 - 1)
        fields = {"prompt": prompt, "width": str(width), "height": str(height),
                  "steps": str(steps or 18), "seed": str(s)}
        body, ct = _multipart(fields, files)
        req = urllib.request.Request(
            url, data=body,
            headers={"Authorization": f"Bearer {token}", "Content-Type": ct}, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
                raw, rct = resp.read(), resp.headers.get("Content-Type", "")
        except urllib.error.HTTPError as e:
            msg = e.read().decode("utf-8", "replace")
            if e.code == 400 and "flagged" in msg.lower():
                log(f"seed {s} flagged by the safety filter; retrying with a new seed ({attempt}/{attempts})")
                continue
            raise SystemExit(f"[mist-image] cloudflare flux-2 error ({e.code}): {msg[:300]}")
        except Exception as e:  # noqa: BLE001
            log(f"attempt {attempt}/{attempts} failed ({e}); retrying in 2s")
            time.sleep(2)
            continue
        if rct.lower().startswith("image/"):
            return raw, rct
        try:
            data = json.loads(raw)
        except Exception:
            raise SystemExit(f"[mist-image] flux-2 returned unparseable response: {raw[:300]!r}")
        b64 = data.get("result", {}).get("image") or data.get("image")
        if not b64:
            raise SystemExit(f"[mist-image] flux-2 response missing image: {raw[:300]!r}")
        img = base64.b64decode(b64)
        sniff = "image/jpeg" if img[:2] == b"\xff\xd8" else "image/png"
        return img, sniff
    raise SystemExit(
        "[mist-image] flux-2 safety filter flagged every seed; try a more modest prompt or a different reference")


def main(argv=None):
    ap = argparse.ArgumentParser(prog="MIST Console.exe image",
                                 description="Generate an image from a prompt.")
    ap.add_argument("prompt", nargs="+", help="the image description")
    ap.add_argument("-o", "--out", help="output filename or path (bare name lands in --dir)")
    ap.add_argument("--dir", default=None,
                    help="output directory (default %%MIST_IMAGE_DIR%% or Pictures\\MIST Gallery)")
    ap.add_argument("--size", type=int, help="square dimension shortcut, e.g. 1024")
    ap.add_argument("--width", type=int, default=1024)
    ap.add_argument("--height", type=int, default=1024)
    ap.add_argument("--seed", type=int, help="reproducible seed")
    ap.add_argument("--model", default="flux", help="model name (backend-specific)")
    ap.add_argument("--backend", default="auto", choices=["auto", "pollinations", "cloudflare"])
    ap.add_argument("--ref", action="append", metavar="PATH",
                    help="reference image(s) for identity-preserving generation (FLUX.2, up to 4). "
                         "Forces the cloudflare backend; address them as 'image 0' etc. in the prompt.")
    ap.add_argument("--steps", type=int, help="diffusion steps for --ref mode (default 18)")
    ap.add_argument("--open", action="store_true", help="open the image when done")
    args = ap.parse_args(argv)

    config_win.load_env_file()
    backend = pick_backend(args.backend)
    prompt = " ".join(args.prompt).strip()
    width = args.size or args.width
    height = args.size or args.height
    out_dir = os.path.expanduser(args.dir or os.environ.get("MIST_IMAGE_DIR")
                                 or config_win.GALLERY_DIR)
    os.makedirs(out_dir, exist_ok=True)

    refs = args.ref or []
    seedlog = f", seed {args.seed}" if args.seed is not None else ""
    if refs:
        log(f"cloudflare flux-2 (ref x{len(refs)}): \"{prompt}\" ({width}x{height}{seedlog})")
        body, ct = gen_cloudflare_flux2(prompt, width, height, args.seed, args.model, refs, args.steps)
    else:
        log(f"{backend}: \"{prompt}\" ({width}x{height}{seedlog})")
        if backend == "cloudflare":
            body, ct = gen_cloudflare(prompt, width, height, args.seed, args.model)
        else:
            body, ct = gen_pollinations(prompt, width, height, args.seed, args.model)

    path = resolve_out(args.out, prompt, ct, out_dir)
    with open(path, "wb") as f:
        f.write(body)
    log(f"saved {len(body):,} bytes -> {path}")
    if args.open:
        try:
            os.startfile(path)  # noqa: S606 - Windows-native open
        except Exception:
            pass
    # stdout = the path only, so callers can capture it cleanly
    print(path)


if __name__ == "__main__":
    main()
