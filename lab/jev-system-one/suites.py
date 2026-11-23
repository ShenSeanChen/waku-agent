"""Four suites, four shapes of judgment. Every case is hand-labelled first.

Each suite gives three things: the questions to ask, a `decide` that turns the
answers into one label, and the cases. The split is the point -- the model
answers, the code decides. Change a threshold and nothing needs re-running.

The messages are invented. No real customer text is used anywhere here.
"""

from jev import choice, noul, score, value

# --- 1. inbound leads -------------------------------------------------------
# A supplier's inbox. Which of these deserves a human today?

LEADS = {
    "questions": {
        "buying": noul("The person writing this is buying for a business and has a "
                       "specific need, rather than browsing or asking in general."),
        "budget_signal": noul("This message mentions quantities, timelines, budget, or "
                              "an existing supplier -- evidence they have actually bought before."),
        "intent": choice("What does this person want right now?", {
            "quote": "A price for a specific quantity of something.",
            "sample": "To see or try the product before buying.",
            "support": "Help with something they already bought.",
            "partnership": "To sell something to us, or work together.",
            "nothing": "No clear ask. Vague, automated, or spam."}),
        "heat": score("How close is this person to placing an order?", [
            "Not a buyer at all.",
            "Curious. Months away, if ever.",
            "Comparing options seriously.",
            "Ready to order once one question is answered."]),
    },
    "decide": lambda a: (
        "ignore" if value(a["intent"]) == "nothing" or value(a["buying"]) < 0.4
        else "call_today" if value(a["heat"]) >= 2.0 and value(a["budget_signal"]) >= 0.5
        else "nurture"),
    "cases": [
        ("Hi, we run three cafes in Lisbon and go through about 80kg of beans a month. "
         "Currently with a local roaster but the price went up. What would 80kg/month cost?",
         "call_today", "quantity, incumbent supplier, clear ask"),
        ("Do you ship to Canada?", "nurture", "real question, no signal of size"),
        ("Hello Sir, I am interested in your products. Please send catalogue and best price.",
         "nurture", "the classic low-signal blast"),
        ("We're a 40-person agency, our contract with Ascend ends in November and we're "
         "scoping replacements. Can we see pricing for 40 seats?",
         "call_today", "headcount, deadline, incumbent"),
        ("I bought a grinder from you in March and the burr is loose. Who do I email?",
         "nurture", "support, not a lead -- routed away from sales"),
        ("GROW YOUR BUSINESS 10X WITH OUR SEO SERVICES. Reply STOP to opt out.",
         "ignore", "spam"),
        ("Been following you for a while, love what you're building. Any chance of a "
         "student discount one day?", "nurture", "warm but not buying"),
        ("Need 500 units of the 12oz size delivered before Black Friday. We've ordered "
         "from Kraft Supply before at $2.10/unit. Can you beat that?",
         "call_today", "quantity, deadline, price anchor"),
        ("Hi! I'd like to explore a partnership where we resell your product in Japan.",
         "nurture", "partnership, not an order"),
        ("what's the price", "nurture", "real but almost no information"),
        ("Following up on my email from Tuesday -- we have budget approved for Q4 and "
         "need to close this week. Are you the right person?",
         "call_today", "budget approved, urgency"),
        ("Dear Sir/Madam, We are a leading manufacturer of solar panels seeking "
         "distributors in your region.", "ignore", "inbound sales pitch at us"),
    ],
}

# --- 2. refund decisions ----------------------------------------------------
# A Stripe-style dispute queue. Code owns the money, the model owns the reading.

REFUNDS = {
    "questions": {
        "within_policy": noul("This request is covered by a 30-day no-questions refund "
                              "policy: the purchase was under 30 days ago and the customer "
                              "is asking for their money back on a normal order."),
        "fraud_smell": noul("Something here suggests abuse: a pattern of repeat refunds, "
                            "a claim that contradicts itself, or pressure and threats."),
        "kind": choice("What is the customer actually asking for?", {
            "refund": "Their money back.",
            "exchange": "A replacement or a different item.",
            "cancel": "To stop a subscription from renewing.",
            "complaint": "To be heard. No specific remedy asked for.",
            "unclear": "Cannot tell from the message."}),
    },
    "decide": lambda a: (
        "human" if value(a["fraud_smell"]) >= 0.4 or value(a["kind"]) == "unclear"
        else "refund" if value(a["kind"]) == "refund" and value(a["within_policy"]) >= 0.6
        else "human" if value(a["kind"]) == "refund"
        else "route"),
    "cases": [
        ("Ordered this 9 days ago, it doesn't fit. I'd like a refund please.",
         "refund", "plain, in policy"),
        ("This is the fourth time I've had to write. Refund me or I'm calling my bank "
         "and posting about this everywhere.", "human", "threat pattern"),
        ("Bought in January. It broke last week. I want my money back.",
         "human", "outside 30 days, needs judgement"),
        ("Can I swap the blue one for the green one?", "route", "exchange, not money"),
        ("Please cancel my subscription before it renews on the 3rd.",
         "route", "cancellation"),
        ("I've been a customer for six years and this is the worst service I've received.",
         "route", "complaint with no ask"),
        ("refund", "human", "one word, ambiguous"),
        ("Hi, the parcel arrived smashed. Photos attached. Happy with a replacement or "
         "a refund, whichever is easier.", "human", "two acceptable outcomes"),
        ("I never authorised this charge and I don't recognise your company.",
         "human", "possible fraud, never auto-refund"),
        ("Bought it two weeks ago, changed my mind, no complaints about the product.",
         "refund", "clean in-policy case"),
    ],
}

