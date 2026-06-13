---
name: email-processing
description: AI-powered email processing assistant using pm-cli and Hindsight memory. Processes emails from INBOX by first reflecting on content via Hindsight, then categorizing and forwarding to matze@vongerichten.com using plus-alias addressing (+asap/+daily/+weekly/+monthly/+triage). During training phase, asks user for each decision; progressively learns from user decisions via Hindsight retain operations. In background mode (non-interactive), uncertain emails are sent to triage for user decision.
metadata:
  {
    "openclaw":
      {
        "requires": { "bins": ["pm-cli"] },
     },
  }
---

# Email Processing Skill

AI-powered email processing assistant that uses memory to learn from user decisions and progressively automate email handling.

> Note: As of June 2026, pm-cli in this sandbox is a thin TCP proxy to a host-side daemon. Credentials (PAT, bridge password) remain on the host. Domain restrictions are enforced both by the daemon and by the pm-cli config.

## Required Dependencies

| Component | Installation |
|-----------|-------------|
| **ProtonMail Bridge** | |
| **pass** | |
| **pm-cli** | |
| **Hindsight** | |

**Email Account:** `epa.vcom@proton.me` (configured in ProtonMail Bridge)

## Email Categorization

| Priority | Target Email | Criteria |
|----------|--------------|----------|
| **Critical/Emergency** | `matze+asap@vongerichten.com` | Security alerts, critical incidents requiring immediate action |
| **Urgent + Important** | `matze+daily@vongerichten.com` | Needs reaction today |
| **Important, Not Urgent** | `matze+weekly@vongerichten.com` | Needs reaction by end of week |
| **Not Important, Not Urgent** | `matze+monthly@vongerichten.com` | Can wait, no real urgency |
| **Triage (Background Mode)** | `matze+triage@vongerichten.com` | Uncertain decisions, needs user input |

## Interactive vs Background Mode

### Interactive Mode
- User is directly chat with the agent
- Ask user for decision on uncertain emails
- Process feedback and retain decisions

### Background Mode (cron, Non-Interactive)
- Agent running autonomously with no direct chat interaction
- For uncertain emails: forward to `matze+triage@vongerichten.com` with questions
- User replies to triage email with instructions
- Agent detects triage responses and processes them accordingly and retains decision

## Processing Workflow

> IMPORTANT: YOU NEVER BATCH EMAILS, you process them one-by-one!

### Phase 1: Fetch Email (use stable UIDs, oldest first)
```
# List inbox with JSON to see stable UIDs
pm-cli mail list --json --limit=100

# Read by uid (stable identifier - survives deletions)
# Format: uid:<IMAP-uid> (e.g., uid:57)
pm-cli mail read uid:<uid> --json
```

**DO**:
- Use `uid:<uid>` (from the `uid` field in JSON output) instead of `seq_num` (sequence number). Sequence numbers re-shuffle after deletions. UIDs are assigned by the IMAP server and are permanent for a message's lifetime in a mailbox.
- Process from **oldest to newest** (bottom of the inbox list to top). Use the `date_iso` field to determine age
- Process ALL emails regardless of seen/unread flag

**DO NOT**:
- Return an overview of all the emails, focus on the next email
- Reflect generally on last processed emails, YOU DO ONLY reflect on specific emails to process

### Phase 2: Reflection — ALWAYS FIRST (never present options before reflecting)
**HARD RULE: Reflection MUST be completed and its results read/considered BEFORE presenting any triage options, recommendations, or questions to the user.** Do not show categories, do not ask for a decision, do not present options — until reflection has been called and its output has been read. Presenting options before reflecting is a process violation.

Use Hindsight `reflect` (aka `agent_knowledge_reflect`) to query prior decisions and patterns.

**CRITICAL: The reflect query MUST include a substantive summary of the email content**, not just the subject line. Read the full email body (or a concise plain-text extraction of it) and include key details in the summary field. The subject alone is often too vague for Hindsight to produce good results.
```
hindsight_reflect(
    query="What should I do with an email about: <subject>? Summary: <short but meaty content summary — key details from the body>. Sender: <from>."
)
```
**Bad example (subject only):** `Summary: Newsletter from Sunday Natural.`
**Good example (with body content):** `Summary: Promotional newsletter from Sunday Natural with a 10% discount code DEWAUPD3G77 on bestsellers like Omega 3 Complete, expiring today 08.06.2026.`
If `reflect` is not available, fallback to `recall` (aka `agent_knowledge_recall`)

**Only after reflection is complete**, proceed to Phase 3.

### Phase 3: Determine Decision Mode

#### Training Mode (No Prior Knowledge)
If reflect returns no confident decision:
- **Interactive session**: Present email summary to user, ask for decision
- **Background mode**: Forward to `matze+triage@vongerichten.com` with questions

#### Automated Mode (Learning Active)
If reflect returns confident guidance:
- **Interactive session**: Present suggested action, execute if approved
- **Background mode**: Execute automatically if confident

### Phase 4: Detect Triage Responses
When processing new emails, check if:
- Email is a reply to a previously sent triage email
- Contains user instructions in response to a triage request
- If so, extract the decision and process accordingly, then retain

### Phase 5: Retain Decision (include stable UID)
After each decision, call `hindsight_retain` (aka `agent_knowledge_retain`):
```
hindsight_retain(
    content="Email from <sender> about <subject>: <content summary>. Decision: <category>. User said: <full reasoning>",
    context="email-processing",
    timestamp="<ISO date>"
)
```

### Phase 6: Execute Forward (use stable UID)
```
pm-cli mail forward uid:<uid> -t matze+<category>@vongerichten.com
```
In case of triage, add question(s) by appending ` -b <question>`.

### Phase 7: Move to Trash (use stable UID)
```
pm-cli mail move --destination=Trash uid:<uid>
```
**Important:** Use `move` instead of `delete` - the `delete` command only marks as `\Deleted` but does NOT actually move messages to the Trash mailbox. Always use `uid:<uid>` format for the stable ID.

## Notes

- EXACTLY use the commands mentioned - only these have exec approvals, others will be rejected! 
- Always use `--json` flag with pm-cli for structured output (shows the `uid` field)
- **ALWAYS use `uid:<uid>` (not seq_num `<id>`)** — the `seq_num` re-indexes after deletions/archives. The `uid` is the permanent IMAP UID assigned by the server and survives the message's lifetime in a mailbox.
- When referencing an email in Hindsight retain or questions, include both the sender and the subject for cross-reference
- Hindsight "Mental Model" updates happen after retain operations, which improves reflection results over time
- Background mode activates when no interactive chat session is detected
