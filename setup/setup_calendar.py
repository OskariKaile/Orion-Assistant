"""One-time Google Calendar OAuth setup for Orion.

Run this once:

    python setup_calendar.py

What it does
------------
1. Looks for `credentials.json` in the Orion folder. If missing, prints
   instructions on how to download it from Google Cloud Console.
2. Opens a browser window to the Google consent screen. You click
   "Allow" -- this grants Orion read/write access to your primary
   Google Calendar.
3. Saves the resulting refresh token to `token.json`. After this Orion
   refreshes the token itself; you never need to re-auth.

How to get credentials.json
---------------------------
1. https://console.cloud.google.com/ -> create a new project (any name).
2. APIs & Services -> Library -> search "Google Calendar API" -> Enable.
3. APIs & Services -> OAuth consent screen -> External -> fill the bare
   minimum (app name "Orion", your email, save). Add your Google
   account as a Test User on the next page.
4. APIs & Services -> Credentials -> Create Credentials -> OAuth client
   ID -> Application type "Desktop app" -> Create.
5. Download JSON -> rename to `credentials.json` -> drop it next to
   this script.
"""

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import _paths  # noqa: F401 -- bootstrap sys.path for sibling subfolders


_HERE = _paths.ROOT
_CRED_PATH = os.path.join(_HERE, "credentials.json")
_TOKEN_PATH = os.path.join(_HERE, "token.json")
_SCOPES = ["https://www.googleapis.com/auth/calendar"]


def main():
    if not os.path.exists(_CRED_PATH):
        print("credentials.json not found in the Orion folder.")
        print()
        print(__doc__)
        sys.exit(1)

    try:
        from google_auth_oauthlib.flow import InstalledAppFlow
        from googleapiclient.discovery import build
    except ImportError:
        print("Google client libraries aren't installed. Run:")
        print("  pip install google-api-python-client google-auth-oauthlib")
        sys.exit(1)

    print("Opening your browser for Google sign-in...")
    flow = InstalledAppFlow.from_client_secrets_file(_CRED_PATH, _SCOPES)
    creds = flow.run_local_server(port=0)
    with open(_TOKEN_PATH, "w", encoding="utf-8") as fh:
        fh.write(creds.to_json())
    print(f"Saved token to {_TOKEN_PATH}")

    # Quick sanity check -- list the upcoming 3 events.
    svc = build("calendar", "v3", credentials=creds, cache_discovery=False)
    import datetime
    now = datetime.datetime.utcnow().isoformat() + "Z"
    resp = svc.events().list(calendarId="primary", timeMin=now,
                              maxResults=3, singleEvents=True,
                              orderBy="startTime").execute()
    events = resp.get("items", [])
    if not events:
        print("No upcoming events found on your primary calendar.")
    else:
        print("Next few events:")
        for ev in events:
            start = ev["start"].get("dateTime") or ev["start"].get("date")
            print(f"  {start}  {ev.get('summary', '(no title)')}")
    print("Done. Orion will use Google Calendar from now on.")


if __name__ == "__main__":
    main()
