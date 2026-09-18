"""JustWatch非公式APIの実データ確認用スクリプト（GitHub Actions上で手動実行）。

Discordには一切送信せず、標準出力にのみ結果を出す。
本実装（justwatch_client.py）のクエリ設計を確定させるための調査用ツール。
実装完了後もJustWatch側の仕様変更を疑ったときの切り分けに使えるので残す。
"""

import json

import httpx
from simplejustwatchapi.justwatch import popular, providers

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


def probe_sort_by_release_year(short_name: str) -> None:
    section("popularTitles(sortBy: RELEASE_YEAR, sortOrder: DESC) の動作確認")
    query = """
    query ProbeSortedPopular(
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
            sortOrder: DESC
        ) {
            edges {
                node {
                    objectId
                    objectType
                    content(country: $country, language: $language) {
                        title
                        originalReleaseDate
                        fullPath
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
    result = raw_graphql("ProbeSortedPopular", query, variables)
    print(json.dumps(result, ensure_ascii=False, indent=2)[:4000])


if __name__ == "__main__":
    probe_providers()
    probe_popular_baseline("nfx")
    probe_new_titles_field("nfx")
    probe_sort_by_release_year("nfx")
