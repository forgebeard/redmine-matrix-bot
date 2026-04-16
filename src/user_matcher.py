"""
Сопоставление сотрудников из Redmine с аккаунтами в Matrix.

Чистая логика (без I/O) — транслитерация, парсинг, скоринг — + async
обёртки для HTTP-запросов через httpx.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

import httpx

# ═══════════════════════════════════════════════════════════════════════════
# Константы
# ═══════════════════════════════════════════════════════════════════════════

WORD_REGEX = re.compile(r"^[А-Яа-яA-Za-zёЁ']+([.\-][А-Яа-яA-Za-zёЁ']+)*$")

SYSTEM_WORDS = [
    "портал",
    "поддержка",
    "админ",
    "систем",
    "бот",
    "service",
    "redsoft",
    "техподдержка",
    "support",
    "admin",
    "system",
]

TRANSLIT_MAP = {
    "а": "a",
    "б": "b",
    "в": "v",
    "г": "g",
    "д": "d",
    "е": "e",
    "ё": "yo",
    "ж": "zh",
    "з": "z",
    "и": "i",
    "й": "y",
    "к": "k",
    "л": "l",
    "м": "m",
    "н": "n",
    "о": "o",
    "п": "p",
    "р": "r",
    "с": "s",
    "т": "t",
    "у": "u",
    "ф": "f",
    "х": "kh",
    "ц": "ts",
    "ч": "ch",
    "ш": "sh",
    "щ": "shch",
    "ъ": "",
    "ы": "y",
    "ь": "",
    "э": "e",
    "ю": "yu",
    "я": "ya",
}

TRANSLIT_ALTERNATIVES: dict[str, list[str]] = {
    "е": ["e", "ye"],
    "ё": ["yo", "e", "jo", "o"],
    "ж": ["zh", "j"],
    "й": ["y", "i"],
    "х": ["kh", "h", "x"],
    "ц": ["ts", "c", "tz"],
    "ч": ["ch", "tch"],
    "ш": ["sh"],
    "щ": ["shch", "sch"],
    "ы": ["y", "i"],
    "э": ["e"],
    "ю": ["yu", "iu", "ju"],
    "я": ["ya", "ia", "ja"],
    "кс": ["x", "ks"],
}

REQUEST_TIMEOUT = 15.0
MATRIX_TIMEOUT = 5.0
MATRIX_SEARCH_LIMIT = 10
RATE_LIMIT_DELAY = 0.0

# ═══════════════════════════════════════════════════════════════════════════
# Данные
# ═══════════════════════════════════════════════════════════════════════════


@dataclass
class Match:
    """Результат сопоставления одного сотрудника."""

    redmine_name: str
    redmine_id: int
    matrix_localpart: str | None = None
    matrix_display_name: str | None = None
    matrix_user_id: str | None = None
    status: str = "not_found"  # "found" | "existing" | "not_found"

    @property
    def is_found(self) -> bool:
        return self.status == "found"

    @property
    def is_existing(self) -> bool:
        return self.status == "existing"


# ═══════════════════════════════════════════════════════════════════════════
# Чистая логика — транслитерация, нормализация, скоринг
# ═══════════════════════════════════════════════════════════════════════════


def transliterate(text: str) -> str:
    """Транслитерирует кириллицу в латиницу."""
    return "".join(TRANSLIT_MAP.get(char, char) for char in text.lower())


def normalize_yo(text: str) -> str:
    """Заменяет ё на е."""
    return text.replace("ё", "е").replace("Ё", "Е")


def transliterate_variants(word: str) -> set[str]:
    """Генерирует возможные варианты транслитерации одного слова."""
    word_lower = word.lower()
    variants: set[str] = set()

    variants.add(transliterate(word_lower))
    variants.add(transliterate(normalize_yo(word_lower)))

    for cyr, alts in TRANSLIT_ALTERNATIVES.items():
        if cyr in word_lower:
            base = transliterate(word_lower)
            base_cyr = transliterate(cyr)
            for alt in alts:
                if base_cyr in base:
                    variants.add(base.replace(base_cyr, alt, 1))

            word_no_yo = normalize_yo(word_lower)
            base2 = transliterate(word_no_yo)
            for alt in alts:
                if base_cyr in base2:
                    variants.add(base2.replace(base_cyr, alt, 1))

    variants.discard("")
    return variants


def generate_translit_queries(name: str) -> list[str]:
    """Генерирует список поисковых запросов на латинице из кириллического имени."""
    parts = name.strip().split()
    if not parts:
        return []

    queries: set[str] = set()
    primary_parts = [transliterate(p.lower()) for p in parts]
    primary_parts_noyo = [transliterate(normalize_yo(p.lower())) for p in parts]

    for tp_set in (primary_parts, primary_parts_noyo):
        queries.add(" ".join(tp_set))
        queries.add(" ".join(reversed(tp_set)))
        queries.add("_".join(tp_set))
        queries.add("_".join(reversed(tp_set)))
        queries.add(".".join(tp_set))
        queries.add(".".join(reversed(tp_set)))

    for part in parts:
        for variant in transliterate_variants(part):
            if len(variant) >= 3:
                queries.add(variant)

    if len(primary_parts) >= 2:
        for tp_set in (primary_parts, primary_parts_noyo):
            for i, tp in enumerate(tp_set):
                others = [tp_set[j] for j in range(len(tp_set)) if j != i]
                for other in others:
                    queries.add(f"{tp[0]}.{other}")
                    queries.add(f"{tp[0]}_{other}")
                    queries.add(f"{other}.{tp[0]}")
                    queries.add(f"{other}_{tp[0]}")

    queries.discard("")
    return list(queries)


def count_translit_matches(localpart_clean: str, name_parts: list[str]) -> int:
    """Считает сколько частей имени нашлось в localpart через транслит."""
    matches = 0
    for part in name_parts:
        if len(part) < 2:
            continue
        variants = transliterate_variants(part)
        if any(v in localpart_clean for v in variants):
            matches += 1
    return matches


def normalize_name(name: str) -> str:
    return re.sub(r"\s+", " ", name.strip().lower())


def score_matrix_candidate(redmine_name: str, matrix_user: dict[str, Any]) -> float:
    """Оценивает совпадение кандидата из Matrix.

    Ключевое правило: нужно совпадение МИНИМУМ 2 частей имени
    (и имя, и фамилия), иначе score = 0.
    """
    matrix_display = matrix_user.get("display_name", "") or ""
    matrix_id = matrix_user.get("user_id", "")

    localpart_match = re.match(r"@([^:]+):", matrix_id)
    localpart = localpart_match.group(1) if localpart_match else ""

    rn_parts = normalize_name(redmine_name).split()
    rn_parts_noyo = [normalize_yo(p) for p in rn_parts]
    total_name_parts = len(rn_parts)

    # Разбиваем localpart на части по разделителям (., _, -)
    # e.g. "sergey.tikhonenko" -> ["sergey", "tikhonenko"]
    localpart_parts = re.split(r"[._\-]+", localpart.lower())
    localpart_parts = [p for p in localpart_parts if p]

    # Также создаём "чистую" версию без разделителей для legacy-сравнений
    localpart_clean = re.sub(r"[._\-]", "", localpart.lower())

    score = 0.0
    display_matched_parts = 0
    localpart_matched_parts = 0

    # ── 1. Matching against display_name ──
    if matrix_display:
        mn_parts = set(normalize_name(matrix_display).split())
        mn_parts_noyo = set(normalize_name(normalize_yo(matrix_display)).split())

        rn_set = set(rn_parts)
        rn_set_noyo = set(rn_parts_noyo)
        intersection = (rn_set & mn_parts) | (rn_set_noyo & mn_parts_noyo)
        display_matched_parts = len(intersection)

        if display_matched_parts >= 2:
            score += display_matched_parts * 2.0

    # ── 2. Matching against localpart parts (разделённые точками/подчёркиваниями) ──
    # Сравниваем каждую часть localpart с транслитом каждой части имени Redmine
    for lp_part in localpart_parts:
        if len(lp_part) < 2:
            continue
        for rn_part in rn_parts:
            if len(rn_part) < 2:
                continue
            # Транслитерация части имени
            rn_translit = transliterate(rn_part.lower())
            rn_translit_noyo = transliterate(normalize_yo(rn_part.lower()))
            # Также генерируем варианты
            rn_variants = transliterate_variants(rn_part) | transliterate_variants(normalize_yo(rn_part))

            if (lp_part == rn_translit or
                lp_part == rn_translit_noyo or
                lp_part in rn_variants or
                rn_translit in lp_part or
                rn_translit_noyo in lp_part):
                localpart_matched_parts += 1
                break  # Одна часть имени уже совпала с частью localpart

    if localpart_matched_parts >= 2:
        score += localpart_matched_parts * 2.0
    elif localpart_matched_parts == 1:
        score += 0.5  # Частичное совпадение

    # ── 3. Legacy matching (чистый localpart без разделителей) ──
    # Для случаев когда localpart слитный, e.g. "sergeytikhonenko"
    lp_matches = count_translit_matches(localpart_clean, rn_parts)
    lp_matches_noyo = count_translit_matches(localpart_clean, rn_parts_noyo)
    legacy_matched = max(lp_matches, lp_matches_noyo)

    if legacy_matched >= 2 and legacy_matched > localpart_matched_parts:
        score += legacy_matched * 1.5

    # ── 4. Translit matching against display_name ──
    if matrix_display:
        display_clean = re.sub(r"[._\-]", " ", normalize_name(matrix_display))
        display_parts = display_clean.split()

        translit_display_matches = 0
        for part in rn_parts:
            if len(part) < 2:
                continue
            variants = transliterate_variants(part) | transliterate_variants(normalize_yo(part))
            for variant in variants:
                if any(variant == dp or variant in dp for dp in display_parts):
                    translit_display_matches += 1
                    break

        if translit_display_matches >= 2:
            score += translit_display_matches * 1.0

    # ── 5. Final check: минимум 2 части должны совпасть ──
    best_part_matches = max(display_matched_parts, localpart_matched_parts, legacy_matched)
    if best_part_matches < 2 and total_name_parts >= 2:
        return 0.0

    return score


def find_best_match(
    redmine_name: str, matrix_results: list[dict[str, Any]], min_score: float = 0.5
) -> dict[str, Any] | None:
    if not matrix_results:
        return None

    candidates = []
    for user in matrix_results:
        score = score_matrix_candidate(redmine_name, user)
        import sys as _fbs
        _fbs.stderr.write(
            f"  [SCORE] '{redmine_name}' vs '{user.get('display_name','?')}' "
            f"({user.get('user_id','?')}) → score={score:.2f}\n"
        )
        _fbs.stderr.flush()
        if score >= min_score:
            candidates.append((score, user))

    if not candidates:
        return None

    candidates.sort(key=lambda c: -c[0])
    return candidates[0][1]


def extract_matrix_localpart(user: dict[str, Any]) -> str | None:
    if not user:
        return None
    user_id = user.get("user_id", "")
    m = re.match(r"@([^:]+):", user_id)
    return m.group(1) if m else None


# ═══════════════════════════════════════════════════════════════════════════
# Redmine — парсинг URL и извлечение данных
# ═══════════════════════════════════════════════════════════════════════════


def parse_url_to_endpoint(
    target_url: str,
) -> tuple[str | None, str | None, dict[str, Any] | None, str | None]:
    """Парсит URL страницы Redmine → (base_url, api_url, params, endpoint_type)."""
    parsed = urlparse(target_url)
    base_url = f"{parsed.scheme}://{parsed.netloc}"
    path = parsed.path.rstrip("/")

    if m := re.search(r"/groups/(\d+)", path):
        return base_url, f"{base_url}/groups/{m.group(1)}.json", {"include": "users"}, "group"
    if m := re.search(r"/projects/([^/]+)/members", path):
        return (
            base_url,
            f"{base_url}/projects/{m.group(1)}/memberships.json",
            {},
            "memberships",
        )
    if path.endswith("/users") or path == "/users":
        return base_url, f"{base_url}/users.json", {"limit": 100}, "users"
    if m := re.search(r"/users/(\d+)", path):
        return base_url, f"{base_url}/users/{m.group(1)}.json", {}, "single_user"
    return None, None, None, None


def extract_name(user: dict[str, Any]) -> str:
    """Извлекает человеческое имя из dict пользователя Redmine."""
    if "name" in user and user["name"]:
        return user["name"]
    if "firstname" in user or "lastname" in user:
        n = f"{user.get('lastname', '')} {user.get('firstname', '')}".strip()
        if n:
            return n
    if "login" in user:
        return user["login"]
    if "user" in user:
        return extract_name(user["user"])
    return ""


def is_human_entry(user: dict[str, Any]) -> bool:
    """Проверяет что запись — реальный человек, не системный аккаунт."""
    name = extract_name(user).strip()
    if not name:
        return False
    if "type" in user and user.get("type", "").lower() != "user":
        return False
    words = name.split()
    if not (2 <= len(words) <= 3):
        return False
    if not all(WORD_REGEX.match(w) for w in words):
        return False
    return not any(sw in name.lower() for sw in SYSTEM_WORDS)


# ═══════════════════════════════════════════════════════════════════════════
# Async HTTP — Redmine + Matrix
# ═══════════════════════════════════════════════════════════════════════════


async def fetch_redmine_users(
    client: httpx.AsyncClient,
    api_url: str,
    params: dict[str, Any],
    endpoint_type: str,
    api_key: str,
) -> list[dict[str, Any]]:
    """Забирает список пользователей из Redmine API."""
    headers = {"X-Redmine-API-Key": api_key}
    all_users: list[dict[str, Any]] = []

    try:
        if endpoint_type == "group":
            response = await client.get(
                api_url, headers=headers, params=params, timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            data = response.json()
            return data.get("group", {}).get("users", [])

        if endpoint_type == "single_user":
            response = await client.get(
                api_url, headers=headers, params=params, timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            data = response.json()
            return [data["user"]] if "user" in data else []

        # users или memberships — постраничная загрузка
        offset = 0
        limit = min(params.get("limit", 100), 100)

        while True:
            page_params = {**params, "offset": offset, "limit": limit}
            response = await client.get(
                api_url, headers=headers, params=page_params, timeout=REQUEST_TIMEOUT
            )
            response.raise_for_status()
            data = response.json()

            if endpoint_type == "memberships":
                users = [m.get("user", {}) for m in data.get("memberships", []) if "user" in m]
            else:
                users = data.get("users", [])

            if not users:
                break

            all_users.extend(users)
            total = data.get("total_count", len(all_users))
            offset += limit

            if offset >= total:
                break

        return all_users

    except (httpx.RequestError, httpx.HTTPStatusError):
        return []


async def search_matrix_user(
    client: httpx.AsyncClient,
    homeserver: str,
    access_token: str,
    search_term: str,
) -> list[dict[str, Any]]:
    """Ищет пользователей в Matrix User Directory."""
    search_url = f"https://{homeserver}/_matrix/client/r0/user_directory/search"
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    payload = {"search_term": search_term, "limit": MATRIX_SEARCH_LIMIT}

    try:
        response = await client.post(
            search_url, headers=headers, json=payload, timeout=REQUEST_TIMEOUT
        )
        if response.status_code != 200:
            return []
        return response.json().get("results", [])
    except (httpx.RequestError, httpx.HTTPStatusError):
        return []


# ═══════════════════════════════════════════════════════════════════════════
# Главная функция сканирования
# ═══════════════════════════════════════════════════════════════════════════


def _normalize_homeserver(homeserver: str) -> str:
    """Убирает https://, http:// и trailing slash."""
    hs = homeserver.strip().rstrip("/")
    for prefix in ("https://", "http://"):
        if hs.startswith(prefix):
            hs = hs[len(prefix):]
    return hs