# --- 3. support triage ------------------------------------------------------
# One Score, used as a queue order rather than a category.

SUPPORT = {
    "questions": {
        "urgency": score("How fast does a human need to answer this?", [
            "No rush. Nobody is blocked.",
            "This week is fine.",
            "Today. Someone is stuck.",
            "Right now. Money or data is actively at risk."]),
    },
    "decide": lambda a: round(value(a["urgency"])),
    "cases": [
        ("Just wanted to say the new dashboard looks great.", 0, "praise"),
        ("Is there a dark mode on the roadmap?", 0, "feature curiosity"),
        ("How do I change the email on my account?", 1, "routine how-to"),
        ("Our invoices have the wrong VAT number on them, can that be fixed before "
         "month end?", 1, "deadline, not urgent"),
        ("I can't log in and I have a client demo in an hour.", 2, "blocked with a clock"),
        ("The export button has been spinning for twenty minutes.", 2, "blocked now"),
        ("We're being charged twice a month and have been for three months.",
         3, "money leaking"),
        ("Our production API has been returning 500s for the last ten minutes.",
         3, "outage"),
        ("Someone who left the company still has admin access to our workspace.",
         3, "security"),
        ("Small typo on your pricing page, 'recieve' should be 'receive'.", 0, "trivial"),
    ],
}

# --- 4. memory retrieval ----------------------------------------------------
# The Waku Memory question: which of these earns a slot in the context window?

_TURN = ("The user asks: \"can you book me the same flight as last time, but a week "
         "later?\"")

MEMORY = {
    "questions": {
        "earns_slot": score("An assistant is answering the user's current request and "
                            "can only carry a few remembered facts into it. How much "
                            "does leaving this one out change the answer?", [
            "Not at all. Irrelevant here.",
            "A little. Nice colour, not needed.",
            "A lot. The answer would be vaguer or need a question asked.",
            "Completely. Without this the request cannot be answered."]),
    },
    "decide": lambda a: "keep" if value(a["earns_slot"]) >= 1.8 else "drop",
    "cases": [
        (_TURN + "\nMemory: Flew LIS -> LHR on 12 Aug, TAP 1234, 07:20 departure.",
         "keep", "the actual referent of 'same flight'"),
        (_TURN + "\nMemory: Prefers aisle seats.", "keep", "changes the booking"),
        (_TURN + "\nMemory: Passport expires 2031-04-02.", "drop", "true, not needed now"),
        (_TURN + "\nMemory: Hates early mornings, asked to avoid flights before 9am.",
         "keep", "conflicts with the 07:20 -- worth surfacing"),
        (_TURN + "\nMemory: Allergic to shellfish.", "drop", "relevant to meals, not booking"),
        (_TURN + "\nMemory: Company travel policy caps economy fares at 400 EUR.",
         "keep", "constrains the purchase"),
        (_TURN + "\nMemory: Favourite coffee order is a flat white.", "drop", "noise"),
        (_TURN + "\nMemory: Has a British Airways frequent flyer number, no TAP account.",
         "keep", "affects which fare to pick"),
        (_TURN + "\nMemory: Lives in Lisbon.", "keep", "origin airport"),
        (_TURN + "\nMemory: Last week asked about hotels in Porto.", "drop",
         "recent but unrelated"),
        (_TURN + "\nMemory: Uses the company Amex ending 4021 for travel.",
         "keep", "payment method for the booking"),
        (_TURN + "\nMemory: Dislikes being called by their full name.", "drop", "tone, not task"),
    ],
}

SUITES = {"leads": LEADS, "refunds": REFUNDS, "support": SUPPORT, "memory": MEMORY}
