# viggo-watch

Watches [ViGGO](https://viggo.dk) (the Danish school/efterskole communication
platform) for new messages, bulletin board posts ("opslagstavle"/ugebreve),
and room forum threads, and pushes a [ntfy](https://ntfy.sh) notification for
each one. Optionally uses Claude to read the full text and flag concrete
action items buried in otherwise-long posts (a common ViGGO complaint: a
"bring X by week N" instruction gets lost in a long ugebrev).

Runs as a single Docker container, polling on a fixed interval. No ViGGO API
access required - it logs in and scrapes the same HTML endpoints the regular
web client uses, authenticated with your own account.

## Setup

1. `cp .env.template .env` and fill in your values - see the comments in
   `.env.template` for where to find each one (your ViGGO subdomain, room
   IDs, bulletin board type IDs, etc.).
2. The trickiest one-time step is `VIGGO_FINGERPRINT`: log into your school's
   ViGGO in a normal browser once (complete the email 2FA code as usual),
   then copy the `fingerprint` value out of DevTools -> Local Storage for
   that site. That value lets the container skip 2FA on future logins.
3. `docker compose up -d --build`
4. Subscribe to your `NTFY_TOPIC` in the [ntfy app](https://ntfy.sh/app) or
   at `ntfy.sh/<your-topic>`.

First run only baselines what's currently there (no notification spam) -
notifications start from the second run onward.

## Notes / limitations

- Built and tested against one school's ViGGO instance
  (`vibyfriskole.viggo.dk`). Page structure may differ slightly between
  schools or change over time - if `docker compose logs` shows nothing
  being found, check `app/main.py`'s CSS selectors against your instance.
- The device fingerprint trick relies on undocumented ViGGO behavior and
  could stop working if they change it. If so, the app alerts you via ntfy
  that it needs a fresh fingerprint (re-run step 2 above).
- AI triage is optional - leave `ANTHROPIC_API_KEY` empty to get raw
  notifications with the full post text, no action-item extraction.
