
def format_big_number(num):
    num = float(num)
    if num >= 1000000:
        return f"{num / 1000000:.2f}M"
    elif num >= 1000:
        return f"{num / 1000:.2f}K"
    else:
        return str(num)

def time_to_string(timestamp1=None):
    from datetime import datetime
    import time
    timestamp1 = timestamp1 or time.time()
    return datetime.fromtimestamp(timestamp1).strftime("%Y-%m-%d, %H:%M:%S")

def format_day_hour_minute(seconds):
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)
    parts = []
    if days > 0:
        parts.append(f"{int(days)}d")
    if hours > 0:
        parts.append(f"{int(hours)}h")
    if minutes > 0:
        parts.append(f"{int(minutes)}m")
    if seconds > 0 or not parts:
        if not days:
            parts.append(f"{int(seconds)}s")
    return ' '.join(parts)