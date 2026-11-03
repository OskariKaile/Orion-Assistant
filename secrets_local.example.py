"""Template for secrets_local.py.

Copy this file to secrets_local.py and fill in your real values. The real
secrets_local.py is git-ignored so your keys never get committed.
"""

# Free Gemini API key -- get one at https://aistudio.google.com/apikey
GEMINI_API_KEY = ""

# xAI Grok API key -- get one at https://console.x.ai
GROK_API_KEY = ""

# DeepSeek API key -- get one at https://platform.deepseek.com
# This is Orion's default brain (config.LLM_BACKEND = "deepseek").
DEEPSEEK_API_KEY = ""

# Home Assistant long-lived access token (smart-home control).
# Create one under your Home Assistant profile page. Leave blank to disable.
HOME_ASSISTANT_TOKEN = ""

# Google Maps API key for the optional Maps MCP server (directions, places).
# Google Cloud Console -> enable the Maps APIs. Leave blank to disable.
GOOGLE_MAPS_API_KEY = ""

# Email accounts Orion can READ from (read-only IMAP -- no sending, no
# marking-as-read, no deleting). For Gmail / Google Workspace each account
# needs an "app password":
#   1. Google account -> Security -> turn on 2-Step Verification (required).
#   2. Security -> App passwords -> create one for "Mail / Other (Orion)".
#   3. Paste the 16-character password Google gives you below (spaces ok).
# Account names ("personal", "work") are the friendly labels you'd say to
# Orion -- they must match what's in config.EMAIL_ACCOUNTS. Common hosts:
#   Gmail / Google Workspace : imap.gmail.com
#   Outlook / Office 365     : outlook.office365.com
#   iCloud                   : imap.mail.me.com
#   Fastmail                 : imap.fastmail.com
EMAIL_CREDENTIALS = {
    # "personal": {
    #     "address":   "you@gmail.com",
    #     "password":  "abcd efgh ijkl mnop",
    #     "imap_host": "imap.gmail.com",
    # },
    # "work": {
    #     "address":   "you@company.com",
    #     "password":  "...",
    #     "imap_host": "imap.gmail.com",
    # },
}
