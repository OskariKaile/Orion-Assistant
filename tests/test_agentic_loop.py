"""Tests for the multi-step tool loop in orion.py.

The loop is driven by a scripted fake model, so no API key is needed and
nothing here touches the network. Each test hands the fake a list of turns
to return; the fake records the history it was given so the test can check
that tool results actually made it back to the model.

The headline case is the one that used to fail: two reminders at the same
time, and a spoken request to cancel both.
"""

import os
import sys
import types
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)
import _paths  # noqa: F401,E402

import config  # noqa: E402
import openai_shapes  # noqa: E402


# --- a stand-in for orion.Orion, holding just what _ask_ai_tools touches ---

class FakeOrion:
    """The slice of Orion the tool loop actually uses."""

    def __init__(self, approve=True):
        self.said = []
        self.shown = []
        self.state = None
        self.asked = []          # every confirmation prompt, in order
        self.approve = approve
        self.chips = []          # HUD tool chips: [label, done?]

    def _call_label(self, name, args):
        import orion
        return orion.Orion._call_label(name, args)

    def show_tool_start(self, label):
        self.chips.append([label, False])

    def show_tool_done(self, label, ok=True):
        for chip in self.chips:
            if chip[0] == label and not chip[1]:
                chip[1] = True
                break

    def _speak(self, text):
        self.said.append(text)

    def show_last(self, text):
        self.shown.append(text)

    def set_state(self, state):
        self.state = state

    def confirm_action(self, description):
        self.asked.append(description)
        return self.approve


class FakeModel:
    """Returns scripted turns and remembers what it was asked."""

    def __init__(self, turns):
        self.turns = list(turns)
        self.histories = []

    def send_with_tools(self, prompt, decls, history=None):
        self.histories.append(list(history or []))
        if not self.turns:
            return ("text", "")
        return self.turns.pop(0)


def call(name, args, cid="c1"):
    return {"id": cid, "name": name, "args": args}