async def scan_redmine_group(
    target_url: str,
    redmine_url: str,
    redmine_api_key: str,
    matrix_homeserver: str,
    matrix_access_token: str,
    existing_redmine_ids: set[int] | None = None,
) -> list[Match]:
    """
    Сканирует группу Redmine и сопоставляет сотрудников с Matrix.
    """
    import asyncio
    import sys as _sys
    import time as _time

    if existing_redmine_ids is None:
        existing_redmine_ids = set()

    _sys.stderr.write(f"[MATCH] START: target_url={target_url}\n")
    _sys.stderr.flush()
    _t_start = _time.monotonic()
    base_url, api_url, params, endpoint_type = parse_url_to_endpoint(target_url)
    if not api_url:
        return []

    # Нормализуем homeserver (убираем https://)
    clean_hs = _normalize_homeserver(matrix_homeserver)
    _sys.stderr.write(f"[MATCH] homeserver raw='{matrix_homeserver}' → clean='{clean_hs}'\n")
    _sys.stderr.flush()

    _sys.stderr.write(f"[MATCH] URL parsed: {endpoint_type}\n")
    _sys.stderr.flush()

    async with httpx.AsyncClient(
        base_url=redmine_url,
        timeout=REQUEST_TIMEOUT,
        follow_redirects=True,
    ) as redmine_client:
        # Для Matrix — отдельный клиент с коротким таймаутом
        matrix_client = httpx.AsyncClient(timeout=MATRIX_TIMEOUT)

        try:
            _sys.stderr.write("[MATCH] fetching Redmine users...\n")
            _sys.stderr.flush()
            _t_redmine = _time.monotonic()
            rm_users = await fetch_redmine_users(
                redmine_client, api_url, params or {}, endpoint_type, redmine_api_key
            )
            rm_users = [u for u in rm_users if is_human_entry(u)]
            _sys.stderr.write(f"[MATCH] Redmine: {len(rm_users)} human users in {_time.monotonic()-_t_redmine:.2f}s\n")
            _sys.stderr.flush()

            if not rm_users:
                return []

            results: list[Match] = []
            seen_matrix_ids: set[str] = set()

            _sys.stderr.write(f"[MATCH] starting Matrix matching for {len(rm_users)} users...\n")
            _sys.stderr.flush()
            _t_matrix_start = _time.monotonic()
            _match_count = 0

            # Параллельный матчинг батчами по 5
            _batch_size = 5
            for _batch_start in range(0, len(rm_users), _batch_size):
                _batch = rm_users[_batch_start:_batch_start + _batch_size]
                _sys.stderr.write(
                    f"[MATCH] batch {_batch_start//_batch_size + 1}: processing {_batch_start+1}-{_batch_start+len(_batch)}/{len(rm_users)}...\n"
                )
                _sys.stderr.flush()
                _batch_t0 = _time.monotonic()

                # Запускаем параллельно
                _batch_tasks = [
                    _search_and_match(matrix_client, clean_hs, matrix_access_token,
                                       extract_name(u), seen_matrix_ids)
                    for u in _batch
                ]
                _batch_results = await asyncio.gather(*_batch_tasks, return_exceptions=True)

                for u, match_result in zip(_batch, _batch_results):
                    _match_count += 1
                    rm_name = extract_name(u)
                    rm_id = u.get("id", 0)

                    if isinstance(match_result, Exception):
                        _sys.stderr.write(
                            f"[MATCH]   #{_match_count}/{len(rm_users)} {rm_name}: ERROR {match_result}\n"
                        )
                        _sys.stderr.flush()
                        best_match = None
                    else:
                        best_match = match_result
                        _sys.stderr.write(
                            f"[MATCH]   #{_match_count}/{len(rm_users)} {rm_name}: "
                            + ("FOUND" if best_match else "NOT FOUND")
                            + f" ({_time.monotonic()-_batch_t0:.1f}s elapsed)\n"
                        )
                        _sys.stderr.flush()

                    if rm_id in existing_redmine_ids:
                        results.append(
                            Match(
                                redmine_name=rm_name,
                                redmine_id=rm_id,
                                status="existing",
                            )
                        )
                        continue

                    localpart = extract_matrix_localpart(best_match)
                    display = best_match.get("display_name", "") if best_match else None
                    user_id = best_match.get("user_id") if best_match else None

                    results.append(
                        Match(
                            redmine_name=rm_name,
                            redmine_id=rm_id,
                            matrix_localpart=localpart,
                            matrix_display_name=display,
                            matrix_user_id=user_id,
                            status="found" if localpart else "not_found",
                        )
                    )

            _t_total = _time.monotonic() - _t_start
            _t_matrix = _time.monotonic() - _t_matrix_start
            _sys.stderr.write(f"[MATCH] DONE: {len(rm_users)} users, matrix={_t_matrix:.1f}s total={_t_total:.1f}s\n")
            _sys.stderr.flush()

            return results

        finally:
            await matrix_client.aclose()


