"""Probe the two candidate build-detection sources and print what they return.

Run locally (needs internet; stdlib only):

    python scripts/check_builds.py
    python scripts/check_builds.py --product wow_classic_beta --region us

Purpose: confirm real response shapes before we design the BuildSource layer.
Nothing here writes to data/. Paste the output back so the parsers can be
written against what the servers actually return.
"""

import argparse
import json
import urllib.request

TACT_URL = "http://{region}.patch.battle.net:1119/{product}/versions"
WAGO_BUILDS_URL = "https://wago.tools/api/builds"
TIMEOUT = 20


def http_get(url: str) -> str:
    req = urllib.request.Request(url, headers={"User-Agent": "wow-forever-calculator/0.1"})
    with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
        return resp.read().decode("utf-8", errors="replace")


def parse_bpsv(text: str) -> tuple[list[str], list[dict], int | None]:
    """Parse Blizzard's pipe-separated-values format.

    Header line: Name!TYPE:length|Name!TYPE:length|...
    Comment line: '## seqn = 12345' (sequence number of the manifest).
    """
    columns: list[str] = []
    rows: list[dict] = []
    seqn = None
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.startswith("##"):
            if "seqn" in line and "=" in line:
                try:
                    seqn = int(line.split("=", 1)[1].strip())
                except ValueError:
                    pass
            continue
        parts = line.split("|")
        if not columns:
            columns = [p.split("!", 1)[0] for p in parts]
            continue
        rows.append(dict(zip(columns, parts)))
    return columns, rows, seqn


def probe_tact(product: str, region: str) -> None:
    print(f"\n=== Blizzard TACT: {product} ({region}) ===")
    url = TACT_URL.format(region=region, product=product)
    print("GET", url)
    try:
        text = http_get(url)
    except Exception as exc:  # noqa: BLE001 - diagnostic script
        print("FAILED:", exc)
        return
    columns, rows, seqn = parse_bpsv(text)
    print("columns:", columns)
    print("seqn:", seqn)
    for row in rows:
        print(
            f"  region={row.get('Region')} build={row.get('BuildId')} "
            f"version={row.get('VersionsName')}"
        )
    if not rows:
        print("raw response (first 600 chars):")
        print(text[:600])


def probe_wago(product: str) -> None:
    print(f"\n=== Wago.Tools /api/builds (filter: {product}) ===")
    print("GET", WAGO_BUILDS_URL)
    try:
        data = json.loads(http_get(WAGO_BUILDS_URL))
    except Exception as exc:  # noqa: BLE001
        print("FAILED:", exc)
        return

    # Shape is not confirmed: accept a flat list, or a dict keyed by product.
    if isinstance(data, dict):
        print("top-level: dict, keys (first 15):", list(data.keys())[:15])
        flat = data.get(product, [])
        if not isinstance(flat, list):
            flat = []
    else:
        print("top-level: list, length:", len(data))
        flat = data

    if flat:
        print("example entry keys:", sorted(flat[0].keys()) if isinstance(flat[0], dict) else type(flat[0]))
        print("example entry:", json.dumps(flat[0], indent=2)[:500])

    matches = [
        b for b in flat
        if isinstance(b, dict) and product in json.dumps(b).lower()
    ]
    print(f"entries mentioning '{product}': {len(matches)}")
    for b in matches[:5]:
        print(" ", json.dumps(b)[:300])


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--product", default="wow_classic_beta")
    ap.add_argument("--region", default="us")
    args = ap.parse_args()
    probe_tact(args.product, args.region)
    probe_wago(args.product)


if __name__ == "__main__":
    main()
