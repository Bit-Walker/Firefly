"""Measurements from finite, right-censored digital captures."""
import statistics


def analyze(segments):
    merged = []
    for item in segments:
        level, us = item['level'], item['us']
        if merged and merged[-1]['level'] == level:
            merged[-1]['us'] += us
        else:
            merged.append({'level': level, 'us': us})
    # Firmware stops decoding at RMT's zero-duration EOF. Every returned duration
    # is bounded by a transition; the final idle duration is absent, not the last
    # valid item. Dropping that item would incorrectly lose the last high pulse.
    complete = merged
    highs = [s['us'] for s in complete if s['level'] == 1]
    lows = [s['us'] for s in complete if s['level'] == 0]
    periods = [complete[i]['us'] + complete[i+1]['us'] for i in range(len(complete)-1) if complete[i]['level'] == 1 and complete[i+1]['level'] == 0]
    avg = lambda xs: statistics.mean(xs) if xs else None
    period = avg(periods)
    hi, lo = avg(highs), avg(lows)
    return {'high_pulses': len(highs), 'high_us_mean': hi, 'high_us_min': min(highs) if highs else None,
            'high_us_max': max(highs) if highs else None, 'low_us_mean': lo,
            'period_us_mean': period, 'frequency_hz': 1000000/period if period else None,
            'duty_percent': 100*hi/(hi+lo) if hi is not None and lo is not None else None,
            'duration_us': sum(s['us'] for s in merged), 'resolution_us': 1,
            'right_censored': True, 'segments': len(merged)}