class LoopTest(unittest.TestCase):
    """Runs orion._ask_ai_tools with the model and tools stubbed out."""

    def setUp(self):
        import orion
        import memory
        self.orion_mod = orion
        # Keep prompts cheap and deterministic: no facts, no live state.
        self._prompt = memory.build_prompt
        memory.build_prompt = lambda q, **kw: f"PROMPT: {q}"
        self._add_user, self._add_orion = memory.add_user, memory.add_orion
        memory.add_user = lambda *a, **kw: None
        memory.add_orion = lambda *a, **kw: None
        self.memory = memory

    def tearDown(self):
        self.memory.build_prompt = self._prompt
        self.memory.add_user = self._add_user
        self.memory.add_orion = self._add_orion

    def run_loop(self, turns, handlers, query="do the thing", approve=True):
        """Drive the loop with scripted model turns and fake tools.

        `handlers` maps tool name -> return value (a string, or None for a
        tool that speaks for itself). Confirmation is NOT stubbed -- the
        real tools.confirm_batch runs against FakeOrion.confirm_action, so
        the gate on destructive tools is exercised here too.

        Returns (fake_orion, model, ran).
        """
        import tools as tools_mod
        ran = []
        model = FakeModel(turns)
        fake = FakeOrion(approve=approve)

        def run_call(name, args, orion, approved=None):
            ran.append((name, args))
            return handlers.get(name)

        saved = (self.orion_mod.ai, tools_mod.run_call, tools_mod.declarations)
        self.orion_mod.ai = model
        tools_mod.run_call = run_call
        tools_mod.declarations = lambda: []
        try:
            self.orion_mod.Orion._ask_ai_tools(fake, query)
        finally:
            (self.orion_mod.ai, tools_mod.run_call,
             tools_mod.declarations) = saved
        return fake, model, ran

    # --- the case that used to drop the second reminder ------------------

    def test_cancels_both_reminders_in_one_turn(self):
        """Two cancel calls in one reply both run, and both are spoken."""
        turns = [
            ("calls", [call("cancel_reminder", {"id": "r_1a89"}, "c1"),
                       call("cancel_reminder", {"id": "r_a286"}, "c2")]),
        ]
        handlers = {"cancel_reminder": "Cancelled the reminder, Sir."}
        fake, model, ran = self.run_loop(turns, handlers,
                                         "cancel both German reminders")

        self.assertEqual([a["id"] for _, a in ran], ["r_1a89", "r_a286"])
        # Both results reach the user, not just the first.
        self.assertEqual(fake.said[0].count("Cancelled"), 2)
        # Pure action tools finish in one round trip.
        self.assertEqual(len(model.histories), 1)
        # One spoken confirmation for the pair, and it says how many.
        self.assertEqual(len(fake.asked), 1)
        self.assertIn("those 2 reminders", fake.asked[0])

    def test_every_tool_call_shows_a_finished_hud_chip(self):
        """Each call the model makes appears as a HUD chip, marked done."""
        turns = [
            ("calls", [call("open", {"app": "spotify"}, "c1"),
                       call("set_timer", {"minutes": 20}, "c2")]),
        ]
        handlers = {"open": "Opening Spotify.", "set_timer": "Timer set."}
        fake, _model, _ran = self.run_loop(turns, handlers)

        self.assertEqual(fake.chips, [['open(app="spotify")', True],
                                      ["set_timer(minutes=20)", True]])

    def test_refusing_stops_every_call_in_the_batch(self):
        """Saying no to the batch cancels nothing and says so once."""
        turns = [
            ("calls", [call("cancel_reminder", {"id": "r_1a89"}, "c1"),
                       call("cancel_reminder", {"id": "r_a286"}, "c2")]),
        ]
        fake, _model, ran = self.run_loop(
            turns, {"cancel_reminder": "Cancelled."},
            "cancel both", approve=False)

        self.assertEqual(ran, [])                  # nothing ran
        self.assertEqual(len(fake.asked), 1)       # asked once
        self.assertEqual(fake.said, ["Okay, I'll leave it."])  # said once

    def test_harmless_tools_are_never_confirmed(self):
        turns = [("calls", [call("lights_on", {}, "c1")])]
        fake, _m, _r = self.run_loop(turns, {"lights_on": "On."}, "lights on")
        self.assertEqual(fake.asked, [])

    def test_lookup_then_act(self):
        """A query tool sends the loop back to the model with its result."""
        turns = [
            ("calls", [call("list_reminders", {}, "c1")]),
            ("calls", [call("cancel_reminder", {"id": "r_1a89"}, "c2"),
                       call("cancel_reminder", {"id": "r_a286"}, "c3")]),
        ]
        handlers = {
            "list_reminders": "r_1a89 study for German; r_a286 study German",
            "cancel_reminder": "Cancelled it, Sir.",
        }
        fake, model, ran = self.run_loop(turns, handlers,
                                         "cancel the two at ten")

        self.assertEqual([n for n, _ in ran],
                         ["list_reminders", "cancel_reminder",
                          "cancel_reminder"])
        # Second call carried the lookup's result back to the model.
        second = model.histories[1]
        self.assertEqual(second[1]["role"], "assistant")
        self.assertEqual(second[2]["role"], "tool")
        self.assertIn("study German", second[2]["content"])
        # The list itself is never read aloud -- only the cancellations.
        self.assertNotIn("r_1a89", fake.said[0])

    def test_model_summary_wins_over_raw_lookup(self):
        """After a lookup, the model's own words are what get spoken."""
        turns = [
            ("calls", [call("list_reminders", {}, "c1")]),
            ("text", "You've got two, Sir: German at ten, and the gym at six."),
        ]
        handlers = {"list_reminders": "r_1 german; r_2 gym"}
        fake, _model, _ran = self.run_loop(turns, handlers,
                                            "what are my reminders")
        self.assertEqual(fake.said, ["You've got two, Sir: German at ten, "
                                     "and the gym at six."])

    # --- latency, safety and failure behaviour ---------------------------

    def test_action_only_does_not_revisit_the_model(self):
        """An ordinary command still costs exactly one API call."""
        turns = [("calls", [call("lights_on", {}, "c1")])]
        _fake, model, _ran = self.run_loop(turns, {"lights_on": "Lights on."},
                                            "lights on")
        self.assertEqual(len(model.histories), 1)

    def test_self_speaking_tool_stays_quiet(self):
        """run_call returning None means the handler already spoke."""
        turns = [("calls", [call("explain_screen", {}, "c1")])]
        fake, _model, ran = self.run_loop(turns, {"explain_screen": None},
                                           "what's on my screen")
        self.assertEqual(len(ran), 1)
        self.assertEqual(fake.said, [])  # no duplicate "Done, Sir."

    def test_step_ceiling_is_enforced(self):
        """A model that keeps asking for lookups gets cut off."""
        turns = [("calls", [call("list_reminders", {}, f"c{i}")])
                 for i in range(20)]
        _fake, model, _ran = self.run_loop(turns, {"list_reminders": "none"},
                                            "loop forever")
        self.assertEqual(len(model.histories), config.MAX_TOOL_STEPS)

    def test_partial_work_is_reported_when_the_model_dies(self):
        """A mid-loop API failure still says what already happened."""
        turns = [
            ("calls", [call("list_reminders", {}, "c1")]),
            ("error", ""),
        ]
        handlers = {"list_reminders": "one reminder, Sir: German at ten"}
        fake, _model, _ran = self.run_loop(turns, handlers, "my reminders?")
        self.assertIn("German", fake.said[0])
        self.assertNotIn("API", fake.said[0])

    def test_hard_failure_reports_the_api_key(self):
        """Failing before anything ran gives the actionable message."""
        fake, _model, ran = self.run_loop([("error", "")], {}, "hello")
        self.assertEqual(ran, [])
        self.assertIn("API key", fake.said[0])

    def test_always_ends_idle(self):
        """Every exit path releases the UI state."""
        for turns in ([("text", "hi")], [("error", "")],
                      [("calls", [call("lights_on", {}, "c1")])]):
            fake, _m, _r = self.run_loop(turns, {"lights_on": "On."})
            self.assertEqual(fake.state, "idle")


