"""Sukuria nauja rakta ir ipsaugo JI I .env TIESIOGIAI.

Raktas NIEKADA neispausdinamas - nei i ekrana, nei i atmintine,
nei i schaukli. Vienintelis kelias: i .env faila. To del ekrano
nuotrauka po sia komanda yra SAUGI, nes joje bus tik adresas,
o adresas nera slaptazodis.

Naudojimas:
    python generate_wallet.py             # sukuria ir iraso
    python generate_wallet.py --force     # perraso esanti rakta
"""

import argparse
import re
import sys
from pathlib import Path

import base58
from solders.keypair import Keypair

from bot.envfile import read_env, update_env

ENV_PATH = Path(__file__).resolve().parent / ".env"


def main() -> int:
    parser = argparse.ArgumentParser(description="Sukuria rakta tiesiogiai i .env")
    parser.add_argument(
        "--force",
        action="store_true",
        help="perraso jau esanti rakta ( kitaip komanda atsargiai atsisako )",
    )
    args = parser.parse_args()

    if not ENV_PATH.exists():
        print("NERASTAS .env failas.")
        print("Pirma: copy .env.example .env")
        return 1

    existing = (read_env(ENV_PATH).get("PRIVATE_KEY_BASE58") or "").strip()
    if existing and not args.force:
        print(f".env jau turi rakta ({len(existing)} simboliu).")
        jo = str(ENV_PATH).lower()
        if any(f[:6] in existing for f in ("5XsmNJ", "F9vTM8", "oAGTFX")):
            print("Atkreipk demesi: tai VIENAS IS PAZEISTU rakto, kurie buvo")
            print("parodyti ekrane ar pokalbyje. Jis neturetu likti cia.")
        print()
        print("Neperrasiu jo be --force - kitaip galima netycia")
        print("prarasti pinigine, kurioje yra pinigu.")
        print("Jei ta pinigine tuscia arba jos raktas matytas ekrane:")
        print("    python generate_wallet.py --force")
        return 1

    one_drive = "onedrive" in str(ENV_PATH).lower()
    if one_drive:
        print("=" * 62)
        print("PERSPEJIMAS. Projektas yra OneDrive aplanke:")
        print(f"  {ENV_PATH.parent}")
        print()
        print("Raktas butu sinchronizuotas i Microsoft serverius")
        print("KARTU SU FAILO VERSIJU ISTORIJA. Net jei rakta veliau")
        print("isvalysi, jis liks senose versijose.")
        print()
        print("Rekomenduojama pirma:")
        print("  1. Uzbaik OneDrive sinchronizavima siai programai")
        print("  2. Perkelk visa crypto-bot aplanka i C:\\crypto-bot")
        print("  3. Tada paleisk sia komanda is naujo")
        print("=" * 62)
        if existing and not args.force:
            print()
            print("NUTRAUKTA. Perkelk aplanka ir paleisk is naujo.")
            print("(arba priversk su --force, jei tikrai zinai, ka darai)")
            return 1
        if existing:
            print()
            print("Perrasiau su --force, taciau rizika lieka.")
        else:
            print()
            print("Einu toliau, nes .env dar neturi rakto ir nera ko prarasti.")

    keypair = Keypair()
    secret = base58.b58encode(bytes(keypair)).decode("ascii")

    update_env(ENV_PATH, {"PRIVATE_KEY_BASE58": secret})

    # Patikriname TA, KAS TIK IRASEM - bet niekada nerodydami rakto.
    saved = (read_env(ENV_PATH).get("PRIVATE_KEY_BASE58") or "").strip()
    raw = base58.b58decode(saved)
    loaded = Keypair.from_bytes(raw)
    dry_run = (read_env(ENV_PATH).get("DRY_RUN") or "").strip()

    print()
    print("RAKTAS SUKURTAS IR IRASYTAS I .env")
    print(f"  .env kelias       : {ENV_PATH}")
    print(f"  rakto ilgis       : {len(saved)} simboliu")
    print(f"  base58 dekodavimas: OK, {len(raw)} baitu")
    print(f"  Keypair.from_bytes: OK")
    print()
    print(f"  PINIGINES ADRESAS : {loaded.pubkey()}")
    print()
    print("Adresas NERA slaptazodis - screenshot su juo saugus.")
    print("Raktas niekur kitur nebuvo parodytas. Nekopijuok jo i pokalbi.")
    print()

    if dry_run != "false":
        print(f"DRY_RUN = {dry_run or '(nenurodyta)'} - botas NEPREKIAUS tikrais")
        print("pinigais. Kai busi pasiruoses, Dashboard'e spausk")
        print("'Go Live' ir irasyk GO LIVE, arba ranka pakeisk .env.")
    else:
        print("DRY_RUN = false - botas prekiaus TIKRAIS pinigais.")
        print("Pirmiausia patikrink: python main.py check")

    print()
    print("Tolesni zingsniai:")
    print("  python main.py check     # patikrina viska be sandoriu")
    print("  python main.py run       # paleidzia bota")
    print("  python run_tests.py      # 59 testai")
    return 0


if __name__ == "__main__":
    sys.exit(main())
