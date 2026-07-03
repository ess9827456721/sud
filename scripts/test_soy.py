"""
Manual verification script for the SOY scraper (Block 2 acceptance).

Runs scrape_case on a real sudrf.ru card and pretty-prints the result.

Expected (case 2-280/2026, kovd--mrm.sudrf.ru):
  success=True
  case_info: case_number 2-280/2026, judge Толстова Тамара Васильевна,
             category path, receipt_date 2026-06-25,
             UID 51RS0018-01-2026-000467-24
  events:   «Судебное заседание» on 2026-07-21 10:00, is_future=1
  participants: ответчик АО "Ковдорский ГОК", inn=5104002234,
                representative is NOT '510401001' (that's КПП)
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

TEST_URL = (
    "https://kovd--mrm.sudrf.ru/modules.php?name=sud_delo&srv_num=1"
    "&name_op=case&case_id=207018412"
    "&case_uid=11430ee0-9ed4-4d1b-a370-6e7fb8409223&delo_id=1540005"
)


def main() -> int:
    from court_tracker.scraper.soy_scraper import SOYScraper

    url = sys.argv[1] if len(sys.argv) > 1 else TEST_URL
    print(f"Scraping: {url}\n")

    result = SOYScraper(headless=True).scrape_case(url)

    print("=" * 70)
    print(f"success   : {result['success']}")
    print(f"status    : {result['status']}")
    print(f"error_msg : {result['error_msg']}")

    print("\n--- case_info ---")
    print(json.dumps(result.get("case_info") or {}, ensure_ascii=False, indent=2))

    print(f"\n--- participants ({len(result['participants'])}) ---")
    for p in result["participants"]:
        print(f"  [{p['role']}] {p['name']}  ИНН={p['inn']}  "
              f"представитель={p['representative']}")

    print(f"\n--- events ({len(result['events'])}) ---")
    for ev in result["events"]:
        future = " [БУДУЩЕЕ]" if ev.get("is_future") else ""
        t = f" {ev['event_time']}" if ev.get("event_time") else ""
        print(f"  {ev['event_date']}{t}  ({ev['event_type']}){future}")
        print(f"      {ev['description'][:100]}")

    # Quick assertions against the known card
    if url == TEST_URL and result["success"]:
        ci = result.get("case_info") or {}
        checks = [
            ("case_number 2-280/2026", ci.get("case_number") == "2-280/2026"),
            ("uid 51RS0018-01-2026-000467-24",
             ci.get("uid") == "51RS0018-01-2026-000467-24"),
            ("receipt_date 2026-06-25", ci.get("receipt_date") == "2026-06-25"),
            ("hearing 2026-07-21 present",
             any(e["event_date"] == "2026-07-21" for e in result["events"])),
            ("no representative == КПП 510401001",
             all(p.get("representative") != "510401001"
                 for p in result["participants"])),
            ("ответчик with inn 5104002234",
             any(p.get("inn") == "5104002234" for p in result["participants"])),
        ]
        print("\n--- acceptance checks ---")
        ok = True
        for label, passed in checks:
            print(f"  {'PASS' if passed else 'FAIL'}  {label}")
            ok = ok and passed
        return 0 if ok else 1

    return 0 if result["success"] else 1


if __name__ == "__main__":
    sys.exit(main())
