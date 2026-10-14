"""Offline tests: parsers, formatting and the state machine. Run: python3 -m unittest -v"""

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIX = Path(__file__).resolve().parent / "fixtures"
sys.path.insert(0, str(ROOT))

import any2fedi as a2f  # noqa: E402


def tg(cfg=None):
    return a2f.TelegramSource({"channel": "testchan", **(cfg or {})}, None)


class TelegramParserTest(unittest.TestCase):
    def setUp(self):
        self.page = (FIX / "telegram.html").read_text(encoding="utf-8")

    def by_id(self, items):
        return {i.uid: i for i in items}

    def test_default_options(self):
        items = self.by_id(tg().parse(self.page))
        # service message, sticker, bare video and bare "open in Telegram" are dropped
        self.assertEqual(sorted(items), ["100", "101", "102", "103", "105", "109"])

    def test_formatting(self):
        text = self.by_id(tg().parse(self.page))["101"].text
        self.assertEqual(text, "**Breaking:** road closed\n"
                               "Details [here](https://example.com/a) #city *italic*")

    def test_album(self):
        it = self.by_id(tg().parse(self.page))["102"]
        self.assertEqual(len(it.media), 3)
        self.assertEqual(it.url, "https://t.me/testchan/102")

    def test_video_thumb_and_flag(self):
        it = self.by_id(tg().parse(self.page))["103"]
        self.assertEqual(it.media, ["https://cdn.example/v103.jpg"])
        self.assertTrue(it.has_unsupported_media)

    def test_forwarded(self):
        self.assertEqual(self.by_id(tg().parse(self.page))["105"].forwarded_from, "Other Channel")

    def test_options_off(self):
        items = self.by_id(tg({"video_thumbs": False, "skip_textless_video": False}).parse(self.page))
        self.assertIn("104", items)
        self.assertIn("108", items)
        self.assertEqual(items["103"].media, [])

    def test_channel_name_forms(self):
        for raw in ("@testchan", "https://t.me/testchan", "t.me/s/testchan/"):
            self.assertEqual(a2f.TelegramSource({"channel": raw}, None).channel, "testchan")


class FeedParserTest(unittest.TestCase):
    def test_rss(self):
        src = a2f.RSSSource({"url": "https://example.com/rss"}, None)
        items = src.parse((FIX / "feed.rss").read_bytes())
        self.assertEqual([i.uid for i in items], ["ex-1", "ex-2"])          # oldest first
        self.assertEqual(items[0].media, ["https://example.com/m.jpg"])
        self.assertEqual(items[1].text, "**Second**\n\nBody with [link](https://example.com/x)")
        self.assertEqual(items[1].media, ["https://example.com/i.jpg"])
        self.assertEqual(src.label, "example.com")

    def test_atom(self):
        src = a2f.RSSSource({"url": "https://example.org/atom"}, None)
        (it,) = src.parse((FIX / "feed.atom").read_bytes())
        self.assertEqual((it.uid, it.url), ("urn:a1", "https://example.org/a1"))
        self.assertEqual(it.text, "**Atom entry**\n\nHello **world**")

    def test_title_only_and_custom_title(self):
        src = a2f.RSSSource({"url": "https://example.com/rss", "title_only": True}, None)
        src.texts = a2f.merged_texts({"rss_title": "📌 {title}"})
        self.assertEqual(src.parse((FIX / "feed.rss").read_bytes())[1].text, "📌 Second")


