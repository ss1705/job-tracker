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


def test_search_recall_estimate_from_two_samples():
    matched = [it(True, True)] * 8 + [it(False, False)] * 2                    # 80% of matched mail is job-related
    unmatched = [it(True, False)] * 2 + [it(False, False)] * 98                # 2% of unmatched mail is job-related
    s = m.search_recall(matched, unmatched, n_matched_universe=300, n_unmatched_universe=2000)
    assert s["estimated_found"] == 240 and s["estimated_missed"] == 40
    assert round(s["recall"], 3) == round(240 / 280, 3)
    assert (s["unmatched_sample"]["k"], s["unmatched_sample"]["n"]) == (2, 100)