async def _search_and_match(
    client: httpx.AsyncClient,
    homeserver: str,
    access_token: str,
    rm_name: str,
    _unused_seen_ids: set[str] | None = None,  # оставлен для совместимости сигнатуры
) -> dict[str, Any] | None:
    """Ищет сотрудника в Matrix: кириллица → транслит. Оптимизировано для скорости."""
    import asyncio as _asyncio
    import logging as _search_log
    import sys as _sys
    import time as _st

    _s_logger = _search_log.getLogger("user_matcher.search")
    _t0 = _st.monotonic()
    combined: list[dict[str, Any]] = []

    # seen_ids — ЛОКАЛЬНЫЙ для каждого поиска, чтобы не терять кандидатов
    # из-за того что другой пользователь в батче уже нашёл этого же человека
    local_seen: set[str] = set()

    # Шаг 1: прямой поиск по кириллице (3 запроса: прямой + обратный + только фамилия)
    search_names = [rm_name]
    name_noyo = normalize_yo(rm_name)
    if name_noyo != rm_name:
        search_names.append(name_noyo)

    # Добавляем поиск только по фамилии (первое слово)
    parts = rm_name.split()
    if len(parts) >= 2:
        search_names.append(parts[0])  # Фамилия
        name_noyo_parts = name_noyo.split()
        if len(name_noyo_parts) >= 2 and name_noyo_parts[0] != parts[0]:
            search_names.append(name_noyo_parts[0])

    for name in search_names:
        _sys.stderr.write(f"[MATCH-SEARCH] '{rm_name}' → searching '{name}'...\n")
        _sys.stderr.flush()
        results = await search_matrix_user(client, homeserver, access_token, name)
        _sys.stderr.write(f"[MATCH-SEARCH]   got {len(results)} results\n")
        _sys.stderr.flush()
        if results:
            _sys.stderr.write(f"[MATCH-SEARCH]   sample: {results[0].get('user_id')} / {results[0].get('display_name')}\n")
            _sys.stderr.flush()
        for user in results:
            uid = user.get("user_id", "")
            if uid not in local_seen:
                local_seen.add(uid)
                combined.append(user)

        # Обратный порядок слов
        parts = name.split()
        if len(parts) >= 2:
            alt_name = f"{parts[-1]} {' '.join(parts[:-1])}"
            _sys.stderr.write(f"[MATCH-SEARCH] '{rm_name}' → searching reversed '{alt_name}'...\n")
            _sys.stderr.flush()
            results_rev = await search_matrix_user(client, homeserver, access_token, alt_name)
            _sys.stderr.write(f"[MATCH-SEARCH]   got {len(results_rev)} results\n")
            _sys.stderr.flush()
            for user in results_rev:
                uid = user.get("user_id", "")
                if uid not in local_seen:
                    local_seen.add(uid)
                    combined.append(user)

    _sys.stderr.write(f"[MATCH-SEARCH] '{rm_name}': {len(combined)} candidates after direct search\n")
    _sys.stderr.flush()

    match = find_best_match(rm_name, combined)
    if match:
        _sys.stderr.write(f"[MATCH-SEARCH] '{rm_name}': MATCHED via direct (score>threshold)\n")
        _sys.stderr.flush()
        return match

    # Шаг 1.5: поиск по транслиту (для имён вроде "irina_sorochan")
    # Также ищем по отдельным словам, чтобы найти "sergey.tikhonenko"
    translit_name = transliterate(rm_name)
    translit_rev = " ".join(reversed(translit_name.split()))
    translit_searches = [translit_name]
    if translit_rev != translit_name:
        translit_searches.append(translit_rev)

    # Добавляем поиск по отдельным словам (транслит)
    for part in rm_name.split():
        t_part = transliterate(part)
        if len(t_part) >= 3 and t_part not in translit_searches:
            translit_searches.append(t_part)

    _sys.stderr.write(f"[MATCH-SEARCH] '{rm_name}': falling back to translit searches: {translit_searches}\n")
    _sys.stderr.flush()

    for t_name in translit_searches:
        results_t = await search_matrix_user(client, homeserver, access_token, t_name)
        _sys.stderr.write(f"[MATCH-SEARCH]   translit '{t_name}' got {len(results_t)} results\n")
        _sys.stderr.flush()
        if results_t:
            _sys.stderr.write(f"[MATCH-SEARCH]   sample: {results_t[0].get('user_id')} / {results_t[0].get('display_name')}\n")
            _sys.stderr.flush()
        for user in results_t:
            uid = user.get("user_id", "")
            if uid not in local_seen:
                local_seen.add(uid)
                combined.append(user)

    match = find_best_match(rm_name, combined)
    if match:
        _sys.stderr.write(f"[MATCH-SEARCH] '{rm_name}': MATCHED via translit\n")
        _sys.stderr.flush()
        return match

    # Шаг 2: fallback — только 2 лучших транслит-варианта
    translit_queries = generate_translit_queries(rm_name)[:2]
    _sys.stderr.write(f"[MATCH-SEARCH] '{rm_name}': falling back to translit queries: {translit_queries}\n")
    _sys.stderr.flush()
    for query in translit_queries:
        await _asyncio.sleep(RATE_LIMIT_DELAY)

        results = await search_matrix_user(client, homeserver, access_token, query)
        _sys.stderr.write(f"[MATCH-SEARCH] '{rm_name}' → translit '{query}' got {len(results)} results\n")
        _sys.stderr.flush()
        new_results = []
        for user in results:
            uid = user.get("user_id", "")
            if uid not in local_seen:
                local_seen.add(uid)
                new_results.append(user)
                combined.append(user)

        if new_results:
            match = find_best_match(rm_name, combined)
            if match:
                _sys.stderr.write(
                    f"[MATCH-SEARCH] '{rm_name}': MATCHED via translit '{query}'\n"
                )
                _sys.stderr.flush()
                return match

    _sys.stderr.write(f"[MATCH-SEARCH] '{rm_name}': NO MATCH after {_st.monotonic()-_t0:.1f}s\n")
    _sys.stderr.flush()
    return None
