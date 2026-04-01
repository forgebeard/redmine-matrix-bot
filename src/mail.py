"""Маскировка идентификаторов для логов (без внешней доставки сообщений)."""


def mask_at_localpart(value: str) -> str:
    """Маскировка строки с символом @ (типично логин вида user@host)."""
    s = (value or "").strip()
    if "@" not in s:
        return "***"
    local, domain = s.split("@", 1)
    if len(local) <= 2:
        local_masked = local[:1] + "***"
    else:
        local_masked = local[:2] + "***"
    return f"{local_masked}@{domain}"


def mask_identifier(value: str) -> str:
    """Маскировка логина панели для логов."""
    v = (value or "").strip()
    if not v:
        return "***"
    if "@" in v:
        return mask_at_localpart(v)
    if len(v) <= 2:
        return v[:1] + "***"
    return v[:2] + "***"
