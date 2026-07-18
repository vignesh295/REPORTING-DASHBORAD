"""
Quick offline check of the parser — no Google / email needed.

Usage:
    python selftest.py "path/to/NEW FINAL ORDER REPORTS (INDIA TO USA).xlsm"
"""
import sys

import parser as xparser


def main():
    if len(sys.argv) < 2:
        print("Usage: python selftest.py <path-to-xlsm>")
        sys.exit(1)
    path = sys.argv[1]
    result = xparser.parse_workbook(path)
    print(f"RED (OVERDUE):  {result['red_count']}")
    print(f"YELLOW (TODAY): {result['yellow_count']}")
    print("\nColumns:", result["headers"])
    print("\nFirst red row:   ", result["red"][0] if result["red"] else "(none)")
    print("First yellow row:", result["yellow"][0] if result["yellow"] else "(none)")


if __name__ == "__main__":
    main()
