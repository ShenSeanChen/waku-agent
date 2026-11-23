"""The questions the Judgment Arena races over.

A suite is five texts. Each text carries three questions written for it and for
nothing else: one Noul, one Choice, one Score. Five texts times three questions
is **fifteen different questions** per suite -- five of each shape -- and no
question is ever asked twice.

    Noul    right when the probability lands on the same side of 0.5 as the label
    Choice  right when the option picked is the labelled one
    Score   right when the expected value rounds to the labelled level

The three questions about one text go in one call and come back together. That
is the whole point of a System One model, and it is why the questions are
grouped by text rather than asked one at a time.

There is no policy here and no verdict. What an application does with three
answers is its own business; this file only asks whether the answers are right.

Nothing is combined and nothing is thresholded beyond the coin flip. Every case
is invented -- no real customer or investor text appears.
"""

from __future__ import annotations


def noul(qid: str, instructions: str, expected: bool) -> dict:
    """Probability that a statement is true. No confidence field: the
    probability is the answer, and P(x) + P(not x) does not make 1."""
    return {"id": qid, "type": "noul", "instructions": instructions, "expected": expected}


def choice(qid: str, instructions: str, options: dict[str, str], expected: str) -> dict:
    """One option wins. The key is what code switches on, the description is
    what the judge reads. Max 255 options."""
    return {"id": qid, "type": "choice", "instructions": instructions,
            "criteria": options, "expected": expected}


def score(qid: str, instructions: str, levels: list[str], expected: int) -> dict:
    """A position on an ordered rubric, 2-10 levels. The answer is the
    probability-weighted mean, so it lands between levels, not on one."""
    return {"id": qid, "type": "score", "instructions": instructions,
            "criteria": levels, "expected": expected}


def case(state: str, note: str, *questions: dict) -> dict:
    return {"state": state, "note": note, "questions": list(questions)}


LEADS = {
    "id": "leads",
    "title": "Inbound leads",
    "blurb": "five messages to a supplier, three questions written for each",
    "cases": [
        case(
            "Hi, we run three cafes in Lisbon and go through about 80kg of beans a "
            "month. Currently with a local roaster but the price went up. What "
            "would 80kg/month cost?",
            "a buyer shopping on price",
            noul("switching",
                 "This buyer already has a supplier and is shopping because that "
                 "supplier put its prices up.", True),
            choice("ask", "What is this message actually asking us for?", {
                "a_price": "A number they can compare against what they pay now.",
                "a_sample": "Something to try before committing.",
                "a_meeting": "Time with a person.",
                "support": "Help with something they already bought.",
                "nothing": "No ask at all.",
            }, "a_price"),
            score("readiness", "How ready is this buyer to place an order?", [
                "Not a buyer. Curious at most.",
                "Would buy one day, nothing in motion.",
                "Actively comparing suppliers right now.",
                "Will order as soon as one number is answered.",
            ], 2)),
        case(
            "We are a 40-person agency, our contract with Ascend ends in November "
            "and we are scoping replacements. Can we see pricing for 40 seats?",
            "a deadline and a headcount",
            noul("budget_approved",
                 "The message says a budget for this purchase has already been "
                 "approved.", False),
            choice("stage", "Where in buying is this company?", {
                "just_looking": "No purchase is contemplated yet.",
                "comparing": "Weighing named alternatives against each other.",
                "ready_to_buy": "Decided, and asking how to pay.",
                "already_committed": "Signed with someone else and telling us so.",
            }, "comparing"),
            score("account_size", "How large would this account be for a supplier?", [
                "One person, occasional.",
                "A small team.",
                "A department, dozens of seats.",
                "A whole company, hundreds.",
            ], 2)),
        case(
            "I bought a grinder from you in March and the burr is loose. Who do I "
            "email?",
            "support wearing a sales envelope",
            noul("existing_customer",
                 "The writer has already bought something from us.", True),
            choice("route_to", "Which team should answer this?", {
                "sales": "Someone who can quote and close.",
                "support": "Someone who can fix or replace a product.",
                "billing": "Someone who can change an invoice or a charge.",
                "nobody": "No reply is needed.",
            }, "support"),
            score("frustration", "How annoyed does the writer sound?", [
                "Not at all. A plain question.",
                "Mildly inconvenienced.",
                "Clearly unhappy.",
                "Angry, and saying so.",
            ], 0)),
        case(
            "GROW YOUR BUSINESS 10X WITH OUR SEO SERVICES. Reply STOP to opt out.",
            "a machine, selling at us",
            noul("about_what_we_sell",
                 "This message is about a product or service that we sell.", False),
            choice("sender_wants", "What does the sender want from this exchange?", {
                "to_buy_from_us": "To become our customer.",
                "to_sell_to_us": "To make us their customer.",
                "to_partner": "To work together as equals.",
                "nothing": "It is not addressed to anyone in particular.",
            }, "to_sell_to_us"),
            score("attention", "How much human attention does this deserve?", [
                "None. Delete it.",
                "A glance, then delete.",
                "A short reply.",
                "A proper answer today.",
            ], 0)),
        case(
            "Hi! I would like to explore a partnership where we resell your product "
            "in Japan.",
            "a reseller, not a buyer",
            noul("reseller",
                 "The writer wants to sell our product on to their own customers "
                 "rather than use it themselves.", True),
            choice("market", "Which market does the message name?", {
                "japan": "Japan.",
                "europe": "Somewhere in Europe.",
                "north_america": "The United States or Canada.",
                "not_stated": "No market is named.",
            }, "japan"),
            score("concreteness", "How concrete is the proposal?", [
                "An idea, with no detail at all.",
                "A direction, no terms.",
                "Terms sketched: volumes or territory.",
                "A full proposal, ready to negotiate.",
            ], 1)),
    ],
}

