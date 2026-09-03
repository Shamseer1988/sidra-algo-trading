from app.services.journal_analytics import ScoreRecord, analyse_score_components


def _record(good: int, noise: int, status: str) -> ScoreRecord:
    realized_r = 2.0 if status == "TARGET" else -1.0
    return ScoreRecord("orb-retest-v1@1", {"good": good, "noise": noise}, status, realized_r)


def test_score_analysis_reports_insufficient_data_below_the_sample_floor() -> None:
    result = analyse_score_components([_record(20, 5, "TARGET")], minimum_samples=6)
    assert result["insufficient_data"] is True
    assert result["components"] == []


def test_score_analysis_ranks_a_predictive_component_above_noise() -> None:
    records = []
    for index in range(8):
        win = index % 2 == 0
        records.append(_record(20 if win else 0, (index * 7) % 25, "TARGET" if win else "STOP"))

    result = analyse_score_components(records, minimum_samples=6)
    assert result["insufficient_data"] is False
    assert result["resolved_signals"] == 8
    assert result["overall"]["win_rate_percent"] == 50.0

    by_name = {item["component"]: item for item in result["components"]}
    assert by_name["good"]["lift_r"] == 3.0
    assert by_name["good"]["above"]["win_rate_percent"] == 100.0
    assert by_name["good"]["below"]["win_rate_percent"] == 0.0
    assert abs(by_name["noise"]["lift_r"]) < 0.01
    # Components are ordered by descending lift.
    assert result["components"][0]["component"] == "good"


def test_score_analysis_skips_a_constant_component() -> None:
    records = [_record(20 if index % 2 == 0 else 0, 10, "TARGET" if index % 2 == 0 else "STOP") for index in range(8)]
    names = {item["component"] for item in analyse_score_components(records, minimum_samples=6)["components"]}
    assert names == {"good"}  # "noise" is constant, so it cannot be split
