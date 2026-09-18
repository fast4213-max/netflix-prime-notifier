"""JustWatch非公式APIの実データ確認用スクリプト（GitHub Actions上で手動実行）。

Discordには一切送信せず、標準出力にのみ結果を出す。
本実装（justwatch_client.py）のクエリ設計を確定させるための調査用ツール。
実装完了後もJustWatch側の仕様変更を疑ったときの切り分けに使えるので残す。
"""

import json
import sys
from pathlib import Path

import httpx
from simplejustwatchapi.justwatch import popular, providers

sys.path.insert(0, str(Path(__file__).parent.parent))

COUNTRY = "JP"
LANGUAGE = "ja"
GRAPHQL_URL = "https://apis.justwatch.com/graphql"


def section(title: str) -> None:
    print(f"\n{'=' * 10} {title} {'=' * 10}")


def probe_providers() -> None:
    section("providers(JP) からNetflix/Prime Videoの短縮名を特定")
    for p in providers(country=COUNTRY):
        if "netflix" in p.technical_name.lower() or "prime" in p.technical_name.lower() or "amazon" in p.technical_name.lower():
            print(
                f"name={p.name!r} technical_name={p.technical_name!r} "
                f"short_name={p.short_name!r} monetization_types={p.monetization_types}"
            )


def probe_popular_baseline(short_name: str) -> None:
    section(f"popular(providers={short_name!r}) の基本動作確認")
    try:
        entries = popular(
            country=COUNTRY, language=LANGUAGE, count=5, providers=[short_name]
        )
        for e in entries:
            print(f"- {e.title} ({e.release_year}) offers={len(e.offers)}件")
            for o in e.offers[:3]:
                print(
                    f"    monetization_type={o.monetization_type} "
                    f"package.short_name={o.package.short_name}"
                )
    except Exception as ex:  # noqa: BLE001 調査用スクリプトなので広めにキャッチ
        print(f"ERROR: {ex}")


def raw_graphql(operation_name: str, query: str, variables: dict) -> dict:
    body = {"operationName": operation_name, "variables": variables, "query": query}
    resp = httpx.post(GRAPHQL_URL, json=body, timeout=30)
    print(f"HTTP {resp.status_code}")
    try:
        return resp.json()
    except Exception:  # noqa: BLE001
        print(resp.text[:2000])
        return {}


def probe_new_titles_field(short_name: str) -> None:
    section("Query.newTitles フィールドの存在確認（存在しなければエラーで判明する）")
    query = """
    query ProbeNewTitles($country: Country!, $first: Int!) {
        newTitles(country: $country, first: $first) {
            edges {
                node {
                    objectId
                    objectType
                }
            }
        }
    }
    """
    result = raw_graphql(
        "ProbeNewTitles", query, {"country": COUNTRY, "first": 3}
    )
    print(json.dumps(result, ensure_ascii=False, indent=2)[:3000])


def probe_new_titles_with_filter(short_name: str) -> None:
    section(f"newTitles(filter: TitleFilter{{packages: [{short_name!r}]}}) の動作確認")
    query = """
    query ProbeNewTitlesFiltered(
        $country: Country!,
        $first: Int!,
        $filter: TitleFilter,
        $language: Language!,
        $formatPoster: ImageFormat,
        $profile: PosterProfile
    ) {
        newTitles(country: $country, first: $first, filter: $filter) {
            edges {
                node {
                    id
                    objectId
                    objectType
                    content(country: $country, language: $language) {
                        title
                        fullPath
                        originalReleaseDate
                        posterUrl(profile: $profile, format: $formatPoster)
                    }
                    offers(country: $country, platform: WEB) {
                        monetizationType
                        package {
                            shortName
                        }
                    }
                }
            }
        }
    }
    """
    variables = {
        "country": COUNTRY,
        "first": 10,
        "language": LANGUAGE,
        "formatPoster": "JPG",
        "profile": "S718",
        "filter": {"packages": [short_name], "objectTypes": ["MOVIE", "SHOW"]},
    }
    result = raw_graphql("ProbeNewTitlesFiltered", query, variables)
    print(json.dumps(result, ensure_ascii=False, indent=2)[:5000])


def probe_new_titles_bogus_argument() -> None:
    section("newTitles に存在しない引数を渡してエラーメッセージから仕様を推測")
    query = """
    query ProbeNewTitlesArgs($country: Country!) {
        newTitles(country: $country, bogusArgument: 1) {
            edges { node { objectId } }
        }
    }
    """
    result = raw_graphql("ProbeNewTitlesArgs", query, {"country": COUNTRY})
    print(json.dumps(result, ensure_ascii=False, indent=2)[:2000])


def probe_sort_by_enum_values() -> None:
    section("popularTitles の sortBy に不正な値を渡して列挙値のヒントを得る")
    query = """
    query ProbeSortByEnum($country: Country!, $first: Int!) {
        popularTitles(country: $country, first: $first, sortBy: BOGUS_SORT_VALUE) {
            edges { node { objectId } }
        }
    }
    """
    result = raw_graphql(
        "ProbeSortByEnum", query, {"country": COUNTRY, "first": 3}
    )
    print(json.dumps(result, ensure_ascii=False, indent=2)[:2000])


def probe_sort_by_release_year_order(short_name: str) -> None:
    section("popularTitles(sortBy: RELEASE_YEAR) の並び順（昇順/降順）を確認")
    query = """
    query ProbeReleaseYearOrder(
        $country: Country!,
        $first: Int!,
        $filter: TitleFilter,
        $language: Language!
    ) {
        popularTitles(
            country: $country
            filter: $filter
            first: $first
            sortBy: RELEASE_YEAR
        ) {
            edges {
                node {
                    content(country: $country, language: $language) {
                        title
                        originalReleaseDate
                    }
                }
            }
        }
    }
    """
    variables = {
        "country": COUNTRY,
        "first": 10,
        "language": LANGUAGE,
        "filter": {"packages": [short_name], "objectTypes": ["MOVIE", "SHOW"]},
    }
    result = raw_graphql("ProbeReleaseYearOrder", query, variables)
    print(json.dumps(result, ensure_ascii=False, indent=2)[:4000])


def probe_production_client() -> None:
    section("本番コード justwatch_client.fetch_new_titles() の動作確認")
    from justwatch_client import fetch_new_titles

    for short_name in ("nfx", "amp"):
        titles = fetch_new_titles(
            provider_short_name=short_name,
            count=5,
            country=COUNTRY,
            language=LANGUAGE,
            object_types=["MOVIE", "SHOW"],
        )
        print(f"-- {short_name} --")
        for t in titles:
            print(f"  {t.id} {t.title!r} poster={t.poster_url}")
            for o in t.offers:
                print(f"      {o.package_short_name} / {o.monetization_type}")


if __name__ == "__main__":
    probe_providers()
    probe_popular_baseline("nfx")
    probe_new_titles_field("nfx")
    probe_new_titles_with_filter("nfx")
    probe_new_titles_with_filter("amp")
    probe_new_titles_bogus_argument()
    probe_sort_by_enum_values()
    probe_production_client()
