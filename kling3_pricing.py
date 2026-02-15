def calculate_kling3_price(resolution: str, enable_audio: bool, duration: int) -> int:
    if duration < 3 or duration > 15:
        raise ValueError("Invalid duration")

    if resolution == "720" and not enable_audio:
        per_sec = 2
    elif resolution == "720" and enable_audio:
        per_sec = 2
    elif resolution == "1080" and not enable_audio:
        per_sec = 2
    elif resolution == "1080" and enable_audio:
        per_sec = 3
    else:
        raise ValueError("Invalid config")

    return per_sec * duration
