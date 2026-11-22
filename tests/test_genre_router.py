"""ジャンル振り分けの検証。実際に取得した全ジャンル版カレンダー（2026-11）の見本で、
アニメ以外の全件が漏れなく1チャンネルに入ることを確認する。

実行: python -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import sys
import unittest
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import genre_router  # noqa: E402
from animephilia_client import CalendarEvent  # noqa: E402

CONFIG = json.loads((ROOT / "config.json").read_text(encoding="utf-8"))
GENRE_CFG = CONFIG["genre"]
SAMPLE = json.loads((ROOT / "tests/fixtures/all_calendar_sample.json").read_text(encoding="utf-8"))
CHANNEL_KEYS = [c["key"] for c in GENRE_CFG["channels"]]


def event(title="t", kind=None, region=None, genres=()):
    return CalendarEvent(
        id=title, title=title, start_date="2026-11-16", url=None, image_url=None,
        kind=kind, region=region, genres=tuple(genres),
    )


def to_events(provider: str) -> list[tuple[CalendarEvent, bool]]:
    """(イベント, アニメ版ページに載っているか)"""
    anime = set(SAMPLE[provider]["anime_urls"])
    result = []
    for i in SAMPLE[provider]["all"]:
        key = i["url"] or i["title"]
        ev = CalendarEvent(
            id=key, title=i["title"], start_date=i["start"], url=i["url"], image_url=None,
            kind=i["kind"], region=i["region"], genres=tuple(i["genres"]),
        )
        result.append((ev, key in anime))
    return result


class ClassifyRules(unittest.TestCase):
    def check(self, ev, expected):
        self.assertEqual(genre_router.classify(ev, GENRE_CFG), expected)

    def test_movie_by_region(self):
        self.check(event(kind="movie", region="domestic", genres=["ドラマ"]), "movie_jp")
        self.check(event(kind="movie", region="overseas", genres=["SF映画"]), "movie_foreign")

    def test_tv_drama_by_region(self):
        self.check(event(kind="tv", region="domestic", genres=["サスペンス"]), "drama_jp")
        self.check(event(kind="tv", region="overseas", genres=["TVヒューマンドラマ"]), "drama_foreign")

    def test_documentary_and_variety_win_over_kind(self):
        self.check(event(kind="movie", region="overseas", genres=["ドキュメンタリー"]), "documentary")
        self.check(event(kind="tv", region="domestic", genres=["国内", "バラエティ番組"]), "variety")
        self.check(event(kind="tv", region="overseas", genres=["リアリティショー"]), "variety")
        self.check(event(kind="tv", region="domestic", genres=["トークショー・バラエティ"]), "variety")

    def test_tv_without_drama_genre_goes_to_other(self):
        self.check(event(kind="tv", region="domestic", genres=["スポーツ"]), "other")
        self.check(event(kind="tv", region="domestic", genres=["国内"]), "other")

    def test_unknown_kind_or_region_never_dropped(self):
        self.check(event(kind=None, region=None, genres=[]), "other")
        self.check(event(kind="live", region="domestic", genres=["ドラマ"]), "other")

    def test_is_anime(self):
        self.assertTrue(genre_router.is_anime(event(genres=["恋愛アニメ"]), GENRE_CFG))
        self.assertTrue(genre_router.is_anime(event(genres=["アニメ", "キッズ"]), GENRE_CFG))
        self.assertFalse(genre_router.is_anime(event(genres=["ドラマ"]), GENRE_CFG))

    def test_webhook_env_names(self):
        self.assertEqual(
            genre_router.webhook_env_name(GENRE_CFG, "netflix", "movie_jp"),
            "DISCORD_WEBHOOK_NETFLIX_MOVIE_JP",
        )
        self.assertEqual(
            genre_router.webhook_env_name(GENRE_CFG, "prime_video", "drama_foreign"),
            "DISCORD_WEBHOOK_PRIME_DRAMA_FOREIGN",
        )


class RealSample(unittest.TestCase):
    def test_every_non_anime_lands_in_exactly_one_known_channel(self):
        for provider in SAMPLE:
            rest = [
                ev
                for ev, in_anime_page in to_events(provider)
                if not in_anime_page and not genre_router.is_anime(ev, GENRE_CFG)
            ]
            self.assertGreater(len(rest), 0, provider)
            counts = Counter(genre_router.classify(ev, GENRE_CFG) for ev in rest)
            self.assertEqual(sum(counts.values()), len(rest), provider)
            self.assertLessEqual(set(counts), set(CHANNEL_KEYS), provider)
            print(provider, len(rest), dict(counts))

    def test_all_page_anime_is_subset_of_anime_page(self):
        # アニメ表記なのにアニメ版に載っていない作品が無い（=二重通知・取りこぼしが無い）ことの確認。
        for provider in SAMPLE:
            stray = [
                ev.title
                for ev, in_anime_page in to_events(provider)
                if genre_router.is_anime(ev, GENRE_CFG) and not in_anime_page
            ]
            self.assertEqual(stray, [], provider)

    def test_expected_distribution_netflix(self):
        rest = [ev for ev, a in to_events("netflix") if not a and not genre_router.is_anime(ev, GENRE_CFG)]
        counts = Counter(genre_router.classify(ev, GENRE_CFG) for ev in rest)
        self.assertEqual(counts["movie_foreign"], 13)
        self.assertEqual(counts["variety"], 4)
        self.assertEqual(counts["documentary"], 1)


if __name__ == "__main__":
    unittest.main()