class RenderTest(unittest.TestCase):
    def src(self, texts=None, **cfg):
        s = a2f.TelegramSource({"channel": "testchan", "label": "Test", **cfg}, None)
        s.texts = a2f.merged_texts(texts)
        return s

    def test_default_layout(self):
        it = a2f.Item(uid="1", url="https://t.me/testchan/1", text="Hello",
                      has_unsupported_media=True, forwarded_from="Other")
        self.assertEqual(a2f.render(it, self.src(), {"hashtags": ["#a"]}),
                         "Hello\n\n↪ forwarded from: Other\n\n"
                         "🎬 video / attachment — see the original\n\n"
                         "— Test: https://t.me/testchan/1\n\n#a")

    def test_empty_blocks_collapse(self):
        it = a2f.Item(uid="1", url="u", text="Hello")
        self.assertEqual(a2f.render(it, self.src(), {}), "Hello\n\n— Test: u")

    def test_texts_override_and_source_hashtags(self):
        it = a2f.Item(uid="1", url="u", text="Привет", forwarded_from="Канал")
        s = self.src({"forwarded": "переслано из {name}", "footer": "Источник: {source} {url}",
                      "template": "{footer}\n\n{text}\n\n{forwarded}\n\n{hashtags}"},
                     hashtags=["#x"])
        self.assertEqual(a2f.render(it, s, {"hashtags": ["#ignored"]}),
                         "Источник: Test u\n\nПривет\n\nпереслано из Канал\n\n#x")

    def test_truncation_keeps_footer(self):
        it = a2f.Item(uid="1", url="https://t.me/testchan/1", text="x" * 1000)
        out = a2f.render(it, self.src({"truncated": "[…]"}), {"max_chars": 200})
        self.assertLessEqual(len(out), 200)
        self.assertTrue(out.endswith("— Test: https://t.me/testchan/1"))
        self.assertIn("[…]", out)

    def test_texts_layering(self):
        t = a2f.merged_texts({"footer": "g"}, {"footer": "a", "truncated": "!"}, {"footer": "s"})
        self.assertEqual((t["footer"], t["truncated"]), ("s", "!"))
        self.assertEqual(t["forwarded"], a2f.DEFAULT_TEXTS["forwarded"])


class FakeFedi:
    posts = []

    def __init__(self, *a, **kw):
        pass

    def upload(self, url, max_bytes):
        return "m:" + url

    def post(self, text, media_ids, visibility, language, idem):
        FakeFedi.posts.append(text.splitlines()[0])
        return f"s{len(FakeFedi.posts)}"


class RunnerTest(unittest.TestCase):
    def setUp(self):
        FakeFedi.posts = []
        self.feeds = {}
        feeds = self.feeds

        class FakeTG(a2f.TelegramSource):
            def fetch(self):
                return list(feeds[self.channel])

        self._orig = a2f.SOURCE_TYPES["telegram"]
        a2f.SOURCE_TYPES["telegram"] = FakeTG
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = {
            "instance": "https://x", "state_db": f"{self.tmp.name}/s.db",
            "post_delay_seconds": 0,
            "accounts": {"acc": {"token": "t", "backfill": 1, "sources": [
                {"channel": "one"}, {"channel": "two"}]}},
        }

    def tearDown(self):
        a2f.SOURCE_TYPES["telegram"] = self._orig
        self.tmp.cleanup()

    @staticmethod
    def items(*ids):
        return [a2f.Item(uid=str(i), url=f"u{i}", text=f"post {i}", sort_key=(i,)) for i in ids]

    def run_once(self):
        FakeFedi.posts = []
        a2f.run(self.cfg, fedi_factory=FakeFedi)
        return FakeFedi.posts

    def test_lifecycle(self):
        self.feeds.update(one=self.items(1, 2, 3), two=self.items(50))
        self.assertEqual(self.run_once(), ["post 3", "post 50"])     # backfill=1 per source
        self.assertEqual(self.run_once(), [])                        # nothing new
        self.feeds["one"] += self.items(4, 5)
        self.feeds["two"] += self.items(51)
        self.assertEqual(self.run_once(), ["post 4", "post 5", "post 51"])

    def test_new_source_added_later(self):
        self.cfg["accounts"]["acc"]["backfill"] = 0
        self.feeds.update(one=self.items(1), two=self.items(9))
        self.assertEqual(self.run_once(), [])
        self.cfg["accounts"]["acc"]["sources"].append({"channel": "three"})
        self.feeds["three"] = self.items(70, 71)
        self.assertEqual(self.run_once(), [])                        # history not dumped
        self.feeds["three"] += self.items(72)
        self.assertEqual(self.run_once(), ["post 72"])

    def test_disabled_account(self):
        self.cfg["accounts"]["acc"]["enabled"] = False
        self.feeds.update(one=self.items(1), two=self.items(2))
        self.assertEqual(self.run_once(), [])


if __name__ == "__main__":
    unittest.main()
