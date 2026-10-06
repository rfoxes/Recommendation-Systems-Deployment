"""The ranker from the model repo (src/ranking.py: Goal 2 policy + Goals 3-4 layer), adapted to one
request at a time. Exploration budgets that were shares of an hour's traffic become probabilities.

  score z   = logit(V1) + log(campaign repeat penalty, if the user saw the campaign in the last 24h)
  band u    = sqrt(u_model^2 + u_data^2): half the LightGBM/FM gap in log-odds, widened for scarce data
  pick      = highest z, except
    - cold exploration: a cold campaign (< 1,000 impressions in 72h) whose optimistic end z + u reaches
      the leader's z wins with probability `cold_share` (highest optimistic end first)
    - toss-up exploration: if the runner-up's band overlaps the leader's, the least-known tied campaign
      (fewest impressions in the last 24h) wins with probability `toss_up_share`
"""

import math
import random
from dataclasses import dataclass

from app.models import RankingReason

# Measured in the model repo (results/layer/metrics.md): click rate after a repeat exposure in the last
# 24h, relative to a first exposure. Ranking is per campaign, so the campaign-level penalty applies.
CAMPAIGN_REPEAT_PENALTY = 0.82
GRADUATION_IMPRESSIONS = 1000
SMOOTHING = 20


@dataclass(frozen=True)
class RankInput:
    campaign_id: str
    p_v1: float
    z_model: float  # logit(V1)
    u_model: float  # half the LightGBM/FM gap
    seen_campaign_24h: bool
    impressions_24h: int
    impressions_72h: int


@dataclass(frozen=True)
class Ranked:
    campaign_id: str
    score: float
    uncertainty: float
    cold: bool
    impressions_24h: int


@dataclass(frozen=True)
class Decision:
    campaign_id: str
    reason: RankingReason
    ranked: list[Ranked]


def score(candidate: RankInput) -> Ranked:
    z = candidate.z_model + (math.log(CAMPAIGN_REPEAT_PENALTY) if candidate.seen_campaign_24h else 0.0)
    p = min(max(candidate.p_v1, 1e-6), 1 - 1e-6)
    u_data = 1 / math.sqrt((candidate.impressions_72h + SMOOTHING) * p * (1 - p))
    return Ranked(
        campaign_id=candidate.campaign_id,
        score=z,
        uncertainty=math.hypot(candidate.u_model, u_data),
        cold=candidate.impressions_72h < GRADUATION_IMPRESSIONS,
        impressions_24h=candidate.impressions_24h,
    )


def rank(candidates: list[RankInput], rng: random.Random, toss_up_share: float, cold_share: float) -> Decision:
    shuffled = [score(c) for c in candidates]
    rng.shuffle(shuffled)  # exact ties (common before there's data) must not always favor the same campaign
    ranked = sorted(shuffled, key=lambda r: r.score, reverse=True)
    leader = ranked[0]
    if len(ranked) == 1:
        return Decision(leader.campaign_id, "only_candidate", ranked)

    promising_cold = [r for r in ranked[1:] if r.cold and r.score + r.uncertainty >= leader.score]
    if promising_cold and rng.random() < cold_share:
        pick = max(promising_cold, key=lambda r: r.score + r.uncertainty)
        return Decision(pick.campaign_id, "explore_cold", ranked)

    runner_up = ranked[1]
    if runner_up.score + runner_up.uncertainty >= leader.score - leader.uncertainty and rng.random() < toss_up_share:
        tied = [r for r in ranked if r.score + r.uncertainty >= leader.score - leader.uncertainty]
        pick = min(tied, key=lambda r: (r.impressions_24h, -r.score))
        if pick.campaign_id != leader.campaign_id:
            return Decision(pick.campaign_id, "explore_toss_up", ranked)
    return Decision(leader.campaign_id, "best_score", ranked)
