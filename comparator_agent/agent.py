# Copyright (c) 2025, Sandip Lahiri. All rights reserved.
"""
Comparator A2A Agent

Compares HHS/CMS regulations in the current period (last N days)
with the previous equal-length period, using the Federal Register API.

Run:
    python agent.py

This will start an A2A agent on port 8002 with an agent card at:
    http://localhost:8002/.well-known/agent-card.json
"""

import datetime as dt
from typing import List, Dict

import requests
import uvicorn

from google.adk.agents import LlmAgent
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from google.adk.models.google_llm import Gemini
from google.genai import types
import requests
from dotenv import load_dotenv
from pathlib import Path
import os

PROJECT_ROOT = Path(__file__).resolve().parents[1]
env_path = PROJECT_ROOT / ".env"

# NOTE: override=True so .env value wins
load_dotenv(dotenv_path=env_path, override=True)

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

if os.environ["GOOGLE_API_KEY"] is None:
    
    print(
        "\n❌ERROR: GOOGLE_API_KEY environment variable is not set. "
        "The agent will not function properly without it.\n"
    )
    exit(1)

FR_URL = "https://www.federalregister.gov/api/v1/documents.json"

AGENCY_SLUGS = {
    "HHS": "health-and-human-services-department",
    "CMS": "centers-for-medicare-medicaid-services",
}

retry_config = types.HttpRetryOptions(
    attempts=5,
    exp_base=2,
    initial_delay=1,
    http_status_codes=[429, 500, 503, 504],
)


def _build_params_for_range(
    slugs: List[str],
    start_date_iso: str,
    end_date_iso: str,
) -> Dict:
    """
    Build query parameters for a date range [start_date, end_date].
    """
    params = {
        "per_page": "1000",
        "order": "newest",
        "conditions[type][]": ["RULE", "PRORULE"],
        "conditions[publication_date][gte]": start_date_iso,
        "conditions[publication_date][lte]": end_date_iso,
        "fields[]": [
            "title",
            "document_number",
            "publication_date",
            "type",
            "html_url",
        ],
    }
    for slug in slugs:
        params.setdefault("conditions[agencies][]", [])
        params["conditions[agencies][]"].append(slug)
    return params


def _fetch_docs_for_range(
    slugs: List[str],
    start_date: dt.date,
    end_date: dt.date,
) -> List[Dict]:
    params = _build_params_for_range(
        slugs,
        start_date.isoformat(),
        end_date.isoformat(),
    )
    resp = requests.get(FR_URL, params=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    return data.get("results", []) or []


def compare_regulation_changes(
    agency: str = "BOTH",
    days_back: int = 30,
) -> str:
    """
    Compare HHS/CMS regulations in the last `days_back` days
    against the previous `days_back` days.

    Returns a human-readable summary string.
    """
    agency_normalized = (agency or "BOTH").upper()
    if agency_normalized not in ("HHS", "CMS", "BOTH"):
        agency_normalized = "BOTH"

    if days_back <= 0:
        days_back = 1

    today = dt.date.today()
    current_start = today - dt.timedelta(days=days_back)
    previous_end = current_start - dt.timedelta(days=1)
    previous_start = previous_end - dt.timedelta(days=days_back)

    if agency_normalized == "HHS":
        slugs = [AGENCY_SLUGS["HHS"]]
    elif agency_normalized == "CMS":
        slugs = [AGENCY_SLUGS["CMS"]]
    else:
        slugs = [AGENCY_SLUGS["HHS"], AGENCY_SLUGS["CMS"]]

    try:
        current_docs = _fetch_docs_for_range(slugs, current_start, today)
        previous_docs = _fetch_docs_for_range(slugs, previous_start, previous_end)
    except Exception as e:
        return f"Error calling Federal Register API in comparator agent: {e}"

    current_nums = {d.get("document_number") for d in current_docs}
    previous_nums = {d.get("document_number") for d in previous_docs}

    new_nums = current_nums - previous_nums

    # Count by type
    def _count_types(docs: List[Dict]) -> Dict[str, int]:
        counts = {"RULE": 0, "PRORULE": 0, "OTHER": 0}
        for d in docs:
            t = (d.get("type") or "").upper()
            if t in ("RULE", "PRORULE"):
                counts[t] += 1
            else:
                counts["OTHER"] += 1
        return counts

    current_counts = _count_types(current_docs)
    previous_counts = _count_types(previous_docs)

    # Gather details for new docs
    new_docs = [d for d in current_docs if d.get("document_number") in new_nums]

    lines = [
        f"Comparator analysis for {agency_normalized} regulations.",
        "",
        f"Current period:   {current_start.isoformat()} to {today.isoformat()} (inclusive)",
        f"Previous period:  {previous_start.isoformat()} to {previous_end.isoformat()} (inclusive)",
        "",
        f"Current period:  {len(current_docs)} document(s) "
        f"(Final rules: {current_counts['RULE']}, Proposed rules: {current_counts['PRORULE']}, Other: {current_counts['OTHER']})",
        f"Previous period: {len(previous_docs)} document(s) "
        f"(Final rules: {previous_counts['RULE']}, Proposed rules: {previous_counts['PRORULE']}, Other: {previous_counts['OTHER']})",
        "",
        f"Net change in total docs: {len(current_docs) - len(previous_docs)}",
        f"New document(s) in current period that did not appear in the previous period: {len(new_docs)}",
        "",
    ]

    if new_docs:
        lines.append("Newly introduced document(s) in the current period:")
        for d in new_docs[:10]:
            title = (d.get("title") or "").strip()
            num = d.get("document_number", "")
            pub_date = d.get("publication_date", "")
            doc_type = d.get("type", "")
            url = d.get("html_url", "")
            lines.append(
                f"- [{pub_date}] ({doc_type}) {num}\n"
                f"  Title: {title}\n"
                f"  URL: {url}"
            )
        if len(new_docs) > 10:
            lines.append("")
            lines.append(f"...and {len(new_docs) - 10} more new document(s) in the current period.")
    else:
        lines.append("No documents in the current period are new relative to the previous period.")

    return "\n".join(lines)


# LlmAgent for the comparator
comparator_agent = LlmAgent(
    model=Gemini(model="gemini-2.5-flash-lite", retry_options=retry_config),
    name="reg_change_comparator_agent",
    description=(
        "Agent that compares recent HHS and CMS regulations in the last N days "
        "with the previous N days using the Federal Register API."
    ),
    instruction=(
        "You expose a single tool compare_regulation_changes that compares the number and type "
        "of HHS/CMS regulations in the last N days with the previous N-day window. "
        "When asked for comparison or 'changes over time', ALWAYS call that tool and "
        "return its output. Do not fabricate regulations."
    ),
    tools=[compare_regulation_changes],
)

# Wrap as A2A app on port 8002
comparator_a2a_app = to_a2a(comparator_agent, port=8002)


if __name__ == "__main__":
    print("Starting Comparator A2A agent on http://0.0.0.0:8002 ...")
    print("Agent card: http://localhost:8002/.well-known/agent-card.json")
    uvicorn.run(comparator_a2a_app, host="0.0.0.0", port=8002)
