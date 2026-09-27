# Skool Post Bot

Drafts **two posts a day** for a peptide/fitness Skool community:

| Slot | Default time | What it writes |
|------|------|----------------|
| `morning` | 9am ET | **News post.** Breaks down the freshest peptide headline or study: what happened, why lifters should care, and what we still don't know. |
| `evening` | 7pm ET | **Creative post.** Rotates daily through content pillars: Myth vs. Fact, Gym Tie-In, Explain It Like I Lift, Community Poll, Study Snack, Hot Take, Member Challenge. |

**Skool has no public API for creating posts**, so the bot doesn't post for
you. It sends a ready-to-paste draft (title, body, poll options, sources,
image idea) that you review and paste into Skool. Reviewing each post also
keeps a human check on health claims.

## How it works

```
Google News RSS + PubMed  ->  drop items already posted about  ->  Claude writes the post
                                                                      |
                              drafts/2026-09-27-morning.md  <---------+--> email / webhook
```

- `config.yaml` sets your community name, audience, voice, news searches,
  and the creative pillars. **Edit it to make the posts sound like you.**
- `data/used_items.json` records which stories and titles have been used,
  so the bot doesn't repeat itself.
- Built-in guardrails: no dosing or protocols, no vendor or sourcing talk,
  clear evidence levels ("rats" vs. "humans"), correct FDA status, sources
  only from the fetched news (any other links are removed), and an
  "Educational only, not medical advice" line. These keep the community
  safer and help keep your group within Skool's rules.

## Setup (runs free on GitHub Actions)

1. Get an Anthropic API key at https://console.anthropic.com. Each post
   costs a few cents.
2. In GitHub, open the repo's **Settings -> Secrets and variables -> Actions**
   and add:
   - `ANTHROPIC_API_KEY` (required)
   - Email delivery (optional, works with Gmail):
     `SMTP_HOST=smtp.gmail.com`, `SMTP_USER=you@gmail.com`,
     `SMTP_PASSWORD=<Gmail app password>` (not your normal password; create
     one at https://myaccount.google.com/apppasswords), and `POST_EMAIL_TO`
     set to the address that should receive the drafts.
   - `POST_WEBHOOK_URL` (optional): a Zapier, Make, Discord or Slack webhook,
     for sending drafts to your phone or another tool.
3. Merge this into the repo's default branch. GitHub only runs scheduled
   workflows from the default branch.
4. Test it: **Actions -> Skool daily posts -> Run workflow**, then pick a slot.

Every draft is also committed to `skool_bot/drafts/`, so you can read it in
the GitHub mobile app even without email.

To change the posting times, edit the two `cron` lines in
`.github/workflows/skool-posts.yml`. They use UTC. If you change them,
update the matching `if` line in that file too.

## Run locally

```bash
cd skool_bot
pip install -r requirements.txt
export ANTHROPIC_API_KEY=...
python -m skoolbot --slot morning             # writes drafts/<date>-morning.md
python -m skoolbot --slot evening --preview   # shows the prompt, no API call
python -m pytest tests
```