REFUNDS = {
    "id": "refunds",
    "title": "Refund requests",
    "blurb": "five messages to a support desk, three questions written for each",
    "cases": [
        case(
            "Ordered this 9 days ago, it does not fit. I would like a refund please.",
            "plain, in policy",
            noul("in_window",
                 "The purchase described happened within the last 30 days.", True),
            choice("reason", "Why does the customer want to send it back?", {
                "wrong_size": "It does not fit.",
                "damaged": "It arrived broken.",
                "changed_mind": "They simply no longer want it.",
                "not_as_described": "It is not what the listing promised.",
                "not_stated": "No reason is given.",
            }, "wrong_size"),
            score("handling", "How much work is settling this?", [
                "One click. Nothing to check.",
                "A quick look at the order.",
                "Someone has to make a judgement call.",
                "It needs a manager or an investigation.",
            ], 0)),
        case(
            "This is the fourth time I have had to write. Refund me or I am calling "
            "my bank and posting about this everywhere.",
            "a threat, after silence",
            noul("first_contact",
                 "This is the first time the customer has written about this "
                 "problem.", False),
            choice("threat", "What does the customer threaten to do?", {
                "chargeback": "Go to their bank and reverse the charge.",
                "publicity": "Post about it publicly.",
                "both": "Both of those.",
                "none": "No threat is made.",
            }, "both"),
            score("anger", "How angry is this message?", [
                "Calm.",
                "Irritated but polite.",
                "Openly angry.",
                "Furious, and finished with being patient.",
            ], 3)),
        case(
            "Please cancel my subscription before it renews on the 3rd.",
            "a deadline, calmly put",
            noul("time_critical",
                 "There is a date after which acting on this stops being useful.",
                 True),
            choice("wants", "What is the customer asking us to do?", {
                "cancel_now": "Stop the subscription before it charges again.",
                "pause": "Suspend it and keep the account.",
                "downgrade": "Move to a cheaper plan.",
                "refund_past_charges": "Return money already taken.",
            }, "cancel_now"),
            score("clarity", "How unambiguous is the request?", [
                "Cannot tell what they want.",
                "Roughly clear, details missing.",
                "Clear, with one thing to confirm.",
                "Completely clear. Act on it as written.",
            ], 3)),
        case(
            "Hi, the parcel arrived smashed. Photos attached. Happy with a "
            "replacement or a refund, whichever is easier.",
            "damaged, with proof and flexibility",
            noul("customer_at_fault",
                 "The damage described was caused by the customer rather than in "
                 "transit or manufacture.", False),
            choice("fault", "Where did the problem happen?", {
                "shipping": "In transit.",
                "manufacturing": "Before it was packed.",
                "customer": "After it arrived.",
                "unknown": "The message does not say.",
            }, "shipping"),
            score("flexibility", "How flexible is the customer about the remedy?", [
                "One remedy only; nothing else will do.",
                "A preference, but open to argument.",
                "Two remedies named as equally fine.",
                "Whatever we think is best.",
            ], 3)),
        case(
            "I never authorised this charge and I do not recognise your company.",
            "a dispute, not a refund",
            noul("disputes_purchase",
                 "The writer denies making the purchase at all.", True),
            choice("next_step", "What should happen first?", {
                "verify_identity": "Establish who they are and what was bought.",
                "issue_refund": "Send the money back immediately.",
                "send_receipt": "Show them what they bought.",
                "close_as_spam": "Ignore it.",
            }, "verify_identity"),
            score("fraud_risk", "How likely is fraud somewhere in this?", [
                "None. An ordinary request.",
                "Unlikely, but worth a glance.",
                "Possible. Check before paying out.",
                "Likely. Do not act without verification.",
            ], 2)),
    ],
}

