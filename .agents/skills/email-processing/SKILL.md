---
name: email-processing
description: AI-powered email processing assistant using Proton Bridge, pm-cli, and Hindsight memory. Processes emails from INBOX by first reflecting on content via Hindsight, then categorizing and forwarding to matze@vongerichten.com using plus-alias addressing (+daily/+weekly/+monthly/+triage). During training phase, asks user for each decision; progressively learns from user decisions via Hindsight retain operations. In background mode (non-interactive), uncertain emails are sent to triage for user decision.
---

# Email Processing Skill

AI-powered email processing assistant that uses Hindsight memory to learn from user decisions and progressively automate email handling.

## Required Dependencies

| Component | Installation |
|-----------|-------------|
| **Proton Bridge** | `dpkg -i protonmail-bridge_*.deb` + `apt --fix-broken install` |
| **pass + gnome-keyring** | `sudo apt install pass gnome-keyring` |
| **pm-cli** | `git clone https://github.com/bscott/pm-cli && cd pm-cli && go build ./cmd/pm-cli` |
| **Hindsight** | Opencode plugin (already integrated) |

**Email Account:** `epa.vcom@proton.me` (configured in Proton Bridge)

## Email Categorization

| Priority | Target Email | Criteria |
|----------|--------------|----------|
| **Critical/Emergency** | `matze+asap@vongerichten.com` | Security alerts, critical incidents requiring immediate action |
| **Urgent + Important** | `matze+daily@vongerichten.com` | Needs reaction today |
| **Important, Not Urgent** | `matze+weekly@vongerichten.com` | Needs reaction by end of week |
| **Not Important, Not Urgent** | `matze+monthly@vongerichten.com` | Can wait, no real urgency |
| **Triage (Background Mode)** | `matze+triage@vongerichten.com` | Uncertain decisions, needs user input |

## Interactive vs Background Mode

### Interactive Mode (Current Session)
- User is directly chat with the agent
- Ask user for decision on uncertain emails
- Process feedback and retain decisions

### Background Mode (Non-Interactive)
- Agent running autonomously with no direct chat interaction
- For uncertain emails: forward to `matze+triage@vongerichten.com` with questions
- Triage emails include: original sender, subject, content summary, and specific questions
- User replies to triage email with instructions
- Agent detects triage responses and processes them accordingly

## Processing Workflow

### Phase 1: Fetch Email (use stable UIDs, oldest first)
```
# List inbox with JSON to see stable UIDs
pm-cli mail list --json

# Read by uid (stable identifier - survives deletions)
# Format: uid:<IMAP-uid> (e.g., uid:57)
pm-cli mail read uid:<uid> --json
```

**Important:** Always use `uid:<uid>` (from the `uid` field in JSON output) instead of `seq_num` (sequence number). Sequence numbers re-shuffle after deletions. UIDs are assigned by the IMAP server and are permanent for a message's lifetime in a mailbox.

**Processing order:** Always process from **oldest to newest** (bottom of the inbox list to top). Use the `date_iso` field to determine age — the oldest messages have been waiting longest and should be handled first. **Process ALL emails regardless of seen/unread flag** — a seen email still needs categorization and forwarding.

### Phase 2: Hindsight Reflection
Use Hindsight `reflect` to query prior decisions and patterns:
```
hindsight_reflect(
    query="What should I do with an email about: <subject>? Summary: <short content summary>. Sender: <from>."
)
```

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
- If so, extract the decision and process accordingly, then retain via Hindsight

### Phase 5: Retain Decision (include stable UID)
After each decision, call `hindsight_retain`:
```
hindsight_retain(
    content="Email uid:<uid> from <sender> about <subject>: <content summary>. Decision: <category>. User said: <full reasoning>",
    context="email-processing-decision",
    timestamp="<ISO date>"
)
```

### Phase 6: Execute Forward (use stable UID)
```
pm-cli mail forward uid:<uid> -t matze+<category>@vongerichten.com
```

### Phase 7: Move to Trash (use stable UID)
```
pm-cli mail move --destination=Trash uid:<uid>
```
**Important:** Use `move` instead of `delete` - the `delete` command only marks as `\Deleted` but does NOT actually move messages to the Trash mailbox. Always use `uid:<uid>` format for the stable ID.

## Triage Email Format

When forwarding uncertain emails to triage, include:
```
Subject: [TRIAGE] <original subject>

Original email from: <sender>
Date: <date>

Content summary:
<summary>

Questions for decision:
1. <specific question 1>
2. <specific question 2>

Please reply with your decision (asap/daily/weekly/monthly or custom action).
```

## Notes

- Always use `--json` flag with pm-cli for structured output (shows the `uid` field)
- **ALWAYS use `uid:<uid>` (not seq_num `<id>`)** — the `seq_num` re-indexes after deletions/archives. The `uid` is the permanent IMAP UID assigned by the server and survives the message's lifetime in a mailbox.
- When referencing an email in Hindsight retain or questions, include both `uid:<uid>` and the subject for cross-reference
- Use `--json` or `--help-json` flags to get structured output for AI processing
- Training phase: ask user for EVERY decision until confidence is high
- Consolidation improves reflection quality over time
- Mental model updates happen during retain operations
- Background mode activates when no interactive chat session is detected
