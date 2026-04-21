class KeywordFilter:

    @staticmethod
    def signal_hit(text: str, signal_name: str, signals: dict) -> bool:
        keywords = signals.get(signal_name, {}).get("keywords", [])
        lower = text.lower()
        return any(k.lower() in lower for k in keywords)

    @staticmethod
    def radar_hit(text: str, keywords: list[str]) -> str | None:
        lower = text.lower()
        for k in keywords:
            if k.lower() in lower:
                return k
        return None
