# sustainable-construction-digest

A Mistral Workflow that produces a weekly digest of sustainable construction and green building news for [sustainableconstructionreview.com](https://sustainableconstructionreview.com): it pulls RSS feeds from construction and sustainability outlets, keeps relevant items from the last 7 days, summarizes them with a Mistral model, features recent posts from the site itself, and emails the digest via Amazon SES with open/click tracking.

## How it works

- Workflow `sustainable-construction-digest` (see `src/worker/workflows/construction_digest.py`):
  1. `fetch_articles` — fetches all configured RSS feeds, filters entries for sustainability keywords (green building, net zero, embodied carbon, retrofit, Passivhaus, …), dedupes, and ranks by recency.
  2. `fetch_site_articles` — pulls recent posts from the site's own RSS feed (`https://sustainableconstructionreview.com/feed/`) to feature in a dedicated section; failures never break the run.
  3. `filter_relevant_articles` — keeps keyword matches plus model-picked relevant items, and records a decision (included/rejected with reason) for every candidate.
  4. `market_briefing` — asks `mistral-small-latest` for a hyperlinked market briefing (prose plus key developments); it leads the digest.
  5. `categorize_articles` — assigns each selected article to exactly one section (Policy & regulation, Green materials, Net-zero & energy, Technology, Projects & case studies, Industry news, Other).
  6. `send_digest_email` — emails the digest via Amazon SES SMTP. Skipped when `SES_SMTP_PASSWORD` is not set, or when the run is a dry run.

- **Audit trail**: the execution result carries a `selection` report with fetch stats and a per-article decision list. Running executions also expose it via the `selection_report` query.

## Configuration

Input fields (all optional):

| Field | Default | Description |
| --- | --- | --- |
| `dry_run` | `false` | Build the digest but skip sending the email. |
| `lookback_hours` | `168` | How far back to scan feeds (one week). |
| `max_articles` | `15` | Maximum news items per digest. |
| `max_site_posts` | `5` | Maximum site posts featured in "From Sustainable Construction Review". |
| `recipient` | `newsletter@sustainableconstructionreview.com` | Digest recipient. |
| `require_approval` | `false` | Wait for a review before sending. |
| `approval_timeout_hours` | `48` | Skip sending if not approved in time. |

### Review (optional)

With `require_approval` on, the run builds the digest and then suspends. Read the draft via the execution's `pending_digest` query in [Studio](https://console.mistral.ai/build/workflows), then send the `approve` signal (or `reject` to discard). On approval the digest is emailed; if neither signal arrives within `approval_timeout_hours`, sending is skipped.

## Email delivery via Amazon SES

Sending uses the SES SMTP interface (drop-in compatible with the `smtplib` code). Bind these environment variables as workspace secrets at deploy time:

| Variable | Default | Description |
| --- | --- | --- |
| `SES_SMTP_PASSWORD` | — | Required SES SMTP password. Sending is skipped when unset. |
| `SES_SMTP_HOST` | `email-smtp.us-east-1.amazonaws.com` | SES SMTP endpoint for your region. |
| `SES_SMTP_PORT` | `587` | SMTP port (STARTTLS). |
| `SES_SMTP_USER` | — | SES SMTP username (generated in the SES console). |
| `SES_SMTP_FROM` | `newsletter@sustainableconstructionreview.com` | From address (must be a verified SES identity). |
| `SES_SENDER_NAME` | `Sustainable Construction Review` | From display name. |
| `SES_CONFIGURATION_SET` | — | When set, adds the `X-SES-CONFIGURATION-SET` header so the send uses that configuration set. |
| `SES_LIST_UNSUBSCRIBE_URL` | — | When set, adds one-click `List-Unsubscribe` headers. |

### Open/click tracking

SES handles tracking natively — no code changes needed:

1. In the SES console (us-east-1), create a **configuration set** and enable **open and click tracking**.
2. Add an **event destination** (SNS, EventBridge, or CloudWatch) publishing `OPEN`, `CLICK`, `BOUNCE`, `COMPLAINT`, and `DELIVERY` events.
3. Set `SES_CONFIGURATION_SET` to the configuration set name — every digest email is then tagged and tracked automatically.
4. Optional: set a **custom redirect domain** (e.g. `link.sustainableconstructionreview.com`) in the configuration set's tracking options and add the CNAME record, so tracked links carry the site's branding.

## Site posts

`fetch_site_articles` reads the site's public RSS feed — no authentication or Cloudflare changes needed. Posts from the last `lookback_hours` are featured (up to `max_site_posts`) in the **From Sustainable Construction Review** section, right after the market briefing, and never count against the `max_articles` cap.

## Local development

```bash
python -m venv .venv
.venv/bin/pip install -e '.[dev]'
.venv/bin/pytest
```

Run a worker locally with `MISTRAL_API_KEY` set in `.env` (see `.env.example`).

## Deployment

Managed deployment builds from this repository with `deploy/docker/Dockerfile.worker`. Push the branch, then create/redeploy the deployment pinned to the commit SHA, bind the SES secrets, and add a **weekly schedule trigger** (e.g. Mondays 07:00 Europe/London).