class NeutralShapeTest(unittest.TestCase):
    """The neutral history has to survive the trip to each wire format."""

    HISTORY = [
        {"role": "user", "content": "cancel both"},
        {"role": "assistant", "content": "",
         "calls": [{"id": "c1", "name": "cancel_reminder",
                    "args": {"id": "r_1"}},
                   {"id": "c2", "name": "cancel_reminder",
                    "args": {"id": "r_2"}}]},
        {"role": "tool", "id": "c1", "name": "cancel_reminder",
         "content": "Cancelled one."},
        {"role": "tool", "id": "c2", "name": "cancel_reminder",
         "content": "Cancelled two."},
    ]

    def test_openai_pairs_results_to_calls_by_id(self):
        msgs = openai_shapes.to_messages(self.HISTORY)
        self.assertEqual([m["role"] for m in msgs],
                         ["user", "assistant", "tool", "tool"])
        self.assertEqual([c["id"] for c in msgs[1]["tool_calls"]],
                         ["c1", "c2"])
        self.assertEqual([m["tool_call_id"] for m in msgs[2:]], ["c1", "c2"])
        # Arguments go over the wire as a JSON string, not a dict.
        self.assertIsInstance(
            msgs[1]["tool_calls"][0]["function"]["arguments"], str)

    def test_openai_survives_malformed_arguments(self):
        """A model emitting broken JSON still gets its call run."""
        bad = types.SimpleNamespace(
            id="c9",
            function=types.SimpleNamespace(name="lights_on",
                                            arguments="{not json"))
        message = types.SimpleNamespace(tool_calls=[bad])
        calls = openai_shapes.parse_calls(message)
        self.assertEqual(calls, [{"id": "c9", "name": "lights_on",
                                  "args": {}}])

    def test_gemini_groups_results_into_one_turn(self):
        """Gemini pairs call to result by position, so grouping matters."""
        try:
            import gemini_bridge
            from google.genai import types as gtypes  # noqa: F401
        except Exception:
            self.skipTest("google-genai not installed")
        if gemini_bridge.types is None:
            self.skipTest("google-genai not importable")

        contents = gemini_bridge._to_gemini_contents(self.HISTORY)
        self.assertEqual([c.role for c in contents], ["user", "model", "user"])
        # Two calls in one model turn, two results in the turn after, in the
        # same order -- anything else and Gemini can't tell them apart.
        self.assertEqual(len(contents[1].parts), 2)
        self.assertEqual(len(contents[2].parts), 2)

    def test_gemini_replays_the_thought_signature(self):
        """Gemini 3 rejects a replayed call that lost its signature.

        Live symptom without this: step 1 works, and every step after it
        comes back 400 INVALID_ARGUMENT -- so the loop silently degrades to
        the single-shot behaviour it was built to replace.
        """
        try:
            import gemini_bridge
        except Exception:
            self.skipTest("google-genai not installed")
        if gemini_bridge.types is None:
            self.skipTest("google-genai not importable")

        history = [
            {"role": "user", "content": "cancel them"},
            {"role": "assistant", "content": "",
             "calls": [{"id": "call_0", "name": "list_reminders",
                        "args": {}, "signature": b"sig-blob"}]},
            {"role": "tool", "id": "call_0", "name": "list_reminders",
             "content": "two of them"},
        ]
        contents = gemini_bridge._to_gemini_contents(history)
        self.assertEqual(contents[1].parts[0].thought_signature, b"sig-blob")

    def test_openai_ignores_the_gemini_signature(self):
        """The opaque field is Gemini's; it must not reach the OpenAI wire."""
        history = [{"role": "assistant", "content": "",
                    "calls": [{"id": "c1", "name": "x", "args": {},
                               "signature": b"sig-blob"}]}]
        fn = openai_shapes.to_messages(history)[0]["tool_calls"][0]
        self.assertEqual(set(fn), {"id", "type", "function"})
        self.assertEqual(set(fn["function"]), {"name", "arguments"})


if __name__ == "__main__":
    unittest.main()
