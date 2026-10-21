from jobtracker import evalmetrics as m


def it(t_job, p_job, t_status="Applied", p_status="Applied", t_co="Acme", p_co="Acme", t_role="SWE", p_role="SWE"):
    return {"truth": {"is_job": t_job, "company": t_co, "role": t_role, "status": t_status},
            "pred": {"is_job": p_job, "company": p_co, "role": p_role, "status": p_status}}


def test_wilson_interval_is_wide_for_small_samples():
    lo, hi = m.wilson(9, 10)
    assert 0.55 < lo < 0.60 and 0.97 < hi < 1.0            # 9/10 is NOT "90% +- 0"
    assert m.wilson(0, 0) == (0.0, 0.0)
    lo2, hi2 = m.wilson(90, 100)
    assert (hi2 - lo2) < (hi - lo)


def test_detection_precision_recall():
    items = [it(True, True), it(True, True), it(True, False), it(False, True), it(False, False), it(False, False)]
    d = m.detection(items)
    assert (d["tp"], d["fp"], d["fn"], d["tn"]) == (2, 1, 1, 2)
    assert round(d["precision"], 3) == 0.667 and round(d["recall"], 3) == 0.667 and round(d["f1"], 3) == 0.667


def test_extraction_counts_missed_emails_as_wrong_and_ignores_non_jobs():
    items = [it(True, True),                                                   # all right
             it(True, True, p_status="Rejected"),                              # wrong status
             it(True, True, p_co="Other Corp"),                                # wrong company
             it(True, True, t_role="Software Engineer, New Grad", p_role="Software Engineer New Grad"),   # same role, punctuation
             it(True, False),                                                  # missed entirely
             it(False, True)]                                                  # not a job email: not in the denominators
    e = m.extraction(items)
    assert e["n_job_emails"] == 5
    assert (e["status"]["k"], e["status"]["n"]) == (3, 5)                      # 1 wrong status + 1 missed
    assert (e["status_given_caught"]["k"], e["status_given_caught"]["n"]) == (3, 4)
    assert (e["company"]["k"], e["company"]["n"]) == (3, 5)
    assert (e["role"]["k"], e["role"]["n"]) == (4, 5)
    assert m.company_match("Maven Securities", "Maven") and not m.company_match("Apple", "Applebee's")


def test_status_breakdown_confusion_and_other_label():
    items = [it(True, True, "Rejected", "Rejected"), it(True, True, "Rejected", "Applied"), it(True, True, "Applied", "Applied"),
             it(True, False, "OA", None), it(True, True, "Other", None)]
    b = m.status_breakdown(items)
    assert b["confusion"]["Rejected"] == {"Rejected": 1, "Applied": 1}
    assert b["confusion"]["OA"] == {"Missed": 1}
    assert b["confusion"]["Other"] == {"Other": 1}                              # a None prediction counts as 'Other'
    assert b["per_class"]["Applied"]["tp"] == 1 and b["per_class"]["Applied"]["fp"] == 1
    assert b["per_class"]["Rejected"]["recall"] == 0.5 and b["per_class"]["Rejected"]["precision"] == 1.0


def test_zero_misses_in_a_small_sample_does_not_claim_perfect_recall():
    matched = [it(True, True)] * 10
    unmatched = [it(False, False)] * 60                                           # 0 of 60 hiding
    s = m.search_recall(matched, unmatched, n_matched_universe=130, n_unmatched_universe=2254)
    assert s["recall"] == 1.0                                                     # the point estimate
    assert s["recall_ci"][0] < 0.6                                                # but the data can't rule out many misses
    assert s["missed_upper_bound"] > 100


def test_stratified_recall_exact_hot_stratum_and_wide_cold_interval():
    matched = [it(True, True)] * 9 + [it(False, False)]                                # 90% of matched mail is application mail
    hot = [it(True, False)] * 3 + [it(False, False)] * 17                              # all 20 suspects labeled: 3 are real misses
    cold = [it(False, False)] * 100                                                    # 0 of 100 random others
    s = m.search_recall_stratified(matched, cold, hot, n_matched_universe=100, n_hot_universe=20, n_cold_universe=2000)
    assert s["hot"]["exact"] and s["hot"]["job"] == 3
    assert s["estimated_found"] == 90 and s["estimated_missed"] == 3                   # hot is exact, cold estimate is 0
    assert round(s["recall"], 3) == round(90 / 93, 3)
    lo, hi = s["recall_ci"]
    assert lo < s["recall"] <= hi and lo < 0.8                                         # the cold stratum's 0/100 can still hide ~70
    assert s["missed_upper_bound"] > 50

    # more random labels shrink the bound; with 400 cold labels and none positive the worst case is far smaller
    tight = m.search_recall_stratified(matched, [it(False, False)] * 400, hot, 100, 20, 2000)
    assert tight["missed_upper_bound"] < s["missed_upper_bound"] and tight["recall_ci"][0] > lo
    # a partly labeled hot stratum is an estimate, not exact
    partial = m.search_recall_stratified(matched, cold, hot[:10], 100, 20, 2000)
    assert not partial["hot"]["exact"]
