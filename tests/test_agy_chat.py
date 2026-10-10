"""agy-chat tests: routing, and whole turns against fake_agy.py and a fake Telegram adapter.

Run with the gateway's interpreter (it imports Hermes' gateway modules):
  <hermes venv python> tests/test_agy_chat.py
"""

import asyncio
import importlib.util
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace

PLUGIN_DIR = Path(__file__).resolve().parent.parent
TESTS_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(Path(os.environ.get("HERMES_HOME") or Path.home() / ".hermes") / "hermes-agent"))


def _load_plugin():
    spec = importlib.util.spec_from_file_location("agy_chat_under_test", PLUGIN_DIR / "__init__.py",
                                                  submodule_search_locations=[str(PLUGIN_DIR)])
    module = importlib.util.module_from_spec(spec)
    sys.modules["agy_chat_under_test"] = module
    spec.loader.exec_module(module)
    return module


plugin = _load_plugin()
engine = sys.modules["agy_chat_under_test.engine"]
store = sys.modules["agy_chat_under_test.state"]
workspace = sys.modules["agy_chat_under_test.workspace"]
CHAT = "424242"
SETTINGS = {"chat_id": CHAT, "user_name": "Alex"}
plugin.configure(lambda key, default=None: SETTINGS.get(key, default))


def ok(message_id=None):
    return SimpleNamespace(success=True, message_id=message_id, continuation_message_ids=(), error=None)


class FakeBot:
    def __init__(self, adapter):
        self.adapter = adapter

    async def send_message(self, chat_id, text, reply_markup=None, **kw):
        self.adapter.calls.append(("bot_send", text, reply_markup))
        return SimpleNamespace(message_id=int(self.adapter._new_id()))

    async def pin_chat_message(self, chat_id, message_id, disable_notification=False):
        self.adapter.calls.append(("pin", message_id, disable_notification))

    async def unpin_chat_message(self, chat_id, message_id=None):
        self.adapter.calls.append(("unpin", message_id))

    async def delete_message(self, chat_id, message_id):
        self.adapter.calls.append(("delete", message_id))


class FakeAdapter:
    def __init__(self):
        self.calls, self.next_id = [], 1000
        self._bot = FakeBot(self)

    def _new_id(self):
        self.next_id += 1
        return str(self.next_id)

    async def send(self, chat_id, content, reply_to=None, metadata=None):
        self.calls.append(("send", content, reply_to))
        return ok(self._new_id())

    async def edit_message(self, chat_id, message_id, content, *, finalize=False, metadata=None):
        self.calls.append(("edit", message_id, content, finalize))
        return ok(message_id)

    async def send_typing(self, chat_id, metadata=None):
        self.calls.append(("typing",))

    async def _set_reaction(self, chat_id, message_id, emoji):
        self.calls.append(("react", str(message_id), emoji))
        return True

    async def _file(self, kind, path, reply_to):
        self.calls.append((kind, path, reply_to))
        return ok(self._new_id())

    async def send_image_file(self, chat_id, image_path, caption=None, reply_to=None, metadata=None, **kw):
        return await self._file("image", image_path, reply_to)

    async def send_document(self, chat_id, file_path, caption=None, file_name=None, reply_to=None, metadata=None, **kw):
        return await self._file("document", file_path, reply_to)

    async def send_video(self, chat_id, video_path, caption=None, reply_to=None, metadata=None, **kw):
        return await self._file("video", video_path, reply_to)

    async def send_voice(self, chat_id, audio_path, caption=None, reply_to=None, metadata=None, **kw):
        return await self._file("voice", audio_path, reply_to)

    async def send_choice_picker(self, chat_id, title, choices, session_key, on_choice_selected, metadata=None):
        self.calls.append(("picker", title, choices))
        self.on_choice = on_choice_selected
        return ok(self._new_id())

    def sent(self, kind="send"):
        return [c for c in self.calls if c[0] == kind]


def event(text="", media=(), types=(), mtype="text", msg_id="1", reply_to=None, reply_text=None, chat=CHAT):
    return SimpleNamespace(
        text=text, media_urls=list(media), media_types=list(types), message_type=SimpleNamespace(value=mtype),
        message_id=msg_id, reply_to_message_id=reply_to, reply_to_text=reply_text, internal=False,
        allow_gateway_control=True, source=SimpleNamespace(platform=SimpleNamespace(value="telegram"), chat_id=chat))


