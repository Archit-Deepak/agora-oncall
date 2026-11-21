"""Part 11a test: the intent router maps real phrasings to the right intent."""
from app import router


def test_slow_checks():
    assert router.classify("are the other carriers ok?") == (router.SLOW, "compare_carriers")
    assert router.classify("is it just one route") == (router.SLOW, "compare_carriers")
    assert router.classify("give me an example call") == (router.SLOW, "sample_failed_call")
    assert router.classify("can I get a call uuid") == (router.SLOW, "sample_failed_call")
    assert router.classify("did the customer change anything?") == (router.SLOW, "recent_config_changes")


def test_confirm_yes_no():
    assert router.classify("yes go ahead") == (router.CONFIRM_YES, None)
    assert router.classify("do it") == (router.CONFIRM_YES, None)
    assert router.classify("no not yet") == (router.CONFIRM_NO, None)


def test_end():
    assert router.classify("okay, bye") == (router.END, None)
    assert router.classify("that's all, thanks") == (router.END, None)


def test_fast_default():
    assert router.classify("what's the impact right now") == (router.FAST, None)
    assert router.classify("how many calls are failing") == (router.FAST, None)


def test_no_false_positive_on_substrings():
    # "now" must not trigger the "no" intent.
    assert router.classify("what is happening now") == (router.FAST, None)


def test_negations_never_read_as_yes():
    from app.router import classify, CONFIRM_NO, CONFIRM_YES
    for t in ["I'm not sure", "don't do it", "not now", "nah", "no, not yet", "do not do it"]:
        assert classify(t)[0] == CONFIRM_NO, t
    for t in ["hold on", "wait"]:  # a pause: neutral, and never a yes
        assert classify(t)[0] != CONFIRM_YES, t
    for t in ["yes go ahead", "sure", "do it", "okay do it", "yep"]:
        assert classify(t)[0] == CONFIRM_YES, t


def test_pause_is_not_a_decline_and_done_is_goodbye():
    from app import router as R
    assert R.classify("Wait, I was just— start again, what is going on exactly?")[0] != R.CONFIRM_NO
    assert R.classify("hold on a sec")[0] != R.CONFIRM_NO
    assert R.classify("Uh, no, I'm done. Thank you.")[0] == R.END
    assert R.classify("no, not now")[0] == R.CONFIRM_NO


def test_requests_to_act_vs_questions():
    from app import router as R
    assert R.asks_to_act("Okay, so can you apply the fix for this?")
    assert R.asks_to_act("go ahead and apply the fix")
    assert not R.asks_to_act("should we reroute?")
    assert not R.asks_to_act("how should we proceed?")
    assert not R.asks_to_act("what's the max concurrency?")


def test_ticket_requests_are_not_fix_approval():
    from app import router as R
    for t in ["Could you make a linear ticket for this?", "I think we might have to make a ticket for this."]:
        assert R.asks_for_ticket(t) and not R.asks_to_act(t), t


def test_sentence_cap_stops_role_play():
    from app.brain_proxy import SentenceCap
    c, out = SentenceCap(2), ""
    for ch in ["All failures are TATA.", " Enabling HA fixes it; just say yes.", "Yes, please enable HA.", " HA is now enabled."]:
        out += c.feed(ch)
    assert out == "All failures are TATA. Enabling HA fixes it; just say yes." and c.done


def test_telegram_summary_intent():
    from app import router as R
    assert R.asks_for_summary("can you send my telegram a summary of the issue?")
    assert R.asks_for_summary("text me the summary on telegram")
    assert not R.asks_for_summary("what's the summary of the issue?")


def test_loop_in_picks_people():
    from app import escalation, router as R
    assert R.asks_to_loop_in("Okay, loop in the carrier team and the account manager")
    assert [p["name"] for p in escalation.pick("loop in the carrier team and the account manager", "carrier")] == ["Rahul Menon", "Neha Kapoor"]
    assert [p["key"] for p in escalation.pick("loop in the right people", "customer")] == ["am", "noc"]
    assert len(escalation.pick("loop everyone in", None)) == 3


def test_hedges_and_questions_never_approve():
    # sim 21 Nov: "Sure, but wait." and "should we do it?" applied the fix
    from app import router as R
    for t in ["Sure, but wait.", "should we do it?", "Yeah no.", "I guess.", "Hmm okay maybe.", "What if we enable HA?",
              "Should we reroute?", "Not sure.", "yes but hold on", "maybe go ahead?"]:
        assert R.classify(t)[0] != R.CONFIRM_YES, t
        assert not R.asks_to_act(t), t
    for t in ["Yes, go ahead.", "Okay yes, apply the fix.", "Fine, go ahead and apply it."]:
        assert R.classify(t)[0] == R.CONFIRM_YES or R.asks_to_act(t), t


def test_non_english_turns_trigger_nothing_and_garbled_phrases_work():
    from app import router as R, screen
    for t in ["haan theek hai, fix kar do", "kya ho raha hai?"]:  # robustness only: no action from a non-English turn
        assert R.classify(t)[0] != R.CONFIRM_YES and not R.asks_to_act(t), t
    assert R.asks_to_loop_in("lupe in the carrier team")
    assert screen.quick("compair the carriers")["name"] == "show_chart"
    assert screen.quick("open won of those call logs")["name"] == "open_call"
    assert screen.quick("show me there call flow")["name"] == "show_flow"
    assert R.classify("Okay bye, call me later.")[0] == R.END


def test_offtopic_who_is_not_an_investigation():
    from app import router as R
    assert not R.needs_investigation("Who made you?")
    assert R.needs_investigation("who owns this problem?")


def test_unverified_cause_guard():
    from types import SimpleNamespace as NS
    from app.brain_proxy import states_unverified_cause, unverified_line
    cf = NS(hypotheses=[NS(id="cps_burst", status="leading", title="CPS burst")])
    assert states_unverified_cause("We're seeing a burst from their dialer hitting twelve calls per second.", cf)
    assert states_unverified_cause("Their traffic is hitting the CPS limit, causing failures.", cf)
    assert not states_unverified_cause("The leading idea is a CPS burst, still checking.", cf)
    assert not states_unverified_cause("About forty percent of their calls are failing right now.", cf)
    assert unverified_line(cf).startswith("Not confirmed yet: the leading idea is cps burst")
