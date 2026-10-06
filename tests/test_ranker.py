import math
import random
from collections import Counter

from app.ranking.ranker import CAMPAIGN_REPEAT_PENALTY, RankInput, rank, score


def candidate(
    cid: str, p: float, *, u: float = 0.05, seen: bool = False, n24: int = 5000, n72: int = 5000
) -> RankInput:
    return RankInput(cid, p, math.log(p / (1 - p)), u, seen, n24, n72)


def picks(candidates: list[RankInput], runs: int = 4000, toss_up: float = 0.10, cold: float = 0.05) -> Counter[str]:
    rng = random.Random(7)
    return Counter(rank(candidates, rng, toss_up, cold).campaign_id for _ in range(runs))


def test_single_candidate() -> None:
    decision = rank([candidate("a", 0.2)], random.Random(0), 0.1, 0.05)
    assert (decision.campaign_id, decision.reason) == ("a", "only_candidate")


def test_clear_winner_always_wins() -> None:
    assert picks([candidate("strong", 0.30), candidate("weak", 0.10)]) == {"strong": 4000}


def test_exact_ties_are_split_fairly() -> None:
    counts = picks([candidate("a", 0.157), candidate("b", 0.157)])
    assert 1800 < counts["a"] < 2200  # no campaign is favored just by its position


def test_repeat_penalty_moves_score_by_log_of_penalty() -> None:
    fresh, seen = score(candidate("a", 0.2)), score(candidate("a", 0.2, seen=True))
    assert math.isclose(seen.score - fresh.score, math.log(CAMPAIGN_REPEAT_PENALTY))


def test_repeat_penalty_steers_away_from_a_campaign_just_seen() -> None:
    assert picks([candidate("seen", 0.3, seen=True), candidate("other", 0.3)]) == {"other": 4000}


def test_scarce_data_widens_the_uncertainty_band() -> None:
    assert score(candidate("new", 0.2, n72=0)).uncertainty > score(candidate("known", 0.2, n72=50_000)).uncertainty


def test_cold_campaign_is_explored_about_cold_share_of_the_time() -> None:
    # A cold campaign whose optimistic end reaches a well-known leader gets ~5% of picks (toss-up rule off).
    counts = picks([candidate("leader", 0.20), candidate("cold", 0.18, n24=0, n72=10)], toss_up=0.0)
    assert 100 < counts["cold"] < 320


def test_toss_up_explores_the_least_known_campaign() -> None:
    # Overlapping bands, both graduated (>= 1,000 impressions in 72h): the least-known wins ~10% of the time.
    leader, runner = candidate("leader", 0.200, u=0.2, n24=9000), candidate("runner", 0.195, u=0.2, n24=100)
    counts = picks([leader, runner], toss_up=0.10, cold=0.0)
    assert 200 < counts["runner"] < 700


def test_both_rules_can_stack_for_a_cold_least_known_campaign() -> None:
    # Cold (5%) and toss-up (10% of the rest) both favor a brand-new campaign: ~14.5% of picks.
    counts = picks([candidate("leader", 0.20), candidate("cold", 0.18, n24=0, n72=10)])
    assert 450 < counts["cold"] < 720
