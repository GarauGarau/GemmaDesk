"""Readable zero-based scales, including very small topic percentages."""
import math


def axis_maximum(values, percent=False, full=False):
    if full:return 100.
    peak=max((float(v) for v in values if v is not None and math.isfinite(v)),default=0.)
    if peak<=0:return 1.
    target=peak*1.12
    power=10**math.floor(math.log10(target))
    maximum=next(step*power for step in (1,2,2.5,5,10) if step*power>=target)
    return min(100.,maximum) if percent else maximum


def tick_label(value,maximum):
    decimals=max(0,2-math.floor(math.log10(maximum)))
    return f'{value:.{decimals}f}'.rstrip('0').rstrip('.') if decimals else f'{value:.0f}'
