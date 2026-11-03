"""Orion's self-knowledge.

Single source of truth for "what model are you using?", "how do you do
speech recognition?", "what can you do?" -- so Gemini stops hallucinating
plausible-sounding nonsense about Orion's own stack.

Live values (model names, voice, wake word) are pulled from config so they
stay in sync. Hand-written prose covers the parts config doesn't know
(why those choices, what categories of tools exist).
"""

import config


# config.LLM_BACKEND value -> (spoken provider name, config key holding the model).
_PROVIDERS = {
    "deepseek": ("DeepSeek", "DEEPSEEK_MODEL"),
    "gemini":   ("Google Gemini", "GEMINI_MODEL"),
    "grok":     ("Grok", "GROK_MODEL"),
}


def _backend():
    return (getattr(config, "LLM_BACKEND", "deepseek") or "").strip().lower()


def _llm_provider(name=None):
    """(provider label, model name) for a backend -- the active one by default."""
    entry = _PROVIDERS.get(name or _backend())
    if entry is None:
        return "an unknown backend", "unknown"
    label, key = entry
    return label, getattr(config, key, "unknown")


def active_model():
    """Model name of the backend answering ordinary questions."""
    return _llm_provider()[1]


def brain_line():
    provider, model = _llm_provider()
    line = (f"My reasoning runs on {provider}, specifically the {model} model. "
            "I send commands to it with my tool list attached so it picks "
            "the right action from natural phrasing, Sir.")
    # DeepSeek is text-only and has no web search, so those two jobs go
    # elsewhere. Mention it, since it changes which model sees what.
    helper = _PROVIDERS.get(
        (getattr(config, "LLM_IMAGE_BACKEND", "gemini") or "").lower())
    if _backend() == "deepseek" and helper:
        line += (f" It can't see images or search the web, so screenshots and "
                 f"real-time questions go to {helper[0]} instead, Sir.")
    return line


def stt_line():
    engine = getattr(config, "STT_ENGINE", "whisper")
    if engine == "whisper":
        # The codebase actually uses Moonshine ONNX now -- WHISPER_* keys are
        # legacy. See roadmap entry 12b.
        model = getattr(config, "MOONSHINE_MODEL", "base")
        return (f"For speech recognition I use Moonshine {model}, a small "
                "ONNX model that runs locally on the CPU. It transcribes a "
                "short phrase in about a tenth of a second, Sir.")
    return ("My speech recognition is the Google web speech API, Sir -- "
            "free but needs the internet.")


def tts_line():
    engine = getattr(config, "TTS_ENGINE", "piper")
    if engine == "piper":
        voice = getattr(config, "PIPER_VOICE", "en_US-ryan-medium")
        return (f"My voice is Piper neural TTS, the {voice} model. It runs "
                "locally on the CPU -- no cloud, no GPU, fully offline, Sir.")
    return ("My voice is the built-in Windows speech synthesiser, Sir.")


def wake_line():
    word = getattr(config, "WAKE_WORD", "orion")
    if getattr(config, "USE_LOCAL_WAKE", False):
        return (f"My wake word is '{word}'. A small openWakeWord ONNX model "
                "listens for it continuously on the CPU, Sir.")
    return f"My wake word is '{word}', Sir."


def capabilities_line():
    base = (
        "Broadly, Sir: I can open apps, websites and folders; play music on "
        "YouTube; read out unread email; identify a song that's playing; "
        "control the PC -- volume, screenshots, lock, shutdown; manage a "
        "unified agenda of calendar events, reminders and todos; run a "
        "system health check; control smart-home devices through Home "
        "Assistant; and search the web for real-time information. I also "
        "host a local web dashboard called the Hub on port "
        f"{getattr(config, 'HUB_PORT', 8765)}."
    )
    if getattr(config, "MCP_ENABLED", False):
        base += (
            " On top of my built-in tools, I'm also an MCP client: I connect "
            "to external Model Context Protocol servers, discover the tools "
            "they expose, and offer those to my reasoning model too -- so I "
            "can be extended without code changes, Sir."
        )
    return base


def stack_line():
    return (
        "I'm written in Python, Sir. The notable libraries I lean on: "
        "moonshine-onnx for speech-to-text, Piper for text-to-speech, "
        "openWakeWord for the wake word, resemblyzer for recognising your "
        "voice, fastembed for the semantic-intent and conversation-recall "
        "embeddings, and shazamio for song identification. The reasoning "
        "is delegated to the cloud model."
        + (" I talk to external tools over the Model Context Protocol using "
           "the official mcp SDK -- I'm an MCP client."
           if getattr(config, "MCP_ENABLED", False) else "")
    )


def privacy_line():
    bits = []
    if getattr(config, "VOICE_VERIFICATION", False):
        bits.append("I verify your voice on every phrase so strangers can't "
                    "issue commands or see your personal context")
    if getattr(config, "STT_ENGINE", "whisper") == "whisper":
        bits.append("speech-to-text runs locally")
    if getattr(config, "TTS_ENGINE", "piper") == "piper":
        bits.append("my voice synthesis runs locally")
    bits.append("only the reasoning step goes to the cloud model")
    return "On privacy, Sir: " + "; ".join(bits) + "."


# Topic -> builder. Keep keys short and lowercase; the matcher returns them.
_TOPICS = {
    "brain": brain_line,
    "stt": stt_line,
    "tts": tts_line,
    "wake": wake_line,
    "capabilities": capabilities_line,
    "stack": stack_line,
    "privacy": privacy_line,
}


def describe(topic=None):
    """Return a spoken description of Orion's stack.

    `topic` is one of the keys in `_TOPICS`, or None for a short general
    overview that hits brain + STT + TTS in one breath.
    """
    if topic and topic in _TOPICS:
        return _TOPICS[topic]()
    # General overview: brain + STT + TTS, in that order.
    return " ".join([brain_line(), stt_line(), tts_line()])


def full_report():
    """Every topic concatenated. Used by the Gemini tool when the user
    asks an open 'tell me about yourself' style question."""
    return " ".join(fn() for fn in _TOPICS.values())
