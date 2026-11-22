#!/usr/bin/env python3
"""
Key manager (CLI)

  python manage_keys.py add --owner Rahul --days 30 --limit 50
  python manage_keys.py add --key DARK-VIP --limit 100 --regions IND,BD
  python manage_keys.py list
  python manage_keys.py disable DARK-VIP
  python manage_keys.py enable DARK-VIP
  python manage_keys.py remove DARK-VIP

Changes keys.json mein save hote hain. Vercel par deploy karte ho to
keys.json update karke dobara deploy (git push) karo.
"""
import argparse

import keys_manager as km


def main():
    p = argparse.ArgumentParser(description="API key manager")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="Nayi key banao")
    a.add_argument("--key", help="Custom key (nahi diya to random banegi)")
    a.add_argument("--owner", default="")
    a.add_argument("--days", type=int, help="Kitne din valid (nahi diya = kabhi expire nahi)")
    a.add_argument("--limit", type=int, help="Daily limit (nahi diya = unlimited)")
    a.add_argument("--regions", help="Comma separated, e.g. IND,BD (nahi diya = sab)")

    sub.add_parser("list", help="Saari keys dikhao")
    for name in ("remove", "enable", "disable"):
        s = sub.add_parser(name)
        s.add_argument("key")

    args = p.parse_args()

    if args.cmd == "add":
        regions = [r.strip() for r in args.regions.split(",")] if args.regions else None
        key, cfg = km.add_key(args.key, args.owner, args.days, args.limit, regions)
        print(f"Key created: {key}")
        print(cfg)
    elif args.cmd == "list":
        keys = km.list_keys()
        if not keys:
            print("Koi key nahi hai.")
        for k, c in keys.items():
            print(
                f"{k:28} owner={c.get('owner')!r} active={c.get('active', True)} "
                f"expires={c.get('expires')} limit={c.get('daily_limit')} "
                f"used_today={c.get('used_today')} regions={c.get('regions')}"
            )
    elif args.cmd == "remove":
        print("Removed" if km.remove_key(args.key) else "Key nahi mili")
    elif args.cmd in ("enable", "disable"):
        ok = km.set_active(args.key, args.cmd == "enable")
        print("Done" if ok else "Key nahi mili")


if __name__ == "__main__":
    main()