class Base(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="agy-chat-test-"))
        self.saved = {
            (store, "STATE_FILE"): store.STATE_FILE, (store, "TURNS_LOG"): store.TURNS_LOG,
            (workspace, "WORKSPACE"): workspace.WORKSPACE, (workspace, "INBOX"): workspace.INBOX,
            (workspace, "OUTBOX"): workspace.OUTBOX, (workspace, "prepare"): workspace.prepare,
            (engine, "AGY_BIN"): engine.AGY_BIN, (engine, "TYPING_EVERY_S"): engine.TYPING_EVERY_S,
            (engine, "STATUS_EVERY_S"): engine.STATUS_EVERY_S, (engine, "_transcribe"): engine._transcribe,
        }
        store.STATE_FILE, store.TURNS_LOG = self.tmp / "state.json", self.tmp / "turns.log"
        workspace.WORKSPACE, workspace.INBOX, workspace.OUTBOX = self.tmp, self.tmp / "inbox", self.tmp / "out"
        workspace.prepare = lambda: {}
        engine.AGY_BIN = str(TESTS_DIR / "fake_agy.py")
        engine.TYPING_EVERY_S, engine.STATUS_EVERY_S = 0.2, 0.3
        engine._transcribe = lambda path: "transcription du vocal"
        for f in ("calls.jsonl", "mode.txt", "media.txt"):
            (TESTS_DIR / f).unlink(missing_ok=True)
        self.adapter = FakeAdapter()
        engine._rt.update(adapter=self.adapter, gateway=None, loop=asyncio.get_running_loop(), notice_sent=False)
        engine._queue.clear()
        engine._worker, engine._run = None, None

    async def asyncTearDown(self):
        if engine._worker and not engine._worker.done():
            engine.stop()
            await asyncio.wait_for(engine._worker, 15)
        for (module, name), value in self.saved.items():
            setattr(module, name, value)
        for f in ("calls.jsonl", "mode.txt", "media.txt"):
            (TESTS_DIR / f).unlink(missing_ok=True)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mode(self, mode):
        (TESTS_DIR / "mode.txt").write_text(mode)

    def calls(self):
        path = TESTS_DIR / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    async def dispatch(self, ev):
        return await plugin._on_dispatch(event=ev)

    async def finish(self, timeout=20):
        await asyncio.wait_for(engine._worker, timeout)