SUPPORT = {
    "id": "support",
    "title": "Support triage",
    "blurb": "five tickets to a support desk, three questions written for each",
    "cases": [
        case(
            "Just wanted to say the new dashboard looks great.",
            "praise, needing nothing",
            noul("needs_reply",
                 "This ticket needs an answer before it can be closed.", False),
            choice("kind", "What kind of message is this?", {
                "praise": "A compliment. Nothing is wrong.",
                "question": "They want to know something.",
                "bug_report": "Something is broken.",
                "security_report": "Someone has access they should not.",
            }, "praise"),
            score("product_value", "How useful is this to the product team?", [
                "Nothing to learn from it.",
                "Pleasant, but not informative.",
                "Tells them something about what is landing.",
                "Worth acting on.",
            ], 1)),
        case(
            "How do I change the email on my account?",
            "the question every desk has answered a thousand times",
            noul("answerable_from_docs",
                 "A published help article would answer this completely, with "
                 "nothing left to ask.", True),
            choice("self_serve", "Can the user do this without us?", {
                "yes_in_settings": "It is a setting they can change themselves.",
                "yes_with_a_link": "Yes, once someone points them at the page.",
                "no_needs_agent": "An agent has to do it for them.",
                "no_needs_engineer": "It needs someone who can change data.",
            }, "yes_in_settings"),
            score("frequency", "How often does a support desk see this question?", [
                "Almost never.",
                "Now and then.",
                "Most weeks.",
                "Every day.",
            ], 3)),
        case(
            "The export button has been spinning for twenty minutes.",
            "one user, one broken feature",
            noul("affects_everyone",
                 "This is happening to every user of the product, not only the one "
                 "who wrote in.", False),
            choice("severity", "How bad is the failure described?", {
                "cosmetic": "It looks wrong but works.",
                "degraded": "It works, slowly or partly.",
                "feature_broken": "One feature does not work at all.",
                "service_down": "The whole product is unusable.",
            }, "feature_broken"),
            score("diagnosis", "How much digging before anyone can answer?", [
                "None. The cause is obvious.",
                "Check the account.",
                "Read the logs.",
                "Reproduce it before anything else.",
            ], 2)),
        case(
            "Our production API has been returning 500s for the last ten minutes.",
            "an outage, reported by a customer",
            noul("outage",
                 "A service is failing right now for more than one customer.", True),
            choice("page_who", "Who should be woken up for this?", {
                "support": "A support agent, in working hours.",
                "on_call_engineer": "Whoever is on call, immediately.",
                "account_manager": "The person who owns the relationship.",
                "nobody": "It can wait for the queue.",
            }, "on_call_engineer"),
            score("response_time", "How fast does someone have to respond?", [
                "Within the week.",
                "Within a day.",
                "Within the hour.",
                "Within minutes.",
            ], 3)),
        case(
            "Someone who left the company still has admin access to our workspace.",
            "access that should already be gone",
            noul("security_incident",
                 "This describes access that should have been removed and has not "
                 "been.", True),
            choice("action", "What is the first thing to do?", {
                "revoke_access": "Remove the access, then ask questions.",
                "audit_logs": "Find out what they did first.",
                "ask_for_details": "Write back for more information.",
                "ignore": "Nothing. It is not a problem.",
            }, "revoke_access"),
            score("blast_radius", "How much could go wrong while this stands?", [
                "Nothing. The access is harmless.",
                "One person's data.",
                "A team's data.",
                "The whole workspace.",
            ], 3)),
    ],
}

TURN = "The user asks: can you book me the same flight as last time, but a week later?"

