"""
Crude Average Analysis Agent — powered by Google Gemini
=========================================================
Reads docs/crude_weekly_avg.json (produced by crude_weekly_pipeline.py) and
uses Gemini 2.0 Flash to generate a plain-text energy market analysis.

Usage:
    python crude_avg_agent.py              # pretty output with banner
    python crude_avg_agent.py -p           # stdout only (pipe-friendly)
    python crude_avg_agent.py -p --input docs/crude_weekly_avg.json

Environment:
    GEMINI_API_KEY   — Google AI Studio API key (required)

Run the pipeline first if the output file does not exist:
    python crude_weekly_pipeline.py --mode both
"""
import argparse
import json
import os
import sys
from pathlib import Path

_DEFAULT_INPUT = Path(__file__).parent / "docs" / "crude_weekly_avg.json"

# MT → barrels conversion for crude oil (approximate, varies by grade)
_MT_TO_BARRELS = 7.33


def _load_data(input_path: Path) -> dict:
    if not input_path.exists():
        print(
            f"Error: {input_path} not found.\n"
            "Run the pipeline first: python crude_weekly_pipeline.py --mode both",
            file=sys.stderr,
        )
        sys.exit(1)
    return json.loads(input_path.read_text(encoding="utf-8"))


def _build_prompt(data: dict) -> str:
    rec = data.get("recommended_weekly_avg_mt")
    note = data.get("recommendation_note", "")
    generated_at = data.get("generated_at", "unknown")

    official = data.get("official", {})
    live = data.get("live", {})

    lines = [
        "You are an energy market analyst specialising in Indian crude oil imports.",
        "Provide a concise, factual plain-text briefing (4–6 paragraphs) based on the data below.",
        "Do not use markdown formatting. Write in a professional but readable tone.",
        "",
        f"Data generated at: {generated_at}",
        "",
    ]

    # Recommended figure
    if rec:
        rec_bbl = int(rec * _MT_TO_BARRELS)
        lines += [
            f"RECOMMENDED WEEKLY CRUDE AVERAGE: {rec:,} MT ({rec_bbl:,} barrels)",
            f"Basis: {note}",
            "",
        ]
    else:
        lines += ["RECOMMENDED WEEKLY CRUDE AVERAGE: Insufficient data", ""]

    # Official data
    if official and not official.get("error"):
        monthly = official.get("pol_crude_tonnes_monthly", 0)
        weekly_off = official.get("weekly_avg_mt", 0)
        share = official.get("pol_crude_share_pct", 0)
        month = official.get("report_month", "")
        total = official.get("total_cargo_tonnes", 0)
        lines += [
            f"OFFICIAL DATA ({month}):",
            f"  POL & Crude Products monthly total: {monthly:,} MT",
            f"  Derived weekly average: {weekly_off:,} MT",
            f"  Share of total port cargo: {share}%",
            f"  Total cargo across all major ports: {total:,} MT",
            "",
        ]
    elif official.get("error"):
        lines += [f"OFFICIAL DATA: Unavailable ({official['error']})", ""]

    # Live data
    if live and live.get("weekly_avg_mt") is not None:
        wmt = live.get("weekly_avg_mt", 0)
        vessels = live.get("vessel_count", 0)
        date_range = live.get("date_range", "")
        sparse = live.get("data_sparse", True)
        per_port = live.get("per_port", {})

        lines += [
            f"LIVE SCRAPER DATA ({date_range}):",
            f"  7-day crude vessel tonnage: {wmt:,} MT across {vessels} unique vessel(s)",
            f"  Data quality: {'SPARSE (fewer than 3 vessels with quantity data)' if sparse else 'ADEQUATE'}",
        ]
        if per_port:
            lines.append("  Per-port breakdown:")
            for port, mt in sorted(per_port.items(), key=lambda x: -x[1]):
                lines.append(f"    {port}: {mt:,} MT")
        lines.append("")

    lines += [
        "Please provide your analysis covering:",
        "1. What the weekly crude import figure implies for India's daily crude intake",
        "   (India refining capacity is ~5.5 million barrels per day; context is helpful)",
        "2. How the official (monthly aggregate) and live (vessel-level) figures compare, if both present",
        "3. Key caveats about data completeness (only major ports, only vessels with reported quantities)",
        "4. A brief outlook comment on India's crude import demand",
    ]

    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Gemini-powered analysis of India crude weekly import averages."
    )
    parser.add_argument(
        "-p", "--print",
        dest="print_only",
        action="store_true",
        help="Print analysis to stdout only (pipe-friendly, no banner).",
    )
    parser.add_argument(
        "--input",
        default=str(_DEFAULT_INPUT),
        help=f"Path to crude_weekly_avg.json (default: {_DEFAULT_INPUT})",
    )
    parser.add_argument(
        "--output",
        default=None,
        help="Write analysis text to this file (in addition to stdout).",
    )
    args = parser.parse_args()

    # Load data
    data = _load_data(Path(args.input))

    # Configure Gemini
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print(
            "Error: GEMINI_API_KEY environment variable not set.\n"
            "Get a key at https://aistudio.google.com/app/apikey",
            file=sys.stderr,
        )
        sys.exit(1)

    try:
        from google import genai
        from google.genai import types as genai_types
    except ImportError:
        print(
            "Error: google-genai package not installed.\n"
            "Run: pip install google-genai>=1.0.0",
            file=sys.stderr,
        )
        sys.exit(1)

    client = genai.Client(api_key=api_key)
    prompt = _build_prompt(data)

    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        analysis = response.text
    except Exception as exc:
        print(f"Error: Gemini API call failed: {exc}", file=sys.stderr)
        sys.exit(1)

    if args.output:
        Path(args.output).write_text(analysis, encoding="utf-8")

    if args.print_only:
        print(analysis)
    else:
        banner = "=" * 70
        rec = data.get("recommended_weekly_avg_mt")
        rec_str = f"{rec:,} MT" if rec else "N/A"
        print(banner)
        print(f"  INDIA CRUDE IMPORT ANALYSIS — Weekly Avg: {rec_str}")
        print(f"  Generated: {data.get('generated_at', 'unknown')}")
        print(banner)
        print()
        print(analysis)
        print()
        print(banner)


if __name__ == "__main__":
    main()