class RoutingTests(Base):
    def route(self, ev, **state):
        return plugin.route(ev, state)

    def test_trigger_words(self):
        job = self.route(event("Jet, relance le serveur"))[1]
        self.assertEqual(job.text, "relance le serveur")
        self.assertEqual(self.route(event("AGY: salut"))[1].text, "salut")
        self.assertEqual(self.route(event("jet"))[1].text, "")
        for text in ("Jette un œil", "jet-ski demain ?", "Le jet privé", "salut"):
            self.assertIsNone(self.route(event(text)), text)

    def test_custom_triggers(self):
        self.assertEqual(self.route(event("Ovni : fais ça"), triggers=["ovni"])[1].text, "fais ça")
        self.assertIsNone(self.route(event("jet fais ça"), triggers=["ovni"]))

    def test_commands(self):
        self.assertEqual(self.route(event("/agy liste les fichiers"))[1].text, "liste les fichiers")
        self.assertEqual(self.route(event("/agy@HermesBot liste"))[1].text, "liste")
        job = self.route(event("/agy_new on repart"))[1]
        self.assertTrue(job.new_conversation)
        self.assertIsNone(self.route(event("/agy")))  # status card: the command handler answers
        self.assertIsNone(self.route(event("/agy_model")))
        self.assertIsNone(self.route(event("/new")))

    def test_sticky_mode(self):
        self.assertEqual(self.route(event("salut"), sticky=True)[1].text, "salut")
        self.assertIsNone(self.route(event("Hermes, fais le point"), sticky=True))
        self.assertIsNone(self.route(event("Hermès : ça va ?"), sticky=True))
        self.assertIsNone(self.route(event("/status"), sticky=True))
        self.assertEqual(self.route(event("/new"), sticky=True), ("new",))
        self.assertIsNone(self.route(event("/stop"), sticky=True))  # nothing running: Hermes' /stop
        engine._run = object()
        self.assertEqual(self.route(event("/stop"), sticky=True), ("stop",))

    def test_reply_to_agy(self):
        state = {"agy_msgs": ["55"]}
        job = self.route(event("et maintenant ?", reply_to="55", reply_text="réponse d'agy"), **state)[1]
        self.assertEqual((job.text, job.quoted), ("et maintenant ?", ""))
        self.assertIsNone(self.route(event("et maintenant ?", reply_to="56"), **state))
        quoted = self.route(event("jet fais ça", reply_to="56", reply_text="le plan de Hermes"), **state)[1]
        self.assertEqual(quoted.quoted, "le plan de Hermes")

    def test_media_voice_and_inlined_documents(self):
        doc = self.tmp / "notes.txt"
        doc.write_text("ligne 1\nligne 2")
        text = f"[Content of notes.txt]:\nligne 1\nligne 2\n\njet résume\n[document 'notes.txt' saved at: {doc}]"
        job = self.route(event(text, media=[doc], types=["text/plain"], mtype="document"))[1]
        self.assertEqual((job.text, job.files), ("résume", [(str(doc), "text/plain")]))
        voice = self.route(event("", media=["/c/v.ogg"], types=["audio/ogg"], mtype="voice"), sticky=True)[1]
        self.assertEqual((voice.voice, voice.files), (["/c/v.ogg"], []))
        self.assertIsNone(self.route(event("", media=["/c/v.ogg"], mtype="voice")))
        photo = self.route(event("/agy", media=["/c/p.jpg"], types=["image/jpeg"], mtype="photo"))[1]
        self.assertEqual(photo.files, [("/c/p.jpg", "image/jpeg")])

    async def test_dispatch_ignores_other_chats(self):
        self.assertIsNone(await self.dispatch(event("jet salut", chat="42")))
        self.assertIsNone(engine._worker)

    async def test_nothing_routed_without_chat_id(self):
        engine.TARGET_CHAT = ""
        try:
            self.assertIsNone(await self.dispatch(event("jet salut", chat="")))
            self.assertIsNone(await self.dispatch(event("jet salut")))
        finally:
            engine.TARGET_CHAT = CHAT
        self.assertIsNone(engine._worker)