MEMORY = {
    "id": "memory",
    "title": "Memory retrieval",
    "blurb": "five remembered facts against one turn, three questions for each",
    "cases": [
        case(
            TURN + "\nMemory: Flew LIS -> LHR on 12 Aug, TAP 1234, 07:20 departure.",
            "the flight the request points at",
            noul("is_the_referent",
                 "This is the flight the user means when they say the same flight.",
                 True),
            choice("use_for", "What is this fact for, in this request?", {
                "repeat_the_booking": "Working out what to book again.",
                "choose_a_seat": "Deciding where they sit.",
                "pick_a_payment": "Deciding how it gets paid for.",
                "discard": "Nothing here.",
            }, "repeat_the_booking"),
            score("completeness", "How much of the booking can be rebuilt from it?", [
                "None of it.",
                "The route only.",
                "Route and airline.",
                "Everything needed to book it again.",
            ], 3)),
        case(
            TURN + "\nMemory: Prefers aisle seats.",
            "a preference that changes the booking",
            noul("required_to_proceed",
                 "The assistant could not complete this booking at all without "
                 "knowing this.", False),
            choice("stage", "When does this fact start to matter?", {
                "choosing_flight": "While picking the flight itself.",
                "choosing_seat": "Once the flight is chosen.",
                "paying": "At payment.",
                "never": "Not on this trip.",
            }, "choosing_seat"),
            score("cost_if_ignored", "What happens if the assistant ignores it?", [
                "Nothing at all.",
                "Slightly worse trip.",
                "The user would notice and mind.",
                "The user would ask why it was booked that way.",
            ], 2)),
        case(
            TURN + "\nMemory: Passport expires 2031-04-02.",
            "true, and not needed here",
            noul("relevant_now",
                 "This fact bears on the trip the user is asking to book.", False),
            choice("why_kept", "Why would an assistant hold on to this at all?", {
                "travel_document": "It gates international travel.",
                "payment_detail": "It is needed to pay for things.",
                "preference": "It says what the user likes.",
                "unrelated": "There is no reason to keep it.",
            }, "travel_document"),
            score("shelf_life", "How long does this stay true?", [
                "Days.",
                "Months.",
                "A year or two.",
                "Years.",
            ], 3)),
        case(
            TURN + "\nMemory: Hates early mornings, asked to avoid flights before 9am.",
            "a rule that argues with the request",
            noul("conflicts",
                 "This sits against what the user just asked for, so an assistant "
                 "should raise it rather than quietly proceed.", True),
            choice("resolution", "What should the assistant do about the clash?", {
                "ask_the_user": "Point it out and let them choose.",
                "book_later_flight": "Quietly pick a later departure.",
                "ignore_preference": "Book exactly what was asked for.",
                "cancel": "Refuse to book anything.",
            }, "ask_the_user"),
            score("strength", "How strongly is the preference put?", [
                "Mentioned in passing.",
                "A stated like or dislike.",
                "A request made of the assistant.",
                "A standing rule.",
            ], 3)),
        case(
            TURN + "\nMemory: Uses the company Amex ending 4021 for travel.",
            "how it gets paid for",
            noul("payment_known",
                 "The assistant could pay for this trip without asking the user "
                 "anything further about payment.", True),
            choice("card_belongs_to", "Whose card is this?", {
                "the_company": "The company the user works for.",
                "the_user_personally": "The user's own.",
                "someone_else": "A third party's.",
                "not_stated": "The fact does not say.",
            }, "the_company"),
            score("sensitivity", "How carefully should this be repeated back?", [
                "Freely. It is not sensitive.",
                "Fine in a private reply.",
                "Only the last four digits.",
                "Never repeat it at all.",
            ], 2)),
    ],
}

