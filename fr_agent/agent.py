# Copyright (c) 2025, Sandip Lahiri. All rights reserved.
"""
Remote Regulation Data Agent (A2A server)

- Exposes an ADK LlmAgent via A2A using to_a2a()
- Provides a single tool: fetch_recent_regulations(agency, days_back)
- Uses the Federal Register API to fetch HHS and CMS rules
- Can be consumed by other agents using RemoteA2aAgent

Run:
    python agent.py

This will start an A2A agent on port 8002 with an agent card at:
    http://localhost:8001/.well-known/agent-card.json
"""

import os
import datetime as dt
from typing import List
from dotenv import load_dotenv
from pathlib import Path
import requests

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

from google.adk.agents import LlmAgent
from google.adk.a2a.utils.agent_to_a2a import to_a2a
from google.adk.models.google_llm import Gemini
from google.genai import types

# Federal Register endpoint for documents
FR_URL = "https://www.federalregister.gov/api/v1/documents.json"

# Agency slugs as used by FederalRegister.gov
AGENCY_SLUGS = {
    "HHS": "health-and-human-services-department",
    "CMS": "centers-for-medicare-medicaid-services",
    "BOTH": None,  # special handling: query both
}


def _build_params(slugs: List[str], since_date_iso: str) -> dict:
    """
    Build query parameters for Federal Register API.
    We request Final Rules and Proposed Rules.
    """
    params = {
        "per_page": "40",
        "order": "newest",
        "conditions[type][]": ["RULE", "PRORULE"],
        "conditions[publication_date][gte]": since_date_iso,
        "fields[]": [
            "title",
            "document_number",
            "publication_date",
            "type",
            "html_url",
        ],
    }

    # Add one or more agency filters
    for slug in slugs:
        params.setdefault("conditions[agencies][]", [])
        params["conditions[agencies][]"].append(slug)

    return params


def fetch_recent_regulations(agency: str = "BOTH", days_back: int = 30) -> str:
    """
    Fetch recent HHS and CMS regulations from the Federal Register API.

    Args:
        agency: "HHS", "CMS", or "BOTH"
        days_back: Look back this many days from today

    Returns:
        A human-readable summary string listing the most recent rules.
        The function never fabricates data; it only reports what is returned
        by the Federal Register API.
    """
    agency_normalized = (agency or "BOTH").upper()
    if agency_normalized not in AGENCY_SLUGS:
        agency_normalized = "BOTH"

    if days_back <= 0:
        days_back = 1

    since_date = dt.date.today() - dt.timedelta(days=days_back)
    since_iso = since_date.isoformat()

    if agency_normalized == "BOTH":
        slugs = [
            AGENCY_SLUGS["health-and-human-services-department"]
            if "health-and-human-services-department" in AGENCY_SLUGS
            else "health-and-human-services-department"
        ]
    # Build the slugs
    if agency_normalized == "HHS":
        slugs = [AGENCY_SLUGS["HHS"]]
    elif agency_normalized == "CMS":
        slugs = [AGENCY_SLUGS["CMS"]]
    else:
        slugs = [AGENCY_SLUGS["HHS"], AGENCY_SLUGS["CMS"]]

    params = _build_params(slugs, since_iso)

    try:
        resp = requests.get(FR_URL, params=params, timeout=30)
        resp.raise_for_status()
    except Exception as e:
        return f"Error calling Federal Register API: {e}"

    data = resp.json()
    results = data.get("results", []) or []

    if not results:
        return (
            f"No {agency_normalized} regulations found in the last "
            f"{days_back} days (since {since_iso})."
        )

    lines = [
        f"Recent {agency_normalized} regulations in the last {days_back} days "
        f"(since {since_iso}):",
        "",
    ]

    for doc in results[:10]:
        title = doc.get("title", "").strip()
        num = doc.get("document_number", "")
        pub_date = doc.get("publication_date", "")
        doc_type = doc.get("type", "")
        url = doc.get("html_url", "")

        lines.append(
            f"- [{pub_date}] ({doc_type}) {num}\n"
            f"  Title: {title}\n"
            f"  URL: {url}"
        )

    if len(results) > 10:
        lines.append("")
        lines.append(f"...and {len(results) - 10} more document(s).")

    return "\n".join(lines)


# Configure the Gemini model with retry behavior
retry_config = types.HttpRetryOptions(
    attempts=5,
    exp_base=2,
    initial_delay=1,
    http_status_codes=[429, 500, 503, 504],
)

# Define the remote ADK agent that uses the tool
reg_data_agent = LlmAgent(
    model=Gemini(model="gemini-2.5-flash-lite", retry_options=retry_config),
    name="reg_data_agent",
    description=(
        "Remote agent that fetches recent HHS and CMS regulations from the "
        "Federal Register and explains them."
    ),
    instruction=(
        "You are a regulatory data agent. "
        "When users ask about recent or current regulations from HHS or CMS, "
        "use the fetch_recent_regulations tool to retrieve real data from the "
        "Federal Register. Do not invent regulations. "
        "After calling the tool, briefly summarize the key points in plain "
        "language for compliance and engineering audiences."
    ),
    tools=[fetch_recent_regulations],
)

# Wrap the agent into an A2A-compatible ASGI app
fr_a2a_app = to_a2a(reg_data_agent, port=8001)


if __name__ == "__main__":
    import uvicorn

    print("Starting Reg Data A2A agent on http://0.0.0.0:8001 ...")
    print("Agent card:", "http://localhost:8001/.well-known/agent-card.json")
    uvicorn.run(fr_a2a_app, host="0.0.0.0", port=8001)