class TurnTests(Base):
    async def test_basic_turn(self):
        result = await self.dispatch(event("jet liste mes projets", msg_id="7"))
        self.assertEqual(result, {"action": "skip", "reason": "agy-chat"})
        await self.finish()
        reply = self.adapter.sent()[-1]
        self.assertIn("ECHO:", reply[1])
        self.assertIn("liste mes projets", reply[1])
        self.assertIn("Voir main.py.", reply[1])  # file:// link reduced to its label
        self.assertIn("— 🛸 agy", reply[1])
        self.assertNotIn("run_command", reply[1])  # discreet mode hides actions
        self.assertEqual(reply[2], "7")
        self.assertIn(("react", "7", "👀"), self.adapter.calls)
        self.assertEqual(self.adapter.calls[-1], ("react", "7", None))
        self.assertTrue(self.adapter.sent("typing"))
        state = store.load()
        self.assertTrue(state["conversation_id"])
        self.assertNotIn("inflight", state)
        self.assertTrue(store.is_agy_message(state, int(self.adapter.next_id)))
        call = self.calls()[0]
        self.assertIsNone(call["conversation"])
        self.assertIn("Context:", call["prompt"])  # first turn carries the rules pointer
        log = [json.loads(line) for line in store.TURNS_LOG.read_text().splitlines()]
        self.assertEqual((log[-1]["status"], log[-1]["tokens"]), ("SUCCESS", 54321))

        await self.dispatch(event("jet et ensuite ?", msg_id="8"))
        await self.finish()
        second = self.calls()[1]
        self.assertEqual(second["conversation"], state["conversation_id"])
        self.assertNotIn("Context:", second["prompt"])

    async def test_reply_threading_reaches_agy(self):
        await self.dispatch(event("jet salut", msg_id="7"))
        await self.finish()
        agy_msg = self.adapter.sent()[-1]
        agy_id = str(self.adapter.next_id)
        self.assertIsNotNone(agy_msg)
        result = await self.dispatch(event("précise", msg_id="9", reply_to=agy_id))
        self.assertEqual(result["action"], "skip")
        await self.finish()
        self.assertTrue(self.calls()[-1]["prompt"].endswith("précise"))

    async def test_queue_merges_messages(self):
        self.mode("verbose")
        await self.dispatch(event("jet tâche longue", msg_id="1"))
        await asyncio.sleep(0.5)
        await self.dispatch(event("jet et aussi ça", msg_id="2"))
        await self.dispatch(event("jet et ça", msg_id="3"))
        self.assertIn(("react", "2", "✍"), self.adapter.calls)
        self.mode("ok")
        await self.finish(40)
        prompts = [c["prompt"] for c in self.calls()]
        self.assertEqual(len(prompts), 2)
        self.assertIn("Several messages from Alex", prompts[1])
        self.assertIn("1. et aussi ça", prompts[1])
        self.assertIn("2. et ça", prompts[1])
        self.assertEqual(self.adapter.sent()[-1][2], "3")

    async def test_stop_kills_the_turn(self):
        self.mode("slow")
        await self.dispatch(event("jet attends", msg_id="1"))
        await asyncio.sleep(1.5)
        started = time.time()
        self.assertEqual(await plugin._cmd_stop(""), "🛑 Stop requested.")
        await self.finish()
        self.assertLess(time.time() - started, 8)
        self.assertIn("🛑 Stopped.", self.adapter.sent()[-1][1])
        self.assertEqual(await plugin._cmd_stop(""), "Nothing running.")

    async def test_stop_while_transcribing_never_starts_agy(self):
        store.update(sticky=True, voice="local")
        engine._transcribe = lambda path: time.sleep(1.5) or "vocal"
        await self.dispatch(event("", media=[self.tmp / "v.ogg"], types=["audio/ogg"], mtype="voice", msg_id="1"))
        await asyncio.sleep(0.5)
        self.assertEqual(await plugin._cmd_stop(""), "🛑 Stop requested.")
        await self.finish()
        self.assertEqual(self.calls(), [])
        self.assertIn("🛑 Stopped.", self.adapter.sent()[-1][1])

    async def test_launch_failure_cleans_up(self):
        engine.AGY_BIN = str(self.tmp / "missing-agy")
        await self.dispatch(event("jet salut", msg_id="7"))
        await self.finish()
        self.assertIn("❌ agy-chat", self.adapter.sent()[-1][1])
        self.assertNotIn("inflight", store.load())
        self.assertEqual(self.adapter.calls[-1], ("react", "7", None))
        self.assertIsNone(engine._run)

    async def test_saturated_conversation_restarts_fresh(self):
        store.update(conversation_id="old-conv")
        store.log_turn({"conversation_id": "old-conv", "message": "avant", "response": "réponse d'avant"})
        self.mode("saturated")
        await self.dispatch(event("jet continue", msg_id="1"))
        await self.finish()
        calls = self.calls()
        self.assertEqual([c["conversation"] for c in calls], ["old-conv", None])
        self.assertIn("réponse d'avant", calls[1]["prompt"])
        reply = self.adapter.sent()[-1][1]
        self.assertIn("♻️", reply)
        self.assertIn("ECHO:", reply)
        self.assertNotEqual(store.load()["conversation_id"], "old-conv")

    async def test_crash_reports_error_and_retry_works(self):
        self.mode("crash")
        await self.dispatch(event("jet plante", msg_id="1"))
        await self.finish()
        reply = self.adapter.sent()[-1][1]
        self.assertIn("❌", reply)
        self.assertIn("panic: something broke", reply)
        self.assertIn("/agy_retry", reply)
        self.mode("ok")
        self.assertEqual(await plugin._cmd_retry(""), "🔁 Sent to agy again.")
        await self.finish()
        self.assertIn("plante", self.calls()[-1]["prompt"])

    async def test_media_delivery(self):
        image, doc = self.tmp / "out.png", self.tmp / "rapport.pdf"
        image.write_bytes(b"png")
        doc.write_bytes(b"pdf")
        (TESTS_DIR / "media.txt").write_text(
            f"MEDIA:{image}\nMEDIA:{doc}\nMEDIA:{self.tmp}/absent.png\nMEDIA:{Path.home()}/.hermes/.env")
        self.mode("media")
        await self.dispatch(event("jet envoie", msg_id="4"))
        await self.finish()
        self.assertEqual(self.adapter.sent("image"), [("image", str(image), "4")])
        self.assertEqual(self.adapter.sent("document"), [("document", str(doc), "4")])
        warnings = [c[1] for c in self.adapter.sent() if c[1].startswith("⚠️")]
        self.assertEqual(len(warnings), 1, warnings)
        self.assertIn("absent.png", warnings[0])
        # Hermes never extracts an extension-less path it refuses: the line stays as text, nothing is sent.
        self.assertIn(".hermes/.env", self.adapter.sent()[0][1])
        self.assertNotIn(f"MEDIA:{image}", self.adapter.sent()[0][1])

    async def test_verbose_status_and_summary(self):
        store.update(verbose=True)
        self.mode("verbose")
        await self.dispatch(event("jet build", msg_id="5"))
        await self.finish(30)
        status = self.adapter.sent()[0]
        self.assertTrue(status[1].startswith("🛸 agy working"))
        self.assertIn("▶ run_command ls ~/projects", status[1])
        self.assertIn("💬 Je regarde.", status[1])
        final_edit = [c for c in self.adapter.calls if c[0] == "edit" and c[3]][-1]
        self.assertIn("🔧 1 actions", final_edit[2])
        self.assertIn("54k tokens", final_edit[2])
        self.assertIn("gemini-3.8-flash-high", self.adapter.sent()[-1][1])

    async def test_long_reply_goes_to_file(self):
        self.mode("long")
        await self.dispatch(event("jet long", msg_id="6"))
        await self.finish()
        text = self.adapter.sent()[-1][1]
        self.assertIn("[…continued in the attached file]", text)
        self.assertLess(len(text), engine.MAX_REPLY_CHARS + 200)
        attached = self.adapter.sent("document")[0][1]
        self.assertGreater(len(Path(attached).read_text()), 15000)

    async def test_voice_note_in_sticky_mode(self):
        store.update(sticky=True)
        voice = self.tmp / "v.ogg"
        voice.write_bytes(b"ogg")
        await self.dispatch(event("", media=[voice], types=["audio/ogg"], mtype="voice", msg_id="3"))
        await self.finish()
        prompt = self.calls()[0]["prompt"]
        self.assertIn("view_file", prompt)
        self.assertIn("audio/ogg", prompt)

    async def test_voice_note_local_mode(self):
        store.update(sticky=True, voice="local")
        voice = self.tmp / "v.ogg"
        voice.write_bytes(b"ogg")
        await self.dispatch(event("", media=[voice], types=["audio/ogg"], mtype="voice", msg_id="4"))
        await self.finish()
        self.assertIn("transcription du vocal", self.calls()[0]["prompt"])

    async def test_command_voice(self):
        self.assertIn("direct", await plugin._cmd_voice("direct"))
        self.assertEqual(store.voice_mode(store.load()), "direct")
        self.assertIn("local", await plugin._cmd_voice("local"))
        self.assertEqual(store.voice_mode(store.load()), "local")

    async def test_attachments_are_copied_to_inbox(self):
        photo = self.tmp / "photo.jpg"
        photo.write_bytes(b"jpg")
        await self.dispatch(event("jet c'est quoi ?", media=[photo], types=["image/jpeg"], mtype="photo"))
        await self.finish()
        prompt = self.calls()[0]["prompt"]
        self.assertIn("Files attached by Alex", prompt)
        copied = [p for p in workspace.INBOX.iterdir()]
        self.assertEqual(len(copied), 1)
        self.assertIn(str(copied[0]), prompt)

    async def test_huge_message_goes_through_a_file(self):
        await self.dispatch(event("jet " + "a" * 150_000))
        await self.finish()
        prompt = self.calls()[0]["prompt"]
        self.assertIn("too long for the command line", prompt)
        self.assertLess(len(prompt), 1000)

    async def test_restart_notice(self):
        store.update(inflight={"message": "relance best-stuff", "at": "2026-10-06T21:40:00"})
        engine.schedule_startup(delay=0.1)
        await asyncio.sleep(0.5)
        notice = self.adapter.sent()[-1][1]
        self.assertIn("relance best-stuff", notice)
        self.assertIn("/agy_retry", notice)
        self.assertNotIn("inflight", store.load())


