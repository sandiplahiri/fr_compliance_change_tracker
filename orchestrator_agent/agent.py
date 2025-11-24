# Copyright (c) 2025, Sandip Lahiri. All rights reserved.
"""
Compliance Change Orchestrator

This agent:
- Uses a RemoteA2aAgent sub-agent that talks to a remote HHS/CMS regulations agent
- Summarizes recent changes for compliance, security, and engineering
- Optionally "sends" notifications via a simple email tool (currently just prints)

Run (example):
    python agent.py "Summarize new HHS and CMS rules from the last 30 days"
"""

import os
import sys
import asyncio
import uuid
import warnings
from dotenv import load_dotenv
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. SUPPRESS CLEANUP NOISE
# ---------------------------------------------------------------------------
warnings.filterwarnings("ignore", category=UserWarning)
warnings.simplefilter("ignore", UserWarning)
warnings.simplefilter("ignore")


PROJECT_ROOT = Path(__file__).resolve().parents[1]
env_path = PROJECT_ROOT / ".env"

# Note: override=True so .env value wins
load_dotenv(dotenv_path=env_path, override=True)

GOOGLE_API_KEY = os.getenv("GOOGLE_API_KEY")

if os.environ["GOOGLE_API_KEY"] is None:
    
    print(
        "\n❌ERROR: GOOGLE_API_KEY environment variable is not set. "
        "The agent will not function properly without it.\n"
    )
    exit(1)

from google.adk.agents import LlmAgent
from google.adk.agents.remote_a2a_agent import (
    RemoteA2aAgent,
    AGENT_CARD_WELL_KNOWN_PATH,
)
from google.adk.models.google_llm import Gemini
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.adk.tools import FunctionTool
from google.genai import types

import smtplib
from email.message import EmailMessage

# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------
DEFAULT_EMAIL_RECIPIENT = os.getenv("COMPLIANCE_EMAIL_TO", "compliance@acme.com")

def send_email_notification(summary: str, recipient: str = DEFAULT_EMAIL_RECIPIENT) -> str:
    """
    Send a real email with the given summary as the body.

    SMTP settings are taken from environment variables:
      SMTP_SERVER   (e.g. 'smtp.gmail.com')
      SMTP_PORT     (e.g. '587')
      SMTP_USER     (your SMTP username / email address)
      SMTP_PASSWORD (SMTP password or app password)

    Returns a short status string for logging.
    """
    GOOGLE_API_KEY = os.environ.get("GOOGLE_API_KEY")
    
    smtp_server = os.getenv("SMTP_SERVER", "smtp.gmail.com")
    smtp_port = int(os.getenv("SMTP_PORT", "587"))
    smtp_user = os.getenv("SMTP_USER")
    smtp_password = os.getenv("SMTP_PASSWORD")

    if not smtp_user or not smtp_password:
        # Fallback: log but don't crash the agent
        print("\n⚠️ Email not sent: SMTP_USER and/or SMTP_PASSWORD are not set.\n")
        print("Summary that would have been emailed:\n")
        print(summary)
        return "Email not sent: missing SMTP credentials."

    print(summary)

    # Build the email
    msg = EmailMessage()
    msg["Subject"] = "HHS/CMS Regulatory Change Summary"
    msg["From"] = smtp_user
    msg["To"] = recipient
    msg.set_content(summary)

    try:
        with smtplib.SMTP(smtp_server, smtp_port) as smtp:
            # STARTTLS for secure connection
            smtp.starttls()
            smtp.login(smtp_user, smtp_password)
            smtp.send_message(msg)

        print(f"\n✅ Email sent to {recipient}\n")
        return f"Email sent to {recipient}"
    except Exception as e:
        print(f"\n❌ Email send failed: {e}\n")
        return f"Email send failed: {e}"


# ---------------------------------------------------------------------------
# Builders
# ---------------------------------------------------------------------------

def build_remote_reg_agent(base_url: str = "http://localhost:8001") -> RemoteA2aAgent:
    agent_card_url = f"{base_url}{AGENT_CARD_WELL_KNOWN_PATH}"
    return RemoteA2aAgent(
        name="hhs_cms_reg_changes_agent",
        description=(
            "Remote agent that fetches recent regulations and rules from HHS and CMS "
            "via the Federal Register API, then returns them as text."
        ),
        agent_card=agent_card_url,
    )

def build_remote_comparator_agent(base_url: str = "http://127.0.0.1:8002") -> RemoteA2aAgent:
    """
    Remote comparator agent that compares regulations in the last N days
    vs the previous N days.
    """
    agent_card_url = f"{base_url}{AGENT_CARD_WELL_KNOWN_PATH}"
    return RemoteA2aAgent(
        name="reg_change_comparator_agent",
        description=(
            "Remote agent that compares recent HHS/CMS regulations in the last N days "
            "to the previous N days, reporting counts and newly introduced rules."
        ),
        agent_card=agent_card_url,
    )