INVESTORS = {
    "id": "investors",
    "title": "Inbound investor DMs",
    "blurb": "five messages to an angel, three questions written for each",
    "cases": [
        case(
            "hey - not raising right now, the repo is doing fine on its own. but a "
            "couple people have asked, so I left the last SAFE open at the same "
            "cap. if you want in, say the word.",
            "an ask dressed as a non-ask",
            noul("open_to_money",
                 "Despite saying they are not raising, the writer is offering the "
                 "reader a way to put money in.", True),
            choice("instrument", "What is being offered?", {
                "safe": "A SAFE.",
                "priced_round": "Equity at an agreed valuation.",
                "note": "A convertible note.",
                "none_named": "No instrument is named.",
            }, "safe"),
            score("urgency", "How soon would the reader have to act?", [
                "No deadline at all.",
                "Whenever they get round to it.",
                "Within a few weeks.",
                "Within days, or it is gone.",
            ], 1)),
        case(
            "Quarterly update: revenue is up 40% on Q2, churn flat, 14 months of "
            "runway. We are not raising and there is nothing to action here -- "
            "just keeping you posted.",
            "a real update with no ask",
            noul("raising",
                 "The company is currently raising money.", False),
            choice("relationship", "What does this message imply about the reader?", {
                "existing_investor": "They already hold something in the company.",
                "cold_contact": "The sender does not know them.",
                "customer": "They buy the product.",
                "unclear": "It gives no clue.",
            }, "existing_investor"),
            score("health", "On this message alone, how is the company doing?", [
                "Badly. This reads like trouble.",
                "Surviving.",
                "Solidly. Growing, in control.",
                "Very well. Growth and runway both.",
            ], 3)),
        case(
            "Cold email, sorry. We are raising a 2M seed for developer tooling, "
            "deck attached, minimum cheque is 50k and we are closing end of month.",
            "cold, but complete",
            noul("names_terms",
                 "The message names both an amount the reader would put in and a "
                 "date by which they must decide.", True),
            choice("round", "Which round is this?", {
                "pre_seed": "Before a seed round.",
                "seed": "A seed round.",
                "series_a": "A Series A.",
                "not_stated": "The round is not named.",
            }, "seed"),
            score("preparedness", "How prepared does this sender look?", [
                "Not at all. Nothing is ready.",
                "An idea and enthusiasm.",
                "Materials ready, terms roughly set.",
                "Everything ready; the round is running.",
            ], 3)),
        case(
            "Would love to pick your brain about how you think the memory tooling "
            "market shakes out over the next two years. Coffee sometime?",
            "an ask on time, not money",
            noul("asks_for_money",
                 "The message asks the reader for money.", False),
            choice("topic", "What do they want to talk about?", {
                "market_view": "How a market will develop.",
                "fundraising_help": "Getting introduced to money.",
                "hiring": "Finding people.",
                "product_feedback": "What they are building.",
            }, "market_view"),
            score("time_cost", "How much of the reader's time would yes cost?", [
                "Minutes, over text.",
                "One short call.",
                "An hour, in person.",
                "An ongoing commitment.",
            ], 2)),
        case(
            "DEAR INVESTOR. Our AI trading fund GUARANTEES 40% ANNUAL RETURNS with "
            "ZERO RISK. Minimum entry 25,000 USD. Reply now, places are limited.",
            "a scam, in capitals",
            noul("guarantees_returns",
                 "The message promises a return that no honest investment can "
                 "promise.", True),
            choice("disposition", "What should the reader do with this?", {
                "report_as_spam": "Report it and move on.",
                "reply_declining": "Send a polite no.",
                "forward_to_someone": "Pass it to someone who might want it.",
                "engage": "Ask for more information.",
            }, "report_as_spam"),
            score("plausibility", "How plausible is what it promises?", [
                "Impossible. Nobody can promise this.",
                "Wildly unlikely.",
                "Optimistic but conceivable.",
                "Entirely plausible.",
            ], 0)),
    ],
}

SUITES = {s["id"]: s for s in (LEADS, REFUNDS, SUPPORT, MEMORY, INVESTORS)}


def questions_of(suite_id: str) -> list[tuple[int, dict]]:
    """Every (case index, question) in a suite, in reading order. Fifteen of
    them: five Nouls, five Choices, five Scores, none repeated."""
    return [(i, q) for i, c in enumerate(SUITES[suite_id]["cases"]) for q in c["questions"]]


def answer_shape(question: dict) -> str:
    """What this question may answer, in one line -- the options for a Choice,
    the numbered levels for a Score, a probability for a Noul."""
    criteria = question.get("criteria")
    if isinstance(criteria, dict):
        return "one of: " + ", ".join(criteria)
    if isinstance(criteria, list):
        return f"{len(criteria)} levels: " + " | ".join(
            f"{i} {level}" for i, level in enumerate(criteria))
    return "a probability from 0 to 1"


def is_right(kind: str, expected, answer: dict) -> bool:
    """Did this answer match the label? One rule per primitive, and none of them
    involves a threshold anyone tuned: a Noul lands on a side of the coin flip,
    a Choice picks an option, a Score rounds to a level."""
    if kind == "noul":
        return (answer.get("noul", 0.0) >= 0.5) is bool(expected)
    if kind == "choice":
        return answer.get("choice") == expected
    return round(float(answer.get("score", -1))) == int(expected)


def suite_list() -> list[dict]:
    """What the dashboard draws the picker from. Carries the questions in full --
    they are the teaching content -- and never the labels."""
    out = []
    for s in SUITES.values():
        cases = []
        for c in s["cases"]:
            shown = []
            for q in c["questions"]:
                row = {"id": q["id"], "type": q["type"], "instructions": q["instructions"]}
                criteria = q.get("criteria")
                if isinstance(criteria, dict):
                    row["options"] = list(criteria)
                elif isinstance(criteria, list):
                    row["levels"] = criteria
                shown.append(row)
            cases.append({"note": c["note"], "questions": shown})
        out.append({"id": s["id"], "title": s["title"], "blurb": s["blurb"],
                    "cases": cases, "n": sum(len(c["questions"]) for c in s["cases"])})
    return out
