"""Creator-scoped coverage preference, shared by the legacy and live planners."""
import json
import math
from pathlib import Path


def coverage_limit(profile=None, rules=None):
    if rules is None:
        rules=json.loads((Path(__file__).resolve().parents[1]/'config/broll_rules.json').read_text())
    profile=profile or {}
    value=profile.get('rules',{}).get('max_coverage',
        profile.get('parameters',{}).get('max_coverage',rules['max_coverage']))
    if type(value) not in (int,float) or not math.isfinite(value) or not 0<=value<=1:
        raise ValueError('创作者 max_coverage 须为 0 到 1 的数值')
    return value
