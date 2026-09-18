"""JustWatch非公式GraphQL APIから「新着」タイトルを取得する薄いクライアント。

`newTitles` フィールドはJustWatch公式には非公開・無告知で変更されうる。
実際に動作することは `scripts/debug_justwatch.py` をGitHub Actions上で
実行して確認済み（このリポジトリのdocs/DESIGN.md参照）。
"""

from __future__ import annotations

from dataclasses import dataclass

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
