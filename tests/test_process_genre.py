"""main.process_genre の結合テスト（通信なし。取得とDiscord送信は差し替える）。"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import main  # noqa: E402
import queue_runner  # noqa: E402
import state_manager  # noqa: E402
from animephilia_client import CalendarEvent  # noqa: E402

CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
CONFIG = {**CONFIG, "send_interval_seconds": 0}


def ev(i, title, kind, region, genres):
    return CalendarEvent(
        id=f"https://www.netflix.com/title/{i}", title=title, start_date="2026-11-16",
        url=f"https://www.netflix.com/title/{i}", image_url="https://img/x.jpg",
        kind=kind, region=region, genres=tuple(genres),
    )


EVENTS = [
    ev(1, "洋画A", "movie", "overseas", ["SF映画"]),
    ev(2, "邦画B", "movie", "domestic", ["国内"]),
    ev(3, "ドラマC", "tv", "domestic", ["TVヒューマンドラマ"]),
    ev(4, "海外ドラマD", "tv", "overseas", ["犯罪TV番組・ドラマ"]),
    ev(5, "アニメE", "tv", "domestic", ["TVアニメ"]),       # アニメ表記 → 除外
    ev(6, "アニメF", "tv", "domestic", ["国内"]),            # アニメ版に載っている → 除外
    ev(7, "バラエティG", "tv", "domestic", ["バラエティ番組"]),
]
ENVS = {
    "DISCORD_WEBHOOK_NETFLIX_MOVIE_FOREIGN": "https://hook/movie_foreign",
    "DISCORD_WEBHOOK_NETFLIX_MOVIE_JP": "https://hook/movie_jp",
    "DISCORD_WEBHOOK_NETFLIX_DRAMA_JP": "https://hook/drama_jp",
    "DISCORD_WEBHOOK_NETFLIX_DRAMA_FOREIGN": "https://hook/drama_foreign",
    # variety / documentary / other は未設定
}


class ProcessGenre(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        patches = [
            mock.patch.object(state_manager, "STATE_DIR", Path(self.tmp.name)),
            mock.patch.dict(os.environ, ENVS, clear=False),
            mock.patch.object(main, "fetch_recent_events", lambda p, scope="anime": list(EVENTS)),
        ]
        self.sent: list[tuple[str, list]] = []
        patches.append(
            mock.patch.object(
                queue_runner, "send_title_embeds", lambda url, items: self.sent.append((url, items))
            )
        )
        for p in patches:
            p.start()
            self.addCleanup(p.stop)
        self.addCleanup(self.tmp.cleanup)

    def run_once(self, anime_ids):
        return main.process_genre("netflix", CONFIG, anime_ids)

    def test_routes_to_channels_and_does_not_resend(self):
        errors = self.run_once({"https://www.netflix.com/title/6"})
        self.assertEqual(errors, [])
        by_url = {url: [t[0] for t in items] for url, items in self.sent}
        self.assertEqual(by_url["https://hook/movie_foreign"], ["洋画A"])
        self.assertEqual(by_url["https://hook/movie_jp"], ["邦画B"])
        self.assertEqual(by_url["https://hook/drama_jp"], ["ドラマC"])
        self.assertEqual(by_url["https://hook/drama_foreign"], ["海外ドラマD"])
        self.assertNotIn("アニメE", str(self.sent))
        self.assertNotIn("アニメF", str(self.sent))
        # Webhook未設定のバラエティは送らず、既読にもしない（設定後に通知できる）。
        active = state_manager.load_active("netflix_genre")
        self.assertNotIn("https://www.netflix.com/title/7", active)
        self.assertEqual(len(active), 4)
        # 2回目は何も送らない。
        self.sent.clear()
        self.run_once({"https://www.netflix.com/title/6"})
        self.assertEqual(self.sent, [])

    def test_webhook_added_later_notifies_held_items(self):
        self.run_once({"https://www.netflix.com/title/6"})
        self.sent.clear()
        with mock.patch.dict(os.environ, {"DISCORD_WEBHOOK_NETFLIX_VARIETY": "https://hook/variety"}):
            self.run_once({"https://www.netflix.com/title/6"})
        self.assertEqual([(u, [t[0] for t in i]) for u, i in self.sent], [("https://hook/variety", ["バラエティG"])])

    def test_skips_when_anime_ids_unknown(self):
        self.assertEqual(self.run_once(None), [])
        self.assertEqual(self.sent, [])

    def test_failed_send_keeps_queue_for_next_run(self):
        from notifier import WebhookUnusable

        def boom(url, items):
            raise WebhookUnusable(404, "gone")

        with mock.patch.object(queue_runner, "send_title_embeds", boom):
            errors = self.run_once({"https://www.netflix.com/title/6"})
        self.assertTrue(errors)
        self.assertEqual(len(state_manager.load_queue("netflix_genre")), 4)
        self.sent.clear()
        self.assertEqual(self.run_once({"https://www.netflix.com/title/6"}), [])
        self.assertEqual(len(self.sent), 4)
        self.assertEqual(state_manager.load_queue("netflix_genre"), [])

    def test_same_title_other_variant_later_is_suppressed(self):
        """字幕版→吹替版が別の日に別URLで載っても、同じチャンネルへ再通知しない。"""
        first = [ev(10, "ロッキー (字幕版)", "movie", "overseas", ["ドラマ"])]
        second = first + [ev(11, "ロッキー (吹替版)", "movie", "overseas", ["ドラマ"])]
        with mock.patch.object(main, "fetch_recent_events", lambda p, scope="anime": list(first)):
            self.run_once(set())
        self.sent.clear()
        with mock.patch.object(main, "fetch_recent_events", lambda p, scope="anime": list(second)):
            self.run_once(set())
        self.assertEqual(self.sent, [])
        # 吹替版は既読にして、毎時の再判定で蒸し返さない。
        self.assertIn("https://www.netflix.com/title/11", state_manager.load_active("netflix_genre"))

    def test_same_title_different_channel_is_not_suppressed(self):
        movie = ev(20, "キャリー", "movie", "overseas", ["ホラー"])
        series = ev(21, "キャリー", "tv", "overseas", ["TVヒューマンドラマ"])
        with mock.patch.object(main, "fetch_recent_events", lambda p, scope="anime": [movie]):
            self.run_once(set())
        self.sent.clear()
        with mock.patch.object(main, "fetch_recent_events", lambda p, scope="anime": [movie, series]):
            self.run_once(set())
        self.assertEqual([[t[0] for t in i] for _, i in self.sent], [["キャリー"]])
        self.assertEqual(self.sent[0][0], "https://hook/drama_foreign")

    def test_known_titles_are_remembered_after_init_read(self):
        # 初回既読化で登録済み（通知していない）の作品の吹替版が後から載っても通知しない。
        base = ev(30, "サメ (字幕版)", "movie", "overseas", ["アクション"])
        state_manager.save_active("netflix_genre", {base.id: "2026-11-22T00:00:00+00:00"})
        with mock.patch.object(main, "fetch_recent_events", lambda p, scope="anime": [base]):
            self.run_once(set())
        later = ev(31, "サメ (吹替版)", "movie", "overseas", ["アクション"])
        with mock.patch.object(main, "fetch_recent_events", lambda p, scope="anime": [base, later]):
            self.run_once(set())
        self.assertEqual(self.sent, [])


if __name__ == "__main__":
    unittest.main()