class CommandTests(Base):
    async def test_status_card_and_modes(self):
        card = await plugin._cmd_agy("")
        self.assertIn("Hermes mode", card)
        self.assertIn("jet", card)
        self.assertIsNone(await plugin._cmd_on(""))
        self.assertTrue(store.load()["sticky"])
        _kind, banner, _markup = self.adapter.sent("bot_send")[-1]
        self.assertTrue(banner.startswith("🛸 agy mode on"))
        pin_id = store.load()["mode_pin"]
        self.assertEqual(self.adapter.sent("pin"), [("pin", pin_id, True)])
        self.assertEqual(await plugin._cmd_on(""), "🛸 agy mode already on.")
        self.assertEqual(len(self.adapter.sent("pin")), 1)
        self.assertIn("agy mode:", await plugin._cmd_agy(""))
        self.assertIn("Back to Hermes", await plugin._cmd_off(""))
        self.assertNotIn("sticky", store.load())
        self.assertNotIn("mode_pin", store.load())
        self.assertEqual((self.adapter.sent("unpin"), self.adapter.sent("delete")),
                         ([("unpin", pin_id)], [("delete", pin_id)]))

    async def test_banner_follows_state_at_startup(self):
        store.update(sticky=True)  # mode on, no banner yet (e.g. state from an older version)
        engine.schedule_startup(delay=0.1)
        await asyncio.sleep(0.5)
        self.assertTrue(store.load()["mode_pin"])
        self.assertEqual(len(self.adapter.sent("pin")), 1)

    async def test_model_picker(self):
        plugin._models_cache.update(at=0.0, models=[])
        self.assertIsNone(await plugin._cmd_model(""))
        title, choices = self.adapter.sent("picker")[0][1:]
        self.assertEqual([c["value"] for c in choices], ["gemini-3.8-flash-high", "claude-opus-4-6-thinking"])
        self.assertEqual(await self.adapter.on_choice(CHAT, "claude-opus-4-6-thinking"),
                         "agy model → claude-opus-4-6-thinking")
        self.assertEqual(store.load()["model"], "claude-opus-4-6-thinking")
        self.assertEqual(await plugin._cmd_model("1"), "agy model → gemini-3.8-flash-high")
        self.assertIn("Unknown", await plugin._cmd_model("gpt-9"))

    async def test_verbose_and_words(self):
        self.assertIn("Detailed", await plugin._cmd_verbose(""))
        self.assertIn("Discreet", await plugin._cmd_verbose(""))
        self.assertIn("ovni, jet", await plugin._cmd_words("Ovni jet"))
        self.assertEqual(store.triggers(store.load()), ["ovni", "jet"])
        self.assertIn("Invalid", await plugin._cmd_words("j3t"))
        await plugin._cmd_words("reset")
        self.assertEqual(store.triggers(store.load()), ["jet", "agy"])

    async def test_conversations(self):
        self.assertEqual(await plugin._cmd_conv(""), "No agy conversation recorded.")
        store.log_turn({"conversation_id": "c1", "message": "premier sujet"})
        store.log_turn({"conversation_id": "c2", "message": "second sujet"})
        self.assertIsNone(await plugin._cmd_conv(""))
        choices = self.adapter.sent("picker")[0][2]
        self.assertEqual([c["value"] for c in choices], ["c2", "c1"])
        self.assertIn("premier sujet", await plugin._cmd_conv("2"))
        self.assertEqual(store.load()["conversation_id"], "c1")


if __name__ == "__main__":
    unittest.main(verbosity=1)
