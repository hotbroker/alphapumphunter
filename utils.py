
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
