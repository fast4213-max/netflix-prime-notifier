"""JustWatch非公式GraphQL APIから「新着」タイトルを取得する薄いクライアント。

`newTitles` フィールドはJustWatch公式には非公開・無告知で変更されうる。
実際に動作することは `scripts/debug_justwatch.py` をGitHub Actions上で
実行して確認済み（このリポジトリのdocs/DESIGN.md参照）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

import httpx

# requestsのデフォルトUser-Agent("python-requests/...")だとJustWatch側のWAFに
# 403で弾かれることをGitHub Actions上での実機確認で確認済み。httpxのデフォルト
# UA（ヘッダーを何も指定しない状態）では通ることも実機確認済みなのでhttpxを使う。

_GRAPHQL_URL = "https://apis.justwatch.com/graphql"
_IMAGES_URL = "https://images.justwatch.com"

# JustWatch側がfirstに150以上を渡すと"page too large"(TOO_BIG)で拒否することを
# 実機確認済み。100は成功するため、1リクエストあたりの上限として使い、
# それ以上必要な場合はoffsetでページングする(offsetの重複無し動作も実機確認済み)。
_MAX_PAGE_SIZE = 100

_NEW_TITLES_QUERY = """
query GetNewTitles(
    $country: Country!,
    $language: Language!,
    $first: Int!,
    $offset: Int,
    $filter: TitleFilter,
    $formatPoster: ImageFormat,
    $profile: PosterProfile,
    $offerFilter: OfferFilter!
) {
    newTitles(country: $country, first: $first, offset: $offset, filter: $filter) {
        edges {
            node {
                id
                objectType
                content(country: $country, language: $language) {
                    title
                    posterUrl(profile: $profile, format: $formatPoster)
                }
                offers(country: $country, platform: WEB, filter: $offerFilter) {
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


_POPULAR_TITLES_QUERY = """
query GetPopularTitles(
    $country: Country!,
    $language: Language!,
    $first: Int!,
    $offset: Int,
    $filter: TitleFilter,
    $formatPoster: ImageFormat,
    $profile: PosterProfile,
    $offerFilter: OfferFilter!
) {
    popularTitles(
        country: $country, first: $first, offset: $offset, filter: $filter, sortBy: POPULAR
    ) {
        edges {
            node {
                id
                objectType
                content(country: $country, language: $language) {
                    title
                    posterUrl(profile: $profile, format: $formatPoster)
                }
                offers(country: $country, platform: WEB, filter: $offerFilter) {
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

# popularTitles(≒全件カタログ取得用)は`first + offset`が2000以上になると
# エラーにならず無条件で空リストを返す（実機確認済み。TOO_BIGエラーにはならない）。
# 1クエリだけでは最大1999件までしか取得できないため、`fetch_full_catalog`では
# `min_release_year`/`max_release_year`で年代を分割し、各区間が1999件を
# 超えないようにして全件を合算する（実機確認済み：Netflix/Prime Videoともに
# 1年単位に分割すれば各区間は1999件を大きく下回る）。
_CATALOG_PAGE_CAP = 1999
_CATALOG_PAGE_CAP_WARN_THRESHOLD = 1900  # この件数に達したら分割が粗すぎる可能性
_CATALOG_OLD_ERA_BUCKETS = [(None, 1979), (1980, 1999)]


class JustWatchError(Exception):
    """JustWatch APIの取得に失敗したときに送出する。"""


@dataclass(frozen=True)
class Offer:
    package_short_name: str
    monetization_type: str


@dataclass(frozen=True)
class NewTitle:
    id: str
    title: str
    poster_url: str | None
    offers: list[Offer]

    def has_offer(self, package_short_name: str, monetization_types: list[str]) -> bool:
        return any(
            o.package_short_name == package_short_name
            and o.monetization_type in monetization_types
            for o in self.offers
        )


def fetch_new_titles(
    provider_short_name: str,
    count: int,
    country: str,
    language: str,
    object_types: list[str],
) -> list[NewTitle]:
    """指定プロバイダの新着候補を最大count件取得する。

    件数(count)ベースの取得であり、日付での絞り込みは行わない。
    JustWatch側の「新着」インデックス反映が数日遅れることがあるため、
    ここで多めに取得し、呼び出し側(state_manager)でIDベースの
    重複排除を行う設計にしている。

    countが1ページの上限(_MAX_PAGE_SIZE=100)を超える場合は、
    offsetを進めながら複数回リクエストして連結する。
    """
    results: list[NewTitle] = []
    offset = 0
    while len(results) < count:
        page_size = min(_MAX_PAGE_SIZE, count - len(results))
        page = _fetch_page(
            provider_short_name, page_size, offset, country, language, object_types
        )
        results.extend(page)
        if len(page) < page_size:
            break  # これ以上ページが無い
        offset += page_size
    return results


def _fetch_page(
    provider_short_name: str,
    first: int,
    offset: int,
    country: str,
    language: str,
    object_types: list[str],
) -> list[NewTitle]:
    variables = {
        "country": country,
        "language": language,
        "first": first,
        "offset": offset,
        "formatPoster": "JPG",
        "profile": "S718",
        "offerFilter": {"bestOnly": True},
        "filter": {
            "packages": [provider_short_name],
            "objectTypes": object_types,
        },
    }
    body = {"operationName": "GetNewTitles", "variables": variables, "query": _NEW_TITLES_QUERY}

    try:
        response = httpx.post(_GRAPHQL_URL, json=body, timeout=30)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise JustWatchError(f"JustWatchへのリクエストに失敗しました: {exc}") from exc

    if "errors" in payload:
        raise JustWatchError(f"JustWatch APIがエラーを返しました: {payload['errors']}")

    try:
        edges = payload["data"]["newTitles"]["edges"]
    except (KeyError, TypeError) as exc:
        raise JustWatchError(
            f"JustWatchのレスポンス形式が想定と異なります: {payload}"
        ) from exc

    return [_parse_node(edge["node"]) for edge in edges]


def fetch_full_catalog(
    provider_short_name: str,
    country: str,
    language: str,
    object_types: list[str],
) -> list[NewTitle]:
    """指定プロバイダの現在配信中の全タイトルを取得する（週次の全件チェック用）。

    `popularTitles`は1クエリあたり最大1999件までしか返せない制約があるため、
    `min_release_year`/`max_release_year`で公開年ごとに区切って複数回に分けて
    取得し、結果をID重複排除しつつ連結する。年の上限は実行時点の翌年まで
    （JustWatchには公開前の作品が翌年の年号で既に登録されていることがあるため）
    とし、毎回の実行時に動的に計算するので年が変わっても対応不要。
    """
    current_year = datetime.now(timezone.utc).year
    year_buckets = list(_CATALOG_OLD_ERA_BUCKETS) + [
        (year, year) for year in range(2000, current_year + 2)
    ]

    results_by_id: dict[str, NewTitle] = {}
    for min_year, max_year in year_buckets:
        bucket_count = 0
        offset = 0
        while offset + _MAX_PAGE_SIZE <= _CATALOG_PAGE_CAP:
            page_size = min(_MAX_PAGE_SIZE, _CATALOG_PAGE_CAP - offset)
            page = _fetch_catalog_page(
                provider_short_name,
                page_size,
                offset,
                country,
                language,
                object_types,
                min_year,
                max_year,
            )
            for title in page:
                results_by_id[title.id] = title
            bucket_count += len(page)
            if len(page) < page_size:
                break  # この区間はこれ以上ページが無い
            offset += page_size
        if bucket_count >= _CATALOG_PAGE_CAP_WARN_THRESHOLD:
            print(
                f"[fetch_full_catalog] 警告: 区間({min_year}-{max_year})が"
                f"{bucket_count}件でJustWatchの1999件上限に接近しています。"
                "この区間の一部タイトルが取得できていない可能性があるため、"
                "年の分割をさらに細かくすることを検討してください。"
            )

    return list(results_by_id.values())


def _fetch_catalog_page(
    provider_short_name: str,
    first: int,
    offset: int,
    country: str,
    language: str,
    object_types: list[str],
    min_release_year: int | None,
    max_release_year: int | None,
) -> list[NewTitle]:
    variables = {
        "country": country,
        "language": language,
        "first": first,
        "offset": offset,
        "formatPoster": "JPG",
        "profile": "S718",
        "offerFilter": {"bestOnly": True},
        "filter": {
            "packages": [provider_short_name],
            "objectTypes": object_types,
            "releaseYear": {"min": min_release_year, "max": max_release_year},
        },
    }
    body = {
        "operationName": "GetPopularTitles",
        "variables": variables,
        "query": _POPULAR_TITLES_QUERY,
    }

    try:
        response = httpx.post(_GRAPHQL_URL, json=body, timeout=30)
        response.raise_for_status()
        payload = response.json()
    except httpx.HTTPError as exc:
        raise JustWatchError(f"JustWatchへのリクエストに失敗しました: {exc}") from exc

    if "errors" in payload:
        raise JustWatchError(f"JustWatch APIがエラーを返しました: {payload['errors']}")

    try:
        edges = payload["data"]["popularTitles"]["edges"]
    except (KeyError, TypeError) as exc:
        raise JustWatchError(
            f"JustWatchのレスポンス形式が想定と異なります: {payload}"
        ) from exc

    return [_parse_node(edge["node"]) for edge in edges]


def _parse_node(node: dict) -> NewTitle:
    content = node.get("content") or {}
    poster_path = content.get("posterUrl")
    offers = [
        Offer(
            package_short_name=o["package"]["shortName"],
            monetization_type=o["monetizationType"],
        )
        for o in node.get("offers", [])
    ]
    return NewTitle(
        id=node["id"],
        title=content.get("title") or "(タイトル不明)",
        poster_url=(_IMAGES_URL + poster_path) if poster_path else None,
        offers=offers,
    )