def build_orchestrator_agent(
    reg_agent: RemoteA2aAgent,
    comparator_agent: RemoteA2aAgent,
) -> LlmAgent:
    """
    Build the top-level orchestrator agent that ADK will run.

    It uses:
    - reg_agent as a sub-agent for raw regulations
    - comparator_agent as a sub-agent for period-over-period analysis
    - email_tool as a normal tool
    """
    retry_config = types.HttpRetryOptions(
        attempts=5,
        exp_base=2,
        initial_delay=1,
        http_status_codes=[429, 500, 503, 504],
    )

    instruction = """
You are a Compliance Change Orchestrator for US healthcare organizations.

You have TWO remote sub-agents and ONE special routing tool:

Sub-agents:
1. hhs_cms_reg_changes_agent
   - Fetches real recent regulations from HHS and CMS via the Federal Register,
     given an agency ('HHS', 'CMS', or 'BOTH') and a days_back integer.

2. reg_change_comparator_agent
   - Compares the number and type of regulations in the last N days with the
     previous N-day window, for HHS/CMS/BOTH.

Tools:
1. transfer_to_agent
   - This is the ONLY tool you use to delegate work to a sub-agent.
   - To call a sub-agent, invoke transfer_to_agent with the appropriate
     target_agent_name (either 'hhs_cms_reg_changes_agent' or
     'reg_change_comparator_agent') and a natural-language input describing
     what you want that sub-agent to do.

2. send_email_notification
   - Sends a notification email with a text summary. The CLI wrapper already
     sends the final integrated answer by email, so you typically only use
     this tool if the user explicitly asks you to send an additional email.

When the user asks about recent HHS/CMS rules and a time window
(e.g., "Summarize new HHS and CMS rules from the last 15 days"):

1. FIRST, call transfer_to_agent with target_agent_name equal to
   'hhs_cms_reg_changes_agent', asking it to retrieve regulations for the
   specified agency and time window.

2. THEN, call transfer_to_agent with target_agent_name equal to
   'reg_change_comparator_agent', asking it to compare that same agency and
   time window with the previous N-day window.

3. Once you have BOTH sub-agent outputs, produce a single integrated answer:

   - Section 1: "Recent Rules"
     * Separate Final rules vs Proposed rules where possible.
     * Show key details: document number, publication date, title, URL.

   - Section 2: "Change vs Previous Period"
     * Use the comparator agent's results to describe how many rules there
       were in the current vs previous period, and highlight any newly
       introduced rules.

   - Section 3: "Why This Matters"
     * Compliance / privacy impact
     * Security / IT impact
     * Engineering / product impact

4. Be concise, structured, and avoid speculation. If there are no rules in
   the period, or no change vs the previous period, say so clearly.

You do NOT have tools named 'hhs_cms_reg_changes_agent' or
'reg_change_comparator_agent' directly. You must always call them via
the transfer_to_agent tool.
""".strip()
    
    email_tool = FunctionTool(send_email_notification)

    orchestrator = LlmAgent(
        model=Gemini(model="gemini-2.5-flash-lite", retry_options=retry_config),
        name="compliance_change_orchestrator",
        description=(
            "Orchestrator that summarizes recent HHS/CMS regulatory changes, "
            "compares them with the previous period, and explains their impact "
            "on compliance, security, and engineering teams."
        ),
        instruction=instruction,
        sub_agents=[reg_agent, comparator_agent],
        tools=[email_tool],
    )

    return orchestrator


# ---------------------------------------------------------------------------
# Runner
# ---------------------------------------------------------------------------

async def run_once(prompt: str) -> None:
    # 1. Build agents locally
    remote_reg_agent = build_remote_reg_agent()
    remote_comparator_agent = build_remote_comparator_agent()
    root_agent: LlmAgent = build_orchestrator_agent(remote_reg_agent, remote_comparator_agent)


    session_service = InMemorySessionService()
    app_name = "compliance_change_app"
    user_id = "cli_user"
    session_id = f"session_{uuid.uuid4().hex[:8]}"

    session = await session_service.create_session(
        app_name=app_name,
        user_id=user_id,
        session_id=session_id,
    )

    runner = Runner(
        agent=root_agent,
        app_name=app_name,
        session_service=session_service,
    )

    user_content = types.Content(parts=[types.Part(text=prompt)])

    print(f"\n👤 User: {prompt}\n")
    print("🧠 Compliance Change Orchestrator:\n")
    print("-" * 60)

    final_text_parts = []

    try:
        async for event in runner.run_async(
            user_id=user_id,
            session_id=session_id,
            new_message=user_content,
        ):
            if event.is_final_response() and event.content:
                for part in event.content.parts:
                    text = getattr(part, "text", None)
                    raw_text = getattr(part, "text", None)
                    if raw_text:
                        # Convert literal "\n" sequences into actual new lines
                        # Otherwise, these show up as "\n" characters in the output and email
                        text = raw_text.replace("\\n", "\n")
                   
                        # Print to console
                        print(text)
                        # Accumulate for email body
                        final_text_parts.append(text)
    finally:
        print("------------------------------------------------------------")

    # Build the full final answer text
    final_text = "\n".join(final_text_parts).strip()

    if final_text:
        recipient = os.getenv("COMPLIANCE_EMAIL_TO", "compliance@example.com")
        print(f"\n📧 Sending email notification to {recipient}...\n")
        send_email_notification(final_text, recipient=recipient)
    else:
        print("\nℹ️ No final text produced; skipping email notification.\n")

def main():
    if len(sys.argv) > 1:
        prompt = " ".join(sys.argv[1:]).strip()
    else:
        if not sys.stdin.isatty():
            prompt = sys.stdin.read().strip()
        else:
            prompt = ""

    if not prompt:
        print("Usage: python agent.py 'Summarize new HHS and CMS rules from the last 30 days'")
        sys.exit(1)

    try:
        asyncio.run(run_once(prompt))
    except KeyboardInterrupt:
        print("\nInterrupted by user.")
    except Exception as e:
        print(f"\nError: {e}")
        sys.exit(1)


if __name__ == "__main__":
    main()